"""Patient record, new-patient intake and the SIMULATED insurance check.

Scripted model, real database (private test schema). Outcomes are checked in the
database, not from what the model said.
"""

import pytest
from fastapi.testclient import TestClient

from backend import api, db, tools
from backend.agent import Session, run_turn
from backend.decision import RuleDecider
from backend.llm import ScriptedLLM
from tests.conftest import BUSY_TUESDAY, PATIENT, TODAY, act

DAY = TODAY.isoformat()
DETAILS = ("I'm Jordan Testcase, born 1995-02-28, phone 555-0142, email jordan@example.com, "
           "insurance Delta Dental member DD4400123")
GOOD = dict(first_name="Jordan", last_name="Testcase", date_of_birth="1995-02-28", phone="555-0142",
            email="jordan@example.com", insurance_provider="Delta Dental", insurance_member_id="DD4400123")


class Approver:
    def __init__(self, answer=True):
        self.answer, self.requests = answer, []

    def __call__(self, request):
        self.requests.append(request)
        return self.answer


def say(session, script, message, approve=True):
    approver = Approver(approve)
    turn = run_turn(session, message, ScriptedLLM(script), RuleDecider(), approver, channel="test", save=False)
    return turn, approver


def rejections(turn):
    return [e for s in turn.steps if s["kind"] == "VALIDATION" and not s["passed"] for e in s["errors"]]


@pytest.fixture
def new_patient(conn):
    return db.create_new_patient(conn)


# --- returning patients: history and context ------------------------------------

def test_history_has_profile_and_past_visits(conn):
    h = tools.get_my_history(conn, PATIENT, DAY)
    assert h["registered"] and h["profile"]["first_name"] == "Avery"
    assert h["summary"]["completed_visits"] == 3
    statuses = {v["status"] for v in h["past_visits"]}
    assert statuses == {"completed", "cancelled"}
    assert all(v["date"] < DAY for v in h["past_visits"])          # all in the past, newest first
    assert h["past_visits"] == sorted(h["past_visits"], key=lambda v: (v["date"], v["time"]), reverse=True)


def test_history_is_only_this_patients(conn):
    b = tools.get_my_history(conn, "Test Patient B", DAY)
    assert b["profile"]["first_name"] == "Blake" and b["summary"]["no_shows"] == 1
    assert not any("Avery" in str(v) for v in b["past_visits"])


def test_prompt_context_returning_vs_new(conn, new_patient):
    ctx = tools.patient_context(conn, PATIENT, DAY)
    assert "RETURNING PATIENT" in ctx and "Avery Example" in ctx and "3 completed" in ctx
    assert "NEW PATIENT" in tools.patient_context(conn, new_patient, DAY)


def test_agent_answers_history_from_the_record(conn):
    session = Session(conn=conn, patient_alias=PATIENT, today=TODAY)
    last = tools.get_my_history(conn, PATIENT, DAY)["summary"]["last_visit"]
    turn, _ = say(session, [act("get_my_history"),
                            act("reply", message=f"Your last visit was a {last['service']} on {last['date']}.")],
                  "when was I last in?")
    obs = next(s for s in turn.steps if s["kind"] == "TOOL_OBSERVATION" and s.get("action") == "get_my_history")
    assert obs["observation"]["profile"]["first_name"] == "Avery"
    assert turn.stop_reason == "replied"


# --- new patients: intake -------------------------------------------------------

def test_register_rejects_real_looking_details(conn, new_patient):
    r = tools.register_patient(conn, new_patient, **(GOOD | {"phone": "617-867-5309", "email": "jordan@gmail.com",
                                                          "date_of_birth": "1995-02-30", "insurance_member_id": None}),
                               today=DAY)
    assert not r["ok"] and set(r["invalid_fields"]) == {"phone", "email", "date_of_birth", "insurance_member_id"}
    assert not tools.is_registered(conn, new_patient)                # nothing saved


