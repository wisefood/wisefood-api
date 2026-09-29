"""Turning a guest into a permanent account.

A guest is already a real Keycloak user and owns their household by `sub`, so
the claim is an in-place upgrade with nothing to migrate. What has to be right
is the *order* of the writes and the guards around them: this endpoint takes a
password, changes an identity, and decides whether an account keeps existing.
"""
import pytest

import main  # noqa: F401
import guests


class FakeAdmin:
    """Enough of KeycloakAdmin to watch what a claim does, and in what order."""

    def __init__(self, *, roles=("guest",), users_with_email=()):
        self.roles = list(roles)
        self.users_with_email = list(users_with_email)
        self.calls = []
        self.updates = []
        self.password = None
        self.verify_sent = False
        self.fail_on = set()

    def _record(self, name):
        self.calls.append(name)
        if name in self.fail_on:
            raise RuntimeError(f"{name} failed")

    def get_realm_roles_of_user(self, user_id):
        return [{"name": r} for r in self.roles]

    def get_users(self, query):
        return list(self.users_with_email)

    def update_user(self, user_id, payload):
        self._record("update_user")
        self.updates.append(payload)

    def set_user_password(self, user_id, password, temporary=False):
        self._record("set_user_password")
        self.password = password

    def get_realm_role(self, name):
        return {"name": name}

    def delete_realm_roles_of_user(self, user_id, roles):
        self._record("delete_realm_roles_of_user")
        for r in roles:
            if r["name"] in self.roles:
                self.roles.remove(r["name"])

    def send_verify_email(self, user_id):
        self._record("send_verify_email")
        self.verify_sent = True


class FakeHousehold:
    """The household entity as the claim sees it: one lookup, one patch."""

    def __init__(self, household=None):
        self.household = household
        self.patches = []
        self.fail = False

    async def get_by_owner(self, owner_id):
        if self.fail:
            raise RuntimeError("database away")
        return self.household

    async def patch(self, household_id, spec):
        self.patches.append((household_id, spec))
        self.household = {**self.household, **spec}
        return self.household


@pytest.fixture
def household(monkeypatch):
    import api.v1.households as households

    fake = FakeHousehold({"id": "h-1", "name": "Guest Household",
                          "metadata": {"guest": True}})
    monkeypatch.setattr(households, "HOUSEHOLD", fake)
    return fake


@pytest.fixture
def admin(monkeypatch, household):
    fake = FakeAdmin()
    monkeypatch.setattr(guests, "KEYCLOAK_ADMIN_CLIENT", lambda: fake)
    return fake


async def claim(**kw):
    return await guests.claim_guest(
        "user-1", email=kw.pop("email", "ana@example.org"),
        password=kw.pop("password", "a-long-enough-password"), **kw
    )


class TestTheUpgrade:
    @pytest.mark.asyncio
    async def test_the_guest_role_is_removed(self, admin):
        await claim()
        assert "guest" not in admin.roles

    @pytest.mark.asyncio
    async def test_identity_and_password_are_set(self, admin):
        await claim(first_name="Ana", last_name="K")
        identity = admin.updates[0]
        assert identity["email"] == "ana@example.org"
        assert identity["username"] == "ana@example.org", "username must leave the guest- prefix"
        assert identity["firstName"] == "Ana"
        assert admin.password == "a-long-enough-password"

    @pytest.mark.asyncio
    async def test_the_email_is_not_marked_verified(self, admin):
        # Marking it verified would let anyone claim an address they do not
        # own, and permanently block the real owner from registering it.
        await claim()
        assert admin.updates[0]["emailVerified"] is False
        assert admin.verify_sent is True

    @pytest.mark.asyncio
    async def test_the_email_is_normalised(self, admin):
        result = await claim(email="  Ana@Example.ORG  ")
        assert result["email"] == "ana@example.org"


