"""The background reminder agent, notifications (email / SMS / calendar invite) and the chat invite tool.

Scripted model, real database (private test schema). What was sent is checked in the notifications table.
"""

import time
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from backend import api, notify, reminders, tools
from backend.agent import Session, run_turn
from backend.decision import RuleDecider
from backend.llm import ScriptedLLM
from backend.validator import validate
from tests.conftest import PATIENT, TODAY, act

NOW = datetime(2026, 11, 20, 10, 0)       # Friday morning on the test calendar
MONDAY_9 = "S-20261123-0900-P2"           # Dr. Chen, Mon Nov 23 09:00 (open in the seed)
THURSDAY = "2026-11-26"


def book(conn, slot_id, patient=PATIENT):
    req = tools.prepare_booking(conn, slot_id, patient, "cleaning")["approval_request"]
    assert tools.decide_and_apply(conn, req, True, "test")["verified"]
    return tools.list_appointments(conn, patient)[-1]


def sent(conn, **where):
    rows = notify.list_notifications(conn)
    return [r for r in rows if all(r[k] == v for k, v in where.items())]


GOOD = [act("send_email", subject="Confirmed: Monday at 09:00",
            message="Hi Avery, your cleaning with Dr. Chen is confirmed for Monday, November 23 at 09:00. "
                    "The calendar invite is attached. Reply in the chat to move or cancel."),
        act("send_sms", message="Bright Smile: confirmed Monday, November 23 at 09:00 with Dr. Chen."),
        act("finish")]


# --- which notices are due (decided by Python) ----------------------------------

def test_new_booking_needs_a_confirmation(conn):
    appt = book(conn, MONDAY_9)
    jobs = reminders.due_jobs(conn, NOW)
    assert [(j["kind"], j["appt"]["id"]) for j in jobs] == [("confirmation", appt["id"])]


def test_seeded_filler_bookings_are_ignored(conn):
    assert reminders.due_jobs(conn, NOW) == []          # the seed has many 'Test Patient D' bookings


def test_reminder_only_inside_the_window_and_after_an_early_confirmation(conn):
    slot = tools.list_openings(conn, THURSDAY, provider="Dr. Chen")["openings"][0]["slot_id"]
    appt = book(conn, slot)
    reminders.run_once(conn, ScriptedLLM([]), NOW)        # confirmation (Thursday is > 48 h away)
    assert reminders.due_jobs(conn, NOW) == []            # nothing else yet
    two_days_before = datetime.fromisoformat(f"{THURSDAY}T08:00")
    assert [j["kind"] for j in reminders.due_jobs(conn, two_days_before)] == ["reminder"]
    assert reminders.due_jobs(conn, datetime.fromisoformat(f"{THURSDAY}T23:00")) == []   # already happened
    assert appt["id"]


def test_booking_inside_the_window_gets_no_second_reminder(conn):
    book(conn, MONDAY_9)                                   # Monday 9:00, booked Friday: already inside 72 h?
    reminders.run_once(conn, ScriptedLLM([]), NOW)
    sunday = datetime(2026, 11, 22, 12, 0)                # Monday 09:00 is within 48 h
    # Confirmation was sent Friday 10:00, which is earlier than 48 h before Monday 09:00 -> reminder IS due
    assert [j["kind"] for j in reminders.due_jobs(conn, sunday)] == ["reminder"]
    reminders.run_once(conn, ScriptedLLM([]), sunday)
    assert reminders.due_jobs(conn, sunday) == []


def test_cancelled_appointments_get_nothing(conn):
    appt = book(conn, MONDAY_9)
    req = tools.prepare_cancel(conn, appt["id"], PATIENT, "test")["approval_request"]
    assert tools.decide_and_apply(conn, req, True, "test")["verified"]
    assert reminders.due_jobs(conn, NOW) == []


# --- the agent run --------------------------------------------------------------

def test_agent_sends_email_with_invite_and_sms_once(conn):
    appt = book(conn, MONDAY_9)
    summary = reminders.run_once(conn, ScriptedLLM(list(GOOD)), NOW)
    assert summary["jobs"] == 1 and summary["fallbacks"] == 0
    email, = sent(conn, channel="email")
    sms, = sent(conn, channel="sms")
    assert email["to_address"] == "avery.example@example.com" and email["has_invite"] and email["sent_by"] == "reminder-agent"
    assert sms["to_address"] == "555-0101" and email["status"] == sms["status"] == "simulated"
    assert email["appointment_id"] == appt["id"]
    assert reminders.run_once(conn, ScriptedLLM(list(GOOD)), NOW)["jobs"] == 0          # never twice


