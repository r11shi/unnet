"""Gemini backend, using the free tier and its structured-output mode.

``responseSchema`` is why Gemini is the primary here: the model is constrained
to emit JSON matching a schema, so the parsing failures that dominate free-form
LLM output simply do not happen. Anything it does emit still goes through the
verifier — a well-formed proposal and a correct one are different things.
"""

from __future__ import annotations

import json
import os

import httpx

ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"


class GeminiBackend:
    name = "gemini"

    def __init__(self, model: str | None = None, timeout: float = 45.0) -> None:
        self.model = model or os.environ.get("UNNET_GEMINI_MODEL", "gemini-2.0-flash")
        self.api_key = os.environ.get("GEMINI_API_KEY", "")
        self.timeout = timeout

    def complete(self, prompt: str, schema: dict) -> dict:
        if not self.api_key:
            raise RuntimeError("GEMINI_API_KEY is not set")

        payload = {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {
                # Reconciliation is not a creative task. The same books should
                # produce the same answer twice.
                "temperature": 0.0,
                "responseMimeType": "application/json",
                "responseSchema": _to_gemini_schema(schema),
            },
        }

        response = httpx.post(
            ENDPOINT.format(model=self.model),
            params={"key": self.api_key},
            json=payload,
            timeout=self.timeout,
        )
        response.raise_for_status()
        body = response.json()

        try:
            text = body["candidates"][0]["content"]["parts"][0]["text"]
        except (KeyError, IndexError) as exc:
            raise RuntimeError(f"Unexpected Gemini response shape: {body}") from exc

        return json.loads(text)


def _to_gemini_schema(schema: dict) -> dict:
    """Convert a JSON-Schema fragment to the subset Gemini accepts.

    Gemini's schema dialect uses upper-case type names and rejects the
    validation keywords (``additionalProperties``, ``$schema``) that a normal
    JSON Schema carries, so unknown keys are dropped rather than passed through.
    """
    allowed = {"type", "properties", "items", "required", "enum", "description", "nullable"}
    type_map = {
        "string": "STRING",
        "integer": "INTEGER",
        "number": "NUMBER",
        "boolean": "BOOLEAN",
        "array": "ARRAY",
        "object": "OBJECT",
    }

    converted: dict = {}
    for key, value in schema.items():
        if key not in allowed:
            continue
        if key == "type":
            converted["type"] = type_map.get(value, "STRING")
        elif key == "properties":
            converted["properties"] = {k: _to_gemini_schema(v) for k, v in value.items()}
        elif key == "items":
            converted["items"] = _to_gemini_schema(value)
        else:
            converted[key] = value
    return converted
