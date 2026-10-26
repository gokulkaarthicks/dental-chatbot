"""Additive schema reconciliation for existing SQLite databases.

Phase 1 shipped without a migration tool because the schema was fixed. Phase 2
adds a handful of columns and one table, and an existing Docker volume must keep
its records rather than being wiped.

Strategy: ``Base.metadata.create_all()`` already creates *missing tables*, so
only *missing columns on existing tables* need handling. Each entry below is
idempotent and runs inside the caller's transaction. Nothing is ever dropped or
rewritten, so this is safe to run on every startup -- with one exception: a
changed CHECK constraint, or a UNIQUE/PK clause that has to become per-clinic
(SQLite can do neither with ``ALTER TABLE``) is applied by rebuilding that table
from the model definition and copying every row across.

Phase 7 is that exception at scale. Tenancy adds a non-nullable ``clinic_id`` to
every tenant table and turns seven globally-unique columns into per-clinic ones,
so the reconciliation here is: make tenant 1 exist, add the columns (backfilling
them with 1, because a row written before tenancy belonged to the only clinic
there was), then rebuild the tables whose constraint shape changed.
"""

from __future__ import annotations

import logging
import re

from sqlalchemy import UniqueConstraint, inspect, text
from sqlalchemy.dialects import sqlite
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session
from sqlalchemy.schema import CreateIndex, CreateTable

from app.models import Base

logger = logging.getLogger(__name__)

