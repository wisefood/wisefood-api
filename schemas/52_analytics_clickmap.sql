-- Click maps that a person can read, and a page to draw them on.
--
-- Applied by hand like every other schema here. Both columns are additive and
-- nullable, so an older gateway keeps writing to this table unchanged and the
-- reports simply have nothing to show for rows written before the rollout.
--
-- `element_label` is what the control calls itself. `element_key` groups
-- clicks correctly and reads as `div.flex.items-center>button.px-3.py-1`,
-- which names nothing anybody can go and look at; the label is the half a
-- curator can act on.
--
-- `page_path` is the concrete address, alongside the route pattern in `path`
-- that makes clicks poolable across visits. The console needs one real URL to
-- render the page behind the map, and `/recipe-wrangler/:id()` cannot be
-- navigated to.

ALTER TABLE analytics.ui_interaction
    ADD COLUMN IF NOT EXISTS element_label VARCHAR(80);

ALTER TABLE analytics.ui_interaction
    ADD COLUMN IF NOT EXISTS page_path VARCHAR(255);

-- Picking a representative concrete page for a route pattern is a
-- most-recent-per-pattern lookup, run once per click-map view.
CREATE INDEX IF NOT EXISTS ix_interaction_page_path
    ON analytics.ui_interaction (path, occurred_at DESC)
    WHERE page_path IS NOT NULL;
