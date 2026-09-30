# ReachOut OS — Architecture 06C

Local event intelligence desk for **N. Chandrababu Naidu, Chief Minister of Andhra Pradesh, India**, with factual ingestion, claim-specific verification, evidence-gated content decisions, approval-ready structured content packages, explicit media rendering, and separate media QA. The explicit workspace key is `n-chandrababu-naidu-andhra-pradesh`; the ambiguous abbreviation “CBN” is not used as workspace identity. Architecture 06C stops at generated assets in `READY_FOR_REVIEW`, with human review still required. It does not publish posts, connect to social platforms, auto-approve assets, or perform demographic or political targeting.

## Run

Requires Python 3.10+ and no external packages.

```bash
cp config/sources.example.json config/sources.json
python3 app.py
```

Open <http://127.0.0.1:8000>. The server applies pending SQLite migrations and idempotent data corrections at startup. Click **Ingest URL** to fetch a dated, individual public report for a manual test. Private and loopback addresses, non-HTTP URLs, redirects to private hosts, and responses over 2 MB are rejected.

Open an event from Live intelligence to enter the Event Room. Research defaults to the deterministic test adapter and is visibly labeled `TEST DATA`. For a live, paid Grok request, copy `.env.example` to `.env`, set `XAI_API_KEY`, optionally set `XAI_MODEL`, explicitly select **Grok · live paid call**, then click **Research event**. `.env` and SQLite databases are ignored by Git; credentials are never included in API responses or structured logs.

After a completed research run, **Find corroboration** starts a separate claim-specific verification job. Live research selects live Grok verification; deterministic research selects a no-network fixture and remains `TEST DATA`. The search is explicit and bounded with the provider-supported `max_turns` control and disabled parallel tool calls. The application records actual search/open counts when returned, while clearly noting that exact tool and result counts are not provider-guaranteed. Search snippets remain leads only: a URL must pass safe fetch, registered-source identity/workspace validation, page parsing, and claim-specific content checks before it becomes evidence.

The Event Room’s **Content decision** action first runs a deterministic eligibility gate. Ineligible evidence produces a persisted `HOLD` without calling a model. Eligible inputs can use the deterministic preview adapter or an explicitly selected live Grok call. Deterministic and TEST_ONLY decisions are visibly labeled and never executable.

The **Production** tab is a separate explicit action. It is enabled only for the current executable `CREATE` decision with a current production-approved claim set and format-compatible rights-cleared media. Set `ANTHROPIC_API_KEY` to enable the live adapter; the model defaults to `claude-opus-4-5-20251101` and is configurable through `ANTHROPIC_MODEL`. The provider receives only approved claim versions and tightly scoped creative context. One structured call returns an editorial package, which must pass the local claim/version/media validator before an immutable package reaches `READY_FOR_APPROVAL`. There is no Publish action.

**Generate media** is a second, explicit action after a package reaches `READY_FOR_APPROVAL`. Provider selection is configured independently for image, video, and audio with `RENDERER_PROVIDER_IMAGE`, `RENDERER_PROVIDER_VIDEO`, and `RENDERER_PROVIDER_AUDIO`. This repository currently includes the generic renderer boundary and a deterministic IMAGE fixture for tests/demonstrations; it does not claim a live renderer integration. With provider settings blank, the UI clearly reports that live rendering is unavailable and creates no job. Setting `fixture` is appropriate only in an isolated test or demonstration environment.

Architecture 06C adds a provider-neutral asynchronous lifecycle behind the same renderer contract: submit, bounded poll, normalized completion/failure, temporary-output download, and controlled storage. No supported live renderer credentials are present in this environment, so live rendering reports `LIVE_RENDERER_NOT_CONFIGURED`; fixture output is never substituted silently. Future live adapters must use `LIVE_RENDERER_API_KEY` or a provider-specific secret, and must normalize their responses without exposing provider-specific payloads to business logic.

