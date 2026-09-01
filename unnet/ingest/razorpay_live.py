"""Optional: read a real settlement recon report from Razorpay test mode.

Everything else in this project runs on synthetic fixtures, which is what the
track asks for and what makes the metrics reproducible. This adapter exists to
show the canonical schema was not invented to fit the fixtures: it is Razorpay's
own recon field list, so a live report lands in exactly the same shape and the
rest of the pipeline does not know the difference.

Usage::

    export RAZORPAY_KEY_ID=rzp_test_...
    export RAZORPAY_KEY_SECRET=...
    python -m unnet.ingest.razorpay_live --from 2026-08-01 --to 2026-08-21

Writes ``razorpay_settlement_recon.csv`` and ``razorpay_settlements.csv`` in the
same layout the generator produces, so ``unnet recon --data <dir>`` reads them
with no other change. Test mode only — this never needs live credentials.
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx

API = "https://api.razorpay.com/v1"

#: Fields Razorpay's settlement-recon endpoint returns, in the order we write
#: them. Matches unnet.evaluation.generator's output exactly.
RECON_FIELDS = [
    "entity_id", "type", "debit", "credit", "amount", "currency", "fee", "tax",
    "on_hold", "settled", "created_at", "settled_at", "settlement_id",
    "settlement_utr", "credit_type", "payment_id", "order_id", "dispute_id",
    "method", "card_network", "card_issuer", "card_type", "description",
]


def _auth() -> tuple[str, str]:
    key = os.environ.get("RAZORPAY_KEY_ID", "")
    secret = os.environ.get("RAZORPAY_KEY_SECRET", "")
    if not key or not secret:
        raise SystemExit("Set RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET (test mode).")
    if not key.startswith("rzp_test_"):
        raise SystemExit(
            f"Refusing to run against non-test credentials ({key[:12]}...). "
            "This adapter is for test mode only."
        )
    return key, secret


def _epoch(date_str: str) -> int:
    return int(
        datetime.strptime(date_str, "%Y-%m-%d")
        .replace(tzinfo=timezone.utc)
        .timestamp()
    )


def _paise_to_rupees(value) -> str:
    """Razorpay returns paise. Our CSVs carry rupees, as their reports do."""
    return f"{int(value or 0) / 100:.2f}"


def _iso(epoch) -> str:
    if not epoch:
        return ""
    return datetime.fromtimestamp(int(epoch), tz=timezone.utc).replace(tzinfo=None).isoformat()


def fetch_settlements(client: httpx.Client, frm: int, to: int) -> list[dict]:
    out: list[dict] = []
    skip = 0
    while True:
        response = client.get(
            f"{API}/settlements",
            params={"from": frm, "to": to, "count": 100, "skip": skip},
        )
        response.raise_for_status()
        items = response.json().get("items", [])
        out.extend(items)
        if len(items) < 100:
            return out
        skip += 100


def fetch_recon(client: httpx.Client, day: int) -> list[dict]:
    """One day of settlement recon rows.

    Razorpay's recon endpoint is keyed by settlement day rather than by
    settlement id, which is why this walks days rather than settlements.
    """
    out: list[dict] = []
    skip = 0
    while True:
        response = client.get(
            f"{API}/settlements/recon/combined",
            params={"year": day // 10000, "month": (day // 100) % 100,
                    "day": day % 100, "count": 100, "skip": skip},
        )
        response.raise_for_status()
        items = response.json().get("items", [])
        out.extend(items)
        if len(items) < 100:
            return out
        skip += 100


def write_recon(rows: list[dict], path: Path) -> None:
    with path.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=RECON_FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    "entity_id": row.get("entity_id", ""),
                    "type": row.get("type", ""),
                    "debit": _paise_to_rupees(row.get("debit")),
                    "credit": _paise_to_rupees(row.get("credit")),
                    "amount": _paise_to_rupees(row.get("amount")),
                    "currency": row.get("currency", "INR"),
                    "fee": _paise_to_rupees(row.get("fee")),
                    "tax": _paise_to_rupees(row.get("tax")),
                    "on_hold": str(bool(row.get("on_hold"))).lower(),
                    "settled": str(bool(row.get("settled", True))).lower(),
                    "created_at": _iso(row.get("created_at")),
                    "settled_at": _iso(row.get("settled_at")),
                    "settlement_id": row.get("settlement_id") or "",
                    "settlement_utr": row.get("settlement_utr") or "",
                    "credit_type": row.get("credit_type") or "default",
                    "payment_id": row.get("payment_id") or "",
                    "order_id": row.get("order_id") or "",
                    "dispute_id": row.get("dispute_id") or "",
                    "method": row.get("method") or "",
                    "card_network": row.get("card_network") or "",
                    "card_issuer": row.get("card_issuer") or "",
                    "card_type": row.get("card_type") or "",
                    "description": row.get("description") or "",
                }
            )


def write_settlements(rows: list[dict], path: Path) -> None:
    with path.open("w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(
            ["settlement_id", "utr", "amount", "currency", "status", "created_at", "settled_at"]
        )
        for row in rows:
            writer.writerow(
                [
                    row.get("id", ""),
                    row.get("utr", ""),
                    _paise_to_rupees(row.get("amount")),
                    row.get("currency", "INR"),
                    row.get("status", "processed"),
                    _iso(row.get("created_at")),
                    _iso(row.get("created_at")),
                ]
            )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--from", dest="frm", required=True, help="YYYY-MM-DD")
    parser.add_argument("--to", dest="to", required=True, help="YYYY-MM-DD")
    parser.add_argument("--out", default="data/live", help="output directory")
    args = parser.parse_args(argv)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    with httpx.Client(auth=_auth(), timeout=60.0) as client:
        settlements = fetch_settlements(client, _epoch(args.frm), _epoch(args.to))
        write_settlements(settlements, out / "razorpay_settlements.csv")

        recon: list[dict] = []
        days = {
            datetime.fromtimestamp(int(s["created_at"]), tz=timezone.utc)
            for s in settlements
            if s.get("created_at")
        }
        for day in sorted(days):
            recon.extend(fetch_recon(client, day.year * 10000 + day.month * 100 + day.day))
        write_recon(recon, out / "razorpay_settlement_recon.csv")

    print(f"Wrote {len(settlements)} settlements and {len(recon)} recon rows to {out}/")
    print("Add your own merchant_ledger.csv and bank_statement.csv, then:")
    print(f"  python -m unnet.cli --data {out} recon")
    return 0


if __name__ == "__main__":
    sys.exit(main())
