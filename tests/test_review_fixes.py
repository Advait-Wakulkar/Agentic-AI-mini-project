"""Regression tests for the findings of the independent pre-submission code review.

Scripted model, real database (private test schema). SMTP is always mocked.
"""

from datetime import datetime

import pytest

from backend import db, notify, reminders, tools
from backend.agent import Session, redact_real_contacts, run_turn
from backend.decision import RuleDecider
from backend.llm import ScriptedLLM
from backend.validator import validate
from tests.conftest import PATIENT, TODAY, act

NOW = datetime(2026, 11, 20, 10, 0)
MONDAY_9 = "S-20261123-0900-P2"
OWNER = "owner.test@gmail.com"


def book(conn, slot_id, patient=PATIENT):
    req = tools.prepare_booking(conn, slot_id, patient, "cleaning")["approval_request"]
    assert tools.decide_and_apply(conn, req, True, "test")["verified"]
    return tools.list_appointments(conn, patient)[-1]


def say(conn, script, message, patient=PATIENT):
    session = Session(conn=conn, patient_alias=patient, today=TODAY)
    return run_turn(session, message, ScriptedLLM(script), RuleDecider(), lambda r: True, channel="test", save=False)


# --- 2, 3, 10: null / empty / odd arguments never crash ----------------------------

@pytest.mark.parametrize("proposal", [
    {"action": "reply", "argument": {"message": None}},
    {"action": "list_openings", "argument": {"date": None}},
    {"action": "book_appointment", "argument": {"slot_id": MONDAY_9, "reason": None}},
    {"action": "cancel_appointment", "argument": {"appointment_id": 1, "reason": None}},
    {"action": "cancel_appointment", "argument": {"appointment_id": 1, "reason": "  "}},
    {"action": "reschedule_appointment", "argument": {"appointment_id": "²", "new_date": "2026-11-24",
                                                      "new_time": "09:00", "reason": "x"}},
    {"action": "send_email", "argument": {"subject": "Hi\r\nBcc: someone@example.org", "message": "x"}},
    {"action": "reply", "argument": {"message": "x" * 5000}},
])
def test_bad_arguments_are_rejected_not_crashing(conn, proposal):
    agent = "reminders" if proposal["action"] == "send_email" else "scheduling"
    assert validate(proposal, TODAY, conn, agent=agent)          # a list of failed checks, no exception


def test_null_message_does_not_crash_the_loop(conn):
    turn = say(conn, ['{"action": "reply", "argument": {"message": null}}', act("reply", message="Hello!")], "hi")
    assert turn.stop_reason == "replied" and turn.reply == "Hello!"


def test_null_in_reminder_job_falls_back_instead_of_crashing(conn):
    book(conn, MONDAY_9)
    script = ['{"action": "send_email", "argument": {"subject": null, "message": null}}'] * 3
    r = reminders.run_once(conn, ScriptedLLM(script), NOW)["results"][0]
    assert r["fallback"] == ["email", "sms"]


def test_one_failing_job_does_not_stop_the_others(conn, monkeypatch):
    book(conn, MONDAY_9)
    book(conn, "S-20261123-0930-P2", patient="Test Patient B")
    real = reminders.run_job
    calls = []

    def flaky(conn_, llm, job, now):
        calls.append(job["appt"]["patient_alias"])
        if len(calls) == 1:
            raise RuntimeError("boom")
        return real(conn_, llm, job, now)
    monkeypatch.setattr(reminders, "run_job", flaky)
    s = reminders.run_once(conn, ScriptedLLM([]), NOW)
    assert s["jobs"] == 2 and "error" in s["results"][0] and s["results"][1]["sent"]


# --- 4, 5: sending is race-safe and never stale -----------------------------------

