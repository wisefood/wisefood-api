"""The activity recorder.

One rule governs every line here: **recording must never affect the request
being recorded.** Not its result, not its latency, not its error behaviour. That
rule is what dictates the shape — a bounded in-memory queue drained by a
background task, rather than an insert on the request path:

* ``record()`` does no I/O. It snapshots the context and returns.
* The queue is bounded. When it fills, events are dropped and counted, because
  the alternative is a slow database turning into unbounded memory growth and
  then into a dead pod.
* Every failure is swallowed and counted. A broken analytics table degrades to
  no analytics, never to a failed request.

The counters are exposed at ``GET /analytics/health`` so "we have no data" can
be distinguished from "we are dropping data", which are very different problems
with identical symptoms in the console.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import random
import re
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import case, func

import context
from analytics.settings import CONSENT, SETTINGS

logger = logging.getLogger(__name__)

#: Identity that consent can strip from any row.
_IDENTITY_COLUMNS = ("user_id", "member_id", "household_id")
#: Columns that hold something the user typed, stripped alongside identity
#: unless raw-text capture is on and the user consented.
_TEXT_COLUMNS = {
    "search_query": ("raw_query",),
    "feedback": ("comment",),
    # A full user agent identifies a device across visits, so it is treated as
    # identity rather than as environment. The parsed browser, OS and form
    # factor stay: "12% of visits are iOS Safari" is about nobody. The address
    # is not listed because it never arrives whole — the recorder stores a /24
    # or /48 network and nothing finer, which is the point of truncating it.
    "client_session": ("user_agent",),
    # Only the free-form context object. An error's message, stack and
    # breadcrumbs are redacted and truncated at record time — that is where
    # the privacy control for them lives — and stripping them again here would
    # leave an error report with no error in it, which under opt-in consent
    # means every error report. `context` is arbitrary client-supplied JSON
    # with no shape to redact against, so it goes.
    "client_error": ("context",),
}
#: Keys inside `event.props` that may hold something a person typed. Dropped
#: with identity, like any other free text. `props` is client-supplied JSON, so
#: this is an allowlist of what is *removed*, plus a hard cap below on what a
#: single event may carry at all.
_TEXT_PROP_KEYS = frozenset({"q", "query", "question", "text", "comment", "term"})

#: Props that are usually safe but can be made unsafe by their value. `path`
#: is the case: the browser sends a route *pattern* (`/recipe-wrangler/[id]`),
#: which names a page and nobody in it, and stripping it outright emptied the
#: top-pages report for every user under opt-in consent — which is every user.
#: But an emitter could put a resolved URL there, and a resolved URL carries
#: ids and query strings. So the value decides, not the key.
_CONDITIONAL_PROP_KEYS = frozenset({"path", "from", "route", "url"})
#: A route pattern: slash-separated segments of word characters, with bracketed
#: or colon-prefixed parameters allowed. No query string, no origin, no spaces.
#: Vue Router spells a dynamic segment `:id()` — with the parentheses — so a
#: page pattern that arrived as `/sessions/:id()` was failing this check and
#: losing its `path`, which is why session-page views showed a `from` and no
#: destination. The UI now normalises those to `[id]`; the parens stay
#: permitted here so an older client is not silently stripped either.
_ROUTE_PATTERN = re.compile(r"^/[\w\-./\[\]():*]{0,200}$")

def _tunable(name: str, default: int, low: int, high: int) -> int:
    """A throughput knob from the environment, clamped to something sane.

    These are the numbers that decide how much traffic the recorder can absorb,
    and the right value depends on the pod's memory and the database's write
    capacity — neither of which this module can see. Tunable, therefore, but
    never to a value that would make the recorder the reason a pod dies.
    """
    try:
        value = int(os.getenv(name, "") or default)
    except ValueError:
        return default
    return max(low, min(value, high))


#: Rows that may wait in memory. At a thousand events a second this is roughly
#: fifty seconds of buffer, which is what makes a database hiccup invisible
#: rather than lossy. Bounded because unbounded means the pod dies instead.
_QUEUE_MAX = _tunable("ANALYTICS_QUEUE_MAX", 50_000, 1_000, 500_000)
#: Rows per INSERT. The round trip dominates at small sizes — a batch of a
#: thousand costs barely more than a batch of two hundred and does five times
#: the work.
_BATCH_MAX = _tunable("ANALYTICS_BATCH_MAX", 1_000, 50, 10_000)
#: Batches that may be in flight at once.
#:
#: This is the change that makes throughput scale. With a single writer the
#: loop was assemble, write, assemble, write — and during every write nothing
#: was being assembled, so the ceiling was one batch per round trip no matter
#: how fast events arrived. Writers now run alongside assembly, so a slow
#: insert costs latency rather than throughput.
#:
#: Bounded, and the bound is deliberate: each writer holds a connection from a
#: pool the rest of the gateway shares, and analytics must never be the reason
#: a user-facing request cannot get one.
_WRITER_CONCURRENCY = _tunable("ANALYTICS_WRITERS", 4, 1, 32)
_FLUSH_INTERVAL = 2.0
_SHUTDOWN_DRAIN_SECONDS = 5.0
#: How long the worker waits for a first event before re-reading settings
#: anyway. Without this a replica that had been paused — and so admitted no
#: events — never reached the refresh that runs after a dequeue, and stayed
#: paused after the admin un-paused it.
_IDLE_REFRESH_SECONDS = 15.0
#: How often unattributed service rows are matched to the gateway's own row for
#: the same request. Not on the write path — see `_correlate_loop`.
_CORRELATE_INTERVAL_SECONDS = 60.0
#: A client-reported `occurred_at` older than this is a wrong clock, not a
#: long-buffered batch, and is replaced with the arrival time.
_MAX_EVENT_AGE_SECONDS = 24 * 3600

_WS = re.compile(r"\s+")

#: Queued to ask the drain worker to finish and exit. Sent through the queue
#: rather than set as a flag so a worker blocked waiting for events wakes up
#: immediately instead of after the flush interval.
_STOP = object()


def normalize_query(text: Optional[str]) -> Optional[str]:
    """A stable key for "the same search".

    Casefold and collapse whitespace, nothing cleverer. Stemming and synonym
    folding belong next to the search engine that already does them, not here —
    a trending list that silently merges "vegan cake" into "vegetarian cake"
    would be wrong in a way nobody could see.
    """
    if not text:
        return None
    collapsed = _WS.sub(" ", text).strip().casefold()
    return collapsed or None


def query_hash(normalized: Optional[str]) -> Optional[str]:
    """A key for counting a query whose text we are not allowed to keep."""
    if not normalized:
        return None
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:32]


#: Route prefixes -> product surface. Matched on whole path segments, never by
#: substring: `/api/v1/users/me/analytics-consent` contains "/analytics" and is
#: a user's privacy setting, not console traffic. A substring match filed it
#: under `console`, which is how a per-app report acquires a phantom surface.
_APP_BY_SEGMENT = {
    "foodchat": "foodchat",
    "foodscholar": "foodscholar",
    "recipewrangler": "recipewrangler",
    "observability": "console",
    "analytics": "console",
}


def _clamp_time(value: Optional[datetime]) -> datetime:
    """A client's timestamp, kept only if it is plausible.

    Ahead of now would sort to the top of every report; more than a day behind
    is a wrong clock rather than a buffered batch. Either way the arrival time
    is the truer answer.
    """
    now = datetime.now(timezone.utc)
    if value is None:
        return now
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    if value > now or (now - value).total_seconds() > _MAX_EVENT_AGE_SECONDS:
        return now
    return value


def app_for_route(path: Optional[str]) -> str:
    """Which product surface a gateway route belongs to.

    Reads the first segment after the API prefix, so `/api/v1/foodchat/...`
    is FoodChat and `/api/v1/users/me/analytics-consent` is the platform.
    """
    if not path:
        return "platform"
    segments = [segment for segment in path.split("/") if segment]
    # Skip a root path and the `api/v1` prefix if present.
    while segments and segments[0] in ("rest", "api"):
        segments.pop(0)
    if segments and segments[0].startswith("v") and segments[0][1:].isdigit():
        segments.pop(0)
    if not segments:
        return "platform"
    return _APP_BY_SEGMENT.get(segments[0], "platform")


#: Web vitals the console understands. An allowlist, so a client cannot invent
#: a metric name and create a bucket nothing renders.
_VITAL_METRICS = frozenset({"LCP", "CLS", "INP", "TTFB", "FCP"})

#: Things that must never reach an error message or a stack trace in storage.
#: A browser error string is assembled from whatever was in scope — a failed
#: request often carries its own URL, and that URL often carries a token.
_REDACTIONS = (
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.]{2,}"), "<email>"),
    (re.compile(r"\b(?:ey[A-Za-z0-9_-]{10,}\.){2}[A-Za-z0-9_-]{10,}"), "<jwt>"),
    (re.compile(r"\b[Bb]earer\s+[A-Za-z0-9._~+/-]{16,}"), "<bearer>"),
    # A query string is where identifiers and search text end up. The path is
    # what makes an error findable; the parameters are what makes it personal.
    (re.compile(r"\?[^\s\"')]{1,400}"), "?<redacted>"),
    (re.compile(r"\b[0-9a-fA-F]{32,}\b"), "<hex>"),
    (re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b"), "<ip>"),
)

#: A stack frame, as every browser spells it. Chrome and Safari disagree about
#: whether the function name comes first, so both shapes are tried.
_FRAME = re.compile(
    r"(?:at\s+)?(?P<fn>[\w$.<>\[\]]+)?\s*\(?"
    r"(?P<file>(?:https?://|/|webpack|chunk)[^\s):]+)"
)
#: Frames from inside a bundled dependency say nothing about our own code, so
#: the culprit search skips them until it runs out of frames.
_VENDOR_FRAME = re.compile(r"node_modules|/_nuxt/(?:entry|vendor)|chunk-vendors")


def _redact(text: Optional[str], limit: int = 1000) -> Optional[str]:
    """An error string with the parts that identify somebody taken out."""
    if not text:
        return None
    value = str(text)
    for pattern, replacement in _REDACTIONS:
        value = pattern.sub(replacement, value)
    return value[:limit]


def _culprit(stack: Optional[str], url_path: Optional[str]) -> Optional[str]:
    """The frame an error should be filed under.

    The first frame that is our own code. Falling back to the first frame at
    all, and then to the page — an error with no usable stack still has to land
    somewhere a person can look, and "unknown" is not a place.
    """
    first: Optional[str] = None
    for line in (stack or "").splitlines():
        match = _FRAME.search(line.strip())
        if not match:
            continue
        location = match.group("file")
        function = match.group("fn")
        frame = f"{function} ({_short_path(location)})" if function else _short_path(location)
        if first is None:
            first = frame
        if not _VENDOR_FRAME.search(location):
            return frame[:255]
    return (first or url_path or None) and str(first or url_path)[:255]


def _short_path(location: str) -> str:
    """A frame's file, without the origin, the query or the line numbers."""
    cleaned = re.sub(r"^https?://[^/]+", "", location)
    cleaned = cleaned.split("?", 1)[0]
    return re.sub(r":\d+(?::\d+)?$", "", cleaned)


