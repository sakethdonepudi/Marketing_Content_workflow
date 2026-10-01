# ReachOut OS — Architecture 06F-S

Local event intelligence desk for **N. Chandrababu Naidu, Chief Minister of Andhra Pradesh, India**, with factual ingestion, claim-specific source acquisition and verification, evidence-gated content decisions, approval-ready structured content packages, explicit media rendering, and separate media QA. The explicit workspace key is `n-chandrababu-naidu-andhra-pradesh`; the ambiguous abbreviation “CBN” is not used as workspace identity. The system stops at generated assets in `READY_FOR_REVIEW`, with human review still required. It does not publish posts, connect to social platforms, auto-approve evidence or assets, or perform demographic or political targeting.

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

**Generate media** is a second, explicit action after a package reaches `READY_FOR_APPROVAL`. Provider selection is configured independently for image, video, and audio with `RENDERER_PROVIDER_IMAGE`, `RENDERER_PROVIDER_VIDEO`, and `RENDERER_PROVIDER_AUDIO`. This repository includes the generic renderer boundary, one live still-image adapter (`xai`, Grok Imagine), and a deterministic IMAGE fixture for tests/demonstrations. With provider settings blank, the UI clearly reports that live rendering is unavailable and creates no job. Setting `fixture` is appropriate only in an isolated test or demonstration environment.

Architecture 06C adds a provider-neutral asynchronous lifecycle behind the same renderer contract: submit, bounded poll, normalized completion/failure, temporary-output download, and controlled storage. Live rendering is enabled only when `RENDERER_PROVIDER_IMAGE=xai` is set explicitly. The credential is `LIVE_RENDERER_API_KEY` when present; otherwise the xAI renderer reuses the existing `XAI_API_KEY` internally without copying it. Only the source variable name is ever surfaced, never the value. Without a usable credential the UI shows "Image renderer not configured" and no job is created. A caller cannot substitute a different provider, including the fixture, through the API.

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

### Architecture 06F-R — verification reliability

Migration `015_verification_reliability.sql` adds additive phase checkpoints, independent provider-attempt records, explicit recoverable-timeout state, and pairwise source-family provenance. The factual policy is unchanged: official primary support or two genuinely independent reporting families is still required, and any contradiction or unresolved required claim still fails closed.

Verification now checkpoints primary evidence extraction, corroboration discovery, source retrieval, claim/source matching, contradiction analysis, and final adjudication. A transient timeout pauses the existing run with a `REVIEW_REQUIRED` claim set; it does not create an `INSUFFICIENT_EVIDENCE` decision. “Resume verification” keeps the same run, snapshots, lineage, completed checkpoints, and partial analysis while recording every new provider request as a separate attempt linked to its predecessor.

Live discovery uses a 180-second search timeout, a 30-second source-retrieval phase timeout, and a 300-second per-execution total budget by default. Only connection/response timeout, temporary network interruption, retryable HTTP 408/409/429, or temporary 5xx responses receive one bounded retry. `Retry-After` is respected up to the configured 30-second cap. Authentication failure, malformed provider output, invalid sources, contradictions, policy rejection, and insufficient evidence are never retried. Failed attempts without authoritative billing remain `unknown`, even when a later attempt succeeds.

Source families are conservatively merged for the same normalized host, the same registered publisher, identical normalized text, or syndication-level text overlap. Every pair is stored as `SAME_FAMILY` or `INDEPENDENT_FAMILY` with its hosts, similarity, and reason. Concrete references to notifications, orders, ministries, regulators, filings, courts, or other first-party records are passed to discovery as official-source hints; they remain leads until fetched, source-validated, and matched to a claim.

### Architecture 06F-S — source acquisition hardening

Migration `016_source_acquisition.sql` adds a configurable official-authority registry, provider-neutral discovery plans, candidate documents, page-addressable PDF text, source-family assessments, claim/candidate matching, and structured evidence packets. It is additive and forward-only. The registry lives in `config/official_sources.json`; matching a government domain identifies an authoritative candidate but never makes a claim true.

Every verification run receives an offline discovery plan covering authoritative-domain search, exact phrases, document-title fragments, entity/date terms, independent corroboration, and direct-document links. The provider boundary works with deterministic, composite, or future search adapters, so a zero-result provider does not disable direct retrieval or another provider. No provider search runs merely because a plan was created.

