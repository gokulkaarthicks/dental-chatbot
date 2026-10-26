"""Booking proposals: what a patient is *about to* book, and what was booked.

A proposal is the only way the assistant can move towards a booking. It is
created from real availability slots, persisted, held (optionally) by
``slot_claims``, and only an explicit confirmation turns it into appointments.

Status lifecycle
----------------
``pending``    -> shown as "Awaiting confirmation"
``accepted``   -> appointments exist and slot claims were transferred
``rejected``   -> patient declined; any held intervals were released
``expired``    -> superseded, timed out, or the demo data was reset

Kinds
-----
``booking``      new appointment(s), possibly for several family members
``reschedule``   replacement time(s) for an existing appointment (kept booked)
``cancellation`` an existing appointment to be released
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import JSON, CheckConstraint, DateTime, ForeignKey, Index, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, ClinicScoped, ProposalStatus, TimestampMixin, UTCDateTime

if TYPE_CHECKING:
    from app.models.conversations import Conversation


class BookingProposal(Base, ClinicScoped, TimestampMixin):
    """A multi-appointment suggestion awaiting an explicit confirmation."""

    __tablename__ = "booking_proposals"

    id: Mapped[int] = mapped_column(primary_key=True)
    public_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    conversation_id: Mapped[int] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=ProposalStatus.PENDING.value
    )
    kind: Mapped[str] = mapped_column(
        String(16), nullable=False, default="booking", server_default=text("'booking'")
    )
    # What the patient asked for when this proposal was created. ``kind`` says
    # what confirming *does* (book / move / release); ``intent`` says what was
    # requested, which is the same thing except cancellation, and is what the
    # "one move at a time" guard reads. Recorded once at creation and never
    # changed by a later click.
    intent: Mapped[str | None] = mapped_column(String(16))
    # What confirming this card books a *party*: ``solo`` (one visit) or
    # ``household`` (one visit per person in the card). Recorded at creation so a
    # card left over from a request that is already booked can be told apart from
    # a genuine additional visit. NULL on older rows.
    visit_shape: Mapped[str | None] = mapped_column(String(16))
    # Binds alternatives and their lifecycle to the one structured request in
    # the conversation.  Historical proposals retain the old request id.
    request_id: Mapped[str | None] = mapped_column(String(128))
    summary: Mapped[str | None] = mapped_column(Text)
    reset_generation: Mapped[str] = mapped_column(String(36), nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    confirmed_by: Mapped[int | None] = mapped_column(
        ForeignKey("staff_users.id", ondelete="SET NULL")
    )

    # Created by the patient clicking a slot card, so the slot cards should be
    # treated as answered once it exists.
    from_ui: Mapped[bool] = mapped_column(default=False, server_default=text("0"))

    # Intervals a reschedule or cancellation proposal frees. JSON list of
    # ``availability_slots.id``. Empty for ordinary bookings, whose holds live in
    # ``slot_claims``.
    held_slot_ids: Mapped[list[int] | None] = mapped_column(JSON)

    # Appointment ids a confirmed proposal released inside its transaction
    # (recorded by the confirmation, read on replay so a repeated confirmation
    # can prove the original really moved rather than describe it second-hand).
    released_appointment_ids: Mapped[list[int] | None] = mapped_column(JSON)

    # Idempotency keys for creation and confirmation. A retried click or
    # confirm resolves to the same proposal instead of creating a second one.
    create_key: Mapped[str | None] = mapped_column(String(128))
    confirm_key: Mapped[str | None] = mapped_column(String(128))

    resolved_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    confirmed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    # What exactly this proposal committed, in the confirmation's own words:
    # ``{"action": "created" | "moved", "appointment_ids": [...]}``. Written in
    # the confirming transaction and re-read on a replay, so a repeated
    # confirmation reports the first result instead of re-deriving one.
    confirm_outcome: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    # Set when confirmation failed because a slot was taken, so the UI can say
    # "that time was just booked" rather than "your options changed".
    conflict_reason: Mapped[str | None] = mapped_column(String(64))

    # Reschedule proposals: the single appointment being moved. Confirmation
    # retires this row (``status = rescheduled``, ``rescheduled_to_id`` set) and
    # creates a linked replacement, all in one transaction, so the original keeps
    # its time, provider and duration as history.
    target_appointment_id: Mapped[int | None] = mapped_column(
        ForeignKey("appointments.id", ondelete="SET NULL")
    )
    # Cancellation proposals carry the patient's stated reason for staff.
    cancellation_reason: Mapped[str | None] = mapped_column(Text)

    # Family bookings: the household this proposal's appointments belong to, so
    # a confirmed family run keeps its members associated with each other and
    # the authorisation check can be re-verified at confirmation time.
    family_group_id: Mapped[int | None] = mapped_column(
        ForeignKey("family_groups.id", ondelete="SET NULL")
    )

    # Bookings created from this proposal (ids are internal, never exposed).
    booking_group_id: Mapped[int | None] = mapped_column(
        ForeignKey("booking_groups.id", ondelete="SET NULL")
    )

    conversation: Mapped[Conversation] = relationship(back_populates="proposals")
    items: Mapped[list[ProposalItem]] = relationship(
        back_populates="proposal", cascade="all, delete-orphan", order_by="ProposalItem.id"
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'accepted', 'rejected', 'expired', 'superseded')",
            name="ck_proposal_status",
        ),
        CheckConstraint(
            "kind IN ('booking', 'reschedule', 'cancellation')", name="ck_proposal_kind"
        ),
        Index("ix_proposals_status", "status"),
        Index("ix_proposals_conversation", "conversation_id"),
        Index("ix_proposals_generation", "reset_generation"),
        Index("ix_proposals_confirm_key", "confirm_key"),
        Index("ix_proposals_clinic_status", "clinic_id", "status"),
        Index("ix_proposals_clinic_conversation", "clinic_id", "conversation_id"),
    )


class ProposalItem(Base, ClinicScoped):
    """One patient/timetable row inside a proposal.

    Availability is *snapshotted* at creation time (``proposed_starts_at``,
    ``provider_name``, ``stable_slot_id``), so the UI renders exactly what the
    patient saw, while confirmation re-reads the live slot to revalidate.
    """

    __tablename__ = "proposal_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    proposal_id: Mapped[int] = mapped_column(
        ForeignKey("booking_proposals.id", ondelete="CASCADE"), nullable=False
    )
    patient_id: Mapped[int | None] = mapped_column(ForeignKey("patients.id", ondelete="SET NULL"))
    provider_id: Mapped[int | None] = mapped_column(
        ForeignKey("providers.id", ondelete="SET NULL")
    )
    appointment_type_id: Mapped[int | None] = mapped_column(
        ForeignKey("appointment_types.id", ondelete="SET NULL")
    )
    availability_slot_id: Mapped[int | None] = mapped_column(
        ForeignKey("availability_slots.id", ondelete="SET NULL")
    )
    proposed_starts_at: Mapped[datetime | None] = mapped_column(DateTime())
    duration_minutes: Mapped[int] = mapped_column(nullable=False, default=30)
    order_index: Mapped[int] = mapped_column(nullable=False, default=0)
    note: Mapped[str | None] = mapped_column(Text)

    # Snapshot fields: describe what the grid showed, independent of later
    # changes to the slot row, and let a card render without extra joins.
    stable_slot_id: Mapped[str | None] = mapped_column(String(64))
    provider_name: Mapped[str | None] = mapped_column(String(120))
    provider_credentials: Mapped[str | None] = mapped_column(String(40))
    appointment_type_code: Mapped[str | None] = mapped_column(String(48))
    appointment_type_name: Mapped[str | None] = mapped_column(String(120))
    patient_first_name: Mapped[str | None] = mapped_column(String(80))

    # Reschedule / cancellation targets.
    target_appointment_id: Mapped[int | None] = mapped_column(
        ForeignKey("appointments.id", ondelete="SET NULL")
    )
    # Snapshot of the appointment's current time, so the card can show
    # "Tue 3:00 PM → Thu 10:00 AM" regardless of what happens to the row.
    original_starts_at: Mapped[datetime | None] = mapped_column(DateTime())
    # Set by a confirmed booking so the card can show its confirmation.
    result_appointment_id: Mapped[int | None] = mapped_column(
        ForeignKey("appointments.id", ondelete="SET NULL")
    )

    # Per-item validation detail captured when an option failed.
    issue: Mapped[str | None] = mapped_column(Text)

    proposal: Mapped[BookingProposal] = relationship(back_populates="items")

    __table_args__ = (
        Index("ix_proposal_items_proposal", "proposal_id"),
        Index("ix_proposal_items_clinic_proposal", "clinic_id", "proposal_id"),
    )


class BookingGroup(Base, ClinicScoped, TimestampMixin):
    """Everything confirmed together, so a family booking can be reasoned about.

    ``confirmation_key`` is UNIQUE: replaying the same confirmation (double
    click, retried request) cannot create a second group of appointments, and
    the original result is returned instead.
    """

    __tablename__ = "booking_groups"

    id: Mapped[int] = mapped_column(primary_key=True)
    public_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    conversation_id: Mapped[int | None] = mapped_column(
        ForeignKey("conversations.id", ondelete="SET NULL")
    )
    proposal_id: Mapped[int | None] = mapped_column(
        ForeignKey("booking_proposals.id", ondelete="SET NULL")
    )
    patient_session_id: Mapped[int | None] = mapped_column(
        ForeignKey("patient_sessions.id", ondelete="SET NULL")
    )
    confirmation_key: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    kind: Mapped[str] = mapped_column(String(16), nullable=False, default="booking")
    family_group_id: Mapped[int | None] = mapped_column(
        ForeignKey("family_groups.id", ondelete="SET NULL")
    )
    summary: Mapped[str | None] = mapped_column(Text)
    details: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    reset_generation: Mapped[str] = mapped_column(String(36), nullable=False)

    __table_args__ = (
        Index("ix_booking_groups_conversation", "conversation_id"),
        Index("ix_booking_groups_patient_session", "patient_session_id"),
        Index("ix_booking_groups_clinic_conversation", "clinic_id", "conversation_id"),
    )
