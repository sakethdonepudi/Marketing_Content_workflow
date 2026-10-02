"""Architecture 09C — YouTube live discovery (YOUTUBE_DISCOVERY_V1).

YouTube is a DISCOVERY SIGNAL, never verified evidence. This module queries the official
YouTube Data API v3 when `YOUTUBE_API_KEY` is configured, normalizes videos (videos, Shorts,
live/recent streams) into the same discovery-signal shape as every other family, and feeds
them into the existing `fast_discovery` clustering — it never invents a YouTube-specific
clustering path and never verifies anything.

Fail closed: with no key the source is UNCONFIGURED and no scraping happens. Popularity is a
routing/priority signal only, never a factual claim. Media is discovery-only: rights_status
stays UNKNOWN so it can never become production B-roll.

Off by default (`YOUTUBE_DISCOVERY_ENABLED=0`).
"""

from datetime import datetime, timezone
import json
import os
import re

API_BASE = "https://www.googleapis.com/youtube/v3"
# Official API quota is 10,000 units/day; search.list costs 100, videos.list costs 1.
SEARCH_COST = 100
VIDEO_COST = 1
DEFAULT_DAILY_QUOTA = 10_000
# 09D: discovery budget + adaptive polling modes.
DEFAULT_DISCOVERY_BUDGET = 7_000
CONSERVATION_THRESHOLD = 0.70
HIGH_PRIORITY_THRESHOLD = 0.85
STOP_THRESHOLD = 0.95
DEFAULT_INTERVAL_SECONDS = 900
NORMAL_INTERVAL_SECONDS = 900
ACTIVE_EVENT_INTERVAL_SECONDS = 300
QUIET_INTERVAL_SECONDS = 1800
CONSERVATION_INTERVAL_SECONDS = 3600
QUERY_COOLDOWN_MINUTES = 60
VIDEO_CACHE_MAX_AGE_HOURS = 24

# ---------- query strategy ----------

# Hard-coded rotating buckets (section 3); cycle cursor rotates through them.
CORE_ENTITY_QUERIES = (
    "Chandrababu Naidu", "Pawan Kalyan", "Nara Lokesh", "Andhra Pradesh CM",
    "AP Government", "AP Ministers",
)
LOCATION_QUERIES = (
    "Vijayawada", "Visakhapatnam", "Tirupati", "Guntur", "Nellore", "Ongole",
    "Madanapalle", "Kurnool", "Kadapa", "Anantapur", "Prakasam",
)
TOPIC_QUERIES = (
    "inauguration", "launch", "speech", "farmers", "project", "investment",
    "agriculture", "horticulture", "industry", "welfare", "cabinet", "meeting", "review",
)
# Static Telugu vocabulary made searchable; dynamic entities expand at runtime.
TELUGU_QUERY_MAP = {
    "chandrababu naidu": "చంద్రబాబు నాయుడు ఆంధ్రప్రదేశ్",
    "pawan kalyan": "పవన్ కల్యాణ్ ఆంధ్రప్రదేశ్",
    "nara lokesh": "నారా లోకేష్ ఆంధ్రప్రదేశ్",
    "andhra pradesh cm": "ఆంధ్రప్రదేశ్ ముఖ్యమంత్రి",
}
QUERY_BUCKETS = ("core_entities", "locations", "topic_bursts", "dynamic_entities")

# ---------- channel priority ----------

# Official government/party/institution channels.
OFFICIAL_CHANNELS = {
    "tdp": "OFFICIAL", "telugu desam party": "OFFICIAL", "pib": "OFFICIAL",
    "press information bureau": "OFFICIAL", "mygov": "OFFICIAL",
    "andhra pradesh government": "OFFICIAL", "cm office ap": "OFFICIAL",
    "cmo andhra pradesh": "OFFICIAL", "janasena party": "OFFICIAL",
}
# Established Telugu news broadcasters.
NEWS_CHANNELS = {
    "ntv telugu": "NEWS", "tv9 telugu": "NEWS", "abn andhra jyothy": "NEWS",
    "sakshi": "NEWS", "eenadu": "NEWS", "samayam telugu": "NEWS",
    "news18 telugu": "NEWS", "10tv": "NEWS", "99tv": "NEWS", "v6 news": "NEWS",
    "hmtv": "NEWS", "raj news": "NEWS", "etv andhra pradesh": "NEWS",
    "etv telugu": "NEWS", "abn": "NEWS", "tv5": "NEWS", "bharat today": "NEWS",
    "sakshi tv": "NEWS", "n tv telugu": "NEWS", "ntv": "NEWS", "tv9": "NEWS",
}
# Other established media outlets (wire/print/publishers).
KNOWN_MEDIA_CHANNELS = {
    "the hindu": "KNOWN_MEDIA", "the news minute": "KNOWN_MEDIA",
    "the federal": "KNOWN_MEDIA", "india today": "KNOWN_MEDIA",
    "pti": "KNOWN_MEDIA", "aninews": "KNOWN_MEDIA", "deccanherald": "KNOWN_MEDIA",
    "thehindu": "KNOWN_MEDIA", "reuters": "KNOWN_MEDIA",
}

_YT_VIDEO_ID = re.compile(r"(?:v=|youtu\.be/|/shorts/|/embed/|/live/)([A-Za-z0-9_-]{11})(?![A-Za-z0-9_-])")
_TELUGU = re.compile(r"[\u0C00-\u0C7F]")


def _now():
    return datetime.now(timezone.utc).isoformat()


def youtube_enabled():
    return os.environ.get("YOUTUBE_DISCOVERY_ENABLED", "0") == "1"


def api_key():
    return os.environ.get("YOUTUBE_API_KEY") or None


def daily_quota_limit():
    try:
        return int(os.environ.get("YOUTUBE_DAILY_QUOTA_LIMIT", os.environ.get("YOUTUBE_DAILY_QUOTA", DEFAULT_DAILY_QUOTA)))
    except (TypeError, ValueError):
        return DEFAULT_DAILY_QUOTA


def discovery_budget():
    try:
        return int(os.environ.get("YOUTUBE_DISCOVERY_QUOTA_BUDGET", DEFAULT_DISCOVERY_BUDGET))
    except (TypeError, ValueError):
        return DEFAULT_DISCOVERY_BUDGET


def discovery_interval_seconds():
    try:
        return max(60, int(os.environ.get("YOUTUBE_DISCOVERY_INTERVAL_SECONDS", DEFAULT_INTERVAL_SECONDS)))
    except (TypeError, ValueError):
        return DEFAULT_INTERVAL_SECONDS


