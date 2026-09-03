"""FastAPI app: read-only views over a completed run, plus two write paths.

The two writes are an analyst accepting or rejecting an exception, and asking a
question. Both append to the audit trail. Nothing here recomputes a
reconciliation — the run is the record, and the dashboard reads it.
"""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlmodel import Session, select

from unnet.api.auth import require_write, writes_enabled
from unnet.core.db import AuditLog, make_engine
from unnet.core.models import (
    AuditEntry,
    BankTxn,
    DecidedBy,
    ExceptionStatus,
    Match,
    MerchantOrder,
    ReconException,
    Run,
    SettlementBatch,
    SettlementLine,
)
from unnet.core.money import format_inr
from unnet.engine import casefile

app = FastAPI(title="Unnet", version="0.1.0")
engine = make_engine(os.environ.get("UNNET_DB", "data/unnet.db"))

# The dashboard is one static file with no build step. A reviewer needs Python
# and nothing else — no node, no npm install, no committed bundle to distrust.
WEB_DIR = Path(__file__).resolve().parents[2] / "web"
WEB_INDEX = WEB_DIR / "index.html"


def _session() -> Session:
    return Session(engine, expire_on_commit=False)


def _latest_run_id(session: Session) -> str:
    run = session.exec(select(Run).order_by(Run.id.desc())).first()
    if run is None:
        raise HTTPException(404, "No runs yet. Run `make recon` first.")
    return run.run_id


def _resolve(session: Session, run_id: str | None) -> str:
    return run_id or _latest_run_id(session)


# --------------------------------------------------------------------------- #
# Runs
# --------------------------------------------------------------------------- #


@app.get("/api/runs")
def list_runs(limit: int = Query(30, le=100)):
    """Runs newest first, each with a delta against the one before it.

    Change over time is the thing a single run cannot show: whether the match
    rate is holding, whether the exception queue is draining or growing, and
    how many cases were carried forward rather than raised fresh. The deltas
    are computed here so the client never has to align two rows itself.
    """
    with _session() as session:
        runs = session.exec(select(Run).order_by(Run.id.desc()).limit(limit + 1)).all()

        rows = []
        for index, run in enumerate(runs[:limit]):
            previous = runs[index + 1] if index + 1 < len(runs) else None
            notes = run.notes or {}
            cases = notes.get("cases", {})
            records = (
                run.orders_count + run.settlement_lines_count + run.bank_txns_count
            )
            matched = run.matched_count
            match_rate = matched / records if records else 0.0

            def delta(field: str, current):
                if previous is None:
                    return None
                return current - getattr(previous, field)

            rows.append(
                {
                    "run_id": run.run_id,
                    "label": run.label,
                    "ai_enabled": run.ai_enabled,
                    "started_at": run.started_at,
                    "duration_ms": run.duration_ms,
                    "duration_delta": delta("duration_ms", run.duration_ms),
                    "records": records,
                    "matched_count": matched,
                    "matched_delta": delta("matched_count", matched),
                    "match_rate": match_rate,
                    "exceptions_open": run.exceptions_open,
                    "exceptions_delta": delta("exceptions_open", run.exceptions_open),
                    "value_reconciled_paise": run.value_reconciled_paise,
                    "value_in_exceptions_paise": run.value_in_exceptions_paise,
                    "open_cases": cases.get("open_cases", 0),
                    "resolved_cases": cases.get("resolved_cases", 0),
                    "llm_calls": run.llm_calls,
                    "llm_degraded": run.llm_degraded,
                }
            )
        return {"items": rows}


