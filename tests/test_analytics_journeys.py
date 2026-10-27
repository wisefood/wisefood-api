"""Following what a session did, rather than counting it.

A session page used to answer "how many requests" and "which routes". The
question people bring to it is what somebody tried to do and whether it worked:
how many plans they made in FoodChat and what the conversation looked like on
the way, what they asked FoodScholar, whether a search led anywhere. These pin
how those answers are shaped, and the correlation that lets a session see the
rows FoodChat, FoodScholar and RecipeWrangler report in the first place.

The statements were run against PostgreSQL 17 with seeded sessions; what is
guarded here is the shaping and the properties that keep the statements safe.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import main  # noqa: F401 — `sql` imports `backend.postgres`, which needs config

T0 = datetime(2026, 10, 27, 10, 0, tzinfo=timezone.utc)


def at(seconds: int) -> datetime:
    return T0 + timedelta(seconds=seconds)


def chat_turn(request_id, seconds, intent, outcome="ok", conversation="conv-1", **extra):
    return {
        "occurred_at": at(seconds),
        "event_type": "chat.turn",
        "request_id": request_id,
        "props": {"session_id": conversation, "intent": intent, "outcome": outcome, **extra},
    }


def plan(request_id, seconds, refinement=False, conversation="conv-1", plan_id="p1"):
    return {
        "occurred_at": at(seconds),
        "event_type": "chat.plan_generated",
        "request_id": request_id,
        "props": {
            "session_id": conversation,
            "plan_id": plan_id,
            "plan_type": "daily",
            "version": 2 if refinement else 1,
            "refinement": refinement,
            "item_count": 4,
        },
    }


def saved(request_id, seconds, conversation="conv-1"):
    return {
        "occurred_at": at(seconds),
        "event_type": "chat.plan_saved",
        "request_id": request_id,
        "props": {"session_id": conversation, "plan_id": "p2"},
    }


def search(request_id, seconds, surface, query, results=5):
    return {
        "occurred_at": at(seconds).isoformat(),
        "occurred_at_dt": at(seconds),
        "request_id": request_id,
        "surface": surface,
        "query": query,
        "results": results,
        "zero_result": results == 0,
        "relaxed": False,
        "lexical_fallback": False,
        "latency_ms": 100,
    }


class TestASessionsConversations:
    """One FoodChat conversation, as the turns somebody took."""

    def build(self):
        from analytics.reports import _build_journey

        events = [
            # A plan is reported during its turn, so before the turn's own row.
            plan("r1", 7),
            chat_turn("r1", 8, "daily_plan", opening_turn=True, latency_ms=8000),
            plan("r2", 65, refinement=True, plan_id="p2"),
            chat_turn("r2", 66, "refine_plan"),
            chat_turn("r3", 90, None, outcome="error"),
            saved("r4", 120),
        ]
        searches = [
            search("r1", 3, "tools", "chicken salad", 12),
            search("r2", 62, "tools", "vegan stew", 0),
        ]
        llm = {"r1": {"calls": 2, "cost_usd": 0.003}, "r2": {"calls": 1, "cost_usd": 0.0015}}
        return _build_journey(events, searches, llm, [])

    def test_the_plans_are_counted_and_told_apart_from_refinements(self):
        totals = self.build()["foodchat"]["totals"]
        assert totals["plans_generated"] == 2
        assert totals["plans_refined"] == 1
        assert totals["plans_saved"] == 1

    def test_each_plan_hangs_on_the_turn_that_made_it(self):
        """Not a separate row after it: the plan arrives first, and as its own
        step it read as though the plan came before anybody asked for it."""
        steps = self.build()["foodchat"]["conversations"][0]["steps"]
        assert [s["kind"] for s in steps] == ["turn", "turn", "turn", "plan_saved"]
        assert steps[0]["plan"]["refinement"] is False
        assert steps[1]["plan"]["refinement"] is True
        assert steps[2]["plan"] is None

    def test_the_path_says_what_was_done_including_what_failed(self):
        path = self.build()["foodchat"]["conversations"][0]["path"]
        assert [p["step"] for p in path] == ["daily_plan", "refine_plan", "error", "saved"]

    def test_lookups_and_model_calls_are_the_turns_own(self):
        """A turn that produced nothing can be seen to have searched and found
        nothing — the explanation, next to the symptom."""
        conv = self.build()["foodchat"]["conversations"][0]
        second = conv["steps"][1]
        assert second["lookups"] == [
            {"query": "vegan stew", "results": 0, "zero_result": True, "relaxed": False}
        ]
        assert conv["steps"][0]["llm_calls"] == 2
        assert conv["lookups"] == 2
        assert conv["llm_calls"] == 3
        assert conv["failed_turns"] == 1

    def test_two_conversations_stay_two(self):
        from analytics.reports import _build_journey

        events = [
            chat_turn("a", 1, "chat", conversation="one"),
            chat_turn("b", 2, "daily_plan", conversation="two"),
        ]
        conversations = _build_journey(events, [], {}, [])["foodchat"]["conversations"]
        assert [c["conversation_id"] for c in conversations] == ["one", "two"]


class TestASessionsQuestions:
    def test_a_question_joins_its_retrieval_answer_and_rating(self):
        from analytics.reports import _build_journey

        events = [{
            "occurred_at": at(5),
            "event_type": "qa.answered",
            "request_id": "q1",
            "props": {"qa_request_id": "qa-9", "mode": "simple", "cache_hit": False},
        }]
        searches = [search("q1", 1, "qa", "is olive oil healthy", 7)]
        feedback = [{"target_type": "qa_answer", "target_id": "qa-9", "rating_value": "not_helpful"}]
        scholar = _build_journey(events, searches, {}, feedback)["foodscholar"]
        assert len(scholar["questions"]) == 1
        question = scholar["questions"][0]
        assert question["question"] == "is olive oil healthy"
        assert question["answered"] is True
        assert question["sources_found"] == 7
        assert question["rating"] == "not_helpful"
        assert scholar["totals"]["rated_negative"] == 1

    def test_a_question_is_not_counted_twice(self):
        """The browser reports asking and FoodScholar reports answering. Added
        together, as the session page used to, every question was two."""
        from analytics.reports import _build_journey

        events = [
            {"occurred_at": at(0), "event_type": "qa.ask", "request_id": None, "props": {}},
            {
                "occurred_at": at(4),
                "event_type": "qa.answered",
                "request_id": "q1",
                "props": {"qa_request_id": "qa-1"},
            },
        ]
        totals = _build_journey(events, [], {}, [])["foodscholar"]["totals"]
        assert totals["questions"] == 1

    def test_a_question_whose_answer_never_reported_still_counts(self):
        from analytics.reports import _build_journey

        events = [{"occurred_at": at(0), "event_type": "qa.ask", "request_id": None, "props": {}}]
        assert _build_journey(events, [], {}, [])["foodscholar"]["totals"]["questions"] == 1


class TestWhatCountsAsSomebodySearching:
    def test_lookups_questions_and_keystrokes_are_not_searches(self):
        """FoodChat's lookups, a question's retrieval and autocomplete all land
        in the search table. Counted as searches, one chat turn was three."""
        from analytics.reports import _build_journey

        searches = [
            search("a", 1, "recipes", "lentil soup"),
            search("b", 2, "tools", "chicken"),
            search("c", 3, "qa", "is fat bad"),
            search("d", 4, "autocomplete", "len"),
        ]
        totals = _build_journey([], searches, {}, [])["recipes"]["totals"]
        assert totals["searches"] == 1
        assert totals["suggestions"] == 1

    def test_opening_a_result_after_searching_is_noticed(self):
        from analytics.reports import _build_journey

        events = [{"occurred_at": at(10), "event_type": "recipe.view", "request_id": None, "props": {}}]
        searches = [search("a", 1, "recipes", "lentil soup")]
        assert _build_journey(events, searches, {}, [])["recipes"]["totals"]["searched_then_opened"]
        # A view before any search is browsing, not a search that worked.
        late = [search("a", 20, "recipes", "lentil soup")]
        assert not _build_journey(events, late, {}, [])["recipes"]["totals"]["searched_then_opened"]


class TestConversationFlows:
    """Across a period: the common shapes of a conversation and where they end."""

    @staticmethod
    def rows(conversation, steps, session="s1"):
        out = []
        for step in steps:
            if step == "plan":
                out.append({"event_type": "chat.plan_generated", "conversation_id": conversation,
                            "client_session_id": session, "refinement": False})
            elif step == "refined":
                out.append({"event_type": "chat.plan_generated", "conversation_id": conversation,
                            "client_session_id": session, "refinement": True})
            elif step == "saved":
                out.append({"event_type": "chat.plan_saved", "conversation_id": conversation,
                            "client_session_id": session})
            elif step in ("error", "busy"):
                out.append({"event_type": "chat.turn", "conversation_id": conversation,
                            "client_session_id": session, "intent": None, "outcome": step})
            else:
                out.append({"event_type": "chat.turn", "conversation_id": conversation,
                            "client_session_id": session, "intent": step, "outcome": "ok"})
        return out

    def flows(self):
        from analytics.reports import _conversation_flows

        rows = (
            self.rows("c1", ["daily_plan", "plan", "refine_plan", "refined", "saved"], "s1")
            + self.rows("c2", ["daily_plan", "plan", "refine_plan", "refined"], "s1")
            + self.rows("c3", ["chat", "error"], "s2")
            + self.rows("c4", ["refine_plan"] * 5, "s3")
        )
        return _conversation_flows(rows)

    def test_save_rate_is_of_conversations_that_made_a_plan(self):
        flows = self.flows()
        assert flows["conversations"] == 4
        assert flows["with_plan"] == 2
        assert flows["conversations_saved"] == 1
        assert flows["save_rate"] == 50.0

    def test_where_conversations_end_is_reported(self):
        exits = {row["step"]: row["conversations"] for row in self.flows()["exits"]}
        assert exits == {"saved": 1, "refine_plan": 2, "error": 1}

    def test_runs_of_one_step_group_as_three_or_more(self):
        paths = self.flows()["paths"]
        refining = [p for p in paths if p["path"][0]["step"] == "refine_plan"][0]
        assert refining["path"] == [{"step": "refine_plan", "repeats": 3, "or_more": True}]

    def test_transitions_include_how_conversations_start_and_end(self):
        pairs = {(t["from"], t["to"]): t["count"] for t in self.flows()["transitions"]}
        assert pairs[("start", "daily_plan")] == 2
        assert pairs[("daily_plan", "refine_plan")] == 2
        assert pairs[("error", "end")] == 1

    def test_plans_are_counted_per_browser_session(self):
        """Two conversations in one visit made four plans between them — the
        figure somebody means by "how many plans did they make"."""
        per_session = {row["bucket"]: row["count"] for row in self.flows()["plans_per_session"]}
        assert per_session["3–5"] == 1  # s1: two plans in each of two conversations
        assert per_session["0"] == 2  # s2 and s3 made none


class TestSessionCorrelation:
    """Rows a service reports carry the request, not the browser session."""

    def test_every_table_a_service_writes_is_linked(self):
        import sql as models
        from analytics.correlate import _SESSION_TARGETS

        tables = {
            "event": models.ActivityEvent,
            "search_query": models.SearchQuery,
            "llm_usage": models.LLMUsage,
            "feedback": models.FeedbackRecord,
        }
        assert set(_SESSION_TARGETS) == set(tables)
        for name in _SESSION_TARGETS:
            columns = {column.name for column in tables[name].__table__.columns}
            assert {"request_id", "client_session_id", "occurred_at", "id"} <= columns, name

    def test_a_request_shared_by_two_tabs_links_to_neither(self):
        from analytics.correlate import _RESOLVE_SESSION_IDS

        assert "HAVING count(DISTINCT client_session_id) = 1" in _RESOLVE_SESSION_IDS

    def test_only_blanks_and_only_recent_ones_are_filled(self):
        from analytics.correlate import _RESOLVE_SESSION_IDS

        assert "target.client_session_id IS NULL" in _RESOLVE_SESSION_IDS
        assert _RESOLVE_SESSION_IDS.count("make_interval(hours => :hours)") >= 2

    def test_the_batch_is_drawn_from_rows_that_can_be_filled(self):
        """Drawn from blank rows in general, it picked the same unfillable
        rows — SDK calls, crawlers — on every pass and starved the rest."""
        from analytics.correlate import _RESOLVE, _RESOLVE_SESSION_IDS

        for statement in (_RESOLVE, _RESOLVE_SESSION_IDS):
            pending = statement[statement.index("pending AS"):]
            assert pending.index("JOIN source") < pending.index("LIMIT :batch")

    def test_the_pass_runs_with_the_others(self):
        import inspect

        from analytics import correlate

        source = inspect.getsource(correlate.resolve_identities)
        assert "_RESOLVE_SESSION_IDS" in source


class TestClickMapSizing:
    """A click is a fraction of the visitor's page at their window's width, so
    the map has to say which widths its clicks came from."""

    def test_the_report_carries_the_device_split_and_the_window(self):
        import inspect

        from analytics.reports import click_map

        source = inspect.getsource(click_map)
        assert '"by_device"' in source
        assert '"viewport"' in source

    def test_the_device_split_ignores_the_selected_size(self):
        """Otherwise picking "phone" would report every click as a phone's,
        and the console could never offer the other sizes."""
        import inspect

        from analytics.reports import click_map

        source = inspect.getsource(click_map)
        split = source[source.index("sizes = ("):source.index("viewport = (")]
        assert "*page_filters" in split
        assert "*filters" not in split

    def test_rows_follow_the_page_not_the_column_count(self):
        """A square grid over a tall phone page draws stripes a cell's height
        away from the control; the console asks for as many rows as the page's
        shape needs, within a bound."""
        import inspect

        from analytics.reports import click_map

        source = inspect.getsource(click_map)
        assert "UIInteraction.y_pct * row_cells / 10000" in source
        assert "min(int(rows or cells), 400)" in source
        assert '"rows": row_cells' in source