def _server_culprit(exc: BaseException) -> Optional[str]:
    """The deepest frame in our own code.

    Walked from the bottom up, because the innermost frame is usually inside
    a library — asyncpg, httpx, SQLAlchemy — and the useful answer is the last
    line of ours that led there. Falling back to the innermost frame of all
    when nothing matches, so an error thrown entirely inside a dependency
    still lands somewhere findable.
    """
    frames = traceback.extract_tb(exc.__traceback__)
    if not frames:
        return None
    for frame in reversed(frames):
        path = frame.filename or ""
        if "site-packages" in path or "/python3" in path:
            continue
        name = os.path.basename(path)
        return f"{frame.name} ({name}:{frame.lineno})"[:255]
    last = frames[-1]
    return f"{last.name} ({os.path.basename(last.filename)}:{last.lineno})"[:255]


def _fingerprint(
    app: str,
    kind: str,
    name: Optional[str],
    message: Optional[str],
    stack: Optional[str],
    url_path: Optional[str],
) -> str:
    """A stable key for "the same failure happening again".

    The culprit frame rather than the whole stack, and the message with its
    numbers removed. Both matter: a full stack differs between two browsers
    hitting one bug, and a message like "Cannot read x of undefined at index
    41" differs on every occurrence of the same fault. Grouping on the raw
    strings produces one group per user, which is the failure mode that makes
    error tracking useless.
    """
    skeleton = re.sub(r"\d+", "N", _redact(message) or "")
    parts = (app or "", kind or "", name or "", skeleton[:200], _culprit(stack, url_path) or "")
    return hashlib.sha256("|".join(parts).encode("utf-8", "replace")).hexdigest()[:64]


def _clean_breadcrumbs(crumbs: Optional[List[Any]], keep: int = 20) -> List[Any]:
    """The last few things that happened, with the text taken out.

    Breadcrumbs are what turn a stack trace into a reproduction and they are
    also the easiest place to leak something typed, so every string value in
    them goes through the same redaction as the message, and only the most
    recent are kept.
    """
    if not isinstance(crumbs, list):
        return []
    cleaned: List[Any] = []
    for crumb in crumbs[-keep:]:
        if isinstance(crumb, dict):
            cleaned.append(
                {
                    str(key)[:32]: (
                        _redact(value, limit=200) if isinstance(value, str) else value
                    )
                    for key, value in list(crumb.items())[:12]
                    # A crumb carrying what someone typed is not a breadcrumb.
                    if key not in _TEXT_PROP_KEYS
                }
            )
        elif isinstance(crumb, str):
            cleaned.append(_redact(crumb, limit=200))
    return cleaned


