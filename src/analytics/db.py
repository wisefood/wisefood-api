"""A connection pool that belongs to analytics alone.

The recorder writes from background tasks. Left on the pool the rest of the
gateway shares, those writes compete for connections with the requests they are
describing — and they compete hardest exactly when traffic is heaviest, which
is when a user can least afford to wait behind a batch of page views. At four
concurrent writers plus the correlator that is a sixth of the shared pool's
capacity, held by work nobody is waiting for.

So analytics gets its own, and it is deliberately small and hard-capped:
``max_overflow=0`` means it cannot grow under load. When every analytics
connection is busy the recorder waits, its bounded queue absorbs the delay, and
if the delay outlasts the queue it drops events and says so in its counters.
Every one of those outcomes is preferable to a person waiting for a page.

Reads are not routed here. A console report is somebody's request, served on
the request path, and belongs on the pool that serves requests.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)

_engine: Optional[Any] = None
_session_factory: Optional[Any] = None


def session_factory():
    """The analytics session factory, built on first use.

    Lazy because the engine binds to the running event loop, and because a
    deployment with analytics switched off should never open these connections
    at all.
    """
    global _engine, _session_factory
    if _session_factory is not None:
        return _session_factory

    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from analytics.recorder import _WRITER_CONCURRENCY
    from backend.postgres import PostgresConnectionSingleton

    try:
        url = PostgresConnectionSingleton._get_database_url(async_driver=True)
        _engine = create_async_engine(
            url,
            # One per writer, plus one for the correlator, and no overflow:
            # this pool exists to have a ceiling.
            pool_size=_WRITER_CONCURRENCY + 1,
            max_overflow=0,
            pool_pre_ping=True,
            # A recycled connection costs a background task nothing and avoids
            # the stale-connection errors that a long-idle pool collects
            # overnight, when analytics is the only thing still writing.
            pool_recycle=1800,
        )
        _session_factory = async_sessionmaker(_engine, expire_on_commit=False)
        logger.info(
            "analytics.pool_ready size=%d", _WRITER_CONCURRENCY + 1
        )
    except Exception:
        # A pool that cannot be built is not a reason to fail: fall back to the
        # shared one, which is how this worked before, and log it loudly enough
        # that somebody notices analytics is now competing for connections.
        logger.warning("analytics.pool_unavailable_using_shared", exc_info=True)
        from backend.postgres import POSTGRES_ASYNC_SESSION_FACTORY

        _session_factory = POSTGRES_ASYNC_SESSION_FACTORY()
    return _session_factory


async def close() -> None:
    """Release the analytics connections. Called on shutdown."""
    global _engine, _session_factory
    engine = _engine
    _engine = None
    _session_factory = None
    if engine is not None:
        try:
            await engine.dispose()
        except Exception:
            logger.debug("analytics.pool_dispose_failed", exc_info=True)
