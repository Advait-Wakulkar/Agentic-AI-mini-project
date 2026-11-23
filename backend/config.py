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
DECIDER = os.environ.get("DECIDER", "rules").lower()

# Harness limits.
MAX_STEPS = int(os.environ.get("MAX_STEPS", "8"))
MAX_INVALID = int(os.environ.get("MAX_INVALID", "3"))


def today() -> date:
    """The clinic's 'today'. CLINIC_TODAY=YYYY-MM-DD pins it for reproducible tests and evals."""
    pinned = os.environ.get("CLINIC_TODAY")
    return date.fromisoformat(pinned) if pinned else date.today()
