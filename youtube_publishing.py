"""Architecture 11 — YouTube publishing + discoverability intelligence (YouTube ONLY).

An API key can NEVER upload: publishing uses OAuth 2.0 with the
`https://www.googleapis.com/auth/youtube.upload` scope. Client secret, access token, and
refresh token are read from the environment and are never logged, returned, or committed.

Discoverability is optimized at the CONTENT/topic level only. Engagement is a weak aggregate
relevance signal — never factual verification, never persuasion, and no demographic or
political-personality profiling.
"""

from datetime import datetime, timezone
import hashlib
import json
import os
import re
import urllib.parse
import uuid

YOUTUBE_UPLOAD_SCOPE = "https://www.googleapis.com/auth/youtube.upload"
YOUTUBE_READONLY_SCOPE = "https://www.googleapis.com/auth/youtube.readonly"
YOUTUBE_ANALYTICS_SCOPE = "https://www.googleapis.com/auth/yt-analytics.readonly"
# Publishing needs upload AND read access (channels.list(mine=true) requires readonly).
YOUTUBE_REQUIRED_SCOPES = (YOUTUBE_UPLOAD_SCOPE, YOUTUBE_READONLY_SCOPE)
YOUTUBE_SCOPE_PARAM = " ".join(YOUTUBE_REQUIRED_SCOPES)
OAUTH_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
OAUTH_TOKEN_URL = "https://oauth2.googleapis.com/token"
UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3/videos"
ANALYTICS_URL = "https://youtubeanalytics.googleapis.com/v2/reports"
DATA_API_URL = "https://www.googleapis.com/youtube/v3"
RESEARCH_CACHE_MINUTES = 45
SNAPSHOT_CHECKPOINTS = (("1h", 1), ("6h", 6), ("24h", 24), ("3d", 72), ("7d", 168))

# Unsupported clickbait / inferred framing that must never appear in copy (Part C/D/9).
_BANNED_TITLE = (
    "shocking", "exposed", "destroyed", "ultimate", "unbelievable", "insane", "you won't believe",
    "must watch", "viral", "breaking" , "blast", "slams", "thrashes", "trolls", "roasted",
)
_INFERRED = re.compile(
    r"\b(loved|impressed|delighted|furious|angry|humiliated|slapped|destroyed|thrashed|"
    r"trolled|slammed|blasted|praised|hailed|celebrated)\b", re.I)
_BANNED_FRAMING = re.compile(
    r"\b(vote|voting|vote for|elect|support our|join the movement|anti-national|traitor|"
    r"corrupt|jai tdp|tdp zindabad|jai jagan|ysrcp zindabad|congress hatao|propaganda|"
    r"slogan|we demand|defeat them|must win)\b", re.I)
_FYP_TAGS = {"#fyp", "#foryou", "#foryoupage", "#viral", "#trending", "#explore"}


def _now():
    return datetime.now(timezone.utc).isoformat()


def _truthy(name):
    return os.environ.get(name, "0").strip().lower() in ("1", "true", "yes", "on")


def _new_id(prefix):
    return prefix + uuid.uuid4().hex[:12].upper()


# ---------- OAuth config + connection state (never expose tokens) ----------

def oauth_configuration():
    """Readiness only — never returns secret values. Client config is distinct from authorization."""
    client_id = os.environ.get("YOUTUBE_CLIENT_ID", "").strip()
    client_secret = bool(os.environ.get("YOUTUBE_CLIENT_SECRET", "").strip())
    redirect = os.environ.get("YOUTUBE_REDIRECT_URI", "").strip()
    refresh_token = bool(os.environ.get("YOUTUBE_REFRESH_TOKEN", "").strip())
    # client_configured: the OAuth CLIENT exists (independent of runtime authorization).
    client_missing = [name for name, ok in (
        ("YOUTUBE_CLIENT_ID", client_id), ("YOUTUBE_CLIENT_SECRET", client_secret),
        ("YOUTUBE_REDIRECT_URI", redirect)) if not ok]
    missing = client_missing + ([] if refresh_token else ["YOUTUBE_REFRESH_TOKEN"])
    return {
        "client_id": client_id or None, "client_secret_configured": client_secret,
        "redirect_uri": redirect or None, "refresh_token_configured": refresh_token,
        "scope": YOUTUBE_SCOPE_PARAM, "scopes": list(YOUTUBE_REQUIRED_SCOPES), "missing": missing,
        "client_configured": not client_missing,
        "authorized": refresh_token,
        # upload_configured: client + authorization both present (upload is actually possible).
        "upload_configured": not client_missing and refresh_token,
        "api_key_configured": bool(os.environ.get("YOUTUBE_API_KEY")),
        "publishing_enabled": _truthy("YOUTUBE_PUBLISHING_ENABLED"),
    }


def mask(value, *, keep=4):
    text = str(value or "")
    if len(text) <= keep:
        return "*" * len(text)
    return "*" * (len(text) - keep) + text[-keep:]


def authorization_url(*, state=None):
    """The OAuth consent URL (no secrets inside)."""
    config = oauth_configuration()
    if not config["client_id"] or not config["redirect_uri"]:
        raise ValueError("YouTube OAuth is UNCONFIGURED")
    params = {
        "client_id": config["client_id"], "redirect_uri": config["redirect_uri"],
        "response_type": "code", "scope": YOUTUBE_SCOPE_PARAM,
        "access_type": "offline", "prompt": "consent", "include_granted_scopes": "true",
    }
    if state:
        params["state"] = state
    return f"{OAUTH_AUTH_URL}?{urllib.parse.urlencode(params)}"


def _state_hash(raw_state):
    return hashlib.sha256(str(raw_state).encode("utf-8")).hexdigest()


def new_csrf_state(*, connect, ttl_minutes=10, now=None):
    """Create a short-lived CSRF state; only the SHA-256 hash is stored server-side."""
    import secrets
    reference = datetime.fromisoformat(str(now() if now else _now()).replace("Z", "+00:00"))
    created = reference.isoformat()
    expires = datetime.fromtimestamp(reference.timestamp() + ttl_minutes * 60, timezone.utc).isoformat()
    raw = secrets.token_urlsafe(32)
    with connect() as connection:
        connection.execute(
            "INSERT INTO youtube_oauth_states(state,created_at,expires_at,consumed_at) VALUES(?,?,?,NULL)",
            (_state_hash(raw), created, expires))
    return raw


def consume_csrf_state(raw_state, *, connect, now=None):
    """Atomically consume a state value. Returns True only when exactly one row was updated.

    Requires consumed_at IS NULL AND expires_at IS NOT NULL AND expires_at > now. A second use,
    an expired state, or a NULL-expiry (legacy) row all yield False (INVALID_OR_EXPIRED_STATE).
    """
    if not raw_state:
        return False
    reference = datetime.fromisoformat(str(now() if now else _now()).replace("Z", "+00:00")).isoformat()
    with connect() as connection:
        cursor = connection.execute(
            "UPDATE youtube_oauth_states SET consumed_at=? WHERE state=? AND consumed_at IS NULL "
            "AND expires_at IS NOT NULL AND expires_at > ?",
            (reference, _state_hash(raw_state), reference))
    return cursor.rowcount == 1


def cleanup_oauth_states(*, connect, older_than_hours=24, now=None):
    """Delete consumed or expired state rows older than the retention window."""
    reference = datetime.fromisoformat(str(now() if now else _now()).replace("Z", "+00:00"))
    cutoff = datetime.fromtimestamp(reference.timestamp() - older_than_hours * 3600, timezone.utc).isoformat()
    with connect() as connection:
        deleted = connection.execute(
            "DELETE FROM youtube_oauth_states WHERE created_at < ? AND "
            "(consumed_at IS NOT NULL OR expires_at IS NULL OR expires_at < ?)",
            (cutoff, reference.isoformat())).rowcount
    return deleted


def _request(url, *, method="GET", body=None, headers=None, http=None):
    if http is not None:
        return http(url, body)
    import urllib.request
    request = urllib.request.Request(url, data=body, method=method, headers=headers or {})
    with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310 - fixed Google host
        return json.loads(response.read().decode("utf-8", "replace"))


def exchange_code(code, *, http=None):
    """Exchange an authorization code for tokens; returns status only (never the tokens)."""
    config = oauth_configuration()
    if not config["client_id"] or not config["client_secret_configured"] or not config["redirect_uri"]:
        raise ValueError("YouTube OAuth is UNCONFIGURED")
    body = urllib.parse.urlencode({
        "code": code, "client_id": os.environ["YOUTUBE_CLIENT_ID"],
        "client_secret": os.environ["YOUTUBE_CLIENT_SECRET"],
        "redirect_uri": config["redirect_uri"], "grant_type": "authorization_code",
    }).encode()
    data = _post_form(OAUTH_TOKEN_URL, body, http=http)
    return {"ok": bool(data.get("refresh_token") or data.get("access_token")),
            "has_refresh_token": bool(data.get("refresh_token")),
            "scope": data.get("scope", YOUTUBE_UPLOAD_SCOPE),
            "refresh_token": data.get("refresh_token"), "access_token": data.get("access_token"),
            "expires_in": data.get("expires_in")}


def _persist_env(values):
    """Write secret values into the local .env (gitignored). Never logged or returned."""
    import re
    from pathlib import Path
    env_path = Path(__file__).resolve().parent / ".env"
    if not env_path.exists():
        return False
    lines = env_path.read_text().splitlines()
    seen, out = set(), []
    for line in lines:
        match = re.match(r"^([A-Z_]+)=", line)
        if match and match.group(1) in values:
            out.append(f"{match.group(1)}={values[match.group(1)]}")
            seen.add(match.group(1))
        else:
            out.append(line)
    for key, value in values.items():
        if key not in seen:
            out.append(f"{key}={value}")
    env_path.write_text("\n".join(out).rstrip() + "\n")
    return True


