"""End-to-end behaviour of the matching engine against the held-out truth.

These run the real pipeline over the committed fixtures. They are slower than
unit tests and worth it: the bugs that actually mattered here — a UTR match not
checking the amount, held lines being matched as settled, duplicates blocking
their own match — were all invisible to unit tests and obvious the moment the
whole run was scored.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import pytest

from unnet.core.models import EntityType, ExceptionCode, ExceptionStatus, MatchTier
from unnet.engine.pipeline import SourcePaths, reconcile
from unnet.evaluation.score import score

FIXTURES = Path("data/synthetic")
MESSY = Path("data/synthetic_messy")

pytestmark = pytest.mark.skipif(
    not (FIXTURES / "ground_truth.json").exists(),
    reason="fixtures not generated; run `make gen`",
)


@pytest.fixture(scope="module")
def run():
    return reconcile(SourcePaths.synthetic(FIXTURES), ai_enabled=False, label="test")


@pytest.fixture(scope="module")
def truth():
    return json.loads((FIXTURES / "ground_truth.json").read_text())


@pytest.fixture(scope="module")
def report(run, truth):
    return score(run, truth)


def test_never_makes_a_wrong_link(report):
    """The metric that matters. A missed link costs minutes; a wrong one costs
    a restated ledger."""
    assert report.total_links_wrong == 0, report.links


def test_finds_every_link_that_exists(report):
    assert report.auto_match_rate == pytest.approx(1.0)


def test_every_payout_nets_to_the_paisa(run):
    """Both derivations of every batch must agree, exactly."""
    for breakdown in run.netting.breakdowns:
        assert breakdown.internally_consistent, breakdown.settlement_id
        assert breakdown.computed_net_paise == breakdown.reported_net_paise


def test_reports_every_injected_defect_class(report, truth):
    expected = {e["code"] for e in truth["expected_exceptions"]}
    reported = {s.code for s in report.exceptions if s.reported}
    missing = expected - reported
    assert not missing, f"injected but never reported: {missing}"


def test_exception_subjects_are_right_not_just_the_counts(report, truth):
    """Reporting the right number of exceptions about the wrong records is not
    a pass."""
    for item in report.exceptions:
        if item.expected:
            assert item.matched == item.expected, item.code


def test_a_held_payment_is_never_reported_as_settled(run):
    """Money frozen by risk is not money that arrived."""
    ctx = run.ctx
    held_orders = {
        e.subject_id for e in ctx.exceptions if e.code == ExceptionCode.ON_HOLD
    }
    assert held_orders, "fixture should contain risk holds"

    matched_orders = {
        m.right_id for m in ctx.matches if m.tier == MatchTier.TIER2_LINE_TO_ORDER
    }
    assert not (held_orders & matched_orders)


def test_a_timing_break_is_rolled_forward_not_called_an_error(run):
    """A payout initiated yesterday has not gone missing. Reporting it as an
    error every morning is how a recon tool trains people to ignore it."""
    rolled = [
        e
        for e in run.ctx.exceptions
        if e.code == ExceptionCode.TIMING_DIFFERENCE
    ]
    assert rolled
    assert all(e.status == ExceptionStatus.ROLLED_FORWARD for e in rolled)


def test_sub_rupee_drift_is_rounding_not_a_short_credit(run):
    for exception in run.ctx.exceptions:
        if exception.code == ExceptionCode.ROUNDING:
            assert abs(exception.residual_paise) <= 5
        if exception.code == ExceptionCode.SHORT_CREDIT:
            assert abs(exception.residual_paise) > 5


def test_a_short_credit_is_caught_even_when_the_utr_matched(run):
    """Regression: a UTR match asserts identity, not amount. An earlier version
    checked the residual only on the fuzzy path, so the most common real case —
    correct UTR, bank took its own charge — reconciled silently."""
    shorts = [
        e
        for e in run.ctx.exceptions
        if e.code in {ExceptionCode.SHORT_CREDIT, ExceptionCode.ROUNDING}
    ]
    assert shorts
    matched_by = {(e.evidence or {}).get("matched_by") for e in shorts}
    # At least one of them was found on a non-fuzzy path.
    assert matched_by - {"T1.AMOUNT_NEAR", None}


def test_a_duplicated_row_costs_exactly_one_match_not_two(run):
    """Two rows sharing an order id make every lookup ambiguous. Collapsing to a
    canonical row keeps the real settlement line out of the orphan pile."""
    ctx = run.ctx
    duplicated = {
        e.subject_id for e in ctx.exceptions if e.code == ExceptionCode.DUPLICATE
    }
    assert duplicated

    matched = {
        m.right_id for m in ctx.matches if m.tier == MatchTier.TIER2_LINE_TO_ORDER
    }
    # Every duplicated order that had a settlement line still got matched once.
    with_lines = {
        line.order_id
        for line in ctx.lines
        if line.type == EntityType.PAYMENT and line.order_id in duplicated
        and not line.on_hold
    }
    assert with_lines <= matched


def test_one_exception_per_order_even_when_two_things_are_wrong(run):
    """An order can be duplicated *and* on risk hold. Both are reported, but
    each exactly once."""
    counts = Counter(
        (e.code, e.subject_id)
        for e in run.ctx.exceptions
        if e.subject_kind == "merchant_order"
    )
    assert all(n == 1 for n in counts.values())


def test_unrelated_bank_activity_is_not_a_reconciliation_break(run):
    """The merchant's electricity bill is not our problem."""
    flagged = {
        e.subject_id
        for e in run.ctx.exceptions
        if e.code == ExceptionCode.UNMATCHED_BANK_CREDIT
    }
    noise = [
        t
        for t in run.ctx.bank_txns
        if t.debit_paise > 0 and "razorpay" not in t.narration.lower()
    ]
    assert noise, "fixture should contain unrelated bank rows"
    assert not ({t.bank_ref for t in noise} & flagged)


