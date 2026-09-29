"""The entity cache empties when the entity changes.

`verify_access` fills it through `aget_entity` on every request that names a
member or a household, and `GET /members/{id}` and `GET /households/{id}`
answer straight out of it. Until now nothing ever emptied it and the entries
never expired, so a member renamed through PATCH kept answering to the old
name for as long as Redis held the row — which was forever. These pin the
writes that must drop the cached copy, and the expiry that bounds the damage
if one is ever missed.
"""
from types import SimpleNamespace

import pytest

import main  # noqa: F401
import entity as entity_module
import api.v1.household_members as members_module
import api.v1.households as households_module
from api.v1.household_members import HOUSEHOLD_MEMBER
from api.v1.households import HOUSEHOLD
from main import config


class FakeResult:
    def __init__(self, one=None, rowcount=1, rows=()):
        self._one = one
        self.rowcount = rowcount
        self._rows = list(rows)

    def scalar_one_or_none(self):
        return self._one

    def scalar_one(self):
        return self._one

    def scalars(self):
        return SimpleNamespace(all=lambda: list(self._rows))


class FakeSession:
    """Answers each `execute` with the next prepared result, in order."""

    def __init__(self, results):
        self.results = list(results)
        self.committed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, statement):
        return self.results.pop(0) if self.results else FakeResult()

    async def flush(self):
        pass

    async def commit(self):
        self.committed = True

    def add(self, obj):
        pass


def install_session(monkeypatch, module, results):
    session = FakeSession(results)
    monkeypatch.setattr(module, "POSTGRES_ASYNC_SESSION_FACTORY", lambda: (lambda: session))
    return session


def fake_member(**overrides):
    member = SimpleNamespace(
        id="m-1", name="Guest", age_group=None, image_url=None, household_id="h-1", profile=None
    )
    for key, value in overrides.items():
        setattr(member, key, value)
    member.to_dict = lambda include_profile=False: {
        "id": member.id, "name": member.name, "household_id": member.household_id
    }
    return member


def fake_household(**overrides):
    household = SimpleNamespace(
        id="h-1", name="Guest Household", region=None, metadata_={}, updated_at=None
    )
    for key, value in overrides.items():
        setattr(household, key, value)
    household.to_dict = lambda include_members=False: {
        "id": household.id, "name": household.name, "metadata": household.metadata_
    }
    return household


def fake_profile():
    profile = SimpleNamespace(
        nutritional_preferences={}, dietary_groups=[], allergies=[], properties={}, updated_at=None
    )
    profile.to_dict = lambda: {"household_member_id": "m-1"}
    return profile


@pytest.fixture
def dropped(monkeypatch):
    """Every cache entry a write drops, as (kind, id)."""
    calls = []
    monkeypatch.setattr(
        HOUSEHOLD_MEMBER, "invalidate_cache", lambda entity_id: calls.append(("member", entity_id))
    )
    monkeypatch.setattr(
        HOUSEHOLD, "invalidate_cache", lambda entity_id: calls.append(("household", entity_id))
    )
    # The profile cache has its own, already-tested invalidation; keep Redis out of it.
    monkeypatch.setattr(members_module, "_profile_cache_invalidate", lambda member_id: None)
    return calls


class TestMemberWrites:
    @pytest.mark.asyncio
    async def test_renaming_a_member_drops_its_cached_copy(self, monkeypatch, dropped):
        session = install_session(monkeypatch, members_module, [FakeResult(one=fake_member())])
        result = await HOUSEHOLD_MEMBER.patch("m-1", {"name": "Maya"})
        assert result["name"] == "Maya"
        assert session.committed
        assert ("member", "m-1") in dropped

    @pytest.mark.asyncio
    async def test_a_profile_change_drops_the_member_copy_too(self, monkeypatch, dropped):
        # The member's cached copy embeds the profile, so a profile write that
        # only emptied the profile cache left GET /members/{id} stale.
        install_session(monkeypatch, members_module, [FakeResult(one=fake_profile())])
        await HOUSEHOLD_MEMBER.update_member_profile("m-1", {"allergies": ["nuts"]})
        assert ("member", "m-1") in dropped

    @pytest.mark.asyncio
    async def test_deleting_a_member_drops_the_member_and_its_household(self, monkeypatch, dropped):
        # The household's cached copy embeds its member list.
        install_session(
            monkeypatch, members_module, [FakeResult(one="h-1"), FakeResult(rowcount=1)]
        )
        assert await HOUSEHOLD_MEMBER.delete("m-1") is True
        assert ("member", "m-1") in dropped
        assert ("household", "h-1") in dropped

    @pytest.mark.asyncio
    async def test_adding_a_member_drops_the_household_copy(self, monkeypatch, dropped):
        install_session(
            monkeypatch, members_module,
            [FakeResult(one=fake_household()), FakeResult(one=fake_member(name="Ana"))],
        )
        created = await HOUSEHOLD_MEMBER.create(
            {"household_id": "h-1", "name": "Ana", "age_group": "adult"}, creator={"sub": "u-1"}
        )
        assert created["name"] == "Ana"
        assert ("household", "h-1") in dropped


class TestHouseholdWrites:
    @pytest.mark.asyncio
    async def test_renaming_a_household_drops_its_cached_copy(self, monkeypatch, dropped):
        install_session(monkeypatch, households_module, [FakeResult(one=fake_household())])
        result = await HOUSEHOLD.patch("h-1", {"name": "The Papadopoulos kitchen"})
        assert result["name"] == "The Papadopoulos kitchen"
        assert ("household", "h-1") in dropped

    @pytest.mark.asyncio
    async def test_deleting_a_household_drops_it_and_every_member(self, monkeypatch, dropped):
        # Members are removed by cascade; a cached copy must not outlive the row.
        install_session(
            monkeypatch, households_module,
            [FakeResult(rows=["m-1", "m-2"]), FakeResult(rowcount=1)],
        )
        assert await HOUSEHOLD.delete("h-1") is True
        assert ("household", "h-1") in dropped
        assert ("member", "m-1") in dropped
        assert ("member", "m-2") in dropped


class TestTheExpiry:
    def test_a_cached_entity_expires(self, monkeypatch):
        # A missed invalidation is then a stale minute, not a stale forever.
        stored = {}
        monkeypatch.setattr(
            entity_module.REDIS, "set",
            lambda key, value, ttl_seconds=None: stored.update({key: ttl_seconds}),
        )
        monkeypatch.setitem(config.settings, "CACHE_ENABLED", True)
        HOUSEHOLD.cache("h-1", {"id": "h-1"})
        assert stored["h-1"] == config.settings["ENTITY_CACHE_TTL_SECONDS"] > 0
