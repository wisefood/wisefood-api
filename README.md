# WiseFood API

<!-- Badges -->
[![Python](https://img.shields.io/badge/python-3.11-blue.svg)](https://www.python.org/downloads/release/python-3110/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115-009688.svg?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![Pydantic](https://img.shields.io/badge/Pydantic-2.9-e92063.svg?logo=pydantic&logoColor=white)](https://docs.pydantic.dev/)
[![PostgreSQL](https://img.shields.io/badge/PostgreSQL-async-336791.svg?logo=postgresql&logoColor=white)](https://www.postgresql.org/)
[![Redis](https://img.shields.io/badge/Redis-cache-DC382D.svg?logo=redis&logoColor=white)](https://redis.io/)
[![Keycloak](https://img.shields.io/badge/Keycloak-JWT-4d4d4d.svg?logo=keycloak&logoColor=white)](https://www.keycloak.org/)
[![MinIO](https://img.shields.io/badge/MinIO-S3-C72E49.svg?logo=minio&logoColor=white)](https://min.io/)
[![Langfuse](https://img.shields.io/badge/Langfuse-observability-000000.svg)](https://langfuse.com/)
[![License: Apache 2.0](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](LICENSE)
[![WiseFood EU](https://img.shields.io/badge/WiseFood-EU%20Project-2e7d32.svg)](https://wisefood-project.eu/)

The **WiseFood API** is the **core gateway** of the WiseFood platform — the single, authenticated entry point that every frontend and application talks to. It owns the user-facing domain (households, member profiles, meal plans, saved libraries, consent) in its own PostgreSQL database, and it **proxies** the platform's specialised services (FoodChat, FoodScholar, RecipeWrangler, the Data Catalog, Langfuse) behind one consistent contract: uniform authentication, a single response envelope, and one CORS/identity boundary.

Think of it as the **backend-for-frontend (BFF)** and **security perimeter** for the whole platform. Downstream services trust that a request arriving from this gateway has already been authenticated; clients trust that everything they need is reachable through one base URL with one token.

> **Interactive API reference (OpenAPI / Swagger UI):** available on any running instance at **`/docs`**, with the raw spec at **`/openapi.json`**.

---

## Table of Contents

- [What This Service Does](#what-this-service-does)
- [Platform Topology](#platform-topology)
- [High-Level Architecture](#high-level-architecture)
- [Request Lifecycle](#request-lifecycle)
- [The Response Envelope](#the-response-envelope)
- [Authentication & Authorization](#authentication--authorization)
- [Guest Access](#guest-access)
- [Data Stores](#data-stores)
- [Data Model (PostgreSQL)](#data-model-postgresql)
- [API Surface](#api-surface)
- [Observability](#observability)
- [GDPR / Data Erasure](#gdpr--data-erasure)
- [Repository Structure](#repository-structure)
- [Running the Service](#running-the-service)
- [Deployment](#deployment)
- [Environment Variables](#environment-variables)
- [Database Migrations](#database-migrations)
- [Testing](#testing)
- [License](#license)

---

## What This Service Does

The gateway plays two distinct roles at once:

**1. It owns a domain.** Households, household members and their nutrition profiles, meal plans, the per-member saved library (recipes + literature), and GDPR consent all live in **this** service's PostgreSQL database. These are served by a classic router → entity → SQLAlchemy stack.

**2. It fronts the platform.** FoodChat, FoodScholar, RecipeWrangler, and the Langfuse observability backend are separate services. The gateway exposes them under `/api/v1/{foodchat,foodscholar,recipewrangler,observability}/…`, forwarding calls over HTTP after verifying the caller. Downstream services do **not** re-authenticate — they trust the gateway to have done so and to tell them who the caller is.

The result for a client: **one host, one token, one response shape** for recipes, meal chat, scientific literature, image storage, member data, and platform metrics.

---

## Platform Topology

```
                         ┌──────────────────────────────────────────┐
   Browser / App  ─────► │            WiseFood API (this)            │
   (one token,           │  gateway · BFF · auth perimeter · CORS    │
    one base URL)        └───────┬───────────────────────┬──────────┘
                                 │ owns                   │ proxies (HTTP)
                    ┌────────────┴─────────────┐   ┌──────┴──────────────────────┐
                    ▼                          ▼   ▼            ▼            ▼
             PostgreSQL (wisefood)      FoodChat   FoodScholar   RecipeWrangler   Langfuse
             households, members,       meal-plan  scientific    recipes,         metrics /
             profiles, meal plans,      chat &     literature    search,          traces
             saved library, consent     sessions   Q&A / RAG     nutrition        (read-only)

   Cross-cutting infra:  Redis (cache · guest budgets)   MinIO / S3 (images)   Keycloak (identity)
```

- **FoodChat** — conversational meal-plan generation and session store.
- **FoodScholar** — scientific-literature Q&A and retrieval (RAG over the Data Catalog).
- **RecipeWrangler** — recipe search, nutrition profiling, and adaptation (Neo4j / Elasticsearch backed).
- **Data Catalog** ([`wisefood-data-api`](https://github.com/wisefood/wisefood-data-api)) — the editorial catalog of guides, articles, textbooks, and recipe collections that FoodScholar retrieves from. Saved literature in this service points at Data Catalog URNs (`urn:article:…`, `urn:guide:…`, `urn:textbook:…`).
- **Langfuse** — LLM observability; this service exposes a read-only metrics/traces proxy.

---

## High-Level Architecture

The service is organised into clear layers, each with a single responsibility:

| Layer | Path | Responsibility |
|-------|------|----------------|
| **App / lifecycle** | `src/main.py` | FastAPI setup, configuration, CORS, identity middleware, router registration, startup/shutdown, guest-reaper task |
| **Routers (HTTP)** | `src/routers/` | Route definitions, auth dependencies, request validation, response enveloping. **Never** touch the database directly. |
| **Entities (domain)** | `src/api/v1/` | Business logic and data orchestration for locally-owned domains (households, members, meal plans, images) |
| **Backends (infra)** | `src/backend/` | Adapters: PostgreSQL, Redis, Elasticsearch, MinIO, Keycloak, Langfuse, and the HTTP clients for FoodChat / FoodScholar / RecipeWrangler |
| **Schemas** | `src/schemas.py` | Pydantic request/response models and validators |
| **ORM** | `src/sql.py` | SQLAlchemy 2.0 async models for the `wisefood` schema |

### Two kinds of domain

The layering differs depending on whether the gateway **owns** the data or **proxies** it:

**Locally-owned** (e.g. households, meal plans, saved items):
```
router (src/routers/households.py)
   → entity (src/api/v1/households.py)
      → ORM (src/sql.py)  → PostgreSQL
```
The router authenticates and envelopes; the entity holds the business rules and ownership checks; the ORM persists.

**Proxied** (e.g. recipes, chat, literature):
```
router (src/routers/recipewrangler.py)
   → HTTP client (src/backend/recipewrangler.py)  → downstream service
```
The router authenticates and envelopes; the client forwards the call over HTTP and relays the downstream response (and errors) verbatim. There is no local database involved.

### Runtime dependencies

- **FastAPI + Uvicorn** — HTTP layer and OpenAPI docs
- **PostgreSQL** (via SQLAlchemy 2.0 async + asyncpg) — the gateway's own domain data
- **Redis** — caching (member profiles, downscaled images) and per-guest rate budgets
- **MinIO / S3** — image object storage
- **Keycloak** (via `python-keycloak` + `python-jose`) — JWT verification and RBAC
- **httpx** (HTTP/2) — the transport for all downstream proxying
- **Elasticsearch** client — available for catalog-adjacent reads
- **Langfuse** public API — read-only observability metrics

---

## Request Lifecycle

1. **CORS middleware** vets the origin (`src/main.py`).
2. **Identity middleware** best-effort-decodes the bearer token into a context variable, so proxied calls carry the caller's identity downstream by default (never rejects here — see below).
3. **Router** matches the path; its `Depends(auth(...))` dependency verifies the token and enforces role requirements.
4. For **owned** domains: the router calls an **entity** in `src/api/v1/`, which applies ownership/business rules and reads/writes PostgreSQL via `src/sql.py`.
   For **proxied** domains: the router calls a **backend HTTP client** in `src/backend/`, which forwards to the downstream service.
5. The **`@render()` decorator** wraps the result in the standard success envelope.
6. Any raised `APIException` is caught by the global handler and rendered as the standard error shape.

### Identity forwarding (zero-trust downstream)

Downstream services do not re-authenticate — but they learn *who* the caller is in two different ways. **RecipeWrangler** performs **no authentication of its own** and trusts identity **headers** from this gateway. To make that safe *and* automatic, the gateway sets identity as **middleware**, not per-route:

```python
# src/main.py — runs for every request
@api.middleware("http")
async def rw_identity_middleware(request, call_next):
    token = kutils.current_user(request)      # best-effort, never raises
    reset = CURRENT_TOKEN_PAYLOAD.set(token)
    ...
```

The RecipeWrangler client then attaches the caller's identity as headers (`X-User-Sub`, `X-User-Name`, `X-User-Roles`) derived from the **verified** token (`src/backend/recipewrangler.py`). Because it's middleware, a newly added proxied endpoint forwards identity by default instead of relying on someone remembering to. Authorization itself still lives in each route's `Depends(auth(...))`; the middleware decode is purely to *tell downstream who the caller is*, and an absent/unreadable token simply makes the downstream call anonymous.

**FoodChat** takes the opposite tack: routes pass an explicit `member_id`, and the gateway verifies the caller owns that member (`verify_member_access`) **before** forwarding — so FoodChat receives an already-authorized member id rather than trusting headers. Both clients map downstream errors back to typed exceptions, preserving the real upstream status instead of collapsing everything to 500.

---

## The Response Envelope

Every successful response shares one shape, produced by the `@render()` decorator (`src/routers/generic.py`):

```json
{
  "help": "https://demo.wisefood-project.eu/rest/api/v1/members/…",
  "success": true,
  "result": { }
}
```

- `help` — the request URL, for traceability.
- `success` — always `true` on this branch.
- `result` — the actual payload (object, list, or scalar).

Errors share a uniform shape, produced by the global exception handler:

```json
{
  "success": false,
  "error": {
    "detail": "…",
    "title": "NotFoundError",
    "code": "…"
  },
  "help": "<url>"
}
```

Application errors are raised as typed exceptions (`src/exceptions.py`), each mapping to an HTTP status:

| Exception | Status | | Exception | Status |
|-----------|:------:|-|-----------|:------:|
| `InvalidError` | 400 | | `ConflictError` | 409 |
| `AuthenticationError` | 401 | | `RateLimitError` | 429 |
| `AuthorizationError` | 403 | | `InternalError` | 500 |
| `NotFoundError` | 404 | | `BadGatewayError` | 502 |
| `NotAllowedError` | 405 | | `ServiceUnavailableError` | 503 |
| `DataError` (validation) | 422 | | `GatewayTimeoutError` | 504 |

FastAPI request-validation failures are normalised into a `DataError` (422) so clients see the same shape everywhere.

---

## Authentication & Authorization

Authentication is **Keycloak / OIDC bearer tokens**. Routes declare their requirements with the `auth()` dependency (`src/auth.py`):

```python
@router.get("/status", dependencies=[Depends(auth())])                 # any authenticated user
@router.post("/recipes/", dependencies=[Depends(auth("admin,expert"))]) # admin OR expert
```

- **`auth()`** — requires a valid token, no specific role.
- **`auth("admin,expert")`** — requires one of the listed roles (default `match="any"`; `match="all"` requires all).
- **`mode`** — `"local"` (verify the JWT signature locally, the default), `"introspect"` (call Keycloak's introspection endpoint), or `"both"`.

Roles are extracted (`_extract_roles`) from both `realm_access.roles` and `resource_access[client].roles`, lowercased and de-duplicated. Common roles across the platform:

| Role | Meaning |
|------|---------|
| `admin` | Full administrative access |
| `expert` | Editorial / curation privileges (e.g. managing recipes, catalog assets) |
| `agent` | Service-to-service callers (e.g. FoodChat acting on a member's behalf) |
| `guest` | Ephemeral, sandboxed access (see below) |

Token verification, JWKS handling, and the Keycloak admin client live in `src/kutils.py` and `src/backend/keycloak.py`.

---

## Guest Access

The platform supports **ephemeral guest accounts** for demos and conference booths — zero-friction access that self-destructs. `POST /api/v1/system/guest` mints a real, short-lived Keycloak user with the `guest` role and a pre-provisioned household + member, so the rest of the platform treats it like any isolated user (`src/guests.py`, `src/routers/core.py`).

- **TTL & reaper** — guests expire after `GUEST_TTL_SECONDS`; a background task (`_guest_reaper_loop` in `src/main.py`) periodically deletes expired guests and all their data.
- **On-demand erasure** — `DELETE /api/v1/system/guest` wipes the calling guest immediately (household, members, FoodChat sessions, and the Keycloak user) — useful between one booth visitor and the next, without waiting for the TTL.
- **Per-guest budgets** — expensive endpoints (chat, Q&A, search, sessions) are rate-limited per guest per day via Redis counters (`src/budget.py`), configurable through `GUEST_BUDGET_*`.
- **Creation is IP-rate-limited** to curb abuse.

Guest access is feature-flagged with `GUEST_ENABLED`; when off, the reaper does not run and the endpoints are inert.

---

## Data Stores

| Store | Adapter | Used for |
|-------|---------|----------|
| **PostgreSQL** | `src/backend/postgres.py` | The gateway's owned domain: households, members, profiles, meal plans, saved library, consent. SQLAlchemy 2.0 **async** (asyncpg), pooled singleton. |
| **Redis** | `src/backend/redis.py` | Member-profile cache (invalidated on write), downscaled image cache, per-guest rate budgets. Toggled with `CACHE_ENABLED`. |
| **MinIO / S3** | `src/backend/minio.py` | Member/recipe image object storage; serves external URLs. |
| **Elasticsearch** | `src/backend/elastic.py` | Catalog-adjacent reads where needed. |
| **Keycloak** | `src/backend/keycloak.py` | Identity: token verification, admin operations (guest provisioning, account deletion). |

> **Note:** The PostgreSQL connection layer uses a reentrant-locked singleton and warms the engine at startup (`lifespan` in `src/main.py`) before serving traffic.

---

## Data Model (PostgreSQL)

All tables live in the `wisefood` schema (`src/sql.py`, DDL in `schemas/`). The Keycloak user id (the token `sub`) is the anchor for ownership; household members are the anchor for personalization.

| Table | Purpose |
|-------|---------|
| `household` | A household, owned by a Keycloak user (`owner_id` → `keycloak.user_entity`, `ON DELETE SET NULL`) |
| `household_member` | A profile within a household (name, age group, avatar) |
| `household_member_profile` | Per-member nutrition profile: `nutritional_preferences` (JSONB — includes likes/dislikes, gender), `dietary_groups`, `allergies`, `properties` (JSONB — memory log, dietary goals, opt-outs) |
| `meal_plan` | A meal plan pinned to a calendar date (`applied_on`), with breakfast/lunch/dinner JSONB |
| `meal_plan_member` | Join table assigning a meal plan to one or more members |
| `saved_meal_plan` | A named, **undated** meal plan snapshot in a member's library (separate lifecycle from scheduled plans) |
| `member_favorite` | Legacy recipe-favourites table (retained for rollback; superseded by `member_saved_item`) |
| `member_saved_item` | **Typed library**: one row per `(member, item_type, item_ref)` — `recipe` (opaque RecipeWrangler id) or `article`/`guide`/`textbook` (Data Catalog URN). The `/favorites` endpoints are now a recipe-only view over this table. |
| `member_adapted_recipe` | A member's personal adapted version of a recipe (ingredient swap / reduced quantity) |
| `user_consent` | GDPR consent ledger keyed by Keycloak user id |

Enum types `age_groups` and `dietary_groups` are defined in the init schema.

---

## API Surface

All routes are under `/api/v1`. On the deployed platform the service also sits behind a `CONTEXT_PATH` of `/rest`, so the public prefix is `/rest/api/v1` (see [Deployment](#deployment)).

| Prefix | Router | Kind | Purpose |
|--------|--------|------|---------|
| `/api/v1/households` | `households.py` | owned | Household CRUD |
| `/api/v1/members` | `household_members.py`, `meal_plans.py` | owned | Members, profiles, favourites, **saved library**, adapted recipes, meal plans, saved plans |
| `/api/v1/users` | `users.py` | owned | Consent ledger; self-service account deletion (GDPR) |
| `/api/v1/images` | `images.py` | owned | Image upload / retrieval (MinIO) |
| `/api/v1/system` | `core.py` | core | Health (`/ping`, `/info`), login, guest access |
| `/api/v1/recipewrangler` | `recipewrangler.py` | proxy | Recipe search, details, profiling, adaptation, soft-delete |
| `/api/v1/foodchat` | `foodchat.py` | proxy | Conversational meal planning, sessions, memory |
| `/api/v1/foodscholar` | `foodscholar.py` | proxy | Scientific-literature Q&A / retrieval |
| `/api/v1/observability` | `observability.py` | proxy | Langfuse metrics / traces (read-only) |

Browse the live, exhaustive list at **`/docs`**.

---

## Observability

The service exposes a **read-only** window onto the platform's Langfuse instance (`src/backend/langfuse_read.py`, `src/routers/observability.py`):

- Metrics, time-series, and traces are fetched through Langfuse's public API and normalised (`src/backend/metrics_normalize.py`) for dashboard consumption.
- A batch `/observability/dashboard` endpoint aggregates several metrics in one call.
- The integration is **no-op when disabled** — if `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` are unset, the endpoints return empty results rather than erroring.

---

## GDPR / Data Erasure

`src/erasure.py` implements right-to-erasure. `purge_user(user_id)` deletes the user's households and members (which cascade to profiles, meal plans, saved items, favourites, adapted recipes), removes their FoodChat sessions downstream, and optionally deletes the Keycloak user itself.

Ordering is deliberate: the account deletion must not be blocked by a failure elsewhere, or the caller would be left logged into an account they asked to erase. The consent ledger (`user_consent`) is intentionally **retained** as a record that consent existed — the privacy notice covers this. Self-service deletion is wired at `DELETE /api/v1/users/me` (`src/routers/users.py`); guest erasure is the separate flow described above.

---

## Repository Structure

```text
.
├── README.md
├── Dockerfile
├── docker-compose.yml
├── Makefile
├── entrypoint.sh              # start server, or `init-db` to apply schemas/*.sql
├── requirements.txt
├── .env.example
├── pytest.ini
├── schemas/                   # ordered, idempotent SQL DDL (applied by init-db)
│   ├── 10_init_schema.sql
│   ├── 20_adapted_recipes.sql
│   ├── 30_meal_plan_library.sql
│   └── 40_saved_items.sql
├── scripts/
│   └── diagnose_guests.py
├── tests/
│   ├── test_guest_reaper.py
│   ├── test_meal_plan_library.py
│   ├── test_observability.py
│   ├── test_recipewrangler_identity.py
│   └── test_saved_items.py
└── src/
    ├── main.py                # app, config, lifespan, middleware, router registration
    ├── auth.py                # auth() dependency, role extraction, token verify
    ├── kutils.py              # Keycloak/JWT utilities
    ├── budget.py              # per-guest Redis rate budgets
    ├── guests.py              # ephemeral guest lifecycle
    ├── erasure.py             # GDPR right-to-erasure
    ├── exceptions.py          # typed APIException hierarchy → HTTP statuses
    ├── schemas.py             # Pydantic request/response models
    ├── sql.py                 # SQLAlchemy 2.0 async ORM (wisefood schema)
    ├── entity.py, utils.py, logsys.py
    ├── routers/               # HTTP layer (never touches the DB directly)
    │   ├── core.py            # health, login, guest
    │   ├── generic.py         # @render() envelope + global error handler
    │   ├── households.py
    │   ├── household_members.py
    │   ├── meal_plans.py
    │   ├── users.py
    │   ├── images.py
    │   ├── recipewrangler.py  # proxy
    │   ├── foodchat.py        # proxy
    │   ├── foodscholar.py     # proxy
    │   └── observability.py   # proxy
    ├── api/v1/                # entity layer for owned domains
    │   ├── households.py
    │   ├── household_members.py
    │   ├── meal_plans.py
    │   ├── images.py
    │   └── users.py
    └── backend/               # infrastructure + downstream HTTP clients
        ├── postgres.py
        ├── redis.py
        ├── elastic.py
        ├── minio.py
        ├── keycloak.py
        ├── langfuse_read.py
        ├── metrics_normalize.py
        ├── recipewrangler.py  # HTTP client (forwards identity headers)
        ├── foodchat.py        # HTTP client
        └── foodscholar.py     # HTTP client
```

---

## Running the Service

### Option 1: Docker Compose

The bundled `docker-compose.yml` starts the API and a Redis instance.

```bash
cp .env.example .env      # then edit values for your environment
docker compose up --build
```

> **Important:** PostgreSQL, Keycloak, MinIO, and the downstream services (FoodChat / FoodScholar / RecipeWrangler / Langfuse) are **not** provisioned by this Compose file. The API expects reachable endpoints for whichever of them you exercise, either from the wider WiseFood platform or run separately.

### Option 2: Local Python run

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cd src
uvicorn main:api --reload --host 0.0.0.0 --port 8000
```

You will still need reachable PostgreSQL, Redis, Keycloak, and MinIO services configured via environment variables.

### Building & publishing the image

```bash
make build     # docker build -t wisefood/wisefood-api:latest .
make push      # docker push wisefood/wisefood-api:latest
make all       # build + push
```

The image is Python 3.11, installs `postgresql-client` (for the `init-db` path), copies `src/` and `schemas/`, and runs `entrypoint.sh`.

---

## Deployment

On the WiseFood platform the service runs in Kubernetes (see `platform-deployment/lib/api.libsonnet`) as image `wisefood/wisefood-api:latest`. Key deployment facts:

- **Path prefix:** `CONTEXT_PATH=/rest`, so routes are served under `/rest/api/v1/…` and the public base URL is e.g. `https://demo.wisefood-project.eu/rest`.
- **In-cluster service discovery:** downstream URLs and infra hosts resolve to cluster service names — `foodscholar`, `recipewrangler`, `foodchat`, `elastic`, `minio`, `keycloak`, `redis`, and the shared `db` (PostgreSQL).
- **Secrets** (Keycloak client secret, DB password, MinIO password, Langfuse keys) are injected from Kubernetes secrets, not baked into the image.
- **Init containers** gate startup until PostgreSQL, Elasticsearch, and Keycloak report healthy (`wait4-db`, `wait4-elastic`, `wait4-keycloak`).
- **Startup** warms the DB connection (`SELECT 1`) and launches the guest reaper before serving traffic.

The `entrypoint.sh` has two modes: normal start (`uvicorn main:api`), and **`init-db`** — invoked once against a fresh database to grant Keycloak-schema privileges and apply every `schemas/*.sql` file in order.

---

## Environment Variables

Configuration is read from environment variables in `src/main.py` (`Config.setup()`). See `.env.example` for a starting point. Notable settings:

| Group | Variables |
|-------|-----------|
| **App** | `HOST`, `PORT`, `DEBUG`, `CONTEXT_PATH`, `APP_EXT_DOMAIN` |
| **PostgreSQL** | `POSTGRES_HOST`, `POSTGRES_PORT`, `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB`, `POSTGRES_POOL_SIZE`, `POSTGRES_MAX_OVERFLOW` |
| **Keycloak** | `KEYCLOAK_URL`, `KEYCLOAK_EXT_URL`, `KEYCLOAK_ISSUER_URL`, `KEYCLOAK_REALM`, `KEYCLOAK_CLIENT_ID`, `KEYCLOAK_CLIENT_SECRET`, `KEYCLOAK_POOL_SIZE` |
| **Downstream services** | `FOODCHAT_URL`, `FOODSCHOLAR_URL`, `RECIPEWRANGLER_URL` |
| **Redis / cache** | `CACHE_ENABLED`, `REDIS_HOST`, `REDIS_PORT`, `IMAGE_CACHE_*` |
| **MinIO / S3** | `MINIO_ENDPOINT`, `MINIO_ROOT`, `MINIO_ROOT_PASSWORD`, `MINIO_BUCKET`, `MINIO_EXT_URL_API`, `MINIO_EXT_URL_CONSOLE` |
| **Elasticsearch** | `ELASTIC_HOST`, `ES_DIM` |
| **Langfuse** | `LANGFUSE_BASE_URL`, `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY` |
| **Guest access** | `GUEST_ENABLED`, `GUEST_TTL_SECONDS`, `GUEST_MAX_ACTIVE`, `GUEST_EMAIL_DOMAIN`, `GUEST_REAPER_INTERVAL_SECONDS`, `GUEST_BUDGET_CHAT`, `GUEST_BUDGET_QA`, `GUEST_BUDGET_SEARCH`, `GUEST_BUDGET_SESSIONS` |
| **Init-db only** | `PG_ROOT_USER`, `PG_ROOT_PASSWORD`, `KEYCLOAK_SCHEMA`, `INITIALIZE_DB` |

---

## Database Migrations

Schema is versioned as **ordered, idempotent SQL** in `schemas/` (`10_…`, `20_…`, `30_…`, `40_…`). Every statement is `IF NOT EXISTS`, so applying the set is always safe to repeat.

- **On a fresh database:** run the container with `INITIALIZE_DB=1` or `entrypoint.sh init-db`. It grants the Keycloak-schema privileges and applies **all** `schemas/*.sql` in filename order.
- **On an already-initialized database:** the init path does **not** run on a normal container start. New `NN_*.sql` files must be **applied manually** against the live database (each is `IF NOT EXISTS`, so re-running the whole set is also safe).

> `alembic` is a dependency but the project currently ships raw ordered SQL rather than Alembic revisions; adopting Alembic would replace the manual-apply step with a tracked `upgrade`.

---

## Testing

```bash
pip install -r requirements.txt   # includes pytest + pytest-asyncio
pytest
```

`pytest.ini` sets `pythonpath = src` and `asyncio_mode = auto`. Current suites cover the guest reaper, the meal-plan library, saved items, observability normalisation, and RecipeWrangler identity forwarding (`tests/`). Coverage is still growing; entity-layer paths that need a live PostgreSQL are typically exercised against a throwaway container.

---

## License

Apache License 2.0 — see [LICENSE](LICENSE).

Part of the [WiseFood EU project](https://wisefood-project.eu/).
