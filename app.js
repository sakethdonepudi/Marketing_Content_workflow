const metrics = ['Views', 'Reach', 'Likes', 'Comments', 'Shares', 'Saves'];
const fmt = time => time ? new Date(time).toLocaleString(undefined, {
  month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit'
}) : 'Not stated';
let activeEventId = null;
let activeTab = 'overview';
let roomData = null;
let researchPoll = null;

function node(tag, className, text) {
  const item = document.createElement(tag);
  if (className) item.className = className;
  if (text !== undefined && text !== null) item.textContent = text;
  return item;
}

function fact(label, value) {
  const item = node('div', 'room-fact');
  item.append(node('small', '', label), node('strong', '', value || 'Unknown'));
  return item;
}

async function refresh() {
  try {
    const response = await fetch('/api/overview');
    const data = await response.json();
    document.getElementById('clock').textContent = fmt(data.updated_at);
    document.getElementById('event-count').textContent = `${data.events.length} event${data.events.length === 1 ? '' : 's'}`;
    document.getElementById('reference-count').textContent = data.reference_count;
    document.getElementById('review-count').textContent = data.review_count;
    document.getElementById('rejected-count').textContent = data.rejected_count;
    document.getElementById('discovered-event-count').textContent = data.events.length;
    document.getElementById('metrics').innerHTML = metrics.map(name => `<div class="metric"><label>${name}</label><strong class="empty">—</strong></div>`).join('');
    const grokOption = document.querySelector('#research-provider option[value="grok"]');
    grokOption.textContent = data.research?.grok_configured ? 'Grok · live paid call' : 'Grok · API key missing';
    const list = document.getElementById('event-list');
    list.replaceChildren();
    if (!data.events.length) {
      list.innerHTML = '<div class="empty-row">No detected events yet. Ingest a public report URL to test the source monitor.</div>';
      return;
    }
    for (const event of data.events) {
      const row = node('div', 'event');
      const sourceCount = Number(event.source_count) || 0;
      const titleCell = node('span', 'title');
      const open = node('button', 'title-button', event.title);
      open.type = 'button';
      open.title = 'Open Event Room';
      open.onclick = () => openEventRoom(event.id);
      titleCell.appendChild(open);
      row.appendChild(titleCell);
      const values = [
        [`${sourceCount} source${sourceCount === 1 ? '' : 's'}`, 'sources', (event.source_names || []).join(', ')],
        [event.priority, '', ''],
        [event.status, 'status', event.research_status || 'NOT_RESEARCHED'],
        [fmt(event.publication_time || event.event_time), '', '']
      ];
      for (const [value, className, title] of values) {
        const cell = node('span', className, value);
        if (title) cell.title = title;
        row.appendChild(cell);
      }
      list.appendChild(row);
    }
  } catch (error) {
    document.getElementById('event-list').textContent = 'Cannot reach the local API.';
  }
}

const latestSummary = data => data.runs.find(run => run.summary)?.summary || null;

function renderOverview(data, content) {
  const event = data.event;
  const summary = latestSummary(data);
  const grid = node('div', 'room-facts');
  grid.append(fact('EVENT STATUS', event.status), fact('RESEARCH STATUS', event.research_status), fact('EVENT TIME', fmt(event.event_time)), fact('LINKED SIGNALS', String(data.signals.length)));
  content.appendChild(grid);
  if (!summary) {
    content.appendChild(node('p', 'room-empty', 'No research run yet. Choose deterministic test data or explicitly select live Grok.'));
    return;
  }
  const latestRun = data.runs.find(run => run.summary);
  const report = node('div', 'research-summary');
  report.append(node('h4', '', 'What happened'), node('p', '', summary.what_happened || 'Unknown'));
  const timing = node('div', 'room-facts compact');
  timing.append(fact('PUBLICATION TIME', fmt(summary.publication_time)), fact('STATED EVENT TIME', fmt(summary.stated_event_time)), fact('OCCURRENCE TYPE', summary.occurrence_kind), fact('RELEVANCE', summary.relevance));
  report.appendChild(timing);
  if (summary.unknowns?.length) {
    report.appendChild(node('h4', '', 'Missing information'));
    const list = node('ul', 'plain-list');
    summary.unknowns.forEach(item => list.appendChild(node('li', '', item)));
    report.appendChild(list);
  }
  if (summary.contradictions?.length) {
    report.appendChild(node('h4', '', 'Contradictions'));
    const list = node('ul', 'plain-list warning');
    summary.contradictions.forEach(item => list.appendChild(node('li', '', item.description)));
    report.appendChild(list);
  }
  report.appendChild(node('p', 'policy-note', latestRun.verification_explanation));
  content.appendChild(report);
}

