"""Demo reset, scoped to one tenant at a time.

Three modes:

``reset_tenant_data(slug)`` -- ``make reset-demo --clinic <slug>``
    Empties *that clinic's* rows inside a single transaction, rotates *that
    clinic's* generation, and reseeds only it. The other tenants keep their
    data and their generation, so a browser holding tenant B's conversation ID
    is not told to discard it because tenant A was reset. This is why an
    in-place reset is used rather than a file swap: a file swap cannot be
    scoped.

``reset_demo_data()`` -- ``make reset-demo`` with no clinic
    Destructive file swap. Removes the SQLite file and its WAL/SHM sidecars,
    recreates the schema, and reseeds every tenant. Because the database is
    brand new, every conversation ID, staff session, proposal, and idempotency
    key from before the reset no longer exists, and every generation rotates.

``reset_demo_in_place()``
    Same as the file swap for callers that cannot delete the file (a read-only
    volume, a Windows lock). Empties every tenant table in one transaction.

Sidecars are removed *before* the new database is created so no stale WAL
frames are replayed into a fresh file.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from sqlalchemy import create_engine, delete, text

from app.bootstrap import SCHEMA_VERSION, create_schema
from app.config import get_settings
from app.db import database_path, delete_database_files, dispose_engine, session_scope
from app.models import Base, Clinic
from app.services import generations, seed
from app.tenancy import find_clinic, scheduling_config_for
from app.timeutil import utc_now_iso

logger = logging.getLogger(__name__)

# Rows that describe a clinic rather than live inside one. A tenant reset must
# not destroy them: ``ensure_clinic_rows`` refreshes them from the template, and
# deleting ``clinics`` itself would orphan everything the reset is about to write
# back.
CLINIC_CONFIG_TABLES = frozenset({"clinics", "clinic_insurance_plans", "clinic_policies"})


@dataclass(frozen=True)
class ResetResult:
    generation: str
    reset_count: int
    anchor_date: str | None
    occurred_at: str
    strategy: str
    slot_interval_minutes: int
    horizon_days: int
    clinic_slug: str | None = None
    clinic_id: int | None = None
    fixtures_created: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "generation": self.generation,
            "reset_count": self.reset_count,
            "strategy": self.strategy,
            "occurred_at": self.occurred_at,
            "anchor_date": self.anchor_date,
            "clinic_slug": self.clinic_slug,
            "clinic_id": self.clinic_id,
            "slot_interval_minutes": self.slot_interval_minutes,
            "horizon_days": self.horizon_days,
            "fixtures_created": self.fixtures_created,
        }


def _tenant_tables() -> list[str]:
    """Tenant-owned table names, child tables first so deletes stay clean."""
    return [
        table.name
        for table in reversed(Base.metadata.sorted_tables)
        if "clinic_id" in table.columns and table.name not in CLINIC_CONFIG_TABLES
    ]


def _previous_reset_count(clinic_id: int | None = None) -> int:
    """Read a tenant's reset counter before its rows go away. Best effort."""
    if not database_path().exists():
        return 0
    try:
        engine = create_engine(get_settings().database_url, future=True)
        try:
            with engine.connect() as connection:
                row = connection.execute(
                    text(
                        "SELECT value FROM app_metadata WHERE key = 'reset_count'"
                        + (" AND clinic_id = :clinic_id" if clinic_id is not None else "")
                    ),
                    {"clinic_id": clinic_id} if clinic_id is not None else {},
                ).first()
            return int(row[0]) if row else 0
        finally:
            engine.dispose()
    except Exception:  # a missing table or corrupt file must not block a reset
        logger.warning("Could not read previous reset count; assuming 0.")
        return 0


