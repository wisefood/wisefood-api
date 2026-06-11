"""Read-only Langfuse public-API client. No-op unless both keys are set.

Self-hosted Langfuse: use the v1 Metrics API (`/api/public/metrics`); the v2
Metrics API is Cloud-only. All methods degrade to empty data on any failure and
never raise into the request path (logged at WARNING).
"""
import json
import logging
import os
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)

_DEFAULT_BASE_URL = "http://langfuse-web.langfuse.svc.cluster.local:3000"


def langfuse_read_enabled() -> bool:
    return bool(os.getenv("LANGFUSE_PUBLIC_KEY") and os.getenv("LANGFUSE_SECRET_KEY"))


class LangfuseRead:
    """Singleton httpx client for the Langfuse public API (Basic auth)."""

    _client: Optional[httpx.AsyncClient] = None

    @classmethod
    def _get_client(cls) -> Optional[httpx.AsyncClient]:
        if not langfuse_read_enabled():
            return None
        if cls._client is None:
            base = os.getenv("LANGFUSE_BASE_URL", _DEFAULT_BASE_URL).rstrip("/")
            cls._client = httpx.AsyncClient(
                base_url=base,
                auth=(os.environ["LANGFUSE_PUBLIC_KEY"], os.environ["LANGFUSE_SECRET_KEY"]),
                timeout=20.0,
            )
        return cls._client

    @classmethod
    async def fetch_metrics(
        cls, *, view: str, measure: str, aggregation: str,
        dimension: Optional[str], from_ts: str, to_ts: str,
        granularity: Optional[str] = None,
    ) -> Dict[str, Any]:
        """v1 GET /api/public/metrics. Returns {"data": [...]} or {"data": []} on failure."""
        client = cls._get_client()
        if client is None:
            return {"data": []}
        query: Dict[str, Any] = {
            "view": view,
            "metrics": [{"measure": measure, "aggregation": aggregation}],
            "dimensions": [{"field": dimension}] if dimension else [],
            "filters": [],
            "fromTimestamp": from_ts,
            "toTimestamp": to_ts,
        }
        if granularity:
            query["timeDimension"] = {"granularity": granularity}
        try:
            resp = await client.get("/api/public/metrics", params={"query": json.dumps(query)})
            resp.raise_for_status()
            body = resp.json()
            return {"data": body.get("data", []) if isinstance(body, dict) else []}
        except Exception as exc:  # noqa: BLE001 — never raise into request path
            logger.warning("Langfuse fetch_metrics failed: %s", exc)
            return {"data": []}

    @classmethod
    async def fetch_traces(cls, *, limit: int = 50, tag: Optional[str] = None) -> List[Dict[str, Any]]:
        client = cls._get_client()
        if client is None:
            return []
        params: Dict[str, Any] = {"limit": max(1, min(limit, 100))}
        if tag:
            params["tags"] = tag
        try:
            resp = await client.get("/api/public/traces", params=params)
            resp.raise_for_status()
            body = resp.json()
            return body.get("data", []) if isinstance(body, dict) else []
        except Exception as exc:  # noqa: BLE001
            logger.warning("Langfuse fetch_traces failed: %s", exc)
            return []

    @classmethod
    async def fetch_prompts(cls) -> List[Dict[str, Any]]:
        client = cls._get_client()
        if client is None:
            return []
        try:
            resp = await client.get("/api/public/v2/prompts", params={"limit": 100})
            resp.raise_for_status()
            body = resp.json()
            return body.get("data", []) if isinstance(body, dict) else []
        except Exception as exc:  # noqa: BLE001
            logger.warning("Langfuse fetch_prompts failed: %s", exc)
            return []

    @classmethod
    async def fetch_prompt(cls, name: str) -> Optional[Dict[str, Any]]:
        client = cls._get_client()
        if client is None:
            return None
        try:
            resp = await client.get(f"/api/public/v2/prompts/{name}")
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Langfuse fetch_prompt(%s) failed: %s", name, exc)
            return None

    @classmethod
    async def reachable(cls) -> bool:
        client = cls._get_client()
        if client is None:
            return False
        try:
            resp = await client.get("/api/public/health")
            return resp.status_code < 500
        except Exception:  # noqa: BLE001
            return False


LANGFUSE_READ = LangfuseRead
