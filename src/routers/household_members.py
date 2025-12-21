"""
Household Member management endpoints (independent entity)
"""

from typing import Dict, Any
from fastapi import APIRouter, Depends, Request
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
    HouseholdMemberProfileResponse,
)
from api.v1.household_members import HOUSEHOLD_MEMBER
from api.v1.households import HOUSEHOLD

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/v1/members", tags=["Household Members Management Operations"]
)


# ========== Helper Functions ==========


async def verify_member_access(request: Request, member_id: str) -> Dict[str, Any]:
    """
    Verify that the user has access to the member.
    Returns the member if access is granted, raises AuthorizationError otherwise.
    """
    user = kutils.current_user(request)
    member = await HOUSEHOLD_MEMBER.aget_entity(member_id)

    # Get the household to check ownership
    household = await HOUSEHOLD.aget_entity(member["household_id"])

    # Check if user is the owner or admin
    if household["owner_id"] != user["sub"] and not kutils.is_admin(request):
        raise AuthorizationError(detail="You do not have access to this member")

    return member


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
    user = kutils.current_user(request)

    # Verify user owns the household
    household = await HOUSEHOLD.aget_entity(member_data.household_id)
    if household["owner_id"] != user["sub"] and not kutils.is_admin(request):
        raise AuthorizationError(detail="Only the household owner can add members")

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
    member = await verify_member_access(request, member_id)
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
    user = kutils.current_user(request)

    # Verify user owns the household
    household = await HOUSEHOLD.aget_entity(household_id)
    if household["owner_id"] != user["sub"] and not kutils.is_admin(request):
        raise AuthorizationError(detail="You do not have access to this household")

    members = await HOUSEHOLD_MEMBER.fetch(limit=limit, offset=offset, household_id=household_id)
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
    await verify_member_access(request, member_id)

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
    await verify_member_access(request, member_id)

    # Delete member
    await HOUSEHOLD_MEMBER.delete(member_id)

    return {"message": "Member deleted successfully"}


# ========== Household Member Profile Endpoints ==========


@router.put(
    "/{member_id}/profile",
    dependencies=[Depends(auth())],
    summary="Create or update a household member's profile",
    description="Create or update a household member's profile. User must be the household owner or admin.",
)
@render()
async def api_update_member_profile(
    request: Request,
    member_id: str,
    profile_data: HouseholdMemberProfileCreate,
):
    """Create or update a household member's profile. User must have access."""
    await verify_member_access(request, member_id)

    # Create/update profile
    spec = profile_data.model_dump()
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
    await verify_member_access(request, member_id)

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
    await verify_member_access(request, member_id)

    # Delete profile
    await HOUSEHOLD_MEMBER.delete_member_profile(member_id)

    return {"message": "Profile deleted successfully"}
