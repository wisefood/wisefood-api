"""How the inactivity reminders are configured.

The whole configuration is one value in the admin settings table
(``analytics.settings``, key ``notifications.inactivity``) — the same table
that holds maintenance mode, for the same reasons: an admin-only write,
validation, an audit trail of who changed it, and thirty-second propagation to
every replica, none of which is worth building twice. It is one object rather
than nine keys so that a change of plan ("every 21 days, at most 3") lands
atomically and reads as one line in the audit trail.

Three modes:

``off``
    Nothing is sent. Activity is still recorded, so switching on later starts
    from real data instead of from the moment it was switched on.
``experimental``
    The real cycle runs — same people, same timing, same mails — but every
    reminder goes to the test recipients (the admins) instead, with a banner
    naming who it would have gone to. Nothing reaches a participant, and a
    participant's own reminder history is untouched by it.
``live``
    Reminders go to participants.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List

SETTING_KEY = "notifications.inactivity"
KIND = "inactivity_reminder"
MODES = ("off", "experimental", "live")

#: Defaults. ``off`` until an admin decides otherwise: a deployment must never
#: start writing to participants because it was upgraded.
DEFAULT_CONFIG: Dict[str, Any] = {
    "mode": "off",
    # Days without using the platform before the first reminder.
    "inactive_after_days": 14,
    # Days between reminders while the person stays away.
    "repeat_every_days": 14,
    # Reminders per stretch of inactivity. Coming back resets the count.
    "max_reminders": 2,
    # The UTC hour the daily cycle runs at (09 UTC is 10–12 local across the
    # pilot countries).
    "send_hour_utc": 9,
    # A cap per cycle, so a first run over a large backlog is spread over days
    # instead of arriving as one burst a mail provider reads as spam.
    "max_per_run": 50,
    # Where experimental mode delivers. Empty means the deployment's
    # ADMIN_EMAIL.
    "test_recipients": [],
    # Only write to addresses Keycloak has verified. An unverified address may
    # not belong to the account holder at all — a claimed guest account is
    # deliberately left unverified for that reason.
    "verified_only": True,
    # Admins and experts run the platform; they are not who this is for.
    "include_staff": False,
}

#: Inclusive bounds for the integer settings.
LIMITS: Dict[str, tuple] = {
    "inactive_after_days": (1, 365),
    "repeat_every_days": (1, 365),
    "max_reminders": (1, 10),
    "send_hour_utc": (0, 23),
    "max_per_run": (1, 1000),
}

MAX_TEST_RECIPIENTS = 10
_ADDRESS = re.compile(r"^[^@\s]+@[^@\s.]+\.[^@\s]+$")


def validate(value: Any) -> Dict[str, Any]:
    """Check an admin-supplied configuration and return it normalised.

    Strict like the rest of the settings table: a bool where a number belongs,
    or a number as a string, is refused rather than coerced. Keys left out keep
    their defaults, so a partial object cannot silently reset what it did not
    mention. Raises ValueError with a message fit for the API.
    """
    if not isinstance(value, dict):
        raise ValueError(f"'{SETTING_KEY}' must be an object")
    unknown = sorted(set(value) - set(DEFAULT_CONFIG))
    if unknown:
        raise ValueError(f"'{SETTING_KEY}' has unknown keys: {unknown}")

    merged: Dict[str, Any] = {**DEFAULT_CONFIG, **value}

    mode = merged["mode"]
    if mode not in MODES:
        raise ValueError(f"'mode' must be one of {list(MODES)}")

    for key, (low, high) in LIMITS.items():
        number = merged[key]
        if isinstance(number, bool) or not isinstance(number, int):
            raise ValueError(f"'{key}' must be a whole number")
        if not low <= number <= high:
            raise ValueError(f"'{key}' must be between {low} and {high}")

    for key in ("verified_only", "include_staff"):
        if not isinstance(merged[key], bool):
            raise ValueError(f"'{key}' must be true or false")

    recipients = merged["test_recipients"]
    if not isinstance(recipients, list):
        raise ValueError("'test_recipients' must be a list of email addresses")
    cleaned: List[str] = []
    for raw in recipients:
        if not isinstance(raw, str) or not _ADDRESS.match(raw.strip()):
            raise ValueError(f"'test_recipients' has an unusable address: {raw!r}")
        address = raw.strip().lower()
        if address not in cleaned:
            cleaned.append(address)
    if len(cleaned) > MAX_TEST_RECIPIENTS:
        raise ValueError(f"'test_recipients' may hold at most {MAX_TEST_RECIPIENTS} addresses")
    merged["test_recipients"] = cleaned
    return merged


def current() -> Dict[str, Any]:
    """The configuration in force, from the settings cache. Never raises.

    A stored value that no longer validates (a key removed in a later release,
    say) falls back to the defaults — that is, to ``off`` — rather than to a
    half-understood configuration that might write to people.
    """
    from analytics import SETTINGS

    stored = SETTINGS.get(SETTING_KEY, DEFAULT_CONFIG)
    try:
        return validate(stored)
    except ValueError:
        return dict(DEFAULT_CONFIG)


def effective_test_recipients(config: Dict[str, Any]) -> List[str]:
    """Where experimental mode delivers: the list, or the deployment's admin."""
    from main import config as app_config

    if config.get("test_recipients"):
        return list(config["test_recipients"])
    fallback = str(app_config.settings.get("ADMIN_EMAIL") or "").strip().lower()
    return [fallback] if _ADDRESS.match(fallback) else []
