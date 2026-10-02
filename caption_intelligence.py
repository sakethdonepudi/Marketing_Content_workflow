"""Architecture 10 — CAPTION_PACKAGE_V1 + CONTENT_DISCOVERY_INTELLIGENCE_V1.

Factual, non-partisan caption/hashtag/keyword metadata for verified reels. Captions are built
ONLY from the approved claim set, so they can never introduce a new factual claim. Hashtags are
scored on relevance/recency/frequency/geography — never on political persuasion potential.
Aggregate CONTENT signals only: no viewer demographic or political-personality profiles.
"""

from datetime import datetime, timezone
import hashlib
import json
import re
import uuid

COPY_POLICY_VERSION = "caption-package-v1.0"
DEFAULT_HASHTAG_TARGET = (5, 10)

# Partisan/unsupported framing that must never appear in copy or hashtags (C.10, C.19).
_BANNED_TERMS = (
    "vote", "voting", "vote for", "support our", "join the movement", "elect",
    "anti-national", "traitor", "corrupt", "criminals", "jagan", "ycongress", "ysrcp",
    "tdp zindabad", "jai tdp", "congress hatao", "bjp", "modi zindabad", "opposition party",
    "propaganda", "slogan", "we demand", "must win", "defeat them",
)
_INFERRED_REACTION = re.compile(
    r"\b(loved|impressed|delighted|furious|angry|humiliated|slapped|destroyed|thrashed|"
    r"trolled|slammed|blasted|praised|hailed)\b", re.I)
_PROPER_NOUN = re.compile(r"\b[A-Z][A-Za-z0-9&.']+\b")


def _now():
    return datetime.now(timezone.utc).isoformat()


def _flat(text):
    return re.sub(r"\s+", " ", str(text or "")).strip()


# ---------- hashtag + keyword generation (C.13, C.15) ----------

def _slug(value, *, max_words=4):
    words = re.findall(r"[A-Za-z0-9]+", str(value or ""))
    if not words:
        return None
    return "".join(word[:1].upper() + word[1:] for word in words[:max_words])


def event_hashtags(entities, location, *, event_name=None):
    return [value for value in (event_name, location) if value]


def candidate_hashtags(*, entities, location, event_name=None, topic_terms=(), related=()):
    """Build candidate hashtags by category (A event, B location, C entities, D topic, E related)."""
    out, seen = [], set()

    def add(raw, kind):
        slug = _slug(raw)
        if not slug:
            return
        tag = "#" + slug
        if tag.casefold() in seen:
            return
        seen.add(tag.casefold())
        out.append({"tag": tag, "kind": kind, "source": _flat(raw)})

    if event_name:
        add(event_name, "event")
    if location:
        add(location, "location")
        add("Andhra Pradesh", "location")
    for entity in entities or []:
        add(entity, "entity")
    for term in topic_terms or []:
        add(term, "topic")
    for term in related or []:
        add(term, "related")
    return out


def score_hashtag(candidate, *, recency_band="NORMAL", frequency=0, source_count=1,
                  geographic=False, event_entity=False):
    """Score on exact relevance, recency, frequency, cross-source, geography, event/entity (C.14).

    Never scores on political persuasion potential.
    """
    base = {"event": 1.0, "entity": 0.9, "location": 0.8, "topic": 0.6, "related": 0.5}.get(candidate["kind"], 0.4)
    recency = {"VERY_HIGH": 0.2, "HIGH": 0.14, "NORMAL": 0.07, "LOW": 0.0}.get(recency_band, 0.05)
    freq = min(0.2, 0.02 * max(0, frequency))
    cross = min(0.15, 0.05 * max(0, source_count - 1))
    geo = 0.1 if geographic else 0.0
    ev = 0.1 if event_entity else 0.0
    score = round(min(1.0, base + recency + freq + cross + geo + ev), 4)
    return {"tag": candidate["tag"], "kind": candidate["kind"], "score": score,
            "components": {"relevance": base, "recency": recency, "frequency": freq,
                           "cross_source": cross, "geographic": geo, "event_entity": ev}}


