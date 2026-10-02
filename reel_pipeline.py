"""AUTO_REEL_PIPELINE_V1 — durable, resumable orchestration of the automated reel factory.

A verified, eligible event progresses automatically:
  EVENT -> SOURCE ACQUISITION -> VERIFICATION -> CONTENT DECISION -> NATURAL NARRATION
        -> MEDIA DISCOVERY -> RIGHTS CHECK -> MEDIA SELECTION -> REEL GENERATION -> QA
        -> READY_FOR_REVIEW

Human intervention happens only when a gate cannot pass. The automatic terminal state is
READY_FOR_REVIEW; the pipeline never publishes or schedules. Every paid/provider step is
idempotent and checkpointed so a restart never repeats work.
"""

from datetime import datetime, timezone
import json
import uuid

PIPELINE_VERSION = "AUTO_REEL_PIPELINE_V1"
FAST_PATH_VERSION = "AUTO_REEL_FAST_PATH_V1"
TERMINAL_STATES = ("READY_FOR_REVIEW", "HALTED", "COMPLETE")
STAGES = ("ELIGIBILITY", "SOURCE_ACQUISITION", "VERIFICATION", "CONTENT_DECISION", "NARRATION",
          "MEDIA_DISCOVERY", "RIGHTS_CHECK", "MEDIA_SELECTION", "REEL_GENERATION", "QA")
# Stages that cost money; never re-run if their checkpoint exists.
PAID_STAGES = ("CONTENT_DECISION", "NARRATION", "MEDIA_DISCOVERY", "REEL_GENERATION")
# Stages that are independent within a group and may run concurrently (Arch 10, Part B.6).
# Each group runs after the previous group completes; the stages inside share no output.
PARALLEL_GROUPS = (
    ("NARRATION", "MEDIA_DISCOVERY"),            # narration prep + real-media discovery
    ("RIGHTS_CHECK", "MEDIA_SELECTION"),          # rights metadata prep + selection
)


class PipelineError(RuntimeError):
    """A stage failed; the pipeline moves to NEEDS_ATTENTION with a reason."""

    def __init__(self, message, *, stage=None, retryable=False, recommended_action=None):
        super().__init__(message)
        self.stage = stage
        self.retryable = retryable
        self.recommended_action = recommended_action


def _now():
    return datetime.now(timezone.utc).isoformat()


def _latest_reel_id(connect, event_id):
    with connect() as connection:
        row = connection.execute(
            "SELECT id FROM final_reel_assets WHERE event_id=? ORDER BY created_at DESC LIMIT 1", (event_id,)
        ).fetchone()
    return row["id"] if row else None


def eligibility(connect, event_id):
    """Auto-eligibility: never start a reel from REVIEW_REQUIRED/insufficient evidence."""
    with connect() as connection:
        event = connection.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
        if event is None:
            return {"eligible": False, "blockers": ["Event does not exist."]}
        blockers = []
        if event["verification_status"] != "VERIFIED":
            blockers.append("Event is not VERIFIED.")
        decision = connection.execute(
            "SELECT * FROM content_decisions WHERE event_id=? ORDER BY decided_at DESC,rowid DESC LIMIT 1",
            (event_id,),
        ).fetchone()
        if not decision or decision["decision"] != "CREATE":
            blockers.append("No executable CREATE content decision.")
        elif not decision["executable"]:
            blockers.append("Content decision is not executable.")
        claim_set = connection.execute(
            "SELECT * FROM approved_claim_sets WHERE event_id=? AND status='APPROVED' "
            "ORDER BY version_number DESC LIMIT 1", (event_id,),
        ).fetchone()
        if not claim_set:
            blockers.append("No APPROVED claim set.")
        if event["research_status"] in ("REVIEW_REQUIRED",) and not claim_set:
            blockers.append("Story has insufficient verified factual material.")
        return {"eligible": not blockers, "blockers": blockers,
                "event": dict(event),
                "decision": dict(decision) if decision else None,
                "claim_set": dict(claim_set) if claim_set else None}


def _record(connection, run_id, stage, status, **detail):
    connection.execute(
        "INSERT INTO reel_pipeline_events(pipeline_run_id,stage,status,detail_json,occurred_at) VALUES(?,?,?,?,?)",
        (run_id, stage, status, json.dumps(detail, ensure_ascii=False, sort_keys=True), _now()),
    )


