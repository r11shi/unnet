"""Ask the run a question.

Two paths, and the cheaper one is tried first:

1. **Intents** — a handful of questions finance actually asks every week ("why
   was Tuesday short", "what is still open", "how much is stuck on risk hold")
   are answered by parameterised queries. No model, instant, and the same
   answer every time.

2. **Model NL->SQL** — anything else becomes a SELECT, if a model is
   configured. The statement is shown to the user alongside the answer, because
   a number you cannot check is not an answer.

The SQL a model writes is never executed unguarded: it must be a single SELECT,
it must not touch anything outside the allowed tables, and it is run against a
read-only connection with a row cap. A model that decides the best way to
reconcile the books is to DROP TABLE gets an error, not a migration.
"""

from __future__ import annotations

import re
from contextlib import contextmanager

from sqlalchemy import text
from sqlmodel import Session, select

from unnet.core.models import ExceptionStatus, Match, ReconException, Run, SettlementBatch
from unnet.core.money import format_inr
from unnet.llm.provider import LLMUnavailable, build_client

ALLOWED_TABLES = {
    "merchant_order",
    "settlement_line",
    "settlement_batch",
    "bank_txn",
    "match",
    "recon_exception",
    "recon_audit",
    "run",
}

FORBIDDEN = re.compile(
    r"\b(insert|update|delete|drop|alter|create|replace|attach|detach|pragma|vacuum)\b",
    re.IGNORECASE,
)

#: SQLite functions that escape the sandbox without ever looking like a write.
#: `load_extension` loads an arbitrary shared object; the fileio extension's
#: `readfile`/`writefile` reach the filesystem. Statement shape and a table
#: allow-list say nothing about any of them — `SELECT load_extension('x')` is a
#: perfectly well-formed SELECT over no tables at all. Python's sqlite3 disables
#: extension loading by default, so this is depth rather than the only guard,
#: which is exactly why it should not be left to the build.
FORBIDDEN_FUNCTIONS = re.compile(
    r"\b(load_extension|readfile|writefile|edit|fts3_tokenizer|zipfile|sqlite_compileoption\w*)\s*\(",
    re.IGNORECASE,
)

MAX_ROWS = 200

SCHEMA_FOR_MODEL = """\
Tables (SQLite). All money columns are INTEGER paise.

merchant_order(run_id, order_id, payment_id, invoice_no, customer_ref,
               gross_paise, method, captured_at, refunded_paise)
settlement_line(run_id, entity_id, type, debit_paise, credit_paise, amount_paise,
                fee_paise, tax_paise, on_hold, settled, created_at, settled_at,
                settlement_id, settlement_utr, payment_id, order_id, dispute_id,
                method, card_network, card_issuer, card_type)
settlement_batch(run_id, settlement_id, settlement_utr, reported_amount_paise,
                 status, created_at, settled_at)
bank_txn(run_id, bank_ref, value_date, narration, credit_paise, debit_paise,
         balance_paise, extracted_utr, utr_source)
match(run_id, tier, rule_id, confidence, decided_by, left_kind, left_id,
      right_kind, right_id, amount_paise)
recon_exception(run_id, code, status, subject_kind, subject_id, residual_paise,
                summary, verifier_verdict, verifier_reason)

settlement_line.type is one of: payment, refund, adjustment, dispute, transfer.
recon_exception.status is one of: open, auto_resolved, ai_resolved, ai_rejected,
rolled_forward, accepted_by_human, rejected_by_human.
"""

SQL_SCHEMA = {
    "type": "object",
    "properties": {
        "sql": {"type": "string", "description": "A single SELECT statement."},
        "explanation": {"type": "string"},
    },
    "required": ["sql"],
}


def answer(
    session: Session, run_id: str, question: str, *, allow_model: bool = True
) -> dict:
    """Answer a question about one run, cheapest path first.

    `allow_model` is how the API hands down its spend budget. When it is False
    the deterministic intents still answer — they cost nothing — and anything
    else says so rather than silently returning a worse answer or an error.
    """
    intent = _match_intent(session, run_id, question)
    if intent is not None:
        return intent
    if not allow_model:
        return _result(
            "The natural-language path is paused: this demo caps how many model "
            "calls it will make in a day, and today's budget is spent. Questions "
            "about shortfalls, open exceptions, risk holds, chargebacks, fees and "
            "totals are answered from the ledger directly and still work.",
            [],
            source="budget_exhausted",
        )
    return _model_answer(session, run_id, question)


# --------------------------------------------------------------------------- #
# Intents
# --------------------------------------------------------------------------- #


