"""Real user monitoring: the device, the errors, the clicks and the speed.

The schema files here are applied to the live database **by hand** — this
platform has no migration tooling — so a model that has drifted from the DDL
fails at runtime in production, on an INSERT, silently swallowed by the
recorder's own error handling. `TestSchemaParity` is the guard.

The other thing worth guarding is redaction. Everything in this module records
text a browser assembled from whatever it had in scope, which is the one place
on the platform where a token or an email can arrive without anybody having
decided to send it.
"""
import os
import re

import pytest

import main  # noqa: F401 — `sql` imports `backend.postgres`, which needs config
from analytics.device import (
    country_from_headers,
    parse_user_agent,
    truncate_ip,
)
from analytics.pricing import DEFAULT_PRICES, estimate_cost, normalise, rate_for
from analytics.recorder import (
    _clean_breadcrumbs,
    _culprit,
    _fingerprint,
    _prop_is_safe,
    _redact,
)

UA_MAC = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.4 Safari/605.1.15"
)
UA_IPHONE = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1"
)
UA_WINDOWS = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
UA_EDGE = (
    "Mozilla/5.0 (Windows NT 10.0) AppleWebKit/537.36 Chrome/120 Safari/537.36 "
    "Edg/120.0.0.0"
)

STACK = (
    "TypeError: Cannot read properties of undefined (reading 'id')\n"
    "    at useRecipe (https://app/_nuxt/vendor.js:1:200)\n"
    "    at setup (https://app/_nuxt/RecipeCard.vue:41:9)"
)


# ------------------------------------------------------------- the device --
class TestUserAgent:
    def test_the_common_browsers_are_named(self):
        assert parse_user_agent(UA_MAC)["browser"] == "Safari"
        assert parse_user_agent(UA_WINDOWS)["browser"] == "Chrome"
        assert parse_user_agent(UA_IPHONE)["os"] == "iOS"

    def test_edge_is_not_reported_as_chrome(self):
        """Edge's user agent claims to be Chrome, and Chrome's claims to be
        Safari. The order of the checks is the whole implementation."""
        assert parse_user_agent(UA_EDGE)["browser"] == "Edge"

    def test_windows_is_named_as_people_name_it(self):
        """Nobody calls it Windows NT 10.0."""
        assert parse_user_agent(UA_WINDOWS)["os_version"] == "10/11"

    def test_the_form_factor_is_derived(self):
        assert parse_user_agent(UA_IPHONE)["device_type"] == "mobile"
        assert parse_user_agent(UA_MAC)["device_type"] == "desktop"

    def test_a_crawler_is_named_and_not_analysed(self):
        """A bot's 'browser' is noise in every report; the useful fact is only
        that it was not a person."""
        parsed = parse_user_agent("Googlebot/2.1 (+http://www.google.com/bot.html)")
        assert parsed["is_bot"] is True
        assert parsed["device_type"] == "bot"
        assert parsed["browser"] is None

    def test_scripts_count_as_crawlers(self):
        for agent in ("python-requests/2.31.0", "curl/8.4.0", "axios/1.6.0"):
            assert parse_user_agent(agent)["is_bot"] is True

    def test_an_unrecognised_agent_answers_nothing_rather_than_guessing(self):
        """A partial guess files a visit under whatever matched loosest, which
        is worse than an honest 'unknown' bucket."""
        parsed = parse_user_agent("something nobody has ever sent")
        assert parsed == {
            "browser": None,
            "browser_version": None,
            "os": None,
            "os_version": None,
            "device_type": None,
            "is_bot": False,
        }

    def test_it_never_raises(self):
        for value in (None, "", "\x00", "x" * 10_000, "Mozilla/5.0 ("):
            parse_user_agent(value)


