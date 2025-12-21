from fastapi import APIRouter, Request, Depends
from routers.generic import render
from typing import Optional
import logging
from auth import auth
from schemas import ChatRequest
import kutils
from backend.foodchat import FOODCHAT

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/foodchat", tags=["Food Chat Operations"])


@router.get("/status", dependencies=[Depends(auth())])
@render()
async def status(request: Request):
    return await FOODCHAT.status()