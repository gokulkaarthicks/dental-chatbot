"""Upgrading a volume written before tenancy existed.

The reconciler runs on every startup against whatever file is there, so what
matters is not "did it run" but *what happened to the rows that were already in
it*. Four things have to be true afterwards, and each one is a test below:

* every tenant table gained ``clinic_id``, backfilled to the clinic the volume
  was written for -- not to whatever clinic happened to be created first;
* every pre-existing row is still there, with its values intact;
* the uniques that used to be global are now per-clinic, so a second practice can
  hold the same ``seed_key`` or email address as the first;
* nothing is left dangling, and the indexes the query paths need exist.

The fixture is built the way a real upgrade happens rather than by hand-writing
an "old" schema: the current models are compiled and then *de-tenanted*, which is
precisely the difference between the two versions. A hand-written old table drifts
from the models on every column added later, and the test then fails on its own
fixture instead of on the migration.
"""

from __future__ import annotations

import re

import pytest
from sqlalchemy import Engine, inspect, text
from sqlalchemy.schema import CreateTable

from app.db import create_db_engine
from app.migrations import apply_additive_migrations
from app.models import Base

# Tenancy introduced these three tables. A volume written before it has none of
# them, which is why ``create_all`` runs first in ``create_schema`` -- the
# reconciler can only add a column to a table that exists.
TENANCY_ONLY_TABLES = frozenset({"clinics", "clinic_insurance_plans", "clinic_policies"})

# One row of each kind, in the pre-tenancy shape. FK-free tables only: this is a
# fixture, not a reconstruction of the whole practice, and the four tables chosen
# are all rebuilt by the migration (so "the row survived" is a real assertion).
LEGACY_PATIENT = {
    "id": 1,
    "seed_key": "patient:legacy",
    "full_name": "Dana Reyes",
    "phone": "+16505550123",
    "date_of_birth": "1990-04-12",
}
LEGACY_PROVIDER = {
    "id": 1,
    "seed_key": "provider:legacy",
    "full_name": "Dr. Naomi Bell",
    "credentials": "DDS",
    "email": "naomi.bell@example.test",
    "active": 1,
}
LEGACY_TYPE = {
    "id": 1,
    "code": "cleaning",
    "name": "Cleaning",
    "duration_minutes": 60,
    "is_emergency": 0,
    "sort_order": 1,
}
LEGACY_RESET_GENERATION = "gen-before-tenancy"


def _detenant(ddl: str) -> str:
    """Rewrite one compiled model table into the shape it had before tenancy.

    Exactly three edits, each matching how the model declares the part: drop the
    ``clinic_id`` column, drop the foreign key that pointed at ``clinics``, and
    reduce the per-clinic uniques and primary key back to the global ones they
    replaced. Anything left behind fails the assertions rather than silently
    producing a schema no version ever wrote.
    """
    ddl = re.sub(
        r",\s*\n\s*FOREIGN KEY\(clinic_id\) REFERENCES clinics \(id\) ON DELETE \w+", "", ddl
    )
    ddl = re.sub(r"CONSTRAINT \w+ UNIQUE \(clinic_id, ([^)]*?)\)", r"UNIQUE (\1)", ddl)
    # ``key`` compiles quoted (it is a SQL keyword), so the primary key is matched
    # by shape rather than by its exact text.
    ddl = re.sub(r"PRIMARY KEY \(clinic_id, ([^)]*)\)", r"PRIMARY KEY (\1)", ddl)
    ddl = re.sub(r"\n\s*clinic_id INTEGER NOT NULL,", "", ddl)
    assert "clinic_id" not in ddl, ddl
    assert not re.search(r",\s*\)", ddl), f"dangling comma after de-tenancy:\n{ddl}"
    return ddl


def _create_legacy_schema(engine: Engine) -> None:
    """Create every table as it looked before tenancy.

    Iterated in declaration order rather than ``sorted_tables``: creation order
    does not matter, because SQLite resolves a foreign key when a row is written
    rather than when the table is created, and the sorted walk only adds a
    cycle warning for a schema that has one.
    """
    with engine.begin() as connection:
        for table in Base.metadata.tables.values():
            if table.name in TENANCY_ONLY_TABLES:
                continue
            ddl = str(CreateTable(table).compile(dialect=engine.dialect)).strip()
            connection.execute(text(_detenant(ddl)))


