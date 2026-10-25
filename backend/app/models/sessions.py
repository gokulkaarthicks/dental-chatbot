"""Patient-facing chat sessions.

A *patient session* is the credential a browser holds while talking to the
receptionist. It is deliberately separate from staff sessions
(``staff_sessions``) in both storage and semantics:

* The browser holds an **opaque token** (32 random bytes, URL-safe). Only its
  SHA-256 digest is stored, so a database leak cannot be replayed.
* The token, not the conversation ID, is what authorises access. Routes resolve
  the conversation *through* the session; knowing an ID alone grants nothing.
* Every session records the reset generation it was created under. After
  ``make reset-demo`` the whole table is gone, and any surviving token fails to
  resolve (404), which is how browser state is invalidated.

A session may span several conversations ("new conversation" keeps the same
verified patient, but starts a fresh thread), which is the same shape a real
front desk sees: one caller, several calls.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, UTCDateTime

if TYPE_CHECKING:
    from app.models.conversations import Conversation
    from app.models.patients import Patient


class PatientSession(Base, TimestampMixin):
    __tablename__ = "patient_sessions"

    id: Mapped[int] = mapped_column(primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    reset_generation: Mapped[str] = mapped_column(String(36), nullable=False)

    # Set only after a successful verification or registration. This is the
    # single source of truth for "who is the assistant talking to"; the model
    # can never set it, only the registration/verification services can.
    verified_patient_id: Mapped[int | None] = mapped_column(
        ForeignKey("patients.id", ondelete="SET NULL")
    )
    verified_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    verification_method: Mapped[str | None] = mapped_column(String(24))

    # Contact details for an UNVERIFIED caller, captured only when an emergency
    # report has to be filed before identity is confirmed. Kept apart from
    # ``verified_patient_id`` on purpose: a name and number typed into a chat is
    # contact information, not proof of identity, so it must never authorise
    # chart access, bookings, or history.
    contact_name: Mapped[str | None] = mapped_column(String(120))
    contact_phone: Mapped[str | None] = mapped_column(String(32))

    last_seen_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)

    conversations: Mapped[list[Conversation]] = relationship(
        back_populates="patient_session", order_by="Conversation.id"
    )
    verified_patient: Mapped[Patient | None] = relationship()

    __table_args__ = (
        Index("ix_patient_sessions_generation", "reset_generation"),
        Index("ix_patient_sessions_expires", "expires_at"),
        Index("ix_patient_sessions_verified_patient", "verified_patient_id"),
    )

    @property
    def is_verified(self) -> bool:
        return self.verified_patient_id is not None
