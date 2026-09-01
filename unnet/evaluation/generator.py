"""Synthetic three-source fixture generator.

Produces the three files a real merchant actually has to reconcile:

1. ``merchant_ledger.csv``          — the merchant's own books, with their own
                                      column names and date format.
2. ``razorpay_settlement_recon.csv`` — the gateway's settlement recon report.
3. ``bank_statement.csv``            — the bank's export, where a whole payout
                                      arrives as one lumped credit and the UTR
                                      is buried in a narration string.

It also writes ``ground_truth.json``. **The engine never reads that file.** It
exists only so ``unnet.evaluation.score`` can grade a run against what actually
happened, which is what lets us publish a false-match rate instead of a vibe.

Everything is driven by a seeded RNG, so ``make gen`` reproduces the exact
fixtures — and therefore the exact published metrics — on any machine.
"""

from __future__ import annotations

import csv
import json
import random
import string
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from unnet.core.money import apply_bps, gst_on, paise_to_rupees

# Razorpay's own MDR shape: UPI is zero-rated, cards cost the most.
RATE_CARD_BPS = {
    "upi": 0,
    "netbanking": 150,
    "wallet": 180,
    "card": 200,
}

# What a chargeback costs the merchant on top of the disputed amount.
DISPUTE_FEE_PAISE = 59_000  # ₹590

BANK_IFSC_PREFIXES = ["KKBKH", "HDFCH", "ICICH", "UTIBH", "SBINH"]
CARD_NETWORKS = ["Visa", "MasterCard", "RuPay", "Amex"]
CARD_ISSUERS = ["HDFC Bank", "ICICI Bank", "Axis Bank", "SBI Cards", "Kotak"]


@dataclass
class DefectRates:
    """How often each break is injected.

    These are the knobs the eval harness reports against. Rates are deliberately
    higher than a healthy production month — a fixture where everything matches
    proves nothing about the exception path.
    """

    utr_missing_from_narration: float = 0.22
    short_credit: float = 0.10
    orphan_settlement_line: float = 0.02
    unsettled_on_hold: float = 0.02
    fee_mismatch: float = 0.015
    gst_mismatch: float = 0.01
    refund_without_original: float = 0.15  # of refunds
    partial_refund_split: float = 0.20  # of refunds
    duplicate_order_row: float = 0.01
    rounding_drift: float = 0.02
    #: Fraction of payments that get refunded at all.
    refund_rate: float = 0.06
    #: Fraction of payments that get charged back.
    dispute_rate: float = 0.012


@dataclass
class GeneratorConfig:
    seed: int = 20260905
    n_payments: int = 1500
    n_days: int = 21
    settlement_lag_days: int = 2
    merchant_name: str = "Kirana Katalog Pvt Ltd"
    out_dir: Path = Path("data/synthetic")
    defects: DefectRates = field(default_factory=DefectRates)


@dataclass
class GroundTruth:
    """What actually happened. Held out from the engine."""

    #: order_id -> settlement entity_id of the payment line
    order_to_line: dict[str, str] = field(default_factory=dict)
    #: entity_id -> settlement_id
    line_to_batch: dict[str, str] = field(default_factory=dict)
    #: settlement_id -> bank_ref of the credit that paid it
    batch_to_bank: dict[str, str] = field(default_factory=dict)
    #: reversal entity_id -> payment_id it reverses
    reversal_to_payment: dict[str, str] = field(default_factory=dict)
    #: Breaks we deliberately created, as (code, subject_kind, subject_id, residual).
    expected_exceptions: list[dict] = field(default_factory=list)
    stats: dict = field(default_factory=dict)


