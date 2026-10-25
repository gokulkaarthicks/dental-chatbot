"""Offline smoke check for the Phase 3 scheduling engine.

Runs against a throwaway SQLite file in the OS temp directory. No network, no
LLM, no live integration: it exercises the deterministic Python/SQLite layer
that the agent tools and the UI are thin wrappers around.

    cd backend && python scripts/smoke_phase3.py
"""

from __future__ import annotations

import sys
import tempfile
from datetime import timedelta
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="dental-smoke-"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app.config as config  # noqa: E402

_original = config.get_settings()
config.get_settings = lambda: config.Settings(  # type: ignore[assignment]
    deepseek_api_key="",
    deepseek_model=_original.deepseek_model,
    deepseek_base_url=_original.deepseek_base_url,
    database_path=TMP / "smoke.db",
)

from sqlalchemy import select, update  # noqa: E402

from app.bootstrap import create_schema  # noqa: E402
from app.clinic import CLINIC  # noqa: E402
from app.db import new_session  # noqa: E402
from app.models import (  # noqa: E402
    Appointment,
    FamilyGroup,
    FamilyMember,
    Patient,
    PatientSession,
    SlotClaim,
)
from app.seed_fixtures import anchor_date  # noqa: E402
from app.services import emergency, patient_sessions, scheduling  # noqa: E402
from app.services.conversations import create_conversation  # noqa: E402
from app.services.seed import seed_database  # noqa: E402
from app.timeutil import clinic_today  # noqa: E402

create_schema()
seed_database()

session = new_session()
patient = session.query(Patient).order_by(Patient.id).first()
print("clinic:", CLINIC.name)
print("patient:", patient.full_name)

issued = patient_sessions.create_patient_session(session)
p_session = session.get(PatientSession, issued.session_id)
patient_sessions.mark_verified(session, p_session, patient, method="smoke")
conversation = create_conversation(
    session, patient_session=p_session, generation=issued.reset_generation
)
session.commit()
requester = scheduling.Requester(
    patient=patient, patient_session=p_session, conversation=conversation
)


def _proposal_row_id(session, public_id: str) -> int:
    from app.services.proposals import find_by_public_id

    row = find_by_public_id(session, public_id)
    return row.id if row is not None else 0


def _new_requester(session, target, *, label: str):
    """A second caller: its own session and conversation, as a browser would have."""
    issued = patient_sessions.create_patient_session(session)
    record = session.get(PatientSession, issued.session_id)
    patient_sessions.mark_verified(session, record, target, method="smoke")
    thread = create_conversation(session, patient_session=record, generation=issued.reset_generation)
    session.commit()
    print(f"   ({label} session {record.id}, conversation {thread.id})")
    return scheduling.Requester(patient=target, patient_session=record, conversation=thread)

print("\n-- search: within the seeded horizon")
start_day = anchor_date(clinic_today())
answer = scheduling.find_availability(
    session,
    patient=patient,
    appointment_type_code="checkup",
    start_date=(start_day + timedelta(days=1)).isoformat(),
)
print("   ok:", answer.payload["ok"], "| available:", answer.payload.get("available"),
      "| slots:", answer.payload.get("slot_count"))
for card in answer.payload.get("cards", [])[:4]:
    print("   ", card["day_label"], card["time_range"], "with", card["provider"],
          f"({card['duration_minutes']}m)", card["slot_id"])

if not answer.options:
    raise SystemExit("no availability seeded for tomorrow; stopping here")

chosen = answer.options[0]
print("\n-- select a real slot -> awaiting-confirmation proposal")
proposal = scheduling.propose_booking(
    session, requester=requester, appointment_type=answer.appointment_type, options=[chosen]
)
print("   ok:", proposal["ok"], "| awaiting:", proposal.get("awaiting_confirmation"),
      "| id:", proposal.get("proposal_id"), "| error:", proposal.get("error"),
      proposal.get("message") if not proposal["ok"] else "")
if not proposal["ok"]:
    raise SystemExit("proposal failed")
print("   appointments booked by selecting alone:", session.query(Appointment).count())

