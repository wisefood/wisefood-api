import uuid
import httpx
from typing import Any, Dict, Optional
from main import config
from api.v1.households import HOUSEHOLD
import logging

logger = logging.getLogger(__name__)


class FoodChat:
    """Singleton HTTP client for accessing the FoodChat API with connection pooling."""

    _client: Optional[httpx.AsyncClient] = None

    @classmethod
    def get_client(
        cls,
        base_url: str = config.settings["FOODCHAT_URL"],
        timeout: float = 15.0,
        max_connections: int = 15,
        max_keepalive_connections: int = 7,
        verify: bool = True,
        http2: bool = True,
    ) -> "FoodChat":
        """Get or create a singleton FoodChat client instance."""
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
                "FoodChat client not initialized. Call get_client() first."
            )
        response = await cls._client.get(endpoint, params=params, **kwargs)
        response.raise_for_status()
        return response.json()

    @classmethod
    async def post(cls, endpoint: str, data: Any = None, json: Any = None, **kwargs):
        if cls._client is None:
            raise RuntimeError(
                "FoodChat client not initialized. Call get_client() first."
            )
        response = await cls._client.post(endpoint, data=data, json=json, **kwargs)
        response.raise_for_status()
        return response.json()

    @classmethod
    async def put(cls, endpoint: str, data: Any = None, json: Any = None, **kwargs):
        if cls._client is None:
            raise RuntimeError(
                "FoodChat client not initialized. Call get_client() first."
            )
        response = await cls._client.put(endpoint, data=data, json=json, **kwargs)
        response.raise_for_status()
        return response.json()

    @classmethod
    async def delete(cls, endpoint: str, **kwargs):
        if cls._client is None:
            raise RuntimeError(
                "FoodChat client not initialized. Call get_client() first."
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
        return await cls.get("/health")

    @classmethod
    async def create_session(cls, member_id: str):
        """Create a new chat session for a household member."""
        return await cls.post("/foodchat/sessions", json={"member_id": member_id})

    @classmethod
    async def get_session(cls, session_id: str):
        """Get session state and metadata."""
        return await cls.get(f"/foodchat/sessions/{session_id}")

    @classmethod
    async def delete_session(cls, session_id: str):
        """Delete a session."""
        return await cls.delete(f"/foodchat/sessions/{session_id}")

    @classmethod
    async def send_message(cls, session_id: str, content: str):
        """Send a message and get a response. Uses 60s timeout for meal plan generation."""
        return await cls.post(
            f"/foodchat/sessions/{session_id}/messages",
            json={"content": content},
            timeout=60.0
        )

    @classmethod
    async def get_messages(cls, session_id: str, limit: Optional[int] = None):
        """Get message history for a session."""
        params = {}
        if limit is not None:
            params["limit"] = limit
        return await cls.get(f"/foodchat/sessions/{session_id}/messages", params=params or None)

    @classmethod
    async def get_meal_plans(cls, session_id: str):
        """Get all meal plans generated in a session."""
        return await cls.get(f"/foodchat/sessions/{session_id}/meal-plans")

    @classmethod
    async def get_member_sessions(cls, member_id: str):
        """Get all sessions for a specific member."""
        return await cls.get(f"/foodchat/members/{member_id}/sessions")

FOODCHAT = FoodChat.get_client()