class TestAddress:
    def test_only_the_network_survives(self):
        assert truncate_ip("81.4.127.66") == "81.4.127.0/24"
        assert truncate_ip("2a02:8109:9c80:1234::5") == "2a02:8109:9c80::/48"

    def test_the_client_is_taken_from_a_forwarded_chain(self):
        assert truncate_ip("81.4.127.66, 10.0.0.1, 10.0.0.2") == "81.4.127.0/24"

    def test_a_port_is_not_mistaken_for_an_address(self):
        assert truncate_ip("1.2.3.4:8080") == "1.2.3.0/24"
        assert truncate_ip("[2a02::1]:443") == "2a02::/48"

    def test_nothing_useless_is_stored(self):
        for value in ("127.0.0.1", "garbage", "", None, "0.0.0.0"):
            assert truncate_ip(value) is None

    def test_a_full_address_can_never_come_back(self):
        """There is no flag that widens this. The part of an address that is
        useful here is the network; the part that makes it personal data is
        exactly the part being dropped."""
        for address in ("81.4.127.66", "10.11.12.13", "203.0.113.99"):
            assert truncate_ip(address).endswith(".0/24")

    def test_country_comes_only_from_the_ingress(self):
        assert country_from_headers({"cf-ipcountry": "si"}) == "SI"
        assert country_from_headers({"x-geo-country": "HU"}) == "HU"
        # Cloudflare's "unknown" and "Tor" are not countries.
        assert country_from_headers({"cf-ipcountry": "XX"}) is None
        assert country_from_headers({"cf-ipcountry": "T1"}) is None
        assert country_from_headers({}) is None


# ------------------------------------------------------------- the prices --
class TestPricing:
    def test_a_provider_prefix_and_a_tag_are_the_same_model(self):
        assert normalise("openai/gpt-oss-20b") == "gpt-oss-20b"
        assert normalise("GPT-OSS-120B:latest") == "gpt-oss-120b"

    def test_output_tokens_cost_more_than_input(self):
        """The reason a single total is the one number from which cost cannot
        be reconstructed."""
        rate_in, rate_out = DEFAULT_PRICES["gpt-oss-20b"]
        assert rate_out > rate_in
        assert estimate_cost(
            model="gpt-oss-20b", input_tokens=1_000_000, output_tokens=1_000_000
        ) == pytest.approx(rate_in + rate_out)

    def test_an_unknown_model_is_unpriced_rather_than_free(self):
        """A zero meaning 'we never priced this' and a zero meaning 'this was
        free' look identical in a report."""
        assert estimate_cost(model="a-model-nobody-configured", input_tokens=10) is None

    def test_a_bare_total_undercounts_knowably(self):
        """Applying the input rate to the lot undercounts, which is better than
        inventing an input/output split that was never measured."""
        rate_in, _ = DEFAULT_PRICES["llama-3.1-8b-instant"]
        assert estimate_cost(
            model="llama-3.1-8b-instant", total_tokens=1_000_000
        ) == pytest.approx(rate_in)

    def test_an_override_beats_the_built_in_table(self):
        assert rate_for("gpt-oss-20b", {"openai/gpt-oss-20b": [1.0, 2.0]}) == (1.0, 2.0)

    def test_a_malformed_override_falls_back_rather_than_pricing_at_nothing(self):
        assert rate_for("gpt-oss-20b", {"gpt-oss-20b": "nonsense"}) == DEFAULT_PRICES[
            "gpt-oss-20b"
        ]

    def test_a_self_hosted_model_may_be_declared_free(self):
        assert estimate_cost(model="bge-small-en-v1.5", total_tokens=10_000_000) == 0.0


