"""Share links: one artifact, one unguessable token, no account to read it.

Three decisions shape everything here.

**The payload is a snapshot.** It is written once and never read back from the
plan it came from. A link somebody sent to their mother must not change when
they edit the plan, must not break when they delete it, and must not quietly
start showing a different week. It also means a guest's share outlives the
reaper that deletes the guest — which is the point rather than a side effect:
the link keeps working, and keeping the account is how you get to edit it
again.

**The token is the authorisation.** There is no owner check on read, because
there is no reader to check. That puts the whole weight on the token being
unguessable (256 bits) and on it never leaking: see `share_headers`.

**The snapshot is scrubbed to an allowlist.** A meal plan is health-adjacent
personal data, and the parts that make it *personal* are exactly the parts the
planner is proudest of — `match_reasons` says why a recipe suits this person,
and a constraint row is documented as naming "the member it protects... so one
member's goal or allergy can be seen for what it is". A share carries the
food, never the people. An allowlist rather than a blocklist because the
planner keeps learning to say more, and a blocklist only knows about the
fields that existed when it was written.
"""
from __future__ import annotations

import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

#: What a share may say about one dish. Everything here describes the food.
MEAL_FIELDS = (
    "recipe_id",
    "title",
    "ingredients",
    "directions",
    "role",
    "nutrition",
    "image_url",
)

#: Where the meals live on a plan, in the order anyone eats them.
MEAL_SLOTS = ("breakfast", "lunch", "dinner")

#: Kinds a token can point at. A closed set so a typo cannot mint a share of
#: something nobody wrote a scrubber for.
KINDS = ("meal_plan", "saved_meal_plan", "weekly_meal_plan")

#: Longest a share may be set to live. Not a limit on usefulness — a link with
#: no expiry is still allowed — but a cap on what a single call can ask for.
MAX_TTL_DAYS = 365


def new_token() -> str:
    """256 bits, URL-safe. This is the only credential a reader will hold."""
    return secrets.token_urlsafe(32)


def _clean_meal(meal: Any) -> Optional[Dict[str, Any]]:
    """One dish, reduced to what describes the dish."""
    if not isinstance(meal, dict):
        return None
    kept = {k: meal[k] for k in MEAL_FIELDS if k in meal and meal[k] is not None}
    return kept or None


def _clean_slot(value: Any) -> Any:
    """A slot holds one dish, or several (main, side, dessert)."""
    if isinstance(value, list):
        return [m for m in (_clean_meal(item) for item in value) if m]
    return _clean_meal(value)


def scrub_meal_plan(plan: Dict[str, Any]) -> Dict[str, Any]:
    """The shareable half of a meal plan.

    Keeps the date and the food. Drops, deliberately:

    * `reasoning` — the planner's prose about why this plan suits this
      household, which is where ages, goals and allergies surface in sentences
    * `match_reasons` on each dish — the same thing per recipe
    * `constraints_applied` — documented as naming the member a constraint
      protects
    * every identifier — household, member, plan and source ids. They say
      nothing to a reader and everything to someone probing the API.
    """
    if not isinstance(plan, dict):
        return {"meals": {}}

    meals: Dict[str, Any] = {}
    for slot in MEAL_SLOTS:
        cleaned = _clean_slot(plan.get(slot))
        if cleaned:
            meals[slot] = cleaned

    out: Dict[str, Any] = {"meals": meals}
    # The date is about the food, not the person, and a plan without one reads
    # as undated rather than as broken.
    for key in ("date", "source_applied_on"):
        if plan.get(key):
            out["date"] = str(plan[key])
            break
    return out


#: Weekly entry fields that describe the slot rather than the household.
WEEKLY_ENTRY_FIELDS = ("day", "meal_type", "meal_idx")


