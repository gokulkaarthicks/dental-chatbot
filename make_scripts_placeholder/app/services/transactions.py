"""Guaranteed write-lock transactions for SQLite.

Every booking mutation runs inside an exclusive write transaction opened with
``BEGIN IMMEDIATE``. That takes SQLite's RESERVED lock *before* any read, so a
second writer waits (up to ``busy_timeout``) instead of both requests reading
the same free slot and one failing late with a confusing error.

Why not rely on ``session.begin()``?
------------------------------------
SQLAlchemy opens a *deferred* transaction, upgrading to a write lock only when
a write executes. Two requests could therefore both read "slot is free" before
either wrote. ``BEGIN IMMEDIATE`` removes that window.

Commit semantics
----------------
``sqlite3`` can fail a COMMIT late, and the failure may surface as an exception
with the transaction already ended. Callers append a small *commit trailer* row
after committing (see ``app.services.audit.trailer_committed``) so "this really
committed" is observable rather than assumed.

Two rules keep that trailer and concurrent callers from stalling
------------------------------------------------------------------
1. **A shared implicit read transaction is not a write block.** A caller that
   loaded a proposal first has a deferred transaction open; reusing it would
   skip ``BEGIN IMMEDIATE`` *and* never commit the new rows. Only a genuine
   nest inside ``write_transaction`` may reuse one (tracked by ``_WRITE_DEPTH``);
   anything else is rolled back and a real write lock taken.
2. **A caller's session is left with no transaction open.** After commit, an
   implicit read transaction still holds a SHARED lock, so the *next* writer on
   another connection (the trailer, a concurrent booking) waits the whole
   ``busy_timeout`` and then fails with "database is locked".
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from typing import TypeVar

from sqlalchemy.orm import Session

from app.db import new_session

logger = logging.getLogger(__name__)

T = TypeVar("T")


class WriteTransactionError(RuntimeError):
    """The write transaction could not be started or committed."""


class SlotConflictError(RuntimeError):
    """A requested interval stopped being free before it could be claimed."""

    def __init__(
        self,
        message: str,
        *,
        stable_slot_ids: list[str] | None = None,
        reason: str = "slot_taken",
    ) -> None:
        super().__init__(message)
        self.stable_slot_ids = stable_slot_ids or []
        self.reason = reason


# Tracks nesting per session so a write block can tell "I am already inside an
# outer write_transaction" apart from "the caller happens to have a read open".
_WRITE_DEPTH: dict[int, int] = {}


def _write_depth(session: Session) -> int:
    return _WRITE_DEPTH.get(id(session), 0)


def _set_write_depth(session: Session, depth: int) -> None:
    if depth:
        _WRITE_DEPTH[id(session)] = depth
    else:
        _WRITE_DEPTH.pop(id(session), None)


@contextmanager
def write_transaction(session: Session | None = None) -> Iterator[Session]:
    """Yield a session inside an exclusive ``BEGIN IMMEDIATE`` transaction.

    Pass ``session`` to reuse the request's session (the API does this). With no
    session, a fresh one is opened and closed here.
    """
    owned = session is None
    active = session or new_session()
    depth = _write_depth(active)
    try:
        if depth > 0:
            # Genuinely nested inside an outer write_transaction: that block
            # owns the write lock and the commit, so sharing it keeps the whole
            # operation atomic.
            yield active
            return
        if active.in_transaction():
            # An implicit read transaction (e.g. from loading the proposal this
            # call acts on) is NOT a write block and never commits. Sqlite
            # cannot BEGIN inside it, so the caller would silently lose the
            # work. Discard it and take a real write lock instead.
            active.rollback()

        try:
            raw = active.connection().connection.driver_connection
            raw.execute("BEGIN IMMEDIATE")
        except Exception as exc:  # noqa: BLE001 - surface as a domain error
            logger.warning("BEGIN IMMEDIATE failed", exc_info=True)
            raise WriteTransactionError("Could not obtain the database write lock.") from exc

        _set_write_depth(active, 1)
        try:
            yield active
            active.commit()
        except Exception:
            active.rollback()
            raise
    finally:
        _set_write_depth(active, depth)
        if not owned and depth == 0:
            # A read-then-write session keeps an *implicit* read transaction
            # open after commit. Its SHARED lock then makes the next writer on
            # another connection (the commit trailer, another booking) wait the
            # full busy_timeout, so always end the caller's transaction here.
            # Sessions we own are simply closed below.
            active.rollback()
        if owned:
            active.close()


@contextmanager
def fresh_write_transaction() -> Iterator[Session]:
    """A new session in its own write transaction, independent of any caller.

    Use this for bookkeeping (commit trailers) that must persist even when the
    caller's transaction is being torn down.
    """
    session = new_session()
    try:
        raw = session.connection().connection.driver_connection
        raw.execute("BEGIN IMMEDIATE")
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
    finally:
        session.close()


__all__ = [
    "SlotConflictError",
    "WriteTransactionError",
    "fresh_write_transaction",
    "write_transaction",
]