@app.get("/api/summary")
def summary(run_id: str | None = None):
    """Everything the dashboard's headline tiles need, in one call."""
    with _session() as session:
        rid = _resolve(session, run_id)
        run = session.exec(select(Run).where(Run.run_id == rid)).first()
        if run is None:
            raise HTTPException(404, f"No run {rid}")

        exceptions = session.exec(
            select(ReconException).where(ReconException.run_id == rid)
        ).all()

        by_status: dict[str, int] = {}
        by_code: dict[str, dict] = {}
        for exception in exceptions:
            by_status[exception.status.value] = by_status.get(exception.status.value, 0) + 1
            entry = by_code.setdefault(
                exception.code.value, {"code": exception.code.value, "open": 0, "resolved": 0, "value_paise": 0}
            )
            if exception.status in {ExceptionStatus.OPEN, ExceptionStatus.AI_REJECTED}:
                entry["open"] += 1
                entry["value_paise"] += abs(exception.residual_paise)
            else:
                entry["resolved"] += 1

        matches = session.exec(select(Match).where(Match.run_id == rid)).all()
        by_rule: dict[str, int] = {}
        by_decider: dict[str, int] = {}
        for match in matches:
            by_rule[match.rule_id] = by_rule.get(match.rule_id, 0) + 1
            by_decider[match.decided_by.value] = by_decider.get(match.decided_by.value, 0) + 1

        gross = sum(
            o.gross_paise
            for o in session.exec(
                select(MerchantOrder).where(MerchantOrder.run_id == rid)
            ).all()
        )

        return {
            "run": {
                "run_id": run.run_id,
                "label": run.label,
                "ai_enabled": run.ai_enabled,
                "duration_ms": run.duration_ms,
                "orders_count": run.orders_count,
                "settlement_lines_count": run.settlement_lines_count,
                "bank_txns_count": run.bank_txns_count,
                "matched_count": run.matched_count,
                "exceptions_open": run.exceptions_open,
                "value_reconciled_paise": run.value_reconciled_paise,
                "value_in_exceptions_paise": run.value_in_exceptions_paise,
                "gross_paise": gross,
                "llm_calls": run.llm_calls,
                "llm_degraded": run.llm_degraded,
            },
            "exceptions_by_status": by_status,
            "exceptions_by_code": sorted(
                by_code.values(), key=lambda e: (-e["open"], e["code"])
            ),
            "matches_by_rule": [
                {"rule_id": k, "count": v} for k, v in sorted(by_rule.items())
            ],
            "matches_by_decider": by_decider,
            "notes": run.notes,
        }


@app.get("/api/waterfall")
def waterfall(run_id: str | None = None):
    """Per-payout un-netting: gross, every deduction, and the bank credit."""
    with _session() as session:
        rid = _resolve(session, run_id)
        run = session.exec(select(Run).where(Run.run_id == rid)).first()
        if run is None:
            raise HTTPException(404, f"No run {rid}")
        return {"run_id": rid, "batches": (run.notes or {}).get("breakdowns", [])}


# --------------------------------------------------------------------------- #
# Matches and exceptions
# --------------------------------------------------------------------------- #


@app.get("/api/matches")
def list_matches(
    run_id: str | None = None,
    tier: str | None = None,
    rule_id: str | None = None,
    decided_by: str | None = None,
    max_confidence: int | None = None,
    q: str | None = None,
    limit: int = Query(100, le=500),
    offset: int = 0,
):
    with _session() as session:
        rid = _resolve(session, run_id)
        statement = select(Match).where(Match.run_id == rid)
        if tier:
            statement = statement.where(Match.tier == tier)
        if rule_id:
            statement = statement.where(Match.rule_id == rule_id)
        if decided_by:
            statement = statement.where(Match.decided_by == decided_by)
        if max_confidence is not None:
            statement = statement.where(Match.confidence <= max_confidence)

        rows = session.exec(statement).all()
        if q:
            needle = q.lower()
            rows = [
                m
                for m in rows
                if needle in m.left_id.lower() or needle in m.right_id.lower()
            ]

        total = len(rows)
        page = rows[offset : offset + limit]
        return {
            "total": total,
            "items": [
                {
                    "id": m.id,
                    "tier": m.tier.value,
                    "rule_id": m.rule_id,
                    "confidence": m.confidence,
                    "decided_by": m.decided_by.value,
                    "left_kind": m.left_kind,
                    "left_id": m.left_id,
                    "right_kind": m.right_kind,
                    "right_id": m.right_id,
                    "amount_paise": m.amount_paise,
                    "amount_display": format_inr(m.amount_paise),
                    "evidence": m.evidence,
                }
                for m in page
            ],
        }