class TestTheOrderOfWrites:
    """Failure must fall backwards into "still a guest", never forwards."""

    @pytest.mark.asyncio
    async def test_credentials_are_set_before_the_role_is_dropped(self, admin):
        await claim()
        assert admin.calls.index("set_user_password") < admin.calls.index(
            "delete_realm_roles_of_user"
        ), "a de-guested account with no password cannot sign in at all"

    @pytest.mark.asyncio
    async def test_a_failed_password_leaves_them_a_guest(self, admin):
        admin.fail_on = {"set_user_password"}
        with pytest.raises(Exception):
            await claim()
        assert "guest" in admin.roles, "still reapable, still retryable"

    @pytest.mark.asyncio
    async def test_tidying_up_cannot_fail_the_claim(self, admin):
        # By the time the attributes are cleared the account is already
        # permanent; reporting failure would tell the user it did not work.
        admin.fail_on = {"send_verify_email"}
        result = await claim()
        assert "guest" not in admin.roles
        assert result["verification_sent"] is False


class TestTheHousehold:
    """The claim leaves "Guest Household" and "Guest" in place; the flag it
    sets is what sends the person through the setup wizard to fix that."""

    @pytest.mark.asyncio
    async def test_the_household_is_flagged_for_setup(self, admin, household):
        result = await claim()
        assert result["household_setup_pending"] is True
        (household_id, spec), = household.patches
        assert household_id == "h-1"
        assert spec["metadata"]["onboarding"] == "pending"

    @pytest.mark.asyncio
    async def test_the_household_stops_being_a_guests(self, admin, household):
        # A permanent account's household must not read as a guest's — the
        # flag is what `guest: true` becomes, not something beside it.
        await claim()
        metadata = household.patches[0][1]["metadata"]
        assert metadata["guest"] is False
        assert isinstance(metadata["claimed_at"], int)

    @pytest.mark.asyncio
    async def test_other_metadata_survives(self, admin, household):
        # The PATCH replaces metadata wholesale, so the helper has to carry
        # whatever else is there rather than write only its own keys.
        household.household["metadata"]["theme"] = "dark"
        await claim()
        assert household.patches[0][1]["metadata"]["theme"] == "dark"

    @pytest.mark.asyncio
    async def test_the_flag_is_set_after_the_account_is_permanent(self, admin, household):
        # Recorded through the admin call log: the role drop must come first,
        # so a failure here can only leave a permanent account unflagged,
        # never a flagged guest that the reaper then deletes.
        calls = []
        original = household.patch

        async def patch(household_id, spec):
            calls.append(list(admin.calls))
            return await original(household_id, spec)

        household.patch = patch
        await claim()
        assert "delete_realm_roles_of_user" in calls[0]

    @pytest.mark.asyncio
    async def test_a_missing_household_cannot_fail_the_claim(self, admin, household):
        # A guest who erased their household before claiming still gets to
        # keep the account; they just start from the ordinary setup wizard.
        household.household = None
        result = await claim()
        assert "guest" not in admin.roles
        assert result["household_setup_pending"] is False
        assert household.patches == []

    @pytest.mark.asyncio
    async def test_a_database_fault_cannot_fail_the_claim(self, admin, household):
        household.fail = True
        result = await claim()
        assert "guest" not in admin.roles
        assert result["household_setup_pending"] is False


class TestTheGuards:
    @pytest.mark.asyncio
    async def test_a_real_account_cannot_be_claimed(self, admin):
        admin.roles = ["expert"]
        from exceptions import AuthorizationError

        with pytest.raises(AuthorizationError):
            await claim()

    @pytest.mark.asyncio
    async def test_an_email_already_in_use_is_refused(self, admin):
        admin.users_with_email = [{"id": "somebody-else"}]
        from exceptions import ConflictError

        with pytest.raises(ConflictError):
            await claim()
        assert "guest" in admin.roles, "a refused claim changes nothing"

    @pytest.mark.asyncio
    async def test_the_caller_matching_the_email_is_not_a_collision(self, admin):
        # Keycloak returns the caller themselves when the address is already
        # theirs; that is a retry, not a conflict.
        admin.users_with_email = [{"id": "user-1"}]
        await claim()
        assert "guest" not in admin.roles
