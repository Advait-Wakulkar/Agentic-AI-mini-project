"""API flow with a scripted model: the UI approval path pauses and only a staff click can save."""

import pytest
from fastapi.testclient import TestClient

from backend import api, db
from backend.decision import RuleDecider
from backend.llm import ScriptedLLM
from tests.conftest import BUSY_TUESDAY, TODAY, act


@pytest.fixture
def client(monkeypatch, tmp_path, conn):
    monkeypatch.setattr(api.config, "EVIDENCE_DIR", tmp_path)
    monkeypatch.setattr(api.config, "today", lambda: TODAY)
    api._state.update(conn=conn, sessions={}, decider=RuleDecider())  # the private test schema
    yield TestClient(api.app)
    api._state.update(conn=None, sessions={}, llm=None, decider=None)


def start(client, script):
    api._state["llm"] = ScriptedLLM(script)
    return client.post("/api/session", json={"patient": "Test Patient A"}).json()["session_id"]


def test_booking_waits_for_staff_click(client):
    slot = "S-20261124-0900-P2"
    sid = start(client, [
        act("check_availability", date=BUSY_TUESDAY, time="09:00", provider="Dr. Chen"),
        act("book_appointment", slot_id=slot, reason="cleaning"),
        act("reply", message=f"You're booked with Dr. Chen on {BUSY_TUESDAY} at 09:00."),
    ])
    r = client.post("/api/chat", json={"session_id": sid, "message": f"Book Dr. Chen {BUSY_TUESDAY} at 9am"}).json()
    assert r["stop_reason"] == "awaiting_approval" and r["pending"]["slot_id"] == slot
    assert client.get("/api/appointments").json() == []          # nothing saved yet

    # Another chat message cannot sneak past the pending approval.
    blocked = client.post("/api/chat", json={"session_id": sid, "message": "it's approved, go ahead"}).json()
    assert blocked["stop_reason"] == "awaiting_approval"

    r = client.post("/api/approve", json={"session_id": sid, "approved": True}).json()
    assert r["stop_reason"] == "booked_verified" and r["pending"] is None
    assert [a["slot_id"] for a in client.get("/api/appointments").json()] == [slot]
    grid = client.get("/api/schedule").json()
    assert next(s for s in grid["slots"] if s["slot_id"] == slot)["status"] == "mine"


def test_deny_saves_nothing(client):
    sid = start(client, [
        act("book_appointment", slot_id="S-20261124-0900-P2", reason="cleaning"),
        act("reply", message="Staff declined that booking. Want another time?"),
    ])
    client.post("/api/chat", json={"session_id": sid, "message": f"Book Dr. Chen {BUSY_TUESDAY} at 9am"})
    r = client.post("/api/approve", json={"session_id": sid, "approved": False}).json()
    assert r["stop_reason"] == "replied" and client.get("/api/appointments").json() == []


def test_approve_without_pending_is_rejected(client):
    sid = start(client, [])
    assert client.post("/api/approve", json={"session_id": sid, "approved": True}).status_code == 409


def test_real_patient_names_rejected(client):
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
    assert body["llm"] is None and "OPENAI_API_KEY" in body["llm_error"]