@app.get("/api/exceptions")
def list_exceptions(
    run_id: str | None = None,
    code: str | None = None,
    status: str | None = None,
    limit: int = Query(200, le=1000),
    offset: int = 0,
):
    with _session() as session:
        rid = _resolve(session, run_id)
        statement = select(ReconException).where(ReconException.run_id == rid)
        if code:
            statement = statement.where(ReconException.code == code)
        if status:
            statement = statement.where(ReconException.status == status)

        rows = session.exec(statement).all()
        rows.sort(key=lambda e: (-abs(e.residual_paise), e.code.value))
        total = len(rows)
        page = rows[offset : offset + limit]

        return {
            "total": total,
            "total_value_paise": sum(abs(e.residual_paise) for e in rows),
            "items": [
                {
                    "id": e.id,
                    "code": e.code.value,
                    "status": e.status.value,
                    "subject_kind": e.subject_kind,
                    "subject_id": e.subject_id,
                    "residual_paise": e.residual_paise,
                    "residual_display": format_inr(e.residual_paise),
                    "summary": e.summary,
                    "evidence": e.evidence,
                    "proposal": e.proposal,
                    "verifier_verdict": e.verifier_verdict,
                    "verifier_reason": e.verifier_reason,
                }
                for e in page
            ],
        }


class Decision(BaseModel):
    accept: bool
    note: str = ""


@app.post("/api/exceptions/{exception_id}/decision")
def decide(
    exception_id: int, decision: Decision, actor: str = Depends(require_write)
):
    """An analyst's call. Appends to the audit trail; never edits history."""
    with _session() as session:
        exception = session.get(ReconException, exception_id)
        if exception is None:
            raise HTTPException(404, f"No exception {exception_id}")

        exception.status = (
            ExceptionStatus.ACCEPTED_BY_HUMAN
            if decision.accept
            else ExceptionStatus.REJECTED_BY_HUMAN
        )
        session.add(exception)

        audit = AuditLog(session, exception.run_id)
        audit.record(
            stage="human_review",
            subject_kind=exception.subject_kind,
            subject_id=exception.subject_id,
            decision=f"{'accepted' if decision.accept else 'rejected'}: {exception.code.value}",
            decided_by=DecidedBy.HUMAN,
            decider_ref=f"dashboard:{actor}",
            evidence={"note": decision.note, "exception_id": exception_id},
        )
        session.commit()
        return {"ok": True, "status": exception.status.value}


# --------------------------------------------------------------------------- #
# Audit
# --------------------------------------------------------------------------- #


@app.get("/api/audit")
def audit_trail(
    run_id: str | None = None,
    stage: str | None = None,
    decided_by: str | None = None,
    limit: int = Query(200, le=2000),
    offset: int = 0,
):
    with _session() as session:
        rid = _resolve(session, run_id)
        statement = select(AuditEntry).where(AuditEntry.run_id == rid)
        if stage:
            statement = statement.where(AuditEntry.stage == stage)
        if decided_by:
            statement = statement.where(AuditEntry.decided_by == decided_by)

        rows = session.exec(statement.order_by(AuditEntry.seq)).all()
        total = len(rows)
        page = rows[offset : offset + limit]
        return {
            "total": total,
            "items": [
                {
                    "seq": a.seq,
                    "stage": a.stage,
                    "subject_kind": a.subject_kind,
                    "subject_id": a.subject_id,
                    "decision": a.decision,
                    "decided_by": a.decided_by.value,
                    "decider_ref": a.decider_ref,
                    "confidence": a.confidence,
                    "verifier_result": a.verifier_result,
                    "evidence": a.evidence,
                    "created_at": a.created_at,
                }
                for a in page
            ],
        }


