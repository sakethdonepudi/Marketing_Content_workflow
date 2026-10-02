"""Architecture 13 — dashboard authentication.

One administrator account. The password is stored only as a salted scrypt hash
(`DASHBOARD_PASSWORD_HASH`), never in source, DB, logs, or API responses. Sessions are
server-side, referenced by an opaque HttpOnly/Secure/SameSite cookie. Failed logins are
rate-limited per client without revealing whether the username or password was wrong.
"""

from datetime import datetime, timezone
import hashlib
import hmac
import os
import secrets

SESSION_COOKIE = "reachout_session"
SESSION_TTL_SECONDS = 12 * 3600          # 12 hours
MAX_FAILED_ATTEMPTS = 5
COOLDOWN_SECONDS = 300                    # 5 minutes after 5 failures


def _now():
    return datetime.now(timezone.utc).isoformat()


def _truthy(name, default="0"):
    return os.environ.get(name, default).strip().lower() in ("1", "true", "yes", "on")


def login_enabled():
    return bool(os.environ.get("DASHBOARD_USERNAME") and os.environ.get("DASHBOARD_PASSWORD_HASH"))


def _region():
    # Only relevant when a persisted region is provided; tests pass it explicitly.
    return None


def hash_password(password, *, salt=None, iterations=16384):
    """scrypt hash in `scrypt$N$salt$hash` form (no external dependency, no plaintext)."""
    salt = salt or secrets.token_hex(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt.encode("utf-8"),
                            n=iterations, r=8, p=1, dklen=32)
    return f"scrypt${iterations}${salt}${digest.hex()}"


def verify_password(password, stored):
    """Constant-time verification against the configured hash. Never logs the password."""
    if not stored or not password:
        return False
    try:
        scheme, iterations, salt, digest = stored.split("$", 3)
        if scheme != "scrypt":
            return False
        candidate = hashlib.scrypt(password.encode("utf-8"), salt=salt.encode("utf-8"),
                                   n=int(iterations), r=8, p=1, dklen=32)
        return hmac.compare_digest(candidate.hex(), digest)
    except (ValueError, TypeError):
        return False


# ---------- sessions (server-side, opaque cookie) ----------

def create_session(*, connect, username, now=None, ttl_seconds=SESSION_TTL_SECONDS):
    reference = datetime.fromisoformat(str(now() if now else _now()).replace("Z", "+00:00"))
    token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    created = reference.isoformat()
    expires = datetime.fromtimestamp(reference.timestamp() + ttl_seconds, timezone.utc).isoformat()
    with connect() as connection:
        connection.execute(
            "INSERT INTO dashboard_sessions(token_hash,username,created_at,expires_at,revoked_at) "
            "VALUES(?,?,?,?,NULL)", (token_hash, username, created, expires))
    return {"token": token, "expires_at": expires, "username": username}


def session_user(token, *, connect, now=None):
    """Return the username for a valid, unexpired, unrevoked session, else None."""
    if not token:
        return None
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    reference = datetime.fromisoformat(str(now() if now else _now()).replace("Z", "+00:00")).isoformat()
    with connect() as connection:
        row = connection.execute(
            "SELECT username FROM dashboard_sessions WHERE token_hash=? AND revoked_at IS NULL "
            "AND expires_at > ?", (token_hash, reference)).fetchone()
    return row["username"] if row else None


def revoke_session(token, *, connect, now=None):
    if not token:
        return False
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    with connect() as connection:
        cursor = connection.execute(
            "UPDATE dashboard_sessions SET revoked_at=? WHERE token_hash=? AND revoked_at IS NULL",
            (_now(), token_hash))
    return cursor.rowcount == 1


def cleanup_sessions(*, connect, older_than_hours=48, now=None):
    reference = datetime.fromisoformat(str(now() if now else _now()).replace("Z", "+00:00"))
    cutoff = datetime.fromtimestamp(reference.timestamp() - older_than_hours * 3600, timezone.utc).isoformat()
    with connect() as connection:
        return connection.execute(
            "DELETE FROM dashboard_sessions WHERE created_at < ? AND "
            "(revoked_at IS NOT NULL OR expires_at < ?)", (cutoff, reference.isoformat())).rowcount


# ---------- login attempts (rate limiting) ----------

def login_cooldown(*, connect, client_key, now=None):
    """Seconds remaining in a cooldown, or 0 when the client may attempt again."""
    reference = datetime.fromisoformat(str(now() if now else _now()).replace("Z", "+00:00"))
    with connect() as connection:
        row = connection.execute(
            "SELECT failed_count,last_failed_at,locked_until FROM dashboard_login_attempts WHERE client_key=?",
            (client_key,)).fetchone()
    if not row or not row["locked_until"]:
        return 0
    try:
        locked = datetime.fromisoformat(str(row["locked_until"]).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return 0
    remaining = (locked - reference).total_seconds()
    return int(max(0, remaining))


def record_failed_login(*, connect, client_key, now=None):
    reference = datetime.fromisoformat(str(now() if now else _now()).replace("Z", "+00:00"))
    with connect() as connection:
        row = connection.execute(
            "SELECT failed_count FROM dashboard_login_attempts WHERE client_key=?", (client_key,)).fetchone()
        count = (row["failed_count"] if row else 0) + 1
        locked_until = None
        if count >= MAX_FAILED_ATTEMPTS:
            locked_until = datetime.fromtimestamp(
                reference.timestamp() + COOLDOWN_SECONDS, timezone.utc).isoformat()
            count = 0  # reset after locking
        if row:
            connection.execute(
                "UPDATE dashboard_login_attempts SET failed_count=?,last_failed_at=?,locked_until=? "
                "WHERE client_key=?", (count, reference.isoformat(), locked_until, client_key))
        else:
            connection.execute(
                "INSERT INTO dashboard_login_attempts(client_key,failed_count,last_failed_at,locked_until) "
                "VALUES(?,?,?,?)", (client_key, count, reference.isoformat(), locked_until))
    return {"failed_count": count, "locked": locked_until is not None, "locked_until": locked_until}


def clear_login_attempts(*, connect, client_key):
    with connect() as connection:
        connection.execute("DELETE FROM dashboard_login_attempts WHERE client_key=?", (client_key,))


def authenticate(username, password, *, connect, client_key, now=None):
    """Full login: rate limit -> credentials -> session. Generic failure message (no oracle)."""
    remaining = login_cooldown(connect=connect, client_key=client_key, now=now)
    if remaining > 0:
        return {"ok": False, "reason": "RATE_LIMITED", "retry_after": remaining,
                "message": "Too many attempts. Try again shortly."}
    expected_user = os.environ.get("DASHBOARD_USERNAME", "")
    expected_hash = os.environ.get("DASHBOARD_PASSWORD_HASH", "")
    user_ok = hmac.compare_digest(str(username or ""), expected_user)
    pass_ok = verify_password(password, expected_hash)
    if not (user_ok and pass_ok):
        record_failed_login(connect=connect, client_key=client_key, now=now)
        return {"ok": False, "reason": "INVALID", "message": "Invalid username or password."}
    clear_login_attempts(connect=connect, client_key=client_key)
    session = create_session(connect=connect, username=expected_user, now=now)
    return {"ok": True, "session": session}
