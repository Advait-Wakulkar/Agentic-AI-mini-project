"""Central settings, read from environment variables (and a .env file if present).

Everything that changes between machines lives here, so the rest of the code
never reads os.environ directly.
"""

import os
from datetime import date
from pathlib import Path

try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except ImportError:  # python-dotenv is optional
    pass

PROJECT_ROOT = Path(__file__).resolve().parent.parent
# Supabase (or any Postgres) connection string. Required.
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()

# Supabase Auth (optional). With both set, patients sign in; without them the UI runs in demo mode.
SUPABASE_URL = os.environ.get("SUPABASE_URL", "").strip()
SUPABASE_ANON_KEY = os.environ.get("SUPABASE_ANON_KEY", "").strip()
# Who may use the staff dashboard (comma-separated emails).
STAFF_EMAILS = {e.strip().lower() for e in os.environ.get("STAFF_EMAILS", "").split(",") if e.strip()}
EVIDENCE_DIR = Path(os.environ.get("EVIDENCE_DIR", PROJECT_ROOT / "evidence"))

# Which language model proposes actions: ollama (local), groq or claude (hosted).
LLM_PROVIDER = os.environ.get("LLM_PROVIDER", "ollama").lower()
LLM_MODEL = os.environ.get("LLM_MODEL", "")  # empty = provider default
OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434")

# Which decision model answers fixed-choice questions: rules (default) or jev.
DECIDER = os.environ.get("DECIDER", "llm").lower()               # llm | rules | jev
# Model for the natural-language router (DECIDER=llm). Empty = same provider as the agents.
DECIDER_PROVIDER = os.environ.get("DECIDER_PROVIDER", "").lower()  # e.g. openrouter
DECIDER_MODEL = os.environ.get("DECIDER_MODEL", "")               # e.g. typesafe/jev-router

# Harness limits.
MAX_STEPS = int(os.environ.get("MAX_STEPS", "8"))
MAX_INVALID = int(os.environ.get("MAX_INVALID", "3"))
# Simulated insurance eligibility check: seconds of fake "payer round trip" (0 in tests).
INSURANCE_CHECK_DELAY = float(os.environ.get("INSURANCE_CHECK_DELAY", "1.5"))

# Reminder agent (runs in the background inside the API). Email is a simulated outbox unless SMTP_HOST is set
# (e.g. a local Mailpit on port 1025); SMS is always simulated. Calendar invites (.ics) are real files.
REMINDERS_ENABLED = os.environ.get("REMINDERS_ENABLED", "1").lower() not in ("0", "false", "no", "")
REMINDER_INTERVAL_SECONDS = float(os.environ.get("REMINDER_INTERVAL_SECONDS", "60"))
REMINDER_WINDOW_HOURS = float(os.environ.get("REMINDER_WINDOW_HOURS", "48"))
SMTP_HOST = os.environ.get("SMTP_HOST", "")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "1025"))
MAIL_FROM = os.environ.get("MAIL_FROM", "Bright Smile Dental <frontdesk@brightsmile.example>")
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")          # e.g. a Gmail app password; never printed or logged
SMTP_STARTTLS = os.environ.get("SMTP_STARTTLS", "").lower() in ("1", "true", "yes") or SMTP_PORT == 587
# Real contact details allowed past the fictional-data check (your own, for a live test). Comma-separated emails or
# phone numbers. Real email is only ever SENT to addresses on this list (or to a local mail catcher like Mailpit).
REAL_CONTACT_ALLOWLIST = {c.strip().lower() for c in os.environ.get("REAL_CONTACT_ALLOWLIST", "").split(",") if c.strip()}


def today() -> date:
    """The clinic's 'today'. CLINIC_TODAY=YYYY-MM-DD pins it for reproducible tests and evals."""
    pinned = os.environ.get("CLINIC_TODAY")
    return date.fromisoformat(pinned) if pinned else date.today()
