"""CSV -> canonical rows, driven by a validated :class:`MappingSpec`."""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Optional

from unnet.core.models import BankTxn, EntityType, MerchantOrder, SettlementBatch, SettlementLine
from unnet.core.money import rupees_to_paise
from unnet.ingest.mapping import MappingSpec, parse_datetime
from unnet.ingest.narration import parse_narration


def read_csv(path: Path | str) -> tuple[list[str], list[dict[str, str]]]:
    with Path(path).open(newline="") as fh:
        reader = csv.DictReader(fh)
        headers = list(reader.fieldnames or [])
        return headers, list(reader)


def _money(spec: MappingSpec, row: dict, name: str) -> int:
    raw = spec.get(row, name)
    if not raw:
        return 0
    try:
        return rupees_to_paise(raw)
    except Exception:
        return 0


def _date(spec: MappingSpec, row: dict, name: str) -> Optional[object]:
    return parse_datetime(spec.get(row, name), spec.date_format)


def _bool(spec: MappingSpec, row: dict, name: str, default: bool = False) -> bool:
    raw = spec.get(row, name).lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "y", "t"}


def load_merchant_orders(
    rows: list[dict[str, str]], spec: MappingSpec, run_id: str
) -> list[MerchantOrder]:
    orders: list[MerchantOrder] = []
    for row in rows:
        captured = _date(spec, row, "captured_at")
        if captured is None:
            # Without a date this row cannot participate in any time-windowed
            # rule. It is reported as SCHEMA_UNPARSEABLE rather than dropped.
            continue
        orders.append(
            MerchantOrder(
                run_id=run_id,
                order_id=spec.get(row, "order_id"),
                payment_id=spec.get(row, "payment_id") or None,
                invoice_no=spec.get(row, "invoice_no") or None,
                customer_ref=spec.get(row, "customer_ref") or None,
                gross_paise=_money(spec, row, "gross"),
                method=(spec.get(row, "method") or "").lower() or None,
                captured_at=captured,
                refunded_paise=_money(spec, row, "refunded"),
            )
        )
    return orders


def load_settlement_lines(
    rows: list[dict[str, str]], spec: MappingSpec, run_id: str
) -> list[SettlementLine]:
    lines: list[SettlementLine] = []
    for row in rows:
        raw_type = (spec.get(row, "type") or "payment").lower()
        try:
            entity_type = EntityType(raw_type)
        except ValueError:
            entity_type = EntityType.ADJUSTMENT

        created = _date(spec, row, "created_at")
        lines.append(
            SettlementLine(
                run_id=run_id,
                entity_id=spec.get(row, "entity_id"),
                type=entity_type,
                debit_paise=_money(spec, row, "debit"),
                credit_paise=_money(spec, row, "credit"),
                amount_paise=_money(spec, row, "amount"),
                currency=spec.get(row, "currency") or "INR",
                fee_paise=_money(spec, row, "fee"),
                tax_paise=_money(spec, row, "tax"),
                on_hold=_bool(spec, row, "on_hold"),
                settled=_bool(spec, row, "settled", default=True),
                credit_type=spec.get(row, "credit_type") or "default",
                created_at=created,
                settled_at=_date(spec, row, "settled_at"),
                settlement_id=spec.get(row, "settlement_id") or None,
                settlement_utr=spec.get(row, "settlement_utr") or None,
                payment_id=spec.get(row, "payment_id") or None,
                order_id=spec.get(row, "order_id") or None,
                dispute_id=spec.get(row, "dispute_id") or None,
                method=(spec.get(row, "method") or "").lower() or None,
                card_network=spec.get(row, "card_network") or None,
                card_issuer=spec.get(row, "card_issuer") or None,
                card_type=spec.get(row, "card_type") or None,
                description=spec.get(row, "description") or None,
            )
        )
    return lines


def load_settlement_batches(
    rows: list[dict[str, str]], spec: MappingSpec, run_id: str
) -> list[SettlementBatch]:
    batches: list[SettlementBatch] = []
    for row in rows:
        created = _date(spec, row, "created_at")
        settled = _date(spec, row, "settled_at")
        batches.append(
            SettlementBatch(
                run_id=run_id,
                settlement_id=spec.get(row, "settlement_id"),
                settlement_utr=spec.get(row, "utr") or None,
                reported_amount_paise=_money(spec, row, "amount"),
                currency=spec.get(row, "currency") or "INR",
                status=spec.get(row, "status") or "processed",
                created_at=created or settled,
                settled_at=settled,
            )
        )
    return batches


def load_bank_txns(
    rows: list[dict[str, str]], spec: MappingSpec, run_id: str
) -> list[BankTxn]:
    txns: list[BankTxn] = []
    for index, row in enumerate(rows):
        value_date = _date(spec, row, "value_date") or _date(spec, row, "txn_date")
        if value_date is None:
            continue

        narration = spec.get(row, "narration")
        parsed = parse_narration(narration)

        txns.append(
            BankTxn(
                run_id=run_id,
                bank_ref=spec.get(row, "bank_ref") or f"row{index + 1}",
                value_date=value_date,
                narration=narration,
                credit_paise=_money(spec, row, "credit"),
                debit_paise=_money(spec, row, "debit"),
                balance_paise=_money(spec, row, "balance") or None,
                extracted_utr=parsed.utr,
                utr_source=parsed.source,
            )
        )
    return txns
