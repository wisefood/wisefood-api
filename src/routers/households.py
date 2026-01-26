"""
Household management endpoints
"""

from typing import Dict, Any
from fastapi import APIRouter, Depends, Request
from httpx import request
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


async def verify_access(
    request: Request, household_id: str, member_id: str = None
) -> Dict[str, Any]:
    """
    Verify that the current user has access to the household.
    Raises AuthorizationError if access is denied.
    Returns the member data if member_id is provided, else None.
    """
    user = kutils.current_user(request)

    member = None
    if member_id:
        member = await HOUSEHOLD_MEMBER.aget_entity(member_id)
        household_id = member["household_id"]

    household = await HOUSEHOLD.aget_entity(household_id)

    # Check if user is the owner or admin or AI agent
    if (
        household["owner_id"] != user["sub"]
        and not kutils.is_admin(request)
        and not kutils.is_agent(request)
    ):
        raise AuthorizationError(detail="You do not have access to this member")

    return (member, household) if member_id else household


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
        household_data.model_dump(exclude_unset=True), kutils.current_user(request)
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

    await verify_access(request, household_id)

    household = await HOUSEHOLD.aget_entity(household_id)

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

    await verify_access(request, household_id)

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

    await verify_access(request, household_id)

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
