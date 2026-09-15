"""Share links.

Two audiences on one table. Everything under `/shares` belongs to the person
who made the share and is authenticated as usual; `/shares/public/{token}`
belongs to whoever was sent the link and is authenticated by the token alone.

The server reads the plan itself rather than accepting one from the client.
A client-supplied payload is a client-supplied *claim* — that this is their
plan, and that it says what they say it says — and neither is checkable after
the fact. Fetching it here means the ownership check and the scrub happen on
the same object that gets published.
"""
from typing import Optional

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import select

import kutils
from auth import auth
from exceptions import AuthorizationError, NotFoundError
from routers.generic import render

router = APIRouter(prefix="/api/v1/shares", tags=["Sharing"])


class ShareCreate(BaseModel):
    """What to publish. The payload is not accepted from the client."""

    kind: str = Field(description="meal_plan | saved_meal_plan")
    id: str = Field(max_length=100, description="The plan to share")
    title: str = Field(default="", max_length=200)
    #: None means "until revoked".
    expires_in_days: Optional[int] = Field(default=None, ge=1, le=365)


async def _load_owned_plan(request: Request, kind: str, plan_id: str):
    """The plan, its title, and proof the caller owns it.

    Ownership runs through the household exactly as everywhere else: a plan
    belongs to a household, a household has an `owner_id`, and that must be
    the `sub` on the token. Guests included — a guest owns their household
    like anybody else, which is what lets them share before they have an
    account to share from.
    """
    from api.v1.households import HOUSEHOLD
    from api.v1.household_members import HOUSEHOLD_MEMBER
    from backend.postgres import POSTGRES_ASYNC_SESSION_FACTORY
    from sql import MealPlan, SavedMealPlan

    user_id = kutils.current_user(request)["sub"]

    async with POSTGRES_ASYNC_SESSION_FACTORY()() as db:
        if kind == "meal_plan":
            plan = (
                await db.execute(select(MealPlan).where(MealPlan.id == plan_id))
            ).scalar_one_or_none()
            if plan is None:
                raise NotFoundError(detail="No such meal plan")
            household_id = plan.household_id
            payload = plan.to_dict()
            default_title = f"Meal plan for {plan.applied_on.isoformat()}"
        else:
            plan = (
                await db.execute(
                    select(SavedMealPlan).where(SavedMealPlan.id == plan_id)
                )
            ).scalar_one_or_none()
            if plan is None:
                raise NotFoundError(detail="No such saved meal plan")
            member = await HOUSEHOLD_MEMBER.aget_entity(plan.member_id)
            household_id = member["household_id"]
            payload = {
                "source_applied_on": (
                    plan.source_applied_on.isoformat() if plan.source_applied_on else None
                ),
                "breakfast": plan.breakfast,
                "lunch": plan.lunch,
                "dinner": plan.dinner,
            }
            default_title = plan.name

    household = await HOUSEHOLD.aget_entity(household_id)
    if household["owner_id"] != user_id and not kutils.is_admin(request):
        raise AuthorizationError(detail="You do not have access to this plan")

    return payload, default_title, user_id


@router.post(
    "",
    dependencies=[Depends(auth())],
    summary="Publish a meal plan behind a share link",
    description=(
        "Creates an unguessable link anyone can open without an account. The "
        "plan is copied at this moment and scrubbed to the food alone — no "
        "names, ages, allergies, goals or ids travel with it — so editing or "
        "deleting the original later changes nothing for whoever holds the "
        "link. Available to guests, and the link outlives the guest account."
    ),
)
@render()
async def api_create_share(request: Request, body: ShareCreate):
    import sharing

    if body.kind not in sharing.KINDS:
        raise NotFoundError(detail=f"Cannot share a {body.kind}")

    payload, default_title, user_id = await _load_owned_plan(
        request, body.kind, body.id
    )
    return await sharing.create_share(
        owner_id=user_id,
        kind=body.kind,
        payload=payload,
        source_id=body.id,
        title=body.title or default_title,
        expires_in_days=body.expires_in_days,
    )


