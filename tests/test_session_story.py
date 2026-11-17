"""A session told once, in order, chapter by chapter.

The session page used to be a panel per source, and nobody could say what a
visit had been about without lining the panels up by hand. These pin how the
sources become one story: what a moment is, where a chapter starts, what a
chapter carries with it, and what is left out on purpose.
"""
from __future__ import annotations

import inspect
from datetime import datetime, timedelta, timezone

import main  # noqa: F401 — `sql` imports `backend.postgres`, which needs config

from analytics import reports, story
from analytics.story import build_story, product_of_path

T0 = datetime(2026, 11, 16, 9, 0, tzinfo=timezone.utc)


def at(seconds: int) -> datetime:
    return T0 + timedelta(seconds=seconds)


def iso(seconds: int) -> str:
    return at(seconds).isoformat()


def journey(conversations=(), questions=()):
    return {
        "foodchat": {"conversations": list(conversations)},
        "foodscholar": {"questions": list(questions)},
    }


def conversation(conversation_id, *steps):
    return {"conversation_id": conversation_id, "steps": list(steps)}


def turn(seconds, intent="daily_plan", outcome="ok", plan=None):
    return {
        "kind": "turn", "occurred_at": iso(seconds), "intent": intent, "outcome": outcome,
        "plan": plan, "lookups": [], "llm_calls": 2, "cost_usd": 0.001, "latency_ms": 900,
    }


def page(seconds, path):
    return {"occurred_at": at(seconds), "event_type": "page.view", "app": "platform",
            "props": {"path": path}}


def rating(seconds, value="down", target_type="chat_message", target_id="41", **extra):
    return {"id": 7, "occurred_at": iso(seconds), "app": "foodchat", "target_type": target_type,
            "target_id": target_id, "rating_value": value, **extra}


def kinds(result):
    return [[m["kind"] for m in chapter["moments"]] for chapter in result["chapters"]]


class TestWhereChaptersStart:
    def test_moving_to_another_product_starts_one(self):
        result = build_story(
            journey=journey([conversation("c1", turn(10), turn(40))]),
            searches=[{"occurred_at": iso(100), "surface": "recipes", "query": "lentils",
                       "results": 0, "zero_result": True}],
            events=[page(0, "/foodchat"), page(90, "/recipe-wrangler")],
            feedback=[],
        )
        assert [c["product"] for c in result["chapters"]] == ["foodchat", "recipes"]
        assert kinds(result) == [["page", "turn", "turn"], ["page", "search"]]

    def test_a_new_conversation_starts_one_in_the_same_product(self):
        result = build_story(
            journey=journey([conversation("c1", turn(10)), conversation("c2", turn(60))]),
            searches=[], events=[], feedback=[],
        )
        assert [c["conversation_id"] for c in result["chapters"]] == ["c1", "c2"]

    def test_half_an_hour_away_starts_one(self):
        result = build_story(
            journey=journey([conversation("c1", turn(10), turn(10 + 31 * 60))]),
            searches=[], events=[], feedback=[],
        )
        assert len(result["chapters"]) == 2

    def test_errors_and_frustration_stay_where_they_happened(self):
        """They are evidence about the chapter, not a place somebody went."""
        result = build_story(
            journey=journey([conversation("c1", turn(10), turn(50))]),
            searches=[],
            events=[{"occurred_at": at(30), "event_type": "http.request", "app": "foodchat",
                     "route": "/foodchat/chat", "status": 502, "props": {}}],
            feedback=[],
            frustration=[{"occurred_at": at(35), "kind": "rage", "path": "/foodchat",
                          "element_key": "foodchat.composer › button", "repeats": 5}],
        )
        assert kinds(result) == [["turn", "error", "frustration", "turn"]]
        counts = result["chapters"][0]["counts"]
        assert counts["errors"] == 1 and counts["frustration"] == 5

    def test_the_home_page_is_its_own_chapter(self):
        result = build_story(
            journey=journey([conversation("c1", turn(30))]),
            searches=[], events=[page(0, "/"), page(5, "/profiles"), page(20, "/foodchat")],
            feedback=[],
        )
        assert [c["product"] for c in result["chapters"]] == ["other", "foodchat"]


