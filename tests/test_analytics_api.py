"""The analytics ingest contract.

Everything here is about what a caller is *allowed* to assert. A browser may
say what happened to it; it may not say who it is, invent event types, or post
unbounded payloads. A platform service may say more, but only with a signature.
"""
import hashlib
import hmac
import json
import time

import pytest
from pydantic import ValidationError

import main  # noqa: F401 — routers.analytics reaches config through it
from exceptions import APIException, AuthenticationError
from routers.analytics import _verify_service_signature
from schemas import (
    ActivityEventBatch,
    ActivityEventIn,
    CLIENT_EVENT_TYPES,
    MAX_EVENT_PROPS_CHARS,
    PlatformFeedbackRequest,
)

SECRET = "s3cr3t-for-tests"


def _sign(body: bytes, secret: str = SECRET, issued_at: int = None) -> str:
    issued_at = int(time.time()) if issued_at is None else issued_at
    digest = hmac.new(
        secret.encode(), f"{issued_at}|".encode() + body, hashlib.sha256
    ).hexdigest()
    return f"{issued_at}.{digest}"


@pytest.fixture()
def secret(monkeypatch):
    monkeypatch.setenv("ANALYTICS_INGEST_SECRET", SECRET)
    return SECRET


class TestServiceSignature:
    def test_a_correct_signature_is_accepted(self, secret):
        body = b'{"events":[]}'
        _verify_service_signature(body, _sign(body))  # does not raise

    def test_a_tampered_body_is_refused(self, secret):
        signature = _sign(b'{"events":[{"type":"page.view"}]}')
        with pytest.raises(AuthenticationError):
            _verify_service_signature(b'{"events":[{"type":"admin.god_mode"}]}', signature)

    def test_the_wrong_secret_is_refused(self, secret):
        body = b'{"events":[]}'
        with pytest.raises(AuthenticationError):
            _verify_service_signature(body, _sign(body, secret="not-the-secret"))

    def test_an_old_signature_is_refused(self, secret):
        """A captured request must not be replayable indefinitely."""
        body = b'{"events":[]}'
        stale = _sign(body, issued_at=int(time.time()) - 3600)
        with pytest.raises(AuthenticationError):
            _verify_service_signature(body, stale)

    def test_the_timestamp_cannot_be_edited_to_extend_it(self, secret):
        """The timestamp is inside the signed payload, not beside it."""
        body = b'{"events":[]}'
        issued_at = int(time.time()) - 3600
        _, digest = _sign(body, issued_at=issued_at).split(".", 1)
        forged = f"{int(time.time())}.{digest}"
        with pytest.raises(AuthenticationError):
            _verify_service_signature(body, forged)

    @pytest.mark.parametrize("header", ["", "garbage", "notanumber.abc", "12345"])
    def test_malformed_signatures_are_refused(self, secret, header):
        with pytest.raises(AuthenticationError):
            _verify_service_signature(b"{}", header)

    def test_without_a_configured_secret_the_endpoint_is_closed(self, monkeypatch):
        """Unset means closed, not open — the FoodChat assertion's rule."""
        monkeypatch.delenv("ANALYTICS_INGEST_SECRET", raising=False)
        body = b'{"events":[]}'
        with pytest.raises(APIException) as caught:
            _verify_service_signature(body, _sign(body))
        assert caught.value.status_code == 503


class TestClientEventValidation:
    def test_a_known_event_type_is_accepted(self):
        event = ActivityEventIn(type="page.view", app="platform")
        assert event.type == "page.view"

    def test_an_invented_event_type_is_refused(self):
        """`event_type` is indexed; an unbounded set of client-chosen values is
        both a cardinality problem and a way to make the console's filters
        useless."""
        with pytest.raises(ValidationError):
            ActivityEventIn(type="totally.made.up")

    def test_an_unknown_app_is_refused(self):
        with pytest.raises(ValidationError):
            ActivityEventIn(type="page.view", app="not-a-surface")

    def test_oversized_props_are_refused(self):
        with pytest.raises(ValidationError):
            ActivityEventIn(
                type="page.view", props={"blob": "x" * (MAX_EVENT_PROPS_CHARS + 1)}
            )

    def test_props_at_the_limit_are_accepted(self):
        payload = {"blob": "x" * (MAX_EVENT_PROPS_CHARS - 20)}
        assert len(json.dumps(payload)) <= MAX_EVENT_PROPS_CHARS
        ActivityEventIn(type="page.view", props=payload)

    def test_the_body_cannot_name_the_user(self):
        """Identity comes from the token. A `user_id` here would be a way to
        file activity under someone else's name — pydantic ignores unknown
        fields, so it must not land in the model."""
        event = ActivityEventIn(
            type="page.view", props={}, **{"user_id": "somebody-else"}
        )
        assert not hasattr(event, "user_id")

    def test_batches_are_capped(self):
        one = {"type": "page.view", "app": "platform"}
        ActivityEventBatch(events=[one] * 50)
        with pytest.raises(ValidationError):
            ActivityEventBatch(events=[one] * 51)

    def test_an_empty_batch_is_refused(self):
        with pytest.raises(ValidationError):
            ActivityEventBatch(events=[])

    def test_every_allowlisted_type_actually_validates(self):
        for event_type in CLIENT_EVENT_TYPES:
            ActivityEventIn(type=event_type)


