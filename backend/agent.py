"""The agent loop: the model proposes, Python decides and acts.

One user message = one "turn". Inside a turn the loop repeats:

    1. LLM proposes one action (JSON)              <- model decision
    2. Python parses + validates it                 <- harness check
    3. Python runs the tool (or the approval gate)  <- program action
    4. The result goes back as an OBSERVATION       <- observation
    5. State is updated; go to 1 until a stop condition

Stop conditions are decided by Python, not the model:
    replied             model chose 'reply' (answer, question, alternatives)
    booked_verified     a booking was approved by a human, saved, re-read from the DB, then replied
    awaiting_approval   booking is waiting for a human (UI); resume with resume_turn()
    front_desk          decider routed the message to staff (medical advice, cancellation)
    reply_corrected     model claimed a booking that does not exist twice; harness replaced the reply
    too_many_invalid    MAX_INVALID rejected proposals in one turn
    max_steps           MAX_STEPS passes without finishing
    llm_error           the model could not be reached (reported honestly)
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

_CLAIM_WORD = re.compile(r"\b(confirm(ed|ation)|booked|scheduled|reserved|all set|you'?re set|is set)\b", re.I)
_NOT_A_CLAIM = re.compile(
    r"\b(not|n't|no|nothing|never|cannot|unable|yet|already|fully|taken|unavailable|"
    r"if|once|after|before|would|shall|should|want|need|can I|could|may I|please (choose|confirm|pick))\b|\?",
    re.I,
)


def claims_booking(text: str) -> bool:
    """Does any sentence tell the patient an appointment IS booked/confirmed?

    Found in eval (qwen3:1.7b, 2026-11-22): "Appointment confirmed for Tuesday ..." with
    nothing saved slipped past the earlier phrase list. Now any sentence with a
    confirm/booked/scheduled/reserved word counts, unless it is negated, a question,
    conditional, or about a time being unavailable ("9:00 is already booked").
    """
    for sentence in re.split(r"(?<=[.!?])\s+|\n+", text):
        if _CLAIM_WORD.search(sentence) and not _NOT_A_CLAIM.search(sentence):
            if re.search(r"\b(is|are) booked\b", sentence, re.I) and not re.search(r"\b(you|your)\b", sentence, re.I):
                continue  # "Dr. Rivera is booked at 9:00" describes the schedule, not the patient's booking
            return True
    return False


@dataclass
class Session:
    """Everything the agent remembers between turns (the model itself is stateless)."""
    conn: object
    patient_alias: str
    today: date = field(default_factory=config.today)
    prompt_variant: str = "baseline"
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    history: list[dict] = field(default_factory=list)   # user / assistant / observation messages
    pending: Optional[dict] = None                       # turn paused for human approval
    last_offered: list = field(default_factory=list)     # slots named in the assistant's LAST reply, earliest first


@dataclass
class Turn:
    user_message: str
    steps: list[dict] = field(default_factory=list)
    invalid: int = 0
    guard_retried: bool = False
    unavailable_seen: bool = False                       # requested time turned out to be taken / not offered
    turn_slots: set = field(default_factory=set)         # slot ids returned by tools this turn
    calls_made: set = field(default_factory=set)         # (action, args) already executed this turn
    booked: Optional[dict] = None
    decision: Optional[dict] = None
    reply: str = ""
    stop_reason: str = ""
    started: float = field(default_factory=time.time)
    record_path: Optional[str] = None
    on_step: Optional[Callable[[dict], None]] = None   # live trace printer (CLI)


def _log(turn: Turn, kind: str, **data):
    entry = {"step": len(turn.steps) + 1, "kind": kind, **data}
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


def _finish_booking(session: Session, turn: Turn, request: dict, approved: bool, channel: str) -> dict:
    """Record the HUMAN decision, then save and independently verify."""
    approval_id = tools.record_decision(session.conn, request, approved, channel)
    _log(turn, "HUMAN_DECISION", approved=approved, channel=channel, approval_id=approval_id, request=request)
    if not approved:
        return {"ok": False, "approved": False,
                "detail": "The staff member DENIED this booking. Nothing was saved. Tell the patient and offer to find another time."}
    saved = tools.commit_booking(session.conn, request, approval_id)
    verified = tools.verify_booking(session.conn, request["slot_id"], request["patient_alias"])
    _log(turn, "VERIFY", saved=saved, verified=verified, slot_id=request["slot_id"])
    if saved.get("ok") and verified:
        turn.booked = request
        return {"ok": True, "approved": True, "verified": True, "appointment": request}
    return {"ok": False, "approved": True, "verified": False, "error": saved.get("error", "Save could not be verified.")}


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
                f"{chosen['provider']}, not this slot. Book slot {chosen['slot_id']} instead.")
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
    options = _grounded_options(session, turn)
    if options:
        return f"{base} The open times are: {_options_text(options)}. Which would you like?"
    return f"{base} Could you tell me a day or time that works for you?"


def _stop(session: Session, turn: Turn, reason: str, reply: str) -> Turn:
    turn.stop_reason = reason
    turn.reply = reply
    # Remember exactly which options the patient was shown, so their answer can be checked next turn.
    session.last_offered = _offered_in_reply(session, turn, reply) if reason != "awaiting_approval" else []
    _log(turn, "STOP", stop_reason=reason, reply=reply)
    return turn


def _loop(session: Session, turn: Turn, llm, approver: Approver, channel: str) -> Turn:
    system = {"role": "system", "content": build_system_prompt(session.today, session.patient_alias, session.prompt_variant)}

    while sum(1 for s in turn.steps if s["kind"] == "MODEL_PROPOSED") < config.MAX_STEPS:
        # 1. Model proposes
        try:
            raw = llm.chat([system, *session.history])
        except LLMError as e:
            _log(turn, "LLM_ERROR", error=str(e))
            return _stop(session, turn, "llm_error", f"Sorry, the assistant's language model is unavailable right now ({e}). Nothing was booked.")
        session.history.append({"role": "assistant", "content": raw})

        # 2. Parse + validate
        proposal, parse_error = parse_proposal(raw)
        errors = [f"parser: {parse_error}"] if parse_error else validate(proposal, session.today, session.conn)
        _log(turn, "MODEL_PROPOSED", raw=raw[:2000], proposal=proposal)
        _log(turn, "VALIDATION", passed=not errors, errors=errors)
        if errors:
            turn.invalid += 1
            obs = {"ok": False, "rejected": True, "errors": errors, "hint": "Fix the proposal and reply with one valid JSON action."}
            _log(turn, "TOOL_OBSERVATION", observation=obs)
            _observe(session, turn, obs)
            if turn.invalid >= config.MAX_INVALID:
                return _stop(session, turn, "too_many_invalid", _fallback_reply(session, turn, "Sorry, I ran into a problem processing that. Nothing was booked."))
            continue

        action, arg = proposal["action"], proposal.get("argument", {}) or {}

        # Loop detection (found in eval: qwen3:4b repeated the same list_openings 7 times).
        call_key = json.dumps([action, arg], sort_keys=True)
        if action != "reply" and call_key in turn.calls_made:
            turn.invalid += 1
            problem = (f"check 'no_repeat': you already called {action} with these exact arguments in this turn and "
                       "the result has not changed. Use the earlier OBSERVATION and decide the next step, usually a reply to the patient.")
            _log(turn, "VALIDATION", passed=False, errors=[problem])
            obs = {"ok": False, "rejected": True, "errors": [problem]}
            _log(turn, "TOOL_OBSERVATION", observation=obs)
            _observe(session, turn, obs)
            if turn.invalid >= config.MAX_INVALID:
                return _stop(session, turn, "too_many_invalid", _fallback_reply(session, turn, "Sorry, I got stuck on that request. Nothing was booked."))
            continue
        turn.calls_made.add(call_key)

        # Tool-choice check (found in eval: patient said "Tuesday November 24 at 13:30",
        # model used find_next_openings, which returned Monday instead).
        if action == "find_next_openings":
            named = named_dates(turn.user_message, session.today)
            if named:
                turn.invalid += 1
                problem = (f"check 'use_named_date': the patient named {named[0].isoformat()} ({named[0]:%A}). "
                           "Use check_availability (if they gave a time) or list_openings for that date instead.")
                _log(turn, "VALIDATION", passed=False, errors=[problem])
                obs = {"ok": False, "rejected": True, "errors": [problem]}
                _log(turn, "TOOL_OBSERVATION", observation=obs)
                _observe(session, turn, obs)
                continue

        # 3a. Final reply, with an output guardrail checked against the database
        if action == "reply":
            message = arg["message"]
            if claims_booking(message) and not turn.booked:
                if not turn.guard_retried:
                    turn.guard_retried = True
                    obs = {"ok": False, "rejected": True, "errors": [
                        "check 'grounded_reply': your reply says an appointment is booked, but no booking was approved, "
                        "saved and verified in this turn. Do not claim it. Rewrite the reply."]}
                    _log(turn, "OUTPUT_GUARD", passed=False, observation=obs)
                    _observe(session, turn, obs)
                    continue
                _log(turn, "OUTPUT_GUARD", passed=False, action="replaced")
                return _stop(session, turn, "reply_corrected",
                             _fallback_reply(session, turn, "I haven't booked anything yet."))
            if turn.booked and not mentions_time(message, turn.booked["time"]) and not turn.guard_retried:
                # Found in eval: model booked Monday 13:30 and told the patient "Tuesday". Make it restate the facts.
                turn.guard_retried = True
                b = turn.booked
                obs = {"ok": False, "rejected": True, "errors": [
                    f"check 'grounded_reply': the verified booking is {b['date']} at {b['time']} with {b['provider']}. "
                    "Your reply must state exactly this date and time."]}
                _log(turn, "OUTPUT_GUARD", passed=False, observation=obs)
                _observe(session, turn, obs)
                continue
            _log(turn, "OUTPUT_GUARD", passed=True)
            return _stop(session, turn, "booked_verified" if turn.booked else "replied", message)

        # 3b. Booking goes through the approval gate, never straight to the database
        if action == "book_appointment":
            problem = _substitution_problem(session, turn, arg["slot_id"])
            prep = (tools.prepare_booking(session.conn, arg["slot_id"], session.patient_alias, arg["reason"])
                    if not problem else None)
            if problem:
                turn.invalid += 1
                obs = {"ok": False, "rejected": True, "errors": [problem]}
                options = _grounded_options(session, turn)
                if options:  # give the model a concrete, grounded way forward
                    lead = "That time isn't available. I can offer" if turn.unavailable_seen else "Here are open times:"
                    obs["suggested_next_action"] = {"action": "reply", "argument": {"message":
                        f"{lead} {_options_text(options)}. Which would you like?"}}
                _log(turn, "VALIDATION", passed=False, errors=[problem])
                if turn.invalid >= config.MAX_INVALID:
                    _log(turn, "TOOL_OBSERVATION", action=action, observation=obs)
                    return _stop(session, turn, "too_many_invalid",
                                 _fallback_reply(session, turn, "Your requested time isn't available and nothing was booked."))
            elif not prep["ok"]:
                obs = prep
            else:
                request = prep["approval_request"] | {"patient_request": turn.user_message}
                _log(turn, "APPROVAL_REQUESTED", request=request)
                decision = approver(request)
                if decision is None:  # a human will decide later (UI)
                    session.pending = {"request": request, "turn": turn}
                    return _stop(session, turn, "awaiting_approval",
                                 f"Waiting for staff approval: {request['date']} at {request['time']} with {request['provider']}.")
                obs = _finish_booking(session, turn, request, bool(decision), channel)
        else:
            # 3c. Read-only tool
            if action == "find_next_openings":
                arg = arg | {"from_date": session.today.isoformat()}  # injected by Python, not chosen by the model
            try:
                obs = tools.READ_TOOLS[action](session.conn, **arg)
            except Exception as e:  # a tool failure is an observation, not a crash
                obs = {"ok": False, "error": f"Tool '{action}' failed: {e}"}
            _remember_slots(turn, action, obs)

        # 4. Observation back to the model
        _log(turn, "TOOL_OBSERVATION", action=action, observation=obs)
        _observe(session, turn, obs)

    return _stop(session, turn, "max_steps", _fallback_reply(
        session, turn, "Sorry, I couldn't finish that request." + ("" if turn.booked else " Nothing was booked.")))


def _save_record(session: Session, turn: Turn, llm, decider_name: str) -> None:
    config.EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    record = {
        "session": session.id,
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "llm": getattr(llm, "name", "?"),
        "decider": decider_name,
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
    turn = Turn(user_message=user_message, on_step=on_step)
    if session.pending:
        return _stop(session, turn, "awaiting_approval", "A booking is still waiting for staff approval. Please approve or deny it first.")

    # 0. Decision model: fixed-choice routing before the LLM is involved
    decision = decider.decide(user_message)
    routing = route(decision, policy)
    turn.decision = decision.to_dict() | {"route": routing}
    _log(turn, "DECIDER", decider=decision.source, decision=turn.decision)

    if routing["action"] == "front_desk":
        session.history += [{"role": "user", "content": user_message},
                            {"role": "assistant", "content": json.dumps({"action": "reply", "argument": {"message": FRONT_DESK_REPLIES[routing["reason"]]}})}]
        _stop(session, turn, "front_desk", FRONT_DESK_REPLIES[routing["reason"]])
    else:
        content = user_message
        if routing["notes"]:
            content += "\n\n[System notes: " + " ".join(routing["notes"]) + "]"
        session.history.append({"role": "user", "content": content})
        _loop(session, turn, llm, approver, channel)

    if save:
        _save_record(session, turn, llm, getattr(decider, "name", "?"))
    return turn


def resume_turn(session: Session, approved: bool, llm, decider_name: str = "?", channel: str = "ui", save: bool = True) -> Turn:
    """Continue a turn that stopped at 'awaiting_approval' once a human has decided."""
    if not session.pending:
        raise ValueError("Nothing is waiting for approval.")
    request, turn = session.pending["request"], session.pending["turn"]
    session.pending = None
    turn.steps.pop()  # drop the interim awaiting_approval STOP
    obs = _finish_booking(session, turn, request, approved, channel)
    _log(turn, "TOOL_OBSERVATION", action="book_appointment", observation=obs)
    _observe(session, turn, obs)
    _loop(session, turn, llm, approver=lambda r: None, channel=channel)
    if save:
        _save_record(session, turn, llm, decider_name)
    return turn
