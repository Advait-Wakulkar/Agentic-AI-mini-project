"""Agents: a front-desk router, a SCHEDULING agent and an FAQ agent.

One user message = one "turn".

    0. Router (decision model, no LLM): front desk for medical questions,
       otherwise picks the agent: scheduling (book / cancel / reschedule) or faq.
    Then the chosen agent's loop repeats:
    1. LLM proposes one action (JSON)              <- model decision
    2. Python parses + validates it, including     <- harness check
       "is this tool one THIS agent may use?"
    3. Python runs the tool, the approval gate,    <- program action
       or a transfer to the other agent
    4. The result goes back as an OBSERVATION      <- observation
    5. State is updated; go to 1 until a stop condition

Booking, cancelling and rescheduling all go through the same gate: the request
is stored in the staff queue, a HUMAN approves or denies, Python carries it out
and re-reads the database to verify.

Stop conditions are decided by Python, not the model:
    replied               agent chose 'reply' (answer, question, alternatives)
    booked_verified       a booking was approved, saved and re-read from the DB
    cancelled_verified    a cancellation was approved, applied and re-read
    rescheduled_verified  a reschedule was approved, applied and re-read
    awaiting_approval     waiting for staff (UI); resumed by resume_turn()
    front_desk            router sent the message to staff (medical advice)
    reply_corrected       agent twice claimed something not in the DB; reply replaced
    too_many_invalid      MAX_INVALID rejected proposals in one turn
    max_steps             MAX_STEPS passes without finishing
    llm_error             the model could not be reached (reported honestly)
"""

import json
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Callable, Optional

from backend import config, tools
from backend.decision import FRONT_DESK_REPLIES, route
from backend.llm import LLMError
from backend.prompts import build_system_prompt
from backend.validator import parse_proposal, validate

# Approver: gets the approval request, returns True (approve), False (deny) or None (decide later, e.g. UI).
Approver = Callable[[dict], Optional[bool]]

AGENTS = ("scheduling", "faq")
GATED = {"book_appointment": "book", "cancel_appointment": "cancel", "reschedule_appointment": "reschedule"}
VERIFIED_STOP = {"book": "booked_verified", "cancel": "cancelled_verified", "reschedule": "rescheduled_verified"}

# "confirmed" is a claim; "call for confirmation" is not (found in the demo re-run).
_CLAIM_WORD = re.compile(r"\b(confirmed|booked|scheduled|reserved|all set|you'?re set|is set)\b", re.I)
_CHANGE_WORD = re.compile(r"\b(cancell?ed|rescheduled|moved|changed)\b", re.I)
_NOT_A_CLAIM = re.compile(
    r"\b(not|n't|no|nothing|never|cannot|unable|yet|already|fully|taken|unavailable|"
    r"if|once|after|before|would|shall|should|want|need|can I|could|may I|please (choose|confirm|pick))\b|\?",
    re.I,
)


def _claims(text: str, word: re.Pattern) -> bool:
    for sentence in re.split(r"(?<=[.!?])\s+|\n+", text):
        if word.search(sentence) and not _NOT_A_CLAIM.search(sentence):
            if re.search(r"\b(is|are) booked\b", sentence, re.I) and not re.search(r"\b(you|your)\b", sentence, re.I):
                continue  # "Dr. Rivera is booked at 9:00" describes the schedule, not the patient's booking
            return True
    return False


def claims_booking(text: str) -> bool:
    """Does any sentence tell the patient an appointment IS booked/confirmed?

    Found in eval (qwen3:1.7b, 2026-11-22): "Appointment confirmed for Tuesday ..." with
    nothing saved slipped past the earlier phrase list. Now any sentence with a
    confirm/booked/scheduled/reserved word counts, unless it is negated, a question,
    conditional, or about a time being unavailable ("9:00 is already booked").
    """
    return _claims(text, _CLAIM_WORD)


def claims_change(text: str) -> bool:
    """Does any sentence say an appointment WAS cancelled / rescheduled / moved?"""
    return _claims(text, _CHANGE_WORD)


@dataclass
class Session:
    """Everything the agents remember between turns (the model itself is stateless)."""
    conn: object
    patient_alias: str
    today: date = field(default_factory=config.today)
    prompt_variant: str = "baseline"
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    history: list[dict] = field(default_factory=list)   # user / assistant / observation messages
    pending: Optional[dict] = None                       # turn paused for human approval
    last_offered: list = field(default_factory=list)     # slots named in the assistant's LAST reply, earliest first
    active_agent: str = "scheduling"                     # who handled the last turn (follow-ups stay there)
    last_user_message: str = ""
    last_reply: str = ""                                 # the router reads it to understand short follow-ups
    last_intent: str = ""                                # router intent of the previous message
    owner_id: Optional[str] = None                       # signed-in user who owns this conversation
    events: list = field(default_factory=list)           # finished turns the patient UI has not shown yet


@dataclass
class Turn:
    user_message: str
    agent: str = "scheduling"
    steps: list[dict] = field(default_factory=list)
    invalid: int = 0
    guard_retried: bool = False
    transferred: bool = False
    looked_up: bool = False                              # list_my_appointments seen this turn
    unknown_provider: Optional[str] = None               # a dentist name the tools did not recognise
    unavailable_seen: bool = False                       # requested time turned out to be taken / not offered
    turn_slots: set = field(default_factory=set)         # slot ids returned by tools this turn
    calls_made: set = field(default_factory=set)         # (action, args) already executed this turn
    booked: Optional[dict] = None                        # verified book or reschedule (the new slot)
    changed: Optional[dict] = None                       # verified cancel or reschedule
    done_action: Optional[str] = None
    decision: Optional[dict] = None
    reply: str = ""
    stop_reason: str = ""
    started: float = field(default_factory=time.time)
    record_path: Optional[str] = None
    on_step: Optional[Callable[[dict], None]] = None   # live trace printer (CLI)


def _log(turn: Turn, kind: str, **data):
    entry = {"step": len(turn.steps) + 1, "kind": kind, "agent": turn.agent, **data}
    turn.steps.append(entry)
    if turn.on_step:
        turn.on_step(entry)
    return entry