def _result(
    start_count: int,
    strategy: str,
    summary: seed.SeedSummary,
    *,
    clinic_id: int | None = None,
    clinic_slug: str | None = None,
    config=None,
) -> ResetResult:
    """Re-read the operational state *after* the reset, from the tenant's session.

    The generation is reported per clinic, so a whole-database reset has to pick
    one clinic's row to describe; callers that reset a single tenant pass it.
    """
    with session_scope(clinic_id) as session:
        generation = generations.get_generation(session, clinic_id=clinic_id) or "unknown"
        reset_count = generations.get_reset_count(session, clinic_id=clinic_id) or start_count
    if config is None:
        with session_scope() as session:
            row = session.get(Clinic, clinic_id) if clinic_id else None
            config = scheduling_config_for(row) if row is not None else None
    return ResetResult(
        generation=generation,
        reset_count=reset_count,
        anchor_date=summary.anchor_date.isoformat() if summary.anchor_date else None,
        occurred_at=utc_now_iso(),
        strategy=strategy,
        slot_interval_minutes=config.slot_minutes if config else 0,
        horizon_days=config.seed_horizon_days if config else 0,
        clinic_slug=clinic_slug,
        clinic_id=clinic_id,
        fixtures_created=summary.as_dict(),
    )


def reset_tenant_data(slug: str) -> ResetResult:
    """Reset one clinic: clear its rows, rotate its generation, reseed it."""
    with session_scope() as session:
        clinic = find_clinic(session, slug)
        if clinic is None:
            raise LookupError(f"unknown clinic: {slug}")
        clinic_id = clinic.id
        config = scheduling_config_for(clinic)

    next_count = _previous_reset_count(clinic_id) + 1
    tables = _tenant_tables()

    with session_scope(clinic_id) as session:
        for name in tables:
            session.execute(
                text(f'DELETE FROM "{name}" WHERE clinic_id = :clinic_id'),
                {"clinic_id": clinic_id},
            )
        session.flush()
        generations.rotate_generation(session, count=next_count, clinic_id=clinic_id)
        generations.set_schema_version(session, SCHEMA_VERSION)
        session.commit()

    with session_scope(clinic_id) as session:
        plan = _plan_for_clinic(session, clinic_id)
        tenant = seed.tenant_for(session, plan) if plan is not None else None
        if tenant is None:
            raise LookupError(f"clinic {slug} has no fixture template")
        summary = seed.seed_tenant(session, tenant)
        session.commit()

    logger.info("Demo reset complete for tenant %s (in place).", slug)
    return _result(
        next_count, "tenant-in-place", summary, clinic_id=clinic_id, clinic_slug=slug, config=config
    )


def _plan_for_clinic(session, clinic_id: int):
    """The template plan backing a clinic row, if one exists.

    Clinics created through ``create-clinic`` have no fixture template, so a
    reset of one only clears its data and rotates its generation.
    """
    row = session.get(Clinic, clinic_id)
    if row is None:
        return None
    return _plan_for_slug(row.slug)


def _rotate_all(
    session, clinics: list[tuple[int, str]], counts: dict[str, int]
) -> None:
    """Give every named tenant a fresh generation and a continued counter.

    Rotated *before* the reseed: ``seed_tenant`` only creates a generation when
    one is missing, so seeding first would pin every clinic to count 1 and lose
    the running total operators use to tell resets apart.
    """
    for clinic_id, slug in clinics:
        generations.rotate_generation(
            session, count=counts.get(slug, 0) + 1, clinic_id=clinic_id
        )
    generations.set_schema_version(session, SCHEMA_VERSION)
    session.flush()


_clinics_query = text("SELECT id, slug FROM clinics ORDER BY id")


def _results(
    strategy: str,
    clinics: list[tuple[int, str]],
    counts: dict[str, int],
    summaries: dict,
) -> list[ResetResult]:
    results: list[ResetResult] = []
    for clinic_id, slug in clinics:
        plan = _plan_for_slug(slug)
        summary = summaries.get(slug) or seed.SeedSummary(anchor_date=None)
        results.append(
            _result(
                counts.get(slug, 0) + 1,
                strategy,
                summary,
                clinic_id=clinic_id,
                clinic_slug=slug,
                config=plan.scheduling if plan is not None else None,
            )
        )
    return results