def select_hashtags(candidates, *, terms=None, target=DEFAULT_HASHTAG_TARGET, recency_band="NORMAL"):
    """Return a small high-quality set (default 5-10), not a 25-30 generic dump (C.14)."""
    stats = {}
    for term in terms or []:
        stats[term.get("term", "").casefold()] = term
    scored = []
    for candidate in candidates:
        stat = stats.get(candidate["source"].casefold(), {})
        scored.append(score_hashtag(
            candidate, recency_band=recency_band,
            frequency=stat.get("frequency_24h", 0), source_count=stat.get("source_count", 1),
            geographic=candidate["kind"] == "location",
            event_entity=candidate["kind"] in ("event", "entity")))
    scored.sort(key=lambda item: (-item["score"], item["tag"]))
    low, high = target
    return scored[:high] if len(scored) >= low else scored


def search_keywords(*, entities, location, event_name=None, topic_terms=(), extra=()):
    """Search keywords generated separately from hashtags (C.15). Factual only."""
    keywords, seen = [], set()

    def add(value):
        value = _flat(value)
        if value and value.casefold() not in seen:
            seen.add(value.casefold())
            keywords.append(value)

    for entity in entities or []:
        if location:
            add(f"{entity} {location}")
        add(entity)
    if event_name and location:
        add(f"{event_name} {location}")
    if location:
        add(f"{location} Andhra Pradesh")
    for term in topic_terms or []:
        add(f"Andhra Pradesh {term}")
    for value in extra:
        add(value)
    return keywords[:10]


def topic_tags(*, entities, location, event_name=None, topic_terms=()):
    tags, seen = [], set()
    for value in list(topic_terms or []) + list(entities or []) + [event_name, location]:
        value = _flat(value)
        if value and value.casefold() not in seen:
            seen.add(value.casefold())
            tags.append(value)
    return tags[:8]


# ---------- caption construction (C.9, C.10, C.17) ----------

def _grounding_text(claims):
    parts = []
    for claim in claims or []:
        parts.append(_flat(claim.get("text") if isinstance(claim, dict) else claim))
    return " ".join(parts)


def build_caption(*, claims, headline=None, entities=None, location=None, source_attribution=None,
                  context=None, language_mix="BILINGUAL", hashtags=None):
    """Assemble caption_primary / caption_short from approved claim text only (C.10).

    Structure: HOOK/HEADLINE, 1-2 sentence factual summary, optional context, attribution,
    hashtags. No praise/blame, calls to action, voting messages, slogans, or inferred reactions.
    """
    claim_texts = [(_flat(c.get("text") if isinstance(c, dict) else c)) for c in (claims or [])]
    claim_texts = [text for text in claim_texts if text]
    hook = _flat(headline) or (claim_texts[0] if claim_texts else "")
    summary = " ".join(claim_texts[:2])
    lines = []
    if hook:
        lines.append(hook)
    if summary and summary.casefold() != hook.casefold():
        lines.append(summary)
    if context:
        lines.append(_flat(context))
    if source_attribution:
        lines.append(f"Source: {_flat(source_attribution)}")
    primary = "\n\n".join(lines)
    short = " ".join(part for part in (hook, source_attribution and f"({_flat(source_attribution)})") if part)
    short = short[:180]
    return {
        "caption_primary": primary,
        "caption_short": short,
        "headline": hook,
        "language_mix": language_mix,
        "entities": list(entities or []),
        "location": location,
        "grounding": _grounding_text(claims),
    }


