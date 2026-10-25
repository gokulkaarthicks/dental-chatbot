"""Staff users, sessions, alerts, and pending requests."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import CheckConstraint, ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import (
    AlertStatus,
    Base,
    RequestStatus,
    RequestType,
    StaffRole,
    TimestampMixin,
    UTCDateTime,
)

if TYPE_CHECKING:
    from app.models.conversations import Conversation


class StaffUser(Base, TimestampMixin):
    """A clinic employee who can review proposals and pick up requests."""

    __tablename__ = "staff_users"

    id: Mapped[int] = mapped_column(primary_key=True)
    seed_key: Mapped[str | None] = mapped_column(String(64), unique=True)
    email: Mapped[str] = mapped_column(String(160), nullable=False, unique=True)
    # Phase 4: the dashboard login identifier. Email stays for contact purposes.
    username: Mapped[str | None] = mapped_column(String(64), unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String(120), nullable=False)
    role: Mapped[str] = mapped_column(
        String(24), nullable=False, default=StaffRole.RECEPTIONIST.value
    )
    # Only a digest is persisted. Demo passwords are hashed at seed time.
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    active: Mapped[bool] = mapped_column(default=True, nullable=False)

    # Phase 4: brute-force friction for the demo login. Counters are reset on a
    # successful sign-in; the lockout is time based rather than permanent so a
    # demo cannot be bricked by a typo.
    failed_login_count: Mapped[int] = mapped_column(nullable=False, default=0, server_default="0")
    locked_until: Mapped[datetime | None] = mapped_column(UTCDateTime())

    sessions: Mapped[list[StaffSession]] = relationship(
        back_populates="staff_user", cascade="all, delete-orphan"
    )

    __table_args__ = (
        CheckConstraint("role IN ('receptionist', 'clinic_manager')", name="ck_staff_role"),
    )


class StaffSession(Base):
    """Server-side session token. Invalidated in bulk on demo reset."""

    __tablename__ = "staff_sessions"

    id: Mapped[int] = mapped_column(primary_key=True)
    staff_user_id: Mapped[int] = mapped_column(
        ForeignKey("staff_users.id", ondelete="CASCADE"), nullable=False
    )
    token_hash: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    # Phase 4: CSRF defence. The browser gets this value in a readable cookie and
    # echoes it in ``X-Staff-CSRF``; only the hash is stored here. A cross-site
    # request can make the browser send the HttpOnly session cookie, but it
    # cannot read this token to forge the matching header.
    csrf_token_hash: Mapped[str | None] = mapped_column(String(128))
    reset_generation: Mapped[str] = mapped_column(String(36), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    last_seen_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime())

    staff_user: Mapped[StaffUser] = relationship(back_populates="sessions")

    __table_args__ = (
        Index("ix_staff_sessions_user", "staff_user_id"),
        Index("ix_staff_sessions_expires", "expires_at"),
    )


class StaffAlert(Base, TimestampMixin):
    """Escalations raised by the assistant (emergencies, low-confidence matches)."""

    __tablename__ = "staff_alerts"

    id: Mapped[int] = mapped_column(primary_key=True)
    conversation_id: Mapped[int | None] = mapped_column(
        ForeignKey("conversations.id", ondelete="SET NULL")
    )
    patient_id: Mapped[int | None] = mapped_column(ForeignKey("patients.id", ondelete="SET NULL"))
    kind: Mapped[str] = mapped_column(String(40), nullable=False)
    severity: Mapped[str] = mapped_column(String(16), nullable=False, default="normal")
    message: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=AlertStatus.OPEN.value)
    # Phase 4: who picked it up and when, so the dashboard can show a real
    # acknowledgement instead of inferring one from a status change.
    acknowledged_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    acknowledged_by: Mapped[int | None] = mapped_column(
        ForeignKey("staff_users.id", ondelete="SET NULL")
    )
    resolved_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    resolution_note: Mapped[str | None] = mapped_column(Text)

    # Phase 3: a stable key identifying *which* situation this alert is about
    # (e.g. ``emergency:42`` for conversation 42). Re-reporting the same
    # emergency reuses the open alert instead of paging the front desk twice.
    dedupe_key: Mapped[str | None] = mapped_column(String(96))
    # Repeated reports that add detail (symptoms worsening) bump this and
    # refresh ``message``/``updated_at`` so the update is visible to staff.
    report_count: Mapped[int] = mapped_column(nullable=False, default=1, server_default="1")
    last_reported_at: Mapped[datetime | None] = mapped_column(UTCDateTime())

    # Caller-supplied contact details when the alert is filed before identity is
    # confirmed. Copied here, not looked up, so the front desk can call back even
    # though no patient chart is linked yet. Contact information only: this must
    # never be read as verification.
    caller_name: Mapped[str | None] = mapped_column(String(120))
    caller_phone: Mapped[str | None] = mapped_column(String(32))

    conversation: Mapped[Conversation | None] = relationship()

    __table_args__ = (
        CheckConstraint(
            "severity IN ('low', 'normal', 'high', 'urgent')", name="ck_alert_severity"
        ),
        CheckConstraint("status IN ('open', 'acknowledged', 'resolved')", name="ck_alert_status"),
        Index("ix_staff_alerts_status_created", "status", "created_at"),
        Index("ix_staff_alerts_conversation", "conversation_id"),
        Index("ix_staff_alerts_dedupe", "dedupe_key"),
    )


class PendingRequest(Base, TimestampMixin):
    """Callbacks and other items the assistant could not resolve itself."""

    __tablename__ = "pending_requests"

    id: Mapped[int] = mapped_column(primary_key=True)
    conversation_id: Mapped[int | None] = mapped_column(
        ForeignKey("conversations.id", ondelete="SET NULL")
    )
    patient_id: Mapped[int | None] = mapped_column(ForeignKey("patients.id", ondelete="SET NULL"))
    request_type: Mapped[str] = mapped_column(
        String(32), nullable=False, default=RequestType.CALLBACK.value
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=RequestStatus.PENDING.value
    )
    details: Mapped[str] = mapped_column(Text, nullable=False)
    preferred_callback_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    assigned_to: Mapped[int | None] = mapped_column(
        ForeignKey("staff_users.id", ondelete="SET NULL")
    )
    resolved_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    resolution_note: Mapped[str | None] = mapped_column(Text)
    # Phase 4: set when a staff member explicitly clears an approval-gated item
    # (insurance/payment verification). Routine callbacks never populate this.
    approved_by: Mapped[int | None] = mapped_column(
        ForeignKey("staff_users.id", ondelete="SET NULL")
    )
    approved_at: Mapped[datetime | None] = mapped_column(UTCDateTime())

    # Phase 3: same idea as ``StaffAlert.dedupe_key`` -- asking twice for the
    # same callback updates the existing request rather than filing duplicates.
    dedupe_key: Mapped[str | None] = mapped_column(String(96))

    conversation: Mapped[Conversation | None] = relationship()

    __table_args__ = (
        CheckConstraint(
            "request_type IN ('callback', 'reschedule', 'clinical_question', 'billing')",
            name="ck_request_type",
        ),
        CheckConstraint(
            "status IN ('pending', 'in_progress', 'resolved')", name="ck_request_status"
        ),
        Index("ix_pending_requests_status_created", "status", "created_at"),
        Index("ix_pending_requests_dedupe", "dedupe_key"),
    )
