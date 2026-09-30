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
    const contentGrokOption = document.querySelector('#content-provider option[value="grok"]');
    if (contentGrokOption) contentGrokOption.textContent = data.content_ceo?.grok_configured ? 'Grok · live paid call' : 'Grok · API key missing';
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
  grid.append(fact('EVENT STATUS', event.status), fact('RESEARCH STATUS', event.research_status), fact('VERIFICATION', event.verification_status), fact('CONTENT DECISION', event.content_decision_status));
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

function renderContentDecision(data, content) {
  if (!data.content_decision_runs?.length) {
    content.appendChild(node('p', 'room-empty', 'No Content CEO decision yet. The evidence gate runs before any provider call.'));
    return;
  }
  data.content_decision_runs.forEach(run => {
    const record = run.decision_record;
    const card = node('article', 'verification-run content-decision-card');
    const head = node('div', 'verification-head');
    head.append(node('strong', '', record ? `${record.decision} · ${record.recommended_format}` : `${run.progress}% · ${run.progress_message}`), node('span', `mode ${run.mode}`, run.mode === 'live' ? 'LIVE' : 'TEST / DETERMINISTIC'), node('span', '', run.status));
    card.appendChild(head);
    const meta = node('div', 'verification-meta');
    [
      ['ELIGIBILITY', run.eligibility_status],
      ['EXECUTABLE', record ? (record.executable ? 'Yes' : 'No') : 'Pending'],
      ['LANGUAGE', record?.language || 'Pending'],
      ['DURATION', record ? `${record.proposed_duration_seconds}s` : 'Pending'],
      ['PRIORITY', record?.priority || 'Pending'],
      ['POLICY', record?.policy_version || 'Pending'],
    ].forEach(([label, value]) => {
      const item = node('div'); item.append(node('span', '', label), node('strong', '', value)); meta.appendChild(item);
    });
    card.appendChild(meta);
    if (record) {
      card.appendChild(node('p', 'claim-text', record.factual_rationale));
      card.appendChild(node('p', 'limit-note', `Claim set v${record.approved_claim_set_version ?? 'none'} · evidence ${record.evidence_version ? record.evidence_version.slice(0, 16) : 'none'} · input ${record.input_version.slice(0, 16)}`));
      if (record.test_only) card.appendChild(node('p', 'policy-note', 'TEST / deterministic decision. It cannot enqueue or execute production work.'));
      if (record.missing_evidence_or_media.length) {
        const blockers = node('ul', 'plain-list warning');
        record.missing_evidence_or_media.forEach(item => blockers.appendChild(node('li', '', item)));
        card.append(node('h4', '', 'Blockers'), blockers);
      }
    }
    const cost = run.cost_status === 'known' && run.cost_usd != null ? `$${Number(run.cost_usd).toFixed(6)}` : 'unknown';
    card.appendChild(node('p', 'limit-note', `${run.provider} / ${run.model} · provider called ${run.provider_called ? 'yes' : 'no'} · tokens ${run.total_tokens ?? 'unknown'} · cost ${cost}`));
    if (run.error_message) card.appendChild(node('p', 'history-error', run.error_message));
    content.appendChild(card);
  });
}

