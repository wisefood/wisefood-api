from fastapi import APIRouter, Request, Depends 
from routers.generic import render
from auth import auth
import uuid
import kutils
from backend.foodscholar import FOODSCHOLAR
router = APIRouter(prefix="/api/v1/foodscholar", tags=["Food Scholar Operations"])


@router.get("/status", dependencies=[Depends(auth())])
@render()
async def status(request: Request):
    return await FOODSCHOLAR.get("/")


@router.get("/sessions", dependencies=[Depends(auth())])
@render()
async def sessions(request: Request):
    user = kutils.current_user(request)
    return await FOODSCHOLAR.get(f"/user/{user['sub']}/sessions")

@router.get("/sessions/{session_id}/history", dependencies=[Depends(auth())])
@render()
async def session_history(request: Request, session_id: str):
    user = kutils.current_user(request)
    return await FOODSCHOLAR.get(f"/user/{user['sub']}/session/{session_id}/history")

@router.post("/sessions", dependencies=[Depends(auth())])
@render()
async def create_session(request: Request):
    user = kutils.current_user(request)
    spec = {
        "session_id": str(uuid.uuid4()),
        "user_context": user,
        "user_id": user['sub'],
        "max_history": 20
    }
    FOODSCHOLAR.post("/start", json=spec)
   
@router.post("/chat/{session_id}", dependencies=[Depends(auth())])
@render()
async def chat(request: Request, session_id: str):
    body = await request.json()
    message = body.get("message", "")
    if not message:
        return {"error": "Message is required"}
    user = kutils.current_user(request)
    spec = {
        "session_id": session_id,
        "user_id": user['sub'],
        "message": message,
    }
    return await FOODSCHOLAR.post("/chat", json=spec)

