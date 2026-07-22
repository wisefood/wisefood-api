"""
Meal plan storage and retrieval endpoints scoped to household members.
"""

from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, Query, Request

from auth import auth
from routers.generic import render
from routers.households import verify_access
from api.v1.meal_plans import MEAL_PLAN, SAVED_MEAL_PLAN
from schemas import (
    MealPlanRevokeResponse,
    MealPlanResponse,
    MealPlanStoreRequest,
    SavedMealPlanCreateRequest,
    SavedMealPlanDeleteResponse,
    SavedMealPlanListResponse,
    SavedMealPlanResponse,
    SavedMealPlanUpdateRequest,
)


router = APIRouter(prefix="/api/v1/members", tags=["Household Meal Plans"])


@router.post(
    "/{member_id}/meal-plans",
    dependencies=[Depends(auth())],
    summary="Store a meal plan for a member date",
    description=(
        "Stores a meal plan for the given member and date. "
        "The same stored plan can also be assigned to additional members in the same household."
    ),
)
@render()
async def api_store_meal_plan(
    request: Request,
    member_id: str,
    payload: MealPlanStoreRequest,
):
    await verify_access(request, None, member_id)

    target_date = payload.date or date.today()
    created = await MEAL_PLAN.store_for_members(
        requested_member_id=member_id,
        target_date=target_date,
        meal_plan_spec=payload.meal_plan.model_dump(exclude_none=True),
        additional_member_ids=payload.applies_to_member_ids,
    )
    return MealPlanResponse(**created)


@router.get(
    "/{member_id}/meal-plans",
    dependencies=[Depends(auth())],
    summary="Get member meal plan for a date",
    description="Returns the member's meal plan for a date. Date defaults to current date.",
)
@render()
async def api_get_member_meal_plan(
    request: Request,
    member_id: str,
    meal_date: Optional[date] = Query(default=None, alias="date"),
):
    await verify_access(request, None, member_id)

    target_date = meal_date or date.today()
    meal_plan = await MEAL_PLAN.get_for_member_and_date(
        member_id=member_id,
        target_date=target_date,
    )
    return MealPlanResponse(**meal_plan)


@router.delete(
    "/{member_id}/meal-plans/{meal_plan_id}",
    dependencies=[Depends(auth())],
    summary="Revoke stored meal plan",
    description=(
        "Revokes a meal plan assignment for a member. "
        "Set revoke_for_all_members=true to revoke the plan for all assigned members."
    ),
)
@render()
async def api_revoke_member_meal_plan(
    request: Request,
    member_id: str,
    meal_plan_id: str,
    revoke_for_all_members: bool = False,
):
    await verify_access(request, None, member_id)

    revoked = await MEAL_PLAN.revoke(
        member_id=member_id,
        meal_plan_id=meal_plan_id,
        revoke_for_all_members=revoke_for_all_members,
    )
    return MealPlanRevokeResponse(**revoked)


@router.get(
    "/{member_id}/saved-meal-plans",
    dependencies=[Depends(auth())],
    summary="List a member's saved meal plans",
    description="Returns the member's saved meal plans, newest first.",
)
@render()
async def api_list_saved_meal_plans(request: Request, member_id: str):
    await verify_access(request, None, member_id)

    listing = await SAVED_MEAL_PLAN.list_for_member(member_id=member_id)
    return SavedMealPlanListResponse(**listing)


@router.post(
    "/{member_id}/saved-meal-plans",
    dependencies=[Depends(auth())],
    summary="Save a meal plan to a member's library",
    description=(
        "Saves a meal plan under a name. Pass meal_plan_id to snapshot a stored "
        "plan, or meal_plan to save meals directly. The saved copy is independent "
        "of the stored plan, so revoking that plan does not remove it. Saving the "
        "same source plan again renames the existing entry instead of duplicating it."
    ),
)
@render()
async def api_save_meal_plan(
    request: Request,
    member_id: str,
    payload: SavedMealPlanCreateRequest,
):
    await verify_access(request, None, member_id)

    saved = await SAVED_MEAL_PLAN.save(
        member_id=member_id,
        name=payload.name,
        meal_plan_id=payload.meal_plan_id,
        meal_plan_spec=(
            payload.meal_plan.model_dump(exclude_none=True) if payload.meal_plan else None
        ),
    )
    return SavedMealPlanResponse(**saved)


@router.get(
    "/{member_id}/saved-meal-plans/{saved_meal_plan_id}",
    dependencies=[Depends(auth())],
    summary="Get a saved meal plan",
    description="Returns a single saved meal plan owned by the member.",
)
@render()
async def api_get_saved_meal_plan(
    request: Request,
    member_id: str,
    saved_meal_plan_id: str,
):
    await verify_access(request, None, member_id)

    saved = await SAVED_MEAL_PLAN.get(
        member_id=member_id,
        saved_meal_plan_id=saved_meal_plan_id,
    )
    return SavedMealPlanResponse(**saved)


@router.patch(
    "/{member_id}/saved-meal-plans/{saved_meal_plan_id}",
    dependencies=[Depends(auth())],
    summary="Rename a saved meal plan",
    description=(
        "Renames a saved meal plan. The meals are a snapshot and are not editable."
    ),
)
@render()
async def api_rename_saved_meal_plan(
    request: Request,
    member_id: str,
    saved_meal_plan_id: str,
    payload: SavedMealPlanUpdateRequest,
):
    await verify_access(request, None, member_id)

    saved = await SAVED_MEAL_PLAN.rename(
        member_id=member_id,
        saved_meal_plan_id=saved_meal_plan_id,
        name=payload.name,
    )
    return SavedMealPlanResponse(**saved)


@router.delete(
    "/{member_id}/saved-meal-plans/{saved_meal_plan_id}",
    dependencies=[Depends(auth())],
    summary="Delete a saved meal plan",
    description="Removes a saved meal plan from the member's library.",
)
@render()
async def api_delete_saved_meal_plan(
    request: Request,
    member_id: str,
    saved_meal_plan_id: str,
):
    await verify_access(request, None, member_id)

    deleted = await SAVED_MEAL_PLAN.delete(
        member_id=member_id,
        saved_meal_plan_id=saved_meal_plan_id,
    )
    return SavedMealPlanDeleteResponse(**deleted)
