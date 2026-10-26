"""Request-scoped state every tool reads through ``InjectedState``.

The model never sees this object: ``ToolNode`` injects it from graph state, so a
tool signature can ask for the database session without exposing it as a
parameter the provider could fill in. That is what keeps identity trustworthy in
a plain ``@tool`` function.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field

from langchain_core.language_models import BaseChatModel
from sqlalchemy.orm import Session

from app.models import Conversation, Patient, PatientSession
from app.services import patient_sessions
from app.services.scheduling import Requester
from app.tenancy import ClinicContext


@dataclass(frozen=True)
class TurnDeps:
    """Everything one turn owns. Built by ``run_turn``, never by the model."""

    session: Session
    conversation: Conversation
    patient_session: PatientSession
    llm: BaseChatModel
    # The tenant this turn belongs to. Tools read clinic facts (hours, carriers,
    # slot length, policies) from here rather than from an argument the provider
    # could fill in, which would let one clinic answer with another's prices.
    clinic: ClinicContext
    family_option_identity: str | None = None
    family_option_request_id: str | None = None
    # Seed for the idempotency keys the server derives, so a retry inside one
    # turn cannot hold or book twice while a later turn is still free to make a
    # genuinely new request. The model never sees or sets it.
    turn_key: str = field(default_factory=lambda: secrets.token_hex(8))

    @property
    def conversation_id(self) -> int:
        return self.conversation.id

    @property
    def verified_patient(self) -> Patient | None:
        return patient_sessions.verified_patient(self.session, self.patient_session)

    def requester(self) -> Requester | None:
        patient = self.verified_patient
        if patient is None:
            return None
        return Requester(
            patient=patient,
            patient_session=self.patient_session,
            conversation=self.conversation,
            clinic=self.clinic,
        )


__all__ = ["TurnDeps"]
