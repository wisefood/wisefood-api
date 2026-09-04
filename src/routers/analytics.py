"""Activity analytics: ingest, health, and runtime settings.

Three kinds of caller reach this router, and they are trusted differently:

* **Browsers and the SDK** post to ``/analytics/events`` and
  ``/analytics/feedback`` with a normal user token. Identity comes from the
  token, event types come from an allowlist, and batches are capped — a client
  is trusted to say *what happened to it*, never *who it is*.
* **Platform services** post to ``/analytics/internal/events`` with an
  HMAC-signed body, because they legitimately need to report activity on behalf
  of a user whose token they never saw.
* **Admins and experts** read ``/analytics/health`` and edit
  ``/analytics/settings``.

Ingest never fails because collection is off. A client that has to know whether
analytics is enabled would need a way to ask, and would then have a reason to
retry — so a disabled platform accepts the batch and discards it.
"""

import hashlib
import hmac
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse

import context
import kutils
from analytics import APPS, DEFAULTS, RECORDER, SETTINGS
from analytics.settings import CONSENT
from auth import auth
from budget import guest_budget
from exceptions import (
    APIException,
    AuthenticationError,
    DataError,
    RateLimitError,
)
from routers.generic import render
from schemas import (
    ClientErrorBatch,
    ErrorStatusUpdate,
    ClientSessionIn,
    InteractionBatch,
    WebVitalBatch,
    ActivityEventBatch,
    ExpertReviewCreate,
    FeedbackStatusUpdate,
    ActivityIngestResponse,
    AnalyticsSettingUpdate,
    PlatformFeedbackRequest,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/analytics", tags=["analytics"])

#: Shared secret for the service-to-service ingest. Same construction as the
#: FoodChat member assertion: unset means the endpoint is closed, not open.
_SIGNATURE_HEADER = "X-WiseFood-Analytics-Signature"
#: A signature older than this is refused, so a captured request cannot be
#: replayed indefinitely.
_SIGNATURE_MAX_AGE_SECONDS = 300


def _ingest_secret() -> str:
    return (os.getenv("ANALYTICS_INGEST_SECRET") or "").strip()


def _verify_service_signature(raw_body: bytes, header: str) -> None:
    """Verify ``<issued_at>.<hmac-sha256(issued_at|body)>``.

    The timestamp is inside the signed payload, so it cannot be edited to
    extend a captured signature's life.
    """
    secret = _ingest_secret()
    if not secret:
        raise APIException(
            status_code=503,
            detail="Service analytics ingest is not configured",
            code="analytics/ingest_disabled",
            extra={"title": "IngestDisabled"},
        )
    try:
        issued_at_raw, digest = header.split(".", 1)
        issued_at = int(issued_at_raw)
    except (ValueError, AttributeError) as exc:
        raise AuthenticationError(detail="Malformed analytics signature") from exc

    if abs(int(time.time()) - issued_at) > _SIGNATURE_MAX_AGE_SECONDS:
        raise AuthenticationError(detail="Analytics signature expired")

    expected = hmac.new(
        secret.encode(), f"{issued_at}|".encode() + raw_body, hashlib.sha256
    ).hexdigest()
    try:
        valid = hmac.compare_digest(expected, digest)
    except TypeError as exc:
        # A non-ASCII digest is a malformed signature, not a server error.
        raise AuthenticationError(detail="Malformed analytics signature") from exc
    if not valid:
        raise AuthenticationError(detail="Invalid analytics signature")


# --------------------------------------------------------------- ingest ----
def _enforce_ingest_budget(rows: int) -> None:
    """Refuse a caller reporting more than any honest client would.

    Applied to every ingest endpoint and to every caller, which the platform's
    existing guest budget is not: that one exempts anyone signed in, and a
    signed-in account is not hard to obtain. The cost being bounded here is
    durable — rows on the volume the whole platform shares — so an unbounded
    authenticated caller is a disk-exhaustion outage for every service, not
    merely noisy analytics.

    Charged in rows, not requests: one request may carry two hundred
    interactions or a single page view, and only one of those is expensive.
    """
    from analytics.ingest_limit import LIMITER

    allowed, retry_after = LIMITER.check(context.get_user_sub(), rows)
    if allowed:
        return
    RECORDER.stats.dropped_rate_limited += rows
    raise RateLimitError(
        detail=(
            "Too much activity reported at once. This limit is far above what "
            f"a browser sends; try again in {retry_after}s."
        )
    )



@router.post(
    "/events",
    dependencies=[Depends(auth()), Depends(guest_budget("analytics"))],
)
@render()
async def ingest_events(request: Request, body: ActivityEventBatch):
    """Record a batch of client-observed events.

    Guests included: their activity is as interesting as anyone's, and their
    identity is ephemeral by construction. What a client may *not* do is name
    the user — that comes from the token, via the request context.
    """
    _enforce_ingest_budget(len(body.events))
    if not RECORDER.enabled:
        return ActivityIngestResponse(accepted=0, discarded=True).model_dump()

    now = datetime.now(timezone.utc)
    for event in body.events:
        occurred_at = event.occurred_at or now
        if occurred_at.tzinfo is None:
            occurred_at = occurred_at.replace(tzinfo=timezone.utc)
        # A client clock that is ahead would sort future events to the top of
        # every report; one that is far behind is a stale buffer, which is
        # legitimate. Clamp forwards only.
        if occurred_at > now:
            occurred_at = now
        # No `route`: the ingest URL is not where the event happened, and the
        # client names its surface explicitly through `app`.
        RECORDER.record_event(
            event.type,
            app=event.app,
            props=event.props,
            occurred_at=occurred_at,
            capability="client_events",
        )
    return ActivityIngestResponse(accepted=len(body.events)).model_dump()


@router.post(
    "/feedback",
    dependencies=[Depends(auth()), Depends(guest_budget("analytics"))],
)
@render()
async def submit_platform_feedback(request: Request, body: PlatformFeedbackRequest):
    """Record a feedback signal from any surface.

    This is what the floating satisfaction widget has needed since it shipped:
    it has been logging ratings to the browser console and discarding them.
    """
    _enforce_ingest_budget(1)
    RECORDER.record_feedback(
        app=body.app,
        target_type=body.target_type,
        target_id=body.target_id,
        rating_kind=body.rating_kind,
        rating_value=body.rating_value,
        rating_value_num=body.rating_value_num,
        reason=body.reason,
        comment=body.comment,
        source="ui",
    )
    return {"recorded": True, "collecting": RECORDER.enabled}


@router.post(
    "/session",
    dependencies=[Depends(auth()), Depends(guest_budget("analytics"))],
)
@render()
async def ingest_client_session(request: Request, body: ClientSessionIn):
    """Record what one browser session is running on.

    The user agent and the address are read from the request rather than from
    the body, on purpose. A client that could state its own browser could also
    misstate it, and every device report groups by the parsed result — so a
    page that lied would not merely mislabel itself, it would invent a bucket.
    The address never leaves this function intact: the recorder stores a
    network prefix and nothing finer.
    """
    _enforce_ingest_budget(1)
    if not RECORDER.enabled:
        return {"accepted": False, "collecting": False}

    from analytics.device import client_ip, country_from_headers

    RECORDER.record_client_session(
        session_id=body.session_id,
        user_agent=request.headers.get("user-agent"),
        ip=client_ip(request.scope, request.headers),
        country=country_from_headers(request.headers),
        app=body.app,
        release=body.release,
        screen_w=body.screen_w,
        screen_h=body.screen_h,
        viewport_w=body.viewport_w,
        viewport_h=body.viewport_h,
        device_pixel_ratio=body.device_pixel_ratio,
        color_scheme=body.color_scheme,
        reduced_motion=body.reduced_motion,
        timezone_name=body.timezone,
        connection=body.connection,
    )
    return {"accepted": True, "collecting": True}


@router.post(
    "/errors",
    dependencies=[Depends(auth()), Depends(guest_budget("analytics"))],
)
@render()
async def ingest_client_errors(request: Request, body: ClientErrorBatch):
    """Record things that broke in a browser.

    Accepted from guests as well. An error that only happens to people who are
    not signed in is a whole class of fault — the sign-up page, the first
    load — and refusing those reports would hide exactly that class.
    """
    _enforce_ingest_budget(len(body.events))
    if not RECORDER.enabled:
        return ActivityIngestResponse(accepted=0, discarded=True).model_dump()

    user_agent = request.headers.get("user-agent")
    for event in body.events:
        RECORDER.record_client_error(
            app=event.app,
            user_agent=user_agent,
            kind=event.kind,
            name=event.name,
            message=event.message,
            stack=event.stack,
            url_path=event.url_path,
            line_no=event.line_no,
            col_no=event.col_no,
            handled=event.handled,
            breadcrumbs=event.breadcrumbs,
            context_data=event.context,
            release=event.release,
            occurred_at=event.occurred_at,
        )
    return ActivityIngestResponse(accepted=len(body.events)).model_dump()


@router.post(
    "/interactions",
    dependencies=[Depends(auth()), Depends(guest_budget("analytics"))],
)
@render()
async def ingest_interactions(request: Request, body: InteractionBatch):
    """Record clicks, rage clicks, dead clicks and scroll depth."""
    _enforce_ingest_budget(len(body.events))
    if not RECORDER.enabled:
        return ActivityIngestResponse(accepted=0, discarded=True).model_dump()

    for event in body.events:
        RECORDER.record_interaction(
            app=event.app,
            path=event.path,
            kind=event.kind,
            element_key=event.element_key,
            element_role=event.element_role,
            x_pct=event.x_pct,
            y_pct=event.y_pct,
            viewport_w=event.viewport_w,
            viewport_h=event.viewport_h,
            depth_pct=event.depth_pct,
            repeats=event.repeats,
            occurred_at=event.occurred_at,
        )
    return ActivityIngestResponse(accepted=len(body.events)).model_dump()


@router.post(
    "/vitals",
    dependencies=[Depends(auth()), Depends(guest_budget("analytics"))],
)
@render()
async def ingest_vitals(request: Request, body: WebVitalBatch):
    """Record how fast pages felt, as the browser measured it."""
    _enforce_ingest_budget(len(body.events))
    if not RECORDER.enabled:
        return ActivityIngestResponse(accepted=0, discarded=True).model_dump()

    for event in body.events:
        RECORDER.record_web_vital(
            app=event.app,
            path=event.path,
            metric=event.metric,
            value=event.value,
            rating=event.rating,
            navigation_type=event.navigation_type,
            occurred_at=event.occurred_at,
        )
    return ActivityIngestResponse(accepted=len(body.events)).model_dump()


@router.post("/internal/events")
@render()
async def ingest_service_events(request: Request):
    """Service-to-service ingest, HMAC-signed.

    Unlike the public endpoint this accepts identity in the body, because the
    caller is a platform service reporting on behalf of a user whose token it
    never held — FoodChat knows a plan was generated, and only the gateway can
    say for whom.
    """
    raw = await request.body()
    _verify_service_signature(raw, request.headers.get(_SIGNATURE_HEADER, ""))

    import json

    try:
        payload = json.loads(raw or b"{}")
    except ValueError as exc:
        raise DataError(detail="Body is not valid JSON") from exc
    events = payload.get("events")
    if not isinstance(events, list) or not events:
        raise DataError(detail="Body must carry a non-empty 'events' list")
    if len(events) > 200:
        raise DataError(detail="At most 200 events per batch")

    if not RECORDER.enabled:
        return ActivityIngestResponse(accepted=0, discarded=True).model_dump()

    accepted = 0
    for event in events:
        if not isinstance(event, dict) or not event.get("type"):
            continue
        kind = str(event.get("kind") or "event")
        app = str(event.get("app") or "platform")
        if app not in APPS:
            app = "platform"
        # The signature is what makes this trustworthy: an unsigned caller
        # cannot reach here, so the identity in the body may be believed.
        identity = {
            "user_id": event.get("user_id"),
            "member_id": event.get("member_id"),
            "request_id": event.get("request_id") or context.get_request_id(),
            "client_session_id": event.get("client_session_id"),
            "client": event.get("client") or "internal",
            "is_guest": event.get("is_guest"),
        }
        if kind == "search":
            RECORDER.record_search(
                surface=str(event.get("surface") or "unknown"),
                app=app,
                raw_query=event.get("raw_query"),
                filters=event.get("filters") or {},
                result_count_first_pass=event.get("result_count_first_pass"),
                result_count_final=event.get("result_count_final"),
                relaxed=bool(event.get("relaxed")),
                lexical_fallback=bool(event.get("lexical_fallback")),
                latency_ms=event.get("latency_ms"),
                identity=identity,
            )
        elif kind == "feedback":
            # The owning service keeps its own copy where it drives behaviour;
            # this mirror is what lets an expert see every surface's feedback
            # in one list instead of four unjoinable tables.
            RECORDER.record_feedback(
                app=app,
                target_type=str(event.get("target_type") or "platform"),
                target_id=event.get("target_id"),
                rating_kind=str(event.get("rating_kind") or "thumbs"),
                rating_value=event.get("rating_value"),
                rating_value_num=event.get("rating_value_num"),
                reason=event.get("reason"),
                comment=event.get("comment"),
                source="service",
                identity=identity,
            )
        elif kind == "llm_usage":
            RECORDER.record_llm_usage(
                app=app,
                feature=event.get("feature"),
                provider=event.get("provider"),
                model=event.get("model"),
                input_tokens=event.get("input_tokens"),
                output_tokens=event.get("output_tokens"),
                total_tokens=event.get("total_tokens"),
                cost_usd=event.get("cost_usd"),
                latency_ms=event.get("latency_ms"),
                trace_id=event.get("trace_id"),
                identity=identity,
            )
        else:
            RECORDER.record_event(
                str(event["type"]),
                app=app,
                props=event.get("props") or {},
                route=event.get("route"),
                capability="client_events",
                identity=identity,
            )
        accepted += 1
    return ActivityIngestResponse(accepted=accepted).model_dump()


@router.get("/runtime-flags")
@render()
async def runtime_flags(request: Request):
    """The switches every platform service needs to obey, right now.

    Services poll this so an operator can stop tracing across the platform from
    the console, without a redeploy. Before this, turning tracing off meant
    unsetting the Langfuse keys and rolling every pod — not something anyone
    can do while an incident is in progress, or when a study participant
    withdraws mid-session.

    Signed like the internal ingest: the caller is a platform service, and the
    answer tells it whether to keep recording. Deliberately cheap and cacheable
    — services refresh it on a timer, not per request.
    """
    _verify_service_signature(
        await request.body(), request.headers.get(_SIGNATURE_HEADER, "")
    )
    values = await SETTINGS.refresh_if_stale()
    return {
        # The deployment-level switch always wins; the table can only narrow.
        "analytics_enabled": RECORDER.enabled and not values.get("paused", False),
        "tracing_enabled": bool(
            values.get("tracing.enabled", True)
        ),
        "tracing_langfuse": bool(
            values.get("tracing.enabled", True)
            and values.get("tracing.langfuse", True)
        ),
        "apps": values.get("apps", {}),
        "capture": {
            key.split(".", 1)[1]: value
            for key, value in values.items()
            if key.startswith("capture.")
        },
        "sample_rate": values.get("sample_rate", 1.0),
    }


@router.get("/client-flags", dependencies=[Depends(auth())])
@render()
async def client_flags(request: Request):
    """What a browser should bother collecting, right now.

    Separate from `/runtime-flags`, which is signature-verified and meant for
    platform services. A browser cannot sign anything, and it needs a
    different, much smaller answer: only which of its own capture streams to
    run and how much to sample them.

    This exists because the cheap streams and the expensive ones are not alike.
    A page view costs nothing to gather and can be posted and discarded
    server-side. Click capture means a listener on every click, a mutation
    observer and a buffer — work the browser must not do at all when nobody is
    collecting. Telling it so is the only way to switch that off.

    Nothing here is privileged: it is a set of booleans about the platform's
    own configuration, and it is already implied by whether anything is being
    recorded. A user token is required simply because every other analytics
    route requires one.
    """
    values = await SETTINGS.refresh_if_stale()
    collecting = RECORDER.enabled and not values.get("paused", False)

    def capturing(capability: str) -> bool:
        return collecting and bool(values.get(f"capture.{capability}", False))

    return {
        "collecting": collecting,
        "capture": {
            "events": capturing("client_events"),
            "session": capturing("client_sessions"),
            "errors": capturing("errors"),
            "interactions": capturing("interactions"),
            "vitals": capturing("vitals"),
        },
        "sample_rate": {
            "interactions": float(values.get("sample_rate.interactions", 0.25)),
            "vitals": float(values.get("sample_rate.vitals", values.get("sample_rate", 1.0))),
        },
        # How long a client may cache this. Matches the settings TTL, so a
        # switch thrown in the console reaches every open tab in about the
        # same time it reaches every replica.
        "ttl_seconds": 30,
    }


# --------------------------------------------------------------- reports ----
#
# Everything here is admin+expert. Reviewing what the product is doing is the
# expert's job; a normal user has no route to any of it. Reads that name
# individual people — the user table, a session, the Q&A review surface — are
# recorded as expert activity, because "who looked at the userbase" is exactly
# the kind of privileged action the platform never used to keep.


@router.get("/overview", dependencies=[Depends(auth("admin,expert"))])
@render()
async def analytics_overview(request: Request, days: int = 7, since: Optional[str] = None, until: Optional[str] = None):
    """The console front page: activity, searches, feedback and cost."""
    from analytics.reports import overview

    return await overview(days=days, since=since, until=until)


@router.get("/attention", dependencies=[Depends(auth("admin,expert"))])
@render()
async def attention_items(request: Request, days: int = 7, since: Optional[str] = None, until: Optional[str] = None):
    """What is worth doing something about, and the page that does it."""
    from analytics.reports import attention

    return await attention(days=days, since=since, until=until)


@router.get("/queries/trending", dependencies=[Depends(auth("admin,expert"))])
@render()
async def trending(request: Request, days: int = 7, limit: int = 20, since: Optional[str] = None, until: Optional[str] = None):
    """Top and rising searches. Rising compares against the previous window."""
    from analytics.reports import trending_queries

    return await trending_queries(days=days, limit=limit, since=since, until=until)


@router.get("/queries/zero-result", dependencies=[Depends(auth("admin,expert"))])
@render()
async def zero_result(request: Request, days: int = 7, limit: int = 20, since: Optional[str] = None, until: Optional[str] = None):
    """Searches that found nothing — what the catalogue is missing."""
    from analytics.reports import zero_result_queries

    return await zero_result_queries(days=days, limit=limit, since=since, until=until)


@router.get("/performance", dependencies=[Depends(auth("admin,expert"))])
@render()
async def performance(request: Request, days: int = 7, limit: int = 25, since: Optional[str] = None, until: Optional[str] = None):
    """Latency percentiles and error rate per route."""
    from analytics.reports import route_performance

    return await route_performance(days=days, limit=limit, since=since, until=until)


@router.get("/search-quality", dependencies=[Depends(auth("admin,expert"))])
@render()
async def search_quality_report(request: Request, days: int = 7, since: Optional[str] = None, until: Optional[str] = None):
    """How well search is working, including searches that only worked after
    the constraints were loosened."""
    from analytics.reports import search_quality

    return await search_quality(days=days, since=since, until=until)


@router.get("/funnel", dependencies=[Depends(auth("admin,expert"))])
@render()
async def funnel(request: Request, days: int = 7, since: Optional[str] = None, until: Optional[str] = None):
    """Search, click, open — and where people fall out."""
    from analytics.reports import search_funnel

    return await search_funnel(days=days, since=since, until=until)


@router.get("/feedback/targets", dependencies=[Depends(auth("admin,expert"))])
@render()
async def feedback_targets(request: Request, days: int = 30, limit: int = 25, since: Optional[str] = None, until: Optional[str] = None):
    """Which specific recipes, articles and answers draw complaints."""
    from analytics.reports import feedback_by_target

    return await feedback_by_target(days=days, limit=limit, since=since, until=until)


#: Reports a researcher can take away as a file, and the key holding the rows.
#: An allowlist rather than a lookup by name: this turns a URL parameter into a
#: function call, and a report not on this list is one nobody decided to expose
#: in bulk.
_EXPORTS = {
    "queries": ("trending_queries", "top"),
    "zero-result": ("zero_result_queries", "queries"),
    "users": ("user_activity", "users"),
    "performance": ("route_performance", "routes"),
    "feedback-targets": ("feedback_by_target", "targets"),
    "search-facets": ("search_filters", "facets"),
    "llm-usage": ("llm_usage_report", "by_user"),
    "reviewers": ("review_summary", "by_reviewer"),
    "pages": ("content_report", "pages"),
}


@router.get("/export.csv", dependencies=[Depends(auth("admin,expert"))])
async def export_csv(
    request: Request,
    report: str = "queries",
    days: int = 30,
    limit: int = 1000,
    since: Optional[str] = None,
    until: Optional[str] = None,
):
    """One report as a CSV file.

    A console that can only be read on screen is a dead end for the people this
    is built for: the evaluation work behind this platform happens in
    spreadsheets and notebooks, and retyping a table from a browser is not a
    workflow. Deliberately the same numbers as the page — this exports a
    report, it does not open a second query path with its own rounding.

    Streamed, so a large export does not build the whole file in memory first.
    """
    import csv
    import io

    import analytics.reports as reports_module

    choice = _EXPORTS.get(report)
    if choice is None:
        raise DataError(
            f"Unknown report '{report}'. One of: {', '.join(sorted(_EXPORTS))}"
        )
    function_name, rows_key = choice
    report_fn = getattr(reports_module, function_name)

    kwargs = {"days": days, "since": since, "until": until}
    if "limit" in report_fn.__code__.co_varnames:
        kwargs["limit"] = max(1, min(int(limit or 1000), 10_000))
    payload = await report_fn(**kwargs)
    rows = payload.get(rows_key) or []

    # Reading a report in bulk is exactly the privileged act the platform never
    # used to record, and an export leaves the building.
    RECORDER.record_event(
        "expert.report_exported",
        app="console",
        props={"report": report, "rows": len(rows), "days": payload.get("days")},
    )

    def generate():
        buffer = io.StringIO()
        writer = csv.writer(buffer)

        def flush():
            value = buffer.getvalue()
            buffer.seek(0)
            buffer.truncate(0)
            return value

        # A leading comment line, so a file that ends up in a shared drive
        # still says what it is and over what period.
        writer.writerow(
            [f"# wisefood {report}", f"since={payload.get('since')}", f"until={payload.get('until')}"]
        )
        if not rows:
            # A file holding only a header comment reads as a broken download.
            # Say plainly that the period was empty, so nobody mistakes it for
            # a report full of zeros.
            writer.writerow(["# no rows in this period"])
            yield flush()
            return
        columns = list(rows[0].keys())
        writer.writerow(columns)
        yield flush()
        for row in rows:
            writer.writerow([_csv_cell(row.get(column)) for column in columns])
            yield flush()

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    return StreamingResponse(
        generate(),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="wisefood-{report}-{stamp}.csv"'
        },
    )