The Verification tab includes **Add evidence URL**. The backend applies the same public-network and redirect restrictions, retrieves HTML or PDF bytes, stores the final URL/status/type/checksum/text/metadata, classifies source quality, groups syndicated families, matches passages to individual claims, and builds evidence packets. PDF text is extracted locally with macOS PDFKit and retains page numbers; unreadable PDFs cannot support a claim. A supplied URL never changes a decision by itself. A later explicit verification run re-applies the unchanged policy, and can skip paid discovery when staged evidence already satisfies the deterministic routing condition.

Acquisition accounting keeps search-provider calls, direct HTTP retrievals, and LLM adjudication calls separate. Missing provider billing remains null/`unknown`, never zero. The offline fixtures cover an official document, a second independent family, a syndicated copy, an official page with absent claim text, page-specific PDF support, and exhausted discovery that remains `REVIEW_REQUIRED` with no downstream production.

## Content CEO

Migration `009_content_ceo.sql` adds a media inventory, publishing history, Content CEO runs and audit history, and immutable structured decisions. A decision records `CREATE`, `HOLD`, `MONITOR`, `SKIP`, or `HUMAN_REVIEW`; the recommended format; language; duration; priority; factual rationale; exact claim-set/evidence versions; blockers; timestamp; policy version; and whether it is executable.

Production eligibility requires the latest claim set to be `APPROVED`, backed by a completed live verification run, attached to a currently `VERIFIED` event, non-empty, and unchanged against current evidence and claim-version hashes. This gate executes before provider selection. A stale or missing approval produces `HOLD` with `provider_called=0`. TEST_ONLY claim sets may exercise the full decision path, but their decisions carry `test_only=1`, `executable=0`, and never transition the event to production work.

`content_ceo.py` provides deterministic and Grok adapters. Providers see only event metadata, approved claim versions, media inventory, freshness, and recent publishing records. Local enforcement prevents `CREATE` when the selected format lacks rights-verified media and converts recent duplicates to `SKIP`. Input hashes include evidence, approved claims, media state, publishing history, policy version, and decision day; unchanged inputs reuse a cached decision.

## Content production orchestrator

Migration `010_content_production_orchestrator.sql` adds the explicit production state machine, immutable input snapshots, job history, claim/evidence/media links, provider drafts, and immutable versioned content packages. Entry is rejected before job creation for `HOLD`, `MONITOR`, `SKIP`, `HUMAN_REVIEW`, non-executable, TEST_ONLY, stale, revoked, superseded, unverified, or media-ineligible decisions. Duplicate active jobs are prevented by a partial unique index; completed inputs are idempotent, and regeneration requires an explicit flag and produces a new job/package version.

`content_production.py` defines the generic provider interface, live Anthropic adapter, and controlled fixture adapter. The live adapter sends one `POST /v1/messages` request with no tools and uses Anthropic structured outputs. The response is not trusted merely because it matches the schema: a deterministic validator checks exact approved claim-version references, unknown references, numerical values, quotations, certainty upgrades, format/language/duration, revoked evidence, current media, and mid-run version changes. A failed check creates no package and routes the job to `HUMAN_REVIEW` or `BLOCKED`.

Usage fields are persisted exactly when Anthropic returns them. Cost remains `unknown` unless Anthropic returns an authoritative cost value; the application never converts token counts with local price assumptions or treats missing usage as zero.

## Media rendering orchestrator

Migration `011_media_rendering.sql` adds the RenderJob state machine, immutable prompt snapshots, append-only attempts and status history, generated-asset records, and render-to-asset lineage. A RenderJob moves through `QUEUED`, `PREPARING`, `RENDERING`, and `VALIDATING` before reaching `READY_FOR_REVIEW`; fail-closed terminal outcomes include `HUMAN_REVIEW`, `BLOCKED`, `FAILED`, and `CANCELLED`. Direct arbitrary transitions are rejected.

`media_rendering.py` defines the replaceable `MediaRenderer` interface and deterministic fixture renderer. `media_storage.py` defines the replaceable storage contract and content-addressed local implementation. Stored asset metadata includes the originating RenderJob, exact package/version, provider/model, provider IDs when available, dimensions or duration, MIME type, SHA-256 checksum, prompt/config versions, rights-cleared references, provenance, provider-returned text metadata, validation result, and immutable version. Identical binaries are linked rather than duplicated.

