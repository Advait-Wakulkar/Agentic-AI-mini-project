import json
import os
from datetime import date

import pytest

os.environ.setdefault("INSURANCE_CHECK_DELAY", "0")   # simulated payer delay off in tests (set before config loads)
os.environ["REMINDERS_ENABLED"] = "0"                  # no background reminder worker inside API tests
os.environ.pop("SMTP_HOST", None)                      # tests never send real email

from backend import config, db, tools  # noqa: E402

TODAY = date(2026, 11, 20)            # a Friday; first schedule day is Mon 2026-11-23
BUSY_TUESDAY = "2026-11-24"           # Dr. Rivera's whole morning is pre-booked here
PATIENT = "Test Patient A"

# Tests run in their OWN schema (test_<random>) inside your Supabase database and
# drop it afterwards, so the app's tables in 'public' are never touched.
TEST_URL = os.environ.get("TEST_DATABASE_URL") or config.DATABASE_URL


@pytest.fixture(scope="session")
def _seeded_db():
    if not TEST_URL:
        pytest.skip("DATABASE_URL is not set: database tests need Supabase/Postgres (see .env.example)")
    database = db.init_db(TEST_URL, TODAY, schema=db.private_schema("test"))
    yield database
    database.drop_schema()
    database.close()


@pytest.fixture
def conn(_seeded_db):
    """A freshly reset copy of the fictional schedule for each test."""
    db.reset_to_seed(_seeded_db)
    return _seeded_db


@pytest.fixture
def open_slot(conn):
    """Some open slot with Dr. Chen on the busy Tuesday."""
    o = tools.list_openings(conn, BUSY_TUESDAY, provider="Dr. Chen")["openings"][0]
    return o


def act(action, **argument):
    """Build the JSON text a model would send."""
    return json.dumps({"action": action, "argument": argument})


def count_appointments(conn, patient=PATIENT):
    return len(tools.list_appointments(conn, patient))