class TestPlatformFeedback:
    def test_a_rating_is_required(self):
        """A feedback row with no signal is a row nobody can act on."""
        with pytest.raises(ValidationError):
            PlatformFeedbackRequest(target_type="platform")

    def test_a_numeric_rating_is_enough(self):
        body = PlatformFeedbackRequest(rating_kind="likert5", rating_value_num=4)
        assert body.rating_value_num == 4

    def test_a_textual_rating_is_enough(self):
        body = PlatformFeedbackRequest(rating_kind="thumbs", rating_value="down")
        assert body.rating_value == "down"

    def test_the_comment_is_bounded(self):
        with pytest.raises(ValidationError):
            PlatformFeedbackRequest(rating_value="up", comment="x" * 4001)

    def test_an_unknown_target_type_is_refused(self):
        with pytest.raises(ValidationError):
            PlatformFeedbackRequest(rating_value="up", target_type="the_moon")


class TestSettingsSurface:
    def test_defaults_cover_every_documented_switch(self):
        from analytics.settings import DEFAULTS

        expected = {
            "paused",
            "sample_rate",
            "apps",
            "capture.http_requests",
            "capture.client_events",
            "capture.search_queries",
            "capture.llm_usage",
            "capture.raw_query_text",
            # Real user monitoring.
            "capture.client_sessions",
            "capture.errors",
            "capture.interactions",
            "capture.vitals",
            "sample_rate.interactions",
            "tracing.enabled",
            "tracing.langfuse",
            "pricing.overrides",
        }
        assert set(DEFAULTS) == expected

    def test_the_expensive_capture_streams_are_off_by_default(self):
        """Clicks and page-speed cost the browser work to gather, and clicks
        are the stream a study participant is most likely to consider
        surveillance. Both are switched on deliberately, for a period, to
        answer a question — never by shipping."""
        from analytics.settings import DEFAULTS

        assert DEFAULTS["capture.interactions"] is False
        assert DEFAULTS["capture.vitals"] is False
        # Errors are the exception: an error nobody recorded is one nobody can
        # fix, and it describes the software rather than the person.
        assert DEFAULTS["capture.errors"] is True

    def test_a_price_override_must_be_a_pair_of_numbers(self):
        """A rate that arrived as the string "0.15" and was accepted would
        price every call at nothing, and a spend report reading $0.00 looks
        exactly like one for a platform nobody used."""
        from analytics.settings import SettingsCache

        assert SettingsCache.validate("pricing.overrides", {"m": [0.1, 0.5]}) == {
            "m": [0.1, 0.5]
        }
        for bad in ({"m": "0.15"}, {"m": [1]}, {"m": [-1, 2]}, {"m": [True, 1]}, "x"):
            with pytest.raises(ValueError):
                SettingsCache.validate("pricing.overrides", bad)

    def test_tracing_can_be_switched_off_without_a_redeploy(self):
        """Before this, stopping tracing meant unsetting the Langfuse keys and
        rolling every pod."""
        from analytics.settings import DEFAULTS, SettingsCache

        assert DEFAULTS["tracing.enabled"] is True
        cache = SettingsCache()
        assert cache.tracing_enabled() is True
        assert cache.tracing_enabled("langfuse") is True

        cache._values = dict(DEFAULTS, **{"tracing.langfuse": False})
        assert cache.tracing_enabled() is True
        assert cache.tracing_enabled("langfuse") is False

        cache._values = dict(DEFAULTS, **{"tracing.enabled": False})
        assert cache.tracing_enabled() is False
        # The master switch beats a sink that is still nominally on.
        assert cache.tracing_enabled("langfuse") is False

    def test_collection_is_on_per_app_by_default(self):
        from analytics.settings import APPS, DEFAULTS

        assert set(DEFAULTS["apps"]) == set(APPS)
        assert all(DEFAULTS["apps"].values())
