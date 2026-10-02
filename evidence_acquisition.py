"""Architecture 10 — EVIDENCE_ACQUISITION_V1.

Bounded, claim-directed evidence acquisition. When required claims resolve to
INSUFFICIENT_EVIDENCE, this runs at most TWO acquisition passes over the existing,
provider-neutral A-F discovery plan, then stops with REVIEW_REQUIRED. Search snippets are
discovery only — a page is fetched and snapshotted before it can become evidence.

Direct-evidence priority (A.3):
  1 official government release > 2 official institution/event release > 3 official
  speech/transcript > 4 official participant statement > 5 established wire > 6 independent
  established news. Query generation is claim-directed (English / Telugu / Romanized) and
  passes run in bounded parallelism with no provider duplication.
"""

from datetime import datetime, timezone
import re

MAX_PASSES = 2
MAX_CONCURRENCY = 4

# Support types used by the claim-source matrix (A.4).
SUPPORT_TYPES = ("DIRECT_SUPPORT", "PARTIAL_SUPPORT", "MENTIONS_ONLY", "CONTRADICTS", "IRRELEVANT")

# Direct-evidence priority tiers, highest first.
EVIDENCE_PRIORITY = (
    ("official_government", 1),
    ("official_institution", 2),
    ("official_speech", 3),
    ("official_participant", 4),
    ("established_wire", 5),
    ("independent_news", 6),
)

_OFFICIAL_GOV = re.compile(r"\.(gov\.in|gov|nic\.in)\b|pib\.gov|ap\.gov|cm\.ap\.gov", re.I)
_WIRE = re.compile(r"\b(pti|ani|ians|reuters|ap news|bloomberg)\b", re.I)


def _now():
    return datetime.now(timezone.utc).isoformat()


def claim_queries(claim_text, *, entities=None, location=None, max_queries=6):
    """Claim-directed query variants: English + Telugu + Romanized (A.2).

    Exact claim resolution is prioritized over broad event search.
    """
    from fast_discovery import expand_query
    base = re.sub(r"\s+", " ", str(claim_text or "")).strip()
    # Strip leading articles so the phrase is search-friendly.
    base = re.sub(r"^(the|a|an)\s+", "", base, flags=re.I)
    queries, seen = [], set()

    def add(value):
        value = re.sub(r"\s+", " ", str(value or "")).strip()
        if value and value.casefold() not in seen:
            seen.add(value.casefold())
            queries.append(value)

    # 1) The exact claim phrase.
    add(base)
    # 2) Entity + location combinations (targeted).
    for entity in (entities or [])[:3]:
        add(f"{entity} {location}" if location else entity)
    # 3) Multilingual / Romanized variants of the claim and entities.
    for variant in expand_query(base):
        add(variant)
    for entity in (entities or [])[:2]:
        for variant in expand_query(entity):
            add(variant)
    # 4) Fallback: entity-only.
    for entity in (entities or [])[:2]:
        add(entity)
    return queries[:max_queries]


def _source_tier(url, source_class, source_name=""):
    text = f"{url or ''} {source_name or ''}"
    if _OFFICIAL_GOV.search(url or ""):
        return "official_government", 1
    if _WIRE.search(text):
        return "established_wire", 5
    if source_class == "official_primary":
        return "official_institution", 2
    return "independent_news", 6


def prioritize_leads(leads):
    """Sort discovered leads by direct-evidence priority (A.3), then by claim targeting."""
    def key(lead):
        tier, rank = _source_tier(lead.get("url"), lead.get("source_class"))
        return (rank, -len(lead.get("claim_ids") or []))
    return sorted(leads, key=key)


def acquisition_plan(*, connect, verification_run_id, entities=None, location=None):
    """Build claim-directed target queries for one verification run (reads claim versions)."""
    with connect() as connection:
        run = connection.execute("SELECT * FROM verification_runs WHERE id=?", (verification_run_id,)).fetchone()
        if run is None:
            raise KeyError(verification_run_id)
        versions = [dict(r) for r in connection.execute(
            "SELECT id,claim_id,text,required_for_event FROM claim_versions WHERE "
            "claim_id IN (SELECT id FROM claims WHERE event_id=?) AND revoked_at IS NULL",
            (run["event_id"],))]
    plan = []
    for version in versions:
        plan.append({
            "claim_id": version["claim_id"], "claim_version_id": version["id"],
            "required": bool(version["required_for_event"]),
            "queries": claim_queries(version["text"], entities=entities, location=location),
        })
    return plan


