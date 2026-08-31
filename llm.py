"""CrewKeep LLM dispatcher — lean version of the xjobs llm_chat pattern.

Provider selection: env LLM_PROVIDER (deepseek default | anthropic), model via
LLM_MODEL or provider default. Keys: DEEPSEEK_API_KEY / ANTHROPIC_API_KEY.

DeepSeek is the default: ~$0.001 per screening call, quality is plenty for
resume triage. Anthropic (claude-sonnet-5) is the upgrade path if the mate
wants the best judgement; costs ~$0.01-0.03 per screen.

PITFALL (verified on xjobs): model name `deepseek-v4-flash` returns EMPTY
content — use the `deepseek-chat` alias, which routes to the same model.
"""
from __future__ import annotations

import json
import os

DEEPSEEK_BASE = "https://api.deepseek.com/chat/completions"
DEFAULT_MODELS = {"deepseek": "deepseek-chat", "anthropic": "claude-sonnet-5"}


def _get_key(name: str) -> str | None:
    return os.environ.get(name) or None


def _effective() -> dict:
    provider = os.environ.get("LLM_PROVIDER", "deepseek").strip().lower()
    if provider not in DEFAULT_MODELS:
        provider = "deepseek"
    model = os.environ.get("LLM_MODEL") or DEFAULT_MODELS[provider]
    return {"provider": provider, "model": model}


def _anthropic_chat(messages: list[dict], model: str, timeout: int = 120) -> str:
    import requests
    key = _get_key("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY not set")
    system = "\n\n".join(m["content"] for m in messages if m.get("role") == "system")
    msgs = [{"role": m["role"], "content": m["content"]}
            for m in messages if m.get("role") != "system"]
    body: dict = {"model": model, "max_tokens": 4000, "messages": msgs}
    if system:
        body["system"] = system
    r = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                 "content-type": "application/json"},
        json=body, timeout=timeout)
    r.raise_for_status()
    data = r.json()
    return "".join(b.get("text", "") for b in data.get("content", [])
                   if b.get("type") == "text")


def _openai_compat_chat(messages: list[dict], model: str, timeout: int = 120) -> str:
    import requests
    key = _get_key("DEEPSEEK_API_KEY")
    if not key:
        raise RuntimeError("DEEPSEEK_API_KEY not set")
    r = requests.post(
        DEEPSEEK_BASE,
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {key}"},
        json={"model": model, "messages": messages, "temperature": 0.2,
              "max_tokens": 4000,
              "response_format": {"type": "json_object"}},
        timeout=timeout)
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"]


def llm_json(messages: list[dict], timeout: int = 120) -> dict:
    """Chat -> parsed JSON dict. Tolerates prose-wrapped JSON; retries once
    with a 'pure JSON' reprimand before raising (the xjobs _extract_json
    lesson)."""
    cfg = _effective()
    raw = None
    for attempt in (1, 2):
        try:
            if cfg["provider"] == "anthropic":
                raw = _anthropic_chat(messages, cfg["model"], timeout=timeout)
            else:
                raw = _openai_compat_chat(messages, cfg["model"], timeout=timeout)
            return extract_json(raw)
        except ValueError:
            if attempt == 2:
                raise
            messages = messages + [{
                "role": "user",
                "content": "Your previous reply was not valid JSON. "
                           "Reply with ONLY the JSON object, no prose."}]
    raise RuntimeError(f"LLM reply not parseable: {raw[:300] if raw else 'empty'}")


def extract_json(raw: str) -> dict:
    """Fence-strip -> full parse -> first-{..last-} slice fallback."""
    if not raw:
        raise ValueError("empty LLM reply")
    s = raw.strip()
    if s.startswith("```"):
        s = re_sub_fences(s)
    try:
        return json.loads(s)
    except Exception:
        pass
    i, j = s.find("{"), s.rfind("}")
    if i != -1 and j > i:
        try:
            return json.loads(s[i:j + 1])
        except Exception:
            pass
    raise ValueError(f"no JSON object in reply: {raw[:200]!r}")


def re_sub_fences(s: str) -> str:
    import re
    s = re.sub(r"^```[a-zA-Z]*\n?", "", s)
    s = re.sub(r"\n?```$", "", s)
    return s.strip()


def test_connection(timeout: int = 45) -> dict:
    cfg = _effective()
    try:
        import time
        t0 = time.time()
        out = llm_json([{"role": "user", "content": "Reply with JSON: {\"ok\": true}"}],
                       timeout=timeout)
        return {"ok": out.get("ok") is True, "provider": cfg["provider"],
                "model": cfg["model"],
                "latency_ms": int((time.time() - t0) * 1000),
                "note": "engine reachable"}
    except Exception as e:
        return {"ok": False, "provider": cfg["provider"], "model": cfg["model"],
                "note": str(e)}
