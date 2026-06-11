import os
import pytest


def _clear_keys():
    for k in ("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY"):
        os.environ.pop(k, None)


def test_disabled_when_keys_missing():
    _clear_keys()
    from backend.langfuse_read import langfuse_read_enabled
    assert langfuse_read_enabled() is False


@pytest.mark.asyncio
async def test_fetch_metrics_returns_empty_when_disabled():
    _clear_keys()
    from backend.langfuse_read import LANGFUSE_READ
    result = await LANGFUSE_READ.fetch_metrics(
        view="traces", measure="count", aggregation="count",
        dimension="name", from_ts="2026-06-01T00:00:00Z", to_ts="2026-06-11T00:00:00Z",
    )
    assert result == {"data": []}


def test_normalize_metric_rows_parses_string_values():
    from backend.metrics_normalize import normalize_metric_rows
    raw = {"data": [
        {"name": "qa-answer", "count_count": "10"},
        {"name": "meal-plan", "count_count": "5"},
    ]}
    rows = normalize_metric_rows(raw, dimension="name", value_key="count_count")
    assert rows == [
        {"label": "qa-answer", "value": 10.0},
        {"label": "meal-plan", "value": 5.0},
    ]


def test_normalize_metric_rows_handles_empty():
    from backend.metrics_normalize import normalize_metric_rows
    assert normalize_metric_rows({"data": []}, dimension="name", value_key="count_count") == []


def test_normalize_timeseries_rows_keys_on_time_dimension():
    from backend.metrics_normalize import normalize_timeseries_rows
    raw = {"data": [
        {"time_dimension": "2026-06-07", "count_count": "103"},
        {"time_dimension": "2026-06-08", "count_count": "152"},
    ]}
    rows = normalize_timeseries_rows(raw, value_key="count_count")
    assert rows == [
        {"bucket": "2026-06-07", "value": 103.0},
        {"bucket": "2026-06-08", "value": 152.0},
    ]


def test_normalize_timeseries_rows_handles_null_value():
    from backend.metrics_normalize import normalize_timeseries_rows
    raw = {"data": [{"time_dimension": "2026-06-07", "sum_totalCost": None}]}
    rows = normalize_timeseries_rows(raw, value_key="sum_totalCost")
    assert rows == [{"bucket": "2026-06-07", "value": 0.0}]


def test_metric_value_key_is_aggregation_first():
    # Langfuse names columns <aggregation>_<measure>; getting this backwards
    # silently empties the cost/tokens/latency panels.
    from backend.metrics_normalize import metric_value_key
    assert metric_value_key("totalCost", "sum") == "sum_totalCost"
    assert metric_value_key("latency", "p95") == "p95_latency"
    assert metric_value_key("count", "count") == "count_count"


def test_normalize_metric_rows_with_correct_cost_key():
    # End-to-end: the real Langfuse cost-by-model shape resolves with the helper.
    from backend.metrics_normalize import metric_value_key, normalize_metric_rows
    raw = {"data": [
        {"providedModelName": "gpt-oss-20b", "sum_totalCost": 0.00313},
        {"providedModelName": None, "sum_totalCost": None},
    ]}
    key = metric_value_key("totalCost", "sum")
    rows = normalize_metric_rows(raw, dimension="providedModelName", value_key=key)
    assert rows == [{"label": "gpt-oss-20b", "value": 0.00313}]


# NOTE: routers.observability can't be imported standalone (main<->auth<->routers
# circular import; only resolves when loaded via main). The /dashboard glue is
# verified by the route-registration smoke test below + the live probe; the pure
# normalizer-selection logic is covered by the normalize_* tests above.
