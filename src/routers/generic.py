from __future__ import annotations

import functools
import inspect
import time
from logging import Logger, getLogger
from typing import Any, Awaitable, Callable, Optional, Union, TypeVar, Dict
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from exceptions import APIException, DataError 
from starlette.responses import Response
from starlette.concurrency import run_in_threadpool
from pydantic import BaseModel

from exceptions import APIException

import context

log = getLogger(__name__)

# Resolved lazily and cached: `main` imports this module, so it cannot be
# imported at module scope here. The setting is read once at startup and never
# changes, and a service running without `main` (a unit test importing a router
# in isolation) simply gets the default.
_REQUEST_LOG: Optional[bool] = None


def _request_log_enabled() -> bool:
    global _REQUEST_LOG
    if _REQUEST_LOG is None:
        try:
            from main import config

            _REQUEST_LOG = bool(config.settings.get("REQUEST_LOG_ENABLED", False))
        except Exception:
            _REQUEST_LOG = False
    return _REQUEST_LOG


def _route_template(req: Request) -> Optional[str]:
    """The matched route's pattern, e.g. `/api/v1/members/{member_id}/profile`.

    Starlette has finished routing by the time a handler runs, so
    `scope["route"]` is set here even though it is not yet set when the
    middleware first sees the request. The pattern is what analytics wants: it
    groups every member's profile fetch under one route instead of one per id,
    and it never contains a root path.
    """
    route = req.scope.get("route")
    path = getattr(route, "path", None)
    if isinstance(path, str) and path:
        return path
    return None


def _bind_route(req: Request) -> None:
    """Make the route visible to everything recorded during this handler.

    The middleware cannot do this: it runs before routing. Without it every
    event recorded from a handler — a search, a question — carried no route and
    fell into the `platform` bucket, which broke every per-app report while
    looking perfectly healthy. The request itself is recorded by the
    middleware, which sees every response including the ones dependencies
    reject before a handler runs.
    """
    try:
        template = _route_template(req)
        if template:
            context.set_route(template)
    except Exception:
        pass

# ---------- Success envelope ----------
class APIEnvelope(BaseModel):
    help: str
    success: bool = True
    result: Any

def _ok(result: Any, request: Request) -> APIEnvelope:
    return APIEnvelope(help=str(request.url), result=result)

# ---------- Helpers ----------
T = TypeVar("T")
EndpointFn = Union[Callable[..., T], Callable[..., Awaitable[T]]]
ResultMapper = Callable[[Any], Any]

REDACT_KEYS = {
    "password",
    "pwd",
    "token",
    "access_token",
    "authorization",
    "secret",
    "apikey",
    "api_key",
}

