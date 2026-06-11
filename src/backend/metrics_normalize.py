"""Pure helpers for shaping Langfuse v1 Metrics API responses for the UI.

Kept dependency-free (no FastAPI/auth imports) so it is unit-testable in
isolation and reusable across routers.
"""
from typing import Any, Dict, List


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