function renderSources(data, content) {
  if (!data.signals.length) return content.appendChild(node('p', 'room-empty', 'No event evidence is linked.'));
  data.signals.forEach(signal => {
    const card = node('article', 'source-card');
    const title = node('a', '', signal.title);
    title.href = signal.canonical_url;
    title.target = '_blank';
    title.rel = 'noopener noreferrer';
    card.append(title, node('p', 'source-meta', `${signal.registered_source_name} · ${signal.source_class.replace('_', ' ')} · published ${fmt(signal.publication_time)}`), node('p', 'source-excerpt', signal.text));
    content.appendChild(card);
  });
}

function renderClaims(data, content) {
  if (!data.claims.length) return content.appendChild(node('p', 'room-empty', 'No claims have been entered in the ledger.'));
  data.claims.forEach(claim => {
    const card = node('article', 'claim-card');
    const head = node('div', 'claim-head');
    head.append(node('span', `claim-status ${claim.verification_status.toLowerCase()}`, claim.verification_status), node('small', `mode ${claim.research_mode}`, claim.research_mode === 'live' ? 'LIVE' : 'TEST DATA'), node('small', '', claim.assertion_scope.replaceAll('_', ' ')));
    card.append(head, node('p', 'claim-text', claim.text), node('p', 'claim-attribution', claim.attribution ? `Attributed to ${claim.attribution}` : 'No attribution supplied'));
    claim.evidence.forEach(evidence => {
      const block = node('div', `claim-evidence ${evidence.validation_status === 'VALID' ? '' : 'invalid'}`);
      const link = node('a', '', evidence.source_name || evidence.source_url);
      link.href = evidence.source_url;
      link.target = '_blank';
      link.rel = 'noopener noreferrer';
      block.append(link, node('blockquote', '', evidence.supporting_excerpt || 'No excerpt supplied'), node('small', '', `${evidence.support_kind} · ${evidence.validation_status} · snapshot ${evidence.retrieved_at ? fmt(evidence.retrieved_at) : 'unavailable'}`));
      card.appendChild(block);
    });
    card.appendChild(node('p', 'review-note', claim.reviewer_notes));
    content.appendChild(card);
  });
}

function verificationCost(run) {
  if (run.cost_status === 'known' && run.cost_usd != null) return `$${Number(run.cost_usd).toFixed(6)}`;
  return run.cost_usd_ticks != null ? `${run.cost_usd_ticks} USD ticks · conversion unavailable` : 'unknown';
}

function renderVerification(data, content) {
  if (!data.verification_runs?.length) {
    content.appendChild(node('p', 'room-empty', 'No verification run yet. Complete research, then explicitly choose Find corroboration.'));
    return;
  }
  data.verification_runs.forEach(run => {
    const card = node('article', 'verification-run');
    const head = node('div', 'verification-head');
    head.append(node('strong', '', `${run.provider} / ${run.model}`), node('span', `mode ${run.mode}`, run.mode === 'live' ? 'LIVE' : 'TEST DATA'), node('span', '', run.status));
    card.appendChild(head);
    const meta = node('div', 'verification-meta');
    [
      ['CLAIM SET', run.summary?.claim_set_status || 'Pending'],
      ['TOOLS USED', `${run.actual_search_calls ?? 'unknown'} searches · ${run.actual_open_calls ?? 'unknown'} opens`],
      ['SOURCES RETURNED', String(run.actual_sources_returned ?? 'unknown')],
      ['TOKENS', String(run.total_tokens ?? 'unknown')],
      ['COST', verificationCost(run)],
      ['EVIDENCE VERSION', run.final_evidence_version ? run.final_evidence_version.slice(0, 12) : 'Pending'],
    ].forEach(([label, value]) => {
      const item = node('div'); item.append(node('span', '', label), node('strong', '', value)); meta.appendChild(item);
    });
    card.appendChild(meta);
    card.appendChild(node('p', 'limit-note', run.limit_notes));
    if (run.decision_explanation) card.appendChild(node('p', 'policy-note', run.decision_explanation));
    if (run.error_message) card.appendChild(node('p', 'history-error', run.error_message));
    run.decisions.forEach(decision => {
      const row = node('section', 'matrix-row');
      const decisionHead = node('div', 'claim-head');
      decisionHead.append(node('span', `claim-status ${decision.decision.toLowerCase()}`, decision.decision), node('small', '', decision.required_for_event ? 'REQUIRED' : 'OPTIONAL'), node('small', '', `claim v${decision.claim_version}`));
      row.append(decisionHead, node('p', 'claim-text', decision.claim_text), node('p', 'matrix-rationale', decision.rationale));
      decision.evidence.forEach(evidence => {
        const evidenceBlock = node('div', `matrix-evidence ${evidence.relationship}`);
        const link = node('a', '', evidence.source_name);
        link.href = evidence.canonical_url; link.target = '_blank'; link.rel = 'noopener noreferrer';
        evidenceBlock.append(link, node('blockquote', '', evidence.excerpt || 'No matching excerpt'), node('small', '', `${evidence.relationship} · ${evidence.directness.replaceAll('_', ' ')} · family ${evidence.evidence_family_id} · published ${fmt(evidence.publication_time)}`));
        row.appendChild(evidenceBlock);
      });
      if (decision.missing_information.length) {
        const missing = node('ul', 'plain-list warning');
        decision.missing_information.forEach(item => missing.appendChild(node('li', '', item)));
        row.appendChild(missing);
      }
      card.appendChild(row);
    });
    if (run.leads.length) {
      const leadList = node('ul', 'plain-list');
      run.leads.forEach(lead => leadList.appendChild(node('li', '', `${lead.status} · ${lead.url} · ${lead.status_reason}`)));
      card.append(node('h4', '', 'Discovery leads'), leadList);
    }
    content.appendChild(card);
  });
}

