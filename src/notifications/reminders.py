"""Inactivity reminders: who is due one, and sending it exactly once.

The daily cycle, in order:

1. **Who exists.** Every Keycloak account, with the realm roles that decide
   eligibility (guest, agent, admin, expert).
2. **Who may be written to.** Enabled, a deliverable address — verified, by
   default — not a guest, an agent or a service account, not staff unless
   the admin includes them, and not someone who turned reminders off.
3. **Who is due.** From ``user_activity``: away at least
   ``inactive_after_days``, fewer than ``max_reminders`` sent in this stretch
   of absence, and ``repeat_every_days`` since the last one. A stretch is
   identified by the ``last_seen_at`` it was computed from, so coming back
   (a newer last visit) starts a new stretch with a fresh count.
4. **Send, once.** Each reminder is claimed in ``notification_log`` before it
   is sent; a unique index on (person, stretch, sequence) means a second
   replica, a second worker or a manual run racing the scheduled one simply
   finds the claim taken. Failed sends are retried on later cycles, up to
   three attempts.

Accounts that existed before activity tracking have no recorded visit. They
are given one at the moment the cycle first sees them, marked ``estimated``,
so nobody is reminded on day one for an absence nobody measured — and the mail
they eventually get says "a while" rather than inventing a number.

Experimental mode runs all of the above against its own history (``mode`` is
part of every key), delivering each reminder to the test recipients with a
banner naming who it was for. Switching to live afterwards starts clean: a
participant is never denied their first reminder because an admin previewed it.
"""
from __future__ import annotations

import asyncio
import logging
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

import mailer
from notifications import config as ncfg
from notifications import content, preferences
from notifications.config import KIND

logger = logging.getLogger(__name__)

STAFF_ROLES = frozenset({"admin", "expert"})
_ROLE_LOOKUPS = ("guest", "agent", "admin", "expert")
MAX_ATTEMPTS = 3
#: A claim this old whose send never reported back belongs to a process that
#: died mid-send; it is fair game for the next cycle.
STALE_CLAIM = timedelta(hours=1)
PREVIEW_TOKEN = "preview"


class ReminderError(RuntimeError):
    """A cycle that cannot run, with a message fit for the console."""


# ------------------------------------------------------------------ model --

@dataclass(frozen=True)
class Account:
    user_id: str
    username: str
    email: str
    first_name: str
    email_verified: bool
    enabled: bool
    roles: frozenset = frozenset()
    locale: Optional[str] = None


@dataclass(frozen=True)
class Activity:
    last_seen_at: datetime
    estimated: bool = False
    locale: Optional[str] = None


@dataclass
class Due:
    account: Account
    activity: Activity
    sequence: int

    def days_inactive(self, now: datetime) -> Optional[int]:
        if self.activity.estimated:
            return None
        return max((now - self.activity.last_seen_at).days, 0)

    @property
    def locale(self) -> Optional[str]:
        return self.activity.locale or self.account.locale


@dataclass
class Plan:
    considered: int = 0
    eligible: int = 0
    due: List[Due] = field(default_factory=list)
    skipped: Dict[str, int] = field(default_factory=dict)


# ------------------------------------------------------------ pure rules --

def due_sequence(
    *,
    now: datetime,
    last_seen: datetime,
    sent_in_stretch: int,
    last_sent_at: Optional[datetime],
    config: Dict[str, Any],
) -> Optional[int]:
    """Which reminder (1st, 2nd, ...) is due now in this stretch, if any."""
    if now - last_seen < timedelta(days=config["inactive_after_days"]):
        return None
    if sent_in_stretch >= config["max_reminders"]:
        return None
    if sent_in_stretch == 0:
        return 1
    if last_sent_at is None or now - last_sent_at >= timedelta(days=config["repeat_every_days"]):
        return sent_in_stretch + 1
    return None


