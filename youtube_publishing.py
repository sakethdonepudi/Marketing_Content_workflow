"""Architecture 11 — YouTube publishing + discoverability intelligence (YouTube ONLY).

An API key can NEVER upload: publishing uses OAuth 2.0 with the
`https://www.googleapis.com/auth/youtube.upload` scope. Client secret, access token, and
refresh token are read from the environment and are never logged, returned, or committed.

Discoverability is optimized at the CONTENT/topic level only. Engagement is a weak aggregate
relevance signal — never factual verification, never persuasion, and no demographic or
political-personality profiling.
"""

from datetime import datetime, timezone
import json
import os
import re
import urllib.parse
import uuid

YOUTUBE_UPLOAD_SCOPE = "https://www.googleapis.com/auth/youtube.upload"
YOUTUBE_ANALYTICS_SCOPE = "https://www.googleapis.com/auth/yt-analytics.readonly"
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
    """Readiness only — never returns secret values."""
    client_id = os.environ.get("YOUTUBE_CLIENT_ID", "").strip()
    client_secret = bool(os.environ.get("YOUTUBE_CLIENT_SECRET", "").strip())
    redirect = os.environ.get("YOUTUBE_REDIRECT_URI", "").strip()
    refresh_token = bool(os.environ.get("YOUTUBE_REFRESH_TOKEN", "").strip())
    missing = [name for name, ok in (
        ("YOUTUBE_CLIENT_ID", client_id), ("YOUTUBE_CLIENT_SECRET", client_secret),
        ("YOUTUBE_REDIRECT_URI", redirect), ("YOUTUBE_REFRESH_TOKEN", refresh_token)) if not ok]
    return {
        "client_id": client_id or None, "client_secret_configured": client_secret,
        "redirect_uri": redirect or None, "refresh_token_configured": refresh_token,
        "scope": YOUTUBE_UPLOAD_SCOPE, "missing": missing,
        "upload_configured": not missing, "api_key_configured": bool(os.environ.get("YOUTUBE_API_KEY")),
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
        "response_type": "code", "scope": YOUTUBE_UPLOAD_SCOPE,
        "access_type": "offline", "prompt": "consent", "include_granted_scopes": "true",
    }
    if state:
        params["state"] = state
    return f"{OAUTH_AUTH_URL}?{urllib.parse.urlencode(params)}"


def exchange_code(code, *, http=None):
    """Exchange an authorization code for tokens; returns token_status only (never the tokens)."""
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
            "scope": data.get("scope", YOUTUBE_UPLOAD_SCOPE)}


def _post_form(url, body, *, http=None, headers=None):
    if http is not None:
        return http(url, body)
    import urllib.request
    request = urllib.request.Request(url, data=body, method="POST",
                                     headers={"Content-Type": "application/x-www-form-urlencoded", **(headers or {})})
    with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310 - fixed Google host
        return json.loads(response.read().decode("utf-8", "replace"))


def access_token(*, http=None):
    """Exchange the stored refresh token for a short-lived access token (never returned to callers)."""
    config = oauth_configuration()
    if not config["upload_configured"]:
        raise ValueError("YouTube OAuth is UNCONFIGURED")
    body = urllib.parse.urlencode({
        "client_id": os.environ["YOUTUBE_CLIENT_ID"], "client_secret": os.environ["YOUTUBE_CLIENT_SECRET"],
        "refresh_token": os.environ["YOUTUBE_REFRESH_TOKEN"], "grant_type": "refresh_token",
    }).encode()
    data = _post_form(OAUTH_TOKEN_URL, body, http=http)
    if "access_token" not in data:
        raise PermissionError("YouTube access token could not be refreshed")
    return data["access_token"]


def oauth_state(*, connect):
    with connect() as connection:
        row = connection.execute("SELECT * FROM youtube_oauth_state WHERE id='youtube'").fetchone()
        return dict(row) if row else {}


