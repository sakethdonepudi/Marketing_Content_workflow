"""Architecture 13 — Latest Updates (verified-news text feed) + private upload panel.

The site shows factual pipeline data only. A story appears as TEXT as soon as it is VERIFIED,
without waiting for a reel. VERIFYING information is never presented as confirmed news.
No secrets, no demographic/political inference, no unsupported conclusions.
"""

import json
import re
from datetime import datetime, timezone


# Ordered by how "news-ready" a state is (VERIFIED first for the default view).
NEWS_STATES = ("VERIFIED", "REVIEW_REQUIRED", "VERIFYING", "DISCOVERED")


def _now():
    return datetime.now(timezone.utc).isoformat()


def _norm(value):
    return re.sub(r"\s+", " ", str(value or "")).strip()


def story_state(event, *, research_status=None):
    """Map raw pipeline state to a single user-facing news state (label is always explicit)."""
    if event["verification_status"] == "VERIFIED":
        return "VERIFIED"
    if event["verification_status"] in ("REVIEW_REQUIRED", "FAILED"):
        return "REVIEW_REQUIRED"
    if event["verification_status"] in ("QUEUED", "RUNNING") or event["status"] == "VERIFYING":
        return "VERIFYING"
    return "DISCOVERED"


STATE_LABELS = {"VERIFIED": "Verified", "REVIEW_REQUIRED": "Needs review",
                "VERIFYING": "Verifying", "DISCOVERED": "Discovered"}


def verified_summary(event_id, *, connect, limit=2):
    """A factual summary assembled only from APPROVED claim text for a VERIFIED event."""
    with connect() as connection:
        claims = [row["text"] for row in connection.execute(
            "SELECT cv.text FROM approved_claim_set_items aci "
            "JOIN claim_versions cv ON cv.id=aci.claim_version_id "
            "JOIN approved_claim_sets acs ON acs.id=aci.claim_set_id "
            "WHERE acs.event_id=? AND acs.status='APPROVED' ORDER BY aci.claim_version_id LIMIT ?",
            (event_id, limit))]
    return " ".join(_norm(text) for text in claims if text).strip()


def latest_updates(*, connect, include_unverified=True, limit=20):
    """Latest Updates cards: factual pipeline data only, VERIFIED prioritized by default."""
    with connect() as connection:
        events = [dict(row) for row in connection.execute(
            "SELECT * FROM events ORDER BY updated_at DESC LIMIT ?", (limit * 2,))]
        cards = []
        for event in events:
            state = story_state(event)
            if state != "VERIFIED" and not include_unverified:
                continue
            candidate = connection.execute(
                "SELECT * FROM event_candidates WHERE event_id=? ORDER BY rowid DESC LIMIT 1",
                (event["id"],)).fetchone()
            source_count = connection.execute(
                "SELECT COUNT(DISTINCT COALESCE(source_id,source_name)) FROM signals WHERE event_id=?",
                (event["id"],)).fetchone()[0]
            primary = connection.execute(
                "SELECT source_name FROM signals WHERE event_id=? AND source_class='official_primary' "
                "ORDER BY detected_at LIMIT 1", (event["id"],)).fetchone()
            reel = connection.execute(
                "SELECT id,status FROM final_reel_assets WHERE event_id=? ORDER BY created_at DESC LIMIT 1",
                (event["id"],)).fetchone()
            summary = verified_summary(event["id"], connect=connect) if state == "VERIFIED" else ""
            if not summary:
                summary = _norm(event.get("title") or "")
            location = (candidate["location"] if candidate else None)
            entities = json.loads(candidate["entities_json"] or "[]") if candidate else []
            topic = entities[0] if entities else None
            cards.append({
                "event_id": event["id"], "headline": _norm(event.get("title")),
                "summary": summary[:400], "location": location, "topic": topic,
                "state": state, "state_label": STATE_LABELS[state],
                "updated_at": event.get("updated_at"), "source_count": source_count,
                "primary_source": (primary["source_name"] if primary else None),
                "reel_status": (reel["status"] if reel else None),
                "reel_id": (reel["id"] if reel else None),
            })
    order = {state: index for index, state in enumerate(NEWS_STATES)}
    cards.sort(key=lambda card: (order.get(card["state"], 99), card["updated_at"] or ""), reverse=False)
    # VERIFIED first, then most-recent within each state.
    verified = [c for c in cards if c["state"] == "VERIFIED"]
    rest = [c for c in cards if c["state"] != "VERIFIED"]
    verified.sort(key=lambda c: c["updated_at"] or "", reverse=True)
    rest.sort(key=lambda c: c["updated_at"] or "", reverse=True)
    return (verified + rest)[:limit]


