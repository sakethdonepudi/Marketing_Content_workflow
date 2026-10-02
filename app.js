// ReachOut OS — automated reel factory UI (Architecture 07).
// All data is rendered with textContent; no HTML from the API is injected.

const state = {
  page: 'home',
  eventId: null,
  tab: 'overview',
  mediaFilter: 'all',
  room: null,
  overview: null,
  pipelines: [],
  uploads: [],
  expandedReelId: null,
  poll: null,
};

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
const fmt = t => t ? new Date(t).toLocaleString(undefined, {month:'short', day:'numeric', hour:'2-digit', minute:'2-digit'}) : '—';
const money = v => v === null || v === undefined ? 'Unknown' : `$${Number(v) < 0.01 && Number(v) > 0 ? Number(v).toFixed(4) : Number(v).toFixed(2)}`;
const label = code => code ? String(code).replaceAll('_',' ').toLowerCase().replace(/^\w/, c => c.toUpperCase()) : '—';

const UI_STATUS = {
  RESEARCHING: 'Researching', VERIFYING: 'Verifying', PRODUCING: 'Producing',
  READY_FOR_REVIEW: 'Ready for review', APPROVED: 'Approved', NEEDS_ATTENTION: 'Needs attention',
};
function uiStatusTone(s) {
  return s === 'READY_FOR_REVIEW' ? 'good' : s === 'APPROVED' ? 'good' : s === 'NEEDS_ATTENTION' ? 'bad' : 'info';
}
// Normalize any internal reel/event status to one of the six user states.
function reelUiStatus(reel, eventStatus) {
  if (reel?.latest_review?.action === 'APPROVED') return 'APPROVED';
  if (reel && reel.status === 'READY_FOR_REVIEW') return 'READY_FOR_REVIEW';
  if (reel && reel.status === 'BLOCKED') return 'NEEDS_ATTENTION';
  return null;
}
function eventUiStatus(event) {
  if (event.verification_status === 'VERIFIED' && event.content_decision_status === 'CREATE') {
    if (event.render_status === 'READY_FOR_REVIEW') return 'READY_FOR_REVIEW';
    return 'PRODUCING';
  }
  if (event.verification_status === 'VERIFYING') return 'VERIFYING';
  if (event.verification_status === 'REJECTED') return 'NEEDS_ATTENTION';
  return 'RESEARCHING';
}

const pill = (code, text, toneName) => el('span', `pill ${toneName || ''}`, text || label(code));
const uiPill = status => el('span', `pill ${uiStatusTone(status)}`, UI_STATUS[status] || status);
const card = (cls, ...kids) => el('section', `card ${cls || ''}`, ...kids);
const cardHead = (title, ...right) => el('div', 'card-head', el('h2', '', title), el('div', 'chip-row', ...right));
const facts = pairs => el('dl', 'facts', pairs.filter(p => p && p[1] !== undefined)
  .map(([t, v]) => el('div', '', el('dt', '', t), el('dd', '', v === null || v === '' ? '—' : v))));
const empty = (t, s) => el('div', 'empty', el('strong', '', t), s);
const bullets = (items, cls='list') => el('ul', cls, (items || []).map(i => el('li', '', i)));
const disclosure = (summary, ...body) => { const d = el('details', 'disclosure'); d.append(el('summary', '', summary), el('div', 'disclosure-body', ...body)); return d; };
const refs = ids => el('div', 'chip-row', (ids||[]).map(id => el('span', 'ref', id)));

function openLightbox(src) { const b = document.getElementById('lightbox'); b.querySelector('img').src = src; b.hidden = false; }
document.getElementById('lightbox').onclick = () => { document.getElementById('lightbox').hidden = true; };

/* ---------- data ---------- */

async function fetchJson(url, opts) {
  const r = await fetch(url, opts);
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || 'Request failed');
  return data;
}

async function refresh() {
  try {
    const [overview, pipelines, uploads, production, discovered] = await Promise.all([
      fetchJson('/api/overview'),
      fetchJson('/api/pipelines').catch(() => ({pipelines: []})),
      fetchJson('/api/uploads').catch(() => ({assets: []})),
      fetchJson('/api/production').catch(() => ({})),
      fetchJson('/api/discovered').catch(() => ({candidates: []})),
    ]);
    state.overview = overview;
    state.pipelines = pipelines.pipelines || [];
    state.uploads = uploads.assets || [];
    state.health = production.health || null;
    state.queue = production.queue || [];
    state.notifications = production.notifications || [];
    state.discovered = discovered.candidates || [];
    state.discoveryHealth = discovered.discovery_health || null;
    document.getElementById('clock').textContent = `Updated ${fmt(overview.updated_at)}`;
    document.getElementById('standard-tag').textContent = overview.reel_standard?.version || 'Standard';
    renderLeader(overview);
    renderRailCounts();
    renderPage();
  } catch (error) {
    document.getElementById('page-root').replaceChildren(empty('Cannot reach the local API', error.message));
  }
}

function renderLeader(overview) {
  const photo = (overview.reference_media?.cbn_options || [])[0];
  if (photo) {
    const img = document.getElementById('leader-photo');
    img.src = `/api/uploads/${encodeURIComponent(photo.id)}/content`; img.hidden = false;
    document.getElementById('leader-initials').hidden = true;
  }
}

function renderRailCounts() {
  const reviews = (state.overview?.events || []).filter(e => eventUiStatus(e) === 'READY_FOR_REVIEW').length;
  const attention = state.pipelines.filter(p => p.ui_status === 'NEEDS_ATTENTION').length;
  const total = (state.overview?.events || []).length;
  document.querySelector('[data-page=home]').textContent = 'Home';
  document.querySelector('[data-page=stories]').textContent = `Stories${total ? ` (${total})` : ''}`;
  document.querySelector('[data-page=review]').textContent = `Review${reviews ? ` (${reviews})` : ''}`;
}

