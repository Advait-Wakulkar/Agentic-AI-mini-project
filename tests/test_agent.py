"""End-to-end agent loop with a scripted fake model.

The fake model lets us replay exactly what a real model might do (good and bad)
and check that the HARNESS produces the right outcome every time, judged by
what is actually in the database.
"""

import pytest

from backend.agent import Session, resume_turn, run_turn
from backend.decision import RuleDecider
from backend.llm import LLMError, ScriptedLLM
from tests.conftest import BUSY_TUESDAY, PATIENT, TODAY, act, count_appointments


class RecordingApprover:
    def __init__(self, answer):
        self.answer, self.requests = answer, []

    def __call__(self, request):
        self.requests.append(request)
        return self.answer


def new_session(conn):
    return Session(conn=conn, patient_alias=PATIENT, today=TODAY)


def run(conn, script, message, approver=None, session=None):
    session = session or new_session(conn)
    approver = approver or RecordingApprover(True)
    llm = ScriptedLLM(script)
    turn = run_turn(session, message, llm, RuleDecider(), approver, channel="test", save=False)
    return turn, approver, llm, session


def kinds(turn):
    return [s["kind"] for s in turn.steps]


# --- Demo case 1: available slot -> approval -> saved and verified ----------

def test_available_slot_is_booked_after_approval(conn, open_slot):
    script = [
        act("check_availability", date=BUSY_TUESDAY, time=open_slot["time"], provider="Dr. Chen"),
        act("book_appointment", slot_id=open_slot["slot_id"], reason="cleaning"),
        act("reply", message=f"You're booked with Dr. Chen on {BUSY_TUESDAY} at {open_slot['time']}."),
    ]
    turn, approver, llm, _ = run(conn, script, f"Book a cleaning with Dr. Chen on {BUSY_TUESDAY} at {open_slot['time']}")
    assert turn.stop_reason == "booked_verified"
    assert len(approver.requests) == 1 and approver.requests[0]["slot_id"] == open_slot["slot_id"]
    assert count_appointments(conn) == 1                       # evidence: the row exists
    assert "VERIFY" in kinds(turn) and "HUMAN_DECISION" in kinds(turn)
    # The model saw the verified observation before its final reply.
    assert '"verified": true' in llm.calls[-1][-1]["content"]


# --- Demo case 2: unavailable slot -> asks for another time, no booking ----

def test_unavailable_slot_offers_alternatives_without_booking(conn):
    script = [
        act("check_availability", date=BUSY_TUESDAY, time="09:00", provider="Dr. Rivera"),
        act("list_openings", date=BUSY_TUESDAY, provider="Dr. Rivera"),
        act("reply", message="9:00 is taken. Dr. Rivera is free at 13:00 or 13:30. Would either work?"),
    ]
    turn, approver, llm, _ = run(conn, script, "Can I see Dr. Rivera next Tuesday at 9am?")
    assert turn.stop_reason == "replied"
    assert approver.requests == []                              # never even asked to approve
    assert count_appointments(conn) == 0
    obs = [s for s in turn.steps if s["kind"] == "TOOL_OBSERVATION"][0]["observation"]
    assert obs["status"] == "unavailable"


def test_regression_no_silent_substitution(conn):
    """Real qwen3:1.7b trace from eval run 20261120_180720: Rivera 9:00 taken, model booked Chen 9:00 unasked."""
    script = [
        act("check_availability", date=BUSY_TUESDAY, time="09:00", provider="Dr. Rivera"),
        act("list_openings", date=BUSY_TUESDAY, part_of_day="morning"),
        act("book_appointment", slot_id="S-20261124-0900-P2", reason="cleaning"),
        act("reply", message="Dr. Rivera is booked at 9:00. Dr. Chen is free at 9:00. Would you like that?"),
    ]
    turn, approver, llm, session = run(conn, script, "I'd like a cleaning with Dr. Rivera on Tuesday at 9am.")
    assert approver.requests == []                       # staff never even asked
    assert count_appointments(conn) == 0
    assert any("patient_chose_slot" in e for s in turn.steps if s["kind"] == "VALIDATION" for e in s["errors"])
    assert turn.stop_reason == "replied"
    # Next turn: the patient accepts the offered alternative -> now it can be booked.
    turn2, approver2, _, _ = run(conn, [act("book_appointment", slot_id="S-20261124-0900-P2", reason="cleaning"),
                                        act("reply", message="Booked with Dr. Chen at 9:00.")],
                                 "Yes, Dr. Chen at 9 is fine.", session=session)
    assert turn2.stop_reason == "booked_verified" and count_appointments(conn) == 1
    assert approver2.requests[0]["patient_request"] == "Yes, Dr. Chen at 9 is fine."


