-- Reminder emails for people who have stopped using WiseFood, and what each
-- person has said about receiving them.
--
-- Applied by hand like every other schema here. Until it is, the gateway runs
-- normally: activity is simply not recorded, and the console's reminder card
-- says the schema is missing instead of pretending to work.
--
-- How the pieces fit:
--
--   user_activity            when each account last used the platform
--   notification_preference  whether they want reminders, and the token in
--                            every reminder's "turn these off" link
--   notification_run         one row per reminder cycle, scheduled or run by
--                            an admin from the console
--   notification_log         one row per reminder: who it was about, which
--                            stretch of inactivity, sent or failed
--
-- Two unique indexes are what keep a reminder from going out twice when the
-- gateway runs on several replicas and workers: one scheduled run per day,
-- and one reminder per (person, stretch of inactivity, sequence number).

CREATE TABLE IF NOT EXISTS wisefood.user_activity (
    -- Keycloak user id (token `sub`), like owner_id elsewhere.
    user_id         VARCHAR(100) PRIMARY KEY,
    last_seen_at    TIMESTAMPTZ  NOT NULL,
    first_seen_at   TIMESTAMPTZ  NOT NULL DEFAULT now(),
    -- The language the person last used the app in; a reminder is written in it.
    locale          VARCHAR(8),
    -- True while last_seen_at is the moment tracking began rather than a real
    -- visit: the account existed before this table did, so its clock starts
    -- at the first reminder cycle instead of at some unknowable last login.
    estimated       BOOLEAN      NOT NULL DEFAULT false
);

-- "Who has been away longer than N days."
CREATE INDEX IF NOT EXISTS ix_user_activity_last_seen
    ON wisefood.user_activity (last_seen_at);

CREATE TABLE IF NOT EXISTS wisefood.notification_preference (
    user_id               VARCHAR(100) PRIMARY KEY,
    inactivity_reminders  BOOLEAN      NOT NULL DEFAULT true,
    -- 256 bits of randomness, URL-safe. The only credential the one-click
    -- unsubscribe needs, so a link works from any mail client without a login.
    unsubscribe_token     VARCHAR(64)  UNIQUE,
    updated_at            TIMESTAMPTZ  NOT NULL DEFAULT now(),
    -- profile | email_link | list_header | admin: how the last change was made.
    updated_via           VARCHAR(16)
);

CREATE TABLE IF NOT EXISTS wisefood.notification_run (
    id              BIGSERIAL    PRIMARY KEY,
    kind            VARCHAR(32)  NOT NULL,
    -- live: to participants. experimental: the same mails, to the admins only.
    mode            VARCHAR(16)  NOT NULL,
    -- scheduled (the daily cycle) or manual (an admin pressed "Run now").
    source          VARCHAR(16)  NOT NULL,
    -- The UTC day a scheduled run belongs to.
    run_date        DATE         NOT NULL,
    triggered_by    VARCHAR(100),
    started_at      TIMESTAMPTZ  NOT NULL DEFAULT now(),
    finished_at     TIMESTAMPTZ,
    -- Counts: considered, due, sent, failed, and why the rest were skipped.
    stats           JSONB        NOT NULL DEFAULT '{}'::jsonb,
    error           TEXT
);

-- One scheduled run per kind, mode and day, however many replicas wake up
-- for it. Mode is part of the key so that switching from experimental to live
-- in the afternoon starts live delivery that day rather than the next.
CREATE UNIQUE INDEX IF NOT EXISTS ux_notification_run_daily
    ON wisefood.notification_run (kind, mode, run_date)
    WHERE source = 'scheduled';

CREATE INDEX IF NOT EXISTS ix_notification_run_recent
    ON wisefood.notification_run (kind, started_at DESC);

CREATE TABLE IF NOT EXISTS wisefood.notification_log (
    id              BIGSERIAL    PRIMARY KEY,
    kind            VARCHAR(32)  NOT NULL,
    -- live | experimental | test (an admin's "send me a sample")
    mode            VARCHAR(16)  NOT NULL,
    -- Whom the reminder is about. NULL for a test send.
    user_id         VARCHAR(100),
    -- Where it went. Masked for live sends (a***@example.org): the console
    -- needs to tell rows apart, not to hold a second copy of every address.
    recipient       VARCHAR(255),
    -- The stretch of inactivity this reminder belongs to (the last_seen_at it
    -- was computed from) and its place in that stretch.
    inactive_since  TIMESTAMPTZ,
    sequence        SMALLINT,
    -- claimed (about to send) | sent | failed
    status          VARCHAR(16)  NOT NULL,
    attempts        SMALLINT     NOT NULL DEFAULT 1,
    error           TEXT,
    run_id          BIGINT,
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT now(),
    sent_at         TIMESTAMPTZ
);

-- At most one reminder per person, stretch and sequence number in each mode.
-- A test send has NULLs here and never collides.
CREATE UNIQUE INDEX IF NOT EXISTS ux_notification_log_once
    ON wisefood.notification_log (kind, mode, user_id, inactive_since, sequence);

CREATE INDEX IF NOT EXISTS ix_notification_log_recent
    ON wisefood.notification_log (kind, created_at DESC);

-- Erasure deletes a person's rows.
CREATE INDEX IF NOT EXISTS ix_notification_log_user
    ON wisefood.notification_log (user_id);
