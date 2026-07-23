from fastapi import APIRouter, Request, Depends, Query
from routers.generic import render
import logging
from auth import auth, _extract_roles
from backend.recipewrangler import RECIPEWRANGLER
from budget import guest_budget
from exceptions import AuthorizationError
from schemas import (
    RecipeAdaptSimulateRequest,
    RecipeAdaptSimulateResponse,
    RecipeAdaptSuggestionsRequest,
    RecipeAdaptSuggestionsResponse,
    RecipeProfileRequest,
    RecipeSearchRequest,
    RecipeParamSearchRequest,
    RecipeBulkStatusRequest,
    RecipeCreateRequest,
    RecipeCreateResponse,
    RecipeDetailsBatchRequest,
    RecipeDisableByQueryRequest,
    RecipeDisableRequest,
    RecipeRegionEnum,
    RecipeStatusResponse,
    RecipeSubstituteRequest,
    RecipeSubstituteResponse,
    RecipeUpdateRequest,
    RecipeUpdateResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/recipewrangler", tags=["Recipe Wrangler Operations"])

# Roles allowed to manage recipe status and see disabled recipes.
_CONSOLE_ROLES = {"admin", "expert"}


def _require_console_roles(user: dict, capability: str) -> None:
    roles = {str(r).lower() for r in _extract_roles(user)}
    if not (_CONSOLE_ROLES & roles):
        raise AuthorizationError(
            detail=f"{capability} requires one of: {sorted(_CONSOLE_ROLES)}",
            extra={"roles": sorted(roles)},
        )


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


@router.get("/recipes/{recipe_id}")
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
    include_disabled: bool = Query(
        default=False,
        description="Console/admin only: also resolve disabled (soft-deleted) recipes.",
    ),
    user: dict = Depends(auth()),
):
    """Retrieve a recipe by id, optionally using its lightweight card representation."""
    if include_disabled:
        _require_console_roles(user, "include_disabled")
    return await RECIPEWRANGLER.get_recipe(
        recipe_id,
        region=region.value if region else None,
        slim=slim,
        include_disabled=include_disabled,
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


@router.post(
    "/recipes/search",
    dependencies=[Depends(auth()), Depends(guest_budget("search"))],
)
@render()
async def search_recipes(payload: RecipeSearchRequest, request: Request):
    """Search recipes via the knowledge graph."""
    return await RECIPEWRANGLER.search_recipes(
        question=payload.question,
        exclude_allergens=payload.exclude_allergens,
        diet_tags=payload.diet_tags,
        preferred_ingredients=payload.preferred_ingredients,
        region=payload.region,
    )


@router.post(
    "/recipes/param_search",
    dependencies=[Depends(guest_budget("search"))],
)
@render()
async def param_search_recipes(
    payload: RecipeParamSearchRequest,
    request: Request,
    user: dict = Depends(auth()),
):
    """Run deterministic parameter-based recipe search."""
    if payload.include_disabled:
        _require_console_roles(user, "include_disabled")
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
        include_disabled=payload.include_disabled,
    )


@router.post(
    "/recipes/details",
    dependencies=[Depends(auth())],
)
@render()
async def recipe_details_batch(payload: RecipeDetailsBatchRequest, request: Request):
    """Batch-resolve recipe ids to slim cards (favourites hydrate through this)."""
    return await RECIPEWRANGLER.recipe_details_batch(
        recipe_ids=payload.recipe_ids,
        region=payload.region,
    )


@router.post(
    "/recipes/profile",
    dependencies=[Depends(auth()), Depends(guest_budget("search"))],
)
@render()
async def profile_recipe(payload: RecipeProfileRequest, request: Request):
    """Run parsing + profiling pipeline on raw recipe text."""
    return await RECIPEWRANGLER.profile_recipe(
        raw_recipe=payload.raw_recipe,
        region=payload.region,
        persist_trace=payload.persist_trace,
        parse_only=payload.parse_only,
    )


