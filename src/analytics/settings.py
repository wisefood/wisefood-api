"""Runtime analytics settings and per-user consent.

Two independent gates decide whether an event is recorded, and with whose name
on it:

* **Settings** — what the platform is collecting right now. Editable by an admin
  from the console with no redeploy, but only ever *narrowing* what the
  deployment's ``ANALYTICS_ENABLED`` already permits.
* **Consent** — whether a particular user's identity may be attached. An event
  from a user who has not consented is still recorded; it simply lands with its
  identity columns NULL, because a count of how many people searched for
  something is not personal data while a list of who they were is.

Both are cached in-process with a short TTL. Not Redis, despite the design note:
this sits on the write path of every request, an extra network hop to decide
whether to record is worse than a few seconds of staleness on a settings change,
and a Redis cache would have its own TTL anyway. The cost is that a settings
change takes up to ``_SETTINGS_TTL`` to reach every replica.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Dict, Iterable, Optional, Set

logger = logging.getLogger(__name__)

#: Consent kinds in wisefood.user_consent. The ledger is append-only and a row
#: means "this happened", so a refusal is its own kind rather than a flag: the
#: later of the two rows wins.
CONSENT_GRANT = "analytics"
CONSENT_OPT_OUT = "analytics_opt_out"

#: Product surfaces an event can be attributed to.
APPS = ("foodchat", "foodscholar", "recipewrangler", "catalog", "console", "platform")

_SETTINGS_TTL = 30.0
#: Consent decisions are cached per process. Invalidation on a change reaches
#: only the replica that served the request, so this TTL is the bound on how
#: long another replica keeps attributing a user who has just withdrawn. Kept
#: short for that reason; the lookup is one indexed query per batch.
_CONSENT_TTL = 30.0
_CONSENT_CACHE_MAX = 4096

#: Every runtime-editable key, with the value used when the row is absent.
#: Anything not listed here is rejected by the settings endpoint, so a typo
#: cannot silently become a setting nothing reads.
DEFAULTS: Dict[str, Any] = {
    # Master pause. Distinct from ANALYTICS_ENABLED: that is a deployment
    # decision, this is an operator stopping collection right now.
    "paused": False,
    # Fraction of *high-volume* events kept (http.request). Domain events and
    # feedback are never sampled — losing one of those loses a fact, not a
    # data point.
    "sample_rate": 1.0,
    "apps": {app: True for app in APPS},
    "capture.http_requests": True,
    "capture.client_events": True,
    "capture.search_queries": True,
    "capture.llm_usage": True,
    # Whether the text a user typed is kept alongside its normalised form.
    # Turning this off still leaves trending working, on the hash.
    "capture.raw_query_text": True,
    # --- Real user monitoring -----------------------------------------
    # The device a session ran on. Cheap: one row per session, not per
    # action, and the raw user agent is stripped for anyone who has not
    # consented, so what remains is a browser and an OS name.
    "capture.client_sessions": True,
    # What broke in the browser. On by default and never sampled — an error
    # nobody recorded is one nobody can fix, and a sampled crash report makes
    # a reproducible fault look intermittent.
    "capture.errors": True,
    # Where people clicked. OFF by default: this is the highest-volume thing
    # the platform can record, and it is the one a participant is most likely
    # to consider surveillance. Turn it on deliberately, for a period, to
    # answer a question.
    "capture.interactions": False,
    # How fast pages felt. Off by default for volume, not for sensitivity —
    # a page load time is about the page.
    "capture.vitals": False,
    # Fraction of ordinary clicks kept. Rage and dead clicks ignore this:
    # they are the actionable minority and sampling them away defeats the
    # point of collecting any of it.
    "sample_rate.interactions": 0.25,
    # --- Tracing ------------------------------------------------------
    # The platform-wide kill switch for LLM tracing, honoured by every
    # service. Previously tracing could only be stopped by unsetting the
    # Langfuse keys and redeploying, which is not something anyone can do
    # while an incident is in progress, or when a participant withdraws.
    #
    # `tracing.enabled` is the master; `tracing.langfuse` turns off the
    # Langfuse sink alone. Both are read by foodchat, foodscholar,
    # RecipeWrangler and the gateway's own Langfuse proxy.
    "tracing.enabled": True,
    "tracing.langfuse": True,
    # --- Pricing ------------------------------------------------------
    # Model name -> [USD per 1M input tokens, USD per 1M output tokens].
    # Providers change their rates without warning and the built-in table in
    # analytics.pricing is a snapshot, so an operator can correct a stale rate
    # or price a newly added model from the console instead of waiting for a
    # release. Empty by default: the built-in table applies until overridden.
    "pricing.overrides": {},
    # --- Platform ----------------------------------------------------------
    # Only admins may use the platform while this is on; everyone else gets a
    # maintenance page and a 503 from the API. Lives in this table rather than
    # in a second one because this table already has what a platform switch
    # needs — an admin-only write, validation, an audit trail of who flipped it
    # and when, and thirty-second propagation to every replica — and building
    # those again for one boolean would be the wrong kind of tidy.
    "platform.maintenance_mode": False,
}

#: Settings whose value is an open-ended map rather than a fixed set of
#: booleans. `validate` treats a dict default as a closed allowlist of flags,
#: which is right for `apps` and wrong for these.
_OPEN_MAP_KEYS = ("pricing.overrides",)


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def consent_mode() -> str:
    """``opt_in`` (default) or ``opt_out``.

    Left configurable because the project has not decided yet, and the two
    differ only in what happens for a user who has expressed nothing. The safe
    reading is the default.
    """
    mode = (os.getenv("ANALYTICS_CONSENT_MODE", "opt_in") or "").strip().lower()
    return mode if mode in ("opt_in", "opt_out") else "opt_in"


class SettingsCache:
    """The `analytics.settings` table, read rarely and never on failure."""

    def __init__(self, ttl: float = _SETTINGS_TTL):
        self._ttl = ttl
        self._values: Dict[str, Any] = dict(DEFAULTS)
        self._loaded_at = 0.0
        self._refresh_failed_once = False

    def invalidate(self) -> None:
        self._loaded_at = 0.0

    def current(self) -> Dict[str, Any]:
        """The last known settings. Never blocks and never raises."""
        return self._values

    def get(self, key: str, default: Any = None) -> Any:
        """One setting, falling back to its declared default.

        Used on the write path, so it reads the cache and never touches the
        database: a settings lookup must not add a query to every recorded row.
        """
        if key in self._values:
            return self._values[key]
        return DEFAULTS.get(key, default)

    async def refresh_if_stale(self) -> Dict[str, Any]:
        if (time.monotonic() - self._loaded_at) < self._ttl:
            return self._values
        try:
            from sqlalchemy import select

            from backend.postgres import POSTGRES_ASYNC_SESSION_FACTORY
            from sql import AnalyticsSetting

            async with POSTGRES_ASYNC_SESSION_FACTORY()() as db:
                rows = (await db.execute(select(AnalyticsSetting))).scalars().all()
            values = dict(DEFAULTS)
            for row in rows:
                if row.key in DEFAULTS:
                    values[row.key] = row.value
            self._values = values
        except Exception as exc:
            # Keep serving the last good values. A settings table that is
            # unreachable must not stop collection, and must not start it
            # either — the previous values are the safest answer. Logged at
            # WARNING once and DEBUG after: a deployment that has not applied
            # the schema would otherwise emit this every 30 s forever.
            level = logging.DEBUG if self._refresh_failed_once else logging.WARNING
            self._refresh_failed_once = True
            logger.log(level, "analytics.settings_refresh_failed: %s", exc)
        finally:
            self._loaded_at = time.monotonic()
        return self._values

    @staticmethod
    def validate(key: str, value: Any) -> Any:
        """Check an admin-supplied value against the shape of its default.

        Strict on purpose. `isinstance(True, int)` holds in Python, so a bare
        type check accepted `sample_rate=true`; and `apps={"foodchat": "false"}`
        passed as a dict while `bool("false")` is True — collection stayed on
        while the admin believed it was off. Returns the normalised value, or
        raises ValueError with a message fit for the API.
        """
        if key not in DEFAULTS:
            raise ValueError(f"Unknown analytics setting '{key}'")
        default = DEFAULTS[key]
        if isinstance(default, bool):
            if not isinstance(value, bool):
                raise ValueError(f"'{key}' must be true or false")
            return value
        if isinstance(default, float):
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"'{key}' must be a number")
            if not 0.0 <= float(value) <= 1.0:
                raise ValueError(f"'{key}' must be between 0 and 1")
            return float(value)
        if key in _OPEN_MAP_KEYS:
            return SettingsCache._validate_price_map(key, value)
        if isinstance(default, dict):
            if not isinstance(value, dict):
                raise ValueError(f"'{key}' must be an object")
            unknown = sorted(set(value) - set(default))
            if unknown:
                raise ValueError(f"'{key}' has unknown keys: {unknown}")
            bad = sorted(k for k, v in value.items() if not isinstance(v, bool))
            if bad:
                raise ValueError(f"'{key}' values must be true or false: {bad}")
            # Missing apps keep their default, so a partial object cannot
            # silently switch off what it did not mention.
            return {**default, **value}
        return value

    @staticmethod
    def _validate_price_map(key: str, value: Any) -> Dict[str, list]:
        """Check a model -> [input_rate, output_rate] map.

        Rejected rather than coerced: a rate that arrives as the string "0.15"
        and is silently accepted would price every call at zero once it failed
        to multiply, and a spend report that reads $0.00 looks exactly like one
        for a platform nobody used.
        """
        if not isinstance(value, dict):
            raise ValueError(f"'{key}' must be an object of model -> [input, output]")
        if len(value) > 200:
            raise ValueError(f"'{key}' may hold at most 200 models")
        cleaned: Dict[str, list] = {}
        for model, rate in value.items():
            name = str(model).strip()
            if not name or len(name) > 128:
                raise ValueError(f"'{key}' has an unusable model name")
            if not isinstance(rate, (list, tuple)) or len(rate) != 2:
                raise ValueError(
                    f"'{key}' entry '{name}' must be [input_rate, output_rate]"
                )
            numbers = []
            for part in rate:
                if isinstance(part, bool) or not isinstance(part, (int, float)):
                    raise ValueError(f"'{key}' rates for '{name}' must be numbers")
                if part < 0 or part > 10_000:
                    raise ValueError(
                        f"'{key}' rates for '{name}' must be dollars per million tokens"
                    )
                numbers.append(float(part))
            cleaned[name] = numbers
        return cleaned

    async def put(self, key: str, value: Any, updated_by: Optional[str]) -> None:
        value = self.validate(key, value)
        from sqlalchemy import func
        from sqlalchemy.dialects.postgresql import insert

        from backend.postgres import POSTGRES_ASYNC_SESSION_FACTORY
        from sql import AnalyticsSetting

        async with POSTGRES_ASYNC_SESSION_FACTORY()() as db:
            stmt = insert(AnalyticsSetting.__table__).values(
                key=key, value=value, updated_by=updated_by
            )
            await db.execute(
                stmt.on_conflict_do_update(
                    index_elements=[AnalyticsSetting.__table__.c.key],
                    # `updated_at` has a default, but a default only fires on
                    # INSERT; without setting it here every later change kept
                    # the timestamp of the first one.
                    set_={
                        "value": value,
                        "updated_by": updated_by,
                        "updated_at": func.now(),
                    },
                )
            )
            await db.commit()
        self.invalidate()

    # -- the questions the recorder actually asks ---------------------------
    def collecting(self, app: str) -> bool:
        values = self._values
        if values.get("paused"):
            return False
        apps = values.get("apps") or {}
        return bool(apps.get(app, True))

    def captures(self, capability: str) -> bool:
        return bool(self._values.get(f"capture.{capability}", True))

    def tracing_enabled(self, sink: Optional[str] = None) -> bool:
        """Whether tracing may run at all, or for one named sink."""
        if not self._values.get("tracing.enabled", True):
            return False
        if sink is None:
            return True
        return bool(self._values.get(f"tracing.{sink}", True))

    def sample_rate(self, capability: Optional[str] = None) -> float:
        """The fraction of a sampled stream to keep.

        A capability may declare its own rate. Clicks do, because they arrive
        at a rate nothing else on the platform approaches, and holding them to
        the same fraction as page views would either drown the database or
        throw away most of the page views.
        """
        keys = [f"sample_rate.{capability}"] if capability else []
        keys.append("sample_rate")
        for key in keys:
            if key not in self._values:
                continue
            try:
                rate = float(self._values[key])
            except (TypeError, ValueError):
                continue
            return min(max(rate, 0.0), 1.0)
        return 1.0


class ConsentCache:
    """Whether a user's identity may be attached to their events."""

    def __init__(self, ttl: float = _CONSENT_TTL):
        self._ttl = ttl
        self._entries: Dict[str, tuple] = {}  # user_id -> (monotonic, allowed)

    def invalidate(self, user_id: Optional[str] = None) -> None:
        if user_id is None:
            self._entries.clear()
        else:
            self._entries.pop(user_id, None)

    def _cached(self, user_id: str) -> Optional[bool]:
        hit = self._entries.get(user_id)
        if hit is None or (time.monotonic() - hit[0]) >= self._ttl:
            return None
        return hit[1]

    def _remember(self, user_id: str, allowed: bool) -> None:
        if len(self._entries) >= _CONSENT_CACHE_MAX:
            self._entries.clear()
        self._entries[user_id] = (time.monotonic(), allowed)

    async def allowed_for(self, user_ids: Iterable[str]) -> Set[str]:
        """The subset of ``user_ids`` whose identity may be recorded.

        Resolved for a whole batch in one query rather than per event, then
        cached. On any failure the answer is "nobody", because writing an
        identity we were not sure about is the error that cannot be undone.
        """
        wanted = {uid for uid in user_ids if uid}
        if not wanted:
            return set()

        allowed: Set[str] = set()
        unknown = set()
        for uid in wanted:
            cached = self._cached(uid)
            if cached is None:
                unknown.add(uid)
            elif cached:
                allowed.add(uid)
        if not unknown:
            return allowed

        opt_in = consent_mode() == "opt_in"
        try:
            latest = await self._latest_consent_rows(unknown)
        except Exception as exc:
            logger.warning("analytics.consent_lookup_failed: %s", exc)
            return allowed  # unknown users stay unattributed, uncached

        for uid in unknown:
            granted_at = latest.get((uid, CONSENT_GRANT))
            declined_at = latest.get((uid, CONSENT_OPT_OUT))
            if declined_at and (not granted_at or declined_at >= granted_at):
                decision = False
            elif granted_at:
                decision = True
            else:
                decision = not opt_in  # never said anything either way
            self._remember(uid, decision)
            if decision:
                allowed.add(uid)
        return allowed

    @staticmethod
    async def _latest_consent_rows(user_ids: Set[str]) -> Dict[tuple, Any]:
        from sqlalchemy import select

        from backend.postgres import POSTGRES_ASYNC_SESSION_FACTORY
        from sql import UserConsent

        async with POSTGRES_ASYNC_SESSION_FACTORY()() as db:
            result = await db.execute(
                select(
                    UserConsent.user_id,
                    UserConsent.consent_type,
                    UserConsent.granted_at,
                )
                .where(
                    UserConsent.user_id.in_(list(user_ids)),
                    UserConsent.consent_type.in_([CONSENT_GRANT, CONSENT_OPT_OUT]),
                )
                .order_by(UserConsent.granted_at.desc(), UserConsent.id.desc())
            )
            latest: Dict[tuple, Any] = {}
            for user_id, consent_type, granted_at in result.all():
                latest.setdefault((user_id, consent_type), granted_at)
            return latest


SETTINGS = SettingsCache()
CONSENT = ConsentCache()
