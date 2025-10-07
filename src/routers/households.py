"""
Household management endpoints
"""

from typing import Dict, Any, List
from sqlalchemy import text
from fastapi import APIRouter, Depends, Request
import kutils
import logging
import asyncio
from auth import auth
from exceptions import NotFoundError, AuthorizationError, ConflictError
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
    HouseholdMemberProfileUpdate,
    HouseholdMemberProfileResponse,
)
from api.v1.households import HOUSEHOLD

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/v1/households", tags=["Households Management Operations"]
)


# ========== Household Endpoints ==========


@router.post("", dependencies=[Depends(auth())], response_model=HouseholdDetailResponse)
@render()
async def api_create_household(
    request: Request,
    household_data: HouseholdCreate,
) -> HouseholdDetailResponse:
    """
    Create a new household.

    The authenticated user becomes the owner.
    Users can only own one household at a time.
    """

    # Create household using entity
    household = await HOUSEHOLD.create(
        household_data.model_dump(), kutils.current_user(request)
    )

    return HouseholdDetailResponse(**household)


@router.get(
    "/me", dependencies=[Depends(auth())], response_model=HouseholdDetailResponse
)
@render()
async def api_get_my_household(
    request: Request,
) -> HouseholdDetailResponse:
    """Get the household that the authenticated user owns."""
    id = kutils.current_user(request).get("sub")
    household = await HOUSEHOLD.get_by_owner(id)
    if not household:
        raise NotFoundError(detail="User does not own a household")

    return HouseholdDetailResponse(household)


@router.get("/{household_id}", response_model=HouseholdDetailResponse)
@render()
async def api_get_household(
    request: Request,
    household_id: str,
    token_payload: Dict[str, Any] = Depends(auth()),
) -> HouseholdDetailResponse:
    """Get household details by ID. User must be the owner."""
    user_id = token_payload.get("sub")

    household = await HOUSEHOLD.get(household_id)

    # Check if user is the owner
    if household["owner_id"] != user_id:
        raise AuthorizationError(detail="You are not the owner of this household")

    return HouseholdDetailResponse(**household)


@router.patch("/{household_id}", response_model=HouseholdDetailResponse)
@render()
async def api_patch_household(
    request: Request,
    household_id: str,
    household_data: HouseholdUpdate,
    token_payload: Dict[str, Any] = Depends(auth()),
) -> HouseholdDetailResponse:
    """Update household details. Only the owner can update."""
    user_id = token_payload.get("sub")

    household = await HOUSEHOLD.get(household_id)

    # Check if user is the owner
    if household["owner_id"] != user_id:
        raise AuthorizationError(detail="Only the household owner can update details")

    # Update household
    spec = household_data.model_dump(exclude_unset=True)
    updated_household = await HOUSEHOLD.patch(household_id, spec)

    return HouseholdDetailResponse(**updated_household)


@router.delete("/{household_id}")
@render()
async def api_delete_household(
    request: Request,
    household_id: str,
    token_payload: Dict[str, Any] = Depends(auth()),
) -> Dict[str, str]:
    """Delete a household. Only the owner can delete."""
    user_id = token_payload.get("sub")

    household = await HOUSEHOLD.get(household_id)

    # Check if user is the owner
    if household["owner_id"] != user_id:
        raise AuthorizationError(
            detail="Only the household owner can delete the household"
        )

    # Delete household
    await HOUSEHOLD.delete(household_id)

    return {"message": "Household deleted successfully"}


@router.get("", response_model=List[HouseholdResponse])
@render()
async def api_list_households(
    request: Request,
    token_payload: Dict[str, Any] = Depends(auth()),
    limit: int = 100,
    offset: int = 0,
) -> List[HouseholdResponse]:
    """List households owned by the authenticated user."""
    user_id = token_payload.get("sub")

    households = await HOUSEHOLD.fetch(limit=limit, offset=offset, owner_id=user_id)
    return [HouseholdResponse(**h) for h in households]


# ========== Household Member Endpoints ==========


@router.post("/{household_id}/members", response_model=HouseholdMemberResponse)
@render()
async def api_add_household_member(
    request: Request,
    household_id: str,
    member_data: HouseholdMemberCreate,
    token_payload: Dict[str, Any] = Depends(auth()),
) -> HouseholdMemberResponse:
    """
    Add a member to the household. Only the owner can add members.
    """
    user_id = token_payload.get("sub")

    # Check if user is the owner
    household = await HOUSEHOLD.get(household_id)
    if household["owner_id"] != user_id:
        raise AuthorizationError(detail="Only the household owner can add members")

    # Add member
    spec = member_data.model_dump()
    member = await HOUSEHOLD.add_member(household_id, spec)

    return HouseholdMemberResponse(**member)


@router.get("/{household_id}/members", response_model=List[HouseholdMemberResponse])
@render()
async def api_list_household_members(
    request: Request,
    household_id: str,
    token_payload: Dict[str, Any] = Depends(auth()),
) -> List[HouseholdMemberResponse]:
    """List all members of a household. User must be the owner."""
    user_id = token_payload.get("sub")

    # Check if user is the owner
    household = await HOUSEHOLD.get(household_id)
    if household["owner_id"] != user_id:
        raise AuthorizationError(detail="You are not the owner of this household")

    members = await HOUSEHOLD.list_members(household_id)
    return [HouseholdMemberResponse(**m) for m in members]


