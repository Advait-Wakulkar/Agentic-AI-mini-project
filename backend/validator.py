"""Parser and validator for the model's proposed actions.

Parser:    turns the model's raw text into a Python dict (json.loads). It only
           answers "is this JSON?".
Validator: checks that the parsed proposal is ALLOWED: known action, required
           arguments present, no extra arguments, right types, sensible values.

A proposal that fails either step is never executed. The failure goes back to
the model as an observation so it can correct itself.
"""

import json
import re
from datetime import date

# Every action the model may propose. 'book_appointment' is special: the harness
# routes it through human approval instead of running it directly.
ACTIONS = {
    "list_openings": {
        "required": {"date": str},
        "optional": {"provider": str, "part_of_day": str},
        "description": "List open 30-minute slots on a date. part_of_day: morning | afternoon.",
    },
    "find_next_openings": {
        "required": {},
        "optional": {"provider": str, "part_of_day": str},
        "description": "The earliest open slots from tomorrow on. Use when the patient gives no date ('sometime', 'asap').",
    },
    "check_availability": {
        "required": {"date": str, "time": str},
        "optional": {"provider": str},
        "description": "Check whether an exact date and time (HH:MM) is open.",
    },
    "get_office_info": {
        "required": {"topic": str},
        "optional": {},
        "description": "Office facts. topic: hours | address | services | providers | cancellation_policy | insurance | prices.",
    },
    "book_appointment": {
        "required": {"slot_id": str, "reason": str},
        "optional": {},
        "description": "Request to book an open slot_id for the current patient. A human must approve; you cannot approve it yourself.",
    },
    "list_my_appointments": {
        "required": {},
        "optional": {},
        "description": "The current patient's upcoming appointments, with their appointment_id.",
    },
    "cancel_appointment": {
        "required": {"appointment_id": (int, str), "reason": str},
        "optional": {},
        "description": "Request to cancel one of the patient's appointments (appointment_id from list_my_appointments). A human must approve.",
    },
    "reschedule_appointment": {
        "required": {"appointment_id": (int, str), "new_date": str, "new_time": str, "reason": str},
        "optional": {"provider": str},
        "description": "Request to move one of the patient's appointments to new_date (YYYY-MM-DD) at new_time (HH:MM). "
                       "A human must approve.",
    },
    "get_my_history": {
        "required": {},
        "optional": {},
        "description": "Everything on file for the current patient: profile, insurance, upcoming appointments, past visits "
                       "(completed, cancelled, no-shows).",
    },
    "register_patient": {
        "required": {"first_name": str, "last_name": str, "date_of_birth": str, "phone": str, "email": str,
                     "insurance_provider": str},
        "optional": {"insurance_member_id": str},
        "description": "NEW patients only: save the details the patient gave in the chat. date_of_birth YYYY-MM-DD; "
                       "insurance_provider is a plan name or 'self-pay' (member ID needed unless self-pay). "
                       "Use only values the patient actually gave.",
    },
    "verify_insurance": {
        "required": {},
        "optional": {"insurance_provider": str, "member_id": str},
        "description": "Run a SIMULATED insurance eligibility check (demo; no real insurer). Without arguments it checks "
                       "the plan on file.",
    },
    "send_calendar_invite": {
        "required": {"appointment_id": (int, str)},
        "optional": {},
        "description": "Email the patient a calendar invite (.ics) for one of their booked appointments "
                       "(appointment_id from list_my_appointments). Email is a simulated outbox in this demo.",
    },
    # Reminder agent only. Python fills in the recipient from the patient record; the model never chooses it.
    "send_email": {
        "required": {"subject": str, "message": str},
        "optional": {},
        "description": "Email the patient (the calendar invite is attached automatically).",
    },
    "send_sms": {
        "required": {"message": str},
        "optional": {},
        "description": "Text the patient a short message (under 300 characters).",
    },
    "finish": {
        "required": {},
        "optional": {},
        "description": "End this job once the patient has been notified.",
    },
    "transfer": {
        "required": {"to": str},
        "optional": {},
        "description": "Hand the conversation to another agent: 'scheduling' (book, cancel, reschedule) or 'faq' (office questions).",
    },
    "reply": {
        "required": {"message": str},
        "optional": {},
        "description": "Send a message to the patient and end your turn (answer, question, or alternatives).",
    },
}

# Each agent may only use its own tools (least privilege). The validator enforces this.
AGENT_ACTIONS = {
    "scheduling": ["list_openings", "find_next_openings", "check_availability", "list_my_appointments",
                   "get_my_history", "register_patient", "verify_insurance", "send_calendar_invite",
                   "book_appointment", "cancel_appointment", "reschedule_appointment", "transfer", "reply"],
    "faq": ["get_office_info", "get_my_history", "verify_insurance", "transfer", "reply"],
    "reminders": ["send_email", "send_sms", "finish"],   # background agent: no chat, no schedule changes
}
CHAT_AGENTS = ("scheduling", "faq")   # the agents a chat can be transferred to

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
SLOT_RE = re.compile(r"^S-\d{8}-\d{4}-P\d+$")