# (table, column, SQL type + default). Deliberately narrow: only additions.
# One per line so the list stays readable and lint-clean.
_ADDED_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("conversations", "patient_session_id", "INTEGER"),
    ("conversations", "title", "VARCHAR(120)"),
    ("conversations", "last_message_at", "DATETIME"),
    ("conversations", "scheduling_request", "JSON"),
    ("messages", "tool_call_id", "VARCHAR(64)"),
    ("messages", "tool_name", "VARCHAR(64)"),
    ("messages", "finished_at", "DATETIME"),
    ("messages", "cards", "JSON"),
    ("app_metadata", "public", "BOOLEAN NOT NULL DEFAULT 0"),
    # --- Phase 3 -----------------------------------------------------------
    ("availability_slots", "hold_reason", "VARCHAR(120)"),
    ("appointments", "booking_group_id", "INTEGER"),
    ("slot_claims", "held_by_proposal_id", "INTEGER"),
    ("booking_proposals", "kind", "VARCHAR(16) NOT NULL DEFAULT 'booking'"),
    ("booking_proposals", "from_ui", "BOOLEAN NOT NULL DEFAULT 0"),
    ("booking_proposals", "create_key", "VARCHAR(128)"),
    ("booking_proposals", "confirm_key", "VARCHAR(128)"),
    ("booking_proposals", "resolved_at", "DATETIME"),
    ("booking_proposals", "confirmed_at", "DATETIME"),
    ("booking_proposals", "conflict_reason", "VARCHAR(64)"),
    ("booking_proposals", "booking_group_id", "INTEGER"),
    ("booking_proposals", "expires_at", "DATETIME"),
    ("booking_proposals", "target_appointment_id", "INTEGER"),
    ("booking_proposals", "cancellation_reason", "TEXT"),
    ("proposal_items", "stable_slot_id", "VARCHAR(64)"),
    ("proposal_items", "provider_name", "VARCHAR(120)"),
    ("proposal_items", "provider_credentials", "VARCHAR(40)"),
    ("proposal_items", "appointment_type_code", "VARCHAR(48)"),
    ("proposal_items", "appointment_type_name", "VARCHAR(120)"),
    ("proposal_items", "patient_first_name", "VARCHAR(80)"),
    ("proposal_items", "target_appointment_id", "INTEGER"),
    ("proposal_items", "original_starts_at", "DATETIME"),
    ("proposal_items", "result_appointment_id", "INTEGER"),
    ("proposal_items", "issue", "TEXT"),
    ("booking_proposals", "held_slot_ids", "JSON"),
    ("idempotency_records", "state", "VARCHAR(16) NOT NULL DEFAULT 'complete'"),
    ("idempotency_records", "owner_token", "VARCHAR(64)"),
    ("audit_records", "visibility", "VARCHAR(16) NOT NULL DEFAULT 'COMMIT'"),
    ("staff_alerts", "dedupe_key", "VARCHAR(96)"),
    ("staff_alerts", "report_count", "INTEGER NOT NULL DEFAULT 1"),
    ("staff_alerts", "last_reported_at", "DATETIME"),
    ("pending_requests", "dedupe_key", "VARCHAR(96)"),
    ("booking_proposals", "family_group_id", "INTEGER REFERENCES family_groups(id)"),
    # --- Phase 4 -----------------------------------------------------------
    ("staff_users", "username", "VARCHAR(64)"),
    ("staff_users", "failed_login_count", "INTEGER NOT NULL DEFAULT 0"),
    ("staff_users", "locked_until", "DATETIME"),
    ("staff_sessions", "csrf_token_hash", "VARCHAR(128)"),
    ("staff_alerts", "acknowledged_at", "DATETIME"),
    ("staff_alerts", "acknowledged_by", "INTEGER"),
    ("staff_alerts", "resolution_note", "TEXT"),
    ("pending_requests", "resolution_note", "TEXT"),
    ("pending_requests", "approved_by", "INTEGER"),
    ("pending_requests", "approved_at", "DATETIME"),
    # --- Phase 5: reschedule keeps history on both rows ---------------------
    ("appointments", "rescheduled_to_id", "INTEGER REFERENCES appointments(id)"),
    ("appointments", "rescheduled_from_id", "INTEGER REFERENCES appointments(id)"),
    ("appointments", "rescheduled_at", "DATETIME"),
    ("appointments", "rescheduled_from_starts_at", "DATETIME"),
    ("appointments", "rescheduled_from_provider_id", "INTEGER REFERENCES providers(id)"),
    (
        "appointments",
        "rescheduled_from_appointment_type_id",
        "INTEGER REFERENCES appointment_types(id)",
    ),
    ("appointments", "rescheduled_from_duration_minutes", "INTEGER"),
    ("booking_proposals", "released_appointment_ids", "JSON"),
    # --- Phase 5: record the requested intent beside what confirming does ----
    ("booking_proposals", "intent", "VARCHAR(16)"),
    ("booking_proposals", "confirm_outcome", "JSON"),
    # --- Phase 6: one request books a party once ---------------------------
    ("booking_proposals", "visit_shape", "VARCHAR(16)"),
    ("booking_proposals", "request_id", "VARCHAR(128)"),
    ("appointments", "visit_shape", "VARCHAR(16)"),
    # --- Contact details for an unverified emergency caller ----------------
    ("patient_sessions", "contact_name", "VARCHAR(120)"),
    ("patient_sessions", "contact_phone", "VARCHAR(32)"),
    # --- Chat-side identity lookups are counted and can lock out -----------
    ("patient_sessions", "verification_attempts", "INTEGER NOT NULL DEFAULT 0"),
    ("patient_sessions", "verification_locked_until", "DATETIME"),
    ("staff_alerts", "caller_name", "VARCHAR(120)"),
    ("staff_alerts", "caller_phone", "VARCHAR(32)"),
    # --- Phase 7: tenancy ---------------------------------------------------
    # ``ADD COLUMN`` cannot carry a foreign key when the column is NOT NULL with
    # a default, so the REFERENCES clause is deliberately absent here: the
    # rebuilt tables get the real key from their model definition, and the rest
    # are held honest by the write guard in ``app.models.base`` plus the
    # ``PRAGMA foreign_key_check`` that closes every rebuild.
    ("app_metadata", "clinic_id", "INTEGER NOT NULL DEFAULT 1"),
    ("appointment_types", "clinic_id", "INTEGER NOT NULL DEFAULT 1"),
    ("appointments", "clinic_id", "INTEGER NOT NULL DEFAULT 1"),
    ("audit_records", "clinic_id", "INTEGER NOT NULL DEFAULT 1"),
    ("availability_slots", "clinic_id", "INTEGER NOT NULL DEFAULT 1"),
    ("booking_groups", "clinic_id", "INTEGER NOT NULL DEFAULT 1"),
    ("booking_proposals", "clinic_id", "INTEGER NOT NULL DEFAULT 1"),
    ("clinic_insurance_plans", "clinic_id", "INTEGER NOT NULL DEFAULT 1"),
    ("clinic_policies", "clinic_id", "INTEGER NOT NULL DEFAULT 1"),
    ("conversation_feedback", "clinic_id", "INTEGER NOT NULL DEFAULT 1"),
    ("conversations", "clinic_id", "INTEGER NOT NULL DEFAULT 1"),
    ("family_groups", "clinic_id", "INTEGER NOT NULL DEFAULT 1"),
    ("family_members", "clinic_id", "INTEGER NOT NULL DEFAULT 1"),
    ("idempotency_records", "clinic_id", "INTEGER NOT NULL DEFAULT 1"),
    ("messages", "clinic_id", "INTEGER NOT NULL DEFAULT 1"),
    ("patient_sessions", "clinic_id", "INTEGER NOT NULL DEFAULT 1"),
    ("patients", "clinic_id", "INTEGER NOT NULL DEFAULT 1"),
    ("pending_requests", "clinic_id", "INTEGER NOT NULL DEFAULT 1"),
    ("proposal_items", "clinic_id", "INTEGER NOT NULL DEFAULT 1"),
    ("provider_specialties", "clinic_id", "INTEGER NOT NULL DEFAULT 1"),
    ("providers", "clinic_id", "INTEGER NOT NULL DEFAULT 1"),
    ("seed_manifests", "clinic_id", "INTEGER NOT NULL DEFAULT 1"),
    ("slot_claims", "clinic_id", "INTEGER NOT NULL DEFAULT 1"),
    ("staff_alerts", "clinic_id", "INTEGER NOT NULL DEFAULT 1"),
    ("staff_sessions", "clinic_id", "INTEGER NOT NULL DEFAULT 1"),
    ("staff_users", "clinic_id", "INTEGER NOT NULL DEFAULT 1"),
)

