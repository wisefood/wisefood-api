"""Ports from the environment, and the Kubernetes trap behind them.

Kubernetes injects Docker-link-style variables for every service in the
namespace, so a service called `redis` sets `REDIS_PORT` to
`tcp://10.105.85.77:6379`. `int()` on that raises at import time — before
logging is up, with a traceback that names `int()` rather than the cause.

The API deployment sets `REDIS_PORT` explicitly and so never saw it. The
retention CronJob does not, and died on every scheduled run for two days.
"""
import pytest

from main import _env_port


class TestServiceLinks:
    @pytest.mark.parametrize("value", [
        "tcp://10.105.85.77:6379",
        "tcp://redis.wf-prod.svc.cluster.local:6379",
        "udp://10.0.0.1:6379",
    ])
    def test_the_port_is_taken_out_of_a_service_link(self, value, monkeypatch):
        monkeypatch.setenv("REDIS_PORT", value)
        assert _env_port("REDIS_PORT", 1111) == 6379

    def test_a_plain_port_still_works(self, monkeypatch):
        monkeypatch.setenv("REDIS_PORT", "6380")
        assert _env_port("REDIS_PORT", 6379) == 6380


class TestItNeverStopsTheProcess:
    """A bad port is a degraded cache. A cache is not worth refusing to start
    over — and refusing at import time is the least debuggable way to do it."""

    @pytest.mark.parametrize("value", ["", "   ", "not-a-port", "tcp://host:", "::"])
    def test_nonsense_falls_back_to_the_default(self, value, monkeypatch):
        monkeypatch.setenv("REDIS_PORT", value)
        assert _env_port("REDIS_PORT", 6379) == 6379

    def test_an_absent_variable_uses_the_default(self, monkeypatch):
        monkeypatch.delenv("REDIS_PORT", raising=False)
        assert _env_port("REDIS_PORT", 6379) == 6379


class TestEveryPortIsGuarded:
    def test_no_port_is_parsed_with_a_bare_int(self):
        """Postgres has the same hazard — there is a `db` service, and the
        CronJob would have hit it on the next line."""
        import inspect
        import re

        import main

        source = inspect.getsource(main)
        bare = re.findall(r'int\(os\.getenv\("[A-Z_]*PORT"[^)]*\)\)', source)
        assert not bare, f"parsed without the service-link guard: {bare}"
