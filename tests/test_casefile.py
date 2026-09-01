"""Closing the loop: route, track, and stop re-raising what was settled.

The Finance Controller track asks for an agent that *closes* a finance-ops loop.
Detecting a break is the first quarter of one. These tests cover the rest:
every unresolved exception gets an owner and an ask, identity survives across
runs, and a case a human settled does not come back the next morning.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from unnet.core.db import make_engine, session_scope
from unnet.engine import casefile
from unnet.engine.casefile import Impact, Owner, case_key
from unnet.engine.pipeline import SourcePaths, reconcile

FIXTURES = Path("data/synthetic")

pytestmark = pytest.mark.skipif(
    not (FIXTURES / "ground_truth.json").exists(), reason="run `make gen`"
)


@pytest.fixture(scope="module")
def cases():
    result = reconcile(SourcePaths.synthetic(FIXTURES), ai_enabled=False)
    return casefile.build_cases(result.ctx, "run-1")


def test_every_case_has_an_owner_and_an_ask(cases):
    """'Someone should look at this' is not a workflow."""
    assert cases
    for case in cases:
        assert case.owner, case.code
        assert case.action, case.code
        assert case.message and "{" not in case.message, case.message


def test_owners_are_the_party_who_can_actually_fix_it(cases):
    """Routing is a table, not a judgement call — so it can be asserted."""
    by_code = {c.code: c for c in cases}
    if "FEE_MISMATCH" in by_code:
        assert by_code["FEE_MISMATCH"].owner == Owner.RAZORPAY_SUPPORT
    if "ON_HOLD" in by_code:
        assert by_code["ON_HOLD"].owner == Owner.RAZORPAY_RISK
    if "SHORT_CREDIT" in by_code:
        assert by_code["SHORT_CREDIT"].owner == Owner.BANK


def test_impact_buckets_are_never_summed_into_one_number(cases):
    """A chargeback already lost and a fee a supplier owes back are not the same
    rupee. Keeping them apart is the difference between a figure and a claim."""
    summary = casefile.summarise(cases)
    assert set(summary["by_impact"]) <= {
        Impact.CLAIMABLE, Impact.AT_RISK, Impact.BOOKKEEPING, Impact.CONTESTABLE_LOSS,
    }
    # The summary exposes them separately and offers no combined total.
    assert "total_paise" not in summary


def test_a_timing_difference_never_becomes_a_case(cases):
    """It resolves itself when the money lands. Opening a ticket every morning
    is how people learn to ignore tickets."""
    assert not [c for c in cases if c.code == "TIMING_DIFFERENCE"]


def test_case_identity_is_stable_across_runs():
    """Identity must come from what the problem is, not from a row id — every
    run re-parses the source files and allocates new ids."""
    a = case_key("SHORT_CREDIT", "settlement_batch", "setl_X")
    b = case_key("SHORT_CREDIT", "settlement_batch", "setl_X")
    c = case_key("SHORT_CREDIT", "settlement_batch", "setl_Y")
    assert a == b and a != c


def test_a_resolved_case_stays_resolved_on_the_next_run(tmp_path):
    """The loop actually closing. Run 1 raises and routes; a human settles one;
    run 2 must not raise it again."""
    db = tmp_path / "loop.db"
    engine = make_engine(db)

    with session_scope(engine) as session:
        first = reconcile(SourcePaths.synthetic(FIXTURES), run_id="run-1", ai_enabled=False)
        built = casefile.build_cases(first.ctx, "run-1")
        casefile.persist(session, built, "run-1")
    before = len(built)
    target = max(built, key=lambda c: c.amount_paise)

    with session_scope(engine) as session:
        assert casefile.resolve(session, target.case_key, run_id="manual-1", note="settled") == 1

    with session_scope(engine) as session:
        previous = casefile.load_previous(session)
        second = reconcile(
            SourcePaths.synthetic(FIXTURES), run_id="run-2", ai_enabled=False
        )
        again = casefile.build_cases(second.ctx, "run-2", previous)
        casefile.persist(session, again, "run-2")

    resolved = {c.case_key for c in again if c.status == "resolved"}
    assert target.case_key in resolved, "a settled case must not be re-raised"

    summary = casefile.summarise(again)
    assert summary["open_cases"] == before - 1
    assert summary["resolved_cases"] == 1


def test_ageing_survives_the_second_run(tmp_path):
    """A case still open on run 2 keeps its original first-seen run, so ageing
    is real rather than resetting every morning."""
    engine = make_engine(tmp_path / "age.db")
    with session_scope(engine) as session:
        first = reconcile(SourcePaths.synthetic(FIXTURES), run_id="run-1", ai_enabled=False)
        built = casefile.build_cases(first.ctx, "run-1")
        casefile.persist(session, built, "run-1")

    with session_scope(engine) as session:
        previous = casefile.load_previous(session)
        second = reconcile(SourcePaths.synthetic(FIXTURES), run_id="run-2", ai_enabled=False)
        again = casefile.build_cases(second.ctx, "run-2", previous)

    still_open = [c for c in again if c.status != "resolved"]
    assert still_open
    assert all(c.first_seen_run == "run-1" for c in still_open)
    assert all(c.last_seen_run == "run-2" for c in still_open)
