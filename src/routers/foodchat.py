from typing import Optional

import kutils
import context
from analytics import RECORDER
from api.v1.household_members import HOUSEHOLD_MEMBER
from api.v1.households import HOUSEHOLD
from auth import auth
from backend.foodchat import FOODCHAT
from budget import guest_budget
from exceptions import AuthorizationError
from fastapi import APIRouter, Depends, Query, Request
from routers.generic import render
from schemas import (
    FoodChatChatRequest,
    FoodChatComposeRequest,
    FoodChatCreateSessionRequest,
    FoodChatFacetRequest,
    FoodChatFeedbackRequest,
    FoodChatMemoryDecisionRequest,
    FoodChatPantryRequest,
    FoodChatPlanParametersRequest,
    FoodChatRenameSessionRequest,
    FoodChatReplanRequest,
    FoodChatSavePlanRequest,
    FoodChatToolInvokeRequest,
    FoodChatUpdateDinersRequest,
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

    # Recorded only once access is granted, so the activity context can never
    # name a member the caller was not allowed to act on.
    context.set_member_id(member_id)
    context.set_household_id(household.get("id"))
    return member, household


async def verify_cooking_for_household(member: dict, cooking_for: list[str]):
    """
    Verify that every member ID in cooking_for belongs to the same
    household as the given (already authorized) member.
    """
    for diner_id in cooking_for:
        if diner_id == member["id"]:
            continue
        diner = await HOUSEHOLD_MEMBER.aget_entity(diner_id)
        if diner["household_id"] != member["household_id"]:
            raise AuthorizationError(
                detail="All cooking_for members must belong to the same household"
            )


@router.get("/status", dependencies=[Depends(auth())])
@render()
async def status(request: Request):
    """Health check for the FoodChat service."""
    return await FOODCHAT.status()


@router.post(
    "/sessions",
    dependencies=[Depends(auth()), Depends(guest_budget("sessions"))],
)
@render()
async def create_session(request: Request, payload: FoodChatCreateSessionRequest):
    """Create a new chat session for a household member."""
    member, _ = await verify_member_access(request, payload.member_id)
    if payload.cooking_for is not None:
        await verify_cooking_for_household(member, payload.cooking_for)
    return await FOODCHAT.create_session(
        member_id=payload.member_id,
        cooking_for=payload.cooking_for,
    )


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


@router.get("/sessions/{session_id}/meal-plans", dependencies=[Depends(auth())])
@render()
async def get_meal_plans(
    request: Request,
    session_id: str,
    member_id: str = Query(..., description=MEMBER_ID_QUERY_DESCRIPTION),
):
    """Get all daily meal plan versions for a session."""
    await verify_member_access(request, member_id)
    return await FOODCHAT.get_meal_plans(session_id=session_id, member_id=member_id)


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


@router.get("/sessions/{session_id}/weekly-meal-plans", dependencies=[Depends(auth())])
@render()
async def get_weekly_meal_plans(
    request: Request,
    session_id: str,
    member_id: str = Query(..., description=MEMBER_ID_QUERY_DESCRIPTION),
):
    """Get all weekly meal plan versions for a session."""
    await verify_member_access(request, member_id)
    return await FOODCHAT.get_weekly_meal_plans(session_id=session_id, member_id=member_id)


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


@router.get("/members/{member_id}/current-plans", dependencies=[Depends(auth())])
@render()
async def get_member_current_plans(request: Request, member_id: str):
    """Most recent saved daily/weekly plans for a member (dashboard widget)."""
    await verify_member_access(request, member_id)
    return await FOODCHAT.get_member_current_plans(member_id=member_id)


# The three routes below were the gap between a built feature and a working
# one: the UI has shipped a save/unsave button, a saved-plans list and a
# rename control since the plan-canvas work, FoodChat has implemented all
# three, and every call 404'd here because nothing proxied them.


@router.patch("/sessions/{session_id}", dependencies=[Depends(auth())])
@render()
async def rename_session(
    request: Request, session_id: str, payload: FoodChatRenameSessionRequest
):
    """Give a session a member-facing name (replaces the timestamp label)."""
    await verify_member_access(request, payload.member_id)
    return await FOODCHAT.rename_session(
        session_id=session_id,
        member_id=payload.member_id,
        title=payload.title,
    )


@router.post(
    "/sessions/{session_id}/meal-plans/{plan_id}/save",
    dependencies=[Depends(auth())],
)
@render()
async def save_meal_plan(
    request: Request,
    session_id: str,
    plan_id: str,
    payload: FoodChatSavePlanRequest,
):
    """Save (or unsave) a plan so it outlives its conversation."""
    await verify_member_access(request, payload.member_id)
    return await FOODCHAT.save_meal_plan(
        session_id=session_id,
        plan_id=plan_id,
        member_id=payload.member_id,
        saved=payload.saved,
        title=payload.title,
    )


@router.get("/members/{member_id}/saved-plans", dependencies=[Depends(auth())])
@render()
async def get_member_saved_plans(request: Request, member_id: str):
    """Every plan the member saved, across all their sessions, newest first."""
    await verify_member_access(request, member_id)
    return await FOODCHAT.get_member_saved_plans(member_id=member_id)


@router.post(
    "/sessions/{session_id}/chat",
    dependencies=[Depends(auth()), Depends(guest_budget("chat"))],
)
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
    "/sessions/{session_id}/memory",
    dependencies=[Depends(auth())],
)
@render()
async def submit_memory_decision(
    request: Request,
    session_id: str,
    payload: FoodChatMemoryDecisionRequest,
):
    """Accept or decline a memory suggestion for a session."""
    await verify_member_access(request, payload.member_id)
    return await FOODCHAT.submit_memory_decision(
        session_id=session_id,
        member_id=payload.member_id,
        decision=payload.decision,
        suggestion=payload.suggestion.model_dump(),
    )


