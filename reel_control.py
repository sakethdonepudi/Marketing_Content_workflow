"""Architecture 08 — production control plane: approvals, revisions, scheduling,
notifications, cost, and the user-facing production queue.

Additive on top of AUTO_REEL_PIPELINE_V1 and REEL_PRODUCTION_STANDARD_V1. Never weakens
verification, factual QA, rights provenance, rendered-frame QA, or human review, and never
publishes without a valid approval plus configured switches and credentials.
"""

from datetime import datetime, timezone
import hashlib
import json
import sqlite3
import uuid

# User-facing queue states (backend keeps finer detail).
UI_QUEUE_STATES = ("QUEUED", "RESEARCHING", "VERIFYING", "SOURCING_MEDIA", "GENERATING", "QA",
                   "READY_FOR_REVIEW", "NEEDS_ATTENTION", "APPROVED", "SCHEDULED", "PUBLISHED")
UI_QUEUE_LABELS = {
    "QUEUED": "Queued", "RESEARCHING": "Researching", "VERIFYING": "Verifying",
    "SOURCING_MEDIA": "Sourcing media", "GENERATING": "Generating", "QA": "QA",
    "READY_FOR_REVIEW": "Ready for review", "NEEDS_ATTENTION": "Needs attention",
    "APPROVED": "Approved", "SCHEDULED": "Scheduled", "PUBLISHED": "Published",
}
# Pipeline stage -> user queue state.
_STAGE_UI = {
    "ELIGIBILITY": "QUEUED", "SOURCE_ACQUISITION": "RESEARCHING", "VERIFICATION": "VERIFYING",
    "CONTENT_DECISION": "GENERATING", "NARRATION": "GENERATING", "MEDIA_DISCOVERY": "SOURCING_MEDIA",
    "RIGHTS_CHECK": "SOURCING_MEDIA", "MEDIA_SELECTION": "SOURCING_MEDIA", "REEL_GENERATION": "GENERATING",
    "QA": "QA",
}
REVISION_CATEGORIES = ("Narration", "Visuals", "Subtitles", "Audio", "Sources", "Other")
# Failures that may be retried automatically, and those that never may.
RETRYABLE_CATEGORIES = ("timeout", "temporary_provider", "network")
NON_RETRYABLE_CATEGORIES = ("FACTUAL_QA", "RIGHTS", "HUMAN_REJECTION", "UNSUPPORTED_EVIDENCE")


class ControlError(RuntimeError):
    pass


def _now():
    return datetime.now(timezone.utc).isoformat()


def queue_state(run):
    """Map a pipeline run to a single user-facing queue state (approval/publish aware)."""
    if run.get("status") == "NEEDS_ATTENTION":
        return "NEEDS_ATTENTION"
    if run.get("published_at"):
        return "PUBLISHED"
    if run.get("scheduled_count"):
        return "SCHEDULED"
    if run.get("approved"):
        return "APPROVED"
    if run.get("status") == "READY_FOR_REVIEW":
        return "READY_FOR_REVIEW"
    return _STAGE_UI.get(run.get("current_stage"), "QUEUED")


# ---------- approvals ----------

def _render_hash(reel):
    """Stable hash of the exact reel bytes plus its lineage, so approval is version-exact."""
    parts = [reel.get("id"), str(reel.get("version_number")), reel.get("checksum_sha256"),
             reel.get("transform_hash"), reel.get("production_standard_version"),
             reel.get("source_asset_checksum_sha256")]
    return hashlib.sha256("|".join(str(p) for p in parts).encode()).hexdigest()


