"""Additive schema reconciliation for existing SQLite databases.

Phase 1 shipped without a migration tool because the schema was fixed. Phase 2
adds a handful of columns and one table, and an existing Docker volume must keep
its records rather than being wiped.

Strategy: ``Base.metadata.create_all()`` already creates *missing tables*, so
only *missing columns on existing tables* need handling. Each entry below is
idempotent and runs inside the caller's transaction. Nothing is ever dropped or
rewritten, so this is safe to run on every startup -- with one exception: a
changed CHECK constraint (SQLite cannot alter one) is applied by rebuilding the
table from the model definition and copying every row across.
"""

from __future__ import annotations

import logging
import re

from sqlalchemy import inspect, text
from sqlalchemy.engine import Connection

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
    ("staff_alerts", "caller_name", "VARCHAR(120)"),
    ("staff_alerts", "caller_phone", "VARCHAR(32)"),
)

# Indexes introduced after the corresponding tables already existed. Without
# these, an upgraded database would silently lose the constraints concurrency
# depends on (``create_all`` only creates missing *tables*, never missing
# indexes). Each statement is idempotent via ``IF NOT EXISTS`` and uses the
# exact DDL SQLAlchemy generates, so fresh and upgraded databases converge.
_REQUIRED_INDEXES: tuple[tuple[str, str], ...] = (
    (
        "uq_slot_claims_active_slot",
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_slot_claims_active_slot "
        "ON slot_claims (availability_slot_id) WHERE active = 1",
    ),
    (
        "uq_slot_claims_active_appointment_slot",
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_slot_claims_active_appointment_slot "
        "ON slot_claims (appointment_id, availability_slot_id) WHERE active = 1",
    ),
    (
        "ix_slot_claims_appointment",
        "CREATE INDEX IF NOT EXISTS ix_slot_claims_appointment ON slot_claims (appointment_id)",
    ),
    (
        "uq_idempotency_scope_key",
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_idempotency_scope_key "
        "ON idempotency_records (scope, key)",
    ),
    (
        "ix_proposals_confirm_key",
        "CREATE INDEX IF NOT EXISTS ix_proposals_confirm_key ON booking_proposals (confirm_key)",
    ),
    (
        "ix_staff_alerts_dedupe",
        "CREATE INDEX IF NOT EXISTS ix_staff_alerts_dedupe ON staff_alerts (dedupe_key)",
    ),
    (
        "ix_pending_requests_dedupe",
        "CREATE INDEX IF NOT EXISTS ix_pending_requests_dedupe ON pending_requests (dedupe_key)",
    ),
    (
        "uq_staff_username",
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_staff_username ON staff_users (username) "
        "WHERE username IS NOT NULL",
    ),
    (
        "ix_staff_sessions_user",
        "CREATE INDEX IF NOT EXISTS ix_staff_sessions_user ON staff_sessions (staff_user_id)",
    ),
    (
        "ix_staff_sessions_expires",
        "CREATE INDEX IF NOT EXISTS ix_staff_sessions_expires ON staff_sessions (expires_at)",
    ),
)


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


def _rebuild_appointments_table(connection: Connection) -> None:
    """Recreate ``appointments`` with the expanded status CHECK, preserving every row.

    SQLite has no ``ALTER TABLE ... DROP CONSTRAINT``. The documented sequence is:
    turn foreign keys off, capture and expand the stored table DDL, rename the
    old table, recreate that same schema with the new status value, copy every
    row, drop the old table, recreate its indexes, then re-check foreign keys.
    Anything that
    referenced ``appointments`` by name follows the rename automatically because
    SQLite rewrites those references with ``legacy_alter_table`` off (the
    default).

    This is the one non-additive step in this module. It keeps every existing
    row -- nothing is dropped -- but it runs inside the caller's transaction, so
    a failure leaves the original table in place.
    """
    original_sql = connection.execute(
        text("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'appointments'")
    ).scalar_one()
    upgraded_sql, replacements = re.subn(
        r"(status\s+IN\s*\()([^)]*)(\))",
        lambda match: (
            match.group(1) + match.group(2).rstrip() + ", 'rescheduled'" + match.group(3)
        ),
        original_sql,
        count=1,
        flags=re.IGNORECASE,
    )
    if replacements != 1:
        raise RuntimeError("Could not locate the appointments status constraint")
    index_sql = [
        row[0]
        for row in connection.execute(
            text(
                "SELECT sql FROM sqlite_master WHERE type = 'index' "
                "AND tbl_name = 'appointments' AND sql IS NOT NULL"
            )
        )
        if row[0]
    ]

    legacy = connection.execute(text("PRAGMA legacy_alter_table")).scalar()
    connection.execute(text("PRAGMA legacy_alter_table = ON"))
    connection.execute(text("PRAGMA foreign_keys = OFF"))
    try:
        connection.execute(text('ALTER TABLE "appointments" RENAME TO "appointments_old"'))
        connection.execute(text(upgraded_sql))
        columns = {
            row[1] for row in connection.execute(text('PRAGMA table_info("appointments_old")'))
        }
        shared = ", ".join(sorted(columns))
        connection.execute(
            text(f'INSERT INTO "appointments" ({shared}) SELECT {shared} FROM "appointments_old"')
        )
        connection.execute(text('DROP TABLE "appointments_old"'))
        for statement in index_sql:
            connection.execute(text(statement))
    finally:
        connection.execute(text("PRAGMA foreign_keys = ON"))
        if legacy is not None:
            connection.execute(text(f"PRAGMA legacy_alter_table = {int(legacy)}"))

    violations = connection.execute(text("PRAGMA foreign_key_check")).fetchall()
    if violations:
        raise RuntimeError(
            f"appointments rebuild left {len(violations)} foreign-key violation(s); aborting."
        )


def apply_additive_migrations(connection: Connection) -> list[str]:
    """Add any missing columns. Returns a list of applied changes for logging."""
    inspector = inspect(connection)
    existing_tables = set(inspector.get_table_names())
    applied: list[str] = []

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
    # before ``rescheduled`` existed would reject the new value with a
    # constraint error mid-transaction. SQLite cannot alter a constraint, so the
    # table is rebuilt with the current model definition (see helper below).
    if "appointments" in existing_tables and not _appointment_status_allows_rescheduled(
        connection
    ):
        _rebuild_appointments_table(connection)
        applied.append("appointments.status:add 'rescheduled'")
        logger.info("Rebuilt appointments table to allow status 'rescheduled'")

    # ``create_all`` creates missing tables but never missing indexes, so an
    # upgraded database would silently lose the unique hold constraints that
    # make concurrent booking safe. Create whatever the models declare.
    present: dict[str, set[str]] = {
        table: {index["name"] for index in inspector.get_indexes(table)}
        for table in existing_tables
    }
    for name, statement in _REQUIRED_INDEXES:
        if any(name in names for names in present.values()):
            continue
        try:
            connection.execute(text(statement))
            applied.append(f"index:{name}")
            logger.info("Created missing index %s", name)
        except Exception:  # noqa: BLE001 - never block startup on an index
            logger.warning("Could not create index %s", name, exc_info=True)

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
