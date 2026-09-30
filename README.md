# ReachOut OS — Architecture 04

Local event intelligence desk for **N. Chandrababu Naidu, Chief Minister of Andhra Pradesh, India**, with factual, source-attributed ingestion, research, and claim-specific corroboration. The explicit workspace key is `n-chandrababu-naidu-andhra-pradesh`; the ambiguous abbreviation “CBN” is not used as workspace identity. Architecture 04 ends at a versioned approved claim set ready for the Story Architect. It does not generate stories, render media, publish posts, or connect to Instagram.

## Run

Requires Python 3.10+ and no external packages.

```bash
cp config/sources.example.json config/sources.json
python3 app.py
```

Open <http://127.0.0.1:8000>. The server applies pending SQLite migrations and idempotent data corrections at startup. Click **Ingest URL** to fetch a dated, individual public report for a manual test. Private and loopback addresses, non-HTTP URLs, redirects to private hosts, and responses over 2 MB are rejected.

Open an event from Live intelligence to enter the Event Room. Research defaults to the deterministic test adapter and is visibly labeled `TEST DATA`. For a live, paid Grok request, copy `.env.example` to `.env`, set `XAI_API_KEY`, optionally set `XAI_MODEL`, explicitly select **Grok · live paid call**, then click **Research event**. `.env` and SQLite databases are ignored by Git; credentials are never included in API responses or structured logs.

After a completed research run, **Find corroboration** starts a separate claim-specific verification job. Live research selects live Grok verification; deterministic research selects a no-network fixture and remains `TEST DATA`. The search is explicit and bounded with the provider-supported `max_turns` control and disabled parallel tool calls. The application records actual search/open counts when returned, while clearly noting that exact tool and result counts are not provider-guaranteed. Search snippets remain leads only: a URL must pass safe fetch, registered-source identity/workspace validation, page parsing, and claim-specific content checks before it becomes evidence.

To poll enabled configured sources once:

```bash
curl -X POST -H 'Content-Type: application/json' -d '{}' http://127.0.0.1:8000/api/ingest-configured
```

Scheduling is intentionally outside this local milestone. A scheduler can call that endpoint or `app.ingest_configured_sources()` later.

## Source configuration

Workspace identity and allowed source hosts live in `config/workspace.json`. Copy `config/sources.example.json` to `config/sources.json`, or set `REACHOUT_SOURCES_CONFIG` to another JSON file. Each entry supports:

- `type: "rss"`: every valid RSS/Atom item is evaluated as an individual item.
- `type: "webpage"`: the registered page is monitored and retained as reference material. To monitor a listing page, set `metadata.link_pattern` to a regular expression; matching individual report links are fetched and evaluated separately. `metadata.max_items` defaults to 20.
- `official`, `name`, and arbitrary `metadata`: snapshotted onto every signal for durable attribution.
- `metadata.content_role`: required registration role: `homepage`, `profile`, `listing`, `reference`, or `item_stream`. A homepage, profile, or listing is never itself a breaking event.
- `source_class`: `official_primary` or `independent_reporting`. The official host allowlist applies only to primary sources; verified independent publishers use their configured HTTPS host.
- `identity`: verification status, method, evidence URL, and a human-readable verification note. A self-asserted “official” label is rejected without government-domain or cross-source evidence.
- `rate_limit_seconds` and `poll_interval_seconds`: per-source request spacing and poll throttling.
- `metadata.link_pattern`, `pagination_pattern`, `max_pages`, and `max_items`: bounded listing discovery and pagination. The last leading item is retained as an incremental cursor.
- `workspace_match`: required leader, jurisdiction, and topic declarations. Registration fails unless the leader resolves to N. Chandrababu Naidu, the jurisdiction is Andhra Pradesh, all topics are approved for the workspace, and the exact source host is allowlisted.
- `enabled`: permits staged configuration without polling the source.

