"""Database layer on Postgres/Supabase (runs in a private test schema)."""

import pytest

from backend import db
from tests.conftest import BUSY_TUESDAY


def test_seed_is_deterministic(conn):
    n_slots = conn.one("SELECT COUNT(*) AS n FROM slots")["n"]
    assert n_slots == 10 * len(db.PROVIDERS) * len(db.TIMES)
    rivera_tue_morning = conn.query(
        "SELECT a.slot_id FROM appointments a JOIN slots s ON s.id = a.slot_id"
        " WHERE s.date = ? AND s.provider_id = 'P1' AND s.start_time < '12:00'", (BUSY_TUESDAY,))
    assert len(rivera_tue_morning) == 6


def test_insert_returns_new_id_after_seeded_ids(conn):
    max_seed = conn.one("SELECT MAX(id) AS m FROM approvals")["m"]
    new_id = conn.insert(
        "INSERT INTO approvals (slot_id, patient_alias, reason, decision, decided_by, decided_at) VALUES (?,?,?,?,?,?)",
        ("S-20261124-1600-P3", "Test Patient A", "x", "denied", "test", "2026-11-20T00:00:00"))
    assert new_id == max_seed + 1


def test_unique_slot_raises_integrity_error(conn):
    taken = conn.one("SELECT slot_id, approval_id FROM appointments LIMIT 1")
    with pytest.raises(db.IntegrityError):
        conn.run("INSERT INTO appointments (slot_id, patient_alias, reason, approval_id, created_at) VALUES (?,?,?,?,?)",
                 (taken["slot_id"], "Test Patient A", "x", taken["approval_id"], "now"))


def test_decision_must_be_approved_or_denied(conn):
    with pytest.raises(db.IntegrityError):
        conn.run("INSERT INTO approvals (slot_id, patient_alias, reason, decision, decided_by, decided_at) VALUES (?,?,?,?,?,?)",
                 ("S-x", "Test Patient A", "x", "auto-approved-by-model", "model", "now"))


def test_transaction_rolls_back_on_error(conn):
    before = conn.one("SELECT COUNT(*) AS n FROM patients")["n"]
    with pytest.raises(RuntimeError):
        with conn.transaction():
            conn.run("INSERT INTO patients VALUES (?)", ("Test Patient Z",))
            raise RuntimeError("boom")
    assert conn.one("SELECT COUNT(*) AS n FROM patients")["n"] == before


def test_placeholders_converted():
    assert db.Database._pg("a = ? AND b = ?") == "a = %s AND b = %s"


def test_missing_url_is_a_clear_error():
    with pytest.raises(db.DatabaseError, match="DATABASE_URL"):
        db.Database("")


def test_bad_schema_name_rejected():
    with pytest.raises(db.DatabaseError, match="schema"):
        db.Database("postgresql://x@y/z", schema='public"; DROP TABLE x; --')


def test_tests_run_in_private_schema(conn):
    assert conn.schema.startswith("test_")
    with pytest.raises(db.DatabaseError):
        db.Database.drop_schema(type("Fake", (), {"schema": "public"})())  # never drops the app schema


def test_reset_to_seed_removes_test_changes(conn, open_slot):
    from backend import tools
    req = tools.prepare_booking(conn, open_slot["slot_id"], "Test Patient A", "x")["approval_request"]
    tools.commit_booking(conn, req, tools.record_decision(conn, req, True, "test"))
    db.reset_to_seed(conn)
    assert tools.list_appointments(conn, "Test Patient A") == []


def test_password_with_special_characters_is_encoded():
    url = "postgresql://postgres.ref:p@ss#w/rd?@db.example.supabase.co:5432/postgres"
    out = db.normalize_url(url)
    assert out == "postgresql://postgres.ref:p%40ss%23w%2Frd%3F@db.example.supabase.co:5432/postgres"
    assert db.normalize_url(out) == out                     # already-encoded URLs are left alone


def test_error_messages_never_contain_credentials():
    url = "postgresql://postgres.ref:hunter2@secret@db.example.supabase.co:5432/postgres"
    msg = "failed to resolve host 'secret@db.example.supabase.co' for user postgres.ref password hunter2"
    scrubbed = db._scrub(msg, url)
    assert "hunter2" not in scrubbed and "secret" not in scrubbed


def test_literal_percent_in_sql_without_params(conn):
    assert conn.one("SELECT COUNT(*) AS n FROM slots WHERE id LIKE 'S-2026%'")["n"] > 0
