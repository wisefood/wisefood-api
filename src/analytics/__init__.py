"""Activity analytics for the WiseFood gateway.

The gateway is the only tier every request already passes through, and the only
one that can name the user, so it is where activity is recorded. Services and
clients that know something the gateway cannot see post it back through the
ingest endpoints in ``routers/analytics.py``.

Everything here is inert unless ``ANALYTICS_ENABLED`` is true. See
``schemas/50_analytics.sql`` for the storage and the reasoning behind it.
"""

from analytics.recorder import (  # noqa: F401
    RECORDER,
    ActivityRecorder,
    app_for_route,
    normalize_query,
    query_hash,
)
from analytics.settings import (  # noqa: F401
    APPS,
    CONSENT,
    CONSENT_GRANT,
    CONSENT_OPT_OUT,
    DEFAULTS,
    SETTINGS,
    consent_mode,
)

__all__ = [
    "RECORDER",
    "ActivityRecorder",
    "SETTINGS",
    "CONSENT",
    "APPS",
    "DEFAULTS",
    "CONSENT_GRANT",
    "CONSENT_OPT_OUT",
    "consent_mode",
    "app_for_route",
    "normalize_query",
    "query_hash",
]
