// ReachOut OS dashboard. All data is rendered with textContent; no HTML from the API is injected.

const state = {
  eventId: null,
  tab: 'overview',
  room: null,
  overview: null,
  poll: null,
  selectedAssetId: null,
  researchProvider: 'test',
  researchSearch: false,
  contentProvider: 'test',
  messages: {},
  busy: {},
  reelInputs: {cbn: null, tdp: null},
};

const TABS = ['overview', 'evidence', 'verification', 'decision', 'package', 'media', 'distribution', 'activity'];
const LEGACY_TABS = {sources: 'evidence', claims: 'evidence', content: 'decision', production: 'media', history: 'activity'};
const ACTIVE_STATUSES = ['QUEUED', 'RUNNING', 'GENERATING', 'PREPARING', 'RENDERING', 'VALIDATING'];

/* ---------- helpers ---------- */

function el(tag, className, ...children) {
  const item = document.createElement(tag);
  if (className) item.className = className;
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    item.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return item;
}

const fmt = time => time ? new Date(time).toLocaleString(undefined, {month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit'}) : 'Not stated';
const fmtShort = time => time ? new Date(time).toLocaleString(undefined, {month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit'}) : '—';
const money = value => value === null || value === undefined ? 'Unknown' : `$${Number(value) < 0.01 && Number(value) > 0 ? Number(value).toFixed(4) : Number(value).toFixed(2)}`;
const bytes = value => value == null ? '—' : value > 1048576 ? `${(value / 1048576).toFixed(1)} MB` : `${Math.max(1, Math.round(value / 1024))} KB`;

const LABELS = {
  READY_FOR_REVIEW: 'Ready for review', READY_FOR_APPROVAL: 'Ready for approval', HUMAN_REVIEW: 'Needs human review',
  NOT_PERFORMED: 'Not performed', NOT_RESEARCHED: 'Not researched', REVIEW_REQUIRED: 'Review required',
  TEST_ONLY: 'Test data only', INSUFFICIENT_EVIDENCE: 'Insufficient evidence', REQUIRED: 'Required',
  LIVE_RENDERER_NOT_CONFIGURED: 'Image renderer not configured', LIVE_RENDERER_CREDENTIALS_MISSING: 'Image renderer not configured',
  LIVE_RENDERER_PROVIDER_UNSUPPORTED: 'Provider unsupported', LIVE_RENDERER_CONFIGURED: 'Live renderer ready', FIXTURE_ONLY: 'Fixture only',
  TEXT_TO_VIDEO: 'Text to video', IMAGE_TO_VIDEO: 'Image to video', REFERENCE_TO_VIDEO: 'Reference to video',
  CLAUDE_PRODUCTION_UNAVAILABLE: 'Claude production provider unavailable', CLAUDE_PRODUCTION_READY: 'Claude ready',
  POLL_ATTEMPTS_EXHAUSTED: 'Polling timed out', DUPLICATE_PROVIDER_OUTPUT: 'Duplicate provider output',
  PROVIDER_PENDING: 'Provider pending', NEEDS_INTERVENTION: 'Needs intervention', INTERRUPTED: 'Recovery needed',
  PAUSED_TRANSIENT: 'Provider timed out', TRANSIENT_PROVIDER_TIMEOUT: 'Provider timed out',
  TRANSIENT_PROVIDER_FAILURE: 'Temporary provider failure', RETRIEVAL_TIMEOUT: 'Source retrieval timed out',
  OCR_QA: 'OCR QA', VISUAL_QA: 'Visual QA', CHANGES_REQUIRED: 'Changes required',
};
function label(code) {
  if (code === null || code === undefined || code === '') return '—';
  const key = String(code);
  if (LABELS[key]) return LABELS[key];
  const text = key.replaceAll('_', ' ').toLowerCase();
  return text.charAt(0).toUpperCase() + text.slice(1);
}

const TONES = {
  good: ['COMPLETED', 'VERIFIED', 'SUPPORTED', 'CREATE', 'READY_FOR_APPROVAL', 'READY_FOR_REVIEW', 'PASSED', 'VALIDATED', 'APPROVED', 'VALID', 'ELIGIBLE', 'LIVE_RENDERER_CONFIGURED'],
  warn: ['REVIEW_REQUIRED', 'HUMAN_REVIEW', 'HOLD', 'MONITOR', 'FLAG', 'FLAGGED', 'UNKNOWN', 'INSUFFICIENT_EVIDENCE', 'TEST_ONLY', 'PARTIAL', 'FIXTURE_ONLY', 'LIVE_RENDERER_NOT_CONFIGURED', 'LIVE_RENDERER_CREDENTIALS_MISSING', 'REQUIRED', 'PROVIDER_PENDING', 'NEEDS_INTERVENTION', 'INTERRUPTED', 'CHANGES_REQUIRED', 'PAUSED_TRANSIENT', 'TRANSIENT_PROVIDER_TIMEOUT', 'TRANSIENT_PROVIDER_FAILURE', 'RETRIEVAL_TIMEOUT'],
  bad: ['FAILED', 'BLOCKED', 'REJECTED', 'CONFLICTED', 'CONTRADICTED', 'SKIP', 'CANCELLED', 'INVALID', 'LIVE_RENDERER_PROVIDER_UNSUPPORTED'],
  info: ACTIVE_STATUSES.concat(['VERIFYING', 'RESEARCHING']),
};
function tone(code) {
  for (const [name, codes] of Object.entries(TONES)) if (codes.includes(code)) return name;
  return '';
}
const pill = (code, text) => el('span', `pill ${tone(code)}`, text || label(code));
const plainPill = (text, toneName = '') => el('span', `pill plain ${toneName}`, text);

function facts(pairs) {
  const list = el('dl', 'facts');
  for (const [term, value] of pairs) {
    if (value === undefined) continue;
    list.append(el('div', '', el('dt', '', term), el('dd', '', value === null || value === '' ? '—' : value)));
  }
  return list;
}
function card(className, ...children) { return el('section', `card ${className || ''}`, ...children); }
function cardHead(title, ...right) { return el('div', 'card-head', el('h2', '', title), el('div', 'chip-row', ...right)); }
function callout(kind, title, ...body) { return el('div', `callout ${kind}`, title ? el('strong', '', title) : null, ...body); }
function empty(title, text) { return el('div', 'empty', el('strong', '', title), text); }
function disclosure(summary, ...body) {
  const details = el('details', 'disclosure');
  details.append(el('summary', '', summary), el('div', 'disclosure-body', ...body));
  return details;
}
function bullets(items, className = 'list') { return el('ul', className, items.map(item => el('li', '', item))); }
function refs(ids) { return (ids || []).length ? el('div', 'chip-row', ids.map(id => el('span', 'ref', id))) : null; }
function externalLink(text, href, className = '') {
  const link = el('a', className, text);
  if (href && /^https?:\/\//i.test(href)) { link.href = href; link.target = '_blank'; link.rel = 'noopener noreferrer'; }
  return link;
}
function timeline(items) {
  return el('ol', 'timeline', items.map(item => el('li', '',
    el('time', '', fmtShort(item.at)), el('span', `tl-dot ${item.tone || ''}`),
    el('div', 'tl-body', el('strong', '', item.title), item.detail ? ` · ${item.detail}` : ''),
  )));
}
const PROVIDER_NAMES = {xai: 'xAI', 'deterministic-image-fixture': 'Deterministic fixture'};
const providerName = name => PROVIDER_NAMES[name] || name;
const productionLabel = job => job.fixture_only ? plainPill('Fixture · demo', 'warn') : plainPill(`Claude · live`, 'good');
const isVideo = type => ['VIDEO', 'SHORT_FORM_VIDEO', 'LONG_FORM_VIDEO'].includes(type);
const modeLabel = mode => mode === 'live' ? plainPill('Live', 'good') : plainPill('Test data', 'warn');

/* ---------- desk view ---------- */

function eventPipeline(event) {
  const stages = [
    ['Research', event.research_status, s => !s || s === 'NOT_RESEARCHED' ? '' : s === 'COMPLETED' || s === 'REVIEW_REQUIRED' ? 'done' : tone(s)],
    ['Verify', event.verification_status, s => s === 'VERIFIED' ? 'done' : tone(s)],
    ['Decide', event.content_decision_status, s => s === 'CREATE' ? 'done' : tone(s)],
    ['Package', event.production_status, s => s === 'READY_FOR_APPROVAL' ? 'done' : tone(s)],
    ['Media', event.render_status, s => s === 'READY_FOR_REVIEW' ? 'done' : tone(s)],
  ];
  const dots = stages.map(([, value, classify]) => { const cls = value ? classify(value) : ''; return cls === 'good' ? 'done' : cls; });
  let furthest = null;
  stages.forEach(([name, value], index) => { if (value && value !== 'NOT_RESEARCHED') furthest = [name, value, index]; });
  const wrap = el('div', '', el('div', 'mini-pipe', dots.map(cls => el('i', cls))));
  wrap.append(el('div', 'mini-pipe-label', furthest ? `${furthest[0]} · ${label(furthest[1])}` : 'Not started'));
  return wrap;
}

function renderProviders(data) {
  const list = document.getElementById('provider-list');
  const image = data.media_rendering?.image || {};
  const video = data.media_rendering?.video || {};
  const rows = [
    ['Grok', 'research', data.research?.grok_configured],
    ['Claude', 'production', data.content_production?.anthropic_configured],
    ['Image renderer', image.live ? providerName(image.provider) : 'media', image.live],
    ['Video renderer', video.live ? providerName(video.provider) : 'media', video.live],
  ];
  list.replaceChildren(...rows.map(([name, role, ready]) => el('li', '',
    el('span', '', name, ' ', el('small', '', role)), plainPill(ready ? 'Ready' : 'Off', ready ? 'good' : ''),
  )));
}

async function refresh() {
  try {
    const response = await fetch('/api/overview');
    const data = await response.json();
    state.overview = data;
    document.getElementById('clock').textContent = `Updated ${fmt(data.updated_at)}`;
    document.getElementById('event-count').textContent = `${data.events.length} event${data.events.length === 1 ? '' : 's'}`;
    document.getElementById('reference-count').textContent = data.reference_count;
    document.getElementById('review-count').textContent = data.review_count;
    document.getElementById('rejected-count').textContent = data.rejected_count;
    document.getElementById('discovered-event-count').textContent = data.events.length;
    renderProviders(data);
    const list = document.getElementById('event-list');
    if (!data.events.length) {
      list.replaceChildren(el('div', 'empty-state', 'No events yet. Paste a public report URL above to test the source monitor.'));
      return;
    }
    list.replaceChildren(...data.events.map(event => {
      const count = Number(event.source_count) || 0;
      const row = el('button', 'event-row',
        el('div', '', el('div', 'event-title', event.title), el('div', 'event-sub', `${label(event.priority)} priority · ${label(event.status)}`)),
        eventPipeline(event),
        el('div', 'cell-muted', `${count} source${count === 1 ? '' : 's'}`),
        el('div', 'cell-muted', fmtShort(event.publication_time || event.event_time)),
      );
      row.type = 'button';
      row.title = (event.source_names || []).join(', ');
      row.onclick = () => openEvent(event.id);
      return row;
    }));
  } catch (error) {
    document.getElementById('event-list').replaceChildren(el('div', 'empty-state', 'Cannot reach the local API.'));
  }
}

document.getElementById('ingest-form').onsubmit = async submitEvent => {
  submitEvent.preventDefault();
  const input = document.getElementById('ingest-url');
  const button = document.getElementById('ingest');
  const status = document.getElementById('ingest-status');
  button.disabled = true;
  status.textContent = 'Fetching and matching source…';
  try {
    const response = await fetch('/api/ingest-url', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({url: input.value})});
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || 'Ingestion failed');
    status.textContent = result.duplicate ? 'Already ingested — existing signal retained.' : result.reference ? 'Saved as reference material — no event created.' : result.review ? 'Held for review — the date or identity is uncertain.' : result.rejected ? 'Rejected from event discovery.' : result.clustered ? 'Signal added to an existing event.' : 'New event detected.';
    input.value = '';
    await refresh();
  } catch (error) { status.textContent = error.message; } finally { button.disabled = false; }
};

/* ---------- pipeline model ---------- */

const latest = list => (list || [])[0] || null;
const isActive = item => item && ACTIVE_STATUSES.includes(item.status);

function steps(data) {
  const event = data.event;
  const runs = data.runs || [];
  const research = runs.find(isActive) ? {state: 'active', status: 'Running'}
    : runs.find(run => run.status === 'COMPLETED') ? {state: 'done', status: event.research_status === 'REVIEW_REQUIRED' ? 'Complete · review flagged' : 'Complete'}
    : latest(runs)?.status === 'FAILED' ? {state: 'bad', status: 'Failed'} : {state: '', status: 'Not started'};

  const verifications = data.verification_runs || [];
  const verification = verifications.find(isActive) ? {state: 'active', status: 'Running'}
    : event.verification_status === 'VERIFIED' ? {state: 'done', status: 'Verified'}
    : verifications.length ? {state: tone(event.verification_status) === 'bad' ? 'bad' : 'warn', status: label(event.verification_status)}
    : {state: '', status: 'Not started'};

  const decisionRun = latest(data.content_decision_runs);
  const record = decisionRun?.decision_record;
  const decision = isActive(decisionRun) ? {state: 'active', status: 'Deciding'}
    : !record ? {state: decisionRun?.status === 'FAILED' ? 'bad' : '', status: decisionRun ? label(decisionRun.status) : 'Not started'}
    : record.decision === 'CREATE' && record.executable ? {state: 'done', status: `Create · ${label(record.recommended_format)}`}
    : record.decision === 'CREATE' ? {state: 'warn', status: 'Create · test decision'}
    : {state: record.decision === 'SKIP' ? 'bad' : 'warn', status: label(record.decision)};

  const jobs = data.production_jobs || [];
  const readyJob = jobs.find(job => job.status === 'READY_FOR_APPROVAL' && job.package);
  const production = jobs.find(isActive) ? {state: 'active', status: label(jobs.find(isActive).status)}
    : readyJob ? {state: readyJob.fixture_only ? 'warn' : 'done', status: readyJob.fixture_only ? 'Ready · fixture demo' : 'Ready · Claude live'}
    : jobs.length ? {state: tone(jobs[0].status) === 'bad' ? 'bad' : 'warn', status: label(jobs[0].status)}
    : {state: '', status: 'Not started'};

  const renders = data.render_jobs || [];
  const renderJob = latest(renders);
  const media = isActive(renderJob) ? {state: 'active', status: label(renderJob.render_phase?.phase || renderJob.status) + (renderJob.render_phase?.progress != null ? ` · ${renderJob.render_phase.progress}%` : '')}
    : renderJob?.status === 'READY_FOR_REVIEW' ? {state: renderJob.fixture_only ? 'warn' : 'done', status: renderJob.fixture_only ? 'Fixture placeholder' : `${isVideo(renderJob.media_type) ? 'Video' : 'Image'} ready for review`}
    : renderJob && renders.some(job => job.status === 'READY_FOR_REVIEW') ? {state: 'warn', status: `Latest attempt ${label(renderJob.status).toLowerCase()} · earlier version ready`}
    : renderJob ? {state: tone(renderJob.status) === 'bad' ? 'bad' : 'warn', status: label(renderJob.status)}
    : {state: '', status: 'Not started'};

  const newestReel = (data.final_reels || [])[0];
  const reelReview = newestReel?.latest_review;
  const newestAsset = renders.flatMap(job => job.assets || []).sort((a, b) => (b.created_at || '').localeCompare(a.created_at || ''))[0];
  const latestReview = newestAsset?.latest_review;
  const review = reelReview ? {state: reelReview.action === 'APPROVED' ? 'done' : reelReview.action === 'REJECTED' ? 'bad' : 'warn', status: `Final Reel · ${label(reelReview.action)}`}
    : newestReel ? {state: 'active', status: 'Final Reel · awaiting review'}
    : latestReview ? {state: latestReview.action === 'APPROVED' ? 'done' : latestReview.action === 'REJECTED' ? 'bad' : 'warn', status: label(latestReview.action)}
    : renders.some(job => job.status === 'READY_FOR_REVIEW' || job.status === 'HUMAN_REVIEW') ? {state: 'active', status: 'Awaiting human review'} : {state: '', status: 'After media'};

  return [
    {name: 'Research', tab: 'overview', ...research},
    {name: 'Verify', tab: 'verification', ...verification},
    {name: 'Decide', tab: 'decision', ...decision},
    {name: 'Package', tab: 'package', ...production},
    {name: 'Media', tab: 'media', ...media},
    {name: 'Review', tab: 'media', ...review},
  ];
}

function renderStepper(data) {
  const icons = {done: '✓', warn: '!', bad: '×', active: '…'};
  document.getElementById('stepper').replaceChildren(...steps(data).map((step, index) => {
    const button = el('button', '', el('span', 'step-top', el('span', 'step-icon', icons[step.state] || String(index + 1)), step.name), el('span', 'step-status', step.status));
    button.type = 'button';
    button.onclick = () => setTab(step.tab);
    const current = step.tab === state.tab && (step.name !== 'Review' || state.tab !== 'media');
    return el('li', `step ${step.state} ${current ? 'current' : ''}`, button);
  }));
}

/* ---------- actions ---------- */

function anyActive(data) {
  return (data.runs || []).find(isActive) || (data.verification_runs || []).find(isActive)
    || (data.content_decision_runs || []).find(isActive) || (data.production_jobs || []).find(isActive)
    || (data.render_jobs || []).find(isActive);
}

function actionButton(text, key, handler, {primary = true, disabled = false, allowDuringActive = false} = {}) {
  const button = el('button', `btn ${primary ? 'primary' : ''}`, state.busy[key] ? 'Submitting…' : text);
  button.type = 'button';
  button.disabled = disabled || Boolean(state.busy[key]) || (!allowDuringActive && Boolean(anyActive(state.room)));
  button.onclick = handler;
  return button;
}

const clientRequestId = () => globalThis.crypto?.randomUUID?.() || `request-${Date.now()}-${Math.random().toString(16).slice(2)}`;

function confirmPaidAction({title, warning, rows = [], thumbnail = null, confirmText = 'Start paid generation'}) {
  return new Promise(resolve => {
    const dialog = el('dialog', 'paid-dialog');
    const close = answer => { dialog.close(); dialog.remove(); resolve(answer); };
    const content = el('div', 'paid-dialog-content', el('h2', '', title), callout('warn', 'Paid provider action', warning));
    if (thumbnail) {
      const image = el('img'); image.src = thumbnail; image.alt = 'Exact source asset thumbnail';
      content.append(el('div', 'confirmation-source', image));
    }
    content.append(facts(rows), el('p', 'secondary-text', 'This action creates media only. It cannot publish, schedule, or auto-approve anything.'));
    const cancel = el('button', 'btn ghost', 'Cancel'); cancel.type = 'button'; cancel.onclick = () => close(false);
    const confirm = el('button', 'btn primary', confirmText); confirm.type = 'button'; confirm.onclick = () => close(true);
    content.append(el('div', 'dialog-actions', cancel, confirm));
    dialog.append(content); dialog.oncancel = event => { event.preventDefault(); close(false); };
    document.body.append(dialog); dialog.showModal();
  });
}

async function post(url, body, key, pending, successMessage, nextTab) {
  state.busy[key] = true;
  state.messages[key] = pending;
  renderRoom();
  try {
    const response = await fetch(url, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || 'The request could not start');
    state.messages[key] = successMessage(result);
    if (nextTab) state.tab = nextTab;
  } catch (error) {
    state.messages[key] = error.message;
  } finally {
    state.busy[key] = false;
    await loadRoom();
  }
}

function progressFor(key, activeItem) {
  if (activeItem?.render_phase) {
    const phase = activeItem.render_phase;
    return el('span', 'action-progress', `${label(phase.phase)}${phase.progress != null ? ` · ${phase.progress}%` : ''} · polls ${activeItem.poll_count || 0}`);
  }
  const text = activeItem ? (activeItem.progress_message ? `${activeItem.progress ?? ''}${activeItem.progress != null ? '% · ' : ''}${activeItem.progress_message}` : label(activeItem.status)) : state.messages[key];
  return el('span', 'action-progress', text || '');
}

function actionCopy(title, text) { return el('div', 'action-copy', el('strong', '', title), el('span', '', text)); }

function renderActionBar(data) {
  const bar = document.getElementById('action-bar');
  const grok = state.overview?.research?.grok_configured;
  bar.replaceChildren();
  if (state.tab === 'overview') {
    const select = el('select', '', el('option', '', 'Deterministic test data'), el('option', '', grok ? 'Grok · live paid call' : 'Grok · API key missing'));
    select.options[0].value = 'test'; select.options[1].value = 'grok';
    select.value = state.researchProvider;
    select.onchange = () => { state.researchProvider = select.value; if (select.value !== 'grok') state.researchSearch = false; renderActionBar(state.room); };
    const search = el('input'); search.type = 'checkbox'; search.checked = state.researchSearch; search.disabled = state.researchProvider !== 'grok';
    search.onchange = () => { state.researchSearch = search.checked; };
    bar.append(
      actionCopy('Research', 'Extract claims and timing from linked evidence. Live Grok is a paid call.'),
      el('label', '', 'Provider', select), el('label', '', search, 'One bounded web search'),
      actionButton((data.runs || []).length ? 'Re-run research' : 'Research event', 'research', () => post(
        `/api/events/${encodeURIComponent(state.eventId)}/research`,
        {provider: state.researchProvider, search_limit: state.researchProvider === 'grok' && state.researchSearch ? 1 : 0},
        'research', state.researchProvider === 'grok' ? 'Starting explicit live research…' : 'Starting test research…',
        result => result.cached ? 'Evidence unchanged · cached result reused' : result.duplicate ? 'Research already in progress' : 'Research queued',
      )),
      progressFor('research', (data.runs || []).find(isActive)),
    );
  } else if (state.tab === 'verification') {
    const run = (data.runs || []).find(item => item.status === 'COMPLETED' && item.mode === 'live') || (data.runs || []).find(item => item.status === 'COMPLETED');
    const paused = (data.verification_runs || []).find(item => item.recoverable && item.resume_state === 'PAUSED_TRANSIENT');
    const verification = (data.verification_runs || [])[0];
    const evidenceUrl = el('input'); evidenceUrl.type = 'url'; evidenceUrl.placeholder = 'Add evidence URL…'; evidenceUrl.setAttribute('aria-label', 'Evidence URL');
    bar.append(
      actionCopy('Corroboration', paused ? 'Verification paused because the evidence provider timed out. Resume preserves prior evidence and records a new provider attempt.' : run ? (run.mode === 'live' ? 'Paid Grok search for independent corroboration of each claim.' : 'Test-data corroboration using the latest completed research.') : 'Complete research first.'),
      paused ? actionButton('Resume verification', 'verify-resume', () => post(
        `/api/verification/${encodeURIComponent(paused.id)}/resume`, {}, 'verify-resume',
        'Resuming from the last safe checkpoint…', () => 'Verification resume queued',
      )) : actionButton('Find corroboration', 'verify', () => post(
        `/api/research/${encodeURIComponent(run.id)}/verify`, {provider: run.mode === 'live' ? 'grok' : 'test'}, 'verify',
        run.mode === 'live' ? 'Starting explicit paid corroboration search…' : 'Starting test corroboration…',
        result => result.resume_required ? 'Use Resume verification for the paused run' : result.cached ? 'Evidence and claims unchanged · cached decision reused' : result.duplicate ? 'Verification already in progress' : 'Corroboration queued',
      ), {disabled: !run}),
      el('label', 'evidence-url-control', 'Reviewer evidence', evidenceUrl),
      actionButton('Add evidence URL', 'add-evidence', () => {
        if (!evidenceUrl.value.trim()) { state.messages['add-evidence'] = 'Enter a public evidence URL.'; renderRoom(); return; }
        post(`/api/verification/${encodeURIComponent(verification.id)}/evidence-url`, {url: evidenceUrl.value.trim()},
          'add-evidence', 'Retrieving, classifying, deduplicating, and matching…',
          result => `${result.candidates.filter(item => item.state === 'RETRIEVED').length} candidate(s) staged · no decision changed`, 'verification');
      }, {primary: false, disabled: !verification}),
      progressFor('verify', (data.verification_runs || []).find(isActive)),
      progressFor('add-evidence', null),
    );
  } else if (state.tab === 'decision') {
    const select = el('select', '', el('option', '', 'Deterministic preview'), el('option', '', state.overview?.content_ceo?.grok_configured ? 'Grok · live paid call' : 'Grok · API key missing'));
    select.options[0].value = 'test'; select.options[1].value = 'grok';
    select.value = state.contentProvider;
    select.onchange = () => { state.contentProvider = select.value; };
    bar.append(
      actionCopy('Content CEO', 'Decides whether verified facts justify a post. The evidence gate runs before any provider call.'),
      el('label', '', 'Provider', select),
      actionButton('Decide content', 'decide', () => post(
        `/api/events/${encodeURIComponent(state.eventId)}/content-decision`, {provider: state.contentProvider}, 'decide',
        state.contentProvider === 'grok' ? 'Applying evidence gate before live Content CEO…' : 'Applying deterministic Content CEO policy…',
        result => result.cached ? 'Inputs unchanged · cached decision reused' : result.duplicate ? 'Content decision already in progress' : 'Content decision queued',
      )),
      progressFor('decide', (data.content_decision_runs || []).find(isActive)),
    );
  } else if (state.tab === 'package') {
    const decisionId = data.production_gate?.content_decision_id;
    const regenerate = (data.production_jobs || []).some(job => Boolean(job.package));
    const claude = data.production_gate?.provider || {};
    bar.append(
      actionCopy('Content package', claude.live ? `Claude · live · ${claude.model} · one structured call · no publishing` : 'Claude production provider unavailable — add ANTHROPIC_API_KEY to .env. Packages are never generated with a fixture instead.'),
      actionButton(regenerate ? 'Regenerate package' : 'Generate package', 'package', async () => {
        if (!window.confirm(`This starts a paid Claude generation${regenerate ? ' for a new immutable package version' : ''}. Continue?`)) return;
        post(`/api/content-decisions/${encodeURIComponent(decisionId)}/production`, {
          provider: 'anthropic', regenerate, confirmed_paid_action: true, client_request_id: clientRequestId(),
        }, 'package',
          'Capturing immutable inputs before Claude…',
          result => result.cached ? 'Existing package version retained' : result.duplicate ? 'Production already in progress' : 'Production queued', 'package');
      }, {disabled: !data.production_gate?.eligible || !decisionId || !claude.live}),
      progressFor('package', (data.production_jobs || []).find(isActive)),
    );
  } else if (state.tab === 'media') {
    const gate = data.render_gate || {};
    const regenerate = (data.render_jobs || []).some(job => job.assets?.length);
    bar.append(
      actionCopy('Media', gate.live_renderer_configured ? `Live renderer: ${providerName(gate.configured_provider)} · paid call · human review required` : `${label(gate.renderer_configuration_status || 'LIVE_RENDERER_NOT_CONFIGURED')} · generation disabled`),
      actionButton(isVideo(gate.media_type) ? (regenerate ? 'Regenerate video' : 'Generate video') : (regenerate ? 'Regenerate media' : 'Generate media'), 'media', async () => {
        const video = isVideo(gate.media_type);
        const confirmed = await confirmPaidAction({
          title: video ? 'Confirm text-to-video generation' : 'Confirm image generation',
          warning: 'This starts a paid xAI generation. xAI may bill even if local polling later times out. Automated Claude visual QA may be a separate paid call when configured.',
          rows: [['Generation mode', video ? 'Text to video' : 'Image'], ['Provider / model', `${providerName(gate.configured_provider)} / ${gate.model || '—'}`],
            ['Aspect ratio', gate.aspect_ratio || 'From package'], ['Duration', video ? `${gate.duration_seconds || '—'}s` : undefined],
            ['Resolution', video ? gate.resolution || '—' : undefined], ['Package version', gate.content_package_version ? `v${gate.content_package_version}` : '—']].filter(([, value]) => value !== undefined),
          confirmText: regenerate ? 'Generate new version' : 'Start paid generation',
        });
        if (!confirmed) return;
        post(`/api/content-packages/${encodeURIComponent(gate.content_package_id)}/render`,
          {media_type: gate.media_type, provider: gate.configured_provider, regenerate,
            confirmed_paid_action: true, client_request_id: clientRequestId()}, 'media',
          'Rechecking package, evidence, and media rights…',
          result => result.cached ? 'Existing generated asset retained' : result.duplicate ? 'Rendering already in progress' : 'Render queued', 'media');
      }, {disabled: !gate.eligible || !gate.live_renderer_configured}),
      progressFor('media', (data.render_jobs || []).find(isActive)),
    );
    const reelSource = data.final_reel_source_asset_id;
    const newReel = actionButton('Create final Reel', 'final-reel', () => post(
      `/api/generated-assets/${encodeURIComponent(reelSource)}/final-reels`,
      {cbn_asset_id: state.reelInputs.cbn, tdp_asset_id: state.reelInputs.tdp}, 'final-reel',
      'Composing 9:16 Reel (narration + burned-in subtitles + quiet music)…',
      () => 'Final Reel composed — human review required', 'media',
    ), {primary: false, disabled: !reelSource, allowDuringActive: true});
    const reelCopy = actionCopy('Final Reel',
      reelSource ? `Compose a new 9:16 Reel from ${reelSource}: approved narration, burned-in subtitles, and quiet music. Never overwrites a prior version.`
        : 'Compose a Final Reel once a QA-passed generated video exists.');
    bar.append(reelCopy, newReel, progressFor('final-reel', null));
  }
}

/* ---------- tabs ---------- */

function renderOverview(data, root) {
  const run = (data.runs || []).find(item => item.summary);
  const summary = run?.summary;
  if (!summary) {
    root.append(empty('No research yet', 'Choose a provider above and run research to summarise this event from its linked evidence.'));
  } else {
    const whatHappened = card('', cardHead('What happened', modeLabel(run.mode), pill(data.event.research_status)), el('p', 'body-text', summary.what_happened || 'Unknown'));
    whatHappened.append(facts([
      ['Published', fmt(summary.publication_time)], ['Event time', fmt(summary.stated_event_time)],
      ['Occurrence', label(summary.occurrence_kind)], ['Relevance', label(summary.relevance)],
    ]));
    root.append(whatHappened);
    const issues = [];
    if (summary.unknowns?.length) issues.push(callout('warn', 'Missing information', bullets(summary.unknowns)));
    if (summary.contradictions?.length) issues.push(callout('bad', 'Contradictions', bullets(summary.contradictions.map(item => item.description))));
    if (issues.length) root.append(el('div', 'split', issues));
    if (run.verification_explanation) root.append(callout('info', 'Evidence policy', run.verification_explanation));
  }
  const event = data.event;
  root.append(card('', cardHead('Status'), facts([
    ['Event', label(event.status)], ['Research', label(event.research_status)], ['Verification', label(event.verification_status)],
    ['Content decision', label(event.content_decision_status)], ['Package', label(event.production_status)], ['Media', label(event.render_status)],
  ])));
}

function renderEvidence(data, root) {
  const signals = data.signals || [];
  const sources = card('', cardHead(`Sources (${signals.length})`));
  if (!signals.length) sources.append(el('p', 'secondary-text', 'No event evidence is linked.'));
  signals.forEach(signal => sources.append(el('div', 'evidence-item source-card',
    externalLink(signal.title, signal.canonical_url, 'source-title'),
    el('p', 'source-meta', `${signal.registered_source_name} · ${label(signal.source_class)} · published ${fmt(signal.publication_time)}`),
    el('div', 'excerpt', signal.text),
  )));
  root.append(sources);
  const claims = data.claims || [];
  if (!claims.length) { root.append(empty('No claims yet', 'Claims appear after research extracts them from evidence.')); return; }
  claims.forEach(claim => {
    const claimCard = card('', el('div', 'card-head', el('div', 'chip-row', pill(claim.verification_status), modeLabel(claim.research_mode), el('span', 'chip', label(claim.assertion_scope)))));
    claimCard.append(el('p', 'body-text', claim.text), el('p', 'secondary-text', claim.attribution ? `Attributed to ${claim.attribution}` : 'No attribution supplied'));
    (claim.evidence || []).forEach(evidence => claimCard.append(el('div', 'evidence-item',
      externalLink(evidence.source_name || evidence.source_url, evidence.source_url),
      el('blockquote', '', evidence.supporting_excerpt || 'No excerpt supplied'),
      el('small', '', `${label(evidence.support_kind)} · ${label(evidence.validation_status)} · snapshot ${evidence.retrieved_at ? fmt(evidence.retrieved_at) : 'unavailable'}`),
    )));
    if (claim.reviewer_notes) claimCard.append(el('p', 'secondary-text', claim.reviewer_notes));
    root.append(claimCard);
  });
}

function verificationCost(run) {
  if (run.cost_status === 'known' && run.cost_usd != null) return money(run.cost_usd);
  return run.cost_usd_ticks != null ? `${run.cost_usd_ticks} USD ticks` : 'Unknown';
}

function verificationCard(run) {
  const visibleStatus = run.resume_state || run.status;
  const runCard = card('', cardHead(`${run.provider} · ${run.model}`, modeLabel(run.mode), pill(visibleStatus), run.summary?.claim_set_status ? pill(run.summary.claim_set_status, `Claim set ${label(run.summary.claim_set_status).toLowerCase()}`) : null));
  if (run.recoverable) runCard.append(callout('warn', 'Provider timed out', 'Verification paused because the evidence provider timed out. Previously retrieved evidence and completed checkpoints are preserved.'));
  else if ((run.decisions || []).some(item => item.decision === 'CONFLICTED')) runCard.append(callout('bad', 'Contradictory evidence', 'One or more claims have claim-specific conflicting evidence.'));
  else if (run.status === 'COMPLETED' && run.summary?.claim_set_status === 'REVIEW_REQUIRED') runCard.append(callout('warn', 'Needs more evidence', 'Verification completed, but the unchanged evidence policy was not satisfied.'));
  else if (run.status === 'FAILED') runCard.append(callout('bad', 'Verification failed', run.error_message || 'The verification provider failed.'));
  else if (run.status === 'COMPLETED') runCard.append(callout('good', 'Verification completed', run.decision_explanation || 'Claim adjudication completed.'));
  if (run.decision_explanation) runCard.append(el('p', 'body-text', run.decision_explanation));
  if (run.error_message && !run.recoverable && run.status !== 'FAILED') runCard.append(callout('bad', 'Run failed', run.error_message));
  runCard.append(facts([
    ['Searches', String(run.actual_search_calls ?? 'Unknown')], ['Pages opened', String(run.actual_open_calls ?? 'Unknown')],
    ['Sources returned', String(run.actual_sources_returned ?? 'Unknown')], ['Tokens', String(run.total_tokens ?? 'Unknown')],
    ['Cost', verificationCost(run)], ['Phase', label(run.current_phase)], ['Attempts', String(run.attempts?.length || 0)],
    ['Requested', fmt(run.requested_at)],
  ]));
  if (run.attempts?.length) runCard.append(disclosure(`Attempt audit (${run.attempts.length})`, run.attempts.map(attempt =>
    el('div', 'evidence-item', el('strong', '', `Attempt ${attempt.attempt_number} · ${label(attempt.status)}`),
      el('small', '', `${attempt.provider} · ${attempt.model} · ${label(attempt.failure_category || attempt.phase)} · cost ${attempt.cost_status === 'known' ? money(attempt.cost_usd) : label(attempt.cost_status)}`))
  )));
  if (run.checkpoints?.length) runCard.append(disclosure(`Checkpoints (${run.checkpoints.length})`, bullets(run.checkpoints.map(item => `${label(item.phase)} · ${label(item.status)}`))));
  return runCard;
}

function renderVerification(data, root) {
  const runs = data.verification_runs || [];
  if (!runs.length) { root.append(empty('No verification yet', 'Complete research, then find corroboration for each claim.')); return; }
  const [current, ...older] = runs;
  root.append(verificationCard(current));
  const acquisitionRuns = data.source_acquisition_runs || [];
  const attempts = acquisitionRuns.flatMap(run => run.attempts || []);
  const candidates = acquisitionRuns.flatMap(run => run.candidates || []);
  const packets = acquisitionRuns.flatMap(run => run.packets || []);
  const requiredDecisions = (current.decisions || []).filter(item => item.required_for_event);
  const unresolved = requiredDecisions.filter(item => item.decision !== 'SUPPORTED');
  const discovery = card('', cardHead('Evidence discovery', plainPill(`${(data.official_source_registry || []).filter(item => item.enabled).length} official authorities`, 'info')));
  if (!requiredDecisions.length) {
    discovery.append(callout('warn', 'Evidence still insufficient', 'No final claim decisions exist for this run yet. Discovery candidates and checkpoints remain available for review.'));
  } else if (!unresolved.length) {
    discovery.append(callout('good', 'Evidence sufficient', 'No required claim in this run remains unresolved.'));
  } else {
    unresolved.forEach(decision => {
      const claimCandidates = candidates.filter(candidate => (candidate.claim_matches || []).some(match => match.claim_version_id === decision.claim_version_id));
      const matched = claimCandidates.filter(candidate => candidate.state === 'RETRIEVED' && (candidate.claim_matches || []).some(match => match.claim_version_id === decision.claim_version_id && match.relationship === 'CANDIDATE'));
      const official = matched.filter(item => item.source_class === 'OFFICIAL_PRIMARY');
      const independent = new Set(matched.filter(item => item.source_class === 'INDEPENDENT_REPORTING').map(item => item.evidence_family_id));
      const unavailable = claimCandidates.filter(item => item.state === 'UNAVAILABLE');
      const duplicates = matched.filter(item => /same|syndicat|copied/i.test(item.family_reason || ''));
      const claimAttempts = attempts.filter(item => (item.target_claim_ids || []).includes(decision.claim_version_id));
      const completedStrategies = [...new Set(claimAttempts.filter(item => item.status === 'COMPLETED').map(item => item.strategy))];
      const plannedStrategies = [...new Set(claimAttempts.filter(item => item.status === 'PLANNED').map(item => item.strategy))];
      const packet = packets.find(item => item.claim_version_id === decision.claim_version_id);
      const stateCode = packet?.deterministically_sufficient ? 'SUPPORTED' : matched.length ? 'RUNNING' : completedStrategies.length ? 'INSUFFICIENT_EVIDENCE' : 'REVIEW_REQUIRED';
      const stateText = packet?.deterministically_sufficient ? 'Evidence sufficient' : matched.length ? 'Candidates found' : completedStrategies.length ? 'No additional candidates' : 'Evidence still insufficient';
      discovery.append(el('div', 'evidence-item discovery-claim',
        el('div', 'chip-row', pill(stateCode, stateText), el('span', 'ref', decision.claim_id)),
        el('p', 'body-text', decision.claim_text),
        facts([
          ['Required condition', packet?.packet?.required_condition || 'Explicit official primary support or two independent reporting families.'],
          ['Current supporting families', String(decision.independent_family_count || 0)],
          ['Official sources found', String(official.length)], ['Independent families found', String(independent.size)],
          ['Rejected duplicates', String(duplicates.length)], ['Unavailable pages', String(unavailable.length)],
          ['Strategies attempted', completedStrategies.length ? completedStrategies.map(label).join(' · ') : 'None'],
          ['Strategies planned', plannedStrategies.length ? plannedStrategies.map(label).join(' · ') : 'None'],
        ]),
      ));
    });
  }
  discovery.append(el('p', 'secondary-text', 'Candidate classification and packet sufficiency never approve a claim. A fresh verification run applies the unchanged evidence policy.'));
  root.append(discovery);
  (current.decisions || []).forEach(decision => {
    const decisionCard = card('', el('div', 'card-head', el('div', 'chip-row', pill(decision.decision), el('span', 'chip', decision.required_for_event ? 'Required claim' : 'Optional claim'), el('span', 'chip', `v${decision.claim_version}`))));
    decisionCard.append(el('p', 'body-text', decision.claim_text), el('p', 'secondary-text', decision.rationale));
    (decision.evidence || []).forEach(evidence => decisionCard.append(el('div', 'evidence-item',
      externalLink(evidence.source_name, evidence.canonical_url),
      el('blockquote', '', evidence.excerpt || 'No matching excerpt'),
      el('small', '', `${label(evidence.relationship)} · ${label(evidence.directness)} · published ${fmt(evidence.publication_time)}`),
    )));
    if (decision.missing_information?.length) decisionCard.append(callout('warn', 'Still missing', bullets(decision.missing_information)));
    root.append(decisionCard);
  });
  if (current.leads?.length) root.append(disclosure(`Discovery leads (${current.leads.length})`, bullets(current.leads.map(lead => `${label(lead.status)} · ${lead.url} · ${lead.status_reason}`))));
  if (current.limit_notes) root.append(disclosure('Search limits', el('p', 'secondary-text', current.limit_notes)));
  if (older.length) root.append(disclosure(`Earlier runs (${older.length})`, older.map(verificationCard)));
}

function decisionCard(run, prominent) {
  const record = run.decision_record;
  const head = record
    ? cardHead(prominent ? 'Content decision' : `${label(record.decision)} · ${fmt(run.requested_at)}`, pill(record.decision), el('span', 'chip', label(record.recommended_format)), modeLabel(run.mode))
    : cardHead(`${label(run.status)} · ${fmt(run.requested_at)}`, pill(run.status), modeLabel(run.mode));
  const decision = card('', head);
  if (record) {
    decision.append(el('p', prominent ? 'body-text' : 'secondary-text', record.factual_rationale));
    if (record.test_only) decision.append(callout('warn', 'Test decision', 'Deterministic test decision. It cannot start production work.'));
    if (record.missing_evidence_or_media?.length) decision.append(callout('warn', 'Blockers', bullets(record.missing_evidence_or_media)));
    if (prominent) decision.append(facts([
      ['Executable', record.executable ? 'Yes' : 'No'], ['Language', record.language], ['Duration', `${record.proposed_duration_seconds}s`],
      ['Priority', label(record.priority)], ['Provider', `${run.provider} · ${run.model}`],
      ['Cost', run.cost_status === 'known' && run.cost_usd != null ? money(run.cost_usd) : 'Unknown'],
    ]));
  } else if (run.progress_message) decision.append(el('p', 'secondary-text', run.progress_message));
  if (run.error_message) decision.append(callout('bad', 'Run failed', run.error_message));
  return decision;
}

function renderDecision(data, root) {
  const runs = data.content_decision_runs || [];
  if (!runs.length) { root.append(empty('No content decision yet', 'Run the Content CEO once the event is verified.')); return; }
  const [current, ...older] = runs;
  root.append(decisionCard(current, true));
  if (older.length) root.append(disclosure(`Earlier decisions (${older.length})`, older.map(run => decisionCard(run, false))));
}

function gateCallout(gate, eligibleText, blockedTitle) {
  if (gate.eligible) return callout('good', 'Eligible', eligibleText);
  return callout('warn', blockedTitle, bullets(gate.blockers || ['Gate unavailable.']));
}

function packageDocument(job) {
  const pkg = job.package.package;
  const doc = el('div', 'doc');
  const block = (name, value) => el('div', 'doc-block', el('div', 'doc-label', name), el('p', '', value.text), refs(value.claim_version_ids));
  doc.append(
    el('div', '', el('div', 'doc-label', 'Headline'), el('div', 'doc-headline', pkg.headline.text), refs(pkg.headline.claim_version_ids)),
    el('div', 'split', block('Hook', pkg.hook), block('Caption', pkg.caption)),
    el('div', '', el('div', 'doc-label', 'Script'), el('ol', 'script-list', pkg.script.map(line => el('li', '', line.text, refs(line.claim_version_ids))))),
    el('div', '', el('div', 'doc-label', 'Storyboard'), el('div', 'scene-grid', pkg.storyboard.map(scene => el('div', 'scene',
      el('span', 'scene-num', `Scene ${scene.scene_number} · ${scene.duration_seconds}s`), el('span', '', scene.narration),
      el('span', 'secondary-text', scene.visual_prompt), refs(scene.claim_version_ids))))),
  );
  const brief = pkg.media_brief;
  if (brief) {
    doc.append(el('div', 'scene', el('span', 'scene-num', `Media brief · ${label(brief.media_type)} · ${pkg.platform_metadata?.aspect_ratio || '—'} · ${pkg.platform_metadata?.duration_seconds ?? '—'}s`),
      el('span', '', brief.generation_prompt), el('span', 'secondary-text', brief.visual_brief),
      brief.negative_constraints?.length ? el('span', 'secondary-text', `Avoid: ${brief.negative_constraints.join(' · ')}`) : null,
      brief.factual_constraints?.length ? el('span', 'secondary-text', `Factual limits: ${brief.factual_constraints.join(' · ')}`) : null));
  }
  return doc;
}

function renderPackage(data, root) {
  root.append(gateCallout(data.production_gate || {}, 'Executable CREATE decision, approved claims, evidence snapshots, and rights-cleared media passed the entry gate.', "Can't generate a new package yet"));
  const jobs = data.production_jobs || [];
  if (!jobs.length) { root.append(empty('No content package yet', 'Claude is called only after you explicitly generate a package.')); return; }
  const current = jobs.find(job => job.package && job.status === 'READY_FOR_APPROVAL') || jobs.find(job => job.package) || jobs[0];
  const cost = current.cost_status === 'known' && current.cost_usd != null ? money(current.cost_usd) : 'Unknown';
  const main = card('', cardHead(current.package?.package?.story_angle || `Package attempt v${current.regeneration_number}`,
    pill(current.status), productionLabel(current), el('span', 'chip', `v${current.regeneration_number}`)));
  if (current.fixture_only) main.append(callout('warn', 'Fixture · demo package', 'Produced by the controlled demo provider, not a live Claude call. Live production never falls back to it.'));
  if (current.error_message) main.append(callout('bad', 'Generation failed', current.error_message));
  if (current.package?.package) {
    main.append(el('p', 'secondary-text', current.package.package.content_objective), packageDocument(current));
  }
  main.append(el('div', '', facts([
    ['Provider', `${current.provider} · ${current.model}`], ['Validation', label(current.validation_status)],
    ['Tokens', String(current.total_tokens ?? 'Unknown')], ['Cost', cost], ['Claim set', `v${current.approved_claim_set_version}`],
    ['Created', fmt(current.requested_at)],
  ])));
  root.append(main);
  const details = [];
  if (current.package) details.push(facts([
    ['Claim versions', current.package.approved_claim_version_ids.join(', ')], ['Evidence snapshots', current.package.evidence_snapshot_ids.join(', ')],
    ['Evidence version', current.evidence_version?.slice(0, 16)], ['Media version', current.media_version?.slice(0, 16)], ['Schema', current.prompt_schema_version],
    ['Request snapshot', current.request_snapshot_hash ? `${current.request_snapshot_hash.slice(0, 16)}…` : 'Not stored (pre-06D)'], ['Provider request', current.provider_request_id],
  ]));
  if (current.history?.length) details.push(timeline(current.history.map(item => ({at: item.changed_at, title: label(item.to_status), detail: item.message, tone: tone(item.to_status)}))));
  if (details.length) root.append(disclosure('Technical details', details));
  const others = jobs.filter(job => job !== current);
  if (others.length) root.append(disclosure(`Other attempts (${others.length})`, others.map(job => el('div', 'evidence-item',
    el('div', 'chip-row', pill(job.status), productionLabel(job), el('span', 'chip', `v${job.regeneration_number}`), el('span', 'muted', fmt(job.requested_at))),
    job.error_message ? el('p', 'secondary-text', job.error_message) : null,
  ))));
}

function qaRow(name, status, note) {
  const kind = ['PASSED', 'PASS', 'VALIDATED', 'APPROVED'].includes(status) ? 'good' : ['FAILED', 'FLAG', 'FLAGGED', 'REJECTED'].includes(status) ? 'bad' : ['REQUIRED', 'CHANGES_REQUIRED', 'UNKNOWN'].includes(status) ? 'warn' : '';
  const icon = {good: '✓', bad: '×', warn: '!'}[kind] || '–';
  return el('li', '', el('span', `qa-icon ${kind}`, icon), el('div', '', el('div', 'qa-name', name), note ? el('div', 'qa-note', note) : null), pill(status, status === 'REQUIRED' ? 'Required' : label(status)));
}

function renderCostSummary(data) {
  const summary = data.cost_summary;
  if (!summary) return null;
  const max = Math.max(0.000001, ...summary.stages.map(stage => stage.known_cost_usd || 0));
  const costCard = card('', cardHead('Story production cost', plainPill(`${money(summary.known_total_usd)} known`, summary.total_status === 'complete' ? 'good' : 'warn')));
  costCard.append(el('div', 'cost-bars', summary.stages.map(stage => {
    const bar = el('div', 'bar', el('i'));
    bar.firstChild.style.width = `${Math.round(((stage.known_cost_usd || 0) / max) * 100)}%`;
    const value = !stage.live_runs ? 'No live runs' : `${stage.known_cost_usd != null ? money(stage.known_cost_usd) : '—'}${stage.unknown_cost_runs ? ` · ${stage.unknown_cost_runs} unknown` : ''}`;
    return el('div', 'cost-row', el('span', '', label(stage.stage)), bar, el('span', 'val', value));
  })));
  if (summary.unknown_cost_runs) costCard.append(el('p', 'secondary-text', 'Unknown provider costs are excluded from the total, never counted as zero.'));
  return costCard;
}

function openLightbox(src) {
  const box = document.getElementById('lightbox');
  box.querySelector('img').src = src;
  box.hidden = false;
}
document.getElementById('lightbox').onclick = () => { document.getElementById('lightbox').hidden = true; };

function reelReviewActions(reel) {
  const actions = el('div', 'review-actions');
  for (const action of ['APPROVED', 'CHANGES_REQUIRED', 'REJECTED']) {
    const key = `reel-review-${reel.id}-${action}`;
    actions.append(actionButton(label(action), key, () => {
      const reviewer = window.prompt('Reviewer name for this Final Reel'); if (!reviewer?.trim()) return;
      const comment = window.prompt('Optional review comment') || '';
      post(`/api/final-reels/${encodeURIComponent(reel.id)}/review`, {action, reviewer, comment}, key,
        'Recording immutable Final Reel review…',
        result => `${label(result.asset.latest_review.action)} for Final Reel ${result.asset.id}`, 'media');
    }, {primary: action === 'APPROVED', allowDuringActive: true, disabled: action === 'APPROVED' && reel.status !== 'READY_FOR_REVIEW'}));
  }
  return actions;
}

function finalReelCard(reel, {current = false} = {}) {
  const src = `/api/final-reels/${encodeURIComponent(reel.id)}/content`;
  const frame = el('div', 'preview-frame');
  const video = el('video');
  video.controls = true; video.playsInline = true; video.preload = 'metadata'; video.src = src; video.className = 'preview-video';
  frame.append(video, el('div', 'preview-badge', plainPill('Final Reel · 9:16 composited', 'good')));
  if (current) frame.append(el('div', 'preview-actions reel-current-tag', plainPill('Current', 'good')));
  frame.append(el('div', 'preview-actions', externalLink('Open full size', src, 'btn ghost')));
  const left = el('div', '', frame,
    el('div', 'preview-caption', `${reel.width}×${reel.height} · ${reel.duration_seconds}s · ${reel.mime_type} · ${bytes(reel.file_size)} · ${label(reel.status)}`),
    el('div', 'doc', el('div', '', el('div', 'doc-label', 'Narration (approved package text only)'),
      el('p', 'body-text pre-wrap', reel.narration_text))));

  const technical = reel.technical_qa || {};
  const subtitles = reel.subtitle_qa || {};
  const audio = reel.audio_qa || {};
  const factual = reel.factual_qa || {};
  const instagram = reel.instagram_compatibility || {};
  const facebook = reel.facebook_compatibility || {};
  const review = reel.latest_review;

  const side = card('', cardHead(reel.id, pill(reel.status), plainPill(`Voice · ${reel.voice_model}`, '')));
  side.append(review?.action === 'APPROVED'
    ? callout('good', 'Final Reel approved', 'Approval binds to this exact immutable version only. It never publishes.')
    : callout('info', 'Human review required', 'Ready for review is not approval. Distribution uses the approved Final Reel when one exists.'));
  side.append(el('ul', 'qa-list',
    qaRow('Technical QA', technical.status || 'UNKNOWN', (technical.errors || []).join(' ')
      || `${reel.width}×${reel.height} · ${reel.duration_seconds}s · ${reel.codec || '—'}${reel.has_audio ? ` + ${reel.audio_codec || 'audio'}` : ''}`),
    qaRow('Subtitle QA', subtitles.status || 'UNKNOWN', subtitles.burned_in
      ? `Burned-in · ${(subtitles.checks || []).length} cue(s) · matches narration` : 'Not burned in'),
    qaRow('Audio QA', audio.status || 'UNKNOWN', audio.status
      ? `music ${audio.music_volume ?? '—'} below speech ${audio.speech_volume ?? '—'}` : 'Audio status unavailable'),
    qaRow('Factual QA', factual.status || 'UNKNOWN', factual.no_new_factual_claims
      ? 'No new factual claims' : 'Factual check flagged'),
    qaRow('Instagram compatibility', instagram.compliant ? 'PASS' : 'FLAG', (instagram.errors || []).join(' ') || 'Reels specs · no edit lists'),
    qaRow('Facebook compatibility', facebook.compliant ? 'PASS' : 'FLAG', (facebook.errors || []).join(' ') || 'Reels specs'),
    qaRow('Human review', review?.action || 'REQUIRED', review
      ? `${review.reviewer} · ${fmt(review.created_at)}${review.comment ? ` · ${review.comment}` : ''}`
      : 'Required for this exact Final Reel version'),
  ));
  side.append(el('div', 'review-workflow', el('strong', '', 'Final Reel review'), reelReviewActions(reel),
    el('span', 'secondary-text', 'Approval is per exact Final Reel version and is never inherited.')));
  const figureQa = reel.public_figure_qa || {};
  side.append(facts([
    ['Source video', `${reel.source_asset_id} v${reel.source_asset_version}`],
    ['CBN asset', reel.cbn_asset_id || 'Not included'],
    ['TDP asset', reel.tdp_asset_id || 'Not included'],
    ['Public-figure QA', figureQa.status || 'UNKNOWN'],
    ['Source render job', reel.source_render_job_id],
    ['Content package', `${reel.content_package_id} v${reel.content_package_version}`],
    ['Voice', `${reel.voice_provider} · ${reel.voice_model}`],
    ['Cost', reel.cost_status === 'known' ? money(reel.cost_usd) : label(reel.cost_status)],
    ['Created', fmt(reel.created_at)],
  ]));
  if (figureQa.figures?.length) side.append(callout('info', 'Contextual figures',
    bullets(figureQa.figures.map(figure => `${figure.asset_id} · ${label(figure.role)} · rights ${figure.rights_status}`)),
    el('p', 'secondary-text', figureQa.disclaimer || '')));

  const details = [facts([
    ['Final Reel', reel.id], ['Transform hash', `${(reel.transform_hash || '').slice(0, 24)}…`],
    ['Checksum', reel.checksum_sha256], ['Storage', reel.storage_uri],
    ['Source checksum', reel.source_asset_checksum_sha256],
    ['Frame rate', reel.frame_rate ? `${reel.frame_rate} fps` : '—'],
    ['Audio', reel.has_audio ? (reel.audio_codec || 'yes') : 'no'],
    ['Policy', (reel.transform_manifest || {}).policy_version || '—'],
  ])];
  if (reel.subtitle_qa?.checks?.length) details.push(el('h3', '', 'Subtitle OCR checks'), bullets(
    reel.subtitle_qa.checks.map(check => `“${check.cue || check.text}” · coverage ${check.token_coverage}`)));
  if (reel.public_figure_qa && reel.public_figure_qa.status) details.push(el('h3', '', 'Public-figure context QA'), facts([
    ['Status', label(reel.public_figure_qa.status)],
    ['Neutral labels', (reel.public_figure_qa.neutral_labels || []).join(' · ') || 'None'],
    ['Reason', reel.public_figure_qa.reason || '—'],
  ]), reel.public_figure_qa.errors?.length ? bullets(reel.public_figure_qa.errors) : null);
  if (reel.composition_manifest?.segments?.length) details.push(el('h3', '', 'Composition plan'), bullets(
    reel.composition_manifest.segments.map(segment => `${label(segment.kind)}${segment.start != null ? ` · ${segment.start}–${segment.end ?? 'end'}s` : ''}${segment.headline ? ` · ${segment.headline}` : ''}${segment.motion ? ` · ${segment.motion}` : ''}`)));
  if (reel.instagram_compatibility || reel.facebook_compatibility) details.push(facts([
    ['Spec version', instagram.spec_api_version || facebook.spec_api_version || '—'],
  ]));
  if (reel.reviews?.length) details.push(el('h3', '', 'Review history'), timeline(reel.reviews.map(item => ({
    at: item.created_at, title: label(item.action),
    detail: `${item.reviewer}${item.comment ? ` · ${item.comment}` : ''}`, tone: tone(item.action),
  }))));
  return card(`reel-card${current ? ' reel-current' : ''}`, el('div', 'media-layout', left, el('div', '', side, disclosure('Lineage & technical details', details))));
}

function uploadReferenceMedia(kind, assetType, form, status) {
  const fileInput = form.querySelector('input[type=file]');
  const fields = name => form.querySelector(`[name=${name}]`)?.value.trim() || '';
  const file = fileInput?.files?.[0];
  if (!file) { status.textContent = 'Choose an image file first.'; return; }
  const rights = fields('rights_status') || 'VERIFIED';
  if (!fields('label') || !fields('source_name') || !fields('license_note') || !fields('uploader')) {
    status.textContent = 'Label, source, license note, and uploader are required.';
    return;
  }
  status.textContent = 'Uploading…';
  const reader = new FileReader();
  reader.onload = () => {
    fetch('/api/uploads', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        data: reader.result, filename: file.name, asset_type: assetType, label: fields('label'),
        source_name: fields('source_name'), source_url: fields('source_url'),
        license_note: fields('license_note'), rights_status: rights, uploader: fields('uploader'),
        reviewer: fields('reviewer'), identity_subject: fields('identity_subject'),
      }),
    }).then(response => response.json().then(result => ({ok: response.ok, result})))
      .then(({ok, result}) => {
        if (!ok) throw new Error(result.error || 'Upload failed');
        status.textContent = result.duplicate ? `Already registered · ${result.asset.id}` : `Registered ${result.asset.id} · ${label(result.asset.rights_status)}`;
        loadRoom();
      })
      .catch(error => { status.textContent = error.message; });
  };
  reader.onerror = () => { status.textContent = 'The file could not be read.'; };
  reader.readAsDataURL(file);
}