def parse_proposal(text: str) -> tuple[dict | None, str | None]:
    """Return (proposal, None) or (None, error). Tolerates code fences and <think> blocks."""
    if not text or not text.strip():
        return None, "Empty reply. Respond with one JSON object."
    cleaned = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned).strip()
    start = cleaned.find("{")
    if start == -1:
        return None, "No JSON object found. Respond with exactly one JSON object."
    try:
        obj, _ = json.JSONDecoder().raw_decode(cleaned[start:])
    except json.JSONDecodeError as e:
        return None, f"Invalid JSON: {e.msg} at position {e.pos}."
    if not isinstance(obj, dict):
        return None, "The JSON must be an object like {\"action\": ..., \"argument\": {...}}."
    # Repair one common slip: arguments placed at the top level, e.g.
    # {"action": "reply", "message": "..."}  ->  {"action": "reply", "argument": {"message": "..."}}
    # This only moves fields; the validator still checks every one of them.
    if "argument" not in obj and isinstance(obj.get("action"), str):
        extra = {k: v for k, v in obj.items() if k not in ("action", "thought")}
        if extra:
            obj = {"action": obj["action"], "argument": extra, "_repaired": True}
    return obj, None


def validate(proposal: dict, today: date, conn=None, agent: str | None = None) -> list[str]:
    """Return the list of failed checks. An empty list means the proposal is allowed."""
    errors: list[str] = []
    action = proposal.get("action")
    if not isinstance(action, str):
        return ["check 'action_present': missing or non-text 'action' field."]
    if action not in ACTIONS:
        allowed_list = AGENT_ACTIONS[agent] if agent else list(ACTIONS)
        return [f"check 'known_action': '{action}' is not an allowed action. Allowed: {', '.join(allowed_list)}."]
    if agent and action not in AGENT_ACTIONS[agent]:
        return [f"check 'agent_tools': the {agent} agent cannot use '{action}'. It can use: "
                f"{', '.join(AGENT_ACTIONS[agent])}. Use transfer if another agent should handle this."]

    unknown_top = set(proposal) - {"action", "argument", "thought", "_repaired"}
    if unknown_top:
        return [f"check 'shape': unexpected fields {sorted(unknown_top)}; put arguments inside 'argument'."]
    arg = proposal.get("argument", {})
    if not isinstance(arg, dict):
        return ["check 'argument_object': 'argument' must be a JSON object."]

    spec = ACTIONS[action]
    allowed = {**spec["required"], **spec["optional"]}
    for name, typ in spec["required"].items():
        if name not in arg:
            errors.append(f"check 'required_arg': '{action}' needs argument '{name}'.")
    for name, value in arg.items():
        if name not in allowed:
            # This also blocks a model trying to pass its own 'approval' or 'patient_alias'.
            errors.append(f"check 'no_extra_args': '{name}' is not an argument of '{action}'.")
        elif value is not None and not isinstance(value, allowed[name]):
            want = "a number" if allowed[name] == (int, str) else "text"
            errors.append(f"check 'arg_type': '{name}' must be {want}, got {type(value).__name__}.")
    if errors:
        return errors

    # Value checks.
    if "new_date" in arg or "new_time" in arg:   # reschedule uses the same date/time checks
        arg = {**arg, "date": arg.get("new_date", arg.get("date")), "time": arg.get("new_time", arg.get("time"))}
    if "date" in arg:
        if not DATE_RE.match(arg["date"]):
            errors.append("check 'date_format': date must be YYYY-MM-DD.")
        else:
            try:
                d = date.fromisoformat(arg["date"])
                if d < today:
                    errors.append(f"check 'date_not_past': {arg['date']} is before today ({today}).")
            except ValueError:
                errors.append(f"check 'date_real': {arg['date']} is not a real calendar date.")
    if "time" in arg and not TIME_RE.match(arg["time"]):
        errors.append("check 'time_format': time must be HH:MM in 24-hour format, e.g. 14:30.")
    if arg.get("part_of_day") not in (None, "morning", "afternoon"):
        errors.append("check 'part_of_day': must be 'morning' or 'afternoon'.")
    if action == "book_appointment":
        if not SLOT_RE.match(arg["slot_id"]):
            errors.append("check 'slot_id_format': use a slot_id exactly as returned by a tool, e.g. S-20261124-0900-P1.")
        elif conn is not None and conn.one("SELECT 1 AS found FROM slots WHERE id = ?", (arg["slot_id"],)) is None:
            errors.append(f"check 'slot_exists': slot '{arg['slot_id']}' does not exist.")
        if not arg["reason"].strip():
            errors.append("check 'reason_nonempty': give a short visit reason, e.g. 'cleaning'.")
    if "appointment_id" in arg and not str(arg["appointment_id"]).strip().isdigit():
        errors.append("check 'appointment_id': use the numeric appointment_id from list_my_appointments.")
    if action == "transfer" and arg["to"] not in CHAT_AGENTS:
        errors.append(f"check 'transfer_target': 'to' must be one of {', '.join(CHAT_AGENTS)}.")
    if action in ("reply", "send_email", "send_sms") and not arg["message"].strip():
        errors.append("check 'message_nonempty': the message is empty.")
    if action == "send_sms" and len(arg["message"]) > 300:
        errors.append("check 'sms_length': keep the text under 300 characters.")
    if action == "send_email" and not arg["subject"].strip():
        errors.append("check 'subject_nonempty': give the email a short subject.")
    return errors
