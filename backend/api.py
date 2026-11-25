"""HTTP API for the Next.js UI. Same agents, same harness as the CLI.

    .venv/bin/uvicorn backend.api:app --port 8000

Two kinds of user:
  - patients chat with the agents. When an agent asks to book / cancel /
    reschedule, the turn pauses and the request goes into the staff queue.
    Patients cannot approve anything.
  - staff see the queue on the dashboard and approve or deny. The patient's
    chat then continues (it polls /api/session/{id}/events).

With SUPABASE_URL + SUPABASE_ANON_KEY set, everyone signs in with Supabase Auth
(see backend/auth.py). Without them the app runs in demo mode.
"""

import threading

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel

from backend import auth, config, db, notify, reminders, tools
from backend.agent import Session, resume_turn, run_turn
from backend.decision import get_decider
from backend.llm import PROVIDERS, LLMError, get_provider, provider_status

app = FastAPI(title="Bright Smile Dental scheduling agents")


@app.exception_handler(db.DatabaseError)
def _db_unavailable(_: Request, exc: db.DatabaseError):
    """Missing DATABASE_URL or Supabase unreachable: tell the UI plainly."""
    return JSONResponse(status_code=503, content={"detail": str(exc)})


_lock = threading.Lock()                     # one agent turn at a time on the shared database connection
_init_lock = threading.Lock()                # the UI fires several requests at once on page load
_state: dict = {"conn": None, "sessions": {}, "llm": None, "decider": None}


def _conn():
    with _init_lock:
        if _state["conn"] is None:
            _state["conn"] = db.init_db(config.DATABASE_URL, config.today())
    return _state["conn"]


def _llm():
    if _state["llm"] is None:
        try:
            _state["llm"] = get_provider()
        except LLMError as e:
            raise HTTPException(503, str(e))
    return _state["llm"]


def _decider():
    if _state["decider"] is None:
        _state["decider"] = get_decider()
    return _state["decider"]


def _patient_for(user, requested: str | None = None) -> str:
    """Signed in: the user's own alias. Demo mode: the alias picked in the dropdown."""
    if user is not None:
        return auth.ensure_patient(_conn(), user)
    patient = requested or db.PATIENTS[0]
    if patient in db.PATIENTS:
        return patient
    # A demo "New patient" created by /api/session (fictional alias like 'New Patient 3F9A').
    if patient.startswith(db.NEW_PATIENT_PREFIX) and _conn().one("SELECT 1 AS ok FROM patients WHERE alias = ?", (patient,)):
        return patient
    raise HTTPException(400, f"Patient must be one of {db.PATIENTS} or a demo new patient (fictional aliases only).")


def _session(session_id: str, user) -> Session:
    s = _state["sessions"].get(session_id)
    if s is None:
        raise HTTPException(404, "This conversation has ended (the server restarted). Start a new one.")
    if user is not None and s.owner_id != user["id"]:
        raise HTTPException(403, "This conversation belongs to someone else.")
    return s


def _require_staff(user=Depends(auth.current_user)):
    if not auth.is_staff(user):
        raise HTTPException(403, "Staff only.")
    return user


def _turn_payload(session: Session, turn) -> dict:
    return {
        "reply": turn.reply,
        "stop_reason": turn.stop_reason,
        "agent": turn.agent,
        "steps": turn.steps,
        "decision": turn.decision,
        "pending": session.pending["request"] if session.pending else None,
        "record_path": turn.record_path,
    }


class NewSession(BaseModel):
    patient: str | None = None
    variant: str = "baseline"


class ChatIn(BaseModel):
    session_id: str
    message: str


class DecideIn(BaseModel):
    approved: bool


class ModelIn(BaseModel):
    provider: str
    model: str = ""


# ---------------------------------------------------------------------------
# General
# ---------------------------------------------------------------------------

@app.get("/api/config")
def get_config():
    try:
        llm_name, llm_error = _llm().name, None
    except HTTPException as e:  # e.g. missing key: the UI still loads and shows why
        llm_name, llm_error = None, e.detail
    return {"llm": llm_name, "llm_error": llm_error, "decider": _decider().name, "database": _conn().label,
            "today": config.today().isoformat(), "patients": db.PATIENTS, "providers": [p[1] for p in db.PROVIDERS],
            "auth_enabled": auth.enabled()}


@app.get("/api/me")
def me(user=Depends(auth.current_user)):
    if user is None:
        return {"signed_in": False, "demo": True, "is_staff": True}
    return {"signed_in": True, "demo": False, "email": user["email"], "patient": auth.ensure_patient(_conn(), user),
            "is_staff": auth.is_staff(user)}


