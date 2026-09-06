"""Putting a name to an identified person.

Every per-person view in the console showed a Keycloak subject — a UUID — and
nothing else. That was defensible while the question was whether the platform
may name anyone at all, and it is the wrong answer once it may: a subject that
reaches an analytics row is there *because the person consented to being
named*, and then showing them as `f3889e88-…` is withholding the one thing
they agreed to. An expert reading a session should see who it was.

Two sources, joined here so no report has to know about either:

* **Keycloak** — the account: username, and the full name if they gave one.
  One blocking admin round-trip per user, which is why this batches, caps how
  many run at once, and remembers answers for a while. A session board of fifty
  rows must not become fifty synchronous Keycloak calls on every render.
* **The household table** — the household the account owns, by name. One SQL
  statement for the whole batch.

Never raises. Keycloak being down, or a subject that no longer exists, leaves
that person as their short id — which is what the console showed for everyone
before this — rather than failing the report they appear in.

Not a consent gate. This is only ever called with subjects already present on
analytics rows, and a subject is present there only if the consent path let it
through. What arrives here has already been decided.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, Iterable, List, Optional

logger = logging.getLogger(__name__)

#: How long a resolved name is remembered. Names change rarely; the cost of a
#: stale one is a renamed account showing its old name for a few minutes.
_TTL_SECONDS = 300.0
#: Distinct people remembered per process. Beyond this the oldest are dropped —
#: a bounded cache that forgets beats one that grows with the userbase.
_MAX_ENTRIES = 5_000
#: Keycloak admin calls in flight at once. The admin API is not built for
#: bursts, and the console is not the only thing talking to it.
_CONCURRENCY = 6

# subject -> (monotonic timestamp, resolved record)
_cache: Dict[str, tuple] = {}


def _short(user_id: str) -> str:
    """What the console showed before there were names: the id, shortened."""
    return f"{user_id[:8]}…" if len(user_id) > 12 else user_id


def _record(user_id: str, account: Optional[Dict[str, Any]], household: Optional[str]) -> Dict[str, Any]:
    full = (account or {}).get("fullname") or ""
    username = (account or {}).get("username") or ""
    # The best name available, in order: what they typed as their name, their
    # username, the short id. Never blank — a blank cell reads as a bug.
    display = full.strip() or username.strip() or _short(user_id)
    return {
        "user_id": user_id,
        "display_name": display,
        "username": username or None,
        "household_name": household,
        # So a page can tell a real name from a fallback without string-matching.
        "resolved": bool(full.strip() or username.strip()),
    }


def _fetch_account(user_id: str) -> Optional[Dict[str, Any]]:
    """One Keycloak lookup. Runs in a worker thread; see resolve_people."""
    try:
        import kutils

        return kutils.get_user(user_id)
    except Exception:
        # A subject that no longer exists, or Keycloak unreachable. Either way
        # this person keeps their short id and the report renders.
        logger.debug("analytics.people_account_lookup_failed", extra={"user_id": user_id})
        return None


async def _fetch_households(user_ids: List[str]) -> Dict[str, str]:
    """Household name by owner, one statement for the batch."""
    if not user_ids:
        return {}
    try:
        from sqlalchemy import text

        from backend.postgres import POSTGRES_ASYNC_SESSION_FACTORY

        async with POSTGRES_ASYNC_SESSION_FACTORY()() as db:
            rows = (
                await db.execute(
                    text(
                        "SELECT owner_id, name FROM wisefood.household "
                        "WHERE owner_id = ANY(CAST(:ids AS varchar[]))"
                    ),
                    {"ids": user_ids},
                )
            ).all()
        # An owner with several households: the first name is as good as any
        # for a label, and a label is all this is.
        out: Dict[str, str] = {}
        for owner_id, name in rows:
            out.setdefault(owner_id, name)
        return out
    except Exception:
        logger.debug("analytics.people_household_lookup_failed", exc_info=True)
        return {}


async def resolve_people(user_ids: Iterable[Optional[str]]) -> Dict[str, Dict[str, Any]]:
    """Names for a set of subjects. Missing or unknown ids resolve to their short id."""
    from starlette.concurrency import run_in_threadpool

    wanted = sorted({uid for uid in user_ids if uid})
    if not wanted:
        return {}

    now = time.monotonic()
    result: Dict[str, Dict[str, Any]] = {}
    unknown: List[str] = []
    for uid in wanted:
        hit = _cache.get(uid)
        if hit and (now - hit[0]) < _TTL_SECONDS:
            result[uid] = hit[1]
        else:
            unknown.append(uid)
    if not unknown:
        return result

    # Keycloak and Postgres in parallel; the Keycloak calls throttled among
    # themselves so a cold cache does not burst the admin API.
    gate = asyncio.Semaphore(_CONCURRENCY)

    async def account(uid: str):
        async with gate:
            return uid, await run_in_threadpool(_fetch_account, uid)

    accounts_task = asyncio.gather(*(account(uid) for uid in unknown), return_exceptions=True)
    households_task = _fetch_households(unknown)
    accounts_raw, households = await asyncio.gather(accounts_task, households_task)

    accounts: Dict[str, Optional[Dict[str, Any]]] = {}
    for item in accounts_raw:
        if isinstance(item, tuple):
            accounts[item[0]] = item[1]

    for uid in unknown:
        record = _record(uid, accounts.get(uid), households.get(uid))
        result[uid] = record
        _cache[uid] = (now, record)

    if len(_cache) > _MAX_ENTRIES:
        # Oldest first. A full cache is a cache of people nobody has looked at
        # lately, and forgetting them costs one lookup when somebody does.
        for stale in sorted(_cache, key=lambda k: _cache[k][0])[: len(_cache) - _MAX_ENTRIES]:
            _cache.pop(stale, None)
    return result


def _reset_cache_for_tests() -> None:
    _cache.clear()
