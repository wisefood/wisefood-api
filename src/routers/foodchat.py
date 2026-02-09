from fastapi import APIRouter, Request, Depends
from routers.generic import render
from typing import Optional
import logging
from auth import auth
from schemas import FoodChatCreateSessionRequest, FoodChatMessageRequest
import kutils
from backend.foodchat import FOODCHAT
from api.v1.households import HOUSEHOLD
from api.v1.household_members import HOUSEHOLD_MEMBER
from exceptions import AuthorizationError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/foodchat", tags=["Food Chat Operations"])


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
async def get_session(request: Request, session_id: str):
    """Get session state and metadata."""
    session = await FOODCHAT.get_session(session_id)
    await verify_member_access(request, session["member_id"])
    return session


@router.delete("/sessions/{session_id}", dependencies=[Depends(auth())])
@render()
async def delete_session(request: Request, session_id: str):
    """Delete a session."""
    session = await FOODCHAT.get_session(session_id)
    await verify_member_access(request, session["member_id"])
    return await FOODCHAT.delete_session(session_id)


@router.post("/sessions/{session_id}/messages", dependencies=[Depends(auth())])
@render()
async def send_message(request: Request, session_id: str, payload: FoodChatMessageRequest):
    """Send a message and get a response."""
    session = await FOODCHAT.get_session(session_id)
    await verify_member_access(request, session["member_id"])
    return await FOODCHAT.send_message(session_id=session_id, content=payload.content)


@router.get("/sessions/{session_id}/messages", dependencies=[Depends(auth())])
@render()
async def get_messages(request: Request, session_id: str, limit: Optional[int] = None):
    """Get message history for a session."""
    session = await FOODCHAT.get_session(session_id)
    await verify_member_access(request, session["member_id"])
    return await FOODCHAT.get_messages(session_id=session_id, limit=limit)


@router.get("/sessions/{session_id}/meal-plans", dependencies=[Depends(auth())])
@render()
async def get_meal_plans(request: Request, session_id: str):
    """Get all meal plans generated in this session."""
    session = await FOODCHAT.get_session(session_id)
    await verify_member_access(request, session["member_id"])
    return await FOODCHAT.get_meal_plans(session_id=session_id)


@router.get("/members/{member_id}/sessions", dependencies=[Depends(auth())])
@render()
async def get_member_sessions(request: Request, member_id: str):
    """Get all sessions for a specific member."""
    await verify_member_access(request, member_id)
    return await FOODCHAT.get_member_sessions(member_id=member_id)
