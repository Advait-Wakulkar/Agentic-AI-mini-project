"""Sign-in with Supabase Auth.

Auth is ON when SUPABASE_URL and SUPABASE_ANON_KEY are set in .env. Then:
  - every patient request carries the user's Supabase access token,
  - the backend checks it with Supabase (GET /auth/v1/user) and links the user
    to a patient row with an auto-generated fictional alias (no real names),
  - staff are the emails listed in STAFF_EMAILS.

Auth is OFF (demo mode) without those settings: a patient dropdown is shown
and anyone can open the staff dashboard. Fine for local demos and tests.
"""

import hashlib
import time

import requests
from fastapi import HTTPException, Request

from backend import config

_cache: dict[str, tuple[float, dict]] = {}   # token hash -> (expires, user), so we don't call Supabase every request


def enabled() -> bool:
    return bool(config.SUPABASE_URL and config.SUPABASE_ANON_KEY)


def verify_token(token: str) -> dict:
    """Ask Supabase who this access token belongs to. Raises 401 if it is not valid."""
    key = hashlib.sha256(token.encode()).hexdigest()
    hit = _cache.get(key)
    if hit and hit[0] > time.time():
        return hit[1]
    try:
        r = requests.get(f"{config.SUPABASE_URL.rstrip('/')}/auth/v1/user",
                         headers={"apikey": config.SUPABASE_ANON_KEY, "Authorization": f"Bearer {token}"}, timeout=10)
    except requests.RequestException as e:
        raise HTTPException(503, f"Could not reach Supabase Auth: {e}") from None
    if r.status_code != 200:
        raise HTTPException(401, "Your session has expired. Please sign in again.")
    body = r.json()
    user = {"id": body["id"], "email": (body.get("email") or "").lower(),
            "email_confirmed_at": body.get("email_confirmed_at") or body.get("confirmed_at")}
    _cache[key] = (time.time() + 300, user)
    return user


def current_user(request: Request) -> dict | None:
    """FastAPI dependency: the signed-in user, or None in demo mode."""
    if not enabled():
        return None
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("bearer "):
        raise HTTPException(401, "Please sign in.")
    return verify_token(header[7:].strip())


def is_staff(user: dict | None) -> bool:
    if not enabled():
        return True  # demo mode: the local user plays both roles
    # (review: without email confirmation anyone could sign up with a listed staff address)
    return bool(user) and user["email"] in config.STAFF_EMAILS and bool(user.get("email_confirmed_at"))


def staff_name(user: dict | None) -> str:
    return f"staff:{user['email']}" if user else "staff:demo"


def ensure_patient(conn, user: dict) -> str:
    """The patient alias linked to this user, creating a fictional one on first sign-in."""
    row = conn.one("SELECT alias FROM patients WHERE user_id = ?", (user["id"],))
    if row:
        return row["alias"]
    uid = user["id"].replace("-", "").upper()
    for length in (6, 12, 32):  # fictional alias derived from the random user id; longer if it ever collides
        alias = f"Patient {uid[:length]}"
        conn.run("INSERT INTO patients (alias, user_id) VALUES (?, ?) ON CONFLICT (alias) DO NOTHING", (alias, user["id"]))
        row = conn.one("SELECT alias FROM patients WHERE user_id = ?", (user["id"],))
        if row:
            return row["alias"]
    raise HTTPException(500, "Could not create a patient profile.")