Validation checks response presence, non-zero bytes, MIME/media consistency, persisted storage, checksum, PNG structure and dimensions, output count, minimum dimensions, aspect ratio, exact lineage, current rights, and post-render invalidation. Fixture assets are visibly labeled, non-executable, and never treated as live production output. Usage and provider costs remain null/`unknown` when absent; no price is guessed. Architecture 06E adds actual local OCR and multimodal semantic QA as described below.

## Live-provider lifecycle and media QA

Migration `012_live_media_qa.sql` adds provider job/poll metadata, append-only provider lifecycle events, separate QA results, asset staleness/usability, and a normalized cost ledger. Asynchronous adapters own provider-specific authentication, response parsing, polling, Retry-After handling, and output download. Polling and transient retries are bounded; authentication, invalid requests, policy rejection, missing output, download failure, and malformed responses fail closed with normalized safe error codes.

QA is deliberately split into four visible states:

- Technical validation: deterministic bytes, format, MIME, dimensions/duration, checksum, storage, and current factual/rights lineage.
- Text-overlay validation: compares expected package text with reliable provider-returned detected text. Without reliable OCR or returned text it records `NOT_PERFORMED`.
- Semantic visual QA: replaceable `VisualQAProvider`; without a configured provider it records `NOT_PERFORMED` and flags public-figure identity for human inspection.
- Human review: always `REQUIRED`. Neither a passing technical check nor `READY_FOR_REVIEW` is publication approval.

Provider-reported cost is preferred when available. Otherwise rendering cost remains null/`unknown`. The append-only `cost_ledger` provides a normalized stage/reference model for eventual whole-story cost aggregation without altering prior research, verification, Content CEO, or production records. Signed output URLs, credentials, authorization headers, and secret-bearing provider metadata are scrubbed and never used as durable storage references.

### xAI image adapter

`XAIImageRenderer` calls `POST https://api.x.ai/v1/images/generations` once per RenderJob attempt. The xAI image endpoint is synchronous, so there is no provider job ID or polling; the lifecycle is recorded as `SUBMITTED → COMPLETED → DOWNLOADED` with `poll_count = 0`. The request is derived only from the immutable prompt snapshot (visual prompts, thumbnail concept, non-factual style, aspect ratio, output count) plus fixed constraints: no rendered text, numbers, logos, flags, maps, party symbols, recognizable real people, crowds, or persuasive messaging. Text overlays are left for a later, reviewed compositing step. The exact safe request payload is persisted in the `SUBMITTED` provider event.

- Aspect ratio: the canonical image-post ratio is **3:4** (target 1200×1600). The production validator rejects IMAGE packages with any other ratio, and xAI is asked for native `aspect_ratio: "3:4"`; generated media is never stretched or cropped. Historical 4:5 packages and fixture assets remain readable, but xAI cannot render them and they are rejected **before** a job or network call (`RENDERER_CAPABILITY_MISMATCH`).
- Replays: if a live provider returns bytes identical to an existing asset, the job goes to `HUMAN_REVIEW` with `DUPLICATE_PROVIDER_OUTPUT`; the earlier asset is never modified.
- Download: the temporary output URL is fetched over HTTPS without the API key, capped at 25 MB, then saved through `MediaStorage`. The signed URL is not persisted.
- Errors: 401/403 → `AUTH_ERROR` (no retry); 429 → `RATE_LIMITED` (bounded retry honoring `Retry-After`, capped at 60 s); 5xx → `PROVIDER_5XX` (bounded retry with exponential backoff); content-policy 4xx → `CONTENT_POLICY_REJECTED` → `HUMAN_REVIEW`; other 400/422 → `INVALID_REQUEST`; malformed or output-less responses → `INVALID_RESPONSE`; download failure → `DOWNLOAD_FAILED`. A timeout **after** the request was sent is not retried because the provider may already have billed the render.
- Cost: `usage.cost_in_usd_ticks` is converted to provider-reported USD (`pricing_version = xai-reported-cost-ticks`). Missing usage stays `unknown`.
- Video is implemented by Architecture 06D below. Audio remains unsupported and fails closed.

`media_inspection.py` decodes PNG (chunk CRCs and pixel payload), JPEG (segment chain, frame header, scan, end marker), and WebP (RIFF/VP8/VP8L/VP8X) without new dependencies. Technical QA uses decoded format and dimensions, and rejects mismatches between the sniffed format, the result MIME type, and the provider-declared MIME type.

