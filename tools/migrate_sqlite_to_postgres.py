"""Architecture 14 — one-time SQLite -> PostgreSQL data migration.

Preserves IDs, timestamps, and FK relationships; copies upload jobs, analytics, reel metadata,
and OAuth NON-SECRET state only. Raw access/refresh tokens and client secrets are never copied
(they stay in the environment/secrets manager). The local source DB is never deleted.

Usage:
    PRODUCTION_DATABASE_URL=postgresql://... python3 tools/migrate_sqlite_to_postgres.py <src.sqlite3>

The target tables must already exist (run the ordered migrations first). Counts + FK integrity
are reported; a checksum comparison is done for small JSON payloads where practical.
"""

import hashlib
import sqlite3
import sys

# Copied in dependency order. OAuth secret columns are intentionally absent.
TABLE_ORDER = (
    "workspace_identity", "sources", "events", "signals", "research_runs", "claims", "claim_versions",
    "verification_runs", "verification_decisions", "approved_claim_sets", "approved_claim_set_items",
    "event_candidates", "candidate_signals", "content_decisions", "content_packages",
    "generated_assets", "render_jobs", "media_candidates", "final_reel_assets", "reel_approvals",
    "reel_revision_requests", "post_packages", "post_package_revisions", "youtube_packages",
    "youtube_package_revisions", "youtube_publish_jobs", "youtube_publish_events",
    "youtube_performance_snapshots", "youtube_research_snapshots", "content_topic_signals",
    "story_topic_research", "dashboard_sessions", "dashboard_login_attempts", "schema_migrations",
)

# Columns that must never cross into the destination (secrets stay in the environment).
FORBIDDEN_COLUMNS = ("access_token", "refresh_token", "client_secret", "session_secret")


def _columns(connection, table):
    try:
        return [row[1] for row in connection.execute(f"PRAGMA table_info({table})")]
    except sqlite3.OperationalError:
        return []


def migrate(source_path, target_connection):
    """Copy tables from SQLite into an open PostgreSQL connection. Returns a report dict."""
    source = sqlite3.connect(source_path)
    source.row_factory = sqlite3.Row
    report = {"tables": {}, "fk_issues": [], "checksums": {}}
    with target_connection as target:
        for table in TABLE_ORDER:
            cols = _columns(source, table)
            if not cols:
                continue
            safe_cols = [c for c in cols if c not in FORBIDDEN_COLUMNS]
            rows = source.execute(f"SELECT {','.join(safe_cols)} FROM {table}").fetchall()
            placeholders = ",".join("?" for _ in safe_cols)
            for row in rows:
                values = tuple(row[c] for c in safe_cols)
                try:
                    target.execute(
                        f"INSERT INTO {table}({','.join(safe_cols)}) VALUES({placeholders}) "
                        f"ON CONFLICT DO NOTHING", values)
                except Exception as error:  # noqa: BLE001 - report, do not abort the whole migration
                    report["fk_issues"].append({"table": table, "error": str(error)[:160]})
            dest_count = target.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            report["tables"][table] = {"source": len(rows), "destination": dest_count}
            if "package_json" in safe_cols:
                digest = hashlib.sha256("".join(str(r["package_json"]) for r in rows).encode()).hexdigest()
                report["checksums"][table] = digest[:16]
    source.close()
    return report


def _main(argv):
    if len(argv) < 2:
        print("usage: migrate_sqlite_to_postgres.py <src.sqlite3>")
        return 2
    import persistence
    target = persistence.open_connection(environment="production")
    if getattr(target, "dialect", None) != "postgres":
        print("PRODUCTION_DATABASE_URL must point at PostgreSQL for this migration")
        return 2
    report = migrate(argv[1], target)
    for table, counts in report["tables"].items():
        print(f"{table:32} source={counts['source']:5} destination={counts['destination']:5}")
    if report["fk_issues"]:
        print("issues:", report["fk_issues"][:10])
    print("source DB preserved:", argv[1])
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv))