def skip_reason(account: Account, *, config: Dict[str, Any], opted_out: Set[str]) -> Optional[str]:
    """Why this account is never written to, or None if it may be."""
    if account.username.startswith("service-account-"):
        return "service_account"
    if "guest" in account.roles or account.username.startswith("guest-"):
        return "guest"
    if "agent" in account.roles:
        return "agent"
    if not account.enabled:
        return "disabled"
    if not mailer.valid_address(account.email):
        return "no_email"
    if config["verified_only"] and not account.email_verified:
        return "unverified"
    if not config["include_staff"] and account.roles & STAFF_ROLES:
        return "staff"
    if account.user_id in opted_out:
        return "opted_out"
    return None


def mask_email(address: str) -> str:
    """``ana.k@example.org`` -> ``a***@example.org``: enough to tell rows apart."""
    local, _, domain = (address or "").partition("@")
    if not domain:
        return "***"
    return f"{local[:1]}***@{domain}"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------- reading --

def _fetch_accounts_blocking() -> List[Account]:
    from backend.keycloak import KEYCLOAK_ADMIN_CLIENT

    admin = KEYCLOAK_ADMIN_CLIENT()
    roles_by_user: Dict[str, Set[str]] = {}
    for role in _ROLE_LOOKUPS:
        try:
            members = admin.get_realm_role_members(role) or []
        except Exception:
            # A realm without the role has nobody in it.
            logger.debug("notifications.role_lookup_failed role=%s", role, exc_info=True)
            continue
        for member in members:
            if member.get("id"):
                roles_by_user.setdefault(member["id"], set()).add(role)

    accounts: List[Account] = []
    for user in admin.get_users({"briefRepresentation": False}) or []:
        user_id = user.get("id")
        if not user_id:
            continue
        attributes = user.get("attributes") or {}
        locale = (attributes.get("locale") or [None])[0]
        accounts.append(
            Account(
                user_id=user_id,
                username=str(user.get("username") or ""),
                email=str(user.get("email") or "").strip(),
                first_name=str(user.get("firstName") or "").strip(),
                email_verified=bool(user.get("emailVerified")),
                enabled=bool(user.get("enabled", True)),
                roles=frozenset(roles_by_user.get(user_id, ())),
                locale=locale,
            )
        )
    return accounts


async def fetch_accounts() -> List[Account]:
    from starlette.concurrency import run_in_threadpool

    return await run_in_threadpool(_fetch_accounts_blocking)


def _chunks(items: Sequence[str], size: int = 500) -> Iterable[Sequence[str]]:
    for start in range(0, len(items), size):
        yield items[start:start + size]


async def load_activity(user_ids: Sequence[str], now: datetime) -> Dict[str, Activity]:
    """Each account's last visit, starting the clock for anyone never seen."""
    from sqlalchemy import select
    from sqlalchemy.dialects.postgresql import insert

    from backend.postgres import POSTGRES_ASYNC_SESSION_FACTORY
    from sql import UserActivity

    table = UserActivity.__table__
    found: Dict[str, Activity] = {}
    async with POSTGRES_ASYNC_SESSION_FACTORY()() as db:
        for chunk in _chunks(list(user_ids)):
            await db.execute(
                insert(table)
                .values([
                    {"user_id": uid, "last_seen_at": now, "first_seen_at": now, "estimated": True}
                    for uid in chunk
                ])
                .on_conflict_do_nothing(index_elements=[table.c.user_id])
            )
            rows = (
                await db.execute(
                    select(table.c.user_id, table.c.last_seen_at, table.c.estimated, table.c.locale)
                    .where(table.c.user_id.in_(chunk))
                )
            ).all()
            for uid, last_seen, estimated, locale in rows:
                found[uid] = Activity(last_seen_at=last_seen, estimated=bool(estimated), locale=locale)
        await db.commit()
    return found


