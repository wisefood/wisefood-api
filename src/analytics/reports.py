"""Reading the activity record back.

The write path is deliberately dumb — snapshot, queue, insert — so everything
that needs a join, a count or a window lives here instead.

The first report is the one a user can trigger themselves: the page footer shows
them a session id, and quoting it to support has to lead somewhere. That is what
:func:`session_summary` is for. Reading it stays admin- and expert-only; what the
user gets is the id, not the data behind it.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import Numeric, func, select
from sqlalchemy.dialects.postgresql import JSONB

logger = logging.getLogger(__name__)

MAX_TIMELINE = 500

#: Rating values that count as a complaint. Spelled once: the same list was
#: written out at five call sites, so adding a scale meant remembering all five.
_NEGATIVE_VALUES = ("down", "not_helpful", "bad", "awful")



async def session_summary(
    session_id: str, *, timeline_limit: int = 100, timeline_offset: int = 0
) -> Dict[str, Any]:
    """Everything one browser session did, counted and in order.

    Answers the questions the requirement actually names — how many searches,
    how many questions, how many meal plans — without the caller having to know
    which table each of those lives in.
    """
    from backend.postgres import POSTGRES_ASYNC_SESSION_FACTORY
    from sql import ActivityEvent, FeedbackRecord, LLMUsage, SearchQuery

    limit = max(1, min(int(timeline_limit or 100), MAX_TIMELINE))
    start = max(0, int(timeline_offset or 0))

    async with POSTGRES_ASYNC_SESSION_FACTORY()() as db:
        counts_by_type = (
            await db.execute(
                select(ActivityEvent.event_type, func.count())
                .where(ActivityEvent.client_session_id == session_id)
                .group_by(ActivityEvent.event_type)
            )
        ).all()

        span = (
            await db.execute(
                select(
                    func.min(ActivityEvent.occurred_at),
                    func.max(ActivityEvent.occurred_at),
                    func.count(func.distinct(ActivityEvent.user_id)),
                    func.count().filter(ActivityEvent.status >= 400),
                    func.count().filter(ActivityEvent.status >= 500),
                    func.percentile_disc(0.95).within_group(ActivityEvent.duration_ms),
                ).where(ActivityEvent.client_session_id == session_id)
            )
        ).one()

        # The ids themselves, not just how many. A count told the reader that
        # the session belonged to somebody and refused to say who, so the page
        # could not link on to that person's other sessions — which is the
        # first thing anyone wants after reading one.
        # One row per person, not per (person, household member) pair. A
        # household with three members produced the same user id three times
        # on the page, which read as three people. The member ids are kept as
        # a list against the one person they belong to.
        who = (
            await db.execute(
                select(
                    ActivityEvent.user_id,
                    func.array_agg(func.distinct(ActivityEvent.member_id)).filter(
                        ActivityEvent.member_id.isnot(None)
                    ),
                    func.count(),
                )
                .where(
                    ActivityEvent.client_session_id == session_id,
                    (ActivityEvent.user_id.isnot(None))
                    | (ActivityEvent.member_id.isnot(None)),
                )
                .group_by(ActivityEvent.user_id)
                .order_by(func.count().desc())
                .limit(10)
            )
        ).all()

        apps = (
            await db.execute(
                select(ActivityEvent.app, func.count())
                .where(ActivityEvent.client_session_id == session_id)
                .group_by(ActivityEvent.app)
                .order_by(func.count().desc())
            )
        ).all()

        # The individual model calls, so a costly session can be explained
        # rather than merely totalled.
        llm_rows = (
            await db.execute(
                select(
                    LLMUsage.occurred_at,
                    LLMUsage.app,
                    LLMUsage.feature,
                    LLMUsage.model,
                    LLMUsage.input_tokens,
                    LLMUsage.output_tokens,
                    LLMUsage.total_tokens,
                    LLMUsage.cost_usd,
                    LLMUsage.latency_ms,
                    LLMUsage.trace_id,
                )
                .where(LLMUsage.client_session_id == session_id)
                .order_by(LLMUsage.occurred_at.asc())
                .limit(200)
            )
        ).all()

        searches = (
            await db.execute(
                select(
                    func.count(),
                    func.count().filter(SearchQuery.zero_result.is_(True)),
                ).where(SearchQuery.client_session_id == session_id)
            )
        ).one()

        # Listed, not just counted. "3 pieces of feedback" is a number; the
        # words somebody typed are the reason anyone opened this session.
        feedback_rows = (
            (
                await db.execute(
                    select(FeedbackRecord)
                    .where(FeedbackRecord.client_session_id == session_id)
                    .order_by(FeedbackRecord.occurred_at.asc())
                )
            )
            .scalars()
            .all()
        )

        # Same for searches: the queries themselves, with whether each one had
        # to be loosened to return anything.
        search_rows = (
            (
                await db.execute(
                    select(SearchQuery)
                    .where(SearchQuery.client_session_id == session_id)
                    .order_by(SearchQuery.occurred_at.asc())
                    .limit(MAX_TIMELINE)
                )
            )
            .scalars()
            .all()
        )

        usage = (
            await db.execute(
                select(
                    func.coalesce(func.sum(LLMUsage.total_tokens), 0),
                    func.coalesce(func.sum(LLMUsage.cost_usd), 0),
                    func.count(),
                ).where(LLMUsage.client_session_id == session_id)
            )
        ).one()

        # How many actions there are in total, so the page can say "100 of 4,312"
        # rather than a silent "truncated" footnote — the previous version gave
        # no way to reach action 101 at all.
        timeline_total = await db.scalar(
            select(func.count()).where(ActivityEvent.client_session_id == session_id)
        )

        timeline_rows = (
            (
                await db.execute(
                    select(ActivityEvent)
                    .where(ActivityEvent.client_session_id == session_id)
                    .order_by(ActivityEvent.occurred_at.asc(), ActivityEvent.id.asc())
                    .limit(limit)
                    .offset(start)
                )
            )
            .scalars()
            .all()
        )

    by_type = {event_type: int(count) for event_type, count in counts_by_type}
    started_at, ended_at, distinct_users, errors, server_errors, slowest = span

    from analytics.people import resolve_people

    people = await resolve_people(user_id for user_id, _members, _count in who)

    return {
        "session_id": session_id,
        "started_at": started_at.isoformat() if started_at else None,
        "ended_at": ended_at.isoformat() if ended_at else None,
        "events": sum(by_type.values()),
        "events_by_type": by_type,
        # Named counts, so a console does not have to know which event type
        # means "asked a question".
        "questions_asked": by_type.get("qa.ask", 0) + by_type.get("qa.answered", 0),
        "chat_turns": by_type.get("chat.turn", 0) + by_type.get("chat.message", 0),
        "meal_plans_generated": by_type.get("chat.plan_generated", 0),
        "meal_plans_saved": by_type.get("chat.plan_saved", 0),
        "recipes_viewed": by_type.get("recipe.view", 0),
        "searches": int(searches[0] or 0),
        "searches_with_no_results": int(searches[1] or 0),
        "feedback_given": len(feedback_rows),
        "feedback": [row.to_dict() for row in feedback_rows],
        "searches_performed": [
            {
                "occurred_at": row.occurred_at.isoformat() if row.occurred_at else None,
                "surface": row.surface,
                # raw_query is absent for anyone who did not consent to it
                # being kept; the normalised form is always there.
                "query": row.raw_query or row.normalized_query,
                "results": row.result_count_final,
                "zero_result": bool(row.zero_result),
                # Recorded since the first release and never shown: a search
                # that only returned anything because the constraints were
                # loosened is a different outcome from one that just worked.
                "relaxed": bool(row.relaxed),
                "lexical_fallback": bool(row.lexical_fallback),
                "latency_ms": row.latency_ms,
            }
            for row in search_rows
        ],
        "llm_calls": int(usage[2] or 0),
        "total_tokens": int(usage[0] or 0),
        "cost_usd": float(usage[1] or 0),
        # Zero when nobody in the session had consented; the counts above still
        # hold, because they never needed an identity.
        "identified_users": int(distinct_users or 0),
        # Named, not just identified. A subject reaches this row only because
        # the person consented to being named, and then showing them as a UUID
        # withholds the one thing they agreed to. Resolved here, for this one
        # session — deliberately not on the board of fifty, where a name per
        # row is a Keycloak call per row and nobody reads rows that closely.
        "users": [
            {
                "user_id": user_id,
                # First member for the field the UI already renders, the whole
                # list for anything that wants it.
                "member_id": (members or [None])[0],
                "member_ids": list(members or []),
                "events": int(count),
                **{
                    k: v
                    for k, v in people.get(user_id or "", {}).items()
                    if k in ("display_name", "username", "household_name", "resolved")
                },
            }
            for user_id, members, count in who
        ],
        "apps": [{"app": app, "events": int(count)} for app, count in apps],
        "duration_seconds": (
            int((ended_at - started_at).total_seconds())
            if started_at and ended_at
            else None
        ),
        # A session where things went wrong is the one worth reading, and the
        # page that exists to explain what went wrong for somebody was showing
        # no errors at all.
        "errors": int(errors or 0),
        "server_errors": int(server_errors or 0),
        "slowest_request_ms": int(slowest) if slowest is not None else None,
        "llm_calls_detail": [
            {
                "occurred_at": at.isoformat() if at else None,
                "app": app,
                "feature": feature,
                "model": model,
                "input_tokens": inp,
                "output_tokens": out,
                "total_tokens": total,
                "cost_usd": float(cost) if cost is not None else None,
                "latency_ms": latency,
                "trace_id": trace,
            }
            for at, app, feature, model, inp, out, total, cost, latency, trace in llm_rows
        ],
        "timeline": _timeline(timeline_rows),
        "timeline_total": int(timeline_total or 0),
        "timeline_offset": start,
        "timeline_limit": limit,
        "timeline_truncated": (start + len(timeline_rows)) < int(timeline_total or 0),
    }


async def recent_sessions(
    *, limit: int = 50, user_id: Optional[str] = None, days: int = 30
) -> List[Dict[str, Any]]:
    """The most recent sessions, newest first, within a time window.

    Windowed on purpose: an unbounded GROUP BY over the whole event table on
    every console load would become the slowest query in the application by the
    end of the first year. Only sessions that carry an identity can be filtered
    by user — an unconsented session is still counted, it just cannot be
    attributed.
    """
    from backend.postgres import POSTGRES_ASYNC_SESSION_FACTORY
    from sql import ActivityEvent

    size = max(1, min(int(limit or 50), 200))
    since = datetime.now(timezone.utc) - timedelta(days=max(1, min(int(days or 30), 365)))
    query = (
        select(
            ActivityEvent.client_session_id,
            func.min(ActivityEvent.occurred_at).label("started_at"),
            func.max(ActivityEvent.occurred_at).label("ended_at"),
            func.count().label("events"),
            func.max(ActivityEvent.user_id).label("user_id"),
        )
        .where(ActivityEvent.client_session_id.isnot(None))
        .where(ActivityEvent.occurred_at >= since)
        .group_by(ActivityEvent.client_session_id)
        .order_by(func.max(ActivityEvent.occurred_at).desc())
        .limit(size)
    )
    if user_id:
        query = query.where(ActivityEvent.user_id == user_id)

    async with POSTGRES_ASYNC_SESSION_FACTORY()() as db:
        rows = (await db.execute(query)).all()

    return [
        {
            "session_id": row.client_session_id,
            "started_at": row.started_at.isoformat() if row.started_at else None,
            "ended_at": row.ended_at.isoformat() if row.ended_at else None,
            "events": int(row.events or 0),
            "user_id": row.user_id,
        }
        for row in rows
    ]

# ---------------------------------------------------------------- overview --
async def overview(*, days: int = 7, since: Optional[str] = None, until: Optional[str] = None) -> Dict[str, Any]:
    """The numbers the console's front page needs, in one round trip.

    One call rather than eight, because the alternative is a dashboard that
    fires eight queries on load and shows eight separate spinners.
    """
    from backend.postgres import POSTGRES_ASYNC_SESSION_FACTORY
    from sql import ActivityEvent, FeedbackRecord, LLMUsage, SearchQuery

    window = _window(days, since, until)
    since = window.since

    async with POSTGRES_ASYNC_SESSION_FACTORY()() as db:
        totals = (
            await db.execute(
                select(
                    func.count(),
                    func.count(func.distinct(ActivityEvent.user_id)),
                    func.count(func.distinct(ActivityEvent.client_session_id)),
                ).where(ActivityEvent.occurred_at >= since)
            )
        ).one()

        by_app = (
            await db.execute(
                select(ActivityEvent.app, func.count())
                .where(ActivityEvent.occurred_at >= since)
                .group_by(ActivityEvent.app)
                .order_by(func.count().desc())
            )
        ).all()

        by_day = (
            await db.execute(
                select(
                    func.date_trunc("day", ActivityEvent.occurred_at).label("day"),
                    func.count(),
                    func.count(func.distinct(ActivityEvent.user_id)),
                )
                .where(ActivityEvent.occurred_at >= since)
                .group_by("day")
                .order_by("day")
            )
        ).all()

        searches = (
            await db.execute(
                select(
                    func.count(),
                    func.count().filter(SearchQuery.zero_result.is_(True)),
                ).where(SearchQuery.occurred_at >= since)
            )
        ).one()

        feedback = (
            await db.execute(
                select(
                    func.count(),
                    func.count().filter(FeedbackRecord.status == "new"),
                    func.count().filter(
                        FeedbackRecord.rating_value.in_(
                            _NEGATIVE_VALUES
                        )
                    ),
                ).where(FeedbackRecord.occurred_at >= since)
            )
        ).one()

        usage = (
            await db.execute(
                select(
                    func.coalesce(func.sum(LLMUsage.total_tokens), 0),
                    func.coalesce(func.sum(LLMUsage.cost_usd), 0),
                    func.count(),
                ).where(LLMUsage.occurred_at >= since)
            )
        ).one()

        guests = await db.scalar(
            select(func.count(func.distinct(ActivityEvent.client_session_id))).where(
                ActivityEvent.occurred_at >= since, ActivityEvent.is_guest.is_(True)
            )
        )

    previous = await _totals_for(window.previous_since, since)

    searches_total = int(searches[0] or 0)
    feedback_total = int(feedback[0] or 0)

    # Why the per-person figures may read zero.
    #
    # Under opt-in, activity is counted but nobody is named until they agree,
    # so "identified users: 0" is the setting working rather than a fault —
    # and a bare zero beside a healthy session count reads exactly like one.
    # The console needs the mode and the number of people who have agreed in
    # order to tell the reader which of the two it is looking at.
    from analytics.settings import CONSENT_GRANT, CONSENT_OPT_OUT, consent_mode
    from sql import UserConsent

    consented = 0
    try:
        async with POSTGRES_ASYNC_SESSION_FACTORY()() as db:
            rows = (
                await db.execute(
                    select(UserConsent.user_id, UserConsent.consent_type)
                    .where(
                        UserConsent.consent_type.in_([CONSENT_GRANT, CONSENT_OPT_OUT])
                    )
                    .order_by(UserConsent.granted_at.desc(), UserConsent.id.desc())
                )
            ).all()
        # The ledger is append-only, so a user's newest row is their current
        # answer — the same rule the write path applies.
        latest: Dict[str, str] = {}
        for user_id, consent_type in rows:
            latest.setdefault(user_id, consent_type)
        consented = sum(1 for kind in latest.values() if kind == CONSENT_GRANT)
    except Exception:
        # The ledger being absent is not a reason to fail the whole overview.
        logger.debug("analytics.consent_count_unavailable", exc_info=True)

    return {
        "consent": {
            "mode": consent_mode(),
            # People who have actively agreed to be named. Under opt-in this is
            # the ceiling on every per-person figure in the console.
            "consented_users": int(consented or 0),
        },
        # The same figures for the period before this one. A number with
        # nothing to compare it against cannot tell you whether to act.
        "previous": previous,
        **window,
        "events": int(totals[0] or 0),
        # Distinct *identified* users. Anyone who has not consented is counted
        # in `sessions` but cannot appear here, so this is a floor, not a total.
        "active_users": int(totals[1] or 0),
        "sessions": int(totals[2] or 0),
        "guest_sessions": int(guests or 0),
        "events_by_app": [{"app": app, "events": int(n)} for app, n in by_app],
        "daily": [
            {
                "day": day.date().isoformat() if hasattr(day, "date") else str(day),
                "events": int(events),
                "active_users": int(users),
            }
            for day, events, users in by_day
        ],
        "searches": searches_total,
        "searches_with_no_results": int(searches[1] or 0),
        "zero_result_rate": _ratio(int(searches[1] or 0), searches_total),
        "feedback": feedback_total,
        "feedback_new": int(feedback[1] or 0),
        "feedback_negative": int(feedback[2] or 0),
        "negative_feedback_rate": _ratio(int(feedback[2] or 0), feedback_total),
        "llm_calls": int(usage[2] or 0),
        "total_tokens": int(usage[0] or 0),
        "cost_usd": float(usage[1] or 0),
    }


async def _totals_for(since, until) -> Dict[str, Any]:
    """The handful of figures the overview compares between two periods."""
    from backend.postgres import POSTGRES_ASYNC_SESSION_FACTORY
    from sql import ActivityEvent, FeedbackRecord, LLMUsage, SearchQuery

    async with POSTGRES_ASYNC_SESSION_FACTORY()() as db:
        events, users, sessions = (
            await db.execute(
                select(
                    func.count(),
                    func.count(func.distinct(ActivityEvent.user_id)),
                    func.count(func.distinct(ActivityEvent.client_session_id)),
                ).where(
                    ActivityEvent.occurred_at >= since, ActivityEvent.occurred_at < until
                )
            )
        ).one()
        searches, zero = (
            await db.execute(
                select(
                    func.count(),
                    func.count().filter(SearchQuery.zero_result.is_(True)),
                ).where(
                    SearchQuery.occurred_at >= since, SearchQuery.occurred_at < until
                )
            )
        ).one()
        feedback, negative = (
            await db.execute(
                select(
                    func.count(),
                    func.count().filter(
                        FeedbackRecord.rating_value.in_(
                            _NEGATIVE_VALUES
                        )
                    ),
                ).where(
                    FeedbackRecord.occurred_at >= since,
                    FeedbackRecord.occurred_at < until,
                )
            )
        ).one()
        cost = await db.scalar(
            select(func.coalesce(func.sum(LLMUsage.cost_usd), 0)).where(
                LLMUsage.occurred_at >= since, LLMUsage.occurred_at < until
            )
        )
    return {
        "events": int(events or 0),
        "active_users": int(users or 0),
        "sessions": int(sessions or 0),
        "searches": int(searches or 0),
        "searches_with_no_results": int(zero or 0),
        "zero_result_rate": _ratio(int(zero or 0), int(searches or 0)),
        "feedback": int(feedback or 0),
        "feedback_negative": int(negative or 0),
        "negative_feedback_rate": _ratio(int(negative or 0), int(feedback or 0)),
        "cost_usd": float(cost or 0),
    }


async def attention(*, days: int = 7, since: Optional[str] = None, until: Optional[str] = None) -> Dict[str, Any]:
    """What is worth doing something about, with the number that justifies it.

    A dashboard of totals tells you the product is being used. This tells you
    where it is failing: the searches that found nothing, the complaints nobody
    has read, the answers an expert marked wrong and never followed up. Each
    item carries a count, a severity and the page that acts on it — a number
    with no action attached is decoration.
    """
    from backend.postgres import POSTGRES_ASYNC_SESSION_FACTORY
    from sql import ActivityEvent, ExpertReview, FeedbackRecord, LLMUsage, SearchQuery

    window = _window(days, since, until)
    since = window.since
    items: List[Dict[str, Any]] = []

    async with POSTGRES_ASYNC_SESSION_FACTORY()() as db:
        zero_total, zero_distinct = (
            await db.execute(
                select(func.count(), func.count(func.distinct(SearchQuery.query_hash)))
                .where(
                    SearchQuery.occurred_at >= since,
                    SearchQuery.zero_result.is_(True),
                )
            )
        ).one()
        worst = (
            await db.execute(
                select(func.min(SearchQuery.normalized_query), func.count())
                .where(
                    SearchQuery.occurred_at >= since,
                    SearchQuery.zero_result.is_(True),
                    SearchQuery.query_hash.isnot(None),
                )
                .group_by(SearchQuery.query_hash)
                .order_by(func.count().desc())
                .limit(1)
            )
        ).first()

        untriaged = await db.scalar(
            select(func.count()).where(
                FeedbackRecord.status == "new",
                FeedbackRecord.rating_value.in_(
                    _NEGATIVE_VALUES
                ),
            )
        )
        untriaged_all = await db.scalar(
            select(func.count()).where(FeedbackRecord.status == "new")
        )

        # Negative feedback on an answer that no expert has judged. The join is
        # the point: an unread complaint and one that was read and dismissed are
        # different states, and only the first needs someone.
        reviewed = select(ExpertReview.target_id).where(
            ExpertReview.target_type == "qa_answer"
        )
        unreviewed = await db.scalar(
            select(func.count()).where(
                FeedbackRecord.target_type == "qa_answer",
                FeedbackRecord.rating_value.in_(
                    _NEGATIVE_VALUES
                ),
                FeedbackRecord.target_id.isnot(None),
                FeedbackRecord.target_id.notin_(reviewed),
            )
        )

        stale_sessions = await db.scalar(
            select(func.count(func.distinct(ActivityEvent.client_session_id))).where(
                ActivityEvent.occurred_at >= since,
                ActivityEvent.user_id.is_(None),
                ActivityEvent.client_session_id.isnot(None),
            )
        )

        # FoodScholar says so explicitly when it answers a question and then
        # fails to store it. The signal has been arriving since the first
        # release and appearing in no report and on no page.
        persist_failed = await db.scalar(
            select(func.count()).where(
                ActivityEvent.occurred_at >= since,
                ActivityEvent.event_type == "qa.persist_failed",
            )
        )

        # Asked and never answered. Not proof of a fault on its own — a
        # question can straddle the window edge — but a standing gap is people
        # who got nothing back.
        asked, answered = (
            await db.execute(
                select(
                    func.count().filter(ActivityEvent.event_type == "qa.ask"),
                    func.count().filter(ActivityEvent.event_type == "qa.answered"),
                ).where(ActivityEvent.occurred_at >= since)
            )
        ).one()

        # An endpoint that fails for most callers is the one thing here a user
        # cannot report, because it stops them before they reach the feedback
        # button.
        failing = (
            await db.execute(
                select(
                    ActivityEvent.route,
                    func.count(),
                    func.count().filter(ActivityEvent.status >= 500),
                )
                .where(
                    ActivityEvent.occurred_at >= since,
                    ActivityEvent.event_type == "http.request",
                    ActivityEvent.route.isnot(None),
                )
                .group_by(ActivityEvent.route)
                .having(func.count().filter(ActivityEvent.status >= 500) > 0)
                .order_by(func.count().filter(ActivityEvent.status >= 500).desc())
                .limit(1)
            )
        ).first()

        # A model nobody has priced makes the spend figure quietly partial.
        unpriced = await db.scalar(
            select(func.count()).where(
                LLMUsage.occurred_at >= since, LLMUsage.cost_usd.is_(None)
            )
        )

    if zero_total:
        items.append(
            {
                "key": "zero_result",
                "severity": "warning" if zero_total < 50 else "error",
                "count": int(zero_total),
                "title": f"{int(zero_total)} searches found nothing",
                "detail": (
                    f"{int(zero_distinct or 0)} distinct queries"
                    + (f", most often \u201c{worst[0]}\u201d ({int(worst[1])}\u00d7)" if worst else "")
                ),
                "action": "Review the gaps",
                "to": "/console/insights/queries",
            }
        )
    if untriaged:
        items.append(
            {
                "key": "untriaged_negative",
                "severity": "error",
                "count": int(untriaged),
                "title": f"{int(untriaged)} negative comments not yet read",
                "detail": f"{int(untriaged_all or 0)} pieces of feedback are still marked new",
                "action": "Open the inbox",
                "to": "/console/insights/feedback",
            }
        )
    if unreviewed:
        items.append(
            {
                "key": "unreviewed_answers",
                "severity": "warning",
                "count": int(unreviewed),
                "title": f"{int(unreviewed)} criticised answers have no expert verdict",
                "detail": "Someone said the answer was unhelpful and no expert has judged it",
                "action": "Review answers",
                "to": "/console/insights/qa?negative=1",
            }
        )
    if failing:
        route, requests, server_errors = failing
        items.append(
            {
                "key": "failing_route",
                "severity": "error",
                "count": int(server_errors),
                "title": f"{int(server_errors)} server errors on {route}",
                "detail": (
                    f"{_ratio(int(server_errors), int(requests))}% of "
                    f"{int(requests)} requests to this endpoint crashed"
                ),
                "action": "See service health",
                "to": "/console/insights/performance",
            }
        )
    if persist_failed:
        items.append(
            {
                "key": "qa_persist_failed",
                "severity": "error",
                "count": int(persist_failed),
                "title": f"{int(persist_failed)} answers were not saved",
                "detail": (
                    "FoodScholar answered the question and then failed to store "
                    "it, so it is missing from review and from the user's history."
                ),
                "action": "See Q&A",
                "to": "/console/insights/qa",
            }
        )
    unanswered = max(int(asked or 0) - int(answered or 0), 0)
    if unanswered and int(asked or 0) >= 10 and unanswered / int(asked) > 0.1:
        items.append(
            {
                "key": "unanswered_questions",
                "severity": "warning",
                "count": unanswered,
                "title": f"{unanswered} questions produced no answer",
                "detail": (
                    f"{int(asked)} asked, {int(answered)} answered. Some of the gap "
                    "is questions still in flight at the window edge."
                ),
                "action": "See Q&A",
                "to": "/console/insights/qa",
            }
        )
    if unpriced:
        items.append(
            {
                "key": "unpriced_models",
                "severity": "info",
                "count": int(unpriced),
                "title": f"{int(unpriced)} model calls could not be priced",
                "detail": (
                    "Their model has no rate, so their tokens are counted and "
                    "their cost is not. Add a rate under model pricing."
                ),
                "action": "See model usage",
                "to": "/console/insights/usage",
            }
        )
    if stale_sessions:
        items.append(
            {
                "key": "unattributed",
                "severity": "info",
                "count": int(stale_sessions),
                "title": f"{int(stale_sessions)} sessions are unattributed",
                "detail": (
                    "Activity from people who have not agreed to be named. Counted "
                    "in totals, absent from per-person views — expected under opt-in."
                ),
                "action": "See people",
                "to": "/console/insights/users",
            }
        )

    return {**window, "items": items}


# ----------------------------------------------------------------- queries --
async def trending_queries(*, days: int = 7, limit: int = 20, since: Optional[str] = None, until: Optional[str] = None) -> Dict[str, Any]:
    """What people are searching for, and what is newly popular.

    "Rising" compares the window against the one before it. A query that ran
    twice yesterday and forty times today matters more than one that has run
    forty times a day for a month, and a raw top-N never shows it.
    """
    from backend.postgres import POSTGRES_ASYNC_SESSION_FACTORY
    from sql import SearchQuery

    window = _window(days, since, until)
    since, previous_since = window.since, window.previous_since
    size = _clamp(limit, 20, 100)

    async with POSTGRES_ASYNC_SESSION_FACTORY()() as db:
        current = (
            await db.execute(
                select(
                    SearchQuery.query_hash,
                    func.min(SearchQuery.normalized_query).label("query"),
                    func.count().label("searches"),
                    func.count(func.distinct(SearchQuery.client_session_id)),
                    func.count().filter(SearchQuery.zero_result.is_(True)),
                )
                .where(
                    SearchQuery.occurred_at >= since,
                    SearchQuery.query_hash.isnot(None),
                )
                .group_by(SearchQuery.query_hash)
                .order_by(func.count().desc())
                .limit(size)
            )
        ).all()

        previous = dict(
            (
                await db.execute(
                    select(SearchQuery.query_hash, func.count())
                    .where(
                        SearchQuery.occurred_at >= previous_since,
                        SearchQuery.occurred_at < since,
                        SearchQuery.query_hash.isnot(None),
                    )
                    .group_by(SearchQuery.query_hash)
                )
            ).all()
        )

    top = []
    for query_hash, query, searches, sessions, zero in current:
        before = int(previous.get(query_hash, 0) or 0)
        top.append(
            {
                "query": query,
                "query_hash": query_hash,
                "searches": int(searches),
                "sessions": int(sessions or 0),
                "zero_result": int(zero or 0),
                "previous": before,
                # None rather than infinity for a query that is new this
                # window: "no previous data" is not "up 100%".
                "change_pct": (
                    None if before == 0 else round(((int(searches) - before) / before) * 100, 1)
                ),
                "is_new": before == 0,
            }
        )
    rising = sorted(
        (row for row in top if row["searches"] >= 3),
        key=lambda row: (row["change_pct"] is None, row["change_pct"] or 0),
        reverse=True,
    )[:10]
    return {**window, "top": top, "rising": rising}


async def zero_result_queries(*, days: int = 7, limit: int = 20,
    offset: int = 0, since: Optional[str] = None, until: Optional[str] = None) -> Dict[str, Any]:
    """Searches that found nothing — the catalogue's to-do list."""
    from backend.postgres import POSTGRES_ASYNC_SESSION_FACTORY
    from sql import SearchQuery

    window = _window(days, since, until)
    size, start = _page(limit, offset, 20, 100)

    async with POSTGRES_ASYNC_SESSION_FACTORY()() as db:
        rows = (
            await db.execute(
                select(
                    func.min(SearchQuery.normalized_query),
                    func.count(),
                    func.count(func.distinct(SearchQuery.client_session_id)),
                    func.max(SearchQuery.occurred_at),
                    func.min(SearchQuery.surface),
                )
                .where(
                    SearchQuery.occurred_at >= window.since,
                    SearchQuery.zero_result.is_(True),
                    SearchQuery.query_hash.isnot(None),
                )
                .group_by(SearchQuery.query_hash)
                .order_by(func.count().desc())
                .limit(size)
                .offset(start)
            )
        ).all()

        total = await db.scalar(
            select(func.count(func.distinct(SearchQuery.query_hash))).where(
                SearchQuery.occurred_at.between(window.since, window.until),
                SearchQuery.zero_result.is_(True),
            )
        )

    return {
        **window,
        "total": int(total or 0),
        "offset": start,
        "limit": size,
        "queries": [
            {
                "query": query,
                "searches": int(count),
                "sessions": int(sessions or 0),
                "last_seen": last.isoformat() if last else None,
                "surface": surface,
            }
            for query, count, sessions, last, surface in rows
        ],
    }