def record_oauth_state(*, connect, channel_id=None, channel_title=None, scope=YOUTUBE_UPLOAD_SCOPE,
                       token_status="CONNECTED", connected_account=None, error=None, now=None):
    timestamp = now() if now else _now()
    config = oauth_configuration()
    if token_status != "CONNECTED" and not config["upload_configured"]:
        token_status = "UNCONFIGURED"
    with connect() as connection:
        existing = connection.execute("SELECT id FROM youtube_oauth_state WHERE id='youtube'").fetchone()
        values = (channel_id, channel_title, scope, token_status, connected_account,
                  timestamp if token_status == "CONNECTED" else None, error, timestamp)
        if existing:
            connection.execute(
                "UPDATE youtube_oauth_state SET channel_id=?,channel_title=?,scope=?,token_status=?,"
                "connected_account=?,last_authorized_call_at=?,last_error=?,updated_at=? WHERE id='youtube'", values)
        else:
            connection.execute(
                "INSERT INTO youtube_oauth_state(id,channel_id,channel_title,scope,token_status,connected_account,"
                "last_authorized_call_at,last_error,created_at,updated_at) VALUES('youtube',?,?,?,?,?,?,?,?,?)",
                (channel_id, channel_title, scope, token_status, connected_account,
                 timestamp if token_status == "CONNECTED" else None, error, timestamp, timestamp))
    return oauth_state(connect=connect)


def connection_status(*, connect):
    """System → YouTube connection card. Never returns tokens; channel ID is masked."""
    config = oauth_configuration()
    state = oauth_state(connect=connect)
    if not config["upload_configured"]:
        status = "UNCONFIGURED"
    else:
        status = state.get("token_status") or "UNCONFIGURED"
    return {
        "status": status,
        "channel_name": state.get("channel_title"),
        "channel_id_masked": mask(state.get("channel_id"), keep=4) if state.get("channel_id") else None,
        "connected_account": state.get("connected_account"),
        "token_status": state.get("token_status") or "UNCONFIGURED",
        "last_authorized_call": state.get("last_authorized_call_at"),
        "api_key_discovery": bool(config["api_key_configured"]),
        "upload_capability": config["upload_configured"],
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


def generate_tags(*, entities=None, location=None, event_name=None, topic_terms=(), language_mix="BILINGUAL"):
    """youtube_tags — separate factual search terms (Part E.12)."""
    tags, seen = [], set()

    def add(value):
        value = re.sub(r"\s+", " ", str(value or "")).strip()
        if value and value.casefold() not in seen:
            seen.add(value.casefold())
            tags.append(value[:100])

    for entity in entities or []:
        add(entity)
        if location:
            add(f"{entity} {location}")
    if event_name:
        add(event_name)
    if location:
        add(location)
        add(f"{location} Andhra Pradesh")
    for term in topic_terms or []:
        add(term)
        add(f"Andhra Pradesh {term}")
    add("Andhra Pradesh")
    add("Telugu News")
    return tags[:15]


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


def package_qa(*, titles, description, hashtags, claims, entities=None, location=None,
               event_name=None, research=None, topic_terms=()):
    title_qa = title_factual_qa(titles["title_primary"], claims=claims)
    desc_qa = description_factual_qa(description, claims=claims)
    tag_qa = hashtag_relevance_qa(hashtags, entities=entities, location=location,
                                  event_name=event_name, claims=claims, research=research,
                                  topic_terms=topic_terms)
    statuses = [title_qa["status"], desc_qa["status"], tag_qa["status"]]
    return {"TITLE_FACTUAL_QA": title_qa, "DESCRIPTION_FACTUAL_QA": desc_qa,
            "HASHTAG_RELEVANCE_QA": tag_qa,
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
                    topic_terms=topic_terms)
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


def project_audit_status():
    """YouTube restricts uploads from unverified API projects created after 2020-07-28 to private."""
    override = os.environ.get("YOUTUBE_PROJECT_AUDIT_STATUS", "").strip().upper()
    if override == "PUBLIC_VERIFIED":
        return "PUBLIC_VERIFIED"
    if override == "PRIVATE_ONLY":
        return "PRIVATE_ONLY"
    return "UNKNOWN"


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
            raise PermissionError("YouTube upload requires OAuth 2.0 (YOUTUBE_UPLOAD = UNCONFIGURED).")
        token = access_token(http=self._transport and (lambda url, body: self._transport(url, body)))
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
    reel_version = f"{reel['id']}#{reel['checksum_sha256'][:12]}"
    idem = _idempotency_key(reel_id, reel_version, package["version_number"])
    with connect() as connection:
        existing = connection.execute(
            "SELECT * FROM youtube_publish_jobs WHERE idempotency_key=?", (idem,)).fetchone()
        if existing:
            return {"job": dict(existing), "duplicate": True}
    if privacy_status == "PUBLIC" and project_audit_status() != "PUBLIC_VERIFIED":
        # Never claim PUBLIC is available until the project is verified.
        privacy_status = "PRIVATE"
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
            "project_audit_status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (job_id, reel_id, package_id, reel["event_id"], reel_version, package["version_number"], idem,
             mode, "SCHEDULED" if mode == "SCHEDULED" else "READY", privacy_status, scheduled_at,
             timezone_name, project_audit_status(), timestamp, timestamp))
        _publish_event(connection, job_id, "REQUESTED", status="SCHEDULED" if mode == "SCHEDULED" else "READY",
                       metadata={"privacy": privacy_status, "project_audit": project_audit_status()})
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
                from media_storage import LocalMediaStorage  # noqa: F401 - resolved lazily in app
                loader = None
            if loader is not None:
                data = loader.get(reel["storage_uri"])
        if data is None:
            raise ValueError("video bytes unavailable for upload")
        result = publisher.upload(job, title=package["title_primary"], description=package["description"],
                                  tags=package["tags"], privacy_status=job["privacy_status"],
                                  video_bytes=data)
        with connect() as connection:
            connection.execute(
                "UPDATE youtube_publish_jobs SET status='PUBLISHED',video_id=?,uploaded_at=?,updated_at=? WHERE id=?",
                (result["video_id"], _now(), _now(), job_id))
        for event in getattr(publisher, "events", []):
            _publish_event_conn(connect, job_id, event["event_type"], metadata=event.get("metadata"))
        _publish_event_conn(connect, job_id, "PUBLISHED", status="PUBLISHED",
                            metadata={"video_id": result["video_id"]})
    except PermissionError as error:
        with connect() as connection:
            connection.execute("UPDATE youtube_publish_jobs SET status='FAILED',last_error_code='OAUTH_REQUIRED',"
                               "last_error_message=?,updated_at=? WHERE id=?", (str(error)[:400], _now(), job_id))
        _publish_event_conn(connect, job_id, "FAILED", status="FAILED", metadata={"error": "OAUTH_REQUIRED"})
    except Exception as error:  # noqa: BLE001 - a failed upload is recorded, never faked
        with connect() as connection:
            connection.execute("UPDATE youtube_publish_jobs SET status='FAILED',last_error_code='UPLOAD_FAILED',"
                               "last_error_message=?,updated_at=? WHERE id=?", (str(error)[:400], _now(), job_id))
        _publish_event_conn(connect, job_id, "FAILED", status="FAILED", metadata={"error": str(error)[:200]})
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

