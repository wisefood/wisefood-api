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

**One look.** Every body is wrapped in `email_layout` — the logo, the card,
the consortium footer — which the Keycloak email theme mirrors, so a
participant's verification mail, a shared plan and a reminder read as the
same sender. The frame's images travel inline; `send_email` attaches them
whenever the HTML refers to them.
"""
from __future__ import annotations

import html
import logging
import re
import smtplib
import time
from email import policy as email_policy
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from starlette.concurrency import run_in_threadpool

import email_layout
from email_layout import Button, InlineImage

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


def build_message(
    *,
    to: str,
    subject: str,
    html_body: str,
    text_body: str,
    reply_to: Optional[str] = None,
    images: Optional[Sequence[InlineImage]] = None,
    unsubscribe_url: Optional[str] = None,
    headers: Optional[Mapping[str, str]] = None,
) -> EmailMessage:
    """The MIME message `send_email` would send. Split out so it can be tested.

    Structure: multipart/alternative of the plain text and a
    multipart/related holding the HTML and its inline images, which is the
    shape every client agrees on for ``cid:`` references.
    """
    from main import config

    settings = config.settings
    # 7-bit transfer encoding for every body: Hungarian, Slovene and Greek text
    # otherwise goes out as raw 8-bit, which most servers accept and a few
    # relays still mangle. Quoted-printable or base64 survives all of them.
    message = EmailMessage(policy=email_policy.default.clone(cte_type="7bit"))
    message["Subject"] = subject
    message["From"] = formataddr(
        (settings.get("SMTP_FROM_NAME") or "WiseFood", settings["SMTP_FROM"])
    )
    message["To"] = to
    message["Date"] = formatdate(localtime=True)
    message["Message-ID"] = make_msgid(domain=_sender_domain(settings["SMTP_FROM"]))
    if reply_to:
        message["Reply-To"] = reply_to
    # Never a reply-worthy human message, so an out-of-office does not answer it.
    message["Auto-Submitted"] = "auto-generated"
    if unsubscribe_url:
        # RFC 2369 + RFC 8058: mail a person did not ask for, one at a time,
        # carries a one-click way out that the mail client can show next to
        # the sender. Gmail and Yahoo require it of anything bulk-shaped.
        # A transactional mail (a plan someone asked to receive) passes none:
        # offering an unsubscribe we do not honour is worse than offering none.
        message["List-Unsubscribe"] = f"<{unsubscribe_url}>"
        message["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"
    for name, value in (headers or {}).items():
        message[name] = value

    message.set_content(text_body)
    message.add_alternative(html_body, subtype="html")

    if images is None and "cid:wf-" in html_body:
        images = email_layout.brand_images()
    if images:
        html_part = message.get_payload()[1]
        for image in images:
            html_part.add_related(
                image.data,
                maintype="image",
                subtype=image.subtype,
                cid=f"<{image.cid}>",
                filename=image.filename,
                disposition="inline",
            )
    # Serialise headers with the 998-character limit RFC 5322 allows rather
    # than the library's 78. A URL has no spaces to fold at, and at 78 the
    # library rewrote List-Unsubscribe as an RFC 2047 encoded word — which no
    # mail client reads as a link, so one-click unsubscribe silently vanished.
    # The bodies were already encoded above under the default policy, so they
    # stay 7-bit safe; only header folding changes.
    message.policy = message.policy.clone(max_line_length=998)
    return message


def _sender_domain(address: str) -> Optional[str]:
    _, _, domain = (address or "").rpartition("@")
    return domain or None


async def send_email(
    *,
    to: str,
    subject: str,
    html_body: str,
    text_body: str,
    reply_to: Optional[str] = None,
    images: Optional[Sequence[InlineImage]] = None,
    unsubscribe_url: Optional[str] = None,
    headers: Optional[Mapping[str, str]] = None,
) -> bool:
    """Send one mail. Returns False rather than raising.

    A failed send must not fail the request that asked for it — the plan is
    saved either way, and the caller gets to say "we could not send that"
    instead of a 500 that suggests the plan is gone too.
    """
    if not enabled():
        logger.info("mail.disabled subject=%r", subject[:60])
        return False
    if not valid_address(to):
        logger.info("mail.bad_address")
        return False

    message = build_message(
        to=to,
        subject=subject,
        html_body=html_body,
        text_body=text_body,
        reply_to=reply_to,
        images=images,
        unsubscribe_url=unsubscribe_url,
        headers=headers,
    )

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
    font = email_layout.FONT
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
            f'<div style="margin:0 0 14px;">'
            f'<div style="font-family:{font};font-size:15px;font-weight:600;color:{email_layout.HEADING};">{title}</div>'
            + (f'<div style="font-family:{font};font-size:12px;color:{email_layout.MUTED};margin-top:2px;">{meta}</div>' if meta else "")
            + (f'<div style="font-family:{font};font-size:13px;color:{email_layout.TEXT};margin-top:6px;line-height:1.5;">{ingredients}</div>' if ingredients else "")
            + "</div>"
        )
    if not rows:
        return ""
    return (
        f'<div style="margin:0 0 16px;padding:14px 16px 2px;border-radius:12px;background:{email_layout.PANEL_BG};'
        f'border:1px solid {email_layout.CARD_BORDER};border-left:3px solid {email_layout.BRAND_GREEN};">'
        f'<div style="font-family:{font};font-size:11px;letter-spacing:.08em;text-transform:uppercase;'
        f'color:{email_layout.MUTED};margin:0 0 8px;">{html.escape(slot)}</div>'
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

    The plan's own markup is a single column of slots; the frame around it is
    `email_layout`'s. `payload` is the scrubbed share payload, so there is
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
        f"{from_name} shared a meal plan with you."
        if from_name else "Here is your meal plan."
    )
    heading = f"Meal plan{f' for {date}' if date else ''}"

    slots = "".join(_slot_html(slot, meals[slot]) for slot in ("breakfast", "lunch", "dinner") if slot in meals)
    if not slots:
        slots = email_layout.paragraph("This plan has no meals in it yet.", muted=True)

    lines = [intro, ""]
    for slot in ("breakfast", "lunch", "dinner"):
        if slot not in meals:
            continue
        lines.append(slot.upper())
        items = meals[slot] if isinstance(meals[slot], list) else [meals[slot]]
        for dish in items:
            if isinstance(dish, dict):
                lines.append(f"  - {dish.get('title') or 'Untitled'}")
        lines.append("")

    link_note = (
        "Anyone with this link can see the plan. It shows the food only — no "
        "names, ages or dietary details travel with it."
    )
    rendered = email_layout.render(
        locale="en",
        title=heading,
        preheader=intro,
        body_html=email_layout.paragraph(intro) + slots,
        body_text="\n".join(lines),
        button=Button("Open the plan", share_url) if share_url else None,
        after_button_html=email_layout.paragraph(link_note, muted=True, size=12) if share_url else "",
        after_button_text=link_note if share_url else "",
        footer_note="Sent by WiseFood because you asked for this plan by email.",
        home_url=app_url(),
    )
    return rendered.html, rendered.text


def app_url(path: str = "") -> str:
    """The console's public address, for links in mail. ``path`` is appended."""
    from main import config

    base = str(config.settings.get("APP_EXT_DOMAIN") or "").rstrip("/")
    return f"{base}/app{path}"


def api_url(path: str = "") -> str:
    """This gateway's public address, for links a mail client calls directly."""
    from main import config

    base = str(config.settings.get("APP_EXT_DOMAIN") or "").rstrip("/")
    root = str(config.settings.get("CONTEXT_PATH") or "").rstrip("/")
    return f"{base}{root}{path}"