The examples use the leader's official website, an Andhra Pradesh government Chief Minister profile hosted by the National Informatics Centre, the NIC Andhra Pradesh news page, and a verified independent-reporting topic page from The New Indian Express. No RSS URL is inferred. Source availability and layouts can change, so configuration must be revalidated before production monitoring.

Manual URL ingestion is subject to the same host allowlist and additionally checks the fetched page for the workspace leader, Andhra Pradesh jurisdiction, and an approved topic marker.

## Signal and event model

Migration `002_source_ingestion.sql` adds:

- `sources`: mutable polling configuration.
- `signals`: immutable observations containing original and canonical URLs, publication and detection times, extracted title/text, source identity and metadata, content hash, an optional event link, and classification/clustering audit fields.
- event detection timestamps and indexes.

Database triggers reject ordinary signal updates and deletes. Canonical URLs are unique; host casing, default ports, fragments, trailing slashes, and common tracking parameters do not create duplicates. Reference signals remain source-attributed observations without an event link.

Event discovery only accepts a dated, individual news item, announcement, speech, or post. It requires a defensible publication or event timestamp and never substitutes crawl/detection time. Items older than `REACHOUT_EVENT_MAX_AGE_DAYS` (30 by default), undated pages, profiles, homepages, and listings remain reference material. Eligible signals are clustered deterministically within 72 hours using token/title similarity, N. Chandrababu Naidu alias normalization, entity overlap, and event-time proximity. The score and method are stored on the signal.

Migration `003_workspace_identity_and_corrections.sql` adds explicit workspace keys plus `data_corrections` and `correction_audit`. The idempotent correction archives full JSON snapshots before removing Nigeria-source records or ambiguous legacy sample events. Unrelated signals and events are retained; mixed events are repointed to their earliest surviving signal.

Migration `004_reference_material_and_event_time.sql` separates source monitoring from event discovery, makes signal-to-event linkage optional for reference material, and adds an explicit event time. Its idempotent correction reclassifies the three legacy homepage/profile/listing observations as references, archives before/after snapshots in the correction audit, removes only their false events, and preserves all valid source registrations.

Migration `005_individual_item_discovery.sql` adds source classes, identity evidence, per-source polling state, incremental cursors, author attribution, time provenance, and separate `reference`, `review`, `rejected`, and `event` signal dispositions. HTML adapters prefer canonical URLs and Schema.org article metadata; missing or implausible dates are held for review and stale items are rejected from event creation.

## Research and claim ledger

Migration `006_research_claim_ledger.sql` adds evidence snapshots, research runs and status history, claims, claim evidence, and claim status history. Every run records provider/model, `test` or `live` mode, progress, retry attempts, configurable search/token limits, usage when returned, and cost as either known or unknown. The evidence-version hash enables cached runs without another provider call, while a partial unique index prevents two queued/running jobs for the same event.

`research.py` defines the replaceable provider interface, a no-network deterministic adapter, and the Grok adapter. The live adapter follows xAI's current [Responses API](https://docs.x.ai/developers/rest-api-reference/inference/responses), [Structured Outputs](https://docs.x.ai/developers/model-capabilities/text/structured-outputs), and [web search](https://docs.x.ai/developers/tools/web-search) contracts. Requests use bearer authentication, `store: false`, low reasoning effort by default, structured JSON Schema output, bounded output tokens, distinct connection/response timeouts, and bounded retries. Retrieved and stored source text is explicitly treated as untrusted evidence rather than instructions.

Live event research is evidence-only by default. Web search is a separate unchecked Event Room option; when explicitly enabled it uses `max_turns`, disables parallel tool calls, filters to linked evidence domains, and is capped by `RESEARCH_SEARCH_LIMIT`. This matters because server-side search may consume substantially more reasoning and source-processing tokens than the final text limit suggests. Test-adapter claims are retained for inspection but are categorically excluded from production event verification; the manual verification gate only accepts a passing completed `live` run.

