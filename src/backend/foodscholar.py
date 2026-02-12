import uuid
import httpx
from typing import Any, Dict, Optional
from main import config
from api.v1.households import HOUSEHOLD
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
        return await cls.get(f"/user/{user_id}/sessions")

    @classmethod
    async def get_session_history(cls, user_id: str, session_id: str):
        return await cls.get(f"/user/{user_id}/session/{session_id}/history")

    @classmethod
    async def create_session(cls, user: dict, member_id: Optional[str] = None):

        context = "Name: " + user.get("name", "Anonymous")
        if member_id:
            household = await HOUSEHOLD.get_by_owner(user["sub"])
            member = await HOUSEHOLD.get_member(member_id)
            member_profile = await HOUSEHOLD.get_member_profile(member_id)
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
        return await FOODSCHOLAR.post("/start", json=spec)

    @classmethod
    async def chat_message(cls, session_id: str, user: dict, message: str):
        spec = {
            "session_id": session_id,
            "user_id": user["sub"],
            "message": message,
        }
        return await FOODSCHOLAR.post("/chat", json=spec)


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
    async def submit_qa_feedback(cls, payload: dict):
        return await FOODSCHOLAR.post("/api/v1/qa/feedback", json=payload)

    @classmethod
    async def list_qa_models(cls):
        return await FOODSCHOLAR.get("/api/v1/qa/models")

FOODSCHOLAR = FoodScholar.get_client()
