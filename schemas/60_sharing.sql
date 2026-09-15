-- Share links: one artifact, one unguessable token, no account needed to read.
--
-- Applied by hand like every other schema here.
--
-- The payload is a SNAPSHOT, stored on this row, not a reference to the plan
-- it came from. A link someone sent to their mother must not change when they
-- edit the plan, must not break when they delete it, and must not start
-- showing a different week. It also means a guest's share outlives the reaper
-- that deletes the guest — which is the point: the link keeps working, and
-- keeping the account is how you get to edit it again.
--
-- The payload is scrubbed before it is written. A meal plan is health-adjacent
-- personal data — allergies, a child's age, who lives in the house — and a
-- share carries the food, never the people. See `sharing.scrub_meal_plan`.

CREATE TABLE IF NOT EXISTS wisefood.share_link (
    -- URL-safe, 256 bits of randomness. This is the only credential, so it is
    -- the primary key: a lookup is the authorisation check.
    token           VARCHAR(64) PRIMARY KEY,
    kind            VARCHAR(32)  NOT NULL,
    -- What it was made from. Kept for the owner's "what have I shared" list
    -- and for revoking by source; never used to re-read the live object.
    source_id       VARCHAR(100),
    owner_id        VARCHAR(100) NOT NULL,
    title           VARCHAR(200),
    payload         JSONB        NOT NULL,
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT now(),
    -- NULL means "until revoked". A date is offered so somebody can share a
    -- week's plan without it outliving the week.
    expires_at      TIMESTAMPTZ,
    revoked_at      TIMESTAMPTZ,
    view_count      INTEGER      NOT NULL DEFAULT 0,
    last_viewed_at  TIMESTAMPTZ
);

-- "Everything I have shared", newest first.
CREATE INDEX IF NOT EXISTS ix_share_link_owner
    ON wisefood.share_link (owner_id, created_at DESC);

-- Revoking every link made from one plan.
CREATE INDEX IF NOT EXISTS ix_share_link_source
    ON wisefood.share_link (kind, source_id);
