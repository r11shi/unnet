"""Column mapping: getting three differently-shaped CSVs into one schema.

Every merchant names their ledger columns differently, every bank exports a
different statement layout, and dates arrive in whatever format the exporting
system felt like. This module defines the contract both mappers produce:

* :func:`heuristic_map` handles headers we recognise, with no model call.
* ``unnet.ingest.mapper_agent`` asks a model when the heuristic comes up short.

Both return a :class:`MappingSpec`, and **a spec is never trusted until
:func:`validate_spec` has dry-run it against real rows**. That gate is the
reason a model is safe to use here: it proposes, the parser disposes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterable, Optional

from unnet.core.money import rupees_to_paise

#: Formats seen across Indian bank and gateway exports, most specific first.
DATE_FORMATS = [
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d",
    "%d-%m-%Y %H:%M:%S",
    "%d-%m-%Y %H:%M",
    "%d-%m-%Y",
    "%d/%m/%Y %H:%M:%S",
    "%d/%m/%Y %H:%M",
    "%d/%m/%Y",
    "%d/%m/%y",
    "%d-%b-%Y",
    "%d %b %Y",
    "%m/%d/%Y",
]


class SourceKind:
    MERCHANT_LEDGER = "merchant_ledger"
    SETTLEMENT_RECON = "settlement_recon"
    SETTLEMENTS = "settlements"
    BANK_STATEMENT = "bank_statement"


#: Canonical field -> whether the pipeline can proceed without it.
REQUIRED_FIELDS: dict[str, set[str]] = {
    SourceKind.MERCHANT_LEDGER: {"order_id", "gross", "captured_at"},
    SourceKind.SETTLEMENT_RECON: {"entity_id", "type", "amount", "settlement_id"},
    SourceKind.SETTLEMENTS: {"settlement_id", "amount"},
    SourceKind.BANK_STATEMENT: {"narration", "value_date"},
}

OPTIONAL_FIELDS: dict[str, set[str]] = {
    SourceKind.MERCHANT_LEDGER: {"payment_id", "invoice_no", "customer_ref", "method", "refunded"},
    SourceKind.SETTLEMENT_RECON: {
        "debit", "credit", "fee", "tax", "currency", "on_hold", "settled",
        "created_at", "settled_at", "settlement_utr", "credit_type", "payment_id",
        "order_id", "dispute_id", "method", "card_network", "card_issuer",
        "card_type", "description",
    },
    SourceKind.SETTLEMENTS: {"utr", "currency", "status", "created_at", "settled_at"},
    SourceKind.BANK_STATEMENT: {"bank_ref", "txn_date", "debit", "credit", "balance"},
}

#: Header aliases we have seen in the wild, lower-cased and stripped.
ALIASES: dict[str, dict[str, list[str]]] = {
    SourceKind.MERCHANT_LEDGER: {
        "order_id": ["order ref", "order_id", "order id", "orderref", "order no", "reference"],
        "payment_id": ["txn id", "payment_id", "payment id", "transaction id", "gateway txn"],
        "invoice_no": ["invoice #", "invoice no", "invoice", "invoice_no", "bill no"],
        "customer_ref": ["customer", "customer id", "customer_ref", "buyer"],
        "gross": ["amount (inr)", "amount", "gross", "order value", "total", "amt"],
        "method": ["mode", "method", "payment mode", "channel"],
        "captured_at": ["booked on", "captured_at", "date", "order date", "created at"],
        "refunded": ["refund amt", "refunded", "refund amount", "refund"],
    },
    SourceKind.SETTLEMENT_RECON: {
        "entity_id": ["entity_id", "id", "entity id"],
        "type": ["type", "entity type"],
        "debit": ["debit"],
        "credit": ["credit"],
        "amount": ["amount"],
        "currency": ["currency"],
        "fee": ["fee", "fees"],
        "tax": ["tax", "gst"],
        "on_hold": ["on_hold", "on hold"],
        "settled": ["settled"],
        "created_at": ["created_at", "created at"],
        "settled_at": ["settled_at", "settled at"],
        "settlement_id": ["settlement_id", "settlement id"],
        "settlement_utr": ["settlement_utr", "utr", "settlement utr"],
        "credit_type": ["credit_type"],
        "payment_id": ["payment_id", "payment id"],
        "order_id": ["order_id", "order id"],
        "dispute_id": ["dispute_id", "dispute id"],
        "method": ["method"],
        "card_network": ["card_network"],
        "card_issuer": ["card_issuer"],
        "card_type": ["card_type"],
        "description": ["description", "notes"],
    },
    SourceKind.SETTLEMENTS: {
        "settlement_id": ["settlement_id", "settlement id", "id"],
        "utr": ["utr", "settlement_utr", "reference"],
        "amount": ["amount", "net amount"],
        "currency": ["currency"],
        "status": ["status"],
        "created_at": ["created_at", "created at"],
        "settled_at": ["settled_at", "settled at", "settlement date"],
    },
    SourceKind.BANK_STATEMENT: {
        "txn_date": ["txn date", "transaction date", "date", "post date"],
        "value_date": ["value date", "value dt", "val date", "date"],
        "narration": ["description", "narration", "particulars", "remarks", "details"],
        "bank_ref": ["chq/ref no", "ref no", "reference", "cheque no", "utr no", "bank ref"],
        "debit": ["debit", "withdrawal", "dr", "withdrawal amt"],
        "credit": ["credit", "deposit", "cr", "deposit amt"],
        "balance": ["balance", "closing balance", "running balance"],
    },
}


@dataclass
class MappingSpec:
    """How to read one CSV into canonical fields."""

    source_kind: str
    #: canonical field name -> the header it lives under in this file.
    columns: dict[str, str] = field(default_factory=dict)
    #: strptime format for date columns; ``None`` means try DATE_FORMATS in order.
    date_format: Optional[str] = None
    #: Where the spec came from — surfaced in the audit trail.
    produced_by: str = "heuristic"
    confidence: int = 1000
    notes: str = ""

    def get(self, row: dict[str, str], field_name: str, default: str = "") -> str:
        header = self.columns.get(field_name)
        if header is None:
            return default
        return (row.get(header) or default).strip()


@dataclass
class ValidationReport:
    ok: bool
    missing_required: list[str] = field(default_factory=list)
    unparseable: list[str] = field(default_factory=list)
    rows_checked: int = 0

    @property
    def reason(self) -> str:
        parts = []
        if self.missing_required:
            parts.append(f"missing required fields: {', '.join(sorted(self.missing_required))}")
        if self.unparseable:
            parts.append(f"unparseable columns: {', '.join(sorted(self.unparseable))}")
        return "; ".join(parts) or "ok"


def _norm(header: str) -> str:
    return header.strip().lower().replace("_", " ").replace("-", " ")


def heuristic_map(headers: Iterable[str], source_kind: str) -> MappingSpec:
    """Map headers by alias table. Cheap, deterministic, and usually enough."""
    spec = MappingSpec(source_kind=source_kind, produced_by="heuristic")
    normalised = {_norm(h): h for h in headers}
    aliases = ALIASES.get(source_kind, {})

    for canonical, candidates in aliases.items():
        for candidate in candidates:
            header = normalised.get(_norm(candidate))
            if header is not None and canonical not in spec.columns:
                spec.columns[canonical] = header
                break

    return spec


def parse_datetime(value: str, preferred: str | None = None) -> Optional[datetime]:
    """Parse a date, trying the spec's format first then the known shapes."""
    text = (value or "").strip()
    if not text:
        return None

    formats = ([preferred] if preferred else []) + DATE_FORMATS
    for fmt in formats:
        if not fmt:
            continue
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue

    # Last resort: ISO-8601 with a timezone or fractional seconds.
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


