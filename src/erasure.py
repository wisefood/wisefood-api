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

    if delete_account:
        try:
            KEYCLOAK_ADMIN_CLIENT().delete_user(user_id)
            summary["account_deleted"] = True
        except Exception:
            logger.warning("Erasure %s: account deletion failed", user_id, exc_info=True)
            summary["failures"].append("account")

    logger.info("Erasure complete for %s: %s", user_id, summary)
    return summary
