"""The REMINDER agent: a background agent that notifies patients without being asked.

It runs on its own schedule (a worker thread inside the API, or `python -m backend.reminders --once`), not in
response to a chat message.

    1. Python picks the work (due_jobs): new bookings that have no confirmation yet, and appointments starting
       within REMINDER_WINDOW_HOURS that have had no reminder. The model never decides WHO gets a message.
    2. For each job, the language model writes the messages and proposes actions: send_email, send_sms, finish.
    3. Python checks every message (the right date and time, no other times, no invented phone numbers, nothing
       medical), fills in the recipient from the patient record, attaches the calendar invite, and records it.
    4. If the model fails, Python sends a correct template instead (grounded fallback), so a patient is never left
       without a notice and never gets a wrong one.

Each confirmation / reminder is sent at most once per appointment and channel (a unique index in the database).
"""

import argparse
import json
import logging
import re
import threading
from datetime import date, datetime, timedelta
from typing import Optional

from backend import config, db, notify, tools
from backend.agent import _invented_phone, mentions_date, mentions_time, times_in
from backend.llm import LLMError, get_provider
from backend.prompts import _actions_block
from backend.validator import parse_proposal, validate

log = logging.getLogger("reminders")
_MEDICAL = re.compile(r"\b(ibuprofen|advil|tylenol|acetaminophen|antibiotic|painkiller|dosage|\d+\s?mg|prescription)\b", re.I)


def clinic_now() -> datetime:
    """'Now' on the clinic's calendar (CLINIC_TODAY can pin the date for demos and tests)."""
    return datetime.combine(config.today(), datetime.now().time())


def _starts_at(appt: dict) -> datetime:
    return datetime.combine(date.fromisoformat(appt["date"]), datetime.strptime(appt["time"], "%H:%M").time())


def due_jobs(conn, now: datetime) -> list[dict]:
    """The notices that are due now (decided by Python). Seeded filler bookings are not real patients' visits."""
    rows = conn.query(
        "SELECT a.id, a.patient_alias, s.date, s.start_time AS time, p.name AS provider, a.reason,"
        " (SELECT MIN(created_at) FROM notifications n WHERE n.appointment_id = a.id AND n.kind = 'confirmation')"
        "   AS confirmed_at,"
        " EXISTS (SELECT 1 FROM notifications n WHERE n.appointment_id = a.id AND n.kind = 'reminder') AS reminded"
        " FROM appointments a JOIN slots s ON s.id = a.slot_id JOIN providers p ON p.id = s.provider_id"
        " JOIN approvals ap ON ap.id = a.approval_id"
        " WHERE a.status = 'booked' AND ap.decided_by <> 'seed' ORDER BY s.date, s.start_time")
    window = timedelta(hours=config.REMINDER_WINDOW_HOURS)
    jobs = []
    for r in rows:
        start = _starts_at(r)
        if start <= now:
            continue                                    # already happened
        appt = {k: r[k] for k in ("id", "patient_alias", "date", "time", "provider", "reason")}
        if not r["confirmed_at"]:
            jobs.append({"kind": "confirmation", "appt": appt})
        elif not r["reminded"] and start - now <= window \
                and datetime.fromisoformat(r["confirmed_at"]) <= start - window:
            # (a booking made inside the window already got its confirmation; no second message minutes later)
            jobs.append({"kind": "reminder", "appt": appt})
    return jobs


def _when(appt: dict) -> str:
    d = date.fromisoformat(appt["date"])
    return f"{d:%A, %B} {d.day} at {appt['time']} with {appt['provider']}"


SYSTEM = """You are the REMINDER agent for Bright Smile Dental, a fictional dental office.
You run in the background (no chat). For the job below, notify the patient by email AND by text message, then finish.
All data is fictional.

Rules:
- State the exact date, time and dentist from the JOB. Do not mention any other time.
- Confirmation: the appointment was just booked (approved by staff). Reminder: it is coming up soon.
- Friendly and short. Greet them by first name. Say they can reply in the chat to move or cancel.
- No medical advice. No phone numbers. Do not ask them to confirm by text.
- The calendar invite is attached to the email automatically; you may mention it.
- Python fills in the recipient; never write an email address.

Reply with exactly one JSON object per step: {{"action": "<name>", "argument": {{...}}}}
Actions:
{actions}
"""