@app.get("/api/models")
def models():
    """Providers the UI can switch to, with whether each is ready (key set / server running)."""
    current = _state["llm"].name if _state["llm"] else None
    return {"current": current, "providers": provider_status()}


@app.post("/api/model")
def set_model(body: ModelIn, _staff=Depends(_require_staff)):
    """Switch the language model at runtime (staff only). Conversations continue with the new model."""
    if body.provider not in PROVIDERS:
        raise HTTPException(400, f"Unknown provider. Choose from {list(PROVIDERS)}.")
    try:
        new = get_provider(body.provider, body.model.strip() or None)
    except LLMError as e:
        raise HTTPException(400, str(e))
    with _lock:
        _state["llm"] = new
    return {"llm": new.name}


# ---------------------------------------------------------------------------
# Patient: chat with the agents
# ---------------------------------------------------------------------------

@app.post("/api/session")
def new_session(body: NewSession, user=Depends(auth.current_user)):
    if body.patient == "new" and user is None:
        alias = db.create_new_patient(_conn())          # demo intake: an empty, unregistered record
    else:
        alias = _patient_for(user, body.patient)
    s = Session(conn=_conn(), patient_alias=alias, prompt_variant=body.variant,
                owner_id=user["id"] if user else None)
    _state["sessions"][s.id] = s
    return {"session_id": s.id, "patient": s.patient_alias, "today": s.today.isoformat()}


@app.post("/api/chat")
def chat(body: ChatIn, user=Depends(auth.current_user)):
    session = _session(body.session_id, user)
    if not body.message.strip():
        raise HTTPException(400, "Empty message.")
    with _lock:
        # approver returns None: a staff member decides later on the dashboard, so the turn pauses.
        turn = run_turn(session, body.message.strip(), _llm(), _decider(), approver=lambda request: None, channel="ui")
    return _turn_payload(session, turn)


@app.get("/api/session/{session_id}/events")
def session_events(session_id: str, user=Depends(auth.current_user)):
    """Patient chat polls this while a request waits for staff: replies that arrived after a staff decision."""
    session = _session(session_id, user)
    events, session.events = session.events, []
    return {"pending": session.pending["request"] if session.pending else None, "events": events}


@app.get("/api/appointments")
def appointments(patient: str | None = None, user=Depends(auth.current_user)):
    return tools.list_appointments(_conn(), _patient_for(user, patient))


@app.get("/api/history")
def history(patient: str | None = None, user=Depends(auth.current_user)):
    """The patient's own record: profile, upcoming and past visits (the same data the agent sees)."""
    return tools.get_my_history(_conn(), _patient_for(user, patient), config.today().isoformat())


@app.get("/api/schedule")
def schedule(start: str | None = None, days: int = 5, patient: str | None = None, user=Depends(auth.current_user)):
    """Week grid: open / booked / mine, straight from the database. Other patients' names are never sent."""
    me_alias = _patient_for(user, patient)
    conn = _conn()
    all_dates = [r["date"] for r in conn.query("SELECT DISTINCT date FROM slots WHERE date > ? ORDER BY date",
                                               (config.today().isoformat(),))]
    if not all_dates:
        return {"dates": [], "all_dates": [], "times": db.TIMES, "providers": [], "slots": []}
    first = start if start in all_dates else all_dates[0]
    dates = all_dates[all_dates.index(first):][:days]
    rows = conn.query(
        "SELECT s.id, s.date, s.start_time, p.name AS provider, a.patient_alias"
        " FROM slots s JOIN providers p ON p.id = s.provider_id"
        " LEFT JOIN appointments a ON a.slot_id = s.id AND a.status = 'booked'"
        f" WHERE s.date IN ({','.join('?' * len(dates))}) ORDER BY s.date, s.start_time, s.provider_id", tuple(dates))
    slots = [{"slot_id": r["id"], "date": r["date"], "time": r["start_time"], "provider": r["provider"],
              "status": "open" if r["patient_alias"] is None else ("mine" if r["patient_alias"] == me_alias else "booked")}
             for r in rows]
    return {"dates": dates, "all_dates": all_dates, "times": db.TIMES,
            "providers": [p[1] for p in db.PROVIDERS], "slots": slots}


# ---------------------------------------------------------------------------
# Staff: approval queue, decisions, audit history
# ---------------------------------------------------------------------------

@app.get("/api/staff/requests")
def staff_requests(status: str | None = "pending", staff=Depends(_require_staff)):
    rows = tools.list_requests(_conn(), status or None)
    for r in rows:
        r["chat_live"] = r["session_id"] in _state["sessions"]
    return rows


