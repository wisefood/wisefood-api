from fastapi import APIRouter, Query, Request, Depends
from fastapi.responses import StreamingResponse
from routers.generic import render
from typing import List, Optional
from pydantic import BaseModel, Field
import logging
from auth import auth
from schemas import (
    ArticleEnrichmentBatchRequest,
    ArticleEnrichmentCriteriaBatchRequest,
    ArticleEnrichmentRequest,
    ArticleInput,
    ChatRequest,
    EnrichmentSweeperPauseRequest,
    EnrichmentWorkerRestartRequest,
    # Memory nudge payloads are deliberately the same shape in both apps —
    # the FoodChat* models double as FoodScholar's.
    FoodChatMemoryDecisionRequest,
    GuidelineEnrichmentEnqueueRequest,
    GuidelineEnrichmentPreviewRequest,
    GuidelineExtractionRequest,
    GuidelineImportRequest,
    QAFeedbackRequest,
    QARequest,
    SummarizeRequest,
)
import kutils
import context
from analytics import RECORDER
from backend.foodscholar import FOODSCHOLAR
from budget import deny_guests, guest_budget
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

    # Recorded only once access is granted, so the activity context can never
    # name a member the caller was not allowed to act on.
    context.set_member_id(member_id)
    context.set_household_id(household.get("id"))
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


@router.post(
    "/sessions",
    dependencies=[Depends(auth()), Depends(guest_budget("sessions"))],
)
@render()
async def create_session(request: Request, member_id: Optional[str] = None):
    user = kutils.current_user(request)
    if member_id:
        await verify_member_access(request, member_id)
    return await FOODSCHOLAR.create_session(user, member_id)


@router.post(
    "/chat/{session_id}",
    dependencies=[Depends(auth()), Depends(guest_budget("chat"))],
)
@render()
async def chat(request: Request, session_id: str, body: ChatRequest):
    message = body.message
    user = kutils.current_user(request)
    return await FOODSCHOLAR.chat_message(session_id, user, message)


@router.post(
    "/search/summarize",
    dependencies=[Depends(auth()), Depends(guest_budget("search"))],
)
@render()
async def search_summarize(request: Request, body: SummarizeRequest):
    return await FOODSCHOLAR.get_search_summary(
        query=body.query,
        results=body.results,
        language=body.language,
        user_id=body.user_id,
        expertise_level=body.expertise_level
    )

@router.post(
    "/enrich/article",
    dependencies=[Depends(auth()), Depends(deny_guests)],
)
@render()
async def enrich_article(request: Request, body: ArticleInput):
    return await FOODSCHOLAR.enrich_article(
        urn=body.urn,
        title=body.title,
        abstract=body.abstract,
        authors=body.authors
    )


# --------------------------------------------------------------------------- #
# Selective enrichment (console operations)
#
# These drive the article catalog's enrichment from the console: enrich one or
# more articles on demand, inspect per-article state, and pause the background
# sweeper. Admin/expert only — they mutate catalog records and cost LLM calls.
# --------------------------------------------------------------------------- #


@router.post(
    "/enrich/articles",
    dependencies=[Depends(auth("admin,expert"))],
    status_code=202,
)
@render()
async def enqueue_articles_enrichment(
    request: Request, body: ArticleEnrichmentBatchRequest
):
    user = kutils.current_user(request)
    return await FOODSCHOLAR.enqueue_articles_enrichment(
        {
            "urns": body.urns,
            "force": body.force,
            "requested_by": user["sub"],
        }
    )


@router.get("/enrich/overview", dependencies=[Depends(auth("admin,expert"))])
@render()
async def get_enrichment_overview(request: Request):
    """Corpus-wide enrichment coverage with per-journal breakdown."""
    return await FOODSCHOLAR.get_enrichment_overview()


@router.post(
    "/enrich/batches",
    dependencies=[Depends(auth("admin,expert"))],
    status_code=202,
)
@render()
async def enqueue_enrichment_batch_by_criteria(
    request: Request, body: ArticleEnrichmentCriteriaBatchRequest
):
    """Queue an enrichment batch by criteria (journal, missing-only, limit)."""
    user = kutils.current_user(request)
    return await FOODSCHOLAR.enqueue_enrichment_batch(
        {
            "venue": body.venue,
            "only_missing": body.only_missing,
            "force": body.force,
            "limit": body.limit,
            "requested_by": user["sub"],
        }
    )


@router.get("/enrich/batches", dependencies=[Depends(auth("admin,expert"))])
@render()
async def list_enrichment_batches(request: Request):
    """Recent criteria batches, newest first."""
    return await FOODSCHOLAR.list_enrichment_batches()


