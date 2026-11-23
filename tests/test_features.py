"""Cancel / reschedule, the router + two agents, and the persisted staff queue.

Scripted model, real database (private test schema). Each outcome is checked
in the database, not from what the model said.
"""

from backend import tools
from backend.agent import Session, choose_agent, run_turn
from backend.decision import RuleDecider
from backend.llm import ScriptedLLM
from tests.conftest import BUSY_TUESDAY, PATIENT, TODAY, act, count_appointments


class Approver:
    def __init__(self, answer):
        self.answer, self.requests = answer, []

    def __call__(self, request):
        self.requests.append(request)
        return self.answer


def session_for(conn):
    return Session(conn=conn, patient_alias=PATIENT, today=TODAY)


def say(conn, session, script, message, approve=True):
    approver = Approver(approve)
    llm = ScriptedLLM(script)
    turn = run_turn(session, message, llm, RuleDecider(), approver, channel="test", save=False)
    return turn, approver, llm


def book(conn, session, slot):
    """Give the patient an existing booking through the normal approved path."""
    req = tools.prepare_booking(conn, slot["slot_id"], PATIENT, "cleaning")["approval_request"]
    result = tools.decide_and_apply(conn, req, True, "test")
    assert result["verified"]
    return tools.list_appointments(conn, PATIENT)[-1]


def kinds(turn):
    return [s["kind"] for s in turn.steps]


# --- cancel --------------------------------------------------------------------

def test_cancel_needs_approval_and_is_verified(conn, open_slot):
    session = session_for(conn)
    appt = book(conn, session, open_slot)
    turn, approver, _ = say(conn, session, [
        act("list_my_appointments"),
        act("cancel_appointment", appointment_id=appt["id"], reason="patient request"),
        act("reply", message="Your appointment has been cancelled."),
    ], "Please cancel my appointment")
    assert approver.requests[0]["action"] == "cancel"
    assert turn.stop_reason == "cancelled_verified"
    assert count_appointments(conn) == 0
    row = conn.one("SELECT status, cancel_approval_id FROM appointments WHERE id = ?", (appt["id"],))
    assert row["status"] == "cancelled" and row["cancel_approval_id"]     # kept for the audit trail
    assert tools.slot_details(conn, open_slot["slot_id"])["booked"] is False  # slot is free again


def test_denied_cancel_changes_nothing(conn, open_slot):
    session = session_for(conn)
    appt = book(conn, session, open_slot)
    turn, _, _ = say(conn, session, [
        act("list_my_appointments"),
        act("cancel_appointment", appointment_id=appt["id"], reason="x"),
        act("reply", message="Staff did not approve the cancellation; your appointment is unchanged."),
    ], "cancel my appointment", approve=False)
    assert turn.stop_reason == "replied" and count_appointments(conn) == 1


def test_no_cancel_unless_the_patient_asked(conn, open_slot):
    session = session_for(conn)
    appt = book(conn, session, open_slot)
    turn, approver, llm = say(conn, session, [
        act("list_my_appointments"),
        act("cancel_appointment", appointment_id=appt["id"], reason="tidy up"),
        act("reply", message="What can I help you with?"),
    ], "What appointments do I have?")
    assert approver.requests == [] and count_appointments(conn) == 1
    assert "patient_asked" in llm.calls[2][-1]["content"]


def test_cannot_cancel_someone_elses_appointment(conn):
    other = conn.one("SELECT id FROM appointments WHERE patient_alias = 'Test Patient D' LIMIT 1")
    session = session_for(conn)
    turn, approver, llm = say(conn, session, [
        act("list_my_appointments"),
        act("cancel_appointment", appointment_id=other["id"], reason="x"),
        act("reply", message="I couldn't find that appointment."),
    ], f"cancel appointment {other['id']}")
    assert approver.requests == []
    assert "not an upcoming appointment of this patient" in llm.calls[2][-1]["content"]
    assert conn.one("SELECT status FROM appointments WHERE id = ?", (other["id"],))["status"] == "booked"


