"""The activity recorder, and the DDL it writes into.

The recorder sits on the path of every request, so the tests that matter most
are the ones asserting what it does *not* do: raise, block, or grow without
bound. The rest cover consent, which is the part where a bug is not a bug but a
disclosure.

`schemas/50_analytics.sql` is applied to the live database **by hand** — this
platform has no migration tooling — so a model that has drifted from the DDL
fails at runtime in production, on an INSERT, silently swallowed by the
recorder's own error handling. `TestSchemaParity` is the guard against that.
"""
import asyncio
import os
import re

import pytest

import main  # noqa: F401 — `sql` imports `backend.postgres`, which needs config
import context
from analytics.recorder import ActivityRecorder, app_for_route, normalize_query, query_hash
from analytics.settings import DEFAULTS, SettingsCache


# --------------------------------------------------------------- helpers --
class _StubSettings:
    """Settings without a database behind them."""

    def __init__(self, **overrides):
        self.values = dict(DEFAULTS)
        self.values.update(overrides)
        self.refreshed = 0

    def current(self):
        return self.values

    async def refresh_if_stale(self):
        self.refreshed += 1
        return self.values

    def collecting(self, app):
        if self.values.get("paused"):
            return False
        return bool((self.values.get("apps") or {}).get(app, True))

    def captures(self, capability):
        return bool(self.values.get(f"capture.{capability}", True))

    def sample_rate(self, capability=None):
        # Mirrors SettingsCache: a capability may carry its own rate, and
        # falls back to the platform-wide one. The signature has to track the
        # real thing — the recorder swallows exceptions on the record path, so
        # a stub that drifts turns a TypeError into a silently dropped row and
        # a test that fails somewhere else entirely.
        if capability is not None:
            key = f"sample_rate.{capability}"
            if key in self.values:
                return float(self.values[key])
        return float(self.values.get("sample_rate", 1.0))

    def get(self, key, default=None):
        return self.values.get(key, DEFAULTS.get(key, default))


@pytest.fixture()
def recorder(monkeypatch):
    """A started-but-not-draining recorder, so the queue can be inspected."""
    import analytics.recorder as recorder_module

    stub = _StubSettings()
    monkeypatch.setattr(recorder_module, "SETTINGS", stub)
    rec = ActivityRecorder(queue_max=100)
    rec._enabled = True  # started without spawning the drain task
    rec.settings_stub = stub
    return rec


def _queued(rec):
    return [rec._queue.get_nowait() for _ in range(rec._queue.qsize())]


# ------------------------------------------------------------ normalising --
class TestNormalisation:
    def test_case_and_whitespace_collapse_to_one_key(self):
        assert normalize_query("  Vegan   DESSERTS ") == "vegan desserts"
        assert normalize_query("vegan desserts") == "vegan desserts"

    def test_same_text_hashes_the_same(self):
        """Trending has to count a query whose text we may not store."""
        a = query_hash(normalize_query("Vegan Desserts"))
        b = query_hash(normalize_query("vegan   desserts"))
        assert a == b and a is not None

    def test_different_text_hashes_differently(self):
        assert query_hash(normalize_query("vegan cake")) != query_hash(
            normalize_query("vegetarian cake")
        )

    def test_empty_input_yields_nothing(self):
        for value in ("", "   ", None):
            assert normalize_query(value) is None
            assert query_hash(normalize_query(value)) is None

    @pytest.mark.parametrize(
        "path,app",
        [
            ("/api/v1/foodchat/sessions/1/chat", "foodchat"),
            ("/api/v1/foodscholar/qa/ask", "foodscholar"),
            ("/api/v1/recipewrangler/recipes/search", "recipewrangler"),
            ("/api/v1/observability/dashboard", "console"),
            ("/api/v1/members/abc/profile", "platform"),
            (None, "platform"),
        ],
    )
    def test_route_maps_to_a_surface(self, path, app):
        assert app_for_route(path) == app