def _message_problem(text: str, appt: dict, today: date) -> Optional[str]:
    d = date.fromisoformat(appt["date"])
    if not mentions_time(text, appt["time"]):
        return f"the message must state the appointment time {appt['time']}."
    if not mentions_date(text, d, today) and d.isoformat() not in text:
        return f"the message must state the date ({d:%A, %B} {d.day})."
    extra = times_in(text) - {appt["time"]}
    if extra:
        return f"the message mentions other times ({', '.join(sorted(extra))}); only {appt['time']} is correct."
    phone = _invented_phone(text)
    if phone:
        return f"do not include phone numbers ({phone})."
    if _MEDICAL.search(text):
        return "no medical advice in reminders."
    return None


def _template(kind: str, appt: dict, first: str) -> tuple[str, str, str]:
    when = _when(appt)
    lead = "Your appointment is confirmed" if kind == "confirmation" else "Reminder: your appointment is coming up"
    subject = f"{'Confirmed' if kind == 'confirmation' else 'Reminder'}: {when}"
    email = (f"Hi {first},\n\n{lead}: {when} at Bright Smile Dental. The calendar invite is attached.\n\n"
             "To move or cancel, just reply in the chat.\n\nBright Smile Dental")
    sms = f"Bright Smile Dental: {lead.lower()}, {when}. Reply in the chat to move or cancel."
    return subject, email, sms


def run_job(conn, llm, job: dict, now: datetime) -> dict:
    """One notice: the model writes and proposes; Python checks, sends and records. Returns a trace."""
    appt, kind, today = job["appt"], job["kind"], now.date()
    profile = tools.get_profile(conn, appt["patient_alias"]) or {}
    first = profile.get("first_name") or "there"
    name = f"{first} {profile.get('last_name') or ''}".strip()
    ics = notify.make_ics(appt, name, tools.OFFICE_INFO["address"])
    facts = {"kind": kind, "first_name": first, "date": appt["date"], "weekday": f"{date.fromisoformat(appt['date']):%A}",
             "time": appt["time"], "dentist": appt["provider"], "reason": appt["reason"]}
    history = [{"role": "system", "content": SYSTEM.format(actions=_actions_block("reminders"))},
               {"role": "user", "content": "JOB: " + json.dumps(facts)}]
    steps, sent, invalid = [], {}, 0

    def observe(obs):
        steps.append({"kind": "TOOL_OBSERVATION", "observation": obs})
        history.append({"role": "user", "content": "OBSERVATION: " + json.dumps(obs)})

    for _ in range(config.MAX_STEPS):
        try:
            raw = llm.chat(history)
        except LLMError as e:
            steps.append({"kind": "LLM_ERROR", "error": str(e)})
            break
        history.append({"role": "assistant", "content": raw})
        proposal, parse_error = parse_proposal(raw)
        errors = [f"parser: {parse_error}"] if parse_error else validate(proposal, today, conn, agent="reminders")
        steps.append({"kind": "MODEL_PROPOSED", "proposal": proposal, "raw": raw[:500]})
        action, arg = (proposal or {}).get("action"), (proposal or {}).get("argument") or {}
        if not errors and action in ("send_email", "send_sms"):
            channel = "email" if action == "send_email" else "sms"
            if channel in sent:
                errors = [f"check 'once_per_channel': the {channel} was already sent."]
            else:
                problem = _message_problem(arg["message"], appt, today)
                if problem:
                    errors = [f"check 'grounded_message': {problem} Rewrite it."]
        if not errors and action == "finish" and "email" not in sent:
            errors = ["check 'email_first': send the email before finishing."]
        if errors:
            invalid += 1
            steps.append({"kind": "VALIDATION", "passed": False, "errors": errors})
            observe({"ok": False, "rejected": True, "errors": errors})
            if invalid >= config.MAX_INVALID:
                break
            continue
        steps.append({"kind": "VALIDATION", "passed": True})
        if action == "finish":
            break
        channel = "email" if action == "send_email" else "sms"
        result = notify.deliver(conn, appt, profile, kind, channel, arg["message"],
                                subject=arg.get("subject"), ics=ics if channel == "email" else None, at=now)
        sent[channel] = result
        observe(result)
        if "email" in sent and "sms" in sent:
            break

    # Grounded fallback: whatever the model did not send correctly, Python sends from a template.
    fallback = []
    subject, email_body, sms_body = _template(kind, appt, first)
    for channel, body in (("email", email_body), ("sms", sms_body)):
        if channel not in sent:
            sent[channel] = notify.deliver(conn, appt, profile, kind, channel, body,
                                           subject=subject if channel == "email" else None,
                                           ics=ics if channel == "email" else None, sent_by="reminder-fallback", at=now)
            fallback.append(channel)
    if fallback:
        steps.append({"kind": "FALLBACK", "channels": fallback})
    return {"appointment_id": appt["id"], "patient": appt["patient_alias"], "kind": kind, "when": _when(appt),
            "sent": {c: r.get("status") or ("duplicate" if r.get("duplicate") else "failed") for c, r in sent.items()},
            "fallback": fallback, "rejected": invalid, "steps": steps}