def build_platform_package(caption, *, platform, hashtags, keywords, attribution, topic_tags=None,
                           accessibility_text=None):
    """Platform-specific package (C.18). Instagram and Facebook are not assumed identical."""
    tag_line = " ".join(item["tag"] if isinstance(item, dict) else item for item in hashtags)
    if platform == "INSTAGRAM":
        body = "\n\n".join(part for part in (caption["caption_primary"], tag_line) if part)
        return {
            "platform": "INSTAGRAM", "caption": body, "description": None,
            "hashtags": [item["tag"] if isinstance(item, dict) else item for item in hashtags],
            "keywords": keywords, "attribution": attribution, "topic_tags": topic_tags or [],
            "alt_text": accessibility_text,
        }
    # Facebook: description-first, hashtags trimmed to the top 5 (relevance over volume).
    top = [item["tag"] if isinstance(item, dict) else item for item in hashtags][:5]
    body = "\n\n".join(part for part in (caption["caption_short"] or caption["caption_primary"],
                                         caption["caption_primary"], " ".join(top)) if part)
    deduped = []
    for line in body.split("\n\n"):
        if line not in deduped:
            deduped.append(line)
    return {
        "platform": "FACEBOOK", "caption": None, "description": "\n\n".join(deduped),
        "hashtags": top, "keywords": keywords, "attribution": attribution,
        "topic_tags": topic_tags or [], "alt_text": None,
    }


# ---------- QA (C.19) ----------

def _contains_banned(text):
    lowered = _flat(text).casefold()
    return [term for term in _BANNED_TERMS if term in lowered]


def caption_factual_qa(caption, *, claims):
    """Hard fail if the caption introduces a factual claim not in the approved claim set."""
    approved = _grounding_text(claims)
    approved_norm = re.sub(r"[^a-z0-9\u0C00-\u0C7F]+", " ", approved.casefold())
    errors = []
    body = caption["caption_primary"]
    # Every sentence must be grounded in approved claim text (verbatim phrase overlap).
    for sentence in re.split(r"(?<=[.!?])\s+|\n+", body):
        sentence = _flat(sentence)
        if not sentence or sentence.casefold().startswith("source:"):
            continue
        words = [w for w in re.findall(r"[a-z0-9\u0C00-\u0C7F]+", sentence.casefold()) if len(w) > 2]
        if not words:
            continue
        overlap = sum(1 for w in words if w in approved_norm) / len(words)
        if overlap < 0.6:
            errors.append(f"Caption sentence is not grounded in approved claims: {sentence[:80]!r}.")
    # Numbers must be present in approved text.
    approved_numbers = set(re.findall(r"\d+", approved))
    for number in re.findall(r"\d+", body):
        if number not in approved_numbers:
            errors.append(f"Caption introduces a number not in approved claims: {number!r}.")
    if _contains_banned(body):
        errors.append("Caption contains unsupported political framing.")
    if _INFERRED_REACTION.search(body):
        errors.append("Caption infers a reaction not present in approved claims.")
    return {"status": "PASS" if not errors else "FAIL", "errors": errors, "policy_version": COPY_POLICY_VERSION}


def _hashtag_words(bare):
    """Split a hashtag into lowercase words (handles camelCase and numbers)."""
    spaced = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", str(bare))
    return [w.casefold() for w in re.findall(r"[A-Za-z]+", spaced)]


def hashtag_relevance_qa(hashtags, *, entities, location, event_name=None, claims=None):
    """Hard fail if a hashtag implies unsupported political framing or is unrelated."""
    # Allowed grounded tokens: entity/location/event words + the full approved-claim vocabulary.
    allowed_sources = [v for v in list(entities or []) + [location, event_name] if v]
    allowed_tokens = set()
    for source in allowed_sources:
        allowed_tokens.update(_hashtag_words(source))
    approved = _grounding_text(claims).casefold()
    approved_tokens = set(re.findall(r"[a-z\u0C00-\u0C7F]+", approved))
    errors = []
    for item in hashtags:
        tag = item["tag"] if isinstance(item, dict) else item
        bare = tag.lstrip("#")
        if _contains_banned(bare.casefold()) or _contains_banned(tag):
            errors.append(f"Hashtag {tag} implies unsupported political framing.")
            continue
        parts = _hashtag_words(bare)
        # A hashtag is supported when at least one of its words is grounded.
        grounded_parts = [p for p in parts if p in allowed_tokens or p in approved_tokens]
        if parts and not grounded_parts:
            errors.append(f"Hashtag {tag} is not supported by the story.")
    return {"status": "PASS" if not errors else "FAIL", "errors": errors}


