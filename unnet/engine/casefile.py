"""Turning an exception into something a person can actually action.

The Finance Controller brief asks for an agent that **closes one finance-ops
loop**. Detecting a break and printing it is not closing a loop — it is the
first quarter of one. A loop closes when the break is investigated, packaged,
routed to whoever can fix it, and then *tracked until it goes away*.

So every unresolved exception becomes a case file with four things:

* an **owner** — the party who can actually resolve it (Razorpay support, the
  bank, the risk team, internal finance). "Someone should look at this" is not
  a workflow.
* an **action** — what that owner is being asked to do.
* an **evidence pack** — the specific rows that justify the ask, so nobody has
  to re-derive it.
* a **stable key** — so the next run recognises the same case rather than
  raising it again, and can see it has since been settled.

That last point is what makes it a loop rather than a report. Run 1 raises and
routes; run 2 sees what is still outstanding, what has been fixed, and what is
new. Without cross-run identity you have a very tidy way of printing the same
130 problems every morning.

On wording: nothing here is "recovered". The output is money **identified** as
claimable, at risk, or needing a bookkeeping correction. Recovery happens when
the bank credits it back, which is not an event this system can observe.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from unnet.core.models import ExceptionCode, ExceptionStatus
from unnet.core.money import format_inr


class Owner:
    """Who can actually fix this. Not a team name — a resolution path."""

    RAZORPAY_SUPPORT = "razorpay_support"
    RAZORPAY_RISK = "razorpay_risk"
    BANK = "bank"
    MERCHANT_OPS = "merchant_ops"
    FINANCE_OPS = "finance_ops"
    ENGINEERING = "engineering"


class Impact:
    """How the money is at stake. Kept separate so nothing is double-counted.

    Conflating these is the standard overclaim: a chargeback and a mis-billed
    fee are both "financial impact", but one is money already gone and the other
    is money a supplier owes you. Summing them into a single headline is how a
    demo turns into a lie.
    """

    #: A specific counterparty owes this back. A claim can be filed today.
    CLAIMABLE = "claimable"
    #: Real money whose whereabouts are unresolved. Not yet lost, not yet safe.
    AT_RISK = "at_risk"
    #: No money moves; the books are wrong and need correcting.
    BOOKKEEPING = "bookkeeping"
    #: Money already gone unless someone contests it.
    CONTESTABLE_LOSS = "contestable_loss"


@dataclass(frozen=True)
class Route:
    owner: str
    impact: str
    action: str
    #: Rendered into the draft message, so the ask is specific.
    template: str


#: Deliberately a table, not a model call. The owner of a FEE_MISMATCH is always
#: Razorpay support; asking a model to decide that would spend a token to be
#: less reliable than a dict.
ROUTES: dict[ExceptionCode, Route] = {
    ExceptionCode.FEE_MISMATCH: Route(
        Owner.RAZORPAY_SUPPORT, Impact.CLAIMABLE,
        "Claim the MDR overcharge against the contracted rate card.",
        "MDR on {subject} was billed {charged} against a rate card of {expected} "
        "({rate_bps} bps on {amount}). Requesting a credit of {residual}.",
    ),
    ExceptionCode.GST_MISMATCH: Route(
        Owner.RAZORPAY_SUPPORT, Impact.CLAIMABLE,
        "Correct the GST line so input tax credit can be claimed.",
        "GST on {subject} is {charged}; 18% of the {mdr} MDR is {expected}. "
        "The invoice needs correcting before this ITC can be claimed.",
    ),
    ExceptionCode.SHORT_CREDIT: Route(
        Owner.BANK, Impact.CLAIMABLE,
        "Raise the inward credit shortfall, citing the UTR.",
        "Payout {subject} was {expected} but only {charged} was credited on "
        "{value_date}. Shortfall {residual}. UTR {utr}.",
    ),
    ExceptionCode.OVER_CREDIT: Route(
        Owner.BANK, Impact.AT_RISK,
        "Confirm the excess credit before it is clawed back.",
        "Payout {subject} was {expected} but {charged} was credited. "
        "Excess {residual} — confirm before it is reversed.",
    ),
    ExceptionCode.ON_HOLD: Route(
        Owner.RAZORPAY_RISK, Impact.AT_RISK,
        "Chase release of the risk hold.",
        "Payment for order {subject} ({residual}) is on risk hold and is in no "
        "payout. Requesting the reason and an expected release date.",
    ),
    ExceptionCode.CHARGEBACK_DEDUCTION: Route(
        Owner.MERCHANT_OPS, Impact.CONTESTABLE_LOSS,
        "Contest with evidence, or accept and write off.",
        "Chargeback on {subject} deducted {residual} including dispute fees. "
        "Decide whether to contest before the representment window closes.",
    ),
    ExceptionCode.UNMATCHED_BANK_CREDIT: Route(
        Owner.FINANCE_OPS, Impact.AT_RISK,
        "Identify which payout this credit belongs to.",
        "A credit of {residual} on {value_date} looks like a Razorpay payout but "
        "ties to no settlement. Narration: {narration}",
    ),
    ExceptionCode.MISSING_BANK_CREDIT: Route(
        Owner.FINANCE_OPS, Impact.AT_RISK,
        "Trace the payout that never arrived.",
        "Payout {subject} of {residual} was reported settled but never reached "
        "the bank. UTR {utr}.",
    ),
    ExceptionCode.ORPHAN_SETTLEMENT_LINE: Route(
        Owner.FINANCE_OPS, Impact.BOOKKEEPING,
        "Find or create the missing order record.",
        "Razorpay settled {subject} for {residual} with no matching order in the "
        "ledger. The sale exists; our books do not show it.",
    ),
    ExceptionCode.UNSETTLED_ORDER: Route(
        Owner.FINANCE_OPS, Impact.AT_RISK,
        "Establish why this capture never settled.",
        "Order {subject} ({residual}) was captured but appears in no settlement.",
    ),
    ExceptionCode.REFUND_WITHOUT_ORIGINAL: Route(
        Owner.FINANCE_OPS, Impact.AT_RISK,
        "Locate the payment this refund reverses.",
        "A refund of {residual} was deducted for a payment absent from this "
        "dataset ({subject}). Confirm it is ours before accepting the deduction.",
    ),
    ExceptionCode.PARTIAL_REFUND_SPLIT: Route(
        Owner.FINANCE_OPS, Impact.BOOKKEEPING,
        "Reconcile one booked refund against several settlement lines.",
        "One refund is reported as multiple settlement lines ({subject}). "
        "No money is missing; the books need the split reflected.",
    ),
    ExceptionCode.DUPLICATE: Route(
        Owner.FINANCE_OPS, Impact.BOOKKEEPING,
        "Remove the duplicate row from the ledger export.",
        "Order {subject} appears more than once in the ledger, overstating "
        "revenue by {residual}.",
    ),
    ExceptionCode.ROUNDING: Route(
        Owner.FINANCE_OPS, Impact.BOOKKEEPING,
        "Post a rounding adjustment.",
        "Payout {subject} differs from the credit by {residual} — rounding, not "
        "a deduction.",
    ),
    ExceptionCode.SCHEMA_UNPARSEABLE: Route(
        Owner.ENGINEERING, Impact.BOOKKEEPING,
        "The source report is internally inconsistent; investigate the export.",
        "{subject}: the settlement report disagrees with itself. {residual} "
        "unexplained between its own columns.",
    ),
}

#: A timing break is not a case. It resolves itself when the money lands, and
#: opening a ticket for it every morning is how people learn to ignore tickets.
NO_CASE_STATUSES = {ExceptionStatus.ROLLED_FORWARD}
NO_CASE_CODES = {ExceptionCode.TIMING_DIFFERENCE}


@dataclass
class CaseFile:
    """One actionable item, stable across runs."""

    case_key: str
    code: str
    subject_kind: str
    subject_id: str
    owner: str
    impact: str
    action: str
    message: str
    amount_paise: int
    status: str = "open"  # open | routed | resolved
    evidence: dict = field(default_factory=dict)
    hypothesis: dict | None = None
    first_seen_run: str = ""
    last_seen_run: str = ""
    resolved_run: str = ""

    @property
    def amount_display(self) -> str:
        return format_inr(self.amount_paise)


def case_key(code: str, subject_kind: str, subject_id: str) -> str:
    """Identity that survives across runs.

    Deliberately derived from *what the problem is about* rather than from a row
    id, because every run re-parses the source files and allocates new row ids.
    Two runs of the same broken payout must produce the same key or the loop
    never closes.
    """
    raw = f"{code}|{subject_kind}|{subject_id}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def build_cases(ctx, run_id: str, previous: dict[str, CaseFile] | None = None) -> list[CaseFile]:
    """Turn this run's unresolved exceptions into routed, tracked case files.

    ``previous`` is the previous run's cases keyed by ``case_key``. Anything
    already resolved there stays resolved; anything still open keeps its
    original first-seen run so ageing is real.
    """
    prior = previous or {}
    cases: list[CaseFile] = []

    for exception in ctx.exceptions:
        if exception.code in NO_CASE_CODES or exception.status in NO_CASE_STATUSES:
            continue
        # Anything the run genuinely closed needs no owner.
        if exception.status in {
            ExceptionStatus.AUTO_RESOLVED,
            ExceptionStatus.AI_RESOLVED,
            ExceptionStatus.ACCEPTED_BY_HUMAN,
        }:
            continue

        route = ROUTES.get(exception.code)
        if route is None:
            continue

        key = case_key(exception.code.value, exception.subject_kind, exception.subject_id)
        known = prior.get(key)

        # A case a human already settled does not come back just because the
        # underlying rows still look the same.
        if known is not None and known.status == "resolved":
            known.last_seen_run = run_id
            cases.append(known)
            continue

        evidence = dict(exception.evidence or {})
        cases.append(
            CaseFile(
                case_key=key,
                code=exception.code.value,
                subject_kind=exception.subject_kind,
                subject_id=exception.subject_id,
                owner=route.owner,
                impact=route.impact,
                action=route.action,
                message=_render(route.template, exception, evidence),
                amount_paise=abs(exception.residual_paise),
                status="routed",
                evidence=evidence,
                hypothesis=(
                    exception.proposal
                    if exception.status == ExceptionStatus.AI_HYPOTHESIS
                    else None
                ),
                first_seen_run=known.first_seen_run if known else run_id,
                last_seen_run=run_id,
            )
        )

    cases.sort(key=lambda c: -c.amount_paise)
    return cases


def _render(template: str, exception, evidence: dict) -> str:
    """Fill a routing template from the exception's own evidence.

    Missing keys render as ``—`` rather than raising: an evidence pack that is
    thin still produces a usable message, and a KeyError in the middle of a
    reconciliation run is a bad trade for a prettier sentence.
    """
    values = {
        "subject": exception.subject_id,
        "residual": format_inr(abs(exception.residual_paise)),
        "charged": format_inr(
            evidence.get("charged_mdr_paise")
            or evidence.get("bank_credit_paise")
            or evidence.get("charged_gst_paise")
            or 0
        ),
        "expected": format_inr(
            evidence.get("expected_mdr_paise")
            or evidence.get("batch_amount_paise")
            or evidence.get("expected_gst_paise")
            or 0
        ),
        "mdr": format_inr(evidence.get("mdr_paise") or 0),
        "amount": format_inr(evidence.get("amount_paise") or 0),
        "rate_bps": evidence.get("rate_bps", "—"),
        "utr": evidence.get("settlement_utr") or evidence.get("utr") or "—",
        "value_date": (evidence.get("value_date") or "—")[:10],
        "narration": str(evidence.get("narration") or "—")[:160],
    }

    class _Safe(dict):
        def __missing__(self, key: str) -> str:  # noqa: D105
            return "—"

    return template.format_map(_Safe(values))


def summarise(cases: list[CaseFile]) -> dict:
    """Headline numbers, split by how the money is at stake.

    Never summed into one figure. "Identified" is not "recovered", and a
    chargeback already lost is not the same rupee as a fee a supplier owes back.
    """
    by_impact: dict[str, dict] = {}
    by_owner: dict[str, dict] = {}

    for case in cases:
        if case.status == "resolved":
            continue
        impact = by_impact.setdefault(case.impact, {"count": 0, "paise": 0})
        impact["count"] += 1
        impact["paise"] += case.amount_paise

        owner = by_owner.setdefault(case.owner, {"count": 0, "paise": 0})
        owner["count"] += 1
        owner["paise"] += case.amount_paise

    return {
        "open_cases": sum(1 for c in cases if c.status != "resolved"),
        "resolved_cases": sum(1 for c in cases if c.status == "resolved"),
        "by_impact": by_impact,
        "by_owner": by_owner,
        "claimable_paise": by_impact.get(Impact.CLAIMABLE, {}).get("paise", 0),
        "at_risk_paise": by_impact.get(Impact.AT_RISK, {}).get("paise", 0),
        "bookkeeping_paise": by_impact.get(Impact.BOOKKEEPING, {}).get("paise", 0),
        "contestable_loss_paise": by_impact.get(Impact.CONTESTABLE_LOSS, {}).get("paise", 0),
    }


# --------------------------------------------------------------------------- #
# Persistence. Without this the loop cannot close: identity has to outlive the
# process that created it.
# --------------------------------------------------------------------------- #


def load_previous(session) -> dict[str, CaseFile]:
    """The most recent state of every case this account has ever opened.

    Read across all runs rather than just the last one, keyed by ``case_key``,
    so a case resolved three runs ago is still known to be resolved even if the
    intervening runs never saw it.
    """
    from sqlmodel import select

    from unnet.core.models import CaseFileRow

    rows = session.exec(select(CaseFileRow).order_by(CaseFileRow.id)).all()
    latest: dict[str, CaseFile] = {}
    for row in rows:
        latest[row.case_key] = CaseFile(
            case_key=row.case_key,
            code=row.code,
            subject_kind=row.subject_kind,
            subject_id=row.subject_id,
            owner=row.owner,
            impact=row.impact,
            action=row.action,
            message=row.message,
            amount_paise=row.amount_paise,
            status=row.status,
            evidence=row.evidence or {},
            hypothesis=row.hypothesis,
            first_seen_run=row.first_seen_run,
            last_seen_run=row.last_seen_run,
            resolved_run=row.resolved_run,
        )
    return latest


def persist(session, cases: list[CaseFile], run_id: str) -> None:
    from unnet.core.models import CaseFileRow

    for case in cases:
        session.add(
            CaseFileRow(
                run_id=run_id,
                case_key=case.case_key,
                code=case.code,
                subject_kind=case.subject_kind,
                subject_id=case.subject_id,
                owner=case.owner,
                impact=case.impact,
                action=case.action,
                message=case.message,
                amount_paise=case.amount_paise,
                status=case.status,
                evidence=case.evidence,
                hypothesis=case.hypothesis,
                first_seen_run=case.first_seen_run,
                last_seen_run=case.last_seen_run,
                resolved_run=case.resolved_run,
            )
        )


def resolve(session, case_key_value: str, run_id: str, note: str = "") -> int:
    """Mark a case settled. Returns how many rows were updated.

    Writes a new row rather than mutating history: the trail should show that a
    case was open and then became resolved, not that it was always resolved.
    """
    from sqlmodel import select

    from unnet.core.models import CaseFileRow

    rows = session.exec(
        select(CaseFileRow).where(CaseFileRow.case_key == case_key_value)
    ).all()
    if not rows:
        return 0

    latest = rows[-1]
    session.add(
        CaseFileRow(
            run_id=run_id,
            case_key=latest.case_key,
            code=latest.code,
            subject_kind=latest.subject_kind,
            subject_id=latest.subject_id,
            owner=latest.owner,
            impact=latest.impact,
            action=latest.action,
            message=latest.message,
            amount_paise=latest.amount_paise,
            status="resolved",
            evidence=latest.evidence,
            hypothesis=latest.hypothesis,
            first_seen_run=latest.first_seen_run,
            last_seen_run=run_id,
            resolved_run=run_id,
            resolved_note=note,
        )
    )
    return 1
