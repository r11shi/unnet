"""A measurement is not an operation on the books.

`eval` and `ablation` answer "how good is this code?". They used to answer it
by writing four extra runs into `data/unnet.db` — including the robustness
profile, whose exceptions became permanent cases with subject ids no standard
run ever revisits, so nothing could ever clear them. `make demo && make
ablation` left the dashboard showing 1,208 open cases and an audit trail whose
most recent run had consulted no model at all.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest
from sqlmodel import select

from unnet.cli import cmd_ablation, cmd_eval
from unnet.core.db import make_engine, session_scope
from unnet.core.models import AuditEntry, CaseFileRow, Run

FIXTURES = Path("data/synthetic")

pytestmark = pytest.mark.skipif(
    not (FIXTURES / "ground_truth.json").exists(), reason="run `make gen`"
)


def _counts(db: Path) -> tuple[int, int, int]:
    with session_scope(make_engine(db)) as session:
        return (
            len(session.exec(select(CaseFileRow)).all()),
            len(session.exec(select(AuditEntry)).all()),
            len(session.exec(select(Run)).all()),
        )


def _args(db: Path, **extra) -> argparse.Namespace:
    return argparse.Namespace(
        data=str(FIXTURES),
        messy_data="data/synthetic_messy",
        db=str(db),
        provider="offline",
        rules_only=False,
        json_out=str(db.parent / "metrics.json"),
        out=str(db.parent / "METRICS.md"),
        label=None,
        **extra,
    )


def test_eval_does_not_write_to_the_operational_database(tmp_path):
    db = tmp_path / "unnet.db"
    make_engine(db)  # create it empty, as a deployment would have it
    before = _counts(db)

    cmd_eval(_args(db))

    assert _counts(db) == before, "eval must not write runs into the real store"


def test_ablation_does_not_write_to_the_operational_database(tmp_path):
    db = tmp_path / "unnet.db"
    make_engine(db)
    before = _counts(db)

    cmd_ablation(_args(db))

    # Four runs happen inside ablation. None of them belong here.
    assert _counts(db) == before, "ablation must not write runs into the real store"


def test_a_scratch_database_is_removed_after_use():
    from unnet.cli import _scratch_db

    with _scratch_db() as path:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        make_engine(path)
        assert Path(path).exists()
    assert not Path(path).exists(), "the scratch database outlived its measurement"