#: Canonical fields that must parse as a date, by source kind.
_DATE_FIELDS = {"captured_at", "created_at", "settled_at", "txn_date", "value_date"}
#: Canonical fields that must parse as money.
_MONEY_FIELDS = {"gross", "amount", "debit", "credit", "fee", "tax", "balance", "refunded"}


def validate_spec(
    spec: MappingSpec, rows: list[dict[str, str]], *, sample: int = 25
) -> ValidationReport:
    """Dry-run a spec against real rows before anything is allowed to use it.

    This is the gate that makes a model-proposed mapping safe. A spec that names
    a column that does not exist, or that points a money field at a free-text
    column, fails here rather than silently producing a zero-rupee ledger.
    """
    required = REQUIRED_FIELDS.get(spec.source_kind, set())
    missing = sorted(required - set(spec.columns))

    unparseable: set[str] = set()
    checked = rows[:sample]

    for canonical, header in spec.columns.items():
        # A named column that is not in the file at all.
        if checked and header not in checked[0]:
            unparseable.add(canonical)
            continue

        if canonical in _DATE_FIELDS:
            values = [(r.get(header) or "").strip() for r in checked]
            populated = [v for v in values if v]
            if populated and not any(parse_datetime(v, spec.date_format) for v in populated):
                unparseable.add(canonical)

        elif canonical in _MONEY_FIELDS:
            values = [(r.get(header) or "").strip() for r in checked]
            populated = [v for v in values if v]
            if populated:
                failures = 0
                for value in populated:
                    try:
                        rupees_to_paise(value)
                    except Exception:
                        failures += 1
                # Money columns are routinely blank (a credit column on a debit
                # row), but a column where the populated values do not parse is
                # simply not a money column.
                if failures > len(populated) // 2:
                    unparseable.add(canonical)

    return ValidationReport(
        ok=not missing and not unparseable,
        missing_required=missing,
        unparseable=sorted(unparseable),
        rows_checked=len(checked),
    )
