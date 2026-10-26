"""The one look every WiseFood email has.

Every mail the platform sends — a shared meal plan, an inactivity reminder, and
(through the Keycloak email theme in core-components) verification and
password-reset mail — goes out in this frame: the WiseFood logo on a white
page, a white card, one clear button, and the consortium footer
with the EU emblem. Before this, each sender styled its own body inline and
Keycloak sent the bare base theme, so a participant could not tell at a glance
that three mails came from the same place.

**The Keycloak theme mirrors this file.** It lives in
``core-components/keycloak/themes/wisefood/email/html/template.ftl`` and cannot
import Python, so a change to the frame — colours, spacing, footer — has to be
made in both. The palette below is the console's (``app/assets/css/main.css``).

Mail clients are not browsers, which explains the shape of the markup:

* Tables for layout and inline styles everywhere. Outlook renders with Word;
  a ``<div>`` layout or a stylesheet-only rule arrives as a heap.
* A "bulletproof" button: a coloured table cell around a padded link, so the
  whole thing is clickable and keeps its colour where background images and
  ``border-radius`` on links do not survive.
* Images are attached inline (``cid:``) rather than linked. Linked images are
  blocked by default in Outlook and several webmail clients, and the logo is
  the one thing that says who wrote the mail.
* A hidden preheader, the line a mail client shows after the subject.
"""
from __future__ import annotations

import datetime as _dt
import html
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

# --------------------------------------------------------------- palette --
BRAND = "#d53355"          # --brand, the console's primary
BRAND_DARK = "#aa2944"     # --color-brand-600, links on white
BRAND_GREEN = "#a6b52b"    # --brandg
# White page, neutral greys: no warm tints. Gmail's apps and Outlook ignore
# `color-scheme: light` and darken light colours in dark mode while keeping
# their hue, so the site's warm cream (#f6efe6) arrived as a brown page.
# Colourless greys darken to grey.
PAGE_BG = "#ffffff"
CARD_BORDER = "#e4e4e7"    # --ui-border (zinc-200)
PANEL_BG = "#f4f4f5"       # a tinted box inside the card (zinc-100)
HEADING = "#1d2125"
TEXT = "#3d4148"
MUTED = "#80858d"
FONT = (
    "-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,"
    "sans-serif"
)

#: Where the EU emblem links: the project's CORDIS page, as on the site footer.
CORDIS_URL = "https://cordis.europa.eu/project/id/101181895"

_ASSETS = Path(__file__).resolve().parent / "assets" / "email"


@dataclass(frozen=True)
class InlineImage:
    """An image attached to the mail and referenced as ``cid:<cid>``."""

    cid: str
    filename: str
    data: bytes
    subtype: str = "png"


def _asset(name: str) -> bytes:
    return (_ASSETS / name).read_bytes()


def brand_images() -> List[InlineImage]:
    """The logo and the EU emblem, read fresh so a test can swap the files."""
    return [
        InlineImage("wf-logo@wisefood", "wisefood-logo.png", _asset("wisefood-logo.png")),
        InlineImage("wf-eu@wisefood", "eu-emblem.png", _asset("eu-emblem.png")),
    ]


# ------------------------------------------------------------ the footer --
#: Fixed footer copy per language. Kept beside the frame because it is part of
#: it; the message-specific "why you got this" line is the caller's.
_FOOTER: Dict[str, Dict[str, str]] = {
    "en": {
        "consortium": "The WiseFood Consortium",
        "eu_alt": "Funded by the European Union",
        "fallback": "If the button does not work, copy this link into your browser:",
    },
    "hu": {
        "consortium": "A WiseFood konzorcium",
        "eu_alt": "Az Európai Unió finanszírozásával",
        "fallback": "Ha a gomb nem működik, másold be ezt a linket a böngésződbe:",
    },
    "sl": {
        "consortium": "Konzorcij WiseFood",
        "eu_alt": "Financira Evropska unija",
        "fallback": "Če gumb ne deluje, kopiraj to povezavo v brskalnik:",
    },
    "el": {
        "consortium": "Η Κοινοπραξία WiseFood",
        "eu_alt": "Χρηματοδοτείται από την Ευρωπαϊκή Ένωση",
        "fallback": "Αν το κουμπί δεν λειτουργεί, αντέγραψε αυτόν τον σύνδεσμο στον περιηγητή σου:",
    },
}

