"""Demo database reset.

Two modes are supported:

``reset_demo_data()`` (file swap, used by ``make reset-demo``)
    Removes the SQLite file and its WAL/SHM sidecars, then recreates the schema
    and reseeds. Because the database is brand new, every conversation ID,
    staff session, proposal, and idempotency key from before the reset no longer
    exists. A fresh reset generation is written and the reset counter continues
    from where it was, so operators can tell resets apart.

``reset_demo_in_place()`` (used by the API when the file cannot be deleted)
    Empties every table inside a single transaction, reseeds, and rotates the
    generation.

Sidecars are removed *before* the new database is created so no stale WAL
frames are replayed into a fresh file.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from sqlalchemy import create_engine, text

from app.bootstrap import SCHEMA_VERSION, create_schema
from app.config import get_settings
from app.db import database_path, delete_database_files, dispose_engine, session_scope
from app.models import Base
from app.services import generations, seed
from app.timeutil import SLOT_MINUTES, local_now_iso

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ResetResult:
    generation: str
    reset_count: int
    anchor_date: str | None
    occurred_at: str
    strategy: str
    fixtures_created: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "generation": self.generation,
            "reset_count": self.reset_count,
            "strategy": self.strategy,
            "occurred_at": self.occurred_at,
            "anchor_date": self.anchor_date,
            "slot_interval_minutes": SLOT_MINUTES,
            "fixtures_created": self.fixtures_created,
        }


def _previous_reset_count() -> int:
    """Read the reset counter before the database is replaced. Best effort."""
    if not database_path().exists():
        return 0
    try:
        engine = create_engine(get_settings().database_url, future=True)
        try:
            with engine.connect() as connection:
                row = connection.execute(
                    text("SELECT value FROM app_metadata WHERE key = 'reset_count'")
                ).first()
            return int(row[0]) if row else 0
        finally:
            engine.dispose()
    except Exception:  # a missing table or corrupt file must not block a reset
        logger.warning("Could not read previous reset count; assuming 0.")
        return 0


def _result(start_count: int, strategy: str, summary: seed.SeedSummary) -> ResetResult:
    with session_scope() as session:
        generation = generations.get_generation(session) or "unknown"
        reset_count = generations.get_reset_count(session) or start_count
    return ResetResult(
        generation=generation,
        reset_count=reset_count,
        anchor_date=summary.anchor_date.isoformat() if summary.anchor_date else None,
        occurred_at=local_now_iso(),
        strategy=strategy,
        fixtures_created=summary.as_dict(),
    )


def reset_demo_data() -> ResetResult:
    """Destructive reset: delete the database file, recreate it, and reseed."""
    next_count = _previous_reset_count() + 1

    # dispose_engine() also runs inside delete_database_files(); calling it here
    # makes the "no writers" intent explicit before any file is touched.
    dispose_engine()
    delete_database_files()
    create_schema()

    with session_scope() as session:
        generations.rotate_generation(session, count=next_count)
        generations.set_schema_version(session, SCHEMA_VERSION)

    summary = seed.seed_database()
    logger.info("Demo reset complete (file swap); generation rotated.")
    return _result(next_count, "file-swap", summary)


def reset_demo_in_place() -> ResetResult:
    """Destructive reset that empties tables instead of deleting the file."""
    next_count = _previous_reset_count() + 1
    create_schema()

    with session_scope() as session:
        for table in reversed(Base.metadata.sorted_tables):
            session.execute(text(f'DELETE FROM "{table.name}"'))
        generations.rotate_generation(session, count=next_count)
        generations.set_schema_version(session, SCHEMA_VERSION)

    summary = seed.seed_database()
    logger.info("Demo reset complete (in place); generation rotated.")
    return _result(next_count, "in-place", summary)
