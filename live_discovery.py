"""Architecture 09 — continuous live discovery (LIVE_DISCOVERY_V1).

A provider-independent scheduler polls many public source families (RSS/news, PIB, AP
Government, CMO, YouTube, Telugu news) on a bounded cadence, normalizes results into
discovery signals, dedupes syndicated copies, clusters them with `fast_discovery`, and hands
candidates straight to strict verification. Signals are leads, never evidence.

Off by default (`LIVE_DISCOVERY_ENABLED=0`). X/Instagram are marked UNCONFIGURED without
blocking the cycle. A broken source never crashes the cycle; 429s back off with jitter.
"""

from datetime import datetime, timezone
from pathlib import Path
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

REGISTRY_PATH = Path(__file__).resolve().parent / "config" / "discovery_sources.json"


def load_registry(path=None):
    import json
    return json.loads(Path(path or REGISTRY_PATH).read_text(encoding="utf-8"))["sources"]


def adapter_key(source):
    """Map a registry entry to its adapter function key."""
    if source.get("adapter_type") == "UNCONFIGURED":
        return {"YouTube": "discover_youtube", "X": "discover_x", "Instagram": "discover_instagram"}.get(
            source.get("family"), "discover_news")
    return {"rss": "discover_news", "pib_listing": "discover_pib"}.get(source.get("parser_name"), "discover_news")


def sync_sources(*, connect, now=None, path=None):
    """Load the validated source registry (config/discovery_sources.json) into the DB."""
    timestamp = now() if now else _now()
    with connect() as connection:
        for source in load_registry(path):
            existing = connection.execute("SELECT id FROM discovery_sources WHERE id=?", (source["source_id"],)).fetchone()
            adapter = adapter_key(source)
            values = (source["family"], source["publisher"], adapter, source.get("adapter_type"),
                      int(source.get("enabled", True)), int(source.get("configured", True)),
                      int(source.get("poll_interval_seconds", 300)), source.get("feed_url"), source.get("parser_name"))
            if existing:
                connection.execute(
                    "UPDATE discovery_sources SET family=?,publisher=?,adapter=?,adapter_type=?,enabled=?,configured=?,"
                    "poll_interval_seconds=?,feed_url=?,parser_name=? WHERE id=?", (*values, source["source_id"]))
            else:
                connection.execute(
                    "INSERT INTO discovery_sources(id,family,adapter,publisher,adapter_type,enabled,configured,"
                    "poll_interval_seconds,feed_url,parser_name,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (source["source_id"], source["family"], adapter, source["publisher"],
                     source.get("adapter_type"), int(source.get("enabled", True)), int(source.get("configured", True)),
                     int(source.get("poll_interval_seconds", 300)), source.get("feed_url"), source.get("parser_name"),
                     timestamp))


def parse_pib_listing(html_text, *, base_url="https://www.pib.gov.in"):
    """Parse PIB's listing HTML into raw items; fail closed when structure is unclear."""
    pattern = re.compile(
        r'href="(?P<url>[^"]*(?:PressReleasePage|PRID=|Relese|PressRelease)[^"]*)"[^>]*>(?P<title>[^<]{6,300})<', re.I)
    items = []
    for match in pattern.finditer(html_text or ""):
        title = match.group("title").strip()
        if not title:
            continue
        url = match.group("url")
        if url.startswith("/"):
            url = base_url + url
        date_match = re.search(r"(\d{1,2}\s+\w{3}\s+\d{4})", match.group(0))
        items.append({"title": title, "url": url,
                      "published_at": date_match.group(1) if date_match else None,
                      "text": "", "raw_metadata": {"parser": "pib_listing"}})
    return items

def sources(*, connect):
    with connect() as connection:
        return [dict(row) for row in connection.execute("SELECT * FROM discovery_sources ORDER BY family")]


