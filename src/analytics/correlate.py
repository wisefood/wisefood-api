"""Filling in who a service-reported row belongs to.

FoodChat never holds a Keycloak subject. It works in household members, so
every row it reports carries a `member_id` and leaves `user_id` NULL. Every
per-user report groups on `user_id`. The result is that a whole product — its
activity, its turns, its token spend — was absent from every per-person view,
and nothing in the console said so.

Resolving a member to a user is not the answer. A household member is a person
in a household, not an account: only the owner maps to a Keycloak user, and
attributing a child's activity to the parent who pays for the account would be
a wrong answer that looks like a right one.

What is well defined is the *request*. The gateway records its own row for the
same `request_id`, and that row knows exactly who was holding the token. So the
question this module answers is "who made this request", not "who is this
member" — and it answers it by copying from the gateway's own row.

Two properties fall out of that and are worth stating:

* **Consent is respected for free.** The gateway's row carries a `user_id` only
  if that user consented to being named; consent stripping nulls it otherwise.
  Copying a NULL copies a NULL.
* **It is idempotent and bounded.** Only rows that are still unattributed are
  touched, only within a recent window, and only in capped batches, so it can
  run on a timer without ever becoming the expensive thing in the database.
"""

from __future__ import annotations

import logging
from typing import Dict

from sqlalchemy import text

logger = logging.getLogger(__name__)

#: How far back a pass looks. A row is correlated within seconds of arriving in
#: normal operation; the window only has to cover an outage of the resolver
#: itself, not the retention period.
DEFAULT_LOOKBACK_HOURS = 48

#: Rows changed per table per pass. Keeps one pass's lock footprint small
#: enough that it never competes with the recorder's inserts.
DEFAULT_BATCH = 5000

#: The tables that carry both a request id and an identity, mapped to whether
#: they also carry `is_guest`. Two of them do not — a token count and a rating
#: have no use for it — and setting a column that is not there fails the whole
#: statement, which is how these two tables came to be silently skipped while
#: the other two worked.
_TARGETS = {
    "event": True,
    "search_query": True,
    "llm_usage": False,
    "feedback": False,
}

_RESOLVE = """
UPDATE analytics.{table} AS target
SET user_id = source.user_id{guest_assignment}
FROM (
    -- One user per request, or no attribution at all. A caller may supply its
    -- own X-Request-Id, so two people can end up sharing one — by accident or
    -- because somebody guessed an id in flight. Where that happens the
    -- correct answer is unknown, and an unattributed row is a smaller error
    -- than one attributed to the wrong person.
    SELECT request_id,
           min(user_id) AS user_id,
           bool_and(is_guest) AS is_guest
    FROM analytics.event
    WHERE request_id IS NOT NULL
      AND user_id IS NOT NULL
      AND occurred_at >= now() - make_interval(hours => :hours)
    GROUP BY request_id
    HAVING count(DISTINCT user_id) = 1
) AS source
WHERE target.request_id = source.request_id
  AND target.user_id IS NULL
  AND target.occurred_at >= now() - make_interval(hours => :hours)
  AND target.id IN (
      SELECT id FROM analytics.{table}
      WHERE user_id IS NULL
        AND request_id IS NOT NULL
        AND occurred_at >= now() - make_interval(hours => :hours)
      LIMIT :batch
  )
"""


#: A browser session, attributed from the activity recorded during it.
#:
#: The other statements here key on the request; this one keys on the session,
#: because a session row is written once — at the first beacon of a visit —
#: and has no request to borrow from. When that beacon goes out before the
#: token is available, or before the person signs in at all, the row is
#: written with no identity and nothing ever fills it in.
#:
#: The consequence was a page that contradicted itself: the people report
#: counts a person's sessions from `analytics.event`, whose identity *is*
#: correlated, so it said "3 sessions"; the session board filters
#: `client_session.user_id`, which was never correlated, so opening that
#: person showed none of them.
#:
#: Same guard as the request join, for the same reason: a browser session can
#: legitimately carry two people (someone signs out, someone else signs in),
#: and where it does the right answer is unknown. Consent is respected for
#: free — `event.user_id` is already NULL for anyone who did not consent, and
#: copying a NULL copies a NULL.
_RESOLVE_SESSIONS = """
UPDATE analytics.client_session AS target
SET user_id = source.user_id
FROM (
    SELECT client_session_id,
           min(user_id) AS user_id
    FROM analytics.event
    WHERE client_session_id IS NOT NULL
      AND user_id IS NOT NULL
      AND occurred_at >= now() - make_interval(hours => :hours)
    GROUP BY client_session_id
    HAVING count(DISTINCT user_id) = 1
) AS source
WHERE target.session_id = source.client_session_id
  AND target.user_id IS NULL
  AND target.started_at >= now() - make_interval(hours => :hours)
  AND target.session_id IN (
      SELECT session_id FROM analytics.client_session
      WHERE user_id IS NULL
        AND started_at >= now() - make_interval(hours => :hours)
      LIMIT :batch
  )
"""


