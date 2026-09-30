"""The knowledge graph browse routes, as proxied by this gateway.

FoodScholar grew a whole `/api/v1/graph` surface — hierarchy, search,
autocomplete, evidence, ontology entities and two SSE streams — and none of it
was reachable. The UI does not talk to FoodScholar; it talks to this gateway,
and a route that exists on one end and not the other is a 404 the browser sees
and nobody else does. That is the same gap the FoodChat plan-library proxy was
written to close, so these tests are shaped the same way: assert the path, the
verb, the gate and the forwarding, so it cannot silently reopen.
"""
import asyncio

import pytest


PREFIX = "/api/v1/foodscholar"


def _routes():
    import sys

    sys.path.insert(0, "src")
    import main

    return {
        (method, route.path)
        for route in main.api.routes
        if getattr(route, "path", "").startswith(f"{PREFIX}/graph")
        for method in getattr(route, "methods", set())
    }


def _route(path):
    import sys

    sys.path.insert(0, "src")
    import main

    for route in main.api.routes:
        if getattr(route, "path", "") == path:
            return route
    raise AssertionError(f"{path} is not registered")


# ── every route the graph browser calls ───────────────────────────────────

@pytest.mark.parametrize("verb,path", [
    ("GET", f"{PREFIX}/graph/summary"),
    ("GET", f"{PREFIX}/graph/facets"),
    ("GET", f"{PREFIX}/graph/facets/{{facet}}/roots"),
    ("GET", f"{PREFIX}/graph/search"),
    ("GET", f"{PREFIX}/graph/suggest"),
    ("GET", f"{PREFIX}/graph/filters"),
    ("GET", f"{PREFIX}/graph/nodes/{{node_id:path}}"),
    ("GET", f"{PREFIX}/graph/nodes/{{node_id:path}}/children"),
    ("GET", f"{PREFIX}/graph/nodes/{{node_id:path}}/themes"),
    ("GET", f"{PREFIX}/graph/nodes/{{node_id:path}}/breadcrumb"),
    ("GET", f"{PREFIX}/graph/nodes/{{node_id:path}}/chunks"),
    ("GET", f"{PREFIX}/graph/cards/{{target_id:path}}"),
    ("GET", f"{PREFIX}/graph/entities"),
    ("GET", f"{PREFIX}/graph/entities/{{ontology_id}}"),
    ("GET", f"{PREFIX}/graph/entities/{{ontology_id}}/chunks"),
    ("GET", f"{PREFIX}/graph/stream"),
    ("GET", f"{PREFIX}/graph/stream/expand"),
    ("POST", f"{PREFIX}/graph/reindex"),
])
def test_the_route_the_ui_calls_is_registered(verb, path):
    assert (verb, path) in _routes(), f"{verb} {path} is not proxied"


@pytest.mark.parametrize("method", [
    "graph_summary", "graph_facets", "graph_facet_roots", "graph_node",
    "graph_node_children", "graph_node_themes", "graph_node_breadcrumb",
    "graph_node_chunks", "graph_card", "graph_search", "graph_suggest",
    "graph_filters", "graph_entities", "graph_entity", "graph_entity_chunks",
    "graph_reindex", "graph_stream",
])
def test_the_client_exposes_the_method(method):
    from backend.foodscholar import FOODSCHOLAR

    assert hasattr(FOODSCHOLAR, method)


# ── slashed ids reach their routes ────────────────────────────────────────

THEME = "foods/olive_oil/monounsaturated_fat_r1"
CARD = f"card:{THEME}"


def _match(path):
    """Which endpoint the gateway's router picks for a request path, and the
    path params it extracts. Route matching only: no auth, no upstream."""
    import sys

    from starlette.routing import Match

    sys.path.insert(0, "src")
    import main

    scope = {"type": "http", "method": "GET", "path": path, "root_path": ""}
    for route in main.api.router.routes:
        match, child = route.matches(scope)
        if match is Match.FULL:
            return route.endpoint.__name__, child["path_params"]
    raise AssertionError(f"nothing matches {path}")


