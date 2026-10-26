"""Family card implementation details stay out of patient-visible messages."""

from app.agent import graph
from app.models import Message, utcnow
from app.schemas import ChatSendMessageRequest
from app.services.conversations import public_message

OPTION_IDENTITY = "family1:slot_1_202610310800,3:slot_1_202610310900"


def test_family_click_metadata_is_hidden_from_the_patient_message() -> None:
    request = ChatSendMessageRequest(
        message=(
            "Please hold the clicked family option for Marcus, Ellie, and Noah. "
            "The selected times are Marcus at 8:00 AM, Ellie at 9:00 AM, and Noah at 9:30 AM."
        ),
        action={"type": "family_option_click", "option_identity": OPTION_IDENTITY},
    )
    system_prompt = "You are a dental receptionist."

    prompt = graph._with_family_option_action(
        system_prompt, request.action.option_identity, request.action.request_id
    )

    assert OPTION_IDENTITY in prompt
    assert OPTION_IDENTITY not in request.message


def test_legacy_family_click_message_is_scrubbed_from_public_history() -> None:
    legacy = Message(
        role="patient",
        content=(
            "Please hold this option. The clicked card's option_identity is "
            f"{OPTION_IDENTITY}; pass that exact option_identity to hold_family_option."
        ),
        sequence=1,
        created_at=utcnow(),
    )

    public = public_message(legacy)

    assert public["content"] == "Please hold this option."
    assert OPTION_IDENTITY not in public["content"]
