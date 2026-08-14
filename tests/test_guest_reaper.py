"""Unit tests for guest expiry determination and the reaper's decisions.

The reaper silently stopped deleting anything because guests were identified by
an attribute Keycloak may never have stored. These tests pin the two behaviours
that broke: identification must not depend on the attribute, and an expiry that
cannot be read must never be treated as "expired in 1970".
"""
import time

import pytest


def _user(username="guest-abc123", expires_at=None, created_ms=None, uid="u-1"):
    user = {"id": uid, "username": username}
    if expires_at is not None:
        user["attributes"] = {"wisefood_guest_expires_at": [str(expires_at)]}
    if created_ms is not None:
        user["createdTimestamp"] = created_ms
    return user


class TestGuestExpiry:
    def test_attribute_is_preferred(self):
        import guests

        stamp = int(time.time()) + 500
        assert guests._guest_expires_at(_user(expires_at=stamp)) == stamp

    def test_falls_back_to_creation_timestamp_plus_ttl(self):
        """A realm that dropped the attribute must still yield an expiry."""
        import guests
        from main import config

        created_s = int(time.time()) - 10
        expires = guests._guest_expires_at(_user(created_ms=created_s * 1000))
        assert expires == created_s + config.settings["GUEST_TTL_SECONDS"]

    def test_no_signal_at_all_is_undetermined_not_expired(self):
        import guests

        assert guests._guest_expires_at(_user()) is None

    def test_unparseable_attribute_falls_back_rather_than_expiring(self):
        import guests
        from main import config

        created_s = int(time.time()) - 10
        user = _user(created_ms=created_s * 1000)
        user["attributes"] = {"wisefood_guest_expires_at": ["not-a-number"]}
        assert guests._guest_expires_at(user) == created_s + config.settings["GUEST_TTL_SECONDS"]


class TestReaper:
    @pytest.fixture
    def deleted(self, monkeypatch):
        import guests

        removed = []

        async def fake_delete(user_id):
            removed.append(user_id)

        monkeypatch.setattr(guests, "delete_guest", fake_delete)
        return removed

    async def test_expired_guest_is_deleted(self, monkeypatch, deleted):
        import guests

        monkeypatch.setattr(
            guests, "_guest_users",
            lambda max_results: [_user(expires_at=int(time.time()) - 1, uid="dead")],
        )
        assert await guests.reap_expired_guests() == 1
        assert deleted == ["dead"]

    async def test_live_guest_is_left_alone(self, monkeypatch, deleted):
        import guests

        monkeypatch.setattr(
            guests, "_guest_users",
            lambda max_results: [_user(expires_at=int(time.time()) + 3600, uid="alive")],
        )
        assert await guests.reap_expired_guests() == 0
        assert deleted == []

    async def test_guest_with_no_determinable_expiry_survives(self, monkeypatch, deleted):
        """The regression that would have deleted every guest at once."""
        import guests

        monkeypatch.setattr(
            guests, "_guest_users", lambda max_results: [_user(uid="unknown")]
        )
        assert await guests.reap_expired_guests() == 0
        assert deleted == []

    async def test_one_failure_does_not_stop_the_sweep(self, monkeypatch):
        import guests

        removed = []

        async def flaky_delete(user_id):
            if user_id == "boom":
                raise RuntimeError("keycloak said no")
            removed.append(user_id)

        past = int(time.time()) - 1
        monkeypatch.setattr(guests, "delete_guest", flaky_delete)
        monkeypatch.setattr(
            guests, "_guest_users",
            lambda max_results: [
                _user(expires_at=past, uid="boom"),
                _user(expires_at=past, uid="fine"),
            ],
        )
        assert await guests.reap_expired_guests() == 1
        assert removed == ["fine"]


class TestGuestIdentification:
    def test_role_members_are_used_and_prefix_still_guards(self, monkeypatch):
        """Attributes may be absent; the realm role is what marks a guest."""
        import guests

        class FakeAdmin:
            def get_realm_role_members(self, role, query=None):
                assert role == guests.GUEST_ROLE
                return [
                    {"id": "g1", "username": "guest-aaa"},                 # no attributes
                    {"id": "x1", "username": "real.person@example.org"},   # not a guest name
                ]

            def get_users(self, query=None):
                raise AssertionError("must not fall back when the role lookup works")

        monkeypatch.setattr(guests, "KEYCLOAK_ADMIN_CLIENT", lambda: FakeAdmin())
        found = guests._guest_users(max_results=10)
        assert [u["id"] for u in found] == ["g1"]

    def test_falls_back_to_username_search_when_role_lookup_fails(self, monkeypatch):
        import guests

        class FakeAdmin:
            def get_realm_role_members(self, role, query=None):
                raise RuntimeError("role does not exist")

            def get_users(self, query=None):
                assert query["briefRepresentation"] is False
                return [{"id": "g2", "username": "guest-bbb"}]

        monkeypatch.setattr(guests, "KEYCLOAK_ADMIN_CLIENT", lambda: FakeAdmin())
        assert [u["id"] for u in guests._guest_users(max_results=10)] == ["g2"]
