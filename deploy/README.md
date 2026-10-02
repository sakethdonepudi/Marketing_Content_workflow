# ReachOut OS - production deployment runbook

The app is a single Python process (stdlib only). It must NOT be exposed directly to the
internet: run it bound to 127.0.0.1 behind a reverse proxy that terminates TLS.

## 1. Host + files
- Ubuntu/Debian VPS (1 vCPU / 2 GB is enough), unprivileged user `reachout`.
- /srv/reachout = repo checkout; /var/lib/reachout = persisted DB + media;
  /var/log/reachout = rotated logs.
- Set in /srv/reachout/.env (gitignored): DASHBOARD_USERNAME, DASHBOARD_PASSWORD_HASH
  (scrypt), SESSION_SECRET, DASHBOARD_COOKIE_SECURE=1, YOUTUBE_* credentials,
  YOUTUBE_AUTOMATED_PRIVACY=PRIVATE, AUTO_REEL_PIPELINE, LIVE_DISCOVERY_ENABLED.

## 1b. Create user + directories (required before the service starts)
    sudo useradd --system --home /srv/reachout --shell /usr/sbin/nologin reachout || true
    sudo mkdir -p /srv/reachout /var/lib/reachout/generated_media /var/log/reachout
    sudo chown -R reachout:reachout /srv/reachout /var/lib/reachout /var/log/reachout

Without these directories/system user, systemd fails to start the unit.

## 2. Service
    sudo cp deploy/reachout.service /etc/systemd/system/reachout.service
    sudo systemctl daemon-reload && sudo systemctl enable --now reachout
    sudo systemctl status reachout
    curl -fsS http://127.0.0.1:8000/api/health

## 3. Domain + HTTPS
- Add the domain to deploy/Caddyfile, point DNS at the host, then run Caddy
  (systemctl enable --now caddy). DNS + a public IP are required - this cannot be
  provisioned from inside the app.
- HTTP redirects to HTTPS automatically; /login is the authenticated entry point.
- Caddy writes /var/log/reachout/caddy.log; ensure the Caddy user can write there, or
  switch the `log` block to `output stderr` to use the journal instead.

## 3b. Production OAuth callback (important)
Once the site is public, the OAuth callback must use the public HTTPS URL. Set in the
production .env:
    YOUTUBE_REDIRECT_URI=https://<domain>/oauth/youtube/callback
Add that EXACT URI to the Google OAuth client's Authorized redirect URIs. Keep the
existing http://127.0.0.1:8000/oauth/youtube/callback registered too, for local dev.

## 4. Pre-expose safety checklist
- python3 -m unittest -q (full suite green)
- git check-ignore .env returns .env
- secret scan: no env secret values appear in tracked files
- DB/media live outside the repo and are never served as static files
- DASHBOARD_COOKIE_SECURE=1; debug logging off

## 5. YouTube
All automated uploads remain privacyStatus=PRIVATE (YOUTUBE_AUTOMATED_PRIVACY=PRIVATE).
Public uploads require the Google Cloud project to pass YouTube's API audit
(YOUTUBE_PROJECT_AUDIT_STATUS=PUBLIC_VERIFIED) AND an explicit operator change - neither
is enabled by default.