def _match_intent(session: Session, run_id: str, question: str) -> dict | None:
    text_q = question.lower().strip()

    # Patterns are prefix-matched rather than whole-word: people ask about
    # "chargebacks" and "fees", and \bchargeback\b does not match the plural.
    if re.search(r"\b(short|shortfall|less than|missing money|unexplained|deduct)", text_q):
        return _short_payouts(session, run_id)
    if re.search(r"\b(open|outstanding|unresolved|still.*exception|need.*review)", text_q):
        return _open_exceptions(session, run_id)
    if re.search(r"\b(hold|held|frozen|risk)", text_q):
        return _on_hold(session, run_id)
    if re.search(r"\b(chargeback|dispute)", text_q):
        return _chargebacks(session, run_id)
    if re.search(r"\b(fee|mdr|gst|tax|commission)", text_q):
        return _fees(session, run_id)
    if re.search(r"\b(how much|total|summary|overall).*(reconcil|match|settle)", text_q):
        return _reconciled(session, run_id)
    return None


def _result(
    answer_text: str, rows: list[dict], *, source: str, sql: str = "", detail: str = ""
) -> dict:
    return {
        "answer": answer_text,
        "rows": rows,
        "source": source,
        "sql": sql,
        "detail": detail,
    }


def _short_payouts(session: Session, run_id: str) -> dict:
    rows = session.exec(
        select(ReconException)
        .where(ReconException.run_id == run_id)
        .where(ReconException.code.in_(["SHORT_CREDIT", "OVER_CREDIT", "ROUNDING"]))
    ).all()
    if not rows:
        return _result("Every payout arrived for the exact amount promised.", [], source="intent")

    total = sum(abs(r.residual_paise) for r in rows)
    return _result(
        f"{len(rows)} payouts differ from what the bank credited, "
        f"{format_inr(total)} in total.",
        [
            {
                "settlement_id": r.subject_id,
                "code": r.code.value,
                "difference": format_inr(r.residual_paise),
                "why": r.summary,
                "narration": (r.evidence or {}).get("narration", ""),
            }
            for r in sorted(rows, key=lambda x: -abs(x.residual_paise))
        ],
        source="intent",
    )


def _open_exceptions(session: Session, run_id: str) -> dict:
    rows = session.exec(
        select(ReconException)
        .where(ReconException.run_id == run_id)
        .where(ReconException.status.in_([ExceptionStatus.OPEN, ExceptionStatus.AI_REJECTED]))
    ).all()
    by_code: dict[str, dict] = {}
    for row in rows:
        entry = by_code.setdefault(row.code.value, {"code": row.code.value, "count": 0, "value_paise": 0})
        entry["count"] += 1
        entry["value_paise"] += abs(row.residual_paise)

    total = sum(e["value_paise"] for e in by_code.values())
    return _result(
        f"{len(rows)} exceptions are still open, covering {format_inr(total)}.",
        [
            {"code": e["code"], "count": e["count"], "value": format_inr(e["value_paise"])}
            for e in sorted(by_code.values(), key=lambda x: -x["value_paise"])
        ],
        source="intent",
    )


def _on_hold(session: Session, run_id: str) -> dict:
    rows = session.exec(
        select(ReconException)
        .where(ReconException.run_id == run_id)
        .where(ReconException.code == "ON_HOLD")
    ).all()
    total = sum(r.residual_paise for r in rows)
    return _result(
        f"{len(rows)} payments are on risk hold, holding back {format_inr(total)}. "
        "They are not missing — they are frozen and will settle when the hold lifts.",
        [
            {
                "order_id": r.subject_id,
                "amount": format_inr(r.residual_paise),
                "method": (r.evidence or {}).get("method"),
                "captured_at": (r.evidence or {}).get("captured_at"),
            }
            for r in sorted(rows, key=lambda x: -x.residual_paise)[:50]
        ],
        source="intent",
    )


def _chargebacks(session: Session, run_id: str) -> dict:
    rows = session.exec(
        select(ReconException)
        .where(ReconException.run_id == run_id)
        .where(ReconException.code == "CHARGEBACK_DEDUCTION")
    ).all()
    total = sum(r.residual_paise for r in rows)
    return _result(
        f"{len(rows)} chargebacks cost {format_inr(total)} including dispute fees. "
        "Each was deducted from a later payout than the sale it reverses.",
        [
            {
                "dispute_id": (r.evidence or {}).get("dispute_id"),
                "payment_id": (r.evidence or {}).get("payment_id"),
                "disputed": format_inr((r.evidence or {}).get("disputed_paise", 0)),
                "fee": format_inr((r.evidence or {}).get("dispute_fee_paise", 0)),
                "deducted_in": (r.evidence or {}).get("deducted_in_settlement"),
                "original_settlement": (r.evidence or {}).get("original_settlement"),
            }
            for r in rows
        ],
        source="intent",
    )


def _fees(session: Session, run_id: str) -> dict:
    rows = session.exec(
        select(ReconException)
        .where(ReconException.run_id == run_id)
        .where(ReconException.code.in_(["FEE_MISMATCH", "GST_MISMATCH"]))
    ).all()
    run = session.exec(select(Run).where(Run.run_id == run_id)).first()
    breakdowns = (run.notes or {}).get("breakdowns", []) if run else []
    mdr = sum(b.get("mdr_paise", 0) for b in breakdowns)
    gst = sum(b.get("gst_paise", 0) for b in breakdowns)

    text_answer = (
        f"Razorpay charged {format_inr(mdr)} in MDR plus {format_inr(gst)} GST across "
        f"{len(breakdowns)} payouts."
    )
    if rows:
        delta = sum(abs(r.residual_paise) for r in rows)
        text_answer += (
            f" {len(rows)} lines were billed off the rate card, "
            f"{format_inr(delta)} in total — worth raising with support."
        )
    else:
        text_answer += " Every line matched the rate card."

    return _result(
        text_answer,
        [
            {
                "entity_id": r.subject_id,
                "code": r.code.value,
                "difference": format_inr(r.residual_paise),
                "why": r.summary,
            }
            for r in rows
        ],
        source="intent",
    )


