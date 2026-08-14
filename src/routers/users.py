"""
User account endpoints (scoped to the authenticated Keycloak USER — the
token `sub` claim — NOT a household member).

Hosts GDPR-style consent recording: after login the UI shows a consent bar
("cookies + processing of personal information solely for service
provision"); acceptance is recorded per user with timestamp and client IP.
The user id is ALWAYS derived from the verified token, never from client
input, and the endpoints are available to any authenticated user,
including guests.
"""

import logging
from typing import Optional

from fastapi import APIRouter, Depends, Query, Request

import kutils
from auth import auth
from exceptions import InternalError
from routers.generic import render
from schemas import UserConsentCreate, UserConsentRecord, UserConsentStatus
from api.v1.users import USER_CONSENT, DEFAULT_CONSENT_TYPE

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/users", tags=["User Account Operations"])


def client_ip(request: Request) -> Optional[str]:
    """
    Best-effort client IP for consent audit records.

    The API is deployed behind nginx-ingress, which appends the original
    client address to X-Forwarded-For; the FIRST entry of that header is the
    real client. When the header is absent (e.g. direct access in local
    development) fall back to the socket peer address. Returns None only if
    neither is available (e.g. some test clients).
    """
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else None


# ========== Account Erasure ==========


@router.delete(
    "/me",
    dependencies=[Depends(auth())],
    summary="Delete the authenticated account and all of its data",
    description=(
        "Irreversible. Deletes the caller's household and every member in it, "
        "their profiles, meal plans, favorites, adapted recipes and saved "
        "items (all by database cascade), their FoodChat conversations, and "
        "finally the account itself.\n\n"
        "Three things survive, and the privacy notice says so: the append-only "
        "consent ledger, which is the record that a lawful basis existed for "
        "the processing that already happened and which no longer resolves to "
        "a person once the account is gone; FoodScholar Q&A sessions, which "
        "carry their own short expiry; and Langfuse traces of model requests, "
        "which live in a separate system this flow does not reach.\n\n"
        "Household members other than the owner are deleted with the "
        "household — a household cannot outlive the account that owns it."
    ),
)
@render()
async def api_delete_my_account(request: Request):
    """
    Erase the calling user: their data first, their account last.

    Reports what actually happened rather than assuming success — a household
    that refuses to delete must not be reported to the user as erased.
    """
    from erasure import purge_user

    user_id = kutils.current_user(request)["sub"]
    summary = await purge_user(user_id)

    if not summary["account_deleted"]:
        logger.error("Account erasure incomplete for %s: %s", user_id, summary)
        raise InternalError(
            detail=(
                "We could not finish deleting your account. Nothing has been "
                "left in an unusable state — please try again, or contact us "
                "so we can complete it manually."
            )
        )

    logger.info("Account erased on user request: %s", summary)
    return {
        "erased": True,
        "households_deleted": summary["households_deleted"],
        "members_deleted": summary["members_deleted"],
        "chat_sessions_deleted": summary["chat_sessions_deleted"],
        "retained": ["consent_ledger", "model_request_traces"],
    }


# ========== User Consent Endpoints ==========


@router.get(
    "/me/consent",
    dependencies=[Depends(auth())],
    summary="Get the current user's consent status",
    description=(
        "Return the latest recorded consent for the authenticated user and "
        "the given consent type. granted=false with null version/granted_at "
        "means the user has never accepted it. Consent covers cookies and "
        "the processing of personal information solely for the provision of "
        "the service. Available to any authenticated user, including guests."
    ),
)
@render()
async def api_get_my_consent(
    request: Request,
    consent_type: str = Query(
        DEFAULT_CONSENT_TYPE,
        min_length=1,
        max_length=64,
        description="Kind of consent to check",
    ),
):
    """
    Get the authenticated user's latest consent for a consent type.

    The consent covers processing of personal information solely for the
    provision of the service. The user id is taken from the token's `sub`
    claim (Keycloak user, not household member).
    """
    user_id = kutils.current_user(request)["sub"]

    consent = await USER_CONSENT.get_latest_consent(user_id, consent_type)
    if consent is None:
        return UserConsentStatus(
            granted=False,
            consent_type=consent_type,
            version=None,
            granted_at=None,
        )

    return UserConsentStatus(
        granted=True,
        consent_type=consent["consent_type"],
        version=consent["version"],
        granted_at=consent["granted_at"],
    )


@router.post(
    "/me/consent",
    dependencies=[Depends(auth())],
    summary="Record the current user's consent acceptance",
    description=(
        "Record that the authenticated user accepted the consent bar "
        "(cookies + processing of personal information solely for service "
        "provision). Appends a new row with timestamp and client IP; the "
        "ledger is append-only for auditability. Available to any "
        "authenticated user, including guests."
    ),
)
@render()
async def api_record_my_consent(
    request: Request,
    consent_data: UserConsentCreate,
):
    """
    Record a consent acceptance for the authenticated user.

    The consent covers processing of personal information solely for the
    provision of the service. The user id is ALWAYS taken from the token's
    `sub` claim — never from the request body — and the client IP is
    captured for the audit trail.
    """
    user_id = kutils.current_user(request)["sub"]

    consent = await USER_CONSENT.record_consent(
        user_id=user_id,
        version=consent_data.version,
        consent_type=consent_data.consent_type,
        ip_address=client_ip(request),
    )

    return UserConsentRecord(**consent)
