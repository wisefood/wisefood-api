-- Activity analytics: what people did, what they searched for, what the LLMs
-- cost, and what they told us about the answers.
--
-- Everything lives in its own `analytics` schema rather than alongside the
-- domain tables in `wisefood`, for three reasons: it is append-mostly and will
-- dwarf the domain tables in row count; it can be granted read-only to a
-- reporting role (Grafana) without exposing household data; and it can be
-- truncated or dropped wholesale without touching anything a user owns.
--
-- WHY NOT PARTITIONED. The design note called for monthly range partitions.
-- They are not used here, deliberately. This platform has no migration tooling,
-- no CI and no scheduled DDL: schema files are applied by hand. Declarative
-- partitioning in that setting adds a failure mode nobody is positioned to
-- handle — once rows land in a DEFAULT partition, creating the real partition
-- for that range fails, and the fix is a manual data move. At the volumes this
-- is sized for (order 10^4 events/day, 10^7/year) a single indexed table is
-- comfortable, and retention is a batched DELETE. Partitioning is the upgrade
-- path when daily volume reaches the millions; the column layout below is
-- already partition-ready (occurred_at first in every time index).
--
-- IDENTITY AND CONSENT. user_id/member_id/household_id are nullable and are
-- written only for users whose analytics consent allows it (see
-- ANALYTICS_CONSENT_MODE). An event from a user who has not consented is still
-- recorded — the aggregate is not personal data — but lands with its identity
-- columns NULL, and with free text they typed (raw_query, feedback.comment)
-- NULL as well.
--
-- Such a row is PSEUDONYMOUS, not anonymous: request_id is kept, because it is
-- the operational correlation key that joins this row to the service logs and
-- to foodscholar.qa_requests, and those operational records carry the subject
-- for service provision regardless of analytics consent. An administrator with
-- access to both can re-identify a stripped row through that join. That is a
-- deliberate trade — request ids are what make an incident debuggable — and
-- it is why reading this schema is admin/expert only and every read is itself
-- recorded. client_session_id is likewise kept and links one sitting's rows. Nothing here is a foreign key into `wisefood`: analytics rows
-- must survive the deletion of the household they describe, and erasure nulls
-- the identity rather than removing the row.
--
--
-- SESSIONS. `client_session_id` appears on every table rather than only on
-- `event`, so "everything this person did in one sitting" is a single
-- predicate instead of a join through the event table. The id is minted by the
-- browser, shown to the user in the page footer, and quoted back to support —
-- so it has to be present on the rows those questions are actually asked of:
-- how many searches, how many questions, how much did it cost.
--
-- It is not an identity. It is per browser tab, resets after idle and on
-- logout, and says nothing about who the person is.
-- NOTE: this file only runs on database initialization (entrypoint.sh init-db /
-- INITIALIZE_DB=1); on already-initialized deployments apply these statements
-- manually (they are IF NOT EXISTS, so re-running init-db is also safe).

CREATE SCHEMA IF NOT EXISTS analytics;


