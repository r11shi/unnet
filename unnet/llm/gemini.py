"""Gemini backend, using the free tier and its structured-output mode.

``responseSchema`` is why Gemini is the primary here: the model is constrained
to emit JSON matching a schema, so the parsing failures that dominate free-form
LLM output simply do not happen. Anything it does emit still goes through the
verifier — a well-formed proposal and a correct one are different things.
"""

from __future__ import annotations

import json
import os
import time

import httpx

ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
LIST_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models"

#: Google retires models for *new* keys while still listing them, so a name that
#: works on one account 404s on another with a message naming its replacement.
#: Verified working on a fresh free-tier key in Sept 2026.
DEFAULT_MODEL = "gemini-3.6-flash"

#: Attempts per model, and the pause before each retry.
RETRY_ATTEMPTS = 3
RETRY_BACKOFF_S = (0.8, 2.0, 4.0)

#: Tried in order when the primary is overloaded. Free-tier capacity moves
#: around between models by the hour — "this model is currently experiencing
#: high demand" is a property of the model, not of the key — so a single name
#: makes a live demo a coin flip. All three are stable (non-preview) and answer
#: the same structured-output contract, so falling through changes which model
#: replies, not what the reply has to look like.
FALLBACK_MODELS = ("gemini-3.5-flash", "gemini-2.5-flash")

#: Statuses worth retrying, and worth trying another model for. Everything else
#: is a bad request and retrying it only spends quota.
TRANSIENT_STATUS = frozenset({429, 500, 502, 503, 504})


class GeminiBackend:
    name = "gemini"

    def __init__(self, model: str | None = None, timeout: float = 45.0) -> None:
        self.model = model or os.environ.get("UNNET_GEMINI_MODEL", DEFAULT_MODEL)
        self.api_key = os.environ.get("GEMINI_API_KEY", "")
        self.timeout = timeout
        self.last_tokens = 0
        #: Which model actually answered. The trace should name the model that
        #: produced the output, not the one that was asked first.
        self.last_model = self.model

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

        candidates = [self.model] + [m for m in FALLBACK_MODELS if m != self.model]
        response = None
        for index, model in enumerate(candidates):
            response = self._post_with_retry(model, payload)
            self.last_model = model
            if response.status_code == 404:
                raise RuntimeError(self._retirement_hint(response))
            if response.status_code < 400 or index == len(candidates) - 1:
                break
            # Only capacity failures are worth asking a different model about.
            # A 400 means the request is wrong and every model will say so.
            if response.status_code not in TRANSIENT_STATUS:
                break

        response.raise_for_status()
        body = response.json()

        try:
            text = body["candidates"][0]["content"]["parts"][0]["text"]
        except (KeyError, IndexError) as exc:
            raise RuntimeError(f"Unexpected Gemini response shape: {body}") from exc

        usage = body.get("usageMetadata") or {}
        self.last_tokens = int(usage.get("totalTokenCount") or 0)
        return json.loads(text)

    def _post_with_retry(self, model: str, payload: dict):
        """POST, retrying only the failures that are the server's fault.

        The free tier answers 503 "model is overloaded" under load, and 429
        when the per-minute quota bites. Both are transient and both are
        common enough that a single attempt makes a live demo a coin flip.
        A 400 or a 404 is a bug in the request and retrying it just wastes
        the quota, so those return immediately.

        Bounded on purpose: three attempts and roughly five seconds of
        backoff. Past that the circuit breaker upstream should take over and
        the run should finish on rules, which is the whole design.
        """
        last = None
        for attempt in range(RETRY_ATTEMPTS):
            try:
                last = httpx.post(
                    ENDPOINT.format(model=model),
                    params={"key": self.api_key},
                    json=payload,
                    timeout=self.timeout,
                )
            except httpx.TimeoutException:
                if attempt == RETRY_ATTEMPTS - 1:
                    raise
                time.sleep(RETRY_BACKOFF_S[attempt])
                continue

            if last.status_code not in TRANSIENT_STATUS or attempt == RETRY_ATTEMPTS - 1:
                return last
            time.sleep(RETRY_BACKOFF_S[attempt])
        return last

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