def test_register_saves_fictional_details_once(conn, new_patient):
    r = tools.register_patient(conn, new_patient, **GOOD, today=DAY)
    assert r["ok"] and tools.is_registered(conn, new_patient)
    assert tools.get_profile(conn, new_patient)["phone"] == "555-0142"
    again = tools.register_patient(conn, new_patient, **GOOD, today=DAY)
    assert not again["ok"] and "already registered" in again["error"]


def test_self_pay_needs_no_member_id(conn, new_patient):
    r = tools.register_patient(conn, new_patient, **(GOOD | {"insurance_provider": "no insurance",
                                                          "insurance_member_id": None}), today=DAY)
    assert r["ok"] and tools.get_profile(conn, new_patient)["insurance_status"] == "self_pay"


def test_new_patient_cannot_book_before_registering(conn, new_patient, open_slot):
    session = Session(conn=conn, patient_alias=new_patient, today=TODAY)
    turn, approver = say(session, [act("book_appointment", slot_id=open_slot["slot_id"], reason="cleaning"),
                                   act("reply", message="I need a few details first. What is your name?")],
                         f"book {BUSY_TUESDAY} at {open_slot['time']} with {open_slot['provider']}")
    assert any("patient_registered" in e for e in rejections(turn))
    assert approver.requests == [] and tools.list_appointments(conn, new_patient) == []


def test_register_refuses_invented_details(conn, new_patient):
    session = Session(conn=conn, patient_alias=new_patient, today=TODAY)
    turn, _ = say(session, [act("register_patient", **GOOD),
                            act("reply", message="Could you tell me your date of birth, phone and email?")],
                  "I'm Jordan Testcase, I'd like to register")
    errors = rejections(turn)
    assert errors and "details_from_patient" in errors[0]
    for field in ("date_of_birth", "phone", "email", "insurance_provider"):
        assert field in errors[0]
    assert not tools.is_registered(conn, new_patient)


def test_intake_then_booking_shows_profile_to_staff(conn, new_patient, open_slot):
    session = Session(conn=conn, patient_alias=new_patient, today=TODAY)
    turn, _ = say(session, [act("register_patient", **GOOD),
                            act("reply", message="Thanks Jordan, you're registered. Which day suits you?")], DETAILS)
    assert tools.is_registered(conn, new_patient) and turn.stop_reason == "replied"

    when = f"{BUSY_TUESDAY} at {open_slot['time']} with {open_slot['provider']}"
    turn, approver = say(session, [act("book_appointment", slot_id=open_slot["slot_id"], reason="cleaning"),
                                   act("reply", message=f"Booked: {when}.")], f"book {when}")
    card = approver.requests[0]["patient_profile"]
    assert card["name"] == "Jordan Testcase" and card["new_patient"] and card["insurance"] == "Delta Dental"
    assert turn.stop_reason == "booked_verified" and len(tools.list_appointments(conn, new_patient)) == 1


# --- simulated insurance check --------------------------------------------------

@pytest.mark.parametrize("plan, member, status, in_network", [
    ("Delta Dental", "DD1048275", "active", True),
    ("Humana", "HU5512345", "active", False),
    ("Aetna", "AE5531000", "inactive", None),
    ("Acme Dental Plus", "AC123456", "payer_not_found", None),
])
def test_insurance_check_rules(conn, plan, member, status, in_network):
    r = tools.verify_insurance(conn, "Test Patient B", plan, member)
    assert r["ok"] and r["simulated"] and r["status"] == status and r.get("in_network") == in_network
    assert r["steps"] and "X12 270" in r["steps"][0]


def test_insurance_timeout_is_a_reported_failure(conn):
    r = tools.verify_insurance(conn, "Test Patient B", "Aetna", "AE5531999")
    assert not r["ok"] and r["status"] == "payer_timeout" and "front desk" in r["error"]