async def load_history(
    mode: str, stretches: Dict[str, datetime]
) -> Dict[str, Tuple[int, Optional[datetime]]]:
    """Reminders already sent in each account's current stretch, and when."""
    from sqlalchemy import func, select

    from backend.postgres import POSTGRES_ASYNC_SESSION_FACTORY
    from sql import NotificationLog

    table = NotificationLog.__table__
    history: Dict[str, Tuple[int, Optional[datetime]]] = {}
    if not stretches:
        return history
    async with POSTGRES_ASYNC_SESSION_FACTORY()() as db:
        for chunk in _chunks(list(stretches)):
            rows = (
                await db.execute(
                    select(
                        table.c.user_id,
                        table.c.inactive_since,
                        func.count(),
                        func.max(table.c.sent_at),
                    )
                    .where(
                        table.c.kind == KIND,
                        table.c.mode == mode,
                        table.c.status == "sent",
                        table.c.user_id.in_(chunk),
                    )
                    .group_by(table.c.user_id, table.c.inactive_since)
                )
            ).all()
            for uid, since, count, last_sent in rows:
                if since == stretches.get(uid):
                    history[uid] = (int(count), last_sent)
    return history


async def plan(*, config: Dict[str, Any], mode: str, now: datetime) -> Plan:
    """Everyone who would get a reminder if a cycle ran at ``now``."""
    accounts = await fetch_accounts()
    opted_out = await preferences.opted_out()

    skipped: Counter = Counter()
    eligible: List[Account] = []
    for account in accounts:
        reason = skip_reason(account, config=config, opted_out=opted_out)
        if reason:
            skipped[reason] += 1
        else:
            eligible.append(account)

    activity = await load_activity([a.user_id for a in eligible], now)
    history = await load_history(mode, {uid: act.last_seen_at for uid, act in activity.items()})

    due: List[Due] = []
    threshold = timedelta(days=config["inactive_after_days"])
    for account in eligible:
        act = activity.get(account.user_id)
        if act is None:
            skipped["no_activity_row"] += 1
            continue
        sent, last_sent = history.get(account.user_id, (0, None))
        sequence = due_sequence(
            now=now, last_seen=act.last_seen_at, sent_in_stretch=sent,
            last_sent_at=last_sent, config=config,
        )
        if sequence is None:
            if now - act.last_seen_at < threshold:
                skipped["recently_active"] += 1
            elif sent >= config["max_reminders"]:
                skipped["reminders_used_up"] += 1
            else:
                skipped["waiting_for_next"] += 1
            continue
        due.append(Due(account=account, activity=act, sequence=sequence))

    # Longest away first, so a capped cycle reaches them before newer absences.
    due.sort(key=lambda d: d.activity.last_seen_at)
    return Plan(considered=len(accounts), eligible=len(eligible), due=due, skipped=dict(skipped))


# ---------------------------------------------------------------- writing --

async def _open_run(*, source: str, mode: str, now: datetime, triggered_by: Optional[str]) -> Optional[int]:
    from sqlalchemy import text
    from sqlalchemy.dialects.postgresql import insert

    from backend.postgres import POSTGRES_ASYNC_SESSION_FACTORY
    from sql import NotificationRun

    table = NotificationRun.__table__
    stmt = insert(table).values(
        kind=KIND, mode=mode, source=source, run_date=now.date(),
        triggered_by=triggered_by, started_at=now, stats={},
    )
    if source == "scheduled":
        stmt = stmt.on_conflict_do_nothing(
            index_elements=[table.c.kind, table.c.mode, table.c.run_date],
            # A literal, not a bound parameter. Postgres has to prove the
            # partial index applies when it plans the statement; with
            # `source = $8` it can do that for the first few executions, which
            # get custom plans, and not once asyncpg's cached statement goes
            # generic — after which every scheduled tick failed with "no unique
            # or exclusion constraint matching the ON CONFLICT specification".
            index_where=text("source = 'scheduled'"),
        )
    async with POSTGRES_ASYNC_SESSION_FACTORY()() as db:
        run_id = (await db.execute(stmt.returning(table.c.id))).scalar_one_or_none()
        await db.commit()
    return run_id


async def _close_run(run_id: int, stats: Dict[str, Any], error: Optional[str]) -> None:
    from sqlalchemy import update

    from backend.postgres import POSTGRES_ASYNC_SESSION_FACTORY
    from sql import NotificationRun

    async with POSTGRES_ASYNC_SESSION_FACTORY()() as db:
        await db.execute(
            update(NotificationRun.__table__)
            .where(NotificationRun.__table__.c.id == run_id)
            .values(finished_at=_utcnow(), stats=stats, error=error)
        )
        await db.commit()