again = scheduling.find_availability(
    session,
    patient=patient,
    appointment_type_code="checkup",
    start_date=(start_day + timedelta(days=1)).isoformat(),
)
print("   held slot withheld from a fresh search:",
      all(o.stable_id != chosen.stable_id for o in again.options))

print("\n-- confirm")
first = scheduling.confirm(session, requester=requester, proposal_id=proposal["proposal_id"])
print("   ok:", first["ok"], "| card:", first["cards"][0]["type"] if first["cards"] else None)
print("   appointments now:", session.query(Appointment).count())

second = scheduling.confirm(session, requester=requester, proposal_id=proposal["proposal_id"])
print("   repeat confirm -> ok:", second["ok"], "| replayed:", second.get("replayed"))
print("   appointments after repeat:", session.query(Appointment).count())

after = scheduling.find_availability(
    session,
    patient=patient,
    appointment_type_code="checkup",
    start_date=(start_day + timedelta(days=1)).isoformat(),
)
print("   confirmed slot no longer offered:",
      all(o.stable_id != chosen.stable_id for o in after.options))

print("\n-- patient appointments")
from app.services import appointments as appts  # noqa: E402

views = appts.upcoming_for_patient(session, patient.id)
card = scheduling.appointment_cards(views)
print("   count:", len(card["appointments"]),
      "| first:", card["appointments"][0]["starts_at"] if views else None)

print("\n-- emergency")
triage = emergency.triage(
    session, conversation_id=conversation.id, patient_id=patient.id,
    description="Severe pain and my face is swelling badly",
)
print("   life threatening:", triage.life_threatening, "| symptoms:", triage.symptoms)
print("   alert record:", triage.alert.record_id if triage.alert else None,
      "| created:", triage.alert.created if triage.alert else None,
      "| staff notified:", triage.alert.staff_notified if triage.alert else None)
repeat = emergency.triage(
    session, conversation_id=conversation.id, patient_id=patient.id,
    description="Severe pain and my face is swelling badly",
)
print("   repeat report reuses alert:",
      bool(repeat.alert) and repeat.alert.record_id == (triage.alert.record_id if triage.alert else None),
      "| report count:", repeat.alert.report_count if repeat.alert else None)
airway = emergency.triage(
    session, conversation_id=conversation.id, patient_id=patient.id,
    description="My throat is closing and I can't breathe",
)
print("   airway -> 911:", airway.life_threatening, "| no clinic alert:", airway.alert is None)

print("\n-- worsening symptoms update the same alert")
worse = emergency.triage(
    session, conversation_id=conversation.id, patient_id=patient.id,
    description="Severe pain, face is swelling badly, and now I have a fever and trouble swallowing",
)
print("   same record:", worse.alert.record_id == repeat.alert.record_id,
      "| count:", worse.alert.report_count, "| severity:", worse.alert.severity)

print("\n-- two callers race for the same slot")
race = scheduling.find_availability(
    session,
    patient=patient,
    appointment_type_code="checkup",
    start_date=(start_day + timedelta(days=2)).isoformat(),
)
if race.options:
    contended = race.options[0]
    rival_requester = _new_requester(session, patient, label="rival")
    # Search results are only a snapshot; both callers saw the same free time.
    a = scheduling.propose_booking(
        session, requester=requester, appointment_type=race.appointment_type, options=[contended]
    )
    b = scheduling.propose_booking(
        session, requester=rival_requester, appointment_type=race.appointment_type,
        options=[contended],
    )
    print("   first hold ok:", a["ok"], "|", a.get("error") or "held")
    print("   rival hold ok:", b["ok"], "|", b.get("error"), "-", (b.get("message") or "")[:60])
    confirm_a = scheduling.confirm(session, requester=requester, proposal_id=a["proposal_id"])
    print("   confirm winner ok:", confirm_a["ok"])

print("\n-- family run")
family = session.scalar(
    select(FamilyGroup).join(FamilyMember, FamilyMember.family_group_id == FamilyGroup.id)
    .where(FamilyMember.patient_id == patient.id)
)
if family is None:
    print("   seeded patient has no household; skipping")
