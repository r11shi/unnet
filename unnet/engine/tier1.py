"""Tier 1 — bank credit <-> settlement batch.

This is the outermost link: "Razorpay says it paid me; did the money arrive?"

The rules run strictest-first and each one only sees what earlier rules left
behind. The fuzzy rule deliberately refuses to guess when more than one
candidate fits: in reconciliation an open exception costs an analyst five
minutes, and a wrong match costs a restated ledger.
"""

from __future__ import annotations

from unnet.core.models import ExceptionCode, ExceptionStatus, MatchTier
from unnet.core.money import format_inr
from unnet.engine.context import ReconContext, days_between

#: How far apart a payout and its bank credit may sit and still be the same event.
CREDIT_WINDOW_DAYS = 3.0
#: Largest gap the fuzzy rule will bridge, in paise. Above this it is not a
#: bank charge or a rounding drift, it is a different transaction.
FUZZY_AMOUNT_TOLERANCE_PAISE = 5_000  # ₹50
#: A payout still inside this window has not failed to arrive — it is in flight.
IN_FLIGHT_WINDOW_DAYS = 2.0
#: Sub-rupee drift is a rounding artefact, not a deduction. Calling a one-paisa
#: difference a "short credit" sends an analyst hunting for a missing payment
#: that was never missing.
ROUNDING_TOLERANCE_PAISE = 5


def run(ctx: ReconContext) -> None:
    _match_by_utr(ctx)
    _match_by_exact_amount(ctx)
    _match_by_near_amount(ctx)
    _check_residuals(ctx)
    _flag_unmatched(ctx)


def _credits(ctx: ReconContext):
    """Bank rows that could plausibly be a payout landing."""
    return [t for t in ctx.bank_txns if t.credit_paise > 0]


def _match_by_utr(ctx: ReconContext) -> None:
    """The only exact link that exists between the two systems."""
    for txn in _credits(ctx):
        if not txn.extracted_utr or ctx.is_claimed("bank_txn", txn.bank_ref):
            continue

        candidates = [
            b
            for b in ctx.batch_by_utr.get(txn.extracted_utr, [])
            if not ctx.is_claimed("settlement_batch", b.settlement_id)
        ]
        if len(candidates) != 1:
            continue

        batch = candidates[0]
        ctx.record_match(
            tier=MatchTier.TIER1_BANK_TO_BATCH,
            rule_id="T1.UTR_EXACT",
            left_kind="bank_txn",
            left_id=txn.bank_ref,
            right_kind="settlement_batch",
            right_id=batch.settlement_id,
            amount_paise=txn.credit_paise,
            confidence=1000,
            evidence={
                "utr": txn.extracted_utr,
                "utr_source": txn.utr_source,
                "bank_credit_paise": txn.credit_paise,
                "batch_amount_paise": batch.reported_amount_paise,
                "delta_paise": txn.credit_paise - batch.reported_amount_paise,
                "narration": txn.narration,
            },
        )


def _match_by_exact_amount(ctx: ReconContext) -> None:
    """No UTR, but the rupee figure and the date line up exactly and uniquely."""
    for txn in _credits(ctx):
        if ctx.is_claimed("bank_txn", txn.bank_ref):
            continue

        candidates = [
            b
            for b in ctx.batches
            if not ctx.is_claimed("settlement_batch", b.settlement_id)
            and b.reported_amount_paise == txn.credit_paise
            and _within_window(txn, b, CREDIT_WINDOW_DAYS)
        ]
        if len(candidates) != 1:
            continue

        batch = candidates[0]
        ctx.record_match(
            tier=MatchTier.TIER1_BANK_TO_BATCH,
            rule_id="T1.AMOUNT_DATE_EXACT",
            left_kind="bank_txn",
            left_id=txn.bank_ref,
            right_kind="settlement_batch",
            right_id=batch.settlement_id,
            amount_paise=txn.credit_paise,
            confidence=900,
            evidence={
                "reason": "unique batch with identical amount inside the date window",
                "bank_credit_paise": txn.credit_paise,
                "batch_amount_paise": batch.reported_amount_paise,
                "delta_paise": 0,
                "days_apart": days_between(txn.value_date, batch.settled_at),
                "narration": txn.narration,
            },
        )


def _match_by_near_amount(ctx: ReconContext) -> None:
    """The short-credit case: right payout, wrong amount.

    A bank that deducts its own NEFT charge breaks exact matching. We accept the
    link only when a single batch is close enough, and the difference is then
    raised as a residual rather than quietly absorbed.
    """
    for txn in _credits(ctx):
        if ctx.is_claimed("bank_txn", txn.bank_ref):
            continue

        scored = []
        for batch in ctx.batches:
            if ctx.is_claimed("settlement_batch", batch.settlement_id):
                continue
            delta = txn.credit_paise - batch.reported_amount_paise
            if abs(delta) <= FUZZY_AMOUNT_TOLERANCE_PAISE and _within_window(
                txn, batch, CREDIT_WINDOW_DAYS
            ):
                scored.append((abs(delta), batch, delta))

        # Ambiguity is not resolved by picking the closest. Two candidates
        # within ₹50 of each other means we genuinely do not know.
        if len(scored) != 1:
            continue

        _, batch, delta = scored[0]
        ctx.record_match(
            tier=MatchTier.TIER1_BANK_TO_BATCH,
            rule_id="T1.AMOUNT_NEAR",
            left_kind="bank_txn",
            left_id=txn.bank_ref,
            right_kind="settlement_batch",
            right_id=batch.settlement_id,
            amount_paise=txn.credit_paise,
            confidence=700,
            evidence={
                "reason": "single batch within tolerance; difference raised as a residual",
                "bank_credit_paise": txn.credit_paise,
                "batch_amount_paise": batch.reported_amount_paise,
                "delta_paise": delta,
                "tolerance_paise": FUZZY_AMOUNT_TOLERANCE_PAISE,
                "days_apart": days_between(txn.value_date, batch.settled_at),
                "narration": txn.narration,
            },
        )


