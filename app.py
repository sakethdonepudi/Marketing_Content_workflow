import hashlib
import html
import ipaddress
import json
import logging
import os
import re
import socket
import sqlite3
import struct
import sys
import time
import uuid
import xml.etree.ElementTree as ET
import zlib
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
    PROMPT_SCHEMA_VERSION,
    ProductionProviderResult,
    production_provider_for,
    validate_production_package,
)
from media_rendering import (
    RENDER_GENERATION_CONFIG_VERSION,
    RENDER_PROMPT_VERSION,
    InvalidRendererResponse,
    MissingRendererConfiguration,
    configured_renderer_name,
    renderer_configuration,
    renderer_for,
)
from media_storage import LocalMediaStorage
from media_qa import MEDIA_QA_POLICY_VERSION, MediaQAResult, NoopVisualQAProvider, evaluate_text_overlay

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
USER_AGENT = "ReachOut-OS/0.2 (+local factual source monitor)"
MAX_RESPONSE_BYTES = 2_000_000
CLUSTER_WINDOW_HOURS = 72
EVENT_MAX_AGE_DAYS = int(os.environ.get("REACHOUT_EVENT_MAX_AGE_DAYS", "30"))
RESEARCH_SEARCH_LIMIT = max(0, int(os.environ.get("RESEARCH_SEARCH_LIMIT", "1")))
RESEARCH_TOKEN_LIMIT = max(256, int(os.environ.get("RESEARCH_TOKEN_LIMIT", "4000")))
RESEARCH_TIMEOUT_SECONDS = max(5, int(os.environ.get("RESEARCH_TIMEOUT_SECONDS", "60")))
RESEARCH_MAX_RETRIES = max(0, int(os.environ.get("RESEARCH_MAX_RETRIES", "1")))
RESEARCH_EXECUTOR = ThreadPoolExecutor(max_workers=max(1, int(os.environ.get("RESEARCH_WORKERS", "2"))))
VERIFICATION_SEARCH_TURNS = max(1, min(3, int(os.environ.get("VERIFICATION_SEARCH_TURNS", "1"))))
VERIFICATION_TOKEN_LIMIT = max(256, int(os.environ.get("VERIFICATION_TOKEN_LIMIT", "1200")))
VERIFICATION_TIMEOUT_SECONDS = max(10, int(os.environ.get("VERIFICATION_TIMEOUT_SECONDS", "75")))
VERIFICATION_MAX_RETRIES = max(0, int(os.environ.get("VERIFICATION_MAX_RETRIES", "0")))
VERIFICATION_MAX_LEADS = max(1, min(10, int(os.environ.get("VERIFICATION_MAX_LEADS", "5"))))
VERIFICATION_EXECUTOR = ThreadPoolExecutor(max_workers=1)
CONTENT_POLICY_VERSION = os.environ.get("CONTENT_POLICY_VERSION", "content-ceo-v1.1")
CONTENT_FRESHNESS_DAYS = max(1, int(os.environ.get("CONTENT_FRESHNESS_DAYS", "7")))
CONTENT_HISTORY_DAYS = max(1, int(os.environ.get("CONTENT_HISTORY_DAYS", "30")))
CONTENT_TOKEN_LIMIT = max(256, int(os.environ.get("CONTENT_TOKEN_LIMIT", "900")))
CONTENT_TIMEOUT_SECONDS = max(5, int(os.environ.get("CONTENT_TIMEOUT_SECONDS", "45")))
CONTENT_EXECUTOR = ThreadPoolExecutor(max_workers=1)
PRODUCTION_POLICY_VERSION = os.environ.get("PRODUCTION_POLICY_VERSION", "content-production-v1")
PRODUCTION_TOKEN_LIMIT = max(512, int(os.environ.get("PRODUCTION_TOKEN_LIMIT", "3000")))
PRODUCTION_CONNECTION_TIMEOUT_SECONDS = max(2, int(os.environ.get("PRODUCTION_CONNECTION_TIMEOUT_SECONDS", "10")))
PRODUCTION_RESPONSE_TIMEOUT_SECONDS = max(5, int(os.environ.get("PRODUCTION_RESPONSE_TIMEOUT_SECONDS", "60")))
PRODUCTION_MAX_RETRIES = max(0, min(2, int(os.environ.get("PRODUCTION_MAX_RETRIES", "0"))))
PRODUCTION_EXECUTOR = ThreadPoolExecutor(max_workers=1)
PRODUCTION_ACTIVE_STATES = ("QUEUED", "GENERATING", "VALIDATING")
PRODUCTION_TRANSITIONS = {
    "QUEUED": {"GENERATING", "BLOCKED"},
    "GENERATING": {"VALIDATING", "HUMAN_REVIEW", "FAILED", "BLOCKED"},
    "VALIDATING": {"READY_FOR_APPROVAL", "HUMAN_REVIEW", "BLOCKED", "FAILED"},
    "READY_FOR_APPROVAL": set(), "HUMAN_REVIEW": set(), "BLOCKED": set(), "FAILED": set(),
}
RENDER_POLICY_VERSION = os.environ.get("RENDER_POLICY_VERSION", "media-render-policy-v1")
RENDERER_CONFIG_VERSION = os.environ.get("RENDERER_CONFIG_VERSION", "live-renderer-config-v1")
RENDER_STORAGE_ROOT = Path(os.environ.get("RENDER_STORAGE_ROOT", ROOT / ".context" / "generated_media"))
RENDER_TIMEOUT_SECONDS = max(5, int(os.environ.get("RENDER_TIMEOUT_SECONDS", "90")))
LIVE_RENDERER_TIMEOUT_SECONDS = max(5, int(os.environ.get("LIVE_RENDERER_TIMEOUT_SECONDS", "90")))
RENDER_MAX_RETRIES = max(0, min(3, int(os.environ.get("RENDER_MAX_RETRIES", "1"))))
RENDER_MIN_IMAGE_WIDTH = max(64, int(os.environ.get("RENDER_MIN_IMAGE_WIDTH", "1080")))
RENDER_MIN_IMAGE_HEIGHT = max(64, int(os.environ.get("RENDER_MIN_IMAGE_HEIGHT", "1080")))
RENDER_ASPECT_TOLERANCE = max(0.01, float(os.environ.get("RENDER_ASPECT_TOLERANCE", "0.03")))
RENDER_EXECUTOR = ThreadPoolExecutor(max_workers=max(1, int(os.environ.get("RENDER_WORKERS", "1"))))
RENDER_TRANSITIONS = {
    "QUEUED": {"PREPARING", "BLOCKED", "CANCELLED"},
    "PREPARING": {"RENDERING", "BLOCKED", "HUMAN_REVIEW", "FAILED", "CANCELLED"},
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


def load_source_config(path=None):
    config_path = Path(path or SOURCES_CONFIG)
    data = json.loads(config_path.read_text(encoding="utf-8"))
    if not isinstance(data.get("sources"), list):
        raise ValueError("source configuration must contain a sources array")
    return data


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
    material = sorted(set(research + later + signals))
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
            "limit_guaranteed,limit_notes,summary_json) VALUES(?,?,?,?,?,?,'QUEUED',?,?,?,0,'Queued',?,?,?,?,?,?)",
            (
                run_id, research_run["event_id"], research_run_id, provider.name, provider.model, provider.mode,
                evidence_version, claims_version, timestamp, VERIFICATION_MAX_RETRIES + 1, search_turns,
                VERIFICATION_TOKEN_LIMIT, int(provider.mode == "live"), limit_notes,
                json.dumps({"test_leads": test_leads or []}) if provider.mode == "test" else None,
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
    return {
        "workspace": workspace_identity()["display_name"], "event_id": run["event_id"],
        "claims": claims, "gaps": list(dict.fromkeys(gaps)),
        "test_leads": test_payload.get("test_leads") or [],
    }


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

    for left in range(len(rows)):
        for right in range(left + 1, len(rows)):
            same_text = rows[left]["text_family_hash"] == rows[right]["text_family_hash"]
            similarity_score = SequenceMatcher(
                None, _normalized_text(rows[left]["text"]).lower()[:20000],
                _normalized_text(rows[right]["text"]).lower()[:20000],
            ).ratio()
            if same_text or similarity_score >= 0.86:
                union(left, right)
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


def _best_claim_excerpt(claim_text, source_text):
    segments = [segment.strip() for segment in re.split(r"(?<=[.!?])\s+|\n+", source_text) if len(segment.strip()) > 20]
    if not segments:
        segments = [_normalized_text(source_text)[:1200]]
    claim_tokens = _tokens(claim_text)
    best = None
    for segment in segments:
        segment_tokens = _tokens(segment)
        score = len(claim_tokens & segment_tokens) / len(claim_tokens) if claim_tokens else 0
        if best is None or score > best[0]:
            best = (score, segment[:1600])
    return best or (0.0, "")


def _claim_numbers(value):
    return set(re.findall(r"\b\d[\d,.]*(?:\s*(?:crore|lakh|million|billion|%|kg|km|mw|days?))?\b", value, re.I))


def _quote_is_direct(claim_text, excerpt):
    quoted = re.findall(r"[\"“](.*?)[\"”]", claim_text)
    if quoted:
        return all(_normalized_text(item) in _normalized_text(excerpt) for item in quoted)
    return _normalized_text(claim_text) in _normalized_text(excerpt)


def _evaluate_verification_claim(connection, run, version, snapshots):
    known_refs = {
        canonicalize_url(row["source_url"]): dict(row)
        for row in connection.execute(
            "SELECT * FROM claim_evidence WHERE claim_id=? AND validation_status='VALID'", (version["claim_id"],)
        )
    }
    evaluated = []
    relative_date = False
    claim_numbers = _claim_numbers(version["text"])
    positive = any(word in version["text"].lower() for word in ("approved", "permitted", "authorised", "authorized"))
    for snapshot in snapshots:
        known = known_refs.get(snapshot["canonical_url"])
        if known:
            excerpt = known["supporting_excerpt"] or ""
            relationship = known["support_kind"]
            score = 1.0
        else:
            score, excerpt = _best_claim_excerpt(version["text"], snapshot["text"])
            relationship = "supports" if score >= 0.48 else "mentions_only"
        excerpt_numbers = _claim_numbers(excerpt)
        if claim_numbers and not all(number.lower() in excerpt.lower() for number in claim_numbers):
            if score >= 0.4 and excerpt_numbers and excerpt_numbers != claim_numbers:
                relationship = "conflicts"
            else:
                relationship = "mentions_only"
        if positive and score >= 0.4 and re.search(r"\b(denied|rejected|not permitted|prohibited|withdrew)\b", excerpt, re.I):
            relationship = "conflicts"
        if relationship == "supports" and not _explicit_values_supported(version["text"], [excerpt]):
            relationship = "mentions_only"
        if version["claim_type"] == "quotation" and not _quote_is_direct(version["text"], excerpt):
            relationship = "mentions_only"
        if re.search(r"\b(today|yesterday|tomorrow|monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b", excerpt, re.I):
            relative_date = True
        if relationship == "mentions_only" and score < 0.25:
            continue
        evaluated.append({"snapshot": snapshot, "relationship": relationship, "excerpt": excerpt, "score": score})

    supports = [item for item in evaluated if item["relationship"] == "supports"]
    conflicts = [item for item in evaluated if item["relationship"] == "conflicts"]
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


def _inspect_verification_leads(connection, run, gap_bundle, leads):
    inspected = []
    allowed_claims = gap_bundle["claims"]
    for proposed in (leads or [])[:VERIFICATION_MAX_LEADS]:
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
    return inspected


def run_verification_job(run_id, provider=None):
    with connect() as connection:
        run = connection.execute("SELECT * FROM verification_runs WHERE id=?", (run_id,)).fetchone()
        if run is None:
            raise KeyError(run_id)
        if run["status"] != "QUEUED":
            return dict(run)
        provider = provider or verification_provider_for("test" if run["mode"] == "test" else "grok")
        _record_verification_status(
            connection, run_id, "QUEUED", "RUNNING", "Preparing claim-specific evidence gaps",
            started_at=now(), progress=10,
        )
        connection.execute(
            "UPDATE events SET verification_status='RUNNING',updated_at=? WHERE id=?", (now(), run["event_id"])
        )
        run = connection.execute("SELECT * FROM verification_runs WHERE id=?", (run_id,)).fetchone()
        versions = _ensure_claim_versions(connection, run["research_run_id"])
        for version in versions:
            connection.execute(
                "INSERT OR IGNORE INTO verification_run_claims(verification_run_id,claim_version_id,required_for_event) VALUES(?,?,?)",
                (run_id, version["id"], version["required_for_event"]),
            )
        _snapshot_research_evidence(connection, run)
        gap_bundle = _verification_gap_bundle(connection, run, versions)
        connection.execute(
            "UPDATE verification_runs SET progress=25,progress_message='Seeking targeted corroboration' WHERE id=?", (run_id,)
        )
    result = None
    error = None
    attempts = 0
    domains = _verification_domains(run["research_run_id"])
    if run["mode"] == "live" and not domains:
        error = ValueError("no verified corroboration domains are registered")
    else:
        for attempts in range(1, int(run["max_attempts"]) + 1):
            try:
                result = provider.find_corroboration(
                    gap_bundle, allowed_domains=domains, search_turn_limit=int(run["search_turn_limit"]),
                    token_limit=int(run["token_limit"]), timeout_seconds=VERIFICATION_TIMEOUT_SECONDS,
                )
                break
            except ResearchProviderError as caught:
                error = caught
                if not caught.retryable or attempts >= int(run["max_attempts"]):
                    break
                time.sleep(min(2 ** (attempts - 1), 4))
            except Exception as caught:
                error = caught
                break
    provider_failure = None
    if result is None:
        safe_message = str(error or "verification provider failed")[:500]
        safe_code = getattr(error, "code", "verification_error")
        provider_failure = (safe_code, safe_message)
        result = VerificationProviderResult(result={
            "search_summary": "The provider failed before returning new discovery leads.",
            "unresolved_gaps": gap_bundle["gaps"],
            "leads": [],
        })
        log_error("verification_failed", safe_message, run_id=run_id, error_code=safe_code)
    with connect() as connection:
        run = connection.execute("SELECT * FROM verification_runs WHERE id=?", (run_id,)).fetchone()
        connection.execute(
            "UPDATE verification_runs SET progress=50,progress_message='Inspecting discovered source pages' WHERE id=?",
            (run_id,),
        )
        _inspect_verification_leads(connection, run, gap_bundle, result.result.get("leads") or [])
        _regroup_evidence_families(connection, run_id)
        snapshots = [dict(row) for row in connection.execute(
            "SELECT * FROM verification_snapshots WHERE verification_run_id=? ORDER BY id", (run_id,)
        )]
        connection.execute(
            "UPDATE verification_runs SET progress=75,progress_message='Evaluating claims by evidence family' WHERE id=?",
            (run_id,),
        )
        decisions = []
        for version in versions:
            decision, rationale, missing, evidence, family_count = _evaluate_verification_claim(
                connection, run, version, snapshots
            )
            decision_id = "VD-" + uuid.uuid4().hex[:12].upper()
            approved = int(decision == "SUPPORTED")
            connection.execute(
                "INSERT INTO verification_decisions(id,verification_run_id,claim_version_id,decision,approved,"
                "required_for_event,independent_family_count,rationale,missing_information_json,decided_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    decision_id, run_id, version["id"], decision, approved, version["required_for_event"],
                    family_count, rationale, json.dumps(missing, ensure_ascii=False), now(),
                ),
            )
            if run["mode"] == "live":
                ledger_status = decision if decision in ("SUPPORTED", "CONFLICTED", "INSUFFICIENT_EVIDENCE") else "INSUFFICIENT_EVIDENCE"
                ledger_claim = connection.execute(
                    "SELECT verification_status FROM claims WHERE id=?", (version["claim_id"],)
                ).fetchone()
                if ledger_claim and ledger_claim["verification_status"] != ledger_status:
                    connection.execute(
                        "UPDATE claims SET verification_status=?,updated_at=? WHERE id=?",
                        (ledger_status, now(), version["claim_id"]),
                    )
                    connection.execute(
                        "INSERT INTO claim_status_history(claim_id,from_status,to_status,reason,changed_at) "
                        "VALUES(?,?,?,?,?)",
                        (
                            version["claim_id"], ledger_claim["verification_status"], ledger_status,
                            f"Verification run {run_id}: {rationale}", now(),
                        ),
                    )
            family_sizes = {}
            for item in snapshots:
                family_sizes[item["evidence_family_id"]] = family_sizes.get(item["evidence_family_id"], 0) + 1
            for item in evidence:
                snapshot = item["snapshot"]
                directness = (
                    "direct_primary" if snapshot["source_class"] == "official_primary"
                    else "syndicated_report" if family_sizes[snapshot["evidence_family_id"]] > 1
                    else "independent_report"
                )
                connection.execute(
                    "INSERT INTO verification_decision_evidence(decision_id,snapshot_id,relationship,excerpt,excerpt_valid,"
                    "directness,evidence_family_id,rationale) VALUES(?,?,?,?,1,?,?,?)",
                    (
                        decision_id, snapshot["id"], item["relationship"], item["excerpt"], directness,
                        snapshot["evidence_family_id"], f"Claim-token overlap {item['score']:.2f}; inspected full stored page.",
                    ),
                )
            decisions.append({
                "id": decision_id, "claim_version_id": version["id"], "decision": decision,
                "approved": approved, "required": bool(version["required_for_event"]),
            })
        required_pass = all(item["decision"] == "SUPPORTED" for item in decisions if item["required"])
        has_required = any(item["required"] for item in decisions)
        production_pass = run["mode"] == "live" and not provider_failure and has_required and required_pass
        if run["mode"] == "test":
            set_status = "TEST_ONLY"
            explanation = "TEST DATA verification is excluded from production event verification."
        elif provider_failure:
            set_status = "REVIEW_REQUIRED"
            explanation = (
                "Corroboration provider failed before returning inspectable leads; existing evidence was evaluated, "
                "but the event remains REVIEW_REQUIRED."
            )
        elif production_pass:
            set_status = "APPROVED"
            explanation = "Every required claim passed the documented claim-specific corroboration policy."
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
            (claim_set_id, run["event_id"], run_id, set_version, final_version, run["claim_set_version"], set_status, now()),
        )
        for item in decisions:
            if item["approved"]:
                connection.execute(
                    "INSERT INTO approved_claim_set_items(claim_set_id,claim_version_id,verification_decision_id) VALUES(?,?,?)",
                    (claim_set_id, item["claim_version_id"], item["id"]),
                )
        summary = {
            **result.result,
            "domains": domains,
            "inspected_sources": sum(lead["status"] == "INGESTED" for lead in connection.execute(
                "SELECT status FROM verification_leads WHERE verification_run_id=?", (run_id,)
            )),
            "claim_set_id": claim_set_id,
            "claim_set_status": set_status,
        }
        _record_verification_status(
            connection, run_id, "RUNNING", "FAILED" if provider_failure else "COMPLETED",
            provider_failure[1] if provider_failure else "Verification decisions complete",
            completed_at=now(), progress=100, attempt_count=attempts,
            final_evidence_version=final_version, actual_search_calls=result.actual_search_calls,
            actual_open_calls=result.actual_open_calls, actual_sources_returned=result.actual_sources_returned,
            input_tokens=result.input_tokens, output_tokens=result.output_tokens, total_tokens=result.total_tokens,
            cost_usd=result.cost_usd, cost_usd_ticks=result.cost_usd_ticks,
            cost_status="known" if result.cost_usd is not None else "unknown",
            provider_request_id=result.provider_request_id, provider_elapsed_seconds=result.elapsed_seconds,
            decision_explanation=explanation, summary_json=json.dumps(summary, ensure_ascii=False),
            error_code=provider_failure[0] if provider_failure else None,
            error_message=provider_failure[1] if provider_failure else None,
        )
        event = connection.execute("SELECT status FROM events WHERE id=?", (run["event_id"],)).fetchone()
        if production_pass and event["status"] == "VERIFYING":
            connection.execute(
                "UPDATE events SET status='VERIFIED',verification_status='VERIFIED',updated_at=? WHERE id=?",
                (now(), run["event_id"]),
            )
            connection.execute(
                "INSERT INTO transitions(event_id,from_state,to_state,at) VALUES(?,'VERIFYING','VERIFIED',?)",
                (run["event_id"], now()),
            )
        else:
            verification_status = "TEST_ONLY" if run["mode"] == "test" else "REVIEW_REQUIRED"
            connection.execute(
                "UPDATE events SET verification_status=?,updated_at=? WHERE id=?",
                (verification_status, now(), run["event_id"]),
            )


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
    if not any(
        item["availability_status"] == "available" and item["rights_status"] == "verified"
        and item["media_type"] in ("image", "video")
        for item in bundle["media"]
    ):
        missing.append("No rights-verified image or video is currently available for content production.")
    return {
        "decision": "HOLD",
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
    allowed_decisions = {"CREATE", "HOLD", "MONITOR", "SKIP", "HUMAN_REVIEW"}
    allowed_formats = {"REEL", "STORY", "CAROUSEL", "IMAGE"}
    decision = proposal.get("decision") if proposal.get("decision") in allowed_decisions else "HUMAN_REVIEW"
    content_format = proposal.get("recommended_format") if proposal.get("recommended_format") in allowed_formats else "IMAGE"
    missing = [str(item) for item in proposal.get("missing_evidence_or_media", [])]
    rationale = str(proposal.get("factual_rationale") or "Provider returned no factual decision rationale.")
    if bundle["recent_duplicate"] and decision == "CREATE":
        decision = "SKIP"
        rationale = "Recent publishing history already covers this event and approved claim-set version."
    if decision == "CREATE" and not _format_supported(content_format, bundle["media"]):
        decision = "HOLD"
        missing.append(f"Available rights-verified media does not support {content_format}.")
        rationale += " The proposed format failed the deterministic media-eligibility check."
    claim_set = bundle["approved_claim_set"]
    return {
        "decision": decision,
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
    connection.execute(
        "INSERT INTO content_decisions(id,run_id,event_id,decision,recommended_format,language,"
        "proposed_duration_seconds,priority,factual_rationale,approved_claim_set_id,approved_claim_set_version,"
        "evidence_version,missing_evidence_or_media_json,executable,test_only,decided_at,policy_version,input_version) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            decision_id, run["id"], run["event_id"], proposal["decision"], proposal["recommended_format"],
            proposal["language"], proposal["proposed_duration_seconds"], proposal["priority"],
            proposal["factual_rationale"], proposal.get("approved_claim_set_id"),
            proposal.get("approved_claim_set_version"), proposal.get("evidence_version"),
            json.dumps(proposal.get("missing_evidence_or_media") or [], ensure_ascii=False),
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
            "decision": "HUMAN_REVIEW", "recommended_format": "IMAGE", "language": "English",
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
    if not _format_supported(decision["recommended_format"], media):
        blockers.append(f"Current rights-cleared media does not support {decision['recommended_format']}.")
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


def enqueue_production(content_decision_id, provider_name="anthropic", *, background=True, regenerate=False):
    provider = production_provider_for(provider_name)
    snapshot = _production_eligibility(content_decision_id)
    if not snapshot["eligible"]:
        raise ValueError("Production entry gate blocked: " + " ".join(snapshot["blockers"]))
    timestamp = now()
    with connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        active = connection.execute(
            "SELECT * FROM production_jobs WHERE content_decision_id=? AND status IN ('QUEUED','GENERATING','VALIDATING')",
            (content_decision_id,),
        ).fetchone()
        if active:
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
            "language": decision["language"], "proposed_duration_seconds": decision["proposed_duration_seconds"],
            "priority": decision["priority"], "factual_rationale": decision["factual_rationale"],
        },
        "approved_claim_set": {"id": job["approved_claim_set_id"], "version": job["approved_claim_set_version"]},
        "approved_claims": [{
            "claim_version_id": item["claim_version_id"], "text": item["text"], "claim_type": item["claim_type"],
            "assertion_scope": item["assertion_scope"], "attribution": item["attribution"],
            "required_for_event": item["required_for_event"],
        } for item in claims],
        "rights_cleared_media": [{
            "media_asset_id": item["media_asset_id"], "media_type": item["media_type"], "url": item["url"],
            "content_hash": item["content_hash"],
        } for item in media],
        "constraints": ["Use only approved_claims for factual content.", "Do not browse or research.",
                        "Preserve attribution and certainty.", "No publishing or rendering is authorized."],
    }


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
        version_number = connection.execute(
            "SELECT COALESCE(MAX(version_number),0)+1 FROM content_packages WHERE event_id=?", (job["event_id"],)
        ).fetchone()[0]
        package_json = json.dumps(result.package, ensure_ascii=False, sort_keys=True)
        snapshot_ids = [row["snapshot_id"] for row in connection.execute(
            "SELECT DISTINCT snapshot_id FROM production_job_evidence WHERE job_id=? ORDER BY snapshot_id", (job_id,)
        )]
        connection.execute(
            "INSERT INTO content_packages(id,job_id,event_id,version_number,status,requested_format,story_angle,"
            "content_objective,package_json,content_hash,approved_claim_set_id,approved_claim_set_version,"
            "approved_claim_version_ids_json,evidence_snapshot_ids_json,evidence_version,media_version,"
            "production_policy_version,prompt_schema_version,provider,model,provider_mode,fixture_only,"
            "provider_request_id,input_tokens,output_tokens,cache_creation_input_tokens,cache_read_input_tokens,"
            "total_tokens,latency_ms,cost_usd,cost_status,validation_result_json,created_at) "
            "VALUES(?,?,?,?, 'READY_FOR_APPROVAL',?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "CP-" + uuid.uuid4().hex[:12].upper(), job_id, job["event_id"], version_number,
                job["requested_format"], result.package["story_angle"], result.package["content_objective"],
                package_json, hashlib.sha256(package_json.encode()).hexdigest(), job["approved_claim_set_id"],
                job["approved_claim_set_version"], json.dumps(validation["approved_claim_version_ids"]),
                json.dumps(snapshot_ids), job["evidence_version"], job["media_version"], job["production_policy_version"],
                job["prompt_schema_version"], job["provider"], job["model"], job["provider_mode"], job["fixture_only"],
                result.provider_request_id, result.input_tokens, result.output_tokens, result.cache_creation_input_tokens,
                result.cache_read_input_tokens, result.total_tokens, result.latency_ms, result.cost_usd,
                "known" if result.cost_usd is not None else "unknown", json.dumps(validation), now(),
            ),
        )
        connection.execute(
            "UPDATE production_jobs SET validation_status='PASSED',validation_result_json=?,updated_at=? WHERE id=?",
            (json.dumps(validation), now(), job_id),
        )
        _transition_production_job(connection, job_id, "READY_FOR_APPROVAL", "Validated immutable package is ready for human approval.")
        connection.commit()
    return production_job(job_id)


