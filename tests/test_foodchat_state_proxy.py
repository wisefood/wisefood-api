"""
The FoodChat proxy routes for standing planning state and the tool surface.

Both were unreachable from the browser. FoodChat holds the pantry and the
inferred facets in session state, and the gateway is the only route the UI has —
so a member could say "I have zucchini", have it heard, stored and used, and
have no way to see it or correct a misheard word. The tool surface was worse:
five typed capabilities (summarise the week, replace one day, total a plan) with
no proxy at all, so four of the five had no possible caller.

The tests assert the contract the UI calls — path, verb, payload — and the two
things that carry real risk: that a member-typed pantry item cannot escape its
path segment, and that every one of these routes authorizes before forwarding.
"""
import inspect

import pytest
from pydantic import ValidationError


FOODCHAT_PREFIX = "/api/v1/foodchat"


def _routes():
    import sys

    sys.path.insert(0, "src")
    import main

    return {
        (method, route.path)
        for route in main.api.routes
        if getattr(route, "path", "").startswith(FOODCHAT_PREFIX)
        for method in getattr(route, "methods", set())
    }


# ── the routes ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("verb,path", [
    ("GET", f"{FOODCHAT_PREFIX}/sessions/{{session_id}}/planning-state"),
    ("PUT", f"{FOODCHAT_PREFIX}/sessions/{{session_id}}/pantry"),
    ("POST", f"{FOODCHAT_PREFIX}/sessions/{{session_id}}/pantry"),
    ("DELETE", f"{FOODCHAT_PREFIX}/sessions/{{session_id}}/pantry/{{item}}"),
    ("POST", f"{FOODCHAT_PREFIX}/sessions/{{session_id}}/facets"),
    ("DELETE", f"{FOODCHAT_PREFIX}/sessions/{{session_id}}/facets/{{value}}"),
    ("POST", f"{FOODCHAT_PREFIX}/sessions/{{session_id}}/replan"),
    ("GET", f"{FOODCHAT_PREFIX}/vocabularies"),
    ("GET", f"{FOODCHAT_PREFIX}/tools"),
    ("POST", f"{FOODCHAT_PREFIX}/tools/{{tool_name}}"),
])
def test_the_route_is_proxied(verb, path):
    assert (verb, path) in _routes(), f"{verb} {path} is not proxied"


def test_the_pantry_route_carries_both_write_verbs():
    """PUT replaces the whole list, POST adds to it. Collapsing them would make
    "I cleared the last item" inexpressible."""
    routes = _routes()
    pantry = f"{FOODCHAT_PREFIX}/sessions/{{session_id}}/pantry"
    assert ("PUT", pantry) in routes and ("POST", pantry) in routes


def test_the_new_routes_did_not_displace_the_existing_session_verbs():
    routes = _routes()
    session = f"{FOODCHAT_PREFIX}/sessions/{{session_id}}"
    for verb in ("GET", "DELETE", "PATCH"):
        assert (verb, session) in routes, f"{verb} lost on the session route"


# ── every one of them authorizes before forwarding ────────────────────────

@pytest.mark.parametrize("handler", [
    "get_planning_state", "set_pantry", "add_pantry_items",
    "remove_pantry_item", "remove_facet", "replan", "invoke_tool",
])
def test_a_session_scoped_route_verifies_member_access(handler):
    """The gateway is the layer that knows WHO the caller is — FoodChat only
    knows which member_id it was handed. A route that forwards without this
    check lets any authenticated user act as any member."""
    from routers import foodchat as router_module

    src = inspect.getsource(getattr(router_module, handler))
    assert "verify_member_access" in src, handler


def test_the_check_runs_before_the_forward():
    """Authorizing after the call would still have done the thing."""
    from routers import foodchat as router_module

    for handler in ("set_pantry", "remove_facet", "invoke_tool"):
        src = inspect.getsource(getattr(router_module, handler))
        assert src.find("verify_member_access") < src.find("FOODCHAT."), handler