Every render revalidates the exact package, production job, executable decision, approved claim versions, evidence snapshots, and reference-media rights before the provider call and again after it returns. Output bytes are copied through the storage abstraction (local development storage defaults to `.context/generated_media`), checksummed, deterministically inspected, and linked to an immutable request snapshot. A passing artifact stops at `READY_FOR_REVIEW`; human editorial review remains mandatory. Repeated clicks reuse the same logical result, while explicit regeneration creates a new render job and immutable asset version without overwriting prior history.

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

## Content CEO

Migration `009_content_ceo.sql` adds a media inventory, publishing history, Content CEO runs and audit history, and immutable structured decisions. A decision records `CREATE`, `HOLD`, `MONITOR`, `SKIP`, or `HUMAN_REVIEW`; the recommended format; language; duration; priority; factual rationale; exact claim-set/evidence versions; blockers; timestamp; policy version; and whether it is executable.

Production eligibility requires the latest claim set to be `APPROVED`, backed by a completed live verification run, attached to a currently `VERIFIED` event, non-empty, and unchanged against current evidence and claim-version hashes. This gate executes before provider selection. A stale or missing approval produces `HOLD` with `provider_called=0`. TEST_ONLY claim sets may exercise the full decision path, but their decisions carry `test_only=1`, `executable=0`, and never transition the event to production work.

`content_ceo.py` provides deterministic and Grok adapters. Providers see only event metadata, approved claim versions, media inventory, freshness, and recent publishing records. Local enforcement prevents `CREATE` when the selected format lacks rights-verified media and converts recent duplicates to `SKIP`. Input hashes include evidence, approved claims, media state, publishing history, policy version, and decision day; unchanged inputs reuse a cached decision.

## Content production orchestrator

Migration `010_content_production_orchestrator.sql` adds the explicit production state machine, immutable input snapshots, job history, claim/evidence/media links, provider drafts, and immutable versioned content packages. Entry is rejected before job creation for `HOLD`, `MONITOR`, `SKIP`, `HUMAN_REVIEW`, non-executable, TEST_ONLY, stale, revoked, superseded, unverified, or media-ineligible decisions. Duplicate active jobs are prevented by a partial unique index; completed inputs are idempotent, and regeneration requires an explicit flag and produces a new job/package version.

`content_production.py` defines the generic provider interface, live Anthropic adapter, and controlled fixture adapter. The live adapter sends one `POST /v1/messages` request with no tools and uses Anthropic structured outputs. The response is not trusted merely because it matches the schema: a deterministic validator checks exact approved claim-version references, unknown references, numerical values, quotations, certainty upgrades, format/language/duration, revoked evidence, current media, and mid-run version changes. A failed check creates no package and routes the job to `HUMAN_REVIEW` or `BLOCKED`.

Usage fields are persisted exactly when Anthropic returns them. Cost remains `unknown` unless both optional operator-maintained per-million token rates and a cost-policy version are configured; the application never guesses a price or converts missing usage to zero.

## Media rendering orchestrator

Migration `011_media_rendering.sql` adds the RenderJob state machine, immutable prompt snapshots, append-only attempts and status history, generated-asset records, and render-to-asset lineage. A RenderJob moves through `QUEUED`, `PREPARING`, `RENDERING`, and `VALIDATING` before reaching `READY_FOR_REVIEW`; fail-closed terminal outcomes include `HUMAN_REVIEW`, `BLOCKED`, `FAILED`, and `CANCELLED`. Direct arbitrary transitions are rejected.

`media_rendering.py` defines the replaceable `MediaRenderer` interface and deterministic fixture renderer. `media_storage.py` defines the replaceable storage contract and content-addressed local implementation. Stored asset metadata includes the originating RenderJob, exact package/version, provider/model, provider IDs when available, dimensions or duration, MIME type, SHA-256 checksum, prompt/config versions, rights-cleared references, provenance, provider-returned text metadata, validation result, and immutable version. Identical binaries are linked rather than duplicated.