class TestWhatAMomentCarries:
    def test_a_turn_keeps_its_plan_and_its_place_in_the_conversation(self):
        plan = {"plan_id": "p1", "plan_type": "daily", "version": 1}
        result = build_story(
            journey=journey([conversation("c1", turn(10, intent="greeting"), turn(30, plan=plan))]),
            searches=[], events=[], feedback=[],
        )
        second = result["chapters"][0]["moments"][1]
        assert second["number"] == 2 and second["plan"] == plan
        assert second["offset_seconds"] == 20 and second["gap_seconds"] == 20
        assert result["chapters"][0]["counts"]["plans"] == 1

    def test_a_chat_rating_carries_its_reason_and_the_id_to_open_it_by(self):
        result = build_story(
            journey=journey([conversation("c1", turn(10))]),
            searches=[], events=[],
            feedback=[rating(20, comment="Too generic")],
        )
        rated = result["chapters"][0]["moments"][1]
        assert rated["kind"] == "rating" and rated["negative"] is True
        assert rated["comment"] == "Too generic" and rated["feedback_id"] == 7
        assert rated["target_id"] == "41"
        assert result["chapters"][0]["counts"]["rated_down"] == 1

    def test_a_question_carries_its_own_rating_rather_than_a_second_moment(self):
        question = {"occurred_at": iso(5), "question": "Is okra safe?", "answered": True,
                    "qa_request_id": "q9", "rating": "not_helpful"}
        result = build_story(
            journey=journey(questions=[question]), searches=[], events=[],
            feedback=[rating(9, value="not_helpful", target_type="qa_answer",
                             target_id="q9", reason="wrong")],
        )
        assert kinds(result) == [["question"]]
        moment = result["chapters"][0]["moments"][0]
        assert moment["rating"]["reason"] == "wrong" and moment["rating"]["negative"] is True

    def test_successful_requests_and_duplicated_events_are_not_moments(self):
        result = build_story(
            journey=journey(), searches=[],
            events=[
                {"occurred_at": at(1), "event_type": "http.request", "status": 200, "props": {}},
                {"occurred_at": at(2), "event_type": "chat.turn", "props": {}},
                {"occurred_at": at(3), "event_type": "recipe.autocomplete", "props": {}},
                {"occurred_at": at(4), "event_type": "recipe.view", "app": "recipewrangler",
                 "props": {"recipe_id": "r1", "source": "search"}},
            ],
            feedback=[],
        )
        assert kinds(result) == [["recipe_view"]]
        assert result["chapters"][0]["moments"][0]["recipe_id"] == "r1"

    def test_only_allowlisted_props_reach_a_generic_event(self):
        result = build_story(
            journey=journey(), searches=[],
            events=[{"occurred_at": at(1), "event_type": "feature.used", "app": "platform",
                     "props": {"feature": "help", "action": "open", "free_text": "my address"}}],
            feedback=[],
        )
        moment = result["chapters"][0]["moments"][0]
        assert moment["feature"] == "help" and "free_text" not in moment


class TestItIsBounded:
    def test_a_long_session_says_how_much_it_left_out(self, monkeypatch):
        monkeypatch.setattr(story, "MAX_STORY_MOMENTS", 3)
        result = build_story(
            journey=journey([conversation("c1", *[turn(i * 10) for i in range(5)])]),
            searches=[], events=[], feedback=[],
        )
        assert result["moments"] == 3 and result["total"] == 5 and result["truncated"]

    def test_an_empty_session_has_no_chapters(self):
        assert build_story(journey=journey(), searches=[], events=[], feedback=[]) == {
            "chapters": [], "moments": 0, "total": 0, "truncated": False,
        }

    def test_paths_name_products_by_whole_segments(self):
        assert product_of_path("/foodchat") == "foodchat"
        assert product_of_path("/foodscholar/guides/gr") == "foodscholar"
        assert product_of_path("/foodchatter") == "other"
        assert product_of_path(None) == "other"


class TestTheReportsReadIt:
    def test_the_session_summary_tells_the_story(self):
        source = inspect.getsource(reports.session_summary)
        assert '"story": story' in source
        # A click's label passes the same guard on this read as on every other.
        assert "label_allowed(role, key)" in source

    def test_the_board_can_be_narrowed_to_what_needs_a_look(self):
        source = inspect.getsource(reports.session_board)
        for activity in ('"rated_down"', '"frustrated"', '"needs_look"'):
            assert activity in source
        assert '"down_reasons"' in source and '"frustrated_clicks"' in source


