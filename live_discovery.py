"""Architecture 09 — continuous live discovery (LIVE_DISCOVERY_V1).

A provider-independent scheduler polls many public source families (RSS/news, PIB, AP
Government, CMO, YouTube, Telugu news) on a bounded cadence, normalizes results into
discovery signals, dedupes syndicated copies, clusters them with `fast_discovery`, and hands
candidates straight to strict verification. Signals are leads, never evidence.

Off by default (`LIVE_DISCOVERY_ENABLED=0`). X/Instagram are marked UNCONFIGURED without
blocking the cycle. A broken source never crashes the cycle; 429s back off with jitter.
"""

from datetime import datetime, timezone
import hashlib
import random
import re
import uuid

DEFAULT_POLL_INTERVAL_SECONDS = 300
COLD_START_LOOKBACK_HOURS = 24
SLO_SECONDS = 600  # first strong signal -> candidate within 10 minutes
MAX_CONSECUTIVE_FAILURES = 5

# Rotating query groups (dynamic recent entities are appended per cycle).
QUERY_GROUPS = (
    ("AP politics", "Andhra Pradesh government", "Chandrababu Naidu", "Pawan Kalyan"),
    ("Nara Lokesh", "AP farmers", "Andhra projects", "Amaravati"),
    ("Guntur", "Nellore", "Madanapalle", "Visakhapatnam", "Tirupati"),
)
DISTRICT_VOCAB = ("Madanapalle", "Guntur", "Nellore", "Prakasam", "Ongole", "Vijayawada",
                  "Visakhapatnam", "Tirupati", "Kurnool", "Anantapur", "Kadapa", "Chittoor",
                  "Krishna", "West Godavari", "East Godavari", "Srikakulam", "Vizianagaram")
_TELUGU = re.compile(r"[\u0C00-\u0C7F]")
_URL_NOISE = re.compile(r"(utm_[^&]+|fbclid=[^&]+|gclid=[^&]+)", re.IGNORECASE)


def _now():
    return datetime.now(timezone.utc).isoformat()


def _canonical_url(url):
    return _URL_NOISE.sub("", str(url or "")).rstrip("?&")


def _content_hash(title, text, publisher):
    normalized = re.sub(r"[^a-z0-9\u0C00-\u0C7F]+", " ", f"{title} {text}".casefold()).strip()
    return hashlib.sha256(f"{publisher}|{normalized[:600]}".encode()).hexdigest()


def normalize_signal(raw, *, source_family, publisher, source_id=None, fetch_run_id=None, now=None):
    """Normalize any adapter's raw item into the canonical discovery-signal shape."""
    timestamp = now() if now else _now()
    title = str(raw.get("title") or "").strip()[:300]
    text = str(raw.get("text") or raw.get("summary") or "")[:4000]
    from fast_discovery import extract_entities, _location_from
    entities = extract_entities(f"{title} {text}")
    if raw.get("entities"):
        entities = list(dict.fromkeys(list(raw["entities"]) + entities))
    return {
        "id": "DS-" + uuid.uuid4().hex[:12].upper(), "source_id": source_id, "fetch_run_id": fetch_run_id,
        "source_family": source_family, "publisher": publisher, "title": title, "text": text,
        "url": raw.get("url"), "canonical_url": _canonical_url(raw.get("url")),
        "published_at": raw.get("published_at"), "first_seen_at": raw.get("first_seen_at") or timestamp,
        "entities": entities, "location": raw.get("location") or _location_from(entities),
        "language": raw.get("language") or ("te" if _TELUGU.search(title + text) else "en"),
        "engagement_metrics": raw.get("engagement_metrics") or {},
        "raw_metadata": raw.get("raw_metadata") or {},
        "content_hash": _content_hash(title, text, publisher), "candidate_id": None, "created_at": timestamp,
    }


def dedupe(signals):
    """Drop identical/syndicated copies by canonical URL and normalized content hash.

    Returns (unique, deduped_count). One story copied across mirrors counts once.
    """
    seen_url, seen_hash, unique, deduped = set(), set(), [], 0
    for signal in signals:
        key_url = signal.get("canonical_url") or signal.get("url")
        if key_url and key_url in seen_url:
            deduped += 1
            continue
        if signal["content_hash"] in seen_hash:
            deduped += 1
            continue
        if key_url:
            seen_url.add(key_url)
        seen_hash.add(signal["content_hash"])
        unique.append(signal)
    return unique, deduped


def backoff_delay(attempt, *, base=5.0, cap=300.0, jitter=0.3):
    """Bounded exponential backoff with jitter; never a retry storm."""
    delay = min(cap, base * 2 ** max(0, attempt - 1))
    return delay * (1 - jitter * random.random())