def video_id_for(url_or_id):
    """Extract an 11-char video ID from a URL, or accept a bare ID."""
    text = str(url_or_id or "")
    match = _YT_VIDEO_ID.search(text)
    if match:
        return match.group(1)
    if re.fullmatch(r"[A-Za-z0-9_-]{11}", text):
        return text
    return None


def canonical_video_url(url_or_id):
    video_id = video_id_for(url_or_id)
    return f"https://www.youtube.com/watch?v={video_id}" if video_id else str(url_or_id or "")


# ---------- query rotation (bounded budget) ----------

def recent_entities_for_queries(*, connect, hours=24, limit=6):
    """Dynamic expansion: entities seen across all sources in the last 24h (section 4).

    Reads discovery_signals so a same-day entity surfaced by NTV/PIB is also searched on
    YouTube, which improves cross-source clustering.
    """
    cutoff = datetime.now(timezone.utc).timestamp() - hours * 3600
    entities = []
    with connect() as connection:
        rows = connection.execute(
            "SELECT entities_json FROM discovery_signals WHERE created_at>=? ORDER BY created_at DESC LIMIT 400",
            (datetime.fromtimestamp(cutoff, timezone.utc).isoformat(),)).fetchall()
    seen = set()
    for row in rows:
        for entity in json.loads(row[0] or "[]"):
            if entity not in seen:
                seen.add(entity)
                entities.append(entity)
            if len(entities) >= limit:
                return entities
    return entities


def dynamic_queries_for_entities(entities, *, limit=8):
    """Entity -> English + Telugu + location variants (cross-source clustering aid)."""
    from fast_discovery import expand_query
    from live_discovery import DISTRICT_VOCAB
    queries, seen = [], set()
    for entity in entities or []:
        variants = list(expand_query(entity))
        for location in DISTRICT_VOCAB[:4]:
            variants.append(f"{entity} {location}")
        for variant in variants:
            if variant and variant not in seen:
                seen.add(variant)
                queries.append(variant)
            if len(queries) >= limit:
                return queries
    return queries


def build_query_buckets(*, connect, now=None):
    """The rotating buckets; dynamic entities come from the last 24h of signals."""
    entities = recent_entities_for_queries(connect=connect)
    return {
        "core_entities": list(CORE_ENTITY_QUERIES),
        "locations": list(LOCATION_QUERIES),
        "topic_bursts": list(TOPIC_QUERIES),
        "dynamic_entities": dynamic_queries_for_entities(entities),
    }


# ---------- priority-ordered, coalesced query selection (09D) ----------

def _entity_family_evidence(*, connect, hours=2):
    """Entities appearing in Telugu news/wire sources in the last `hours` (priority 1)."""
    cutoff = datetime.fromtimestamp(
        datetime.now(timezone.utc).timestamp() - hours * 3600, timezone.utc).isoformat()
    counts = {}
    with connect() as connection:
        rows = connection.execute(
            "SELECT e.value FROM discovery_signals s, json_each(s.entities_json) e "
            "WHERE s.created_at>=? AND s.source_id NOT IN ('youtube')", (cutoff,)).fetchall()
    for row in rows:
        counts[row[0]] = counts.get(row[0], 0) + 1
    return sorted(counts, key=lambda e: (-counts[e], e))


def _active_candidate_entities(*, connect):
    """Salient entities on open candidates (priority 2), newest first."""
    import fast_discovery
    entities = []
    for candidate in fast_discovery.list_candidates(connect=connect):
        if candidate.get("state") not in ("CANDIDATE", "HANDED_OFF"):
            continue
        for entity in candidate.get("entities") or []:
            if entity not in entities:
                entities.append(entity)
    return entities


def coalesce_entities(entities, *, location=None, limit=4):
    """Combine co-occurring entities into ONE search expression (section 5).

    "Chandrababu Naidu" + "Pawan Kalyan" + "Madanapalle" -> one query, not three.
    """
    parts = [e for e in (entities or []) if e]
    if location:
        parts.append(location)
    # Prefer proper-noun-ish entities, keep insertion order, dedupe case-insensitively.
    seen, picked = set(), []
    for part in parts:
        key = part.casefold()
        if key in seen:
            continue
        seen.add(key)
        picked.append(part)
        if len(picked) >= limit:
            break
    return " ".join(picked)


def _query_key(text):
    return re.sub(r"[^a-z0-9\u0C00-\u0C7F]+", " ", str(text or "").casefold()).strip()


def query_cache_lookup(query_text, *, connect):
    """Return the cached row for a query, or None (section 6)."""
    with connect() as connection:
        row = connection.execute("SELECT * FROM youtube_query_cache WHERE query_key=?",
                                 (_query_key(query_text),)).fetchone()
        return dict(row) if row else None


def query_cache_store(query_text, video_ids, *, connect, reason=None, now=None):
    timestamp = now() if now else _now()
    with connect() as connection:
        connection.execute(
            "INSERT OR REPLACE INTO youtube_query_cache(query_text,query_key,executed_at,result_video_ids,"
            "result_count,cycle_reason) VALUES(?,?,?,?,?,?)",
            (query_text, _query_key(query_text), timestamp, json.dumps(list(video_ids or [])),
             len(video_ids or []), reason))
    return query_cache_lookup(query_text, connect=connect)