def _observe(session: Session, turn: Turn, obs: dict):
    # Step-budget awareness (found in eval: small models searched until they ran out of steps).
    left = config.MAX_STEPS - sum(1 for s in turn.steps if s["kind"] == "MODEL_PROPOSED")
    if left <= 2:
        obs = obs | {"budget": f"Only {left} action(s) left in this turn. Your next action should be 'reply' to the patient "
                               "with what you found (or a question)."}
    session.history.append({"role": "user", "content": "OBSERVATION: " + json.dumps(obs)})


def _finish_request(session: Session, turn: Turn, request: dict, approved: bool, channel: str,
                    request_id: Optional[int]) -> dict:
    """Record the HUMAN decision, carry it out, re-read the DB, and close the staff-queue entry."""
    result = tools.decide_and_apply(session.conn, request, approved, channel)
    action = request.get("action", "book")
    _log(turn, "HUMAN_DECISION", approved=approved, channel=channel, approval_id=result["approval_id"], request=request)
    if request_id is not None:
        tools.close_request(session.conn, request_id, "approved" if approved else "denied", channel,
                            result["approval_id"], result["outcome"])
    if not approved:
        return {"ok": False, "approved": False, "action": action,
                "detail": f"The staff member DENIED this {action} request. Nothing changed. Tell the patient."}
    _log(turn, "VERIFY", action=action, saved=result["saved"], verified=result["verified"], slot_id=request["slot_id"])
    if result["saved"].get("ok") and result["verified"]:
        turn.done_action = action
        if action in ("book", "reschedule"):
            turn.booked = request
        if action in ("cancel", "reschedule"):
            turn.changed = request
        return {"ok": True, "approved": True, "verified": True, "action": action, "request": request}
    return {"ok": False, "approved": True, "verified": False, "action": action,
            "error": result["saved"].get("error", "Could not be verified.")}


PROVIDER_NAMES = ["Rivera", "Chen", "Okafor"]


def mentions_date(text: str, d: date, today: date) -> bool:
    """Does the patient's message name this date? (ISO, 'November 24', '11/24', weekday, tomorrow)."""
    t = text.lower()
    if d.isoformat() in t or f"{d.month}/{d.day}" in t:
        return True
    if re.search(rf"\b{d:%B}\s+{d.day}(st|nd|rd|th)?\b".lower(), t) or re.search(rf"\b{d.day}(st|nd|rd|th)?\s+(of\s+)?{d:%B}\b".lower(), t):
        return True
    if "tomorrow" in t and (d - today).days == 1:
        return True
    weekday = f"{d:%A}".lower()
    # A bare weekday means the next one (or the one after, for "next Tuesday" said early in the week).
    return weekday in t and 0 < (d - today).days <= 13 and not re.search(r"\b(january|february|march|april|may|june|july|august|september|october|november|december)\b", t)


def named_dates(text: str, today: date, horizon: int = 21) -> list[date]:
    """Upcoming dates the message names, earliest first (empty for 'sometime', 'asap')."""
    return [d for d in (today + timedelta(days=i) for i in range(1, horizon)) if mentions_date(text, d, today)]


def mentions_time(text: str, hhmm: str) -> bool:
    """Does the message name this time? ('13:30', '1:30pm', '9am', '9:00', 'at 9')."""
    h, m = int(hhmm[:2]), int(hhmm[3:])
    h12 = h - 12 if h > 12 else h
    suffix = "pm" if h >= 12 else "am"
    t = text.lower()
    patterns = [rf"\b{h:02d}:{m:02d}\b", rf"\b{h}:{m:02d}\b", rf"\b{h12}:{m:02d}\s*({suffix})?\b"]
    if m == 0:
        patterns += [rf"\b{h12}\s*{suffix}\b", rf"\b{h12}\s*o'?clock\b", rf"\bat {h12}\b(?!:)"]
    return any(re.search(p, t) for p in patterns)


def _substitution_problem(session: Session, turn: Turn, slot_id: str) -> Optional[str]:
    """Python rule: only book a slot the PATIENT chose.

    Found in eval (qwen3:1.7b): booking Dr. Chen when Dr. Rivera was asked for,
    booking Monday when Tuesday was asked for, and picking a time on its own
    for "I need an appointment sometime". Found in a UI run (qwen3:4b): the
    patient said "yes" to FOUR offered times and the model picked 09:00.

    A slot is bookable only if
      (a) the last reply offered it and this message picks it out: the only
          option offered, an ordinal ("the first one"), or its time (plus
          provider/date when needed to tell options apart), or
      (b) this message names its date AND time (and provider, if one was named).
    """
    slot = tools.slot_details(session.conn, slot_id)
    if slot is None:
        return None  # prepare_booking reports a missing slot
    msg = turn.user_message
    offered = session.last_offered
    if any(o["slot_id"] == slot_id for o in offered):
        chosen = picked_option(msg, offered, session.today)
        if chosen is not None and chosen["slot_id"] == slot_id:
            return None
        if chosen is None:
            listing = "; ".join(f"{o['date']} {o['time']} with {o['provider']}" for o in offered)
            return (f"check 'patient_chose_slot': {len(offered)} times were offered ({listing}) and the patient's "
                    "reply does not say which one. Ask which time they want; do not pick for them.")
        return (f"check 'patient_chose_slot': the patient picked {chosen['date']} {chosen['time']} with "
                f"{chosen['provider']}, not this slot. Use slot {chosen['slot_id']} instead.")
    named = [p for p in PROVIDER_NAMES if p.lower() in msg.lower()]
    what = f"{slot['date']} {slot['time']} with {slot['provider']}"
    if named and not any(p in slot["provider"] for p in named):
        return (f"check 'patient_chose_slot': the patient asked for Dr. {' or Dr. '.join(named)}, but {what} is with "
                f"another provider. Do not substitute. Reply with the options and let the patient choose.")
    date_ok = mentions_date(msg, date.fromisoformat(slot["date"]), session.today)
    time_ok = mentions_time(msg, slot["time"])
    if not (date_ok and time_ok):
        missing = " and ".join(x for x, ok in (("date", date_ok), ("time", time_ok)) if not ok)
        return (f"check 'patient_chose_slot': the patient has not chosen {what} (the {missing} does not match their "
                "message). Reply with 2-3 options and let the patient choose before booking.")
    return None