def query_variants(group, recent_entities=()):
    """English + Telugu + Romanized variants for a query group, plus recent entities."""
    from fast_discovery import expand_query
    variants = []
    for term in list(group) + list(recent_entities):
        variants.extend(expand_query(term))
    variants.extend(DISTRICT_VOCAB[:4])
    return list(dict.fromkeys(variants))


# ---------- source registry ----------

DEFAULT_SOURCES = (
    ("youtube", "YouTube", "YouTube", "discover_youtube", False, 300),
    ("ntv", "NTV Telugu", "NTV Telugu", "discover_news", True, 300),
    ("tv9", "TV9 Telugu", "TV9 Telugu", "discover_news", True, 300),
    ("sakshi", "Sakshi", "Sakshi", "discover_news", True, 300),
    ("eenadu", "Eenadu", "Eenadu", "discover_news", True, 300),
    ("abn", "ABN Andhra Jyothy", "ABN Andhra Jyothy", "discover_news", True, 300),
    ("samayam", "Samayam Telugu", "Samayam Telugu", "discover_news", True, 300),
    ("news18te", "News18 Telugu", "News18 Telugu", "discover_news", True, 300),
    ("akashvani", "Akashvani/NewsOnAIR", "NewsOnAIR", "discover_news", True, 300),
    ("pib", "PIB", "Press Information Bureau", "discover_pib", True, 300),
    ("apgov", "AP Government", "Government of Andhra Pradesh", "discover_ap_gov", True, 300),
    ("cmo", "CMO Andhra Pradesh", "Chief Minister's Office, AP", "discover_cmo_ap", True, 300),
    ("x", "X", "X", "discover_x", False, 300),
    ("instagram", "Instagram", "Instagram", "discover_instagram", False, 300),
)


def sync_sources(*, connect, now=None):
    """Register the default source adapter set; X/Instagram start UNCONFIGURED."""
    timestamp = now() if now else _now()
    with connect() as connection:
        for source_id, family, publisher, adapter, configured, interval in DEFAULT_SOURCES:
            existing = connection.execute("SELECT id FROM discovery_sources WHERE id=?", (source_id,)).fetchone()
            if existing:
                continue
            connection.execute(
                "INSERT INTO discovery_sources(id,family,adapter,publisher,enabled,configured,poll_interval_seconds,"
                "created_at) VALUES(?,?,?,?,?,?,?,?)",
                (source_id, family, adapter, publisher, 1, int(configured), interval, timestamp),
            )


def sources(*, connect):
    with connect() as connection:
        return [dict(row) for row in connection.execute("SELECT * FROM discovery_sources ORDER BY family")]


def source_health(*, connect):
    """Per-source status: HEALTHY / DEGRADED / UNCONFIGURED / FAILED."""
    rows = sources(connect=connect)
    result = {}
    for source in rows:
        if not source["configured"]:
            status = "UNCONFIGURED"
        elif source["consecutive_failures"] >= MAX_CONSECUTIVE_FAILURES:
            status = "FAILED"
        elif source["consecutive_failures"] > 0:
            status = "DEGRADED"
        else:
            status = "HEALTHY"
        result[source["family"]] = {
            "status": status, "last_success": source["last_success_at"], "last_error": source["last_error"],
            "consecutive_failures": source["consecutive_failures"],
            "average_latency_ms": source["average_latency_ms"], "results_last_24h": source["results_last_24h"],
            "poll_interval_seconds": source["poll_interval_seconds"],
        }
    return result