def _redact(d: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(d, dict):
        return d  # best effort
    out = {}
    for k, v in d.items():
        if k.lower() in REDACT_KEYS:
            out[k] = "***"
        else:
            out[k] = v
    return out

def _pick_request(args, kwargs, fn) -> Optional[Request]:
    req = kwargs.get("request")
    if isinstance(req, Request):
        return req
    sig = inspect.signature(fn)
    bound = sig.bind_partial(*args, **kwargs)
    for name, param in sig.parameters.items():
        val = bound.arguments.get(name)
        if isinstance(val, Request):
            return val
    return None

# ---------- Decorator ----------
def render(
    map_result: Optional[ResultMapper] = None,
    *,
    logger: Optional[Logger] = None,
    event: Optional[str] = None,
) -> Callable[[EndpointFn], EndpointFn]:
    """
    Wrap an endpoint to:
      - pass through Response objects
      - re-raise APIException for global handlers
      - wrap unknown errors via APIException.from_unexpected
      - envelope successful results uniformly
      - log only on exceptions
    """
    logger = logger or log

    def decorator(func: EndpointFn) -> EndpointFn:
        is_coro = inspect.iscoroutinefunction(func)

        @functools.wraps(func)
        async def async_wrapper(*args, **kwargs):
            req = _pick_request(args, kwargs, func)
            if req is None:
                raise RuntimeError(
                    "render(): endpoint must accept a 'request: Request' parameter "
                    "to build the success envelope."
                )

            started = time.perf_counter()
            ev = event or func.__name__
            _bind_route(req)
            # The middleware is the authority on the correlation id; the
            # request state is the fallback for a request that somehow bypassed
            # it (a test calling a handler directly, say).
            rid = context.get_request_id() or getattr(
                getattr(req, "state", None), "request_id", None
            )

            try:
                if is_coro:
                    result = await func(*args, **kwargs)
                else:
                    result = await run_in_threadpool(func, *args, **kwargs)

                if isinstance(result, Response):
                    return result
                if map_result:
                    result = map_result(result)

                duration = (time.perf_counter() - started) * 1000

                if _request_log_enabled():
                    logger.info(
                        f"api.request:{ev}",
                        extra={
                            "method": req.method,
                            "path": req.url.path,
                            "status": 200,
                            "duration_ms": round(duration, 2),
                            "request_id": rid,
                            "member_id": context.get_member_id(),
                        },
                    )

                return _ok(result, req)

            except APIException as exc:
                # Log only on exception
                level = 30 if exc.status_code < 500 else 40  # WARNING for 4xx, ERROR for 5xx
                dur = (time.perf_counter() - started) * 1000
                logger.log(
                    level,
                    f"api.api_exception:{ev}",
                    extra={
                        "method": req.method,
                        "path": req.url.path,
                        "status": exc.status_code,
                        "code": getattr(exc, "code", None),
                        "detail": exc.detail,
                        "duration_ms": round(dur, 2),
                        "request_id": rid,
                    },
                    exc_info=exc.status_code >= 500,
                )
                # A 5xx APIException is a genuine server fault that happens
                # to have been given a shape; a 4xx is the caller's problem and
                # is already counted as a request status.
                if exc.status_code >= 500:
                    _record_server_error(exc, req, exc.status_code, handled=True)
                raise  # handled by global APIException handler

            except Exception as exc:
                dur = (time.perf_counter() - started) * 1000
                logger.exception(
                    f"api.unexpected:{ev}",
                    extra={
                        "method": req.method,
                        "path": req.url.path,
                        "duration_ms": round(dur, 2),
                        "request_id": rid,
                    },
                )
                # This is where every unexpected server error ends up, so it
                # is the one hook that makes backend faults visible in the
                # console instead of only in a pod log that rotates.
                _record_server_error(exc, req, 500, handled=False)
                raise APIException.from_unexpected(exc) from exc

        return async_wrapper  # Always return the async wrapper

    return decorator


def _record_server_error(exc, req, status: int, *, handled: bool) -> None:
    """File an exception as an error occurrence. Never raises.

    Imported inside the function because `analytics` imports a good deal of the
    application, and this module is imported by nearly every router.
    """
    try:
        from analytics import RECORDER

        route = getattr(getattr(req, "scope", {}), "get", lambda _k: None)("route")
        RECORDER.record_server_error(
            exc=exc,
            route=getattr(route, "path", None) or context.get_route(),
            method=getattr(req, "method", None),
            status=status,
            handled=handled,
        )
    except Exception:
        # An analytics failure inside an exception handler would replace a
        # useful error with a useless one.
        pass


def install_error_handler(app: FastAPI) -> None:
    """
    Installs exception handlers that render a minimal, uniform error shape:

    {
      "success": false,
      "error": {
         "detail": "...",
         "title": "...",
         "code": "..."
      },
      "help": "<url>"
    }
    """
    @app.exception_handler(APIException)
    async def handle_api_exception(request: Request, exc: APIException):
        return _to_simple_response(request, exc)

    @app.exception_handler(RequestValidationError)
    async def handle_validation(request: Request, exc: RequestValidationError):
        data_error = DataError(
            detail="Validation failed",
            errors=exc.errors(),
            extra={"title": "RequestValidationError"},
        )
        return _to_simple_response(request, data_error)

    @app.exception_handler(Exception)
    async def handle_unexpected(request: Request, exc: Exception):
        from exceptions import APIException as _APIException
        internal = _APIException.from_unexpected(exc)
        return _to_simple_response(request, internal)

def _to_simple_response(request: Request, exc: APIException):
    """
    Convert any APIException into the minimal shape.
    """
    title = exc.extra.get("title", exc.__class__.__name__)
    code = getattr(exc, "code", None)
    detail = exc.detail

    body = {
        "success": False,
        "error": {
            "title": title,
            "detail": detail,
            "code": code,
        },
        "help": str(request.url),
    }
    from fastapi.responses import JSONResponse
    return JSONResponse(
        body,
        status_code=exc.status_code,
        headers=exc.headers,
    )