@router.post(
    "/sessions/{session_id}/compose",
    dependencies=[Depends(auth())],
)
@render()
async def compose_plan(
    request: Request,
    session_id: str,
    payload: FoodChatComposeRequest,
):
    """Complete a hand-started daily plan: pin the user's picked recipes and
    let FoodChat fill the remaining slots."""
    await verify_member_access(request, payload.member_id)
    return await FOODCHAT.compose_plan(
        session_id=session_id,
        member_id=payload.member_id,
        picks=[p.model_dump() for p in payload.picks],
        plan_type=payload.plan_type,
        message=payload.message,
    )


@router.post(
    "/sessions/{session_id}/plan-parameters",
    dependencies=[Depends(auth())],
)
@render()
async def apply_plan_parameters(
    request: Request,
    session_id: str,
    payload: FoodChatPlanParametersRequest,
):
    """Apply interactive plan-parameter card values (time budget, difficulty,
    goal) as a deterministic plan refinement."""
    await verify_member_access(request, payload.member_id)
    return await FOODCHAT.apply_plan_parameters(
        session_id=session_id,
        member_id=payload.member_id,
        values=payload.values,
        plan_type=payload.plan_type,
    )


@router.put(
    "/sessions/{session_id}/diners",
    dependencies=[Depends(auth())],
)
@render()
async def update_diners(
    request: Request,
    session_id: str,
    payload: FoodChatUpdateDinersRequest,
):
    """Update the diners (cooking_for) of a session."""
    member, _ = await verify_member_access(request, payload.member_id)
    await verify_cooking_for_household(member, payload.cooking_for)
    return await FOODCHAT.update_diners(
        session_id=session_id,
        member_id=payload.member_id,
        cooking_for=payload.cooking_for,
    )


# There is deliberately no gateway proxy for FoodChat's member feedback listing.
#
# Two reasons, and the second is the real one. First, every proxy here that
# names a member must authorize that member (see
# tests/test_foodchat_state_proxy.py) — an expert reading someone else's
# feedback cannot satisfy that, and carving out an exception would weaken the
# invariant for the one route that wanted it. Second, reading FoodChat's table
# directly would return comments regardless of whether their author consented
# to analytics; the feedback inbox in `analytics.feedback` applies consent and
# carries FoodScholar's and the platform widget's feedback alongside chat's.
# The inbox is the reviewing surface. FoodChat's own endpoint stays, scoped to
# a member and guarded by the signed assertion.


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


# --------------------------------------------------------------------------- #
# Standing planning state — the pantry panel and the removable facet chips     #
# --------------------------------------------------------------------------- #
# These were unreachable from the browser: FoodChat held the pantry and the
# inferred facets in session state and exposed no way to see or correct them,
# and the gateway is the only route the UI has. Same authorization as every
# other session-scoped proxy here — the household owner (or an admin/agent),
# checked against the member the payload names, before anything is forwarded.


@router.get("/sessions/{session_id}/planning-state", dependencies=[Depends(auth())])
@render()
async def get_planning_state(
    request: Request,
    session_id: str,
    member_id: str = Query(..., description=MEMBER_ID_QUERY_DESCRIPTION),
):
    """What is standing for this session: pantry, facets, stated diet, claims.

    The plan's own ledger says what was applied to THAT plan; this says what is
    still in force for the next one — which is what lets the constraints on a
    plan survive a page reload.
    """
    await verify_member_access(request, member_id)
    return await FOODCHAT.get_planning_state(
        session_id=session_id, member_id=member_id,
    )


