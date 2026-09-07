"""Request context middleware.

Assigns every request a correlation id, resolves who is asking, and makes both
available to logging, to the downstream HTTP clients, and (from Phase 1) to the
activity recorder.

Why pure ASGI rather than ``@app.middleware("http")``: ``BaseHTTPMiddleware``
runs the application in a child task, so ContextVars set by a *route* are not
visible again when the middleware regains control. The activity recorder has to
read the member id a route resolved, at the moment the response goes out, so the
middleware and the route must share one context. Pure ASGI gives us that, and
along the way avoids BaseHTTPMiddleware's long-standing quirks around streaming
responses — this service proxies an SSE endpoint.

This middleware never rejects a request. Authorization stays where it is, on
each route's ``Depends(auth(...))``; an absent or unreadable token here simply
makes the request anonymous, exactly as before.
"""

from __future__ import annotations

import hashlib
import logging
import time
from typing import Any, Dict, Optional, Tuple

from starlette.concurrency import run_in_threadpool
from starlette.datastructures import Headers, MutableHeaders

import context

logger = logging.getLogger(__name__)

#: Routes whose own traffic is not recorded: the ingest endpoints would produce
#: one activity row per batch of activity rows, and health checks would swamp
#: everything else. Matched against the ROUTE TEMPLATE, never the URL — the
#: service runs behind a `/rest` root path and `request.url.path` carries it.
_UNRECORDED_ROUTES = ("/api/v1/analytics/", "/api/v1/system/ping")
#: Paths that stay open during maintenance, matched against the URL path with
#: any root path stripped. The status endpoints so the browser can learn the
#: platform is closed; the settings endpoints so an admin can open it again —
#: a maintenance mode nobody can switch off is an outage with a nicer page.
_MAINTENANCE_OPEN = (
    "/api/v1/system/ping",
    "/api/v1/system/info",
    "/api/v1/analytics/settings",
    # Service-to-service, and neither is user access. `runtime-flags` is how
    # every service reads the tracing kill switch — refusing it makes them all
    # fall back to defaults for the length of the maintenance — and the signed
    # internal ingest is how they report what they did, which is simply lost
    # if it is turned away. Closing the platform to people is the point;
    # blinding the platform to itself is not.
    "/api/v1/analytics/runtime-flags",
    "/api/v1/analytics/internal/",
    # The browser's half of the same thing. Refusing it does not stop
    # recording, it stops the page ever learning that recording is *on* — so
    # capture silently disables itself and the console shows empty reports
    # for the whole maintenance, which reads as the feature being broken.
    "/api/v1/analytics/client-flags",
    "/docs",
    "/openapi.json",
)
#: How long a FAILED introspection is remembered. See `_token_payload`.
_FAILURE_TTL = 3.0


async def _send_json(send, status: int, body: dict, extra_headers=None) -> None:
    """One JSON response, straight from the middleware.

    Pure ASGI has no Response object to lean on; this is the whole of what one
    does. Kept minimal on purpose — it is the only thing this middleware ever
    sends itself, and it must not be able to fail.
    """
    import json

    payload = json.dumps(body).encode("utf-8")
    headers = [
        (b"content-type", b"application/json"),
        (b"content-length", str(len(payload)).encode("ascii")),
        (b"cache-control", b"no-store"),
    ]
    for name, value in (extra_headers or {}).items():
        headers.append((name.lower().encode("ascii"), str(value).encode("ascii")))
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": payload})


