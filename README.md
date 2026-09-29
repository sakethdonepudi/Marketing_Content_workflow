# ReachOut OS — Architecture 01

Minimal CEO dashboard and event state foundation. This is a local development slice; it does not connect to Instagram, Grok, or live sources.

## Run

Requires Python 3.10+; no external packages.

```bash
python3 app.py
```

Open http://127.0.0.1:8000. Click **Add sample event** to inspect the live desk. Events persist in `reachout.sqlite3` (ignored by Git). The dashboard polls the API every 15 seconds.

## Test

```bash
python3 -m unittest discover -s tests
```

## API

- `GET /api/health`
- `GET /api/overview`
- `POST /api/events` — `{ "title": "...", "source": "...", "source_url": "...", "priority": "NORMAL" }`
- `POST /api/events/{id}/transition` — `{ "state": "VERIFYING" }`

The API is intentionally bound to localhost; it has no authentication. Add authentication and a production datastore before deploying. `metrics` is an empty placeholder; the dashboard displays dashes rather than fabricated Instagram numbers.

## Next components

1. PostgreSQL migrations and authenticated admin API.
2. Source ingestion and event clustering, with source provenance.
3. Grok research and claim ledger.
4. Story, scripts, asset rights, rendering, and QA.
5. Meta authorization, publishing, and analytics collection.

All external services should be adapters around persistent events. The server's SQLite schema is a small local prototype, not the final distributed architecture.

## Conductor

Initialize this folder as a Git repository and use Conductor's **Open project** for a local repo, or push it and use **Open GitHub project**. Conductor workspaces use Git-backed isolated branches. Run script: `python3 app.py`; setup: no dependencies. The `PORT` environment variable is honored.