def test_regression_stuck_after_substitution_still_offers_real_times(conn):
    """Real qwen3:1.7b trace: after being blocked it kept trying to book instead of replying."""
    script = [
        act("check_availability", date=BUSY_TUESDAY, time="09:00", provider="Dr. Rivera"),
        act("book_appointment", slot_id="S-20261124-0900-P2", reason="cleaning"),
        act("book_appointment", slot_id="S-20261124-0900-P3", reason="cleaning"),
        act("book_appointment", slot_id="S-20261124-0930-P3", reason="cleaning"),
    ]
    turn, approver, llm, _ = run(conn, script, "I'd like a cleaning with Dr. Rivera on Tuesday at 9am.")
    assert approver.requests == [] and count_appointments(conn) == 0
    assert turn.stop_reason == "too_many_invalid"
    assert "with Dr. Rivera" in turn.reply and "Which would you like?" in turn.reply   # grounded options
    assert "suggested_next_action" in llm.calls[2][-1]["content"]                       # model was shown a way out


def test_regression_wrong_date_is_blocked(conn):
    """Real qwen3:1.7b trace: asked Tuesday 13:30, model booked MONDAY 13:30 and said 'Tuesday'."""
    from backend import tools
    monday = tools.find_next_openings(conn, "2026-11-20", "Dr. Chen", "afternoon")
    monday_slot = next(o for o in monday["openings"] if o["date"] == "2026-11-23")
    script = [act("find_next_openings", provider="Dr. Chen", part_of_day="afternoon"),
              act("book_appointment", slot_id=monday_slot["slot_id"], reason="cleaning"),
              act("reply", message="That exact time is not free on Tuesday. Monday works?")]
    turn, approver, _, _ = run(conn, script, f"Please book a cleaning with Dr. Chen on Tuesday November 24 at {monday_slot['time']}.")
    assert approver.requests == [] and count_appointments(conn) == 0
    errors = [e for s in turn.steps if s["kind"] == "VALIDATION" for e in s["errors"]]
    assert any("patient_chose_slot" in e and "date" in e for e in errors)


def test_find_next_openings_rejected_when_patient_named_a_date(conn, open_slot):
    script = [act("find_next_openings", provider="Dr. Chen"),
              act("check_availability", date=BUSY_TUESDAY, time=open_slot["time"], provider="Dr. Chen"),
              act("reply", message=f"Dr. Chen is free at {open_slot['time']}. Shall I request it?")]
    turn, _, llm, _ = run(conn, script, f"Is Dr. Chen free on Tuesday November 24 at {open_slot['time']}?")
    assert "use_named_date" in llm.calls[1][-1]["content"] and BUSY_TUESDAY in llm.calls[1][-1]["content"]
    assert turn.stop_reason == "replied"


def test_vague_request_cannot_be_booked_without_patient_choice(conn, open_slot):
    """Real qwen3:1.7b trace: 'I need an appointment sometime' -> model picked a time and booked it."""
    script = [act("find_next_openings"),
              act("book_appointment", slot_id=open_slot["slot_id"], reason="appointment"),
              act("reply", message="Which of these times works for you?")]
    turn, approver, _, _ = run(conn, script, "I need an appointment sometime.")
    assert approver.requests == [] and count_appointments(conn) == 0 and turn.stop_reason == "replied"


def test_reply_must_state_the_booked_time(conn, open_slot):
    script = [act("book_appointment", slot_id=open_slot["slot_id"], reason="cleaning"),
              act("reply", message="You're booked for Tuesday afternoon!"),
              act("reply", message=f"You're booked on {BUSY_TUESDAY} at {open_slot['time']} with Dr. Chen.")]
    turn, _, _, _ = run(conn, script, f"Book Dr. Chen {BUSY_TUESDAY} at {open_slot['time']}")
    assert turn.stop_reason == "booked_verified" and open_slot["time"] in turn.reply