def test_which_appointment_when_there_are_several(conn):
    session = session_for(conn)
    mon = tools.list_openings(conn, "2026-11-23", provider="Dr. Chen")["openings"][0]
    wed = tools.list_openings(conn, "2026-11-25", provider="Dr. Chen")["openings"][0]
    a1 = book(conn, session, mon)
    book(conn, session, wed)
    turn, approver, llm = say(conn, session, [
        act("list_my_appointments"),
        act("cancel_appointment", appointment_id=a1["id"], reason="x"),
        act("reply", message="Which appointment would you like to cancel?"),
    ], "cancel my appointment")
    assert approver.requests == [] and "patient_chose_appointment" in llm.calls[2][-1]["content"]
    turn2, approver2, _ = say(conn, session, [
        act("list_my_appointments"),
        act("cancel_appointment", appointment_id=a1["id"], reason="x"),
        act("reply", message="Your Monday appointment is cancelled."),
    ], "the Monday one")
    assert turn2.stop_reason == "cancelled_verified" and count_appointments(conn) == 1


def test_false_cancel_claim_is_caught(conn, open_slot):
    session = session_for(conn)
    book(conn, session, open_slot)
    turn, _, llm = say(conn, session, [
        act("reply", message="Done, your appointment has been cancelled."),
        act("reply", message="Which appointment should I cancel?"),
    ], "cancel it")
    assert "grounded_reply" in llm.calls[1][-1]["content"] and count_appointments(conn) == 1


# --- reschedule ---------------------------------------------------------------

def test_reschedule_moves_atomically(conn, open_slot):
    session = session_for(conn)
    appt = book(conn, session, open_slot)
    new = tools.list_openings(conn, "2026-11-25", provider="Dr. Chen")["openings"][0]
    turn, approver, _ = say(conn, session, [
        act("list_my_appointments"),
        act("reschedule_appointment", appointment_id=appt["id"], new_date="2026-11-25", new_time=new["time"], provider="Dr. Chen", reason="conflict"),
        act("reply", message=f"Moved to Wednesday November 25 at {new['time']} with Dr. Chen."),
    ], f"Can you move my appointment to Wednesday November 25 at {new['time']}?")
    assert approver.requests[0]["action"] == "reschedule" and approver.requests[0]["from"]["slot_id"] == open_slot["slot_id"]
    assert turn.stop_reason == "rescheduled_verified"
    assert [a["slot_id"] for a in tools.list_appointments(conn, PATIENT)] == [new["slot_id"]]


def test_reschedule_to_a_time_the_patient_did_not_pick_is_blocked(conn, open_slot):
    session = session_for(conn)
    appt = book(conn, session, open_slot)
    new = tools.list_openings(conn, "2026-11-25", provider="Dr. Chen")["openings"][0]
    turn, approver, llm = say(conn, session, [
        act("list_my_appointments"),
        act("reschedule_appointment", appointment_id=appt["id"], new_date="2026-11-25", new_time=new["time"], reason="x"),
        act("reply", message="What day and time would you like instead?"),
    ], "I need to reschedule")
    assert approver.requests == [] and "patient_chose_slot" in llm.calls[2][-1]["content"]


def test_reschedule_rolls_back_if_new_slot_was_taken(conn, open_slot):
    session = session_for(conn)
    appt = book(conn, session, open_slot)
    new = tools.list_openings(conn, "2026-11-25", provider="Dr. Chen")["openings"][0]
    req = tools.prepare_reschedule(conn, appt["id"], new["slot_id"], PATIENT, "x")["approval_request"]
    taker = tools.prepare_booking(conn, new["slot_id"], "Test Patient B", "x")["approval_request"]
    assert tools.decide_and_apply(conn, taker, True, "test")["verified"]          # someone else gets it first
    result = tools.decide_and_apply(conn, req, True, "test")
    assert result["verified"] is False and "taken" in result["saved"]["error"]
    assert [a["slot_id"] for a in tools.list_appointments(conn, PATIENT)] == [open_slot["slot_id"]]  # old one intact