def _insert_legacy_rows(engine: Engine) -> None:
    """Write the rows a real practice would already have, with no clinic anywhere."""
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO patients (id, seed_key, full_name, phone, date_of_birth, "
                "created_at, updated_at) "
                "VALUES (:id, :seed_key, :full_name, :phone, :date_of_birth, :now, :now)"
            ),
            {**LEGACY_PATIENT, "now": "2026-01-05 09:00:00"},
        )
        connection.execute(
            text(
                "INSERT INTO providers (id, seed_key, full_name, credentials, email, active, "
                "created_at, updated_at) "
                "VALUES (:id, :seed_key, :full_name, :credentials, :email, :active, :now, :now)"
            ),
            {**LEGACY_PROVIDER, "now": "2026-01-05 09:00:00"},
        )
        connection.execute(
            text(
                "INSERT INTO appointment_types (id, code, name, duration_minutes, "
                "is_emergency, sort_order) "
                "VALUES (:id, :code, :name, :duration_minutes, :is_emergency, :sort_order)"
            ),
            LEGACY_TYPE,
        )
        # The two keys a demo reset and the status endpoint read.
        connection.execute(
            text(
                "INSERT INTO app_metadata (key, value, updated_at, public) "
                "VALUES ('reset_generation', :generation, :now, 1)"
            ),
            {"generation": LEGACY_RESET_GENERATION, "now": "2026-01-05 09:00:00"},
        )
        connection.execute(
            text(
                "INSERT INTO app_metadata (key, value, updated_at, public) "
                "VALUES ('reset_count', '7', :now, 1)"
            ),
            {"now": "2026-01-05 09:00:00"},
        )


@pytest.fixture()
def upgraded(tmp_path):
    """A pre-tenancy file, upgraded the way ``create_schema`` upgrades one.

    The order is the contract: additive ``create_all`` first (it creates
    ``clinics`` and the knowledge tables), then the reconciler (which can only
    backfill against a tenant row that exists).
    """
    engine = create_db_engine(f"sqlite+pysqlite:///{tmp_path / 'legacy.db'}")
    _create_legacy_schema(engine)
    _insert_legacy_rows(engine)

    with engine.begin() as connection:
        before = set(inspect(connection).get_table_names())
    assert TENANCY_ONLY_TABLES.isdisjoint(before), "the fixture already looked tenanted"

    Base.metadata.create_all(bind=engine)
    with engine.begin() as connection:
        applied = apply_additive_migrations(connection)

    yield engine, applied
    engine.dispose()


def test_the_upgrade_adds_clinic_id_to_every_tenant_table(upgraded):
    """The column is the whole migration; one missed table is a leak later.

    The three tenancy-only tables are created whole by ``create_all`` rather than
    altered, so they are checked for the column but not for an ``applied`` entry.
    """
    engine, applied = upgraded
    scoped = [
        table for table in Base.metadata.tables.values() if "clinic_id" in table.columns
    ]
    assert len(scoped) > 20
    with engine.begin() as connection:
        inspector = inspect(connection)
        for table in scoped:
            columns = {item["name"] for item in inspector.get_columns(table.name)}
            assert "clinic_id" in columns, f"{table.name} was left unterminated"
            if table.name not in TENANCY_ONLY_TABLES:
                assert f"{table.name}.clinic_id" in applied


def test_every_pre_existing_row_is_attributed_to_the_first_clinic(upgraded):
    """A whole practice's chart must not be re-homed by a default value."""
    engine, _ = upgraded
    with engine.begin() as connection:
        assert connection.execute(
            text("SELECT DISTINCT clinic_id FROM patients")
        ).scalars().all() == [1]
        assert connection.execute(
            text("SELECT DISTINCT clinic_id FROM providers")
        ).scalars().all() == [1]
        assert connection.execute(
            text("SELECT DISTINCT clinic_id FROM appointment_types")
        ).scalars().all() == [1]
        assert connection.execute(
            text("SELECT DISTINCT clinic_id FROM app_metadata")
        ).scalars().all() == [1]


def test_the_legacy_rows_survive_the_table_rebuilds(upgraded):
    """The rebuilds copy rows; a rebuild that loses one is data loss."""
    engine, applied = upgraded
    assert "rebuild:patients" in applied
    assert "rebuild:app_metadata" in applied
    with engine.begin() as connection:
        patient = connection.execute(
            text("SELECT seed_key, full_name, phone FROM patients WHERE id = :id"),
            {"id": LEGACY_PATIENT["id"]},
        ).one()
        assert tuple(patient) == (
            LEGACY_PATIENT["seed_key"],
            LEGACY_PATIENT["full_name"],
            LEGACY_PATIENT["phone"],
        )

        provider = connection.execute(
            text("SELECT email, active FROM providers WHERE id = :id"),
            {"id": LEGACY_PROVIDER["id"]},
        ).one()
        assert tuple(provider) == (LEGACY_PROVIDER["email"], 1)

        generation = connection.execute(
            text("SELECT value, public FROM app_metadata WHERE key = 'reset_generation'")
        ).one()
        assert tuple(generation) == (LEGACY_RESET_GENERATION, 1)


