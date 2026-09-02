"""Shared state for one reconciliation pass.

The tiers are plain functions over this context rather than methods on a big
object, so each one can be read — and tested — on its own.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from unnet.core.db import AuditLog
from unnet.core.models import (
    BankTxn,
    DecidedBy,
    EntityType,
    ExceptionCode,
    ExceptionStatus,
    Match,
    MatchTier,
    MerchantOrder,
    ReconException,
    SettlementBatch,
    SettlementLine,
)


@dataclass
class ReconContext:
    run_id: str
    orders: list[MerchantOrder] = field(default_factory=list)
    lines: list[SettlementLine] = field(default_factory=list)
    batches: list[SettlementBatch] = field(default_factory=list)
    bank_txns: list[BankTxn] = field(default_factory=list)

    audit: Optional[AuditLog] = None
    ai_enabled: bool = True

    matches: list[Match] = field(default_factory=list)
    exceptions: list[ReconException] = field(default_factory=list)

    #: Ids already consumed by a match, so nothing is matched twice.
    claimed: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))

    # ------------------------------------------------------------------ #
    # Indexes, built once after ingest.
    # ------------------------------------------------------------------ #
    orders_by_order_id: dict[str, list[MerchantOrder]] = field(default_factory=dict)
    orders_by_payment_id: dict[str, list[MerchantOrder]] = field(default_factory=dict)
    lines_by_settlement: dict[str, list[SettlementLine]] = field(default_factory=dict)
    lines_by_entity_id: dict[str, SettlementLine] = field(default_factory=dict)
    payment_lines_by_payment_id: dict[str, list[SettlementLine]] = field(default_factory=dict)
    batch_by_id: dict[str, SettlementBatch] = field(default_factory=dict)
    batch_by_utr: dict[str, list[SettlementBatch]] = field(default_factory=dict)

    def build_indexes(self) -> None:
        self.orders_by_order_id = defaultdict(list)
        self.orders_by_payment_id = defaultdict(list)
        for order in self.orders:
            self.orders_by_order_id[order.order_id].append(order)
            if order.payment_id:
                self.orders_by_payment_id[order.payment_id].append(order)

        self.lines_by_settlement = defaultdict(list)
        self.payment_lines_by_payment_id = defaultdict(list)
        self.lines_by_entity_id = {}
        for line in self.lines:
            self.lines_by_entity_id[line.entity_id] = line
            if line.settlement_id:
                self.lines_by_settlement[line.settlement_id].append(line)
            if line.type == EntityType.PAYMENT and line.payment_id:
                self.payment_lines_by_payment_id[line.payment_id].append(line)

        self.batch_by_id = {b.settlement_id: b for b in self.batches}
        self.batch_by_utr = defaultdict(list)
        for batch in self.batches:
            if batch.settlement_utr:
                self.batch_by_utr[batch.settlement_utr].append(batch)

    # ------------------------------------------------------------------ #
    # Business dates. A reconciliation is run *as of* a date, and ageing is
    # measured against that date rather than against the wall clock.
    # ------------------------------------------------------------------ #

    def as_of(self) -> Optional[datetime]:
        """The business date this run reconciles to.

        The latest date present in the source data, not ``now``. A recon closes
        a period: if you reconcile last Tuesday's files on Friday, every break
        is still aged from Tuesday, and re-running the same files next month
        must not make the same break a month older. Wall-clock ageing would
        also mean a fixed demo dataset drifts into a single "14d+" bucket and
        stays there, which is a symptom of the wrong clock, not of the data.
        """
        dates = [o.captured_at for o in self.orders]
        dates += [l.settled_at or l.created_at for l in self.lines]
        dates += [b.settled_at or b.created_at for b in self.batches]
        dates += [t.value_date for t in self.bank_txns]
        real = [d for d in dates if d is not None]
        return max(real) if real else None

    def occurred_at(self, subject_kind: str, subject_id: str) -> Optional[datetime]:
        """When the money event behind a case actually happened.

        Ageing has to run from here, not from the moment Unnet first noticed.
        A chargeback deducted a fortnight ago is a fortnight-old break; if it
        were aged from first sight, deploying the tool would silently reset
        every outstanding item to zero days — the same "resets each morning"
        failure that makes recon queues unreadable, one level up.
        """
        if subject_kind == "settlement_line":
            line = self.lines_by_entity_id.get(subject_id)
            return (line.settled_at or line.created_at) if line else None
        if subject_kind == "settlement_batch":
            batch = self.batch_by_id.get(subject_id)
            return (batch.settled_at or batch.created_at) if batch else None
        if subject_kind == "bank_txn":
            for txn in self.bank_txns:
                if txn.bank_ref == subject_id:
                    return txn.value_date
            return None
        if subject_kind == "merchant_order":
            # The subject may be either identifier depending on which side
            # raised the exception.
            found = self.orders_by_order_id.get(subject_id) or \
                self.orders_by_payment_id.get(subject_id)
            return min((o.captured_at for o in found), default=None) if found else None
        return None

    # ------------------------------------------------------------------ #
    # Recording. Everything the engine concludes goes through these two, so
    # nothing can reach the output without also reaching the audit trail.
    # ------------------------------------------------------------------ #

    def is_claimed(self, kind: str, ident: str) -> bool:
        return ident in self.claimed[kind]

    def record_match(
        self,
        *,
        tier: MatchTier,
        rule_id: str,
        left_kind: str,
        left_id: str,
        right_kind: str,
        right_id: str,
        amount_paise: int = 0,
        confidence: int = 1000,
        decided_by: DecidedBy = DecidedBy.RULE,
        evidence: dict | None = None,
    ) -> Match:
        match = Match(
            run_id=self.run_id,
            tier=tier,
            rule_id=rule_id,
            confidence=confidence,
            decided_by=decided_by,
            left_kind=left_kind,
            left_id=left_id,
            right_kind=right_kind,
            right_id=right_id,
            amount_paise=amount_paise,
            evidence=evidence or {},
        )
        self.matches.append(match)
        self.claimed[left_kind].add(left_id)
        self.claimed[right_kind].add(right_id)

        if self.audit:
            self.audit.record(
                stage=tier.value,
                subject_kind=left_kind,
                subject_id=left_id,
                decision=f"matched to {right_kind}:{right_id}",
                decided_by=decided_by,
                decider_ref=rule_id,
                confidence=confidence,
                evidence=evidence or {},
            )
        return match

    def record_exception(
        self,
        *,
        code: ExceptionCode,
        subject_kind: str,
        subject_id: str,
        summary: str,
        residual_paise: int = 0,
        status: ExceptionStatus = ExceptionStatus.OPEN,
        evidence: dict | None = None,
    ) -> ReconException:
        exception = ReconException(
            run_id=self.run_id,
            code=code,
            status=status,
            subject_kind=subject_kind,
            subject_id=subject_id,
            residual_paise=residual_paise,
            summary=summary,
            evidence=evidence or {},
        )
        self.exceptions.append(exception)

        if self.audit:
            self.audit.record(
                stage="exception",
                subject_kind=subject_kind,
                subject_id=subject_id,
                decision=f"{code.value}: {summary}",
                decided_by=DecidedBy.RULE,
                decider_ref=code.value,
                evidence=evidence or {},
            )
        return exception


def days_between(a: datetime | None, b: datetime | None) -> Optional[float]:
    if a is None or b is None:
        return None
    return abs((a - b).total_seconds()) / 86400.0