/* ---------- navigation ---------- */

function setPage(page, eventId = null, tab = 'overview') {
  state.page = page; state.eventId = eventId; state.tab = tab;
  document.querySelectorAll('.nav-item').forEach(b => b.classList.toggle('active', b.dataset.page === page));
  renderPage();
  window.scrollTo({top: 0});
}
document.querySelectorAll('.nav-item').forEach(b => b.onclick = () => {
  if (b.dataset.page === 'home') setPage('home');
  else if (b.dataset.page === 'stories') setPage('stories');
  else if (b.dataset.page === 'review') setPage('review');
  else if (b.dataset.page === 'media') setPage('media');
  else setPage('system');
});

function renderPage() {
  const root = document.getElementById('page-root');
  root.replaceChildren();
  const view = el('div', 'view');
  if (state.page === 'home') renderHome(view);
  else if (state.page === 'stories') renderStories(view);
  else if (state.page === 'review') renderReviewList(view);
  else if (state.page === 'media') renderMediaLibrary(view);
  else if (state.page === 'system') renderSystem(view);
  else if (state.page === 'story') renderStoryWorkspace(view);
  root.append(view);
}

/* ---------- Home ---------- */

function allReels() {
  return (state.overview?.events || []).map(event => ({event, reel: null}));
}
function readyStories() {
  return (state.overview?.events || []).filter(e => eventUiStatus(e) === 'READY_FOR_REVIEW');
}

async function loadReelFor(eventId) {
  try { const room = await fetchJson(`/api/events/${encodeURIComponent(eventId)}`); return (room.final_reels || [])[0] || null; }
  catch { return null; }
}

function storyCard(event, reel) {
  const status = reel ? reelUiStatus(reel, event.status) : eventUiStatus(event);
  const thumb = reel ? `/api/final-reels/${encodeURIComponent(reel.id)}/content` : null;
  const media = thumb ? el('video', 'thumb') : el('div', 'thumb-empty', 'No reel yet');
  if (thumb) { media.muted = true; media.preload = 'metadata'; media.src = thumb; }
  const btn = el('button', 'story-card', media, el('div', 'sc-body',
    el('div', 'sc-title', event.title),
    el('div', 'sc-sub', `${event.workspace_key === 'n-chandrababu-naidu-andhra-pradesh' ? 'Andhra Pradesh' : '—'} · ${fmt(event.event_time)}`),
    el('div', 'chip-row', uiPill(status), reel ? el('span', 'pill plain', `${Math.round(reel.duration_seconds)}s Reel`) : null),
  ));
  btn.type = 'button';
  btn.onclick = () => setPage('story', event.id, 'reel');
  return btn;
}

function renderOpsHome(container) {
  fetchJson('/api/dashboard/home').then(({system, latest_updates, private_uploads}) => {
    const blocks = [];
    const s = system || {};
    blocks.push(el('div', 'ops-summary',
      el('div', 'ops-title', 'ReachOut'),
      el('div', 'ops-row', `System: ${s.application || 'ONLINE'}`),
      el('div', 'ops-row', `YouTube: ${s.youtube_status || '—'}${s.youtube_channel ? ` — ${s.youtube_channel}` : ''}`),
      el('div', 'ops-row', `Publishing: ${s.publishing || 'PRIVATE ONLY'}`)));

    const updates = el('div', 'section',
      el('div', 'section-title', el('h2', '', 'Latest updates'),
        el('span', 'pill plain', `${(latest_updates || []).length}`)));
    const list = (latest_updates || []);
    if (!list.length) {
      updates.append(empty('No verified updates yet.', 'Verified news appears here as text while the reel is produced.'));
    } else {
      updates.append(el('div', 'update-grid', list.map(item => {
        const tone = item.state === 'VERIFIED' ? 'good' : item.state === 'VERIFYING' ? 'warn' : '';
        const card = el('div', 'card update-card',
          el('div', 'chip-row', el('span', `pill ${tone}`, item.state_label || item.state),
            el('span', 'muted', `${item.source_count || 0} sources`)),
          el('div', 'update-headline', item.headline || ''),
          el('div', 'muted pre-wrap', item.summary || ''),
          el('div', 'update-meta', [item.location, fmt(item.updated_at), item.reel_status ? `Reel: ${label(item.reel_status)}` : null]
            .filter(Boolean).join(' · ')));
        const b = el('button', 'btn', 'View story'); b.type = 'button';
        b.onclick = () => setPage('story', item.event_id, 'reel');
        card.append(el('div', 'chip-row', b));
        return card;
      })));
    }
    blocks.push(updates);

    const uploads = el('div', 'section',
      el('div', 'section-title', el('h2', '', 'Private YouTube uploads'),
        el('span', 'pill plain', `${(private_uploads || []).length}`)));
    const ups = (private_uploads || []);
    if (!ups.length) {
      uploads.append(empty('No private uploads yet.', 'Approved reels upload to YouTube as PRIVATE.'));
    } else {
      uploads.append(el('div', 'update-grid', ups.map(u => {
        const card = el('div', 'card update-card',
          el('div', 'chip-row', el('span', 'pill bad', 'PRIVATE'),
            el('span', 'muted', u.processing_state || '—')),
          el('div', 'update-headline', u.title || ''),
          el('div', 'muted', `Reel ${u.reel_id || '—'} · duration ${u.duration_seconds != null ? `${Math.round(u.duration_seconds)}s` : '—'}${u.views != null ? ` · views ${u.views}` : ''}`),
          el('div', 'update-meta', [fmt(u.uploaded_at), u.video_id ? `video ${u.video_id}` : null].filter(Boolean).join(' · ')));
        const actions = el('div', 'chip-row');
        if (u.youtube_url) { const a = el('a', 'btn', 'Open YouTube'); a.href = u.youtube_url; a.target = '_blank'; a.rel = 'noopener'; actions.append(a); }
        const vr = el('button', 'btn', 'View reel'); vr.type = 'button';
        vr.onclick = () => setPage('story', u.event_id, 'reel'); actions.append(vr);
        card.append(actions);
        return card;
      })));
    }
    blocks.push(uploads);
    container.replaceChildren(...blocks);
  }).catch(() => { container.replaceChildren(empty('Dashboard unavailable', 'Could not load the operations summary.')); });
}