else:
    members = [(m.relation_to_primary, m.patient) for m in family.membership_rows]
    for relation, member in members:
        age = clinic_today().year - member.date_of_birth.year
        print(f"   {member.full_name} ({relation}, ~{age}y)")
    children = [
        member
        for _relation, member in members
        if member.id != patient.id and member.date_of_birth.year > clinic_today().year - 18
    ]
    if not children:
        print("   adult-only household: no minors to book for")
    else:
        day = (start_day + timedelta(days=3)).isoformat()
        inputs = [
            scheduling.FamilyMemberInput(patient=patient, appointment_type_code="checkup"),
            *(
                scheduling.FamilyMemberInput(patient=child, appointment_type_code="cleaning")
                for child in children[:1]
            ),
        ]
        search = scheduling.propose_family_booking(
            session, requester=requester, members=inputs, start_date=day
        )
        print("   family search ok:", search["ok"], "| available:", search.get("available"),
              "| plans:", search.get("plan_count"))
        if search.get("available"):
            plan = scheduling.family_options_for_index(
                session, requester=requester, members=inputs, option_index=0, start_date=day
            )
            if plan:
                for option, appointment_type in plan:
                    print("     ", option.patient_first_name, option.starts_at.strftime("%a %-I:%M %p"),
                          appointment_type.name, f"({appointment_type.duration_minutes}m)")
                from app.services.proposals import DraftItem, create_proposal  # noqa: E402

                drafts = [
                    DraftItem(
                        patient_id=option.patient_id or patient.id,
                        patient_first_name=option.patient_first_name or "Patient",
                        appointment_type=appointment_type,
                        option=option,
                    )
                    for option, appointment_type in plan
                ]
                grouped = create_proposal(
                    session,
                    conversation_id=conversation.id,
                    items=drafts,
                    family_group_id=family.id,
                )
                print("   combined proposal ok:", grouped.ok, "| id:", grouped.proposal_id)
                target = grouped.proposal_id or ""
                booked_before = session.query(Appointment).count()
                done = scheduling.confirm(session, requester=requester, proposal_id=target)
                print("   family confirm ok:", done["ok"], "| appointments added:",
                      session.query(Appointment).count() - booked_before)
                for entry in done.get("appointments", []):
                    print("     ", entry["patient_name"], entry["starts_at"],
                          entry["appointment_type"])

                print("\n-- family rollback: one member's time is taken before confirming")
                day2 = (start_day + timedelta(days=4)).isoformat()
                search2 = scheduling.propose_family_booking(
                    session, requester=requester, members=inputs, start_date=day2
                )
                plan2 = scheduling.family_options_for_index(
                    session, requester=requester, members=inputs, option_index=0, start_date=day2
                ) if search2.get("available") else None
                if not plan2:
                    print("   no second family plan; skipping")
                else:
                    drafts2 = [
                        DraftItem(
                            patient_id=option.patient_id or patient.id,
                            patient_first_name=option.patient_first_name or "Patient",
                            appointment_type=appointment_type,
                            option=option,
                        )
                        for option, appointment_type in plan2
                    ]
                    grouped2 = create_proposal(
                        session, conversation_id=conversation.id, items=drafts2,
                        family_group_id=family.id,
                    )
                    # Steal ONLY the last member's interval, leaving our proposal
                    # otherwise intact: point its claims at another appointment so
                    # it is genuinely booked, not merely held.
                    intruder = session.query(Appointment).order_by(Appointment.id).first()
                    session.execute(
                        update(SlotClaim)
                        .where(
                            SlotClaim.held_by_proposal_id
                            == _proposal_row_id(session, grouped2.proposal_id or ""),
                            SlotClaim.active.is_(True),
                        )
                        .values(appointment_id=intruder.id, held_by_proposal_id=None)
                    )
                    session.commit()
                    before = session.query(Appointment).count()
                    rolled_back = scheduling.confirm(
                        session, requester=requester, proposal_id=grouped2.proposal_id or ""
                    )
                    print("   family confirm ok:", rolled_back["ok"], "| error:",
                          rolled_back.get("error"))
                    print("   appointments added (must be 0):",
                          session.query(Appointment).count() - before)

session.close()
print("\nsmoke OK")
