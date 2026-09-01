"""The reconciliation run, end to end."""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

from unnet.agents import resolvers
from unnet.core.db import AuditLog
from unnet.core.models import DecidedBy, ExceptionStatus, Run
from unnet.engine import netting, tier1, tier2, tier3
from unnet.engine.context import ReconContext
from unnet.ingest import loaders
from unnet.ingest.mapping import MappingSpec, SourceKind, heuristic_map, validate_spec


@dataclass
class SourcePaths:
    merchant_ledger: Path
    settlement_recon: Path
    settlements: Path
    bank_statement: Path

    @classmethod
    def synthetic(cls, root: Path | str = "data/synthetic") -> "SourcePaths":
        base = Path(root)
        return cls(
            merchant_ledger=base / "merchant_ledger.csv",
            settlement_recon=base / "razorpay_settlement_recon.csv",
            settlements=base / "razorpay_settlements.csv",
            bank_statement=base / "bank_statement.csv",
        )


@dataclass
class ReconResult:
    ctx: ReconContext
    run: Run
    netting: netting.NettingResult
    specs: dict[str, MappingSpec] = field(default_factory=dict)


def reconcile(
    paths: SourcePaths,
    *,
    run_id: Optional[str] = None,
    audit: Optional[AuditLog] = None,
    ai_enabled: bool = True,
    label: str = "",
    llm_client=None,
) -> ReconResult:
    """Run one pass.

    With ``ai_enabled=False`` no agent is constructed and no model is consulted,
    which is exactly what the ablation baseline needs: the two runs differ only
    by this flag, so any difference in the numbers is attributable to the model
    layer and nothing else.
    """
    run_id = run_id or uuid.uuid4().hex[:12]
    started = time.perf_counter()

    ctx = ReconContext(run_id=run_id, audit=audit, ai_enabled=ai_enabled)
    specs: dict[str, MappingSpec] = {}

    mapper = None
    triage_agent = None
    if ai_enabled and llm_client is not None:
        from unnet.agents.mapper import ModelSchemaMapper
        from unnet.agents.triage import TriageAgent

        mapper = ModelSchemaMapper(llm_client)
        triage_agent = TriageAgent(llm_client)

    sources = [
        (SourceKind.MERCHANT_LEDGER, paths.merchant_ledger),
        (SourceKind.SETTLEMENT_RECON, paths.settlement_recon),
        (SourceKind.SETTLEMENTS, paths.settlements),
        (SourceKind.BANK_STATEMENT, paths.bank_statement),
    ]

    loaded: dict[str, list[dict[str, str]]] = {}
    for kind, path in sources:
        headers, rows = loaders.read_csv(path)
        spec = heuristic_map(headers, kind)
        report = validate_spec(spec, rows)

        if not report.ok and mapper is not None:
            # The heuristic could not name every required column. This is the
            # one place a model genuinely beats a rule: arbitrary headers in a
            # file nobody has seen before. Whatever it proposes is re-validated
            # below, and a proposal that does not parse is discarded.
            proposed = mapper.propose(kind, headers, rows[:5])
            if proposed is not None:
                proposed_report = validate_spec(proposed, rows)
                if proposed_report.ok:
                    spec, report = proposed, proposed_report

        specs[kind] = spec
        loaded[kind] = rows

        if audit:
            audit.record(
                stage="ingest",
                subject_kind="source",
                subject_id=kind,
                decision=f"mapped {len(spec.columns)} columns via {spec.produced_by}",
                decided_by=(
                    DecidedBy.MODEL if spec.produced_by.startswith("model") else DecidedBy.RULE
                ),
                decider_ref=spec.produced_by,
                confidence=spec.confidence,
                evidence={
                    "path": str(path),
                    "headers": headers,
                    "columns": spec.columns,
                    "rows": len(rows),
                },
                verifier_result="ok" if report.ok else report.reason,
            )

    ctx.orders = loaders.load_merchant_orders(
        loaded[SourceKind.MERCHANT_LEDGER], specs[SourceKind.MERCHANT_LEDGER], run_id
    )
    ctx.lines = loaders.load_settlement_lines(
        loaded[SourceKind.SETTLEMENT_RECON], specs[SourceKind.SETTLEMENT_RECON], run_id
    )
    ctx.batches = loaders.load_settlement_batches(
        loaded[SourceKind.SETTLEMENTS], specs[SourceKind.SETTLEMENTS], run_id
    )
    ctx.bank_txns = loaders.load_bank_txns(
        loaded[SourceKind.BANK_STATEMENT], specs[SourceKind.BANK_STATEMENT], run_id
    )
    ctx.build_indexes()

    tier1.run(ctx)
    tier2.run(ctx)
    tier3.run(ctx)

    # Exact search runs before any model is consulted. If arithmetic can close
    # an exception, arithmetic closes it.
    resolvers.subset_sum_resolve(ctx)

    # Only now, on what is left, is a model worth the call.
    if triage_agent is not None:
        triage_agent.run(ctx)

    netting_result = netting.run(ctx)

    duration_ms = int((time.perf_counter() - started) * 1000)
    run = _summarise(ctx, netting_result, duration_ms, ai_enabled=ai_enabled, label=label)

    # The un-netting waterfall is stored with the run rather than recomputed on
    # request: it is small, it is what the dashboard opens with, and persisting
    # it means the chart shows what this run actually concluded rather than what
    # a later recomputation would conclude from mutated data.
    run.notes = {
        **run.notes,
        "breakdowns": [
            {
                "settlement_id": b.settlement_id,
                "settlement_utr": b.settlement_utr,
                "settled_at": b.settled_at,
                "gross_paise": b.gross_paise,
                "mdr_paise": b.mdr_paise,
                "gst_paise": b.gst_paise,
                "refunds_paise": b.refunds_paise,
                "disputes_paise": b.disputes_paise,
                "dispute_fees_paise": b.dispute_fees_paise,
                "adjustments_paise": b.adjustments_paise,
                "transfers_paise": b.transfers_paise,
                "computed_net_paise": b.computed_net_paise,
                "dual_net_paise": b.dual_net_paise,
                "reported_net_paise": b.reported_net_paise,
                "bank_credit_paise": b.bank_credit_paise,
                "bank_residual_paise": b.bank_residual_paise,
                "internally_consistent": b.internally_consistent,
                "line_count": b.line_count,
                "payment_count": b.payment_count,
                "refund_count": b.refund_count,
                "dispute_count": b.dispute_count,
                "waterfall": b.waterfall(),
            }
            for b in netting_result.breakdowns
        ],
    }

    if llm_client is not None:
        stats = llm_client.stats()
        run.llm_calls = stats["calls"]
        run.llm_degraded = stats["degraded"]
        run.notes = {
            **run.notes,
            "llm": stats,
            "triage": (
                {
                    "attempted": triage_agent.attempted,
                    "proposed": triage_agent.proposed,
                    # Auto-closed: every component traced to a ledger row.
                    "resolved_verified": triage_agent.accepted,
                    # Sums exactly but rests on an invented component. Useful to
                    # a human, never counted as a resolution.
                    "hypotheses": triage_agent.hypotheses,
                    "rejected_by_verifier": triage_agent.rejected,
                }
                if triage_agent
                else {}
            ),
        }

    return ReconResult(ctx=ctx, run=run, netting=netting_result, specs=specs)


