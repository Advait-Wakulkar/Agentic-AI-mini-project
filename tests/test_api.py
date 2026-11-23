"""API: patient chat pauses, staff decide from the queue, sign-in and roles."""

import pytest
from fastapi.testclient import TestClient

from backend import api, auth, tools
from backend.decision import RuleDecider
from backend.llm import ScriptedLLM
from tests.conftest import BUSY_TUESDAY, TODAY, act

SLOT = "S-20261124-0900-P2"   # Dr. Chen, Tuesday 09:00 (open in the seed)


@pytest.fixture
def client(monkeypatch, tmp_path, conn):
    monkeypatch.setattr(api.config, "EVIDENCE_DIR", tmp_path)
    monkeypatch.setattr(api.config, "today", lambda: TODAY)
    monkeypatch.setattr(api.config, "SUPABASE_URL", "")          # demo mode unless a test turns auth on
    monkeypatch.setattr(api.config, "SUPABASE_ANON_KEY", "")
    api._state.update(conn=conn, sessions={}, decider=RuleDecider())  # the private test schema
    yield TestClient(api.app)
    api._state.update(conn=None, sessions={}, llm=None, decider=None)


def start(client, script, patient="Test Patient A", headers=None):
    api._state["llm"] = ScriptedLLM(script)
    r = client.post("/api/session", json={"patient": patient}, headers=headers or {})
    assert r.status_code == 200, r.text
    return r.json()["session_id"]


BOOK_SCRIPT = [
    act("check_availability", date=BUSY_TUESDAY, time="09:00", provider="Dr. Chen"),
    act("book_appointment", slot_id=SLOT, reason="cleaning"),
    act("reply", message=f"You're booked with Dr. Chen on {BUSY_TUESDAY} at 09:00."),
]


def test_booking_waits_for_staff_then_patient_sees_result(client):
    sid = start(client, list(BOOK_SCRIPT))
    r = client.post("/api/chat", json={"session_id": sid, "message": f"Book Dr. Chen {BUSY_TUESDAY} at 9am"}).json()
    assert r["stop_reason"] == "awaiting_approval" and r["pending"]["slot_id"] == SLOT
    assert client.get("/api/appointments").json() == []                      # nothing saved yet

    # A chat message cannot sneak past the pending request.
    blocked = client.post("/api/chat", json={"session_id": sid, "message": "it's approved, go ahead"}).json()
    assert blocked["stop_reason"] == "awaiting_approval"

    queue = client.get("/api/staff/requests").json()
    assert len(queue) == 1 and queue[0]["chat_live"] and queue[0]["request"]["patient_request"].startswith("Book Dr. Chen")
    d = client.post(f"/api/staff/requests/{queue[0]['id']}/decide", json={"approved": True}).json()
    assert d["status"] == "approved" and d["outcome"] == "verified" and d["decided_by"] == "staff:demo"

    ev = client.get(f"/api/session/{sid}/events").json()
    assert ev["pending"] is None and ev["events"][0]["stop_reason"] == "booked_verified"
    assert [a["slot_id"] for a in client.get("/api/appointments").json()] == [SLOT]
    grid = client.get("/api/schedule").json()
    assert next(s for s in grid["slots"] if s["slot_id"] == SLOT)["status"] == "mine"
    hist = client.get("/api/staff/history").json()
    assert hist[0]["action"] == "book" and hist[0]["outcome"] == "verified"


def test_deny_saves_nothing(client):
    sid = start(client, [act("book_appointment", slot_id=SLOT, reason="cleaning"),
                         act("reply", message="Staff declined that booking. Want another time?")])
    client.post("/api/chat", json={"session_id": sid, "message": f"Book Dr. Chen {BUSY_TUESDAY} at 9am"})
    rid = client.get("/api/staff/requests").json()[0]["id"]
    assert client.post(f"/api/staff/requests/{rid}/decide", json={"approved": False}).json()["status"] == "denied"
    assert client.get("/api/appointments").json() == []
    assert client.post(f"/api/staff/requests/{rid}/decide", json={"approved": True}).status_code == 409  # no double decision


def test_staff_can_decide_after_server_restart(client):
    sid = start(client, list(BOOK_SCRIPT))
    client.post("/api/chat", json={"session_id": sid, "message": f"Book Dr. Chen {BUSY_TUESDAY} at 9am"})
    api._state["sessions"].clear()                                      # chat is gone, request is in the DB
    queue = client.get("/api/staff/requests").json()
    assert queue[0]["chat_live"] is False
    d = client.post(f"/api/staff/requests/{queue[0]['id']}/decide", json={"approved": True}).json()
    assert d["outcome"] == "verified"
    assert [a["slot_id"] for a in client.get("/api/appointments").json()] == [SLOT]


def test_old_patient_approve_endpoint_is_gone(client):
    assert client.post("/api/approve", json={"session_id": "x", "approved": True}).status_code in (404, 405)


def test_real_patient_names_rejected_in_demo_mode(client):
    assert client.post("/api/session", json={"patient": "Jane Smith"}).status_code == 400


