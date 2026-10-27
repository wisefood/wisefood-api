"""What a click map may say about what was clicked.

Browsers sent the words of whatever was clicked, and on FoodChat that was a
household member's name on a chip, a line of somebody's plan, and their own
chat message — shown to everybody with console access. A label is now kept
only for a control, or for a key a developer named; and the same rule is
applied when reading, so labels recorded before the fix stop showing.
"""
from __future__ import annotations

import inspect

import main  # noqa: F401 — `sql` imports `backend.postgres`, which needs config

from analytics.recorder import label_allowed


class TestWhichLabelsAreKept:
    def test_a_controls_own_name(self):
        assert label_allowed("button", "div.flex>button.inline-flex")
        assert label_allowed("textarea", "foodchat.composer › div>textarea")

    def test_never_the_text_of_something_that_is_not_a_control(self):
        # The leaks: a plan line, a chat message, a name inside a chip's span.
        assert not label_allowed("p", "div.plan-card>div.flex>p.text-xs")
        assert not label_allowed("p", "div.fc-bubble.fc-bubble-user>p.text-sm")
        assert not label_allowed("span", "button.fc-diner-chip>span.truncate")
        assert not label_allowed(None, "div>div")

    def test_a_developer_named_key_keeps_its_label(self):
        assert label_allowed("separator", "foodchat.splitter")
        assert label_allowed(None, "foodchat.splitter")

    def test_the_recorder_and_every_reader_apply_it(self):
        from analytics import recorder, reports

        assert "label_allowed(element_role, element_key)" in inspect.getsource(
            recorder.ActivityRecorder.record_interaction
        )
        for fn in (reports.click_map, reports.interaction_overview, reports.session_device):
            assert "label_allowed(" in inspect.getsource(fn), fn.__name__


class TestAreas:
    def test_an_area_is_the_prefix_of_a_key(self):
        from analytics.reports import _area_of

        assert _area_of("foodchat.plan › div.px-3>span") == "foodchat.plan"
        assert _area_of("div.px-3>span") is None
        assert _area_of("foodchat.splitter") is None
        assert _area_of(None) is None

    def test_the_click_map_reports_them(self):
        from analytics.reports import click_map

        assert '"areas"' in inspect.getsource(click_map)