# ------------------------------------------------------------ the redaction --
class TestRedaction:
    def test_an_email_never_reaches_storage(self):
        assert "bob@example.com" not in _redact("failed for bob@example.com")

    def test_a_token_never_reaches_storage(self):
        for secret in (
            "Bearer abcdefghijklmnopqrstuvwxyz012345",
            "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.abcdefghijklmnop",
            "9f2c1a4b5e6d7f8a9b0c1d2e3f4a5b6c",
        ):
            assert secret not in _redact(f"request failed: {secret}")

    def test_a_query_string_goes_and_the_path_stays(self):
        """The path is what makes an error findable; the parameters are what
        make it personal."""
        cleaned = _redact("GET /recipes/search?q=my+private+search failed")
        assert "/recipes/search" in cleaned
        assert "my+private" not in cleaned

    def test_a_breadcrumb_cannot_carry_what_someone_typed(self):
        crumbs = _clean_breadcrumbs(
            [{"type": "click", "query": "a private search", "url": "/a?token=zzz"}]
        )
        assert "query" not in crumbs[0]
        assert "zzz" not in crumbs[0]["url"]

    def test_only_the_most_recent_breadcrumbs_are_kept(self):
        assert len(_clean_breadcrumbs([{"i": n} for n in range(200)])) == 20

    def test_breadcrumbs_survive_a_shape_nobody_expected(self):
        assert _clean_breadcrumbs(None) == []
        assert _clean_breadcrumbs("not a list") == []


class TestGrouping:
    def test_the_same_fault_groups_despite_a_changing_index(self):
        """A message like 'undefined at index 41' differs on every occurrence.
        Grouping on the raw string produces one group per user, which is the
        failure mode that makes error tracking useless."""
        first = _fingerprint("ui", "error", "TypeError", "x at index 41", STACK, "/r")
        second = _fingerprint("ui", "error", "TypeError", "x at index 7", STACK, "/r")
        assert first == second

    def test_a_different_fault_stays_separate(self):
        first = _fingerprint("ui", "error", "TypeError", "x at index 41", STACK, "/r")
        other = _fingerprint("ui", "error", "TypeError", "a different fault", STACK, "/r")
        assert first != other

    def test_the_culprit_skips_a_bundled_dependency(self):
        """A frame inside node_modules says nothing about our own code."""
        assert _culprit(STACK, "/r") == "setup (/_nuxt/RecipeCard.vue)"

    def test_an_error_with_no_usable_stack_still_lands_somewhere(self):
        assert _culprit(None, "/recipe-wrangler/[id]") == "/recipe-wrangler/[id]"


class TestConditionalProps:
    def test_a_route_pattern_survives_identity_stripping(self):
        """Stripping `path` outright emptied the top-pages report for every
        user under opt-in consent — which is every user."""
        assert _prop_is_safe("path", "/recipe-wrangler/[id]") is True

    def test_a_resolved_url_does_not(self):
        assert _prop_is_safe("path", "/r/1842?q=secret+recipe") is False
        assert _prop_is_safe("path", "https://app/r/1842") is False

    def test_an_unrelated_prop_is_untouched(self):
        assert _prop_is_safe("results", 12) is True


# -------------------------------------------------------------- the schema --
class TestSchemaParity:
    """The DDL is applied by hand; drift would only show up in production."""

    @staticmethod
    def _ddl_columns():
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        sql_text = open(os.path.join(here, "schemas", "51_analytics_rum.sql")).read()
        tables = {}
        for match in re.finditer(
            r"CREATE TABLE IF NOT EXISTS analytics\.(\w+)\s*\((.*?)\n\);", sql_text, re.S
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
            "client_session",
            "error_group",
            "client_error",
            "ui_interaction",
            "web_vital",
        }, f"unexpected tables in DDL: {sorted(ddl)}"

        for model in (
            models.ClientSession,
            models.ErrorGroup,
            models.ClientError,
            models.UIInteraction,
            models.WebVital,
        ):
            mapped = {column.name for column in model.__table__.columns}
            declared = ddl[model.__tablename__]
            assert mapped == declared, (
                f"{model.__tablename__}: model-only {sorted(mapped - declared)}, "
                f"ddl-only {sorted(declared - mapped)}"
            )

    def test_the_ddl_is_re_runnable(self):
        """It is applied by hand against a live database, so a second run has
        to be harmless."""
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        sql_text = open(os.path.join(here, "schemas", "51_analytics_rum.sql")).read()
        statements = [
            line for line in sql_text.splitlines()
            if re.match(r"^\s*CREATE (TABLE|INDEX|SCHEMA)", line, re.I)
        ]
        assert statements
        for statement in statements:
            assert "IF NOT EXISTS" in statement.upper(), statement