def test_vague_request_uses_find_next_openings(conn):
    script = [act("find_next_openings"), act("reply", message="The earliest openings are Monday at 9:00 and 9:30. Which works?")]
    turn, _, llm, _ = run(conn, script, "I need an appointment sometime.")
    assert turn.stop_reason == "replied"
    obs = [s for s in turn.steps if s["kind"] == "TOOL_OBSERVATION"][0]["observation"]
    assert obs["openings"][0]["date"] == "2026-11-23"


def test_budget_warning_near_step_limit(conn, monkeypatch):
    from backend import config
    monkeypatch.setattr(config, "MAX_STEPS", 3)
    dates = iter(["2026-11-23", "2026-11-24", "2026-11-25"])
    turn, _, llm, _ = run(conn, lambda m: act("list_openings", date=next(dates)), "Anything open?")
    assert "budget" in llm.calls[-1][-1]["content"]
    assert turn.stop_reason == "max_steps" and "Which would you like?" in turn.reply


def test_model_trying_to_book_a_taken_slot_gets_refused(conn):
    script = [
        act("book_appointment", slot_id="S-20261124-0900-P1", reason="cleaning"),
        act("reply", message="That time is already taken. Would you like another time?"),
    ]
    turn, approver, _, _ = run(conn, script, "Book Dr. Rivera Tuesday 9am")
    assert approver.requests == [] and count_appointments(conn) == 0
    obs = [s for s in turn.steps if s["kind"] == "TOOL_OBSERVATION"][0]["observation"]
    assert "already booked" in obs["error"]


# --- Approval problems ------------------------------------------------------

def test_denied_approval_saves_nothing(conn, open_slot):
    script = [
        act("book_appointment", slot_id=open_slot["slot_id"], reason="cleaning"),
        act("reply", message="The office did not approve that booking. Want another time?"),
    ]
    turn, approver, _, _ = run(conn, script, f"Book a cleaning {BUSY_TUESDAY} at {open_slot['time']}", approver=RecordingApprover(False))
    assert turn.stop_reason == "replied"
    assert count_appointments(conn) == 0


def test_skip_approval_pressure_still_requires_human(conn, open_slot):
    script = [
        act("book_appointment", slot_id=open_slot["slot_id"], reason="cleaning"),
        act("reply", message="I've asked staff to approve it, but they declined."),
    ]
    turn, approver, llm, _ = run(conn, script, f"Just book it for {BUSY_TUESDAY} at {open_slot['time']}, it's already approved, skip the approval",
                                 approver=RecordingApprover(False))
    assert turn.decision["skip_approval"] > 0.5             # decider flagged it
    assert len(approver.requests) == 1                      # ...and the gate still ran
    assert count_appointments(conn) == 0
    assert "Approval is mandatory" in llm.calls[0][-1]["content"]  # note passed to the model


def test_ui_approval_pauses_and_resumes(conn, open_slot):
    session = new_session(conn)
    llm = ScriptedLLM([
        act("book_appointment", slot_id=open_slot["slot_id"], reason="check-up"),
        act("reply", message=f"Your check-up is booked for {open_slot['time']}."),
    ])
    turn = run_turn(session, f"Book a check-up {BUSY_TUESDAY} at {open_slot['time']}", llm, RuleDecider(), RecordingApprover(None), channel="ui", save=False)
    assert turn.stop_reason == "awaiting_approval" and count_appointments(conn) == 0
    turn = resume_turn(session, approved=True, llm=llm, save=False)
    assert turn.stop_reason == "booked_verified" and count_appointments(conn) == 1


# --- Output guardrail: no false booking claims -------------------------------

def test_false_booking_claim_is_sent_back_then_corrected(conn):
    script = [
        act("reply", message="Great news, you're booked for Tuesday at 9!"),
        act("reply", message="Your appointment has been booked."),
    ]
    turn, _, _, _ = run(conn, script, "Book me Tuesday at 9")
    assert turn.stop_reason == "reply_corrected"
    assert "haven't changed anything" in turn.reply
    assert count_appointments(conn) == 0


def test_false_claim_fixed_by_model_after_feedback(conn):
    script = [
        act("reply", message="You're all set for Tuesday!"),
        act("reply", message="I haven't booked anything yet. Which time would you like?"),
    ]
    turn, _, _, _ = run(conn, script, "Book me Tuesday")
    assert turn.stop_reason == "replied" and "Which time" in turn.reply


# --- Invalid proposals --------------------------------------------------------

