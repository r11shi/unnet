"""Tier 3 — refunds and chargebacks <-> the payment they reverse.

This is where naive reconciliation quietly goes wrong. A refund or a chargeback
does not adjust the original payment; it is deducted from a *later* payout. So
the money leaves in one settlement cycle for a sale that closed in another, and
anyone matching cycle-by-cycle sees two unexplained holes instead of one linked
pair.
"""

from __future__ import annotations

from collections import defaultdict

from unnet.core.models import EntityType, ExceptionCode, MatchTier
from unnet.core.money import format_inr
from unnet.engine.context import ReconContext

REVERSAL_TYPES = {EntityType.REFUND, EntityType.DISPUTE}


def run(ctx: ReconContext) -> None:
    _match_reversals(ctx)
    _flag_split_refunds(ctx)
    _flag_chargebacks(ctx)


def _reversals(ctx: ReconContext):
    return [line for line in ctx.lines if line.type in REVERSAL_TYPES]


def _match_reversals(ctx: ReconContext) -> None:
    for line in _reversals(ctx):
        original = None
        rule = None

        if line.payment_id:
            candidates = ctx.payment_lines_by_payment_id.get(line.payment_id, [])
            if len(candidates) == 1:
                original, rule = candidates[0], "T3.PAYMENT_ID_EXACT"

        if original is None and line.order_id:
            candidates = [
                p
                for p in ctx.lines
                if p.type == EntityType.PAYMENT and p.order_id == line.order_id
            ]
            if len(candidates) == 1:
                original, rule = candidates[0], "T3.ORDER_ID_EXACT"

        if original is None:
            ctx.record_exception(
                code=ExceptionCode.REFUND_WITHOUT_ORIGINAL,
                subject_kind="settlement_line",
                subject_id=line.entity_id,
                residual_paise=line.amount_paise,
                summary=(
                    f"{line.type.value.capitalize()} deducted from a payout, but the "
                    "payment it reverses is not in this dataset."
                ),
                evidence={
                    "type": line.type.value,
                    "payment_id": line.payment_id,
                    "order_id": line.order_id,
                    "amount_paise": line.amount_paise,
                    "fee_paise": line.fee_paise,
                    "settlement_id": line.settlement_id,
                },
            )
            continue

        cross_cycle = original.settlement_id != line.settlement_id
        ctx.record_match(
            tier=MatchTier.TIER3_REVERSAL_TO_PAYMENT,
            rule_id=rule,
            left_kind="settlement_line",
            left_id=line.entity_id,
            right_kind="settlement_line",
            right_id=original.entity_id,
            amount_paise=line.amount_paise,
            confidence=1000,
            evidence={
                "type": line.type.value,
                "payment_id": line.payment_id,
                "reversal_amount_paise": line.amount_paise,
                "original_amount_paise": original.amount_paise,
                "partial": line.amount_paise < original.amount_paise,
                "original_settlement_id": original.settlement_id,
                "reversal_settlement_id": line.settlement_id,
                "cross_cycle": cross_cycle,
            },
        )


def _flag_split_refunds(ctx: ReconContext) -> None:
    """One refund the merchant booked, two lines the gateway reported.

    Not an error — but the merchant's ledger has a single number and the
    gateway has two, so anything comparing them row-for-row reports a false
    break. Naming it explicitly is what stops that.
    """
    by_payment: dict[str, list] = defaultdict(list)
    for line in _reversals(ctx):
        if line.type == EntityType.REFUND and line.payment_id:
            by_payment[line.payment_id].append(line)

    for payment_id, refunds in by_payment.items():
        if len(refunds) < 2:
            continue
        total = sum(r.amount_paise for r in refunds)
        ctx.record_exception(
            code=ExceptionCode.PARTIAL_REFUND_SPLIT,
            subject_kind="settlement_line",
            subject_id=refunds[0].entity_id,
            residual_paise=0,
            summary=(
                f"One refund reported as {len(refunds)} settlement lines totalling "
                f"{format_inr(total)}."
            ),
            evidence={
                "payment_id": payment_id,
                "line_ids": [r.entity_id for r in refunds],
                "amounts_paise": [r.amount_paise for r in refunds],
                "total_paise": total,
                "settlement_ids": sorted({r.settlement_id for r in refunds if r.settlement_id}),
            },
        )


def _flag_chargebacks(ctx: ReconContext) -> None:
    """Chargebacks always need a human, so they are always surfaced."""
    for line in _reversals(ctx):
        if line.type != EntityType.DISPUTE:
            continue

        original = None
        if line.payment_id:
            candidates = ctx.payment_lines_by_payment_id.get(line.payment_id, [])
            original = candidates[0] if len(candidates) == 1 else None

        ctx.record_exception(
            code=ExceptionCode.CHARGEBACK_DEDUCTION,
            subject_kind="settlement_line",
            subject_id=line.entity_id,
            residual_paise=line.amount_paise + line.fee_paise,
            summary=(
                f"Chargeback of {format_inr(line.amount_paise)} plus "
                f"{format_inr(line.fee_paise)} in dispute fees deducted from this payout."
            ),
            evidence={
                "dispute_id": line.dispute_id,
                "payment_id": line.payment_id,
                "disputed_paise": line.amount_paise,
                "dispute_fee_paise": line.fee_paise,
                "gst_on_fee_paise": line.tax_paise,
                "deducted_in_settlement": line.settlement_id,
                "original_settlement": original.settlement_id if original else None,
                "cross_cycle": bool(original and original.settlement_id != line.settlement_id),
            },
        )
