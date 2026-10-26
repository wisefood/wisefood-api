"""Inactivity reminders against a real Postgres, through the real routes.

Needs a database with schemas/70_notifications.sql and 50_analytics.sql
applied, at the POSTGRES_* settings — like test_sharing_e2e.py. Keycloak, the
mail server and token verification are stubbed at their seams; the SQL is the
thing under test: the upserts, the partial unique index that makes the daily
run happen once, the claim that makes a reminder go out once, the retry rule,
and the stretch arithmetic that resets when a person comes back.
"""
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

import main
import mailer
from main import config
from notifications import activity, config as ncfg, preferences, reminders
from notifications.reminders import Account, Activity, Due

pytestmark = pytest.mark.asyncio(loop_scope="module")

#: Far from any real cycle, so the cleanup below can never touch real runs in
#: a shared development database.
NOW = datetime(2030, 1, 7, 10, 0, tzinfo=timezone.utc)
REAL_CURRENT = ncfg.current
CFG = dict(ncfg.DEFAULT_CONFIG, mode="live", inactive_after_days=14, repeat_every_days=7, max_reminders=2)

ADMIN = {"sub": "admin-1", "email": "admin@wisefood.gr", "given_name": "Dimitris",
         "realm_access": {"roles": ["admin"]}}
PEOPLE = ["e2e-ana", "e2e-ben", "e2e-cleo"]


async def _sql(statement, **params):
    from sqlalchemy import text

    from backend.postgres import POSTGRES_ASYNC_SESSION_FACTORY

    async with POSTGRES_ASYNC_SESSION_FACTORY()() as db:
        result = await db.execute(text(statement), params)
        await db.commit()
        try:
            return result.all()
        except Exception:
            return None


@pytest_asyncio.fixture(autouse=True, loop_scope="module")
async def clean():
    """Each test starts with none of its people, runs or settings in the tables."""
    ids = PEOPLE + ["admin-1"]
    for table in ("notification_log", "notification_preference", "user_activity"):
        await _sql(f"DELETE FROM wisefood.{table} WHERE user_id = ANY(:ids)", ids=ids)
    await _sql("DELETE FROM wisefood.notification_log WHERE mode = 'test' AND recipient = :r", r=ADMIN["email"])
    await _sql("DELETE FROM wisefood.notification_run WHERE run_date >= '2030-01-01' "
               "OR triggered_by = 'admin-1'")
    await _sql("DELETE FROM analytics.settings WHERE key = :k", k=ncfg.SETTING_KEY)
    from analytics import SETTINGS

    SETTINGS.invalidate()
    activity._last_write.clear()
    yield


def _account(uid, **kw):
    base = dict(user_id=uid, username=f"{uid}@example.org", email=f"{uid}@example.org",
                first_name=uid.split("-")[1].title(), email_verified=True, enabled=True)
    base.update(kw)
    return Account(**base)


# ---------------------------------------------------------------- activity --

async def test_a_visit_only_ever_moves_last_seen_forward():
    later, earlier = NOW, NOW - timedelta(days=3)
    await activity._record("e2e-ana", "sl", later)
    await activity._record("e2e-ana", None, earlier)       # a slower worker, arriving late
    rows = await _sql("SELECT last_seen_at, locale, estimated FROM wisefood.user_activity WHERE user_id='e2e-ana'")
    assert rows == [(later, "sl", False)], "older write ignored, locale kept"


async def test_the_clock_starts_for_an_account_never_seen():
    await activity._record("e2e-ben", "hu", NOW - timedelta(days=40))
    found = await reminders.load_activity(["e2e-ben", "e2e-cleo"], NOW)
    assert found["e2e-ben"] == Activity(NOW - timedelta(days=40), estimated=False, locale="hu")
    assert found["e2e-cleo"] == Activity(NOW, estimated=True, locale=None)
    # A real visit afterwards replaces the estimate.
    await activity._record("e2e-cleo", "en", NOW + timedelta(minutes=5))
    again = await reminders.load_activity(["e2e-cleo"], NOW + timedelta(minutes=10))
    assert again["e2e-cleo"].estimated is False


# ------------------------------------------------------------- preferences --