@pytest.mark.parametrize("suffix,endpoint", [
    ("", "graph_node"),
    ("/children", "graph_node_children"),
    ("/themes", "graph_node_themes"),
    ("/breadcrumb", "graph_node_breadcrumb"),
    ("/chunks", "graph_node_chunks"),
])
def test_a_theme_id_with_slashes_reaches_its_node_route(suffix, endpoint):
    """Theme ids are slash-separated and the server decodes %2F before routing.
    With a plain `{node_id}` the tree opened and every theme inside it was a
    404 raised here, never reaching FoodScholar."""
    name, params = _match(f"{PREFIX}/graph/nodes/{THEME}{suffix}")
    assert name == endpoint
    assert params == {"node_id": THEME}


def test_a_card_id_with_slashes_reaches_the_card_route():
    name, params = _match(f"{PREFIX}/graph/cards/{CARD}")
    assert name == "graph_card"
    assert params == {"target_id": CARD}


def test_a_shelf_id_still_routes_as_before():
    name, params = _match(f"{PREFIX}/graph/nodes/foodon:00001234/children")
    assert (name, params) == ("graph_node_children", {"node_id": "foodon:00001234"})


def test_the_bare_node_route_does_not_swallow_sub_routes():
    """`path` matches slashes, so the bare route must be registered last."""
    for suffix in ("children", "themes", "breadcrumb", "chunks"):
        name, params = _match(f"{PREFIX}/graph/nodes/foodon:1/{suffix}")
        assert name == f"graph_node_{suffix}", f"/{suffix} was taken as part of the id"
        assert params["node_id"] == "foodon:1"


# ── the gates ─────────────────────────────────────────────────────────────

def _dependency_source(path):
    """The source of every dependency callable guarding a route.

    `auth("admin")` closes over its required permissions, so the role cannot be
    read off the dependency by name. Reading the closure is the only way to
    tell an admin gate from an open one without standing up Keycloak.
    """
    route = _route(path)
    found = []
    for dependant in route.dependant.dependencies:
        call = dependant.call
        cells = getattr(call, "__closure__", None) or ()
        found.append([c.cell_contents for c in cells])
    return found


def test_reindex_is_admin_only():
    """It reads the whole graph and rewrites an index. Not a browse action."""
    closures = _dependency_source(f"{PREFIX}/graph/reindex")
    flat = [str(value) for cells in closures for value in cells]
    assert any("admin" in value for value in flat), (
        "reindex must be gated on the admin role"
    )


@pytest.mark.parametrize("path", [
    f"{PREFIX}/graph/summary",
    f"{PREFIX}/graph/search",
    f"{PREFIX}/graph/stream",
])
def test_browsing_is_authenticated(path):
    assert _route(path).dependant.dependencies, f"{path} has no auth dependency"


def test_browsing_is_not_restricted_to_a_role():
    """The graph is the corpus's table of contents: no household or member
    data in it, and a guest who may ask a question may see what answers are
    drawn from. A role gate here would be a feature nobody asked for."""
    closures = _dependency_source(f"{PREFIX}/graph/search")
    flat = [str(value) for cells in closures for value in cells]
    assert not any("admin" in value or "expert" in value for value in flat)


# ── parameter forwarding ──────────────────────────────────────────────────

def test_filters_are_forwarded_verbatim():
    """The filter vocabulary belongs to FoodScholar. Restating it here would
    mean every new filter needs two edits and does nothing after one."""
    import routers.foodscholar as fs

    class _Req:
        query_params = {
            "q": "olive oil",
            "kind": "shelf,theme",
            "facet": "foods",
            "min_chunks": "5",
            "has_card": "true",
        }

    assert fs._graph_params(_Req()) == dict(_Req.query_params)


def test_blank_filters_are_dropped():
    """A cleared search box sends `q=`; forwarded, that is a query for the
    empty string rather than no query at all, and it matches nothing."""
    import routers.foodscholar as fs

    class _Req:
        query_params = {"q": "", "facet": "foods", "cursor": ""}

    assert fs._graph_params(_Req()) == {"facet": "foods"}