# ---------------------------------------------------------------- gating --
class TestGating:
    def test_disabled_recorder_records_nothing(self, recorder):
        recorder._enabled = False
        recorder.record_event("page.view", app="platform")
        recorder.record_search(surface="recipes", app="recipewrangler", raw_query="x")
        recorder.record_feedback(
            app="platform", target_type="platform", target_id=None,
            rating_kind="likert5", rating_value="5",
        )
        assert recorder._queue.qsize() == 0

    def test_pausing_stops_collection(self, recorder):
        recorder.settings_stub.values["paused"] = True
        recorder.record_event("page.view", app="platform")
        assert recorder._queue.qsize() == 0
        assert recorder.stats.dropped_disabled == 1

    def test_an_app_can_be_switched_off_alone(self, recorder):
        recorder.settings_stub.values["apps"] = {"foodchat": False, "platform": True}
        recorder.record_event("chat.message", app="foodchat")
        recorder.record_event("page.view", app="platform")
        assert len(_queued(recorder)) == 1

    def test_capture_flag_gates_its_own_kind(self, recorder):
        recorder.settings_stub.values["capture.http_requests"] = False
        recorder.record_event("http.request", app="platform", capability="http_requests")
        recorder.record_event("page.view", app="platform", capability="client_events")
        rows = _queued(recorder)
        assert [r.values["event_type"] for r in rows] == ["page.view"]

    def test_sampling_applies_only_to_sampled_events(self, recorder):
        recorder.settings_stub.values["sample_rate"] = 0.0
        recorder.record_event("http.request", app="platform", sampled=True)
        assert recorder._queue.qsize() == 0
        assert recorder.stats.dropped_sampled == 1
        # Feedback is a fact somebody took the trouble to give; never sampled.
        recorder.record_feedback(
            app="platform", target_type="platform", target_id=None,
            rating_kind="likert5", rating_value="2",
        )
        assert recorder._queue.qsize() == 1


# -------------------------------------------------------------- safety ----
class TestNeverHarmsTheRequest:
    def test_a_full_queue_drops_and_counts(self, recorder):
        for _ in range(150):  # queue_max is 100
            recorder.record_event("page.view", app="platform")
        assert recorder._queue.qsize() == 100
        assert recorder.stats.dropped_queue_full == 50
        assert recorder.stats.enqueued == 100

    def test_a_broken_context_does_not_raise(self, recorder, monkeypatch):
        import analytics.recorder as recorder_module

        def explode():
            raise RuntimeError("context is broken")

        monkeypatch.setattr(recorder_module.context, "snapshot", explode)
        recorder.record_event("page.view", app="platform")
        recorder.record_search(surface="recipes", app="recipewrangler", raw_query="x")
        recorder.record_llm_usage(app="foodchat", model="m")
        recorder.record_feedback(
            app="platform", target_type="platform", target_id=None,
            rating_kind="thumbs", rating_value="up",
        )
        assert recorder._queue.qsize() == 0  # nothing recorded, nothing raised

    def test_unserialisable_props_do_not_raise(self, recorder):
        recorder.record_event("page.view", app="platform", props={"o": object()})
        assert recorder._queue.qsize() == 1  # JSON encoding is the writer's problem


# ------------------------------------------------------------- identity ----
class TestIdentityCapture:
    def test_context_identity_is_snapshotted_at_record_time(self, recorder):
        token = context.bind(
            request_id="rid-1",
            user_sub="sub-1",
            user_roles=["expert"],
            client="wisefood-ui/1.0",
            client_session="sess-1",
            route="/api/v1/foodscholar/qa/ask",
        )
        context.set_member_id("member-1")
        recorder.record_event("qa.ask", props={"mode": "simple"})
        context.reset(token)

        row = _queued(recorder)[0]
        assert row.table == "event"
        assert row.values["request_id"] == "rid-1"
        assert row.values["user_id"] == "sub-1"
        assert row.values["member_id"] == "member-1"
        assert row.values["client"] == "wisefood-ui/1.0"
        assert row.values["client_session_id"] == "sess-1"
        # Route decides the surface when the caller does not name one.
        assert row.values["app"] == "foodscholar"

    def test_a_guest_is_marked_as_one(self, recorder):
        token = context.bind(
            request_id="rid-2", user_sub="guest-9", user_roles=["guest"]
        )
        recorder.record_event("page.view", app="platform")
        context.reset(token)
        assert _queued(recorder)[0].values["is_guest"] is True

    def test_search_records_both_result_counts(self, recorder):
        """The recipe search retries on an empty hit set, so the final count
        hides the original miss."""
        recorder.record_search(
            surface="recipes", app="recipewrangler", raw_query="pickled moon cheese",
            result_count_first_pass=0, result_count_final=5, relaxed=True,
        )
        values = _queued(recorder)[0].values
        assert values["result_count_first_pass"] == 0
        assert values["result_count_final"] == 5
        assert values["relaxed"] is True
        assert values["zero_result"] is False

    def test_a_genuine_zero_result_is_flagged(self, recorder):
        recorder.record_search(
            surface="recipes", app="recipewrangler", raw_query="nothing at all",
            result_count_first_pass=0, result_count_final=0,
        )
        assert _queued(recorder)[0].values["zero_result"] is True

    def test_token_total_is_derived_when_absent(self, recorder):
        recorder.record_llm_usage(
            app="foodchat", model="llama", input_tokens=100, output_tokens=25
        )
        assert _queued(recorder)[0].values["total_tokens"] == 125