class TestConsentReach:
    def test_the_raw_user_agent_is_treated_as_identity(self):
        """It is a fingerprint that singles a device out across visits. The
        parsed browser and OS are not, and stay."""
        from analytics.recorder import _TEXT_COLUMNS

        assert "user_agent" in _TEXT_COLUMNS["client_session"]

    def test_an_error_keeps_its_message_when_identity_is_stripped(self):
        """Redaction at record time is the privacy control for error text.
        Stripping it again here would leave an error report with no error in
        it — which, under opt-in consent, means every error report."""
        from analytics.recorder import _TEXT_COLUMNS

        stripped = _TEXT_COLUMNS["client_error"]
        assert "message" not in stripped
        assert "stack" not in stripped
        # `context` is arbitrary client JSON with no shape to redact against.
        assert "context" in stripped


# ------------------------------------------------------------- throughput --
class TestTheRequestPathIsNeverMadeToWait:
    """The whole design rests on one promise: recording an event costs the
    request nothing. These are the tests that would fail if somebody ever put
    I/O, a lock, or an exception path on it."""

    @pytest.fixture()
    def recorder(self, monkeypatch):
        import analytics.recorder as recorder_module
        from analytics.settings import DEFAULTS

        class _Settings:
            values = dict(DEFAULTS, **{"capture.interactions": True})

            def current(self):
                return self.values

            async def refresh_if_stale(self):
                return self.values

            def collecting(self, app):
                return True

            def captures(self, capability):
                return bool(self.values.get(f"capture.{capability}", True))

            def sample_rate(self, capability=None):
                return 1.0

            def get(self, key, default=None):
                return self.values.get(key, default)

        monkeypatch.setattr(recorder_module, "SETTINGS", _Settings())
        rec = recorder_module.ActivityRecorder(queue_max=100_000)
        rec._enabled = True  # started without spawning the drain task
        return rec

    def test_recording_does_no_io_and_takes_no_lock(self, recorder):
        """`record_event` is called from inside request handling. It enqueues
        and returns; anything else — a query, a lock, an await — would put the
        database's latency on a person's page load."""
        import inspect

        source = inspect.getsource(recorder.record_event)
        assert "await" not in source
        assert not inspect.iscoroutinefunction(recorder.record_event)

    def test_a_full_queue_drops_rather_than_blocks(self, recorder):
        """The bound exists so that a database outage costs data rather than
        availability. Dropping is the correct behaviour and it is counted."""
        recorder._queue = __import__("asyncio").Queue(maxsize=10)
        for _ in range(50):
            recorder.record_event("page.view", app="platform")
        assert recorder._queue.qsize() == 10
        assert recorder.stats.dropped_queue_full == 40

    def test_the_high_water_mark_survives_the_spike(self, recorder):
        """A depth reading taken after a burst has drained says nothing about
        how close the burst came to overflowing."""
        for _ in range(100):
            recorder.record_event("page.view", app="platform")
        while not recorder._queue.empty():
            recorder._queue.get_nowait()
        assert recorder._queue.qsize() == 0
        assert recorder.stats.queue_high_water == 100

    def test_thousands_of_events_are_accepted_without_awaiting(self, recorder):
        """The stated requirement is thousands a second. Enqueueing is a
        `put_nowait`, so the real figure is far higher — this asserts only that
        the path is synchronous and does not degrade with volume."""
        import time

        started = time.perf_counter()
        for index in range(20_000):
            recorder.record_event("page.view", app="platform", props={"n": index})
        elapsed = time.perf_counter() - started
        assert recorder.stats.enqueued == 20_000
        # Two seconds is a very loose bound for 20k enqueues; the point is to
        # fail loudly if I/O ever appears on this path, not to police the CPU.
        assert elapsed < 2.0, f"20k events took {elapsed:.2f}s — is there I/O on the record path?"

    def test_writes_run_alongside_assembly(self):
        """With one writer the loop was assemble, write, assemble, write, and
        the ceiling was one batch per round trip however fast events arrived."""
        from analytics.recorder import _WRITER_CONCURRENCY

        assert _WRITER_CONCURRENCY > 1

    def test_analytics_cannot_exhaust_the_pool_user_requests_share(self):
        """Background writes must never hold a connection somebody is waiting
        for, so they are served from a separate, hard-capped pool."""
        import inspect

        from analytics import db as analytics_db
        from analytics.recorder import ActivityRecorder

        assert "max_overflow=0" in inspect.getsource(analytics_db.session_factory)
        assert "analytics.db" in inspect.getsource(ActivityRecorder._insert)