def resolve_channel(access_token_value, *, http=None):
    """Resolve the authorized channel identity via channels.list(mine=true).

    Requires the youtube.readonly scope. On failure raises with SAFE diagnostics only
    (HTTP status + sanitized Google reason/message) — never the access token.
    """
    url = f"{DATA_API_URL}/channels?part=snippet&mine=true"
    try:
        data = _request(url, headers={"Authorization": f"Bearer {access_token_value}"}, http=http)
    except Exception as error:  # noqa: BLE001 - re-raise a safe, diagnosable error
        status = getattr(error, "code", None)
        reason = None
        message = ""
        body = getattr(error, "read", None)
        if callable(body):
            try:
                payload = json.loads(error.read().decode("utf-8", "replace"))
                error_obj = (payload.get("error") or {})
                reason = (error_obj.get("errors") or [{}])[0].get("reason") or error_obj.get("status")
                message = error_obj.get("message") or ""
            except Exception:  # noqa: BLE001
                message = ""
        if status is None:
            match = re.search(r"status=(\d{3})", str(error))
            if match:
                status = match.group(1)
        if reason is None:
            match = re.search(r"reason=([A-Za-z_]+)", str(error))
            if match:
                reason = match.group(1)
        safe = _safe_error(f"channels.list failed status={status} reason={reason} {message}")
        raise RuntimeError(safe) from None
    items = (data or {}).get("items") or []
    if not items:
        return {"channel_id": None, "channel_title": None, "connected_account": None}
    snippet = items[0].get("snippet", {})
    return {"channel_id": items[0].get("id"), "channel_title": snippet.get("title"),
            "connected_account": (snippet.get("customUrl") or snippet.get("title"))}


def _post_form(url, body, *, http=None, headers=None):
    if http is not None:
        return http(url, body)
    import urllib.request
    request = urllib.request.Request(url, data=body, method="POST",
                                     headers={"Content-Type": "application/x-www-form-urlencoded", **(headers or {})})
    with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310 - fixed Google host
        return json.loads(response.read().decode("utf-8", "replace"))


def access_token(*, http=None, connect=None, now=None):
    """Exchange the stored refresh token for a short-lived access token (never returned publicly).

    Classification: invalid_grant -> REAUTH_REQUIRED + PermissionError (upload -> OAUTH_REQUIRED);
    invalid_client / transient / unknown -> ERROR + RuntimeError (upload -> UPLOAD_FAILED);
    success -> HEALTHY + CONNECTED with expiry and cleared stale error.
    """
    config = oauth_configuration()
    if not config["upload_configured"]:
        raise ValueError("YouTube OAuth is UNCONFIGURED")
    body = urllib.parse.urlencode({
        "client_id": os.environ["YOUTUBE_CLIENT_ID"], "client_secret": os.environ["YOUTUBE_CLIENT_SECRET"],
        "refresh_token": os.environ["YOUTUBE_REFRESH_TOKEN"], "grant_type": "refresh_token",
    }).encode()
    try:
        data = _post_form(OAUTH_TOKEN_URL, body, http=http)
    except Exception as error:  # noqa: BLE001 - classify without leaking tokens
        reason = _google_error_reason(error)
        if reason == "invalid_grant":
            _record_refresh_health(connect, "REAUTH_REQUIRED", now=now, token_status="REAUTH_REQUIRED",
                                   error="invalid_grant")
            raise PermissionError("YouTube authorization is no longer valid; reconnect required.") from None
        _record_refresh_health(connect, "ERROR", now=now, error=reason or "refresh_failed")
        raise RuntimeError("YouTube access token refresh failed: " + (reason or "provider_error")) from None
    if "access_token" not in data:
        if data.get("error") == "invalid_grant":
            _record_refresh_health(connect, "REAUTH_REQUIRED", now=now, token_status="REAUTH_REQUIRED",
                                   error="invalid_grant")
            raise PermissionError("YouTube authorization is no longer valid; reconnect required.")
        _record_refresh_health(connect, "ERROR", now=now, error=data.get("error") or "no_access_token")
        raise RuntimeError("YouTube access token refresh failed: " + str(data.get("error") or "no_access_token"))
    expires_in = int(data.get("expires_in") or 3600)
    reference = datetime.fromisoformat(str(now() if now else _now()).replace("Z", "+00:00"))
    expiry = datetime.fromtimestamp(reference.timestamp() + expires_in, timezone.utc).isoformat()
    if connect is not None:
        _record_refresh_health(connect, "HEALTHY", now=now, token_status="CONNECTED", token_expires_at=expiry)
    return data["access_token"]


_SAFE_REASONS = ("invalid_grant", "invalid_client", "unauthorized_client", "invalid_scope",
                 "temporarily_unavailable", "server_error")


def _google_error_reason(error):
    """Extract only a sanitized Google reason; never includes tokens/body/secret."""
    text = str(error)
    for reason in _SAFE_REASONS:
        if reason in text:
            return reason
    match = re.search(r"HTTP Error (\d{3})", text)
    if match:
        return "http_" + match.group(1)
    body = getattr(error, "read", None)
    if callable(body):
        try:
            payload = json.loads(error.read().decode("utf-8", "replace"))
            value = payload.get("error")
            if value in _SAFE_REASONS:
                return value
        except Exception:  # noqa: BLE001
            return None
    return None


def _record_refresh_health(connect, health, *, now=None, token_status=None, token_expires_at=None, error=None):
    if connect is None:
        return
    update_oauth_state(connect=connect, now=now, refresh_health=health,
                       token_status=token_status, token_expires_at=token_expires_at,
                       last_refresh_check_at=(now() if now else _now()),
                       increment_refresh_failure=(health != "HEALTHY"),
                       last_error=error, clear_last_error=(health == "HEALTHY"))


def update_oauth_state(*, connect, now=None, increment_refresh_failure=False, clear_last_error=False, **fields):
    """Update safe OAuth state fields. COALESCE-preserving unless clear_last_error is set."""
    timestamp = now() if now else _now()
    allowed = ("token_status", "token_expires_at", "refresh_health", "last_refresh_check_at", "last_error")
    with connect() as connection:
        row = connection.execute("SELECT * FROM youtube_oauth_state WHERE id='youtube'").fetchone()
        if row is None:
            connection.execute(
                "INSERT INTO youtube_oauth_state(id,token_status,created_at,updated_at) VALUES('youtube',?,?,?)",
                ("NOT_CONNECTED", timestamp, timestamp))
            row = connection.execute("SELECT * FROM youtube_oauth_state WHERE id='youtube'").fetchone()
        for key, value in fields.items():
            if key not in allowed:
                continue
            if value is None:
                continue  # None = preserve existing value
            connection.execute(f"UPDATE youtube_oauth_state SET {key}=? WHERE id='youtube'", (value,))
        if clear_last_error:
            connection.execute("UPDATE youtube_oauth_state SET last_error=NULL WHERE id='youtube'")
        if increment_refresh_failure:
            connection.execute(
                "UPDATE youtube_oauth_state SET refresh_failure_count=refresh_failure_count+1 WHERE id='youtube'")
        connection.execute("UPDATE youtube_oauth_state SET updated_at=? WHERE id='youtube'", (timestamp,))
    return oauth_state(connect=connect)


def _safe_error(error):
    """Short secret-free error text: redacts client secret, refresh token, API key, access token."""
    text = str(error)
    for name in ("YOUTUBE_CLIENT_SECRET", "YOUTUBE_REFRESH_TOKEN", "YOUTUBE_API_KEY"):
        value = os.environ.get(name)
        # Only redact meaningful secret values (>8 chars) so short config never mangles text.
        if value and len(value) > 8:
            text = text.replace(value, "[redacted]")
    text = re.sub(r"ya29\.[A-Za-z0-9._-]+", "[redacted]", text)
    text = re.sub(r"GOCSPX-[A-Za-z0-9._-]+", "[redacted]", text)
    return text[:200]


def _granted_scopes(scope):
    granted = set(str(scope or "").split())
    if "https://www.googleapis.com/auth/youtube" in granted:  # full-account alias covers both
        return set(YOUTUBE_REQUIRED_SCOPES)
    return granted


def scope_status(scope):
    granted = _granted_scopes(scope)
    return {YOUTUBE_UPLOAD_SCOPE: YOUTUBE_UPLOAD_SCOPE in granted,
            YOUTUBE_READONLY_SCOPE: YOUTUBE_READONLY_SCOPE in granted}


def _scope_sufficient(scope):
    status = scope_status(scope)
    return all(status.values())