function referenceMediaPanel(data) {
  const ref = data.reference_media || {cbn_options: [], tdp_options: [], all: []};
  const noneOption = () => el('option', '', 'Not included');
  const panel = card('', cardHead('Reference media', plainPill('Rights-verified only', 'good')));
  panel.append(el('p', 'secondary-text', 'Upload a rights-cleared N. Chandrababu Naidu portrait and/or party logo, mark provenance and usage rights, then select them for the next Final Reel. Unverified assets can never enter a Reel.'));

  for (const [kind, assetType, heading] of [['cbn', 'PUBLIC_FIGURE_PHOTO', 'CBN portrait'], ['tdp', 'PARTY_LOGO', 'Party logo']]) {
    const choices = kind === 'cbn' ? (ref.cbn_options || []) : (ref.tdp_options || []);
    const block = el('div', 'reference-block', el('h3', '', heading));
    const select = el('select', '');
    select.append(noneOption());
    choices.forEach(asset => {
      const option = el('option', '', `${asset.label} · ${asset.id} · ${asset.rights_status}`);
      option.value = asset.id;
      select.append(option);
    });
    select.value = state.reelInputs[kind] || '';
    select.onchange = () => { state.reelInputs[kind] = select.value || null; renderRoom(); };
    const img = el('img', 'reference-thumb');
    const selected = choices.find(asset => asset.id === state.reelInputs[kind]);
    if (selected) { img.src = `/api/uploads/${encodeURIComponent(selected.id)}/content`; img.alt = selected.label; }
    else img.hidden = true;
    block.append(el('div', 'reference-select', el('label', '', 'Selected asset', select), img));
    if (selected) block.append(facts([
      ['Source', selected.source_name], ['License', selected.license_note],
      ['Checksum', `${selected.checksum_sha256.slice(0, 16)}…`],
    ]));

    const form = el('form', 'reference-upload');
    const text = (name, placeholder, required = true) => {
      const input = el('input'); input.name = name; input.placeholder = placeholder; input.required = required; return input;
    };
    const fileInput = el('input'); fileInput.type = 'file'; fileInput.accept = 'image/jpeg,image/png,image/webp';
    const rights = el('select'); rights.name = 'rights_status';
    for (const value of ['VERIFIED', 'RESTRICTED', 'UNKNOWN']) { const option = el('option', '', label(value)); option.value = value; rights.append(option); }
    const status = el('p', 'secondary-text');
    form.append(el('div', 'reference-grid', fileInput, text('label', 'Label'), text('source_name', 'Source name'),
      text('source_url', 'Source URL (optional)', false), text('license_note', 'License / permission note'),
      text('uploader', 'Uploader'), text('reviewer', 'Reviewer (optional)', false), rights));
    const submit = el('button', 'btn', `Upload ${heading}`); submit.type = 'button';
    submit.onclick = () => uploadReferenceMedia(kind, assetType, form, status);
    form.append(submit, status);
    block.append(form);
    panel.append(block);
  }
  return panel;
}

