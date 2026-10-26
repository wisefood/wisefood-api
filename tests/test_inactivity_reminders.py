"""Inactivity reminders: the rules, the mail, and the cycle around them.

The database and Keycloak are replaced at their seams here; the SQL itself is
exercised against a real Postgres in test_notifications_e2e.py. What these pin
is the behaviour a participant would notice: who gets a reminder, when, in
which language, how many, and that experimental mode never reaches them.
"""
import asyncio
from datetime import datetime, timedelta, timezone
from email import policy
from email.parser import BytesParser

import pytest

import main  # noqa: F401 — settings before anything reads them
import email_layout
import mailer
from main import config
from notifications import activity, config as ncfg, content, reminders
from notifications.reminders import Account, Activity, Due, Plan

NOW = datetime(2026, 10, 26, 9, 30, tzinfo=timezone.utc)
CFG = dict(ncfg.DEFAULT_CONFIG, mode="live")


def account(uid="u1", **kw):
    base = dict(
        user_id=uid, username=f"{uid}@example.org", email=f"{uid}@example.org",
        first_name="Ana", email_verified=True, enabled=True, roles=frozenset(),
    )
    base.update(kw)
    return Account(**base)


# ------------------------------------------------------------- the config --

class TestTheConfiguration:
    def test_it_is_off_until_someone_switches_it_on(self):
        assert ncfg.DEFAULT_CONFIG["mode"] == "off"

    def test_a_partial_object_keeps_the_rest_of_the_defaults(self):
        value = ncfg.validate({"mode": "live", "inactive_after_days": 21})
        assert value["inactive_after_days"] == 21
        assert value["max_reminders"] == ncfg.DEFAULT_CONFIG["max_reminders"]

    @pytest.mark.parametrize("bad", [
        {"mode": "on"},
        {"inactive_after_days": 0},
        {"repeat_every_days": "7"},
        {"max_reminders": True},          # isinstance(True, int) holds in Python
        {"send_hour_utc": 24},
        {"verified_only": "yes"},
        {"test_recipients": "admin@x.org"},
        {"test_recipients": ["not an address"]},
        {"test_recipients": [f"a{i}@x.org" for i in range(11)]},
        {"surprise": 1},
    ])
    def test_a_bad_value_is_refused_not_coerced(self, bad):
        with pytest.raises(ValueError):
            ncfg.validate(bad)

    def test_test_recipients_are_normalised_and_deduplicated(self):
        value = ncfg.validate({"test_recipients": [" Admin@X.org ", "admin@x.org"]})
        assert value["test_recipients"] == ["admin@x.org"]

    def test_experimental_falls_back_to_the_deployment_admin(self, monkeypatch):
        monkeypatch.setitem(config.settings, "ADMIN_EMAIL", "Root@WiseFood.gr")
        assert ncfg.effective_test_recipients(dict(CFG, test_recipients=[])) == ["root@wisefood.gr"]
        assert ncfg.effective_test_recipients(dict(CFG, test_recipients=["a@x.org"])) == ["a@x.org"]

    def test_a_stored_value_that_no_longer_validates_means_off(self, monkeypatch):
        from analytics import SETTINGS

        monkeypatch.setitem(SETTINGS._values, ncfg.SETTING_KEY, {"mode": "live", "retired_key": 1})
        assert ncfg.current()["mode"] == "off"

    def test_the_settings_table_validates_it_too(self):
        from analytics.settings import SettingsCache

        with pytest.raises(ValueError):
            SettingsCache.validate(ncfg.SETTING_KEY, {"mode": "loud"})
        assert SettingsCache.validate(ncfg.SETTING_KEY, {"mode": "live"})["mode"] == "live"


# -------------------------------------------------------------- the rules --

