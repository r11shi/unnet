"""Local model backend — llama.cpp, Ollama, LM Studio, vLLM.

All of them expose the same OpenAI-compatible ``/v1/chat/completions``, so one
backend covers the lot and no API key is involved. Point ``UNNET_LOCAL_BASE_URL``
at whichever is running:

    llama.cpp   llama-server -m model.gguf --port 8080   -> http://localhost:8080/v1
    Ollama      ollama serve                             -> http://localhost:11434/v1
    LM Studio   (start its local server)                 -> http://localhost:1234/v1

Swapping to a hosted API later is a change of ``UNNET_LLM_PROVIDER``, not a
change of code: the agents talk to :class:`LLMClient` and never to a backend
directly, and every proposal goes through the same verifier regardless of which
model produced it. A small local model that occasionally proposes nonsense is
therefore safe here in a way it would not be in a pipeline that trusted output.
"""

from __future__ import annotations

import json
import os
import re

import httpx


class LocalBackend:
    name = "local"

    def __init__(
        self,
        base_url: str | None = None,
        model: str | None = None,
        timeout: float = 180.0,
    ) -> None:
        self.base_url = (
            base_url or os.environ.get("UNNET_LOCAL_BASE_URL", "http://localhost:8080/v1")
        ).rstrip("/")
        # llama-server ignores the model name and serves whatever is loaded;
        # Ollama and LM Studio need a real one.
        self.model = model or os.environ.get("UNNET_LOCAL_MODEL", "local-model")
        self.timeout = timeout
        self.last_tokens = 0

    def complete(self, prompt: str, schema: dict) -> dict:
        payload = {
            "model": self.model,
            "temperature": 0.0,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You are a reconciliation assistant. Reply with a single JSON "
                        "object and nothing else — no prose, no markdown fences. It "
                        "must match this schema:\n"
                        f"{json.dumps(schema)}"
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            # Honoured by llama.cpp, Ollama and LM Studio; harmless if ignored,
            # since _extract_json below copes with a model that adds prose anyway.
            "response_format": {"type": "json_object"},
        }

        response = httpx.post(
            f"{self.base_url}/chat/completions",
            json=payload,
            timeout=self.timeout,
        )
        response.raise_for_status()
        body = response.json()

        try:
            text = body["choices"][0]["message"]["content"]
        except (KeyError, IndexError) as exc:
            raise RuntimeError(f"Unexpected local-server response shape: {body}") from exc

        return _extract_json(text)


def _extract_json(text: str) -> dict:
    """Pull a JSON object out of whatever a local model actually returned.

    Small models wrap JSON in markdown fences or prefix it with a sentence far
    more often than hosted ones do. Recovering from that here keeps a fixable
    formatting quirk from being reported as a model failure — and if there is
    genuinely no object in the reply, this raises and the caller degrades.
    """
    text = (text or "").strip()

    fenced = re.search(r"```(?:json)?\s*(.+?)```", text, re.DOTALL)
    if fenced:
        text = fenced.group(1).strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Fall back to the outermost balanced object in the reply.
    start = text.find("{")
    if start != -1:
        depth = 0
        in_string = False
        escaped = False
        for index in range(start, len(text)):
            char = text[index]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    return json.loads(text[start : index + 1])

    raise RuntimeError(f"No JSON object in local model reply: {text[:200]!r}")