Assets report `current_for_review` at read time: a physically valid asset is not current once a newer ContentPackage exists or its lineage is no longer eligible. `cost_summary` in the Event Room aggregates live research, verification, Content CEO, production, and rendering spend, reporting unknown-cost runs separately instead of counting them as zero.

## Architecture 06D — live Claude packages and xAI video

Canonical flow: verified event → Content CEO decision → **live Claude package** → immutable media prompt → xAI image or video render → QA → human review. Nothing publishes, schedules, or auto-approves.

**Claude production.** Package generation always calls Claude when requested live. Without `ANTHROPIC_API_KEY` the request fails before any job is created ("Claude production provider unavailable"); it never falls back to the fixture. The HTTP API accepts the fixture provider only with `REACHOUT_DEMO_MODE=1`; tests call it directly. The adapter authenticates with `x-api-key` (OAuth `sk-ant-oat` tokens use `Authorization: Bearer` plus the OAuth beta header), treats `refusal` and `max_tokens` stops as failures, and stores the exact credential-free request (`production_jobs.request_snapshot_json`/`_hash`) alongside the existing response snapshot, usage, and request ID. Cost stays `unknown` unless Anthropic itself reports an authoritative value. Schema `production-package-v2` adds `media_brief` (media type, visual brief, generation prompt, negative constraints, factual constraints). Visual text may not introduce numbers, quotations, or named entities absent from the approved claims.

**Video.** `XAIVideoRenderer` submits to `POST /v1/videos/generations`, polls `GET /v1/videos/{request_id}` a bounded number of times (`XAI_VIDEO_MAX_POLL_ATTEMPTS` × `XAI_VIDEO_POLL_INTERVAL_SECONDS`), and downloads the MP4 without sending the API key. Before any paid submission it lists `GET /v1/video-generation-models` and refuses if the configured model is missing. Provider capabilities are declared per provider and media type: video supports 9:16, 16:9, 1:1, 4:3, 3:4, 3:2, 2:3; 1–15 s; 480p/720p/1080p. Modes:

- Text-to-video: REEL packages; the package aspect ratio and duration are requested natively.
- Image-to-video: "Generate video from image" binds one exact live image by ID and checksum. `aspect_ratio` is omitted because xAI would stretch the source; the video inherits the source ratio. Fixture images can never be sources. A different source image always creates a new job.
- Reference-to-video: supported by the adapter (capped at 720p) but not exposed in the UI yet.

Provider events stream into `render_provider_events` as they happen, so the UI shows the phase (Submitted → Generating n% → Downloading → Validating). Polling exhaustion, provider failures, expiry, moderation (`respect_moderation: false`), download failures, and malformed, zero-duration, wrong-ratio or truncated MP4s all fail closed. The MP4 inspector (`media_inspection.inspect_video`) decodes duration, dimensions, frame rate, codec, and audio presence from the ISO-BMFF structure. Image and video costs are reported separately (`IMAGE_RENDERING`, `VIDEO_RENDERING`); missing usage stays unknown.

**Semantic QA foundation.** Seven advisory checks are recorded individually as PASS, FLAG, or UNKNOWN: subject match, unsupported text, unintended symbols or logos, unexpected public figures, reference consistency, generation defects, and factual contradictions. Any FLAG routes to human review; nothing can approve. The model never verifies a public figure's identity.

Migration `013_live_production_and_video.sql` is additive and forward-only: production request snapshots; render-job mode, requested ratio, duration and resolution, and source asset ID and checksum; asset codec and audio flag.

## Architecture 06E — production-safe paid media

Migration `014_production_safe_media.sql` is additive and forward-only. It adds resumable-job metadata, stable request fingerprints and client replay keys, immutable source/QA derivatives, immutable versioned QA runs, and immutable per-asset review actions. Existing 06C/06D records remain readable.

**No-resubmit recovery.** A timed-out or interrupted asynchronous xAI job with a saved provider job ID remains `PROVIDER_PENDING` (its compatible database status remains `RENDERING`). “Resume status check” performs one read-only lookup of that exact ID. Processing stays pending; completion downloads and validates the original output; terminal failure is recorded; unknown/not-found status requires human intervention. Startup only marks unfinished work for recovery and never submits replacement generation.

