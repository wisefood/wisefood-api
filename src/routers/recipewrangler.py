from fastapi import APIRouter, Request, Depends, Query
from routers.generic import render
import logging
from auth import auth, _extract_roles
from backend.recipewrangler import RECIPEWRANGLER
from budget import guest_budget
from exceptions import AuthorizationError
from schemas import (
    CatalogSearchRequest,
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


# ----------------------------------------------------------------------
# Catalog passthrough — RecipeWrangler's /api/v2/recipes surface.
#
# The /recipes/* routes above read Neo4j. The annotation facets (cuisine,
# mood, flavour, food group) are Elasticsearch-owned and exist only on the
# catalog index, so no v1 route can return them however it is called — which
# is why they were unreachable from the UI entirely.
#
# `/catalog/{recipe_id}` is declared last on purpose: FastAPI matches in
# declaration order, so a path parameter registered before /facets, /browse
# or /vocabulary would swallow them and proxy "facets" as a recipe id.
# ----------------------------------------------------------------------


@router.post("/catalog/search", dependencies=[Depends(guest_budget("search"))])
@render()
async def catalog_search(
    payload: CatalogSearchRequest,
    request: Request,
    user: dict = Depends(auth()),
):
    """Search the catalog index. Returns {results, facets, total, max_result_window}."""
    # Gated exactly as include_disabled is on the v1 search paths. RecipeWrangler
    # scopes visibility from the forwarded identity as well, but refusing here
    # gives the caller a real 403 instead of silently narrowed results.
    if payload.include_inactive:
        _require_console_roles(user, "include_inactive")
    return await RECIPEWRANGLER.catalog_search(payload.model_dump())


@router.get("/catalog/facets", dependencies=[Depends(auth())])
@render()
async def catalog_facets(request: Request):
    """List every field that can be passed in `facets` on catalog search."""
    return await RECIPEWRANGLER.catalog_facets()


@router.get("/catalog/browse", dependencies=[Depends(auth())])
@render()
async def catalog_browse(
    request: Request,
    course_type: str | None = Query(
        default=None, description="e.g. desserts, main-dish, soup"
    ),
    cuisine: str | None = Query(default=None, description="e.g. italian, thai"),
    source: str | None = Query(default=None, description="Canonical source slug"),
    q: str | None = Query(default=None, description="Optional free-text query"),
    limit: int = Query(default=24, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    """Browse the catalog by category."""
    return await RECIPEWRANGLER.catalog_browse(
        course_type=course_type,
        cuisine=cuisine,
        source=source,
        q=q,
        limit=limit,
        offset=offset,
    )


@router.get("/catalog/vocabulary", dependencies=[Depends(auth())])
@render()
async def catalog_vocabulary(request: Request):
    """The closed value sets the UI should offer instead of free text."""
    return await RECIPEWRANGLER.catalog_vocabulary()


@router.get("/catalog/{recipe_id}", dependencies=[Depends(auth())])
@render()
async def catalog_get_recipe(recipe_id: str, request: Request):
    """Fetch one recipe document from the catalog index.

    Carries the annotation facets the v1 detail route cannot: `cuisines`,
    `moods`, `flavor_profiles`, `food_groups`.
    """
    return await RECIPEWRANGLER.catalog_get_recipe(recipe_id)


@router.get("/recipes/{recipe_id}")
@render()
async def get_recipe(
    recipe_id: str,
    request: Request,
    region: RecipeRegionEnum | None = Query(
        default=None,
        description="Optional nutrition region selector: IE, HU, EU, or SI.",
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
    dependencies=[Depends(guest_budget("search"))],
)
@render()
async def search_recipes(
    payload: RecipeSearchRequest,
    request: Request,
    user: dict = Depends(auth()),
):
    """Search recipes via the knowledge graph."""
    # Gated exactly as on /recipes/param_search. Withdrawn recipes are visible
    # to the console, and a text search is how an expert actually finds one —
    # leaving this ungated here would make the param_search gate decorative.
    if payload.include_disabled:
        _require_console_roles(user, "include_disabled")
    return await RECIPEWRANGLER.search_recipes(
        question=payload.question,
        exclude_allergens=payload.exclude_allergens,
        diet_tags=payload.diet_tags,
        preferred_ingredients=payload.preferred_ingredients,
        region=payload.region,
        include_disabled=payload.include_disabled,
        limit=payload.limit,
        offset=payload.offset,
        # Forwarded by name from the request model. Anything in
        # RECIPEWRANGLER._FILTER_FIELDS that the model also declares goes
        # through, so adding a facet is a schema change only.
        **{
            name: getattr(payload, name)
            for name in RECIPEWRANGLER._FILTER_FIELDS
            if getattr(payload, name, None)
        },
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
        # Same name-driven forwarding as /recipes/search, minus the fields
        # already passed by name above — repeating one would be a duplicate
        # keyword argument, not a harmless overwrite.
        **{
            name: getattr(payload, name)
            for name in RECIPEWRANGLER._FILTER_FIELDS
            if name not in {"sources", "dish_types"} and getattr(payload, name, None)
        },
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
