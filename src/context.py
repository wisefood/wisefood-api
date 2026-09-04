"""Per-request context: the correlation id, and who is asking.

Populated once per request by ``RequestContextMiddleware`` (``src/middleware.py``)
and read by three consumers:

* the logging filter in ``logsys``, so every line a request produces carries the
  same ``request_id``;
* the downstream HTTP clients in ``backend/``, so FoodChat, FoodScholar and
  RecipeWrangler log the *same* id for the call they were asked to make;
* from Phase 1, the activity recorder.

ContextVars rather than ``request.state`` because the backend clients are
classmethods with no access to the ``Request``. Threading a ``headers=``
argument through every proxied route is the pattern that breaks the moment
someone adds a route and forgets — the same reasoning that put the RecipeWrangler
token payload and the FoodChat member assertion in a ContextVar already.

This module imports nothing from the application (stdlib only) so that anything,
including ``main``, can import it without a cycle.
"""

from __future__ import annotations

import re
import uuid
from contextvars import ContextVar, Token
from typing import Any, Dict, List, Optional

# Inbound/outbound header names. The request id is echoed on every response and
# forwarded on every proxied call; the client headers are informational and are
# only ever read, never trusted for authorization.
REQUEST_ID_HEADER = "X-Request-Id"
CLIENT_HEADER = "X-Client"
CLIENT_SESSION_HEADER = "X-Client-Session"
#: The interface language the caller is using. The browser's Accept-Language is
#: what the *device* prefers, which is a different thing from what the user
#: switched the interface to, and this product is trilingual — so the UI states
#: its choice and Accept-Language is only the fallback.
LOCALE_HEADER = "X-Locale"

# A client-supplied correlation id is untrusted input that ends up in log lines
# and in outbound headers. Anything outside this alphabet (notably CR/LF) is
# rejected and replaced with a generated id rather than sanitised in place, so a
# malformed id can never become a half-honoured one.
_SAFE_ID = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")
_SAFE_LABEL = re.compile(r"^[A-Za-z0-9._:/+-]{1,64}$")

_REQUEST_ID: ContextVar[Optional[str]] = ContextVar("wf_request_id", default=None)
_USER_SUB: ContextVar[Optional[str]] = ContextVar("wf_user_sub", default=None)
_USER_ROLES: ContextVar[Optional[List[str]]] = ContextVar("wf_user_roles", default=None)
_MEMBER_ID: ContextVar[Optional[str]] = ContextVar("wf_member_id", default=None)
_HOUSEHOLD_ID: ContextVar[Optional[str]] = ContextVar("wf_household_id", default=None)
_CLIENT: ContextVar[Optional[str]] = ContextVar("wf_client", default=None)
_CLIENT_SESSION: ContextVar[Optional[str]] = ContextVar("wf_client_session", default=None)
_ROUTE: ContextVar[Optional[str]] = ContextVar("wf_route", default=None)
_LOCALE: ContextVar[Optional[str]] = ContextVar("wf_locale", default=None)

_ALL_VARS = (
    _REQUEST_ID,
    _USER_SUB,
    _USER_ROLES,
    _MEMBER_ID,
    _HOUSEHOLD_ID,
    _CLIENT,
    _CLIENT_SESSION,
    _ROUTE,
    _LOCALE,
)

#: A language tag, as narrow as it can be: two or three letters, optionally a
#: region. Anything else is dropped rather than truncated — a partial tag in a
#: GROUP BY is worse than no tag.
_SAFE_LOCALE = re.compile(r"^[A-Za-z]{2,3}(-[A-Za-z0-9]{2,8})?$")


def new_request_id() -> str:
    """A fresh correlation id for a request that arrived without one."""
    return uuid.uuid4().hex


def clean_locale(raw: Optional[str]) -> Optional[str]:
    """A language tag from a header, normalised, or None.

    Accepts the Accept-Language form as well as a bare tag, taking the first
    and highest-weighted entry: `hu-HU,hu;q=0.9,en;q=0.8` is a Hungarian
    interface, and the rest of that string is the browser explaining itself.
    """
    if not raw:
        return None
    first = str(raw).split(",", 1)[0].split(";", 1)[0].strip()
    if not first or not _SAFE_LOCALE.match(first):
        return None
    language, _, region = first.partition("-")
    return language.lower() + (f"-{region.upper()}" if region else "")