class TestIdentityCorrelation:
    """FoodChat holds no Keycloak subject, so every row it reports arrives
    unattributed and every per-user report groups on `user_id`. Correlation is
    what stops a whole product being invisible in those reports."""

    def test_the_guest_column_is_only_set_where_it_exists(self):
        """`llm_usage` and `feedback` have no `is_guest` column. Setting it
        anyway failed the whole UPDATE, and because each table is its own
        transaction with a logged failure, those two were silently skipped for
        as long as the bug existed while the other two appeared to work."""
        import sql as models
        from analytics.correlate import _TARGETS

        tables = {
            "event": models.ActivityEvent,
            "search_query": models.SearchQuery,
            "llm_usage": models.LLMUsage,
            "feedback": models.FeedbackRecord,
        }
        for name, has_guest in _TARGETS.items():
            columns = {column.name for column in tables[name].__table__.columns}
            assert ("is_guest" in columns) is has_guest, name
            # Every target must have the two columns the join is built on.
            assert "request_id" in columns and "user_id" in columns, name

    def test_a_shared_request_id_attributes_nobody(self):
        """A caller may supply its own X-Request-Id, so two people can end up
        sharing one. An unattributed row is a smaller error than one attributed
        to the wrong person."""
        from analytics.correlate import _RESOLVE

        assert "HAVING count(DISTINCT user_id) = 1" in _RESOLVE

    def test_correlation_is_bounded(self):
        """It runs on a timer next to the recorder and must never become the
        expensive thing in the database."""
        from analytics.correlate import _RESOLVE

        assert "LIMIT :batch" in _RESOLVE
        assert _RESOLVE.count("make_interval(hours => :hours)") >= 2

    def test_it_never_uses_the_shared_connection_pool(self):
        import inspect

        from analytics.correlate import resolve_identities

        source = inspect.getsource(resolve_identities)
        assert "analytics.db" in source
        assert "POSTGRES_ASYNC_SESSION_FACTORY" not in source


class TestServerErrorsAreCaptured:
    """Before this the platform recorded only what broke in a browser, which is
    the smaller half. A 500 was a status code in one table and a stack trace in
    a pod log that rotates."""

    def test_a_server_error_names_our_own_frame(self):
        from analytics.recorder import _server_culprit

        def inner():
            raise ValueError("boom")

        try:
            inner()
        except ValueError as exc:
            culprit = _server_culprit(exc)
        assert culprit and culprit.startswith("inner ("), culprit

    def test_the_browser_cannot_claim_a_server_error(self):
        """`kind` is set by the recorder for server errors and the ingest
        schema does not accept that value, so a page cannot file its own
        exceptions as backend faults."""
        import typing

        from schemas import ClientErrorIn

        allowed = typing.get_args(ClientErrorIn.model_fields["kind"].annotation)
        assert "server" not in allowed

    def test_both_hooks_are_installed(self):
        """`render()` catches nearly everything; the middleware catches what
        escapes it. Miss either and a class of failure stays invisible."""
        import inspect

        import middleware
        from routers import generic

        assert "record_server_error" in inspect.getsource(generic)
        assert "record_server_error" in inspect.getsource(middleware)