function renderHome(root) {
  const overview = state.overview || {};
  // Operational header: system summary + latest verified updates + private uploads (Arch 13).
  const ops = el('div', 'ops-home');
  root.append(ops);
  renderOpsHome(ops);
  if (state._opsTimer) clearInterval(state._opsTimer);
  state._opsTimer = setInterval(() => { if (state.page === 'home') renderOpsHome(ops); }, 60000);

  const events = overview.events || [];
  const ready = events.filter(e => eventUiStatus(e) === 'READY_FOR_REVIEW');
  const generating = events.filter(e => ['PRODUCING','VERIFYING','RESEARCHING'].includes(eventUiStatus(e)));
  const attention = state.pipelines.filter(p => p.ui_status === 'NEEDS_ATTENTION');

  const cta = el('button', 'btn primary', `Review reels${ready.length ? ` (${ready.length})` : ''}`);
  cta.type = 'button'; cta.onclick = () => setPage('review');
  root.append(el('header', 'home-head',
    el('div', 'greeting', 'N. Chandrababu Naidu · Andhra Pradesh'),
    el('h1', '', `${ready.length} ready for review`),
    el('p', 'summary-line', `${generating.length} generating · ${attention.length} needs attention`),
    el('div', 'f-actions', cta)));

  const t = state.health?.today;
  if (t) root.append(el('div', 'section', el('div', 'section-title', el('h2', '', 'Today')),
    el('div', 'pipeline-strip',
      el('div', 'pstage', el('div', 'n', String(t.reels_generated)), el('div', 'l', 'Generated')),
      el('div', 'pstage', el('div', 'n', String(t.approved)), el('div', 'l', 'Approved')),
      el('div', `pstage ${t.needs_attention ? 'active' : ''}`, el('div', 'n', String(t.needs_attention)), el('div', 'l', 'Needs attention')),
    )));

  const featured = ready[0];
  if (featured) {
    const wrap = el('div', 'featured');
    loadReelFor(featured.id).then(reel => {
      const video = el('video'); video.muted = true; video.controls = true; video.preload = 'metadata';
      if (reel) video.src = `/api/final-reels/${encodeURIComponent(reel.id)}/content`;
      const info = el('div', '',
        el('div', 'chip-row', el('span', 'pill gold', 'Featured · Ready for review')),
        el('div', 'f-title', featured.title),
        el('div', 'f-meta', `Andhra Pradesh · ${reel ? `${Math.round(reel.duration_seconds)}s Reel` : 'Reel ready'}`),
        el('div', 'f-actions', (() => { const b = el('button', 'btn primary', 'Review'); b.type = 'button';
          b.onclick = () => setPage('story', featured.id, 'reel'); return b; })()));
      wrap.append(video, info);
    });
    root.append(el('div', 'section', el('div', 'section-title', el('h2', '', 'Featured')), wrap));
  }

  root.append(el('div', 'section',
    el('div', 'section-title', el('h2', '', `Ready for review (${ready.length})`)),
    ready.length ? el('div', 'story-grid', ready.map(e => storyCard(e))) : empty('Nothing waiting', 'No reels are ready for review right now.')));

  const stages = [
    ['Research', events.filter(e => ['RESEARCHING'].includes(eventUiStatus(e))).length],
    ['Verification', events.filter(e => ['VERIFYING'].includes(eventUiStatus(e))).length],
    ['Production', events.filter(e => eventUiStatus(e) === 'PRODUCING').length],
    ['Review', ready.length],
  ];
  root.append(el('div', 'section', el('div', 'section-title', el('h2', '', 'Pipeline')),
    el('div', 'pipeline-strip', stages.map(([name, count]) =>
      el('div', `pstage ${count ? 'active' : ''}`, el('div', 'n', String(count)), el('div', 'l', name))))));

  const activity = state.pipelines.slice(0, 6);
  if (activity.length) root.append(el('div', 'section', el('div', 'section-title', el('h2', '', 'Recent activity')),
    card('', el('ul', 'timeline', activity.map(p => el('li', '',
      el('time', '', fmt(p.updated_at)), el('span', `tl-dot ${uiStatusTone(p.ui_status)}`),
      el('div', 'tl-body', el('strong', '', UI_STATUS[p.ui_status] || label(p.status)), ` · ${label(p.current_stage)}`)))))));
}

/* ---------- Stories ---------- */

function renderStories(root) {
  const events = state.overview?.events || [];
  root.append(el('header', 'page-head', el('div', '', el('div', 'eyebrow', 'All coverage'), el('h1', '', 'Stories'),
    el('p', 'lede', 'Every event the desk is tracking, from first source to finished reel.'))));
  const discovered = state.discovered || [];
  if (discovered.length) {
    root.append(el('div', 'section', el('div', 'section-title', el('h2', '', `Discovered (${discovered.length})`),
      el('span', 'muted', 'Fast same-day leads — not yet verified')),
      el('div', 'story-grid', discovered.slice(0, 12).map(c => el('div', 'card',
        el('div', 'sc-title', c.headline),
        el('div', 'chip-row', pill(c.verification_status, label(c.verification_status)),
          el('span', 'pill plain', c.location || 'Location unknown'),
          el('span', 'pill plain', `${c.source_count} signals · ${c.entity_count} entities`),
          el('span', 'pill plain', c.confidence)),
        el('p', 'secondary-text', `First seen ${fmt(c.first_seen_at)}`),
      )))));
  }
  root.append(el('div', 'section-title', el('h2', '', `Events (${events.length})`)));
  root.append(events.length ? el('div', 'story-grid', events.map(e => storyCard(e))) : empty('No stories yet', 'Ingest a source to begin.'));
}

