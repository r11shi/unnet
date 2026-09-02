"""A model's reply is untrusted *structure*, not only untrusted text.

Structured-output mode makes the right shape likely, not guaranteed, and the
local OpenAI-compatible path parses whatever JSON comes back. A bare array here
used to take the whole reconciliation down with an AttributeError — a model
returning the wrong shape must never be able to abort a run, and must certainly
never be able to close a financial exception.

Every case below asserts the same thing: whatever the model said, nothing was
auto-closed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from unnet.agents.triage import TriageAgent
from unnet.core.models import ExceptionCode, ExceptionStatus
from unnet.engine.pipeline import SourcePaths, reconcile
from unnet.llm.provider import LLMResponse, LLMUnavailable

FIXTURES = Path("data/synthetic")

pytestmark = pytest.mark.skipif(
    not (FIXTURES / "ground_truth.json").exists(), reason="run `make gen`"
)

CLOSED = {ExceptionStatus.AUTO_RESOLVED, ExceptionStatus.AI_RESOLVED}


@pytest.fixture(scope="module")
def ctx():
    return reconcile(SourcePaths.synthetic(FIXTURES), ai_enabled=False).ctx


def _fresh(ctx):
    for exception in ctx.exceptions:
        if exception.code == ExceptionCode.SHORT_CREDIT:
            exception.status = ExceptionStatus.OPEN
            exception.proposal = None
            exception.verifier_verdict = None
            exception.evidence = {
                k: v for k, v in (exception.evidence or {}).items() if k != "agent_trace"
            }
            return exception
    pytest.skip("no short credit in fixtures")


class _Replies:
    def __init__(self, payload):
        self.payload = payload
        self.calls = 0
        self.limiter = type("L", (), {"per_minute": 0, "waited_seconds": 0.0})()
        self.degraded = False

    def complete(self, task, prompt, schema):
        self.calls += 1
        if isinstance(self.payload, Exception):
            raise self.payload
        return LLMResponse(data=self.payload, source="t", model="t", prompt_hash="0" * 12)

    def stats(self):
        return {"calls": self.calls, "tokens": 0, "degraded": False,
                "rate_limit_wait_s": 0.0}


MALFORMED = {
    "a bare array instead of an object": [1, 2, 3],
    "an empty object": {},
    "components as a string": {"components": "everything is fine", "reasoning": "x"},
    "components as an object": {"components": {"kind": "bank_charge"}, "reasoning": "x"},
    "a component that is not an object": {"components": ["just a string"], "reasoning": "x"},
    "a component with no amount": {
        "components": [{"kind": "bank_charge", "ref": "fee"}], "reasoning": "x",
    },
    "an amount that is prose": {
        "components": [{"kind": "bank_charge", "ref": "fee",
                        "amount_paise": "about eleven rupees"}], "reasoning": "x",
    },
    "an amount that is null": {
        "components": [{"kind": "bank_charge", "ref": "fee", "amount_paise": None}],
        "reasoning": "x",
    },
    "a negative amount": {
        "components": [{"kind": "bank_charge", "ref": "fee", "amount_paise": -1180}],
        "reasoning": "x",
    },
    "an invented settlement id": {
        "components": [{"kind": "settlement_batch", "ref": "setl_DOESNOTEXIST",
                        "amount_paise": 590}], "reasoning": "x",
    },
    "an unknown component kind": {
        "components": [{"kind": "vibes", "ref": "fee", "amount_paise": 590}],
        "reasoning": "x",
    },
    "a confidence that is prose": {
        "components": [{"kind": "bank_charge", "ref": "fee", "amount_paise": 590}],
        "reasoning": "x", "confidence": "very high",
    },
}


@pytest.mark.parametrize("name,payload", sorted(MALFORMED.items()))
def test_a_malformed_reply_never_closes_an_exception(ctx, name, payload):
    exception = _fresh(ctx)

    TriageAgent(_Replies(payload))._triage_one(ctx, exception)

    assert exception.status not in CLOSED, f"{name} closed a financial exception"
    # And it reached a recorded terminal state rather than falling through.
    assert exception.verifier_verdict, f"{name} left no verdict behind"


def test_a_provider_failure_is_not_a_resolution(ctx):
    """The one that matters most: an outage must not look like an answer."""
    exception = _fresh(ctx)

    TriageAgent(_Replies(LLMUnavailable("503 Service Unavailable")))._triage_one(
        ctx, exception
    )

    assert exception.status == ExceptionStatus.OPEN
    assert exception.verifier_verdict == "not_attempted"


def test_a_malformed_reply_does_not_abort_the_run(ctx):
    """The original bug: an AttributeError halfway through reconciliation."""
    exception = _fresh(ctx)
    agent = TriageAgent(_Replies([1, 2, 3]))

    agent._triage_one(ctx, exception)  # must not raise

    assert exception.verifier_verdict == "abstained"