class TestNobodyCanFloodUs:
    """Authentication answers "is this somebody". It does not answer "should
    somebody be allowed to send this much" — and the platform's existing guest
    budget exempts every signed-in account, which is not hard to obtain."""

    def test_a_burst_is_allowed_and_a_flood_is_not(self):
        from analytics.ingest_limit import IngestLimiter

        limiter = IngestLimiter(rows_per_minute=600, burst=1000)
        # A tab that was closed for an hour comes back and flushes its buffer.
        # That is legitimate and must not be refused.
        assert limiter.check("u1", 1000) == (True, 0)
        allowed, retry_after = limiter.check("u1", 500)
        assert not allowed and retry_after > 0

    def test_one_caller_cannot_throttle_another(self):
        from analytics.ingest_limit import IngestLimiter

        limiter = IngestLimiter(rows_per_minute=600, burst=1000)
        limiter.check("noisy", 1000)
        assert limiter.check("quiet", 900)[0] is True

    def test_a_refusal_does_not_charge_the_bucket(self):
        """Otherwise a client that keeps hammering pushes its own recovery
        further away with every attempt, and never gets back in."""
        from analytics.ingest_limit import IngestLimiter

        limiter = IngestLimiter(rows_per_minute=600, burst=1000)
        limiter.check("u1", 1000)
        first = limiter.check("u1", 500)[1]
        for _ in range(20):
            limiter.check("u1", 500)
        assert limiter.check("u1", 500)[1] <= first

    def test_the_bucket_refills(self):
        import time

        from analytics.ingest_limit import IngestLimiter

        limiter = IngestLimiter(rows_per_minute=600, burst=1000)
        limiter.check("u1", 1000)
        limiter._buckets["u1"] = (0.0, time.monotonic() - 60)
        assert limiter.check("u1", 500)[0] is True

    def test_tracking_cannot_grow_without_bound(self):
        """A dictionary that grows until the pod dies is a worse outcome than
        the one being prevented."""
        from analytics import ingest_limit
        from analytics.ingest_limit import IngestLimiter

        limiter = IngestLimiter(rows_per_minute=6000, burst=6000)
        original = ingest_limit.MAX_TRACKED
        ingest_limit.MAX_TRACKED = 50
        try:
            for index in range(500):
                limiter.check(f"user-{index}", 1)
            assert limiter.tracked() <= 50
        finally:
            ingest_limit.MAX_TRACKED = original

    def test_every_ingest_endpoint_is_charged(self):
        """Miss one and it becomes the way in."""
        import inspect

        from routers import analytics as router_module

        source = inspect.getsource(router_module)
        for handler in (
            "ingest_events",
            "submit_platform_feedback",
            "ingest_client_session",
            "ingest_client_errors",
            "ingest_interactions",
            "ingest_vitals",
        ):
            body = source.split(f"async def {handler}(", 1)[1].split("\n@router", 1)[0]
            assert "_enforce_ingest_budget(" in body, handler

    def test_every_ingest_endpoint_requires_a_token(self):
        import inspect

        from routers import analytics as router_module

        source = inspect.getsource(router_module)
        for handler in (
            "ingest_events",
            "submit_platform_feedback",
            "ingest_client_session",
            "ingest_client_errors",
            "ingest_interactions",
            "ingest_vitals",
        ):
            # The decorator sits above the def, so look at what precedes it.
            preamble = source.split(f"async def {handler}(", 1)[0].rsplit("@router.post", 1)[1]
            assert "Depends(auth())" in preamble, handler

    def test_the_batch_sizes_are_capped(self):
        """The limiter bounds the rate; these bound one request, so a single
        POST cannot carry an unbounded payload before the limiter sees it."""
        from schemas import (
            ActivityEventBatch,
            ClientErrorBatch,
            InteractionBatch,
            WebVitalBatch,
        )

        for model in (ActivityEventBatch, ClientErrorBatch, InteractionBatch, WebVitalBatch):
            field = model.model_fields["events"]
            caps = [m for m in field.metadata if hasattr(m, "max_length")]
            assert caps and caps[0].max_length <= 200, model.__name__