class FakeSMTP:
    sent = []

    def __init__(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def starttls(self):
        pass

    def login(self, *a):
        pass

    def send_message(self, msg):
        FakeSMTP.sent.append(msg)


@pytest.fixture
def owner(conn, monkeypatch):
    FakeSMTP.sent = []
    monkeypatch.setattr(notify.smtplib, "SMTP", FakeSMTP)
    for mod in (notify.config,):
        monkeypatch.setattr(mod, "SMTP_HOST", "smtp.gmail.com")
        monkeypatch.setattr(mod, "SMTP_PORT", 587)
        monkeypatch.setattr(mod, "SMTP_USER", "sender@gmail.com")
        monkeypatch.setattr(mod, "REAL_CONTACT_ALLOWLIST", {OWNER})
    alias = db.create_new_patient(conn)
    conn.run("UPDATE patients SET first_name='Owner', email=?, phone='555-0150', registered_at='2026-11-20'"
             " WHERE alias = ?", (OWNER, alias))
    return alias


def test_two_runners_send_one_real_email(conn, owner):
    appt = tools.appointment_for_notice(conn, book(conn, MONDAY_9, patient=owner)["id"])
    profile = tools.get_profile(conn, owner)
    first = notify.deliver(conn, appt, profile, "confirmation", "email", "Mon 09:00", subject="x")
    second = notify.deliver(conn, appt, profile, "confirmation", "email", "Mon 09:00", subject="x")
    assert first["status"] == "sent" and second.get("duplicate")
    assert len(FakeSMTP.sent) == 1                                  # the loser never reaches SMTP


def test_cancelled_while_writing_sends_nothing(conn, owner):
    a = book(conn, MONDAY_9, patient=owner)
    appt = tools.appointment_for_notice(conn, a["id"])
    req = tools.prepare_cancel(conn, a["id"], owner, "x")["approval_request"]
    tools.decide_and_apply(conn, req, True, "test")                 # staff cancel it mid-job
    r = notify.deliver(conn, appt, tools.get_profile(conn, owner), "reminder", "email", "Mon 09:00", subject="x")
    assert r.get("stale") and FakeSMTP.sent == [] and notify.list_notifications(conn) == []


def test_calendar_invite_is_rate_limited(conn):
    a = book(conn, MONDAY_9)
    assert tools.send_calendar_invite(conn, PATIENT, a["id"])["ok"]
    again = tools.send_calendar_invite(conn, PATIENT, a["id"])
    assert not again["ok"] and "few minutes ago" in again["error"]


def test_newline_in_subject_cannot_inject_headers(conn, owner):
    appt = tools.appointment_for_notice(conn, book(conn, MONDAY_9, patient=owner)["id"])
    notify.deliver(conn, appt, tools.get_profile(conn, owner), "confirmation", "email", "Mon 09:00",
                   subject="Hi\r\nBcc: someone@example.org")
    assert "\n" not in FakeSMTP.sent[0]["Subject"] and FakeSMTP.sent[0]["Bcc"] is None


# --- 6: allowlisted address belongs to one patient; email digits are not phones ---

def test_allowlisted_email_only_for_one_patient(conn, owner, monkeypatch):
    monkeypatch.setattr(tools.config, "REAL_CONTACT_ALLOWLIST", {OWNER, "sam1990@gmail.com"})
    other = db.create_new_patient(conn)
    r = tools.register_patient(conn, other, "Eve", "Other", "1990-01-01", "555-0170", OWNER, "self-pay",
                               today=TODAY.isoformat())
    assert not r["ok"] and "another patient" in r["invalid_fields"]["email"]
    r = tools.register_patient(conn, other, "Eve", "Other", "1990-01-01", "1990", "eve@example.com", "self-pay",
                               today=TODAY.isoformat())
    assert not r["ok"] and "phone" in r["invalid_fields"]          # '1990' from the email is not a phone


# --- 9: calendar text is escaped --------------------------------------------------

def test_ics_reason_cannot_inject_properties(conn):
    appt = tools.appointment_for_notice(conn, book(conn, MONDAY_9)["id"]) | {"reason": "x\r\nATTENDEE:mailto:a@b.c"}
    ics = notify.make_ics(appt, "Avery", "100 Example Street, Suite 2")
    assert "\r\nATTENDEE" not in ics and "Suite 2" in ics and "Street\\, Suite" in ics


# --- 1: real contacts never reach run records ------------------------------------

def test_run_records_redact_real_contacts(monkeypatch):
    from backend import config
    monkeypatch.setattr(config, "REAL_CONTACT_ALLOWLIST", {OWNER})
    out = redact_real_contacts('{"email": "Owner.Test@gmail.com", "x": "avery@example.com"}')
    assert OWNER not in out.lower() and "[real contact redacted]" in out and "avery@example.com" in out


# --- 14: a local host only counts as a mail catcher on the catcher port ----------

def test_local_relay_on_other_port_is_not_trusted(monkeypatch):
    monkeypatch.setattr(notify.config, "SMTP_HOST", "localhost")
    monkeypatch.setattr(notify.config, "REAL_CONTACT_ALLOWLIST", set())
    monkeypatch.setattr(notify.config, "SMTP_PORT", 25)
    assert not notify.may_send_for_real("stranger@example.org")
    monkeypatch.setattr(notify.config, "SMTP_PORT", 1025)
    assert notify.may_send_for_real("stranger@example.org")       # Mailpit catches everything locally


# --- edge battery: an emergency must never reach the AI agent ---------------------

@pytest.mark.parametrize("text, emergency", [
    ("My face is swelling up and I can't breathe properly after an extraction", True),
    ("I knocked a tooth out and it won't stop bleeding", True),
    ("I just knocked a tooth out playing soccer and there's a lot of blood", True),
    ("my gums bleed a little when I floss", False),
    ("I'm struggling to swallow and my jaw is swollen", True),
    ("it's hard to breathe through my nose since the cold, can I book a cleaning?", False),
    ("my gums are swollen, can I book Monday?", False),
])
def test_emergency_red_flags_go_to_the_front_desk(text, emergency):
    from backend.decision import RuleDecider, route
    out = route(RuleDecider().decide(text))
    assert (out["action"] == "front_desk" and out["reason"] == "emergency") is emergency


def test_emergency_skips_the_llm_even_if_the_router_misjudges(conn):
    from backend.decision import Decision, SafetyNetDecider

    class CalmRouter:                       # a router that wrongly thinks this is routine
        name = "calm"

        def decide(self, text, context=None):
            return Decision("book", {"book": 0.9}, 0.0, 0.0, 0.0, "calm")
    session = Session(conn=conn, patient_alias=PATIENT, today=TODAY)
    turn = run_turn(session, "my face is swelling and I can't breathe", ScriptedLLM([]), SafetyNetDecider(CalmRouter()),
                    lambda r: True, save=False)
    assert turn.stop_reason == "front_desk" and not [s for s in turn.steps if s["kind"] == "MODEL_PROPOSED"]


def test_title_does_not_split_a_history_sentence():
    """Final eval: 'a cleaning on September 20, 2026, with Dr. Okafor, listed as cancelled' was split at 'Dr.' and
    the date-less half was read as a false cancellation claim."""
    from backend.agent import claims_booking, claims_change, sentences
    assert len(sentences("Your cleaning with Dr. Okafor is on Monday. See you then!")) == 2
    assert not claims_change("Your record also shows a cleaning on September 20, 2026, with Dr. Okafor, listed as cancelled.", TODAY)
    assert claims_change("Your appointment with Dr. Okafor on November 30, 2026 was cancelled.", TODAY)
    assert claims_booking("You are booked with Dr. Chen. See you then!", TODAY)
