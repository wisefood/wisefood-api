from fastapi import APIRouter, Query, Request, Depends
from fastapi.responses import StreamingResponse
from routers.generic import render
from typing import List, Optional
import logging
from auth import auth
from schemas import (
    ArticleEnrichmentBatchRequest,
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


@router.post("/qa/feedback", dependencies=[Depends(auth())])
@render()
async def submit_feedback(request: Request, body: QAFeedbackRequest):
    return await FOODSCHOLAR.submit_qa_feedback(body.model_dump(exclude_none=True))


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