# Indexes introduced after the corresponding tables already existed. Without
# these, an upgraded database would silently lose the constraints concurrency
# depends on (``create_all`` only creates missing *tables*, never missing
# indexes). Hand-writing the DDL for each one drifts the moment a model changes,
# so every statement is compiled from the model itself and a fresh database and
# an upgraded one are built from a single source of truth. That covers the
# tenancy set too: ``ix_<table>_clinic_id`` on all 26 tenant tables, the uniques
# that used to be global (``uq_idempotency_scope_key``, ``uq_staff_username``) and
# the widened hot-path reads rely on (``ix_appointments_clinic_provider_start``,
# ``ix_slots_clinic_status_start``, ``ix_messages_clinic_conversation``).
_DIALECT = sqlite.dialect()


def _declared_index_specs() -> tuple[tuple[str, str, tuple[str, ...], str], ...]:
    """``(name, table, columns, ddl)`` for every index the models declare."""
    specs = []
    for table in Base.metadata.tables.values():
        for index in table.indexes:
            ddl = str(CreateIndex(index).compile(dialect=_DIALECT)).strip()
            specs.append((index.name, table.name, tuple(col.name for col in index.columns), ddl))
    return tuple(specs)


_INDEX_SPECS = _declared_index_specs()
_INDEX_DDL = {name: ddl for name, _table, _columns, ddl in _INDEX_SPECS}

# Name + statement, as the module's readable contract: any of these that a given
# database is missing gets created before it is used again.
_REQUIRED_INDEXES: tuple[tuple[str, str], ...] = tuple(
    (name, ddl) for name, _table, _columns, ddl in _INDEX_SPECS
)


def _sync_indexes(
    connection: Connection, existing_tables: set[str], applied: list[str]
) -> None:
    """Create the indexes the models declare and this database lacks.

    A same-named index with a *different* column list is dropped and rebuilt.
    ``uq_idempotency_scope_key`` was ``(scope, key)`` and is now
    ``(clinic_id, scope, key)``, and SQLite has no ``ALTER INDEX`` -- so "an index
    of that name exists" is not proof the constraint is the right constraint.
    Leaving the old one in place would let one clinic's idempotency key block
    another clinic's retry, which is the failure tenancy is meant to remove.
    """
    inspector = inspect(connection)
    for name, table, columns, ddl in _INDEX_SPECS:
        if table not in existing_tables:
            continue
        stored = next((i for i in inspector.get_indexes(table) if i["name"] == name), None)
        if stored is not None and tuple(stored["column_names"] or ()) == columns:
            continue
        replacing = stored is not None
        if replacing:
            connection.execute(text(f'DROP INDEX IF EXISTS "{name}"'))
        try:
            connection.execute(text(ddl))
        except Exception:  # noqa: BLE001 - never block startup on an index
            logger.warning("Could not create index %s on %s", name, table, exc_info=True)
            applied.append(f"index:{name}:dropped" if replacing else f"index:{name}:failed")
            continue
        applied.append(f"index:{name}:replaced" if replacing else f"index:{name}")
        logger.info("Created missing index %s on %s", name, table)


def _appointment_status_allows_rescheduled(connection: Connection) -> bool:
    """Whether the stored ``appointments`` CHECK constraint accepts ``rescheduled``."""
    row = connection.execute(
        text("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'appointments'")
    ).first()
    if row is None or row[0] is None:
        return True
    # Do not use a plain substring check: additive columns such as
    # `rescheduled_to_id` are installed before this check and would make an old
    # status constraint look upgraded. Inspect the status IN(...) clause.
    return bool(
        re.search(
            r"status\s+in\s*\([^)]*['\"]rescheduled['\"]",
            str(row[0]),
            flags=re.IGNORECASE,
        )
    )


