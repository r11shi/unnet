"""The gate between a model and the ledger.

A model may *propose* an explanation for missing money. It may not *post* one.

v1 of this file made a mistake worth naming, because it is the mistake most
"AI + verifier" designs make: it treated **arithmetic consistency as financial
truth**. If the components summed to the residual, the proposal was accepted.

They are not the same thing. A ₹11.80 shortfall is equally "satisfied" by:

* a ₹10 NEFT charge plus ₹1.80 GST,
* a single ₹11.80 adjustment,
* two unrelated fees that happen to add up.

Only one of those is what actually happened, and arithmetic cannot tell them
apart. Worse, v1 let a model **invent** a component of up to ₹500 and have that
invention pass verification — hallucinated evidence laundered as verified fact.

So verification now returns three verdicts, not two:

``RESOLVED_VERIFIED``
    Every component cites a real record, and the cited value was **read back
    from the ledger at verify time** and matched. Arithmetic exact. Safe to
    close automatically, because every rupee is traceable to a row.

``HYPOTHESIS``
    The arithmetic is exact, but the explanation rests on something we cannot
    evidence — an invented component, or more than one distinct component set
    that satisfies the same residual. **Never closed automatically.** It goes to
    a human *with* the hypothesis, which is genuinely useful and honestly
    labelled.

``REJECTED``
    The arithmetic or the citations fail outright.

The distinction between the first two is the whole point. A model that says
"probably a NEFT charge" is being helpful. A system that records that as fact is
lying to an auditor.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Optional


class Verdict(str, Enum):
    #: Every component traced to a real row. Auto-closeable.
    RESOLVED_VERIFIED = "resolved_verified"
    #: Arithmetic holds but the explanation is not evidenced. Human decides.
    HYPOTHESIS = "hypothesis"

    REJECTED_SUM_MISMATCH = "rejected_sum_mismatch"
    REJECTED_UNKNOWN_COMPONENT = "rejected_unknown_component"
    REJECTED_DUPLICATE_COMPONENT = "rejected_duplicate_component"
    REJECTED_ALREADY_MATCHED = "rejected_already_matched"
    REJECTED_EMPTY = "rejected_empty"
    REJECTED_IMPLAUSIBLE_ADJUSTMENT = "rejected_implausible_adjustment"
    REJECTED_PROVENANCE_FAILED = "rejected_provenance_failed"


#: Verdicts that mean "do not touch the books".
REJECTED_VERDICTS = {
    Verdict.REJECTED_SUM_MISMATCH,
    Verdict.REJECTED_UNKNOWN_COMPONENT,
    Verdict.REJECTED_DUPLICATE_COMPONENT,
    Verdict.REJECTED_ALREADY_MATCHED,
    Verdict.REJECTED_EMPTY,
    Verdict.REJECTED_IMPLAUSIBLE_ADJUSTMENT,
    Verdict.REJECTED_PROVENANCE_FAILED,
}


@dataclass
class Provenance:
    """Where a component's value was actually read from.

    A reference string is a *name*. Evidence is a name plus the row it was read
    back from at verification time. Without this, "verified" means "the model
    quoted an id that appears in a list we sent it", which is not verification.
    """

    table: str
    row_id: str
    field: str
    value_paise: int


@dataclass
class Component:
    """One named part of a proposed explanation."""

    kind: str  # settlement_batch | settlement_line | bank_charge | adjustment
    ref: str
    amount_paise: int
    note: str = ""
    #: Filled by the verifier, never by the proposer.
    provenance: Optional[Provenance] = None

    @property
    def is_evidenced(self) -> bool:
        return self.provenance is not None


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
    #: Components the model invented rather than cited.
    unevidenced: list[str] = field(default_factory=list)
    #: Other component sets that satisfy the same residual, if any.
    rival_explanations: int = 0

    @property
    def accepted(self) -> bool:
        """True only for a fully evidenced resolution.

        Deliberately excludes HYPOTHESIS. Callers that want "did the model say
        something useful" should ask for the verdict; callers that want "may I
        close this" get one honest boolean.
        """
        return self.verdict is Verdict.RESOLVED_VERIFIED

    @property
    def is_hypothesis(self) -> bool:
        return self.verdict is Verdict.HYPOTHESIS


#: An unmodelled component is only credible at the scale banks actually charge.
#: A proposal balancing itself with a ₹40,000 "miscellaneous adjustment" is
#: fitting the number, not explaining it.
MAX_UNMODELLED_COMPONENT_PAISE = 50_000  # ₹500

_CITED_KINDS = {"settlement_batch", "settlement_line"}
_UNMODELLED_KINDS = {"bank_charge", "adjustment"}
_KNOWN_KINDS = _CITED_KINDS | _UNMODELLED_KINDS

#: Signature of a provenance lookup: (kind, ref) -> Provenance or None.
ProvenanceLookup = Callable[[str, str], Optional[Provenance]]


def verify(
    proposal: Proposal,
    *,
    known_refs: dict[str, int],
    already_matched: set[str] | None = None,
    lookup: ProvenanceLookup | None = None,
    rival_explanations: int = 0,
) -> VerificationResult:
    """Check a proposal against arithmetic, against citations, and against reality.

    ``known_refs`` maps every reference the proposal may cite to its true amount.
    ``lookup`` reads a cited reference back out of the ledger; when supplied, a
    component that cannot be read back fails rather than being trusted.
    ``rival_explanations`` is how many *other* distinct component sets the
    caller found that also satisfy this residual — any rival means the
    arithmetic does not identify a unique explanation.
    """
    matched = already_matched or set()

    if not proposal.components:
        return VerificationResult(
            verdict=Verdict.REJECTED_EMPTY,
            reason="Proposal names no components.",
            proposal=proposal,
        )

    seen: set[str] = set()
    unevidenced: list[str] = []

    for component in proposal.components:
        if component.kind not in _KNOWN_KINDS:
            return VerificationResult(
                verdict=Verdict.REJECTED_UNKNOWN_COMPONENT,
                reason=(
                    f"Component kind '{component.kind}' is not one this ledger has. "
                    f"Expected one of: {', '.join(sorted(_KNOWN_KINDS))}."
                ),
                proposal=proposal,
            )

        if component.kind in _UNMODELLED_KINDS:
            # Nothing to look up: by definition this is not in any table. It
            # must at least be small enough to be a real bank charge.
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
            unevidenced.append(f"{component.kind}:{component.ref}")
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

        # The cited amount must be the record's real amount, not one chosen to
        # make the sum work. This is the subtle attack: a doctored amount can
        # produce a correct total, so arithmetic alone passes it.
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

        # Read the value back out of the ledger. A citation nobody can look up
        # is a name, not evidence.
        if lookup is not None:
            provenance = lookup(component.kind, component.ref)
            if provenance is None or provenance.value_paise != component.amount_paise:
                return VerificationResult(
                    verdict=Verdict.REJECTED_PROVENANCE_FAILED,
                    reason=(
                        f"'{component.ref}' could not be read back from the ledger "
                        "at verification time."
                    ),
                    proposal=proposal,
                )
            component.provenance = provenance

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
            unevidenced=unevidenced,
        )

    # Arithmetic holds. Now: is it *evidence*, or merely consistent?
    if unevidenced:
        return VerificationResult(
            verdict=Verdict.HYPOTHESIS,
            reason=(
                f"Sums exactly, but {len(unevidenced)} component(s) are not in any "
                f"record and cannot be evidenced: {', '.join(unevidenced)}. "
                "Plausible, not proven — a human decides."
            ),
            proposal=proposal,
            unevidenced=unevidenced,
        )

    if rival_explanations:
        return VerificationResult(
            verdict=Verdict.HYPOTHESIS,
            reason=(
                f"Sums exactly, but {rival_explanations} other combination(s) of real "
                "records also explain this residual. Arithmetic does not identify "
                "which one actually happened."
            ),
            proposal=proposal,
            rival_explanations=rival_explanations,
        )

    return VerificationResult(
        verdict=Verdict.RESOLVED_VERIFIED,
        reason=(
            f"{len(proposal.components)} component(s), every one traced to a ledger "
            f"row, summing exactly to {proposal.target_paise} paise."
        ),
        proposal=proposal,
    )
