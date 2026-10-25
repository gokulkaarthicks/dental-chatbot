"""Family booking groups.

Families are a first-class concept because the receptionist must be able to
match a caller by shared phone number and offer back-to-back slots for several
members. A group is an optional wrapper: every patient can exist without one.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin

if TYPE_CHECKING:
    from app.models.patients import Patient


class FamilyGroup(Base, TimestampMixin):
    __tablename__ = "family_groups"

    id: Mapped[int] = mapped_column(primary_key=True)
    seed_key: Mapped[str | None] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    shared_phone: Mapped[str | None] = mapped_column(String(32))
    notes: Mapped[str | None] = mapped_column(Text)

    membership_rows: Mapped[list[FamilyMember]] = relationship(
        back_populates="family_group", cascade="all, delete-orphan"
    )

    __table_args__ = (Index("ix_family_groups_shared_phone", "shared_phone"),)


class FamilyMember(Base):
    __tablename__ = "family_members"

    id: Mapped[int] = mapped_column(primary_key=True)
    family_group_id: Mapped[int] = mapped_column(
        ForeignKey("family_groups.id", ondelete="CASCADE"), nullable=False
    )
    patient_id: Mapped[int] = mapped_column(
        ForeignKey("patients.id", ondelete="CASCADE"), nullable=False
    )
    # Column is named ``relationship`` in SQL, but the class attribute cannot use
    # that name because it would shadow sqlalchemy.orm.relationship above.
    relation_to_primary: Mapped[str] = mapped_column("relationship", String(40), nullable=False)
    is_primary_contact: Mapped[bool] = mapped_column(default=False, nullable=False)

    family_group: Mapped[FamilyGroup] = relationship(back_populates="membership_rows")
    patient: Mapped[Patient] = relationship(
        back_populates="family_memberships", foreign_keys=[patient_id]
    )

    __table_args__ = (
        UniqueConstraint("family_group_id", "patient_id", name="uq_family_member"),
        Index("ix_family_members_patient", "patient_id"),
    )
