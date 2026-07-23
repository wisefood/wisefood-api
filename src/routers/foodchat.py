from typing import Optional

import kutils
from api.v1.household_members import HOUSEHOLD_MEMBER
from api.v1.households import HOUSEHOLD
from auth import auth
from backend.foodchat import FOODCHAT
from budget import guest_budget
from exceptions import AuthorizationError
from fastapi import APIRouter, Depends, Query, Request
from routers.generic import render
from schemas import (
    FoodChatChatRequest,
    FoodChatComposeRequest,
    FoodChatCreateSessionRequest,
    FoodChatFeedbackRequest,
    FoodChatMemoryDecisionRequest,
    FoodChatPlanParametersRequest,
    FoodChatUpdateDinersRequest,
)

router = APIRouter(prefix="/api/v1/foodchat", tags=["Food Chat Operations"])

MEMBER_ID_QUERY_DESCRIPTION = (
    "Household member ID used by WiseFood to authorize access to the FoodChat session"
)


async def verify_member_access(request: Request, member_id: str):
    """
    Verify that the current user has access to the given member.
    The member must belong to a household owned by the current user,
    or the user must be an admin/agent.
    """
    user = kutils.current_user(request)
    member = await HOUSEHOLD_MEMBER.aget_entity(member_id)
    household = await HOUSEHOLD.aget_entity(member["household_id"])

    if (
        household["owner_id"] != user["sub"]
        and not kutils.is_admin(request)
        and not kutils.is_agent(request)
    ):
        raise AuthorizationError(detail="You do not have access to this member")

    return member, household


async def verify_cooking_for_household(member: dict, cooking_for: list[str]):
    """
    Verify that every member ID in cooking_for belongs to the same
    household as the given (already authorized) member.
    """
    for diner_id in cooking_for:
        if diner_id == member["id"]:
            continue
        diner = await HOUSEHOLD_MEMBER.aget_entity(diner_id)
        if diner["household_id"] != member["household_id"]:
            raise AuthorizationError(
                detail="All cooking_for members must belong to the same household"
            )


@router.get("/status", dependencies=[Depends(auth())])
@render()
async def status(request: Request):
    """Health check for the FoodChat service."""
    return await FOODCHAT.status()


@router.post(
    "/sessions",
    dependencies=[Depends(auth()), Depends(guest_budget("sessions"))],
)
@render()
async def create_session(request: Request, payload: FoodChatCreateSessionRequest):
    """Create a new chat session for a household member."""
    member, _ = await verify_member_access(request, payload.member_id)
    if payload.cooking_for is not None:
        await verify_cooking_for_household(member, payload.cooking_for)
    return await FOODCHAT.create_session(
        member_id=payload.member_id,
        cooking_for=payload.cooking_for,
    )


@router.get("/sessions/{session_id}", dependencies=[Depends(auth())])
@render()
async def get_session(
    request: Request,
    session_id: str,
    member_id: str = Query(..., description=MEMBER_ID_QUERY_DESCRIPTION),
):
    """Get session state and metadata."""
    await verify_member_access(request, member_id)
    return await FOODCHAT.get_session(session_id=session_id, member_id=member_id)


@router.delete("/sessions/{session_id}", dependencies=[Depends(auth())])
@render()
async def delete_session(
    request: Request,
    session_id: str,
    member_id: str = Query(..., description=MEMBER_ID_QUERY_DESCRIPTION),
):
    """Delete a session."""
    await verify_member_access(request, member_id)
    return await FOODCHAT.delete_session(session_id=session_id, member_id=member_id)


@router.get("/sessions/{session_id}/meal-plans", dependencies=[Depends(auth())])
@render()
async def get_meal_plans(
    request: Request,
    session_id: str,
    member_id: str = Query(..., description=MEMBER_ID_QUERY_DESCRIPTION),
):
    """Get all daily meal plan versions for a session."""
    await verify_member_access(request, member_id)
    return await FOODCHAT.get_meal_plans(session_id=session_id, member_id=member_id)


@router.get("/sessions/{session_id}/meal-plans/current", dependencies=[Depends(auth())])
@render()
async def get_current_meal_plan(
    request: Request,
    session_id: str,
    member_id: str = Query(..., description=MEMBER_ID_QUERY_DESCRIPTION),
):
    """Get the current daily meal plan for a session."""
    await verify_member_access(request, member_id)
    return await FOODCHAT.get_current_meal_plan(
        session_id=session_id,
        member_id=member_id,
    )


@router.get("/sessions/{session_id}/meal-plans/history", dependencies=[Depends(auth())])
@render()
async def get_meal_plan_history(
    request: Request,
    session_id: str,
    member_id: str = Query(..., description=MEMBER_ID_QUERY_DESCRIPTION),
):
    """Get daily meal plan history for a session."""
    await verify_member_access(request, member_id)
    return await FOODCHAT.get_meal_plan_history(
        session_id=session_id,
        member_id=member_id,
    )


@router.get("/sessions/{session_id}/weekly-meal-plans", dependencies=[Depends(auth())])
@render()
async def get_weekly_meal_plans(
    request: Request,
    session_id: str,
    member_id: str = Query(..., description=MEMBER_ID_QUERY_DESCRIPTION),
):
    """Get all weekly meal plan versions for a session."""
    await verify_member_access(request, member_id)
    return await FOODCHAT.get_weekly_meal_plans(session_id=session_id, member_id=member_id)


