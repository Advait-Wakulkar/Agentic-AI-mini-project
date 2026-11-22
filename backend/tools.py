"""Deterministic Python tools the agent can ask for.

The model never runs these itself. It proposes an action, the harness validates
it, and only then does Python call one of these functions. Each returns a plain
dict that becomes the model's next OBSERVATION.

There is deliberately no delete or cancel tool, and no raw SQL access.
"""

from datetime import date, datetime

from backend.db import Database, IntegrityError

OFFICE_INFO = {
    "hours": "Bright Smile Dental is open Monday to Friday, 9:00 to 17:00, closed 12:00-13:00 for lunch. Closed weekends.",
    "address": "Bright Smile Dental, 100 Example Street, Suite 2 (fictional office).",
    "services": "Cleanings, check-ups, fillings and emergency visits. Each appointment is 30 minutes.",
    "providers": "Dr. Rivera (general dentistry), Dr. Chen (general dentistry), Dr. Okafor (hygienist, cleanings).",
    "cancellation_policy": "Please call the front desk at least 24 hours ahead to cancel. The assistant cannot cancel appointments.",
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
        " LEFT JOIN appointments a ON a.slot_id = s.id"
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
    return conn.insert(
        "INSERT INTO approvals (slot_id, patient_alias, reason, decision, decided_by, decided_at) VALUES (?,?,?,?,?,?)",
        (request["slot_id"], request["patient_alias"], request["reason"],
         "approved" if approved else "denied", decided_by, datetime.now().isoformat(timespec="seconds")),
    )


def commit_booking(conn, request: dict, approval_id: int) -> dict:
    """Write the appointment. Only the harness calls this, with a human approval id.

    The approval row must exist, be 'approved', and match this exact slot and
    patient. The UNIQUE constraint on slot_id stops double booking even if two
    approvals race for the same slot.
    """
    row = conn.one("SELECT * FROM approvals WHERE id = ?", (approval_id,))
    if row is None or row["decision"] != "approved" or row["decided_by"] in ("model", "seed"):
        return {"ok": False, "error": "No valid human approval for this booking."}
    if row["slot_id"] != request["slot_id"] or row["patient_alias"] != request["patient_alias"]:
        return {"ok": False, "error": "Approval does not match this slot and patient."}
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
        "SELECT 1 AS found FROM appointments WHERE slot_id = ? AND patient_alias = ?", (slot_id, patient_alias)
    )
    return row is not None


def list_appointments(conn, patient_alias: str | None = None) -> list[dict]:
    sql = ("SELECT a.id, a.slot_id, s.date, s.start_time AS time, p.name AS provider, a.patient_alias,"
           " a.reason, a.approval_id, a.created_at FROM appointments a JOIN slots s ON s.id = a.slot_id"
           " JOIN providers p ON p.id = s.provider_id")
    params: tuple = ()
    if patient_alias:
        sql += " WHERE a.patient_alias = ?"
        params = (patient_alias,)
    return conn.query(sql + " ORDER BY s.date, s.start_time", params)


# Read-only tools the model may call directly, by name.
READ_TOOLS = {
    "list_openings": list_openings,
    "find_next_openings": find_next_openings,   # the harness adds from_date=today
    "check_availability": check_availability,
    "get_office_info": get_office_info,
}
