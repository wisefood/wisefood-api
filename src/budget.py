"""Per-guest usage budgets for expensive (mostly LLM-backed) endpoints.

Guests are anonymous and free, so the costly operations they can trigger
(chat turns, QA, LLM-backed search, session creation) are capped per guest
per day. Regular authenticated users are unaffected.

Usage — add alongside the auth dependency on a route:

    @router.post("/chat", dependencies=[Depends(auth()), Depends(guest_budget("chat"))])

Counters live in Redis (``guest:budget:<sub>:<category>:<day>``) with a
24h TTL so they clean themselves up. If Redis is unavailable the budget
fails open (logged) — availability over strict cost enforcement.

``deny_guests`` fully blocks a route for guests (expert workflows such as
guideline extraction).
"""
import logging
import os
import time
from typing import Any, Callable, Dict

from fastapi import Depends, Request

from auth import auth, _extract_roles
from backend.redis import REDIS
from exceptions import AuthorizationError, RateLimitError

logger = logging.getLogger(__name__)

_BUDGET_TTL = 24 * 3600

# Requests per guest per day, by category. Overridable via env through
# config (GUEST_BUDGET_<CATEGORY>); these are the fallbacks.
_DEFAULT_BUDGETS = {
    "chat": 30,        # conversational LLM turns (foodchat / foodscholar)
    "qa": 20,          # foodscholar Q&A
    "search": 60,      # LLM-backed recipe/literature search
    "sessions": 10,    # chat session creations
    "default": 40,
}


def _budget_limit(category: str) -> int:
    env_key = f"GUEST_BUDGET_{category.upper()}"
    raw = os.getenv(env_key)
    if raw is not None:
        try:
            return int(raw)
        except ValueError:
            logger.warning("Invalid %s=%r — using default", env_key, raw)
    return _DEFAULT_BUDGETS.get(category, _DEFAULT_BUDGETS["default"])


def _consume(sub: str, category: str) -> int:
    """Increment today's counter for this guest/category. Returns new count."""
    day = time.strftime("%Y%m%d", time.gmtime())
    key = f"guest:budget:{sub}:{category}:{day}"
    return REDIS.incr_with_ttl(key, _BUDGET_TTL)


def guest_budget(category: str) -> Callable[..., Any]:
    """Dependency enforcing a daily per-guest budget for one category."""

    async def dependency(payload: Dict[str, Any] = Depends(auth())) -> None:
        if "guest" not in _extract_roles(payload):
            return
        limit = _budget_limit(category)
        try:
            count = _consume(payload["sub"], category)
        except Exception:
            logger.warning(
                "Guest budget check unavailable (Redis?) — allowing request",
                exc_info=True,
            )
            return
        if count > limit:
            raise RateLimitError(
                detail=(
                    f"Guest budget exceeded for '{category}' "
                    f"({limit} per day). Create a free account to continue."
                )
            )

    return dependency


async def deny_guests(payload: Dict[str, Any] = Depends(auth())) -> None:
    """Dependency blocking guests entirely from a route."""
    if "guest" in _extract_roles(payload):
        raise AuthorizationError(
            detail="This feature is not available to guest accounts."
        )


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def ip_rate_limit(
    category: str, limit: int, window_seconds: int
) -> Callable[..., Any]:
    """Dependency throttling an (unauthenticated) route per client IP.

    Used on anonymous entry points such as guest creation, where there is
    no token yet to budget against. Fails open if Redis is unavailable.
    """

    async def dependency(request: Request) -> None:
        env_key = f"RATE_LIMIT_{category.upper()}"
        try:
            effective_limit = int(os.getenv(env_key, limit))
        except ValueError:
            effective_limit = limit
        key = f"ratelimit:{category}:{_client_ip(request)}"
        try:
            count = REDIS.incr_with_ttl(key, window_seconds)
        except Exception:
            logger.warning(
                "IP rate limit check unavailable (Redis?) — allowing request",
                exc_info=True,
            )
            return
        if count > effective_limit:
            raise RateLimitError(
                detail="Too many requests — please try again later."
            )

    return dependency
