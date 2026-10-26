"""The inactivity reminder, in the four languages the platform speaks.

Written in the language the person last used the app in (recorded with their
activity), falling back to English. The tone is the app's own: informal,
second person, and short — it is a nudge, not a newsletter. The three app
descriptions follow the dashboard's (``dashboard.apps.*`` in the console's
locale files) so the mail and the page it opens describe the same things in
the same words.

Two rules the copy keeps on purpose:

* **Never claim more than is known.** When an account's last visit predates
  activity tracking, nobody knows how long it has been, so the mail says
  "a while" instead of a number.
* **No gendered verbs.** Slovenian and Greek past tenses agree with the
  subject's gender, which an account does not record, so those sentences are
  built around the visit ("your last visit was ...") rather than the person.
"""
from __future__ import annotations

import html
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import email_layout
from email_layout import Button, Email

#: (app name, accent colour) in the order they are listed — the dashboard's.
APPS: Tuple[Tuple[str, str], ...] = (
    ("FoodChat", "#a35ece"),
    ("RecipeWrangler", "#a6b52b"),
    ("FoodScholar", "#d53355"),
)

COPY: Dict[str, Dict[str, object]] = {
    "en": {
        "subject": "Your WiseFood is still here",
        "subject_named": "{name}, your WiseFood is still here",
        "preheader": "Plan a week of meals, find a recipe that fits, or ask a food question — it only takes a minute.",
        "title": "It's been a while",
        "greeting": "Hi there,",
        "greeting_named": "Hi {name},",
        "lead_days": "Your last visit to WiseFood was {days} ago.",
        "lead_unknown": "It has been a while since your last visit to WiseFood.",
        "lead_rest": "Your profile, your preferences and everything you saved are just where you left them. Here is what you can pick up again:",
        "apps": (
            "Plan personalised meals for the week, around what you like.",
            "Find and adapt recipes that fit your diet.",
            "Ask, learn and discover — with answers that show their sources.",
        ),
        "button": "Open WiseFood",
        "why": "You are receiving this because you have a WiseFood account that has not been used for a while. We send only a few of these, and none once you are back.",
        "turn_off": "Turn off these reminders",
        "manage": "Email settings",
        "banner_experimental": "Experimental mode — this reminder would have gone to {recipient} ({name}). While experimental mode is on, only admins receive reminders.",
        "banner_test": "Test send from the console — a sample of the inactivity reminder.",
    },
    "hu": {
        "subject": "Még mindig vár rád a WiseFood",
        "subject_named": "{name}, még mindig vár rád a WiseFood",
        "preheader": "Tervezz heti menüt, találj hozzád illő receptet, vagy tegyél fel egy kérdést az étkezésről – csak egy perc.",
        "title": "Rég láttunk",
        "greeting": "Szia!",
        "greeting_named": "Szia {name}!",
        "lead_days": "Utoljára {days} jártál a WiseFoodon.",
        "lead_unknown": "Már egy ideje nem jártál a WiseFoodon.",
        "lead_rest": "A profilod, a beállításaid és minden, amit elmentettél, ott vár, ahol hagytad. Ezeket folytathatod:",
        "apps": (
            "Tervezz személyre szabott étkezéseket a hétre, az ízlésed szerint.",
            "Keress és adaptálj az étrendedhez illő recepteket.",
            "Kérdezz, tanulj és fedezz fel – a válaszok a forrásaikat is megmutatják.",
        ),
        "button": "WiseFood megnyitása",
        "why": "Azért kapod ezt az üzenetet, mert van WiseFood-fiókod, amelyet egy ideje nem használtál. Csak néhány ilyen emlékeztetőt küldünk, és amint visszatérsz, egyet sem.",
        "turn_off": "Emlékeztetők kikapcsolása",
        "manage": "E-mail-beállítások",
        "banner_experimental": "Kísérleti mód – ez az emlékeztető ide ment volna: {recipient} ({name}). Amíg a kísérleti mód be van kapcsolva, csak az adminisztrátorok kapnak emlékeztetőt.",
        "banner_test": "Tesztküldés a konzolból – minta az inaktivitási emlékeztetőből.",
    },
    "sl": {
        "subject": "Tvoj WiseFood te še vedno čaka",
        "subject_named": "{name}, tvoj WiseFood te še vedno čaka",
        "preheader": "Načrtuj tedenske obroke, poišči recept, ki ti ustreza, ali postavi vprašanje o prehrani – vzame le minuto.",
        "title": "Dolgo se nismo videli",
        "greeting": "Živjo!",
        "greeting_named": "Živjo, {name}!",
        "lead_days": "Tvoj zadnji obisk WiseFooda je bil pred {days}.",
        "lead_unknown": "Od tvojega zadnjega obiska WiseFooda je minilo že nekaj časa.",
        "lead_rest": "Tvoj profil, tvoje nastavitve in vse, kar je shranjeno, je še vedno tam. Tole lahko nadaljuješ:",
        "apps": (
            "Načrtuj personalizirane obroke za ves teden, po svojem okusu.",
            "Poišči in prilagodi recepte, ki ustrezajo tvoji prehrani.",
            "Vprašaj, uči se in odkrivaj – odgovori pokažejo tudi svoje vire.",
        ),
        "button": "Odpri WiseFood",
        "why": "To sporočilo dobivaš, ker imaš račun WiseFood, ki že nekaj časa ni bil v uporabi. Pošljemo le nekaj takih opomnikov in nobenega več, ko se vrneš.",
        "turn_off": "Izklopi te opomnike",
        "manage": "Nastavitve e-pošte",
        "banner_experimental": "Poskusni način – ta opomnik bi bil poslan na {recipient} ({name}). Dokler je poskusni način vklopljen, opomnike prejemajo le skrbniki.",
        "banner_test": "Testno pošiljanje iz konzole – vzorec opomnika o neaktivnosti.",
    },
    "el": {
        "subject": "Το WiseFood σε περιμένει",
        "subject_named": "{name}, το WiseFood σε περιμένει",
        "preheader": "Σχεδίασε τα γεύματα της εβδομάδας, βρες μια συνταγή που σου ταιριάζει ή κάνε μια ερώτηση για τη διατροφή – χρειάζεται μόνο ένα λεπτό.",
        "title": "Πάει καιρός",
        "greeting": "Γεια σου,",
        "greeting_named": "Γεια σου {name},",
        "lead_days": "Η τελευταία σου επίσκεψη στο WiseFood ήταν πριν από {days}.",
        "lead_unknown": "Έχει περάσει καιρός από την τελευταία σου επίσκεψη στο WiseFood.",
        "lead_rest": "Το προφίλ σου, οι προτιμήσεις σου και ό,τι έχεις αποθηκεύσει είναι εκεί που τα άφησες. Μπορείς να συνεχίσεις από εδώ:",
        "apps": (
            "Σχεδίασε εξατομικευμένα γεύματα για την εβδομάδα, με βάση τις προτιμήσεις σου.",
            "Βρες και προσάρμοσε συνταγές που ταιριάζουν στη διατροφή σου.",
            "Ρώτα, μάθε και ανακάλυψε – οι απαντήσεις δείχνουν και τις πηγές τους.",
        ),
        "button": "Άνοιξε το WiseFood",
        "why": "Λαμβάνεις αυτό το μήνυμα επειδή έχεις λογαριασμό στο WiseFood που δεν έχει χρησιμοποιηθεί εδώ και λίγο καιρό. Στέλνουμε μόνο λίγες τέτοιες υπενθυμίσεις και καμία μόλις επιστρέψεις.",
        "turn_off": "Απενεργοποίηση αυτών των υπενθυμίσεων",
        "manage": "Ρυθμίσεις email",
        "banner_experimental": "Πειραματική λειτουργία – αυτή η υπενθύμιση θα πήγαινε στο {recipient} ({name}). Όσο είναι ενεργή η πειραματική λειτουργία, υπενθυμίσεις λαμβάνουν μόνο οι διαχειριστές.",
        "banner_test": "Δοκιμαστική αποστολή από την κονσόλα – δείγμα της υπενθύμισης αδράνειας.",
    },
}