# -------------------------------------------------------------- consent ----
class TestConsent:
    """Identity is stripped, the row is kept: a count of how many people
    searched for something is not personal data; a list of who they were is."""

    def _batch(self, recorder):
        token = context.bind(request_id="r", user_sub="sub-x", user_roles=["user"])
        context.set_member_id("member-x")
        recorder.record_event("page.view", app="platform")
        recorder.record_search(
            surface="recipes", app="recipewrangler", raw_query="secret diet"
        )
        context.reset(token)
        return _queued(recorder)

    def test_identity_is_stripped_when_consent_is_absent(self, recorder, monkeypatch):
        import analytics.recorder as recorder_module

        async def nobody(_ids):
            return set()

        monkeypatch.setattr(recorder_module.CONSENT, "allowed_for", nobody)
        batch = self._batch(recorder)
        asyncio.run(recorder._apply_consent(batch))

        for row in batch:
            assert row.values["user_id"] is None
            assert row.values["member_id"] is None
        search = next(r for r in batch if r.table == "search_query")
        # The text they typed goes with the identity; the normalised form and
        # the hash stay, so the query still counts towards trending.
        assert search.values["raw_query"] is None
        assert search.values["normalized_query"] == "secret diet"
        assert search.values["query_hash"] is not None
        assert recorder.stats.identities_stripped == 2

    def test_identity_is_kept_when_consent_is_present(self, recorder, monkeypatch):
        import analytics.recorder as recorder_module

        async def everyone(ids):
            return set(ids)

        monkeypatch.setattr(recorder_module.CONSENT, "allowed_for", everyone)
        batch = self._batch(recorder)
        asyncio.run(recorder._apply_consent(batch))

        for row in batch:
            assert row.values["user_id"] == "sub-x"
        search = next(r for r in batch if r.table == "search_query")
        assert search.values["raw_query"] == "secret diet"
        assert recorder.stats.identities_stripped == 0

    def test_raw_text_capture_can_be_off_for_consenting_users_too(
        self, recorder, monkeypatch
    ):
        import analytics.recorder as recorder_module

        async def everyone(ids):
            return set(ids)

        monkeypatch.setattr(recorder_module.CONSENT, "allowed_for", everyone)
        recorder.settings_stub.values["capture.raw_query_text"] = False
        batch = self._batch(recorder)
        asyncio.run(recorder._apply_consent(batch))

        search = next(r for r in batch if r.table == "search_query")
        assert search.values["raw_query"] is None
        assert search.values["query_hash"] is not None
        assert search.values["user_id"] == "sub-x"  # identity is a separate question


class TestConsentModeDefault:
    def test_the_default_is_the_conservative_reading(self, monkeypatch):
        from analytics.settings import consent_mode

        monkeypatch.delenv("ANALYTICS_CONSENT_MODE", raising=False)
        assert consent_mode() == "opt_in"

    def test_an_unrecognised_mode_falls_back_to_opt_in(self, monkeypatch):
        from analytics.settings import consent_mode

        monkeypatch.setenv("ANALYTICS_CONSENT_MODE", "whatever")
        assert consent_mode() == "opt_in"

    def test_opt_out_is_honoured_when_asked_for(self, monkeypatch):
        from analytics.settings import consent_mode

        monkeypatch.setenv("ANALYTICS_CONSENT_MODE", "opt_out")
        assert consent_mode() == "opt_out"