@app.get("/api/batch/{settlement_id}")
def batch_detail(settlement_id: str, run_id: str | None = None):
    """Everything that went into one payout, for the drill-down."""
    with _session() as session:
        rid = _resolve(session, run_id)
        batch = session.exec(
            select(SettlementBatch)
            .where(SettlementBatch.run_id == rid)
            .where(SettlementBatch.settlement_id == settlement_id)
        ).first()
        if batch is None:
            raise HTTPException(404, f"No settlement {settlement_id}")

        lines = session.exec(
            select(SettlementLine)
            .where(SettlementLine.run_id == rid)
            .where(SettlementLine.settlement_id == settlement_id)
        ).all()

        credit_match = session.exec(
            select(Match)
            .where(Match.run_id == rid)
            .where(Match.right_id == settlement_id)
            .where(Match.left_kind == "bank_txn")
        ).first()

        bank = None
        if credit_match:
            bank = session.exec(
                select(BankTxn)
                .where(BankTxn.run_id == rid)
                .where(BankTxn.bank_ref == credit_match.left_id)
            ).first()

        return {
            "settlement_id": batch.settlement_id,
            "settlement_utr": batch.settlement_utr,
            "reported_amount_paise": batch.reported_amount_paise,
            "settled_at": batch.settled_at,
            "bank": (
                {
                    "bank_ref": bank.bank_ref,
                    "narration": bank.narration,
                    "credit_paise": bank.credit_paise,
                    "value_date": bank.value_date,
                    "extracted_utr": bank.extracted_utr,
                    "utr_source": bank.utr_source,
                    "matched_by": credit_match.rule_id if credit_match else None,
                }
                if bank
                else None
            ),
            "lines": [
                {
                    "entity_id": line.entity_id,
                    "type": line.type.value,
                    "amount_paise": line.amount_paise,
                    "fee_paise": line.fee_paise,
                    "tax_paise": line.tax_paise,
                    "net_paise": line.net_paise,
                    "method": line.method,
                    "order_id": line.order_id,
                    "payment_id": line.payment_id,
                }
                for line in sorted(lines, key=lambda x: -abs(x.amount_paise))[:500]
            ],
            "line_count": len(lines),
        }


# --------------------------------------------------------------------------- #
# Ask
# --------------------------------------------------------------------------- #


class Question(BaseModel):
    question: str
    run_id: str | None = None


@app.post("/api/ask")
def ask(question: Question):
    from unnet.agents.qa import answer

    with _session() as session:
        rid = _resolve(session, question.run_id)
        return answer(session, rid, question.question)


@app.get("/api/cases")
def cases(owner: str | None = None, impact: str | None = None, status: str | None = None):
    """Outstanding work, routed. This is the landing view for a reason: it is
    the only screen that answers "what do I do now"."""
    with _session() as session:
        everything = casefile.load_previous(session)
        rows = list(everything.values())

    summary = casefile.summarise(rows)
    def keep(case) -> bool:
        if owner and case.owner != owner:
            return False
        if impact and case.impact != impact:
            return False
        if status:
            return case.status == status
        # Default view is work still to do; settled cases are available via
        # ?status=resolved rather than cluttering the queue.
        return case.status != "resolved"

    rank = {"P1": 0, "P2": 1, "P3": 2}
    shown = sorted(
        (c for c in rows if keep(c)),
        key=lambda c: (rank.get(c.priority, 9), -c.amount_paise),
    )

    #: The cap is a page-size guard, not a filter. Returning the true matched
    #: count alongside it is what lets the dashboard say "showing 400 of N"
    #: rather than quietly presenting a slice as the whole queue.
    return {
        "summary": summary,
        "total": len(shown),
        "items": [
            {
                "case_key": c.case_key,
                "code": c.code,
                "owner": c.owner,
                "impact": c.impact,
                "status": c.status,
                "priority": c.priority,
                "age_days": round(c.age_days, 1),
                "ageing_bucket": c.ageing_bucket,
                "subject_kind": c.subject_kind,
                "subject_id": c.subject_id,
                "amount_paise": c.amount_paise,
                "amount_display": c.amount_display,
                "action": c.action,
                "message": c.message,
                "hypothesis": c.hypothesis,
                "first_seen_run": c.first_seen_run,
                "last_seen_run": c.last_seen_run,
                "resolved_run": c.resolved_run,
            }
            for c in shown[:400]
        ],
    }


