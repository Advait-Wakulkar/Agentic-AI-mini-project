"""Deterministic Python tools the agent can ask for.

The model never runs these itself. It proposes an action, the harness validates
it, and only then does Python call one of these functions. Each returns a plain
dict that becomes the model's next OBSERVATION.

There is no delete tool and no raw SQL. Cancel and reschedule exist, but like
booking they only PREPARE a request; a human approves before anything changes,
and a cancelled appointment is kept (status='cancelled') for the audit trail.
"""

import hashlib
import json
import re
import time
from datetime import date, datetime

from backend import config
from backend.db import PROFILE_FIELDS, Database, IntegrityError

OFFICE_INFO = {
    "hours": "Bright Smile Dental is open Monday to Friday, 9:00 to 17:00, closed 12:00-13:00 for lunch. Closed weekends.",
    "address": "Bright Smile Dental, 100 Example Street, Suite 2 (fictional office).",
    "services": "Cleanings, check-ups, fillings and emergency visits. Each appointment is 30 minutes.",
    "providers": "Dr. Rivera (general dentistry), Dr. Chen (general dentistry), Dr. Okafor (hygienist, cleanings).",
    "cancellation_policy": "Please cancel at least 24 hours ahead. You can cancel or move appointments here; staff approve every change.",
    "insurance": "The front desk checks insurance coverage for each patient; the assistant cannot confirm whether a specific plan is accepted. "
                 "The assistant can run a SIMULATED eligibility check (demo only, no real insurer is contacted) with your plan and member ID; "
                 "the front desk confirms final coverage.",
    "prices": "Prices depend on the treatment and your insurance. The front desk can give you a quote before your visit.",
}


def _provider_id(conn: Database, provider: str | None) -> str | None:
    """Match 'Rivera', 'Dr. Rivera' or 'P1' to a provider id. None if no match."""
    if not provider:
        return None
    needle = provider.lower().replace("dr.", "").replace("dr ", "").strip()
    for row in conn.query("SELECT id, name FROM providers"):
        if needle in (row["id"].lower(), row["name"].lower().replace("dr.", "").strip()):
            return row["id"]
    return None


def _slot_rows(conn, where: str, params: tuple):
    return conn.query(
        "SELECT s.id, s.date, s.start_time, p.name AS provider,"
        " a.id IS NOT NULL AS booked"
        " FROM slots s JOIN providers p ON p.id = s.provider_id"
        " LEFT JOIN appointments a ON a.slot_id = s.id AND a.status = 'booked'"
        f" WHERE {where} ORDER BY s.date, s.start_time, s.provider_id",
        params,
    )


def list_openings(conn, date: str, provider: str | None = None, part_of_day: str | None = None) -> dict:
    """Open slots on one date, optionally for one provider and morning/afternoon."""
    where, params = "s.date = ?", [date]
    if provider:
        pid = _provider_id(conn, provider)
        if pid is None:
            return {"ok": False, "error": f"Unknown provider '{provider}'. Known: Dr. Rivera, Dr. Chen, Dr. Okafor."}
        where += " AND s.provider_id = ?"
        params.append(pid)
    if part_of_day == "morning":
        where += " AND s.start_time < '12:00'"
    elif part_of_day == "afternoon":
        where += " AND s.start_time >= '12:00'"

    rows = _slot_rows(conn, where, tuple(params))
    if not rows:
        return {"ok": True, "date": date, "openings": [], "note": "The office has no slots on this date (weekend or outside the 2-week schedule)."}
    openings = [
        {"slot_id": r["id"], "time": r["start_time"], "provider": r["provider"]}
        for r in rows if not r["booked"]
    ]
    return {"ok": True, "date": date, "openings": openings[:12], "total_open": len(openings)}


def _open_slots(conn, where: str, params: tuple, limit: int) -> list[dict]:
    rows = _slot_rows(conn, where, params)
    return [{"slot_id": r["id"], "date": r["date"], "time": r["start_time"], "provider": r["provider"]}
            for r in rows if not r["booked"]][:limit]


def _nearest_open(conn, date: str, time: str, pid: str | None, limit: int = 4) -> list[dict]:
    """Open slots on the same date (same provider if given), closest to the requested time."""
    where, params = "s.date = ?", [date]
    if pid:
        where += " AND s.provider_id = ?"
        params.append(pid)
    slots = _open_slots(conn, where, tuple(params), 100)
    to_min = lambda t: int(t[:2]) * 60 + int(t[3:])
    slots.sort(key=lambda s: abs(to_min(s["time"]) - to_min(time)))
    return sorted(slots[:limit], key=lambda s: s["time"])


