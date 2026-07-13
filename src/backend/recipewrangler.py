import httpx
from typing import Any, Dict, Optional
from main import config
import logging

logger = logging.getLogger(__name__)


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

    @classmethod
    async def get(
        cls, endpoint: str, params: Optional[Dict[str, Any]] = None, **kwargs
    ):
        if cls._client is None:
            raise RuntimeError(
                "RecipeWrangler client not initialized. Call get_client() first."
            )
        response = await cls._client.get(endpoint, params=params, **kwargs)
        response.raise_for_status()
        return response.json()

    @classmethod
    async def post(cls, endpoint: str, data: Any = None, json: Any = None, **kwargs):
        if cls._client is None:
            raise RuntimeError(
                "RecipeWrangler client not initialized. Call get_client() first."
            )
        response = await cls._client.post(endpoint, data=data, json=json, **kwargs)
        response.raise_for_status()
        return response.json()

    @classmethod
    async def put(cls, endpoint: str, data: Any = None, json: Any = None, **kwargs):
        if cls._client is None:
            raise RuntimeError(
                "RecipeWrangler client not initialized. Call get_client() first."
            )
        response = await cls._client.put(endpoint, data=data, json=json, **kwargs)
        response.raise_for_status()
        return response.json()

    @classmethod
    async def patch(cls, endpoint: str, data: Any = None, json: Any = None, **kwargs):
        if cls._client is None:
            raise RuntimeError(
                "RecipeWrangler client not initialized. Call get_client() first."
            )
        response = await cls._client.patch(endpoint, data=data, json=json, **kwargs)
        response.raise_for_status()
        return response.json()

    @classmethod
    async def delete(cls, endpoint: str, **kwargs):
        if cls._client is None:
            raise RuntimeError(
                "RecipeWrangler client not initialized. Call get_client() first."
            )
        response = await cls._client.delete(endpoint, **kwargs)
        response.raise_for_status()
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

    @classmethod
    async def search_recipes(cls, question: str, exclude_allergens: list[str] = None):
        """Search recipes via the knowledge graph."""
        payload = {
            "question": question,
            "exclude_allergens": exclude_allergens or []
        }
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
    ):
        """Ranked ingredient-swap suggestions to improve Nutri-Score or CO2e."""
        payload = {
            "region": region,
            "mode": mode,
            "max_swaps": max_swaps,
            "use_llm": use_llm,
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