class TestSettingsCacheDegradesSafely:
    def test_an_unreachable_settings_table_keeps_the_last_values(self, monkeypatch):
        """A settings table nobody can read must not start collection, and must
        not stop it either.

        The failure is forced rather than relied upon: the earlier version
        assumed no database was reachable, so it passed in any environment,
        including one where the table exists and is simply empty.
        """
        cache = SettingsCache(ttl=0.0)
        cache._values = dict(DEFAULTS, paused=True)
        before = dict(cache.current())

        import analytics.settings as settings_module

        def explode():
            raise RuntimeError("settings table unreachable")

        monkeypatch.setattr(settings_module, "consent_mode", explode, raising=False)
        monkeypatch.setattr(
            "backend.postgres.POSTGRES_ASYNC_SESSION_FACTORY", explode, raising=False
        )
        asyncio.run(cache.refresh_if_stale())
        assert cache.current() == before
        assert cache.collecting("foodchat") is False  # still paused, as it was


# --------------------------------------------------------------- parity ----
class TestSchemaParity:
    """The DDL is applied by hand; drift would only show up in production."""

    @staticmethod
    def _ddl_columns():
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        sql_text = open(os.path.join(here, "schemas", "50_analytics.sql")).read()
        tables = {}
        for match in re.finditer(
            r"CREATE TABLE IF NOT EXISTS analytics\.(\w+)\s*\((.*?)\n\);",
            sql_text,
            re.S,
        ):
            name, body = match.group(1), match.group(2)
            columns = set()
            for line in body.splitlines():
                line = line.strip()
                if not line or line.startswith("--"):
                    continue
                if re.match(r"^(PRIMARY|UNIQUE|FOREIGN|CONSTRAINT|CHECK)\b", line, re.I):
                    continue
                col = re.match(r"^(\w+)\s+\S", line)
                if col:
                    columns.add(col.group(1))
            tables[name] = columns
        return tables

    def test_every_model_column_exists_in_the_ddl(self):
        import sql as models

        ddl = self._ddl_columns()
        assert set(ddl) == {
            "event",
            "search_query",
            "llm_usage",
            "feedback",
            "expert_review",
            "settings",
        }, f"unexpected tables in DDL: {sorted(ddl)}"

        for model in (
            models.ActivityEvent,
            models.SearchQuery,
            models.LLMUsage,
            models.FeedbackRecord,
            models.ExpertReview,
            models.AnalyticsSetting,
        ):
            table = model.__table__.name
            mapped = {c.name for c in model.__table__.columns}
            missing = mapped - ddl[table]
            extra = ddl[table] - mapped
            assert not missing, f"analytics.{table}: model columns absent from DDL: {sorted(missing)}"
            assert not extra, f"analytics.{table}: DDL columns absent from model: {sorted(extra)}"

    def test_the_ddl_is_written_to_be_reapplied(self):
        """entrypoint.sh re-runs every schema file; a bare CREATE would abort it."""
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        sql_text = open(os.path.join(here, "schemas", "50_analytics.sql")).read()
        creates = re.findall(r"CREATE (TABLE|INDEX|SCHEMA)(?! IF NOT EXISTS)", sql_text)
        assert not creates, f"non-idempotent statements: {creates}"


class TestShutdown:
    """A restarting pod is exactly when you most want the record.

    The worker spends most of its life holding a partly-assembled batch,
    waiting for the flush interval to expire. An earlier version cancelled the
    task at shutdown, which threw that batch away — losing up to a full batch
    on every deploy, silently, with the stats still reading zero errors.
    """

    def test_events_held_by_the_worker_survive_shutdown(self, monkeypatch):
        import analytics.recorder as recorder_module

        stub = _StubSettings()
        monkeypatch.setattr(recorder_module, "SETTINGS", stub)

        written = []

        async def run():
            rec = ActivityRecorder(queue_max=50)

            async def capture(batch):
                written.extend(batch)

            rec._write = capture
            rec.start(enabled=True)

            rec.record_event("page.view", app="platform")
            rec.record_event("recipe.view", app="recipewrangler")
            # Hand control to the worker so it picks the events up and settles
            # into waiting for more — the state the old code lost them in.
            await asyncio.sleep(0.05)
            assert rec._queue.qsize() == 0, "worker should be holding the batch"

            await rec.stop()
            return rec

        rec = asyncio.run(run())
        assert [row.values["event_type"] for row in written] == [
            "page.view",
            "recipe.view",
        ]
        assert rec._task is None

    def test_stopping_an_unstarted_recorder_is_harmless(self):
        rec = ActivityRecorder()
        asyncio.run(rec.stop())

    def test_events_recorded_after_stop_are_ignored(self, monkeypatch):
        import analytics.recorder as recorder_module

        monkeypatch.setattr(recorder_module, "SETTINGS", _StubSettings())

        async def run():
            rec = ActivityRecorder(queue_max=50)
            rec._write = lambda batch: asyncio.sleep(0)
            rec.start(enabled=True)
            await rec.stop()
            rec.record_event("page.view", app="platform")
            return rec

        rec = asyncio.run(run())
        assert rec._queue.qsize() == 0


