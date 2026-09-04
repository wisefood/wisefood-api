"""Correlation id and per-request context.

The point of this middleware is that one user action can be followed across
five services. Two properties have to hold for that, and neither is obvious
from reading the code:

* a caller-supplied id is honoured but never trusted — it lands in log lines and
  in outbound headers, so a header-splitting attempt has to be replaced, not
  patched up;
* a value set by a *route* must still be visible when the middleware regains
  control. This is exactly what `BaseHTTPMiddleware` does not give you (it runs
  the app in a child task), and it is why this middleware is pure ASGI. The
  activity recorder in Phase 1 depends on it to learn which member a request
  acted on.
"""
import sys
import types

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture()
def ctx_app(monkeypatch):
    """A minimal app behind the real middleware, with auth stubbed out.

    `kutils`, `auth` and `backend.recipewrangler` all import `main`, which
    builds the whole service. The middleware only reaches for three names from
    them, so those are stubbed — the same approach the RecipeWrangler identity
    test takes.
    """
    sys.path.insert(0, "src")

    calls = {"introspect": 0}

    def fake_get_user_by_token(token):
        calls["introspect"] += 1
        if token == "bad":
            raise RuntimeError("token is not active")
        return {
            "sub": f"sub-of-{token}",
            "preferred_username": "tester",
            "realm_access": {"roles": ["expert"]},
        }

    kutils_stub = types.ModuleType("kutils")
    kutils_stub.get_user_by_token = fake_get_user_by_token

    auth_stub = types.ModuleType("auth")
    auth_stub._extract_roles = lambda payload: sorted(
        (payload.get("realm_access") or {}).get("roles") or []
    )

    from contextvars import ContextVar

    rw_stub = types.ModuleType("backend.recipewrangler")
    rw_stub.CURRENT_TOKEN_PAYLOAD = ContextVar("rw_stub", default=None)

    for name, module in (
        ("kutils", kutils_stub),
        ("auth", auth_stub),
        ("backend.recipewrangler", rw_stub),
    ):
        monkeypatch.setitem(sys.modules, name, module)

    import context
    from middleware import RequestContextMiddleware

    seen = {}
    app = FastAPI()

    @app.get("/probe")
    async def probe():
        seen["inside"] = context.snapshot()
        seen["outbound"] = context.outbound_headers()
        context.set_member_id("member-42")
        return {"ok": True}

    @app.get("/boom")
    async def boom():
        # How a failure actually reaches the client here: `render()` converts an
        # unexpected exception into an APIException, and the APIException
        # handler renders it *inside* this middleware. (A raw exception is
        # answered by Starlette's ServerErrorMiddleware, which sits outside
        # every user middleware and so cannot carry the header.)
        from exceptions import APIException

        raise APIException(
            status_code=500,
            detail="handler exploded",
            code="server/internal",
            extra={"title": "InternalError"},
        )

    # Wired the same way main.py wires it: the service's own exception handlers
    # sit inside the middleware, so even a failed request leaves with an id.
    # Without them Starlette's outermost handler answers, and the response never
    # passes back through this middleware.
    from routers.generic import install_error_handler

    install_error_handler(app)
    app.add_middleware(RequestContextMiddleware)

    return types.SimpleNamespace(
        app=app, seen=seen, calls=calls, context=context, rw=rw_stub
    )


class TestCorrelationId:
    def test_generated_when_absent_and_echoed(self, ctx_app):
        with TestClient(ctx_app.app) as client:
            response = client.get("/probe")
        assert response.status_code == 200
        echoed = response.headers["X-Request-Id"]
        assert echoed and len(echoed) == 32
        assert ctx_app.seen["inside"]["request_id"] == echoed

    def test_caller_supplied_id_is_honoured(self, ctx_app):
        with TestClient(ctx_app.app) as client:
            response = client.get("/probe", headers={"X-Request-Id": "ui-abc.123"})
        assert response.headers["X-Request-Id"] == "ui-abc.123"
        assert ctx_app.seen["inside"]["request_id"] == "ui-abc.123"

    @pytest.mark.parametrize(
        "hostile",
        [
            "has space",
            "x" * 65,
            "semi;colon",
            "quote\"mark",
        ],
    )
    def test_unsafe_id_is_replaced_not_sanitised(self, ctx_app, hostile):
        """A malformed id becomes a fresh one, never a half-honoured one."""
        with TestClient(ctx_app.app) as client:
            response = client.get("/probe", headers={"X-Request-Id": hostile})
        assigned = response.headers["X-Request-Id"]
        assert assigned != hostile
        assert len(assigned) == 32

    def test_id_survives_a_failed_request(self, ctx_app):
        """The error envelope is rendered inside this middleware, so it keeps the id."""
        with TestClient(ctx_app.app, raise_server_exceptions=False) as client:
            response = client.get("/boom", headers={"X-Request-Id": "keep-me"})
        assert response.status_code == 500
        assert response.json()["success"] is False
        assert response.headers["X-Request-Id"] == "keep-me"


