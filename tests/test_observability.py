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