_ASKED = {
    "cancel": re.compile(r"\b(cancel\w*|drop|call off|don'?t need)\b", re.I),
    "reschedule": re.compile(r"\b(resched\w*|move|change|switch|push|postpone|different (time|day)|another (time|day))\b", re.I),
}


def _appointment_problem(session: Session, turn: Turn, appointment_id, kind: str) -> Optional[str]:
    """Python rule: only cancel/reschedule what the PATIENT asked to, and only the appointment they meant.

    - The patient must have asked for a change in this message or the previous one
      (so "yes" after "Shall I cancel your Monday visit?" counts).
    - With several upcoming appointments, the messages must pick one out (its
      date/time, or an ordinal); otherwise the agent has to ask which.
    """
    msg, prev = turn.user_message, session.last_user_message
    if not _ASKED[kind].search(f"{msg} {prev}"):
        # Found in eval (qwen3:1.7b): "Please cancel my appointment" became a reschedule request.
        other = "reschedule" if kind == "cancel" else "cancel"
        hint = f" They asked to {other}; use {other}_appointment." if _ASKED[other].search(f"{msg} {prev}") else ""
        return (f"check 'patient_asked': the patient has not asked to {kind} an appointment.{hint} "
                "Do not change appointments unless they ask.")
    mine = tools.list_appointments(session.conn, session.patient_alias)
    if not any(str(a["id"]) == str(appointment_id) for a in mine) or len(mine) == 1:
        return None  # not theirs -> the tool reports it; only one -> nothing to choose between
    options = [{"slot_id": str(a["id"]), "date": a["date"], "time": a["time"], "provider": a["provider"]} for a in mine]
    picked = picked_option(msg, options, session.today) or (picked_option(prev, options, session.today) if prev else None)
    if picked is not None and picked["slot_id"] == str(appointment_id):
        return None
    listing = "; ".join(f"#{o['slot_id']} {o['date']} {o['time']} with {o['provider']}" for o in options)
    return (f"check 'patient_chose_appointment': the patient has {len(mine)} appointments ({listing}) and it is not "
            f"clear that they meant #{appointment_id}. Ask which appointment to {kind}.")


ORDINALS = {"first": 0, "1st": 0, "earliest": 0, "second": 1, "2nd": 1, "third": 2, "3rd": 2,
            "fourth": 3, "4th": 3, "last": -1, "latest": -1}


def picked_option(message: str, offered: list[dict], today: date) -> Optional[dict]:
    """Which ONE of the offered slots does the patient's message pick? None if unclear."""
    if not offered:
        return None
    t = message.lower()
    if len(offered) == 1:
        return offered[0]                       # "yes" / "that works" can only mean the one option
    for word, idx in ORDINALS.items():
        if re.search(rf"\b{word}\b", t):
            return offered[idx] if -len(offered) <= idx < len(offered) else None
    candidates = [o for o in offered if mentions_time(t, o["time"])]
    named = [p for p in PROVIDER_NAMES if p.lower() in t]
    if named:
        candidates = [o for o in candidates if any(p in o["provider"] for p in named)]
    if len(candidates) > 1:
        dated = [o for o in candidates if mentions_date(t, date.fromisoformat(o["date"]), today)]
        candidates = dated or candidates
    if not candidates:  # "cancel my Wednesday one": a date alone can pick it out
        candidates = [o for o in offered if mentions_date(t, date.fromisoformat(o["date"]), today)]
    return candidates[0] if len(candidates) == 1 else None


def _offered_in_reply(session: Session, turn: Turn, reply: str) -> list[dict]:
    """The slots this reply actually put in front of the patient (time named in the text), earliest first."""
    # Candidates: this turn's tool results plus last turn's options (a reply may repeat them).
    candidates = set(turn.turn_slots) | {o["slot_id"] for o in session.last_offered}
    slots = [s for s in (tools.slot_details(session.conn, sid) for sid in candidates) if s and not s["booked"]]
    offered = [s for s in slots if mentions_time(reply, s["time"])]
    named = [p for p in PROVIDER_NAMES if p.lower() in reply.lower()]
    if named:  # "Dr. Chen is free at 9:00" offers Chen's 9:00, not Okafor's
        offered = [s for s in offered if any(p in s["provider"] for p in named)] or offered
    dated = [s for s in offered if mentions_date(reply, date.fromisoformat(s["date"]), session.today)]
    offered = dated or offered
    return sorted(offered, key=lambda s: (s["date"], s["time"], s["provider"]))


def _remember_slots(turn: Turn, action: str, obs: dict) -> None:
    if action == "check_availability" and obs.get("status") in ("unavailable", "not_offered"):
        turn.unavailable_seen = True
    for item in obs.get("openings", []) + obs.get("slots", []) + obs.get("alternatives", []):
        turn.turn_slots.add(item["slot_id"])


def _grounded_options(session: Session, turn: Turn, limit: int = 3) -> list[dict]:
    """Real open slots to offer, re-checked in the DB.

    Slots tools returned this turn (named provider first); if this turn found none,
    e.g. the patient just answered "yes", the options from the previous reply.
    """
    if not turn.turn_slots and session.last_offered:
        still_open = [s for s in (tools.slot_details(session.conn, o["slot_id"]) for o in session.last_offered)
                      if s and not s["booked"]]
        return still_open[:max(limit, 5)]
    named = [p for p in PROVIDER_NAMES if p.lower() in turn.user_message.lower()]
    slots = [s for s in (tools.slot_details(session.conn, sid) for sid in turn.turn_slots) if s and not s["booked"]]
    slots.sort(key=lambda s: (not any(p in s["provider"] for p in named), s["date"], s["time"]))
    return slots[:limit]


