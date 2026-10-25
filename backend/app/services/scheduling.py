"""Scheduling orchestration: the workflow layer between tools and primitives.

Each public function answers one "what the patient wants to do", composes the
deterministic pieces (date resolution, availability search, proposals,
appointments, emergencies), and returns a plain dict that is safe to hand to the
model. Nothing here trusts model-supplied identity: patients always come from
the verified session or from an explicit, already-authorised family member.

Returned dicts carry a ``cards`` list describing what the UI should render.
The backend decides card contents (it knows the real slot ids); the model only
decides prose.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    Appointment,
    AppointmentStatus,
    AppointmentType,
    AuditRecord,
    BookingProposal,
    Conversation,
    FamilyMember,
    Message,
    Patient,
    PatientSession,
    ProposalStatus,
    utcnow,
)
from app.observability import log_event
from app.services import appointments, audit, booking_policy, date_parsing, patients
from app.services.availability_engine import (
    FamilyMemberRequest,
    SlotOption,
    is_interval_bookable,
    resolve_appointment_type,
    search_availability,
    search_nearby_family_plans,
)
from app.services.proposals import (
    DraftItem,
    Outcome,
    confirm_proposal,
    create_proposal,
    find_by_public_id,
    pending_for_conversation,
    proposal_intent,
    reject_proposal,
    release_holds,
    supersede_pending,
)
from app.services.transactions import WriteTransactionError, write_transaction

logger = logging.getLogger(__name__)

MAX_FAMILY_MEMBERS = 4


@dataclass
class Requester:
    """Who is asking. Built by the tool layer from the session only."""

    patient: Patient
    patient_session: PatientSession
    conversation: Conversation

    @property
    def conversation_id(self) -> int:
        return self.conversation.id


# --------------------------------------------------------------------------- #
# Shared helpers
# --------------------------------------------------------------------------- #


def _fail(code: str, message: str, **extra: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {"ok": False, "error": code, "message": message, "cards": []}
    payload.update(extra)
    return payload


def _ok(**payload: Any) -> dict[str, Any]:
    payload.setdefault("ok", True)
    payload.setdefault("cards", [])
    return payload


def _first_name(patient: Patient) -> str:
    return patients.first_name(patient.full_name)


def _type_or_error(
    session: Session, code: str | None, *, default: str | None = None
) -> tuple[AppointmentType | None, dict[str, Any] | None]:
    appointment_type, error = resolve_appointment_type(session, code or default)
    if appointment_type is None:
        return None, _fail(
            "unknown_appointment_type",
            "I can look up cleanings, checkups, or emergency visits.",
        )
    return appointment_type, None


def slot_card(
    option: SlotOption,
    appointment_type: AppointmentType,
    *,
    intent: str = "new_booking",
    target_appointment_id: int | None = None,
    settled: bool = False,
    additional_visit: bool = False,
) -> dict[str, Any]:
    """One clickable time. The card carries everything the patient needs to choose.

    ``intent`` is what a click *means*: a card from an ordinary search books a
    new visit, while replacement slots for a pending move carry
    ``intent="reschedule"`` plus the appointment they would replace. The UI posts
    the card back verbatim, so picking a replacement cannot be mistaken for a
    second booking.

    ``settled`` says the request behind this grid already has a booking held or
    confirmed: the time is context, and the UI must not offer it as a new hold.

    ``require_open`` is the reverse of ``settled``: this turn searched the
    schedule and found the time holding nothing, so the card stands for an
    offer made *now*. The client re-checks a grid it cached at restore with
    ``/scheduling/availability``; a card that does not say it was just searched
    must not answer that check, or a stale grid from an older request is read
    as still open and stays disabled ("superseded or taken") forever.
    """
    return {
        "type": "slot",
        "intent": intent,
        "target_appointment_id": target_appointment_id,
        "require_open": not settled,
        "slot_id": option.stable_id,
        "starts_at": option.starts_at.isoformat(timespec="minutes"),
        "ends_at": option.ends_at.isoformat(timespec="minutes"),
        "day": option.starts_at.date().isoformat(),
        "day_label": option.starts_at.strftime("%a, %b %-d"),
        "time": option.starts_at.strftime("%-I:%M %p"),
        "end_time": option.ends_at.strftime("%-I:%M %p"),
        "time_range": (
            f"{option.starts_at.strftime('%-I:%M %p')} – {option.ends_at.strftime('%-I:%M %p')}"
        ),
        "provider": option.provider_name,
        "provider_credentials": option.provider_credentials,
        "duration_minutes": appointment_type.duration_minutes,
        "appointment_type": appointment_type.name,
        "appointment_type_code": appointment_type.code,
        "patient_first_name": option.patient_first_name,
        # Set when this request already has a booking held or confirmed: the
        # time is shown as context, never as something to book again.
        "settled": settled,
        # An explicitly requested second visit remains a live booking request
        # even when this conversation already has an appointment.  The client
        # sends this provenance back during a freshness re-check and selection.
        "additional_visit": additional_visit,
    }


# --------------------------------------------------------------------------- #
# Availability
# --------------------------------------------------------------------------- #


@dataclass
class AvailabilityAnswer:
    payload: dict[str, Any]
    options: list[SlotOption]
    appointment_type: AppointmentType


def _family_member_requests(
    session: Session,
    requester: Requester,
    *,
    appointment_type: AppointmentType,
    additional_first_name: str | None = None,
) -> list[FamilyMemberRequest]:
    """The people this conversation may book together: the verified adult, plus
    a child on the account only when the patient named that child.

    Another adult is never added here. The household link alone is not consent,
    and quietly booking two adults would not reflect what the patient asked for.
    """
    members: list[Patient] = [requester.patient]
    named = (additional_first_name or "").strip().lower()
    if named:
        allowed, _ = check_family_authorisation(session, requester)
        for patient in allowed:
            if patient.full_name.split(" ")[0].lower() == named:
                members.append(patient)
                break
    return [
        FamilyMemberRequest(
            patient_id=patient.id,
            patient_first_name=_first_name(patient),
            appointment_type=appointment_type,
        )
        for patient in members
    ]


def family_option_cards(
    *,
    plans: list[list[SlotOption]],
    appointment_types: dict[int, AppointmentType],
    start_index: int = 0,
) -> list[dict[str, Any]]:
    """One card per complete family plan, carrying every member's own time.

    ``option_id`` and ``option_identity`` both survive a page reload: the click
    posts them back, and the server re-derives the same option from SQLite.
    """
    cards: list[dict[str, Any]] = []
    for offset, plan in enumerate(plans):
        identity = family_plan_identity(plan)
        ordered = sorted(plan, key=lambda option: option.starts_at)
        is_back_to_back = all(
            previous.ends_at == current.starts_at
            for previous, current in zip(ordered, ordered[1:], strict=False)
        )
        cards.append(
            {
                "type": "family_option",
                "option_id": identity,
                "option_index": start_index + offset,
                "option_identity": identity,
                "slots": [
                    slot_card(option, appointment_types[option.patient_id or 0])
                    | {"intent": "new_booking"}
                    for option in plan
                ],
                "back_to_back": is_back_to_back,
                "title": "Book everyone together",
                "slot_count": len(plan),
                "starts_at": (
                    ordered[0].starts_at.isoformat(timespec="minutes") if ordered else None
                ),
                "ends_at": ordered[-1].ends_at.isoformat(timespec="minutes") if ordered else None,
            }
        )
    return cards


def find_availability(
    session: Session,
    *,
    patient: Patient,
    patient_id: int | None = None,
    appointment_type_code: str | None,
    date_phrases: list[str] | None = None,
    start_date: str | None = None,
    time_windows: list[str] | None = None,
    weekdays: list[str] | None = None,
    exclude_weekdays: list[str] | None = None,
    range_days: int | None = None,
    provider_name: str | None = None,
    per_day: int = 3,
    requester: Requester | None = None,
    group_first_name: str | None = None,
    requests_additional: bool = False,
) -> AvailabilityAnswer:
    """Search real availability under the patient's stated constraints.

    ``group_first_name`` is set when the patient asked for a household booking
    and named a second person. Then the answer is a set of complete nearby plans
    for both people rather than a list of single times, because one time can
    never satisfy a two-person request.
    """
    empty_type = AppointmentType(id=0, code="", name="", duration_minutes=30)
    appointment_type, problem = _type_or_error(session, appointment_type_code, default="checkup")
    if problem is not None or appointment_type is None:
        return AvailabilityAnswer(
            problem or _fail("unknown_type", "Unknown visit type."), [], empty_type
        )

    preferences = date_parsing.resolve_preferences(
        start_date=start_date,
        date_phrases=date_phrases,
        time_windows=time_windows,
        weekdays=weekdays,
        exclude_weekdays=exclude_weekdays,
        range_days=range_days,
        provider_name=provider_name,
    )
    base: dict[str, Any] = {
        "appointment_type": appointment_type.name,
        "appointment_type_code": appointment_type.code,
        "duration_minutes": appointment_type.duration_minutes,
        "requested": preferences.requested,
        "understood_window": preferences.window_label,
    }
    if preferences.rejected:
        base["rejected"] = preferences.rejected
    if preferences.warnings:
        base["notes"] = preferences.warnings

    if preferences.ambiguous:
        return AvailabilityAnswer(
            _ok(
                **base,
                ambiguous=True,
                needs_clarification=preferences.clarification,
                message=(
                    preferences.clarification
                    or "Which date did you mean? I want to check the right ones."
                ),
            ),
            [],
            appointment_type,
        )
    if preferences.past_rejected and not preferences.days:
        return AvailabilityAnswer(
            _ok(
                **base,
                rejected=["past_date"],
                message="That date has already passed. Which upcoming day works?",
            ),
            [],
            appointment_type,
        )

    if provider_name:
        available_providers = _provider_names(session)
        if not any(
            provider_name.strip().lower() in name.lower() for name in available_providers
        ):
            return AvailabilityAnswer(
                _ok(
                    **base,
                    unknown_provider=True,
                    message=(
                        f"We don't have a dentist named {provider_name}. "
                        "I can check the other dentists instead."
                    ),
                    providers=available_providers,
                ),
                [],
                appointment_type,
            )

    target_id = patient_id if patient_id is not None else patient.id
    targets = [session.get(Patient, target_id)] if target_id else []
    targets = [p for p in targets if p is not None]

    # How many times does this request need, and does it already have one? Read
    # from SQLite before offering anything, so a solo booking can never offer
    # (or hold) the same person twice.
    scope = booking_policy.booking_scope(
        session,
        patients=targets,
        conversation_id=requester.conversation_id if requester is not None else None,
    )

    base.update(
        booking_mode=scope.mode,
        desired_slot_count=scope.desired_slot_count(),
        covered_people=len(scope.covered_patient_ids),
        already_booked=scope.has_booked_visit,
    )

    family_members: list[FamilyMemberRequest] = []
    if group_first_name:
        family_members = _family_member_requests(
            session,
            requester,
            appointment_type=appointment_type,
            additional_first_name=group_first_name,
        )
        if len(family_members) < 2:
            return AvailabilityAnswer(
                _ok(
                    **base,
                    available=False,
                    needs_family_member=True,
                    message=(
                        f"I can only book you and the children on your account, so I can't add "
                        f"{group_first_name} here. They'd need to call us, or tell me if this is "
                        "just for you."
                    ),
                ),
                [],
                appointment_type,
            )
    if family_members:
        return _group_answer(
            session,
            members=family_members,
            appointment_type=appointment_type,
            preferences=preferences,
            base=base,
            scope=scope,
        )

    # One request books once. While a booking card is waiting, searching again
    # would either replace that hold or offer times that can never be booked, so
    # the waiting card comes back instead.
    #
    # The exception is the patient changing their mind: "Change" on a card, or
    # simply asking for other times, means the hold is no longer wanted. It is
    # handed back *now* rather than at the end of the hold window, so a click
    # that was reconsidered does not keep blocking the dentist's calendar. That
    # is checked first, because the guard looks for exactly the hold being
    # released -- asking for alternatives would otherwise get the card back and
    # never reach the search. A move and a new booking are both covered: "keep
    # the move, show me other times" is the same "I don't want that one" for the
    # held replacement.
    if requester is not None and not requests_additional and not group_first_name:
        changed_mind = _declined_after_latest_search(session, requester=requester) or bool(
            release_hold_on_change(session, requester=requester)
        )
        if not changed_mind:
            waiting = _pending_hold_answer(session, requester=requester)
            if waiting is not None:
                return AvailabilityAnswer(
                    {**waiting, **{key: value for key, value in base.items()}},
                    [],
                    appointment_type,
                )
        else:
            # Re-read what the thread still has in force: the released hold is
            # gone, and the grid below must not be marked settled by it.
            scope = booking_policy.booking_scope(
                session,
                patients=targets,
                conversation_id=requester.conversation_id,
            )
            base.update(
                booking_mode=scope.mode,
                desired_slot_count=scope.desired_slot_count(),
                covered_people=len(scope.covered_patient_ids),
                already_booked=scope.has_booked_visit,
            )

    result = search_availability(
        session,
        appointment_type=appointment_type,
        preferences=preferences,
        patients=targets,
        provider_name=provider_name,
        per_day=per_day,
    )
    options = booking_policy.restrict_plain_slots(result.options, scope)

    cards = [
        slot_card(option, appointment_type, additional_visit=requests_additional)
        for option in options
    ]
    # A time outside an existing visit is still on offer (a move, or an extra
    # visit); hiding it would leave the patient with nothing to pick.
    # One request books once. A booking already held *or* confirmed makes every
    # other time in that request context: the grid must not offer a second hold
    # for the same visit. A hold counts even though nothing is booked yet —
    # otherwise the grid stays fully clickable after the patient has picked a
    # time and moved on, which is exactly the "old slot cards stay live" bug.
    # An explicit request for a separate visit still gets clickable times.
    settled = (
        (scope.has_booked_visit or scope.pending) and not requests_additional
    )
    # Why the grid is settled, so the prose can say "I'm holding your time"
    # for a hold and "you're booked" for a booking.
    settled_by = "booked" if scope.has_booked_visit else ("hold" if settled else None)
    for card in cards:
        card["settled"] = settled
        if settled_by is not None:
            card["settled_by"] = settled_by
    if not options:
        message = (
            "I couldn't find an open time that matches that. I can widen the dates or "
            "check a different part of the day."
        )
        if preferences.days:
            message = (
                "There's nothing open on those specific dates. I can check nearby days if "
                "you're flexible."
            )
        if scope.has_booked_visit and not requests_additional:
            message = booking_policy.already_booked_message(scope)
        return AvailabilityAnswer(
            _ok(
                **base,
                available=False,
                message=message,
                closed_days=[day.isoformat() for day in result.closed_days],
            ),
            [],
            appointment_type,
        )

    days_available = sorted({option.day for option in options})
    payload = _ok(
        **base,
        available=True,
        slot_count=len(options),
        days=[day.isoformat() for day in days_available],
        providers=result.providers_considered,
        message=(
            f"{len(options)} open time(s) across {len(days_available)} day(s). "
            "These are real slots from the schedule; choosing one only holds it until you confirm."
        ),
        cards=cards,
    )
    if scope.has_booked_visit and not requests_additional:
        payload["note"] = (
            booking_policy.already_booked_message(scope)
            + " These times are what's open, and holding one would only be right for a move "
            "or a separate visit."
        )
    return AvailabilityAnswer(payload, options, appointment_type)


def _group_answer(
    session: Session,
    *,
    members: list[FamilyMemberRequest],
    appointment_type: AppointmentType,
    preferences,
    base: dict[str, Any],
    scope: booking_policy.BookingScope,
) -> AvailabilityAnswer:
    """Complete nearby same-day plans for a household, shaped as family options.

    Every option is a real open run of the schedule for each person, so one card
    covers exactly the visits the request needs. There is no list of single times
    here: a single time could only ever book one of the people.
    """
    appointment_types = {member.patient_id: member.appointment_type for member in members}
    plans = search_nearby_family_plans(
        session, members=members, preferences=preferences, max_plans=6
    )
    people = [member.patient_first_name for member in members]
    base.update(
        booking_mode="family",
        family=True,
        people=people,
        desired_slot_count=len(members),
        covered_people=len(scope.covered_patient_ids),
        back_to_back=False,
    )
    if not plans:
        return AvailabilityAnswer(
            _ok(
                **base,
                available=False,
                message=(
                    "I couldn't find currently open times for everyone on those days. I can "
                    "check other days, or tell me if you'd like separate visits."
                ),
            ),
            [],
            appointment_type,
        )

    plans = plans[:3]
    _FAMILY_PLAN_CACHE[_family_cache_key(members)] = plans
    days = sorted({plan[0].day for plan in plans if plan})
    return AvailabilityAnswer(
        _ok(
            **base,
            available=True,
            plan_count=len(plans),
            slots_per_option=len(members),
            days=[day.isoformat() for day in days],
            message=(
                f"{len(plans)} complete family option(s) for "
                f"{' and '.join(people)}, ordered by closest appointment times. "
                "One confirmation books everyone."
            ),
            cards=family_option_cards(plans=plans, appointment_types=appointment_types),
        ),
        [option for plan in plans for option in plan],
        appointment_type,
    )


def _provider_names(session: Session) -> list[str]:
    from app.services.availability_engine import load_providers

    return [provider.full_name for provider in load_providers(session)]


def options_for_slots(
    session: Session,
    *,
    appointment_type: AppointmentType,
    slot_ids: list[str],
    patient_id: int | None = None,
    patient_first_name: str | None = None,
    date_phrases: list[str] | None = None,
    start_date: str | None = None,
    time_windows: list[str] | None = None,
    weekdays: list[str] | None = None,
    exclude_weekdays: list[str] | None = None,
    range_days: int | None = None,
    provider_name: str | None = None,
) -> list[SlotOption]:
    """Turn slot ids chosen in the UI into SlotOptions, re-derived from SQLite.

    The client's ids are treated as *hints*: each one is decoded and checked for
    real bookability, and the same preference constraints are applied. A stale,
    fabricated, or now-taken id simply does not appear. Nothing is booked from a
    client payload directly.
    """
    wanted = {slot_id for slot_id in slot_ids if slot_id}
    if not wanted:
        return []

    constraints = date_parsing.resolve_preferences(
        start_date=start_date,
        date_phrases=date_phrases,
        time_windows=time_windows,
        weekdays=weekdays,
        exclude_weekdays=exclude_weekdays,
        range_days=range_days,
        provider_name=provider_name,
    )
    resolved: list[SlotOption] = []
    if not constraints.ambiguous:
        for slot_id in wanted:
            option = _option_from_stable_id(session, appointment_type, slot_id)
            if option is None:
                continue
            if not constraints.accepts_day(option.starts_at.date()):
                continue
            if not constraints.accepts_time(option.starts_at):
                continue
            resolved.append(option)
    for option in resolved:
        option.patient_id = patient_id
        option.patient_first_name = patient_first_name
    resolved.sort(key=lambda option: option.starts_at)
    return resolved


def _option_from_stable_id(
    session: Session, appointment_type: AppointmentType, stable_id: str
) -> SlotOption | None:
    """Decode ``slot_<provider>_<YYYYmmddHHMM>`` and verify it is bookable now."""
    parts = stable_id.split("_")
    if len(parts) != 3 or parts[0] != "slot":
        return None
    try:
        provider_id = int(parts[1])
        starts_at = datetime.strptime(parts[2], "%Y%m%d%H%M")
    except ValueError:
        return None
    rows = is_interval_bookable(
        session,
        provider_id=provider_id,
        starts_at=starts_at,
        duration_minutes=appointment_type.duration_minutes,
        appointment_type=appointment_type,
    )
    if rows is None:
        return None
    provider = rows[0].provider
    return SlotOption(
        slot_ids=[row.id for row in rows],
        stable_id=stable_id,
        provider_id=provider_id,
        provider_name=provider.full_name,
        provider_credentials=provider.credentials,
        provider_specialties=provider.specialty_names,
        appointment_type_code=appointment_type.code,
        appointment_type_name=appointment_type.name,
        starts_at=rows[0].starts_at,
        ends_at=rows[-1].ends_at,
        duration_minutes=appointment_type.duration_minutes,
    )


# --------------------------------------------------------------------------- #
# Proposals
# --------------------------------------------------------------------------- #


def propose_booking(
    session: Session,
    *,
    requester: Requester,
    appointment_type: AppointmentType,
    options: list[SlotOption],
    summary: str | None = None,
    from_ui: bool = False,
    create_key: str | None = None,
    family_group_id: int | None = None,
) -> dict[str, Any]:
    """Hold selected slots for a NEW visit and persist an awaiting-confirmation proposal.

    This is the only path a new booking takes, and it always produces
    ``kind="booking"``: there is no appointment to retire, so confirming it adds
    a visit. Moving a visit must go through :func:`propose_reschedule`.

    Before anything is held, the request is checked against what this booking
    already has (``booking_policy``): one visit per person, no overlapping holds,
    and never a second hold while one is waiting. A refusal names the live card
    so the patient confirms that instead of booking twice.
    """
    if not options:
        return _fail("no_slots", "Which time would you like me to hold?")

    # A fresh slot list is only for a fresh request. With a card already
    # waiting, another hold would give the same request two bookings; the guard
    # below refuses it, and this makes the refusal the same whether the slot
    # came from a grid or from the assistant's own tool call.
    if not booking_policy.requests_additional_visit(summary):
        waiting = _pending_hold_answer(session, requester=requester)
        if waiting is not None:
            return waiting

    option_patients = {option.patient_id for option in options if option.patient_id is not None}
    scope_patients = [requester.patient]
    if option_patients - {requester.patient.id}:
        scope_patients = list(
            session.scalars(select(Patient).where(Patient.id.in_(sorted(option_patients))))
        ) or [requester.patient]
    scope = booking_policy.booking_scope(
        session, patients=scope_patients, conversation_id=requester.conversation_id
    )
    refusal = booking_policy.refusal_for_hold(
        session,
        scope=scope,
        intervals=[
            booking_policy.HeldInterval(
                patient_id=option.patient_id or requester.patient.id,
                starts_at=option.starts_at,
                duration_minutes=appointment_type.duration_minutes,
            )
            for option in options
        ],
        summary=summary,
    )
    if refusal is not None:
        if refusal.get("error") == "pending_booking_exists":
            # Hand back the live card so the UI shows what to confirm instead.
            refusal["cards"] = [
                card
                for card in live_proposal_cards(session, requester.conversation_id)
                if card.get("proposal_id") == refusal.get("pending_proposal_id")
            ]
        return refusal

    supersede_pending(
        session, conversation_id=requester.conversation_id, kinds=("booking",)
    )
    items = [
        DraftItem(
            patient_id=option.patient_id or requester.patient.id,
            patient_first_name=option.patient_first_name or _first_name(requester.patient),
            appointment_type=appointment_type,
            option=option,
        )
        for option in options
    ]
    outcome = create_proposal(
        session,
        conversation_id=requester.conversation_id,
        items=items,
        kind="booking",
        intent="new_booking",
        summary=summary,
        from_ui=from_ui,
        create_key=create_key,
        family_group_id=family_group_id,
        visit_shape="household" if len(items) > 1 else "solo",
    )
    if not outcome.ok:
        return _fail(
            outcome.error or "proposal_failed",
            outcome.message or "I couldn't hold that time.",
            conflicts=outcome.conflicts,
        )

    proposal = find_by_public_id(session, outcome.proposal_id or "")
    if proposal is None:
        return _fail("proposal_failed", "I couldn't hold that time.")
    card = proposal_card(proposal, include_actions=True)
    return _ok(
        intent="new_booking",
        proposal_id=proposal.public_id,
        awaiting_confirmation=True,
        replayed=outcome.replayed,
        message=(
            "Held for you. Nothing is booked until you confirm."
            if not outcome.replayed
            else "That proposal is already waiting for your confirmation."
        ),
        cards=[card],
    )


def _pending_hold_answer(
    session: Session, *, requester: Requester, kind: tuple[str, ...] = ("booking",)
) -> dict[str, Any] | None:
    """A booking card already waiting for confirmation, if there is one.

    Search and hold both refuse rather than replace it: the patient is one click
    from booking, and silently releasing that hold would make them pick again.
    The live card is returned so the UI keeps showing what to confirm, even
    after a refresh or a restart.
    """
    scope = booking_policy.booking_scope(
        session, patients=[requester.patient], conversation_id=requester.conversation_id
    )
    pending = next(
        (
            proposal
            for proposal in pending_for_conversation(session, requester.conversation_id)
            if proposal.kind in kind and proposal.visit_shape != "household"
        ),
        None,
    )
    if scope.pending_booking is None and pending is None:
        return None
    proposal = scope.pending_booking or pending
    if proposal is None:
        return None
    card = proposal_card(proposal, include_actions=True)
    if scope.pending_booking is None and len(proposal.items) > 1:
        # A household card held by an earlier search: keep its family shape so
        # the UI shows one confirmation for everyone, not a single-visit card.
        card["type"] = "family_proposal"
        card["title"] = "Confirm family booking"
    return _fail(
        "pending_booking_exists",
        booking_policy.pending_card_message(proposal),
        pending_proposal_id=proposal.public_id,
        # A held time is not a booked one: these cards are context, not offers.
        cards=[card],
        next_step=(
            "A booking is already held for this request. Show the card back and ask the "
            "patient to confirm or decline it, instead of searching for or holding another "
            "time. Only if they explicitly ask for extra times, call "
            "change_booking_proposal with the new slot ids."
        ),
    )


def _declined_after_latest_search(session: Session, *, requester: Requester) -> bool:
    """Whether the last thing that happened to this request was a decline.

    The guard that returns a waiting hold exists so a patient who is one click
    from booking is not silently offered something else. Once they themselves
    declined it, that guard inverts: the search must run so the reply can name
    the times that are actually open.
    """
    searched_at = session.scalar(
        select(func.max(Message.created_at)).where(
            Message.conversation_id == requester.conversation_id,
            Message.cards.is_not(None),
        )
    )
    declined_at = session.scalar(
        select(func.max(AuditRecord.occurred_at)).where(
            AuditRecord.conversation_id == requester.conversation_id,
            AuditRecord.action == "proposal.rejected",
        )
    )
    if declined_at is None:
        return False
    return searched_at is None or declined_at >= searched_at


def session_has_booking_for(
    session: Session,
    *,
    requester: Requester,
) -> bool:
    """Whether this request already has a booking held or confirmed.

    Used by the availability refresh: the times behind an old grid are still
    real, but once one of them (or a household card) is held or booked the rest
    are alternatives that were not chosen, and offering them as fresh holds
    would let one request book twice.
    """
    scope = booking_policy.booking_scope(
        session, patients=[requester.patient], conversation_id=requester.conversation_id
    )
    if scope.pending_booking is not None or scope.has_booked_visit:
        return True
    return any(
        proposal.kind == "booking"
        for proposal in pending_for_conversation(session, requester.conversation_id)
    )


def _release_previous_replacement_holds(
    session: Session,
    *,
    requester: Requester,
    target_id: int,
    keep_public_id: str | None = None,
) -> None:
    """Release every held replacement this visit already had, then supersede it.

    Picking a second replacement time must free the first one immediately --
    otherwise the old hold keeps blocking the slot (including for the patient's
    own retry) until it expires. Releasing and superseding happen in one write
    transaction; ``keep_public_id`` protects the proposal being replayed.
    """
    stale = [
        proposal
        for proposal in pending_for_conversation(session, requester.conversation_id)
        if proposal.kind == "reschedule"
        and proposal.public_id != keep_public_id
        and (
            proposal.target_appointment_id == target_id
            or any(item.target_appointment_id == target_id for item in proposal.items)
        )
    ]
    if not stale:
        return
    try:
        with write_transaction(session):
            now = utcnow()
            for proposal in stale:
                release_holds(session, proposal, now)
                proposal.status = ProposalStatus.SUPERSEDED.value
                proposal.resolved_at = now
    except WriteTransactionError:
        logger.warning(
            "Could not release an earlier replacement hold in conversation %s",
            requester.conversation_id,
        )


def release_hold_on_change(
    session: Session,
    *,
    requester: Requester,
    kinds: tuple[str, ...] = ("booking", "reschedule"),
    keep_public_id: str | None = None,
) -> list[str]:
    """Hand back held times the patient has just moved off of.

    The patient changing their mind is the common case, and it must free the
    dentist's calendar *immediately* -- not when the hold window lapses. "Change"
    on a card, or asking for other times, means this hold is no longer wanted:
    nothing was booked, so keeping the interval reserved would let a click that
    was reconsidered in ten seconds block another patient for the whole window.

    This is deliberately separate from ``supersede_pending``: superseding also
    expects a *replacement* proposal to be created (a new hold takes the place of
    the old one). A search holds nothing, so it needs a release without a
    successor. The card still says "Held until ..." where it stands in the
    transcript; the server is the authority on whether it is still actionable,
    and the next pending read stops offering it.
    """
    stale = [
        proposal
        for proposal in pending_for_conversation(session, requester.conversation_id)
        if proposal.kind in kinds and proposal.public_id != keep_public_id
    ]
    if not stale:
        return []
    released: list[str] = []
    try:
        with write_transaction(session):
            now = utcnow()
            for proposal in stale:
                release_holds(session, proposal, now)
                proposal.status = ProposalStatus.SUPERSEDED.value
                proposal.resolved_at = now
                audit.record(
                    session,
                    action="proposal.released_on_change",
                    actor_type="patient",
                    entity_type="booking_proposal",
                    entity_id=proposal.public_id,
                    conversation_id=requester.conversation_id,
                    details={"reason": "patient_changed_their_mind"},
                )
                released.append(proposal.public_id)
    except WriteTransactionError:
        logger.warning(
            "Could not release holds after a change in conversation %s",
            requester.conversation_id,
        )
    return released


def _finalize_reschedule_outcome(
    session: Session,
    *,
    requester: Requester,
    target_id: int,
    outcome: Outcome,
    from_ui: bool,
) -> dict[str, Any]:
    """Shared tail for a reschedule proposal, whichever surface asked for it."""
    if not outcome.ok:
        return _fail(
            outcome.error or "reschedule_failed",
            outcome.message or "I couldn't set up that change.",
            appointment_unchanged=True,
            kind="reschedule",
            intent="reschedule",
            appointment_id=target_id,
            conflicts=outcome.conflicts,
        )
    _release_previous_replacement_holds(
        session, requester=requester, target_id=target_id, keep_public_id=outcome.proposal_id
    )
    proposal = find_by_public_id(session, outcome.proposal_id or "")
    if proposal is None:  # pragma: no cover - the row was just committed
        return _fail("proposal_failed", "I couldn't hold that time.")

    original = session.get(Appointment, target_id)
    first = min(proposal.items, key=lambda item: item.order_index, default=None)
    return _ok(
        kind="reschedule",
        intent="reschedule",
        awaiting_confirmation=True,
        appointment_unchanged=True,
        replayed=outcome.replayed,
        proposal_id=proposal.public_id,
        target_appointment_id=target_id,
        original_starts_at=(
            original.starts_at.isoformat(timespec="minutes")
            if original is not None and original.starts_at
            else (
                first.original_starts_at.isoformat(timespec="minutes")
                if first is not None and first.original_starts_at
                else None
            )
        ),
        new_starts_at=(
            first.proposed_starts_at.isoformat(timespec="minutes")
            if first is not None and first.proposed_starts_at
            else None
        ),
        appointment_type=first.appointment_type_code if first is not None else None,
        duration_minutes=first.duration_minutes if first is not None else None,
        message=(
            "I've held the new time. Your current appointment stays booked until you confirm."
        ),
        next_step=(
            "Ask the patient to confirm. When they do, call confirm_appointment_change with "
            "this proposal_id -- not confirm_booking_proposal."
        ),
        cards=[proposal_card(proposal, include_actions=True)],
    )


def propose_reschedule(
    session: Session,
    *,
    requester: Requester,
    view: appointments.AppointmentView,
    option: SlotOption,
    appointment_type: AppointmentType,
    from_ui: bool = False,
) -> dict[str, Any]:
    """Hold a replacement time for one existing appointment. Books nothing.

    ``view`` is only ever produced by ``appointments.resolve_reschedule_target``,
    which proves the appointment belongs to the verified patient (or by the
    pending proposal's own target). ``requester.patient`` must be the same
    person, so a replacement can never be created for somebody else's visit.

    The reschedule card records ``intent="reschedule"`` and the original's id on
    every slot, so whichever surface continues the move (a click, a reply, or the
    model's own tool call) knows it is moving this visit rather than booking a
    second one.
    """
    target = view.appointment
    if target.patient_id != requester.patient.id:
        return _fail(
            "unknown_appointment",
            "I can't find that appointment.",
            appointment_unchanged=True,
            kind="reschedule",
            intent="reschedule",
        )
    if option.patient_id is not None and option.patient_id != target.patient_id:
        return _fail(
            "invalid_replacement",
            "That time is for a different patient. Nothing was changed.",
            appointment_unchanged=True,
            kind="reschedule",
            intent="reschedule",
            appointment_id=target.id,
        )
    if option.starts_at == target.starts_at and option.provider_id == target.provider_id:
        return _fail(
            "slot_unavailable",
            "That is already your current appointment time. Nothing was changed.",
            appointment_unchanged=True,
            kind="reschedule",
            intent="reschedule",
            appointment_id=target.id,
        )

    outcome = appointments.propose_reschedule(
        session,
        conversation_id=requester.conversation_id,
        patient_id=requester.patient.id,
        view=view,
        option=option,
        appointment_type=appointment_type,
    )
    return _finalize_reschedule_outcome(
        session,
        requester=requester,
        target_id=target.id,
        outcome=outcome,
        from_ui=from_ui,
    )


def replacement_for_slot(
    session: Session,
    *,
    requester: Requester,
    target_id: int | None,
    slot_id: str,
    proposal_id: str | None = None,
    appointment_type_code: str | None = None,
    which: str | None = None,
    from_ui: bool = False,
) -> dict[str, Any]:
    """A replacement time was chosen -- by a click or by the model -- for a move.

    Context resolution, most specific first:

    1. ``target_id`` from the slot card / tool argument, re-proved against the
       verified patient (so a forged id cannot move another patient's visit);
    2. the pending reschedule proposal (or a named one) -- this is what preserves
       ``intent=reschedule`` when the patient clicks a replacement slot that the
       assistant rendered without the extra fields;
    3. otherwise there is nothing to move: this is a new booking, and the caller
       must say so explicitly by calling ``propose_booking``.

    The result always has ``kind="reschedule"`` or ``ok=False``; it never falls
    through to a booking.
    """
    pending = {
        "reschedule": _pending_reschedule(session, requester),
    }
    if target_id is None and proposal_id:
        named = find_by_public_id(session, proposal_id)
        if (
            named is not None
            and named.conversation_id == requester.conversation_id
            and named.kind == "reschedule"
            and named.status == ProposalStatus.PENDING.value
        ):
            pending["reschedule"] = named
    if target_id is None:
        proposal = pending["reschedule"]
        if proposal is not None:
            target_id = proposal.target_appointment_id or next(
                (
                    item.target_appointment_id
                    for item in proposal.items
                    if item.target_appointment_id
                ),
                None,
            )

    if target_id is None:
        return _fail(
            "no_reschedule_target",
            (
                "I don't have a visit to move yet. Tell me which appointment you mean, or "
                "pick a time to book a new one."
            ),
            kind="booking",
            intent="new_booking",
            next_step=(
                "This is a new booking: call create_booking_proposal. To move a visit, call "
                "reschedule_appointment with its appointment_id first."
            ),
        )

    try:
        view = appointments.resolve_reschedule_target(
            session,
            patient_id=requester.patient.id,
            appointment_id=target_id,
            which=which,
        )
    except appointments.RescheduleTargetError as exc:
        details = dict(exc.extra)
        details.setdefault("appointment_unchanged", True)
        return _fail(
            exc.code,
            exc.message,
            kind="reschedule",
            intent="reschedule",
            appointment_id=target_id,
            **details,
        )

    type_code = appointment_type_code or (
        view.appointment_type.code if view.appointment_type else None
    )
    appointment_type, problem = _type_or_error(session, type_code)
    if problem is not None or appointment_type is None:
        return _fail(
            "unknown_appointment_type",
            "I couldn't tell which kind of visit that is. Nothing was changed.",
            appointment_unchanged=True,
            kind="reschedule",
            intent="reschedule",
            appointment_id=target_id,
        )

    options = options_for_slots(
        session,
        appointment_type=appointment_type,
        slot_ids=[slot_id],
        patient_id=requester.patient.id,
        patient_first_name=view.first_name,
    )
    if not options:
        return _fail(
            "slot_unavailable",
            "That new time is no longer open. Nothing was changed. I can show other times.",
            appointment_unchanged=True,
            kind="reschedule",
            intent="reschedule",
            appointment_id=target_id,
        )
    return propose_reschedule(
        session,
        requester=requester,
        view=view,
        option=options[0],
        appointment_type=appointment_type,
        from_ui=from_ui,
    )


def _pending_booking(session: Session, requester: Requester) -> BookingProposal | None:
    return next(
        (
            proposal
            for proposal in pending_for_conversation(session, requester.conversation_id)
            if proposal.kind == "booking"
        ),
        None,
    )


def _pending_reschedule(session: Session, requester: Requester) -> BookingProposal | None:
    return next(
        (
            proposal
            for proposal in pending_for_conversation(session, requester.conversation_id)
            if proposal.kind == "reschedule"
        ),
        None,
    )


def pending_target(session: Session, requester: Requester) -> int | None:
    """The appointment a pending reschedule is about, if any."""
    proposal = _pending_reschedule(session, requester)
    if proposal is None:
        return None
    return proposal.target_appointment_id or next(
        (item.target_appointment_id for item in proposal.items if item.target_appointment_id),
        None,
    )


def replacement_cards(
    session: Session,
    *,
    requester: Requester,
    options: list[SlotOption],
    appointment_type: AppointmentType,
    target_id: int,
) -> list[dict[str, Any]]:
    """Slot cards that mean "move this visit", carrying the target explicitly."""
    cards = []
    for option in options:
        card = slot_card(
            option,
            appointment_type,
            intent="reschedule",
            target_appointment_id=target_id,
        )
        card["moving_from"] = _original_starts_label(session, target_id)
        cards.append(card)
    return cards


def _original_starts_label(session: Session, target_id: int) -> str | None:
    row = session.get(Appointment, target_id)
    if row is None or row.starts_at is None:
        return None
    return row.starts_at.isoformat(timespec="minutes")


def find_replacement_availability(
    session: Session,
    *,
    requester: Requester,
    target_id: int | None,
    date_phrases: list[str] | None = None,
    start_date: str | None = None,
    time_windows: list[str] | None = None,
    weekdays: list[str] | None = None,
    exclude_weekdays: list[str] | None = None,
    range_days: int | None = None,
    provider_name: str | None = None,
    per_day: int = 3,
) -> dict[str, Any]:
    """Times a *pending move* could take, as cards that say reschedule.

    Availability is the same read-only search an ordinary booking uses; the
    difference is entirely in the cards (``intent="reschedule"`` and the target
    id) and in keeping the visit's own type and duration.
    """
    if target_id is None:
        return _fail(
            "no_active_booking",
            "There is no pending appointment move to search for.",
            appointment_unchanged=True,
            kind="reschedule",
            intent="reschedule",
        )
    try:
        view = appointments.resolve_reschedule_target(
            session, patient_id=requester.patient.id, appointment_id=target_id
        )
    except appointments.RescheduleTargetError as exc:
        return _fail(
            exc.code,
            exc.message,
            appointment_unchanged=True,
            kind="reschedule",
            intent="reschedule",
            **exc.extra,
        )

    appointment_type = view.appointment_type
    if appointment_type is None:
        return _fail(
            "invalid",
            "I can't find that appointment's visit type.",
            appointment_unchanged=True,
            kind="reschedule",
        )

    answer = find_availability(
        session,
        patient=requester.patient,
        appointment_type_code=appointment_type.code,
        date_phrases=date_phrases,
        start_date=start_date,
        time_windows=time_windows,
        weekdays=weekdays,
        exclude_weekdays=exclude_weekdays,
        range_days=range_days,
        provider_name=provider_name,
        per_day=per_day,
        # The requester is what lets the search hand back a held replacement
        # the patient has just moved off of ("show me other new times"): without
        # it the old hold would keep blocking the slot for the full window.
        requester=requester,
    )
    payload = dict(answer.payload)
    payload.update(
        ok=True,
        kind="reschedule",
        intent="reschedule",
        appointment_unchanged=True,
        target_appointment_id=view.appointment.id,
        original_starts_at=view.appointment.starts_at.isoformat(timespec="minutes"),
        # Never let a replacement search read as "nothing to move": the visit is
        # named (identically) by both keys the rest of the app reads.
        appointment_id=view.appointment.id,
    )
    if payload.get("available") and answer.options:
        cards = replacement_cards(
            session,
            requester=requester,
            options=answer.options,
            appointment_type=answer.appointment_type,
            target_id=view.appointment.id,
        )
        payload["cards"] = cards
        payload["replacement_slot_count"] = len(
            [card for card in cards if card.get("type") == "slot"]
        )
        payload["message"] = (
            f"{len(answer.options)} open time(s) to move your "
            f"{appointment_type.name.lower()} to. Your current appointment stays booked "
            "until you confirm one."
        )
    else:
        # No replacement times: say explicitly that the move is still pending and
        # the visit is untouched, so the model does not fall back to a booking.
        payload["kind"] = "reschedule"
        payload["intent"] = "reschedule"
        payload["message"] = (
            "I couldn't find a new time that matches that. Your current appointment is "
            "still booked. I can check other days or times for the move."
        )
    return payload


def confirm_booking(
    session: Session,
    *,
    requester: Requester,
    proposal_id: str,
    confirm_key: str | None = None,
    actor: str = "patient",
) -> dict[str, Any]:
    """Confirm a *new-booking* proposal. Refuses a move or cancellation card.

    The refusal is deliberate: a booking confirmation has no original to retire,
    so letting it accept a reschedule card is exactly how a move turned into a
    second appointment. The caller is told which tool to use instead.
    """
    proposal = find_by_public_id(session, proposal_id)
    if proposal is None or proposal.conversation_id != requester.conversation_id:
        return _fail("unknown_proposal", "I don't see that proposal anymore.")
    if proposal.kind != "booking":
        return _fail(
            "wrong_proposal_kind",
            "That's a change to an existing visit. Use confirm_appointment_change.",
            kind=proposal.kind,
            intent=proposal_intent(proposal),
            appointment_unchanged=True,
            next_step="Confirm it with confirm_appointment_change (or the card's own button).",
        )
    if proposal_intent(proposal) != "new_booking":
        return _fail(
            "wrong_proposal_kind",
            "That proposal is not a new booking. Use confirm_appointment_change.",
            kind=proposal.kind,
            intent=proposal_intent(proposal),
            appointment_unchanged=True,
        )
    return confirm(
        session,
        requester=requester,
        proposal_id=proposal_id,
        confirm_key=confirm_key,
        actor=actor,
    )


def confirm_change(
    session: Session,
    *,
    requester: Requester,
    proposal_id: str,
    confirm_key: str | None = None,
    actor: str = "patient",
) -> dict[str, Any]:
    """Confirm a reschedule or cancellation proposal. Refuses a new booking."""
    proposal = find_by_public_id(session, proposal_id)
    if proposal is None or proposal.conversation_id != requester.conversation_id:
        return _fail("unknown_proposal", "I don't see that proposal anymore.")
    if proposal.kind not in {"reschedule", "cancellation"}:
        return _fail(
            "wrong_proposal_kind",
            "That isn't a reschedule or cancellation. Use confirm_booking_proposal instead.",
            kind=proposal.kind,
            intent=proposal_intent(proposal),
        )
    return confirm(
        session,
        requester=requester,
        proposal_id=proposal_id,
        confirm_key=confirm_key,
        actor=actor,
    )


def confirm(
    session: Session,
    *,
    requester: Requester,
    proposal_id: str,
    confirm_key: str | None = None,
    actor: str = "patient",
) -> dict[str, Any]:
    """Confirm *whichever* kind of proposal this is, through one atomic path.

    This is what a card button calls, and it needs no hint from the model or the
    client: the row's own ``kind`` decides whether the transaction books a visit
    or moves one. Either way the work happens in a single ``BEGIN IMMEDIATE``
    (see ``proposals.confirm_proposal``): revalidation, releasing the original's
    claims, retiring it, creating the replacement, and accepting the proposal
    all commit together or not at all. A repeat replays the committed result.
    """
    proposal = find_by_public_id(session, proposal_id)
    if proposal is None:
        return _fail("unknown_proposal", "I don't see that proposal anymore.")
    if proposal.conversation_id != requester.conversation_id:
        # Never confirm a proposal belonging to another conversation.
        return _fail("unknown_proposal", "I don't see that proposal anymore.")
    if (
        proposal.status != ProposalStatus.ACCEPTED.value
        and proposal.kind in {"reschedule", "cancellation"}
        and not _change_target_is_live(session, proposal)
    ):
        # The visit this change was about has already moved or been cancelled.
        # Confirming now would move (or release) a row that is no longer active,
        # which is how a stale card turns into a second appointment. Accepted
        # proposals are excluded: repeating their confirmation must replay the
        # already committed result even though their original is now retired.
        _close_stale_change(session, proposal)
        payload = _fail(
            "proposal_stale",
            (
                "That appointment has already been changed, so this old card no longer "
                "applies. Nothing else was changed."
            ),
            proposal_id=proposal_id,
            kind=proposal.kind,
            appointment_unchanged=True,
            cards=[proposal_card(proposal, include_actions=False)],
        )
        return payload

    outcome: Outcome = confirm_proposal(
        session,
        proposal=proposal,
        confirm_key=confirm_key,
        actor=actor,
        patient_session_id=requester.patient_session.id,
    )
    if not outcome.ok:
        payload = _fail(
            outcome.error or "confirm_failed",
            outcome.message or "I couldn't confirm that booking.",
            proposal_id=proposal_id,
            conflicts=outcome.conflicts,
            appointment_unchanged=True,
            kind=proposal.kind,
        )
        if outcome.error == "slot_taken":
            fresh_family_cards: list[dict[str, Any]] = []
            family_conflict = proposal.kind == "booking" and len(proposal.items) > 1
            if family_conflict:
                # The family request is one atomic booking. Retire the stale
                # hold before searching so the next cards represent one fresh
                # complete plan for everyone, never a partial booking.
                supersede_pending(
                    session,
                    conversation_id=requester.conversation_id,
                    kinds=("booking",),
                    intent="new_booking",
                )
                fresh_family_cards = family_options_after_conflict(
                    session, requester=requester, proposal=proposal
                )
            if fresh_family_cards:
                payload["message"] = (
                    "Those times changed before confirmation, so nothing was booked. "
                    "I released that hold and found fresh nearby options for everyone."
                )
                payload["next_step"] = "Offer the fresh family options; do not retry the old card."
                payload["proposal_stale"] = True
                payload["cards"] = fresh_family_cards
            elif family_conflict:
                payload["message"] = (
                    "Those times changed before confirmation, so nothing was booked. "
                    "I couldn't find another complete nearby plan on that day."
                )
                payload["next_step"] = "Ask whether the patient wants another day or separate visits."
                payload["proposal_stale"] = True
                payload["cards"] = []
            else:
                payload["next_step"] = "Search again and offer the fresh alternatives."
                payload["cards"] = [proposal_card(proposal, include_actions=True)]
        elif outcome.error == "booking_conflict":
            # Nothing was written, but this card can never succeed as it stands
            # (its visit moved, or the booking would duplicate one). Stop
            # offering it; the patient is told what happened instead.
            payload["next_step"] = (
                "Explain that nothing changed and ask what they'd like to do next. "
                "Do not retry this card."
            )
            payload["proposal_stale"] = True
            payload["cards"] = [proposal_card(proposal, include_actions=False)]
        return payload

    session.refresh(proposal)
    # Read back what was written. If the rows are not there, the booking did not
    # commit: report a failure rather than a false "Confirmed."
    if not _outcome_is_persisted(session, outcome, proposal):
        log_event(
            logger,
            "booking.unverified",
            proposal_id=proposal_id,
            kind=proposal.kind,
            level=logging.ERROR,
        )
        _restore_pending(session, proposal)
        return _fail(
            "not_saved",
            "I couldn't save that booking. Nothing has been changed. Please try again.",
            proposal_id=proposal_id,
            appointment_unchanged=proposal.kind == "reschedule",
            cards=[proposal_card(proposal)],
        )

    cards: list[dict[str, Any]] = []
    if proposal.kind == "cancellation":
        cards.append(cancellation_confirmation_card(session, proposal))
    else:
        cards.append(confirmation_card(session, proposal, outcome))
    payload = _ok(
        confirmed=True,
        replayed=outcome.replayed,
        kind=proposal.kind,
        intent=proposal_intent(proposal),
        proposal_id=proposal.public_id,
        booking_group_id=outcome.booking_group_id,
        appointments=outcome.appointments,
        released_appointment_ids=outcome.released_appointment_ids,
        rescheduled=outcome.rescheduled,
        appointment_unchanged=False,
        message=outcome.message or "Confirmed.",
        cards=cards,
    )
    if proposal.kind != "cancellation":
        # The same "what is active now" read that the chat listing, the staff
        # dashboard and the history use, so a confirmed move cannot describe
        # itself as one booking here and two elsewhere.
        payload["active_appointments"] = [
            view.as_dict()
            for view in appointments.active_for_patient(session, requester.patient.id)
        ]
        payload["active_appointment_count"] = len(payload["active_appointments"])
    return payload


def decline(
    session: Session, *, requester: Requester, proposal_id: str, reason: str | None = None
) -> dict[str, Any]:
    proposal = find_by_public_id(session, proposal_id)
    if proposal is None or proposal.conversation_id != requester.conversation_id:
        return _fail("unknown_proposal", "I don't see that proposal anymore.")
    outcome = reject_proposal(session, proposal=proposal, reason=reason)
    if not outcome.ok:
        return _fail(
            outcome.error or "decline_failed", outcome.message or "I couldn't cancel that."
        )
    return _ok(
        proposal_id=proposal_id,
        declined=True,
        message="No problem. I've released that time. Nothing was booked.",
        cards=[resolved_proposal_card(session, proposal)],
    )


def _change_target_is_live(session: Session, proposal: BookingProposal) -> bool:
    """Whether the appointment a reschedule/cancellation refers to is still booked."""
    target_id = proposal.target_appointment_id
    if target_id is None:
        target_id = next(
            (item.target_appointment_id for item in proposal.items if item.target_appointment_id),
            None,
        )
    if target_id is None:
        return False
    target = session.get(Appointment, target_id)
    return (
        target is not None
        and target.status == AppointmentStatus.BOOKED.value
        and target.rescheduled_to_id is None
    )


def _close_stale_change(session: Session, proposal: BookingProposal) -> None:
    """Retire a change proposal whose appointment already moved. Books nothing."""
    try:
        with write_transaction(session):
            fresh = session.get(BookingProposal, proposal.id)
            if fresh is not None and fresh.status == ProposalStatus.PENDING.value:
                release_holds(session, fresh, utcnow())
                fresh.status = ProposalStatus.SUPERSEDED.value
                fresh.resolved_at = utcnow()
    except Exception:  # noqa: BLE001 - closing a stale card is best effort
        logger.warning("Could not close stale proposal %s", proposal.public_id, exc_info=True)
    session.refresh(proposal)


def _outcome_is_persisted(
    session: Session, outcome: Outcome, proposal: BookingProposal
) -> bool:
    """Confirm the write is really in the database before telling the patient so."""
    if outcome.replayed:
        # A replay is the original committed booking coming back.
        return True
    if proposal.kind == "cancellation":
        target = (
            session.get(Appointment, proposal.target_appointment_id)
            if proposal.target_appointment_id
            else None
        )
        return target is not None and target.status == AppointmentStatus.CANCELLED.value

    expected_ids = [row.get("appointment_id") for row in outcome.appointments or []]
    if not expected_ids:
        return False
    wanted = [entry for entry in expected_ids if entry is not None]
    found = {
        row.id: row
        for row in session.scalars(select(Appointment).where(Appointment.id.in_(wanted))).all()
    }
    if len(found) != len(expected_ids):
        return False
    if not all(row.status == AppointmentStatus.BOOKED.value for row in found.values()):
        return False

    if proposal.kind == "reschedule":
        # The replacement alone is not enough: the move only happened if its
        # original is inactive and points back at the replacement, in the same
        # committed state. A replacement without a retired original would leave
        # two active appointments -- exactly the bug this checks for.
        if not outcome.rescheduled:
            return False
        for move in outcome.rescheduled:
            original = session.get(Appointment, move["original_appointment_id"])
            replacement = found.get(move["replacement_appointment_id"])
            if original is None or replacement is None:
                return False
            if original.status not in {
                AppointmentStatus.RESCHEDULED.value,
                AppointmentStatus.CANCELLED.value,
            }:
                return False
            if original.rescheduled_to_id != replacement.id:
                return False
            if replacement.rescheduled_from_id != original.id:
                return False
            if replacement.patient_id != original.patient_id:
                return False
    return True


def _restore_pending(session: Session, proposal: BookingProposal) -> None:
    """Reopen a proposal so the patient can retry after an unverified commit."""
    try:
        proposal.status = ProposalStatus.PENDING.value
        proposal.resolved_at = None
        session.commit()
    except Exception:  # noqa: BLE001 - best effort; the failure is already logged
        session.rollback()
        logger.exception("Could not restore proposal after an unverified commit")


# --------------------------------------------------------------------------- #
# Cards
# --------------------------------------------------------------------------- #


def proposal_card(proposal, *, include_actions: bool = True) -> dict[str, Any]:
    items = [
        {
            "patient_first_name": item.patient_first_name,
            "provider": item.provider_name,
            "provider_credentials": item.provider_credentials,
            "appointment_type": item.appointment_type_name,
            "appointment_type_code": item.appointment_type_code,
            "starts_at": item.proposed_starts_at.isoformat(timespec="minutes")
            if item.proposed_starts_at
            else None,
            "duration_minutes": item.duration_minutes,
            "original_starts_at": item.original_starts_at.isoformat(timespec="minutes")
            if item.original_starts_at
            else None,
        }
        for item in sorted(proposal.items, key=lambda row: row.order_index)
    ]
    return {
        "type": "proposal",
        "proposal_id": proposal.public_id,
        "kind": proposal.kind,
        # Explicit, so a card, a tool result and a confirmation all agree on
        # whether this is a new visit or a move of an existing one.
        "intent": proposal_intent(proposal),
        "status": proposal.status,
        # Present for reschedule/cancellation so the card can name the visit it
        # is about; ordinary bookings leave it empty. Read from the row, then
        # from the items, so a move card always names its original.
        "target_appointment_id": proposal.target_appointment_id
        or next(
            (
                item.target_appointment_id
                for item in sorted(proposal.items, key=lambda row: row.order_index)
                if item.target_appointment_id
            ),
            None,
        ),
        "title": _proposal_title(proposal),
        "items": items,
        "summary": proposal.summary,
        "actions": ["confirm", "change", "cancel"] if include_actions else [],
        "expires_at": (
            proposal.expires_at.isoformat(timespec="seconds") if proposal.expires_at else None
        ),
    }


def _proposal_title(proposal) -> str:
    if proposal.kind == "reschedule":
        # Named as a move, not a booking: the card is the same object either
        # way, so its title has to say which one it is.
        return "Confirm new time for your move"
    if proposal.kind == "cancellation":
        return "Confirm cancellation"
    if len(proposal.items) > 1:
        return "Confirm family booking"
    return "Confirm appointment"


def confirmation_card(session: Session, proposal, outcome: Outcome) -> dict[str, Any]:
    appointments_payload = outcome.appointments
    if proposal.kind == "reschedule":
        # The moved appointment is shown as a move, with the retired row named,
        # so the patient sees one booking -- not the original plus a new one.
        moved = outcome.rescheduled[0] if outcome.rescheduled else {}
        return {
            "type": "confirmation",
            "proposal_id": proposal.public_id,
            "booking_group_id": outcome.booking_group_id,
            "kind": "reschedule",
            "title": "Appointment moved",
            "appointments": appointments_payload,
            "moved_from": {
                "appointment_id": moved.get("original_appointment_id"),
                "starts_at": moved.get("from_starts_at"),
                "ends_at": moved.get("from_ends_at"),
                "status": AppointmentStatus.RESCHEDULED.value,
            },
            "released_appointment_ids": list(outcome.released_appointment_ids),
            "replayed": outcome.replayed,
        }
    return {
        "type": "confirmation",
        "proposal_id": proposal.public_id,
        "booking_group_id": outcome.booking_group_id,
        "kind": proposal.kind,
        "title": "Appointment confirmed",
        "appointments": appointments_payload,
        "released_appointment_ids": list(outcome.released_appointment_ids),
        "replayed": outcome.replayed,
    }


def cancellation_confirmation_card(session: Session, proposal) -> dict[str, Any]:
    from app.models import BookingGroup

    group = (
        session.get(BookingGroup, proposal.booking_group_id)
        if proposal.booking_group_id
        else None
    )
    cancelled = (
        group.details.get("cancelled") if group and isinstance(group.details, dict) else None
    )
    return {
        "type": "cancellation_confirmation",
        "proposal_id": proposal.public_id,
        "title": "Appointment cancelled",
        "cancelled": cancelled,
    }


def live_proposal_cards(session: Session, conversation_id: int) -> list[dict[str, Any]]:
    """The conversation's pending proposals, re-derived from rows that exist now.

    This is the single source for "what is actionable right now". A stale card in
    the transcript is never trusted: the client renders exactly these proposals
    as actionable and treats every other proposal card as history.
    """
    cards: list[dict[str, Any]] = []
    for proposal in pending_for_conversation(session, conversation_id):
        if proposal.kind in {"reschedule", "cancellation"} and not _change_target_is_live(
            session, proposal
        ):
            # The visit this change was about has already moved or been
            # cancelled, so the card must not look actionable. The confirming
            # path closes it when a click arrives; skipping it here means the UI
            # never offers that click in the first place.
            continue
        card = proposal_card(proposal, include_actions=True)
        cards.append(card)
    return cards


def with_live_proposals(
    session: Session, conversation_id: int, cards: list[dict[str, Any]] | None
) -> list[dict[str, Any]]:
    """Append any pending proposal the given cards do not already include.

    Used by the chat turn so a turn that only shows fresh availability still
    returns the still-pending proposal, and ``viewer`` refreshes on the client
    can treat the last message's cards as the authoritative actionable set.
    """
    output = list(cards or [])
    have = {card.get("proposal_id") for card in output if card.get("proposal_id")}
    for card in live_proposal_cards(session, conversation_id):
        if card["proposal_id"] not in have:
            output.append(card)
            have.add(card["proposal_id"])
    return output


def settles_plain_grids(
    session: Session, conversation_id: int
) -> tuple[bool, str | None]:
    """Whether every ordinary grid in this conversation is now context.

    True once the conversation has a booking held or confirmed. A pending hold
    is not a booking, but it is one click from one: leaving the grids clickable
    lets the patient hold a second time for the same visit, which the server
    refuses anyway. The reason is returned so the UI can word it honestly
    ("I'm holding your time" vs "you're booked").

    An explicitly requested extra visit is a different request and is handled by
    the search itself, not here: this only retires grids that were *offers*.
    """
    # `booked_visits` filters by patient, so the conversation's people must be
    # resolved first; a pending hold alone is already conversation-scoped.
    conversation = session.get(Conversation, conversation_id)
    patient_ids: set[int] = set()
    if conversation is not None and conversation.patient_id is not None:
        patient_ids.add(conversation.patient_id)
    for proposal in pending_for_conversation(session, conversation_id):
        patient_ids.update(
            item.patient_id for item in proposal.items if item.patient_id is not None
        )
    patients = (
        list(session.scalars(select(Patient).where(Patient.id.in_(sorted(patient_ids)))))
        if patient_ids
        else []
    )
    scope = booking_policy.booking_scope(
        session, patients=patients, conversation_id=conversation_id
    )
    if scope.has_booked_visit:
        return True, "booked"
    if scope.pending_booking is not None:
        return True, "hold"
    return False, None


def settle_superseded_grids(
    session: Session, conversation_id: int, cards: list[dict[str, Any]] | None
) -> list[dict[str, Any]]:
    """Mark stored slot cards as context once this request is held or booked.

    Cards are persisted with the turn that produced them, so a grid kept its
    ``settled: false`` long after the patient picked a time and moved on: the
    thread then showed old times as clickable while a proposal waited above
    them. Replayed history and newly attached turns both pass through here so
    the displayed state matches SQLite rather than the moment of creation.
    """
    output = [
        dict(card) if isinstance(card, dict) else card for card in (cards or [])
    ]
    settled, reason = settles_plain_grids(session, conversation_id)
    if not settled:
        return output
    for card in output:
        if not isinstance(card, dict) or card.get("type") != "slot":
            continue
        # An extra visit the patient asked for stays bookable; the search marks
        # those itself, so only cards that were plain offers are retired here.
        if card.get("additional_visit"):
            continue
        card["settled"] = True
        # ``require_open`` is the reverse of ``settled`` (see ``slot_card``): a
        # retired grid was not just searched open, so it must not answer the
        # client's availability re-check as if it had been.
        card["require_open"] = False
        if reason is not None:
            card["settled_by"] = reason
    return output


def resolved_proposal_card(session: Session, proposal) -> dict[str, Any]:
    return {
        "type": "proposal",
        "proposal_id": proposal.public_id,
        "kind": proposal.kind,
        "status": proposal.status,
        "title": {
            "rejected": "Cancelled",
            "superseded": "Replaced by a newer time",
            "expired": "Expired",
            "accepted": "Confirmed",
        }.get(proposal.status, "Updated"),
        "items": [],
        "actions": [],
    }


def appointment_cards(views: list[appointments.AppointmentView]) -> dict[str, Any]:
    return {
        "type": "appointments",
        "title": "Your appointments",
        "appointments": [view.as_dict() for view in views],
    }


def handoff_cards(entries: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "type": "handoff",
        "title": "Front desk follow-up",
        "entries": entries,
    }


# --------------------------------------------------------------------------- #
# Family
# --------------------------------------------------------------------------- #


@dataclass
class FamilyMemberInput:
    patient: Patient
    appointment_type_code: str
    provider_name: str | None = None


def check_family_authorisation(
    session: Session, requester: Requester
) -> tuple[list[Patient], dict[str, Any] | None]:
    """Patients this session may book for.

    A verified adult may book for themselves and for *minors* in their household.
    Another adult's records (their appointments, history, insurance) are never
    exposed: the household link alone is not consent.
    """
    from app.models import FamilyMember
    from app.timeutil import clinic_today

    memberships = session.scalars(
        select(FamilyMember).where(FamilyMember.patient_id == requester.patient.id)
    ).all()
    allowed: list[Patient] = []
    children: list[Patient] = []
    today = clinic_today()
    for membership in memberships:
        siblings = session.scalars(
            select(FamilyMember).where(FamilyMember.family_group_id == membership.family_group_id)
        ).all()
        for sibling in siblings:
            other = session.get(Patient, sibling.patient_id)
            if other is None or other.id == requester.patient.id:
                continue
            age = today.year - other.date_of_birth.year - (
                (today.month, today.day) < (other.date_of_birth.month, other.date_of_birth.day)
            )
            if age < 18:
                children.append(other)
                allowed.append(other)
    return allowed, None


def _hold_family_group_id(session: Session, requester: Requester) -> int | None:
    """The household a family hold belongs to, if the patient has one.

    Returns ``None`` rather than guessing when there is no household link:
    family semantics only apply to members who are actually linked.
    """
    membership = session.scalars(
        select(FamilyMember).where(FamilyMember.patient_id == requester.patient.id)
    ).first()
    return membership.family_group_id if membership is not None else None


def family_search(
    session: Session,
    *,
    requester: Requester,
    members: list[FamilyMemberInput],
    date_phrases: list[str] | None = None,
    start_date: str | None = None,
    time_windows: list[str] | None = None,
    range_days: int | None = None,
    back_to_back: bool = True,
) -> dict[str, Any]:
    """Find complete same-day family plans with nearby appointment times.

    Read-only. Each card carries one time per person, so the number of times on
    a card is exactly how many visits one confirmation would book.
    """
    del back_to_back  # retained for tool compatibility; gaps are now allowed
    requests, types, problem = _family_requests(session, requester, members)
    if problem is not None or requests is None or types is None:
        return problem or _fail("invalid_family", "I couldn't put that family together.")

    preferences = date_parsing.resolve_preferences(
        start_date=start_date,
        date_phrases=date_phrases,
        time_windows=time_windows,
        range_days=range_days,
    )
    if preferences.ambiguous:
        return _ok(ambiguous=True, message=preferences.clarification or "Which date did you mean?")
    if preferences.past_rejected and not preferences.days:
        return _ok(message="That date has already passed. Which upcoming day works?")

    plans = search_nearby_family_plans(
        session, members=requests, preferences=preferences, max_plans=6
    )
    if not plans:
        return _ok(
            available=False,
            people=[member.patient_first_name for member in requests],
            message=(
                "I couldn't find currently open times for everyone on those days. I can check "
                "other days, or book you separately if you'd rather."
            ),
        )
    plans = plans[:3]
    _FAMILY_PLAN_CACHE[_family_cache_key(requests)] = plans
    return _ok(
        available=True,
        plan_count=len(plans),
        slots_per_option=len(requests),
        people=[member.patient_first_name for member in requests],
        message=(
            "Here are complete options with everyone's currently open times, ordered as close "
            "together as possible. Confirming holds the whole group; it's booked only after you "
            "confirm."
        ),
        cards=family_option_cards(plans=plans, appointment_types=types),
    )


def family_options_after_conflict(
    session: Session, *, requester: Requester, proposal: BookingProposal
) -> list[dict[str, Any]]:
    """Find a fresh complete family run after a held run became stale."""
    if proposal.kind != "booking" or len(proposal.items) < 2:
        return []

    items = sorted(proposal.items, key=lambda item: item.order_index)
    starts = [item.proposed_starts_at for item in items if item.proposed_starts_at is not None]
    if not starts:
        return []

    members: list[FamilyMemberInput] = []
    for item in items:
        if item.patient_id is None or not item.appointment_type_code:
            return []
        patient = session.get(Patient, item.patient_id)
        if patient is None:
            return []
        members.append(
            FamilyMemberInput(
                patient=patient,
                appointment_type_code=item.appointment_type_code,
            )
        )

    # Keep the requested calendar day while finding another complete block on
    # that day if the original run was taken.
    refreshed = family_search(
        session,
        requester=requester,
        members=members,
        start_date=min(starts).date().isoformat(),
        range_days=1,
    )
    return [card for card in refreshed.get("cards", []) if isinstance(card, dict)]


def _family_requests(
    session: Session, requester: Requester, members: list[FamilyMemberInput]
) -> tuple[list[FamilyMemberRequest] | None, dict[int, AppointmentType] | None, dict | None]:
    """Resolve and authorise the members of a family request."""
    if not members:
        return None, None, _fail("no_members", "Who would you like to book for?")
    if len(members) > MAX_FAMILY_MEMBERS:
        return (
            None,
            None,
            _fail(
                "too_many_members",
                f"I can coordinate up to {MAX_FAMILY_MEMBERS} people at once.",
            ),
        )

    allowed, problem = check_family_authorisation(session, requester)
    if problem is not None:
        return None, None, problem
    permitted_ids = {requester.patient.id, *(patient.id for patient in allowed)}

    requests: list[FamilyMemberRequest] = []
    types: dict[int, AppointmentType] = {}
    for member in members:
        if member.patient.id not in permitted_ids:
            return (
                None,
                None,
                _fail(
                    "not_authorized",
                    (
                        "I can only book for you and the children on your account. "
                        "Another adult needs to contact us themselves."
                    ),
                ),
            )
        appointment_type, type_problem = _type_or_error(session, member.appointment_type_code)
        if appointment_type is None or type_problem is not None:
            return None, None, type_problem or _fail("unknown_type", "Unknown visit type.")
        if member.patient.id in types:
            return (
                None,
                None,
                _fail(
                    "duplicate_member",
                    "Each person needs their own appointment in a family booking.",
                ),
            )
        types[member.patient.id] = appointment_type
        requests.append(
            FamilyMemberRequest(
                patient_id=member.patient.id,
                patient_first_name=_first_name(member.patient),
                appointment_type=appointment_type,
                provider_name=member.provider_name,
            )
        )
    return requests, types, None


def hold_family_option(
    session: Session,
    *,
    requester: Requester,
    members: list[FamilyMemberInput],
    option_index: int,
    start_index: int | None = None,
    date_phrases: list[str] | None = None,
    start_date: str | None = None,
    time_windows: list[str] | None = None,
    range_days: int | None = None,
    summary: str | None = None,
    option_identity: str | None = None,
    from_ui: bool = False,
) -> dict[str, Any]:
    """Hold one family option as a single combined proposal. Books nothing.

    The option is re-derived from SQLite: the indexed plan from the same search,
    or, when a card is clicked after a restart, the plan whose start times match
    the card the patient saw (``option_identity``). Every run is re-checked for
    bookability, so a stale card can only fail, never book a taken time.
    """
    requests, types, problem = _family_requests(session, requester, members)
    if problem is not None or requests is None or types is None:
        return problem or _fail("invalid_family", "I couldn't put that family together.")

    scope = booking_policy.booking_scope(
        session, patients=[member.patient for member in members], conversation_id=requester.conversation_id
    )
    if scope.has_booked_visit:
        return _fail(
            "already_booked",
            booking_policy.already_booked_message(scope),
            already_booked=True,
            next_step=(
                "This family booking is already booked. Do not hold another. To move a visit, "
                "use reschedule_appointment with its appointment_id."
            ),
        )

    preferences = date_parsing.resolve_preferences(
        start_date=start_date,
        date_phrases=date_phrases,
        time_windows=time_windows,
        range_days=range_days,
    )
    if preferences.ambiguous:
        return _ok(ambiguous=True, message=preferences.clarification or "Which date did you mean?")

    resolved = _family_plan_for_card(
        session,
        requests=requests,
        types=types,
        preferences=preferences,
        option_index=option_index,
        start_index=start_index,
        option_identity=option_identity,
    )
    if resolved is None:
        return _fail(
            "option_unavailable",
            "That family option is no longer available. I can search again.",
            cards=[],
        )

    # A family option is one record for the party. This search's booking is
    # replaced, never the group's pending card: if one is already held, the
    # guard below hands it back so the patient confirms it instead of the same
    # family being held twice.
    supersede_pending(session, conversation_id=requester.conversation_id, kinds=("booking",))
    drafts = [
        DraftItem(
            patient_id=option.patient_id or requester.patient.id,
            patient_first_name=option.patient_first_name
            or _first_name(requester.patient),
            appointment_type=appointment_type,
            option=option,
        )
        for option, appointment_type in resolved
    ]
    scope = booking_policy.booking_scope(
        session,
        patients=[member.patient for member in members],
        conversation_id=requester.conversation_id,
    )
    refusal = booking_policy.refusal_for_hold(
        session,
        scope=scope,
        intervals=[
            booking_policy.HeldInterval(
                patient_id=option.patient_id or requester.patient.id,
                starts_at=option.starts_at,
                duration_minutes=appointment_type.duration_minutes,
            )
            for option, appointment_type in resolved
        ],
        summary=summary,
    )
    if refusal is not None:
        if refusal.get("error") == "pending_booking_exists":
            # Show the card that is already waiting, not a second family hold.
            refusal["cards"] = [
                card
                for card in live_proposal_cards(session, requester.conversation_id)
                if card.get("proposal_id") == refusal.get("pending_proposal_id")
            ]
        return refusal
    outcome = create_proposal(
        session,
        conversation_id=requester.conversation_id,
        items=drafts,
        kind="booking",
        intent="new_booking",
        summary=summary,
        family_group_id=_hold_family_group_id(session, requester),
        from_ui=from_ui,
        visit_shape="household",
    )
    if not outcome.ok:
        return _fail(
            outcome.error or "proposal_failed",
            outcome.message or "I couldn't hold those times.",
            conflicts=outcome.conflicts,
        )
    if outcome.replayed:
        supersede_pending(
            session,
            conversation_id=requester.conversation_id,
            kinds=("booking",),
            keep_public_id=outcome.proposal_id,
        )
    session.commit()

    proposal = find_by_public_id(session, outcome.proposal_id or "")
    if proposal is None:  # pragma: no cover - the row was just committed
        return _fail("proposal_failed", "I couldn't hold those times.")
    card = proposal_card(proposal, include_actions=True)
    card["type"] = "family_proposal"
    card["title"] = "Confirm family booking"
    return _ok(
        awaiting_confirmation=True,
        family=True,
        kind="booking",
        intent="new_booking",
        proposal_id=proposal.public_id,
        proposal_ids=[proposal.public_id],
        slot_count=len(proposal.items),
        message=(
            "I've held nearby times for the whole family. Confirming books everyone "
            "together. If any time is taken, nothing is booked."
        ),
        next_step=(
            "Exactly one card covers everyone. Ask the patient to confirm it with "
            "confirm_booking_proposal. Do not hold another option as well."
        ),
        cards=[card],
    )


def _family_plan_for_card(
    session: Session,
    *,
    requests: list[FamilyMemberRequest],
    types: dict[int, AppointmentType],
    preferences,
    option_index: int,
    start_index: int | None,
    option_identity: str | None,
) -> list[tuple[SlotOption, AppointmentType]] | None:
    plans = _FAMILY_PLAN_CACHE.get(_family_cache_key(requests)) or []
    plan = None
    if option_identity:
        plan = next(
            (
                candidate
                for candidate in plans
                if family_plan_identity(candidate) == option_identity
            ),
            None,
        )
        if plan is None:
            plan = _decode_family_identity(session, option_identity, types)
    if plan is None and options_card_fallback(plans, option_index, start_index):
        plan = plans[start_index + option_index]
    if plan is None:
        # The cache is gone (a restart, or a chattier turn): recompute and match
        # the card's start times so the click still means the option it showed.
        recomputed = search_nearby_family_plans(
            session, members=requests, preferences=preferences, max_plans=6
        )
        if option_identity:
            plan = next(
                (
                    candidate
                    for candidate in recomputed
                    if family_plan_identity(candidate) == option_identity
                ),
                None,
            )
        if plan is None and 0 <= option_index < len(recomputed) and not option_identity:
            plan = recomputed[option_index]
        if plan is None:
            return None
        _FAMILY_PLAN_CACHE[_family_cache_key(requests)] = recomputed

    resolved: list[tuple[SlotOption, AppointmentType]] = []
    for option in plan:
        appointment_type = types.get(option.patient_id or 0)
        if appointment_type is None:
            return None
        rows = is_interval_bookable(
            session,
            provider_id=option.provider_id,
            starts_at=option.starts_at,
            duration_minutes=appointment_type.duration_minutes,
            appointment_type=appointment_type,
        )
        if rows is None:
            return None
        resolved.append(
            (
                SlotOption(
                    slot_ids=[row.id for row in rows],
                    stable_id=option.stable_id,
                    provider_id=option.provider_id,
                    provider_name=option.provider_name,
                    provider_credentials=option.provider_credentials,
                    provider_specialties=option.provider_specialties,
                    appointment_type_code=appointment_type.code,
                    appointment_type_name=appointment_type.name,
                    starts_at=rows[0].starts_at,
                    ends_at=rows[-1].ends_at,
                    duration_minutes=appointment_type.duration_minutes,
                    patient_id=option.patient_id,
                    patient_first_name=option.patient_first_name,
                ),
                appointment_type,
            )
        )
    if len({option.stable_id for option, _ in resolved}) != len(resolved):
        return None
    return resolved


def options_card_fallback(
    plans: list[list[SlotOption]], option_index: int, start_index: int | None
) -> bool:
    index = (start_index or 0) + option_index
    return start_index is not None and 0 <= index < len(plans)


def family_plan_identity(plan: list[SlotOption]) -> str:
    """The option's slots in order, each tagged with the person it is for.

    This is what a family card posts back. It names people as well as times, so
    a click still means "book these two together" after a restart cleared the
    in-process plan cache.
    """
    return FAMILY_IDENTITY_PREFIX + ",".join(
        f"{option.patient_id or 0}:{option.stable_id}" for option in plan
    )


def _decode_family_identity(
    session: Session, identity: str, types: dict[int, AppointmentType]
) -> list[SlotOption] | None:
    """Rebuild an option from the clicked card's own slot ids.

    The clicked grid is the patient's intent, but nothing is trusted from it:
    each stable id is decoded and checked, and the person is only accepted when
    the run's type is one this family booking already includes.
    """
    if not identity.startswith(FAMILY_IDENTITY_PREFIX):
        return None
    plan: list[SlotOption] = []
    for part in identity[len(FAMILY_IDENTITY_PREFIX) :].split(","):
        if not part:
            continue
        try:
            patient_id, stable_id = part.split(":", 1)
            patient_key = int(patient_id)
        except ValueError:
            return None
        appointment_type = types.get(patient_key)
        if appointment_type is None:
            return None
        option = _option_from_stable_id(session, appointment_type, stable_id)
        if option is None:
            return None
        option.patient_id = patient_key
        patient = session.get(Patient, patient_key)
        option.patient_first_name = _first_name(patient) if patient else None
        plan.append(option)
    if len(plan) < 2:
        return None
    return plan


# Recently computed family plans. Keyed by the group's exact start times and
# their order, so one thread's plan can never be served for another (the plan is
# always re-checked for bookability before it is held; entries are replaced on
# each new search).
_FAMILY_PLAN_CACHE: dict[str, list[list[SlotOption]]] = {}
FAMILY_IDENTITY_PREFIX = "family|"


def _family_cache_key(members: list[FamilyMemberRequest]) -> str:
    """What the family request is, so only a matching search can look it up."""
    return "|".join(
        f"{member.patient_id}:{member.appointment_type.code}:{(member.provider_name or '').lower()}"
        for member in members
    )


__all__ = [
    "AvailabilityAnswer",
    "FAMILY_IDENTITY_PREFIX",
    "FamilyMemberInput",
    "Requester",
    "appointment_cards",
    "cancellation_confirmation_card",
    "check_family_authorisation",
    "confirm",
    "confirm_booking",
    "confirm_change",
    "confirmation_card",
    "decline",
    "family_option_cards",
    "family_plan_identity",
    "family_search",
    "find_availability",
    "find_replacement_availability",
    "handoff_cards",
    "hold_family_option",
    "live_proposal_cards",
    "options_for_slots",
    "pending_target",
    "proposal_card",
    "propose_booking",
    "propose_reschedule",
    "replacement_cards",
    "replacement_for_slot",
    "resolved_proposal_card",
    "slot_card",
    "with_live_proposals",
]