def _csv_cell(value: Any) -> Any:
    """One value as a spreadsheet sees it.

    Two hazards. A nested list or dict rendered by `str()` becomes Python
    syntax in a cell, so it is joined instead. And a value beginning with `=`,
    `+`, `-` or `@` is a *formula* to Excel and Sheets — the classic CSV
    injection — so it is prefixed with a quote, which those tools strip on
    display but never execute.
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, tuple)):
        return "; ".join(str(part) for part in value)
    if isinstance(value, dict):
        return "; ".join(f"{k}={v}" for k, v in value.items())
    text = str(value)
    if text[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + text
    return text


@router.get("/patterns", dependencies=[Depends(auth("admin,expert"))])
@render()
async def patterns(
    request: Request, days: int = 30, since: Optional[str] = None, until: Optional[str] = None
):
    """When the platform is used, how deep a visit goes, and who comes back."""
    from analytics.reports import engagement_patterns

    return await engagement_patterns(days=days, since=since, until=until)


@router.get("/content", dependencies=[Depends(auth("admin,expert"))])
@render()
async def content(
    request: Request,
    days: int = 7,
    limit: int = 20,
    since: Optional[str] = None,
    until: Optional[str] = None,
):
    """What the answers, chats and pages themselves look like."""
    from analytics.reports import content_report

    return await content_report(days=days, limit=limit, since=since, until=until)


@router.get("/feedback/quality", dependencies=[Depends(auth("admin,expert"))])
@render()
async def feedback_quality_report(
    request: Request, days: int = 30, since: Optional[str] = None, until: Optional[str] = None
):
    """Feedback as a trend and a score, not an inbox."""
    from analytics.reports import feedback_quality

    return await feedback_quality(days=days, since=since, until=until)


@router.get("/search-filters", dependencies=[Depends(auth("admin,expert"))])
@render()
async def search_filter_report(
    request: Request,
    days: int = 30,
    limit: int = 20,
    since: Optional[str] = None,
    until: Optional[str] = None,
):
    """Which facets people apply, and which combinations find nothing."""
    from analytics.reports import search_filters

    return await search_filters(days=days, limit=limit, since=since, until=until)


@router.get("/audience", dependencies=[Depends(auth("admin,expert"))])
@render()
async def audience(
    request: Request, days: int = 30, since: Optional[str] = None, until: Optional[str] = None
):
    """Who is calling, in what language, from which client."""
    from analytics.reports import audience_breakdown

    return await audience_breakdown(days=days, since=since, until=until)


@router.get("/reviews/summary", dependencies=[Depends(auth("admin,expert"))])
@render()
async def reviews_summary(
    request: Request, days: int = 90, since: Optional[str] = None, until: Optional[str] = None
):
    """What the experts concluded, in aggregate, and where they disagreed."""
    from analytics.reports import review_summary

    return await review_summary(days=days, since=since, until=until)


@router.get("/users", dependencies=[Depends(auth("admin,expert"))])
@render()
async def user_report(request: Request, days: int = 30, limit: int = 50, since: Optional[str] = None, until: Optional[str] = None):
    """Per-user activity and cost, for users whose consent allows naming them."""
    from analytics.reports import user_activity

    result = await user_activity(days=days, limit=limit, since=since, until=until)
    RECORDER.record_event(
        "expert.users_reviewed",
        app="console",
        props={"days": days, "returned": len(result.get("users") or [])},
    )
    return result


@router.get("/llm-usage", dependencies=[Depends(auth("admin,expert"))])
@render()
async def llm_usage(request: Request, days: int = 30, since: Optional[str] = None, until: Optional[str] = None):
    """Tokens and cost by model, app, feature and user."""
    from analytics.reports import llm_usage_report

    return await llm_usage_report(days=days, since=since, until=until)


@router.get("/expert-activity", dependencies=[Depends(auth("admin,expert"))])
@render()
async def expert_activity_report(request: Request, days: int = 30, limit: int = 100, since: Optional[str] = None, until: Optional[str] = None):
    """Who used their privileges, and on what."""
    from analytics.reports import expert_activity

    return await expert_activity(days=days, limit=limit, since=since, until=until)


# -------------------------------------------------------- feedback review ----
@router.get("/feedback/inbox", dependencies=[Depends(auth("admin,expert"))])
@render()
async def feedback_inbox_page(
    request: Request,
    limit: int = 50,
    offset: int = 0,
    status: Optional[str] = None,
    app: Optional[str] = None,
    negative_only: bool = False,
):
    """Every surface's feedback in one triage list."""
    from analytics.reports import feedback_inbox

    result = await feedback_inbox(
        limit=limit, offset=offset, status=status, app=app, negative_only=negative_only
    )
    RECORDER.record_event(
        "expert.feedback_reviewed",
        app="console",
        props={"returned": len(result.get("items") or []), "status": status},
    )
    return result


