"""Staff authentication and server-side sessions.

Demonstration-only login
------------------------
The seeded ``root`` / ``root`` account exists so a reviewer can open the
dashboard without provisioning anything. It is a *demo credential*, hashed at
seed time like any other, and documented openly. Nothing here is intended as a
production identity system (no MFA, no password policy, no email delivery).

Threat model that is still taken seriously
------------------------------------------
* **The password** is only ever stored as a PBKDF2 digest (see
  :mod:`app.services.security`); the plaintext never reaches the database.
* **The session token** lives in an HttpOnly, SameSite=Lax cookie, so page
  JavaScript cannot read it and a cross-site form post cannot send it with
  credentials attached. Only its SHA-256 digest is stored, so a database leak
  cannot be replayed.
* **CSRF**: because cookies *are* sent automatically by the browser, a matching
  ``X-Staff-CSRF`` header is required on every state-changing request. The token
  is issued alongside the session, delivered in a *readable* cookie, and stored
  server-side only as a digest. A cross-origin attacker can cause the cookie to
  be sent but cannot read the token to forge the header.
* **Expiry and revocation**: sessions expire absolutely, are validated against
  the current reset generation (``make reset-demo`` invalidates everything), and
  can be revoked individually on logout.
* **Brute force**: repeated failures lock an account for a short window.

What is deliberately *not* logged
---------------------------------
No password, hash, token, or patient detail is ever written to logs. Failure
messages returned to the browser are identical for unknown user, wrong password,
and disabled account, so the endpoint cannot be used to enumerate staff.
"""

from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import StaffSession, StaffUser, utcnow
from app.services import generations
from app.services.security import hash_token, verify_password

logger = logging.getLogger(__name__)

# A reception shift, not a permanent login.
SESSION_TTL = timedelta(hours=8)

STAFF_SESSION_COOKIE = "mb_staff_session"
STAFF_CSRF_COOKIE = "mb_staff_csrf"
STAFF_CSRF_HEADER = "X-Staff-CSRF"

# Deliberately generic: "invalid username or password" for every failure mode.
INVALID_CREDENTIALS = "Invalid username or password."
LOCKED_OUT = "Too many failed attempts. Please wait a few minutes and try again."

MAX_FAILED_LOGINS = 5
LOCKOUT = timedelta(minutes=5)


@dataclass(frozen=True)
class IssuedStaffSession:
    """What the route needs to set cookies. Never persisted or logged."""

    token: str
    csrf_token: str
    session_id: int
    staff_user_id: int
    expires_at: datetime


def _issue_token() -> str:
    return secrets.token_urlsafe(32)


def _aware(value: datetime | None) -> datetime | None:
    """Treat a naive stored timestamp as UTC.

    Rows written before the session columns used ``UTCDateTime`` round-trip as
    naive values; comparing those to ``utcnow()`` would raise, so normalise here
    rather than trusting the column type of whatever database is open.
    """
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def authenticate(session: Session, *, username: str, password: str) -> tuple[StaffUser | None, str | None]:
    """Verify credentials. Returns ``(user, None)`` or ``(None, error_message)``.

    The caller owns the transaction and must commit on success (the failure
    counter also needs persisting, so the caller commits either way).
    """
    needle = (username or "").strip().lower()
    user = session.scalar(select(StaffUser).where(StaffUser.username == needle))
    now = utcnow()

    if user is None:
        # Hash anyway so a missing user and a wrong password take similar time.
        verify_password(password or "", _DUMMY_HASH)
        return None, INVALID_CREDENTIALS

    if user.locked_until is not None and _aware(user.locked_until) > now:
        return None, LOCKED_OUT

    if not user.active:
        return None, INVALID_CREDENTIALS

    if not verify_password(password or "", user.password_hash):
        user.failed_login_count = (user.failed_login_count or 0) + 1
        if user.failed_login_count >= MAX_FAILED_LOGINS:
            user.locked_until = now + LOCKOUT
            user.failed_login_count = 0
            logger.warning("Staff account %s locked after repeated failures", user.id)
        return None, INVALID_CREDENTIALS

    user.failed_login_count = 0
    user.locked_until = None
    return user, None