/* ---------- Review list ---------- */

function renderReviewList(root) {
  const ready = readyStories().slice().sort((a, b) => (a.event_time || '').localeCompare(b.event_time || ''));
  root.append(el('header', 'page-head', el('div', '', el('div', 'eyebrow', 'Editorial queue'),
    el('h1', '', `Review (${ready.length})`),
    el('p', 'lede', 'Oldest first. Press J for next, K for previous. Approval never publishes.'))));
  root.append(ready.length ? el('div', 'story-grid', ready.map(e => storyCard(e)))
    : empty('No reels to review', 'Finished reels appear here once automated QA passes.'));
  // Keyboard inbox navigation (J next / K previous).
  document.onkeydown = event => {
    if (state.page !== 'review' || event.metaKey || event.ctrlKey || event.target.matches('input, textarea')) return;
    const key = event.key.toLowerCase();
    if (key !== 'j' && key !== 'k') return;
    state.reviewIndex = state.reviewIndex || 0;
    state.reviewIndex = key === 'j'
      ? Math.min(ready.length - 1, state.reviewIndex + 1)
      : Math.max(0, state.reviewIndex - 1);
    if (ready[state.reviewIndex]) setPage('story', ready[state.reviewIndex].id, 'reel');
  };
}

/* ---------- Story workspace (Story + Reel review) ---------- */

const TABS = ['overview', 'sources', 'media', 'reel'];

async function renderStoryWorkspace(root) {
  root.append(el('button', 'back-link', '← Stories'));
  root.querySelector('.back-link').onclick = () => setPage('stories');
  if (!state.room || state.room.event?.id !== state.eventId) {
    const loading = empty('Loading story…', '');
    root.append(loading);
    try { state.room = await fetchJson(`/api/events/${encodeURIComponent(state.eventId)}`); loading.remove(); }
    catch (e) { loading.replaceWith(empty('Could not load story', e.message)); return; }
  }
  const data = state.room;
  const event = data.event;
  const reel = (data.final_reels || [])[0];
  root.append(el('header', 'story-head',
    el('div', '', el('div', 'eyebrow', 'Andhra Pradesh · Story'),
      el('h1', '', event.title),
      el('div', 'story-sub', uiPill(reel ? reelUiStatus(reel, event.status) : eventUiStatus(event)),
        el('span', 'pill plain', fmt(event.event_time)),
        reel ? el('span', 'pill plain', `${Math.round(reel.duration_seconds)}s Reel`) : null,
        el('span', 'pill plain', data.reel_standard?.version || 'Standard')))));

  const tabs = el('div', 'tabs', TABS.map(t => {
    const b = el('button', '', t === 'reel' ? 'Reel' : label(t)); b.type = 'button';
    b.className = state.tab === t ? 'active' : '';
    b.onclick = () => { state.tab = t; renderPage(); };
    return b;
  }));
  root.append(tabs);

  const panel = el('div', 'tab-panel');
  if (state.tab === 'overview') renderStoryOverview(panel, data);
  else if (state.tab === 'sources') renderStorySources(panel, data);
  else if (state.tab === 'media') renderStoryMedia(panel, data);
  else renderReelReview(panel, data, reel);
  root.append(panel);
}

function renderStoryOverview(root, data) {
  const event = data.event;
  const run = (data.runs || []).find(r => r.summary);
  root.append(card('', cardHead('Summary'),
    el('p', 'body-text', run?.summary?.what_happened || 'No research summary yet.')));
  root.append(card('', cardHead('Status'), facts([
    ['Verification', label(event.verification_status)], ['Decision', label(event.content_decision_status)],
    ['Package', label(event.production_status)], ['Media', label(event.render_status)],
  ])));
  const required = (data.verification_runs || []).flatMap(r => r.decisions || []).filter(d => d.required_for_event);
  if (required.length) root.append(card('', cardHead(`Verified claims (${required.length})`),
    required.map(d => el('div', '', el('div', 'chip-row', pill(d.decision), el('span', 'ref', d.claim_id)),
      el('p', 'body-text', d.claim_text)))));
}

function renderStorySources(root, data) {
  const signals = data.signals || [];
  root.append(card('', cardHead(`Sources (${signals.length})`),
    signals.length ? signals.map(s => el('div', '',
      el('a', '', s.title), el('p', 'secondary-text', `${s.registered_source_name || s.source_name || ''} · ${fmt(s.publication_time)}`))) :
      el('p', 'secondary-text', 'No linked evidence.')));
}

function renderStoryMedia(root, data) {
  const assets = (data.render_jobs || []).flatMap(j => j.assets || []);
  root.append(card('', cardHead(`Visual assets (${assets.length})`),
    assets.length ? el('div', 'thumb-row', assets.map(a => {
      const img = el('img'); img.src = `/api/generated-assets/${encodeURIComponent(a.id)}/content`;
      img.alt = a.id; img.onclick = () => openLightbox(img.src); return img;
    })) : el('p', 'secondary-text', 'No generated assets.')));
}

