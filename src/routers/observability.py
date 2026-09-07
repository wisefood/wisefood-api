import asyncio
from typing import Optional

from fastapi import APIRouter, Depends, Query, Request

import context
from analytics import RECORDER, SETTINGS
from auth import auth
from backend.langfuse_read import LANGFUSE_READ, langfuse_read_enabled
from backend.metrics_normalize import metric_value_key, normalize_metric_rows, normalize_timeseries_rows
from routers.generic import render

router = APIRouter(prefix="/api/v1/observability", tags=["Observability"])


# Why the split between what an expert may see and what an admin may see.
#
# Aggregates — counts, cost, tokens, latency, per model or per feature — carry
# no user content, and an expert needs them to reason about the service. Raw
# traces are a different thing entirely: a Langfuse trace holds the prompt a
# person typed, the model's answer, and every intermediate agent call, for
# whoever happened to be using the platform. That is the content of other
# people's conversations, and "expert" is a role granted for curating the
# corpus, not for reading the userbase's questions.
#
# Experts review Q&A content through `/analytics/qa`, which is scoped to
# questions asked of FoodScholar and carries consent-aware identity. That is
# the reviewing surface; this is the operating one.
def _is_admin() -> bool:
    return "admin" in context.get_user_roles()


def _audit_trace_access(scope: str, count: int) -> None:
    """Record who read raw trace content, and how much of it.

    Reading other people's prompts is exactly the kind of privileged action the
    platform has never recorded — the audit found that every expert and admin
    proxy authorised and forwarded without leaving any trace of who did what.
    """
    RECORDER.record_event(
        "admin.traces_read",
        app="console",
        props={"scope": scope, "count": int(count or 0)},
    )


@router.get("/status", dependencies=[Depends(auth("admin,expert"))])
@render()
async def status(request: Request):
    """Whether traces can be read, and whether any are still being produced.

    The two are independent: turning tracing off from the console stops new
    traces without hiding the ones already recorded.
    """
    enabled = langfuse_read_enabled()
    reachable = await LANGFUSE_READ.reachable() if enabled else False
    values = await SETTINGS.refresh_if_stale()
    return {
        "enabled": enabled,
        "langfuse_reachable": reachable,
        "tracing_enabled": bool(values.get("tracing.enabled", True)),
        "tracing_langfuse": bool(
            values.get("tracing.enabled", True) and values.get("tracing.langfuse", True)
        ),
        "can_read_traces": _is_admin(),
    }


@router.get("/metrics", dependencies=[Depends(auth("admin,expert"))])
@render()
async def metrics(
    request: Request,
    from_ts: str = Query(..., alias="from"),
    to_ts: str = Query(..., alias="to"),
    view: str = Query("observations"),
    measure: str = Query("count"),
    aggregation: str = Query("count"),
    dimension: Optional[str] = Query("providedModelName"),
    granularity: Optional[str] = Query(None),
):
    raw = await LANGFUSE_READ.fetch_metrics(
        view=view, measure=measure, aggregation=aggregation,
        dimension=dimension, from_ts=from_ts, to_ts=to_ts, granularity=granularity,
    )
    value_key = metric_value_key(measure, aggregation)
    rows = normalize_metric_rows(raw, dimension=dimension or "name", value_key=value_key)
    return {"rows": rows, "enabled": langfuse_read_enabled()}


@router.get("/traces", dependencies=[Depends(auth("admin"))])
@render()
async def traces(
    request: Request,
    limit: int = Query(50, ge=1, le=100),
    tag: Optional[str] = Query(None),
):
    """Raw Langfuse traces. Admin only — see the note above.

    A trace carries the prompt a person typed and the answer they got, for
    whoever was using the platform at the time. Experts review answer quality
    through the Q&A endpoints, which are scoped and consent-aware; this is the
    unscoped operational view.
    """
    rows = await LANGFUSE_READ.fetch_traces(limit=limit, tag=tag)
    _audit_trace_access("traces", len(rows or []))
    return {"traces": rows, "enabled": langfuse_read_enabled()}


@router.get("/prompts", dependencies=[Depends(auth("admin,expert"))])
@render()
async def prompts(request: Request):
    return {"prompts": await LANGFUSE_READ.fetch_prompts(), "enabled": langfuse_read_enabled()}


