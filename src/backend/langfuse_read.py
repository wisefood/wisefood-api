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
    async def push_score(
        cls,
        *,
        trace_id: str,
        name: str,
        value,
        comment: Optional[str] = None,
    ) -> Optional[str]:
        """Attach an expert's verdict to a trace, and return the score's id.

        The schema has always described doing this and nothing ever did, which
        left every expert judgement stranded in one database with no way to see
        it beside the trace it is about. Pushing it makes the verdict visible
        where the answer is, to anyone debugging that answer.

        Returns None on any failure, including Langfuse being switched off or
        unreachable. Deliberately: an expert's verdict is already stored by the
        time this runs, and losing the annotation is a smaller harm than losing
        the review because a third-party service was down. Also honours the
        platform tracing switch — an operator who turned tracing off did not
        expect the console to keep writing to Langfuse.
        """
        client = cls._get_client()
        if client is None or not trace_id:
            return None
        try:
            from analytics import SETTINGS

            if not SETTINGS.tracing_enabled("langfuse"):
                return None
        except Exception:
            pass
        payload: Dict[str, Any] = {
            "traceId": trace_id,
            "name": name,
            "value": value,
            "dataType": "NUMERIC" if isinstance(value, (int, float)) else "CATEGORICAL",
        }
        if comment:
            payload["comment"] = comment[:1000]
        try:
            response = await client.post("/api/public/scores", json=payload)
            response.raise_for_status()
            body = response.json()
            return str(body.get("id") or "")[:64] or None
        except Exception:
            logger.warning("langfuse.score_push_failed", exc_info=True)
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
