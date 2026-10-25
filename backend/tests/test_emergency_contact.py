"""Emergency reports must reach the front desk with a way to call back.

The board used to show "Unknown caller" with a blank phone: an urgent alert
nobody could act on. These tests pin the rule that a non-life-threatening
report is only filed when a name and a valid number are present, that the
details are contact information rather than identity, and that a thread does
not have to repeat them.

A 911 report is exempt on purpose: asking for a callback number delays the only
correct answer.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app import migrations
from app.db import create_db_engine
from app.models import PatientSession, StaffAlert
from app.services import emergency, patient_sessions, staff_handoff_board

PHONE = "(650) 555-0147"


def _session(db: Session) -> PatientSession:
    created = patient_sessions.create_patient_session(db)
    db.flush()
    record = db.get(PatientSession, created.session_id)
    assert record is not None
    return record


def _alerts(db: Session) -> list[StaffAlert]:
    return db.query(StaffAlert).order_by(StaffAlert.id).all()


def _report(db: Session, world, record: PatientSession, **kwargs):
    params = {
        "description": "Severe tooth pain, asking to book an emergency visit.",
        "patient_session": record,
    }
    params.update(kwargs)
    return emergency.triage(db, conversation_id=world.conversation.id, patient_id=None, **params)


# --------------------------------------------------------------------------- #
# The rule
# --------------------------------------------------------------------------- #


def test_missing_contact_files_nothing(scenario):
    db, world = scenario
    record = _session(db)

    result = _report(db, world, record)

    assert result.contact_required is True
    assert set(result.field_errors) == {"caller_name", "caller_phone"}
    assert result.alert is None
    assert _alerts(db) == []
    payload = result.as_dict()
    assert payload["ok"] is False
    assert payload["reason"] == "contact_details_required"
    assert "Nothing has been sent" in payload["next_step"]


def test_invalid_phone_is_rejected_and_nothing_is_filed(scenario):
    db, world = scenario
    record = _session(db)

    result = _report(db, world, record, caller_name="Avery Stone", caller_phone="555-0147")

    assert result.contact_required is True
    assert set(result.field_errors) == {"caller_phone"}
    assert _alerts(db) == []
    # A rejected number is never remembered as if it were usable.
    assert record.contact_phone is None


def test_name_and_phone_file_the_alert_with_a_call_back(scenario):
    db, world = scenario
    record = _session(db)

    result = _report(db, world, record, caller_name="avery stone", caller_phone=PHONE)

    assert result.contact_required is False
    alert = _alerts(db)[0]
    assert alert.status == "open"
    # Contact information, not a chart: the alert stays unlinked to a patient.
    assert alert.patient_id is None
    assert alert.caller_name == "Avery Stone"
    assert alert.caller_phone == "+16505550147"
    assert "Call back: Avery Stone, +16505550147" in alert.message
    assert record.contact_name == "Avery Stone"
    assert record.contact_phone == "+16505550147"
    assert "name and number" in emergency.emergency_summary_line(result)


def test_contact_details_never_verify_the_session(scenario):
    db, world = scenario
    record = _session(db)

    _report(db, world, record, caller_name="Avery Stone", caller_phone=PHONE)

    assert record.verified_patient_id is None
    assert patient_sessions.verified_patient(db, record) is None


def test_follow_up_reuses_details_the_thread_already_has(scenario):
    db, world = scenario
    record = _session(db)
    _report(db, world, record, caller_name="Avery Stone", caller_phone=PHONE)
    db.flush()

    report = emergency.follow_up(
        db,
        conversation_id=world.conversation.id,
        patient_id=None,
        description="It is getting worse and it is more swollen than before.",
        patient_session=record,
    )

    assert report.triage.contact_required is False
    recorded = report.triage.alert
    assert recorded is not None
    assert recorded.report_count == 2
    assert recorded.caller_contact == {"name": "Avery Stone", "phone": "+16505550147"}
    # The follow-up summary is staff-readable updated detail, not a bare call back.
    assert "getting worse" in recorded.message
    assert recorded.message.count("Call back:") == 1
    assert recorded.message.count("Severe tooth pain") == 1, "the original concern is kept"
    assert len(_alerts(db)) == 1, "a worsening report updates the same alert"


def test_follow_up_reopens_a_case_staff_already_resolved(scenario):
    db, world = scenario
    record = _session(db)
    _report(db, world, record, caller_name="Avery Stone", caller_phone=PHONE)
    db.flush()
    alert = _alerts(db)[0]
    alert.status = "resolved"
    db.flush()

    second = emergency.follow_up(
        db,
        conversation_id=world.conversation.id,
        patient_id=world.patient.id,
        description="It is getting worse and it is more swollen than before.",
        patient_session=record,
    )

    # One case, reopened, with the follow-up counted on it.
    assert second.triage.alert is not None
    assert second.triage.alert.record_id == alert.id
    assert second.triage.alert.status == "open"
    assert second.triage.alert.report_count == 2
    assert len(_alerts(db)) == 1, "a follow-up must not open a second case"
    # The model is told what actually happened, never that staff are waiting.
    assert second.triage.reopened is True
    assert "reopened" in second.triage.guidance[0]


def test_life_threatening_report_is_never_blocked(scenario):
    db, world = scenario
    record = _session(db)

    result = _report(db, world, record, description="I can't breathe and my throat is closing")

    assert result.life_threatening is True
    assert result.contact_required is False
    assert _alerts(db) == [], "911 is the responder, so no front-desk alert is filed"
    assert result.guidance[0] == emergency.EMERGENCY_CALL_ADVICE


def test_verified_patient_needs_no_extra_details(scenario):
    db, world = scenario

    result = emergency.triage(
        db,
        conversation_id=world.conversation.id,
        patient_id=world.patient.id,
        description="Severe tooth pain",
    )

    assert result.contact_required is False
    alert = _alerts(db)[0]
    assert alert.patient_id == world.patient.id
    assert alert.caller_phone is None, "the chart is the call back for a known patient"
    assert "Call back:" not in alert.message


# --------------------------------------------------------------------------- #
# What the staff board reads
# --------------------------------------------------------------------------- #


def test_board_shows_the_caller_instead_of_an_unknown_caller(scenario):
    db, world = scenario
    record = _session(db)
    _report(db, world, record, caller_name="Avery Stone", caller_phone=PHONE)
    db.flush()

    payload = staff_handoff_board.alert_dict(db, _alerts(db)[0])

    assert payload["patient"] is None
    assert payload["caller"] == {"name": "Avery Stone", "phone": "+16505550147"}


def test_board_reports_no_caller_when_a_patient_is_on_the_chart(scenario):
    db, world = scenario
    emergency.triage(
        db,
        conversation_id=world.conversation.id,
        patient_id=world.patient.id,
        description="Severe tooth pain",
    )
    db.flush()

    payload = staff_handoff_board.alert_dict(db, _alerts(db)[0])

    assert payload["patient"]["full_name"] == world.patient.full_name
    assert payload["caller"] is None


# --------------------------------------------------------------------------- #
# Upgrading an existing database
# --------------------------------------------------------------------------- #


def test_existing_database_gains_the_new_columns(tmp_path):
    """A Docker volume from before this change must keep working."""
    path = tmp_path / "upgrade.db"
    engine = create_db_engine(f"sqlite+pysqlite:///{path}")
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql(
                "CREATE TABLE patient_sessions ("
                "id INTEGER PRIMARY KEY, token_hash VARCHAR(64), reset_generation VARCHAR(36),"
                "verified_patient_id INTEGER, verified_at DATETIME,"
                "verification_method VARCHAR(24), last_seen_at DATETIME, expires_at DATETIME,"
                "created_at DATETIME, updated_at DATETIME)"
            )
            connection.exec_driver_sql(
                "CREATE TABLE staff_alerts (id INTEGER PRIMARY KEY, conversation_id INTEGER)"
            )
            migrations.apply_additive_migrations(connection)
            session_columns = {
                row[1] for row in connection.exec_driver_sql("PRAGMA table_info(patient_sessions)")
            }
            alert_columns = {
                row[1] for row in connection.exec_driver_sql("PRAGMA table_info(staff_alerts)")
            }

        assert {"contact_name", "contact_phone"} <= session_columns
        assert {"caller_name", "caller_phone"} <= alert_columns
    finally:
        engine.dispose()