def scrub_weekly_meal_plan(plan: Dict[str, Any]) -> Dict[str, Any]:
    """The shareable half of a weekly plan.

    Same rule as the daily scrubber and a different shape: a week is a flat
    list of entries keyed by day and slot, not three named fields. Grouped
    here into days so a reader gets a week rather than a list to sort.

    Dropped for the same reasons as the daily one, and one more that matters
    on this shape: a weekly plan's `constraints_applied` rows are *measured*
    and carry statuses like "relaxed" and "violated" against named
    constraints — which is a per-household account of whose needs the planner
    could not meet. That is the last thing to publish under a link anyone can
    open.
    """
    if not isinstance(plan, dict):
        return {"days": []}

    by_day: Dict[int, Dict[str, Any]] = {}
    for entry in plan.get("entries") or []:
        if not isinstance(entry, dict):
            continue
        try:
            day = int(entry.get("day"))
        except (TypeError, ValueError):
            continue
        slot = str(entry.get("meal_type") or "").strip()
        dish = _clean_meal(entry.get("recipe"))
        if not slot or not dish:
            continue
        bucket = by_day.setdefault(day, {"day": day, "meals": {}})
        bucket["meals"].setdefault(slot, []).append(dish)

    summaries = plan.get("day_summaries")
    if isinstance(summaries, dict):
        for key, headline in summaries.items():
            try:
                day = int(key)
            except (TypeError, ValueError):
                continue
            if day in by_day and isinstance(headline, str):
                # A headline describes the food ("dinner with fish"), which is
                # why it survives where `reasoning` does not.
                by_day[day]["summary"] = headline[:200]

    out: Dict[str, Any] = {"days": [by_day[d] for d in sorted(by_day)]}
    if plan.get("created_at"):
        out["date"] = str(plan["created_at"])
    return out


