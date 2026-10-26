"""People and catalogue tables: patients, providers, appointment types."""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING

from sqlalchemy import Date, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, ClinicScoped, TimestampMixin

if TYPE_CHECKING:
    from app.models.family import FamilyMember
    from app.models.scheduling import Appointment, AvailabilitySlot


class Patient(Base, ClinicScoped, TimestampMixin):
    """A patient record.

    Phone numbers are deliberately **not unique**: family members routinely share
    one household number, and matching is resolved by name/DOB conversationally.

    ``phone`` and ``date_of_birth`` are only unique *within* a clinic. Two
    practices may each have a patient on the same household number, and
    verification must never be able to resolve the other clinic's chart.
    """

    __tablename__ = "patients"

    id: Mapped[int] = mapped_column(primary_key=True)
    seed_key: Mapped[str | None] = mapped_column(String(64))
    full_name: Mapped[str] = mapped_column(String(120), nullable=False)
    phone: Mapped[str] = mapped_column(String(32), nullable=False)
    date_of_birth: Mapped[date] = mapped_column(Date, nullable=False)
    insurance_provider: Mapped[str | None] = mapped_column(String(120))
    insurance_member_id: Mapped[str | None] = mapped_column(String(64))
    notes: Mapped[str | None] = mapped_column(Text)

    # ``passive_deletes="all"`` is what makes the ORM agree with the schema.
    # The FK is ``ondelete="RESTRICT"`` and the column is ``nullable=False``:
    # the database's position is that a patient with visits cannot be deleted.
    # Without this flag SQLAlchemy defaults to ``save-update, merge``, so a
    # parent delete tries to null the child FK first and dies on the NOT NULL
    # constraint -- a crash instead of the refusal the schema intends. "all"
    # (not ``True``) also stops it loading the collection just to null it.
    appointments: Mapped[list[Appointment]] = relationship(
        back_populates="patient", passive_deletes="all"
    )
    family_memberships: Mapped[list[FamilyMember]] = relationship(
        back_populates="patient",
        foreign_keys="FamilyMember.patient_id",
        cascade="all, delete-orphan",
    )

    __table_args__ = (
        UniqueConstraint("clinic_id", "seed_key", name="uq_patients_clinic_seed_key"),
        Index("ix_patients_phone", "phone"),
        Index("ix_patients_clinic_phone", "clinic_id", "phone"),
        Index("ix_patients_full_name", "full_name"),
    )


class Provider(Base, ClinicScoped, TimestampMixin):
    """A dentist working at exactly one clinic."""

    __tablename__ = "providers"

    id: Mapped[int] = mapped_column(primary_key=True)
    seed_key: Mapped[str | None] = mapped_column(String(64))
    full_name: Mapped[str] = mapped_column(String(120), nullable=False)
    credentials: Mapped[str] = mapped_column(String(40), nullable=False)
    email: Mapped[str] = mapped_column(String(160), nullable=False)
    bio: Mapped[str | None] = mapped_column(Text)
    active: Mapped[bool] = mapped_column(default=True, nullable=False)

    specialties: Mapped[list[ProviderSpecialty]] = relationship(
        back_populates="provider",
        cascade="all, delete-orphan",
        order_by="ProviderSpecialty.id",
    )
    availability_slots: Mapped[list[AvailabilitySlot]] = relationship(
        back_populates="provider", cascade="all, delete-orphan"
    )
    appointments: Mapped[list[Appointment]] = relationship(
        back_populates="provider", foreign_keys="Appointment.provider_id"
    )

    @property
    def specialty_names(self) -> list[str]:
        return [specialty.specialty for specialty in self.specialties]

    __table_args__ = (
        UniqueConstraint("clinic_id", "seed_key", name="uq_providers_clinic_seed_key"),
        UniqueConstraint("clinic_id", "email", name="uq_providers_clinic_email"),
        Index("ix_providers_clinic_active", "clinic_id", "active"),
    )


class ProviderSpecialty(Base, ClinicScoped):
    """One row per specialty so a provider can hold several.

    ``clinic_id`` is denormalised from the provider. It costs one column on a
    small table and saves a join on every provider filter the availability engine
    runs, which is the hottest read in the product.
    """

    __tablename__ = "provider_specialties"

    id: Mapped[int] = mapped_column(primary_key=True)
    provider_id: Mapped[int] = mapped_column(
        ForeignKey("providers.id", ondelete="CASCADE"), nullable=False
    )
    specialty: Mapped[str] = mapped_column(String(80), nullable=False)

    provider: Mapped[Provider] = relationship(back_populates="specialties")

    __table_args__ = (
        UniqueConstraint("provider_id", "specialty", name="uq_provider_specialty"),
        UniqueConstraint("clinic_id", "provider_id", "specialty", name="uq_provider_spec_clinic"),
        Index("ix_provider_specialties_specialty", "specialty"),
        Index("ix_provider_specialties_clinic_specialty", "clinic_id", "specialty"),
    )


class AppointmentType(Base, ClinicScoped):
    """Bookable visit kinds.

    ``code`` is a stable slug used by the assistant, unique *per clinic*: two
    practices can both define ``cleaning`` with different durations, and the
    duration is what makes a slot run, so it must never be read from the other
    clinic's row.
    """

    __tablename__ = "appointment_types"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(48), nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    duration_minutes: Mapped[int] = mapped_column(nullable=False)
    is_emergency: Mapped[bool] = mapped_column(default=False, nullable=False)
    required_specialty: Mapped[str | None] = mapped_column(String(80))
    sort_order: Mapped[int] = mapped_column(default=0, nullable=False)

    __table_args__ = (
        UniqueConstraint("clinic_id", "code", name="uq_appointment_types_clinic_code"),
        Index("ix_appointment_types_clinic_sort", "clinic_id", "sort_order"),
    )