def run_once(conn, llm, now: Optional[datetime] = None) -> dict:
    """Run every due job once. Safe to call repeatedly: nothing is ever sent twice."""
    now = now or clinic_now()
    jobs = []
    for job in due_jobs(conn, now):
        try:
            jobs.append(run_job(conn, llm, job, now))
        except Exception as e:      # (review: one bad job used to abort the whole run, every tick)
            log.warning("reminder job for appointment %s failed: %s", job["appt"]["id"], str(e)[:200])
            jobs.append({"appointment_id": job["appt"]["id"], "patient": job["appt"]["patient_alias"], "kind": job["kind"],
                         "when": _when(job["appt"]), "sent": {}, "fallback": [], "rejected": 0, "error": str(e)[:200],
                         "steps": []})
    return {"ran_at": datetime.now().isoformat(timespec="seconds"), "clinic_now": now.isoformat(timespec="minutes"),
            "jobs": len(jobs), "fallbacks": sum(1 for j in jobs if j["fallback"]), "results": jobs}


class ReminderWorker(threading.Thread):
    """Background loop inside the API: every REMINDER_INTERVAL_SECONDS, run the reminder agent.

    It uses its OWN database connection and language-model client, so it never blocks or waits on chat turns.
    """

    def __init__(self, interval: Optional[float] = None, llm=None, conn_factory=None):
        super().__init__(name="reminder-agent", daemon=True)
        self.interval = interval or config.REMINDER_INTERVAL_SECONDS
        self._llm, self._conn_factory = llm, conn_factory
        self._halt = threading.Event()
        self.last_summary: Optional[dict] = None
        self.last_error: Optional[str] = None
        self.ticks = 0

    def run(self):
        conn = None
        while not self._halt.is_set():
            try:
                if conn is None:
                    conn = (self._conn_factory or (lambda: db.Database(config.DATABASE_URL)))()
                    self._llm = self._llm or get_provider()
                self.last_summary = run_once(conn, self._llm)
                self.last_error = None
            except Exception as e:     # keep running: a bad tick must not kill the background agent
                self.last_error = str(e)[:300]
                log.warning("reminder agent tick failed: %s", self.last_error)
                conn = None if isinstance(e, db.DatabaseError) else conn
            self.ticks += 1
            self._halt.wait(self.interval)
        if conn is not None and self._conn_factory is None:
            conn.close()

    def stop(self):
        self._halt.set()


def main():
    ap = argparse.ArgumentParser(description="Run the reminder agent once (e.g. from cron).")
    ap.add_argument("--once", action="store_true", help="run all due jobs once and exit")
    ap.add_argument("--provider")
    ap.add_argument("--model")
    args = ap.parse_args()
    conn = db.init_db(config.DATABASE_URL, config.today())
    try:
        summary = run_once(conn, get_provider(args.provider, args.model))
        print(json.dumps({k: v for k, v in summary.items() if k != "results"} |
                         {"results": [{k: v for k, v in r.items() if k != "steps"} for r in summary["results"]]},
                         indent=2))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
