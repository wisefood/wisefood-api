"""Reading Langfuse prompts in the console: the fetch, and what we say about them.

The two failures this guards against were both invisible: a namespaced prompt
name sent with a literal slash, and a prompt with no ``production`` label.
Both came back from Langfuse as 404, both showed in the console as "Could not
load this prompt", and neither was about the prompt.
"""
import pytest

import main  # noqa: F401
from backend import langfuse_read
from backend.langfuse_read import LANGFUSE_READ
from backend.prompt_template import convention, describe, references, variables

TEXT = {"name": "movie-critic", "type": "text",
        "prompt": "As a {{ criticLevel }} movie critic, do you like {{movie}}? {{movie}} again."}
CHAT = {"name": "movie-critic-chat", "type": "chat", "prompt": [
    {"role": "system", "content": "You are a {{criticLevel}} critic"},
    {"type": "placeholder", "name": "chat_history"},
    {"role": "user", "content": "What about {{movie}}?"},
]}
FOODCHAT = {"name": "foodchat/plan_grader_user", "type": "text",
            "prompt": 'Score {plan} for {member}. Reply as JSON: {{"score": 1, "why": "{reason}"}}'}
WITH_REF = {"name": "outer", "type": "text",
            "prompt": "Intro. @@@langfusePrompt:name=shared/tone|label=production@@@ Then {{topic}}."}


class TestDescribe:
    def test_mustache_variables_deduplicated_in_order(self):
        assert variables(TEXT)["mustache"] == ["criticLevel", "movie"]
        assert describe(TEXT)["convention"] == "mustache"

    def test_chat_prompts_collect_variables_and_placeholders(self):
        d = describe(CHAT)
        assert d["variables"] == ["criticLevel", "movie"]
        assert d["placeholders"] == ["chat_history"]
        assert d["is_chat"] is True

    def test_foodchat_format_convention_is_recognised(self):
        # ``{{"score"...}}`` is an escaped literal brace, not a variable; the
        # real variables are the single-brace ones — and ``{reason}`` sits
        # inside the escaped JSON and is still a field to str.format.
        d = describe(FOODCHAT)
        assert d["convention"] == "format"
        assert d["variables"] == ["plan", "member", "reason"]

    def test_mustache_wins_when_both_appear(self):
        found = {"mustache": ["a"], "format": ["b"]}
        assert convention(found) == "mustache"

    def test_references_are_parsed_and_not_counted_as_variables(self):
        assert references(WITH_REF) == [{"name": "shared/tone", "label": "production"}]
        assert describe(WITH_REF)["variables"] == ["topic"]

    def test_garbage_in_is_an_empty_description(self):
        for junk in (None, {}, {"prompt": 7}, {"prompt": [None, 3, {"content": 1}]}):
            d = describe(junk)
            assert d["variables"] == [] and d["placeholders"] == [] and d["references"] == []


class FakeResponse:
    def __init__(self, status, body=None):
        self.status_code = status
        self._body = body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._body


class FakeClient:
    """Answers like Langfuse: a 404 for anything not in `serving`."""

    def __init__(self, serving):
        self.serving = serving  # (encoded_path, frozenset(params)) -> body
        self.calls = []

    async def get(self, path, params=None):
        self.calls.append((path, dict(params or {})))
        key = (path, frozenset((params or {}).items()))
        body = self.serving.get(key)
        return FakeResponse(200, body) if body is not None else FakeResponse(404)


@pytest.fixture
def langfuse(monkeypatch):
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk")

    def install(serving):
        client = FakeClient(serving)
        monkeypatch.setattr(LANGFUSE_READ, "_client", client)
        return client

    return install


ENCODED = "/api/public/v2/prompts/foodchat%2Fplan_grader_user"


class TestFetchPrompt:
    @pytest.mark.asyncio
    async def test_namespaced_names_are_one_encoded_segment(self, langfuse):
        client = langfuse({(ENCODED, frozenset({("label", "production")})): FOODCHAT})
        got = await LANGFUSE_READ.fetch_prompt("foodchat/plan_grader_user")
        assert got["name"] == "foodchat/plan_grader_user"
        assert all("/plan_grader_user" not in path for path, _ in client.calls)

    @pytest.mark.asyncio
    async def test_falls_back_to_latest_when_nothing_is_in_production(self, langfuse):
        client = langfuse({(ENCODED, frozenset({("label", "latest")})): FOODCHAT})
        got = await LANGFUSE_READ.fetch_prompt("foodchat/plan_grader_user")
        assert got is not None and got["_fetched_with"] == {
            "label": "latest", "version": None, "resolved": True}
        assert [c[1] for c in client.calls[:2]] == [{"label": "production"}, {"label": "latest"}]

    @pytest.mark.asyncio
    async def test_an_explicit_version_is_asked_for_and_not_second_guessed(self, langfuse):
        client = langfuse({(ENCODED, frozenset({("version", 3)})): FOODCHAT})
        got = await LANGFUSE_READ.fetch_prompt("foodchat/plan_grader_user", version=3)
        assert got["_fetched_with"]["version"] == 3
        assert all("label" not in c[1] for c in client.calls)

    @pytest.mark.asyncio
    async def test_unresolvable_references_still_load_raw(self, langfuse):
        client = langfuse({
            (ENCODED, frozenset({("label", "production"), ("resolve", "false")})): WITH_REF,
        })
        got = await LANGFUSE_READ.fetch_prompt("foodchat/plan_grader_user")
        assert got["_fetched_with"]["resolved"] is False
        assert describe(got)["references"][0]["name"] == "shared/tone"

    @pytest.mark.asyncio
    async def test_a_prompt_that_does_not_exist_is_none(self, langfuse):
        langfuse({})
        assert await LANGFUSE_READ.fetch_prompt("nope") is None

    @pytest.mark.asyncio
    async def test_disabled_is_none_without_a_call(self, monkeypatch):
        monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
        monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
        monkeypatch.setattr(LANGFUSE_READ, "_client", None)
        assert await LANGFUSE_READ.fetch_prompt("anything") is None
