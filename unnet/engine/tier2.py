"""Tier 2 — settlement line <-> merchant order.

"Razorpay settled these 1,400 transactions; which of my orders were they?"

Exact identifier matching does almost all the work. The fuzzy rule exists for
the real case where a merchant's export carries no gateway id at all, and it
holds the same line as tier 1: a unique candidate or nothing.
"""

from __future__ import annotations

from collections import defaultdict

from unnet.core.models import EntityType, ExceptionCode, MatchTier
from unnet.engine.context import ReconContext, days_between

#: An order and its settlement line should be within the settlement lag.
CAPTURE_WINDOW_DAYS = 4.0


def run(ctx: ReconContext) -> None:
    _flag_duplicate_orders(ctx)
    _match_by_payment_id(ctx)
    _match_by_order_id(ctx)
    _match_by_amount_and_time(ctx)
    _flag_unmatched(ctx)


def _payment_lines(ctx: ReconContext):
    """Payment lines that are actually in a payout.

    A line on risk hold is reported by the gateway but belongs to no
    settlement. Matching it would tell the merchant their order was settled
    when the money is frozen — so held lines are excluded here and surface
    through the ON_HOLD exception instead.
    """
    return [
        line
        for line in ctx.lines
        if line.type == EntityType.PAYMENT and not line.on_hold and line.settled
    ]


def _flag_duplicate_orders(ctx: ReconContext) -> None:
    """The same order twice in the merchant's own export.

    Resolved before matching, and resolved rather than merely reported. Two rows
    sharing one order id make every downstream lookup ambiguous, so the
    uniqueness rules refuse them and *both* copies go unmatched — one duplicated
    row would otherwise cost two matches and drag a real settlement line into
    the orphan pile with it.

    So we keep the first occurrence as canonical, and claim the surplus copies
    so they are out of the way. The exception still records that the merchant's
    export is duplicated; the money is simply attributed once.
    """
    for order_id, rows in ctx.orders_by_order_id.items():
        if len(rows) <= 1:
            continue

        surplus = rows[1:]
        ctx.record_exception(
            code=ExceptionCode.DUPLICATE,
            subject_kind="merchant_order",
            subject_id=order_id,
            residual_paise=rows[0].gross_paise * len(surplus),
            summary=(
                f"Order appears {len(rows)} times in the merchant ledger; the first "
                "occurrence is treated as canonical and the rest are set aside."
            ),
            evidence={
                "occurrences": len(rows),
                "gross_paise": rows[0].gross_paise,
                "invoice_nos": [r.invoice_no for r in rows],
                "canonical_invoice_no": rows[0].invoice_no,
            },
        )

        # Collapse the index so every later lookup sees a single unambiguous row.
        ctx.orders_by_order_id[order_id] = [rows[0]]
        if rows[0].payment_id:
            by_payment = ctx.orders_by_payment_id.get(rows[0].payment_id, [])
            if len(by_payment) > 1:
                ctx.orders_by_payment_id[rows[0].payment_id] = [rows[0]]


def _match_by_payment_id(ctx: ReconContext) -> None:
    for line in _payment_lines(ctx):
        if not line.payment_id or ctx.is_claimed("settlement_line", line.entity_id):
            continue
        candidates = [
            o
            for o in ctx.orders_by_payment_id.get(line.payment_id, [])
            if not ctx.is_claimed("merchant_order", o.order_id)
        ]
        if len(candidates) != 1:
            continue

        order = candidates[0]
        ctx.record_match(
            tier=MatchTier.TIER2_LINE_TO_ORDER,
            rule_id="T2.PAYMENT_ID_EXACT",
            left_kind="settlement_line",
            left_id=line.entity_id,
            right_kind="merchant_order",
            right_id=order.order_id,
            amount_paise=line.amount_paise,
            confidence=1000,
            evidence={
                "payment_id": line.payment_id,
                "line_amount_paise": line.amount_paise,
                "order_gross_paise": order.gross_paise,
                "amount_delta_paise": line.amount_paise - order.gross_paise,
            },
        )


