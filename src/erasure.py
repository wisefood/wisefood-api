"""Account and data erasure, shared by guest teardown and self-service deletion.

The teardown order matters and is the reason this lives in one place: external
chat sessions first (they are not in our database and no cascade reaches them),
then the household (which cascades to members, profiles, meal plans, favorites,
adapted recipes and saved items), then the Keycloak user last — the household's
FK to the Keycloak user is ON DELETE SET NULL, so deleting the user first would
leave an orphaned household behind.

What is deliberately NOT erased:

- ``wisefood.user_consent`` rows. The ledger is append-only by contract (see
  ``api.v1.users.UserConsentEntity``): it is the record that a lawful basis
  existed for the processing that happened. Once the Keycloak user is gone the
  stored ``user_id`` no longer resolves to a person, so the retained row is
  pseudonymous. Callers must tell the user this rather than promising that
  literally everything disappears.
- ``analytics.*`` rows. They are anonymised in place rather than deleted: the
  identity columns are nulled, the row survives. Deleting them would silently
  change aggregates that were already computed and reported — last month's
  active-user count would drop when someone closes their account today, which
  is both wrong and impossible to explain. Once the identity is gone the row
  says only "somebody searched for X", which is not personal data.
- FoodScholar Q&A sessions, which carry their own Redis TTL and expire on their
  own without an identity to hang from.
- Langfuse traces of model requests, which live in a separate system this code
  does not reach. They are keyed to the member id, so they become pseudonymous
  once the member rows are gone, but they are NOT deleted. The privacy notice
  says so; do not let a caller claim otherwise.
"""
import logging
from typing import Any, Dict

from backend.keycloak import KEYCLOAK_ADMIN_CLIENT

logger = logging.getLogger(__name__)


async def purge_user(user_id: str, *, delete_account: bool = True) -> Dict[str, Any]:
    """Erase everything provisioned for a Keycloak user.

    Best-effort per step and never raises for a partial failure: a household
    that will not delete must not stop the Keycloak user from being removed, or
    the caller would be left logged in to an account they asked us to erase.
    Each failure is logged and reported in the returned summary so the caller
    can tell the user what actually happened.

    :param user_id: Keycloak user id (token ``sub`` claim)
    :param delete_account: also delete the Keycloak user itself
    :return: summary of what was removed and what failed
    """
    from api.v1.households import HOUSEHOLD
    from backend.foodchat import FOODCHAT

    summary: Dict[str, Any] = {
        "user_id": user_id,
        "households_deleted": 0,
        "members_deleted": 0,
        "chat_sessions_deleted": 0,
        "analytics_rows_anonymised": 0,
        "account_deleted": False,
        "failures": [],
    }

    household = None
    try:
        household = await HOUSEHOLD.get_by_owner(user_id)
    except Exception:
        logger.warning("Erasure %s: household lookup failed", user_id, exc_info=True)
        summary["failures"].append("household_lookup")

    if household:
        members = household.get("members") or []
        for member in members:
            try:
                sessions = await FOODCHAT.get_member_sessions(member["id"])
                for session in sessions or []:
                    await FOODCHAT.delete_session(
                        session_id=session["session_id"], member_id=member["id"]
                    )
                    summary["chat_sessions_deleted"] += 1
            except Exception:
                logger.warning(
                    "Erasure %s: chat session cleanup failed for member %s",
                    user_id,
                    member.get("id"),
                    exc_info=True,
                )
                summary["failures"].append("chat_sessions")

        try:
            await HOUSEHOLD.delete(household["id"])
            summary["households_deleted"] = 1
            summary["members_deleted"] = len(members)
        except Exception:
            logger.warning("Erasure %s: household deletion failed", user_id, exc_info=True)
            summary["failures"].append("household")

    try:
        summary["analytics_rows_anonymised"] = await anonymise_analytics(user_id)
    except Exception:
        logger.warning(
            "Erasure %s: analytics anonymisation failed", user_id, exc_info=True
        )
        summary["failures"].append("analytics")

    if delete_account:
        try:
            KEYCLOAK_ADMIN_CLIENT().delete_user(user_id)
            summary["account_deleted"] = True
        except Exception:
            logger.warning("Erasure %s: account deletion failed", user_id, exc_info=True)
            summary["failures"].append("account")

    logger.info("Erasure complete for %s: %s", user_id, summary)
    return summary