def open_or_resume(event_id, *, connect, now=_now, language="te"):
    """Create or resume the single pipeline run for an event (idempotent per event)."""
    with connect() as connection:
        existing = connection.execute("SELECT * FROM reel_pipeline_runs WHERE event_id=?", (event_id,)).fetchone()
        if existing:
            return dict(existing)
    run_id = "PR-" + uuid.uuid4().hex[:12].upper()
    timestamp = now()
    with connect() as connection:
        connection.execute(
            "INSERT INTO reel_pipeline_runs(id,event_id,current_stage,status,production_standard_version,"
            "started_at,updated_at,checkpoint_json) VALUES(?,?,?,?,?,?,?,?)",
            (run_id, event_id, "ELIGIBILITY", "RUNNING", _standard_version(), timestamp, timestamp, "{}"),
        )
        _record(connection, run_id, "ELIGIBILITY", "STARTED")
    return pipeline_run(run_id, connect=connect)


def _standard_version():
    try:
        import reel_standard
        return reel_standard.PRODUCTION_STANDARD_VERSION
    except Exception:
        return "REEL_PRODUCTION_STANDARD_V1"


def pipeline_run(run_id, *, connect):
    with connect() as connection:
        row = connection.execute("SELECT * FROM reel_pipeline_runs WHERE id=?", (run_id,)).fetchone()
        if row is None:
            raise KeyError(run_id)
        result = dict(row)
        result["checkpoint"] = json.loads(result.pop("checkpoint_json") or "{}")
        result["events"] = [dict(item) for item in connection.execute(
            "SELECT * FROM reel_pipeline_events WHERE pipeline_run_id=? ORDER BY id", (run_id,)
        )]
    for event in result["events"]:
        event["detail"] = json.loads(event.pop("detail_json") or "{}")
    return result


def _set_stage(connection, run_id, stage, status):
    connection.execute(
        "UPDATE reel_pipeline_runs SET current_stage=?,status=?,updated_at=? WHERE id=?",
        (stage, status, _now(), run_id),
    )
    _record(connection, run_id, stage, status)


def _checkpoint(connection, run_id, **values):
    row = connection.execute("SELECT checkpoint_json FROM reel_pipeline_runs WHERE id=?", (run_id,)).fetchone()
    state = json.loads(row["checkpoint_json"] or "{}")
    state.update(values)
    connection.execute("UPDATE reel_pipeline_runs SET checkpoint_json=? WHERE id=?", (json.dumps(state), run_id))
    return state


def mark_needs_attention(run_id, error, *, connect):
    """A stage failed: record the reason, retryability, and recommended action; never auto-fallback."""
    with connect() as connection:
        row = connection.execute("SELECT retry_count FROM reel_pipeline_runs WHERE id=?", (run_id,)).fetchone()
        retry_count = (row["retry_count"] if row else 0)
        connection.execute(
            "UPDATE reel_pipeline_runs SET status='NEEDS_ATTENTION',failure_stage=?,failure_reason=?,retryable=?,"
            "recommended_action=?,updated_at=? WHERE id=?",
            (getattr(error, "stage", None) or "", str(error)[:600], int(bool(getattr(error, "retryable", False))),
             (getattr(error, "recommended_action", None) or "")[:400], _now(), run_id),
        )
        _record(connection, run_id, getattr(error, "stage", "UNKNOWN") or "UNKNOWN", "FAILED",
                reason=str(error)[:400], retryable=bool(getattr(error, "retryable", False)))
    return pipeline_run(run_id, connect=connect)


# ---------- parallel groups + stage timing (Arch 10, Part B) ----------

def parallel_group_for(stage):
    for group in PARALLEL_GROUPS:
        if stage in group:
            return group
    return (stage,)


def start_timing(connection, run_id, stage, *, attempt=1, parallel_group=None):
    """Open a stage-timing row (stage_started_at) and return its id."""
    timing_id = "ST-" + uuid.uuid4().hex[:12].upper()
    connection.execute(
        "INSERT INTO reel_pipeline_stage_timings(id,pipeline_run_id,stage,attempt,stage_started_at,"
        "parallel_group,created_at) VALUES(?,?,?,?,?,?,?)",
        (timing_id, run_id, stage, attempt, _now(), parallel_group, _now()),
    )
    return timing_id


def finish_timing(connection, timing_id, *, wait_ms=0.0, provider_ms=0.0):
    row = connection.execute(
        "SELECT stage_started_at FROM reel_pipeline_stage_timings WHERE id=?", (timing_id,)).fetchone()
    completed = _now()
    duration_ms = None
    if row:
        try:
            started = datetime.fromisoformat(row["stage_started_at"])
            duration_ms = round((datetime.fromisoformat(completed) - started).total_seconds() * 1000, 2)
        except (TypeError, ValueError):
            duration_ms = None
    connection.execute(
        "UPDATE reel_pipeline_stage_timings SET stage_completed_at=?,duration_ms=?,wait_ms=?,provider_ms=? "
        "WHERE id=?", (completed, duration_ms, wait_ms, provider_ms, timing_id))
    return duration_ms