def query_in_cooldown(query_text, *, connect, minutes=60, now=None):
    """True when the same query ran within the cooldown window (section 6)."""
    cached = query_cache_lookup(query_text, connect=connect)
    if not cached:
        return False
    try:
        executed = datetime.fromisoformat(str(cached["executed_at"]).replace("Z", "+00:00"))
        reference = datetime.fromisoformat(str(now() if now else _now()).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return False
    return (reference - executed).total_seconds() < minutes * 60


def pending_cross_source_trigger(*, connect):
    """A high-value external signal queued as the next YouTube query (section 4)."""
    state = checkpoint(connect=connect)
    trigger = state.get("pending_trigger")
    return trigger or None


def queue_cross_source_trigger(query_text, *, connect, now=None):
    """Queue an on-demand query from a high-value NTV/TV9/Sakshi/PIB/APGov signal."""
    return update_checkpoint(connect=connect, now=now, pending_trigger=query_text)


def high_value_trigger_from_signals(signals, *, connect, hours=2, now=None):
    """Build a cross-source trigger query from a recent high-value news signal (section 4).

    Only the strongest recent non-YouTube signal forms a query; it replaces the next generic
    query (never adds unbounded searches).
    """
    import fast_discovery
    best, best_score = None, 0.0
    for signal in signals or []:
        if signal.get("source_id") == "youtube" or signal.get("source_family", "").startswith("YouTube"):
            continue
        text = f"{signal.get('title','')} {signal.get('text','')}"
        entities = fast_discovery.extract_entities(text)
        location = signal.get("location") or fast_discovery._location_from(entities)
        score = len(entities) + (1 if location else 0)
        if entities and score > best_score:
            best, best_score = (entities, location), score
    if not best:
        return None
    entities, location = best
    return coalesce_entities(entities, location=location)


def select_query(*, connect, now=None, burst=False):
    """Pick the SINGLE query for this cycle, priority-ordered and coalesced (sections 2-5).

    Priority: (1) entities in news within 2h, (2) open-candidate entities, (3) burst entities,
    (4) core figures, (5) district rotation. A pending cross-source trigger wins first.
    Returns (query_text, reason).
    """
    trigger = pending_cross_source_trigger(connect=connect)
    if trigger:
        return trigger, "cross_source_trigger"
    news_entities = _entity_family_evidence(connect=connect, hours=2)
    if news_entities:
        return coalesce_entities(news_entities), "recent_news_entities"
    candidate_entities = _active_candidate_entities(connect=connect)
    if candidate_entities:
        return coalesce_entities(candidate_entities), "active_candidate"
    if burst:
        entities = recent_entities_for_queries(connect=connect, limit=4)
        if entities:
            return coalesce_entities(entities), "burst"
    buckets = build_query_buckets(connect=connect, now=now)
    cursor = checkpoint(connect=connect)
    index = int(cursor.get("bucket_cursor") or 0) % len(QUERY_BUCKETS)
    bucket = QUERY_BUCKETS[index]
    pool = buckets.get(bucket) or []
    offset = int(cursor.get("query_offset") or 0)
    picked = pool[offset] if offset < len(pool) else (pool[0] if pool else None)
    new_offset = 0 if (offset + 1) >= len(pool) else offset + 1
    update_checkpoint(connect=connect, now=now, bucket_cursor=(index + 1) % len(QUERY_BUCKETS),
                      query_offset=new_offset, last_bucket=bucket)
    return picked, bucket


def next_queries(*, connect, bucket_size=1, limit=4, now=None):
    """Return (reason, [query...], cursor) for one cycle.

    Default is ONE search per cycle (09D section 2); bucket_size=2 only for HIGH_PRIORITY burst
    mode. Never repeats the same query and never clears the pending cross-source trigger here —
    the cycle clears it only after the trigger query is actually executed.
    """
    queries, seen, reason = [], set(), None
    for _ in range(max(1, bucket_size)):
        query, reason = select_query(connect=connect, now=now)
        if not query or query.casefold() in seen:
            break
        if query_in_cooldown(query, connect=connect, now=now):
            # Cooling query: fall through to the rotation for a fresh one.
            update_checkpoint(connect=connect, now=now, query_offset=1)
            query, reason = select_query(connect=connect, now=now)
            if not query or query.casefold() in seen:
                break
        seen.add(query.casefold())
        queries.append(query)
        update_checkpoint(connect=connect, now=now, last_bucket=reason)
    cursor = checkpoint(connect=connect)
    return (reason or "rotation"), queries, cursor


# ---------- quota + checkpoint state ----------

def checkpoint(*, connect):
    with connect() as connection:
        row = connection.execute("SELECT * FROM youtube_discovery_state WHERE id='youtube'").fetchone()
        return dict(row) if row else {}


def update_checkpoint(*, connect, now=None, **fields):
    timestamp = now() if now else _now()
    allowed = ("bucket_cursor", "query_offset", "last_bucket", "last_queries", "quota_date",
               "quota_used", "queries_today", "videos_today", "searches_today", "last_success_at",
               "errors_today", "recent_video_ids", "last_error", "mode", "next_poll_at",
               "last_polled_at", "paused_reason", "pending_trigger")
    with connect() as connection:
        existing = connection.execute("SELECT id FROM youtube_discovery_state WHERE id='youtube'").fetchone()
        if not existing:
            connection.execute("INSERT INTO youtube_discovery_state(id,created_at) VALUES('youtube',?)", (timestamp,))
        for key, value in fields.items():
            if key in allowed:
                if key in ("last_queries", "recent_video_ids") and not isinstance(value, str):
                    value = json.dumps(value or [])
                connection.execute(f"UPDATE youtube_discovery_state SET {key}=? WHERE id='youtube'", (value,))
    return checkpoint(connect=connect)


def _roll_quota_if_new_day(*, connect, now=None):
    timestamp = now() if now else _now()
    today = timestamp[:10]
    state = checkpoint(connect=connect)
    if state.get("quota_date") != today:
        return update_checkpoint(connect=connect, now=now, quota_date=today, quota_used=0,
                                 queries_today=0, videos_today=0, searches_today=0, errors_today=0)
    return state


def quota_status(*, connect):
    """Quota/health bookkeeping for the UI: used, limit, remaining, discovery budget, mode."""
    state = _roll_quota_if_new_day(connect=connect)
    used = int(state.get("quota_used") or 0)
    limit = daily_quota_limit()
    budget = discovery_budget()
    fraction = (used / budget) if budget else 0.0
    mode, paused_reason = quota_mode(used=used, budget=budget)
    return {"quota_used": used, "quota_limit": limit, "quota_remaining": max(0, limit - used),
            "quota_exhausted": used >= limit, "queries_today": int(state.get("queries_today") or 0),
            "videos_today": int(state.get("videos_today") or 0), "searches_today": int(state.get("searches_today") or 0),
            "errors_today": int(state.get("errors_today") or 0), "quota_date": state.get("quota_date"),
            "discovery_budget": budget, "budget_used": used, "budget_fraction": round(fraction, 4),
            "mode": mode, "paused_reason": paused_reason}


def quota_mode(*, used, budget):
    """NORMAL / CONSERVATION / HIGH_PRIORITY_ONLY / STOPPED by discovery-budget fraction."""
    fraction = (used / budget) if budget else 0.0
    if fraction >= STOP_THRESHOLD:
        return "STOPPED", "discovery budget at or above 95%"
    if fraction >= HIGH_PRIORITY_THRESHOLD:
        return "HIGH_PRIORITY_ONLY", "discovery budget at or above 85%"
    if fraction >= CONSERVATION_THRESHOLD:
        return "CONSERVATION", "discovery budget at or above 70%"
    return "NORMAL", None


def current_mode(*, connect):
    """The effective poll mode: quota mode unless an ACTIVE_EVENT overrides it."""
    quota = quota_status(connect=connect)
    if quota["mode"] != "NORMAL":
        return quota["mode"]
    state = checkpoint(connect=connect)
    if state.get("pending_trigger"):
        return "ACTIVE_EVENT"
    import fast_discovery
    open_candidates = [c for c in fast_discovery.list_candidates(connect=connect)
                       if c.get("state") in ("CANDIDATE", "HANDED_OFF")
                       and c.get("confidence") in ("VERY_HIGH", "HIGH")]
    return "ACTIVE_EVENT" if open_candidates else "NORMAL"


def poll_interval_for_mode(mode):
    return {"NORMAL": NORMAL_INTERVAL_SECONDS, "QUIET": QUIET_INTERVAL_SECONDS,
            "ACTIVE_EVENT": ACTIVE_EVENT_INTERVAL_SECONDS,
            "CONSERVATION": CONSERVATION_INTERVAL_SECONDS,
            "HIGH_PRIORITY_ONLY": CONSERVATION_INTERVAL_SECONDS,
            "STOPPED": None}.get(mode, NORMAL_INTERVAL_SECONDS)


def next_poll_status(*, connect, now=None):
    """Next-poll timing + current mode for the UI (section 10)."""
    state = checkpoint(connect=connect)
    quota = quota_status(connect=connect)
    mode = current_mode(connect=connect)
    interval = poll_interval_for_mode(mode)
    last = state.get("last_polled_at")
    next_at = state.get("next_poll_at")
    if interval is not None and last:
        try:
            next_at = datetime.fromtimestamp(
                datetime.fromisoformat(str(last).replace("Z", "+00:00")).timestamp() + interval,
                timezone.utc).isoformat()
        except (TypeError, ValueError):
            next_at = state.get("next_poll_at")
    return {"mode": mode, "interval_seconds": interval, "next_poll_at": next_at,
            "last_polled_at": last, "stops_search": mode == "STOPPED",
            "budget_fraction": quota["budget_fraction"], "quota_mode": quota["mode"]}


def _should_search(*, connect):
    """Whether automatic YouTube search is allowed given the budget (section 8)."""
    quota = quota_status(connect=connect)
    if quota["mode"] == "STOPPED":
        return False, quota["paused_reason"]
    if quota["mode"] == "HIGH_PRIORITY_ONLY":
        # Only dynamic/high-priority queries are allowed.
        return True, "high-priority-only"
    return True, None


def _record_api_usage(*, connect, searches=0, videos=0, errors=0, quota=None, now=None):
    state = _roll_quota_if_new_day(connect=connect, now=now)
    used = int(state.get("quota_used") or 0) + SEARCH_COST * searches + VIDEO_COST * videos
    return update_checkpoint(
        connect=connect, now=now,
        quota_used=used if quota is None else quota,
        searches_today=int(state.get("searches_today") or 0) + searches,
        queries_today=int(state.get("queries_today") or 0) + searches,
        videos_today=int(state.get("videos_today") or 0) + videos,
        errors_today=int(state.get("errors_today") or 0) + errors)


def youtube_health(*, connect):
    """HEALTHY / DEGRADED / QUOTA_EXHAUSTED / UNCONFIGURED / FAILED."""
    state = checkpoint(connect=connect)
    quota = quota_status(connect=connect)
    if not api_key():
        status = "UNCONFIGURED"
    elif quota["quota_exhausted"]:
        status = "QUOTA_EXHAUSTED"
    elif state.get("last_error") and int(state.get("errors_today") or 0) >= 3:
        status = "FAILED"
    elif int(state.get("errors_today") or 0) > 0:
        status = "DEGRADED"
    elif state.get("last_success_at") is None and state.get("searches_today"):
        status = "DEGRADED"
    else:
        status = "HEALTHY"
    poll = next_poll_status(connect=connect)
    with connect() as connection:
        row = connection.execute("SELECT average_latency_ms,results_last_24h FROM discovery_sources WHERE id='youtube'").fetchone()
    avg_latency = row["average_latency_ms"] if row else None
    return {
        "status": status, "configured": bool(api_key()), "enabled": youtube_enabled(),
        "quota_used": quota["quota_used"], "quota_limit": quota["quota_limit"],
        "quota_remaining": quota["quota_remaining"], "queries_today": quota["queries_today"],
        "videos_today": quota["videos_today"], "searches_today": quota["searches_today"],
        "errors_today": quota["errors_today"], "last_success": state.get("last_success_at"),
        "last_error": state.get("last_error"), "last_bucket": state.get("last_bucket"),
        "last_queries": json.loads(state.get("last_queries") or "[]"),
        "recent_video_ids": json.loads(state.get("recent_video_ids") or "[]"),
        "average_latency_ms": avg_latency,
        # 09D: budget + adaptive polling.
        "discovery_budget": quota["discovery_budget"], "budget_used": quota["budget_used"],
        "budget_fraction": quota["budget_fraction"], "mode": poll["mode"],
        "interval_seconds": poll["interval_seconds"], "next_poll_at": poll["next_poll_at"],
        "last_polled": poll["last_polled_at"], "paused_reason": quota["paused_reason"],
        "projected_daily_usage": projected_daily_usage(connect=connect),
    }


def projected_daily_usage(*, connect, now=None):
    """Extrapolate today's quota use to a full day based on elapsed time (section 10)."""
    quota = quota_status(connect=connect)
    reference = datetime.fromisoformat(str(now() if now else _now()).replace("Z", "+00:00"))
    elapsed = max(1.0, reference.timestamp() - reference.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
    return int(round(quota["quota_used"] * (86400 / elapsed)))


def channel_class(channel_title):
    """OFFICIAL / NEWS / KNOWN_MEDIA / UNKNOWN_CHANNEL."""
    key = re.sub(r"[^a-z0-9 ]+", " ", str(channel_title or "").casefold()).strip()
    for mapping in (OFFICIAL_CHANNELS, NEWS_CHANNELS, KNOWN_MEDIA_CHANNELS):
        for token, cls in mapping.items():
            if token and token in key:
                return cls
    return "UNKNOWN_CHANNEL"


def recency_band(published_at, *, now=None):
    """0-2h VERY_HIGH, 2-6h HIGH, 6-24h NORMAL, else LOW."""
    from fast_discovery import recency_weight
    try:
        published = datetime.fromisoformat(str(published_at).replace("Z", "+00:00"))
        reference = datetime.fromisoformat(str(now() if now else _now()).replace("Z", "+00:00"))
        age = max(0.0, (reference - published).total_seconds() / 3600)
    except (TypeError, ValueError):
        return "LOW"
    return recency_weight(age)


# ---------- official API client ----------

def _http_json(url, *, timeout=20, http=None):
    """GET JSON via urllib; `http(url) -> dict` is injectable for tests."""
    if http is not None:
        return http(url)
    import urllib.request
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - fixed googleapis host
        return json.loads(response.read().decode("utf-8", "replace"))


def _api_get(path, params, *, http=None, timeout=20):
    from urllib.parse import urlencode
    query = urlencode(params)
    return _http_json(f"{API_BASE}/{path}?{query}", timeout=timeout, http=http)


def youtube_search(query, *, key=None, published_after=None, max_results=10, region="IN",
                   relevance_language="te", order="date", http=None):
    """One search.list call. Returns (items, error). Costs 100 quota units."""
    params = {
        "key": key, "part": "snippet", "q": query, "type": "video", "order": order,
        "maxResults": max(1, min(50, max_results)), "regionCode": region,
        "relevanceLanguage": relevance_language,
    }
    if published_after:
        params["publishedAfter"] = published_after
    try:
        data = _api_get("search", params, http=http)
    except Exception as error:  # noqa: BLE001 - one query never crashes a cycle
        return [], str(error)
    items = []
    for entry in data.get("items", []):
        snippet = entry.get("snippet", {})
        video_id = (entry.get("id") or {}).get("videoId")
        if not video_id:
            continue
        items.append({
            "video_id": video_id, "query": query,
            "title": snippet.get("title") or "",
            "description": snippet.get("description") or "",
            "channel_id": snippet.get("channelId"),
            "channel_title": snippet.get("channelTitle") or "",
            "published_at": snippet.get("publishedAt"),
            "url": f"https://www.youtube.com/watch?v={video_id}",
            "raw_metadata": {"kind": "youtube#searchResult", "query": query},
        })
    return items, None


def youtube_videos(video_ids, *, key=None, http=None):
    """videos.list for statistics/contentDetails (1 unit per requested ID).

    `connect`-free: the cycle decides which IDs to request via the cache (section 7).
    """
    if not video_ids:
        return {}, None
    params = {"key": key, "part": "snippet,statistics,contentDetails,liveStreamingDetails",
              "id": ",".join(video_ids[:50])}
    try:
        data = _api_get("videos", params, http=http)
    except Exception as error:  # noqa: BLE001
        return {}, str(error)
    result = {}
    for entry in data.get("items", []):
        video_id = entry.get("id")
        if isinstance(video_id, dict):  # defensive: a search-shaped payload never breaks videos.list
            video_id = video_id.get("videoId")
        if not video_id:
            continue
        snippet = entry.get("snippet", {})
        stats = entry.get("statistics", {})
        live = entry.get("liveStreamingDetails") or {}
        result[video_id] = {
            "title": snippet.get("title") or "",
            "description": snippet.get("description") or "",
            "channel_id": snippet.get("channelId"),
            "channel_title": snippet.get("channelTitle") or "",
            "published_at": snippet.get("publishedAt"),
            "tags": snippet.get("tags") or [],
            "duration": (entry.get("contentDetails") or {}).get("duration"),
            "live_broadcast": (snippet).get("liveBroadcastContent"),
            "was_live": bool(live),
            "statistics": {
                "view_count": int(stats["viewCount"]) if str(stats.get("viewCount", "")).isdigit() else None,
                "like_count": int(stats["likeCount"]) if str(stats.get("likeCount", "")).isdigit() else None,
                "comment_count": int(stats["commentCount"]) if str(stats.get("commentCount", "")).isdigit() else None,
            },
        }
    return result, None


# ---------- video metadata cache (09D section 7) ----------

def cached_video(video_id, *, connect, max_age_hours=VIDEO_CACHE_MAX_AGE_HOURS, now=None):
    """Return cached video metadata when fresh, else None."""
    with connect() as connection:
        row = connection.execute("SELECT * FROM youtube_video_cache WHERE video_id=?", (video_id,)).fetchone()
    if not row:
        return None
    row = dict(row)
    try:
        fetched = datetime.fromisoformat(str(row["fetched_at"]).replace("Z", "+00:00"))
        reference = datetime.fromisoformat(str(now() if now else _now()).replace("Z", "+00:00"))
        if (reference - fetched).total_seconds() > max_age_hours * 3600:
            return None
    except (TypeError, ValueError):
        return None
    return {"title": row["title"], "description": row["description"], "channel_id": row["channel_id"],
            "channel_title": row["channel_title"] or "", "published_at": row["published_at"],
            "tags": json.loads(row["tags_json"] or "[]"), "duration": row["duration"],
            "live_broadcast": row["live_broadcast"], "was_live": bool(row["was_live"]),
            "statistics": {"view_count": row["view_count"], "like_count": row["like_count"],
                           "comment_count": row["comment_count"]}}


def cache_videos(entries, *, connect, now=None):
    """Persist fetched video metadata for reuse (never re-charges videos.list for it)."""
    timestamp = now() if now else _now()
    with connect() as connection:
        for video_id, entry in (entries or {}).items():
            stats = entry.get("statistics") or {}
            connection.execute(
                "INSERT OR REPLACE INTO youtube_video_cache(video_id,title,description,channel_id,channel_title,"
                "published_at,tags_json,duration,live_broadcast,was_live,view_count,like_count,comment_count,fetched_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (video_id, entry.get("title"), entry.get("description"), entry.get("channel_id"),
                 entry.get("channel_title"), entry.get("published_at"), json.dumps(entry.get("tags") or []),
                 entry.get("duration"), entry.get("live_broadcast"), int(bool(entry.get("was_live"))),
                 stats.get("view_count"), stats.get("like_count"), stats.get("comment_count"), timestamp))


def resolve_videos(video_ids, *, connect, key=None, http=None, now=None):
    """Return metadata for IDs, fetching ONLY uncached ones via videos.list.

    Returns (metadata_by_id, fetched_count, error). `fetched_count` is the quota-relevant
    number of IDs actually requested from the API.
    """
    resolved, missing = {}, []
    for video_id in dict.fromkeys(video_ids or []):
        cached = cached_video(video_id, connect=connect, now=now)
        if cached is not None:
            resolved[video_id] = cached
        else:
            missing.append(video_id)
    error = None
    if missing:
        fetched, error = youtube_videos(missing, key=key, http=http)
        cache_videos(fetched, connect=connect, now=now)
        resolved.update(fetched)
    return resolved, len(missing), error


# ---------- normalization ----------

def search_span_for_recency(hours=24, *, now=None):
    """publishedAfter ISO-8601 (RFC3339) for the same-day window."""
    reference = datetime.fromisoformat(str(now() if now else _now()).replace("Z", "+00:00"))
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=timezone.utc)
    published = reference.timestamp() - hours * 3600
    return datetime.fromtimestamp(published, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _short_or_video(duration, url):
    """Shorts are <= 60s (ISO-8601 PT#S / PT#M#S); keep the same normalized shape either way."""
    if not duration:
        return "YouTube"
    match = re.fullmatch(r"PT(?:(\d+)M)?(?:(\d+)S)?", str(duration))
    if match:
        seconds = int(match.group(1) or 0) * 60 + int(match.group(2) or 0)
        return "YouTube Shorts" if seconds and seconds <= 60 else "YouTube"
    return "YouTube"


def normalize_video(entry, *, source_id="youtube", fetch_run_id=None, now=None):
    """Normalize one YouTube item into the same discovery-signal shape as every family."""
    from fast_discovery import extract_entities, _location_from
    timestamp = now() if now else _now()
    stats = entry.get("statistics") or {}
    title = str(entry.get("title") or "").strip()[:300]
    description = str(entry.get("description") or "")[:4000]
    channel = str(entry.get("channel_title") or "").strip()
    video_id = entry.get("video_id") or video_id_for(entry.get("url"))
    family = _short_or_video(entry.get("duration"), entry.get("url"))
    cls = channel_class(channel)
    text = " ".join(part for part in (title, description, channel) if part)
    entities = extract_entities(text)
    # Location: explicit field, else the title/description (Madanapalle lives in metadata).
    location = entry.get("location") or _location_from(entities)
    engagement = {
        "views": stats.get("view_count") or 0,
        "likes": stats.get("like_count"),
        "comments": stats.get("comment_count"),
        "subscribers": entry.get("subscriber_count"),
    }
    is_primary = cls == "OFFICIAL"
    return {
        "title": title, "text": description or title, "url": entry.get("url"),
        "published_at": entry.get("published_at"), "first_seen_at": timestamp,
        "entities": entities, "location": location,
        "language": entry.get("language") or ("te" if _TELUGU.search(text) else "en"),
        "source_family": family, "publisher": channel or "YouTube",
        "engagement_metrics": engagement, "is_primary": is_primary,
        "raw_metadata": {
            "provider": "youtube", "video_id": video_id, "channel_id": entry.get("channel_id"),
            "channel_class": cls, "recency_band": recency_band(entry.get("published_at"), now=now),
            "live_broadcast": entry.get("live_broadcast"), "was_live": entry.get("was_live"),
            "duration": entry.get("duration"), "tags": entry.get("tags") or [],
            # Discovery-only: never production B-roll without explicit reuse terms.
            "rights_status": "UNKNOWN",
            "source_id": source_id, "fetch_run_id": fetch_run_id,
        },
    }


def is_recent(published_at, *, hours=24, now=None):
    band = recency_band(published_at, now=now)
    return band != "LOW"


# ---------- dedupe ----------

def dedupe_videos(items):
    """Drop same video ID / canonical URL / near-identical repost title. Returns (unique, details).

    Different channels re-uploading the same clip stay distinct signals (so the corroboration
    engine can see them) but are flagged `is_repost` so mirrored copies never inflate the
    independent-family count.
    """
    seen_ids, seen_urls, seen_titles, unique, dropped = {}, {}, {}, [], 0
    for item in items:
        video_id = item.get("video_id") or video_id_for(item.get("url"))
        url = canonical_video_url(item.get("url") or video_id)
        title_key = re.sub(r"[^a-z0-9\u0C00-\u0C7F]+", " ", str(item.get("title") or "").casefold()).strip()
        channel = str(item.get("channel_title") or "").casefold().strip()
        if (url and url in seen_urls) or (video_id and video_id in seen_ids):
            dropped += 1
            continue
        if title_key and title_key in seen_titles and seen_titles[title_key] != channel:
            # Same clip reposted by another channel: keep, mark as repost.
            item = {**item, "is_repost": True, "repost_of": seen_titles[title_key]}
        if url:
            seen_urls[url] = item
        if video_id:
            seen_ids[video_id] = item
        if title_key:
            seen_titles.setdefault(title_key, channel)
        unique.append(item)
    return unique, {"raw": len(items), "unique": len(unique), "duplicates": dropped,
                    "reposts": sum(1 for i in unique if i.get("is_repost"))}


# ---------- cycle ----------

def run_youtube_cycle(*, connect, now=None, http=None, queries=None, bucket_size=1,
                      max_results=10, handoff_fn=None, hours=24):
    """One bounded, quota-efficient YouTube discovery cycle (09D).

    Default: ONE search.list (100 units) via the priority-ordered, coalesced query selector.
    `bucket_size=2` is only used in HIGH_PRIORITY burst mode. videos.list runs only for IDs
    not already cached. Search is skipped entirely once the discovery budget is exhausted.
    """
    import fast_discovery
    timestamp = now() if now else _now()
    result = {"status": None, "bucket": None, "queries_executed": [], "videos_fetched": 0,
              "same_day_videos": 0, "signals_produced": 0, "signals": [], "dedupe": {},
              "candidates_created": 0, "candidates": [], "handoffs": 0, "cross_source_clusters": [],
              "quota_used": 0, "searches": 0, "errors": [], "video_ids": [], "handoff_details": [],
              "query_cached": False, "query_reused": False, "video_ids_fetched": 0,
              "video_ids_cached": 0, "mode": None}
    if not youtube_enabled() or not api_key():
        result["status"] = "UNCONFIGURED"
        return result
    _roll_quota_if_new_day(connect=connect, now=now)
    allowed, reason = _should_search(connect=connect)
    if not allowed:
        result["status"] = "QUOTA_EXHAUSTED"
        result["mode"] = "STOPPED"
        result["errors"].append(reason or "budget stopped")
        return result
    result["mode"] = current_mode(connect=connect)
    if queries is None:
        # HIGH_PRIORITY_ONLY (or an explicit burst) permits 2 searches; every other mode: 1.
        effective_size = 2 if result["mode"] == "HIGH_PRIORITY_ONLY" else max(1, bucket_size)
        bucket, queries, _ = next_queries(connect=connect, bucket_size=effective_size, now=now)
    else:
        bucket = "explicit"
    result["bucket"] = bucket
    started = datetime.now(timezone.utc)
    published_after = search_span_for_recency(hours, now=now)
    key = api_key()

    raw, performed = [], 0
    for query in queries:
        if quota_status(connect=connect)["quota_remaining"] < SEARCH_COST:
            result["errors"].append("quota exhausted before query: " + query)
            break
        if query_in_cooldown(query, connect=connect, now=now):
            # Serve the cached result set within the cooldown (section 6).
            result["query_reused"] = True
            cached = query_cache_lookup(query, connect=connect) or {}
            ids = json.loads(cached.get("result_video_ids") or "[]")
            raw.extend({"video_id": vid, "title": "", "url": f"https://www.youtube.com/watch?v={vid}",
                        "query": query, "published_at": None} for vid in ids)
            result["queries_executed"].append(query)
            continue
        items, error = youtube_search(query, key=key, published_after=published_after,
                                      max_results=max_results, http=http)
        _record_api_usage(connect=connect, searches=1, errors=1 if error else 0, now=now)
        performed += 1
        result["queries_executed"].append(query)
        result["searches"] += 1
        if error:
            result["errors"].append(f"{query}: {error}")
            continue
        ids = [i["video_id"] for i in items]
        query_cache_store(query, ids, connect=connect, reason=bucket, now=now)
        result["query_cached"] = True
        raw.extend(items)
        if bucket == "cross_source_trigger":
            # Trigger consumed; clear it so it is not repeated forever.
            update_checkpoint(connect=connect, now=now, pending_trigger=None)
    # Fallback: no query executed (all cooling with no cache, or empty pool) -> district rotation.
    if not raw and performed == 0 and queries:
        fallback = LOCATION_QUERIES[0]
        items, error = youtube_search(fallback, key=key, published_after=published_after,
                                      max_results=max_results, http=http)
        _record_api_usage(connect=connect, searches=1, errors=1 if error else 0, now=now)
        result["queries_executed"].append(fallback)
        result["searches"] += 1
        if not error:
            query_cache_store(fallback, [i["video_id"] for i in items], connect=connect,
                              reason="fallback", now=now)
            raw.extend(items)
    raw = [item for item in raw if not item.get("published_at") or is_recent(item.get("published_at"), hours=hours, now=now)]

    video_ids = [i["video_id"] for i in raw]
    video_map, missing_count, video_error = resolve_videos(video_ids, connect=connect, key=key, http=http, now=now)
    result["video_ids_fetched"] = missing_count
    result["video_ids_cached"] = len(set(video_ids)) - missing_count
    if missing_count:
        # videos.list costs 1 unit per requested ID (not per returned entry).
        _record_api_usage(connect=connect, videos=missing_count, now=now)
    if video_error:
        result["errors"].append("videos.list: " + video_error)
    merged = []
    for item in raw:
        detail = video_map.get(item["video_id"], {})
        merged.append({**item, **{k: v for k, v in detail.items() if v not in (None, "", [])}})
    unique, dedupe_info = dedupe_videos(merged)
    result["dedupe"] = dedupe_info
    result["videos_fetched"] = len(merged)
    result["same_day_videos"] = len(unique)

    signals = [normalize_video(item, fetch_run_id=None, now=now) for item in unique]
    for signal, item in zip(signals, unique):
        signal["raw_metadata"]["is_repost"] = bool(item.get("is_repost"))
    result["signals"] = signals
    result["signals_produced"] = len(signals)
    result["video_ids"] = [s["raw_metadata"].get("video_id") for s in signals]

    failed = bool(result["errors"]) and result["searches"] > 0 and result["searches"] == len(result["errors"])
    _record_api_usage(connect=connect, quota=None, now=now)  # persist is handled per-call
    quota = quota_status(connect=connect)
    mode = current_mode(connect=connect)
    update_checkpoint(connect=connect, now=now,
                      last_success_at=None if failed else timestamp,
                      last_error="; ".join(result["errors"])[:400] or None,
                      recent_video_ids=result["video_ids"][:20],
                      last_queries=result["queries_executed"],
                      last_polled_at=timestamp, mode=mode,
                      paused_reason=quota.get("paused_reason"),
                      next_poll_at=_next_poll_timestamp(mode, now=now))
    # Refresh the shared discovery_sources row so the System table shows real YouTube values.
    latency_ms = round((datetime.now(timezone.utc) - started).total_seconds() * 1000, 1)
    _update_youtube_source_row(connect=connect, status="FAILED" if failed else "COMPLETED",
                               signals=len(signals), latency_ms=latency_ms, now=now)
    result["quota_used"] = quota["quota_used"]
    result["quota_remaining"] = quota["quota_remaining"]
    result["budget_used"] = quota["budget_used"]
    result["discovery_budget"] = quota["discovery_budget"]
    result["status"] = "FAILED" if failed else ("COMPLETED" if signals else "COMPLETED_EMPTY")
    result["latency_ms"] = latency_ms
    return result


def _next_poll_timestamp(mode, *, now=None):
    interval = poll_interval_for_mode(mode)
    if interval is None:
        return None
    reference = datetime.fromisoformat(str(now() if now else _now()).replace("Z", "+00:00"))
    return datetime.fromtimestamp(reference.timestamp() + interval, timezone.utc).isoformat()


def _update_youtube_source_row(*, connect, status, signals, latency_ms, now=None):
    """Keep discovery_sources healthy for YouTube using the real API cycle outcome."""
    import live_discovery
    timestamp = now() if now else _now()
    live_discovery.sync_sources(connect=connect, now=now)
    with connect() as connection:
        connection.execute(
            "INSERT OR IGNORE INTO discovery_sources(id,family,adapter,publisher,enabled,configured,"
            "poll_interval_seconds,adapter_type,parser_name,created_at) VALUES('youtube','YouTube',"
            "'discover_youtube','YouTube',1,1,600,'API','youtube_api',?)", (timestamp,))
        if status == "COMPLETED":
            row = connection.execute("SELECT average_latency_ms FROM discovery_sources WHERE id='youtube'").fetchone()
            avg = row["average_latency_ms"]
            avg = latency_ms if avg is None else (avg * 0.7 + latency_ms * 0.3)
            connection.execute(
                "UPDATE discovery_sources SET last_polled_at=?,last_success_at=?,last_error=NULL,"
                "consecutive_failures=0,average_latency_ms=?,results_last_24h=results_last_24h+? WHERE id='youtube'",
                (timestamp, timestamp, avg, signals))
        else:
            connection.execute(
                "UPDATE discovery_sources SET last_polled_at=?,last_error=?,consecutive_failures="
                "consecutive_failures+1 WHERE id='youtube'", (timestamp, "youtube cycle failed"))


def _attach_to_existing_candidate(signals, *, connect, now=None):
    """Attach corroborating signals to a matching open candidate; None when nothing matches.

    Match = same location AND at least one shared salient entity. Thresholds are unchanged:
    the candidate was already created by a qualifying bundle.
    """
    import fast_discovery
    timestamp = now() if now else _now()
    enriched = []
    for signal in signals:
        text = " ".join(str(signal.get(k) or "") for k in ("title", "text"))
        entities = fast_discovery.extract_entities(text)
        enriched.append({"signal": signal, "entities": set(entities),
                         "location": signal.get("location") or fast_discovery._location_from(entities)})
    with connect() as connection:
        rows = [dict(r) for r in connection.execute(
            "SELECT * FROM event_candidates WHERE state IN ('CANDIDATE','HANDED_OFF')")]
        for row in rows:
            existing_entities = set(json.loads(row.get("entities_json") or "[]"))
            existing_location = row.get("location")
            for item in enriched:
                if existing_location and item["location"] and existing_location.lower() != item["location"].lower():
                    continue
                if not (existing_entities & item["entities"]):
                    continue
                # Attach the fresh signals and refresh the family/count metadata.
                families = json.loads(row.get("source_families_json") or "[]")
                new_families = sorted(set(families) | {s["signal"]["source_family"] for s in enriched})
                all_entities = sorted(existing_entities | {e for s in enriched for e in s["entities"]})
                for entry in enriched:
                    connection.execute(
                        "INSERT INTO candidate_signals(id,candidate_id,source_family,url,title,text,published_at,"
                        "is_primary,entities_json,location,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                        ("CS-" + _random_hex(), row["id"], entry["signal"]["source_family"], entry["signal"].get("url"),
                         entry["signal"]["title"], entry["signal"].get("text") or "", entry["signal"].get("published_at"),
                         int(entry["signal"].get("is_primary") or 0),
                         json.dumps(sorted(entry["entities"])), entry["location"], timestamp))
                    connection.execute("UPDATE discovery_signals SET candidate_id=? WHERE url=?",
                                       (row["id"], entry["signal"].get("url")))
                connection.execute(
                    "UPDATE event_candidates SET source_families_json=?,entities_json=?,"
                    "source_count=source_count+?,independent_source_count=? WHERE id=?",
                    (json.dumps(new_families), json.dumps(all_entities), len(enriched), len(new_families), row["id"]))
                return fast_discovery.candidate(row["id"], connect=connect)
    return None