def _reconciled(session: Session, run_id: str) -> dict:
    run = session.exec(select(Run).where(Run.run_id == run_id)).first()
    if run is None:
        return _result("No such run.", [], source="intent")
    matches = session.exec(select(Match).where(Match.run_id == run_id)).all()
    batches = session.exec(
        select(SettlementBatch).where(SettlementBatch.run_id == run_id)
    ).all()
    return _result(
        f"{format_inr(run.value_reconciled_paise)} reconciled across {len(matches):,} links "
        f"and {len(batches)} payouts, in {run.duration_ms:,} ms. "
        f"{format_inr(run.value_in_exceptions_paise)} is still in the exception queue.",
        [],
        source="intent",
    )


# --------------------------------------------------------------------------- #
# Model NL->SQL
# --------------------------------------------------------------------------- #


def _model_answer(session: Session, run_id: str, question: str) -> dict:
    client = build_client()
    prompt = (
        f"{SCHEMA_FOR_MODEL}\n"
        f"Write ONE SQLite SELECT answering this question about run '{run_id}'.\n"
        f"Always filter on run_id = '{run_id}'. Return at most {MAX_ROWS} rows.\n"
        "Money is stored in paise; divide by 100.0 when presenting rupees.\n\n"
        f"Question: {question}"
    )

    try:
        response = client.complete("qa_sql", prompt, SQL_SCHEMA)
    except LLMUnavailable as exc:
        # Two different situations wear the same exception, and an operator
        # needs to know which: nothing is configured (a setup problem, theirs
        # to fix) versus a provider that is configured but not answering right
        # now (transient, nothing to fix). Neither one should put a raw HTTP
        # error in the answer line of a finance tool — that detail belongs in
        # a field the interface can show on request.
        configured = bool(client.backends)
        headline = (
            "The model is configured but did not answer just now — it happens on "
            "the free tier under load. "
            if configured
            else "No model is configured for this deployment. "
        )
        return _result(
            headline + "Questions about short payouts, open exceptions, risk holds, "
            "chargebacks, fees and totals are answered straight from the ledger and "
            "still work.",
            [],
            source="unavailable",
            detail=str(exc),
        )

    sql = str(response.data.get("sql", "")).strip().rstrip(";")
    ok, reason = _is_safe(sql)
    if not ok:
        return _result(f"I refused to run that query: {reason}", [], source="refused", sql=sql)

    try:
        # Belt and braces. `_is_safe` reasons about the text of a statement a
        # model wrote, and text analysis is exactly the kind of guard that gets
        # walked around. `query_only` makes the database itself refuse a write,
        # so a bypass of the regex is still not a way into the ledger.
        with _read_only(session):
            result = session.exec(text(f"SELECT * FROM ({sql}) LIMIT {MAX_ROWS}"))
            columns = list(result.keys())
            rows = [dict(zip(columns, row)) for row in result.fetchall()]
    except Exception as exc:  # noqa: BLE001 - surface the SQL error to the user
        return _result(f"That query failed: {exc}", [], source="sql_error", sql=sql)

    return _result(
        str(response.data.get("explanation") or f"{len(rows)} rows."),
        rows,
        source=f"model:{response.decider_ref}",
        sql=sql,
    )


@contextmanager
def _read_only(session):
    """Run a block with the SQLite connection refusing every write.

    Scoped rather than global: the same session is used by the rest of the API,
    which legitimately writes.
    """
    session.exec(text("PRAGMA query_only = ON"))
    try:
        yield
    finally:
        session.exec(text("PRAGMA query_only = OFF"))


def _is_safe(sql: str) -> tuple[bool, str]:
    """Read-only, single-statement, known tables only."""
    if not sql:
        return False, "empty statement"
    if ";" in sql:
        return False, "more than one statement"
    if not re.match(r"^\s*(select|with)\b", sql, re.IGNORECASE):
        return False, "not a SELECT"
    if FORBIDDEN.search(sql):
        return False, "contains a write or schema operation"
    if FORBIDDEN_FUNCTIONS.search(sql):
        return False, "calls a SQLite function that can reach outside the database"

    referenced = set(re.findall(r"\b(?:from|join)\s+([a-zA-Z_][a-zA-Z0-9_]*)", sql, re.IGNORECASE))
    unknown = referenced - ALLOWED_TABLES
    if unknown:
        return False, f"references unknown tables: {', '.join(sorted(unknown))}"
    return True, "ok"