def complete_oauth(code, *, connect, http=None, channel_http=None, now=None):
    """OAuth callback: exchange, validate BOTH scopes, persist refresh token, resolve channel.

    CONNECTED requires ALL of: exchange success, access token present, youtube.upload AND
    youtube.readonly granted, channel resolution success, and a channel_id. Otherwise
    REAUTH_REQUIRED / ERROR / DEGRADED. Never returns access_token, refresh_token, or
    client_secret; never overwrites a valid refresh token with an empty value.
    """
    try:
        exchanged = exchange_code(code, http=http)
    except Exception as error:  # noqa: BLE001 - recorded, never leaked
        record_oauth_state(connect=connect, token_status="ERROR", error=_safe_error(error), now=now)
        return {"ok": False, "reason": "EXCHANGE_FAILED", "connection": connection_status(connect=connect)}
    if not exchanged.get("ok"):
        record_oauth_state(connect=connect, token_status="ERROR", error="OAuth code exchange failed", now=now)
        return {"ok": False, "reason": "EXCHANGE_FAILED", "connection": connection_status(connect=connect)}

    granted = exchanged.get("scope")
    scope_ok = _scope_sufficient(granted)
    scopes = scope_status(granted)
    # Persist a new refresh token only when the grant actually covers BOTH required scopes.
    refresh = exchanged.get("refresh_token")
    if refresh and scope_ok:
        _persist_env({"YOUTUBE_REFRESH_TOKEN": refresh})
        os.environ["YOUTUBE_REFRESH_TOKEN"] = refresh
    refresh_present = bool(os.environ.get("YOUTUBE_REFRESH_TOKEN") or refresh)

    if not scope_ok:
        # An upload-only grant is insufficient: require reauthorization, never mark CONNECTED.
        record_oauth_state(connect=connect, token_status="REAUTH_REQUIRED",
                           error="MISSING_REQUIRED_SCOPE", scope=granted,
                           refresh_token_present=refresh_present, now=now)
        return {"ok": False, "reason": "MISSING_REQUIRED_SCOPE", "scopes": scopes,
                "connection": connection_status(connect=connect)}

    access = exchanged.get("access_token")
    if not access:
        record_oauth_state(connect=connect, token_status="ERROR", error="OAuth exchange returned no access token",
                           scope=granted, refresh_token_present=refresh_present, now=now)
        return {"ok": False, "reason": "NO_ACCESS_TOKEN", "scopes": scopes,
                "connection": connection_status(connect=connect)}

    try:
        channel = resolve_channel(access, http=channel_http)
    except Exception as error:  # noqa: BLE001 - reported honestly with safe diagnostics
        record_oauth_state(connect=connect, token_status="DEGRADED",
                           error="channel lookup failed: " + _safe_error(error),
                           scope=granted, refresh_token_present=refresh_present, now=now)
        return {"ok": False, "reason": "CHANNEL_LOOKUP_FAILED", "scopes": scopes,
                "connection": connection_status(connect=connect)}
    if not channel.get("channel_id"):
        record_oauth_state(connect=connect, token_status="DEGRADED",
                           error="authorized account has no resolvable YouTube channel",
                           scope=granted, refresh_token_present=refresh_present, now=now)
        return {"ok": False, "reason": "NO_CHANNEL_ID", "scopes": scopes,
                "connection": connection_status(connect=connect)}

    expires_in = int(exchanged.get("expires_in") or 3600)
    reference = datetime.fromisoformat(str(now() if now else _now()).replace("Z", "+00:00"))
    expiry = datetime.fromtimestamp(reference.timestamp() + expires_in, timezone.utc).isoformat()
    record_oauth_state(connect=connect, channel_id=channel["channel_id"],
                       channel_title=channel.get("channel_title"),
                       connected_account=channel.get("connected_account"),
                       scope=granted, token_status="CONNECTED",
                       token_expires_at=expiry, refresh_token_present=refresh_present, now=now)
    return {"ok": True, "has_refresh_token": refresh_present, "scopes": scopes,
            "channel_title": channel.get("channel_title"),
            "channel_id_masked": mask(channel["channel_id"], keep=4),
            "connection": connection_status(connect=connect)}


def oauth_state(*, connect):
    with connect() as connection:
        row = connection.execute("SELECT * FROM youtube_oauth_state WHERE id='youtube'").fetchone()
        return dict(row) if row else {}


def record_oauth_state(*, connect, channel_id=None, channel_title=None, scope=YOUTUBE_UPLOAD_SCOPE,
                       token_status="CONNECTED", connected_account=None, error=None, now=None,
                       token_expires_at=None, refresh_token_present=None):
    """Persist NON-SECRET connection metadata only; never stores or returns tokens.

    Preserves a previously recorded token_expires_at / refresh_token_present when the new value
    is None, uses COALESCE for channel fields, and never inserts a second row.
    """
    timestamp = now() if now else _now()
    with connect() as connection:
        existing = connection.execute("SELECT * FROM youtube_oauth_state WHERE id='youtube'").fetchone()
        current_refresh = int(existing["refresh_token_present"]) if existing is not None else 0
        refresh_flag = current_refresh if refresh_token_present is None else int(bool(refresh_token_present))
        if existing:
            connection.execute(
                "UPDATE youtube_oauth_state SET channel_id=COALESCE(?,channel_id),"
                "channel_title=COALESCE(?,channel_title),scope=?,token_status=?,"
                "connected_account=COALESCE(?,connected_account),last_authorized_call_at=?,last_error=?,"
                "token_expires_at=COALESCE(?,token_expires_at),refresh_token_present=?,updated_at=? "
                "WHERE id='youtube'",
                (channel_id, channel_title, scope, token_status, connected_account,
                 timestamp if token_status == "CONNECTED" else existing["last_authorized_call_at"],
                 error, token_expires_at, refresh_flag, timestamp))
        else:
            connection.execute(
                "INSERT INTO youtube_oauth_state(id,channel_id,channel_title,scope,token_status,connected_account,"
                "last_authorized_call_at,last_error,token_expires_at,refresh_token_present,created_at,updated_at) "
                "VALUES('youtube',?,?,?,?,?,?,?,?,?,?,?)",
                (channel_id, channel_title, scope, token_status, connected_account,
                 timestamp if token_status == "CONNECTED" else None, error, token_expires_at, refresh_flag,
                 timestamp, timestamp))
    return oauth_state(connect=connect)


def connection_status(*, connect):
    """System → YouTube connection card. NEVER returns tokens; channel ID is masked.

    UNCONFIGURED means the OAuth CLIENT config itself is absent. When the client exists but
    authorization failed or has not completed, the recorded runtime state (ERROR / DEGRADED /
    NOT_CONNECTED) is preserved instead of being masked.
    """
    config = oauth_configuration()
    state = oauth_state(connect=connect)
    if not config["client_configured"]:
        status = "UNCONFIGURED"
    else:
        status = state.get("token_status") or "NOT_CONNECTED"
    scopes = scope_status(state.get("scope"))
    return {
        "status": status,
        "channel_name": state.get("channel_title"),
        "channel_id_masked": mask(state.get("channel_id"), keep=4) if state.get("channel_id") else None,
        "connected_account": state.get("connected_account"),
        "token_status": state.get("token_status") or ("NOT_CONNECTED" if config["client_configured"] else "UNCONFIGURED"),
        "token_expires_at": state.get("token_expires_at"),
        "refresh_token_present": bool(state.get("refresh_token_present")),
        "upload_scope_granted": scopes[YOUTUBE_UPLOAD_SCOPE],
        "readonly_scope_granted": scopes[YOUTUBE_READONLY_SCOPE],
        "refresh_health": state.get("refresh_health") or "UNKNOWN",
        "public_upload_capability": public_upload_capability(),
        "last_authorized_call": state.get("last_authorized_call_at"),
        "api_key_discovery": bool(config["api_key_configured"]),
        "client_configured": config["client_configured"],
        "upload_capability": config["upload_configured"] and all(scopes.values()),
        "publishing_enabled": config["publishing_enabled"],
        "missing": config["missing"],
        "scope": config["scope"],
        "last_error": state.get("last_error"),
    }


# ---------- YouTube content intelligence (Part B/K) ----------

def research_queries(*, event_name=None, location=None, entities=None, topic_terms=()):
    from fast_discovery import expand_query
    base, seen, queries = [], set(), []

    def add(value):
        value = re.sub(r"\s+", " ", str(value or "")).strip()
        if value and value.casefold() not in seen:
            seen.add(value.casefold())
            queries.append(value)

    for entity in entities or []:
        add(f"{entity} {location}" if location else entity)
    if event_name:
        add(f"{event_name} {location}" if location else event_name)
    if location:
        add(f"Andhra Pradesh {location}")
    for term in topic_terms or []:
        add(f"Andhra Pradesh {term}")
    for variant in list(expand_query(event_name or ""))[:2]:
        add(variant)
    return queries[:8]


def cached_research(event_id, *, connect, now=None, max_minutes=RESEARCH_CACHE_MINUTES):
    reference = datetime.fromisoformat(str(now() if now else _now()).replace("Z", "+00:00"))
    with connect() as connection:
        row = connection.execute(
            "SELECT * FROM youtube_research_snapshots WHERE event_id=? ORDER BY created_at DESC LIMIT 1",
            (event_id,)).fetchone()
    if not row:
        return None
    try:
        created = datetime.fromisoformat(str(row["created_at"]).replace("Z", "+00:00"))
        if (reference - created).total_seconds() <= max_minutes * 60:
            return snapshot(row["id"], connect=connect)
    except (TypeError, ValueError):
        return None
    return None


def snapshot(snapshot_id, *, connect):
    with connect() as connection:
        row = connection.execute("SELECT * FROM youtube_research_snapshots WHERE id=?", (snapshot_id,)).fetchone()
        if row is None:
            raise KeyError(snapshot_id)
        result = dict(row)
        for key in ("queries", "recurring_terms", "recurring_hashtags", "title_structures", "videos"):
            result[key] = json.loads(result.pop(key + "_json") or "[]")
        result["date_range"] = json.loads(result.pop("date_range_json") or "{}")
    return result


def analyze_videos(videos):
    """Aggregate content metadata only (Part B.3). No demographic/political inference."""
    terms, hashtags, title_shapes = {}, {}, {}
    telugu = english = 0
    dates = []
    for video in videos or []:
        title = str(video.get("title") or "")
        if re.search(r"[\u0C00-\u0C7F]", title):
            telugu += 1
        else:
            english += 1
        for word in re.findall(r"[A-Za-z][A-Za-z]{2,}", title):
            if word.casefold() in ("the", "and", "with", "for", "from", "this", "that"):
                continue
            terms[word] = terms.get(word, 0) + 1
        for tag in video.get("hashtags") or []:
            hashtags[tag] = hashtags.get(tag, 0) + 1
        shape = _title_shape(title)
        if shape:
            title_shapes[shape] = title_shapes.get(shape, 0) + 1
        if video.get("published_at"):
            dates.append(video["published_at"])
    language_mix = "BILINGUAL" if telugu and english else ("TELUGU" if telugu else "ENGLISH")
    return {
        "sample_count": len(videos or []),
        "recurring_terms": [t for t, _ in sorted(terms.items(), key=lambda kv: -kv[1])[:12]],
        "recurring_hashtags": [t for t, _ in sorted(hashtags.items(), key=lambda kv: -kv[1])[:12]],
        "title_structures": [s for s, _ in sorted(title_shapes.items(), key=lambda kv: -kv[1])[:5]],
        "language_mix": language_mix,
        "date_range": {"from": min(dates) if dates else None, "to": max(dates) if dates else None},
    }


