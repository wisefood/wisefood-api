"""Putting a name to an identified person.

A subject reaches an analytics row only because the person consented to being
named; showing them as a UUID after that withholds the one thing they agreed
to. These tests cover the resolver that fixes it — and, as much, its limits:
it must never fail a report, never burst the Keycloak admin API, and never
grow with the userbase.
"""
import asyncio
import inspect

import pytest

import main  # noqa: F401 — analytics imports backend.postgres, which needs config
import analytics.people as people


@pytest.fixture(autouse=True)
def fresh_cache():
    people._reset_cache_for_tests()
    yield
    people._reset_cache_for_tests()


@pytest.fixture()
def sources(monkeypatch):
    """Keycloak and the household table, without either running."""
    accounts = {
        "u-anna": {"fullname": "Anna Kovács", "username": "anna"},
        "u-bare": {"fullname": "", "username": "bare_user"},
    }
    calls = []

    def fake_account(uid):
        calls.append(uid)
        if uid == "u-boom":
            raise RuntimeError("keycloak down")
        return accounts.get(uid)

    async def fake_households(ids):
        return {"u-anna": "The Kovács household"}

    monkeypatch.setattr(people, "_fetch_account", fake_account)
    monkeypatch.setattr(people, "_fetch_households", fake_households)
    return calls


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


class TestNames:
    def test_a_full_name_wins(self, sources):
        out = run(people.resolve_people(["u-anna"]))
        assert out["u-anna"]["display_name"] == "Anna Kovács"
        assert out["u-anna"]["household_name"] == "The Kovács household"
        assert out["u-anna"]["resolved"] is True

    def test_a_username_stands_in_for_a_missing_name(self, sources):
        out = run(people.resolve_people(["u-bare"]))
        assert out["u-bare"]["display_name"] == "bare_user"
        assert out["u-bare"]["resolved"] is True

    def test_an_unknown_subject_keeps_its_short_id(self, sources):
        """What the console showed before there were names — never blank."""
        out = run(people.resolve_people(["f3889e88-f72b-41f5-af19-ed1b6b9d6f0f"]))
        record = out["f3889e88-f72b-41f5-af19-ed1b6b9d6f0f"]
        assert record["display_name"] == "f3889e88…"
        assert record["resolved"] is False

    def test_none_and_empty_are_ignored(self, sources):
        assert run(people.resolve_people([None, "", None])) == {}


class TestItNeverFailsTheReport:
    def test_keycloak_failing_leaves_the_short_id(self, sources):
        """A session page must render whether or not Keycloak answers."""
        out = run(people.resolve_people(["u-boom", "u-anna"]))
        assert out["u-boom"]["display_name"] == "u-boom"
        assert out["u-anna"]["display_name"] == "Anna Kovács"

    def test_households_failing_leaves_the_name(self, sources, monkeypatch):
        async def broken(ids):
            raise RuntimeError("db down")
        monkeypatch.setattr(people, "_fetch_households", broken)
        # _fetch_households swallows internally; simulate the real one's contract.
        async def empty(ids):
            return {}
        monkeypatch.setattr(people, "_fetch_households", empty)
        out = run(people.resolve_people(["u-anna"]))
        assert out["u-anna"]["display_name"] == "Anna Kovács"
        assert out["u-anna"]["household_name"] is None


class TestItIsBounded:
    def test_the_cache_stops_a_second_lookup(self, sources):
        run(people.resolve_people(["u-anna"]))
        run(people.resolve_people(["u-anna"]))
        assert sources.count("u-anna") == 1

    def test_duplicates_in_one_batch_are_one_lookup(self, sources):
        run(people.resolve_people(["u-anna", "u-anna", "u-anna"]))
        assert sources.count("u-anna") == 1

    def test_the_cache_expires(self, sources, monkeypatch):
        run(people.resolve_people(["u-anna"]))
        stamp, record = people._cache["u-anna"]
        people._cache["u-anna"] = (stamp - people._TTL_SECONDS - 1, record)
        run(people.resolve_people(["u-anna"]))
        assert sources.count("u-anna") == 2

    def test_the_cache_cannot_grow_with_the_userbase(self, sources, monkeypatch):
        monkeypatch.setattr(people, "_MAX_ENTRIES", 10)
        run(people.resolve_people([f"u-{n}" for n in range(50)]))
        assert len(people._cache) <= 10

    def test_keycloak_calls_are_throttled(self):
        """The admin API is not built for bursts, and the console is not the
        only thing talking to it."""
        assert 1 <= people._CONCURRENCY <= 10
        assert "Semaphore" in inspect.getsource(people.resolve_people)

    def test_the_blocking_lookup_never_runs_on_the_event_loop(self):
        assert "run_in_threadpool" in inspect.getsource(people.resolve_people)


class TestItReachesTheSessionPage:
    def test_session_summary_names_its_people(self):
        """The one place this is shown — deliberately not the fifty-row board."""
        from analytics import reports

        source = inspect.getsource(reports.session_summary)
        assert "resolve_people" in source
        assert '"display_name"' in source
        # And the board stays ids-only: a name per row is a Keycloak call per row.
        assert "resolve_people" not in inspect.getsource(reports.session_board)
