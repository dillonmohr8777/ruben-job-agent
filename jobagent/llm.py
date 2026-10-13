"""Tiny OpenAI-compatible chat client (OpenRouter by default). Optional: without a key the agent still works."""

from __future__ import annotations

import json
import os
import urllib.request
from collections.abc import Callable

LLM = Callable[[str, str], str]  # (system, user) -> text


def from_env(env=os.environ, model_var: str = "LLM_MODEL") -> LLM | None:
    key = env.get("LLM_API_KEY")
    if not key:
        return None
    base, model = env.get("LLM_BASE_URL", "https://openrouter.ai/api/v1").rstrip("/"), env.get(model_var) or env.get("LLM_MODEL", "anthropic/claude-sonnet-4.5")
    max_tokens = int(env.get("LLM_MAX_TOKENS", "4000"))  # reasoning models spend this budget on hidden thinking first; too small = empty reply

    def call(system: str, user: str) -> str:
        body = json.dumps({"model": model, "temperature": 0.3, "max_tokens": max_tokens,
                           "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}).encode()
        req = urllib.request.Request(f"{base}/chat/completions", body, {"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=90) as r:
            choice = json.load(r)["choices"][0]
        if not (choice["message"].get("content") or "").strip():
            raise ValueError(f"model returned no text (finish_reason={choice.get('finish_reason')}); raise LLM_MAX_TOKENS if this is a reasoning model")
        return choice["message"]["content"]

    return call


def parse_json(text: str) -> dict:
    a, b = text.find("{"), text.rfind("}")
    if a < 0 or b < a:
        raise ValueError("no JSON object in model reply")
    return json.loads(text[a:b + 1])