class _Ids:
    """Razorpay-shaped identifiers, drawn from the seeded RNG."""

    ALPHABET = string.ascii_letters + string.digits

    def __init__(self, rng: random.Random) -> None:
        self.rng = rng
        self._seen: set[str] = set()

    def _token(self, n: int) -> str:
        while True:
            token = "".join(self.rng.choice(self.ALPHABET) for _ in range(n))
            if token not in self._seen:
                self._seen.add(token)
                return token

    def payment(self) -> str:
        return f"pay_{self._token(14)}"

    def order(self) -> str:
        return f"order_{self._token(14)}"

    def refund(self) -> str:
        return f"rfnd_{self._token(14)}"

    def dispute(self) -> str:
        return f"disp_{self._token(14)}"

    def adjustment(self) -> str:
        return f"adj_{self._token(14)}"

    def settlement(self) -> str:
        return f"setl_{self._token(14)}"

    def utr(self) -> str:
        prefix = self.rng.choice(BANK_IFSC_PREFIXES)
        return f"{prefix}{self.rng.randint(10**10, 10**11 - 1)}"


def _defect_count(rate: float, population: int) -> int:
    """How many of ``population`` to corrupt at ``rate``.

    Always at least one whenever the rate is non-zero and there is anything to
    corrupt, so no exception class silently disappears from a fixture.
    """
    if population <= 0 or rate <= 0:
        return 0
    return max(1, min(population, round(rate * population)))