Validation checks response presence, non-zero bytes, MIME/media consistency, persisted storage, checksum, PNG structure and dimensions, output count, minimum dimensions, aspect ratio, exact lineage, current rights, and post-render invalidation. No OCR or semantic visual QA is claimed: provider-returned textual metadata is retained and unexpected text is rejected when detectable. Fixture assets are visibly labeled, non-executable, and never treated as live production output. Usage and provider costs remain null/`unknown` when absent; no price is guessed.

## Live-provider lifecycle and media QA

Migration `012_live_media_qa.sql` adds provider job/poll metadata, append-only provider lifecycle events, separate QA results, asset staleness/usability, and a normalized cost ledger. Asynchronous adapters own provider-specific authentication, response parsing, polling, Retry-After handling, and output download. Polling and transient retries are bounded; authentication, invalid requests, policy rejection, missing output, download failure, and malformed responses fail closed with normalized safe error codes.

QA is deliberately split into four visible states:

- Technical validation: deterministic bytes, format, MIME, dimensions/duration, checksum, storage, and current factual/rights lineage.
- Text-overlay validation: compares expected package text with reliable provider-returned detected text. Without reliable OCR or returned text it records `NOT_PERFORMED`.
- Semantic visual QA: replaceable `VisualQAProvider`; without a configured provider it records `NOT_PERFORMED` and flags public-figure identity for human inspection.
- Human review: always `REQUIRED`. Neither a passing technical check nor `READY_FOR_REVIEW` is publication approval.

Provider-reported cost is preferred when available. Otherwise rendering cost remains null/`unknown`. The append-only `cost_ledger` provides a normalized stage/reference model for eventual whole-story cost aggregation without altering prior research, verification, Content CEO, or production records. Signed output URLs, credentials, authorization headers, and secret-bearing provider metadata are scrubbed and never used as durable storage references.

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

Tests cover fresh and upgrade migrations, ingestion and clustering, research and verification safeguards, Content CEO policy, production entry gates, strict structured-output validation, claim locking, duplicate jobs, race invalidation, immutable packages, renderer entry gates, provider configuration, checksums and persistence, idempotency, regeneration, bounded transient retries, malformed/corrupt media, rights/evidence invalidation, immutable asset versions, usage/cost handling, and fixture isolation.

## API

- `GET /api/health`
- `GET /api/overview`
- `GET /api/events/{id}` — Event Room overview, sources, claims, verification matrix, Content CEO, production, and history
- `GET /api/events/{id}/signals`
- `GET /api/research/{run_id}` — background progress and usage
- `GET /api/verification/{run_id}` — verification progress, limits, usage, and decision summary
- `GET /api/content-decisions/{run_id}` — Content CEO progress and provider usage
- `GET /api/production/{job_id}` — production state, validation, usage, and errors
- `GET /api/render-jobs/{job_id}` — render state, attempts, validation, usage, and asset lineage
- `GET /api/generated-assets/{asset_id}` — generated-asset metadata and provenance
- `GET /api/generated-assets/{asset_id}/content` — controlled-storage asset bytes for preview
- `POST /api/ingest-url` — `{ "url": "https://public.example/report" }`
- `POST /api/ingest-configured` — polls due sources; `{ "force": true }` bypasses the poll interval for testing
- `POST /api/events` — disabled; events must originate from qualified source signals
- `POST /api/events/{id}/research` — `{ "provider": "test" }` or explicit `{ "provider": "grok" }`
- `POST /api/research/{run_id}/verify` — explicit `{ "provider": "test" }` or paid `{ "provider": "grok" }`
- `POST /api/events/{id}/content-decision` — deterministic preview `{ "provider": "test" }` or explicit live `{ "provider": "grok" }`
- `POST /api/content-decisions/{decision_id}/production` — explicit live `{ "provider": "anthropic" }`; optional `{ "regenerate": true }` creates a new version
- `POST /api/content-packages/{package_id}/render` — explicit configured renderer call with media type; optional `{ "regenerate": true }` creates a new immutable render version
- `POST /api/events/{id}/transition` — `{ "state": "VERIFYING" }`

The API remains bound to localhost and has no authentication. Add authentication, request authorization, a production datastore, source-specific extraction adapters, and an external scheduler before deployment.