@router.get(
    "",
    dependencies=[Depends(auth())],
    summary="Everything the current user has shared",
)
@render()
async def api_list_shares(request: Request, limit: int = 50):
    import sharing

    user_id = kutils.current_user(request)["sub"]
    return {"shares": await sharing.list_shares(owner_id=user_id, limit=limit)}


@router.delete(
    "/{token}",
    dependencies=[Depends(auth())],
    summary="Revoke a share link",
    description=(
        "The link stops working for everyone immediately. The row is kept so "
        "the owner can still see it was shared and how often it was opened."
    ),
)
@render()
async def api_revoke_share(request: Request, token: str):
    import sharing

    user_id = kutils.current_user(request)["sub"]
    if not await sharing.revoke_share(owner_id=user_id, token=token):
        raise NotFoundError(detail="No such share link")
    return {"revoked": True}


class ShareEmail(BaseModel):
    """Who to send a share to. Omit `to` to send it to yourself."""

    to: Optional[str] = Field(default=None, max_length=254)
    note: str = Field(default="", max_length=500)


@router.post(
    "/{token}/email",
    dependencies=[Depends(auth())],
    summary="Email a share link, with the plan in the message",
    description=(
        "Sends the plan as a styled email carrying the share link. Only the "
        "owner of the link may send it, and only the scrubbed snapshot goes "
        "out — mail cannot be recalled, so it carries the food and not the "
        "people. Omit `to` to send it to your own address; guests have no "
        "deliverable address and must name one."
    ),
)
@render()
async def api_email_share(request: Request, token: str, body: ShareEmail):
    import mailer
    import sharing

    if not mailer.enabled():
        raise NotFoundError(detail="Email is not configured for this deployment")

    user = kutils.current_user(request)
    user_id = user["sub"]

    # Owner only — and read through `list_shares` so a revoked or expired
    # link cannot be mailed out after the fact.
    mine = {s["token"]: s for s in await sharing.list_shares(owner_id=user_id, limit=200)}
    share = mine.get(token)
    if share is None or not share["live"]:
        raise NotFoundError(detail="No such share link")

    recipient = (body.to or "").strip() or (user.get("email") or "").strip()
    if not mailer.valid_address(recipient):
        raise NotFoundError(detail="No deliverable address to send to")

    # A send from our domain on somebody's say-so is a spam relay if it is
    # unbounded, and a guest account costs nothing to mint.
    if not mailer.within_quota(user_id):
        raise AuthorizationError(
            detail="You have sent the maximum number of emails for today"
        )

    shared = await sharing.read_share(token)
    if shared is None:
        raise NotFoundError(detail="No such share link")

    sender_name = (user.get("given_name") or user.get("name") or "").strip()
    to_self = recipient.lower() == (user.get("email") or "").strip().lower()

    html_body, text_body = mailer.render_meal_plan(
        member_name=sender_name,
        payload=shared.get("payload") or {},
        share_url=sharing.share_url(token),
        # Only say who sent it when it is going to somebody else; "Ana shared
        # a plan with you" is odd when Ana is the one reading it.
        from_name=None if to_self else (sender_name or None),
    )
    sent = await mailer.send_email(
        to=recipient,
        subject=mailer.plan_subject(sender_name if to_self else ""),
        html_body=html_body,
        text_body=text_body,
        reply_to=user.get("email") if not to_self else None,
    )
    return {"sent": sent, "to": recipient}


@router.get(
    "/public/{token}",
    summary="Read a shared plan — no account needed",
    description=(
        "Deliberately unauthenticated: the token is the credential. Answers "
        "404 for a link that is unknown, revoked or expired alike, so it "
        "cannot be used to find out which tokens exist."
    ),
)
@render()
async def api_read_share(request: Request, token: str, response: Response):
    import sharing

    # Set before the lookup so a 404 is as unindexable as a hit.
    for header, value in sharing.share_headers().items():
        response.headers[header] = value

    shared = await sharing.read_share(token)
    if shared is None:
        raise NotFoundError(detail="This link is not available")
    return shared
