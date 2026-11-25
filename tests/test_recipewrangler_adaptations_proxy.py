"""GET /recipes/{id}/adaptations, as proxied by this gateway.

RecipeWrangler answers every adaptation mode (nutrition, sustainability, cost,
time) from one route, and the UI's prod transport only reaches it through
here. A route that exists on the backend and not on the gateway is a 404 the
browser sees and nobody else does, so pin the path, the verb, the query names
the UI sends, and how the client forwards them: `allergens` must reach the
backend as a repeated parameter, and an empty restriction must not be sent at
all (with allergens or a diet the backend never serves a stored row).
"""
import asyncio
import sys

PREFIX = "/api/v1/recipewrangler"
PATH = f"{PREFIX}/recipes/{{recipe_id}}/adaptations"


def _route():
    sys.path.insert(0, "src")
    import main

    for route in main.api.routes:
        if getattr(route, "path", "") == PATH and "GET" in getattr(route, "methods", set()):
            return route
    raise AssertionError(f"GET {PATH} is not registered")


def test_route_is_a_get_with_the_query_the_ui_sends():
    route = _route()
    names = {param.name for param in route.dependant.query_params}
    assert {"mode", "region", "allergens", "diet"} <= names


def _client(monkeypatch):
    sys.path.insert(0, "src")
    from backend.recipewrangler import RecipeWrangler

    seen = {}

    async def fake_get(cls, endpoint, params=None, **kwargs):
        seen.update(endpoint=endpoint, params=params, kwargs=kwargs)
        return {"status": "adapted"}

    monkeypatch.setattr(RecipeWrangler, "get", classmethod(fake_get))
    return RecipeWrangler, seen


def test_client_forwards_mode_region_and_repeats_allergens(monkeypatch):
    client, seen = _client(monkeypatch)
    out = asyncio.run(client.get_adaptation(
        "r1", mode="cost", region="IE", allergens=["milk", "gluten"], diet="vegan",
    ))
    assert out == {"status": "adapted"}
    assert seen["endpoint"] == "/api/v1/recipes/r1/adaptations"
    assert seen["params"] == {
        "mode": "cost", "region": "IE", "allergens": ["milk", "gluten"], "diet": "vegan",
    }
    assert seen["kwargs"]["timeout"] >= 60  # a live nutrition run takes up to a minute


def test_client_sends_no_restriction_when_there_is_none(monkeypatch):
    client, seen = _client(monkeypatch)
    asyncio.run(client.get_adaptation("r1", mode="time", region="EU", allergens=[], diet=None))
    assert seen["params"] == {"mode": "time", "region": "EU"}