@pytest.mark.parametrize("handler", ["get_planning_state", "invoke_tool", "list_tools"])
def test_every_new_route_requires_authentication(handler):
    from routers import foodchat as router_module

    fn = getattr(router_module, handler)
    # The decorator stack is opaque by the time it is a function, so assert on
    # the registered route instead.
    import sys

    sys.path.insert(0, "src")
    import main

    matching = [
        r for r in main.api.routes
        if getattr(r, "endpoint", None) is not None
        and getattr(r.endpoint, "__name__", "") == fn.__name__
    ]
    assert matching, handler
    assert matching[0].dependencies, f"{handler} has no auth dependency"


def test_the_vocabulary_route_is_not_session_scoped():
    """It returns the corpus vocabulary, which is the same for everyone — so it
    takes no member and must not pretend to check one."""
    from routers import foodchat as router_module

    src = inspect.getsource(router_module.get_vocabularies)
    assert "member_id" not in src


# ── request validation ────────────────────────────────────────────────────

def test_an_empty_pantry_list_is_valid():
    """It is how the panel says "the pantry is empty now"."""
    from schemas import FoodChatPantryRequest

    assert FoodChatPantryRequest(member_id="m1").items == []
    assert FoodChatPantryRequest(member_id="m1", items=[]).items == []


def test_the_pantry_is_bounded():
    from schemas import FoodChatPantryRequest

    with pytest.raises(ValidationError):
        FoodChatPantryRequest(member_id="m1", items=["x"] * 51)


def test_pantry_requires_a_member():
    from schemas import FoodChatPantryRequest

    with pytest.raises(ValidationError):
        FoodChatPantryRequest(items=["zucchini"])


def test_replan_defaults_to_the_active_canvas():
    from schemas import FoodChatReplanRequest

    assert FoodChatReplanRequest(member_id="m1").plan_type is None


def test_replan_constrains_the_plan_type():
    from schemas import FoodChatReplanRequest

    assert FoodChatReplanRequest(member_id="m1", plan_type="weekly").plan_type == "weekly"
    with pytest.raises(ValidationError):
        FoodChatReplanRequest(member_id="m1", plan_type="monthly")


def test_a_tool_invocation_defaults_to_no_arguments():
    from schemas import FoodChatToolInvokeRequest

    assert FoodChatToolInvokeRequest(member_id="m1").arguments == {}


# ── the backend client ────────────────────────────────────────────────────

@pytest.mark.parametrize("method", [
    "get_planning_state", "set_pantry", "add_pantry_items",
    "remove_pantry_item", "remove_facet", "replan", "get_vocabularies",
    "list_tools", "invoke_tool",
])
def test_the_client_exposes_the_method(method):
    from backend.foodchat import FOODCHAT

    assert hasattr(FOODCHAT, method)


def test_a_pantry_item_with_a_space_stays_one_path_segment():
    """A member types "ground beef". Unencoded, that is a broken URL; a member
    typing "a/b" would change the route entirely."""
    import asyncio

    from backend.foodchat import FOODCHAT

    captured = {}

    async def fake_delete(endpoint, **kwargs):
        captured["endpoint"] = endpoint

    original = FOODCHAT.delete
    try:
        FOODCHAT.delete = fake_delete
        asyncio.run(FOODCHAT.remove_pantry_item(
            session_id="s1", member_id="m1", item="ground beef",
        ))
        assert captured["endpoint"] == "/foodchat/sessions/s1/pantry/ground%20beef"

        asyncio.run(FOODCHAT.remove_pantry_item(
            session_id="s1", member_id="m1", item="a/b",
        ))
        assert captured["endpoint"] == "/foodchat/sessions/s1/pantry/a%2Fb"
    finally:
        FOODCHAT.delete = original


def test_a_facet_value_is_encoded_the_same_way():
    import asyncio

    from backend.foodchat import FOODCHAT

    captured = {}

    async def fake_delete(endpoint, **kwargs):
        captured["endpoint"] = endpoint

    original = FOODCHAT.delete
    try:
        FOODCHAT.delete = fake_delete
        asyncio.run(FOODCHAT.remove_facet(
            session_id="s1", member_id="m1", value="middle eastern",
        ))
    finally:
        FOODCHAT.delete = original
    assert captured["endpoint"] == "/foodchat/sessions/s1/facets/middle%20eastern"