def test_wrong_time_is_rejected_then_fixed(conn):
    book(conn, MONDAY_9)
    script = [act("send_email", subject="Confirmed", message="Hi Avery, see you Monday, November 23 at 10:00!"),
              *GOOD]
    r = reminders.run_once(conn, ScriptedLLM(script), NOW)["results"][0]
    assert r["rejected"] == 1 and not r["fallback"]
    assert all("09:00" in n["body"] or "9:00" in n["body"] for n in sent(conn))


def test_extra_times_and_phone_numbers_are_rejected(conn):
    book(conn, MONDAY_9)
    script = [act("send_email", subject="x", message="Monday, November 23 at 09:00 or 14:00 with Dr. Chen."),
              act("send_email", subject="x", message="Monday, November 23 at 09:00. Call 617-555-1234."),
              *GOOD]
    r = reminders.run_once(conn, ScriptedLLM(script), NOW)["results"][0]
    assert r["rejected"] == 2


def test_model_failure_falls_back_to_a_correct_template(conn):
    book(conn, MONDAY_9)
    bad = act("send_email", subject="x", message="See you soon!")                 # no date or time, three times
    r = reminders.run_once(conn, ScriptedLLM([bad, bad, bad]), NOW)["results"][0]
    assert r["fallback"] == ["email", "sms"]
    for n in sent(conn):
        assert n["sent_by"] == "reminder-fallback" and "09:00" in n["body"] and "Dr. Chen" in n["body"]


def test_model_cannot_choose_the_recipient(conn):
    errors = validate({"action": "send_email", "argument": {"subject": "x", "message": "y", "to": "someone@gmail.com"}},
                      TODAY, conn, agent="reminders")
    assert any("no_extra_args" in e for e in errors)
    assert validate({"action": "book_appointment", "argument": {"slot_id": MONDAY_9, "reason": "x"}},
                    TODAY, conn, agent="reminders")                               # and cannot touch the schedule


def test_ics_is_a_valid_30_minute_event(conn):
    appt = tools.appointment_for_notice(conn, book(conn, MONDAY_9)["id"])
    ics = notify.make_ics(appt, "Avery Example", tools.OFFICE_INFO["address"])
    assert ics.startswith("BEGIN:VCALENDAR") and "END:VCALENDAR" in ics
    assert "DTSTART:20261123T090000" in ics and "DTEND:20261123T093000" in ics
    assert f"UID:appt-{appt['id']}@brightsmile.example" in ics and "Dr. Chen" in ics


# --- the chat tool --------------------------------------------------------------

def test_chat_can_send_a_calendar_invite_for_own_appointment(conn):
    appt = book(conn, MONDAY_9)
    session = Session(conn=conn, patient_alias=PATIENT, today=TODAY)
    turn = run_turn(session, "can you send me a calendar invite?", ScriptedLLM([
        act("list_my_appointments"), act("send_calendar_invite", appointment_id=appt["id"]),
        act("reply", message="Done: I emailed you a calendar invite for Monday, November 23 at 09:00.")]),
        RuleDecider(), lambda r: True, save=False)
    assert turn.stop_reason == "replied"
    invite, = sent(conn, kind="invite")
    assert invite["has_invite"] and invite["sent_by"] == "chat"


def test_chat_invite_refuses_someone_elses_appointment(conn):
    other = book(conn, MONDAY_9, patient="Test Patient B")
    r = tools.send_calendar_invite(conn, PATIENT, other["id"])
    assert not r["ok"] and sent(conn) == []


# --- API and the background worker ---------------------------------------------

@pytest.fixture
def client(monkeypatch, tmp_path, conn):
    monkeypatch.setattr(api.config, "EVIDENCE_DIR", tmp_path)
    monkeypatch.setattr(api.config, "today", lambda: TODAY)
    monkeypatch.setattr(api.config, "SUPABASE_URL", "")
    monkeypatch.setattr(api.config, "SUPABASE_ANON_KEY", "")
    api._state.update(conn=conn, sessions={}, decider=RuleDecider())
    yield TestClient(api.app)
    api._state.update(conn=None, sessions={}, llm=None, decider=None)


def test_api_patient_sees_only_own_messages_and_downloads_invite(client, conn):
    book(conn, MONDAY_9)
    book(conn, "S-20261123-0930-P2", patient="Test Patient B")
    reminders.run_once(conn, ScriptedLLM([]), NOW)                       # template fallback for both
    mine = client.get("/api/notifications", params={"patient": PATIENT}).json()
    assert mine and {n["patient_alias"] for n in mine} == {PATIENT}
    email = next(n for n in mine if n["has_invite"])
    r = client.get(f"/api/notifications/{email['id']}/invite.ics", params={"patient": PATIENT})
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/calendar") and b"BEGIN:VEVENT" in r.content
    assert client.get(f"/api/notifications/{email['id']}/invite.ics",
                      params={"patient": "Test Patient B"}).status_code == 404        # not theirs


