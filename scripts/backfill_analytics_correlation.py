#!/usr/bin/env python3
"""Link activity recorded before session correlation existed. Run once.

The recorder links new rows to their browser session and their person within
a minute, but only looks back two days. Rows FoodChat, FoodScholar and
RecipeWrangler reported before that change shipped stay unlinked, so their
sessions show no plans, questions or searches. This runs the same statements
over a longer window until a pass fills nothing.

    ANALYTICS_BACKFILL_DAYS=365 python scripts/backfill_analytics_correlation.py

Safe to re-run: every statement touches only rows that are still blank.
"""
import asyncio
import logging
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import main  # noqa: E402,F401  (builds config and the engine)
import logsys  # noqa: E402

logsys.configure()
logger = logging.getLogger("analytics.backfill")

#: Larger than the timer's batch: this runs once, off-hours, and a small batch
#: would mean hundreds of passes that each re-read the window.
BATCH = 50_000


async def run() -> int:
    days = max(1, min(int(os.getenv("ANALYTICS_BACKFILL_DAYS", "365")), 365))

    from analytics import db as analytics_db
    from analytics.correlate import resolve_identities

    try:
        passes = 0
        while True:
            passes += 1
            filled = await resolve_identities(hours=days * 24, batch=BATCH)
            if any(count < 0 for count in filled.values()):
                print(f"Pass {passes} failed on {filled}; see the log", file=sys.stderr)
                return 1
            print(f"Pass {passes}: {filled}")
            if not sum(filled.values()):
                return 0
    finally:
        # The statements run on the analytics pool, not the shared one.
        await analytics_db.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
