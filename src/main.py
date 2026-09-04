import asyncio
import os
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from routers.generic import install_error_handler
from contextlib import asynccontextmanager
from sqlalchemy import text
import uvicorn
import context
import logsys
import logging
from middleware import RequestContextMiddleware


logger = logging.getLogger(__name__)
origins = [
    "https://wisefood.gr",
    "http://localhost:3000",
    "https://dev.wisefood.gr"
]

# Configuration context
class Config:
    def __init__(self):
        self.settings = {}

    def setup(self):
        # Read environment variables and store them in the settings dictionary
        self.settings["HOST"] = os.getenv("HOST", "127.0.0.1")
        self.settings["PORT"] = int(os.getenv("PORT", 8000))
        self.settings["DEBUG"] = os.getenv("DEBUG", "true").lower() == "true"
        self.settings["CONTEXT_PATH"] = os.getenv("CONTEXT_PATH", "")
        self.settings["APP_EXT_DOMAIN"] = os.getenv("APP_EXT_DOMAIN", "http://wisefood.gr")
        self.settings["ELASTIC_HOST"] = os.getenv(
            "ELASTIC_HOST", "http://elasticsearch:9200"
        )
        self.settings["ES_DIM"] = int(os.getenv("ES_DIM", 384))
        self.settings["FOODSCHOLAR_URL"] = os.getenv("FOODSCHOLAR_URL", "http://foodscholar:8001")
        self.settings["RECIPEWRANGLER_URL"] = os.getenv("RECIPEWRANGLER_URL", "http://recipewrangler:8001")
        self.settings["FOODCHAT_URL"] = os.getenv("FOODCHAT_URL", "http://foodchat:8000")
        self.settings["LANGFUSE_BASE_URL"] = os.getenv("LANGFUSE_BASE_URL", "http://langfuse-web.langfuse.svc.cluster.local:3000")
        self.settings["LANGFUSE_PUBLIC_KEY"] = os.getenv("LANGFUSE_PUBLIC_KEY", "")
        self.settings["LANGFUSE_SECRET_KEY"] = os.getenv("LANGFUSE_SECRET_KEY", "")
        self.settings["MINIO_ENDPOINT"] = os.getenv(
            "MINIO_ENDPOINT", "http://minio:9000"
        )
        self.settings["MINIO_ROOT"] = os.getenv("MINIO_ROOT", "root")
        self.settings["MINIO_ROOT_PASSWORD"] = os.getenv(
            "MINIO_ROOT_PASSWORD", "minioadmin"
        )
        self.settings["MINIO_EXT_URL_CONSOLE"] = os.getenv(
            "MINIO_EXT_URL_CONSOLE", "https://s3.wisefood.gr/console"
        )
        self.settings["MINIO_EXT_URL_API"] = os.getenv(
            "MINIO_EXT_URL_API", "https://s3.wisefood.gr"
        )
        self.settings["MINIO_BUCKET"] = os.getenv("MINIO_BUCKET", "system")
        self.settings["KEYCLOAK_URL"] = os.getenv(
            "KEYCLOAK_URL", "http://keycloak:8080"
        )
        self.settings["KEYCLOAK_EXT_URL"] = os.getenv(
            "KEYCLOAK_EXT_URL", "https://auth.wisefood.gr"
        )
        self.settings["KEYCLOAK_ISSUER_URL"] = os.getenv(
            "KEYCLOAK_ISSUER_URL", "https://auth.wisefood.gr/realms/master"
        )
        self.settings["KEYCLOAK_REALM"] = os.getenv("KEYCLOAK_REALM", "master")
        self.settings["KEYCLOAK_CLIENT_ID"] = os.getenv(
            "KEYCLOAK_CLIENT_ID", "wisefood-api"
        )
        self.settings["KEYCLOAK_CLIENT_SECRET"] = os.getenv(
            "KEYCLOAK_CLIENT_SECRET", "secret"
        )
        self.settings["KEYCLOAK_POOL_SIZE"] = int(os.getenv("KEYCLOAK_POOL_SIZE", 5))
        self.settings["KEYCLOAK_AUDIENCES"] = [
            aud.strip()
            for aud in os.getenv(
                "KEYCLOAK_AUDIENCES", "master-realm,account"
            ).split(",")
            if aud.strip()
        ]
        # Ephemeral guest access. Guests are real Keycloak users prefixed
        # 'guest-' carrying the 'guest' realm role and an expiry attribute;
        # the reaper deletes them (and their household) after GUEST_TTL_SECONDS.
        self.settings["GUEST_ENABLED"] = (
            os.getenv("GUEST_ENABLED", "true").lower() == "true"
        )
        self.settings["GUEST_TTL_SECONDS"] = int(
            os.getenv("GUEST_TTL_SECONDS", 24 * 3600)
        )
        self.settings["GUEST_MAX_ACTIVE"] = int(os.getenv("GUEST_MAX_ACTIVE", 200))
        # Synthetic, never-delivered address domain for guest accounts —
        # the realm requires an email on every user.
        self.settings["GUEST_EMAIL_DOMAIN"] = os.getenv(
            "GUEST_EMAIL_DOMAIN", "guests.wisefood.gr"
        )
        self.settings["GUEST_REAPER_INTERVAL_SECONDS"] = int(
            os.getenv("GUEST_REAPER_INTERVAL_SECONDS", 600)
        )
        self.settings["CACHE_ENABLED"] = (
            os.getenv("CACHE_ENABLED", "false").lower() == "true"
        )
        self.settings["REDIS_HOST"] = os.getenv("REDIS_HOST", "redis")
        self.settings["REDIS_PORT"] = int(os.getenv("REDIS_PORT", 6379))
        self.settings["IMAGE_CACHE_REDIS_DB"] = int(
            os.getenv("IMAGE_CACHE_REDIS_DB", 3)
        )
        self.settings["IMAGE_CACHE_MAX_ITEMS"] = int(
            os.getenv("IMAGE_CACHE_MAX_ITEMS", 300)
        )
        self.settings["IMAGE_CACHE_MAX_BYTES"] = int(
            os.getenv("IMAGE_CACHE_MAX_BYTES", 2 * 1024 * 1024)
        )
        self.settings["IMAGE_CACHE_TTL_SECONDS"] = int(
            os.getenv("IMAGE_CACHE_TTL_SECONDS", 7 * 24 * 3600)
        )
        self.settings["POSTGRES_HOST"] = os.getenv("POSTGRES_HOST", "localhost")
        self.settings["POSTGRES_PORT"] = int(os.getenv("POSTGRES_PORT", 5432))
        self.settings["POSTGRES_USER"] = os.getenv("POSTGRES_USER", "postgres")
        self.settings["POSTGRES_PASSWORD"] = os.getenv("POSTGRES_PASSWORD", "postgres")
        self.settings["POSTGRES_DB"] = os.getenv("POSTGRES_DB", "wisefood")
        self.settings["POSTGRES_POOL_SIZE"] = int(os.getenv("POSTGRES_POOL_SIZE", 10))
        self.settings["POSTGRES_MAX_OVERFLOW"] = int(
            os.getenv("POSTGRES_MAX_OVERFLOW", 20)
        )
        # --- Observability / analytics -----------------------------------
        # `text` (default) keeps the human-readable stdout this service has
        # always produced; `json` emits one object per line and, unlike the text
        # formatter, preserves the `extra={...}` fields the code already passes.
        self.settings["LOG_FORMAT"] = os.getenv("LOG_FORMAT", "text").strip().lower()
        # The platform-wide analytics switch. Off means: no activity events are
        # recorded, the ingest endpoints accept and discard, and the console's
        # usage pages report that collection is disabled. Nothing else changes —
        # correlation ids and structured logs are not analytics and stay on.
        self.settings["ANALYTICS_ENABLED"] = (
            os.getenv("ANALYTICS_ENABLED", "false").lower() == "true"
        )
        # One INFO line per completed request, in addition to uvicorn's access
        # log, carrying the route, status, duration and caller. Off by default:
        # it doubles log volume, and it is only useful where logs are collected.
        self.settings["REQUEST_LOG_ENABLED"] = (
            os.getenv("REQUEST_LOG_ENABLED", "false").lower() == "true"
        )


