import os
from fastapi import FastAPI
from routers.generic import install_error_handler
import uvicorn
import logsys


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
        self.settings["CACHE_ENABLED"] = (
            os.getenv("CACHE_ENABLED", "false").lower() == "true"
        )
        self.settings["REDIS_HOST"] = os.getenv("REDIS_HOST", "redis")
        self.settings["REDIS_PORT"] = int(os.getenv("REDIS_PORT", 6379))


# Configure application settings
config = Config()
config.setup()

# Configure logging
logsys.configure()


# create FastAPI app
api = FastAPI(
    title="WiseFood API",
    version="0.0.1",
    root_path=config.settings["CONTEXT_PATH"],
)

# Initiliaze exception handlers
install_error_handler(api)


if __name__ == "__main__":
    # Run Uvicorn programmatically using the configuration
    uvicorn.run(
        "main:api",
        host=config.settings["HOST"],
        port=config.settings["PORT"],
        reload=config.settings["DEBUG"],
    )