async def anonymise_analytics(user_id: str) -> int:
    """Strip a user's identity from their activity rows, keeping the rows.

    Also drops the text they typed: a search someone can be identified by is
    exactly what a free-text query can be. Returns the number of rows touched.

    Tolerates the analytics schema not existing — a deployment that has not
    applied ``schemas/50_analytics.sql`` must still be able to erase an account:
    each table is its own transaction and a failure is logged, not raised.

    Not covered: events for this user still sitting in the recorder's queue
    (at most one flush interval, about two seconds) are written after these
    UPDATEs run. The consent cache is invalidated so they land without an
    identity on this replica; on other replicas the bound is the consent TTL.
    """
    from sqlalchemy import text, update

    from analytics import CONSENT
    from analytics.recorder import _CONDITIONAL_PROP_KEYS, _TEXT_PROP_KEYS
    from backend.postgres import POSTGRES_ASYNC_SESSION_FACTORY
    from sql import (
        ActivityEvent,
        AnalyticsSetting,
        ClientError,
        ClientSession,
        ExpertReview,
        FeedbackRecord,
        LLMUsage,
        SearchQuery,
        UIInteraction,
        WebVital,
    )

    # A cached "may attribute" decision would let events already queued for
    # this user be written with their id after the UPDATEs below ran.
    CONSENT.invalidate(user_id)

    # (table, column holding the subject, other columns to clear). Free text a
    # person typed goes with their identity: a comment can name them as surely
    # as a column can. reviewer_id on expert_review is kept — it is the audit
    # trail of who judged what, and is already an opaque subject — but the
    # human-readable name is not.
    plan = (
        (ActivityEvent, "user_id", {"member_id": None, "household_id": None, "roles": None}),
        (SearchQuery, "user_id", {"member_id": None, "raw_query": None}),
        (LLMUsage, "user_id", {"member_id": None}),
        (FeedbackRecord, "user_id", {"member_id": None, "comment": None}),
        (ExpertReview, "reviewer_id", {"reviewer_name": None}),
        (AnalyticsSetting, "updated_by", {}),
        # Real user monitoring. The device row loses its user agent as well as
        # its identity: a full user agent is a fingerprint that would still
        # single this person out among a few hundred sessions, which is exactly
        # what erasure has to prevent. The parsed browser and OS stay, because
        # "someone was on Safari" identifies nobody.
        (ClientSession, "user_id", {"member_id": None, "user_agent": None, "ip_prefix": None}),
        # An error keeps its stack and message — those describe the software,
        # are already redacted, and are what the group is built from — and
        # loses everything that points at a person.
        (ClientError, "user_id", {"member_id": None, "context": {}}),
        (UIInteraction, "user_id", {}),
        (WebVital, "user_id", {}),
    )

    touched = 0

    # `props` is client-supplied JSON and can hold what someone typed. The
    # consent path already strips these keys; erasure has to as well, or a
    # search term survives in the one place nobody thinks to look. Written as
    # explicit SQL because `jsonb - text[]` is a Postgres operator with no
    # portable ORM spelling, and run FIRST — after the UPDATE below there is no
    # user_id left to find these rows by. Keys are bound, not interpolated.
    try:
        async with POSTGRES_ASYNC_SESSION_FACTORY()() as db:
            await db.execute(
                text(
                    "UPDATE analytics.event SET props = props - CAST(:keys AS text[]) "
                    "WHERE user_id = :user_id AND props IS NOT NULL"
                ),
                # The conditional keys go too, unconditionally. On the
                # recording path a route pattern is kept because it names a
                # page and not a person; erasure is the stronger promise, and
                # losing one route pattern for one erased account costs
                # nothing next to evaluating each value in SQL.
                {
                    "keys": sorted(_TEXT_PROP_KEYS | _CONDITIONAL_PROP_KEYS),
                    "user_id": user_id,
                },
            )
            await db.commit()
    except Exception:
        logger.warning(
            "Erasure %s: event props not stripped", user_id, exc_info=True
        )

    for model, subject, extra in plan:
        table = model.__table__
        values = dict(extra)
        # The subject column itself is nulled except where it is the audit key.
        if model is not ExpertReview:
            values[subject] = None
        if not values:
            continue
        # One transaction per table: a failure on one (the schema not applied,
        # say) must not roll back the others, and must not stop the account
        # deletion that follows.
        try:
            async with POSTGRES_ASYNC_SESSION_FACTORY()() as db:
                result = await db.execute(
                    update(table).where(table.c[subject] == user_id).values(**values)
                )
                await db.commit()
                touched += result.rowcount or 0
        except Exception:
            logger.warning(
                "Erasure %s: analytics table %s not anonymised",
                user_id,
                table.name,
                exc_info=True,
            )
    return touched