class TestTrustedIdentityOverride:
    """Only the signed service ingest may say who an event belongs to."""

    def test_an_override_replaces_the_context(self, recorder):
        token = context.bind(request_id="rid-service", user_sub=None)
        recorder.record_event(
            "chat.plan_generated",
            app="foodchat",
            identity={"user_id": "sub-real", "member_id": "member-real"},
        )
        context.reset(token)
        values = _queued(recorder)[0].values
        assert values["user_id"] == "sub-real"
        assert values["member_id"] == "member-real"
        # Not overridden, so the service's own request id stands.
        assert values["request_id"] == "rid-service"

    def test_absent_override_fields_leave_the_context_alone(self, recorder):
        token = context.bind(request_id="rid-1", user_sub="sub-ctx")
        recorder.record_search(
            surface="recipes",
            app="recipewrangler",
            raw_query="x",
            identity={"member_id": "member-stated"},
        )
        context.reset(token)
        values = _queued(recorder)[0].values
        assert values["user_id"] == "sub-ctx"
        assert values["member_id"] == "member-stated"

    def test_no_override_is_the_normal_path(self, recorder):
        token = context.bind(request_id="rid-1", user_sub="sub-ctx")
        recorder.record_llm_usage(app="foodchat", model="m")
        context.reset(token)
        assert _queued(recorder)[0].values["user_id"] == "sub-ctx"


class TestConsentIsEvaluatedAtWriteTime:
    """Not at record time — and the asymmetry that produces is deliberate."""

    def test_withdrawing_also_covers_events_still_in_the_queue(self, recorder, monkeypatch):
        import analytics.recorder as recorder_module

        token = context.bind(request_id="r", user_sub="sub-w", user_roles=["user"])
        recorder.record_event("page.view", app="platform")
        context.reset(token)
        batch = _queued(recorder)

        # They withdraw before the batch is flushed.
        async def nobody(_ids):
            return set()

        monkeypatch.setattr(recorder_module.CONSENT, "allowed_for", nobody)
        asyncio.run(recorder._apply_consent(batch))
        assert batch[0].values["user_id"] is None

    def test_granting_does_not_reach_back_into_the_queue(self, recorder, monkeypatch):
        """The opposite lean would attribute activity from before they agreed."""
        import analytics.recorder as recorder_module

        token = context.bind(request_id="r", user_sub="sub-g", user_roles=["user"])
        recorder.record_event("page.view", app="platform")
        context.reset(token)
        batch = _queued(recorder)

        async def everyone(ids):
            return set(ids)

        monkeypatch.setattr(recorder_module.CONSENT, "allowed_for", everyone)
        asyncio.run(recorder._apply_consent(batch))
        # Granted by the time it was written, so it is attributed — the window
        # is one flush interval, not the whole session.
        assert batch[0].values["user_id"] == "sub-g"

    def test_a_consent_lookup_failure_drops_the_batch(self, recorder, monkeypatch):
        """Writing an identity we could not verify is the one error that cannot
        be undone, so the whole batch is abandoned instead."""
        import analytics.recorder as recorder_module

        async def broken(_ids):
            raise RuntimeError("consent ledger unreachable")

        monkeypatch.setattr(recorder_module.CONSENT, "allowed_for", broken)

        # Patch the real insert path, not an invented attribute: an earlier
        # version asserted on `recorder._write_rows`, which does not exist, so
        # the assertion held no matter what the code did.
        inserted = []

        async def fake_insert(grouped, tables, quiet=False):
            inserted.append(grouped)
            return True

        recorder._insert = fake_insert

        token = context.bind(request_id="r", user_sub="sub-f")
        recorder.record_event("page.view", app="platform")
        context.reset(token)
        batch = _queued(recorder)

        asyncio.run(recorder._write(batch))
        assert inserted == [], "wrote rows despite not knowing whether consent allowed it"
        assert recorder.stats.write_errors == 1
        assert recorder.stats.written == 0
        assert "consent" in (recorder.stats.last_error or "")


