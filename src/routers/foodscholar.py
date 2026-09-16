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
async def list_qa_tips(request: Request, member_id: Optional[str] = None):
    return await FOODSCHOLAR.get_tips(member_id=member_id)


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
        session_id, {"user_sub": _integrator_sub(request), "message": body.message})


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
        proposal_id, {"user_sub": _integrator_sub(request), "dry_run": body.dry_run})


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
    params = {"limit": limit}
    if session_id:
        params["session_id"] = session_id
    if proposal_id:
        params["proposal_id"] = proposal_id
    return await FOODSCHOLAR.integrator_audit(params)
