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
TERMINAL_STATES = ("READY_FOR_REVIEW", "HALTED", "COMPLETE")
STAGES = ("ELIGIBILITY", "SOURCE_ACQUISITION", "VERIFICATION", "CONTENT_DECISION", "NARRATION",
          "MEDIA_DISCOVERY", "RIGHTS_CHECK", "MEDIA_SELECTION", "REEL_GENERATION", "QA")
# Stages that cost money; never re-run if their checkpoint exists.
PAID_STAGES = ("CONTENT_DECISION", "NARRATION", "MEDIA_DISCOVERY", "REEL_GENERATION")


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


def advance(run_id, *, handler, connect, force_stage=None):
    """Run the next unfinished stage via injectable handlers; resumable and idempotent.

    `handler(stage, run)` performs the stage and returns a dict of checkpoint values, or raises
    PipelineError. Paid stages are skipped when already checkpointed, so restarting never repeats
    provider work.
    """
    run = pipeline_run(run_id, connect=connect)
    if run["status"] in TERMINAL_STATES:
        return run
    start_index = STAGES.index(force_stage) if force_stage else max(0, STAGES.index(run["current_stage"]))
    for stage in STAGES[start_index:]:
        with connect() as connection:
            state = json.loads(
                connection.execute("SELECT checkpoint_json FROM reel_pipeline_runs WHERE id=?", (run_id,)).fetchone()[0]
                or "{}"
            )
        if stage in PAID_STAGES and state.get(f"done:{stage}"):
            run = pipeline_run(run_id, connect=connect)
            continue
        with connect() as connection:
            _set_stage(connection, run_id, stage, "RUNNING")
        try:
            result = handler(stage, pipeline_run(run_id, connect=connect)) or {}
        except PipelineError as error:
            error.stage = stage
            return mark_needs_attention(run_id, error, connect=connect)
        with connect() as connection:
            _checkpoint(connection, run_id, **{f"done:{stage}": True, **result})
        if stage == "QA":
            with connect() as connection:
                reel_id = result.get("reel_id") or _latest_reel_id(connection, run["event_id"])
                connection.execute(
                    "UPDATE reel_pipeline_runs SET status='READY_FOR_REVIEW',reel_id=?,current_stage='COMPLETE',"
                    "updated_at=? WHERE id=?", (reel_id, _now(), run_id),
                )
                _record(connection, run_id, "QA", "READY_FOR_REVIEW", reel_id=reel_id)
    return pipeline_run(run_id, connect=connect)


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