function renderFinalReels(data, root) {
  const reels = data.final_reels || [];
  const source = data.final_reel_source_asset_id;
  const section = el('div', 'final-reel-section');
  const head = el('div', 'reel-section-head', el('h2', '', 'Final Reels'),
    source ? plainPill(`Source ready · ${source}`, 'good') : plainPill('No eligible source', 'warn'));
  section.append(head);
  section.append(referenceMediaPanel(data));
  if (!reels.length) {
    section.append(empty('No Final Reel yet', 'Create a 9:16 Reel from a QA-passed generated video. Every composition is a new immutable version.'));
    root.append(section);
    return;
  }
  // Show only the newest version; older runs stay immutable but are collapsed.
  const [current, ...older] = reels;
  section.append(finalReelCard(current, {current: true}));
  if (older.length) {
    const rows = older.map(reel => el('div', 'reel-history-row',
      pill(reel.status), el('span', 'ref', reel.id), el('span', 'secondary-text',
        `${reel.width}×${reel.height} · ${Math.round(reel.duration_seconds)}s · ${label(reel.latest_review?.action || 'REQUIRED')}`),
      el('span', 'muted', fmt(reel.created_at))));
    section.append(disclosure(`Previous versions (${older.length}) — kept for audit`, rows));
  }
  root.append(section);
}

