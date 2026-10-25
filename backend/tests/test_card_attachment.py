"""Which turn a card belongs to.

The regression: the patient answered "goku son 25-05-1999 7898789865", the
receptionist asked about insurance, then the patient typed "UHC". The turn that
ran the search returned the slot grid, but it was attached to the *insurance
question*, an earlier assistant message with no cards. The thread then showed
the open times above the answer they belonged to, and again on the real one.

Pins the rule: a card attaches to a reply this turn produced. An assistant
message the patient already saw is history and never gains a card later.
"""

from __future__ import annotations

import pytest
from sqlalchemy.orm import sessionmaker

from app.agent import runner
from app.db import create_db_engine
from app.models import Base, PatientSession
from app.services import conversations, patient_sessions

CARD = [{"type": "slot", "slot_id": "slot_1_202610270800"}]


@pytest.fixture()
def thread(tmp_path):
    engine = create_db_engine(f"sqlite+pysqlite:///{tmp_path / 'clinic.db'}")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
    issued = patient_sessions.create_patient_session(session)
    patient_session = session.get(PatientSession, issued.session_id)
    assert patient_session is not None
    conversation = conversations.create_conversation(
        session, patient_session=patient_session, generation=issued.reset_generation
    )
    conversations.append_assistant_message(session, conversation, content="Thanks for reaching us.")
    conversations.append_patient_message(session, conversation, "goku son 25-05-1999 7898789865")
    conversations.append_assistant_message(
        session, conversation, content="Do you have dental insurance, or will this be self-pay?"
    )
    session.commit()
    yield session, conversation
    session.close()


def _texts(session, conversation) -> list[tuple[int, str, bool]]:
    return [
        (message.id, message.content, bool(message.cards))
        for message in conversations.history(session, conversation)
        if message.role == "assistant" and not message.tool_calls
    ]


def test_cards_attach_to_the_reply_this_turn_produced(thread) -> None:
    session, conversation = thread
    floor = conversations.last_message_id(session, conversation)
    conversations.append_patient_message(session, conversation, "uhc")
    reply = conversations.append_assistant_message(
        session, conversation, content="Here are the open cleaning appointments."
    )
    session.commit()

    runner._attach_cards(session, conversation, CARD, turn_floor=floor)
    session.commit()

    rows = _texts(session, conversation)
    insurance = next(row for row in rows if row[1].startswith("Do you have"))
    assert insurance[2] is False, "an earlier question must never gain this turn's grid"
    assert next(row for row in rows if row[0] == reply.id)[2] is True


def test_cards_never_reach_back_past_the_patient_message(thread) -> None:
    """No reply this turn (a model that said nothing): a card-only turn is added."""
    session, conversation = thread
    floor = conversations.last_message_id(session, conversation)
    conversations.append_patient_message(session, conversation, "uhc")
    session.commit()

    runner._attach_cards(session, conversation, CARD, turn_floor=floor)
    session.commit()

    rows = _texts(session, conversation)
    history = conversations.history(session, conversation)
    created = [message for message in history if message.id > floor]
    assert len(created) == 2, "the patient message plus one card-only reply"
    assert history[-1].cards, "the grid goes on its own new turn"
    assert all(row[2] is False for row in rows if row[0] <= floor), (
        "no reply from before this turn may carry the grid"
    )