@router.patch(
    "/feedback/{feedback_id}/status", dependencies=[Depends(auth("admin,expert"))]
)
@render()
async def update_feedback_status(
    request: Request, feedback_id: int, body: FeedbackStatusUpdate
):
    """Move one feedback item through new -> triaged -> resolved."""
    from analytics.reports import set_feedback_status

    try:
        changed = await set_feedback_status(feedback_id, body.status)
    except ValueError as exc:
        raise DataError(detail=str(exc)) from exc
    if not changed:
        raise DataError(detail=f"No feedback with id {feedback_id}")
    RECORDER.record_event(
        "expert.feedback_triaged",
        app="console",
        props={"feedback_id": feedback_id, "status": body.status},
    )
    return {"id": feedback_id, "status": body.status}


# ----------------------------------------------------------------- reviews ---
@router.post("/reviews", dependencies=[Depends(auth("admin,expert"))])
@render()
async def create_review(request: Request, body: ExpertReviewCreate):
    """Record an expert's verdict on an answer, a message or a piece of feedback.

    The reviewer is taken from the token, never the body: a review is an
    attribution, and one somebody could sign with another person's name would
    be worse than no review at all.
    """
    from analytics.reports import record_review

    user = kutils.current_user(request) or {}
    review = await record_review(
        reviewer_id=user.get("sub"),
        reviewer_name=user.get("preferred_username") or user.get("name"),
        target_type=body.target_type,
        target_id=body.target_id,
        verdict=body.verdict,
        notes=body.notes,
        tags=body.tags,
        request_id=body.request_id,
    )
    RECORDER.record_event(
        "expert.review_recorded",
        app="console",
        props={
            "target_type": body.target_type,
            "target_id": body.target_id,
            "verdict": body.verdict,
        },
    )
    return review