def test_node_ids_are_quoted_into_the_upstream_path():
    """FoodOn-derived ids carry colons and slashes. Unquoted, a slash invents
    a path segment and the upstream answers 404 for a node that exists."""
    from backend.foodscholar import FOODSCHOLAR

    captured = {}

    async def fake_get(endpoint, params=None, **kwargs):
        captured["endpoint"] = endpoint
        return {}

    original = FOODSCHOLAR.get
    try:
        FOODSCHOLAR.get = fake_get
        asyncio.run(FOODSCHOLAR.graph_node("shelf/FOODON:03301234"))
    finally:
        FOODSCHOLAR.get = original

    assert captured["endpoint"] == (
        "/api/v1/graph/nodes/shelf%2FFOODON%3A03301234"
    )


# ── the streams ───────────────────────────────────────────────────────────

def test_the_stream_routes_are_not_enveloped():
    """`@render()` builds a {help, success, result} envelope. Wrapping an SSE
    body in one would turn a live stream into a JSON document that arrives
    once, at the end — which is the opposite of the point."""
    import routers.foodscholar as fs

    for name in ("graph_stream", "graph_stream_expand"):
        handler = getattr(fs, name)
        assert not hasattr(handler, "__wrapped__"), (
            f"{name} looks like it went through @render()"
        )


def test_the_stream_is_primed_before_the_response_is_returned():
    """An upstream that refuses the connection must surface as an error
    response, not as a 200 that opens and then says nothing. Priming means
    pulling the first chunk before StreamingResponse is constructed."""
    import routers.foodscholar as fs

    calls = {"started": 0}

    async def fake_stream(path, params):
        calls["started"] += 1
        raise RuntimeError("upstream refused")
        yield b""  # pragma: no cover - makes this an async generator

    original = fs.FOODSCHOLAR.graph_stream
    try:
        fs.FOODSCHOLAR.graph_stream = fake_stream
        with pytest.raises(RuntimeError):
            asyncio.run(fs._graph_sse("/api/v1/graph/stream", {}))
    finally:
        fs.FOODSCHOLAR.graph_stream = original

    assert calls["started"] == 1


def test_the_stream_response_disables_proxy_buffering():
    """Without X-Accel-Buffering an nginx-style ingress buffers the whole
    stream and the progressive draw the endpoint exists for never happens."""
    import routers.foodscholar as fs

    async def fake_stream(path, params):
        yield b"event: meta\ndata: {}\n\n"

    original = fs.FOODSCHOLAR.graph_stream
    try:
        fs.FOODSCHOLAR.graph_stream = fake_stream
        response = asyncio.run(fs._graph_sse("/api/v1/graph/stream", {}))
    finally:
        fs.FOODSCHOLAR.graph_stream = original

    assert response.media_type == "text/event-stream"
    assert response.headers.get("x-accel-buffering") == "no"
    assert response.headers.get("cache-control") == "no-cache"


# ── telemetry ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("event_type", [
    "graph.select", "graph.expand", "graph.scope", "graph.search",
    "graph.ask_bridge",
])
def test_the_graph_events_are_accepted(event_type):
    """The allowlist is the thing that actually decides.

    A browser event type missing from CLIENT_EVENT_TYPES is a 422, and the
    whole batch it travelled in is rejected with it — so the graph tab would
    have silently lost its telemetry AND the page-view events batched
    alongside. The UI mirrors this list in services/analyticsApi.ts, where it
    is only a type: this is the copy with teeth.
    """
    from schemas import ActivityEventIn

    assert ActivityEventIn(type=event_type).type == event_type


def test_an_invented_graph_event_is_still_refused():
    """The allowlist exists because event_type is an indexed column and the
    console filters on it. Adding five is not opening it."""
    from pydantic import ValidationError
    from schemas import ActivityEventIn

    with pytest.raises(ValidationError):
        ActivityEventIn(type="graph.whatever")