def test_insurance_result_is_stored_and_reset(conn):
    tools.verify_insurance(conn, PATIENT)                                   # plan on file: Delta Dental
    assert tools.get_profile(conn, PATIENT)["insurance_status"] == "verified_in_network"
    db.reset_to_seed(conn)
    assert tools.get_profile(conn, PATIENT)["insurance_status"] == "unverified"


def test_plan_claim_blocked_until_the_check_says_in_network(conn):
    claim = "Good news: Delta Dental is accepted here and your cleanings are covered."
    session = Session(conn=conn, patient_alias=PATIENT, today=TODAY)
    turn, _ = say(session, [act("reply", message=claim), act("reply", message=claim)], "do you take my insurance?")
    assert turn.stop_reason == "reply_corrected"                           # no check yet: guard replaces it

    session = Session(conn=conn, patient_alias=PATIENT, today=TODAY)
    turn, _ = say(session, [act("verify_insurance"), act("reply", message=claim)], "can you check my insurance?")
    assert turn.stop_reason == "replied" and turn.reply == claim           # the simulated check said in network


def test_patients_own_phone_is_not_an_invented_number(conn):
    session = Session(conn=conn, patient_alias=PATIENT, today=TODAY)
    turn, _ = say(session, [act("reply", message="We have your phone as 555-0101.")], "what number do you have for me?")
    assert turn.stop_reason == "replied"
    turn, _ = say(session, [act("reply", message="Call us at 555-0177."), act("reply", message="Call us at 555-0177.")],
                  "how do I reach you?")
    assert turn.stop_reason == "reply_corrected"                           # a made-up number is still blocked
    # Live UI run: explaining the demo range was flagged as an invented number.
    turn, _ = say(session, [act("reply", message="Please use a number in the 555-0100 to 555-0199 range.")], "which number?")
    assert turn.stop_reason == "replied"


def test_reset_removes_new_patients(conn, new_patient):
    tools.register_patient(conn, new_patient, **GOOD, today=DAY)
    db.reset_to_seed(conn)
    assert tools.get_profile(conn, new_patient) is None


# --- API ------------------------------------------------------------------------

@pytest.fixture
def client(monkeypatch, tmp_path, conn):
    monkeypatch.setattr(api.config, "EVIDENCE_DIR", tmp_path)
    monkeypatch.setattr(api.config, "today", lambda: TODAY)
    monkeypatch.setattr(api.config, "SUPABASE_URL", "")
    monkeypatch.setattr(api.config, "SUPABASE_ANON_KEY", "")
    api._state.update(conn=conn, sessions={}, decider=RuleDecider())
    yield TestClient(api.app)
    api._state.update(conn=None, sessions={}, llm=None, decider=None)


def test_api_new_patient_session_and_history(client):
    r = client.post("/api/session", json={"patient": "new"}).json()
    alias = r["patient"]
    assert alias.startswith(db.NEW_PATIENT_PREFIX)
    h = client.get("/api/history", params={"patient": alias}).json()
    assert h["registered"] is False
    assert client.get("/api/history", params={"patient": "Test Patient A"}).json()["profile"]["first_name"] == "Avery"
    assert client.get("/api/history", params={"patient": "New Patient ZZZZ"}).status_code == 400   # must exist


# --- regressions found by the eval / demo run with Claude Haiku 5.5 --------------

def test_intake_and_history_sentences_are_not_booking_claims():
    from backend.agent import claims_booking, claims_change
    assert not claims_booking("Your name, date of birth (1994-03-03), and self-pay status are all set.", TODAY)
    assert not claims_change("A cleaning that was scheduled for 2026-09-20 was cancelled (noted as cancelled by you).", TODAY)
    assert not claims_booking("A cleaning that was scheduled for 2026-09-20 was cancelled.", TODAY)
    # still caught:
    assert claims_booking("You're all set for Tuesday at 9:00.", TODAY)
    assert claims_booking("Your cleaning is all set.", TODAY)
    assert claims_change("Your appointment on 2026-11-30 was cancelled.", TODAY)     # a future date is a claim


