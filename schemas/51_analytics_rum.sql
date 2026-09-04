-- =============================================================================
-- Real user monitoring: the device, the errors, the clicks, the speed.
--
-- 50_analytics.sql records what people DID. This records what it was LIKE:
-- which browser on which screen, what broke, where they clicked, how long the
-- page took to become usable. Together they answer the question neither can
-- alone — "this person's session failed, and here is the machine, the error
-- and the click that did it".
--
-- CONSENT works exactly as it does next door, and matters more here. A user
-- agent string identifies a device, and an IP address identifies a household;
-- both are personal data. So:
--
--   * The full user agent is kept only for a consenting user. The *parsed*
--     browser, OS and device type are kept either way, because "12% of visits
--     are on iOS Safari" is not about anybody.
--   * The IP address is NEVER stored. Only a truncated prefix (IPv4 /24, IPv6
--     /48) survives, which is enough to spot one broken network and not enough
--     to name a house. There is no setting that turns the full address on.
--
-- VOLUME. Clicks and vitals are the highest-rate things the platform will ever
-- record, and both are sampled and off by default. Errors are never sampled:
-- an error that only happens to one person in a thousand is the interesting
-- one, and a sampled crash report is worse than none.
-- =============================================================================

CREATE SCHEMA IF NOT EXISTS analytics;

