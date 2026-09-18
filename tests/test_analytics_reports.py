"""Reading time out of activity events.

Nothing records when somebody closes a tab, so "how long did they spend in
FoodScholar?" has to be inferred from what they did. These pin the inference:
where visits are cut, what a single-event visit is worth, and that the split
is per person per app — because one number for both apps cannot answer the
question that prompted it.
"""
from __future__ import annotations

import pytest


class TestTimeOnApp:
    """How long somebody spent in each app, read out of their events.

    The question behind it: FoodScholar has 54 comments and FoodChat 5 — is
    that a difference in use or a difference in willingness to comment? A
    single event count cannot tell you; time per app per person can.
    """

    def test_a_quiet_gap_starts_a_new_visit(self):
        """Two clicks an hour apart are two visits, not one hour of use."""
        from analytics.reports import IDLE_GAP_MINUTES

        assert IDLE_GAP_MINUTES >= 5, "a gap shorter than this splits real reading"

    def test_a_visit_is_credited_a_tail(self):
        """A visit of one event lasts zero seconds otherwise, and somebody who
        opened a page and read it would show as having spent no time."""
        from analytics.reports import VISIT_TAIL_SECONDS

        assert VISIT_TAIL_SECONDS > 0

    def test_the_query_groups_by_person_and_app(self):
        """The whole point is the split: one number for both apps cannot
        answer which of them somebody was actually in."""
        from analytics.reports import _TIME_ON_APP_SQL

        assert "PARTITION BY user_id, app" in _TIME_ON_APP_SQL
        assert "GROUP BY user_id, app" in _TIME_ON_APP_SQL

    def test_events_with_no_identity_are_left_out(self):
        """A guest's time cannot be attributed to anybody, and summing it into
        a named person's total would be worse than omitting it."""
        from analytics.reports import _TIME_ON_APP_SQL

        assert "user_id IS NOT NULL" in _TIME_ON_APP_SQL

    def test_the_window_is_bounded_at_both_ends(self):
        from analytics.reports import _TIME_ON_APP_SQL

        assert ":since" in _TIME_ON_APP_SQL and ":until" in _TIME_ON_APP_SQL

    @pytest.mark.asyncio
    async def test_it_shapes_the_rows_per_user_and_app(self):
        from analytics.reports import time_on_app

        class Result:
            def all(self):
                return [
                    ("user-1", "foodscholar", 3600.0, 4),
                    ("user-1", "foodchat", 1800.0, 2),
                    ("user-2", "foodscholar", 60.0, 1),
                ]

        class DB:
            async def execute(self, *_a, **_kw):
                return Result()

        out = await time_on_app(DB(), since=None)
        assert out["user-1"]["seconds"] == 5400
        assert out["user-1"]["visits"] == 6
        assert out["user-1"]["by_app"]["foodscholar"]["seconds"] == 3600
        assert out["user-1"]["by_app"]["foodchat"]["visits"] == 2
        assert out["user-2"]["by_app"]["foodscholar"]["seconds"] == 60

    @pytest.mark.asyncio
    async def test_an_app_that_did_not_say_its_name(self):
        from analytics.reports import time_on_app

        class Result:
            def all(self):
                return [("user-1", None, 120.0, 1)]

        class DB:
            async def execute(self, *_a, **_kw):
                return Result()

        out = await time_on_app(DB(), since=None)
        assert "unknown" in out["user-1"]["by_app"]

    @pytest.mark.asyncio
    async def test_nobody_active_is_an_empty_report_not_an_error(self):
        from analytics.reports import time_on_app

        class Result:
            def all(self):
                return []

        class DB:
            async def execute(self, *_a, **_kw):
                return Result()

        assert await time_on_app(DB(), since=None) == {}