Provider output cannot verify a claim by itself. Local validation requires every cited URL to match a linked snapshot and every supporting excerpt to occur in its stored text. Numbers, units, names, dates, and quotations must be explicit. Conflicting evidence produces `CONFLICTED`; missing or fabricated evidence produces `INSUFFICIENT_EVIDENCE`. Identical syndicated text is one evidence family. Official statements support what was announced, approved, allocated, promised, or said—not completion unless the evidence explicitly establishes completion.

Claim statuses are `UNVERIFIED`, `SUPPORTED`, `CONFLICTED`, and `INSUFFICIENT_EVIDENCE`. Research completion is stored separately from event verification. Every required claim must have explicit support from an inspected official primary source or from two genuinely independent reporting families, with no claim-specific conflict. Until this policy passes, the event stays in review and the Event Room explains what is missing. Manual transition to `VERIFIED` is rejected unless a completed live verification run produced an `APPROVED` claim set under the same policy.

## Verification and approved claim sets

Migration `008_verification_and_corroboration.sql` adds independent verification runs and history, discovery leads, immutable claim versions, verification evidence snapshots, syndicated evidence families, claim-by-claim decisions, and versioned approved claim sets. Each required claim is evaluated against inspected source text. An official primary source can directly support what that authority announced or said; otherwise, two genuinely independent evidence families are required. Copied reports count once. Topic overlap does not corroborate an exact number, date, quotation, or action. Optional unsupported claims are explicitly excluded rather than silently approved.

Quotations require verbatim source text; paraphrases retain publisher attribution. Relative dates are flagged and never silently converted from publication time. Any claim or evidence change alters its version hash and prevents reuse of cached decisions. Later story work must reference the exact approved claim-set and claim-version IDs.

The xAI Responses API reports raw `cost_in_usd_ticks`. Per the official response schema, `100,000,000` ticks equal one US cent, so ReachOut converts `10,000,000,000` ticks to one US dollar and retains the raw ticks. Missing usage—including timed-out requests—is `unknown`, never zero. Deterministic fixture runs report literal zero usage because no provider call occurs.

## Structured errors

Application and ingestion errors are emitted to stderr as one JSON object per line. Records include UTC time, level, event name, message, request/source context, and never include fetched page bodies. Configure verbosity with `LOG_LEVEL`.

Example:

```json
{"event":"source_ingestion_failed","level":"ERROR","message":"URL hostname could not be resolved","source_id":"ap-example","source_url":"https://example.invalid/feed","time":"2026-09-29T09:00:00+00:00"}
```

## Test

```bash
python3 -m unittest -v
```

Tests cover fresh and upgrade migrations, listing-to-item extraction, pagination, incremental cursors, duplicate URLs/jobs, stale items, source classification, database immutability, event clustering, audited corrections, rejection of `cbn.gov.ng`, CM profile/homepage safety, fabricated citations, missing excerpts, conflicting numbers, publication/event-time separation, provider failures, missing credentials, syndicated duplicates, unrelated mentions, conflicting primary evidence, paraphrases mislabeled as quotations, evidence-version invalidation, duplicate verification jobs, test-data exclusion, and verification transitions.

## API

- `GET /api/health`
- `GET /api/overview`
- `GET /api/events/{id}` — Event Room overview, sources, claims, verification matrix, and history
- `GET /api/events/{id}/signals`
- `GET /api/research/{run_id}` — background progress and usage
- `GET /api/verification/{run_id}` — verification progress, limits, usage, and decision summary
- `POST /api/ingest-url` — `{ "url": "https://public.example/report" }`
- `POST /api/ingest-configured` — polls due sources; `{ "force": true }` bypasses the poll interval for testing
- `POST /api/events` — disabled; events must originate from qualified source signals
- `POST /api/events/{id}/research` — `{ "provider": "test" }` or explicit `{ "provider": "grok" }`
- `POST /api/research/{run_id}/verify` — explicit `{ "provider": "test" }` or paid `{ "provider": "grok" }`
- `POST /api/events/{id}/transition` — `{ "state": "VERIFYING" }`

The API remains bound to localhost and has no authentication. Add authentication, request authorization, a production datastore, source-specific extraction adapters, and an external scheduler before deployment.
