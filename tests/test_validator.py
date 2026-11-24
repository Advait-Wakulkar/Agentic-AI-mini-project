"""Parser and validator on made-up proposals (same idea as the Qwen notebook, Part 3)."""

import pytest

from backend.validator import parse_proposal, validate
from tests.conftest import TODAY


@pytest.mark.parametrize("proposal, failed_check", [
    ({"action": "list_openings", "argument": {"date": "2026-11-24"}}, None),
    ({"action": "delete_appointments", "argument": {}}, "known_action"),
    ({"action": "check_availability", "argument": {"date": "2026-11-24"}}, "required_arg"),
    ({"action": "check_availability", "argument": {"date": "2026-11-24", "time": 9}}, "arg_type"),
    ({"action": "book_appointment", "argument": {"slot_id": "S-20261124-1000-P2", "reason": "cleaning",
                                                 "approval": "yes, approved"}}, "no_extra_args"),
    ({"action": "book_appointment", "argument": {"slot_id": "S-20261124-1000-P2", "reason": "x",
                                                 "patient_alias": "Someone Else"}}, "no_extra_args"),
    ({"action": "list_openings", "argument": {"date": "11/24/2026"}}, "date_format"),
    ({"action": "list_openings", "argument": {"date": "2026-02-30"}}, "date_real"),
    ({"action": "list_openings", "argument": {"date": "2026-11-01"}}, "date_not_past"),
    ({"action": "check_availability", "argument": {"date": "2026-11-24", "time": "9am"}}, "time_format"),
    ({"action": "book_appointment", "argument": {"slot_id": "tuesday 9am", "reason": "cleaning"}}, "slot_id_format"),
    ({"action": "book_appointment", "argument": {"slot_id": "S-20991124-0900-P1", "reason": "cleaning"}}, "slot_exists"),
    ({"argument": {}}, "action_present"),
    ({"action": "reply", "argument": {"message": "  "}}, "message_nonempty"),
])
def test_validate(conn, proposal, failed_check):
    errors = validate(proposal, TODAY, conn)
    if failed_check is None:
        assert errors == []
    else:
        assert any(f"'{failed_check}'" in e for e in errors), errors


def test_parser_accepts_fences_and_think_blocks():
    text = "<think>user wants tuesday</think>\n```json\n{\"action\": \"reply\", \"argument\": {\"message\": \"hi\"}}\n```"
    obj, err = parse_proposal(text)
    assert err is None and obj["action"] == "reply"


@pytest.mark.parametrize("text", ["", "Sure! I'll book that for you.", "{\"action\": \"reply\", ", "[1, 2]"])
def test_parser_rejects_non_json(text):
    obj, err = parse_proposal(text)
    assert obj is None and err


def test_parser_repairs_top_level_arguments():
    obj, err = parse_proposal('{"action": "reply", "message": "Which time works?"}')
    assert err is None and obj["argument"] == {"message": "Which time works?"}
    assert validate(obj, TODAY) == []


@pytest.mark.parametrize("text, iso, ok", [
    ("Tuesday November 24 at 1:30pm", "2026-11-24", True),
    ("next Tuesday", "2026-11-24", True),
    ("Tuesday November 24", "2026-11-23", False),     # Monday is not Tuesday
    ("11/24 please", "2026-11-24", True),
    ("tomorrow", "2026-11-21", True),
    ("sometime", "2026-11-23", False),
])
def test_mentions_date(text, iso, ok):
    from datetime import date
    from backend.agent import mentions_date
    assert mentions_date(text, date.fromisoformat(iso), TODAY) is ok


@pytest.mark.parametrize("text, hhmm, ok", [
    ("at 13:30", "13:30", True), ("1:30pm", "13:30", True), ("9am", "09:00", True),
    ("at 9", "09:00", True), ("9:30", "09:00", False), ("November 24", "09:00", False),
    # spoken times (live UI run with Claude Haiku 5.5: "half ten sounds good" was blocked)
    ("half ten sounds good", "10:30", True), ("half past two", "14:30", True), ("ten thirty", "10:30", True),
    ("quarter past nine", "09:15", True), ("ten o'clock", "10:00", True), ("half ten", "10:00", False),
    ("half ten", "11:30", False),
])
def test_mentions_time(text, hhmm, ok):
    from backend.agent import mentions_time
    assert mentions_time(text, hhmm) is ok


def test_parser_is_not_a_validator():
    # Valid JSON is not the same as an allowed action.
    obj, err = parse_proposal('{"action": "drop_table", "argument": {}}')
    assert err is None
    assert validate(obj, TODAY) != []