def story_detail(event_id, *, connect):
    """Story detail: factual fields at top; technical internals separated for Advanced."""
    with connect() as connection:
        event = connection.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
        if event is None:
            return None
        event = dict(event)
        candidate = connection.execute(
            "SELECT * FROM event_candidates WHERE event_id=? ORDER BY rowid DESC LIMIT 1", (event_id,)).fetchone()
        claims = [dict(row) for row in connection.execute(
            "SELECT cv.text, aci.verification_decision_id FROM approved_claim_set_items aci "
            "JOIN claim_versions cv ON cv.id=aci.claim_version_id "
            "JOIN approved_claim_sets acs ON acs.id=aci.claim_set_id "
            "WHERE acs.event_id=? AND acs.status='APPROVED' ORDER BY aci.claim_version_id", (event_id,))]
        sources = [dict(row) for row in connection.execute(
            "SELECT source_name,source_class,url,publication_time FROM signals WHERE event_id=? "
            "ORDER BY detected_at", (event_id,))]
        reel = connection.execute(
            "SELECT id,status,width,height,duration_seconds FROM final_reel_assets WHERE event_id=? "
            "ORDER BY created_at DESC LIMIT 1", (event_id,)).fetchone()
        upload = None
        if reel:
            upload = connection.execute(
                "SELECT id,status,privacy_status,video_id,processing_state,uploaded_at FROM "
                "youtube_performance_snapshots WHERE reel_id=? ORDER BY recorded_at DESC LIMIT 1",
                (reel["id"],)).fetchone()
            if upload is None:
                upload = connection.execute(
                    "SELECT id,status,privacy_status,video_id,uploaded_at FROM youtube_publish_jobs "
                    "WHERE reel_id=? ORDER BY created_at DESC LIMIT 1", (reel["id"],)).fetchone()
    state = story_state(event)
    return {
        "event_id": event_id, "headline": _norm(event.get("title")),
        "state": state, "state_label": STATE_LABELS[state],
        "summary": verified_summary(event_id, connect=connect) if state == "VERIFIED" else "",
        "approved_claims": [c["text"] for c in claims],
        "sources": [{"name": s["source_name"], "class": s["source_class"], "url": s["url"],
                     "published": s["publication_time"]} for s in sources],
        "location": (candidate["location"] if candidate else None),
        "updated_at": event.get("updated_at"),
        "media_status": (reel["status"] if reel else None),
        "reel_id": (reel["id"] if reel else None),
        "youtube": (dict(upload) if upload else None),
        "advanced": {"verification_status": event.get("verification_status"),
                     "research_status": event.get("research_status"),
                     "content_decision_status": event.get("content_decision_status")},
    }


def private_uploads(*, connect, limit=20):
    """PRIVATE YouTube uploads for the operator panel (aggregate, no secrets)."""
    with connect() as connection:
        rows = [dict(row) for row in connection.execute(
            "SELECT j.id,j.reel_id,j.privacy_status,j.video_id,j.uploaded_at,j.event_id,j.status,"
            "p.title_primary, r.duration_seconds, r.width, r.height, s.views, s.processing_state "
            "FROM youtube_publish_jobs j "
            "LEFT JOIN youtube_packages p ON p.id=j.youtube_package_id "
            "LEFT JOIN final_reel_assets r ON r.id=j.reel_id "
            "LEFT JOIN youtube_performance_snapshots s ON s.youtube_publish_job_id=j.id "
            "WHERE j.status='PUBLISHED' AND j.privacy_status='PRIVATE' "
            "ORDER BY j.uploaded_at DESC LIMIT ?", (limit,))]
    uploads = []
    for row in rows:
        uploads.append({
            "job_id": row["id"], "reel_id": row["reel_id"], "event_id": row["event_id"],
            "title": row["title_primary"], "video_id": row["video_id"],
            "privacy": row["privacy_status"], "uploaded_at": row["uploaded_at"],
            "duration_seconds": row["duration_seconds"], "width": row["width"], "height": row["height"],
            "processing_state": row["processing_state"] or row["status"],
            "views": row["views"], "youtube_url": (f"https://www.youtube.com/watch?v={row['video_id']}"
                                                   if row["video_id"] else None),
        })
    return uploads
