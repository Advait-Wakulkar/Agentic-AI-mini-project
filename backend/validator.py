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
        "description": "Office facts. topic: hours | address | services | providers | cancellation_policy.",
    },
    "book_appointment": {
        "required": {"slot_id": str, "reason": str},
        "optional": {},
        "description": "Request to book an open slot_id for the current patient. A human must approve; you cannot approve it yourself.",
    },
    "reply": {
        "required": {"message": str},
        "optional": {},
        "description": "Send a message to the patient and end your turn (answer, question, or alternatives).",
    },
}

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


def validate(proposal: dict, today: date, conn=None) -> list[str]:
    """Return the list of failed checks. An empty list means the proposal is allowed."""
    errors: list[str] = []
    action = proposal.get("action")
    if not isinstance(action, str):
        return ["check 'action_present': missing or non-text 'action' field."]
    if action not in ACTIONS:
        return [f"check 'known_action': '{action}' is not an allowed action. Allowed: {', '.join(ACTIONS)}."]

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
            errors.append(f"check 'arg_type': '{name}' must be text, got {type(value).__name__}.")
    if errors:
        return errors

    # Value checks.
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
    if action == "reply" and not arg["message"].strip():
        errors.append("check 'message_nonempty': the reply message is empty.")
    return errors
