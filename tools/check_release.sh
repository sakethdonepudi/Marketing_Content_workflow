#!/usr/bin/env bash
# Architecture 14 — staging -> main release gate.
# Runs every required check; exits non-zero on the first failure. Manual approval is separate.
set -euo pipefail
cd "$(dirname "$0")/.."

echo "== py_compile =="
python3 -m py_compile app.py youtube_publishing.py dashboard_auth.py news_feed.py \
  performance_learning.py persistence.py media_storage.py reel_standard.py

echo "== full unittest suite =="
python3 -m unittest -q

echo "== migration validation (fresh DB applies 001..latest) =="
TMPDB="$(mktemp -d)/release-gate.sqlite3"
REACHOUT_DB="$TMPDB" python3 -c "import app; app.init(); print('migrated to v'+str(max(r[0] for r in app.connect().execute('SELECT version FROM schema_migrations'))))"

echo "== secret scan (no env secret values in tracked files) =="
python3 - <<'PY'
import re, pathlib, subprocess, sys
env = pathlib.Path(".env")
if env.exists():
    vals = {m.group(1): m.group(2).strip() for m in re.finditer(r"^([A-Z_]+)=(.*)$", env.read_text(), re.M)}
    secretish = {k: v for k, v in vals.items()
                 if any(t in k for t in ("SECRET", "TOKEN", "KEY", "PASSWORD", "HASH")) and len(v) > 8}
    files = subprocess.run(["git", "ls-files"], capture_output=True, text=True).stdout.split()
    hits = [(f, k) for f in files for k, v in secretish.items()
            if v and pathlib.Path(f).read_text(errors="ignore").find(v) != -1]
    if hits:
        print("SECRET LEAK:", hits); sys.exit(1)
print("secret scan: PASS")
PY

echo "== production configuration isolation =="
python3 - <<'PY'
import os, sys, persistence
os.environ["STAGING_DATABASE_URL"] = "postgresql://s"
os.environ["PRODUCTION_DATABASE_URL"] = "postgresql://s"
if not persistence.staging_points_at_production():
    print("FAIL: staging/production isolation not detected"); sys.exit(1)
print("staging cannot target production: PASS")
PY

echo "== staging health (optional; skips if STAGING_HEALTH_URL unset) =="
if [ -n "${STAGING_HEALTH_URL:-}" ]; then
  curl -fsS "$STAGING_HEALTH_URL" >/dev/null && echo "staging health: PASS"
else
  echo "staging health: SKIPPED (set STAGING_HEALTH_URL after staging is deployed)"
fi

echo
echo "ALL GATES PASSED. Manual approval is still required before merging staging -> main."