class Resolution(BaseModel):
    note: str = ""


@app.post("/api/cases/{case_key}/resolve")
def resolve_case(
    case_key: str, resolution: Resolution, actor: str = Depends(require_write)
):
    """Settle a case. The next run will see it settled and not raise it again —
    which is the whole point of tracking identity across runs."""
    import uuid

    with _session() as session:
        run_id = f"dashboard-{uuid.uuid4().hex[:6]}"
        updated = casefile.resolve(session, case_key, run_id=run_id, note=resolution.note)
        if not updated:
            raise HTTPException(404, f"No case {case_key}")

        audit = AuditLog(session, run_id)
        audit.record(
            stage="case_resolution",
            subject_kind="case_file",
            subject_id=case_key,
            decision="resolved by a human in the dashboard",
            decided_by=DecidedBy.HUMAN,
            decider_ref=f"dashboard:{actor}",
            evidence={"note": resolution.note},
        )
        session.commit()
    return {"ok": True, "case_key": case_key, "status": "resolved"}


@app.get("/api/cases/{case_key}")
def case_detail(case_key: str):
    """Everything needed to work one case, in one call.

    The primary workflow screen answers three questions — why is this open,
    what has Unnet already done, and what do I do next — and each needs a
    different slice of the run. Assembling them server-side keeps the client
    from making six requests and then having to join them.
    """
    with _session() as session:
        cases = casefile.load_previous(session)
        case = cases.get(case_key)
        if case is None:
            raise HTTPException(404, f"No case {case_key}")

        events = casefile.load_events(session, case_key)

        # The exception this case came from carries the agent trace and the
        # verifier's verdict — the part that shows what was actually tried.
        exception = session.exec(
            select(ReconException)
            .where(ReconException.subject_id == case.subject_id)
            .where(ReconException.code == case.code)
            .order_by(ReconException.id.desc())
        ).first()

        evidence_rows = _evidence_rows(session, case)

        return {
            "case": {
                "case_key": case.case_key,
                "code": case.code,
                "owner": case.owner,
                "impact": case.impact,
                "status": case.status,
                "priority": case.priority,
                "subject_kind": case.subject_kind,
                "subject_id": case.subject_id,
                "amount_paise": case.amount_paise,
                "amount_display": case.amount_display,
                "action": case.action,
                "message": case.message,
                "hypothesis": case.hypothesis,
                "age_days": round(case.age_days, 1),
                "ageing_bucket": case.ageing_bucket,
                "first_seen_at": case.first_seen_at,
                "last_seen_at": case.last_seen_at,
                "occurred_at": case.occurred_at,
                "as_of": case.as_of,
                "first_seen_run": case.first_seen_run,
                "last_seen_run": case.last_seen_run,
            },
            "summary": exception.summary if exception else "",
            "verdict": (exception.verifier_verdict if exception else None),
            "verdict_reason": (exception.verifier_reason if exception else None),
            "agent_trace": ((exception.evidence or {}).get("agent_trace") if exception else []) or [],
            "evidence": evidence_rows,
            "history": [
                {
                    "kind": e.kind,
                    "actor": e.actor.value,
                    "note": e.note,
                    "from_status": e.from_status,
                    "to_status": e.to_status,
                    "at": e.at,
                    "detail": e.detail,
                }
                for e in events
            ],
        }


