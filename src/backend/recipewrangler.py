import httpx
from contextvars import ContextVar
from typing import Any, Dict, Optional
from main import config
from exceptions import APIException
import logging

logger = logging.getLogger(__name__)

# The verified token payload for the in-flight request.
#
# Set by the identity middleware in main.py and read by `_request`, so every
# proxied call forwards the caller automatically. The alternative was threading
# a `headers=` argument through all eighteen routes, which fails the moment
# someone adds a nineteenth and forgets.
CURRENT_TOKEN_PAYLOAD: ContextVar[Optional[Dict[str, Any]]] = ContextVar(
    "rw_current_token_payload", default=None
)


def _raise_for_upstream(response: httpx.Response) -> None:
    """Propagate RecipeWrangler errors with their real status and detail.

    raise_for_status() used to surface every upstream 4xx as an opaque 500
    InternalError, hiding messages like "No profile found ... Profile the
    recipe first." from clients.
    """
    if response.status_code < 400:
        return
    detail = ""
    code = None
    try:
        body = response.json()
        if isinstance(body, dict):
            err = body.get("error")
            if isinstance(err, dict):
                detail = str(err.get("detail") or "")
                code = err.get("code")
            if not detail and body.get("detail") is not None:
                raw = body.get("detail")
                detail = raw if isinstance(raw, str) else str(raw)
    except Exception:
        detail = (response.text or "")[:300]
    raise APIException(
        status_code=response.status_code,
        detail=detail or f"RecipeWrangler returned HTTP {response.status_code}",
        code=code or "upstream/recipewrangler",
        extra={"title": "UpstreamError", "upstream_status": response.status_code},
    )