def test_api_staff_run_now(client, conn, monkeypatch):
    book(conn, MONDAY_9)
    api._state["llm"] = ScriptedLLM(list(GOOD))
    monkeypatch.setattr(reminders, "clinic_now", lambda: NOW)
    r = client.post("/api/staff/reminders/run").json()
    assert r["jobs"] == 1 and r["results"][0]["sent"] == {"email": "simulated", "sms": "simulated"}
    log = client.get("/api/staff/notifications").json()
    assert len(log["notifications"]) == 2


def test_background_worker_ticks_and_stops(conn, monkeypatch):
    book(conn, MONDAY_9)
    monkeypatch.setattr(reminders, "clinic_now", lambda: NOW)
    worker = reminders.ReminderWorker(interval=0.2, llm=ScriptedLLM([]), conn_factory=lambda: conn)
    worker.start()
    deadline = time.time() + 20
    while worker.ticks < 2 and time.time() < deadline:
        time.sleep(0.1)
    worker.stop()
    worker.join(timeout=5)
    assert not worker.is_alive() and worker.ticks >= 2 and worker.last_error is None
    assert len(sent(conn, kind="confirmation")) == 2          # sent once by the first tick, not again by the second


# --- real email for an allowlisted owner (live test), everyone else simulated ----

OWNER = "owner.test@gmail.com"     # a placeholder; the real address lives only in the owner's .env


class FakeSMTP:
    sent = []

    def __init__(self, host, port, timeout=None):
        self.host = host

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def starttls(self):
        pass

    def login(self, user, password):
        pass

    def send_message(self, msg):
        FakeSMTP.sent.append(msg)


@pytest.fixture
def smtp(monkeypatch):
    FakeSMTP.sent = []
    monkeypatch.setattr(notify.smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(notify.config, "SMTP_HOST", "smtp.gmail.com")
    monkeypatch.setattr(notify.config, "SMTP_PORT", 587)
    monkeypatch.setattr(notify.config, "SMTP_STARTTLS", True)
    monkeypatch.setattr(notify.config, "SMTP_USER", "sender@gmail.com")
    monkeypatch.setattr(notify.config, "REAL_CONTACT_ALLOWLIST", {OWNER})
    return FakeSMTP


def test_allowlisted_real_email_can_register_others_cannot(conn, monkeypatch):
    from backend import db
    monkeypatch.setattr(tools.config, "REAL_CONTACT_ALLOWLIST", {OWNER})
    alias = db.create_new_patient(conn)
    base = dict(first_name="Owner", last_name="Test", date_of_birth="1999-01-01", phone="555-0150",
                insurance_provider="self-pay", today=TODAY.isoformat())
    assert not tools.register_patient(conn, alias, email="someone.else@gmail.com", **base)["ok"]
    assert tools.register_patient(conn, alias, email=OWNER, **base)["ok"]


def test_real_email_only_to_allowlisted_address(conn, smtp):
    from backend import db
    alias = db.create_new_patient(conn)
    conn.run("UPDATE patients SET first_name='Owner', last_name='Test', phone='555-0150', email=?, "
             "insurance_provider='self-pay', registered_at='2026-11-20' WHERE alias = ?", (OWNER, alias))
    book(conn, MONDAY_9, patient=alias)
    book(conn, "S-20261123-0930-P2")                                  # Avery: an @example.com address
    reminders.run_once(conn, ScriptedLLM([]), NOW)
    owner_email, = sent(conn, patient_alias=alias, channel="email")
    avery_email, = sent(conn, patient_alias=PATIENT, channel="email")
    assert owner_email["status"] == "sent" and avery_email["status"] == "simulated"
    assert [m["To"] for m in smtp.sent] == [OWNER]                    # exactly one real email, to the owner
    att = [p for p in smtp.sent[0].iter_attachments()]
    assert att and att[0].get_filename() == "appointment.ics" and b"BEGIN:VEVENT" in att[0].get_content().encode()
    assert "sender@gmail.com" in smtp.sent[0]["From"]


def test_smtp_failure_is_recorded_not_hidden(conn, smtp, monkeypatch):
    from backend import db

    class Broken(FakeSMTP):
        def login(self, user, password):
            raise notify.smtplib.SMTPAuthenticationError(535, b"bad credentials")
    monkeypatch.setattr(notify.smtplib, "SMTP", Broken)
    alias = db.create_new_patient(conn)
    conn.run("UPDATE patients SET first_name='Owner', email=?, phone='555-0150', registered_at='2026-11-20' "
             "WHERE alias = ?", (OWNER, alias))
    book(conn, MONDAY_9, patient=alias)
    reminders.run_once(conn, ScriptedLLM([]), NOW)
    email, = sent(conn, channel="email")
    assert email["status"] == "failed" and "SMTPAuthenticationError" in email["body"]