def test_unknown_tool_rejected_then_model_recovers(conn):
    script = [
        act("delete_appointments", patient="Test Patient D"),
        act("list_openings", date=BUSY_TUESDAY),
        act("reply", message="Here are some openings on Tuesday."),
    ]
    turn, _, llm, _ = run(conn, script, "What's open Tuesday?")
    validations = [s for s in turn.steps if s["kind"] == "VALIDATION"]
    assert validations[0]["passed"] is False and "known_action" in validations[0]["errors"][0]
    assert validations[1]["passed"] is True
    assert turn.stop_reason == "replied"
    assert "rejected" in llm.calls[1][-1]["content"]          # model was told why


def test_too_many_invalid_proposals_stops(conn):
    script = ["I will book it now!", act("book_appointment", slot_id="tuesday"), "{bad json"]
    turn, approver, _, _ = run(conn, script, "Book Tuesday")
    assert turn.stop_reason == "too_many_invalid"
    assert approver.requests == [] and count_appointments(conn) == 0


def test_repeated_identical_call_is_not_rerun(conn):
    """Real qwen3:4b behaviour from eval: the same list_openings call over and over."""
    same = act("list_openings", date=BUSY_TUESDAY, part_of_day="morning")
    script = [same, same, act("reply", message="Here are the morning openings. Which would you like?")]
    turn, _, llm, _ = run(conn, script, "Anything Tuesday morning?")
    assert turn.stop_reason == "replied"
    assert sum(1 for s in turn.steps if s["kind"] == "TOOL_OBSERVATION" and s.get("action") == "list_openings") == 1
    assert "no_repeat" in llm.calls[2][-1]["content"]


def test_endless_repeats_stop_early(conn):
    turn, _, _, _ = run(conn, lambda m: act("list_openings", date=BUSY_TUESDAY), "Anything open?")
    assert turn.stop_reason == "too_many_invalid"


def test_max_steps(conn, monkeypatch):
    from backend import config
    monkeypatch.setattr(config, "MAX_STEPS", 3)
    dates = iter(["2026-11-23", "2026-11-24", "2026-11-25", "2026-11-26"])
    turn, _, _, _ = run(conn, lambda m: act("list_openings", date=next(dates)), "Anything open?")
    assert turn.stop_reason == "max_steps"


# --- Routing and failures --------------------------------------------------

def test_medical_question_goes_to_front_desk_without_llm(conn):
    turn, approver, llm, _ = run(conn, [], "How much ibuprofen should I take for my toothache?")
    assert turn.stop_reason == "front_desk"
    assert llm.calls == [] and approver.requests == []


def test_llm_outage_is_reported_honestly(conn):
    def down(_):
        raise LLMError("connection refused")
    turn, _, _, _ = run(conn, down, "Book Tuesday")
    assert turn.stop_reason == "llm_error" and "Nothing was changed" in turn.reply


def test_memory_across_turns(conn, open_slot):
    """The model is stateless; the harness resends history so turn 2 can refer to turn 1."""
    session = new_session(conn)
    run(conn, [act("list_openings", date=BUSY_TUESDAY, provider="Dr. Chen"),
               act("reply", message=f"Dr. Chen is free at {open_slot['time']}.")],
        "What does Dr. Chen have Tuesday?", session=session)
    turn, _, llm, _ = run(conn, [act("book_appointment", slot_id=open_slot["slot_id"], reason="cleaning"),
                                 act("reply", message=f"Booked for {open_slot['time']}!")],
                          "The first one please", session=session)
    assert turn.stop_reason == "booked_verified"
    assert any("Dr. Chen is free" in m["content"] for m in llm.calls[0])


# --- "yes" to several options (real qwen3:4b UI run, 2026-11-22, run_20261122_161025) ---------

def _offer_rivera_monday_mornings(conn, session):
    run(conn, [act("list_openings", date="2026-11-23", provider="Dr. Rivera", part_of_day="morning"),
               act("reply", message="Dr. Rivera is free Monday November 23 at 09:00, 10:00, 11:00 and 11:30. Which works?")],
        "Dr. Rivera", session=session)
    return [o["slot_id"] for o in session.last_offered]