class RecipeWrangler:
    """Singleton HTTP client for accessing the RecipeWrangler API with connection pooling."""

    _client: Optional[httpx.AsyncClient] = None

    @classmethod
    def get_client(
        cls,
        base_url: str = config.settings["RECIPEWRANGLER_URL"],
        timeout: float = 60.0,
        max_connections: int = 15,
        max_keepalive_connections: int = 7,
        verify: bool = True,
        http2: bool = True,
    ) -> "RecipeWrangler":
        """Get or create a singleton RecipeWrangler client instance."""
        if cls._client is None:
            cls._client = httpx.AsyncClient(
                base_url=base_url.rstrip("/"),
                timeout=timeout,
                verify=verify,
                http2=http2,
                limits=httpx.Limits(
                    max_connections=max_connections,
                    max_keepalive_connections=max_keepalive_connections,
                ),
            )
        return cls

    @staticmethod
    def identity_headers(token_payload: Optional[Dict[str, Any]]) -> Dict[str, str]:
        """Identity headers for a downstream RecipeWrangler call.

        RecipeWrangler performs no authentication of its own — this service
        has already verified the token, so it forwards the result. Without
        these headers every downstream caller is anonymous: writes record no
        creator, and `creator` plus withdrawn recipes stay hidden.

        Roles are taken from the same resolution this service uses for its own
        authorization (`auth._extract_roles`: realm_access.roles merged with
        resource_access[client].roles, lowercased), so the two cannot disagree
        about who an expert is.

        Only safe while RecipeWrangler is ClusterIP-only. If it is ever exposed
        through an ingress these become spoofable and must be replaced with
        token verification downstream.
        """
        if not token_payload:
            return {}
        from auth import _extract_roles

        headers: Dict[str, str] = {}
        sub = str(token_payload.get("sub") or "").strip()
        if sub:
            headers["X-User-Sub"] = sub
        username = str(token_payload.get("preferred_username") or "").strip()
        if username:
            headers["X-User-Name"] = username
        roles = _extract_roles(token_payload)
        if roles:
            headers["X-User-Roles"] = ",".join(roles)
        return headers

    @classmethod
    async def _request(cls, method: str, endpoint: str, **kwargs) -> httpx.Response:
        if cls._client is None:
            raise RuntimeError(
                "RecipeWrangler client not initialized. Call get_client() first."
            )
        # Forward who is asking. RecipeWrangler authenticates nobody; it
        # trusts this service to have done so.
        identity = cls.identity_headers(CURRENT_TOKEN_PAYLOAD.get())
        if identity:
            kwargs['headers'] = {**identity, **(kwargs.get('headers') or {})}

        try:
            response = await cls._client.request(method, endpoint, **kwargs)
        except httpx.TimeoutException as exc:
            raise APIException(
                status_code=504,
                detail=f"RecipeWrangler timed out on {method} {endpoint}",
                code="upstream/timeout",
                extra={"title": "UpstreamTimeout"},
            ) from exc
        _raise_for_upstream(response)
        return response

    @classmethod
    async def get(
        cls, endpoint: str, params: Optional[Dict[str, Any]] = None, **kwargs
    ):
        response = await cls._request("GET", endpoint, params=params, **kwargs)
        return response.json()

    @classmethod
    async def post(cls, endpoint: str, data: Any = None, json: Any = None, **kwargs):
        response = await cls._request("POST", endpoint, data=data, json=json, **kwargs)
        return response.json()

    @classmethod
    async def put(cls, endpoint: str, data: Any = None, json: Any = None, **kwargs):
        response = await cls._request("PUT", endpoint, data=data, json=json, **kwargs)
        return response.json()

    @classmethod
    async def patch(cls, endpoint: str, data: Any = None, json: Any = None, **kwargs):
        response = await cls._request("PATCH", endpoint, data=data, json=json, **kwargs)
        return response.json()

    @classmethod
    async def delete(cls, endpoint: str, **kwargs):
        response = await cls._request("DELETE", endpoint, **kwargs)
        return response.json() if response.text else {"status": "deleted"}

    @classmethod
    async def aclose(cls):
        if cls._client:
            await cls._client.aclose()
            cls._client = None

    @classmethod
    async def status(cls):
        return await cls.get("/health")

    @classmethod
    async def get_recipe(
        cls,
        recipe_id: str,
        *,
        region: Optional[str] = None,
        slim: bool = False,
        include_disabled: bool = False,
    ):
        """Retrieve a recipe by id, optionally requesting a slim response."""
        params: Dict[str, Any] = {}
        if region is not None:
            params["region"] = region.strip().upper()
        if slim:
            params["slim"] = True
        if include_disabled:
            params["include_disabled"] = True
        return await cls.get(
            f"/api/v1/recipes/{recipe_id}",
            params=params or None,
        )

    # Filter fields forwarded verbatim on both search paths.
    #
    # Listed once rather than spelled out per method: every one of these had to
    # be added in four places (schema, route, method signature, payload dict),
    # and the facet filters were silently dropped for exactly that reason —
    # a field absent from the payload dict reaches RecipeWrangler as "no
    # filter", so the request succeeds and quietly ignores what was asked.
    _FILTER_FIELDS = (
        "dish_types",
        "sources",
        "cuisines",
        "moods",
        "flavor_profiles",
        "food_groups",
        "require_diet_tags",
    )

    @classmethod
    async def search_recipes(
        cls,
        question: str,
        exclude_allergens: list[str] = None,
        diet_tags: list[str] = None,
        preferred_ingredients: list[str] = None,
        region: str = None,
        include_disabled: bool = False,
        **filters,
    ):
        """Search recipes via the knowledge graph."""
        payload = {
            "question": question,
            "exclude_allergens": exclude_allergens or [],
            "diet_tags": diet_tags or [],
            "preferred_ingredients": preferred_ingredients or [],
        }
        if region:
            payload["region"] = region
        if include_disabled:
            payload["include_disabled"] = True
        # Only non-empty filters go out. An empty list is not a constraint, and
        # omitting it keeps the request readable in the RecipeWrangler logs.
        payload.update(
            {name: filters[name] for name in cls._FILTER_FIELDS if filters.get(name)}
        )
        return await cls.post("/api/v1/recipes/search", json=payload)

    @classmethod
    async def param_search_recipes(
        cls,
        include_ingredients: list[str] = None,
        exclude_ingredients: list[str] = None,
        exclude_allergens: list[str] = None,
        diet_tags: list[str] = None,
        sources: list[str] = None,
        dish_types: list[str] = None,
        max_duration_minutes: Optional[int] = None,
        limit: int = 10,
        offset: int = 0,
        sort_by: str = "title_asc",
        include_facets: bool = False,
        include_disabled: bool = False,
        **filters,
    ):
        """Run deterministic parameter-based recipe search."""
        payload = {
            "include_ingredients": include_ingredients or [],
            "exclude_ingredients": exclude_ingredients or [],
            "exclude_allergens": exclude_allergens or [],
            "diet_tags": diet_tags or [],
            "sources": sources or [],
            "dish_types": dish_types or [],
            "max_duration_minutes": max_duration_minutes,
            "limit": limit,
            "offset": offset,
            "sort_by": sort_by,
            "include_facets": include_facets,
            "include_disabled": include_disabled,
        }
        payload.update(
            {name: filters[name] for name in cls._FILTER_FIELDS if filters.get(name)}
        )
        return await cls.post("/api/v1/recipes/param_search", json=payload)

    @classmethod
    async def profile_recipe(
        cls,
        raw_recipe: str,
        region: Optional[str] = None,
        persist_trace: bool = False,
        parse_only: bool = False,
    ):
        """Run parsing + profiling pipeline on raw recipe text."""
        payload = {
            "raw_recipe": raw_recipe,
            "persist_trace": persist_trace,
            "parse_only": parse_only,
        }
        if region is not None:
            payload["region"] = region
        return await cls.post("/api/v1/recipes/profile", json=payload)

    @classmethod
    async def recipe_details_batch(
        cls,
        recipe_ids: list[str],
        region: Optional[str] = None,
    ):
        """Batch-resolve recipe ids to slim cards with per-serving macros."""
        payload: dict = {"recipe_ids": recipe_ids}
        if region:
            payload["region"] = region
        return await cls.post("/api/v1/recipes/details", json=payload)

    @classmethod
    async def autocomplete_recipes(cls, q: str = "", limit: int = 8):
        """Autocomplete recipe titles from Elasticsearch."""
        return await cls.get(
            "/api/v1/recipes/autocomplete",
            params={"q": q, "limit": limit},
        )

    @classmethod
    async def count_recipes(cls):
        """Return the total number of recipes in the graph."""
        return await cls.get("/api/v1/recipes/count")

    # ------------------------------------------------------------------
    # Catalog (RecipeWrangler /api/v2/recipes)
    #
    # The v1 recipe routes read Neo4j, which has never held the annotation
    # facets — cuisine, mood, flavour, food group live only on the catalog
    # index. A client that needs them has no way to get there through the v1
    # surface, which is why the recipe page could not show a cuisine it had
    # already been classified with.
    #
    # These proxy RecipeWrangler's v2 catalog contract unchanged. They stay
    # under this service's own /api/v1 prefix: the version in our path is our
    # contract with the UI, not RecipeWrangler's with us.
    # ------------------------------------------------------------------

    @classmethod
    async def catalog_search(cls, payload: Dict[str, Any]):
        """Search the catalog index (q/fq/fl/sort/facets contract)."""
        return await cls.post("/api/v2/recipes/search", json=payload)

    @classmethod
    async def catalog_facets(cls):
        """List every field the catalog index can facet on."""
        return await cls.get("/api/v2/recipes/facets")

    @classmethod
    async def catalog_browse(
        cls,
        *,
        course_type: Optional[str] = None,
        cuisine: Optional[str] = None,
        source: Optional[str] = None,
        q: Optional[str] = None,
        limit: int = 24,
        offset: int = 0,
    ):
        """Browse the catalog by category."""
        params: Dict[str, Any] = {"limit": limit, "offset": offset}
        if course_type:
            params["course_type"] = course_type
        if cuisine:
            params["cuisine"] = cuisine
        if source:
            params["source"] = source
        if q:
            params["q"] = q
        return await cls.get("/api/v2/recipes/browse", params=params)

    @classmethod
    async def catalog_vocabulary(cls):
        """The closed value sets the UI should offer instead of free text."""
        return await cls.get("/api/v2/recipes/vocabulary")

    @classmethod
    async def catalog_get_recipe(cls, recipe_id: str):
        """Fetch one recipe document from the catalog index."""
        return await cls.get(f"/api/v2/recipes/{recipe_id}")

    @classmethod
    async def create_recipe(cls, payload: Dict[str, Any]):
        """Create a new structured recipe."""
        return await cls.post("/api/v1/recipes/", json=payload)

    @classmethod
    async def update_recipe(cls, recipe_id: str, payload: Dict[str, Any]):
        """Patch mutable recipe fields on an existing recipe."""
        return await cls.patch(f"/api/v1/recipes/{recipe_id}", json=payload)

    @classmethod
    async def disable_recipe(cls, recipe_id: str, reason: Optional[str] = None):
        """Disable (soft-delete) a single recipe."""
        return await cls.post(
            f"/api/v1/recipes/{recipe_id}/disable",
            json={"reason": reason},
        )

    @classmethod
    async def enable_recipe(cls, recipe_id: str):
        """Re-enable a previously disabled recipe."""
        return await cls.post(f"/api/v1/recipes/{recipe_id}/enable", json={})

    @classmethod
    async def bulk_disable_recipes(cls, recipe_ids: list[str], reason: Optional[str] = None):
        """Bulk disable recipes by explicit IDs."""
        return await cls.post(
            "/api/v1/recipes/disable",
            json={"recipe_ids": recipe_ids, "reason": reason},
        )

    @classmethod
    async def bulk_enable_recipes(cls, recipe_ids: list[str]):
        """Bulk re-enable recipes by explicit IDs."""
        return await cls.post(
            "/api/v1/recipes/enable",
            json={"recipe_ids": recipe_ids},
        )

    @classmethod
    async def disable_recipes_by_query(cls, payload: Dict[str, Any]):
        """Bulk disable every recipe matching param_search filters."""
        return await cls.post(
            "/api/v1/recipes/disable-by-query",
            json=payload,
            timeout=300.0,  # by-query operations can touch large ID sets
        )

    @classmethod
    async def substitute_recipe_ingredient(
        cls,
        recipe_id: str,
        ingredient: str,
        region: str = "IE",
    ):
        """Substitute an ingredient and return the updated nutrition profile."""
        payload = {
            "ingredient": ingredient,
            "region": region,
        }
        return await cls.post(f"/api/v1/recipes/{recipe_id}/substitute", json=payload)

    @classmethod
    async def adapt_suggestions(
        cls,
        recipe_id: str,
        region: str = "IE",
        mode: str = "nutrition",
        max_swaps: int = 1,
        use_llm: bool = False,
        goal_nutrients: list[str] = None,
    ):
        """Ranked ingredient-swap suggestions to improve Nutri-Score or CO2e."""
        payload = {
            "region": region,
            "mode": mode,
            "max_swaps": max_swaps,
            "use_llm": use_llm,
            "goal_nutrients": goal_nutrients or [],
        }
        return await cls.post(
            f"/api/v1/recipes/{recipe_id}/adapt/suggestions",
            json=payload,
            timeout=120.0,  # candidate search + optional LLM judge can be slow
        )

    @classmethod
    async def adapt_simulate(
        cls,
        recipe_id: str,
        region: str,
        original_ingredient: str,
        substitute_ingredient: str,
        weight_g: Optional[float] = None,
    ):
        """Simulate one specific ingredient swap and return nutrition deltas."""
        payload = {
            "region": region,
            "swap": {
                "original_ingredient": original_ingredient,
                "substitute_ingredient": substitute_ingredient,
                "weight_g": weight_g,
            },
        }
        return await cls.post(
            f"/api/v1/recipes/{recipe_id}/adapt/simulate",
            json=payload,
            timeout=120.0,
        )

RECIPEWRANGLER = RecipeWrangler.get_client()