@router.post(
    "/recipes/{recipe_id}/substitute",
    dependencies=[Depends(auth()), Depends(guest_budget("search"))],
)
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


@router.post(
    "/recipes/{recipe_id}/adapt/suggestions",
    dependencies=[Depends(auth()), Depends(guest_budget("search"))],
)
@render()
async def adapt_recipe_suggestions(
    recipe_id: str,
    payload: RecipeAdaptSuggestionsRequest,
    request: Request,
):
    """Ranked ingredient-swap suggestions to improve the recipe's Nutri-Score or CO2e."""
    suggestions = await RECIPEWRANGLER.adapt_suggestions(
        recipe_id=recipe_id,
        region=payload.region.value,
        mode=payload.mode.value,
        max_swaps=payload.max_swaps,
        use_llm=payload.use_llm,
        goal_nutrients=payload.goal_nutrients,
    )
    return RecipeAdaptSuggestionsResponse(**suggestions)


@router.post(
    "/recipes/{recipe_id}/adapt/simulate",
    dependencies=[Depends(auth()), Depends(guest_budget("search"))],
)
@render()
async def adapt_recipe_simulate(
    recipe_id: str,
    payload: RecipeAdaptSimulateRequest,
    request: Request,
):
    """Simulate one specific ingredient swap and return the nutrition deltas."""
    simulated = await RECIPEWRANGLER.adapt_simulate(
        recipe_id=recipe_id,
        region=payload.region.value,
        original_ingredient=payload.swap.original_ingredient,
        substitute_ingredient=payload.swap.substitute_ingredient,
        weight_g=payload.swap.weight_g,
    )
    return RecipeAdaptSimulateResponse(**simulated)


# ---------------------------------------------------------------------------
# Recipe soft-delete (disable/enable) — console/admin operations
# ---------------------------------------------------------------------------

@router.post(
    "/recipes/disable",
    dependencies=[Depends(auth("admin,expert"))],
)
@render()
async def bulk_disable_recipes(payload: RecipeBulkStatusRequest, request: Request):
    """Bulk disable (soft-delete) recipes by explicit IDs. Reversible."""
    result = await RECIPEWRANGLER.bulk_disable_recipes(
        payload.recipe_ids, reason=payload.reason
    )
    return RecipeStatusResponse(**result)


@router.post(
    "/recipes/enable",
    dependencies=[Depends(auth("admin,expert"))],
)
@render()
async def bulk_enable_recipes(payload: RecipeBulkStatusRequest, request: Request):
    """Bulk re-enable previously disabled recipes by explicit IDs."""
    result = await RECIPEWRANGLER.bulk_enable_recipes(payload.recipe_ids)
    return RecipeStatusResponse(**result)


@router.post(
    "/recipes/disable-by-query",
    dependencies=[Depends(auth("admin,expert"))],
)
@render()
async def disable_recipes_by_query(payload: RecipeDisableByQueryRequest, request: Request):
    """Bulk disable every recipe matching param_search filters.

    Refuses an unconstrained query unless allow_unfiltered is set.
    """
    result = await RECIPEWRANGLER.disable_recipes_by_query(
        payload.model_dump(exclude_none=True)
    )
    return RecipeStatusResponse(**result)


@router.post(
    "/recipes/{recipe_id}/disable",
    dependencies=[Depends(auth("admin,expert"))],
)
@render()
async def disable_recipe(
    recipe_id: str,
    request: Request,
    payload: RecipeDisableRequest | None = None,
):
    """Disable (soft-delete) a single recipe so it is never served anywhere."""
    result = await RECIPEWRANGLER.disable_recipe(
        recipe_id, reason=payload.reason if payload else None
    )
    return RecipeStatusResponse(**result)


@router.post(
    "/recipes/{recipe_id}/enable",
    dependencies=[Depends(auth("admin,expert"))],
)
@render()
async def enable_recipe(recipe_id: str, request: Request):
    """Re-enable a previously disabled recipe."""
    result = await RECIPEWRANGLER.enable_recipe(recipe_id)
    return RecipeStatusResponse(**result)