def _summarise(
    ctx: ReconContext,
    netting_result: netting.NettingResult,
    duration_ms: int,
    *,
    ai_enabled: bool,
    label: str,
) -> Run:
    open_states = {ExceptionStatus.OPEN, ExceptionStatus.AI_REJECTED}
    open_exceptions = [e for e in ctx.exceptions if e.status in open_states]

    # Value reconciled is the gross that made it into a match, not the count of
    # matches: 1,000 matched ₹10 orders and one missed ₹10 lakh order is not a
    # 99.9% result in any sense a finance team cares about.
    matched_orders = ctx.claimed["merchant_order"]
    value_reconciled = sum(
        o.gross_paise for o in ctx.orders if o.order_id in matched_orders
    )
    value_in_exceptions = sum(abs(e.residual_paise) for e in open_exceptions)

    return Run(
        run_id=ctx.run_id,
        label=label,
        ai_enabled=ai_enabled,
        started_at=datetime.utcnow(),
        finished_at=datetime.utcnow(),
        duration_ms=duration_ms,
        orders_count=len(ctx.orders),
        settlement_lines_count=len(ctx.lines),
        bank_txns_count=len(ctx.bank_txns),
        matched_count=len(ctx.matches),
        exceptions_open=len(open_exceptions),
        exceptions_ai_resolved=sum(
            1 for e in ctx.exceptions if e.status == ExceptionStatus.AI_RESOLVED
        ),
        exceptions_ai_rejected=sum(
            1 for e in ctx.exceptions if e.status == ExceptionStatus.AI_REJECTED
        ),
        value_reconciled_paise=value_reconciled,
        value_in_exceptions_paise=value_in_exceptions,
        notes={
            "batches": len(netting_result.breakdowns),
            "batches_internally_inconsistent": sum(
                1 for b in netting_result.breakdowns if not b.internally_consistent
            ),
        },
    )