SUPPORTED_LOCALES: Tuple[str, ...] = tuple(_FOOTER)


def normalize_locale(value: Optional[str]) -> str:
    """``hu-HU`` -> ``hu``; anything unsupported -> ``en``."""
    code = (value or "").strip().lower().replace("_", "-").split("-")[0]
    return code if code in _FOOTER else "en"


@dataclass(frozen=True)
class Button:
    label: str
    url: str


@dataclass
class Email:
    """A rendered mail: both bodies and the images the HTML refers to."""

    html: str
    text: str
    images: List[InlineImage] = field(default_factory=list)


def _esc(value: object) -> str:
    return html.escape(str(value), quote=True)


def paragraph(text: str, *, muted: bool = False, size: int = 15) -> str:
    """One body paragraph in the frame's type. ``text`` is escaped."""
    colour = MUTED if muted else TEXT
    return (
        f'<p style="margin:0 0 14px;font-family:{FONT};font-size:{size}px;'
        f'line-height:1.6;color:{colour};">{_esc(text)}</p>'
    )


def button_html(button: Button) -> str:
    url, label = _esc(button.url), _esc(button.label)
    return (
        '<table role="presentation" cellspacing="0" cellpadding="0" border="0" '
        'style="margin:26px 0 10px;border-collapse:separate;">'
        f'<tr><td align="center" bgcolor="{BRAND}" style="border-radius:10px;background:{BRAND};">'
        f'<a href="{url}" target="_blank" rel="noopener" '
        f'style="display:inline-block;padding:13px 26px;font-family:{FONT};font-size:15px;'
        f'font-weight:600;line-height:1.2;color:#ffffff;text-decoration:none;border-radius:10px;">'
        f"{label}</a></td></tr></table>"
    )


