"""One consent for everything the platform does with a person's data.

The consent bar used to record one acceptance and then, quietly, a second one
— analytics attribution — so a person agreed to two things in one click and
the ledger showed two unrelated rows. Using conversations to test the product
would have been a third. Asked three times, people stop reading; asked once
for three things they cannot later separate, the consent is not free.

So it is one question and three purposes:

* **service** — running WiseFood at all. The terms row, `service_data_processing`.
* **analytics** — naming the person on their own activity records.
* **research** — using their conversations, with identifying details removed,
  to test and evaluate WiseFood's answers. Never to train models.

Accepting the current terms records all three at once, with one version and one
timestamp. Each of the last two can then be withdrawn, and given again, on its
own from the profile — and the product works the same without them, which is
what makes bundling them into one question defensible.

The ledger stays append-only: a withdrawal is its own row kind, and the later
of a grant and a withdrawal wins. Silence means no for research whatever the
analytics mode says: nobody's conversations are used because they never
answered.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Tuple

#: The terms row. Kept as the long-standing consent kind so every existing
#: acceptance still reads as one.
TERMS = "service_data_processing"

#: The terms text that covers every purpose below. Anything older covers the
#: service and analytics only, and the person is asked again.
TERMS_VERSION = "2.0"

#: purpose -> (grant kind, withdrawal kind) in wisefood.user_consent.
PURPOSES: Dict[str, Tuple[str, str]] = {
    "analytics": ("analytics", "analytics_opt_out"),
    "research": ("research", "research_opt_out"),
}


def _version_key(version: Optional[str]) -> Tuple[int, ...]:
    try:
        return tuple(int(part) for part in str(version or "").split("."))
    except ValueError:
        return (0,)


def terms_current(version: Optional[str]) -> bool:
    """Whether an accepted terms version covers everything asked today."""
    return _version_key(version) >= _version_key(TERMS_VERSION)


def resolve(
    granted_at: Optional[datetime],
    withdrawn_at: Optional[datetime],
    *,
    silence: bool = False,
) -> Tuple[bool, Optional[datetime]]:
    """A purpose's state from its latest grant and withdrawal.

    The later of the two wins; a tie is a withdrawal, because recording an
    answer as yes when it may have been no is the error that cannot be undone.
    ``silence`` is what never having answered means.
    """
    if withdrawn_at and (not granted_at or withdrawn_at >= granted_at):
        return False, withdrawn_at
    if granted_at:
        return True, granted_at
    return silence, None


async def latest_rows(user_ids: Iterable[str], kinds: Iterable[str]) -> Dict[tuple, Dict[str, Any]]:
    """The newest ledger row per (user, kind), for many users in one query."""
    from sqlalchemy import select

    from backend.postgres import POSTGRES_ASYNC_SESSION_FACTORY
    from sql import UserConsent

    wanted = [uid for uid in set(user_ids) if uid]
    if not wanted:
        return {}
    async with POSTGRES_ASYNC_SESSION_FACTORY()() as db:
        result = await db.execute(
            select(
                UserConsent.user_id,
                UserConsent.consent_type,
                UserConsent.version,
                UserConsent.granted_at,
            )
            .where(
                UserConsent.user_id.in_(wanted),
                UserConsent.consent_type.in_(list(kinds)),
            )
            .order_by(UserConsent.granted_at.desc(), UserConsent.id.desc())
        )
        latest: Dict[tuple, Dict[str, Any]] = {}
        for user_id, kind, version, granted_at in result.all():
            latest.setdefault((user_id, kind), {"version": version, "granted_at": granted_at})
        return latest


def _all_kinds() -> List[str]:
    kinds = [TERMS]
    for grant, withdraw in PURPOSES.values():
        kinds += [grant, withdraw]
    return kinds


async def status(user_id: str, *, analytics_silence: bool) -> Dict[str, Any]:
    """Everything one person has agreed to, in one answer."""
    rows = await latest_rows([user_id], _all_kinds())
    terms = rows.get((user_id, TERMS))
    purposes: Dict[str, Any] = {}
    for purpose, (grant, withdraw) in PURPOSES.items():
        granted = rows.get((user_id, grant))
        withdrawn = rows.get((user_id, withdraw))
        enabled, decided_at = resolve(
            granted["granted_at"] if granted else None,
            withdrawn["granted_at"] if withdrawn else None,
            silence=analytics_silence if purpose == "analytics" else False,
        )
        purposes[purpose] = {
            "enabled": enabled,
            "decided": bool(granted or withdrawn),
            "decided_at": decided_at,
        }
    return {
        "current_version": TERMS_VERSION,
        "terms": {
            "granted": bool(terms),
            "version": terms["version"] if terms else None,
            "granted_at": terms["granted_at"] if terms else None,
            "current": bool(terms) and terms_current(terms["version"]),
        },
        "purposes": purposes,
    }


async def accept_all(
    user_id: str,
    version: str,
    *,
    ip_address: Optional[str],
    purposes: Iterable[str],
) -> None:
    """Record one acceptance of the terms and every purpose it names.

    One transaction, so the ledger never shows a person who accepted the terms
    and only some of what the terms said.
    """
    from backend.postgres import POSTGRES_ASYNC_SESSION_FACTORY
    from sql import UserConsent

    kinds = [TERMS] + [PURPOSES[p][0] for p in purposes if p in PURPOSES]
    async with POSTGRES_ASYNC_SESSION_FACTORY()() as db:
        for kind in kinds:
            db.add(
                UserConsent(
                    user_id=user_id,
                    consent_type=kind,
                    version=version,
                    ip_address=ip_address,
                )
            )
        await db.commit()


async def research_eligible_members() -> List[Dict[str, Any]]:
    """Household members whose conversations may be used for research.

    A member is a profile, not a person with an account: the account holder
    manages every profile in the household and answers for it, so their
    research consent covers the household. Guests are never eligible —
    their data is promised to expire, and nothing of theirs may outlive it.
    """
    from sqlalchemy import select

    from backend.postgres import POSTGRES_ASYNC_SESSION_FACTORY
    from sql import Household, HouseholdMember, UserConsent

    grant, withdraw = PURPOSES["research"]
    async with POSTGRES_ASYNC_SESSION_FACTORY()() as db:
        owners = [
            row[0]
            for row in (
                await db.execute(
                    select(UserConsent.user_id)
                    .where(UserConsent.consent_type == grant)
                    .distinct()
                )
            ).all()
        ]
    if not owners:
        return []

    rows = await latest_rows(owners, [grant, withdraw])
    consenting: Dict[str, datetime] = {}
    for owner in owners:
        granted = rows.get((owner, grant))
        withdrawn = rows.get((owner, withdraw))
        enabled, decided_at = resolve(
            granted["granted_at"] if granted else None,
            withdrawn["granted_at"] if withdrawn else None,
        )
        if enabled and decided_at is not None:
            consenting[owner] = decided_at
    if not consenting:
        return []

    # The endpoints never record research consent for a guest; this is the
    # second lock, for a row written some other way. One Keycloak call per
    # consenting account, which is fine for an export run by hand.
    import asyncio

    from guests import is_guest

    for owner in list(consenting):
        if await asyncio.to_thread(is_guest, owner):
            consenting.pop(owner)
    if not consenting:
        return []

    async with POSTGRES_ASYNC_SESSION_FACTORY()() as db:
        members = (
            await db.execute(
                select(HouseholdMember.id, Household.id, Household.owner_id)
                .join(Household, Household.id == HouseholdMember.household_id)
                .where(Household.owner_id.in_(list(consenting)))
            )
        ).all()

    return [
        {
            "member_id": member_id,
            "household_id": household_id,
            "consented_at": consenting[owner_id],
        }
        for member_id, household_id, owner_id in members
    ]
