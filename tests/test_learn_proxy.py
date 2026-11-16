"""FoodScholar Learn, as proxied by this gateway.

Two things the UI relies on: a claim check's `intent` must reach FoodScholar
(QARequest is rebuilt here, so a field this schema does not know is silently
dropped), and the daily quiz must have a route at all. Same shape as the other
proxy tests: assert the route, the schema and the forwarding.
"""
import asyncio
import sys

import pytest

PREFIX = "/api/v1/foodscholar"


def _main():
    sys.path.insert(0, "src")
    import main

    return main


def _route(path):
    for route in _main().api.routes:
        if getattr(route, "path", "") == path:
            return route
    raise AssertionError(f"{path} is not registered")


def test_quiz_route_is_registered_as_get():
    route = _route(f"{PREFIX}/qa/quiz")
    assert "GET" in route.methods


def test_intent_is_forwarded_when_set():
    sys.path.insert(0, "src")
    from schemas import QARequest

    body = QARequest(question="Eggs raise your blood cholesterol.", intent="claim_check")
    assert body.model_dump(exclude_none=True)["intent"] == "claim_check"


def test_intent_is_left_out_when_not_set():
    sys.path.insert(0, "src")
    from schemas import QARequest

    body = QARequest(question="Is fruit juice as healthy as fruit?")
    assert "intent" not in body.model_dump(exclude_none=True)


def test_unknown_intent_is_rejected():
    sys.path.insert(0, "src")
    from pydantic import ValidationError
    from schemas import QARequest

    with pytest.raises(ValidationError):
        QARequest(question="Coffee dehydrates you.", intent="something_else")


def test_get_quiz_forwards_member_language_and_count(monkeypatch):
    sys.path.insert(0, "src")
    from backend import foodscholar as fs_backend

    calls = []

    async def fake_get(path, params=None, **_kwargs):
        calls.append((path, params))
        return {"questions": []}

    monkeypatch.setattr(fs_backend.FOODSCHOLAR, "get", fake_get)
    asyncio.run(fs_backend.FOODSCHOLAR.get_quiz(member_id="m-1", language="hu", count=3))
    assert calls == [("/api/v1/qa/quiz", {"member_id": "m-1", "language": "hu", "count": 3})]
