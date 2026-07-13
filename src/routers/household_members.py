"""
Household Member management endpoints (independent entity)
"""

from typing import Dict, Any
from fastapi import APIRouter, Depends, Path, Request
import kutils
import logging
from auth import auth
from exceptions import NotFoundError, AuthorizationError
from routers.generic import render
from schemas import (
    HouseholdMemberCreateWithHousehold,
    HouseholdMemberUpdate,
    HouseholdMemberResponse,
    HouseholdMemberProfileCreate,
    HouseholdMemberProfileUpdate,
    HouseholdMemberProfileResponse,
    MemberAdaptedRecipeDeleteResponse,
    MemberAdaptedRecipeResponse,
    MemberAdaptedRecipeStoreRequest,
    MemberFavoriteResponse,
    MemberFavoriteDeleteResponse,
)
from api.v1.household_members import HOUSEHOLD_MEMBER
from routers.households import verify_access

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/v1/members", tags=["Household Members Management Operations"]
)

# ========== Household Member Endpoints ==========


@router.post(
    "",
    dependencies=[Depends(auth())],
    summary="Create a new household member",
    description="Create a new household member. User must be the household owner or admin.",
)
@render()
async def api_create_member(
    request: Request,
    member_data: HouseholdMemberCreateWithHousehold,
):
    """
    Create a new household member.
    User must be the owner of the household or an admin.
    """
    # Verify access to household
    await verify_access(request, member_data.household_id)

    user = kutils.current_user(request)

    # Create member
    spec = member_data.model_dump()
    member = await HOUSEHOLD_MEMBER.acreate_entity(spec, user)

    return HouseholdMemberResponse(**member)


@router.get(
    "/{member_id}",
    dependencies=[Depends(auth())],
    summary="Get household member details",
    description="Get household member details by ID. User must be the household owner or admin.",
)
@render()
async def api_get_member(
    request: Request,
    member_id: str,
):
    """Get household member details by ID. User must have access to the household."""
    member, _ = await verify_access(request, None, member_id)

    return HouseholdMemberResponse(**member)


@router.get(
    "",
    dependencies=[Depends(auth())],
    summary="List household members",
    description="List household members filtered by household_id. User must be the household owner or admin.",
)
@render()
async def api_list_members(
    request: Request,
    household_id: str,
    limit: int = 100,
    offset: int = 0,
):
    """List all members of a household. User must have access to the household."""

    await verify_access(request, household_id)

    user = kutils.current_user(request)

    members = await HOUSEHOLD_MEMBER.fetch(
        limit=limit, offset=offset, household_id=household_id
    )
    return [HouseholdMemberResponse(**m) for m in members]


@router.patch(
    "/{member_id}",
    dependencies=[Depends(auth())],
    summary="Update household member details",
    description="Update household member details by ID. User must be the household owner or admin.",
)
@render()
async def api_patch_member(
    request: Request,
    member_id: str,
    member_data: HouseholdMemberUpdate,
):
    """Update household member details. User must have access to the household."""
    await verify_access(request, None, member_id)

    # Update member
    spec = member_data.model_dump(exclude_unset=True)
    updated_member = await HOUSEHOLD_MEMBER.patch(member_id, spec)

    return HouseholdMemberResponse(**updated_member)


@router.delete(
    "/{member_id}",
    dependencies=[Depends(auth())],
    summary="Delete a household member",
    description="Delete a household member by ID. User must be the household owner or admin.",
)
@render()
async def api_delete_member(
    request: Request,
    member_id: str,
):
    """Delete a household member. User must have access to the household."""
    await verify_access(request, None, member_id)

    # Delete member
    await HOUSEHOLD_MEMBER.delete(member_id)

    return {"message": "Member deleted successfully"}


# ========== Household Member Profile Endpoints ==========


