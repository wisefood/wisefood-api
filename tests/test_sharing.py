"""Share links and the mail that carries them.

The two things worth guarding are what a share *says* and who may act on it.
A share leaves the building — it is read without an account and mailed to
people who have none — so the scrub is the whole privacy boundary, and the
token is the whole authorisation.
"""
from datetime import datetime, timedelta, timezone

import pytest

import main  # noqa: F401
import mailer
import sharing


# A plan as the planner actually produces one: the food, plus everything the
# planner knows about the household that made it necessary.
PLAN = {
    "id": "plan-1",
    "household_id": "house-1",
    "date": "2026-09-20",
    "source_meal_plan_id": "src-1",
    "reasoning": "Ana is 7 and allergic to peanuts, so this week avoids nuts.",
    "applies_to_member_ids": ["m1", "m2"],
    "other_member_ids": ["m2"],
    "constraints_applied": [
        {"constraint": "no peanuts", "detail": "for Ana's allergy", "members": ["m1"]}
    ],
    "breakfast": {
        "recipe_id": "r1",
        "title": "Porridge",
        "ingredients": "oats, milk",
        "directions": "Stir.",
        "nutrition": {"kcal": 310},
        "image_url": "/img/porridge.png",
        "match_reasons": [{"why": "low sodium for Dimitris's blood pressure"}],
    },
    "lunch": [
        {"recipe_id": "r2", "title": "Soup", "ingredients": "veg", "role": "main"},
        {"recipe_id": "r3", "title": "Bread", "role": "side"},
    ],
}


class TestTheScrub:
    """The privacy boundary. Everything else here is plumbing by comparison."""

    def test_the_food_survives(self):
        out = sharing.scrub_meal_plan(PLAN)
        assert out["date"] == "2026-09-20"
        assert out["meals"]["breakfast"]["title"] == "Porridge"
        assert out["meals"]["breakfast"]["nutrition"] == {"kcal": 310}
        assert [d["title"] for d in out["meals"]["lunch"]] == ["Soup", "Bread"]

    @pytest.mark.parametrize("secret", [
        "Ana", "peanut", "allerg", "Dimitris", "blood pressure",
        "house-1", "plan-1", "src-1", "m1",
    ])
    def test_nothing_about_a_person_survives(self, secret):
        blob = repr(sharing.scrub_meal_plan(PLAN)).lower()
        assert secret.lower() not in blob, f"{secret!r} reached a public page"

    @pytest.mark.parametrize("field", ["reasoning", "constraints_applied",
                                       "applies_to_member_ids", "household_id", "id"])
    def test_the_dangerous_fields_are_gone_by_name(self, field):
        assert field not in sharing.scrub_meal_plan(PLAN)

    def test_match_reasons_do_not_ride_along_on_a_dish(self):
        # The per-recipe version of `reasoning`, and the easiest to forget.
        assert "match_reasons" not in sharing.scrub_meal_plan(PLAN)["meals"]["breakfast"]

    def test_an_allowlist_not_a_blocklist(self):
        # A field the planner learns to emit tomorrow must not appear today.
        plan = {"breakfast": {"title": "X", "newly_invented_field": "a member's name"}}
        kept = sharing.scrub_meal_plan(plan)["meals"]["breakfast"]
        assert set(kept) <= set(sharing.MEAL_FIELDS)

    @pytest.mark.parametrize("junk", [None, {}, {"breakfast": "not a dish"},
                                      {"breakfast": [None, 3]}, []])
    def test_rubbish_in_is_an_empty_plan_not_a_crash(self, junk):
        assert sharing.scrub_meal_plan(junk)["meals"] == {}

    def test_an_unknown_kind_has_no_scrubber_and_is_refused(self):
        # Better to refuse than to publish something unexamined.
        with pytest.raises(ValueError):
            sharing.scrub("chat_session", {"messages": ["anything at all"]})


class TestTheToken:
    def test_it_is_long_enough_to_be_the_only_credential(self):
        token = sharing.new_token()
        assert len(token) >= 40
        assert len({sharing.new_token() for _ in range(200)}) == 200


class Row:
    def __init__(self, **kw):
        self.revoked_at = kw.get("revoked_at")
        self.expires_at = kw.get("expires_at")