# --- router + agents ---------------------------------------------------------------

def test_router_sends_office_questions_to_faq_agent(conn):
    session = session_for(conn)
    turn, _, llm = say(conn, session, [act("get_office_info", topic="hours"),
                                       act("reply", message="We're open weekdays 9 to 5.")], "What are your hours?")
    assert turn.agent == "faq" and "FAQ agent" in llm.calls[0][0]["content"]
    assert any(s["kind"] == "ROUTER" and s["to_agent"] == "faq" for s in turn.steps)


def test_faq_agent_cannot_use_scheduling_tools(conn, open_slot):
    session = session_for(conn)
    turn, approver, llm = say(conn, session, [
        act("book_appointment", slot_id=open_slot["slot_id"], reason="x"),
        act("reply", message="I can help with office questions."),
    ], "What are your hours?")
    assert approver.requests == [] and "agent_tools" in llm.calls[1][-1]["content"]


def test_faq_agent_hands_off_to_scheduling(conn):
    session = session_for(conn)
    turn, _, llm = say(conn, session, [
        act("get_office_info", topic="hours"),
        act("transfer", to="scheduling"),
        act("find_next_openings"),
        act("reply", message="We're open 9-5. The earliest openings are Monday at 9:00 and 9:30. Which works?"),
    ], "What are your hours?")
    assert any(s["kind"] == "HANDOFF" and s["to_agent"] == "scheduling" for s in turn.steps)
    assert turn.agent == "scheduling" and "SCHEDULING agent" in llm.calls[-1][0]["content"]


def test_only_one_handoff_per_turn(conn):
    session = session_for(conn)
    turn, _, llm = say(conn, session, [act("transfer", to="scheduling"), act("transfer", to="faq"),
                                       act("reply", message="How can I help?")], "Where is the office located?")
    assert "no further transfers" in llm.calls[2][-1]["content"]


def test_follow_up_stays_with_current_agent(conn):
    session = session_for(conn)
    session.active_agent = "scheduling"
    assert choose_agent(session, RuleDecider().decide("yes"))[0] == "scheduling"
    session.active_agent = "faq"
    assert choose_agent(session, RuleDecider().decide("the second one"))[0] == "faq"
    assert choose_agent(session, RuleDecider().decide("book me a cleaning"))[0] == "scheduling"


# --- staff queue -----------------------------------------------------------------

def test_every_request_is_queued_and_closed(conn, open_slot):
    session = session_for(conn)
    say(conn, session, [act("book_appointment", slot_id=open_slot["slot_id"], reason="cleaning"),
                        act("reply", message=f"Booked for {open_slot['time']}.")],
        f"Book Dr. Chen {BUSY_TUESDAY} at {open_slot['time']}")
    rows = tools.list_requests(conn)
    assert len(rows) == 1 and rows[0]["status"] == "approved" and rows[0]["outcome"] == "verified"
    assert rows[0]["request"]["patient_request"].startswith("Book Dr. Chen")


def test_ui_request_stays_pending_in_queue(conn, open_slot):
    session = session_for(conn)
    turn, _, _ = say(conn, session, [act("book_appointment", slot_id=open_slot["slot_id"], reason="cleaning")],
                     f"Book Dr. Chen {BUSY_TUESDAY} at {open_slot['time']}", approve=None)
    assert turn.stop_reason == "awaiting_approval" and "sent" in turn.reply
    pending = tools.list_requests(conn, "pending")
    assert len(pending) == 1 and pending[0]["session_id"] == session.id and count_appointments(conn) == 0