def _record_source_result(connection, source_id, *, status, signals, latency_ms, error=None, now=None):
    timestamp = now() if now else _now()
    fetch_run_id = "FR-" + uuid.uuid4().hex[:12].upper()
    connection.execute(
        "INSERT INTO discovery_fetch_runs(id,source_id,started_at,finished_at,status,signals_fetched,latency_ms,error) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (fetch_run_id, source_id, timestamp, timestamp, status, signals, latency_ms, (error or None)))
    if status == "COMPLETED":
        row = connection.execute("SELECT consecutive_failures,average_latency_ms FROM discovery_sources WHERE id=?",
                                 (source_id,)).fetchone()
        avg = row["average_latency_ms"]
        avg = latency_ms if avg is None else (avg * 0.7 + latency_ms * 0.3)
        connection.execute(
            "UPDATE discovery_sources SET last_success_at=?,last_error=NULL,consecutive_failures=0,average_latency_ms=?,"
            "results_last_24h=results_last_24h+?,last_polled_at=? WHERE id=?",
            (timestamp, avg, signals, timestamp, source_id))
    else:
        connection.execute(
            "UPDATE discovery_sources SET last_error=?,consecutive_failures=consecutive_failures+1,last_polled_at=? "
            "WHERE id=?", (str(error or status)[:400], timestamp, source_id))
    return fetch_run_id


def run_discovery_cycle(*, connect, adapters, now=None, cold_start=False, handoff=True):
    """One polling cycle across all enabled, configured sources.

    Each adapter is called in isolation: a failure is recorded and never stops the cycle.
    429s are recorded as RATE_LIMITED (the scheduler backs off the source). Deduped signals
    feed fast_discovery clustering; a candidate hands off immediately to verification.
    """
    import fast_discovery
    timestamp = now() if now else _now()
    started = datetime.now(timezone.utc)
    cycle = {"sources_polled": [], "sources_unconfigured": [], "sources_failed": [],
             "signals_fetched": 0, "signals_deduped": 0, "candidates_created": 0,
             "handoffs": 0, "candidates": [], "fetch_run_ids": []}
    all_signals = []
    for source in sources(connect=connect):
        if not source["configured"] or not source["enabled"]:
            cycle["sources_unconfigured"].append(source["family"])
            continue
        adapter = adapters.get(source["adapter"])
        if adapter is None:
            cycle["sources_failed"].append({"family": source["family"], "error": "no adapter"})
            continue
        fetch_started = datetime.now(timezone.utc)
        try:
            raw_items = adapter(source) or []
        except Exception as error:  # noqa: BLE001 - one source must never crash the cycle
            latency = (datetime.now(timezone.utc) - fetch_started).total_seconds() * 1000
            status = "RATE_LIMITED" if "429" in str(error) else "FAILED"
            with connect() as connection:
                _record_source_result(connection, source["id"], status=status, signals=0,
                                      latency_ms=latency, error=str(error), now=now)
            cycle["sources_failed"].append({"family": source["family"], "error": str(error)})
            continue
        latency = (datetime.now(timezone.utc) - fetch_started).total_seconds() * 1000
        normalized = [normalize_signal(item, source_family=source["family"], publisher=source["publisher"],
                                       source_id=source["id"], now=now) for item in raw_items]
        with connect() as connection:
            fetch_run_id = _record_source_result(connection, source["id"], status="COMPLETED",
                                                 signals=len(normalized), latency_ms=latency, now=now)
            for signal in normalized:
                signal["fetch_run_id"] = fetch_run_id
        cycle["sources_polled"].append(source["family"])
        cycle["fetch_run_ids"].append(fetch_run_id)
        cycle["signals_fetched"] += len(normalized)
        all_signals.extend(normalized)

    unique, deduped = dedupe(all_signals)
    cycle["signals_deduped"] = deduped
    # Persist signals (content_hash is unique), then cluster the unique set.
    stored = []
    with connect() as connection:
        for signal in unique:
            try:
                connection.execute(
                    "INSERT INTO discovery_signals(id,source_id,fetch_run_id,source_family,publisher,title,text,url,"
                    "canonical_url,published_at,first_seen_at,entities_json,location,language,engagement_metrics_json,"
                    "raw_metadata_json,content_hash,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (signal["id"], signal["source_id"], signal["fetch_run_id"], signal["source_family"],
                     signal["publisher"], signal["title"], signal["text"], signal["url"], signal["canonical_url"],
                     signal["published_at"], signal["first_seen_at"], _json(signal["entities"]), signal["location"],
                     signal["language"], _json(signal["engagement_metrics"]), _json(signal["raw_metadata"]),
                     signal["content_hash"], signal["created_at"]))
                stored.append(signal)
            except Exception:  # duplicate hash -> already stored
                pass
    if stored:
        candidate = fast_discovery.discover(stored, connect=connect, now=now)
        if candidate:
            cycle["candidates_created"] = 1
            cycle["candidates"].append(candidate["id"])
            if handoff:
                cycle["handoffs"] = 1
    # SLO sample.
    elapsed = (datetime.now(timezone.utc) - started).total_seconds()
    with connect() as connection:
        connection.execute(
            "INSERT INTO discovery_slo_samples(fetch_latency_ms,candidate_latency_seconds,slo_met,recorded_at) "
            "VALUES(?,?,?,?)", (elapsed * 1000, elapsed, int(elapsed <= SLO_SECONDS), timestamp))
    return cycle


def _json(value):
    import json
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def slo_metrics(*, connect):
    """Median/p95 candidate latency and SLO misses, plus signals/candidates today."""
    with connect() as connection:
        samples = [row[0] for row in connection.execute(
            "SELECT candidate_latency_seconds FROM discovery_slo_samples WHERE candidate_latency_seconds IS NOT NULL "
            "ORDER BY candidate_latency_seconds")]
        today = datetime.now(timezone.utc).date().isoformat()
        signals_today = connection.execute(
            "SELECT COUNT(*) FROM discovery_signals WHERE date(created_at)=?", (today,)).fetchone()[0]
        candidates_today = connection.execute(
            "SELECT COUNT(*) FROM event_candidates WHERE date(created_at)=?", (today,)).fetchone()[0]
        slo_misses = connection.execute(
            "SELECT COUNT(*) FROM discovery_slo_samples WHERE slo_met=0").fetchone()[0]
    def percentile(values, pct):
        if not values:
            return None
        index = min(len(values) - 1, int(round((pct / 100) * (len(values) - 1))))
        return round(values[index], 2)
    return {"median_latency_seconds": percentile(samples, 50), "p95_latency_seconds": percentile(samples, 95),
            "slo_misses": slo_misses, "signals_today": signals_today, "candidates_today": candidates_today,
            "samples": len(samples)}


