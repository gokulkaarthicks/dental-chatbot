"""Patient session issuance and resolution.

Security model
--------------
* The token is 32 random bytes, URL-safe, returned **once** to the browser.
* Only ``sha256(token)`` is stored, so a database read cannot reconstruct it.
* Possession of a conversation ID never authorises anything; routes always
  resolve ``token -> session -> conversation`` and verify ownership.
* Sessions are scoped to the reset generation. After a demo reset the table is
  gone, so every previously issued token resolves to nothing (HTTP 404) and the
  browser is expected to start over.
* Staff sessions live in ``staff_sessions`` with their own table, tokens, and
  expiry rules. The two never share a lookup path or a header.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import bound_clinic_id
from app.models import Patient, PatientSession, utcnow
from app.services import generations
from app.services.security import hash_token

# A demo session outlives a long browser tab; reset is the real invalidation.
SESSION_TTL = timedelta(days=7)

PATIENT_TOKEN_HEADER = "X-Patient-Session"

VERIFICATION_BY_LOOKUP = "verified_lookup"
VERIFICATION_BY_REGISTRATION = "registered"

# Chat-side identity lookups are counted. Phone plus date of birth is a two-factor
# guess, so an unlimited oracle over it is how a clinic's charts get enumerated.
# The lockout is time based rather than permanent, so an honest patient who
# mistyped their birth date is not bricked out of their own account.
MAX_VERIFICATION_ATTEMPTS = 5
VERIFICATION_LOCKOUT = timedelta(minutes=15)

VERIFICATION_LOCKED_MESSAGE = (
    "Too many details didn't match for this chat. For your privacy, checking a chart "
    "is paused for a few minutes. If that was you, try again shortly or ask the front "
    "desk to verify you."
)


def _aware(value: datetime | None) -> datetime | None:
    """Treat a naive stored timestamp as UTC.

    Mirrors :mod:`app.services.staff_auth`: rows written before a column used
    ``UTCDateTime`` round-trip naive, and comparing one to ``utcnow()`` raises.
    """
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def issue_token() -> str:
    return secrets.token_urlsafe(32)


@dataclass(frozen=True)
class IssuedSession:
    token: str
    session_id: int
    reset_generation: str
    expires_at: datetime


def create_patient_session(session: Session, *, clinic_id: int | None = None) -> IssuedSession:
    """Create a fresh patient session under the current reset generation.

    The new credential *is* a tenant pin for the rest of the thread, so it has to
    be stamped now: ``clinic_id`` comes from the session's tenant binding, which
    the request dependency set when it resolved ``X-Clinic``. An unbound session
    raises here instead of writing a row the insert guard would reject with a less
    actionable message.
    """
    owner = clinic_id if clinic_id is not None else bound_clinic_id(session)
    if owner is None:
        raise RuntimeError("create_patient_session needs a clinic; session is not bound")
    token = issue_token()
    generation = generations.ensure_generation(session, clinic_id=owner)
    now = utcnow()
    record = PatientSession(
        clinic_id=owner,
        token_hash=hash_token(token),
        reset_generation=generation,
        expires_at=now + SESSION_TTL,
        last_seen_at=now,
    )
    session.add(record)
    session.flush()
    return IssuedSession(
        token=token,
        session_id=record.id,
        reset_generation=generation,
        expires_at=record.expires_at,
    )


def find_by_token(session: Session, token: str | None) -> PatientSession | None:
    """Resolve a raw token. Returns None for unknown, expired, or stale sessions.

    The lookup crosses tenants deliberately. The stored value is a digest of a
    32-byte random token, so finding it globally leaks nothing, and doing it here
    lets the caller see that the credential belongs to *another clinic* and answer
    403 ``clinic_mismatch`` instead of the misleading 404 an automatically
    tenant-filtered query would produce.
    """
    if not token or len(token) < 20 or len(token) > 128:
        return None
    record = session.scalar(
        select(PatientSession)
        .where(PatientSession.token_hash == hash_token(token))
        .execution_options(tenant_scoped=False)
    )
    if record is None:
        return None
    if record.expires_at <= utcnow():
        return None
    current_generation = generations.get_generation(session, clinic_id=record.clinic_id)
    if current_generation and record.reset_generation != current_generation:
        return None
    return record


def touch(record: PatientSession) -> None:
    record.last_seen_at = utcnow()


def verification_locked(record: PatientSession) -> bool:
    """True while this chat is in the cool-off window after failed lookups."""
    locked_until = _aware(record.verification_locked_until)
    return locked_until is not None and locked_until > utcnow()


def record_failed_verification(session: Session, record: PatientSession) -> bool:
    """Count one failed lookup. Returns True when this attempt started a lockout."""
    if verification_locked(record):
        return True
    record.verification_attempts = (record.verification_attempts or 0) + 1
    if record.verification_attempts >= MAX_VERIFICATION_ATTEMPTS:
        record.verification_locked_until = utcnow() + VERIFICATION_LOCKOUT
        record.verification_attempts = 0
        session.flush()
        return True
    # A window that already ran out must not make the next miss read as a lockout.
    record.verification_locked_until = None
    session.flush()
    return False


def clear_verification_attempts(record: PatientSession) -> None:
    """A successful verification proves the caller, so the counter no longer applies."""
    record.verification_attempts = 0
    record.verification_locked_until = None


def mark_verified(
    session: Session,
    record: PatientSession,
    patient: Patient,
    *,
    method: str,
) -> None:
    """Bind the session to a patient. Only registration/verification call this."""
    record.verified_patient_id = patient.id
    record.verified_at = utcnow()
    record.verification_method = method
    clear_verification_attempts(record)
    session.flush()


def verified_patient(session: Session, record: PatientSession) -> Patient | None:
    if record.verified_patient_id is None:
        return None
    return session.get(Patient, record.verified_patient_id)