-- -----------------------------------------------------------------------------
-- ONE ROW PER BROWSER SESSION. Everything about the machine lives here rather
-- than on every event: a device does not change mid-visit, and repeating the
-- user agent on ten thousand rows would cost more than the events themselves.
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS analytics.client_session (
    session_id          VARCHAR(64) PRIMARY KEY,
    started_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at        TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- Identity, on the same terms as everywhere else: NULL unless the person
    -- agreed to be named. The session still counts; it just has nobody on it.
    user_id             VARCHAR(100),
    member_id           VARCHAR(100),
    is_guest            BOOLEAN NOT NULL DEFAULT FALSE,

    app                 VARCHAR(32),
    client              VARCHAR(64),
    release             VARCHAR(64),

    -- The machine. Parsed fields are kept for everyone; the raw string is
    -- stripped for anyone who has not consented, because it is a fingerprint.
    user_agent          TEXT,
    browser             VARCHAR(48),
    browser_version     VARCHAR(24),
    os                  VARCHAR(48),
    os_version          VARCHAR(24),
    device_type         VARCHAR(16),
    is_bot              BOOLEAN NOT NULL DEFAULT FALSE,

    -- The screen. `viewport` is the part of the page a person can actually
    -- see, which is the number that decides whether a layout works; `screen`
    -- is the monitor, which decides whether the window was ever maximised.
    screen_w            INTEGER,
    screen_h            INTEGER,
    viewport_w          INTEGER,
    viewport_h          INTEGER,
    device_pixel_ratio  NUMERIC(4, 2),
    color_scheme        VARCHAR(8),
    reduced_motion      BOOLEAN,

    -- Where from. Never the address itself — see the header of this file.
    ip_prefix           VARCHAR(64),
    country             VARCHAR(2),
    timezone            VARCHAR(64),
    connection          VARCHAR(16),
    locale              VARCHAR(16),

    -- Rollups, so the session list can be sorted by "worst" without touching
    -- four other tables. Maintained on write, approximate by design.
    events              INTEGER NOT NULL DEFAULT 0,
    errors              INTEGER NOT NULL DEFAULT 0,
    pages               INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS ix_client_session_started
    ON analytics.client_session (started_at DESC);
CREATE INDEX IF NOT EXISTS ix_client_session_user
    ON analytics.client_session (user_id, started_at DESC) WHERE user_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_client_session_browser
    ON analytics.client_session (browser, started_at DESC);
CREATE INDEX IF NOT EXISTS ix_client_session_os
    ON analytics.client_session (os, started_at DESC);
CREATE INDEX IF NOT EXISTS ix_client_session_device
    ON analytics.client_session (device_type, started_at DESC);
CREATE INDEX IF NOT EXISTS ix_client_session_country
    ON analytics.client_session (country, started_at DESC) WHERE country IS NOT NULL;
-- The one that makes "show me the sessions that went wrong" instant.
CREATE INDEX IF NOT EXISTS ix_client_session_errors
    ON analytics.client_session (started_at DESC) WHERE errors > 0;

-- -----------------------------------------------------------------------------
-- WHAT BROKE. One row per occurrence, grouped by fingerprint.
--
-- Occurrences and groups are separate tables on purpose. A single bad deploy
-- produces one group and fifty thousand occurrences; a console that reads the
-- group table shows fifty thousand as a number instead of a page of scrolling,
-- and retention can thin the occurrences while keeping the history of what
-- happened.
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS analytics.error_group (
    fingerprint     VARCHAR(64) PRIMARY KEY,
    first_seen_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    app             VARCHAR(32),
    kind            VARCHAR(24),
    name            VARCHAR(128),
    message         TEXT,
    culprit         VARCHAR(255),
    occurrences     BIGINT NOT NULL DEFAULT 0,
    sessions        BIGINT NOT NULL DEFAULT 0,
    users           BIGINT NOT NULL DEFAULT 0,
    -- new | acknowledged | resolved | ignored. Mirrors the feedback inbox, so
    -- the console has one idea of "somebody has dealt with this".
    status          VARCHAR(16) NOT NULL DEFAULT 'new',
    -- The release it was last seen in. A group whose last_release is older
    -- than the current one is fixed, whether or not anybody marked it so.
    first_release   VARCHAR(64),
    last_release    VARCHAR(64),
    resolved_at     TIMESTAMPTZ,
    resolved_by     VARCHAR(100),
    notes           TEXT
);

CREATE INDEX IF NOT EXISTS ix_error_group_last_seen
    ON analytics.error_group (last_seen_at DESC);
CREATE INDEX IF NOT EXISTS ix_error_group_status
    ON analytics.error_group (status, last_seen_at DESC);
CREATE INDEX IF NOT EXISTS ix_error_group_app
    ON analytics.error_group (app, last_seen_at DESC);

CREATE TABLE IF NOT EXISTS analytics.client_error (
    id                  BIGSERIAL PRIMARY KEY,
    occurred_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    received_at         TIMESTAMPTZ NOT NULL DEFAULT now(),

    request_id          VARCHAR(64),
    client_session_id   VARCHAR(64),
    user_id             VARCHAR(100),
    member_id           VARCHAR(100),
    is_guest            BOOLEAN NOT NULL DEFAULT FALSE,

    app                 VARCHAR(32) NOT NULL,
    release             VARCHAR(64),
    fingerprint         VARCHAR(64) NOT NULL,

    -- error | unhandledrejection | vue | http | resource | csp
    kind                VARCHAR(24) NOT NULL,
    name                VARCHAR(128),
    message             TEXT,
    -- The frame the error is attributed to. What a list is sorted and read by,
    -- so it is stored rather than re-derived from the stack every time.
    culprit             VARCHAR(255),
    stack               TEXT,
    url_path            VARCHAR(255),
    line_no             INTEGER,
    col_no              INTEGER,
    -- FALSE means it reached the top of the stack. An unhandled error is a
    -- different severity from one a component caught and reported.
    handled             BOOLEAN NOT NULL DEFAULT FALSE,

    -- The last few things that happened before it. This is what turns a stack
    -- trace into a reproduction, and it is also the riskiest field here: the
    -- recorder truncates it and strips anything that looks like typed text.
    breadcrumbs         JSONB NOT NULL DEFAULT '[]',
    context             JSONB NOT NULL DEFAULT '{}',

    -- Denormalised from client_session so an error list can show the device
    -- without a join, and so an error outlives a trimmed session row.
    browser             VARCHAR(48),
    os                  VARCHAR(48),
    device_type         VARCHAR(16)
);

CREATE INDEX IF NOT EXISTS ix_client_error_occurred
    ON analytics.client_error (occurred_at DESC);
CREATE INDEX IF NOT EXISTS ix_client_error_fingerprint
    ON analytics.client_error (fingerprint, occurred_at DESC);
CREATE INDEX IF NOT EXISTS ix_client_error_session
    ON analytics.client_error (client_session_id, occurred_at DESC)
    WHERE client_session_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_client_error_user
    ON analytics.client_error (user_id, occurred_at DESC) WHERE user_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_client_error_request
    ON analytics.client_error (request_id) WHERE request_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_client_error_app_time
    ON analytics.client_error (app, occurred_at DESC);

-- -----------------------------------------------------------------------------
-- WHERE PEOPLE CLICKED.
--
-- Two readings of the same rows. `element_key` answers "which control gets
-- used", which is the actionable one — it survives a redesign and names a
-- thing you can change. The coordinates answer "where on the page", which is
-- what draws a heatmap and what shows you people clicking something that is
-- not a button.
--
-- Coordinates are stored as ten-thousandths of the page box, not pixels: a
-- heatmap has to overlay sessions from a 1280-wide laptop and a 390-wide
-- phone, and pixels from those two do not belong on the same picture.
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS analytics.ui_interaction (
    id                  BIGSERIAL PRIMARY KEY,
    occurred_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    client_session_id   VARCHAR(64),
    user_id             VARCHAR(100),
    is_guest            BOOLEAN NOT NULL DEFAULT FALSE,

    app                 VARCHAR(32) NOT NULL,
    -- The route pattern, not the URL: `/recipes/[id]`, so ten thousand recipe
    -- pages make one heatmap instead of ten thousand of one click each.
    path                VARCHAR(255) NOT NULL,

    -- click | rage | dead | scroll
    kind                VARCHAR(16) NOT NULL DEFAULT 'click',
    -- A stable name for the thing clicked, from an explicit data attribute
    -- where the UI supplies one and a short structural path otherwise.
    element_key         VARCHAR(160),
    element_role        VARCHAR(32),

    -- 0..10000 across the page box.
    x_pct               INTEGER,
    y_pct               INTEGER,
    viewport_w          INTEGER,
    viewport_h          INTEGER,
    -- For a scroll row: how far down the page they got, same scale.
    depth_pct           INTEGER,
    -- How many clicks this row stands for. A rage click is three to five
    -- clicks in one spot, recorded once with a count rather than five times.
    repeats             SMALLINT NOT NULL DEFAULT 1
);

CREATE INDEX IF NOT EXISTS ix_interaction_occurred
    ON analytics.ui_interaction (occurred_at DESC);
CREATE INDEX IF NOT EXISTS ix_interaction_path_time
    ON analytics.ui_interaction (path, occurred_at DESC);
CREATE INDEX IF NOT EXISTS ix_interaction_element
    ON analytics.ui_interaction (element_key, occurred_at DESC)
    WHERE element_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_interaction_session
    ON analytics.ui_interaction (client_session_id, occurred_at DESC)
    WHERE client_session_id IS NOT NULL;
-- Rage and dead clicks are a small fraction of rows and the only ones anybody
-- goes looking for by kind.
CREATE INDEX IF NOT EXISTS ix_interaction_trouble
    ON analytics.ui_interaction (kind, path, occurred_at DESC)
    WHERE kind IN ('rage', 'dead');

-- -----------------------------------------------------------------------------
-- HOW FAST IT FELT.
--
-- Server latency is already recorded per route, and it is not the same thing.
-- A route that answers in 80ms can still take four seconds to become usable,
-- and only the browser can say so.
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS analytics.web_vital (
    id                  BIGSERIAL PRIMARY KEY,
    occurred_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    client_session_id   VARCHAR(64),
    user_id             VARCHAR(100),

    app                 VARCHAR(32) NOT NULL,
    path                VARCHAR(255) NOT NULL,
    -- LCP | CLS | INP | TTFB | FCP
    metric              VARCHAR(8) NOT NULL,
    -- Milliseconds for every metric except CLS, which is unitless. Kept in one
    -- column with the unit implied by the metric, because a column per metric
    -- would need a migration for the next one the standard adds.
    value               NUMERIC(12, 4) NOT NULL,
    -- good | needs-improvement | poor, as the browser's own thresholds say.
    rating              VARCHAR(20),
    navigation_type     VARCHAR(16),
    device_type         VARCHAR(16)
);

CREATE INDEX IF NOT EXISTS ix_vital_occurred
    ON analytics.web_vital (occurred_at DESC);
CREATE INDEX IF NOT EXISTS ix_vital_metric_time
    ON analytics.web_vital (metric, occurred_at DESC);
CREATE INDEX IF NOT EXISTS ix_vital_path
    ON analytics.web_vital (path, metric, occurred_at DESC);
CREATE INDEX IF NOT EXISTS ix_vital_session
    ON analytics.web_vital (client_session_id) WHERE client_session_id IS NOT NULL;