# ------------------------------------------------------------- performance --
async def route_performance(*, days: int = 7, limit: int = 25,
    offset: int = 0, since: Optional[str] = None, until: Optional[str] = None) -> Dict[str, Any]:
    """Latency and error rate per route.

    `status` and `duration_ms` have been recorded on every request since the
    first release and read by nothing. They are the two figures that say
    whether the platform is healthy for the people using it, as opposed to
    healthy from the outside: a route that answers in eight seconds is up, and
    a route that 403s for everyone is up.

    Percentiles rather than an average, because an average latency hides the
    tail that people actually notice.
    """
    from backend.postgres import POSTGRES_ASYNC_SESSION_FACTORY
    from sql import ActivityEvent

    window = _window(days, since, until)
    size, start = _page(limit, offset, 25, 100)
    requests_only = [
        ActivityEvent.occurred_at >= window.since,
        ActivityEvent.event_type == "http.request",
        ActivityEvent.route.isnot(None),
    ]

    async with POSTGRES_ASYNC_SESSION_FACTORY()() as db:
        rows = (
            await db.execute(
                select(
                    ActivityEvent.route,
                    ActivityEvent.app,
                    # Without the verb, `GET /recipes/{id}` and
                    # `DELETE /recipes/{id}` were one row, averaging a read
                    # against a write and hiding whichever was slower.
                    ActivityEvent.method,
                    func.count(),
                    func.count().filter(ActivityEvent.status >= 400),
                    func.count().filter(ActivityEvent.status >= 500),
                    func.percentile_disc(0.5)
                    .within_group(ActivityEvent.duration_ms)
                    .label("p50"),
                    func.percentile_disc(0.95)
                    .within_group(ActivityEvent.duration_ms)
                    .label("p95"),
                    func.max(ActivityEvent.duration_ms),
                )
                .where(*requests_only)
                .group_by(ActivityEvent.route, ActivityEvent.app, ActivityEvent.method)
                .order_by(func.count().desc())
                .limit(size)
                .offset(start)
            )
        ).all()

        by_status = (
            await db.execute(
                select(ActivityEvent.status, func.count())
                .where(*requests_only)
                .group_by(ActivityEvent.status)
                .order_by(ActivityEvent.status)
            )
        ).all()

    routes = [
        {
            "route": route,
            "app": app,
            "method": method,
            "requests": int(count),
            "errors": int(errors or 0),
            "server_errors": int(server or 0),
            "error_rate": _ratio(int(errors or 0), int(count)),
            "p50_ms": int(p50) if p50 is not None else None,
            "p95_ms": int(p95) if p95 is not None else None,
            "max_ms": int(worst) if worst is not None else None,
        }
        for route, app, method, count, errors, server, p50, p95, worst in rows
    ]
    return {
        **window,
        "routes": routes,
        # Ranked by what to look at first, not by traffic.
        # Keyed by verb and route together, so two entries for the same path
        # do not collide in a list the console renders by route.
        "slowest": sorted(
            (r for r in routes if r["p95_ms"] is not None),
            key=lambda r: r["p95_ms"],
            reverse=True,
        )[:10],
        "most_errors": sorted(
            (r for r in routes if r["errors"]),
            key=lambda r: (r["error_rate"], r["errors"]),
            reverse=True,
        )[:10],
        "by_status": [
            {"status": int(status) if status is not None else None, "count": int(n)}
            for status, n in by_status
        ],
    }