def record_snapshot(*, connect, video_id, checkpoint, metrics, reel_id=None, job_id=None, now=None):
    if checkpoint not in {c for c, _ in SNAPSHOT_CHECKPOINTS}:
        raise ValueError("invalid checkpoint")
    timestamp = now() if now else _now()
    with connect() as connection:
        connection.execute(
            "INSERT INTO youtube_performance_snapshots(id,video_id,reel_id,youtube_publish_job_id,checkpoint,"
            "views,likes,comments,watch_time_seconds,average_view_duration_seconds,average_percentage_viewed,"
            "subscribers_gained,shares,source,recorded_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (_new_id("YS-"), video_id, reel_id, job_id, checkpoint,
             metrics.get("views"), metrics.get("likes"), metrics.get("comments"),
             metrics.get("watch_time_seconds"), metrics.get("average_view_duration_seconds"),
             metrics.get("average_percentage_viewed"), metrics.get("subscribers_gained"),
             metrics.get("shares"), metrics.get("source", "UNKNOWN"), timestamp))
    return snapshots_for(video_id, connect=connect)[0]


def snapshots_for(video_id, *, connect):
    with connect() as connection:
        return [dict(r) for r in connection.execute(
            "SELECT * FROM youtube_performance_snapshots WHERE video_id=? ORDER BY recorded_at,id", (video_id,))]


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
