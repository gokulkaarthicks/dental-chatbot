"""Identity gates and outcome logging for tools.

Every tool decides for itself whether it needs a verified caller, and each tool
reads the caller from injected graph state rather than from model-supplied
arguments. These tests pin both facts: a gated tool refuses before it touches the
domain layer, the public surface keeps working unverified, and an executed tool
logs its outcome without logging arguments.
"""

from __future__ import annotations

import logging

import pytest
from langchain_core.messages import AIMessage

from app.agent import runner
from app.agent.tools import ALL_TOOLS
from app.db import require_clinic_id
from app.models import Conversation, PatientSession, utcnow
from app.services import patient_sessions, scheduling
from tests.support import ScriptedChatModel, clinic_of, deps_for, invoke_tool

NOT_VERIFIED = "not_verified"

# The tools that must work before a chart is bound to the session. An emergency
# report is on the list deliberately: it outranks registration.
PUBLIC_ARGS = {
    "get_session_context": {},
    "get_practice_info": {},
    "get_practice_summary": {},
    "verify_existing_patient": {"phone": "+16505550199", "date_of_birth": "1990-01-01"},
    "find_registration_duplicates": {"full_name": "Nobody Atall", "phone": "+16505550199"},
    "register_patient": {
        "full_name": "Nobody Atall",
        "phone": "+16505550199",
        "date_of_birth": "1990-01-01",
        "insurance_provider": "Self-pay",
    },
    "report_emergency": {"description": "A tooth broke and it is bleeding."},
}


def _unverified_deps(session):  # noqa: ANN001, ANN202
    """A real patient session that has not been bound to a chart."""
    record = patient_sessions.create_patient_session(session)
    patient_session = session.get(PatientSession, record.session_id)
    conversation = Conversation(
        clinic_id=require_clinic_id(session),
        public_id="conv_unverified_test",
        patient_session_id=patient_session.id,
        status="active",
        patient_id=None,
        reset_generation=record.reset_generation,
        started_at=utcnow(),
        last_message_at=utcnow(),
    )
    session.add(conversation)
    session.commit()
    assert patient_sessions.verified_patient(session, patient_session) is None
    return deps_for(session, _Requester(patient_session, conversation, clinic_of(session)))


class _Requester:
    """The minimum ``deps_for`` needs when there is no patient yet.

    The clinic is the session's own tenant rather than a chart fact: an
    unverified chat still belongs to one clinic, and that is what decides which
    practice details a public tool may answer with.
    """

    def __init__(self, patient_session, conversation, clinic) -> None:  # noqa: ANN001
        self.patient_session = patient_session
        self.conversation = conversation
        self.clinic = clinic


def test_unverified_session_is_refused_before_the_domain_layer_runs(
    scenario, monkeypatch
) -> None:
    session, _ = scenario
    deps = _unverified_deps(session)
    calls: list[str] = []

    def spy(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        calls.append("ran")
        return None

    monkeypatch.setattr(scheduling, "pending_target", spy)

    result = invoke_tool(deps, "search_availability", {"appointment_type": "cleaning"})

    assert result["ok"] is False
    assert result["error"] == NOT_VERIFIED
    assert calls == [], "the domain layer must not run for an unverified session"


def test_a_gated_tool_refuses_a_session_without_a_patient(scenario) -> None:
    session, _ = scenario
    result = invoke_tool(_unverified_deps(session), "get_patient_appointments", {})

    assert result["ok"] is False
    assert result["error"] == NOT_VERIFIED


@pytest.mark.parametrize("name", sorted(PUBLIC_ARGS))
def test_the_public_surface_works_without_a_verified_patient(scenario, name) -> None:
    session, _ = scenario
    result = invoke_tool(_unverified_deps(session), name, PUBLIC_ARGS[name])

    assert result.get("error") != NOT_VERIFIED, result


def test_every_tool_is_registered_exactly_once() -> None:
    names = [tool.name for tool in ALL_TOOLS]

    assert len(names) == len(set(names))
    assert set(PUBLIC_ARGS) <= set(names)


def test_tool_execution_is_logged_without_its_arguments(scenario, caplog) -> None:
    session, requester = scenario
    llm = ScriptedChatModel(
        [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "register_child_for_verified_parent",
                        "args": {"full_name": "Kid Whitfield", "date_of_birth": "2017-05-14"},
                        "id": "call_1",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(content="Done."),
        ]
    )

    with caplog.at_level(logging.INFO, logger="app.agent.graph"):
        runner.run_turn(
            session,
            requester.clinic,
            conversation=requester.conversation,
            patient_session=requester.patient_session,
            patient_text="register my son",
            llm=llm,
        )

    events = [
        record for record in caplog.records if getattr(record, "event", None) == "tool.executed"
    ]
    assert len(events) == 1
    fields = events[0].fields
    assert fields["tool"] == "register_child_for_verified_parent"
    assert "ok" in fields and "cards" in fields
    # Outcome only: no patient details ever reach the log.
    assert "full_name" not in fields
    assert "date_of_birth" not in fields