async def search_quality(*, days: int = 7, since: Optional[str] = None, until: Optional[str] = None) -> Dict[str, Any]:
    """How well search is actually working, beyond "did it return rows".

    `relaxed` and `lexical_fallback` are recorded on every recipe search and
    have never been shown. A search that only returned results because the
    constraints were loosened looks identical to a good one in a hit count, and
    it is the clearest signal that the query understanding is producing
    unsatisfiable filters.
    """
    from backend.postgres import POSTGRES_ASYNC_SESSION_FACTORY
    from sql import SearchQuery

    window = _window(days, since, until)
    scoped = [SearchQuery.occurred_at >= window.since]

    async with POSTGRES_ASYNC_SESSION_FACTORY()() as db:
        totals = (
            await db.execute(
                select(
                    func.count(),
                    func.count().filter(SearchQuery.zero_result.is_(True)),
                    func.count().filter(SearchQuery.relaxed.is_(True)),
                    func.count().filter(SearchQuery.lexical_fallback.is_(True)),
                    func.percentile_disc(0.5)
                    .within_group(SearchQuery.latency_ms)
                    .label("p50"),
                    func.percentile_disc(0.95)
                    .within_group(SearchQuery.latency_ms)
                    .label("p95"),
                ).where(*scoped)
            )
        ).one()

        by_surface = (
            await db.execute(
                select(
                    SearchQuery.surface,
                    func.count(),
                    func.count().filter(SearchQuery.zero_result.is_(True)),
                    func.count().filter(SearchQuery.relaxed.is_(True)),
                    func.percentile_disc(0.95)
                    .within_group(SearchQuery.latency_ms)
                    .label("p95"),
                )
                .where(*scoped)
                .group_by(SearchQuery.surface)
                .order_by(func.count().desc())
            )
        ).all()

        # Queries that only worked after loosening. The catalogue has something
        # near enough, but the query understanding could not reach it.
        rescued = (
            await db.execute(
                select(
                    func.min(SearchQuery.normalized_query),
                    func.count(),
                )
                .where(*scoped, SearchQuery.relaxed.is_(True))
                .group_by(SearchQuery.query_hash)
                .order_by(func.count().desc())
                .limit(15)
            )
        ).all()

    total = int(totals[0] or 0)
    return {
        **window,
        "searches": total,
        "zero_result": int(totals[1] or 0),
        "zero_result_rate": _ratio(int(totals[1] or 0), total),
        "relaxed": int(totals[2] or 0),
        "relaxed_rate": _ratio(int(totals[2] or 0), total),
        "lexical_fallback": int(totals[3] or 0),
        "p50_ms": int(totals[4]) if totals[4] is not None else None,
        "p95_ms": int(totals[5]) if totals[5] is not None else None,
        "by_surface": [
            {
                "surface": surface,
                "searches": int(count),
                "zero_result": int(zero or 0),
                "zero_result_rate": _ratio(int(zero or 0), int(count)),
                "relaxed": int(relaxed or 0),
                "p95_ms": int(p95) if p95 is not None else None,
            }
            for surface, count, zero, relaxed, p95 in by_surface
        ],
        "rescued_by_relaxing": [
            {"query": query, "searches": int(count)} for query, count in rescued
        ],
    }


async def feedback_by_target(*, days: int = 30, limit: int = 25,
    offset: int = 0, since: Optional[str] = None, until: Optional[str] = None) -> Dict[str, Any]:
    """Which specific things draw complaints.

    Aggregate rates say the product is or is not liked. This says *what* is
    disliked — the recipe, the article, the answer — which is the version
    somebody can go and fix.
    """
    from backend.postgres import POSTGRES_ASYNC_SESSION_FACTORY
    from sql import FeedbackRecord

    window = _window(days, since, until)
    size, start = _page(limit, offset, 25, 100)
    negative = FeedbackRecord.rating_value.in_(_NEGATIVE_VALUES)

    async with POSTGRES_ASYNC_SESSION_FACTORY()() as db:
        rows = (
            await db.execute(
                select(
                    FeedbackRecord.target_type,
                    FeedbackRecord.target_id,
                    FeedbackRecord.app,
                    func.count(),
                    func.count().filter(negative),
                    func.max(FeedbackRecord.occurred_at),
                )
                .where(
                    FeedbackRecord.occurred_at >= window.since,
                    FeedbackRecord.target_id.isnot(None),
                )
                .group_by(
                    FeedbackRecord.target_type,
                    FeedbackRecord.target_id,
                    FeedbackRecord.app,
                )
                .having(func.count().filter(negative) > 0)
                .order_by(func.count().filter(negative).desc())
                .limit(size)
                .offset(start)
            )
        ).all()

        total = await db.scalar(
            select(
                func.count(
                    func.distinct(
                        func.concat(FeedbackRecord.target_type, FeedbackRecord.target_id)
                    )
                )
            ).where(
                FeedbackRecord.occurred_at.between(window.since, window.until),
                FeedbackRecord.target_id.isnot(None),
            )
        )

    from analytics.people import resolve_titles

    titles = await resolve_titles(
        (target_type, target_id) for target_type, target_id, *_rest in rows
    )

    return {
        **window,
        "total": int(total or 0),
        "offset": start,
        "limit": size,
        "targets": [
            {
                "target_type": target_type,
                "target_id": target_id,
                # The dish, not its uuid. A curator cannot recognise
                # `960c01f9-9a7b-…`, and cannot tell two rows apart without
                # opening both.
                "title": titles.get(str(target_id) or ""),
                "app": app,
                "feedback": int(count),
                "negative": int(bad or 0),
                "negative_rate": _ratio(int(bad or 0), int(count)),
                "last_seen": last.isoformat() if last else None,
            }
            for target_type, target_id, app, count, bad, last in rows
        ],
    }


async def search_funnel(*, days: int = 7, since: Optional[str] = None, until: Optional[str] = None) -> Dict[str, Any]:
    """Search, then click, then open. Where people fall out.

    Uses the three client events already emitted. A high search count with few
    clicks is a results page that is not persuading anyone, which no count of
    searches alone reveals.
    """
    from backend.postgres import POSTGRES_ASYNC_SESSION_FACTORY
    from sql import ActivityEvent

    window = _window(days, since, until)

    async def step(db, event_type):
        return (
            await db.execute(
                select(
                    func.count(),
                    func.count(func.distinct(ActivityEvent.client_session_id)),
                ).where(
                    ActivityEvent.occurred_at >= window.since,
                    ActivityEvent.event_type == event_type,
                )
            )
        ).one()

    async with POSTGRES_ASYNC_SESSION_FACTORY()() as db:
        searched = await step(db, "recipe.search")
        clicked = await step(db, "recipe.result_click")
        viewed = await step(db, "recipe.view")

    stages = [
        {"stage": "Searched", "events": int(searched[0] or 0), "sessions": int(searched[1] or 0)},
        {"stage": "Clicked a result", "events": int(clicked[0] or 0), "sessions": int(clicked[1] or 0)},
        {"stage": "Opened a recipe", "events": int(viewed[0] or 0), "sessions": int(viewed[1] or 0)},
    ]
    base = stages[0]["sessions"]
    for stage in stages:
        # Against the first stage, by sessions: one person searching six times
        # and clicking once is one person who got somewhere.
        stage["rate"] = _ratio(stage["sessions"], base)
    return {**window, "stages": stages}


# ------------------------------------------------------------------- users --
async def user_activity(
    *,
    days: int = 30,
    limit: int = 50,
    offset: int = 0,
    since: Optional[str] = None,
    until: Optional[str] = None,
) -> Dict[str, Any]:
    """Per-user activity and cost, for people whose consent allows naming them.

    Named and paged. The list was a bare top-fifty of opaque subjects with no
    way past it: an expert looking for one person could neither recognise them
    nor reach page two.
    """
    from backend.postgres import POSTGRES_ASYNC_SESSION_FACTORY
    from sql import ActivityEvent, FeedbackRecord, LLMUsage, SearchQuery

    window = _window(days, since, until)
    since = window.since
    size = _clamp(limit, 50, 200)
    start = max(0, int(offset or 0))
    # How many people there are in total, so the table can say "50 of 214"
    # and offer a next page rather than ending in a silent truncation.
    total = 0

    async with POSTGRES_ASYNC_SESSION_FACTORY()() as db:
        rows = (
            await db.execute(
                select(
                    ActivityEvent.user_id,
                    func.count().label("events"),
                    func.count(func.distinct(ActivityEvent.client_session_id)),
                    func.min(ActivityEvent.occurred_at),
                    func.max(ActivityEvent.occurred_at),
                    func.count().filter(
                        ActivityEvent.event_type.in_(["qa.ask", "qa.answered"])
                    ),
                    func.count().filter(
                        ActivityEvent.event_type.in_(["chat.turn", "chat.message"])
                    ),
                    func.count().filter(ActivityEvent.event_type == "recipe.view"),
                    func.count().filter(ActivityEvent.event_type == "page.view"),
                )
                .where(
                    ActivityEvent.occurred_at >= since,
                    ActivityEvent.user_id.isnot(None),
                )
                .group_by(ActivityEvent.user_id)
                .order_by(func.count().desc())
                .limit(size)
            )
        ).all()

        total = await db.scalar(
            select(func.count(func.distinct(ActivityEvent.user_id))).where(
                ActivityEvent.occurred_at >= since,
                ActivityEvent.user_id.isnot(None),
            )
        )

        # Searches come from search_query, not from the `recipe.search` event.
        # Only the browser emits that event, so counting it here meant the
        # per-user "Searches" column and the overview's "Searches" tile were
        # two different measurements wearing one label: every SDK search, and
        # every catalogue, tools, autocomplete and parameter search the
        # services record, was missing from the per-user figure.
        searches = dict(
            (user_id, (int(n), int(zero or 0)))
            for user_id, n, zero in (
                await db.execute(
                    select(
                        SearchQuery.user_id,
                        func.count(),
                        func.count().filter(SearchQuery.zero_result),
                    )
                    .where(
                        SearchQuery.occurred_at >= since,
                        SearchQuery.user_id.isnot(None),
                    )
                    .group_by(SearchQuery.user_id)
                )
            ).all()
        )

        feedback_given = dict(
            (user_id, (int(n), int(bad or 0)))
            for user_id, n, bad in (
                await db.execute(
                    select(
                        FeedbackRecord.user_id,
                        func.count(),
                        func.count().filter(
                            FeedbackRecord.rating_value.in_(_NEGATIVE_VALUES)
                        ),
                    )
                    .where(
                        FeedbackRecord.occurred_at >= since,
                        FeedbackRecord.user_id.isnot(None),
                    )
                    .group_by(FeedbackRecord.user_id)
                )
            ).all()
        )

        costs = dict(
            [
                (user_id, (int(tokens or 0), float(cost or 0)))
                for user_id, tokens, cost in (
                    await db.execute(
                        select(
                            LLMUsage.user_id,
                            func.coalesce(func.sum(LLMUsage.total_tokens), 0),
                            func.coalesce(func.sum(LLMUsage.cost_usd), 0),
                        )
                        .where(
                            LLMUsage.occurred_at >= since,
                            LLMUsage.user_id.isnot(None),
                        )
                        .group_by(LLMUsage.user_id)
                    )
                ).all()
            ]
        )

    from analytics.people import resolve_people

    # One page's worth of names. Bounded by the page size and cached, so the
    # cost is a handful of Keycloak calls per page rather than per row per
    # render — and this table is the one place a name is the whole point.
    people = await resolve_people(row[0] for row in rows)

    return {
        **window,
        "total": int(total or 0),
        "offset": start,
        "limit": size,
        "users": [
            {
                "user_id": user_id,
                **{
                    k: v
                    for k, v in people.get(user_id or "", {}).items()
                    if k in ("display_name", "username", "household_name", "resolved")
                },
                "events": int(events),
                "sessions": int(sessions or 0),
                "first_seen": first.isoformat() if first else None,
                "last_seen": last.isoformat() if last else None,
                "questions_asked": int(questions or 0),
                "chat_turns": int(turns or 0),
                "recipes_viewed": int(viewed or 0),
                "page_views": int(pages or 0),
                "searches": searches.get(user_id, (0, 0))[0],
                "searches_with_no_results": searches.get(user_id, (0, 0))[1],
                "feedback_given": feedback_given.get(user_id, (0, 0))[0],
                "feedback_negative": feedback_given.get(user_id, (0, 0))[1],
                "total_tokens": costs.get(user_id, (0, 0.0))[0],
                "cost_usd": costs.get(user_id, (0, 0.0))[1],
                # Someone whose first event in the whole record falls inside
                # the window is new; the column is what makes a cohort read
                # possible without a second request.
                "is_new": bool(first and first >= since),
            }
            for user_id, events, sessions, first, last, questions, turns, viewed, pages in rows
        ],
    }


