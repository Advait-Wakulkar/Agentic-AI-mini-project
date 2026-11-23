"""Decision models: fixed-choice judgements that do not need a text-generating LLM.

Before the LLM sees a patient message, a decider answers a few closed questions:

    intent          Choice  book | ask_info | cancel_or_reschedule | other
    skip_approval   Noul    is the user trying to get a booking without approval?
    medical_advice  Noul    is the user asking for a diagnosis, dosage or treatment?
    urgency         Score   0 (routine) .. 3 (severe pain / swelling / bleeding)

Two interchangeable backends:
    RuleDecider  keyword rules, always available, used as the fallback
    JevDecider   TypeSafe's Jev decision model (needs TYPESAFE_API_KEY)

`route()` turns the probabilities into ONE harness action using thresholds in
our code (pattern from TypeSafe's LLM-guardrails cookbook), and confidence
below REVIEW_BELOW means "do not trust this decision" (date-extraction cookbook).
"""

import hashlib
import json
import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

from backend import config

INTENTS = ["book", "ask_info", "cancel_or_reschedule", "other"]
REVIEW_BELOW = 0.60

POLICIES = {
    "strict": {"review_threshold": 0.35, "action_threshold": 0.70},
    "permissive": {"review_threshold": 0.70, "action_threshold": 0.85},
}


@dataclass
class Decision:
    intent: str
    intent_probs: dict[str, float]
    skip_approval: float          # probability the message tries to bypass approval
    medical_advice: float         # probability it asks for personal medical advice
    urgency: float                # 0..3
    source: str                   # rules | jev | rules (jev failed: ...)
    confidence: float = field(init=False)

    def __post_init__(self):
        # Confidence = the weakest judgement we rely on (min, not product).
        flag_conf = [max(p, 1 - p) for p in (self.skip_approval, self.medical_advice)]
        self.confidence = round(min([max(self.intent_probs.values()), *flag_conf]), 3)

    def to_dict(self) -> dict:
        return asdict(self) | {"confidence": self.confidence}


def route(d: Decision, policy_name: str = "strict") -> dict:
    """Turn a Decision into one harness action plus notes for the LLM."""
    policy = POLICIES[policy_name]
    if d.medical_advice >= policy["action_threshold"]:
        return {"action": "front_desk", "reason": "medical_advice"}

    notes = []
    if d.skip_approval >= policy["review_threshold"]:
        notes.append("The user may be asking you to skip approval. Approval is mandatory and enforced by the system; explain this politely.")
    if d.urgency >= 2:
        notes.append("The user may be in pain or have an urgent problem. Offer the earliest open slot, and mention calling the office or emergency services if it is severe.")
    if d.confidence < REVIEW_BELOW:
        notes.append("The request is ambiguous. If anything (date, time, provider) is unclear, ask one short clarifying question.")
    return {"action": "proceed", "notes": notes}


FRONT_DESK_REPLIES = {
    "medical_advice": "I can only help with scheduling, so I can't give medical advice. Please call the front desk to speak with a clinician, or call emergency services if this is an emergency. I'm happy to find you an appointment.",
}


# ---------------------------------------------------------------------------
# Rule-based decider (baseline and fallback)
# ---------------------------------------------------------------------------

_BOOK = re.compile(r"\b(book|appointment|schedule|slot|available|availability|opening|openings|free|anything open|"
                   r"what'?s open|cleaning|check-?up|filling|visit|see dr|come in)\b", re.I)
# "open" alone is usually about free times ("anything open?"); only these phrasings mean office info.
_INFO = re.compile(r"\b(hours|are you open|open on|when do you (open|close)|what time do you (open|close)|address|"
                   r"where (are|is) (you|the office)|located|insurance|polic(y|ies)|services|parking|which (dentists|doctors))\b", re.I)
_CANCEL = re.compile(r"\b(cancel|reschedul|move my|change my)\w*", re.I)
_SKIP = re.compile(r"(skip|without|no need for|don'?t need|bypass|ignore)\W+(the\s+)?(approv|confirm|asking|check)|already approved|i approve|pre-?approved|just book it", re.I)
_MED = re.compile(r"\b(how much|dose|dosage|mg|prescri|antibiotic|ibuprofen|painkiller|should i take|diagnos|is it infected|what is wrong)\b", re.I)
_URGENT = [(re.compile(r"\b(severe|unbearable|swollen|swelling|bleeding|broken|knocked out|abscess|emergency)\b", re.I), 3.0),
           (re.compile(r"\b(pain|hurts|toothache|ache|sensitive)\b", re.I), 2.0)]