def _prop_is_safe(key: str, value: Any) -> bool:
    """Whether a conditionally-safe prop may survive identity stripping.

    Only path-shaped keys are conditional, and only their value decides. A
    route pattern is kept because it describes the application; anything with
    an origin, a query string or whitespace in it is a resolved URL and goes.
    """
    if key not in _CONDITIONAL_PROP_KEYS:
        return True
    if value is None:
        return True
    return isinstance(value, str) and bool(_ROUTE_PATTERN.match(value))


def _redact_label(value: Optional[str]) -> Optional[str]:
    """A control's own words, with anything that looks personal taken out."""
    if not value:
        return None
    cleaned = " ".join(_redact(str(value)).split())
    return cleaned[:80] or None


def _clamp_pct(value: Optional[int]) -> Optional[int]:
    """A coordinate in ten-thousandths of the page box, or nothing."""
    if value is None:
        return None
    try:
        return max(0, min(int(value), 10_000))
    except (TypeError, ValueError):
        return None


@dataclass
class Row:
    """One pending write: which table, and the column values for it."""

    table: str
    values: Dict[str, Any] = field(default_factory=dict)


@dataclass
class RecorderStats:
    enqueued: int = 0
    written: int = 0
    dropped_queue_full: int = 0
    dropped_disabled: int = 0
    dropped_sampled: int = 0
    #: Rows refused because one caller was reporting more than any honest
    #: client would. Distinct from a full queue: this is a client being
    #: stopped, that is the platform failing to keep up.
    dropped_rate_limited: int = 0
    write_errors: int = 0
    identities_stripped: int = 0
    #: Batches being written right now. A number that sits at the writer limit
    #: means the database, not the queue, is the ceiling.
    batches_in_flight: int = 0
    #: The deepest the queue has ever been. The figure that says how much
    #: headroom is left before events start being dropped — a depth reading
    #: taken after the spike has passed says nothing.
    queue_high_water: int = 0
    last_flush_at: Optional[str] = None
    last_error: Optional[str] = None

    def as_dict(self) -> Dict[str, Any]:
        return dict(self.__dict__)