def find_next_openings(conn, from_date: str, provider: str | None = None, part_of_day: str | None = None,
                       limit: int = 5) -> dict:
    """The earliest open slots after from_date. For requests like 'sometime' or 'as soon as possible'."""
    where, params = "s.date > ?", [from_date]
    if provider:
        pid = _provider_id(conn, provider)
        if pid is None:
            return {"ok": False, "error": f"Unknown provider '{provider}'. Known: Dr. Rivera, Dr. Chen, Dr. Okafor."}
        where += " AND s.provider_id = ?"
        params.append(pid)
    if part_of_day == "morning":
        where += " AND s.start_time < '12:00'"
    elif part_of_day == "afternoon":
        where += " AND s.start_time >= '12:00'"
    openings = _open_slots(conn, where, tuple(params), limit)
    return {"ok": True, "openings": openings} if openings else {"ok": True, "openings": [], "note": "No open slots in the schedule."}


def check_availability(conn, date: str, time: str, provider: str | None = None) -> dict:
    """Is there an open slot at exactly this date and time (optionally with this provider)?

    When the answer is no, the observation already includes the nearest open
    alternatives, so the model can offer them in its next reply.
    """
    where, params = "s.date = ? AND s.start_time = ?", [date, time]
    pid = None
    if provider:
        pid = _provider_id(conn, provider)
        if pid is None:
            return {"ok": False, "error": f"Unknown provider '{provider}'. Known: Dr. Rivera, Dr. Chen, Dr. Okafor."}
        where += " AND s.provider_id = ?"
        params.append(pid)
    rows = _slot_rows(conn, where, tuple(params))
    if not rows or not [r for r in rows if not r["booked"]]:
        alternatives = _nearest_open(conn, date, time, pid) or _nearest_open(conn, date, time, None)
        if not alternatives:
            alternatives = find_next_openings(conn, date, provider, limit=4).get("openings", [])
        who = f" with {rows[0]['provider']}" if (rows and provider) else ""
        status, detail = (("not_offered", f"The office has no {time} slot on {date}.") if not rows
                          else ("unavailable", f"{time} on {date} is already booked{who}."))
        return {"ok": True, "status": status, "detail": detail, "alternatives": alternatives,
                "next_step": "Do not book. Reply offering these alternatives and ask the patient to choose."}
    open_rows = [r for r in rows if not r["booked"]]
    return {
        "ok": True,
        "status": "available",
        "slots": [{"slot_id": r["id"], "time": r["start_time"], "provider": r["provider"]} for r in open_rows],
    }


def get_office_info(conn, topic: str) -> dict:
    if topic in OFFICE_INFO:
        return {"ok": True, "topic": topic, "info": OFFICE_INFO[topic]}
    # Honest observation for an unknown topic: say so, do not guess.
    return {"ok": False, "error": f"No information on '{topic}'. Known topics: {', '.join(OFFICE_INFO)}."}


def slot_details(conn, slot_id: str) -> dict | None:
    rows = _slot_rows(conn, "s.id = ?", (slot_id,))
    if not rows:
        return None
    r = rows[0]
    return {"slot_id": r["id"], "date": r["date"], "time": r["start_time"], "provider": r["provider"], "booked": bool(r["booked"])}


# ---------------------------------------------------------------------------
# Booking is split in two so the harness can put a human in the middle:
#   1. prepare_booking  -> checks the slot, returns an approval request (no write)
#   2. commit_booking   -> called by the HARNESS only after a human said yes
#   3. verify_booking   -> independent re-read of the database
# ---------------------------------------------------------------------------

def prepare_booking(conn, slot_id: str, patient_alias: str, reason: str) -> dict:
    slot = slot_details(conn, slot_id)
    if slot is None:
        return {"ok": False, "error": f"Slot '{slot_id}' does not exist."}
    if slot["booked"]:
        return {"ok": False, "error": f"Slot {slot_id} ({slot['date']} {slot['time']}, {slot['provider']}) is already booked. Offer the patient another time."}
    return {
        "ok": True,
        "approval_request": {
            "slot_id": slot_id,
            "date": slot["date"],
            "time": slot["time"],
            "provider": slot["provider"],
            "patient_alias": patient_alias,
            "reason": reason,
        },
    }