def create_session(session: Session, *, user: StaffUser) -> IssuedStaffSession:
    """Open a server-side session for ``user`` and return the raw tokens once.

    The session inherits its tenant from the account rather than from the request:
    a staff cookie is proof of *which clinic's dashboard* was signed in to, so the
    pin cannot be negotiated by whoever presents it.
    """
    token = _issue_token()
    csrf_token = _issue_token()
    now = utcnow()
    expires_at = now + SESSION_TTL
    record = StaffSession(
        clinic_id=user.clinic_id,
        staff_user_id=user.id,
        token_hash=hash_token(token),
        csrf_token_hash=hash_token(csrf_token),
        reset_generation=generations.ensure_generation(session, clinic_id=user.clinic_id),
        created_at=now,
        last_seen_at=now,
        expires_at=expires_at,
    )
    session.add(record)
    session.flush()
    return IssuedStaffSession(
        token=token,
        csrf_token=csrf_token,
        session_id=record.id,
        staff_user_id=user.id,
        expires_at=expires_at,
    )


def resolve(session: Session, token: str | None) -> StaffSession | None:
    """The live session for a raw cookie token, or ``None``.

    Rejects unknown, expired, revoked, and pre-reset tokens identically.

    Crosses tenants on purpose, mirroring
    :func:`app.services.patient_sessions.find_by_token`: the caller needs to see
    that a valid cookie belongs to *another clinic* and answer 403 rather than the
    401 a tenant-filtered lookup would produce, which would put the dashboard into
    a re-login loop against the wrong clinic.
    """
    if not token or len(token) < 20 or len(token) > 128:
        return None
    record = session.scalar(
        select(StaffSession)
        .where(StaffSession.token_hash == hash_token(token))
        .execution_options(tenant_scoped=False)
    )
    if record is None or record.revoked_at is not None:
        return None
    expires_at = _aware(record.expires_at)
    if expires_at is None or expires_at <= utcnow():
        return None
    current = generations.get_generation(session, clinic_id=record.clinic_id)
    if record.reset_generation != (current or record.reset_generation):
        return None
    return record


def csrf_matches(record: StaffSession, presented: str | None) -> bool:
    """Constant-time comparison of the CSRF header against the stored digest."""
    if not record.csrf_token_hash or not presented:
        return False
    return secrets.compare_digest(record.csrf_token_hash, hash_token(presented))


def touch(session: Session, record: StaffSession) -> None:
    """Record activity. Bounded to once a minute to avoid a write per request."""
    now = utcnow()
    last_seen = _aware(record.last_seen_at)
    if last_seen is None or (now - last_seen) > timedelta(minutes=1):
        record.last_seen_at = now


def revoke(session: Session, record: StaffSession) -> None:
    record.revoked_at = utcnow()


def revoke_all_for_user(session: Session, staff_user_id: int) -> int:
    """Sign a staff member out everywhere (used after a password change)."""
    rows = list(
        session.scalars(
            select(StaffSession).where(
                StaffSession.staff_user_id == staff_user_id,
                StaffSession.revoked_at.is_(None),
            )
        )
    )
    now = utcnow()
    for row in rows:
        row.revoked_at = now
    return len(rows)


def purge_expired(session: Session) -> int:
    """Delete dead session rows. Cheap housekeeping, safe to call on login."""
    now = utcnow()
    rows = list(
        session.scalars(
            select(StaffSession).where(
                (StaffSession.expires_at <= now) | (StaffSession.revoked_at.is_not(None))
            )
        )
    )
    # Re-check in Python: a database upgraded from naive timestamps may store
    # values the SQL comparison mis-orders, and deleting a live session would
    # sign someone out unexpectedly.
    dead = [
        row
        for row in rows
        if row.revoked_at is not None
        or (_aware(row.expires_at) is not None and _aware(row.expires_at) <= now)
    ]
    for row in dead:
        session.delete(row)
    return len(dead)


def public_user(user: StaffUser) -> dict:
    """Safe projection of a staff account for the dashboard."""
    return {
        "id": user.id,
        "username": user.username,
        "display_name": user.display_name,
        "email": user.email,
        "role": user.role,
    }


# A fixed digest used only to equalise timing when the username is unknown.
_DUMMY_HASH = (
    "pbkdf2_sha256$260000$AAAAAAAAAAAAAAAAAAAAAA==$"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
)

__all__ = [
    "INVALID_CREDENTIALS",
    "IssuedStaffSession",
    "LOCKED_OUT",
    "SESSION_TTL",
    "STAFF_CSRF_COOKIE",
    "STAFF_CSRF_HEADER",
    "STAFF_SESSION_COOKIE",
    "authenticate",
    "create_session",
    "csrf_matches",
    "public_user",
    "purge_expired",
    "resolve",
    "revoke",
    "revoke_all_for_user",
    "touch",
]
