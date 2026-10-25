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
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Conversation, Patient, PatientSession, utcnow
from app.services import generations
from app.services.security import hash_token

# A demo session outlives a long browser tab; reset is the real invalidation.
SESSION_TTL = timedelta(days=7)

PATIENT_TOKEN_HEADER = "X-Patient-Session"

VERIFICATION_BY_LOOKUP = "verified_lookup"
VERIFICATION_BY_REGISTRATION = "registered"


def issue_token() -> str:
    return secrets.token_urlsafe(32)


@dataclass(frozen=True)
class IssuedSession:
    token: str
    session_id: int
    reset_generation: str
    expires_at: datetime


def create_patient_session(session: Session) -> IssuedSession:
    """Create a fresh patient session under the current reset generation."""
    token = issue_token()
    generation = generations.ensure_generation(session)
    now = utcnow()
    record = PatientSession(
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
    """Resolve a raw token. Returns None for unknown, expired, or stale sessions."""
    if not token or len(token) < 20 or len(token) > 128:
        return None
    record = session.scalar(
        select(PatientSession).where(PatientSession.token_hash == hash_token(token))
    )
    if record is None:
        return None
    if record.expires_at <= utcnow():
        return None
    current_generation = generations.get_generation(session)
    if current_generation and record.reset_generation != current_generation:
        return None
    return record


def touch(record: PatientSession) -> None:
    record.last_seen_at = utcnow()


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
    session.flush()


def clear_verification(record: PatientSession) -> None:
    record.verified_patient_id = None
    record.verified_at = None
    record.verification_method = None


def verified_patient(session: Session, record: PatientSession) -> Patient | None:
    if record.verified_patient_id is None:
        return None
    return session.get(Patient, record.verified_patient_id)


def list_for_patient_session(session: Session, record: PatientSession) -> list[int]:
    rows = session.scalars(
        select(Conversation.id).where(Conversation.patient_session_id == record.id)
    ).all()
    return list(rows)
