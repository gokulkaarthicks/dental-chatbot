"""After an empty availability search, hallucinated tool calls must not 400.

The graph withdraws every tool once a search came back complete but empty, so a
call the model still emits becomes prose instead of another tool round. The
second round is executed against the real ``ToolNode``, which is what makes this
a test of the policy rather than of a mock.
"""

from __future__ import annotations

from datetime import date

import pytest
from langchain_core.messages import AIMessage
from sqlalchemy.orm import sessionmaker

from app.agent import runner
from app.agent.tools import ALL_TOOLS, CONFIRM_TOOL_NAMES
from app.db import bind_tenant, create_db_engine
from app.models import Base, Patient, PatientSession
from app.services import conversations, patient_sessions
from tests.support import ScriptedChatModel, patch_tool, seed_clinic

EMPTY_SEARCH = {
    "ok": True,
    "availability_checked": True,
    "cards": [],
    "message": (
        "I checked the requested date range but couldn't find a complete set of open "
        "times for everyone."
    ),
}

SEARCH_CALL = {
    "name": "search_family_availability",
    "args": {
        "date_phrases": ["any day"],
        "members": [{"patient_first_name": "Marcus", "appointment_type": "cleaning"}],
    },
    "id": "call_1",
    "type": "tool_call",
}


@pytest.fixture()
def thread(tmp_path):
    engine = create_db_engine(f"sqlite+pysqlite:///{tmp_path / 'loop.db'}")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
    clinic = seed_clinic(session, slug="loop")
    bind_tenant(session, clinic.clinic_id)
    patient = Patient(
        clinic_id=clinic.clinic_id,
        full_name="Marcus Chen",
        phone="+16505550101",
        date_of_birth=date(1984, 3, 12),
        insurance_provider="Sunnycoast",
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
    session.commit()
    yield session, conversation, patient_session, clinic
    session.close()


def test_tool_calls_ignored_after_availability_exhausted(thread, monkeypatch) -> None:
    session, conversation, patient_session, clinic = thread
    patch_tool(monkeypatch, "search_family_availability", EMPTY_SEARCH)

    llm = ScriptedChatModel(
        [
            AIMessage(content="", tool_calls=[SEARCH_CALL]),
            # The tools are withdrawn after the empty search. A follow-up call the
            # model still emits must become a prose reply, not another tool round.
            AIMessage(content="", tool_calls=[{**SEARCH_CALL, "id": "call_2"}]),
        ]
    )

    result = runner.run_turn(
        session,
        clinic,
        conversation=conversation,
        patient_session=patient_session,
        patient_text="I am fine with any day to schedule",
        llm=llm,
    )

    assert result.degraded is False
    assert "Something went wrong" not in result.reply
    assert result.reply
    # The patient asked for times, not for a booking, so the confirmation tools
    # are not on this turn's surface. Everything else is bound for round one.
    first_round = [tool.name for tool in ALL_TOOLS if tool.name not in CONFIRM_TOOL_NAMES]
    assert llm.tools_offered == [first_round, []]
    assert llm.turns == []