@router.get(
    "/{household_id}/members/{member_id}", response_model=HouseholdMemberResponse
)
@render()
async def api_get_household_member(
    request: Request,
    household_id: str,
    member_id: str,
    token_payload: Dict[str, Any] = Depends(auth()),
) -> HouseholdMemberResponse:
    """Get a household member by ID. User must be the owner."""
    user_id = token_payload.get("sub")

    # Check if user is the owner
    household = await HOUSEHOLD.get(household_id)
    if household["owner_id"] != user_id:
        raise AuthorizationError(detail="You are not the owner of this household")

    member = await HOUSEHOLD.get_member(member_id)

    # Verify member belongs to this household
    if member["household_id"] != household_id:
        raise NotFoundError(detail="Member not found in this household")

    return HouseholdMemberResponse(**member)


@router.patch(
    "/{household_id}/members/{member_id}", response_model=HouseholdMemberResponse
)
@render()
async def api_update_household_member(
    request: Request,
    household_id: str,
    member_id: str,
    member_data: HouseholdMemberUpdate,
    token_payload: Dict[str, Any] = Depends(auth()),
) -> HouseholdMemberResponse:
    """Update a household member. Only the owner can update."""
    user_id = token_payload.get("sub")

    # Check if user is the owner
    household = await HOUSEHOLD.get(household_id)
    if household["owner_id"] != user_id:
        raise AuthorizationError(detail="Only the household owner can update members")

    # Verify member belongs to this household
    member = await HOUSEHOLD.get_member(member_id)
    if member["household_id"] != household_id:
        raise NotFoundError(detail="Member not found in this household")

    # Update member
    spec = member_data.model_dump(exclude_unset=True)
    updated_member = await HOUSEHOLD.update_member(member_id, spec)

    return HouseholdMemberResponse(**updated_member)


@router.delete("/{household_id}/members/{member_id}")
@render()
async def api_delete_household_member(
    request: Request,
    household_id: str,
    member_id: str,
    token_payload: Dict[str, Any] = Depends(auth()),
) -> Dict[str, str]:
    """Delete a household member. Only the owner can delete."""
    user_id = token_payload.get("sub")

    # Check if user is the owner
    household = await HOUSEHOLD.get(household_id)
    if household["owner_id"] != user_id:
        raise AuthorizationError(detail="Only the household owner can delete members")

    # Verify member belongs to this household
    member = await HOUSEHOLD.get_member(member_id)
    if member["household_id"] != household_id:
        raise NotFoundError(detail="Member not found in this household")

    # Delete member
    await HOUSEHOLD.remove_member(member_id)

    return {"message": "Member deleted successfully"}


# ========== Household Member Profile Endpoints ==========


@router.put(
    "/{household_id}/members/{member_id}/profile",
    response_model=HouseholdMemberProfileResponse,
)
@render()
async def api_update_member_profile(
    request: Request,
    household_id: str,
    member_id: str,
    profile_data: HouseholdMemberProfileCreate,
    token_payload: Dict[str, Any] = Depends(auth()),
) -> HouseholdMemberProfileResponse:
    """Create or update a household member's profile. Only the owner can update."""
    user_id = token_payload.get("sub")

    # Check if user is the owner
    household = await HOUSEHOLD.get(household_id)
    if household["owner_id"] != user_id:
        raise AuthorizationError(
            detail="Only the household owner can update member profiles"
        )

    # Verify member belongs to this household
    member = await HOUSEHOLD.get_member(member_id)
    if member["household_id"] != household_id:
        raise NotFoundError(detail="Member not found in this household")

    # Create/update profile
    spec = profile_data.model_dump()
    profile = await HOUSEHOLD.update_member_profile(member_id, spec)

    return HouseholdMemberProfileResponse(**profile)


@router.get(
    "/{household_id}/members/{member_id}/profile",
    response_model=HouseholdMemberProfileResponse,
)
@render()
async def api_get_member_profile(
    request: Request,
    household_id: str,
    member_id: str,
    token_payload: Dict[str, Any] = Depends(auth()),
) -> HouseholdMemberProfileResponse:
    """Get a household member's profile. User must be the owner."""
    user_id = token_payload.get("sub")

    # Check if user is the owner
    household = await HOUSEHOLD.get(household_id)
    if household["owner_id"] != user_id:
        raise AuthorizationError(detail="You are not the owner of this household")

    # Verify member belongs to this household
    member = await HOUSEHOLD.get_member(member_id)
    if member["household_id"] != household_id:
        raise NotFoundError(detail="Member not found in this household")

    # Get profile
    profile = await HOUSEHOLD.get_member_profile(member_id)
    if not profile:
        raise NotFoundError(detail="Profile not found for this member")

    return HouseholdMemberProfileResponse(**profile)


@router.delete("/{household_id}/members/{member_id}/profile")
@render()
async def api_delete_member_profile(
    request: Request,
    household_id: str,
    member_id: str,
    token_payload: Dict[str, Any] = Depends(auth()),
) -> Dict[str, str]:
    """Delete a household member's profile. Only the owner can delete."""
    user_id = token_payload.get("sub")

    # Check if user is the owner
    household = await HOUSEHOLD.get(household_id)
    if household["owner_id"] != user_id:
        raise AuthorizationError(
            detail="Only the household owner can delete member profiles"
        )

    # Verify member belongs to this household
    member = await HOUSEHOLD.get_member(member_id)
    if member["household_id"] != household_id:
        raise NotFoundError(detail="Member not found in this household")

    # Delete profile
    await HOUSEHOLD.delete_member_profile(member_id)

    return {"message": "Profile deleted successfully"}