def attribution_qa(package, *, attribution_required, attribution_text=None):
    errors = []
    if attribution_required and not (package.get("attribution") or attribution_text):
        errors.append("Required attribution is missing from the package.")
    return {"status": "PASS" if not errors else "FAIL", "errors": errors}


def platform_copy_qa(package, *, platform):
    errors = []
    if platform == "INSTAGRAM":
        caption = package.get("caption") or ""
        if len(caption) > 2200:
            errors.append("Instagram captions are limited to 2,200 characters.")
        if caption.count("#") > 30:
            errors.append("Instagram captions are limited to 30 hashtags.")
        if not package.get("hashtags"):
            errors.append("Instagram package is missing hashtags.")
    else:
        description = package.get("description") or ""
        if len(description) > 5000:
            errors.append("Facebook descriptions are too long.")
        if len(package.get("hashtags") or []) > 5:
            errors.append("Facebook package should keep hashtags to a relevant few.")
    if not (package.get("keywords")):
        errors.append("Package is missing search keywords.")
    return {"status": "PASS" if not errors else "FAIL", "errors": errors}


def package_qa(package, *, caption, claims, entities, location, event_name=None,
               attribution_required=False):
    caption_qa = caption_factual_qa(caption, claims=claims)
    hashtag_qa = hashtag_relevance_qa(package.get("hashtags") or [], entities=entities,
                                      location=location, event_name=event_name, claims=claims)
    attribution = attribution_qa(package, attribution_required=attribution_required)
    platform = platform_copy_qa(package, platform=package["platform"])
    statuses = [caption_qa["status"], hashtag_qa["status"], attribution["status"], platform["status"]]
    return {
        "CAPTION_FACTUAL_QA": caption_qa, "HASHTAG_RELEVANCE_QA": hashtag_qa,
        "ATTRIBUTION_QA": attribution, "PLATFORM_COPY_QA": platform,
        "status": "PASS" if all(s == "PASS" for s in statuses) else "FAIL",
    }


# ---------- aggregate content discovery intelligence (D.11-D.16) ----------

def observe_topic_terms(terms, *, connect, language="ENGLISH", source_family="youtube",
                        now=None):
    """Record aggregate CONTENT signals with first/last seen + 24h/7d frequency (D.16).

    Only content terms; never viewer demographic or political profiles.
    """
    timestamp = now() if now else _now()
    with connect() as connection:
        for entry in terms or []:
            term = _flat(entry.get("term") if isinstance(entry, dict) else entry)
            if not term:
                continue
            kind = (entry.get("kind") if isinstance(entry, dict) else None) or "keyword"
            row = connection.execute(
                "SELECT * FROM content_topic_signals WHERE term=? AND term_kind=? AND language=?",
                (term, kind, language)).fetchone()
            if row:
                families = set(json.loads(row["source_families_json"] or "[]"))
                families.add(source_family)
                connection.execute(
                    "UPDATE content_topic_signals SET last_seen=?,frequency_24h=frequency_24h+1,"
                    "frequency_7d=frequency_7d+1,source_count=?,source_families_json=?,updated_at=? "
                    "WHERE id=?",
                    (timestamp, len(families), json.dumps(sorted(families)), timestamp, row["id"]))
            else:
                connection.execute(
                    "INSERT INTO content_topic_signals(id,term,term_kind,language,first_seen,last_seen,"
                    "frequency_24h,frequency_7d,source_count,source_families_json,created_at,updated_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    ("CT-" + uuid.uuid4().hex[:12].upper(), term, kind, language, timestamp, timestamp,
                     1, 1, 1, json.dumps([source_family]), timestamp, timestamp))