# Configure application settings
config = Config()
config.setup()

# Configure logging
logsys.configure()


async def _guest_reaper_loop():
    """Periodically delete expired guest accounts and their data."""
    import guests

    interval = config.settings["GUEST_REAPER_INTERVAL_SECONDS"]
    while True:
        try:
            reaped = await guests.reap_expired_guests()
            if reaped:
                logger.info("Guest reaper: removed %d expired guest(s)", reaped)
        except Exception:
            logger.warning("Guest reaper iteration failed", exc_info=True)
        await asyncio.sleep(interval)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # --- STARTUP ---
    logger.info("App startup: warming up DB")
    from backend.postgres import POSTGRES_ASYNC_ENGINE
    eng = POSTGRES_ASYNC_ENGINE()
    async with eng.connect() as conn:
        await conn.execute(text("SELECT 1"))
    logger.info("Database connection OK")

    # The activity recorder owns a background task and a bounded queue. Started
    # here rather than at import so that a module import never spawns a task,
    # and inert unless ANALYTICS_ENABLED — see src/analytics/.
    from analytics import RECORDER, SETTINGS

    RECORDER.start(enabled=config.settings["ANALYTICS_ENABLED"])
    if config.settings["ANALYTICS_ENABLED"]:
        await SETTINGS.refresh_if_stale()

    reaper_task = None
    if config.settings["GUEST_ENABLED"]:
        reaper_task = asyncio.create_task(_guest_reaper_loop())
        logger.info(
            "Guest access enabled (TTL %ss, max %s active) — reaper running",
            config.settings["GUEST_TTL_SECONDS"],
            config.settings["GUEST_MAX_ACTIVE"],
        )

    # yield control to the application runtime
    yield

    # --- SHUTDOWN ---
    if reaper_task:
        reaper_task.cancel()
    # Drained before the DB pool closes, or the last events die with it.
    await RECORDER.stop()
    logger.info("App shutdown: closing DB connections")
    from backend.postgres import PostgresConnectionSingleton
    await PostgresConnectionSingleton.close()
    logger.info("DB connections closed")