def test_replan_omits_the_plan_type_when_none_was_given():
    """Sending an explicit null would override FoodChat's own "active canvas"
    default with nothing."""
    import asyncio

    from backend.foodchat import FOODCHAT

    captured = {}

    async def fake_post(endpoint, data=None, json=None, **kwargs):
        captured["endpoint"] = endpoint
        captured["json"] = json
        captured["kwargs"] = kwargs

    original = FOODCHAT.post
    try:
        FOODCHAT.post = fake_post
        asyncio.run(FOODCHAT.replan(session_id="s1", member_id="m1"))
    finally:
        FOODCHAT.post = original

    assert captured["json"] == {"member_id": "m1"}
    assert "plan_type" not in captured["json"]


def test_replan_gets_the_planning_timeout_not_the_default():
    """It generates a plan. The default timeout would cut it off mid-flight and
    surface as a gateway error rather than a plan."""
    import asyncio

    from backend.foodchat import FOODCHAT

    captured = {}

    async def fake_post(endpoint, data=None, json=None, **kwargs):
        captured.update(kwargs)

    original = FOODCHAT.post
    try:
        FOODCHAT.post = fake_post
        asyncio.run(FOODCHAT.replan(session_id="s1", member_id="m1"))
    finally:
        FOODCHAT.post = original

    assert captured.get("timeout") == FOODCHAT._extra_long_timeout()


def test_a_tool_invocation_gets_the_same_timeout():
    """`replace_day` regenerates three slots against the recipe search."""
    import asyncio

    from backend.foodchat import FOODCHAT

    captured = {}

    async def fake_post(endpoint, data=None, json=None, **kwargs):
        captured["endpoint"] = endpoint
        captured["json"] = json
        captured.update(kwargs)

    original = FOODCHAT.post
    try:
        FOODCHAT.post = fake_post
        asyncio.run(FOODCHAT.invoke_tool(
            tool_name="replace_day", member_id="m1",
            arguments={"session_id": "s1", "day": 3},
        ))
    finally:
        FOODCHAT.post = original

    assert captured["endpoint"] == "/foodchat/tools/replace_day"
    assert captured["json"]["arguments"] == {"session_id": "s1", "day": 3}
    assert captured.get("timeout") == FOODCHAT._extra_long_timeout()


def test_the_pantry_replace_sends_the_whole_list():
    import asyncio

    from backend.foodchat import FOODCHAT

    captured = {}

    async def fake_put(endpoint, data=None, json=None, **kwargs):
        captured["endpoint"] = endpoint
        captured["json"] = json

    original = FOODCHAT.put
    try:
        FOODCHAT.put = fake_put
        asyncio.run(FOODCHAT.set_pantry(
            session_id="s1", member_id="m1", items=["zucchini"],
        ))
    finally:
        FOODCHAT.put = original

    assert captured["endpoint"] == "/foodchat/sessions/s1/pantry"
    assert captured["json"] == {"member_id": "m1", "items": ["zucchini"]}


# ── the signed member assertion ───────────────────────────────────────────
#
# FoodChat is internally unauthenticated: it takes `member_id` as data and
# believes it. This gateway is the only party that can answer "does this
# Keycloak user own this member", so it signs that answer and FoodChat verifies
# the signature. Without it, anything that can reach FoodChat's port can act as
# any member alive.

SECRET = "test-assertion-secret"