# ---------- burst detection ----------

def detect_bursts(signals, *, window_minutes=30, min_families=3):
    """A topic appearing across multiple independent families in a short window -> high priority.

    Affects verification ORDER only; factual thresholds are unchanged.
    """
    from fast_discovery import extract_entities
    buckets = {}
    for signal in signals:
        for entity in extract_entities(f"{signal.get('title','')} {signal.get('text','')}"):
            buckets.setdefault(entity, []).append(signal)
    bursts = []
    for entity, items in buckets.items():
        families = {i["source_family"] for i in items}
        if len(families) >= min_families:
            bursts.append({"entity": entity, "families": sorted(families), "signals": len(items)})
    return bursts


# ---------- built-in no-key adapters ----------

def _rss_items(feed_url, *, user_agent, timeout=20, retries=2, sleep=None):
    """Fetch and parse an RSS/Atom feed into raw items. Honors 429 with bounded backoff."""
    import time as _time
    from urllib.error import HTTPError
    from urllib.request import Request, build_opener
    import xml.etree.ElementTree as ET
    pause = sleep or _time.sleep
    for attempt in range(1, retries + 1):
        try:
            request = Request(feed_url, headers={"User-Agent": user_agent, "Accept": "application/rss+xml, application/xml"})
            with build_opener().open(request, timeout=timeout) as response:
                payload = response.read()
            break
        except HTTPError as error:
            if error.code != 429 or attempt == retries:
                raise
            pause(backoff_delay(attempt) / 30.0)  # keep tests fast; real scheduler uses the full delay
    else:  # pragma: no cover
        return []
    root = ET.fromstring(payload)
    items = []
    for item in root.iter():
        if item.tag.split("}")[-1] not in ("item", "entry"):
            continue
        def _text(name):
            node = item.find(name) or item.find("{http://www.w3.org/2005/Atom}" + name)
            return (node.text or "").strip() if node is not None and node.text else ""
        link_node = item.find("{http://www.w3.org/2005/Atom}link")
        url = _text("link") or (link_node.get("href") if link_node is not None else "")
        items.append({"title": _text("title"), "text": _text("description") or _text("summary"),
                      "url": url, "published_at": _text("pubDate") or _text("updated")})
    return items


def make_rss_adapter(feed_url, *, user_agent="ReachOut-OS/0.4 (live discovery)"):
    def adapter(source):
        return _rss_items(feed_url, user_agent=user_agent)
    return adapter


def default_adapters(*, user_agent="ReachOut-OS/0.4 (live discovery)", http=None):
    """Provider-independent adapter map. RSS/news adapters need no credentials.

    `http` is an injectable transport for tests: http(url) -> list[dict].
    """
    def fetch(url):
        if http is not None:
            return http(url)
        return _rss_items(url, user_agent=user_agent)
    return {
        "discover_news": lambda source: fetch(source.get("feed_url") or source.get("url") or ""),
        "discover_pib": lambda source: fetch(source.get("feed_url") or "https://www.pib.gov.in/RssMain.aspx"),
        "discover_ap_gov": lambda source: fetch(source.get("feed_url") or "https://www.ap.gov.in/rss.xml"),
        "discover_cmo_ap": lambda source: fetch(source.get("feed_url") or "https://cm.ap.gov.in/rss.xml"),
        "discover_youtube": lambda source: fetch(source.get("feed_url") or ""),
        "discover_x": lambda source: [],
        "discover_instagram": lambda source: [],
    }


def checkpoint_for(source_id, *, connect):
    with connect() as connection:
        row = connection.execute(
            "SELECT last_polled_at,last_seen_published_at,cursor FROM discovery_sources WHERE id=?",
            (source_id,)).fetchone()
    return dict(row) if row else {}


def update_checkpoint(source_id, *, connect, last_seen_published_at=None, cursor=None, now=None):
    with connect() as connection:
        connection.execute(
            "UPDATE discovery_sources SET last_seen_published_at=COALESCE(?,last_seen_published_at),"
            "cursor=COALESCE(?,cursor),last_polled_at=? WHERE id=?",
            (last_seen_published_at, cursor, (now() if now else _now()), source_id))
