"""
Household management endpoints
"""

from typing import Dict, Any
from fastapi import APIRouter, Depends, Request
import kutils
import logging
from auth import auth
from exceptions import NotFoundError, AuthorizationError
from routers.generic import render
from schemas import (
    HouseholdCreate,
    HouseholdUpdate,
    HouseholdResponse,
    HouseholdDetailResponse,
    HouseholdMemberCreate,
    HouseholdMemberUpdate,
    HouseholdMemberResponse,
    HouseholdMemberProfileCreate,
    HouseholdMemberProfileResponse,
)
from api.v1.households import HOUSEHOLD
from api.v1.household_members import HOUSEHOLD_MEMBER

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/v1/households", tags=["Households Management Operations"]
)


# ========== Household Endpoints ==========


@router.post(
    "",
    dependencies=[Depends(auth())],
    summary="Create a new household",
    description="Create a new household. The authenticated user becomes the owner.",
)
@render()
async def api_create_household(
    request: Request,
    household_data: HouseholdCreate,
):
    """
    Create a new household.

    The authenticated user becomes the owner.
    Users can only own one household at a time.
    """

    # Create household using entity
    household = await HOUSEHOLD.acreate_entity(
        household_data.model_dump(), kutils.current_user(request)
    )

    return HouseholdDetailResponse(**household)


@router.get(
    "/me",
    dependencies=[Depends(auth())],
    summary="Get my household",
    description="Get the household that the authenticated user owns.",
)
@render()
async def api_get_my_household(
    request: Request,
):
    """Get the household that the authenticated user owns."""
    id = kutils.current_user(request).get("sub")
    household = await HOUSEHOLD.get_by_owner(id)
    if not household:
        raise NotFoundError(detail="User does not own a household")

    return HouseholdDetailResponse(**household)


@router.get(
    "/{household_id}",
    dependencies=[Depends(auth())],
    summary="Get household details",
    description="Get household details by ID. User must be the owner or admin.",
)
@render()
async def api_get_household(
    request: Request,
    household_id: str,
):
    """Get household details by ID. User must be the owner."""
    user = kutils.current_user(request)

    household = await HOUSEHOLD.aget_entity(household_id)

    # Check if user is the owner or admin
    if household["owner_id"] != user["sub"] and not kutils.is_admin(request):
        raise AuthorizationError(detail="You are not the owner of this household")

    return HouseholdDetailResponse(**household)


@router.patch(
    "/{household_id}",
    dependencies=[Depends(auth())],
    summary="Update household details",
    description="Update household details by ID. Only the owner or the administrator can update.",
)
@render()
async def api_patch_household(
    request: Request,
    household_id: str,
    household_data: HouseholdUpdate,
):
    """Update household details. Only the owner can update."""
    user = kutils.current_user(request)

    household = await HOUSEHOLD.aget_entity(household_id)

    # Check if user is the owner
    if household["owner_id"] != user["sub"] and not kutils.is_admin(request):
        raise AuthorizationError(detail="Only the household owner can update details")

    # Update household
    spec = household_data.model_dump(exclude_unset=True)
    updated_household = await HOUSEHOLD.patch(household_id, spec)

    return HouseholdDetailResponse(**updated_household)


@router.delete(
    "/{household_id}",
    dependencies=[Depends(auth())],
    summary="Delete a household",
    description="Delete a household by ID. Only the owner or the administrator can delete.",
)
@render()
async def api_delete_household(
    request: Request,
    household_id: str,
):
    """Delete a household. Only the owner can delete."""
    user = kutils.current_user(request)

    household = await HOUSEHOLD.aget_entity(household_id)

    # Check if user is the owner
    if household["owner_id"] != user["sub"] and not kutils.is_admin(request):
        raise AuthorizationError(
            detail="Only the household owner can delete the household"
        )

    # Delete household
    await HOUSEHOLD.delete(household_id)

    return {"message": "Household deleted successfully"}


