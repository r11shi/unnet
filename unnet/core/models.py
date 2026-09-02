"""Canonical data model.

The settlement tables mirror the field names Razorpay's own settlement-recon API
returns (``entity_id``, ``type``, ``debit``, ``credit``, ``fee``, ``tax``,
``settlement_id``, ``settlement_utr``, ``on_hold`` ...). Staying on their
vocabulary means the synthetic fixtures and the optional live adapter in
``unnet/ingest/razorpay_live.py`` land in exactly the same shape, and a reader
who knows Razorpay's reports can read this schema without a translation table.
"""

from __future__ import annotations

import enum
from datetime import datetime
from typing import Optional

from sqlmodel import JSON, Column, Field, SQLModel


class EntityType(str, enum.Enum):
    """``type`` on a settlement-recon line."""

    PAYMENT = "payment"
    REFUND = "refund"
    ADJUSTMENT = "adjustment"
    DISPUTE = "dispute"
    TRANSFER = "transfer"


class ExceptionCode(str, enum.Enum):
    """The honest list: every way a rupee can fail to reconcile.

    Ordered roughly by where in the pipeline it surfaces.
    """

    SCHEMA_UNPARSEABLE = "SCHEMA_UNPARSEABLE"
    UNMATCHED_BANK_CREDIT = "UNMATCHED_BANK_CREDIT"
    MISSING_BANK_CREDIT = "MISSING_BANK_CREDIT"
    SHORT_CREDIT = "SHORT_CREDIT"
    OVER_CREDIT = "OVER_CREDIT"
    ORPHAN_SETTLEMENT_LINE = "ORPHAN_SETTLEMENT_LINE"
    UNSETTLED_ORDER = "UNSETTLED_ORDER"
    ON_HOLD = "ON_HOLD"
    FEE_MISMATCH = "FEE_MISMATCH"
    GST_MISMATCH = "GST_MISMATCH"
    REFUND_WITHOUT_ORIGINAL = "REFUND_WITHOUT_ORIGINAL"
    PARTIAL_REFUND_SPLIT = "PARTIAL_REFUND_SPLIT"
    CHARGEBACK_DEDUCTION = "CHARGEBACK_DEDUCTION"
    DUPLICATE = "DUPLICATE"
    TIMING_DIFFERENCE = "TIMING_DIFFERENCE"
    ROUNDING = "ROUNDING"


class ExceptionStatus(str, enum.Enum):
    OPEN = "open"
    #: Closed by a deterministic rule with no human or model involvement.
    AUTO_RESOLVED = "auto_resolved"
    #: Every component traced back to a ledger row. Closed automatically.
    AI_RESOLVED = "ai_resolved"
    #: Arithmetic holds but rests on something unevidenced. A human decides;
    #: never counted as a resolution.
    AI_HYPOTHESIS = "ai_hypothesis"
    #: The model proposed something the verifier rejected. Stays in the queue.
    AI_REJECTED = "ai_rejected"
    #: A timing break, carried into the next run rather than reported as an error.
    ROLLED_FORWARD = "rolled_forward"
    ACCEPTED_BY_HUMAN = "accepted_by_human"
    REJECTED_BY_HUMAN = "rejected_by_human"


class MatchTier(str, enum.Enum):
    #: Bank credit <-> settlement batch.
    TIER1_BANK_TO_BATCH = "tier1_bank_to_batch"
    #: Settlement line <-> merchant order.
    TIER2_LINE_TO_ORDER = "tier2_line_to_order"
    #: Refund / dispute line <-> the payment it reverses.
    TIER3_REVERSAL_TO_PAYMENT = "tier3_reversal_to_payment"


class DecidedBy(str, enum.Enum):
    RULE = "rule"
    MODEL = "model"
    HUMAN = "human"


# --------------------------------------------------------------------------- #
# Source-of-truth tables. One row per record in each of the three inputs.
# --------------------------------------------------------------------------- #


class MerchantOrder(SQLModel, table=True):
    """A row from the merchant's own books — what they believe they sold."""

    __tablename__ = "merchant_order"

    id: Optional[int] = Field(default=None, primary_key=True)
    run_id: str = Field(index=True)

    order_id: str = Field(index=True)
    #: The merchant's ledger may or may not carry the gateway's payment id.
    payment_id: Optional[str] = Field(default=None, index=True)
    invoice_no: Optional[str] = None
    customer_ref: Optional[str] = None

    gross_paise: int
    currency: str = "INR"
    method: Optional[str] = None
    captured_at: datetime = Field(index=True)

    #: Whether the merchant's books already record a refund against this order.
    refunded_paise: int = 0