class TestSessionIsCarriedEverywhere:
    """The id shown in the footer has to land on every kind of row.

    The point of showing a user their session id is that quoting it finds
    everything they did — searches, questions, meal plans, feedback. An earlier
    version dropped `client_session_id` from every table except `event`, which
    would have made "how many searches in this session" a join through the
    event table, and "which of those found nothing" impossible.
    """

    def _record_all(self, recorder):
        token = context.bind(
            request_id="r-1",
            user_sub="sub-1",
            client="wisefood-ui/1",
            client_session="k3f9-2xa7-lm4q",
        )
        recorder.record_event("qa.ask", app="foodscholar")
        recorder.record_search(
            surface="recipes", app="recipewrangler", raw_query="tofu"
        )
        recorder.record_llm_usage(app="foodchat", model="llama")
        recorder.record_feedback(
            app="platform", target_type="platform", target_id=None,
            rating_kind="likert5", rating_value="4",
        )
        context.reset(token)
        return _queued(recorder)

    def test_every_table_gets_the_session_id(self, recorder):
        rows = self._record_all(recorder)
        assert {row.table for row in rows} == {
            "event",
            "search_query",
            "llm_usage",
            "feedback",
        }
        for row in rows:
            assert row.values["client_session_id"] == "k3f9-2xa7-lm4q", row.table

    def test_the_session_id_is_a_column_on_every_table_that_records_one(self):
        """A value the recorder sets but the table lacks is silently lost on
        INSERT — caught here rather than in production."""
        import sql as models

        for model in (
            models.ActivityEvent,
            models.SearchQuery,
            models.LLMUsage,
            models.FeedbackRecord,
        ):
            assert "client_session_id" in {c.name for c in model.__table__.columns}, (
                model.__table__.name
            )

    def test_expert_actions_carry_no_session(self):
        """An expert review is not a user's browsing session, and giving it one
        would put reviewer activity into the reports about users."""
        import sql as models

        for model in (models.ExpertReview, models.AnalyticsSetting):
            assert "client_session_id" not in {
                c.name for c in model.__table__.columns
            }

    def test_a_hostile_session_header_never_reaches_a_row(self, recorder):
        """The id is caller-supplied and lands in log lines and reports."""
        token = context.bind(
            request_id="r-2", client_session=context.clean_id("bad session\nid")
        )
        recorder.record_event("page.view", app="platform")
        context.reset(token)
        assert _queued(recorder)[0].values["client_session_id"] is None


class TestAppClassificationIsSegmentPrecise:
    """A substring match filed `/users/me/analytics-consent` under `console`."""

    @pytest.mark.parametrize(
        "route,app",
        [
            ("/api/v1/users/me/analytics-consent", "platform"),
            ("/api/v1/analytics/events", "console"),
            ("/rest/api/v1/foodchat/sessions/{id}/chat", "foodchat"),
            ("/api/v1/members/{member_id}/favorites", "platform"),
            ("/api/v1/observability/dashboard", "console"),
            ("/api/v1/recipewrangler/recipes/search", "recipewrangler"),
        ],
    )
    def test_first_segment_decides(self, route, app):
        assert app_for_route(route) == app


