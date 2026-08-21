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