async def llm_usage_report(*, days: int = 30, limit: int = 50, since: Optional[str] = None, until: Optional[str] = None) -> Dict[str, Any]:
    """Tokens and cost, sliced the ways that get asked for.

    By user is the one Langfuse cannot answer: its metrics API dropped userId
    as a grouping dimension, which is why this table exists at all.

    Input and output tokens are reported apart as well as together. They are
    not interchangeable — output costs three to five times input on every
    provider here — so a single total is the one number from which cost cannot
    be reconstructed. Calls whose model has no known rate are counted and
    named, because a spend figure that silently omits them reads the same as a
    platform nobody used.
    """
    from backend.postgres import POSTGRES_ASYNC_SESSION_FACTORY
    from sql import LLMUsage

    window = _window(days, since, until)
    since = window.since
    size = _clamp(limit, 50, 200)

    def totals(*group):
        return (
            select(
                *group,
                func.count(),
                func.coalesce(func.sum(LLMUsage.total_tokens), 0),
                func.coalesce(func.sum(LLMUsage.cost_usd), 0),
                func.coalesce(func.sum(LLMUsage.input_tokens), 0),
                func.coalesce(func.sum(LLMUsage.output_tokens), 0),
                func.count().filter(LLMUsage.cost_usd.is_(None)),
                func.percentile_disc(0.95).within_group(LLMUsage.latency_ms),
            )
            .where(LLMUsage.occurred_at >= since)
            .group_by(*group)
            .order_by(func.coalesce(func.sum(LLMUsage.cost_usd), 0).desc())
            .limit(size)
        )

    async with POSTGRES_ASYNC_SESSION_FACTORY()() as db:
        by_model = (await db.execute(totals(LLMUsage.model))).all()
        by_app = (await db.execute(totals(LLMUsage.app))).all()
        by_feature = (await db.execute(totals(LLMUsage.feature))).all()
        by_user = (
            await db.execute(totals(LLMUsage.user_id).where(LLMUsage.user_id.isnot(None)))
        ).all()
        by_provider = (await db.execute(totals(LLMUsage.provider))).all()
        # How much of the spend figure is real. An unpriced call still shows
        # its tokens; it just contributes nothing to the money, and a report
        # that does not say so is misleading rather than merely incomplete.
        coverage = (
            await db.execute(
                select(
                    func.count(),
                    func.count().filter(LLMUsage.cost_usd.is_(None)),
                    func.coalesce(
                        func.sum(LLMUsage.total_tokens).filter(LLMUsage.cost_usd.is_(None)), 0
                    ),
                    func.count(func.distinct(LLMUsage.model)).filter(
                        LLMUsage.cost_usd.is_(None)
                    ),
                ).where(LLMUsage.occurred_at >= since)
            )
        ).one()
        unpriced_models = (
            await db.execute(
                select(LLMUsage.model, func.count())
                .where(LLMUsage.occurred_at >= since, LLMUsage.cost_usd.is_(None))
                .group_by(LLMUsage.model)
                .order_by(func.count().desc())
                .limit(20)
            )
        ).all()
        daily = (
            await db.execute(
                select(
                    func.date_trunc("day", LLMUsage.occurred_at).label("day"),
                    func.coalesce(func.sum(LLMUsage.total_tokens), 0),
                    func.coalesce(func.sum(LLMUsage.cost_usd), 0),
                )
                .where(LLMUsage.occurred_at >= since)
                .group_by("day")
                .order_by("day")
            )
        ).all()

    def rows(result, key):
        return [
            {
                key: value,
                "calls": int(calls),
                "total_tokens": int(tokens or 0),
                "cost_usd": float(cost or 0),
                "input_tokens": int(inp or 0),
                "output_tokens": int(out or 0),
                "unpriced_calls": int(unpriced or 0),
                "p95_ms": int(p95) if p95 is not None else None,
            }
            for value, calls, tokens, cost, inp, out, unpriced, p95 in result
        ]

    calls = int(coverage[0] or 0)
    unpriced = int(coverage[1] or 0)

    return {
        **window,
        "by_model": rows(by_model, "model"),
        "by_app": rows(by_app, "app"),
        "by_feature": rows(by_feature, "feature"),
        "by_user": rows(by_user, "user_id"),
        "by_provider": rows(by_provider, "provider"),
        "pricing": {
            "calls": calls,
            "unpriced_calls": unpriced,
            # The honest headline: what share of the money figure is grounded.
            "priced_share": _ratio(calls - unpriced, calls),
            "unpriced_tokens": int(coverage[2] or 0),
            "unpriced_models": [
                {"model": model, "calls": int(n)} for model, n in unpriced_models
            ],
            "rates_as_of": _prices_as_of(),
        },
        "daily": [
            {
                "day": day.date().isoformat() if hasattr(day, "date") else str(day),
                "total_tokens": int(tokens or 0),
                "cost_usd": float(cost or 0),
            }
            for day, tokens, cost in daily
        ],
    }


