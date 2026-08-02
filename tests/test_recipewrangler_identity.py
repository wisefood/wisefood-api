"""Identity forwarding and filter forwarding to RecipeWrangler.

RecipeWrangler does no authentication of its own — it is ClusterIP-only and
trusts this service to tell it who is calling. That makes the header-building
here the single point where "expert" is decided for everything downstream:
whether a response carries `creator`, and whether withdrawn recipes are visible.

The forwarding tests exist for a duller reason. Every recipe filter has to be
named in four places — schema, route, client signature, payload dict — and a
field missing from the last one reaches RecipeWrangler as *no filter at all*.
The request still succeeds and still returns recipes, so the failure looks like
a search that ignores its own filter panel rather than like an error.
"""
import importlib.util
import sys
import types

import pytest


@pytest.fixture(scope="module")
def rw_backend():
    """`backend.recipewrangler`, loaded without starting the FastAPI app.

    Importing it normally pulls in `main`, which imports every router. The
    module under test only needs `config.settings` and `auth._extract_roles`.
    """
    main_stub = types.ModuleType("main")
    main_stub.config = types.SimpleNamespace(
        settings={"RECIPEWRANGLER_URL": "http://recipewrangler:8000"}
    )

    # Only `main` is stubbed. `exceptions` and `auth` are imported for real —
    # `identity_headers` must be tested against the actual `_extract_roles`,
    # since the whole point is that both services resolve roles identically.
    saved_main = sys.modules.get("main")
    sys.modules["main"] = main_stub
    try:
        spec = importlib.util.spec_from_file_location(
            "_rw_backend_under_test", "src/backend/recipewrangler.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        yield module
    finally:
        if saved_main is None:
            sys.modules.pop("main", None)
        else:
            sys.modules["main"] = saved_main


class TestIdentityHeaders:
    def test_no_token_forwards_nothing(self, rw_backend):
        """An unauthenticated request must not forward a partial identity.

        Anonymous is a valid state downstream — creator is hidden, withdrawn
        recipes stay hidden. Sending an empty `X-User-Roles` instead would be
        parsed as "authenticated with no roles", which is the same outcome by
        accident rather than by intent.
        """
        assert rw_backend.RECIPEWRANGLER.identity_headers(None) == {}
        assert rw_backend.RECIPEWRANGLER.identity_headers({}) == {}

    def test_expert_token_forwards_sub_name_and_roles(self, rw_backend):
        headers = rw_backend.RECIPEWRANGLER.identity_headers(
            {
                "sub": "abc-123",
                "preferred_username": "dr.expert",
                "realm_access": {"roles": ["expert"]},
            }
        )
        assert headers["X-User-Sub"] == "abc-123"
        assert headers["X-User-Name"] == "dr.expert"
        assert "expert" in headers["X-User-Roles"].split(",")

    def test_roles_come_from_both_realm_and_client(self, rw_backend):
        """Resolved exactly as this service resolves them for its own routes.

        Its recipewrangler routes are guarded with `auth("admin,expert")`. If
        header-building used a different resolution, a caller could be an expert
        to this service and a member to RecipeWrangler — or the reverse.
        """
        headers = rw_backend.RECIPEWRANGLER.identity_headers(
            {
                "sub": "abc-123",
                "realm_access": {"roles": ["offline_access"]},
                "resource_access": {"wisefood": {"roles": ["admin"]}},
            }
        )
        assert "admin" in headers["X-User-Roles"].split(",")

    def test_a_member_token_carries_no_privileged_role(self, rw_backend):
        headers = rw_backend.RECIPEWRANGLER.identity_headers(
            {"sub": "m-1", "realm_access": {"roles": ["default-roles-wisefood"]}}
        )
        roles = set(headers["X-User-Roles"].split(","))
        assert not (roles & {"admin", "expert", "agent"})


class TestFilterForwarding:
    """The payload dicts must carry every filter the request model accepts."""

    def test_filter_fields_are_declared_on_both_request_models(self, rw_backend):
        """A field the client forwards but no model declares can never arrive."""
        from schemas import RecipeParamSearchRequest, RecipeSearchRequest

        forwarded = set(rw_backend.RECIPEWRANGLER._FILTER_FIELDS)
        nl_fields = set(RecipeSearchRequest.model_fields)
        assert forwarded <= nl_fields, forwarded - nl_fields

        # param_search has no question to state a diet in, so the hard/soft
        # distinction `require_diet_tags` draws does not apply — its `diet_tags`
        # is already a filter.
        param_fields = set(RecipeParamSearchRequest.model_fields)
        assert (forwarded - {"require_diet_tags"}) <= param_fields

    @pytest.mark.parametrize(
        "facet", ["cuisines", "moods", "flavor_profiles", "food_groups"]
    )
    def test_facets_default_to_empty_on_both_models(self, facet):
        """Defaults must be empty so an unfiltered request stays unfiltered."""
        from schemas import RecipeParamSearchRequest, RecipeSearchRequest

        assert getattr(RecipeSearchRequest(question="x"), facet) == []
        assert getattr(RecipeParamSearchRequest(), facet) == []

    def test_require_diet_tags_is_distinct_from_diet_tags(self):
        """They mean opposite things: hard filter vs. soft ranking boost."""
        from schemas import RecipeSearchRequest

        payload = RecipeSearchRequest(
            question="dinner", diet_tags=["vegetarian"], require_diet_tags=["vegan"]
        )
        assert payload.diet_tags == ["vegetarian"]
        assert payload.require_diet_tags == ["vegan"]


class TestCatalogPassthrough:
    """The catalog routes exist so the annotation facets are reachable at all.

    Cuisine, mood, flavour and food group are Elasticsearch-owned: no v1 recipe
    route can return them however it is called. If this passthrough regresses,
    the symptom downstream is a recipe page that silently shows no cuisine —
    not an error — so the contract is asserted rather than assumed.
    """

    def test_catalog_calls_target_the_v2_surface(self, rw_backend):
        """A v1 path here would reach Neo4j and return no annotations."""
        import inspect

        client = rw_backend.RECIPEWRANGLER
        for name in (
            "catalog_search",
            "catalog_facets",
            "catalog_browse",
            "catalog_vocabulary",
            "catalog_get_recipe",
        ):
            source = inspect.getsource(getattr(client, name))
            assert "/api/v2/recipes" in source, name
            assert "/api/v1/recipes" not in source, name

    def test_search_request_matches_the_catalog_contract(self):
        """Field-for-field with RecipeWrangler's CatalogSearchRequest.

        The contract's value is that one client works against the catalog and
        the recipe corpus alike; a field dropped here is a capability the UI
        cannot ask for even though the index supports it.
        """
        from schemas import CatalogSearchRequest

        assert set(CatalogSearchRequest.model_fields) == {
            "q", "fq", "fl", "sort", "facets", "facet_limit",
            "limit", "offset", "include_inactive", "highlight",
        }

    def test_an_empty_search_is_unconstrained_and_public(self):
        """Defaults must not filter, and must not opt into withdrawn recipes."""
        from schemas import CatalogSearchRequest

        payload = CatalogSearchRequest()
        assert payload.q is None
        assert payload.fq == []
        assert payload.facets == []
        assert payload.include_inactive is False

    def test_recipe_id_route_is_declared_after_the_static_ones(self):
        """FastAPI matches in declaration order.

        `/catalog/{recipe_id}` registered first would swallow /facets, /browse
        and /vocabulary and proxy their names as recipe ids — a 404 that looks
        like missing data rather than like a routing bug.

        Read from the source rather than the built app: importing `main` here
        picks up the stub the `rw_backend` fixture installs, and declaration
        order is a property of the file anyway.
        """
        import ast

        tree = ast.parse(open("src/routers/recipewrangler.py").read())
        paths = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.AsyncFunctionDef):
                continue
            for dec in node.decorator_list:
                if not isinstance(dec, ast.Call) or not dec.args:
                    continue
                arg = dec.args[0]
                if isinstance(arg, ast.Constant) and str(arg.value).startswith("/catalog/"):
                    paths.append(arg.value)

        assert paths, "no catalog routes declared"
        assert paths.index("/catalog/{recipe_id}") == len(paths) - 1
        for static in ("facets", "browse", "vocabulary", "search"):
            assert f"/catalog/{static}" in paths