class TestIdentity:
    def test_anonymous_request_never_introspects(self, ctx_app):
        with TestClient(ctx_app.app) as client:
            client.get("/probe")
        assert ctx_app.calls["introspect"] == 0
        assert ctx_app.seen["inside"]["user_id"] is None
        assert ctx_app.seen["inside"]["roles"] == []

    def test_bearer_token_populates_user_and_roles(self, ctx_app):
        with TestClient(ctx_app.app) as client:
            client.get("/probe", headers={"Authorization": "Bearer t1"})
        assert ctx_app.seen["inside"]["user_id"] == "sub-of-t1"
        assert ctx_app.seen["inside"]["roles"] == ["expert"]

    def test_repeated_token_is_introspected_once(self, ctx_app):
        """Introspection is a blocking network call; it used to run per request."""
        with TestClient(ctx_app.app) as client:
            for _ in range(3):
                client.get("/probe", headers={"Authorization": "Bearer t1"})
        assert ctx_app.calls["introspect"] == 1

    def test_failed_introspection_is_anonymous_not_an_error(self, ctx_app):
        """Authorization is the routes' job. This layer must never reject."""
        with TestClient(ctx_app.app) as client:
            response = client.get("/probe", headers={"Authorization": "Bearer bad"})
        assert response.status_code == 200
        assert ctx_app.seen["inside"]["user_id"] is None

    def test_client_label_is_captured(self, ctx_app):
        with TestClient(ctx_app.app) as client:
            client.get(
                "/probe",
                headers={
                    "X-Client": "wisefood-ui/1.4.0",
                    "X-Client-Session": "sess-9",
                },
            )
        assert ctx_app.seen["inside"]["client"] == "wisefood-ui/1.4.0"
        assert ctx_app.seen["inside"]["client_session_id"] == "sess-9"


class TestPropagation:
    def test_outbound_headers_carry_the_id(self, ctx_app):
        """What the FoodChat/FoodScholar/RecipeWrangler clients will forward."""
        with TestClient(ctx_app.app) as client:
            response = client.get("/probe", headers={"X-Request-Id": "join-me"})
        assert ctx_app.seen["outbound"] == {"X-Request-Id": "join-me"}
        assert response.headers["X-Request-Id"] == "join-me"

    def test_context_does_not_leak_between_requests(self, ctx_app):
        with TestClient(ctx_app.app) as client:
            client.get("/probe", headers={"Authorization": "Bearer t1"})
            client.get("/probe")
        assert ctx_app.seen["inside"]["user_id"] is None
        # `set_member_id` on the previous request must not survive into this one.
        assert ctx_app.seen["inside"]["member_id"] is None


class TestRouteIsKnownToHandlers:
    """Events recorded from inside a handler must carry the matched route.

    The middleware cannot supply it — it runs before routing — and an earlier
    version only set the route on the way out, after the handler had finished.
    Every event a handler recorded, the request itself included, therefore had
    no route and fell into the `platform` bucket. Every per-app report was wrong
    while looking perfectly healthy.

    Also covers the root path: the service runs behind `/rest`, so
    `request.url.path` carries that prefix on real traffic. An exclusion list
    matched against the URL passed in tests and failed in production.
    """

    @pytest.fixture()
    def routed_app(self, monkeypatch):
        sys.path.insert(0, "src")
        import context
        from fastapi import FastAPI
        from routers.generic import install_error_handler, render
        from middleware import RequestContextMiddleware

        for name, module in (
            ("kutils", types.SimpleNamespace(get_user_by_token=lambda t: None)),
            ("auth", types.SimpleNamespace(_extract_roles=lambda p: [])),
            ("backend.recipewrangler",
             types.SimpleNamespace(CURRENT_TOKEN_PAYLOAD=__import__("contextvars").ContextVar("x", default=None))),
        ):
            monkeypatch.setitem(sys.modules, name, module)

        recorded = []

        class FakeRecorder:
            enabled = True

            def record_event(self, event_type, **kwargs):
                recorded.append({"type": event_type, **kwargs, "ctx_route": context.get_route()})

        monkeypatch.setitem(
            sys.modules, "analytics", types.SimpleNamespace(RECORDER=FakeRecorder())
        )

        from fastapi import Request

        app = FastAPI(root_path="/rest")
        seen_in_handler = {}

        @app.get("/api/v1/members/{member_id}/profile")
        @render()
        async def profile(request: Request, member_id: str):
            # What a handler sees mid-request — this is what any event it
            # records (a search, a question) would be filed under.
            seen_in_handler["route"] = context.get_route()
            return {"member": member_id}

        @app.post("/api/v1/analytics/events")
        @render()
        async def ingest(request: Request):
            return {"accepted": 0}

        install_error_handler(app)
        app.add_middleware(RequestContextMiddleware)
        return types.SimpleNamespace(app=app, recorded=recorded, seen=seen_in_handler)

    def test_the_route_template_is_recorded_not_the_url(self, routed_app):
        with TestClient(routed_app.app) as client:
            client.get("/rest/api/v1/members/abc-123/profile")
        events = [e for e in routed_app.recorded if e["type"] == "http.request"]
        assert len(events) == 1
        # The pattern, so one route groups every member; never the id, never
        # the root path.
        assert events[0]["route"] == "/api/v1/members/{member_id}/profile"
        assert events[0]["ctx_route"] == "/api/v1/members/{member_id}/profile"

    def test_the_route_is_visible_while_the_handler_runs(self, routed_app):
        """Not just on the request event — anything a handler records."""
        with TestClient(routed_app.app) as client:
            client.get("/rest/api/v1/members/abc/profile")
        assert routed_app.seen.get("route") == "/api/v1/members/{member_id}/profile"

    def test_ingest_traffic_is_not_recorded_even_behind_the_root_path(self, routed_app):
        with TestClient(routed_app.app) as client:
            client.post("/rest/api/v1/analytics/events", json={})
        assert [e for e in routed_app.recorded if e["type"] == "http.request"] == []