class TestWhatIsStillLive:
    def test_a_fresh_share_is(self):
        assert sharing.is_live(Row()) is True

    def test_revoked_is_not(self):
        assert sharing.is_live(Row(revoked_at=datetime.now(timezone.utc))) is False

    def test_expired_is_not(self):
        past = datetime.now(timezone.utc) - timedelta(minutes=1)
        assert sharing.is_live(Row(expires_at=past)) is False

    def test_a_naive_timestamp_is_read_as_utc(self):
        # Postgres hands back naive datetimes through some drivers; comparing
        # one against an aware "now" raises, and a raise here would 500 a page
        # that should simply have expired.
        past = (datetime.now(timezone.utc) - timedelta(minutes=1)).replace(tzinfo=None)
        assert sharing.is_live(Row(expires_at=past)) is False

    def test_a_missing_share_is_not(self):
        assert sharing.is_live(None) is False


class TestTheHeaders:
    def test_it_cannot_be_indexed(self):
        # An unguessable URL stays unguessable only while no crawler publishes
        # it. This is the difference between "anyone with the link" and
        # "anyone at all".
        assert "noindex" in sharing.share_headers()["X-Robots-Tag"]

    def test_the_token_does_not_travel_in_a_referer(self):
        # Without this the token reaches the access log of every site a reader
        # clicks through to — which is how these links leak in practice.
        assert sharing.share_headers()["Referrer-Policy"] == "no-referrer"

    def test_it_is_not_cached_by_anything_shared(self):
        assert "no-store" in sharing.share_headers()["Cache-Control"]


class TestExpiryWindows:
    def test_none_means_until_revoked(self):
        assert sharing.expiry_from_days(None) is None

    @pytest.mark.parametrize("asked,expected_days", [(1, 1), (30, 30), (99999, 365), (0, 1)])
    def test_a_window_is_clamped_to_something_sane(self, asked, expected_days):
        got = sharing.expiry_from_days(asked)
        assert abs((got - datetime.now(timezone.utc)).days - expected_days) <= 1


class TestTheMail:
    def test_it_is_off_without_a_mail_server(self):
        # A button that says it cannot send beats one that accepts and drops.
        assert mailer.enabled() is False

    def test_the_subject_greets_the_person(self):
        assert mailer.plan_subject("Ana") == "Hey Ana, here is your meal plan"
        assert mailer.plan_subject("  ") == "Here is your meal plan"

    def test_the_body_says_the_food_in_both_formats(self):
        payload = sharing.scrub_meal_plan(PLAN)
        html_body, text_body = mailer.render_meal_plan(
            member_name="Ana", payload=payload, share_url="https://x/app/shared/t"
        )
        for body in (html_body, text_body):
            assert "Porridge" in body and "Soup" in body
        assert "https://x/app/shared/t" in html_body

    def test_the_mail_leaks_no_more_than_the_page(self):
        payload = sharing.scrub_meal_plan(PLAN)
        html_body, text_body = mailer.render_meal_plan(member_name="Ana", payload=payload)
        for secret in ("peanut", "allerg", "Dimitris", "house-1"):
            assert secret.lower() not in (html_body + text_body).lower()

    def test_a_recipe_title_cannot_inject_markup(self):
        html_body, _ = mailer.render_meal_plan(
            member_name="x",
            payload={"meals": {"breakfast": {"title": "<script>alert(1)</script>"}}},
        )
        assert "<script>" not in html_body
        assert "&lt;script&gt;" in html_body

    def test_an_address_that_cannot_be_one_is_refused(self):
        assert mailer.valid_address("ana@example.org") is True
        for bad in ("", "nope", "a@b", "a b@c.com", "@x.com"):
            assert mailer.valid_address(bad) is False

    def test_a_daily_cap_applies_per_account(self, monkeypatch):
        from main import config

        monkeypatch.setitem(config.settings, "MAIL_DAILY_LIMIT", 3)
        mailer._sent.clear()
        assert [mailer.within_quota("u1") for _ in range(4)] == [True, True, True, False]
        # One account's sending does not spend another's.
        assert mailer.within_quota("u2") is True