def record_decision(conn, request: dict, approved: bool, decided_by: str) -> int:
    """Audit log: one row per human decision, for every kind of request."""
    return conn.insert(
        "INSERT INTO approvals (action, appointment_id, slot_id, patient_alias, reason, decision, decided_by, decided_at)"
        " VALUES (?,?,?,?,?,?,?,?)",
        (request.get("action", "book"), request.get("appointment_id"), request["slot_id"], request["patient_alias"],
         request["reason"], "approved" if approved else "denied", decided_by, datetime.now().isoformat(timespec="seconds")),
    )


def _valid_approval(conn, request: dict, approval_id: int) -> str | None:
    """None if approval_id is a HUMAN approval of exactly this request, else the reason it is not."""
    row = conn.one("SELECT * FROM approvals WHERE id = ?", (approval_id,))
    if row is None or row["decision"] != "approved" or row["decided_by"] in ("model", "seed"):
        return "No valid human approval for this request."
    if (row["action"] != request.get("action", "book") or row["slot_id"] != request["slot_id"]
            or row["patient_alias"] != request["patient_alias"]
            or (row["appointment_id"] or None) != (request.get("appointment_id") or None)):
        return "Approval does not match this request."
    return None


def commit_booking(conn, request: dict, approval_id: int) -> dict:
    """Write the appointment. Only the harness calls this, with a human approval id.

    The approval row must exist, be 'approved', and match this exact slot and
    patient. The UNIQUE constraint on slot_id stops double booking even if two
    approvals race for the same slot.
    """
    problem = _valid_approval(conn, request, approval_id)
    if problem:
        return {"ok": False, "error": problem}
    try:
        conn.run(
            "INSERT INTO appointments (slot_id, patient_alias, reason, approval_id, created_at) VALUES (?,?,?,?,?)",
            (request["slot_id"], request["patient_alias"], request["reason"], approval_id,
             datetime.now().isoformat(timespec="seconds")),
        )
    except IntegrityError:  # UNIQUE(slot_id): someone else got the slot first
        return {"ok": False, "error": f"Slot {request['slot_id']} was taken before it could be saved."}
    return {"ok": True}


def verify_booking(conn, slot_id: str, patient_alias: str) -> bool:
    """Independent check: re-read the database instead of trusting any return value."""
    row = conn.one(
        "SELECT 1 AS found FROM appointments WHERE slot_id = ? AND patient_alias = ? AND status = 'booked'",
        (slot_id, patient_alias),
    )
    return row is not None


def list_appointments(conn, patient_alias: str | None = None, include_cancelled: bool = False) -> list[dict]:
    sql = ("SELECT a.id, a.slot_id, s.date, s.start_time AS time, p.name AS provider, a.patient_alias,"
           " a.reason, a.approval_id, a.status, a.created_at, a.cancelled_at FROM appointments a"
           " JOIN slots s ON s.id = a.slot_id JOIN providers p ON p.id = s.provider_id WHERE 1 = 1")
    params: tuple = ()
    if not include_cancelled:
        sql += " AND a.status = 'booked'"
    if patient_alias:
        sql += " AND a.patient_alias = ?"
        params = (patient_alias,)
    return conn.query(sql + " ORDER BY s.date, s.start_time", params)


# ---------------------------------------------------------------------------
# The patient's own appointments: list, cancel, reschedule.
# The harness injects patient_alias from the session, so the model can only
# ever see or change the signed-in patient's own appointments.
# ---------------------------------------------------------------------------

def list_my_appointments(conn, patient_alias: str) -> dict:
    rows = list_appointments(conn, patient_alias)
    return {"ok": True, "appointments": [
        {"appointment_id": r["id"], "date": r["date"], "time": r["time"], "provider": r["provider"], "reason": r["reason"]}
        for r in rows
    ]} if rows else {"ok": True, "appointments": [], "note": "This patient has no upcoming appointments."}


def _own_appointment(conn, appointment_id, patient_alias: str) -> dict | None:
    return conn.one(
        "SELECT a.id, a.slot_id, s.date, s.start_time AS time, p.name AS provider, a.reason FROM appointments a"
        " JOIN slots s ON s.id = a.slot_id JOIN providers p ON p.id = s.provider_id"
        " WHERE a.id = ? AND a.patient_alias = ? AND a.status = 'booked'", (int(appointment_id), patient_alias))


def prepare_cancel(conn, appointment_id, patient_alias: str, reason: str) -> dict:
    appt = _own_appointment(conn, appointment_id, patient_alias)
    if appt is None:
        return {"ok": False, "error": f"Appointment {appointment_id} is not an upcoming appointment of this patient. "
                                      "Use list_my_appointments to see their appointments."}
    return {"ok": True, "approval_request": {
        "action": "cancel", "appointment_id": appt["id"], "slot_id": appt["slot_id"], "date": appt["date"],
        "time": appt["time"], "provider": appt["provider"], "patient_alias": patient_alias, "reason": reason}}


