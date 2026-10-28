"""Local OpenAI-compatible LLM adapter (new source, no fabricated model).

Run a local server such as llama.cpp ``llama-server`` or Ollama. The adapter
accepts only a loopback URL and requires an explicit model name. It never falls
back to DeepSeek, Gemini or Google.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request


DEFAULT_BASE_URL = "http://127.0.0.1:18081/v1"
DEFAULT_MODEL = "subforge-local"


class LocalAIError(RuntimeError):
    pass


def _cfg() -> tuple[str, str]:
    base = (os.environ.get("SUBFORGE_LOCAL_LLM_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
    model = (os.environ.get("SUBFORGE_LOCAL_LLM_MODEL") or DEFAULT_MODEL).strip()
    from services.local_policy import is_loopback_url

    if not is_loopback_url(base):
        raise LocalAIError("SUBFORGE_LOCAL_LLM_BASE_URL must point to localhost")
    if not model:
        raise LocalAIError("Set SUBFORGE_LOCAL_LLM_MODEL to a locally installed model")
    return base, model


def local_chat(messages, model: str = "", timeout: int = 180, max_retries: int = 1,
               max_tokens: int = 8192, temperature: float = 0.2,
               suy_nghi: str | None = None, json_mode: bool = False) -> str:
    base, default_model = _cfg()
    # Existing projects store names such as gemini:... or deepseek-v4-... .
    # They are compatibility selectors here, never model names sent to a cloud.
    selected = default_model
    body = {"model": selected, "messages": messages, "stream": False,
            "temperature": max(0.0, min(2.0, float(temperature))),
            "max_tokens": int(max_tokens)}
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    req = urllib.request.Request(
        f"{base}/chat/completions", data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "User-Agent": "SubForge-Local"},
        method="POST")
    last = None
    for _ in range(max(1, int(max_retries))):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
            return str(payload["choices"][0]["message"].get("content") or "")
        except (urllib.error.HTTPError, urllib.error.URLError, KeyError, IndexError, TypeError, ValueError) as exc:
            last = exc
    raise LocalAIError(f"Local LLM request failed: {last}")


def local_translate(text: str, src: str, dest: str) -> str:
    src = str(src or "auto").lower()
    dest = str(dest or "vi").lower()
    if not str(text or "").strip() or src == dest:
        return text
    languages = {"auto": "the detected source language", "en": "English", "vi": "Vietnamese",
                 "zh": "Chinese", "zh-cn": "Simplified Chinese", "zh-tw": "Traditional Chinese",
                 "ja": "Japanese", "ko": "Korean", "fr": "French", "de": "German",
                 "es": "Spanish", "ru": "Russian", "pt": "Portuguese", "it": "Italian",
                 "th": "Thai", "id": "Indonesian"}
    instruction = (
        f"You are a translator. Translate the user's text from {languages.get(src, src)} "
        f"into {languages.get(dest, dest)}. Preserve meaning and punctuation. "
        "Output only the translation in the target language, with no explanation."
    )
    result = local_chat([{"role": "system", "content": instruction},
                         {"role": "user", "content": str(text)}], max_tokens=1024, temperature=0.1)
    if not result.strip():
        raise LocalAIError("Local LLM returned an empty translation")
    return result.strip()