def _options_text(slots: list[dict]) -> str:
    fmt = lambda s: f"{date.fromisoformat(s['date']):%A %B} {date.fromisoformat(s['date']).day} at {s['time']} with {s['provider']}"
    return "; ".join(fmt(s) for s in slots)


def _fallback_reply(session: Session, turn: Turn, base: str) -> str:
    """When the model fails, Python still gives the patient something true and useful."""
    if turn.agent == "faq":
        return ("Sorry, I can't confirm that here. I can tell you about our hours, address, services, dentists, policies, "
                "insurance and prices, or help you book, cancel or move an appointment.")
    asked_for = re.findall(r"\b(?i:dr|doctor)\.?\s+([A-Z][a-zA-Z'-]+)", turn.user_message)
    unknown = turn.unknown_provider or next((f"Dr. {n}" for n in asked_for if n.capitalize() not in PROVIDER_NAMES), None)
    if unknown:
        turn.unknown_provider = unknown
        # Found in the demo re-run: "Dr. House on Saturday at midnight" ended in a generic error.
        base = (f"{base} We don't have a {turn.unknown_provider}; our dentists are Dr. Rivera, Dr. Chen and Dr. Okafor, "
                "and we're open weekdays 9:00-17:00.").strip()
    if (turn.decision or {}).get("intent") == "cancel_or_reschedule":
        mine = tools.list_appointments(session.conn, session.patient_alias)
        if not mine:
            return f"{base} You don't have any upcoming appointments."
        listing = "; ".join(f"{date.fromisoformat(a['date']):%A %B} {date.fromisoformat(a['date']).day} at {a['time']} "
                            f"with {a['provider']}" for a in mine)
        return f"{base} Your upcoming appointments: {listing}. Which one do you mean, and what would you like to do?"
    options = _grounded_options(session, turn)
    if options:
        return f"{base} The open times are: {_options_text(options)}. Which would you like?"
    return f"{base} Could you tell me a day or time that works for you?"


def _stop(session: Session, turn: Turn, reason: str, reply: str) -> Turn:
    turn.stop_reason = reason
    turn.reply = reply
    # Remember exactly which options the patient was shown, so their answer can be checked next turn.
    session.last_offered = _offered_in_reply(session, turn, reply) if reason != "awaiting_approval" else []
    session.active_agent = turn.agent
    session.last_reply = reply
    _log(turn, "STOP", stop_reason=reason, reply=reply)
    return turn


_SOMEONE_ELSE = re.compile(
    r"\b(my|our|his|her|their)\s+(friend|friends|son|daughter|kid|kids|child|children|wife|husband|partner|boyfriend|"
    r"girlfriend|mom|mum|mother|dad|father|brother|sister|colleague|coworker|co-worker|boss|neighbou?r|roommate|"
    r"grand(ma|mother|pa|father)|aunt|uncle|cousin|family)\b|\bfor (him|her|them)\b", re.I)


def _other_aliases(session: Session) -> list[str]:
    return [r["alias"] for r in session.conn.query("SELECT alias FROM patients WHERE alias <> ?", (session.patient_alias,))]


def _names_other_patient(session: Session, text: str) -> Optional[str]:
    t = text.lower()
    return next((a for a in _other_aliases(session) if a.lower() in t), None)


def _someone_else_problem(session: Session, turn: Turn) -> Optional[str]:
    """Python rule: an agent acts only for the signed-in patient, never on someone else's behalf."""
    msg = turn.user_message
    who = _names_other_patient(session, msg) or (m.group(0) if (m := _SOMEONE_ELSE.search(msg)) else None)
    if not who:
        return None
    return (f"check 'only_this_patient': the request is about someone else ('{who}'). You can only book, cancel or "
            f"reschedule for {session.patient_alias}. Tell the patient the other person needs to contact the office "
            "(or sign in themselves). Do not claim anything was done for them.")


_WEEKDAY_DATE = re.compile(r"\b(monday|tuesday|wednesday|thursday|friday|saturday|sunday),?\s+(?:the\s+)?"
                           r"(january|february|march|april|may|june|july|august|september|october|november|december)\s+"
                           r"(\d{1,2})(?:st|nd|rd|th)?\b", re.I)
_MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
           "november", "december"]
_TIME = re.compile(r"\b(\d{1,2}):(\d{2})\s*(am|pm|a\.m\.|p\.m\.)?|\b(\d{1,2})\s*(am|pm|a\.m\.|p\.m\.)", re.I)
_PLAN_CLAIM = re.compile(r"\b(we|the office|they|you)\s+(do\s+|does\s+)?(accept|take|cover|work with|are in[- ]network)\b|"
                         r"\b(is|are)\s+(accepted|covered|in[- ]network)\b", re.I)
_NEGATION = re.compile(r"\b(not|n't|cannot|can't|unable|no)\b|\?", re.I)
# Only sentences ABOUT insurance count (found in eval: "We take a 12:00 to 1:00 lunch break" was flagged).
_INSURANCE_CONTEXT = re.compile(r"\b(insurance|insurer|plans?|ppo|hmo|coverage|covered|in[- ]network|delta|aetna|cigna|"
                                r"metlife|guardian|humana|united ?healthcare|blue cross|medicaid|medicare)\b", re.I)
_PHONE = re.compile(r"((?:\+\d{1,3}[\s.-]?)?\(?\b\d{3}\)?[\s.-]?\d{3}[\s.-]\d{4}\b)")   # (555) 123-4567, 555-123-4567


def _weekday_mismatch(text: str, today: date) -> Optional[str]:
    """'Wednesday, November 26' when Nov 26 is a Thursday. Found in the demo re-run."""
    for wd, month, day in _WEEKDAY_DATE.findall(text):
        m = _MONTHS.index(month.lower()) + 1
        year = today.year + (1 if m < today.month else 0)
        try:
            d = date(year, m, int(day))
        except ValueError:
            continue
        if f"{d:%A}".lower() != wd.lower():
            return f"{d:%B} {d.day} is a {d:%A}, not a {wd.capitalize()}."
    return None