class TestHouseholdIsRecordedAndStripped:
    """`household_id` was a column on `event` that the recorder hardcoded to
    None, while `verify_member_access` had the household in hand the whole
    time."""

    def test_household_lands_on_events(self, recorder):
        token = context.bind(request_id="r", user_sub="sub-h")
        context.set_member_id("member-h")
        context.set_household_id("household-h")
        recorder.record_event("recipe.view", app="recipewrangler")
        context.reset(token)
        row = _queued(recorder)[0]
        assert row.values["household_id"] == "household-h"

    def test_household_is_an_identity_and_is_stripped_with_it(self, recorder, monkeypatch):
        import analytics.recorder as recorder_module

        async def nobody(_ids):
            return set()

        monkeypatch.setattr(recorder_module.CONSENT, "allowed_for", nobody)
        token = context.bind(request_id="r", user_sub="sub-h")
        context.set_household_id("household-h")
        recorder.record_event("recipe.view", app="recipewrangler")
        context.reset(token)
        batch = _queued(recorder)
        asyncio.run(recorder._apply_consent(batch))
        assert batch[0].values["household_id"] is None

    def test_tables_without_the_column_never_receive_it(self, recorder):
        """A key the table lacks makes the whole batch INSERT fail — and the
        recorder swallows that, so every row in the batch would vanish."""
        import sql as models

        token = context.bind(request_id="r", user_sub="sub-h")
        context.set_household_id("household-h")
        recorder.record_search(surface="recipes", app="recipewrangler", raw_query="x")
        recorder.record_llm_usage(app="foodchat", model="m")
        recorder.record_feedback(app="platform", target_type="platform", target_id=None,
                                 rating_kind="thumbs", rating_value="up")
        context.reset(token)
        for row in _queued(recorder):
            table = {
                "search_query": models.SearchQuery,
                "llm_usage": models.LLMUsage,
                "feedback": models.FeedbackRecord,
            }[row.table].__table__
            columns = {c.name for c in table.columns}
            unknown = set(row.values) - columns
            assert not unknown, f"{row.table}: recorder sets {sorted(unknown)} which the table lacks"


class TestClientPropsAreNotATextLoophole:
    """`props` is client-supplied JSON. Without this, a page could put the
    search text in `props.q` and it would survive the stripping that exists to
    remove exactly that."""

    def _stripped(self, recorder, monkeypatch, props):
        import analytics.recorder as recorder_module

        async def nobody(_ids):
            return set()

        monkeypatch.setattr(recorder_module.CONSENT, "allowed_for", nobody)
        token = context.bind(request_id="r", user_sub="sub-p")
        recorder.record_event("recipe.search", app="recipewrangler", props=props)
        context.reset(token)
        batch = _queued(recorder)
        asyncio.run(recorder._apply_consent(batch))
        return batch[0].values["props"]

    def test_free_text_keys_go_with_the_identity(self, recorder, monkeypatch):
        kept = self._stripped(
            recorder, monkeypatch,
            {"q": "gluten free birthday cake", "results": 4, "zero_result": False},
        )
        assert kept == {"results": 4, "zero_result": False}

    def test_counters_and_ids_survive(self, recorder, monkeypatch):
        kept = self._stripped(
            recorder, monkeypatch, {"recipe_id": "r-1", "rank": 3, "from_cache": True}
        )
        assert kept == {"recipe_id": "r-1", "rank": 3, "from_cache": True}

    def test_props_are_untouched_for_a_consenting_user(self, recorder, monkeypatch):
        import analytics.recorder as recorder_module

        async def everyone(ids):
            return set(ids)

        monkeypatch.setattr(recorder_module.CONSENT, "allowed_for", everyone)
        token = context.bind(request_id="r", user_sub="sub-p")
        recorder.record_event("recipe.search", app="recipewrangler", props={"q": "cake"})
        context.reset(token)
        batch = _queued(recorder)
        asyncio.run(recorder._apply_consent(batch))
        assert batch[0].values["props"] == {"q": "cake"}


class TestOversizedServiceValuesCannotSinkABatch:
    """One bad field from one service used to erase up to 200 rows belonging to
    everyone, because a failed multi-row INSERT drops the whole batch."""

    def test_overlong_strings_are_truncated_to_their_column(self):
        import sql as models
        from analytics.recorder import ActivityRecorder

        coerced = ActivityRecorder._coerce(
            models.ActivityEvent.__table__,
            {"client_session_id": "x" * 200, "app": "platform", "event_type": "page.view"},
        )
        assert len(coerced["client_session_id"]) == 64

    def test_unparseable_numbers_become_null_rather_than_failing(self):
        import sql as models
        from analytics.recorder import ActivityRecorder

        coerced = ActivityRecorder._coerce(
            models.LLMUsage.__table__,
            {"app": "foodchat", "input_tokens": "n/a", "cost_usd": "free"},
        )
        assert coerced["input_tokens"] is None
        assert coerced["cost_usd"] is None

    def test_unknown_keys_are_dropped(self):
        import sql as models
        from analytics.recorder import ActivityRecorder

        coerced = ActivityRecorder._coerce(
            models.SearchQuery.__table__, {"app": "catalog", "not_a_column": "x"}
        )
        assert "not_a_column" not in coerced