function renderProduction(data, content) {
  const gate = data.production_gate || {eligible: false, blockers: ['Production gate unavailable.']};
  const gateCard = node('article', `verification-run production-gate ${gate.eligible ? 'gate-pass' : 'gate-blocked'}`);
  gateCard.append(node('strong', '', gate.eligible ? 'Eligible for content production' : 'Production blocked'));
  if (!gate.eligible) {
    const list = node('ul', 'plain-list warning');
    (gate.blockers || []).forEach(item => list.appendChild(node('li', '', item)));
    gateCard.appendChild(list);
  } else {
    gateCard.appendChild(node('p', 'limit-note', 'Current executable CREATE decision, claim set, evidence snapshots, and rights-cleared media passed the deterministic entry gate.'));
  }
  content.appendChild(gateCard);
  if (!data.production_jobs?.length) {
    content.appendChild(node('p', 'room-empty', 'No production job. Anthropic is called only after the explicit Generate action.'));
    renderMediaRendering(data, content);
    return;
  }
  data.production_jobs.forEach(job => {
    const card = node('article', 'verification-run production-job-card');
    const head = node('div', 'verification-head');
    head.append(
      node('strong', '', `${job.status} · ${job.requested_format}`),
      node('span', `mode ${job.fixture_only ? 'test' : 'live'}`, job.fixture_only ? 'FIXTURE PROVIDER' : 'LIVE'),
      node('span', '', `v${job.regeneration_number}`),
    );
    card.appendChild(head);
    const meta = node('div', 'verification-meta');
    const cost = job.cost_status === 'known' && job.cost_usd != null ? `$${Number(job.cost_usd).toFixed(6)}` : 'unknown';
    [
      ['PROVIDER', `${job.provider} / ${job.model}`],
      ['VALIDATION', job.validation_status],
      ['TOKENS', String(job.total_tokens ?? 'unknown')],
      ['LATENCY', job.latency_ms != null ? `${job.latency_ms}ms` : 'unknown'],
      ['COST', cost],
      ['CLAIM SET', `v${job.approved_claim_set_version}`],
    ].forEach(([label, value]) => {
      const item = node('div'); item.append(node('span', '', label), node('strong', '', value)); meta.appendChild(item);
    });
    card.appendChild(meta);
    if (job.fixture_only) card.appendChild(node('p', 'policy-note', 'Controlled fixture provider. This is not a live Anthropic result and no publishing action exists.'));
    if (job.error_message) card.appendChild(node('p', 'history-error', job.error_message));
    const pkg = job.package?.package;
    if (pkg) {
      card.append(node('h4', '', pkg.story_angle), node('p', 'claim-text', pkg.content_objective));
      [['Headline', pkg.headline], ['Hook', pkg.hook], ['Caption', pkg.caption]].forEach(([label, block]) => {
        const section = node('section', 'package-block');
        section.append(node('small', '', label.toUpperCase()), node('p', '', block.text), node('code', '', block.claim_version_ids.join(', ')));
        card.appendChild(section);
      });
      const script = node('ol', 'plain-list package-script');
      pkg.script.forEach(line => {
        const item = node('li'); item.append(node('span', '', line.text), node('code', '', line.claim_version_ids.join(', '))); script.appendChild(item);
      });
      card.append(node('h4', '', 'Script'), script);
      const storyboard = node('div', 'storyboard-grid');
      pkg.storyboard.forEach(scene => {
        const sceneCard = node('section', 'package-block');
        sceneCard.append(node('small', '', `SCENE ${scene.scene_number} · ${scene.duration_seconds}s`), node('p', '', scene.narration), node('p', 'limit-note', scene.visual_prompt), node('code', '', scene.claim_version_ids.join(', ')));
        storyboard.appendChild(sceneCard);
      });
      card.append(node('h4', '', 'Storyboard and visual prompts'), storyboard);
      card.appendChild(node('p', 'limit-note', `Claim versions ${job.package.approved_claim_version_ids.join(', ')} · evidence snapshots ${job.package.evidence_snapshot_ids.join(', ')}`));
      card.appendChild(node('p', 'limit-note', `Evidence version ${job.evidence_version.slice(0, 16)} · media version ${job.media_version.slice(0, 16)} · schema ${job.prompt_schema_version}`));
    }
    if (job.history?.length) {
      const history = node('ol', 'plain-list production-history');
      job.history.forEach(item => history.appendChild(node('li', '', `${item.to_status.replaceAll('_', ' ')} · ${fmt(item.changed_at)} · ${item.message}`)));
      card.append(node('h4', '', 'Job audit'), history);
    }
    content.appendChild(card);
  });
  renderMediaRendering(data, content);
}

