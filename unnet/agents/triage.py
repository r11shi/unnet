"""Triage agent: explaining residuals that arithmetic alone cannot.

Runs only on exceptions still open after :func:`subset_sum_resolve`. That
ordering is the point. If a bank credit is the exact sum of some unmatched
payouts, exhaustive search finds it, and asking a model instead would be slower,
costlier and less reliable at the one thing computers are already perfect at.

What search cannot do is invent a quantity that appears in no table. When a
payout of ₹8,36,364.23 arrives as ₹8,36,352.43, no combination of known records
explains the missing ₹11.80, because the bank's own NEFT charge was never in the
data. A model that has seen Indian bank statements proposes "₹10 NEFT charge
plus 18% GST" — a hypothesis, from domain knowledge, about a number that is not
in the input.

It is a hypothesis until it sums exactly. :mod:`unnet.agents.verifier` decides
that, and a proposal that misses by a single paisa is rejected and stays in the
queue for a human.
"""

from __future__ import annotations

import json

from unnet.agents.resolvers import make_lookup
from unnet.agents.untrusted import scrub_evidence
from unnet.agents.verifier import Component, Proposal, Verdict, verify
from unnet.core.models import DecidedBy, ExceptionCode, ExceptionStatus
from unnet.engine.context import ReconContext
from unnet.llm.provider import LLMClient, LLMUnavailable

#: Codes worth a model call. Others are either already explained or need a
#: human decision no model should pre-empt (a chargeback is a business dispute,
#: not a matching problem).
TRIAGE_CODES = {
    ExceptionCode.SHORT_CREDIT,
    ExceptionCode.OVER_CREDIT,
    ExceptionCode.UNMATCHED_BANK_CREDIT,
    ExceptionCode.MISSING_BANK_CREDIT,
}

SCHEMA = {
    "type": "object",
    "properties": {
        "components": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "kind": {
                        "type": "string",
                        "enum": ["settlement_batch", "settlement_line", "bank_charge", "adjustment"],
                    },
                    "ref": {"type": "string"},
                    "amount_paise": {"type": "integer"},
                    "note": {"type": "string"},
                },
                "required": ["kind", "ref", "amount_paise"],
            },
        },
        "reasoning": {"type": "string"},
        "confidence": {"type": "integer"},
    },
    "required": ["components", "reasoning"],
}

PROMPT = """\
An Indian merchant is reconciling a Razorpay payout against their bank statement
and one amount does not add up.

Exception: {code}
{summary}

Amount to explain: {target_paise} paise (all amounts are integer paise).

Evidence:
{evidence}

Records still unreconciled that you may cite:
{candidates}

Propose components that sum to EXACTLY {target_paise} paise.

- To cite a real record, use kind "settlement_batch" with its exact id and its
  exact amount as listed. Do not alter a listed amount to make your sum work.
- For a cost that is in no record — a bank NEFT or IMPS charge, GST on that
  charge, an FX spread — use kind "bank_charge" or "adjustment" with a short
  ref naming it. These may not exceed 50000 paise each.
- Indian banks commonly charge ₹10-₹25 plus 18% GST for an inward NEFT.
- If nothing you can name sums exactly, return an empty components list. An
  honest "I cannot explain this" is the correct answer and is preferred over a
  plausible guess. Your proposal is checked arithmetically and a sum that is off
  by even one paisa will be rejected.
"""