def _match_by_order_id(ctx: ReconContext) -> None:
    for line in _payment_lines(ctx):
        if not line.order_id or ctx.is_claimed("settlement_line", line.entity_id):
            continue
        candidates = [
            o
            for o in ctx.orders_by_order_id.get(line.order_id, [])
            if not ctx.is_claimed("merchant_order", o.order_id)
        ]
        if len(candidates) != 1:
            continue

        order = candidates[0]
        ctx.record_match(
            tier=MatchTier.TIER2_LINE_TO_ORDER,
            rule_id="T2.ORDER_ID_EXACT",
            left_kind="settlement_line",
            left_id=line.entity_id,
            right_kind="merchant_order",
            right_id=order.order_id,
            amount_paise=line.amount_paise,
            confidence=980,
            evidence={
                "order_id": line.order_id,
                "line_amount_paise": line.amount_paise,
                "order_gross_paise": order.gross_paise,
                "amount_delta_paise": line.amount_paise - order.gross_paise,
            },
        )


def _match_by_amount_and_time(ctx: ReconContext) -> None:
    """No shared identifier: fall back to amount + method + capture time.

    Bucketed by exact paise so this stays linear rather than comparing every
    line against every order — at 1,500 orders the naive version is a minute of
    wall clock for nothing.
    """
    by_amount: dict[tuple[int, str | None], list] = defaultdict(list)
    for order in ctx.orders:
        if ctx.is_claimed("merchant_order", order.order_id):
            continue
        by_amount[(order.gross_paise, order.method)].append(order)

    for line in _payment_lines(ctx):
        if ctx.is_claimed("settlement_line", line.entity_id):
            continue

        bucket = by_amount.get((line.amount_paise, line.method), [])
        candidates = [
            o
            for o in bucket
            if not ctx.is_claimed("merchant_order", o.order_id)
            and (days_between(o.captured_at, line.created_at) or 99) <= CAPTURE_WINDOW_DAYS
        ]
        if len(candidates) != 1:
            continue

        order = candidates[0]
        ctx.record_match(
            tier=MatchTier.TIER2_LINE_TO_ORDER,
            rule_id="T2.AMOUNT_METHOD_TIME",
            left_kind="settlement_line",
            left_id=line.entity_id,
            right_kind="merchant_order",
            right_id=order.order_id,
            amount_paise=line.amount_paise,
            confidence=650,
            evidence={
                "reason": "no shared identifier; unique order at same amount, method and time",
                "amount_paise": line.amount_paise,
                "method": line.method,
                "days_apart": days_between(order.captured_at, line.created_at),
            },
        )


def _flag_unmatched(ctx: ReconContext) -> None:
    for line in _payment_lines(ctx):
        if ctx.is_claimed("settlement_line", line.entity_id):
            continue
        ctx.record_exception(
            code=ExceptionCode.ORPHAN_SETTLEMENT_LINE,
            subject_kind="settlement_line",
            subject_id=line.entity_id,
            residual_paise=line.net_paise,
            summary="Razorpay settled a payment with no matching order in the merchant ledger.",
            evidence={
                "payment_id": line.payment_id,
                "order_id": line.order_id,
                "amount_paise": line.amount_paise,
                "net_paise": line.net_paise,
                "settlement_id": line.settlement_id,
                "created_at": line.created_at.isoformat() if line.created_at else None,
            },
        )

    seen: set[str] = set()
    for order in ctx.orders:
        if ctx.is_claimed("merchant_order", order.order_id):
            continue
        # One exception per order, not per row: surplus copies of a duplicated
        # row are already accounted for by the DUPLICATE exception, and
        # reporting each copy again would double count the same rupees.
        #
        # Deduplicating on order_id here rather than skipping duplicated orders
        # outright matters — an order can be both duplicated *and* held by
        # risk, and the merchant needs to be told both things.
        if order.order_id in seen:
            continue
        seen.add(order.order_id)

        # Was it held back by risk, or is it simply missing?
        held = [
            line
            for line in ctx.lines
            if line.on_hold
            and (line.order_id == order.order_id or line.payment_id == order.payment_id)
        ]
        ctx.record_exception(
            code=ExceptionCode.ON_HOLD if held else ExceptionCode.UNSETTLED_ORDER,
            subject_kind="merchant_order",
            subject_id=order.order_id,
            residual_paise=order.gross_paise,
            summary=(
                "Payment is on risk hold, so it is not in any payout yet."
                if held
                else "Order was captured but never appeared in a settlement."
            ),
            evidence={
                "payment_id": order.payment_id,
                "gross_paise": order.gross_paise,
                "method": order.method,
                "captured_at": order.captured_at.isoformat() if order.captured_at else None,
                "invoice_no": order.invoice_no,
            },
        )
