"""Pydantic response models for the public API.

These are explicit allow-lists: fields not declared here are never serialised,
which is how patient details stay out of public responses.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded"]
    service: str = "dental-receptionist-api"
    version: str
    timestamp: str
    database: Literal["ok", "unavailable"]
    schema_version: int | None = None
    reset_generation: str | None = None
    seeded: bool = False
    clinic_now: str | None = Field(
        default=None,
        description="Clinic-local time with UTC offset, e.g. 2026-10-24T15:04:00-07:00",
    )


class BusinessHours(BaseModel):
    open: str | None
    close: str | None


class ClinicAddress(BaseModel):
    line1: str
    line2: str
    city: str
    state: str
    postal_code: str


class ClinicPolicyItem(BaseModel):
    topic: str
    question: str
    answer: str


class AcceptedPlan(BaseModel):
    name: str
    note: str


class ClinicInsuranceInfo(BaseModel):
    accepted_plans: list[AcceptedPlan]
    self_pay_label: str
    note: str


class ClinicResponse(BaseModel):
    name: str
    legal_note: str
    address: ClinicAddress
    phone: str
    email: str
    timezone: str
    timezone_note: str = ""
    slot_minutes: int
    business_hours: dict[str, BusinessHours]
    closed_days: list[str]
    parking_note: str
    fictional: bool
    policies: list[ClinicPolicyItem] = Field(default_factory=list)
    insurance: ClinicInsuranceInfo | None = None
    self_pay_label: str = "Self-pay"


class DemoCounts(BaseModel):
    providers: int = 0
    patients: int = 0
    family_groups: int = 0
    appointments: int = 0
    upcoming_appointments: int = 0
    today_appointments: int = 0
    availability_slots: int = 0
    available_slots: int = 0
    occupied_slots: int = 0
    staff_users: int = 0
    conversations: int = 0
    messages: int = 0
    pending_proposals: int = 0
    pending_requests: int = 0
    audit_records: int = 0


class FixtureInfo(BaseModel):
    fixture_set: str
    fixture_version: int
    counts: dict[str, int]


class DemoStatusResponse(BaseModel):
    generated_at: str
    reset_generation: str = Field(
        description="Opaque identifier; changing it means the client must discard cached state."
    )
    reset_count: int
    last_reset_at: str | None = None
    last_seeded_at: str | None = None
    seeded: bool
    database_empty: bool
    clinic_today: str
    clinic_today_label: str = Field(description="Weekday label for clinic_today, e.g. 'Sat'")
    clinic_now: str
    clinic_now_offset: str = Field(description="Clinic-local now with UTC offset")
    horizon_days: int
    slot_minutes: int
    counts: DemoCounts
    fixtures: FixtureInfo
    next_available_slot: str | None = None


# --------------------------------------------------------------------------- #
# Patient chat
#
# Every field here is an explicit allow-list. Message metadata, tool arguments,
# tool payloads, patient record details, and session token hashes are never part
# of any model below.
# --------------------------------------------------------------------------- #


class ChatMessageResponse(BaseModel):
    id: int | None = None
    role: Literal["patient", "assistant"]
    content: str
    sequence: int | None = None
    created_at: str | None = None
    cards: list[dict] = Field(
        default_factory=list,
        description=(
            "Interactive cards rendered with this assistant turn (slots, proposals, "
            "confirmations). Each card references rows that exist in the database."
        ),
    )


class ChatConversationSummary(BaseModel):
    conversation_id: str
    title: str
    preview: str = ""
    started_at: str
    last_message_at: str | None = None
    status: str
    message_count: int


class ChatSessionState(BaseModel):
    """Non-identifying session state, so the UI can tailor the composer."""

    verified: bool = False
    registration_complete: bool = False
    assistant_configured: bool = True


class ChatStartSessionRequest(BaseModel):
    """Optional resume hint sent when a browser has a thread but no token.

    It is never sufficient on its own: the conversation is only handed over
    when the new session is verified as the patient who owned it (see
    ``conversations.resume_for_session``). Anything else gets a new thread.
    """

    resume_conversation_id: str | None = Field(
        default=None,
        max_length=64,
        description="Conversation the client was last in; honored only for the same patient.",
    )


class ChatSessionResponse(BaseModel):
    session_token: str = Field(
        description=(
            "Opaque credential. Send it back in X-Patient-Session; store it, never display it."
        )
    )
    session: ChatSessionState
    reset_generation: str
    conversation_id: str = Field(
        description="The conversation opened with this session; every session starts with one."
    )
    greeting: str
    suggestions: list[str]
    clinic_name: str


class ChatConversationSummaryResponse(BaseModel):
    """Summary fields (minus the id), used when creating or restoring a thread."""

    title: str
    preview: str = ""
    started_at: str
    last_message_at: str | None = None
    status: str
    message_count: int


class ChatConversationResponse(BaseModel):
    conversation_id: str
    conversation: ChatConversationSummaryResponse
    messages: list[ChatMessageResponse] = Field(
        description="Full stored transcript, oldest first; each turn carries its own cards."
    )
    suggestions: list[str]
    session: ChatSessionState
    pending_proposals: list[dict] = Field(
        default_factory=list,
        description=(
            "Proposals still pending in the database, as actionable cards. A card "
            "in the transcript that is absent here is history, not a button."
        ),
    )


class ChatConversationListResponse(BaseModel):
    conversations: list[ChatConversationSummary]
    session: ChatSessionState


class ChatFamilyOptionAction(BaseModel):
    """Opaque request metadata for a family-option card click."""

    type: Literal["family_option_click"]
    option_identity: str = Field(min_length=1, max_length=512)
    request_id: str | None = Field(default=None, max_length=128)


class ChatSendMessageRequest(BaseModel):
    message: str = Field(min_length=1, max_length=2000)
    action: ChatFamilyOptionAction | None = None


class ChatFeedbackRequest(BaseModel):
    """A rating of one assistant turn.

    ``reason`` is optional and only ever shown to staff; it is the useful half
    of a thumbs-down, but requiring it would stop most patients from leaving
    one at all.
    """

    message_id: int = Field(gt=0)
    rating: Literal["up", "down"]
    reason: str | None = Field(default=None, max_length=500)


class ChatFeedbackResponse(BaseModel):
    ok: bool
    rating: Literal["up", "down"]
    message: str


class ChatSendMessageResponse(BaseModel):
    conversation_id: str
    reply: str
    messages: list[ChatMessageResponse]
    session: ChatSessionState
    cards: list[dict] = Field(
        default_factory=list,
        description="Cards produced by this turn, identical to the last message's cards.",
    )
    degraded: bool = Field(
        default=False,
        description="True when the assistant could not complete the turn (provider or timeout).",
    )
