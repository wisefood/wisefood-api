import uuid
import httpx
from typing import Any, Dict, Optional
from main import config
from api.v1.households import HOUSEHOLD
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
    async def get_recipe(cls, recipe_id: str):
        """Retrieve a recipe with full metadata by id."""
        return await cls.get(f"/api/v1/recipes/{recipe_id}")

    @classmethod
    async def search_recipes(cls, question: str, exclude_allergens: list[str] = None):
        """Search recipes via the knowledge graph."""
        payload = {
            "question": question,
            "exclude_allergens": exclude_allergens or []
        }
        return await cls.post("/api/v1/recipes/search", json=payload)

    @classmethod
    async def profile_recipe(cls, raw_recipe: str):
        """Run parsing + profiling pipeline on raw recipe text."""
        payload = {"raw_recipe": raw_recipe}
        return await cls.post("/api/v1/recipes/profile", json=payload)

RECIPEWRANGLER = RecipeWrangler.get_client()