@app.post("/api/staff/requests/{request_id}/decide")
def staff_decide(request_id: int, body: DecideIn, staff=Depends(_require_staff)):
    """A HUMAN decision. If the patient's chat is still open, its agent continues; otherwise the decision is
    applied directly (the request holds everything needed) and the patient sees the result in their list."""
    conn = _conn()
    who = auth.staff_name(staff)
    with _lock:
        row = tools.get_request(conn, request_id)
        if row is None:
            raise HTTPException(404, "No such request.")
        if row["status"] != "pending":
            raise HTTPException(409, f"This request was already {row['status']}.")
        session = _state["sessions"].get(row["session_id"])
        if session and session.pending and session.pending.get("request_id") == request_id:
            turn = resume_turn(session, body.approved, _llm(), decider_name=_decider().name, channel=who)
            session.events.append(_turn_payload(session, turn))
        else:
            result = tools.decide_and_apply(conn, row["request"], body.approved, who)
            tools.close_request(conn, request_id, "approved" if body.approved else "denied", who,
                                result["approval_id"], result["outcome"])
        done = tools.get_request(conn, request_id)
    return {"id": request_id, "status": done["status"], "outcome": done["outcome"], "decided_by": done["decided_by"]}


@app.get("/api/staff/history")
def staff_history(limit: int = 100, staff=Depends(_require_staff)):
    """Audit log: every human decision with what happened afterwards."""
    return _conn().query(
        "SELECT a.id, a.action, a.slot_id, a.appointment_id, a.patient_alias, a.reason, a.decision, a.decided_by,"
        " a.decided_at, s.date, s.start_time AS time, p.name AS provider, r.id AS request_id, r.outcome"
        " FROM approvals a LEFT JOIN slots s ON s.id = a.slot_id LEFT JOIN providers p ON p.id = s.provider_id"
        " LEFT JOIN approval_requests r ON r.approval_id = a.id"
        " WHERE a.decided_by <> 'seed' ORDER BY a.id DESC LIMIT ?", (min(limit, 500),))


# ---------------------------------------------------------------------------
# Reminder agent (background) and the messages it sends
# ---------------------------------------------------------------------------

@app.on_event("startup")
def _start_reminder_agent():
    if not config.REMINDERS_ENABLED or not config.DATABASE_URL:
        return
    _conn()                                    # make sure the schema exists before the worker queries it
    worker = reminders.ReminderWorker()
    worker.start()
    _state["reminders"] = worker


@app.on_event("shutdown")
def _stop_reminder_agent():
    if _state.get("reminders"):
        _state["reminders"].stop()


@app.get("/api/notifications")
def patient_notifications(patient: str | None = None, user=Depends(auth.current_user)):
    """The patient's own messages (confirmations, reminders, invites)."""
    return notify.list_notifications(_conn(), _patient_for(user, patient), limit=50)


@app.get("/api/notifications/{notification_id}/invite.ics")
def invite_file(notification_id: int, patient: str | None = None, user=Depends(auth.current_user)):
    row = notify.get_notification(_conn(), notification_id)
    if row is None or row["patient_alias"] != _patient_for(user, patient) or not row["ics"]:
        raise HTTPException(404, "No calendar invite here.")
    return Response(row["ics"], media_type="text/calendar",
                    headers={"Content-Disposition": f'attachment; filename="appointment-{row["appointment_id"]}.ics"'})


@app.get("/api/staff/notifications")
def staff_notifications(limit: int = 100, _staff=Depends(_require_staff)):
    worker = _state.get("reminders")
    return {"enabled": bool(worker), "interval_seconds": config.REMINDER_INTERVAL_SECONDS,
            "window_hours": config.REMINDER_WINDOW_HOURS, "smtp": bool(config.SMTP_HOST),
            "last_run": (worker.last_summary or {}).get("ran_at") if worker else None,
            "last_error": worker.last_error if worker else None,
            "notifications": notify.list_notifications(_conn(), limit=min(limit, 500))}


@app.post("/api/staff/reminders/run")
def run_reminders_now(_staff=Depends(_require_staff)):
    """Staff 'Run now': one pass of the reminder agent (same code the background worker runs)."""
    with _lock:
        summary = reminders.run_once(_conn(), _llm())
    return {k: v for k, v in summary.items() if k != "results"} | {
        "results": [{k: v for k, v in r.items() if k != "steps"} for r in summary["results"]]}


@app.post("/api/reset")
def reset(_staff=Depends(_require_staff)):
    """Re-seed the fictional schedule and forget all conversations (staff only)."""
    with _lock:
        if _state["conn"] is not None:
            _state["conn"].close()
        _state["conn"] = db.init_db(config.DATABASE_URL, config.today(), reset=True)
        _state["sessions"].clear()
    return {"ok": True}
