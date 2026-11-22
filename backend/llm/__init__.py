"""Provider-agnostic language model layer.

Every provider has the same tiny interface:  chat(messages) -> str
where messages is a list of {"role": "system"|"user"|"assistant", "content": str}.

The agent loop only ever sees that interface, so switching between a local
model and a hosted one is a one-line .env change (or the model picker in the UI):

    LLM_PROVIDER=ollama      LLM_MODEL=qwen3:4b            local, free
    LLM_PROVIDER=groq        LLM_MODEL=qwen/qwen3-32b      hosted, free tier
    LLM_PROVIDER=openai      LLM_MODEL=gpt-4.1-mini        hosted, paid
    LLM_PROVIDER=claude      LLM_MODEL=claude-haiku-5-5    hosted, paid
    LLM_PROVIDER=gemini      LLM_MODEL=gemini-2.5-flash    hosted, free tier
    LLM_PROVIDER=openrouter  LLM_MODEL=<any OpenRouter id> one key, hundreds of models
    LLM_PROVIDER=custom      LLM_MODEL=<name>              any OpenAI-compatible server
                                                           (LM Studio, vLLM, Together, ...)
Model names change often; the defaults below are only starting points.
"""

import os
import time
from typing import Callable

import requests

from backend import config


class LLMError(RuntimeError):
    """The model could not be reached. Reported honestly, never replaced with a fake answer."""


class OllamaProvider:
    """Local model through the Ollama server (same setup as the class notebook)."""

    def __init__(self, model: str = "", host: str = config.OLLAMA_HOST, temperature: float = 0.2):
        self.model = model or "qwen3:4b"
        self.host = host.rstrip("/")
        self.temperature = temperature
        self.name = f"ollama:{self.model}"

    def chat(self, messages: list[dict]) -> str:
        body = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "format": "json",   # ask Ollama to constrain output to JSON
            "think": False,     # Qwen3: skip the long thinking block
            "options": {"temperature": self.temperature},
        }
        try:
            r = requests.post(f"{self.host}/api/chat", json=body, timeout=300)
            if r.status_code == 400 and "think" in r.text.lower():  # model without a thinking mode
                body.pop("think")
                r = requests.post(f"{self.host}/api/chat", json=body, timeout=300)
            r.raise_for_status()
        except requests.RequestException as e:
            raise LLMError(f"Ollama at {self.host} failed: {e}. Is `ollama serve` running and `{self.model}` pulled?") from e
        return r.json()["message"]["content"]


def ollama_models(host: str = config.OLLAMA_HOST) -> list[str]:
    """Models installed in the local Ollama server ([] if it is not running)."""
    try:
        r = requests.get(f"{host.rstrip('/')}/api/tags", timeout=2)
        return sorted(m["name"] for m in r.json().get("models", []))
    except (requests.RequestException, ValueError):
        return []


class OpenAICompatibleProvider:
    """Any OpenAI-style /chat/completions endpoint: Groq, OpenAI, OpenRouter, Gemini, custom servers."""

    def __init__(self, base_url: str, api_key: str, model: str, label: str, temperature: float = 0.2):
        if not model:
            raise LLMError(f"No model name for {label}. Set LLM_MODEL in .env.")
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.temperature = temperature
        self.name = f"{label}:{model}"

    def chat(self, messages: list[dict]) -> str:
        body = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
            "response_format": {"type": "json_object"},
        }
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        r = None
        for attempt in range(5):
            try:
                r = requests.post(f"{self.base_url}/chat/completions", json=body, headers=headers, timeout=180)
            except requests.RequestException as e:
                raise LLMError(f"{self.name} request failed: {e}") from e
            if r.status_code == 429 and attempt < 4:  # rate limit: wait and retry
                time.sleep(min(float(r.headers.get("retry-after", 5) or 5), 30))
                continue
            if r.status_code == 400:
                # Some models reject custom temperature (reasoning models) or JSON mode.
                # Drop the offending option and retry; the prompt still asks for JSON.
                text = r.text.lower()
                if "temperature" in text and "temperature" in body:
                    body.pop("temperature")
                    continue
                if "response_format" in body:
                    body.pop("response_format")
                    continue
            if not r.ok:
                raise LLMError(f"{self.name} returned HTTP {r.status_code}: {r.text[:300]}")
            try:
                return r.json()["choices"][0]["message"]["content"] or ""
            except (KeyError, IndexError, ValueError) as e:
                raise LLMError(f"{self.name} returned an unexpected response: {r.text[:300]}") from e
        raise LLMError(f"{self.name} kept failing (last HTTP {r.status_code if r is not None else '?'}).")