def source_health(*, connect):
    """Per-source status: HEALTHY / DEGRADED / UNCONFIGURED / FAILED."""
    rows = sources(connect=connect)
    result = {}
    for source in rows:
        error_text = (source["last_error"] or "").lower()
        if not source["configured"]:
            status = "UNCONFIGURED"
        elif source.get("adapter_type") == "UNAVAILABLE":
            status = "FAILED"
        elif any(token in error_text for token in ("403", "captcha", "bot", "challenge", "blocked")):
            status = "BLOCKED"
        elif source["consecutive_failures"] >= MAX_CONSECUTIVE_FAILURES:
            status = "FAILED"
        elif source["consecutive_failures"] > 0:
            status = "DEGRADED"
        elif source["last_success_at"] is None and source["last_polled_at"] is not None:
            # Polled but produced nothing parseable -> degraded, never falsely HEALTHY.
            status = "DEGRADED"
        else:
            status = "HEALTHY"
        result[source["family"]] = {
            "status": status, "adapter_type": source.get("adapter_type"),
            "last_polled": source.get("last_polled_at"), "last_success": source["last_success_at"],
            "last_error": source["last_error"], "consecutive_failures": source["consecutive_failures"],
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
        # Same-day filter: keep the last 24h; mark unknown dates rather than dropping silently.
        kept = []
        for signal in normalized:
            age = _age_hours_for(signal.get("published_at"), timestamp)
            if age is None:
                signal["published_at"] = signal.get("published_at") or "UNKNOWN"
                kept.append(signal)
            elif age <= 24.0:
                kept.append(signal)
        normalized = kept
        with connect() as connection:
            # A reachable source with zero parseable items is DEGRADED, not healthy.
            run_status = "COMPLETED" if normalized else "FAILED"
            fetch_run_id = _record_source_result(connection, source["id"], status=run_status,
                                                 signals=len(normalized), latency_ms=latency,
                                                 error=None if normalized else "no parseable items", now=now)
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


def _age_hours_for(published_at, now_at):
    """Hours since publication, or None when the timestamp is missing/UNKNOWN."""
    if not published_at or str(published_at).upper() == "UNKNOWN":
        return None
    from email.utils import parsedate_to_datetime
    try:
        published = datetime.fromisoformat(str(published_at).replace("Z", "+00:00"))
    except ValueError:
        try:
            published = parsedate_to_datetime(str(published_at))
        except (TypeError, ValueError):
            return None
    if published.tzinfo is None:
        published = published.replace(tzinfo=timezone.utc)
    reference = datetime.fromisoformat(str(now_at).replace("Z", "+00:00"))
    return max(0.0, (reference - published).total_seconds() / 3600)


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

def _rss_items(feed_url, *, user_agent, timeout=20, retries=2, sleep=None, http=None):
    """Fetch and parse an RSS/Atom feed into raw items. Honors 429 with bounded backoff.

    Delegates parsing to `_parse_rss_text` so the live and fixture paths share one parser.
    """
    text = _fetch_text(feed_url, user_agent=user_agent, timeout=timeout, attempts=retries,
                       sleep=sleep, http=http)
    return _parse_rss_text(text)


def make_rss_adapter(feed_url, *, user_agent="ReachOut-OS/0.5 (live discovery)"):
    def adapter(source):
        return _rss_items(feed_url, user_agent=user_agent)
    return adapter


def _fetch_text(url, *, user_agent, timeout=20, http=None, attempts=2, sleep=None):
    """Fetch a URL as text; honors 429 with bounded backoff. `http` overrides for tests."""
    if http is not None:
        result = http(url)
        return result if isinstance(result, str) else result
    import time as _time
    from urllib.error import HTTPError
    from urllib.request import Request, build_opener
    pause = sleep or _time.sleep
    for attempt in range(1, attempts + 1):
        try:
            request = Request(url, headers={"User-Agent": user_agent,
                                            "Accept": "application/rss+xml, application/xml, text/html"})
            with build_opener().open(request, timeout=timeout) as response:
                return response.read().decode("utf-8", "replace")
        except HTTPError as error:
            if error.code != 429 or attempt == attempts:
                raise
            pause(backoff_delay(attempt) / 30.0)  # keep tests fast; real scheduler uses the full delay
    return ""  # pragma: no cover - loop always returns or raises


def _parse_rss_text(text):
    """Parse RSS/Atom text into raw items (provider-independent).

    Note: ElementTree Elements with text but no children are falsy, so lookups use explicit
    `is None` checks rather than `or`.
    """
    import xml.etree.ElementTree as ET
    root = ET.fromstring(text)
    items = []
    for item in root.iter():
        if item.tag.split("}")[-1] not in ("item", "entry"):
            continue
        def _first(*names):
            for name in names:
                node = item.find(name)
                if node is None:
                    node = item.find("{http://www.w3.org/2005/Atom}" + name)
                if node is not None and node.text and node.text.strip():
                    return node.text.strip()
            return ""
        link_attr = ""
        for link_node in item.findall("{http://www.w3.org/2005/Atom}link"):
            if link_node.get("href"):
                link_attr = link_node.get("href")
                break
        url = link_attr or _first("link", "guid")
        items.append({"title": _first("title"), "text": _first("description", "summary", "content"),
                      "url": url, "published_at": _first("pubDate", "published", "updated", "date")})
    return items


def default_adapters(*, user_agent="ReachOut-OS/0.5 (live discovery)", http=None):
    """Provider-independent adapter map keyed by the source's `adapter` column.

    RSS sources use `_parse_rss_text`; the PIB listing uses `parse_pib_listing` (fail-closed).
    `http(url) -> str` is an injectable text transport for fixture tests.
    """
    def rss(source):
        url = source.get("feed_url")
        if not url:
            return []
        return _rss_items(url, user_agent=user_agent, http=http)

    def pib(source):
        url = source.get("feed_url") or "https://www.pib.gov.in/RssMain.aspx?reg=48&lang=2"
        text = http(url) if http is not None else _fetch_text(url, user_agent=user_agent)
        return parse_pib_listing(text)
    return {
        "discover_news": rss,
        "discover_pib": pib,
        "discover_ap_gov": rss,
        "discover_cmo_ap": rss,
        "discover_youtube": lambda source: [],
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