function simpleQa(reel) {
  const checks = [
    ['Facts', (reel.factual_qa || {}).status],
    ['Rights', (reel.rights_provenance_qa || {}).status],
    ['Audio', (reel.audio_qa || {}).status],
    ['Visual continuity', (reel.rendered_frame_continuity_qa || {}).status],
    ['Instagram', (reel.instagram_compatibility || {}).compliant ? 'PASS' : 'FLAG'],
    ['Facebook', (reel.facebook_compatibility || {}).compliant ? 'PASS' : 'FLAG'],
  ];
  return el('div', 'qa-simple', checks.map(([name, status]) => el('div', 'qa-row',
    el('span', '', name),
    status === 'PASS' ? el('span', 'tick', '✓') : status ? el('span', 'cross', '!') : el('span', 'dash', '–'))));
}

function changeRequestForm(reel, onDone) {
  const options = ['Narration', 'Visuals', 'Subtitles', 'Audio', 'Sources', 'Other'];
  const checks = options.map(o => { const i = el('input'); i.type = 'checkbox'; i.value = o;
    return el('label', '', i, o); });
  const comment = el('textarea'); comment.placeholder = 'Optional comment';
  const submit = el('button', 'btn primary', 'Submit request'); submit.type = 'button';
  const form = el('form', 'change-form', el('h3', '', 'What needs changing?'), el('div', 'checks', checks), comment, submit);
  submit.onclick = () => {
    const picked = checks.map((l, i) => l.querySelector('input').checked ? options[i] : null).filter(Boolean);
    const reviewer = window.prompt('Reviewer name'); if (!reviewer?.trim()) return;
    submit.disabled = true;
    fetchJson(`/api/final-reels/${encodeURIComponent(reel.id)}/revision`, {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({categories: picked, comment: comment.value, reviewer}),
    }).then(() => { onDone(); refresh(); }).catch(e => { submit.disabled = false; window.alert(e.message); });
  };
  return form;
}

function loadYoutubePackage(reelId, container) {
  fetchJson(`/api/reels/${encodeURIComponent(reelId)}/youtube-packages`).then(({packages, approval_state, connection, project_audit_status}) => {
    const body = el('div', 'copy-block');
    const pkg = packages && packages[0];
    if (!pkg) {
      body.append(empty('No YouTube package yet', 'A package is generated when the reel reaches review.'));
      container.replaceChildren(cardHead('YouTube package'), body);
      return;
    }
    body.append(facts([
      ['Title', pkg.title_primary],
      ['Format', pkg.youtube_format],
      ['Language', pkg.language_mix],
      ['QA', pkg.qa?.status || '—'],
      ['Copy approval', pkg.status === 'YOUTUBE_COPY_APPROVED' ? 'Approved' : label(pkg.status)],
      ['Project audit', project_audit_status || 'UNKNOWN'],
    ]));
    body.append(el('p', 'body-text pre-wrap', pkg.description || ''));
    body.append(el('div', 'chip-row', (pkg.hashtags || []).map(t => el('span', 'ref', t))));
    body.append(disclosure('Search tags', el('div', 'chip-row', (pkg.tags || []).map(t => el('span', 'ref', t)))));
    if (pkg.alternates) body.append(el('div', 'muted', `Alt titles: ${pkg.alternates.filter(Boolean).join(' · ')}`));
    const row = el('div', 'chip-row');
    const approve = el('button', 'btn primary', 'Approve YouTube package'); approve.type = 'button';
    approve.onclick = () => {
      const reviewer = window.prompt('Reviewer name'); if (!reviewer?.trim()) return;
      approve.disabled = true;
      fetchJson(`/api/youtube-packages/${encodeURIComponent(pkg.id)}/copy-review`, {
        method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({reviewer}),
      }).then(() => loadYoutubePackage(reelId, container)).catch(e => { approve.disabled = false; window.alert(e.message); });
    };
    const edit = el('button', 'btn', 'Edit'); edit.type = 'button';
    edit.onclick = () => {
      const title = window.prompt('Title', pkg.title_primary) || pkg.title_primary;
      const description = window.prompt('Description', pkg.description) || pkg.description;
      fetchJson(`/api/youtube-packages/${encodeURIComponent(pkg.id)}`, {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({title, description, edited_by: 'reviewer'}),
      }).then(() => loadYoutubePackage(reelId, container)).catch(e => window.alert(e.message));
    };
    const regen = el('button', 'btn', 'Regenerate copy'); regen.type = 'button';
    regen.onclick = () => { regen.disabled = true;
      fetchJson(`/api/reels/${encodeURIComponent(reelId)}/youtube-packages/regenerate`, {method:'POST', headers:{'Content-Type':'application/json'}, body:'{}'})
        .then(() => loadYoutubePackage(reelId, container)).catch(e => { regen.disabled = false; window.alert(e.message); });
    };
    row.append(approve, edit, regen);
    body.append(row);
    if (!connection || !connection.upload_capability) body.append(el('div', 'muted', 'YouTube upload: UNCONFIGURED (OAuth required).'));
    container.replaceChildren(cardHead('YouTube package', plainPill(pkg.youtube_format, '')), body);
  }).catch(e => container.replaceChildren(cardHead('YouTube package'), empty('YouTube package unavailable', e.message)));
}