async def _claim(*, mode: str, due: Due, run_id: Optional[int], now: datetime) -> Optional[int]:
    """Reserve this reminder. The log row id, or None if it is someone else's."""
    from sqlalchemy.dialects.postgresql import insert

    from backend.postgres import POSTGRES_ASYNC_SESSION_FACTORY
    from sql import NotificationLog

    table = NotificationLog.__table__
    stmt = insert(table).values(
        kind=KIND, mode=mode, user_id=due.account.user_id,
        inactive_since=due.activity.last_seen_at, sequence=due.sequence,
        status="claimed", attempts=1, run_id=run_id, created_at=now,
    )
    retryable = (
        ((table.c.status == "failed") | ((table.c.status == "claimed") & (table.c.created_at < now - STALE_CLAIM)))
        & (table.c.attempts < MAX_ATTEMPTS)
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=[table.c.kind, table.c.mode, table.c.user_id, table.c.inactive_since, table.c.sequence],
        set_={"status": "claimed", "attempts": table.c.attempts + 1, "run_id": run_id, "error": None},
        where=retryable,
    )
    async with POSTGRES_ASYNC_SESSION_FACTORY()() as db:
        log_id = (await db.execute(stmt.returning(table.c.id))).scalar_one_or_none()
        await db.commit()
    return log_id


async def _finish(log_id: int, *, ok: bool, recipient: str, error: Optional[str],
                  at: Optional[datetime] = None) -> None:
    """Record the outcome. ``at`` is the cycle's clock, so "a week after the last
    reminder" is measured on the same clock that decided the reminder was due."""
    from sqlalchemy import update

    from backend.postgres import POSTGRES_ASYNC_SESSION_FACTORY
    from sql import NotificationLog

    async with POSTGRES_ASYNC_SESSION_FACTORY()() as db:
        await db.execute(
            update(NotificationLog.__table__)
            .where(NotificationLog.__table__.c.id == log_id)
            .values(
                status="sent" if ok else "failed",
                recipient=recipient[:255],
                error=None if ok else (error or "the mail server did not accept it")[:500],
                sent_at=(at or _utcnow()) if ok else None,
            )
        )
        await db.commit()


async def _log_test(*, recipient: str, ok: bool, triggered_by: Optional[str]) -> None:
    from sqlalchemy.dialects.postgresql import insert

    from backend.postgres import POSTGRES_ASYNC_SESSION_FACTORY
    from sql import NotificationLog

    now = _utcnow()
    async with POSTGRES_ASYNC_SESSION_FACTORY()() as db:
        await db.execute(
            insert(NotificationLog.__table__).values(
                kind=KIND, mode="test", user_id=None, recipient=recipient[:255],
                status="sent" if ok else "failed", attempts=1, created_at=now,
                sent_at=now if ok else None,
                error=None if ok else "the mail server did not accept it",
            )
        )
        await db.commit()


# --------------------------------------------------------------- sending --

def _urls(token: str, locale: Optional[str] = None) -> Dict[str, str]:
    # The page a person lands on from the mail speaks the mail's language.
    lang = content.email_layout.normalize_locale(locale)
    return {
        "app": mailer.app_url("/dashboard"),
        "unsubscribe_page": mailer.app_url(f"/unsubscribe?token={token}&lang={lang}"),
        "unsubscribe_one_click": mailer.api_url(f"/api/v1/notifications/unsubscribe?token={token}"),
        "settings": mailer.app_url("/my-profile"),
    }


async def _send_live(due: Due, now: datetime) -> Tuple[bool, str]:
    token = await preferences.ensure_token(due.account.user_id)
    urls = _urls(token, due.locale)
    reminder = content.render_reminder(
        locale=due.locale,
        first_name=due.account.first_name,
        days_inactive=due.days_inactive(now),
        app_url=urls["app"],
        unsubscribe_url=urls["unsubscribe_page"],
        settings_url=urls["settings"],
    )
    ok = await mailer.send_email(
        to=due.account.email,
        subject=reminder.subject,
        html_body=reminder.email.html,
        text_body=reminder.email.text,
        images=reminder.email.images,
        unsubscribe_url=urls["unsubscribe_one_click"],
        headers={"X-WiseFood-Notification": KIND},
    )
    return ok, mask_email(due.account.email)