def _check_residuals(ctx: ReconContext) -> None:
    """Every tier-1 match, checked for a money gap — whatever rule made it.

    Deliberately a separate pass rather than a branch inside each rule. A UTR
    match is a statement about *identity*, not about amount: the payout can be
    correctly identified and still arrive short because the bank took its own
    NEFT charge. Checking the residual only on the fuzzy path, as an earlier
    version of this did, meant the most common real case — UTR present, credit
    short — silently reconciled with the shortfall swallowed.
    """
    bank_by_ref = {t.bank_ref: t for t in ctx.bank_txns}

    for match in ctx.matches:
        if match.tier != MatchTier.TIER1_BANK_TO_BATCH:
            continue

        txn = bank_by_ref.get(match.left_id)
        batch = ctx.batch_by_id.get(match.right_id)
        if txn is None or batch is None:
            continue

        delta = txn.credit_paise - batch.reported_amount_paise
        if delta == 0:
            continue

        if abs(delta) <= ROUNDING_TOLERANCE_PAISE:
            code = ExceptionCode.ROUNDING
            summary = (
                f"Bank credit differs from the payout by {abs(delta)} paise — "
                "rounding drift, not a deduction."
            )
        else:
            code = ExceptionCode.SHORT_CREDIT if delta < 0 else ExceptionCode.OVER_CREDIT
            summary = (
                f"Bank credited {format_inr(abs(delta))} "
                f"{'less' if delta < 0 else 'more'} than the payout of "
                f"{format_inr(batch.reported_amount_paise)}."
            )

        ctx.record_exception(
            code=code,
            subject_kind="settlement_batch",
            subject_id=batch.settlement_id,
            residual_paise=abs(delta),
            summary=summary,
            evidence={
                "bank_ref": txn.bank_ref,
                "narration": txn.narration,
                "bank_credit_paise": txn.credit_paise,
                "batch_amount_paise": batch.reported_amount_paise,
                "delta_paise": delta,
                "matched_by": match.rule_id,
                "value_date": txn.value_date.isoformat() if txn.value_date else None,
                # The UTR is the reference the bank will ask for first. Leaving
                # it out produced a draft message that said "UTR —" while the
                # evidence panel right above it showed the number.
                "settlement_utr": batch.settlement_utr,
                "settlement_id": batch.settlement_id,
            },
        )


def _flag_unmatched(ctx: ReconContext) -> None:
    """Everything tier 1 could not link, on both sides."""
    for txn in _credits(ctx):
        if ctx.is_claimed("bank_txn", txn.bank_ref):
            continue
        from unnet.ingest.narration import parse_narration

        parsed = parse_narration(txn.narration)
        if not parsed.looks_like_payout:
            # Not a Razorpay payout at all. The merchant's own business
            # activity is not a reconciliation break.
            continue

        ctx.record_exception(
            code=ExceptionCode.UNMATCHED_BANK_CREDIT,
            subject_kind="bank_txn",
            subject_id=txn.bank_ref,
            residual_paise=txn.credit_paise,
            summary=(
                "Bank credit looks like a Razorpay payout but carries no UTR we "
                "can tie to a settlement."
            ),
            evidence={
                "narration": txn.narration,
                "credit_paise": txn.credit_paise,
                "value_date": txn.value_date.isoformat() if txn.value_date else None,
                "regex_found_utr": parsed.utr,
            },
        )

    latest_credit = max((t.value_date for t in _credits(ctx)), default=None)
    for batch in ctx.batches:
        if ctx.is_claimed("settlement_batch", batch.settlement_id):
            continue

        # A payout initiated inside the last couple of days has not gone
        # missing; NEFT simply has not landed yet. Reporting it as an error is
        # what makes naive recon tools cry wolf every single morning.
        gap = days_between(latest_credit, batch.settled_at)
        in_flight = gap is not None and gap <= IN_FLIGHT_WINDOW_DAYS

        ctx.record_exception(
            code=(
                ExceptionCode.TIMING_DIFFERENCE
                if in_flight
                else ExceptionCode.MISSING_BANK_CREDIT
            ),
            subject_kind="settlement_batch",
            subject_id=batch.settlement_id,
            residual_paise=batch.reported_amount_paise,
            status=(
                ExceptionStatus.ROLLED_FORWARD if in_flight else ExceptionStatus.OPEN
            ),
            summary=(
                "Payout initiated but not yet credited; inside the settlement "
                "window, so carried into the next run."
                if in_flight
                else "Payout was reported as settled but never reached the bank."
            ),
            evidence={
                "settlement_utr": batch.settlement_utr,
                "batch_amount_paise": batch.reported_amount_paise,
                "settled_at": batch.settled_at.isoformat() if batch.settled_at else None,
                "days_since_last_bank_credit": gap,
                "in_flight_window_days": IN_FLIGHT_WINDOW_DAYS,
            },
        )


def _within_window(txn, batch, window_days: float) -> bool:
    gap = days_between(txn.value_date, batch.settled_at or batch.created_at)
    return gap is not None and gap <= window_days
