#!/usr/bin/env python3
"""Report what the guest reaper sees, without deleting anything.

Run inside the API pod (it needs the same env as the service):

    python3 scripts/diagnose_guests.py

Written because a reaper that finds nothing looks exactly like a reaper with
nothing to do. This prints the discrimination: how many guests exist, how each
one was identified, whether Keycloak actually stored the expiry attribute, and
what expiry the reaper would compute for it.

If `attribute stored: no` appears, the realm's declarative user profile is
rejecting unmanaged attributes (Keycloak 24+ default). Expiry then falls back to
createdTimestamp + GUEST_TTL_SECONDS, which works but ignores the exact TTL the
guest was minted with — allow unmanaged attributes in the realm user profile to
restore it.
"""
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import guests  # noqa: E402
from backend.keycloak import KEYCLOAK_ADMIN_CLIENT  # noqa: E402
from main import config  # noqa: E402


def main() -> int:
    now = int(time.time())
    print(f"GUEST_ENABLED            : {config.settings['GUEST_ENABLED']}")
    print(f"GUEST_TTL_SECONDS        : {config.settings['GUEST_TTL_SECONDS']}")
    print(f"GUEST_REAPER_INTERVAL_S  : {config.settings['GUEST_REAPER_INTERVAL_SECONDS']}")
    print()

    admin = KEYCLOAK_ADMIN_CLIENT()

    try:
        by_role = admin.get_realm_role_members(guests.GUEST_ROLE, query={"max": 1000}) or []
        print(f"users holding the '{guests.GUEST_ROLE}' realm role : {len(by_role)}")
    except Exception as exc:
        by_role = []
        print(f"realm-role lookup FAILED : {exc}")
        print("  → the 'guest' role may not exist, or the service account "
              "lacks view-users/query-users")

    try:
        by_name = admin.get_users(
            query={
                "username": guests.GUEST_USERNAME_PREFIX,
                "max": 1000,
                "briefRepresentation": False,
            }
        ) or []
        print(f"users matching username '{guests.GUEST_USERNAME_PREFIX}*'  : "
              f"{len([u for u in by_name if u.get('username', '').startswith(guests.GUEST_USERNAME_PREFIX)])}")
    except Exception as exc:
        by_name = []
        print(f"username search FAILED   : {exc}")

    found = guests._guest_users(max_results=1000)
    print(f"guests the reaper sees   : {len(found)}")
    print()

    if not found:
        print("Nothing to reap. If guests exist in Keycloak, compare the two "
              "counts above: a non-zero role/username count with zero here means "
              "the prefix filter is rejecting them.")
        return 0

    expired = 0
    undetermined = 0
    for user in found:
        attrs = user.get("attributes") or {}
        stored = attrs.get(guests.GUEST_EXPIRES_ATTRIBUTE)
        expires_at = guests._guest_expires_at(user)
        if expires_at is None:
            undetermined += 1
            verdict = "UNDETERMINED — will never be reaped"
        elif expires_at <= now:
            expired += 1
            verdict = f"EXPIRED {now - expires_at}s ago — would be deleted"
        else:
            verdict = f"alive for another {expires_at - now}s"
        print(f"  {user.get('username')}")
        print(f"    attribute stored: {'yes' if stored else 'no'}"
              f"{'' if stored else '  ← realm dropped it'}")
        print(f"    createdTimestamp: {user.get('createdTimestamp')}")
        print(f"    verdict         : {verdict}")

    print()
    print(f"summary: {len(found)} guest(s), {expired} expired, {undetermined} undetermined")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