@router.post(
    "/{member_id}/profile",
    dependencies=[Depends(auth())],
    summary="Create a household member's profile",
    description="Create a household member's profile. User must be the household owner or admin.",
)
@render()
async def api_create_member_profile(
    request: Request,
    member_id: str,
    profile_data: HouseholdMemberProfileCreate,
):
    """Create a household member's profile. User must have access."""
    await verify_access(request, None, member_id)

    # Create profile
    spec = profile_data.model_dump(exclude_unset=True)
    profile = await HOUSEHOLD_MEMBER.create_member_profile(member_id, spec)

    return HouseholdMemberProfileResponse(**profile)


@router.patch(
    "/{member_id}/profile",
    dependencies=[Depends(auth())],
    summary="Update a household member's profile",
    description="Update a household member's profile. User must be the household owner or admin.",
)
@render()
async def api_patch_member_profile(
    request: Request,
    member_id: str,
    profile_data: HouseholdMemberProfileUpdate,
):
    """Create or update a household member's profile. User must have access."""
    await verify_access(request, None, member_id)

    # Create/update profile
    spec = profile_data.model_dump(exclude_unset=True)
    profile = await HOUSEHOLD_MEMBER.update_member_profile(member_id, spec)

    return HouseholdMemberProfileResponse(**profile)


@router.get(
    "/{member_id}/profile",
    dependencies=[Depends(auth())],
    summary="Get a household member's profile",
    description="Get a household member's profile by ID. User must be the household owner or admin.",
)
@render()
async def api_get_member_profile(
    request: Request,
    member_id: str,
):
    """Get a household member's profile. User must have access."""
    await verify_access(request, None, member_id)

    # Get profile
    profile = await HOUSEHOLD_MEMBER.get_member_profile(member_id)
    if not profile:
        raise NotFoundError(detail="Profile not found for this member")

    return HouseholdMemberProfileResponse(**profile)


@router.delete(
    "/{member_id}/profile",
    dependencies=[Depends(auth())],
    summary="Delete a household member's profile",
    description="Delete a household member's profile by ID. User must be the household owner or admin.",
)
@render()
async def api_delete_member_profile(
    request: Request,
    member_id: str,
):
    """Delete a household member's profile. User must have access."""
    await verify_access(request, None, member_id)

    # Delete profile
    await HOUSEHOLD_MEMBER.delete_member_profile(member_id)

    return {"message": "Profile deleted successfully"}


# ========== Household Member Favorites Endpoints ==========


@router.get(
    "/{member_id}/favorites",
    dependencies=[Depends(auth())],
    summary="List a member's favorite recipes",
    description="List a household member's favorite recipes, newest first. User must be the household owner or admin.",
)
@render()
async def api_list_member_favorites(
    request: Request,
    member_id: str,
):
    """List a member's favorite recipes, newest first. User must have access."""
    await verify_access(request, None, member_id)

    favorites = await HOUSEHOLD_MEMBER.list_favorites(member_id)
    return [MemberFavoriteResponse(**f) for f in favorites]


@router.put(
    "/{member_id}/favorites/{recipe_id}",
    dependencies=[Depends(auth())],
    summary="Add a recipe to a member's favorites",
    description="Idempotently add a recipe to a member's favorites. Re-adding returns the existing favorite. User must be the household owner or admin.",
)
@render()
async def api_add_member_favorite(
    request: Request,
    member_id: str,
    recipe_id: str = Path(..., min_length=1, max_length=128, description="Opaque RecipeWrangler recipe id"),
):
    """Add a recipe to a member's favorites (idempotent). User must have access."""
    await verify_access(request, None, member_id)

    favorite = await HOUSEHOLD_MEMBER.add_favorite(member_id, recipe_id)

    return MemberFavoriteResponse(**favorite)


