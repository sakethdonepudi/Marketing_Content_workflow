"""Architecture 14 — content-level performance learning.

Interprets only AGGREGATE content-performance signals (views, engaged views, stayed-to-watch,
average view duration, average percentage viewed, likes/comments/shares) linked to content
features (hook variant, duration, title, topic, language). It never builds political demographic
profiles, infers voter preferences, or optimizes persuasion.
"""

from datetime import datetime, timezone


def _int(value):
    return int(value) if str(value or "").isdigit() or isinstance(value, int) else None


def _float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def record_metrics(*, connect, video_id, metrics, reel_id=None, job_id=None, event_id=None,
                   reel_version=None, copy_version=None, hook_variant=None, topic=None,
                   language=None, title=None, duration_seconds=None, now=None):
    """Store an aggregate performance snapshot linked to content features (no fabrication)."""
    import youtube_publishing as yp
    enriched = {
        "views": _int(metrics.get("views")), "likes": _int(metrics.get("likes")),
        "comments": _int(metrics.get("comments")), "shares": _int(metrics.get("shares")),
        "watch_time_seconds": _float(metrics.get("watch_time_seconds")),
        "average_view_duration_seconds": _float(metrics.get("average_view_duration_seconds")),
        "average_percentage_viewed": _float(metrics.get("average_percentage_viewed")),
        "subscribers_gained": _int(metrics.get("subscribers_gained")),
        "source": metrics.get("source", "BASIC"),
    }
    record = yp.record_snapshot(connect=connect, video_id=video_id, checkpoint="latest", metrics=enriched,
                                reel_id=reel_id, job_id=job_id, event_id=event_id, reel_version=reel_version,
                                youtube_copy_version=copy_version, title=title,
                                processing_state=metrics.get("processing_state"), now=now)
    timestamp = now() if now else datetime.now(timezone.utc).isoformat()
    with connect() as connection:
        connection.execute(
            "UPDATE youtube_performance_snapshots SET engaged_views=?,stayed_to_watch_pct=?,hook_variant=?,"
            "topic=?,language=? WHERE id=?",
            (_int(metrics.get("engaged_views")), _float(metrics.get("stayed_to_watch_pct")),
             hook_variant, topic, language, record["id"]))
    return _snapshot(connect=connect, video_id=video_id)


def _snapshot(*, connect, video_id):
    with connect() as connection:
        row = connection.execute(
            "SELECT * FROM youtube_performance_snapshots WHERE video_id=? ORDER BY recorded_at LIMIT 1",
            (video_id,)).fetchone()
    return dict(row) if row else None


def observations(*, connect):
    """Aggregate content observations (e.g. hook hold vs mid-video retention). Never profiling."""
    with connect() as connection:
        rows = [dict(r) for r in connection.execute(
            "SELECT stayed_to_watch_pct,average_percentage_viewed,views,engaged_views,likes,comments,shares "
            "FROM youtube_performance_snapshots")]
    notes = []
    holds = [r["stayed_to_watch_pct"] for r in rows if r["stayed_to_watch_pct"] is not None]
    viewed = [r["average_percentage_viewed"] for r in rows if r["average_percentage_viewed"] is not None]
    if holds and viewed:
        avg_hold = sum(holds) / len(holds)
        avg_viewed = sum(viewed) / len(viewed)
        # stayed_to_watch (early retention) vs average percentage viewed (mid/late retention).
        if avg_hold < avg_viewed:
            notes.append("Opening retention is weaker than mid-video retention.")
        else:
            notes.append("Opening retention holds at or above mid-video retention.")
    return {"samples": len(rows), "observations": notes}


def content_performance(*, connect, limit=10):
    """CONTENT PERFORMANCE section: recent Shorts with aggregate metrics + observations."""
    with connect() as connection:
        rows = [dict(r) for r in connection.execute(
            "SELECT s.video_id,s.title,s.views,s.engaged_views,s.stayed_to_watch_pct,"
            "s.average_view_duration_seconds,s.average_percentage_viewed,s.likes,s.comments,s.shares,"
            "s.hook_variant,s.topic,s.language,s.recorded_at "
            "FROM youtube_performance_snapshots s ORDER BY s.recorded_at DESC LIMIT ?", (limit,))]
    shorts = []
    for row in rows:
        engagement = None
        if row["views"]:
            engagement = round(((row["likes"] or 0) + (row["comments"] or 0) + (row["shares"] or 0))
                               / row["views"] * 100, 2)
        shorts.append({
            "video_id": row["video_id"], "title": row["title"], "views": row["views"],
            "engaged_views": row["engaged_views"], "stayed_to_watch_pct": row["stayed_to_watch_pct"],
            "average_view_duration_seconds": row["average_view_duration_seconds"],
            "average_percentage_viewed": row["average_percentage_viewed"],
            "likes": row["likes"], "comments": row["comments"], "shares": row["shares"],
            "engagement_pct": engagement, "hook_variant": row["hook_variant"], "topic": row["topic"],
            "language": row["language"], "recorded_at": row["recorded_at"],
        })
    return {"shorts": shorts, "summary": observations(connect=connect)}
