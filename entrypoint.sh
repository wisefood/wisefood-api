#!/bin/bash
set -eu

SCHEMA_FILE="/app/schemas/10_init_schema.sql"

# If called with "init-db" or env INITIALIZE_DB=1, run the schema against Postgres
if [ "${1:-}" = "init-db" ] || [ "${INITIALIZE_DB:-}" = "1" ]; then
    if ! command -v psql >/dev/null 2>&1; then
        echo "[WISEFOOD] psql not found in PATH" >&2
        exit 2
    fi

    if [ ! -f "$SCHEMA_FILE" ]; then
        echo "[WISEFOOD] Schema file not found: $SCHEMA_FILE" >&2
        exit 3
    fi

    # First use the root user to grant privileges for keycloak schema to wisefood user
    ROOT_URL="postgresql://${PG_ROOT_USER}:${PG_ROOT_PASSWORD}@${POSTGRES_HOST}/${POSTGRES_DB}"
    echo "[WISEFOOD] Granting privileges to user ${POSTGRES_USER}..."
    psql "$ROOT_URL" "-v" "ON_ERROR_STOP=on" "-c" "GRANT ALL PRIVILEGES ON SCHEMA ${KEYCLOAK_SCHEMA} TO ${POSTGRES_USER};"
    psql "$ROOT_URL" "-v" "ON_ERROR_STOP=on" "-c" "GRANT SELECT, REFERENCES ON ALL TABLES IN SCHEMA ${KEYCLOAK_SCHEMA} TO ${POSTGRES_USER};"
    
    # Wait 10 seconds to ensure that the Postgres server is ready
    echo "[WISEFOOD] Waiting for Postgres server to be ready..."
    sleep 10

    URL="postgresql://${POSTGRES_USER}:${POSTGRES_PASSWORD}@${POSTGRES_HOST}/${POSTGRES_DB}"
    echo "[WISEFOOD] Initializing database from $SCHEMA_FILE..."
    for sql_file in /app/schemas/*.sql; do
        if [ -f "$sql_file" ]; then  
            echo "[WISEFOOD] Executing $sql_file..."
            psql "$URL" "-v" "ON_ERROR_STOP=on" "-f" "$sql_file"
            
            if [ $? -ne 0 ]; then
                echo "[WISEFOOD] Error executing $sql_file"
                exit 1
            fi
        else
            echo "[WISEFOOD] No SQL files found in /app/schemas."
        fi
    done

    echo "[WISEFOOD] Database initialization finished."
    exit 0
else
    echo "[WISEFOOD] Starting application server..."
    exec /bin/sh -c 'uvicorn main:api --host 0.0.0.0 --port ${PORT:-8000}'
fi
