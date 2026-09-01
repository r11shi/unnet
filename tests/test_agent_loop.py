"""The agent loop, and whether it is real.

The easiest way to fake "agentic" is a loop that never runs. These tests force
the retry path with a stub model, prove the verifier's finding is actually fed
back, and prove the stopping rules hold. The run against real fixtures then
reports honestly how often iteration was *needed* — which on this data is a
number worth publishing either way.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from unnet.agents.triage import MAX_ATTEMPTS, TriageAgent
from unnet.core.models import ExceptionStatus
from unnet.engine.pipeline import SourcePaths, reconcile
from unnet.llm.provider import LLMResponse

FIXTURES = Path("data/synthetic")


class StubClient:
    """Returns a scripted sequence of model replies, and records the prompts."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.prompts: list[str] = []
        self.calls = 0
        self.tokens = 0
        self.limiter = type("L", (), {"per_minute": 0, "waited_seconds": 0.0})()
        self.degraded = False

    def complete(self, task, prompt, schema):
        self.prompts.append(prompt)
        self.calls += 1
        data = self.replies.pop(0) if self.replies else {"components": [], "reasoning": "done"}
        return LLMResponse(data=data, source="stub", model="stub", prompt_hash="0" * 12)

    def stats(self):
        return {"calls": self.calls, "tokens": 0, "degraded": False, "rate_limit_wait_s": 0.0}


def _exception(ctx):
    """A fresh residual for the agent to work on.

    The context is built once per module for speed, so each test resets the
    exception it borrows — otherwise the first test closes it and every later
    test silently skips, which is exactly how a loop ends up untested.
    """
    from unnet.core.models import ExceptionCode

    for e in ctx.exceptions:
        if e.code == ExceptionCode.SHORT_CREDIT:
            e.status = ExceptionStatus.OPEN
            e.proposal = None
            e.verifier_verdict = None
            e.verifier_reason = None
            e.evidence = {k: v for k, v in (e.evidence or {}).items() if k != "agent_trace"}
            return e
    pytest.skip("no short credit in fixtures")


pytestmark = pytest.mark.skipif(
    not (FIXTURES / "ground_truth.json").exists(), reason="run `make gen`"
)


@pytest.fixture(scope="module")
def ctx():
    return reconcile(SourcePaths.synthetic(FIXTURES), ai_enabled=False).ctx


def test_a_rejection_triggers_exactly_one_informed_retry(ctx):
    """The point of the loop: the second attempt knows how far out the first was."""
    target = _exception(ctx)
    amount = abs(target.residual_paise)

    stub = StubClient([
        # First reply is short by 50 paise, so the verifier rejects it.
        {"components": [{"kind": "bank_charge", "ref": "fee", "amount_paise": amount - 50}],
         "reasoning": "first guess"},
        # Second reply sums exactly.
        {"components": [{"kind": "bank_charge", "ref": "fee", "amount_paise": amount}],
         "reasoning": "corrected"},
    ])
    agent = TriageAgent(stub)
    agent._triage_one(ctx, target)

    assert stub.calls == 2, "a rejection must produce one retry"
    assert agent.retries == 1
    # The retry prompt carries the verifier's exact finding, not just "try again".
    assert "REJECTED" in stub.prompts[1]
    assert "out by" in stub.prompts[1]
    assert "-50 paise" in stub.prompts[1] or "50 paise" in stub.prompts[1]
    # It sums now, but the component is still invented — so it is a hypothesis.
    assert target.status == ExceptionStatus.AI_HYPOTHESIS


def test_the_loop_stops_at_the_attempt_limit(ctx):
    """An agent that re-rolls the dice forever is not investigating."""
    target = _exception(ctx)
    wrong = {"components": [{"kind": "bank_charge", "ref": "fee", "amount_paise": 1}],
             "reasoning": "still wrong"}
    stub = StubClient([wrong] * 10)

    agent = TriageAgent(stub)
    agent._triage_one(ctx, target)

    assert stub.calls == MAX_ATTEMPTS
    assert target.status == ExceptionStatus.AI_REJECTED


def test_a_hypothesis_is_terminal_and_never_retried(ctx):
    """Retrying a HYPOTHESIS would spend tokens to arrive at the same place."""
    target = _exception(ctx)
    amount = abs(target.residual_paise)
    stub = StubClient([
        {"components": [{"kind": "bank_charge", "ref": "fee", "amount_paise": amount}],
         "reasoning": "sums exactly, invented component"},
    ])
    agent = TriageAgent(stub)
    agent._triage_one(ctx, target)

    assert stub.calls == 1, "a correct-but-unevidenced answer is already terminal"
    assert agent.retries == 0
    assert target.status == ExceptionStatus.AI_HYPOTHESIS


def test_an_abstention_stops_immediately(ctx):
    target = _exception(ctx)
    stub = StubClient([{"components": [], "reasoning": "cannot explain this"}])
    agent = TriageAgent(stub)
    agent._triage_one(ctx, target)

    assert stub.calls == 1
    assert target.verifier_verdict == "abstained"


def test_every_step_is_recorded_in_the_trace(ctx):
    """A judge must be able to reconstruct exception -> action -> verdict."""
    target = _exception(ctx)
    amount = abs(target.residual_paise)
    stub = StubClient([
        {"components": [{"kind": "bank_charge", "ref": "f", "amount_paise": amount - 50}],
         "reasoning": "a"},
        {"components": [{"kind": "bank_charge", "ref": "f", "amount_paise": amount}],
         "reasoning": "b"},
    ])
    TriageAgent(stub)._triage_one(ctx, target)

    trace = target.evidence["agent_trace"]
    assert [t["step"] for t in trace] == [1, 2]
    assert trace[0]["verdict"].startswith("rejected")
    assert trace[0]["delta_paise"] == -50
    assert trace[1]["verdict"] == "hypothesis"
    assert all("model" in t and "components" in t for t in trace)
