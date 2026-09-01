"""Pulling a UTR out of a bank narration.

The UTR is the only hard link between "Razorpay says it sent you a payout" and
"your bank says money arrived". It is not a field — it is a substring buried in
free text that every bank formats differently:

    NEFT-KKBKH14156891582-RAZORPAY SOFTWARE PVT LTD-HDFC0000123-PAYOUT
    NEFT CR-ICICH0000701-RAZORPAY SOFTWARE PRIVATE LIMITED-ACME RETAIL
    MB:NEFT:RAZORPAY SOFTWARE:PAYOUT:REF8823910

Regex handles the first shape and misses the rest. The model is only consulted
on a miss, and whatever it returns still has to match a settlement we already
know about — see ``unnet.engine.tier1``. An unverifiable UTR is discarded.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

#: Razorpay's UTRs look like a 5-char bank/IFSC-ish prefix then 10-12 digits,
#: or an all-alphanumeric token of 12-22 chars (their newer format).
_UTR_PATTERNS = [
    re.compile(r"\b([A-Z]{4,5}[A-Z]?\d{10,14})\b"),
    re.compile(r"\b(\d{9,12}[a-z0-9]{4,8})\b"),
]

#: A narration that mentions Razorpay is a payout candidate even without a UTR;
#: this is what tells the engine an unmatched credit is worth a model call.
_RAZORPAY_HINT = re.compile(r"razorpay", re.IGNORECASE)

#: Tokens that look like UTRs but never are.
_BLOCKLIST = re.compile(r"^(NEFT|RTGS|IMPS|UPI|PAYOUT|SETTLEMENT)\d*$", re.IGNORECASE)


@dataclass
class NarrationParse:
    utr: Optional[str]
    source: Optional[str]  # "regex" | "model" | None
    looks_like_payout: bool
    matched_pattern: Optional[str] = None


def parse_narration(narration: str) -> NarrationParse:
    """Extract a UTR by regex. Never calls a model — that is the caller's job."""
    text = (narration or "").strip()
    looks_like_payout = bool(_RAZORPAY_HINT.search(text))

    for index, pattern in enumerate(_UTR_PATTERNS):
        for candidate in pattern.findall(text.upper() if index == 0 else text):
            if _BLOCKLIST.match(candidate):
                continue
            # An IFSC code (4 letters + '0' + 6 alphanumerics) is a bank branch,
            # not a transaction reference. It shows up in nearly every NEFT
            # narration and is the single most common false UTR.
            if re.fullmatch(r"[A-Z]{4}0[A-Z0-9]{6}", candidate):
                continue
            return NarrationParse(
                utr=candidate,
                source="regex",
                looks_like_payout=looks_like_payout,
                matched_pattern=pattern.pattern,
            )

    return NarrationParse(utr=None, source=None, looks_like_payout=looks_like_payout)
