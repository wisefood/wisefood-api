
from fastapi import APIRouter, Request, Depends
from routers.generic import render
import logging
from auth import auth
from backend.recipewrangler import RECIPEWRANGLER
from schemas import (
    RecipeProfileRequest,
    RecipeProfileResponse,
    RecipeSearchRequest,
    RecipeDetailResponse
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/recipewrangler", tags=["Recipe Wrangler Operations"])


@router.get("/status", dependencies=[Depends(auth())])
@render()
async def status(request: Request):
    return await RECIPEWRANGLER.status()


@router.get("/recipes/{recipe_id}", dependencies=[Depends(auth())], response_model=RecipeDetailResponse)
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


@router.post("/recipes/profile", dependencies=[Depends(auth())], response_model=RecipeProfileResponse)
@render()
async def profile_recipe(payload: RecipeProfileRequest, request: Request):
    """Run parsing + profiling pipeline on raw recipe text."""
    return await RECIPEWRANGLER.profile_recipe(raw_recipe=payload.raw_recipe)