class SettlementLine(SQLModel, table=True):
    """A row from the Razorpay settlement-recon report."""

    __tablename__ = "settlement_line"

    id: Optional[int] = Field(default=None, primary_key=True)
    run_id: str = Field(index=True)

    entity_id: str = Field(index=True)
    type: EntityType = Field(index=True)

    #: Razorpay reports movement as debit/credit; ``amount`` is the gross value.
    debit_paise: int = 0
    credit_paise: int = 0
    amount_paise: int = 0
    currency: str = "INR"

    fee_paise: int = 0
    tax_paise: int = 0

    on_hold: bool = False
    settled: bool = True
    credit_type: str = "default"

    created_at: datetime = Field(index=True)
    settled_at: Optional[datetime] = Field(default=None, index=True)

    settlement_id: Optional[str] = Field(default=None, index=True)
    settlement_utr: Optional[str] = Field(default=None, index=True)

    payment_id: Optional[str] = Field(default=None, index=True)
    order_id: Optional[str] = Field(default=None, index=True)
    dispute_id: Optional[str] = Field(default=None, index=True)

    method: Optional[str] = None
    card_network: Optional[str] = None
    card_issuer: Optional[str] = None
    card_type: Optional[str] = None

    description: Optional[str] = None

    @property
    def net_paise(self) -> int:
        """Signed contribution of this line to the payout."""
        return self.credit_paise - self.debit_paise


class SettlementBatch(SQLModel, table=True):
    """One payout instruction: many settlement lines netted into one transfer."""

    __tablename__ = "settlement_batch"

    id: Optional[int] = Field(default=None, primary_key=True)
    run_id: str = Field(index=True)

    settlement_id: str = Field(index=True)
    settlement_utr: Optional[str] = Field(default=None, index=True)
    #: What Razorpay says it sent.
    reported_amount_paise: int
    currency: str = "INR"
    status: str = "processed"
    created_at: datetime
    settled_at: Optional[datetime] = None


class BankTxn(SQLModel, table=True):
    """A line from the merchant's bank statement."""

    __tablename__ = "bank_txn"

    id: Optional[int] = Field(default=None, primary_key=True)
    run_id: str = Field(index=True)

    bank_ref: str = Field(index=True)
    value_date: datetime = Field(index=True)
    #: Free text. The UTR is usually in here somewhere, and sometimes it is not.
    narration: str
    credit_paise: int = 0
    debit_paise: int = 0
    balance_paise: Optional[int] = None

    #: Filled by the narration parser (regex first, model only on a miss).
    extracted_utr: Optional[str] = Field(default=None, index=True)
    utr_source: Optional[str] = None  # "regex" | "model" | None


# --------------------------------------------------------------------------- #
# Engine output.
# --------------------------------------------------------------------------- #


class Match(SQLModel, table=True):
    """A link the engine is willing to stand behind, with its evidence."""

    __tablename__ = "match"

    id: Optional[int] = Field(default=None, primary_key=True)
    run_id: str = Field(index=True)

    tier: MatchTier = Field(index=True)
    rule_id: str = Field(index=True)
    #: 1000 == certain. Kept as an int so match scores never drift on a float.
    confidence: int = 1000
    decided_by: DecidedBy = DecidedBy.RULE

    left_kind: str
    left_id: str = Field(index=True)
    right_kind: str
    right_id: str = Field(index=True)

    amount_paise: int = 0
    evidence: dict = Field(default_factory=dict, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=datetime.utcnow)


class ReconException(SQLModel, table=True):
    """Something the engine could not close. Never silently absorbed."""

    __tablename__ = "recon_exception"

    id: Optional[int] = Field(default=None, primary_key=True)
    run_id: str = Field(index=True)

    code: ExceptionCode = Field(index=True)
    status: ExceptionStatus = Field(default=ExceptionStatus.OPEN, index=True)

    subject_kind: str
    subject_id: str = Field(index=True)
    #: Signed rupee impact in paise. Positive means money we cannot account for.
    residual_paise: int = 0

    summary: str
    evidence: dict = Field(default_factory=dict, sa_column=Column(JSON))

    #: What the model proposed, and what the verifier made of it.
    proposal: Optional[dict] = Field(default=None, sa_column=Column(JSON))
    verifier_verdict: Optional[str] = None
    verifier_reason: Optional[str] = None

    created_at: datetime = Field(default_factory=datetime.utcnow)