def test_the_global_uniques_became_per_clinic(upgraded):
    """``seed_key`` and the metadata key are now namespaced, not global."""
    engine, _ = upgraded
    with engine.begin() as connection:
        inspector = inspect(connection)

        patient_uniques = {
            tuple(sorted(item["column_names"]))
            for item in inspector.get_unique_constraints("patients")
        }
        assert ("clinic_id", "seed_key") in patient_uniques
        assert ("seed_key",) not in patient_uniques

        provider_uniques = {
            tuple(sorted(item["column_names"]))
            for item in inspector.get_unique_constraints("providers")
        }
        assert ("clinic_id", "email") in provider_uniques

        # ``app_metadata`` could not be altered at all: its primary key was the
        # key itself, so both tenants would have shared one row.
        assert inspector.get_pk_constraint("app_metadata")["constrained_columns"] == [
            "clinic_id",
            "key",
        ]


def test_the_indexes_the_query_paths_need_exist(upgraded):
    """``create_all`` never adds an index, so an upgrade would silently lose them."""
    engine, applied = upgraded
    required = {
        "patients": "ix_patients_clinic_id",
        "messages": "ix_messages_clinic_conversation",
        "appointments": "ix_appointments_clinic_provider_start",
        "availability_slots": "ix_slots_clinic_status_start",
    }
    with engine.begin() as connection:
        inspector = inspect(connection)
        for table, name in required.items():
            stored = {item["name"] for item in inspector.get_indexes(table)}
            assert name in stored, f"{name} missing after upgrade"
        assert any(change.startswith("index:") for change in applied)


def test_the_first_clinic_row_was_created_as_the_default_tenant(upgraded):
    """``clinic_id = 1`` has to mean the practice the file was written for."""
    engine, _ = upgraded
    with engine.begin() as connection:
        rows = connection.execute(text("SELECT id, slug, is_default FROM clinics")).all()
    assert [(row[0], row[1]) for row in rows] == [(1, "oral-b-dental-care")]
    assert rows[0][2] == 1


def test_the_upgraded_schema_has_no_dangling_foreign_keys(upgraded):
    """The clinic foreign key is re-created by the rebuild, so it has to hold."""
    engine, _ = upgraded
    with engine.begin() as connection:
        assert connection.execute(text("PRAGMA foreign_key_check")).fetchall() == []


def test_a_second_clinic_can_reuse_a_legacy_seed_key(upgraded):
    """The point of the composite unique: two practices, the same fixture keys."""
    engine, _ = upgraded
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO clinics (id, slug, public_id, name, address_line1, city, state, "
                "postal_code, phone, email, timezone, slot_minutes, open_time, close_time, "
                "open_weekdays, closed_days, seed_horizon_days, fictional, is_default, active, "
                "created_at, updated_at) "
                "VALUES (2, 'second-clinic', 'clinic_second', 'Second Clinic', '1 Test Way', "
                "'Brooklyn', 'NY', '11231', '+17185550100', 'hello@example.test', "
                "'America/New_York', 20, '09:00:00', '17:00:00', :open_weekdays, :closed_days, "
                "60, 1, 0, 1, :now, :now)"
            ),
            {
                "open_weekdays": "[1,2,3,4,5]",
                "closed_days": '["Sun", "Mon"]',
                "now": "2026-01-06 09:00:00",
            },
        )
        connection.execute(
            text(
                "INSERT INTO patients (clinic_id, seed_key, full_name, phone, date_of_birth, "
                "created_at, updated_at) "
                "VALUES (2, :seed_key, 'Nadia Baptiste', '+16505550188', '1985-01-09', :now, :now)"
            ),
            {"seed_key": LEGACY_PATIENT["seed_key"], "now": "2026-01-06 09:00:00"},
        )
        rows = connection.execute(
            text(
                "SELECT clinic_id, seed_key FROM patients WHERE seed_key = :seed_key "
                "ORDER BY clinic_id"
            ),
            {"seed_key": LEGACY_PATIENT["seed_key"]},
        ).all()
    assert [tuple(row) for row in rows] == [(1, LEGACY_PATIENT["seed_key"]), (2, LEGACY_PATIENT["seed_key"])]


def test_the_migration_is_idempotent_on_the_upgraded_file(upgraded):
    """Every startup runs it, so a second pass must do nothing at all."""
    engine, _ = upgraded
    with engine.begin() as connection:
        second_pass = apply_additive_migrations(connection)
    assert second_pass == []
