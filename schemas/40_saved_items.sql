-- Typed member library: one row per (member, asset). Generalises the old
-- recipe-only member_favorite so a member can also save literature (articles,
-- guides, textbooks) they found in FoodScholar, alongside recipes.
--
-- item_type says what kind of thing item_ref points at:
--   'recipe'   -> opaque RecipeWrangler recipe id (as member_favorite held)
--   'article'  -> urn:article:<slug>
--   'guide'    -> urn:guide:<slug>
--   'textbook' -> urn:textbook:<slug>
-- The gateway does not dereference item_ref; it is an opaque handle owned by
-- RecipeWrangler / the data-api catalog. New types can be added without DDL.
--
-- Scoped per household member (ON DELETE CASCADE), exactly like favourites.
-- The recipe rows here are the SAME data the /favorites endpoints read and
-- write — those endpoints are now a recipe-only view over this table — so
-- FoodChat and RecipeWrangler keep getting favorite_recipe_ids unchanged.
--
-- NOTE: this file only runs on database initialization (entrypoint.sh init-db /
-- INITIALIZE_DB=1); on already-initialized deployments apply these statements
-- manually (they are IF NOT EXISTS, so re-running init-db is also safe).

CREATE TABLE IF NOT EXISTS wisefood.member_saved_item (
    member_id VARCHAR(100) NOT NULL,
    item_type VARCHAR(32) NOT NULL,
    item_ref VARCHAR(512) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (member_id, item_type, item_ref),
    FOREIGN KEY (member_id) REFERENCES wisefood.household_member(id) ON DELETE CASCADE ON UPDATE CASCADE
);

-- Library listings are newest-first, optionally filtered by type.
CREATE INDEX IF NOT EXISTS ix_member_saved_item_member_created
    ON wisefood.member_saved_item(member_id, created_at DESC);
CREATE INDEX IF NOT EXISTS ix_member_saved_item_member_type
    ON wisefood.member_saved_item(member_id, item_type);

-- Backfill existing recipe favourites into the unified table. Idempotent: safe
-- to re-run, and a no-op once member_favorite has been retired. member_favorite
-- is intentionally left in place for now so a rollback keeps working; a later
-- migration drops it once nothing reads it directly.
INSERT INTO wisefood.member_saved_item (member_id, item_type, item_ref, created_at)
SELECT member_id, 'recipe', recipe_id, created_at
FROM wisefood.member_favorite
ON CONFLICT (member_id, item_type, item_ref) DO NOTHING;
