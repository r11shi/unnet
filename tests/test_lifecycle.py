"""Case state, priority and ageing — all decided by rule, never by a model.

An operator sorting a queue needs the order to be stable, explicable and the
same tomorrow. "The model thought this looked urgent" is not defensible in a
review, so every rule here is a pure function and is tested as one.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from unnet.core.db import make_engine, session_scope
from unnet.engine import casefile
from unnet.engine.lifecycle import (
    CaseStatus,
    ageing_bucket,
    age_days,
    can_transition,
    initial_status,
    priority,
)
from unnet.engine.pipeline import SourcePaths, reconcile

FIXTURES = Path("data/synthetic")
NOW = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)


def test_an_unverified_hypothesis_means_we_are_still_investigating():
    """Asking a bank to act on a guess the verifier would not accept is worse
    than useless, so a case carrying one never leaves our desk."""
    assert initial_status("bank", True) == CaseStatus.INVESTIGATING
    assert initial_status("finance_ops", True) == CaseStatus.INVESTIGATING


def test_external_owners_await_action_and_internal_ones_are_routed():
    """The distinction the three-state model could not express: work we owe
    versus work someone else owes us. Different queues, different people."""
    for owner in ("bank", "razorpay_support", "razorpay_risk"):
        assert initial_status(owner, False) == CaseStatus.AWAITING_ACTION
    for owner in ("finance_ops", "merchant_ops", "engineering"):
        assert initial_status(owner, False) == CaseStatus.ROUTED


def test_priority_rises_with_money():
    assert priority(43_712_20, "contestable_loss", NOW, NOW) == "P1"
    assert priority(62_00, "claimable", NOW, NOW) == "P3"


def test_priority_weights_by_how_the_money_is_at_stake():
    """A rupee a supplier owes back is worth chasing harder than a rupee of
    bookkeeping that moves no money at all."""
    amount = 3_000_00
    assert priority(amount, "claimable", NOW, NOW) == "P2"
    assert priority(amount, "bookkeeping", NOW, NOW) == "P3"


def test_a_small_case_nobody_touched_escalates_on_age_alone():
    """Small items that sit forever are how a queue rots."""
    old = NOW - timedelta(days=20)
    assert priority(62_00, "claimable", NOW, NOW) == "P3"
    assert priority(62_00, "claimable", old, NOW) == "P1"


def test_age_escalates_but_never_de_escalates():
    """A large case does not become less urgent by getting older."""
    big_new = priority(50_000_00, "claimable", NOW, NOW)
    big_old = priority(50_000_00, "claimable", NOW - timedelta(days=30), NOW)
    assert big_new == big_old == "P1"


@pytest.mark.parametrize(
    "days,bucket",
    [(0, "today"), (0.5, "today"), (2, "1-3d"), (5, "3-7d"), (10, "7-14d"), (40, "14d+")],
)
def test_ageing_buckets(days, bucket):
    assert ageing_bucket(NOW - timedelta(days=days), NOW) == bucket


def test_age_is_computed_from_a_timestamp_not_a_run_id():
    """The v2 bug: first_seen_run held a hex id with no time in it, so ageing
    was uncomputable and the queue could only say 'new' or 'carried'."""
    assert age_days(NOW - timedelta(days=3), NOW) == pytest.approx(3.0)
    assert age_days(None) == 0.0


def test_resolved_is_terminal():
    """A settled case does not reopen because the source rows still look the
    same. That is the whole point of tracking identity across runs."""
    assert not can_transition(CaseStatus.RESOLVED, CaseStatus.ROUTED)
    assert can_transition(CaseStatus.ROUTED, CaseStatus.AWAITING_ACTION)


pytestmark_fixtures = pytest.mark.skipif(
    not (FIXTURES / "ground_truth.json").exists(), reason="run `make gen`"
)


@pytestmark_fixtures
def test_case_state_is_stored_once_and_does_not_grow_with_runs(tmp_path):
    """The performance defect: case_file was a snapshot per case per run, and
    both the API and every reconciliation scanned the whole table to work out
    today's state. Answering 'what is outstanding' must not get slower every
    time the job runs."""
    from sqlmodel import select

    from unnet.core.models import CaseFileRow

    engine = make_engine(tmp_path / "growth.db")
    counts = []
    for index in range(4):
        with session_scope(engine) as session:
            previous = casefile.load_previous(session)
            result = reconcile(
                SourcePaths.synthetic(FIXTURES), run_id=f"r{index}", ai_enabled=False
            )
            built = casefile.build_cases(result.ctx, f"r{index}", previous)
            casefile.persist(session, built, f"r{index}", previous)
        with session_scope(engine) as session:
            counts.append(len(session.exec(select(CaseFileRow)).all()))

    assert len(set(counts)) == 1, f"state table grew across runs: {counts}"
    assert counts[0] == len(built)


@pytestmark_fixtures
def test_history_records_detection_and_resolution(tmp_path):
    engine = make_engine(tmp_path / "history.db")
    with session_scope(engine) as session:
        result = reconcile(SourcePaths.synthetic(FIXTURES), run_id="r1", ai_enabled=False)
        built = casefile.build_cases(result.ctx, "r1")
        casefile.persist(session, built, "r1", {})
    target = max(built, key=lambda c: c.amount_paise)

    with session_scope(engine) as session:
        casefile.resolve(session, target.case_key, run_id="manual", note="settled")

    with session_scope(engine) as session:
        kinds = [e.kind for e in casefile.load_events(session, target.case_key)]
    assert kinds[0] == "detected"
    assert "resolved" in kinds


@pytestmark_fixtures
def test_ageing_survives_across_runs(tmp_path):
    """first_seen_at must not reset every morning, or ageing means nothing."""
    engine = make_engine(tmp_path / "age.db")
    with session_scope(engine) as session:
        first = reconcile(SourcePaths.synthetic(FIXTURES), run_id="r1", ai_enabled=False)
        built = casefile.build_cases(first.ctx, "r1")
        casefile.persist(session, built, "r1", {})
    original = {c.case_key: c.first_seen_at for c in built}

    with session_scope(engine) as session:
        previous = casefile.load_previous(session)
        second = reconcile(SourcePaths.synthetic(FIXTURES), run_id="r2", ai_enabled=False)
        again = casefile.build_cases(second.ctx, "r2", previous)

    for case in again:
        if case.case_key in original:
            assert case.first_seen_at == original[case.case_key]


# --------------------------------------------------------------------------- #
# Ageing runs on the business clock, not the wall clock.
# --------------------------------------------------------------------------- #


@pytestmark_fixtures
def test_a_case_is_aged_from_the_money_event_not_from_first_sight(tmp_path):
    """A fortnight-old chargeback is a fortnight old on the day we install.

    Ageing from ``first_seen_at`` would mean pointing Unnet at a backlog resets
    every outstanding break to zero days — the queue would look healthy the
    morning after a deploy precisely because nothing had been fixed.
    """
    result = reconcile(SourcePaths.synthetic(FIXTURES), run_id="r1", ai_enabled=False)
    cases = casefile.build_cases(result.ctx, "r1")

    assert cases, "fixture produced no cases"
    # Every subject in the fixtures is a row we hold, so every case can be
    # dated. A case that could not be dated would silently fall back to the
    # run time, and that fallback should stay an edge case, not the norm.
    assert all(c.occurred_at is not None for c in cases)
    # first_seen_at is still today; the age is not.
    assert max(c.age_days for c in cases) > 7


@pytestmark_fixtures
def test_ageing_is_measured_to_the_data_horizon_not_to_now(tmp_path):
    """Re-running last month's files must not make last month's breaks older."""
    result = reconcile(SourcePaths.synthetic(FIXTURES), run_id="r1", ai_enabled=False)
    cases = casefile.build_cases(result.ctx, "r1")

    as_of = result.ctx.as_of()
    assert as_of is not None
    assert all(c.as_of == as_of for c in cases)
    # Nothing can be aged past the horizon it is measured against, and nothing
    # in the source data postdates that horizon.
    for case in cases:
        assert case.occurred_at <= as_of
        assert case.age_days == pytest.approx(
            (as_of - case.occurred_at).total_seconds() / 86400.0
        )


@pytestmark_fixtures
def test_business_dates_survive_a_round_trip_through_sqlite(tmp_path):
    engine = make_engine(tmp_path / "dates.db")
    with session_scope(engine) as session:
        result = reconcile(SourcePaths.synthetic(FIXTURES), run_id="r1", ai_enabled=False)
        built = casefile.build_cases(result.ctx, "r1")
        casefile.persist(session, built, "r1", {})
    before = {c.case_key: (c.occurred_at, c.as_of, c.ageing_bucket) for c in built}

    with session_scope(engine) as session:
        reloaded = casefile.load_previous(session)

    assert reloaded
    for key, (occurred, as_of, bucket) in before.items():
        case = reloaded[key]
        assert case.occurred_at == occurred
        assert case.as_of == as_of
        # The derived value, not just the stored one: a bucket that changed on
        # reload would mean the queue reordered itself between runs.
        assert case.ageing_bucket == bucket


def test_an_undateable_subject_falls_back_to_when_we_first_saw_it():
    """Age is never negative and never unknown, whatever the subject is."""
    seen = datetime(2026, 8, 1, tzinfo=timezone.utc)
    case = casefile.CaseFile(
        case_key="k", code="ON_HOLD", subject_kind="mystery", subject_id="x",
        owner="finance_ops", impact="at_risk", action="", message="",
        amount_paise=100, first_seen_at=seen, as_of=seen + timedelta(days=3),
    )
    assert case.occurred_at is None
    assert case.aged_from == seen
    assert case.age_days == pytest.approx(3.0)