def decay_topics(*, connect, now=None, half_life_hours=24.0):
    """Time-aware decay (D.16): old terms lose frequency and are removed when cold."""
    reference = datetime.fromisoformat(str(now() if now else _now()).replace("Z", "+00:00"))
    removed, decayed = 0, 0
    with connect() as connection:
        for row in [dict(r) for r in connection.execute("SELECT * FROM content_topic_signals")]:
            try:
                last = datetime.fromisoformat(str(row["last_seen"]).replace("Z", "+00:00"))
            except (TypeError, ValueError):
                continue
            age_hours = max(0.0, (reference - last).total_seconds() / 3600)
            factor = 0.5 ** (age_hours / half_life_hours)
            new_24h = int(round(row["frequency_24h"] * factor)) if age_hours <= 24 else 0
            new_7d = int(round(row["frequency_7d"] * factor)) if age_hours <= 24 * 7 else 0
            if age_hours > 24 * 7 and new_7d <= 0:
                connection.execute("DELETE FROM content_topic_signals WHERE id=?", (row["id"],))
                removed += 1
                continue
            if new_24h != row["frequency_24h"] or new_7d != row["frequency_7d"]:
                connection.execute(
                    "UPDATE content_topic_signals SET frequency_24h=?,frequency_7d=?,updated_at=? WHERE id=?",
                    (new_24h, new_7d, reference.isoformat(), row["id"]))
                decayed += 1
    return {"decayed": decayed, "removed": removed}


def topic_stats(*, connect, terms):
    wanted = {_flat(t).casefold() for t in terms if t}
    with connect() as connection:
        rows = [dict(r) for r in connection.execute("SELECT * FROM content_topic_signals")]
    return {row["term"].casefold(): row for row in rows if row["term"].casefold() in wanted}


def story_topic_research(*, connect, event_id, entities, location, event_name=None,
                         claims=None, source_families=(), language_pattern="BILINGUAL", now=None):
    """Story-specific content/topic/public-interest clusters (D.23). Aggregate only."""
    timestamp = now() if now else _now()
    keywords = search_keywords(entities=entities, location=location, event_name=event_name)
    candidate_terms = candidate_hashtags(entities=entities, location=location, event_name=event_name)
    stats = topic_stats(connect=connect, terms=[c["source"] for c in candidate_terms])
    scored = select_hashtags(candidate_terms, terms=list(stats.values()))
    clusters = []
    if location:
        clusters.append({"cluster": "location", "value": location,
                         "members": list(entities or [])})
    if entities:
        clusters.append({"cluster": "entities", "value": "public figures",
                         "members": list(entities)})
    if event_name:
        clusters.append({"cluster": "event", "value": event_name, "members": []})
    result = {
        "event_id": event_id,
        "recurring_keywords": keywords,
        "relevant_hashtags": scored,
        "entity_tags": list(entities or []),
        "location_tags": [tag for tag in (location, "Andhra Pradesh") if tag],
        "language_pattern": language_pattern,
        "caption_structure": ["headline", "factual_summary", "attribution", "hashtags"],
        "clusters": clusters,
        "sources": list(dict.fromkeys(source_families)) or ["youtube", "telugu_news"],
    }
    with connect() as connection:
        connection.execute(
            "INSERT INTO story_topic_research(id,event_id,recurring_keywords_json,relevant_hashtags_json,"
            "entity_tags_json,location_tags_json,language_pattern,caption_structure_json,clusters_json,"
            "sources_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            ("SR-" + uuid.uuid4().hex[:12].upper(), event_id, json.dumps(result["recurring_keywords"]),
             json.dumps(result["relevant_hashtags"]), json.dumps(result["entity_tags"]),
             json.dumps(result["location_tags"]), language_pattern,
             json.dumps(result["caption_structure"]), json.dumps(clusters),
             json.dumps(result["sources"]), timestamp))
    return result


def content_discovery_intelligence(*, connect, event_id, entities, location, event_name=None,
                                   source_families=(), language_pattern="BILINGUAL"):
    """Aggregate content intelligence for a story (D.11). Content/topic clusters only."""
    return story_topic_research(connect=connect, event_id=event_id, entities=entities,
                                location=location, event_name=event_name,
                                source_families=source_families, language_pattern=language_pattern)