def approve_reel(reel_id, *, reviewer, connect, now=None):
    """Create an immutable approval bound to one exact reel version."""
    reviewer = str(reviewer or "").strip()
    if not reviewer:
        raise ControlError("A reviewer is required to approve.")
    with connect() as connection:
        reel = connection.execute("SELECT * FROM final_reel_assets WHERE id=?", (reel_id,)).fetchone()
        if reel is None:
            raise KeyError(reel_id)
        reel = dict(reel)
        if reel["status"] != "READY_FOR_REVIEW":
            raise ControlError("Only a READY_FOR_REVIEW reel can be approved.")
        render_hash = _render_hash(reel)
        version = f"{reel['id']}#{reel['checksum_sha256'][:12]}"
        approval_id = "AP-" + uuid.uuid4().hex[:12].upper()
        # Reels composed before the standard existed default to the current standard version.
        try:
            import reel_standard
            standard_version = reel.get("production_standard_version") or reel_standard.PRODUCTION_STANDARD_VERSION
        except Exception:
            standard_version = reel.get("production_standard_version") or "REEL_PRODUCTION_STANDARD_V1"
        try:
            connection.execute(
                "INSERT INTO reel_approvals(id,reel_id,reel_version,approved_at,approved_by,"
                "production_standard_version,claim_set_version,media_manifest_hash,render_hash) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (approval_id, reel_id, version, (now() if now else _now()), reviewer,
                 standard_version, str(reel.get("content_package_version")),
                 _media_manifest_hash(reel), render_hash),
            )
        except sqlite3.IntegrityError as error:  # UNIQUE(reel_id,render_hash)
            raise ControlError("This exact reel version is already approved.") from error
        row = connection.execute("SELECT * FROM reel_approvals WHERE id=?", (approval_id,)).fetchone()
    return dict(row)


def _media_manifest_hash(reel):
    try:
        manifest = json.loads(reel.get("composition_manifest_json") or "{}")
    except (TypeError, ValueError):
        manifest = {}
    return hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()


def valid_approval(reel_id, *, connect):
    """A reel is validly approved only when an unrevoked approval matches its current hash."""
    with connect() as connection:
        reel = connection.execute("SELECT * FROM final_reel_assets WHERE id=?", (reel_id,)).fetchone()
        if reel is None:
            raise KeyError(reel_id)
        reel = dict(reel)
        row = connection.execute(
            "SELECT * FROM reel_approvals WHERE reel_id=? AND revoked_at IS NULL AND render_hash=? "
            "ORDER BY approved_at DESC LIMIT 1", (reel_id, _render_hash(reel)),
        ).fetchone()
    return dict(row) if row else None


def invalidate_approvals(reel_id, *, connect, reason="A newer version was generated.", now=None):
    """Regeneration invalidates prior approvals; never mutates the reviewed reel."""
    with connect() as connection:
        connection.execute(
            "UPDATE reel_approvals SET revoked_at=?,revoked_reason=? WHERE reel_id=? AND revoked_at IS NULL",
            ((now() if now else _now()), reason, reel_id),
        )
    return True


# ---------- revision requests ----------

def request_revision(reel_id, *, categories, comment, connect, now=None):
    """Record a change request; a NEW immutable reel version resolves it. Never mutates the reel."""
    picks = [c for c in (categories or []) if c in REVISION_CATEGORIES]
    if not picks:
        raise ControlError("Select at least one category: " + ", ".join(REVISION_CATEGORIES))
    revision_id = "RR-" + uuid.uuid4().hex[:12].upper()
    with connect() as connection:
        if connection.execute("SELECT 1 FROM final_reel_assets WHERE id=?", (reel_id,)).fetchone() is None:
            raise KeyError(reel_id)
        connection.execute(
            "INSERT INTO reel_revision_requests(id,reel_id,categories_json,comment,status,created_at) "
            "VALUES(?,?,?,?, 'OPEN', ?)",
            (revision_id, reel_id, json.dumps(picks), str(comment or "").strip()[:2000] or None,
             (now() if now else _now())),
        )
        row = connection.execute("SELECT * FROM reel_revision_requests WHERE id=?", (revision_id,)).fetchone()
        notify(connection, "revision_requested", "INFO", f"Changes requested on {reel_id}",
               dedupe_key=f"rev:{revision_id}", link={"reel_id": reel_id})
    result = dict(row)
    result["categories"] = json.loads(result.pop("categories_json") or "[]")
    return result


