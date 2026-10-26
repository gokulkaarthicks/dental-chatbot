"""Chat-side identity lookups are counted, then paused.

Phone plus date of birth is a two-factor guess, and ``verify_existing_patient``
was an unlimited oracle over it: a caller could keep sending pairs until one
matched somebody else's chart. The throttle lives in the service, the refusal
lives in the tool, and both are pinned here.

Four properties the patient depends on:

* a lockout is timed, not permanent, so one honest run of typos does not cost
  anybody their own account;
* only a genuine chart miss counts. A malformed phone is not somebody attacking
  the chart;
* a successful lookup clears the tally;
* the lockout is visible to the front desk (audit row, warning event) without
  ever putting the numbers they were guessing at into a log.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from sqlalchemy import select

from app.models import AuditRecord, PatientSession, utcnow
from app.services import patient_sessions
from tests.support import invoke_tool
from tests.test_tool_gates import _unverified_deps

# A valid NANP number that simply is not on any chart. The previous fixture used
# 555-010-9999, whose exchange starts with 0; ``normalize_phone`` rejects that as
# undialable, so the lookup failed validation before it ever reached a chart and
# the counter never moved.
MISS = {"phone": "+16505550199", "date_of_birth": "04/12/1990"}
BAD_PHONE = {"phone": "abc", "date_of_birth": "not a date"}
LOCKED = "verification_locked"
LIMIT = patient_sessions.MAX_VERIFICATION_ATTEMPTS


def _record(session, deps):  # noqa: ANN001, ANN202
    return session.get(PatientSession, deps.patient_session.id)


def _attempts(session, deps) -> int:  # noqa: ANN001
    return _record(session, deps).verification_attempts or 0


def _misses(session, deps, count: int) -> list[dict]:  # noqa: ANN001
    return [invoke_tool(deps, "verify_existing_patient", MISS) for _ in range(count)]


# --- the counter itself ----------------------------------------------------


def test_a_fresh_chat_is_not_locked(scenario) -> None:
    session, _ = scenario

    assert patient_sessions.verification_locked(_unverified_deps(session).patient_session) is False


def test_only_the_last_allowed_miss_starts_the_lockout(scenario) -> None:
    session, _ = scenario
    deps = _unverified_deps(session)

    for attempt in range(1, LIMIT):
        result = invoke_tool(deps, "verify_existing_patient", MISS)
        assert result["error"] != LOCKED, result
        assert _attempts(session, deps) == attempt

    final = invoke_tool(deps, "verify_existing_patient", MISS)

    assert final["error"] == LOCKED, final
    assert patient_sessions.verification_locked(deps.patient_session) is True
    # The tally restarts from zero: a lockout is one pause, not a growing count.
    assert _attempts(session, deps) == 0


def test_a_locked_chat_never_reaches_a_chart(scenario) -> None:
    session, _ = scenario
    deps = _unverified_deps(session)
    _misses(session, deps, LIMIT)

    for _ in range(3):
        result = invoke_tool(deps, "verify_existing_patient", MISS)
        assert result["error"] == LOCKED
        assert result["message"] == patient_sessions.VERIFICATION_LOCKED_MESSAGE

    # Nothing new is counted while the pause is running.
    assert _attempts(session, deps) == 0


def test_the_lockout_expires_and_the_chat_can_try_again(scenario) -> None:
    session, _ = scenario
    deps = _unverified_deps(session)
    _misses(session, deps, LIMIT)
    deps.patient_session.verification_locked_until = utcnow() - timedelta(seconds=1)

    assert patient_sessions.verification_locked(deps.patient_session) is False
    assert invoke_tool(deps, "verify_existing_patient", MISS)["error"] != LOCKED


def test_an_expired_window_does_not_make_the_next_miss_a_lockout(scenario) -> None:
    """A stale timestamp is not a second lockout: one miss is still one miss."""
    session, _ = scenario
    deps = _unverified_deps(session)
    deps.patient_session.verification_locked_until = utcnow() - timedelta(seconds=1)

    result = invoke_tool(deps, "verify_existing_patient", MISS)

    assert result["error"] != LOCKED, result
    assert _attempts(session, deps) == 1


def test_a_malformed_answer_does_not_count_toward_the_lockout(scenario) -> None:
    """A phone that never parses never reached a chart, so it is not a guess."""
    session, _ = scenario
    deps = _unverified_deps(session)

    for _ in range(LIMIT):
        result = invoke_tool(deps, "verify_existing_patient", BAD_PHONE)
        assert result["error"] != LOCKED, result

    assert _attempts(session, deps) == 0


def test_a_successful_lookup_clears_the_counter(scenario) -> None:
    session, requester = scenario
    deps = _unverified_deps(session)
    _misses(session, deps, LIMIT - 1)
    assert _attempts(session, deps) == LIMIT - 1

    result = invoke_tool(
        deps,
        "verify_existing_patient",
        {
            "phone": requester.patient.phone,
            "date_of_birth": requester.patient.date_of_birth.strftime("%m/%d/%Y"),
        },
    )

    assert result["ok"] is True, result
    assert _attempts(session, deps) == 0
    assert _record(session, deps).verification_locked_until is None


# --- what the front desk can see ------------------------------------------


def test_the_lockout_is_audited_without_the_guessed_details(scenario, caplog) -> None:
    session, _ = scenario
    deps = _unverified_deps(session)

    with caplog.at_level(logging.WARNING, logger="app.agent.tools.domains.identity"):
        _misses(session, deps, LIMIT)
    session.commit()

    actions = set(
        session.scalars(
            select(AuditRecord.action).where(AuditRecord.action.like("identity.verification%"))
        )
    )
    assert actions == {"identity.verification_lockout"}

    events = [
        record
        for record in caplog.records
        if getattr(record, "event", None) == "identity.verification_lockout"
    ]
    assert len(events) == 1
    window_minutes = patient_sessions.VERIFICATION_LOCKOUT.total_seconds() / 60
    assert events[0].fields["minutes"] == window_minutes
    # Outcome only: the numbers a caller was guessing at stay out of the log.
    assert "6505550199" not in caplog.text
    assert "(650) 555-0199" not in caplog.text
