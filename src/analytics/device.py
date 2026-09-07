"""Reading a device off a request, without a dependency and without an address.

Two jobs, both small and both easy to get subtly wrong.

**The user agent.** Every parser library is a pile of regexes that goes stale;
this is a smaller pile that goes stale more slowly, because it only tries to
answer the three questions a console asks — which browser, which OS, is it a
phone — and gives up honestly on anything else rather than guessing. Order
matters throughout: Edge claims to be Chrome, Chrome claims to be Safari, and
almost everything claims to be Mozilla, so the checks run most-specific first.

**The address.** The full IP is never returned. IPv4 keeps three octets and
IPv6 keeps its first three groups, which is enough to see one broken office
network and not enough to identify a household. There is no flag to widen it:
the useful part of an address for this purpose is the network, and the part
that makes it personal data is the part being dropped.
"""

from __future__ import annotations

import ipaddress
import logging
import os
import re
from typing import Dict, Optional, Tuple

logger = logging.getLogger(__name__)

#: A user agent is attacker-controlled text that ends up in a database column
#: and, eventually, on a page. Length-capped here rather than at the column.
MAX_USER_AGENT = 512

# Checked in order. The first match wins, so a browser that impersonates
# another must appear before the one it impersonates.
_BROWSERS: Tuple[Tuple[str, str], ...] = (
    ("Edge", r"Edg(?:e|A|iOS)?/([\d.]+)"),
    ("Opera", r"OPR/([\d.]+)"),
    ("Samsung Internet", r"SamsungBrowser/([\d.]+)"),
    ("Vivaldi", r"Vivaldi/([\d.]+)"),
    ("Brave", r"Brave/([\d.]+)"),
    ("Firefox", r"(?:Firefox|FxiOS)/([\d.]+)"),
    # Chrome on iOS is CriOS and is really Safari's engine, but the person
    # chose Chrome and that is what a support conversation will call it.
    ("Chrome", r"(?:Chrome|CriOS|Chromium)/([\d.]+)"),
    ("Safari", r"Version/([\d.]+).*Safari"),
    ("Internet Explorer", r"(?:MSIE |rv:)([\d.]+).*Trident"),
)

_OPERATING_SYSTEMS: Tuple[Tuple[str, str], ...] = (
    # iPadOS reports as Macintosh from iPadOS 13, and is separated below by
    # touch support rather than by string, which is why iOS is checked first.
    ("iOS", r"(?:iPhone|iPad|iPod).*?OS ([\d_]+)"),
    ("Android", r"Android ([\d.]+)"),
    ("Windows", r"Windows NT ([\d.]+)"),
    ("macOS", r"Mac OS X ([\d_.]+)"),
    ("Chrome OS", r"CrOS \S+ ([\d.]+)"),
    ("Ubuntu", r"Ubuntu"),
    ("Linux", r"Linux"),
)

#: Windows reports a kernel version nobody recognises. The console shows what
#: the person would call it.
_WINDOWS_NAMES = {
    "10.0": "10/11",
    "6.3": "8.1",
    "6.2": "8",
    "6.1": "7",
}

_BOT = re.compile(
    r"bot|crawler|spider|crawling|slurp|curl/|wget/|python-requests|okhttp|"
    r"headless|lighthouse|pingdom|uptime|monitor|scrapy|axios/|go-http-client",
    re.I,
)
_MOBILE = re.compile(r"Mobile|iPhone|iPod|Android.*Mobile|Windows Phone", re.I)
_TABLET = re.compile(r"iPad|Tablet|PlayBook|Silk|Android(?!.*Mobile)", re.I)


def parse_user_agent(raw: Optional[str]) -> Dict[str, Optional[object]]:
    """Browser, OS and form factor from a user agent string.

    Never raises and never returns a partially-filled guess: a string it does
    not recognise comes back with None for every field, which a report can show
    as "unknown" honestly instead of filing under whatever matched loosest.
    """
    result: Dict[str, Optional[object]] = {
        "browser": None,
        "browser_version": None,
        "os": None,
        "os_version": None,
        "device_type": None,
        "is_bot": False,
    }
    if not raw:
        return result
    agent = str(raw)[:MAX_USER_AGENT]

    if _BOT.search(agent):
        # Named but not analysed. A crawler's "browser" is noise in every
        # report, and the useful fact is only that it was not a person.
        result["is_bot"] = True
        result["device_type"] = "bot"
        return result

    for name, pattern in _BROWSERS:
        match = re.search(pattern, agent)
        if match:
            result["browser"] = name
            result["browser_version"] = _major(match)
            break

    for name, pattern in _OPERATING_SYSTEMS:
        match = re.search(pattern, agent)
        if not match:
            continue
        version = _major(match)
        if name == "Windows" and version:
            # The captured group is the NT version, so the lookup uses the
            # full match rather than the major number alone.
            raw_version = match.group(1)
            version = _WINDOWS_NAMES.get(raw_version, raw_version)
        result["os"] = name
        result["os_version"] = (version or "").replace("_", ".") or None
        break

    if _MOBILE.search(agent):
        result["device_type"] = "mobile"
    elif _TABLET.search(agent):
        result["device_type"] = "tablet"
    elif result["os"]:
        result["device_type"] = "desktop"
    return result