# ---------------------------------------------------------------- feedback --
async def feedback_inbox(
    *,
    limit: int = 50,
    offset: int = 0,
    status: Optional[str] = None,
    app: Optional[str] = None,
    negative_only: bool = False,
    target_type: Optional[str] = None,
    target_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Every surface's feedback in one triage list.

    Filterable by target so a curator opening one recipe can see what people
    said about *that* recipe. Without it the only route to a complaint was the
    platform-wide inbox, which is the wrong place to stand when the question is
    "is there anything wrong with this dish".
    """
    from backend.postgres import POSTGRES_ASYNC_SESSION_FACTORY
    from sql import FeedbackRecord

    size, start = _clamp(limit, 50, 200), max(0, int(offset or 0))
    query = select(FeedbackRecord)
    if status:
        query = query.where(FeedbackRecord.status == status)
    if app:
        query = query.where(FeedbackRecord.app == app)
    if negative_only:
        query = query.where(
            FeedbackRecord.rating_value.in_(_NEGATIVE_VALUES)
        )
    if target_type:
        query = query.where(FeedbackRecord.target_type == target_type)
    if target_id:
        query = query.where(FeedbackRecord.target_id == str(target_id))

    async with POSTGRES_ASYNC_SESSION_FACTORY()() as db:
        total = await db.scalar(select(func.count()).select_from(query.subquery()))
        rows = (
            (
                await db.execute(
                    query.order_by(FeedbackRecord.occurred_at.desc())
                    .limit(size)
                    .offset(start)
                )
            )
            .scalars()
            .all()
        )
    return {
        "total": int(total or 0),
        "limit": size,
        "offset": start,
        "items": [row.to_dict() for row in rows],
    }


async def set_feedback_status(feedback_id: int, status: str) -> bool:
    """Move one feedback item through the triage workflow."""
    from sqlalchemy import update

    from backend.postgres import POSTGRES_ASYNC_SESSION_FACTORY
    from sql import FeedbackRecord

    if status not in ("new", "triaged", "resolved"):
        raise ValueError("status must be new, triaged or resolved")
    async with POSTGRES_ASYNC_SESSION_FACTORY()() as db:
        result = await db.execute(
            update(FeedbackRecord.__table__)
            .where(FeedbackRecord.__table__.c.id == int(feedback_id))
            .values(status=status)
        )
        await db.commit()
    return bool(result.rowcount)


# ----------------------------------------------------------------- reviews --
async def record_review(
    *,
    reviewer_id: str,
    reviewer_name: Optional[str],
    target_type: str,
    target_id: str,
    verdict: str,
    notes: Optional[str] = None,
    tags: Optional[List[str]] = None,
    request_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Record an expert's verdict, replacing their previous one on the same target.

    One verdict per reviewer per target: an expert revisiting a question is
    changing their mind, not adding a second opinion. A second expert's verdict
    is a separate row, which is the point.
    """
    from sqlalchemy.dialects.postgresql import insert

    from backend.postgres import POSTGRES_ASYNC_SESSION_FACTORY
    from sql import ExpertReview

    table = ExpertReview.__table__
    values = {
        "reviewer_id": reviewer_id,
        "reviewer_name": reviewer_name,
        "target_type": target_type,
        "target_id": str(target_id),
        "verdict": verdict,
        "notes": notes,
        "tags": list(tags or []) or None,
        "request_id": request_id,
    }
    async with POSTGRES_ASYNC_SESSION_FACTORY()() as db:
        stmt = insert(table).values(**values)
        await db.execute(
            stmt.on_conflict_do_update(
                index_elements=[
                    table.c.reviewer_id,
                    table.c.target_type,
                    table.c.target_id,
                ],
                set_={
                    "verdict": verdict,
                    "notes": notes,
                    "tags": values["tags"],
                    "created_at": func.now(),
                },
            )
        )
        await db.commit()

    # Push the verdict to Langfuse as an annotation on the trace it is about.
    # After the commit, never before: the review is the record that matters and
    # a third-party outage must not be able to lose it. A failure here leaves
    # `langfuse_score_id` NULL, which is exactly what it meant before.
    values["langfuse_score_id"] = await _annotate_trace(
        target_type=target_type,
        target_id=str(target_id),
        request_id=request_id,
        verdict=verdict,
        notes=notes,
        reviewer_id=reviewer_id,
    )
    return values


#: An expert verdict as a number Langfuse can chart. Named verdicts are what
#: the console stores; a score has to be ordinal to be worth plotting.
_VERDICT_SCORES = {
    "correct": 1.0,
    "good": 1.0,
    "acceptable": 0.5,
    "partial": 0.5,
    "unclear": 0.5,
    "incorrect": 0.0,
    "wrong": 0.0,
    "harmful": -1.0,
}


async def _annotate_trace(
    *,
    target_type: str,
    target_id: str,
    request_id: Optional[str],
    verdict: str,
    notes: Optional[str],
    reviewer_id: str,
) -> Optional[str]:
    """Attach a verdict to its Langfuse trace, and store the score's id.

    The trace is found through the request that produced the answer: the model
    call recorded its own trace id against the same `request_id`, so the two
    join without anything having to carry a trace id around the platform.

    Never raises. Every failure path — no request id, no trace recorded,
    Langfuse switched off or unreachable — ends in the same place, which is the
    NULL this column has always held.
    """
    if not request_id:
        return None
    try:
        from backend.langfuse_read import LangfuseReadClient
        from backend.postgres import POSTGRES_ASYNC_SESSION_FACTORY
        from sql import ExpertReview, LLMUsage

        async with POSTGRES_ASYNC_SESSION_FACTORY()() as db:
            trace_id = await db.scalar(
                select(LLMUsage.trace_id)
                .where(LLMUsage.request_id == request_id, LLMUsage.trace_id.isnot(None))
                .order_by(LLMUsage.occurred_at.desc())
                .limit(1)
            )
        if not trace_id:
            return None

        score_id = await LangfuseReadClient.push_score(
            trace_id=trace_id,
            name=f"expert_{target_type}",
            value=_VERDICT_SCORES.get(verdict.lower(), 0.5),
            comment=notes,
        )
        if not score_id:
            return None

        from sqlalchemy import update

        async with POSTGRES_ASYNC_SESSION_FACTORY()() as db:
            await db.execute(
                update(ExpertReview.__table__)
                .where(
                    ExpertReview.reviewer_id == reviewer_id,
                    ExpertReview.target_type == target_type,
                    ExpertReview.target_id == target_id,
                )
                .values(langfuse_score_id=score_id)
            )
            await db.commit()
        return score_id
    except Exception:
        logger.debug("analytics.annotate_trace_failed", exc_info=True)
        return None


async def list_reviews(
    *,
    limit: int = 50,
    offset: int = 0,
    target_type: Optional[str] = None,
    target_id: Optional[str] = None,
    reviewer_id: Optional[str] = None,
) -> Dict[str, Any]:
    from backend.postgres import POSTGRES_ASYNC_SESSION_FACTORY
    from sql import ExpertReview

    size, start = _clamp(limit, 50, 200), max(0, int(offset or 0))
    query = select(ExpertReview)
    if target_type:
        query = query.where(ExpertReview.target_type == target_type)
    if target_id:
        query = query.where(ExpertReview.target_id == str(target_id))
    if reviewer_id:
        query = query.where(ExpertReview.reviewer_id == reviewer_id)

    async with POSTGRES_ASYNC_SESSION_FACTORY()() as db:
        total = await db.scalar(select(func.count()).select_from(query.subquery()))
        rows = (
            (
                await db.execute(
                    query.order_by(ExpertReview.created_at.desc())
                    .limit(size)
                    .offset(start)
                )
            )
            .scalars()
            .all()
        )
    return {
        "total": int(total or 0),
        "limit": size,
        "offset": start,
        "items": [row.to_dict() for row in rows],
    }


# --------------------------------------------------------- expert activity --
async def expert_activity(*, days: int = 30, limit: int = 100,
    offset: int = 0, since: Optional[str] = None, until: Optional[str] = None) -> Dict[str, Any]:
    """Who did what with their privileges.

    The record the platform never kept: every privileged proxy authorised and
    forwarded without leaving a trace of who acted or what they looked at.
    """
    from backend.postgres import POSTGRES_ASYNC_SESSION_FACTORY
    from sql import ActivityEvent

    window = _window(days, since, until)
    size, start = _page(limit, offset, 100, 500)

    async with POSTGRES_ASYNC_SESSION_FACTORY()() as db:
        by_actor = (
            await db.execute(
                select(
                    ActivityEvent.user_id,
                    ActivityEvent.event_type,
                    func.count(),
                    func.max(ActivityEvent.occurred_at),
                )
                .where(
                    ActivityEvent.occurred_at >= window.since,
                    ActivityEvent.event_type.like("expert.%")
                    | ActivityEvent.event_type.like("admin.%"),
                )
                .group_by(ActivityEvent.user_id, ActivityEvent.event_type)
                .order_by(func.count().desc())
            )
        ).all()

        recent = (
            (
                await db.execute(
                    select(ActivityEvent)
                    .where(
                        ActivityEvent.occurred_at >= window.since,
                        ActivityEvent.event_type.like("expert.%")
                        | ActivityEvent.event_type.like("admin.%"),
                    )
                    .order_by(ActivityEvent.occurred_at.desc())
                    .limit(size)
                .offset(start)
                )
            )
            .scalars()
            .all()
        )

    return {
        **window,
        "by_actor": [
            {
                "user_id": user_id,
                "action": action,
                "count": int(count),
                "last_seen": last.isoformat() if last else None,
            }
            for user_id, action, count, last in by_actor
        ],
        "recent": [row.to_dict() for row in recent],
    }


# --------------------------------------------------------------- retention --
async def apply_retention(days: int) -> Dict[str, int]:
    """Delete activity older than the retention window.

    Deleted in batches so a first run against a year of data does not hold one
    long transaction and a table lock while it works.
    """
    from sqlalchemy import delete

    from backend.postgres import POSTGRES_ASYNC_SESSION_FACTORY
    from sql import (
        ActivityEvent,
        ClientError,
        ClientSession,
        LLMUsage,
        SearchQuery,
        UIInteraction,
        WebVital,
    )

    cutoff = datetime.now(timezone.utc) - timedelta(days=max(1, int(days)))
    removed: Dict[str, int] = {}
    batch = 10_000

    for model, column in (
        (ActivityEvent, ActivityEvent.occurred_at),
        (SearchQuery, SearchQuery.occurred_at),
        (LLMUsage, LLMUsage.occurred_at),
        # Real user monitoring. Clicks and vitals are by far the highest-volume
        # tables here, so they matter most to a retention run.
        (ClientError, ClientError.occurred_at),
        (UIInteraction, UIInteraction.occurred_at),
        (WebVital, WebVital.occurred_at),
        # Feedback is kept: somebody took the trouble to write it, and an
        # expert may not have read it yet. error_group is kept for the same
        # reason in reverse — it is the history of what has broken, it holds
        # no identity, and one row stands for however many occurrences.
    ):
        table = model.__table__
        total = 0
        while True:
            async with POSTGRES_ASYNC_SESSION_FACTORY()() as db:
                ids = (
                    await db.execute(
                        select(table.c.id).where(column < cutoff).limit(batch)
                    )
                ).scalars().all()
                if not ids:
                    break
                result = await db.execute(delete(table).where(table.c.id.in_(ids)))
                await db.commit()
                total += result.rowcount or 0
            if len(ids) < batch:
                break
        removed[table.name] = total

    # Session rows have no `id` column, so they cannot go through the batched
    # loop above. Deleted by their own key, and only once nothing points at
    # them any more — a session whose errors are still within retention is
    # still the explanation for those errors.
    from sqlalchemy import delete as sql_delete

    total = 0
    while True:
        async with POSTGRES_ASYNC_SESSION_FACTORY()() as db:
            ids = (
                await db.execute(
                    select(ClientSession.session_id)
                    .where(ClientSession.last_seen_at < cutoff)
                    .limit(batch)
                )
            ).scalars().all()
            if not ids:
                break
            result = await db.execute(
                sql_delete(ClientSession.__table__).where(
                    ClientSession.__table__.c.session_id.in_(ids)
                )
            )
            await db.commit()
            total += result.rowcount or 0
        if len(ids) < batch:
            break
    removed["client_session"] = total
    return removed


def _timeline(rows: List[Any]) -> List[Dict[str, Any]]:
    """The ordered actions, each with the pause that preceded it.

    The gap matters more than the timestamp: a forty-second pause before a
    second search is somebody reading results and not finding what they wanted,
    and that is invisible in a list of times.
    """
    entries: List[Dict[str, Any]] = []
    previous = None
    for row in rows:
        gap = None
        if previous is not None and row.occurred_at is not None:
            gap = max(0, int((row.occurred_at - previous).total_seconds()))
        previous = row.occurred_at
        entries.append(
            {
                "occurred_at": row.occurred_at.isoformat() if row.occurred_at else None,
                "event_type": row.event_type,
                "app": row.app,
                "route": row.route,
                "status": row.status,
                "duration_ms": row.duration_ms,
                "gap_seconds": gap,
                "props": row.props or {},
            }
        )
    return entries


# ----------------------------------------------------------------- helpers --
class _Window:
    """The reporting period, with the query bounds and the JSON view separated.

    Spreading raw datetimes into a response works until something in the chain
    stops encoding them; the `meta` form is what goes over the wire, and
    `previous_since` never does — it is an implementation detail of "rising".
    """

    __slots__ = ("days", "since", "until", "previous_since")

    def __init__(self, days: int, since: Optional[str] = None, until: Optional[str] = None):
        start = _parse_instant(since)
        end = _parse_instant(until)
        if start is not None or end is not None:
            # An explicit range wins. `days` becomes a description of the range
            # rather than its definition, so a caller that reads it back gets a
            # number consistent with the bounds it asked for.
            self.until = end or datetime.now(timezone.utc)
            self.since = start or (self.until - timedelta(days=max(1, int(days or 7))))
            if self.since > self.until:
                self.since, self.until = self.until, self.since
            span = self.until - self.since
            # Same ceiling as the day-based path, applied to the range itself
            # so an explicit `since` cannot ask for the whole table.
            if span > timedelta(days=365):
                self.since = self.until - timedelta(days=365)
                span = timedelta(days=365)
            self.days = max(1, round(span.total_seconds() / 86400))
        else:
            self.days = max(1, min(int(days or 7), 365))
            self.until = datetime.now(timezone.utc)
            self.since = self.until - timedelta(days=self.days)
        # "Rising" compares a window against one of the same length before it.
        self.previous_since = self.since - (self.until - self.since)

    def keys(self):
        return ("days", "since", "until")

    def __getitem__(self, key):
        if key == "days":
            return self.days
        if key == "since":
            return self.since.isoformat()
        if key == "until":
            return self.until.isoformat()
        raise KeyError(key)


def _window(
    days: int, since: Optional[str] = None, until: Optional[str] = None
) -> "_Window":
    return _Window(days, since, until)


def _parse_instant(raw: Optional[str]) -> Optional[datetime]:
    """An ISO-8601 bound from a query string, or None.

    Bad input is ignored rather than rejected: a malformed `since` falls back
    to the day count, which is a working report rather than an error page. A
    naive timestamp is read as UTC, because every stored time is UTC and
    guessing a local zone here would silently shift the whole window.
    """
    if not raw:
        return None
    text = str(raw).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _clamp(value: Optional[int], default: int, maximum: int) -> int:
    try:
        size = int(value) if value is not None else default
    except (TypeError, ValueError):
        size = default
    return max(1, min(size, maximum))


def _prices_as_of() -> str:
    from analytics.pricing import PRICES_AS_OF

    return PRICES_AS_OF


def _page(limit: Optional[int], offset: Optional[int], default: int, maximum: int):
    """One page's bounds, and the fields a caller needs to render a pager.

    Every list report capped itself with a LIMIT and said nothing about it, so
    a table showing twenty-five rows and a table showing all twenty-five rows
    looked identical — and there was no way to reach row twenty-six. This gives
    each of them the same three fields, so the console can page them the same
    way rather than each page inventing its own.
    """
    size = _clamp(limit, default, maximum)
    start = max(0, int(offset or 0))
    return size, start


def _ratio(part: int, whole: int) -> float:
    return round((part / whole) * 100, 1) if whole else 0.0


# ------------------------------------------------------------- engagement --
async def engagement_patterns(*, days: int = 30, since: Optional[str] = None, until: Optional[str] = None) -> Dict[str, Any]:
    """When people use the platform, and whether they come back.

    Three questions a count of events cannot answer. *When* decides when a
    deploy is cheap and when a slow endpoint is felt. *Coming back* is the only
    honest read on whether the product is useful, because a first visit measures
    marketing and a second measures the product. *How much per visit* separates
    a hundred people glancing once from ten people working seriously.
    """
    from sqlalchemy import Integer, cast, distinct, extract

    from backend.postgres import POSTGRES_ASYNC_SESSION_FACTORY
    from sql import ActivityEvent

    window = _window(days, since, until)
    in_window = ActivityEvent.occurred_at.between(window.since, window.until)

    async with POSTGRES_ASYNC_SESSION_FACTORY()() as db:
        hour = cast(extract("hour", ActivityEvent.occurred_at), Integer)
        by_hour = (
            await db.execute(
                select(
                    hour,
                    func.count(),
                    func.count(distinct(ActivityEvent.client_session_id)),
                )
                .where(in_window)
                .group_by(hour)
                .order_by(hour)
            )
        ).all()

        # PostgreSQL's dow is 0=Sunday. Kept as the raw number here and named
        # once, below, rather than in every caller.
        dow = cast(extract("dow", ActivityEvent.occurred_at), Integer)
        by_weekday = (
            await db.execute(
                select(
                    dow,
                    func.count(),
                    func.count(distinct(ActivityEvent.client_session_id)),
                )
                .where(in_window)
                .group_by(dow)
                .order_by(dow)
            )
        ).all()

        # Actions per session, as a distribution rather than a mean: an average
        # of 4 hides both "everyone does 4" and "most do 1 and one did 300",
        # which call for opposite responses.
        per_session = (
            select(func.count().label("actions"))
            .where(in_window, ActivityEvent.client_session_id.isnot(None))
            .group_by(ActivityEvent.client_session_id)
            .subquery()
        )
        depth = (
            await db.execute(
                select(
                    func.count(),
                    func.percentile_disc(0.5).within_group(per_session.c.actions),
                    func.percentile_disc(0.9).within_group(per_session.c.actions),
                    func.max(per_session.c.actions),
                    func.count().filter(per_session.c.actions == 1),
                    func.count().filter(per_session.c.actions.between(2, 5)),
                    func.count().filter(per_session.c.actions.between(6, 20)),
                    func.count().filter(per_session.c.actions > 20),
                ).select_from(per_session)
            )
        ).one()

        # Returning vs new, against the whole record and not just this window:
        # someone whose first-ever event predates the window is a returning
        # user even if the window is the only place they appear.
        first_seen = (
            select(
                ActivityEvent.user_id.label("user_id"),
                func.min(ActivityEvent.occurred_at).label("first_seen"),
            )
            .where(ActivityEvent.user_id.isnot(None))
            .group_by(ActivityEvent.user_id)
            .subquery()
        )
        active = (
            select(distinct(ActivityEvent.user_id).label("user_id"))
            .where(in_window, ActivityEvent.user_id.isnot(None))
            .subquery()
        )
        cohorts = (
            await db.execute(
                select(
                    func.count(),
                    func.count().filter(first_seen.c.first_seen >= window.since),
                ).select_from(
                    active.join(first_seen, active.c.user_id == first_seen.c.user_id)
                )
            )
        ).one()

    sessions_total = int(depth[0] or 0)
    identified = int(cohorts[0] or 0)
    new_users = int(cohorts[1] or 0)

    return {
        **window,
        "by_hour": [
            {"hour": int(h), "events": int(n), "sessions": int(s)}
            for h, n, s in by_hour
        ],
        "by_weekday": [
            {"weekday": int(d), "label": _WEEKDAYS[int(d) % 7], "events": int(n), "sessions": int(s)}
            for d, n, s in by_weekday
        ],
        "busiest_hour": (
            max(by_hour, key=lambda row: row[1])[0] if by_hour else None
        ),
        "session_depth": {
            "sessions": sessions_total,
            "median_actions": int(depth[1]) if depth[1] is not None else None,
            "p90_actions": int(depth[2]) if depth[2] is not None else None,
            "max_actions": int(depth[3]) if depth[3] is not None else None,
            "buckets": [
                {"label": "1 action", "sessions": int(depth[4] or 0)},
                {"label": "2-5", "sessions": int(depth[5] or 0)},
                {"label": "6-20", "sessions": int(depth[6] or 0)},
                {"label": "21+", "sessions": int(depth[7] or 0)},
            ],
            # A session of one action is someone who arrived and left.
            "bounce_rate": _ratio(int(depth[4] or 0), sessions_total),
        },
        "retention": {
            # Only counts people who can be named at all: under opt-in consent
            # this is a subset of activity, and calling it "users" without
            # saying so would understate the platform.
            "identified_users": identified,
            "new_users": new_users,
            "returning_users": max(identified - new_users, 0),
            "returning_rate": _ratio(max(identified - new_users, 0), identified),
        },
    }


_WEEKDAYS = ("Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday")


# ---------------------------------------------------------------- content --
async def content_report(*, days: int = 7, limit: int = 20, since: Optional[str] = None, until: Optional[str] = None) -> Dict[str, Any]:
    """What the answers, chats and pages themselves look like.

    Every event carries a `props` object and, until this, nothing ever read
    one — the richest thing the platform records was also the only thing no
    report touched. FoodScholar stamps each answer with its mode, language,
    whether retrieval ran and whether the cache was hit; FoodChat stamps each
    turn with the intent it classified and how long it took; the browser stamps
    each navigation with its path. All of that was going into a column and
    staying there.
    """
    from backend.postgres import POSTGRES_ASYNC_SESSION_FACTORY
    from sql import ActivityEvent

    window = _window(days, since, until)
    size = _clamp(limit, 20, 100)
    in_window = ActivityEvent.occurred_at.between(window.since, window.until)

    def prop(key: str):
        return ActivityEvent.props[key].astext

    async def top_by(db, event_type: str, key: str, cap: int = 12):
        value = prop(key)
        rows = (
            await db.execute(
                select(value, func.count())
                .where(in_window, ActivityEvent.event_type == event_type, value.isnot(None))
                .group_by(value)
                .order_by(func.count().desc())
                .limit(cap)
            )
        ).all()
        return [{"value": v, "count": int(n)} for v, n in rows]

    async with POSTGRES_ASYNC_SESSION_FACTORY()() as db:
        answered = ActivityEvent.event_type == "qa.answered"
        # FoodScholar reports confidence as a word — "high", "medium", "low" —
        # on some paths and as a number on others. Casting the word to NUMERIC
        # failed the whole report. Only values that look numeric are averaged;
        # the words get their own distribution below, which is the more
        # useful reading of them anyway.
        numeric = r"^-?[0-9]+(\.[0-9]+)?$"
        qa = (
            await db.execute(
                select(
                    func.count(),
                    func.count().filter(prop("cache_hit").in_(("true", "True"))),
                    func.count().filter(prop("rag_enabled").in_(("true", "True"))),
                    func.avg(func.cast(prop("confidence"), Numeric)).filter(
                        prop("confidence").op("~")(numeric)
                    ),
                    func.avg(func.cast(prop("articles_consulted"), Numeric)).filter(
                        prop("articles_consulted").op("~")(numeric)
                    ),
                ).where(in_window, answered)
            )
        ).one()
        qa_confidence_words = await top_by(db, "qa.answered", "confidence")

        qa_modes = await top_by(db, "qa.answered", "mode")
        qa_languages = await top_by(db, "qa.answered", "language")
        qa_models = await top_by(db, "qa.answered", "model")

        # Asked versus answered. Every question that produced no answer event
        # is a person who got nothing, and no report has ever counted them.
        asked = (
            await db.execute(
                select(func.count()).where(in_window, ActivityEvent.event_type == "qa.ask")
            )
        ).scalar() or 0
        persist_failed = (
            await db.execute(
                select(func.count()).where(
                    in_window, ActivityEvent.event_type == "qa.persist_failed"
                )
            )
        ).scalar() or 0

        chat_intents = await top_by(db, "chat.turn", "intent")
        chat_latency = (
            await db.execute(
                select(
                    func.count(),
                    func.percentile_disc(0.5).within_group(
                        func.cast(prop("latency_ms"), Numeric)
                    ),
                    func.percentile_disc(0.95).within_group(
                        func.cast(prop("latency_ms"), Numeric)
                    ),
                ).where(
                    in_window,
                    ActivityEvent.event_type == "chat.turn",
                    prop("latency_ms").isnot(None),
                )
            )
        ).one()

        path = prop("path")
        pages = (
            await db.execute(
                select(
                    path,
                    func.count(),
                    func.count(func.distinct(ActivityEvent.client_session_id)),
                )
                .where(in_window, ActivityEvent.event_type == "page.view", path.isnot(None))
                .group_by(path)
                .order_by(func.count().desc())
                .limit(size)
            )
        ).all()

        # Where people land. A page.view with no referring route is an entry
        # point — the door people actually come in through.
        entries = (
            await db.execute(
                select(path, func.count())
                .where(
                    in_window,
                    ActivityEvent.event_type == "page.view",
                    path.isnot(None),
                    prop("from").is_(None),
                )
                .group_by(path)
                .order_by(func.count().desc())
                .limit(10)
            )
        ).all()

        search_cache = (
            await db.execute(
                select(
                    func.count(),
                    func.count().filter(prop("from_cache").in_(("true", "True"))),
                ).where(in_window, ActivityEvent.event_type == "recipe.search")
            )
        ).one()

    answers = int(qa[0] or 0)
    chat_turns = int(chat_latency[0] or 0)
    searches = int(search_cache[0] or 0)

    return {
        **window,
        "qa": {
            "asked": int(asked),
            "answered": answers,
            # Not necessarily a bug — a question can be asked and answered
            # either side of the window boundary — but a persistent gap is
            # people whose question died somewhere.
            "unanswered": max(int(asked) - answers, 0),
            "answer_rate": _ratio(answers, int(asked)),
            "persist_failed": int(persist_failed),
            "cache_hits": int(qa[1] or 0),
            "cache_hit_rate": _ratio(int(qa[1] or 0), answers),
            "with_retrieval": int(qa[2] or 0),
            "retrieval_rate": _ratio(int(qa[2] or 0), answers),
            "avg_confidence": round(float(qa[3]), 3) if qa[3] is not None else None,
            # The word-valued confidence, as a distribution. Where the service
            # says "high" rather than 0.8 this is the only reading there is.
            "by_confidence": [
                row for row in qa_confidence_words
                if not re.match(numeric, str(row["value"]))
            ],
            "avg_articles": round(float(qa[4]), 1) if qa[4] is not None else None,
            "by_mode": qa_modes,
            "by_language": qa_languages,
            "by_model": qa_models,
        },
        "chat": {
            "turns": chat_turns,
            "by_intent": chat_intents,
            "p50_ms": int(chat_latency[1]) if chat_latency[1] is not None else None,
            "p95_ms": int(chat_latency[2]) if chat_latency[2] is not None else None,
        },
        "pages": [
            {"path": p, "views": int(n), "sessions": int(s)} for p, n, s in pages
        ],
        "entry_pages": [{"path": p, "sessions": int(n)} for p, n in entries],
        "search_cache": {
            "searches": searches,
            "from_cache": int(search_cache[1] or 0),
            "cache_hit_rate": _ratio(int(search_cache[1] or 0), searches),
        },
    }


# --------------------------------------------------------------- feedback --
async def feedback_quality(*, days: int = 30, since: Optional[str] = None, until: Optional[str] = None) -> Dict[str, Any]:
    """Feedback as a measurement, not an inbox.

    The inbox answers "what did people say"; this answers "is it getting
    better". Three things were being recorded and never read: the numeric score
    behind a star rating, the reason category behind a complaint, and the scale
    the rating was given on. That last one matters more than it sounds — the
    single negative-feedback rate on the overview pools thumbs-down, a one-star
    likert and an A/B preference, and an A/B preference can never be negative,
    so preference votes were quietly diluting the complaint rate.
    """
    from backend.postgres import POSTGRES_ASYNC_SESSION_FACTORY
    from sql import FeedbackRecord

    window = _window(days, since, until)
    in_window = FeedbackRecord.occurred_at.between(window.since, window.until)
    negative = FeedbackRecord.rating_value.in_(_NEGATIVE_VALUES)

    async with POSTGRES_ASYNC_SESSION_FACTORY()() as db:
        by_kind = (
            await db.execute(
                select(
                    FeedbackRecord.rating_kind,
                    func.count(),
                    func.count().filter(negative),
                    func.avg(FeedbackRecord.rating_value_num),
                )
                .where(in_window)
                .group_by(FeedbackRecord.rating_kind)
                .order_by(func.count().desc())
            )
        ).all()

        reasons = (
            await db.execute(
                select(
                    FeedbackRecord.reason,
                    func.count(),
                    func.count().filter(negative),
                )
                .where(in_window, FeedbackRecord.reason.isnot(None))
                .group_by(FeedbackRecord.reason)
                .order_by(func.count().desc())
                .limit(20)
            )
        ).all()

        by_source = (
            await db.execute(
                select(FeedbackRecord.source, func.count())
                .where(in_window)
                .group_by(FeedbackRecord.source)
            )
        ).all()

        day = func.date_trunc("day", FeedbackRecord.occurred_at)
        daily = (
            await db.execute(
                select(
                    day,
                    func.count(),
                    func.count().filter(negative),
                    func.avg(FeedbackRecord.rating_value_num),
                )
                .where(in_window)
                .group_by(day)
                .order_by(day)
            )
        ).all()

        # The score, on its own scale. Averaging a thumbs-up with a four-star
        # would produce a number that means nothing, so this is likert only.
        scored = (
            await db.execute(
                select(
                    func.count(),
                    func.avg(FeedbackRecord.rating_value_num),
                    func.percentile_disc(0.5).within_group(
                        FeedbackRecord.rating_value_num
                    ),
                ).where(
                    in_window,
                    FeedbackRecord.rating_kind == "likert5",
                    FeedbackRecord.rating_value_num.isnot(None),
                )
            )
        ).one()

        backlog = (
            await db.execute(
                select(FeedbackRecord.status, func.count())
                .where(in_window)
                .group_by(FeedbackRecord.status)
            )
        ).all()

        # How long an untriaged complaint has been sitting. A backlog count
        # says how much; this says how bad.
        oldest = (
            await db.execute(
                select(func.min(FeedbackRecord.occurred_at)).where(
                    FeedbackRecord.status == "new", negative
                )
            )
        ).scalar()

    return {
        **window,
        "by_kind": [
            {
                "rating_kind": kind,
                "feedback": int(count),
                "negative": int(bad or 0),
                # Per scale, so an A/B preference no longer dilutes the rate
                # for the scales on which a complaint is possible.
                "negative_rate": _ratio(int(bad or 0), int(count)),
                "can_be_negative": kind in _NEGATABLE_KINDS,
                "avg_score": round(float(avg), 2) if avg is not None else None,
            }
            for kind, count, bad, avg in by_kind
        ],
        "reasons": [
            {"reason": reason, "count": int(n), "negative": int(bad or 0)}
            for reason, n, bad in reasons
        ],
        "by_source": [{"source": src or "ui", "count": int(n)} for src, n in by_source],
        "daily": [
            {
                "day": d.date().isoformat(),
                "feedback": int(n),
                "negative": int(bad or 0),
                "avg_score": round(float(avg), 2) if avg is not None else None,
            }
            for d, n, bad, avg in daily
        ],
        "score": {
            "responses": int(scored[0] or 0),
            "mean": round(float(scored[1]), 2) if scored[1] is not None else None,
            "median": float(scored[2]) if scored[2] is not None else None,
            "scale": "1-5",
        },
        "backlog": [{"status": s or "new", "count": int(n)} for s, n in backlog],
        "oldest_untriaged": oldest.isoformat() if oldest else None,
    }


#: Rating kinds on which a complaint is even expressible. `ab` records which of
#: two options someone preferred; neither answer is negative.
_NEGATABLE_KINDS = ("thumbs", "likert5", "helpful")


# ----------------------------------------------------------- search facets --
async def search_filters(*, days: int = 30, limit: int = 20, since: Optional[str] = None, until: Optional[str] = None) -> Dict[str, Any]:
    """Which filters people apply, and which combinations find nothing.

    `search_query.filters` has been written in full since the first release and
    read by nothing. A facet that nobody touches is UI to remove; a facet that
    correlates with an empty result page is a catalogue gap with a name on it.
    """
    from sqlalchemy import String, cast

    from backend.postgres import POSTGRES_ASYNC_SESSION_FACTORY
    from sql import SearchQuery

    window = _window(days, since, until)
    size = _clamp(limit, 20, 100)
    in_window = SearchQuery.occurred_at.between(window.since, window.until)

    # How many facets one search carried. Used to tell a filtered search from a
    # bare one, and to average how many people apply at once.
    #
    # A correlated subquery over `jsonb_object_keys` rather than
    # `jsonb_path_query_array`: that function's second argument is `jsonpath`,
    # and a bound parameter arrives as `character varying`, so Postgres finds
    # no matching signature and the whole report 500s. Counting keys needs no
    # jsonpath at all.
    facet_count = (
        select(func.count())
        .select_from(func.jsonb_object_keys(SearchQuery.filters).alias("k"))
        .scalar_subquery()
    )

    async with POSTGRES_ASYNC_SESSION_FACTORY()() as db:
        # One row per key, so a search carrying three facets counts once
        # against each of them.
        #
        # Expanded in the select list of a subquery, which is the one form of
        # this that works without ceremony. A set-returning function in the
        # FROM clause needs LATERAL to see the table's own column, and even
        # then SQLAlchemy renders the alias without a column name — the
        # resulting `anon_1.facet does not exist` names the symptom and not
        # the cause. Postgres expands a set-returning function in the select
        # list directly, so this needs neither.
        expanded = (
            select(
                func.jsonb_object_keys(SearchQuery.filters).label("facet"),
                SearchQuery.zero_result.label("zero_result"),
            )
            .where(in_window)
            .subquery()
        )
        rows = (
            await db.execute(
                select(
                    expanded.c.facet,
                    func.count(),
                    func.count().filter(expanded.c.zero_result),
                )
                .group_by(expanded.c.facet)
                .order_by(func.count().desc())
                .limit(size)
            )
        ).all()

        breadth = (
            await db.execute(
                select(
                    func.count(),
                    func.count().filter(facet_count == 0),
                    func.avg(facet_count).filter(facet_count > 0),
                ).where(in_window)
            )
        ).one()

        # The whole point of keeping two result counts: a search that found
        # nothing on the first pass and something after relaxing is a near
        # miss, not a miss, and the two have never been told apart.
        recovery = (
            await db.execute(
                select(
                    func.count().filter(SearchQuery.result_count_first_pass == 0),
                    func.count().filter(
                        (SearchQuery.result_count_first_pass == 0)
                        & (SearchQuery.result_count_final > 0)
                    ),
                    func.count().filter(SearchQuery.zero_result),
                ).where(in_window, SearchQuery.result_count_first_pass.isnot(None))
            )
        ).one()

        # The exact filter object, as text, for the combinations that keep
        # coming back empty. A facet-by-facet rate cannot show that it is
        # *vegan plus under-20-minutes* that has nothing behind it.
        combo = cast(SearchQuery.filters, String)
        combos = (
            await db.execute(
                select(combo, func.count())
                .where(in_window, facet_count > 0, SearchQuery.zero_result)
                .group_by(combo)
                .order_by(func.count().desc())
                .limit(10)
            )
        ).all()

    total = int(breadth[0] or 0)
    unfiltered = int(breadth[1] or 0)
    empty_first = int(recovery[0] or 0)

    return {
        **window,
        "facets": [
            {
                "facet": name,
                "searches": int(n),
                "zero_result": int(bad or 0),
                "zero_result_rate": _ratio(int(bad or 0), int(n)),
            }
            for name, n, bad in rows
        ],
        "searches": total,
        "unfiltered": unfiltered,
        "filtered_rate": _ratio(total - unfiltered, total),
        "avg_facets_when_filtered": (
            round(float(breadth[2]), 1) if breadth[2] is not None else None
        ),
        "recovery": {
            "empty_first_pass": empty_first,
            "rescued": int(recovery[1] or 0),
            "recovery_rate": _ratio(int(recovery[1] or 0), empty_first),
            "true_misses": int(recovery[2] or 0),
        },
        "empty_combinations": [
            {"filters": combo, "searches": int(n)} for combo, n in combos
        ],
    }


# ----------------------------------------------------------------- audience --
async def audience_breakdown(*, days: int = 30, since: Optional[str] = None, until: Optional[str] = None) -> Dict[str, Any]:
    """Who is calling, in what language, from what.

    Three columns recorded on every event and read by nothing: the client that
    sent it, the roles the caller held, and the locale the interface was in.
    For a trilingual product the language split is not a curiosity, and "how
    much of our traffic is the SDK rather than the browser" was unanswerable.
    """
    from sqlalchemy import distinct, func as sqlfunc

    from backend.postgres import POSTGRES_ASYNC_SESSION_FACTORY
    from sql import ActivityEvent

    window = _window(days, since, until)
    in_window = ActivityEvent.occurred_at.between(window.since, window.until)

    async def group(db, column):
        rows = (
            await db.execute(
                select(
                    column,
                    func.count(),
                    func.count(distinct(ActivityEvent.client_session_id)),
                )
                .where(in_window)
                .group_by(column)
                .order_by(func.count().desc())
                .limit(25)
            )
        ).all()
        return [
            {"value": v, "events": int(n), "sessions": int(s)} for v, n, s in rows
        ]

    async with POSTGRES_ASYNC_SESSION_FACTORY()() as db:
        by_client = await group(db, ActivityEvent.client)
        by_locale = await group(db, ActivityEvent.locale)

        # roles is a text[]; unnest so one event held by an expert counts once
        # against "expert" rather than once against the whole array. Expanded
        # in a subquery rather than joined laterally in place — the aggregate
        # needs the user id alongside the role, and a subquery keeps both
        # visible without a correlation Postgres has to be told about.
        expanded = (
            select(
                sqlfunc.unnest(ActivityEvent.roles).label("role"),
                ActivityEvent.user_id.label("user_id"),
            )
            .where(in_window, ActivityEvent.roles.isnot(None))
            .subquery()
        )
        by_role = (
            await db.execute(
                select(
                    expanded.c.role,
                    func.count(distinct(expanded.c.user_id)),
                    func.count(),
                )
                .group_by(expanded.c.role)
                .order_by(func.count().desc())
                .limit(25)
            )
        ).all()

        guests = (
            await db.execute(
                select(
                    func.count().filter(ActivityEvent.is_guest),
                    func.count().filter(~ActivityEvent.is_guest),
                    func.count(distinct(ActivityEvent.household_id)),
                ).where(in_window)
            )
        ).one()

        # occurred_at is the client's clock, received_at is ours. A large gap
        # is a wrong device clock or a batch that sat in a closed laptop, and
        # both change how a timeline should be read.
        skew = (
            await db.execute(
                select(
                    func.count().filter(
                        func.abs(
                            func.extract(
                                "epoch", ActivityEvent.received_at - ActivityEvent.occurred_at
                            )
                        )
                        > 300
                    ),
                    func.max(
                        func.abs(
                            func.extract(
                                "epoch", ActivityEvent.received_at - ActivityEvent.occurred_at
                            )
                        )
                    ),
                ).where(in_window, ActivityEvent.received_at.isnot(None))
            )
        ).one()

    return {
        **window,
        "by_client": by_client,
        "by_locale": by_locale,
        "by_role": [
            {"role": r, "users": int(u), "events": int(n)} for r, u, n in by_role
        ],
        "guest_events": int(guests[0] or 0),
        "signed_in_events": int(guests[1] or 0),
        "households": int(guests[2] or 0),
        "clock_skew": {
            # Not a fault to act on unless it is large or growing; it is here
            # so a timeline that looks impossible has an explanation.
            "events_over_5min": int(skew[0] or 0),
            "worst_seconds": int(skew[1]) if skew[1] is not None else None,
        },
    }


# ------------------------------------------------------------------ review --
async def review_summary(*, days: int = 90, since: Optional[str] = None, until: Optional[str] = None) -> Dict[str, Any]:
    """What the experts concluded, in aggregate.

    Verdicts have been stored per target since the beginning and never counted.
    A console that lets experts record judgements but cannot say what they
    judged is a data-entry form, not a review tool.
    """
    from sqlalchemy import distinct

    from backend.postgres import POSTGRES_ASYNC_SESSION_FACTORY
    from sql import ExpertReview

    window = _window(days, since, until)
    in_window = ExpertReview.created_at.between(window.since, window.until)

    async with POSTGRES_ASYNC_SESSION_FACTORY()() as db:
        verdicts = (
            await db.execute(
                select(ExpertReview.verdict, func.count())
                .where(in_window)
                .group_by(ExpertReview.verdict)
                .order_by(func.count().desc())
            )
        ).all()

        reviewers = (
            await db.execute(
                select(
                    ExpertReview.reviewer_id,
                    func.max(ExpertReview.reviewer_name),
                    func.count(),
                    func.count(distinct(ExpertReview.target_id)),
                    func.max(ExpertReview.created_at),
                )
                .where(in_window)
                .group_by(ExpertReview.reviewer_id)
                .order_by(func.count().desc())
                .limit(50)
            )
        ).all()

        by_target_type = (
            await db.execute(
                select(
                    ExpertReview.target_type,
                    func.count(),
                    func.count(distinct(ExpertReview.target_id)),
                )
                .where(in_window)
                .group_by(ExpertReview.target_type)
            )
        ).all()

        # Expanded in a subquery for the same reason as the roles above: a
        # set-returning function reading a column of the table cannot sit
        # beside it in the FROM list without LATERAL, and the error it gives
        # — "missing FROM-clause entry" — names the table, not the cause.
        tag_rows = (
            select(func.unnest(ExpertReview.tags).label("tag"))
            .where(in_window, ExpertReview.tags.isnot(None))
            .subquery()
        )
        tags = (
            await db.execute(
                select(tag_rows.c.tag, func.count())
                .group_by(tag_rows.c.tag)
                .order_by(func.count().desc())
                .limit(25)
            )
        ).all()

        # More than one expert on the same thing is the only place disagreement
        # can show up, so it is worth knowing how often it happens at all.
        per_target = (
            select(
                ExpertReview.target_type,
                ExpertReview.target_id,
                func.count().label("reviews"),
                func.count(distinct(ExpertReview.verdict)).label("verdicts"),
            )
            .where(in_window)
            .group_by(ExpertReview.target_type, ExpertReview.target_id)
            .subquery()
        )
        agreement = (
            await db.execute(
                select(
                    func.count(),
                    func.count().filter(per_target.c.reviews > 1),
                    func.count().filter(per_target.c.verdicts > 1),
                ).select_from(per_target)
            )
        ).one()

    total = sum(int(n) for _v, n in verdicts)
    reviewed_twice = int(agreement[1] or 0)

    return {
        **window,
        "reviews": total,
        "by_verdict": [
            {"verdict": v, "count": int(n), "share": _ratio(int(n), total)}
            for v, n in verdicts
        ],
        "by_reviewer": [
            {
                "reviewer_id": rid,
                "reviewer_name": name,
                "reviews": int(n),
                "targets": int(t),
                "last_review": last.isoformat() if last else None,
            }
            for rid, name, n, t, last in reviewers
        ],
        "by_target_type": [
            {"target_type": t, "reviews": int(n), "targets": int(d)}
            for t, n, d in by_target_type
        ],
        "tags": [{"tag": t, "count": int(n)} for t, n in tags],
        "agreement": {
            "targets_reviewed": int(agreement[0] or 0),
            "reviewed_more_than_once": reviewed_twice,
            "disagreements": int(agreement[2] or 0),
            "disagreement_rate": _ratio(int(agreement[2] or 0), reviewed_twice),
        },
    }


# =============================================================================
# Real user monitoring. Reading back the device, the crashes, the clicks and
# the speed — see analytics/recorder.py for how any of it arrives.
# =============================================================================


async def session_board(
    *,
    days: int = 7,
    limit: int = 50,
    offset: int = 0,
    since: Optional[str] = None,
    until: Optional[str] = None,
    user_id: Optional[str] = None,
    device_type: Optional[str] = None,
    browser: Optional[str] = None,
    os: Optional[str] = None,
    country: Optional[str] = None,
    has_errors: Optional[bool] = None,
    search: Optional[str] = None,
    include_bots: bool = False,
) -> Dict[str, Any]:
    """Sessions as a board: who, on what, for how long, and did it break.

    The list the support conversation starts from. Somebody quotes the
    reference from the page footer, or says "it broke on my phone yesterday",
    and this is the only view that can turn either of those into a row.

    Filterable on every column anyone would think to ask about, because the
    question is never "show me all sessions" — it is "show me the ones on
    Safari", or "the ones that errored", or "this person's".
    """
    from sqlalchemy import distinct

    from backend.postgres import POSTGRES_ASYNC_SESSION_FACTORY
    from sql import ActivityEvent, ClientSession

    window = _window(days, since, until)
    size = _clamp(limit, 50, 200)
    start = max(0, int(offset or 0))

    filters = [ClientSession.started_at.between(window.since, window.until)]
    if not include_bots:
        # A crawler is not a person and a board full of them hides the ones
        # that are. Counted separately below rather than silently dropped.
        filters.append(ClientSession.is_bot.is_(False))
    if user_id:
        filters.append(ClientSession.user_id == user_id)
    if device_type:
        filters.append(ClientSession.device_type == device_type)
    if browser:
        filters.append(ClientSession.browser == browser)
    if os:
        filters.append(ClientSession.os == os)
    if country:
        filters.append(ClientSession.country == country.upper())
    if has_errors is True:
        filters.append(ClientSession.errors > 0)
    elif has_errors is False:
        filters.append(ClientSession.errors == 0)
    if search:
        # A prefix match on the session reference, which is what someone reads
        # off the footer and quotes with a typo in the last character.
        filters.append(ClientSession.session_id.ilike(f"{search.strip()}%"))

    async with POSTGRES_ASYNC_SESSION_FACTORY()() as db:
        total = await db.scalar(select(func.count()).select_from(ClientSession).where(*filters))
        bots = await db.scalar(
            select(func.count())
            .select_from(ClientSession)
            .where(
                ClientSession.started_at.between(window.since, window.until),
                ClientSession.is_bot.is_(True),
            )
        )
        rows = (
            (
                await db.execute(
                    select(ClientSession)
                    .where(*filters)
                    .order_by(ClientSession.started_at.desc())
                    .limit(size)
                    .offset(start)
                )
            )
            .scalars()
            .all()
        )

        # Activity is counted from the event table rather than trusted from the
        # session row's own rollup: the rollup is maintained on a best-effort
        # basis and this list is where a discrepancy would be believed.
        ids = [row.session_id for row in rows]
        activity = {}
        if ids:
            activity = {
                sid: (int(n), first, last, int(errors or 0))
                for sid, n, first, last, errors in (
                    await db.execute(
                        select(
                            ActivityEvent.client_session_id,
                            func.count(),
                            func.min(ActivityEvent.occurred_at),
                            func.max(ActivityEvent.occurred_at),
                            func.count().filter(ActivityEvent.status >= 400),
                        )
                        .where(ActivityEvent.client_session_id.in_(ids))
                        .group_by(ActivityEvent.client_session_id)
                    )
                ).all()
            }

        # The mix, over the whole filtered set rather than the visible page —
        # otherwise the summary describes the scroll position.
        def mix(column):
            return (
                select(column, func.count(), func.count(distinct(ClientSession.user_id)))
                .where(*filters)
                .group_by(column)
                .order_by(func.count().desc())
                .limit(15)
            )

        by_device = (await db.execute(mix(ClientSession.device_type))).all()
        by_browser = (await db.execute(mix(ClientSession.browser))).all()
        by_os = (await db.execute(mix(ClientSession.os))).all()
        by_country = (await db.execute(mix(ClientSession.country))).all()

        # Viewport widths, bucketed the way a responsive layout thinks: the
        # useful question is "how many people are below the tablet breakpoint",
        # not "what is the mean width".
        widths = (
            await db.execute(
                select(
                    func.count().filter(ClientSession.viewport_w < 640),
                    func.count().filter(ClientSession.viewport_w.between(640, 1023)),
                    func.count().filter(ClientSession.viewport_w.between(1024, 1439)),
                    func.count().filter(ClientSession.viewport_w >= 1440),
                    func.percentile_disc(0.5).within_group(ClientSession.viewport_w),
                ).where(*filters)
            )
        ).one()

    sessions = []
    for row in rows:
        payload = row.to_dict()
        events, first, last, errors = activity.get(row.session_id, (0, None, None, 0))
        payload["events"] = events
        payload["failed_requests"] = errors
        span_start = first or row.started_at
        span_end = last or row.last_seen_at
        payload["duration_seconds"] = (
            int((span_end - span_start).total_seconds())
            if span_start and span_end
            else None
        )
        sessions.append(payload)

    def named(rows_in, key):
        return [
            {key: value or "unknown", "sessions": int(n), "users": int(u or 0)}
            for value, n, u in rows_in
        ]

    return {
        **window,
        "total": int(total or 0),
        # Excluded from every figure above unless asked for. Shown so a big
        # gap between traffic and sessions has a visible explanation.
        "bots": int(bots or 0),
        "offset": start,
        "limit": size,
        "sessions": sessions,
        "by_device": named(by_device, "device_type"),
        "by_browser": named(by_browser, "browser"),
        "by_os": named(by_os, "os"),
        "by_country": named(by_country, "country"),
        "viewports": {
            "phone": int(widths[0] or 0),
            "tablet": int(widths[1] or 0),
            "laptop": int(widths[2] or 0),
            "desktop": int(widths[3] or 0),
            "median_width": int(widths[4]) if widths[4] is not None else None,
        },
    }


async def session_device(session_id: str) -> Optional[Dict[str, Any]]:
    """The machine one session ran on, and what broke on it."""
    from backend.postgres import POSTGRES_ASYNC_SESSION_FACTORY
    from sql import ClientError, ClientSession, UIInteraction, WebVital

    async with POSTGRES_ASYNC_SESSION_FACTORY()() as db:
        row = await db.get(ClientSession, session_id)
        errors = (
            (
                await db.execute(
                    select(ClientError)
                    .where(ClientError.client_session_id == session_id)
                    .order_by(ClientError.occurred_at.asc())
                    .limit(50)
                )
            )
            .scalars()
            .all()
        )
        vitals = (
            await db.execute(
                select(WebVital.metric, WebVital.path, WebVital.value, WebVital.rating)
                .where(WebVital.client_session_id == session_id)
                .order_by(WebVital.occurred_at.asc())
                .limit(50)
            )
        ).all()
        trouble = (
            await db.execute(
                select(
                    UIInteraction.kind,
                    UIInteraction.path,
                    UIInteraction.element_key,
                    func.sum(UIInteraction.repeats),
                )
                .where(
                    UIInteraction.client_session_id == session_id,
                    UIInteraction.kind.in_(("rage", "dead")),
                )
                .group_by(UIInteraction.kind, UIInteraction.path, UIInteraction.element_key)
                .order_by(func.sum(UIInteraction.repeats).desc())
                .limit(20)
            )
        ).all()

    if row is None and not errors:
        return None

    return {
        "device": row.to_dict() if row is not None else None,
        "errors": [error.to_dict() for error in errors],
        "vitals": [
            {"metric": m, "path": p, "value": float(v), "rating": r}
            for m, p, v, r in vitals
        ],
        # Somebody clicking the same thing five times, or clicking something
        # that does nothing. The clearest signal of frustration the platform
        # can record, and the reason to open a session at all.
        "frustration": [
            {"kind": k, "path": p, "element_key": e, "clicks": int(n or 0)}
            for k, p, e, n in trouble
        ],
    }


async def error_groups(
    *,
    days: int = 7,
    limit: int = 50,
    since: Optional[str] = None,
    until: Optional[str] = None,
    status: Optional[str] = None,
    app: Optional[str] = None,
) -> Dict[str, Any]:
    """Distinct failures, worst first.

    Ranked by people affected rather than by occurrence count. A loop that
    throws two thousand times for one person is a bug; the same error hitting
    two hundred people is an incident, and an occurrence count puts them the
    wrong way round.
    """
    from backend.postgres import POSTGRES_ASYNC_SESSION_FACTORY
    from sql import ClientError, ErrorGroup

    window = _window(days, since, until)
    size = _clamp(limit, 50, 200)

    filters = [ErrorGroup.last_seen_at >= window.since]
    if status:
        filters.append(ErrorGroup.status == status)
    if app:
        filters.append(ErrorGroup.app == app)

    async with POSTGRES_ASYNC_SESSION_FACTORY()() as db:
        groups = (
            (
                await db.execute(
                    select(ErrorGroup)
                    .where(*filters)
                    .order_by(ErrorGroup.sessions.desc(), ErrorGroup.last_seen_at.desc())
                    .limit(size)
                )
            )
            .scalars()
            .all()
        )
        total = await db.scalar(select(func.count()).select_from(ErrorGroup).where(*filters))

        in_window = ClientError.occurred_at.between(window.since, window.until)
        totals = (
            await db.execute(
                select(
                    func.count(),
                    func.count(func.distinct(ClientError.client_session_id)),
                    func.count(func.distinct(ClientError.fingerprint)),
                    func.count().filter(~ClientError.handled),
                ).where(in_window)
            )
        ).one()

        day = func.date_trunc("day", ClientError.occurred_at)
        daily = (
            await db.execute(
                select(day, func.count(), func.count(func.distinct(ClientError.client_session_id)))
                .where(in_window)
                .group_by(day)
                .order_by(day)
            )
        ).all()

        by_browser = (
            await db.execute(
                select(ClientError.browser, func.count())
                .where(in_window)
                .group_by(ClientError.browser)
                .order_by(func.count().desc())
                .limit(10)
            )
        ).all()

        # New in this window: a group first seen inside it is a regression, and
        # is the one thing here that points at a specific deploy.
        new_groups = (
            await db.execute(
                select(func.count()).where(
                    ErrorGroup.first_seen_at >= window.since,
                    ErrorGroup.last_seen_at >= window.since,
                )
            )
        ).scalar()

    occurrences = int(totals[0] or 0)
    return {
        **window,
        "total_groups": int(total or 0),
        "groups": [group.to_dict() for group in groups],
        "occurrences": occurrences,
        "sessions_affected": int(totals[1] or 0),
        "distinct_errors": int(totals[2] or 0),
        "unhandled": int(totals[3] or 0),
        "unhandled_rate": _ratio(int(totals[3] or 0), occurrences),
        "new_groups": int(new_groups or 0),
        "daily": [
            {"day": d.date().isoformat(), "errors": int(n), "sessions": int(s)}
            for d, n, s in daily
        ],
        "by_browser": [
            {"browser": b or "unknown", "errors": int(n)} for b, n in by_browser
        ],
    }


async def error_group_detail(fingerprint: str, *, limit: int = 25) -> Optional[Dict[str, Any]]:
    """One failure: its occurrences, and what they had in common.

    The "what they had in common" is the part that shortens a debugging
    session. An error on one browser is a compatibility bug, on one route a
    logic bug, and for one user a data bug — and those are three different
    afternoons.
    """
    from backend.postgres import POSTGRES_ASYNC_SESSION_FACTORY
    from sql import ClientError, ErrorGroup

    size = _clamp(limit, 25, 100)

    async with POSTGRES_ASYNC_SESSION_FACTORY()() as db:
        group = await db.get(ErrorGroup, fingerprint)
        if group is None:
            return None
        same = ClientError.fingerprint == fingerprint
        occurrences = (
            (
                await db.execute(
                    select(ClientError)
                    .where(same)
                    .order_by(ClientError.occurred_at.desc())
                    .limit(size)
                )
            )
            .scalars()
            .all()
        )

        async def common(column, cap: int = 8):
            rows = (
                await db.execute(
                    select(column, func.count())
                    .where(same)
                    .group_by(column)
                    .order_by(func.count().desc())
                    .limit(cap)
                )
            ).all()
            return [{"value": v or "unknown", "count": int(n)} for v, n in rows]

        by_browser = await common(ClientError.browser)
        by_os = await common(ClientError.os)
        by_device = await common(ClientError.device_type)
        by_path = await common(ClientError.url_path, 12)
        by_release = await common(ClientError.release)

        day = func.date_trunc("day", ClientError.occurred_at)
        daily = (
            await db.execute(
                select(day, func.count()).where(same).group_by(day).order_by(day)
            )
        ).all()

    return {
        "group": group.to_dict(),
        "occurrences": [occurrence.to_dict() for occurrence in occurrences],
        "by_browser": by_browser,
        "by_os": by_os,
        "by_device": by_device,
        "by_path": by_path,
        "by_release": by_release,
        "daily": [{"day": d.date().isoformat(), "errors": int(n)} for d, n in daily],
    }


async def set_error_status(
    fingerprint: str, status: str, *, actor: Optional[str] = None
) -> bool:
    """Mark a failure acknowledged, resolved or ignored."""
    from sqlalchemy import update

    from backend.postgres import POSTGRES_ASYNC_SESSION_FACTORY
    from sql import ErrorGroup

    if status not in ERROR_STATUSES:
        raise ValueError(f"status must be one of {', '.join(sorted(ERROR_STATUSES))}")

    async with POSTGRES_ASYNC_SESSION_FACTORY()() as db:
        values: Dict[str, Any] = {"status": status}
        if status == "resolved":
            values["resolved_at"] = datetime.now(timezone.utc)
            values["resolved_by"] = actor
        else:
            # Un-resolving clears the record of who resolved it, so a group
            # cannot show as reopened and still credit somebody with the fix.
            values["resolved_at"] = None
            values["resolved_by"] = None
        result = await db.execute(
            update(ErrorGroup).where(ErrorGroup.fingerprint == fingerprint).values(**values)
        )
        await db.commit()
        return bool(result.rowcount)


#: What an error group's status may be. `new` is where everything starts;
#: a group that recurs after being resolved is moved back to it automatically.
ERROR_STATUSES = ("new", "acknowledged", "resolved", "ignored")


async def click_map(
    *,
    path: str,
    days: int = 30,
    grid: int = 40,
    since: Optional[str] = None,
    until: Optional[str] = None,
    device_type: Optional[str] = None,
) -> Dict[str, Any]:
    """Where people clicked on one page, two ways.

    By element and by coordinate, from the same rows, because they answer
    different questions. The element list names things you can change — a
    control nobody uses is UI to remove. The grid draws the picture, and the
    picture is what shows people clicking something that is not a button.

    Aggregated server-side into a grid rather than returned as points: a busy
    page is hundreds of thousands of clicks, and a browser asked to plot those
    individually will simply stop responding. The grid is the same picture at a
    thousandth of the size.
    """
    from sqlalchemy import Integer, cast

    from backend.postgres import POSTGRES_ASYNC_SESSION_FACTORY
    from sql import UIInteraction

    window = _window(days, since, until)
    # Bounded because the response is grid x grid cells: 100 is ten thousand.
    cells = max(8, min(int(grid or 40), 100))

    filters = [
        UIInteraction.occurred_at.between(window.since, window.until),
        UIInteraction.path == path,
    ]
    if device_type:
        filters.append(UIInteraction.viewport_w.isnot(None))
        if device_type == "phone":
            filters.append(UIInteraction.viewport_w < 640)
        elif device_type == "tablet":
            filters.append(UIInteraction.viewport_w.between(640, 1023))
        else:
            filters.append(UIInteraction.viewport_w >= 1024)

    positioned = filters + [
        UIInteraction.x_pct.isnot(None),
        UIInteraction.y_pct.isnot(None),
    ]

    async with POSTGRES_ASYNC_SESSION_FACTORY()() as db:
        # Integer division onto the grid. `least` keeps a click at exactly the
        # right or bottom edge inside the last cell instead of one past it.
        col = func.least(cast(UIInteraction.x_pct * cells / 10000, Integer), cells - 1)
        row = func.least(cast(UIInteraction.y_pct * cells / 10000, Integer), cells - 1)
        heat = (
            await db.execute(
                select(
                    col,
                    row,
                    func.sum(UIInteraction.repeats),
                    func.count().filter(UIInteraction.kind == "rage"),
                    func.count().filter(UIInteraction.kind == "dead"),
                )
                .where(*positioned)
                .group_by(col, row)
                .order_by(func.sum(UIInteraction.repeats).desc())
                .limit(cells * cells)
            )
        ).all()

        elements = (
            await db.execute(
                select(
                    UIInteraction.element_key,
                    UIInteraction.element_role,
                    func.sum(UIInteraction.repeats),
                    func.count(func.distinct(UIInteraction.client_session_id)),
                    func.count().filter(UIInteraction.kind == "rage"),
                    func.count().filter(UIInteraction.kind == "dead"),
                    # Where on the page this control sits, averaged over its
                    # clicks. The map is otherwise an abstract cloud with no
                    # reference points — which reads as broken rather than as
                    # a density plot. Labelling the hot regions with the thing
                    # that was clicked is what a screenshot would have given,
                    # from data already on the row.
                    func.avg(UIInteraction.x_pct),
                    func.avg(UIInteraction.y_pct),
                )
                .where(*filters, UIInteraction.element_key.isnot(None))
                .group_by(UIInteraction.element_key, UIInteraction.element_role)
                .order_by(func.sum(UIInteraction.repeats).desc())
                .limit(60)
            )
        ).all()

        totals = (
            await db.execute(
                select(
                    func.coalesce(func.sum(UIInteraction.repeats), 0),
                    func.count(func.distinct(UIInteraction.client_session_id)),
                    func.count().filter(UIInteraction.kind == "rage"),
                    func.count().filter(UIInteraction.kind == "dead"),
                    func.percentile_disc(0.5).within_group(UIInteraction.depth_pct),
                ).where(*filters)
            )
        ).one()

        # How far down people actually get. A page whose median reader stops at
        # 30% has everything below that written for nobody.
        depth = (
            await db.execute(
                select(
                    func.count().filter(UIInteraction.depth_pct >= 2500),
                    func.count().filter(UIInteraction.depth_pct >= 5000),
                    func.count().filter(UIInteraction.depth_pct >= 7500),
                    func.count().filter(UIInteraction.depth_pct >= 9500),
                    func.count(),
                ).where(*filters, UIInteraction.kind == "scroll")
            )
        ).one()

    scrolls = int(depth[4] or 0)
    peak = max((int(n or 0) for _c, _r, n, _g, _d in heat), default=0)

    return {
        **window,
        "path": path,
        "grid": cells,
        "clicks": int(totals[0] or 0),
        "sessions": int(totals[1] or 0),
        "rage_clicks": int(totals[2] or 0),
        "dead_clicks": int(totals[3] or 0),
        "median_scroll_depth": (
            round(float(totals[4]) / 100, 1) if totals[4] is not None else None
        ),
        # The busiest cell, so a client can scale its colours against the real
        # maximum instead of guessing one.
        "peak": peak,
        "cells": [
            {
                "x": int(c),
                "y": int(r),
                "clicks": int(n or 0),
                "rage": int(rage or 0),
                "dead": int(dead or 0),
                # Pre-divided, so the drawing code does not have to find the
                # maximum before it can render a single cell.
                "intensity": round(int(n or 0) / peak, 4) if peak else 0.0,
            }
            for c, r, n, rage, dead in heat
        ],
        "elements": [
            {
                "element_key": key,
                "element_role": role,
                "clicks": int(n or 0),
                "sessions": int(s or 0),
                "rage": int(rage or 0),
                "dead": int(dead or 0),
                # Ten-thousandths of the page box, same scale as the cells.
                "x_pct": int(x) if x is not None else None,
                "y_pct": int(y) if y is not None else None,
            }
            for key, role, n, s, rage, dead, x, y in elements
        ],
        "scroll_depth": {
            "measured": scrolls,
            "reached_25": _ratio(int(depth[0] or 0), scrolls),
            "reached_50": _ratio(int(depth[1] or 0), scrolls),
            "reached_75": _ratio(int(depth[2] or 0), scrolls),
            "reached_bottom": _ratio(int(depth[3] or 0), scrolls),
        },
    }


async def interaction_overview(
    *, days: int = 30, limit: int = 25,
    offset: int = 0, since: Optional[str] = None, until: Optional[str] = None
) -> Dict[str, Any]:
    """Which pages people click on, and which ones frustrate them.

    The index into the heatmaps. Sorted so the pages worth opening are the ones
    at the top: a page with rage clicks is a page where something looks like it
    should work and does not.
    """
    from backend.postgres import POSTGRES_ASYNC_SESSION_FACTORY
    from sql import UIInteraction

    window = _window(days, since, until)
    size, start = _page(limit, offset, 25, 100)
    in_window = UIInteraction.occurred_at.between(window.since, window.until)

    async with POSTGRES_ASYNC_SESSION_FACTORY()() as db:
        pages = (
            await db.execute(
                select(
                    UIInteraction.path,
                    func.coalesce(func.sum(UIInteraction.repeats), 0),
                    func.count(func.distinct(UIInteraction.client_session_id)),
                    func.count().filter(UIInteraction.kind == "rage"),
                    func.count().filter(UIInteraction.kind == "dead"),
                    func.percentile_disc(0.5).within_group(UIInteraction.depth_pct),
                )
                .where(in_window)
                .group_by(UIInteraction.path)
                .order_by(func.coalesce(func.sum(UIInteraction.repeats), 0).desc())
                .limit(size)
                .offset(start)
            )
        ).all()

        worst = (
            await db.execute(
                select(
                    UIInteraction.path,
                    UIInteraction.element_key,
                    UIInteraction.kind,
                    func.count(),
                    func.count(func.distinct(UIInteraction.client_session_id)),
                )
                .where(in_window, UIInteraction.kind.in_(("rage", "dead")))
                .group_by(UIInteraction.path, UIInteraction.element_key, UIInteraction.kind)
                .order_by(func.count(func.distinct(UIInteraction.client_session_id)).desc())
                .limit(20)
            )
        ).all()

    return {
        **window,
        "pages": [
            {
                "path": path,
                "clicks": int(clicks or 0),
                "sessions": int(sessions or 0),
                "rage": int(rage or 0),
                "dead": int(dead or 0),
                "median_scroll_depth": (
                    round(float(depth) / 100, 1) if depth is not None else None
                ),
            }
            for path, clicks, sessions, rage, dead, depth in pages
        ],
        "frustration": [
            {
                "path": path,
                "element_key": element,
                "kind": kind,
                "count": int(n),
                "sessions": int(s or 0),
            }
            for path, element, kind, n, s in worst
        ],
    }


async def vitals_report(
    *, days: int = 7, limit: int = 25, since: Optional[str] = None, until: Optional[str] = None
) -> Dict[str, Any]:
    """How fast pages felt, as the browser measured it.

    Percentiles, and the 75th specifically, because that is the threshold the
    web-vitals standard itself is defined against: a page passes if three
    quarters of visits are good. A mean would let a fast desktop majority hide
    a phone minority for whom the page is unusable.
    """
    from backend.postgres import POSTGRES_ASYNC_SESSION_FACTORY
    from sql import WebVital

    window = _window(days, since, until)
    size = _clamp(limit, 25, 100)
    in_window = WebVital.occurred_at.between(window.since, window.until)

    async with POSTGRES_ASYNC_SESSION_FACTORY()() as db:
        def measures(*group):
            return (
                select(
                    *group,
                    func.count(),
                    func.percentile_disc(0.5).within_group(WebVital.value),
                    func.percentile_disc(0.75).within_group(WebVital.value),
                    func.percentile_disc(0.95).within_group(WebVital.value),
                    func.count().filter(WebVital.rating == "good"),
                    func.count().filter(WebVital.rating == "poor"),
                )
                .where(in_window)
                .group_by(*group)
            )

        by_metric = (await db.execute(measures(WebVital.metric))).all()
        by_device = (
            await db.execute(measures(WebVital.metric, WebVital.device_type))
        ).all()
        by_path = (
            await db.execute(
                measures(WebVital.path, WebVital.metric)
                .order_by(func.count().desc())
                .limit(size * 5)
            )
        ).all()

    def summarise(count, p50, p75, p95, good, poor):
        total = int(count or 0)
        return {
            "samples": total,
            "p50": float(p50) if p50 is not None else None,
            # The number the standard is judged on.
            "p75": float(p75) if p75 is not None else None,
            "p95": float(p95) if p95 is not None else None,
            "good_rate": _ratio(int(good or 0), total),
            "poor_rate": _ratio(int(poor or 0), total),
        }

    return {
        **window,
        "by_metric": [
            {"metric": metric, **summarise(*rest)} for metric, *rest in by_metric
        ],
        "by_device": [
            {"metric": metric, "device_type": device or "unknown", **summarise(*rest)}
            for metric, device, *rest in by_device
        ],
        "by_path": [
            {"path": path, "metric": metric, **summarise(*rest)}
            for path, metric, *rest in by_path
        ],
    }


# --------------------------------------------------- feedback in context --
async def feedback_context(feedback_id: int) -> Optional[Dict[str, Any]]:
    """What a piece of feedback was actually about.

    A rating arrives in the inbox as a verdict, a target type and an opaque id.
    The reviewer sees that somebody was unhappy and no way to see with what —
    which makes the inbox a list of complaints nobody can act on.

    Resolved at read time rather than captured with the rating, for three
    reasons. The conversation already lives in the service that owns it, so
    copying it into the analytics tables would duplicate personal data into a
    second place that then needs its own erasure path. It works retroactively
    on every rating already collected. And a reviewer opening one item is a
    handful of requests a day, not a cost on the write path.

    Never raises: a service being down, or a conversation having been pruned,
    returns context with `available: false` and the inbox still renders.
    """
    from backend.postgres import POSTGRES_ASYNC_SESSION_FACTORY
    from sql import FeedbackRecord

    async with POSTGRES_ASYNC_SESSION_FACTORY()() as db:
        row = await db.get(FeedbackRecord, feedback_id)
    if row is None:
        return None

    context: Dict[str, Any] = {
        "feedback": row.to_dict(),
        "kind": row.target_type,
        "available": False,
        # Why it is not available, when it is not. "We could not reach the
        # service" and "this conversation no longer exists" are different
        # facts and a reviewer should not have to guess which they hit.
        "reason": None,
        "exchange": None,
    }
    if not row.target_id:
        context["reason"] = "This rating was not attached to anything specific."
        return context

    try:
        if row.target_type == "chat_message":
            from backend.foodchat import FoodchatBackend

            context["exchange"] = await FoodchatBackend.get(
                f"/foodchat/review/messages/{row.target_id}/context"
            )
        elif row.target_type == "qa_answer":
            from backend.foodscholar import FOODSCHOLAR

            # The Q&A review service already returns the question, the answer
            # and its sources — the same payload the review page reads.
            context["exchange"] = await FOODSCHOLAR.get_qa_request(str(row.target_id))
        else:
            context["reason"] = (
                f"No conversation is stored for a '{row.target_type}' rating."
            )
            return context
    except Exception:
        logger.info(
            "analytics.feedback_context_unavailable",
            extra={"feedback_id": feedback_id, "target": row.target_type},
            exc_info=True,
        )
        context["reason"] = "The service that holds this conversation did not answer."
        return context

    if not context["exchange"]:
        context["reason"] = (
            "The exchange is no longer stored. Conversations are pruned, and a "
            "rating outlives the thing it was about."
        )
        return context

    context["available"] = True
    return context


async def complaint_counts(target_type: str, target_ids: List[str]) -> Dict[str, Dict[str, int]]:
    """Open and total complaints for a set of things, in one statement.

    So a curation list can carry a badge. Until this, a report could only be
    found by opening the recipe you already suspected — which is the wrong way
    round: the list is where you go to find out *which* one to suspect.

    Keyed by target id; ids with nothing against them are simply absent, so a
    caller renders a badge only where there is something to say.
    """
    wanted = [str(t) for t in target_ids if t]
    if not wanted:
        return {}
    from backend.postgres import POSTGRES_ASYNC_SESSION_FACTORY
    from sql import FeedbackRecord

    try:
        async with POSTGRES_ASYNC_SESSION_FACTORY()() as db:
            rows = (
                await db.execute(
                    select(
                        FeedbackRecord.target_id,
                        func.count(),
                        func.count().filter(FeedbackRecord.status != "resolved"),
                    )
                    .where(
                        FeedbackRecord.target_type == target_type,
                        FeedbackRecord.target_id.in_(wanted),
                    )
                    .group_by(FeedbackRecord.target_id)
                )
            ).all()
    except Exception:
        # A badge is not worth failing the list it sits on.
        logger.debug("analytics.complaint_counts_failed", exc_info=True)
        return {}
    return {
        str(target_id): {"total": int(total), "open": int(open_count or 0)}
        for target_id, total, open_count in rows
    }
