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
    return engine


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
