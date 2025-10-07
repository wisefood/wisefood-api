from fastapi import APIRouter, Request, Depends 
from routers.generic import render
from auth import auth
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