@router.get("/reviews", dependencies=[Depends(auth("admin,expert"))])
@render()
async def list_expert_reviews(
    request: Request,
    limit: int = 50,
    offset: int = 0,
    target_type: Optional[str] = None,
    target_id: Optional[str] = None,
    reviewer_id: Optional[str] = None,
):
    from analytics.reports import list_reviews

    return await list_reviews(
        limit=limit,
        offset=offset,
        target_type=target_type,
        target_id=target_id,
        reviewer_id=reviewer_id,
    )


# -------------------------------------------------------------- sessions ----
#
# Reading a session is admin/expert only. What a normal user gets is the id
# itself, shown in the page footer — enough to quote to support, and useless to
# anyone who cannot query it. Two people cannot read each other's sessions by
# guessing an id, because neither can read sessions at all.


@router.get("/sessions", dependencies=[Depends(auth("admin,expert"))])
@render()
async def list_recent_sessions(
    request: Request, limit: int = 50, user_id: Optional[str] = None, days: int = 30
):
    """The most recent browser sessions, newest first, within `days`."""
    from analytics.reports import recent_sessions

    return {"sessions": await recent_sessions(limit=limit, user_id=user_id, days=days)}


@router.get("/sessions/{session_id}", dependencies=[Depends(auth("admin,expert"))])
@render()
async def get_session_activity(
    request: Request,
    session_id: str,
    timeline_limit: int = 100,
    timeline_offset: int = 0
):
    """Everything one session did: counts, cost, and an ordered timeline.

    This is where a user quoting the id from their footer leads. The counts the
    requirement names — searches, questions, meal plans — are named fields, so a
    console does not have to know which event type means which.
    """
    from analytics.reports import session_summary

    cleaned = context.clean_id(session_id)
    if not cleaned:
        raise DataError(detail="Malformed session id")

    summary = await session_summary(
        cleaned, timeline_limit=timeline_limit, timeline_offset=timeline_offset
    )
    RECORDER.record_event(
        "expert.session_reviewed",
        app="console",
        props={"session_id": cleaned, "events": summary.get("events", 0)},
    )
    return summary