function renderHistory(data, content) {
  if (!data.runs.length && !data.verification_runs?.length) return content.appendChild(node('p', 'room-empty', 'No research or verification history.'));
  data.runs.forEach(run => {
    const card = node('article', 'history-row');
    const cost = run.cost_status === 'known' ? `$${run.cost_usd}` : run.cost_usd_ticks != null ? `${run.cost_usd_ticks} USD ticks` : 'unknown';
    card.append(node('strong', '', `${run.provider} / ${run.model}`), node('span', `mode ${run.mode}`, run.mode === 'live' ? 'LIVE' : 'TEST DATA'), node('span', '', run.status), node('span', '', `${run.progress}% · ${run.progress_message}`), node('small', '', `Requested ${fmt(run.requested_at)} · attempts ${run.attempt_count}/${run.max_attempts} · provider ${run.provider_elapsed_seconds != null ? `${run.provider_elapsed_seconds.toFixed(2)}s` : 'time unknown'}`), node('small', '', `Tokens ${run.total_tokens ?? 'unknown'} · searches ${run.search_count ?? 'unknown'} · cost ${cost}`));
    if (run.verification_explanation) card.appendChild(node('p', '', run.verification_explanation));
    if (run.error_message) card.appendChild(node('p', 'history-error', run.error_message));
    content.appendChild(card);
  });
  (data.verification_runs || []).forEach(run => {
    const card = node('article', 'history-row');
    card.append(node('strong', '', `Verification · ${run.provider} / ${run.model}`), node('span', `mode ${run.mode}`, run.mode === 'live' ? 'LIVE' : 'TEST DATA'), node('span', '', run.status), node('span', '', `${run.progress}% · ${run.progress_message}`), node('small', '', `Requested ${fmt(run.requested_at)} · attempts ${run.attempt_count}/${run.max_attempts} · provider ${run.provider_elapsed_seconds != null ? `${run.provider_elapsed_seconds.toFixed(2)}s` : 'time unknown'}`), node('small', '', `Tokens ${run.total_tokens ?? 'unknown'} · searches ${run.actual_search_calls ?? 'unknown'} · opens ${run.actual_open_calls ?? 'unknown'} · cost ${verificationCost(run)}`));
    if (run.decision_explanation) card.appendChild(node('p', '', run.decision_explanation));
    if (run.error_message) card.appendChild(node('p', 'history-error', run.error_message));
    content.appendChild(card);
  });
}

function renderRoom() {
  if (!roomData) return;
  document.getElementById('room-title').textContent = roomData.event.title;
  document.getElementById('room-meta').textContent = `${roomData.event.id} · ${roomData.event.status} · research ${roomData.event.research_status} · verification ${roomData.event.verification_status}`;
  const content = document.getElementById('room-content');
  content.replaceChildren();
  if (activeTab === 'overview') renderOverview(roomData, content);
  if (activeTab === 'sources') renderSources(roomData, content);
  if (activeTab === 'claims') renderClaims(roomData, content);
  if (activeTab === 'verification') renderVerification(roomData, content);
  if (activeTab === 'history') renderHistory(roomData, content);
  document.querySelectorAll('.room-tabs button').forEach(button => button.classList.toggle('active', button.dataset.tab === activeTab));
  const activeRun = roomData.runs.find(run => ['QUEUED', 'RUNNING'].includes(run.status)) || (roomData.verification_runs || []).find(run => ['QUEUED', 'RUNNING'].includes(run.status));
  document.getElementById('research-progress').textContent = activeRun ? `${activeRun.progress}% · ${activeRun.progress_message}` : '';
  document.getElementById('research-event').disabled = Boolean(activeRun);
  const latestResearch = roomData.runs.find(run => run.status === 'COMPLETED' && run.mode === 'live') || roomData.runs.find(run => run.status === 'COMPLETED');
  document.getElementById('find-corroboration').disabled = Boolean(activeRun) || !latestResearch;
  if (activeRun && !researchPoll) researchPoll = setInterval(() => loadEventRoom(false), 1000);
  if (!activeRun && researchPoll) { clearInterval(researchPoll); researchPoll = null; }
}

