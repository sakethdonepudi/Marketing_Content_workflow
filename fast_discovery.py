"""Architecture 08 addendum — fast same-day discovery.

Discovery is permissive and fast; it creates *event candidates* from co-occurring salient
entities across many public source families (YouTube, X, Instagram, Telugu TV/news,
PIB/AP Government/CMO, wire). A candidate is a lead, never evidence: verification stays the
strict gate that follows. Discovery scores on public relevance, recency, corroboration, and
event salience only — never persuasion, demographics, or political advantage.
"""

from datetime import datetime, timezone
import re
import uuid

# Salient AP/Telangana public entities and event anchors to look for.
KNOWN_ENTITIES = (
    "anant ambani", "n. chandrababu naidu", "chandrababu naidu", "nara chandrababu naidu",
    "pawan kalyan", "jagan", "ys jagan", "lokesh", "nara lokesh", "tdp", "telugu desam",
)
KNOWN_PROJECTS = ("global horticulture hub", "indian school of agriculture", "horticulture hub")
KNOWN_LOCATIONS = ("madanapalle", "madanaapalle", "madnapalle", "amaravati", "visakhapatnam",
                   "vijayawada", "guntur", "tirupati", "ongole", "nellore")
SOURCE_FAMILIES = ("YouTube", "YouTube Shorts", "X", "Instagram", "TV9 Telugu", "NTV Telugu",
                   "Sakshi", "Eenadu", "ABN Andhra Jyothy", "Samayam Telugu", "News18 Telugu",
                   "Akashvani/NewsOnAIR", "PIB", "AP Government", "CMO Andhra Pradesh", "Wire")
PRIMARY_FAMILIES = ("PIB", "AP Government", "CMO Andhra Pradesh", "Akashvani/NewsOnAIR")

# Recency bands for same-day weighting.
RECENCY_BANDS = ((2, "VERY_HIGH"), (6, "HIGH"), (24, "NORMAL"))
_TELUGU = re.compile(r"[\u0C00-\u0C7F]")


def _now():
    return datetime.now(timezone.utc).isoformat()


def _age_hours(published_at, now_at):
    try:
        published = datetime.fromisoformat(str(published_at).replace("Z", "+00:00"))
        reference = datetime.fromisoformat(str(now_at).replace("Z", "+00:00"))
        return max(0.0, (reference - published).total_seconds() / 3600)
    except (TypeError, ValueError):
        return 999.0


def recency_weight(age_hours):
    for limit, band in RECENCY_BANDS:
        if age_hours <= limit:
            return band
    return "LOW"


def extract_entities(text):
    """Salient entities + projects + locations found in free text (case-insensitive)."""
    lowered = str(text or "").casefold()
    found = []
    for entity in KNOWN_ENTITIES:
        if entity in lowered:
            found.append(entity)
    for project in KNOWN_PROJECTS:
        if project in lowered:
            found.append(project)
    for location in KNOWN_LOCATIONS:
        if location in lowered:
            found.append(location)
    # De-duplicate while preserving order; normalize entity casing.
    seen, ordered = set(), []
    for item in found:
        if item not in seen:
            seen.add(item)
            ordered.append(item)
    return ordered


def expand_query(term):
    """Query expansion: English, Telugu, and Romanized Telugu variants."""
    base = str(term).strip()
    telugu_map = {
        "madanapalle": "మదనపల్లె",
        "global horticulture hub": "గ్లోబల్ హార్టికల్చర్ హబ్",
        "anant ambani": "అనంత్ అంబానీ",
        "chandrababu naidu": "చంద్రబాబు నాయుడు",
        "pawan kalyan": "పవన్ కల్యాణ్",
    }
    variants = [base]
    lowered = base.casefold()
    for key, telugu in telugu_map.items():
        if key in lowered:
            variants.append(telugu)
    # Romanized variant: drop punctuation/spacing noise.
    romanized = re.sub(r"[^a-z0-9 ]", "", lowered)
    if romanized and romanized not in (v.casefold() for v in variants):
        variants.append(romanized)
    return list(dict.fromkeys(variants))


def _candidate_threshold(signals):
    """ONE primary + ONE independent, OR THREE independent same-day signals."""
    primary = [s for s in signals if s.get("is_primary")]
    independent = set(s["source_family"] for s in signals)
    if primary and len(independent) >= 2:
        return True
    return len(independent) >= 3


def _cluster_key(signals):
    """One physical event clusters on shared location + entities + project."""
    locations = {s.get("location") for s in signals if s.get("location")}
    shared = set()
    for s in signals:
        ents = set(s.get("entities") or [])
        shared = ents if not shared else (shared & ents)
    return (sorted(locations), sorted(e for e in shared if e))