function renderReelReview(root, data, reel) {
  if (!reel) { root.append(empty('No reel yet', 'The automated pipeline has not produced a reel for this story.')); return; }
  const layout = el('div', 'review-layout');
  const video = el('video'); video.controls = true; video.playsInline = true; video.preload = 'metadata';
  video.src = `/api/final-reels/${encodeURIComponent(reel.id)}/content`;
  layout.append(el('div', 'player-wrap', video));

  const panel = el('div', 'review-panel');
  panel.append(el('div', 'rp-title', data.event.title));
  panel.append(facts([
    ['Duration', `${Math.round(reel.duration_seconds)}s`], ['Language', 'Telugu'],
    ['Standard', reel.production_standard_version || data.reel_standard?.version || '—'],
    ['Status', reel.status],
  ]));
  panel.append(card('', cardHead('Narration'),
    disclosure('Show narration', el('p', 'body-text pre-wrap', reel.narration_text || ''))));

  const sources = ((reel.composition_manifest || {}).beats || []).filter(b => b.asset_source);
  if (sources.length) panel.append(card('', cardHead('Visual sources'),
    el('div', 'thumb-row', sources.map(b => {
      const img = el('img'); img.src = `/api/uploads/${encodeURIComponent(b.asset_source)}/content`.replace('/uploads/', '/api/uploads/');
      img.onerror = () => { img.replaceWith(el('div', 'mini-card', (b.label || '').split(',')[0] || '—')); };
      return img;
    }))));

  panel.append(card('', cardHead('Quality'), simpleQa(reel),
    disclosure('View technical QA', facts([
      ['Subtitle', (reel.subtitle_qa || {}).status], ['Glyph render', ((reel.subtitle_qa || {}).glyph_render_qa || {}).status || '—'],
      ['OCR', ((reel.subtitle_qa || {}).ocr_qa || {}).status || '—'], ['Editorial', (reel.editorial_continuity_qa || {}).status],
      ['Rendered frames', (reel.rendered_frame_continuity_qa || {}).status],
      ['Third-asset transitions', (reel.rendered_frame_continuity_qa || {}).third_asset_transition_count],
      ['Local context', (reel.local_context_qa || {}).status], ['Public figure', (reel.public_figure_qa || {}).status],
      ['Reference standard', (reel.reference_standard_qa || {}).status], ['Cost', reel.cost_status === 'known' ? money(reel.cost_usd) : label(reel.cost_status)],
    ]))));

  // YouTube package (Architecture 11) — titles/description/hashtags/tags, copy approval separate.
  const ytCard = card('', cardHead('YouTube package', plainPill('Copy approved separately', '')));
  ytCard.append(empty('Loading YouTube package…', ''));
  panel.append(ytCard);
  loadYoutubePackage(reel.id, ytCard);

  const actions = el('div', 'sticky-actions');
  const done = () => { state.room = null; renderPage(); };
  for (const [text, action, cls] of [['Reject','REJECTED','danger'], ['Request changes','CHANGES_REQUIRED',''], ['Approve','APPROVED','primary']]) {
    const b = el('button', `btn ${cls}`, text); b.type = 'button';
    b.onclick = () => {
      if (action === 'CHANGES_REQUIRED') { panel.append(changeRequestForm(reel, done)); b.disabled = true; return; }
      const reviewer = window.prompt('Reviewer name'); if (!reviewer?.trim()) return;
      const comment = window.prompt('Optional comment') || '';
      b.disabled = true;
      fetchJson(`/api/final-reels/${encodeURIComponent(reel.id)}/review`, {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({action, reviewer, comment}),
      }).then(() => { done(); refresh(); }).catch(e => { b.disabled = false; window.alert(e.message); });
    };
    actions.append(b);
  }
  panel.append(actions);
  layout.append(panel);
  root.append(layout);
}

/* ---------- Media library ---------- */

const MEDIA_FILTERS = { all: 'All', real: 'Real', generated: 'Generated', government: 'Government', user: 'User', needs: 'Needs rights review' };

function mediaItems() {
  const items = [];
  for (const asset of state.uploads) items.push({
    kind: 'real', id: asset.id, title: asset.label, source: asset.source_name,
    location: asset.asset_type === 'PUBLIC_FIGURE_PHOTO' ? 'AP' : '', rights: asset.rights_status,
    src: `/api/uploads/${encodeURIComponent(asset.id)}/content`,
  });
  const seen = new Set();
  for (const job of (state.room?.render_jobs || [])) for (const a of (job.assets || [])) if (!seen.has(a.id)) {
    seen.add(a.id);
    items.push({kind: 'generated', id: a.id, title: `${label(a.media_type)} · v${a.version_number}`, source: 'AI generated', location: '', rights: 'GENERATED_ORIGINAL', src: `/api/generated-assets/${encodeURIComponent(a.id)}/content`});
  }
  return items;
}

function renderMediaLibrary(root) {
  // Ensure story assets are available even on the standalone Media page.
  if (!state.room) root.append(el('p', 'secondary-text', ''));
  root.append(el('header', 'page-head', el('div', '', el('div', 'eyebrow', 'Visual assets'), el('h1', '', 'Media'),
    el('p', 'lede', 'Real, rights-cleared imagery and generated visuals used across stories.'))));
  const filters = el('div', 'filters', Object.entries(MEDIA_FILTERS).map(([k, v]) => {
    const b = el('button', '', v); b.type = 'button'; b.className = state.mediaFilter === k ? 'active' : '';
    b.onclick = () => { state.mediaFilter = k; renderPage(); }; return b;
  }));
  root.append(filters);
  const items = mediaItems().filter(i => {
    if (state.mediaFilter === 'real') return i.kind === 'real';
    if (state.mediaFilter === 'generated') return i.kind === 'generated';
    if (state.mediaFilter === 'government') return /GODL|PIB|Government/i.test(i.source || '');
    if (state.mediaFilter === 'user') return /USER_PROVIDED/.test(i.rights || '');
    if (state.mediaFilter === 'needs') return !/VERIFIED_REUSE|ATTRIBUTION_REQUIRED|USER_PROVIDED|GENERATED_ORIGINAL/.test(i.rights || '');
    return true;
  });
  root.append(items.length ? el('div', 'media-grid', items.map(i => el('div', 'media-card',
    (() => { const img = el('img'); img.src = i.src; img.alt = i.title; img.onclick = () => openLightbox(i.src); return img; })(),
    el('div', 'mc-body', el('div', 'mc-title', i.title || i.id), el('div', 'mc-sub', i.source || ''),
      el('div', 'chip-row', pill(i.rights, label(i.rights)))))))
    : empty('No media', 'Nothing matches this filter yet.'));
}

