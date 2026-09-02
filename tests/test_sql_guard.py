"""The Ask path lets a model write SQL. This is what stops that mattering.

Two layers, deliberately. `_is_safe` reasons about the text of a statement, and
text analysis is exactly the kind of guard that gets walked around — so the
connection also refuses writes outright while a model's SQL runs. A bypass of
the first is then still not a way into the ledger.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import text

from unnet.agents.qa import _is_safe, _read_only
from unnet.core.db import make_engine, session_scope
from unnet.core.models import Run


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM case_file",
        "UPDATE case_file SET status = 'resolved'",
        "DROP TABLE recon_exception",
        "SELECT 1; DROP TABLE case_file",
        "ATTACH DATABASE '/tmp/evil.db' AS e",
        "SELECT * FROM sqlite_master",
        "SELECT * FROM recon_exception UNION SELECT * FROM sqlite_master",
        "select * from (delete from case_file)",
        "PRAGMA table_info(run)",
        "",
    ],
)
def test_the_obvious_attacks_are_refused(sql):
    ok, _ = _is_safe(sql)
    assert not ok


@pytest.mark.parametrize(
    "sql",
    [
        # Perfectly well-formed SELECTs over no tables at all, so neither the
        # statement shape nor the table allow-list has anything to say about
        # them. `load_extension` loads an arbitrary shared object.
        "SELECT load_extension('/tmp/evil.so')",
        "SELECT writefile('/tmp/owned', 'x')",
        "SELECT readfile('/etc/passwd')",
    ],
)
def test_functions_that_reach_outside_the_database_are_refused(sql):
    ok, reason = _is_safe(sql)
    assert not ok
    assert "outside the database" in reason


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT count(*) FROM recon_exception",
        "select code, count(*) from recon_exception group by code",
        "SELECT * FROM settlement_line JOIN merchant_order ON 1=1",
    ],
)
def test_ordinary_questions_still_run(sql):
    ok, reason = _is_safe(sql)
    assert ok, reason


def test_the_connection_refuses_writes_even_if_the_guard_is_bypassed(tmp_path):
    """The backstop, tested on its own terms: no regex involved."""
    engine = make_engine(tmp_path / "guard.db")
    with session_scope(engine) as session:
        session.add(Run(run_id="r1", label="t", orders_count=0,
                        settlement_lines_count=0, bank_txns_count=0))

    with session_scope(engine) as session:
        with _read_only(session):
            # Reads are unaffected.
            assert session.exec(text("SELECT count(*) FROM run")).first()[0] == 1
            with pytest.raises(Exception) as caught:
                session.exec(text("DELETE FROM run"))
            assert "readonly" in str(caught.value).lower() or "query_only" in str(caught.value).lower()

    # And the session is writable again afterwards, or the rest of the API breaks.
    with session_scope(engine) as session:
        session.exec(text("DELETE FROM run"))
    with session_scope(engine) as session:
        assert session.exec(text("SELECT count(*) FROM run")).first()[0] == 0