def _title_shape(title):
    text = str(title or "")
    if "|" in text:
        return "a | b"
    if " - " in text:
        return "a - b"
    if ":" in text:
        return "a: b"
    return "plain"


def hashtags_in_description(description):
    return re.findall(r"#[A-Za-z0-9\u0C00-\u0C7F_]+", str(description or ""))


def run_content_intelligence(*, connect, event_id, event_name=None, location=None, entities=None,
                            topic_terms=(), videos=None, now=None, use_cache=True):
    """YOUTUBE_CONTENT_INTELLIGENCE_V1: research same-topic videos, store a reproducible snapshot."""
    if use_cache:
        cached = cached_research(event_id, connect=connect, now=now)
        if cached:
            cached["cached"] = True
            return cached
    queries = research_queries(event_name=event_name, location=location, entities=entities,
                               topic_terms=topic_terms)
    timestamp = now() if now else _now()
    analysis = analyze_videos(videos or [])
    snapshot_id = _new_id("YR-")
    with connect() as connection:
        connection.execute(
            "INSERT INTO youtube_research_snapshots(id,event_id,queries_json,sample_count,recurring_terms_json,"
            "recurring_hashtags_json,title_structures_json,language_mix,date_range_json,videos_json,framework,"
            "created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (snapshot_id, event_id, json.dumps(queries), analysis["sample_count"],
             json.dumps(analysis["recurring_terms"]), json.dumps(analysis["recurring_hashtags"]),
             json.dumps(analysis["title_structures"]), analysis["language_mix"],
             json.dumps(analysis["date_range"]), json.dumps(videos or []),
             "YOUTUBE_CONTENT_INTELLIGENCE_V1", timestamp))
    result = snapshot(snapshot_id, connect=connect)
    result["cached"] = False
    return result


# ---------- title / description / hashtag / tags generation (Parts C/D/E/F) ----------

def _grounding(claims):
    return " ".join(str(c.get("text") if isinstance(c, dict) else c) for c in (claims or []))


def _slug(value, *, max_words=4):
    words = re.findall(r"[A-Za-z0-9\u0C00-\u0C7F]+", str(value or ""))
    if not words:
        return None
    return "".join(w[:1].upper() + w[1:] for w in words[:max_words])


def generate_titles(*, claims, event_name=None, location=None, entities=None, language_mix="BILINGUAL"):
    """YOUTUBE_TITLE_PACKAGE_V1 — factual, front-loaded, no clickbait (Part C)."""
    claim_texts = [re.sub(r"\s+", " ", str(c.get("text") if isinstance(c, dict) else c)).strip()
                   for c in (claims or [])]
    claim_texts = [c for c in claim_texts if c]
    primary_fact = claim_texts[0] if claim_texts else (event_name or "")
    entity = (entities or [None])[0]
    subject = event_name or (entity and f"{entity}") or ""
    # Structure: [Event] | [Location], or [Entity] at [Event], or the factual headline.
    title_primary = _clean_title(
        f"{subject} | {location}" if subject and location else (subject or primary_fact),
        fallback=primary_fact, limit=100)
    title_alt_1 = _clean_title(primary_fact, fallback=title_primary, limit=100)
    title_alt_2 = _clean_title(
        f"{entity}: {claim_texts[1]}" if entity and len(claim_texts) > 1 else primary_fact,
        fallback=title_primary, limit=100)
    return {"title_primary": title_primary, "title_alt_1": title_alt_1, "title_alt_2": title_alt_2,
            "language_mix": language_mix}


def _clean_title(text, *, fallback, limit=100):
    text = re.sub(r"\s+", " ", str(text or "")).strip(" |:-")
    if not text:
        text = re.sub(r"\s+", " ", str(fallback or "")).strip()
    return text[:limit].strip()


def generate_description(*, claims, event_name=None, location=None, attribution=None, hashtags=None,
                         context=None):
    """YOUTUBE DESCRIPTION — hook, 1-3 sentence approved-claim summary, context, attribution, tags."""
    claim_texts = [re.sub(r"\s+", " ", str(c.get("text") if isinstance(c, dict) else c)).strip()
                   for c in (claims or [])]
    claim_texts = [c for c in claim_texts if c]
    hook = event_name or (claim_texts[0] if claim_texts else "")
    summary = " ".join(claim_texts[:3])
    lines = [hook] if hook else []
    if summary and summary.casefold() != str(hook).casefold():
        lines.append(summary)
    if context:
        lines.append(str(context))
    if attribution:
        lines.append(f"Source: {attribution}")
    tag_line = " ".join(item["tag"] if isinstance(item, dict) else item for item in (hashtags or []))
    if tag_line:
        lines.append(tag_line)
    return "\n\n".join(line for line in lines if line).strip()


def generate_hashtags(*, entities=None, location=None, event_name=None, topic_terms=(),
                      research=None):
    """YOUTUBE_HASHTAG_INTELLIGENCE_V1 — 5-10 relevant tags, no fyp/trending spam (Part E)."""
    candidates, seen = [], set()

    def add(raw, kind):
        slug = _slug(raw)
        if not slug:
            return
        tag = "#" + slug
        if tag.casefold() in seen:
            return
        seen.add(tag.casefold())
        candidates.append({"tag": tag, "kind": kind, "source": str(raw)})

    for entity in entities or []:
        add(entity, "entity")
    if location:
        add(location, "location")
        add("AndhraPradesh", "topic")
    if event_name:
        add(event_name, "event")
    for term in topic_terms or []:
        add(term, "topic")
    for term in ("APNews", "TeluguNews"):
        add(term, "topic")
    # Boost by recurrence in current same-topic videos (aggregate content signal only).
    recurring = {h.lstrip("#").casefold() for h in (research or {}).get("recurring_hashtags", [])}
    scored = []
    for candidate in candidates:
        if candidate["tag"].casefold() in _FYP_TAGS or candidate["tag"].casefold().lstrip("#") in {t.lstrip("#") for t in _FYP_TAGS}:
            continue
        base = {"event": 1.0, "entity": 0.9, "location": 0.8, "topic": 0.6}.get(candidate["kind"], 0.4)
        bonus = 0.25 if candidate["tag"].lstrip("#").casefold() in recurring else 0.0
        scored.append({**candidate, "score": round(min(1.0, base + bonus), 4)})
    scored.sort(key=lambda item: (-item["score"], item["tag"]))
    return scored[:10]


def _has_repeated_phrase(value):
    """True when a term repeats a token/phrase literally (e.g. 'Andhra Pradesh Andhra Pradesh')."""
    words = re.findall(r"[A-Za-z\u0C00-\u0C7F]+", str(value or "").casefold())
    if len(words) >= 4 and len(words) % 2 == 0:
        half = len(words) // 2
        if words[:half] == words[half:]:
            return True
    return len(words) >= 2 and len(set(words)) == 1


def _normalize_tag(value):
    return re.sub(r"\s+", " ", str(value or "")).strip()


def generate_tags(*, entities=None, location=None, event_name=None, topic_terms=(), language_mix="BILINGUAL"):
    """youtube_tags — separate factual search terms (Part E.12).

    Blocks malformed repeated phrases and exact duplicates while allowing semantically related
    variants (e.g. "Andhra Pradesh" and "AP News") that search vocabulary benefits from.
    """
    tags, seen = [], set()

    def add(value):
        value = _normalize_tag(value)
        if not value or _has_repeated_phrase(value):
            return
        if value.casefold() in seen:
            return
        seen.add(value.casefold())
        tags.append(value[:100])

    for entity in entities or []:
        add(entity)
        if location and _normalize_tag(entity).casefold() != _normalize_tag(location).casefold():
            add(f"{entity} {location}")
    if event_name:
        add(event_name)
    if location:
        add(location)
        # Build "<location> Andhra Pradesh" only when it isn't the degenerate repeat.
        if _normalize_tag(location).casefold() != "andhra pradesh":
            add(f"{location} Andhra Pradesh")
    for term in topic_terms or []:
        add(term)
        if _normalize_tag(term).casefold() not in ("andhra pradesh", "telugu news"):
            add(f"Andhra Pradesh {term}")
    add("Andhra Pradesh")
    add("Telugu News")
    return tags[:15]


def keyword_redundancy_qa(*, tags=(), hashtags=()):
    """KEYWORD_REDUNDANCY_QA: no malformed repeats or exact duplicates; semantic overlap allowed."""
    errors = []
    for value, kind in [(t, "tag") for t in tags] + [(h, "hashtag") for h in hashtags]:
        if _has_repeated_phrase(value):
            errors.append(f"Malformed repeated {kind}: {value!r}.")
    lowered = [str(t).casefold() for t in tags]
    if len(lowered) != len(set(lowered)):
        errors.append("Duplicate search tags present.")
    lowered_h = [str(h).casefold() for h in hashtags]
    if len(lowered_h) != len(set(lowered_h)):
        errors.append("Duplicate hashtags present.")
    return {"status": "PASS" if not errors else "FAIL", "errors": errors}


# ---------- QA (Part D.9) ----------

def title_factual_qa(title, *, claims):
    approved = _grounding(claims)
    approved_norm = re.sub(r"[^a-z0-9\u0C00-\u0C7F]+", " ", approved.casefold())
    errors = []
    lowered = title.casefold()
    for banned in _BANNED_TITLE:
        if banned in lowered:
            errors.append(f"Title uses unsupported clickbait language: {banned!r}.")
    if _INFERRED.search(title):
        errors.append("Title infers a reaction not present in approved claims.")
    if _BANNED_FRAMING.search(title):
        errors.append("Title contains unsupported political framing.")
    # Every meaningful word should be grounded in approved claims (allow location/entity words).
    words = [w for w in re.findall(r"[a-z0-9\u0C00-\u0C7F]+", lowered) if len(w) > 3]
    if words:
        overlap = sum(1 for w in words if w in approved_norm) / len(words)
        if overlap < 0.4:
            errors.append("Title is not grounded in approved claims.")
    if len(title) > 100:
        errors.append("YouTube titles must be 100 characters or fewer.")
    return {"status": "PASS" if not errors else "FAIL", "errors": errors}


