"""After an empty availability search, hallucinated tool calls must not 400."""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy.orm import sessionmaker

from app.agent import runner
from app.ai.deepseek import AssistantTurn, ToolCall
from app.db import create_db_engine
from app.models import Base, Patient, PatientSession
from app.services import conversations, patient_sessions


class ScriptedClient:
    """Returns a canned sequence of turns; records which tools were offered."""

    def __init__(self, turns: list[AssistantTurn]) -> None:
        self._turns = list(turns)
        self.tools_offered: list[bool] = []

    def chat(self, *, messages, tools=None, **kwargs) -> AssistantTurn:  # noqa: ANN001
        del messages, kwargs
        self.tools_offered.append(bool(tools))
        if not self._turns:
            return AssistantTurn(content="done", tool_calls=[], finish_reason="stop")
        return self._turns.pop(0)


@pytest.fixture()
def thread(tmp_path):
    engine = create_db_engine(f"sqlite+pysqlite:///{tmp_path / 'loop.db'}")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
    patient = Patient(
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
    yield session, conversation, patient_session
    session.close()


def test_tool_calls_ignored_after_availability_exhausted(thread, monkeypatch) -> None:
    session, conversation, patient_session = thread

    empty_search = {
        "ok": True,
        "availability_checked": True,
        "cards": [],
        "message": (
            "I checked the requested date range but couldn't find a complete set of open "
            "times for everyone."
        ),
    }

    monkeypatch.setattr(
        runner,
        "execute_tool",
        lambda ctx, name, arguments, **kwargs: empty_search,
    )

    client = ScriptedClient(
        [
            AssistantTurn(
                content="",
                tool_calls=[
                    ToolCall(
                        id="call_1",
                        name="search_family_availability",
                        arguments='{"date_phrases":["any day"],"members":[]}',
                    )
                ],
                finish_reason="tool_calls",
            ),
            # Tools are withdrawn after the empty search. A hallucinated follow-up
            # must become a prose reply, not another tool round that 400s.
            AssistantTurn(
                content="",
                tool_calls=[
                    ToolCall(
                        id="dsml_1",
                        name="search_family_availability",
                        arguments='{"date_phrases":["any day"],"members":[]}',
                    )
                ],
                finish_reason="stop",
            ),
        ]
    )

    result = runner.run_turn(
        session,
        conversation=conversation,
        patient_session=patient_session,
        patient_text="I am fine with any day to schedule",
        client=client,
    )

    assert result.degraded is False
    assert "Something went wrong" not in result.reply
    assert result.reply
    assert client.tools_offered == [True, False]
    assert client._turns == []