class TestWhoIsDue:
    def due(self, *, away_days, sent=0, last_sent_days_ago=None, **cfg):
        last_sent = NOW - timedelta(days=last_sent_days_ago) if last_sent_days_ago is not None else None
        return reminders.due_sequence(
            now=NOW, last_seen=NOW - timedelta(days=away_days),
            sent_in_stretch=sent, last_sent_at=last_sent, config=dict(CFG, **cfg),
        )

    def test_not_before_the_threshold(self):
        assert self.due(away_days=13) is None

    def test_the_first_reminder_on_the_day(self):
        assert self.due(away_days=14) == 1

    def test_the_next_waits_for_the_interval(self):
        assert self.due(away_days=20, sent=1, last_sent_days_ago=6) is None
        assert self.due(away_days=28, sent=1, last_sent_days_ago=14) == 2

    def test_never_more_than_the_maximum(self):
        assert self.due(away_days=200, sent=2, last_sent_days_ago=100) is None
        assert self.due(away_days=200, sent=2, last_sent_days_ago=100, max_reminders=3) == 3


class TestWhoIsNeverWrittenTo:
    def reason(self, acct, *, opted_out=(), **cfg):
        return reminders.skip_reason(acct, config=dict(CFG, **cfg), opted_out=set(opted_out))

    def test_an_ordinary_participant_is_eligible(self):
        assert self.reason(account()) is None

    @pytest.mark.parametrize("acct, why", [
        (account(roles=frozenset({"guest"})), "guest"),
        (account(username="guest-1a2b"), "guest"),
        (account(roles=frozenset({"agent"})), "agent"),
        (account(username="service-account-foodchat"), "service_account"),
        (account(enabled=False), "disabled"),
        (account(email=""), "no_email"),
        (account(email="nope"), "no_email"),
        (account(email_verified=False), "unverified"),
        (account(roles=frozenset({"admin"})), "staff"),
        (account(roles=frozenset({"expert"})), "staff"),
    ])
    def test_the_accounts_that_are_never_reminded(self, acct, why):
        assert self.reason(acct) == why

    def test_opting_out_is_honoured(self):
        assert self.reason(account("u9"), opted_out={"u9"}) == "opted_out"

    def test_the_admin_can_include_staff_and_unverified_addresses(self):
        assert self.reason(account(roles=frozenset({"expert"})), include_staff=True) is None
        assert self.reason(account(email_verified=False), verified_only=False) is None

    def test_a_masked_address_tells_rows_apart_without_holding_it(self):
        assert reminders.mask_email("ana.k@example.org") == "a***@example.org"
        assert reminders.mask_email("broken") == "***"


# --------------------------------------------------------------- the mail --

class TestTheReminder:
    def render(self, **kw):
        args = dict(
            locale="en", first_name="Ana", days_inactive=16,
            app_url="https://wf.example/app/dashboard",
            unsubscribe_url="https://wf.example/app/unsubscribe?token=t0k",
            settings_url="https://wf.example/app/my-profile",
        )
        args.update(kw)
        return content.render_reminder(**args)

    def test_it_greets_the_person_and_says_how_long(self):
        mail = self.render()
        assert mail.subject == "Ana, your WiseFood is still here"
        for body in (mail.email.html, mail.email.text):
            assert "Ana" in body and "16 days ago" in body
            assert "FoodChat" in body and "RecipeWrangler" in body and "FoodScholar" in body

    def test_it_carries_the_way_out_in_both_bodies(self):
        mail = self.render()
        for body in (mail.email.html, mail.email.text):
            assert "https://wf.example/app/unsubscribe?token=t0k" in body
            assert "https://wf.example/app/my-profile" in body

    def test_an_unknown_absence_says_a_while_not_a_number(self):
        mail = self.render(days_inactive=None)
        assert "a while since your last visit" in mail.email.text
        assert "days ago" not in mail.email.text

    @pytest.mark.parametrize("locale, expected", [
        ("hu", "16 napja"),
        ("sl", "pred 16 dnevi"),
        ("el", "πριν από 16 ημέρες"),
    ])
    def test_it_is_written_in_the_persons_language(self, locale, expected):
        mail = self.render(locale=locale)
        assert expected in mail.email.text
        assert f'lang="{locale}"' in mail.email.html

    @pytest.mark.parametrize("days, phrase", [(1, "1 dnem"), (2, "2 dnevoma"), (3, "3 dnevi"), (101, "101 dnem")])
    def test_slovenian_numbers_take_the_right_case(self, days, phrase):
        assert content.days_phrase("sl", days) == phrase

    def test_an_unknown_language_falls_back_to_english(self):
        assert self.render(locale="de-AT").subject.startswith("Ana, your WiseFood")

    def test_a_name_cannot_inject_markup_or_headers(self):
        mail = self.render(first_name='<img src=x onerror=alert(1)>\nBcc: x@evil')
        assert "<img src=x" not in mail.email.html
        assert "\n" not in mail.subject

    def test_no_name_still_reads_as_a_sentence(self):
        assert self.render(first_name="").subject == "Your WiseFood is still here"