def days_phrase(locale: str, days: int) -> str:
    """"16 days" in the case each language's sentence needs.

    en: "... was 16 days ago" · hu: "... 16 napja" (no plural after a number)
    · sl: "pred 16 dnevi" (instrumental: dnem / dnevoma / dnevi)
    · el: "πριν από 16 ημέρες" (accusative: ημέρα / ημέρες)
    """
    n = max(int(days), 1)
    if locale == "hu":
        return f"{n} napja"
    if locale == "sl":
        rest = n % 100
        word = "dnem" if rest == 1 else "dnevoma" if rest == 2 else "dnevi"
        return f"{n} {word}"
    if locale == "el":
        return f"{n} ημέρα" if n == 1 else f"{n} ημέρες"
    return f"{n} day" if n == 1 else f"{n} days"


def clean_name(value: Optional[str]) -> str:
    """A first name fit for a subject line: one line, sensible length."""
    name = " ".join(str(value or "").split())
    return name[:40]


@dataclass
class Reminder:
    subject: str
    email: Email


def render_reminder(
    *,
    locale: Optional[str],
    first_name: Optional[str],
    days_inactive: Optional[int],
    app_url: str,
    unsubscribe_url: str,
    settings_url: str,
    banner: Optional[str] = None,
) -> Reminder:
    """The reminder for one person.

    ``days_inactive`` is None when the last visit is not actually known.
    ``banner`` is the experimental or test notice, already in the reader's
    (the admin's) language; it is shown above the card's content.
    """
    lang = email_layout.normalize_locale(locale)
    copy = COPY[lang]
    name = clean_name(first_name)

    subject = (copy["subject_named"] if name else copy["subject"]).format(name=name)
    greeting = (copy["greeting_named"] if name else copy["greeting"]).format(name=name)
    lead = (
        copy["lead_days"].format(days=days_phrase(lang, days_inactive))
        if days_inactive is not None else copy["lead_unknown"]
    )
    lead = f"{lead} {copy['lead_rest']}"

    descriptions: List[str] = list(copy["apps"])  # type: ignore[arg-type]
    rows = []
    for (app, colour), text in zip(APPS, descriptions):
        rows.append(
            '<tr><td style="padding:0 0 10px;">'
            '<table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" '
            'style="border-collapse:separate;">'
            f'<tr><td style="padding:12px 14px;background:#faf7f2;border-radius:10px;'
            f'border-left:3px solid {colour};font-family:{email_layout.FONT};font-size:14px;'
            f'line-height:1.5;color:{email_layout.TEXT};">'
            f'<strong style="color:{email_layout.HEADING};">{html.escape(app)}</strong><br>'
            f"{html.escape(text)}</td></tr></table></td></tr>"
        )
    apps_html = (
        '<table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" '
        'style="margin:6px 0 4px;">' + "".join(rows) + "</table>"
    )
    apps_text = "\n".join(f"- {app}: {text}" for (app, _), text in zip(APPS, descriptions))

    body_html = email_layout.paragraph(greeting) + email_layout.paragraph(lead) + apps_html
    body_text = f"{greeting}\n\n{lead}\n\n{apps_text}"

    rendered = email_layout.render(
        locale=lang,
        title=str(copy["title"]),
        preheader=str(copy["preheader"]),
        body_html=body_html,
        body_text=body_text,
        button=Button(str(copy["button"]), app_url),
        banner_html=html.escape(banner) if banner else "",
        banner_text=banner or "",
        footer_note=str(copy["why"]),
        footer_links=((str(copy["turn_off"]), unsubscribe_url), (str(copy["manage"]), settings_url)),
        home_url=app_url,
    )
    return Reminder(subject=subject, email=rendered)


def banner_text(kind: str, *, locale: str = "en", recipient: str = "", name: str = "") -> str:
    """The admin-facing notice on an experimental or test send."""
    copy = COPY[email_layout.normalize_locale(locale)]
    if kind == "experimental":
        return str(copy["banner_experimental"]).format(recipient=recipient, name=name or "—")
    return str(copy["banner_test"])
