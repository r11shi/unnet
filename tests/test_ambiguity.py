"""Two explanations that both sum exactly. Neither may be posted.

This is the property the whole design rests on: **arithmetic does not identify
which explanation actually happened.** A credit of ₹5,00,000 is satisfied just
as exactly by payout A as by payout B when both are worth ₹5,00,000, and a
system that picks one because it searched first has invented a fact.

It is tested here rather than in the fixtures because on the shipped data the
situation is structurally unreachable — by the time `subset_sum_resolve` runs
there is exactly one unmatched credit and one unclaimed payout, so
`_count_rivals` has no rival to find. That is a property of a 21-payout
fixture, not evidence the guard works, and the difference matters: an
untriggered guard and an absent guard look identical in a metrics table.

So the ambiguity is constructed against the real engine — real `ReconContext`,
real resolver, real verifier — and the refusal is asserted.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import timedelta
from pathlib import Path

import pytest

from unnet.agents.resolvers import (
    _count_rivals,
    _find_exact_combination,
    subset_sum_resolve,
)
from unnet.core.models import ExceptionCode, ExceptionStatus, SettlementBatch
from unnet.engine.pipeline import SourcePaths, reconcile

FIXTURES = Path("data/synthetic")

pytestmark = pytest.mark.skipif(
    not (FIXTURES / "ground_truth.json").exists(), reason="run `make gen`"
)


@pytest.fixture
def ctx():
    """A real reconciliation, rules only, so nothing is closed by a model."""
    return reconcile(SourcePaths.synthetic(FIXTURES), ai_enabled=False).ctx


def _twin(ctx, of: SettlementBatch) -> SettlementBatch:
    """A second payout worth exactly what `of` is worth.

    Two payouts of identical value settling the same day is not exotic — it is
    a Tuesday at any merchant with steady volume. Give the bank a credit for
    one of them with no UTR in the narration and nothing in the arithmetic can
    say which.
    """
    clone = SettlementBatch(
        run_id=of.run_id,
        settlement_id=of.settlement_id + "_TWIN",
        settlement_utr=(of.settlement_utr or "UTR") + "9",
        reported_amount_paise=of.reported_amount_paise,
        currency=of.currency,
        status=of.status,
        created_at=of.created_at,
        settled_at=of.settled_at,
    )
    ctx.batches.append(clone)
    ctx.build_indexes()
    return clone


def _open_credit(ctx):
    for exception in ctx.exceptions:
        if (
            exception.code == ExceptionCode.UNMATCHED_BANK_CREDIT
            and exception.status == ExceptionStatus.OPEN
        ):
            return exception
    pytest.skip("no unmatched credit in the fixtures")


def test_the_shipped_fixtures_cannot_produce_ambiguity(ctx):
    """Stated so the reason this file constructs its own case is on the record.

    If a future fixture change makes ambiguity reachable end to end, this test
    fails and the constructed case below should be replaced by the real one.
    """
    free = [
        b for b in ctx.batches
        if not ctx.is_claimed("settlement_batch", b.settlement_id)
    ]
    assert len(free) <= 1, (
        "more than one unclaimed payout now survives to the resolver; "
        "ambiguity may be reachable from the fixtures directly"
    )


def test_two_payouts_of_equal_value_are_not_guessed_between(ctx):
    """The core refusal, through the real resolver."""
    exception = _open_credit(ctx)
    txn = next(t for t in ctx.bank_txns if t.bank_ref == exception.subject_id)

    # A payout worth exactly what the bank credited, and then a twin of it.
    original = SettlementBatch(
        run_id=ctx.run_id,
        settlement_id="setl_AMBIG_A",
        settlement_utr="UTRAMBIG0001",
        reported_amount_paise=txn.credit_paise,
        currency="INR",
        status="processed",
        settled_at=txn.value_date,
        created_at=txn.value_date - timedelta(hours=3),
    )
    ctx.batches.append(original)
    ctx.build_indexes()
    twin = _twin(ctx, original)

    candidates = [
        b for b in ctx.batches
        if not ctx.is_claimed("settlement_batch", b.settlement_id)
    ]
    chosen = _find_exact_combination(txn.credit_paise, candidates)
    assert chosen, "the arithmetic does fit — that is precisely the trap"
    assert _count_rivals(txn.credit_paise, candidates, chosen) >= 1, (
        "a second exact explanation must be detected"
    )

    closed = subset_sum_resolve(ctx)

    assert closed == 0, "an ambiguous credit must not be auto-closed"
    assert exception.status is not ExceptionStatus.AUTO_RESOLVED
    # Neither payout may be consumed: claiming one silently asserts it was the
    # right one, and the next run would then reconcile against a fiction.
    assert not ctx.is_claimed("settlement_batch", original.settlement_id)
    assert not ctx.is_claimed("settlement_batch", twin.settlement_id)


def test_an_ambiguous_credit_stays_open_for_a_human(ctx):
    """Refusing is only half of it; the work still has to reach someone."""
    exception = _open_credit(ctx)
    txn = next(t for t in ctx.bank_txns if t.bank_ref == exception.subject_id)

    for suffix in ("A", "B"):
        ctx.batches.append(
            SettlementBatch(
                run_id=ctx.run_id,
                settlement_id=f"setl_AMBIG_{suffix}",
                settlement_utr=f"UTRAMBIG000{suffix}",
                reported_amount_paise=txn.credit_paise,
                currency="INR",
                status="processed",
                settled_at=txn.value_date,
                created_at=txn.value_date - timedelta(hours=3),
            )
        )
    ctx.build_indexes()

    subset_sum_resolve(ctx)

    assert exception.status == ExceptionStatus.OPEN
    from unnet.engine.casefile import ROUTES

    route = ROUTES[ExceptionCode.UNMATCHED_BANK_CREDIT]
    assert route.owner, "an unresolved ambiguous credit must have an owner"


def test_a_single_explanation_is_still_closed(ctx):
    """The guard must not be a blanket refusal.

    A resolver that never closes anything would pass every test above while
    being useless. One exact explanation, no rival, is exactly the case the
    deterministic resolver exists to settle.
    """
    exception = _open_credit(ctx)
    txn = next(t for t in ctx.bank_txns if t.bank_ref == exception.subject_id)

    ctx.batches.append(
        SettlementBatch(
            run_id=ctx.run_id,
            settlement_id="setl_UNIQUE_ONLY",
            settlement_utr="UTRUNIQUE001",
            reported_amount_paise=txn.credit_paise,
            currency="INR",
            status="processed",
            settled_at=txn.value_date,
            created_at=txn.value_date - timedelta(hours=3),
        )
    )
    ctx.build_indexes()

    closed = subset_sum_resolve(ctx)

    assert closed == 1, "an unambiguous exact match should still settle"
    assert exception.status == ExceptionStatus.AUTO_RESOLVED
    assert exception.verifier_verdict == "resolved_verified"