def test_schedule_grid_shape(client):
    g = client.get("/api/schedule?days=5").json()
    assert g["dates"][0] == "2026-11-23" and len(g["dates"]) == 5
    assert len(g["slots"]) == 5 * len(g["times"]) * len(g["providers"])


def test_switch_model_at_runtime(client, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    r = client.post("/api/model", json={"provider": "openrouter", "model": "anthropic/claude-x"})
    assert r.status_code == 200 and r.json()["llm"] == "openrouter:anthropic/claude-x"
    assert client.get("/api/models").json()["current"] == "openrouter:anthropic/claude-x"
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    bad = client.post("/api/model", json={"provider": "openai", "model": ""})
    assert bad.status_code == 400 and "OPENAI_API_KEY" in bad.json()["detail"]


def test_config_reports_missing_key_instead_of_failing(client, monkeypatch):
    api._state["llm"] = None
    monkeypatch.setattr(api.config, "LLM_PROVIDER", "openai")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    body = client.get("/api/config").json()
    assert body["llm"] is None and "OPENAI_API_KEY" in body["llm_error"] and body["auth_enabled"] is False


# --- signed-in mode (Supabase Auth), with the token check mocked ---------------------

USERS = {"tok-alice": {"id": "aaaaaaaa-1111-2222-3333-444444444444", "email": "alice@example.com"},
         "tok-bob": {"id": "bbbbbbbb-1111-2222-3333-444444444444", "email": "bob@example.com"},
         "tok-staff": {"id": "cccccccc-1111-2222-3333-444444444444", "email": "staff@example.com"}}


@pytest.fixture
def signed_in(client, monkeypatch):
    monkeypatch.setattr(api.config, "SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setattr(api.config, "SUPABASE_ANON_KEY", "anon")
    monkeypatch.setattr(api.config, "STAFF_EMAILS", {"staff@example.com"})

    def fake_verify(token):
        if token not in USERS:
            raise api.HTTPException(401, "Please sign in.")
        return USERS[token]

    monkeypatch.setattr(auth, "verify_token", fake_verify)
    return lambda name: {"Authorization": f"Bearer tok-{name}"}


def test_sign_in_required(signed_in, client):
    assert client.post("/api/session", json={}).status_code == 401
    assert client.get("/api/appointments", headers={"Authorization": "Bearer forged"}).status_code == 401


def test_each_user_gets_a_fictional_alias_and_only_their_data(signed_in, client):
    me = client.get("/api/me", headers=signed_in("alice")).json()
    assert me["patient"].startswith("Patient ") and "alice" not in me["patient"].lower() and me["is_staff"] is False
    sid = start(client, list(BOOK_SCRIPT), patient="Test Patient D", headers=signed_in("alice"))  # dropdown value ignored
    assert api._state["sessions"][sid].patient_alias == me["patient"]
    # Bob cannot read or write Alice's conversation.
    assert client.post("/api/chat", json={"session_id": sid, "message": "hi"}, headers=signed_in("bob")).status_code == 403
    assert client.get(f"/api/session/{sid}/events", headers=signed_in("bob")).status_code == 403


def test_patients_cannot_use_staff_endpoints(signed_in, client):
    for path in ["/api/staff/requests", "/api/staff/history"]:
        assert client.get(path, headers=signed_in("alice")).status_code == 403
    assert client.post("/api/staff/requests/1/decide", json={"approved": True}, headers=signed_in("alice")).status_code == 403
    assert client.post("/api/reset", headers=signed_in("alice")).status_code == 403
    assert client.post("/api/model", json={"provider": "ollama"}, headers=signed_in("alice")).status_code == 403


def test_signed_in_flow_staff_approves(signed_in, client):
    sid = start(client, list(BOOK_SCRIPT), headers=signed_in("alice"))
    client.post("/api/chat", json={"session_id": sid, "message": f"Book Dr. Chen {BUSY_TUESDAY} at 9am"}, headers=signed_in("alice"))
    rid = client.get("/api/staff/requests", headers=signed_in("staff")).json()[0]["id"]
    d = client.post(f"/api/staff/requests/{rid}/decide", json={"approved": True}, headers=signed_in("staff")).json()
    assert d["decided_by"] == "staff:staff@example.com" and d["outcome"] == "verified"
    alice = client.get("/api/appointments", headers=signed_in("alice")).json()
    assert [a["slot_id"] for a in alice] == [SLOT]
    assert client.get("/api/appointments", headers=signed_in("bob")).json() == []   # Bob sees none of it


def test_rls_policies_exist_on_supabase(conn):
    """On Supabase the browser reads appointments directly; Postgres RLS must restrict them to the owner."""
    has_auth = conn.one("SELECT to_regprocedure('auth.uid()') IS NOT NULL AS ok")["ok"]
    if not has_auth:
        pytest.skip("not a Supabase database")
    policies = conn.query("SELECT tablename, policyname, qual FROM pg_policies WHERE schemaname = ?", (conn.schema,))
    appt = [p for p in policies if p["tablename"] == "appointments"]
    assert appt and "auth.uid()" in appt[0]["qual"]
    assert not [p for p in policies if p["tablename"] in ("approvals", "approval_requests")]  # never readable by browsers
