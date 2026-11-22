"""Provider layer with a fake HTTP transport: no keys, no network."""

import json

import pytest

from backend import llm
from backend.llm import LLMError, get_provider


class FakeResponse:
    def __init__(self, status, body, headers=None):
        self.status_code, self._body, self.headers = status, body, headers or {}
        self.text = json.dumps(body)
        self.ok = status < 400

    def json(self):
        return self._body

    def raise_for_status(self):
        if not self.ok:
            import requests
            raise requests.HTTPError(self.text)


def ok_chat(content='{"action": "reply", "argument": {"message": "hi"}}'):
    return FakeResponse(200, {"choices": [{"message": {"content": content}}]})


@pytest.fixture
def keys(monkeypatch):
    for k in ["GROQ_API_KEY", "OPENAI_API_KEY", "OPENROUTER_API_KEY", "GEMINI_API_KEY", "ANTHROPIC_API_KEY",
              "OPENAI_COMPAT_API_KEY"]:
        monkeypatch.setenv(k, "test-key")
    monkeypatch.setenv("OPENAI_COMPAT_BASE_URL", "http://localhost:1234/v1")


@pytest.mark.parametrize("name, url", [
    ("groq", "https://api.groq.com/openai/v1/chat/completions"),
    ("openai", "https://api.openai.com/v1/chat/completions"),
    ("openrouter", "https://openrouter.ai/api/v1/chat/completions"),
    ("gemini", "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"),
    ("custom", "http://localhost:1234/v1/chat/completions"),
])
def test_openai_compatible_providers(keys, monkeypatch, name, url):
    calls = []
    monkeypatch.setattr(llm.requests, "post", lambda u, json, headers, timeout: calls.append((u, json, headers)) or ok_chat())
    p = get_provider(name, "some-model")
    assert p.chat([{"role": "user", "content": "x"}]).startswith('{"action"')
    assert calls[0][0] == url and calls[0][1]["model"] == "some-model"
    assert calls[0][2] == {"Authorization": "Bearer test-key"}


def test_drops_unsupported_options_and_retries(keys, monkeypatch):
    replies = [FakeResponse(400, {"error": "temperature is not supported for this model"}),
               FakeResponse(400, {"error": "response_format not supported"}),
               ok_chat()]
    bodies = []
    monkeypatch.setattr(llm.requests, "post", lambda u, json, headers, timeout: bodies.append(dict(json)) or replies.pop(0))
    get_provider("openai", "reasoning-model").chat([{"role": "user", "content": "x"}])
    assert "temperature" in bodies[0] and "temperature" not in bodies[1]
    assert "response_format" in bodies[1] and "response_format" not in bodies[2]


def test_claude_request_shape(keys, monkeypatch):
    sent = {}

    def post(url, headers, json, timeout):
        sent.update(url=url, headers=headers, body=json)
        return FakeResponse(200, {"content": [{"type": "text", "text": '{"action": "reply"}'}]})

    monkeypatch.setattr(llm.requests, "post", post)
    out = get_provider("claude").chat([{"role": "system", "content": "rules"}, {"role": "user", "content": "hi"}])
    assert out == '{"action": "reply"}'
    assert sent["body"]["system"] == "rules" and sent["body"]["messages"] == [{"role": "user", "content": "hi"}]
    assert sent["headers"]["x-api-key"] == "test-key"


def test_custom_server_without_key_sends_no_auth(monkeypatch):
    monkeypatch.setenv("OPENAI_COMPAT_BASE_URL", "http://localhost:1234/v1")
    monkeypatch.delenv("OPENAI_COMPAT_API_KEY", raising=False)
    seen = []
    monkeypatch.setattr(llm.requests, "post", lambda u, json, headers, timeout: seen.append(headers) or ok_chat())
    get_provider("custom", "local-model").chat([{"role": "user", "content": "x"}])
    assert seen == [{}]   # LM Studio style: no key needed


def test_missing_key_is_a_clear_error(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    with pytest.raises(LLMError, match="OPENROUTER_API_KEY"):
        get_provider("openrouter")


def test_status_never_contains_key_values(keys):
    assert "test-key" not in json.dumps(llm.provider_status())
