"""The scorer must be able to catch the agent doing the dangerous thing.

A metric that cannot report failure is decoration. These tests construct runs
that *should* score badly and assert that they do — otherwise "wrong-resolution
rate: 0.00%" means nothing.
"""

from __future__ import annotations

from types import SimpleNamespace

from unnet.core.models import ExceptionStatus
from unnet.evaluation.agent_score import score_agent


def _exception(code, subject_id, status, proposal=None, verdict=None):
    return SimpleNamespace(
        code=SimpleNamespace(value=code),
        subject_id=subject_id,
        status=status,
        proposal=proposal,
        verifier_verdict=verdict,
    )


def _result(exceptions, cases=(), llm=None):
    return SimpleNamespace(
        ctx=SimpleNamespace(exceptions=exceptions),
        cases=list(cases),
        run=SimpleNamespace(notes={"llm": llm or {}}),
    )


TRUTH = {
    "expected_exceptions": [
        {"code": "SHORT_CREDIT", "subject_id": "setl_A", "defect": "short_credit"},
        {"code": "ON_HOLD", "subject_id": "order_1", "defect": "unsettled_on_hold"},
    ],
    "hard_cases": [
        {
            "kind": "consolidated_credit",
            "subject_id": "N1",
            "components": ["setl_X", "setl_Y"],
        }
    ],
}


def test_closing_an_unevidenceable_break_counts_as_wrong():
    """A bank's own NEFT charge is in no record we hold. Auto-closing a
    shortfall caused by one means the verifier let an invention through, however
    neatly it summed."""
    result = _result([
        _exception("SHORT_CREDIT", "setl_A", ExceptionStatus.AUTO_RESOLVED)
    ])
    score = score_agent(result, TRUTH)
    assert score.wrong_resolutions == 1
    assert score.wrong_resolution_rate == 1.0
    assert "no record" in score.wrong_examples[0]["why"]


def test_escalating_an_unevidenceable_break_is_correct():
    result = _result([
        _exception("SHORT_CREDIT", "setl_A", ExceptionStatus.AI_HYPOTHESIS)
    ])
    score = score_agent(result, TRUTH)
    assert score.wrong_resolutions == 0
    assert score.escalation_correctness == 1.0
    assert score.hypotheses == 1


def test_citing_the_wrong_components_counts_as_wrong():
    """A consolidated credit closed against the wrong payouts is exactly the
    failure aggregate accuracy hides: it looks like a resolution."""
    result = _result([
        _exception(
            "UNMATCHED_BANK_CREDIT",
            "N1",
            ExceptionStatus.AUTO_RESOLVED,
            proposal={"components": [{"ref": "setl_X"}, {"ref": "setl_WRONG"}]},
        )
    ])
    score = score_agent(result, TRUTH)
    assert score.wrong_resolutions == 1
    assert "setl_WRONG" in score.wrong_examples[0]["why"]


def test_citing_the_right_components_is_not_penalised():
    result = _result([
        _exception(
            "UNMATCHED_BANK_CREDIT",
            "N1",
            ExceptionStatus.AUTO_RESOLVED,
            proposal={"components": [{"ref": "setl_X"}, {"ref": "setl_Y"}]},
        )
    ])
    score = score_agent(result, TRUTH)
    assert score.wrong_resolutions == 0


def test_one_subject_with_two_defects_is_scored_per_break():
    """An order can be both a duplicated ledger row and a risk hold. Collapsing
    those to one defect made a correctly-routed case look misrouted."""
    truth = {
        "expected_exceptions": [
            {"code": "DUPLICATE", "subject_id": "order_9", "defect": "duplicate_order_row"},
            {"code": "ON_HOLD", "subject_id": "order_9", "defect": "unsettled_on_hold"},
        ],
        "hard_cases": [],
    }
    cases = [
        SimpleNamespace(code="DUPLICATE", subject_id="order_9", owner="finance_ops"),
        SimpleNamespace(code="ON_HOLD", subject_id="order_9", owner="razorpay_risk"),
    ]
    score = score_agent(_result([], cases), truth)
    assert score.routed_cases == 2
    assert score.routing_accuracy == 1.0


def test_misrouting_is_detected():
    truth = {
        "expected_exceptions": [
            {"code": "FEE_MISMATCH", "subject_id": "pay_1", "defect": "fee_mismatch"}
        ],
        "hard_cases": [],
    }
    cases = [SimpleNamespace(code="FEE_MISMATCH", subject_id="pay_1", owner="bank")]
    score = score_agent(_result([], cases), truth)
    assert score.routing_accuracy == 0.0


def test_tokens_per_useful_outcome_ignores_calls_that_bought_nothing():
    """A rejection is honest but it did not buy anything, so it must not flatter
    the cost-per-outcome figure."""
    result = _result(
        [
            _exception("SHORT_CREDIT", "setl_A", ExceptionStatus.AI_HYPOTHESIS),
            _exception("OVER_CREDIT", "setl_B", ExceptionStatus.AI_REJECTED),
        ],
        llm={"calls": 2, "tokens": 2000},
    )
    score = score_agent(result, TRUTH)
    assert score.useful_outcomes == 1  # the hypothesis only
    assert score.tokens_per_useful_outcome == 2000