def ingest_signals(signals, *, connect, now=None, handoff_fn=None, adapters=None):
    """Persist normalized YouTube signals, cluster with fast_discovery, hand off to verification.

    Uses the shared path so YouTube never grows a bespoke clustering route.
    """
    import fast_discovery
    import live_discovery
    timestamp = now() if now else _now()
    outcome = {"candidates_created": 0, "candidates": [], "handoffs": 0,
               "cross_source_clusters": [], "handoff_details": []}
    if not signals:
        return outcome
    # Register the YouTube source row + a fetch run so signals keep provenance.
    live_discovery.sync_sources(connect=connect, now=now)
    with connect() as connection:
        connection.execute(
            "INSERT OR IGNORE INTO discovery_sources(id,family,adapter,publisher,enabled,configured,"
            "poll_interval_seconds,adapter_type,parser_name,created_at) VALUES('youtube','YouTube',"
            "'discover_youtube','YouTube',1,1,600,'API','youtube_api',?)", (timestamp,))
        fetch_run_id = "FR-" + _random_hex()
        connection.execute(
            "INSERT INTO discovery_fetch_runs(id,source_id,started_at,finished_at,status,signals_fetched,"
            "latency_ms) VALUES(?,?,?,?,?,?,?)",
            (fetch_run_id, "youtube", timestamp, timestamp, "COMPLETED", len(signals), 0))
    stored = []
    with connect() as connection:
        for signal in signals:
            signal = dict(signal)
            signal["source_id"] = "youtube"
            signal["fetch_run_id"] = fetch_run_id
            signal["raw_metadata"]["fetch_run_id"] = fetch_run_id
            canonical = signal.get("canonical_url") or live_discovery._canonical_url(signal.get("url"))
            content_hash = live_discovery._content_hash(signal["title"], signal["text"], signal["publisher"])
            try:
                connection.execute(
                    "INSERT INTO discovery_signals(id,source_id,fetch_run_id,source_family,publisher,title,text,"
                    "url,canonical_url,published_at,first_seen_at,entities_json,location,language,"
                    "engagement_metrics_json,raw_metadata_json,content_hash,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    ("DS-" + _random_hex(), "youtube", fetch_run_id, signal["source_family"], signal["publisher"],
                     signal["title"], signal["text"], signal["url"], canonical, signal["published_at"],
                     signal["first_seen_at"], live_discovery._json(signal["entities"]), signal["location"],
                     signal["language"], live_discovery._json(signal["engagement_metrics"]),
                     live_discovery._json(signal["raw_metadata"]), content_hash, timestamp))
                stored.append(signal)
            except Exception:  # noqa: BLE001 - duplicate hash already stored
                pass
    if not stored:
        return outcome
    # Corroboration-aware attach: if an existing candidate already matches this event
    # (shared salient entities + location), add the YouTube signals to it BEFORE thresholding.
    # This never weakens the threshold — the candidate already passed it; it just stops a
    # corroborating YouTube clip from spawning a duplicate "candidate of one".
    candidate = _attach_to_existing_candidate(stored, connect=connect, now=now)
    attached = candidate is not None
    if candidate is None:
        candidate = fast_discovery.discover(stored, connect=connect, now=now)
    if candidate:
        outcome["attached"] = attached
        outcome["candidates"].append(candidate["id"])
        if not attached:
            outcome["candidates_created"] = 1
        families = candidate.get("source_families") or []
        if len(families) > 1:
            outcome["cross_source_clusters"].append({"candidate_id": candidate["id"], "families": families})
        if handoff_fn:
            detail = handoff_fn(candidate["id"]) or {}
            if detail.get("handed_off"):
                outcome["handoffs"] += 1
            outcome["handoff_details"].append({"candidate_id": candidate["id"], **detail})
    return outcome


def _random_hex(n=12):
    import uuid
    return uuid.uuid4().hex[:n].upper()
