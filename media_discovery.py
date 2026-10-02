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


def ingested_candidates(*, connect):
    """Approved candidates whose bytes have been stored locally."""
    return [c for c in list_candidates(connect=connect) if c["lifecycle_state"] == "INGESTED"]


MAX_DOWNLOAD_BYTES = 12_000_000


def _resolve_direct_url(url, *, user_agent, timeout=60, thumb_width=None):
    """Resolve a Wikimedia Commons file page to a direct URL (a scaled thumbnail when asked)."""
    from urllib.parse import urlparse, urlencode, urlunparse
    from urllib.request import Request, build_opener
    parsed = urlparse(url)
    if parsed.hostname == "upload.wikimedia.org":
        return urlunparse(parsed._replace(query=""))
    if parsed.hostname and "wikimedia.org" in parsed.hostname and parsed.path.startswith("/wiki/File:"):
        title = parsed.path.split("/wiki/", 1)[1]
        api = ("https://commons.wikimedia.org/w/api.php?action=query&format=json&prop=imageinfo&iiprop=url"
               + (f"&iiurlwidth={int(thumb_width)}" if thumb_width else "")
               + "&titles=" + urlencode({"": title}).lstrip("="))
        request = Request(api, headers={"User-Agent": user_agent})
        with build_opener().open(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
        for page in (payload.get("query") or {}).get("pages", {}).values():
            info = (page.get("imageinfo") or [{}])[0]
            candidate = info.get("thumburl") or info.get("url")
            if candidate:
                return urlunparse(urlparse(candidate)._replace(query=""))
        raise MediaDiscoveryError("Could not resolve a direct media URL for the Commons file page.")
    return urlunparse(parsed._replace(query=""))


def _download(url, *, user_agent, timeout=60, resolver=None, retries=4, sleep=None, thumb_width=2000):
    """Bounded HTTPS download of raw media bytes with backoff for provider rate limits.

    A scaled thumbnail is preferred for very large originals so a 20 MB+ Commons file does
    not exceed the ingest cap; 2000 px is far more than 720x1280 output needs.
    """
    import time as _time
    from urllib.error import HTTPError
    from urllib.parse import urlparse
    from urllib.request import Request, build_opener
    direct = (resolver or _resolve_direct_url)(url, user_agent=user_agent, thumb_width=thumb_width)
    parsed = urlparse(direct)
    if parsed.scheme != "https" or not parsed.hostname:
        raise MediaDiscoveryError("Only HTTPS media URLs may be ingested.")
    pause = sleep or _time.sleep
    last_error = None
    for attempt in range(1, retries + 1):
        request = Request(direct, headers={"User-Agent": user_agent, "Accept": "image/*"})
        try:
            with build_opener().open(request, timeout=timeout) as response:
                data = response.read(MAX_DOWNLOAD_BYTES + 1)
            break
        except HTTPError as error:
            last_error = error
            if error.code not in (429, 500, 502, 503, 504) or attempt == retries:
                raise
            pause(min(30.0, 3.0 * 2 ** (attempt - 1)))
    else:  # pragma: no cover - loop always breaks or raises
        raise MediaDiscoveryError(f"Download failed: {last_error}")
    if len(data) > MAX_DOWNLOAD_BYTES:
        raise MediaDiscoveryError("Media exceeds the 12 MB ingest cap.")
    if not data:
        raise MediaDiscoveryError("Downloaded media was empty.")
    return data


def ingest_approved_candidate(candidate_id, *, connect, storage, user_agent="ReachOut-OS/0.3 (media ingest)",
                              attribution_text=None, downloader=None, now=None):
    """Download and store bytes for an APPROVED_FOR_USE candidate, then mark it INGESTED."""
    row = candidate(candidate_id, connect=connect)
    if row["lifecycle_state"] not in ("APPROVED_FOR_USE", "INGESTED"):
        raise MediaDiscoveryError("Only APPROVED_FOR_USE media can be ingested.")
    if not rights_eligible(row["license_status"]):
        raise MediaDiscoveryError("UNKNOWN-rights media can never be ingested.")
    if row["lifecycle_state"] == "INGESTED":
        return row
    fetch = downloader or (lambda url: _download(url, user_agent=user_agent))
    data = fetch(row["source_url"])
    try:
        from media_inspection import inspect_image
        decoded = inspect_image(data)
    except Exception as error:  # noqa: BLE001
        raise MediaDiscoveryError(f"Downloaded media is not a decodable image: {error}") from error
    extension = "jpg" if decoded.get("format") == "JPEG" else ("png" if decoded.get("format") == "PNG" else "webp")
    stored = storage.save(data, extension=extension, metadata={"purpose": "REAL_MEDIA", "candidate": candidate_id})
    timestamp = (now() if now else _now())
    with connect() as connection:
        connection.execute(
            "UPDATE media_candidates SET lifecycle_state='INGESTED',storage_uri=?,mime_type=?,width=?,height=?,"
            "file_size=?,content_hash=?,downloaded_at=?,attribution_text=COALESCE(?,attribution_text) WHERE id=?",
            (stored.storage_uri, decoded.get("mime_type") or "image/jpeg", decoded.get("width"),
             decoded.get("height"), stored.file_size, stored.checksum_sha256, timestamp,
             attribution_text, candidate_id),
        )
    return candidate(candidate_id, connect=connect)
