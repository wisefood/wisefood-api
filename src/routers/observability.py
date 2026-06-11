from typing import Optional

from fastapi import APIRouter, Depends, Query, Request

from auth import auth
from backend.langfuse_read import LANGFUSE_READ, langfuse_read_enabled
from backend.metrics_normalize import normalize_metric_rows
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
