"""Schema creation and first-run bootstrapping.

Called at FastAPI startup and by the CLI. The contract:

* Missing (or new) tables are always created on an existing database --
  ``create_all`` is additive and never drops anything.
* An **empty** database is seeded immediately so ``docker compose up`` yields a
  usable demo without manual steps.
* A database that already has data is left untouched; seeding stays available
  through ``make seed``.
"""

from __future__ import annotations

import logging

from app.clinic import CLINIC
from app.db import database_path, get_engine, session_scope
from app.migrations import apply_additive_migrations
from app.models import Base
from app.services import generations, seed
from app.services.seed import SeedSummary
from app.timeutil import local_now_iso

logger = logging.getLogger(__name__)

# 2: patient_sessions plus new columns on conversations/messages/app_metadata.
# 3: slot holds, booking groups, proposal lifecycle, idempotency in-flight rows.
# 4: canonical scheduling requests and proposal request identities.
# 5: patient feedback on assistant turns.
SCHEMA_VERSION = 5


def create_schema() -> None:
    path = database_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    engine = get_engine()
    Base.metadata.create_all(bind=engine)
    with engine.begin() as connection:
        applied = apply_additive_migrations(connection)
    if applied:
        logger.info("Schema upgraded: %s", ", ".join(applied))
    logger.info("Schema ensured at %s (version %s)", path, SCHEMA_VERSION)


def bootstrap_database(*, auto_seed: bool = True) -> SeedSummary | None:
    """Create missing tables and seed an empty database. Safe to call repeatedly."""
    create_schema()

    with session_scope() as session:
        generations.ensure_generation(session)
        generations.set_schema_version(session, SCHEMA_VERSION)
        empty = seed.is_empty_database(session)

    if not empty:
        logger.info("Database already populated; skipping automatic seed.")
        return None

    if not auto_seed:
        logger.info("Database is empty and auto-seed is disabled.")
        return None

    logger.info("Empty database detected; seeding demo fixtures for %s.", CLINIC.name)
    summary = seed.seed_database()
    logger.info("Auto-seed finished at %s", local_now_iso())
    return summary
