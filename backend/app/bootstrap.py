"""Schema creation and first-run bootstrapping.

Called at FastAPI startup and by the CLI. The contract:

* Missing (or new) tables are always created on an existing database --
  ``create_all`` is additive and never drops anything.
* The tenant rows are ensured before anything else, because every other table
  hangs off ``clinics.id`` through a ``clinic_id`` foreign key.
* An **empty** tenant is seeded immediately, so ``docker compose up`` yields a
  usable demo without manual steps. A tenant that already has data is left
  untouched; seeding stays available through ``make seed``.
* Emptiness is judged *per tenant*. Under tenancy "is the database empty" is the
  wrong question: a second clinic added to a populated file starts out empty and
  must be seeded, while the first clinic must not be re-seeded.
"""

from __future__ import annotations

import logging

from app.db import database_path, get_engine, session_scope
from app.migrations import apply_additive_migrations
from app.models import Base
from app.seed_tenants import plans_for_slugs
from app.services import generations, seed
from app.services.seed import SeedSummary

logger = logging.getLogger(__name__)

# 2: patient_sessions plus new columns on conversations/messages/app_metadata.
# 3: slot holds, booking groups, proposal lifecycle, idempotency in-flight rows.
# 4: canonical scheduling requests and proposal request identities.
# 5: patient feedback on assistant turns.
# 6: counted chat-side identity lookups with a lockout window.
# 7: multi-tenancy. ``clinics`` plus its knowledge tables, and a non-nullable
#    ``clinic_id`` on every tenant table, with the uniques that used to be global
#    rebuilt as ``(clinic_id, ...)``.
SCHEMA_VERSION = 7


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


def bootstrap_database(
    *, auto_seed: bool = True, slugs: list[str] | None = None
) -> dict[str, SeedSummary] | None:
    """Create missing tables and seed any tenant with nothing in it.

    Returns the per-clinic seed summaries, or ``None`` when no tenant needed
    seeding. Safe to call repeatedly. ``slugs`` narrows the tenants considered --
    a fresh clinic added to a populated file is seeded without touching the
    clinics that already have data.
    """
    create_schema()

    plans = plans_for_slugs(slugs)
    with session_scope() as session:
        seed.ensure_clinic_rows(session, plans)
        session.commit()
        generations.set_schema_version(session, SCHEMA_VERSION)

    seeded: dict[str, SeedSummary] = {}
    for plan in plans:
        with session_scope() as session:
            tenant = seed.tenant_for(session, plan)
            if tenant is None:
                logger.warning("Clinic %s is missing; skipping.", plan.slug)
                continue
            with session_scope(tenant.clinic_id) as scoped:
                generations.ensure_generation(scoped, clinic_id=tenant.clinic_id)
                empty = seed.is_empty_database(scoped) and not seed.database_is_seeded(
                    scoped, tenant.clinic_id
                )
            if not empty:
                continue
            if not auto_seed:
                logger.info("%s is empty and auto-seed is disabled.", tenant.slug)
                continue
            logger.info("Empty tenant detected; seeding demo fixtures for %s.", tenant.slug)
            with session_scope(tenant.clinic_id) as scoped:
                seeded[tenant.slug] = seed.seed_tenant(scoped, tenant)
                scoped.commit()

    if seeded:
        logger.info("Auto-seed finished for %s.", ", ".join(sorted(seeded)))
        return seeded
    logger.info("Database already populated for every tenant; skipping automatic seed.")
    return None


__all__ = ["SCHEMA_VERSION", "bootstrap_database", "create_schema"]
