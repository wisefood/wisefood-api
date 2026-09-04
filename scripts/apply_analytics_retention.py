#!/usr/bin/env python3
"""Delete activity older than the retention window.

Run from a CronJob. Deleting in batches rather than one statement so a first
run against a year of data does not hold one long transaction and a table lock
while it works.

    ANALYTICS_RETENTION_DAYS=365 python scripts/apply_analytics_retention.py

Feedback is deliberately never deleted: somebody took the trouble to write it,
and an expert may not have read it yet. Everything else — request records,
searches, model usage — ages out.
"""
import asyncio
import logging
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import main  # noqa: E402,F401  (builds config and the engine)
import logsys  # noqa: E402

logsys.configure()
logger = logging.getLogger("analytics.retention")


async def run() -> int:
    days = int(os.getenv("ANALYTICS_RETENTION_DAYS", "365"))
    if days <= 0:
        logger.info("retention.disabled days=%s", days)
        return 0

    from analytics.reports import apply_retention
    from backend.postgres import POSTGRES_ASYNC_ENGINE, PostgresConnectionSingleton

    POSTGRES_ASYNC_ENGINE()
    try:
        removed = await apply_retention(days)
        logger.info("retention.applied", extra={"days": days, **removed})
        print(f"Removed rows older than {days} days: {removed}")
        return 0
    except Exception as exc:
        logger.error("retention.failed: %s", exc, exc_info=True)
        print(f"Retention failed: {exc}", file=sys.stderr)
        return 1
    finally:
        await PostgresConnectionSingleton.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