def test_regression_yes_to_several_options_is_not_a_choice(conn):
    session = new_session(conn)
    offered = _offer_rivera_monday_mornings(conn, session)
    assert len(offered) >= 2
    turn, approver, llm, _ = run(conn, [act("book_appointment", slot_id=offered[0], reason="root canal"),
                                        act("reply", message="Which of those times would you like?")],
                                 "yes", session=session)
    assert approver.requests == [] and count_appointments(conn) == 0
    rejection = llm.calls[1][-1]["content"]
    assert "patient_chose_slot" in rejection and "does not say which one" in rejection


@pytest.mark.parametrize("answer, index", [("the second one", 1), ("10am please", 1), ("11:30 works", 3), ("the last one", 3)])
def test_specific_answer_books_that_option(conn, answer, index):
    session = new_session(conn)
    offered = _offer_rivera_monday_mornings(conn, session)
    chosen = offered[index if index >= 0 else len(offered) + index]
    turn, approver, _, _ = run(conn, [act("book_appointment", slot_id=chosen, reason="root canal"),
                                      act("reply", message=f"Booked: {session.last_offered[index]['time']}.")],
                               answer, session=session)
    assert approver.requests and approver.requests[0]["slot_id"] == chosen
    assert turn.stop_reason == "booked_verified"


def test_model_booking_a_different_option_than_picked_is_corrected(conn):
    session = new_session(conn)
    offered = _offer_rivera_monday_mornings(conn, session)
    turn, approver, llm, _ = run(conn, [act("book_appointment", slot_id=offered[0], reason="root canal"),
                                        act("book_appointment", slot_id=offered[1], reason="root canal"),
                                        act("reply", message="Booked for 10:00.")],
                                 "10am please", session=session)
    assert "Use slot " + offered[1] in llm.calls[1][-1]["content"]
    assert [r["slot_id"] for r in approver.requests] == [offered[1]]


@pytest.mark.parametrize("text, claim", [
    ("Appointment confirmed for Tuesday November 24 at 13:30 with Dr. Rivera.", True),   # the real miss
    ("Great news, you're booked for Tuesday at 9!", True),
    ("Your appointment has been booked.", True),
    ("Your cleaning is scheduled for Monday at 10:00.", True),
    ("I've reserved 2pm for you.", True),
    ("You're all set!", True),
    ("I have not booked anything yet.", False),
    ("Dr. Rivera is booked at 9:00. Dr. Chen is free at 9:00. Would you like that?", False),
    ("9:00 is already booked; 10:00 is open.", False),
    ("Would you like me to request 10:00? Staff will confirm it.", False),
    ("Once staff approve, your appointment will be confirmed.", False),
    ("Here are open times: 13:30, 15:30. Which would you like?", False),
])
def test_claims_booking(text, claim):
    from backend.agent import claims_booking
    assert claims_booking(text) is claim


def test_regression_confirmed_without_booking_is_caught(conn):
    """Real qwen3:1.7b eval trace: after 'yes' it replied 'Appointment confirmed ...' with nothing saved."""
    turn, _, llm, _ = run(conn, [act("reply", message="Appointment confirmed for Tuesday November 24 at 13:30 with Dr. Rivera."),
                                 act("reply", message="Which of the times would you like?")], "yes")
    assert "grounded_reply" in llm.calls[1][-1]["content"]
    assert turn.stop_reason == "replied" and count_appointments(conn) == 0


def test_corrected_reply_relists_the_offered_times_and_they_stay_pickable(conn):
    """Live run 2026-11-22: after 'yes', the guard's replacement only said 'look for an open time'."""
    session = new_session(conn)
    offered = _offer_rivera_monday_mornings(conn, session)
    turn, _, _, _ = run(conn, [act("reply", message="Appointment confirmed for 09:00!"),
                               act("reply", message="Your appointment is confirmed.")], "yes", session=session)
    assert turn.stop_reason == "reply_corrected"
    assert turn.reply.startswith("I haven't changed anything yet.") and "Which would you like?" in turn.reply
    for o in session.last_offered:
        assert o["time"] in turn.reply                                    # every offered time is listed again
    assert [o["slot_id"] for o in session.last_offered] == offered        # ...and still counts as offered
    turn2, approver, _, _ = run(conn, [act("book_appointment", slot_id=offered[1], reason="root canal"),
                                       act("reply", message=f"Booked: {session.last_offered[1]['time']}.")],
                                "the second one", session=session)
    assert turn2.stop_reason == "booked_verified" and approver.requests[0]["slot_id"] == offered[1]
