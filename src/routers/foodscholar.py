from fastapi import APIRouter, Request, Depends
from routers.generic import render
from typing import Optional
import logging
from auth import auth
from schemas import (
    ArticleInput,
    ChatRequest,
    QAFeedbackRequest,
    QARequest,
    SummarizeRequest,
)
import kutils
from backend.foodscholar import FOODSCHOLAR
from api.v1.households import HOUSEHOLD
from api.v1.household_members import HOUSEHOLD_MEMBER
from exceptions import AuthorizationError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/foodscholar", tags=["Food Scholar Operations"])


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
    return await FOODSCHOLAR.status()


@router.get("/sessions", dependencies=[Depends(auth())])
@render()
async def sessions(request: Request):
    user = kutils.current_user(request)
    return await FOODSCHOLAR.get_user_sessions(user["sub"])


@router.get("/sessions/{session_id}/history", dependencies=[Depends(auth())])
@render()
async def session_history(request: Request, session_id: str):
    user = kutils.current_user(request)
    return await FOODSCHOLAR.get_session_history(user["sub"], session_id)


@router.post("/sessions", dependencies=[Depends(auth())])
@render()
async def create_session(request: Request, member_id: Optional[str] = None):
    user = kutils.current_user(request)
    if member_id:
        await verify_member_access(request, member_id)
    return await FOODSCHOLAR.create_session(user, member_id)


@router.post("/chat/{session_id}", dependencies=[Depends(auth())])
@render()
async def chat(request: Request, session_id: str, body: ChatRequest):
    message = body.message
    user = kutils.current_user(request)
    return await FOODSCHOLAR.chat_message(session_id, user, message)


@router.post("/search/summarize", dependencies=[Depends(auth())])
@render()
async def search_summarize(request: Request, body: SummarizeRequest):
    return await FOODSCHOLAR.get_search_summary(
        query=body.query,
        results=body.results,
        language=body.language,
        user_id=body.user_id,
        expertise_level=body.expertise_level
    )

@router.post("/enrich/article", dependencies=[Depends(auth())])
@render()
async def enrich_article(request: Request, body: ArticleInput):
    return await FOODSCHOLAR.enrich_article(
        urn=body.urn,
        title=body.title,
        abstract=body.abstract,
        authors=body.authors
    )


@router.post("/qa/ask", dependencies=[Depends(auth())])
@render()
async def ask_question(request: Request, body: QARequest):
    user = kutils.current_user(request)
    payload = body.model_copy(update={"user_id": user["sub"]})

    if payload.member_id:
        await verify_member_access(request, payload.member_id)

    return await FOODSCHOLAR.ask_question(payload.model_dump(exclude_none=True))


@router.post("/qa/feedback", dependencies=[Depends(auth())])
@render()
async def submit_feedback(request: Request, body: QAFeedbackRequest):
    return await FOODSCHOLAR.submit_qa_feedback(body.model_dump(exclude_none=True))


@router.get("/qa/models", dependencies=[Depends(auth())])
@render()
async def list_qa_models(request: Request):
    return await FOODSCHOLAR.list_qa_models()


@router.get("/qa/questions", dependencies=[Depends(auth())])
@render()
async def list_qa_questions(request: Request):
    return await FOODSCHOLAR.get_suggested_questions()


@router.get("/qa/tips", dependencies=[Depends(auth())])
@render()
async def list_qa_tips(request: Request):
    return await FOODSCHOLAR.get_tips()