@router.get(
    "/enrich/batches/{batch_id}", dependencies=[Depends(auth("admin,expert"))]
)
@render()
async def get_enrichment_batch(request: Request, batch_id: str):
    """One batch's live progress."""
    return await FOODSCHOLAR.get_enrichment_batch(batch_id)


@router.get("/enrich/jobs", dependencies=[Depends(auth("admin,expert"))])
@render()
async def get_articles_enrichment_status(
    request: Request,
    urns: List[str] = Query(
        default=[], description="Article URNs to look up (repeat per URN)"
    ),
):
    return await FOODSCHOLAR.get_article_enrichment_statuses(urns)


@router.get("/enrich/worker", dependencies=[Depends(auth("admin,expert"))])
@render()
async def get_enrichment_worker_status(request: Request):
    return await FOODSCHOLAR.get_enrichment_worker_status()


@router.post("/enrich/worker/pause", dependencies=[Depends(auth("admin,expert"))])
@render()
async def set_enrichment_sweeper_paused(
    request: Request, body: EnrichmentSweeperPauseRequest
):
    return await FOODSCHOLAR.set_enrichment_sweeper_paused(body.paused)


@router.post("/enrich/worker/restart", dependencies=[Depends(auth("admin"))])
@render()
async def restart_enrichment_workers(
    request: Request, body: EnrichmentWorkerRestartRequest
):
    """
    Force the enrichment workers back into a running state.

    Admin-only rather than admin,expert: this rebuilds worker threads and
    clears a pause another operator may have set deliberately.
    """
    return await FOODSCHOLAR.restart_enrichment_workers(body.model_dump())


@router.post(
    "/enrich/articles/{urn:path}",
    dependencies=[Depends(auth("admin,expert"))],
    status_code=202,
)
@render()
async def enqueue_article_enrichment(
    request: Request, urn: str, body: Optional[ArticleEnrichmentRequest] = None
):
    user = kutils.current_user(request)
    return await FOODSCHOLAR.enqueue_article_enrichment(
        urn,
        {
            "force": bool(body.force) if body else False,
            "requested_by": user["sub"],
        },
    )


@router.get(
    "/enrich/articles/{urn:path}", dependencies=[Depends(auth("admin,expert"))]
)
@render()
async def get_article_enrichment_status(request: Request, urn: str):
    return await FOODSCHOLAR.get_article_enrichment_status(urn)


@router.delete(
    "/enrich/articles/{urn:path}", dependencies=[Depends(auth("admin,expert"))]
)
@render()
async def reset_article_enrichment(request: Request, urn: str):
    return await FOODSCHOLAR.reset_article_enrichment(urn)


@router.post(
    "/qa/ask",
    dependencies=[Depends(auth()), Depends(guest_budget("qa"))],
)
@render()
async def ask_question(request: Request, body: QARequest):
    user = kutils.current_user(request)
    payload = body.model_copy(update={"user_id": user["sub"]})

    if payload.member_id:
        await verify_member_access(request, payload.member_id)

    return await FOODSCHOLAR.ask_question(payload.model_dump(exclude_none=True))