function renderMediaRendering(data, content) {
  content.appendChild(node('h3', 'production-section-title', 'Media rendering'));
  const gate = data.render_gate || {eligible: false, blockers: ['Render gate unavailable.']};
  const gateCard = node('article', `verification-run production-gate ${gate.eligible ? 'gate-pass' : 'gate-blocked'}`);
  gateCard.appendChild(node('strong', '', gate.eligible ? 'Eligible for media generation' : 'Media generation blocked'));
  if (!gate.eligible) {
    const list = node('ul', 'plain-list warning');
    (gate.blockers || []).forEach(item => list.appendChild(node('li', '', item)));
    gateCard.appendChild(list);
  } else if (!gate.live_renderer_configured) {
    gateCard.appendChild(node('p', 'policy-note', `${gate.renderer_configuration_status || 'LIVE_RENDERER_NOT_CONFIGURED'} · Package lineage is eligible, but no live ${gate.media_type || ''} renderer is available. Fixture rendering is limited to isolated tests and demonstrations.`));
  } else {
    gateCard.appendChild(node('p', 'limit-note', `${gate.configured_provider} is configured for explicit ${gate.media_type} rendering. Human review remains mandatory.`));
  }
  content.appendChild(gateCard);
  if (!data.render_jobs?.length) {
    content.appendChild(node('p', 'room-empty', 'No RenderJob or generated asset exists. Rendering never starts automatically.'));
    return;
  }
  data.render_jobs.forEach(job => {
    const card = node('article', 'verification-run render-job-card');
    const head = node('div', 'verification-head');
    head.append(
      node('strong', '', `${job.status} · ${job.media_type}`),
      node('span', `mode ${job.fixture_only ? 'test' : 'live'}`, job.fixture_only ? 'DETERMINISTIC FIXTURE' : 'LIVE'),
      node('span', '', `render v${job.regeneration_number}`),
    );
    card.appendChild(head);
    const cost = job.cost_status === 'known'
      ? `${job.currency || 'USD'} ${Number(job.provider_cost_usd ?? job.calculated_cost_usd).toFixed(6)}` : 'unknown';
    const meta = node('div', 'verification-meta');
    [
      ['RENDER JOB', job.id], ['PROVIDER', `${job.provider} / ${job.model}`],
      ['PROVIDER JOB', job.provider_job_id || 'synchronous / unavailable'],
      ['PROVIDER STATUS', job.provider_status || 'not reported'],
      ['POLLS', `${job.poll_count || 0}${job.last_polled_at ? ` · ${fmt(job.last_polled_at)}` : ''}`],
      ['SUBMITTED', fmt(job.submitted_at)],
      ['TECHNICAL QA', job.technical_validation_status], ['TEXT QA', job.text_validation_status],
      ['SEMANTIC QA', job.semantic_qa_status], ['HUMAN REVIEW', job.human_review_status],
      ['LATENCY', job.latency_ms != null ? `${job.latency_ms}ms` : 'unknown'],
      ['COST', cost], ['ATTEMPTS', String(Math.max(1, ...job.attempts.map(item => item.attempt_number)))],
    ].forEach(([label, value]) => {
      const item = node('div'); item.append(node('span', '', label), node('strong', '', value)); meta.appendChild(item);
    });
    card.appendChild(meta);
    if (job.fixture_only) card.appendChild(node('p', 'policy-note', 'Deterministic fixture asset. It is not a live renderer output and is not executable for publication.'));
    if (job.failure_reason) card.appendChild(node('p', 'history-error', job.failure_reason));
    job.assets.forEach(asset => {
      const assetCard = node('section', 'generated-asset-card');
      assetCard.append(node('strong', '', `${asset.id} · asset v${asset.version_number}`));
      if (asset.media_type === 'IMAGE') {
        const preview = document.createElement('img');
        preview.className = 'generated-preview'; preview.alt = 'Generated media preview';
        preview.src = `/api/generated-assets/${encodeURIComponent(asset.id)}/content`;
        assetCard.appendChild(preview);
      } else if (['VIDEO', 'SHORT_FORM_VIDEO', 'LONG_FORM_VIDEO'].includes(asset.media_type)) {
        const preview = document.createElement('video'); preview.controls = true; preview.src = `/api/generated-assets/${encodeURIComponent(asset.id)}/content`; assetCard.appendChild(preview);
      } else if (['AUDIO', 'VOICEOVER'].includes(asset.media_type)) {
        const preview = document.createElement('audio'); preview.controls = true; preview.src = `/api/generated-assets/${encodeURIComponent(asset.id)}/content`; assetCard.appendChild(preview);
      }
      assetCard.appendChild(node('p', 'limit-note', `${asset.mime_type} · ${asset.width || '—'}×${asset.height || '—'} · ${asset.file_size} bytes · technical ${asset.technical_validation_status} · text ${asset.text_validation_status} · semantic ${asset.semantic_qa_status}`));
      assetCard.appendChild(node('code', '', `SHA-256 ${asset.checksum_sha256}`));
      assetCard.appendChild(node('p', 'limit-note', `Package ${asset.content_package_id} v${asset.content_package_version} · prompt ${asset.prompt_version} · generated_by_ai ${asset.provenance.generated_by_ai} · stale ${asset.stale ? 'yes' : 'no'} · usable for review ${asset.usable_for_review ? 'yes' : 'no'}`));
      assetCard.appendChild(node('p', 'policy-note', 'Human review required. READY_FOR_REVIEW is not approval to publish.'));
      card.appendChild(assetCard);
    });
    if (job.qa_results?.length) {
      const qaGrid = node('div', 'qa-grid');
      job.qa_results.forEach(qa => {
        const qaCard = node('section', `package-block qa-${qa.status.toLowerCase()}`);
        qaCard.append(node('small', '', qa.qa_type.replaceAll('_', ' ')), node('strong', '', qa.status));
        if (qa.flags?.length) qaCard.appendChild(node('p', 'limit-note', qa.flags.join(' · ')));
        if (qa.provider) qaCard.appendChild(node('p', 'limit-note', `${qa.provider} / ${qa.model || 'model unknown'}`));
        qaGrid.appendChild(qaCard);
      });
      card.append(node('h4', '', 'Media QA'), qaGrid);
    }
    if (job.provider_events?.length) {
      const providerHistory = node('ol', 'plain-list production-history');
      job.provider_events.forEach(item => providerHistory.appendChild(node('li', '', `${item.event_type.replaceAll('_', ' ')} · ${item.provider_status || 'status unavailable'} · ${fmt(item.occurred_at)}`)));
      card.append(node('h4', '', 'Provider lifecycle'), providerHistory);
    }
    if (job.prompt_snapshot) card.appendChild(node('p', 'limit-note', `Prompt snapshot ${job.prompt_snapshot.id} · ${job.prompt_snapshot.request_hash} · config ${job.prompt_snapshot.generation_config_version}`));
    const history = node('ol', 'plain-list production-history');
    job.history.forEach(item => history.appendChild(node('li', '', `${item.to_status.replaceAll('_', ' ')} · ${fmt(item.changed_at)} · ${item.message}`)));
    card.append(node('h4', '', 'Render audit'), history);
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
  (data.content_decision_runs || []).forEach(run => {
    const record = run.decision_record;
    const card = node('article', 'history-row');
    card.append(node('strong', '', `Content CEO · ${run.provider} / ${run.model}`), node('span', `mode ${run.mode}`, run.mode === 'live' ? 'LIVE' : 'TEST'), node('span', '', run.status), node('span', '', record ? `${record.decision} · ${record.recommended_format}` : run.progress_message), node('small', '', `Eligibility ${run.eligibility_status} · input ${run.input_version.slice(0, 12)} · provider called ${run.provider_called ? 'yes' : 'no'}`));
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
  if (activeTab === 'content') renderContentDecision(roomData, content);
  if (activeTab === 'production') renderProduction(roomData, content);
  if (activeTab === 'history') renderHistory(roomData, content);
  document.querySelectorAll('.room-tabs button').forEach(button => button.classList.toggle('active', button.dataset.tab === activeTab));
  const activeProduction = (roomData.production_jobs || []).find(job => ['QUEUED', 'GENERATING', 'VALIDATING'].includes(job.status));
  const activeRender = (roomData.render_jobs || []).find(job => ['QUEUED', 'PREPARING', 'RENDERING', 'VALIDATING'].includes(job.status));
  const activeResearchRun = roomData.runs.find(run => ['QUEUED', 'RUNNING'].includes(run.status)) || (roomData.verification_runs || []).find(run => ['QUEUED', 'RUNNING'].includes(run.status));
  const activeContentRun = (roomData.content_decision_runs || []).find(run => ['QUEUED', 'RUNNING'].includes(run.status));
  const activeRun = activeResearchRun || activeContentRun || activeProduction || activeRender;
  document.getElementById('research-progress').textContent = activeResearchRun ? `${activeResearchRun.progress}% · ${activeResearchRun.progress_message}` : '';
  document.getElementById('content-progress').textContent = activeContentRun ? `${activeContentRun.progress}% · ${activeContentRun.progress_message}` : '';
  document.getElementById('production-progress').textContent = activeProduction ? activeProduction.status.replaceAll('_', ' ') : '';
  document.getElementById('render-progress').textContent = activeRender ? activeRender.status.replaceAll('_', ' ') : '';
  document.getElementById('research-event').disabled = Boolean(activeRun);
  const latestResearch = roomData.runs.find(run => run.status === 'COMPLETED' && run.mode === 'live') || roomData.runs.find(run => run.status === 'COMPLETED');
  document.getElementById('find-corroboration').disabled = Boolean(activeRun) || !latestResearch;
  document.getElementById('decide-content').disabled = Boolean(activeRun);
  const hasPackage = (roomData.production_jobs || []).some(job => Boolean(job.package));
  const hasAsset = (roomData.render_jobs || []).some(job => job.assets?.length);
  document.getElementById('generate-package').textContent = hasPackage ? 'Regenerate package' : 'Generate content package';
  document.getElementById('generate-media').textContent = hasAsset ? 'Regenerate media' : 'Generate media';
  document.getElementById('generate-package').disabled = Boolean(activeRun) || !roomData.production_gate?.eligible;
  document.getElementById('generate-media').disabled = Boolean(activeRun) || !roomData.render_gate?.eligible || !roomData.render_gate?.live_renderer_configured;
  document.getElementById('renderer-config').textContent = roomData.render_gate?.live_renderer_configured
    ? ` ${roomData.render_gate.configured_provider} · explicit generation · human review required`
    : ` ${roomData.render_gate?.renderer_configuration_status || 'LIVE_RENDERER_NOT_CONFIGURED'} · fixtures stay isolated`;
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
document.getElementById('decide-content').onclick = async () => {
  if (!activeEventId) return;
  const button = document.getElementById('decide-content');
  const progress = document.getElementById('content-progress');
  const provider = document.getElementById('content-provider').value;
  button.disabled = true;
  progress.textContent = provider === 'grok' ? 'Applying evidence gate before live Content CEO…' : 'Applying deterministic Content CEO policy…';
  try {
    const response = await fetch(`/api/events/${encodeURIComponent(activeEventId)}/content-decision`, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({provider})});
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || 'Content decision could not start');
    progress.textContent = result.cached ? 'Inputs unchanged · cached decision reused' : result.duplicate ? 'Content decision already in progress' : 'Content decision queued';
    activeTab = 'content';
    await loadEventRoom(false);
  } catch (error) { progress.textContent = error.message; button.disabled = false; }
};
document.getElementById('generate-package').onclick = async () => {
  const decisionId = roomData?.production_gate?.content_decision_id;
  if (!decisionId) return;
  const regenerate = (roomData?.production_jobs || []).some(job => Boolean(job.package));
  if (regenerate && !window.confirm('Generate a new immutable package version from the current approved inputs?')) return;
  const button = document.getElementById('generate-package');
  const progress = document.getElementById('production-progress');
  button.disabled = true;
  progress.textContent = 'Capturing immutable inputs before Anthropic…';
  try {
    const response = await fetch(`/api/content-decisions/${encodeURIComponent(decisionId)}/production`, {
      method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({provider: 'anthropic', regenerate}),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || 'Content production could not start');
    progress.textContent = result.cached ? 'Existing package version retained' : result.duplicate ? 'Production already in progress' : 'Production queued';
    activeTab = 'production';
    await loadEventRoom(false);
  } catch (error) { progress.textContent = error.message; button.disabled = false; }
};
document.getElementById('generate-media').onclick = async () => {
  const packageId = roomData?.render_gate?.content_package_id;
  if (!packageId) return;
  const regenerate = (roomData?.render_jobs || []).some(job => job.assets?.length);
  if (regenerate && !window.confirm('Generate a new media version? The current asset and audit history will be retained.')) return;
  const button = document.getElementById('generate-media');
  const progress = document.getElementById('render-progress');
  button.disabled = true;
  progress.textContent = 'Rechecking package, evidence, and media rights…';
  try {
    const response = await fetch(`/api/content-packages/${encodeURIComponent(packageId)}/render`, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        media_type: roomData.render_gate.media_type,
        provider: roomData.render_gate.configured_provider,
        regenerate,
      }),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || 'Media rendering could not start');
    progress.textContent = result.cached ? 'Existing generated asset retained' : result.duplicate ? 'Rendering already in progress' : 'Render queued';
    activeTab = 'production';
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

const previewParams = new URLSearchParams(window.location.search);
const previewEvent = previewParams.get('event');
const previewTab = previewParams.get('tab');
if (previewTab && ['overview', 'sources', 'claims', 'verification', 'content', 'production', 'history'].includes(previewTab)) activeTab = previewTab;
refresh();
if (previewEvent) openEventRoom(previewEvent);
setInterval(refresh, 15000);