def stage_timings(run_id, *, connect):
    with connect() as connection:
        return [dict(row) for row in connection.execute(
            "SELECT * FROM reel_pipeline_stage_timings WHERE pipeline_run_id=? ORDER BY id", (run_id,))]


def slowest_stages(*, connect, limit=5):
    """Slowest stages across all runs by average duration_ms (System view, B.7)."""
    with connect() as connection:
        return [dict(row) for row in connection.execute(
            "SELECT stage,COUNT(*) samples,ROUND(AVG(duration_ms),1) avg_ms,ROUND(MAX(duration_ms),1) max_ms "
            "FROM reel_pipeline_stage_timings WHERE duration_ms IS NOT NULL GROUP BY stage "
            "ORDER BY avg_ms DESC LIMIT ?", (limit,))]


def pipeline_percentiles(*, connect):
    """PIPELINE_P50 / PIPELINE_P95 end-to-end run duration from completed runs (B.7)."""
    with connect() as connection:
        durations = sorted(
            (datetime.fromisoformat(row["completed_at"]) - datetime.fromisoformat(row["started_at"])).total_seconds()
            for row in connection.execute(
                "SELECT started_at,completed_at FROM reel_pipeline_runs WHERE completed_at IS NOT NULL")
            if row["completed_at"] and row["started_at"])
    def pct(values, p):
        if not values:
            return None
        return round(values[min(len(values) - 1, int(round((p / 100) * (len(values) - 1))))], 2)
    return {"pipeline_p50_seconds": pct(durations, 50), "pipeline_p95_seconds": pct(durations, 95),
            "samples": len(durations)}


def fast_path_eligibility(connect, event_id):
    """AUTO_REEL_FAST_PATH_V1 (B.8): VERIFIED + approved claims + rights-cleared media + no
    unresolved factual issues. Reuses existing assets; never skips mandatory QA."""
    with connect() as connection:
        event = connection.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
        if event is None:
            return {"fast_path": False, "reasons": ["Event does not exist."]}
        reasons = []
        if event["verification_status"] != "VERIFIED":
            reasons.append("Event is not VERIFIED.")
        claim_set = connection.execute(
            "SELECT * FROM approved_claim_sets WHERE event_id=? AND status='APPROVED' "
            "ORDER BY version_number DESC LIMIT 1", (event_id,)).fetchone()
        if not claim_set:
            reasons.append("No APPROVED claim set.")
        if event["research_status"] in ("REVIEW_REQUIRED",):
            reasons.append("Unresolved factual issues remain.")
        media = connection.execute(
            "SELECT COUNT(*) FROM media_candidates WHERE lifecycle_state IN ('APPROVED_FOR_USE','INGESTED')",
        ).fetchone()[0]
        if media <= 0:
            reasons.append("No rights-cleared media available.")
        existing_reel = connection.execute(
            "SELECT id FROM final_reel_assets WHERE event_id=? AND status='READY_FOR_REVIEW' ORDER BY created_at DESC LIMIT 1",
            (event_id,)).fetchone()
    return {"fast_path": not reasons, "reasons": reasons, "media_available": media,
            "existing_reel_id": existing_reel["id"] if existing_reel else None}


