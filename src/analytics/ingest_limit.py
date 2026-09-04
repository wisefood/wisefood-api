"""A ceiling on how much one caller may report.

Authentication answers "is this somebody"; it does not answer "should somebody
be allowed to send this much". Every ingest endpoint required a valid token
from the start, and the platform's existing budget only ever applied to guest
accounts — a signed-in user was unlimited. That is the wrong shape for this
particular set of endpoints, because the cost of an event is not CPU, it is
rows on a shared 5Gi volume that the whole platform lives on. Filling it is an
outage for everything, not just for analytics.

The queue and the writer pool already bound the *transient* cost: a flood is
dropped rather than buffered, and analytics writes come from their own capped
connection pool so a burst cannot hold a connection a page is waiting for.
What neither bounds is the durable cost, which is what this does.

Counted in rows rather than requests, because that is what is actually
expensive: one request may carry two hundred interactions or one page view.

Deliberately generous. A real browser buffers for five seconds and flushes
what it has; the limit below is roughly a hundred rows a second sustained,
which no honest client approaches and no dishonest one gets past.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Dict, Optional, Tuple

logger = logging.getLogger(__name__)


def _limit(name: str, default: int) -> int:
    try:
        return max(1, int(os.getenv(name, "") or default))
    except ValueError:
        return default


#: Sustained rows per minute, per identity.
ROWS_PER_MINUTE = _limit("ANALYTICS_INGEST_ROWS_PER_MINUTE", 6_000)
#: How much may arrive at once after a quiet spell. A tab that was closed for
#: an hour comes back and flushes its buffer; that is legitimate and should not
#: be refused.
BURST_ROWS = _limit("ANALYTICS_INGEST_BURST_ROWS", 12_000)
#: Distinct identities tracked in one process. Beyond this the least recently
#: seen are forgotten, which is the correct failure: forgetting a bucket lets
#: someone through, and the alternative is a dictionary that grows until the
#: pod dies — a worse outcome than the one being prevented.
MAX_TRACKED = 20_000


class IngestLimiter:
    """A token bucket per identity, refilled continuously.

    Process-local on purpose. A Redis counter would be exact across replicas,
    but this sits on an ingest path that must not acquire a network round trip
    per request, and the platform's existing Redis budget *fails open* when
    Redis is unavailable — which is precisely when a limiter is most needed.
    Local buckets are approximate across N replicas (a caller gets up to N
    times the limit if perfectly load-balanced) and always present. For a
    ceiling whose job is to stop a volume filling, approximate and always-on
    beats exact and sometimes-absent.
    """

    __slots__ = ("_buckets", "_rate", "_burst")

    def __init__(self, rows_per_minute: int = ROWS_PER_MINUTE, burst: int = BURST_ROWS):
        # subject -> (tokens remaining, last refill time)
        self._buckets: Dict[str, Tuple[float, float]] = {}
        self._rate = rows_per_minute / 60.0
        self._burst = float(max(burst, rows_per_minute))

    def check(self, subject: Optional[str], rows: int) -> Tuple[bool, int]:
        """Spend `rows` from this subject's bucket.

        Returns (allowed, retry_after_seconds). An unidentified caller is not
        rate limited here — it cannot reach these endpoints without a token, so
        there is no such caller in practice, and inventing a shared bucket for
        one would let any single client throttle everybody.
        """
        if not subject or rows <= 0:
            return True, 0

        now = time.monotonic()
        tokens, last = self._buckets.get(subject, (self._burst, now))
        tokens = min(self._burst, tokens + (now - last) * self._rate)

        if tokens < rows:
            # Refused: the bucket is not charged, so a client that keeps
            # hammering does not push its own recovery further away.
            self._buckets[subject] = (tokens, now)
            deficit = rows - tokens
            return False, max(1, int(deficit / self._rate) + 1)

        self._buckets[subject] = (tokens - rows, now)
        self._evict_if_crowded(now)
        return True, 0

    def _evict_if_crowded(self, now: float) -> None:
        """Forget buckets that have refilled anyway.

        A full bucket carries no information — it is indistinguishable from a
        caller never seen before — so dropping it costs nothing.
        """
        if len(self._buckets) < MAX_TRACKED:
            return
        full_again = [
            subject
            for subject, (tokens, last) in self._buckets.items()
            if min(self._burst, tokens + (now - last) * self._rate) >= self._burst
        ]
        for subject in full_again:
            self._buckets.pop(subject, None)
        if len(self._buckets) >= MAX_TRACKED:
            # Everyone is mid-spend. Clearing is the honest last resort: it
            # briefly lifts the ceiling for everybody, which is better than
            # refusing everybody or growing without bound.
            logger.warning("analytics.ingest_limiter_reset tracked=%d", len(self._buckets))
            self._buckets.clear()

    def tracked(self) -> int:
        return len(self._buckets)


LIMITER = IngestLimiter()