**Paid-call idempotency.** The backend fingerprints event, package/version, media type, generation mode, exact source ID/checksum, immutable prompt request, output shape/duration/resolution, provider, and model. SQLite transaction locking plus a partial unique active-fingerprint index prevents duplicate submissions across double clicks, refresh retries, tabs, threads, and processes. Client request IDs replay the original job. UI controls disable as “Submitting…” and every live media call requires explicit confirmation; video confirmation shows the provider/model, source thumbnail and checksum, ratio, duration, resolution, package version, and billing-after-timeout warning.

**Source preparation.** Image-to-video never mutates the generated image. It creates or reuses an immutable `VIDEO_SOURCE` JPEG derivative, preserves aspect ratio, performs no crop/stretch, stores original and derivative checksums plus complete transformation parameters, and binds `GeneratedAsset → DerivedAsset → RenderJob`. `VIDEO_SOURCE_MAX_BYTES` defaults to 4 MiB; oversize preparation fails before model discovery or paid submission.

**OCR and visual QA.** On macOS, `media_tools.py` uses Apple Vision for visible-text detection and AVFoundation for representative video frames (beginning, 25%, midpoint, 75%, end). OCR detections retain confidence, bounding boxes, frame references, and deterministic policy checks for unexpected lettering, malformed headlines, watermarks, invented numbers/names, and political slogans. Video frames are immutable `QA_FRAME` derivatives with lineage. `visual_qa.py` uses configured Claude image understanding with strict structured PASS/FLAG/UNKNOWN checks; it is advisory, never verifies a person's identity, and never approves media. Missing/malformed provider output becomes UNKNOWN or a human-review warning. Costs are split into package, image, video, OCR, and visual-QA stages; only provider-returned authoritative cost is “known.”

**Human review.** Every generated asset version can be marked `APPROVED`, `CHANGES_REQUIRED`, or `REJECTED` with reviewer, timestamp, and optional comment. Reviews reference the latest QA run IDs and never inherit across regenerated versions. `APPROVED` means approved media asset only—not approval to publish. No publishing, scheduling, or social-platform route exists.

## Architecture 07 — Meta distribution (Instagram Reels, Facebook Reels)

Verified against Meta's official docs (Graph API v25.0, configurable via `META_GRAPH_API_VERSION`). Instagram: resumable container (`POST /{ig-user-id}/media`, `media_type=REELS`, `upload_type=resumable`), byte upload to `rupload.facebook.com/ig-api-upload/{v}/{container}`, `status_code` polling, `media_publish`, `permalink`. 100 API posts per 24 h. Facebook: `POST /{page-id}/video_reels` start, upload to `rupload.facebook.com/video-upload/{v}/{video}`, `fields=status` polling, finish with `video_state=PUBLISHED`. 30 API Reels per Page per 24 h; Reels must be 9:16, at least 540×960, 3–90 s.

- **Binding:** each platform package is an immutable version tied to one approved video (ID, version, checksum), its APPROVED media review, the exact ContentPackage (ID, version, hash), and the approved claim set.
- **Copy:** assembled deterministically from approved package fields; no model is called. A validator rejects any number, line, or hashtag that is not verbatim approved text. Instagram limits (2,200 characters, 30 hashtags, 20 @ tags) are enforced.
- **Compliance:** checked from the decoded file, never altered: codec, audio, fps, duration, ratio, size, moov placement, and edit lists.
- **Approval:** a separate human approval per platform package. Approval never publishes.
- **Publish gate,** re-evaluated at execution time: latest media review APPROVED, platform package APPROVED, lineage current (claims, evidence, rights, latest package and decision), `SOCIAL_PUBLISHING_ENABLED` and the platform switch ON, and credentials present.
- **Scheduling:** local only, so the switches apply at the due time. Meta-native scheduling is never used.
- **Duplicate protection:** client request keys; one in-flight job and one successful post per platform and video, enforced by unique indexes.
- **Retries:** transient failures retry within `PUBLISH_MAX_ATTEMPTS`. An ambiguous publish moves to `NEEDS_INTERVENTION` and is never re-sent; "Check status" is free and never reposts. On restart, in-flight jobs are never re-run automatically.

### Final Reel Composer (migration `020`, `021`)