def prepare_reschedule(conn, appointment_id, new_slot_id: str, patient_alias: str, reason: str) -> dict:
    appt = _own_appointment(conn, appointment_id, patient_alias)
    if appt is None:
        return {"ok": False, "error": f"Appointment {appointment_id} is not an upcoming appointment of this patient. "
                                      "Use list_my_appointments to see their appointments."}
    new = slot_details(conn, new_slot_id)
    if new is None:
        return {"ok": False, "error": f"Slot '{new_slot_id}' does not exist."}
    if new["slot_id"] == appt["slot_id"]:
        return {"ok": False, "error": "The new time is the same as the current appointment."}
    if new["booked"]:
        return {"ok": False, "error": f"{new['date']} {new['time']} with {new['provider']} is already booked. Offer other times."}
    return {"ok": True, "approval_request": {
        "action": "reschedule", "appointment_id": appt["id"], "slot_id": new["slot_id"], "date": new["date"],
        "time": new["time"], "provider": new["provider"], "patient_alias": patient_alias, "reason": reason,
        "from": {"slot_id": appt["slot_id"], "date": appt["date"], "time": appt["time"], "provider": appt["provider"]}}}


def commit_action(conn, request: dict, approval_id: int) -> dict:
    """Carry out an approved request. Only the harness calls this, with a HUMAN approval id."""
    action = request.get("action", "book")
    if action == "book":
        return commit_booking(conn, request, approval_id)
    problem = _valid_approval(conn, request, approval_id)
    if problem:
        return {"ok": False, "error": problem}
    now = datetime.now().isoformat(timespec="seconds")
    try:
        with conn.transaction():  # reschedule = book new + cancel old, all or nothing
            if action == "reschedule":
                conn.run("INSERT INTO appointments (slot_id, patient_alias, reason, approval_id, created_at)"
                         " VALUES (?,?,?,?,?)",
                         (request["slot_id"], request["patient_alias"], request["reason"], approval_id, now))
            cancelled = conn.query(
                "UPDATE appointments SET status = 'cancelled', cancelled_at = ?, cancel_approval_id = ?"
                " WHERE id = ? AND patient_alias = ? AND status = 'booked' RETURNING id",
                (now, approval_id, request["appointment_id"], request["patient_alias"]))
            if not cancelled:
                raise RuntimeError("the appointment was no longer active")
    except IntegrityError:
        return {"ok": False, "error": f"{request['date']} {request['time']} was taken before it could be saved."}
    except RuntimeError as e:
        return {"ok": False, "error": f"Nothing changed: {e}."}
    return {"ok": True}


def verify_action(conn, request: dict) -> bool:
    """Independent re-read of the database after an approved request."""
    action = request.get("action", "book")
    if action == "book":
        return verify_booking(conn, request["slot_id"], request["patient_alias"])
    old = conn.one("SELECT status FROM appointments WHERE id = ? AND patient_alias = ?",
                   (request["appointment_id"], request["patient_alias"]))
    old_cancelled = old is not None and old["status"] == "cancelled"
    if action == "cancel":
        return old_cancelled
    return old_cancelled and verify_booking(conn, request["slot_id"], request["patient_alias"])


# ---------------------------------------------------------------------------
# The patient's profile and history, new-patient intake, and a SIMULATED
# insurance eligibility check. The harness injects patient_alias, so these only
# ever read or change the current patient's own record.
# ---------------------------------------------------------------------------

REQUIRED_PROFILE = ["first_name", "last_name", "date_of_birth", "phone", "email", "insurance_provider"]
SELF_PAY = {"self-pay", "self pay", "selfpay", "none", "no insurance", "uninsured", "cash"}
_NAME_RE = re.compile(r"^[A-Za-zÀ-ÿ][A-Za-zÀ-ÿ' .-]{0,39}$")
_FICTIONAL_PHONE = re.compile(r"^(?:\+?1[\s.-]?)?(?:\(?\d{3}\)?[\s.-]?)?555[\s.-]?01\d{2}$")   # 555-0100..0199
_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@([A-Za-z0-9-]+\.)*(example\.(com|org|net)|[A-Za-z0-9-]+\.test)$", re.I)
_MEMBER_ID = re.compile(r"^[A-Za-z]{1,4}\d{5,10}$")