def test_a_consolidated_credit_is_decomposed_by_exact_search(run):
    """Two payouts in one bank line. No matching rule can close it; exact
    subset-sum can, and does so without a model."""
    resolved = [
        e
        for e in run.ctx.exceptions
        if e.code == ExceptionCode.UNMATCHED_BANK_CREDIT
        and e.status == ExceptionStatus.AUTO_RESOLVED
    ]
    assert resolved, "the consolidated-credit fixture should be closed"

    exception = resolved[0]
    assert exception.verifier_verdict == "accepted"
    components = exception.proposal["components"]
    assert len(components) >= 2
    assert sum(c["amount_paise"] for c in components) == exception.proposal["target_paise"]


def test_every_decision_reaches_the_audit_trail(run):
    """Nothing may reach the output without also reaching the trail."""
    from unnet.core.db import AuditLog, make_engine, session_scope

    engine = make_engine("data/test_audit.db")
    with session_scope(engine) as session:
        audit = AuditLog(session, "audit-test")
        result = reconcile(
            SourcePaths.synthetic(FIXTURES),
            run_id="audit-test",
            audit=audit,
            ai_enabled=False,
        )
        entries = len(session.new)

    # One entry per match, one per exception, plus the four ingest decisions.
    assert entries >= len(result.ctx.matches) + len(result.ctx.exceptions)
    Path("data/test_audit.db").unlink(missing_ok=True)


@pytest.mark.skipif(
    not (MESSY / "ground_truth.json").exists(),
    reason="messy fixtures not generated; run `make gen`",
)
def test_precision_survives_losing_the_identifiers():
    """With 35% of gateway ids gone, recall must fall and precision must not.

    This is the whole design thesis in one assertion: when the engine cannot
    tell two candidates apart it refuses, rather than picking the nearest and
    producing a prettier number.
    """
    result = reconcile(SourcePaths.synthetic(MESSY), ai_enabled=False)
    truth = json.loads((MESSY / "ground_truth.json").read_text())
    report = score(result, truth)

    assert report.auto_match_rate < 0.9, "fixture should genuinely be harder"
    assert report.false_match_rate < 0.01, (
        f"precision collapsed under ambiguity: {report.false_match_rate:.2%}"
    )