def production_job(job_id):
    with connect() as connection:
        row = connection.execute("SELECT * FROM production_jobs WHERE id=?", (job_id,)).fetchone()
    if row is None:
        raise KeyError(job_id)
    return dict(row)


def _default_render_media_type(content_format):
    return {"IMAGE": "IMAGE", "REEL": "VIDEO", "STORY": "IMAGE", "CAROUSEL": "IMAGE"}.get(content_format)


def _render_eligibility(content_package_id, media_type=None):
    blockers = []
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
        if media_type != expected:
            blockers.append(f"Content format {package['requested_format']} currently requires media type {expected}.")
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
        production = _production_eligibility(package["content_decision_id"])
    except Exception as error:
        production = None
        blockers.append(f"Could not revalidate package lineage: {error}")
    if not production or not production["eligible"]:
        blockers.extend(production["blockers"] if production else [])
    elif production["input_version"] != package["production_input_version"]:
        blockers.append("The ProductionJob input version is no longer current.")
    if production and package["evidence_version"] != production["evidence_version"]:
        blockers.append("The package evidence version is stale.")
    if production and package["media_version"] != production["media_version"]:
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
    return {
        "eligible": not blockers, "blockers": list(dict.fromkeys(blockers)), "package": package,
        "package_payload": prompt_package, "media_type": media_type, "input_media": input_media,
        "claim_version_ids": claim_ids, "evidence_snapshot_ids": evidence_ids,
        "input_version": _version_hash(lineage),
    }