async def test_the_link_in_a_reminder_turns_reminders_off_and_back_on():
    assert (await preferences.get("e2e-ana"))["inactivity_reminders"] is True
    token = await preferences.ensure_token("e2e-ana")
    assert token == await preferences.ensure_token("e2e-ana"), "one token per person, stable"
    assert (await preferences.get("e2e-ana"))["inactivity_reminders"] is True, "minting it changed nothing"

    assert await preferences.unsubscribe(token, via="email_link") == "e2e-ana"
    assert await preferences.unsubscribe(token, via="list_header") == "e2e-ana", "idempotent"
    assert "e2e-ana" in await preferences.opted_out()
    assert await preferences.unsubscribe("not-a-token", via="email_link") is None

    await preferences.set_inactivity_reminders("e2e-ana", True, via="profile")
    assert "e2e-ana" not in await preferences.opted_out()


# --------------------------------------------------------------- the runs --

async def test_one_scheduled_run_per_mode_per_day():
    first = await reminders._open_run(source="scheduled", mode="live", now=NOW, triggered_by=None)
    assert first is not None
    assert await reminders._open_run(source="scheduled", mode="live", now=NOW, triggered_by=None) is None
    assert await reminders._open_run(source="scheduled", mode="experimental", now=NOW, triggered_by=None)
    assert await reminders._open_run(source="manual", mode="live", now=NOW, triggered_by="admin-1")
    assert await reminders._open_run(source="scheduled", mode="live", now=NOW + timedelta(days=1), triggered_by=None)


async def test_the_daily_claim_keeps_working_once_postgres_caches_a_generic_plan():
    # Postgres plans a prepared statement's first five executions with the
    # parameters in hand, then may switch to one generic plan. The partial
    # index's predicate has to be provable without them, or the scheduled run
    # works for five ticks and fails on every one after.
    first = await reminders._open_run(source="scheduled", mode="live", now=NOW, triggered_by=None)
    assert first is not None
    for _ in range(12):
        assert await reminders._open_run(source="scheduled", mode="live", now=NOW, triggered_by=None) is None


async def test_a_reminder_is_claimed_once_and_retried_only_after_a_failure():
    due = Due(account=_account("e2e-ana"), activity=Activity(NOW - timedelta(days=20)), sequence=1)
    first = await reminders._claim(mode="live", due=due, run_id=None, now=NOW)
    assert first is not None
    assert await reminders._claim(mode="live", due=due, run_id=None, now=NOW) is None, "in flight elsewhere"
    assert await reminders._claim(mode="experimental", due=due, run_id=None, now=NOW), "modes keep separate books"

    await reminders._finish(first, ok=False, recipient="a***@example.org", error="451")
    assert await reminders._claim(mode="live", due=due, run_id=None, now=NOW) == first, "retry 2"
    await reminders._finish(first, ok=False, recipient="a***@example.org", error="451")
    assert await reminders._claim(mode="live", due=due, run_id=None, now=NOW) == first, "retry 3"
    await reminders._finish(first, ok=False, recipient="a***@example.org", error="451")
    assert await reminders._claim(mode="live", due=due, run_id=None, now=NOW) is None, "three attempts, then stop"


async def test_a_claim_abandoned_by_a_dead_process_is_picked_up_later():
    due = Due(account=_account("e2e-ben"), activity=Activity(NOW - timedelta(days=20)), sequence=1)
    log_id = await reminders._claim(mode="live", due=due, run_id=None, now=NOW - timedelta(hours=2))
    assert await reminders._claim(mode="live", due=due, run_id=None, now=NOW) == log_id


# ---------------------------------------------------- a whole stretch, live --

@pytest.fixture
def world(monkeypatch):
    """Keycloak, the mail server and the clock, replaced."""
    sent = []
    state = {"accounts": [_account(p) for p in PEOPLE], "config": dict(CFG)}

    async def fetch():
        return state["accounts"]

    async def fake_send(**kw):
        sent.append(kw)
        return True

    monkeypatch.setattr(reminders, "fetch_accounts", fetch)
    monkeypatch.setattr(mailer, "send_email", fake_send)
    monkeypatch.setattr(mailer, "enabled", lambda: True)
    monkeypatch.setattr(reminders.ncfg, "current", lambda: state["config"])
    monkeypatch.setitem(config.settings, "NOTIFICATIONS_SEND_INTERVAL_SECONDS", 0)
    monkeypatch.setitem(config.settings, "APP_EXT_DOMAIN", "https://wf.example")
    return sent, state