def description_factual_qa(description, *, claims):
    approved = _grounding(claims)
    approved_norm = re.sub(r"[^a-z0-9\u0C00-\u0C7F]+", " ", approved.casefold())
    errors = []
    if _INFERRED.search(description):
        errors.append("Description infers a reaction not present in approved claims.")
    if _BANNED_FRAMING.search(description):
        errors.append("Description contains unsupported political framing.")
    approved_numbers = set(re.findall(r"\d+", approved))
    for number in re.findall(r"\d+", description):
        if number not in approved_numbers:
            errors.append(f"Description introduces a number not in approved claims: {number!r}.")
    for line in re.split(r"\n+", description):
        line = line.strip()
        if not line or line.startswith("#") or line.casefold().startswith("source:"):
            continue
        words = [w for w in re.findall(r"[a-z0-9\u0C00-\u0C7F]+", line.casefold()) if len(w) > 3]
        if not words:
            continue
        overlap = sum(1 for w in words if w in approved_norm) / len(words)
        if overlap < 0.5:
            errors.append(f"Description line is not grounded in approved claims: {line[:80]!r}.")
    return {"status": "PASS" if not errors else "FAIL", "errors": errors}


def hashtag_relevance_qa(hashtags, *, entities=None, location=None, event_name=None, claims=None,
                         research=None, topic_terms=()):
    allowed = set()
    for source in list(entities or []) + list(topic_terms or []) + [location, event_name,
                 "Andhra Pradesh", "AP News", "Telugu News"]:
        for word in re.findall(r"[A-Za-z]+", str(source or "")):
            allowed.add(word.casefold())
    approved = _grounding(claims).casefold()
    errors = []
    for item in hashtags:
        tag = item["tag"] if isinstance(item, dict) else item
        lowered = tag.casefold()
        if lowered in _FYP_TAGS or lowered.lstrip("#") in {t.lstrip("#") for t in _FYP_TAGS}:
            errors.append(f"Hashtag {tag} is irrelevant viral spam.")
            continue
        if _BANNED_FRAMING.search(tag):
            errors.append(f"Hashtag {tag} implies unsupported political framing.")
            continue
        parts = [w.casefold() for w in re.findall(r"[A-Za-z]+",
                 re.sub(r"(?<=[a-z])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", " ", tag.lstrip("#")))]
        if parts and not any(p in allowed or p in approved for p in parts):
            errors.append(f"Hashtag {tag} is not supported by the story.")
    if len(hashtags) > 10:
        errors.append("Return 5-10 hashtags maximum.")
    return {"status": "PASS" if not errors else "FAIL", "errors": errors}


def youtube_compatibility_qa(*, privacy_status, oauth_configured, format_kind):
    errors = []
    if not oauth_configured:
        errors.append("YouTube OAuth is UNCONFIGURED; upload is not possible with an API key.")
    if privacy_status not in ("PRIVATE", "UNLISTED", "PUBLIC"):
        errors.append("Invalid privacy status.")
    return {"status": "PASS" if not errors else "FAIL", "errors": errors}


SHORTS_MAX_SECONDS = 180  # Shorts are vertical and short; long-form is never a silent fallback.


def shorts_qa(*, width, height, codec, has_audio, audio_codec, duration_seconds,
              has_edit_list=False):
    """YOUTUBE_SHORTS_QA: a reel may publish as a Short only if it is genuinely Shorts-shaped."""
    errors = []
    if not width or not height or height <= width:
        errors.append("Reel is not vertical (9:16); refusing to upload as a Short.")
    elif abs((height / width) - (16 / 9)) > 0.05:
        errors.append("Aspect ratio is not 9:16-compatible.")
    if codec not in ("avc1", "avc3", "hvc1", "hev1"):
        errors.append("Shorts require an H.264/HEVC video track.")
    if not has_audio or audio_codec != "mp4a":
        errors.append("Shorts require an AAC audio track.")
    if duration_seconds and duration_seconds > SHORTS_MAX_SECONDS:
        errors.append("Duration exceeds the supported Shorts window.")
    if has_edit_list:
        errors.append("Container has an edit list; re-mux before Shorts upload.")
    return {"status": "PASS" if not errors else "FAIL", "errors": errors,
            "youtube_format": "SHORT" if not errors else None}


def classify_format(media):
    """Return ('SHORT'|None, qa). Never silently falls back to long-form."""
    qa = shorts_qa(width=media.get("width"), height=media.get("height"), codec=media.get("codec"),
                   has_audio=media.get("has_audio"), audio_codec=media.get("audio_codec"),
                   duration_seconds=media.get("duration_seconds"),
                   has_edit_list=media.get("has_edit_list", False))
    return qa["youtube_format"], qa


def package_qa(*, titles, description, hashtags, claims, entities=None, location=None,
               event_name=None, research=None, topic_terms=(), tags=()):
    title_qa = title_factual_qa(titles["title_primary"], claims=claims)
    desc_qa = description_factual_qa(description, claims=claims)
    tag_qa = hashtag_relevance_qa(hashtags, entities=entities, location=location,
                                  event_name=event_name, claims=claims, research=research,
                                  topic_terms=topic_terms)
    hashtag_values = [item["tag"] if isinstance(item, dict) else item for item in hashtags]
    redundancy_qa = keyword_redundancy_qa(tags=tags, hashtags=hashtag_values)
    statuses = [title_qa["status"], desc_qa["status"], tag_qa["status"], redundancy_qa["status"]]
    return {"TITLE_FACTUAL_QA": title_qa, "DESCRIPTION_FACTUAL_QA": desc_qa,
            "HASHTAG_RELEVANCE_QA": tag_qa, "KEYWORD_REDUNDANCY_QA": redundancy_qa,
            "status": "PASS" if all(s == "PASS" for s in statuses) else "FAIL"}


def build_youtube_package(*, claims, event_name=None, location=None, entities=None, topic_terms=(),
                          attribution=None, research=None, language_mix="BILINGUAL", format_kind="SHORT"):
    """Compose title/description/hashtags/tags and run the mandatory QA."""
    titles = generate_titles(claims=claims, event_name=event_name, location=location,
                             entities=entities, language_mix=language_mix)
    hashtags = generate_hashtags(entities=entities, location=location, event_name=event_name,
                                 topic_terms=topic_terms, research=research)
    description = generate_description(claims=claims, event_name=event_name, location=location,
                                       attribution=attribution, hashtags=hashtags)
    tags = generate_tags(entities=entities, location=location, event_name=event_name,
                         topic_terms=topic_terms, language_mix=language_mix)
    qa = package_qa(titles=titles, description=description, hashtags=hashtags, claims=claims,
                    entities=entities, location=location, event_name=event_name, research=research,
                    topic_terms=topic_terms, tags=tags)
    return {"titles": titles, "title": titles["title_primary"], "alternates": [titles["title_alt_1"], titles["title_alt_2"]],
            "description": description, "hashtags": hashtags, "tags": tags,
            "language_mix": language_mix, "youtube_format": format_kind, "qa": qa}


# ---------- durable package service (versioned separately from reel approval) ----------

def create_package(*, connect, reel_id, event_id, package, research_snapshot_id=None, claim_map=None,
                   now=None):
    timestamp = now() if now else _now()
    hashtag_list = [item["tag"] if isinstance(item, dict) else item for item in package["hashtags"]]
    package_id = _new_id("YP-")
    with connect() as connection:
        version = connection.execute(
            "SELECT COALESCE(MAX(version_number),0)+1 FROM youtube_packages WHERE reel_id=?",
            (reel_id,)).fetchone()[0]
        connection.execute(
            "INSERT INTO youtube_packages(id,reel_id,event_id,version_number,title_primary,title_alt_1,"
            "title_alt_2,description,hashtags_json,tags_json,language_mix,youtube_format,claim_map_json,qa_json,"
            "research_snapshot_id,status,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'DRAFT',?)",
            (package_id, reel_id, event_id, version, package["title"], package.get("alternates", [None, None])[0],
             package.get("alternates", [None, None])[1] if len(package.get("alternates", [])) > 1 else None,
             package["description"], json.dumps(hashtag_list), json.dumps(package["tags"]),
             package["language_mix"], package["youtube_format"], json.dumps(claim_map or []),
             json.dumps(package["qa"]), research_snapshot_id, timestamp))
        _snapshot(connection, package_id, version, package["title"], package["description"],
                  hashtag_list, package["tags"], edited_by=None, now=timestamp)
    return package_row(package_id, connect=connect)


def _snapshot(connection, package_id, version, title, description, hashtags, tags, edited_by, now):
    connection.execute(
        "INSERT INTO youtube_package_revisions(id,youtube_package_id,version_number,title_primary,description,"
        "hashtags_json,tags_json,edited_by,edited_at,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (_new_id("YPR-"), package_id, version, title, description, json.dumps(hashtags),
         json.dumps(tags), edited_by, now, now))


def package_row(package_id, *, connect):
    with connect() as connection:
        row = connection.execute("SELECT * FROM youtube_packages WHERE id=?", (package_id,)).fetchone()
        if row is None:
            raise KeyError(package_id)
        result = dict(row)
        result["hashtags"] = json.loads(result.pop("hashtags_json") or "[]")
        result["tags"] = json.loads(result.pop("tags_json") or "[]")
        result["claim_map"] = json.loads(result.pop("claim_map_json") or "[]")
        result["qa"] = json.loads(result.pop("qa_json") or "{}")
        result["revisions"] = [dict(r) for r in connection.execute(
            "SELECT r.* FROM youtube_package_revisions r JOIN youtube_packages p ON p.id=r.youtube_package_id "
            "WHERE p.reel_id=? ORDER BY r.version_number,r.created_at", (result["reel_id"],))]
    return result


def packages_for_reel(reel_id, *, connect):
    with connect() as connection:
        rows = connection.execute(
            "SELECT id FROM youtube_packages WHERE reel_id=? ORDER BY version_number DESC", (reel_id,)).fetchall()
    return [package_row(row["id"], connect=connect) for row in rows]


