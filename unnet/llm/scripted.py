"""A model that says exactly what you tell it to.

The agent loop has a retry path: a rejected proposal comes back with the
verifier's signed delta and the model gets one more attempt. On the production
fixtures that path never fires, because deterministic candidate generation runs
first and the model's single attempt is usually right. That is the honest
result and it is published as such — but "we have a loop that never runs" is
also how every fake agent is built, so the path has to be demonstrable on
demand.

This client scripts the replies. It is the mechanism behind both the loop tests
and `unnet agent --demo`, and it is deliberately in the package rather than in
the test tree so that the demonstration a reviewer runs is the same code the
tests exercise.
"""

from __future__ import annotations

from typing import Any

from unnet.llm.provider import LLMResponse


class ScriptedClient:
    """Returns a fixed sequence of replies and records the prompts it was sent.

    Recording the prompts is the point: what proves the loop is a loop is that
    the second prompt contains the verifier's finding from the first.
    """

    def __init__(self, replies: list[dict[str, Any]], *, model: str = "scripted") -> None:
        self.replies = list(replies)
        self.prompts: list[str] = []
        self.calls = 0
        self.tokens = 0
        self.model = model
        self.limiter = type("L", (), {"per_minute": 0, "waited_seconds": 0.0})()
        self.degraded = False

    def complete(self, task: str, prompt: str, schema: dict) -> LLMResponse:
        self.prompts.append(prompt)
        self.calls += 1
        data = self.replies.pop(0) if self.replies else {"components": [], "reasoning": "done"}
        return LLMResponse(data=data, source="scripted", model=self.model,
                           prompt_hash="0" * 12)

    def stats(self) -> dict:
        return {"calls": self.calls, "tokens": 0, "degraded": False,
                "rate_limit_wait_s": 0.0}
