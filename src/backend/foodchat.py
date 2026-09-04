import hashlib
import hmac
import logging
import os
import re
import time
from typing import Any, Dict, List, Optional
from urllib.parse import quote, unquote

import httpx

import context

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
        # For the reads: session lists, conversation pages, planning state.
        # These do no model work, so 15s is generous — and keeping it low means
        # a wedged FoodChat surfaces quickly on the cheap routes instead of
        # tying up gateway connections. Generating calls pass
        # `_extra_long_timeout()` explicitly.
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

    # ------------------------------------------------------------------ #
    # Member assertion                                                    #
    # ------------------------------------------------------------------ #
    # FoodChat is internally unauthenticated: it takes `member_id` as data and
    # believes it. This gateway is the only party that can answer "does this
    # Keycloak user own this member" — the household tables are here — so it
    # signs that answer and FoodChat verifies the signature.
    #
    # `X-WiseFood-Member: <member_id>.<expires_at>.<hmac-sha256>`
    #
    # Minted in `request()` rather than in each of the twenty-odd methods
    # below, because a header added in twenty places is a header missing from
    # one of them, and the one that is missing it is the route that stays open.

    ASSERTION_HEADER = "X-WiseFood-Member"
    ASSERTION_TTL_SECONDS = 300

    @classmethod
    def _assertion_secret(cls) -> Optional[str]:
        value = (os.getenv("FOODCHAT_ASSERTION_SECRET") or "").strip()
        return value or None

    # `/foodchat/members/{member_id}/...` — the third place a member can ride.
    # Caught by pattern rather than by an argument each method has to remember
    # to pass, for the same reason the header is minted in one place.
    _MEMBER_PATH = re.compile(r"^/foodchat/members/([^/]+)/")

    @classmethod
    def _member_from(cls, endpoint: str, kwargs: Dict[str, Any]) -> Optional[str]:
        """The member this request is about, wherever it happens to ride.

        Three places, all of them real: the JSON body, the query string, and
        the path. The header follows the payload rather than being passed
        separately, so the two can never disagree about who is acting.
        """
        body = kwargs.get("json")
        if isinstance(body, dict) and body.get("member_id"):
            return str(body["member_id"])
        params = kwargs.get("params")
        if isinstance(params, dict) and params.get("member_id"):
            return str(params["member_id"])
        match = cls._MEMBER_PATH.match(endpoint or "")
        if match:
            return unquote(match.group(1))
        return None

    @classmethod
    def _sign_member(cls, member_id: str, secret: str) -> str:
        expires = int(time.time()) + cls.ASSERTION_TTL_SECONDS
        # The separator is inside the signed payload, so a member id containing
        # a dot cannot be shifted into the expiry field and re-signed.
        payload = f"{member_id}|{expires}".encode()
        digest = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
        return f"{member_id}.{expires}.{digest}"

    @classmethod
    async def request(cls, method: str, endpoint: str, **kwargs):
        client = cls._require_client()

        secret = cls._assertion_secret()
        member_id = cls._member_from(endpoint, kwargs)
        # The correlation id rides on every call, signed assertion or not, so
        # FoodChat's lines for this turn carry the same id as the gateway's.
        headers = {**context.outbound_headers(), **(kwargs.pop("headers", None) or {})}
        if secret and member_id:
            headers[cls.ASSERTION_HEADER] = cls._sign_member(member_id, secret)
        if headers:
            kwargs["headers"] = headers

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
    async def get_member_feedback(cls, member_id: str, params: Dict[str, Any]):
        """One member's chat ratings.

        Member-scoped on purpose: FoodChat authenticates nobody, so an unscoped
        listing there would hand every member's comments to anything that can
        reach the port. The cross-member view experts need is the gateway's own
        feedback inbox, which carries every surface's feedback together.
        """
        return await cls.get(
            f"/foodchat/members/{quote(str(member_id), safe='')}/feedback",
            params=params,
        )

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
    async def patch(cls, endpoint: str, data: Any = None, json: Any = None, **kwargs):
        return await cls.request("PATCH", endpoint, data=data, json=json, **kwargs)

    @classmethod
    async def delete(cls, endpoint: str, **kwargs):
        return await cls.request("DELETE", endpoint, **kwargs)

    @classmethod
    def _member_params(cls, member_id: str) -> Dict[str, Any]:
        return {"member_id": member_id}

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

    # The timeout ladder, outermost to innermost:
    #
    #   UI              180s   generous; it is a person watching a spinner
    #   gateway          90s   this, for anything that generates a plan
    #   FoodChat turn    70s   its own budget — it sheds work and answers
    #   Groq call        45s   one model call, bounded, one retry
    #
    # Each layer must be strictly larger than the one inside it. When that
    # inverted — FoodChat had NO budget and Groq had no timeout — a slow turn
    # produced the worst available outcome: this gateway cut the connection at
    # 90 seconds, the member was told the plan failed, and FoodChat carried on,
    # finished it and stored it. The plan existed; they found it on reload.
    #
    # 90 stays where it is precisely so FoodChat's 70-second budget is the
    # thing that fires first, and the member gets a real (if plainer) answer
    # instead of a severed request.
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
    async def create_session(
        cls,
        member_id: str,
        cooking_for: Optional[List[str]] = None,
    ):
        """Create a new chat session for a household member."""
        payload: Dict[str, Any] = {"member_id": member_id}
        if cooking_for is not None:
            payload["cooking_for"] = cooking_for
        return await cls.post("/foodchat/sessions", json=payload)

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
    async def rename_session(cls, session_id: str, member_id: str, title: str):
        """Give a session a member-facing name."""
        return await cls.patch(
            f"/foodchat/sessions/{session_id}",
            json={"member_id": member_id, "title": title},
        )

    @classmethod
    async def save_meal_plan(
        cls,
        session_id: str,
        plan_id: str,
        member_id: str,
        saved: bool = True,
        title: Optional[str] = None,
    ):
        """Save (or unsave) a plan so it outlives its conversation."""
        payload: Dict[str, Any] = {"member_id": member_id, "saved": saved}
        if title is not None:
            payload["title"] = title
        return await cls.post(
            f"/foodchat/sessions/{session_id}/meal-plans/{plan_id}/save",
            json=payload,
        )

    @classmethod
    async def get_member_saved_plans(cls, member_id: str):
        """Every plan the member saved, across all their sessions."""
        return await cls.get(f"/foodchat/members/{member_id}/saved-plans")

    @classmethod
    async def get_meal_plans(cls, session_id: str, member_id: str):
        """Get all daily meal plan versions in a session."""
        return await cls.get(
            f"/foodchat/sessions/{session_id}/meal-plans",
            params=cls._member_params(member_id),
        )

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
    async def get_weekly_meal_plans(cls, session_id: str, member_id: str):
        """Get all weekly meal plan versions in a session."""
        return await cls.get(
            f"/foodchat/sessions/{session_id}/weekly-meal-plans",
            params=cls._member_params(member_id),
        )

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
    async def get_member_current_plans(cls, member_id: str):
        """Latest saved plan canvases for a member (dashboard widget)."""
        return await cls.get(f"/foodchat/members/{member_id}/current-plans")

    @classmethod
    async def chat(cls, session_id: str, member_id: str, content: str):
        """Send a message through the unified FoodChat endpoint."""
        return await cls.post(
            f"/foodchat/sessions/{session_id}/chat",
            json=cls._chat_payload(content, member_id),
            timeout=cls._extra_long_timeout(),
        )

    @classmethod
    async def compose_plan(
        cls,
        session_id: str,
        member_id: str,
        picks: List[Dict[str, Any]],
        plan_type: str = "daily",
        message: Optional[str] = None,
    ):
        """Complete a hand-started plan — generates like a chat turn."""
        return await cls.post(
            f"/foodchat/sessions/{session_id}/compose",
            json={
                "member_id": member_id, "picks": picks,
                "plan_type": plan_type, "message": message,
            },
            timeout=cls._extra_long_timeout(),
        )

    @classmethod
    async def apply_plan_parameters(
        cls,
        session_id: str,
        member_id: str,
        values: Dict[str, Any],
        plan_type: Optional[str] = None,
    ):
        """Apply plan-parameter card values — generates like a chat turn."""
        return await cls.post(
            f"/foodchat/sessions/{session_id}/plan-parameters",
            json={
                "member_id": member_id, "values": values,
                "plan_type": plan_type,
            },
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

    @classmethod
    async def submit_memory_decision(
        cls,
        session_id: str,
        member_id: str,
        decision: str,
        suggestion: Dict[str, Any],
    ):
        """Accept or decline a memory suggestion for a session."""
        return await cls.post(
            f"/foodchat/sessions/{session_id}/memory",
            json={
                "member_id": member_id,
                "decision": decision,
                "suggestion": suggestion,
            },
        )

    @classmethod
    async def update_diners(
        cls,
        session_id: str,
        member_id: str,
        cooking_for: List[str],
    ):
        """Update the diners (cooking_for) of a session."""
        return await cls.put(
            f"/foodchat/sessions/{session_id}/diners",
            json={
                "member_id": member_id,
                "cooking_for": cooking_for,
            },
        )

    # ---------------------------------------------------------------- #
    # Standing planning state — the pantry panel and the facet chips    #
    # ---------------------------------------------------------------- #

    @classmethod
    async def get_planning_state(cls, session_id: str, member_id: str):
        """What is standing for this session: pantry, facets, stated diet."""
        return await cls.get(
            f"/foodchat/sessions/{session_id}/planning-state",
            params=cls._member_params(member_id),
        )

    @classmethod
    async def set_pantry(cls, session_id: str, member_id: str, items: List[str]):
        """Replace the pantry with exactly these items (the panel's save)."""
        return await cls.put(
            f"/foodchat/sessions/{session_id}/pantry",
            json={"member_id": member_id, "items": items},
        )

    @classmethod
    async def add_pantry_items(cls, session_id: str, member_id: str, items: List[str]):
        """Add items, leaving the rest of the pantry alone."""
        return await cls.post(
            f"/foodchat/sessions/{session_id}/pantry",
            json={"member_id": member_id, "items": items},
        )

    @classmethod
    async def remove_pantry_item(cls, session_id: str, member_id: str, item: str):
        """Tick one item off."""
        return await cls.delete(
            f"/foodchat/sessions/{session_id}/pantry/{quote(item, safe='')}",
            params=cls._member_params(member_id),
        )

    @classmethod
    async def add_facets(cls, session_id: str, member_id: str, values: List[str]):
        """Ask for a taste the assistant did not infer."""
        return await cls.post(
            f"/foodchat/sessions/{session_id}/facets",
            json={"member_id": member_id, "values": values},
        )

    @classmethod
    async def remove_facet(cls, session_id: str, member_id: str, value: str):
        """Take back one inferred facet — the removable chip on the plan."""
        return await cls.delete(
            f"/foodchat/sessions/{session_id}/facets/{quote(value, safe='')}",
            params=cls._member_params(member_id),
        )

    @classmethod
    async def replan(cls, session_id: str, member_id: str, plan_type: Optional[str] = None):
        """Re-plan from the standing state. Generates a plan, so it gets the
        same extended timeout as the other planning calls."""
        payload: Dict[str, Any] = {"member_id": member_id}
        if plan_type is not None:
            payload["plan_type"] = plan_type
        return await cls.post(
            f"/foodchat/sessions/{session_id}/replan",
            json=payload,
            timeout=cls._extra_long_timeout(),
        )

    @classmethod
    async def get_vocabularies(cls):
        """The live facet vocabulary the recipe corpus actually carries."""
        return await cls.get("/foodchat/vocabularies")

    # ---------------------------------------------------------------- #
    # Tool surface                                                      #
    # ---------------------------------------------------------------- #

    @classmethod
    async def list_tools(cls):
        """Every tool the agent can call, with its schema."""
        return await cls.get("/foodchat/tools")

    @classmethod
    async def invoke_tool(
        cls, tool_name: str, member_id: str, arguments: Dict[str, Any],
    ):
        """Run one tool by name. Some regenerate part of a plan, so this gets
        the extended timeout rather than the default."""
        return await cls.post(
            f"/foodchat/tools/{tool_name}",
            json={"member_id": member_id, "arguments": arguments},
            timeout=cls._extra_long_timeout(),
        )


FOODCHAT = FoodChat.get_client()