# `{name:path}` because Langfuse prompt names are namespaced with slashes
# ("foodchat/batch_grader_user"). A plain `{name}` never matches across a
# `/`, so every namespaced prompt 404d and the console drawer showed
# "Could not load this prompt."
@router.get("/prompts/{name:path}", dependencies=[Depends(auth("admin,expert"))])
@render()
async def prompt_detail(
    request: Request,
    name: str,
    label: Optional[str] = Query(None, max_length=64),
    version: Optional[int] = Query(None, ge=1),
):
    """One prompt, with what a reader needs to understand its template.

    ``template`` says which variables it expects, where messages get spliced
    in, what other prompts it references, and whether it uses Langfuse's
    ``{{var}}`` or FoodChat's ``{var}`` convention — the console cannot show
    a fill-in preview without knowing that, and a person cannot tell by eye.
    """
    from backend.prompt_template import describe

    prompt = await LANGFUSE_READ.fetch_prompt(name, label=label, version=version)
    return {
        "prompt": prompt,
        "template": describe(prompt) if prompt else None,
        "enabled": langfuse_read_enabled(),
    }


async def _none():
    """A resolved no-op, so the dashboard's gather stays one shape."""
    return None


# Observation latency from Langfuse is in MILLISECONDS; cost in USD; tokens count.
async def _metric(view, measure, aggregation, dimension, from_ts, to_ts, granularity):
    raw = await LANGFUSE_READ.fetch_metrics(
        view=view, measure=measure, aggregation=aggregation,
        dimension=dimension, from_ts=from_ts, to_ts=to_ts, granularity=granularity,
    )
    value_key = metric_value_key(measure, aggregation)
    if granularity:
        return normalize_timeseries_rows(raw, value_key=value_key)
    return normalize_metric_rows(raw, dimension=dimension or "name", value_key=value_key)


@router.get("/dashboard", dependencies=[Depends(auth("admin,expert"))])
@render()
async def dashboard(
    request: Request,
    from_ts: str = Query(..., alias="from"),
    to_ts: str = Query(..., alias="to"),
    granularity: str = Query("day"),
):
    """One bundled call that fans out every observability query the dashboard
    needs, server-side and concurrently. Returns normalized panels so the browser
    makes a single round-trip. Degrades to empty panels when Langfuse is off."""
    enabled = langfuse_read_enabled()
    admin = _is_admin()
    if not enabled:
        return {
            "enabled": False,
            "requests_over_time": [], "cost_over_time": [], "tokens_over_time": [],
            "requests_by_model": [], "cost_by_model": [], "tokens_by_model": [],
            "latency_by_model": {"p50": [], "p95": [], "p99": []},
            "requests_by_feature": [],
            "traces": [], "prompts": [],
        }

    obs = "observations"
    (
        requests_over_time, cost_over_time, tokens_over_time,
        requests_by_model, cost_by_model, tokens_by_model,
        lat_p50, lat_p95, lat_p99,
        requests_by_feature,
        traces, prompts,
    ) = await asyncio.gather(
        _metric(obs, "count", "count", None, from_ts, to_ts, granularity),
        _metric(obs, "totalCost", "sum", None, from_ts, to_ts, granularity),
        _metric(obs, "totalTokens", "sum", None, from_ts, to_ts, granularity),
        _metric(obs, "count", "count", "providedModelName", from_ts, to_ts, None),
        _metric(obs, "totalCost", "sum", "providedModelName", from_ts, to_ts, None),
        _metric(obs, "totalTokens", "sum", "providedModelName", from_ts, to_ts, None),
        _metric(obs, "latency", "p50", "providedModelName", from_ts, to_ts, None),
        _metric(obs, "latency", "p95", "providedModelName", from_ts, to_ts, None),
        _metric(obs, "latency", "p99", "providedModelName", from_ts, to_ts, None),
        _metric("traces", "count", "count", "name", from_ts, to_ts, None),
        # Raw traces only for admins. Restricting `/traces` while the dashboard
        # kept bundling 25 of them would have moved the door, not closed it.
        LANGFUSE_READ.fetch_traces(limit=25) if admin else _none(),
        LANGFUSE_READ.fetch_prompts(),
    )
    if admin:
        _audit_trace_access("dashboard", len(traces or []))

    return {
        "enabled": True,
        "requests_over_time": requests_over_time,
        "cost_over_time": cost_over_time,
        "tokens_over_time": tokens_over_time,
        "requests_by_model": requests_by_model,
        "cost_by_model": cost_by_model,
        "tokens_by_model": tokens_by_model,
        "latency_by_model": {"p50": lat_p50, "p95": lat_p95, "p99": lat_p99},
        "requests_by_feature": requests_by_feature,
        "traces": traces or [],
        # So the console can say "admin only" rather than showing an empty
        # panel that looks like a broken query.
        "traces_restricted": not admin,
        "prompts": prompts,
    }