@router.post(
    "/qa/ask/stream",
    dependencies=[Depends(auth()), Depends(guest_budget("qa"))],
)
async def ask_question_stream(request: Request, body: QARequest):
    """
    Streaming QA: proxies FoodScholar's agentic pipeline as Server-Sent Events.

    The stream narrates the pipeline (`step` events for collapsible reasoning
    steps, `stage.*` detail events, `answer_delta` token chunks, `citations`)
    and terminates with `done`, `clarification`, or `error`. Frames pass
    through unbuffered; no APIEnvelope wrapping.
    """
    user = kutils.current_user(request)
    payload = body.model_copy(update={"user_id": user["sub"]})

    if payload.member_id:
        await verify_member_access(request, payload.member_id)

    upstream = FOODSCHOLAR.ask_question_stream(
        payload.model_dump(exclude_none=True)
    )
    # Prime the stream: an upstream connection/HTTP failure surfaces here as a
    # normal error response instead of a dead 200 stream.
    try:
        first_chunk = await upstream.__anext__()
    except StopAsyncIteration:
        first_chunk = b""

    async def frames():
        if first_chunk:
            yield first_chunk
        async for chunk in upstream:
            yield chunk

    return StreamingResponse(
        frames(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ---------------------------------------------------------------- review ----
#
# admin+expert, not admin-only: reviewing whether an answer was any good is the
# expert's job, and it is the reason this whole surface exists. That is a
# narrower grant than raw Langfuse traces, which stay admin-only — those carry
# every internal agent prompt for whoever was using the platform, where these
# are scoped to questions asked of FoodScholar and carry consent-aware
# identity.
#
# Every read is recorded, because "who looked at the userbase's questions" is
# exactly the kind of privileged action the platform has never kept a record of.


@router.get("/qa/requests", dependencies=[Depends(auth("admin,expert"))])
@render()
async def list_qa_requests(
    request: Request,
    limit: int = 50,
    offset: int = 0,
    user_id: Optional[str] = None,
    member_id: Optional[str] = None,
    correlation_id: Optional[str] = None,
    language: Optional[str] = None,
    mode: Optional[str] = None,
    search: Optional[str] = None,
    has_feedback: Optional[bool] = None,
    negative_only: bool = False,
):
    """Questions that were asked, newest first, with their feedback counts."""
    params = {
        "limit": limit,
        "offset": offset,
        "user_id": user_id,
        "member_id": member_id,
        "correlation_id": correlation_id,
        "language": language,
        "mode": mode,
        "search": search,
        "has_feedback": has_feedback,
        "negative_only": negative_only,
    }
    result = await FOODSCHOLAR.list_qa_requests(
        {k: v for k, v in params.items() if v is not None}
    )
    RECORDER.record_event(
        "expert.qa_reviewed",
        app="console",
        props={
            "scope": "list",
            "returned": len((result or {}).get("items") or []),
            "negative_only": bool(negative_only),
            "filtered_user": bool(user_id),
        },
    )
    return result


@router.get("/qa/requests/{request_id}", dependencies=[Depends(auth("admin,expert"))])
@render()
async def get_qa_request(request: Request, request_id: str):
    """One question with its answers, sources, pipeline trace and feedback."""
    result = await FOODSCHOLAR.get_qa_request(request_id)
    RECORDER.record_event(
        "expert.qa_reviewed",
        app="console",
        props={"scope": "detail", "qa_request_id": request_id},
    )
    return result


@router.get("/qa/feedback/list", dependencies=[Depends(auth("admin,expert"))])
@render()
async def list_qa_feedback(
    request: Request,
    limit: int = 50,
    offset: int = 0,
    negative_only: bool = False,
    user_id: Optional[str] = None,
):
    """Feedback received on answers, newest first, each with its question."""
    params = {
        "limit": limit,
        "offset": offset,
        "negative_only": negative_only,
        "user_id": user_id,
    }
    result = await FOODSCHOLAR.list_qa_feedback(
        {k: v for k, v in params.items() if v is not None}
    )
    RECORDER.record_event(
        "expert.feedback_reviewed",
        app="console",
        props={"returned": len((result or {}).get("items") or [])},
    )
    return result


@router.post("/qa/feedback", dependencies=[Depends(auth())])
@render()
async def submit_feedback(request: Request, body: QAFeedbackRequest):
    # Stamped here rather than accepted from the body, for the same reason
    # `/qa/ask` does it: the token is the only trustworthy source of who is
    # speaking, and feedback nobody can be attributed to cannot be reviewed.
    # `QAFeedbackRequest` deliberately has no `user_id` field, so a client
    # cannot claim to be someone else.
    user = kutils.current_user(request)
    payload = body.model_dump(exclude_none=True)
    payload["user_id"] = user["sub"]
    member_id = context.get_member_id()
    if member_id:
        payload["member_id"] = member_id
    return await FOODSCHOLAR.submit_qa_feedback(payload)


@router.post("/qa/memory", dependencies=[Depends(auth())])
@render()
async def decide_memory(request: Request, body: FoodChatMemoryDecisionRequest):
    """Accept/decline a memory nudge from a FoodScholar QA answer."""
    await verify_member_access(request, body.member_id)
    return await FOODSCHOLAR.submit_memory_decision(body.model_dump())


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
async def list_qa_tips(
    request: Request,
    member_id: Optional[str] = None,
    language: Optional[str] = None,
):
    # The tip is generated, so it is only in the reader's language if we say
    # which one. Falls back to the locale the browser already sends on every
    # request, so a caller that forgets the parameter still gets it right.
    return await FOODSCHOLAR.get_tips(
        member_id=member_id,
        language=language or context.get_locale() or "en",
    )


@router.get("/guidelines/storage/{artifact_uuid}", dependencies=[Depends(auth())])
@render()
async def get_guideline_storage(request: Request, artifact_uuid: str):
    return await FOODSCHOLAR.get_guideline_storage(artifact_uuid)


@router.post(
    "/guidelines/extract/{artifact_uuid}",
    dependencies=[Depends(auth()), Depends(deny_guests)],
)
@render()
async def enqueue_guideline_extraction(
    request: Request,
    artifact_uuid: str,
    body: GuidelineExtractionRequest | None = None,
):
    # The body is optional so existing callers keep working, but passing
    # guide_id is what gives every extracted rule its population context.
    payload = body.model_dump(exclude_none=True) if body else {}
    return await FOODSCHOLAR.enqueue_guideline_extraction(artifact_uuid, payload)


@router.get(
    "/guidelines/worker",
    dependencies=[Depends(auth("admin,expert"))],
)
@render()
async def get_guideline_worker_status(request: Request):
    """Extraction worker stats and queue depth."""
    return await FOODSCHOLAR.get_guideline_worker_status()


@router.get("/guidelines/extract/{artifact_uuid}", dependencies=[Depends(auth())])
@render()
async def get_guideline_extraction_status(request: Request, artifact_uuid: str):
    return await FOODSCHOLAR.get_guideline_extraction_status(artifact_uuid)


@router.post(
    "/guidelines/import/{artifact_uuid}",
    dependencies=[Depends(auth()), Depends(deny_guests)],
)
@render()
async def import_guidelines(
    request: Request, artifact_uuid: str, body: GuidelineImportRequest
):
    return await FOODSCHOLAR.import_guidelines(
        artifact_uuid, body.model_dump(exclude_none=True)
    )


# --------------------------------------------------------------------------- #
# Guideline facet enrichment
#
# Reads are open to curators, who need to see what enrichment proposed before
# trusting it. Writes queue corpus-wide model work, so they are admin-only.
# --------------------------------------------------------------------------- #


@router.post(
    "/guidelines/enrichment/preview",
    dependencies=[Depends(auth("admin,expert")), Depends(deny_guests)],
)
@render()
async def preview_guideline_enrichment(
    request: Request, body: GuidelineEnrichmentPreviewRequest
):
    return await FOODSCHOLAR.preview_guideline_enrichment(
        body.model_dump(exclude_none=True)
    )


@router.post(
    "/guidelines/enrichment/enqueue",
    dependencies=[Depends(auth("admin")), Depends(deny_guests)],
)
@render()
async def enqueue_guideline_enrichment(
    request: Request, body: GuidelineEnrichmentEnqueueRequest | None = None
):
    payload = body.model_dump(exclude_none=True) if body else {}
    return await FOODSCHOLAR.enqueue_guideline_enrichment(payload)


@router.get(
    "/guidelines/enrichment/status",
    dependencies=[Depends(auth("admin,expert"))],
)
@render()
async def get_guideline_enrichment_status(request: Request):
    return await FOODSCHOLAR.get_guideline_enrichment_status()


@router.get(
    "/guidelines/enrichment/worker",
    dependencies=[Depends(auth("admin,expert"))],
)
@render()
async def get_guideline_enrichment_worker_status(request: Request):
    return await FOODSCHOLAR.get_guideline_enrichment_worker_status()


# --------------------------------------------------------------------------- #
# Guideline corpus state and activation
#
# Retrieval only surfaces active guidelines, so activation is what puts a rule
# in front of users — an admin decision, and previewable before it is made.
# --------------------------------------------------------------------------- #


@router.get(
    "/guidelines/corpus/audit",
    dependencies=[Depends(auth("admin,expert"))],
)
@render()
async def audit_guideline_corpus(request: Request):
    return await FOODSCHOLAR.audit_guideline_corpus()


@router.get(
    "/guidelines/corpus/activation-plan",
    dependencies=[Depends(auth("admin,expert"))],
)
@render()
async def get_guideline_activation_plan(
    request: Request, require_verified: bool = True
):
    return await FOODSCHOLAR.get_guideline_activation_plan(
        require_verified=require_verified
    )


@router.post(
    "/guidelines/corpus/page-summaries/{guide_urn:path}",
    dependencies=[Depends(auth("admin,expert")), Depends(deny_guests)],
)
@render()
async def backfill_guide_page_summaries(
    request: Request, guide_urn: str, dry_run: bool = True
):
    """Backfill extraction page summaries onto a guide's existing rules."""
    return await FOODSCHOLAR.backfill_guide_page_summaries(
        guide_urn, dry_run=dry_run
    )


@router.post(
    "/guidelines/corpus/activate/{guide_urn:path}",
    dependencies=[Depends(auth("admin")), Depends(deny_guests)],
)
@render()
async def activate_guide_guidelines(
    request: Request,
    guide_urn: str,
    require_verified: bool = True,
    dry_run: bool = True,
):
    return await FOODSCHOLAR.activate_guide_guidelines(
        guide_urn,
        require_verified=require_verified,
        dry_run=dry_run,
    )


# ---------------------------------------------------------------------------
# Source Integrator
#
# Every route here is admin-or-expert. The integrator researches sources, can
# reach the open web, and — from Phase 2 — writes to the catalog; it is not a
# participant-facing surface and must never become one by a missing dependency.
#
# `user_sub` is taken from the token and put in the body, never accepted from
# the client: FoodScholar trusts whatever subject it is handed, so the moment
# a caller could name their own, one curator could read another's sessions.
# ---------------------------------------------------------------------------

class IntegratorChatBody(BaseModel):
    message: str = Field(min_length=1, max_length=8000)


class IntegratorSessionBody(BaseModel):
    title: Optional[str] = Field(default=None, max_length=300)


class IntegratorProposalBody(BaseModel):
    session_id: Optional[str] = None
    kind: str
    title: str = Field(max_length=500)
    source_url: Optional[str] = None
    country: Optional[str] = None
    language: Optional[str] = None
    population_group: Optional[str] = None
    licence: Optional[str] = None
    rationale: Optional[str] = None


class IntegratorApproveBody(BaseModel):
    override_reason: Optional[str] = Field(default=None, max_length=1000)


class IntegratorRejectBody(BaseModel):
    reason: str = Field(default="", max_length=1000)


class IntegratorRerankBody(BaseModel):
    order: List[str]


class IntegratorIntegrateBody(BaseModel):
    dry_run: bool = False


def _integrator_sub(request: Request) -> str:
    return kutils.current_user(request)["sub"]


def _integrator_is_admin(request: Request) -> bool:
    """Whether this caller may read the whole audit trail, not only their own.

    Reads the same claim the route's own gate read, through the same
    extractor, so "admin" cannot come to mean two things in one request.
    """
    from auth import _extract_roles

    user = kutils.current_user(request) or {}
    return "admin" in _extract_roles(user)


def _integrator_token(request: Request) -> Optional[str]:
    """The caller's own bearer, to be forwarded to the agent.

    The agent reads and writes the catalog on this person's behalf, so it does
    so with their token: the catalog applies their roles and refuses them
    exactly where it would refuse them directly. Without this the agent would
    act as a service account, and an expert who may not edit guides could edit
    one by asking for it.

    Taken from the request that this route already authenticated, so it is the
    token whose `sub` became `user_sub` above — the two cannot disagree.
    """
    header = request.headers.get("authorization") or ""
    scheme, _, token = header.partition(" ")
    return token.strip() if scheme.lower() == "bearer" and token.strip() else None


@router.post("/integrator/sessions", dependencies=[Depends(auth("admin,expert"))])
@render()
async def integrator_create_session(request: Request, body: IntegratorSessionBody):
    """Start a conversation with the source integrator."""
    return await FOODSCHOLAR.integrator_create_session(
        {"user_sub": _integrator_sub(request), "title": body.title})


@router.get("/integrator/sessions", dependencies=[Depends(auth("admin,expert"))])
@render()
async def integrator_list_sessions(request: Request, limit: int = 50):
    """This curator's own conversations."""
    return await FOODSCHOLAR.integrator_list_sessions(_integrator_sub(request), limit)


@router.get("/integrator/sessions/{session_id}/history",
            dependencies=[Depends(auth("admin,expert"))])
@render()
async def integrator_history(request: Request, session_id: str):
    return await FOODSCHOLAR.integrator_history(session_id, _integrator_sub(request))


@router.post("/integrator/sessions/{session_id}/chat",
             dependencies=[Depends(auth("admin,expert"))])
@render()
async def integrator_chat(request: Request, session_id: str, body: IntegratorChatBody):
    """One turn. The model may search the web and read the catalog."""
    return await FOODSCHOLAR.integrator_chat(
        session_id, {"user_sub": _integrator_sub(request), "message": body.message},
        delegated_token=_integrator_token(request))


@router.get("/integrator/proposals", dependencies=[Depends(auth("admin,expert"))])
@render()
async def integrator_list_proposals(request: Request, session_id: Optional[str] = None,
                                    status: Optional[str] = None, limit: int = 100):
    params = {"limit": limit}
    if session_id:
        params["session_id"] = session_id
    if status:
        params["status"] = status
    return await FOODSCHOLAR.integrator_list_proposals(params)


@router.get("/integrator/proposals/{proposal_id}",
            dependencies=[Depends(auth("admin,expert"))])
@render()
async def integrator_get_proposal(request: Request, proposal_id: str):
    return await FOODSCHOLAR.integrator_get_proposal(proposal_id)


@router.post("/integrator/proposals", dependencies=[Depends(auth("admin,expert"))])
@render()
async def integrator_create_proposal(request: Request, body: IntegratorProposalBody):
    """Add a candidate source by hand."""
    return await FOODSCHOLAR.integrator_create_proposal(
        {**body.model_dump(exclude_none=True), "user_sub": _integrator_sub(request)})


@router.post("/integrator/proposals/{proposal_id}/approve",
             dependencies=[Depends(auth("admin,expert"))])
@render()
async def integrator_approve(request: Request, proposal_id: str,
                             body: IntegratorApproveBody):
    """Approve a proposal for integration.

    The only way a proposal becomes integratable, and it is a person doing it:
    there is no tool the model can call that reaches this.
    """
    return await FOODSCHOLAR.integrator_approve(
        proposal_id, {"user_sub": _integrator_sub(request),
                      "override_reason": body.override_reason})


@router.post("/integrator/proposals/{proposal_id}/reject",
             dependencies=[Depends(auth("admin,expert"))])
@render()
async def integrator_reject(request: Request, proposal_id: str,
                            body: IntegratorRejectBody):
    return await FOODSCHOLAR.integrator_reject(
        proposal_id, {"user_sub": _integrator_sub(request), "reason": body.reason})


@router.post("/integrator/proposals/rerank", dependencies=[Depends(auth("admin,expert"))])
@render()
async def integrator_rerank(request: Request, body: IntegratorRerankBody):
    """Put the proposals in the order the curator wants them."""
    return await FOODSCHOLAR.integrator_rerank(
        {"user_sub": _integrator_sub(request), "order": body.order})


@router.post("/integrator/proposals/{proposal_id}/integrate",
             dependencies=[Depends(auth("admin,expert"))])
@render()
async def integrator_integrate(request: Request, proposal_id: str,
                               body: IntegratorIntegrateBody):
    """Run an approved proposal into the catalog.

    Returns a run to poll rather than waiting: the guideline extraction behind
    it reads a PDF page by page and outlasts any sensible request timeout.
    """
    return await FOODSCHOLAR.integrator_integrate(
        proposal_id, {"user_sub": _integrator_sub(request), "dry_run": body.dry_run},
        delegated_token=_integrator_token(request))


@router.post("/integrator/sessions/{session_id}/chat/stream",
             dependencies=[Depends(auth("admin,expert"))])
async def integrator_chat_stream(request: Request, session_id: str,
                                 body: IntegratorChatBody):
    """The turn, streamed. Steps arrive as they happen rather than at the end.

    Not wrapped in `@render()`: this is `text/event-stream`, and there is no
    JSON envelope to build. Primed like the QA stream so an upstream failure
    surfaces as a normal error response rather than as a dead 200.
    """
    upstream = FOODSCHOLAR.integrator_chat_stream(
        session_id,
        {"user_sub": _integrator_sub(request), "message": body.message},
        delegated_token=_integrator_token(request),
    )
    try:
        first_chunk = await upstream.__anext__()
    except StopAsyncIteration:
        first_chunk = b""

    async def frames():
        if first_chunk:
            yield first_chunk
        async for chunk in upstream:
            yield chunk

    return StreamingResponse(
        frames(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/integrator/runs/{run_id}", dependencies=[Depends(auth("admin,expert"))])
@render()
async def integrator_run(request: Request, run_id: str):
    """One integration run and its timeline."""
    return await FOODSCHOLAR.integrator_run(run_id)


@router.get("/integrator/runs", dependencies=[Depends(auth("admin,expert"))])
@render()
async def integrator_runs(request: Request, proposal_id: Optional[str] = None,
                          limit: int = 20):
    """Every attempt at a proposal, newest first."""
    params: dict = {"limit": limit}
    if proposal_id:
        params["proposal_id"] = proposal_id
    return await FOODSCHOLAR.integrator_runs(params)


@router.get("/integrator/backlog", dependencies=[Depends(auth("admin,expert"))])
@render()
async def integrator_backlog(request: Request, kind: Optional[str] = None,
                             status: Optional[str] = None, limit: int = 100,
                             offset: int = 0):
    """The queue of candidate sources."""
    params = {"limit": limit, "offset": offset}
    if kind:
        params["kind"] = kind
    if status:
        params["status"] = status
    return await FOODSCHOLAR.integrator_backlog(params)


@router.get("/integrator/audit", dependencies=[Depends(auth("admin,expert"))])
@render()
async def integrator_audit(request: Request, session_id: Optional[str] = None,
                           proposal_id: Optional[str] = None, limit: int = 100):
    """Every tool the agent ran, with what it was given and what came back."""
    # A tool call carries the queries a curator typed and the URLs they were
    # chasing. That is their work, so an expert sees their own and an admin
    # sees everything — an audit trail nobody can read in full is not one.
    params = {"limit": limit, "user_sub": _integrator_sub(request),
              "is_admin": _integrator_is_admin(request)}
    if session_id:
        params["session_id"] = session_id
    if proposal_id:
        params["proposal_id"] = proposal_id
    return await FOODSCHOLAR.integrator_audit(params)


# --------------------------------------------------------------------------- #
# Knowledge graph browsing
#
# FoodScholar serves the graph from a denormalized browse index; this is a
# straight proxy in front of it. Three things are decided here rather than
# upstream, because they are the gateway's job and not the graph's:
#
#   who may look      Reads take a plain `auth()`: the graph is the corpus's
#                     table of contents, it carries no household or member
#                     data, and a guest who may ask a question may certainly
#                     see what the answer was drawn from. No guest_budget —
#                     these are Elasticsearch reads with no model behind them,
#                     so the thing budgets exist to ration is not being spent.
#
#   who may rebuild   Reindex is admin only. It reads the whole graph and
#                     rewrites an index.
#
#   which filters     Query parameters are forwarded verbatim. The filter
#                     vocabulary belongs to FoodScholar, which validates it and
#                     returns a readable 400; restating it here would mean
#                     every new filter needs two edits and silently does
#                     nothing after one.
# --------------------------------------------------------------------------- #


def _graph_params(request: Request) -> dict:
    """Query parameters as sent, minus the blanks.

    `multi_dict` flattening is deliberate: every graph filter is either single
    valued or comma-separated, so a repeated parameter is a client bug rather
    than a list, and taking the last one is the same thing FastAPI would do.
    """
    return {k: v for k, v in request.query_params.items() if v not in (None, "")}


@router.get("/graph/summary", dependencies=[Depends(auth())])
@render()
async def graph_summary(request: Request):
    """Size, facets and which build is being served.

    `built: false` is the honest answer before the projector has ever run, and
    the interface is expected to render it rather than treat it as an error.
    """
    return await FOODSCHOLAR.graph_summary()


@router.get("/graph/facets", dependencies=[Depends(auth())])
@render()
async def graph_facets(request: Request):
    """The Layer A facets, with how much of the graph sits in each."""
    return await FOODSCHOLAR.graph_facets()


@router.get("/graph/facets/{facet}/roots", dependencies=[Depends(auth())])
@render()
async def graph_facet_roots(request: Request, facet: str):
    """Top-level shelves of one facet — where a browse session starts."""
    return await FOODSCHOLAR.graph_facet_roots(facet, _graph_params(request))


@router.get("/graph/search", dependencies=[Depends(auth())])
@render()
async def graph_search(request: Request):
    """Search and filter the graph, with the filter panel's counts included."""
    return await FOODSCHOLAR.graph_search(_graph_params(request))


@router.get("/graph/suggest", dependencies=[Depends(auth())])
@render()
async def graph_suggest(request: Request):
    """Autocomplete over node labels."""
    return await FOODSCHOLAR.graph_suggest(_graph_params(request))


@router.get("/graph/filters", dependencies=[Depends(auth())])
@render()
async def graph_filters(request: Request):
    """Counts for the filter panel, scoped to the filters already applied."""
    return await FOODSCHOLAR.graph_filters(_graph_params(request))


@router.get("/graph/entities", dependencies=[Depends(auth())])
@render()
async def graph_entities(request: Request):
    """The linked ontology entities behind the corpus."""
    return await FOODSCHOLAR.graph_entities(_graph_params(request))


@router.get("/graph/entities/{ontology_id}", dependencies=[Depends(auth())])
@render()
async def graph_entity(request: Request, ontology_id: str):
    """One ontology entity."""
    return await FOODSCHOLAR.graph_entity(ontology_id)


@router.get("/graph/entities/{ontology_id}/chunks", dependencies=[Depends(auth())])
@render()
async def graph_entity_chunks(request: Request, ontology_id: str):
    """Passages that mention an entity."""
    return await FOODSCHOLAR.graph_entity_chunks(ontology_id, _graph_params(request))


# Node and card ids take the `path` converter. Theme ids are slash-separated
# (`foods/olive_oil/monounsaturated_fat_r1`) and card ids embed them; the
# server decodes the browser's `%2F` before routing, so a plain `{node_id}`
# made every theme and card a 404 that never reached a handler, while shelves
# (`foodon:…`) worked.
#
# `path` matches slashes, so the order below is load-bearing: the bare
# `/graph/nodes/{node_id:path}` comes last, or it takes `x/children` as an id.
# No real id ends in one of the sub-route names — theme ids end in `_r1`,
# `_m2` or `_g3` — so a sub-route cannot claim one in return.


@router.get("/graph/cards/{target_id:path}", dependencies=[Depends(auth())])
@render()
async def graph_card(request: Request, target_id: str):
    """The Layer C card describing a shelf or theme."""
    return await FOODSCHOLAR.graph_card(target_id)


@router.get("/graph/nodes/{node_id:path}/children", dependencies=[Depends(auth())])
@render()
async def graph_node_children(request: Request, node_id: str):
    """Child shelves of a shelf."""
    return await FOODSCHOLAR.graph_node_children(node_id, _graph_params(request))


@router.get("/graph/nodes/{node_id:path}/themes", dependencies=[Depends(auth())])
@render()
async def graph_node_themes(request: Request, node_id: str):
    """Themes discovered on a shelf."""
    return await FOODSCHOLAR.graph_node_themes(node_id, _graph_params(request))


@router.get("/graph/nodes/{node_id:path}/breadcrumb", dependencies=[Depends(auth())])
@render()
async def graph_node_breadcrumb(request: Request, node_id: str):
    """Ancestors of a node, root first."""
    return await FOODSCHOLAR.graph_node_breadcrumb(node_id)


@router.get("/graph/nodes/{node_id:path}/chunks", dependencies=[Depends(auth())])
@render()
async def graph_node_chunks(request: Request, node_id: str):
    """Evidence passages attached to a shelf or theme."""
    return await FOODSCHOLAR.graph_node_chunks(node_id, _graph_params(request))


@router.get("/graph/nodes/{node_id:path}", dependencies=[Depends(auth())])
@render()
async def graph_node(request: Request, node_id: str):
    """One node with everything its detail page needs, in one response."""
    return await FOODSCHOLAR.graph_node(node_id)


# ------------------------------------------------------------------ streams --


async def _graph_sse(path: str, params: dict) -> StreamingResponse:
    """Proxy one graph stream, primed so a dead upstream is not a dead 200.

    Same shape as the QA and integrator streams: pull the first chunk before
    returning, so an upstream connection failure or 4xx surfaces as a normal
    error response instead of a stream that opens and then says nothing.
    """
    upstream = FOODSCHOLAR.graph_stream(path, params)
    try:
        first_chunk = await upstream.__anext__()
    except StopAsyncIteration:
        first_chunk = b""

    async def frames():
        if first_chunk:
            yield first_chunk
        async for chunk in upstream:
            yield chunk

    return StreamingResponse(
        frames(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            # Without this an nginx-style ingress buffers the whole stream and
            # the progressive draw the endpoint exists for never happens.
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/graph/stream", dependencies=[Depends(auth())])
async def graph_stream(request: Request):
    """Stream the filtered graph as SSE, for drawing.

    Not wrapped in `@render()`: this is `text/event-stream` and there is no
    JSON envelope to build. Frames pass through untouched.
    """
    return await _graph_sse("/api/v1/graph/stream", _graph_params(request))


@router.get("/graph/stream/expand", dependencies=[Depends(auth())])
async def graph_stream_expand(request: Request):
    """Stream one node's neighborhood — parent, children, themes, card."""
    return await _graph_sse("/api/v1/graph/stream/expand", _graph_params(request))


# ------------------------------------------------------------------- admin --


@router.post("/graph/reindex", dependencies=[Depends(auth("admin"))])
@render()
async def graph_reindex(request: Request, drop_old: bool = True):
    """Rebuild the browse index from the graph.

    Admin only, and slow: it reads the whole graph. Until it runs after an
    offline build, the browse routes keep serving the previous projection,
    which is the behaviour you want — a stale graph beats no graph.
    """
    return await FOODSCHOLAR.graph_reindex({"drop_old": drop_old})