def record_run(*, connect, verification_run_id, event_id, pass_number, status, queries,
               urls_discovered=0, urls_fetched=0, parallel=False, started_at=None, now=None):
    run_id = "EA-" + __import__("uuid").uuid4().hex[:12].upper()
    timestamp = now() if now else _now()
    with connect() as connection:
        connection.execute(
            "INSERT INTO evidence_acquisition_runs(id,verification_run_id,event_id,pass_number,status,"
            "queries_json,urls_discovered,urls_fetched,parallel,started_at,completed_at,duration_ms) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (run_id, verification_run_id, event_id, pass_number, status,
             __import__("json").dumps(queries, ensure_ascii=False), urls_discovered, urls_fetched,
             int(parallel), started_at or timestamp, timestamp, None))
    return run_id


def passes_used(*, connect, verification_run_id):
    with connect() as connection:
        return connection.execute(
            "SELECT COUNT(*) FROM evidence_acquisition_runs WHERE verification_run_id=?",
            (verification_run_id,)).fetchone()[0]


def can_run_pass(*, connect, verification_run_id):
    """Bound the number of acquisition passes (A.1): at most MAX_PASSES."""
    return passes_used(connect=connect, verification_run_id=verification_run_id) < MAX_PASSES


def run_parallel(executors, *, max_concurrency=MAX_CONCURRENCY):
    """Run independent callables concurrently with a bounded pool; returns results in order.

    Bounded concurrency prevents a retry storm; only independent work is parallelized.
    """
    from concurrent.futures import ThreadPoolExecutor
    if not executors:
        return []
    results = [None] * len(executors)
    with ThreadPoolExecutor(max_workers=max(1, min(max_concurrency, len(executors)))) as pool:
        futures = {pool.submit(fn): index for index, fn in enumerate(executors)}
        for future, index in futures.items():
            try:
                results[index] = future.result()
            except Exception as error:  # noqa: BLE001 - one lane must not sink the pass
                results[index] = {"error": str(error)}
    return results


def record_matrix_row(*, connect, verification_run_id, claim_id, claim_version_id=None,
                      source_id=None, source_url=None, source_name=None, source_family=None,
                      support_type="MENTIONS_ONLY", support_span=None, published_at=None,
                      source_class=None, now=None):
    """Persist one claim-source matrix row with direct-evidence priority recorded (A.4)."""
    if support_type not in SUPPORT_TYPES:
        raise ValueError(f"invalid support_type: {support_type}")
    tier, rank = _source_tier(source_url, source_class, source_name)
    primary_or_independent = "primary" if rank <= 4 else "independent"
    row_id = "CM-" + __import__("uuid").uuid4().hex[:12].upper()
    timestamp = now() if now else _now()
    with connect() as connection:
        connection.execute(
            "INSERT INTO claim_source_matrix(id,verification_run_id,claim_id,claim_version_id,source_id,"
            "source_url,source_name,source_family,primary_or_independent,support_type,support_span,published_at,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (row_id, verification_run_id, claim_id, claim_version_id, source_id, source_url, source_name,
             source_family or tier, primary_or_independent, support_type, (support_span or "")[:1000],
             published_at, timestamp))
    return {"id": row_id, "primary_or_independent": primary_or_independent, "tier": tier, "rank": rank,
            "support_type": support_type}


def matrix_for_claims(*, connect, verification_run_id):
    with connect() as connection:
        rows = [dict(r) for r in connection.execute(
            "SELECT * FROM claim_source_matrix WHERE verification_run_id=? ORDER BY claim_id,id",
            (verification_run_id,))]
    grouped = {}
    for row in rows:
        grouped.setdefault(row["claim_id"], []).append(row)
    return grouped


def resolve_claims_from_matrix(*, connect, verification_run_id):
    """Apply the strict gate to the matrix: a required claim is resolved only with an official
    primary DIRECT_SUPPORT or >=2 independent DIRECT_SUPPORT families."""
    grouped = matrix_for_claims(connect=connect, verification_run_id=verification_run_id)
    resolved, unresolved = [], []
    for claim_id, rows in grouped.items():
        directs = [r for r in rows if r["support_type"] == "DIRECT_SUPPORT"]
        official = [r for r in directs if r["primary_or_independent"] == "primary"]
        independent_families = {r["source_family"] for r in directs if r["primary_or_independent"] == "independent"}
        if official or len(independent_families) >= 2:
            resolved.append(claim_id)
        else:
            unresolved.append(claim_id)
    return {"resolved": resolved, "unresolved": unresolved}
