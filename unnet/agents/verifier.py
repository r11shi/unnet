"""The gate every proposal has to pass.

A model may propose a decomposition of an unexplained amount. It may not post
one. The difference between those two sentences is this module.

The rule is conservation: the components a proposal names must sum to the
residual it claims to explain, **exactly, in integer paise**. Not within a
tolerance, not "close enough to be convincing" — a decomposition that is ₹0.50
out is not a slightly wrong answer, it is evidence that the reasoning behind it
was wrong, and posting it would put a wrong number in a ledger that people file
taxes from.

Every proposal is also checked for the failure modes a plausible-sounding model
answer actually has: naming a settlement that does not exist, naming the same
one twice, or claiming money that was already matched elsewhere.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Verdict(str, Enum):
    ACCEPTED = "accepted"
    REJECTED_SUM_MISMATCH = "rejected_sum_mismatch"
    REJECTED_UNKNOWN_COMPONENT = "rejected_unknown_component"
    REJECTED_DUPLICATE_COMPONENT = "rejected_duplicate_component"
    REJECTED_ALREADY_MATCHED = "rejected_already_matched"
    REJECTED_EMPTY = "rejected_empty"
    REJECTED_IMPLAUSIBLE_ADJUSTMENT = "rejected_implausible_adjustment"


@dataclass
class Component:
    """One named part of a proposed explanation."""

    kind: str  # "settlement_batch" | "bank_charge" | "adjustment"
    ref: str
    amount_paise: int
    note: str = ""


@dataclass
class Proposal:
    """What a resolver — model or rule — thinks explains a residual."""

    subject_kind: str
    subject_id: str
    target_paise: int
    components: list[Component] = field(default_factory=list)
    reasoning: str = ""
    produced_by: str = "unknown"
    confidence: int = 0

    def total_paise(self) -> int:
        return sum(c.amount_paise for c in self.components)


@dataclass
class VerificationResult:
    verdict: Verdict
    reason: str
    proposal: Proposal
    delta_paise: int = 0

    @property
    def accepted(self) -> bool:
        return self.verdict is Verdict.ACCEPTED


#: An unmodelled component (a bank charge we cannot see in any table) is only
#: credible at the scale banks actually charge. A proposal that balances itself
#: with a ₹40,000 "miscellaneous adjustment" is fitting the number, not
#: explaining it.
MAX_UNMODELLED_COMPONENT_PAISE = 50_000  # ₹500

_KNOWN_KINDS = {"settlement_batch", "settlement_line", "bank_charge", "adjustment"}
_UNMODELLED_KINDS = {"bank_charge", "adjustment"}


def verify(
    proposal: Proposal,
    *,
    known_refs: dict[str, int],
    already_matched: set[str] | None = None,
) -> VerificationResult:
    """Check a proposal against arithmetic and against reality.

    ``known_refs`` maps every real reference the proposal is allowed to cite to
    its true amount in paise. A component citing a reference that is not in
    there is a hallucination, and is rejected on that basis alone — before the
    arithmetic is even considered.
    """
    matched = already_matched or set()

    if not proposal.components:
        return VerificationResult(
            verdict=Verdict.REJECTED_EMPTY,
            reason="Proposal names no components.",
            proposal=proposal,
        )

    seen: set[str] = set()
    for component in proposal.components:
        if component.kind not in _KNOWN_KINDS:
            return VerificationResult(
                verdict=Verdict.REJECTED_UNKNOWN_COMPONENT,
                reason=f"Component kind '{component.kind}' is not a thing this ledger has.",
                proposal=proposal,
            )

        if component.kind in _UNMODELLED_KINDS:
            # Nothing to look up — but it must be small enough to be real.
            if abs(component.amount_paise) > MAX_UNMODELLED_COMPONENT_PAISE:
                return VerificationResult(
                    verdict=Verdict.REJECTED_IMPLAUSIBLE_ADJUSTMENT,
                    reason=(
                        f"Unmodelled {component.kind} of {component.amount_paise} paise "
                        f"exceeds the {MAX_UNMODELLED_COMPONENT_PAISE} paise ceiling; "
                        "this is balancing the books, not explaining them."
                    ),
                    proposal=proposal,
                )
            continue

        if component.ref not in known_refs:
            return VerificationResult(
                verdict=Verdict.REJECTED_UNKNOWN_COMPONENT,
                reason=f"'{component.ref}' does not exist in this dataset.",
                proposal=proposal,
            )

        if component.ref in seen:
            return VerificationResult(
                verdict=Verdict.REJECTED_DUPLICATE_COMPONENT,
                reason=f"'{component.ref}' is claimed twice in one proposal.",
                proposal=proposal,
            )
        seen.add(component.ref)

        if component.ref in matched:
            return VerificationResult(
                verdict=Verdict.REJECTED_ALREADY_MATCHED,
                reason=f"'{component.ref}' is already reconciled against something else.",
                proposal=proposal,
            )

        # The cited amount must be the real amount, not one chosen to make the
        # sum work out.
        actual = known_refs[component.ref]
        if component.amount_paise != actual:
            return VerificationResult(
                verdict=Verdict.REJECTED_SUM_MISMATCH,
                reason=(
                    f"'{component.ref}' is {actual} paise, but the proposal claims "
                    f"{component.amount_paise} paise."
                ),
                proposal=proposal,
                delta_paise=component.amount_paise - actual,
            )

    delta = proposal.total_paise() - proposal.target_paise
    if delta != 0:
        return VerificationResult(
            verdict=Verdict.REJECTED_SUM_MISMATCH,
            reason=(
                f"Components sum to {proposal.total_paise()} paise but must explain "
                f"{proposal.target_paise} paise — out by {delta} paise."
            ),
            proposal=proposal,
            delta_paise=delta,
        )

    return VerificationResult(
        verdict=Verdict.ACCEPTED,
        reason=(
            f"{len(proposal.components)} components sum exactly to "
            f"{proposal.target_paise} paise."
        ),
        proposal=proposal,
    )