def test_regression_guessed_appointment_id_gets_real_ids(conn, open_slot):
    """Real qwen3:1.7b eval trace: cancel_appointment(123) before looking anything up (real id was 120)."""
    session = session_for(conn)
    appt = book(conn, session, open_slot)
    turn, approver, llm = say(conn, session, [
        act("cancel_appointment", appointment_id=99999, reason="x"),
        act("cancel_appointment", appointment_id=appt["id"], reason="patient request"),
        act("reply", message="Your appointment has been cancelled."),
    ], "Please cancel my appointment.")
    first = llm.calls[1][-1]["content"]
    assert "look_up_first" in first and f'"appointment_id": {appt["id"]}' in first
    assert turn.stop_reason == "cancelled_verified"


def test_regression_cancel_request_cannot_become_reschedule(conn, open_slot):
    """Real qwen3:1.7b eval trace: 'Please cancel my appointment' -> reschedule_appointment."""
    session = session_for(conn)
    appt = book(conn, session, open_slot)
    turn, approver, llm = say(conn, session, [
        act("list_my_appointments"),
        act("reschedule_appointment", appointment_id=appt["id"], new_date="2026-11-25", new_time="14:00", reason="x"),
        act("reply", message="Should I cancel it?"),
    ], "Please cancel my appointment.")
    assert approver.requests == []
    assert "use cancel_appointment" in llm.calls[2][-1]["content"]


def test_reschedule_to_taken_time_offers_alternatives(conn, open_slot):
    session = session_for(conn)
    appt = book(conn, session, open_slot)
    turn, approver, llm = say(conn, session, [
        act("list_my_appointments"),
        act("reschedule_appointment", appointment_id=appt["id"], new_date=BUSY_TUESDAY, new_time="09:00", provider="Dr. Rivera", reason="x"),
        act("reply", message="9:00 is taken. Which of these would you like instead?"),
    ], f"Move my appointment to Tuesday at 9am with Dr. Rivera")
    assert approver.requests == []
    obs = llm.calls[2][-1]["content"]
    assert '"status": "unavailable"' in obs and "alternatives" in obs


def test_regression_cancel_not_handed_to_faq_agent(conn, open_slot):
    """Real qwen3:1.7b eval trace: after look_up_first it transferred to FAQ, which then claimed 'canceled'."""
    session = session_for(conn)
    book(conn, session, open_slot)
    turn, _, llm = say(conn, session, [
        act("cancel_appointment", appointment_id=123, reason="x"),
        act("transfer", to="faq"),
        act("reply", message="Your appointment has been canceled."),
        act("reply", message="Your appointment has been canceled."),
    ], "Please cancel my appointment.")
    assert "suggested_next_action" in llm.calls[1][-1]["content"]          # real id offered
    assert "Stay and finish it" in llm.calls[2][-1]["content"]              # transfer refused
    assert turn.agent == "scheduling" and turn.stop_reason == "reply_corrected"
    assert "Your upcoming appointments:" in turn.reply and count_appointments(conn) == 1


# --- regressions from the logged demo run (evidence/demo_20261123_135243.md) -------------------

def test_regression_known_id_and_retry_are_not_blocked(conn, open_slot):
    """Demo: reschedule with the correct id #114 was rejected (look_up_first), then the correct retry was
    blocked as a 'repeat' although the first attempt never ran. Both must now go through."""
    session = session_for(conn)
    appt = book(conn, session, open_slot)
    new = tools.list_openings(conn, "2026-11-25", provider="Dr. Chen")["openings"][0]
    turn, approver, llm = say(conn, session, [
        act("reschedule_appointment", appointment_id=appt["id"], new_date="2026-11-25", new_time=new["time"], provider="Dr. Chen", reason="x"),
        act("reply", message=f"Moved to Wednesday November 25 at {new['time']}."),
    ], f"something came up, can we push that to Wednesday November 25 at {new['time']} instead?")
    assert turn.stop_reason == "rescheduled_verified", [s.get("errors") for s in turn.steps if s["kind"] == "VALIDATION"]
    assert len(approver.requests) == 1


