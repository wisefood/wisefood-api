"""Ephemeral guest account lifecycle.

Guests are real Keycloak users so every existing ownership check
(``owner_id == sub``) isolates them from each other with no special-casing:
two guests can never collide because each gets a unique ``sub``.

Conventions (Keycloak is the single source of truth — no side registry):
- username:   ``guest-<hex>`` (GUEST_USERNAME_PREFIX)
- realm role: ``guest`` (created by keycloak-init)
- attributes: ``wisefood_guest=true``, ``wisefood_guest_expires_at=<unix ts>``

Each guest is provisioned with a household + one adult member so the UI's
profile gate passes immediately. Teardown deletes external chat sessions,
the household (cascades to members/plans), then the Keycloak user — in that
order, because the household FK to the Keycloak user is ON DELETE SET NULL
and would otherwise leave an orphaned household behind.
"""
import logging
import secrets
import time
import uuid
from typing import Any, Dict, List

import kutils
from backend.keycloak import KEYCLOAK_ADMIN_CLIENT
from exceptions import AuthenticationError, AuthorizationError, ConflictError
from main import config

logger = logging.getLogger(__name__)

GUEST_USERNAME_PREFIX = "guest-"
GUEST_ROLE = "guest"
GUEST_ATTRIBUTE = "wisefood_guest"
GUEST_EXPIRES_ATTRIBUTE = "wisefood_guest_expires_at"


def _generate_password() -> str:
    # Suffix guarantees the realm password policy
    # (length, special char, upper case, digit) is always satisfied.
    return secrets.token_urlsafe(24) + "!A1a"


def _guest_users(max_results: int) -> List[Dict[str, Any]]:
    """Fetch Keycloak users matching the guest username prefix."""
    users = KEYCLOAK_ADMIN_CLIENT().get_users(
        query={"username": GUEST_USERNAME_PREFIX, "max": max_results}
    )
    # Keycloak username search is infix — keep strict prefix matches only,
    # and only accounts stamped with the guest attribute (defense in depth
    # against a real user registering a 'guest-…' lookalike name).
    return [
        u
        for u in users
        if u.get("username", "").startswith(GUEST_USERNAME_PREFIX)
        and (u.get("attributes") or {}).get(GUEST_ATTRIBUTE) == ["true"]
    ]


async def create_guest() -> Dict[str, Any]:
    """Create an ephemeral guest: Keycloak user + household/member + token."""
    if not config.settings["GUEST_ENABLED"]:
        raise AuthorizationError(detail="Guest access is disabled")

    max_active = config.settings["GUEST_MAX_ACTIVE"]
    if len(_guest_users(max_results=max_active + 1)) >= max_active:
        raise ConflictError(
            detail="Guest capacity reached — please try again later"
        )

    ttl = config.settings["GUEST_TTL_SECONDS"]
    expires_at = int(time.time()) + ttl
    username = GUEST_USERNAME_PREFIX + uuid.uuid4().hex[:12]
    # The realm requires an email on every user (registrationEmailAsUsername),
    # so guests get a synthetic, never-delivered address. emailVerified=True
    # prevents any verification mail from being triggered.
    email = f"{username}@{config.settings['GUEST_EMAIL_DOMAIN']}"
    password = _generate_password()

    admin = KEYCLOAK_ADMIN_CLIENT()
    user_id = admin.create_user(
        {
            "username": username,
            "email": email,
            "firstName": "Guest",
            "lastName": "User",
            "enabled": True,
            "emailVerified": True,
            "attributes": {
                GUEST_ATTRIBUTE: ["true"],
                GUEST_EXPIRES_ATTRIBUTE: [str(expires_at)],
            },
            "credentials": [
                {"type": "password", "value": password, "temporary": False}
            ],
        }
    )

    try:
        admin.assign_realm_roles(user_id, [admin.get_realm_role(GUEST_ROLE)])

        from api.v1.households import HOUSEHOLD

        household = await HOUSEHOLD.create(
            spec={
                "name": "Guest Household",
                "metadata": {"guest": True},
                "members": [{"name": "Guest", "age_group": "adult"}],
            },
            creator={"sub": user_id},
        )

        # With registrationEmailAsUsername the realm may have stored the
        # email as the username, so fall back to it for the token grant.
        try:
            token = kutils.get_token(username, password)
        except AuthenticationError:
            token = kutils.get_token(email, password)
    except Exception:
        # Never leave a half-provisioned guest behind.
        logger.exception("Guest provisioning failed — rolling back %s", username)
        await delete_guest(user_id)
        raise

    members = household.get("members") or []
    return {
        "token": token,
        "guest": {
            "user_id": user_id,
            "username": username,
            "expires_at": expires_at,
            "ttl_seconds": ttl,
            "household_id": household["id"],
            "member_id": members[0]["id"] if members else None,
        },
    }


async def delete_guest(user_id: str) -> None:
    """Tear down a guest: chat sessions, household (cascades), Keycloak user."""
    from api.v1.households import HOUSEHOLD
    from backend.foodchat import FOODCHAT

    household = None
    try:
        household = await HOUSEHOLD.get_by_owner(user_id)
    except Exception:
        logger.warning("Guest %s: household lookup failed", user_id, exc_info=True)

    if household:
        # FoodChat sessions live outside our DB and are not covered by the
        # cascade. FoodScholar sessions expire via their own Redis TTL.
        for member in household.get("members") or []:
            try:
                sessions = await FOODCHAT.get_member_sessions(member["id"])
                for session in sessions or []:
                    await FOODCHAT.delete_session(
                        session_id=session["session_id"], member_id=member["id"]
                    )
            except Exception:
                logger.warning(
                    "Guest %s: FoodChat session cleanup failed for member %s",
                    user_id,
                    member.get("id"),
                    exc_info=True,
                )
        try:
            await HOUSEHOLD.delete(household["id"])
        except Exception:
            logger.warning(
                "Guest %s: household deletion failed", user_id, exc_info=True
            )

    KEYCLOAK_ADMIN_CLIENT().delete_user(user_id)
    logger.info("Guest %s deleted", user_id)


async def reap_expired_guests() -> int:
    """Delete all guests whose expiry attribute is in the past."""
    now = int(time.time())
    reaped = 0
    for user in _guest_users(max_results=1000):
        raw = (user.get("attributes") or {}).get(GUEST_EXPIRES_ATTRIBUTE, ["0"])
        try:
            expires_at = int(raw[0])
        except (ValueError, IndexError, TypeError):
            expires_at = 0
        if expires_at <= now:
            try:
                await delete_guest(user["id"])
                reaped += 1
            except Exception:
                logger.warning(
                    "Failed to reap guest %s", user.get("username"), exc_info=True
                )
    return reaped