def reset_demo_data(slugs: list[str] | None = None) -> list[ResetResult]:
    """Destructive reset: delete the database file, recreate it, reseed everyone.

    ``--clinic`` cannot be honoured here. A file swap destroys every tenant, so
    naming one and re-seeding only it would silently erase the others; the
    per-tenant path (``reset_tenant_data``) is the only honest way to reset one
    clinic, and in-place is how a named subset is reset.
    """
    if slugs:
        raise ValueError(
            "file-swap resets every clinic; use --strategy in-place with --clinic "
            "to reset named tenants, or omit --clinic entirely"
        )
    counts = {slug: _previous_reset_count_for_slug(slug) for slug in _known_slugs()}

    # dispose_engine() also runs inside delete_database_files(); calling it here
    # makes the "no writers" intent explicit before any file is touched.
    dispose_engine()
    delete_database_files()
    create_schema()

    with session_scope() as session:
        seed.ensure_clinic_rows(session)
        session.commit()
        clinics = [(int(row[0]), row[1]) for row in session.execute(_clinics_query)]
        _rotate_all(session, clinics, counts)
        session.commit()

    summaries = seed.seed_database()
    logger.info("Demo reset complete (file swap); every generation rotated.")
    return _results("file-swap", clinics, counts, summaries)


def reset_demo_in_place(slugs: list[str] | None = None) -> list[ResetResult]:
    """Destructive reset that empties tables instead of deleting the file.

    With ``slugs`` only those tenants are emptied, rotated and reseeded, so a
    demo reset of one clinic leaves another clinic's bookings and browser state
    alone. Without it every tenant is reset, which is the whole-database case.
    """
    create_schema()

    with session_scope() as session:
        rows = [(int(row[0]), row[1]) for row in session.execute(_clinics_query)]
    wanted = {slug.strip().lower() for slug in slugs} if slugs else None
    if wanted is not None:
        rows = [row for row in rows if row[1] in wanted]
        missing = wanted - {row[1] for row in rows}
        if missing:
            raise LookupError(f"unknown clinic(s): {', '.join(sorted(missing))}")
    if not rows:
        raise LookupError("no tenants to reset")

    # The counters have to be read before the rows go away: re-reading them after
    # the delete would report 1 for every clinic instead of continuing the total.
    counts = {slug: _previous_reset_count_for_slug(slug) for _id, slug in rows}
    clinic_ids = [clinic_id for clinic_id, _slug in rows]

    with session_scope() as session:
        for name in _tenant_tables():
            if wanted is None:
                session.execute(delete(Base.metadata.tables[name]))
                continue
            session.execute(
                text(f'DELETE FROM "{name}" WHERE clinic_id IN :ids'),
                {"ids": tuple(clinic_ids)},
            )
        _rotate_all(session, rows, counts)

    summaries = seed.seed_database(slugs=[slug for _id, slug in rows])
    logger.info(
        "Demo reset complete (in place) for %s.", ", ".join(slug for _id, slug in rows)
    )
    return _results("in-place", rows, counts, summaries)


def _known_slugs() -> list[str]:
    if not database_path().exists():
        return []
    try:
        engine = create_engine(get_settings().database_url, future=True)
        try:
            with engine.connect() as connection:
                return [row[0] for row in connection.execute(text("SELECT slug FROM clinics"))]
        finally:
            engine.dispose()
    except Exception:  # a fresh or damaged file simply has no tenants yet
        return []


def _previous_reset_count_for_slug(slug: str) -> int:
    if not database_path().exists():
        return 0
    try:
        engine = create_engine(get_settings().database_url, future=True)
        try:
            with engine.connect() as connection:
                row = connection.execute(
                    text(
                        "SELECT m.value FROM app_metadata m JOIN clinics c ON c.id = m.clinic_id"
                        " WHERE c.slug = :slug AND m.key = 'reset_count'"
                    ),
                    {"slug": slug},
                ).first()
            return int(row[0]) if row else 0
        finally:
            engine.dispose()
    except Exception:
        logger.warning("Could not read previous reset count for %s; assuming 0.", slug)
        return 0


def _plan_for_slug(slug: str):
    from app.seed_tenants import plan_for_slug

    return plan_for_slug(slug)


__all__ = [
    "CLINIC_CONFIG_TABLES",
    "ResetResult",
    "reset_demo_data",
    "reset_demo_in_place",
    "reset_tenant_data",
]
