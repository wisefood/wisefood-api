-- Member-adapted recipes: a member's personal adapted version of a
-- RecipeWrangler recipe (ingredient swap / reduced quantity), produced by the
-- recipe adaptation endpoints. recipe_id is the ORIGINAL RecipeWrangler id;
-- one adaptation per (member, recipe) — saving again replaces it. Strictly
-- owner-scoped: served only to the owning member's household owner (or
-- admin/agent service callers such as FoodChat).
-- NOTE: this file only runs on database initialization (entrypoint.sh init-db /
-- INITIALIZE_DB=1); on already-initialized deployments apply this statement
-- manually (it is IF NOT EXISTS, so re-running init-db is also safe).
CREATE TABLE IF NOT EXISTS wisefood.member_adapted_recipe (
    member_id VARCHAR(100) NOT NULL,
    recipe_id VARCHAR(128) NOT NULL,
    title VARCHAR(512),
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (member_id, recipe_id),
    FOREIGN KEY (member_id) REFERENCES wisefood.household_member(id) ON DELETE CASCADE ON UPDATE CASCADE
);