class ClaudeProvider:
    """Anthropic Messages API over plain HTTP (no SDK needed)."""

    def __init__(self, model: str = "", api_key: str = "", temperature: float = 0.2):
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
        if not self.api_key:
            raise LLMError("No ANTHROPIC_API_KEY in .env.")
        self.model = model or "claude-haiku-5-5"
        self.temperature = temperature
        self.name = f"claude:{self.model}"

    def chat(self, messages: list[dict]) -> str:
        system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
        convo = [m for m in messages if m["role"] != "system"]
        try:
            r = requests.post(
                "https://api.anthropic.com/v1/messages",
                headers={"x-api-key": self.api_key, "anthropic-version": "2023-06-01"},
                json={"model": self.model, "max_tokens": 1024, "system": system,
                      "messages": convo, "temperature": self.temperature},
                timeout=120,
            )
        except requests.RequestException as e:
            raise LLMError(f"Claude request failed: {e}") from e
        if not r.ok:
            raise LLMError(f"Claude returned HTTP {r.status_code}: {r.text[:300]}")
        return "".join(b.get("text", "") for b in r.json()["content"] if b.get("type") == "text")


class ScriptedLLM:
    """Fake model for tests: returns pre-written replies in order, or computes them.

    This makes tests deterministic and lets us script BAD proposals (unknown
    tools, missing arguments, false claims) to prove the harness catches them.
    """

    def __init__(self, script: list[str] | Callable[[list[dict]], str]):
        self.script = script
        self.calls: list[list[dict]] = []
        self.name = "scripted"

    def chat(self, messages: list[dict]) -> str:
        self.calls.append([dict(m) for m in messages])
        if callable(self.script):
            return self.script(messages)
        if not self.script:
            raise LLMError("ScriptedLLM ran out of replies.")
        return self.script.pop(0)


# One entry per provider. OpenAI-compatible ones only differ by URL, key and default model.
PROVIDERS = {
    "ollama":     {"kind": "ollama", "default": "qwen3:4b", "local": True, "key_env": None},
    "groq":       {"kind": "openai", "base_url": "https://api.groq.com/openai/v1",
                   "key_env": "GROQ_API_KEY", "default": "qwen/qwen3-32b"},
    "openai":     {"kind": "openai", "base_url": "https://api.openai.com/v1",
                   "key_env": "OPENAI_API_KEY", "default": "gpt-4.1-mini"},
    "openrouter": {"kind": "openai", "base_url": "https://openrouter.ai/api/v1",
                   "key_env": "OPENROUTER_API_KEY", "default": "qwen/qwen3-32b"},
    "gemini":     {"kind": "openai", "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
                   "key_env": "GEMINI_API_KEY", "default": "gemini-2.5-flash"},
    "claude":     {"kind": "claude", "key_env": "ANTHROPIC_API_KEY", "default": "claude-haiku-5-5"},
    "custom":     {"kind": "openai", "base_url_env": "OPENAI_COMPAT_BASE_URL",
                   "key_env": "OPENAI_COMPAT_API_KEY", "key_optional": True, "default": ""},
}


def provider_status() -> list[dict]:
    """Which providers are usable right now (key present / server running). Never returns key values."""
    out = []
    for name, p in PROVIDERS.items():
        if p["kind"] == "ollama":
            models = ollama_models()
            ready, why = bool(models), ("" if models else "Ollama server not reachable")
        elif name == "custom":
            models = []
            ready = bool(os.environ.get(p["base_url_env"]))
            why = "" if ready else "set OPENAI_COMPAT_BASE_URL"
        else:
            models = []
            ready = bool(os.environ.get(p["key_env"]))
            why = "" if ready else f"set {p['key_env']}"
        out.append({"provider": name, "ready": ready, "why_not": why, "default_model": p["default"],
                    "local": p.get("local", False), "models": models})
    return out


def get_provider(provider: str | None = None, model: str | None = None):
    """Build the provider named in .env (or passed in)."""
    provider = (provider or config.LLM_PROVIDER).lower()
    model = model if model else (config.LLM_MODEL if provider == config.LLM_PROVIDER else "")
    spec = PROVIDERS.get(provider)
    if spec is None:
        raise LLMError(f"Unknown LLM_PROVIDER '{provider}'. Use one of: {', '.join(PROVIDERS)}.")
    model = model or spec["default"]
    if spec["kind"] == "ollama":
        return OllamaProvider(model)
    if spec["kind"] == "claude":
        return ClaudeProvider(model)
    key = os.environ.get(spec["key_env"], "")
    if not key and not spec.get("key_optional"):
        raise LLMError(f"No {spec['key_env']} in .env for provider '{provider}'.")
    base_url = spec.get("base_url") or os.environ.get(spec.get("base_url_env", ""), "")
    if not base_url:
        raise LLMError(f"Provider '{provider}' needs {spec['base_url_env']} in .env.")
    return OpenAICompatibleProvider(base_url, key, model, provider)
