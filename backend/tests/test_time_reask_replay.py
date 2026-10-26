"""The turn that answers "any other slots?" carries the times itself.

Pins the runner half of the fix: when the model replies in prose without
searching again, the grid the server already built is replayed onto this turn's
reply, stated as refreshed. The patient never has to scroll back, and never sees
"those times are no longer on screen" under a grid that is right there.
"""

from __future__ import annotations

from datetime import date

import pytest
from langchain_core.messages import AIMessage
from sqlalchemy.orm import sessionmaker

from app.agent import runner
from app.db import bind_tenant, create_db_engine
from app.models import Base, Patient, PatientSession
from app.services import conversations, patient_sessions
from app.services.scheduling import Requester
from tests.support import ScriptedChatModel, seed_clinic

GRID = [
    {
        "type": "slot",
        "intent": "reschedule",
        "target_appointment_id": 41,
        "slot_id": "slot_1_202610260830",
        "starts_at": "2026-10-26T08:30",
        "ends_at": "2026-10-26T09:00",
        "appointment_type": "Emergency Visit",
        "provider": "Kwame Osei",
        "settled": False,
        "require_open": True,
    }
]


def silent_model(reply: str) -> ScriptedChatModel:
    """A model that answers a request for times with prose and no tool call."""
    return ScriptedChatModel([AIMessage(content=reply)])


@pytest.fixture()
def thread(tmp_path):
    engine = create_db_engine(f"sqlite+pysqlite:///{tmp_path / 'reask.db'}")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
    clinic = seed_clinic(session, slug="reask")
    bind_tenant(session, clinic.clinic_id)
    patient = Patient(
        clinic_id=clinic.clinic_id,
        full_name="Dharshs",
        phone="+19999998787",
        date_of_birth=date(1999, 7, 25),
        insurance_provider="Self-pay",
    )
    session.add(patient)
    session.flush()
    issued = patient_sessions.create_patient_session(session)
    patient_session = session.get(PatientSession, issued.session_id)
    patient_sessions.mark_verified(
        session,
        patient_session,
        patient,
        method=patient_sessions.VERIFICATION_BY_LOOKUP,
    )
    conversation = conversations.create_conversation(
        session, patient_session=patient_session, generation=issued.reset_generation
    )
    conversations.append_assistant_message(
        session, conversation, content="Here are times I can move it to for Monday, "
        "October 26. Which works for you?", cards=GRID
    )
    conversations.append_patient_message(session, conversation, "mornign")
    session.commit()
    yield session, conversation, patient_session, clinic
    session.close()
    engine.dispose()


def _last_reply(session, conversation):
    for message in reversed(conversations.history(session, conversation)):
        if message.role == "assistant" and not message.tool_calls:
            return message
    raise AssertionError("no assistant reply")


def test_a_re_ask_replays_the_grid_on_its_own_turn(thread) -> None:
    session, conversation, patient_session, clinic = thread
    result = runner.run_turn(
        session,
        clinic,
        conversation=conversation,
        patient_session=patient_session,
        patient_text="do you have any other slots on monday",
        llm=silent_model("Those times are no longer on screen. Ask me again and I'll check."),
    )
    assert result.degraded is False
    reply = _last_reply(session, conversation)
    slots = [card for card in (reply.cards or []) if card.get("type") == "slot"]
    assert slots, "the turn carries the times it was asked for"
    assert slots[0]["slot_id"] in {card["slot_id"] for card in GRID}
    assert reply.content.startswith("Just refreshed."), reply.content
    assert "ask me again" not in reply.content.lower()
    # The grid is still a move: a click continues the reschedule, never a booking.
    assert slots[0]["intent"] == "reschedule"
    assert slots[0]["target_appointment_id"] == 41


def test_the_replayed_grid_is_attached_only_once(thread) -> None:
    session, conversation, patient_session, clinic = thread
    runner.run_turn(
        session,
        clinic,
        conversation=conversation,
        patient_session=patient_session,
        patient_text="current availability",
        llm=silent_model("Sure, one moment."),
    )
    replies = [
        message
        for message in conversations.history(session, conversation)
        if message.role == "assistant" and message.cards
    ]
    # The original grid keeps its own turn; the new answer gets a second copy.
    assert len(replies) == 2
    assert replies[-1].cards[0]["slot_id"] == GRID[0]["slot_id"]
