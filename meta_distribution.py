"""Meta distribution: deterministic platform copy, platform compliance, and the Reels publisher adapter.

Verified against Meta's official documentation (Graph API v25.0):
- Instagram content publishing: POST /{ig-user-id}/media (media_type=REELS, upload_type=resumable),
  POST https://rupload.facebook.com/ig-api-upload/{version}/{container-id} (Authorization: OAuth, offset, file_size),
  GET /{container-id}?fields=status_code (EXPIRED|ERROR|FINISHED|IN_PROGRESS|PUBLISHED),
  POST /{ig-user-id}/media_publish (creation_id), GET /{ig-media-id}?fields=permalink,shortcode,
  GET /{ig-user-id}/content_publishing_limit. 100 API-published posts per 24 hours.
- Facebook Page Reels: POST /{page-id}/video_reels upload_phase=start -> video_id, upload_url;
  POST https://rupload.facebook.com/video-upload/{version}/{video-id}; GET /{video-id}?fields=status;
  POST /{page-id}/video_reels upload_phase=finish, video_id, video_state=PUBLISHED, description, title.
  30 API-published Reels per Page per 24 hours.

Copy is assembled only from approved ContentPackage fields; nothing is generated, so no new facts
can appear. Scheduling is local (never Meta-native) so the kill switches apply at publish time.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
from http.client import HTTPException, HTTPSConnection
import json
import os
import re
import socket
from urllib.parse import urlencode, urlparse

COPY_POLICY_VERSION = "meta-distribution-copy-v1"
PLATFORMS = ("INSTAGRAM_REELS", "FACEBOOK_REELS")
PLATFORM_SWITCH = {"INSTAGRAM_REELS": "INSTAGRAM_PUBLISHING_ENABLED", "FACEBOOK_REELS": "FACEBOOK_PUBLISHING_ENABLED"}
_POLITICAL_TAGS = re.compile(r"(tdp|ysrcp|ysr|bjp|congress|janasena|jsp|vote|election|party)", re.I)
_NUMBER = re.compile(r"\d[\d,.]*")
_PHRASE = re.compile(r"\b(?:[A-Z][A-Za-z]+|[A-Z]{2,})(?:[ -](?:[A-Z][A-Za-z]+|[A-Z]{2,}))+\b|\b[A-Z]{3,}\b")


def _truthy(name):
    return os.environ.get(name, "0").strip().lower() in ("1", "true", "yes", "on")


def api_version():
    return os.environ.get("META_GRAPH_API_VERSION", "v25.0").strip() or "v25.0"


def publishing_switches():
    return {
        "SOCIAL_PUBLISHING_ENABLED": _truthy("SOCIAL_PUBLISHING_ENABLED"),
        "INSTAGRAM_PUBLISHING_ENABLED": _truthy("INSTAGRAM_PUBLISHING_ENABLED"),
        "FACEBOOK_PUBLISHING_ENABLED": _truthy("FACEBOOK_PUBLISHING_ENABLED"),
    }


def platform_enabled(platform):
    switches = publishing_switches()
    return switches["SOCIAL_PUBLISHING_ENABLED"] and switches[PLATFORM_SWITCH[platform]]


def platform_configuration(platform):
    """Readiness without ever exposing tokens."""
    if platform == "INSTAGRAM_REELS":
        account = os.environ.get("INSTAGRAM_USER_ID", "").strip()
        token = bool(os.environ.get("INSTAGRAM_ACCESS_TOKEN", "").strip())
        host = os.environ.get("INSTAGRAM_GRAPH_HOST", "graph.instagram.com").strip()
        missing = [name for name, ok in (("INSTAGRAM_USER_ID", account), ("INSTAGRAM_ACCESS_TOKEN", token)) if not ok]
        return {"platform": platform, "account_id": account or None, "token_configured": token, "graph_host": host,
                "missing": missing, "enabled": platform_enabled(platform), "api_version": api_version()}
    account = os.environ.get("FACEBOOK_PAGE_ID", "").strip()
    token = bool(os.environ.get("FACEBOOK_PAGE_ACCESS_TOKEN", "").strip())
    missing = [name for name, ok in (("FACEBOOK_PAGE_ID", account), ("FACEBOOK_PAGE_ACCESS_TOKEN", token)) if not ok]
    return {"platform": platform, "account_id": account or None, "token_configured": token, "graph_host": "graph.facebook.com",
            "missing": missing, "enabled": platform_enabled(platform), "api_version": api_version()}


# ---------- deterministic copy ----------

def _approved_text(package, claims):
    parts = [package.get("headline", {}).get("text", ""), package.get("hook", {}).get("text", ""),
             package.get("caption", {}).get("text", ""), (package.get("platform_metadata") or {}).get("accessibility_text", "")]
    parts.extend(claim.get("text", "") for claim in claims)
    return "\n".join(part for part in parts if part)


def derive_hashtags(package, claims, limit=5):
    """Hashtags only from multi-word proper phrases/acronyms that appear verbatim in approved claims."""
    claims_text = " ".join(claim.get("text", "") for claim in claims)
    tags = []
    for phrase in _PHRASE.findall(claims_text):
        words = re.split(r"[ -]+", phrase)
        if words[0] in ("The", "A", "An") and len(words) > 1:
            words = words[1:]
        tag = "#" + "".join(word if word.isupper() else word[:1].upper() + word[1:] for word in words)
        if len(tag) < 4 or _POLITICAL_TAGS.search(tag) or tag.lower() in (existing.lower() for existing in tags):
            continue
        tags.append(tag)
        if len(tags) >= limit:
            break
    static = [tag.strip() for tag in os.environ.get("DISTRIBUTION_STATIC_HASHTAGS", "").split(",") if tag.strip().startswith("#")]
    return (tags + [tag for tag in static if tag.lower() not in (t.lower() for t in tags)])[: max(limit, len(static))]


def build_platform_copy(platform, package, claims, *, cover_time_ms):
    """Assemble platform copy purely from approved package fields, with provenance for each part."""
    headline = package.get("headline", {})
    caption_block = package.get("caption", {})
    hashtags = derive_hashtags(package, claims)
    body = caption_block.get("text", "").strip()
    accessibility = (package.get("platform_metadata") or {}).get("accessibility_text")
    provenance = {
        "headline": {"source": "content_package.headline", "claim_version_ids": headline.get("claim_version_ids", [])},
        "body": {"source": "content_package.caption", "claim_version_ids": caption_block.get("claim_version_ids", [])},
        "hashtags": {"source": "approved claim phrases (verbatim)", "values": hashtags},
        "accessibility_text": {"source": "content_package.platform_metadata.accessibility_text"},
    }
    tag_line = " ".join(hashtags)
    if platform == "INSTAGRAM_REELS":
        caption = "\n\n".join(part for part in (headline.get("text", "").strip(), body, tag_line) if part)
        title = None
        metadata = {"media_type": "REELS", "share_to_feed": True, "thumb_offset": cover_time_ms,
                    "accessibility_text_sent": False,
                    "accessibility_note": "The Instagram Reels publishing API exposes no alt-text field; kept for reviewers and records."}
    else:
        caption = "\n\n".join(part for part in (body, tag_line) if part)
        title = headline.get("text", "").strip()[:255] or None
        metadata = {"video_state": "PUBLISHED", "accessibility_text_sent": False,
                    "cover_note": "Facebook Reels publishing exposes no cover parameter; Meta selects the cover.",
                    "accessibility_note": "Facebook Reels publishing exposes no alt-text field; kept for reviewers and records."}
    return {
        "caption": caption, "title": title, "hashtags": hashtags, "accessibility_text": accessibility,
        "cover": {"strategy": "thumb_offset" if platform == "INSTAGRAM_REELS" else "provider_default", "time_ms": cover_time_ms},
        "platform_metadata": metadata, "provenance": provenance,
    }


def validate_copy(copy, package, claims, platform):
    """Fail-closed check that copy adds nothing: numbers, phrases, and sentences must trace to approved text."""
    approved = _approved_text(package, claims)
    approved_flat = re.sub(r"\s+", " ", approved).casefold()
    errors = []
    text = "\n".join(filter(None, (copy["caption"], copy.get("title"))))
    for number in _NUMBER.findall(text):
        if number.rstrip(".,") not in approved:
            errors.append(f"Copy introduces a number not in the approved package: {number!r}.")
    for line in re.split(r"\n+", text):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if re.sub(r"\s+", " ", line).casefold() not in approved_flat:
            errors.append(f"Copy line is not verbatim approved package text: {line[:80]!r}.")
    claims_text = " ".join(claim.get("text", "") for claim in claims).replace(" ", "").replace("-", "").casefold()
    for tag in copy["hashtags"]:
        if tag.lstrip("#").casefold() not in claims_text and tag not in os.environ.get("DISTRIBUTION_STATIC_HASHTAGS", ""):
            errors.append(f"Hashtag {tag} does not come from approved claims.")
    if platform == "INSTAGRAM_REELS":
        if len(copy["caption"]) > 2200:
            errors.append("Instagram captions are limited to 2,200 characters.")
        if copy["caption"].count("#") > 30:
            errors.append("Instagram captions are limited to 30 hashtags.")
        if copy["caption"].count("@") > 20:
            errors.append("Instagram captions are limited to 20 @ tags.")
    return {"valid": not errors, "errors": errors, "policy_version": COPY_POLICY_VERSION}


# ---------- platform compliance (from decoded file metadata, never provider claims) ----------

def check_compliance(platform, video):
    """video: decoded inspect_video() output plus file_size. Returns errors (blocking) and warnings."""
    errors, warnings = [], []
    width, height = video.get("width") or 0, video.get("height") or 0
    duration, fps = video.get("duration_seconds") or 0, video.get("frame_rate")
    if video.get("mime_type") != "video/mp4":
        errors.append("Container must be MP4.")
    if video.get("codec") not in ("avc1", "avc3", "hvc1", "hev1"):
        errors.append(f"Video codec {video.get('codec')} is not H.264/HEVC.")
    if video.get("has_audio") and video.get("audio_codec") != "mp4a":
        errors.append(f"Audio codec {video.get('audio_codec')} is not AAC.")
    if platform == "INSTAGRAM_REELS":
        if video.get("moov_before_mdat") is False:
            errors.append("Instagram requires the moov atom at the front of the file.")
        if video.get("edit_lists"):
            errors.append("Instagram Reels specs require no edit lists; this file contains edit lists (edts).")
        if fps is not None and not 23 <= fps <= 60:
            errors.append(f"Instagram Reels require 23–60 fps (file: {fps}).")
        if not 3 <= duration <= 900:
            errors.append(f"Instagram Reels must be 3 s–15 min (file: {duration}s).")
        if width > 1920:
            errors.append("Instagram Reels allow at most 1920 horizontal pixels.")
        if height and not 0.01 <= width / height <= 10:
            errors.append("Instagram aspect ratio must be between 0.01:1 and 10:1.")
        elif height and abs(width / height - 9 / 16) > 0.02:
            warnings.append(f"Not 9:16 ({width}×{height}); Instagram may show blank space or crop. Media is never altered here.")
        if (video.get("file_size") or 0) > 300 * 1024 * 1024:
            errors.append("Instagram Reels are limited to 300 MB.")
    else:
        if not height or abs(width / height - 9 / 16) > 0.02:
            errors.append(f"Facebook Reels require 9:16 (file: {width}×{height}). Media is never cropped or stretched to fit.")
        if width < 540 or height < 960:
            errors.append(f"Facebook Reels require at least 540×960 (file: {width}×{height}).")
        if fps is not None and not 24 <= fps <= 60:
            errors.append(f"Facebook Reels require 24–60 fps (file: {fps}).")
        if not 3 <= duration <= 90:
            errors.append(f"Facebook Reels must be 3–90 seconds (file: {duration}s).")
    return {"compliant": not errors, "errors": errors, "warnings": warnings, "spec_api_version": "v25.0"}


# ---------- publisher adapter ----------

class MetaError(Exception):
    code = "META_ERROR"
    retryable = False
    ambiguous = False

    def __init__(self, message, *, status=None, meta_code=None):
        super().__init__(message)
        self.status = status
        self.meta_code = meta_code


class MetaTransientError(MetaError):
    code = "META_TRANSIENT"
    retryable = True


class MetaRateLimitError(MetaTransientError):
    code = "META_RATE_LIMITED"


class MetaAuthError(MetaError):
    code = "META_AUTH_ERROR"


class MetaRejectedError(MetaError):
    code = "META_REJECTED"


class MetaAmbiguousError(MetaError):
    """Request may have been applied (timeout after send); never retried automatically."""
    code = "META_OUTCOME_UNKNOWN"
    ambiguous = True


class MetaProcessingError(MetaError):
    code = "META_PROCESSING_FAILED"


class MetaPending(Exception):
    """Provider processing is still in progress after the bounded polling window."""


def meta_https(method, url, *, headers=None, body=None, timeout_seconds=60):
    endpoint = urlparse(url)
    if endpoint.scheme != "https" or not endpoint.hostname:
        raise MetaError("Meta requests must use HTTPS.")
    path = endpoint.path + (("?" + endpoint.query) if endpoint.query else "")
    connection = HTTPSConnection(endpoint.hostname, endpoint.port or 443, timeout=min(timeout_seconds, 15))
    sent = False
    try:
        try:
            connection.connect()
            if connection.sock:
                connection.sock.settimeout(timeout_seconds)
            connection.request(method, path, body=body, headers=headers or {})
            sent = True
            response = connection.getresponse()
            raw = response.read()
        except (socket.timeout, TimeoutError, OSError, HTTPException) as error:
            if sent:
                raise MetaAmbiguousError("Meta request outcome unknown (connection ended after sending).") from error
            raise MetaTransientError("Meta could not be reached.") from error
    finally:
        connection.close()
    return response.status, {key.lower(): value for key, value in response.getheaders()}, raw


@dataclass
class PublishOutcome:
    container_id: str
    post_id: str | None
    permalink: str | None
    events: list


class MetaReelsPublisher:
    """Shared upload → bounded processing poll → publish flow with injectable transport."""

    platform = None

    def __init__(self, *, token, account_id, transport=None, poll_interval_seconds=None, max_polls=None, sleep=None):
        self._token = token
        self.account_id = account_id
        self.version = api_version()
        self._transport = transport or meta_https
        self.poll_interval_seconds = float(poll_interval_seconds if poll_interval_seconds is not None else os.environ.get("META_STATUS_POLL_INTERVAL_SECONDS", "30"))
        self.max_polls = int(max_polls if max_polls is not None else os.environ.get("META_STATUS_MAX_POLLS", "10"))
        import time as _time
        self._sleep = sleep or _time.sleep
        self.events = []

    def _record(self, event_type, status=None, **metadata):
        self.events.append({"event_type": event_type, "status": status, "metadata": metadata,
                            "at": datetime.now(timezone.utc).isoformat()})

    def _graph_url(self, host, path, params=None):
        query = ("?" + urlencode(params)) if params else ""
        return f"https://{host}/{self.version}/{path}{query}"

    def _json(self, method, url, *, form=None, headers=None, body=None, idempotent=True):
        payload = body
        all_headers = {"Authorization": "Bearer " + self._token, **(headers or {})}
        if form is not None:
            payload = urlencode(form).encode()
            all_headers["Content-Type"] = "application/x-www-form-urlencoded"
        try:
            status, response_headers, raw = self._transport(method, url, headers=all_headers, body=payload, timeout_seconds=120)
        except MetaAmbiguousError:
            if idempotent:
                raise MetaTransientError("Meta read timed out; safe to retry.")
            raise
        try:
            data = json.loads(raw.decode("utf-8") or "{}")
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise MetaTransientError(f"Meta returned non-JSON (HTTP {status}).", status=status) from error
        if status >= 400 or "error" in data:
            error = data.get("error") or {}
            meta_code, message = error.get("code"), str(error.get("message") or f"HTTP {status}")[:300]
            if status == 429 or meta_code in (4, 17, 32, 613, 80001, 80002):
                raise MetaRateLimitError(f"Meta rate limit: {message}", status=status, meta_code=meta_code)
            if status in (401, 403) or meta_code in (102, 190, 200, 10):
                raise MetaAuthError(f"Meta authorization failed: {message}", status=status, meta_code=meta_code)
            if status >= 500 or meta_code in (1, 2):
                raise MetaTransientError(f"Meta server error: {message}", status=status, meta_code=meta_code)
            raise MetaRejectedError(f"Meta rejected the request: {message}", status=status, meta_code=meta_code)
        return data

    def _upload(self, upload_url, video_bytes):
        headers = {"Authorization": "OAuth " + self._token, "offset": "0", "file_size": str(len(video_bytes)),
                   "Content-Type": "application/octet-stream"}
        try:
            status, _, raw = self._transport("POST", upload_url, headers=headers, body=video_bytes, timeout_seconds=600)
        except MetaAmbiguousError as error:
            # Re-uploading bytes to the same container/video is safe; it never creates a post.
            raise MetaTransientError("Upload outcome unknown; safe to retry the same upload session.") from error
        try:
            data = json.loads(raw.decode("utf-8") or "{}")
        except (UnicodeDecodeError, json.JSONDecodeError):
            data = {}
        if status >= 500:
            raise MetaTransientError(f"Upload failed with HTTP {status}.", status=status)
        if status >= 400 or not data.get("success"):
            raise MetaRejectedError(f"Upload was not accepted (HTTP {status}).", status=status)
        self._record("UPLOADED", "SUCCESS", bytes=len(video_bytes))


class InstagramReelsPublisher(MetaReelsPublisher):
    platform = "INSTAGRAM_REELS"

    def __init__(self, *, host=None, **kwargs):
        super().__init__(**kwargs)
        self.host = host or os.environ.get("INSTAGRAM_GRAPH_HOST", "graph.instagram.com")

    def publishing_limit(self):
        data = self._json("GET", self._graph_url(self.host, f"{self.account_id}/content_publishing_limit",
                                                 {"fields": "quota_usage,config"}))
        entry = (data.get("data") or [{}])[0]
        return {"quota_usage": entry.get("quota_usage"), "quota_total": (entry.get("config") or {}).get("quota_total")}

    def create_container(self, copy):
        params = {"media_type": "REELS", "upload_type": "resumable", "caption": copy["caption"],
                  "share_to_feed": "true" if copy["platform_metadata"].get("share_to_feed", True) else "false"}
        if copy["cover"].get("time_ms") is not None:
            params["thumb_offset"] = str(int(copy["cover"]["time_ms"]))
        # Container creation creates no post; an unknown outcome is treated as retryable.
        data = self._json("POST", self._graph_url(self.host, f"{self.account_id}/media"), form=params)
        container = data.get("id")
        if not container:
            raise MetaRejectedError("Instagram returned no container ID.")
        self._record("CONTAINER_CREATED", "SUCCESS", container_id=container)
        return container

    def upload(self, container_id, video_bytes):
        self._upload(f"https://rupload.facebook.com/ig-api-upload/{self.version}/{container_id}", video_bytes)

    def container_status(self, container_id):
        data = self._json("GET", self._graph_url(self.host, container_id, {"fields": "status_code,status"}))
        return data.get("status_code"), data.get("status")

    def wait_until_ready(self, container_id):
        for attempt in range(1, self.max_polls + 1):
            code, detail = self.container_status(container_id)
            self._record("STATUS", code, poll=attempt)
            if code == "FINISHED":
                return "FINISHED"
            if code == "PUBLISHED":
                return "PUBLISHED"
            if code in ("ERROR", "EXPIRED"):
                raise MetaProcessingError(f"Instagram container {code}: {str(detail or '')[:200]}")
            if attempt < self.max_polls:
                self._sleep(self.poll_interval_seconds)
        raise MetaPending("Instagram is still processing the container.")

    def publish(self, container_id):
        data = self._json("POST", self._graph_url(self.host, f"{self.account_id}/media_publish"),
                          form={"creation_id": container_id}, idempotent=False)
        media_id = data.get("id")
        if not media_id:
            raise MetaAmbiguousError("Instagram media_publish returned no media ID.")
        self._record("PUBLISHED", "SUCCESS", post_id=media_id)
        return media_id

    def find_published_media(self, container_id):
        """After an ambiguous publish: (published?, post_id). Never republishes and never guesses a post ID."""
        code, _ = self.container_status(container_id)
        # A PUBLISHED container proves the post exists, but Meta documents no container→media lookup.
        return code == "PUBLISHED", None

    def permalink(self, post_id):
        try:
            data = self._json("GET", self._graph_url(self.host, post_id, {"fields": "permalink,shortcode"}))
        except MetaError:
            return None
        return data.get("permalink")


class FacebookReelsPublisher(MetaReelsPublisher):
    platform = "FACEBOOK_REELS"
    host = "graph.facebook.com"

    def create_container(self, copy):
        data = self._json("POST", self._graph_url(self.host, f"{self.account_id}/video_reels"), form={"upload_phase": "start"})
        video_id = data.get("video_id")
        if not video_id:
            raise MetaRejectedError("Facebook returned no video_id.")
        self._record("CONTAINER_CREATED", "SUCCESS", container_id=video_id)
        return video_id

    def upload(self, container_id, video_bytes):
        self._upload(f"https://rupload.facebook.com/video-upload/{self.version}/{container_id}", video_bytes)

    def video_status(self, video_id):
        data = self._json("GET", self._graph_url(self.host, video_id, {"fields": "status"}))
        return data.get("status") or {}

    def wait_until_ready(self, container_id):
        for attempt in range(1, self.max_polls + 1):
            status = self.video_status(container_id)
            video_status = status.get("video_status")
            publish_status = (status.get("publishing_phase") or {}).get("publish_status")
            self._record("STATUS", video_status, poll=attempt, publish_status=publish_status)
            if publish_status == "published":
                return "PUBLISHED"
            if video_status in ("error", "expired", "upload_failed"):
                raise MetaProcessingError(f"Facebook video {video_status}.")
            if video_status in ("ready", "upload_complete") or (status.get("uploading_phase") or {}).get("status") == "complete":
                return "FINISHED"
            if attempt < self.max_polls:
                self._sleep(self.poll_interval_seconds)
        raise MetaPending("Facebook is still processing the video.")

    def publish(self, container_id, copy=None):
        form = {"upload_phase": "finish", "video_id": container_id, "video_state": "PUBLISHED",
                "description": (copy or {}).get("caption", "")}
        if (copy or {}).get("title"):
            form["title"] = copy["title"]
        data = self._json("POST", self._graph_url(self.host, f"{self.account_id}/video_reels"), form=form, idempotent=False)
        if not data.get("success"):
            raise MetaAmbiguousError("Facebook finish returned no success flag.")
        self._record("PUBLISHED", "SUCCESS", post_id=container_id)
        return container_id

    def find_published_media(self, container_id):
        status = self.video_status(container_id)
        published = (status.get("publishing_phase") or {}).get("publish_status") == "published"
        return published, container_id if published else None

    def permalink(self, post_id):
        try:
            data = self._json("GET", self._graph_url(self.host, post_id, {"fields": "permalink_url"}))
        except MetaError:
            return None
        link = data.get("permalink_url")
        if link and link.startswith("/"):
            link = "https://www.facebook.com" + link
        return link


def publisher_for(platform, transport=None, **overrides):
    configuration = platform_configuration(platform)
    if configuration["missing"]:
        raise MetaAuthError("Missing configuration: " + ", ".join(configuration["missing"]))
    if platform == "INSTAGRAM_REELS":
        return InstagramReelsPublisher(token=os.environ["INSTAGRAM_ACCESS_TOKEN"].strip(), account_id=configuration["account_id"],
                                       transport=transport, **overrides)
    return FacebookReelsPublisher(token=os.environ["FACEBOOK_PAGE_ACCESS_TOKEN"].strip(), account_id=configuration["account_id"],
                                  transport=transport, **overrides)


def content_hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
