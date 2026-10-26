"""When each account last used the platform.

Recorded by the request middleware on authenticated requests, which is the
only place that sees every visit to every app — FoodChat, FoodScholar and
RecipeWrangler all come through this gateway. Analytics would be the obvious
other source, but it attributes a person only with their consent, and "has
this person been away for two weeks" has to be answerable for everyone the
reminder may write to.

Three properties matter more than precision:

**Never in the way.** A visit costs one dictionary lookup on the hot path. At
most one write per account per ``ACTIVITY_TOUCH_SECONDS`` per worker, done on
a background task the request does not wait for, and a failed write is
dropped. "Last seen" is wrong by at most that interval, which for a reminder
measured in days is nothing.

**Guests and agents are not recorded.** A guest is gone within a day and an
agent is a service; neither is ever reminded.

**Monotonic.** Concurrent workers write in any order, so the stored value only
ever moves forward (``GREATEST``).
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Dict, Iterable, Optional, Set

logger = logging.getLogger(__name__)

#: account id -> monotonic time of the last write this process scheduled.
_last_write: Dict[str, float] = {}
_MAX_TRACKED = 20_000
#: Keeps a reference to every in-flight write; a task with none can be
#: garbage-collected before it runs.
_inflight: Set[asyncio.Task] = set()
_warned_missing_table = False

_SKIP_ROLES = ("guest", "agent")


def _interval() -> float:
    from main import config

    return float(config.settings.get("ACTIVITY_TOUCH_SECONDS", 600))


def touch(user_id: Optional[str], *, roles: Optional[Iterable[str]] = None,
          locale: Optional[str] = None) -> None:
    """Note that this account is using the platform. Returns immediately."""
    if not user_id:
        return
    if roles and any(role in _SKIP_ROLES for role in roles):
        return

    now = time.monotonic()
    previous = _last_write.get(user_id)
    if previous is not None and now - previous < _interval():
        return
    if len(_last_write) >= _MAX_TRACKED:
        # Forgetting the oldest only means one extra write for that account.
        for stale in sorted(_last_write, key=_last_write.get)[: _MAX_TRACKED // 10]:
            _last_write.pop(stale, None)
    _last_write[user_id] = now

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    # "hu-HU" -> "hu": the reminder is written per language, not per region.
    language = (locale or "").split("-")[0].strip().lower()[:8] or None
    task = loop.create_task(_record(user_id, language, datetime.now(timezone.utc)))
    _inflight.add(task)
    task.add_done_callback(_inflight.discard)


async def _record(user_id: str, locale: Optional[str], seen_at: datetime) -> None:
    global _warned_missing_table
    try:
        from sqlalchemy import func
        from sqlalchemy.dialects.postgresql import insert

        from backend.postgres import POSTGRES_ASYNC_SESSION_FACTORY
        from sql import UserActivity

        table = UserActivity.__table__
        stmt = insert(table).values(
            user_id=user_id,
            last_seen_at=seen_at,
            first_seen_at=seen_at,
            locale=(locale or None),
            estimated=False,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[table.c.user_id],
            set_={
                "last_seen_at": func.greatest(table.c.last_seen_at, stmt.excluded.last_seen_at),
                "locale": func.coalesce(stmt.excluded.locale, table.c.locale),
                "estimated": False,
            },
        )
        async with POSTGRES_ASYNC_SESSION_FACTORY()() as db:
            await db.execute(stmt)
            await db.commit()
    except Exception as exc:
        # The schema may not be applied yet. Say so once, then stay quiet:
        # this runs on every tenth minute of every active account.
        level = logging.DEBUG if _warned_missing_table else logging.WARNING
        _warned_missing_table = True
        logger.log(level, "notifications.activity_write_failed: %s", exc)
        # Let the next visit try again rather than waiting out the interval.
        _last_write.pop(user_id, None)


async def drain() -> None:
    """Wait for in-flight writes. For tests and shutdown."""
    if _inflight:
        await asyncio.gather(*list(_inflight), return_exceptions=True)


def forget(user_id: str) -> None:
    """Drop the throttle entry, so an erased account leaves no trace here."""
    _last_write.pop(user_id, None)
