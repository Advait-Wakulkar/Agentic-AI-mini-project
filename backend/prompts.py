"""System prompt and tool descriptions.

Kept separate from the loop so the prompt experiment (eval/run_eval.py
--variant) can swap them without touching code. Note: everything here is a
POLICY HINT. The rules that matter (approval, no false bookings) are enforced in
Python by the harness, whatever the prompt says.
"""

import json
from datetime import date, timedelta

from backend.validator import ACTIONS

VARIANTS = ["baseline", "checklist", "example", "combined"]


def _calendar(today: date, days: int = 21) -> str:
    lines = []
    for i in range(days):
        d = today + timedelta(days=i)
        tag = " (today)" if i == 0 else (" (tomorrow)" if i == 1 else "")
        lines.append(f"  {d:%A} {d.isoformat()}{tag}")
    return "\n".join(lines)


def _actions_block() -> str:
    out = []
    for name, spec in ACTIONS.items():
        args = {**{k: "text (required)" for k in spec["required"]}, **{k: "text (optional)" for k in spec["optional"]}}
        out.append(f"- {name}: {spec['description']}\n  argument: {json.dumps(args)}")
    return "\n".join(out)


CHECKLIST = """
Workflow to follow:
1. Work out the exact date (YYYY-MM-DD) from the calendar above. If the user gave no day, use find_next_openings.
2. If the user named a specific time, call check_availability. Otherwise call list_openings.
3. If the requested time is unavailable or not offered, DO NOT book anything. Use list_openings and reply with 2-3 alternative times, then wait for the user to pick.
4. Only call book_appointment after the user has chosen a specific open slot, using the exact slot_id from a tool result.
5. After book_appointment, read the OBSERVATION. Only say the appointment is booked if it says "verified": true.
"""

EXAMPLE = """
Example (different dates):
User: Can I get a check-up with Dr. Chen on Thursday at 10?
You: {"action": "check_availability", "argument": {"date": "2030-01-10", "time": "10:00", "provider": "Dr. Chen"}}
OBSERVATION: {"ok": true, "status": "unavailable", "detail": "10:00 on 2030-01-10 is already booked with Dr. Chen."}
You: {"action": "list_openings", "argument": {"date": "2030-01-10", "provider": "Dr. Chen"}}
OBSERVATION: {"ok": true, "openings": [{"slot_id": "S-20300110-1030-P2", "time": "10:30", "provider": "Dr. Chen"}, ...]}
You: {"action": "reply", "argument": {"message": "10:00 is taken, but Dr. Chen is free at 10:30 or 11:00 on Thursday. Would either work?"}}
"""


def build_system_prompt(today: date, patient_alias: str, variant: str = "baseline") -> str:
    if variant not in VARIANTS:
        raise ValueError(f"Unknown prompt variant '{variant}'. Choose from {VARIANTS}.")
    prompt = f"""You are the scheduling assistant for Bright Smile Dental, a fictional dental office.
You help ONE patient, "{patient_alias}", find and book a 30-minute appointment.
All data is fictional. Never ask for or repeat real personal or medical information.

Today is {today:%A} {today.isoformat()}. Calendar:
{_calendar(today)}
The office is open weekdays 09:00-17:00 (lunch 12:00-13:00) with Dr. Rivera, Dr. Chen and Dr. Okafor (hygienist).

You act by proposing ONE action at a time. Reply with exactly one JSON object and nothing else:
{{"action": "<action name>", "argument": {{...}}}}

Actions:
{_actions_block()}

After each action (except reply) you will receive an OBSERVATION with the real result. Base every
statement on observations; never invent slot times or slot ids.

Rules:
- The schedule starts on the next weekday; there are no slots today or on weekends.
- If the patient gives no day, call find_next_openings (do not search day by day), then reply with 2-3 options.
- If an OBSERVATION lists "alternatives" or a "suggested_next_action", use them in your reply.
- Booking needs approval from a human staff member. You cannot approve, and you cannot skip it.
- If a requested time is unavailable, do not book a different time on your own. Offer alternatives and ask.
- Never say an appointment is booked unless an OBSERVATION shows "verified": true.
- You cannot cancel or change appointments, or give medical advice.
"""
    if variant in ("checklist", "combined"):
        prompt += CHECKLIST
    if variant in ("example", "combined"):
        prompt += EXAMPLE
    return prompt