def discover(signals, *, connect, now=None, jurisdiction="Andhra Pradesh"):
    """Cluster signals into at most one event candidate (no duplicate events per physical event).

    Each signal: {source_family, url, title, text, published_at, is_primary}. Returns the
    candidate dict, or None when the threshold is not met.
    """
    timestamp = now() if now else _now()
    enriched = []
    for signal in signals:
        text = " ".join(str(signal.get(k) or "") for k in ("title", "text"))
        entities = extract_entities(text)
        enriched.append({
            "source_family": signal.get("source_family"), "url": signal.get("url"),
            "title": str(signal.get("title") or "")[:300], "text": str(signal.get("text") or "")[:4000],
            "published_at": signal.get("published_at"), "is_primary": bool(signal.get("is_primary")),
            "entities": entities, "location": signal.get("location") or _location_from(entities),
        })
    if not _candidate_threshold(enriched):
        return None
    locations, shared_entities = _cluster_key(enriched)
    location = _pretty_location(locations[0]) if locations else None
    families = sorted({s["source_family"] for s in enriched if s["source_family"]})
    all_entities = sorted({e for s in enriched for e in s["entities"]})
    ages = [_age_hours(s["published_at"], timestamp) for s in enriched if s.get("published_at")]
    band = recency_weight(min(ages)) if ages else "LOW"
    confidence = round(min(1.0, len(families) / 4.0) * 0.6 + (0.4 if band in ("VERY_HIGH", "HIGH") else 0.15), 3)

    with connect() as connection:
        # Idempotent: reuse an existing candidate for the same location + shared entities.
        for row in connection.execute("SELECT * FROM event_candidates WHERE state IN ('CANDIDATE','HANDED_OFF')"):
            existing = dict(row)
            if (existing.get("location") or None) == location and shared_entities:
                import json as _json
                existing_entities = set(_json.loads(existing["entities_json"] or "[]"))
                if existing_entities & set(shared_entities):
                    return candidate(existing["id"], connect=connect)
        candidate_id = "EC-" + uuid.uuid4().hex[:12].upper()
        headline = _headline(enriched, location)
        first_seen = min((s["published_at"] for s in enriched if s.get("published_at")), default=timestamp)
        latency = round(max(0.0, _age_hours(first_seen, timestamp)) * 3600, 1)
        import json
        record = {
            "id": candidate_id, "headline": headline, "location": location, "jurisdiction": jurisdiction,
            "state": "CANDIDATE", "confidence": band, "discovery_confidence": confidence,
            "entities_json": json.dumps(all_entities), "source_families_json": json.dumps(families),
            "source_count": len(enriched), "independent_source_count": len(families),
            "event_id": None, "first_seen_at": first_seen, "candidate_created_at": timestamp,
            "discovery_latency_seconds": latency, "created_at": timestamp,
        }
        connection.execute(
            f"INSERT INTO event_candidates({','.join(record)}) VALUES({','.join('?' for _ in record)})",
            tuple(record.values()),
        )
        for index, signal in enumerate(enriched):
            connection.execute(
                "INSERT INTO candidate_signals(id,candidate_id,source_family,url,title,text,published_at,"
                "is_primary,entities_json,location,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                ("CS-" + uuid.uuid4().hex[:12].upper(), candidate_id, signal["source_family"], signal["url"],
                 signal["title"], signal["text"], signal["published_at"], int(signal["is_primary"]),
                 json.dumps(signal["entities"]), signal["location"], timestamp),
            )
    return candidate(candidate_id, connect=connect)


def _location_from(entities):
    for entity in entities:
        if entity in KNOWN_LOCATIONS:
            return entity
    return None


def _pretty_location(value):
    return " ".join(word.capitalize() for word in str(value).split())


def _headline(signals, location):
    for signal in signals:
        if signal["title"]:
            return signal["title"]
    return f"Event near {location}" if location else "Discovered event"


def candidate(candidate_id, *, connect):
    import json
    with connect() as connection:
        row = connection.execute("SELECT * FROM event_candidates WHERE id=?", (candidate_id,)).fetchone()
        if row is None:
            raise KeyError(candidate_id)
        result = dict(row)
        result["entities"] = json.loads(result.pop("entities_json") or "[]")
        result["source_families"] = json.loads(result.pop("source_families_json") or "[]")
        result["signals"] = [dict(s) for s in connection.execute(
            "SELECT * FROM candidate_signals WHERE candidate_id=? ORDER BY created_at", (candidate_id,))]
    for signal in result["signals"]:
        signal["entities"] = json.loads(signal.pop("entities_json") or "[]")
    return result


def list_candidates(*, connect, states=None):
    import json
    with connect() as connection:
        if states:
            marks = ",".join("?" for _ in states)
            rows = connection.execute(
                f"SELECT * FROM event_candidates WHERE state IN ({marks}) ORDER BY first_seen_at DESC", states).fetchall()
        else:
            rows = connection.execute("SELECT * FROM event_candidates ORDER BY first_seen_at DESC").fetchall()
    results = []
    for row in rows:
        item = dict(row)
        item["entities"] = json.loads(item.pop("entities_json") or "[]")
        item["source_families"] = json.loads(item.pop("source_families_json") or "[]")
        results.append(item)
    return results


def discovery_recall_qa(*, connect):
    """Missed-event recall: the Madanapalle fixture must have produced a candidate."""
    madanapalle = [c for c in list_candidates(connect=connect)
                   if (c.get("location") or "").startswith("Madanapalle")]
    if not madanapalle:
        return {"status": "FLAG", "errors": ["No Madanapalle candidate was discovered."], "fixture": "Madanapalle Global Horticulture Hub"}
    top = madanapalle[0]
    have = set(top["entities"])
    expected = {"anant ambani", "n. chandrababu naidu", "chandrababu naidu", "pawan kalyan", "global horticulture hub"}
    missing = [e for e in ("anant ambani", "chandrababu naidu", "pawan kalyan") if not any(e in h for h in have)]
    errors = []
    if missing:
        errors.append("Missing expected entities: " + ", ".join(missing))
    return {"status": "PASS" if not errors else "FLAG", "errors": errors,
            "candidate_id": top["id"], "entities": sorted(have), "location": top["location"],
            "source_families": top["source_families"]}
