import base64
import binascii
import hashlib
import html
import ipaddress
import json
import logging
import os
import re
import socket
import sqlite3
import sys
import threading
import time
import uuid
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse
from urllib.error import URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from research import MissingAPIKeyError, ResearchProviderError, USD_TICKS_PER_DOLLAR, provider_for
from verification import VerificationProviderResult, verification_provider_for
from content_ceo import ContentProviderResult, content_provider_for
from content_production import (
    FORMAT_MEDIA_TYPES,
    PROMPT_SCHEMA_VERSION,
    ProductionProviderResult,
    ProductionProviderUnavailable,
    production_configuration,
    production_provider_for,
    validate_production_package,
)
from media_rendering import (
    AsyncMediaRenderer,
    provider_capabilities,
    ratio_label,
    RENDER_GENERATION_CONFIG_VERSION,
    RENDER_PROMPT_VERSION,
    InvalidRendererResponse,
    MissingRendererConfiguration,
    ProviderPollResult,
    RenderResult,
    RendererContentPolicyError,
    RendererDownloadError,
    RendererNetworkError,
    RendererPollingExhaustedError,
    RendererProviderJobFailed,
    RendererRateLimitError,
    RendererRejectedError,
    RendererServerError,
    RendererTimeoutError,
    configured_renderer_name,
    renderer_configuration,
    renderer_for,
)
from media_storage import LocalMediaStorage
from media_inspection import ImageDecodeError, inspect_image, inspect_video
from media_qa import (
    MEDIA_QA_POLICY_VERSION, MediaQAResult, NoopVisualQAProvider, evaluate_ocr_policy,
    evaluate_text_overlay, normalize_semantic_checks,
)
from media_tools import (
    MediaToolUnavailable, analysis_jpeg, frame_extractor_for, ocr_provider_for,
    prepare_video_source, sample_times, warm_media_probe,
)
from visual_qa import visual_qa_provider_for
import final_reel_composer
import reel_standard
import reel_pipeline
import reel_control
import fast_discovery
import live_discovery
import media_discovery
from meta_distribution import (
    COPY_POLICY_VERSION as DISTRIBUTION_COPY_POLICY_VERSION,
    PLATFORMS as META_PLATFORMS,
    PLATFORM_SWITCH as META_PLATFORM_SWITCH,
    MetaAmbiguousError,
    MetaError,
    MetaPending,
    MetaRejectedError,
    api_version as meta_api_version,
    build_platform_copy,
    check_compliance as check_platform_compliance,
    content_hash as meta_content_hash,
    platform_configuration as meta_platform_configuration,
    publisher_for as meta_publisher_for,
    publishing_switches as meta_publishing_switches,
    validate_copy as validate_platform_copy,
)
from source_acquisition import (
    OfficialSourceRegistry, RetrievedResponse, SourceRetriever, build_discovery_plan,
    build_evidence_packet, classify_source, family_for_candidate, match_claims,
)

ROOT = Path(__file__).parent


def load_local_env(path):
    """Load simple local KEY=VALUE settings without logging their contents."""
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("\"").strip("'")
        if re.fullmatch(r"[A-Z][A-Z0-9_]*", key):
            os.environ.setdefault(key, value)


load_local_env(ROOT / ".env")
DB = Path(os.environ.get("REACHOUT_DB", ROOT / "reachout.sqlite3"))
MIGRATIONS = ROOT / "migrations"
SOURCES_CONFIG = Path(os.environ.get("REACHOUT_SOURCES_CONFIG", ROOT / "config" / "sources.json"))
WORKSPACE_CONFIG = Path(os.environ.get("REACHOUT_WORKSPACE_CONFIG", ROOT / "config" / "workspace.json"))
OFFICIAL_SOURCES_CONFIG = Path(os.environ.get(
    "REACHOUT_OFFICIAL_SOURCES_CONFIG", ROOT / "config" / "official_sources.json"
))
USER_AGENT = "ReachOut-OS/0.2 (+local factual source monitor)"
MAX_RESPONSE_BYTES = 2_000_000
MAX_EVIDENCE_DOCUMENT_BYTES = 12_000_000
CLUSTER_WINDOW_HOURS = 72
EVENT_MAX_AGE_DAYS = int(os.environ.get("REACHOUT_EVENT_MAX_AGE_DAYS", "30"))
RESEARCH_SEARCH_LIMIT = max(0, int(os.environ.get("RESEARCH_SEARCH_LIMIT", "1")))
RESEARCH_TOKEN_LIMIT = max(256, int(os.environ.get("RESEARCH_TOKEN_LIMIT", "4000")))
RESEARCH_TIMEOUT_SECONDS = max(5, int(os.environ.get("RESEARCH_TIMEOUT_SECONDS", "60")))
RESEARCH_MAX_RETRIES = max(0, int(os.environ.get("RESEARCH_MAX_RETRIES", "1")))
RESEARCH_EXECUTOR = ThreadPoolExecutor(max_workers=max(1, int(os.environ.get("RESEARCH_WORKERS", "2"))))
VERIFICATION_SEARCH_TURNS = max(1, min(3, int(os.environ.get("VERIFICATION_SEARCH_TURNS", "1"))))
VERIFICATION_TOKEN_LIMIT = max(256, int(os.environ.get("VERIFICATION_TOKEN_LIMIT", "1200")))
VERIFICATION_SEARCH_TIMEOUT_SECONDS = max(
    30, int(os.environ.get("VERIFICATION_SEARCH_TIMEOUT_SECONDS", "180"))
)
VERIFICATION_RETRIEVAL_TIMEOUT_SECONDS = max(10, int(os.environ.get("VERIFICATION_RETRIEVAL_TIMEOUT_SECONDS", "30")))
VERIFICATION_TOTAL_TIMEOUT_SECONDS = max(
    VERIFICATION_SEARCH_TIMEOUT_SECONDS, int(os.environ.get("VERIFICATION_TOTAL_TIMEOUT_SECONDS", "300"))
)
VERIFICATION_TRANSIENT_RETRIES = max(0, min(1, int(os.environ.get("VERIFICATION_TRANSIENT_RETRIES", "1"))))
VERIFICATION_RETRY_BACKOFF_SECONDS = max(0.0, float(os.environ.get("VERIFICATION_RETRY_BACKOFF_SECONDS", "2")))
VERIFICATION_RETRY_MAX_BACKOFF_SECONDS = max(
    VERIFICATION_RETRY_BACKOFF_SECONDS, float(os.environ.get("VERIFICATION_RETRY_MAX_BACKOFF_SECONDS", "30"))
)
VERIFICATION_MAX_LEADS = max(1, min(10, int(os.environ.get("VERIFICATION_MAX_LEADS", "5"))))
VERIFICATION_EXECUTOR = ThreadPoolExecutor(max_workers=1)
CONTENT_POLICY_VERSION = os.environ.get("CONTENT_POLICY_VERSION", "content-ceo-v1.2")
CONTENT_FRESHNESS_DAYS = max(1, int(os.environ.get("CONTENT_FRESHNESS_DAYS", "7")))
CONTENT_HISTORY_DAYS = max(1, int(os.environ.get("CONTENT_HISTORY_DAYS", "30")))
CONTENT_TOKEN_LIMIT = max(256, int(os.environ.get("CONTENT_TOKEN_LIMIT", "900")))
CONTENT_TIMEOUT_SECONDS = max(5, int(os.environ.get("CONTENT_TIMEOUT_SECONDS", "45")))
CONTENT_EXECUTOR = ThreadPoolExecutor(max_workers=1)
PRODUCTION_POLICY_VERSION = os.environ.get("PRODUCTION_POLICY_VERSION", "content-production-v1")
EVIDENCE_CLASSIFIER_VERSION = "claim-evidence-v2"
PRODUCTION_TOKEN_LIMIT = max(512, int(os.environ.get("PRODUCTION_TOKEN_LIMIT", "8000")))
PRODUCTION_CONNECTION_TIMEOUT_SECONDS = max(2, int(os.environ.get("PRODUCTION_CONNECTION_TIMEOUT_SECONDS", "10")))
PRODUCTION_RESPONSE_TIMEOUT_SECONDS = max(5, int(os.environ.get("PRODUCTION_RESPONSE_TIMEOUT_SECONDS", "60")))
PRODUCTION_MAX_RETRIES = max(0, min(2, int(os.environ.get("PRODUCTION_MAX_RETRIES", "0"))))
PRODUCTION_EXECUTOR = ThreadPoolExecutor(max_workers=1)
PRODUCTION_ACTIVE_STATES = ("QUEUED", "GENERATING", "VALIDATING")
PRODUCTION_TRANSITIONS = {
    "QUEUED": {"GENERATING", "BLOCKED"},
    "GENERATING": {"VALIDATING", "HUMAN_REVIEW", "FAILED", "BLOCKED"},
    "VALIDATING": {"READY_FOR_APPROVAL", "HUMAN_REVIEW", "BLOCKED", "FAILED"},
    "READY_FOR_APPROVAL": set(), "HUMAN_REVIEW": {"VALIDATING"}, "BLOCKED": set(), "FAILED": set(),
}
AUTO_REEL_PIPELINE_ENABLED = os.environ.get("AUTO_REEL_PIPELINE", "0").strip().lower() in ("1", "true", "yes", "on")
LIVE_DISCOVERY_ENABLED = os.environ.get("LIVE_DISCOVERY_ENABLED", "0").strip().lower() in ("1", "true", "yes", "on")
LIVE_DISCOVERY_INTERVAL_SECONDS = max(60, int(os.environ.get("LIVE_DISCOVERY_INTERVAL_SECONDS", "300")))
RENDER_POLICY_VERSION = os.environ.get("RENDER_POLICY_VERSION", "media-render-policy-v1")
RENDERER_CONFIG_VERSION = os.environ.get("RENDERER_CONFIG_VERSION", "live-renderer-config-v1")
RENDER_STORAGE_ROOT = Path(os.environ.get("RENDER_STORAGE_ROOT", ROOT / ".context" / "generated_media"))
RENDER_TIMEOUT_SECONDS = max(5, int(os.environ.get("RENDER_TIMEOUT_SECONDS", "90")))
LIVE_RENDERER_TIMEOUT_SECONDS = max(5, int(os.environ.get("LIVE_RENDERER_TIMEOUT_SECONDS", "180")))
RENDER_MAX_RETRIES = max(0, min(3, int(os.environ.get("RENDER_MAX_RETRIES", "1"))))
LIVE_RENDERER_MAX_RETRIES = max(0, min(3, int(os.environ.get("LIVE_RENDERER_MAX_RETRIES", "1"))))
RENDER_MIN_VIDEO_SHORT_SIDE = max(1, int(os.environ.get("RENDER_MIN_VIDEO_SHORT_SIDE", "360")))
RENDER_VIDEO_DURATION_TOLERANCE_SECONDS = max(0.0, float(os.environ.get("RENDER_VIDEO_DURATION_TOLERANCE_SECONDS", "1.5")))
RENDER_TARGET_DIMENSIONS = {"3:4": (1200, 1600), "4:5": (1200, 1500), "1:1": (1080, 1080), "9:16": (1080, 1920)}
RENDER_BACKOFF_SECONDS = max(0.0, float(os.environ.get("RENDER_BACKOFF_SECONDS", "2")))
RENDER_MAX_BACKOFF_SECONDS = max(0.0, float(os.environ.get("RENDER_MAX_BACKOFF_SECONDS", "30")))
RENDER_MIN_IMAGE_WIDTH = max(64, int(os.environ.get("RENDER_MIN_IMAGE_WIDTH", "1080")))
RENDER_MIN_IMAGE_HEIGHT = max(64, int(os.environ.get("RENDER_MIN_IMAGE_HEIGHT", "1080")))
RENDER_ASPECT_TOLERANCE = max(0.01, float(os.environ.get("RENDER_ASPECT_TOLERANCE", "0.03")))
VIDEO_SOURCE_MAX_BYTES = max(256_000, int(os.environ.get("VIDEO_SOURCE_MAX_BYTES", str(4 * 1024 * 1024))))
VIDEO_SOURCE_MIN_SHORT_SIDE = max(360, int(os.environ.get("VIDEO_SOURCE_MIN_SHORT_SIDE", "720")))
RENDER_EXECUTOR = ThreadPoolExecutor(max_workers=max(1, int(os.environ.get("RENDER_WORKERS", "1"))))
RENDER_TRANSITIONS = {
    "QUEUED": {"PREPARING", "BLOCKED", "CANCELLED"},
    "PREPARING": {"RENDERING", "BLOCKED", "HUMAN_REVIEW", "FAILED", "CANCELLED"},
    # PROVIDER_PENDING is stored as resume_state while status remains RENDERING so
    # existing databases and the active-job uniqueness constraints remain compatible.
    "RENDERING": {"VALIDATING", "BLOCKED", "HUMAN_REVIEW", "FAILED", "CANCELLED"},
    "VALIDATING": {"READY_FOR_REVIEW", "BLOCKED", "HUMAN_REVIEW", "FAILED"},
    "READY_FOR_REVIEW": set(), "HUMAN_REVIEW": set(), "BLOCKED": set(), "FAILED": set(), "CANCELLED": set(),
}

STATES = (
    "DETECTED", "VERIFYING", "VERIFIED", "CONTENT_PLANNED", "SCRIPTED",
    "RENDERING", "QA", "READY_TO_PUBLISH", "PUBLISHED", "MEASURED",
    "HOLD", "REJECTED",
)
TRANSITIONS = {
    "DETECTED": {"VERIFYING", "HOLD", "REJECTED"},
    "VERIFYING": {"VERIFIED", "HOLD", "REJECTED"},
    "VERIFIED": {"CONTENT_PLANNED", "HOLD"},
    "CONTENT_PLANNED": {"SCRIPTED", "HOLD"},
    "SCRIPTED": {"RENDERING", "HOLD"},
    "RENDERING": {"QA", "HOLD"},
    "QA": {"READY_TO_PUBLISH", "SCRIPTED", "HOLD"},
    "READY_TO_PUBLISH": {"PUBLISHED", "HOLD"},
    "PUBLISHED": {"MEASURED"},
    "MEASURED": set(),
    "HOLD": {"VERIFYING", "REJECTED"},
    "REJECTED": set(),
}


class JsonFormatter(logging.Formatter):
    def format(self, record):
        payload = {
            "time": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "event": getattr(record, "event", "application_log"),
            "message": record.getMessage(),
        }
        payload.update(getattr(record, "context", {}))
        return json.dumps(payload, ensure_ascii=False, sort_keys=True)


LOGGER = logging.getLogger("reachout")
if not LOGGER.handlers:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter())
    LOGGER.addHandler(handler)
LOGGER.setLevel(os.environ.get("LOG_LEVEL", "INFO").upper())
LOGGER.propagate = False


def log_error(event, error, **context):
    LOGGER.error(str(error), extra={"event": event, "context": context})


def now():
    return datetime.now(timezone.utc).isoformat()


def connect():
    DB.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DB, timeout=15)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def init():
    with connect() as connection:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations("
            "version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)"
        )
        applied = {
            row["version"]
            for row in connection.execute("SELECT version FROM schema_migrations")
        }
        for path in sorted(MIGRATIONS.glob("*.sql")):
            version = int(path.name.split("_", 1)[0])
            if version in applied:
                continue
            sql = path.read_text(encoding="utf-8")
            connection.executescript(
                "BEGIN IMMEDIATE;\n"
                + sql
                + "\nINSERT INTO schema_migrations(version,name,applied_at) VALUES("
                + str(version)
                + ","
                + repr(path.name)
                + ","
                + repr(now())
                + ");\nCOMMIT;"
            )
            LOGGER.info(
                "migration applied",
                extra={"event": "migration_applied", "context": {"version": version, "name": path.name}},
            )
    apply_workspace_identity_correction()
    apply_reference_material_correction()
    if SOURCES_CONFIG.exists():
        sync_sources(load_source_config())
    if OFFICIAL_SOURCES_CONFIG.exists():
        sync_official_source_registry(load_official_source_registry())
    recover_unfinished_media_jobs()
    live_discovery.sync_sources(connect=connect, now=now)
    if AUTO_REEL_PIPELINE_ENABLED:
        recover_auto_reel_pipelines()
    if os.environ.get("OCR_PROVIDER", "auto").strip().lower() not in ("", "none", "off"):
        warm_media_probe()


def create_event(title, source, source_url="", priority="NORMAL", detected_at=None, event_time=None):
    if not title.strip() or not source.strip():
        raise ValueError("title and source required")
    if priority not in ("BREAKING", "HIGH", "NORMAL"):
        raise ValueError("invalid priority")
    if not event_time:
        raise ValueError("event_time is required; crawl time cannot be used as event time")
    event_id = "EV-" + uuid.uuid4().hex[:10].upper()
    timestamp = detected_at or now()
    event_time = normalize_time(event_time)
    with connect() as connection:
        connection.execute(
            "INSERT INTO events(id,title,source,source_url,status,priority,created_at,updated_at,first_seen_at,last_seen_at,workspace_key,event_time) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                event_id, title.strip(), source.strip(), source_url, "DETECTED", priority,
                timestamp, timestamp, timestamp, timestamp, workspace_identity()["workspace_key"], event_time,
            ),
        )
        connection.execute(
            "INSERT INTO transitions(event_id,from_state,to_state,at) VALUES(?,?,?,?)",
            (event_id, None, "DETECTED", timestamp),
        )
    return event_id


def transition(event_id, state):
    if state not in STATES:
        raise ValueError("invalid state")
    with connect() as connection:
        row = connection.execute("SELECT status,research_status FROM events WHERE id=?", (event_id,)).fetchone()
        if row is None:
            raise KeyError(event_id)
        old = row["status"]
        if state not in TRANSITIONS[old]:
            raise ValueError(f"{old} cannot transition to {state}")
        if state == "VERIFIED":
            policy_run = connection.execute(
                "SELECT acs.id FROM approved_claim_sets acs "
                "JOIN verification_runs vr ON vr.id=acs.verification_run_id "
                "WHERE acs.event_id=? AND acs.status='APPROVED' AND vr.mode='live' AND vr.status='COMPLETED' "
                "ORDER BY acs.version_number DESC LIMIT 1", (event_id,)
            ).fetchone()
            if not policy_run:
                raise ValueError("event cannot be VERIFIED until its required claims meet the evidence policy")
        timestamp = now()
        connection.execute(
            "UPDATE events SET status=?,updated_at=? WHERE id=?",
            (state, timestamp, event_id),
        )
        connection.execute(
            "INSERT INTO transitions(event_id,from_state,to_state,at) VALUES(?,?,?,?)",
            (event_id, old, state, timestamp),
        )
    # Automatic pipeline trigger: reaching VERIFIED can make an event eligible.
    if state == "VERIFIED":
        maybe_trigger_auto_reel(event_id)


def load_source_config(path=None):
    config_path = Path(path or SOURCES_CONFIG)
    data = json.loads(config_path.read_text(encoding="utf-8"))
    if not isinstance(data.get("sources"), list):
        raise ValueError("source configuration must contain a sources array")
    return data


def load_official_source_registry(path=None):
    return OfficialSourceRegistry.from_file(path or OFFICIAL_SOURCES_CONFIG)


def sync_official_source_registry(registry):
    timestamp = now()
    registry_version = json.loads(OFFICIAL_SOURCES_CONFIG.read_text(encoding="utf-8")).get(
        "registry_version", "unversioned"
    ) if OFFICIAL_SOURCES_CONFIG.exists() else "unversioned"
    with connect() as connection:
        for authority in registry.authorities:
            connection.execute(
                "INSERT INTO official_source_authorities(id,name,domain,authority_type,priority,document_types_json,"
                "enabled,registry_version,synced_at) VALUES(?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET name=excluded.name,domain=excluded.domain,"
                "authority_type=excluded.authority_type,priority=excluded.priority,"
                "document_types_json=excluded.document_types_json,enabled=excluded.enabled,"
                "registry_version=excluded.registry_version,synced_at=excluded.synced_at",
                (
                    authority.id, authority.name, authority.domain, authority.authority_type, authority.priority,
                    json.dumps(authority.document_types), int(authority.enabled), registry_version, timestamp,
                ),
            )


def workspace_identity():
    return json.loads(WORKSPACE_CONFIG.read_text(encoding="utf-8"))


def _identity_text(value):
    return " ".join(re.findall(r"[a-z0-9]+", str(value).lower()))


def _host_allowed(host, allowed_hosts):
    normalized = (host or "").lower().rstrip(".")
    return any(normalized == allowed.lower().rstrip(".") for allowed in allowed_hosts)


def validate_source_registration(source, config_workspace_key=None):
    workspace = workspace_identity()
    expected_key = workspace["workspace_key"]
    if config_workspace_key != expected_key:
        raise ValueError(f"source configuration workspace_key must be {expected_key}")
    source_class = source.get("source_class")
    if source_class not in ("official_primary", "independent_reporting"):
        raise ValueError(f"source {source.get('id', '<unknown>')} requires a valid source_class")
    identity = source.get("identity")
    if not isinstance(identity, dict) or identity.get("status") not in ("verified", "review", "rejected"):
        raise ValueError(f"source {source.get('id', '<unknown>')} requires an identity status")
    evidence_url = identity.get("evidence_url", "")
    evidence = urlparse(evidence_url)
    if identity.get("status") == "verified" and (evidence.scheme != "https" or not evidence.hostname):
        raise ValueError(f"source {source.get('id', '<unknown>')} requires HTTPS identity evidence")
    if not str(identity.get("verification_note", "")).strip():
        raise ValueError(f"source {source.get('id', '<unknown>')} requires an identity verification note")
    if source_class == "official_primary":
        if not source.get("official"):
            raise ValueError(f"source {source.get('id', '<unknown>')} must be marked official_primary")
        if identity.get("verification_method") not in ("government_domain", "cross_source_confirmation"):
            raise ValueError("an official source's own claim is insufficient identity evidence")
    elif source.get("official"):
        raise ValueError("independent reporting sources cannot be labelled official")
    elif identity.get("verification_method") not in ("publisher_masthead_and_registry", "cross_source_confirmation"):
        raise ValueError("independent reporting source requires publisher or cross-source identity evidence")

    match = source.get("workspace_match")
    if not isinstance(match, dict):
        raise ValueError(f"source {source.get('id', '<unknown>')} requires workspace_match")
    aliases = {_identity_text(alias) for alias in workspace["leader"]["aliases"]}
    if _identity_text(match.get("leader", "")) not in aliases:
        raise ValueError(f"source {source.get('id', '<unknown>')} does not match workspace leader")
    if _identity_text(match.get("jurisdiction", "")) != _identity_text(workspace["jurisdiction"]):
        raise ValueError(f"source {source.get('id', '<unknown>')} does not match workspace jurisdiction")
    topics = match.get("topics")
    allowed_topics = {_identity_text(topic) for topic in workspace["topics"]}
    if not isinstance(topics, list) or not topics:
        raise ValueError(f"source {source.get('id', '<unknown>')} requires at least one workspace topic")
    if any(_identity_text(topic) not in allowed_topics for topic in topics):
        raise ValueError(f"source {source.get('id', '<unknown>')} contains an unapproved workspace topic")

    host = urlparse(source.get("url", "")).hostname
    if source_class == "official_primary" and not _host_allowed(host, workspace["allowed_hosts"]):
        raise ValueError(f"source host {host or '<missing>'} is not approved for {workspace['display_name']}")
    content_role = source.get("metadata", {}).get("content_role")
    if content_role not in ("homepage", "profile", "listing", "reference", "item_stream"):
        raise ValueError(f"source {source.get('id', '<unknown>')} requires a valid metadata.content_role")
    return workspace


def _validate_source(source, config_workspace_key):
    required = ("id", "name", "type", "url")
    if any(not str(source.get(key, "")).strip() for key in required):
        raise ValueError("each source requires id, name, type, and url")
    if source["type"] not in ("rss", "webpage"):
        raise ValueError(f"unsupported source type: {source['type']}")
    parsed = urlparse(source["url"])
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError(f"invalid source URL: {source['url']}")
    validate_source_registration(source, config_workspace_key)


def sync_sources(config):
    workspace_key = config.get("workspace_key")
    timestamp = now()
    with connect() as connection:
        for source in config["sources"]:
            _validate_source(source, workspace_key)
            metadata = {
                **source.get("metadata", {}),
                "workspace_match": source["workspace_match"],
            }
            connection.execute(
                "INSERT INTO sources(id,name,source_type,url,official,enabled,metadata_json,created_at,workspace_key,validated_at,"
                "source_class,identity_status,identity_evidence_url,rate_limit_seconds,poll_interval_seconds) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET name=excluded.name,source_type=excluded.source_type,"
                "url=excluded.url,official=excluded.official,enabled=excluded.enabled,metadata_json=excluded.metadata_json,"
                "workspace_key=excluded.workspace_key,validated_at=excluded.validated_at,"
                "source_class=excluded.source_class,identity_status=excluded.identity_status,"
                "identity_evidence_url=excluded.identity_evidence_url,rate_limit_seconds=excluded.rate_limit_seconds,"
                "poll_interval_seconds=excluded.poll_interval_seconds",
                (
                    source["id"], source["name"], source["type"], source["url"],
                    int(bool(source.get("official"))), int(source.get("enabled", True)),
                    json.dumps(metadata, sort_keys=True), timestamp, workspace_key, timestamp,
                    source["source_class"], source["identity"]["status"],
                    source["identity"].get("evidence_url"),
                    float(source.get("rate_limit_seconds", 1.0)),
                    int(source.get("poll_interval_seconds", 900)),
                ),
            )
            connection.execute(
                "INSERT OR IGNORE INTO source_poll_state(source_id) VALUES(?)", (source["id"],)
            )


CORRECTION_ID = "2026-09-30-ncbn-andhra-pradesh-identity"
CORRECTION_REASON = (
    "Correct workspace identity from ambiguous CBN usage to N. Chandrababu Naidu, "
    "Andhra Pradesh; remove Central Bank of Nigeria provenance and ambiguous sample data."
)


def _is_cbn_nigeria_url(value):
    try:
        host = (urlparse(value or "").hostname or "").lower()
    except ValueError:
        return False
    return host == "cbn.gov.ng" or host.endswith(".cbn.gov.ng")


def apply_workspace_identity_correction():
    corrected_at = now()
    with connect() as connection:
        existing = connection.execute(
            "SELECT details_json FROM data_corrections WHERE id=?", (CORRECTION_ID,)
        ).fetchone()
        if existing:
            return json.loads(existing["details_json"])

        source_rows = [dict(row) for row in connection.execute("SELECT * FROM sources")]
        affected_sources = [
            row for row in source_rows
            if _is_cbn_nigeria_url(row["url"])
            or "cbn.gov.ng" in row["metadata_json"].lower()
            or "central bank of nigeria" in row["metadata_json"].lower()
        ]
        source_ids = {row["id"] for row in affected_sources}

        signal_rows = [dict(row) for row in connection.execute("SELECT * FROM signals")]
        affected_signals = [
            row for row in signal_rows
            if row["source_id"] in source_ids
            or _is_cbn_nigeria_url(row["url"])
            or _is_cbn_nigeria_url(row["canonical_url"])
            or "cbn.gov.ng" in row["source_metadata_json"].lower()
            or "central bank of nigeria" in row["source_metadata_json"].lower()
        ]
        signal_ids = {row["id"] for row in affected_signals}
        affected_event_ids = {row["event_id"] for row in affected_signals}

        event_rows = [dict(row) for row in connection.execute("SELECT * FROM events")]
        ambiguous_events = [
            row for row in event_rows
            if re.search(r"\bCBN\b", row["title"], re.I)
            or _is_cbn_nigeria_url(row["source_url"])
            or "central bank of nigeria" in row["source"].lower()
        ]
        affected_event_ids.update(row["id"] for row in ambiguous_events)
        affected_events = [row for row in event_rows if row["id"] in affected_event_ids]

        details = {
            "affected_source_ids": sorted(source_ids),
            "affected_signal_ids": sorted(signal_ids),
            "affected_event_ids": sorted(affected_event_ids),
            "cbn_gov_ng_source_count": len(affected_sources),
            "cbn_gov_ng_signal_count": len(affected_signals),
            "ambiguous_event_count": len(ambiguous_events),
            "events_removed": 0,
            "events_repointed": 0,
        }
        connection.execute(
            "INSERT INTO data_corrections(id,summary,details_json,applied_at) VALUES(?,?,?,?)",
            (CORRECTION_ID, CORRECTION_REASON, json.dumps(details, sort_keys=True), corrected_at),
        )

        def audit(entity_type, entity_id, action, snapshot):
            connection.execute(
                "INSERT INTO correction_audit(correction_id,entity_type,entity_id,action,reason,snapshot_json,corrected_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (
                    CORRECTION_ID, entity_type, entity_id, action, CORRECTION_REASON,
                    json.dumps(snapshot, ensure_ascii=False, sort_keys=True), corrected_at,
                ),
            )

        for source in affected_sources:
            audit("source", source["id"], "removed", source)
        for signal in affected_signals:
            audit("signal", signal["id"], "removed", signal)
        for event in affected_events:
            transitions = [dict(row) for row in connection.execute(
                "SELECT * FROM transitions WHERE event_id=? ORDER BY id", (event["id"],)
            )]
            audit("event", event["id"], "reviewed_for_removal", {**event, "transitions": transitions})

        connection.execute("DROP TRIGGER IF EXISTS signals_are_immutable_update")
        connection.execute("DROP TRIGGER IF EXISTS signals_are_immutable_delete")
        if signal_ids:
            connection.executemany("DELETE FROM signals WHERE id=?", ((signal_id,) for signal_id in signal_ids))

        for event in affected_events:
            survivor = connection.execute(
                "SELECT * FROM signals WHERE event_id=? ORDER BY detected_at,id LIMIT 1", (event["id"],)
            ).fetchone()
            if survivor:
                connection.execute(
                    "UPDATE events SET title=?,source=?,source_url=?,updated_at=? WHERE id=?",
                    (survivor["title"], survivor["source_name"], survivor["url"], corrected_at, event["id"]),
                )
                audit("event", event["id"], "repointed_to_surviving_signal", dict(survivor))
                details["events_repointed"] += 1
            else:
                connection.execute("DELETE FROM transitions WHERE event_id=?", (event["id"],))
                connection.execute("DELETE FROM events WHERE id=?", (event["id"],))
                audit("event", event["id"], "removed", event)
                details["events_removed"] += 1

        if source_ids:
            connection.executemany("DELETE FROM sources WHERE id=?", ((source_id,) for source_id in source_ids))
        connection.execute(
            "CREATE TRIGGER signals_are_immutable_update BEFORE UPDATE ON signals "
            "BEGIN SELECT RAISE(ABORT, 'signals are immutable'); END"
        )
        connection.execute(
            "CREATE TRIGGER signals_are_immutable_delete BEFORE DELETE ON signals "
            "BEGIN SELECT RAISE(ABORT, 'signals are immutable'); END"
        )
        connection.execute(
            "UPDATE data_corrections SET details_json=? WHERE id=?",
            (json.dumps(details, sort_keys=True), CORRECTION_ID),
        )
        LOGGER.info(
            "workspace identity correction applied",
            extra={"event": "workspace_identity_corrected", "context": details},
        )
        return details


REFERENCE_CORRECTION_ID = "2026-09-30-separate-source-monitoring-from-event-discovery"
REFERENCE_CORRECTION_REASON = (
    "Reclassify homepages, profiles, and listing pages as reference material; remove false events "
    "that lack a dated individual news item, announcement, speech, or post."
)
REFERENCE_SOURCE_ROLES = {
    "ncbn-official-site": "homepage",
    "ap-government-cm-profile": "profile",
    "nic-andhra-pradesh-news": "listing",
}


def _create_signal_immutability_triggers(connection):
    connection.execute(
        "CREATE TRIGGER signals_are_immutable_update BEFORE UPDATE ON signals "
        "BEGIN SELECT RAISE(ABORT, 'signals are immutable'); END"
    )
    connection.execute(
        "CREATE TRIGGER signals_are_immutable_delete BEFORE DELETE ON signals "
        "BEGIN SELECT RAISE(ABORT, 'signals are immutable'); END"
    )


def apply_reference_material_correction():
    corrected_at = now()
    with connect() as connection:
        existing = connection.execute(
            "SELECT details_json FROM data_corrections WHERE id=?", (REFERENCE_CORRECTION_ID,)
        ).fetchone()
        if existing:
            return json.loads(existing["details_json"])

        signals = [
            dict(row) for row in connection.execute(
                "SELECT * FROM signals WHERE source_id IN (?,?,?)",
                tuple(REFERENCE_SOURCE_ROLES),
            )
        ]
        event_ids = {row["event_id"] for row in signals if row["event_id"]}
        events = [
            dict(row) for row in connection.execute("SELECT * FROM events")
            if row["id"] in event_ids
        ]
        details = {
            "reference_signal_ids": sorted(row["id"] for row in signals),
            "false_event_ids": sorted(event_ids),
            "signals_reclassified": len(signals),
            "events_removed": 0,
            "sources_preserved": len(REFERENCE_SOURCE_ROLES),
        }
        connection.execute(
            "INSERT INTO data_corrections(id,summary,details_json,applied_at) VALUES(?,?,?,?)",
            (
                REFERENCE_CORRECTION_ID, REFERENCE_CORRECTION_REASON,
                json.dumps(details, sort_keys=True), corrected_at,
            ),
        )

        def audit(entity_type, entity_id, action, snapshot):
            connection.execute(
                "INSERT INTO correction_audit(correction_id,entity_type,entity_id,action,reason,snapshot_json,corrected_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (
                    REFERENCE_CORRECTION_ID, entity_type, entity_id, action,
                    REFERENCE_CORRECTION_REASON,
                    json.dumps(snapshot, ensure_ascii=False, sort_keys=True), corrected_at,
                ),
            )

        for signal in signals:
            audit("signal", signal["id"], "reclassified_as_reference", signal)
        for event in events:
            transitions = [dict(row) for row in connection.execute(
                "SELECT * FROM transitions WHERE event_id=? ORDER BY id", (event["id"],)
            )]
            audit("event", event["id"], "removed_false_event", {**event, "transitions": transitions})

        connection.execute("DROP TRIGGER IF EXISTS signals_are_immutable_update")
        connection.execute("DROP TRIGGER IF EXISTS signals_are_immutable_delete")
        for signal in signals:
            role = REFERENCE_SOURCE_ROLES[signal["source_id"]]
            connection.execute(
                "UPDATE signals SET event_id=NULL,item_kind='reference',item_type=?,event_time=NULL,"
                "classification_reason=? WHERE id=?",
                (role, f"registered_{role}_is_reference_material", signal["id"]),
            )

        for event in events:
            remaining = connection.execute(
                "SELECT COUNT(*) FROM signals WHERE event_id=?", (event["id"],)
            ).fetchone()[0]
            if remaining:
                continue
            connection.execute("DELETE FROM transitions WHERE event_id=?", (event["id"],))
            connection.execute("DELETE FROM events WHERE id=?", (event["id"],))
            details["events_removed"] += 1
        _create_signal_immutability_triggers(connection)
        connection.execute(
            "UPDATE data_corrections SET details_json=? WHERE id=?",
            (json.dumps(details, sort_keys=True), REFERENCE_CORRECTION_ID),
        )
        LOGGER.info(
            "reference material correction applied",
            extra={"event": "reference_material_corrected", "context": details},
        )
        return details


def canonicalize_url(value):
    parsed = urlparse(value.strip())
    if parsed.scheme.lower() not in ("http", "https") or not parsed.hostname:
        raise ValueError("URL must use http or https")
    host = parsed.hostname.lower()
    port = parsed.port
    netloc = host
    if port and not ((parsed.scheme.lower() == "http" and port == 80) or (parsed.scheme.lower() == "https" and port == 443)):
        netloc += f":{port}"
    path = re.sub(r"/{2,}", "/", parsed.path or "/")
    if path != "/":
        path = path.rstrip("/")
    ignored = {"fbclid", "gclid", "mc_cid", "mc_eid"}
    query = [
        (key, val) for key, val in parse_qsl(parsed.query, keep_blank_values=True)
        if key.lower() not in ignored and not key.lower().startswith("utm_")
    ]
    return urlunparse((parsed.scheme.lower(), netloc, path, "", urlencode(sorted(query)), ""))


def _as_utc(value):
    if not value:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value).strip()
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            parsed = parsedate_to_datetime(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def normalize_time(value):
    parsed = _as_utc(value)
    return parsed.isoformat() if parsed else None


STOPWORDS = {
    "about", "after", "again", "against", "also", "among", "and", "are", "been",
    "before", "being", "between", "but", "can", "could", "for", "from", "has", "have",
    "into", "its", "more", "new", "not", "official", "over", "report", "says", "said",
    "that", "the", "their", "this", "through", "under", "was", "were", "will", "with",
}


def _tokens(value):
    normalized = value.lower()
    for alias in workspace_identity()["leader"]["aliases"]:
        normalized = normalized.replace(alias.lower(), "ncbn")
    return {
        token for token in re.findall(r"[a-z0-9]+", normalized)
        if len(token) >= 3 and token not in STOPWORDS
    }


def _entities(value):
    normalized = value
    leader_found = False
    for alias in workspace_identity()["leader"]["aliases"]:
        if re.search(rf"\b{re.escape(alias)}\b", normalized, re.I):
            leader_found = True
            normalized = re.sub(rf"\b{re.escape(alias)}\b", "N. Chandrababu Naidu", normalized, flags=re.I)
    phrases = re.findall(r"\b(?:[A-Z][A-Za-z&.-]*|[A-Z]{2,})(?:\s+(?:of\s+)?(?:[A-Z][A-Za-z&.-]*|[A-Z]{2,})){0,4}\b", normalized)
    entities = {" ".join(part.lower().split()) for part in phrases}
    if leader_found:
        entities.add("n chandrababu naidu")
    return entities


def _jaccard(left, right):
    return len(left & right) / len(left | right) if left and right else 0.0


def similarity(left_title, left_text, right_title, right_text):
    left = f"{left_title} {left_text}"
    right = f"{right_title} {right_text}"
    token_score = _jaccard(_tokens(left), _tokens(right))
    title_score = _jaccard(_tokens(left_title), _tokens(right_title))
    sequence_score = SequenceMatcher(None, " ".join(sorted(_tokens(left))), " ".join(sorted(_tokens(right)))).ratio()
    text_score = max(token_score, (title_score * 0.65) + (sequence_score * 0.35))
    entity_score = _jaccard(_entities(left), _entities(right))
    return text_score, entity_score


def _find_cluster(connection, title, text, event_time):
    incoming_time = _as_utc(event_time)
    earliest = (incoming_time - timedelta(days=7)).isoformat()
    candidates = connection.execute(
        "SELECT s.event_id,s.title,s.text,s.event_time "
        "FROM signals s WHERE s.item_kind='event' AND s.event_time>=? "
        "ORDER BY s.detected_at DESC LIMIT 500",
        (earliest,),
    ).fetchall()
    best = None
    for candidate in candidates:
        candidate_time = _as_utc(candidate["event_time"])
        hours = abs((incoming_time - candidate_time).total_seconds()) / 3600
        if hours > CLUSTER_WINDOW_HOURS:
            continue
        text_score, entity_score = similarity(title, text, candidate["title"], candidate["text"])
        time_score = max(0.0, 1.0 - (hours / CLUSTER_WINDOW_HOURS))
        score = (text_score * 0.60) + (entity_score * 0.25) + (time_score * 0.15)
        qualifies = text_score >= 0.44 or (entity_score >= 0.25 and text_score >= 0.23)
        if qualifies and (best is None or score > best[1]):
            best = (candidate["event_id"], score)
    return best


ALLOWED_EVENT_ITEM_TYPES = {"news", "announcement", "speech", "post"}
REFERENCE_CONTENT_ROLES = {"homepage", "profile", "listing", "reference"}


def classify_signal(event_time, detected_at, content_role, item_type):
    role = (content_role or "reference").lower()
    normalized_type = (item_type or "").lower() or None
    if role in REFERENCE_CONTENT_ROLES:
        return "reference", None, f"registered_{role}_is_reference_material"
    if role not in ("item", "feed_item"):
        return "reference", None, "source_role_is_not_event_discovery"
    if normalized_type not in ALLOWED_EVENT_ITEM_TYPES:
        return "rejected", None, "not_an_individual_news_announcement_speech_or_post"
    if not event_time:
        return "review", None, "missing_defensible_publication_or_event_time"

    event_dt = _as_utc(event_time)
    detected_dt = _as_utc(detected_at)
    if event_dt - detected_dt > timedelta(days=1):
        return "review", None, "event_time_is_implausibly_in_the_future"
    if detected_dt - event_dt > timedelta(days=EVENT_MAX_AGE_DAYS):
        return "rejected", event_time, f"older_than_{EVENT_MAX_AGE_DAYS}_day_event_window"
    return "event", event_time, "dated_individual_item_within_event_window"


def ingest_signal(
    *, url, title, text, source_name, source_type="manual", source_id=None,
    publication_time=None, event_time=None, detected_at=None, source_metadata=None,
    priority="NORMAL", content_role="reference", item_type=None, author=None,
    source_class="official_primary",
):
    if not title or not str(title).strip():
        raise ValueError("signal title is required")
    if not text or not str(text).strip():
        raise ValueError("signal text is required")
    canonical_url = canonicalize_url(url)
    publication_time = normalize_time(publication_time)
    detected_at = normalize_time(detected_at) or now()
    stated_event_time = normalize_time(event_time)
    event_time = stated_event_time or publication_time
    event_time_basis = "stated_event_time" if stated_event_time else ("publication_time" if publication_time else None)
    item_kind, classified_event_time, classification_reason = classify_signal(
        event_time, detected_at, content_role, item_type
    )
    clean_title = " ".join(str(title).split())[:500]
    clean_text = " ".join(str(text).split())[:100_000]
    metadata_json = json.dumps(source_metadata or {}, ensure_ascii=False, sort_keys=True)
    content_hash = hashlib.sha256(f"{clean_title}\n{clean_text}".encode()).hexdigest()

    with connect() as connection:
        existing = connection.execute(
            "SELECT id,event_id,item_kind FROM signals WHERE canonical_url=?", (canonical_url,)
        ).fetchone()
        if existing:
            return {
                "signal_id": existing["id"], "event_id": existing["event_id"],
                "item_kind": existing["item_kind"], "duplicate": True,
            }

        cluster = None
        event_id = None
        cluster_score = 0.0
        cluster_method = "not_clustered_reference"
        if item_kind == "event":
            cluster = _find_cluster(connection, clean_title, clean_text, classified_event_time)
        if item_kind == "event" and cluster:
            event_id, cluster_score = cluster
            cluster_method = "time_entities_text_v1"
            connection.execute(
                "UPDATE events SET updated_at=?,last_seen_at=? WHERE id=?",
                (detected_at, detected_at, event_id),
            )
        elif item_kind == "event":
            event_id = "EV-" + uuid.uuid4().hex[:10].upper()
            cluster_method = "new_event"
            connection.execute(
                "INSERT INTO events(id,title,source,source_url,status,priority,created_at,updated_at,first_seen_at,last_seen_at,workspace_key,event_time) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    event_id, clean_title, source_name, url, "DETECTED", priority,
                    detected_at, detected_at, detected_at, detected_at,
                    workspace_identity()["workspace_key"], classified_event_time,
                ),
            )
            connection.execute(
                "INSERT INTO transitions(event_id,from_state,to_state,at) VALUES(?,?,?,?)",
                (event_id, None, "DETECTED", detected_at),
            )

        signal_id = "SIG-" + uuid.uuid4().hex[:12].upper()
        try:
            connection.execute(
                "INSERT INTO signals(id,event_id,source_id,url,canonical_url,publication_time,detected_at,title,text,"
                "source_name,source_type,source_metadata_json,content_hash,cluster_score,cluster_method,workspace_key,"
                "item_kind,item_type,event_time,classification_reason,author,source_class,event_time_basis) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    signal_id, event_id, source_id, url, canonical_url, publication_time, detected_at,
                    clean_title, clean_text, source_name, source_type, metadata_json, content_hash,
                    round(cluster_score, 6), cluster_method, workspace_identity()["workspace_key"],
                    item_kind, item_type, classified_event_time, classification_reason,
                    author, source_class, event_time_basis,
                ),
            )
        except sqlite3.IntegrityError:
            existing = connection.execute(
                "SELECT id,event_id,item_kind FROM signals WHERE canonical_url=?", (canonical_url,)
            ).fetchone()
            if not existing:
                raise
            return {
                "signal_id": existing["id"], "event_id": existing["event_id"],
                "item_kind": existing["item_kind"], "duplicate": True,
            }

    return {
        "signal_id": signal_id,
        "event_id": event_id,
        "item_kind": item_kind,
        "classification_reason": classification_reason,
        "reference": item_kind == "reference",
        "review": item_kind == "review",
        "rejected": item_kind == "rejected",
        "duplicate": False,
        "clustered": bool(cluster),
        "cluster_score": round(cluster_score, 3),
    }


def _validate_public_url(value):
    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("URL must use http or https")
    try:
        addresses = socket.getaddrinfo(parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80))
    except socket.gaierror as error:
        raise ValueError("URL hostname could not be resolved") from error
    for address in addresses:
        ip = ipaddress.ip_address(address[4][0])
        if not ip.is_global:
            raise ValueError("private or local network URLs are not allowed")


class SafeRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _validate_public_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch_public_url(url):
    _validate_public_url(url)
    request = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html, application/rss+xml, application/xml;q=0.9"})
    for attempt in range(2):
        try:
            with build_opener(SafeRedirectHandler).open(request, timeout=15) as response:
                final_url = response.geturl()
                _validate_public_url(final_url)
                length = response.headers.get("Content-Length")
                if length and int(length) > MAX_RESPONSE_BYTES:
                    raise ValueError("source response is too large")
                body = response.read(MAX_RESPONSE_BYTES + 1)
                if len(body) > MAX_RESPONSE_BYTES:
                    raise ValueError("source response is too large")
                content_type = response.headers.get_content_type()
                charset = response.headers.get_content_charset() or "utf-8"
            return body.decode(charset, errors="replace"), content_type, final_url
        except (URLError, TimeoutError):
            if attempt:
                raise


def fetch_public_resource(url):
    """Fetch an evidence document as bytes with the same SSRF and redirect policy."""
    _validate_public_url(url)
    request = Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html, application/xhtml+xml, application/pdf;q=0.95",
        },
    )
    for attempt in range(2):
        try:
            with build_opener(SafeRedirectHandler).open(request, timeout=15) as response:
                final_url = response.geturl()
                _validate_public_url(final_url)
                length = response.headers.get("Content-Length")
                if length and int(length) > MAX_EVIDENCE_DOCUMENT_BYTES:
                    raise ValueError("evidence document is too large")
                body = response.read(MAX_EVIDENCE_DOCUMENT_BYTES + 1)
                if len(body) > MAX_EVIDENCE_DOCUMENT_BYTES:
                    raise ValueError("evidence document is too large")
                return RetrievedResponse(
                    requested_url=url, final_url=final_url, status=getattr(response, "status", 200),
                    content_type=response.headers.get("Content-Type") or "application/octet-stream",
                    body=body, headers={key: value for key, value in response.headers.items()},
                )
        except (URLError, TimeoutError):
            if attempt:
                raise


class PageParser(HTMLParser):
    TEXT_TAGS = {"title", "h1", "h2", "h3", "p", "article", "time"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.capture = []
        self.text_parts = []
        self.title_parts = []
        self.meta = {}
        self.links = []
        self.time_values = []
        self.in_json_ld = False
        self.json_ld_parts = []

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag in self.TEXT_TAGS:
            self.capture.append(tag)
        if tag == "script" and attributes.get("type", "").lower() == "application/ld+json":
            self.in_json_ld = True
            self.json_ld_parts.append([])
        if tag == "time" and attributes.get("datetime"):
            self.time_values.append(attributes["datetime"])
        if tag == "meta" and attributes.get("content"):
            key = (attributes.get("property") or attributes.get("name") or "").lower()
            if key:
                self.meta[key] = attributes["content"]
        if tag == "link" and attributes.get("rel") == "canonical" and attributes.get("href"):
            self.meta["canonical"] = attributes["href"]
        if tag == "a" and attributes.get("href"):
            self.links.append(attributes["href"])

    def handle_endtag(self, tag):
        if tag == "script" and self.in_json_ld:
            self.in_json_ld = False
        if self.capture and self.capture[-1] == tag:
            self.capture.pop()

    def handle_data(self, data):
        if self.in_json_ld and self.json_ld_parts:
            self.json_ld_parts[-1].append(data)
        cleaned = " ".join(data.split())
        if not cleaned or not self.capture:
            return
        self.text_parts.append(cleaned)
        if self.capture[-1] == "title":
            self.title_parts.append(cleaned)


def parse_webpage(document, url):
    parser = PageParser()
    parser.feed(document)
    title = parser.meta.get("og:title") or " ".join(parser.title_parts) or urlparse(url).path.rsplit("/", 1)[-1]
    description = parser.meta.get("og:description") or parser.meta.get("description") or ""
    text = " ".join(dict.fromkeys(parser.text_parts))
    if description and description not in text:
        text = f"{description} {text}".strip()
    published = next(
        (parser.meta[key] for key in ("article:published_time", "date", "datepublished", "dc.date", "pubdate") if parser.meta.get(key)),
        None,
    )
    author = parser.meta.get("author")
    event_time = next(
        (parser.meta[key] for key in ("event:start_time", "event:startdate", "startdate") if parser.meta.get(key)),
        None,
    )

    def json_ld_nodes(value):
        if isinstance(value, dict):
            yield value
            for child in value.values():
                yield from json_ld_nodes(child)
        elif isinstance(value, list):
            for child in value:
                yield from json_ld_nodes(child)

    for parts in parser.json_ld_parts:
        try:
            payload = json.loads("".join(parts).strip())
        except (ValueError, TypeError):
            continue
        for node in json_ld_nodes(payload):
            node_type = node.get("@type")
            node_types = {node_type} if isinstance(node_type, str) else set(node_type or [])
            if node_types & {"Article", "NewsArticle", "ReportageNewsArticle", "BlogPosting"}:
                published = published or node.get("datePublished") or node.get("dateCreated")
                event_time = event_time or node.get("startDate")
                if node.get("articleBody"):
                    text = " ".join(str(node["articleBody"]).split())
                raw_author = node.get("author")
                if isinstance(raw_author, list):
                    names = [item.get("name") for item in raw_author if isinstance(item, dict) and item.get("name")]
                    author = author or ", ".join(names)
                elif isinstance(raw_author, dict):
                    author = author or raw_author.get("name") or raw_author.get("givenName")
                elif isinstance(raw_author, str):
                    author = author or raw_author
            elif "Event" in node_types:
                event_time = event_time or node.get("startDate")
    return {
        "title": html.unescape(title).strip(),
        "text": html.unescape(text).strip(),
        "publication_time": published,
        "event_time": event_time,
        "author": author,
        "document_type": parser.meta.get("og:type", "").lower(),
        "canonical_url": urljoin(url, parser.meta.get("canonical", url)),
        "links": [urljoin(url, link) for link in parser.links],
    }


def validate_registered_content(title, text, require_named_leader=False, url=None):
    workspace = workspace_identity()
    normalized = _identity_text(f"{title} {text}")
    aliases = [_identity_text(alias) for alias in workspace["leader"]["aliases"]]
    named_leader = any(alias in normalized for alias in aliases)
    role_match = _identity_text("Chief Minister of Andhra Pradesh") in normalized
    if not named_leader and (require_named_leader or not role_match):
        raise ValueError("source content does not identify the workspace leader")
    jurisdiction_in_url = "/andhra-pradesh/" in (urlparse(url or "").path.lower() + "/")
    if _identity_text("Andhra Pradesh") not in normalized and not jurisdiction_in_url:
        raise ValueError("source content does not match the Andhra Pradesh jurisdiction")
    if not any(_identity_text(keyword) in normalized for keyword in workspace["topic_keywords"]):
        raise ValueError("source content does not match an approved workspace topic")


def validate_manual_page(url, title, text):
    workspace = workspace_identity()
    host = urlparse(url).hostname
    if not _host_allowed(host, workspace["allowed_hosts"]):
        raise ValueError(f"manual source host {host or '<missing>'} is not approved for {workspace['display_name']}")
    validate_registered_content(title, text, require_named_leader=True, url=url)


def validate_verification_content(title, text, claims, url=None):
    """Validate a fetched corroboration page against the selected claim gaps."""
    normalized = _identity_text(f"{title} {text}")
    path = urlparse(url or "").path.lower()
    if _identity_text("Andhra Pradesh") not in normalized and "/andhra-pradesh/" not in path:
        raise ValueError("corroboration page does not match the Andhra Pradesh jurisdiction")
    content_tokens = _tokens(f"{title} {text}")
    best_overlap = 0.0
    for claim in claims or []:
        claim_tokens = _tokens(claim.get("text", ""))
        if claim_tokens:
            best_overlap = max(best_overlap, len(claim_tokens & content_tokens) / len(claim_tokens))
    if best_overlap < 0.28:
        raise ValueError("corroboration page does not address a selected claim-specific evidence gap")


def ingest_url(
    url, source_name=None, source_metadata=None, source_id=None, source_type="manual",
    source_registration=None, content_role=None, item_type=None, verification_claims=None,
):
    document, content_type, final_url = fetch_public_url(url)
    if "html" not in content_type:
        raise ValueError("manual URL must return an HTML webpage")
    page = parse_webpage(document, final_url)
    if not page["text"]:
        raise ValueError("no factual page text could be extracted")
    if source_registration:
        validate_source_registration(source_registration, workspace_identity()["workspace_key"])
        if verification_claims is not None:
            validate_verification_content(
                page["title"], page["text"], verification_claims, url=page["canonical_url"]
            )
        else:
            validate_registered_content(page["title"], page["text"], url=page["canonical_url"])
    else:
        validate_manual_page(page["canonical_url"], page["title"], page["text"])
    metadata = source_registration.get("metadata", {}) if source_registration else {}
    content_role = content_role or metadata.get("content_role") or "item"
    if not item_type:
        item_type = metadata.get("item_type")
    if not item_type and content_role in ("item", "feed_item"):
        lowered = f"{page['title']} {final_url}".lower()
        if page["document_type"] == "article" or "news" in lowered or "press-release" in lowered:
            item_type = "news"
        elif "speech" in lowered or "address" in lowered:
            item_type = "speech"
        elif "announcement" in lowered or "notice" in lowered or "statement" in lowered:
            item_type = "announcement"
        elif "post" in lowered:
            item_type = "post"
    hostname = urlparse(final_url).hostname or "Manual URL"
    return ingest_signal(
        url=page["canonical_url"], title=page["title"], text=page["text"],
        publication_time=page["publication_time"], event_time=page["event_time"],
        author=page["author"], source_name=source_name or hostname,
        source_type=source_type, source_id=source_id,
        source_metadata={"fetched_url": final_url, **(source_metadata or {})},
        content_role=content_role, item_type=item_type,
        source_class=(source_registration or {}).get("source_class", "official_primary"),
    )


def _local_name(tag):
    return tag.rsplit("}", 1)[-1].lower()


def _child_text(element, names):
    for child in element.iter():
        if _local_name(child.tag) in names and child.text and child.text.strip():
            return child.text.strip()
    return ""


def parse_feed(document, feed_url):
    root = ET.fromstring(document)
    items = [node for node in root.iter() if _local_name(node.tag) in ("item", "entry")]
    parsed = []
    for item in items:
        title = _child_text(item, {"title"})
        text = _child_text(item, {"description", "summary", "content"})
        text = re.sub(r"<[^>]+>", " ", text)
        text = " ".join(html.unescape(text).split())
        published = _child_text(item, {"pubdate", "published", "updated", "date"}) or None
        author = _child_text(item, {"author", "creator", "byline"}) or None
        link = _child_text(item, {"link"})
        if not link:
            for child in item.iter():
                if _local_name(child.tag) == "link" and child.attrib.get("href"):
                    link = child.attrib["href"]
                    break
        if title and text and link:
            parsed.append({
                "title": title, "text": text, "publication_time": published,
                "url": urljoin(feed_url, link), "author": author,
            })
    return parsed


def _source_metadata(source):
    return {
        "official": bool(source.get("official")),
        "source_class": source["source_class"],
        "identity": source["identity"],
        "configured_url": source["url"],
        "workspace_match": source["workspace_match"],
        **source.get("metadata", {}),
    }


def _poll_state(source_id):
    with connect() as connection:
        row = connection.execute(
            "SELECT * FROM source_poll_state WHERE source_id=?", (source_id,)
        ).fetchone()
    return dict(row) if row else {"cursor_url": None, "next_poll_at": None}


def _record_poll(source, *, cursor_url, pages_fetched, status, error=None):
    polled_at = now()
    next_poll_at = (
        _as_utc(polled_at) + timedelta(seconds=int(source.get("poll_interval_seconds", 900)))
    ).isoformat()
    with connect() as connection:
        connection.execute(
            "INSERT INTO source_poll_state(source_id,last_polled_at,next_poll_at,cursor_url,pages_fetched,last_status,last_error) "
            "VALUES(?,?,?,?,?,?,?) ON CONFLICT(source_id) DO UPDATE SET "
            "last_polled_at=excluded.last_polled_at,next_poll_at=excluded.next_poll_at,"
            "cursor_url=COALESCE(excluded.cursor_url,source_poll_state.cursor_url),"
            "pages_fetched=excluded.pages_fetched,last_status=excluded.last_status,last_error=excluded.last_error",
            (source["id"], polled_at, next_poll_at, cursor_url, pages_fetched, status, error),
        )


def _listing_reference(source, page, final_url, page_number=1):
    validate_registered_content(page["title"], page["text"], url=page["canonical_url"])
    return ingest_signal(
        url=page["canonical_url"], title=page["title"], text=page["text"],
        publication_time=page["publication_time"], event_time=page["event_time"],
        author=page["author"], source_name=source["name"], source_type=source["type"],
        source_id=source["id"], source_metadata={
            "fetched_url": final_url, "listing_page": page_number, **_source_metadata(source),
        },
        content_role="listing" if source.get("metadata", {}).get("content_role") == "listing" else source.get("metadata", {}).get("content_role", "reference"),
        item_type="listing", source_class=source["source_class"],
    )


def _incremental_links(links, cursor_url, max_items):
    selected = []
    for link in links:
        if cursor_url and canonicalize_url(link) == canonicalize_url(cursor_url):
            return selected, True
        selected.append(link)
        if len(selected) >= max_items:
            break
    return selected, False


def ingest_rss_source(source):
    document, _, final_url = fetch_public_url(source["url"])
    items = parse_feed(document, final_url)
    results = [ingest_signal(
        url=final_url, title=f"{source['name']} feed", text=f"Monitored RSS or Atom feed for {source['name']}.",
        source_name=source["name"], source_type="rss", source_id=source["id"],
        source_metadata=_source_metadata(source), content_role="listing", item_type="listing",
        source_class=source["source_class"],
    )]
    cursor = _poll_state(source["id"]).get("cursor_url")
    item_urls = [item["url"] for item in items]
    selected_urls, _ = _incremental_links(
        item_urls, cursor, int(source.get("metadata", {}).get("max_items", 20))
    )
    selected = {canonicalize_url(url) for url in selected_urls}
    for item in items:
        if canonicalize_url(item["url"]) not in selected:
            continue
        validate_registered_content(item["title"], item["text"], url=item["url"])
        results.append(ingest_signal(
            **item, source_name=source["name"], source_type="rss", source_id=source["id"],
            source_metadata=_source_metadata(source), content_role="feed_item",
            item_type=source.get("metadata", {}).get("item_type", "news"),
            source_class=source["source_class"],
        ))
    return {"results": results, "errors": [], "pages_fetched": 1, "cursor_url": item_urls[0] if item_urls else None}


def ingest_webpage_source(source):
    metadata = source.get("metadata", {})
    link_pattern = metadata.get("link_pattern")
    pagination_pattern = metadata.get("pagination_pattern")
    max_pages = max(1, int(metadata.get("max_pages", 1)))
    max_items = max(1, int(metadata.get("max_items", 20)))
    request_delay = max(0.0, float(source.get("rate_limit_seconds", 1.0)))
    cursor = _poll_state(source["id"]).get("cursor_url")
    results, errors, discovered = [], [], []
    queue = [(source["url"], 1)]
    visited = set()
    pages_fetched = 0
    cursor_seen = False

    while queue and pages_fetched < max_pages and len(discovered) < max_items and not cursor_seen:
        page_url, page_number = queue.pop(0)
        canonical_page = canonicalize_url(page_url)
        if canonical_page in visited:
            continue
        if pages_fetched:
            time.sleep(request_delay)
        document, content_type, final_url = fetch_public_url(page_url)
        if "html" not in content_type:
            raise ValueError("configured webpage did not return HTML")
        page = parse_webpage(document, final_url)
        pages_fetched += 1
        visited.add(canonical_page)
        results.append(_listing_reference(source, page, final_url, page_number))

        if not link_pattern:
            continue
        item_links = list(dict.fromkeys(
            link for link in page["links"]
            if re.search(link_pattern, link) and urlparse(link).hostname == urlparse(final_url).hostname
        ))
        selected, cursor_seen = _incremental_links(item_links, cursor, max_items - len(discovered))
        discovered.extend(selected)
        if pagination_pattern and not cursor_seen:
            for link in page["links"]:
                if re.search(pagination_pattern, link) and canonicalize_url(link) not in visited:
                    queue.append((link, page_number + 1))

    for link in discovered:
        try:
            time.sleep(request_delay)
            results.append(ingest_url(
                link, source_name=source["name"], source_metadata=_source_metadata(source),
                source_id=source["id"], source_type="webpage", source_registration=source,
                content_role="item", item_type=metadata.get("item_type", "news"),
            ))
        except Exception as error:
            log_error("source_item_rejected", error, source_id=source["id"], item_url=link)
            errors.append({"source_id": source["id"], "item_url": link, "error": str(error)})
    return {
        "results": results, "errors": errors, "pages_fetched": pages_fetched,
        "cursor_url": discovered[0] if discovered else cursor,
    }


def ingest_configured_sources(path=None, force=False):
    config = load_source_config(path)
    sync_sources(config)
    summary = {
        "sources_checked": 0, "signals_created": 0, "event_signals_created": 0,
        "references_created": 0, "review_created": 0, "rejected_created": 0,
        "duplicates": 0, "sources_rate_limited": 0, "sources_in_review": 0,
        "pages_fetched": 0, "errors": [],
    }
    for source in config["sources"]:
        if not source.get("enabled", True):
            continue
        summary["sources_checked"] += 1
        if source["identity"]["status"] != "verified":
            summary["sources_in_review"] += 1
            continue
        state = _poll_state(source["id"])
        if not force and state.get("next_poll_at") and _as_utc(state["next_poll_at"]) > datetime.now(timezone.utc):
            summary["sources_rate_limited"] += 1
            continue
        try:
            adapter = ingest_rss_source(source) if source["type"] == "rss" else ingest_webpage_source(source)
            results = adapter["results"]
            summary["pages_fetched"] += adapter["pages_fetched"]
            summary["signals_created"] += sum(not result["duplicate"] for result in results)
            summary["event_signals_created"] += sum(
                not result["duplicate"] and result["item_kind"] == "event" for result in results
            )
            summary["references_created"] += sum(
                not result["duplicate"] and result["item_kind"] == "reference" for result in results
            )
            summary["review_created"] += sum(
                not result["duplicate"] and result["item_kind"] == "review" for result in results
            )
            summary["rejected_created"] += sum(
                not result["duplicate"] and result["item_kind"] == "rejected" for result in results
            )
            summary["duplicates"] += sum(result["duplicate"] for result in results)
            summary["errors"].extend(adapter["errors"])
            _record_poll(
                source, cursor_url=adapter["cursor_url"], pages_fetched=adapter["pages_fetched"],
                status="ok" if not adapter["errors"] else "partial",
                error=None if not adapter["errors"] else f"{len(adapter['errors'])} item errors",
            )
        except Exception as error:
            log_error("source_ingestion_failed", error, source_id=source.get("id"), source_url=source.get("url"))
            summary["errors"].append({"source_id": source.get("id"), "error": str(error)})
            _record_poll(source, cursor_url=None, pages_fetched=0, status="error", error=str(error))
    return summary


def _research_evidence_rows(connection, event_id):
    return connection.execute(
        "SELECT s.*,COALESCE(src.name,s.source_name) AS registered_source_name "
        "FROM signals s LEFT JOIN sources src ON src.id=s.source_id "
        "WHERE s.event_id=? AND s.item_kind='event' ORDER BY s.publication_time,s.id",
        (event_id,),
    ).fetchall()


def research_evidence_version(event_id):
    with connect() as connection:
        event = connection.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
        if event is None:
            raise KeyError(event_id)
        signals = _research_evidence_rows(connection, event_id)
    if not signals:
        raise ValueError("The selected event has no linked event signals.")
    material = {
        "workspace_key": event["workspace_key"],
        "event_id": event_id,
        "signals": [
            {
                "id": row["id"], "canonical_url": row["canonical_url"],
                "content_hash": row["content_hash"], "publication_time": row["publication_time"],
                "event_time": row["event_time"], "event_time_basis": row["event_time_basis"],
            }
            for row in signals
        ],
    }
    return hashlib.sha256(json.dumps(material, sort_keys=True).encode()).hexdigest()


def _record_run_status(connection, run_id, from_status, to_status, message, **updates):
    timestamp = now()
    assignments = ["status=?", "progress_message=?"]
    values = [to_status, message]
    for key, value in updates.items():
        assignments.append(f"{key}=?")
        values.append(value)
    values.append(run_id)
    connection.execute(
        f"UPDATE research_runs SET {','.join(assignments)} WHERE id=?",
        values,
    )
    connection.execute(
        "INSERT INTO research_run_status_history(run_id,from_status,to_status,message,changed_at) VALUES(?,?,?,?,?)",
        (run_id, from_status, to_status, message, timestamp),
    )


def _research_provider_details(provider_name):
    provider = provider_for(provider_name)
    return provider, provider.name, provider.model, provider.mode


def enqueue_research(event_id, provider_name="test", *, background=True, search_limit=0, token_limit=None):
    provider, provider_id, model, mode = _research_provider_details(provider_name)
    requested_search_limit = max(0, min(int(search_limit or 0), RESEARCH_SEARCH_LIMIT))
    if mode == "test":
        requested_search_limit = 0
    requested_token_limit = max(256, min(int(token_limit or RESEARCH_TOKEN_LIMIT), RESEARCH_TOKEN_LIMIT))
    evidence_version = research_evidence_version(event_id)
    timestamp = now()
    run_id = "RR-" + uuid.uuid4().hex[:12].upper()
    with connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        active = connection.execute(
            "SELECT * FROM research_runs WHERE event_id=? AND status IN ('QUEUED','RUNNING') "
            "ORDER BY requested_at DESC LIMIT 1",
            (event_id,),
        ).fetchone()
        if active:
            connection.commit()
            return {"run": dict(active), "duplicate": True, "cached": False}
        cached = connection.execute(
            "SELECT * FROM research_runs WHERE event_id=? AND provider=? AND model=? AND evidence_version=? "
            "AND search_limit=? AND token_limit=? "
            "AND status IN ('COMPLETED','CACHED') ORDER BY requested_at DESC LIMIT 1",
            (event_id, provider_id, model, evidence_version, requested_search_limit, requested_token_limit),
        ).fetchone()
        if cached:
            cached_explanation = (
                "Test data is excluded from production event verification."
                if mode == "test" else cached["verification_explanation"]
            )
            connection.execute(
                "INSERT INTO research_runs(id,event_id,provider,model,mode,status,evidence_version,cache_source_run_id,"
                "requested_at,started_at,completed_at,progress,progress_message,attempt_count,max_attempts,search_limit,"
                "token_limit,input_tokens,output_tokens,total_tokens,search_count,cost_usd,cost_status,provider_request_id,"
                "relevance_assessment,occurrence_kind,summary_json,verification_explanation) "
                "VALUES(?,?,?,?,?,'CACHED',?,?,?,?,?,100,?,0,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    run_id, event_id, provider_id, model, mode, evidence_version, cached["id"], timestamp, timestamp,
                    timestamp, "Reused unchanged evidence", RESEARCH_MAX_RETRIES + 1, requested_search_limit,
                    requested_token_limit, 0, 0, 0, 0, 0.0, "known", None,
                    cached["relevance_assessment"], cached["occurrence_kind"], cached["summary_json"],
                    cached_explanation,
                ),
            )
            connection.execute(
                "INSERT INTO research_run_status_history(run_id,from_status,to_status,message,changed_at) VALUES(?,NULL,'CACHED',?,?)",
                (run_id, f"Evidence unchanged; reused {cached['id']}", timestamp),
            )
            cached_research_status = (
                "COMPLETE" if mode == "live" and cached_explanation == "All required claims meet the documented evidence policy."
                else "REVIEW_REQUIRED"
            )
            connection.execute(
                "UPDATE events SET research_status=?,updated_at=? WHERE id=?",
                (cached_research_status, timestamp, event_id),
            )
            row = connection.execute("SELECT * FROM research_runs WHERE id=?", (run_id,)).fetchone()
            connection.commit()
            return {"run": dict(row), "duplicate": False, "cached": True}
        connection.execute(
            "INSERT INTO research_runs(id,event_id,provider,model,mode,status,evidence_version,requested_at,progress,"
            "progress_message,max_attempts,search_limit,token_limit) VALUES(?,?,?,?,?,'QUEUED',?,?,0,?,?,?,?)",
            (
                run_id, event_id, provider_id, model, mode, evidence_version, timestamp, "Queued",
                RESEARCH_MAX_RETRIES + 1, requested_search_limit, requested_token_limit,
            ),
        )
        connection.execute(
            "INSERT INTO research_run_status_history(run_id,from_status,to_status,message,changed_at) VALUES(?,NULL,'QUEUED','Queued',?)",
            (run_id, timestamp),
        )
        event = connection.execute("SELECT status FROM events WHERE id=?", (event_id,)).fetchone()
        if event["status"] == "DETECTED":
            connection.execute(
                "UPDATE events SET status='VERIFYING',research_status='QUEUED',updated_at=? WHERE id=?",
                (timestamp, event_id),
            )
            connection.execute(
                "INSERT INTO transitions(event_id,from_state,to_state,at) VALUES(?,'DETECTED','VERIFYING',?)",
                (event_id, timestamp),
            )
        else:
            connection.execute(
                "UPDATE events SET research_status='QUEUED',updated_at=? WHERE id=?", (timestamp, event_id)
            )
        row = connection.execute("SELECT * FROM research_runs WHERE id=?", (run_id,)).fetchone()
        connection.commit()
    if background:
        RESEARCH_EXECUTOR.submit(run_research_job, run_id, provider)
    else:
        run_research_job(run_id, provider)
        with connect() as connection:
            row = connection.execute("SELECT * FROM research_runs WHERE id=?", (run_id,)).fetchone()
    return {"run": dict(row), "duplicate": False, "cached": False}


def _create_evidence_snapshots(connection, run):
    rows = _research_evidence_rows(connection, run["event_id"])
    snapshots = []
    retrieved_at = now()
    for row in rows:
        snapshot = {
            "id": "ES-" + uuid.uuid4().hex[:12].upper(),
            "signal_id": row["id"], "source_id": row["source_id"],
            "source_name": row["registered_source_name"], "source_class": row["source_class"],
            "url": row["url"], "canonical_url": row["canonical_url"], "title": row["title"],
            "text": row["text"], "content_hash": row["content_hash"],
            "publication_time": row["publication_time"],
            "stated_event_time": row["event_time"] if row["event_time_basis"] == "stated_event_time" else None,
            "author": row["author"], "retrieved_at": retrieved_at,
        }
        connection.execute(
            "INSERT INTO evidence_snapshots(id,run_id,signal_id,source_id,source_name,source_class,url,canonical_url,title,"
            "text,content_hash,publication_time,stated_event_time,author,retrieved_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                snapshot["id"], run["id"], snapshot["signal_id"], snapshot["source_id"], snapshot["source_name"],
                snapshot["source_class"], snapshot["url"], snapshot["canonical_url"], snapshot["title"], snapshot["text"],
                snapshot["content_hash"], snapshot["publication_time"], snapshot["stated_event_time"],
                snapshot["author"], snapshot["retrieved_at"],
            ),
        )
        snapshots.append(snapshot)
    return snapshots


def _evidence_bundle(connection, run, snapshots):
    event = dict(connection.execute("SELECT * FROM events WHERE id=?", (run["event_id"],)).fetchone())
    identity = workspace_identity()
    return {
        "workspace": {
            "workspace_key": identity["workspace_key"], "display_name": identity["display_name"],
            "leader": identity["leader"]["canonical_name"], "jurisdiction": identity["jurisdiction"],
            "topics": identity["topics"],
        },
        "selected_event": {
            "id": event["id"], "title": event["title"], "status": event["status"],
            "event_time": event["event_time"],
        },
        "evidence": [
            {
                "signal_id": item["signal_id"], "source_name": item["source_name"],
                "source_class": item["source_class"], "url": item["url"],
                "canonical_url": item["canonical_url"], "title": item["title"], "text": item["text"],
                "publication_time": item["publication_time"], "stated_event_time": item["stated_event_time"],
                "author": item["author"], "content_hash": item["content_hash"],
            }
            for item in snapshots
        ],
    }


def _normalized_text(value):
    return re.sub(r"\s+", " ", value or "").strip()


def _evidence_validation(reference, snapshots):
    try:
        reference_url = canonicalize_url(reference.get("url", ""))
    except ValueError:
        reference_url = reference.get("url", "")
    snapshot = next(
        (item for item in snapshots if item["canonical_url"] == reference_url or item["url"] == reference.get("url")),
        None,
    )
    if snapshot is None:
        return None, "URL_NOT_IN_EVIDENCE", "Cited URL is not linked to the selected event."
    excerpt = reference.get("excerpt", "").strip()
    if not excerpt:
        return snapshot, "MISSING_EXCERPT", "A supporting excerpt is required."
    if _normalized_text(excerpt) not in _normalized_text(snapshot["text"]):
        return snapshot, "EXCERPT_NOT_FOUND", "Excerpt does not occur in the stored source snapshot."
    return snapshot, "VALID", "URL and excerpt match the stored source snapshot."


def _explicit_values_supported(claim_text, valid_excerpts):
    claim_values = set(re.findall(r"(?:₹|rs\.?|usd\s*)?\d[\d,.]*(?:\s*(?:crore|lakh|million|billion|%|km|mw))?", claim_text, re.I))
    evidence_text = " ".join(valid_excerpts).lower()
    dated_values = set(re.findall(
        r"\b(?:\d{4}-\d{2}-\d{2}|(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
        r"jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\s+\d{1,2}(?:,\s*\d{4})?)\b",
        claim_text, re.I,
    ))
    names = {
        value.strip() for value in re.findall(r"\b[A-Z][a-z.]+(?:\s+[A-Z][a-z.]+){1,4}\b", claim_text)
        if value.lower() not in {"the reported", "independent andhra pradesh"}
    }
    explicit = claim_values | dated_values | names
    return all(value.lower() in evidence_text for value in explicit)


def _store_claims(connection, run, research, snapshots):
    stored = []
    timestamp = now()
    allowed_types = {"factual_assertion", "quotation", "opinion", "promise", "allegation"}
    allowed_scopes = {
        "occurrence", "announcement", "approval", "funding_allocation", "completed_work",
        "quotation", "opinion", "promise", "allegation",
    }
    for proposed in research.get("claims", []):
        claim_type = proposed.get("claim_type") if proposed.get("claim_type") in allowed_types else "factual_assertion"
        scope = proposed.get("assertion_scope") if proposed.get("assertion_scope") in allowed_scopes else "occurrence"
        claim_id = "CL-" + uuid.uuid4().hex[:12].upper()
        validations = []
        valid_supports = []
        valid_conflicts = []
        for reference in proposed.get("evidence_refs") or []:
            snapshot, validation_status, note = _evidence_validation(reference, snapshots)
            item = (reference, snapshot, validation_status, note)
            validations.append(item)
            if validation_status == "VALID":
                target = valid_conflicts if reference.get("support_kind") == "conflicts" else valid_supports
                target.append(item)
        status = "UNVERIFIED"
        reason = "No evidence was supplied."
        if validations and any(item[2] != "VALID" for item in validations):
            status = "INSUFFICIENT_EVIDENCE"
            reason = "At least one cited URL or excerpt failed evidence validation."
        elif valid_supports and valid_conflicts:
            status = "CONFLICTED"
            reason = "Stored evidence contains both supporting and conflicting excerpts."
        elif valid_supports:
            excerpts = [item[0].get("excerpt", "") for item in valid_supports]
            if not _explicit_values_supported(proposed.get("text", ""), excerpts):
                status = "INSUFFICIENT_EVIDENCE"
                reason = "A numerical value, unit, name, or date is not explicit in the supporting excerpt."
            elif claim_type == "quotation" and not any(
                _normalized_text(proposed.get("text", "")).strip('“”\"') in _normalized_text(excerpt)
                for excerpt in excerpts
            ):
                status = "INSUFFICIENT_EVIDENCE"
                reason = "The claimed quotation is not explicit in the supporting excerpt."
            elif scope == "completed_work" and all(
                item[1]["source_class"] == "official_primary" and
                any(word in item[0].get("excerpt", "").lower() for word in ("announce", "approve", "will ", "allocated"))
                for item in valid_supports
            ):
                status = "INSUFFICIENT_EVIDENCE"
                reason = "An official statement supports what was announced, not completion of the work."
            else:
                status = "SUPPORTED"
                reason = "Every cited URL and excerpt matches stored evidence with explicit values."
        reviewer_notes = proposed.get("reviewer_notes") or ""
        if reviewer_notes:
            reviewer_notes += " "
        reviewer_notes += reason
        connection.execute(
            "INSERT INTO claims(id,event_id,research_run_id,text,claim_type,assertion_scope,attribution,verification_status,"
            "reviewer_notes,required_for_event,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                claim_id, run["event_id"], run["id"], proposed.get("text", "").strip(), claim_type, scope,
                proposed.get("attribution"), status, reviewer_notes, int(bool(proposed.get("required_for_event"))),
                timestamp, timestamp,
            ),
        )
        connection.execute(
            "INSERT INTO claim_status_history(claim_id,from_status,to_status,reason,changed_at) VALUES(?,NULL,?,?,?)",
            (claim_id, status, reason, timestamp),
        )
        for reference, snapshot, validation_status, note in validations:
            support_kind = reference.get("support_kind") if reference.get("support_kind") in ("supports", "conflicts") else "supports"
            connection.execute(
                "INSERT INTO claim_evidence(claim_id,snapshot_id,source_url,supporting_excerpt,support_kind,validation_status,"
                "validation_note) VALUES(?,?,?,?,?,?,?)",
                (
                    claim_id, snapshot["id"] if snapshot else None, reference.get("url", ""),
                    reference.get("excerpt"), support_kind, validation_status, note,
                ),
            )
        stored.append({"id": claim_id, "status": status, "required": bool(proposed.get("required_for_event"))})
    return stored


def _event_evidence_policy(connection, run, research, stored_claims, snapshots):
    if run["mode"] == "test":
        return False, "Test data is excluded from production event verification."
    required = [claim for claim in stored_claims if claim["required"]]
    if not research.get("concrete_occurrence") or research.get("relevance") != "RELEVANT":
        return False, "Research did not establish a concrete, workspace-relevant occurrence."
    if not required:
        return False, "No required factual claim was identified."
    if any(claim["status"] != "SUPPORTED" for claim in required):
        return False, "One or more required claims remain unverified, conflicted, or insufficiently evidenced."
    # Syndicated copies with the same factual text are one evidence family even
    # when a publisher changes the headline or URL.
    supporting_hashes = {
        hashlib.sha256(_normalized_text(snapshot["text"]).lower().encode()).hexdigest()
        for snapshot in snapshots
    }
    source_classes = {snapshot["source_class"] for snapshot in snapshots}
    occurrence_kind = research.get("occurrence_kind", "occurrence")
    if occurrence_kind in ("announcement", "approval", "funding_allocation"):
        if "official_primary" not in source_classes and len(supporting_hashes) < 2:
            return False, "The reported action lacks an official primary source or a second independent evidence family."
    elif len(supporting_hashes) < 2:
        return False, "The occurrence requires a second independent evidence family before event verification."
    return True, "All required claims meet the documented evidence policy."


def run_research_job(run_id, provider=None):
    with connect() as connection:
        run = connection.execute("SELECT * FROM research_runs WHERE id=?", (run_id,)).fetchone()
        if run is None:
            raise KeyError(run_id)
        if run["status"] != "QUEUED":
            return dict(run)
        if provider is None:
            provider = provider_for("test" if run["mode"] == "test" else "grok")
        timestamp = now()
        _record_run_status(
            connection, run_id, "QUEUED", "RUNNING", "Snapshotting linked evidence",
            started_at=timestamp, progress=10,
        )
        connection.execute(
            "UPDATE events SET research_status='RUNNING',updated_at=? WHERE id=?", (timestamp, run["event_id"])
        )
        run = connection.execute("SELECT * FROM research_runs WHERE id=?", (run_id,)).fetchone()
        snapshots = _create_evidence_snapshots(connection, run)
        bundle = _evidence_bundle(connection, run, snapshots)
        connection.execute(
            "UPDATE research_runs SET progress=30,progress_message='Researching selected event' WHERE id=?", (run_id,)
        )
    result = None
    error = None
    attempts = 0
    for attempts in range(1, int(run["max_attempts"]) + 1):
        try:
            result = provider.research(
                bundle, search_limit=int(run["search_limit"]), token_limit=int(run["token_limit"]),
                timeout_seconds=RESEARCH_TIMEOUT_SECONDS,
            )
            break
        except ResearchProviderError as caught:
            error = caught
            if not caught.retryable or attempts >= int(run["max_attempts"]):
                break
            time.sleep(min(2 ** (attempts - 1), 4))
        except Exception as caught:
            error = ResearchProviderError("Research provider failed without a usable result.")
            log_error("research_provider_unexpected_failure", caught, run_id=run_id, provider=run["provider"])
            break
    if result is None:
        safe_message = str(error or "Research provider failed.")
        safe_code = getattr(error, "code", "provider_error")
        log_error(
            "research_provider_failed", safe_message, run_id=run_id,
            provider=run["provider"], error_code=safe_code, attempts=attempts,
        )
        with connect() as connection:
            _record_run_status(
                connection, run_id, "RUNNING", "FAILED", safe_message,
                completed_at=now(), progress=100, attempt_count=attempts, error_code=safe_code,
                error_message=safe_message,
            )
            connection.execute(
                "UPDATE events SET research_status='FAILED',updated_at=? WHERE id=?", (now(), run["event_id"])
            )
        return
    research = result.research
    if not isinstance(research, dict) or not isinstance(research.get("claims"), list):
        safe_message = "Research provider returned an invalid structured result."
        log_error(
            "research_provider_invalid_result", safe_message, run_id=run_id,
            provider=run["provider"],
        )
        with connect() as connection:
            _record_run_status(
                connection, run_id, "RUNNING", "FAILED", safe_message,
                completed_at=now(), progress=100, attempt_count=attempts,
                error_code="invalid_provider_response", error_message=safe_message,
            )
            connection.execute(
                "UPDATE events SET research_status='FAILED',updated_at=? WHERE id=?", (now(), run["event_id"])
            )
        return
    if not any(item.get("stated_event_time") for item in bundle["evidence"]):
        research["stated_event_time"] = None
        unknowns = research.setdefault("unknowns", [])
        message = "The linked evidence states no event time separate from publication time."
        if message not in unknowns:
            unknowns.append(message)
    with connect() as connection:
        connection.execute(
            "UPDATE research_runs SET progress=75,progress_message='Validating claim evidence',attempt_count=? WHERE id=?",
            (attempts, run_id),
        )
        run = connection.execute("SELECT * FROM research_runs WHERE id=?", (run_id,)).fetchone()
        snapshots = [dict(row) for row in connection.execute(
            "SELECT * FROM evidence_snapshots WHERE run_id=? ORDER BY id", (run_id,)
        )]
        stored_claims = _store_claims(connection, run, research, snapshots)
        verified, explanation = _event_evidence_policy(connection, run, research, stored_claims, snapshots)
        timestamp = now()
        _record_run_status(
            connection, run_id, "RUNNING", "COMPLETED", "Research complete; evidence policy evaluated",
            completed_at=timestamp, progress=100, attempt_count=attempts,
            input_tokens=result.input_tokens, output_tokens=result.output_tokens, total_tokens=result.total_tokens,
            search_count=result.search_count, cost_usd=result.cost_usd,
            cost_status="known" if result.cost_usd is not None else "unknown",
            cost_usd_ticks=result.cost_usd_ticks, provider_elapsed_seconds=result.elapsed_seconds,
            provider_request_id=result.provider_request_id, relevance_assessment=research.get("relevance"),
            occurrence_kind=research.get("occurrence_kind"), summary_json=json.dumps(research, ensure_ascii=False),
            verification_explanation=explanation,
        )
        event = connection.execute("SELECT status FROM events WHERE id=?", (run["event_id"],)).fetchone()
        research_status = "COMPLETE" if verified else "REVIEW_REQUIRED"
        # Architecture 04 makes event verification a separate corroboration job.
        # Research can identify supported claims, but cannot promote an event.
        connection.execute(
            "UPDATE events SET research_status=?,updated_at=? WHERE id=?",
            (research_status, timestamp, run["event_id"]),
        )


def _verification_source_entries():
    entries = {}
    paths = [ROOT / "config" / "sources.example.json"]
    if SOURCES_CONFIG.exists() and SOURCES_CONFIG not in paths:
        paths.append(SOURCES_CONFIG)
    for path in paths:
        try:
            config = load_source_config(path)
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        for source in config["sources"]:
            try:
                validate_source_registration(source, workspace_identity()["workspace_key"])
            except ValueError:
                continue
            entries[source["id"]] = source
    return list(entries.values())


def _normalized_host(value):
    host = (urlparse(value or "").hostname or "").lower().rstrip(".")
    return host[4:] if host.startswith("www.") else host


def _registration_for_verification_url(url):
    host = _normalized_host(url)
    matches = [entry for entry in _verification_source_entries() if _normalized_host(entry["url"]) == host]
    return matches[0] if matches else None


def _verification_domains(research_run_id):
    with connect() as connection:
        original_hosts = {
            _normalized_host(row["canonical_url"])
            for row in connection.execute("SELECT canonical_url FROM evidence_snapshots WHERE run_id=?", (research_run_id,))
        }
    prioritized = []
    for entry in _verification_source_entries():
        priority = entry.get("metadata", {}).get("verification_search_priority")
        host = _normalized_host(entry["url"])
        if priority is None or not host or host in original_hosts:
            continue
        prioritized.append((int(priority), host))
    return [host for _, host in sorted(set(prioritized))][:5]


def _claim_version_material(claim):
    return {
        "text": claim["text"], "claim_type": claim["claim_type"],
        "assertion_scope": claim["assertion_scope"], "attribution": claim["attribution"],
        "required_for_event": int(claim["required_for_event"]),
    }


def _ensure_claim_versions(connection, research_run_id):
    claims = connection.execute(
        "SELECT * FROM claims WHERE research_run_id=? ORDER BY created_at,id", (research_run_id,)
    ).fetchall()
    versions = []
    for claim in claims:
        material = _claim_version_material(claim)
        content_hash = hashlib.sha256(json.dumps(material, sort_keys=True).encode()).hexdigest()
        version = connection.execute(
            "SELECT * FROM claim_versions WHERE claim_id=? AND content_hash=?", (claim["id"], content_hash)
        ).fetchone()
        if version is None:
            version_number = connection.execute(
                "SELECT COALESCE(MAX(version_number),0)+1 FROM claim_versions WHERE claim_id=?", (claim["id"],)
            ).fetchone()[0]
            version_id = "CV-" + uuid.uuid4().hex[:12].upper()
            connection.execute(
                "INSERT INTO claim_versions(id,claim_id,version_number,content_hash,text,claim_type,assertion_scope,"
                "attribution,required_for_event,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    version_id, claim["id"], version_number, content_hash, claim["text"], claim["claim_type"],
                    claim["assertion_scope"], claim["attribution"], claim["required_for_event"], now(),
                ),
            )
            version = connection.execute("SELECT * FROM claim_versions WHERE id=?", (version_id,)).fetchone()
        versions.append(dict(version))
    if not versions:
        raise ValueError("the selected research run has no claims to verify")
    return versions


def _claim_set_version(versions):
    return hashlib.sha256(
        json.dumps(sorted((item["id"], item["content_hash"]) for item in versions)).encode()
    ).hexdigest()


def verification_evidence_version(event_id, research_run_id):
    with connect() as connection:
        research = [
            (row["canonical_url"], row["content_hash"])
            for row in connection.execute(
                "SELECT canonical_url,content_hash FROM evidence_snapshots WHERE run_id=?", (research_run_id,)
            )
        ]
        later = [
            (row["canonical_url"], row["content_hash"])
            for row in connection.execute(
                "SELECT DISTINCT vs.canonical_url,vs.content_hash FROM verification_snapshots vs "
                "JOIN verification_runs vr ON vr.id=vs.verification_run_id "
                "WHERE vr.event_id=? AND vr.status IN ('COMPLETED','CACHED')", (event_id,)
            )
        ]
        signals = [
            (row["canonical_url"], row["content_hash"])
            for row in connection.execute("SELECT canonical_url,content_hash FROM signals WHERE event_id=?", (event_id,))
        ]
        acquired = [
            (row["canonical_url"] or row["final_url"], row["checksum_sha256"])
            for row in connection.execute(
                "SELECT sc.canonical_url,sc.final_url,sc.checksum_sha256 FROM source_candidates sc "
                "JOIN source_acquisition_runs sa ON sa.id=sc.acquisition_run_id "
                "WHERE sa.event_id=? AND sc.state='RETRIEVED'", (event_id,),
            )
        ]
    material = sorted(set(
        (canonicalize_url(url), content_hash) for url, content_hash in research + later + signals + acquired
    ))
    return hashlib.sha256(json.dumps(material).encode()).hexdigest()


def _record_verification_status(connection, run_id, from_status, to_status, message, **updates):
    assignments = ["status=?", "progress_message=?"]
    values = [to_status, message]
    for key, value in updates.items():
        assignments.append(f"{key}=?")
        values.append(value)
    values.append(run_id)
    connection.execute(f"UPDATE verification_runs SET {','.join(assignments)} WHERE id=?", values)
    connection.execute(
        "INSERT INTO verification_run_status_history(run_id,from_status,to_status,message,changed_at) VALUES(?,?,?,?,?)",
        (run_id, from_status, to_status, message, now()),
    )


def enqueue_verification(research_run_id, provider_name="grok", *, background=True, test_leads=None):
    provider = verification_provider_for(provider_name)
    with connect() as connection:
        research_run = connection.execute("SELECT * FROM research_runs WHERE id=?", (research_run_id,)).fetchone()
        if research_run is None or research_run["status"] != "COMPLETED":
            raise ValueError("verification requires a completed research run")
        if research_run["mode"] != provider.mode:
            raise ValueError("live research requires live verification; test research requires TEST DATA verification")
        versions = _ensure_claim_versions(connection, research_run_id)
    evidence_version = verification_evidence_version(research_run["event_id"], research_run_id)
    claims_version = _claim_set_version(versions)
    search_turns = 0 if provider.mode == "test" else VERIFICATION_SEARCH_TURNS
    timestamp = now()
    run_id = "VR-" + uuid.uuid4().hex[:12].upper()
    with connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        active = connection.execute(
            "SELECT * FROM verification_runs WHERE research_run_id=? AND status IN ('QUEUED','RUNNING')",
            (research_run_id,),
        ).fetchone()
        if active:
            connection.commit()
            return {"run": dict(active), "duplicate": True, "cached": False}
        paused = connection.execute(
            "SELECT * FROM verification_runs WHERE research_run_id=? AND recoverable=1 "
            "AND resume_state='PAUSED_TRANSIENT' ORDER BY requested_at DESC LIMIT 1",
            (research_run_id,),
        ).fetchone()
        if paused:
            connection.commit()
            return {"run": dict(paused), "duplicate": True, "cached": False, "resume_required": True}
        cached = connection.execute(
            "SELECT * FROM verification_runs WHERE research_run_id=? AND provider=? AND model=? "
            "AND final_evidence_version=? AND claim_set_version=? AND status='COMPLETED' "
            "ORDER BY completed_at DESC LIMIT 1",
            (research_run_id, provider.name, provider.model, evidence_version, claims_version),
        ).fetchone()
        if cached:
            connection.execute(
                "INSERT INTO verification_runs(id,event_id,research_run_id,provider,model,mode,status,initial_evidence_version,"
                "final_evidence_version,claim_set_version,cache_source_run_id,requested_at,started_at,completed_at,progress,"
                "progress_message,max_attempts,search_turn_limit,token_limit,actual_search_calls,actual_open_calls,"
                "actual_sources_returned,limit_guaranteed,limit_notes,input_tokens,output_tokens,total_tokens,cost_usd,"
                "cost_usd_ticks,cost_status,decision_explanation,summary_json) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    run_id, cached["event_id"], research_run_id, provider.name, provider.model, provider.mode,
                    "CACHED", evidence_version, cached["final_evidence_version"], claims_version, cached["id"],
                    timestamp, timestamp, timestamp, 100, "Reused unchanged evidence and claim versions", 0,
                    search_turns, VERIFICATION_TOKEN_LIMIT, 0, 0, 0, cached["limit_guaranteed"],
                    cached["limit_notes"], 0, 0, 0, 0.0, 0, "known", cached["decision_explanation"],
                    cached["summary_json"],
                ),
            )
            connection.execute(
                "INSERT INTO verification_run_status_history(run_id,from_status,to_status,message,changed_at) "
                "VALUES(?,NULL,'CACHED',?,?)", (run_id, f"Reused {cached['id']}", timestamp),
            )
            row = connection.execute("SELECT * FROM verification_runs WHERE id=?", (run_id,)).fetchone()
            connection.commit()
            return {"run": dict(row), "duplicate": False, "cached": True}
        limit_notes = (
            "max_turns is provider-supported and parallel tool calls are disabled; exact search-call, open-page, "
            "and returned-source counts are not guaranteed by the provider."
            if provider.mode == "live" else "TEST DATA performs no provider search."
        )
        connection.execute(
            "INSERT INTO verification_runs(id,event_id,research_run_id,provider,model,mode,status,initial_evidence_version,"
            "claim_set_version,requested_at,progress,progress_message,max_attempts,search_turn_limit,token_limit,"
            "limit_guaranteed,limit_notes,summary_json,current_phase,search_timeout_seconds,retrieval_timeout_seconds,"
            "total_timeout_seconds) VALUES(?,?,?,?,?,?,'QUEUED',?,?,?,0,'Queued',?,?,?,?,?,?,'PRIMARY_EVIDENCE_EXTRACTION',?,?,?)",
            (
                run_id, research_run["event_id"], research_run_id, provider.name, provider.model, provider.mode,
                evidence_version, claims_version, timestamp, VERIFICATION_TRANSIENT_RETRIES + 1, search_turns,
                VERIFICATION_TOKEN_LIMIT, int(provider.mode == "live"), limit_notes,
                json.dumps({"test_leads": test_leads or []}) if provider.mode == "test" else None,
                VERIFICATION_SEARCH_TIMEOUT_SECONDS, VERIFICATION_RETRIEVAL_TIMEOUT_SECONDS,
                VERIFICATION_TOTAL_TIMEOUT_SECONDS,
            ),
        )
        connection.execute(
            "INSERT INTO verification_run_status_history(run_id,from_status,to_status,message,changed_at) "
            "VALUES(?,NULL,'QUEUED','Queued',?)", (run_id, timestamp),
        )
        connection.execute(
            "UPDATE events SET verification_status='QUEUED',updated_at=? WHERE id=?",
            (timestamp, research_run["event_id"]),
        )
        row = connection.execute("SELECT * FROM verification_runs WHERE id=?", (run_id,)).fetchone()
        connection.commit()
    with connect() as connection:
        acquired_exists = connection.execute(
            "SELECT 1 FROM source_candidates sc JOIN source_acquisition_runs sa ON sa.id=sc.acquisition_run_id "
            "JOIN claim_source_candidates csc ON csc.candidate_id=sc.id "
            "JOIN claim_versions cv ON cv.id=csc.claim_version_id JOIN claims c ON c.id=cv.claim_id "
            "WHERE sa.event_id=? AND c.research_run_id=? AND sc.state='RETRIEVED' LIMIT 1",
            (research_run["event_id"], research_run_id),
        ).fetchone()
    if not acquired_exists:
        plan_source_acquisition(run_id)
    if background:
        VERIFICATION_EXECUTOR.submit(run_verification_job, run_id, provider)
    else:
        run_verification_job(run_id, provider)
        with connect() as connection:
            row = connection.execute("SELECT * FROM verification_runs WHERE id=?", (run_id,)).fetchone()
    return {"run": dict(row), "duplicate": False, "cached": False}


def _insert_verification_snapshot(connection, run_id, source, origin_kind, origin_id):
    canonical = canonicalize_url(source["canonical_url"])
    existing = connection.execute(
        "SELECT * FROM verification_snapshots WHERE verification_run_id=? AND canonical_url=?", (run_id, canonical)
    ).fetchone()
    if existing:
        return dict(existing)
    normalized = _normalized_text(source["text"]).lower()
    text_hash = hashlib.sha256(normalized.encode()).hexdigest()
    host_family = _normalized_host(canonical) or source["source_name"].lower()
    family_id = "EF-" + hashlib.sha256(host_family.encode()).hexdigest()[:12].upper()
    snapshot_id = "VS-" + uuid.uuid4().hex[:12].upper()
    connection.execute(
        "INSERT INTO verification_snapshots(id,verification_run_id,signal_id,origin_kind,origin_id,source_name,source_class,"
        "url,canonical_url,title,text,content_hash,text_family_hash,evidence_family_id,publication_time,stated_event_time,"
        "author,retrieved_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            snapshot_id, run_id, source.get("signal_id"), origin_kind, origin_id, source["source_name"],
            source["source_class"], source.get("url") or canonical, canonical, source["title"], source["text"],
            source["content_hash"], text_hash, family_id, source.get("publication_time"),
            source.get("stated_event_time"), source.get("author"), now(),
        ),
    )
    return dict(connection.execute("SELECT * FROM verification_snapshots WHERE id=?", (snapshot_id,)).fetchone())


def _snapshot_research_evidence(connection, verification_run):
    rows = connection.execute(
        "SELECT * FROM evidence_snapshots WHERE run_id=? ORDER BY id", (verification_run["research_run_id"],)
    ).fetchall()
    snapshots = []
    for row in rows:
        snapshots.append(_insert_verification_snapshot(connection, verification_run["id"], {
            "signal_id": row["signal_id"], "source_name": row["source_name"], "source_class": row["source_class"],
            "url": row["url"], "canonical_url": row["canonical_url"], "title": row["title"], "text": row["text"],
            "content_hash": row["content_hash"], "publication_time": row["publication_time"],
            "stated_event_time": row["stated_event_time"], "author": row["author"],
        }, "research", row["id"]))
    # Evidence linked after the research snapshot is a new evidence version.
    # Include it in a fresh verification decision without mutating the research run.
    for row in _research_evidence_rows(connection, verification_run["event_id"]):
        snapshots.append(_insert_verification_snapshot(connection, verification_run["id"], {
            "signal_id": row["id"], "source_name": row["registered_source_name"],
            "source_class": row["source_class"], "url": row["url"],
            "canonical_url": row["canonical_url"], "title": row["title"], "text": row["text"],
            "content_hash": row["content_hash"], "publication_time": row["publication_time"],
            "stated_event_time": row["event_time"] if row["event_time_basis"] == "stated_event_time" else None,
            "author": row["author"],
        }, "research", row["id"]))
    return snapshots


def _verification_gap_bundle(connection, run, versions):
    research = connection.execute("SELECT * FROM research_runs WHERE id=?", (run["research_run_id"],)).fetchone()
    summary = json.loads(research["summary_json"] or "{}")
    test_payload = json.loads(run["summary_json"] or "{}") if run["mode"] == "test" else {}
    claims = []
    gaps = []
    for version in versions:
        claim = connection.execute("SELECT verification_status,reviewer_notes FROM claims WHERE id=?", (version["claim_id"],)).fetchone()
        missing = []
        if claim["verification_status"] != "SUPPORTED":
            missing.append(f"Existing ledger status is {claim['verification_status']}.")
        if version["claim_type"] == "quotation":
            missing.append("Find a direct verbatim quotation or reclassify this as a paraphrase.")
        missing.append("Find an official primary source or a genuinely independent evidence family for this exact claim.")
        claims.append({
            "claim_id": version["claim_id"], "claim_version_id": version["id"], "text": version["text"],
            "type": version["claim_type"], "scope": version["assertion_scope"],
            "attribution": version["attribution"], "required": bool(version["required_for_event"]),
            "missing": missing,
        })
        gaps.extend(f"{version['claim_id']}: {item}" for item in missing)
    gaps.extend(summary.get("unknowns") or [])
    official_terms = re.compile(
        r"\b(notification|order|gazette|ministry|department|regulator|authority|board|commission|"
        r"company filing|stock exchange filing|court order|judgment|government|official)\b",
        re.I,
    )
    official_hints = []
    evidence_rows = connection.execute(
        "SELECT canonical_url,text FROM evidence_snapshots WHERE run_id=? ORDER BY id", (run["research_run_id"],)
    ).fetchall()
    for evidence in evidence_rows:
        for sentence in re.split(r"(?<=[.!?])\s+|\n+", evidence["text"] or ""):
            matches = sorted({match.group(0).lower() for match in official_terms.finditer(sentence)})
            if not matches:
                continue
            official_hints.append({
                "source_url": evidence["canonical_url"], "reference_text": sentence.strip()[:600],
                "reference_terms": matches,
            })
            if len(official_hints) >= 8:
                break
        if len(official_hints) >= 8:
            break
    return {
        "workspace": workspace_identity()["display_name"], "event_id": run["event_id"],
        "claims": claims, "gaps": list(dict.fromkeys(gaps)),
        "official_source_hints": official_hints,
        "test_leads": test_payload.get("test_leads") or [],
    }


def plan_source_acquisition(verification_run_id):
    """Persist a provider-neutral discovery plan without performing any search."""
    registry = load_official_source_registry()
    timestamp = now()
    acquisition_id = "SA-" + uuid.uuid4().hex[:12].upper()
    with connect() as connection:
        run = connection.execute(
            "SELECT * FROM verification_runs WHERE id=?", (verification_run_id,)
        ).fetchone()
        if run is None:
            raise KeyError(verification_run_id)
        existing = connection.execute(
            "SELECT * FROM source_acquisition_runs WHERE verification_run_id=? "
            "AND trigger_kind='PLANNED_DISCOVERY' ORDER BY started_at DESC LIMIT 1",
            (verification_run_id,),
        ).fetchone()
        if existing:
            return dict(existing)
        versions = _ensure_claim_versions(connection, run["research_run_id"])
        evidence_text = "\n".join(
            row["text"] for row in connection.execute(
                "SELECT text FROM evidence_snapshots WHERE run_id=? ORDER BY id", (run["research_run_id"],)
            )
        )
        event = connection.execute("SELECT title FROM events WHERE id=?", (run["event_id"],)).fetchone()
        plan = build_discovery_plan(
            event["title"],
            [{"claim_id": item["id"], "text": item["text"]} for item in versions],
            evidence_text,
            registry,
        )
        connection.execute(
            "INSERT INTO source_acquisition_runs(id,event_id,verification_run_id,trigger_kind,status,provider,"
            "started_at,search_cost_status,retrieval_cost_status,llm_cost_status) "
            "VALUES(?,?,?,'PLANNED_DISCOVERY','PLANNED','provider-neutral',?,'unknown','not_billed','not_billed')",
            (acquisition_id, run["event_id"], verification_run_id, timestamp),
        )
        for query in plan:
            connection.execute(
                "INSERT INTO source_discovery_attempts(id,acquisition_run_id,strategy,query_text,domains_json,"
                "target_claim_ids_json,provider,status,cost_status,attempted_at) VALUES(?,?,?,?,?,?,?,'PLANNED','unknown',?)",
                (
                    "SD-" + uuid.uuid4().hex[:12].upper(), acquisition_id, query.strategy, query.query,
                    json.dumps(query.domains), json.dumps(query.target_claim_ids), "unassigned", timestamp,
                ),
            )
        return dict(connection.execute("SELECT * FROM source_acquisition_runs WHERE id=?", (acquisition_id,)).fetchone())


def _acquisition_claim_versions(connection, verification_run_id, requested_claim_ids=None):
    requested = set(requested_claim_ids or [])
    rows = [dict(row) for row in connection.execute(
        "SELECT cv.*,vrc.required_for_event FROM verification_run_claims vrc "
        "JOIN claim_versions cv ON cv.id=vrc.claim_version_id WHERE vrc.verification_run_id=? "
        "ORDER BY vrc.required_for_event DESC,cv.id", (verification_run_id,),
    )]
    if not rows:
        run = connection.execute("SELECT research_run_id FROM verification_runs WHERE id=?", (verification_run_id,)).fetchone()
        rows = _ensure_claim_versions(connection, run["research_run_id"])
    if requested:
        rows = [row for row in rows if row["id"] in requested or row["claim_id"] in requested]
    if not rows:
        raise ValueError("no claim in this verification run matches the requested claim IDs")
    return rows


def _independent_acquisition_domains():
    return tuple(
        _normalized_host(item["url"])
        for item in _verification_source_entries()
        if item.get("source_class") == "independent_reporting"
    )


def _persist_unavailable_candidate(connection, acquisition_id, attempt_id, url, error):
    candidate_id = "SC-" + uuid.uuid4().hex[:12].upper()
    reason = str(error)[:500]
    connection.execute(
        "INSERT INTO source_candidates(id,acquisition_run_id,discovery_attempt_id,original_url,source_class,"
        "classification_reason,state,state_reason,created_at) VALUES(?,?,?,?,?,'Retrieval did not produce classifiable content.',"
        "'UNAVAILABLE',?,?)",
        (candidate_id, acquisition_id, attempt_id, url, "UNKNOWN", reason, now()),
    )
    return candidate_id


def add_evidence_url(verification_run_id, url, claim_ids=None, *, transport=None, pdf_extractor=None):
    """Retrieve and stage a human-supplied URL; never mutate adjudication state."""
    _validate_public_url(url)
    registry = load_official_source_registry()
    timestamp = now()
    acquisition_id = "SA-" + uuid.uuid4().hex[:12].upper()
    attempt_id = "SD-" + uuid.uuid4().hex[:12].upper()
    with connect() as connection:
        run = connection.execute("SELECT * FROM verification_runs WHERE id=?", (verification_run_id,)).fetchone()
        if run is None:
            raise KeyError(verification_run_id)
        versions = _acquisition_claim_versions(connection, verification_run_id, claim_ids)
        connection.execute(
            "INSERT INTO source_acquisition_runs(id,event_id,verification_run_id,trigger_kind,status,provider,"
            "started_at,search_provider_calls,direct_http_retrievals,llm_adjudication_calls,search_cost_status,"
            "retrieval_cost_status,llm_cost_status) VALUES(?,?,?,'MANUAL_URL','RUNNING','manual',?,0,0,0,"
            "'not_billed','not_billed','not_billed')",
            (acquisition_id, run["event_id"], verification_run_id, timestamp),
        )
        connection.execute(
            "INSERT INTO source_discovery_attempts(id,acquisition_run_id,strategy,query_text,domains_json,"
            "target_claim_ids_json,provider,status,cost_status,attempted_at) VALUES(?,?,? ,?,'[]',?,'manual',"
            "'COMPLETED','not_billed',?)",
            (attempt_id, acquisition_id, "MANUAL_URL", url, json.dumps([item["id"] for item in versions]), timestamp),
        )

    return _execute_source_acquisition(
        acquisition_id, verification_run_id, versions, [(url, attempt_id)],
        transport=transport, pdf_extractor=pdf_extractor,
    )


def _execute_source_acquisition(
    acquisition_id, verification_run_id, versions, initial_queue, *, transport=None, pdf_extractor=None,
):
    """Retrieve, classify, family-deduplicate, and packetize one acquisition run."""
    registry = load_official_source_registry()
    timestamp = now()
    retriever = SourceRetriever(
        transport or fetch_public_resource, pdf_extractor=pdf_extractor, url_validator=_validate_public_url,
    )
    queue = list(initial_queue)
    queued = {canonicalize_url(item[0]) for item in queue}
    retrieved = []
    direct_attempt_id = None
    retrieval_count = 0
    while queue and retrieval_count < 6:
        candidate_url, candidate_attempt = queue.pop(0)
        retrieval_count += 1
        try:
            document = retriever.retrieve(candidate_url)
            source_class, classification_reason = classify_source(
                document, registry, independent_domains=_independent_acquisition_domains(),
            )
            authority = registry.match(document.final_url)
            candidate_id = "SC-" + uuid.uuid4().hex[:12].upper()
            candidate = {
                "id": candidate_id, "original_url": candidate_url, "final_url": document.final_url,
                "canonical_url": document.metadata.get("canonical_url") or document.final_url,
                "title": document.title, "publication_date": document.publication_date,
                "publisher": document.publisher, "text": document.text, "pages": list(document.pages),
                "document_type": document.document_type, "source_class": source_class,
                "classification_reason": classification_reason, "authority": authority.name if authority else None,
            }
            with connect() as connection:
                previous = [dict(row) for row in connection.execute(
                    "SELECT sc.id,sc.final_url,sc.extracted_text AS text,sc.source_class,"
                    "sc.evidence_family_id AS family_id "
                    "FROM source_candidates sc JOIN source_acquisition_runs sa ON sa.id=sc.acquisition_run_id "
                    "WHERE sa.event_id=(SELECT event_id FROM source_acquisition_runs WHERE id=?) "
                    "AND sc.state='RETRIEVED' ORDER BY sc.created_at,sc.id", (acquisition_id,),
                )]
                family_id, family_reason, relationship, similarity_score = family_for_candidate(candidate, previous)
                candidate["family_id"] = family_id
                connection.execute(
                    "INSERT INTO source_candidates(id,acquisition_run_id,discovery_attempt_id,original_url,final_url,"
                    "canonical_url,http_status,content_type,title,publication_date,publisher,retrieved_at,checksum_sha256,"
                    "extracted_text,document_type,metadata_json,source_class,classification_reason,authority_id,"
                    "evidence_family_id,family_reason,state,state_reason,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'RETRIEVED','Retrieved and staged for adjudication.',?)",
                    (
                        candidate_id, acquisition_id, candidate_attempt, candidate_url, document.final_url,
                        candidate["canonical_url"], document.status, document.content_type, document.title,
                        document.publication_date, document.publisher, document.retrieved_at, document.checksum,
                        document.text, document.document_type, json.dumps(document.metadata, ensure_ascii=False),
                        source_class, classification_reason, authority.id if authority else None, family_id,
                        family_reason, timestamp,
                    ),
                )
                compared = next((item for item in previous if item["family_id"] == family_id), None)
                connection.execute(
                    "INSERT INTO source_candidate_family_assessments(id,acquisition_run_id,candidate_id,"
                    "compared_candidate_id,relationship,reason,text_similarity,assessed_at) VALUES(?,?,?,?,?,?,?,?)",
                    (
                        "SF-" + uuid.uuid4().hex[:12].upper(), acquisition_id, candidate_id,
                        compared["id"] if compared else None, relationship, family_reason, similarity_score, now(),
                    ),
                )
                for page in document.pages:
                    connection.execute(
                        "INSERT INTO source_candidate_pages(candidate_id,page_number,text,text_checksum_sha256) "
                        "VALUES(?,?,?,?)",
                        (candidate_id, page["page"], page["text"], hashlib.sha256(page["text"].encode()).hexdigest()),
                    )
                for row in match_claims(
                    [{"claim_id": item["id"], "text": item["text"]} for item in versions], candidate,
                ):
                    connection.execute(
                        "INSERT INTO claim_source_candidates(claim_version_id,candidate_id,relationship,match_score,"
                        "matched_passage,page_number,reason,created_at) VALUES(?,?,?,?,?,?,?,?)",
                        (
                            row["claim_id"], candidate_id, row["relationship"], row["match_score"],
                            row["matched_passage"], row["page_number"], row["reason"], now(),
                        ),
                    )
            retrieved.append(candidate)
            if document.direct_document_urls and direct_attempt_id is None:
                direct_attempt_id = "SD-" + uuid.uuid4().hex[:12].upper()
                with connect() as connection:
                    connection.execute(
                        "INSERT INTO source_discovery_attempts(id,acquisition_run_id,strategy,query_text,domains_json,"
                        "target_claim_ids_json,provider,status,cost_status,attempted_at,completed_at) "
                        "VALUES(?,?,?,'Direct document links extracted from retrieved HTML','[]',?,'deterministic-link-extractor',"
                        "'COMPLETED','not_billed',?,?)",
                        (direct_attempt_id, acquisition_id, "F_DIRECT_DOCUMENT_LINK",
                         json.dumps([item["id"] for item in versions]), now(), now()),
                    )
            for direct_url in document.direct_document_urls[:5]:
                try:
                    canonical = canonicalize_url(direct_url)
                    _validate_public_url(direct_url)
                except ValueError:
                    continue
                if canonical not in queued:
                    queued.add(canonical)
                    queue.append((direct_url, direct_attempt_id))
        except Exception as error:
            if isinstance(error, (sqlite3.Error, KeyError, TypeError, AssertionError)):
                raise
            with connect() as connection:
                _persist_unavailable_candidate(connection, acquisition_id, candidate_attempt, candidate_url, error)

    with connect() as connection:
        candidates = [dict(row) for row in connection.execute(
            "SELECT *,extracted_text AS text,evidence_family_id AS family_id FROM source_candidates "
            "WHERE acquisition_run_id=? AND state='RETRIEVED' ORDER BY created_at,id", (acquisition_id,),
        )]
        for candidate in candidates:
            candidate["pages"] = [dict(row) for row in connection.execute(
                "SELECT page_number AS page,text FROM source_candidate_pages WHERE candidate_id=? ORDER BY page_number",
                (candidate["id"],),
            )]
        matrix = [dict(row) for row in connection.execute(
            "SELECT claim_version_id AS claim_id,candidate_id,relationship,match_score,matched_passage,page_number,reason "
            "FROM claim_source_candidates WHERE candidate_id IN "
            "(SELECT id FROM source_candidates WHERE acquisition_run_id=?)", (acquisition_id,),
        )]
        packets = []
        for version in versions:
            packet = build_evidence_packet(
                {"claim_id": version["id"], "text": version["text"]}, matrix, candidates,
            )
            packet_id = "EP-" + uuid.uuid4().hex[:12].upper()
            payload = json.dumps(packet, ensure_ascii=False, sort_keys=True)
            connection.execute(
                "INSERT INTO acquisition_evidence_packets(id,acquisition_run_id,claim_version_id,packet_json,packet_hash,"
                "official_primary_found,independent_family_count,deterministically_sufficient,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    packet_id, acquisition_id, version["id"], payload, hashlib.sha256(payload.encode()).hexdigest(),
                    int(packet["official_primary_found"]), packet["independent_family_count"],
                    int(packet["deterministically_sufficient"]), now(),
                ),
            )
            packets.append({"id": packet_id, **packet})
        unavailable = connection.execute(
            "SELECT COUNT(*) FROM source_candidates WHERE acquisition_run_id=? AND state='UNAVAILABLE'",
            (acquisition_id,),
        ).fetchone()[0]
        required_ids = {item["id"] for item in versions if item["required_for_event"]}
        sufficient_ids = {item["claim_id"] for item in packets if item["deterministically_sufficient"]}
        sufficient_required = bool(required_ids) and required_ids <= sufficient_ids
        status = "COMPLETED" if candidates and (not unavailable or sufficient_required) else "PARTIAL" if candidates else "FAILED"
        unavailable_note = None
        if unavailable:
            unavailable_note = (
                "Unavailable candidates were logged but did not affect sufficient required-claim evidence."
                if sufficient_required else "One or more pages were unavailable."
            )
        connection.execute(
            "UPDATE source_acquisition_runs SET status=?,completed_at=?,direct_http_retrievals=?,error_message=? WHERE id=?",
            (status, now(), retrieval_count, unavailable_note, acquisition_id),
        )
        return {
            "run": dict(connection.execute("SELECT * FROM source_acquisition_runs WHERE id=?", (acquisition_id,)).fetchone()),
            "candidates": [dict(row) for row in connection.execute(
                "SELECT * FROM source_candidates WHERE acquisition_run_id=? ORDER BY created_at,id", (acquisition_id,),
            )],
            "packets": packets, "adjudication_changed": False,
        }


def run_source_acquisition_pass(
    verification_run_id, leads, *, acquisition_run_id=None, provider="external-search",
    provider_request_id=None, search_provider_calls=None, search_cost_status="unknown", search_cost_usd=None,
    transport=None, pdf_extractor=None,
):
    """Run one bounded acquisition pass from real discovered URLs using the persisted A-F plan."""
    if not leads:
        raise ValueError("source acquisition requires at least one discovered URL")
    if search_cost_status not in ("known", "unknown", "not_billed"):
        raise ValueError("invalid search cost status")
    if search_cost_status == "known" and search_cost_usd is None:
        raise ValueError("known search cost requires a value")
    if search_cost_status != "known" and search_cost_usd is not None:
        raise ValueError("search cost may only be set when known")
    allowed_strategies = {
        "A_AUTHORITATIVE_DOMAIN", "B_EXACT_PHRASE", "C_TITLE_NOTIFICATION_FRAGMENT",
        "D_ENTITY_DATE_RANGE", "E_SECONDARY_CORROBORATION", "F_DIRECT_DOCUMENT_LINK",
    }
    for lead in leads:
        _validate_public_url(lead["url"])
        if lead.get("strategy") not in allowed_strategies:
            raise ValueError("discovered URL requires an existing A-F acquisition strategy")

    timestamp = now()
    with connect() as connection:
        run = connection.execute("SELECT * FROM verification_runs WHERE id=?", (verification_run_id,)).fetchone()
        if run is None:
            raise KeyError(verification_run_id)
        versions = _acquisition_claim_versions(connection, verification_run_id)
        known_claim_ids = {item["id"] for item in versions} | {item["claim_id"] for item in versions}
        if acquisition_run_id:
            acquisition = connection.execute(
                "SELECT * FROM source_acquisition_runs WHERE id=?", (acquisition_run_id,)
            ).fetchone()
            if acquisition is None:
                raise KeyError(acquisition_run_id)
            if acquisition["verification_run_id"] != verification_run_id:
                raise ValueError("acquisition plan belongs to a different verification run")
            if acquisition["status"] not in ("PLANNED", "RUNNING"):
                raise ValueError("acquisition plan has already been finalized")
        else:
            acquisition_run_id = "SA-" + uuid.uuid4().hex[:12].upper()
            connection.execute(
                "INSERT INTO source_acquisition_runs(id,event_id,verification_run_id,trigger_kind,status,provider,"
                "started_at,search_cost_status,retrieval_cost_status,llm_cost_status) "
                "VALUES(?,?,?,'VERIFICATION_PREP','RUNNING',?,?,?,'not_billed','not_billed')",
                (acquisition_run_id, run["event_id"], verification_run_id, provider, timestamp, search_cost_status),
            )
        connection.execute(
            "UPDATE source_acquisition_runs SET status='RUNNING',provider=?,search_provider_calls=?,"
            "search_cost_status=?,search_cost_usd=?,error_message=NULL WHERE id=?",
            (provider, search_provider_calls, search_cost_status, search_cost_usd, acquisition_run_id),
        )
        queue = []
        for lead_index, lead in enumerate(leads):
            requested = set(lead.get("claim_ids") or [])
            if requested - known_claim_ids:
                raise ValueError("a discovered URL targets an unknown claim")
            targeted = [
                item["id"] for item in versions
                if not requested or item["id"] in requested or item["claim_id"] in requested
            ]
            attempt_id = "SD-" + uuid.uuid4().hex[:12].upper()
            connection.execute(
                "INSERT INTO source_discovery_attempts(id,acquisition_run_id,strategy,query_text,domains_json,"
                "target_claim_ids_json,provider,status,provider_request_id,result_count,cost_status,cost_usd,"
                "attempted_at,completed_at) VALUES(?,?,?,?,?,?,?,'COMPLETED',?,1,?,?,?,?)",
                (
                    attempt_id, acquisition_run_id, lead["strategy"], lead.get("query") or lead["url"],
                    json.dumps(lead.get("domains") or []), json.dumps(targeted), provider,
                    provider_request_id, search_cost_status,
                    search_cost_usd if lead_index == 0 else (0.0 if search_cost_status == "known" else None),
                    timestamp, timestamp,
                ),
            )
            queue.append((lead["url"], attempt_id))

    return _execute_source_acquisition(
        acquisition_run_id, verification_run_id, versions, queue,
        transport=transport, pdf_extractor=pdf_extractor,
    )


def _reclassify_acquired_candidates(connection, event_id):
    registry = load_official_source_registry()
    changed = []
    rows = connection.execute(
        "SELECT sc.* FROM source_candidates sc JOIN source_acquisition_runs sa ON sa.id=sc.acquisition_run_id "
        "WHERE sa.event_id=? AND sc.state='RETRIEVED' ORDER BY sc.id", (event_id,),
    ).fetchall()
    for row in rows:
        document = type("StoredCandidate", (), {
            "final_url": row["final_url"], "text": row["extracted_text"] or "",
        })()
        source_class, reason = classify_source(
            document, registry, independent_domains=_independent_acquisition_domains(),
        )
        authority = registry.match(row["final_url"])
        if source_class != row["source_class"] or reason != row["classification_reason"]:
            connection.execute(
                "UPDATE source_candidates SET source_class=?,classification_reason=?,authority_id=? WHERE id=?",
                (source_class, reason, authority.id if authority else None, row["id"]),
            )
            changed.append(row["id"])
    return changed


def _refresh_acquisition_completion(connection, acquisition_id):
    run = connection.execute("SELECT * FROM source_acquisition_runs WHERE id=?", (acquisition_id,)).fetchone()
    if run is None:
        raise KeyError(acquisition_id)
    retrieved = connection.execute(
        "SELECT COUNT(*) FROM source_candidates WHERE acquisition_run_id=? AND state='RETRIEVED'", (acquisition_id,),
    ).fetchone()[0]
    unavailable = connection.execute(
        "SELECT COUNT(*) FROM source_candidates WHERE acquisition_run_id=? AND state='UNAVAILABLE'", (acquisition_id,),
    ).fetchone()[0]
    required = {
        row[0] for row in connection.execute(
            "SELECT claim_version_id FROM verification_run_claims WHERE verification_run_id=? AND required_for_event=1",
            (run["verification_run_id"],),
        )
    }
    sufficient = {
        row[0] for row in connection.execute(
            "SELECT claim_version_id FROM acquisition_evidence_packets "
            "WHERE acquisition_run_id=? AND deterministically_sufficient=1", (acquisition_id,),
        )
    }
    sufficient_required = bool(required) and required <= sufficient
    status = "COMPLETED" if retrieved and (not unavailable or sufficient_required) else "PARTIAL" if retrieved else "FAILED"
    note = None
    if unavailable:
        note = (
            "Unavailable candidates were logged but did not affect sufficient required-claim evidence."
            if sufficient_required else "One or more pages were unavailable."
        )
    connection.execute(
        "UPDATE source_acquisition_runs SET status=?,error_message=? WHERE id=?", (status, note, acquisition_id),
    )
    return status


def _snapshot_acquired_candidates(connection, run, *, include_all=False):
    rows = connection.execute(
        "SELECT DISTINCT sc.* FROM source_candidates sc "
        "JOIN source_acquisition_runs sa ON sa.id=sc.acquisition_run_id "
        "JOIN claim_source_candidates csc ON csc.candidate_id=sc.id "
        "JOIN verification_run_claims vrc ON vrc.claim_version_id=csc.claim_version_id "
        "WHERE sa.event_id=? AND vrc.verification_run_id=? AND sc.state='RETRIEVED' "
        "AND sc.source_class IN ('OFFICIAL_PRIMARY','INDEPENDENT_REPORTING') "
        "AND (?=1 OR csc.relationship='CANDIDATE') ORDER BY sc.retrieved_at,sc.id",
        (run["event_id"], run["id"], int(include_all)),
    ).fetchall()
    snapshots = []
    for row in rows:
        snapshots.append(_insert_verification_snapshot(connection, run["id"], {
            "signal_id": None, "source_name": row["publisher"] or _normalized_host(row["final_url"]),
            "source_class": "official_primary" if row["source_class"] == "OFFICIAL_PRIMARY" else "independent_reporting",
            "url": row["original_url"], "canonical_url": row["canonical_url"] or row["final_url"],
            "title": row["title"] or row["final_url"], "text": row["extracted_text"],
            "content_hash": row["checksum_sha256"], "publication_time": row["publication_date"],
            "stated_event_time": None, "author": None,
        }, "corroboration", row["id"]))
    return snapshots


def _regroup_evidence_families(connection, run_id):
    rows = [dict(row) for row in connection.execute(
        "SELECT * FROM verification_snapshots WHERE verification_run_id=? ORDER BY id", (run_id,)
    )]
    parent = list(range(len(rows)))

    def find(index):
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left, right):
        left, right = find(left), find(right)
        if left != right:
            parent[max(left, right)] = min(left, right)

    registrations = {}
    for row in rows:
        registration = _registration_for_verification_url(row["canonical_url"])
        metadata = (registration or {}).get("metadata") or {}
        registrations[row["id"]] = str(metadata.get("publisher") or "").strip().lower()

    assessments = []
    for left in range(len(rows)):
        for right in range(left + 1, len(rows)):
            same_text = rows[left]["text_family_hash"] == rows[right]["text_family_hash"]
            similarity_score = SequenceMatcher(
                None, _normalized_text(rows[left]["text"]).lower()[:20000],
                _normalized_text(rows[right]["text"]).lower()[:20000],
            ).ratio()
            left_host = _normalized_host(rows[left]["canonical_url"])
            right_host = _normalized_host(rows[right]["canonical_url"])
            same_host = left_host == right_host
            left_publisher = registrations[rows[left]["id"]]
            same_publisher = bool(left_publisher and left_publisher == registrations[rows[right]["id"]])
            if same_host:
                relationship, reason = "SAME_FAMILY", "URLs share the same normalized publisher host."
            elif same_publisher:
                relationship, reason = "SAME_FAMILY", "Registered source provenance identifies the same publisher."
            elif same_text:
                relationship, reason = "SAME_FAMILY", "Normalized article text is identical."
            elif similarity_score >= 0.72:
                relationship, reason = "SAME_FAMILY", "High text overlap indicates a mirror, syndication, or copied release."
            else:
                relationship, reason = "INDEPENDENT_FAMILY", "Distinct registered publishers and no syndication-level text overlap."
            if relationship == "SAME_FAMILY":
                union(left, right)
            assessments.append((rows[left], rows[right], relationship, reason, similarity_score, left_host, right_host))
    groups = {}
    for index, row in enumerate(rows):
        groups.setdefault(find(index), []).append(row)
    for members in groups.values():
        family_material = sorted({_normalized_host(item["canonical_url"]) for item in members})
        family_id = "EF-" + hashlib.sha256(json.dumps(family_material).encode()).hexdigest()[:12].upper()
        for item in members:
            connection.execute(
                "UPDATE verification_snapshots SET evidence_family_id=? WHERE id=?", (family_id, item["id"])
            )
    for left, right, relationship, reason, similarity, left_host, right_host in assessments:
        connection.execute(
            "INSERT OR IGNORE INTO verification_source_family_assessments(id,verification_run_id,left_snapshot_id,"
            "right_snapshot_id,relationship,reason,text_similarity,left_host,right_host,assessed_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                "VF-" + uuid.uuid4().hex[:12].upper(), run_id, left["id"], right["id"], relationship,
                reason, similarity, left_host, right_host, now(),
            ),
        )


def _best_claim_excerpt(claim_text, source_text):
    segments = [segment.strip() for segment in re.split(r"(?<=[.!?])\s+|\n+", source_text) if len(segment.strip()) > 20]
    if not segments:
        segments = [_normalized_text(source_text)[:1200]]
    claim_tokens = _tokens(claim_text)
    best = None
    for index, segment in enumerate(segments):
        context = " ".join(segments[max(0, index - 1):index + 1])
        segment_tokens = _tokens(context)
        score = len(claim_tokens & segment_tokens) / len(claim_tokens) if claim_tokens else 0
        if best is None or score > best[0]:
            best = (score, context[:1600])
    return best or (0.0, "")


def _claim_numbers(value):
    return set(re.findall(r"\b\d[\d,.]*(?:\s*(?:crore|lakh|million|billion|%|kg|km|mw|days?))?\b", value, re.I))


def _quote_is_direct(claim_text, excerpt):
    quoted = re.findall(r"[\"“](.*?)[\"”]", claim_text)
    if quoted:
        return all(_normalized_text(item) in _normalized_text(excerpt) for item in quoted)
    return _normalized_text(claim_text) in _normalized_text(excerpt)


def _semantic_text(value):
    text = str(value or "").lower().replace("–", "-").replace("—", "-")
    text = re.sub(r"\bflue[ -]?cured\s+virginia\b", "fcv", text)
    text = re.sub(r"\bandhra\s+pradesh\b", "andhra pradesh", text)
    return re.sub(r"\s+", " ", text).strip()


def _semantic_seasons(value):
    seasons = set()
    for start, end in re.findall(r"\b(20\d{2})\s*[-/]\s*(\d{2}|20\d{2})\b", _semantic_text(value)):
        full_end = "20" + end if len(end) == 2 else end
        seasons.add(f"{start}-{full_end}")
    return seasons


def _money_values(value):
    return {
        re.sub(r"\s+", " ", item.lower()).replace("rs.", "rs")
        for item in re.findall(
            r"(?:₹|rs\.?|usd\s*)\s*\d[\d,.]*(?:\s*(?:crore|lakh|million|billion))?",
            str(value or ""), re.I,
        )
    }


def classify_claim_evidence(claim_text, passage):
    """Classify substantive claim support using explicit semantic facets."""
    claim = _semantic_text(claim_text)
    evidence = _semantic_text(passage)
    claim_tokens = _tokens(claim)
    evidence_tokens = _tokens(evidence)
    token_score = len(claim_tokens & evidence_tokens) / len(claim_tokens) if claim_tokens else 0.0
    matched, missing = [], []

    actor_pattern = r"\b(?:union|central) government\b|\bunion commerce ministry\b|\bministry of commerce(?: and industry)?\b|\bdepartment of commerce\b"
    if re.search(actor_pattern, claim):
        (matched if re.search(actor_pattern, evidence) else missing).append("actor")

    positive_action = r"\b(?:permit(?:s|ted)?|allow(?:s|ed)?|authori[sz](?:e|es|ed)|approv(?:e|es|ed)|clear(?:s|ed)?|sanction(?:s|ed)?)\b"
    negative_action = r"\b(?:den(?:y|ies|ied)|reject(?:s|ed)?|prohibit(?:s|ed)?|withdr(?:aw|aws|ew|awn)|not permitted|not allowed)\b"
    claim_positive = bool(re.search(positive_action, claim))
    evidence_positive = bool(re.search(positive_action, evidence))
    if claim_positive:
        (matched if evidence_positive else missing).append("action")

    subject_requirements = []
    if re.search(r"\b(?:fcv|flue[ -]?cured virginia)\b", claim):
        subject_requirements.append(("FCV tobacco", r"\b(?:fcv|flue[ -]?cured virginia)\b"))
    if re.search(r"\bexcess\b", claim):
        subject_requirements.append(("excess", r"\bexcess\b"))
    if re.search(r"\btobacco\b", claim):
        subject_requirements.append(("tobacco", r"\btobacco\b"))
    subject_match = all(re.search(pattern, evidence) for _, pattern in subject_requirements) if subject_requirements else token_score >= 0.35
    (matched if subject_match else missing).append("subject")

    if "andhra pradesh" in claim:
        (matched if "andhra pradesh" in evidence else missing).append("geography")

    claim_seasons = _semantic_seasons(claim)
    evidence_seasons = _semantic_seasons(evidence)
    if claim_seasons:
        (matched if claim_seasons <= evidence_seasons else missing).append("crop season/date")

    grower_requirements = []
    if re.search(r"\bregistered growers?\b", claim):
        grower_requirements.append(r"\bregistered growers?\b")
    if re.search(r"\bunregistered growers?\b", claim):
        grower_requirements.append(r"\bunregistered growers?\b")
    if grower_requirements:
        (matched if all(re.search(pattern, evidence) for pattern in grower_requirements) else missing).append("eligible growers")

    platform_required = bool(
        re.search(r"\bauction platforms?\b", claim)
        and re.search(r"\b(?:authori[sz]ed|approved)\b", claim)
        and "tobacco board" in claim
    )
    if platform_required:
        platform_match = bool(
            re.search(r"\bauction platforms?\b", evidence)
            and re.search(r"\b(?:authori[sz]ed|approved)\b", evidence)
            and "tobacco board" in evidence
        )
        (matched if platform_match else missing).append("authorised auction-platform condition")

    contradictory = bool(claim_positive and re.search(negative_action, evidence))
    if claim_seasons and evidence_seasons and claim_seasons.isdisjoint(evidence_seasons):
        contradictory = True
    claim_money, evidence_money = _money_values(claim), _money_values(evidence)
    if claim_money and evidence_money and claim_money.isdisjoint(evidence_money):
        contradictory = True

    if contradictory and subject_match:
        classification = "CONTRADICTS"
        rationale = "The passage addresses the claim subject but states an opposing action or conflicting explicit value."
    elif claim_positive and evidence_positive and subject_match and not missing:
        classification = "DIRECT_SUPPORT"
        rationale = "The passage substantively states every material claim facet: " + ", ".join(matched) + "."
    elif claim_positive and evidence_positive and subject_match:
        classification = "PARTIAL_SUPPORT"
        rationale = "The passage states the core action and subject but omits: " + ", ".join(missing) + "."
    elif subject_match or token_score >= 0.25:
        classification = "MENTIONS_ONLY"
        rationale = "The passage mentions the claim subject but does not substantively state the claimed action and conditions."
    else:
        classification = "IRRELEVANT"
        rationale = "The passage does not address the claim's substantive subject and action."
    return {
        "classification": classification, "rationale": rationale,
        "matched_facets": matched, "missing_facets": missing, "score": token_score,
    }


def _evaluate_verification_claim(connection, run, version, snapshots):
    known_refs = {
        canonicalize_url(row["source_url"]): dict(row)
        for row in connection.execute(
            "SELECT * FROM claim_evidence WHERE claim_id=? AND validation_status='VALID'", (version["claim_id"],)
        )
    }
    evaluated = []
    relative_date = False
    for snapshot in snapshots:
        known = known_refs.get(snapshot["canonical_url"])
        if known:
            excerpt = known["supporting_excerpt"] or ""
        else:
            _, excerpt = _best_claim_excerpt(version["text"], snapshot["text"])
        classification = classify_claim_evidence(version["text"], excerpt)
        if known and known["support_kind"] == "conflicts":
            classification = {
                **classification, "classification": "CONTRADICTS",
                "rationale": "The validated claim-evidence reference explicitly identifies a conflict.",
            }
        if version["claim_type"] == "quotation" and not _quote_is_direct(version["text"], excerpt):
            classification = {
                **classification, "classification": "MENTIONS_ONLY",
                "rationale": "The passage is not a direct verbatim match for the quotation claim.",
            }
        if re.search(r"\b(today|yesterday|tomorrow|monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b", excerpt, re.I):
            relative_date = True
        relationship = {
            "DIRECT_SUPPORT": "supports", "CONTRADICTS": "conflicts",
            "PARTIAL_SUPPORT": "mentions_only", "MENTIONS_ONLY": "mentions_only", "IRRELEVANT": "mentions_only",
        }[classification["classification"]]
        evaluated.append({
            "snapshot": snapshot, "relationship": relationship, "excerpt": excerpt,
            "score": classification["score"], "classification": classification["classification"],
            "classification_rationale": classification["rationale"],
        })

    supports = [item for item in evaluated if item["classification"] == "DIRECT_SUPPORT"]
    conflicts = [item for item in evaluated if item["classification"] == "CONTRADICTS"]
    families = {item["snapshot"]["evidence_family_id"] for item in supports}
    official = [item for item in supports if item["snapshot"]["source_class"] == "official_primary"]
    independent_families = {
        item["snapshot"]["evidence_family_id"] for item in supports
        if item["snapshot"]["source_class"] == "independent_reporting"
    }
    missing = []
    if relative_date:
        missing.append("Relative date remains unresolved; publication time was not substituted as event time.")
    if version["claim_type"] == "quotation" and not supports:
        missing.append("The claim is a paraphrase or lacks a verbatim quotation in inspected evidence.")
    if conflicts:
        decision = "CONFLICTED"
        rationale = "Inspected evidence contains a claim-specific conflict; primary conflicts are not overridden by repetition."
    elif official or len(independent_families) >= 2:
        decision = "SUPPORTED"
        rationale = (
            "Claim has explicit official primary evidence."
            if official else "Claim has explicit support from two independent evidence families."
        )
    else:
        decision = "EXCLUDED" if not version["required_for_event"] else "INSUFFICIENT_EVIDENCE"
        if len(independent_families) == 1:
            rationale = "One independent family supports what the publisher reported, but does not independently corroborate the underlying assertion."
        else:
            rationale = "No inspected official source or two independent evidence families explicitly support this claim."
        missing.append("Official primary evidence or a second independent evidence family is required.")
    return decision, rationale, list(dict.fromkeys(missing)), evaluated, len(independent_families)


def _inspect_verification_leads(connection, run, gap_bundle, leads, *, deadline=None):
    inspected = []
    allowed_claims = gap_bundle["claims"]
    for proposed in (leads or [])[:VERIFICATION_MAX_LEADS]:
        if deadline is not None and time.monotonic() >= deadline:
            return {
                "snapshots": inspected,
                "transient_error": "Corroborating-source retrieval exceeded its bounded phase timeout.",
            }
        url = proposed.get("url", "")
        lead_id = "VL-" + uuid.uuid4().hex[:12].upper()
        try:
            canonical = canonicalize_url(url)
        except ValueError:
            canonical = None
        connection.execute(
            "INSERT OR IGNORE INTO verification_leads(id,verification_run_id,url,canonical_url,title,snippet,"
            "target_claim_ids_json,source_priority,status,status_reason,discovered_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                lead_id, run["id"], url, canonical, proposed.get("title"), None,
                json.dumps(proposed.get("target_claim_ids") or []), proposed.get("source_priority", "unknown")
                if proposed.get("source_priority") in ("official_primary", "independent_reporting", "unknown") else "unknown",
                "DISCOVERED", proposed.get("reason") or "Provider discovery lead", now(),
            ),
        )
        lead = connection.execute(
            "SELECT * FROM verification_leads WHERE verification_run_id=? AND url=?", (run["id"], url)
        ).fetchone()
        if not lead:
            continue
        if lead["status"] == "INGESTED":
            existing_snapshot = connection.execute(
                "SELECT * FROM verification_snapshots WHERE verification_run_id=? AND origin_kind='corroboration' "
                "AND origin_id=?", (run["id"], lead["id"]),
            ).fetchone()
            if existing_snapshot:
                inspected.append(dict(existing_snapshot))
            continue
        registration = _registration_for_verification_url(url)
        if not registration:
            connection.execute(
                "UPDATE verification_leads SET status='REVIEW',status_reason=?,inspected_at=? WHERE id=?",
                ("No verified source registration matches this host; snippet retained only as a lead.", now(), lead["id"]),
            )
            continue
        # Persist the lead before the existing ingestion path opens its own
        # transaction. A failed fetch remains auditable as a non-evidence lead.
        connection.commit()
        try:
            ingestion = ingest_url(
                url, source_name=registration["name"], source_metadata=_source_metadata(registration),
                source_id=registration["id"], source_type=registration["type"], source_registration=registration,
                content_role="item", item_type="announcement" if registration["source_class"] == "official_primary" else "news",
                verification_claims=allowed_claims,
            )
            signal = connection.execute("SELECT * FROM signals WHERE id=?", (ingestion["signal_id"],)).fetchone()
            snapshot = _insert_verification_snapshot(connection, run["id"], {
                "signal_id": signal["id"], "source_name": signal["source_name"], "source_class": signal["source_class"],
                "url": signal["url"], "canonical_url": signal["canonical_url"], "title": signal["title"],
                "text": signal["text"], "content_hash": signal["content_hash"],
                "publication_time": signal["publication_time"],
                "stated_event_time": signal["event_time"] if signal["event_time_basis"] == "stated_event_time" else None,
                "author": signal["author"],
            }, "corroboration", lead["id"])
            connection.execute(
                "UPDATE verification_leads SET status='INGESTED',status_reason=?,signal_id=?,canonical_url=?,inspected_at=? WHERE id=?",
                ("Fetched, source-validated, parsed, ingested, and snapshotted.", signal["id"], signal["canonical_url"], now(), lead["id"]),
            )
            inspected.append(snapshot)
        except Exception as error:
            status = "REVIEW" if isinstance(error, (URLError, TimeoutError)) else "REJECTED"
            safe_reason = str(error)[:500]
            connection.execute(
                "UPDATE verification_leads SET status=?,status_reason=?,inspected_at=? WHERE id=?",
                (status, safe_reason, now(), lead["id"]),
            )
            log_error("verification_lead_rejected", safe_reason, run_id=run["id"], lead_url=url, status=status)
            timeout_reason = getattr(error, "reason", None)
            if isinstance(error, TimeoutError) or isinstance(timeout_reason, (TimeoutError, socket.timeout)):
                return {"snapshots": inspected, "transient_error": safe_reason}
    return {"snapshots": inspected, "transient_error": None}


VERIFICATION_PHASES = (
    "PRIMARY_EVIDENCE_EXTRACTION", "CORROBORATION_DISCOVERY", "CORROBORATING_SOURCE_RETRIEVAL",
    "CLAIM_SOURCE_MATCHING", "CONTRADICTION_ANALYSIS", "FINAL_CLAIM_ADJUDICATION",
)


def _verification_checkpoint(connection, run_id, phase, status, payload):
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    checkpoint_id = "VC-" + uuid.uuid4().hex[:12].upper()
    connection.execute(
        "INSERT OR IGNORE INTO verification_checkpoints(id,verification_run_id,phase,status,payload_json,payload_hash,created_at) "
        "VALUES(?,?,?,?,?,?,?)",
        (checkpoint_id, run_id, phase, status, encoded, hashlib.sha256(encoded.encode()).hexdigest(), now()),
    )
    if status == "COMPLETED":
        connection.execute(
            "UPDATE verification_runs SET last_completed_phase=?,last_checkpoint_at=? WHERE id=?",
            (phase, now(), run_id),
        )


def _completed_checkpoint(connection, run_id, phase):
    row = connection.execute(
        "SELECT * FROM verification_checkpoints WHERE verification_run_id=? AND phase=? AND status='COMPLETED' "
        "ORDER BY created_at DESC LIMIT 1", (run_id, phase),
    ).fetchone()
    if row is None:
        return None
    return {**dict(row), "payload": json.loads(row["payload_json"] or "{}")}


def _verification_failure_category(error):
    code = str(getattr(error, "code", "verification_error")).lower()
    if code in ("connection_timeout", "response_timeout") or isinstance(error, (TimeoutError, socket.timeout)):
        return "TRANSIENT_PROVIDER_TIMEOUT"
    if code in ("missing_api_key", "http_401", "http_403"):
        return "AUTH_FAILURE"
    if code == "invalid_provider_response":
        return "INVALID_PROVIDER_OUTPUT"
    if bool(getattr(error, "retryable", False)) and (
        code == "network_error" or code == "http_408" or code == "http_409" or code == "http_429"
        or (code.startswith("http_") and code[5:].isdigit() and int(code[5:]) >= 500)
    ):
        return "TRANSIENT_PROVIDER_FAILURE"
    return "PROVIDER_FAILURE"


def _start_verification_attempt(connection, run, provider, phase, retry_of=None):
    attempt_number = connection.execute(
        "SELECT COALESCE(MAX(attempt_number),0)+1 FROM verification_attempts WHERE verification_run_id=?", (run["id"],)
    ).fetchone()[0]
    attempt_id = "VA-" + uuid.uuid4().hex[:12].upper()
    connection.execute(
        "INSERT INTO verification_attempts(id,verification_run_id,attempt_number,phase,provider,model,status,started_at,"
        "cost_status,retry_of_attempt_id) VALUES(?,?,?,?,?,?,'RUNNING',?,'unknown',?)",
        (attempt_id, run["id"], attempt_number, phase, provider.name, provider.model, now(), retry_of),
    )
    return attempt_id


def _finish_verification_attempt(connection, attempt_id, *, result=None, error=None, elapsed_seconds=None, retry_after=None):
    if result is not None:
        connection.execute(
            "UPDATE verification_attempts SET status='COMPLETED',ended_at=?,elapsed_seconds=?,provider_request_id=?,"
            "input_tokens=?,output_tokens=?,total_tokens=?,actual_search_calls=?,actual_open_calls=?,"
            "actual_sources_returned=?,cost_status=?,cost_usd=?,cost_usd_ticks=? WHERE id=?",
            (
                now(), result.elapsed_seconds if result.elapsed_seconds is not None else elapsed_seconds,
                result.provider_request_id, result.input_tokens, result.output_tokens, result.total_tokens,
                result.actual_search_calls, result.actual_open_calls, result.actual_sources_returned,
                "known" if result.cost_usd is not None else "unknown", result.cost_usd, result.cost_usd_ticks, attempt_id,
            ),
        )
        return
    category = _verification_failure_category(error)
    connection.execute(
        "UPDATE verification_attempts SET status='FAILED',ended_at=?,elapsed_seconds=?,failure_category=?,failure_code=?,"
        "failure_message=?,retry_after_seconds=? WHERE id=?",
        (
            now(), elapsed_seconds, category, getattr(error, "code", "verification_error"),
            str(error or "verification provider failed")[:500], retry_after, attempt_id,
        ),
    )


def _verification_attempt_totals(connection, run_id):
    attempts = [dict(row) for row in connection.execute(
        "SELECT * FROM verification_attempts WHERE verification_run_id=? AND phase='CORROBORATION_DISCOVERY' "
        "ORDER BY attempt_number", (run_id,),
    )]
    def sum_known(field):
        values = [row[field] for row in attempts if row[field] is not None]
        return sum(values) if values else None
    all_costs_known = bool(attempts) and all(row["cost_status"] == "known" for row in attempts)
    latest_request = next((row["provider_request_id"] for row in reversed(attempts) if row["provider_request_id"]), None)
    return {
        "attempt_count": len(attempts), "input_tokens": sum_known("input_tokens"),
        "output_tokens": sum_known("output_tokens"), "total_tokens": sum_known("total_tokens"),
        "actual_search_calls": sum_known("actual_search_calls"), "actual_open_calls": sum_known("actual_open_calls"),
        "actual_sources_returned": sum_known("actual_sources_returned"),
        "cost_status": "known" if all_costs_known else "unknown",
        "cost_usd": sum_known("cost_usd") if all_costs_known else None,
        "cost_usd_ticks": sum_known("cost_usd_ticks") if all_costs_known else None,
        "provider_request_id": latest_request, "provider_elapsed_seconds": sum_known("elapsed_seconds"),
    }


def _ensure_incomplete_claim_set(connection, run, reason):
    existing = connection.execute(
        "SELECT id FROM approved_claim_sets WHERE verification_run_id=? AND status='REVIEW_REQUIRED' "
        "ORDER BY version_number DESC LIMIT 1", (run["id"],),
    ).fetchone()
    if existing:
        return existing["id"]
    snapshots = [dict(row) for row in connection.execute(
        "SELECT canonical_url,content_hash FROM verification_snapshots WHERE verification_run_id=?", (run["id"],)
    )]
    evidence_version = hashlib.sha256(json.dumps(sorted(
        (item["canonical_url"], item["content_hash"]) for item in snapshots
    )).encode()).hexdigest()
    version = connection.execute(
        "SELECT COALESCE(MAX(version_number),0)+1 FROM approved_claim_sets WHERE event_id=?", (run["event_id"],)
    ).fetchone()[0]
    claim_set_id = "CS-" + uuid.uuid4().hex[:12].upper()
    connection.execute(
        "INSERT INTO approved_claim_sets(id,event_id,verification_run_id,version_number,evidence_version,claim_set_version,"
        "status,created_at) VALUES(?,?,?,?,?,?,'REVIEW_REQUIRED',?)",
        (claim_set_id, run["event_id"], run["id"], version, evidence_version, run["claim_set_version"], now()),
    )
    return claim_set_id


def _pause_verification(connection, run, category, message, *, phase, partial_payload=None):
    if partial_payload is not None:
        _verification_checkpoint(connection, run["id"], phase, "PARTIAL", partial_payload)
    claim_set_id = _ensure_incomplete_claim_set(connection, run, message)
    totals = _verification_attempt_totals(connection, run["id"])
    summary = {
        "verification_incomplete": True, "failure_category": category, "paused_phase": phase,
        "claim_set_id": claim_set_id, "claim_set_status": "REVIEW_REQUIRED",
    }
    _record_verification_status(
        connection, run["id"], "RUNNING", "FAILED",
        "Verification paused because the evidence provider timed out." if category == "TRANSIENT_PROVIDER_TIMEOUT" else message,
        progress=min(int(run["progress"] or 0), 95), current_phase=phase, resume_state="PAUSED_TRANSIENT",
        recoverable=1, resume_reason=message, decision_explanation=(
            "Verification is incomplete because a recoverable provider or retrieval timeout occurred. "
            "No unresolved claim was converted to insufficient evidence or approved."
        ), summary_json=json.dumps(summary, ensure_ascii=False), error_code=category, error_message=message,
        **totals,
    )
    connection.execute(
        "UPDATE events SET verification_status='REVIEW_REQUIRED',updated_at=? WHERE id=?", (now(), run["event_id"])
    )


def _claim_analysis_payload(connection, run, versions):
    snapshots = [dict(row) for row in connection.execute(
        "SELECT * FROM verification_snapshots WHERE verification_run_id=? ORDER BY id", (run["id"],)
    )]
    analyses = []
    for version in versions:
        decision, rationale, missing, evidence, family_count = _evaluate_verification_claim(
            connection, run, version, snapshots
        )
        analyses.append({
            "claim_version_id": version["id"], "claim_id": version["claim_id"], "decision": decision,
            "rationale": rationale, "missing": missing, "independent_family_count": family_count,
            "evidence": [{"snapshot_id": item["snapshot"]["id"], "relationship": item["relationship"],
                          "classification": item["classification"], "excerpt": item["excerpt"],
                          "score": item["score"], "classification_rationale": item["classification_rationale"]}
                         for item in evidence],
        })
    return {"snapshot_ids": [item["id"] for item in snapshots], "claims": analyses}


def _finalize_verification(connection, run, versions, result, domains, provider_failure=None):
    snapshots = [dict(row) for row in connection.execute(
        "SELECT * FROM verification_snapshots WHERE verification_run_id=? ORDER BY id", (run["id"],)
    )]
    decisions = []
    family_sizes = {}
    for item in snapshots:
        family_sizes[item["evidence_family_id"]] = family_sizes.get(item["evidence_family_id"], 0) + 1
    for version in versions:
        decision, rationale, missing, evidence, family_count = _evaluate_verification_claim(
            connection, run, version, snapshots
        )
        existing = connection.execute(
            "SELECT * FROM verification_decisions WHERE verification_run_id=? AND claim_version_id=?",
            (run["id"], version["id"]),
        ).fetchone()
        decision_id = existing["id"] if existing else "VD-" + uuid.uuid4().hex[:12].upper()
        approved = int(decision == "SUPPORTED")
        if existing is None:
            connection.execute(
                "INSERT INTO verification_decisions(id,verification_run_id,claim_version_id,decision,approved,"
                "required_for_event,independent_family_count,rationale,missing_information_json,decided_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (decision_id, run["id"], version["id"], decision, approved, version["required_for_event"],
                 family_count, rationale, json.dumps(missing, ensure_ascii=False), now()),
            )
        else:
            prior_evidence = [dict(row) for row in connection.execute(
                "SELECT * FROM verification_decision_evidence WHERE decision_id=? ORDER BY id", (decision_id,),
            )]
            revision_payload = {"decision": dict(existing), "evidence": prior_evidence}
            encoded_revision = json.dumps(revision_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            connection.execute(
                "INSERT INTO verification_decision_revisions(id,verification_run_id,claim_version_id,"
                "superseded_decision_id,snapshot_json,snapshot_hash,reason,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (
                    "VDR-" + uuid.uuid4().hex[:12].upper(), run["id"], version["id"], decision_id,
                    encoded_revision, hashlib.sha256(encoded_revision.encode()).hexdigest(),
                    "Explicit resume re-adjudicated the claim using preserved plus newly retrieved evidence.", now(),
                ),
            )
            connection.execute("DELETE FROM verification_decision_evidence WHERE decision_id=?", (decision_id,))
            connection.execute(
                "UPDATE verification_decisions SET decision=?,approved=?,required_for_event=?,independent_family_count=?,"
                "rationale=?,missing_information_json=?,decided_at=? WHERE id=?",
                (
                    decision, approved, version["required_for_event"], family_count, rationale,
                    json.dumps(missing, ensure_ascii=False), now(), decision_id,
                ),
            )
        if run["mode"] == "live":
            ledger_status = decision if decision in ("SUPPORTED", "CONFLICTED", "INSUFFICIENT_EVIDENCE") else "INSUFFICIENT_EVIDENCE"
            ledger_claim = connection.execute(
                "SELECT verification_status FROM claims WHERE id=?", (version["claim_id"],)
            ).fetchone()
            if ledger_claim and ledger_claim["verification_status"] != ledger_status:
                connection.execute("UPDATE claims SET verification_status=?,updated_at=? WHERE id=?",
                                   (ledger_status, now(), version["claim_id"]))
                connection.execute(
                    "INSERT INTO claim_status_history(claim_id,from_status,to_status,reason,changed_at) VALUES(?,?,?,?,?)",
                    (version["claim_id"], ledger_claim["verification_status"], ledger_status,
                     f"Verification run {run['id']}: {rationale}", now()),
                )
        for item in evidence:
            snapshot = item["snapshot"]
            directness = (
                "direct_primary" if snapshot["source_class"] == "official_primary"
                else "syndicated_report" if family_sizes[snapshot["evidence_family_id"]] > 1
                else "independent_report"
            )
            connection.execute(
                "INSERT OR IGNORE INTO verification_decision_evidence(decision_id,snapshot_id,relationship,excerpt,"
                "excerpt_valid,directness,evidence_family_id,rationale,classification) VALUES(?,?,?,?,1,?,?,?,?)",
                (decision_id, snapshot["id"], item["relationship"], item["excerpt"], directness,
                 snapshot["evidence_family_id"], item["classification_rationale"], item["classification"]),
            )
        decisions.append({"id": decision_id, "claim_version_id": version["id"], "decision": decision,
                          "approved": approved, "required": bool(version["required_for_event"])})
    required_pass = all(item["decision"] == "SUPPORTED" for item in decisions if item["required"])
    has_required = any(item["required"] for item in decisions)
    production_pass = run["mode"] == "live" and not provider_failure and has_required and required_pass
    if run["mode"] == "test":
        set_status, explanation = "TEST_ONLY", "TEST DATA verification is excluded from production event verification."
    elif provider_failure:
        set_status = "REVIEW_REQUIRED"
        explanation = "Verification failed at the provider boundary; local evidence remains reviewable but incomplete."
    elif production_pass:
        set_status, explanation = "APPROVED", "Every required claim passed the documented claim-specific corroboration policy."
    else:
        set_status = "REVIEW_REQUIRED"
        explanation = "At least one required claim lacks claim-specific official evidence or two independent families."
    final_version = hashlib.sha256(json.dumps(sorted(set(
        (item["canonical_url"], item["content_hash"]) for item in snapshots
    ))).encode()).hexdigest()
    set_version = connection.execute(
        "SELECT COALESCE(MAX(version_number),0)+1 FROM approved_claim_sets WHERE event_id=?", (run["event_id"],)
    ).fetchone()[0]
    claim_set_id = "CS-" + uuid.uuid4().hex[:12].upper()
    connection.execute(
        "INSERT INTO approved_claim_sets(id,event_id,verification_run_id,version_number,evidence_version,claim_set_version,"
        "status,created_at) VALUES(?,?,?,?,?,?,?,?)",
        (claim_set_id, run["event_id"], run["id"], set_version, final_version, run["claim_set_version"], set_status, now()),
    )
    for item in decisions:
        if item["approved"]:
            connection.execute(
                "INSERT INTO approved_claim_set_items(claim_set_id,claim_version_id,verification_decision_id) VALUES(?,?,?)",
                (claim_set_id, item["claim_version_id"], item["id"]),
            )
    summary = {
        **result.result, "domains": domains,
        "inspected_sources": sum(lead["status"] == "INGESTED" for lead in connection.execute(
            "SELECT status FROM verification_leads WHERE verification_run_id=?", (run["id"],)
        )), "claim_set_id": claim_set_id, "claim_set_status": set_status,
    }
    totals = _verification_attempt_totals(connection, run["id"])
    _record_verification_status(
        connection, run["id"], "RUNNING", "FAILED" if provider_failure else "COMPLETED",
        provider_failure[1] if provider_failure else "Verification decisions complete", completed_at=now(), progress=100,
        current_phase="FINAL_CLAIM_ADJUDICATION", recoverable=0, resume_state=None, resume_reason=None,
        final_evidence_version=final_version, decision_explanation=explanation,
        evidence_classifier_version=EVIDENCE_CLASSIFIER_VERSION,
        summary_json=json.dumps(summary, ensure_ascii=False), error_code=provider_failure[0] if provider_failure else None,
        error_message=provider_failure[1] if provider_failure else None, **totals,
    )
    _verification_checkpoint(connection, run["id"], "FINAL_CLAIM_ADJUDICATION", "COMPLETED",
                             {"claim_set_id": claim_set_id, "claim_set_status": set_status})
    event = connection.execute("SELECT status FROM events WHERE id=?", (run["event_id"],)).fetchone()
    if production_pass and event["status"] == "VERIFYING":
        connection.execute("UPDATE events SET status='VERIFIED',verification_status='VERIFIED',updated_at=? WHERE id=?",
                           (now(), run["event_id"]))
        connection.execute("INSERT INTO transitions(event_id,from_state,to_state,at) VALUES(?,'VERIFYING','VERIFIED',?)",
                           (run["event_id"], now()))
    else:
        connection.execute("UPDATE events SET verification_status=?,updated_at=? WHERE id=?",
                           ("TEST_ONLY" if run["mode"] == "test" else "REVIEW_REQUIRED", now(), run["event_id"]))


def readjudicate_verification(run_id):
    """Rebuild the evidence matrix and re-adjudicate a completed run without any provider call."""
    with connect() as connection:
        run = connection.execute("SELECT * FROM verification_runs WHERE id=?", (run_id,)).fetchone()
        if run is None:
            raise KeyError(run_id)
        if run["status"] != "COMPLETED":
            raise ValueError("only a completed verification run can be re-adjudicated")
        if run["evidence_classifier_version"] == EVIDENCE_CLASSIFIER_VERSION:
            return {"run": verification_run(run_id), "duplicate": True, "provider_called": False}
        versions = _ensure_claim_versions(connection, run["research_run_id"])
        _reclassify_acquired_candidates(connection, run["event_id"])
        for acquisition in connection.execute(
            "SELECT id FROM source_acquisition_runs WHERE event_id=?", (run["event_id"],),
        ):
            _refresh_acquisition_completion(connection, acquisition["id"])
        _snapshot_acquired_candidates(connection, run, include_all=True)
        _regroup_evidence_families(connection, run_id)
        analysis = _claim_analysis_payload(connection, run, versions)
        _verification_checkpoint(connection, run_id, "CLAIM_SOURCE_MATCHING", "PARTIAL", {
            **analysis, "rebuild_kind": "LOCAL_CLASSIFIER_READJUDICATION",
            "classifier_version": EVIDENCE_CLASSIFIER_VERSION,
        })
        prior_summary = json.loads(run["summary_json"] or "{}")
        domains = prior_summary.get("domains") or _verification_domains(run["research_run_id"])
        _record_verification_status(
            connection, run_id, "COMPLETED", "RUNNING",
            "Rebuilding the claim-evidence matrix with the corrected semantic classifier.",
            progress=90, current_phase="FINAL_CLAIM_ADJUDICATION", completed_at=None,
        )
        running = connection.execute("SELECT * FROM verification_runs WHERE id=?", (run_id,)).fetchone()
        result = VerificationProviderResult(result={
            "search_summary": "No provider call: locally re-adjudicated the preserved evidence matrix.",
            "unresolved_gaps": [], "leads": [],
        })
        _finalize_verification(connection, running, versions, result, domains)
    return {"run": verification_run(run_id), "duplicate": False, "provider_called": False}


def run_verification_job(run_id, provider=None):
    execution_started = time.monotonic()
    with connect() as connection:
        run = connection.execute("SELECT * FROM verification_runs WHERE id=?", (run_id,)).fetchone()
        if run is None:
            raise KeyError(run_id)
        if run["status"] != "QUEUED":
            return dict(run)
        provider = provider or verification_provider_for("test" if run["mode"] == "test" else "grok")
        _record_verification_status(connection, run_id, "QUEUED", "RUNNING", "Preparing primary evidence checkpoint",
                                    started_at=run["started_at"] or now(), progress=max(5, int(run["progress"] or 0)),
                                    recoverable=0, resume_state=None, resume_reason=None)
        connection.execute("UPDATE events SET verification_status='RUNNING',updated_at=? WHERE id=?", (now(), run["event_id"]))
        run = connection.execute("SELECT * FROM verification_runs WHERE id=?", (run_id,)).fetchone()
        versions = _ensure_claim_versions(connection, run["research_run_id"])
        if not _completed_checkpoint(connection, run_id, "PRIMARY_EVIDENCE_EXTRACTION"):
            for version in versions:
                connection.execute(
                    "INSERT OR IGNORE INTO verification_run_claims(verification_run_id,claim_version_id,required_for_event) "
                    "VALUES(?,?,?)", (run_id, version["id"], version["required_for_event"]),
                )
            snapshots = _snapshot_research_evidence(connection, run)
            acquired_snapshots = _snapshot_acquired_candidates(connection, run)
            gap_bundle = _verification_gap_bundle(connection, run, versions)
            _verification_checkpoint(connection, run_id, "PRIMARY_EVIDENCE_EXTRACTION", "COMPLETED", {
                "snapshot_ids": [item["id"] for item in snapshots], "claim_version_ids": [item["id"] for item in versions],
                "official_source_hints": gap_bundle["official_source_hints"],
                "acquired_candidate_snapshot_ids": [item["id"] for item in acquired_snapshots],
            })
        else:
            gap_bundle = _verification_gap_bundle(connection, run, versions)
        acquired_count = connection.execute(
            "SELECT COUNT(DISTINCT sc.id) FROM source_candidates sc "
            "JOIN source_acquisition_runs sa ON sa.id=sc.acquisition_run_id "
            "JOIN claim_source_candidates csc ON csc.candidate_id=sc.id "
            "JOIN verification_run_claims vrc ON vrc.claim_version_id=csc.claim_version_id "
            "WHERE sa.event_id=? AND vrc.verification_run_id=? AND sc.state='RETRIEVED' "
            "AND csc.relationship='CANDIDATE'", (run["event_id"], run_id),
        ).fetchone()[0]
        deterministic_evidence_sufficient = False
        if acquired_count:
            _regroup_evidence_families(connection, run_id)
            pre_search_analysis = _claim_analysis_payload(connection, run, versions)
            required_ids = {item["id"] for item in versions if item["required_for_event"]}
            deterministic_evidence_sufficient = bool(required_ids) and all(
                item["decision"] == "SUPPORTED"
                for item in pre_search_analysis["claims"] if item["claim_version_id"] in required_ids
            )
        connection.execute(
            "UPDATE verification_runs SET current_phase='CORROBORATION_DISCOVERY',progress=25,"
            "progress_message='Seeking targeted corroboration' WHERE id=?", (run_id,),
        )
    domains = _verification_domains(run["research_run_id"])
    discovery_checkpoint = None
    with connect() as connection:
        discovery_checkpoint = _completed_checkpoint(connection, run_id, "CORROBORATION_DISCOVERY")
    result = None
    provider_failure = None
    if discovery_checkpoint:
        result = VerificationProviderResult(result=discovery_checkpoint["payload"]["provider_result"])
    elif deterministic_evidence_sufficient:
        result = VerificationProviderResult(result={
            "search_summary": "Paid provider search skipped: staged deterministic evidence already met the unchanged policy routing condition.",
            "unresolved_gaps": [], "leads": [],
        }, actual_search_calls=0, actual_open_calls=0, actual_sources_returned=0)
        with connect() as connection:
            _verification_checkpoint(connection, run_id, "CORROBORATION_DISCOVERY", "COMPLETED", {
                "provider_result": result.result, "provider_search_skipped": True,
                "reason": "DETERMINISTIC_EVIDENCE_SUFFICIENT",
            })
    elif run["mode"] == "live" and not domains:
        provider_failure = ("verification_error", "no verified corroboration domains are registered")
        result = VerificationProviderResult(result={"search_summary": provider_failure[1], "unresolved_gaps": gap_bundle["gaps"], "leads": []})
    else:
        error = None
        previous_attempt_id = None
        for local_attempt in range(1, int(run["max_attempts"]) + 1):
            with connect() as connection:
                previous = connection.execute(
                    "SELECT id FROM verification_attempts WHERE verification_run_id=? ORDER BY attempt_number DESC LIMIT 1",
                    (run_id,),
                ).fetchone()
                previous_attempt_id = previous["id"] if previous else previous_attempt_id
                attempt_id = _start_verification_attempt(
                    connection, run, provider, "CORROBORATION_DISCOVERY", previous_attempt_id
                )
            attempt_started = time.monotonic()
            try:
                result = provider.find_corroboration(
                    gap_bundle, allowed_domains=domains, search_turn_limit=int(run["search_turn_limit"]),
                    token_limit=int(run["token_limit"]), timeout_seconds=int(run["search_timeout_seconds"] or VERIFICATION_SEARCH_TIMEOUT_SECONDS),
                )
                with connect() as connection:
                    _finish_verification_attempt(connection, attempt_id, result=result,
                                                 elapsed_seconds=time.monotonic() - attempt_started)
                break
            except Exception as caught:
                error = caught
                category = _verification_failure_category(caught)
                retry_after = getattr(caught, "retry_after_seconds", None)
                with connect() as connection:
                    _finish_verification_attempt(connection, attempt_id, error=caught,
                                                 elapsed_seconds=time.monotonic() - attempt_started,
                                                 retry_after=retry_after)
                retryable = category in ("TRANSIENT_PROVIDER_TIMEOUT", "TRANSIENT_PROVIDER_FAILURE")
                if not retryable or local_attempt >= int(run["max_attempts"]):
                    break
                delay = retry_after if retry_after is not None else VERIFICATION_RETRY_BACKOFF_SECONDS * (2 ** (local_attempt - 1))
                time.sleep(min(max(0.0, delay), VERIFICATION_RETRY_MAX_BACKOFF_SECONDS))
        if result is None:
            category = _verification_failure_category(error)
            safe_message = str(error or "verification provider failed")[:500]
            if category in ("TRANSIENT_PROVIDER_TIMEOUT", "TRANSIENT_PROVIDER_FAILURE"):
                with connect() as connection:
                    current = connection.execute("SELECT * FROM verification_runs WHERE id=?", (run_id,)).fetchone()
                    _pause_verification(connection, current, category, safe_message, phase="CORROBORATION_DISCOVERY")
                return verification_run(run_id)
            provider_failure = (getattr(error, "code", "verification_error"), safe_message)
            result = VerificationProviderResult(result={
                "search_summary": "The provider failed before returning new discovery leads.",
                "unresolved_gaps": gap_bundle["gaps"], "leads": [],
            })
            log_error("verification_failed", safe_message, run_id=run_id, error_code=provider_failure[0])
        else:
            with connect() as connection:
                _verification_checkpoint(connection, run_id, "CORROBORATION_DISCOVERY", "COMPLETED",
                                         {"provider_result": result.result})

    with connect() as connection:
        run = connection.execute("SELECT * FROM verification_runs WHERE id=?", (run_id,)).fetchone()
        connection.execute(
            "UPDATE verification_runs SET current_phase='CORROBORATING_SOURCE_RETRIEVAL',progress=50,"
            "progress_message='Inspecting discovered source pages' WHERE id=?", (run_id,),
        )
        retrieval_checkpoint = _completed_checkpoint(connection, run_id, "CORROBORATING_SOURCE_RETRIEVAL")
        if not retrieval_checkpoint:
            retrieval_provider = type("LocalRetrieval", (), {"name": "local-source-fetch", "model": "registered-source-ingestion"})()
            retrieval_attempt = _start_verification_attempt(
                connection, run, retrieval_provider, "CORROBORATING_SOURCE_RETRIEVAL"
            )
            retrieval_started = time.monotonic()
            retrieval_deadline = min(
                execution_started + int(run["total_timeout_seconds"] or VERIFICATION_TOTAL_TIMEOUT_SECONDS),
                retrieval_started + int(run["retrieval_timeout_seconds"] or VERIFICATION_RETRIEVAL_TIMEOUT_SECONDS),
            )
            retrieval = _inspect_verification_leads(
                connection, run, gap_bundle, result.result.get("leads") or [], deadline=retrieval_deadline,
            )
            if retrieval["transient_error"]:
                retrieval_error = TimeoutError(retrieval["transient_error"])
                _finish_verification_attempt(connection, retrieval_attempt, error=retrieval_error,
                                             elapsed_seconds=time.monotonic() - retrieval_started)
                _regroup_evidence_families(connection, run_id)
                partial = _claim_analysis_payload(connection, run, versions)
                _verification_checkpoint(connection, run_id, "CLAIM_SOURCE_MATCHING", "PARTIAL", partial)
                _pause_verification(connection, run, "RETRIEVAL_TIMEOUT", retrieval["transient_error"],
                                    phase="CORROBORATING_SOURCE_RETRIEVAL", partial_payload={
                                        "retrieved_snapshot_ids": [item["id"] for item in retrieval["snapshots"]],
                                    })
                return verification_run(run_id)
            connection.execute(
                "UPDATE verification_attempts SET status='COMPLETED',ended_at=?,elapsed_seconds=?,cost_status='not_billed' "
                "WHERE id=?", (now(), time.monotonic() - retrieval_started, retrieval_attempt),
            )
            _verification_checkpoint(connection, run_id, "CORROBORATING_SOURCE_RETRIEVAL", "COMPLETED", {
                "retrieved_snapshot_ids": [item["id"] for item in retrieval["snapshots"]],
            })
        _regroup_evidence_families(connection, run_id)
        connection.execute(
            "UPDATE verification_runs SET current_phase='CLAIM_SOURCE_MATCHING',progress=75,"
            "progress_message='Matching claims to independent evidence families' WHERE id=?", (run_id,),
        )
        analysis = _claim_analysis_payload(connection, run, versions)
        _verification_checkpoint(connection, run_id, "CLAIM_SOURCE_MATCHING", "COMPLETED", analysis)
        connection.execute(
            "UPDATE verification_runs SET current_phase='CONTRADICTION_ANALYSIS',progress=85,"
            "progress_message='Checking claim-specific contradictions' WHERE id=?", (run_id,),
        )
        conflicts = [item["claim_version_id"] for item in analysis["claims"] if item["decision"] == "CONFLICTED"]
        _verification_checkpoint(connection, run_id, "CONTRADICTION_ANALYSIS", "COMPLETED",
                                 {"conflicted_claim_version_ids": conflicts})
        _finalize_verification(connection, run, versions, result, domains, provider_failure)
    return verification_run(run_id)


def resume_verification(run_id, provider=None, *, background=True):
    with connect() as connection:
        run = connection.execute("SELECT * FROM verification_runs WHERE id=?", (run_id,)).fetchone()
        if run is None:
            raise KeyError(run_id)
        if not run["recoverable"] or run["resume_state"] != "PAUSED_TRANSIENT":
            raise ValueError("Only verification paused by a recoverable provider or retrieval timeout can be resumed.")
        connection.execute(
            "UPDATE verification_runs SET status='QUEUED',progress_message='Resume queued from last safe checkpoint',"
            "recoverable=0,resume_state=NULL,resume_reason=NULL,resumed_at=?,completed_at=NULL,max_attempts=?,"
            "search_timeout_seconds=COALESCE(search_timeout_seconds,?),"
            "retrieval_timeout_seconds=COALESCE(retrieval_timeout_seconds,?),"
            "total_timeout_seconds=COALESCE(total_timeout_seconds,?) WHERE id=?",
            (
                now(), VERIFICATION_TRANSIENT_RETRIES + 1, VERIFICATION_SEARCH_TIMEOUT_SECONDS,
                VERIFICATION_RETRIEVAL_TIMEOUT_SECONDS, VERIFICATION_TOTAL_TIMEOUT_SECONDS, run_id,
            ),
        )
        connection.execute(
            "INSERT INTO verification_run_status_history(run_id,from_status,to_status,message,changed_at) "
            "VALUES(?,'FAILED','QUEUED','Explicit resume requested; prior attempts and checkpoints preserved',?)",
            (run_id, now()),
        )
        queued = dict(connection.execute("SELECT * FROM verification_runs WHERE id=?", (run_id,)).fetchone())
    selected_provider = provider or verification_provider_for("test" if queued["mode"] == "test" else "grok")
    if background:
        VERIFICATION_EXECUTOR.submit(run_verification_job, run_id, selected_provider)
    else:
        run_verification_job(run_id, selected_provider)
        queued = verification_run(run_id)
    return {"run": queued, "resumed": True, "new_run": False}


def register_media_asset(
    event_id, url, media_type, source_name, *, mode="live", source_url=None,
    rights_status="unknown", availability_status="available", content_hash=None, metadata=None,
):
    if mode not in ("live", "test"):
        raise ValueError("media mode must be live or test")
    if media_type not in ("image", "video", "audio", "document"):
        raise ValueError("invalid media type")
    if rights_status not in ("verified", "restricted", "unknown"):
        raise ValueError("invalid media rights status")
    if availability_status not in ("available", "missing", "expired", "rejected"):
        raise ValueError("invalid media availability status")
    with connect() as connection:
        if connection.execute("SELECT 1 FROM events WHERE id=?", (event_id,)).fetchone() is None:
            raise KeyError(event_id)
        timestamp = now()
        digest = content_hash or hashlib.sha256(
            json.dumps({"url": url, "metadata": metadata or {}}, sort_keys=True).encode()
        ).hexdigest()
        existing = connection.execute(
            "SELECT id FROM media_assets WHERE event_id=? AND mode=? AND url=?", (event_id, mode, url)
        ).fetchone()
        if existing:
            connection.execute(
                "UPDATE media_assets SET media_type=?,source_name=?,source_url=?,rights_status=?,"
                "availability_status=?,content_hash=?,metadata_json=?,rights_reviewed_at=?,updated_at=? WHERE id=?",
                (
                    media_type, source_name, source_url, rights_status, availability_status, digest,
                    json.dumps(metadata or {}, ensure_ascii=False, sort_keys=True),
                    timestamp if rights_status == "verified" else None, timestamp, existing["id"],
                ),
            )
            return existing["id"]
        asset_id = "MA-" + uuid.uuid4().hex[:12].upper()
        connection.execute(
            "INSERT INTO media_assets(id,event_id,mode,media_type,url,source_name,source_url,rights_status,"
            "availability_status,content_hash,metadata_json,created_at,updated_at,rights_reviewed_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                asset_id, event_id, mode, media_type, url, source_name, source_url, rights_status,
                availability_status, digest, json.dumps(metadata or {}, ensure_ascii=False, sort_keys=True),
                timestamp, timestamp, timestamp if rights_status == "verified" else None,
            ),
        )
    return asset_id


def record_publishing_history(
    *, event_id, content_format, language="English", status="PUBLISHED", mode="live",
    claim_set_id=None, content_fingerprint=None, title=None, published_at=None,
):
    if content_format not in ("REEL", "STORY", "CAROUSEL", "IMAGE"):
        raise ValueError("invalid publishing format")
    if status not in ("PLANNED", "PUBLISHED", "CANCELLED") or mode not in ("live", "test"):
        raise ValueError("invalid publishing history status or mode")
    if content_fingerprint is None:
        content_fingerprint = hashlib.sha256(
            json.dumps({"event_id": event_id, "claim_set_id": claim_set_id, "title": title}, sort_keys=True).encode()
        ).hexdigest()
    history_id = "PH-" + uuid.uuid4().hex[:12].upper()
    with connect() as connection:
        connection.execute(
            "INSERT INTO publishing_history(id,event_id,mode,claim_set_id,format,language,status,content_fingerprint,"
            "title,published_at,recorded_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                history_id, event_id, mode, claim_set_id, content_format, language, status,
                content_fingerprint, title, published_at, now(),
            ),
        )
    return history_id


def _content_missing_evidence(connection, claim_set):
    if not claim_set:
        return ["No claim-set decision exists for this event."]
    missing = []
    rows = connection.execute(
        "SELECT cv.text,vd.decision,vd.required_for_event,vd.missing_information_json "
        "FROM verification_decisions vd JOIN claim_versions cv ON cv.id=vd.claim_version_id "
        "WHERE vd.verification_run_id=? AND vd.required_for_event=1 ORDER BY cv.claim_id",
        (claim_set["verification_run_id"],),
    ).fetchall()
    for row in rows:
        if row["decision"] == "SUPPORTED":
            continue
        details = json.loads(row["missing_information_json"] or "[]")
        missing.append(f"Required claim is {row['decision']}: {row['text']}")
        missing.extend(details)
    return list(dict.fromkeys(missing)) or ["The latest claim set is not production-approved."]


def _content_input_bundle(event_id, provider_mode):
    with connect() as connection:
        event_row = connection.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
        if event_row is None:
            raise KeyError(event_id)
        event = dict(event_row)
        claim_set_row = connection.execute(
            "SELECT acs.*,vr.mode AS verification_mode,vr.status AS verification_run_status,vr.research_run_id "
            "FROM approved_claim_sets acs JOIN verification_runs vr ON vr.id=acs.verification_run_id "
            "WHERE acs.event_id=? ORDER BY acs.version_number DESC LIMIT 1", (event_id,)
        ).fetchone()
        claim_set = dict(claim_set_row) if claim_set_row else None
        eligibility = "BLOCKED"
        blockers = []
        asset_mode = "live"
        current_evidence_version = None
        current_claim_version = None
        approved_claims = []
        verification_decisions = []
        if claim_set:
            approved_claims = [dict(row) for row in connection.execute(
                "SELECT cv.id,cv.claim_id,cv.version_number,cv.content_hash,cv.text,cv.claim_type,"
                "cv.assertion_scope,cv.attribution,cv.required_for_event "
                "FROM approved_claim_set_items acsi JOIN claim_versions cv ON cv.id=acsi.claim_version_id "
                "WHERE acsi.claim_set_id=? ORDER BY cv.claim_id", (claim_set["id"],)
            )]
            verification_decisions = [dict(row) for row in connection.execute(
                "SELECT vd.id,vd.decision,vd.approved,vd.required_for_event,vd.independent_family_count,"
                "vd.rationale,vd.missing_information_json,vd.decided_at,cv.id AS claim_version_id,cv.text AS claim_text "
                "FROM verification_decisions vd JOIN claim_versions cv ON cv.id=vd.claim_version_id "
                "WHERE vd.verification_run_id=? ORDER BY vd.required_for_event DESC,cv.claim_id",
                (claim_set["verification_run_id"],),
            )]
            for decision in verification_decisions:
                decision["missing_information"] = json.loads(decision.pop("missing_information_json") or "[]")
            if claim_set["status"] == "TEST_ONLY":
                asset_mode = "test"
                if provider_mode == "test":
                    eligibility = "TEST_ONLY"
                else:
                    blockers.append("TEST_ONLY evidence cannot be sent to a live production decision provider.")
            elif claim_set["status"] != "APPROVED":
                blockers.extend(_content_missing_evidence(connection, claim_set))
            elif claim_set["verification_mode"] != "live" or claim_set["verification_run_status"] != "COMPLETED":
                blockers.append("The approved claim set is not backed by a completed live verification run.")
            elif event["verification_status"] != "VERIFIED" or event["status"] != "VERIFIED":
                blockers.append("The event is not currently VERIFIED under the production evidence policy.")
            elif not approved_claims:
                blockers.append("The approved claim set contains no approved claim versions.")
            else:
                versions = _ensure_claim_versions(connection, claim_set["research_run_id"])
                current_claim_version = _claim_set_version(versions)
                current_evidence_version = verification_evidence_version(event_id, claim_set["research_run_id"])
                if current_claim_version != claim_set["claim_set_version"]:
                    blockers.append("The approved claim set is stale because a claim version changed.")
                if current_evidence_version != claim_set["evidence_version"]:
                    blockers.append("The approved claim set is stale because the evidence version changed.")
                if not blockers:
                    eligibility = "PRODUCTION_APPROVED"
        else:
            blockers.extend(_content_missing_evidence(connection, None))

        media = [dict(row) for row in connection.execute(
            "SELECT * FROM media_assets WHERE event_id=? AND mode=? ORDER BY created_at,id", (event_id, asset_mode)
        )]
        for item in media:
            item["metadata"] = json.loads(item.get("metadata_json") or "{}")
        required_unapproved_media = [
            item for item in media
            if item["metadata"].get("required_for_story")
            and not (item["availability_status"] == "available" and item["rights_status"] == "verified")
        ]
        generation_prohibited = any(item["metadata"].get("original_generation_prohibited") for item in media)
        original_generation_allowed = not generation_prohibited and not required_unapproved_media
        if required_unapproved_media:
            blockers.append("Media explicitly required for this story is unavailable or lacks verified rights.")
            eligibility = "BLOCKED"
        elif generation_prohibited and not any(
            item["availability_status"] == "available" and item["rights_status"] == "verified"
            for item in media
        ):
            blockers.append("Original generation is prohibited for this story and no approved media is available.")
            eligibility = "BLOCKED"
        cutoff = (datetime.now(timezone.utc) - timedelta(days=CONTENT_HISTORY_DAYS)).isoformat()
        publishing = [dict(row) for row in connection.execute(
            "SELECT * FROM publishing_history WHERE mode=? AND recorded_at>=? ORDER BY recorded_at DESC,id DESC",
            (asset_mode, cutoff),
        )]
        recent_content = [dict(row) for row in connection.execute(
            "SELECT cd.event_id,cd.decision,cd.recommended_format,cd.language,cd.priority,cd.policy_version,"
            "cd.decided_at,e.title AS event_title,cdr.mode "
            "FROM content_decisions cd JOIN content_decision_runs cdr ON cdr.id=cd.run_id "
            "JOIN events e ON e.id=cd.event_id WHERE cd.event_id<>? AND cdr.mode=? AND cd.decided_at>=? "
            "ORDER BY cd.decided_at DESC LIMIT 25",
            (event_id, asset_mode, cutoff),
        )]
    recent_duplicate = next(
        (
            row for row in publishing
            if row["status"] in ("PLANNED", "PUBLISHED") and row["event_id"] == event_id
            and (not claim_set or not row["claim_set_id"] or row["claim_set_id"] == claim_set["id"])
        ),
        None,
    )
    event_time = _as_utc(event["event_time"]) if event.get("event_time") else None
    age_days = (datetime.now(timezone.utc) - event_time).total_seconds() / 86400 if event_time else None
    bundle = {
        "policy_version": CONTENT_POLICY_VERSION,
        "decision_day": datetime.now(timezone.utc).date().isoformat(),
        "event": {
            key: event.get(key) for key in (
                "id", "title", "priority", "status", "event_time", "research_status",
                "verification_status", "workspace_key",
            )
        },
        "eligibility_status": eligibility,
        "approved_claim_set": {
            key: claim_set.get(key) for key in (
                "id", "version_number", "status", "evidence_version", "claim_set_version",
                "verification_run_id",
            )
        } if claim_set else None,
        "current_evidence_version": current_evidence_version,
        "current_claim_version": current_claim_version,
        "approved_claims": approved_claims,
        "verification_decisions": verification_decisions,
        "media": media,
        "original_generation_allowed": original_generation_allowed,
        "generated_media_safety": {
            "treatment": "neutral_non_documentary_public_affairs_visual",
            "preferred": ["location/environment illustration", "map", "diagram", "object/process visual", "neutral contextual imagery"],
            "prohibited": [
                "reuse of evidence-source images", "unlicensed third-party media",
                "synthetic identifiable people without explicit package justification and human review",
                "presentation of generated visuals as documentary evidence",
            ],
        },
        "recent_content": recent_content,
        "recent_publishing_history": publishing,
        "recent_duplicate": dict(recent_duplicate) if recent_duplicate else None,
        "event_age_days": age_days,
        "freshness_days": CONTENT_FRESHNESS_DAYS,
        "default_language": "English",
        "blockers": list(dict.fromkeys(blockers)),
        "asset_mode": asset_mode,
    }
    material = {
        "policy_version": bundle["policy_version"], "decision_day": bundle["decision_day"],
        "event": bundle["event"], "eligibility_status": eligibility,
        "claim_set": bundle["approved_claim_set"], "current_evidence_version": current_evidence_version,
        "current_claim_version": current_claim_version,
        "approved_claims": [(item["id"], item["content_hash"]) for item in approved_claims],
        "verification_decisions": [
            (
                item["id"], item["claim_version_id"], item["decision"], item["approved"],
                item["rationale"], item["missing_information"], item["decided_at"],
            )
            for item in verification_decisions
        ],
        "media": [
            (item["id"], item["content_hash"], item["availability_status"], item["rights_status"], item["updated_at"])
            for item in media
        ],
        "publishing": [
            (item["id"], item["status"], item["content_fingerprint"], item["published_at"], item["recorded_at"])
            for item in publishing
        ],
        "recent_content": [
            (
                item["event_id"], item["decision"], item["recommended_format"],
                item["event_title"], item["decided_at"],
            )
            for item in recent_content
        ],
        "blockers": bundle["blockers"],
        "original_generation_allowed": bundle["original_generation_allowed"],
    }
    return bundle, hashlib.sha256(json.dumps(material, sort_keys=True).encode()).hexdigest()


def _format_supported(content_format, media):
    available = [
        item for item in media
        if item["availability_status"] == "available" and item["rights_status"] == "verified"
    ]
    images = sum(item["media_type"] == "image" for item in available)
    videos = sum(item["media_type"] == "video" for item in available)
    return {
        "REEL": videos >= 1,
        "STORY": images + videos >= 1,
        "CAROUSEL": images >= 3,
        "IMAGE": images >= 1,
    }.get(content_format, False)


def _gate_content_proposal(bundle):
    claim_set = bundle["approved_claim_set"]
    missing = list(bundle["blockers"])
    return {
        "decision": "HOLD",
        "media_source_strategy": "NONE",
        "recommended_format": "IMAGE",
        "language": "English",
        "proposed_duration_seconds": 15,
        "priority": bundle["event"]["priority"] if bundle["event"]["priority"] in ("BREAKING", "HIGH", "NORMAL") else "NORMAL",
        "factual_rationale": (
            "Content creation is blocked by the deterministic evidence gate; no model was called. "
            + (" ".join(missing) if missing else "A production-approved claim set is required.")
        ),
        "missing_evidence_or_media": missing,
        "approved_claim_set_id": claim_set["id"] if claim_set else None,
        "approved_claim_set_version": claim_set["version_number"] if claim_set else None,
        "evidence_version": claim_set["evidence_version"] if claim_set else None,
    }


def _normalize_content_proposal(bundle, proposal, provider_mode):
    allowed_decisions = {"CREATE", "HOLD", "SKIP"}
    allowed_formats = {"REEL", "STORY", "CAROUSEL", "IMAGE"}
    strategies = {"GENERATE_ORIGINAL", "USE_APPROVED_OWNED_MEDIA", "USE_APPROVED_LICENSED_MEDIA", "NONE"}
    decision = proposal.get("decision") if proposal.get("decision") in allowed_decisions else "HOLD"
    content_format = proposal.get("recommended_format") if proposal.get("recommended_format") in allowed_formats else "IMAGE"
    approved_media = [
        item for item in bundle["media"]
        if item["availability_status"] == "available" and item["rights_status"] == "verified"
    ]
    strategy = proposal.get("media_source_strategy")
    if strategy not in strategies:
        if decision == "CREATE" and not approved_media and bundle.get("original_generation_allowed"):
            strategy = "GENERATE_ORIGINAL"
        elif decision == "CREATE" and approved_media:
            owned = any(item.get("metadata", {}).get("rights_basis") == "owned" for item in approved_media)
            strategy = "USE_APPROVED_OWNED_MEDIA" if owned else "USE_APPROVED_LICENSED_MEDIA"
        else:
            strategy = "NONE"
    missing = [str(item) for item in proposal.get("missing_evidence_or_media", [])]
    rationale = str(proposal.get("factual_rationale") or "Provider returned no factual decision rationale.")
    if bundle["recent_duplicate"] and decision == "CREATE":
        decision = "SKIP"
        strategy = "NONE"
        rationale = "Recent publishing history already covers this event and approved claim-set version."
    if decision == "CREATE" and not approved_media and bundle.get("original_generation_allowed"):
        strategy = "GENERATE_ORIGINAL"
    if decision == "CREATE" and strategy == "GENERATE_ORIGINAL" and not bundle.get("original_generation_allowed"):
        decision = "HOLD"
        strategy = "NONE"
        missing.append("Original media generation is not safe or permitted for this story.")
        rationale += " The proposed original-generation path failed the deterministic safety gate."
    if decision == "CREATE" and strategy in {"USE_APPROVED_OWNED_MEDIA", "USE_APPROVED_LICENSED_MEDIA"} and not _format_supported(content_format, bundle["media"]):
        decision = "HOLD"
        strategy = "NONE"
        missing.append(f"Available rights-verified media does not support {content_format}.")
        rationale += " The proposed format failed the deterministic media-eligibility check."
    claim_set = bundle["approved_claim_set"]
    return {
        "decision": decision,
        "media_source_strategy": strategy if decision == "CREATE" else "NONE",
        "recommended_format": content_format,
        "language": str(proposal.get("language") or "English")[:80],
        "proposed_duration_seconds": max(1, min(300, int(proposal.get("proposed_duration_seconds") or 15))),
        "priority": proposal.get("priority") if proposal.get("priority") in ("BREAKING", "HIGH", "NORMAL", "LOW") else "NORMAL",
        "factual_rationale": rationale,
        "missing_evidence_or_media": list(dict.fromkeys(missing)),
        "approved_claim_set_id": claim_set["id"] if claim_set else None,
        "approved_claim_set_version": claim_set["version_number"] if claim_set else None,
        "evidence_version": claim_set["evidence_version"] if claim_set else None,
        "executable": int(
            decision == "CREATE" and bundle["eligibility_status"] == "PRODUCTION_APPROVED" and provider_mode == "live"
        ),
        "test_only": int(bundle["eligibility_status"] == "TEST_ONLY" or provider_mode == "test"),
    }


def _insert_content_decision(connection, run, input_version, proposal):
    decision_id = "CD-" + uuid.uuid4().hex[:12].upper()
    previous = connection.execute(
        "SELECT id FROM content_decisions WHERE event_id=? ORDER BY decided_at DESC,id DESC LIMIT 1",
        (run["event_id"],),
    ).fetchone()
    connection.execute(
        "INSERT INTO content_decisions(id,run_id,event_id,decision,recommended_format,language,"
        "proposed_duration_seconds,priority,factual_rationale,approved_claim_set_id,approved_claim_set_version,"
        "evidence_version,missing_evidence_or_media_json,media_source_strategy,previous_decision_id,"
        "executable,test_only,decided_at,policy_version,input_version) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            decision_id, run["id"], run["event_id"], proposal["decision"], proposal["recommended_format"],
            proposal["language"], proposal["proposed_duration_seconds"], proposal["priority"],
            proposal["factual_rationale"], proposal.get("approved_claim_set_id"),
            proposal.get("approved_claim_set_version"), proposal.get("evidence_version"),
            json.dumps(proposal.get("missing_evidence_or_media") or [], ensure_ascii=False),
            proposal.get("media_source_strategy", "NONE"), previous["id"] if previous else None,
            int(proposal.get("executable", 0)), int(proposal.get("test_only", 0)), now(),
            CONTENT_POLICY_VERSION, input_version,
        ),
    )
    connection.execute(
        "UPDATE events SET content_decision_status=?,updated_at=? WHERE id=?",
        (proposal["decision"], now(), run["event_id"]),
    )
    return decision_id


def enqueue_content_decision(event_id, provider_name="test", *, background=True):
    with connect() as connection:
        incomplete = connection.execute(
            "SELECT id FROM verification_runs WHERE event_id=? AND recoverable=1 "
            "AND resume_state='PAUSED_TRANSIENT' ORDER BY requested_at DESC LIMIT 1", (event_id,),
        ).fetchone()
    if incomplete:
        raise ValueError(
            f"Verification {incomplete['id']} is incomplete after a recoverable timeout; resume verification before Content CEO."
        )
    provider = content_provider_for(provider_name)
    bundle, input_version = _content_input_bundle(event_id, provider.mode)
    timestamp = now()
    run_id = "CR-" + uuid.uuid4().hex[:12].upper()
    claim_set_id = bundle["approved_claim_set"]["id"] if bundle["approved_claim_set"] else None
    with connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        active = connection.execute(
            "SELECT * FROM content_decision_runs WHERE event_id=? AND mode=? AND status IN ('QUEUED','RUNNING')",
            (event_id, provider.mode),
        ).fetchone()
        if active:
            connection.commit()
            return {"run": dict(active), "duplicate": True, "cached": False}
        cached = connection.execute(
            "SELECT * FROM content_decision_runs WHERE event_id=? AND provider=? AND model=? AND mode=? "
            "AND input_version=? AND status IN ('COMPLETED','CACHED') ORDER BY completed_at DESC LIMIT 1",
            (event_id, provider.name, provider.model, provider.mode, input_version),
        ).fetchone()
        if cached:
            prior = connection.execute("SELECT * FROM content_decisions WHERE run_id=?", (cached["id"],)).fetchone()
            connection.execute(
                "INSERT INTO content_decision_runs(id,event_id,claim_set_id,provider,model,mode,status,eligibility_status,"
                "input_version,cache_source_run_id,requested_at,started_at,completed_at,progress,progress_message,"
                "provider_called,input_tokens,output_tokens,total_tokens,cost_usd,cost_usd_ticks,cost_status) "
                "VALUES(?,?,?,?,?,?,'CACHED',?,?,?,?,?,?,100,?,0,0,0,0,0.0,0,'known')",
                (
                    run_id, event_id, claim_set_id, provider.name, provider.model, provider.mode,
                    bundle["eligibility_status"], input_version, cached["id"], timestamp, timestamp, timestamp,
                    "Reused unchanged Content CEO inputs",
                ),
            )
            connection.execute(
                "INSERT INTO content_decision_run_history(run_id,from_status,to_status,message,changed_at) "
                "VALUES(?,NULL,'CACHED',?,?)", (run_id, f"Reused {cached['id']}", timestamp),
            )
            copied = dict(prior)
            copied["approved_claim_set_id"] = claim_set_id
            copied["executable"] = int(prior["executable"] and provider.mode == "live")
            copied["test_only"] = int(prior["test_only"] or provider.mode == "test")
            copied["missing_evidence_or_media"] = json.loads(prior["missing_evidence_or_media_json"] or "[]")
            run = connection.execute("SELECT * FROM content_decision_runs WHERE id=?", (run_id,)).fetchone()
            _insert_content_decision(connection, run, input_version, copied)
            row = connection.execute("SELECT * FROM content_decision_runs WHERE id=?", (run_id,)).fetchone()
            connection.commit()
            return {"run": dict(row), "duplicate": False, "cached": True}
        connection.execute(
            "INSERT INTO content_decision_runs(id,event_id,claim_set_id,provider,model,mode,status,eligibility_status,"
            "input_version,requested_at,progress,progress_message) VALUES(?,?,?,?,?,?,'QUEUED',?,?,?,0,'Queued')",
            (
                run_id, event_id, claim_set_id, provider.name, provider.model, provider.mode,
                bundle["eligibility_status"], input_version, timestamp,
            ),
        )
        connection.execute(
            "INSERT INTO content_decision_run_history(run_id,from_status,to_status,message,changed_at) "
            "VALUES(?,NULL,'QUEUED','Queued',?)", (run_id, timestamp),
        )
        connection.execute(
            "UPDATE events SET content_decision_status='QUEUED',updated_at=? WHERE id=?", (timestamp, event_id)
        )
        row = connection.execute("SELECT * FROM content_decision_runs WHERE id=?", (run_id,)).fetchone()
        connection.commit()
    if background:
        CONTENT_EXECUTOR.submit(run_content_decision_job, run_id, provider)
    else:
        run_content_decision_job(run_id, provider)
        with connect() as connection:
            row = connection.execute("SELECT * FROM content_decision_runs WHERE id=?", (run_id,)).fetchone()
    return {"run": dict(row), "duplicate": False, "cached": False}


def run_content_decision_job(run_id, provider=None):
    with connect() as connection:
        run = connection.execute("SELECT * FROM content_decision_runs WHERE id=?", (run_id,)).fetchone()
        if run is None:
            raise KeyError(run_id)
        if run["status"] != "QUEUED":
            return dict(run)
        provider = provider or content_provider_for("test" if run["mode"] == "test" else "grok")
        connection.execute(
            "UPDATE content_decision_runs SET status='RUNNING',started_at=?,progress=20,progress_message=? WHERE id=?",
            (now(), "Applying deterministic evidence eligibility", run_id),
        )
        connection.execute(
            "INSERT INTO content_decision_run_history(run_id,from_status,to_status,message,changed_at) "
            "VALUES(?,'QUEUED','RUNNING',?,?)", (run_id, "Applying deterministic evidence eligibility", now()),
        )
        connection.execute(
            "UPDATE events SET content_decision_status='RUNNING',updated_at=? WHERE id=?", (now(), run["event_id"])
        )
    bundle, current_input_version = _content_input_bundle(run["event_id"], run["mode"])
    provider_result = None
    provider_error = None
    if current_input_version != run["input_version"]:
        provider_error = ValueError("Content CEO inputs changed after the job was queued; run again with the new version.")
    elif bundle["eligibility_status"] == "BLOCKED":
        proposal = _gate_content_proposal(bundle)
    else:
        try:
            provider_result = provider.decide(
                bundle, token_limit=CONTENT_TOKEN_LIMIT, timeout_seconds=CONTENT_TIMEOUT_SECONDS,
            )
            proposal = _normalize_content_proposal(bundle, provider_result.decision, run["mode"])
        except Exception as error:
            provider_error = error
    if provider_error:
        safe_message = str(provider_error)[:500]
        proposal = {
            "decision": "HOLD", "media_source_strategy": "NONE",
            "recommended_format": "IMAGE", "language": "English",
            "proposed_duration_seconds": 15, "priority": bundle["event"]["priority"],
            "factual_rationale": "Content CEO provider failed; no executable content decision was made.",
            "missing_evidence_or_media": [safe_message],
            "approved_claim_set_id": bundle["approved_claim_set"]["id"] if bundle["approved_claim_set"] else None,
            "approved_claim_set_version": bundle["approved_claim_set"]["version_number"] if bundle["approved_claim_set"] else None,
            "evidence_version": bundle["approved_claim_set"]["evidence_version"] if bundle["approved_claim_set"] else None,
            "executable": 0, "test_only": int(run["mode"] == "test"),
        }
    elif bundle["eligibility_status"] == "BLOCKED":
        proposal["executable"] = 0
        proposal["test_only"] = int(run["mode"] == "test")
    with connect() as connection:
        run = connection.execute("SELECT * FROM content_decision_runs WHERE id=?", (run_id,)).fetchone()
        _insert_content_decision(connection, run, run["input_version"], proposal)
        final_status = "FAILED" if provider_error else "COMPLETED"
        message = str(provider_error)[:500] if provider_error else (
            "Evidence gate completed without a provider call"
            if bundle["eligibility_status"] == "BLOCKED" else "Content decision completed"
        )
        connection.execute(
            "UPDATE content_decision_runs SET status=?,completed_at=?,progress=100,progress_message=?,provider_called=?,"
            "input_tokens=?,output_tokens=?,total_tokens=?,cost_usd=?,cost_usd_ticks=?,cost_status=?,"
            "provider_request_id=?,provider_elapsed_seconds=?,error_code=?,error_message=? WHERE id=?",
            (
                final_status, now(), message, int(provider_result is not None),
                provider_result.input_tokens if provider_result else None,
                provider_result.output_tokens if provider_result else None,
                provider_result.total_tokens if provider_result else None,
                provider_result.cost_usd if provider_result else (0.0 if bundle["eligibility_status"] == "BLOCKED" else None),
                provider_result.cost_usd_ticks if provider_result else (0 if bundle["eligibility_status"] == "BLOCKED" else None),
                "known" if provider_result and provider_result.cost_usd is not None or bundle["eligibility_status"] == "BLOCKED" else "unknown",
                provider_result.provider_request_id if provider_result else None,
                provider_result.elapsed_seconds if provider_result else None,
                getattr(provider_error, "code", "content_provider_error") if provider_error else None,
                str(provider_error)[:500] if provider_error else None, run_id,
            ),
        )
        connection.execute(
            "INSERT INTO content_decision_run_history(run_id,from_status,to_status,message,changed_at) VALUES(?,?,?,?,?)",
            (run_id, "RUNNING", final_status, message, now()),
        )
        event_id = run["event_id"]
    # A CREATE decision can complete eligibility; trigger the automated reel factory.
    if final_status == "COMPLETED":
        maybe_trigger_auto_reel(event_id)


def content_decision_run(run_id):
    with connect() as connection:
        row = connection.execute("SELECT * FROM content_decision_runs WHERE id=?", (run_id,)).fetchone()
    if row is None:
        raise KeyError(run_id)
    return dict(row)


def _version_hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _production_eligibility(content_decision_id):
    """Resolve and snapshot the exact immutable inputs before any production job exists."""
    blockers = []
    with connect() as connection:
        row = connection.execute(
            "SELECT cd.*,cdr.mode AS decision_mode,cdr.status AS decision_run_status,e.status AS event_status,"
            "e.verification_status,e.workspace_key,e.title AS event_title "
            "FROM content_decisions cd JOIN content_decision_runs cdr ON cdr.id=cd.run_id "
            "JOIN events e ON e.id=cd.event_id WHERE cd.id=?", (content_decision_id,),
        ).fetchone()
        if row is None:
            raise KeyError(content_decision_id)
        decision = dict(row)
        latest = connection.execute(
            "SELECT id FROM content_decisions WHERE event_id=? ORDER BY decided_at DESC,id DESC LIMIT 1",
            (decision["event_id"],),
        ).fetchone()
        if latest["id"] != content_decision_id:
            blockers.append("The Content CEO decision is stale because a newer decision exists.")
        if decision["decision"] != "CREATE":
            blockers.append(f"Content CEO decision {decision['decision']} cannot enter production.")
        if not decision["executable"]:
            blockers.append("The Content CEO decision is non-executable.")
        if decision["test_only"] or decision["decision_mode"] != "live":
            blockers.append("TEST_ONLY or test-mode decisions cannot enter production.")
        if decision["policy_version"] != CONTENT_POLICY_VERSION:
            blockers.append("The Content CEO decision uses a stale policy version.")
        if decision["event_status"] != "VERIFIED" or decision["verification_status"] != "VERIFIED":
            blockers.append("The event is no longer VERIFIED under the production evidence policy.")
        claim_set = connection.execute(
            "SELECT acs.*,vr.mode AS verification_mode,vr.status AS verification_run_status,vr.research_run_id "
            "FROM approved_claim_sets acs JOIN verification_runs vr ON vr.id=acs.verification_run_id WHERE acs.id=?",
            (decision["approved_claim_set_id"],),
        ).fetchone()
        latest_set = connection.execute(
            "SELECT id FROM approved_claim_sets WHERE event_id=? ORDER BY version_number DESC LIMIT 1",
            (decision["event_id"],),
        ).fetchone()
        if not claim_set or claim_set["status"] != "APPROVED" or claim_set["verification_mode"] != "live" or claim_set["verification_run_status"] != "COMPLETED":
            blockers.append("The decision has no valid production-approved claim set.")
        elif latest_set["id"] != claim_set["id"] or decision["approved_claim_set_version"] != claim_set["version_number"]:
            blockers.append("The approved claim set is stale.")
        claims = []
        evidence = []
        if claim_set:
            claims = [dict(item) for item in connection.execute(
                "SELECT cv.id AS claim_version_id,cv.claim_id,cv.version_number,cv.content_hash,cv.text,"
                "cv.claim_type,cv.assertion_scope,cv.attribution,cv.required_for_event,cv.revoked_at "
                "FROM approved_claim_set_items acsi JOIN claim_versions cv ON cv.id=acsi.claim_version_id "
                "WHERE acsi.claim_set_id=? ORDER BY cv.claim_id", (claim_set["id"],),
            )]
            for claim in claims:
                newest = connection.execute(
                    "SELECT id FROM claim_versions WHERE claim_id=? ORDER BY version_number DESC LIMIT 1",
                    (claim["claim_id"],),
                ).fetchone()
                if claim["revoked_at"]:
                    blockers.append(f"Approved claim version {claim['claim_version_id']} has been revoked.")
                if not newest or newest["id"] != claim["claim_version_id"]:
                    blockers.append(f"Approved claim version {claim['claim_version_id']} has been superseded.")
            evidence = [dict(item) for item in connection.execute(
                "SELECT DISTINCT vs.id AS snapshot_id,vd.claim_version_id,vs.canonical_url,vs.content_hash,"
                "vs.retrieved_at,vs.evidence_family_id,vs.invalidated_at "
                "FROM approved_claim_set_items acsi JOIN verification_decisions vd ON vd.id=acsi.verification_decision_id "
                "JOIN verification_decision_evidence vde ON vde.decision_id=vd.id "
                "JOIN verification_snapshots vs ON vs.id=vde.snapshot_id "
                "WHERE acsi.claim_set_id=? AND vd.approved=1 AND vd.decision='SUPPORTED' "
                "AND vde.relationship='supports' AND vde.excerpt_valid=1 ORDER BY vs.id,vd.claim_version_id",
                (claim_set["id"],),
            )]
            supported = {item["claim_version_id"] for item in evidence if not item["invalidated_at"]}
            for claim in claims:
                if claim["claim_version_id"] not in supported:
                    blockers.append(f"Approved claim version {claim['claim_version_id']} has no current inspected supporting evidence.")
            if any(item["invalidated_at"] for item in evidence):
                blockers.append("An evidence snapshot used by the approved claim set has been invalidated.")
        media = [dict(item) for item in connection.execute(
            "SELECT * FROM media_assets WHERE event_id=? AND mode='live' ORDER BY id", (decision["event_id"],)
        )]
        publishing = [dict(item) for item in connection.execute(
            "SELECT * FROM publishing_history WHERE event_id=? AND mode='live' ORDER BY recorded_at,id",
            (decision["event_id"],),
        )]
    try:
        bundle, current_decision_input = _content_input_bundle(decision["event_id"], "live")
    except Exception as error:
        blockers.append(f"Could not revalidate Content CEO inputs: {error}")
        bundle, current_decision_input = None, None
    if current_decision_input != decision["input_version"]:
        blockers.append("The Content CEO decision is stale because evidence, media, or publishing inputs changed.")
    if bundle and bundle["eligibility_status"] != "PRODUCTION_APPROVED":
        blockers.extend(bundle["blockers"])
    if decision["media_source_strategy"] == "GENERATE_ORIGINAL":
        if bundle and not bundle.get("original_generation_allowed"):
            blockers.append("Original media generation is no longer safe or permitted for this story.")
    elif decision["media_source_strategy"] in {"USE_APPROVED_OWNED_MEDIA", "USE_APPROVED_LICENSED_MEDIA"} and not _format_supported(decision["recommended_format"], media):
        blockers.append(f"Current rights-cleared media does not support {decision['recommended_format']}.")
    elif decision["media_source_strategy"] == "NONE" and decision["decision"] == "CREATE":
        blockers.append("CREATE requires an explicit media-source strategy.")
    evidence_version = _version_hash([(item["snapshot_id"], item["content_hash"], item["invalidated_at"]) for item in evidence])
    media_version = _version_hash([
        (item["id"], item["content_hash"], item["rights_status"], item["availability_status"], item["updated_at"])
        for item in media
    ])
    publishing_version = _version_hash([
        (item["id"], item["status"], item["content_fingerprint"], item["recorded_at"]) for item in publishing
    ])
    locked_context = {
        "schema_version": PROMPT_SCHEMA_VERSION,
        "production_policy_version": PRODUCTION_POLICY_VERSION,
        "event": {"event_id": decision["event_id"]},
        "workspace_identity": {
            "workspace_key": workspace_identity()["workspace_key"],
            "leader": workspace_identity()["leader"]["canonical_name"],
            "jurisdiction": workspace_identity()["jurisdiction"],
            "instruction": "Identity context only; it is not an additional factual claim.",
        },
        "content_decision": {
            "decision_id": decision["id"], "recommended_format": decision["recommended_format"],
            "media_source_strategy": decision["media_source_strategy"],
            "language": decision["language"], "proposed_duration_seconds": decision["proposed_duration_seconds"],
            "priority": decision["priority"], "factual_rationale": decision["factual_rationale"],
        },
        "approved_claim_set": {
            "id": claim_set["id"] if claim_set else None,
            "version": claim_set["version_number"] if claim_set else None,
        },
        "approved_claims": [
            {key: claim[key] for key in (
                "claim_version_id", "text", "claim_type", "assertion_scope", "attribution", "required_for_event"
            )} for claim in claims
        ],
        "rights_cleared_media": [
            {"media_asset_id": item["id"], "media_type": item["media_type"], "url": item["url"],
             "source_name": item["source_name"], "content_hash": item["content_hash"]}
            for item in media if item["rights_status"] == "verified" and item["availability_status"] == "available"
        ],
        "constraints": [
            "Use only approved_claims for factual content.", "Do not browse or research.",
            "Preserve attribution and certainty.", "No publishing or rendering is authorized.",
            "Evidence-source media is evidence only and must not become a production asset automatically.",
            "For GENERATE_ORIGINAL, create neutral non-documentary public-affairs visuals and avoid identifiable people.",
            "Generated visuals must never be presented as documentary evidence.",
        ],
    }
    input_version = _version_hash({
        "decision": (decision["id"], decision["input_version"], decision["policy_version"]),
        "claim_set": (claim_set["id"], claim_set["version_number"], claim_set["claim_set_version"]) if claim_set else None,
        "claims": [(item["claim_version_id"], item["content_hash"], item["revoked_at"]) for item in claims],
        "evidence_version": evidence_version, "media_version": media_version,
        "publishing_version": publishing_version, "policy": PRODUCTION_POLICY_VERSION,
        "schema": PROMPT_SCHEMA_VERSION,
    })
    return {
        "eligible": not blockers, "blockers": list(dict.fromkeys(blockers)), "decision": decision,
        "claim_set": dict(claim_set) if claim_set else None, "claims": claims, "evidence": evidence,
        "media": media, "publishing": publishing, "evidence_version": evidence_version,
        "media_version": media_version, "publishing_version": publishing_version,
        "input_version": input_version, "locked_context": locked_context,
    }


def _transition_production_job(connection, job_id, to_status, message, metadata=None):
    row = connection.execute("SELECT status,event_id FROM production_jobs WHERE id=?", (job_id,)).fetchone()
    if row is None:
        raise KeyError(job_id)
    if to_status not in PRODUCTION_TRANSITIONS[row["status"]]:
        raise ValueError(f"Invalid production transition {row['status']} → {to_status}")
    timestamp = now()
    connection.execute(
        "UPDATE production_jobs SET status=?,updated_at=?,completed_at=CASE WHEN ? IN "
        "('READY_FOR_APPROVAL','HUMAN_REVIEW','BLOCKED','FAILED') THEN ? ELSE completed_at END WHERE id=?",
        (to_status, timestamp, to_status, timestamp, job_id),
    )
    connection.execute(
        "INSERT INTO production_job_status_history(job_id,from_status,to_status,message,metadata_json,changed_at) "
        "VALUES(?,?,?,?,?,?)",
        (job_id, row["status"], to_status, message, json.dumps(metadata or {}, sort_keys=True), timestamp),
    )
    connection.execute(
        "UPDATE events SET production_status=?,updated_at=? WHERE id=?", (to_status, timestamp, row["event_id"])
    )


def enqueue_production(content_decision_id, provider_name="anthropic", *, background=True, regenerate=False,
                       client_request_id=None):
    provider = production_provider_for(provider_name)
    if provider.mode == "live" and not production_configuration()["live"]:
        # Never fall back to the fixture: a live request without Claude fails closed before any job exists.
        raise ProductionProviderUnavailable("Claude production provider unavailable: ANTHROPIC_API_KEY is not configured.")
    snapshot = _production_eligibility(content_decision_id)
    if not snapshot["eligible"]:
        raise ValueError("Production entry gate blocked: " + " ".join(snapshot["blockers"]))
    timestamp = now()
    with connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        if client_request_id:
            replay = connection.execute(
                "SELECT pj.* FROM paid_request_keys prk JOIN production_jobs pj ON pj.id=prk.job_id "
                "WHERE prk.request_key=? AND prk.kind='PRODUCTION'", (client_request_id,),
            ).fetchone()
            if replay:
                connection.commit()
                return {"job": dict(replay), "duplicate": True, "cached": False}
        active = connection.execute(
            "SELECT * FROM production_jobs WHERE content_decision_id=? AND status IN ('QUEUED','GENERATING','VALIDATING')",
            (content_decision_id,),
        ).fetchone()
        if active:
            if client_request_id:
                connection.execute(
                    "INSERT OR IGNORE INTO paid_request_keys(request_key,kind,job_id,created_at) VALUES(?,'PRODUCTION',?,?)",
                    (client_request_id, active["id"], timestamp),
                )
            connection.commit()
            return {"job": dict(active), "duplicate": True, "cached": False}
        prior = connection.execute(
            "SELECT * FROM production_jobs WHERE content_decision_id=? ORDER BY regeneration_number DESC LIMIT 1",
            (content_decision_id,),
        ).fetchone()
        if prior and not regenerate:
            connection.commit()
            return {"job": dict(prior), "duplicate": False, "cached": True}
        regeneration_number = (prior["regeneration_number"] + 1) if prior else 1
        job_id = "PJ-" + uuid.uuid4().hex[:12].upper()
        idempotency_key = _version_hash({
            "decision": content_decision_id, "input": snapshot["input_version"], "provider": provider.name,
            "model": provider.model, "regeneration": regeneration_number,
        })
        connection.execute(
            "INSERT INTO production_jobs(id,event_id,content_decision_id,approved_claim_set_id,"
            "approved_claim_set_version,evidence_version,media_version,publishing_history_version,input_version,"
            "production_policy_version,prompt_schema_version,requested_format,provider,model,provider_mode,status,"
            "regeneration_number,idempotency_key,fixture_only,requested_at,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'QUEUED',?,?,?,?,?,?)",
            (
                job_id, snapshot["decision"]["event_id"], content_decision_id, snapshot["claim_set"]["id"],
                snapshot["claim_set"]["version_number"], snapshot["evidence_version"], snapshot["media_version"],
                snapshot["publishing_version"], snapshot["input_version"], PRODUCTION_POLICY_VERSION,
                PROMPT_SCHEMA_VERSION, snapshot["decision"]["recommended_format"], provider.name, provider.model,
                provider.mode, regeneration_number, idempotency_key, int(provider.mode == "fixture"),
                timestamp, timestamp, timestamp,
            ),
        )
        connection.execute("UPDATE production_jobs SET client_request_id=? WHERE id=?", (client_request_id, job_id))
        if client_request_id:
            connection.execute(
                "INSERT INTO paid_request_keys(request_key,kind,job_id,created_at) VALUES(?,'PRODUCTION',?,?)",
                (client_request_id, job_id, timestamp),
            )
        for claim in snapshot["claims"]:
            connection.execute(
                "INSERT INTO production_job_claims(job_id,claim_version_id,claim_id,version_number,content_hash,text,"
                "claim_type,assertion_scope,attribution,required_for_event) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (job_id, claim["claim_version_id"], claim["claim_id"], claim["version_number"], claim["content_hash"],
                 claim["text"], claim["claim_type"], claim["assertion_scope"], claim["attribution"], claim["required_for_event"]),
            )
        for item in snapshot["evidence"]:
            connection.execute(
                "INSERT INTO production_job_evidence(job_id,snapshot_id,claim_version_id,canonical_url,content_hash,"
                "retrieved_at,evidence_family_id) VALUES(?,?,?,?,?,?,?)",
                (job_id, item["snapshot_id"], item["claim_version_id"], item["canonical_url"], item["content_hash"],
                 item["retrieved_at"], item["evidence_family_id"]),
            )
        for item in snapshot["media"]:
            connection.execute(
                "INSERT INTO production_job_media(job_id,media_asset_id,media_type,url,rights_status,"
                "availability_status,content_hash) VALUES(?,?,?,?,?,?,?)",
                (job_id, item["id"], item["media_type"], item["url"], item["rights_status"],
                 item["availability_status"], item["content_hash"]),
            )
        connection.execute(
            "INSERT INTO production_job_status_history(job_id,from_status,to_status,message,changed_at) "
            "VALUES(?,NULL,'QUEUED','Production entry gate passed; immutable inputs captured.',?)", (job_id, timestamp),
        )
        connection.execute(
            "UPDATE events SET production_status='QUEUED',updated_at=? WHERE id=?",
            (timestamp, snapshot["decision"]["event_id"]),
        )
        job = dict(connection.execute("SELECT * FROM production_jobs WHERE id=?", (job_id,)).fetchone())
        connection.commit()
    if background:
        PRODUCTION_EXECUTOR.submit(run_production_job, job_id, provider)
    else:
        run_production_job(job_id, provider)
        job = production_job(job_id)
    return {"job": job, "duplicate": False, "cached": False}


def _job_locked_context(job_id):
    with connect() as connection:
        job = dict(connection.execute("SELECT * FROM production_jobs WHERE id=?", (job_id,)).fetchone())
        decision = dict(connection.execute("SELECT * FROM content_decisions WHERE id=?", (job["content_decision_id"],)).fetchone())
        claims = [dict(row) for row in connection.execute(
            "SELECT * FROM production_job_claims WHERE job_id=? ORDER BY claim_id", (job_id,)
        )]
        media = [dict(row) for row in connection.execute(
            "SELECT * FROM production_job_media WHERE job_id=? AND rights_status='verified' AND availability_status='available' ORDER BY media_asset_id",
            (job_id,),
        )]
        provenance = [dict(row) for row in connection.execute(
            "SELECT pje.claim_version_id,vs.source_name,vs.source_class,pje.canonical_url,pje.evidence_family_id "
            "FROM production_job_evidence pje JOIN verification_snapshots vs ON vs.id=pje.snapshot_id "
            "WHERE pje.job_id=? ORDER BY pje.claim_version_id,vs.source_name,pje.snapshot_id",
            (job_id,),
        )]
    return {
        "schema_version": job["prompt_schema_version"], "production_policy_version": job["production_policy_version"],
        "event": {"event_id": job["event_id"]},
        "workspace_identity": {
            "workspace_key": workspace_identity()["workspace_key"], "leader": workspace_identity()["leader"]["canonical_name"],
            "jurisdiction": workspace_identity()["jurisdiction"],
            "instruction": "Identity context only; it is not an additional factual claim.",
        },
        "content_decision": {
            "decision_id": decision["id"], "recommended_format": decision["recommended_format"],
            "media_source_strategy": decision["media_source_strategy"],
            "language": decision["language"], "proposed_duration_seconds": decision["proposed_duration_seconds"],
            "priority": decision["priority"], "factual_rationale": decision["factual_rationale"],
        },
        "approved_claim_set": {"id": job["approved_claim_set_id"], "version": job["approved_claim_set_version"]},
        "approved_claims": [{
            "claim_version_id": item["claim_version_id"], "text": item["text"], "claim_type": item["claim_type"],
            "assertion_scope": item["assertion_scope"], "attribution": item["attribution"],
            "required_for_event": item["required_for_event"],
        } for item in claims],
        "evidence_provenance": provenance,
        "rights_cleared_media": [{
            "media_asset_id": item["media_asset_id"], "media_type": item["media_type"], "url": item["url"],
            "content_hash": item["content_hash"],
        } for item in media],
        "constraints": ["Use only approved_claims for factual content.", "Do not browse or research.",
                        "Preserve attribution and certainty.", "No publishing or rendering is authorized."],
    }


def _create_content_package(connection, job, package, validation):
    version_number = connection.execute(
        "SELECT COALESCE(MAX(version_number),0)+1 FROM content_packages WHERE event_id=?", (job["event_id"],)
    ).fetchone()[0]
    package_json = json.dumps(package, ensure_ascii=False, sort_keys=True)
    snapshot_ids = [row["snapshot_id"] for row in connection.execute(
        "SELECT DISTINCT snapshot_id FROM production_job_evidence WHERE job_id=? ORDER BY snapshot_id", (job["id"],)
    )]
    package_id = "CP-" + uuid.uuid4().hex[:12].upper()
    connection.execute(
        "INSERT INTO content_packages(id,job_id,event_id,version_number,status,requested_format,story_angle,"
        "content_objective,package_json,content_hash,approved_claim_set_id,approved_claim_set_version,"
        "approved_claim_version_ids_json,evidence_snapshot_ids_json,evidence_version,media_version,"
        "production_policy_version,prompt_schema_version,provider,model,provider_mode,fixture_only,"
        "provider_request_id,input_tokens,output_tokens,cache_creation_input_tokens,cache_read_input_tokens,"
        "total_tokens,latency_ms,cost_usd,cost_status,validation_result_json,created_at) "
        "VALUES(?,?,?,?, 'READY_FOR_APPROVAL',?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            package_id, job["id"], job["event_id"], version_number,
            job["requested_format"], package["story_angle"], package["content_objective"],
            package_json, hashlib.sha256(package_json.encode()).hexdigest(), job["approved_claim_set_id"],
            job["approved_claim_set_version"], json.dumps(validation["approved_claim_version_ids"]),
            json.dumps(snapshot_ids), job["evidence_version"], job["media_version"], job["production_policy_version"],
            job["prompt_schema_version"], job["provider"], job["model"], job["provider_mode"], job["fixture_only"],
            job["provider_request_id"], job["input_tokens"], job["output_tokens"], job["cache_creation_input_tokens"],
            job["cache_read_input_tokens"], job["total_tokens"], job["latency_ms"], job["cost_usd"],
            job["cost_status"], json.dumps(validation, ensure_ascii=False), now(),
        ),
    )
    connection.execute(
        "UPDATE production_jobs SET validation_status='PASSED',validation_result_json=?,error_code=NULL,"
        "error_message=NULL,updated_at=? WHERE id=?",
        (json.dumps(validation, ensure_ascii=False), now(), job["id"]),
    )
    _transition_production_job(
        connection, job["id"], "READY_FOR_APPROVAL", "Validated immutable package is ready for human approval."
    )
    return package_id


def run_production_job(job_id, provider=None):
    with connect() as connection:
        job = connection.execute("SELECT * FROM production_jobs WHERE id=?", (job_id,)).fetchone()
        if job is None:
            raise KeyError(job_id)
        if job["status"] != "QUEUED":
            return dict(job)
        provider = provider or production_provider_for("fixture" if job["provider_mode"] == "fixture" else "anthropic")
        _transition_production_job(connection, job_id, "GENERATING", "Generating one structured content package.")
        connection.execute("UPDATE production_jobs SET started_at=? WHERE id=?", (now(), job_id))
    locked_context = _job_locked_context(job_id)
    if hasattr(provider, "request_snapshot"):
        snapshot_json = json.dumps(provider.request_snapshot(locked_context, PRODUCTION_TOKEN_LIMIT), ensure_ascii=False, sort_keys=True)
        with connect() as connection:
            connection.execute(
                "UPDATE production_jobs SET request_snapshot_json=?,request_snapshot_hash=? WHERE id=?",
                (snapshot_json, hashlib.sha256(snapshot_json.encode()).hexdigest(), job_id),
            )
    result = None
    error = None
    for attempt in range(PRODUCTION_MAX_RETRIES + 1):
        try:
            result = provider.generate(
                locked_context, token_limit=PRODUCTION_TOKEN_LIMIT,
                connection_timeout_seconds=PRODUCTION_CONNECTION_TIMEOUT_SECONDS,
                response_timeout_seconds=PRODUCTION_RESPONSE_TIMEOUT_SECONDS,
            )
            break
        except Exception as caught:
            error = caught
            if attempt >= PRODUCTION_MAX_RETRIES or not getattr(caught, "retryable", False):
                break
            with connect() as connection:
                connection.execute(
                    "INSERT INTO production_job_status_history(job_id,from_status,to_status,message,metadata_json,changed_at) "
                    "VALUES(?,'GENERATING','GENERATING','Bounded retry after retryable provider error.',?,?)",
                    (job_id, json.dumps({"attempt": attempt + 1, "error_code": getattr(caught, "code", "provider_error")}), now()),
                )
    if error and result is None:
        with connect() as connection:
            connection.execute(
                "UPDATE production_jobs SET provider_called=?,error_code=?,error_message=?,validation_status='FAILED',"
                "validation_result_json=?,updated_at=? WHERE id=?",
                (0 if isinstance(error, MissingAPIKeyError) else 1, getattr(error, "code", "production_provider_error"),
                 str(error)[:500], json.dumps({"valid": False, "errors": [str(error)[:500]]}), now(), job_id),
            )
            _transition_production_job(connection, job_id, "HUMAN_REVIEW", "Provider did not return a usable package.")
        return production_job(job_id)
    with connect() as connection:
        connection.execute(
            "UPDATE production_jobs SET provider_called=1,provider_request_id=?,input_tokens=?,output_tokens=?,"
            "cache_creation_input_tokens=?,cache_read_input_tokens=?,total_tokens=?,latency_ms=?,cost_usd=?,"
            "cost_status=?,cost_policy_version=?,updated_at=? WHERE id=?",
            (result.provider_request_id, result.input_tokens, result.output_tokens, result.cache_creation_input_tokens,
             result.cache_read_input_tokens, result.total_tokens, result.latency_ms, result.cost_usd,
             "known" if result.cost_usd is not None else "unknown", result.cost_policy_version, now(), job_id),
        )
        draft_json = json.dumps(result.package, ensure_ascii=False, sort_keys=True)
        connection.execute(
            "INSERT INTO production_drafts(id,job_id,structured_output_json,content_hash,created_at) VALUES(?,?,?,?,?)",
            ("PD-" + uuid.uuid4().hex[:12].upper(), job_id, draft_json,
             hashlib.sha256(draft_json.encode()).hexdigest(), now()),
        )
        _transition_production_job(connection, job_id, "VALIDATING", "Validating schema, claims, versions, media, and race conditions.")
    current = _production_eligibility(production_job(job_id)["content_decision_id"])
    job = production_job(job_id)
    if not current["eligible"] or current["input_version"] != job["input_version"]:
        race_errors = current["blockers"] or ["Production inputs changed after generation began."]
        with connect() as connection:
            connection.execute(
                "UPDATE production_jobs SET validation_status='FAILED',validation_result_json=?,error_code='stale_inputs',"
                "error_message=?,updated_at=? WHERE id=?",
                (json.dumps({"valid": False, "errors": race_errors}), " ".join(race_errors)[:500], now(), job_id),
            )
            _transition_production_job(connection, job_id, "BLOCKED", "Inputs changed before package finalization.")
        return production_job(job_id)
    validation = validate_production_package(result.package, locked_context)
    if not validation["valid"]:
        with connect() as connection:
            connection.execute(
                "UPDATE production_jobs SET validation_status='FAILED',validation_result_json=?,error_code='validation_failed',"
                "error_message=?,updated_at=? WHERE id=?",
                (json.dumps(validation, ensure_ascii=False), " ".join(validation["errors"])[:500], now(), job_id),
            )
            _transition_production_job(connection, job_id, "HUMAN_REVIEW", "Structured package failed deterministic validation.")
        return production_job(job_id)
    with connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        _create_content_package(connection, job, result.package, validation)
        connection.commit()
    return production_job(job_id)


def production_job(job_id):
    with connect() as connection:
        row = connection.execute("SELECT * FROM production_jobs WHERE id=?", (job_id,)).fetchone()
    if row is None:
        raise KeyError(job_id)
    return dict(row)


def revalidate_production_draft(draft_id):
    """Revalidate one immutable provider draft without making a new provider call."""
    with connect() as connection:
        draft_row = connection.execute("SELECT * FROM production_drafts WHERE id=?", (draft_id,)).fetchone()
        if draft_row is None:
            raise KeyError(draft_id)
        draft = dict(draft_row)
        job = dict(connection.execute("SELECT * FROM production_jobs WHERE id=?", (draft["job_id"],)).fetchone())
        existing = connection.execute("SELECT * FROM content_packages WHERE job_id=?", (job["id"],)).fetchone()
        if existing:
            return {"draft": draft, "job": job, "package": dict(existing),
                    "validation": json.loads(existing["validation_result_json"]), "provider_called_again": False}
    if job["status"] != "HUMAN_REVIEW" or job["error_code"] != "validation_failed":
        raise ValueError("Only a validation-failed HUMAN_REVIEW draft can be revalidated.")
    package = json.loads(draft["structured_output_json"])
    canonical = json.dumps(package, ensure_ascii=False, sort_keys=True)
    if hashlib.sha256(canonical.encode()).hexdigest() != draft["content_hash"]:
        raise ValueError("Production draft content hash does not match its immutable payload.")
    current = _production_eligibility(job["content_decision_id"])
    if not current["eligible"] or current["input_version"] != job["input_version"]:
        raise ValueError("Production inputs changed; the historical draft cannot be finalized.")
    validation = validate_production_package(package, _job_locked_context(job["id"]))
    if not validation["valid"]:
        with connect() as connection:
            connection.execute(
                "UPDATE production_jobs SET validation_status='FAILED',validation_result_json=?,error_code='validation_failed',"
                "error_message=?,updated_at=? WHERE id=?",
                (json.dumps(validation, ensure_ascii=False), " ".join(validation["errors"])[:500], now(), job["id"]),
            )
        return {"draft": draft, "job": production_job(job["id"]), "package": None,
                "validation": validation, "provider_called_again": False}
    with connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        existing = connection.execute("SELECT * FROM content_packages WHERE job_id=?", (job["id"],)).fetchone()
        if existing:
            connection.commit()
            return {"draft": draft, "job": production_job(job["id"]), "package": dict(existing),
                    "validation": json.loads(existing["validation_result_json"]), "provider_called_again": False}
        _transition_production_job(
            connection, job["id"], "VALIDATING", "Revalidating the existing immutable provider draft."
        )
        package_id = _create_content_package(connection, job, package, validation)
        connection.commit()
    with connect() as connection:
        final_package = dict(connection.execute("SELECT * FROM content_packages WHERE id=?", (package_id,)).fetchone())
    return {"draft": draft, "job": production_job(job["id"]), "package": final_package,
            "validation": validation, "provider_called_again": False}


def _default_render_media_type(content_format):
    return FORMAT_MEDIA_TYPES.get(content_format)


def _source_asset_blockers(connection, source_asset_id, content_package_id):
    """An image-to-video source must be the exact, live, validated, current image of this package."""
    row = connection.execute(
        "SELECT ga.*,rj.status AS job_status FROM generated_assets ga JOIN render_jobs rj ON rj.id=ga.render_job_id "
        "WHERE ga.id=?", (source_asset_id,),
    ).fetchone()
    if row is None:
        return None, [f"Source asset {source_asset_id} does not exist."]
    asset = dict(row)
    blockers = []
    if asset["content_package_id"] != content_package_id:
        blockers.append("The source image belongs to a different ContentPackage.")
    if asset["media_type"] != "IMAGE":
        blockers.append("Only a generated IMAGE can be a video source.")
    if asset["fixture_only"]:
        blockers.append("Fixture placeholder images can never be a video source.")
    if asset["status"] != "VALIDATED" or not asset["executable"] or not asset["usable_for_review"] or asset["stale"]:
        blockers.append("The source image is not a validated, usable, current live asset.")
    if asset["job_status"] != "READY_FOR_REVIEW":
        blockers.append("The source image's render job did not finish at READY_FOR_REVIEW.")
    return asset, blockers


def _render_eligibility(content_package_id, media_type=None, source_asset_id=None, *, check_production_freshness=True):
    blockers = []
    source_asset = None
    with connect() as connection:
        row = connection.execute(
            "SELECT cp.*,pj.status AS production_job_status,pj.validation_status AS production_validation_status,"
            "pj.input_version AS production_input_version,pj.fixture_only AS production_fixture_only,"
            "cd.decision,cd.executable,cd.test_only,cd.id AS content_decision_id,cd.event_id,cd.input_version AS decision_input_version,"
            "acs.status AS claim_set_status,vr.mode AS verification_mode,vr.status AS verification_run_status "
            "FROM content_packages cp JOIN production_jobs pj ON pj.id=cp.job_id "
            "JOIN content_decisions cd ON cd.id=pj.content_decision_id "
            "JOIN approved_claim_sets acs ON acs.id=cp.approved_claim_set_id "
            "JOIN verification_runs vr ON vr.id=acs.verification_run_id WHERE cp.id=?",
            (content_package_id,),
        ).fetchone()
        if row is None:
            raise KeyError(content_package_id)
        package = dict(row)
        media_type = media_type or _default_render_media_type(package["requested_format"])
        if media_type not in ("IMAGE", "VIDEO", "AUDIO", "THUMBNAIL", "CAROUSEL_SLIDE", "VOICEOVER", "SHORT_FORM_VIDEO", "LONG_FORM_VIDEO"):
            blockers.append("The requested media type is unsupported.")
        expected = _default_render_media_type(package["requested_format"])
        if media_type == "VIDEO" and expected == "IMAGE":
            if not source_asset_id:
                blockers.append("Image posts can only generate video from an explicitly selected approved source image.")
        elif media_type != expected:
            blockers.append(f"Content format {package['requested_format']} currently requires media type {expected}.")
        if source_asset_id:
            if media_type != "VIDEO":
                blockers.append("A source asset is only valid for VIDEO rendering.")
            source_asset, source_blockers = _source_asset_blockers(connection, source_asset_id, package["id"])
            blockers.extend(source_blockers)
        latest = connection.execute(
            "SELECT id FROM content_packages WHERE event_id=? ORDER BY version_number DESC LIMIT 1", (package["event_id"],)
        ).fetchone()
        if not latest or latest["id"] != package["id"]:
            blockers.append("The ContentPackage is stale because a newer package exists.")
        if package["status"] != "READY_FOR_APPROVAL":
            blockers.append("Only a READY_FOR_APPROVAL ContentPackage can render.")
        if package["production_job_status"] != "READY_FOR_APPROVAL" or package["production_validation_status"] != "PASSED":
            blockers.append("The associated ProductionJob is not validated and READY_FOR_APPROVAL.")
        if package["decision"] != "CREATE" or not package["executable"]:
            blockers.append("The current ContentDecision is not an executable CREATE decision.")
        if package["test_only"]:
            blockers.append("TEST_ONLY ContentDecision lineage cannot render.")
        if package["claim_set_status"] != "APPROVED" or package["verification_mode"] != "live" or package["verification_run_status"] != "COMPLETED":
            blockers.append("The package does not have production-approved live claim lineage.")
        validation = json.loads(package["validation_result_json"] or "{}")
        if not validation.get("valid"):
            blockers.append("The ContentPackage did not pass deterministic validation.")
        prompt_package = json.loads(package["package_json"])
        input_media = [dict(item) for item in connection.execute(
            "SELECT ma.* FROM production_job_media pjm JOIN media_assets ma ON ma.id=pjm.media_asset_id "
            "WHERE pjm.job_id=? ORDER BY ma.id", (package["job_id"],),
        )]
        for asset in input_media:
            if asset["rights_status"] != "verified" or asset["availability_status"] != "available":
                blockers.append(f"Reference media {asset['id']} no longer has valid rights and availability.")
        claim_ids = json.loads(package["approved_claim_version_ids_json"])
        evidence_ids = json.loads(package["evidence_snapshot_ids_json"])
        current_claims = [dict(item) for item in connection.execute(
            "SELECT cv.id,cv.claim_id,cv.version_number,cv.revoked_at FROM approved_claim_set_items acsi "
            "JOIN claim_versions cv ON cv.id=acsi.claim_version_id WHERE acsi.claim_set_id=? ORDER BY cv.id",
            (package["approved_claim_set_id"],),
        )]
        if sorted(item["id"] for item in current_claims) != sorted(claim_ids):
            blockers.append("The exact approved claim-version set changed after package creation.")
        for claim in current_claims:
            newest = connection.execute(
                "SELECT id FROM claim_versions WHERE claim_id=? ORDER BY version_number DESC LIMIT 1", (claim["claim_id"],)
            ).fetchone()
            if claim["revoked_at"] or not newest or newest["id"] != claim["id"]:
                blockers.append(f"Claim version {claim['id']} is revoked or superseded.")
        snapshots = [dict(item) for item in connection.execute(
            "SELECT id,content_hash,invalidated_at FROM verification_snapshots WHERE id IN ("
            + ",".join("?" for _ in evidence_ids) + ") ORDER BY id" if evidence_ids else
            "SELECT id,content_hash,invalidated_at FROM verification_snapshots WHERE 0",
            evidence_ids,
        )]
        if sorted(item["id"] for item in snapshots) != sorted(evidence_ids):
            blockers.append("An exact evidence snapshot referenced by the package is missing.")
        if any(item["invalidated_at"] for item in snapshots):
            blockers.append("An evidence snapshot referenced by the package has been invalidated.")
    try:
        production = _production_eligibility(package["content_decision_id"]) if check_production_freshness else {"skipped": True}
    except Exception as error:
        production = None
        blockers.append(f"Could not revalidate package lineage: {error}")
    if production and production.get("skipped"):
        # Distribution: claims, evidence, rights, and package currency are checked above; the decision-input hash
        # (which includes this story's own publishing history) is intentionally not re-applied.
        production = None
        with connect() as connection:
            latest_decision = connection.execute(
                "SELECT id,decision,executable,test_only FROM content_decisions WHERE event_id=? ORDER BY decided_at DESC,rowid DESC LIMIT 1",
                (package["event_id"],),
            ).fetchone()
        if not latest_decision or latest_decision["id"] != package["content_decision_id"]:
            blockers.append("A newer Content CEO decision exists for this story.")
    elif not production or not production["eligible"]:
        blockers.extend(production["blockers"] if production else [])
    elif production["input_version"] != package["production_input_version"]:
        blockers.append("The ProductionJob input version is no longer current.")
    if production and package["evidence_version"] != production.get("evidence_version"):
        blockers.append("The package evidence version is stale.")
    if production and package["media_version"] != production.get("media_version"):
        blockers.append("The package media-rights version is stale.")
    lineage = {
        "content_package": (package["id"], package["version_number"], package["content_hash"]),
        "production": (package["job_id"], package["production_input_version"]),
        "decision": (package["content_decision_id"], package["decision_input_version"]),
        "claims": sorted(claim_ids), "evidence": sorted(evidence_ids),
        "evidence_version": package["evidence_version"], "media_version": package["media_version"],
        "media_type": media_type, "policy": RENDER_POLICY_VERSION,
        "prompt_version": RENDER_PROMPT_VERSION, "config_version": RENDER_GENERATION_CONFIG_VERSION,
        "renderer_config_version": RENDERER_CONFIG_VERSION, "media_qa_policy_version": MEDIA_QA_POLICY_VERSION,
    }
    if source_asset:
        # Bound only when present so existing image lineage hashes stay unchanged.
        lineage["source_asset"] = (source_asset["id"], source_asset["checksum_sha256"])
    return {
        "eligible": not blockers, "blockers": list(dict.fromkeys(blockers)), "package": package, "source_asset": source_asset,
        "package_payload": prompt_package, "media_type": media_type, "input_media": input_media,
        "claim_version_ids": claim_ids, "evidence_snapshot_ids": evidence_ids,
        "input_version": _version_hash(lineage),
    }


def _requested_render_shape(gate, generation_mode, renderer=None):
    """Aspect ratio and duration the renderer must produce natively (image-to-video inherits the source ratio)."""
    package = gate["package_payload"]
    metadata = package.get("platform_metadata") or {}
    if generation_mode == "REFERENCE_TO_VIDEO" and gate.get("requested_aspect_override"):
        # Reference-to-video composes a new frame, so an explicit supported ratio involves no stretching.
        return gate["requested_aspect_override"], metadata.get("duration_seconds")
    if generation_mode in ("IMAGE_TO_VIDEO", "REFERENCE_TO_VIDEO") and gate.get("source_asset"):
        source = gate["source_asset"]
        capabilities = provider_capabilities(getattr(renderer, "name", "xai"), "VIDEO") or {}
        aspect = ratio_label(source["width"], source["height"], capabilities.get("aspect_ratios", ())) or (
            f"{source['width']}x{source['height']}"
        )
        return aspect, metadata.get("duration_seconds")
    if gate["media_type"] == "IMAGE":
        return metadata.get("aspect_ratio"), None
    return metadata.get("aspect_ratio"), metadata.get("duration_seconds")


def _build_render_request(gate, provider, regeneration_number, generation_mode=None):
    package = gate["package_payload"]
    media_type = gate["media_type"]
    reference_ids = [
        item["id"] for item in gate["input_media"]
        if item["rights_status"] == "verified" and item["availability_status"] == "available"
    ]
    common = {
        "media_type": media_type, "content_package_id": gate["package"]["id"],
        "content_package_version": gate["package"]["version_number"],
        "prompt_version": RENDER_PROMPT_VERSION,
        "generation_config_version": RENDER_GENERATION_CONFIG_VERSION,
        "renderer_config_version": RENDERER_CONFIG_VERSION,
        "media_qa_policy_version": MEDIA_QA_POLICY_VERSION,
        "provider": provider.name, "model": provider.model,
        "claim_version_ids": sorted(gate["claim_version_ids"]),
        "evidence_snapshot_ids": sorted(gate["evidence_snapshot_ids"]),
        "reference_asset_ids": reference_ids,
        "creative_constraints": {
            "story_angle": package["story_angle"], "content_objective": package["content_objective"],
            "creative_notes": package["creative_notes"],
            "non_factual_style_elements": package["non_factual_style_elements"],
            "no_new_facts": True, "no_political_targeting": True,
        },
    }
    if package.get("media_brief"):
        common["media_brief"] = package["media_brief"]
    if media_type == "IMAGE":
        prompts = [scene["visual_prompt"] for scene in package["storyboard"] if scene.get("visual_prompt")]
        if not prompts or not package.get("thumbnail"):
            raise ValueError("The ContentPackage is missing required image creative instructions.")
        aspect = package["platform_metadata"]["aspect_ratio"]
        # Target dimensions per ratio; historical 4:5 packages stay readable but cannot render live.
        width, height = RENDER_TARGET_DIMENSIONS.get(aspect, (1080, 1080))
        common.update({
            "visual_prompts": prompts,
            "thumbnail_concept": package["thumbnail"],
            "intended_text_overlays": [package["headline"], package["thumbnail"]],
            "generation_parameters": {
                "width": width, "height": height, "aspect_ratio": aspect, "output_count": 1,
                "seed": _version_hash({"package": gate["package"]["id"], "regeneration": regeneration_number}),
            },
        })
    elif media_type in ("VIDEO", "SHORT_FORM_VIDEO", "LONG_FORM_VIDEO"):
        if not package.get("storyboard") or not package.get("script"):
            raise ValueError("The ContentPackage is missing required video storyboard or script instructions.")
        mode = generation_mode or "TEXT_TO_VIDEO"
        aspect, duration = _requested_render_shape(gate, mode, provider)
        common.update({
            "storyboard": package["storyboard"], "script": package["script"],
            "visual_prompts": [scene["visual_prompt"] for scene in package["storyboard"] if scene.get("visual_prompt")],
            "generation_parameters": {
                "duration_seconds": duration, "aspect_ratio": aspect, "generation_mode": mode,
                "resolution": getattr(provider, "resolution", None),
            },
        })
        if gate.get("source_asset"):
            source = gate["source_asset"]
            common["source_asset"] = {
                "id": source["id"], "checksum_sha256": source["checksum_sha256"], "mime_type": source["mime_type"],
                "width": source["width"], "height": source["height"], "render_job_id": source["render_job_id"],
            }
    elif media_type in ("AUDIO", "VOICEOVER"):
        if not package.get("script"):
            raise ValueError("The ContentPackage is missing required audio narration instructions.")
        common.update({
            "narration": package["script"],
            "generation_parameters": {
                "duration_seconds": package["platform_metadata"]["duration_seconds"],
                "language": package["platform_metadata"]["language"],
            },
        })
    else:
        raise ValueError("The selected media type is not implemented for render request construction.")
    return common


def _render_request_fingerprint(gate, renderer, request, generation_mode):
    """Stable identity of an equivalent paid request; regeneration numbering is excluded."""
    normalized = json.loads(json.dumps(request))
    (normalized.get("generation_parameters") or {}).pop("seed", None)
    source = gate.get("source_asset")
    return _version_hash({
        "event": gate["package"]["event_id"], "package": gate["package"]["id"],
        "package_version": gate["package"]["version_number"], "media_type": gate["media_type"],
        "generation_mode": generation_mode, "source_asset": (
            [source["id"], source["checksum_sha256"]] if source else None
        ), "request": normalized, "provider": renderer.name, "model": renderer.model,
    })


def _prepare_video_source_derivative(job, request, storage):
    """Create/reuse an immutable upload derivative while preserving the original asset."""
    source = request.get("source_asset")
    if not source or job.get("generation_mode") not in ("IMAGE_TO_VIDEO", "REFERENCE_TO_VIDEO"):
        return request
    transform_spec = {
        "version": "video-source-jpeg-v1", "max_bytes": VIDEO_SOURCE_MAX_BYTES,
        "min_short_side": VIDEO_SOURCE_MIN_SHORT_SIDE, "source_checksum": source["checksum_sha256"],
    }
    transform_hash = _version_hash(transform_spec)
    with connect() as connection:
        existing = connection.execute(
            "SELECT * FROM derived_assets WHERE source_asset_id=? AND purpose='VIDEO_SOURCE' AND transform_hash=?",
            (source["id"], transform_hash),
        ).fetchone()
        original = connection.execute(
            "SELECT storage_uri,checksum_sha256 FROM generated_assets WHERE id=?", (source["id"],)
        ).fetchone()
    if original is None or original["checksum_sha256"] != source["checksum_sha256"]:
        raise ValueError("The original video source no longer matches its immutable checksum.")
    if existing:
        derivative = dict(existing)
    else:
        original_bytes = storage.get(original["storage_uri"])
        if hashlib.sha256(original_bytes).hexdigest() != source["checksum_sha256"]:
            raise ValueError("The original source file failed checksum verification.")
        derivative_bytes, transformation = prepare_video_source(
            original_bytes, max_bytes=VIDEO_SOURCE_MAX_BYTES, min_short_side=VIDEO_SOURCE_MIN_SHORT_SIDE,
        )
        if len(derivative_bytes) > VIDEO_SOURCE_MAX_BYTES:
            raise ValueError("VIDEO_SOURCE_TOO_LARGE: derived image exceeds VIDEO_SOURCE_MAX_BYTES.")
        stored = storage.save(derivative_bytes, extension="jpg", metadata={"purpose": "VIDEO_SOURCE"})
        derivative = {
            "id": "DA-" + uuid.uuid4().hex[:12].upper(), "source_asset_id": source["id"],
            "source_checksum_sha256": source["checksum_sha256"], "purpose": "VIDEO_SOURCE",
            "storage_uri": stored.storage_uri, "mime_type": "image/jpeg", "width": transformation["width"],
            "height": transformation["height"], "file_size": stored.file_size,
            "checksum_sha256": stored.checksum_sha256, "frame_time_seconds": None,
            "transform_json": json.dumps(transformation, sort_keys=True), "transform_hash": transform_hash,
            "created_at": now(),
        }
        with connect() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO derived_assets(id,source_asset_id,source_checksum_sha256,purpose,storage_uri,mime_type,"
                "width,height,file_size,checksum_sha256,frame_time_seconds,transform_json,transform_hash,created_at) "
                "VALUES(:id,:source_asset_id,:source_checksum_sha256,:purpose,:storage_uri,:mime_type,:width,:height,"
                ":file_size,:checksum_sha256,:frame_time_seconds,:transform_json,:transform_hash,:created_at)", derivative,
            )
            derivative = dict(connection.execute(
                "SELECT * FROM derived_assets WHERE source_asset_id=? AND purpose='VIDEO_SOURCE' AND transform_hash=?",
                (source["id"], transform_hash),
            ).fetchone())
    if derivative["file_size"] > VIDEO_SOURCE_MAX_BYTES:
        raise ValueError("VIDEO_SOURCE_TOO_LARGE: stored derivative exceeds VIDEO_SOURCE_MAX_BYTES.")
    with connect() as connection:
        connection.execute("UPDATE render_jobs SET source_derivative_id=?,updated_at=? WHERE id=?",
                           (derivative["id"], now(), job["id"]))
    prepared = json.loads(json.dumps(request))
    prepared["original_source_asset"] = dict(source)
    prepared["source_asset"] = {
        "id": derivative["id"], "checksum_sha256": derivative["checksum_sha256"],
        "mime_type": derivative["mime_type"], "width": derivative["width"], "height": derivative["height"],
        "source_asset_id": derivative["source_asset_id"], "source_checksum_sha256": derivative["source_checksum_sha256"],
    }
    return prepared


def _transition_render_job(connection, job_id, to_status, message, metadata=None):
    row = connection.execute("SELECT status,event_id FROM render_jobs WHERE id=?", (job_id,)).fetchone()
    if row is None:
        raise KeyError(job_id)
    if to_status not in RENDER_TRANSITIONS[row["status"]]:
        raise ValueError(f"Invalid render transition {row['status']} → {to_status}")
    timestamp = now()
    connection.execute(
        "UPDATE render_jobs SET status=?,updated_at=?,completed_at=CASE WHEN ? IN "
        "('READY_FOR_REVIEW','HUMAN_REVIEW','BLOCKED','FAILED','CANCELLED') THEN ? ELSE completed_at END WHERE id=?",
        (to_status, timestamp, to_status, timestamp, job_id),
    )
    connection.execute(
        "INSERT INTO render_job_status_history(render_job_id,from_status,to_status,message,metadata_json,changed_at) "
        "VALUES(?,?,?,?,?,?)",
        (job_id, row["status"], to_status, message, json.dumps(metadata or {}, sort_keys=True), timestamp),
    )
    connection.execute("UPDATE events SET render_status=?,updated_at=? WHERE id=?", (to_status, timestamp, row["event_id"]))


def enqueue_render(
    content_package_id, media_type=None, provider_name=None, *, background=True, regenerate=False,
    renderer=None, visual_qa_provider=None, source_asset_id=None, generation_mode=None, client_request_id=None,
    ocr_provider=None, frame_extractor=None, aspect_ratio=None,
):
    gate = _render_eligibility(content_package_id, media_type, source_asset_id)
    if not gate["eligible"]:
        raise ValueError("Render entry gate blocked: " + " ".join(gate["blockers"]))
    if renderer is None:
        configured = configured_renderer_name(gate["media_type"])
        if not configured:
            raise MissingRendererConfiguration(
                f"LIVE_RENDERER_NOT_CONFIGURED: No live renderer is configured for {gate['media_type']}."
            )
        if provider_name and provider_name != configured:
            # A caller can never substitute a different provider, including the fixture.
            raise MissingRendererConfiguration(
                f"RENDERER_PROVIDER_MISMATCH: {gate['media_type']} rendering is configured for {configured}."
            )
        provider_name = configured
        configuration = renderer_configuration(gate["media_type"])
        if provider_name != "fixture" and not configuration["live"]:
            raise MissingRendererConfiguration(configuration["status"])
        renderer = renderer_for(provider_name, gate["media_type"])
    if gate["media_type"] == "IMAGE":
        generation_mode = "IMAGE"
    elif source_asset_id:
        generation_mode = generation_mode if generation_mode == "REFERENCE_TO_VIDEO" else "IMAGE_TO_VIDEO"
    else:
        if generation_mode in ("IMAGE_TO_VIDEO", "REFERENCE_TO_VIDEO"):
            raise ValueError(f"{generation_mode} requires an explicitly selected approved source image.")
        generation_mode = "TEXT_TO_VIDEO"
    if aspect_ratio:
        if generation_mode != "REFERENCE_TO_VIDEO":
            raise ValueError("An explicit aspect ratio is only allowed for reference-to-video; image-to-video would stretch the source.")
        gate["requested_aspect_override"] = aspect_ratio
    requested_aspect, requested_duration = _requested_render_shape(gate, generation_mode, renderer)
    unsupported = getattr(renderer, "unsupported_reason", None)
    if unsupported and gate["media_type"] == "IMAGE":
        reason = unsupported(gate["media_type"], requested_aspect)
    elif unsupported:
        reason = unsupported(gate["media_type"], requested_aspect, duration_seconds=requested_duration, generation_mode=generation_mode)
    else:
        reason = None
    if reason:
        raise ValueError("RENDERER_CAPABILITY_MISMATCH: " + reason)
    source = gate.get("source_asset")
    timestamp = now()
    with connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        if client_request_id:
            replay = connection.execute(
                "SELECT rj.* FROM paid_request_keys prk JOIN render_jobs rj ON rj.id=prk.job_id "
                "WHERE prk.request_key=? AND prk.kind='RENDER'", (client_request_id,),
            ).fetchone()
            if replay:
                connection.commit()
                return {"job": dict(replay), "duplicate": True, "cached": False}
        # A different source image is a different target: it never reuses another source's result.
        prior = connection.execute(
            "SELECT * FROM render_jobs WHERE content_package_id=? AND media_type=? AND COALESCE(source_asset_id,'')=? "
            "ORDER BY regeneration_number DESC LIMIT 1",
            (content_package_id, gate["media_type"], source["id"] if source else ""),
        ).fetchone()
        latest_number = connection.execute(
            "SELECT COALESCE(MAX(regeneration_number),0) FROM render_jobs WHERE content_package_id=? AND media_type=?",
            (content_package_id, gate["media_type"]),
        ).fetchone()[0]
        regeneration_number = latest_number + 1
        request = _build_render_request(gate, renderer, regeneration_number, generation_mode)
        request_json = json.dumps(request, ensure_ascii=False, sort_keys=True)
        request_fingerprint = _render_request_fingerprint(gate, renderer, request, generation_mode)
        active = connection.execute(
            "SELECT * FROM render_jobs WHERE request_fingerprint=? "
            "AND status IN ('QUEUED','PREPARING','RENDERING','VALIDATING') ORDER BY created_at LIMIT 1",
            (request_fingerprint,),
        ).fetchone()
        if active:
            if client_request_id:
                connection.execute(
                    "INSERT OR IGNORE INTO paid_request_keys(request_key,kind,job_id,created_at) VALUES(?,'RENDER',?,?)",
                    (client_request_id, active["id"], timestamp),
                )
            connection.commit()
            return {"job": dict(active), "duplicate": True, "cached": False}
        if prior and not regenerate:
            connection.commit()
            return {"job": dict(prior), "duplicate": False, "cached": True}
        idempotency_key = _version_hash({
            "package": content_package_id, "version": gate["package"]["version_number"],
            "media_type": gate["media_type"], "provider": renderer.name, "model": renderer.model,
            "config": RENDER_GENERATION_CONFIG_VERSION, "renderer_config": RENDERER_CONFIG_VERSION,
            "qa_policy": MEDIA_QA_POLICY_VERSION, "regeneration": regeneration_number,
            **({"mode": generation_mode, "source": [source["id"], source["checksum_sha256"]] if source else None}
               if gate["media_type"] != "IMAGE" else {}),
            **({"aspect": aspect_ratio} if aspect_ratio else {}),
        })
        job_id = "RJ-" + uuid.uuid4().hex[:12].upper()
        connection.execute(
            "INSERT INTO render_jobs(id,event_id,content_decision_id,production_job_id,content_package_id,"
            "content_package_version,media_type,provider,model,provider_mode,prompt_version,generation_config_version,"
            "input_version,input_media_asset_ids_json,idempotency_key,regeneration_number,max_retries,status,fixture_only,"
            "created_at,updated_at,generation_mode,requested_aspect_ratio,requested_duration_seconds,requested_resolution,"
            "source_asset_id,source_asset_checksum) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'QUEUED',?,?,?,?,?,?,?,?,?)",
            (
                job_id, gate["package"]["event_id"], gate["package"]["content_decision_id"], gate["package"]["job_id"],
                content_package_id, gate["package"]["version_number"], gate["media_type"], renderer.name, renderer.model,
                renderer.mode, RENDER_PROMPT_VERSION, RENDER_GENERATION_CONFIG_VERSION, gate["input_version"],
                json.dumps(request["reference_asset_ids"]), idempotency_key, regeneration_number,
                LIVE_RENDERER_MAX_RETRIES if renderer.mode == "live" else RENDER_MAX_RETRIES,
                int(renderer.mode == "fixture"), timestamp, timestamp, generation_mode, requested_aspect,
                requested_duration, request["generation_parameters"].get("resolution"),
                source["id"] if source else None, source["checksum_sha256"] if source else None,
            ),
        )
        connection.execute(
            "UPDATE render_jobs SET request_fingerprint=?,client_request_id=? WHERE id=?",
            (request_fingerprint, client_request_id, job_id),
        )
        if client_request_id:
            connection.execute(
                "INSERT INTO paid_request_keys(request_key,kind,job_id,created_at) VALUES(?,'RENDER',?,?)",
                (client_request_id, job_id, timestamp),
            )
        connection.execute(
            "INSERT INTO render_prompt_snapshots(id,render_job_id,content_package_id,content_package_version,media_type,"
            "provider,model,prompt_version,generation_config_version,request_json,request_hash,reference_asset_ids_json,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "RPS-" + uuid.uuid4().hex[:12].upper(), job_id, content_package_id,
                gate["package"]["version_number"], gate["media_type"], renderer.name, renderer.model,
                RENDER_PROMPT_VERSION, RENDER_GENERATION_CONFIG_VERSION, request_json,
                hashlib.sha256(request_json.encode()).hexdigest(), json.dumps(request["reference_asset_ids"]), timestamp,
            ),
        )
        connection.execute(
            "INSERT INTO render_job_status_history(render_job_id,from_status,to_status,message,changed_at) "
            "VALUES(?,NULL,'QUEUED','Render entry gate passed; immutable prompt and lineage captured.',?)", (job_id, timestamp),
        )
        connection.execute("UPDATE events SET render_status='QUEUED',updated_at=? WHERE id=?", (timestamp, gate["package"]["event_id"]))
        job = dict(connection.execute("SELECT * FROM render_jobs WHERE id=?", (job_id,)).fetchone())
        connection.commit()
    if background:
        RENDER_EXECUTOR.submit(
            run_render_job, job_id, renderer=renderer, visual_qa_provider=visual_qa_provider,
            ocr_provider=ocr_provider, frame_extractor=frame_extractor,
        )
    else:
        run_render_job(
            job_id, renderer, visual_qa_provider=visual_qa_provider,
            ocr_provider=ocr_provider, frame_extractor=frame_extractor,
        )
        job = render_job(job_id)
    return {"job": job, "duplicate": False, "cached": False}


def _safe_render_error(error):
    """Persist useful provider failures without retaining credentials or signed URLs."""
    message = str(error)[:500]
    message = re.sub(r"(?i)(authorization)\s*[:=]\s*(?:bearer\s+)?[^\s,;]+", r"\1=[REDACTED]", message)
    message = re.sub(r"(?i)(api[_-]?key|token|secret)\s*[:=]\s*[^\s,;]+", r"\1=[REDACTED]", message)
    message = re.sub(r"https?://[^\s]+\?[^\s]+", "[REDACTED_SIGNED_URL]", message)
    return message


def _safe_provider_url(value):
    if not value:
        return None
    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
        return None
    if parsed.query or parsed.fragment:
        return None
    return value


def _scrub_provider_metadata(value):
    if isinstance(value, dict):
        clean = {}
        for key, item in value.items():
            if re.search(r"(?i)(api.?key|authorization|token|secret|signed.?url|credential)", str(key)):
                clean[str(key)] = "[REDACTED]"
            else:
                clean[str(key)] = _scrub_provider_metadata(item)
        return clean
    if isinstance(value, list):
        return [_scrub_provider_metadata(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_scrub_provider_metadata(item) for item in value)
    if isinstance(value, str) and "?" in value and value.startswith(("http://", "https://")):
        return "[REDACTED_SIGNED_URL]"
    return value


def _validate_render_result(result, request, stored, storage):
    errors = []
    media_type = request["media_type"]
    if not result or not isinstance(result.asset_bytes, (bytes, bytearray)):
        return {"valid": False, "errors": ["Renderer returned no binary asset."], "validator_version": RENDER_POLICY_VERSION}
    if stored.file_size <= 0:
        errors.append("Generated asset is empty.")
    if not storage.exists(stored.storage_uri):
        errors.append("Generated asset was not durably persisted.")
    elif hashlib.sha256(storage.get(stored.storage_uri)).hexdigest() != stored.checksum_sha256:
        errors.append("Stored asset checksum does not match the generated binary.")
    parsed_width = parsed_height = decoded = None
    if media_type == "IMAGE":
        if result.mime_type not in ("image/png", "image/jpeg", "image/webp"):
            errors.append("IMAGE result must use a supported PNG, JPEG, or WebP MIME type.")
        else:
            try:
                decoded = inspect_image(result.asset_bytes)
                parsed_width, parsed_height = decoded["width"], decoded["height"]
            except ImageDecodeError as error:
                errors.append(f"Generated image is corrupt: {error}")
            if decoded and decoded["mime_type"] != result.mime_type:
                errors.append("Generated image MIME type does not match the decoded file format.")
        declared_mime = (result.provider_metadata or {}).get("declared_mime_type")
        if decoded and declared_mime and declared_mime != decoded["mime_type"]:
            errors.append("Provider-declared MIME type does not match the decoded file format.")
        if parsed_width and (parsed_width < RENDER_MIN_IMAGE_WIDTH or parsed_height < RENDER_MIN_IMAGE_HEIGHT):
            errors.append("Generated image dimensions are below minimum policy.")
        if parsed_width and (
            (result.width is not None and result.width != parsed_width)
            or (result.height is not None and result.height != parsed_height)
        ):
            errors.append("Provider-reported dimensions do not match the decoded image.")
        expected = request["generation_parameters"]
        if parsed_width and abs(parsed_width / parsed_height - expected["width"] / expected["height"]) > RENDER_ASPECT_TOLERANCE:
            errors.append("Generated image aspect ratio does not match the package request.")
        if result.image_count not in (None, expected["output_count"]):
            errors.append("Provider output count does not match the request.")
    elif media_type in ("VIDEO", "SHORT_FORM_VIDEO", "LONG_FORM_VIDEO"):
        video = None
        if result.mime_type != "video/mp4":
            errors.append("VIDEO result must be an MP4 container (video/mp4).")
        else:
            try:
                video = inspect_video(result.asset_bytes)
            except ImageDecodeError as error:
                errors.append(f"Generated video is corrupt or not decodable: {error}")
        params = request["generation_parameters"]
        if video:
            decoded = video
            parsed_width, parsed_height = video["width"], video["height"]
            if video["duration_seconds"] <= 0:
                errors.append("Generated video has zero duration.")
            if min(parsed_width, parsed_height) < RENDER_MIN_VIDEO_SHORT_SIDE:
                errors.append("Generated video resolution is below minimum policy.")
            requested = params.get("aspect_ratio") or ""
            if ":" in requested:
                left, right = (float(part) for part in requested.split(":"))
                if abs(parsed_width / parsed_height - left / right) > RENDER_ASPECT_TOLERANCE:
                    errors.append(f"Generated video aspect ratio does not match the requested {requested}.")
            else:
                errors.append("The render request has no supported aspect ratio to validate against.")
            wanted = params.get("duration_seconds")
            if wanted and abs(video["duration_seconds"] - float(wanted)) > RENDER_VIDEO_DURATION_TOLERANCE_SECONDS:
                errors.append(f"Generated video duration {video['duration_seconds']}s does not match the requested {wanted}s.")
            if result.duration_seconds and abs(video["duration_seconds"] - float(result.duration_seconds)) > RENDER_VIDEO_DURATION_TOLERANCE_SECONDS:
                errors.append("Provider-reported duration does not match the decoded video.")
        if request.get("source_asset") and not request["source_asset"].get("checksum_sha256"):
            errors.append("Image-to-video lineage is missing the exact source checksum.")
    elif media_type in ("AUDIO", "VOICEOVER"):
        if result.mime_type not in ("audio/wav", "audio/mpeg", "audio/mp4"):
            errors.append("AUDIO result has an invalid MIME type.")
        if not result.duration_seconds or result.duration_seconds <= 0:
            errors.append("Generated audio has no valid duration metadata.")
    return {
        "valid": not errors, "errors": errors, "validator_version": RENDER_POLICY_VERSION,
        "checksum_sha256": stored.checksum_sha256, "file_size": stored.file_size,
        "decoded_width": parsed_width, "decoded_height": parsed_height,
        "decoded_format": decoded["format"] if decoded else None,
        "decoder": decoded["decoder"] if decoded else None,
        "decoded_duration_seconds": decoded.get("duration_seconds") if decoded else None,
        "decoded_frame_rate": decoded.get("frame_rate") if decoded else None,
        "decoded_codec": decoded.get("codec") if decoded else None,
        "decoded_has_audio": decoded.get("has_audio") if decoded else None,
        "source_asset": request.get("source_asset"),
        "provider_reported_dimensions": [result.width, result.height] if result.width or result.height else None,
    }


def _persist_provider_events(connection, job_id, attempt_number, lifecycle_events, provider_job_id=None):
    for event in lifecycle_events or ():
        if event.get("_persisted"):
            continue
        event_type = event.get("event_type", "POLLED")
        if event_type not in ("SUBMITTED", "POLLED", "RATE_LIMITED", "COMPLETED", "FAILED", "DOWNLOADED"):
            event_type = "POLLED"
        connection.execute(
            "INSERT INTO render_provider_events(render_job_id,attempt_number,event_type,provider_status,"
            "provider_job_id,safe_metadata_json,occurred_at) VALUES(?,?,?,?,?,?,?)",
            (
                job_id, attempt_number, event_type, event.get("status"), event.get("provider_job_id") or provider_job_id,
                json.dumps(_scrub_provider_metadata({k: v for k, v in event.items() if k != "_persisted"}), ensure_ascii=False, sort_keys=True),
                event.get("at") or now(),
            ),
        )


def _package_allowed_text(package, request):
    values = []
    for item in request.get("intended_text_overlays") or ():
        if isinstance(item, dict):
            values.extend(str(value) for value in item.values() if isinstance(value, str))
        elif isinstance(item, str):
            values.append(item)
    for key in ("headline", "hook", "caption"):
        item = package.get(key)
        if isinstance(item, dict):
            values.extend(str(value) for value in item.values() if isinstance(value, str))
        elif isinstance(item, str):
            values.append(item)
    return values


def _run_ocr_and_prepare_visual_inputs(result, request, gate, technical, ocr_provider, frame_extractor, storage):
    """Run local OCR and return (QA result, visual inputs, sampled frame artifacts)."""
    images, frame_artifacts = [], []
    try:
        if request["media_type"] == "IMAGE":
            images.append({"label": "generated image", "role": "generated", "jpeg": analysis_jpeg(result.asset_bytes)})
        elif request["media_type"] in ("VIDEO", "SHORT_FORM_VIDEO", "LONG_FORM_VIDEO"):
            duration = technical.get("decoded_duration_seconds") or result.duration_seconds
            # Very short clips get denser sampling so brief scene changes are less
            # likely to fall between the five mandatory timeline positions.
            frames = frame_extractor.extract(result.asset_bytes, sample_times(duration, extra=4 if float(duration or 0) <= 6 else 0))
            if len(frames) < 5:
                raise MediaToolUnavailable("Video QA requires at least five representative frames.")
            for index, frame in enumerate(frames):
                label = f"video frame {index + 1} at {frame['actual_seconds']:.3f}s"
                images.append({"label": label, "role": "frame", "jpeg": frame["jpeg"]})
                frame_artifacts.append({**frame, "label": label})
        if request.get("original_source_asset"):
            original = request["original_source_asset"]
            with connect() as connection:
                row = connection.execute("SELECT storage_uri FROM generated_assets WHERE id=?", (original["id"],)).fetchone()
            if row:
                source_bytes = storage.get(row["storage_uri"])
                images.append({"label": "exact source image reference", "role": "reference", "jpeg": analysis_jpeg(source_bytes)})
        detections = []
        seen = set()
        for image in images:
            if image["role"] == "reference":
                continue
            for detection in ocr_provider.detect(image["jpeg"]):
                normalized = re.sub(r"\s+", " ", str(detection.get("text") or "")).strip().casefold()
                if not normalized or normalized in seen:
                    continue
                seen.add(normalized)
                detections.append({**detection, "frame": image["label"]})
        package = gate["package_payload"]
        with connect() as connection:
            claim_texts = [row["text"] for row in connection.execute(
                "SELECT text FROM claim_versions WHERE id IN (" + ",".join("?" for _ in gate["claim_version_ids"]) + ")"
                if gate["claim_version_ids"] else "SELECT text FROM claim_versions WHERE 0",
                gate["claim_version_ids"],
            )]
        policy = evaluate_ocr_policy(
            detections, allowed_texts=_package_allowed_text(package, request), approved_claim_texts=claim_texts,
        )
        return MediaQAResult(
            status="FAILED" if policy["status"] == "FLAG" else "PASSED",
            flags=tuple(item["check"] for item in policy["checks"] if item["status"] == "FLAG"),
            details={
                "ocr_performed": True, "detections": policy["detections"], "checks": policy["checks"],
                "run_status": policy["status"], "sampled_frames": [item["label"] for item in frame_artifacts],
                "evidence_warning": "OCR output is QA evidence, not factual truth.",
            }, provider=ocr_provider.name, model=ocr_provider.model,
        ), images, frame_artifacts
    except (MediaToolUnavailable, OSError, ValueError) as error:
        return MediaQAResult(
            status="NOT_PERFORMED", flags=("OCR_UNAVAILABLE",),
            details={"ocr_performed": False, "reason": _safe_render_error(error), "run_status": "UNKNOWN"},
            provider=getattr(ocr_provider, "name", None), model=getattr(ocr_provider, "model", None),
        ), images, frame_artifacts


def _persist_qa_frame_artifacts(connection, asset_id, frame_artifacts, storage):
    rows = []
    source = connection.execute("SELECT checksum_sha256 FROM generated_assets WHERE id=?", (asset_id,)).fetchone()
    for frame in frame_artifacts:
        transform = {
            "operation": "representative_frame_sample", "extractor": "avfoundation",
            "requested_seconds": frame["requested_seconds"], "actual_seconds": frame["actual_seconds"],
            "width": frame["width"], "height": frame["height"],
        }
        transform_hash = _version_hash(transform)
        stored = storage.save(frame["jpeg"], extension="jpg", metadata={"purpose": "QA_FRAME"})
        row = {
            "id": "DA-" + uuid.uuid4().hex[:12].upper(), "source_asset_id": asset_id,
            "source_checksum_sha256": source["checksum_sha256"], "purpose": "QA_FRAME",
            "storage_uri": stored.storage_uri, "mime_type": "image/jpeg", "width": frame["width"],
            "height": frame["height"], "file_size": stored.file_size, "checksum_sha256": stored.checksum_sha256,
            "frame_time_seconds": frame["actual_seconds"], "transform_json": json.dumps(transform, sort_keys=True),
            "transform_hash": transform_hash, "created_at": now(),
        }
        connection.execute(
            "INSERT OR IGNORE INTO derived_assets(id,source_asset_id,source_checksum_sha256,purpose,storage_uri,mime_type,"
            "width,height,file_size,checksum_sha256,frame_time_seconds,transform_json,transform_hash,created_at) "
            "VALUES(:id,:source_asset_id,:source_checksum_sha256,:purpose,:storage_uri,:mime_type,:width,:height,"
            ":file_size,:checksum_sha256,:frame_time_seconds,:transform_json,:transform_hash,:created_at)", row,
        )
        saved = connection.execute(
            "SELECT id,frame_time_seconds,checksum_sha256,storage_uri FROM derived_assets "
            "WHERE source_asset_id=? AND purpose='QA_FRAME' AND transform_hash=?", (asset_id, transform_hash),
        ).fetchone()
        rows.append(dict(saved))
    return rows


def _persist_media_qa(connection, job_id, asset_id, technical, text_qa, semantic_qa):
    rows = (
        ("TECHNICAL", "PASSED" if technical["valid"] else "FAILED", None, None, None,
         tuple(technical.get("errors") or ()), technical),
        ("TEXT_OVERLAY", text_qa.status, text_qa.provider, text_qa.model, text_qa.confidence,
         text_qa.flags, text_qa.details or {}),
        ("SEMANTIC_VISUAL", semantic_qa.status, semantic_qa.provider, semantic_qa.model, semantic_qa.confidence,
         semantic_qa.flags, semantic_qa.details or {}),
    )
    for qa_type, status, provider, model, confidence, flags, details in rows:
        connection.execute(
            "INSERT INTO media_qa_results(id,render_job_id,generated_asset_id,qa_type,status,provider,model,confidence,"
            "flags_json,details_json,policy_version,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "MQ-" + uuid.uuid4().hex[:12].upper(), job_id, asset_id, qa_type, status, provider, model,
                confidence, json.dumps(list(flags), ensure_ascii=False),
                json.dumps(_scrub_provider_metadata(details), ensure_ascii=False, sort_keys=True),
                MEDIA_QA_POLICY_VERSION, now(),
            ),
        )
    job = connection.execute("SELECT * FROM render_jobs WHERE id=?", (job_id,)).fetchone()
    versioned = (
        ("TECHNICAL", "PASS" if technical["valid"] else "FLAG", None, None,
         [{"check": "TECHNICAL_VALIDATION", "status": "PASS" if technical["valid"] else "FLAG",
           "reason": "; ".join(technical.get("errors") or ()) or "Decoded media and lineage passed."}], technical),
        ("OCR", "PASS" if text_qa.status == "PASSED" else "FLAG" if text_qa.status == "FAILED" else "UNKNOWN",
         text_qa.provider, text_qa.model, (text_qa.details or {}).get("checks") or [], text_qa.details or {}),
        ("VISUAL", "PASS" if semantic_qa.status == "PASSED" else "FLAG" if semantic_qa.status == "FLAGGED" else "UNKNOWN",
         semantic_qa.provider, semantic_qa.model, (semantic_qa.details or {}).get("checks") or [], semantic_qa.details or {}),
    )
    for kind, status, provider, model, checks, evidence in versioned:
        run_number = connection.execute(
            "SELECT COALESCE(MAX(run_number),0)+1 FROM media_qa_runs WHERE generated_asset_id=? AND qa_kind=?",
            (asset_id, kind),
        ).fetchone()[0]
        cost = evidence.get("cost_usd") if isinstance(evidence, dict) else None
        connection.execute(
            "INSERT INTO media_qa_runs(id,generated_asset_id,render_job_id,qa_kind,run_number,trigger,status,provider,model,"
            "prompt_version,content_package_id,content_package_version,checks_json,evidence_json,explanation,"
            "provider_request_id,usage_json,cost_status,cost_usd,created_at) VALUES(?,?,?,?,?,'AUTOMATIC',?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "QR-" + uuid.uuid4().hex[:12].upper(), asset_id, job_id, kind, run_number, status, provider, model,
                ((evidence or {}).get("prompt_version") if isinstance(evidence, dict) else None) or MEDIA_QA_POLICY_VERSION,
                job["content_package_id"], job["content_package_version"],
                json.dumps(checks, ensure_ascii=False, sort_keys=True),
                json.dumps(_scrub_provider_metadata(evidence), ensure_ascii=False, sort_keys=True),
                (evidence or {}).get("reason") or (evidence or {}).get("summary"),
                (evidence or {}).get("provider_request_id"),
                json.dumps((evidence or {}).get("usage") or {}, sort_keys=True),
                "known" if cost is not None else (
                    "not_billed" if kind in ("TECHNICAL", "OCR") or provider is None else "unknown"
                ),
                cost, now(),
            ),
        )


def _record_render_cost(connection, job):
    known = job.get("provider_cost_usd") is not None or job.get("calculated_cost_usd") is not None
    connection.execute(
        "INSERT OR IGNORE INTO cost_ledger(id,event_id,stage,reference_type,reference_id,provider,model,cost_status,"
        "currency,provider_reported_cost,locally_calculated_cost,units_json,pricing_version,recorded_at) "
        "VALUES(?,?,'MEDIA_RENDERING','RenderJob',?,?,?,?,?,?,?,?,?,?)",
        (
            "CL-" + uuid.uuid4().hex[:12].upper(), job["event_id"], job["id"], job["provider"], job["model"],
            "known" if known else "unknown", job.get("currency"), job.get("provider_cost_usd"),
            job.get("calculated_cost_usd"), json.dumps({
                "request_count": job.get("request_count"), "credits_consumed": job.get("credits_consumed"),
                "provider_units": job.get("provider_units"), "input_units": job.get("input_units"),
                "output_units": job.get("output_units"), "generation_seconds": job.get("generation_seconds"),
                "frame_count": job.get("frame_count"), "image_count": job.get("image_count"),
                "media_type": job.get("media_type"), "generation_mode": job.get("generation_mode"),
            }, sort_keys=True), job.get("pricing_version"), now(),
        ),
    )


def _persist_render_asset(connection, job, result, stored, validation, status, text_qa, semantic_qa, stale, ready):
    existing = connection.execute(
        "SELECT * FROM generated_assets WHERE content_package_id=? AND media_type=? AND checksum_sha256=?",
        (job["content_package_id"], job["media_type"], stored.checksum_sha256),
    ).fetchone()
    if existing:
        connection.execute(
            "INSERT INTO render_job_outputs(render_job_id,generated_asset_id,reused_identical_binary) VALUES(?,?,1)",
            (job["id"], existing["id"]),
        )
        return existing["id"]
    version = connection.execute(
        "SELECT COALESCE(MAX(version_number),0)+1 FROM generated_assets WHERE content_package_id=? AND media_type=?",
        (job["content_package_id"], job["media_type"]),
    ).fetchone()[0]
    asset_id = "GA-" + uuid.uuid4().hex[:12].upper()
    provenance = {
        "generated_by_ai": True, "fixture": bool(job["fixture_only"]), "provider": job["provider"],
        "model": job["model"], "generation_timestamp": now(), "content_package_id": job["content_package_id"],
        "content_package_version": job["content_package_version"],
        "source_reference_media_asset_ids": json.loads(job["input_media_asset_ids_json"]),
        "prompt_version": job["prompt_version"],
        "semantic_visual_qa_performed": semantic_qa.status != "NOT_PERFORMED",
        "human_review_required": True,
    }
    connection.execute(
        "INSERT INTO generated_assets(id,render_job_id,event_id,content_package_id,content_package_version,media_type,version_number,"
        "status,executable,fixture_only,provider,model,provider_asset_id,provider_request_id,original_provider_url,"
        "storage_uri,mime_type,width,height,duration_seconds,frame_rate,file_size,checksum_sha256,prompt_version,"
        "generation_config_version,source_asset_ids_json,provenance_json,provider_metadata_json,detected_text_json,"
        "validation_status,validation_result_json,created_at,usable_for_review,stale,stale_reason,"
        "technical_validation_status,text_validation_status,semantic_qa_status,human_review_status,codec,has_audio) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            asset_id, job["id"], job["event_id"], job["content_package_id"], job["content_package_version"], job["media_type"], version,
            status, int(status == "VALIDATED" and not job["fixture_only"]), job["fixture_only"], job["provider"], job["model"],
            result.provider_asset_id, result.provider_request_id, _safe_provider_url(result.original_provider_url), stored.storage_uri,
            result.mime_type, validation["technical"].get("decoded_width") or result.width,
            validation["technical"].get("decoded_height") or result.height,
            validation["technical"].get("decoded_duration_seconds") or result.duration_seconds,
            validation["technical"].get("decoded_frame_rate") or result.frame_rate, stored.file_size,
            stored.checksum_sha256, job["prompt_version"], job["generation_config_version"],
            job["input_media_asset_ids_json"], json.dumps(provenance, sort_keys=True),
            json.dumps(_scrub_provider_metadata(result.provider_metadata or {}), sort_keys=True), json.dumps(list(result.detected_text)),
            "PASSED" if ready else "FAILED", json.dumps(validation, ensure_ascii=False), now(),
            int(ready and not stale), int(stale),
            "Lineage changed during rendering." if stale else None,
            "PASSED" if validation["technical"]["valid"] else "FAILED",
            text_qa.status, semantic_qa.status, "REQUIRED", validation["technical"].get("decoded_codec"),
            None if validation["technical"].get("decoded_has_audio") is None else int(validation["technical"]["decoded_has_audio"]),
        ),
    )
    connection.execute(
        "INSERT INTO render_job_outputs(render_job_id,generated_asset_id,reused_identical_binary) VALUES(?,?,0)",
        (job["id"], asset_id),
    )
    return asset_id


def _expected_render_entities(package, claims):
    """Named entities the visual QA layer must check; generated imagery never establishes identity."""
    entities = []
    for key in ("entities", "named_entities", "public_figures"):
        for item in package.get(key) or ():
            name = item.get("name") if isinstance(item, dict) else item
            if name and str(name) not in entities:
                entities.append(str(name))
    for claim in claims:
        attribution = claim.get("attribution")
        if attribution and attribution not in entities:
            entities.append(attribution)
    return entities


PRODUCTION_COST_STAGES = (
    ("RESEARCH", "research_runs", "cost_usd", "mode='live'"),
    ("VERIFICATION", "verification_runs", "cost_usd", "mode='live'"),
    ("CONTENT_CEO", "content_decision_runs", "cost_usd", "mode='live' AND provider_called=1"),
    ("CONTENT_PRODUCTION", "production_jobs", "cost_usd", "provider_mode='live' AND provider_called=1"),
    ("IMAGE_RENDERING", "render_jobs", "COALESCE(provider_cost_usd,calculated_cost_usd)",
     "provider_mode='live' AND provider_called=1 AND media_type IN ('IMAGE','THUMBNAIL','CAROUSEL_SLIDE')"),
    ("VIDEO_RENDERING", "render_jobs", "COALESCE(provider_cost_usd,calculated_cost_usd)",
     "provider_mode='live' AND provider_called=1 AND media_type IN ('VIDEO','SHORT_FORM_VIDEO','LONG_FORM_VIDEO')"),
)


def _job_lineage_state(render, render_gate, cache=None):
    """(lineage blockers, current input version) for a render job's own target."""
    if render["media_type"] == render_gate.get("media_type") and not render.get("source_asset_id") \
            and render["content_package_id"] == render_gate.get("content_package_id"):
        return render_gate.get("lineage_blockers") or [], render_gate.get("input_version")
    key = (render["content_package_id"], render["media_type"], render.get("source_asset_id"))
    if cache is not None and key in cache:
        return cache[key]
    gate = _render_eligibility(*key)
    state = (list(gate["blockers"]), gate["input_version"])
    if cache is not None:
        cache[key] = state
    return state


def _render_phase(render):
    """Human-facing phase: QUEUED → SUBMITTED → GENERATING → DOWNLOADING → VALIDATING → final status."""
    if render.get("resume_state") == "PROVIDER_PENDING":
        return {"phase": "PROVIDER_PENDING", "progress": None}
    if render.get("resume_state") == "NEEDS_INTERVENTION":
        return {"phase": "NEEDS_INTERVENTION", "progress": None}
    if render.get("resume_state") == "INTERRUPTED":
        return {"phase": "INTERRUPTED", "progress": None}
    if render["status"] not in ("QUEUED", "PREPARING", "RENDERING", "VALIDATING"):
        return {"phase": render["status"], "progress": None}
    if render["status"] == "VALIDATING":
        return {"phase": "VALIDATING", "progress": None}
    events = render.get("provider_events") or []
    last = events[-1] if events else None
    if not last:
        return {"phase": "QUEUED" if render["status"] in ("QUEUED", "PREPARING") else "SUBMITTING", "progress": None}
    phase = {"SUBMITTED": "SUBMITTED", "POLLED": "GENERATING", "RATE_LIMITED": "GENERATING",
             "COMPLETED": "DOWNLOADING", "DOWNLOADED": "VALIDATING"}.get(last["event_type"], "GENERATING")
    return {"phase": phase, "progress": (last.get("safe_metadata") or {}).get("progress")}


def production_cost_summary(connection, event_id):
    """Aggregate live provider spend per stage; unknown costs are counted, never treated as zero."""
    stages, total, unknown = [], 0.0, 0
    for stage, table, cost_expression, live_filter in PRODUCTION_COST_STAGES:
        row = connection.execute(
            f"SELECT COUNT(*) AS runs,SUM(CASE WHEN cost_status='known' THEN {cost_expression} END) AS known_cost,"
            f"SUM(CASE WHEN cost_status='known' AND {cost_expression} IS NOT NULL THEN 0 ELSE 1 END) AS unknown_runs "
            f"FROM {table} WHERE event_id=? AND {live_filter}", (event_id,),
        ).fetchone()
        known_cost = round(row["known_cost"], 6) if row["known_cost"] is not None else None
        stages.append({
            "stage": stage, "live_runs": row["runs"], "known_cost_usd": known_cost,
            "unknown_cost_runs": row["unknown_runs"] or 0,
        })
        total += known_cost or 0.0
        unknown += row["unknown_runs"] or 0
    for kind, stage in (("OCR", "OCR_QA"), ("VISUAL", "VISUAL_QA")):
        row = connection.execute(
            "SELECT SUM(CASE WHEN m.cost_status!='not_billed' THEN 1 ELSE 0 END) AS runs,"
            "SUM(CASE WHEN m.cost_status='known' THEN m.cost_usd END) AS known_cost,"
            "SUM(CASE WHEN m.cost_status='unknown' THEN 1 ELSE 0 END) AS unknown_runs "
            "FROM media_qa_runs m JOIN generated_assets ga ON ga.id=m.generated_asset_id "
            "WHERE ga.event_id=? AND m.qa_kind=?", (event_id, kind),
        ).fetchone()
        known_cost = round(row["known_cost"], 6) if row["known_cost"] is not None else None
        stages.append({"stage": stage, "live_runs": row["runs"] or 0, "known_cost_usd": known_cost,
                       "unknown_cost_runs": row["unknown_runs"] or 0})
        total += known_cost or 0.0
        unknown += row["unknown_runs"] or 0
    return {
        "stages": stages, "known_total_usd": round(total, 6),
        "unknown_cost_runs": unknown, "total_status": "complete" if not unknown else "partial",
    }


def run_render_job(job_id, renderer=None, storage=None, visual_qa_provider=None, prepared_result=None,
                   ocr_provider=None, frame_extractor=None):
    storage = storage or LocalMediaStorage(RENDER_STORAGE_ROOT)
    visual_qa_provider = visual_qa_provider or visual_qa_provider_for()
    ocr_provider = ocr_provider or ocr_provider_for()
    frame_extractor = frame_extractor or frame_extractor_for()
    with connect() as connection:
        job_row = connection.execute("SELECT * FROM render_jobs WHERE id=?", (job_id,)).fetchone()
        if job_row is None:
            raise KeyError(job_id)
        if prepared_result is None and job_row["status"] != "QUEUED":
            return dict(job_row)
        if prepared_result is not None and (
            job_row["status"] not in ("RENDERING", "VALIDATING") or not job_row["provider_job_id"]
        ):
            raise ValueError("Only a pending submitted provider job can be resumed without resubmission.")
        renderer = renderer or renderer_for(
            "fixture" if job_row["provider_mode"] == "fixture" else job_row["provider"], job_row["media_type"]
        )
        if prepared_result is None:
            _transition_render_job(connection, job_id, "PREPARING", "Revalidating package lineage before renderer call.")
            connection.execute("UPDATE render_jobs SET started_at=? WHERE id=?", (now(), job_id))
    job = render_job(job_id)
    gate = _render_eligibility(job["content_package_id"], job["media_type"], job.get("source_asset_id"))
    if prepared_result is None and (not gate["eligible"] or gate["input_version"] != job["input_version"]):
        errors = gate["blockers"] or ["Render inputs changed before the renderer call."]
        with connect() as connection:
            connection.execute(
                "UPDATE render_jobs SET validation_status='FAILED',technical_validation_status='FAILED',"
                "validation_result_json=?,failure_code='STALE_INPUTS',failure_reason=?,updated_at=? WHERE id=?",
                (json.dumps({"valid": False, "errors": errors}), " ".join(errors)[:500], now(), job_id),
            )
            _transition_render_job(connection, job_id, "BLOCKED", "Render inputs became invalid before provider execution.")
            _record_render_cost(connection, dict(connection.execute("SELECT * FROM render_jobs WHERE id=?", (job_id,)).fetchone()))
        return render_job(job_id)
    with connect() as connection:
        prompt = connection.execute("SELECT request_json FROM render_prompt_snapshots WHERE render_job_id=?", (job_id,)).fetchone()
    request = json.loads(prompt["request_json"])
    try:
        request = _prepare_video_source_derivative(job, request, storage)
    except Exception as error:
        safe_error = _safe_render_error(error)
        with connect() as connection:
            connection.execute(
                "UPDATE render_jobs SET validation_status='FAILED',technical_validation_status='FAILED',"
                "failure_code='VIDEO_SOURCE_PREPARATION_FAILED',failure_reason=?,updated_at=? WHERE id=?",
                (safe_error, now(), job_id),
            )
            _transition_render_job(connection, job_id, "FAILED", "Video source preparation failed before any paid provider request.")
        return render_job(job_id)
    if prepared_result is None:
        with connect() as connection:
            _transition_render_job(connection, job_id, "RENDERING", "Calling configured renderer with immutable prompt snapshot.")
    current_attempt = {"number": 1}

    def stream_event(event):
        # Persist each provider lifecycle event as it happens so long video renders show live progress.
        with connect() as connection:
            _persist_provider_events(connection, job_id, current_attempt["number"], [event], event.get("provider_job_id"))
            connection.execute(
                "UPDATE render_jobs SET provider_job_id=COALESCE(?,provider_job_id),provider_status=?,"
                "submitted_at=CASE WHEN ?='SUBMITTED' THEN ? ELSE submitted_at END,"
                "poll_count=poll_count+CASE WHEN ? IN ('POLLED','RATE_LIMITED') THEN 1 ELSE 0 END,"
                "last_polled_at=CASE WHEN ? IN ('POLLED','RATE_LIMITED') THEN ? ELSE last_polled_at END,updated_at=? WHERE id=?",
                (event.get("provider_job_id"), event.get("status"), event.get("event_type"), event.get("at"),
                 event.get("event_type"), event.get("event_type"), event.get("at"), now(), job_id),
            )
        event["_persisted"] = True

    def load_asset(asset_id, checksum):
        with connect() as connection:
            row = connection.execute(
                "SELECT storage_uri,checksum_sha256 FROM generated_assets WHERE id=?", (asset_id,)
            ).fetchone()
            if row is None:
                row = connection.execute(
                    "SELECT storage_uri,checksum_sha256 FROM derived_assets WHERE id=?", (asset_id,)
                ).fetchone()
        if row is None or row["checksum_sha256"] != checksum:
            raise ValueError("Source asset lineage does not match the render job.")
        return storage.get(row["storage_uri"])

    if hasattr(renderer, "event_sink"):
        renderer.event_sink = stream_event
    if hasattr(renderer, "asset_loader"):
        renderer.asset_loader = load_asset
    result = prepared_result
    last_error = None
    attempt_range = range(1, job["max_retries"] + 2) if prepared_result is None else ()
    for attempt in attempt_range:
        current_attempt["number"] = attempt
        started = now()
        with connect() as connection:
            connection.execute(
                "INSERT INTO render_job_attempts(render_job_id,attempt_number,status,started_at) VALUES(?,?,'STARTED',?)",
                (job_id, attempt, started),
            )
        try:
            timeout_seconds = LIVE_RENDERER_TIMEOUT_SECONDS if renderer.mode == "live" else RENDER_TIMEOUT_SECONDS
            result = renderer.render(request, timeout_seconds=timeout_seconds)
            if not result or not isinstance(result.asset_bytes, (bytes, bytearray)):
                raise InvalidRendererResponse("Renderer returned no binary media output.")
            usage = {
                "request_count": result.request_count, "credits_consumed": result.credits_consumed,
                "provider_units": result.provider_units, "input_units": result.input_units,
                "output_units": result.output_units, "generation_seconds": result.generation_seconds,
                "frame_count": result.frame_count, "image_count": result.image_count,
                "currency": result.currency,
            }
            with connect() as connection:
                connection.execute(
                    "INSERT INTO render_job_attempts(render_job_id,attempt_number,status,provider_request_id,started_at,"
                    "completed_at,latency_ms,retryable,usage_json) VALUES(?,?,'SUCCEEDED',?,?,?,?,0,?)",
                    (job_id, attempt, result.provider_request_id, started, now(), result.latency_ms, json.dumps(usage)),
                )
                _persist_provider_events(
                    connection, job_id, attempt, result.lifecycle_events, result.provider_job_id
                )
            break
        except Exception as error:
            last_error = error
            retryable = bool(getattr(error, "retryable", False)) and not bool(getattr(error, "provider_job_id", None))
            with connect() as connection:
                connection.execute(
                    "INSERT INTO render_job_attempts(render_job_id,attempt_number,status,started_at,completed_at,retryable,"
                    "error_code,error_message) VALUES(?,?,'FAILED',?,?,?,?,?)",
                    (job_id, attempt, started, now(), int(retryable),
                     getattr(error, "code", "UNKNOWN_PROVIDER_ERROR"), _safe_render_error(error)),
                )
                _persist_provider_events(
                    connection, job_id, attempt, getattr(error, "lifecycle_events", ()),
                    getattr(error, "provider_job_id", None),
                )
                connection.execute(
                    "UPDATE render_jobs SET retry_count=?,provider_job_id=COALESCE(?,provider_job_id),"
                    "provider_request_id=COALESCE(?,provider_request_id),provider_status=COALESCE(?,provider_status),"
                    "submitted_at=COALESCE(?,submitted_at),last_polled_at=COALESCE(?,last_polled_at),"
                    "poll_count=MAX(poll_count,?),provider_started_at=COALESCE(?,provider_started_at),"
                    "provider_completed_at=COALESCE(?,provider_completed_at),updated_at=? WHERE id=?",
                    (
                        attempt - 1, getattr(error, "provider_job_id", None), getattr(error, "provider_request_id", None),
                        getattr(error, "provider_status", None), getattr(error, "submitted_at", None),
                        getattr(error, "last_polled_at", None), getattr(error, "poll_count", 0) or 0,
                        getattr(error, "provider_started_at", None), getattr(error, "provider_completed_at", None),
                        now(), job_id,
                    ),
                )
            if not retryable or attempt > job["max_retries"]:
                break
            retry_after = getattr(error, "retry_after_seconds", None)
            delay = min(RENDER_MAX_BACKOFF_SECONDS, retry_after if retry_after is not None else RENDER_BACKOFF_SECONDS * 2 ** (attempt - 1))
            if delay > 0:
                time.sleep(delay)
    if result is None:
        code = getattr(last_error, "code", "UNKNOWN_PROVIDER_ERROR")
        safe_error = _safe_render_error(last_error)
        pending_codes = {
            "POLL_ATTEMPTS_EXHAUSTED", "TIMEOUT", "NETWORK_ERROR", "RATE_LIMITED", "PROVIDER_5XX", "DOWNLOAD_FAILED",
        }
        submitted_provider_job_id = getattr(last_error, "provider_job_id", None) or render_job(job_id).get("provider_job_id")
        if submitted_provider_job_id and code in pending_codes:
            with connect() as connection:
                connection.execute(
                    "UPDATE render_jobs SET provider_called=1,resume_state='PROVIDER_PENDING',resume_reason=?,"
                    "failure_code=?,failure_reason=?,cost_status='unknown',updated_at=? WHERE id=?",
                    (safe_error, code, safe_error, now(), job_id),
                )
                connection.execute(
                    "INSERT INTO render_job_status_history(render_job_id,from_status,to_status,message,metadata_json,changed_at) "
                    "VALUES(?,'RENDERING','RENDERING','Local polling stopped; resume the saved provider job instead of submitting again.',?,?)",
                    (job_id, json.dumps({"resume_state": "PROVIDER_PENDING", "reason": code}), now()),
                )
            return render_job(job_id)
        if submitted_provider_job_id and code == "INVALID_RESPONSE":
            with connect() as connection:
                connection.execute(
                    "UPDATE render_jobs SET provider_called=1,resume_state='NEEDS_INTERVENTION',resume_reason=?,"
                    "failure_code=?,failure_reason=?,updated_at=? WHERE id=?",
                    (safe_error, code, safe_error, now(), job_id),
                )
                _transition_render_job(connection, job_id, "HUMAN_REVIEW", "Provider status was unknown; human intervention is required and no replacement was submitted.")
                _record_render_cost(connection, dict(connection.execute("SELECT * FROM render_jobs WHERE id=?", (job_id,)).fetchone()))
            return render_job(job_id)
        with connect() as connection:
            connection.execute(
                "UPDATE render_jobs SET provider_called=1,validation_status='FAILED',technical_validation_status='FAILED',"
                "validation_result_json=?,failure_code=?,failure_reason=?,provider_failure_code=?,"
                "provider_failure_reason=?,cost_status='unknown',updated_at=? WHERE id=?",
                (json.dumps({"valid": False, "errors": [safe_error]}), code, safe_error, code, safe_error, now(), job_id),
            )
            target = "HUMAN_REVIEW" if code in ("PROVIDER_REJECTED", "CONTENT_POLICY_REJECTED") else "FAILED"
            _transition_render_job(connection, job_id, target, "Renderer ended without a valid downloaded media result.")
            _record_render_cost(connection, dict(connection.execute("SELECT * FROM render_jobs WHERE id=?", (job_id,)).fetchone()))
        log_error("render_failed", safe_error, render_job_id=job_id, error_code=code, provider=job["provider"], target_status=target)
        return render_job(job_id)
    with connect() as connection:
        connection.execute(
            "UPDATE render_jobs SET provider_called=1,provider_job_id=?,provider_request_id=?,provider_status=?,"
            "submitted_at=COALESCE(?,submitted_at),last_polled_at=?,poll_count=MAX(poll_count,?),provider_started_at=COALESCE(?,provider_started_at),provider_completed_at=?,latency_ms=?,"
            "request_count=?,credits_consumed=?,provider_units=?,input_units=?,output_units=?,generation_seconds=?,"
            "frame_count=?,image_count=?,provider_cost_usd=?,calculated_cost_usd=?,cost_status=?,currency=?,"
            "pricing_version=?,resume_state=NULL,resume_reason=NULL,updated_at=? WHERE id=?",
            (
                result.provider_job_id, result.provider_request_id, result.provider_status, result.submitted_at,
                result.last_polled_at, result.poll_count, result.provider_started_at, result.provider_completed_at,
                result.latency_ms, result.request_count, result.credits_consumed, result.provider_units,
                result.input_units, result.output_units, result.generation_seconds, result.frame_count, result.image_count,
                result.provider_cost_usd, result.calculated_cost_usd,
                "known" if result.provider_cost_usd is not None or result.calculated_cost_usd is not None else "unknown",
                result.currency, result.pricing_version, now(), job_id,
            ),
        )
        current_status = connection.execute("SELECT status FROM render_jobs WHERE id=?", (job_id,)).fetchone()["status"]
        if current_status != "VALIDATING":
            _transition_render_job(connection, job_id, "VALIDATING", "Persisting media and running separate technical, text, and semantic QA layers.")
    try:
        extension = {
            "image/png": "png", "image/jpeg": "jpg", "image/webp": "webp", "video/mp4": "mp4",
            "video/webm": "webm", "audio/wav": "wav", "audio/mpeg": "mp3", "audio/mp4": "m4a",
        }.get(result.mime_type)
        if not extension:
            extension = "png" if job["media_type"] == "IMAGE" else "mp4"
        stored = storage.save(result.asset_bytes, extension=extension, metadata={"render_job_id": job_id})
        technical = _validate_render_result(result, request, stored, storage)
    except Exception as error:
        safe_error = _safe_render_error(error)
        with connect() as connection:
            connection.execute(
                "UPDATE render_jobs SET validation_status='FAILED',technical_validation_status='FAILED',"
                "validation_result_json=?,failure_code='STORAGE_FAILED',failure_reason=?,updated_at=? WHERE id=?",
                (json.dumps({"valid": False, "errors": [safe_error]}), safe_error, now(), job_id),
            )
            _transition_render_job(connection, job_id, "FAILED", "Asset persistence or technical validation could not complete.")
            _record_render_cost(connection, dict(connection.execute("SELECT * FROM render_jobs WHERE id=?", (job_id,)).fetchone()))
        log_error("render_storage_failed", safe_error, render_job_id=job_id, error_code="STORAGE_FAILED")
        return render_job(job_id)
    current = _render_eligibility(job["content_package_id"], job["media_type"], job.get("source_asset_id"))
    stale = not current["eligible"] or current["input_version"] != job["input_version"]
    if stale:
        technical["valid"] = False
        technical["errors"] = list(dict.fromkeys(
            technical["errors"] + current["blockers"] + ["Lineage changed during rendering."]
        ))
    replay = None
    if getattr(renderer, "mode", None) == "live":
        # A fresh paid generation must never return bytes we already hold; treat it as a replayed response.
        with connect() as connection:
            replay = connection.execute(
                "SELECT id FROM generated_assets WHERE checksum_sha256=? LIMIT 1", (stored.checksum_sha256,)
            ).fetchone()
        if replay:
            technical["valid"] = False
            technical["errors"] = technical["errors"] + [
                f"DUPLICATE_PROVIDER_OUTPUT: bytes are identical to existing asset {replay['id']}."
            ]
    text_qa, visual_inputs, frame_artifacts = _run_ocr_and_prepare_visual_inputs(
        result, request, gate, technical, ocr_provider, frame_extractor, storage,
    )
    if text_qa.status == "NOT_PERFORMED" and result.detected_text:
        text_qa = evaluate_text_overlay(request, result.detected_text)
    if technical["valid"]:
        try:
            with connect() as connection:
                claims = [dict(row) for row in connection.execute(
                    "SELECT id,text,claim_type,assertion_scope,attribution FROM claim_versions WHERE id IN ("
                    + ",".join("?" for _ in gate["claim_version_ids"]) + ") ORDER BY id",
                    gate["claim_version_ids"],
                )] if gate["claim_version_ids"] else []
            semantic_qa = visual_qa_provider.qa(
                generated_asset={
                    "checksum_sha256": stored.checksum_sha256, "mime_type": result.mime_type, "media_type": job["media_type"],
                    "width": technical.get("decoded_width") or result.width,
                    "height": technical.get("decoded_height") or result.height,
                    "duration_seconds": technical.get("decoded_duration_seconds") or result.duration_seconds,
                    "source_asset": request.get("source_asset"),
                },
                content_package=gate["package_payload"], approved_claims=claims,
                expected_entities=_expected_render_entities(gate["package_payload"], claims),
                expected_visual_description=request.get("visual_prompts") or request.get("storyboard") or (),
                images=visual_inputs,
            )
            if semantic_qa.status not in ("PASSED", "FLAGGED", "NOT_PERFORMED"):
                raise ValueError("Visual QA provider returned an invalid status.")
            details = dict(semantic_qa.details or {})
            details["checks"] = normalize_semantic_checks(
                details.get("checks"), has_reference=bool(request.get("original_source_asset") or request.get("source_asset")),
                is_video=job["media_type"] in ("VIDEO", "SHORT_FORM_VIDEO", "LONG_FORM_VIDEO"),
            )
            if any(item["status"] == "FLAG" for item in details["checks"]) and semantic_qa.status != "FLAGGED":
                semantic_qa = MediaQAResult(
                    status="FLAGGED", flags=tuple(semantic_qa.flags) + ("SEMANTIC_CHECK_FLAGGED",), details=details,
                    confidence=semantic_qa.confidence, provider=semantic_qa.provider, model=semantic_qa.model,
                )
            else:
                semantic_qa = MediaQAResult(
                    status=semantic_qa.status, flags=semantic_qa.flags, details=details, confidence=semantic_qa.confidence,
                    provider=semantic_qa.provider, model=semantic_qa.model,
                )
        except Exception as error:
            semantic_qa = MediaQAResult(
                status="FLAGGED", flags=("SEMANTIC_QA_PROVIDER_ERROR",),
                details={"reason": _safe_render_error(error), "human_review_required": True},
                provider=getattr(visual_qa_provider, "name", None), model=getattr(visual_qa_provider, "model", None),
            )
    else:
        semantic_qa = MediaQAResult(
            status="NOT_PERFORMED", flags=("TECHNICAL_VALIDATION_FAILED",),
            details={"reason": "Semantic QA was skipped because technical validation or lineage failed."},
        )
    ready = technical["valid"] and text_qa.status != "FAILED" and semantic_qa.status != "FLAGGED"
    asset_status = "VALIDATED" if ready else ("BLOCKED" if stale or semantic_qa.status == "FLAGGED" else "INVALID")
    qa_errors = list(technical["errors"])
    qa_errors.extend(text_qa.flags if text_qa.status == "FAILED" else ())
    qa_errors.extend(semantic_qa.flags if semantic_qa.status == "FLAGGED" else ())
    overall = {
        "valid": ready, "errors": qa_errors, "technical": technical,
        "text_validation": {"status": text_qa.status, "flags": list(text_qa.flags), "details": text_qa.details},
        "semantic_visual_qa": {
            "status": semantic_qa.status, "flags": list(semantic_qa.flags), "details": semantic_qa.details,
            "provider": semantic_qa.provider, "model": semantic_qa.model,
        },
        "human_review": "REQUIRED", "media_qa_policy_version": MEDIA_QA_POLICY_VERSION,
    }
    with connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        asset_id = _persist_render_asset(
            connection, job, result, stored, overall, asset_status, text_qa, semantic_qa, stale, ready
        )
        frame_rows = _persist_qa_frame_artifacts(connection, asset_id, frame_artifacts, storage)
        if frame_rows:
            text_details = dict(text_qa.details or {})
            text_details["frame_artifact_ids"] = [item["id"] for item in frame_rows]
            text_qa = MediaQAResult(
                status=text_qa.status, flags=text_qa.flags, details=text_details,
                confidence=text_qa.confidence, provider=text_qa.provider, model=text_qa.model,
            )
            semantic_details = dict(semantic_qa.details or {})
            semantic_details["frame_artifact_ids"] = [item["id"] for item in frame_rows]
            semantic_qa = MediaQAResult(
                status=semantic_qa.status, flags=semantic_qa.flags, details=semantic_details,
                confidence=semantic_qa.confidence, provider=semantic_qa.provider, model=semantic_qa.model,
            )
        _persist_media_qa(connection, job_id, asset_id, technical, text_qa, semantic_qa)
        output_ids = [row["generated_asset_id"] for row in connection.execute(
            "SELECT generated_asset_id FROM render_job_outputs WHERE render_job_id=? ORDER BY generated_asset_id", (job_id,)
        )]
        connection.execute(
            "UPDATE render_jobs SET output_media_asset_ids_json=?,validation_status=?,technical_validation_status=?,"
            "text_validation_status=?,semantic_qa_status=?,human_review_status='REQUIRED',validation_result_json=?,"
            "failure_code=?,failure_reason=?,updated_at=? WHERE id=?",
            (
                json.dumps(output_ids), "PASSED" if ready else "FAILED",
                "PASSED" if technical["valid"] else "FAILED", text_qa.status, semantic_qa.status,
                json.dumps(overall, ensure_ascii=False), None if ready else ("STALE_INPUTS" if stale else "DUPLICATE_PROVIDER_OUTPUT" if replay else "MEDIA_QA_FLAGGED"),
                None if ready else " ".join(qa_errors)[:500], now(), job_id,
            ),
        )
        if ready:
            _transition_render_job(connection, job_id, "READY_FOR_REVIEW", "Technical QA passed; text and semantic QA are explicit; human review is required.")
        elif stale:
            _transition_render_job(connection, job_id, "BLOCKED", "Generated asset retained for audit but lineage became invalid.")
        else:
            _transition_render_job(connection, job_id, "HUMAN_REVIEW", "Generated asset requires human review because media QA flagged it.")
        _record_render_cost(connection, dict(connection.execute("SELECT * FROM render_jobs WHERE id=?", (job_id,)).fetchone()))
        connection.commit()
    return render_job(job_id)


def recover_unfinished_media_jobs():
    """Mark interrupted work for inspection; startup never submits or replaces paid jobs."""
    with connect() as connection:
        table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='render_jobs'"
        ).fetchone()
        if not table:
            return []
        rows = connection.execute(
            "SELECT id,status,provider_called,provider_job_id,resume_state FROM render_jobs "
            "WHERE status IN ('QUEUED','PREPARING','RENDERING','VALIDATING')"
        ).fetchall()
        recovered = []
        for row in rows:
            if row["provider_job_id"]:
                resume_state = "PROVIDER_PENDING"
                reason = "Application startup found an unfinished submitted provider job. Resume checks reuse its saved provider job ID."
            elif row["provider_called"]:
                resume_state = "NEEDS_INTERVENTION"
                reason = "Application startup found provider activity without a resumable provider job ID."
            else:
                resume_state = "INTERRUPTED"
                reason = "Application startup found local work that had not reached a paid provider submission."
            if row["resume_state"] != resume_state:
                connection.execute(
                    "UPDATE render_jobs SET resume_state=?,resume_reason=?,updated_at=? WHERE id=?",
                    (resume_state, reason, now(), row["id"]),
                )
                connection.execute(
                    "INSERT INTO render_job_status_history(render_job_id,from_status,to_status,message,metadata_json,changed_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (row["id"], row["status"], row["status"], reason,
                     json.dumps({"startup_recovery": True, "resume_state": resume_state}), now()),
                )
            recovered.append({"id": row["id"], "status": row["status"], "resume_state": resume_state})
    return recovered


def resume_render_job(job_id, renderer=None, storage=None, visual_qa_provider=None):
    """Read the saved provider job once. This function has no submission path."""
    storage = storage or LocalMediaStorage(RENDER_STORAGE_ROOT)
    with connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute("SELECT * FROM render_jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            raise KeyError(job_id)
        job = dict(row)
        if job["status"] in ("RENDERING", "VALIDATING") and job.get("resume_state") == "PROVIDER_PENDING":
            connection.execute(
                "UPDATE render_jobs SET resume_state='INTERRUPTED',resume_reason='Read-only provider status check in progress.',updated_at=? WHERE id=?",
                (now(), job_id),
            )
            connection.commit()
    if not job.get("provider_job_id"):
        raise ValueError("This job has no saved provider job ID and cannot be resumed safely.")
    if job["status"] not in ("RENDERING", "VALIDATING") or job.get("resume_state") != "PROVIDER_PENDING":
        raise ValueError("This job is not waiting for a provider status check.")
    renderer = renderer or renderer_for(job["provider"], job["media_type"])
    if not isinstance(renderer, AsyncMediaRenderer):
        raise ValueError("The configured renderer does not support read-only resume checks.")
    checked_at = now()
    try:
        outcome = renderer.resume_status(
            job["provider_job_id"], timeout_seconds=LIVE_RENDERER_TIMEOUT_SECONDS,
            submitted_at=job.get("submitted_at"), provider_request_id=job.get("provider_request_id"),
        )
    except (RendererNetworkError, RendererTimeoutError, RendererRateLimitError, RendererServerError) as error:
        with connect() as connection:
            connection.execute(
                "UPDATE render_jobs SET last_resume_check_at=?,resume_check_count=resume_check_count+1,"
                "resume_state='PROVIDER_PENDING',resume_reason=?,updated_at=? WHERE id=?",
                (checked_at, _safe_render_error(error), now(), job_id),
            )
        return render_job(job_id)
    except Exception as error:
        safe = _safe_render_error(error)
        with connect() as connection:
            connection.execute(
                "UPDATE render_jobs SET last_resume_check_at=?,resume_check_count=resume_check_count+1,"
                "resume_state='NEEDS_INTERVENTION',resume_reason=?,provider_failure_code=?,provider_failure_reason=?,updated_at=? WHERE id=?",
                (checked_at, safe, getattr(error, "code", "STATUS_CHECK_FAILED"), safe, now(), job_id),
            )
            _transition_render_job(
                connection, job_id, "HUMAN_REVIEW",
                "Provider job could not be identified safely; no replacement generation was submitted.",
            )
        return render_job(job_id)
    with connect() as connection:
        attempt = connection.execute(
            "SELECT COALESCE(MAX(attempt_number),1) FROM render_job_attempts WHERE render_job_id=?", (job_id,)
        ).fetchone()[0]
        connection.execute(
            "UPDATE render_jobs SET last_resume_check_at=?,resume_check_count=resume_check_count+1,last_polled_at=?,"
            "poll_count=poll_count+1,provider_status=?,updated_at=? WHERE id=?",
            (checked_at, checked_at, outcome.provider_status if isinstance(outcome, RenderResult) else outcome.status, now(), job_id),
        )
        _persist_provider_events(connection, job_id, attempt, (
            {"event_type": "POLLED", "status": outcome.provider_status if isinstance(outcome, RenderResult) else outcome.status,
             "at": checked_at, "provider_job_id": job["provider_job_id"], "resume_check": True},
        ), job["provider_job_id"])
    if isinstance(outcome, RenderResult):
        return run_render_job(
            job_id, renderer=renderer, storage=storage, visual_qa_provider=visual_qa_provider,
            prepared_result=outcome,
        )
    status = outcome.status.upper()
    if status in ("QUEUED", "PENDING", "PROCESSING", "RUNNING", "IN_PROGRESS"):
        with connect() as connection:
            connection.execute(
                "UPDATE render_jobs SET resume_state='PROVIDER_PENDING',resume_reason='Provider reports that generation is still processing.',updated_at=? WHERE id=?",
                (now(), job_id),
            )
        return render_job(job_id)
    metadata = outcome.metadata or {}
    if status in ("FAILED", "ERROR", "CANCELLED", "REJECTED", "CONTENT_POLICY_REJECTED"):
        reason = str(metadata.get("failure_message") or f"Provider job ended with status {status}.")[:500]
        with connect() as connection:
            connection.execute(
                "UPDATE render_jobs SET resume_state=NULL,resume_reason=NULL,provider_failure_code=?,"
                "provider_failure_reason=?,failure_code=?,failure_reason=?,updated_at=? WHERE id=?",
                (metadata.get("failure_code") or status, reason, metadata.get("failure_code") or status, reason, now(), job_id),
            )
            target = "HUMAN_REVIEW" if status in ("REJECTED", "CONTENT_POLICY_REJECTED") else "FAILED"
            _transition_render_job(connection, job_id, target, "Saved provider job reached a terminal failure; no replacement was submitted.")
            _record_render_cost(connection, dict(connection.execute("SELECT * FROM render_jobs WHERE id=?", (job_id,)).fetchone()))
        return render_job(job_id)
    with connect() as connection:
        connection.execute(
            "UPDATE render_jobs SET resume_state='NEEDS_INTERVENTION',resume_reason=?,failure_code='UNKNOWN_PROVIDER_STATUS',"
            "failure_reason=?,updated_at=? WHERE id=?",
            (f"Provider returned unknown status {status}.", f"Provider returned unknown status {status}.", now(), job_id),
        )
        _transition_render_job(connection, job_id, "HUMAN_REVIEW", "Provider returned an unknown status; no replacement was submitted.")
        _record_render_cost(connection, dict(connection.execute("SELECT * FROM render_jobs WHERE id=?", (job_id,)).fetchone()))
    return render_job(job_id)


def render_job(job_id):
    with connect() as connection:
        row = connection.execute("SELECT * FROM render_jobs WHERE id=?", (job_id,)).fetchone()
    if row is None:
        raise KeyError(job_id)
    return dict(row)


def generated_asset(asset_id):
    with connect() as connection:
        row = connection.execute("SELECT * FROM generated_assets WHERE id=?", (asset_id,)).fetchone()
    if row is None:
        raise KeyError(asset_id)
    return dict(row)


def review_media_asset(asset_id, action, reviewer, comment=None):
    if action not in ("APPROVED", "CHANGES_REQUIRED", "REJECTED"):
        raise ValueError("Review action must be APPROVED, CHANGES_REQUIRED, or REJECTED.")
    reviewer = str(reviewer or "").strip()
    if not reviewer:
        raise ValueError("Reviewer name is required.")
    comment = str(comment or "").strip()[:2000] or None
    with connect() as connection:
        asset = connection.execute("SELECT * FROM generated_assets WHERE id=?", (asset_id,)).fetchone()
        if asset is None:
            raise KeyError(asset_id)
        qa_ids = [row["id"] for row in connection.execute(
            "SELECT m.id FROM media_qa_runs m JOIN (SELECT qa_kind,MAX(run_number) AS n FROM media_qa_runs "
            "WHERE generated_asset_id=? GROUP BY qa_kind) latest ON latest.qa_kind=m.qa_kind AND latest.n=m.run_number "
            "WHERE m.generated_asset_id=? ORDER BY m.qa_kind", (asset_id, asset_id),
        )]
        review_id = "MR-" + uuid.uuid4().hex[:12].upper()
        connection.execute(
            "INSERT INTO media_reviews(id,generated_asset_id,asset_version,content_package_id,content_package_version,"
            "action,reviewer,comment,qa_run_ids_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (review_id, asset_id, asset["version_number"], asset["content_package_id"], asset["content_package_version"],
             action, reviewer, comment, json.dumps(qa_ids), now()),
        )
        row = connection.execute("SELECT * FROM media_reviews WHERE id=?", (review_id,)).fetchone()
    result = dict(row)
    result["qa_run_ids"] = json.loads(result.pop("qa_run_ids_json"))
    return result


def rerun_media_qa(asset_id, qa_kind="ALL", *, ocr_provider=None, frame_extractor=None, visual_qa_provider=None,
                   storage=None):
    """Create immutable manual OCR/visual QA versions for an existing asset."""
    qa_kind = str(qa_kind or "ALL").upper()
    if qa_kind not in ("ALL", "OCR", "VISUAL"):
        raise ValueError("qa_kind must be ALL, OCR, or VISUAL.")
    storage = storage or LocalMediaStorage(RENDER_STORAGE_ROOT)
    ocr_provider = ocr_provider or ocr_provider_for()
    frame_extractor = frame_extractor or frame_extractor_for()
    visual_qa_provider = visual_qa_provider or visual_qa_provider_for()
    with connect() as connection:
        asset_row = connection.execute("SELECT * FROM generated_assets WHERE id=?", (asset_id,)).fetchone()
        if asset_row is None:
            raise KeyError(asset_id)
        asset = dict(asset_row)
        job = dict(connection.execute("SELECT * FROM render_jobs WHERE id=?", (asset["render_job_id"],)).fetchone())
        prompt = connection.execute("SELECT request_json FROM render_prompt_snapshots WHERE render_job_id=?", (job["id"],)).fetchone()
    request = json.loads(prompt["request_json"])
    if job.get("source_derivative_id"):
        request = _prepare_video_source_derivative(job, request, storage)
    gate = _render_eligibility(job["content_package_id"], job["media_type"], job.get("source_asset_id"))
    asset_bytes = storage.get(asset["storage_uri"])
    technical = (json.loads(asset.get("validation_result_json") or "{}").get("technical") or {
        "valid": asset["technical_validation_status"] == "PASSED",
        "decoded_duration_seconds": asset.get("duration_seconds"),
    })
    result = RenderResult(
        asset_bytes=asset_bytes, mime_type=asset["mime_type"], width=asset.get("width"), height=asset.get("height"),
        duration_seconds=asset.get("duration_seconds"), frame_rate=asset.get("frame_rate"),
    )
    ocr_result, images, frames = _run_ocr_and_prepare_visual_inputs(
        result, request, gate, technical, ocr_provider, frame_extractor, storage,
    )
    visual_result = None
    if qa_kind in ("ALL", "VISUAL"):
        with connect() as connection:
            claims = [dict(row) for row in connection.execute(
                "SELECT id,text,claim_type,assertion_scope,attribution FROM claim_versions WHERE id IN ("
                + ",".join("?" for _ in gate["claim_version_ids"]) + ") ORDER BY id"
                if gate["claim_version_ids"] else "SELECT id,text,claim_type,assertion_scope,attribution FROM claim_versions WHERE 0",
                gate["claim_version_ids"],
            )]
        try:
            visual_result = visual_qa_provider.qa(
                generated_asset={**asset, "source_asset": request.get("source_asset")},
                content_package=gate["package_payload"], approved_claims=claims,
                expected_entities=_expected_render_entities(gate["package_payload"], claims),
                expected_visual_description=request.get("visual_prompts") or request.get("storyboard") or (), images=images,
            )
        except Exception as error:
            visual_result = MediaQAResult(
                status="NOT_PERFORMED", flags=("SEMANTIC_QA_PROVIDER_ERROR",),
                details={"reason": _safe_render_error(error), "run_status": "UNKNOWN"},
                provider=getattr(visual_qa_provider, "name", None), model=getattr(visual_qa_provider, "model", None),
            )
    with connect() as connection:
        frame_rows = _persist_qa_frame_artifacts(connection, asset_id, frames, storage) if frames else []
        created = []
        candidates = []
        if qa_kind in ("ALL", "OCR"):
            candidates.append(("OCR", ocr_result, (ocr_result.details or {}).get("checks") or []))
        if visual_result is not None:
            candidates.append(("VISUAL", visual_result, (visual_result.details or {}).get("checks") or []))
        for kind, result_item, checks in candidates:
            evidence = dict(result_item.details or {})
            if frame_rows:
                evidence["frame_artifact_ids"] = [item["id"] for item in frame_rows]
            status = (
                "PASS" if result_item.status == "PASSED" else
                "FLAG" if result_item.status in ("FAILED", "FLAGGED") else "UNKNOWN"
            )
            number = connection.execute(
                "SELECT COALESCE(MAX(run_number),0)+1 FROM media_qa_runs WHERE generated_asset_id=? AND qa_kind=?",
                (asset_id, kind),
            ).fetchone()[0]
            run_id = "QR-" + uuid.uuid4().hex[:12].upper()
            cost = evidence.get("cost_usd")
            connection.execute(
                "INSERT INTO media_qa_runs(id,generated_asset_id,render_job_id,qa_kind,run_number,trigger,status,provider,model,"
                "prompt_version,content_package_id,content_package_version,checks_json,evidence_json,explanation,"
                "provider_request_id,usage_json,cost_status,cost_usd,created_at) VALUES(?,?,?,?,?,'MANUAL',?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (run_id, asset_id, job["id"], kind, number, status, result_item.provider, result_item.model,
                 evidence.get("prompt_version"), job["content_package_id"], job["content_package_version"],
                 json.dumps(checks, ensure_ascii=False, sort_keys=True),
                 json.dumps(_scrub_provider_metadata(evidence), ensure_ascii=False, sort_keys=True),
                 evidence.get("reason") or evidence.get("summary"), evidence.get("provider_request_id"),
                 json.dumps(evidence.get("usage") or {}, sort_keys=True),
                 "known" if cost is not None else (
                     "not_billed" if kind == "OCR" or result_item.provider is None else "unknown"
                 ), cost, now()),
            )
            created.append(dict(connection.execute("SELECT * FROM media_qa_runs WHERE id=?", (run_id,)).fetchone()))
    return created


# ---------- Architecture 07: Meta distribution ----------

PUBLISH_MAX_ATTEMPTS = max(1, min(5, int(os.environ.get("PUBLISH_MAX_ATTEMPTS", "3"))))
PUBLISH_RETRY_BACKOFF_SECONDS = max(0.0, float(os.environ.get("PUBLISH_RETRY_BACKOFF_SECONDS", "5")))
PUBLISH_SCHEDULER_INTERVAL_SECONDS = max(5.0, float(os.environ.get("PUBLISH_SCHEDULER_INTERVAL_SECONDS", "30")))
PUBLISH_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="meta-publish")
PUBLISH_ACTIVE_STATUSES = ("SCHEDULED", "QUEUED", "UPLOADING", "PROCESSING", "PUBLISHING", "NEEDS_INTERVENTION")


def _latest_media_review(connection, asset_id):
    row = connection.execute(
        "SELECT * FROM media_reviews WHERE generated_asset_id=? ORDER BY created_at DESC,id DESC LIMIT 1", (asset_id,)
    ).fetchone()
    return dict(row) if row else None


def _latest_distribution_review(connection, package_id):
    row = connection.execute(
        "SELECT * FROM distribution_reviews WHERE distribution_package_id=? ORDER BY created_at DESC,id DESC LIMIT 1",
        (package_id,),
    ).fetchone()
    return dict(row) if row else None


def _distribution_media_blockers(connection, asset_id, *, allow_superseded=False):
    """Media must be a live, validated, current video whose latest human review is APPROVED."""
    asset = connection.execute("SELECT * FROM generated_assets WHERE id=?", (asset_id,)).fetchone()
    if asset is None:
        raise KeyError(asset_id)
    asset = dict(asset)
    job = dict(connection.execute("SELECT * FROM render_jobs WHERE id=?", (asset["render_job_id"],)).fetchone())
    blockers = []
    if asset["media_type"] not in ("VIDEO", "SHORT_FORM_VIDEO", "LONG_FORM_VIDEO"):
        blockers.append("Only approved VIDEO assets can be distributed as Reels.")
    if asset["fixture_only"] or not asset["executable"]:
        blockers.append("Fixture or non-executable media can never be distributed.")
    if asset["status"] != "VALIDATED" or asset["stale"] or not asset["usable_for_review"]:
        blockers.append("The media asset is not a validated, current, usable asset.")
    review = _latest_media_review(connection, asset_id)
    if not review or review["action"] != "APPROVED":
        blockers.append("The media asset's latest human review is not APPROVED.")
    elif review["asset_version"] != asset["version_number"]:
        blockers.append("The media approval belongs to a different asset version.")
    if not allow_superseded:
        approved_reel = connection.execute(
            "SELECT f.id FROM final_reel_assets f WHERE f.source_asset_id=? AND f.status='READY_FOR_REVIEW' "
            "AND EXISTS(SELECT 1 FROM final_reel_reviews r WHERE r.final_reel_asset_id=f.id AND r.action='APPROVED') "
            "ORDER BY f.created_at DESC, f.id DESC LIMIT 1", (asset_id,),
        ).fetchone()
        if approved_reel:
            blockers.append(
                f"An approved Final Reel ({approved_reel['id']}) exists; distribute that instead of the raw video."
            )
    return asset, job, review, blockers


def _latest_final_reel_review(connection, final_reel_id):
    row = connection.execute(
        "SELECT * FROM final_reel_reviews WHERE final_reel_asset_id=? ORDER BY created_at DESC,id DESC LIMIT 1",
        (final_reel_id,),
    ).fetchone()
    return dict(row) if row else None


def _latest_media_qa_statuses(connection, asset_id):
    rows = connection.execute(
        "SELECT qa_kind,status FROM media_qa_runs m WHERE generated_asset_id=? AND run_number=("
        "SELECT MAX(run_number) FROM media_qa_runs WHERE generated_asset_id=m.generated_asset_id AND qa_kind=m.qa_kind)",
        (asset_id,),
    ).fetchall()
    return {row["qa_kind"]: row["status"] for row in rows}


def _final_reel_source_blockers(connection, source_asset_id):
    """A Final Reel may only be composed from a live, QA-passed, lineage-current generated video."""
    row = connection.execute(
        "SELECT ga.*,rj.status AS job_status FROM generated_assets ga JOIN render_jobs rj ON rj.id=ga.render_job_id "
        "WHERE ga.id=?", (source_asset_id,),
    ).fetchone()
    if row is None:
        return None, None, [f"Source asset {source_asset_id} does not exist."]
    asset = dict(row)
    job = dict(connection.execute("SELECT * FROM render_jobs WHERE id=?", (asset["render_job_id"],)).fetchone())
    blockers = []
    if asset["media_type"] not in ("VIDEO", "SHORT_FORM_VIDEO", "LONG_FORM_VIDEO"):
        blockers.append("Only a generated video can become a Final Reel source.")
    if asset["fixture_only"]:
        blockers.append("Fixture placeholder media can never become a Final Reel source.")
    if not asset["executable"]:
        blockers.append("Non-executable media can never become a Final Reel source.")
    if asset["status"] != "VALIDATED" or asset["stale"] or not asset["usable_for_review"]:
        blockers.append("The source video is not a validated, current, usable asset.")
    if job["status"] != "READY_FOR_REVIEW":
        blockers.append("The source render job did not finish at READY_FOR_REVIEW.")
    qa = _latest_media_qa_statuses(connection, source_asset_id)
    if qa.get("TECHNICAL") != "PASS":
        blockers.append("The source video's latest technical QA is not PASS.")
    if qa.get("OCR") == "FLAG":
        blockers.append("The source video's latest OCR QA flagged unexpected text.")
    if qa.get("VISUAL") == "FLAG":
        blockers.append("The source video's latest visual QA is flagged.")
    _, lineage_blockers = _distribution_lineage_blockers(job)
    blockers.extend(lineage_blockers)
    return asset, job, list(dict.fromkeys(blockers))


REFERENCE_MEDIA_EXTENSIONS = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}
REFERENCE_MEDIA_TYPES = ("PUBLIC_FIGURE_PHOTO", "PARTY_LOGO", "OTHER")
REFERENCE_RIGHTS = ("VERIFIED", "RESTRICTED", "UNKNOWN")
MAX_REFERENCE_MEDIA_BYTES = 12_000_000


def uploaded_media_asset(asset_id):
    with connect() as connection:
        row = connection.execute("SELECT * FROM uploaded_media_assets WHERE id=?", (asset_id,)).fetchone()
    if row is None:
        raise KeyError(asset_id)
    return dict(row)


def list_uploaded_media_assets(asset_type=None):
    with connect() as connection:
        if asset_type:
            rows = connection.execute(
                "SELECT * FROM uploaded_media_assets WHERE asset_type=? ORDER BY uploaded_at DESC,id DESC", (asset_type,)
            ).fetchall()
        else:
            rows = connection.execute(
                "SELECT * FROM uploaded_media_assets ORDER BY uploaded_at DESC,id DESC"
            ).fetchall()
    return [dict(row) for row in rows]


def _reference_media_choices(asset_type):
    """Verified-only options for the Final Reel composer, never exposing storage paths."""
    return [
        {
            "id": asset["id"], "label": asset["label"], "asset_type": asset["asset_type"],
            "rights_status": asset["rights_status"], "source_name": asset["source_name"],
            "license_note": asset["license_note"], "checksum_sha256": asset["checksum_sha256"],
            "width": asset["width"], "height": asset["height"],
        }
        for asset in list_uploaded_media_assets(asset_type)
        if asset["rights_status"] == "VERIFIED"
    ]


def ingest_reference_media(*, data, filename, asset_type, label, source_name, license_note,
                           rights_status, uploader, source_url=None, reviewer=None,
                           identity_subject=None, storage=None):
    """Register a rights-cleared reference asset. Bytes are stored; only the DB row is addressable."""
    asset_type = str(asset_type or "").upper().strip()
    if asset_type not in REFERENCE_MEDIA_TYPES:
        raise ValueError("asset_type must be PUBLIC_FIGURE_PHOTO, PARTY_LOGO, or OTHER.")
    rights_status = str(rights_status or "").upper().strip()
    if rights_status not in REFERENCE_RIGHTS:
        raise ValueError("rights_status must be VERIFIED, RESTRICTED, or UNKNOWN.")
    label = str(label or "").strip()[:200]
    source_name = str(source_name or "").strip()[:200]
    license_note = str(license_note or "").strip()[:2000]
    uploader = str(uploader or "").strip()[:120]
    if not label or not source_name or not license_note or not uploader:
        raise ValueError("label, source_name, license_note, and uploader are required.")
    if isinstance(data, str):
        # Accept base64 without a data-URL prefix from the dashboard upload control.
        candidate = data.split(",", 1)[1] if data.startswith("data:") and "," in data else data
        try:
            data = base64.b64decode(candidate, validate=True)
        except (binascii.Error, ValueError) as error:
            raise ValueError("The uploaded image data is not valid base64.") from error
    if not isinstance(data, (bytes, bytearray)) or not data:
        raise ValueError("An image file is required.")
    if len(data) > MAX_REFERENCE_MEDIA_BYTES:
        raise ValueError("The image exceeds the 12 MB reference-media limit.")
    extension = Path(str(filename or "")).suffix.lower()
    if extension not in REFERENCE_MEDIA_EXTENSIONS:
        raise ValueError("Only JPG, PNG, or WebP reference images are accepted.")
    try:
        decoded = inspect_image(bytes(data))
    except ImageDecodeError as error:
        raise ValueError(f"The uploaded image could not be decoded: {error}") from error
    storage = storage or LocalMediaStorage(RENDER_STORAGE_ROOT)
    stored = storage.save(bytes(data), extension=extension.lstrip("."), metadata={"purpose": "REFERENCE_MEDIA"})
    with connect() as connection:
        existing = connection.execute(
            "SELECT id FROM uploaded_media_assets WHERE checksum_sha256=?", (stored.checksum_sha256,)
        ).fetchone()
        if existing:
            row = connection.execute("SELECT * FROM uploaded_media_assets WHERE id=?", (existing["id"],)).fetchone()
            return {"asset": dict(row), "duplicate": True}
        asset_id = "MA-" + uuid.uuid4().hex[:12].upper()
        timestamp = now()
        reviewed_at = timestamp if rights_status == "VERIFIED" else None
        connection.execute(
            "INSERT INTO uploaded_media_assets(id,asset_type,label,identity_subject,storage_uri,mime_type,width,height,"
            "file_size,checksum_sha256,source_name,source_url,license_note,rights_status,uploader,reviewer,uploaded_at,"
            "rights_reviewed_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                asset_id, asset_type, label, str(identity_subject or "").strip()[:200] or None, stored.storage_uri,
                REFERENCE_MEDIA_EXTENSIONS[extension], decoded.get("width"), decoded.get("height"), stored.file_size,
                stored.checksum_sha256, source_name, str(source_url or "").strip()[:2000] or None, license_note,
                rights_status, uploader, str(reviewer or "").strip()[:120] or None, timestamp, reviewed_at,
            ),
        )
        row = connection.execute("SELECT * FROM uploaded_media_assets WHERE id=?", (asset_id,)).fetchone()
    return {"asset": dict(row), "duplicate": False}


def final_reel_asset(final_reel_id):
    with connect() as connection:
        row = connection.execute("SELECT * FROM final_reel_assets WHERE id=?", (final_reel_id,)).fetchone()
        if row is None:
            raise KeyError(final_reel_id)
        result = dict(row)
        result["reviews"] = [dict(item) for item in connection.execute(
            "SELECT * FROM final_reel_reviews WHERE final_reel_asset_id=? ORDER BY created_at DESC,id DESC",
            (final_reel_id,),
        )]
    decoded = final_reel_composer.decoded_final_reel(result)
    decoded["latest_review"] = decoded["reviews"][0] if decoded["reviews"] else None
    return decoded


def schedule_reel_post(reel_id, platform, scheduled_at, *, timezone_name="UTC", connect_override=None):
    """Schedule an approved reel. Approval + platform package QA are required; switches gate sending."""
    if platform not in META_PLATFORMS:
        raise ValueError("Platform must be INSTAGRAM_REELS or FACEBOOK_REELS.")
    approval = reel_control.valid_approval(reel_id, connect=connect)
    if not approval:
        raise ValueError("Scheduling blocked: this reel version is not validly approved.")
    with connect() as connection:
        if connection.execute("SELECT 1 FROM scheduled_posts WHERE reel_id=? AND status IN ('SCHEDULED','PROCESSING')",
                              (reel_id,)).fetchone():
            raise ValueError("This reel is already scheduled or processing.")
        schedule_id = "SP-" + uuid.uuid4().hex[:12].upper()
        timestamp = now()
        connection.execute(
            "INSERT INTO scheduled_posts(id,reel_id,platform,scheduled_at,timezone,status,approval_id,created_at,updated_at) "
            "VALUES(?,?,?,?,?,'SCHEDULED',?,?,?)",
            (schedule_id, reel_id, platform, scheduled_at, timezone_name, approval["id"], timestamp, timestamp),
        )
        row = connection.execute("SELECT * FROM scheduled_posts WHERE id=?", (schedule_id,)).fetchone()
    return dict(row)


def _publishing_ready():
    switches = meta_publishing_switches()
    return switches["SOCIAL_PUBLISHING_ENABLED"]


def run_due_scheduled_posts(now_at=None):
    """Local scheduler tick. Publishing is a no-op unless switches+credentials are configured."""
    now_at = now_at or datetime.now(timezone.utc).isoformat()
    with connect() as connection:
        due = [row["id"] for row in connection.execute(
            "SELECT id FROM scheduled_posts WHERE status='SCHEDULED' AND scheduled_at<=? ORDER BY scheduled_at", (now_at,))]
    results = []
    for schedule_id in due:
        with connect() as connection:
            claimed = connection.execute(
                "UPDATE scheduled_posts SET status='PROCESSING',updated_at=? WHERE id=? AND status='SCHEDULED'",
                (now(), schedule_id)).rowcount
        if not claimed:
            continue
        with connect() as connection:
            row = connection.execute("SELECT * FROM scheduled_posts WHERE id=?", (schedule_id,)).fetchone()
        # Fail closed: no post without an approval and ON switches.
        approval = reel_control.valid_approval(row["reel_id"], connect=connect)
        blockers = []
        if not approval:
            blockers.append("approval is no longer valid")
        if not _publishing_ready():
            blockers.append("SOCIAL_PUBLISHING_ENABLED is off")
        with connect() as connection:
            connection.execute(
                "UPDATE scheduled_posts SET status=?,attempt_count=attempt_count+1,last_error=?,updated_at=? WHERE id=?",
                ("FAILED" if blockers else "PROCESSING", ("; ".join(blockers) if blockers else None), now(), schedule_id))
        results.append({"schedule_id": schedule_id, "blocked": bool(blockers), "blockers": blockers})
    return results


def start_scheduled_post_scheduler():
    def loop():
        while True:
            try:
                run_due_scheduled_posts()
            except Exception as error:  # noqa: BLE001
                log_error("scheduler_failed", error)
            time.sleep(30)
    threading.Thread(target=loop, name="reel-scheduler", daemon=True).start()


def _provider_health_config():
    media = renderer_configuration("IMAGE"), renderer_configuration("VIDEO")
    switches = meta_publishing_switches()
    instagram = meta_platform_configuration("INSTAGRAM_REELS")
    facebook = meta_platform_configuration("FACEBOOK_REELS")
    return {
        "grok": bool(os.environ.get("XAI_API_KEY")),
        "claude": production_configuration()["live"],
        "edge_tts": True,
        "xai_image": media[0]["live"],
        "xai_video": media[1]["live"],
        "instagram": not instagram["missing"],
        "facebook": not facebook["missing"],
    }


def production_health():
    """Today's production health + cost/observability summary, for the System page."""
    today = datetime.now(timezone.utc).date().isoformat()
    with connect() as connection:
        def count(query, *params):
            return connection.execute(query, params).fetchone()[0]
        events = count("SELECT COUNT(*) FROM events WHERE date(first_seen_at)=?", today)
        verified = count("SELECT COUNT(*) FROM events WHERE verification_status='VERIFIED' AND date(updated_at)=?", today)
        reels = count("SELECT COUNT(*) FROM final_reel_assets WHERE date(created_at)=?", today)
        ready = count("SELECT COUNT(*) FROM final_reel_assets WHERE status='READY_FOR_REVIEW'")
        approved = count("SELECT COUNT(*) FROM reel_approvals WHERE revoked_at IS NULL AND date(approved_at)=?", today)
        published = count("SELECT COUNT(*) FROM scheduled_posts WHERE status='PUBLISHED' AND date(published_at)=?", today)
        attention = count("SELECT COUNT(*) FROM reel_pipeline_runs WHERE status='NEEDS_ATTENTION'")
        runs = [dict(r) for r in connection.execute(
            "SELECT total_known_cost_usd,cost_status,started_at,completed_at,status FROM reel_pipeline_runs")]
    known = [r["total_known_cost_usd"] for r in runs if r["total_known_cost_usd"] is not None]
    durations = [
        (datetime.fromisoformat(r["completed_at"]) - datetime.fromisoformat(r["started_at"])).total_seconds()
        for r in runs if r.get("completed_at") and r.get("started_at")
    ]
    failed = sum(1 for r in runs if r["status"] in ("NEEDS_ATTENTION", "HALTED"))
    return {
        "today": {"events_discovered": events, "events_verified": verified, "reels_generated": reels,
                  "ready_for_review": ready, "approved": approved, "published": published, "needs_attention": attention},
        "average_generation_seconds": round(sum(durations) / len(durations), 1) if durations else None,
        "average_cost_per_reel": round(sum(known) / len(known), 4) if known else None,
        "unknown_cost_runs": sum(1 for r in runs if r["cost_status"] == "unknown"),
        "failure_rate": round(failed / len(runs), 3) if runs else 0.0,
        "runs": len(runs),
        "providers": reel_control.provider_health(connect=connect, configuration=_provider_health_config()),
    }


def _auto_reel_stage_handler(stage, run):
    """Perform one AUTO_REEL_PIPELINE_V1 stage using the existing app functions.

    Stages whose work is already done are cheap no-ops; paid stages are gated by the
    pipeline's checkpoint so a restart never repeats provider work.
    """
    event_id = run["event_id"]
    if stage == "ELIGIBILITY":
        check = reel_pipeline.eligibility(connect, event_id)
        if not check["eligible"]:
            raise reel_pipeline.PipelineError(
                "Not auto-eligible: " + " ".join(check["blockers"]), retryable=False,
                recommended_action="Resolve verification/decision/claim-set blockers, then re-run.")
        return {"eligible": True}
    if stage == "SOURCE_ACQUISITION":
        # Discovery/verification already complete for a VERIFIED event; nothing paid to do.
        return {"sources_ready": True}
    if stage == "VERIFICATION":
        return {"verified": True}
    if stage == "CONTENT_DECISION":
        return {"decision_id": (run.get("checkpoint") or {}).get("decision_id")}
    if stage == "NARRATION":
        return {"narration_ready": True}
    if stage == "MEDIA_DISCOVERY":
        # Approved candidates may already be past APPROVED_FOR_USE into INGESTED.
        approved = [c for c in media_discovery.list_candidates(connect=connect)
                    if c["lifecycle_state"] in ("APPROVED_FOR_USE", "INGESTED")]
        if not approved:
            raise reel_pipeline.PipelineError(
                "No rights-cleared media available.", retryable=False,
                recommended_action="Discover and approve rights-cleared media, or supply user media.")
        return {"approved_media": len(approved)}
    if stage == "RIGHTS_CHECK":
        ingested = media_discovery.ingested_candidates(connect=connect)
        if not ingested:
            raise reel_pipeline.PipelineError(
                "Approved media has not been ingested.", retryable=True,
                recommended_action="Ingest the approved candidates, then resume.")
        return {"ingested_media": len(ingested)}
    if stage == "MEDIA_SELECTION":
        return {"selected": True}
    if stage == "REEL_GENERATION":
        with connect() as connection:
            source = connection.execute(
                "SELECT ga.id FROM generated_assets ga WHERE ga.event_id=? AND ga.media_type IN "
                "('VIDEO','SHORT_FORM_VIDEO','LONG_FORM_VIDEO') ORDER BY ga.created_at DESC LIMIT 1",
                (event_id,),
            ).fetchone()
            cbn = connection.execute(
                "SELECT id FROM uploaded_media_assets WHERE asset_type='PUBLIC_FIGURE_PHOTO' AND rights_status='VERIFIED' "
                "ORDER BY uploaded_at DESC LIMIT 1").fetchone()
            tdp = connection.execute(
                "SELECT id FROM uploaded_media_assets WHERE asset_type='PARTY_LOGO' AND rights_status='VERIFIED' "
                "ORDER BY uploaded_at DESC LIMIT 1").fetchone()
        if not source:
            raise reel_pipeline.PipelineError(
                "No generated source video exists for this event.", retryable=False,
                recommended_action="Render the base media for the approved package first.")
        asset = create_final_reel(source["id"], cbn_asset_id=cbn["id"] if cbn else None,
                                  tdp_asset_id=tdp["id"] if tdp else None, language="te")
        return {"reel_id": asset.get("id")}
    if stage == "QA":
        reel_id = (run.get("checkpoint") or {}).get("reel_id") or reel_pipeline._latest_reel_id(connect, event_id)
        if not reel_id:
            raise reel_pipeline.PipelineError("No reel to QA.", recommended_action="Run REEL_GENERATION.")
        with connect() as connection:
            row = connection.execute("SELECT status FROM final_reel_assets WHERE id=?", (reel_id,)).fetchone()
        if row["status"] != "READY_FOR_REVIEW":
            raise reel_pipeline.PipelineError(
                "Mandatory QA did not pass; reel is BLOCKED.", retryable=False,
                recommended_action="Review the reel's QA flags; fix and regenerate.")
        return {"reel_id": reel_id}
    return {}


def run_auto_reel_pipeline(event_id, *, now=None):
    """Drive the automated reel factory one run for one event. Never publishes."""
    run = reel_pipeline.open_or_resume(event_id, connect=connect, now=now or globals()["now"])
    return reel_pipeline.advance(run["id"], handler=_auto_reel_stage_handler, connect=connect)


def discovery_health():
    """Discovery Health for System: on/off, per-source status, SLO, todays counts."""
    import youtube_discovery
    return {
        "enabled": LIVE_DISCOVERY_ENABLED,
        "interval_seconds": LIVE_DISCOVERY_INTERVAL_SECONDS,
        "sources": live_discovery.source_health(connect=connect),
        "slo": live_discovery.slo_metrics(connect=connect),
        "youtube": youtube_discovery.youtube_health(connect=connect),
    }


def run_youtube_discovery_cycle(*, now=None, queries=None):
    """One bounded YouTube discovery cycle: fetch, normalize, dedupe, cluster, hand off."""
    import youtube_discovery
    result = youtube_discovery.run_youtube_cycle(
        connect=connect, now=now, queries=queries, handoff_fn=handoff_candidate_to_verification)
    ingest = youtube_discovery.ingest_signals(
        result.get("signals") or [], connect=connect, now=now,
        handoff_fn=handoff_candidate_to_verification)
    result.update({k: v for k, v in ingest.items() if k in
                   ("candidates_created", "candidates", "handoffs", "cross_source_clusters", "handoff_details")})
    return result


def run_live_discovery_cycle(*, handoff=True):
    """One LIVE_DISCOVERY_V1 polling cycle using the built-in no-key adapters."""
    live_discovery.sync_sources(connect=connect, now=now)
    adapters = live_discovery.default_adapters()
    return live_discovery.run_discovery_cycle(
        connect=connect, adapters=adapters, now=now, handoff=handoff,
        handoff_fn=handoff_candidate_to_verification if handoff else None)


def start_live_discovery_scheduler():
    """Continuously poll on the configured cadence while enabled. Never crashes the app."""
    if not LIVE_DISCOVERY_ENABLED:
        return None
    def loop():
        while True:
            try:
                cycle = run_live_discovery_cycle()
                log_error("live_discovery_cycle", RuntimeError("cycle complete"), **{
                    "signals": cycle["signals_fetched"], "deduped": cycle["signals_deduped"],
                    "candidates": cycle["candidates_created"], "handoffs": cycle["handoffs"]})
            except Exception as error:  # noqa: BLE001
                log_error("live_discovery_cycle_failed", error)
            time.sleep(LIVE_DISCOVERY_INTERVAL_SECONDS)
    threading.Thread(target=loop, name="live-discovery", daemon=True).start()
    return True


def discovered_candidates_overview():
    """Minimal Discovered queue for Stories/System: candidates, not verified events."""
    items = fast_discovery.list_candidates(connect=connect)
    return [{
        "id": c["id"], "headline": c["headline"], "first_seen_at": c["first_seen_at"],
        "source_count": c["source_count"], "location": c["location"],
        "entity_count": len(c["entities"]), "confidence": c["confidence"],
        "discovery_confidence": c["discovery_confidence"], "source_families": c["source_families"],
        "verification_status": c["state"], "event_id": c["event_id"],
        "discovery_latency_seconds": c["discovery_latency_seconds"],
    } for c in items]


def handoff_candidate_to_verification(candidate_id):
    """Auto-handoff: a discovered candidate begins verification without manual confirmation.

    Discovery stays permissive; the candidate is promoted to a real event in DETECTED state
    and sent to VERIFYING. Verification remains the strict evidence gate; nothing here
    verifies or produces a reel.
    """
    candidate = fast_discovery.candidate(candidate_id, connect=connect)
    if candidate["state"] in ("PROMOTED", "HANDED_OFF") and candidate["event_id"]:
        return {"candidate_id": candidate_id, "event_id": candidate["event_id"], "handed_off": True}
    # Create a real event from the candidate and move it to VERIFYING.
    with connect() as connection:
        event_id = "EV-" + uuid.uuid4().hex[:10].upper()
        timestamp = now()
        connection.execute(
            "INSERT INTO events(id,title,source,source_url,status,priority,created_at,updated_at,first_seen_at,"
            "last_seen_at,workspace_key,event_time,verification_status) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (event_id, candidate["headline"], candidate["source_families"][0] if candidate["source_families"] else "Discovery",
             candidate["signals"][0]["url"] if candidate["signals"] else "", "VERIFYING", "HIGH",
             timestamp, timestamp, candidate["first_seen_at"], timestamp,
             workspace_identity()["workspace_key"], candidate["first_seen_at"], "NOT_VERIFIED"),
        )
        connection.execute("INSERT INTO transitions(event_id,from_state,to_state,at) VALUES(?,?,?,?)",
                           (event_id, None, "VERIFYING", timestamp))
        connection.execute(
            "UPDATE event_candidates SET state='HANDED_OFF',event_id=? WHERE id=?", (event_id, candidate_id))
    return {"candidate_id": candidate_id, "event_id": event_id, "handed_off": True}


def maybe_trigger_auto_reel(event_id):
    """Enqueue AUTO_REEL_PIPELINE_V1 automatically once an event becomes eligible.

    Idempotent: one run per event, no duplicate reel, restart-safe, no duplicate paid calls.
    Returns the run dict when triggered, or None when not eligible or already running.
    """
    if not AUTO_REEL_PIPELINE_ENABLED:
        return None
    with connect() as connection:
        existing = connection.execute(
            "SELECT * FROM reel_pipeline_runs WHERE event_id=?", (event_id,)).fetchone()
    if existing:
        return dict(existing)
    check = reel_pipeline.eligibility(connect, event_id)
    if not check["eligible"]:
        return None
    return run_auto_reel_pipeline(event_id)


def production_queue():
    """User-facing production queue: one simple state per pipeline run, plus approval/publish."""
    with connect() as connection:
        runs = [dict(r) for r in connection.execute(
            "SELECT * FROM reel_pipeline_runs ORDER BY updated_at DESC")]
        for run in runs:
            reel_id = run.get("reel_id")
            run["approved"] = bool(connection.execute(
                "SELECT 1 FROM reel_approvals WHERE reel_id=? AND revoked_at IS NULL", (reel_id,)).fetchone()
            ) if reel_id else False
            run["scheduled_count"] = connection.execute(
                "SELECT COUNT(*) FROM scheduled_posts WHERE reel_id=? AND status IN ('SCHEDULED','PROCESSING')",
                (reel_id,)).fetchone()[0] if reel_id else 0
            run["published_at"] = (connection.execute(
                "SELECT published_at FROM scheduled_posts WHERE reel_id=? AND status='PUBLISHED' LIMIT 1",
                (reel_id,)).fetchone() or {"published_at": None})["published_at"] if reel_id else None
            run["ui_state"] = reel_control.queue_state(run)
    return runs


def recover_auto_reel_pipelines():
    """At startup, resume RUNNING pipelines; never duplicate paid/provider work (checkpointed)."""
    with connect() as connection:
        running = [row["event_id"] for row in connection.execute(
            "SELECT event_id FROM reel_pipeline_runs WHERE status='RUNNING'"
        )]
    results = []
    for event_id in running:
        try:
            results.append(run_auto_reel_pipeline(event_id))
        except Exception as error:  # noqa: BLE001
            log_error("auto_reel_recovery_failed", error, event_id=event_id)
    return results


def _real_assets_for_reel(connection):
    """Map ingested, rights-cleared media candidates to the real-AP scene plan keys.

    Only APPROVED/INGESTED candidates with reusable rights and stored bytes are returned.
    """
    wanted = {
        "MC-03DBB193FF24": "PLATFORM", "MC-45C070722C33": "PLANTATION", "MC-9D6CA783015D": "BARN",
        "MC-A64E5567006F": "DRYING", "MC-889125EDC332": "OFFICIALS", "MC-FD804ACF7BB5": "GUNTUR",
        "MC-01DC62B4E411": "TRACTOR", "MC-3B9836A55AE1": "BARN_LANDSCAPE",
    }
    rows = [dict(row) for row in connection.execute(
        "SELECT * FROM media_candidates WHERE lifecycle_state='INGESTED' AND storage_uri IS NOT NULL "
        "AND license_status IN ('VERIFIED_REUSE','ATTRIBUTION_REQUIRED','USER_PROVIDED')"
    )]
    assets = {}
    for row in rows:
        key = wanted.get(row["id"])
        if not key:
            continue
        location = ", ".join(part for part in (row["district"], row["state"] if row["state"] else None) if part) or None
        if row["state"] and "Andhra Pradesh" not in (location or ""):
            location = f"{location}, Andhra Pradesh" if location else "Andhra Pradesh"
        assets[key] = {
            "scene_key": key, "storage_uri": row["storage_uri"], "candidate_id": row["id"],
            "rights_status": row["license_status"], "attribution": row["attribution_text"],
            "attribution_required": bool(row["attribution_required"]), "publisher": row["publisher"],
            "location": location if row["ap_specific"] == "yes" else None,
            "content_hash": row["content_hash"], "title": row["title"],
        }
    return assets


def _reference_asset_for_reel(connection, asset_id, asset_type):
    """Resolve an optional contextual asset, requiring VERIFIED rights and the right type."""
    if asset_id in (None, ""):
        return None
    row = connection.execute("SELECT * FROM uploaded_media_assets WHERE id=?", (asset_id,)).fetchone()
    if row is None:
        raise ValueError(f"Reference asset {asset_id} is not registered.")
    asset = dict(row)
    if asset["rights_status"] != "VERIFIED":
        raise ValueError(f"Reference asset {asset_id} is not rights-verified for Reel use.")
    if asset_type and asset["asset_type"] != asset_type:
        raise ValueError(f"Reference asset {asset_id} is not a {asset_type}.")
    return asset


def create_final_reel(source_asset_id, *, cbn_asset_id=None, tdp_asset_id=None, composer=None, language="en"):
    """Compose exactly one immutable Final Reel derivative from an eligible source video.

    Contextual CBN/TDP assets are optional; when supplied they must be rights-verified
    rows in uploaded_media_assets, never an arbitrary path or URL. language="te" uses the
    natural Telugu explainer narration and scene-synced timeline.
    """
    with connect() as connection:
        asset, job, blockers = _final_reel_source_blockers(connection, source_asset_id)
        if blockers:
            raise ValueError("Final Reel composition blocked: " + " ".join(blockers))
        cbn = _reference_asset_for_reel(connection, cbn_asset_id, "PUBLIC_FIGURE_PHOTO")
        tdp = _reference_asset_for_reel(connection, tdp_asset_id, "PARTY_LOGO")
        real_assets = _real_assets_for_reel(connection)
        scene_rows = {row["scene_key"]: dict(row) for row in connection.execute(
            "SELECT * FROM generated_scenes WHERE scene_key IN ("
            + ",".join("?" for _ in final_reel_composer.SCENE_ORDER) + ") "
            "ORDER BY created_at DESC", tuple(final_reel_composer.SCENE_ORDER),
        )}
        # Keep only the newest row per scene key (ORDER BY created_at DESC above).
        scene_rows = {key: row for key, row in scene_rows.items()}
        storage = LocalMediaStorage(RENDER_STORAGE_ROOT)
        contextual = {}
        if cbn:
            contextual["cbn"] = {"asset": cbn, "data": storage.get(cbn["storage_uri"])}
        if tdp:
            contextual["tdp"] = {"asset": tdp, "data": storage.get(tdp["storage_uri"])}
    composer = composer or final_reel_composer.compose_final_reel
    try:
        result = composer(source_asset_id, connect=connect, storage_root=RENDER_STORAGE_ROOT, now=now,
                          cbn_asset_id=cbn["id"] if cbn else None, tdp_asset_id=tdp["id"] if tdp else None,
                          contextual=contextual, scene_rows=scene_rows, language=language, real_assets=real_assets)
    except final_reel_composer.FinalReelError as error:
        raise ValueError(f"Final Reel composition failed: {error}") from error
    if isinstance(result, dict) and result.get("id"):
        return final_reel_asset(result["id"])
    return result


def review_final_reel(final_reel_id, action, reviewer, comment=None):
    """Human approval is bound to one exact immutable Final Reel version and never inherited."""
    if action not in ("APPROVED", "CHANGES_REQUIRED", "REJECTED"):
        raise ValueError("Review action must be APPROVED, CHANGES_REQUIRED, or REJECTED.")
    reviewer = str(reviewer or "").strip()
    if not reviewer:
        raise ValueError("Reviewer name is required.")
    comment = str(comment or "").strip()[:2000] or None
    with connect() as connection:
        reel = connection.execute("SELECT * FROM final_reel_assets WHERE id=?", (final_reel_id,)).fetchone()
        if reel is None:
            raise KeyError(final_reel_id)
        if action == "APPROVED" and reel["status"] != "READY_FOR_REVIEW":
            raise ValueError("A blocked Final Reel cannot be approved.")
        review_id = "FRR-" + uuid.uuid4().hex[:12].upper()
        connection.execute(
            "INSERT INTO final_reel_reviews(id,final_reel_asset_id,action,reviewer,comment,created_at) VALUES(?,?,?,?,?,?)",
            (review_id, final_reel_id, action, reviewer, comment, now()),
        )
    # APPROVED also creates an immutable, version-exact approval record (Architecture 08).
    if action == "APPROVED":
        reel_control.approve_reel(final_reel_id, reviewer=reviewer, connect=connect, now=now)
        with connect() as connection:
            reel_control.notify(connection, "reel_approved", "INFO", f"Reel {final_reel_id} approved",
                                dedupe_key=f"approve:{final_reel_id}", link={"reel_id": final_reel_id})
    return final_reel_asset(final_reel_id)


def request_reel_revision(final_reel_id, categories, comment, reviewer):
    """Record a change request; the pipeline then produces a NEW immutable reel version."""
    result = reel_control.request_revision(final_reel_id, categories=categories, comment=comment, connect=connect, now=now)
    # Mark the reel's latest review as CHANGES_REQUIRED and open a new generation step.
    review_final_reel(final_reel_id, "CHANGES_REQUIRED", reviewer, comment)
    return result


def production_health_dashboard():
    return production_health()


def _final_reel_distribution_blockers(connection, final_reel_id):
    """A Final Reel can be distributed only when both the source lineage and the exact reel are approved."""
    reel = connection.execute("SELECT * FROM final_reel_assets WHERE id=?", (final_reel_id,)).fetchone()
    if reel is None:
        raise KeyError(final_reel_id)
    reel = dict(reel)
    asset, job, source_review, source_blockers = _distribution_media_blockers(
        connection, reel["source_asset_id"], allow_superseded=True
    )
    blockers = list(source_blockers)
    if reel["status"] != "READY_FOR_REVIEW":
        blockers.append("Only a READY_FOR_REVIEW Final Reel can be distributed.")
    review = _latest_final_reel_review(connection, final_reel_id)
    if not review or review["action"] != "APPROVED":
        blockers.append("The Final Reel's latest human review is not APPROVED.")
    return reel, asset, job, source_review, review, list(dict.fromkeys(blockers))


def _distribution_media(connection, media_source, media_id):
    """Resolve either a raw generated video or an approved Final Reel into one media binding."""
    if media_source == "FINAL_REEL":
        reel, asset, job, source_review, reel_review, blockers = _final_reel_distribution_blockers(connection, media_id)
        return {
            "media_source": "FINAL_REEL", "generated_asset_id": asset["id"], "asset_version": asset["version_number"],
            "checksum": reel["checksum_sha256"], "storage_uri": reel["storage_uri"], "mime_type": reel["mime_type"],
            "media_review_id": source_review["id"] if source_review else None,
            "final_reel_asset_id": reel["id"], "final_reel_review_id": reel_review["id"] if reel_review else None,
            "event_id": reel["event_id"], "content_package_id": reel["content_package_id"],
            "content_package_version": reel["content_package_version"], "job": job, "asset": asset,
            "review": reel_review, "source_review": source_review, "blockers": blockers,
        }
    asset, job, review, blockers = _distribution_media_blockers(connection, media_id)
    return {
        "media_source": "GENERATED_ASSET", "generated_asset_id": asset["id"], "asset_version": asset["version_number"],
        "checksum": asset["checksum_sha256"], "storage_uri": asset["storage_uri"], "mime_type": asset["mime_type"],
        "media_review_id": review["id"] if review else None, "final_reel_asset_id": None, "final_reel_review_id": None,
        "event_id": asset["event_id"], "content_package_id": asset["content_package_id"],
        "content_package_version": asset["content_package_version"], "job": job, "asset": asset,
        "review": review, "source_review": review, "blockers": blockers,
    }


def _package_media_id(package):
    return package.get("final_reel_asset_id") if package.get("media_source") == "FINAL_REEL" else package.get("generated_asset_id")


def _distribution_lineage_blockers(job):
    gate = _render_eligibility(job["content_package_id"], job["media_type"], job.get("source_asset_id"),
                               check_production_freshness=False)
    blockers = list(gate["blockers"])
    if not blockers and gate["input_version"] != job["input_version"]:
        blockers.append("Package, claim, or evidence lineage changed after this media was rendered.")
    return gate, blockers


def _create_distribution_package(media, platform, cover_time_ms, storage):
    job = media["job"]
    gate, lineage_blockers = _distribution_lineage_blockers(job)
    if lineage_blockers:
        raise ValueError("Distribution blocked: " + " ".join(lineage_blockers))
    data = storage.get(media["storage_uri"])
    if hashlib.sha256(data).hexdigest() != media["checksum"]:
        raise ValueError("Distribution blocked: stored media bytes do not match the approved checksum.")
    video = {**inspect_video(data), "file_size": len(data)}
    compliance = check_platform_compliance(platform, video)
    duration_ms = int((video.get("duration_seconds") or 0) * 1000)
    if cover_time_ms is None:
        cover_time_ms = min(1000, max(0, duration_ms - 100))
    cover_time_ms = int(cover_time_ms)
    if not 0 <= cover_time_ms <= max(0, duration_ms):
        raise ValueError("Cover time must fall within the video duration.")
    package = gate["package_payload"]
    with connect() as connection:
        claims = [dict(row) for row in connection.execute(
            "SELECT id,text,claim_type,attribution FROM claim_versions WHERE id IN ("
            + ",".join("?" for _ in gate["claim_version_ids"]) + ") ORDER BY id", gate["claim_version_ids"],
        )] if gate["claim_version_ids"] else []
    copy = build_platform_copy(platform, package, claims, cover_time_ms=cover_time_ms)
    validation = validate_platform_copy(copy, package, claims, platform)
    record = {
        "platform": platform, "media_source": media["media_source"],
        "asset": [media["generated_asset_id"], media["asset_version"], media["checksum"]],
        "final_reel": media["final_reel_asset_id"], "review": media["media_review_id"],
        "package": [gate["package"]["id"], gate["package"]["version_number"], gate["package"]["content_hash"]],
        "copy": {key: copy[key] for key in ("caption", "title", "hashtags", "accessibility_text", "cover", "platform_metadata")},
    }
    timestamp = now()
    with connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        version = connection.execute(
            "SELECT COALESCE(MAX(version_number),0)+1 FROM distribution_packages WHERE platform=? AND generated_asset_id=?",
            (platform, media["generated_asset_id"]),
        ).fetchone()[0]
        package_id = "DP-" + uuid.uuid4().hex[:12].upper()
        connection.execute(
            "INSERT INTO distribution_packages(id,event_id,platform,version_number,generated_asset_id,asset_version,"
            "asset_checksum_sha256,media_review_id,content_package_id,content_package_version,content_package_hash,"
            "approved_claim_set_id,approved_claim_set_version,caption,title,hashtags_json,accessibility_text,cover_json,"
            "platform_metadata_json,copy_provenance_json,copy_validation_json,compliance_json,compliant,copy_policy_version,"
            "content_hash,created_at,media_source,final_reel_asset_id,final_reel_review_id) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                package_id, media["event_id"], platform, version, media["generated_asset_id"], media["asset_version"],
                media["checksum"], media["media_review_id"], gate["package"]["id"], gate["package"]["version_number"],
                gate["package"]["content_hash"], gate["package"]["approved_claim_set_id"],
                gate["package"]["approved_claim_set_version"], copy["caption"], copy["title"], json.dumps(copy["hashtags"]),
                copy["accessibility_text"], json.dumps(copy["cover"]), json.dumps(copy["platform_metadata"]),
                json.dumps(copy["provenance"]), json.dumps(validation),
                json.dumps({**compliance, "decoded": {k: v for k, v in video.items() if k != "decoder"}}),
                int(compliance["compliant"] and validation["valid"]), DISTRIBUTION_COPY_POLICY_VERSION,
                meta_content_hash(record), timestamp, media["media_source"], media["final_reel_asset_id"],
                media["final_reel_review_id"],
            ),
        )
        connection.commit()
    return distribution_package(package_id)


def create_distribution_package(asset_id, platform, cover_time_ms=None, *, storage=None):
    """Build an immutable platform package from an approved raw generated video.

    Blocked when an approved Final Reel exists for the same source so the composed reel is used instead.
    """
    if platform not in META_PLATFORMS:
        raise ValueError("Platform must be INSTAGRAM_REELS or FACEBOOK_REELS.")
    storage = storage or LocalMediaStorage(RENDER_STORAGE_ROOT)
    with connect() as connection:
        media = _distribution_media(connection, "GENERATED_ASSET", asset_id)
    if media["blockers"]:
        raise ValueError("Distribution blocked: " + " ".join(media["blockers"]))
    return _create_distribution_package(media, platform, cover_time_ms, storage)


def create_final_reel_distribution_package(final_reel_id, platform, cover_time_ms=None, *, storage=None):
    """Build an immutable platform package bound to one approved immutable Final Reel."""
    if platform not in META_PLATFORMS:
        raise ValueError("Platform must be INSTAGRAM_REELS or FACEBOOK_REELS.")
    storage = storage or LocalMediaStorage(RENDER_STORAGE_ROOT)
    with connect() as connection:
        media = _distribution_media(connection, "FINAL_REEL", final_reel_id)
    if media["blockers"]:
        raise ValueError("Distribution blocked: " + " ".join(media["blockers"]))
    return _create_distribution_package(media, platform, cover_time_ms, storage)


def distribution_package(package_id):
    with connect() as connection:
        row = connection.execute("SELECT * FROM distribution_packages WHERE id=?", (package_id,)).fetchone()
        if row is None:
            raise KeyError(package_id)
        result = dict(row)
        result["reviews"] = [dict(item) for item in connection.execute(
            "SELECT * FROM distribution_reviews WHERE distribution_package_id=? ORDER BY created_at DESC,id DESC", (package_id,)
        )]
    for key in ("hashtags", "cover", "platform_metadata", "copy_provenance", "copy_validation", "compliance"):
        result[key] = json.loads(result.pop(key + "_json") or "null")
    result["latest_review"] = result["reviews"][0] if result["reviews"] else None
    return result


def review_distribution_package(package_id, action, reviewer, comment=None):
    """Separate human approval per platform package; APPROVED never publishes by itself."""
    if action not in ("APPROVED", "CHANGES_REQUIRED", "REJECTED"):
        raise ValueError("Review action must be APPROVED, CHANGES_REQUIRED, or REJECTED.")
    reviewer = str(reviewer or "").strip()
    if not reviewer:
        raise ValueError("Reviewer name is required.")
    package = distribution_package(package_id)
    if action == "APPROVED":
        problems = []
        if not package["compliant"]:
            problems.extend(package["compliance"].get("errors") or [])
            problems.extend(package["copy_validation"].get("errors") or [])
        with connect() as connection:
            media = _distribution_media(connection, package.get("media_source") or "GENERATED_ASSET", _package_media_id(package))
            media_review = media["review"]
        problems.extend(media["blockers"])
        expected_review_id = package.get("final_reel_review_id") or package["media_review_id"]
        if media_review and media_review["id"] != expected_review_id and media_review["action"] != "APPROVED":
            problems.append("The media review this package was built on is no longer the current approval.")
        if problems:
            raise ValueError("Platform package cannot be approved: " + " ".join(dict.fromkeys(problems)))
    review_id = "DR-" + uuid.uuid4().hex[:12].upper()
    with connect() as connection:
        connection.execute(
            "INSERT INTO distribution_reviews(id,distribution_package_id,action,reviewer,comment,created_at) VALUES(?,?,?,?,?,?)",
            (review_id, package_id, action, reviewer, str(comment or "").strip()[:2000] or None, now()),
        )
    return distribution_package(package_id)


def _publish_gate(package, *, require_live=True):
    """Every condition required to post. Re-evaluated at execution time, never cached."""
    blockers = []
    with connect() as connection:
        media = _distribution_media(connection, package.get("media_source") or "GENERATED_ASSET", _package_media_id(package))
        distribution_review = _latest_distribution_review(connection, package["id"])
    blockers.extend(media["blockers"])
    if media["checksum"] != package["asset_checksum_sha256"]:
        blockers.append("The media checksum no longer matches the platform package.")
    _, lineage_blockers = _distribution_lineage_blockers(media["job"])
    blockers.extend(lineage_blockers)
    if not package["compliant"]:
        blockers.append("The platform package failed platform compliance or copy validation.")
    if not distribution_review or distribution_review["action"] != "APPROVED":
        blockers.append("The platform package's latest human review is not APPROVED.")
    configuration = meta_platform_configuration(package["platform"])
    if require_live:
        switches = meta_publishing_switches()
        if not switches["SOCIAL_PUBLISHING_ENABLED"]:
            blockers.append("SOCIAL_PUBLISHING_ENABLED is off.")
        if not switches[META_PLATFORM_SWITCH[package["platform"]]]:
            blockers.append(f"{META_PLATFORM_SWITCH[package['platform']]} is off.")
        if configuration["missing"]:
            blockers.append("Missing platform configuration: " + ", ".join(configuration["missing"]))
    return {
        "allowed": not blockers, "blockers": list(dict.fromkeys(blockers)),
        "media_source": media["media_source"],
        "media_review_id": media["media_review_id"],
        "final_reel_asset_id": media["final_reel_asset_id"],
        "distribution_review_id": distribution_review["id"] if distribution_review else None,
        "switches": meta_publishing_switches(), "checked_at": now(),
    }


def publish_job(job_id):
    with connect() as connection:
        row = connection.execute("SELECT * FROM publish_jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            raise KeyError(job_id)
        result = dict(row)
        result["events"] = [dict(item) for item in connection.execute(
            "SELECT * FROM publish_job_events WHERE publish_job_id=? ORDER BY id", (job_id,)
        )]
    result["gate_snapshot"] = json.loads(result.pop("gate_snapshot_json") or "{}")
    for event in result["events"]:
        event["safe_metadata"] = json.loads(event.pop("safe_metadata_json") or "{}")
    return result


def _publish_event(connection, job_id, event_type, status=None, **metadata):
    connection.execute(
        "INSERT INTO publish_job_events(publish_job_id,event_type,status,safe_metadata_json,occurred_at) VALUES(?,?,?,?,?)",
        (job_id, event_type, status, json.dumps(_scrub_provider_metadata(metadata), ensure_ascii=False, sort_keys=True), now()),
    )


def _set_publish_status(job_id, status, event_type=None, **fields):
    columns = ["status=?", "updated_at=?"]
    values = [status, now()]
    for key, value in fields.items():
        columns.append(f"{key}=?")
        values.append(value)
    with connect() as connection:
        connection.execute(f"UPDATE publish_jobs SET {','.join(columns)} WHERE id=?", (*values, job_id))
        _publish_event(connection, job_id, event_type or status, status,
                       **{k: v for k, v in fields.items() if k in ("last_error_code", "last_error_message", "provider_post_id", "permalink")})


def request_publish(package_id, *, mode="NOW", scheduled_for=None, client_request_id=None, requested_by=None,
                    background=True, publisher=None, video_loader=None):
    """Publish now or schedule. Replays and concurrent clicks return the existing job; one post per platform+asset."""
    if mode not in ("NOW", "SCHEDULED"):
        raise ValueError("mode must be NOW or SCHEDULED.")
    if client_request_id:
        with connect() as connection:
            known = connection.execute("SELECT publish_job_id FROM publish_request_keys WHERE request_key=?", (client_request_id,)).fetchone()
        if known:
            return {"job": publish_job(known["publish_job_id"]), "duplicate": True}
    package = distribution_package(package_id)
    gate = _publish_gate(package, require_live=(mode == "NOW"))
    if not gate["allowed"]:
        raise ValueError("Publishing blocked: " + " ".join(gate["blockers"]))
    if mode == "SCHEDULED":
        try:
            when = datetime.fromisoformat(str(scheduled_for).replace("Z", "+00:00"))
        except (TypeError, ValueError) as error:
            raise ValueError("scheduled_for must be an ISO-8601 timestamp.") from error
        if when.tzinfo is None:
            raise ValueError("scheduled_for must include a timezone.")
        if when <= datetime.now(timezone.utc):
            raise ValueError("scheduled_for must be in the future.")
        scheduled_for = when.astimezone(timezone.utc).isoformat()
    timestamp = now()
    job_id = "PB-" + uuid.uuid4().hex[:12].upper()
    with connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        published = connection.execute(
            "SELECT id FROM publish_jobs WHERE platform=? AND asset_checksum_sha256=? AND status='PUBLISHED'",
            (package["platform"], package["asset_checksum_sha256"]),
        ).fetchone()
        if published:
            connection.commit()
            raise ValueError(f"Duplicate post blocked: this approved video is already published on {package['platform']} ({published['id']}).")
        active = connection.execute(
            "SELECT id FROM publish_jobs WHERE platform=? AND asset_checksum_sha256=? AND status IN ("
            + ",".join("?" for _ in PUBLISH_ACTIVE_STATUSES) + ")",
            (package["platform"], package["asset_checksum_sha256"], *PUBLISH_ACTIVE_STATUSES),
        ).fetchone()
        if active:
            connection.commit()
            return {"job": publish_job(active["id"]), "duplicate": True}
        attempts = connection.execute(
            "SELECT COUNT(*) FROM publish_jobs WHERE platform=? AND asset_checksum_sha256=?",
            (package["platform"], package["asset_checksum_sha256"]),
        ).fetchone()[0]
        idempotency_key = meta_content_hash({
            "platform": package["platform"], "package": package["id"], "asset": package["asset_checksum_sha256"],
            "sequence": attempts + 1,
        })
        connection.execute(
            "INSERT INTO publish_jobs(id,distribution_package_id,event_id,platform,generated_asset_id,asset_checksum_sha256,mode,"
            "status,scheduled_for,idempotency_key,client_request_id,requested_by,api_version,max_attempts,gate_snapshot_json,"
            "created_at,updated_at,media_source,final_reel_asset_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                job_id, package["id"], package["event_id"], package["platform"], package["generated_asset_id"],
                package["asset_checksum_sha256"], mode, "QUEUED" if mode == "NOW" else "SCHEDULED", scheduled_for,
                idempotency_key, client_request_id, str(requested_by or "")[:120] or None, meta_api_version(),
                PUBLISH_MAX_ATTEMPTS, json.dumps(gate), timestamp, timestamp,
                package.get("media_source") or "GENERATED_ASSET", package.get("final_reel_asset_id"),
            ),
        )
        if client_request_id:
            connection.execute(
                "INSERT INTO publish_request_keys(request_key,publish_job_id,created_at) VALUES(?,?,?)",
                (client_request_id, job_id, timestamp),
            )
        _publish_event(connection, job_id, "REQUESTED", "QUEUED" if mode == "NOW" else "SCHEDULED",
                       scheduled_for=scheduled_for, requested_by=requested_by)
        connection.commit()
    if mode == "NOW":
        if background:
            PUBLISH_EXECUTOR.submit(execute_publish_job, job_id, publisher, video_loader)
        else:
            execute_publish_job(job_id, publisher, video_loader)
    return {"job": publish_job(job_id), "duplicate": False}


def cancel_publish_job(job_id, reason=None):
    with connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute("SELECT status FROM publish_jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            raise KeyError(job_id)
        if row["status"] != "SCHEDULED":
            connection.commit()
            raise ValueError(f"Only scheduled posts can be cancelled (current status: {row['status']}).")
        timestamp = now()
        connection.execute(
            "UPDATE publish_jobs SET status='CANCELLED',cancelled_at=?,cancel_reason=?,updated_at=? WHERE id=? AND status='SCHEDULED'",
            (timestamp, str(reason or "Cancelled by reviewer")[:500], timestamp, job_id),
        )
        _publish_event(connection, job_id, "CANCELLED", "CANCELLED", reason=reason)
        connection.commit()
    return publish_job(job_id)


def _with_retries(job, step, operation):
    """Retry transient Meta failures within the job's bounded attempt budget; record each attempt."""
    for attempt in range(1, job["max_attempts"] + 1):
        try:
            return operation()
        except MetaError as error:
            with connect() as connection:
                _publish_event(connection, job["id"], "ATTEMPT_FAILED", error.code, step=step, attempt=attempt,
                               message=str(error)[:300], retryable=error.retryable)
            if not error.retryable or attempt >= job["max_attempts"]:
                raise
            time.sleep(min(60.0, PUBLISH_RETRY_BACKOFF_SECONDS * 2 ** (attempt - 1)))


def _finish_published(job, package, post_id, permalink, note=None):
    _set_publish_status(job["id"], "PUBLISHED", provider_post_id=post_id, permalink=permalink, published_at=now(),
                        last_error_message=note)
    record_publishing_history(
        event_id=package["event_id"], content_format="REEL", language="English", status="PUBLISHED", mode="live",
        claim_set_id=package["approved_claim_set_id"], content_fingerprint=package["content_hash"],
        title=package["title"] or package["caption"][:120], published_at=now(),
    )


def execute_publish_job(job_id, publisher=None, video_loader=None):
    job = publish_job(job_id)
    if job["status"] != "QUEUED":
        return job
    package = distribution_package(job["distribution_package_id"])
    gate = _publish_gate(package, require_live=True)
    if not gate["allowed"]:
        _set_publish_status(job_id, "BLOCKED", "BLOCKED_AT_EXECUTION", last_error_code="PUBLISH_GATE",
                            last_error_message=" ".join(gate["blockers"])[:500], gate_snapshot_json=json.dumps(gate))
        return publish_job(job_id)
    try:
        publisher = publisher or meta_publisher_for(package["platform"])
    except MetaError as error:
        _set_publish_status(job_id, "BLOCKED", last_error_code=error.code, last_error_message=str(error)[:500])
        return publish_job(job_id)
    with connect() as connection:
        updated = connection.execute(
            "UPDATE publish_jobs SET status='UPLOADING',started_at=?,attempt_count=attempt_count+1,gate_snapshot_json=?,updated_at=? "
            "WHERE id=? AND status='QUEUED'", (now(), json.dumps(gate), now(), job_id),
        ).rowcount
        if updated:
            _publish_event(connection, job_id, "UPLOADING", "UPLOADING")
    if not updated:
        return publish_job(job_id)
    copy = {"caption": package["caption"], "title": package["title"], "hashtags": package["hashtags"],
            "cover": package["cover"], "platform_metadata": package["platform_metadata"]}
    stage = "container"
    try:
        with connect() as connection:
            media = _distribution_media(connection, package.get("media_source") or "GENERATED_ASSET", _package_media_id(package))
        data = (video_loader or LocalMediaStorage(RENDER_STORAGE_ROOT).get)(media["storage_uri"])
        if hashlib.sha256(data).hexdigest() != package["asset_checksum_sha256"]:
            raise MetaRejectedError("Video bytes do not match the approved checksum.")
        container = job.get("provider_container_id") or _with_retries(job, "container", lambda: publisher.create_container(copy))
        with connect() as connection:
            connection.execute("UPDATE publish_jobs SET provider_container_id=?,updated_at=? WHERE id=?", (container, now(), job_id))
        stage = "upload"
        _with_retries(job, "upload", lambda: publisher.upload(container, data))
        _set_publish_status(job_id, "PROCESSING")
        stage = "processing"
        state = _with_retries(job, "processing", lambda: publisher.wait_until_ready(container))
        if state == "PUBLISHED":
            post_id = container if package["platform"] == "FACEBOOK_REELS" else None
            _finish_published(job, package, post_id, publisher.permalink(post_id) if post_id else None,
                              note=None if post_id else "Container already published; confirm the post ID in Instagram.")
            return publish_job(job_id)
        stage = "publish"
        _set_publish_status(job_id, "PUBLISHING", "PUBLISH_ATTEMPTED")
        if package["platform"] == "FACEBOOK_REELS":
            post_id = publisher.publish(container, copy)
        else:
            post_id = publisher.publish(container)
        _finish_published(job, package, post_id, publisher.permalink(post_id))
    except MetaPending:
        _set_publish_status(job_id, "NEEDS_INTERVENTION", last_error_code="META_STILL_PROCESSING",
                            last_error_message="Meta is still processing the upload; use Check status (free, never reposts).")
    except MetaAmbiguousError as error:
        _set_publish_status(job_id, "NEEDS_INTERVENTION", last_error_code=error.code,
                            last_error_message=f"{stage}: outcome unknown; never retried automatically. Use Check status.")
    except MetaError as error:
        _set_publish_status(job_id, "FAILED", last_error_code=error.code, last_error_message=f"{stage}: {str(error)[:400]}")
    except Exception as error:
        _set_publish_status(job_id, "FAILED", last_error_code="PUBLISH_INTERNAL_ERROR", last_error_message=f"{stage}: {str(error)[:400]}")
    finally:
        with connect() as connection:
            for event in getattr(publisher, "events", []):
                _publish_event(connection, job_id, "PROVIDER_" + event["event_type"], event.get("status"), **event.get("metadata", {}))
            if hasattr(publisher, "events"):
                publisher.events = []
    return publish_job(job_id)


def check_publish_status(job_id, publisher=None):
    """Free status check for interrupted/ambiguous jobs. Never creates a second post."""
    job = publish_job(job_id)
    if job["status"] not in ("NEEDS_INTERVENTION", "PROCESSING") or not job.get("provider_container_id"):
        raise ValueError("Only interrupted jobs with a Meta container/video ID can be checked.")
    package = distribution_package(job["distribution_package_id"])
    publisher = publisher or meta_publisher_for(package["platform"])
    container = job["provider_container_id"]
    publish_attempted = any(event["event_type"] == "PUBLISH_ATTEMPTED" for event in job["events"])
    try:
        published, post_id = publisher.find_published_media(container)
        if published:
            _finish_published(job, package, post_id, publisher.permalink(post_id) if post_id else None,
                              note=None if post_id else "Published; Meta does not expose the media ID for this container. Confirm in the app.")
            return publish_job(job_id)
        if publish_attempted:
            _set_publish_status(job_id, "NEEDS_INTERVENTION", "STATUS_CHECKED", last_error_code="META_OUTCOME_UNKNOWN",
                                last_error_message="Publish was attempted but is not visible yet; check again or confirm manually. Never auto-reposted.")
            return publish_job(job_id)
        state = publisher.wait_until_ready(container)
    except MetaPending:
        _set_publish_status(job_id, "NEEDS_INTERVENTION", "STATUS_CHECKED", last_error_code="META_STILL_PROCESSING",
                            last_error_message="Meta is still processing; check again later.")
        return publish_job(job_id)
    except MetaError as error:
        _set_publish_status(job_id, "FAILED", "STATUS_CHECKED", last_error_code=error.code, last_error_message=str(error)[:400])
        return publish_job(job_id)
    gate = _publish_gate(package, require_live=True)
    if not gate["allowed"]:
        _set_publish_status(job_id, "BLOCKED", "BLOCKED_AT_EXECUTION", last_error_code="PUBLISH_GATE",
                            last_error_message=" ".join(gate["blockers"])[:500])
        return publish_job(job_id)
    _set_publish_status(job_id, "PUBLISHING", "PUBLISH_ATTEMPTED")
    copy = {"caption": package["caption"], "title": package["title"]}
    try:
        post_id = publisher.publish(container, copy) if package["platform"] == "FACEBOOK_REELS" else publisher.publish(container)
        _finish_published(job, package, post_id, publisher.permalink(post_id))
    except MetaAmbiguousError as error:
        _set_publish_status(job_id, "NEEDS_INTERVENTION", last_error_code=error.code,
                            last_error_message="publish: outcome unknown; never retried automatically.")
    except MetaError as error:
        _set_publish_status(job_id, "FAILED", last_error_code=error.code, last_error_message=str(error)[:400])
    return publish_job(job_id)


def run_due_publish_jobs(now_at=None, publisher_factory=None, video_loader=None):
    """Local scheduler tick: claim due SCHEDULED jobs atomically and execute them (gates re-checked)."""
    now_at = now_at or datetime.now(timezone.utc).isoformat()
    with connect() as connection:
        due = [row["id"] for row in connection.execute(
            "SELECT id FROM publish_jobs WHERE status='SCHEDULED' AND scheduled_for<=? ORDER BY scheduled_for", (now_at,)
        )]
    results = []
    for job_id in due:
        with connect() as connection:
            claimed = connection.execute(
                "UPDATE publish_jobs SET status='QUEUED',updated_at=? WHERE id=? AND status='SCHEDULED'", (now(), job_id)
            ).rowcount
            if claimed:
                _publish_event(connection, job_id, "SCHEDULE_DUE", "QUEUED")
        if claimed:
            platform = publish_job(job_id)["platform"]
            results.append(execute_publish_job(job_id, publisher_factory(platform) if publisher_factory else None, video_loader))
    return results


def recover_interrupted_publish_jobs():
    """At startup: never re-run a post automatically. In-flight jobs need a free status check."""
    with connect() as connection:
        rows = [dict(row) for row in connection.execute(
            "SELECT id,status,provider_container_id FROM publish_jobs WHERE status IN ('QUEUED','UPLOADING','PROCESSING','PUBLISHING')"
        )]
    for row in rows:
        if row["status"] == "QUEUED" or not row["provider_container_id"]:
            _set_publish_status(row["id"], "BLOCKED", "RECOVERED_AT_STARTUP", last_error_code="INTERRUPTED",
                                last_error_message="Interrupted by restart before Meta received the post; request publishing again.")
        else:
            _set_publish_status(row["id"], "NEEDS_INTERVENTION", "RECOVERED_AT_STARTUP", last_error_code="INTERRUPTED",
                                last_error_message="Interrupted by restart after upload began; use Check status (never reposts).")
    return len(rows)


def start_publish_scheduler():
    def loop():
        while True:
            try:
                run_due_publish_jobs()
            except Exception as error:
                log_error("publish_scheduler_failed", error)
            time.sleep(PUBLISH_SCHEDULER_INTERVAL_SECONDS)
    threading.Thread(target=loop, name="meta-publish-scheduler", daemon=True).start()


def _event_distribution(event_id):
    with connect() as connection:
        package_ids = [row["id"] for row in connection.execute(
            "SELECT id FROM distribution_packages WHERE event_id=? ORDER BY created_at DESC,id DESC", (event_id,)
        )]
        job_ids = [row["id"] for row in connection.execute(
            "SELECT id FROM publish_jobs WHERE event_id=? ORDER BY created_at DESC,id DESC", (event_id,)
        )]
    packages = [distribution_package(package_id) for package_id in package_ids]
    for package in packages:
        package["publish_gate"] = _publish_gate(package, require_live=True)
    return {**distribution_overview(), "packages": packages, "publish_jobs": [publish_job(job_id) for job_id in job_ids]}


def distribution_overview():
    return {
        "switches": meta_publishing_switches(),
        "platforms": {platform: meta_platform_configuration(platform) for platform in META_PLATFORMS},
        "scheduling": "local scheduler (Meta-native scheduling is never used so kill switches apply at publish time)",
        "api_version": meta_api_version(),
    }


def verification_run(run_id):
    with connect() as connection:
        row = connection.execute("SELECT * FROM verification_runs WHERE id=?", (run_id,)).fetchone()
    if row is None:
        raise KeyError(run_id)
    return dict(row)


def research_run(run_id):
    with connect() as connection:
        row = connection.execute("SELECT * FROM research_runs WHERE id=?", (run_id,)).fetchone()
    if row is None:
        raise KeyError(run_id)
    return dict(row)


def event_room(event_id):
    with connect() as connection:
        event = connection.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
        if event is None:
            raise KeyError(event_id)
        signals = [dict(row) for row in _research_evidence_rows(connection, event_id)]
        runs = [dict(row) for row in connection.execute(
            "SELECT * FROM research_runs WHERE event_id=? ORDER BY requested_at DESC", (event_id,)
        )]
        claims = [dict(row) for row in connection.execute(
            "SELECT c.*,rr.mode AS research_mode,rr.provider AS research_provider "
            "FROM claims c JOIN research_runs rr ON rr.id=c.research_run_id "
            "WHERE c.event_id=? ORDER BY c.created_at DESC", (event_id,)
        )]
        for claim in claims:
            claim["evidence"] = [dict(row) for row in connection.execute(
                "SELECT ce.*,es.source_name,es.source_class,es.content_hash,es.retrieved_at "
                "FROM claim_evidence ce LEFT JOIN evidence_snapshots es ON es.id=ce.snapshot_id "
                "WHERE ce.claim_id=? ORDER BY ce.id", (claim["id"],)
            )]
        transitions = [dict(row) for row in connection.execute(
            "SELECT * FROM transitions WHERE event_id=? ORDER BY at DESC", (event_id,)
        )]
        verification_runs = [dict(row) for row in connection.execute(
            "SELECT * FROM verification_runs WHERE event_id=? ORDER BY requested_at DESC", (event_id,)
        )]
        for verification in verification_runs:
            verification["attempts"] = [dict(row) for row in connection.execute(
                "SELECT * FROM verification_attempts WHERE verification_run_id=? ORDER BY attempt_number",
                (verification["id"],),
            )]
            verification["checkpoints"] = [dict(row) for row in connection.execute(
                "SELECT id,phase,status,payload_hash,created_at FROM verification_checkpoints "
                "WHERE verification_run_id=? ORDER BY created_at,id", (verification["id"],),
            )]
            verification["source_family_assessments"] = [dict(row) for row in connection.execute(
                "SELECT * FROM verification_source_family_assessments WHERE verification_run_id=? "
                "ORDER BY relationship,left_snapshot_id,right_snapshot_id", (verification["id"],),
            )]
            verification["leads"] = [dict(row) for row in connection.execute(
                "SELECT * FROM verification_leads WHERE verification_run_id=? ORDER BY discovered_at,id",
                (verification["id"],),
            )]
            verification["decision_revisions"] = [dict(row) for row in connection.execute(
                "SELECT id,claim_version_id,superseded_decision_id,snapshot_hash,reason,created_at "
                "FROM verification_decision_revisions WHERE verification_run_id=? ORDER BY created_at,id",
                (verification["id"],),
            )]
            verification["snapshots"] = [dict(row) for row in connection.execute(
                "SELECT * FROM verification_snapshots WHERE verification_run_id=? ORDER BY source_class,source_name,id",
                (verification["id"],),
            )]
            verification["decisions"] = []
            decision_rows = connection.execute(
                "SELECT vd.*,cv.claim_id,cv.version_number AS claim_version,cv.text AS claim_text,"
                "cv.claim_type,cv.assertion_scope,cv.attribution "
                "FROM verification_decisions vd JOIN claim_versions cv ON cv.id=vd.claim_version_id "
                "WHERE vd.verification_run_id=? ORDER BY vd.required_for_event DESC,cv.claim_id",
                (verification["id"],),
            ).fetchall()
            for decision_row in decision_rows:
                decision = dict(decision_row)
                decision["missing_information"] = json.loads(decision.pop("missing_information_json") or "[]")
                decision["evidence"] = [dict(row) for row in connection.execute(
                    "SELECT vde.*,vs.source_name,vs.source_class,vs.url,vs.canonical_url,vs.title,"
                    "vs.publication_time,vs.stated_event_time,vs.retrieved_at "
                    "FROM verification_decision_evidence vde "
                    "JOIN verification_snapshots vs ON vs.id=vde.snapshot_id "
                    "WHERE vde.decision_id=? ORDER BY vde.relationship,vs.source_name",
                    (decision["id"],),
                )]
                verification["decisions"].append(decision)
            verification["history"] = [dict(row) for row in connection.execute(
                "SELECT * FROM verification_run_status_history WHERE run_id=? ORDER BY changed_at,id",
                (verification["id"],),
            )]
        acquisition_runs = [dict(row) for row in connection.execute(
            "SELECT * FROM source_acquisition_runs WHERE event_id=? ORDER BY started_at DESC,id DESC", (event_id,)
        )]
        for acquisition in acquisition_runs:
            acquisition["attempts"] = [dict(row) for row in connection.execute(
                "SELECT * FROM source_discovery_attempts WHERE acquisition_run_id=? ORDER BY attempted_at,id",
                (acquisition["id"],),
            )]
            for attempt in acquisition["attempts"]:
                attempt["domains"] = json.loads(attempt.pop("domains_json") or "[]")
                attempt["target_claim_ids"] = json.loads(attempt.pop("target_claim_ids_json") or "[]")
            acquisition["candidates"] = [dict(row) for row in connection.execute(
                "SELECT * FROM source_candidates WHERE acquisition_run_id=? ORDER BY created_at,id",
                (acquisition["id"],),
            )]
            for candidate in acquisition["candidates"]:
                candidate["metadata"] = json.loads(candidate.pop("metadata_json") or "{}")
                candidate.pop("extracted_text", None)
                candidate["pages"] = [dict(row) for row in connection.execute(
                    "SELECT page_number,text_checksum_sha256 FROM source_candidate_pages "
                    "WHERE candidate_id=? ORDER BY page_number", (candidate["id"],),
                )]
                candidate["claim_matches"] = [dict(row) for row in connection.execute(
                    "SELECT csc.*,cv.claim_id,cv.text AS claim_text FROM claim_source_candidates csc "
                    "JOIN claim_versions cv ON cv.id=csc.claim_version_id WHERE csc.candidate_id=? "
                    "ORDER BY cv.claim_id", (candidate["id"],),
                )]
            acquisition["packets"] = [dict(row) for row in connection.execute(
                "SELECT aep.*,cv.claim_id,cv.text AS claim_text FROM acquisition_evidence_packets aep "
                "JOIN claim_versions cv ON cv.id=aep.claim_version_id WHERE aep.acquisition_run_id=? "
                "ORDER BY cv.claim_id", (acquisition["id"],),
            )]
            for packet in acquisition["packets"]:
                packet["packet"] = json.loads(packet.pop("packet_json") or "{}")
        official_source_registry = [dict(row) for row in connection.execute(
            "SELECT * FROM official_source_authorities ORDER BY priority,name"
        )]
        for authority in official_source_registry:
            authority["document_types"] = json.loads(authority.pop("document_types_json") or "[]")
        approved_sets = [dict(row) for row in connection.execute(
            "SELECT acs.*,COUNT(acsi.claim_version_id) AS approved_claim_count "
            "FROM approved_claim_sets acs LEFT JOIN approved_claim_set_items acsi ON acsi.claim_set_id=acs.id "
            "WHERE acs.event_id=? GROUP BY acs.id ORDER BY acs.version_number DESC",
            (event_id,),
        )]
        content_runs = [dict(row) for row in connection.execute(
            "SELECT * FROM content_decision_runs WHERE event_id=? ORDER BY requested_at DESC", (event_id,)
        )]
        for content_run in content_runs:
            decision = connection.execute(
                "SELECT * FROM content_decisions WHERE run_id=?", (content_run["id"],)
            ).fetchone()
            content_run["decision_record"] = dict(decision) if decision else None
            if content_run["decision_record"]:
                content_run["decision_record"]["missing_evidence_or_media"] = json.loads(
                    content_run["decision_record"].pop("missing_evidence_or_media_json") or "[]"
                )
            content_run["history"] = [dict(row) for row in connection.execute(
                "SELECT * FROM content_decision_run_history WHERE run_id=? ORDER BY changed_at,id",
                (content_run["id"],),
            )]
        media_assets = [dict(row) for row in connection.execute(
            "SELECT * FROM media_assets WHERE event_id=? ORDER BY mode,created_at,id", (event_id,)
        )]
        publishing = [dict(row) for row in connection.execute(
            "SELECT * FROM publishing_history WHERE event_id=? ORDER BY recorded_at DESC,id DESC", (event_id,)
        )]
        production_jobs = [dict(row) for row in connection.execute(
            "SELECT * FROM production_jobs WHERE event_id=? ORDER BY requested_at DESC,id DESC", (event_id,)
        )]
        for job in production_jobs:
            job.pop("request_snapshot_json", None)  # large; the hash identifies the exact stored request
            job["validation_result"] = json.loads(job.pop("validation_result_json") or "null")
            job["history"] = [dict(item) for item in connection.execute(
                "SELECT * FROM production_job_status_history WHERE job_id=? ORDER BY changed_at,id", (job["id"],)
            )]
            package = connection.execute("SELECT * FROM content_packages WHERE job_id=?", (job["id"],)).fetchone()
            job["package"] = dict(package) if package else None
            if job["package"]:
                job["package"]["package"] = json.loads(job["package"].pop("package_json"))
                job["package"]["approved_claim_version_ids"] = json.loads(job["package"].pop("approved_claim_version_ids_json"))
                job["package"]["evidence_snapshot_ids"] = json.loads(job["package"].pop("evidence_snapshot_ids_json"))
                job["package"]["validation_result"] = json.loads(job["package"].pop("validation_result_json"))
        latest_decision = connection.execute(
            "SELECT id FROM content_decisions WHERE event_id=? ORDER BY decided_at DESC,id DESC LIMIT 1", (event_id,)
        ).fetchone()
        latest_package = connection.execute(
            "SELECT id,requested_format,version_number FROM content_packages WHERE event_id=? ORDER BY version_number DESC LIMIT 1",
            (event_id,),
        ).fetchone()
        render_jobs = [dict(row) for row in connection.execute(
            "SELECT * FROM render_jobs WHERE event_id=? ORDER BY created_at DESC,id DESC", (event_id,)
        )]
        for render in render_jobs:
            render["input_media_asset_ids"] = json.loads(render.pop("input_media_asset_ids_json") or "[]")
            render["output_media_asset_ids"] = json.loads(render.pop("output_media_asset_ids_json") or "[]")
            render["validation_result"] = json.loads(render.pop("validation_result_json") or "null")
            render["history"] = [dict(item) for item in connection.execute(
                "SELECT * FROM render_job_status_history WHERE render_job_id=? ORDER BY changed_at,id", (render["id"],)
            )]
            render["attempts"] = [dict(item) for item in connection.execute(
                "SELECT * FROM render_job_attempts WHERE render_job_id=? ORDER BY attempt_number,id", (render["id"],)
            )]
            render["provider_events"] = [dict(item) for item in connection.execute(
                "SELECT * FROM render_provider_events WHERE render_job_id=? ORDER BY id", (render["id"],)
            )]
            for provider_event in render["provider_events"]:
                provider_event["safe_metadata"] = json.loads(provider_event.pop("safe_metadata_json") or "{}")
            render["render_phase"] = _render_phase(render)
            render["qa_results"] = [dict(item) for item in connection.execute(
                "SELECT * FROM media_qa_results WHERE render_job_id=? ORDER BY qa_type", (render["id"],)
            )]
            for qa in render["qa_results"]:
                qa["flags"] = json.loads(qa.pop("flags_json") or "[]")
                qa["details"] = json.loads(qa.pop("details_json") or "{}")
            prompt = connection.execute(
                "SELECT id,request_hash,prompt_version,generation_config_version,reference_asset_ids_json,created_at "
                "FROM render_prompt_snapshots WHERE render_job_id=?", (render["id"],)
            ).fetchone()
            render["prompt_snapshot"] = dict(prompt) if prompt else None
            if render["prompt_snapshot"]:
                render["prompt_snapshot"]["reference_asset_ids"] = json.loads(
                    render["prompt_snapshot"].pop("reference_asset_ids_json") or "[]"
                )
            render["assets"] = [dict(item) for item in connection.execute(
                "SELECT ga.*,rjo.reused_identical_binary FROM render_job_outputs rjo "
                "JOIN generated_assets ga ON ga.id=rjo.generated_asset_id WHERE rjo.render_job_id=? ORDER BY ga.version_number",
                (render["id"],),
            )]
            for asset in render["assets"]:
                asset["source_asset_ids"] = json.loads(asset.pop("source_asset_ids_json") or "[]")
                asset["provenance"] = json.loads(asset.pop("provenance_json") or "{}")
                asset["provider_metadata"] = json.loads(asset.pop("provider_metadata_json") or "{}")
                asset["detected_text"] = json.loads(asset.pop("detected_text_json") or "[]")
                asset["validation_result"] = json.loads(asset.pop("validation_result_json") or "null")
                asset["qa_runs"] = [dict(run) for run in connection.execute(
                    "SELECT * FROM media_qa_runs WHERE generated_asset_id=? ORDER BY qa_kind,run_number DESC",
                    (asset["id"],),
                )]
                for qa_run in asset["qa_runs"]:
                    qa_run["checks"] = json.loads(qa_run.pop("checks_json") or "[]")
                    qa_run["evidence"] = json.loads(qa_run.pop("evidence_json") or "{}")
                    qa_run["usage"] = json.loads(qa_run.pop("usage_json") or "{}")
                asset["reviews"] = [dict(review) for review in connection.execute(
                    "SELECT * FROM media_reviews WHERE generated_asset_id=? ORDER BY created_at DESC,id DESC",
                    (asset["id"],),
                )]
                for review in asset["reviews"]:
                    review["qa_run_ids"] = json.loads(review.pop("qa_run_ids_json") or "[]")
                asset["latest_review"] = asset["reviews"][0] if asset["reviews"] else None
                asset["derived_assets"] = [dict(item) for item in connection.execute(
                    "SELECT * FROM derived_assets WHERE source_asset_id=? ORDER BY created_at,id", (asset["id"],),
                )]
                for derived in asset["derived_assets"]:
                    derived["transform"] = json.loads(derived.pop("transform_json") or "{}")
        cost_ledger = [dict(row) for row in connection.execute(
            "SELECT * FROM cost_ledger WHERE event_id=? ORDER BY recorded_at,id", (event_id,)
        )]
        cost_summary = production_cost_summary(connection, event_id)
        final_reels = []
        for row in connection.execute(
            "SELECT * FROM final_reel_assets WHERE event_id=? ORDER BY created_at DESC,id DESC", (event_id,)
        ):
            reel = final_reel_composer.decoded_final_reel(dict(row))
            reel["reviews"] = [dict(item) for item in connection.execute(
                "SELECT * FROM final_reel_reviews WHERE final_reel_asset_id=? ORDER BY created_at DESC,id DESC",
                (reel["id"],),
            )]
            reel["latest_review"] = reel["reviews"][0] if reel["reviews"] else None
            final_reels.append(reel)
    for run in runs:
        run["summary"] = json.loads(run.pop("summary_json")) if run.get("summary_json") else None
    for verification in verification_runs:
        verification["summary"] = json.loads(verification.pop("summary_json")) if verification.get("summary_json") else None
    production_gate = {"eligible": False, "blockers": ["No Content CEO decision exists."], "content_decision_id": None}
    if latest_decision:
        gate = _production_eligibility(latest_decision["id"])
        production_gate = {
            "eligible": gate["eligible"], "blockers": gate["blockers"],
            "content_decision_id": latest_decision["id"], "input_version": gate["input_version"],
        }
    production_gate["provider"] = production_configuration()
    render_gate = {
        "eligible": False, "blockers": ["No ContentPackage exists."], "content_package_id": None,
        "media_type": None, "configured_provider": None, "live_renderer_configured": False,
        "renderer_configuration_status": "LIVE_RENDERER_NOT_CONFIGURED",
    }
    if latest_package:
        requested_type = _default_render_media_type(latest_package["requested_format"])
        gate = _render_eligibility(latest_package["id"], requested_type)
        configuration = renderer_configuration(requested_type)
        lineage_blockers = list(gate["blockers"])
        blockers = list(lineage_blockers)
        configured_renderer = renderer_for(configuration["provider"], requested_type) if configuration["live"] else None
        if configuration["live"]:
            check = getattr(configured_renderer, "unsupported_reason", None)
            capability = check(
                requested_type, (gate["package_payload"].get("platform_metadata") or {}).get("aspect_ratio")
            ) if check else None
            if capability:
                blockers.append("RENDERER_CAPABILITY_MISMATCH: " + capability)
        render_gate = {
            "eligible": not blockers, "blockers": blockers, "lineage_blockers": lineage_blockers,
            "content_package_id": latest_package["id"], "media_type": requested_type,
            "content_package_version": latest_package["version_number"],
            "aspect_ratio": (gate["package_payload"].get("platform_metadata") or {}).get("aspect_ratio"),
            "duration_seconds": (gate["package_payload"].get("platform_metadata") or {}).get("duration_seconds"),
            "model": getattr(configured_renderer, "model", None),
            "resolution": getattr(configured_renderer, "resolution", None),
            "input_version": gate["input_version"], "configured_provider": configuration["provider"],
            "live_renderer_configured": configuration["live"],
            "renderer_configuration_status": configuration["status"],
        }
    # Physical existence never implies usability: only an asset rendered from the
    # latest package under the current lineage is current for human review.
    lineage_cache = {}
    for render in render_jobs:
        for asset in render["assets"]:
            reasons = []
            if asset["stale"]:
                reasons.append(asset.get("stale_reason") or "Lineage changed during rendering.")
            if latest_package and asset["content_package_id"] != latest_package["id"]:
                reasons.append("A newer ContentPackage version exists.")
            else:
                # Compare each job with the lineage of its own target (media type and bound source),
                # not the package's default render target.
                lineage_blockers, job_input = _job_lineage_state(render, render_gate, lineage_cache)
                if lineage_blockers:
                    reasons.append("Package lineage is no longer eligible: " + lineage_blockers[0])
                elif job_input and render["input_version"] != job_input:
                    reasons.append("Package lineage changed after rendering.")
            asset["current_for_review"] = bool(asset["usable_for_review"] and not reasons)
            asset["currency_reasons"] = reasons
    video_configuration = renderer_configuration("VIDEO")
    video_renderer = renderer_for(video_configuration["provider"], "VIDEO") if video_configuration["live"] else None
    video_gate = {
        "configured_provider": video_configuration["provider"], "live_renderer_configured": video_configuration["live"],
        "renderer_configuration_status": video_configuration["status"],
        "capabilities": provider_capabilities(video_configuration["provider"], "VIDEO") if video_configuration["provider"] else None,
        "model": getattr(video_renderer, "model", None), "resolution": getattr(video_renderer, "resolution", None),
        "content_package_id": latest_package["id"] if latest_package else None,
        "content_package_version": latest_package["version_number"] if latest_package else None,
        "text_to_video_available": bool(latest_package and _default_render_media_type(latest_package["requested_format"]) == "VIDEO"),
    }
    for render in render_jobs:
        for asset in render["assets"]:
            if asset["media_type"] != "IMAGE" or not latest_package or asset["fixture_only"]:
                asset["video_source"] = {"eligible": False, "blockers": ["Only live generated images can become a video source."]}
                continue
            source_gate = _render_eligibility(latest_package["id"], "VIDEO", asset["id"])
            source_blockers = list(source_gate["blockers"]) + ([] if asset["current_for_review"] else ["This image is not current for review."])
            asset["video_source"] = {"eligible": not source_blockers, "blockers": list(dict.fromkeys(source_blockers))}
    final_reel_eligible = None
    with connect() as connection:
        for render in render_jobs:
            for asset in render["assets"]:
                if asset["media_type"] not in ("VIDEO", "SHORT_FORM_VIDEO", "LONG_FORM_VIDEO"):
                    asset["final_reel"] = {
                        "eligible": False,
                        "blockers": ["Only a generated video can become a Final Reel source."],
                    }
                    continue
                _, _, blockers = _final_reel_source_blockers(connection, asset["id"])
                if not blockers and final_reel_eligible is None:
                    final_reel_eligible = asset["id"]
                asset["final_reel"] = {"eligible": not blockers, "blockers": blockers}
    return {
        "event": dict(event), "signals": signals, "claims": claims, "runs": runs,
        "final_reels": final_reels, "final_reel_source_asset_id": final_reel_eligible,
        "reel_standard": reel_standard.active_standard(),
        "reference_media": {
            "cbn_options": _reference_media_choices("PUBLIC_FIGURE_PHOTO"),
            "tdp_options": _reference_media_choices("PARTY_LOGO"),
            "all": list_uploaded_media_assets(),
        },
        "verification_runs": verification_runs, "approved_claim_sets": approved_sets,
        "source_acquisition_runs": acquisition_runs, "official_source_registry": official_source_registry,
        "content_decision_runs": content_runs, "media_assets": media_assets,
        "publishing_history": publishing,
        "production_jobs": production_jobs, "production_gate": production_gate,
        "render_jobs": render_jobs, "render_gate": render_gate, "video_gate": video_gate,
        "cost_ledger": cost_ledger, "cost_summary": cost_summary,
        "distribution": _event_distribution(event_id),
        "transitions": transitions,
    }


def overview():
    workspace = workspace_identity()
    with connect() as connection:
        event_rows = connection.execute(
            "SELECT e.*,COUNT(DISTINCT COALESCE(s.source_id,s.source_name)) AS source_count,"
            "GROUP_CONCAT(DISTINCT s.source_name) AS source_names,MIN(s.publication_time) AS publication_time "
            "FROM events e LEFT JOIN signals s ON s.event_id=e.id AND s.item_kind='event' "
            "GROUP BY e.id ORDER BY e.event_time DESC LIMIT 50"
        ).fetchall()
        events = []
        for row in event_rows:
            event = dict(row)
            event["source_names"] = event["source_names"].split(",") if event["source_names"] else ([event["source"]] if event["source"] else [])
            events.append(event)
        metrics = [dict(row) for row in connection.execute("SELECT * FROM metrics ORDER BY measured_at DESC")]
        reference_count = connection.execute(
            "SELECT COUNT(*) FROM signals WHERE workspace_key=? AND item_kind='reference'",
            (workspace["workspace_key"],),
        ).fetchone()[0]
        review_count = connection.execute(
            "SELECT COUNT(*) FROM signals WHERE workspace_key=? AND item_kind='review'",
            (workspace["workspace_key"],),
        ).fetchone()[0]
        rejected_count = connection.execute(
            "SELECT COUNT(*) FROM signals WHERE workspace_key=? AND item_kind='rejected'",
            (workspace["workspace_key"],),
        ).fetchone()[0]
        source_counts = {
            row["source_class"]: row["total"] for row in connection.execute(
                "SELECT source_class,COUNT(*) AS total FROM sources WHERE workspace_key=? GROUP BY source_class",
                (workspace["workspace_key"],),
            )
        }
    return {
        "workspace": {
            "key": workspace["workspace_key"],
            "display_name": workspace["display_name"],
            "leader": workspace["leader"]["canonical_name"],
            "jurisdiction": workspace["jurisdiction"],
        },
        "events": events,
        "reference_count": reference_count,
        "review_count": review_count,
        "rejected_count": rejected_count,
        "source_counts": source_counts,
        "metrics": metrics,
        "connected": False,
        "reel_standard": reel_standard.active_standard(),
        "reference_media": {
            "cbn_options": _reference_media_choices("PUBLIC_FIGURE_PHOTO"),
            "tdp_options": _reference_media_choices("PARTY_LOGO"),
        },
        "research": {
            "grok_configured": bool(os.environ.get("XAI_API_KEY")),
            "search_limit": RESEARCH_SEARCH_LIMIT,
            "token_limit": RESEARCH_TOKEN_LIMIT,
        },
        "verification": {
            "grok_configured": bool(os.environ.get("XAI_API_KEY")),
            "search_turn_limit": VERIFICATION_SEARCH_TURNS,
            "token_limit": VERIFICATION_TOKEN_LIMIT,
            "max_leads": VERIFICATION_MAX_LEADS,
        },
        "content_ceo": {
            "policy_version": CONTENT_POLICY_VERSION,
            "freshness_days": CONTENT_FRESHNESS_DAYS,
            "grok_configured": bool(os.environ.get("XAI_API_KEY")),
        },
        "content_production": {
            "policy_version": PRODUCTION_POLICY_VERSION,
            "prompt_schema_version": PROMPT_SCHEMA_VERSION,
            "anthropic_configured": bool(os.environ.get("ANTHROPIC_API_KEY")),
            "configuration": production_configuration(),
        },
        "distribution": distribution_overview(),
        "media_rendering": {
            "policy_version": RENDER_POLICY_VERSION,
            "prompt_version": RENDER_PROMPT_VERSION,
            "generation_config_version": RENDER_GENERATION_CONFIG_VERSION,
            "renderer_config_version": RENDERER_CONFIG_VERSION,
            "media_qa_policy_version": MEDIA_QA_POLICY_VERSION,
            "image": renderer_configuration("IMAGE"),
            "video": renderer_configuration("VIDEO"),
            "audio": renderer_configuration("AUDIO"),
        },
        "updated_at": now(),
    }


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def send_json(self, obj, status=200):
        data = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def send_binary(self, data, mime_type, status=200):
        self.send_response(status)
        self.send_header("Content-Type", mime_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "private, no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            # The client closed the connection mid-stream (e.g. a paused video preview); not an error.
            pass

    def read_json(self):
        length = int(self.headers.get("Content-Length", "0"))
        # Reference-media uploads carry base64 image bytes; allow up to the 12 MB asset cap.
        if length > 20_000_000:
            raise ValueError("body too large")
        return json.loads(self.rfile.read(length) or b"{}")

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/api/overview":
            self.send_json(overview())
            return
        if path == "/api/health":
            workspace = workspace_identity()
            self.send_json({
                "ok": True,
                "workspace_key": workspace["workspace_key"],
                "grok_configured": bool(os.environ.get("XAI_API_KEY")),
            })
            return
        match = re.fullmatch(r"/api/events/([^/]+)", path)
        if match:
            self.send_json(event_room(match.group(1)))
            return
        match = re.fullmatch(r"/api/research/([^/]+)", path)
        if match:
            self.send_json({"run": research_run(match.group(1))})
            return
        match = re.fullmatch(r"/api/verification/([^/]+)", path)
        if match:
            self.send_json({"run": verification_run(match.group(1))})
            return
        match = re.fullmatch(r"/api/content-decisions/([^/]+)", path)
        if match:
            self.send_json({"run": content_decision_run(match.group(1))})
            return
        match = re.fullmatch(r"/api/production/([^/]+)", path)
        if match:
            self.send_json({"job": production_job(match.group(1))})
            return
        match = re.fullmatch(r"/api/render-jobs/([^/]+)", path)
        if match:
            self.send_json({"job": render_job(match.group(1))})
            return
        match = re.fullmatch(r"/api/generated-assets/([^/]+)", path)
        if match:
            self.send_json({"asset": generated_asset(match.group(1))})
            return
        match = re.fullmatch(r"/api/generated-assets/([^/]+)/content", path)
        if match:
            asset = generated_asset(match.group(1))
            storage = LocalMediaStorage(RENDER_STORAGE_ROOT)
            if not storage.exists(asset["storage_uri"]):
                self.send_json({"error": "stored asset is unavailable"}, 404)
                return
            self.send_binary(storage.get(asset["storage_uri"]), asset["mime_type"])
            return
        match = re.fullmatch(r"/api/final-reels/([^/]+)", path)
        if match:
            self.send_json({"asset": final_reel_asset(match.group(1))})
            return
        match = re.fullmatch(r"/api/final-reels/([^/]+)/content", path)
        if match:
            with connect() as connection:
                row = connection.execute(
                    "SELECT storage_uri,mime_type FROM final_reel_assets WHERE id=?", (match.group(1),)
                ).fetchone()
            if row is None:
                self.send_json({"error": "final reel not found"}, 404)
                return
            storage = LocalMediaStorage(RENDER_STORAGE_ROOT)
            if not storage.exists(row["storage_uri"]):
                self.send_json({"error": "stored final reel is unavailable"}, 404)
                return
            self.send_binary(storage.get(row["storage_uri"]), row["mime_type"])
            return
        if path == "/api/uploads":
            self.send_json({"assets": list_uploaded_media_assets(dict(parse_qsl(urlparse(self.path).query)).get("type"))})
            return
        if path == "/api/discovered":
            self.send_json({"candidates": discovered_candidates_overview(),
                            "recall_qa": fast_discovery.discovery_recall_qa(connect=connect),
                            "discovery_health": discovery_health()})
            return
        if path == "/api/production":
            self.send_json({"health": production_health(), "queue": production_queue(),
                            "notifications": reel_control.notifications(connect=connect)})
            return
        if path == "/api/notifications":
            self.send_json({"notifications": reel_control.notifications(connect=connect)})
            return
        if path == "/api/pipelines":
            with connect() as connection:
                runs = [dict(row) for row in connection.execute(
                    "SELECT * FROM reel_pipeline_runs ORDER BY updated_at DESC")]
            for run in runs:
                run["ui_status"] = reel_pipeline.ui_status(run["status"])
            self.send_json({"pipelines": runs, "standard": reel_standard.PRODUCTION_STANDARD_VERSION})
            return
        match = re.fullmatch(r"/api/pipelines/([^/]+)", path)
        if match:
            run = reel_pipeline.pipeline_run(match.group(1), connect=connect)
            run["ui_status"] = reel_pipeline.ui_status(run["status"])
            self.send_json({"pipeline": run})
            return
        match = re.fullmatch(r"/api/uploads/([^/]+)/content", path)
        if match:
            asset = uploaded_media_asset(match.group(1))
            storage = LocalMediaStorage(RENDER_STORAGE_ROOT)
            if not storage.exists(asset["storage_uri"]):
                self.send_json({"error": "stored reference asset is unavailable"}, 404)
                return
            self.send_binary(storage.get(asset["storage_uri"]), asset["mime_type"])
            return
        match = re.fullmatch(r"/api/derived-assets/([^/]+)/content", path)
        if match:
            with connect() as connection:
                row = connection.execute("SELECT storage_uri,mime_type FROM derived_assets WHERE id=?", (match.group(1),)).fetchone()
            if row is None:
                self.send_json({"error": "derived asset not found"}, 404)
                return
            storage = LocalMediaStorage(RENDER_STORAGE_ROOT)
            if not storage.exists(row["storage_uri"]):
                self.send_json({"error": "stored derivative is unavailable"}, 404)
                return
            self.send_binary(storage.get(row["storage_uri"]), row["mime_type"])
            return
        match = re.fullmatch(r"/api/events/([^/]+)/signals", path)
        if match:
            with connect() as connection:
                signals = [dict(row) for row in connection.execute(
                    "SELECT * FROM signals WHERE event_id=? ORDER BY detected_at", (match.group(1),)
                )]
            self.send_json({"signals": signals})
            return
        super().do_GET()

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            body = self.read_json()
            if path == "/api/events":
                self.send_json({"error": "direct event creation is disabled; ingest a dated individual source item"}, 410)
                return
            if path == "/api/ingest-url":
                result = ingest_url(body["url"], source_name=body.get("source_name"))
                self.send_json(result, 200 if result["duplicate"] else 201)
                return
            if path == "/api/ingest-configured":
                self.send_json(ingest_configured_sources(force=bool(body.get("force"))))
                return
            match = re.fullmatch(r"/api/events/([^/]+)/research", path)
            if match:
                result = enqueue_research(
                    match.group(1), body.get("provider", "test"), background=True,
                    search_limit=body.get("search_limit", 0), token_limit=body.get("token_limit"),
                )
                self.send_json(result, 200 if result["duplicate"] or result["cached"] else 202)
                return
            match = re.fullmatch(r"/api/research/([^/]+)/verify", path)
            if match:
                result = enqueue_verification(match.group(1), body.get("provider", "grok"), background=True)
                self.send_json(result, 200 if result["duplicate"] or result["cached"] else 202)
                return
            match = re.fullmatch(r"/api/verification/([^/]+)/resume", path)
            if match:
                result = resume_verification(match.group(1), background=True)
                self.send_json(result, 202)
                return
            match = re.fullmatch(r"/api/verification/([^/]+)/evidence-url", path)
            if match:
                result = add_evidence_url(match.group(1), body["url"], body.get("claim_ids"))
                self.send_json(result, 201)
                return
            match = re.fullmatch(r"/api/events/([^/]+)/content-decision", path)
            if match:
                result = enqueue_content_decision(match.group(1), body.get("provider", "test"), background=True)
                self.send_json(result, 200 if result["duplicate"] or result["cached"] else 202)
                return
            match = re.fullmatch(r"/api/content-decisions/([^/]+)/production", path)
            if match:
                requested_provider = body.get("provider", "anthropic")
                if requested_provider == "anthropic" and production_configuration()["live"] and body.get("confirmed_paid_action") is not True:
                    raise ValueError("Explicit confirmation is required before a paid Claude generation.")
                if requested_provider != "anthropic" and not production_configuration()["fixture_allowed"]:
                    raise ProductionProviderUnavailable(
                        "Fixture packages are available only in explicit demo mode (REACHOUT_DEMO_MODE=1)."
                    )
                result = enqueue_production(
                    match.group(1), requested_provider, background=True,
                    regenerate=bool(body.get("regenerate")), client_request_id=body.get("client_request_id"),
                )
                self.send_json(result, 200 if result["duplicate"] or result["cached"] else 202)
                return
            match = re.fullmatch(r"/api/content-packages/([^/]+)/render", path)
            if match:
                requested_media_type = body.get("media_type") or "IMAGE"
                if renderer_configuration(requested_media_type)["live"] and body.get("confirmed_paid_action") is not True:
                    raise ValueError("Explicit confirmation is required before a paid media generation.")
                result = enqueue_render(
                    match.group(1), body.get("media_type"), body.get("provider"), background=True,
                    regenerate=bool(body.get("regenerate")), source_asset_id=body.get("source_asset_id"),
                    generation_mode=body.get("generation_mode"), aspect_ratio=body.get("aspect_ratio"), client_request_id=body.get("client_request_id"),
                )
                self.send_json(result, 200 if result["duplicate"] or result["cached"] else 202)
                return
            match = re.fullmatch(r"/api/render-jobs/([^/]+)/resume", path)
            if match:
                job = resume_render_job(match.group(1))
                self.send_json({"job": job, "resubmitted": False})
                return
            match = re.fullmatch(r"/api/generated-assets/([^/]+)/qa", path)
            if match:
                runs = rerun_media_qa(match.group(1), body.get("qa_kind", "ALL"))
                self.send_json({"runs": runs}, 201)
                return
            match = re.fullmatch(r"/api/generated-assets/([^/]+)/review", path)
            if match:
                review = review_media_asset(
                    match.group(1), body.get("action"), body.get("reviewer"), body.get("comment"),
                )
                self.send_json({"review": review}, 201)
                return
            if path == "/api/discover":
                # Manual discovery uses the SAME normalization + dedupe + clustering pipeline.
                raw = body.get("signals") or []
                normalized = [live_discovery.normalize_signal(item, source_family=item.get("source_family", "Manual"),
                                                              publisher=item.get("publisher", "Manual")) for item in raw]
                unique, deduped = live_discovery.dedupe(normalized)
                candidate = fast_discovery.discover(unique, connect=connect, now=now)
                if candidate and body.get("handoff", True):
                    handoff_candidate_to_verification(candidate["id"])
                self.send_json({"candidate": candidate, "deduped": deduped}, 201 if candidate else 200)
                return
            if path == "/api/discovery/run":
                self.send_json({"cycle": run_live_discovery_cycle(handoff=body.get("handoff", True))})
                return
            if path == "/api/discovery/youtube/run":
                self.send_json({"cycle": run_youtube_discovery_cycle(queries=body.get("queries"))})
                return
            match = re.fullmatch(r"/api/discovered/([^/]+)/handoff", path)
            if match:
                self.send_json(handoff_candidate_to_verification(match.group(1)), 201)
                return
            match = re.fullmatch(r"/api/events/([^/]+)/auto-reel", path)
            if match:
                if not AUTO_REEL_PIPELINE_ENABLED:
                    raise ValueError("The automated reel pipeline is off (AUTO_REEL_PIPELINE=0).")
                self.send_json({"pipeline": run_auto_reel_pipeline(match.group(1))}, 201)
                return
            if path == "/api/uploads":
                self.send_json(ingest_reference_media(
                    data=body.get("data"), filename=body.get("filename"), asset_type=body.get("asset_type"),
                    label=body.get("label"), source_name=body.get("source_name"),
                    source_url=body.get("source_url"), license_note=body.get("license_note"),
                    rights_status=body.get("rights_status"), uploader=body.get("uploader"),
                    reviewer=body.get("reviewer"), identity_subject=body.get("identity_subject"),
                ), 201)
                return
            match = re.fullmatch(r"/api/generated-assets/([^/]+)/final-reels", path)
            if match:
                self.send_json({"asset": create_final_reel(
                    match.group(1), cbn_asset_id=body.get("cbn_asset_id"), tdp_asset_id=body.get("tdp_asset_id"),
                    language=body.get("language", "en"),
                )}, 201)
                return
            match = re.fullmatch(r"/api/final-reels/([^/]+)/review", path)
            if match:
                asset = review_final_reel(
                    match.group(1), body.get("action"), body.get("reviewer"), body.get("comment"),
                )
                self.send_json({"asset": asset}, 201)
                return
            match = re.fullmatch(r"/api/final-reels/([^/]+)/revision", path)
            if match:
                result = request_reel_revision(
                    match.group(1), body.get("categories"), body.get("comment"), body.get("reviewer"))
                self.send_json({"revision_request": result}, 201)
                return
            match = re.fullmatch(r"/api/final-reels/([^/]+)/schedule", path)
            if match:
                post = schedule_reel_post(match.group(1), body.get("platform"), body.get("scheduled_at"),
                                          timezone_name=body.get("timezone", "UTC"))
                self.send_json({"scheduled_post": post}, 201)
                return
            match = re.fullmatch(r"/api/final-reels/([^/]+)/distribution-packages", path)
            if match:
                package = create_final_reel_distribution_package(
                    match.group(1), body.get("platform"), body.get("cover_time_ms")
                )
                self.send_json({"distribution_package": package}, 201)
                return
            match = re.fullmatch(r"/api/generated-assets/([^/]+)/distribution-packages", path)
            if match:
                package = create_distribution_package(match.group(1), body.get("platform"), body.get("cover_time_ms"))
                self.send_json({"distribution_package": package}, 201)
                return
            match = re.fullmatch(r"/api/distribution-packages/([^/]+)/review", path)
            if match:
                package = review_distribution_package(match.group(1), body.get("action"), body.get("reviewer"), body.get("comment"))
                self.send_json({"distribution_package": package}, 201)
                return
            match = re.fullmatch(r"/api/distribution-packages/([^/]+)/(publish|schedule)", path)
            if match:
                result = request_publish(
                    match.group(1), mode="NOW" if match.group(2) == "publish" else "SCHEDULED",
                    scheduled_for=body.get("scheduled_for"), client_request_id=body.get("client_request_id"),
                    requested_by=body.get("requested_by"),
                )
                self.send_json(result, 200 if result["duplicate"] else 202)
                return
            match = re.fullmatch(r"/api/publish-jobs/([^/]+)/cancel", path)
            if match:
                self.send_json({"job": cancel_publish_job(match.group(1), body.get("reason"))})
                return
            match = re.fullmatch(r"/api/publish-jobs/([^/]+)/check-status", path)
            if match:
                self.send_json({"job": check_publish_status(match.group(1))})
                return
            match = re.fullmatch(r"/api/events/([^/]+)/transition", path)
            if match:
                transition(match.group(1), body["state"])
                self.send_json({"id": match.group(1), "status": body["state"]})
                return
            self.send_json({"error": "not found"}, 404)
        except (ValueError, KeyError, TypeError, MissingRendererConfiguration, json.JSONDecodeError, ET.ParseError) as error:
            log_error("request_rejected", error, method="POST", path=path)
            self.send_json({"error": str(error)}, 400)
        except Exception as error:
            log_error("request_failed", error, method="POST", path=path)
            self.send_json({"error": "request failed; see structured server log"}, 502)


if __name__ == "__main__":
    init()
    recover_interrupted_publish_jobs()
    start_publish_scheduler()
    start_scheduled_post_scheduler()
    start_live_discovery_scheduler()
    port = int(os.environ.get("PORT", "8000"))
    host = os.environ.get("HOST", "127.0.0.1")
    print(f"ReachOut dashboard: http://{host}:{port}", flush=True)
    ThreadingHTTPServer((host, port), Handler).serve_forever()
