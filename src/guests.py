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
    """Fetch the guest accounts Keycloak currently holds.

    Identification is by the ``guest`` REALM ROLE, not by the guest attribute.
    Keycloak's declarative user profile rejects unmanaged attributes by default
    from version 24 on, so ``wisefood_guest`` can be silently dropped at
    creation — and an attribute-only filter then matches nothing, which leaves
    every guest un-reaped forever. The role is assigned through its own API call
    and cannot be dropped that way.

    Both guards are kept: the role says it was minted as a guest, and the strict
    username prefix keeps a real user who somehow holds the role out of reach.
    """
    admin = KEYCLOAK_ADMIN_CLIENT()
    users: List[Dict[str, Any]] = []

    try:
        users = admin.get_realm_role_members(
            GUEST_ROLE, query={"max": max_results}
        ) or []
    except Exception:
        logger.warning(
            "Guest lookup by realm role failed — falling back to username search",
            exc_info=True,
        )

    if not users:
        # briefRepresentation=False asks for attributes explicitly rather than
        # relying on the server default, which has changed across versions.
        users = admin.get_users(
            query={
                "username": GUEST_USERNAME_PREFIX,
                "max": max_results,
                "briefRepresentation": False,
            }
        ) or []

    # Keycloak username search is infix — keep strict prefix matches only.
    # With registrationEmailAsUsername the stored username is the synthetic
    # guest email, which carries the same prefix.
    return [
        u for u in users if u.get("username", "").startswith(GUEST_USERNAME_PREFIX)
    ]


def _guest_expires_at(user: Dict[str, Any]) -> int | None:
    """When this guest is due for deletion, or None if it cannot be determined.

    Prefers the expiry attribute written at creation. When that is missing —
    the realm dropped it, or the guest predates it — falls back to Keycloak's
    own ``createdTimestamp`` plus the configured TTL, which is present in every
    user representation.

    Returning None means "do not touch": the previous code defaulted an
    unreadable expiry to 0, which reads as "expired since 1970" and would have
    deleted every guest it could not parse.
    """
    raw = (user.get("attributes") or {}).get(GUEST_EXPIRES_ATTRIBUTE) or []
    if raw:
        try:
            return int(raw[0])
        except (ValueError, TypeError):
            logger.warning(
                "Guest %s: unparseable expiry %r", user.get("username"), raw[0]
            )

    created_ms = user.get("createdTimestamp")
    if created_ms:
        try:
            return int(created_ms) // 1000 + config.settings["GUEST_TTL_SECONDS"]
        except (ValueError, TypeError):
            pass

    return None


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

    # Keycloak 24+ drops attributes the realm's user profile does not declare,
    # without erroring. Say so once per creation instead of discovering it as
    # guests that never expire.
    try:
        stored = (admin.get_user(user_id).get("attributes") or {})
        if stored.get(GUEST_EXPIRES_ATTRIBUTE) != [str(expires_at)]:
            logger.warning(
                "Guest %s: realm did not store %s (got %r). Expiry will fall "
                "back to createdTimestamp + TTL; allow unmanaged attributes in "
                "the realm user profile to restore exact expiries.",
                username, GUEST_EXPIRES_ATTRIBUTE,
                stored.get(GUEST_EXPIRES_ATTRIBUTE),
            )
    except Exception:
        logger.debug("Guest %s: attribute read-back failed", username, exc_info=True)

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
    """Tear down a guest: chat sessions, household (cascades), Keycloak user.

    Shares the teardown with self-service account erasure (``erasure.purge_user``)
    so the two can never drift — a guest and a registered account must be erased
    the same way. Still raises when the Keycloak user survives, because
    ``reap_expired_guests`` counts successes and a guest that outlives its expiry
    has to keep being retried.
    """
    from erasure import purge_user

    summary = await purge_user(user_id)
    if not summary["account_deleted"]:
        raise RuntimeError(f"Guest {user_id} could not be deleted from Keycloak")
    logger.info("Guest %s deleted", user_id)


async def reap_expired_guests() -> int:
    """Delete every guest whose lifetime is up.

    Logs what it saw, not only what it deleted: a reaper that quietly finds zero
    guests looks identical to a reaper with nothing to do, which is how this went
    unnoticed. Counts are logged whenever any guest exists at all.
    """
    now = int(time.time())
    reaped = 0
    guests = _guest_users(max_results=1000)
    undetermined = 0

    for user in guests:
        expires_at = _guest_expires_at(user)
        if expires_at is None:
            undetermined += 1
            continue
        if expires_at > now:
            continue
        try:
            await delete_guest(user["id"])
            reaped += 1
        except Exception:
            logger.warning(
                "Failed to reap guest %s", user.get("username"), exc_info=True
            )

    if guests:
        logger.info(
            "Guest reaper: %d guest(s) found, %d expired and removed, "
            "%d with no determinable expiry (left alone)",
            len(guests), reaped, undetermined,
        )
    if undetermined:
        logger.warning(
            "%d guest(s) carry neither an expiry attribute nor a creation "
            "timestamp — they will never be reaped. Check whether the realm's "
            "user profile permits unmanaged attributes.",
            undetermined,
        )
    return reaped