class ActivityRecorder:
    """Queue events on the request path; write them on a background task."""

    def __init__(self, queue_max: int = _QUEUE_MAX):
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=queue_max)
        self._task: Optional[asyncio.Task] = None
        self._correlator: Optional[asyncio.Task] = None
        # Created lazily: an asyncio primitive built at import time binds to
        # whichever loop happened to be current, which is not this one.
        self._writers: Optional[asyncio.Semaphore] = None
        self._in_flight: set = set()
        self._enabled = False
        self.stats = RecorderStats()

    # ---------------------------------------------------------- lifecycle --
    def start(self, enabled: bool) -> None:
        self._enabled = enabled
        if not enabled:
            logger.info("analytics.disabled (ANALYTICS_ENABLED is not true)")
            return
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="analytics-recorder")
            logger.info("analytics.recorder_started queue_max=%s", self._queue.maxsize)
        if self._correlator is None or self._correlator.done():
            self._correlator = asyncio.create_task(
                self._correlate_loop(), name="analytics-correlator"
            )

    async def stop(self) -> None:
        """Let the worker finish what it holds, then stop.

        The worker spends most of its life *holding* a partly-assembled batch,
        waiting up to the flush interval for more events. Cancelling it there
        would throw that batch away — up to a full batch lost on every pod
        restart, which is exactly when you most want the record. So it is asked
        to finish through the queue rather than cancelled, and cancellation is
        only the fallback if it does not.

        Bounded on purpose: a terminating pod has a grace period, and losing a
        few events beats a container the orchestrator has to kill.
        """
        if self._task is None:
            return
        self._enabled = False
        try:
            self._queue.put_nowait(_STOP)
        except asyncio.QueueFull:
            # Nothing can be added, so the worker is already saturated; it will
            # be cancelled below and the queue lost either way.
            logger.warning("analytics.shutdown_queue_full")
        try:
            await asyncio.wait_for(self._task, timeout=_SHUTDOWN_DRAIN_SECONDS)
        except asyncio.TimeoutError:
            logger.warning(
                "analytics.shutdown_drain_timed_out queued=%d", self._queue.qsize()
            )
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: B014
                pass
        except Exception as exc:
            logger.warning("analytics.shutdown_drain_failed: %s", exc)
        # A batch handed to a writer is out of the queue and not yet in the
        # database. Without this it would be lost on every restart.
        try:
            await asyncio.wait_for(
                self._await_in_flight(), timeout=_SHUTDOWN_DRAIN_SECONDS
            )
        except (asyncio.TimeoutError, Exception):  # noqa: B014
            logger.warning(
                "analytics.shutdown_writers_unfinished in_flight=%d", len(self._in_flight)
            )
        self._task = None
        # The correlator holds no unwritten data, so unlike the drain it is
        # simply cancelled: anything it would have attributed is still in the
        # database, waiting for the next pod's first pass.
        if self._correlator is not None:
            self._correlator.cancel()
            try:
                await self._correlator
            except (asyncio.CancelledError, Exception):  # noqa: B014
                pass
            self._correlator = None
        # Last, so nothing is still trying to use it.
        from analytics.db import close as close_pool

        await close_pool()

    async def _correlate_loop(self) -> None:
        """Attribute service-reported rows to the user who made the request.

        A separate task from the drain because it must never delay a write:
        the drain holds a partly-assembled batch while it waits, and adding an
        UPDATE to that path would hold those rows in memory for the duration.

        Every replica runs one. The statement only touches rows that are still
        unattributed, so two replicas racing produce the same result as one.
        """
        from analytics.correlate import link_feedback_traces, resolve_identities

        while True:
            try:
                await asyncio.sleep(_CORRELATE_INTERVAL_SECONDS)
                if not self._enabled or SETTINGS.current().get("paused"):
                    continue
                await resolve_identities()
                await link_feedback_traces()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.debug("analytics.correlate_loop_failed", exc_info=True)

    @property
    def enabled(self) -> bool:
        return self._enabled

    def health(self) -> Dict[str, Any]:
        return {
            "enabled": self._enabled,
            # What the ceiling currently is, so a queue that is filling can be
            # read against the capacity that is meant to drain it.
            "writers": _WRITER_CONCURRENCY,
            "batch_max": _BATCH_MAX,
            "running": bool(self._task and not self._task.done()),
            "queue_depth": self._queue.qsize(),
            "queue_max": self._queue.maxsize,
            "settings": SETTINGS.current(),
            "stats": self.stats.as_dict(),
        }

    # ------------------------------------------------------------ enqueue --
    def _admit(self, app: str, capability: str, sampled: bool) -> bool:
        if not self._enabled:
            return False
        if not SETTINGS.collecting(app):
            self.stats.dropped_disabled += 1
            return False
        if not SETTINGS.captures(capability):
            self.stats.dropped_disabled += 1
            return False
        if sampled:
            # Clicks are an order of magnitude more numerous than anything
            # else, so they get their own rate rather than being forced to
            # share one with page views.
            rate = SETTINGS.sample_rate(capability)
            if rate < 1.0 and random.random() >= rate:
                self.stats.dropped_sampled += 1
                return False
        return True

    def _submit(self, row: Row) -> None:
        try:
            self._queue.put_nowait(row)
            self.stats.enqueued += 1
            depth = self._queue.qsize()
            if depth > self.stats.queue_high_water:
                self.stats.queue_high_water = depth
        except asyncio.QueueFull:
            # Counted, not logged: a full queue means thousands of events a
            # second, and a log line each would make the problem worse.
            self.stats.dropped_queue_full += 1
        except Exception:
            self.stats.dropped_queue_full += 1

    #: Fields a trusted caller may state on another party's behalf.
    _OVERRIDABLE = (
        "request_id",
        "client_session_id",
        "user_id",
        "member_id",
        "is_guest",
        "client",
    )

    @classmethod
    def _identity(cls, override: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Who this event belongs to.

        Normally the in-flight request's context. ``override`` exists for the
        signed service ingest: FoodChat knows a plan was generated and for which
        member, but it is reporting that over its own connection, so its request
        context names nobody. Only a caller that has proved it is a platform
        service may set this — the public ingest endpoint must never pass it, or
        a browser could file activity under someone else's name.
        """
        snapshot = context.snapshot()
        identity = {
            "request_id": snapshot["request_id"],
            "client_session_id": snapshot["client_session_id"],
            "user_id": snapshot["user_id"],
            "member_id": snapshot["member_id"],
            "household_id": snapshot["household_id"],
            "is_guest": bool(snapshot["is_guest"]),
            "client": snapshot["client"],
        }
        for field_name in cls._OVERRIDABLE:
            value = (override or {}).get(field_name)
            if value is not None:
                identity[field_name] = value
        return identity

    def record_event(
        self,
        event_type: str,
        *,
        app: Optional[str] = None,
        props: Optional[Dict[str, Any]] = None,
        route: Optional[str] = None,
        method: Optional[str] = None,
        status: Optional[int] = None,
        duration_ms: Optional[float] = None,
        locale: Optional[str] = None,
        capability: str = "client_events",
        sampled: bool = False,
        occurred_at: Optional[datetime] = None,
        identity: Optional[Dict[str, Any]] = None,
        inherit_route: bool = True,
    ) -> None:
        """Record one activity. Never raises, never blocks, never does I/O."""
        try:
            # The request context knows the route being served. For an event
            # the gateway itself observes mid-request that is the event's
            # route. For an event that *arrived over the ingest endpoint* it is
            # the ingest endpoint — and inheriting it filed every page view
            # under `/api/v1/analytics/events`, the URL the report travelled
            # over rather than the page it was about. The ingest handlers say
            # so explicitly; nobody else needs to.
            resolved_route = route or (context.get_route() if inherit_route else None)
            resolved_app = app or app_for_route(resolved_route)
            if not self._admit(resolved_app, capability, sampled):
                return
            identity = self._identity(identity)
            snapshot = context.snapshot()
            self._submit(
                Row(
                    "event",
                    {
                        **identity,
                        "roles": snapshot["roles"] or None,
                        "occurred_at": _clamp_time(occurred_at),
                        "received_at": datetime.now(timezone.utc),
                        "app": resolved_app,
                        "event_type": event_type[:64],
                        "route": (resolved_route or None) and resolved_route[:255],
                        "method": method,
                        "status": status,
                        "duration_ms": (
                            int(duration_ms) if duration_ms is not None else None
                        ),
                        # An explicit locale wins — a service reporting on
                        # behalf of a user knows the language it answered in.
                        # Otherwise it comes from the request, which is the only
                        # place the interface language is stated.
                        "locale": locale or snapshot.get("locale"),
                        "props": props or {},
                    },
                )
            )
        except Exception:
            # An analytics bug must not surface as a request failure.
            logger.debug("analytics.record_event_failed", exc_info=True)

    def record_search(
        self,
        *,
        surface: str,
        app: str,
        raw_query: Optional[str],
        filters: Optional[Dict[str, Any]] = None,
        result_count_first_pass: Optional[int] = None,
        result_count_final: Optional[int] = None,
        relaxed: bool = False,
        lexical_fallback: bool = False,
        latency_ms: Optional[float] = None,
        identity: Optional[Dict[str, Any]] = None,
    ) -> None:
        try:
            if not self._admit(app, "search_queries", sampled=False):
                return
            normalized = normalize_query(raw_query)
            identity = self._identity(identity)
            identity.pop("household_id", None)
            final = (
                result_count_final
                if result_count_final is not None
                else result_count_first_pass
            )
            self._submit(
                Row(
                    "search_query",
                    {
                        **identity,
                        "occurred_at": datetime.now(timezone.utc),
                        "app": app,
                        "surface": surface[:32],
                        "raw_query": raw_query,
                        "normalized_query": normalized,
                        "query_hash": query_hash(normalized),
                        "filters": filters or {},
                        "result_count_first_pass": result_count_first_pass,
                        "result_count_final": final,
                        "zero_result": bool(final == 0),
                        "relaxed": bool(relaxed),
                        "lexical_fallback": bool(lexical_fallback),
                        "latency_ms": (
                            int(latency_ms) if latency_ms is not None else None
                        ),
                    },
                )
            )
        except Exception:
            logger.debug("analytics.record_search_failed", exc_info=True)

    def record_llm_usage(
        self,
        *,
        app: str,
        feature: Optional[str] = None,
        provider: Optional[str] = None,
        model: Optional[str] = None,
        input_tokens: Optional[int] = None,
        output_tokens: Optional[int] = None,
        total_tokens: Optional[int] = None,
        cost_usd: Optional[float] = None,
        latency_ms: Optional[float] = None,
        trace_id: Optional[str] = None,
        identity: Optional[Dict[str, Any]] = None,
    ) -> None:
        try:
            if not self._admit(app, "llm_usage", sampled=False):
                return
            identity = self._identity(identity)
            identity.pop("client", None)
            identity.pop("is_guest", None)
            identity.pop("household_id", None)
            totals = total_tokens
            if totals is None and (input_tokens is not None or output_tokens is not None):
                totals = (input_tokens or 0) + (output_tokens or 0)
            # Nothing upstream reports money — every service sends token counts
            # and stops there — so the price is applied here or the whole spend
            # side of the console is a column of zeros. A model with no known
            # rate stays NULL rather than becoming 0, so a report can say how
            # much of its total it could not price.
            if cost_usd is None:
                from analytics.pricing import estimate_cost

                cost_usd = estimate_cost(
                    model=model,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    total_tokens=totals,
                    overrides=SETTINGS.get("pricing.overrides"),
                )
            self._submit(
                Row(
                    "llm_usage",
                    {
                        **identity,
                        "occurred_at": datetime.now(timezone.utc),
                        "trace_id": trace_id,
                        "app": app,
                        "feature": feature,
                        "provider": provider,
                        "model": model,
                        "input_tokens": input_tokens,
                        "output_tokens": output_tokens,
                        "total_tokens": totals,
                        "cost_usd": cost_usd,
                        "latency_ms": (
                            int(latency_ms) if latency_ms is not None else None
                        ),
                    },
                )
            )
        except Exception:
            logger.debug("analytics.record_llm_usage_failed", exc_info=True)

    def record_feedback(
        self,
        *,
        app: str,
        target_type: str,
        target_id: Optional[str],
        rating_kind: str,
        rating_value: Optional[str] = None,
        rating_value_num: Optional[float] = None,
        reason: Optional[str] = None,
        comment: Optional[str] = None,
        source: str = "ui",
        identity: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Record a feedback signal.

        Never sampled and never gated by a capture flag: feedback is a fact a
        person took the trouble to give, and dropping one is not the same kind
        of loss as dropping a page view.
        """
        try:
            if not self._enabled or not SETTINGS.collecting(app):
                return
            identity = self._identity(identity)
            identity.pop("is_guest", None)
            identity.pop("client", None)
            identity.pop("household_id", None)
            self._submit(
                Row(
                    "feedback",
                    {
                        **identity,
                        "occurred_at": datetime.now(timezone.utc),
                        "app": app,
                        "target_type": target_type[:32],
                        "target_id": (target_id or None) and str(target_id)[:512],
                        "rating_kind": rating_kind[:16],
                        "rating_value": (
                            (rating_value or None) and str(rating_value)[:32]
                        ),
                        "rating_value_num": rating_value_num,
                        "reason": (reason or None) and str(reason)[:128],
                        "comment": comment,
                        "source": source[:16],
                        "status": "new",
                    },
                )
            )
        except Exception:
            logger.debug("analytics.record_feedback_failed", exc_info=True)

    # ---------------------------------------------------- real user monitoring --
    def record_client_session(
        self,
        *,
        session_id: str,
        user_agent: Optional[str] = None,
        ip: Optional[str] = None,
        country: Optional[str] = None,
        app: Optional[str] = None,
        release: Optional[str] = None,
        screen_w: Optional[int] = None,
        screen_h: Optional[int] = None,
        viewport_w: Optional[int] = None,
        viewport_h: Optional[int] = None,
        device_pixel_ratio: Optional[float] = None,
        color_scheme: Optional[str] = None,
        reduced_motion: Optional[bool] = None,
        timezone_name: Optional[str] = None,
        connection: Optional[str] = None,
        identity: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Record the machine one browser session is running on.

        Parsed here rather than in the browser for two reasons. A client can
        claim to be anything, and the parsed fields are what every report
        groups by — so a page that lied would not just mislabel itself, it
        would create a new bucket. And the address is only visible on this
        side, which is where it must be truncated before it can be stored.
        """
        try:
            if not session_id:
                return
            if not self._admit(app or "platform", "client_sessions", sampled=False):
                return
            from analytics.device import parse_user_agent, truncate_ip

            parsed = parse_user_agent(user_agent)
            identity = self._identity(identity)
            identity.pop("request_id", None)
            identity.pop("client_session_id", None)
            identity.pop("household_id", None)
            now = datetime.now(timezone.utc)
            snapshot = context.snapshot()
            self._submit(
                Row(
                    "client_session",
                    {
                        **identity,
                        "session_id": session_id,
                        "started_at": now,
                        "last_seen_at": now,
                        "app": app,
                        "release": release,
                        "user_agent": (user_agent or None) and str(user_agent)[:512],
                        "browser": parsed["browser"],
                        "browser_version": parsed["browser_version"],
                        "os": parsed["os"],
                        "os_version": parsed["os_version"],
                        "device_type": parsed["device_type"],
                        "is_bot": bool(parsed["is_bot"]),
                        "screen_w": screen_w,
                        "screen_h": screen_h,
                        "viewport_w": viewport_w,
                        "viewport_h": viewport_h,
                        "device_pixel_ratio": device_pixel_ratio,
                        "color_scheme": color_scheme,
                        "reduced_motion": reduced_motion,
                        # Never the address. See analytics.device.
                        "ip_prefix": truncate_ip(ip),
                        "country": country,
                        "timezone": timezone_name,
                        "connection": connection,
                        "locale": snapshot.get("locale"),
                    },
                )
            )
        except Exception:
            logger.debug("analytics.record_client_session_failed", exc_info=True)

    def record_client_error(
        self,
        *,
        app: str,
        kind: str,
        name: Optional[str] = None,
        message: Optional[str] = None,
        stack: Optional[str] = None,
        url_path: Optional[str] = None,
        line_no: Optional[int] = None,
        col_no: Optional[int] = None,
        handled: bool = False,
        breadcrumbs: Optional[List[Any]] = None,
        context_data: Optional[Dict[str, Any]] = None,
        release: Optional[str] = None,
        user_agent: Optional[str] = None,
        occurred_at: Optional[datetime] = None,
        identity: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Record something breaking in a browser.

        Never sampled. An error that happens to one person in a thousand is the
        one worth having, and a sampled crash report is worse than no crash
        report because it makes a real fault look intermittent.
        """
        try:
            # Deliberately not gated on a capture flag the way clicks are: the
            # `errors` capability exists so it *can* be turned off, but it
            # defaults on and is never sampled.
            if not self._admit(app, "errors", sampled=False):
                return
            identity = self._identity(identity)
            identity.pop("household_id", None)
            clean_message = _redact(message)
            # The device is copied onto the error rather than joined to the
            # session row, for two reasons: an error outlives a session that
            # retention has trimmed, and "which browser is this happening on"
            # is the first question asked of an error list, so it must not
            # cost a join. Parsed from this request's own header — the report
            # came from the browser it is about — rather than read back from
            # the database, which would put a query on the error path.
            from analytics.device import parse_user_agent

            device = parse_user_agent(user_agent)
            self._submit(
                Row(
                    "client_error",
                    {
                        **identity,
                        "occurred_at": _clamp_time(occurred_at),
                        "received_at": datetime.now(timezone.utc),
                        "app": app,
                        "release": release,
                        "kind": str(kind)[:24],
                        "name": (name or None) and str(name)[:128],
                        "message": clean_message,
                        "culprit": _culprit(stack, url_path),
                        "stack": _redact(stack, limit=8000),
                        "url_path": (url_path or None) and str(url_path)[:255],
                        "line_no": line_no,
                        "col_no": col_no,
                        "handled": bool(handled),
                        "breadcrumbs": _clean_breadcrumbs(breadcrumbs),
                        "context": context_data or {},
                        "browser": device["browser"],
                        "os": device["os"],
                        "device_type": device["device_type"],
                        # Grouping is computed here, so two browsers reporting
                        # the same fault land in one group and a client cannot
                        # split or merge groups by choosing its own key.
                        "fingerprint": _fingerprint(
                            app, kind, name, clean_message, stack, url_path
                        ),
                    },
                )
            )
        except Exception:
            logger.debug("analytics.record_client_error_failed", exc_info=True)

    def record_server_error(
        self,
        *,
        exc: BaseException,
        app: Optional[str] = None,
        route: Optional[str] = None,
        method: Optional[str] = None,
        status: int = 500,
        handled: bool = False,
    ) -> None:
        """Record an exception raised on this side of the wire.

        Without this the platform recorded only what broke in a *browser*,
        which is the smaller half. A 500 was visible in the request table as a
        status code and nowhere as a cause: to find out what actually threw you
        had to go to the pod logs, know which replica served it, and get there
        before the log rotated.

        Grouped by the same fingerprint as a browser error, so one console
        lists both and a failure that spans the two — a frontend call failing
        because a handler threw — appears as two groups you can put side by
        side rather than one you can see and one you cannot.

        Never raises, and deliberately does no I/O: it is called from an
        exception handler, and an analytics failure there would replace a
        useful error with a useless one.
        """
        try:
            resolved_app = app or app_for_route(route or context.get_route())
            if not self._admit(resolved_app, "errors", sampled=False):
                return
            identity = self._identity()
            identity.pop("household_id", None)
            stack = "".join(
                traceback.format_exception(type(exc), exc, exc.__traceback__)
            )
            message = _redact(str(exc) or exc.__class__.__name__)
            self._submit(
                Row(
                    "client_error",
                    {
                        **identity,
                        "occurred_at": datetime.now(timezone.utc),
                        "received_at": datetime.now(timezone.utc),
                        "app": resolved_app,
                        "release": os.getenv("WISEFOOD_RELEASE") or None,
                        # A server exception is its own kind. The ingest schema
                        # does not accept it, so a browser cannot claim its
                        # errors happened on the server.
                        "kind": "server",
                        "name": exc.__class__.__name__[:128],
                        "message": message,
                        "culprit": _server_culprit(exc),
                        "stack": _redact(stack, limit=8000),
                        # The verb belongs here rather than in `context`,
                        # which consent stripping nulls: it is part of what
                        # identifies the endpoint, and a failing DELETE is not
                        # the same fault as a failing GET on the same path.
                        "url_path": (
                            f"{method} {route}".strip()[:255]
                            if route
                            else (method or None)
                        ),
                        "handled": bool(handled),
                        "breadcrumbs": [],
                        "context": {},
                        "fingerprint": _fingerprint(
                            resolved_app,
                            "server",
                            exc.__class__.__name__,
                            message,
                            stack,
                            f"{method} {route}",
                        ),
                    },
                )
            )
        except Exception:
            logger.debug("analytics.record_server_error_failed", exc_info=True)

    def record_interaction(
        self,
        *,
        app: str,
        path: str,
        kind: str = "click",
        element_key: Optional[str] = None,
        element_label: Optional[str] = None,
        element_role: Optional[str] = None,
        page_path: Optional[str] = None,
        x_pct: Optional[int] = None,
        y_pct: Optional[int] = None,
        viewport_w: Optional[int] = None,
        viewport_h: Optional[int] = None,
        depth_pct: Optional[int] = None,
        repeats: int = 1,
        occurred_at: Optional[datetime] = None,
        identity: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Record a click, a rage click, a dead click, or a scroll depth.

        The highest-volume thing the platform can record, so it is sampled and
        off by default. A rage click arrives already collapsed — one row with a
        repeat count — because the interesting fact is that somebody clicked
        five times, not that there were five clicks.
        """
        try:
            if not path:
                return
            # Rage and dead clicks are the actionable minority and are never
            # thrown away by sampling; ordinary clicks are the volume.
            sampled = kind == "click"
            if not self._admit(app, "interactions", sampled=sampled):
                return
            identity = self._identity(identity)
            for field_name in ("request_id", "member_id", "household_id", "client"):
                identity.pop(field_name, None)
            self._submit(
                Row(
                    "ui_interaction",
                    {
                        **identity,
                        "occurred_at": _clamp_time(occurred_at),
                        "app": app,
                        "path": str(path)[:255],
                        "kind": str(kind)[:16],
                        "element_key": (element_key or None) and str(element_key)[:160],
                        # Words a browser read off the page, so redacted like
                        # any other captured text: a control's own label is
                        # chrome, but nothing stops a page putting a name in
                        # an aria-label, and this column is readable by
                        # everyone with console access.
                        "element_label": _redact_label(element_label),
                        "element_role": (element_role or None) and str(element_role)[:32],
                        "page_path": (page_path or None) and str(page_path)[:255],
                        "x_pct": _clamp_pct(x_pct),
                        "y_pct": _clamp_pct(y_pct),
                        "viewport_w": viewport_w,
                        "viewport_h": viewport_h,
                        "depth_pct": _clamp_pct(depth_pct),
                        "repeats": max(1, min(int(repeats or 1), 32767)),
                    },
                )
            )
        except Exception:
            logger.debug("analytics.record_interaction_failed", exc_info=True)

    def record_web_vital(
        self,
        *,
        app: str,
        path: str,
        metric: str,
        value: float,
        rating: Optional[str] = None,
        navigation_type: Optional[str] = None,
        device_type: Optional[str] = None,
        occurred_at: Optional[datetime] = None,
        identity: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Record how fast a page felt, as the browser measured it.

        Not the same thing as the route latency already recorded per request. A
        route that answers in 80ms can still take four seconds to become
        usable, and only the browser is in a position to say so.
        """
        try:
            if not path or metric not in _VITAL_METRICS:
                return
            if not self._admit(app, "vitals", sampled=True):
                return
            identity = self._identity(identity)
            for field_name in ("request_id", "member_id", "household_id", "client", "is_guest"):
                identity.pop(field_name, None)
            self._submit(
                Row(
                    "web_vital",
                    {
                        **identity,
                        "occurred_at": _clamp_time(occurred_at),
                        "app": app,
                        "path": str(path)[:255],
                        "metric": metric,
                        "value": float(value),
                        "rating": (rating or None) and str(rating)[:20],
                        "navigation_type": (
                            (navigation_type or None) and str(navigation_type)[:16]
                        ),
                        "device_type": (device_type or None) and str(device_type)[:16],
                    },
                )
            )
        except Exception:
            logger.debug("analytics.record_web_vital_failed", exc_info=True)

    # -------------------------------------------------------------- drain --
    async def _run(self) -> None:
        while True:
            try:
                if await self._drain_once():
                    return
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.stats.write_errors += 1
                self.stats.last_error = f"{exc.__class__.__name__}: {exc}"
                logger.warning("analytics.drain_failed: %s", exc, exc_info=True)
                # Back off rather than spin on a persistent failure.
                await asyncio.sleep(5.0)

    async def _drain_once(self) -> bool:
        """Assemble one batch and write it. True means "and now stop"."""
        while True:
            try:
                first = await asyncio.wait_for(
                    self._queue.get(), timeout=_IDLE_REFRESH_SECONDS
                )
                break
            except asyncio.TimeoutError:
                # Idle. Re-read settings so a pause, an app switch or a capture
                # flag flipped elsewhere is noticed even though no event is
                # arriving to trigger the refresh below.
                await SETTINGS.refresh_if_stale()
        if first is _STOP:
            await self._drain_remaining()
            return True

        batch = [first]
        stopping = False
        deadline = time.monotonic() + _FLUSH_INTERVAL
        while len(batch) < _BATCH_MAX:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                item = await asyncio.wait_for(self._queue.get(), timeout=remaining)
            except asyncio.TimeoutError:
                break
            if item is _STOP:
                stopping = True
                break
            batch.append(item)

        await SETTINGS.refresh_if_stale()
        await self._dispatch(batch)
        if stopping:
            await self._drain_remaining()
        return stopping

    async def _dispatch(self, batch: List[Row]) -> None:
        """Hand a batch to a writer and go back to assembling.

        The semaphore is acquired *here*, in the drain, rather than inside the
        writer — so when every writer is busy this awaits, the drain stops
        consuming, and the pressure lands on the bounded queue, which has a
        defined behaviour for being full. Acquiring inside the task instead
        would let an unbounded pile of pending tasks accumulate, which is the
        same overflow with none of the accounting.
        """
        if not batch:
            return
        writers = self._writer_slots()
        await writers.acquire()
        task = asyncio.create_task(self._write_and_release(batch, writers))
        # Held so a batch in flight cannot be garbage collected mid-write, and
        # so shutdown knows what it is waiting for.
        self._in_flight.add(task)
        task.add_done_callback(self._in_flight.discard)

    async def _write_and_release(self, batch: List[Row], writers) -> None:
        self.stats.batches_in_flight += 1
        try:
            await self._write(batch)
        finally:
            self.stats.batches_in_flight -= 1
            writers.release()

    def _writer_slots(self) -> asyncio.Semaphore:
        if self._writers is None:
            self._writers = asyncio.Semaphore(_WRITER_CONCURRENCY)
        return self._writers

    async def _await_in_flight(self) -> None:
        """Let every dispatched batch finish. Called only on the way out."""
        while self._in_flight:
            await asyncio.gather(*list(self._in_flight), return_exceptions=True)

    async def _drain_remaining(self) -> None:
        """Write everything already queued, without waiting for more."""
        while not self._queue.empty():
            batch: List[Row] = []
            while len(batch) < _BATCH_MAX and not self._queue.empty():
                item = self._queue.get_nowait()
                if item is not _STOP:
                    batch.append(item)
            if batch:
                # Dispatched, not written inline: shutdown has a grace period
                # and the last few batches are exactly the ones most likely to
                # be lost, so they go out in parallel too.
                await self._dispatch(batch)
        await self._await_in_flight()

    async def _apply_consent(self, batch: List[Row]) -> None:
        """Strip identity from rows belonging to users who have not consented.

        Evaluated when the batch is *written*, not when the event was recorded.
        That is a deliberate asymmetry and it leans the same way both times: a
        user who withdraws consent also un-attributes the events still sitting
        in the queue, and a user who grants it does not retroactively attribute
        them. The window is one flush interval, so in normal use an action taken
        after granting consent is attributed and one taken after withdrawing is
        not — which is what a person clicking the toggle expects.
        """
        user_ids = {
            row.values.get("user_id") for row in batch if row.values.get("user_id")
        }
        allowed = await CONSENT.allowed_for(user_ids) if user_ids else set()
        keep_text = SETTINGS.captures("raw_query_text")
        for row in batch:
            user_id = row.values.get("user_id")
            if user_id and user_id in allowed:
                if not keep_text:
                    for column in _TEXT_COLUMNS.get(row.table, ()):
                        row.values[column] = None
                continue
            if user_id:
                self.stats.identities_stripped += 1
            for column in _IDENTITY_COLUMNS:
                if column in row.values:
                    row.values[column] = None
            if "roles" in row.values:
                row.values["roles"] = None
            for column in _TEXT_COLUMNS.get(row.table, ()):
                row.values[column] = None
            # `props` is client-supplied JSON and can hold whatever a page put
            # there, including the text of a search. Free-text keys go with the
            # identity; the counters and ids that make the event useful stay.
            props = row.values.get("props")
            if isinstance(props, dict) and props:
                row.values["props"] = {
                    key: value
                    for key, value in props.items()
                    if key not in _TEXT_PROP_KEYS and _prop_is_safe(key, value)
                }

    @staticmethod
    def _tables():
        from sql import (
            ActivityEvent,
            ClientError,
            ClientSession,
            FeedbackRecord,
            LLMUsage,
            SearchQuery,
            UIInteraction,
            WebVital,
        )

        return {
            "event": ActivityEvent.__table__,
            "search_query": SearchQuery.__table__,
            "llm_usage": LLMUsage.__table__,
            "feedback": FeedbackRecord.__table__,
            "client_session": ClientSession.__table__,
            "client_error": ClientError.__table__,
            "ui_interaction": UIInteraction.__table__,
            "web_vital": WebVital.__table__,
        }

    @classmethod
    def _coerce(cls, table, values: Dict[str, Any]) -> Dict[str, Any]:
        """Fit a row to its table before it reaches Postgres.

        The public ingest validates its input; the signed service ingest
        trusts its callers, and a trusted caller can still be wrong. One
        70-character session id or a token count of "n/a" made the whole
        multi-row INSERT fail, and the recorder drops a failed batch by design
        — so one bad field from one service erased up to 200 rows belonging to
        everyone. Strings are truncated to their column, numbers coerced or
        dropped, unknown keys removed.
        """
        from sqlalchemy import Boolean, Integer, Numeric, String

        out: Dict[str, Any] = {}
        for column in table.columns:
            if column.name not in values:
                continue
            value = values[column.name]
            if value is None:
                out[column.name] = None
                continue
            kind = column.type
            if isinstance(kind, String):
                text = str(value)
                out[column.name] = text[: kind.length] if kind.length else text
            elif isinstance(kind, Integer):
                try:
                    out[column.name] = int(value)
                except (TypeError, ValueError, OverflowError):
                    out[column.name] = None
            elif isinstance(kind, Numeric):
                try:
                    out[column.name] = float(value)
                except (TypeError, ValueError, OverflowError):
                    out[column.name] = None
            elif isinstance(kind, Boolean):
                out[column.name] = bool(value)
            else:
                out[column.name] = value
        return out

    async def _write(self, batch: List[Row]) -> None:
        if not batch:
            return
        try:
            await self._apply_consent(batch)
        except Exception as exc:
            # Could not establish consent: drop the batch rather than write
            # identities we are not sure we may keep.
            self.stats.write_errors += 1
            self.stats.last_error = f"consent: {exc}"
            logger.warning("analytics.consent_failed_batch_dropped: %s", exc)
            return

        tables = self._tables()
        grouped: Dict[str, List[Dict[str, Any]]] = {}
        for row in batch:
            table = tables.get(row.table)
            if table is None:
                continue
            grouped.setdefault(row.table, []).append(self._coerce(table, row.values))

        if await self._insert(grouped, tables):
            self.stats.written += len(batch)
            self.stats.last_flush_at = datetime.now(timezone.utc).isoformat()
            return

        # The batch failed as a whole. Before giving up on all of it, try each
        # row alone, so one row Postgres rejects costs one row and not the 199
        # unrelated ones queued beside it. Rows that still fail are lost on
        # purpose: retrying a structurally bad insert would retry it forever.
        saved = 0
        for name, rows in grouped.items():
            for values in rows:
                if await self._insert({name: [values]}, tables, quiet=True):
                    saved += 1
        self.stats.written += saved
        self.stats.write_errors += 1
        if saved:
            self.stats.last_flush_at = datetime.now(timezone.utc).isoformat()
        logger.warning(
            "analytics.write_failed batch=%d saved_individually=%d",
            len(batch),
            saved,
        )

    @staticmethod
    async def _upsert_sessions(db, table, rows) -> None:
        """Insert a session row, or fold what is new into the existing one.

        `COALESCE(excluded, existing)` in that order everywhere: a later report
        knows more than an earlier one — the first arrives before the user has
        signed in, the browser learns its own viewport only after layout — and
        a NULL in a later report means "still don't know", never "forget".
        `started_at` is the exception and keeps the earliest value, because a
        session starts once.
        """
        from sqlalchemy.dialects.postgresql import insert as pg_insert

        # Two reports for one session inside a single batch would make the
        # statement update the same row twice, which Postgres refuses. The
        # later one wins, having merged the earlier's fields.
        merged: Dict[str, Dict[str, Any]] = {}
        for row in rows:
            key = row.get("session_id")
            if not key:
                continue
            if key in merged:
                merged[key].update({k: v for k, v in row.items() if v is not None})
            else:
                merged[key] = dict(row)

        for values in merged.values():
            statement = pg_insert(table).values(**values)
            updates = {
                column.name: func.coalesce(
                    statement.excluded[column.name], table.c[column.name]
                )
                for column in table.columns
                if column.name not in ("session_id", "started_at", "events", "errors", "pages")
            }
            updates["started_at"] = func.least(
                statement.excluded.started_at, table.c.started_at
            )
            updates["last_seen_at"] = func.greatest(
                statement.excluded.last_seen_at, table.c.last_seen_at
            )
            await db.execute(
                statement.on_conflict_do_update(
                    index_elements=["session_id"], set_=updates
                )
            )

    @staticmethod
    async def _roll_up_error_groups(db, rows) -> None:
        """Keep one row per distinct failure, with its counts and its span.

        Occurrences are cheap to write and expensive to read; a console opens
        on the groups. Counted here in the same transaction as the occurrences
        so the two can never disagree.
        """
        from sqlalchemy.dialects.postgresql import insert as pg_insert

        from sql import ErrorGroup

        by_fingerprint: Dict[str, Dict[str, Any]] = {}
        for row in rows:
            fingerprint = row.get("fingerprint")
            if not fingerprint:
                continue
            group = by_fingerprint.setdefault(
                fingerprint,
                {
                    "fingerprint": fingerprint,
                    "app": row.get("app"),
                    "kind": row.get("kind"),
                    "name": row.get("name"),
                    "message": row.get("message"),
                    "culprit": row.get("culprit"),
                    "occurrences": 0,
                    "sessions": 0,
                    "users": 0,
                    "first_release": row.get("release"),
                    "last_release": row.get("release"),
                    "_sessions": set(),
                    "_users": set(),
                    "first_seen_at": row.get("occurred_at"),
                    "last_seen_at": row.get("occurred_at"),
                },
            )
            group["occurrences"] += 1
            if row.get("client_session_id"):
                group["_sessions"].add(row["client_session_id"])
            if row.get("user_id"):
                group["_users"].add(row["user_id"])
            when = row.get("occurred_at")
            if when:
                if not group["first_seen_at"] or when < group["first_seen_at"]:
                    group["first_seen_at"] = when
                if not group["last_seen_at"] or when > group["last_seen_at"]:
                    group["last_seen_at"] = when

        table = ErrorGroup.__table__
        for group in by_fingerprint.values():
            # Distinct within this batch only. Across batches the count drifts
            # upward, which is the right direction for a figure read as "how
            # many people is this hitting" and not worth a second query per
            # error to make exact.
            group["sessions"] = len(group.pop("_sessions"))
            group["users"] = len(group.pop("_users"))
            statement = pg_insert(table).values(**group)
            await db.execute(
                statement.on_conflict_do_update(
                    index_elements=["fingerprint"],
                    set_={
                        "last_seen_at": func.greatest(
                            statement.excluded.last_seen_at, table.c.last_seen_at
                        ),
                        "first_seen_at": func.least(
                            statement.excluded.first_seen_at, table.c.first_seen_at
                        ),
                        "occurrences": table.c.occurrences + statement.excluded.occurrences,
                        "sessions": table.c.sessions + statement.excluded.sessions,
                        "users": table.c.users + statement.excluded.users,
                        "last_release": func.coalesce(
                            statement.excluded.last_release, table.c.last_release
                        ),
                        "message": func.coalesce(
                            statement.excluded.message, table.c.message
                        ),
                        "culprit": func.coalesce(
                            statement.excluded.culprit, table.c.culprit
                        ),
                        # A group somebody marked resolved that happens again is
                        # not resolved. Reopening it is the whole point of
                        # keeping the status here rather than in a note.
                        "status": case(
                            (table.c.status == "resolved", "new"),
                            else_=table.c.status,
                        ),
                    },
                )
            )

    async def _insert(self, grouped, tables, *, quiet: bool = False) -> bool:
        from sqlalchemy import insert

        # The analytics pool, not the shared one: these writes happen in the
        # background and must never hold a connection a user request needs.
        from analytics.db import session_factory

        try:
            async with session_factory()() as db:
                for name, values in grouped.items():
                    if not values:
                        continue
                    if name == "client_session":
                        # One row per session for its whole life, so this is an
                        # update as often as an insert: the first page view
                        # creates it and every later one refines it.
                        await self._upsert_sessions(db, tables[name], values)
                        continue
                    await db.execute(insert(tables[name]), values)
                    if name == "client_error":
                        # Groups are derived, not reported. Deriving them here
                        # rather than in the browser means a client cannot
                        # invent a fingerprint or inflate a count.
                        await self._roll_up_error_groups(db, values)
                await db.commit()
            return True
        except Exception as exc:
            self.stats.last_error = f"{exc.__class__.__name__}: {exc}"
            if not quiet:
                logger.warning("analytics.insert_failed: %s", exc)
            return False


RECORDER = ActivityRecorder()