class TestTheFrame:
    def test_the_logo_and_emblem_travel_inline(self):
        mail = email_layout.render(locale="en", title="T", preheader="p", body_html="", body_text="")
        assert "cid:wf-logo@wisefood" in mail.html and "cid:wf-eu@wisefood" in mail.html
        assert {i.cid for i in mail.images} == {"wf-logo@wisefood", "wf-eu@wisefood"}
        assert all(i.data.startswith(b"\x89PNG") for i in mail.images)

    def test_the_title_is_escaped(self):
        mail = email_layout.render(locale="en", title="<b>x</b>", preheader="", body_html="", body_text="")
        assert "<b>x</b>" not in mail.html and "&lt;b&gt;x&lt;/b&gt;" in mail.html

    def test_the_meal_plan_mail_now_wears_it(self):
        html_body, _ = mailer.render_meal_plan(member_name="Ana", payload={"meals": {}})
        assert "cid:wf-logo@wisefood" in html_body and "WiseFood Consortium" in html_body


class TestTheMessage:
    @pytest.fixture(autouse=True)
    def sender(self, monkeypatch):
        monkeypatch.setitem(config.settings, "SMTP_FROM", "admin@wisefood.gr")

    def parse(self, message):
        return BytesParser(policy=policy.default).parsebytes(message.as_bytes())

    def test_a_reminder_offers_one_click_unsubscribe(self):
        msg = self.parse(mailer.build_message(
            to="a@x.org", subject="s", html_body="<p>x</p>", text_body="x",
            unsubscribe_url="https://wf.example/rest/api/v1/notifications/unsubscribe?token=t",
        ))
        assert msg["List-Unsubscribe"] == "<https://wf.example/rest/api/v1/notifications/unsubscribe?token=t>"
        assert msg["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"

    def test_the_header_reaches_the_wire_as_a_plain_url(self):
        # Parsing the header back hides the bug this guards against: at the
        # default 78-character limit the library folded a long URL into an
        # RFC 2047 encoded word, which reads back fine and no client follows.
        url = "https://demo.wisefood-project.eu/rest/api/v1/notifications/unsubscribe?token=" + "x" * 43
        raw = mailer.build_message(
            to="a@x.org", subject="Ana, tvoj WiseFood te še vedno čaka", html_body="<p>x</p>",
            text_body="x", unsubscribe_url=url,
        ).as_bytes()
        assert f"List-Unsubscribe: <{url}>".encode() in raw
        assert b"=?utf-8?" in raw, "a non-ASCII subject is still encoded, as it must be"

    def test_bodies_stay_seven_bit_safe(self):
        html_body, text_body = mailer.render_meal_plan(member_name="Ana", payload={"meals": {}})
        msg = mailer.build_message(to="a@x.org", subject="s", html_body=html_body + "é" * 10, text_body=text_body + "é")
        for part in msg.walk():
            if part.get_content_maintype() == "text":
                assert part["Content-Transfer-Encoding"] in ("7bit", "quoted-printable", "base64")
        assert all(len(line) <= 998 for line in msg.as_bytes().split(b"\n"))

    def test_a_requested_mail_offers_none(self):
        msg = self.parse(mailer.build_message(to="a@x.org", subject="s", html_body="<p>x</p>", text_body="x"))
        assert msg["List-Unsubscribe"] is None

    def test_framed_html_carries_its_images_as_related_parts(self):
        html_body, text_body = mailer.render_meal_plan(member_name="Ana", payload={"meals": {}})
        msg = self.parse(mailer.build_message(to="a@x.org", subject="s", html_body=html_body, text_body=text_body))
        assert msg.get_content_type() == "multipart/alternative"
        related = msg.get_payload()[1]
        assert related.get_content_type() == "multipart/related"
        ids = {part["Content-ID"] for part in related.iter_parts() if part.get_content_maintype() == "image"}
        assert ids == {"<wf-logo@wisefood>", "<wf-eu@wisefood>"}


# -------------------------------------------------------------- the cycle --

class FakeStore:
    """The database seams of a cycle, in memory."""

    def __init__(self, *, daily_taken=False, claimed=()):
        self.daily_taken = daily_taken
        self.claimed = set(claimed)
        self.finished = []
        self.closed = []
        self.runs = 0

    async def open_run(self, *, source, mode, now, triggered_by):
        if source == "scheduled" and self.daily_taken:
            return None
        self.runs += 1
        return self.runs

    async def close_run(self, run_id, stats, error):
        self.closed.append((run_id, stats, error))

    async def claim(self, *, mode, due, run_id, now):
        key = (mode, due.account.user_id, due.sequence)
        if key in self.claimed:
            return None
        self.claimed.add(key)
        return len(self.claimed)

    async def finish(self, log_id, *, ok, recipient, error, at=None):
        self.finished.append((log_id, ok, recipient, error))


@pytest.fixture
def cycle(monkeypatch):
    """A cycle with everything outside the module replaced."""
    store = FakeStore()
    sent = []
    state = {"config": dict(CFG), "plan": Plan()}

    async def fake_send(**kw):
        sent.append(kw)
        return not kw["to"].startswith("bounce")

    async def fake_plan(*, config, mode, now):
        return state["plan"]

    async def fake_token(user_id):
        return f"tok-{user_id}"

    monkeypatch.setattr(reminders, "_open_run", store.open_run)
    monkeypatch.setattr(reminders, "_close_run", store.close_run)
    monkeypatch.setattr(reminders, "_claim", store.claim)
    monkeypatch.setattr(reminders, "_finish", store.finish)
    monkeypatch.setattr(reminders, "plan", fake_plan)
    monkeypatch.setattr(reminders.preferences, "ensure_token", fake_token)
    monkeypatch.setattr(reminders.ncfg, "current", lambda: state["config"])
    monkeypatch.setattr(mailer, "send_email", fake_send)
    monkeypatch.setattr(mailer, "enabled", lambda: True)
    monkeypatch.setitem(config.settings, "NOTIFICATIONS_SEND_INTERVAL_SECONDS", 0)
    monkeypatch.setitem(config.settings, "APP_EXT_DOMAIN", "https://wf.example")
    monkeypatch.setitem(config.settings, "CONTEXT_PATH", "/rest")

    class Cycle:
        pass

    c = Cycle()
    c.store, c.sent, c.state = store, sent, state

    def due(uid, days=20, seq=1, **kw):
        return Due(account=account(uid, **kw), activity=Activity(NOW - timedelta(days=days), locale="en"), sequence=seq)

    c.due = due
    c.run = lambda **kw: asyncio.run(reminders.run_cycle(source=kw.pop("source", "manual"), now=NOW, **kw))
    return c


class TestTheCycle:
    def test_live_writes_to_the_participant_with_a_way_out(self, cycle):
        cycle.state["plan"] = Plan(considered=3, eligible=1, due=[cycle.due("u1")])
        stats = cycle.run()
        assert stats["sent"] == 1 and stats["failed"] == 0
        (mail,) = cycle.sent
        assert mail["to"] == "u1@example.org"
        assert mail["unsubscribe_url"] == "https://wf.example/rest/api/v1/notifications/unsubscribe?token=tok-u1"
        assert "https://wf.example/app/unsubscribe?token=tok-u1&amp;lang=en" in mail["html_body"]
        assert cycle.store.finished[0][2] == "u***@example.org", "the log keeps a masked address"

    def test_experimental_reaches_only_the_admins(self, cycle):
        cycle.state["config"] = dict(CFG, mode="experimental", test_recipients=["ops@wisefood.gr", "dp@x.org"])
        cycle.state["plan"] = Plan(due=[cycle.due("u1")])
        stats = cycle.run()
        assert stats["sent"] == 1
        assert [m["to"] for m in cycle.sent] == ["ops@wisefood.gr", "dp@x.org"]
        for mail in cycle.sent:
            assert mail["subject"].startswith("[Experimental] ")
            assert "u***@example.org" in mail["html_body"], "the banner names who it was for"
            assert "unsubscribe_url" not in mail, "an admin's preview offers no real unsubscribe"
            assert "token=preview" in mail["html_body"]
        assert ("experimental", "u1", 1) in cycle.store.claimed

    def test_nobody_is_reminded_twice(self, cycle):
        cycle.store.claimed.add(("live", "u1", 1))
        cycle.state["plan"] = Plan(due=[cycle.due("u1"), cycle.due("u2")])
        stats = cycle.run()
        assert stats["already_handled"] == 1 and stats["sent"] == 1
        assert [m["to"] for m in cycle.sent] == ["u2@example.org"]

    def test_the_cap_defers_the_rest_to_later_cycles(self, cycle):
        cycle.state["config"] = dict(CFG, max_per_run=2)
        cycle.state["plan"] = Plan(due=[cycle.due(f"u{i}") for i in range(5)])
        stats = cycle.run()
        assert stats["sent"] == 2 and stats["deferred"] == 3

    def test_one_bounce_does_not_stop_the_cycle(self, cycle):
        cycle.state["plan"] = Plan(due=[cycle.due("bounce1"), cycle.due("u2")])
        stats = cycle.run()
        assert stats["failed"] == 1 and stats["sent"] == 1

    def test_the_scheduled_cycle_runs_once_a_day(self, cycle):
        cycle.store.daily_taken = True
        cycle.state["plan"] = Plan(due=[cycle.due("u1")])
        assert cycle.run(source="scheduled") is None
        assert cycle.sent == []

    @pytest.mark.parametrize("cfg, smtp, message", [
        ({"mode": "off"}, True, "Reminders are off"),
        ({"mode": "live"}, False, "SMTP_HOST"),
        ({"mode": "experimental", "test_recipients": []}, True, "nowhere to send"),
    ])
    def test_a_cycle_that_cannot_run_says_why(self, cycle, monkeypatch, cfg, smtp, message):
        monkeypatch.setitem(config.settings, "ADMIN_EMAIL", "")
        monkeypatch.setattr(mailer, "enabled", lambda: smtp)
        cycle.state["config"] = dict(CFG, **cfg)
        with pytest.raises(reminders.ReminderError, match=message):
            cycle.run()


class TestThePlan:
    def test_it_sorts_counts_and_never_invents_a_number(self, monkeypatch):
        accounts = [
            account("recent"), account("long"), account("estimated"), account("done"),
            account("guest", roles=frozenset({"guest"})), account("out"),
        ]
        activity_rows = {
            "recent": Activity(NOW - timedelta(days=2)),
            "long": Activity(NOW - timedelta(days=60)),
            "estimated": Activity(NOW - timedelta(days=30), estimated=True),
            "done": Activity(NOW - timedelta(days=90)),
        }

        async def fetch():
            return accounts

        async def opted():
            return {"out"}

        async def load_activity(ids, now):
            assert "guest" not in ids and "out" not in ids
            return {uid: activity_rows[uid] for uid in ids}

        async def load_history(mode, stretches):
            return {"done": (2, NOW - timedelta(days=30))}

        monkeypatch.setattr(reminders, "fetch_accounts", fetch)
        monkeypatch.setattr(reminders.preferences, "opted_out", opted)
        monkeypatch.setattr(reminders, "load_activity", load_activity)
        monkeypatch.setattr(reminders, "load_history", load_history)

        result = asyncio.run(reminders.plan(config=CFG, mode="live", now=NOW))
        assert [d.account.user_id for d in result.due] == ["long", "estimated"], "longest away first"
        assert result.skipped == {"guest": 1, "opted_out": 1, "recently_active": 1, "reminders_used_up": 1}
        by_id = {d.account.user_id: d for d in result.due}
        assert by_id["long"].days_inactive(NOW) == 60
        assert by_id["estimated"].days_inactive(NOW) is None


class TestTheSchedule:
    def test_it_waits_for_the_hour_and_for_the_mode(self, monkeypatch):
        calls = []

        async def fake_run_cycle(**kw):
            calls.append(kw)
            return {"sent": 0}

        async def no_refresh():
            return {}

        from analytics import SETTINGS

        monkeypatch.setattr(SETTINGS, "refresh_if_stale", no_refresh)
        monkeypatch.setattr(reminders, "run_cycle", fake_run_cycle)
        state = {"config": dict(CFG, send_hour_utc=10)}
        monkeypatch.setattr(reminders.ncfg, "current", lambda: state["config"])

        assert asyncio.run(reminders.scheduler_tick(NOW)) is None, "09:30 is before 10:00"
        assert asyncio.run(reminders.scheduler_tick(NOW.replace(hour=10))) == {"sent": 0}
        state["config"] = dict(CFG, mode="off")
        assert asyncio.run(reminders.scheduler_tick(NOW.replace(hour=11))) is None
        assert len(calls) == 1

    def test_the_next_run_is_today_until_it_has_happened(self):
        cfg = dict(CFG, send_hour_utc=10)
        assert reminders.next_scheduled_run(cfg, now=NOW, ran_today=False) == NOW.replace(hour=10, minute=0)
        assert reminders.next_scheduled_run(cfg, now=NOW, ran_today=True) == NOW.replace(hour=10, minute=0) + timedelta(days=1)
        assert reminders.next_scheduled_run(dict(cfg, mode="off"), now=NOW, ran_today=False) is None


# ----------------------------------------------------------- the activity --

class TestRecordingAVisit:
    @pytest.fixture
    def writes(self, monkeypatch):
        seen = []

        async def fake_record(user_id, locale, seen_at):
            seen.append((user_id, locale))

        monkeypatch.setattr(activity, "_record", fake_record)
        monkeypatch.setitem(config.settings, "ACTIVITY_TOUCH_SECONDS", 600)
        activity._last_write.clear()
        return seen

    def run(self, *calls):
        async def go():
            for args, kw in calls:
                activity.touch(*args, **kw)
            await activity.drain()

        asyncio.run(go())

    def test_one_write_per_account_per_interval(self, writes):
        self.run((("u1",), {"locale": "hu-HU"}), (("u1",), {}), (("u2",), {}))
        assert writes == [("u1", "hu"), ("u2", None)]

    def test_guests_and_agents_are_not_recorded(self, writes):
        self.run((("g",), {"roles": ["guest"]}), (("a",), {"roles": ["agent"]}), ((None,), {}))
        assert writes == []
