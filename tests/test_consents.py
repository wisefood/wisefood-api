"""One consent for everything, and each purpose withdrawable on its own.

The ledger statements were run against PostgreSQL 17 with accounts that
accepted, withdrew, withdrew and agreed again, accepted only the old terms,
and a guest; what is pinned here is the reading of the ledger and the guards
that keep research use to people who can give it.
"""
from __future__ import annotations

import inspect
from datetime import datetime, timedelta, timezone

import main  # noqa: F401 — `sql` imports `backend.postgres`, which needs config

import consents

T0 = datetime(2026, 10, 27, 12, 0, tzinfo=timezone.utc)


class TestReadingTheLedger:
    def test_the_later_answer_wins(self):
        assert consents.resolve(T0, T0 + timedelta(seconds=1)) == (False, T0 + timedelta(seconds=1))
        assert consents.resolve(T0 + timedelta(seconds=1), T0) == (True, T0 + timedelta(seconds=1))

    def test_a_tie_is_a_withdrawal(self):
        """Recording yes when it may have been no cannot be undone."""
        assert consents.resolve(T0, T0) == (False, T0)

    def test_silence_is_no_unless_told_otherwise(self):
        assert consents.resolve(None, None) == (False, None)
        assert consents.resolve(None, None, silence=True) == (True, None)

    def test_research_silence_is_always_no(self):
        """Nobody's conversations are used because they never answered —
        whatever the analytics mode says about silence."""
        source = inspect.getsource(consents.status)
        assert 'silence=analytics_silence if purpose == "analytics" else False' in source


class TestTheTermsVersion:
    def test_older_terms_are_asked_again(self):
        """1.0 covered the service and analytics only."""
        assert not consents.terms_current("1.0")
        assert consents.terms_current(consents.TERMS_VERSION)
        assert consents.terms_current("2.1")

    def test_a_malformed_version_is_not_current(self):
        assert not consents.terms_current("draft")
        assert not consents.terms_current(None)

    def test_every_purpose_has_a_grant_and_a_withdrawal(self):
        assert set(consents.PURPOSES) == {"analytics", "research"}
        for grant, withdraw in consents.PURPOSES.values():
            assert grant != withdraw
        # The analytics kinds are the ones the recorder's consent cache reads.
        from analytics import CONSENT_GRANT, CONSENT_OPT_OUT

        assert consents.PURPOSES["analytics"] == (CONSENT_GRANT, CONSENT_OPT_OUT)


class TestOneAcceptance:
    def test_terms_and_purposes_are_one_transaction(self):
        """Never a person who accepted the terms and only some of what they said."""
        source = inspect.getsource(consents.accept_all)
        assert source.count("await db.commit()") == 1
        assert "kinds = [TERMS]" in source


class TestGuestsAndResearch:
    def test_a_guest_never_accepts_research(self):
        from routers import users

        source = inspect.getsource(users.api_accept_my_consents)
        assert 'p != "research" or not _is_guest_request(request)' in source

    def test_a_guest_cannot_switch_research_on(self):
        from routers import users

        source = inspect.getsource(users.api_set_my_consent_purpose)
        assert 'purpose == "research" and body.enabled and _is_guest_request(request)' in source

    def test_eligibility_drops_guests_a_second_time(self):
        source = inspect.getsource(consents.research_eligible_members)
        assert "is_guest" in source


class TestWhoMayReadEligibility:
    def test_only_admins_and_service_accounts(self):
        from routers import research

        route = next(r for r in research.router.routes if r.path.endswith("/eligible-members"))
        dependency = route.dependencies[0].dependency
        # auth() closes over the roles it was built with.
        required = inspect.getclosurevars(dependency).nonlocals.get("required")
        assert sorted(required) == ["admin", "agent"]

    def test_each_read_is_recorded(self):
        from routers import research

        assert "admin.research_eligibility_read" in inspect.getsource(research.eligible_members)
