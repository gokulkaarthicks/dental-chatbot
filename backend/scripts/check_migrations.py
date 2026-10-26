"""Verify that additive migrations upgrade an older database in place.

Simulates a pre-tenancy database: every table present, but with no ``clinic_id``,
the uniques that used to be global written the way SQLite stored them (inline,
unnamed), the old single-column ``app_metadata`` primary key, the two same-named
unique indexes whose columns changed, and an ``appointments`` status CHECK that
does not allow ``rescheduled``. Then runs the reconciler and asserts the result:
tenant 1 exists, every legacy row belongs to it, the per-clinic constraints
replaced the global ones, nothing was lost, and two clinics can now hold the same
``seed_key`` -- which is the whole point, and the only proof that the constraints
really changed rather than just being renamed in the models.
"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

TMP = Path(tempfile.mkdtemp(prefix="dental-migrate-"))
DB = TMP / "old.db"

from sqlalchemy import create_engine, inspect, text  # noqa: E402

from app.migrations import (  # noqa: E402
    _ADDED_COLUMNS,
    _INDEX_SPECS,
    _appointment_status_allows_rescheduled,
    apply_additive_migrations,
)
from app.models import Base  # noqa: E402
from app.seed_tenants import TENANT_PLANS  # noqa: E402

# Constraint clauses the pre-tenancy schema carried. Written as SQLite stored
# them: unnamed inline UNIQUE for the columns that ``mapped_column(unique=True)``
# covered, which is precisely why the reconciler has to compare column sets.
_OLD_CONSTRAINTS: dict[str, tuple[str, ...]] = {
    "patients": ("UNIQUE (seed_key)",),
    "providers": ("UNIQUE (seed_key)", "UNIQUE (email)"),
    "appointment_types": ("UNIQUE (code)",),
    "seed_manifests": ("UNIQUE (fixture_set)",),
    "family_groups": ("UNIQUE (seed_key)",),
    "staff_users": ("UNIQUE (seed_key)", "UNIQUE (email)"),
    "provider_specialties": (
        "CONSTRAINT uq_provider_specialty UNIQUE (provider_id, specialty)",
    ),
    "appointments": ("CHECK (status IN ('booked', 'cancelled', 'completed'))",),
}

# Indexes that existed under the old, global shape. Same names as the models use
# now, different columns -- so "an index of that name exists" must not be enough.
_OLD_INDEXES = (
    'CREATE UNIQUE INDEX uq_idempotency_scope_key ON idempotency_records (scope, "key")',
    "CREATE UNIQUE INDEX uq_staff_username ON staff_users (username) WHERE username IS NOT NULL",
)

# Tables tenancy introduced. They did not exist on the volume being upgraded, so
# the simulation leaves them out and lets ``create_all`` add them complete --
# which is exactly the order ``create_schema`` runs in.
_NEW_TENANCY_TABLES = {"clinics", "clinic_insurance_plans", "clinic_policies"}

engine = create_engine(f"sqlite+pysqlite:///{DB}", future=True)
# The volume being upgraded is at the previous schema version: every column that
# predates tenancy is there, and only ``clinic_id`` is missing. Rewinding the
# older additive columns too would simulate a database that no deployment runs.
omitted = {(table, column) for table, column, _ddl in _ADDED_COLUMNS if column == "clinic_id"}
with engine.begin() as connection:
    # Declaration order, not ``sorted_tables``: these statements carry no foreign
    # keys, so creation order is irrelevant, and the sorted walk only adds a
    # cycle warning for a schema that has one.
    for table in Base.metadata.tables.values():
        if table.name in _NEW_TENANCY_TABLES:
            continue
        declarations = []
        for column in table.columns:
            if (table.name, column.name) in omitted:
                continue
            type_sql = column.type.compile(dialect=engine.dialect)
            ddl = f'"{column.name}" {type_sql}'
            if column.primary_key:
                ddl += " PRIMARY KEY"
            elif not column.nullable:
                ddl += " NOT NULL"
                default = getattr(column, "default", None)
                if default is not None and getattr(default, "is_scalar", False):
                    value = default.arg
                    ddl += f" DEFAULT '{value}'" if isinstance(value, str) else f" DEFAULT {value}"
            declarations.append(ddl)
        declarations.extend(_OLD_CONSTRAINTS.get(table.name, ()))
        connection.execute(text(f'CREATE TABLE "{table.name}" ({", ".join(declarations)})'))
    for statement in _OLD_INDEXES:
        connection.execute(text(statement))
# What ``create_schema`` does before the reconciler runs: missing tables appear,
# fully formed.
Base.metadata.create_all(engine)
engine.dispose()

# A small coherent dataset: a patient, a provider, an appointment type, a booked
# appointment, a staff user, an idempotency row and the demo generation counter.
NOW = datetime.now(UTC).replace(tzinfo=None, microsecond=0).isoformat(sep=" ")


def _rows() -> list[tuple[str, tuple]]:
    return [
        (
            "INSERT INTO app_metadata (key, value, updated_at) "
            "VALUES ('reset_generation', 'gen-old', '2026-01-01')",
            (),
        ),
        (
            "INSERT INTO app_metadata (key, value, updated_at) "
            "VALUES ('reset_count', '7', '2026-01-01')",
            (),
        ),
        (
            "INSERT INTO patients (id, seed_key, full_name, phone, date_of_birth, "
            "created_at, updated_at) VALUES (1, 'patient:dana', 'Dana Reyes', "
            "'+14155550100', '1988-04-02', :now, :now)",
            {"now": NOW},
        ),
        (
            "INSERT INTO providers (id, seed_key, full_name, credentials, email, active, "
            "created_at, updated_at) VALUES (1, 'provider:nhai', 'Nhai Ostrom', 'DDS', "
            "'nhai@oralbdental.example', 1, :now, :now)",
            {"now": NOW},
        ),
        (
            "INSERT INTO appointment_types (id, code, name, duration_minutes, is_emergency, "
            "sort_order) VALUES (1, 'new-patient', 'New Patient Exam', 60, 0, 1)",
            (),
        ),
        (
            "INSERT INTO appointments (id, patient_id, provider_id, appointment_type_id, "
            "starts_at, ends_at, duration_minutes, status, created_at, updated_at) "
            "VALUES (1, 1, 1, 1, '2026-02-03 15:00:00', '2026-02-03 16:00:00', 60, "
            "'booked', :now, :now)",
            {"now": NOW},
        ),
        (
            "INSERT INTO staff_users (id, seed_key, email, username, display_name, role, "
            "password_hash, active, created_at, updated_at) VALUES (1, 'staff:reception', "
            "'front@oralbdental.example', 'frontdesk', 'Front Desk', 'receptionist', "
            "'x', 1, :now, :now)",
            {"now": NOW},
        ),
        (
            'INSERT INTO idempotency_records ("key", scope, reset_generation, created_at) '
            "VALUES ('k1', 'confirm', 'gen-old', :now)",
            {"now": NOW},
        ),
        (
            "INSERT INTO seed_manifests (id, fixture_set, fixture_version, checksum, "
            "applied_at, created_at, updated_at) VALUES (1, 'core', 1, 'abc', "
            ":now, :now, :now)",
            {"now": NOW},
        ),
    ]


raw = sqlite3.connect(DB)
for statement, params in _rows():
    raw.execute(statement, params)
raw.commit()
raw.close()
print(f"old schema simulates {len(omitted)} missing columns and global uniques")

engine = create_engine(f"sqlite+pysqlite:///{DB}", future=True)
with engine.begin() as connection:
    applied = apply_additive_migrations(connection)
engine.dispose()

check = create_engine(f"sqlite+pysqlite:///{DB}", future=True)
inspector = inspect(check)
tables = set(inspector.get_table_names())
db = sqlite3.connect(DB)

missing_columns = [
    f"{table}.{column}"
    for table, column, _ddl in _ADDED_COLUMNS
    if table in tables and column not in {c["name"] for c in inspector.get_columns(table)}
]

# Every index the models declare, with the exact columns, or the upgrade did not
# really replace the global constraint.
present_indexes = {table: {i["name"]: i for i in inspector.get_indexes(table)} for table in tables}
missing_indexes = [
    name
    for name, table, columns, _ddl in _INDEX_SPECS
    if table in tables and name not in present_indexes[table]
]
stale_indexes = [
    name
    for name, table, columns, _ddl in _INDEX_SPECS
    if table in tables
    and name in present_indexes[table]
    and tuple(present_indexes[table][name]["column_names"] or ()) != columns
]

# The uniques that moved from global to per-clinic live in the table definition,
# so they are asserted through the constraint column sets the reconciler rebuilt.
_EXPECTED_UNIQUES = {
    "patients": ("clinic_id", "seed_key"),
    "providers": ("clinic_id", "email"),
    "appointment_types": ("clinic_id", "code"),
    "seed_manifests": ("clinic_id", "fixture_set"),
    "family_groups": ("clinic_id", "seed_key"),
    "staff_users": ("clinic_id", "email"),
    "provider_specialties": ("clinic_id", "provider_id", "specialty"),
}
missing_uniques = [
    f"{table}:{columns}"
    for table, columns in _EXPECTED_UNIQUES.items()
    if columns
    not in {
        tuple(sorted(item["column_names"])) for item in inspector.get_unique_constraints(table)
    }
]
wrong_pk = [
    table
    for table, expected in (("app_metadata", ("clinic_id", "key")),)
    if tuple(sorted(inspector.get_pk_constraint(table)["constrained_columns"])) != expected
]

tenant_tables = [
    t.name for t in Base.metadata.tables.values() if "clinic_id" in t.columns and t.name in tables
]
unbackfilled = [
    table
    for table in tenant_tables
    if db.execute(f'SELECT count(*) FROM "{table}" WHERE clinic_id IS NULL').fetchone()[0]
]

generation = db.execute(
    "SELECT clinic_id, value, public FROM app_metadata WHERE key = 'reset_generation'"
).fetchone()
resets = db.execute("SELECT value FROM app_metadata WHERE key = 'reset_count'").fetchone()
patient = db.execute("SELECT clinic_id, seed_key, full_name FROM patients WHERE id = 1").fetchone()
appointment_status = db.execute("SELECT status, clinic_id FROM appointments WHERE id = 1").fetchone()
clinic_row = db.execute(
    "SELECT id, slug, name, timezone, slot_minutes FROM clinics"
).fetchall()
fk_violations = db.execute("PRAGMA foreign_key_check").fetchall()

# The proof that tenancy is real: a second clinic may reuse tenant 1's seed_key
# and email, which the old global uniques refused. Two tenants with the same
# patient name is an ordinary Tuesday; a database that rejects it is still
# single-tenant no matter what the models say.
cross_tenant_error = None
try:
    db.execute(
        "INSERT INTO clinics (id, slug, public_id, name, address_line1, city, state, "
        "postal_code, phone, email, timezone, slot_minutes, open_time, close_time, "
        "open_weekdays, closed_days, seed_horizon_days, self_pay_label, fictional, "
        "is_default, active, created_at, updated_at) VALUES (2, 'second', 'c2', 'Second', "
        "'1 x', 'Y', 'CA', '00000', 'p', 'e@x', 'America/New_York', 20, '09:00', '17:00', "
        "'[1,2,3,4,5]', '[6,0]', 60, 'Self-pay', 1, 0, 1, ?, ?)",
        (NOW, NOW),
    )
    db.execute(
        "INSERT INTO patients (id, clinic_id, seed_key, full_name, phone, date_of_birth, "
        "created_at, updated_at) VALUES (2, 2, 'patient:dana', 'Other Dana', '+14155550111', "
        "'1988-04-02', ?, ?)",
        (NOW, NOW),
    )
    db.execute(
        'INSERT INTO idempotency_records (clinic_id, "key", scope, reset_generation, '
        "created_at) VALUES (2, 'k1', 'confirm', 'gen-old', ?)",
        (NOW,),
    )
    db.commit()
except sqlite3.IntegrityError as exc:
    cross_tenant_error = str(exc)
    db.rollback()

with check.connect() as probe:
    status_check_ok = _appointment_status_allows_rescheduled(probe)

print("applied:", applied)
print("missing columns:", missing_columns or "none")
print("missing indexes:", missing_indexes or "none")
print("stale indexes:", stale_indexes or "none")
print("missing per-clinic uniques:", missing_uniques or "none")
print("wrong primary keys:", wrong_pk or "none")
print("tables with unbackfilled clinic_id:", unbackfilled or "none")
print("clinics:", clinic_row)
print("demo metadata preserved:", generation, "| reset_count:", resets)
print("patient preserved:", patient, "| appointment:", appointment_status)
print("foreign key violations:", fk_violations or "none")
print("appointments status CHECK allows rescheduled:", status_check_ok)
print("second clinic accepted duplicate seed_key:", cross_tenant_error or "yes")

assert not missing_columns, missing_columns
assert not missing_indexes, missing_indexes
assert not stale_indexes, stale_indexes
assert not missing_uniques, missing_uniques
assert not wrong_pk, wrong_pk
assert not unbackfilled, unbackfilled
assert not fk_violations, fk_violations
assert generation == (1, "gen-old", 1), generation
assert resets == ("7",), resets
assert patient == (1, "patient:dana", "Dana Reyes"), patient
assert appointment_status == ("booked", 1), appointment_status
assert [row[1] for row in clinic_row] == [TENANT_PLANS[0].slug], clinic_row
assert clinic_row[0][0] == 1, clinic_row
assert status_check_ok
assert cross_tenant_error is None, cross_tenant_error
print("migration OK")