# ----------------------------------------------------------- operations ----
# ------------------------------------------------- real user monitoring ----


@router.get("/board", dependencies=[Depends(auth("admin,expert"))])
@render()
async def sessions_board(
    request: Request,
    days: int = 7,
    limit: int = 50,
    offset: int = 0,
    since: Optional[str] = None,
    until: Optional[str] = None,
    user_id: Optional[str] = None,
    device_type: Optional[str] = None,
    browser: Optional[str] = None,
    os: Optional[str] = None,
    country: Optional[str] = None,
    has_errors: Optional[bool] = None,
    search: Optional[str] = None,
    include_bots: bool = False,
):
    """Sessions with the device they ran on, filterable on every column."""
    from analytics.reports import session_board

    result = await session_board(
        days=days,
        limit=limit,
        offset=offset,
        since=since,
        until=until,
        user_id=user_id,
        device_type=device_type,
        browser=browser,
        os=os,
        country=country,
        has_errors=has_errors,
        search=search,
        include_bots=include_bots,
    )
    # Naming individual people is the privileged act, so it is recorded — the
    # same rule the user list follows.
    RECORDER.record_event(
        "expert.sessions_reviewed",
        app="console",
        props={"days": days, "returned": len(result.get("sessions") or [])},
    )
    return result


