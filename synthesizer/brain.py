"""The writer.

A ladder of language-model providers. It never raises: each rung reports its
own failure and the next rung is tried. The final rung is keyless and anonymous
(Pollinations), so the engine can always produce prose and structured JSON even
with no API key at all.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

import httpx

from .config import SETTINGS


@dataclass
class Completion:
    ok: bool
    text: str = ""
    provider: str = ""
    reasons: list[str] = field(default_factory=list)


def _extract_json(text: str) -> dict[str, Any] | None:
    """Pull the first JSON object out of a model reply."""
    if not text:
        return None
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    candidate = fenced.group(1) if fenced else None
    if candidate is None:
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            candidate = text[start : end + 1]
    if not candidate:
        return None
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        return None


class Brain:
    """Provider ladder: Gemini (if key) -> Groq (if key) -> Pollinations (keyless)."""

    def __init__(self, timeout: int | None = None) -> None:
        self.timeout = timeout or SETTINGS.llm_timeout_seconds

    # -- rungs ---------------------------------------------------------------

    def _gemini(self, system: str, user: str) -> Completion:
        if not SETTINGS.gemini_key:
            return Completion(False, provider="gemini", reasons=["no_key"])
        url = (
            "https://generativelanguage.googleapis.com/v1beta/models/"
            f"gemini-1.5-flash:generateContent?key={SETTINGS.gemini_key}"
        )
        body = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": {"temperature": 0.2},
        }
        try:
            r = httpx.post(url, json=body, timeout=self.timeout)
            if r.status_code != 200:
                return Completion(False, provider="gemini", reasons=[f"http_{r.status_code}"])
            data = r.json()
            text = data["candidates"][0]["content"]["parts"][0]["text"]
            return Completion(True, text=text, provider="gemini")
        except Exception as exc:  # noqa: BLE001 - never raise from a rung
            return Completion(False, provider="gemini", reasons=[type(exc).__name__])

    def _groq(self, system: str, user: str) -> Completion:
        if not SETTINGS.groq_key:
            return Completion(False, provider="groq", reasons=["no_key"])
        url = "https://api.groq.com/openai/v1/chat/completions"
        body = {
            "model": "llama-3.3-70b-versatile",
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0.2,
        }
        try:
            r = httpx.post(
                url,
                json=body,
                headers={"Authorization": f"Bearer {SETTINGS.groq_key}"},
                timeout=self.timeout,
            )
            if r.status_code != 200:
                return Completion(False, provider="groq", reasons=[f"http_{r.status_code}"])
            text = r.json()["choices"][0]["message"]["content"]
            return Completion(True, text=text, provider="groq")
        except Exception as exc:  # noqa: BLE001
            return Completion(False, provider="groq", reasons=[type(exc).__name__])

    def _pollinations(self, system: str, user: str) -> Completion:
        """Keyless, anonymous floor. A ladder of models covers per-model throttles."""
        body_base = {
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0.2,
        }
        reasons: list[str] = []
        for model in ("openai", "openai-fast"):
            body = dict(body_base, model=model)
            try:
                r = httpx.post(
                    SETTINGS.pollinations_url,
                    json=body,
                    headers={"Content-Type": "application/json"},
                    timeout=self.timeout,
                )
                if r.status_code != 200:
                    reasons.append(f"{model}:http_{r.status_code}")
                    continue
                data = r.json()
                text = data["choices"][0]["message"]["content"]
                if text and text.strip():
                    return Completion(True, text=text, provider=f"pollinations:{model}")
                reasons.append(f"{model}:empty")
            except Exception as exc:  # noqa: BLE001
                reasons.append(f"{model}:{type(exc).__name__}")
        return Completion(False, provider="pollinations", reasons=reasons)

    # -- public API ----------------------------------------------------------

    def complete(self, system: str, user: str) -> Completion:
        reasons: list[str] = []
        for rung in (self._gemini, self._groq, self._pollinations):
            result = rung(system, user)
            if result.ok:
                result.reasons = reasons + result.reasons
                return result
            reasons.extend(f"{result.provider}:{r}" for r in result.reasons)
        return Completion(False, reasons=reasons)

    def complete_json(self, system: str, user: str) -> dict[str, Any] | None:
        result = self.complete(system, user)
        if not result.ok:
            return None
        return _extract_json(result.text)