def _build_render_request(gate, provider, regeneration_number):
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
    if media_type == "IMAGE":
        prompts = [scene["visual_prompt"] for scene in package["storyboard"] if scene.get("visual_prompt")]
        if not prompts or not package.get("thumbnail"):
            raise ValueError("The ContentPackage is missing required image creative instructions.")
        aspect = package["platform_metadata"]["aspect_ratio"]
        width, height = (1200, 1500) if aspect == "4:5" else (1080, 1080)
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
        common.update({
            "storyboard": package["storyboard"], "script": package["script"],
            "generation_parameters": {
                "duration_seconds": package["platform_metadata"]["duration_seconds"],
                "aspect_ratio": package["platform_metadata"]["aspect_ratio"],
            },
        })
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
    renderer=None, visual_qa_provider=None,
):
    gate = _render_eligibility(content_package_id, media_type)
    if not gate["eligible"]:
        raise ValueError("Render entry gate blocked: " + " ".join(gate["blockers"]))
    if renderer is None:
        provider_name = provider_name or configured_renderer_name(gate["media_type"])
        if not provider_name:
            raise MissingRendererConfiguration(
                f"LIVE_RENDERER_NOT_CONFIGURED: No live renderer is configured for {gate['media_type']}."
            )
        configuration = renderer_configuration(gate["media_type"])
        if provider_name != "fixture" and not configuration["live"]:
            raise MissingRendererConfiguration(configuration["status"])
        renderer = renderer_for(provider_name, gate["media_type"])
    timestamp = now()
    with connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        active = connection.execute(
            "SELECT * FROM render_jobs WHERE content_package_id=? AND media_type=? "
            "AND status IN ('QUEUED','PREPARING','RENDERING','VALIDATING')",
            (content_package_id, gate["media_type"]),
        ).fetchone()
        if active:
            connection.commit()
            return {"job": dict(active), "duplicate": True, "cached": False}
        prior = connection.execute(
            "SELECT * FROM render_jobs WHERE content_package_id=? AND media_type=? ORDER BY regeneration_number DESC LIMIT 1",
            (content_package_id, gate["media_type"]),
        ).fetchone()
        if prior and not regenerate:
            connection.commit()
            return {"job": dict(prior), "duplicate": False, "cached": True}
        regeneration_number = prior["regeneration_number"] + 1 if prior else 1
        request = _build_render_request(gate, renderer, regeneration_number)
        request_json = json.dumps(request, ensure_ascii=False, sort_keys=True)
        idempotency_key = _version_hash({
            "package": content_package_id, "version": gate["package"]["version_number"],
            "media_type": gate["media_type"], "provider": renderer.name, "model": renderer.model,
            "config": RENDER_GENERATION_CONFIG_VERSION, "renderer_config": RENDERER_CONFIG_VERSION,
            "qa_policy": MEDIA_QA_POLICY_VERSION, "regeneration": regeneration_number,
        })
        job_id = "RJ-" + uuid.uuid4().hex[:12].upper()
        connection.execute(
            "INSERT INTO render_jobs(id,event_id,content_decision_id,production_job_id,content_package_id,"
            "content_package_version,media_type,provider,model,provider_mode,prompt_version,generation_config_version,"
            "input_version,input_media_asset_ids_json,idempotency_key,regeneration_number,max_retries,status,fixture_only,"
            "created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'QUEUED',?,?,?)",
            (
                job_id, gate["package"]["event_id"], gate["package"]["content_decision_id"], gate["package"]["job_id"],
                content_package_id, gate["package"]["version_number"], gate["media_type"], renderer.name, renderer.model,
                renderer.mode, RENDER_PROMPT_VERSION, RENDER_GENERATION_CONFIG_VERSION, gate["input_version"],
                json.dumps(request["reference_asset_ids"]), idempotency_key, regeneration_number, RENDER_MAX_RETRIES,
                int(renderer.mode == "fixture"), timestamp, timestamp,
            ),
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
        RENDER_EXECUTOR.submit(run_render_job, job_id, renderer, None, visual_qa_provider)
    else:
        run_render_job(job_id, renderer, visual_qa_provider=visual_qa_provider)
        job = render_job(job_id)
    return {"job": job, "duplicate": False, "cached": False}


def _parse_png(data):
    if not isinstance(data, (bytes, bytearray)) or not data.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValueError("PNG signature is invalid.")
    offset, width, height, idat, ended = 8, None, None, [], False
    while offset + 12 <= len(data):
        length = int.from_bytes(data[offset:offset + 4], "big")
        kind = data[offset + 4:offset + 8]
        chunk = data[offset + 8:offset + 8 + length]
        checksum = int.from_bytes(data[offset + 8 + length:offset + 12 + length], "big")
        if len(chunk) != length or (zlib.crc32(kind + chunk) & 0xFFFFFFFF) != checksum:
            raise ValueError("PNG chunk is truncated or corrupt.")
        if kind == b"IHDR":
            width, height, bit_depth, color_type, compression, filtering, interlace = struct.unpack(">IIBBBBB", chunk)
            if (bit_depth, color_type, compression, filtering, interlace) != (8, 2, 0, 0, 0):
                raise ValueError("PNG encoding is outside the supported validation profile.")
        elif kind == b"IDAT":
            idat.append(chunk)
        elif kind == b"IEND":
            ended = True
            break
        offset += 12 + length
    if not width or not height or not idat or not ended:
        raise ValueError("PNG is missing required chunks.")
    decoded = zlib.decompress(b"".join(idat))
    if len(decoded) != height * (1 + width * 3):
        raise ValueError("PNG pixel payload has an invalid length.")
    if any(decoded[row * (1 + width * 3)] != 0 for row in range(height)):
        raise ValueError("PNG uses an unsupported scanline filter.")
    return width, height


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
    parsed_width = parsed_height = None
    if media_type == "IMAGE":
        if result.mime_type != "image/png":
            errors.append("IMAGE result must use the expected image/png MIME type.")
        else:
            try:
                parsed_width, parsed_height = _parse_png(result.asset_bytes)
            except Exception as error:
                errors.append(f"Generated image is corrupt: {error}")
        if parsed_width and (parsed_width < RENDER_MIN_IMAGE_WIDTH or parsed_height < RENDER_MIN_IMAGE_HEIGHT):
            errors.append("Generated image dimensions are below minimum policy.")
        if parsed_width and (result.width != parsed_width or result.height != parsed_height):
            errors.append("Provider-reported dimensions do not match the decoded image.")
        expected = request["generation_parameters"]
        if parsed_width and abs(parsed_width / parsed_height - expected["width"] / expected["height"]) > RENDER_ASPECT_TOLERANCE:
            errors.append("Generated image aspect ratio does not match the package request.")
        if result.image_count not in (None, expected["output_count"]):
            errors.append("Provider output count does not match the request.")
    elif media_type in ("VIDEO", "SHORT_FORM_VIDEO", "LONG_FORM_VIDEO"):
        if result.mime_type not in ("video/mp4", "video/webm"):
            errors.append("VIDEO result has an invalid MIME type.")
        if not result.duration_seconds or result.duration_seconds <= 0:
            errors.append("Generated video has no valid duration metadata.")
    elif media_type in ("AUDIO", "VOICEOVER"):
        if result.mime_type not in ("audio/wav", "audio/mpeg", "audio/mp4"):
            errors.append("AUDIO result has an invalid MIME type.")
        if not result.duration_seconds or result.duration_seconds <= 0:
            errors.append("Generated audio has no valid duration metadata.")
    return {
        "valid": not errors, "errors": errors, "validator_version": RENDER_POLICY_VERSION,
        "checksum_sha256": stored.checksum_sha256, "file_size": stored.file_size,
        "decoded_width": parsed_width, "decoded_height": parsed_height,
    }


def _persist_provider_events(connection, job_id, attempt_number, lifecycle_events, provider_job_id=None):
    for event in lifecycle_events or ():
        event_type = event.get("event_type", "POLLED")
        if event_type not in ("SUBMITTED", "POLLED", "RATE_LIMITED", "COMPLETED", "FAILED", "DOWNLOADED"):
            event_type = "POLLED"
        connection.execute(
            "INSERT INTO render_provider_events(render_job_id,attempt_number,event_type,provider_status,"
            "provider_job_id,safe_metadata_json,occurred_at) VALUES(?,?,?,?,?,?,?)",
            (
                job_id, attempt_number, event_type, event.get("status"), event.get("provider_job_id") or provider_job_id,
                json.dumps(_scrub_provider_metadata(event), ensure_ascii=False, sort_keys=True),
                event.get("at") or now(),
            ),
        )


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
        "technical_validation_status,text_validation_status,semantic_qa_status,human_review_status) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            asset_id, job["id"], job["event_id"], job["content_package_id"], job["content_package_version"], job["media_type"], version,
            status, int(status == "VALIDATED" and not job["fixture_only"]), job["fixture_only"], job["provider"], job["model"],
            result.provider_asset_id, result.provider_request_id, _safe_provider_url(result.original_provider_url), stored.storage_uri,
            result.mime_type, result.width, result.height, result.duration_seconds, result.frame_rate, stored.file_size,
            stored.checksum_sha256, job["prompt_version"], job["generation_config_version"],
            job["input_media_asset_ids_json"], json.dumps(provenance, sort_keys=True),
            json.dumps(_scrub_provider_metadata(result.provider_metadata or {}), sort_keys=True), json.dumps(list(result.detected_text)),
            "PASSED" if ready else "FAILED", json.dumps(validation, ensure_ascii=False), now(),
            int(validation["technical"]["valid"] and not stale), int(stale),
            "Lineage changed during rendering." if stale else None,
            "PASSED" if validation["technical"]["valid"] else "FAILED",
            text_qa.status, semantic_qa.status, "REQUIRED",
        ),
    )
    connection.execute(
        "INSERT INTO render_job_outputs(render_job_id,generated_asset_id,reused_identical_binary) VALUES(?,?,0)",
        (job["id"], asset_id),
    )
    return asset_id


