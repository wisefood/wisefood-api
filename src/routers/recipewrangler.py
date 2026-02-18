
from fastapi import APIRouter, Request, Depends
from routers.generic import render
import logging
from auth import auth
from backend.recipewrangler import RECIPEWRANGLER
from schemas import (
    RecipeProfileRequest,
    RecipeProfileResponse,
    RecipeSearchRequest,
    RecipeParamSearchRequest,
    RecipeDetailResponse
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/recipewrangler", tags=["Recipe Wrangler Operations"])


@router.get("/status", dependencies=[Depends(auth())])
@render()
async def status(request: Request):
    return await RECIPEWRANGLER.status()


@router.get("/recipes/{recipe_id}", dependencies=[Depends(auth())])
@render()
async def get_recipe(recipe_id: str, request: Request):
    """Retrieve a recipe with full metadata by id."""
    return await RECIPEWRANGLER.get_recipe(recipe_id)


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
        max_duration_minutes=payload.max_duration_minutes,
        limit=payload.limit,
    )


@router.post("/recipes/profile", dependencies=[Depends(auth())])
@render()
async def profile_recipe(payload: RecipeProfileRequest, request: Request):
    """Run parsing + profiling pipeline on raw recipe text."""
    return await RECIPEWRANGLER.profile_recipe(raw_recipe=payload.raw_recipe)
