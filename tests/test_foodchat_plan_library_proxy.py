"""
The FoodChat proxy routes for session naming and the saved-plan library.

These three existed on both ends and nowhere in between: the UI has shipped a
save/unsave button, a saved-plans list and a rename control since the
plan-canvas work, FoodChat implements all three, and every call 404'd at this
gateway because no route proxied it. The tests assert the contract the UI
already calls — path, verb and payload — so the gap cannot silently reopen.
"""
import pytest
from pydantic import ValidationError


# ── request validation ────────────────────────────────────────────────────

def test_rename_requires_a_non_empty_title():
    from schemas import FoodChatRenameSessionRequest

    with pytest.raises(ValidationError):
        FoodChatRenameSessionRequest(member_id="m1", title="")


def test_rename_rejects_an_overlong_title():
    # FoodChat caps the column at 120; rejecting here beats a silent truncation.
    from schemas import FoodChatRenameSessionRequest

    with pytest.raises(ValidationError):
        FoodChatRenameSessionRequest(member_id="m1", title="x" * 121)


def test_rename_requires_a_member():
    from schemas import FoodChatRenameSessionRequest

    with pytest.raises(ValidationError):
        FoodChatRenameSessionRequest(title="Pasta week")


def test_save_defaults_to_saving():
    from schemas import FoodChatSavePlanRequest

    assert FoodChatSavePlanRequest(member_id="m1").saved is True


def test_save_carries_an_optional_title():
    from schemas import FoodChatSavePlanRequest

    assert FoodChatSavePlanRequest(member_id="m1").title is None
    req = FoodChatSavePlanRequest(member_id="m1", title="Pasta week")
    assert req.title == "Pasta week"


def test_unsave_is_expressible():
    # The same endpoint toggles both ways; the UI button depends on it.
    from schemas import FoodChatSavePlanRequest

    assert FoodChatSavePlanRequest(member_id="m1", saved=False).saved is False


def test_save_rejects_an_overlong_title():
    from schemas import FoodChatSavePlanRequest

    with pytest.raises(ValidationError):
        FoodChatSavePlanRequest(member_id="m1", title="x" * 121)


# ── the routes the UI actually calls ──────────────────────────────────────

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


@pytest.mark.parametrize("verb,path", [
    ("PATCH", f"{FOODCHAT_PREFIX}/sessions/{{session_id}}"),
    ("POST", f"{FOODCHAT_PREFIX}/sessions/{{session_id}}/meal-plans/{{plan_id}}/save"),
    ("GET", f"{FOODCHAT_PREFIX}/members/{{member_id}}/saved-plans"),
])
def test_the_route_the_ui_calls_is_registered(verb, path):
    assert (verb, path) in _routes(), f"{verb} {path} is not proxied"


def test_patch_does_not_displace_the_existing_session_verbs():
    routes = _routes()
    session = f"{FOODCHAT_PREFIX}/sessions/{{session_id}}"
    for verb in ("GET", "DELETE", "PATCH"):
        assert (verb, session) in routes, f"{verb} lost on the session route"


# ── the backend client ────────────────────────────────────────────────────

def test_the_client_can_speak_patch():
    """The client had get/post/put/delete only, which is why rename could not
    be proxied at all — the verb was missing, not just the route."""
    from backend.foodchat import FOODCHAT

    assert hasattr(FOODCHAT, "patch")


@pytest.mark.parametrize("method", [
    "rename_session", "save_meal_plan", "get_member_saved_plans",
])
def test_the_client_exposes_the_three_methods(method):
    from backend.foodchat import FOODCHAT

    assert hasattr(FOODCHAT, method)


def test_save_omits_the_title_when_none_was_given():
    """FoodChat falls back to the session title; sending an explicit null
    would overwrite that fallback with nothing."""
    import asyncio

    from backend.foodchat import FOODCHAT

    captured = {}

    async def fake_post(endpoint, data=None, json=None, **kwargs):
        captured["endpoint"] = endpoint
        captured["json"] = json

    original = FOODCHAT.post
    try:
        FOODCHAT.post = fake_post
        asyncio.run(FOODCHAT.save_meal_plan(
            session_id="s1", plan_id="p1", member_id="m1",
        ))
    finally:
        FOODCHAT.post = original

    assert captured["endpoint"] == "/foodchat/sessions/s1/meal-plans/p1/save"
    assert "title" not in captured["json"]
    assert captured["json"] == {"member_id": "m1", "saved": True}
