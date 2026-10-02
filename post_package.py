"""Architecture 10 — POST_PACKAGE_V1 service.

Persists caption/hashtag packages for a reel, versioned independently of reel approval.
Changing copy invalidates only POST_COPY_APPROVED (never reel approval); changing the reel
invalidates both. Publishing (off by default) requires REEL_APPROVED + POST_COPY_APPROVED.
"""

from datetime import datetime, timezone
import json
import uuid

import caption_intelligence as ci


def _now():
    return datetime.now(timezone.utc).isoformat()


def _row_id(prefix):
    return prefix + uuid.uuid4().hex[:12].upper()


def create_package(*, connect, reel_id, event_id, platform, caption, hashtags, keywords,
                   topic_tags, attribution, language_mix="BILINGUAL", entities=None,
                   location=None, event_name=None, attribution_required=False, claims=None, now=None):
    """Create a versioned post package and run the mandatory copy QA (C.19)."""
    timestamp = now() if now else _now()
    hashtag_list = [item["tag"] if isinstance(item, dict) else item for item in hashtags]
    keywords = list(keywords or [])
    topic_tags = list(topic_tags or [])
    # Build a platform package shell for QA.
    platform_pkg = {
        "platform": platform,
        "caption": caption.get("caption_primary") if platform == "INSTAGRAM" else None,
        "description": None if platform == "INSTAGRAM" else caption.get("caption_primary"),
        "hashtags": hashtag_list, "keywords": keywords, "attribution": attribution,
        "topic_tags": topic_tags,
    }
    qa = ci.package_qa(platform_pkg, caption=caption, claims=claims, entities=entities,
                       location=location, event_name=event_name,
                       attribution_required=attribution_required)
    package_id = _row_id("PP-")
    with connect() as connection:
        version = connection.execute(
            "SELECT COALESCE(MAX(version_number),0)+1 FROM post_packages WHERE reel_id=? AND platform=?",
            (reel_id, platform)).fetchone()[0]
        connection.execute(
            "INSERT INTO post_packages(id,reel_id,event_id,version_number,platform,caption_primary,"
            "caption_short,headline,hashtags_json,search_keywords_json,topic_tags_json,source_attribution,"
            "language_mix,qa_json,status,copied_label,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,'DRAFT',NULL,?)",
            (package_id, reel_id, event_id, version, platform, caption["caption_primary"],
             caption.get("caption_short"), caption.get("headline"), json.dumps(hashtag_list),
             json.dumps(keywords), json.dumps(topic_tags), attribution, language_mix,
             json.dumps(qa), timestamp))
        _snapshot_revision(connection, package_id, version, caption, hashtag_list, keywords,
                           topic_tags, edited_by=None, now=timestamp)
    return package(package_id, connect=connect)


def _snapshot_revision(connection, package_id, version, caption, hashtags, keywords, topic_tags,
                       edited_by, now):
    connection.execute(
        "INSERT INTO post_package_revisions(id,post_package_id,version_number,caption_primary,"
        "caption_short,headline,hashtags_json,search_keywords_json,topic_tags_json,edited_by,edited_at,created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (_row_id("PR-"), package_id, version, caption["caption_primary"], caption.get("caption_short"),
         caption.get("headline"), json.dumps(hashtags), json.dumps(keywords), json.dumps(topic_tags),
         edited_by, now, now))


def package(package_id, *, connect):
    with connect() as connection:
        row = connection.execute("SELECT * FROM post_packages WHERE id=?", (package_id,)).fetchone()
        if row is None:
            raise KeyError(package_id)
        result = dict(row)
        result["hashtags"] = json.loads(result.pop("hashtags_json") or "[]")
        result["search_keywords"] = json.loads(result.pop("search_keywords_json") or "[]")
        result["topic_tags"] = json.loads(result.pop("topic_tags_json") or "[]")
        result["qa"] = json.loads(result.pop("qa_json") or "{}")
        # Revision history across the whole platform lineage for this reel (C.21).
        result["revisions"] = [dict(r) for r in connection.execute(
            "SELECT r.* FROM post_package_revisions r JOIN post_packages p ON p.id=r.post_package_id "
            "WHERE p.reel_id=? AND p.platform=? ORDER BY r.version_number,r.created_at",
            (result["reel_id"], result["platform"]))]
    return result


def packages_for_reel(reel_id, *, connect):
    with connect() as connection:
        rows = connection.execute(
            "SELECT id FROM post_packages WHERE reel_id=? ORDER BY platform,version_number DESC",
            (reel_id,)).fetchall()
    return [package(row["id"], connect=connect) for row in rows]