def _capture_request(monkeypatch, secret=SECRET):
    """Run one FoodChat call against a fake transport, return what it sent."""
    import asyncio
    import sys

    import httpx

    # `backend.foodchat` imports `main`, which imports the router, which imports
    # `backend.foodchat` — so importing the backend FIRST hits a half-built
    # module. Importing main first is what the route tests above already do.
    sys.path.insert(0, "src")
    import main  # noqa: F401

    from backend.foodchat import FoodChat, FOODCHAT

    if secret is None:
        monkeypatch.delenv("FOODCHAT_ASSERTION_SECRET", raising=False)
    else:
        monkeypatch.setenv("FOODCHAT_ASSERTION_SECRET", secret)

    seen = {}

    class _FakeClient:
        async def request(self, method, endpoint, **kwargs):
            seen["method"] = method
            seen["endpoint"] = endpoint
            seen["headers"] = kwargs.get("headers") or {}
            # A response needs its request attached, or `raise_for_status()`
            # raises on the missing link rather than on the status.
            return httpx.Response(
                200, json={"ok": True},
                request=httpx.Request(method, f"http://foodchat.test{endpoint}"),
            )

    monkeypatch.setattr(FoodChat, "_require_client", classmethod(lambda cls: _FakeClient()))
    return seen, asyncio, FOODCHAT


def test_a_member_scoped_call_carries_a_signed_assertion(monkeypatch):
    seen, asyncio, client = _capture_request(monkeypatch)
    asyncio.run(client.get_planning_state(session_id="s1", member_id="m1"))
    header = seen["headers"].get("X-WiseFood-Member")
    assert header and header.startswith("m1.")


def test_the_assertion_verifies_with_the_shared_secret(monkeypatch):
    """Signed the way FoodChat's `auth.verify` expects — same bytes, or the
    header is just noise the other side rejects."""
    import hashlib
    import hmac

    seen, asyncio, client = _capture_request(monkeypatch)
    asyncio.run(client.get_planning_state(session_id="s1", member_id="m1"))
    member_id, expires, digest = seen["headers"]["X-WiseFood-Member"].rsplit(".", 2)
    expected = hmac.new(
        SECRET.encode(), f"{member_id}|{expires}".encode(), hashlib.sha256
    ).hexdigest()
    assert digest == expected


def test_it_expires(monkeypatch):
    import time

    seen, asyncio, client = _capture_request(monkeypatch)
    asyncio.run(client.get_planning_state(session_id="s1", member_id="m1"))
    expires = int(seen["headers"]["X-WiseFood-Member"].rsplit(".", 2)[1])
    assert 0 < expires - int(time.time()) <= 300


def test_the_member_in_the_body_is_the_one_signed(monkeypatch):
    """The header follows the payload rather than being passed separately, so
    the two cannot disagree about who is acting."""
    seen, asyncio, client = _capture_request(monkeypatch)
    asyncio.run(client.set_pantry(session_id="s1", member_id="body-member", items=[]))
    assert seen["headers"]["X-WiseFood-Member"].startswith("body-member.")


def test_a_call_with_no_member_sends_no_assertion(monkeypatch):
    """`/tools` and `/vocabularies` are the same for everyone and name nobody."""
    seen, asyncio, client = _capture_request(monkeypatch)
    asyncio.run(client.list_tools())
    assert "X-WiseFood-Member" not in seen["headers"]


def test_no_secret_means_no_header(monkeypatch):
    """The two services deploy independently. A gateway with no secret behaves
    exactly as before, so neither side can be deployed into an outage."""
    seen, asyncio, client = _capture_request(monkeypatch, secret=None)
    asyncio.run(client.get_planning_state(session_id="s1", member_id="m1"))
    assert "X-WiseFood-Member" not in seen["headers"]


def test_it_is_minted_in_one_place_not_twenty(monkeypatch):
    """Every client method goes through `request()`. A header added per-method
    is a header missing from one of them, and that one is the open route."""
    import inspect

    from backend.foodchat import FoodChat

    source = inspect.getsource(FoodChat)
    assert source.count("ASSERTION_HEADER") == 2  # the constant, and its one use


def test_a_member_named_only_in_the_path_is_still_asserted(monkeypatch):
    """`/members/{id}/sessions` lists someone's whole conversation history from
    their id alone, and the id is in neither the body nor the query."""
    seen, asyncio, client = _capture_request(monkeypatch)
    asyncio.run(client.get_member_sessions(member_id="m1"))
    assert seen["headers"]["X-WiseFood-Member"].startswith("m1.")