def scrub(kind: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """Scrub by kind. Refuses a kind nobody has written a scrubber for."""
    if kind in ("meal_plan", "saved_meal_plan"):
        return scrub_meal_plan(payload)
    if kind == "weekly_meal_plan":
        return scrub_weekly_meal_plan(payload)
    raise ValueError(f"No scrubber for share kind {kind!r}")


def expiry_from_days(days: Optional[int]) -> Optional[datetime]:
    """A deadline from a number of days, or None for "until revoked"."""
    if days is None:
        return None
    days = max(1, min(int(days), MAX_TTL_DAYS))
    return datetime.now(timezone.utc) + timedelta(days=days)


def is_live(share, *, now: Optional[datetime] = None) -> bool:
    """Whether a share should still be served.

    Revoked and expired are both simply "not live". The endpoint answers 404
    for either, and for a token that never existed — telling them apart out
    loud would turn the endpoint into an oracle for guessing tokens.
    """
    if share is None:
        return False
    if getattr(share, "revoked_at", None) is not None:
        return False
    expires_at = getattr(share, "expires_at", None)
    if expires_at is None:
        return True
    now = now or datetime.now(timezone.utc)
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    return expires_at > now


def share_headers() -> Dict[str, str]:
    """Headers every share response must carry.

    `noindex` keeps the page out of search results, which is the difference
    between "anyone with the link" and "anyone at all" — the whole premise of
    an unguessable URL is that it stays unguessed, and a crawler that finds one
    publishes it forever.

    `no-referrer` matters just as much and is easier to forget: without it the
    token travels in the `Referer` header to every external link a reader
    clicks from the page, which hands it to anybody's access log.
    """
    return {
        "X-Robots-Tag": "noindex, nofollow, noarchive",
        "Referrer-Policy": "no-referrer",
        "Cache-Control": "private, no-store",
    }


def share_url(token: str) -> str:
    """Where a reader opens a share.

    Built from `APP_EXT_DOMAIN` so it is right in every environment. The path
    is the UI's, not this API's: what we put in an email has to be a page a
    person can read, not a JSON document.
    """
    from main import config

    base = str(config.settings.get("APP_EXT_DOMAIN") or "").rstrip("/")
    return f"{base}/app/shared/{token}"


async def create_share(
    *,
    owner_id: str,
    kind: str,
    payload: Dict[str, Any],
    source_id: Optional[str] = None,
    title: Optional[str] = None,
    expires_in_days: Optional[int] = None,
) -> Dict[str, Any]:
    """Publish a scrubbed snapshot and return its token."""
    if kind not in KINDS:
        raise ValueError(f"Unknown share kind {kind!r}")

    from backend.postgres import POSTGRES_ASYNC_SESSION_FACTORY
    from sql import ShareLink

    share = ShareLink(
        token=new_token(),
        kind=kind,
        source_id=source_id,
        owner_id=owner_id,
        title=(title or "").strip()[:200] or None,
        payload=scrub(kind, payload),
        expires_at=expiry_from_days(expires_in_days),
    )
    async with POSTGRES_ASYNC_SESSION_FACTORY()() as db:
        db.add(share)
        await db.commit()

    logger.info("share.created kind=%s token=%s", kind, share.token[:8])
    return {
        "token": share.token,
        "kind": kind,
        "title": share.title,
        "expires_at": share.expires_at.isoformat() if share.expires_at else None,
    }


async def read_share(token: str) -> Optional[Dict[str, Any]]:
    """The snapshot behind a token, or None if it is not live.

    Counting the view is best-effort and never blocks the read: a share that
    will not increment is still a share somebody is trying to look at.
    """
    from sqlalchemy import select, update

    from backend.postgres import POSTGRES_ASYNC_SESSION_FACTORY
    from sql import ShareLink

    if not token or len(token) > 64:
        return None

    async with POSTGRES_ASYNC_SESSION_FACTORY()() as db:
        share = (
            await db.execute(select(ShareLink).where(ShareLink.token == token))
        ).scalar_one_or_none()
        if not is_live(share):
            return None

        seen = {
            "kind": share.kind,
            "title": share.title,
            "payload": share.payload or {},
            "created_at": share.created_at.isoformat() if share.created_at else None,
        }
        try:
            await db.execute(
                update(ShareLink)
                .where(ShareLink.token == token)
                .values(
                    view_count=ShareLink.view_count + 1,
                    last_viewed_at=datetime.now(timezone.utc),
                )
            )
            await db.commit()
        except Exception:
            logger.warning("share.view_not_counted token=%s", token[:8], exc_info=True)

    return seen


async def revoke_share(*, owner_id: str, token: str) -> bool:
    """Stop serving a share. Only its owner may, and only once."""
    from sqlalchemy import update

    from backend.postgres import POSTGRES_ASYNC_SESSION_FACTORY
    from sql import ShareLink

    async with POSTGRES_ASYNC_SESSION_FACTORY()() as db:
        result = await db.execute(
            update(ShareLink)
            .where(
                ShareLink.token == token,
                ShareLink.owner_id == owner_id,
                ShareLink.revoked_at.is_(None),
            )
            .values(revoked_at=datetime.now(timezone.utc))
        )
        await db.commit()
    return bool(result.rowcount)


async def list_shares(*, owner_id: str, limit: int = 50) -> List[Dict[str, Any]]:
    """What this person has shared, newest first.

    The payload is left out: this answers "what is out there" and the owner
    already knows what is in it.
    """
    from sqlalchemy import select

    from backend.postgres import POSTGRES_ASYNC_SESSION_FACTORY
    from sql import ShareLink

    async with POSTGRES_ASYNC_SESSION_FACTORY()() as db:
        rows = (
            await db.execute(
                select(ShareLink)
                .where(ShareLink.owner_id == owner_id)
                .order_by(ShareLink.created_at.desc())
                .limit(max(1, min(int(limit), 200)))
            )
        ).scalars().all()

    return [
        {
            "token": r.token,
            "kind": r.kind,
            "title": r.title,
            "created_at": r.created_at.isoformat() if r.created_at else None,
            "expires_at": r.expires_at.isoformat() if r.expires_at else None,
            "revoked": r.revoked_at is not None,
            "live": is_live(r),
            "view_count": r.view_count,
            "last_viewed_at": r.last_viewed_at.isoformat() if r.last_viewed_at else None,
        }
        for r in rows
    ]


#: How long a share outlives the account that made it, when that account went
#: away on its own rather than by request.
ORPHAN_GRACE_DAYS = 30


async def purge_owner_shares(owner_id: str, *, grace: bool = False) -> int:
    """Remove — or time-limit — the shares an account left behind.

    Two different endings, because two different things happened.

    Somebody who asks to be erased is asking for their content to go, and a
    scrubbed snapshot is still their content. Those are deleted.

    A guest whose lifetime simply ran out never asked for anything. Deleting
    their links would break a page somebody else already has open, and the
    snapshot names nobody, so those are given a grace period instead: long
    enough that a link sent to a friend still works, short enough that it does
    not become an immortal orphan with no owner left to revoke it. An existing
    earlier expiry is left alone — it was chosen deliberately.
    """
    from sqlalchemy import delete, update

    from backend.postgres import POSTGRES_ASYNC_SESSION_FACTORY
    from sql import ShareLink

    async with POSTGRES_ASYNC_SESSION_FACTORY()() as db:
        if grace:
            deadline = datetime.now(timezone.utc) + timedelta(days=ORPHAN_GRACE_DAYS)
            result = await db.execute(
                update(ShareLink)
                .where(
                    ShareLink.owner_id == owner_id,
                    ShareLink.revoked_at.is_(None),
                    (ShareLink.expires_at.is_(None)) | (ShareLink.expires_at > deadline),
                )
                .values(expires_at=deadline)
            )
        else:
            result = await db.execute(
                delete(ShareLink).where(ShareLink.owner_id == owner_id)
            )
        await db.commit()
    return int(result.rowcount or 0)