def get_profile(conn, patient_alias: str) -> dict | None:
    return conn.one("SELECT alias, " + ", ".join(PROFILE_FIELDS) + ", insurance_status, insurance_checked_at,"
                    " registered_at FROM patients WHERE alias = ?", (patient_alias,))


def is_registered(conn, patient_alias: str) -> bool:
    p = get_profile(conn, patient_alias)
    return bool(p and p["registered_at"])


def past_visits(conn, patient_alias: str, today: str) -> list[dict]:
    """Visit history plus any appointments whose date has passed, newest first."""
    rows = conn.query("SELECT v.date, v.time, p.name AS provider, v.service, v.status, v.note FROM visit_history v"
                      " JOIN providers p ON p.id = v.provider_id WHERE v.patient_alias = ?", (patient_alias,))
    rows += [{"date": a["date"], "time": a["time"], "provider": a["provider"], "service": a["reason"],
              "status": "completed" if a["status"] == "booked" else "cancelled", "note": None}
             for a in list_appointments(conn, patient_alias, include_cancelled=True) if a["date"] < today]
    return sorted(rows, key=lambda r: (r["date"], r["time"]), reverse=True)


def get_my_history(conn, patient_alias: str, today: str) -> dict:
    """Everything on file for this patient: profile, upcoming appointments, past visits, cancellations."""
    profile = get_profile(conn, patient_alias)
    if profile is None:
        return {"ok": False, "error": "No patient record."}
    if not profile["registered_at"]:
        return {"ok": True, "registered": False, "upcoming": [], "past_visits": [],
                "note": "New patient: no record yet. Collect the required details and call register_patient."}
    upcoming = [a for a in list_my_appointments(conn, patient_alias)["appointments"] if a["date"] >= today]
    cancelled = [{"date": a["date"], "time": a["time"], "provider": a["provider"], "reason": a["reason"]}
                 for a in list_appointments(conn, patient_alias, include_cancelled=True)
                 if a["status"] == "cancelled" and a["date"] >= today]
    visits = past_visits(conn, patient_alias, today)
    completed = [v for v in visits if v["status"] == "completed"]
    return {"ok": True, "registered": True,
            "profile": {k: profile[k] for k in PROFILE_FIELDS} | {"insurance_status": profile["insurance_status"],
                                                                   "patient_since": profile["registered_at"][:10]},
            "upcoming": upcoming, "past_visits": visits, "cancelled_upcoming": cancelled,
            "summary": {"completed_visits": len(completed),
                        "last_visit": completed[0] if completed else None,
                        "no_shows": sum(1 for v in visits if v["status"] == "no_show")}}


def patient_context(conn, patient_alias: str, today: str) -> str:
    """A short, factual summary for the agent's system prompt (from the database, not from the model)."""
    h = get_my_history(conn, patient_alias, today)
    if not h.get("ok"):
        return ""
    if not h["registered"]:
        return ("NEW PATIENT: no record on file yet. Before you can request a booking you must collect, in the chat: "
                "first name, last name, date of birth, phone, email, and insurance provider with member ID (or 'self-pay'). "
                "Ask only for what is still missing, a few details at a time, and never invent or guess a value. "
                "This is a demo, so made-up details are expected (e.g. a phone in the 555-0100 to 555-0199 range and an "
                "email ending in @example.com), but do NOT judge the details yourself: some real addresses are approved "
                "by the office. When you have everything, call register_patient with exactly what the patient gave; "
                "Python checks every field and returns invalid_fields for anything not accepted. Only then ask them to "
                "correct those fields. They may look at openings first.")
    p, s = h["profile"], h["summary"]
    ins = ("self-pay" if (p["insurance_provider"] or "").lower() in SELF_PAY
           else f"{p['insurance_provider']} (member ID {p['insurance_member_id']}; eligibility {p['insurance_status']})")
    upcoming = "; ".join(f"#{a['appointment_id']} {a['date']} {a['time']} with {a['provider']} ({a['reason']})"
                         for a in h["upcoming"]) or "none"
    last = s["last_visit"]
    lines = [f"RETURNING PATIENT ON FILE (from the database): {p['first_name']} {p['last_name']}, born {p['date_of_birth']}, "
             f"phone {p['phone']}, email {p['email']}, patient since {p['patient_since']}.",
             f"Insurance: {ins}.",
             f"Upcoming appointments: {upcoming}.",
             f"Past visits: {s['completed_visits']} completed" + (f", last {last['service']} on {last['date']} with "
                                                                   f"{last['provider']}" if last else "")
             + (f", {s['no_shows']} no-show(s)" if s["no_shows"] else "") + ". Use get_my_history for the full list.",
             "Greet them by first name. Do not ask for details that are already on file."]
    return "\n".join(lines)


