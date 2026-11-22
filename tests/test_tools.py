"""Tools behave correctly when Python calls them directly (no model involved)."""

from backend import tools
from tests.conftest import BUSY_TUESDAY, PATIENT, count_appointments


def test_openings_exclude_booked_slots(conn):
    result = tools.list_openings(conn, BUSY_TUESDAY, provider="Rivera", part_of_day="morning")
    assert result["openings"] == []  # whole morning pre-booked


def test_check_availability_unavailable_and_available(conn, open_slot):
    busy = tools.check_availability(conn, BUSY_TUESDAY, "09:00", provider="Dr. Rivera")
    assert busy["status"] == "unavailable"
    free = tools.check_availability(conn, BUSY_TUESDAY, open_slot["time"], provider="Dr. Chen")
    assert free["status"] == "available"
    assert free["slots"][0]["slot_id"] == open_slot["slot_id"]


def test_unavailable_includes_grounded_alternatives(conn):
    r = tools.check_availability(conn, BUSY_TUESDAY, "09:00", provider="Dr. Rivera")
    assert r["status"] == "unavailable" and r["alternatives"]
    for alt in r["alternatives"]:
        assert alt["provider"] == "Dr. Rivera"                              # same provider first
        assert tools.slot_details(conn, alt["slot_id"])["booked"] is False   # really open


def test_find_next_openings_skips_today_and_weekend(conn):
    r = tools.find_next_openings(conn, "2026-11-20")
    assert r["openings"] and all(o["date"] > "2026-11-20" for o in r["openings"])
    assert r["openings"][0]["date"] == "2026-11-23"  # Monday


def test_weekend_and_unknown_provider(conn):
    assert tools.check_availability(conn, "2026-11-28", "10:00")["status"] == "not_offered"  # Saturday
    assert tools.list_openings(conn, BUSY_TUESDAY, provider="Dr. Nobody")["ok"] is False


def test_unknown_office_topic_is_reported_not_guessed(conn):
    r = tools.get_office_info(conn, "parking")
    assert r["ok"] is False and "No information" in r["error"]


def test_prepare_booking_refuses_booked_slot(conn):
    r = tools.prepare_booking(conn, "S-20261124-0900-P1", PATIENT, "cleaning")
    assert r["ok"] is False and "already booked" in r["error"]


def test_commit_requires_human_approval(conn, open_slot):
    req = tools.prepare_booking(conn, open_slot["slot_id"], PATIENT, "cleaning")["approval_request"]
    assert tools.commit_booking(conn, req, approval_id=9999)["ok"] is False        # no such approval
    denied = tools.record_decision(conn, req, approved=False, decided_by="test")
    assert tools.commit_booking(conn, req, denied)["ok"] is False                   # denied approval
    assert count_appointments(conn) == 0


def test_approval_must_match_slot(conn, open_slot):
    req = tools.prepare_booking(conn, open_slot["slot_id"], PATIENT, "cleaning")["approval_request"]
    other = dict(req, slot_id="S-20261124-1600-P3")
    approval = tools.record_decision(conn, other, approved=True, decided_by="test")
    assert tools.commit_booking(conn, req, approval)["ok"] is False


def test_approved_booking_is_saved_and_verified(conn, open_slot):
    req = tools.prepare_booking(conn, open_slot["slot_id"], PATIENT, "cleaning")["approval_request"]
    approval = tools.record_decision(conn, req, approved=True, decided_by="test")
    assert tools.commit_booking(conn, req, approval)["ok"] is True
    assert tools.verify_booking(conn, open_slot["slot_id"], PATIENT) is True


def test_double_booking_blocked_by_database(conn, open_slot):
    req_a = tools.prepare_booking(conn, open_slot["slot_id"], PATIENT, "cleaning")["approval_request"]
    req_b = dict(req_a, patient_alias="Test Patient B")
    # Both approved before either is saved (a race).
    a = tools.record_decision(conn, req_a, True, "test")
    b = tools.record_decision(conn, req_b, True, "test")
    assert tools.commit_booking(conn, req_a, a)["ok"] is True
    second = tools.commit_booking(conn, req_b, b)
    assert second["ok"] is False and "taken" in second["error"]