def latest_package(reel_id, platform, *, connect):
    with connect() as connection:
        row = connection.execute(
            "SELECT id FROM post_packages WHERE reel_id=? AND platform=? AND status!='SUPERSEDED' "
            "ORDER BY version_number DESC LIMIT 1", (reel_id, platform)).fetchone()
    return package(row["id"], connect=connect) if row else None


def edit_package(package_id, *, caption=None, hashtags=None, keyword_list=None, topic_tag_list=None,
                 edited_by, connect, now=None):
    """Human copy edit -> NEW version. Invalidates POST_COPY_APPROVED only, never reel approval."""
    timestamp = now() if now else _now()
    current = package(package_id, connect=connect)
    caption_obj = {
        "caption_primary": (caption or current["caption_primary"]),
        "caption_short": current.get("caption_short"),
        "headline": current.get("headline"),
    }
    hashtag_list = list(hashtags if hashtags is not None else current["hashtags"])
    keywords = list(keyword_list if keyword_list is not None else current["search_keywords"])
    topic_tags = list(topic_tag_list if topic_tag_list is not None else current["topic_tags"])
    # Re-run QA against the event's approved claims so an edit cannot smuggle in a new claim.
    with connect() as connection:
        claims = [dict(r) for r in connection.execute(
            "SELECT cv.text FROM approved_claim_set_items aci "
            "JOIN claim_versions cv ON cv.id=aci.claim_version_id "
            "JOIN approved_claim_sets acs ON acs.id=aci.claim_set_id "
            "WHERE acs.event_id=? AND acs.status='APPROVED' ORDER BY aci.claim_version_id",
            (current["event_id"],))]
    qa = ci.package_qa({"platform": current["platform"], "hashtags": hashtag_list, "keywords": keywords,
                        "caption": caption_obj["caption_primary"],
                        "description": caption_obj["caption_primary"] if current["platform"] == "FACEBOOK" else None,
                        "attribution": current.get("source_attribution")},
                       caption=caption_obj, claims=claims, entities=None, location=None)
    with connect() as connection:
        # Supersede the previous version and record a new one.
        connection.execute("UPDATE post_packages SET status='SUPERSEDED' WHERE id=?", (package_id,))
        new_id = _row_id("PP-")
        version = (current["version_number"] or 1) + 1
        connection.execute(
            "INSERT INTO post_packages(id,reel_id,event_id,version_number,platform,caption_primary,"
            "caption_short,headline,hashtags_json,search_keywords_json,topic_tags_json,source_attribution,"
            "language_mix,qa_json,status,copied_label,created_at,edited_by,edited_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,'DRAFT',NULL,?,?,?)",
            (new_id, current["reel_id"], current["event_id"], version, current["platform"],
             caption_obj["caption_primary"], caption_obj.get("caption_short"), caption_obj.get("headline"),
             json.dumps(hashtag_list), json.dumps(keywords), json.dumps(topic_tags),
             current.get("source_attribution"), current["language_mix"], json.dumps(qa), timestamp,
             edited_by, timestamp))
        _snapshot_revision(connection, new_id, version, caption_obj, hashtag_list, keywords, topic_tags,
                           edited_by=edited_by, now=timestamp)
    return package(new_id, connect=connect)


def approve_copy(package_id, *, reviewer, connect, now=None):
    """Approve the POST COPY only (independent of reel approval)."""
    timestamp = now() if now else _now()
    current = package(package_id, connect=connect)
    if current["qa"].get("status") != "PASS":
        raise ValueError("Copy QA must pass before post-copy approval.")
    with connect() as connection:
        connection.execute("UPDATE post_packages SET status='POST_COPY_APPROVED',copied_label=? WHERE id=?",
                           (reviewer, package_id))
    return package(package_id, connect=connect)


def copy_approval_state(*, connect, reel_id):
    """(reel_approved, post_copy_approved) for the publish-readiness gate (C.22)."""
    with connect() as connection:
        reel_approved = connection.execute(
            "SELECT 1 FROM reel_approvals WHERE reel_id=? AND revoked_at IS NULL LIMIT 1",
            (reel_id,)).fetchone() is not None
        approved = connection.execute(
            "SELECT platform FROM post_packages WHERE reel_id=? AND status='POST_COPY_APPROVED'",
            (reel_id,)).fetchall()
    return {"reel_approved": reel_approved,
            "post_copy_approved": sorted({row["platform"] for row in approved}),
            "ready_to_publish": reel_approved and bool(approved)}


def publishing_allowed(reel_id, platform, *, connect):
    """Publishing requires BOTH reel approval and post-copy approval (C.22)."""
    state = copy_approval_state(connect=connect, reel_id=reel_id)
    return state["reel_approved"] and platform in state["post_copy_approved"]
