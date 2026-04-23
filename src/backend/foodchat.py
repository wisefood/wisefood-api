import logging
from typing import Any, Dict, Optional

import httpx

from exceptions import (
    APIException,
    AuthenticationError,
    AuthorizationError,
    BadGatewayError,
    DataError,
    GatewayTimeoutError,
    InvalidError,
    NotFoundError,
    RateLimitError,
    ServiceUnavailableError,
)
from main import config

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
    def _require_client(cls) -> httpx.AsyncClient:
        if cls._client is None:
            raise RuntimeError(
                "FoodChat client not initialized. Call get_client() first."
            )
        return cls._client

    @classmethod
    def _extract_error_payload(
        cls,
        response: httpx.Response,
    ) -> tuple[str, Any]:
        detail = f"FoodChat request failed with status {response.status_code}"
        errors = None

        try:
            payload = response.json()
        except ValueError:
            text = response.text.strip()
            return (text or detail, None)

        if isinstance(payload, dict):
            nested_error = payload.get("error")
            if isinstance(nested_error, dict):
                detail = (
                    nested_error.get("detail")
                    or nested_error.get("message")
                    or detail
                )
                errors = nested_error.get("errors")
            elif isinstance(payload.get("detail"), str):
                detail = payload["detail"]
            elif isinstance(payload.get("detail"), list):
                detail = "Validation failed"
                errors = payload["detail"]
            elif isinstance(payload.get("message"), str):
                detail = payload["message"]
        elif isinstance(payload, str):
            detail = payload

        return detail, errors

    @classmethod
    def _raise_api_error(cls, response: httpx.Response) -> APIException:
        detail, errors = cls._extract_error_payload(response)
        extra = {
            "title": "FoodChatError",
            "upstream_status": response.status_code,
        }
        retry_after = response.headers.get("Retry-After")
        retry_after_seconds = (
            int(retry_after) if retry_after and retry_after.isdigit() else None
        )

        if response.status_code == 400:
            return InvalidError(detail=detail, errors=errors, extra=extra)
        if response.status_code == 401:
            return AuthenticationError(detail=detail, extra=extra)
        if response.status_code == 403:
            return AuthorizationError(detail=detail, extra=extra)
        if response.status_code == 404:
            return NotFoundError(detail=detail, extra=extra)
        if response.status_code == 422:
            return DataError(detail=detail, errors=errors, extra=extra)
        if response.status_code == 429:
            return RateLimitError(
                detail=detail,
                retry_after=retry_after_seconds,
                extra=extra,
            )
        if response.status_code == 503:
            return ServiceUnavailableError(
                detail=detail,
                retry_after=retry_after_seconds,
                extra=extra,
            )
        if response.status_code == 504:
            return GatewayTimeoutError(detail=detail, extra=extra)
        if response.status_code >= 500:
            return BadGatewayError(detail=detail, extra=extra)
        return APIException(
            status_code=response.status_code,
            detail=detail,
            errors=errors,
            extra=extra,
        )

    @classmethod
    def _decode_response(cls, response: httpx.Response):
        if response.status_code == 204 or not response.content:
            return {"status": "deleted"}
        try:
            return response.json()
        except ValueError:
            return response.text

    @classmethod
    async def request(cls, method: str, endpoint: str, **kwargs):
        client = cls._require_client()

        try:
            response = await client.request(method, endpoint, **kwargs)
            response.raise_for_status()
        except httpx.TimeoutException as exc:
            raise GatewayTimeoutError(detail="FoodChat request timed out") from exc
        except httpx.HTTPStatusError as exc:
            raise cls._raise_api_error(exc.response) from exc
        except httpx.RequestError as exc:
            logger.warning(
                "foodchat.request_error",
                extra={
                    "method": method,
                    "endpoint": endpoint,
                    "error": str(exc),
                },
            )
            raise ServiceUnavailableError(
                detail="FoodChat service is unavailable"
            ) from exc

        return cls._decode_response(response)

    @classmethod
    async def get(
        cls, endpoint: str, params: Optional[Dict[str, Any]] = None, **kwargs
    ):
        return await cls.request("GET", endpoint, params=params, **kwargs)

    @classmethod
    async def post(cls, endpoint: str, data: Any = None, json: Any = None, **kwargs):
        return await cls.request("POST", endpoint, data=data, json=json, **kwargs)

    @classmethod
    async def put(cls, endpoint: str, data: Any = None, json: Any = None, **kwargs):
        return await cls.request("PUT", endpoint, data=data, json=json, **kwargs)

    @classmethod
    async def delete(cls, endpoint: str, **kwargs):
        return await cls.request("DELETE", endpoint, **kwargs)

    @classmethod
    def _member_params(cls, member_id: str) -> Dict[str, Any]:
        return {"member_id": member_id}

    @classmethod
    def _optional_limit_params(cls, limit: Optional[int]) -> Optional[Dict[str, Any]]:
        params: Dict[str, Any] = {}
        if limit is not None:
            params["limit"] = limit
        return params or None

    @classmethod
    def _conversation_params(
        cls,
        member_id: str,
        before_id: Optional[int] = None,
        limit: int = 20,
    ) -> Dict[str, Any]:
        params: Dict[str, Any] = {
            "member_id": member_id,
            "limit": limit,
        }
        if before_id is not None:
            params["before_id"] = before_id
        return params

    @classmethod
    def _message_payload(cls, content: str) -> Dict[str, Any]:
        return {"content": content}

    @classmethod
    def _chat_payload(cls, content: str, member_id: str) -> Dict[str, Any]:
        return {
            "content": content,
            "member_id": member_id,
        }

    @classmethod
    def _feedback_payload(
        cls,
        member_id: str,
        rating: str,
        comment: Optional[str] = None,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "member_id": member_id,
            "rating": rating,
        }
        if comment is not None:
            payload["comment"] = comment
        return payload

    @classmethod
    def _long_timeout(cls) -> float:
        return 60.0

    @classmethod
    def _extra_long_timeout(cls) -> float:
        return 90.0

    @classmethod
    async def aclose(cls):
        if cls._client:
            await cls._client.aclose()
            cls._client = None

    @classmethod
    async def status(cls):
        return await cls.get("/foodchat/health")

    @classmethod
    async def create_session(cls, member_id: str):
        """Create a new chat session for a household member."""
        return await cls.post("/foodchat/sessions", json={"member_id": member_id})

    @classmethod
    async def get_session(cls, session_id: str, member_id: str):
        """Get session state and metadata."""
        return await cls.get(
            f"/foodchat/sessions/{session_id}",
            params=cls._member_params(member_id),
        )

    @classmethod
    async def delete_session(cls, session_id: str, member_id: str):
        """Delete a session."""
        return await cls.delete(
            f"/foodchat/sessions/{session_id}",
            params=cls._member_params(member_id),
        )

    @classmethod
    async def send_message(cls, session_id: str, content: str):
        """Send a message and get a response. Uses 60s timeout for meal plan generation."""
        return await cls.post(
            f"/foodchat/sessions/{session_id}/messages",
            json=cls._message_payload(content),
            timeout=cls._long_timeout(),
        )

    @classmethod
    async def get_messages(cls, session_id: str, limit: Optional[int] = None):
        """Get message history for a session."""
        return await cls.get(
            f"/foodchat/sessions/{session_id}/messages",
            params=cls._optional_limit_params(limit),
        )

    @classmethod
    async def get_meal_plans(cls, session_id: str):
        """Get all daily meal plan versions in a session."""
        return await cls.get(f"/foodchat/sessions/{session_id}/meal-plans")

    @classmethod
    async def get_current_meal_plan(cls, session_id: str, member_id: str):
        """Get the latest daily meal plan for a session."""
        return await cls.get(
            f"/foodchat/sessions/{session_id}/meal-plans/current",
            params=cls._member_params(member_id),
        )

    @classmethod
    async def get_meal_plan_history(cls, session_id: str, member_id: str):
        """Get the daily meal plan version history for a session."""
        return await cls.get(
            f"/foodchat/sessions/{session_id}/meal-plans/history",
            params=cls._member_params(member_id),
        )

    @classmethod
    async def send_weekly_message(cls, session_id: str, content: str):
        """Send a message and get a weekly meal plan response."""
        return await cls.post(
            f"/foodchat/sessions/{session_id}/weekly",
            json=cls._message_payload(content),
            timeout=cls._extra_long_timeout(),
        )

    @classmethod
    async def get_weekly_messages(cls, session_id: str, limit: Optional[int] = None):
        """Get weekly message history for a session."""
        return await cls.get(
            f"/foodchat/sessions/{session_id}/weekly",
            params=cls._optional_limit_params(limit),
        )

    @classmethod
    async def get_weekly_meal_plans(cls, session_id: str):
        """Get all weekly meal plan versions in a session."""
        return await cls.get(f"/foodchat/sessions/{session_id}/weekly-meal-plans")

    @classmethod
    async def get_current_weekly_meal_plan(cls, session_id: str, member_id: str):
        """Get the latest weekly meal plan for a session."""
        return await cls.get(
            f"/foodchat/sessions/{session_id}/weekly-meal-plans/current",
            params=cls._member_params(member_id),
        )

    @classmethod
    async def get_weekly_meal_plan_history(cls, session_id: str, member_id: str):
        """Get the weekly meal plan version history for a session."""
        return await cls.get(
            f"/foodchat/sessions/{session_id}/weekly-meal-plans/history",
            params=cls._member_params(member_id),
        )

    @classmethod
    async def get_member_sessions(cls, member_id: str):
        """Get all sessions for a specific member."""
        return await cls.get(f"/foodchat/members/{member_id}/sessions")

    @classmethod
    async def chat(cls, session_id: str, member_id: str, content: str):
        """Send a message through the unified FoodChat endpoint."""
        return await cls.post(
            f"/foodchat/sessions/{session_id}/chat",
            json=cls._chat_payload(content, member_id),
            timeout=cls._extra_long_timeout(),
        )

    @classmethod
    async def get_conversation(
        cls,
        session_id: str,
        member_id: str,
        before_id: Optional[int] = None,
        limit: int = 20,
    ):
        """Get cursor-based conversation history for a session."""
        return await cls.get(
            f"/foodchat/sessions/{session_id}/conversation",
            params=cls._conversation_params(
                member_id=member_id,
                before_id=before_id,
                limit=limit,
            ),
        )

    @classmethod
    async def submit_feedback(
        cls,
        session_id: str,
        message_id: int,
        member_id: str,
        rating: str,
        comment: Optional[str] = None,
    ):
        """Submit feedback for an assistant message."""
        return await cls.post(
            f"/foodchat/sessions/{session_id}/messages/{message_id}/feedback",
            json=cls._feedback_payload(
                member_id=member_id,
                rating=rating,
                comment=comment,
            ),
        )


FOODCHAT = FoodChat.get_client()