def times_in(text: str) -> set[str]:
    """Clock times written in a reply, as HH:MM (24h)."""
    out = set()
    for h1, m1, ap1, h2, ap2 in _TIME.findall(text):
        h, m, ap = (int(h1), int(m1), ap1) if h1 else (int(h2), 0, ap2)
        ap = ap.lower().replace(".", "")
        if ap == "pm" and h < 12:
            h += 12
        if ap == "am" and h == 12:
            h = 0
        if 0 <= h < 24 and 0 <= m < 60:
            out.add(f"{h:02d}:{m:02d}")
    return out


def _claims_plan_accepted(text: str) -> bool:
    for sentence in re.split(r"(?<=[.!?])\s+", text):
        if _PLAN_CLAIM.search(sentence) and _INSURANCE_CONTEXT.search(sentence) and not _NEGATION.search(sentence):
            return True
    return False


def _invented_phone(text: str) -> Optional[str]:
    """A phone number the office info does not contain. Found in eval: the model made up '(555) 123-4567'."""
    known = " ".join(tools.OFFICE_INFO.values())
    for m in _PHONE.findall(text):
        digits = re.sub(r"\D", "", m)
        if len(digits) >= 7 and digits not in re.sub(r"\D", "", known):
            return m.strip()
    return None


def _confirmation(request: dict) -> str:
    d = date.fromisoformat(request["date"])
    when = f"{d:%A %B} {d.day} at {request['time']} with {request['provider']}"
    if request.get("action") == "reschedule":
        return f"Your appointment has been moved to {when}."
    return f"Your appointment is confirmed: {when}."


def _reply_problem(session: Session, turn: Turn, message: str) -> tuple[Optional[str], str]:
    """The first thing wrong with a reply, and the reply Python would send instead. (None, "") if it is fine."""
    other = _names_other_patient(session, message)
    if other:
        # Demo run: "book my friend Test Patient B" -> reply said "booked for Test Patient B".
        return (f"your reply mentions another patient ({other}); you only act for {session.patient_alias}.",
                f"I can only help with appointments for you ({session.patient_alias}). Anyone else needs to contact the office.")
    # The replacement must say what really happened (found in eval: a booking WAS made, and the replacement
    # reply said "I haven't changed anything yet").
    done = _confirmation(turn.booked) if turn.booked else ("Your appointment has been cancelled." if turn.changed else None)
    if claims_booking(message) and not turn.booked:
        return ("your reply says an appointment is booked, but nothing was approved, saved and verified in this turn.",
                done or _fallback_reply(session, turn, "I haven't changed anything yet."))
    if claims_change(message) and not turn.changed:
        what = "your reply says an appointment was cancelled or moved, but nothing like that was approved and verified."
        if turn.booked:
            return what + " A NEW appointment was booked; say exactly that.", _confirmation(turn.booked) + " Your other appointments are unchanged."
        return what, done or _fallback_reply(session, turn, "I haven't changed anything yet.")
    wrong = _weekday_mismatch(message, session.today)
    if wrong:
        return (f"a weekday in your reply is wrong: {wrong} Use the dates exactly as the tools returned them.",
                _confirmation(turn.booked) if turn.booked else _fallback_reply(session, turn, "Let me restate that."))
    if turn.booked:
        b = turn.booked
        if not mentions_time(message, b["time"]):
            # Eval: booked Monday 13:30 and told the patient "Tuesday".
            return (f"the verified appointment is {b['date']} at {b['time']} with {b['provider']}; state exactly this.",
                    _confirmation(b))
        allowed = {b["time"]} | ({b["from"]["time"]} if b.get("from") else set())
        extra = times_in(message) - allowed
        if extra and claims_booking(message):
            # Demo re-run: one slot booked, reply said "I've booked your 9:00 AM and 1:00 PM slots".
            return (f"only {b['time']} was booked; your reply also mentions {', '.join(sorted(extra))} as if booked.",
                    _confirmation(b))
    phone = _invented_phone(message)
    if phone:
        return (f"your reply gives a phone number ({phone}) that is not in the office info; never invent contact details.",
                _fallback_reply(session, turn, "") if turn.agent == "scheduling" and not turn.booked
                else (_confirmation(turn.booked) if turn.booked else
                      "Please contact the office directly; I don't have a phone number to share here."))
    if _claims_plan_accepted(message):
        # Demo re-run: "We do accept Delta Dental" although the office info says coverage is confirmed by staff.
        return ("the office info does not say which insurance plans are accepted; do not claim any plan is accepted.",
                tools.OFFICE_INFO["insurance"])
    return None, ""


_DATE_NEGATION = re.compile(r"\b(not|n't|no good|doesn't work|instead of|other than|except|besides|rather than)\b", re.I)


def _wrong_date_problem(session: Session, turn: Turn, looked_up: str) -> Optional[str]:
    """Python rule: if the patient named a day, look up THAT day. Found in the demo re-run: "push that to
    Wednesday afternoon" -> the model searched Thursday and called it Wednesday."""
    if _DATE_NEGATION.search(turn.user_message):
        return None   # "Monday doesn't work, what else?" names a day to avoid
    named = named_dates(turn.user_message, session.today)
    if named and looked_up not in {d.isoformat() for d in named}:
        d = named[0]
        return (f"check 'use_named_date': the patient asked for {d:%A} {d.isoformat()}, but you looked up {looked_up}. "
                f"Look up {d.isoformat()}.")
    return None


_NEW_VISIT = re.compile(r"\b(another|new|additional|second|extra|also|as well|too|separate)\b", re.I)