async def _send_experimental(due: Due, now: datetime, recipients: Sequence[str]) -> Tuple[bool, str]:
    # A preview token: the page explains that nothing changed, so an admin
    # clicking "turn off" in a preview cannot unsubscribe the participant.
    urls = _urls(PREVIEW_TOKEN, due.locale)
    banner = content.banner_text(
        "experimental", recipient=mask_email(due.account.email), name=due.account.first_name
    )
    reminder = content.render_reminder(
        locale=due.locale,
        first_name=due.account.first_name,
        days_inactive=due.days_inactive(now),
        app_url=urls["app"],
        unsubscribe_url=urls["unsubscribe_page"],
        settings_url=urls["settings"],
        banner=banner,
    )
    results = []
    for address in recipients:
        results.append(
            await mailer.send_email(
                to=address,
                subject=f"[Experimental] {reminder.subject}",
                html_body=reminder.email.html,
                text_body=reminder.email.text,
                images=reminder.email.images,
                headers={"X-WiseFood-Notification": f"{KIND}; mode=experimental"},
            )
        )
    return all(results) and bool(results), ", ".join(recipients)


def _pace() -> float:
    from main import config

    return float(config.settings.get("NOTIFICATIONS_SEND_INTERVAL_SECONDS", 0.5))


def _check_can_send(config: Dict[str, Any]) -> List[str]:
    """Refuse plainly what would otherwise fail one mail at a time."""
    if config["mode"] == "off":
        raise ReminderError("Reminders are off. Choose experimental or live first.")
    if not mailer.enabled():
        raise ReminderError("Email is not configured on this deployment (SMTP_HOST is unset).")
    recipients: List[str] = []
    if config["mode"] == "experimental":
        recipients = ncfg.effective_test_recipients(config)
        if not recipients:
            raise ReminderError(
                "Experimental mode has nowhere to send: add a test recipient or set ADMIN_EMAIL."
            )
    return recipients


async def run_cycle(
    *, source: str, triggered_by: Optional[str] = None, now: Optional[datetime] = None
) -> Optional[Dict[str, Any]]:
    """Run one reminder cycle. None if today's scheduled run already happened."""
    config = ncfg.current()
    recipients = _check_can_send(config)
    mode = config["mode"]
    now = now or _utcnow()

    run_id = await _open_run(source=source, mode=mode, now=now, triggered_by=triggered_by)
    if run_id is None:
        return None

    stats: Dict[str, Any] = {
        "mode": mode, "considered": 0, "eligible": 0, "due": 0,
        "sent": 0, "failed": 0, "already_handled": 0, "deferred": 0, "skipped": {},
    }
    try:
        result = await plan(config=config, mode=mode, now=now)
        batch = result.due[: config["max_per_run"]]
        stats.update(
            considered=result.considered, eligible=result.eligible, due=len(result.due),
            deferred=len(result.due) - len(batch), skipped=result.skipped,
        )
        for index, due in enumerate(batch):
            log_id = await _claim(mode=mode, due=due, run_id=run_id, now=now)
            if log_id is None:
                stats["already_handled"] += 1
                continue
            try:
                if mode == "live":
                    ok, recipient = await _send_live(due, now)
                else:
                    ok, recipient = await _send_experimental(due, now, recipients)
                error = None
            except Exception as exc:  # rendering or token trouble for one person
                logger.warning("notifications.send_failed user=%s", due.account.user_id, exc_info=True)
                ok, recipient, error = False, mask_email(due.account.email), str(exc)
            await _finish(log_id, ok=ok, recipient=recipient, error=error, at=now)
            stats["sent" if ok else "failed"] += 1
            if index < len(batch) - 1:
                await asyncio.sleep(_pace())
    except Exception as exc:
        await _close_run(run_id, stats, error=str(exc)[:500])
        raise
    await _close_run(run_id, stats, error=None)
    logger.info("notifications.cycle_done", extra={"run_id": run_id, "source": source, **{k: v for k, v in stats.items() if k != "skipped"}})
    return {"run_id": run_id, **stats}


