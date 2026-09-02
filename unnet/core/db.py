"""SQLite engine and the append-only audit writer."""

from __future__ import annotations

import itertools
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from sqlmodel import Session, SQLModel, create_engine, select

from unnet.core import models  # noqa: F401  (import registers the tables)
from unnet.core.models import AuditEntry, DecidedBy

DEFAULT_DB_PATH = Path(os.environ.get("UNNET_DB", "data/unnet.db"))


def make_engine(db_path: Path | str = DEFAULT_DB_PATH, *, echo: bool = False):
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(
        f"sqlite:///{path}",
        echo=echo,
        connect_args={"check_same_thread": False},
    )
    SQLModel.metadata.create_all(engine)
    _add_missing_columns(engine)
    return engine


def _add_missing_columns(engine) -> list[str]:
    """Bring an existing database up to the current model.

    ``create_all`` creates missing *tables* and silently ignores missing
    *columns*, so a database written before a field was added keeps working
    until the first query names that column — and then fails at runtime, in
    production, on a table full of real data.

    Every schema change in this project so far has been additive, and SQLite's
    ``ALTER TABLE ADD COLUMN`` is safe and instant, so that is the whole
    migration story. It is deliberately not Alembic: one file of version
    history for a prototype with an additive-only schema is machinery without a
    problem. The day a column needs renaming or dropping, this stops being
    enough and should be replaced rather than extended.
    """
    from sqlalchemy import inspect, text

    added: list[str] = []
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())

    with engine.begin() as connection:
        for table_name, table in SQLModel.metadata.tables.items():
            if table_name not in existing_tables:
                continue
            have = {c["name"] for c in inspector.get_columns(table_name)}
            for column in table.columns:
                if column.name in have:
                    continue
                ddl = column.type.compile(engine.dialect)
                default = ""
                if not column.nullable:
                    # A NOT NULL column cannot be added to a populated table
                    # without a default; pick a harmless one by type.
                    literal = "0" if "INT" in ddl.upper() or "FLOAT" in ddl.upper() else "\'\'"
                    default = f" NOT NULL DEFAULT {literal}"
                connection.execute(
                    text(f'ALTER TABLE "{table_name}" ADD COLUMN "{column.name}" {ddl}{default}')
                )
                added.append(f"{table_name}.{column.name}")
    return added


@contextmanager
def session_scope(engine) -> Iterator[Session]:
    # expire_on_commit=False keeps the in-memory objects usable after the
    # commit. The engine builds a whole run in memory, persists it, and then
    # reports on it; with the default the reporting step would re-query every
    # attribute it touches, and any object read after the session closed would
    # raise DetachedInstanceError instead.
    session = Session(engine, expire_on_commit=False)
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


class AuditLog:
    """Append-only decision log.

    Sequence numbers are assigned per run so the trail can be replayed in the
    order decisions were actually taken, which a timestamp alone does not
    guarantee at sub-millisecond resolution.
    """

    def __init__(self, session: Session, run_id: str) -> None:
        self.session = session
        self.run_id = run_id
        start = session.exec(
            select(AuditEntry).where(AuditEntry.run_id == run_id)
        ).all()
        self._seq = itertools.count(len(start) + 1)

    def record(
        self,
        *,
        stage: str,
        subject_kind: str,
        subject_id: str,
        decision: str,
        decided_by: DecidedBy,
        decider_ref: str,
        confidence: int = 1000,
        evidence: dict | None = None,
        verifier_result: str | None = None,
    ) -> AuditEntry:
        entry = AuditEntry(
            run_id=self.run_id,
            seq=next(self._seq),
            stage=stage,
            subject_kind=subject_kind,
            subject_id=subject_id,
            decision=decision,
            decided_by=decided_by,
            decider_ref=decider_ref,
            confidence=confidence,
            evidence=evidence or {},
            verifier_result=verifier_result,
        )
        self.session.add(entry)
        return entry