function renderMedia(data, root) {
  const gate = data.render_gate || {};
  const notices = [];
  if (!gate.live_renderer_configured) {
    const howTo = el('details', 'inline', el('summary', '', 'How to enable real images'),
      el('ol', '', el('li', '', 'Add ', el('code', '', 'RENDERER_PROVIDER_IMAGE=xai'), ' to .env'),
        el('li', '', 'Keep ', el('code', '', 'XAI_API_KEY'), ' set (or add a dedicated ', el('code', '', 'LIVE_RENDERER_API_KEY'), ')'), el('li', '', 'Restart the server')),
      el('p', '', 'Each render is a paid call and still requires human review.'));
    notices.push(callout('info', 'Image renderer not configured',
      el('p', '', 'Images here are deterministic fixture placeholders used to test storage and QA — not real content.'), howTo));
  }
  if (!gate.eligible && gate.blockers?.length) notices.push(callout('warn', "Can't generate new media yet", bullets(gate.blockers)));

  const jobs = data.render_jobs || [];
  const resumable = jobs.filter(job => job.resume_state === 'PROVIDER_PENDING');
  if (resumable.length) {
    const job = resumable[0];
    root.append(callout('warn', 'Provider job still running',
      el('p', '', `${job.id} stopped polling locally, but its saved provider job ${job.provider_job_id} will be reused. No replacement generation will be submitted.`),
      actionButton('Resume status check', `resume-${job.id}`, () => post(
        `/api/render-jobs/${encodeURIComponent(job.id)}/resume`, {}, `resume-${job.id}`, 'Checking the saved provider job…',
        result => result.job?.resume_state === 'PROVIDER_PENDING' ? 'Provider is still processing' : `Status: ${label(result.job?.status)}`, 'media',
      ), {primary: false, allowDuringActive: true}),
      progressFor(`resume-${job.id}`, null)));
  }
  const assets = jobs.flatMap(job => (job.assets || []).map(asset => ({asset, job}))).sort((a, b) => (b.asset.created_at || '').localeCompare(a.asset.created_at || ''));
  if (!assets.length) {
    root.append(...notices, empty('No generated media yet', 'Rendering never starts automatically. Generate media once a package is ready and a renderer is configured.'));
  } else {
    if (!assets.some(item => item.asset.id === state.selectedAssetId)) {
      state.selectedAssetId = (assets.find(item => item.asset.current_for_review) || assets[0]).asset.id;
    }
    const {asset, job} = assets.find(item => item.asset.id === state.selectedAssetId);
    const src = `/api/generated-assets/${encodeURIComponent(asset.id)}/content`;

    const frame = el('div', 'preview-frame');
    if (asset.media_type === 'IMAGE') {
      const image = el('img'); image.src = src; image.alt = 'Generated media preview'; image.onclick = () => openLightbox(src);
      frame.append(image);
    } else if (isVideo(asset.media_type)) {
      const video = el('video'); video.controls = true; video.playsInline = true; video.preload = 'metadata'; video.src = src;
      video.className = 'preview-video';
      frame.append(video);
    } else {
      const audio = el('audio'); audio.controls = true; audio.src = src; frame.append(audio);
    }
    frame.append(el('div', 'preview-badge', asset.fixture_only ? plainPill('Fixture placeholder · not AI generated', 'warn') : plainPill(`${providerName(asset.provider)} · AI generated`, 'good')));
    const open = externalLink('Open full size', src, 'btn ghost'); frame.append(el('div', 'preview-actions', open));

    const left = el('div', '', frame, el('div', 'preview-caption', `${label(asset.media_type)} · ${asset.width || '—'}×${asset.height || '—'}${asset.duration_seconds ? ` · ${asset.duration_seconds}s` : ''} · ${asset.mime_type} · ${bytes(asset.file_size)} · version ${asset.version_number}`));
    if (assets.length > 1) {
      left.append(el('div', 'versions', assets.map(item => {
        const thumb = el('button', `version-thumb ${item.asset.id === state.selectedAssetId ? 'selected' : ''}`);
        thumb.type = 'button'; thumb.title = `${item.asset.id} · ${label(item.job.status)}`;
        if (item.asset.media_type === 'IMAGE') { const img = el('img'); img.src = `/api/generated-assets/${encodeURIComponent(item.asset.id)}/content`; img.alt = ''; thumb.append(img); }
        else thumb.append(el('div', 'video-tile', '▶'));
        thumb.append(el('span', '', `${isVideo(item.asset.media_type) ? 'Video ' : ''}v${item.asset.version_number}`));
        thumb.onclick = () => { state.selectedAssetId = item.asset.id; renderRoom(); };
        return thumb;
      })));
    }

    const cost = job.cost_status === 'known' ? money(job.provider_cost_usd ?? job.calculated_cost_usd) : 'Unknown';
    const side = card('', cardHead('Review', pill(job.status), job.fixture_only ? plainPill('Fixture provider', 'warn') : plainPill(`Live · ${providerName(job.provider)}`, 'good')));
    side.append(asset.current_for_review
      ? callout('info', 'Human review required', 'Ready for review is not approval to publish. There is no publish action in this build.')
      : callout('warn', 'Not current', (asset.currency_reasons || []).join(' ') || 'This asset failed media QA.'));
    const latestQa = kind => (asset.qa_runs || []).find(item => item.qa_kind === kind);
    const technicalQa = latestQa('TECHNICAL');
    const ocrQa = latestQa('OCR');
    const visualQa = latestQa('VISUAL');
    const review = asset.latest_review;
    side.append(el('ul', 'qa-list',
      qaRow('Technical QA', technicalQa?.status || 'UNKNOWN', technicalQa?.explanation || (isVideo(asset.media_type) ? 'Container, duration, dimensions, aspect ratio, checksum, storage, and lineage' : 'File, dimensions, checksum, storage, and lineage')),
      qaRow('OCR QA', ocrQa?.status || 'UNKNOWN', ocrQa?.evidence?.ocr_performed ? `${ocrQa.evidence.detections?.length || 0} unique text detection(s) · run v${ocrQa.run_number}` : ocrQa?.explanation || 'OCR result unavailable'),
      qaRow('Visual QA', visualQa?.status || 'UNKNOWN', visualQa?.explanation || `${visualQa?.provider ? providerName(visualQa.provider) : 'No provider'} · latest run ${visualQa?.run_number || '—'}`),
      qaRow('Human review', review?.action || 'REQUIRED', review ? `${review.reviewer} · ${fmt(review.created_at)}${review.comment ? ` · ${review.comment}` : ''}` : 'Required for this exact asset version'),
    ));
    const warnings = [...(ocrQa?.checks || []), ...(visualQa?.checks || [])].filter(check => check.status === 'FLAG');
    if (warnings.length) side.append(callout('warn', 'Automated QA flagged this version', bullets(warnings.map(item => item.label || item.check))));
    const reviewActions = el('div', 'review-actions');
    for (const action of ['APPROVED', 'CHANGES_REQUIRED', 'REJECTED']) {
      const key = `review-${asset.id}-${action}`;
      reviewActions.append(actionButton(label(action), key, () => {
        const reviewer = window.prompt('Reviewer name'); if (!reviewer?.trim()) return;
        const comment = window.prompt('Optional review comment') || '';
        post(`/api/generated-assets/${encodeURIComponent(asset.id)}/review`, {action, reviewer, comment}, key,
          'Recording immutable review…', result => `${label(result.review.action)} for media version ${result.review.asset_version}`, 'media');
      }, {primary: action === 'APPROVED', allowDuringActive: true}));
    }
    side.append(el('div', 'review-workflow', el('strong', '', 'Asset review'), reviewActions,
      el('span', 'secondary-text', 'Approved means approved media asset only. It never authorizes publication.')));
    const requestedRatio = asset.provider_metadata?.provider_request?.aspect_ratio
      || (asset.width && asset.height ? `${asset.width}×${asset.height}` : null);
    const videoFacts = isVideo(asset.media_type) ? [
      ['Generation mode', label(job.generation_mode)], ['Duration', asset.duration_seconds ? `${asset.duration_seconds}s (requested ${job.requested_duration_seconds ?? '—'}s)` : '—'],
      ['Frame rate', asset.frame_rate ? `${asset.frame_rate} fps` : 'Unknown'], ['Codec', asset.codec || 'Unknown'],
      ['Audio', asset.has_audio == null ? 'Unknown' : asset.has_audio ? 'Yes' : 'No'], ['Resolution', job.requested_resolution || '—'],
      ...(job.source_asset_id ? [['Source image', `${job.source_asset_id} · ${job.source_asset_checksum.slice(0, 12)}…`]] : []),
    ] : [];
    side.append(facts([
      ['Provider', providerName(job.provider)], ['Model', job.model], ['Cost', cost],
      ['Generation time', job.latency_ms != null ? `${(job.latency_ms / 1000).toFixed(1)}s` : 'Unknown'],
      ['Aspect ratio', job.requested_aspect_ratio || requestedRatio], ['Dimensions', asset.width ? `${asset.width}×${asset.height}` : '—'],
      ...videoFacts,
      ['Created', fmt(asset.created_at)], ['Package', `${asset.content_package_id} v${asset.content_package_version}`],
    ]));
    const videoGate = data.video_gate || {};
    if (asset.media_type === 'IMAGE' && !asset.fixture_only) {
      const source = asset.video_source || {eligible: false, blockers: []};
      const ready = source.eligible && videoGate.live_renderer_configured;
      const button = actionButton('Generate video from image', 'video-from-image', async () => {
        const checksum = asset.checksum_sha256.slice(0, 12);
        const confirmed = await confirmPaidAction({
          title: 'Confirm image-to-video generation',
          warning: 'This starts a paid xAI generation. xAI may bill even if local polling later times out. Automated Claude visual QA may be a separate paid call when configured.',
          thumbnail: `/api/generated-assets/${encodeURIComponent(asset.id)}/content`,
          rows: [['Generation mode', 'Image to video'], ['Provider / model', `${providerName(videoGate.configured_provider)} / ${videoGate.model || '—'}`],
            ['Source asset', asset.id], ['Source checksum', `${checksum}…`], ['Aspect ratio', job.requested_aspect_ratio || `${asset.width}:${asset.height}`],
            ['Duration', `${data.production_jobs?.[0]?.package?.package?.platform_metadata?.duration_seconds || 15}s`], ['Resolution', videoGate.resolution || '—'],
            ['Package version', `v${asset.content_package_version}`]],
          confirmText: 'Start paid xAI video',
        });
        if (!confirmed) return;
        post(`/api/content-packages/${encodeURIComponent(asset.content_package_id)}/render`,
          {media_type: 'VIDEO', provider: videoGate.configured_provider, source_asset_id: asset.id, generation_mode: 'IMAGE_TO_VIDEO',
            confirmed_paid_action: true, client_request_id: clientRequestId()}, 'video-from-image',
          'Rechecking lineage and binding the exact source image…',
          result => result.cached ? 'A video from this exact image already exists' : result.duplicate ? 'A video render is already in progress' : 'Video render queued', 'media');
      }, {primary: false, disabled: !ready});
      side.append(el('div', 'video-action', button,
        el('span', 'secondary-text', ready ? 'Binds this exact image (ID and checksum). Paid call; human review required.'
          : !videoGate.live_renderer_configured ? `Video renderer: ${label(videoGate.renderer_configuration_status)}` : (source.blockers || []).join(' ')),
        progressFor('video-from-image', null)));
    }
    root.append(el('div', 'media-layout', left, side), ...notices);

    const technical = [facts([
      ['Render job', job.id], ['Asset', asset.id], ['Provider request', job.provider_request_id || 'Not returned'],
      ['Provider job', job.provider_job_id || 'Synchronous · none'],
      ['Provider status', label(job.provider_status)], ['Polls', String(job.poll_count || 0)], ['Submitted', fmt(job.submitted_at)],
      ['Download', (job.provider_events || []).some(item => item.event_type === 'DOWNLOADED') ? 'Stored in controlled storage' : job.fixture_only ? 'Not applicable · fixture' : 'Not downloaded'],
      ['Storage', asset.storage_uri], ['SHA-256', asset.checksum_sha256],
      ['Prompt snapshot', job.prompt_snapshot ? `${job.prompt_snapshot.id} · ${job.prompt_snapshot.request_hash.slice(0, 16)}…` : '—'],
    ])];
    const checks = visualQa?.checks || [];
    if (checks.length) technical.push(el('h3', '', 'Semantic checks (advisory — never approval)'), el('ul', 'qa-list', checks.map(check =>
      el('li', '', el('span', `qa-icon ${check.status === 'PASS' ? 'good' : check.status === 'FLAG' ? 'bad' : ''}`, check.status === 'PASS' ? '✓' : check.status === 'FLAG' ? '×' : '?'),
        el('div', '', el('div', 'qa-name', check.label), check.note ? el('div', 'qa-note', check.note) : null), plainPill(check.status)))));
    const ocrChecks = ocrQa?.checks || [];
    if (ocrChecks.length) technical.push(el('h3', '', 'OCR policy evidence'), el('ul', 'qa-list', ocrChecks.map(check =>
      el('li', '', el('span', `qa-icon ${check.status === 'PASS' ? 'good' : check.status === 'FLAG' ? 'bad' : ''}`, check.status === 'PASS' ? '✓' : check.status === 'FLAG' ? '×' : '?'),
        el('div', '', el('div', 'qa-name', check.label || label(check.check)), check.note ? el('div', 'qa-note', check.note) : null), plainPill(check.status)))));
    const detections = ocrQa?.evidence?.detections || [];
    if (detections.length) technical.push(el('h3', '', 'Detected text (evidence only)'), bullets(detections.map(item =>
      `${item.text} · confidence ${item.confidence == null ? 'unknown' : Number(item.confidence).toFixed(2)} · ${item.frame || 'generated image'}${item.box ? ` · box ${item.box.map(value => Number(value).toFixed(3)).join(', ')}` : ''}`)));
    if (visualQa?.evidence?.summary) technical.push(callout('info', 'Visual QA summary', visualQa.evidence.summary));
    if (asset.qa_runs?.length) technical.push(el('h3', '', 'Immutable QA runs'), timeline(asset.qa_runs.map(run => ({
      at: run.created_at, title: `${label(run.qa_kind)} QA v${run.run_number} · ${label(run.status)}`,
      detail: `${run.provider || 'local policy'}${run.provider_request_id ? ` · request ${run.provider_request_id}` : ''} · cost ${run.cost_status === 'known' ? money(run.cost_usd) : run.cost_status}`,
      tone: tone(run.status),
    }))));
    if (asset.reviews?.length) technical.push(el('h3', '', 'Review history'), timeline(asset.reviews.map(item => ({at: item.created_at, title: label(item.action), detail: `${item.reviewer}${item.comment ? ` · ${item.comment}` : ''}`, tone: tone(item.action)}))));
    if (job.provider_events?.length) technical.push(el('h3', '', 'Provider lifecycle'), timeline(job.provider_events.map(item => ({at: item.occurred_at, title: label(item.event_type), detail: label(item.provider_status), tone: tone(item.provider_status)}))));
    technical.push(el('h3', '', 'Render audit'), timeline((job.history || []).map(item => ({at: item.changed_at, title: label(item.to_status), detail: item.message, tone: tone(item.to_status)}))));
    root.append(disclosure('Technical details', technical));
  }
  const failed = jobs.filter(job => !(job.assets || []).length && !isActive(job));
  if (failed.length) root.append(disclosure(`Attempts without media (${failed.length})`, failed.map(job => el('div', 'evidence-item',
    el('div', 'chip-row', pill(job.status), el('span', 'chip', `${job.provider} · ${job.model}`), el('span', 'muted', fmt(job.created_at))),
    job.failure_reason ? el('p', 'secondary-text', job.failure_reason) : null,
  ))));
  const cost = renderCostSummary(data);
  if (cost) root.append(cost);
  renderFinalReels(data, root);
}

