"""The netting proof: turning one lumped credit back into its components.

Razorpay documents the payout as::

    settled = Payment - Adjustment - Tax - Fee - Transfer + Refunds

We compute it two independent ways and require them to agree:

1. From the ``credit``/``debit`` columns, which are unambiguous about direction.
2. From ``amount`` and ``fee`` per line, by entity type.

Two derivations that agree is a proof; one derivation is an assertion. If they
disagree the report itself is internally inconsistent, and that is worth saying
out loud rather than picking whichever number looks nicer.

The fee and GST on every payment line are also recomputed from a rate card, so
an incorrectly billed MDR surfaces as its own exception instead of vanishing
into the total.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from unnet.core.models import EntityType, ExceptionCode
from unnet.core.money import apply_bps, gst_on
from unnet.engine.context import ReconContext

#: The merchant's contracted MDR, in basis points, by method.
DEFAULT_RATE_CARD_BPS = {
    "upi": 0,
    "netbanking": 150,
    "wallet": 180,
    "card": 200,
}

#: Fee recomputation tolerance. One paisa of rounding is not a billing dispute.
FEE_TOLERANCE_PAISE = 1
GST_TOLERANCE_PAISE = 1


@dataclass
class BatchBreakdown:
    """Everything that turned gross sales into one bank credit."""

    settlement_id: str
    settlement_utr: str | None = None
    settled_at: str | None = None

    gross_paise: int = 0
    mdr_paise: int = 0
    gst_paise: int = 0
    refunds_paise: int = 0
    disputes_paise: int = 0
    dispute_fees_paise: int = 0
    adjustments_paise: int = 0
    transfers_paise: int = 0

    #: Σcredit − Σdebit.
    computed_net_paise: int = 0
    #: The same figure derived from amount/fee by type.
    dual_net_paise: int = 0
    #: What the settlements report claims it paid.
    reported_net_paise: int = 0
    #: What actually landed, if we matched a bank credit.
    bank_credit_paise: int | None = None

    line_count: int = 0
    payment_count: int = 0
    refund_count: int = 0
    dispute_count: int = 0

    @property
    def internally_consistent(self) -> bool:
        return self.computed_net_paise == self.dual_net_paise

    @property
    def report_residual_paise(self) -> int:
        """Gap between our recomputation and what the report claims."""
        return self.computed_net_paise - self.reported_net_paise

    @property
    def bank_residual_paise(self) -> int | None:
        """Gap between what was promised and what arrived."""
        if self.bank_credit_paise is None:
            return None
        return self.bank_credit_paise - self.reported_net_paise

    def waterfall(self) -> list[dict]:
        """Steps for the un-netting chart, in the order money leaves."""
        steps = [
            {"label": "Gross sales", "delta_paise": self.gross_paise, "kind": "start"},
            {"label": "MDR", "delta_paise": -self.mdr_paise, "kind": "fee"},
            {"label": "GST on MDR", "delta_paise": -self.gst_paise, "kind": "fee"},
            {"label": "Refunds", "delta_paise": -self.refunds_paise, "kind": "reversal"},
            {"label": "Chargebacks", "delta_paise": -self.disputes_paise, "kind": "reversal"},
            {
                "label": "Dispute fees",
                "delta_paise": -self.dispute_fees_paise,
                "kind": "fee",
            },
            {
                "label": "Adjustments",
                "delta_paise": -self.adjustments_paise,
                "kind": "adjustment",
            },
            {"label": "Transfers", "delta_paise": -self.transfers_paise, "kind": "transfer"},
            {"label": "Net payout", "delta_paise": self.computed_net_paise, "kind": "total"},
        ]
        if self.bank_credit_paise is not None:
            residual = self.bank_credit_paise - self.computed_net_paise
            if residual:
                steps.append(
                    {
                        "label": "Unexplained at bank",
                        "delta_paise": residual,
                        "kind": "residual",
                    }
                )
            steps.append(
                {
                    "label": "Bank credit",
                    "delta_paise": self.bank_credit_paise,
                    "kind": "total",
                }
            )
        # Drop zero-value middle steps so the chart shows what actually happened.
        return [s for s in steps if s["delta_paise"] or s["kind"] in {"start", "total"}]


@dataclass
class NettingResult:
    breakdowns: list[BatchBreakdown] = field(default_factory=list)

    def by_id(self) -> dict[str, BatchBreakdown]:
        return {b.settlement_id: b for b in self.breakdowns}


def run(ctx: ReconContext, rate_card: dict[str, int] | None = None) -> NettingResult:
    rates = rate_card or DEFAULT_RATE_CARD_BPS
    result = NettingResult()

    bank_by_batch = {
        m.right_id: m
        for m in ctx.matches
        if m.left_kind == "bank_txn" and m.right_kind == "settlement_batch"
    }
    bank_by_ref = {t.bank_ref: t for t in ctx.bank_txns}

    for batch in ctx.batches:
        lines = ctx.lines_by_settlement.get(batch.settlement_id, [])
        breakdown = BatchBreakdown(
            settlement_id=batch.settlement_id,
            settlement_utr=batch.settlement_utr,
            settled_at=batch.settled_at.isoformat() if batch.settled_at else None,
            reported_net_paise=batch.reported_amount_paise,
            line_count=len(lines),
        )

        for line in lines:
            breakdown.computed_net_paise += line.net_paise

            if line.type == EntityType.PAYMENT:
                breakdown.payment_count += 1
                breakdown.gross_paise += line.amount_paise
                breakdown.gst_paise += line.tax_paise
                breakdown.mdr_paise += line.fee_paise - line.tax_paise
                breakdown.dual_net_paise += line.amount_paise - line.fee_paise
                _check_fee(ctx, line, rates)
            else:
                breakdown.dual_net_paise -= line.amount_paise + line.fee_paise
                if line.type == EntityType.REFUND:
                    breakdown.refund_count += 1
                    breakdown.refunds_paise += line.amount_paise
                elif line.type == EntityType.DISPUTE:
                    breakdown.dispute_count += 1
                    breakdown.disputes_paise += line.amount_paise
                    breakdown.dispute_fees_paise += line.fee_paise
                elif line.type == EntityType.TRANSFER:
                    breakdown.transfers_paise += line.amount_paise + line.fee_paise
                else:
                    breakdown.adjustments_paise += line.amount_paise + line.fee_paise

        match = bank_by_batch.get(batch.settlement_id)
        if match:
            txn = bank_by_ref.get(match.left_id)
            if txn:
                breakdown.bank_credit_paise = txn.credit_paise

        _check_batch(ctx, breakdown)
        result.breakdowns.append(breakdown)

    return result


def _check_fee(ctx: ReconContext, line, rates: dict[str, int]) -> None:
    """Recompute MDR and GST from the rate card rather than trusting the report."""
    rate = rates.get((line.method or "").lower())
    if rate is None:
        return

    charged_mdr = line.fee_paise - line.tax_paise
    expected_mdr = apply_bps(line.amount_paise, rate)

    if abs(charged_mdr - expected_mdr) > FEE_TOLERANCE_PAISE:
        ctx.record_exception(
            code=ExceptionCode.FEE_MISMATCH,
            subject_kind="settlement_line",
            subject_id=line.entity_id,
            residual_paise=charged_mdr - expected_mdr,
            summary=(
                f"MDR billed {charged_mdr} paise, rate card says {expected_mdr} paise "
                f"({rate} bps on {line.amount_paise} paise)."
            ),
            evidence={
                "method": line.method,
                "rate_bps": rate,
                "amount_paise": line.amount_paise,
                "charged_mdr_paise": charged_mdr,
                "expected_mdr_paise": expected_mdr,
                "delta_paise": charged_mdr - expected_mdr,
                "settlement_id": line.settlement_id,
            },
        )
        return

    # Only worth checking GST once we know the fee it is levied on is right.
    expected_gst = gst_on(charged_mdr)
    if abs(line.tax_paise - expected_gst) > GST_TOLERANCE_PAISE:
        ctx.record_exception(
            code=ExceptionCode.GST_MISMATCH,
            subject_kind="settlement_line",
            subject_id=line.entity_id,
            residual_paise=line.tax_paise - expected_gst,
            summary=(
                f"GST charged {line.tax_paise} paise, expected {expected_gst} paise "
                f"(18% of {charged_mdr} paise MDR). Input tax credit needs this to be right."
            ),
            evidence={
                "mdr_paise": charged_mdr,
                "charged_gst_paise": line.tax_paise,
                "expected_gst_paise": expected_gst,
                "delta_paise": line.tax_paise - expected_gst,
                "settlement_id": line.settlement_id,
            },
        )


def _check_batch(ctx: ReconContext, breakdown: BatchBreakdown) -> None:
    if not breakdown.internally_consistent:
        ctx.record_exception(
            code=ExceptionCode.SCHEMA_UNPARSEABLE,
            subject_kind="settlement_batch",
            subject_id=breakdown.settlement_id,
            residual_paise=breakdown.computed_net_paise - breakdown.dual_net_paise,
            summary=(
                "The settlement report disagrees with itself: credit/debit and "
                "amount/fee give different net figures for this payout."
            ),
            evidence={
                "credit_debit_net_paise": breakdown.computed_net_paise,
                "amount_fee_net_paise": breakdown.dual_net_paise,
            },
        )

    if breakdown.report_residual_paise:
        ctx.record_exception(
            code=ExceptionCode.SHORT_CREDIT
            if breakdown.report_residual_paise < 0
            else ExceptionCode.OVER_CREDIT,
            subject_kind="settlement_batch",
            subject_id=breakdown.settlement_id,
            residual_paise=abs(breakdown.report_residual_paise),
            summary=(
                "Sum of settlement lines does not equal the payout amount Razorpay "
                "reported for this settlement."
            ),
            evidence={
                "lines_net_paise": breakdown.computed_net_paise,
                "reported_net_paise": breakdown.reported_net_paise,
                "delta_paise": breakdown.report_residual_paise,
                "line_count": breakdown.line_count,
            },
        )
