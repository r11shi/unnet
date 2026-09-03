"""Every run rewrites the whole dataset. Something has to bound that.

A reconciliation re-reads the source files and stores them again, because a
historical run view has to be reproducible from what that run actually saw.
That is defensible; growing forever is not. Three runs over a 1,516-order
fixture already left 4,548 order rows and 5,166 audit entries, and a nightly
job would add that much every night.

What must *not* be pruned is the loop: a case outlives the run that raised it —
that is the whole point of a stable `case_key` — and its history is the record
of what a human did.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest
from sqlmodel import select

from unnet.cli import _run
from unnet.core.db import RUN_SCOPED_TABLES, make_engine, prune_runs, session_scope
from unnet.core.models import AuditEntry, CaseEvent, CaseFileRow, MerchantOrder, Run

FIXTURES = Path("data/synthetic")

pytestmark = pytest.mark.skipif(
    not (FIXTURES / "ground_truth.json").exists(), reason="run `make gen`"
)


def _args(db: Path, keep: int) -> argparse.Namespace:
    return argparse.Namespace(
        data=str(FIXTURES), db=str(db), provider="offline",
        rules_only=True, label="retention", keep_runs=keep,
    )


def _counts(db: Path) -> dict:
    with session_scope(make_engine(db)) as session:
        return {
            "runs": len(session.exec(select(Run)).all()),
            "orders": len(session.exec(select(MerchantOrder)).all()),
            "audit": len(session.exec(select(AuditEntry)).all()),
            "cases": len(session.exec(select(CaseFileRow)).all()),
            "events": len(session.exec(select(CaseEvent)).all()),
        }


def test_row_counts_stop_growing_once_the_window_is_full(tmp_path):
    db = tmp_path / "retention.db"
    for _ in range(2):
        _run(_args(db, keep=2), ai_enabled=False, label="r")
    settled = _counts(db)

    for _ in range(3):
        _run(_args(db, keep=2), ai_enabled=False, label="r")
    after = _counts(db)

    assert after["runs"] == 2
    assert after["orders"] == settled["orders"], "source rows kept growing"
    assert after["audit"] == settled["audit"], "the audit trail kept growing"


def test_pruning_never_touches_the_case_loop(tmp_path):
    """The one thing retention must not eat."""
    db = tmp_path / "loop.db"
    for _ in range(4):
        _run(_args(db, keep=1), ai_enabled=False, label="r")

    after = _counts(db)

    assert after["runs"] == 1, "retention did not run"
    assert after["cases"] > 0, "pruning deleted the case queue"
    assert after["events"] > 0, "pruning deleted the case history"
    assert "case_file" not in RUN_SCOPED_TABLES
    assert "case_event" not in RUN_SCOPED_TABLES


def test_keeping_everything_is_still_possible(tmp_path):
    db = tmp_path / "keepall.db"
    for _ in range(3):
        _run(_args(db, keep=0), ai_enabled=False, label="r")

    assert _counts(db)["runs"] == 3


def test_pruning_a_fresh_database_is_a_no_op(tmp_path):
    engine = make_engine(tmp_path / "empty.db")
    assert prune_runs(engine, keep=5) == {}


def test_the_cli_and_the_api_agree_on_which_database_to_use(monkeypatch, tmp_path):
    """`UNNET_DB=x unnet recon` used to write somewhere else than `serve` read.

    The CLI defaulted `--db` to the literal "data/unnet.db" while the API builds
    its engine from DEFAULT_DB_PATH, which honours the variable. Two commands,
    two databases, and no error anywhere to say so.
    """
    import importlib

    monkeypatch.setenv("UNNET_DB", str(tmp_path / "declared.db"))
    import unnet.cli as cli_module
    import unnet.core.db as db_module

    importlib.reload(db_module)
    importlib.reload(cli_module)
    try:
        default = next(
            action.default
            for action in cli_module.build_parser()._actions
            if action.dest == "db"
        )
        assert default == str(db_module.DEFAULT_DB_PATH)
        assert "declared.db" in default
    finally:
        # Leave the modules as the rest of the suite expects to find them.
        monkeypatch.delenv("UNNET_DB", raising=False)
        importlib.reload(db_module)
        importlib.reload(cli_module)