@router.get("/sessions/{session_id}/device", dependencies=[Depends(auth("admin,expert"))])
@render()
async def session_device_detail(request: Request, session_id: str):
    """The machine one session ran on, what broke, and where it frustrated."""
    from analytics.reports import session_device

    result = await session_device(session_id)
    if result is None:
        raise DataError(f"No device record for session '{session_id}'")
    return result


@router.get("/errors", dependencies=[Depends(auth("admin,expert"))])
@render()
async def error_group_list(
    request: Request,
    days: int = 7,
    limit: int = 50,
    since: Optional[str] = None,
    until: Optional[str] = None,
    status: Optional[str] = None,
    app: Optional[str] = None,
):
    """Distinct failures, ranked by how many people they reached."""
    from analytics.reports import error_groups

    return await error_groups(
        days=days, limit=limit, since=since, until=until, status=status, app=app
    )


@router.get("/errors/{fingerprint}", dependencies=[Depends(auth("admin,expert"))])
@render()
async def error_group_view(request: Request, fingerprint: str, limit: int = 25):
    """One failure, its occurrences, and what they had in common."""
    from analytics.reports import error_group_detail

    result = await error_group_detail(fingerprint, limit=limit)
    if result is None:
        raise DataError(f"No error group '{fingerprint}'")
    return result


