"""Groq backend — the fallback when Gemini's free quota is spent.

Groq's OpenAI-compatible API supports ``response_format: json_object``, which
guarantees valid JSON but not a particular shape, so the schema is restated in
the prompt. Shape errors surface as a missing key downstream and are treated as
a failed proposal, which the verifier already knows how to reject.
"""

from __future__ import annotations

import json
import os

import httpx

ENDPOINT = "https://api.groq.com/openai/v1/chat/completions"


class GroqBackend:
    name = "groq"

    def __init__(self, model: str | None = None, timeout: float = 45.0) -> None:
        self.model = model or os.environ.get("UNNET_GROQ_MODEL", "llama-3.3-70b-versatile")
        self.api_key = os.environ.get("GROQ_API_KEY", "")
        self.timeout = timeout

    def complete(self, prompt: str, schema: dict) -> dict:
        if not self.api_key:
            raise RuntimeError("GROQ_API_KEY is not set")

        payload = {
            "model": self.model,
            "temperature": 0.0,
            "response_format": {"type": "json_object"},
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You are a reconciliation assistant. Reply with JSON only, "
                        "matching this schema exactly:\n"
                        f"{json.dumps(schema)}"
                    ),
                },
                {"role": "user", "content": prompt},
            ],
        }

        response = httpx.post(
            ENDPOINT,
            headers={"Authorization": f"Bearer {self.api_key}"},
            json=payload,
            timeout=self.timeout,
        )
        response.raise_for_status()
        body = response.json()

        try:
            text = body["choices"][0]["message"]["content"]
        except (KeyError, IndexError) as exc:
            raise RuntimeError(f"Unexpected Groq response shape: {body}") from exc

        return json.loads(text)