@pytest.mark.parametrize("method,kwargs", [
    ("get_planning_state", {"session_id": "s1", "member_id": "m1"}),
    ("get_member_sessions", {"member_id": "m1"}),
    ("get_member_current_plans", {"member_id": "m1"}),
    ("set_pantry", {"session_id": "s1", "member_id": "m1", "items": []}),
    ("add_pantry_items", {"session_id": "s1", "member_id": "m1", "items": ["x"]}),
    ("remove_pantry_item", {"session_id": "s1", "member_id": "m1", "item": "x"}),
    ("remove_facet", {"session_id": "s1", "member_id": "m1", "value": "light"}),
    ("add_facets", {"session_id": "s1", "member_id": "m1", "values": ["light"]}),
    ("replan", {"session_id": "s1", "member_id": "m1"}),
    ("invoke_tool", {"tool_name": "summarize_week", "member_id": "m1", "arguments": {}}),
    ("get_session", {"session_id": "s1", "member_id": "m1"}),
    ("get_member_saved_plans", {"member_id": "m1"}),
])
def test_every_member_scoped_call_is_asserted(monkeypatch, method, kwargs):
    seen, asyncio, client = _capture_request(monkeypatch)
    asyncio.run(getattr(client, method)(**kwargs))
    assert seen["headers"].get("X-WiseFood-Member", "").startswith("m1."), method


# ── the audit that keeps this true as routes are added ────────────────────
#
# Every check above names its handler by hand, which is fine until someone adds
# the thirty-second proxy. This walks the router's own table instead: any
# handler that names a member must authorize that member before forwarding,
# and any that does not name one must be listed here with a reason.

MEMBERLESS_PROXIES = {
    "status": "a health probe, and the member has nothing to do with it",
    "list_tools": "the tool manifest is identical for every member",
    "get_vocabularies": "the corpus vocabulary is identical for every member",
}


def _foodchat_handlers():
    import sys

    sys.path.insert(0, "src")
    import main

    seen = {}
    for route in main.api.routes:
        path = getattr(route, "path", "")
        endpoint = getattr(route, "endpoint", None)
        if path.startswith(FOODCHAT_PREFIX) and endpoint is not None:
            seen[endpoint.__name__] = endpoint
    return seen


def test_every_proxy_that_names_a_member_authorizes_it():
    """The gateway is the only layer that knows WHO the caller is. A proxy that
    forwards a member_id without checking it lets any authenticated user act as
    any member — which is the whole reason FoodChat's own assertion exists."""
    import inspect

    offenders = []
    for name, fn in _foodchat_handlers().items():
        src = inspect.getsource(fn)
        names_member = "member_id" in src
        authorizes = "verify_member_access" in src
        if names_member and not authorizes:
            offenders.append(name)
    assert not offenders, (
        "these proxies forward a member without authorizing it: "
        + ", ".join(sorted(offenders))
    )


def test_every_proxy_without_a_member_is_declared():
    """So a route that quietly stops naming a member gets noticed."""
    import inspect

    undeclared = []
    for name, fn in _foodchat_handlers().items():
        if "member_id" not in inspect.getsource(fn) and name not in MEMBERLESS_PROXIES:
            undeclared.append(name)
    assert not undeclared, (
        "these proxies name no member and are not declared memberless: "
        + ", ".join(sorted(undeclared))
    )


def test_every_proxy_requires_authentication():
    import sys

    sys.path.insert(0, "src")
    import main

    unguarded = [
        route.path
        for route in main.api.routes
        if getattr(route, "path", "").startswith(FOODCHAT_PREFIX)
        and not getattr(route, "dependencies", None)
    ]
    assert not unguarded, f"unauthenticated foodchat proxies: {unguarded}"


def test_authorization_precedes_the_forward_everywhere():
    """Authorizing after the call would still have done the thing."""
    import inspect

    late = []
    for name, fn in _foodchat_handlers().items():
        src = inspect.getsource(fn)
        if "verify_member_access" not in src:
            continue
        if src.find("verify_member_access") > src.find("FOODCHAT."):
            late.append(name)
    assert not late, f"these authorize after forwarding: {late}"