def _evidence_rows(session: Session, case) -> list[dict]:
    """The actual records behind a case, as a table an analyst can read.

    Which records matter depends on what the case is about, so this dispatches
    on the subject rather than dumping every table.
    """
    rows: list[dict] = []

    def add(kind: str, ident: str, when, amount: int | None, detail: str):
        rows.append(
            {
                "kind": kind,
                "id": ident,
                "at": when,
                "amount_display": format_inr(amount) if amount is not None else "",
                "detail": detail,
            }
        )

    if case.subject_kind == "settlement_batch":
        batch = session.exec(
            select(SettlementBatch).where(SettlementBatch.settlement_id == case.subject_id)
        ).first()
        if batch:
            add("Payout", batch.settlement_id, batch.settled_at,
                batch.reported_amount_paise, f"UTR {batch.settlement_utr or '—'}")
            credit = session.exec(
                select(Match)
                .where(Match.right_id == batch.settlement_id)
                .where(Match.left_kind == "bank_txn")
                .order_by(Match.id.desc())
            ).first()
            if credit:
                txn = session.exec(
                    select(BankTxn).where(BankTxn.bank_ref == credit.left_id)
                ).first()
                if txn:
                    add("Bank credit", txn.bank_ref, txn.value_date,
                        txn.credit_paise, txn.narration)

    elif case.subject_kind == "bank_txn":
        txn = session.exec(
            select(BankTxn).where(BankTxn.bank_ref == case.subject_id)
        ).first()
        if txn:
            add("Bank credit", txn.bank_ref, txn.value_date, txn.credit_paise, txn.narration)

    elif case.subject_kind == "settlement_line":
        line = session.exec(
            select(SettlementLine).where(SettlementLine.entity_id == case.subject_id)
            .order_by(SettlementLine.id.desc())
        ).first()
        if line:
            add(f"Settlement {line.type.value}", line.entity_id, line.created_at,
                line.amount_paise,
                f"fee {format_inr(line.fee_paise)} · GST {format_inr(line.tax_paise)} · "
                f"{line.method or '—'}")
            if line.settlement_id:
                add("In payout", line.settlement_id, line.settled_at, None,
                    f"UTR {line.settlement_utr or '—'}")

    elif case.subject_kind == "merchant_order":
        order = session.exec(
            select(MerchantOrder).where(MerchantOrder.order_id == case.subject_id)
            .order_by(MerchantOrder.id.desc())
        ).first()
        if order:
            add("Order", order.order_id, order.captured_at, order.gross_paise,
                f"{order.method or '—'} · invoice {order.invoice_no or '—'}")
            if order.payment_id:
                line = session.exec(
                    select(SettlementLine)
                    .where(SettlementLine.payment_id == order.payment_id)
                    .order_by(SettlementLine.id.desc())
                ).first()
                if line:
                    add("Gateway line", line.entity_id, line.created_at, line.amount_paise,
                        "on risk hold" if line.on_hold else f"in {line.settlement_id or '—'}")

    return rows


@app.get("/api/health")
def health():
    """Liveness: the process is up. Deliberately does no I/O."""
    return {"ok": True, "dashboard": WEB_INDEX.exists()}


@app.get("/api/ready")
def ready():
    """Readiness: can this instance actually serve a request?

    Separate from liveness because they fail for different reasons and want
    different responses — a process that is up but has no run to show should
    not receive traffic, but restarting it will not help.
    """
    from unnet.llm.provider import CassetteStore

    checks = {"database": False, "run_present": False, "cassettes": 0}
    try:
        with _session() as session:
            run = session.exec(select(Run).order_by(Run.id.desc())).first()
            checks["database"] = True
            checks["run_present"] = run is not None
    except Exception as exc:  # noqa: BLE001 - readiness reports, never raises
        checks["error"] = str(exc)[:200]

    checks["cassettes"] = CassetteStore().count()
    checks["writes_enabled"] = writes_enabled()
    ok = checks["database"] and checks["run_present"]
    if not ok:
        raise HTTPException(503, detail=checks)
    return {"ok": True, **checks}


# --------------------------------------------------------------------------- #
# The dashboard. Declared last so it never shadows an /api route.
# --------------------------------------------------------------------------- #


@app.get("/")
def dashboard():
    if not WEB_INDEX.exists():
        raise HTTPException(404, f"Dashboard missing at {WEB_INDEX}")
    return FileResponse(WEB_INDEX)