@router.put("/sessions/{session_id}/pantry", dependencies=[Depends(auth())])
@render()
async def set_pantry(
    request: Request, session_id: str, payload: FoodChatPantryRequest,
):
    """Replace the pantry with exactly these items — the panel's save.

    The whole list rather than a delta: a member who cleared the last item
    means the pantry is empty, which an additive-only write cannot express.
    """
    await verify_member_access(request, payload.member_id)
    return await FOODCHAT.set_pantry(
        session_id=session_id, member_id=payload.member_id, items=payload.items,
    )


@router.post("/sessions/{session_id}/pantry", dependencies=[Depends(auth())])
@render()
async def add_pantry_items(
    request: Request, session_id: str, payload: FoodChatPantryRequest,
):
    """Add on-hand ingredients, leaving the rest of the pantry alone."""
    await verify_member_access(request, payload.member_id)
    return await FOODCHAT.add_pantry_items(
        session_id=session_id, member_id=payload.member_id, items=payload.items,
    )


@router.delete("/sessions/{session_id}/pantry/{item}", dependencies=[Depends(auth())])
@render()
async def remove_pantry_item(
    request: Request,
    session_id: str,
    item: str,
    member_id: str = Query(..., description=MEMBER_ID_QUERY_DESCRIPTION),
):
    """Tick one item off — used up, or heard wrong."""
    await verify_member_access(request, member_id)
    return await FOODCHAT.remove_pantry_item(
        session_id=session_id, member_id=member_id, item=item,
    )


@router.post("/sessions/{session_id}/facets", dependencies=[Depends(auth())])
@render()
async def add_facets(
    request: Request, session_id: str, payload: FoodChatFacetRequest,
):
    """Ask for a taste the assistant did not infer — the other half of the
    removable chip, and the only thing that can act on `/vocabularies`."""
    await verify_member_access(request, payload.member_id)
    return await FOODCHAT.add_facets(
        session_id=session_id, member_id=payload.member_id, values=payload.values,
    )


@router.delete("/sessions/{session_id}/facets/{value}", dependencies=[Depends(auth())])
@render()
async def remove_facet(
    request: Request,
    session_id: str,
    value: str,
    member_id: str = Query(..., description=MEMBER_ID_QUERY_DESCRIPTION),
):
    """Take back one facet FoodChat inferred from something the member said."""
    await verify_member_access(request, member_id)
    return await FOODCHAT.remove_facet(
        session_id=session_id, member_id=member_id, value=value,
    )


@router.post("/sessions/{session_id}/replan", dependencies=[Depends(auth())])
@render()
async def replan(request: Request, session_id: str, payload: FoodChatReplanRequest):
    """Re-plan from the standing state — what a facet removal or pantry edit
    calls once the member is done changing things.

    Deliberately separate from the state writes above: ticking off three
    pantry items should not run three regenerations.
    """
    await verify_member_access(request, payload.member_id)
    return await FOODCHAT.replan(
        session_id=session_id,
        member_id=payload.member_id,
        plan_type=payload.plan_type,
    )


@router.get("/vocabularies", dependencies=[Depends(auth())])
@render()
async def get_vocabularies(request: Request):
    """The facet vocabulary the recipe corpus actually carries.

    Not a convenience: the recipe search ANDs facet values and never relaxes an
    unlisted one to nothing, so a value the corpus does not carry does not
    soften a search — it empties it, and the member is told no meals exist.
    """
    return await FOODCHAT.get_vocabularies()


# --------------------------------------------------------------------------- #
# Tool surface                                                                 #
# --------------------------------------------------------------------------- #
# FoodChat exposes typed, individually callable capabilities — summarise the
# week, replace one day, total a plan — and none of them had a route through
# the gateway, so four of the five had no possible caller from the browser.


@router.get("/tools", dependencies=[Depends(auth())])
@render()
async def list_tools(request: Request):
    """Every tool the agent can call, with its schema.

    Generated from FoodChat's registry, so a tool that exists is listed and a
    tool that is listed exists. Each entry says whether it changes the plan and
    whether it spends model calls, so a caller can decide before invoking.
    """
    return await FOODCHAT.list_tools()


@router.post("/tools/{tool_name}", dependencies=[Depends(auth())])
@render()
async def invoke_tool(
    request: Request, tool_name: str, payload: FoodChatToolInvokeRequest,
):
    """Run one tool by name.

    Authorized here on the member the payload names, and again inside FoodChat
    against the session the arguments name — two layers, because this route can
    rewrite a whole day of someone's plan.
    """
    await verify_member_access(request, payload.member_id)
    return await FOODCHAT.invoke_tool(
        tool_name=tool_name,
        member_id=payload.member_id,
        arguments=payload.arguments,
    )