class TestReadingAPlan:
    def test_only_reviewers_may_and_each_read_is_recorded(self):
        from routers import analytics

        route = next(r for r in analytics.router.routes if r.path.endswith("/foodchat/plans/{plan_id}"))
        dependency = route.dependencies[0].dependency
        required = inspect.getclosurevars(dependency).nonlocals.get("required")
        assert sorted(required) == ["admin", "expert"]
        assert '"expert.plan_read"' in inspect.getsource(analytics.foodchat_plan_review)

    def _read(self, monkeypatch, outcome):
        import asyncio

        from backend.foodchat import FOODCHAT

        async def get(endpoint, *args, **kwargs):
            assert endpoint == "/foodchat/review/plans/p1"
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        monkeypatch.setattr(FOODCHAT, "review_get", get)
        return asyncio.run(reports.plan_review("p1"))

    def test_a_plan_comes_back(self, monkeypatch):
        result = self._read(monkeypatch, {"plan_id": "p1", "meals": []})
        assert result["available"] and result["plan"]["plan_id"] == "p1"

    def test_a_pruned_plan_is_a_sentence(self, monkeypatch):
        from exceptions import NotFoundError

        result = self._read(monkeypatch, NotFoundError(detail="gone"))
        assert not result["available"] and "no longer stored" in result["reason"]

    def test_a_silent_service_is_a_different_sentence(self, monkeypatch):
        result = self._read(monkeypatch, RuntimeError("connection refused"))
        assert not result["available"] and result["reason"] == "FoodChat did not answer."


class TestReadingAConversation:
    """What was said in a FoodChat conversation, opened from a session's story."""

    def test_only_reviewers_may_and_each_read_is_recorded(self):
        from routers import analytics

        route = next(
            r for r in analytics.router.routes
            if r.path.endswith("/foodchat/conversations/{session_id}")
        )
        dependency = route.dependencies[0].dependency
        required = inspect.getclosurevars(dependency).nonlocals.get("required")
        assert sorted(required) == ["admin", "expert"]
        assert '"expert.conversation_read"' in inspect.getsource(analytics.foodchat_conversation_review)

    def _read(self, monkeypatch, outcome):
        import asyncio

        from backend.foodchat import FOODCHAT

        async def get(endpoint, *args, **kwargs):
            assert endpoint == "/foodchat/review/sessions/c1/transcript"
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        monkeypatch.setattr(FOODCHAT, "review_get", get)
        return asyncio.run(reports.conversation_transcript("c1"))

    def test_a_conversation_comes_back(self, monkeypatch):
        result = self._read(monkeypatch, {"session_id": "c1", "messages": [], "plans": []})
        assert result["available"] and result["transcript"]["session_id"] == "c1"

    def test_a_deleted_conversation_is_a_sentence(self, monkeypatch):
        from exceptions import NotFoundError

        result = self._read(monkeypatch, NotFoundError(detail="gone"))
        assert not result["available"] and "no longer stored" in result["reason"]

    def test_a_silent_service_is_a_different_sentence(self, monkeypatch):
        result = self._read(monkeypatch, RuntimeError("connection refused"))
        assert not result["available"] and result["reason"] == "FoodChat did not answer."


class TestReadingARatedExchange:
    def test_the_foodchat_client_it_names_exists(self):
        """It imported a client that never existed, so every chat rating's
        exchange read as "the service did not answer"."""
        import backend.foodchat as client

        source = inspect.getsource(reports.feedback_context)
        assert "from backend.foodchat import FOODCHAT" in source
        assert hasattr(client, "FOODCHAT")
        assert "except NotFoundError" in source

    def test_review_reads_are_signed_as_the_reviewer(self, monkeypatch):
        """FoodChat's review routes accept the reviewer and nobody else."""
        import asyncio

        from backend.foodchat import FOODCHAT, FoodChat

        sent = {}

        class _Client:
            async def request(self, method, endpoint, **kwargs):
                import httpx

                sent.update(kwargs.get("headers") or {})
                return httpx.Response(200, json={}, request=httpx.Request(method, "http://fc" + endpoint))

        monkeypatch.setenv("FOODCHAT_ASSERTION_SECRET", "s3cret")
        monkeypatch.setattr(FoodChat, "_require_client", classmethod(lambda cls: _Client()))
        asyncio.run(FOODCHAT.review_get("/foodchat/review/plans/p1"))
        assert sent[FoodChat.ASSERTION_HEADER].startswith(FoodChat.REVIEWER + ".")
        for read in (reports.feedback_context, reports.plan_review):
            assert "FOODCHAT.review_get(" in inspect.getsource(read)