function renderActivity(data, root) {
  const items = [];
  (data.runs || []).forEach(run => items.push({at: run.requested_at, title: `Research · ${label(run.status)}`, detail: `${run.provider} · ${run.mode === 'live' ? 'live' : 'test'}${run.error_message ? ` · ${run.error_message}` : ''}`, tone: tone(run.status)}));
  (data.verification_runs || []).forEach(run => items.push({at: run.requested_at, title: `Verification · ${label(run.status)}`, detail: `${run.provider} · ${run.mode === 'live' ? 'live' : 'test'} · cost ${verificationCost(run)}`, tone: tone(run.status)}));
  (data.content_decision_runs || []).forEach(run => items.push({at: run.requested_at, title: `Content decision · ${run.decision_record ? label(run.decision_record.decision) : label(run.status)}`, detail: `${run.provider} · provider called ${run.provider_called ? 'yes' : 'no'}`, tone: tone(run.decision_record?.decision || run.status)}));
  (data.production_jobs || []).forEach(job => (job.history || []).forEach(item => items.push({at: item.changed_at, title: `Package v${job.regeneration_number} · ${label(item.to_status)}`, detail: item.message, tone: tone(item.to_status)})));
  (data.render_jobs || []).forEach(job => (job.history || []).forEach(item => items.push({at: item.changed_at, title: `Media v${job.regeneration_number} · ${label(item.to_status)}`, detail: item.message, tone: tone(item.to_status)})));
  if (!items.length) { root.append(empty('No activity yet', 'Research, verification, decisions, and production appear here as they happen.')); return; }
  items.sort((a, b) => (b.at || '').localeCompare(a.at || ''));
  root.append(card('', cardHead(`Activity (${items.length})`), timeline(items)));
}


