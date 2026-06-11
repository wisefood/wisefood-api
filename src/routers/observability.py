import asyncio
from typing import Optional

from fastapi import APIRouter, Depends, Query, Request

from auth import auth
from backend.langfuse_read import LANGFUSE_READ, langfuse_read_enabled
from backend.metrics_normalize import normalize_metric_rows, normalize_timeseries_rows
from routers.generic import render

router = APIRouter(prefix="/api/v1/observability", tags=["Observability"])


@router.get("/status", dependencies=[Depends(auth("admin,expert"))])
@render()
async def status(request: Request):
    enabled = langfuse_read_enabled()
    reachable = await LANGFUSE_READ.reachable() if enabled else False
    return {"enabled": enabled, "langfuse_reachable": reachable}


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
    value_key = f"{measure}_{aggregation}"
    rows = normalize_metric_rows(raw, dimension=dimension or "name", value_key=value_key)
    return {"rows": rows, "enabled": langfuse_read_enabled()}


@router.get("/traces", dependencies=[Depends(auth("admin,expert"))])
@render()
async def traces(
    request: Request,
    limit: int = Query(50, ge=1, le=100),
    tag: Optional[str] = Query(None),
):
    rows = await LANGFUSE_READ.fetch_traces(limit=limit, tag=tag)
    return {"traces": rows, "enabled": langfuse_read_enabled()}


@router.get("/prompts", dependencies=[Depends(auth("admin,expert"))])
@render()
async def prompts(request: Request):
    return {"prompts": await LANGFUSE_READ.fetch_prompts(), "enabled": langfuse_read_enabled()}


@router.get("/prompts/{name}", dependencies=[Depends(auth("admin,expert"))])
@render()
async def prompt_detail(request: Request, name: str):
    return {"prompt": await LANGFUSE_READ.fetch_prompt(name), "enabled": langfuse_read_enabled()}


# Observation latency from Langfuse is in MILLISECONDS; cost in USD; tokens count.
async def _metric(view, measure, aggregation, dimension, from_ts, to_ts, granularity):
    raw = await LANGFUSE_READ.fetch_metrics(
        view=view, measure=measure, aggregation=aggregation,
        dimension=dimension, from_ts=from_ts, to_ts=to_ts, granularity=granularity,
    )
    value_key = f"{measure}_{aggregation}"
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
        LANGFUSE_READ.fetch_traces(limit=25),
        LANGFUSE_READ.fetch_prompts(),
    )

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
        "traces": traces,
        "prompts": prompts,
    }
