"""Outgoing product mail.

Separate from Keycloak's SMTP, which sends verification and password-reset
mail and knows nothing about meal plans. This is the platform speaking to a
person about their own content.

Three things this module insists on.

**Off unless configured.** With no `SMTP_HOST` the feature reports itself
unavailable rather than accepting a request and dropping it. A send that
silently goes nowhere is worse than a button that says it cannot.

**Off the event loop.** `smtplib` is blocking, and a slow mail server would
otherwise stall every other request on the worker. Callers await
`send_email`, which does the talking in a thread.

**The same scrub as a share link.** Mail leaves the building and cannot be
recalled, so a plan sent to somebody else carries the food and not the
people — see `sharing.scrub_meal_plan`. Mail to yourself goes through the
identical path, because a mailbox is not a safe place either.
"""
from __future__ import annotations

import html
import logging
import re
import smtplib
import time
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid
from typing import Any, Dict, List, Optional, Tuple

from starlette.concurrency import run_in_threadpool

logger = logging.getLogger(__name__)

#: Deliberately permissive — the mail server is the real judge of an address.
#: This only rejects what cannot be an address at all, so a typo fails here
#: instead of becoming a bounce nobody reads.
_ADDRESS = re.compile(r"^[^@\s]+@[^@\s.]+\.[^@\s]+$")

#: Per-account sends, as (user_id -> [timestamps]). In-process: a cap that
#: resets when a pod restarts is a weak cap, but it is a bound on the obvious
#: abuse and needs no new table. Redis would make it exact across replicas.
_sent: Dict[str, List[float]] = {}
_DAY = 24 * 3600


def enabled() -> bool:
    """Whether this deployment can send mail at all."""
    from main import config

    return bool(config.settings.get("SMTP_HOST"))


def valid_address(address: str) -> bool:
    return bool(_ADDRESS.match((address or "").strip()))


def within_quota(user_id: str) -> bool:
    """Whether this account has sends left today, and count one if so."""
    from main import config

    limit = int(config.settings.get("MAIL_DAILY_LIMIT", 20))
    now = time.time()
    recent = [t for t in _sent.get(user_id, []) if now - t < _DAY]
    if len(recent) >= limit:
        _sent[user_id] = recent
        return False
    recent.append(now)
    _sent[user_id] = recent
    return True


def _send_blocking(message: EmailMessage) -> None:
    from main import config

    settings = config.settings
    host, port = settings["SMTP_HOST"], int(settings["SMTP_PORT"])
    timeout = int(settings.get("SMTP_TIMEOUT_SECONDS", 20))

    if settings.get("SMTP_SSL"):
        server = smtplib.SMTP_SSL(host, port, timeout=timeout)
    else:
        server = smtplib.SMTP(host, port, timeout=timeout)
    try:
        server.ehlo()
        if settings.get("SMTP_STARTTLS") and not settings.get("SMTP_SSL"):
            server.starttls()
            server.ehlo()
        if settings.get("SMTP_USERNAME"):
            server.login(settings["SMTP_USERNAME"], settings["SMTP_PASSWORD"])
        server.send_message(message)
    finally:
        try:
            server.quit()
        except Exception:
            server.close()


async def send_email(
    *,
    to: str,
    subject: str,
    html_body: str,
    text_body: str,
    reply_to: Optional[str] = None,
) -> bool:
    """Send one mail. Returns False rather than raising.

    A failed send must not fail the request that asked for it — the plan is
    saved either way, and the caller gets to say "we could not send that"
    instead of a 500 that suggests the plan is gone too.
    """
    from main import config

    if not enabled():
        logger.info("mail.disabled subject=%r", subject[:60])
        return False
    if not valid_address(to):
        logger.info("mail.bad_address")
        return False

    settings = config.settings
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = formataddr(
        (settings.get("SMTP_FROM_NAME") or "WiseFood", settings["SMTP_FROM"])
    )
    message["To"] = to
    message["Date"] = formatdate(localtime=True)
    message["Message-ID"] = make_msgid()
    if reply_to:
        message["Reply-To"] = reply_to
    # Transactional, and never a list: say so, so a mail client does not offer
    # an unsubscribe we do not honour and a filter does not read it as bulk.
    message["Auto-Submitted"] = "auto-generated"
    message.set_content(text_body)
    message.add_alternative(html_body, subtype="html")

    try:
        await run_in_threadpool(_send_blocking, message)
        logger.info("mail.sent subject=%r", subject[:60])
        return True
    except Exception:
        logger.warning("mail.failed subject=%r", subject[:60], exc_info=True)
        return False


# ------------------------------------------------------------- meal plans --

def plan_subject(member_name: str) -> str:
    """"Hey Ana, here is your meal plan" — a person, not a notification."""
    name = (member_name or "").strip()
    return f"Hey {name}, here is your meal plan" if name else "Here is your meal plan"


