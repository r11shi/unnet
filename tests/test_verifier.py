"""The verifier is the only thing standing between a model and the ledger.

These tests are adversarial on purpose: each one is a proposal that a competent
model could plausibly produce, and each one must be refused.
"""

from __future__ import annotations

import pytest

from unnet.agents.verifier import (
    Component,
    Proposal,
    Verdict,
    verify,
)

KNOWN = {
    "setl_A": 100_000,
    "setl_B": 250_000,
    "setl_C": 75_000,
}


def _proposal(components, target):
    return Proposal(
        subject_kind="bank_txn",
        subject_id="N123",
        target_paise=target,
        components=components,
        produced_by="test",
    )


def test_accepts_a_fully_evidenced_decomposition():
    result = verify(
        _proposal(
            [
                Component("settlement_batch", "setl_A", 100_000),
                Component("settlement_batch", "setl_B", 250_000),
            ],
            350_000,
        ),
        known_refs=KNOWN,
    )
    assert result.accepted
    assert result.verdict is Verdict.RESOLVED_VERIFIED


def test_rejects_a_proposal_that_is_off_by_fifty_paise():
    """The headline failure case: plausible, well-formed, and wrong.

    Fifty paise is nothing to a person reading the summary and everything to
    the books. A tolerance here would be the single change that made this
    system untrustworthy.
    """
    result = verify(
        _proposal(
            [
                Component("settlement_batch", "setl_A", 100_000),
                Component("bank_charge", "neft_fee", 1_180),
            ],
            101_230,  # 50 paise more than the components account for
        ),
        known_refs=KNOWN,
    )
    assert not result.accepted
    assert result.verdict is Verdict.REJECTED_SUM_MISMATCH
    assert result.delta_paise == -50
    assert "out by" in result.reason


def test_rejects_a_settlement_that_does_not_exist():
    result = verify(
        _proposal([Component("settlement_batch", "setl_ZZZ", 350_000)], 350_000),
        known_refs=KNOWN,
    )
    assert result.verdict is Verdict.REJECTED_UNKNOWN_COMPONENT


def test_rejects_a_restated_amount_even_when_the_total_is_right():
    """Citing a real payout with a doctored amount is the subtle one.

    The sum comes out exactly right, so an arithmetic-only check passes it. It
    is still wrong: setl_A is not 150000 paise, and accepting this would
    attribute money to a payout that never carried it.
    """
    result = verify(
        _proposal(
            [
                Component("settlement_batch", "setl_A", 150_000),  # actually 100_000
                Component("settlement_batch", "setl_C", 75_000),
            ],
            225_000,
        ),
        known_refs=KNOWN,
    )
    assert result.verdict is Verdict.REJECTED_SUM_MISMATCH
    assert "but the proposal claims" in result.reason


def test_rejects_claiming_the_same_payout_twice():
    result = verify(
        _proposal(
            [
                Component("settlement_batch", "setl_A", 100_000),
                Component("settlement_batch", "setl_A", 100_000),
            ],
            200_000,
        ),
        known_refs=KNOWN,
    )
    assert result.verdict is Verdict.REJECTED_DUPLICATE_COMPONENT


def test_rejects_a_payout_already_reconciled_elsewhere():
    result = verify(
        _proposal([Component("settlement_batch", "setl_B", 250_000)], 250_000),
        known_refs=KNOWN,
        already_matched={"setl_B"},
    )
    assert result.verdict is Verdict.REJECTED_ALREADY_MATCHED


def test_rejects_a_large_invented_adjustment():
    """Balancing the books with a big unnamed number is not an explanation."""
    result = verify(
        _proposal(
            [
                Component("settlement_batch", "setl_A", 100_000),
                Component("adjustment", "misc", 250_000),
            ],
            350_000,
        ),
        known_refs=KNOWN,
    )
    assert result.verdict is Verdict.REJECTED_IMPLAUSIBLE_ADJUSTMENT


def test_a_realistic_bank_charge_is_a_hypothesis_not_a_resolution():
    """A ₹10 NEFT charge plus GST is a good guess and not evidence.

    It sums exactly and it is probably right. It is still a number the model
    invented, present in no record, so it can never close an exception on its
    own — it goes to a human labelled as what it is.
    """
    result = verify(
        _proposal(
            [
                Component("settlement_batch", "setl_A", 100_000),
                Component("bank_charge", "neft_fee_plus_gst", -1_180),
            ],
            98_820,
        ),
        known_refs=KNOWN,
    )
    assert result.verdict is Verdict.HYPOTHESIS
    assert not result.accepted
    assert result.unevidenced == ["bank_charge:neft_fee_plus_gst"]


def test_provenance_read_back_is_required_when_a_lookup_is_supplied():
    """A citation nobody can look up is a name, not evidence."""
    result = verify(
        _proposal([Component("settlement_batch", "setl_A", 100_000)], 100_000),
        known_refs=KNOWN,
        lookup=lambda kind, ref: None,  # ledger cannot confirm it
    )
    assert result.verdict is Verdict.REJECTED_PROVENANCE_FAILED


def test_a_rival_explanation_downgrades_to_hypothesis():
    """Two combinations that both sum exactly mean the arithmetic does not say
    which one happened. Numerical agreement is not proof of explanation."""
    result = verify(
        _proposal([Component("settlement_batch", "setl_A", 100_000)], 100_000),
        known_refs=KNOWN,
        rival_explanations=1,
    )
    assert result.verdict is Verdict.HYPOTHESIS
    assert result.rival_explanations == 1


def test_rejects_an_empty_proposal():
    result = verify(_proposal([], 100), known_refs=KNOWN)
    assert result.verdict is Verdict.REJECTED_EMPTY


@pytest.mark.parametrize("kind", ["vibes", "plug", ""])
def test_rejects_an_invented_component_kind(kind):
    result = verify(
        _proposal([Component(kind, "whatever", 100)], 100),
        known_refs=KNOWN,
    )
    assert result.verdict is Verdict.REJECTED_UNKNOWN_COMPONENT
