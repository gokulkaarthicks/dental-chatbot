"""Per-clinic reset-generation and operational-state tracking.

A *reset generation* is an opaque UUID stored in ``app_metadata`` keyed on
``(clinic_id, key)``. It changes only when that one clinic's demo database is
recreated (``make reset-demo --clinic <slug>``). Clients compare the generation
from ``/api/health`` or ``/api/demo/status`` with what they last saw; any
mismatch means their conversation ID, staff session, and open proposals are gone,
so local browser state must be discarded.

Keying the state per clinic is what makes resetting one practice safe for the
others: the old single-key store meant a reset of the demo clinic invalidated
every other clinic's browser state as well, because they shared one UUID row.

The exception is ``schema_version``, which describes the SQLite *file*, not a
tenant. It is written identically for every clinic and read back unscoped, so a
health check that names no clinic can still answer it.

The generation is *not* regenerated on ``make seed`` or container restarts.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import require_clinic_id
from app.models import AppMetadata, Clinic, utcnow

GENERATION_KEY = "reset_generation"
RESET_COUNT_KEY = "reset_count"
RESET_AT_KEY = "last_reset_at"
SEEDED_AT_KEY = "last_seeded_at"
SCHEMA_VERSION_KEY = "schema_version"

# Keys that a client is allowed to see through /api/demo/status.
PUBLIC_KEYS = frozenset(
    {GENERATION_KEY, RESET_COUNT_KEY, RESET_AT_KEY, SEEDED_AT_KEY, SCHEMA_VERSION_KEY}
)


def _get(session: Session, key: str, clinic_id: int | None = None) -> str | None:
    owner = clinic_id if clinic_id is not None else require_clinic_id(session)
    record = session.get(AppMetadata, (owner, key))
    return record.value if record else None


def _set(
    session: Session,
    key: str,
    value: str,
    now: datetime | None = None,
    *,
    public: bool = True,
    clinic_id: int | None = None,
) -> None:
    """Upsert one tenant's metadata row.

    ``public`` defaults to True for the demo-state keys Phase 1 exposes through
    ``/api/demo/status``. Patient-identifying counters must pass ``public=False``.
    """
    owner = clinic_id if clinic_id is not None else require_clinic_id(session)
    record = session.get(AppMetadata, (owner, key))
    if record is None:
        session.add(
            AppMetadata(
                clinic_id=owner, key=key, value=value, updated_at=now or utcnow(), public=public
            )
        )
    else:
        record.value = value
        record.updated_at = now or utcnow()
        record.public = public


def get_generation(session: Session, *, clinic_id: int | None = None) -> str | None:
    return _get(session, GENERATION_KEY, clinic_id)


def ensure_generation(session: Session, *, clinic_id: int | None = None) -> str:
    """Return this clinic's generation, creating one if the tenant is new."""
    existing = _get(session, GENERATION_KEY, clinic_id)
    if existing:
        return existing
    generation = new_generation()
    _set(session, GENERATION_KEY, generation, clinic_id=clinic_id)
    _set(session, RESET_COUNT_KEY, "1", clinic_id=clinic_id)
    session.flush()
    return generation


def new_generation() -> str:
    return str(uuid.uuid4())


def rotate_generation(
    session: Session, count: int | None = None, *, clinic_id: int | None = None
) -> str:
    """Issue a fresh generation for one clinic. Used by the reset service only."""
    generation = new_generation()
    now = utcnow()
    _set(session, GENERATION_KEY, generation, now, clinic_id=clinic_id)
    if count is not None:
        _set(session, RESET_COUNT_KEY, str(count), now, clinic_id=clinic_id)
    _set(session, RESET_AT_KEY, now.isoformat(), now, clinic_id=clinic_id)
    session.flush()
    return generation


def get_reset_count(session: Session, *, clinic_id: int | None = None) -> int:
    raw = _get(session, RESET_COUNT_KEY, clinic_id)
    try:
        return int(raw) if raw is not None else 0
    except ValueError:
        return 0


def get_last_reset_at(session: Session, *, clinic_id: int | None = None) -> str | None:
    return _get(session, RESET_AT_KEY, clinic_id)


def mark_seeded(session: Session, when: datetime, *, clinic_id: int | None = None) -> None:
    _set(session, SEEDED_AT_KEY, when.isoformat(timespec="seconds"), when, clinic_id=clinic_id)


def get_last_seeded_at(session: Session, *, clinic_id: int | None = None) -> str | None:
    return _get(session, SEEDED_AT_KEY, clinic_id)


# --------------------------------------------------------------------------- #
# schema_version: the one key that is not per-tenant state
# --------------------------------------------------------------------------- #


def set_schema_version(session: Session, version: int) -> None:
    """Record the file's schema version against every tenant.

    The value describes the SQLite file rather than a clinic, so it is written
    identically for all of them. Doing it per row keeps ``app_metadata``'s
    ``(clinic_id, key)`` primary key honest -- a nullable or sentinel clinic id
    would let a genuinely global row slip past the tenant guard that protects
    every other write.
    """
    now = utcnow()
    for clinic_id in session.scalars(select(Clinic.id).execution_options(tenant_scoped=False)):
        _set(session, SCHEMA_VERSION_KEY, str(version), now, clinic_id=int(clinic_id))
    session.flush()


def get_schema_version(session: Session) -> int | None:
    """The stored version, or ``None`` on a database that predates versioning.

    Read unscoped and reduced to the lowest value across tenants: a health check
    names no clinic, and if two tenants somehow disagree the lower number is the
    truthful answer about the shared file.
    """
    raw = session.scalar(
        select(func.min(AppMetadata.value)).where(
            AppMetadata.key == SCHEMA_VERSION_KEY,
            AppMetadata.public.is_(True),
        )
    )
    try:
        return int(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


# --- Phase 2: patient registration counter -------------------------------- #
#
# Stored but never exposed: it would reveal how many real registrations have
# happened, which is patient-identifying behaviour rather than fixture data.

PATIENTS_REGISTERED_KEY = "patients_registered_total"


def increment_patient_registration(session: Session, *, clinic_id: int | None = None) -> None:
    current = _get(session, PATIENTS_REGISTERED_KEY, clinic_id) or "0"
    try:
        count = int(current)
    except ValueError:
        count = 0
    _set(session, PATIENTS_REGISTERED_KEY, str(count + 1), public=False, clinic_id=clinic_id)


__all__ = [
    "GENERATION_KEY",
    "PATIENTS_REGISTERED_KEY",
    "PUBLIC_KEYS",
    "RESET_AT_KEY",
    "RESET_COUNT_KEY",
    "SCHEMA_VERSION_KEY",
    "SEEDED_AT_KEY",
    "ensure_generation",
    "get_generation",
    "get_last_reset_at",
    "get_last_seeded_at",
    "get_reset_count",
    "get_schema_version",
    "increment_patient_registration",
    "mark_seeded",
    "new_generation",
    "rotate_generation",
    "set_schema_version",
]