def test_example_demo_number_is_not_an_invented_phone(conn):
    session = Session(conn=conn, patient_alias=PATIENT, today=TODAY)
    msg = "Please use a number in the demo range (for example, 555-0142) and an @example.com email."
    turn, _ = say(session, [act("reply", message=msg)], "what phone should I use?")
    assert turn.stop_reason == "replied"


def test_new_patient_fallback_asks_for_details(conn, new_patient):
    session = Session(conn=conn, patient_alias=new_patient, today=TODAY)
    bad = "Your appointment is confirmed for Monday at 9:00."          # a false claim, twice: Python replaces it
    turn, _ = say(session, [act("reply", message=bad), act("reply", message=bad)], "hi, I'm new")
    assert turn.stop_reason == "reply_corrected"
    assert "date of birth" in turn.reply and "example.com" in turn.reply


def test_first_one_means_first_listed_not_alphabetical(conn):
    """Demo run: reply listed '1. 09:00 Dr. Rivera  2. 09:00 Dr. Chen'; 'the first one' was read as Dr. Chen."""
    monday = "2026-11-23"
    opens = tools.list_openings(conn, monday)["openings"]
    t = next(o["time"] for o in opens if o["provider"] == "Dr. Rivera"
             and any(p["time"] == o["time"] and p["provider"] == "Dr. Chen" for p in opens))
    rivera = next(o for o in opens if o["time"] == t and o["provider"] == "Dr. Rivera")
    session = Session(conn=conn, patient_alias=PATIENT, today=TODAY)
    say(session, [act("list_openings", date=monday),
                  act("reply", message=f"Monday November 23:\n1. {t} with Dr. Rivera\n2. {t} with Dr. Chen\nWhich one?")],
        "what do you have monday?")
    assert session.last_offered[0]["provider"] == "Dr. Rivera"
    turn, approver = say(session, [act("book_appointment", slot_id=rivera["slot_id"], reason="cleaning"),
                                   act("reply", message=f"Booked Monday November 23 at {t} with Dr. Rivera.")],
                         "the first one please")
    assert [r["slot_id"] for r in approver.requests] == [rivera["slot_id"]]
    assert turn.stop_reason == "booked_verified"


def test_picking_an_offered_time_always_goes_to_scheduling(conn):
    """Demo run: the reply offered times and an insurance check; Jev sent 'the first one please' to the FAQ agent."""
    from backend.agent import choose_agent
    from backend.decision import Decision
    session = Session(conn=conn, patient_alias=PATIENT, today=TODAY)
    session.active_agent = "faq"
    session.last_offered = [tools.slot_details(conn, s) for s in ("S-20261123-0900-P1", "S-20261123-0900-P2")]
    faq_intent = Decision(intent="ask_info", intent_probs={"ask_info": 0.9}, skip_approval=0.0, medical_advice=0.0,
                          urgency=0, source="test")
    assert choose_agent(session, faq_intent, "the first one please")[0] == "scheduling"
    assert choose_agent(session, faq_intent, "what are your hours?")[0] == "faq"     # not a pick: router decides


def test_provider_named_by_role_picks_the_slot(conn):
    """Live demo: 'the 9:30 with the hygienist' (two 9:30 options) was blocked; the hygienist is Dr. Okafor."""
    from backend.agent import named_providers, picked_option
    assert named_providers("the 9:30 with the hygienist please") == ["Okafor"]
    offered = [tools.slot_details(conn, s) for s in ("S-20261123-0930-P2", "S-20261123-0930-P3")]
    offered = [o for o in offered if not o["booked"]]
    if len(offered) == 2:
        assert picked_option("the 9:30 with the hygienist", offered, TODAY)["provider"] == "Dr. Okafor"