def _clean_phone(phone: str) -> str:
    digits = re.sub(r"\D", "", phone)
    return f"555-{digits[-4:]}"


def register_patient(conn, patient_alias: str, first_name: str, last_name: str, date_of_birth: str, phone: str,
                     email: str, insurance_provider: str, insurance_member_id: str | None = None, today: str = "") -> dict:
    """New-patient intake. Python checks every field; nothing is saved unless all of them pass."""
    profile = get_profile(conn, patient_alias)
    if profile is None:
        return {"ok": False, "error": "No patient record."}
    if profile["registered_at"]:
        return {"ok": False, "error": "This patient is already registered. Use the details on file."}
    values = {"first_name": first_name.strip(), "last_name": last_name.strip(), "date_of_birth": date_of_birth.strip(),
              "phone": phone.strip(), "email": email.strip(), "insurance_provider": insurance_provider.strip(),
              "insurance_member_id": (insurance_member_id or "").strip() or None}
    problems = {}
    for f in ("first_name", "last_name"):
        if not _NAME_RE.match(values[f]):
            problems[f] = "letters only (a made-up name is fine)"
    try:
        dob = date.fromisoformat(values["date_of_birth"])
        age = (date.fromisoformat(today) - dob).days / 365.25 if today else 30
        if not 0 <= age <= 120:
            problems["date_of_birth"] = "must be a real date in the past (age 0-120)"
    except ValueError:
        problems["date_of_birth"] = "use YYYY-MM-DD, a real calendar date"
    # Fictional data only, except contact details the project owner allowlisted in .env for a live test.
    allowed = config.REAL_CONTACT_ALLOWLIST
    allowed_phones = {re.sub(r"\D", "", a)[-10:] for a in allowed if "@" not in a and len(re.sub(r"\D", "", a)) >= 7}
    phone_ok = _FICTIONAL_PHONE.match(values["phone"]) or re.sub(r"\D", "", values["phone"])[-10:] in allowed_phones
    # (review: digits inside an allowlisted EMAIL used to count as an allowed phone number)
    if not phone_ok:
        problems["phone"] = "demo only: use a fictional number from 555-0100 to 555-0199"
    if not (_EMAIL_RE.match(values["email"]) or values["email"].lower() in allowed):
        problems["email"] = "demo only: use a made-up address ending in @example.com"
    elif values["email"].lower() in allowed and conn.one(
            "SELECT 1 AS taken FROM patients WHERE lower(email) = ? AND alias <> ? AND registered_at IS NOT NULL",
            (values["email"].lower(), patient_alias)):
        # (review: anyone could register with the owner's allowlisted address and route real email to them)
        problems["email"] = "this real address is already registered to another patient; use a made-up @example.com one"
    self_pay = values["insurance_provider"].lower() in SELF_PAY
    if not values["insurance_provider"]:
        problems["insurance_provider"] = "give the insurance company, or 'self-pay'"
    elif not self_pay and not (values["insurance_member_id"] and _MEMBER_ID.match(values["insurance_member_id"])):
        problems["insurance_member_id"] = "needed for insured patients: letters then digits, e.g. DD1234567 (made up)"
    if problems:
        return {"ok": False, "saved": False, "invalid_fields": problems,
                "next_step": "Nothing was saved. Ask the patient to correct these fields, then call register_patient again."}
    if self_pay:
        values["insurance_provider"], values["insurance_member_id"] = "self-pay", None
    if _FICTIONAL_PHONE.match(values["phone"]):
        values["phone"] = _clean_phone(values["phone"])   # (an allowlisted real number is kept as typed)
    conn.run("UPDATE patients SET first_name = ?, last_name = ?, date_of_birth = ?, phone = ?, email = ?,"
             " insurance_provider = ?, insurance_member_id = ?, insurance_status = ?, registered_at = ?"
             " WHERE alias = ? AND registered_at IS NULL",
             (*[values[f] for f in PROFILE_FIELDS], "self_pay" if self_pay else "unverified",
              datetime.now().isoformat(timespec="seconds"), patient_alias))
    saved = get_profile(conn, patient_alias)          # re-read: report what the DATABASE holds
    return {"ok": bool(saved and saved["registered_at"]), "saved": True, "patient": patient_alias,
            "profile": {k: saved[k] for k in PROFILE_FIELDS},
            "next_step": "Registered. Offer to verify their insurance (verify_insurance) unless self-pay, then help them book."}


