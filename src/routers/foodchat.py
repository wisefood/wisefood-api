from typing import Optional

import kutils
from api.v1.household_members import HOUSEHOLD_MEMBER
from api.v1.households import HOUSEHOLD
from auth import auth
from backend.foodchat import FOODCHAT
from exceptions import AuthorizationError
from fastapi import APIRouter, Depends, Query, Request
from routers.generic import render
from schemas import (
    FoodChatChatRequest,
    FoodChatCreateSessionRequest,
    FoodChatFeedbackRequest,
    FoodChatMessageRequest,
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


async def verify_legacy_session_access(
    request: Request,
    session_id: str,
    member_id: str,
):
    """
    Legacy FoodChat endpoints do not carry member_id upstream, so we verify locally
    and then preflight the session ownership against FoodChat before forwarding.
    """
    await verify_member_access(request, member_id)
    return await FOODCHAT.get_session(session_id=session_id, member_id=member_id)


@router.get("/status", dependencies=[Depends(auth())])
@render()
async def status(request: Request):
    """Health check for the FoodChat service."""
    return await FOODCHAT.status()


@router.post("/sessions", dependencies=[Depends(auth())])
@render()
async def create_session(request: Request, payload: FoodChatCreateSessionRequest):
    """Create a new chat session for a household member."""
    await verify_member_access(request, payload.member_id)
    return await FOODCHAT.create_session(member_id=payload.member_id)


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


@router.post("/sessions/{session_id}/messages", dependencies=[Depends(auth())])
@render()
async def send_message(
    request: Request,
    session_id: str,
    payload: FoodChatMessageRequest,
    member_id: str = Query(..., description=MEMBER_ID_QUERY_DESCRIPTION),
):
    """Send a message through the legacy daily chat endpoint."""
    await verify_legacy_session_access(request, session_id, member_id)
    return await FOODCHAT.send_message(session_id=session_id, content=payload.content)


@router.get("/sessions/{session_id}/messages", dependencies=[Depends(auth())])
@render()
async def get_messages(
    request: Request,
    session_id: str,
    member_id: str = Query(..., description=MEMBER_ID_QUERY_DESCRIPTION),
    limit: Optional[int] = Query(default=None, ge=1),
):
    """Get message history for a session."""
    await verify_legacy_session_access(request, session_id, member_id)
    return await FOODCHAT.get_messages(session_id=session_id, limit=limit)


@router.get("/sessions/{session_id}/meal-plans", dependencies=[Depends(auth())])
@render()
async def get_meal_plans(
    request: Request,
    session_id: str,
    member_id: str = Query(..., description=MEMBER_ID_QUERY_DESCRIPTION),
):
    """Get all daily meal plan versions for a session."""
    await verify_legacy_session_access(request, session_id, member_id)
    return await FOODCHAT.get_meal_plans(session_id=session_id)


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


@router.post("/sessions/{session_id}/weekly", dependencies=[Depends(auth())])
@render()
async def send_weekly_message(
    request: Request,
    session_id: str,
    payload: FoodChatMessageRequest,
    member_id: str = Query(..., description=MEMBER_ID_QUERY_DESCRIPTION),
):
    """Send a message through the legacy weekly meal planning endpoint."""
    await verify_legacy_session_access(request, session_id, member_id)
    return await FOODCHAT.send_weekly_message(
        session_id=session_id,
        content=payload.content,
    )


@router.get("/sessions/{session_id}/weekly", dependencies=[Depends(auth())])
@render()
async def get_weekly_messages(
    request: Request,
    session_id: str,
    member_id: str = Query(..., description=MEMBER_ID_QUERY_DESCRIPTION),
    limit: Optional[int] = Query(default=None, ge=1),
):
    """Get weekly message history for a session."""
    await verify_legacy_session_access(request, session_id, member_id)
    return await FOODCHAT.get_weekly_messages(session_id=session_id, limit=limit)


@router.get("/sessions/{session_id}/weekly-meal-plans", dependencies=[Depends(auth())])
@render()
async def get_weekly_meal_plans(
    request: Request,
    session_id: str,
    member_id: str = Query(..., description=MEMBER_ID_QUERY_DESCRIPTION),
):
    """Get all weekly meal plan versions for a session."""
    await verify_legacy_session_access(request, session_id, member_id)
    return await FOODCHAT.get_weekly_meal_plans(session_id=session_id)


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


@router.post("/sessions/{session_id}/chat", dependencies=[Depends(auth())])
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