async def test_a_stretch_of_absence_gets_its_reminders_and_resets_on_return(world):
    sent, state = world
    await activity._record("e2e-ana", "sl", NOW - timedelta(days=20))   # away 20 days
    await activity._record("e2e-ben", "en", NOW - timedelta(days=3))    # recently here
    await activity._record("e2e-cleo", "en", NOW - timedelta(days=20))
    await preferences.set_inactivity_reminders("e2e-cleo", False, via="profile")

    stats = await reminders.run_cycle(source="manual", now=NOW)
    assert stats["sent"] == 1 and [m["to"] for m in sent] == ["e2e-ana@example.org"]
    assert stats["skipped"] == {"recently_active": 1, "opted_out": 1}
    assert "pred 20 dnevi" in sent[0]["text_body"], "in her language, with the real number"

    # The same day again: nothing new.
    again = await reminders.run_cycle(source="manual", now=NOW + timedelta(hours=2))
    assert again["sent"] == 0 and again["skipped"].get("waiting_for_next") == 1

    # From here on, follow Ana alone: Ben would start his own stretch in a
    # couple of weeks, which is right but not what this test is about.
    state["accounts"] = [_account("e2e-ana"), _account("e2e-cleo")]

    # A week later: the second and last reminder of this stretch.
    week = await reminders.run_cycle(source="manual", now=NOW + timedelta(days=7))
    assert week["sent"] == 1
    later = await reminders.run_cycle(source="manual", now=NOW + timedelta(days=21))
    assert later["sent"] == 0 and later["skipped"].get("reminders_used_up") == 1

    # She comes back, then drifts off again: a new stretch, a fresh count.
    await activity._record("e2e-ana", "sl", NOW + timedelta(days=22))
    fresh = await reminders.run_cycle(source="manual", now=NOW + timedelta(days=37))
    assert fresh["sent"] == 1

    log = await reminders.recent(limit=10)
    live = [r for r in reversed(log) if r["mode"] == "live" and r["user_id"] == "e2e-ana"]
    assert [r["sequence"] for r in live] == [1, 2, 1]
    assert {r["recipient"] for r in live} == {"e***@example.org"}


async def test_experimental_mode_leaves_the_participants_own_history_alone(world):
    sent, state = world
    state["config"] = dict(CFG, mode="experimental", test_recipients=["ops@wisefood.gr"])
    await activity._record("e2e-ana", "en", NOW - timedelta(days=20))
    state["accounts"] = [_account("e2e-ana")]

    stats = await reminders.run_cycle(source="manual", now=NOW)
    assert stats["sent"] == 1 and sent[-1]["to"] == "ops@wisefood.gr"

    state["config"] = dict(CFG, mode="live")
    live = await reminders.run_cycle(source="manual", now=NOW + timedelta(minutes=1))
    assert live["sent"] == 1 and sent[-1]["to"] == "e2e-ana@example.org", "her first live reminder is still hers"


# ----------------------------------------------------------------- the API --

def _sign_in(monkeypatch, payload, *, guest=False):
    import auth as auth_module
    import kutils

    async def verify(token):
        return payload

    monkeypatch.setattr(auth_module, "api_verify_token", verify)
    monkeypatch.setattr(kutils, "current_user", lambda request: payload)
    monkeypatch.setattr(kutils, "is_guest", lambda request: guest)


@pytest_asyncio.fixture(loop_scope="module")
async def client():
    async with AsyncClient(transport=ASGITransport(app=main.api), base_url="http://test",
                           headers={"Authorization": "Bearer x"}) as http:
        yield http