def test_regression_retry_after_rejection_is_not_a_repeat(conn, open_slot):
    session = session_for(conn)
    appt = book(conn, session, open_slot)
    turn, approver, llm = say(conn, session, [
        act("cancel_appointment", appointment_id=99999, reason="x"),          # rejected: not theirs
        act("cancel_appointment", appointment_id=appt["id"], reason="x"),     # correct
        act("cancel_appointment", appointment_id=appt["id"], reason="x"),     # (would be a real repeat)
        act("reply", message="Your appointment is cancelled."),
    ], "please cancel the whole thing")
    assert turn.stop_reason == "cancelled_verified" and len(approver.requests) == 1


def test_regression_cannot_book_for_a_friend(conn):
    """Demo: 'book my friend Test Patient B' -> booked for the signed-in patient, reply said 'booked for Test Patient B'."""
    session = session_for(conn)
    slot = tools.list_openings(conn, "2026-11-23", provider="Dr. Rivera")["openings"][0]
    turn, approver, llm = say(conn, session, [
        act("book_appointment", slot_id=slot["slot_id"], reason="Booking for Test Patient B"),
        act("reply", message="Appointment booked for Test Patient B at 10:00."),
        act("reply", message="Appointment booked for Test Patient B at 10:00."),
    ], "Can you book my friend Test Patient B in for 10am Monday too?")
    assert approver.requests == [] and count_appointments(conn) == 0
    assert "only_this_patient" in llm.calls[1][-1]["content"]
    assert turn.stop_reason == "reply_corrected" and "Test Patient B" not in turn.reply


def test_my_son_counts_as_someone_else(conn, open_slot):
    session = session_for(conn)
    turn, approver, llm = say(conn, session, [
        act("book_appointment", slot_id=open_slot["slot_id"], reason="x"),
        act("reply", message="I can only book for you; your son will need to contact the office."),
    ], f"Book my son in on {BUSY_TUESDAY} at {open_slot['time']}")
    assert approver.requests == [] and "only_this_patient" in llm.calls[1][-1]["content"]


def test_regression_only_one_booking_per_message(conn):
    """Demo: 'book 9, 9:30 and 10 so I have options' queued two bookings."""
    session = session_for(conn)
    s1, s2 = tools.list_openings(conn, "2026-11-23", provider="Dr. Chen")["openings"][:2]
    turn, approver, llm = say(conn, session, [
        act("book_appointment", slot_id=s1["slot_id"], reason="x"),
        act("book_appointment", slot_id=s2["slot_id"], reason="x"),
        act("reply", message=f"Booked {s1['time']}. Do you want another one?"),
    ], f"Book me at {s1['time']} and {s2['time']} on Monday November 23 with Dr. Chen")
    assert len(approver.requests) == 1 and count_appointments(conn) == 1
    assert "one_request" in llm.calls[2][-1]["content"]


def test_regression_emergency_gets_urgent_reply(conn):
    from backend.decision import Decision, route
    d = Decision("other", {"book": 0.03, "ask_info": 0.03, "cancel_or_reschedule": 0.04, "other": 0.9},
                 0.05, 0.9, 3.0, "test")
    assert route(d) == {"action": "front_desk", "reason": "emergency"}
    session = session_for(conn)

    class Emergency(RuleDecider):
        def decide(self, text, context=None):
            return d

    turn = run_turn(session, "I knocked a tooth out and there's a lot of blood", ScriptedLLM([]), Emergency(),
                    Approver(True), channel="test", save=False)
    assert turn.stop_reason == "front_desk" and "emergency services" in turn.reply and "right now" in turn.reply


def test_insurance_and_prices_have_honest_answers(conn):
    assert "cannot confirm" in tools.get_office_info(conn, "insurance")["info"]
    assert "quote" in tools.get_office_info(conn, "prices")["info"]