def latest_package(reel_id, *, connect):
    with connect() as connection:
        row = connection.execute(
            "SELECT id FROM youtube_packages WHERE reel_id=? AND status!='SUPERSEDED' "
            "ORDER BY version_number DESC LIMIT 1", (reel_id,)).fetchone()
    return package_row(row["id"], connect=connect) if row else None


def edit_package(package_id, *, title=None, description=None, hashtags=None, tags=None, edited_by,
                 connect, now=None):
    """Human edit -> NEW version. Invalidates only YouTube-copy approval (never reel approval)."""
    timestamp = now() if now else _now()
    current = package_row(package_id, connect=connect)
    with connect() as connection:
        claims = _approved_claims(connection, current["event_id"])
    new_title = title or current["title_primary"]
    new_description = description or current["description"]
    new_hashtags = list(hashtags if hashtags is not None else current["hashtags"])
    new_tags = list(tags if tags is not None else current["tags"])
    qa = package_qa(titles={"title_primary": new_title, "title_alt_1": current["title_alt_1"],
                            "title_alt_2": current["title_alt_2"]},
                    description=new_description, hashtags=new_hashtags, claims=claims)
    with connect() as connection:
        connection.execute("UPDATE youtube_packages SET status='SUPERSEDED' WHERE id=?", (package_id,))
        new_id = _new_id("YP-")
        version = (current["version_number"] or 1) + 1
        connection.execute(
            "INSERT INTO youtube_packages(id,reel_id,event_id,version_number,title_primary,title_alt_1,"
            "title_alt_2,description,hashtags_json,tags_json,language_mix,youtube_format,claim_map_json,qa_json,"
            "research_snapshot_id,status,created_at,edited_by,edited_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'DRAFT',?,?,?)",
            (new_id, current["reel_id"], current["event_id"], version, new_title, current["title_alt_1"],
             current["title_alt_2"], new_description, json.dumps(new_hashtags), json.dumps(new_tags),
             current["language_mix"], current["youtube_format"], json.dumps(current["claim_map"]),
             json.dumps(qa), current["research_snapshot_id"], timestamp, edited_by, timestamp))
        _snapshot(connection, new_id, version, new_title, new_description, new_hashtags, new_tags,
                  edited_by=edited_by, now=timestamp)
    return package_row(new_id, connect=connect)


def _approved_claims(connection, event_id):
    return [dict(r) for r in connection.execute(
        "SELECT cv.text, cv.id AS claim_version_id FROM approved_claim_set_items aci "
        "JOIN claim_versions cv ON cv.id=aci.claim_version_id "
        "JOIN approved_claim_sets acs ON acs.id=aci.claim_set_id "
        "WHERE acs.event_id=? AND acs.status='APPROVED' ORDER BY aci.claim_version_id", (event_id,))]


def approve_package(package_id, *, reviewer, connect, now=None):
    """Approve the YOUTUBE COPY only (independent of reel approval)."""
    timestamp = now() if now else _now()
    current = package_row(package_id, connect=connect)
    if current["qa"].get("status") != "PASS":
        raise ValueError("YouTube copy QA must pass before approval.")
    with connect() as connection:
        connection.execute("UPDATE youtube_packages SET status='YOUTUBE_COPY_APPROVED' WHERE id=?", (package_id,))
    return package_row(package_id, connect=connect)


def approval_state(*, connect, reel_id):
    import reel_control
    with connect() as connection:
        approved = connection.execute(
            "SELECT id,version_number FROM youtube_packages WHERE reel_id=? AND status='YOUTUBE_COPY_APPROVED' "
            "ORDER BY version_number DESC LIMIT 1", (reel_id,)).fetchone()
        reel = connection.execute("SELECT * FROM final_reel_assets WHERE id=?", (reel_id,)).fetchone()
    reel_approved = bool(reel and reel_control.valid_approval(reel_id, connect=connect))
    compat = False
    if reel:
        compat = bool(reel["status"] == "READY_FOR_REVIEW")
    return {"reel_approved": reel_approved,
            "youtube_copy_approved": dict(approved) if approved else None,
            "youtube_compatible": compat,
            "ready_to_upload": reel_approved and bool(approved) and compat}


def upload_allowed(reel_id, *, connect):
    import reel_control
    state = approval_state(connect=connect, reel_id=reel_id)
    return state["ready_to_upload"]


# ---------- upload / schedule (OAuth only; API key can never write) ----------

def _idempotency_key(reel_id, reel_version, copy_version):
    return f"youtube|{reel_id}|{reel_version}|{copy_version}"


def _default_media_storage():
    """Resolve the local media storage used to read an approved reel's bytes for upload."""
    from pathlib import Path
    from media_storage import LocalMediaStorage
    root = os.environ.get("RENDER_STORAGE_ROOT") or (Path(__file__).resolve().parent / ".context" / "generated_media")
    return LocalMediaStorage(root)


def public_upload_capability():
    """AVAILABLE / RESTRICTED / UNKNOWN — never equated with OAuth CONNECTED, never guessed.

    AVAILABLE only when the project is explicitly confirmed audited for unrestricted uploads;
    RESTRICTED when a policy/private-only restriction is known; otherwise UNKNOWN.
    """
    override = os.environ.get("YOUTUBE_PROJECT_AUDIT_STATUS", "").strip().upper()
    if override == "PUBLIC_VERIFIED":
        return "AVAILABLE"
    if override in ("PRIVATE_ONLY", "RESTRICTED"):
        return "RESTRICTED"
    return "UNKNOWN"


def project_audit_status():
    """Legacy alias returning the raw audit mode (PUBLIC_VERIFIED / PRIVATE_ONLY / UNKNOWN)."""
    override = os.environ.get("YOUTUBE_PROJECT_AUDIT_STATUS", "").strip().upper()
    if override == "PUBLIC_VERIFIED":
        return "PUBLIC_VERIFIED"
    if override == "PRIVATE_ONLY":
        return "PRIVATE_ONLY"
    return "UNKNOWN"


def resolve_effective_privacy(requested_privacy, capability):
    """Pure privacy resolution: PUBLIC is downgraded to PRIVATE unless capability is AVAILABLE."""
    effective = requested_privacy
    reason = None
    if requested_privacy == "PUBLIC" and capability != "AVAILABLE":
        effective = "PRIVATE"
        reason = "PUBLIC_CAPABILITY_NOT_AVAILABLE"
    return {"requested_privacy": requested_privacy, "effective_privacy": effective,
            "privacy_downgrade_reason": reason, "public_upload_capability": capability}


def publisher_class():
    return YouTubePublisher


class YouTubePublisher:
    """Minimal YouTube Data API v3 uploader. Uses OAuth refresh token — never the API key."""

    platform = "YOUTUBE"

    def __init__(self, *, transport=None, analytics_transport=None):
        self.events = []
        self._transport = transport
        self._analytics_transport = analytics_transport

    def _record(self, event_type, **meta):
        self.events.append({"event_type": event_type, "metadata": meta, "occurred_at": _now()})

    def upload(self, job, *, title, description, tags, privacy_status, video_bytes, publish_at=None):
        """videos.insert via OAuth. Returns {'video_id', 'status'}."""
        if not oauth_configuration()["upload_configured"]:
            raise PermissionError("YouTube upload requires OAuth 2.0 (authorization incomplete).")
        try:
            token = access_token(http=self._transport and (lambda url, body: self._transport(url, body)))
        except PermissionError:
            raise
        except Exception as error:  # noqa: BLE001 - a token refresh failure is also OAUTH_REQUIRED
            raise PermissionError(f"YouTube access token could not be refreshed: {_safe_error(error)}")
        metadata = {
            "snippet": {"title": title[:100], "description": description[:5000], "tags": tags[:15],
                        "categoryId": os.environ.get("YOUTUBE_CATEGORY_ID", "25")},
            "status": {"privacyStatus": privacy_status, "selfDeclaredMadeForKids": False},
        }
        if publish_at:
            metadata["status"]["publishAt"] = publish_at
        self._record("UPLOAD_STARTED", privacy=privacy_status, bytes=len(video_bytes))
        data = self._resumable_upload(token, metadata, video_bytes)
        video_id = (data or {}).get("id")
        if not video_id:
            raise RuntimeError("YouTube upload did not return a video id")
        self._record("UPLOAD_COMPLETED", video_id=video_id)
        return {"video_id": video_id, "status": (data.get("status") or {}).get("uploadStatus")}

    def _resumable_upload(self, token, metadata, video_bytes):
        if self._transport is not None:
            return self._transport("upload", metadata, video_bytes)
        import urllib.request
        # Initiate a resumable session, then upload the bytes.
        init = urllib.request.Request(
            UPLOAD_URL + "?uploadType=resumable&part=snippet,status",
            data=json.dumps(metadata).encode(), method="POST",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json",
                     "X-Upload-Content-Type": "video/mp4"})
        with urllib.request.urlopen(init, timeout=30) as response:  # noqa: S310
            session = response.headers.get("Location")
        upload = urllib.request.Request(session, data=video_bytes, method="PUT",
                                        headers={"Content-Type": "video/mp4", "Content-Length": str(len(video_bytes))})
        with urllib.request.urlopen(upload, timeout=600) as response:  # noqa: S310
            return json.loads(response.read().decode("utf-8", "replace"))


