"""Closing the exceptions the matching tiers left open.

Two resolvers run in order, and the order is the whole argument:

1. :func:`subset_sum_resolve` — exact, bounded, deterministic. If a bank credit
   is the sum of some combination of unmatched payouts, arithmetic finds it and
   no model is needed. This handles the consolidated-credit case outright.

2. :func:`model_resolve` (``unnet.agents.triage``) — only consulted when step 1
   finds nothing, because the explanation requires a quantity that is in no
   table: a bank charge, an FX spread, a fee nobody documented. Search cannot
   find those, since it does not know what it is searching for.

Both produce a :class:`~unnet.agents.verifier.Proposal` and both go through the
same verifier. Nothing reaches the ledger because of who proposed it.
"""

from __future__ import annotations

from itertools import combinations

from unnet.agents.verifier import Component, Proposal, verify
from unnet.core.models import DecidedBy, ExceptionCode, ExceptionStatus, MatchTier
from unnet.engine.context import ReconContext

#: Widest combination the exact search will consider. A bank consolidating more
#: than this into one line is not something we should be guessing at, and the
#: search cost grows as C(n, k).
MAX_COMBINATION_SIZE = 4
#: Cap on candidate payouts per credit, after date filtering.
MAX_CANDIDATES = 24
#: How far either side of a credit to look for the payouts inside it.
CONSOLIDATION_WINDOW_DAYS = 6.0


def subset_sum_resolve(ctx: ReconContext) -> int:
    """Explain unmatched bank credits as exact sums of unmatched payouts.

    Returns the number of exceptions closed.

    The search is deliberately kept exact rather than nearest-fit. A credit that
    is *nearly* the sum of three payouts is not evidence that it is those three
    payouts; it is evidence that something else is going on, and that belongs in
    the exception queue where a person will look at it.
    """
    closed = 0
    bank_by_ref = {t.bank_ref: t for t in ctx.bank_txns}

    open_credit_exceptions = [
        e
        for e in ctx.exceptions
        if e.code == ExceptionCode.UNMATCHED_BANK_CREDIT
        and e.status == ExceptionStatus.OPEN
    ]

    for exception in open_credit_exceptions:
        txn = bank_by_ref.get(exception.subject_id)
        if txn is None or ctx.is_claimed("bank_txn", txn.bank_ref):
            continue

        candidates = [
            b
            for b in ctx.batches
            if not ctx.is_claimed("settlement_batch", b.settlement_id)
            and _within(txn, b)
        ]
        # Nearest in time first, so the cap keeps the plausible ones.
        candidates.sort(key=lambda b: abs(_gap(txn, b) or 0))
        candidates = candidates[:MAX_CANDIDATES]

        combo = _find_exact_combination(txn.credit_paise, candidates)
        if combo is None:
            continue

        proposal = Proposal(
            subject_kind="bank_txn",
            subject_id=txn.bank_ref,
            target_paise=txn.credit_paise,
            components=[
                Component(
                    kind="settlement_batch",
                    ref=b.settlement_id,
                    amount_paise=b.reported_amount_paise,
                    note=f"payout settled {b.settled_at.date() if b.settled_at else '?'}",
                )
                for b in combo
            ],
            reasoning=(
                f"One bank credit of {txn.credit_paise} paise is the exact sum of "
                f"{len(combo)} payouts that had no credit of their own."
            ),
            produced_by="rule:SUBSET_SUM",
            confidence=950,
        )

        known = {b.settlement_id: b.reported_amount_paise for b in ctx.batches}
        result = verify(
            proposal,
            known_refs=known,
            already_matched=ctx.claimed["settlement_batch"],
        )
        if not result.accepted:
            continue

        for batch in combo:
            ctx.record_match(
                tier=MatchTier.TIER1_BANK_TO_BATCH,
                rule_id="T1.CONSOLIDATED_SUBSET_SUM",
                left_kind="bank_txn",
                left_id=txn.bank_ref,
                right_kind="settlement_batch",
                right_id=batch.settlement_id,
                amount_paise=batch.reported_amount_paise,
                confidence=950,
                decided_by=DecidedBy.RULE,
                evidence={
                    "reason": "consolidated bank credit decomposed into exact payout sum",
                    "credit_paise": txn.credit_paise,
                    "components": [b.settlement_id for b in combo],
                    "component_amounts_paise": [b.reported_amount_paise for b in combo],
                    "verifier": result.reason,
                    "narration": txn.narration,
                },
            )

        exception.status = ExceptionStatus.AUTO_RESOLVED
        exception.proposal = _proposal_dict(proposal)
        exception.verifier_verdict = result.verdict.value
        exception.verifier_reason = result.reason
        closed += 1

        # The payouts inside it are no longer missing.
        _close_missing_credit_exceptions(ctx, {b.settlement_id for b in combo})

        if ctx.audit:
            ctx.audit.record(
                stage="triage",
                subject_kind="bank_txn",
                subject_id=txn.bank_ref,
                decision=f"resolved as sum of {len(combo)} payouts",
                decided_by=DecidedBy.RULE,
                decider_ref="rule:SUBSET_SUM",
                confidence=950,
                evidence={
                    "components": [b.settlement_id for b in combo],
                    "credit_paise": txn.credit_paise,
                },
                verifier_result=result.verdict.value,
            )

    return closed


def _find_exact_combination(target_paise: int, candidates: list):
    """Smallest exact-sum combination, or ``None``.

    Searched smallest-first so a credit that is genuinely one payout is never
    explained as three that happen to add up.
    """
    for size in range(1, MAX_COMBINATION_SIZE + 1):
        if size > len(candidates):
            break
        for combo in combinations(candidates, size):
            if sum(b.reported_amount_paise for b in combo) == target_paise:
                return list(combo)
    return None


def _close_missing_credit_exceptions(ctx: ReconContext, settlement_ids: set[str]) -> None:
    for exception in ctx.exceptions:
        if (
            exception.subject_kind == "settlement_batch"
            and exception.subject_id in settlement_ids
            and exception.code
            in {ExceptionCode.MISSING_BANK_CREDIT, ExceptionCode.TIMING_DIFFERENCE}
            and exception.status
            in {ExceptionStatus.OPEN, ExceptionStatus.ROLLED_FORWARD}
        ):
            exception.status = ExceptionStatus.AUTO_RESOLVED
            exception.verifier_verdict = "accepted"
            exception.verifier_reason = (
                "Payout was inside a consolidated bank credit, not missing."
            )


def _gap(txn, batch):
    from unnet.engine.context import days_between

    return days_between(txn.value_date, batch.settled_at or batch.created_at)


def _within(txn, batch) -> bool:
    gap = _gap(txn, batch)
    return gap is not None and gap <= CONSOLIDATION_WINDOW_DAYS


def _proposal_dict(proposal: Proposal) -> dict:
    return {
        "produced_by": proposal.produced_by,
        "confidence": proposal.confidence,
        "reasoning": proposal.reasoning,
        "target_paise": proposal.target_paise,
        "components": [
            {
                "kind": c.kind,
                "ref": c.ref,
                "amount_paise": c.amount_paise,
                "note": c.note,
            }
            for c in proposal.components
        ],
    }