def _book_while_rescheduling_problem(session: Session, turn: Turn, action: str) -> Optional[str]:
    """Python rule: if the patient was moving an appointment, the time they pick moves it; it is not a new booking.
    Found in the demo re-run: 'the first one is fine' after reschedule options became a second appointment."""
    this_intent = (turn.decision or {}).get("intent")
    moving = session.last_intent == "cancel_or_reschedule" or this_intent == "cancel_or_reschedule"
    # (also within one message: found in eval, "move my appointment to Wednesday 9:00" -> book_appointment)
    if action != "book_appointment" or not moving or _NEW_VISIT.search(turn.user_message):
        return None
    mine = tools.list_my_appointments(session.conn, session.patient_alias)["appointments"]
    if not mine:
        return None
    listing = "; ".join(f"#{m['appointment_id']} {m['date']} {m['time']} with {m['provider']}" for m in mine)
    return (f"check 'reschedule_not_book': the patient is moving an existing appointment ({listing}), not adding one. "
            "Use reschedule_appointment with the appointment_id and the new date and time they picked.")


def _one_request_problem(turn: Turn) -> Optional[str]:
    """Python rule: at most one book / cancel / reschedule request per patient message."""
    if any(s["kind"] == "APPROVAL_REQUESTED" for s in turn.steps):
        # Found in the demo run: "book 9, 9:30 and 10 so I have options" queued two bookings.
        return ("check 'one_request': only one booking, cancellation or reschedule per message. Tell the patient what "
                "was done and ask whether they want anything else.")
    return None


def _reject(session: Session, turn: Turn, problem: str, extra: Optional[dict] = None) -> Optional[Turn]:
    """Log a failed harness check, send it to the model, stop if there were too many."""
    turn.invalid += 1
    obs = {"ok": False, "rejected": True, "errors": [problem], **(extra or {})}
    _log(turn, "VALIDATION", passed=False, errors=[problem])
    _log(turn, "TOOL_OBSERVATION", observation=obs)
    _observe(session, turn, obs)
    if turn.invalid >= config.MAX_INVALID:
        return _stop(session, turn, "too_many_invalid", _fallback_reply(session, turn, "Sorry, I ran into a problem. Nothing was changed."))
    return None


def _prepare_gated(session: Session, turn: Turn, action: str, arg: dict) -> tuple[Optional[str], Optional[dict]]:
    """Run the Python checks for a book/cancel/reschedule proposal. Returns (problem, prepared)."""
    kind = GATED[action]
    if kind == "book":
        problem = _substitution_problem(session, turn, arg["slot_id"])
        return problem, (None if problem else tools.prepare_booking(session.conn, arg["slot_id"], session.patient_alias, arg["reason"]))
    problem = _appointment_problem(session, turn, arg["appointment_id"], kind)
    if problem:
        return problem, None
    if kind == "cancel":
        return None, tools.prepare_cancel(session.conn, arg["appointment_id"], session.patient_alias, arg["reason"])
    wrong_day = _wrong_date_problem(session, turn, arg["new_date"])
    if wrong_day:
        return wrong_day, None   # found in the demo re-run: "push that to Wednesday" -> moved to Thursday
    # Reschedule: Python turns the requested date/time into a real slot (small models invent slot ids).
    found = tools.check_availability(session.conn, arg["new_date"], arg["new_time"], arg.get("provider"))
    _remember_slots(turn, "check_availability", found)
    if found.get("status") != "available":
        return None, {"ok": False, **{k: v for k, v in found.items() if k != "ok"},
                      "next_step": "That new time is not available. Offer the alternatives and ask the patient to choose."}
    # Several dentists can be free at that time. Prefer the slot the patient was just offered, then the dentist of the
    # current appointment. (Found in a test: "the first one" = Dr. Chen 13:00, but Python picked Dr. Rivera 13:00.)
    offered = {o["slot_id"] for o in session.last_offered}
    current = next((a for a in tools.list_appointments(session.conn, session.patient_alias)
                    if str(a["id"]) == str(arg["appointment_id"])), None)
    candidates = found["slots"]
    pick = (next((c for c in candidates if c["slot_id"] in offered), None)
            or next((c for c in candidates if current and c["provider"] == current["provider"]), None)
            or candidates[0])
    new_slot = pick["slot_id"]
    problem = _substitution_problem(session, turn, new_slot)
    if problem:
        return problem, None
    return None, tools.prepare_reschedule(session.conn, arg["appointment_id"], new_slot, session.patient_alias, arg["reason"])


def _waiting_text(request: dict) -> str:
    when = f"{request['date']} at {request['time']} with {request['provider']}"
    if request.get("action") == "cancel":
        return f"I've sent your request to cancel {when} to our staff for approval. I'll update you here."
    if request.get("action") == "reschedule":
        f = request["from"]
        return (f"I've sent your request to move {f['date']} at {f['time']} to {when} to our staff for approval. "
                "I'll update you here.")
    return f"I've sent {when} to our staff for approval. I'll update you here once they decide."


