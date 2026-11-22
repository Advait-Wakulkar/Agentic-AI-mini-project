"""HTTP API for the Next.js UI. Same agent, same harness as the CLI.

    .venv/bin/uvicorn backend.api:app --port 8000

Approval in the UI: when the model proposes a booking, the turn pauses with
stop_reason 'awaiting_approval'. A staff member clicks Approve or Deny, which
calls POST /api/approve. The model has no endpoint and no way to approve.
"""

import threading

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from backend import config, db, tools
from backend.agent import Session, resume_turn, run_turn
from backend.decision import get_decider
from backend.llm import PROVIDERS, LLMError, get_provider, provider_status

app = FastAPI(title="Bright Smile Dental scheduling agent")


@app.exception_handler(db.DatabaseError)
def _db_unavailable(_: Request, exc: db.DatabaseError):
    """Missing DATABASE_URL or Supabase unreachable: tell the UI plainly."""
    return JSONResponse(status_code=503, content={"detail": str(exc)})

_lock = threading.Lock()                     # one agent turn at a time on the shared database connection
_state: dict = {"conn": None, "sessions": {}, "llm": None, "decider": None, "llm_error": None}


_init_lock = threading.Lock()                # the UI fires several requests at once on page load


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


def _session(session_id: str) -> Session:
    s = _state["sessions"].get(session_id)
    if s is None:
        raise HTTPException(404, "Unknown session. Start a new conversation.")
    return s


def _turn_payload(session: Session, turn) -> dict:
    return {
        "reply": turn.reply,
        "stop_reason": turn.stop_reason,
        "steps": turn.steps,
        "decision": turn.decision,
        "pending": session.pending["request"] if session.pending else None,
        "record_path": turn.record_path,
    }


class NewSession(BaseModel):
    patient: str = "Test Patient A"
    variant: str = "baseline"


class ChatIn(BaseModel):
    session_id: str
    message: str


class ApproveIn(BaseModel):
    session_id: str
    approved: bool


class ModelIn(BaseModel):
    provider: str
    model: str = ""


@app.get("/api/config")
def get_config():
    try:
        llm_name, llm_error = _llm().name, None
    except HTTPException as e:  # e.g. missing key: the UI still loads and shows why
        llm_name, llm_error = None, e.detail
    return {"llm": llm_name, "llm_error": llm_error, "decider": _decider().name, "database": _conn().label,
            "today": config.today().isoformat(), "patients": db.PATIENTS, "providers": [p[1] for p in db.PROVIDERS]}


@app.get("/api/models")
def models():
    """Providers the UI can switch to, with whether each is ready (key set / server running)."""
    current = _state["llm"].name if _state["llm"] else None
    return {"current": current, "providers": provider_status()}


@app.post("/api/model")
def set_model(body: ModelIn):
    """Switch the language model at runtime. Conversations continue with the new model."""
    if body.provider not in PROVIDERS:
        raise HTTPException(400, f"Unknown provider. Choose from {list(PROVIDERS)}.")
    try:
        new = get_provider(body.provider, body.model.strip() or None)
    except LLMError as e:
        raise HTTPException(400, str(e))
    with _lock:
        _state["llm"] = new
    return {"llm": new.name}


@app.post("/api/session")
def new_session(body: NewSession):
    if body.patient not in db.PATIENTS:
        raise HTTPException(400, f"Patient must be one of {db.PATIENTS} (fictional aliases only).")
    s = Session(conn=_conn(), patient_alias=body.patient, prompt_variant=body.variant)
    _state["sessions"][s.id] = s
    return {"session_id": s.id, "patient": s.patient_alias, "today": s.today.isoformat()}


@app.post("/api/chat")
def chat(body: ChatIn):
    session = _session(body.session_id)
    if not body.message.strip():
        raise HTTPException(400, "Empty message.")
    with _lock:
        # approver returns None = "a human will decide in the UI", so the turn pauses.
        turn = run_turn(session, body.message.strip(), _llm(), _decider(), approver=lambda request: None, channel="ui")
    return _turn_payload(session, turn)


@app.post("/api/approve")
def approve(body: ApproveIn):
    session = _session(body.session_id)
    if not session.pending:
        raise HTTPException(409, "Nothing is waiting for approval.")
    with _lock:
        turn = resume_turn(session, body.approved, _llm(), decider_name=_decider().name, channel="ui-staff")
    return _turn_payload(session, turn)


@app.get("/api/schedule")
def schedule(start: str | None = None, days: int = 5, patient: str = "Test Patient A"):
    """Week grid: every slot with booked / mine / open, straight from the database."""
    conn = _conn()
    all_dates = [r["date"] for r in conn.query("SELECT DISTINCT date FROM slots WHERE date > ? ORDER BY date",
                                               (config.today().isoformat(),))]
    if not all_dates:
        return {"dates": [], "all_dates": [], "times": db.TIMES, "providers": [], "slots": []}
    first = start if start in all_dates else all_dates[0]
    dates = all_dates[all_dates.index(first):][:days]
    rows = conn.query(
        "SELECT s.id, s.date, s.start_time, p.name AS provider, a.patient_alias"
        " FROM slots s JOIN providers p ON p.id = s.provider_id LEFT JOIN appointments a ON a.slot_id = s.id"
        f" WHERE s.date IN ({','.join('?' * len(dates))}) ORDER BY s.date, s.start_time, s.provider_id", tuple(dates))
    slots = [{"slot_id": r["id"], "date": r["date"], "time": r["start_time"], "provider": r["provider"],
              "status": "open" if r["patient_alias"] is None else ("mine" if r["patient_alias"] == patient else "booked")}
             for r in rows]
    return {"dates": dates, "all_dates": all_dates, "times": db.TIMES,
            "providers": [p[1] for p in db.PROVIDERS], "slots": slots}


@app.get("/api/appointments")
def appointments(patient: str = "Test Patient A"):
    return tools.list_appointments(_conn(), patient)


@app.post("/api/reset")
def reset():
    """Re-seed the fictional schedule and forget all conversations."""
    with _lock:
        if _state["conn"] is not None:
            _state["conn"].close()
        _state["conn"] = db.init_db(config.DATABASE_URL, config.today(), reset=True)
        _state["sessions"].clear()
    return {"ok": True}
