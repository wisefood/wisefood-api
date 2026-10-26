"""Inactivity reminder endpoints.

Admin-only, for the console's Platform Operations page: read and change the
configuration, see who a cycle would write to, send yourself a sample, run a
cycle now, and read what was sent.

Two public endpoints, for the link in every reminder: turning reminders off
(and back on) needs nothing but the random token the link carries, so it works
from any mail client without a login. ``POST /unsubscribe`` is also the
RFC 8058 one-click target named in the ``List-Unsubscribe`` header, which mail
clients call directly. Neither acts on a GET: link scanners and prefetchers
follow every URL in a mail, and must not unsubscribe anybody by doing so.
"""
import logging
from typing import Optional

from fastapi import APIRouter, Depends, Query, Request

import kutils
from auth import auth
from budget import ip_rate_limit
from exceptions import ConflictError, DataError, ServiceUnavailableError
from routers.generic import render
from schemas import InactivityReminderConfigUpdate, InactivityReminderTestSend

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/notifications", tags=["Notifications"])

_SCHEMA_HINT = (
    "The notifications schema has not been applied to this database. Apply "
    "schemas/70_notifications.sql (it is idempotent), then try again."
)


async def _require_schema() -> None:
    from notifications import reminders

    if not await reminders.schema_ready():
        raise ServiceUnavailableError(detail=_SCHEMA_HINT)


def _actor(request: Request) -> Optional[str]:
    return (kutils.current_user(request) or {}).get("sub")


# ------------------------------------------------------------------ admin --

@router.get(
    "/inactivity",
    dependencies=[Depends(auth("admin"))],
    summary="Inactivity reminders: configuration, health and recent cycles",
)
@render()
async def get_inactivity_reminders(request: Request):
    from analytics import SETTINGS
    from notifications import reminders

    await SETTINGS.refresh_if_stale()
    return await reminders.status()


@router.put(
    "/inactivity",
    dependencies=[Depends(auth("admin"))],
    summary="Change the inactivity reminder configuration",
    description=(
        "Replaces the whole configuration; keys left out take their defaults. "
        "Stored in the admin settings table, so it reaches every replica within "
        "about thirty seconds and the change is recorded with who made it."
    ),
)
@render()
async def put_inactivity_reminders(request: Request, body: InactivityReminderConfigUpdate):
    from analytics import RECORDER, SETTINGS
    from notifications import config as ncfg, reminders

    try:
        value = ncfg.validate(body.config)
    except ValueError as exc:
        raise DataError(detail=str(exc)) from exc

    actor = _actor(request)
    await SETTINGS.put(ncfg.SETTING_KEY, value, actor)
    await SETTINGS.refresh_if_stale()
    RECORDER.record_event(
        "admin.settings_changed",
        app="console",
        props={"key": ncfg.SETTING_KEY, "value": value},
    )
    logger.info("notifications.config_changed", extra={"actor": actor, "mode": value["mode"]})
    return await reminders.status()


@router.get(
    "/inactivity/preview",
    dependencies=[Depends(auth("admin"))],
    summary="Who a reminder cycle would write to right now",
    description=(
        "Runs the selection without sending anything. Addresses are masked. "
        "In 'off' mode it answers for live, which is the question to ask before "
        "switching it on."
    ),
)
@render()
async def preview_inactivity_reminders(request: Request, limit: int = Query(100, ge=1, le=500)):
    from notifications import reminders

    await _require_schema()
    return await reminders.preview(limit=limit)


@router.get(
    "/inactivity/log",
    dependencies=[Depends(auth("admin"))],
    summary="Recently sent reminders",
)
@render()
async def inactivity_reminder_log(request: Request, limit: int = Query(50, ge=1, le=200)):
    from notifications import reminders

    await _require_schema()
    return {"rows": await reminders.recent(limit=limit)}


@router.post(
    "/inactivity/test",
    dependencies=[Depends(auth("admin"))],
    summary="Send yourself a sample reminder",
    description=(
        "Works in every mode, including off. Goes to `to`, or to your own "
        "address. The unsubscribe link in a sample does nothing."
    ),
)
@render()
async def test_inactivity_reminder(request: Request, body: InactivityReminderTestSend):
    from notifications import reminders

    await _require_schema()
    user = kutils.current_user(request) or {}
    to = (body.to or user.get("email") or "").strip()
    try:
        sent = await reminders.send_test(
            to=to,
            first_name=str(user.get("given_name") or ""),
            locale=body.locale or "en",
            triggered_by=user.get("sub"),
        )
    except reminders.ReminderError as exc:
        raise ConflictError(detail=str(exc)) from exc
    return {"sent": sent, "to": to}


@router.post(
    "/inactivity/run",
    dependencies=[Depends(auth("admin"))],
    summary="Run a reminder cycle now",
    description=(
        "Runs immediately in the configured mode, outside the daily schedule. "
        "Nobody gets a reminder twice: anyone already reminded in this stretch "
        "of absence is skipped, whichever cycle reached them first."
    ),
)
@render()
async def run_inactivity_reminders(request: Request):
    from notifications import reminders

    await _require_schema()
    try:
        return await reminders.run_cycle(source="manual", triggered_by=_actor(request))
    except reminders.ReminderError as exc:
        raise ConflictError(detail=str(exc)) from exc


# ----------------------------------------------------------------- public --

@router.post(
    "/unsubscribe",
    dependencies=[Depends(ip_rate_limit("notifications_unsubscribe", limit=60, window_seconds=3600))],
    summary="Turn off reminder emails — no account needed",
    description=(
        "The token from a reminder is the credential. Also the one-click "
        "target of the List-Unsubscribe header (RFC 8058), which mail clients "
        "POST to directly. Idempotent."
    ),
)
@render()
async def unsubscribe(request: Request, token: str = Query(..., max_length=64)):
    from notifications import preferences, reminders

    if token == reminders.PREVIEW_TOKEN:
        return {"unsubscribed": False, "preview": True}
    via = "list_header" if (request.query_params.get("source") != "page") else "email_link"
    try:
        user_id = await preferences.unsubscribe(token, via=via)
    except Exception as exc:
        logger.warning("notifications.unsubscribe_failed", exc_info=True)
        raise ServiceUnavailableError(detail="We could not record that just now. Please try again.") from exc
    return {"unsubscribed": bool(user_id), "preview": False}


@router.post(
    "/resubscribe",
    dependencies=[Depends(ip_rate_limit("notifications_unsubscribe", limit=60, window_seconds=3600))],
    summary="Turn reminder emails back on, from the same link",
)
@render()
async def resubscribe(request: Request, token: str = Query(..., max_length=64)):
    from notifications import preferences, reminders

    if token == reminders.PREVIEW_TOKEN:
        return {"subscribed": False, "preview": True}
    from sqlalchemy import select

    from backend.postgres import POSTGRES_ASYNC_SESSION_FACTORY
    from sql import NotificationPreference

    async with POSTGRES_ASYNC_SESSION_FACTORY()() as db:
        user_id = (
            await db.execute(
                select(NotificationPreference.user_id).where(
                    NotificationPreference.unsubscribe_token == token.strip()
                )
            )
        ).scalar_one_or_none()
    if not user_id:
        return {"subscribed": False, "preview": False}
    await preferences.set_inactivity_reminders(user_id, True, via="email_link")
    return {"subscribed": True, "preview": False}
