from fastapi import APIRouter, Request, Depends, Query
from routers.generic import render
import logging
from auth import auth
from backend.recipewrangler import RECIPEWRANGLER
from schemas import (
    RecipeProfileRequest,
    RecipeSearchRequest,
    RecipeParamSearchRequest,
    RecipeCreateRequest,
    RecipeCreateResponse,
    RecipeRegionEnum,
    RecipeSubstituteRequest,
    RecipeSubstituteResponse,
    RecipeUpdateRequest,
    RecipeUpdateResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/recipewrangler", tags=["Recipe Wrangler Operations"])


@router.get("/status", dependencies=[Depends(auth())])
@render()
async def status(request: Request):
    return await RECIPEWRANGLER.status()


@router.post("/recipes/", dependencies=[Depends(auth("admin,expert"))])
@render()
async def create_recipe(payload: RecipeCreateRequest, request: Request):
    """Create a new structured recipe. Admin and expert roles only."""
    created = await RECIPEWRANGLER.create_recipe(
        payload.model_dump(exclude_none=True)
    )
    return RecipeCreateResponse(**created)


@router.get("/recipes/autocomplete", dependencies=[Depends(auth())])
@render()
async def autocomplete_recipes(
    request: Request,
    q: str = Query(
        default="",
        min_length=0,
        max_length=120,
        description="Query string used to autocomplete recipe titles",
    ),
    limit: int = Query(
        default=8,
        ge=1,
        le=20,
        description="Maximum number of autocomplete suggestions to return",
    ),
):
    """Autocomplete recipe titles from Elasticsearch."""
    return await RECIPEWRANGLER.autocomplete_recipes(q=q, limit=limit)


@router.get("/recipes/count", dependencies=[Depends(auth())])
@render()
async def get_recipe_count(request: Request):
    """Return the total number of recipes in the graph."""
    return await RECIPEWRANGLER.count_recipes()


@router.get("/recipes/{recipe_id}", dependencies=[Depends(auth())])
@render()
async def get_recipe(
    recipe_id: str,
    request: Request,
    region: RecipeRegionEnum | None = Query(
        default=None,
        description="Optional nutrition region selector: US, IE, or HU.",
    ),
    slim: bool = Query(
        default=False,
        description="When true, return only card-level fields with no nutrition data.",
    ),
):
    """Retrieve a recipe by id, optionally using its lightweight card representation."""
    return await RECIPEWRANGLER.get_recipe(
        recipe_id,
        region=region.value if region else None,
        slim=slim,
    )


@router.patch("/recipes/{recipe_id}", dependencies=[Depends(auth("admin,expert"))])
@render()
async def update_recipe(
    recipe_id: str,
    payload: RecipeUpdateRequest,
    request: Request,
):
    """Patch mutable recipe fields on an existing recipe. Admin and expert roles only."""
    updated = await RECIPEWRANGLER.update_recipe(
        recipe_id,
        payload.model_dump(exclude_none=True),
    )
    return RecipeUpdateResponse(**updated)


@router.post("/recipes/search", dependencies=[Depends(auth())])
@render()
async def search_recipes(payload: RecipeSearchRequest, request: Request):
    """Search recipes via the knowledge graph."""
    return await RECIPEWRANGLER.search_recipes(
        question=payload.question,
        exclude_allergens=payload.exclude_allergens
    )


@router.post("/recipes/param_search", dependencies=[Depends(auth())])
@render()
async def param_search_recipes(payload: RecipeParamSearchRequest, request: Request):
    """Run deterministic parameter-based recipe search."""
    return await RECIPEWRANGLER.param_search_recipes(
        include_ingredients=payload.include_ingredients,
        exclude_ingredients=payload.exclude_ingredients,
        exclude_allergens=payload.exclude_allergens,
        diet_tags=payload.diet_tags,
        sources=payload.sources,
        dish_types=payload.dish_types,
        max_duration_minutes=payload.max_duration_minutes,
        limit=payload.limit,
        offset=payload.offset,
        sort_by=payload.sort_by,
        include_facets=payload.include_facets,
    )


@router.post("/recipes/profile", dependencies=[Depends(auth())])
@render()
async def profile_recipe(payload: RecipeProfileRequest, request: Request):
    """Run parsing + profiling pipeline on raw recipe text."""
    return await RECIPEWRANGLER.profile_recipe(
        raw_recipe=payload.raw_recipe,
        region=payload.region,
        persist_trace=payload.persist_trace,
        parse_only=payload.parse_only,
    )


@router.post("/recipes/{recipe_id}/substitute", dependencies=[Depends(auth())])
@render()
async def substitute_recipe_ingredient(
    recipe_id: str,
    payload: RecipeSubstituteRequest,
    request: Request,
):
    """Substitute an ingredient and return the updated nutrition profile."""
    substituted = await RECIPEWRANGLER.substitute_recipe_ingredient(
        recipe_id=recipe_id,
        ingredient=payload.ingredient,
        region=payload.region.value,
    )
    return RecipeSubstituteResponse(**substituted)