@router.delete(
    "/{member_id}/favorites/{recipe_id}",
    dependencies=[Depends(auth())],
    summary="Remove a recipe from a member's favorites",
    description="Idempotently remove a recipe from a member's favorites. User must be the household owner or admin.",
)
@render()
async def api_delete_member_favorite(
    request: Request,
    member_id: str,
    recipe_id: str = Path(..., min_length=1, max_length=128, description="Opaque RecipeWrangler recipe id"),
):
    """Remove a recipe from a member's favorites (idempotent). User must have access."""
    await verify_access(request, None, member_id)

    deleted = await HOUSEHOLD_MEMBER.remove_favorite(member_id, recipe_id)

    return MemberFavoriteDeleteResponse(deleted=deleted)


# ========== Household Member Adapted Recipes Endpoints ==========
# Strictly owner-scoped: verify_access grants only the household owner
# (or admin/agent service callers such as FoodChat).


@router.get(
    "/{member_id}/adapted-recipes",
    dependencies=[Depends(auth())],
    summary="List a member's adapted recipes",
    description="List a household member's saved adapted recipes, most recently updated first. User must be the household owner or admin.",
)
@render()
async def api_list_member_adapted_recipes(
    request: Request,
    member_id: str,
):
    """List a member's adapted recipes. User must have access."""
    await verify_access(request, None, member_id)

    adapted = await HOUSEHOLD_MEMBER.list_adapted_recipes(member_id)
    return [MemberAdaptedRecipeResponse(**a) for a in adapted]


@router.get(
    "/{member_id}/adapted-recipes/{recipe_id}",
    dependencies=[Depends(auth())],
    summary="Get a member's adaptation of one recipe",
    description="Get a household member's saved adaptation of a specific recipe. 404 if the member has not saved one. User must be the household owner or admin.",
)
@render()
async def api_get_member_adapted_recipe(
    request: Request,
    member_id: str,
    recipe_id: str = Path(..., min_length=1, max_length=128, description="Original opaque RecipeWrangler recipe id"),
):
    """Get a member's adaptation of one recipe. User must have access."""
    await verify_access(request, None, member_id)

    adapted = await HOUSEHOLD_MEMBER.get_adapted_recipe(member_id, recipe_id)
    if adapted is None:
        raise NotFoundError(detail=f"No adapted recipe saved for '{recipe_id}'")
    return MemberAdaptedRecipeResponse(**adapted)


@router.put(
    "/{member_id}/adapted-recipes/{recipe_id}",
    dependencies=[Depends(auth())],
    summary="Save a member's adapted version of a recipe",
    description="Save (or replace) a household member's adapted version of a recipe. One adaptation per (member, recipe). User must be the household owner or admin.",
)
@render()
async def api_save_member_adapted_recipe(
    request: Request,
    member_id: str,
    payload: MemberAdaptedRecipeStoreRequest,
    recipe_id: str = Path(..., min_length=1, max_length=128, description="Original opaque RecipeWrangler recipe id"),
):
    """Save (upsert) a member's adapted version of a recipe. User must have access."""
    await verify_access(request, None, member_id)

    adapted = await HOUSEHOLD_MEMBER.upsert_adapted_recipe(
        member_id, recipe_id, payload.title, payload.payload
    )
    return MemberAdaptedRecipeResponse(**adapted)


@router.delete(
    "/{member_id}/adapted-recipes/{recipe_id}",
    dependencies=[Depends(auth())],
    summary="Remove a member's adaptation of a recipe",
    description="Idempotently remove a household member's saved adaptation of a recipe. User must be the household owner or admin.",
)
@render()
async def api_delete_member_adapted_recipe(
    request: Request,
    member_id: str,
    recipe_id: str = Path(..., min_length=1, max_length=128, description="Original opaque RecipeWrangler recipe id"),
):
    """Remove a member's adaptation of a recipe (idempotent). User must have access."""
    await verify_access(request, None, member_id)

    deleted = await HOUSEHOLD_MEMBER.remove_adapted_recipe(member_id, recipe_id)

    return MemberAdaptedRecipeDeleteResponse(deleted=deleted)
