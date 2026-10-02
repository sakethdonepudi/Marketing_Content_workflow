"""Rights-gated real-world media discovery for Final Reel B-roll.

Candidates are FOUND, recorded, and rights-checked here, but never auto-used. Only a
candidate whose rights status permits reuse may be moved to APPROVED_FOR_USE, and only
then may it be referenced by a reel. UNKNOWN-rights media can never enter production.
"""

from datetime import datetime, timezone
import hashlib
import json
import uuid

RIGHTS_STATUSES = ("VERIFIED_REUSE", "ATTRIBUTION_REQUIRED", "USER_PROVIDED", "UNKNOWN", "REJECTED")
REUSABLE_RIGHTS = ("VERIFIED_REUSE", "ATTRIBUTION_REQUIRED", "USER_PROVIDED")
LIFECYCLE_STATES = ("DISCOVERED", "RIGHTS_CHECK", "APPROVED_FOR_USE", "INGESTED")
AP_SPECIFIC_VALUES = ("yes", "no", "unknown")


class MediaDiscoveryError(RuntimeError):
    pass


def _now():
    return datetime.now(timezone.utc).isoformat()


def rights_eligible(license_status):
    """Only user-provided, explicitly licensed, or CC/attribution-compatible media may be used."""
    return str(license_status or "").upper() in REUSABLE_RIGHTS


def register_candidate(*, connect, source_url, publisher, asset_type, title, license_status,
                       license_text=None, attribution_required=False, ap_specific="unknown",
                       real_footage=True, recommended_scene=None, state=None, district=None,
                       location_confidence="UNKNOWN", usage_scope="contextual", now=None):
    """Record a discovered candidate. Defaults to DISCOVERED; rights are never assumed."""
    license_status = str(license_status or "UNKNOWN").upper()
    if license_status not in RIGHTS_STATUSES:
        raise MediaDiscoveryError(f"Unknown rights status {license_status!r}.")
    ap_specific = (str(ap_specific or "unknown").lower())
    if ap_specific not in AP_SPECIFIC_VALUES:
        ap_specific = "unknown"
    timestamp = now() if now else _now()
    candidate_id = "MC-" + uuid.uuid4().hex[:12].upper()
    record = {
        "id": candidate_id, "source_url": source_url, "publisher": publisher, "asset_type": asset_type,
        "title": title, "license_status": license_status, "license_text": license_text,
        "attribution_required": int(bool(attribution_required)), "ap_specific": ap_specific,
        "real_footage": int(bool(real_footage)), "recommended_scene": recommended_scene,
        "state": state, "district": district, "location_confidence": str(location_confidence or "UNKNOWN").upper(),
        "usage_scope": usage_scope, "verification_status": "UNVERIFIED",
        "lifecycle_state": "DISCOVERED", "content_hash": None, "downloaded_at": None,
        "reviewed_by": None, "reviewed_at": None, "created_at": timestamp,
    }
    fields = ",".join(record)
    with connect() as connection:
        existing = connection.execute(
            "SELECT * FROM media_candidates WHERE source_url=?", (source_url,)
        ).fetchone()
        if existing:
            return dict(existing)
        connection.execute(
            f"INSERT INTO media_candidates({fields}) VALUES({','.join('?' for _ in record)})",
            tuple(record.values()),
        )
        row = connection.execute("SELECT * FROM media_candidates WHERE id=?", (candidate_id,)).fetchone()
    return dict(row)


def rights_check(candidate_id, *, connect, license_status, license_text=None, attribution_required=None,
                 verification_status="CHECKED"):
    """Record the outcome of a rights review without approving for use."""
    with connect() as connection:
        connection.execute(
            "UPDATE media_candidates SET license_status=?, license_text=COALESCE(?,license_text),"
            "attribution_required=COALESCE(?,attribution_required),verification_status=?,"
            "lifecycle_state=CASE WHEN lifecycle_state='DISCOVERED' THEN 'RIGHTS_CHECK' ELSE lifecycle_state END "
            "WHERE id=?",
            (str(license_status).upper(), license_text,
             None if attribution_required is None else int(bool(attribution_required)),
             verification_status, candidate_id),
        )
        row = connection.execute("SELECT * FROM media_candidates WHERE id=?", (candidate_id,)).fetchone()
    if row is None:
        raise KeyError(candidate_id)
    return dict(row)


def approve_for_use(candidate_id, *, connect, reviewer, now=None):
    """Move a rights-eligible candidate to APPROVED_FOR_USE. UNKNOWN rights are refused."""
    with connect() as connection:
        row = connection.execute("SELECT * FROM media_candidates WHERE id=?", (candidate_id,)).fetchone()
        if row is None:
            raise KeyError(candidate_id)
        if not rights_eligible(row["license_status"]):
            raise MediaDiscoveryError(
                f"Candidate {candidate_id} has rights status {row['license_status']}; it cannot enter production."
            )
        connection.execute(
            "UPDATE media_candidates SET lifecycle_state='APPROVED_FOR_USE',verification_status='APPROVED',"
            "reviewed_by=?,reviewed_at=? WHERE id=?",
            (reviewer, (now() if now else _now()), candidate_id),
        )
        updated = connection.execute("SELECT * FROM media_candidates WHERE id=?", (candidate_id,)).fetchone()
    return dict(updated)


def reject_candidate(candidate_id, *, connect, reviewer, reason=None, now=None):
    with connect() as connection:
        connection.execute(
            "UPDATE media_candidates SET lifecycle_state='REJECTED',license_status=CASE WHEN license_status='UNKNOWN' "
            "THEN 'REJECTED' ELSE license_status END,reviewed_by=?,reviewed_at=?,"
            "license_text=COALESCE(?,license_text) WHERE id=?",
            (reviewer, (now() if now else _now()), reason, candidate_id),
        )
    return candidate(candidate_id, connect=connect)


def candidate(candidate_id, *, connect):
    with connect() as connection:
        row = connection.execute("SELECT * FROM media_candidates WHERE id=?", (candidate_id,)).fetchone()
    if row is None:
        raise KeyError(candidate_id)
    return dict(row)


def list_candidates(*, connect, lifecycle_state=None):
    with connect() as connection:
        if lifecycle_state:
            rows = connection.execute(
                "SELECT * FROM media_candidates WHERE lifecycle_state=? ORDER BY created_at DESC", (lifecycle_state,)
            ).fetchall()
        else:
            rows = connection.execute("SELECT * FROM media_candidates ORDER BY created_at DESC").fetchall()
    return [dict(row) for row in rows]


def candidate_report(*, connect):
    """Counts for the human-review media report."""
    candidates = list_candidates(connect=connect)
    def count(predicate):
        return sum(1 for item in candidates if predicate(item))
    return {
        "total": len(candidates),
        "ap_specific": count(lambda c: c["ap_specific"] == "yes"),
        "real_footage": count(lambda c: c["real_footage"] == 1),
        "verified_reuse": count(lambda c: c["license_status"] == "VERIFIED_REUSE"),
        "attribution_required": count(lambda c: c["license_status"] == "ATTRIBUTION_REQUIRED"),
        "user_provided": count(lambda c: c["license_status"] == "USER_PROVIDED"),
        "unknown": count(lambda c: c["license_status"] == "UNKNOWN"),
        "approved_for_use": count(lambda c: c["lifecycle_state"] == "APPROVED_FOR_USE"),
        "candidates": candidates,
    }


def approved_candidates(*, connect):
    """Only candidates cleared for production; the composer may reference these alone."""
    return [c for c in list_candidates(connect=connect) if c["lifecycle_state"] == "APPROVED_FOR_USE"]
