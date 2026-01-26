from fastapi import APIRouter, Request, Depends
from routers.generic import render
from typing import Optional
import logging
from auth import auth
from schemas import ArticleInput, ChatRequest, SummarizeRequest
import kutils
from backend.foodscholar import FOODSCHOLAR

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/foodscholar", tags=["Food Scholar Operations"])


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