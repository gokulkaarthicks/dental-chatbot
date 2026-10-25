"""Verify that additive migrations upgrade an older database in place.

Simulates a Phase-2 database: tables present, but without the Phase-3 columns
and indexes. Confirms nothing is lost and the concurrency guards come back.
"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

TMP = Path(tempfile.mkdtemp(prefix="dental-migrate-"))
DB = TMP / "old.db"

from sqlalchemy import create_engine, inspect, text  # noqa: E402

from app.migrations import (  # noqa: E402
    _ADDED_COLUMNS,
    _REQUIRED_INDEXES,
    apply_additive_migrations,
)
from app.models import Base  # noqa: E402

engine = create_engine(f"sqlite+pysqlite:///{DB}", future=True)
# Build the "old" schema: every table with only the columns that predate the
# Phase-3 additions. (Rewinding with DROP COLUMN fails on foreign keys.)
phase3_columns = {(table, column) for table, column, _ddl in _ADDED_COLUMNS}
with engine.begin() as connection:
    for table in Base.metadata.sorted_tables:
        declarations = []
        for column in table.columns:
            if (table.name, column.name) in phase3_columns:
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
        connection.execute(text(f'CREATE TABLE "{table.name}" ({", ".join(declarations)})'))
engine.dispose()

raw = sqlite3.connect(DB)
raw.execute(
    "INSERT INTO app_metadata (key, value, updated_at) VALUES ('reset_generation', 'gen-old', '2026-01-01')"
)
raw.commit()
raw.close()

print(f"old schema has {len(phase3_columns)} fewer columns and no indexes")

engine = create_engine(f"sqlite+pysqlite:///{DB}", future=True)
with engine.begin() as connection:
    applied = apply_additive_migrations(connection)
engine.dispose()

inspector = inspect(create_engine(f"sqlite+pysqlite:///{DB}", future=True))
tables = set(inspector.get_table_names())

missing_columns = [
    f"{table}.{column}"
    for table, column, _ddl in _ADDED_COLUMNS
    if table in tables and column not in {c["name"] for c in inspector.get_columns(table)}
]
present_indexes = {
    index["name"] for table in tables for index in inspector.get_indexes(table)
}
missing_indexes = [name for name, _stmt in _REQUIRED_INDEXES if name not in present_indexes]

raw = sqlite3.connect(DB)
generation = raw.execute(
    "SELECT value, public FROM app_metadata WHERE key = 'reset_generation'"
).fetchone()
raw.close()

print("applied:", applied)
print("missing columns:", missing_columns or "none")
print("missing indexes:", missing_indexes or "none")
print("demo metadata preserved and made public:", generation)
assert not missing_columns, missing_columns
assert not missing_indexes, missing_indexes
assert generation == ("gen-old", 1), generation
print("migration OK")
