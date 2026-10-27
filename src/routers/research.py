"""What may be used to test WiseFood, and for whom.

The platform's half of the research export. Conversations live in FoodChat;
consent lives here. FoodChat's export asks this endpoint which household
members it may include, and includes nobody else — so the decision is made
where the ledger is, every time, rather than copied somewhere it can go stale.
"""

import logging

from fastapi import APIRouter, Depends, Request

import consents
from analytics import RECORDER
from auth import auth
from routers.generic import render

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/research", tags=["Research use"])


@router.get(
    "/eligible-members",
    dependencies=[Depends(auth("admin,agent"))],
    summary="Household members whose conversations may be used for research",
)
@render()
async def eligible_members(request: Request):
    """Members of every household whose account holder has research consent.

    Ids only — no names, no profiles. Admins and the platform's own service
    accounts (FoodChat's export runs as one) may read it, and each read is
    recorded, because the list is who agreed to have their words studied.
    """
    members = await consents.research_eligible_members()
    RECORDER.record_event(
        "admin.research_eligibility_read",
        app="platform",
        props={"members": len(members)},
    )
    return {
        "terms_version": consents.TERMS_VERSION,
        "members": members,
    }