# create FastAPI app
api = FastAPI(
    title="WiseFood API",
    version="0.0.1",
    root_path=config.settings["CONTEXT_PATH"],
    lifespan=lifespan,
)

api.add_middleware(
    CORSMiddleware,
    allow_origins=origins,            # list of allowed origins (or ["*"] for any origin)
    allow_credentials=True,           # set True if you send cookies / Authorization headers
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["*"],              # or list specific headers
    # X-Request-Id is exposed so the browser can read the correlation id off a
    # response and report it with a bug — without this the header arrives but
    # JavaScript cannot see it.
    expose_headers=["Content-Length", context.REQUEST_ID_HEADER],
)

# Assign a correlation id, resolve who is asking, and populate the
# RecipeWrangler proxy's identity context — once per request, for every route.
#
# RecipeWrangler does no authentication: it trusts this service to have verified
# the token and to say who the caller is. Doing this as middleware rather than
# per-route means a newly added proxied endpoint forwards identity by default
# instead of by someone remembering to.
#
# Resolution here is best-effort and never rejects: authorization remains the
# `Depends(auth(...))` on each route. An absent or unreadable token simply makes
# the downstream call anonymous, which RecipeWrangler handles by hiding creator
# attribution and withdrawn recipes.
#
# Added last, so it sits outermost: every response, CORS preflights included,
# carries the request id, and the id exists before any other layer can log.
api.add_middleware(RequestContextMiddleware)


# Initialize exception handlers
install_error_handler(api)

# Register routers
from routers.households import router as households_router
from routers.household_members import router as household_members_router
from routers.core import router as core_router
from routers.foodscholar import router as foodscholar_router
from routers.recipewrangler import router as recipewrangler_router
from routers.foodchat import router as foodchat_router
from routers.meal_plans import router as meal_plans_router
from routers.images import router as images_router
from routers.observability import router as observability_router
from routers.users import router as users_router
from routers.analytics import router as analytics_router

api.include_router(households_router)
api.include_router(household_members_router)
api.include_router(core_router)
api.include_router(foodscholar_router)
api.include_router(recipewrangler_router)
api.include_router(foodchat_router)
api.include_router(meal_plans_router)
api.include_router(images_router)
api.include_router(observability_router)
api.include_router(users_router)
api.include_router(analytics_router)

if __name__ == "__main__":
    # Run Uvicorn programmatically using the configuration
    uvicorn.run(
        "main:api",
        host=config.settings["HOST"],
        port=config.settings["PORT"],
        reload=config.settings["DEBUG"],
    )