#: Feedback carries the request that produced the thing being rated, and the
#: model call for that same request recorded its Langfuse trace. Joining the
#: two turns a column that nothing ever wrote into a link from a complaint to
#: the exact generation that caused it — which is the first thing anyone wants
#: when reading a negative rating on an answer.
_LINK_FEEDBACK_TRACE = """
UPDATE analytics.feedback AS target
SET langfuse_trace_id = source.trace_id
FROM (
    SELECT DISTINCT ON (request_id) request_id, trace_id
    FROM analytics.llm_usage
    WHERE request_id IS NOT NULL
      AND trace_id IS NOT NULL
      AND occurred_at >= now() - make_interval(hours => :hours)
    ORDER BY request_id, occurred_at DESC
) AS source
WHERE target.request_id = source.request_id
  AND target.langfuse_trace_id IS NULL
  AND target.occurred_at >= now() - make_interval(hours => :hours)
"""


async def link_feedback_traces(*, hours: int = DEFAULT_LOOKBACK_HOURS) -> int:
    """Point each piece of feedback at the generation it is about.

    Never raises: this runs on the same timer as identity resolution and a
    failure here must not stop that.
    """
    from analytics.db import session_factory

    try:
        async with session_factory()() as db:
            result = await db.execute(
                text(_LINK_FEEDBACK_TRACE), {"hours": max(1, min(int(hours), 24 * 30))}
            )
            await db.commit()
            linked = int(result.rowcount or 0)
        if linked:
            logger.info("analytics.feedback_traces_linked", extra={"rows": linked})
        return linked
    except Exception:
        logger.warning("analytics.feedback_trace_link_failed", exc_info=True)
        return 0


async def resolve_identities(
    *, hours: int = DEFAULT_LOOKBACK_HOURS, batch: int = DEFAULT_BATCH
) -> Dict[str, int]:
    """Attribute unattributed rows from the gateway's row for the same request.

    Returns the number of rows filled per table. Never raises: this runs on a
    timer beside the recorder, and a correlation failure must not be able to
    stop anything being recorded.
    """
    # Background work, so the analytics pool rather than the shared one.
    from analytics.db import session_factory

    filled: Dict[str, int] = {}
    hours = max(1, min(int(hours), 24 * 30))
    batch = max(1, min(int(batch), 100_000))

    for table, has_guest in _TARGETS.items():
        try:
            # One transaction per table: a failure on one leaves the others
            # applied, which is the right trade for a job that will run again
            # in a minute anyway.
            async with session_factory()() as db:
                statement = _RESOLVE.format(
                    table=table,
                    guest_assignment=",\n    is_guest = source.is_guest" if has_guest else "",
                )
                result = await db.execute(
                    text(statement), {"hours": hours, "batch": batch}
                )
                await db.commit()
                filled[table] = int(result.rowcount or 0)
        except Exception:
            # Warned, not swallowed: the previous version logged at this level
            # too and the failure still went unnoticed for two whole tables,
            # so the table name is in the message rather than only in `extra`.
            logger.warning(
                "analytics.correlate_failed table=%s", table, exc_info=True
            )
            filled[table] = -1

    # Sessions, keyed on the session rather than the request. Separate because
    # the statement joins on a different column and the table has no
    # `request_id` to offer.
    try:
        async with session_factory()() as db:
            result = await db.execute(
                text(_RESOLVE_SESSIONS), {"hours": hours, "batch": batch}
            )
            await db.commit()
            filled["client_session"] = int(result.rowcount or 0)
    except Exception:
        logger.warning("analytics.correlate_failed table=client_session", exc_info=True)
        filled["client_session"] = -1

    total = sum(count for count in filled.values() if count > 0)
    if total:
        logger.info("analytics.correlated", extra={"rows": total, "tables": filled})
    return filled
