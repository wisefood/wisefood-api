"""End-to-end over HTTP: create a share, read it anonymously, email it, revoke it.

Exercises the real routes through a real ASGI stack against a real Postgres.
Auth and the plan lookup are stubbed at their seams — Keycloak and the
household tables are not what is under test here; the routes, the ownership
check, the scrub and the headers are.
"""
import os
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

import main
import sharing

# The async engine is a process-wide singleton and its pool binds to the loop
# that created it, so every test here shares one loop rather than getting its
# own — otherwise the second test to touch Postgres fails on a connection the
# current loop does not own.
pytestmark = pytest.mark.asyncio(loop_scope="module")

PLAN = {
    "id": "plan-1", "household_id": "house-1", "date": "2026-09-20",
    "reasoning": "Ana is allergic to peanuts",
    "constraints_applied": [{"detail": "for Ana's allergy"}],
    "breakfast": {"recipe_id": "r1", "title": "Porridge", "ingredients": "oats",
                  "match_reasons": [{"why": "low sodium for Dimitris"}]},
}


ALICE = {"sub": "alice", "email": "alice@example.org", "given_name": "Alice",
         "realm_access": {"roles": ["user"]}}


def _signed_in_as(monkeypatch, payload):
    """Make every auth seam agree on who is calling.

    Two of them, because the routes use both: the `auth()` dependency verifies
    the bearer token, and `kutils.current_user` re-reads it to get the claims.
    Stubbing only one leaves a 401 nobody expected.
    """
    import auth as auth_module
    import kutils

    async def verify(token):
        return payload

    monkeypatch.setattr(auth_module, "api_verify_token", verify)
    monkeypatch.setattr(kutils, "current_user", lambda request: payload)
    monkeypatch.setattr(kutils, "is_admin", lambda request: False)


@pytest_asyncio.fixture(autouse=True, loop_scope="module")
async def clean_shares():
    """Start each test with no shares for the people in it.

    These tests run against a real database that other runs have used, and
    "the list contains exactly my share" is only true of a table nobody else
    has touched. Clearing the two owners keeps the assertions about this test
    rather than about the leftovers.
    """
    from sqlalchemy import delete

    from backend.postgres import POSTGRES_ASYNC_SESSION_FACTORY
    from sql import ShareLink

    async def wipe():
        async with POSTGRES_ASYNC_SESSION_FACTORY()() as db:
            await db.execute(
                delete(ShareLink).where(ShareLink.owner_id.in_(["alice", "mallory"]))
            )
            await db.commit()

    await wipe()
    yield
    await wipe()


@pytest.fixture
def owner(monkeypatch):
    """A signed-in owner, and a plan they own."""
    from routers import shares as shares_router

    _signed_in_as(monkeypatch, ALICE)

    async def fake_load(request, kind, plan_id):
        if plan_id != "plan-1":
            from exceptions import NotFoundError
            raise NotFoundError(detail="No such meal plan")
        return PLAN, "Monday dinner", "alice"

    monkeypatch.setattr(shares_router, "_load_owned_plan", fake_load)


@pytest_asyncio.fixture(loop_scope="module")
async def client():
    async with AsyncClient(
        transport=ASGITransport(app=main.api), base_url="http://t",
        headers={"Authorization": "Bearer test-token"},
    ) as c:
        yield c


async def test_the_whole_journey(owner, client, monkeypatch):
    # --- the owner publishes -------------------------------------------------
    made = await client.post("/api/v1/shares",
                             json={"kind": "meal_plan", "id": "plan-1"})
    assert made.status_code == 200, made.text
    token = made.json()["result"]["token"]
    assert len(token) >= 40

    # --- a stranger reads it, with no credentials at all ---------------------
    seen = await client.get(f"/api/v1/shares/public/{token}")
    assert seen.status_code == 200, seen.text
    body = seen.text.lower()
    for secret in ("ana", "peanut", "allerg", "dimitris", "house-1"):
        assert secret not in body, f"{secret!r} reached an anonymous reader"
    assert "porridge" in body

    # ...and the response cannot be indexed or leak the token onward
    assert "noindex" in seen.headers.get("x-robots-tag", "")
    assert seen.headers.get("referrer-policy") == "no-referrer"

    # --- the owner sees it listed -------------------------------------------
    listed = await client.get("/api/v1/shares")
    rows = {r["token"]: r for r in listed.json()["result"]["shares"]}
    assert token in rows
    assert rows[token]["view_count"] == 1 and rows[token]["live"] is True

    # --- emailing goes through the share ------------------------------------
    sent_to = {}

    async def fake_send(*, to, subject, html_body, text_body, reply_to=None):
        sent_to.update(to=to, subject=subject, html=html_body)
        return True

    import mailer
    monkeypatch.setitem(main.config.settings, "SMTP_HOST", "mail.example.org")
    monkeypatch.setattr(mailer, "send_email", fake_send)
    mailer._sent.clear()

    mailed = await client.post(f"/api/v1/shares/{token}/email", json={})
    assert mailed.status_code == 200, mailed.text
    assert mailed.json()["result"]["sent"] is True
    assert sent_to["to"] == "alice@example.org"
    assert sent_to["subject"] == "Hey Alice, here is your meal plan"
    assert "Porridge" in sent_to["html"]
    for secret in ("peanut", "Dimitris"):
        assert secret.lower() not in sent_to["html"].lower()

    # --- revoking closes it for everyone ------------------------------------
    gone = await client.delete(f"/api/v1/shares/{token}")
    assert gone.status_code == 200
    after = await client.get(f"/api/v1/shares/public/{token}")
    assert after.status_code == 404
    # ...and it can no longer be mailed either
    assert (await client.post(f"/api/v1/shares/{token}/email", json={})).status_code == 404


async def test_an_unknown_token_is_indistinguishable_from_a_revoked_one(owner, client):
    missing = await client.get("/api/v1/shares/public/definitely-not-a-real-token")
    assert missing.status_code == 404
    assert "noindex" in missing.headers.get("x-robots-tag", "")


async def test_a_plan_you_do_not_own_cannot_be_shared(owner, client):
    refused = await client.post("/api/v1/shares",
                                json={"kind": "meal_plan", "id": "someone-elses"})
    assert refused.status_code == 404


async def test_an_unshareable_kind_is_refused(owner, client):
    refused = await client.post("/api/v1/shares",
                                json={"kind": "chat_session", "id": "plan-1"})
    assert refused.status_code == 404


async def test_one_owner_cannot_touch_anothers_share(owner, client, monkeypatch):
    made = await client.post("/api/v1/shares", json={"kind": "meal_plan", "id": "plan-1"})
    token = made.json()["result"]["token"]

    _signed_in_as(monkeypatch, {"sub": "mallory", "email": "m@example.org",
                                "realm_access": {"roles": ["user"]}})

    assert (await client.delete(f"/api/v1/shares/{token}")).status_code == 404
    mine = (await client.get("/api/v1/shares")).json()["result"]["shares"]
    assert token not in {r["token"] for r in mine}
    # ...but the link itself still works for whoever holds it
    assert (await client.get(f"/api/v1/shares/public/{token}")).status_code == 200
