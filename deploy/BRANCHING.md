# Architecture 14 — staging / main operating model

Two permanent branches, two environments. All work lands on `staging` first; `main` is production.

- `staging` — test environment (`https://staging.<domain>`). New work lands here, CI + migrations
  run, and the UI is clearly marked **STAGING**.
- `main` — production (`https://<domain>`). Protected: no direct development commits; it receives
  merges only from `staging`, only after the release gate passes and a human approves.

## Release gate (staging -> main)

    tools/check_release.sh

Runs: py_compile, the full unittest suite, fresh-DB migration validation, secret scan,
staging/production database isolation, and (when `STAGING_HEALTH_URL` is set) the staging
health check. Passing the gate is necessary but NOT sufficient — merging is a deliberate,
manual decision.

## Databases (central, not per-machine)

- Production: `PRODUCTION_DATABASE_URL` (PostgreSQL, TLS). All users/devices share it.
- Staging: `STAGING_DATABASE_URL` (PostgreSQL), separate from production. Staging must never
  point at the production DB (`persistence.assert_environment_isolation()` refuses it).
- Development/tests: SQLite (default) — no external service needed.

`app.connect()` routes to the configured backend via `persistence.open_connection()`. Business
code is unchanged.

## Storage (central)

`STORAGE_BACKEND=local|s3`. Production/staging use `s3` (S3/R2-compatible) so media lives in
central object storage; the DB holds references only. Development/tests use `local`.

## Migrations

Additive and ordered (`migrations/NNN_*.sql`). Production rule: back up, validate on staging,
run the backward-compatible migration, deploy the app, health-check, and only later remove
deprecated columns. Never run destructive SQL in the same release, and never roll data back
automatically because the app rolled back.

## One-time data migration

    PRODUCTION_DATABASE_URL=postgresql://... python3 tools/migrate_sqlite_to_postgres.py reachout.sqlite3

Preserves IDs/timestamps/FKs; copies non-secret OAuth state only. The local source DB is kept.

## Failure behaviour

- Staging fails -> do not merge to `main`.
- Production health check fails -> roll back the application release (not the data).
- Migration fails -> stop before the new app version becomes active.