@router.get(
    "",
    dependencies=[Depends(auth("admin"))],
    summary="List all households (admin only)",
    description="List all households in the system. Admin only.",
)
@render()
async def api_list_households(
    request: Request,
    limit: int = 100,
    offset: int = 0,
):
    """List households owned by the authenticated user."""
    user = kutils.current_user(request)

    households = await HOUSEHOLD.fetch(limit=limit, offset=offset, owner_id=user["sub"])
    return [HouseholdResponse(**h) for h in households]


# ========== Household Member Endpoints ==========


@router.post(
    "/{household_id}/members",
    dependencies=[Depends(auth())],
    summary="Add a member to a household",
    description="Add a member to the household. Only the owner or the administrator can add members.",
)
@render()
async def api_add_household_member(
    request: Request,
    household_id: str,
    member_data: HouseholdMemberCreate,
):
    """
    Add a member to the household. Only the owner can add members.
    """
    user = kutils.current_user(request)
    # Check if user is the owner
    household = await HOUSEHOLD.aget_entity(household_id)
    if household["owner_id"] != user["sub"] and not kutils.is_admin(request):
        raise AuthorizationError(detail="Only the household owner can add members")

    # Add member using the HOUSEHOLD_MEMBER entity
    spec = member_data.model_dump()
    spec["household_id"] = household_id
    member = await HOUSEHOLD_MEMBER.acreate_entity(spec, user)

    return HouseholdMemberResponse(**member)


@router.get(
    "/{household_id}/members",
    dependencies=[Depends(auth())],
    summary="List household members",
    description="List all members of a household. User must be the owner or admin.",
)
@render()
async def api_list_household_members(
    request: Request,
    household_id: str,
):
    """List all members of a household. User must be the owner or admin."""
    id = kutils.current_user(request).get("sub")

    # Check if user is the owner
    household = await HOUSEHOLD.aget_entity(household_id)
    if household["owner_id"] != id and not kutils.is_admin(request):
        raise AuthorizationError(detail="You are not the owner of this household")

    members = await HOUSEHOLD_MEMBER.fetch(household_id=household_id)
    return [HouseholdMemberResponse(**m) for m in members]


@router.get(
    "/{household_id}/members/{member_id}",
    dependencies=[Depends(auth())],
    summary="Get a household member by ID",
    description="Get a household member by ID. User must be the owner or admin.",
)
@render()
async def api_get_household_member(
    request: Request,
    household_id: str,
    member_id: str,
):
    """Get a household member by ID. User must be the owner."""
    user = kutils.current_user(request)

    # Check if user is the owner
    household = await HOUSEHOLD.aget_entity(household_id)
    if household["owner_id"] != user["sub"] and not kutils.is_admin(request):
        raise AuthorizationError(detail="You are not the owner of this household")

    member = await HOUSEHOLD_MEMBER.aget_entity(member_id)

    # Verify member belongs to this household
    if member["household_id"] != household_id:
        raise NotFoundError(detail="Member not found in this household")

    return HouseholdMemberResponse(**member)


@router.patch(
    "/{household_id}/members/{member_id}",
    dependencies=[Depends(auth())],
    summary="Update a household member",
    description="Update a household member by ID. Only the owner or the administrator can update.",
)
@render()
async def api_update_household_member(
    request: Request,
    household_id: str,
    member_id: str,
    member_data: HouseholdMemberUpdate,
    token_payload: Dict[str, Any] = Depends(auth()),
):
    """Update a household member. Only the owner can update."""
    user_id = token_payload.get("sub")

    # Check if user is the owner
    household = await HOUSEHOLD.get(household_id)
    if household["owner_id"] != user_id and not kutils.is_admin(request):
        raise AuthorizationError(detail="Only the household owner can update members")

    # Verify member belongs to this household
    member = await HOUSEHOLD_MEMBER.aget_entity(member_id)
    if member["household_id"] != household_id:
        raise NotFoundError(detail="Member not found in this household")

    # Update member
    spec = member_data.model_dump(exclude_unset=True)
    updated_member = await HOUSEHOLD_MEMBER.patch(member_id, spec)

    return HouseholdMemberResponse(**updated_member)