def _rebuild_table(connection: Connection, table_name: str, *, reason: str) -> None:
    """Recreate one table from its model definition, preserving every row.

    SQLite cannot add or drop a UNIQUE clause, change a primary key, or alter a
    CHECK with ``ALTER TABLE``. The documented sequence is: turn foreign keys
    off, rename the old table aside, create the table as the models now declare
    it, copy the columns the two versions share, drop the old table, put the
    indexes back, then re-check foreign keys.

    ``legacy_alter_table`` is switched ON for the rename on purpose. With the
    default behaviour SQLite rewrites *every other table's* ``REFERENCES`` clause
    to follow the temporary ``_old`` name, so dropping that table would leave
    them dangling; staying legacy keeps those references pointing at the name
    this function is about to recreate.

    Indexes whose names the model owns are rebuilt from the model instead of
    replayed from ``sqlite_master``: a stored ``uq_staff_username`` was global,
    and re-running its text would reinstall exactly the constraint tenancy
    removed. Anything else the table had is carried over verbatim.

    This is the only non-additive step in the module. It never drops a row or a
    column, and it runs inside the caller's transaction, so a failure leaves the
    original table in place.
    """
    model_table = Base.metadata.tables[table_name]
    new_ddl = str(CreateTable(model_table).compile(dialect=_DIALECT)).strip()
    stored_indexes = connection.execute(
        text(
            "SELECT name, sql FROM sqlite_master "
            "WHERE type = 'index' AND tbl_name = :table AND sql IS NOT NULL"
        ),
        {"table": table_name},
    ).fetchall()
    declared_names = {index.name for index in model_table.indexes}
    carry_over = [row[1] for row in stored_indexes if row[0] not in declared_names]

    legacy = connection.execute(text("PRAGMA legacy_alter_table")).scalar()
    connection.execute(text("PRAGMA legacy_alter_table = ON"))
    connection.execute(text("PRAGMA foreign_keys = OFF"))
    try:
        connection.execute(text(f'ALTER TABLE "{table_name}" RENAME TO "{table_name}_old"'))
        connection.execute(text(new_ddl))
        stored_columns = {
            row[1] for row in connection.execute(text(f'PRAGMA table_info("{table_name}_old")'))
        }
        shared = sorted(stored_columns & {column.name for column in model_table.columns})
        if not shared:
            raise RuntimeError(f"{table_name} rebuild found no columns in common")
        quoted = ", ".join(f'"{name}"' for name in shared)
        connection.execute(
            text(f'INSERT INTO "{table_name}" ({quoted}) SELECT {quoted} FROM "{table_name}_old"')
        )
        connection.execute(text(f'DROP TABLE "{table_name}_old"'))
        for statement in carry_over:
            connection.execute(text(statement))
        for name in sorted(declared_names):
            connection.execute(text(_INDEX_DDL[name]))
    finally:
        connection.execute(text("PRAGMA foreign_keys = ON"))
        if legacy is not None:
            connection.execute(text(f"PRAGMA legacy_alter_table = {int(legacy)}"))

    violations = connection.execute(text("PRAGMA foreign_key_check")).fetchall()
    if violations:
        raise RuntimeError(
            f"{table_name} rebuild ({reason}) left {len(violations)} "
            "foreign-key violation(s); aborting."
        )
    logger.info("Rebuilt %s (%s)", table_name, reason)


def _constraint_reasons(inspector, model_table) -> list[str]:  # noqa: ANN001
    """Why a stored table cannot satisfy its model definition without a rebuild.

    Column *sets* are compared rather than constraint names, because a
    pre-tenancy database wrote ``UNIQUE (seed_key)`` with no name at all -- matching
    by name would miss the very change this exists to catch. Only a *missing*
    declaration counts as a reason: a stored constraint the models no longer
    declare is exactly what the rebuild discards, and treating extras as reasons
    would mistake a partial unique index for a table constraint and rebuild the
    same table on every startup.
    """
    name = model_table.name
    reasons: list[str] = []
    stored_pk = tuple(inspector.get_pk_constraint(name)["constrained_columns"])
    model_pk = sorted(column.name for column in model_table.primary_key.columns)
    if sorted(stored_pk) != model_pk:
        reasons.append(f"primary key {tuple(stored_pk)} -> {tuple(model_pk)}")
    stored_uq = {
        tuple(sorted(item["column_names"])) for item in inspector.get_unique_constraints(name)
    }
    for constraint in model_table.constraints:
        if not isinstance(constraint, UniqueConstraint):
            continue
        columns = tuple(sorted(column.name for column in constraint.columns))
        if columns not in stored_uq:
            reasons.append(f"unique {columns} is missing")
    return reasons


