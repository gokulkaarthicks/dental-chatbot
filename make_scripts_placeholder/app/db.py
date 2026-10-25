"""Database engine, session factory, and pragma enforcement.

SQLite pragmas that are *connection scoped* (foreign keys, busy timeout) are set
on every new connection through an event listener; WAL mode is persistent once
the database file is created and is also asserted per connection for safety.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool

from app.config import get_settings

logger = logging.getLogger(__name__)

BUSY_TIMEOUT_MS = 5000

_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None


def database_path() -> Path:
    return get_settings().database_path


def database_url() -> str:
    return get_settings().database_url


def _apply_pragmas(connection: sqlite3.Connection, record: Any) -> None:
    """Runs for every physical connection. ``connection`` is the raw sqlite3 one."""
    cursor = connection.cursor()
    try:
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        cursor.execute("PRAGMA synchronous=NORMAL")
    finally:
        cursor.close()


def create_db_engine(url: str | None = None) -> Engine:
    target_url = url or database_url()
    if target_url.startswith("sqlite"):
        path = database_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        # NullPool keeps connection handling predictable: the reset service can
        # drop every writer, delete the file, and open a brand-new connection
        # without pooled handles pointing at a removed inode.
        engine = create_engine(target_url, future=True, poolclass=NullPool)
    else:
        engine = create_engine(target_url, future=True)
    event.listen(engine, "connect", _apply_pragmas)
    return engine


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        _engine = create_db_engine()
    return _engine


def get_session_factory() -> sessionmaker[Session]:
    global _session_factory
    if _session_factory is None:
        _session_factory = sessionmaker(bind=get_engine(), expire_on_commit=False)
    return _session_factory


def new_session() -> Session:
    return get_session_factory()()


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transaction boundary: commit on success, rollback on any exception."""
    session = new_session()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def dispose_engine() -> None:
    """Dispose all pooled connections (used before deleting/replacing the DB)."""
    global _engine, _session_factory
    if _engine is not None:
        _engine.dispose()
        _engine = None
    _session_factory = None


def sqlite_sidecar_paths(path: Path) -> tuple[Path, ...]:
    return (path, Path(f"{path}-wal"), Path(f"{path}-shm"))


def delete_database_files() -> None:
    """Remove the database plus WAL/SHM sidecars. Fails loudly if still locked."""
    dispose_engine()
    for candidate in sqlite_sidecar_paths(database_path()):
        if candidate.exists():
            os.remove(candidate)
            logger.info("Removed %s", candidate)


def count_rows(session: Session, table: str) -> int:
    # Table names are internal constants only; never user input.
    return int(session.execute(text(f'SELECT COUNT(*) FROM "{table}"')).scalar_one())


def get_db() -> Iterator[Session]:
    """FastAPI dependency. Read-mostly endpoints never mutate, but a shared
    session per request keeps behaviour uniform as workflows land later."""
    session = new_session()
    try:
        yield session
    finally:
        session.close()