async def send_test(*, to: str, first_name: str = "", locale: str = "en",
                    triggered_by: Optional[str] = None) -> bool:
    """A sample reminder for an admin to look at. Works in any mode."""
    if not mailer.enabled():
        raise ReminderError("Email is not configured on this deployment (SMTP_HOST is unset).")
    if not mailer.valid_address(to):
        raise ReminderError("That is not an address the reminder can be sent to.")
    urls = _urls(PREVIEW_TOKEN, locale)
    reminder = content.render_reminder(
        locale=locale,
        first_name=first_name,
        days_inactive=ncfg.current()["inactive_after_days"],
        app_url=urls["app"],
        unsubscribe_url=urls["unsubscribe_page"],
        settings_url=urls["settings"],
        banner=content.banner_text("test", locale=locale),
    )
    ok = await mailer.send_email(
        to=to,
        subject=f"[Test] {reminder.subject}",
        html_body=reminder.email.html,
        text_body=reminder.email.text,
        images=reminder.email.images,
        headers={"X-WiseFood-Notification": f"{KIND}; mode=test"},
    )
    try:
        await _log_test(recipient=to, ok=ok, triggered_by=triggered_by)
    except Exception:
        logger.debug("notifications.test_log_failed", exc_info=True)
    return ok


# --------------------------------------------------------------- console --

async def schema_ready() -> bool:
    from sqlalchemy import text

    from backend.postgres import POSTGRES_ASYNC_SESSION_FACTORY

    try:
        async with POSTGRES_ASYNC_SESSION_FACTORY()() as db:
            await db.execute(text(
                "SELECT 1 FROM wisefood.notification_run, wisefood.notification_log, "
                "wisefood.notification_preference, wisefood.user_activity LIMIT 1"
            ))
        return True
    except Exception:
        return False


def next_scheduled_run(config: Dict[str, Any], *, now: datetime, ran_today: bool) -> Optional[datetime]:
    if config["mode"] == "off":
        return None
    today = now.replace(hour=config["send_hour_utc"], minute=0, second=0, microsecond=0)
    if not ran_today and now < today:
        return today
    if not ran_today:
        # Past the hour and not yet run: the next scheduler tick picks it up.
        return now
    return today + timedelta(days=1)


async def status() -> Dict[str, Any]:
    """Everything the console's reminder card shows above the fold."""
    from sqlalchemy import func, select

    from backend.postgres import POSTGRES_ASYNC_SESSION_FACTORY
    from main import config as app_config
    from sql import NotificationLog, NotificationPreference, NotificationRun, UserActivity

    config = ncfg.current()
    now = _utcnow()
    body: Dict[str, Any] = {
        "config": config,
        "defaults": ncfg.DEFAULT_CONFIG,
        "limits": {k: list(v) for k, v in ncfg.LIMITS.items()},
        "mail_enabled": mailer.enabled(),
        "scheduler_enabled": bool(app_config.settings.get("NOTIFICATIONS_ENABLED", True)),
        "effective_test_recipients": ncfg.effective_test_recipients(config),
        "admin_email": app_config.settings.get("ADMIN_EMAIL") or None,
        "schema_ready": await schema_ready(),
        "runs": [],
        "counts": {},
        "next_run_at": None,
        "now": now,
    }
    if not body["schema_ready"]:
        return body

    run = NotificationRun.__table__
    log = NotificationLog.__table__
    async with POSTGRES_ASYNC_SESSION_FACTORY()() as db:
        runs = (
            await db.execute(
                select(run).where(run.c.kind == KIND).order_by(run.c.started_at.desc()).limit(10)
            )
        ).mappings().all()
        ran_today = bool(
            (
                await db.execute(
                    select(func.count()).select_from(run).where(
                        run.c.kind == KIND, run.c.source == "scheduled",
                        run.c.mode == config["mode"], run.c.run_date == now.date(),
                    )
                )
            ).scalar_one()
        )
        tracked = (await db.execute(select(func.count()).select_from(UserActivity.__table__))).scalar_one()
        opted = (
            await db.execute(
                select(func.count()).select_from(NotificationPreference.__table__).where(
                    NotificationPreference.__table__.c.inactivity_reminders.is_(False)
                )
            )
        ).scalar_one()
        since = now - timedelta(days=30)
        sent_30d = dict(
            (
                await db.execute(
                    select(log.c.mode, func.count())
                    .where(log.c.kind == KIND, log.c.status == "sent", log.c.created_at >= since)
                    .group_by(log.c.mode)
                )
            ).all()
        )
    body["runs"] = [dict(r) for r in runs]
    body["counts"] = {
        "tracked_accounts": int(tracked),
        "opted_out": int(opted),
        "sent_last_30_days": {mode: int(n) for mode, n in sent_30d.items()},
    }
    body["next_run_at"] = next_scheduled_run(config, now=now, ran_today=ran_today)
    return body