# The simulated payer list. Plan names are generic company names; no real insurer is contacted.
IN_NETWORK = {"delta dental": "Delta Dental", "aetna": "Aetna", "cigna": "Cigna", "metlife": "MetLife",
              "guardian": "Guardian"}
OUT_OF_NETWORK = {"humana": "Humana", "united healthcare": "UnitedHealthcare", "unitedhealthcare": "UnitedHealthcare",
                  "blue cross": "Blue Cross", "bcbs": "Blue Cross"}


def verify_insurance(conn, patient_alias: str, insurance_provider: str | None = None,
                     member_id: str | None = None) -> dict:
    """SIMULATED dental eligibility check (demo only; no real insurer or clearinghouse is contacted).

    Deterministic rules so demos and tests are repeatable:
      Delta Dental / Aetna / Cigna / MetLife / Guardian  -> active, in network
      Humana / UnitedHealthcare / Blue Cross             -> active, out of network
      any other plan                                     -> payer not found
      member ID ending in 000                            -> coverage inactive
      member ID ending in 999                            -> payer timed out (shows a tool failure)
    """
    profile = get_profile(conn, patient_alias) or {}
    provider = (insurance_provider or profile.get("insurance_provider") or "").strip()
    member = (member_id or profile.get("insurance_member_id") or "").strip().upper()
    sim = {"simulated": True, "service": "Simulated eligibility check (demo only: no real insurer contacted)"}
    if not provider:
        return {"ok": False, **sim, "error": "No insurance provider given or on file. Ask the patient for it (or self-pay)."}
    if provider.lower() in SELF_PAY:
        return {"ok": True, **sim, "status": "self_pay", "detail": "Self-pay patient: no eligibility check needed."}
    if not _MEMBER_ID.match(member):
        return {"ok": False, **sim, "error": "A member ID is needed (letters then digits, e.g. DD1234567)."}
    time.sleep(config.INSURANCE_CHECK_DELAY)            # stands in for the round trip to a payer
    ref = "ELG-" + hashlib.sha1(f"{provider}|{member}".encode()).hexdigest()[:8].upper()
    steps = [f"Sent eligibility request (X12 270) for {provider}, member {member}, to the simulated clearinghouse",
             "Received eligibility response (X12 271)"]
    key = provider.lower()
    if member.endswith("999"):
        return {"ok": False, **sim, "status": "payer_timeout", "steps": steps[:1] + ["No response from payer (timeout)"],
                "error": "The (simulated) payer did not respond. The front desk will verify coverage manually."}
    if key in IN_NETWORK or key in OUT_OF_NETWORK:
        name = IN_NETWORK.get(key) or OUT_OF_NETWORK[key]
        if member.endswith("000"):
            result = {"status": "inactive", "plan": name, "in_network": None,
                      "detail": "Coverage for this member ID is not active."}
        else:
            result = {"status": "active", "plan": name, "in_network": key in IN_NETWORK,
                      "coverage": ({"preventive (cleanings, check-ups)": "100%", "basic (fillings)": "80%", "major": "50%"}
                                   if key in IN_NETWORK else {"all services": "out-of-network rates"}),
                      "detail": "Active coverage." + ("" if key in IN_NETWORK else " The office is out of network for this plan.")}
    else:
        result = {"status": "payer_not_found", "plan": provider, "in_network": None,
                  "detail": "This plan is not in the simulated payer list. The front desk will check it manually."}
    stored = {"active": "verified_in_network" if result.get("in_network") else "verified_out_of_network",
              "inactive": "inactive", "payer_not_found": "not_found"}[result["status"]]
    if profile.get("registered_at"):
        conn.run("UPDATE patients SET insurance_status = ?, insurance_checked_at = ? WHERE alias = ?",
                 (stored, datetime.now().isoformat(timespec="seconds"), patient_alias))
    return {"ok": True, **sim, "member_id": member, "reference": ref, "steps": steps, **result,
            "note": "Report this result to the patient and say it is a simulated check; the front desk confirms final coverage."}


def appointment_for_notice(conn, appointment_id) -> dict | None:
    """One booked appointment with everything a notice needs."""
    return conn.one("SELECT a.id, a.patient_alias, s.date, s.start_time AS time, p.name AS provider, a.reason"
                    " FROM appointments a JOIN slots s ON s.id = a.slot_id JOIN providers p ON p.id = s.provider_id"
                    " WHERE a.id = ? AND a.status = 'booked'", (int(appointment_id),))