def generate(config: GeneratorConfig | None = None) -> GroundTruth:
    cfg = config or GeneratorConfig()
    rng = random.Random(cfg.seed)
    ids = _Ids(rng)
    truth = GroundTruth()

    start = datetime(2026, 8, 3, 0, 0, 0)

    orders: list[dict] = []
    lines: list[dict] = []
    batches: list[dict] = []
    bank_rows: list[dict] = []

    # ------------------------------------------------------------------ #
    # 1. Payments, spread over the window and bucketed by capture day.
    # ------------------------------------------------------------------ #
    payments_by_day: dict[int, list[dict]] = {d: [] for d in range(cfg.n_days)}

    for _ in range(cfg.n_payments):
        day = rng.randrange(cfg.n_days)
        captured = start + timedelta(
            days=day, hours=rng.randrange(7, 23), minutes=rng.randrange(60)
        )
        method = rng.choices(
            ["upi", "card", "netbanking", "wallet"], weights=[58, 26, 11, 5]
        )[0]
        # A long-tailed basket: lots of small UPI, a few large card orders.
        gross = rng.choice(
            [
                rng.randrange(9_900, 150_000),
                rng.randrange(150_000, 800_000),
                rng.randrange(800_000, 4_500_000),
            ]
        )
        gross = (gross // 100) * 100  # whole rupees, as most checkouts are

        payment = {
            "payment_id": ids.payment(),
            "order_id": ids.order(),
            "gross": gross,
            "method": method,
            "captured_at": captured,
            "day": day,
            "on_hold": False,
            "refunded": 0,
        }
        payments_by_day[day].append(payment)
        orders.append(payment)

    # ------------------------------------------------------------------ #
    # 2. Refunds and disputes. Both land in a *later* batch than the
    #    original payment, which is the classic reconciliation break.
    # ------------------------------------------------------------------ #
    reversals_by_day: dict[int, list[dict]] = {d: [] for d in range(cfg.n_days + 4)}

    # Only these days actually produce a settlement batch. Reversals are clamped
    # into the window so every one of them lands in a real batch — a reversal
    # generated outside it would silently vanish and quietly corrupt the ground
    # truth we grade against.
    settle_day_lo = cfg.settlement_lag_days
    settle_day_hi = cfg.n_days + cfg.settlement_lag_days - 1

    def clamp_settle_day(value: int) -> int:
        return max(settle_day_lo, min(value, settle_day_hi))

    for payment in orders:
        if rng.random() < cfg.defects.refund_rate:
            partial = rng.random() < 0.45
            amount = (
                (payment["gross"] * rng.randrange(20, 80) // 100 // 100) * 100
                if partial
                else payment["gross"]
            )
            if amount <= 0:
                continue

            orphaned = rng.random() < cfg.defects.refund_without_original
            # An orphaned refund is one the *gateway* deducted and the merchant
            # has no record of, so it must not appear in the merchant's books.
            # Writing it to both sides would make it a two-sided break and the
            # ground-truth label would no longer describe what we injected.
            if not orphaned:
                payment["refunded"] = amount

            lag = rng.randrange(1, 5)
            day = clamp_settle_day(payment["day"] + lag)

            split = rng.random() < cfg.defects.partial_refund_split and amount > 20_000
            if split:
                # One logical refund reported as two settlement lines. The
                # merchant's books carry a single number.
                first = (amount // 2 // 100) * 100
                pieces = [first, amount - first]
            else:
                pieces = [amount]

            for piece in pieces:
                reversal = {
                    "kind": "refund",
                    "entity_id": ids.refund(),
                    "payment_id": payment["payment_id"],
                    "order_id": payment["order_id"],
                    "amount": piece,
                    "day": day,
                    "method": payment["method"],
                    "split_of": amount if split else None,
                    "orphaned": orphaned,
                }
                if orphaned:
                    # The gateway knows about a payment the merchant's export
                    # does not contain — a real symptom of a partial data pull.
                    reversal["payment_id"] = ids.payment()
                reversals_by_day[reversal["day"]].append(reversal)

        elif rng.random() < cfg.defects.dispute_rate:
            lag = rng.randrange(3, 8)
            day = clamp_settle_day(payment["day"] + lag)
            reversals_by_day[day].append(
                {
                    "kind": "dispute",
                    "entity_id": ids.dispute(),
                    "payment_id": payment["payment_id"],
                    "order_id": payment["order_id"],
                    "amount": payment["gross"],
                    "day": day,
                    "method": payment["method"],
                    "split_of": None,
                    "orphaned": False,
                }
            )

    # ------------------------------------------------------------------ #
    # 3. Settlement batches: T+2 on the day's captures, plus reversals.
    # ------------------------------------------------------------------ #
    # Batch-level defects are injected by *count*, not by coin-flip. There are
    # only ~21 batches, so a 2% per-batch probability routinely produces a
    # fixture with zero instances of a defect class, and an exception the
    # fixture never contains is an exception the metrics can never exercise.
    orphan_days = set(
        rng.sample(
            range(cfg.n_days),
            k=_defect_count(cfg.defects.orphan_settlement_line, cfg.n_days),
        )
    )

    for day in range(cfg.n_days):
        settle_day = day + cfg.settlement_lag_days
        settled_at = start + timedelta(days=settle_day, hours=11)
        batch_lines: list[dict] = []

        for payment in payments_by_day[day]:
            if rng.random() < cfg.defects.unsettled_on_hold:
                # Risk hold: the money is real but it is not in this payout.
                payment["on_hold"] = True
                truth.expected_exceptions.append(
                    {
                        "code": "ON_HOLD",
                        "subject_kind": "merchant_order",
                        "subject_id": payment["order_id"],
                        "residual_paise": payment["gross"],
                        "defect": "unsettled_on_hold",
                    }
                )
                continue

            rate = RATE_CARD_BPS[payment["method"]]
            mdr = apply_bps(payment["gross"], rate)

            if rng.random() < cfg.defects.fee_mismatch and mdr > 0:
                mdr += rng.randrange(100, 900)  # gateway billed off rate card
                truth.expected_exceptions.append(
                    {
                        "code": "FEE_MISMATCH",
                        "subject_kind": "settlement_line",
                        "subject_id": payment["payment_id"],
                        "residual_paise": 0,
                        "defect": "fee_mismatch",
                    }
                )

            tax = gst_on(mdr)
            if rng.random() < cfg.defects.gst_mismatch and mdr > 0:
                tax += rng.randrange(1, 60)
                truth.expected_exceptions.append(
                    {
                        "code": "GST_MISMATCH",
                        "subject_kind": "settlement_line",
                        "subject_id": payment["payment_id"],
                        "residual_paise": 0,
                        "defect": "gst_mismatch",
                    }
                )

            fee = mdr + tax
            batch_lines.append(
                {
                    "entity_id": payment["payment_id"],
                    "type": "payment",
                    "amount": payment["gross"],
                    "fee": fee,
                    "tax": tax,
                    "credit": payment["gross"] - fee,
                    "debit": 0,
                    "payment_id": payment["payment_id"],
                    "order_id": payment["order_id"],
                    "dispute_id": None,
                    "method": payment["method"],
                    "created_at": payment["captured_at"],
                }
            )
            truth.order_to_line[payment["order_id"]] = payment["payment_id"]

        # Reversals maturing on this settlement day.
        for reversal in reversals_by_day.get(settle_day, []):
            is_dispute = reversal["kind"] == "dispute"
            fee = DISPUTE_FEE_PAISE if is_dispute else 0
            tax = gst_on(fee) if fee else 0
            total_fee = fee + tax
            batch_lines.append(
                {
                    "entity_id": reversal["entity_id"],
                    "type": "dispute" if is_dispute else "refund",
                    "amount": reversal["amount"],
                    "fee": total_fee,
                    "tax": tax,
                    "credit": 0,
                    "debit": reversal["amount"] + total_fee,
                    "payment_id": reversal["payment_id"],
                    "order_id": None if reversal["orphaned"] else reversal["order_id"],
                    "dispute_id": reversal["entity_id"] if is_dispute else None,
                    "method": reversal["method"],
                    "created_at": start + timedelta(days=settle_day, hours=9),
                }
            )
            if not reversal["orphaned"]:
                truth.reversal_to_payment[reversal["entity_id"]] = reversal["payment_id"]
            else:
                truth.expected_exceptions.append(
                    {
                        "code": "REFUND_WITHOUT_ORIGINAL",
                        "subject_kind": "settlement_line",
                        "subject_id": reversal["entity_id"],
                        "residual_paise": reversal["amount"],
                        "defect": "refund_without_original",
                    }
                )
            if reversal["split_of"] and not reversal["orphaned"]:
                truth.expected_exceptions.append(
                    {
                        "code": "PARTIAL_REFUND_SPLIT",
                        "subject_kind": "settlement_line",
                        "subject_id": reversal["entity_id"],
                        "residual_paise": 0,
                        "defect": "partial_refund_split",
                    }
                )
            if is_dispute:
                truth.expected_exceptions.append(
                    {
                        "code": "CHARGEBACK_DEDUCTION",
                        "subject_kind": "settlement_line",
                        "subject_id": reversal["entity_id"],
                        "residual_paise": reversal["amount"] + total_fee,
                        "defect": "chargeback_deduction",
                    }
                )

        if not batch_lines:
            continue

        # A settlement line whose order was never in the merchant's export.
        if day in orphan_days:
            ghost_amount = (rng.randrange(50_000, 900_000) // 100) * 100
            ghost_id = ids.payment()
            mdr = apply_bps(ghost_amount, RATE_CARD_BPS["card"])
            tax = gst_on(mdr)
            batch_lines.append(
                {
                    "entity_id": ghost_id,
                    "type": "payment",
                    "amount": ghost_amount,
                    "fee": mdr + tax,
                    "tax": tax,
                    "credit": ghost_amount - mdr - tax,
                    "debit": 0,
                    "payment_id": ghost_id,
                    "order_id": ids.order(),
                    "dispute_id": None,
                    "method": "card",
                    "created_at": settled_at - timedelta(days=2),
                }
            )
            truth.expected_exceptions.append(
                {
                    "code": "ORPHAN_SETTLEMENT_LINE",
                    "subject_kind": "settlement_line",
                    "subject_id": ghost_id,
                    "residual_paise": ghost_amount - mdr - tax,
                    "defect": "orphan_settlement_line",
                }
            )

        settlement_id = ids.settlement()
        utr = ids.utr()
        net = sum(line["credit"] - line["debit"] for line in batch_lines)

        for line in batch_lines:
            line["settlement_id"] = settlement_id
            line["settlement_utr"] = utr
            line["settled_at"] = settled_at
            line["on_hold"] = False
            line["settled"] = True
            truth.line_to_batch[line["entity_id"]] = settlement_id

        batches.append(
            {
                "settlement_id": settlement_id,
                "settlement_utr": utr,
                "amount": net,
                "created_at": settled_at - timedelta(hours=3),
                "settled_at": settled_at,
                "status": "processed",
                "day": settle_day,
            }
        )
        lines.extend(batch_lines)

    # ------------------------------------------------------------------ #
    # 4. The bank statement. One lumped credit per batch — usually.
    # ------------------------------------------------------------------ #
    balance = 4_21_00_000  # ₹4.21 lakh opening balance
    last_settle_day = max(b["day"] for b in batches)

    ordered_batches = sorted(batches, key=lambda b: b["day"])
    # Everything except the final payout, which is still in flight.
    landed = [b for b in ordered_batches if b["day"] < last_settle_day]
    landed_ids = [b["settlement_id"] for b in landed]

    # Count-based again, and drawn from disjoint pools so one batch never
    # carries two different money defects at once — that would make the
    # ground-truth residual for either one wrong.
    short_credit_ids = set(
        rng.sample(landed_ids, k=_defect_count(cfg.defects.short_credit, len(landed_ids)))
    )
    rounding_pool = [sid for sid in landed_ids if sid not in short_credit_ids]
    rounding_ids = set(
        rng.sample(
            rounding_pool, k=_defect_count(cfg.defects.rounding_drift, len(rounding_pool))
        )
    )
    # Hiding the UTR is independent of the amount defects: it breaks the *link*,
    # not the money, so it may legitimately co-occur with either.
    hidden_utr_ids = set(
        rng.sample(
            landed_ids,
            k=_defect_count(cfg.defects.utr_missing_from_narration, len(landed_ids)),
        )
    )

    for batch in ordered_batches:
        # The most recent payout has been initiated but has not landed yet.
        # This is a timing difference, not an error, and the engine has to say so.
        if batch["day"] >= last_settle_day:
            truth.expected_exceptions.append(
                {
                    "code": "TIMING_DIFFERENCE",
                    "subject_kind": "settlement_batch",
                    "subject_id": batch["settlement_id"],
                    "residual_paise": batch["amount"],
                    "defect": "credit_not_yet_landed",
                }
            )
            continue

        credited = batch["amount"]
        defect = None

        if batch["settlement_id"] in short_credit_ids:
            # The bank took its own NEFT charge out of the credit. This is the
            # "unexplained deduction" merchants complain about: the payout is
            # right, the credit is short, and nothing links the difference.
            shortfall = rng.choice([1_180, 2_360, 590])
            credited -= shortfall
            defect = "short_credit"
            truth.expected_exceptions.append(
                {
                    "code": "SHORT_CREDIT",
                    "subject_kind": "settlement_batch",
                    "subject_id": batch["settlement_id"],
                    "residual_paise": shortfall,
                    "defect": "short_credit",
                }
            )
        elif batch["settlement_id"] in rounding_ids:
            credited -= 1  # one paisa
            defect = "rounding_drift"
            truth.expected_exceptions.append(
                {
                    "code": "ROUNDING",
                    "subject_kind": "settlement_batch",
                    "subject_id": batch["settlement_id"],
                    "residual_paise": 1,
                    "defect": "rounding_drift",
                }
            )

        balance += credited
        bank_ref = f"N{rng.randrange(10**11, 10**12 - 1)}"

        hide_utr = batch["settlement_id"] in hidden_utr_ids
        if hide_utr:
            # The narration shapes that actually defeat a plain UTR regex.
            narration = rng.choice(
                [
                    f"NEFT CR-{rng.choice(BANK_IFSC_PREFIXES)}0000{rng.randrange(100, 999)}-"
                    f"RAZORPAY SOFTWARE PRIVATE LIMITED-{cfg.merchant_name.upper()}",
                    f"NEFT INWARD RAZORPAY SOFTWARE PVT LTD SETTLEMENT "
                    f"{batch['settled_at'].strftime('%d%b').upper()}",
                    f"MB:NEFT:RAZORPAY SOFTWARE:PAYOUT:REF{rng.randrange(10**6, 10**7)}",
                ]
            )
            truth.expected_exceptions.append(
                {
                    "code": "UNMATCHED_BANK_CREDIT",
                    "subject_kind": "bank_txn",
                    "subject_id": bank_ref,
                    "residual_paise": 0,
                    "defect": "utr_missing_from_narration",
                }
            )
        else:
            narration = (
                f"NEFT-{batch['settlement_utr']}-RAZORPAY SOFTWARE PVT LTD-"
                f"{rng.choice(BANK_IFSC_PREFIXES)}0000{rng.randrange(100, 999)}-PAYOUT"
            )

        bank_rows.append(
            {
                "bank_ref": bank_ref,
                "value_date": batch["settled_at"],
                "narration": narration,
                "credit": credited,
                "debit": 0,
                "balance": balance,
                "settlement_id": batch["settlement_id"],
                "utr_hidden": hide_utr,
                "defect": defect,
            }
        )
        truth.batch_to_bank[batch["settlement_id"]] = bank_ref

    # Bank noise that has nothing to do with Razorpay. A recon tool that trips
    # over the merchant's electricity bill is not a recon tool.
    for _ in range(6):
        day = rng.randrange(1, cfg.n_days)
        amount = (rng.randrange(20_000, 900_000) // 100) * 100
        balance -= amount
        bank_rows.append(
            {
                "bank_ref": f"N{rng.randrange(10**11, 10**12 - 1)}",
                "value_date": start + timedelta(days=day, hours=15),
                "narration": rng.choice(
                    [
                        "UPI/DR/GSTPMT/GST PAYMENT/SBIN",
                        "ACH D- AWS INDIA CLOUD SERVICES",
                        "NEFT DR-VENDOR PAYOUT-PACKAGING SUPPLIES",
                        "BY CASH DEP MACHINE BRANCH KORAMANGALA",
                    ]
                ),
                "credit": 0,
                "debit": amount,
                "balance": balance,
                "settlement_id": None,
                "utr_hidden": False,
                "defect": "unrelated_bank_activity",
            }
        )

    bank_rows.sort(key=lambda r: r["value_date"])

    # ------------------------------------------------------------------ #
    # 5. Duplicate rows in the merchant's own export — a copy/paste artefact
    #    that shows up constantly in real spreadsheets.
    # ------------------------------------------------------------------ #
    ledger_rows = list(orders)
    for payment in orders:
        if rng.random() < cfg.defects.duplicate_order_row:
            ledger_rows.append(dict(payment))
            truth.expected_exceptions.append(
                {
                    "code": "DUPLICATE",
                    "subject_kind": "merchant_order",
                    "subject_id": payment["order_id"],
                    "residual_paise": payment["gross"],
                    "defect": "duplicate_order_row",
                }
            )
    rng.shuffle(ledger_rows)

    truth.stats = {
        "orders": len(ledger_rows),
        "settlement_lines": len(lines),
        "settlement_batches": len(batches),
        "bank_txns": len(bank_rows),
        "expected_exceptions": len(truth.expected_exceptions),
        "gross_paise": sum(o["gross"] for o in orders),
        "seed": cfg.seed,
    }

    _write_files(cfg, ledger_rows, lines, batches, bank_rows, truth)
    return truth


# --------------------------------------------------------------------------- #
# Writers. Each source deliberately uses its own vocabulary and date format —
# that heterogeneity is exactly what the schema-mapping agent exists to absorb.
# --------------------------------------------------------------------------- #


def _write_files(cfg, ledger_rows, lines, batches, bank_rows, truth: GroundTruth) -> None:
    out = Path(cfg.out_dir)
    out.mkdir(parents=True, exist_ok=True)

    # The merchant's books: non-canonical headers, DD-MM-YYYY dates.
    with (out / "merchant_ledger.csv").open("w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(
            [
                "Order Ref",
                "Txn ID",
                "Invoice #",
                "Customer",
                "Amount (INR)",
                "Mode",
                "Booked On",
                "Refund Amt",
            ]
        )
        for i, row in enumerate(ledger_rows, start=1):
            writer.writerow(
                [
                    row["order_id"],
                    row["payment_id"],
                    f"INV-2026-{i:05d}",
                    f"CUST{(i * 7919) % 90000 + 10000}",
                    f"{paise_to_rupees(row['gross']):,}",
                    row["method"].upper(),
                    row["captured_at"].strftime("%d-%m-%Y %H:%M"),
                    f"{paise_to_rupees(row['refunded'])}" if row["refunded"] else "",
                ]
            )

    # The gateway's recon report: Razorpay's own field names, ISO timestamps.
    with (out / "razorpay_settlement_recon.csv").open("w", newline="") as fh:
        writer = csv.DictWriter(
            fh,
            fieldnames=[
                "entity_id",
                "type",
                "debit",
                "credit",
                "amount",
                "currency",
                "fee",
                "tax",
                "on_hold",
                "settled",
                "created_at",
                "settled_at",
                "settlement_id",
                "settlement_utr",
                "credit_type",
                "payment_id",
                "order_id",
                "dispute_id",
                "method",
                "card_network",
                "card_issuer",
                "card_type",
                "description",
            ],
        )
        writer.writeheader()
        rng = random.Random(cfg.seed + 1)
        for line in lines:
            is_card = line["method"] == "card"
            writer.writerow(
                {
                    "entity_id": line["entity_id"],
                    "type": line["type"],
                    "debit": paise_to_rupees(line["debit"]),
                    "credit": paise_to_rupees(line["credit"]),
                    "amount": paise_to_rupees(line["amount"]),
                    "currency": "INR",
                    "fee": paise_to_rupees(line["fee"]),
                    "tax": paise_to_rupees(line["tax"]),
                    "on_hold": str(line["on_hold"]).lower(),
                    "settled": str(line["settled"]).lower(),
                    "created_at": line["created_at"].isoformat(),
                    "settled_at": line["settled_at"].isoformat(),
                    "settlement_id": line["settlement_id"],
                    "settlement_utr": line["settlement_utr"],
                    "credit_type": "default",
                    "payment_id": line["payment_id"] or "",
                    "order_id": line["order_id"] or "",
                    "dispute_id": line["dispute_id"] or "",
                    "method": line["method"],
                    "card_network": rng.choice(CARD_NETWORKS) if is_card else "",
                    "card_issuer": rng.choice(CARD_ISSUERS) if is_card else "",
                    "card_type": rng.choice(["credit", "debit"]) if is_card else "",
                    "description": "",
                }
            )

    with (out / "razorpay_settlements.csv").open("w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(
            ["settlement_id", "utr", "amount", "currency", "status", "created_at", "settled_at"]
        )
        for batch in batches:
            writer.writerow(
                [
                    batch["settlement_id"],
                    batch["settlement_utr"],
                    paise_to_rupees(batch["amount"]),
                    "INR",
                    batch["status"],
                    batch["created_at"].isoformat(),
                    batch["settled_at"].isoformat(),
                ]
            )

    # The bank: DD/MM/YY, Dr/Cr columns, everything interesting inside prose.
    with (out / "bank_statement.csv").open("w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(
            ["Txn Date", "Value Date", "Description", "Chq/Ref No", "Debit", "Credit", "Balance"]
        )
        for row in bank_rows:
            writer.writerow(
                [
                    row["value_date"].strftime("%d/%m/%y"),
                    row["value_date"].strftime("%d/%m/%y"),
                    row["narration"],
                    row["bank_ref"],
                    f"{paise_to_rupees(row['debit']):,}" if row["debit"] else "",
                    f"{paise_to_rupees(row['credit']):,}" if row["credit"] else "",
                    f"{paise_to_rupees(row['balance']):,}",
                ]
            )

    with (out / "ground_truth.json").open("w") as fh:
        json.dump(asdict(truth), fh, indent=2, default=str)