def clean_id(raw: Optional[str]) -> Optional[str]:
    """A caller-supplied id, or None if it is absent or not plainly safe."""
    if not raw:
        return None
    value = raw.strip()
    return value if _SAFE_ID.match(value) else None


def clean_label(raw: Optional[str]) -> Optional[str]:
    """A caller-supplied label such as ``wisefood-ui/1.4.0``, or None."""
    if not raw:
        return None
    value = raw.strip()
    return value if _SAFE_LABEL.match(value) else None


# ---------------------------------------------------------------- accessors --
def get_request_id() -> Optional[str]:
    return _REQUEST_ID.get()


def get_user_sub() -> Optional[str]:
    return _USER_SUB.get()


def get_user_roles() -> List[str]:
    return list(_USER_ROLES.get() or [])


def get_member_id() -> Optional[str]:
    return _MEMBER_ID.get()


def get_client() -> Optional[str]:
    return _CLIENT.get()


def get_client_session() -> Optional[str]:
    return _CLIENT_SESSION.get()


def get_route() -> Optional[str]:
    return _ROUTE.get()


def get_household_id() -> Optional[str]:
    return _HOUSEHOLD_ID.get()


def set_household_id(household_id: Optional[str]) -> None:
    """Record which household a request acts within. Set with the member,
    at the point ownership was checked — never from a request body."""
    _HOUSEHOLD_ID.set(str(household_id) if household_id else None)


def set_member_id(member_id: Optional[str]) -> None:
    """Record which household member a request is acting on.

    Called from route handlers once the member is known and authorized. The
    middleware runs in the same context (it is pure ASGI, not
    ``BaseHTTPMiddleware``), so a value set here is visible to the recorder when
    the response is on its way out.
    """
    _MEMBER_ID.set(str(member_id) if member_id else None)


def set_route(route: Optional[str]) -> None:
    _ROUTE.set(route or None)


def is_guest() -> bool:
    return "guest" in get_user_roles()


# ------------------------------------------------------------------- binding --
def bind(
    *,
    request_id: str,
    client: Optional[str] = None,
    client_session: Optional[str] = None,
    user_sub: Optional[str] = None,
    user_roles: Optional[List[str]] = None,
    route: Optional[str] = None,
    locale: Optional[str] = None,
) -> List[Token]:
    """Bind the context for one request. Pass the result to :func:`reset`."""
    return [
        _REQUEST_ID.set(request_id),
        _USER_SUB.set(user_sub),
        _USER_ROLES.set(list(user_roles) if user_roles else None),
        _MEMBER_ID.set(None),
        _HOUSEHOLD_ID.set(None),
        _CLIENT.set(client),
        _CLIENT_SESSION.set(client_session),
        _ROUTE.set(route),
        _LOCALE.set(locale),
    ]


def reset(tokens: Optional[List[Token]]) -> None:
    """Restore the context bound before :func:`bind`. Never raises."""
    if not tokens:
        return
    for var, token in zip(_ALL_VARS, tokens):
        try:
            var.reset(token)
        except (ValueError, LookupError):
            # A token created in a different context — nothing to restore.
            pass


# ------------------------------------------------------------------ consumers --
def outbound_headers() -> Dict[str, str]:
    """Headers every downstream call should carry, so its logs join ours."""
    request_id = _REQUEST_ID.get()
    return {REQUEST_ID_HEADER: request_id} if request_id else {}


def log_fields() -> Dict[str, Any]:
    """The context as log record attributes."""
    return {
        "request_id": _REQUEST_ID.get() or "-",
        "user_sub": _USER_SUB.get() or "-",
        "client": _CLIENT.get() or "-",
    }


def snapshot() -> Dict[str, Any]:
    """The full context, for an activity event."""
    return {
        "request_id": _REQUEST_ID.get(),
        "user_id": _USER_SUB.get(),
        "member_id": _MEMBER_ID.get(),
        "household_id": _HOUSEHOLD_ID.get(),
        "roles": get_user_roles(),
        "is_guest": is_guest(),
        "client": _CLIENT.get(),
        "client_session_id": _CLIENT_SESSION.get(),
        "route": _ROUTE.get(),
        "locale": _LOCALE.get(),
    }