/* ---------- distribution (Meta Reels) ---------- */

const PLATFORM_LABELS = {INSTAGRAM_REELS: 'Instagram Reels', FACEBOOK_REELS: 'Facebook Reels'};
const PUBLISH_ACTIVE = ['SCHEDULED', 'QUEUED', 'UPLOADING', 'PROCESSING', 'PUBLISHING', 'NEEDS_INTERVENTION'];

function switchBanner(distribution) {
  const switches = distribution.switches || {};
  const rows = Object.entries(distribution.platforms || {}).map(([platform, config]) =>
    `${PLATFORM_LABELS[platform]}: ${config.enabled ? 'ON' : 'OFF'}${config.missing?.length ? ` · missing ${config.missing.join(', ')}` : ''}`);
  if (!switches.SOCIAL_PUBLISHING_ENABLED) {
    return callout('warn', 'Publishing is OFF', el('p', '', 'SOCIAL_PUBLISHING_ENABLED=0 — no post can reach Meta. Approving packages never publishes by itself.'), bullets(rows));
  }
  return callout('info', 'Publishing switches', bullets(rows), el('p', '', 'Scheduling is local: the switches are re-checked at the scheduled time.'));
}

function publishJobCard(job) {
  const body = card('', cardHead(`${PLATFORM_LABELS[job.platform]} · ${job.mode === 'SCHEDULED' ? 'Scheduled' : 'Publish now'}`, pill(job.status)));
  body.append(facts([
    ['Scheduled for', job.scheduled_for ? fmt(job.scheduled_for) : '—'], ['Container / video', job.provider_container_id || '—'],
    ['Post ID', job.provider_post_id || '—'], ['Attempts', `${job.attempt_count}/${job.max_attempts}`],
    ['Published', job.published_at ? fmt(job.published_at) : '—'], ['API', job.api_version || '—'],
  ]));
  if (job.permalink) body.append(el('p', '', externalLink('Open published post', job.permalink)));
  if (job.last_error_message) body.append(callout(job.status === 'PUBLISHED' ? 'info' : 'warn', label(job.last_error_code || 'Note'), job.last_error_message));
  const actions = el('div', 'review-actions');
  if (job.status === 'SCHEDULED') {
    actions.append(actionButton('Cancel schedule', `cancel-${job.id}`, () => {
      if (!window.confirm('Cancel this scheduled post?')) return;
      post(`/api/publish-jobs/${encodeURIComponent(job.id)}/cancel`, {reason: 'Cancelled in dashboard'}, `cancel-${job.id}`,
        'Cancelling…', () => 'Schedule cancelled', 'distribution');
    }, {primary: false, allowDuringActive: true}));
  }
  if (['NEEDS_INTERVENTION', 'PROCESSING'].includes(job.status) && job.provider_container_id) {
    actions.append(actionButton('Check status', `check-${job.id}`, () => post(`/api/publish-jobs/${encodeURIComponent(job.id)}/check-status`, {},
      `check-${job.id}`, 'Checking Meta (free, never reposts)…', result => `Status: ${label(result.job.status)}`, 'distribution'),
    {primary: false, allowDuringActive: true}));
  }
  if (actions.childNodes.length) body.append(actions, progressFor(`cancel-${job.id}`, null), progressFor(`check-${job.id}`, null));
  if (job.events?.length) body.append(disclosure('Publish audit', timeline(job.events.map(item => ({at: item.occurred_at, title: label(item.event_type), detail: item.status ? label(item.status) : '', tone: tone(item.status)})))));
  return body;
}