def render(
    *,
    locale: str,
    title: str,
    preheader: str,
    body_html: str,
    body_text: str,
    button: Optional[Button] = None,
    after_button_html: str = "",
    after_button_text: str = "",
    banner_html: str = "",
    banner_text: str = "",
    footer_note: str = "",
    footer_links: Sequence[Tuple[str, str]] = (),
    home_url: Optional[str] = None,
) -> Email:
    """Wrap a message in the WiseFood frame.

    ``body_html`` and ``after_button_html`` are trusted markup the caller has
    already escaped (``paragraph`` does that); every other string argument is
    plain text and is escaped here. ``footer_links`` are (label, url) pairs
    shown under the footer note, e.g. "Turn off these reminders".
    """
    lang = normalize_locale(locale)
    copy = _FOOTER[lang]
    year = _dt.date.today().year

    logo = (
        f'<img src="cid:wf-logo@wisefood" width="210" alt="WiseFood" '
        f'style="display:block;border:0;outline:none;width:210px;max-width:210px;height:auto;">'
    )
    if home_url:
        logo = f'<a href="{_esc(home_url)}" target="_blank" rel="noopener" style="text-decoration:none;">{logo}</a>'

    button_block = ""
    if button:
        button_block = button_html(button) + (
            f'<p style="margin:0 0 18px;font-family:{FONT};font-size:12px;line-height:1.5;color:{MUTED};">'
            f'{_esc(copy["fallback"])}<br>'
            f'<a href="{_esc(button.url)}" style="color:{MUTED};word-break:break-all;">{_esc(button.url)}</a></p>'
        )

    links = " &nbsp;·&nbsp; ".join(
        f'<a href="{_esc(url)}" target="_blank" rel="noopener" style="color:{MUTED};text-decoration:underline;">{_esc(label)}</a>'
        for label, url in footer_links
    )

    banner = (
        f'<tr><td style="padding:14px 40px;background:#fff7e6;border-bottom:1px solid #f3dfb5;'
        f'border-radius:16px 16px 0 0;font-family:{FONT};font-size:13px;line-height:1.5;color:#7a5410;">'
        f"{banner_html}</td></tr>"
        if banner_html else ""
    )

    html_doc = f"""<!doctype html>
<html lang="{lang}" xmlns="http://www.w3.org/1999/xhtml">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="x-apple-disable-message-reformatting">
<meta name="color-scheme" content="light only">
<meta name="supported-color-schemes" content="light only">
<title>{_esc(title)}</title>
<style>
  :root {{ color-scheme: light only; }}
  body {{ margin:0; padding:0; background:{PAGE_BG}; }}
  a {{ color:{BRAND_DARK}; }}
  @media (max-width:620px) {{
    .wf-outer {{ padding:20px 8px !important; }}
    .wf-pad {{ padding:28px 22px !important; }}
  }}
</style>
</head>
<body style="margin:0;padding:0;background:{PAGE_BG};">
<div style="display:none;max-height:0;max-width:0;overflow:hidden;opacity:0;mso-hide:all;color:{PAGE_BG};font-size:1px;line-height:1px;">{_esc(preheader)}&#8199;&#847;&#8199;&#847;&#8199;&#847;&#8199;&#847;&#8199;&#847;&#8199;&#847;</div>
<table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" style="background:{PAGE_BG};">
<tr><td class="wf-outer" align="center" style="padding:32px 12px;">
<table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" style="max-width:600px;">
<tr><td align="center" style="padding:0 0 24px;">{logo}</td></tr>
<tr><td style="background:#ffffff;border:1px solid {CARD_BORDER};border-radius:16px;">
<table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0">
{banner}<tr><td class="wf-pad" style="padding:36px 40px 30px;font-family:{FONT};font-size:15px;line-height:1.6;color:{TEXT};">
<h1 style="margin:0 0 18px;font-family:{FONT};font-size:22px;line-height:1.3;font-weight:600;color:{HEADING};">{_esc(title)}</h1>
{body_html}{button_block}{after_button_html}
</td></tr>
</table>
</td></tr>
<tr><td align="center" style="padding:26px 20px 0;font-family:{FONT};font-size:12px;line-height:1.6;color:{MUTED};">
{f'<p style="margin:0 0 8px;">{_esc(footer_note)}</p>' if footer_note else ''}{f'<p style="margin:0 0 14px;">{links}</p>' if links else ''}<p style="margin:0 0 14px;">&copy; {year} {_esc(copy["consortium"])}</p>
<a href="{CORDIS_URL}" target="_blank" rel="noopener" style="text-decoration:none;"><img src="cid:wf-eu@wisefood" width="150" alt="{_esc(copy["eu_alt"])}" style="display:inline-block;border:0;outline:none;width:150px;height:auto;"></a>
</td></tr>
</table>
</td></tr>
</table>
</body>
</html>"""

    text_parts: List[str] = []
    if banner_text:
        text_parts += [banner_text, ""]
    text_parts += [title, "=" * min(len(title), 60), "", body_text.strip(), ""]
    if button:
        text_parts += [f"{button.label}:", button.url, ""]
    if after_button_text:
        text_parts += [after_button_text.strip(), ""]
    text_parts.append("--")
    if footer_note:
        text_parts.append(footer_note)
    for label, url in footer_links:
        text_parts.append(f"{label}: {url}")
    text_parts.append(f"(c) {year} {copy['consortium']} · {copy['eu_alt']}")

    return Email(html=html_doc, text="\n".join(text_parts) + "\n", images=brand_images())
