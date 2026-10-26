"""What each person has said about reminder emails.

Two ways to say no, and both are honoured at once:

* the switch on the profile page (``/users/me/notification-preferences``);
* the link in every reminder, and the one-click unsubscribe a mail client
  offers from the ``List-Unsubscribe`` header. Neither needs a login — the
  random token in the link is the credential, so it works from a phone that
  has never signed in.

No row means "never asked", which is "yes": reminders are part of the service
someone signed up for, and every one of them carries the way out.
"""
from __future__ import annotations

import logging
import secrets
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Set

logger = logging.getLogger(__name__)

VIA = ("profile", "email_link", "list_header", "admin")


def _new_token() -> str:
    # 256 bits; URL-safe, so it travels in a query string untouched.
    return secrets.token_urlsafe(32)


async def get(user_id: str) -> Dict[str, Any]:
    from sqlalchemy import select

    from backend.postgres import POSTGRES_ASYNC_SESSION_FACTORY
    from sql import NotificationPreference

    async with POSTGRES_ASYNC_SESSION_FACTORY()() as db:
        row = (
            await db.execute(
                select(NotificationPreference).where(NotificationPreference.user_id == user_id)
            )
        ).scalar_one_or_none()
    if row is None:
        return {"inactivity_reminders": True, "updated_at": None, "updated_via": None}
    return {
        "inactivity_reminders": bool(row.inactivity_reminders),
        "updated_at": row.updated_at,
        "updated_via": row.updated_via,
    }


async def set_inactivity_reminders(user_id: str, enabled: bool, *, via: str) -> Dict[str, Any]:
    from sqlalchemy.dialects.postgresql import insert

    from backend.postgres import POSTGRES_ASYNC_SESSION_FACTORY
    from sql import NotificationPreference

    if via not in VIA:
        raise ValueError(f"unknown preference source {via!r}")
    now = datetime.now(timezone.utc)
    table = NotificationPreference.__table__
    stmt = insert(table).values(
        user_id=user_id, inactivity_reminders=enabled, updated_at=now, updated_via=via
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=[table.c.user_id],
        set_={"inactivity_reminders": enabled, "updated_at": now, "updated_via": via},
    )
    async with POSTGRES_ASYNC_SESSION_FACTORY()() as db:
        await db.execute(stmt)
        await db.commit()
    logger.info("notifications.preference_changed", extra={"user": user_id, "enabled": enabled, "via": via})
    return {"inactivity_reminders": enabled, "updated_at": now, "updated_via": via}


async def ensure_token(user_id: str) -> str:
    """The token for this person's unsubscribe links, minted on first use.

    Creating the row must not change the preference itself, so a new row is
    inserted with reminders on (what "no row" already meant) and an existing
    row only gains a token if it lacks one.
    """
    from sqlalchemy import select, update
    from sqlalchemy.dialects.postgresql import insert

    from backend.postgres import POSTGRES_ASYNC_SESSION_FACTORY
    from sql import NotificationPreference

    table = NotificationPreference.__table__
    async with POSTGRES_ASYNC_SESSION_FACTORY()() as db:
        await db.execute(
            insert(table)
            .values(user_id=user_id, inactivity_reminders=True, unsubscribe_token=_new_token())
            .on_conflict_do_nothing(index_elements=[table.c.user_id])
        )
        await db.execute(
            update(table)
            .where(table.c.user_id == user_id, table.c.unsubscribe_token.is_(None))
            .values(unsubscribe_token=_new_token())
        )
        token = (
            await db.execute(select(table.c.unsubscribe_token).where(table.c.user_id == user_id))
        ).scalar_one()
        await db.commit()
    return token


async def unsubscribe(token: str, *, via: str) -> Optional[str]:
    """Turn reminders off for whoever owns ``token``. The user id, or None.

    Idempotent: a second click, or a mail client's one-click POST followed by
    the person clicking the link too, is the same answer.
    """
    from sqlalchemy import update

    from backend.postgres import POSTGRES_ASYNC_SESSION_FACTORY
    from sql import NotificationPreference

    token = (token or "").strip()
    if not token or len(token) > 64:
        return None
    table = NotificationPreference.__table__
    async with POSTGRES_ASYNC_SESSION_FACTORY()() as db:
        user_id = (
            await db.execute(
                update(table)
                .where(table.c.unsubscribe_token == token)
                .values(
                    inactivity_reminders=False,
                    updated_at=datetime.now(timezone.utc),
                    updated_via=via,
                )
                .returning(table.c.user_id)
            )
        ).scalar_one_or_none()
        await db.commit()
    if user_id:
        logger.info("notifications.unsubscribed", extra={"user": user_id, "via": via})
    return user_id


async def opted_out() -> Set[str]:
    """Everyone who has turned reminders off."""
    from sqlalchemy import select

    from backend.postgres import POSTGRES_ASYNC_SESSION_FACTORY
    from sql import NotificationPreference

    async with POSTGRES_ASYNC_SESSION_FACTORY()() as db:
        rows = (
            await db.execute(
                select(NotificationPreference.user_id).where(
                    NotificationPreference.inactivity_reminders.is_(False)
                )
            )
        ).scalars().all()
    return set(rows)


async def erase(user_id: str) -> int:
    """Remove every notification record about this person. Rows deleted."""
    from sqlalchemy import delete

    from backend.postgres import POSTGRES_ASYNC_SESSION_FACTORY
    from sql import NotificationLog, NotificationPreference, UserActivity
    from notifications import activity

    activity.forget(user_id)
    removed = 0
    async with POSTGRES_ASYNC_SESSION_FACTORY()() as db:
        for model in (NotificationPreference, UserActivity, NotificationLog):
            result = await db.execute(delete(model).where(model.user_id == user_id))
            removed += int(result.rowcount or 0)
        await db.commit()
    return removed