# ---------- notifications ----------

def notify(connection, kind, severity, message, *, dedupe_key=None, link=None):
    """Internal notification; idempotent via dedupe_key. No noise for successful internal stages."""
    connection.execute(
        "INSERT OR IGNORE INTO notifications(id,kind,severity,message,link_json,dedupe_key,created_at) "
        "VALUES(?,?,?,?,?,?,?)",
        ("NT-" + uuid.uuid4().hex[:12].upper(), kind, severity, str(message)[:500],
         json.dumps(link or {}, sort_keys=True), dedupe_key, _now()),
    )


def notifications(*, connect, unread_only=False):
    with connect() as connection:
        query = "SELECT * FROM notifications"
        if unread_only:
            query += " WHERE read_at IS NULL"
        rows = [dict(r) for r in connection.execute(query + " ORDER BY created_at DESC LIMIT 100")]
    for row in rows:
        row["link"] = json.loads(row.pop("link_json") or "{}")
    return rows


# ---------- retry policy ----------

def failure_category(error):
    text = str(error).lower()
    if "timeout" in text or "timed out" in text:
        return "timeout"
    if "network" in text or "connection" in text:
        return "network"
    if any(k in text for k in ("factual_qa", "does not pass deterministic validation")):
        return "FACTUAL_QA"
    if "rights" in text or "unknown-rights" in text or "not rights-verified" in text:
        return "RIGHTS"
    if any(k in text for k in ("rate limit", "429", "503", "502", "500", "server error")):
        return "temporary_provider"
    return "other"


def is_retryable(error):
    category = failure_category(error)
    if category in NON_RETRYABLE_CATEGORIES:
        return False
    return category in RETRYABLE_CATEGORIES


def backoff_seconds(attempt, *, base=3.0, cap=60.0):
    """Bounded exponential backoff."""
    return min(cap, base * 2 ** max(0, attempt - 1))


# ---------- provider health ----------

def provider_health(*, connect, configuration):
    """Status per provider: HEALTHY / DEGRADED / UNCONFIGURED / FAILED. Never exposes keys."""
    def status(configured, failed=0, total=0):
        if not configured:
            return "UNCONFIGURED"
        if total and failed >= total:
            return "FAILED"
        if failed:
            return "DEGRADED"
        return "HEALTHY"
    with connect() as connection:
        research_failed = connection.execute(
            "SELECT COUNT(*) FROM research_runs WHERE status='FAILED'").fetchone()[0]
        research_total = connection.execute("SELECT COUNT(*) FROM research_runs").fetchone()[0]
        production_failed = connection.execute(
            "SELECT COUNT(*) FROM production_jobs WHERE status IN ('FAILED','BLOCKED')").fetchone()[0]
        production_total = connection.execute("SELECT COUNT(*) FROM production_jobs").fetchone()[0]
        render_failed = connection.execute(
            "SELECT COUNT(*) FROM render_jobs WHERE status IN ('FAILED','BLOCKED')").fetchone()[0]
        render_total = connection.execute("SELECT COUNT(*) FROM render_jobs").fetchone()[0]
    return {
        "grok": status(configuration["grok"], research_failed, research_total),
        "claude": status(configuration["claude"], production_failed, production_total),
        "edge_tts": "HEALTHY" if configuration["edge_tts"] else "UNCONFIGURED",
        "xai_image": status(configuration["xai_image"]),
        "xai_video": status(configuration["xai_video"]),
        "instagram": "HEALTHY" if configuration["instagram"] else "UNCONFIGURED",
        "facebook": "HEALTHY" if configuration["facebook"] else "UNCONFIGURED",
    }
