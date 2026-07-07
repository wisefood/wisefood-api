"""
User-scoped entity operations (Keycloak users, keyed by the token `sub`
claim — NOT household members).

Currently hosts the GDPR-style user consent ledger. The recorded consent
covers cookies and the processing of personal information solely for the
provision of the service (purpose limitation), and is stored append-only so
the trail stays auditable.
"""
from typing import Any, Dict, Optional

from sqlalchemy import select

from sql import UserConsent
from backend.postgres import POSTGRES_ASYNC_SESSION_FACTORY

#: Default consent kind recorded by the post-login consent bar.
DEFAULT_CONSENT_TYPE = "service_data_processing"


class UserConsentEntity:
    """
    Append-only ledger of user consent acceptances.

    Every acceptance inserts a new row in wisefood.user_consent; rows are
    never updated or deleted. The latest row per (user_id, consent_type) is
    the currently effective consent. Consent covers processing of personal
    information solely for the provision of the service.
    """

    async def get_latest_consent(
        self,
        user_id: str,
        consent_type: str = DEFAULT_CONSENT_TYPE,
    ) -> Optional[Dict[str, Any]]:
        """
        Fetch the latest consent row for a user and consent type.

        :param user_id: Keycloak user id (token `sub` claim)
        :param consent_type: Kind of consent
        :return: Consent dictionary, or None if the user never consented
        """
        async with POSTGRES_ASYNC_SESSION_FACTORY()() as db:
            result = await db.execute(
                select(UserConsent)
                .where(
                    UserConsent.user_id == user_id,
                    UserConsent.consent_type == consent_type,
                )
                .order_by(UserConsent.granted_at.desc(), UserConsent.id.desc())
                .limit(1)
            )
            consent = result.scalar_one_or_none()
            return consent.to_dict() if consent else None

    async def record_consent(
        self,
        user_id: str,
        version: str,
        consent_type: str = DEFAULT_CONSENT_TYPE,
        ip_address: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Record a consent acceptance (appends a new ledger row).

        The acceptance documents that the user consented to cookies and to
        the processing of personal information solely for the provision of
        the service.

        :param user_id: Keycloak user id (token `sub` claim) — always taken
            from the verified token, never from client input
        :param version: Version of the consent text the user accepted
        :param consent_type: Kind of consent
        :param ip_address: Client IP at acceptance time (audit trail)
        :return: The stored consent dictionary
        """
        async with POSTGRES_ASYNC_SESSION_FACTORY()() as db:
            consent = UserConsent(
                user_id=user_id,
                consent_type=consent_type,
                version=version,
                ip_address=ip_address,
            )
            db.add(consent)
            await db.flush()
            consent_dict = consent.to_dict()
            await db.commit()
            return consent_dict


# Singleton instance
USER_CONSENT = UserConsentEntity()
