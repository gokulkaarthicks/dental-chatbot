"""Reset-generation tracking.

A *reset generation* is an opaque UUID stored in ``app_metadata``. It changes
only when the demo database is recreated (``make reset-demo``). Clients compare
the generation from ``/api/health`` or ``/api/demo/status`` with what they last
saw; any mismatch means their conversation ID, staff session, and open proposals
are gone, so local browser state must be discarded.

The generation is *not* regenerated on ``make seed`` or container restarts.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy.orm import Session

from app.models import AppMetadata, utcnow

GENERATION_KEY = "reset_generation"
RESET_COUNT_KEY = "reset_count"
RESET_AT_KEY = "last_reset_at"
SEEDED_AT_KEY = "last_seeded_at"
SCHEMA_VERSION_KEY = "schema_version"


def _get(session: Session, key: str) -> str | None:
    record = session.get(AppMetadata, key)
    return record.value if record else None


def _set(
    session: Session, key: str, value: str, now: datetime | None = None, *, public: bool = True
) -> None:
    """Upsert a metadata row.

    ``public`` defaults to True for the demo-state keys Phase 1 exposes through
    ``/api/demo/status``. Patient-identifying counters must pass ``public=False``.
    """
    record = session.get(AppMetadata, key)
    if record is None:
        session.add(AppMetadata(key=key, value=value, updated_at=now or utcnow(), public=public))
    else:
        record.value = value
        record.updated_at = now or utcnow()
        record.public = public


def get_generation(session: Session) -> str | None:
    return _get(session, GENERATION_KEY)


def ensure_generation(session: Session) -> str:
    """Return the current generation, creating one if the database is new."""
    existing = _get(session, GENERATION_KEY)
    if existing:
        return existing
    generation = new_generation()
    _set(session, GENERATION_KEY, generation)
    _set(session, RESET_COUNT_KEY, "1")
    session.flush()
    return generation


def new_generation() -> str:
    return str(uuid.uuid4())


def rotate_generation(session: Session, count: int | None = None) -> str:
    """Issue a fresh generation. Used by the reset service only."""
    generation = new_generation()
    now = utcnow()
    _set(session, GENERATION_KEY, generation, now)
    if count is not None:
        _set(session, RESET_COUNT_KEY, str(count), now)
    _set(session, RESET_AT_KEY, now.isoformat(), now)
    session.flush()
    return generation


def get_reset_count(session: Session) -> int:
    raw = _get(session, RESET_COUNT_KEY)
    try:
        return int(raw) if raw is not None else 0
    except ValueError:
        return 0


def get_last_reset_at(session: Session) -> str | None:
    return _get(session, RESET_AT_KEY)


def mark_seeded(session: Session, when: datetime) -> None:
    _set(session, SEEDED_AT_KEY, when.isoformat(timespec="seconds"), when)


def get_last_seeded_at(session: Session) -> str | None:
    return _get(session, SEEDED_AT_KEY)


def set_schema_version(session: Session, version: int) -> None:
    _set(session, SCHEMA_VERSION_KEY, str(version))


def get_schema_version(session: Session) -> int | None:
    raw = _get(session, SCHEMA_VERSION_KEY)
    try:
        return int(raw) if raw is not None else None
    except ValueError:
        return None


# --- Phase 2: patient registration counter -------------------------------- #
#
# Stored but never exposed: it would reveal how many real registrations have
# happened, which is patient-identifying behaviour rather than fixture data.

PATIENTS_REGISTERED_KEY = "patients_registered_total"


def increment_patient_registration(session: Session) -> None:
    current = _get(session, PATIENTS_REGISTERED_KEY) or "0"
    try:
        count = int(current)
    except ValueError:
        count = 0
    _set(session, PATIENTS_REGISTERED_KEY, str(count + 1), public=False)
