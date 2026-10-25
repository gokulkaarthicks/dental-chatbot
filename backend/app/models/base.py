"""Shared model machinery: declarative base, enums, and column mixins."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy import DateTime
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import TypeDecorator


class UTCDateTime(TypeDecorator[datetime]):
    """Timezone-aware UTC datetime stored as ISO text with an explicit offset.

    A custom type (rather than relying on SQLite's native datetime handling) is
    used so values always round-trip with their offset intact. Aware values are
    converted to UTC; naive values are assumed to already be UTC.
    """

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect) -> datetime | None:
        if value is None or value.tzinfo is None:
            return value
        return value.astimezone(UTC)

    def process_result_value(self, value: datetime | None, dialect) -> datetime | None:
        if value is None:
            return None
        return value if value.tzinfo else value.replace(tzinfo=UTC)


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utcnow, onupdate=utcnow
    )


class SlotStatus(StrEnum):
    AVAILABLE = "available"
    OCCUPIED = "occupied"
    BLOCKED = "blocked"


class AppointmentStatus(StrEnum):
    BOOKED = "booked"
    CANCELLED = "cancelled"
    COMPLETED = "completed"
    # Moved to a replacement appointment (see ``Appointment.rescheduled_to_id``).
    # Inactive like ``cancelled``: the row keeps its slot/time history but no
    # longer occupies a slot or counts as an upcoming visit.
    RESCHEDULED = "rescheduled"


class ProposalStatus(StrEnum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    EXPIRED = "expired"
    # Replaced by a newer proposal for the same patient (the patient changed
    # their mind). Kept distinct from ``rejected`` so the UI can say "updated"
    # rather than "declined", and its held intervals were released.
    SUPERSEDED = "superseded"


class ConversationStatus(StrEnum):
    ACTIVE = "active"
    CLOSED = "closed"


class MessageRole(StrEnum):
    PATIENT = "patient"
    ASSISTANT = "assistant"
    STAFF = "staff"
    SYSTEM = "system"


class StaffRole(StrEnum):
    RECEPTIONIST = "receptionist"
    CLINIC_MANAGER = "clinic_manager"


class AlertStatus(StrEnum):
    OPEN = "open"
    ACKNOWLEDGED = "acknowledged"
    RESOLVED = "resolved"


class AlertKind(StrEnum):
    """Alert categories. Values are stored, so keep them stable."""

    EMERGENCY = "emergency"
    URGENT_CALLBACK = "urgent_callback"
    INSURANCE_VERIFICATION = "insurance_verification"
    ASSISTANCE_REQUEST = "assistance_request"


class RequestType(StrEnum):
    CALLBACK = "callback"
    RESCHEDULE = "reschedule"
    CLINICAL_QUESTION = "clinical_question"
    BILLING = "billing"


class RequestStatus(StrEnum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    RESOLVED = "resolved"