-- One row per recorded activity: a completed HTTP request, a semantic domain
-- action (a recipe viewed, a plan saved), or a client-side event posted by the
-- browser or the SDK. `props` carries whatever is specific to the event type,
-- so a new event type needs no DDL.
CREATE TABLE IF NOT EXISTS analytics.event (
    id BIGSERIAL PRIMARY KEY,
    -- When it happened (a client event may report a time earlier than its
    -- arrival) versus when we stored it. Both, because the gap is the only way
    -- to spot a client with a wrong clock or a long-buffered batch.
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    received_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,

    -- The platform correlation id: joins this row to the gateway's other rows,
    -- to every downstream service's log lines, and to foodscholar.qa_requests.
    request_id VARCHAR(64),
    client_session_id VARCHAR(64),

    user_id VARCHAR(100),
    member_id VARCHAR(100),
    household_id VARCHAR(100),
    is_guest BOOLEAN NOT NULL DEFAULT FALSE,
    roles TEXT[],

    -- Which product surface: foodchat | foodscholar | recipewrangler | catalog
    -- | console | platform. Which caller: ui | sdk | agent | internal.
    app VARCHAR(32) NOT NULL,
    client VARCHAR(64),

    event_type VARCHAR(64) NOT NULL,
    route VARCHAR(255),
    method VARCHAR(10),
    status INTEGER,
    duration_ms INTEGER,
    locale VARCHAR(16),

    props JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS ix_event_occurred_at
    ON analytics.event (occurred_at DESC);
CREATE INDEX IF NOT EXISTS ix_event_type_time
    ON analytics.event (event_type, occurred_at DESC);
CREATE INDEX IF NOT EXISTS ix_event_app_time
    ON analytics.event (app, occurred_at DESC);
-- Partial: most rows have no user (anonymous, guest, or unconsented), and
-- "per user" queries never want those.
CREATE INDEX IF NOT EXISTS ix_event_user_time
    ON analytics.event (user_id, occurred_at DESC) WHERE user_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_event_request_id
    ON analytics.event (request_id) WHERE request_id IS NOT NULL;
-- The index behind "show me everything from session X", which is what a user
-- quoting the id from their footer turns into.
CREATE INDEX IF NOT EXISTS ix_event_session_time
    ON analytics.event (client_session_id, occurred_at DESC)
    WHERE client_session_id IS NOT NULL;


-- One row per search, on any surface. Separate from `event` because trending
-- and zero-result reporting scan it constantly with predicates no JSONB index
-- would serve as well.
CREATE TABLE IF NOT EXISTS analytics.search_query (
    id BIGSERIAL PRIMARY KEY,
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    request_id VARCHAR(64),
    user_id VARCHAR(100),
    member_id VARCHAR(100),
    is_guest BOOLEAN NOT NULL DEFAULT FALSE,
    client_session_id VARCHAR(64),

    app VARCHAR(32) NOT NULL,
    client VARCHAR(64),
    -- recipes | param | catalog | tools | autocomplete | scholar_library | qa
    surface VARCHAR(32) NOT NULL,

    -- raw_query is NULL when the user has not consented to it being kept, or
    -- when ANALYTICS_CAPTURE_QUERY_TEXT is off. normalized_query and
    -- query_hash are always written: trending works on the normalised form,
    -- and the hash lets an unconsented query still be counted without its text
    -- ever being stored.
    raw_query TEXT,
    normalized_query TEXT,
    query_hash VARCHAR(64),
    filters JSONB NOT NULL DEFAULT '{}'::jsonb,

    -- Both counts, because the recipe search retries on an empty hit set: the
    -- returned total hides the original miss, and "no results" and "no results
    -- until we relaxed the constraints" are different product problems.
    result_count_first_pass INTEGER,
    result_count_final INTEGER,
    zero_result BOOLEAN NOT NULL DEFAULT FALSE,
    relaxed BOOLEAN NOT NULL DEFAULT FALSE,
    lexical_fallback BOOLEAN NOT NULL DEFAULT FALSE,
    latency_ms INTEGER
);

CREATE INDEX IF NOT EXISTS ix_search_occurred_at
    ON analytics.search_query (occurred_at DESC);
CREATE INDEX IF NOT EXISTS ix_search_hash_time
    ON analytics.search_query (query_hash, occurred_at DESC);
CREATE INDEX IF NOT EXISTS ix_search_surface_time
    ON analytics.search_query (surface, occurred_at DESC);
CREATE INDEX IF NOT EXISTS ix_search_zero_result
    ON analytics.search_query (occurred_at DESC) WHERE zero_result;
CREATE INDEX IF NOT EXISTS ix_search_user_time
    ON analytics.search_query (user_id, occurred_at DESC) WHERE user_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_search_session
    ON analytics.search_query (client_session_id) WHERE client_session_id IS NOT NULL;


-- One row per LLM call. Langfuse holds the traces; this holds the numbers,
-- because the Langfuse metrics API cannot group by user — high-cardinality
-- dimensions are filter-only — so "tokens and cost per user" is not a report
-- it can produce.
CREATE TABLE IF NOT EXISTS analytics.llm_usage (
    id BIGSERIAL PRIMARY KEY,
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    request_id VARCHAR(64),
    trace_id VARCHAR(64),
    user_id VARCHAR(100),
    member_id VARCHAR(100),
    client_session_id VARCHAR(64),

    app VARCHAR(32) NOT NULL,
    feature VARCHAR(128),
    provider VARCHAR(32),
    model VARCHAR(128),

    input_tokens INTEGER,
    output_tokens INTEGER,
    total_tokens INTEGER,
    cost_usd NUMERIC(12, 6),
    latency_ms INTEGER
);

CREATE INDEX IF NOT EXISTS ix_llm_occurred_at
    ON analytics.llm_usage (occurred_at DESC);
CREATE INDEX IF NOT EXISTS ix_llm_user_time
    ON analytics.llm_usage (user_id, occurred_at DESC) WHERE user_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_llm_model_time
    ON analytics.llm_usage (model, occurred_at DESC);
CREATE INDEX IF NOT EXISTS ix_llm_request_id
    ON analytics.llm_usage (request_id) WHERE request_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_llm_session
    ON analytics.llm_usage (client_session_id) WHERE client_session_id IS NOT NULL;


-- Every feedback signal from every surface in one place. Today they live in
-- four unjoinable homes (foodchat.feedback, foodscholar.qa_feedback, the UI
-- satisfaction widget, Sentry), which is why no one can answer "what did users
-- complain about this week". The owning service keeps its own copy where it
-- drives behaviour — foodchat's feedback still feeds personalisation — and
-- mirrors here for review.
CREATE TABLE IF NOT EXISTS analytics.feedback (
    id BIGSERIAL PRIMARY KEY,
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    request_id VARCHAR(64),
    user_id VARCHAR(100),
    member_id VARCHAR(100),
    client_session_id VARCHAR(64),

    app VARCHAR(32) NOT NULL,
    -- qa_answer | chat_message | recipe | guide | article | textbook | platform
    target_type VARCHAR(32) NOT NULL,
    target_id VARCHAR(512),

    -- thumbs | likert5 | ab | helpful
    rating_kind VARCHAR(16) NOT NULL,
    -- Free-form on purpose: 'up'/'down', '1'..'5', 'a'/'b', 'helpful'. The
    -- numeric reading lives in rating_value_num when there is one.
    rating_value VARCHAR(32),
    rating_value_num NUMERIC(6, 3),

    reason VARCHAR(128),
    comment TEXT,
    source VARCHAR(16) NOT NULL DEFAULT 'ui',
    -- new | triaged | resolved — the expert inbox's workflow state.
    status VARCHAR(16) NOT NULL DEFAULT 'new',

    langfuse_trace_id VARCHAR(64)
);

CREATE INDEX IF NOT EXISTS ix_feedback_occurred_at
    ON analytics.feedback (occurred_at DESC);
CREATE INDEX IF NOT EXISTS ix_feedback_status_time
    ON analytics.feedback (status, occurred_at DESC);
CREATE INDEX IF NOT EXISTS ix_feedback_target
    ON analytics.feedback (target_type, target_id);
CREATE INDEX IF NOT EXISTS ix_feedback_request_id
    ON analytics.feedback (request_id) WHERE request_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_feedback_session
    ON analytics.feedback (client_session_id) WHERE client_session_id IS NOT NULL;
-- Erasure updates by subject.
CREATE INDEX IF NOT EXISTS ix_feedback_user
    ON analytics.feedback (user_id) WHERE user_id IS NOT NULL;


-- An expert's verdict on something a user asked or complained about. This is
-- the record that does not exist anywhere today: the platform can tell you an
-- expert was *allowed* to act, never that they did, or what they concluded.
CREATE TABLE IF NOT EXISTS analytics.expert_review (
    id BIGSERIAL PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    reviewer_id VARCHAR(100) NOT NULL,
    reviewer_name VARCHAR(255),

    -- qa_answer | chat_message | feedback | guideline | recipe | article
    target_type VARCHAR(32) NOT NULL,
    target_id VARCHAR(512) NOT NULL,
    request_id VARCHAR(64),

    -- correct | partially_correct | incorrect | unsafe | off_topic | unclear
    verdict VARCHAR(32) NOT NULL,
    notes TEXT,
    tags TEXT[],
    -- Set once the verdict has also been written to Langfuse as an annotation
    -- score, so the review shows up next to the trace it judges.
    langfuse_score_id VARCHAR(64),

    UNIQUE (reviewer_id, target_type, target_id)
);

CREATE INDEX IF NOT EXISTS ix_review_created_at
    ON analytics.expert_review (created_at DESC);
CREATE INDEX IF NOT EXISTS ix_review_target
    ON analytics.expert_review (target_type, target_id);


-- Runtime switches, editable by an admin from the console without a redeploy.
-- The platform-level ANALYTICS_ENABLED env var still wins: this table can only
-- narrow what is collected, never widen it beyond what the deployment allows.
CREATE TABLE IF NOT EXISTS analytics.settings (
    key VARCHAR(64) PRIMARY KEY,
    value JSONB NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_by VARCHAR(100)
);
