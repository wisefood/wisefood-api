"""Pure helpers for shaping Langfuse v1 Metrics API responses for the UI.

Kept dependency-free (no FastAPI/auth imports) so it is unit-testable in
isolation and reusable across routers.
"""
from typing import Any, Dict, List


def metric_value_key(measure: str, aggregation: str) -> str:
    """Langfuse v1 names each metric column `<aggregation>_<measure>`
    (e.g. sum_totalCost, p95_latency, count_count) — NOT measure-first. Getting
    this order wrong silently yields null values and empty panels."""
    return f"{aggregation}_{measure}"


def normalize_metric_rows(raw: Dict[str, Any], *, dimension: str, value_key: str) -> List[Dict[str, Any]]:
    """Convert v1 metrics rows ({dimension: label, "<measure>_<agg>": "10"}) to
    [{label, value(float)}]. Skips rows that can't be parsed."""
    rows: List[Dict[str, Any]] = []
    for item in raw.get("data", []) if isinstance(raw, dict) else []:
        if not isinstance(item, dict):
            continue
        label = item.get(dimension)
        try:
            value = float(item.get(value_key))
        except (TypeError, ValueError):
            continue
        rows.append({"label": str(label) if label is not None else "unknown", "value": value})
    return rows


def normalize_timeseries_rows(raw: Dict[str, Any], *, value_key: str) -> List[Dict[str, Any]]:
    """Convert time-bucketed v1 metrics rows ({"time_dimension": "2026-06-07",
    "<measure>_<agg>": "103"}) to [{bucket, value(float)}]. A null/unparseable
    value becomes 0.0 (a gap in the series) rather than dropping the bucket, so
    the time axis stays continuous."""
    rows: List[Dict[str, Any]] = []
    for item in raw.get("data", []) if isinstance(raw, dict) else []:
        if not isinstance(item, dict):
            continue
        bucket = item.get("time_dimension")
        if bucket is None:
            continue
        try:
            value = float(item.get(value_key))
        except (TypeError, ValueError):
            value = 0.0
        rows.append({"bucket": str(bucket), "value": value})
    return rows