def _loop(session: Session, turn: Turn, llm, approver: Approver, channel: str) -> Turn:
    def system():
        return {"role": "system", "content": build_system_prompt(session.today, session.patient_alias,
                                                                 session.prompt_variant, agent=turn.agent)}

    while sum(1 for s in turn.steps if s["kind"] == "MODEL_PROPOSED") < config.MAX_STEPS:
        # 1. Model proposes
        try:
            raw = llm.chat([system(), *session.history])
        except LLMError as e:
            _log(turn, "LLM_ERROR", error=str(e))
            return _stop(session, turn, "llm_error", f"Sorry, the assistant's language model is unavailable right now ({e}). Nothing was changed.")
        session.history.append({"role": "assistant", "content": raw})

        # 2. Parse + validate (including: may THIS agent use this tool?)
        proposal, parse_error = parse_proposal(raw)
        errors = [f"parser: {parse_error}"] if parse_error else validate(proposal, session.today, session.conn, agent=turn.agent)
        _log(turn, "MODEL_PROPOSED", raw=raw[:2000], proposal=proposal)
        if errors:
            stopped = _reject(session, turn, errors[0] if len(errors) == 1 else "; ".join(errors),
                              {"hint": "Fix the proposal and reply with one valid JSON action."})
            if stopped:
                return stopped
            continue
        _log(turn, "VALIDATION", passed=True, errors=[])

        action, arg = proposal["action"], proposal.get("argument", {}) or {}

        # Loop detection (found in eval: qwen3:4b repeated the same list_openings 7 times).
        call_key = json.dumps([turn.agent, action, arg], sort_keys=True)
        if action != "reply" and call_key in turn.calls_made:
            stopped = _reject(session, turn, f"check 'no_repeat': you already called {action} with these exact arguments in this "
                              "turn and the result has not changed. Use the earlier OBSERVATION and decide the next step, "
                              "usually a reply to the patient.")
            if stopped:
                return stopped
            continue
        # (call_key is remembered below, only once the call has actually RUN. Found in the demo run:
        #  a correct retry after a rejected attempt was wrongly blocked as a "repeat".)

        # Tool-choice check (found in eval: patient said "Tuesday November 24 at 13:30",
        # model used find_next_openings, which returned Monday instead).
        if action == "find_next_openings":
            named = named_dates(turn.user_message, session.today)
            if named:
                stopped = _reject(session, turn, f"check 'use_named_date': the patient named {named[0].isoformat()} "
                                  f"({named[0]:%A}). Use check_availability (if they gave a time) or list_openings for that date instead.")
                if stopped:
                    return stopped
                continue

        if action in ("list_openings", "check_availability"):
            problem = _wrong_date_problem(session, turn, arg["date"])
            if problem:
                stopped = _reject(session, turn, problem)
                if stopped:
                    return stopped
                continue

        # 3a. Hand-off between agents (at most once per turn, so they cannot ping-pong)
        if action == "transfer":
            target = arg["to"]
            intent = (turn.decision or {}).get("intent", "other")
            if target == turn.agent or turn.transferred:
                stopped = _reject(session, turn, "check 'transfer': no further transfers this turn; reply to the patient.")
                if stopped:
                    return stopped
                continue
            if target == "faq" and intent in ("book", "cancel_or_reschedule"):
                # Found in eval (qwen3:1.7b): a cancel request was handed to the FAQ agent, which cannot cancel.
                stopped = _reject(session, turn, f"check 'transfer': the router classified this as '{intent}', which the "
                                  "scheduling agent handles. Stay and finish it.")
                if stopped:
                    return stopped
                continue
            _log(turn, "HANDOFF", from_agent=turn.agent, to_agent=target, by="agent")
            turn.agent, turn.transferred = target, True
            _observe(session, turn, {"ok": True, "transferred_to": target,
                                     "note": f"You are now the {target} agent. Continue helping the patient."})
            continue

        # 3b. Final reply, with output guardrails checked against the database (first failing check wins:
        #     sent back to the model once, then replaced by a reply Python writes from the facts)
        if action == "reply":
            message = arg["message"]
            problem, fallback = _reply_problem(session, turn, message)
            if problem:
                if not turn.guard_retried:
                    turn.guard_retried = True
                    obs = {"ok": False, "rejected": True, "errors": [f"check 'grounded_reply': {problem} Rewrite the reply."]}
                    _log(turn, "OUTPUT_GUARD", passed=False, observation=obs)
                    _observe(session, turn, obs)
                    continue
                _log(turn, "OUTPUT_GUARD", passed=False, action="replaced", problem=problem)
                reason = VERIFIED_STOP.get(turn.done_action, "reply_corrected") if turn.done_action else "reply_corrected"
                return _stop(session, turn, reason, fallback)
            _log(turn, "OUTPUT_GUARD", passed=True)
            return _stop(session, turn, VERIFIED_STOP.get(turn.done_action, "replied"), message)

        # 3c. Book / cancel / reschedule: Python checks, then the HUMAN approval gate
        if action in GATED:
            if _someone_else_problem(session, turn):
                # Found in the demo re-run: the refusal looped until "I ran into a problem". Answer plainly instead.
                _log(turn, "VALIDATION", passed=False, errors=[_someone_else_problem(session, turn)])
                return _stop(session, turn, "declined",
                             f"I can only book, cancel or move appointments for you ({session.patient_alias}). The other "
                             "person will need to contact the office (or sign in themselves). Can I help with your own appointments?")
            problem = _one_request_problem(turn) or _book_while_rescheduling_problem(session, turn, action)
            if problem:
                stopped = _reject(session, turn, problem)
                if stopped:
                    return stopped
                continue
        mine_ids = ({str(m["appointment_id"]) for m in tools.list_my_appointments(session.conn, session.patient_alias)["appointments"]}
                    if action in ("cancel_appointment", "reschedule_appointment") and not turn.looked_up else set())
        if action in ("cancel_appointment", "reschedule_appointment") and not turn.looked_up \
                and str(arg["appointment_id"]) not in mine_ids:
            # Found in eval (qwen3:1.7b): it guessed appointment ids (123 instead of 120). Hand it the real ones.
            # A real id of this patient (e.g. seen earlier in the conversation) is accepted as is (found in the demo run).
            turn.looked_up = True
            mine = tools.list_my_appointments(session.conn, session.patient_alias)["appointments"]
            extra = {"your_appointments": mine}
            if action == "cancel_appointment" and len(mine) == 1 and _ASKED["cancel"].search(turn.user_message):
                extra["suggested_next_action"] = {"action": "cancel_appointment", "argument": {
                    "appointment_id": mine[0]["appointment_id"], "reason": arg.get("reason", "patient request")}}
            stopped = _reject(session, turn, "check 'look_up_first': you used an appointment_id without looking it up. "
                              "Here are the patient's real appointments; use one of these ids.", extra)
            if stopped:
                return stopped
            continue
        if action in GATED:
            problem, prep = _prepare_gated(session, turn, action, arg)
            if problem:
                options = _grounded_options(session, turn) if action != "cancel_appointment" else []
                extra = {}
                if options:  # give the model a concrete, grounded way forward
                    lead = "That time isn't available. I can offer" if turn.unavailable_seen else "Here are open times:"
                    extra["suggested_next_action"] = {"action": "reply", "argument": {"message":
                        f"{lead} {_options_text(options)}. Which would you like?"}}
                stopped = _reject(session, turn, problem, extra)
                if stopped:
                    return stopped
                continue
            if not prep["ok"]:
                obs = prep
            else:
                request = prep["approval_request"] | {"patient_request": turn.user_message}
                request_id = tools.create_request(session.conn, session.id, request)   # the staff queue
                _log(turn, "APPROVAL_REQUESTED", request=request, request_id=request_id)
                decision = approver(request)
                if decision is None:  # staff will decide later (UI dashboard)
                    session.pending = {"request": request, "turn": turn, "request_id": request_id}
                    return _stop(session, turn, "awaiting_approval", _waiting_text(request))
                obs = _finish_request(session, turn, request, bool(decision), channel, request_id)
        else:
            # 3d. Read-only tool. Python injects who the patient is and what "today" is.
            if action == "find_next_openings":
                arg = arg | {"from_date": session.today.isoformat()}
            if action == "list_my_appointments":
                arg = {"patient_alias": session.patient_alias}
                turn.looked_up = True
            try:
                obs = tools.READ_TOOLS[action](session.conn, **arg)
            except Exception as e:  # a tool failure is an observation, not a crash
                obs = {"ok": False, "error": f"Tool '{action}' failed: {e}"}
            _remember_slots(turn, action, obs)
            if not obs.get("ok") and "Unknown provider" in str(obs.get("error", "")):
                turn.unknown_provider = arg.get("provider")

        # 4. Observation back to the model
        turn.calls_made.add(call_key)   # it ran: an identical repeat would get the same answer
        _log(turn, "TOOL_OBSERVATION", action=action, observation=obs)
        _observe(session, turn, obs)

    return _stop(session, turn, "max_steps", _fallback_reply(
        session, turn, "Sorry, I couldn't finish that request." + ("" if turn.done_action else " Nothing was changed.")))