def advance(run_id, *, handler, connect, force_stage=None):
    """Run the next unfinished stage via injectable handlers; resumable and idempotent.

    `handler(stage, run)` performs the stage and returns a dict of checkpoint values, or raises
    PipelineError. Independent stages in a PARALLEL_GROUPS tuple run concurrently (bounded) while
    preserving the `done:<stage>` checkpoint contract and deterministic ordering. Paid stages are
    skipped when already checkpointed, so restarting never repeats provider work. Every stage is
    timed (stage_started_at/stage_completed_at/duration_ms) for System.
    """
    run = pipeline_run(run_id, connect=connect)
    if run["status"] in TERMINAL_STATES:
        return run
    start_index = STAGES.index(force_stage) if force_stage else max(0, STAGES.index(run["current_stage"]))
    index = start_index
    while index < len(STAGES):
        stage = STAGES[index]
        group = parallel_group_for(stage)
        # Only parallelize when the whole group is still pending and shares the same paid-state.
        pending_group = [item for item in group if STAGES.index(item) >= start_index]
        state = _checkpoint_state(connect, run_id)
        runnable = [item for item in pending_group
                    if not (item in PAID_STAGES and state.get(f"done:{item}"))]
        if len(runnable) > 1:
            _run_group_concurrently(run_id, runnable, handler=handler, connect=connect)
            index = STAGES.index(pending_group[-1]) + 1
            continue
        if not runnable:
            index += 1
            continue
        stage = runnable[0]
        with connect() as connection:
            _set_stage(connection, run_id, stage, "RUNNING")
            timing_id = start_timing(connection, run_id, stage,
                                     parallel_group=group[0] if len(group) > 1 else None)
        try:
            result = handler(stage, pipeline_run(run_id, connect=connect)) or {}
        except PipelineError as error:
            error.stage = stage
            with connect() as connection:
                finish_timing(connection, timing_id)
            return mark_needs_attention(run_id, error, connect=connect)
        with connect() as connection:
            finish_timing(connection, timing_id)
            _checkpoint(connection, run_id, **{f"done:{stage}": True, **result})
        if stage == "QA":
            _complete_ready_for_review(connection_factory=connect, run_id=run_id, result=result,
                                       event_id=run["event_id"])
            return pipeline_run(run_id, connect=connect)
        index = STAGES.index(stage) + 1
    return pipeline_run(run_id, connect=connect)


def _checkpoint_state(connect, run_id):
    with connect() as connection:
        row = connection.execute("SELECT checkpoint_json FROM reel_pipeline_runs WHERE id=?", (run_id,)).fetchone()
    return json.loads(row["checkpoint_json"] or "{}")


def _run_group_concurrently(run_id, stages, *, handler, connect):
    """Run independent stages concurrently; preserve deterministic checkpoint writes."""
    from concurrent.futures import ThreadPoolExecutor
    results = {}
    timings = {}
    with connect() as connection:
        for stage in stages:
            _set_stage(connection, run_id, stage, "RUNNING")
            timings[stage] = start_timing(connection, run_id, stage, parallel_group="|".join(stages))
    def run_stage(stage):
        return stage, handler(stage, pipeline_run(run_id, connect=connect)) or {}
    errors = {}
    with ThreadPoolExecutor(max_workers=max(1, min(4, len(stages)))) as pool:
        futures = {pool.submit(run_stage, stage): stage for stage in stages}
        for future, stage in futures.items():
            try:
                _, result = future.result()
                results[stage] = result
            except PipelineError as error:
                error.stage = stage
                errors[stage] = error
            except Exception as error:  # noqa: BLE001 - one lane must not sink the group
                wrapped = PipelineError(str(error))
                wrapped.stage = stage
                errors[stage] = wrapped
    with connect() as connection:
        for stage in stages:
            finish_timing(connection, timings[stage])
        for stage, result in results.items():
            _checkpoint(connection, run_id, **{f"done:{stage}": True, **result})
    if errors:
        first = next(iter(errors.values()))
        mark_needs_attention(run_id, first, connect=connect)
        raise first


def _complete_ready_for_review(*, connection_factory, run_id, result, event_id):
    with connection_factory() as connection:
        reel_id = result.get("reel_id") or _latest_reel_id(connection, event_id)
        connection.execute(
            "UPDATE reel_pipeline_runs SET status='READY_FOR_REVIEW',reel_id=?,current_stage='COMPLETE',"
            "completed_at=?,updated_at=? WHERE id=?", (reel_id, _now(), _now(), run_id),
        )
        _record(connection, run_id, "QA", "READY_FOR_REVIEW", reel_id=reel_id)


def ui_status(stage_or_status):
    """Normalize a stage or pipeline status to the six user-facing UI states."""
    return {
        # pipeline statuses
        "RUNNING": "PRODUCING", "WAITING": "PRODUCING", "READY_FOR_REVIEW": "READY_FOR_REVIEW",
        "NEEDS_ATTENTION": "NEEDS_ATTENTION", "HALTED": "NEEDS_ATTENTION", "COMPLETE": "APPROVED",
        # stages
        "ELIGIBILITY": "RESEARCHING", "SOURCE_ACQUISITION": "RESEARCHING", "VERIFICATION": "VERIFYING",
        "CONTENT_DECISION": "PRODUCING", "NARRATION": "PRODUCING", "MEDIA_DISCOVERY": "PRODUCING",
        "RIGHTS_CHECK": "PRODUCING", "MEDIA_SELECTION": "PRODUCING", "REEL_GENERATION": "PRODUCING",
        "QA": "READY_FOR_REVIEW",
    }.get(stage_or_status, "PRODUCING")