function platformCard(source, platform, distribution) {
  const isReel = source.kind === 'FINAL_REEL';
  const matches = item => isReel ? item.final_reel_asset_id === source.id : item.generated_asset_id === source.id;
  const packages = (distribution.packages || []).filter(item => matches(item) && item.platform === platform);
  const pkg = packages[0];
  const container = card('', cardHead(PLATFORM_LABELS[platform], pkg ? pill(pkg.latest_review?.action || 'REQUIRED', pkg.latest_review ? label(pkg.latest_review.action) : 'Platform approval required') : plainPill('No package yet')));
  const createKey = `dist-create-${source.kind}-${source.id}-${platform}`;
  const endpoint = isReel
    ? `/api/final-reels/${encodeURIComponent(source.id)}/distribution-packages`
    : `/api/generated-assets/${encodeURIComponent(source.id)}/distribution-packages`;
  container.append(actionButton(pkg ? 'Rebuild platform package' : `Create ${PLATFORM_LABELS[platform]} package`, createKey, () =>
    post(endpoint, {platform}, createKey,
      'Building copy from the approved package…', result => `Package v${result.distribution_package.version_number} created`, 'distribution'),
  {primary: !pkg, allowDuringActive: true}), progressFor(createKey, null));
  if (!pkg) return container;
  const compliance = pkg.compliance || {};
  if (compliance.errors?.length) container.append(callout('bad', 'Not compliant with platform specs', bullets(compliance.errors)));
  if (compliance.warnings?.length) container.append(callout('warn', 'Platform warnings', bullets(compliance.warnings)));
  if (pkg.copy_validation && !pkg.copy_validation.valid) container.append(callout('bad', 'Copy validation failed', bullets(pkg.copy_validation.errors)));
  container.append(el('div', 'doc',
    pkg.title ? el('div', '', el('div', 'doc-label', 'Title'), el('div', 'doc-headline', pkg.title)) : null,
    el('div', '', el('div', 'doc-label', 'Caption'), el('p', 'body-text pre-wrap', pkg.caption)),
    el('div', '', el('div', 'doc-label', 'Hashtags (verbatim from approved claims)'), el('p', '', pkg.hashtags?.join(' ') || 'None')),
    el('div', '', el('div', 'doc-label', 'Accessibility description'), el('p', 'secondary-text', pkg.accessibility_text || '—'),
      el('p', 'secondary-text', pkg.platform_metadata?.accessibility_note || '')),
    el('div', '', el('div', 'doc-label', 'Cover'), el('p', 'secondary-text', pkg.cover?.strategy === 'thumb_offset' ? `Frame at ${pkg.cover.time_ms} ms (thumb_offset)` : (pkg.platform_metadata?.cover_note || 'Provider default'))),
  ));
  container.append(facts([
    ['Package', `${pkg.id} v${pkg.version_number}`],
    ['Media source', pkg.media_source === 'FINAL_REEL'
      ? `Approved Final Reel ${pkg.final_reel_asset_id} · ${pkg.asset_checksum_sha256.slice(0, 12)}…`
      : `Raw video ${pkg.generated_asset_id} v${pkg.asset_version} · ${pkg.asset_checksum_sha256.slice(0, 12)}…`],
    ['Final Reel source', pkg.media_source === 'FINAL_REEL' ? `${pkg.generated_asset_id} v${pkg.asset_version}` : undefined],
    ['Media approval', pkg.final_reel_review_id || pkg.media_review_id], ['Content package', `${pkg.content_package_id} v${pkg.content_package_version}`],
    ['Claim set', `${pkg.approved_claim_set_id} v${pkg.approved_claim_set_version}`], ['Copy policy', pkg.copy_policy_version],
  ]));
  const reviewActions = el('div', 'review-actions');
  for (const action of ['APPROVED', 'CHANGES_REQUIRED', 'REJECTED']) {
    const key = `dist-review-${pkg.id}-${action}`;
    reviewActions.append(actionButton(label(action), key, () => {
      const reviewer = window.prompt(`Reviewer name for the ${PLATFORM_LABELS[platform]} package`); if (!reviewer?.trim()) return;
      const comment = window.prompt('Optional comment') || '';
      post(`/api/distribution-packages/${encodeURIComponent(pkg.id)}/review`, {action, reviewer, comment}, key,
        'Recording platform review…', () => `${label(action)} recorded`, 'distribution');
    }, {primary: action === 'APPROVED', allowDuringActive: true, disabled: action === 'APPROVED' && !pkg.compliant}));
  }
  container.append(el('div', 'review-workflow', el('strong', '', 'Platform package review'), reviewActions,
    el('span', 'secondary-text', 'Each platform needs its own approval. Approval alone never publishes.')));
  const jobs = (distribution.publish_jobs || []).filter(job => job.distribution_package_id === pkg.id
    || (isReel ? job.final_reel_asset_id === source.id : job.generated_asset_id === source.id) && job.platform === platform);
  const active = jobs.find(job => PUBLISH_ACTIVE.includes(job.status));
  const published = jobs.find(job => job.status === 'PUBLISHED');
  const gate = pkg.publish_gate || {allowed: false, blockers: []};
  const publishKey = `publish-${pkg.id}`;
  const scheduleKey = `schedule-${pkg.id}`;
  const controls = el('div', 'publish-controls');
  const publishButton = actionButton('Publish now', publishKey, () => {
    if (!window.confirm(`Publish this approved video publicly to ${PLATFORM_LABELS[platform]} now? This cannot be undone from here.`)) return;
    post(`/api/distribution-packages/${encodeURIComponent(pkg.id)}/publish`, {client_request_id: clientRequestId(), requested_by: 'dashboard'},
      publishKey, 'Submitting…', result => result.duplicate ? 'A post for this video is already in progress' : 'Publishing started', 'distribution');
  }, {allowDuringActive: true, disabled: !gate.allowed || Boolean(active) || Boolean(published)});
  const when = el('input'); when.type = 'datetime-local'; when.className = 'schedule-input';
  const scheduleButton = actionButton('Schedule', scheduleKey, () => {
    if (!when.value) { state.messages[scheduleKey] = 'Choose a date and time first.'; renderRoom(); return; }
    const iso = new Date(when.value).toISOString();
    if (!window.confirm(`Schedule this video for ${PLATFORM_LABELS[platform]} at ${new Date(when.value).toLocaleString()}? Switches are re-checked at that time.`)) return;
    post(`/api/distribution-packages/${encodeURIComponent(pkg.id)}/schedule`, {scheduled_for: iso, client_request_id: clientRequestId(), requested_by: 'dashboard'},
      scheduleKey, 'Submitting…', result => result.duplicate ? 'A post for this video is already scheduled or running' : 'Scheduled', 'distribution');
  }, {primary: false, allowDuringActive: true, disabled: !pkg.compliant || pkg.latest_review?.action !== 'APPROVED' || Boolean(active) || Boolean(published)});
  controls.append(publishButton, when, scheduleButton);
  container.append(el('div', 'review-workflow', el('strong', '', 'Publishing'), controls,
    published ? el('span', 'secondary-text', 'Already published — duplicate posts of this video are blocked.')
      : active ? el('span', 'secondary-text', 'A post for this video is already scheduled or in progress.')
      : !gate.allowed ? bullets(gate.blockers) : el('span', 'secondary-text', 'All gates pass: media APPROVED, platform package APPROVED, switches ON.'),
    progressFor(publishKey, null), progressFor(scheduleKey, null)));
  jobs.forEach(job => container.append(publishJobCard(job)));
  return container;
}