class AuditEntry(SQLModel, table=True):
    """Append-only. Every decision the system made and who made it."""

    __tablename__ = "recon_audit"

    id: Optional[int] = Field(default=None, primary_key=True)
    run_id: str = Field(index=True)
    seq: int = Field(index=True)

    stage: str = Field(index=True)
    subject_kind: str
    subject_id: str = Field(index=True)
    decision: str

    decided_by: DecidedBy
    #: A rule id, or ``model:prompt_hash`` so a model decision is reproducible.
    decider_ref: str
    confidence: int = 1000

    evidence: dict = Field(default_factory=dict, sa_column=Column(JSON))
    verifier_result: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)


class Run(SQLModel, table=True):
    """One reconciliation pass, and the headline numbers it produced."""

    __tablename__ = "run"

    id: Optional[int] = Field(default=None, primary_key=True)
    run_id: str = Field(index=True, unique=True)

    label: str = ""
    #: False for the ablation baseline, so the two runs are comparable.
    ai_enabled: bool = True
    dataset: str = ""

    started_at: datetime = Field(default_factory=datetime.utcnow)
    finished_at: Optional[datetime] = None
    duration_ms: int = 0

    orders_count: int = 0
    settlement_lines_count: int = 0
    bank_txns_count: int = 0

    matched_count: int = 0
    exceptions_open: int = 0
    exceptions_ai_resolved: int = 0
    exceptions_ai_rejected: int = 0

    value_reconciled_paise: int = 0
    value_in_exceptions_paise: int = 0

    llm_calls: int = 0
    llm_degraded: bool = False
    notes: dict = Field(default_factory=dict, sa_column=Column(JSON))


class CaseFileRow(SQLModel, table=True):
    """A routed, trackable piece of work — persisted so the loop can close.

    Keyed by ``case_key`` rather than by row id, because every run re-parses the
    source files from scratch. Identity has to come from what the problem *is*,
    or run 2 raises the same 130 problems as run 1 and nothing is ever closed.
    """

    __tablename__ = "case_file"

    id: Optional[int] = Field(default=None, primary_key=True)
    run_id: str = Field(index=True)
    case_key: str = Field(index=True)

    code: str = Field(index=True)
    subject_kind: str
    subject_id: str = Field(index=True)

    owner: str = Field(index=True)
    impact: str = Field(index=True)
    action: str
    message: str
    amount_paise: int = 0

    #: detected | investigating | routed | awaiting_action | resolved
    status: str = Field(default="detected", index=True)
    #: P1 | P2 | P3, derived arithmetically in engine/lifecycle.py
    priority: str = Field(default="P3", index=True)

    evidence: dict = Field(default_factory=dict, sa_column=Column(JSON))
    #: The model's unverified explanation, when there is one. Carried so the
    #: human who picks this up starts from a specific, checkable guess.
    hypothesis: Optional[dict] = Field(default=None, sa_column=Column(JSON))

    first_seen_run: str = ""
    last_seen_run: str = ""
    resolved_run: str = ""
    resolved_note: str = ""

    #: Real timestamps, because ageing is the prioritisation signal on an ops
    #: queue and a run id carries no time. first_seen_at survives across runs;
    #: last_seen_at moves each time the case is still present.
    first_seen_at: datetime = Field(default_factory=datetime.utcnow, index=True)
    last_seen_at: datetime = Field(default_factory=datetime.utcnow)

    #: The business dates ageing is actually computed from: when the money
    #: event happened, and the date the run reconciles to. Kept separate from
    #: first_seen_at rather than folded into it, so the record still says
    #: honestly both when the break happened and when Unnet first saw it.
    occurred_at: Optional[datetime] = Field(default=None, index=True)
    as_of: Optional[datetime] = Field(default=None)

    created_at: datetime = Field(default_factory=datetime.utcnow)


class CaseEvent(SQLModel, table=True):
    """One thing that happened to a case.

    This is what makes a case operational work rather than an error row: an
    analyst picking it up can see it was detected on Tuesday, that a model
    proposed an explanation the verifier would not accept, that it was routed to
    the bank on Thursday, and that nobody has chased it since.

    Append-only, like the audit trail, and for the same reason — the history of
    a financial item is part of the item.
    """

    __tablename__ = "case_event"

    id: Optional[int] = Field(default=None, primary_key=True)
    case_key: str = Field(index=True)
    run_id: str = Field(default="", index=True)

    #: rule | model | human
    actor: DecidedBy = DecidedBy.RULE
    #: detected | status_changed | proposed | verified | routed | resolved | note
    kind: str = Field(index=True)
    note: str = ""

    from_status: str = ""
    to_status: str = ""

    detail: dict = Field(default_factory=dict, sa_column=Column(JSON))
    at: datetime = Field(default_factory=datetime.utcnow, index=True)
