"""Money handling for Unnet.

Every amount in this system is an ``int`` number of paise. There are no floats
anywhere on the matching path. This is deliberate: reconciliation is an equality
problem, and ``0.1 + 0.2 != 0.3`` is not an acceptable failure mode when the
output is "your payout was short by this much".

The only places a float is permitted are (a) rendering for humans and (b) rate
cards, where a percentage is converted to paise via integer rounding immediately.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

# 18% GST is levied on the MDR (the Razorpay fee), not on the transaction value.
GST_RATE_BPS = 1800  # basis points
BPS_DIVISOR = 10_000


def rupees_to_paise(value: str | int | float | Decimal) -> int:
    """Parse a rupee amount into paise.

    Accepts the messy shapes that turn up in real bank and gateway exports:
    ``"1,234.50"``, ``"(1234.50)"`` for negatives, ``"Rs. 1234.50"``, ``"1234.50 Cr"``.
    """
    if isinstance(value, int):
        return value * 100
    if isinstance(value, Decimal):
        return int((value * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    if isinstance(value, float):
        return int((Decimal(str(value)) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))

    text = str(value).strip()
    if not text:
        return 0

    negative = False
    if text.startswith("(") and text.endswith(")"):
        negative = True
        text = text[1:-1]

    # Strip currency ornaments and Dr/Cr suffixes used by Indian bank statements.
    for token in ("INR", "Rs.", "Rs", "₹", "Dr", "DR", "Cr", "CR"):
        text = text.replace(token, "")
    text = text.replace(",", "").replace(" ", "").strip()

    if text.startswith("-"):
        negative = True
        text = text[1:]
    if not text:
        return 0

    paise = int((Decimal(text) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    return -paise if negative else paise


def paise_to_rupees(paise: int) -> Decimal:
    """Exact rupee value of a paise amount, for display and export only."""
    return (Decimal(paise) / Decimal(100)).quantize(Decimal("0.01"))


def format_inr(paise: int, *, signed: bool = False) -> str:
    """Format paise using the Indian digit grouping (``₹12,34,567.89``)."""
    negative = paise < 0
    whole, frac = divmod(abs(paise), 100)

    digits = str(whole)
    if len(digits) > 3:
        head, tail = digits[:-3], digits[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        grouped = ",".join(groups) + "," + tail
    else:
        grouped = digits

    sign = "-" if negative else ("+" if signed else "")
    return f"{sign}₹{grouped}.{frac:02d}"


def gst_on(fee_paise: int) -> int:
    """GST payable on an MDR fee, rounded half-up to the paisa.

    Kept as one function so the engine and the synthetic generator cannot drift
    apart on rounding — a real source of one-paisa recon breaks.
    """
    return round_half_up(fee_paise * GST_RATE_BPS, BPS_DIVISOR)


def apply_bps(amount_paise: int, rate_bps: int) -> int:
    """Apply a basis-point rate to an amount, rounded half-up to the paisa."""
    return round_half_up(amount_paise * rate_bps, BPS_DIVISOR)


def round_half_up(numerator: int, denominator: int) -> int:
    """Integer division rounding halves away from zero.

    Python's ``round`` uses banker's rounding and ``//`` floors toward negative
    infinity; neither matches what a payment gateway does to a fee.
    """
    if denominator <= 0:
        raise ValueError("denominator must be positive")
    negative = numerator < 0
    value = (abs(numerator) * 2 + denominator) // (denominator * 2)
    return -value if negative else value