class RequestContextMiddleware:
    """Bind :mod:`context` for the lifetime of one HTTP request."""

    def __init__(self, app, *, introspect_ttl: float = 30.0, cache_max: int = 2048):
        self.app = app
        self._ttl = introspect_ttl
        self._cache_max = cache_max
        # token digest -> (monotonic timestamp, payload | None, ttl)
        self._cache: Dict[str, Tuple[float, Optional[Dict[str, Any]], float]] = {}

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)

        # An id supplied by the caller is honoured only if it is plainly safe;
        # anything else is replaced rather than sanitised, so a malformed id can
        # never become a half-honoured one in a log line.
        request_id = (
            context.clean_id(headers.get(context.REQUEST_ID_HEADER))
            or context.new_request_id()
        )
        payload = await self._token_payload(headers)

        tokens = context.bind(
            request_id=request_id,
            client=context.clean_label(headers.get(context.CLIENT_HEADER)),
            client_session=context.clean_id(
                headers.get(context.CLIENT_SESSION_HEADER)
            ),
            locale=context.clean_locale(
                headers.get(context.LOCALE_HEADER) or headers.get("accept-language")
            ),
            user_sub=self._sub(payload),
            user_roles=self._roles(payload),
        )
        # `request.state.request_id` is what routers/generic.py reads.
        scope.setdefault("state", {})["request_id"] = request_id

        # Maintenance: admins through, everyone else told plainly. Read from
        # the settings cache, never the database — this runs on every request.
        if self._closed_to(payload, scope):
            # The context is reset *after* the response, not before: it carries
            # the subject and roles, and resetting first logged every refusal
            # as anonymous. "Who is being turned away" is the only question
            # worth asking of a maintenance log, and it had no answer.
            try:
                await _send_json(
                    send,
                    503,
                    {
                        "success": False,
                        "error": {
                            "code": "platform/maintenance",
                            "title": "Maintenance",
                            "detail": (
                                "WiseFood is briefly closed for maintenance. "
                                "Please try again shortly."
                            ),
                        },
                    },
                    extra_headers={
                        "Retry-After": "300",
                        context.REQUEST_ID_HEADER: request_id,
                    },
                )
                logger.info(
                    "platform.maintenance_refused",
                    extra={"path": scope.get("path"), "sub": self._sub(payload)},
                )
            finally:
                context.reset(tokens)
            return

        # RecipeWrangler authenticates nobody; it trusts this service to say who
        # is calling. Populated here so a newly added proxy route forwards
        # identity by default instead of by someone remembering to.
        from backend.recipewrangler import CURRENT_TOKEN_PAYLOAD

        rw_token = CURRENT_TOKEN_PAYLOAD.set(payload)

        started = time.perf_counter()

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                MutableHeaders(scope=message)[context.REQUEST_ID_HEADER] = request_id
                # Routing has happened by now, so the route template is known.
                route = scope.get("route")
                path = getattr(route, "path", None)
                if path:
                    context.set_route(path)
                self._record_request(scope, path, message.get("status"), started)
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception as exc:
            # Starlette installs the catch-all `Exception` handler on
            # ServerErrorMiddleware, which sits *outside* every user middleware
            # — so the 500 it renders never passes back through `send_wrapper`
            # and cannot carry the id header. Almost nothing reaches here in
            # practice (`render()` turns an unexpected error into an
            # APIException, which is handled inside this middleware), but when
            # something does, the log line is the only place the id can appear.
            try:
                from analytics import RECORDER

                RECORDER.record_server_error(
                    exc=exc,
                    route=getattr(scope.get("route"), "path", None),
                    method=scope.get("method"),
                    status=500,
                    handled=False,
                )
            except Exception:
                pass
            logger.exception(
                "request.unhandled",
                extra={
                    "request_id": request_id,
                    "method": scope.get("method"),
                    "path": scope.get("path"),
                },
            )
            raise
        finally:
            CURRENT_TOKEN_PAYLOAD.reset(rw_token)
            context.reset(tokens)

    # ----------------------------------------------------------- maintenance --
    @classmethod
    def _closed_to(cls, payload, scope) -> bool:
        """Whether this request is refused because the platform is closed.

        Admins are never refused — they are the ones doing the maintenance.
        The check reads the in-process settings cache, so a flip reaches every
        replica within the cache TTL and costs no request a database round trip.
        """
        try:
            from analytics import SETTINGS

            if not SETTINGS.current().get("platform.maintenance_mode", False):
                return False
        except Exception:
            # If the switch cannot be read, the platform is open. A broken
            # settings cache must not be able to lock everyone out.
            return False
        path = scope.get("path") or ""
        root = scope.get("root_path") or ""
        if root and path.startswith(root):
            path = path[len(root):] or "/"
        if any(path.startswith(prefix) for prefix in _MAINTENANCE_OPEN):
            return False
        roles = cls._roles(payload) or []
        return "admin" not in roles

    # ------------------------------------------------------------- recording --
    @staticmethod
    def _record_request(scope, route: Optional[str], status, started: float) -> None:
        """Record one completed request as activity.

        Here and not in `render()`, because this is the only place that sees
        every response: a 401 from `Depends(auth())`, a 429 from the guest
        budget and a 422 from validation are all raised during dependency
        resolution, before any handler body runs, and a streaming response is
        returned before `render()`'s success path. An earlier version recorded
        from `render()` and so was blind to exactly the failures the comment
        next to it claimed to capture.

        Unrouted requests (a 404 for an unknown path, `/docs`) are skipped: they
        have no template to file under and are noise. Never raises.
        """
        try:
            if not route or status is None:
                return
            if any(route.startswith(prefix) for prefix in _UNRECORDED_ROUTES):
                return
            from analytics import RECORDER

            RECORDER.record_event(
                "http.request",
                route=route,
                method=scope.get("method"),
                status=int(status),
                duration_ms=(time.perf_counter() - started) * 1000.0,
                capability="http_requests",
                sampled=True,
            )
        except Exception:
            logger.debug("analytics.request_record_failed", exc_info=True)

    # ------------------------------------------------------------------ auth --
    @staticmethod
    def _sub(payload: Optional[Dict[str, Any]]) -> Optional[str]:
        if not payload:
            return None
        sub = str(payload.get("sub") or "").strip()
        return sub or None

    @staticmethod
    def _roles(payload: Optional[Dict[str, Any]]):
        if not payload:
            return None
        from auth import _extract_roles

        try:
            return _extract_roles(payload)
        except Exception:
            return None

    async def _token_payload(self, headers: Headers) -> Optional[Dict[str, Any]]:
        """The introspected token payload, or None for an anonymous request.

        Introspection is a blocking network call, so it runs off the event loop
        and its result is cached briefly. Both matter: before this it ran inline
        on every authenticated request, stalling the loop for a Keycloak
        round-trip. The TTL matches the one `auth._introspect_active` already
        accepts for the same trade-off. Failures are cached too, so an invalid
        token cannot be used to hammer Keycloak.
        """
        authorization = headers.get("authorization") or ""
        if not authorization.lower().startswith("bearer "):
            return None
        token = authorization.split(" ", 1)[1].strip()
        if not token:
            return None

        key = hashlib.sha256(token.encode()).hexdigest()
        now = time.monotonic()
        hit = self._cache.get(key)
        if hit is not None and (now - hit[0]) < hit[2]:
            return hit[1]

        import kutils

        try:
            payload = await run_in_threadpool(kutils.get_user_by_token, token)
        except Exception:
            payload = None
        if not isinstance(payload, dict):
            payload = None

        # A failure is remembered only briefly. Long enough that an invalid
        # token cannot be used to hammer Keycloak, short enough that a blip
        # does not leave a signed-in user anonymous — and RecipeWrangler hiding
        # their own recipes from them — for the full TTL.
        self._remember(key, now, payload, ttl=self._ttl if payload else _FAILURE_TTL)
        return payload

    def _remember(
        self, key: str, now: float, payload: Optional[Dict[str, Any]], ttl: float
    ) -> None:
        if len(self._cache) >= self._cache_max:
            expired = [
                k for k, (ts, _, entry_ttl) in self._cache.items() if (now - ts) >= entry_ttl
            ]
            for k in expired:
                self._cache.pop(k, None)
            if len(self._cache) >= self._cache_max:
                # Guest tokens churn; a full cache of live entries is better
                # dropped wholesale than grown without bound.
                self._cache.clear()
        self._cache[key] = (now, payload, ttl)
