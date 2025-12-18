
from fastapi import APIRouter, Request, Depends
from routers.generic import render
import logging
from auth import auth
from backend.recipewrangler import RECIPEWRANGLER

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/recipewrangler", tags=["Recipe Wrangler Operations"])


@router.get("/status", dependencies=[Depends(auth())])
@render()
async def status(request: Request):
    return await RECIPEWRANGLER.status()