class TriageAgent:
    def __init__(self, client: LLMClient) -> None:
        self.client = client
        self.attempted = 0
        self.proposed = 0
        self.accepted = 0
        self.hypotheses = 0
        self.rejected = 0

    def run(self, ctx: ReconContext, *, limit: int = 25) -> int:
        """Attempt the open residuals. Returns how many were accepted."""
        candidates = [
            e
            for e in ctx.exceptions
            if e.code in TRIAGE_CODES and e.status == ExceptionStatus.OPEN
        ][:limit]

        for exception in candidates:
            self._triage_one(ctx, exception)
        return self.accepted

    def _triage_one(self, ctx: ReconContext, exception) -> None:
        self.attempted += 1

        unmatched_batches = [
            b
            for b in ctx.batches
            if not ctx.is_claimed("settlement_batch", b.settlement_id)
        ]
        known_refs = {b.settlement_id: b.reported_amount_paise for b in ctx.batches}

        target = abs(exception.residual_paise)
        prompt = PROMPT.format(
            code=exception.code.value,
            summary=exception.summary,
            target_paise=target,
            evidence=json.dumps(scrub_evidence(exception.evidence), indent=2, default=str),
            candidates=json.dumps(
                [
                    {
                        "kind": "settlement_batch",
                        "ref": b.settlement_id,
                        "amount_paise": b.reported_amount_paise,
                        "settled_at": b.settled_at.isoformat() if b.settled_at else None,
                    }
                    for b in unmatched_batches[:20]
                ],
                indent=2,
            ),
        )

        try:
            response = self.client.complete("exception_triage", prompt, SCHEMA)
        except LLMUnavailable as exc:
            exception.verifier_verdict = "not_attempted"
            exception.verifier_reason = f"No model available: {exc}"
            return

        raw_components = response.data.get("components") or []
        if not raw_components:
            # The model declined, which the prompt explicitly asks for when
            # nothing sums. Recorded as an honest abstention, not a failure.
            exception.verifier_verdict = "abstained"
            exception.verifier_reason = str(response.data.get("reasoning", ""))[:400]
            self._audit(ctx, exception, response, "abstained")
            return

        proposal = Proposal(
            subject_kind=exception.subject_kind,
            subject_id=exception.subject_id,
            target_paise=target,
            components=[
                Component(
                    kind=str(c.get("kind", "")),
                    ref=str(c.get("ref", "")),
                    amount_paise=int(c.get("amount_paise", 0)),
                    note=str(c.get("note", "")),
                )
                for c in raw_components
                if isinstance(c, dict)
            ],
            reasoning=str(response.data.get("reasoning", ""))[:1000],
            produced_by=f"model:{response.decider_ref}",
            confidence=int(response.data.get("confidence") or 600),
        )
        self.proposed += 1

        result = verify(
            proposal,
            known_refs=known_refs,
            already_matched=ctx.claimed["settlement_batch"],
            lookup=make_lookup(ctx),
        )

        exception.proposal = {
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
        exception.verifier_verdict = result.verdict.value
        exception.verifier_reason = result.reason

        if result.accepted:
            # Every component traced back to a ledger row. Safe to close.
            exception.status = ExceptionStatus.AI_RESOLVED
            self.accepted += 1
        elif result.verdict is Verdict.HYPOTHESIS:
            # The arithmetic works but the explanation rests on something we
            # cannot evidence — typically an invented bank charge. This is the
            # common case for a residual, and it is genuinely useful: a human
            # gets a specific, checkable starting point instead of a bare
            # number. It is not a resolution and is never counted as one.
            exception.status = ExceptionStatus.AI_HYPOTHESIS
            self.hypotheses += 1
        else:
            # Stays in the queue. A rejected proposal is kept, not discarded:
            # the analyst who picks this up should see what was tried and why
            # it did not hold.
            exception.status = ExceptionStatus.AI_REJECTED
            self.rejected += 1

        self._audit(ctx, exception, response, result.verdict.value)

    def _audit(self, ctx: ReconContext, exception, response, verdict: str) -> None:
        if not ctx.audit:
            return
        ctx.audit.record(
            stage="triage",
            subject_kind=exception.subject_kind,
            subject_id=exception.subject_id,
            decision=f"{exception.code.value} -> {verdict}",
            decided_by=DecidedBy.MODEL,
            decider_ref=response.decider_ref,
            confidence=(exception.proposal or {}).get("confidence", 0),
            evidence=exception.proposal or {"reasoning": exception.verifier_reason},
            verifier_result=verdict,
        )