/* ---------- System ---------- */

function renderSystem(root) {
  const overview = state.overview || {};
  root.append(el('header', 'page-head', el('div', '', el('div', 'eyebrow', 'Operations'), el('h1', '', 'System'),
    el('p', 'lede', 'Providers, pipeline health, costs, and standards. Engineering detail lives here.'))));
  const grid = el('div', 'system-grid');

  const research = overview.research || {}; const production = overview.content_production || {}; const media = overview.media_rendering || {};
  grid.append(card('', cardHead('Providers'),
    el('ul', 'provider-list',
      el('li', '', el('span', '', 'Grok research'), el('small', '', research.grok_configured ? 'ready' : 'off')),
      el('li', '', el('span', '', 'Claude production'), el('small', '', production.anthropic_configured ? 'ready' : 'off')),
      el('li', '', el('span', '', 'Edge TTS (Telugu)'), el('small', '', 'ready')),
      el('li', '', el('span', '', 'Image renderer'), el('small', '', media.image?.live ? 'ready' : 'off')),
      el('li', '', el('span', '', 'Video renderer'), el('small', '', media.video?.live ? 'ready' : 'off')))));

  const attention = state.pipelines.filter(p => p.ui_status === 'NEEDS_ATTENTION');
  grid.append(card('', cardHead('Pipeline health', pill(attention.length ? `${attention.length} need attention` : 'healthy', attention.length ? 'bad' : 'good')),
    state.pipelines.length ? el('ul', 'list', state.pipelines.slice(0, 8).map(p =>
      el('li', '', `${p.id} · ${label(p.current_stage)} · ${UI_STATUS[p.ui_status] || p.status}${p.failure_reason ? ` · ${p.failure_reason}` : ''}`))) :
      el('p', 'secondary-text', 'No pipeline runs.')));

  grid.append(card('', cardHead('Standards'), facts([
    ['Production standard', overview.reel_standard?.version || '—'],
    ['Reference reel', overview.reel_standard?.reference_reel_id || '—'],
    ['Composer policy', overview.reel_standard?.reference_composer_policy || '—'],
    ['Output', '720×1280 · 9:16 · H.264+AAC · 24fps'],
  ])));

  grid.append(card('', cardHead('Failed jobs'),
    attention.length ? el('ul', 'list', attention.map(p => el('li', '', `${p.id} · ${label(p.failure_stage)} · ${p.recommended_action || p.failure_reason}`)))
      : el('p', 'secondary-text', 'No failed jobs.')));

  const switches = overview.distribution?.switches || {};
  grid.append(card('', cardHead('Publishing switches'), facts([
    ['SOCIAL_PUBLISHING_ENABLED', switches.SOCIAL_PUBLISHING_ENABLED ? 'ON' : 'OFF'],
    ['Instagram', switches.INSTAGRAM_PUBLISHING_ENABLED ? 'ON' : 'OFF'],
    ['Facebook', switches.FACEBOOK_PUBLISHING_ENABLED ? 'ON' : 'OFF'],
  ])));

  const yc = state.discoveryHealth?.youtube_connection || null;
  if (yc) grid.append(card('', cardHead('YouTube upload',
      pill(yc.status, yc.status === 'CONNECTED' ? 'good' : yc.status === 'UNCONFIGURED' ? '' : 'warn')),
    facts([
      ['Channel', yc.channel_name || '—'],
      ['Channel ID', yc.channel_id_masked || '—'],
      ['Connected account', yc.connected_account || '—'],
      ['Token status', yc.token_status],
      ['Refresh health', yc.refresh_health || 'UNKNOWN'],
      ['Public upload', yc.public_upload_capability || 'UNKNOWN'],
      ['Token expires', fmt(yc.token_expires_at)],
      ['Last authorized call', fmt(yc.last_authorized_call)],
      ['API-key discovery', yc.api_key_discovery ? 'Configured' : 'Off'],
      ['Upload capability', yc.upload_capability ? 'OAuth ready' : 'OAuth required'],
    ]),
    yc.missing?.length ? disclosure('Advanced', facts([['Missing', yc.missing.join(', ')]])) : null));

  const health = state.health || {};
  if (health.today) grid.append(card('', cardHead('Production health'),
    facts([
      ['Generated today', health.today.reels_generated], ['Ready for review', health.today.ready_for_review],
      ['Approved today', health.today.approved], ['Needs attention', health.today.needs_attention],
      ['Avg generation', health.average_generation_seconds != null ? `${health.average_generation_seconds}s` : '—'],
      ['Avg cost/reel', health.average_cost_per_reel != null ? money(health.average_cost_per_reel) : 'Unknown'],
      ['Failure rate', `${Math.round((health.failure_rate || 0) * 100)}%`],
      ['Unknown-cost runs', health.unknown_cost_runs],
    ])));

  const dh = state.discoveryHealth || {};
  const slo = dh.slo || {};
  const yt = dh.youtube || null;
  const sourceRows = Object.entries(dh.sources || {});
  const sourceTable = el('table', 'data-table discovery-sources',
    el('thead', '', el('tr', '',
      ...['Publisher', 'Adapter', 'Health', 'Last fetch', 'Last success', 'Items 24h', 'Avg latency'].map(h => el('th', '', h)))),
    el('tbody', '', sourceRows.map(([family, s]) => el('tr', '',
      el('td', '', s.publisher || family),
      el('td', '', s.adapter_type || '—'),
      el('td', '', el('span', `pill ${s.status === 'HEALTHY' ? 'good' : s.status === 'DEGRADED' ? 'warn' : s.status === 'UNCONFIGURED' ? '' : 'bad'}`, s.status)),
      el('td', '', fmt(s.last_polled)),
      el('td', '', fmt(s.last_success)),
      el('td', '', s.results_last_24h ?? 0),
      el('td', '', s.average_latency_ms != null ? `${Math.round(s.average_latency_ms)} ms` : '—')))));
  const parserDetail = el('table', 'data-table discovery-parsers',
    el('thead', '', el('tr', '', ...['Publisher', 'Feed URL', 'Parser', 'Poll interval', 'Failures', 'Last error'].map(h => el('th', '', h)))),
    el('tbody', '', sourceRows.map(([family, s]) => el('tr', '',
      el('td', '', s.publisher || family),
      el('td', '', s.feed_url ? el('code', '', s.feed_url) : '—'),
      el('td', '', s.parser_name || '—'),
      el('td', '', s.poll_interval_seconds != null ? `${s.poll_interval_seconds}s` : '—'),
      el('td', '', s.consecutive_failures ?? 0),
      el('td', '', s.last_error || '—')))));
  grid.append(card('', cardHead('Discovery health',
    pill(dh.enabled ? 'Live discovery ON' : 'Live discovery OFF', dh.enabled ? 'good' : ''),
    plainPill(`${dh.interval_seconds || 300}s interval`, ''),
    plainPill(`${sourceRows.filter(([, s]) => s.status === 'HEALTHY').length}/${sourceRows.length} healthy`, '')),
    facts([
      ['Signals today', slo.signals_today ?? 0], ['Candidates today', slo.candidates_today ?? 0],
      ['Median latency', slo.median_latency_seconds != null ? `${slo.median_latency_seconds}s` : '—'],
      ['p95 latency', slo.p95_latency_seconds != null ? `${slo.p95_latency_seconds}s` : '—'],
      ['Median fetch', slo.median_fetch_ms != null ? `${slo.median_fetch_ms} ms` : '—'],
      ['p95 fetch', slo.p95_fetch_ms != null ? `${slo.p95_fetch_ms} ms` : '—'],
      ['SLO misses', slo.slo_misses ?? 0],
    ]),
    sourceRows.length ? sourceTable : empty('No sources registered', 'Run a discovery cycle to populate the registry.'),
    yt ? card('', cardHead('YouTube discovery',
        pill(yt.status, yt.status === 'HEALTHY' ? 'good' : yt.status === 'DEGRADED' ? 'warn' : yt.status === 'UNCONFIGURED' ? '' : 'bad'),
        plainPill(yt.mode || 'NORMAL', yt.mode === 'NORMAL' ? 'good' : 'warn')),
      facts([
        ['Status', yt.status],
        ['Mode', yt.mode || 'NORMAL'],
        ['Quota', `${yt.quota_used ?? 0} / ${yt.quota_limit ?? 0}`],
        ['Discovery budget', `${yt.budget_used ?? yt.quota_used ?? 0} / ${yt.discovery_budget ?? 0}`],
        ['Projected daily usage', yt.projected_daily_usage != null ? `${yt.projected_daily_usage} / ${yt.discovery_budget ?? 0}` : '—'],
        ['Next poll', yt.next_poll_at ? fmt(yt.next_poll_at) : '—'],
        ['Last fetch', fmt(yt.last_success)],
        ['Videos today', yt.videos_today ?? 0],
        ['Queries today', yt.queries_today ?? 0],
        ['Avg latency', yt.average_latency_ms != null ? `${Math.round(yt.average_latency_ms)} ms` : '—'],
      ]),
      (yt.last_queries?.length || yt.recent_video_ids?.length) ? disclosure('Advanced — YouTube debug detail',
        facts([['Last bucket', yt.last_bucket || '—'], ['Last error', yt.last_error || '—'],
               ['Errors today', yt.errors_today ?? 0], ['Interval', yt.interval_seconds != null ? `${yt.interval_seconds}s` : '—'],
               ['Quota remaining', yt.quota_remaining ?? 0], ['Paused reason', yt.paused_reason || '—']]),
        el('div', 'chip-row', (yt.last_queries || []).map(q => el('span', 'ref', q))),
        el('div', 'chip-row', (yt.recent_video_ids || []).map(v => el('span', 'ref', v)))) : null) : null,
    sourceRows.length ? disclosure('Advanced — parser & debug detail', parserDetail) : null));

  const discovered = state.discovered || [];
  if (discovered.length) {
    grid.append(card('', cardHead(`Discovered queue (${discovered.length})`),
      el('ul', 'list', discovered.slice(0, 6).map(d =>
        el('li', '', `${d.headline.slice(0, 60)} · ${d.source_count} signals · ${d.location || '—'} · ${label(d.verification_status)}`)))));
  }

  if (health.providers) grid.append(card('', cardHead('Provider health'),
    el('ul', 'provider-list', Object.entries(health.providers).map(([name, status]) =>
      el('li', '', el('span', '', label(name)), el('small', '', status))))));

  if (state.notifications?.length) grid.append(card('', cardHead('Notifications'),
    el('ul', 'list', state.notifications.slice(0, 8).map(n => el('li', '', n.message)))));

  root.append(grid);
}

/* ---------- boot ---------- */

const params = new URLSearchParams(window.location.search);
if (params.get('event')) setPage('story', params.get('event'), params.get('tab') || 'reel');
else if (params.get('page')) setPage(params.get('page'));
refresh();
setInterval(() => { if (state.page !== 'story') refresh(); }, 15000);