@router.get(
    "/sessions/{session_id}/weekly-meal-plans/current",
    dependencies=[Depends(auth())],
)
@render()
async def get_current_weekly_meal_plan(
    request: Request,
    session_id: str,
    member_id: str = Query(..., description=MEMBER_ID_QUERY_DESCRIPTION),
):
    """Get the current weekly meal plan for a session."""
    await verify_member_access(request, member_id)
    return await FOODCHAT.get_current_weekly_meal_plan(
        session_id=session_id,
        member_id=member_id,
    )


@router.get(
    "/sessions/{session_id}/weekly-meal-plans/history",
    dependencies=[Depends(auth())],
)
@render()
async def get_weekly_meal_plan_history(
    request: Request,
    session_id: str,
    member_id: str = Query(..., description=MEMBER_ID_QUERY_DESCRIPTION),
):
    """Get weekly meal plan history for a session."""
    await verify_member_access(request, member_id)
    return await FOODCHAT.get_weekly_meal_plan_history(
        session_id=session_id,
        member_id=member_id,
    )


@router.get("/members/{member_id}/sessions", dependencies=[Depends(auth())])
@render()
async def get_member_sessions(request: Request, member_id: str):
    """Get all sessions for a specific member."""
    await verify_member_access(request, member_id)
    return await FOODCHAT.get_member_sessions(member_id=member_id)


@router.get("/members/{member_id}/current-plans", dependencies=[Depends(auth())])
@render()
async def get_member_current_plans(request: Request, member_id: str):
    """Most recent saved daily/weekly plans for a member (dashboard widget)."""
    await verify_member_access(request, member_id)
    return await FOODCHAT.get_member_current_plans(member_id=member_id)


@router.post(
    "/sessions/{session_id}/chat",
    dependencies=[Depends(auth()), Depends(guest_budget("chat"))],
)
@render()
async def chat(request: Request, session_id: str, payload: FoodChatChatRequest):
    """Send a message through the unified FoodChat endpoint."""
    await verify_member_access(request, payload.member_id)
    return await FOODCHAT.chat(
        session_id=session_id,
        member_id=payload.member_id,
        content=payload.content,
    )


@router.get("/sessions/{session_id}/conversation", dependencies=[Depends(auth())])
@render()
async def get_conversation(
    request: Request,
    session_id: str,
    member_id: str = Query(..., description=MEMBER_ID_QUERY_DESCRIPTION),
    before_id: Optional[int] = Query(
        default=None,
        description="Cursor: return messages with DB id lower than this value",
    ),
    limit: int = Query(
        default=20,
        ge=1,
        le=100,
        description="Number of messages to return",
    ),
):
    """Get cursor-based paginated conversation history."""
    await verify_member_access(request, member_id)
    return await FOODCHAT.get_conversation(
        session_id=session_id,
        member_id=member_id,
        before_id=before_id,
        limit=limit,
    )


@router.post(
    "/sessions/{session_id}/memory",
    dependencies=[Depends(auth())],
)
@render()
async def submit_memory_decision(
    request: Request,
    session_id: str,
    payload: FoodChatMemoryDecisionRequest,
):
    """Accept or decline a memory suggestion for a session."""
    await verify_member_access(request, payload.member_id)
    return await FOODCHAT.submit_memory_decision(
        session_id=session_id,
        member_id=payload.member_id,
        decision=payload.decision,
        suggestion=payload.suggestion.model_dump(),
    )


@router.post(
    "/sessions/{session_id}/compose",
    dependencies=[Depends(auth())],
)
@render()
async def compose_plan(
    request: Request,
    session_id: str,
    payload: FoodChatComposeRequest,
):
    """Complete a hand-started daily plan: pin the user's picked recipes and
    let FoodChat fill the remaining slots."""
    await verify_member_access(request, payload.member_id)
    return await FOODCHAT.compose_plan(
        session_id=session_id,
        member_id=payload.member_id,
        picks=[p.model_dump() for p in payload.picks],
        plan_type=payload.plan_type,
        message=payload.message,
    )


@router.post(
    "/sessions/{session_id}/plan-parameters",
    dependencies=[Depends(auth())],
)
@render()
async def apply_plan_parameters(
    request: Request,
    session_id: str,
    payload: FoodChatPlanParametersRequest,
):
    """Apply interactive plan-parameter card values (time budget, difficulty,
    goal) as a deterministic plan refinement."""
    await verify_member_access(request, payload.member_id)
    return await FOODCHAT.apply_plan_parameters(
        session_id=session_id,
        member_id=payload.member_id,
        values=payload.values,
        plan_type=payload.plan_type,
    )


@router.put(
    "/sessions/{session_id}/diners",
    dependencies=[Depends(auth())],
)
@render()
async def update_diners(
    request: Request,
    session_id: str,
    payload: FoodChatUpdateDinersRequest,
):
    """Update the diners (cooking_for) of a session."""
    member, _ = await verify_member_access(request, payload.member_id)
    await verify_cooking_for_household(member, payload.cooking_for)
    return await FOODCHAT.update_diners(
        session_id=session_id,
        member_id=payload.member_id,
        cooking_for=payload.cooking_for,
    )


@router.post(
    "/sessions/{session_id}/messages/{message_id}/feedback",
    dependencies=[Depends(auth())],
)
@render()
async def submit_feedback(
    request: Request,
    session_id: str,
    message_id: int,
    payload: FoodChatFeedbackRequest,
):
    """Submit feedback for an assistant message."""
    await verify_member_access(request, payload.member_id)
    return await FOODCHAT.submit_feedback(
        session_id=session_id,
        message_id=message_id,
        member_id=payload.member_id,
        rating=payload.rating,
        comment=payload.comment,
    )