async def preview(limit: int = 100) -> Dict[str, Any]:
    """Who a cycle would write to right now, without writing to anyone.

    In ``off`` mode this answers for live, which is the question an admin has
    before switching it on. Seeing someone here starts their clock if it had
    not started — the same thing the first real cycle would do.
    """
    config = ncfg.current()
    mode = "experimental" if config["mode"] == "experimental" else "live"
    now = _utcnow()
    result = await plan(config=config, mode=mode, now=now)
    rows = [
        {
            "user_id": d.account.user_id,
            "email": mask_email(d.account.email),
            "first_name": d.account.first_name or None,
            "locale": content.email_layout.normalize_locale(d.locale),
            "last_seen_at": d.activity.last_seen_at,
            "estimated": d.activity.estimated,
            "days_inactive": d.days_inactive(now),
            "sequence": d.sequence,
            "in_next_run": index < config["max_per_run"],
        }
        for index, d in enumerate(result.due[:limit])
    ]
    return {
        "mode": mode,
        "considered": result.considered,
        "eligible": result.eligible,
        "due": len(result.due),
        "max_per_run": config["max_per_run"],
        "skipped": result.skipped,
        "rows": rows,
    }


async def recent(limit: int = 50) -> List[Dict[str, Any]]:
    from sqlalchemy import select

    from backend.postgres import POSTGRES_ASYNC_SESSION_FACTORY
    from sql import NotificationLog

    log = NotificationLog.__table__
    async with POSTGRES_ASYNC_SESSION_FACTORY()() as db:
        rows = (
            await db.execute(
                select(
                    log.c.id, log.c.mode, log.c.user_id, log.c.recipient, log.c.sequence,
                    log.c.inactive_since, log.c.status, log.c.attempts, log.c.error,
                    log.c.run_id, log.c.created_at, log.c.sent_at,
                )
                .where(log.c.kind == KIND)
                .order_by(log.c.created_at.desc(), log.c.id.desc())
                .limit(max(1, min(int(limit), 200)))
            )
        ).mappings().all()
    return [dict(r) for r in rows]


# -------------------------------------------------------------- schedule --

_warned: Set[str] = set()


def _warn_once(key: str, message: str) -> None:
    level = logging.DEBUG if key in _warned else logging.WARNING
    _warned.add(key)
    logger.log(level, message)


async def scheduler_tick(now: Optional[datetime] = None) -> Optional[Dict[str, Any]]:
    """What the background loop does every few minutes.

    Cheap when there is nothing to do — a cached settings read and a clock
    check — and at most one cycle per mode per day, whichever replica gets
    there first.
    """
    from analytics import SETTINGS

    await SETTINGS.refresh_if_stale()
    config = ncfg.current()
    if config["mode"] == "off":
        return None
    now = now or _utcnow()
    if now.hour < config["send_hour_utc"]:
        return None
    try:
        return await run_cycle(source="scheduled", now=now)
    except ReminderError as exc:
        _warn_once(str(exc), f"notifications.cycle_skipped: {exc}")
        return None