def _ensure_legacy_clinic_row(connection: Connection) -> int:
    """Make tenant 1 exist before any ``clinic_id`` backfill runs.

    ``ADD COLUMN ... NOT NULL DEFAULT 1`` attributes every pre-tenancy row to
    clinic id 1, so that id has to be the clinic the database was written for --
    otherwise an upgrade quietly re-homes a whole practice's patients. The row
    comes from the same template the seeding path uses, so an upgraded volume and
    a fresh container describe the same practice instead of drifting apart.

    The session works on a savepoint because the caller owns the transaction:
    seeding the tenant row must be committable without committing (or rolling
    back) the rest of the migration around it.
    """
    from app.seed_tenants import TENANT_PLANS
    from app.services import seed

    plan = TENANT_PLANS[0]

    def _stored_id() -> int | None:
        return connection.execute(
            text("SELECT id FROM clinics WHERE slug = :slug"), {"slug": plan.slug}
        ).scalar()

    clinic_id = _stored_id()
    if clinic_id is None:
        session = Session(bind=connection, join_transaction_mode="create_savepoint")
        try:
            seed.ensure_clinic_rows(session, TENANT_PLANS[:1])
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()
        clinic_id = _stored_id()
    if clinic_id is None or int(clinic_id) != 1:
        raise RuntimeError(
            f"legacy backfill needs clinic 1 to be {plan.slug!r}, found {clinic_id}; "
            "refusing to attribute existing rows to the wrong clinic"
        )
    return int(clinic_id)


def apply_additive_migrations(connection: Connection) -> list[str]:
    """Bring an existing database up to the models. Returns a list of applied changes.

    The order is the whole trick: the tenant row first, so a ``clinic_id`` whose
    default is 1 points at something real; columns next, so a rebuild's model DDL
    finds them already populated and copies the backfilled values across; the
    rebuilds last, because they recreate constraints from the current models.
    """
    inspector = inspect(connection)
    existing_tables = set(inspector.get_table_names())
    applied: list[str] = []

    needs_backfill = any(
        table in existing_tables
        and column not in {item["name"] for item in inspector.get_columns(table)}
        for table, column, _ddl in _ADDED_COLUMNS
        if column == "clinic_id"
    )
    if "clinics" in existing_tables and needs_backfill:
        _ensure_legacy_clinic_row(connection)

    for table, column, ddl in _ADDED_COLUMNS:
        if table not in existing_tables:
            continue  # create_all will have produced a complete table
        columns = {item["name"] for item in inspector.get_columns(table)}
        if column in columns:
            continue
        connection.execute(text(f'ALTER TABLE "{table}" ADD COLUMN "{column}" {ddl}'))
        applied.append(f"{table}.{column}")
        logger.info("Added missing column %s.%s", table, column)

    # A status CHECK is baked into the table definition, so a database created
    # before ``rescheduled`` existed would reject the new value mid-transaction.
    # Tenancy has the same shape of problem: ``UNIQUE (seed_key)`` and a
    # single-column ``app_metadata`` primary key are not per-clinic and cannot be
    # altered into one, so those tables are rebuilt from their models.
    inspector = inspect(connection)
    for model_table in Base.metadata.tables.values():
        if model_table.name not in existing_tables:
            continue
        reasons = _constraint_reasons(inspector, model_table)
        if model_table.name == "appointments" and not _appointment_status_allows_rescheduled(
            connection
        ):
            reasons.append("status CHECK does not allow 'rescheduled'")
        if not reasons:
            continue
        _rebuild_table(connection, model_table.name, reason="; ".join(reasons))
        applied.append(f"rebuild:{model_table.name}")

    # ``create_all`` creates missing tables but never missing indexes, so an
    # upgraded database would silently lose the unique hold constraints that
    # make concurrent booking safe.
    _sync_indexes(connection, existing_tables, applied)

    # Keys that existed before the `public` flag was introduced hold demo state
    # that is safe to expose; mark them explicitly so `/api/demo/status` keeps
    # its Phase 1 shape after an upgrade.
    if "app_metadata" in existing_tables:
        connection.execute(
            text(
                "UPDATE app_metadata SET public = 1 "
                "WHERE key IN ('reset_generation', 'reset_count', 'last_reset_at', "
                "'last_seeded_at', 'schema_version')"
            )
        )

    return applied
