"""Maintenance mode: admins through, everyone else told plainly.

The switch lives in the analytics settings table because that table already
has the three things a platform switch needs — an admin-only write, an audit
trail, and thirty-second propagation to every replica. What is worth guarding
here is the gate itself: that it refuses the right people, that it never
refuses the endpoints an admin needs to switch it back off, and that a broken
settings cache cannot lock everyone out.
"""
import asyncio
import json

import pytest

import main  # noqa: F401 — settings import the config
from analytics import SETTINGS
from analytics.settings import DEFAULTS
from middleware import _MAINTENANCE_OPEN, RequestContextMiddleware, _send_json

ADMIN = {"realm_access": {"roles": ["admin", "user"]}}
EXPERT = {"realm_access": {"roles": ["expert", "user"]}}
NOBODY = None


def scope(path: str, root_path: str = "") -> dict:
    return {"type": "http", "path": path, "root_path": root_path}


@pytest.fixture
def closed(monkeypatch):
    monkeypatch.setitem(SETTINGS._values, "platform.maintenance_mode", True)


@pytest.fixture
def open_(monkeypatch):
    monkeypatch.setitem(SETTINGS._values, "platform.maintenance_mode", False)


class TestTheSetting:
    def test_ships_off(self):
        assert DEFAULTS["platform.maintenance_mode"] is False

    def test_is_a_boolean_so_the_admin_only_put_validates_it(self):
        # The settings endpoint validates against the default's type; a bool
        # default is what makes "true"/"maybe" get refused.
        assert isinstance(DEFAULTS["platform.maintenance_mode"], bool)


class TestTheGate:
    def test_open_platform_refuses_nobody(self, open_):
        for payload in (ADMIN, EXPERT, NOBODY):
            assert not RequestContextMiddleware._closed_to(payload, scope("/api/v1/recipes"))

    def test_closed_platform_refuses_experts_and_anonymous(self, closed):
        assert RequestContextMiddleware._closed_to(EXPERT, scope("/api/v1/recipes"))
        assert RequestContextMiddleware._closed_to(NOBODY, scope("/api/v1/recipes"))

    def test_closed_platform_lets_admins_through(self, closed):
        assert not RequestContextMiddleware._closed_to(ADMIN, scope("/api/v1/recipes"))

    def test_the_switch_can_always_be_switched_back(self, closed):
        # An admin whose token has just expired must still reach /info to be
        # told the platform is closed, and the settings endpoint is what turns
        # it back on. A maintenance mode nobody can leave is an outage.
        for path in _MAINTENANCE_OPEN:
            assert not RequestContextMiddleware._closed_to(NOBODY, scope(path)), path
        assert not RequestContextMiddleware._closed_to(
            NOBODY, scope("/api/v1/analytics/settings/platform.maintenance_mode")
        )

    def test_services_can_still_read_flags_and_report(self, closed):
        # Neither is user access. Refusing runtime-flags makes every service
        # fall back to defaults for the length of the maintenance; refusing
        # the signed internal ingest silently loses what they did.
        assert not RequestContextMiddleware._closed_to(
            NOBODY, scope("/api/v1/analytics/runtime-flags"))
        assert not RequestContextMiddleware._closed_to(
            NOBODY, scope("/api/v1/analytics/internal/events"))
        # The browser's equivalent. Refused, it does not stop recording — it
        # stops the page learning that recording is on, which disables capture
        # for the whole maintenance and looks like a broken feature after.
        assert not RequestContextMiddleware._closed_to(
            NOBODY, scope("/api/v1/analytics/client-flags"))

    def test_the_exemptions_are_the_narrow_ones(self, closed):
        # Every other analytics endpoint is closed like anything else — the
        # exemption is for the switch, not for the console.
        assert RequestContextMiddleware._closed_to(EXPERT, scope("/api/v1/analytics/overview"))
        assert RequestContextMiddleware._closed_to(NOBODY, scope("/api/v1/analytics/events"))

    def test_root_path_is_stripped_before_matching(self, closed):
        # Behind APISIX the app is mounted at /rest; the ASGI path carries it.
        assert not RequestContextMiddleware._closed_to(
            NOBODY, scope("/rest/api/v1/system/info", root_path="/rest")
        )
        assert RequestContextMiddleware._closed_to(
            NOBODY, scope("/rest/api/v1/recipes", root_path="/rest")
        )

    def test_an_unreadable_settings_cache_leaves_the_platform_open(self, monkeypatch):
        class Broken:
            def current(self):
                raise RuntimeError("cache exploded")

        import analytics

        monkeypatch.setattr(analytics, "SETTINGS", Broken())
        assert not RequestContextMiddleware._closed_to(NOBODY, scope("/api/v1/recipes"))


class TestTheRefusal:
    def test_send_json_is_a_complete_response(self):
        sent = []

        async def send(message):
            sent.append(message)

        asyncio.run(
            _send_json(
                send,
                503,
                {"success": False, "error": {"code": "platform/maintenance"}},
                extra_headers={"Retry-After": "300"},
            )
        )
        start, body = sent
        assert start["type"] == "http.response.start" and start["status"] == 503
        headers = dict(start["headers"])
        assert headers[b"content-type"] == b"application/json"
        assert headers[b"retry-after"] == b"300"
        assert headers[b"cache-control"] == b"no-store"
        assert int(headers[b"content-length"]) == len(body["body"])
        assert json.loads(body["body"])["error"]["code"] == "platform/maintenance"