function renderDistribution(data, root) {
  const distribution = data.distribution || {};
  root.append(switchBanner(distribution));
  const approvedReels = (data.final_reels || []).filter(reel =>
    reel.status === 'READY_FOR_REVIEW' && reel.latest_review?.action === 'APPROVED');
  const approvedVideos = (data.render_jobs || []).flatMap(job => (job.assets || []).map(asset => ({asset, job})))
    .filter(({asset}) => isVideo(asset.media_type) && !asset.fixture_only && asset.latest_review?.action === 'APPROVED')
    // An approved Final Reel supersedes the raw generated video for the same source.
    .filter(({asset}) => !approvedReels.some(reel => reel.source_asset_id === asset.id));
  if (!approvedReels.length && !approvedVideos.length) {
    root.append(empty('No approved Final Reel yet',
      'Distribution starts from an approved Final Reel (or, when no Reel exists, an approved live video). Approve one first.'));
    return;
  }
  for (const reel of approvedReels) {
    root.append(card('', cardHead(`Approved Final Reel ${reel.id}`, plainPill('Final Reel APPROVED', 'good')),
      el('p', 'secondary-text', `Source ${reel.source_asset_id} v${reel.source_asset_version} · ${reel.width}×${reel.height} · ${reel.duration_seconds}s · checksum ${reel.checksum_sha256.slice(0, 16)}…`)));
    const source = {kind: 'FINAL_REEL', id: reel.id};
    root.append(el('div', 'split', platformCard(source, 'INSTAGRAM_REELS', distribution), platformCard(source, 'FACEBOOK_REELS', distribution)));
  }
  for (const {asset} of approvedVideos) {
    root.append(card('', cardHead(`Approved video ${asset.id} · v${asset.version_number}`, plainPill('Media APPROVED', 'good')),
      el('p', 'secondary-text', `${asset.width}×${asset.height} · ${asset.duration_seconds}s · checksum ${asset.checksum_sha256.slice(0, 16)}…`)));
    const source = {kind: 'GENERATED_ASSET', id: asset.id};
    root.append(el('div', 'split', platformCard(source, 'INSTAGRAM_REELS', distribution), platformCard(source, 'FACEBOOK_REELS', distribution)));
  }
}

const RENDERERS = {overview: renderOverview, evidence: renderEvidence, verification: renderVerification, decision: renderDecision, package: renderPackage, media: renderMedia, distribution: renderDistribution, activity: renderActivity};

/* ---------- event view ---------- */

function renderRoom() {
  const data = state.room;
  if (!data) return;
  const event = data.event;
  document.getElementById('room-title').textContent = event.title;
  document.getElementById('room-eyebrow').textContent = `${event.id} · ${label(event.priority)} priority`;
  const sourceCount = (data.signals || []).length;
  document.getElementById('room-meta').replaceChildren(
    pill(event.verification_status), el('span', 'chip', `${sourceCount} source${sourceCount === 1 ? '' : 's'}`),
    el('span', 'chip', `Event time ${fmt(event.event_time)}`), el('span', 'chip', `Seen ${fmt(event.first_seen_at)}`),
  );
  renderStepper(data);
  document.querySelectorAll('#tabs button').forEach(button => button.classList.toggle('active', button.dataset.tab === state.tab));
  renderActionBar(data);
  const root = document.getElementById('room-content');
  const openDetails = [...root.querySelectorAll('details[open] > summary')].map(item => item.textContent);
  root.replaceChildren();
  RENDERERS[state.tab](data, root);
  root.querySelectorAll('details > summary').forEach(item => { if (openDetails.includes(item.textContent)) item.parentElement.open = true; });
  const active = anyActive(data);
  if (active && !state.poll) state.poll = setInterval(() => loadRoom(), 1500);
  if (!active && state.poll) { clearInterval(state.poll); state.poll = null; }
}

async function loadRoom() {
  if (!state.eventId) return;
  try {
    const response = await fetch(`/api/events/${encodeURIComponent(state.eventId)}`);
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || 'Could not load the event');
    state.room = result;
    renderRoom();
  } catch (error) {
    document.getElementById('room-content').replaceChildren(callout('bad', 'Could not load event', error.message));
  }
}

function syncUrl() {
  const params = new URLSearchParams();
  if (state.eventId) { params.set('event', state.eventId); params.set('tab', state.tab); }
  history.replaceState(null, '', params.toString() ? `?${params}` : window.location.pathname);
}

function setTab(tab) { state.tab = tab; syncUrl(); renderRoom(); window.scrollTo({top: document.getElementById('stepper').offsetTop - 24, behavior: 'smooth'}); }

async function openEvent(eventId, tab = 'overview') {
  state.eventId = eventId; state.tab = tab; state.room = null; state.selectedAssetId = null; state.messages = {};
  document.getElementById('desk-view').hidden = true;
  document.getElementById('event-view').hidden = false;
  document.getElementById('room-content').replaceChildren(el('div', 'empty', 'Loading event…'));
  document.getElementById('action-bar').replaceChildren();
  syncUrl();
  window.scrollTo({top: 0});
  await loadRoom();
}

function closeEvent() {
  state.eventId = null; state.room = null;
  if (state.poll) { clearInterval(state.poll); state.poll = null; }
  document.getElementById('event-view').hidden = true;
  document.getElementById('desk-view').hidden = false;
  syncUrl();
  refresh();
}

document.querySelectorAll('#tabs button').forEach(button => { button.onclick = () => setTab(button.dataset.tab); });
document.getElementById('close-room').onclick = closeEvent;
document.getElementById('nav-desk').onclick = closeEvent;
document.addEventListener('keydown', keyEvent => {
  if (keyEvent.key !== 'Escape') return;
  const box = document.getElementById('lightbox');
  if (!box.hidden) box.hidden = true; else if (state.eventId) closeEvent();
});

const params = new URLSearchParams(window.location.search);
const requestedTab = LEGACY_TABS[params.get('tab')] || params.get('tab');
refresh();
if (params.get('event')) openEvent(params.get('event'), TABS.includes(requestedTab) ? requestedTab : 'overview');
setInterval(() => { if (!state.eventId) refresh(); }, 15000);