def _slot_html(slot: str, dishes: Any) -> str:
    items = dishes if isinstance(dishes, list) else [dishes]
    rows = []
    for dish in items:
        if not isinstance(dish, dict):
            continue
        title = html.escape(str(dish.get("title") or "Untitled"))
        ingredients = html.escape(str(dish.get("ingredients") or ""))
        role = html.escape(str(dish.get("role") or ""))
        nutrition = dish.get("nutrition") or {}
        kcal = nutrition.get("kcal") or nutrition.get("calories")
        meta = " · ".join(x for x in [role, f"{int(kcal)} kcal" if kcal else ""] if x)
        rows.append(
            f'<div style="margin:0 0 14px">'
            f'<div style="font-size:15px;font-weight:600;color:#111">{title}</div>'
            + (f'<div style="font-size:12px;color:#777;margin-top:2px">{meta}</div>' if meta else "")
            + (f'<div style="font-size:13px;color:#444;margin-top:6px;line-height:1.5">{ingredients}</div>' if ingredients else "")
            + "</div>"
        )
    if not rows:
        return ""
    return (
        '<div style="margin:0 0 22px">'
        f'<div style="font-size:11px;letter-spacing:.08em;text-transform:uppercase;'
        f'color:#8a8a8a;margin:0 0 8px">{html.escape(slot)}</div>'
        + "".join(rows)
        + "</div>"
    )


def _flatten_week(days: Any) -> Dict[str, Any]:
    """A week reduced to one set of slots, for the mail body.

    Takes the first day that has food rather than merging all of them: merged
    days would read as one enormous day, which is worse than showing a taste
    and linking to the rest.
    """
    if not isinstance(days, list):
        return {}
    for day in days:
        if isinstance(day, dict) and day.get("meals"):
            return day["meals"]
    return {}


def render_meal_plan(
    *,
    member_name: str,
    payload: Dict[str, Any],
    share_url: Optional[str] = None,
    from_name: Optional[str] = None,
) -> Tuple[str, str]:
    """The mail body for a plan, as (html, plain text).

    Inline styles and a table-free single column, because mail clients are
    not browsers: no stylesheet survives Outlook, and a layout that needs one
    arrives as a heap. `payload` is the scrubbed share payload, so there is
    nothing here to leak.
    """
    payload = payload or {}
    date = payload.get("date")
    # A weekly share carries `days`, a daily one carries `meals`. Flattened to
    # the daily shape rather than given its own template: an email is read on
    # a phone, and seven days of cards is not a thing anybody scrolls. The
    # link goes to the full week.
    meals = payload.get("meals") or _flatten_week(payload.get("days"))

    intro = (
        f"{html.escape(from_name)} shared a meal plan with you."
        if from_name else "Here is your meal plan."
    )
    heading = f"Meal plan{f' for {html.escape(str(date))}' if date else ''}"

    body = "".join(_slot_html(slot, meals[slot]) for slot in ("breakfast", "lunch", "dinner") if slot in meals)
    if not body:
        body = '<p style="color:#777">This plan has no meals in it yet.</p>'

    button = (
        f'<p style="margin:26px 0 0">'
        f'<a href="{html.escape(share_url)}" '
        f'style="display:inline-block;background:#a6b52b;color:#fff;text-decoration:none;'
        f'padding:11px 18px;border-radius:8px;font-size:14px;font-weight:600">'
        f'Open the plan</a></p>'
        f'<p style="font-size:11px;color:#999;margin:10px 0 0">'
        f'Anyone with this link can see the plan. It shows the food only — no '
        f'names, ages or dietary details travel with it.</p>'
        if share_url else ""
    )

    html_body = (
        '<div style="margin:0;padding:24px;background:#f6f6f4;'
        'font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Helvetica,Arial,sans-serif">'
        '<div style="max-width:560px;margin:0 auto;background:#fff;border-radius:14px;padding:28px">'
        f'<p style="font-size:14px;color:#555;margin:0 0 4px">{intro}</p>'
        f'<h1 style="font-size:20px;color:#111;margin:0 0 22px;font-weight:650">{heading}</h1>'
        f"{body}{button}"
        '<p style="font-size:11px;color:#aaa;margin:26px 0 0;border-top:1px solid #eee;padding-top:14px">'
        'Sent by WiseFood because you asked for this plan by email.</p>'
        "</div></div>"
    )

    lines = [intro.replace("&#x27;", "'"), "", heading, ""]
    for slot in ("breakfast", "lunch", "dinner"):
        if slot not in meals:
            continue
        lines.append(slot.upper())
        items = meals[slot] if isinstance(meals[slot], list) else [meals[slot]]
        for dish in items:
            if isinstance(dish, dict):
                lines.append(f"  - {dish.get('title') or 'Untitled'}")
        lines.append("")
    if share_url:
        lines += ["Open the plan:", share_url, ""]
    lines.append("Sent by WiseFood because you asked for this plan by email.")

    return html_body, "\n".join(lines)