class TestClientClockIsNotTrusted:
    def test_a_future_timestamp_is_replaced(self):
        from datetime import datetime, timedelta, timezone
        from analytics.recorder import _clamp_time

        future = datetime.now(timezone.utc) + timedelta(days=2)
        assert _clamp_time(future) <= datetime.now(timezone.utc)

    def test_an_ancient_timestamp_is_replaced(self):
        from datetime import datetime, timezone
        from analytics.recorder import _clamp_time

        assert _clamp_time(datetime(1970, 1, 1, tzinfo=timezone.utc)).year > 2000

    def test_a_plausible_timestamp_is_kept(self):
        from datetime import datetime, timedelta, timezone
        from analytics.recorder import _clamp_time

        recent = datetime.now(timezone.utc) - timedelta(seconds=30)
        assert _clamp_time(recent) == recent


class TestAFailedSearchIsNotAZeroResult:
    """A search that broke and a search that found nothing are different facts.

    Counting an outage as zero results would inflate the zero-result rate
    exactly when the product looks worst, and hide the outage behind it.
    """

    def test_absent_counts_do_not_set_zero_result(self, recorder):
        recorder.record_search(
            surface="recipes", app="recipewrangler", raw_query="tofu",
            result_count_first_pass=None, result_count_final=None,
        )
        values = _queued(recorder)[0].values
        assert values["zero_result"] is False
        assert values["result_count_final"] is None

    def test_a_genuine_zero_still_counts(self, recorder):
        recorder.record_search(
            surface="recipes", app="recipewrangler", raw_query="tofu",
            result_count_first_pass=0, result_count_final=0,
        )
        assert _queued(recorder)[0].values["zero_result"] is True


class TestSessionIdentityCorrelation:
    """A browser session is attributed from the activity recorded during it.

    The session row is written at the first beacon of a visit, often before
    the token exists, so it starts with no identity and no request to borrow
    one from. Nothing filled it in, which made the console contradict itself:
    the people report counts a person's sessions from `analytics.event` (whose
    identity *is* correlated) and said "3 sessions", while the session board
    filters `client_session.user_id` and showed none of them.

    Verified against a real Postgres separately; what is guarded here is that
    the statement keeps the properties that make it safe to run on a timer.
    """

    def test_it_joins_sessions_to_events_not_requests(self):
        from analytics.correlate import _RESOLVE_SESSIONS

        assert "UPDATE analytics.client_session" in _RESOLVE_SESSIONS
        assert "target.session_id = source.client_session_id" in _RESOLVE_SESSIONS
        # There is no request_id on this table to join through.
        assert "request_id" not in _RESOLVE_SESSIONS

    def test_a_session_two_people_shared_is_left_alone(self):
        from analytics.correlate import _RESOLVE_SESSIONS

        # Someone signs out and someone else signs in on the same tab. The
        # right answer is unknown, and unattributed beats wrongly attributed.
        assert "HAVING count(DISTINCT user_id) = 1" in _RESOLVE_SESSIONS

    def test_it_only_fills_blanks_and_only_recent_ones(self):
        from analytics.correlate import _RESOLVE_SESSIONS

        assert "target.user_id IS NULL" in _RESOLVE_SESSIONS
        assert "started_at >= now() - make_interval(hours => :hours)" in _RESOLVE_SESSIONS
        assert "LIMIT :batch" in _RESOLVE_SESSIONS

    def test_consent_is_inherited_rather_than_re_decided(self):
        from analytics.correlate import _RESOLVE_SESSIONS

        # event.user_id is already NULL for anyone who did not consent, so
        # requiring it non-null is the whole of the consent check.
        assert "AND user_id IS NOT NULL" in _RESOLVE_SESSIONS

    def test_the_pass_runs_with_the_others(self):
        import inspect

        from analytics import correlate

        source = inspect.getsource(correlate.resolve_identities)
        assert "_RESOLVE_SESSIONS" in source
        assert 'filled["client_session"]' in source
