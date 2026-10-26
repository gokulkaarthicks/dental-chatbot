"""Database engine, session factory, pragma enforcement, and tenant binding.

SQLite pragmas that are *connection scoped* (foreign keys, busy timeout) are set
on every new connection through an event listener; WAL mode is persistent once
the database file is created and is also asserted per connection for safety.

Tenant binding
--------------
The engine and session factory stay global and tenant-blind. A *session* is what
carries the tenant, in ``session.info["clinic_id"]``, and ``app.models.base`` reads
that value to filter selects and to reject cross-tenant writes. Keeping the tenant
on the session (rather than on the engine or in a contextvar) means:

* one connection pool serves every clinic;
* a session that was never bound can only run unscoped administrative reads,
  which is exactly what the tenant resolver and the bootstrap path need;
* forgetting to bind is visible: the insert guard fails on the first write.
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
CLINIC_ID_KEY = "clinic_id"

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


def new_session(clinic_id: int | None = None) -> Session:
    session = get_session_factory()()
    if clinic_id is not None:
        bind_tenant(session, clinic_id)
    return session


def bind_tenant(session: Session, clinic_id: int) -> Session:
    """Attach a tenant to an open session. Idempotent.

    Called by the HTTP dependency once the tenant is resolved, and by services
    that build their own transaction. Rebinding to a *different* clinic is a bug
    (it would silently re-scope reads already taken through the old tenant), so it
    raises rather than swapping underneath the caller.
    """
    existing = session.info.get(CLINIC_ID_KEY)
    if existing is not None and int(existing) != int(clinic_id):
        raise RuntimeError(
            f"session already bound to clinic_id={existing}; refusing to rebind to "
            f"clinic_id={clinic_id}"
        )
    session.info[CLINIC_ID_KEY] = int(clinic_id)
    return session


def bound_clinic_id(session: Session) -> int | None:
    value = session.info.get(CLINIC_ID_KEY)
    return int(value) if value is not None else None


def require_clinic_id(session: Session) -> int:
    """The tenant this session is bound to.

    Services call this instead of trusting a passed-in clinic id, so a route that
    resolved tenant A cannot hand a service tenant B's identifier.
    """
    clinic_id = bound_clinic_id(session)
    if clinic_id is None:
        raise RuntimeError("session is not bound to a clinic")
    return clinic_id


@contextmanager
def session_scope(clinic_id: int | None = None) -> Iterator[Session]:
    """Transaction boundary: commit on success, rollback on any exception."""
    session = new_session(clinic_id)
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


def count_rows(session: Session, table: str, clinic_id: int | None = None) -> int:
    """Raw-table count.

    This is SQL rather than an ORM select, so the tenant criterion in
    ``app.models.base`` does not reach it. When the table is tenant-owned the
    clinic must be named -- taken from the session binding by default -- because a
    count of every clinic's rows reported as one clinic's is worse than no count.
    """
    owner = clinic_id if clinic_id is not None else bound_clinic_id(session)
    if owner is None:
        return int(session.execute(text(f'SELECT COUNT(*) FROM "{table}"')).scalar_one())
    return int(
        session.execute(
            text(f'SELECT COUNT(*) FROM "{table}" WHERE clinic_id = :clinic_id'),
            {"clinic_id": owner},
        ).scalar_one()
    )


def get_db() -> Iterator[Session]:
    """FastAPI dependency.

    Deliberately *unbound*: the tenant is not known until the dependency graph has
    resolved it from the request, and resolving it requires reading the ``clinics``
    table without a tenant filter. Routes bind the session through
    :func:`bind_tenant` as part of that resolution, so every handler downstream of
    ``CurrentClinic`` is already scoped.

    Read-mostly endpoints never mutate, but a shared session per request keeps
    behaviour uniform as workflows land later.
    """
    session = new_session()
    try:
        yield session
    finally:
        session.close()
