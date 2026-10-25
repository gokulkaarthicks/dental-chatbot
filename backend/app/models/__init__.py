"""Model registry.

Importing this package registers every table on ``Base.metadata``. Alembic-less
schema creation (``Base.metadata.create_all``) relies on that side effect, so
any new model module must be imported here.
"""

from app.models.base import (
    AlertKind,
    AlertStatus,
    AppointmentStatus,
    Base,
    ConversationStatus,
    MessageRole,
    ProposalStatus,
    RequestStatus,
    RequestType,
    SlotStatus,
    StaffRole,
    UTCDateTime,
    utcnow,
)
from app.models.conversations import Conversation, ConversationFeedback, Message
from app.models.family import FamilyGroup, FamilyMember
from app.models.operations import (
    AppMetadata,
    AuditRecord,
    IdempotencyRecord,
    SeedManifest,
)
from app.models.patients import AppointmentType, Patient, Provider, ProviderSpecialty
from app.models.proposals import BookingGroup, BookingProposal, ProposalItem
from app.models.scheduling import Appointment, AvailabilitySlot, SlotClaim
from app.models.sessions import PatientSession
from app.models.staff import PendingRequest, StaffAlert, StaffSession, StaffUser

__all__ = [
    "AlertKind",
    "AlertStatus",
    "AppMetadata",
    "Appointment",
    "AppointmentStatus",
    "AppointmentType",
    "AuditRecord",
    "AvailabilitySlot",
    "Base",
    "BookingGroup",
    "BookingProposal",
    "Conversation",
    "ConversationFeedback",
    "ConversationStatus",
    "FamilyGroup",
    "FamilyMember",
    "IdempotencyRecord",
    "Message",
    "MessageRole",
    "Patient",
    "PatientSession",
    "PendingRequest",
    "ProposalItem",
    "ProposalStatus",
    "Provider",
    "ProviderSpecialty",
    "RequestStatus",
    "RequestType",
    "SeedManifest",
    "SlotClaim",
    "SlotStatus",
    "StaffAlert",
    "StaffRole",
    "StaffSession",
    "StaffUser",
    "UTCDateTime",
    "utcnow",
]
