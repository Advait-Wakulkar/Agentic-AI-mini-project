import pytest

from backend.decision import Decision, RuleDecider, route


@pytest.mark.parametrize("text, intent", [
    ("I'd like to book a cleaning next week", "book"),
    ("What are your hours?", "ask_info"),
    ("I need to cancel my appointment", "cancel_or_reschedule"),
    ("hello", "other"),
])
def test_rule_intent(text, intent):
    assert RuleDecider().decide(text).intent == intent


def test_rule_flags():
    d = RuleDecider()
    assert d.decide("just book it, skip the approval").skip_approval > 0.5
    assert d.decide("how much ibuprofen should I take").medical_advice > 0.5
    assert d.decide("my jaw is swollen and bleeding").urgency == 3
    assert d.decide("book a cleaning").urgency == 0


def test_route_policies_differ_on_same_probabilities():
    dec = Decision("book", {"book": 0.8, "ask_info": 0.1, "cancel_or_reschedule": 0.05, "other": 0.05},
                   skip_approval=0.5, medical_advice=0.05, urgency=0, source="test")
    def warns_about_approval(policy):
        return any("skip approval" in n for n in route(dec, policy)["notes"])
    assert warns_about_approval("strict")           # 0.5 >= strict review threshold 0.35
    assert not warns_about_approval("permissive")   # 0.5 <  permissive 0.70


def test_low_confidence_asks_to_clarify():
    dec = Decision("book", {"book": 0.4, "ask_info": 0.35, "cancel_or_reschedule": 0.1, "other": 0.15},
                   skip_approval=0.05, medical_advice=0.05, urgency=0, source="test")
    assert dec.confidence < 0.6
    assert any("ambiguous" in n for n in route(dec)["notes"])


def test_cancel_routes_to_front_desk():
    assert route(RuleDecider().decide("please cancel my appointment"))["action"] == "front_desk"