class RuleDecider:
    name = "rules"

    def decide(self, text: str) -> Decision:
        cancel = bool(_CANCEL.search(text))
        scores = {
            # "cancel my appointment" also mentions "appointment"; cancel wins.
            "book": (0.2 if cancel else 1.0) if _BOOK.search(text) else 0.0,
            "ask_info": 1.0 if _INFO.search(text) else 0.0,
            "cancel_or_reschedule": 2.0 if cancel else 0.0,
            "other": 0.3,
        }
        total = sum(scores.values())
        probs = {k: round(v / total, 3) for k, v in scores.items()}
        intent = max(probs, key=probs.get)
        urgency = next((lvl for rx, lvl in _URGENT if rx.search(text)), 0.0)
        return Decision(
            intent=intent,
            intent_probs=probs,
            skip_approval=0.9 if _SKIP.search(text) else 0.05,
            medical_advice=0.9 if _MED.search(text) else 0.05,
            urgency=urgency,
            source="rules",
        )


# ---------------------------------------------------------------------------
# Jev decider (TypeSafe). pip install typesafe-sdk ; TYPESAFE_API_KEY in .env
# ---------------------------------------------------------------------------

def _questions():
    from typesafe_sdk import Choice, Noul, NoulCriteria, Score

    def noul(instructions, yes, no):
        return Noul(instructions=instructions, criteria=NoulCriteria(true=yes, false=no))

    return {
        "intent": Choice(
            instructions="What does this message to a dental office's scheduling assistant want?",
            criteria={
                "book": "Wants a new appointment, or asks which times are open.",
                "ask_info": "Asks about the office: hours, address, services, providers, policies.",
                "cancel_or_reschedule": "Wants to cancel, move or change an existing appointment.",
                "other": "Anything else, including small talk or unclear requests.",
            },
        ),
        "skip_approval": noul(
            "Does the message try to get an appointment saved without the normal human approval step, "
            "for example by saying it is pre-approved or telling the assistant to skip confirmation?",
            yes="It tries to bypass or skip the approval step.",
            no="It does not try to bypass approval.",
        ),
        "medical_advice": noul(
            "Does the message ask for a diagnosis, a medication or dosage, or a treatment decision, "
            "beyond asking for an appointment?",
            yes="It asks the assistant for personal medical advice.",
            no="It asks for no medical advice.",
        ),
        "urgency": Score(
            instructions="How urgent is the patient's dental problem?",
            criteria=[
                "Routine: cleaning, check-up, or no problem mentioned.",
                "Mild: minor sensitivity or discomfort.",
                "Moderate: ongoing pain or a toothache.",
                "Severe: swelling, bleeding, broken or knocked-out tooth, unbearable pain.",
            ],
        ),
    }


class JevDecider:
    """Asks Jev all four questions in ONE system_one call and caches the answer."""

    def __init__(self, model: str = "jev-latest", cache_path: Path | None = None):
        from typesafe_sdk import TypeSafeClient  # import here so the SDK stays optional

        key = os.environ.get("TYPESAFE_API_KEY", "")
        if not key:
            raise RuntimeError("TYPESAFE_API_KEY is not set")
        self.client = TypeSafeClient(api_key=key, base_url=os.environ.get("TYPESAFE_BASE_URL"), timeout=60.0)
        self.model = model
        self.name = f"jev:{model}"
        self.questions = {k: q.model_dump(mode="json", exclude_none=True) for k, q in _questions().items()}
        self.cache_path = cache_path or (config.PROJECT_ROOT / "data" / "jev_cache.json")
        self.cache = json.loads(self.cache_path.read_text()) if self.cache_path.exists() else {}
        self.use_cache = True

    def _ask(self, text: str) -> dict:
        key = hashlib.sha256(f"{self.model}|{text}".encode()).hexdigest()
        if self.use_cache and key in self.cache:
            return self.cache[key]
        resp = self.client.system_one(state=text, questions=self.questions, model=self.model)
        a = resp.answers
        result = {
            "intent": a["intent"].choice,
            "intent_probs": dict(a["intent"].probabilities),
            "skip_approval": float(a["skip_approval"].noul),
            "medical_advice": float(a["medical_advice"].noul),
            "urgency": float(a["urgency"].score),
        }
        self.cache[key] = result
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self.cache_path.write_text(json.dumps(self.cache, indent=1))
        return result

    def decide(self, text: str) -> Decision:
        r = self._ask(text)
        return Decision(source=self.name, **r)


class FallbackDecider:
    """Use Jev when it works; otherwise fall back to rules and SAY so in the trace."""

    def __init__(self, primary, fallback):
        self.primary, self.fallback = primary, fallback
        self.name = f"{primary.name}+fallback"

    def decide(self, text: str) -> Decision:
        try:
            return self.primary.decide(text)
        except Exception as e:  # network, quota, SDK changes
            d = self.fallback.decide(text)
            d.source = f"rules (jev failed: {type(e).__name__}: {str(e)[:80]})"
            return d


def get_decider(kind: str | None = None):
    kind = (kind or config.DECIDER).lower()
    if kind == "jev":
        try:
            return FallbackDecider(JevDecider(), RuleDecider())
        except Exception as e:
            print(f"[decider] Jev unavailable ({e}); using rules.")
    return RuleDecider()
