"""Emergency triage, symptom capture, and safe guidance.

Two speeds, deliberately
-----------------------
*Life-threatening* symptoms (trouble breathing, swelling closing the airway,
uncontrolled bleeding, suspected facial trauma, chest pain, loss of
consciousness) get immediate 911 guidance **before** anything else: no
registration, no identity check, no availability search. Slowing that down to
ask for a date of birth would be unsafe.

Everything else (severe pain, facial swelling without airway involvement,
knocked-out tooth, urgent requests) is captured as an emergency report, alerts
the front desk, and only then looks at whether a real emergency slot exists.

Contact details are mandatory
-----------------------------
A front desk cannot act on "someone has severe tooth pain" with no name and no
number. Every non-life-threatening report therefore requires a caller name and
a valid US phone number; without them no alert is written and the tool returns
``contact_details_required`` so the assistant asks for them (or relays exactly
which detail is wrong). The details are contact information, not identity: they
are attached to the alert for a callback and never verify the session or grant
access to a chart. Life-threatening reports are exempt only because 911 does not
need a callback number, and asking for one would delay that answer. Repeat calls
from the same thread do not have to re-enter what the session already holds.

Triage is keyword-driven Python, not an LLM judgement: the model may phrase the
answer, but which branch runs is decided here from what the patient actually
said. ``is_life_threatening`` errs towards caution -- it is better to mention
911 once too often than once too few.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AlertKind, AlertStatus, PatientSession, StaffAlert, utcnow
from app.services import staff_handoff
from app.validation import normalize_name, normalize_phone, phone_digits

EMERGENCY_CALL_ADVICE = (
    "Call 911 or go to the nearest emergency room right now. Do not wait for a dental appointment."
)

# Phrases that indicate an airway/medical emergency. Checked as word-ish
# substrings on a normalised (lowercase, punctuation-stripped) sentence.
_LIFE_THREATENING = (
    "can't breathe",
    "cannot breathe",
    "can not breathe",
    "cant breathe",
    "trouble breathing",
    "difficulty breathing",
    "hard to breathe",
    "struggling to breathe",
    "stopped breathing",
    "throat closing",
    "closing throat",
    "can't swallow",
    "cannot swallow",
    "airway",
    "swelling is spreading",
    "swelling spreading",
    "spreading to my neck",
    "neck swelling",
    "uncontrolled bleeding",
    "bleeding won't stop",
    "bleeding wont stop",
    "won't stop bleeding",
    "wont stop bleeding",
    "severe bleeding",
    "passed out",
    "fainting",
    "unconscious",
    "loss of consciousness",
    "chest pain",
    "jaw broken",
    "broken jaw",
    "face injury",
    "facial trauma",
    "knocked out by",
    "anaphylaxis",
    "allergic reaction",
    "seizure",
)

_URGENT_SYMPTOMS = (
    ("swelling", "facial swelling"),
    ("swollen", "facial swelling"),
    ("severe pain", "severe pain"),
    ("worst pain", "severe pain"),
    ("unbearable", "severe pain"),
    ("abscess", "possible abscess"),
    ("knocked out", "dental trauma"),
    ("broke", "dental trauma"),
    ("broken tooth", "dental trauma"),
    ("bleeding", "bleeding"),
    ("pus", "possible infection"),
    ("fever", "fever"),
    ("can't chew", "pain on chewing"),
    ("toothache", "toothache"),
    ("emergency", "urgent request"),
    ("urgent", "urgent request"),
    ("as soon as possible", "urgent request"),
    ("asap", "urgent request"),
    ("right away", "urgent request"),
)

MAX_SUMMARY_CHARS = 600


@dataclass
class TriageResult:
    life_threatening: bool
    symptoms: list[str] = field(default_factory=list)
    summary: str = ""
    guidance: list[str] = field(default_factory=list)
    alert: staff_handoff.HandoffResult | None = None
    # True when the report carries symptoms but no name/phone, so nothing was
    # filed. The assistant must collect them before this counts as handled.
    contact_required: bool = False
    field_errors: dict[str, str] = field(default_factory=dict)
    # True when a follow-up reopened a case staff had already resolved.
    reopened: bool = False

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "ok": not self.contact_required,
            "life_threatening": self.life_threatening,
            "symptoms": self.symptoms,
            "summary": self.summary,
            "guidance": self.guidance,
            "alert": self.alert.as_dict() if self.alert else None,
        }
        if self.contact_required:
            payload["reason"] = "contact_details_required"
            payload["field_errors"] = self.field_errors
            payload["next_step"] = (
                "Nothing has been sent to the front desk yet. Ask for the missing details in "
                "one calm sentence, then call report_emergency again with the same symptoms "
                "plus caller_name and caller_phone. Ask only for the fields in field_errors."
            )
        return payload


def missing_contact_fields(name: str | None, phone: str | None) -> dict[str, str]:
    """Which caller details are missing or invalid, keyed like other tool fields.

    Empty means the report may be filed. A name must be given as a name (it is
    not a chart lookup, so a single word is acceptable) and the phone must be a
    complete, unambiguous US number: the hold music is worthless if staff cannot
    dial back.
    """
    errors: dict[str, str] = {}
    if not (name or "").strip():
        errors["caller_name"] = "I still need a name for the call back."
    else:
        cleaned = normalize_name(name)
        if not cleaned.ok or not cleaned.value:
            errors["caller_name"] = "Could you give the name as it should appear on the call back?"
    if not (phone or "").strip():
        errors["caller_phone"] = "I still need a phone number so the front desk can call back."
    else:
        # ``normalize_phone`` reads empty and short input the same way ("I still
        # need..."), which sounds like it was never given. Say which it is.
        parsed = normalize_phone(phone)
        if not parsed.ok or not parsed.e164:
            complete = phone_digits(phone)
            if len(complete) < 10:
                errors["caller_phone"] = (
                    "That number looks incomplete. I need the full 10-digit US number."
                )
            else:
                errors["caller_phone"] = parsed.error or "That phone number doesn't look right."
    return errors


def record_contact(
    patient_session: PatientSession | None,
    *,
    name: str | None,
    phone: str | None,
) -> tuple[str | None, str | None]:
    """Store the caller's contact details for the rest of the session.

    Only the *normalized* values are kept, so a later report, the "new chat"
    action, and the front desk all see the same number. Returns the values to
    use now, including ones captured earlier in the session.
    """
    resolved_name = name
    resolved_phone = phone
    if patient_session is not None:
        if name and name.strip():
            cleaned = normalize_name(name)
            if cleaned.ok and cleaned.value:
                patient_session.contact_name = cleaned.value
                resolved_name = cleaned.value
        if phone and phone.strip():
            parsed = normalize_phone(phone)
            if parsed.ok and parsed.e164:
                patient_session.contact_phone = parsed.e164
                resolved_phone = parsed.e164
        resolved_name = patient_session.contact_name or resolved_name
        resolved_phone = patient_session.contact_phone or resolved_phone
    return resolved_name, resolved_phone


def _normalise(text: str) -> str:
    lowered = text.lower().replace("\u2019", "'").replace("\u2018", "'")
    lowered = re.sub(r"[^a-z0-9'\s]", " ", lowered)
    return re.sub(r"\s+", " ", lowered).strip()


def is_life_threatening(text: str) -> bool:
    """True when the description suggests an airway or medical emergency."""
    haystack = _normalise(text)
    return any(phrase in haystack for phrase in _LIFE_THREATENING)


def extract_symptoms(text: str) -> list[str]:
    """Recognised symptom labels, in the order the phrase table lists them."""
    haystack = _normalise(text)
    found: list[str] = []
    for needle, label in _URGENT_SYMPTOMS:
        if needle in haystack and label not in found:
            found.append(label)
    return found


def summarise(
    symptoms: list[str] | None, description: str | None, *, limit: int = MAX_SUMMARY_CHARS
) -> str:
    """A short, staff-readable summary. Never includes unverified identity data."""
    parts: list[str] = []
    if symptoms:
        parts.append("Reported: " + ", ".join(dict.fromkeys(symptoms)))
    if description:
        cleaned = " ".join(description.split())
        parts.append(cleaned[:limit])
    return " | ".join(parts)[: limit + 200]


def _severity(result: TriageResult) -> str:
    if result.life_threatening:
        return "urgent"
    if {"facial swelling", "severe pain", "possible abscess", "dental trauma"} & set(
        result.symptoms
    ):
        return "high"
    return "normal"


def triage(
    session: Session,
    *,
    conversation_id: int,
    patient_id: int | None,
    description: str | None = None,
    symptoms: list[str] | None = None,
    wants_urgent_visit: bool = True,
    caller_name: str | None = None,
    caller_phone: str | None = None,
    patient_session: PatientSession | None = None,
) -> TriageResult:
    """Classify an emergency report and record it for staff.

    For life-threatening reports this does **not** create a clinic alert: the
    correct handoff is 911, and filing a front-desk alert would imply the clinic
    is the appropriate responder.

    For every other report a caller name and phone number are required. When a
    verified patient is already speaking, the chart supplies identity and no
    extra details are asked for; otherwise the details (from this call, or one
    captured earlier in the session) are required, attached to the alert, and
    remembered on the session for follow-ups.
    """
    text = " ".join(part for part in [description or "", " ".join(symptoms or [])] if part).strip()
    detected = list(dict.fromkeys([*(symptoms or []), *extract_symptoms(text)]))
    life_threatening = is_life_threatening(text)
    summary = summarise(detected, description)

    result = TriageResult(
        life_threatening=life_threatening,
        symptoms=detected,
        summary=summary,
    )

    # The contact details belong to the session, not to the outcome: a caller who
    # gave them once is never asked again on this call. Stored before any early
    # return so a blocked report (no symptoms yet) still remembers them.
    if patient_id is None and not life_threatening:
        record_contact(patient_session, name=caller_name, phone=caller_phone)

    if life_threatening:
        result.guidance = [
            EMERGENCY_CALL_ADVICE,
            "If you are with someone who is struggling to breathe or swallow, call 911 immediately.",
            "Once you are safe, the clinic can arrange a follow-up.",
        ]
        # No staff alert is written on this path, so we must not claim one was.
        # The guidance above is the whole answer: call emergency services first.
        return result

    if not detected and not description:
        result.guidance = [
            "Tell me what you're experiencing, for example pain level, swelling, or a broken tooth."
        ]
        return result

    # An identified patient needs nothing more: the chart is the call back.
    contact_name: str | None = None
    contact_phone: str | None = None
    if patient_id is None:
        # Validated on what the session holds, so a retry only needs the fix.
        errors = missing_contact_fields(
            patient_session.contact_name if patient_session else caller_name,
            patient_session.contact_phone if patient_session else caller_phone,
        )
        if errors:
            result.contact_required = True
            result.field_errors = errors
            result.guidance = [
                "Before I pass this to the front desk, I need a name and a phone number "
                "so they can call you back."
            ]
            return result
        # Validated above, so these are the normalized values already stored.
        contact_name = patient_session.contact_name if patient_session else caller_name
        contact_phone = patient_session.contact_phone if patient_session else caller_phone

    result.alert = staff_handoff.raise_alert(
        session,
        conversation_id=conversation_id,
        patient_id=patient_id,
        kind=AlertKind.EMERGENCY,
        severity=_severity(result),
        message=summary or "Urgent dental request",
        subject="emergency",
        caller_name=contact_name,
        caller_phone=contact_phone,
    )
    if contact_name or contact_phone:
        result.alert.caller_contact = {"name": contact_name, "phone": contact_phone}
    if result.alert.created:
        result.guidance = [
            "I've recorded this for the front desk so they can review it.",
            "If your breathing or swallowing becomes difficult, call 911 immediately.",
        ]
    else:
        # ``created`` is False for a repeat report in the same thread, which may
        # already be acknowledged or resolved by staff. The guidance comes from
        # the alert's real status, never from assuming a person is waiting.
        result.guidance = [
            f"The front desk has this, and it is {result.alert.as_dict()['summary']}.",
            "If your breathing or swallowing becomes difficult, call 911 immediately.",
        ]
    if wants_urgent_visit:
        result.guidance.append(
            "I can also check whether an emergency slot is genuinely open today or tomorrow."
        )
    return result


@dataclass
class FollowUpReport:
    """A later message in an emergency conversation."""

    worsening: bool
    triage: TriageResult


_WORSENING = (
    "worse",
    "getting worse",
    "worsening",
    "more swollen",
    "swelling got bigger",
    "spreading",
    "fever now",
    "can't sleep",
    "unbearable",
)


def _reopen_earlier_case(
    session: Session, conversation_id: int, fresh: staff_handoff.HandoffResult
) -> staff_handoff.HandoffResult | None:
    """Keep one case per conversation when staff already handled the first.

    ``raise_alert`` matches open and acknowledged rows, so a follow-up already
    updates a case someone is working. A *resolved* case is the exception: the
    next report opens a brand-new alert beside the closed one, which leaves the
    board showing a fresh case with no history and a closed one that still holds
    the original concern. This reopens the case the board already knows and
    discards the duplicate, so one thread stays one case.

    Returns the result describing the reopened case, or ``None`` when nothing
    needs merging.
    """
    duplicate = session.get(StaffAlert, fresh.record_id)
    if duplicate is None:  # pragma: no cover - the caller passes a live alert
        return None
    if duplicate.status == AlertStatus.ACKNOWLEDGED.value:
        # Someone is already working on it; raising it again would undo their
        # acknowledgement. The follow-up still reached the row itself.
        return None

    earlier_rows = session.scalars(
        select(StaffAlert)
        .where(StaffAlert.dedupe_key == fresh.dedupe_key, StaffAlert.id != duplicate.id)
        .order_by(StaffAlert.id)
    ).all()
    earlier = next((row for row in earlier_rows if row.status == AlertStatus.RESOLVED.value), None)
    if earlier is None:
        return None

    # Reopening rather than inserting a second case: staff closed the *case*, and
    # a call in the same thread is the same ongoing concern. The duplicate and
    # the earlier row share this identity map, so a DELETE and the UPDATE in one
    # flush would share a column list and the earlier row would keep the
    # duplicate's values. Drop the duplicate on its own first, keeping its text.
    carried_name = duplicate.caller_name
    carried_phone = duplicate.caller_phone
    carried_summary = duplicate.message
    carried_severity = duplicate.severity
    session.delete(duplicate)
    session.flush()

    earlier.status = AlertStatus.OPEN.value
    earlier.resolved_at = None
    earlier.message = carried_summary
    earlier.severity = carried_severity
    if carried_name is not None:
        earlier.caller_name = carried_name
    if carried_phone is not None:
        earlier.caller_phone = carried_phone
    earlier.report_count = (earlier.report_count or 1) + 1
    earlier.last_reported_at = utcnow()
    session.flush()
    return staff_handoff.HandoffResult(
        kind=fresh.kind,
        record_id=earlier.id,
        dedupe_key=earlier.dedupe_key or fresh.dedupe_key,
        status=earlier.status,
        severity=earlier.severity,
        message=earlier.message,
        created=False,
        report_count=earlier.report_count,
        caller_contact=fresh.caller_contact,
    )


def follow_up(
    session: Session,
    *,
    conversation_id: int,
    patient_id: int | None,
    description: str,
    prior_summary: str | None = None,
    caller_name: str | None = None,
    caller_phone: str | None = None,
    patient_session: PatientSession | None = None,
) -> FollowUpReport:
    """Record an update to an existing emergency report.

    A worsening report updates the *same* alert (so staff see one case, not
    duplicates) while still triggering life-threatening guidance immediately.

    An unverified caller who already left a name and number is not asked again:
    ``record_contact`` hands back what the session holds, and only a thread that
    never captured them gets ``contact_details_required``.
    """
    normalised = _normalise(description)
    worsening = any(marker in normalised for marker in _WORSENING)
    if prior_summary:
        # The stored summary already ends with the call-back line; drop it so
        # the alert does not accumulate one on every follow-up.
        prior_summary = prior_summary.split(" | Call back: ")[0]
    if prior_summary is None:
        # A follow-up replaces the open alert's summary, so the earlier concern
        # has to come from the row or the worsening detail arrives alone and the
        # original complaint is lost from the board.
        key = staff_handoff.dedupe_key(AlertKind.EMERGENCY, conversation_id, "emergency")
        current = session.scalar(
            select(StaffAlert).where(
                StaffAlert.dedupe_key == key,
                StaffAlert.status.in_([AlertStatus.OPEN.value, AlertStatus.ACKNOWLEDGED.value]),
            )
        )
        if current is not None and current.message:
            prior_summary = current.message.split(" | Call back: ")[0]
    combined = " ".join(part for part in [prior_summary, description] if part)
    result = triage(
        session,
        conversation_id=conversation_id,
        patient_id=patient_id,
        description=combined,
        wants_urgent_visit=True,
        caller_name=caller_name,
        caller_phone=caller_phone,
        patient_session=patient_session,
    )
    if result.alert is not None:
        reopened = _reopen_earlier_case(session, conversation_id, result.alert)
        if reopened is not None:
            result.alert = reopened
            result.reopened = True
            result.guidance = [
                "The front desk had closed this case, and I've reopened it with your new detail.",
                "If your breathing or swallowing becomes difficult, call 911 immediately.",
            ]
    return FollowUpReport(worsening=worsening or result.life_threatening, triage=result)


def emergency_summary_line(result: TriageResult) -> str:
    """One-line chat-safe statement, consistent with the recorded alert."""
    if result.life_threatening:
        return "This sounds like a medical emergency."
    if result.contact_required:
        return "I just need a name and phone number so the front desk can reach you."
    if result.alert is None:
        return "I don't have enough detail yet."
    if not result.alert.created:
        return "I've updated your urgent request with this detail."
    if result.alert.caller_contact:
        return "The front desk now has your name and number and will call you back."
    return "I've logged an urgent request for the front desk."


__all__ = [
    "EMERGENCY_CALL_ADVICE",
    "FollowUpReport",
    "TriageResult",
    "emergency_summary_line",
    "extract_symptoms",
    "follow_up",
    "is_life_threatening",
    "missing_contact_fields",
    "record_contact",
    "summarise",
    "triage",
]
