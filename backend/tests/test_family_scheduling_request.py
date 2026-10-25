"""Regression definitions for the canonical family scheduling request.

These tests intentionally cover the service boundaries only.  The conversational
model is not the source of truth for participants, proposal identity, or date
fallback behavior.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from sqlalchemy import select

from app.agent.scheduling_tools import (
    FamilyMemberArgs,
    StaffRequestArgs,
    _family_member_patients,
    _tool_create_staff_request,
)
from app.agent.tools import ToolContext
from app.models import (
    Appointment,
    AppointmentStatus,
    AppointmentType,
    AvailabilitySlot,
    FamilyGroup,
    FamilyMember,
    Patient,
    ProposalStatus,
    Provider,
    SlotStatus,
)
from app.services import date_parsing, scheduling
from app.services.date_parsing import resolve_preferences
from app.services.proposals import find_by_public_id, release_expired_holds
from app.services.scheduling import FamilyMemberInput


def _family(session, requester):
    ellie = Patient(
        full_name="Ellie Whitfield",
        phone=requester.patient.phone,
        date_of_birth=date(2015, 2, 3),
        insurance_provider="Self-pay",
    )
    noah = Patient(
        full_name="Noah Whitfield",
        phone=requester.patient.phone,
        date_of_birth=date(2017, 6, 4),
        insurance_provider="Self-pay",
    )
    group = FamilyGroup(
        seed_key="request-regression",
        name="Whitfield family",
        shared_phone=requester.patient.phone,
    )
    session.add_all([ellie, noah, group])
    session.flush()
    session.add(
        AppointmentType(
            code="checkup",
            name="Checkup",
            description="Routine exam",
            duration_minutes=30,
            is_emergency=False,
            required_specialty="General Dentistry",
            sort_order=2,
        )
    )
    session.flush()
    session.add_all(
        [
            FamilyMember(
                family_group_id=group.id,
                patient_id=requester.patient.id,
                relation_to_primary="self",
            ),
            FamilyMember(family_group_id=group.id, patient_id=ellie.id, relation_to_primary="child"),
            FamilyMember(family_group_id=group.id, patient_id=noah.id, relation_to_primary="child"),
        ]
    )
    session.commit()
    return ellie, noah


def _add_slots(
    session, *days: date, first_hour: int | float = 8, last_hour: int | float = 12
) -> None:
    provider = session.scalar(select(Provider).order_by(Provider.id))
    assert provider is not None
    for day in days:
        day_start = datetime.combine(day, datetime.min.time())
        cursor = day_start + timedelta(hours=first_hour)
        end = day_start + timedelta(hours=last_hour)
        while cursor < end:
            session.add(
                AvailabilitySlot(
                    provider_id=provider.id,
                    starts_at=cursor,
                    ends_at=cursor + timedelta(minutes=30),
                    status=SlotStatus.AVAILABLE.value,
                )
            )
            cursor += timedelta(minutes=30)
    session.commit()


def _freeze_availability_clock(monkeypatch) -> None:
    frozen = datetime(2026, 10, 25, 7, 0)
    monkeypatch.setattr(date_parsing, "clinic_today", lambda: frozen.date())
    monkeypatch.setattr("app.services.availability_engine.clinic_now", lambda: frozen)


def test_both_children_extend_the_existing_request_without_dropping_parent(scenario) -> None:
    session, requester = scenario
    ellie, noah = _family(session, requester)

    first = scheduling.family_search(
        session,
        requester=requester,
        members=[
            FamilyMemberInput(requester.patient, "cleaning"),
            FamilyMemberInput(ellie, "cleaning"),
        ],
        date_phrases=["2030-05-07"],
    )
    assert first["people"] == ["Dana", "Ellie"]

    updated = scheduling.family_search(
        session,
        requester=requester,
        members=[FamilyMemberInput(ellie, "cleaning"), FamilyMemberInput(noah, "cleaning")],
        date_phrases=["2030-05-07"],
    )
    request = scheduling.scheduling_request(session, requester.conversation.id)
    assert request is not None
    assert [row["patient_id"] for row in request["participants"]] == [
        requester.patient.id,
        ellie.id,
        noah.id,
    ]
    assert updated["slots_per_option"] == 3


def test_both_expands_to_every_authorized_child_and_keeps_the_requester(scenario) -> None:
    session, requester = scenario
    ellie, noah = _family(session, requester)
    context = ToolContext(
        session=session,
        patient_session=requester.patient_session,
        conversation_id=requester.conversation.id,
    )
    members, problem = _family_member_patients(
        context,
        requester,
        [
            FamilyMemberArgs(
                patient_first_name=requester.patient.full_name.split()[0],
                appointment_type="cleaning",
            ),
            FamilyMemberArgs(patient_first_name="btoh", appointment_type="checkup"),
        ],
    )
    assert problem is None
    assert members is not None
    assert [member.patient.id for member in members] == [
        requester.patient.id,
        ellie.id,
        noah.id,
    ]


def test_repeated_family_option_selection_reuses_the_active_hold(scenario) -> None:
    session, requester = scenario
    ellie, _ = _family(session, requester)
    search = scheduling.family_search(
        session,
        requester=requester,
        members=[
            FamilyMemberInput(requester.patient, "cleaning"),
            FamilyMemberInput(ellie, "cleaning"),
        ],
        date_phrases=["2030-05-07"],
    )
    card = search["cards"][0]
    members = [
        FamilyMemberInput(requester.patient, "cleaning"),
        FamilyMemberInput(ellie, "cleaning"),
    ]
    first = scheduling.hold_family_option(
        session,
        requester=requester,
        members=members,
        option_index=0,
        option_identity=card["option_id"],
    )
    second = scheduling.hold_family_option(
        session,
        requester=requester,
        members=members,
        option_index=0,
        option_identity=card["option_id"],
    )
    assert first["proposal_id"] == second["proposal_id"]
    assert second["replayed"] is True


def test_confirm_family_option_books_every_household_member(monkeypatch, scenario) -> None:
    """Parent + child hold must confirm, not mislabel the child as slot_taken."""
    session, requester = scenario
    ellie, _ = _family(session, requester)
    _freeze_availability_clock(monkeypatch)
    _add_slots(session, date(2026, 10, 31))
    members = [
        FamilyMemberInput(requester.patient, "cleaning"),
        FamilyMemberInput(ellie, "checkup"),
    ]
    search = scheduling.family_search(
        session,
        requester=requester,
        members=members,
        date_phrases=["October 31 or whenever possible"],
        back_to_back=True,
    )
    assert search.get("cards"), search
    held = scheduling.hold_family_option(
        session,
        requester=requester,
        members=members,
        option_index=0,
        option_identity=search["cards"][0]["option_id"],
    )
    assert held.get("ok") is True, held
    assert held.get("proposal_id"), held

    confirmed = scheduling.confirm_booking(
        session,
        requester=requester,
        proposal_id=held["proposal_id"],
        confirm_key="family-confirm-regression",
    )
    assert confirmed.get("ok") is True, confirmed
    assert confirmed.get("confirmed") is True
    assert confirmed.get("error") is None

    proposal = find_by_public_id(session, held["proposal_id"])
    assert proposal is not None
    assert proposal.status == ProposalStatus.ACCEPTED.value

    booked = session.scalars(
        select(Appointment).where(
            Appointment.patient_id.in_([requester.patient.id, ellie.id]),
            Appointment.status == AppointmentStatus.BOOKED.value,
        )
    ).all()
    assert {row.patient_id for row in booked} == {requester.patient.id, ellie.id}


def test_different_family_options_leave_one_active_proposal_and_preserve_history(scenario) -> None:
    session, requester = scenario
    ellie, _ = _family(session, requester)
    search = scheduling.family_search(
        session,
        requester=requester,
        members=[
            FamilyMemberInput(requester.patient, "cleaning"),
            FamilyMemberInput(ellie, "cleaning"),
        ],
        date_phrases=["2030-05-07"],
    )
    first = scheduling.hold_family_option(
        session,
        requester=requester,
        members=[
            FamilyMemberInput(requester.patient, "cleaning"),
            FamilyMemberInput(ellie, "cleaning"),
        ],
        option_index=0,
        option_identity=search["cards"][0]["option_id"],
    )
    second = scheduling.hold_family_option(
        session,
        requester=requester,
        members=[
            FamilyMemberInput(requester.patient, "cleaning"),
            FamilyMemberInput(ellie, "cleaning"),
        ],
        option_index=1,
        option_identity=search["cards"][1]["option_id"],
    )
    old = find_by_public_id(session, first["proposal_id"])
    current = find_by_public_id(session, second["proposal_id"])
    assert old is not None and old.status == ProposalStatus.SUPERSEDED.value
    assert current is not None and current.status == ProposalStatus.PENDING.value
    history = scheduling.reconcile_proposal_cards(
        session,
        [
            scheduling.proposal_card(old, include_actions=True),
            scheduling.proposal_card(current, include_actions=True),
        ],
    )
    assert history[0]["title"] == "Replaced by a newer option"
    assert history[0]["actions"] == []
    assert history[1]["status"] == ProposalStatus.PENDING.value
    assert history[1]["actions"]


def test_changing_family_date_retires_old_request_and_hold(scenario) -> None:
    session, requester = scenario
    ellie, _ = _family(session, requester)
    members = [
        FamilyMemberInput(requester.patient, "cleaning"),
        FamilyMemberInput(ellie, "cleaning"),
    ]
    first = scheduling.family_search(
        session,
        requester=requester,
        members=members,
        date_phrases=["2030-05-07"],
    )
    held = scheduling.hold_family_option(
        session,
        requester=requester,
        members=members,
        option_index=0,
        option_identity=first["cards"][0]["option_id"],
    )
    first_request_id = scheduling.scheduling_request(session, requester.conversation.id)["request_id"]

    changed = scheduling.family_search(
        session,
        requester=requester,
        members=members,
        date_phrases=["2030-05-08"],
    )
    second_request_id = scheduling.scheduling_request(session, requester.conversation.id)["request_id"]
    old = find_by_public_id(session, held["proposal_id"])
    assert first_request_id != second_request_id
    assert old is not None and old.status == ProposalStatus.SUPERSEDED.value
    assert changed["cards"][0]["request_id"] == second_request_id


def test_another_day_refreshes_sqlite_with_stored_family_requirements(scenario) -> None:
    session, requester = scenario
    ellie, _ = _family(session, requester)
    scheduling.family_search(
        session,
        requester=requester,
        members=[
            FamilyMemberInput(requester.patient, "cleaning"),
            FamilyMemberInput(ellie, "cleaning"),
        ],
        date_phrases=["2030-05-07"],
    )
    before = scheduling.scheduling_request(session, requester.conversation.id)
    refreshed = scheduling.refresh_scheduling_request(
        session, requester=requester, alternative_day=True
    )
    after = scheduling.scheduling_request(session, requester.conversation.id)
    assert refreshed is not None
    assert before is not None and after is not None
    assert before["request_id"] != after["request_id"]
    assert after["preferences"]["date_phrases"] == []
    assert refreshed["slots_per_option"] == 2


def test_family_availability_failure_retries_before_creating_callback(scenario) -> None:
    session, requester = scenario
    ellie, noah = _family(session, requester)
    scheduling.family_search(
        session,
        requester=requester,
        members=[
            FamilyMemberInput(requester.patient, "cleaning"),
            FamilyMemberInput(ellie, "checkup"),
            FamilyMemberInput(noah, "checkup"),
        ],
        date_phrases=["2030-05-07"],
        time_windows=["morning"],
    )
    context = ToolContext(
        session=session,
        patient_session=requester.patient_session,
        conversation_id=requester.conversation.id,
    )
    result = _tool_create_staff_request(
        context,
        StaffRequestArgs(
            topic="callback",
            details="Family availability could not be retrieved; arrange the household booking.",
        ),
    )
    assert result["cards"]
    assert result["cards"][0]["type"] == "family_option"
    assert len(result["cards"][0]["slots"]) == 3


def test_family_member_type_is_not_silently_defaulted() -> None:
    args = FamilyMemberArgs(patient_first_name="Noah")
    assert args.appointment_type is None


def test_family_search_asks_for_a_missing_member_type(scenario) -> None:
    session, requester = scenario
    ellie, _ = _family(session, requester)
    result = scheduling.family_search(
        session,
        requester=requester,
        members=[
            FamilyMemberInput(requester.patient, "cleaning"),
            FamilyMemberInput(ellie, None),
        ],
        date_phrases=["2030-05-07"],
    )
    assert result["error"] == "missing_appointment_type"
    assert result["needs_clarification"] is True


def test_preferred_date_can_fall_back_to_later_availability() -> None:
    preferences = resolve_preferences(
        date_phrases=["October 31 or whenever possible"],
        today=date(2026, 10, 25),
    )
    assert preferences.days == [date(2026, 10, 31)]
    assert preferences.fallback_allowed is True
    assert preferences.accepts_day(date(2026, 11, 2)) is True


def test_three_person_back_to_back_search_prefers_october_31(monkeypatch, scenario) -> None:
    session, requester = scenario
    ellie, noah = _family(session, requester)
    _freeze_availability_clock(monkeypatch)
    _add_slots(session, date(2026, 10, 31), date(2026, 11, 2))

    result = scheduling.family_search(
        session,
        requester=requester,
        members=[
            FamilyMemberInput(requester.patient, "cleaning"),
            FamilyMemberInput(ellie, "checkup"),
            FamilyMemberInput(noah, "checkup"),
        ],
        date_phrases=["October 31 or whenever possible"],
        back_to_back=True,
    )

    assert result["available"] is True
    assert result["slots_per_option"] == 3
    assert result["people"] == ["Dana", "Ellie", "Noah"]
    assert result["cards"][0]["slots"][0]["day"] == "2026-10-31"
    assert all(len(card["slots"]) == 3 for card in result["cards"])
    assert result["cards"][0]["back_to_back"] is True


def test_three_person_search_uses_alternative_after_october_31_has_no_complete_block(
    monkeypatch, scenario
) -> None:
    session, requester = scenario
    ellie, noah = _family(session, requester)
    _freeze_availability_clock(monkeypatch)
    # October 31 has only 90 minutes, so it can never fit 60 + 30 + 30
    # minutes. November 2 has the complete continuous block.
    _add_slots(session, date(2026, 10, 31), first_hour=8, last_hour=9.5)
    _add_slots(session, date(2026, 11, 2))

    result = scheduling.family_search(
        session,
        requester=requester,
        members=[
            FamilyMemberInput(requester.patient, "cleaning"),
            FamilyMemberInput(ellie, "checkup"),
            FamilyMemberInput(noah, "checkup"),
        ],
        date_phrases=["October 31 or whenever possible"],
        back_to_back=True,
    )

    assert result["available"] is True
    assert result["cards"][0]["slots"][0]["day"] == "2026-11-02"
    assert len(result["cards"][0]["slots"]) == 3


def test_expired_family_hold_does_not_block_a_fresh_complete_search(monkeypatch, scenario) -> None:
    session, requester = scenario
    ellie, noah = _family(session, requester)
    _freeze_availability_clock(monkeypatch)
    _add_slots(session, date(2026, 10, 31))
    members = [
        FamilyMemberInput(requester.patient, "cleaning"),
        FamilyMemberInput(ellie, "checkup"),
        FamilyMemberInput(noah, "checkup"),
    ]
    search = scheduling.family_search(
        session,
        requester=requester,
        members=members,
        date_phrases=["October 31"],
        back_to_back=True,
    )
    held = scheduling.hold_family_option(
        session,
        requester=requester,
        members=members,
        option_index=0,
        option_identity=search["cards"][0]["option_id"],
    )
    proposal = find_by_public_id(session, held["proposal_id"])
    assert proposal is not None
    proposal.expires_at = proposal.created_at - timedelta(seconds=1)
    session.commit()
    release_expired_holds(session, requester.conversation.id)
    session.commit()

    refreshed = scheduling.family_search(
        session,
        requester=requester,
        members=members,
        date_phrases=["October 31"],
        back_to_back=True,
    )
    assert refreshed["available"] is True
    assert len(refreshed["cards"][0]["slots"]) == 3


def test_complete_unavailability_does_not_create_a_callback(scenario) -> None:
    session, requester = scenario
    ellie, noah = _family(session, requester)
    scheduling.family_search(
        session,
        requester=requester,
        members=[
            FamilyMemberInput(requester.patient, "cleaning"),
            FamilyMemberInput(ellie, "checkup"),
            FamilyMemberInput(noah, "checkup"),
        ],
        date_phrases=["2030-05-12"],
        back_to_back=True,
    )
    context = ToolContext(
        session=session,
        patient_session=requester.patient_session,
        conversation_id=requester.conversation.id,
    )

    result = _tool_create_staff_request(
        context,
        StaffRequestArgs(
            topic="callback",
            details="Family availability could not find a complete option; arrange the booking.",
        ),
    )

    assert result["availability_status"] == "unavailable"
    assert result["availability_checked"] is True
    assert result["cards"] == []
    assert result.get("record_id") is None


def test_availability_tool_failure_still_escalates(monkeypatch, scenario) -> None:
    session, requester = scenario
    ellie, noah = _family(session, requester)
    scheduling.family_search(
        session,
        requester=requester,
        members=[
            FamilyMemberInput(requester.patient, "cleaning"),
            FamilyMemberInput(ellie, "checkup"),
            FamilyMemberInput(noah, "checkup"),
        ],
        date_phrases=["2030-05-07"],
    )
    monkeypatch.setattr(
        scheduling,
        "refresh_scheduling_request",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("database unavailable")),
    )
    context = ToolContext(
        session=session,
        patient_session=requester.patient_session,
        conversation_id=requester.conversation.id,
    )

    result = _tool_create_staff_request(
        context,
        StaffRequestArgs(
            topic="callback",
            details="The availability search failed; please arrange the booking.",
        ),
    )

    assert result["kind"] == "callback"
    assert result["created"] is True