def send_calendar_invite(conn, patient_alias: str, appointment_id) -> dict:
    """Chat tool: email the patient an .ics invite for one of THEIR booked appointments (simulated outbox)."""
    from backend import notify
    appt = _own_appointment(conn, appointment_id, patient_alias)
    if appt is None:
        return {"ok": False, "error": f"Appointment {appointment_id} is not a booked appointment of this patient. "
                                      "Use list_my_appointments to see their appointments."}
    if notify.recent_invite(conn, appt["id"]):
        return {"ok": False, "error": "An invite for this appointment was sent a few minutes ago; check Messages."}
    appt = appointment_for_notice(conn, appt["id"])
    profile = get_profile(conn, patient_alias) or {}
    name = f"{profile.get('first_name') or ''} {profile.get('last_name') or ''}".strip() or patient_alias
    ics = notify.make_ics(appt, name, OFFICE_INFO["address"])
    d = date.fromisoformat(appt["date"])
    when = f"{d:%A, %B} {d.day} at {appt['time']} with {appt['provider']}"
    result = notify.deliver(conn, appt, profile, "invite", "email",
                            f"Hi {profile.get('first_name') or 'there'}, here is your calendar invite for {when}. "
                            "Open the attached file to add it to your calendar.",
                            subject=f"Calendar invite: {when}", ics=ics, sent_by="chat")
    if result.get("ok"):
        result["note"] = ("Tell the patient the invite was emailed to the address on file (simulated outbox in this "
                          "demo) and that they can also download it under Messages.")
    return result


# Patient tools: the harness injects patient_alias (and today) so they only touch this patient's record.
PATIENT_TOOLS = {
    "get_my_history": get_my_history,
    "register_patient": register_patient,
    "verify_insurance": verify_insurance,
    "send_calendar_invite": send_calendar_invite,
}


# Read-only tools the model may call directly, by name.
READ_TOOLS = {
    "list_my_appointments": list_my_appointments,  # the harness adds patient_alias
    "list_openings": list_openings,
    "find_next_openings": find_next_openings,   # the harness adds from_date=today
    "check_availability": check_availability,
    "get_office_info": get_office_info,
}


# ---------------------------------------------------------------------------
# Staff approval queue (approval_requests). Every request the agent makes is
# stored here, so staff can see and decide pending requests from any chat.
# ---------------------------------------------------------------------------

def create_request(conn, session_id: str, request: dict) -> int:
    return conn.insert(
        "INSERT INTO approval_requests (session_id, patient_alias, action, request, created_at) VALUES (?,?,?,?,?)",
        (session_id, request["patient_alias"], request.get("action", "book"), json.dumps(request),
         datetime.now().isoformat(timespec="seconds")))


def close_request(conn, request_id: int, status: str, decided_by: str, approval_id: int | None, outcome: str) -> None:
    conn.run("UPDATE approval_requests SET status = ?, decided_at = ?, decided_by = ?, approval_id = ?, outcome = ?"
             " WHERE id = ?",
             (status, datetime.now().isoformat(timespec="seconds"), decided_by, approval_id, outcome, request_id))


def list_requests(conn, status: str | None = None, limit: int = 100) -> list[dict]:
    sql, params = "SELECT * FROM approval_requests", ()
    if status:
        sql, params = sql + " WHERE status = ?", (status,)
    rows = conn.query(sql + " ORDER BY id DESC LIMIT ?", params + (limit,))
    for r in rows:
        r["request"] = json.loads(r["request"])
    return rows


def get_request(conn, request_id: int) -> dict | None:
    row = conn.one("SELECT * FROM approval_requests WHERE id = ?", (request_id,))
    if row:
        row["request"] = json.loads(row["request"])
    return row


def decide_and_apply(conn, request: dict, approved: bool, decided_by: str) -> dict:
    """Record a HUMAN decision, carry it out if approved, and re-read the DB. Used by the agent and by staff."""
    approval_id = record_decision(conn, request, approved, decided_by)
    if not approved:
        return {"approval_id": approval_id, "approved": False, "saved": {"ok": False}, "verified": False,
                "outcome": "denied"}
    saved = commit_action(conn, request, approval_id)
    verified = verify_action(conn, request)
    outcome = "verified" if (saved.get("ok") and verified) else f"not_saved: {saved.get('error', 'could not verify')}"
    return {"approval_id": approval_id, "approved": True, "saved": saved, "verified": verified, "outcome": outcome}