async function loadEventRoom(scroll = false) {
  if (!activeEventId) return;
  const response = await fetch(`/api/events/${encodeURIComponent(activeEventId)}`);
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || 'Could not load Event Room');
  roomData = result;
  renderRoom();
  if (scroll) document.getElementById('event-room').scrollIntoView({behavior: 'smooth', block: 'start'});
}

async function openEventRoom(eventId) {
  activeEventId = eventId;
  document.getElementById('event-room').hidden = false;
  try { await loadEventRoom(true); } catch (error) { document.getElementById('room-content').textContent = error.message; }
}

document.querySelectorAll('.room-tabs button').forEach(button => {
  button.onclick = () => { activeTab = button.dataset.tab; renderRoom(); };
});
document.getElementById('close-room').onclick = () => {
  activeEventId = null; roomData = null; document.getElementById('event-room').hidden = true;
  if (researchPoll) clearInterval(researchPoll);
  researchPoll = null;
};
document.getElementById('research-event').onclick = async () => {
  if (!activeEventId) return;
  const button = document.getElementById('research-event');
  const progress = document.getElementById('research-progress');
  const provider = document.getElementById('research-provider').value;
  const searchLimit = provider === 'grok' && document.getElementById('research-search').checked ? 1 : 0;
  button.disabled = true;
  progress.textContent = provider === 'grok' ? 'Starting explicit live research…' : 'Starting test research…';
  try {
    const response = await fetch(`/api/events/${encodeURIComponent(activeEventId)}/research`, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({provider, search_limit: searchLimit})});
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || 'Research could not start');
    progress.textContent = result.cached ? 'Evidence unchanged · cached result reused' : result.duplicate ? 'Research already in progress' : 'Research queued';
    await loadEventRoom(false);
  } catch (error) { progress.textContent = error.message; button.disabled = false; }
};
document.getElementById('find-corroboration').onclick = async () => {
  const run = roomData?.runs.find(item => item.status === 'COMPLETED' && item.mode === 'live') || roomData?.runs.find(item => item.status === 'COMPLETED');
  if (!run) return;
  const button = document.getElementById('find-corroboration');
  const progress = document.getElementById('research-progress');
  const provider = run.mode === 'live' ? 'grok' : 'test';
  button.disabled = true;
  progress.textContent = run.mode === 'live' ? 'Starting explicit paid corroboration search…' : 'Starting TEST DATA corroboration…';
  try {
    const response = await fetch(`/api/research/${encodeURIComponent(run.id)}/verify`, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({provider})});
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || 'Corroboration could not start');
    progress.textContent = result.cached ? 'Evidence and claims unchanged · cached decision reused' : result.duplicate ? 'Verification already in progress' : 'Corroboration queued';
    activeTab = 'verification';
    await loadEventRoom(false);
  } catch (error) { progress.textContent = error.message; button.disabled = false; }
};
document.getElementById('research-provider').onchange = event => {
  const search = document.getElementById('research-search');
  search.disabled = event.target.value !== 'grok';
  if (search.disabled) search.checked = false;
};
document.getElementById('research-search').disabled = true;

document.getElementById('ingest').onclick = async () => {
  const url = window.prompt('Public report URL to ingest');
  if (!url) return;
  const button = document.getElementById('ingest');
  const status = document.getElementById('ingest-status');
  button.disabled = true;
  status.textContent = 'Fetching and matching source…';
  try {
    const response = await fetch('/api/ingest-url', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({url})});
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || 'Ingestion failed');
    status.textContent = result.duplicate ? 'Already ingested — existing signal retained.' : result.reference ? 'Saved as reference material — no event created.' : result.review ? 'Held for review — the date or identity is uncertain.' : result.rejected ? 'Rejected from event discovery.' : result.clustered ? 'Signal added to an existing event.' : 'New event detected.';
    await refresh();
  } catch (error) { status.textContent = error.message; } finally { button.disabled = false; }
};

refresh();
setInterval(refresh, 15000);