@router.delete(
    "/{household_id}/members/{member_id}",
    dependencies=[Depends(auth())],
    summary="Delete a household member",
    description="Delete a household member by ID. Only the owner or the administrator can delete.",
)
@render()
async def api_delete_household_member(
    request: Request,
    household_id: str,
    member_id: str,
) -> Dict[str, str]:
    """Delete a household member. Only the owner can delete."""
    user = kutils.current_user(request)

    # Check if user is the owner
    household = await HOUSEHOLD.aget_entity(household_id)
    if household["owner_id"] != user["sub"] and not kutils.is_admin(request):
        raise AuthorizationError(detail="Only the household owner can delete members")

    # Verify member belongs to this household
    member = await HOUSEHOLD_MEMBER.aget_entity(member_id)
    if member["household_id"] != household_id:
        raise NotFoundError(detail="Member not found in this household")

    # Delete member
    await HOUSEHOLD_MEMBER.delete(member_id)

    return {"message": "Member deleted successfully"}


# ========== Household Member Profile Endpoints ==========


@router.put(
    "/{household_id}/members/{member_id}/profile",
    dependencies=[Depends(auth())],
    summary="Create or update a household member's profile",
    description="Create or update a household member's profile. Only the owner or the administrator can update.",
)
@render()
async def api_update_member_profile(
    request: Request,
    household_id: str,
    member_id: str,
    profile_data: HouseholdMemberProfileCreate,
):
    """Create or update a household member's profile. Only the owner or admin can update."""
    user = kutils.current_user(request)

    # Check if user is the owner
    household = await HOUSEHOLD.aget_entity(household_id)
    if household["owner_id"] != user["sub"] and not kutils.is_admin(request):
        raise AuthorizationError(
            detail="Only the household owner can update member profiles"
        )

    # Verify member belongs to this household
    member = await HOUSEHOLD_MEMBER.aget_entity(member_id)
    if member["household_id"] != household_id:
        raise NotFoundError(detail="Member not found in this household")

    # Create/update profile
    spec = profile_data.model_dump()
    profile = await HOUSEHOLD_MEMBER.update_member_profile(member_id, spec)

    return HouseholdMemberProfileResponse(**profile)


@router.get(
    "/{household_id}/members/{member_id}/profile",
    dependencies=[Depends(auth())],
    summary="Get a household member's profile",
    description="Get a household member's profile by ID. User must be the owner or admin.",
)
@render()
async def api_get_member_profile(
    request: Request,
    household_id: str,
    member_id: str,
):
    """Get a household member's profile. User must be the owner."""
    user = kutils.current_user(request)

    # Check if user is the owner
    household = await HOUSEHOLD.aget_entity(household_id)
    if household["owner_id"] != user["sub"] and not kutils.is_admin(request):
        raise AuthorizationError(detail="You are not the owner of this household")

    # Verify member belongs to this household
    member = await HOUSEHOLD_MEMBER.aget_entity(member_id)
    if member["household_id"] != household_id:
        raise NotFoundError(detail="Member not found in this household")

    # Get profile
    profile = await HOUSEHOLD_MEMBER.get_member_profile(member_id)
    if not profile:
        raise NotFoundError(detail="Profile not found for this member")

    return HouseholdMemberProfileResponse(**profile)


@router.delete(
    "/{household_id}/members/{member_id}/profile", dependencies=[Depends(auth())], 
    summary="Delete a household member's profile",
    description="Delete a household member's profile by ID. Only the owner or the administrator can delete."
)
@render()
async def api_delete_member_profile(
    request: Request,
    household_id: str,
    member_id: str,
) -> Dict[str, str]:
    """Delete a household member's profile. Only the owner can delete."""
    user = kutils.current_user(request)

    # Check if user is the owner
    household = await HOUSEHOLD.aget_entity(household_id)
    if household["owner_id"] != user["sub"] and not kutils.is_admin(request):
        raise AuthorizationError(
            detail="Only the household owner can delete member profiles"
        )

    # Verify member belongs to this household
    member = await HOUSEHOLD_MEMBER.aget_entity(member_id)
    if member["household_id"] != household_id:
        raise NotFoundError(detail="Member not found in this household")

    # Delete profile
    await HOUSEHOLD_MEMBER.delete_member_profile(member_id)

    return {"message": "Profile deleted successfully"}