@router.patch("/errors/{fingerprint}/status", dependencies=[Depends(auth("admin,expert"))])
@render()
async def update_error_status(request: Request, fingerprint: str, body: ErrorStatusUpdate):
    """Acknowledge, resolve or ignore a failure.

    A resolved group that happens again is reopened automatically by the
    recorder, so this is a statement about now rather than a permanent verdict.
    """
    from analytics.reports import set_error_status

    actor = (kutils.current_user(request) or {}).get("sub")
    try:
        changed = await set_error_status(fingerprint, body.status, actor=actor)
    except ValueError as exc:
        raise DataError(str(exc)) from exc
    if not changed:
        raise DataError(f"No error group '{fingerprint}'")
    RECORDER.record_event(
        "expert.error_triaged",
        app="console",
        props={"fingerprint": fingerprint, "status": body.status},
    )
    return {"fingerprint": fingerprint, "status": body.status}


@router.get("/interactions", dependencies=[Depends(auth("admin,expert"))])
@render()
async def interaction_pages(
    request: Request,
    days: int = 30,
    limit: int = 25,
    since: Optional[str] = None,
    until: Optional[str] = None,
):
    """Which pages get clicked, and which ones frustrate people."""
    from analytics.reports import interaction_overview

    return await interaction_overview(days=days, limit=limit, since=since, until=until)