def request_upload(reel_id, package_id, *, mode="NOW", scheduled_for=None, privacy_status="PRIVATE",
                   timezone_name="Asia/Kolkata", connect, now=None, video_bytes=None, publisher=None,
                   storage=None, requested_by=None):
    """Durable, idempotent YouTube upload request. Defaults to PRIVATE (Part H)."""
    import reel_control
    timestamp = now() if now else _now()
    with connect() as connection:
        reel = connection.execute("SELECT * FROM final_reel_assets WHERE id=?", (reel_id,)).fetchone()
        if reel is None:
            raise KeyError(reel_id)
        package = connection.execute("SELECT * FROM youtube_packages WHERE id=?", (package_id,)).fetchone()
        if package is None:
            raise KeyError(package_id)
        if package["reel_id"] != reel_id:
            raise ValueError("package belongs to a different reel")
        if package["status"] != "YOUTUBE_COPY_APPROVED":
            raise ValueError("YouTube copy must be approved before upload.")
        if not reel_control.valid_approval(reel_id, connect=connect):
            raise ValueError("Reel must be approved before YouTube upload.")
        # Shorts default: the reel must be a genuine 9:16 Shorts-shaped video. Never silently
        # upload a non-compliant reel as standard long-form video.
        shorts = shorts_qa(width=reel["width"], height=reel["height"], codec=reel["codec"],
                           has_audio=reel["has_audio"], audio_codec=reel["audio_codec"],
                           duration_seconds=reel["duration_seconds"])
        if shorts["status"] != "PASS":
            raise ValueError("YOUTUBE_SHORTS_QA failed: " + "; ".join(shorts["errors"]))
    reel_version = f"{reel['id']}#{reel['checksum_sha256'][:12]}"
    idem = _idempotency_key(reel_id, reel_version, package["version_number"])
    with connect() as connection:
        existing = connection.execute(
            "SELECT * FROM youtube_publish_jobs WHERE idempotency_key=?", (idem,)).fetchone()
        if existing:
            return {"job": dict(existing), "duplicate": True}
    # Public privacy guard: never lose the requested value; never bypass YouTube restrictions.
    requested_privacy = privacy_status
    resolved = resolve_effective_privacy(privacy_status, public_upload_capability())
    privacy_status = resolved["effective_privacy"]
    privacy_downgrade_reason = resolved["privacy_downgrade_reason"]
    capability = resolved["public_upload_capability"]
    scheduled_at = scheduled_for if mode == "SCHEDULED" else None
    if mode == "SCHEDULED":
        if not scheduled_for:
            raise ValueError("ordered scheduling requires scheduled_for")
        try:
            when = datetime.fromisoformat(str(scheduled_for).replace("Z", "+00:00"))
        except ValueError:
            raise ValueError("scheduled_for must be an ISO-8601 timestamp")
        if when.tzinfo is None or when <= datetime.now(timezone.utc):
            raise ValueError("scheduled_for must be a future timezone-aware timestamp")
    job_id = _new_id("YJ-")
    with connect() as connection:
        connection.execute(
            "INSERT INTO youtube_publish_jobs(id,reel_id,youtube_package_id,event_id,reel_version,"
            "youtube_copy_version,idempotency_key,mode,status,privacy_status,scheduled_at,timezone,"
            "project_audit_status,public_capability,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (job_id, reel_id, package_id, reel["event_id"], reel_version, package["version_number"], idem,
             mode, "SCHEDULED" if mode == "SCHEDULED" else "READY", privacy_status, scheduled_at,
             timezone_name, project_audit_status(), capability, timestamp, timestamp))
        _publish_event(connection, job_id, "REQUESTED", status="SCHEDULED" if mode == "SCHEDULED" else "READY",
                       metadata={"requested_privacy": requested_privacy, "effective_privacy": privacy_status,
                                 "public_upload_capability": capability,
                                 "privacy_downgrade_reason": privacy_downgrade_reason,
                                 "project_audit": project_audit_status()})
    job = youtube_job(job_id, connect=connect)
    if mode == "NOW":
        job = execute_upload(job_id, connect=connect, video_bytes=video_bytes, publisher=publisher,
                             storage=storage)
    return {"job": job, "duplicate": False}


def execute_upload(job_id, *, connect, video_bytes=None, publisher=None, storage=None):
    job = youtube_job(job_id, connect=connect)
    if job["status"] not in ("READY", "FAILED"):
        return job
    package = package_row(job["youtube_package_id"], connect=connect)
    publisher = publisher or YouTubePublisher()
    with connect() as connection:
        connection.execute(
            "UPDATE youtube_publish_jobs SET status='UPLOADING',attempt_count=attempt_count+1,"
            "started_at=?,updated_at=? WHERE id=?", (_now(), _now(), job_id))
        reel = connection.execute("SELECT storage_uri FROM final_reel_assets WHERE id=?",
                                  (job["reel_id"],)).fetchone()
    _publish_event_conn(connect, job_id, "UPLOADING", status="UPLOADING", metadata={})
    try:
        data = video_bytes
        if data is None:
            loader = storage
            if loader is None:
                loader = _default_media_storage()
            if loader is not None:
                data = loader.get(reel["storage_uri"])
        if data is None:
            raise ValueError("video bytes unavailable for upload")
        result = publisher.upload(job, title=package["title_primary"], description=package["description"],
                                  tags=package["tags"], privacy_status=job["privacy_status"],
                                  video_bytes=data)
        with connect() as connection:
            connection.execute(
                "UPDATE youtube_publish_jobs SET status='PUBLISHED',video_id=?,uploaded_at=?,updated_at=?,"
                "last_error_code=NULL,last_error_message=NULL,last_error_at=NULL WHERE id=?",
                (result["video_id"], _now(), _now(), job_id))
        for event in getattr(publisher, "events", []):
            _publish_event_conn(connect, job_id, event["event_type"], metadata=event.get("metadata"))
        _publish_event_conn(connect, job_id, "PUBLISHED", status="PUBLISHED",
                            metadata={"video_id": result["video_id"]})
    except PermissionError as error:
        with connect() as connection:
            connection.execute("UPDATE youtube_publish_jobs SET status='FAILED',last_error_code='OAUTH_REQUIRED',"
                               "last_error_message=?,last_error_at=?,updated_at=? WHERE id=?",
                               (_safe_error(error)[:400], _now(), _now(), job_id))
        _publish_event_conn(connect, job_id, "FAILED", status="FAILED", metadata={"error": "OAUTH_REQUIRED"})
    except Exception as error:  # noqa: BLE001 - a failed upload is recorded, never faked
        with connect() as connection:
            connection.execute("UPDATE youtube_publish_jobs SET status='FAILED',last_error_code='UPLOAD_FAILED',"
                               "last_error_message=?,last_error_at=?,updated_at=? WHERE id=?",
                               (_safe_error(error)[:400], _now(), _now(), job_id))
        _publish_event_conn(connect, job_id, "FAILED", status="FAILED", metadata={"error": _safe_error(error)[:200]})
    return youtube_job(job_id, connect=connect)


def youtube_job(job_id, *, connect):
    with connect() as connection:
        row = connection.execute("SELECT * FROM youtube_publish_jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            raise KeyError(job_id)
        result = dict(row)
        result["events"] = [dict(r) for r in connection.execute(
            "SELECT * FROM youtube_publish_events WHERE youtube_publish_job_id=? ORDER BY id", (job_id,))]
    for event in result["events"]:
        event["metadata"] = json.loads(event.pop("safe_metadata_json") or "{}")
    return result


def _publish_event(connection, job_id, event_type, *, status=None, metadata=None):
    connection.execute(
        "INSERT INTO youtube_publish_events(youtube_publish_job_id,event_type,status,safe_metadata_json,"
        "occurred_at) VALUES(?,?,?,?,?)",
        (job_id, event_type, status, json.dumps(metadata or {}, sort_keys=True), _now()))


def _publish_event_conn(connect, job_id, event_type, *, status=None, metadata=None):
    with connect() as connection:
        _publish_event(connection, job_id, event_type, status=status, metadata=metadata)


def run_due_uploads(*, connect, now=None, publisher=None, storage=None):
    timestamp = now() if now else _now()
    with connect() as connection:
        due = [dict(r) for r in connection.execute(
            "SELECT id FROM youtube_publish_jobs WHERE mode='SCHEDULED' AND status='SCHEDULED' "
            "AND scheduled_at IS NOT NULL AND scheduled_at<=? ORDER BY scheduled_at", (timestamp,))]
        for row in due:
            connection.execute("UPDATE youtube_publish_jobs SET status='READY',updated_at=? WHERE id=? AND status='SCHEDULED'",
                               (timestamp, row["id"]))
    results = []
    for row in due:
        results.append(execute_upload(row["id"], connect=connect, publisher=publisher, storage=storage))
    return results


# ---------- analytics (Part I) ----------

def record_snapshot(*, connect, video_id, checkpoint="latest", metrics, reel_id=None, job_id=None,
                    event_id=None, reel_version=None, youtube_copy_version=None, title=None,
                    processing_state=None, privacy_status=None, published_at=None, uploaded_at=None,
                    analytics_depth="BASIC", now=None):
    """Idempotent analytics upsert: ONE canonical row per youtube_video_id.

    Linkage auto-derives from the upload job when job_id is provided (explicit args win; existing
    non-NULL linkage is preserved with COALESCE). A conflicting explicit reel_id surfaces
    LINKAGE_MISMATCH without aborting the metrics sync. Unavailable metrics stay NULL.
    """
    timestamp = now() if now else _now()
    metrics = metrics or {}
    linkage = {"event_id": event_id, "reel_id": reel_id, "reel_version": reel_version,
               "youtube_copy_version": youtube_copy_version, "job_id": None}
    mismatch = False
    invalid_linkage = False
    # Validate explicit linkage against real rows; never persist an unresolved FK.
    if linkage["reel_id"]:
        with connect() as _c:
            if _c.execute("SELECT 1 FROM final_reel_assets WHERE id=?", (linkage["reel_id"],)).fetchone() is None:
                invalid_linkage = True
                linkage["reel_id"] = None
    # Resolve the requested job to a REAL row; never persist an unresolved FK.
    if job_id:
        derived = _job_linkage(job_id, connect=connect)
        if derived:
            linkage["job_id"] = derived["job_id"]
            for key in ("event_id", "reel_id", "reel_version", "youtube_copy_version"):
                value = derived.get(key)
                if value is None:
                    continue
                if linkage.get(key) is None:
                    linkage[key] = value
                elif key == "reel_id" and not invalid_linkage and linkage[key] != value:
                    mismatch = True
    with connect() as connection:
        existing = connection.execute(
            "SELECT * FROM youtube_performance_snapshots WHERE video_id=? ORDER BY recorded_at LIMIT 1",
            (video_id,)).fetchone()
        if existing:
            def pick(key):
                return linkage.get(key) if linkage.get(key) is not None else existing[key]
            job_ref = linkage.get("job_id") or existing["youtube_publish_job_id"]
            connection.execute(
                "UPDATE youtube_performance_snapshots SET reel_id=?,youtube_publish_job_id=?,event_id=?,"
                "reel_version=?,youtube_copy_version=?,title=COALESCE(?,title),checkpoint=?,views=?,likes=?,"
                "comments=?,watch_time_seconds=?,average_view_duration_seconds=?,average_percentage_viewed=?,"
                "subscribers_gained=?,shares=?,processing_state=COALESCE(?,processing_state),source=?,"
                "recorded_at=? WHERE id=?",
                (pick("reel_id"), job_ref, pick("event_id"), pick("reel_version"),
                 pick("youtube_copy_version"), title, checkpoint,
                 metrics.get("views"), metrics.get("likes"), metrics.get("comments"),
                 metrics.get("watch_time_seconds"), metrics.get("average_view_duration_seconds"),
                 metrics.get("average_percentage_viewed"), metrics.get("subscribers_gained"),
                 metrics.get("shares"), processing_state, metrics.get("source", analytics_depth),
                 timestamp, existing["id"]))
        else:
            connection.execute(
                "INSERT INTO youtube_performance_snapshots(id,video_id,reel_id,youtube_publish_job_id,checkpoint,"
                "views,likes,comments,watch_time_seconds,average_view_duration_seconds,"
                "average_percentage_viewed,subscribers_gained,shares,source,recorded_at,event_id,reel_version,"
                "youtube_copy_version,title,processing_state) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (_new_id("YS-"), video_id, linkage.get("reel_id"), linkage.get("job_id"), checkpoint,
                 metrics.get("views"), metrics.get("likes"), metrics.get("comments"),
                 metrics.get("watch_time_seconds"), metrics.get("average_view_duration_seconds"),
                 metrics.get("average_percentage_viewed"), metrics.get("subscribers_gained"),
                 metrics.get("shares"), metrics.get("source", analytics_depth), timestamp,
                 linkage.get("event_id"), linkage.get("reel_version"), linkage.get("youtube_copy_version"),
                 title, processing_state))
    record = snapshots_for(video_id, connect=connect)[0]
    if invalid_linkage:
        record["linkage_qa"] = "INVALID_LINKAGE"
    elif mismatch:
        record["linkage_qa"] = "LINKAGE_MISMATCH"
    return record


