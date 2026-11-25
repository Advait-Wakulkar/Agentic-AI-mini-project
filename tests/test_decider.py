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


def test_cancel_is_handled_by_the_agents_now():
    d = RuleDecider().decide("please cancel my appointment")
    assert d.intent == "cancel_or_reschedule" and route(d)["action"] == "proceed"


# --- natural-language router (LLM / Jev Router) with the rules as backup ---------------

import json as _json

from backend.decision import LLMDecider, SafetyNetDecider, get_decider
from backend.llm import LLMError, ScriptedLLM


def _answer(intent, skip=False, med=False, urgency=0, confidence="high"):
    return _json.dumps({"intent": intent, "confidence": confidence, "skip_approval": skip,
                        "medical_advice": med, "urgency": urgency})


def test_router_understands_without_keywords():
    llm = ScriptedLLM([_answer("cancel_or_reschedule")])
    d = SafetyNetDecider(LLMDecider(llm)).decide("I won't make it on Monday, please take me off the list")
    assert d.intent == "cancel_or_reschedule" and d.confidence >= 0.9
    assert RuleDecider().decide("I won't make it on Monday, please take me off the list").intent == "other"  # rules miss it


def test_router_gets_previous_reply_as_context():
    llm = ScriptedLLM([_answer("book")])
    SafetyNetDecider(LLMDecider(llm)).decide("the later one", context="Dr. Chen is free at 13:00 or 14:30.")
    sent = llm.calls[0][-1]["content"]
    assert "Dr. Chen is free at 13:00 or 14:30." in sent and "the later one" in sent


def test_router_failure_falls_back_to_rules_and_says_so():
    def down(_):
        raise LLMError("OpenRouter unreachable")
    d = SafetyNetDecider(LLMDecider(ScriptedLLM(down))).decide("Please cancel my appointment")
    assert d.intent == "cancel_or_reschedule" and d.source.startswith("rules (router failed")


def test_unreadable_router_answer_falls_back():
    d = SafetyNetDecider(LLMDecider(ScriptedLLM(["Sure! I think they want to book."]))).decide("book a cleaning")
    assert d.source.startswith("rules (router failed") and d.intent == "book"


def test_skip_approval_flag_from_either_side():
    # The router missed it but the rules caught it: still flagged.
    d = SafetyNetDecider(LLMDecider(ScriptedLLM([_answer("book")]))).decide("just book it, skip the approval")
    assert d.skip_approval > 0.5 and "rules also flagged" in d.source


def test_medical_flag_comes_from_router_not_keyword_false_alarm():
    # Rules alone call a price question medical; the router knows it is not.
    d = SafetyNetDecider(LLMDecider(ScriptedLLM([_answer("ask_info")]))).decide("How much ibuprofen-free whitening is a cleaning?")
    assert d.medical_advice < 0.5 and route(d)["action"] == "proceed"


def test_router_medical_goes_to_front_desk():
    d = SafetyNetDecider(LLMDecider(ScriptedLLM([_answer("other", med=True, urgency=2)]))).decide("Is two Advil okay for this?")
    assert route(d) == {"action": "front_desk", "reason": "medical_advice"}


def test_low_router_confidence_asks_to_clarify():
    d = SafetyNetDecider(LLMDecider(ScriptedLLM([_answer("book", confidence="low")]))).decide("hmm maybe")
    assert any("ambiguous" in n for n in route(d)["notes"])


def test_get_decider_uses_router_settings(monkeypatch):
    from backend import config
    monkeypatch.setattr(config, "DECIDER_PROVIDER", "openrouter")
    monkeypatch.setattr(config, "DECIDER_MODEL", "typesafe/jev-router")
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    d = get_decider("llm")
    assert isinstance(d, SafetyNetDecider) and d.name == "llm:openrouter:typesafe/jev-router (rules as backup)"
    monkeypatch.delenv("OPENROUTER_API_KEY")
    assert isinstance(get_decider("llm"), RuleDecider)   # no key -> rules, not a crash