def choose_agent(session: Session, decision) -> tuple[str, str]:
    """Front-desk router: which agent handles this message? (decided by the router model, rules as backup)"""
    if decision.intent in ("book", "cancel_or_reschedule"):
        return "scheduling", f"intent={decision.intent}"
    if decision.intent == "ask_info":
        return "faq", "intent=ask_info"
    # Short follow-ups like "yes" or "the second one" stay with whoever is mid-conversation.
    return session.active_agent, f"intent=other, staying with {session.active_agent}"


def _save_record(session: Session, turn: Turn, llm, decider_name: str) -> None:
    config.EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    record = {
        "session": session.id,
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "llm": getattr(llm, "name", "?"),
        "decider": decider_name,
        "agent": turn.agent,
        "prompt_variant": session.prompt_variant,
        "today": session.today.isoformat(),
        "patient_alias": session.patient_alias,
        "user_message": turn.user_message,
        "decision": turn.decision,
        "steps": turn.steps,
        "final_reply": turn.reply,
        "stop_reason": turn.stop_reason,
        "seconds": round(time.time() - turn.started, 2),
        # Independent evidence: what the database actually contains for this patient now.
        "db_appointments_for_patient": tools.list_appointments(session.conn, session.patient_alias),
    }
    path = config.EVIDENCE_DIR / f"run_{datetime.now():%Y%m%d_%H%M%S}_{session.id}_{len(turn.steps)}.json"
    path.write_text(json.dumps(record, indent=2, default=str))
    turn.record_path = str(path)


def run_turn(session: Session, user_message: str, llm, decider, approver: Approver,
             channel: str = "cli", policy: str = "strict", save: bool = True,
             on_step: Optional[Callable[[dict], None]] = None) -> Turn:
    turn = Turn(user_message=user_message, on_step=on_step, agent=session.active_agent)
    if session.pending:
        return _stop(session, turn, "awaiting_approval", "Your previous request is still waiting for staff approval.")

    # 0. Router: decision model first (fixed choices), then pick the agent
    decision = decider.decide(user_message, context=session.last_reply or None)
    routing = route(decision, policy)
    turn.decision = decision.to_dict() | {"route": routing}
    _log(turn, "DECIDER", decider=decision.source, decision=turn.decision)

    if routing["action"] == "front_desk":
        session.history += [{"role": "user", "content": user_message},
                            {"role": "assistant", "content": json.dumps({"action": "reply", "argument": {"message": FRONT_DESK_REPLIES[routing["reason"]]}})}]
        _stop(session, turn, "front_desk", FRONT_DESK_REPLIES[routing["reason"]])
    else:
        agent, why = choose_agent(session, decision)
        if agent != turn.agent or not any(s["kind"] == "ROUTER" for s in turn.steps):
            _log(turn, "ROUTER", to_agent=agent, reason=why)
        turn.agent = agent
        content = user_message
        if routing["notes"]:
            content += "\n\n[System notes: " + " ".join(routing["notes"]) + "]"
        session.history.append({"role": "user", "content": content})
        _loop(session, turn, llm, approver, channel)

    session.last_user_message = user_message
    session.last_intent = (turn.decision or {}).get("intent", "")
    if save:
        _save_record(session, turn, llm, getattr(decider, "name", "?"))
    return turn


def resume_turn(session: Session, approved: bool, llm, decider_name: str = "?", channel: str = "ui", save: bool = True) -> Turn:
    """Continue a turn that stopped at 'awaiting_approval' once a human (staff) has decided."""
    if not session.pending:
        raise ValueError("Nothing is waiting for approval.")
    request, turn, request_id = session.pending["request"], session.pending["turn"], session.pending.get("request_id")
    session.pending = None
    turn.steps.pop()  # drop the interim awaiting_approval STOP
    obs = _finish_request(session, turn, request, approved, channel, request_id)
    action = {"book": "book_appointment", "cancel": "cancel_appointment", "reschedule": "reschedule_appointment"}[request.get("action", "book")]
    _log(turn, "TOOL_OBSERVATION", action=action, observation=obs)
    _observe(session, turn, obs)
    _loop(session, turn, llm, approver=lambda r: None, channel=channel)
    if save:
        _save_record(session, turn, llm, decider_name)
    return turn