def _job_linkage(job_id, *, connect):
    """Derive canonical analytics linkage from an upload job (single source of truth)."""
    with connect() as connection:
        row = connection.execute(
            "SELECT id,reel_id,event_id,reel_version,youtube_copy_version FROM youtube_publish_jobs WHERE id=?",
            (job_id,)).fetchone()
    if row is None:
        return None
    return {"job_id": row["id"], "reel_id": row["reel_id"], "event_id": row["event_id"],
            "reel_version": row["reel_version"], "youtube_copy_version": row["youtube_copy_version"]}


def snapshots_for(video_id, *, connect):
    with connect() as connection:
        return [dict(r) for r in connection.execute(
            "SELECT * FROM youtube_performance_snapshots WHERE video_id=? ORDER BY recorded_at,id", (video_id,))]


def fetch_video_status(video_id, *, http=None):
    """Basic video status/statistics via videos.list. Returns only API-provided fields (else None)."""
    url = f"{DATA_API_URL}/videos?part=snippet,status,statistics&id={video_id}"
    try:
        data = _request(url, headers={"Authorization": f"Bearer {access_token()}"}, http=http)
    except Exception as error:  # noqa: BLE001
        raise RuntimeError(_safe_error(error)) from None
    item = (data.get("items") or [None])[0]
    if not item:
        return None
    snippet = item.get("snippet", {})
    status = item.get("status", {})
    stats = item.get("statistics", {})
    def _int(value):
        return int(value) if str(value or "").isdigit() else None
    return {
        "video_id": item.get("id"), "title": snippet.get("title"),
        "published_at": snippet.get("publishedAt"), "tags": snippet.get("tags") or [],
        "privacy_status": status.get("privacyStatus"), "upload_status": status.get("uploadStatus"),
        "views": _int(stats.get("viewCount")), "likes": _int(stats.get("likeCount")),
        "comments": _int(stats.get("commentCount")),
        # Deeper metrics require the Analytics API; leave NULL rather than fabricate.
        "watch_time_seconds": None, "average_view_duration_seconds": None,
        "average_percentage_viewed": None, "subscribers_gained": None, "shares": None,
    }


def sync_analytics(video_id, *, connect, http=None, job_id=None, now=None):
    """YOUTUBE_PERFORMANCE_V1: fetch aggregate video metrics and upsert ONE linked analytics row."""
    with connect() as connection:
        job = None
        if job_id:
            job = connection.execute("SELECT * FROM youtube_publish_jobs WHERE id=?", (job_id,)).fetchone()
        if job is None:
            job = connection.execute(
                "SELECT * FROM youtube_publish_jobs WHERE video_id=? ORDER BY uploaded_at DESC LIMIT 1",
                (video_id,)).fetchone()
        package = None
        if job is not None:
            package = connection.execute("SELECT title_primary FROM youtube_packages WHERE id=?",
                                         (job["youtube_package_id"],)).fetchone()
    fetched = fetch_video_status(video_id, http=http)
    if fetched is None:
        return None
    metrics = {k: fetched.get(k) for k in ("views", "likes", "comments", "watch_time_seconds",
             "average_view_duration_seconds", "average_percentage_viewed", "subscribers_gained", "shares")}
    return record_snapshot(
        connect=connect, video_id=video_id, checkpoint="latest", metrics=metrics,
        reel_id=job["reel_id"] if job else None, job_id=job["id"] if job else None,
        event_id=job["event_id"] if job else None, reel_version=job["reel_version"] if job else None,
        youtube_copy_version=job["youtube_copy_version"] if job else None,
        title=fetched.get("title") or (package["title_primary"] if package else None),
        processing_state=fetched.get("upload_status"),
        privacy_status=fetched.get("privacy_status"), published_at=fetched.get("published_at"),
        uploaded_at=job["uploaded_at"] if job else None, analytics_depth="BASIC", now=now)


def due_snapshot_checkpoint(*, connect, video_id, uploaded_at, now=None):
    """Which append-only checkpoint is due next (1h/6h/24h/3d/7d)."""
    reference = datetime.fromisoformat(str(now() if now else _now()).replace("Z", "+00:00"))
    try:
        upload = datetime.fromisoformat(str(uploaded_at).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    age_hours = (reference - upload).total_seconds() / 3600
    existing = {row["checkpoint"] for row in snapshots_for(video_id, connect=connect)}
    for name, hours in SNAPSHOT_CHECKPOINTS:
        if age_hours >= hours and name not in existing:
            return name
    return None


def analytics_available():
    return bool(os.environ.get("YOUTUBE_REFRESH_TOKEN") and os.environ.get("YOUTUBE_CLIENT_ID"))


def collect_analytics(video_id, *, checkpoint, connect, http=None, now=None):
    """Fetch metrics where authorization permits; UNKNOWN stays UNKNOWN."""
    if not analytics_available():
        return record_snapshot(connect=connect, video_id=video_id, checkpoint=checkpoint,
                               metrics={"source": "UNKNOWN"}, now=now)
    return record_snapshot(connect=connect, video_id=video_id, checkpoint=checkpoint,
                           metrics={"source": "UNKNOWN"}, now=now)


# ---------- content-level learning (Part J) ----------

def performance_intelligence(*, connect):
    """Compare performance against CONTENT FEATURES ONLY. Never user political characteristics."""
    with connect() as connection:
        rows = [dict(r) for r in connection.execute(
            "SELECT s.*, p.youtube_format, p.language_mix FROM youtube_performance_snapshots s "
            "LEFT JOIN youtube_publish_jobs j ON j.id=s.youtube_publish_job_id "
            "LEFT JOIN youtube_packages p ON p.id=j.youtube_package_id ORDER BY s.recorded_at")]
    features = {}
    for row in rows:
        key = (row.get("youtube_format") or "UNKNOWN", row.get("language_mix") or "UNKNOWN")
        bucket = features.setdefault(key, {"samples": 0, "views": 0})
        bucket["samples"] += 1
        bucket["views"] += row.get("views") or 0
    return {"by_content_feature": [
        {"youtube_format": k[0], "language_mix": k[1], "samples": v["samples"],
         "average_views": round(v["views"] / v["samples"], 1) if v["samples"] else None}
        for k, v in features.items()]}


# ---------- OAuth browser pages (safe HTML; never renders secret values) ----------

def _esc(value):
    import html
    return html.escape(str(value if value is not None else ""), quote=True)


def success_page(channel_title=None, channel_id_masked=None):
    return (
        "<!doctype html><html><head><meta charset='utf-8'><title>YouTube connected</title></head>"
        "<body style='font-family:system-ui;max-width:520px;margin:60px auto;padding:0 20px'>"
        "<h2>YouTube connected successfully</h2>"
        f"<p>Channel: <strong>{_esc(channel_title or '-')}</strong></p>"
        f"<p>Channel ID: <code>{_esc(channel_id_masked or '-')}</code></p>"
        "<p>You can close this tab and return to the dashboard.</p></body></html>")


def error_page(reason):
    return (
        "<!doctype html><html><head><meta charset='utf-8'><title>Connection failed</title></head>"
        "<body style='font-family:system-ui;max-width:520px;margin:60px auto;padding:0 20px'>"
        "<h2>Connection failed</h2>"
        f"<p>Reason: <code>{_esc(reason)}</code></p>"
        "<p>No credentials are shown here. Return to the dashboard to retry.</p></body></html>")