def _major(match: "re.Match") -> Optional[str]:
    """The first two version components. A patch number is never asked about."""
    try:
        version = match.group(1)
    except IndexError:
        return None
    if not version:
        return None
    parts = re.split(r"[._]", version)
    return ".".join(parts[:2])[:24]


#: Whether the whole address is kept.
#:
#: Off by default, and this is the one setting here with a real consequence.
#: An IP address identifies a household and is personal data under GDPR; a /24
#: prefix is enough to spot one broken office network and not enough to name
#: anybody. Turning it on is a deliberate choice by the platform's controller,
#: and it belongs in the DPIA — it does not change what is *collected*, only
#: how precisely it is kept.
#:
#: It also cannot be applied retroactively: rows written while this was off
#: hold a prefix, and the rest of the address is gone.
KEEP_FULL_IP = (os.getenv("ANALYTICS_KEEP_FULL_IP", "").strip().lower()
                in ("1", "true", "yes"))


def truncate_ip(raw: Optional[str]) -> Optional[str]:
    """The address, or the network it is on, depending on ANALYTICS_KEEP_FULL_IP.

    With the flag off — the default — IPv4 keeps its first three octets and
    IPv6 its first three groups, returned in a form that reads as deliberately
    incomplete (`81.4.127.0/24`) so nobody downstream mistakes it for an
    address. With it on, the address is stored as given.
    """
    if not raw:
        return None
    candidate = str(raw).strip()
    if not candidate:
        return None
    # An X-Forwarded-For carries the whole chain; the client is the first one.
    candidate = candidate.split(",")[0].strip()
    # A bracketed IPv6 with a port, or an IPv4 with a port.
    if candidate.startswith("["):
        candidate = candidate[1:].split("]", 1)[0]
    elif candidate.count(":") == 1:
        candidate = candidate.split(":", 1)[0]
    try:
        address = ipaddress.ip_address(candidate)
    except ValueError:
        return None
    if address.is_loopback or address.is_unspecified:
        return None
    if KEEP_FULL_IP:
        return str(address)
    try:
        if address.version == 4:
            return str(ipaddress.ip_network(f"{address}/24", strict=False))
        return str(ipaddress.ip_network(f"{address}/48", strict=False))
    except ValueError:
        return None


#: Headers an ingress or CDN uses to state the caller's country. Read only —
#: nothing here resolves an address to a place itself, because that needs a geo
#: database this platform does not ship, and a wrong country is worse than none.
_COUNTRY_HEADERS = (
    "cf-ipcountry",
    "x-geo-country",
    "x-country-code",
    "x-appengine-country",
)


def country_from_headers(headers) -> Optional[str]:
    """A two-letter country from whatever the ingress stamped, or None."""
    for name in _COUNTRY_HEADERS:
        value = headers.get(name)
        if not value:
            continue
        code = str(value).strip().upper()
        # "XX" and "T1" are what Cloudflare sends for unknown and for Tor.
        if len(code) == 2 and code.isalpha() and code not in ("XX", "T1"):
            return code
    return None


#: A local MaxMind GeoLite2 database, if one is mounted.
#:
#: Local on purpose. The obvious way to get a country is to call a free lookup
#: API, and that sends every visitor's IP address to a third party — which is a
#: transfer of personal data to another processor, needing a legal basis and a
#: line in the DPIA, to learn a two-letter code. A GeoLite2 file answers the
#: same question in-process, offline, for free.
#:
#: Falls back to nothing when the file is absent, so this is opt-in by mounting
#: it and setting GEOIP_DB_PATH.
GEOIP_DB_PATH = os.getenv("GEOIP_DB_PATH", "").strip()
_geoip_reader = None
_geoip_tried = False


def country_from_ip(raw: Optional[str]) -> Optional[str]:
    """A two-letter country for an address, from the local database.

    Returns None when no database is mounted, when the library is not
    installed, or when the address is not in it — all of which are ordinary,
    and none of which is worth failing a request over. The ingress header is
    always preferred; this is the fallback for a cluster whose ingress does not
    set one.
    """
    global _geoip_reader, _geoip_tried

    if not raw or not GEOIP_DB_PATH:
        return None
    if not _geoip_tried:
        _geoip_tried = True
        try:
            import geoip2.database

            _geoip_reader = geoip2.database.Reader(GEOIP_DB_PATH)
        except Exception:
            # Not installed, or the file is not where the variable says.
            logger.info("analytics.geoip_unavailable path=%s", GEOIP_DB_PATH)
            _geoip_reader = None
    if _geoip_reader is None:
        return None

    candidate = str(raw).split(",")[0].strip()
    if candidate.startswith("["):
        candidate = candidate[1:].split("]", 1)[0]
    elif candidate.count(":") == 1:
        candidate = candidate.split(":", 1)[0]
    try:
        code = _geoip_reader.country(candidate).country.iso_code
        return code.upper() if code else None
    except Exception:
        # A private address, or one the database does not cover.
        return None


def client_ip(scope, headers) -> Optional[str]:
    """The caller's address as the platform sees it, before truncation.

    Behind an ingress the socket address is the ingress, so the forwarded
    header is preferred. That header is client-settable when nothing strips it,
    which is why the result only ever reaches storage truncated.
    """
    forwarded = headers.get("x-forwarded-for") or headers.get("x-real-ip")
    if forwarded:
        return forwarded
    client = scope.get("client")
    return client[0] if client else None
