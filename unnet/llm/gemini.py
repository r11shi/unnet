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
LIST_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models"

#: Google retires models for *new* keys while still listing them, so a name that
#: works on one account 404s on another with a message naming its replacement.
#: Verified working on a fresh free-tier key in Sept 2026.
DEFAULT_MODEL = "gemini-3.6-flash"


class GeminiBackend:
    name = "gemini"

    def __init__(self, model: str | None = None, timeout: float = 45.0) -> None:
        self.model = model or os.environ.get("UNNET_GEMINI_MODEL", DEFAULT_MODEL)
        self.api_key = os.environ.get("GEMINI_API_KEY", "")
        self.timeout = timeout
        self.last_tokens = 0

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
        if response.status_code == 404:
            raise RuntimeError(self._retirement_hint(response))
        response.raise_for_status()
        body = response.json()

        try:
            text = body["candidates"][0]["content"]["parts"][0]["text"]
        except (KeyError, IndexError) as exc:
            raise RuntimeError(f"Unexpected Gemini response shape: {body}") from exc

        usage = body.get("usageMetadata") or {}
        self.last_tokens = int(usage.get("totalTokenCount") or 0)
        return json.loads(text)

    def _retirement_hint(self, response) -> str:
        """Turn Google's model-retirement 404 into something actionable.

        Google keeps retired models in the ``models`` listing but refuses them
        for keys created after the cutoff, so the failure looks like a typo
        rather than a retirement. The error body names the replacement; surface
        it instead of a bare 404.
        """
        try:
            message = response.json()["error"]["message"]
        except Exception:  # noqa: BLE001 - fall back to the raw body
            message = response.text[:300]
        return (
            f"Gemini rejected model '{self.model}': {message} "
            f"Set UNNET_GEMINI_MODEL to a model your key can use "
            f"(default here is {DEFAULT_MODEL})."
        )


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