`final_reel_composer.py` composes a 9:16 Reel from one approved generated video: approved-package narration, Apple Speech voice-over, burned-in synchronized subtitles, and a quiet original ambient music bed. Composition runs from the Media tab only for a live, validated, QA-passed source video, and every composition is a new immutable `final_reel_assets` version (never overwrites a prior run). The composer validates that narration is verbatim approved package/claim text, checks the output for 720×1280 H.264/AAC, 12–25 s, fast-start with no edit lists, and records technical, subtitle-OCR, audio, and factual QA plus Instagram/Facebook Reels compliance.

Subtitles are burned in as solid white glyphs with a black outline (drawn as a fill pass plus a separate stroke pass, never AppKit's negative `strokeWidth` which erases glyph interiors), sized to the wrapped text with no tall empty box, inside a 0.08/0.20 safe zone. Subtitle QA samples a frame inside every rendered cue, OCRs it, and fails unless visible-text coverage is high, so a dark box with no readable words can never pass. Narration is calibrated to a phone-speaker level (speech about -16 dBFS mean, true peak below full scale) with the music bed ducked hard under speech; audio QA measures the decoded mix for speech loudness, clipping, music-vs-speech balance, excessive silence, and cue alignment, and fails closed if narration is not intelligible.

Human approval is per exact Final Reel version in `final_reel_reviews` and never inherited. When an approved Final Reel exists, Meta distribution binds to that reel's bytes and checksum (`distribution_packages.media_source = FINAL_REEL`, `final_reel_asset_id`, `final_reel_review_id`); the raw xAI video is refused for the same source. Migration `021` is additive and forward-only. Publishing switches remain off and nothing publishes or schedules automatically.

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

Tests cover fresh and upgrade migrations, ingestion and clustering, research and verification safeguards, official registry/query planning, provider-neutral discovery, safe HTML/PDF retrieval, page references, source classification and family deduplication, claim/candidate packets, manual evidence isolation, no-search sufficiency, exhausted-discovery fail-closed behavior, Content CEO policy, production entry gates, strict structured-output validation, claim locking, duplicate jobs, race invalidation, immutable packages, renderer entry gates, provider configuration, checksums and persistence, idempotency, regeneration, bounded transient retries, malformed/corrupt media, rights/evidence invalidation, immutable asset versions, usage/cost handling, fixture isolation, the xAI adapter (configuration, capability gating, request derivation, download without credentials, error normalization, Retry-After, post-send timeout safety, MIME/dimension checks, secret scrubbing), PNG/JPEG/WebP decoding, asset currency after package supersession, and per-story cost aggregation.

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
- `GET /api/derived-assets/{derived_asset_id}/content` — controlled derivative bytes for review
- `POST /api/ingest-url` — `{ "url": "https://public.example/report" }`
- `POST /api/ingest-configured` — polls due sources; `{ "force": true }` bypasses the poll interval for testing
- `POST /api/events` — disabled; events must originate from qualified source signals
- `POST /api/events/{id}/research` — `{ "provider": "test" }` or explicit `{ "provider": "grok" }`
- `POST /api/research/{run_id}/verify` — explicit `{ "provider": "test" }` or paid `{ "provider": "grok" }`
- `POST /api/verification/{run_id}/evidence-url` — safely stages `{ "url": "https://public.example/document" }`; optional `claim_ids`; never auto-approves
- `POST /api/events/{id}/content-decision` — deterministic preview `{ "provider": "test" }` or explicit live `{ "provider": "grok" }`
- `POST /api/content-decisions/{decision_id}/production` — explicit confirmed live Claude call with `client_request_id`; optional `{ "regenerate": true }`
- `POST /api/content-packages/{package_id}/render` — explicit confirmed configured renderer call with `client_request_id`; optional `{ "regenerate": true }`
- `POST /api/render-jobs/{job_id}/resume` — one free/read-only check of the saved provider job; never resubmits
- `POST /api/generated-assets/{asset_id}/qa` — immutable manual OCR/visual QA rerun
- `POST /api/generated-assets/{asset_id}/review` — asset-version-only `APPROVED`, `CHANGES_REQUIRED`, or `REJECTED`
- `POST /api/events/{id}/transition` — `{ "state": "VERIFYING" }`

The API remains bound to localhost and has no authentication. Add authentication, request authorization, a production datastore, source-specific extraction adapters, and an external scheduler before deployment.
