from datetime import datetime, timedelta

from app.models import AppointmentType
from app.services.availability_engine import (
    SlotOption,
    _family_plan_is_non_overlapping,
    _nearby_family_plan_score,
)
from app.services.scheduling import family_option_cards


def _option(patient_id: int, hour: int, minute: int = 0) -> SlotOption:
    starts_at = datetime(2026, 10, 31, hour, minute)
    ends_at = starts_at + timedelta(minutes=30)
    return SlotOption(
        slot_ids=[patient_id],
        stable_id=f"slot_1_{starts_at:%Y%m%d%H%M}",
        provider_id=1,
        provider_name="Dr. Naomi Bell",
        provider_credentials="DDS",
        provider_specialties=[],
        appointment_type_code="checkup",
        appointment_type_name="General Checkup",
        starts_at=starts_at,
        ends_at=ends_at,
        duration_minutes=30,
        patient_id=patient_id,
        patient_first_name=f"Patient {patient_id}",
    )


def test_nearby_family_plans_prefer_the_smallest_total_gap() -> None:
    close = [_option(1, 8), _option(2, 8, 30)]
    farther = [_option(1, 8), _option(2, 9)]

    assert _nearby_family_plan_score(close) < _nearby_family_plan_score(farther)
    assert _family_plan_is_non_overlapping(close)
    assert _family_plan_is_non_overlapping(farther)


def test_family_card_marks_gapped_plan_as_same_day() -> None:
    appointment_type = AppointmentType(
        id=1,
        code="checkup",
        name="General Checkup",
        duration_minutes=30,
    )

    cards = family_option_cards(
        plans=[[_option(1, 9), _option(2, 8)]],
        appointment_types={1: appointment_type, 2: appointment_type},
    )

    assert cards[0]["back_to_back"] is False
    assert cards[0]["starts_at"] == "2026-10-31T08:00"
    assert cards[0]["ends_at"] == "2026-10-31T09:30"