def run_render_job(job_id, renderer=None, storage=None, visual_qa_provider=None):
    storage = storage or LocalMediaStorage(RENDER_STORAGE_ROOT)
    visual_qa_provider = visual_qa_provider or NoopVisualQAProvider()
    with connect() as connection:
        job_row = connection.execute("SELECT * FROM render_jobs WHERE id=?", (job_id,)).fetchone()
        if job_row is None:
            raise KeyError(job_id)
        if job_row["status"] != "QUEUED":
            return dict(job_row)
        renderer = renderer or renderer_for(
            "fixture" if job_row["provider_mode"] == "fixture" else job_row["provider"], job_row["media_type"]
        )
        _transition_render_job(connection, job_id, "PREPARING", "Revalidating package lineage before renderer call.")
        connection.execute("UPDATE render_jobs SET started_at=? WHERE id=?", (now(), job_id))
    job = render_job(job_id)
    gate = _render_eligibility(job["content_package_id"], job["media_type"])
    if not gate["eligible"] or gate["input_version"] != job["input_version"]:
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
        _transition_render_job(connection, job_id, "RENDERING", "Calling configured renderer with immutable prompt snapshot.")
        prompt = connection.execute("SELECT request_json FROM render_prompt_snapshots WHERE render_job_id=?", (job_id,)).fetchone()
    request = json.loads(prompt["request_json"])
    result = None
    last_error = None
    for attempt in range(1, job["max_retries"] + 2):
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
    if result is None:
        code = getattr(last_error, "code", "UNKNOWN_PROVIDER_ERROR")
        safe_error = _safe_render_error(last_error)
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
        return render_job(job_id)
    with connect() as connection:
        connection.execute(
            "UPDATE render_jobs SET provider_called=1,provider_job_id=?,provider_request_id=?,provider_status=?,"
            "submitted_at=?,last_polled_at=?,poll_count=?,provider_started_at=?,provider_completed_at=?,latency_ms=?,"
            "request_count=?,credits_consumed=?,provider_units=?,input_units=?,output_units=?,generation_seconds=?,"
            "frame_count=?,image_count=?,provider_cost_usd=?,calculated_cost_usd=?,cost_status=?,currency=?,"
            "pricing_version=?,updated_at=? WHERE id=?",
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
        return render_job(job_id)
    current = _render_eligibility(job["content_package_id"], job["media_type"])
    stale = not current["eligible"] or current["input_version"] != job["input_version"]
    if stale:
        technical["valid"] = False
        technical["errors"] = list(dict.fromkeys(
            technical["errors"] + current["blockers"] + ["Lineage changed during rendering."]
        ))
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
                    "checksum_sha256": stored.checksum_sha256, "mime_type": result.mime_type,
                    "width": result.width, "height": result.height, "duration_seconds": result.duration_seconds,
                },
                content_package=gate["package_payload"], approved_claims=claims,
                expected_entities=["N. Chandrababu Naidu", "Andhra Pradesh"],
                expected_visual_description=request.get("visual_prompts") or request.get("storyboard") or (),
            )
            if semantic_qa.status not in ("PASSED", "FLAGGED", "NOT_PERFORMED"):
                raise ValueError("Visual QA provider returned an invalid status.")
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
                json.dumps(overall, ensure_ascii=False), None if ready else ("STALE_INPUTS" if stale else "MEDIA_QA_FLAGGED"),
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
            verification["leads"] = [dict(row) for row in connection.execute(
                "SELECT * FROM verification_leads WHERE verification_run_id=? ORDER BY discovered_at,id",
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
            "SELECT id,requested_format FROM content_packages WHERE event_id=? ORDER BY version_number DESC LIMIT 1",
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
        cost_ledger = [dict(row) for row in connection.execute(
            "SELECT * FROM cost_ledger WHERE event_id=? ORDER BY recorded_at,id", (event_id,)
        )]
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
    render_gate = {
        "eligible": False, "blockers": ["No ContentPackage exists."], "content_package_id": None,
        "media_type": None, "configured_provider": None, "live_renderer_configured": False,
        "renderer_configuration_status": "LIVE_RENDERER_NOT_CONFIGURED",
    }
    if latest_package:
        requested_type = _default_render_media_type(latest_package["requested_format"])
        gate = _render_eligibility(latest_package["id"], requested_type)
        configuration = renderer_configuration(requested_type)
        render_gate = {
            "eligible": gate["eligible"], "blockers": gate["blockers"],
            "content_package_id": latest_package["id"], "media_type": requested_type,
            "input_version": gate["input_version"], "configured_provider": configuration["provider"],
            "live_renderer_configured": configuration["live"],
            "renderer_configuration_status": configuration["status"],
        }
    return {
        "event": dict(event), "signals": signals, "claims": claims, "runs": runs,
        "verification_runs": verification_runs, "approved_claim_sets": approved_sets,
        "content_decision_runs": content_runs, "media_assets": media_assets,
        "publishing_history": publishing,
        "production_jobs": production_jobs, "production_gate": production_gate,
        "render_jobs": render_jobs, "render_gate": render_gate,
        "cost_ledger": cost_ledger,
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
        },
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
        self.wfile.write(data)

    def read_json(self):
        length = int(self.headers.get("Content-Length", "0"))
        if length > 10_000:
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
            match = re.fullmatch(r"/api/events/([^/]+)/content-decision", path)
            if match:
                result = enqueue_content_decision(match.group(1), body.get("provider", "test"), background=True)
                self.send_json(result, 200 if result["duplicate"] or result["cached"] else 202)
                return
            match = re.fullmatch(r"/api/content-decisions/([^/]+)/production", path)
            if match:
                result = enqueue_production(
                    match.group(1), body.get("provider", "anthropic"), background=True,
                    regenerate=bool(body.get("regenerate")),
                )
                self.send_json(result, 200 if result["duplicate"] or result["cached"] else 202)
                return
            match = re.fullmatch(r"/api/content-packages/([^/]+)/render", path)
            if match:
                result = enqueue_render(
                    match.group(1), body.get("media_type"), body.get("provider"), background=True,
                    regenerate=bool(body.get("regenerate")),
                )
                self.send_json(result, 200 if result["duplicate"] or result["cached"] else 202)
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
    port = int(os.environ.get("PORT", "8000"))
    host = os.environ.get("HOST", "127.0.0.1")
    print(f"ReachOut dashboard: http://{host}:{port}", flush=True)
    ThreadingHTTPServer((host, port), Handler).serve_forever()
