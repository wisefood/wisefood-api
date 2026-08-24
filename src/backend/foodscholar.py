import uuid
import httpx
from typing import Any, Dict, Optional
from main import config
from api.v1.households import HOUSEHOLD
from api.v1.household_members import HOUSEHOLD_MEMBER
import logging

logger = logging.getLogger(__name__)


class FoodScholar:
    """Singleton HTTP client for accessing the FoodScholar API with connection pooling."""

    _client: Optional[httpx.AsyncClient] = None

    @classmethod
    def get_client(
        cls,
        base_url: str = config.settings["FOODSCHOLAR_URL"],
        timeout: float = 15.0,
        max_connections: int = 15,
        max_keepalive_connections: int = 7,
        verify: bool = True,
        http2: bool = True,
    ) -> "FoodScholar":
        """Get or create a singleton FoodScholar client instance."""
        if cls._client is None:
            cls._client = httpx.AsyncClient(
                base_url=base_url.rstrip("/"),
                timeout=timeout,
                verify=verify,
                http2=http2,
                limits=httpx.Limits(
                    max_connections=max_connections,
                    max_keepalive_connections=max_keepalive_connections,
                ),
            )
        return cls

    @classmethod
    async def get(
        cls, endpoint: str, params: Optional[Dict[str, Any]] = None, **kwargs
    ):
        if cls._client is None:
            raise RuntimeError(
                "FoodScholar client not initialized. Call get_client() first."
            )
        response = await cls._client.get(endpoint, params=params, **kwargs)
        response.raise_for_status()
        return response.json()

    @classmethod
    async def post(cls, endpoint: str, data: Any = None, json: Any = None, **kwargs):
        if cls._client is None:
            raise RuntimeError(
                "FoodScholar client not initialized. Call get_client() first."
            )
        response = await cls._client.post(endpoint, data=data, json=json, **kwargs)
        response.raise_for_status()
        return response.json()

    @classmethod
    async def put(cls, endpoint: str, data: Any = None, json: Any = None, **kwargs):
        if cls._client is None:
            raise RuntimeError(
                "FoodScholar client not initialized. Call get_client() first."
            )
        response = await cls._client.put(endpoint, data=data, json=json, **kwargs)
        response.raise_for_status()
        return response.json()

    @classmethod
    async def delete(cls, endpoint: str, **kwargs):
        if cls._client is None:
            raise RuntimeError(
                "FoodScholar client not initialized. Call get_client() first."
            )
        response = await cls._client.delete(endpoint, **kwargs)
        response.raise_for_status()
        return response.json() if response.text else {"status": "deleted"}

    @classmethod
    async def aclose(cls):
        if cls._client:
            await cls._client.aclose()
            cls._client = None

    @classmethod
    async def status(cls):
        return await cls.get("/")

    @classmethod
    async def get_user_sessions(cls, user_id: str):
        return await cls.get(f"/api/v1/sessions/users/{user_id}")

    @classmethod
    async def get_session_history(cls, user_id: str, session_id: str):
        return await cls.get(
            f"/api/v1/sessions/{session_id}/history",
            params={"user_id": user_id},
        )

    @classmethod
    async def create_session(cls, user: dict, member_id: Optional[str] = None):

        context = "Name: " + user.get("name", "Anonymous")
        if member_id:
            # Member/profile live on HOUSEHOLD_MEMBER — calling them on
            # HOUSEHOLD raised AttributeError and 500'd every session-create
            # that carried a member_id (fixed in M1).
            household = await HOUSEHOLD.get_by_owner(user["sub"])
            member = await HOUSEHOLD_MEMBER.get(member_id)
            member_profile = await HOUSEHOLD_MEMBER.get_member_profile(member_id)
            if member_profile:
                context += f"Name: {member.get('name', 'Unknown member')}"
                context += f", Region (ISO-3166-1 alpha-2): {household.get('region', 'Unknown region')}"
                context += f", Age: {member.get('age_group', 'Unknown age group')}"
                context += f", Gender: {member_profile.get('nutritional_preferences', {}).get('gender', 'Unknown gender')}"
                context += f" with profile: dietary groups: {', '.join(member_profile.get('dietary_groups', []))}"
                dietary_preferences = member_profile.get('nutritional_preferences', {}).get('dietary_preferences', {})
                preferences_text = []
                for category, items in dietary_preferences.items():
                    preferences_text.append(f"{category}: " + ", ".join([f"{item} ({preference})" for item, preference in items.items()]))
                context += f" with nutritional preferences: {member_profile.get('nutritional_preferences', {}).get('notes', '')}. Dietary preferences: " + "; ".join(preferences_text)

        logger.debug(context)
        spec = {
            "session_id": str(uuid.uuid4()),
            "user_context": str(context),
            "user_id": user["sub"],
            "max_history": 20,
        }
        return await FOODSCHOLAR.post("/api/v1/sessions/start", json=spec)

    @classmethod
    async def chat_message(cls, session_id: str, user: dict, message: str):
        spec = {
            "session_id": session_id,
            "user_id": user["sub"],
            "message": message,
        }
        return await FOODSCHOLAR.post("/api/v1/sessions/chat", json=spec)


    @classmethod
    async def get_search_summary(cls, query: str, results: list, language: str, user_id: str, expertise_level: str):
        spec = {
            "results": results,
            "query": query
        }
        return await FOODSCHOLAR.post("/api/v1/search/summarize", json=spec)

    @classmethod
    async def enrich_article(cls, urn: str, title: str, abstract: str, authors: Optional[str] = None):
        spec = {
            "urn": urn,
            "title": title,
            "abstract": abstract,
            "authors": authors,
        }
        return await FOODSCHOLAR.post("/api/v1/enrich/article", json=spec)

    @classmethod
    async def ask_question(cls, payload: dict):
        return await FOODSCHOLAR.post("/api/v1/qa/ask", json=payload)

    @classmethod
    async def ask_question_stream(cls, payload: dict):
        """Proxy FoodScholar's streaming QA endpoint as raw SSE bytes.

        An async generator over the upstream ``text/event-stream`` body,
        yielded chunk-for-chunk with no buffering or reframing — the agentic
        pipeline's stage/step/answer_delta events pass through untouched.

        The pooled client's 15 s default timeout would kill a stream mid-
        answer, so reads get a generous window; the upstream sends keep-alive
        comments every 15 s, which keeps the read timer fed even during long
        LLM calls.
        """
        if cls._client is None:
            raise RuntimeError(
                "FoodScholar client not initialized. Call get_client() first."
            )
        timeout = httpx.Timeout(connect=10.0, read=180.0, write=30.0, pool=10.0)
        async with cls._client.stream(
            "POST", "/api/v1/qa/ask/stream", json=payload, timeout=timeout
        ) as response:
            response.raise_for_status()
            async for chunk in response.aiter_bytes():
                yield chunk

    @classmethod
    async def submit_qa_feedback(cls, payload: dict):
        return await FOODSCHOLAR.post("/api/v1/qa/feedback", json=payload)

    @classmethod
    async def submit_memory_decision(cls, payload: dict):
        """Accept/decline a memory nudge surfaced by a QA answer."""
        return await FOODSCHOLAR.post("/api/v1/qa/memory", json=payload)

    @classmethod
    async def list_qa_models(cls):
        return await FOODSCHOLAR.get("/api/v1/qa/models")

    @classmethod
    async def get_suggested_questions(cls):
        return await FOODSCHOLAR.get("/api/v1/qa/questions")

    @classmethod
    async def get_tips(cls, member_id: Optional[str] = None):
        # member_id personalizes tips against the member's accumulated
        # profile (FoodScholar falls back to generic content without it).
        params = {"member_id": member_id} if member_id else None
        return await FOODSCHOLAR.get("/api/v1/qa/tips", params=params)

    @classmethod
    async def enqueue_article_enrichment(cls, urn: str, payload: dict):
        """Queue selective enrichment for a single catalog article."""
        return await cls.post(f"/api/v1/enrich/articles/{urn}", json=payload)

    @classmethod
    async def enqueue_articles_enrichment(cls, payload: dict):
        """Queue selective enrichment for several catalog articles."""
        return await cls.post("/api/v1/enrich/articles", json=payload)

    @classmethod
    async def get_article_enrichment_status(cls, urn: str):
        return await cls.get(f"/api/v1/enrich/articles/{urn}")

    @classmethod
    async def get_article_enrichment_statuses(cls, urns: list[str]):
        # FoodScholar expects the parameter repeated once per URN.
        return await cls.get("/api/v1/enrich/jobs", params={"urns": urns})

    @classmethod
    async def reset_article_enrichment(cls, urn: str):
        return await cls.delete(f"/api/v1/enrich/articles/{urn}")

    @classmethod
    async def get_enrichment_worker_status(cls):
        return await cls.get("/api/v1/enrich/worker")

    @classmethod
    async def set_enrichment_sweeper_paused(cls, paused: bool):
        return await cls.post("/api/v1/enrich/worker/pause", json={"paused": paused})

    @classmethod
    async def restart_enrichment_workers(cls, payload: dict):
        return await cls.post("/api/v1/enrich/worker/restart", json=payload)

    @classmethod
    async def get_guideline_storage(cls, artifact_uuid: str):
        return await cls.get(f"/api/v1/guidelines/storage/{artifact_uuid}")

    @classmethod
    async def enqueue_guideline_extraction(cls, artifact_uuid: str, payload: dict | None = None):
        # The payload carries guide_id and the document-profiling options; an
        # empty body means the run has no idea which guide it is reading, and
        # every rule it extracts loses its population context.
        return await cls.post(
            f"/api/v1/guidelines/extract/{artifact_uuid}", json=payload or {}
        )

    @classmethod
    async def get_guideline_worker_status(cls):
        return await cls.get("/api/v1/guidelines/worker/status")

    @classmethod
    async def get_guideline_extraction_status(cls, artifact_uuid: str):
        return await cls.get(f"/api/v1/guidelines/extract/{artifact_uuid}")

    @classmethod
    async def import_guidelines(cls, artifact_uuid: str, payload: dict):
        return await cls.post(f"/api/v1/guidelines/import/{artifact_uuid}", json=payload)

    # ------------------------------------------------------------------ #
    # Guideline facet enrichment (post-extraction)
    # ------------------------------------------------------------------ #

    @classmethod
    async def preview_guideline_enrichment(cls, payload: dict):
        return await cls.post("/api/v1/guidelines/enrichment/preview", json=payload)

    @classmethod
    async def enqueue_guideline_enrichment(cls, payload: dict):
        return await cls.post("/api/v1/guidelines/enrichment/enqueue", json=payload)

    @classmethod
    async def get_guideline_enrichment_status(cls):
        return await cls.get("/api/v1/guidelines/enrichment/status")

    @classmethod
    async def get_guideline_enrichment_worker_status(cls):
        return await cls.get("/api/v1/guidelines/enrichment/worker/status")

    # ------------------------------------------------------------------ #
    # Guideline corpus state and activation
    # ------------------------------------------------------------------ #

    @classmethod
    async def audit_guideline_corpus(cls):
        return await cls.get("/api/v1/guidelines/corpus/audit")

    @classmethod
    async def get_guideline_activation_plan(cls, require_verified: bool = True):
        return await cls.get(
            "/api/v1/guidelines/corpus/activation-plan",
            params={"require_verified": require_verified},
        )

    @classmethod
    async def backfill_guide_page_summaries(
        cls, guide_urn: str, *, dry_run: bool = True
    ):
        """Write extraction page summaries onto a guide's existing rules."""
        return await cls.post(
            f"/api/v1/guidelines/corpus/page-summaries/backfill/{guide_urn}",
            params={"dry_run": dry_run},
        )

    @classmethod
    async def activate_guide_guidelines(
        cls,
        guide_urn: str,
        *,
        require_verified: bool = True,
        dry_run: bool = True,
    ):
        return await cls.post(
            f"/api/v1/guidelines/corpus/activate/{guide_urn}",
            params={"require_verified": require_verified, "dry_run": dry_run},
        )


FOODSCHOLAR = FoodScholar.get_client()
