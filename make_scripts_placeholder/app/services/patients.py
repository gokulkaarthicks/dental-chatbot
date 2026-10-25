"""Patient identity, verification, registration, and profile updates.

These are the only code paths allowed to set a session's verified patient. The
conversation agent reaches them through validated tools, so the model can
*request* verification but can never assert it.

Privacy rules implemented here (not merely prompted):
* ``find_registration_duplicates`` and ``register_patient`` return only counts
  and neutral hints -- never names, DOBs, or partial record contents.
* A patient's details are returned only once the session is verified for them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.clinic import SELF_PAY_LABEL
from app.models import Patient, utcnow
from app.services import audit, generations
from app.timeutil import clinic_today
from app.validation import (
    normalize_insurance,
    normalize_name,
    normalize_phone,
    parse_date_of_birth,
    phone_digits,
)

REGISTRATION_FIELDS = ("full_name", "phone", "date_of_birth", "insurance_provider")

# What gets written to the chart when a full name cannot be read as
# "given family". Nothing is assumed about which word is the given name.
UNSPLIT_NAME = "Patient"


def first_name(full_name: str | None) -> str:
    """The name to greet someone by, or "" when the chart does not carry one.

    Splitting on the first space is only right when there is a second word to
    split off: "Dharshs" was greeted as "Dharsh" by a plain ``split(" ")[0]``,
    which reads as the clinic mishearing the patient's own name. A single word
    is the whole name, so it is either usable as written or not used at all.
    """
    parts = (full_name or "").split()
    if len(parts) >= 2:
        return parts[0]
    word = parts[0] if parts else ""
    return "" if _placeholder_name(word) else word


def _placeholder_name(word: str) -> bool:
    return word.strip(".,'").lower() in {"", "patient", "unknown", "test", "guest", "n/a"}



@dataclass
class FieldIssues:
    errors: dict[str, str] = field(default_factory=dict)

    def add(self, name: str, message: str) -> None:
        self.errors.setdefault(name, message)

    @property
    def ok(self) -> bool:
        return not self.errors


@dataclass
class RegistrationDraft:
    """Normalized values plus any per-field problems."""

    values: dict[str, str | date] = field(default_factory=dict)
    issues: FieldIssues = field(default_factory=FieldIssues)
    date_ambiguous: bool = False


def normalize_registration_fields(
    *,
    full_name: str | None,
    phone: str | None,
    date_of_birth: str | None,
    insurance_provider: str | None,
    required: tuple[str, ...] = REGISTRATION_FIELDS,
) -> RegistrationDraft:
    draft = RegistrationDraft()
    if date_of_birth is not None:
        parsed_dob = parse_date_of_birth(date_of_birth, today=clinic_today())
        draft.date_ambiguous = parsed_dob.ambiguous

    if full_name is not None:
        name = normalize_name(full_name)
        if name.ok and name.value:
            draft.values["full_name"] = name.value
        else:
            draft.issues.add("full_name", name.error or "Could you give the full name?")
    elif "full_name" in required:
        draft.issues.add("full_name", "I still need the patient's full name.")

    if phone is not None:
        parsed_phone = normalize_phone(phone)
        if parsed_phone.ok and parsed_phone.e164:
            draft.values["phone"] = parsed_phone.e164
        else:
            draft.issues.add(
                "phone", parsed_phone.error or "That phone number doesn't look right."
            )
    elif "phone" in required:
        draft.issues.add("phone", "I still need a phone number.")

    if date_of_birth is not None:
        parsed_dob = parse_date_of_birth(date_of_birth, today=clinic_today())
        if parsed_dob.ok and parsed_dob.value:
            draft.values["date_of_birth"] = parsed_dob.value
        else:
            draft.issues.add(
                "date_of_birth", parsed_dob.error or "Could you check the date of birth?"
            )
    elif "date_of_birth" in required:
        draft.issues.add("date_of_birth", "I still need the date of birth.")

    if insurance_provider is not None:
        insurance = normalize_insurance(insurance_provider)
        if insurance.ok and insurance.value:
            draft.values["insurance_provider"] = insurance.value
        else:
            draft.issues.add(
                "insurance_provider", insurance.error or "Which insurance plan, or self-pay?"
            )
    elif "insurance_provider" in required:
        draft.issues.add(
            "insurance_provider", "What insurance will you be using? You can also say self-pay."
        )

    return draft


def _patients_with_phone(session: Session, phone: str) -> list[Patient]:
    """Match on the last 10 digits so stored formatting never matters.

    ``patients.phone`` is deliberately non-unique (families share numbers), so
    this returns a list.
    """
    target = phone_digits(phone)
    if not target:
        return []
    digits_only = func.replace(
        func.replace(func.replace(func.replace(Patient.phone, "+", ""), "-", ""), " ", ""), "(", ""
    )
    candidates = session.scalars(
        select(Patient).where(
            func.replace(func.replace(digits_only, ")", ""), ".", "").like(f"%{target}")
        )
    ).all()
    return [person for person in candidates if phone_digits(person.phone) == target]


def find_duplicate_summary(session: Session, phone: str, full_name: str) -> dict:
    """Neutral duplicate information. Counts only -- never record contents."""
    same_phone = _patients_with_phone(session, phone)
    if not same_phone:
        return {"match_count": 0, "same_name": False, "likely_household": False}

    first = (full_name.split(" ")[0] if full_name else "").lower()
    same_name = any(person.full_name.lower() == full_name.lower() for person in same_phone)
    same_first_name = (
        any(person.full_name.lower().startswith(first + " ") for person in same_phone)
        if first
        else False
    )

    return {
        "match_count": len(same_phone),
        "same_name": same_name,
        "same_first_name": same_first_name,
        "likely_household": len(same_phone) > 0 and not same_name,
    }


def verify_patient(session: Session, *, phone: str, date_of_birth: str) -> dict:
    """Match an existing chart by phone + DOB. Both must agree.

    Returns a neutral failure on mismatch -- no hint about which part was wrong,
    so this cannot be used to probe for a phone number's existence.
    """
    phone_result = normalize_phone(phone)
    dob_result = parse_date_of_birth(date_of_birth, today=clinic_today())
    if not phone_result.ok or not phone_result.e164:
        return {"verified": False, "reason": "phone_invalid", "message": phone_result.error}
    if not dob_result.ok or dob_result.value is None:
        return {"verified": False, "reason": "date_invalid", "message": dob_result.error}

    matches = [
        person
        for person in _patients_with_phone(session, phone_result.e164)
        if person.date_of_birth == dob_result.value
    ]

    if len(matches) == 1:
        patient = matches[0]
        return {
            "verified": True,
            "patient_id": patient.id,
            "first_name": first_name(patient.full_name),
            "message": "Verified. You can share this patient's details.",
        }

    if len(matches) > 1:
        # Two charts share a phone and DOB (e.g. twins with a typo upstream).
        # Never guess; route to the front desk.
        return {
            "verified": False,
            "reason": "ambiguous_match",
            "message": (
                "More than one chart matches that. Our front desk will need to sort out "
                "which record is yours."
            ),
        }

    return {
        "verified": False,
        "reason": "no_match",
        "message": (
            "I couldn't match that phone number and date of birth. Could you double-check "
            "both? If you're new, I can register you instead."
        ),
    }


def registration_completeness(patient: Patient) -> dict[str, bool]:
    return {
        "full_name": bool(patient.full_name),
        "phone": bool(patient.phone),
        "date_of_birth": bool(patient.date_of_birth),
        "insurance_provider": bool(patient.insurance_provider),
    }


def patient_profile(patient: Patient) -> dict:
    """Detail payload. Callers must have already established verification."""
    completeness = registration_completeness(patient)
    return {
        "patient_id": patient.id,
        "full_name": patient.full_name,
        "first_name": first_name(patient.full_name),
        "phone": patient.phone,
        "date_of_birth": patient.date_of_birth.isoformat(),
        "insurance_provider": patient.insurance_provider or SELF_PAY_LABEL,
        "is_self_pay": (patient.insurance_provider or "") == SELF_PAY_LABEL,
        "registration_complete": all(completeness.values()),
        "missing_fields": [name for name, ok in completeness.items() if not ok],
    }


def register_patient(
    session: Session,
    *,
    full_name: str,
    phone: str,
    date_of_birth: str,
    insurance_provider: str,
    allow_duplicate_phone: bool = True,
) -> dict:
    """Create a new chart. Assumes fields were already validated/normalized."""
    duplicates = find_duplicate_summary(session, phone, full_name)
    if duplicates["same_name"]:
        return {
            "created": False,
            "reason": "possible_duplicate",
            "message": (
                "There's already a chart with that name and phone number. If that's you, "
                "I can verify it instead, or tell me if this is a different person."
            ),
        }

    patient = Patient(
        full_name=full_name,
        phone=phone,
        date_of_birth=date.fromisoformat(date_of_birth)
        if isinstance(date_of_birth, str)
        else date_of_birth,
        insurance_provider=insurance_provider,
        insurance_member_id=None,
        notes=None,
    )
    session.add(patient)
    session.flush()
    generations.increment_patient_registration(session)
    audit.record(
        session,
        action="patient.registered",
        actor_type="patient_session",
        entity_type="patient",
        entity_id=patient.id,
        details={"shared_phone": duplicates["match_count"] > 0},
    )
    return {
        "created": True,
        "patient_id": patient.id,
        "first_name": first_name(patient.full_name),
        "message": "Registered. You can now ask about appointment times.",
    }


def update_patient(
    session: Session,
    patient: Patient,
    *,
    full_name: str | None = None,
    phone: str | None = None,
    date_of_birth: str | None = None,
    insurance_provider: str | None = None,
) -> dict:
    """Apply corrections to an existing chart.

    Each field is validated independently, so a wrong phone does not block a
    corrected spelling of the name.
    """
    draft = normalize_registration_fields(
        full_name=full_name,
        phone=phone,
        date_of_birth=date_of_birth,
        insurance_provider=insurance_provider,
        required=(),
    )
    if not draft.issues.ok:
        return {"updated": False, "field_errors": draft.issues.errors}

    changed: list[str] = []
    if "full_name" in draft.values and draft.values["full_name"] != patient.full_name:
        patient.full_name = str(draft.values["full_name"])
        changed.append("full_name")
    if "phone" in draft.values and draft.values["phone"] != patient.phone:
        patient.phone = str(draft.values["phone"])
        changed.append("phone")
    if "date_of_birth" in draft.values and draft.values["date_of_birth"] != patient.date_of_birth:
        patient.date_of_birth = draft.values["date_of_birth"]  # type: ignore[assignment]
        changed.append("date_of_birth")
    if (
        "insurance_provider" in draft.values
        and draft.values["insurance_provider"] != patient.insurance_provider
    ):
        patient.insurance_provider = str(draft.values["insurance_provider"])
        changed.append("insurance_provider")

    patient.updated_at = utcnow()
    session.flush()
    if changed:
        audit.record(
            session,
            action="patient.updated",
            actor_type="patient_session",
            entity_type="patient",
            entity_id=patient.id,
            details={"fields": changed},
        )
    return {
        "updated": True,
        "changed_fields": changed,
        "first_name": first_name(patient.full_name),
    }
