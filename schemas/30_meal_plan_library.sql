-- Saved meal plans ("library"): a member keeps a generated plan under a name
-- so they can find it again, instead of it only being reachable by the date it
-- was scheduled for.
--
-- Deliberately a SEPARATE table from wisefood.meal_plan rather than a nullable
-- applied_on plus a flag on it. A scheduled plan answers "what are we eating on
-- Tuesday" and a saved plan answers "that pasta week we liked"; the same row
-- cannot be both without renaming a library entry silently mutating someone's
-- schedule, or unscheduling a day silently deleting their saved copy. Saving
-- therefore snapshots the meals, and the two lifecycles stay independent.
--
-- source_meal_plan_id keeps the provenance chain back to the FoodChat plan the
-- snapshot came from; it is NOT a foreign key, because the plan it points at
-- may be revoked while the saved copy lives on.
--
-- NOTE: this file only runs on database initialization (entrypoint.sh init-db /
-- INITIALIZE_DB=1); on already-initialized deployments apply these statements
-- manually (they are IF NOT EXISTS, so re-running init-db is also safe).
CREATE TABLE IF NOT EXISTS wisefood.saved_meal_plan (
    id VARCHAR(64) PRIMARY KEY,
    member_id VARCHAR(100) NOT NULL,
    name VARCHAR(255) NOT NULL,
    source_meal_plan_id VARCHAR(64),
    source_applied_on DATE,
    breakfast JSONB NOT NULL DEFAULT '{}'::jsonb,
    lunch JSONB NOT NULL DEFAULT '{}'::jsonb,
    dinner JSONB NOT NULL DEFAULT '{}'::jsonb,
    reasoning TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (member_id) REFERENCES wisefood.household_member(id) ON DELETE CASCADE ON UPDATE CASCADE
);

CREATE INDEX IF NOT EXISTS ix_saved_meal_plan_member_id
    ON wisefood.saved_meal_plan(member_id);

-- Library listings are newest-first per member.
CREATE INDEX IF NOT EXISTS ix_saved_meal_plan_member_created
    ON wisefood.saved_meal_plan(member_id, created_at DESC);

-- Saving the same generated plan twice is an update, not a duplicate entry.
-- Partial, because a plan saved without provenance (source_meal_plan_id NULL)
-- must not collide with every other such plan.
CREATE UNIQUE INDEX IF NOT EXISTS uq_saved_meal_plan_member_source
    ON wisefood.saved_meal_plan(member_id, source_meal_plan_id)
    WHERE source_meal_plan_id IS NOT NULL;