async def test_an_admin_configures_previews_tests_and_runs_from_the_console(world, client, monkeypatch):
    sent, state = world
    _sign_in(monkeypatch, ADMIN)
    # From here the configuration is whatever the console saved to the table.
    monkeypatch.setattr(reminders.ncfg, "current", REAL_CURRENT)
    # The console's endpoints run on the real clock, so this one is real time.
    await activity._record("e2e-ana", "hu", datetime.now(timezone.utc) - timedelta(days=30))
    state["accounts"] = [_account("e2e-ana"), _account("e2e-ben", roles=frozenset({"expert"}))]

    status = (await client.get("/api/v1/notifications/inactivity")).json()["result"]
    assert status["config"]["mode"] == "off" and status["schema_ready"] is True

    refused = await client.put("/api/v1/notifications/inactivity", json={"config": {"mode": "loud"}})
    assert refused.status_code == 422

    saved = await client.put("/api/v1/notifications/inactivity", json={"config": {
        "mode": "experimental", "test_recipients": ["Ops@WiseFood.gr"], "inactive_after_days": 21,
    }})
    assert saved.status_code == 200, saved.text
    assert saved.json()["result"]["config"]["test_recipients"] == ["ops@wisefood.gr"]
    rows = await _sql("SELECT updated_by FROM analytics.settings WHERE key = :k", k=ncfg.SETTING_KEY)
    assert rows == [("admin-1",)], "the audit trail records who changed it"

    preview = (await client.get("/api/v1/notifications/inactivity/preview")).json()["result"]
    assert preview["due"] == 1 and preview["rows"][0]["email"] == "e***@example.org"
    assert preview["rows"][0]["locale"] == "hu"
    assert preview["skipped"] == {"staff": 1}

    test = (await client.post("/api/v1/notifications/inactivity/test", json={"locale": "sl"})).json()["result"]
    assert test == {"sent": True, "to": "admin@wisefood.gr"}
    assert sent[-1]["subject"].startswith("[Test] Dimitris")

    run = (await client.post("/api/v1/notifications/inactivity/run")).json()["result"]
    assert run["mode"] == "experimental" and run["sent"] == 1
    assert sent[-1]["to"] == "ops@wisefood.gr"

    log = (await client.get("/api/v1/notifications/inactivity/log")).json()["result"]["rows"]
    assert [r["mode"] for r in log[:2]] == ["experimental", "test"]


async def test_participants_cannot_reach_the_admin_endpoints(client, monkeypatch):
    _sign_in(monkeypatch, {"sub": "e2e-ana", "realm_access": {"roles": ["user"]}})
    assert (await client.get("/api/v1/notifications/inactivity")).status_code == 403
    assert (await client.post("/api/v1/notifications/inactivity/run")).status_code == 403


async def test_a_person_switches_reminders_off_from_their_profile(client, monkeypatch):
    _sign_in(monkeypatch, {"sub": "e2e-ana", "realm_access": {"roles": ["user"]}})
    current = (await client.get("/api/v1/users/me/notification-preferences")).json()["result"]
    assert current["inactivity_reminders"] is True and current["applies"] is True

    off = await client.put("/api/v1/users/me/notification-preferences", json={"inactivity_reminders": False})
    assert off.json()["result"]["inactivity_reminders"] is False
    assert "e2e-ana" in await preferences.opted_out()


async def test_a_guest_is_told_it_does_not_apply(client, monkeypatch):
    _sign_in(monkeypatch, {"sub": "e2e-ben", "realm_access": {"roles": ["guest"]}}, guest=True)
    current = (await client.get("/api/v1/users/me/notification-preferences")).json()["result"]
    assert current["applies"] is False
    assert (await client.put("/api/v1/users/me/notification-preferences",
                             json={"inactivity_reminders": False})).status_code == 405


async def test_the_one_click_unsubscribe_needs_no_login_and_ignores_a_get(client, monkeypatch):
    token = await preferences.ensure_token("e2e-cleo")
    anonymous = {"Authorization": ""}
    assert (await client.get(f"/api/v1/notifications/unsubscribe?token={token}", headers=anonymous)).status_code == 405
    assert "e2e-cleo" not in await preferences.opted_out(), "a link scanner's GET changes nothing"

    done = await client.post(f"/api/v1/notifications/unsubscribe?token={token}", headers=anonymous)
    assert done.json()["result"] == {"unsubscribed": True, "preview": False}
    assert "e2e-cleo" in await preferences.opted_out()

    back = await client.post(f"/api/v1/notifications/resubscribe?token={token}", headers=anonymous)
    assert back.json()["result"]["subscribed"] is True
    preview = await client.post("/api/v1/notifications/unsubscribe?token=preview", headers=anonymous)
    assert preview.json()["result"] == {"unsubscribed": False, "preview": True}


async def test_erasing_an_account_removes_its_notification_records():
    await activity._record("e2e-ana", "en", NOW)
    await preferences.ensure_token("e2e-ana")
    due = Due(account=_account("e2e-ana"), activity=Activity(NOW), sequence=1)
    await reminders._claim(mode="live", due=due, run_id=None, now=NOW)
    assert await preferences.erase("e2e-ana") == 3
    for table in ("user_activity", "notification_preference", "notification_log"):
        assert await _sql(f"SELECT 1 FROM wisefood.{table} WHERE user_id = 'e2e-ana'") == []