@router.get("/heatmap", dependencies=[Depends(auth("admin,expert"))])
@render()
async def heatmap(
    request: Request,
    path: str,
    days: int = 30,
    grid: int = 40,
    since: Optional[str] = None,
    until: Optional[str] = None,
    device_type: Optional[str] = None,
):
    """Where people clicked on one page, by element and by coordinate.

    `path` is a route pattern such as `/recipe-wrangler/[id]`, not a URL: one
    heatmap covers every recipe page rather than one visit to one recipe.
    """
    from analytics.reports import click_map

    return await click_map(
        path=path,
        days=days,
        grid=grid,
        since=since,
        until=until,
        device_type=device_type,
    )


@router.get("/vitals", dependencies=[Depends(auth("admin,expert"))])
@render()
async def vitals(
    request: Request,
    days: int = 7,
    limit: int = 25,
    since: Optional[str] = None,
    until: Optional[str] = None,
):
    """Page speed as the browser measured it, at the 75th percentile."""
    from analytics.reports import vitals_report

    return await vitals_report(days=days, limit=limit, since=since, until=until)


@router.get("/feedback/{feedback_id}/context", dependencies=[Depends(auth("admin,expert"))])
@render()
async def feedback_in_context(request: Request, feedback_id: int):
    """The exchange a piece of feedback was about.

    Resolved on demand rather than listed with the inbox: it costs a call to
    whichever service owns the conversation, and a reviewer opens one item at a
    time. Reading somebody's conversation is a privileged act even in service
    of reviewing their complaint, so it is recorded like every other one.
    """
    from analytics.reports import feedback_context

    result = await feedback_context(feedback_id)
    if result is None:
        raise DataError(f"No feedback with id {feedback_id}")
    RECORDER.record_event(
        "expert.feedback_context_read",
        app="console",
        props={"feedback_id": feedback_id, "target_type": result.get("kind")},
    )
    return result


@router.get("/health", dependencies=[Depends(auth("admin,expert"))])
@render()
async def analytics_health(request: Request):
    """Whether collection is on, and whether it is keeping up.

    The queue depth and the drop counters are the point: "no data" and "we are
    throwing data away" look identical in the console otherwise.
    """
    return RECORDER.health()


@router.get("/settings", dependencies=[Depends(auth("admin"))])
@render()
async def get_analytics_settings(request: Request):
    """The current switches, and who last moved each one.

    `updated_at` and `updated_by` have been written on every change since the
    first release and returned by nothing, so "who turned collection off, and
    when" was recorded and invisible. For a switch that decides whether a study
    is gathering data, that is the audit trail — and it is the one question
    asked after the fact, when nobody remembers.
    """
    from sqlalchemy import select

    from backend.postgres import POSTGRES_ASYNC_SESSION_FACTORY
    from sql import AnalyticsSetting

    values = await SETTINGS.refresh_if_stale()

    changed: Dict[str, Any] = {}
    try:
        async with POSTGRES_ASYNC_SESSION_FACTORY()() as db:
            rows = (
                await db.execute(
                    select(
                        AnalyticsSetting.key,
                        AnalyticsSetting.updated_at,
                        AnalyticsSetting.updated_by,
                    )
                )
            ).all()
        changed = {
            key: {
                "updated_at": updated_at.isoformat() if updated_at else None,
                "updated_by": updated_by,
            }
            for key, updated_at, updated_by in rows
        }
    except Exception:
        # A missing table must not take the settings page down with it; the
        # switches themselves come from the cache and are already in hand.
        logger.debug("analytics.settings_history_unavailable", exc_info=True)

    return {
        "settings": values,
        "defaults": DEFAULTS,
        "platform_enabled": RECORDER.enabled,
        # Only keys someone has actually changed appear here; anything absent
        # is still at its default and has never been touched.
        "changed": changed,
    }


@router.put("/settings/{key}", dependencies=[Depends(auth("admin"))])
@render()
async def put_analytics_setting(
    request: Request, key: str, body: AnalyticsSettingUpdate
):
    """Change one runtime setting.

    Only narrows what the deployment already permits: with ANALYTICS_ENABLED
    false, nothing here turns collection on.
    """
    try:
        value = SETTINGS.validate(key, body.value)
    except ValueError as exc:
        raise DataError(detail=str(exc), extra={"known": sorted(DEFAULTS)}) from exc

    user = kutils.current_user(request)
    actor = (user or {}).get("sub")
    await SETTINGS.put(key, value, actor)
    values = await SETTINGS.refresh_if_stale()

    # The change is itself activity: an operator turning collection off is
    # exactly the kind of thing a later "why is there no data" needs to find.
    RECORDER.record_event(
        "admin.settings_changed",
        app="console",
        props={"key": key, "value": value},
    )
    logger.info(
        "analytics.setting_changed",
        extra={"key": key, "value": value, "actor": actor},
    )
    return {"settings": values}


@router.post("/consent/invalidate", dependencies=[Depends(auth())])
@render()
async def invalidate_consent_cache(request: Request):
    """Drop this user's cached consent decision.

    Called right after they change the analytics toggle, so the next event is
    recorded under the new answer instead of waiting out the cache TTL.
    """
    user = kutils.current_user(request)
    sub = (user or {}).get("sub")
    CONSENT.invalidate(sub)
    return {"invalidated": bool(sub)}
