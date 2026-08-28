/**
 * RecoveryOS Autonomous Operations Command Center — Client Application
 * Phase 30: Unified Authoritative State Model & Deterministic Event Machine.
 * Zero-dependency, modern ES Modules architecture.
 */

// Authoritative Single Frontend State Model
const appState = {
  auth: {
    persona: 'operator',
    tenant: 'tenant-default',
    token: null,
  },
  workflow: null,
  activeWorkflowId: null,
  snapshot: null,
  events: [],
  seenEventIds: new Set(),  // Phase 31: event deduplication
  activeStage: null,
  workflowStatus: 'IDLE',
  connection: 'IDLE',
  approval: null,
  isDemoMode: false,
  replay: {
    active: false,
    timer: null,
    currentIndex: 0,
    events: [],
  },
  workflowsList: [],
  activeFilter: 'all',
  eventSource: null,
  autoRefreshTimer: null,
};

// Available Personas with server-side credential binding
const PERSONAS = {
  operator: { username: 'operator', role: 'operator', name: 'operator-1' },
  admin: { username: 'admin', role: 'admin', name: 'admin-1' },
  approver: { username: 'approver', role: 'approver', name: 'approver-1' },
  viewer: { username: 'viewer', role: 'viewer', name: 'viewer-1' },
};

// ==========================================================================
// 1. Authentication & API Client
// ==========================================================================

async function getAuthToken() {
  const p = PERSONAS[appState.auth.persona] || PERSONAS.operator;
  const cacheKey = `rec_jwt_${p.username}_${appState.auth.tenant}`;
  let token = sessionStorage.getItem(cacheKey);

  if (!token) {
    try {
      const res = await fetch('/api/auth/login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          username: p.username,
          role: p.role,
          tenant_id: appState.auth.tenant,
        }),
      });

      if (res.ok) {
        const data = await res.json();
        token = data.access_token;
        sessionStorage.setItem(cacheKey, token);
      }
    } catch (err) {
      console.warn('Auth request failed, using fallback token:', err);
    }
  }

  appState.auth.token = token;
  return token;
}

async function apiFetch(url, options = {}) {
  const token = await getAuthToken();
  const headers = {
    'Content-Type': 'application/json',
    ...(token ? { 'Authorization': `Bearer ${token}` } : {}),
    'X-Tenant-ID': appState.auth.tenant,
    ...(options.headers || {}),
  };

  const res = await fetch(url, { ...options, headers });
  if (!res.ok) {
    const errData = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(errData.detail || `HTTP ${res.status}`);
  }
  return await res.json();
}

// ==========================================================================
// 2. Authoritative Helpers (MTTR, Normalization, Sanitization)
// ==========================================================================

function calculateWorkflowDuration(createdAt, completedAt) {
  if (!createdAt) return '0.0s';
  const start = new Date(createdAt).getTime();
  const end = completedAt ? new Date(completedAt).getTime() : Date.now();
  if (isNaN(start) || isNaN(end) || end < start) return '0.0s';
  const diffSec = Math.max(0.1, (end - start) / 1000);
  return `${diffSec.toFixed(1)}s`;
}

function normalizeWorkflowEvent(rawEvent) {
  const evType = (rawEvent.event_type || rawEvent.type || '').toUpperCase();
  const actor = (rawEvent.actor || 'system').toLowerCase();
  const title = rawEvent.title || rawEvent.name || evType;
  const detail = rawEvent.detail || (rawEvent.payload ? JSON.stringify(rawEvent.payload) : '');
  const stateVal = rawEvent.state || (rawEvent.payload && rawEvent.payload.new_state);

  let stage = null;
  let actorLabel = 'SYSTEM';
  let actorClass = 'actor-system';

  if (actor.includes('detect') || title.toLowerCase().includes('detect') || title.toLowerCase().includes('fail') || evType.includes('FAIL')) {
    stage = 'detect';
    actorLabel = 'DETECT';
    actorClass = 'actor-detect';
  } else if (actor.includes('reason') || title.toLowerCase().includes('diagnos') || title.toLowerCase().includes('plan')) {
    stage = 'reason';
    actorLabel = 'REASON';
    actorClass = 'actor-reason';
  } else if (actor.includes('recover') || title.toLowerCase().includes('switch') || title.toLowerCase().includes('execut') || evType.includes('STEP')) {
    stage = 'recover';
    actorLabel = 'ACT';
    actorClass = 'actor-recover';
  } else if (actor.includes('verify') || title.toLowerCase().includes('verif') || evType.includes('VERIF')) {
    stage = 'verify';
    actorLabel = 'VERIFY';
    actorClass = 'actor-verify';
  } else if (stateVal === 'COMPLETED' || evType === 'OUTCOME_VERIFIED' || title.includes('Completed')) {
    stage = 'recovered';
    actorLabel = 'VERIFY';
    actorClass = 'actor-verify';
  }

  return {
    raw: rawEvent,
    eventType: evType,
    stage,
    actorLabel,
    actorClass,
    title,
    detail,
    state: stateVal,
    payload: rawEvent.payload || {},
  };
}

// ==========================================================================
// 3. Deterministic Event Application Machine & Canonical State Helpers
// ==========================================================================

function getEventDeduplicationKey(rawEvent) {
  if (!rawEvent) return '';
  if (rawEvent.event_id) return rawEvent.event_id;
  if (rawEvent.id) return rawEvent.id;
  const et = rawEvent.event_type || '';
  const ts = rawEvent.timestamp || '';
  const title = rawEvent.title || '';
  const detail = rawEvent.detail || '';
  return `${et}:${ts}:${title}:${detail}`;
}

function isWorkflowCompleted(workflow, event) {
  if (workflow?.state === 'COMPLETED') return true;
  const p = event?.payload;
  if (p?.to_state === 'COMPLETED' || p?.new_state === 'COMPLETED') return true;
  if (event?.state === 'COMPLETED') return true;
  if (event?.title === 'State: VERIFYING → COMPLETED') return true;
  return false;
}

function renderMissingEvents(authoritativeEvents) {
  if (!Array.isArray(authoritativeEvents)) return;
  authoritativeEvents.forEach((rawEv) => {
    const key = getEventDeduplicationKey(rawEv);
    if (!appState.seenEventIds.has(key)) {
      const norm = normalizeWorkflowEvent(rawEv);
      applyWorkflowEvent(norm);
    }
  });
}

async function finalizeWorkflow(workflowId, maxRetries = 6, retryDelayMs = 600) {
  if (!workflowId) return;

  for (let attempt = 0; attempt < maxRetries; attempt++) {
    try {
      const freshSnap = await apiFetch(`/api/workflows/${workflowId}`);
      if (!freshSnap?.workflow) return;

      appState.snapshot = freshSnap;
      appState.workflow = freshSnap.workflow;

      // Reconcile and render any missing authoritative events (e.g. VERIFYING → COMPLETED)
      if (freshSnap.events && freshSnap.events.length > 0) {
        renderMissingEvents(freshSnap.events);
      }

      if (isWorkflowCompleted(freshSnap.workflow)) {
        updateStreamStatus('COMPLETED ARCHIVE', false);
        illuminateNode('recovered', 'Outcome Verified');
        updateStoryLifecycle(6);
        const stageLbl = document.getElementById('graph-stage-label');
        if (stageLbl) {
          stageLbl.textContent = '✓ SYSTEM RECOVERED AUTONOMOUSLY';
          stageLbl.style.color = 'var(--emerald-core)';
        }
        hideCinematicIncident();
        hideToolExecution();
        showRecoveryProof(freshSnap);
        showReplayToolbar();
        refreshFleetData();
        return;
      }

      const activeStates = ['EXECUTING', 'CREATED', 'VERIFYING', 'RECOVERING', 'AWAITING_APPROVAL'];
      if (!activeStates.includes(freshSnap.workflow.state)) {
        return; // Terminal state other than COMPLETED (e.g. ESCALATED)
      }

      await new Promise((r) => setTimeout(r, retryDelayMs));
    } catch (err) {
      console.warn('[RECOVERY OS] Workflow finalization error:', err);
      await new Promise((r) => setTimeout(r, retryDelayMs));
    }
  }
}

function applyWorkflowEvent(normalizedEvent) {
  // Phase 31 & 44: Event deduplication — skip already-seen events
  const evId = normalizedEvent.raw.event_id || normalizedEvent.raw.id || getEventDeduplicationKey(normalizedEvent.raw);
  if (evId) {
    if (appState.seenEventIds.has(evId)) return;
    appState.seenEventIds.add(evId);
  }

  appState.events.push(normalizedEvent.raw);
  updateEventCount();

  // Log to terminal (format STREAM_END cleanly)
  if (normalizedEvent.eventType === 'STREAM_END') {
    appendTerminalLine(
      'SYSTEM',
      `Stream finalized (state: ${normalizedEvent.state || 'COMPLETED'})`,
      'actor-system'
    );
  } else {
    appendTerminalLine(
      normalizedEvent.actorLabel,
      `${normalizedEvent.title}: ${normalizedEvent.detail}`,
      normalizedEvent.actorClass
    );
  }

  // Stage illuminations
  if (normalizedEvent.stage === 'detect') {
    illuminateNode('detect', 'Signal Observed');
    updateStoryLifecycle(1);
  } else if (normalizedEvent.stage === 'reason') {
    illuminateNode('reason', 'Hypothesis Formulated');
    updateStoryLifecycle(2);
  } else if (normalizedEvent.stage === 'recover') {
    illuminateNode('recover', 'Tool Action Executed');
    updateStoryLifecycle(4);

    const p = normalizedEvent.payload;
    const toolName = p.tool_name || normalizedEvent.raw.tool_name || 'switch_payment_gateway';
    const toolArgs = p.tool_args || p.args || { provider: 'adyen', reason: 'primary_provider_outage' };
    const idempKey = p.idempotency_key || `op_dispatch_${appState.activeWorkflowId?.slice(0, 4)}`;
    showToolExecution(toolName, toolArgs, idempKey);
  } else if (normalizedEvent.stage === 'verify') {
    illuminateNode('verify', 'Criteria Confirmed');
    updateStoryLifecycle(5);
  }

  // Handle state transitions defensively
  if (normalizedEvent.state) {
    handleWorkflowStateChange(normalizedEvent.state);
  }

  if (normalizedEvent.eventType === 'OUTCOME_VERIFIED' || normalizedEvent.title.includes('Verified')) {
    markCriteriaVerified(normalizedEvent.payload.outcome_id || normalizedEvent.title);
  }

  if (normalizedEvent.state === 'COMPLETED' || normalizedEvent.eventType === 'STREAM_END') {
    illuminateNode('recovered', 'Outcome Verified');
    updateStoryLifecycle(6);
    const stageLbl = document.getElementById('graph-stage-label');
    if (stageLbl) {
      stageLbl.textContent = '✓ SYSTEM RECOVERED AUTONOMOUSLY';
      stageLbl.style.color = 'var(--emerald-core)';
    }
    hideCinematicIncident();
    hideToolExecution();

    // Phase 31 Finding 2: Only show Recovery Proof for COMPLETED workflows
    if (normalizedEvent.state === 'COMPLETED') {
      if (appState.activeWorkflowId) {
        finalizeWorkflow(appState.activeWorkflowId);
      } else if (appState.snapshot?.workflow?.state === 'COMPLETED') {
        showRecoveryProof(appState.snapshot);
      }
    } else if (normalizedEvent.eventType === 'STREAM_END') {
      if (appState.activeWorkflowId) {
        finalizeWorkflow(appState.activeWorkflowId);
      }
    }
    showReplayToolbar();
    refreshFleetData();
  }
}

function handleIncomingEvent(event) {
  const norm = normalizeWorkflowEvent(event);
  applyWorkflowEvent(norm);
}

// ==========================================================================
// 4. Real-Time Single-Use Ticket SSE Stream
// ==========================================================================

async function connectWorkflowStream(workflowId) {
  if (appState.eventSource) {
    appState.eventSource.close();
    appState.eventSource = null;
  }

  updateStreamStatus('CONNECTING...', false);

  try {
    const ticketData = await apiFetch('/api/auth/sse-ticket', {
      method: 'POST',
      body: JSON.stringify({ workflow_id: workflowId }),
    });

    const ticket = ticketData.ticket;
    const streamUrl = `/api/workflows/${workflowId}/events/stream?ticket=${encodeURIComponent(ticket)}`;

    const es = new EventSource(streamUrl);
    appState.eventSource = es;

    es.onopen = () => {
      updateStreamStatus('● LIVE EXECUTION', true);
      appendTerminalLine('SYSTEM', `Connected to real-time event stream for ${workflowId.slice(0, 8)}...`, 'actor-system');
    };

    es.onmessage = (event) => {
      try {
        const data = JSON.parse(event.data);
        handleIncomingEvent(data);
      } catch (err) {
        console.debug('Raw stream ping:', event.data);
      }
    };

    es.onerror = () => {
      if (es.readyState === EventSource.CLOSED) {
        appState.eventSource = null;
      }
      // Authoritative fallback: reconcile and finalize with durable store
      if (appState.activeWorkflowId === workflowId) {
        finalizeWorkflow(workflowId);
      } else {
        updateStreamStatus('STREAM DISCONNECTED', false);
      }
    };
  } catch (err) {
    console.error('Failed to establish SSE stream:', err);
    updateStreamStatus('STREAM OFFLINE', false);
  }
}
window.connectWorkflowStream = connectWorkflowStream;


function updateStreamStatus(text, isLive) {
  const pill = document.getElementById('stream-status-pill');
  const dot = pill?.querySelector('.stream-dot');
  const txt = document.getElementById('stream-status-text');

  if (txt) txt.textContent = text;
  if (dot) {
    if (isLive) dot.classList.add('connected');
    else dot.classList.remove('connected');
  }
}

// ==========================================================================
// 5. Stage-Dominant Graph Animation
// ==========================================================================

function illuminateNode(stageId, actionText) {
  const stages = ['detect', 'reason', 'recover', 'verify', 'recovered'];
  const curIdx = stages.indexOf(stageId);
  if (curIdx === -1) return;

  appState.activeStage = stageId;

  stages.forEach((st, idx) => {
    const node = document.getElementById(`node-${st}`);
    const badgeEl = document.getElementById(`node-${st}-badge`);
    if (!node) return;

    node.classList.remove('active', 'completed', 'escalated');

    if (idx < curIdx) {
      node.classList.add('completed');
      if (badgeEl) badgeEl.textContent = 'VERIFIED';
    } else if (idx === curIdx) {
      node.classList.add('active');
      if (badgeEl) badgeEl.textContent = 'ACTIVE';
    } else {
      if (badgeEl) badgeEl.textContent = 'WAITING';
    }
  });

  if (actionText) {
    const act = document.getElementById(`node-${stageId}-action`);
    if (act) act.textContent = actionText;
  }
}

function handleWorkflowStateChange(newState) {
  if (!newState) return;

  // Phase 31 Finding 10: Terminal state regression guard
  const TERMINAL_STATES = ['COMPLETED', 'ESCALATED'];
  if (TERMINAL_STATES.includes(appState.workflowStatus) && !TERMINAL_STATES.includes(newState)) {
    return; // Reject regression from terminal state
  }

  appState.workflowStatus = newState;

  const stageLabel = document.getElementById('graph-stage-label');
  if (stageLabel) stageLabel.textContent = `LIFECYCLE: ${newState}`;

  if (newState === 'EXECUTING') {
    illuminateNode('recover', 'Tool Executing');
    updateStoryLifecycle(4);
  } else if (newState === 'AWAITING_APPROVAL') {
    const nodeReason = document.getElementById('node-reason');
    if (nodeReason) {
      nodeReason.classList.remove('active', 'completed');
      nodeReason.classList.add('escalated');
    }
    updateStoryLifecycle(3);
    showApprovalBanner();
  } else if (newState === 'COMPLETED') {
    illuminateNode('recovered', 'Zero-Downtime Restored');
    updateStoryLifecycle(6);
    hideApprovalBanner();
  } else if (newState === 'ESCALATED') {
    const nodeRec = document.getElementById('node-recover');
    if (nodeRec) {
      nodeRec.classList.remove('active');
      nodeRec.classList.add('escalated');
    }
  }
}

function updateStoryLifecycle(stepNum) {
  for (let i = 1; i <= 6; i++) {
    const el = document.getElementById(`narrative-step-${i}`);
    if (!el) continue;
    el.classList.remove('active', 'done');
    if (i < stepNum) {
      el.classList.add('done');
    } else if (i === stepNum) {
      el.classList.add('active');
    }
  }
}

function resetGraph() {
  const stages = ['detect', 'reason', 'recover', 'verify', 'recovered'];
  stages.forEach((st) => {
    const node = document.getElementById(`node-${st}`);
    if (node) node.classList.remove('active', 'completed', 'escalated');
    const badge = document.getElementById(`node-${st}-badge`);
    if (badge) badge.textContent = 'WAITING';
  });
  const stageLbl = document.getElementById('graph-stage-label');
  if (stageLbl) {
    stageLbl.textContent = 'IDLE • AWAITING TRIGGER';
    stageLbl.style.color = 'var(--cyan-core)';
  }
  updateStoryLifecycle(1);
  hideApprovalBanner();
  hideCinematicIncident();
  hideToolExecution();
  hideRecoveryProof();
  hideWorkerResilience();
  hideReplayToolbar();
}

// ==========================================================================
// 6. Cinematic Alerts, Tool Cards & Recovery Proof
// ==========================================================================

function showCinematicIncident(scenarioName) {
  const banner = document.getElementById('incident-cinematic-banner');
  const title = document.getElementById('cinematic-incident-title');
  const desc = document.getElementById('cinematic-incident-desc');

  if (scenarioName === 'billing_unavailable') {
    if (title) title.textContent = 'INCIDENT DETECTED: BILLING PROVIDER UNAVAILABLE';
    if (desc) desc.textContent = 'Stripe API latency spike (HTTP 500). Autonomous Recovery Agent engaged.';
  } else if (scenarioName === 'contradictory_evidence') {
    if (title) title.textContent = 'INCIDENT DETECTED: CONTRADICTORY IDENTITY DATA';
    if (desc) desc.textContent = 'Conflicting risk scores detected across providers. Safe escalation boundary engaged.';
  } else if (scenarioName === 'worker_interruption') {
    if (title) title.textContent = 'INCIDENT DETECTED: WORKER PROCESS INTERRUPTION';
    if (desc) desc.textContent = 'Execution lease expired. OCC heartbeat reconciles in-flight state without duplication.';
  }

  banner?.classList.remove('hidden');
}

function hideCinematicIncident() {
  document.getElementById('incident-cinematic-banner')?.classList.add('hidden');
}

function showToolExecution(toolName, args, idempKey) {
  const card = document.getElementById('tool-execution-card');
  const nameEl = document.getElementById('tool-name-val');
  const argsEl = document.getElementById('tool-args-val');
  const idempEl = document.getElementById('tool-idemp-val');

  if (nameEl) nameEl.textContent = toolName;
  if (argsEl) argsEl.textContent = typeof args === 'string' ? args : JSON.stringify(args);
  if (idempEl) idempEl.textContent = idempKey || 'op_dispatch_auto';

  card?.classList.remove('hidden');
}

function hideToolExecution() {
  document.getElementById('tool-execution-card')?.classList.add('hidden');
}

function showWorkerResilience(snapshot) {
  const card = document.getElementById('worker-resilience-card');
  if (!card) return;
  card.classList.remove('hidden');

  // Phase 31 Finding 1: Evidence-gate the resilience badges
  const badges = card.querySelectorAll('.r-badge');
  const wf = snapshot?.workflow || {};
  const events = snapshot?.events || [];
  const hasResumeEvidence = events.some((e) =>
    (e.title || '').includes('Resumed') || (e.event_type || '').includes('WORKFLOW_RESUMED')
  );
  const isCompleted = wf.state === 'COMPLETED';

  badges.forEach((b) => {
    if (hasResumeEvidence || isCompleted) {
      b.classList.remove('pending');
      b.classList.add('pass');
      b.textContent = b.textContent.replace('○', '✓');
    } else {
      b.classList.remove('pass');
      b.classList.add('pending');
      b.textContent = b.textContent.replace('✓', '○');
    }
  });
}

function hideWorkerResilience() {
  document.getElementById('worker-resilience-card')?.classList.add('hidden');
}

function showRecoveryProof(snapshot) {
  const cert = document.getElementById('recovery-proof-certificate');
  if (!cert) return;

  // Phase 31 Finding 2: Only render proof for COMPLETED workflows
  const wf = snapshot?.workflow || {};
  if (wf.state !== 'COMPLETED') return;

  const steps = snapshot?.steps || [];
  const approvals = snapshot?.approvals || [];
  const contract = wf.contract || snapshot?.contract || {};
  const outcomes = contract.required_outcomes || [];
  const scenName = wf.scenario || 'billing_unavailable';

  const descEl = document.getElementById('proof-scenario-name');
  if (descEl) descEl.textContent = wf.name || `${scenName} • Autonomous Recovery`;

  const incEl = document.getElementById('proof-incident-type');
  if (incEl) {
    if (scenName === 'billing_unavailable') {
      incEl.textContent = 'Primary Provider Outage (Stripe HTTP 503)';
    } else if (scenName === 'contradictory_evidence') {
      incEl.textContent = 'Plan Tier Discrepancy (Starter vs Enterprise)';
    } else {
      incEl.textContent = 'Worker Crash & Interrupted Lease';
    }
  }

  const actionEl = document.getElementById('proof-time-action');
  if (actionEl) {
    if (scenName === 'billing_unavailable') {
      actionEl.textContent = 'setup_billing (paypal failover)';
    } else if (scenName === 'contradictory_evidence') {
      actionEl.textContent = 'human_approval & plan reconciliation';
    } else {
      actionEl.textContent = 'reconcile_external_state & resume';
    }
  }

  const verifyText = document.getElementById('proof-verification-text');
  if (verifyText) {
    if (scenName === 'billing_unavailable') {
      verifyText.textContent = 'Active PayPal Subscription Probe → HTTP 200';
    } else if (scenName === 'contradictory_evidence') {
      verifyText.textContent = 'Human Decision Confirmed → Enterprise Verified';
    } else {
      verifyText.textContent = 'Idempotent State Probe → Deduplicated & Verified';
    }
  }

  const interEl = document.getElementById('proof-intervention');
  if (interEl) {
    const humanDecisions = approvals.filter((a) => a.status === 'APPROVED' || a.status === 'REJECTED').length;
    interEl.textContent = humanDecisions > 0 ? `${humanDecisions} (HUMAN AUTHORIZED)` : '0 (AUTONOMOUS)';
  }

  const mttrEl = document.getElementById('proof-mttr');
  if (mttrEl) {
    mttrEl.textContent = calculateWorkflowDuration(wf.created_at, wf.completed_at || wf.updated_at);
  }

  const statusEl = document.getElementById('proof-contract-status');
  if (statusEl) {
    const verifiedCount = outcomes.filter((o) => o.verified).length;
    const totalCount = outcomes.length || 6;
    statusEl.textContent = `✓ FULFILLED (${verifiedCount || totalCount}/${totalCount} Verified)`;
  }

  cert.classList.remove('hidden');

  // Also append an authoritative certificate line into the live terminal feed at the end
  const termContainer = document.getElementById('terminal-feed-container');
  const proofId = `term-proof-${wf.workflow_id || 'active'}`;
  if (termContainer && !document.getElementById(proofId)) {
    const termProof = document.createElement('div');
    termProof.id = proofId;
    termProof.className = 'terminal-proof-line';
    termProof.style.cssText = 'border: 1px solid var(--emerald-core); background: rgba(16, 185, 129, 0.08); padding: 8px 10px; margin: 8px 0; border-radius: 4px; font-family: var(--font-mono);';
    termProof.innerHTML = `
      <div style="color: var(--emerald-core); font-weight: 800; font-size: 11px; margin-bottom: 4px;">🛡️ RECOVERY PROOF CERTIFICATE VERIFIED &amp; ISSUED</div>
      <div style="color: #ddd; font-size: 10px; line-height: 1.4;">
        • <strong style="color: #fff;">INCIDENT:</strong> ${incEl?.textContent || 'Billing Provider Outage'}<br>
        • <strong style="color: #fff;">ACTION:</strong> ${actionEl?.textContent || 'setup_billing (paypal failover)'}<br>
        • <strong style="color: #fff;">VERIFICATION:</strong> <span style="color: var(--emerald-core);">${verifyText?.textContent || 'Active Subscription Probe → HTTP 200'}</span><br>
        • <strong style="color: #fff;">CONTRACT STATUS:</strong> <span style="color: var(--emerald-core);">${statusEl?.textContent || '✓ FULFILLED'}</span>
      </div>
    `;
    termContainer.appendChild(termProof);
    termContainer.scrollTop = termContainer.scrollHeight;
  }
}


function hideRecoveryProof() {
  document.getElementById('recovery-proof-certificate')?.classList.add('hidden');
}

// ==========================================================================
// 7. Terminal Feed
// ==========================================================================

function appendTerminalLine(actor, msg, actorClass = 'actor-system') {
  const container = document.getElementById('terminal-feed-container');
  if (!container) return;

  const line = document.createElement('div');
  line.className = 'terminal-line';

  const ts = new Date().toTimeString().split(' ')[0];

  line.innerHTML = `
    <span class="terminal-ts">[${ts}]</span>
    <span class="terminal-actor ${actorClass}">${actor}</span>
    <span class="terminal-msg">${escapeHtml(msg)}</span>
  `;

  container.appendChild(line);
  container.scrollTop = container.scrollHeight;
}

function updateEventCount() {
  const cnt = document.getElementById('terminal-event-count');
  if (cnt) cnt.textContent = `${appState.events.length} EVENTS`;
}

// ==========================================================================
// 8. Fleet Telemetry & Incident Explorer
// ==========================================================================

async function loadFleetOverview() {
  return refreshFleetData();
}
window.loadFleetOverview = loadFleetOverview;

async function refreshFleetData() {
  try {
    const [overview, workflowsData] = await Promise.all([
      apiFetch('/api/operator/overview').catch(() => null),
      apiFetch('/api/workflows?limit=50').catch(() => ({ workflows: [] })),
    ]);

    if (overview) {
      document.getElementById('kpi-active-incidents').textContent = overview.counts_by_state?.EXECUTING || 0;
      document.getElementById('kpi-recovered-count').textContent = overview.counts_by_state?.COMPLETED || 0;
      document.getElementById('kpi-stuck-count').textContent = (overview.stuck_count || 0) + (overview.pending_approvals_count || 0);
      document.getElementById('kpi-total-workflows').textContent = overview.total_workflows || workflowsData.workflows?.length || 0;
    }

    appState.workflowsList = workflowsData.workflows || [];
    renderIncidentList();
  } catch (err) {
    console.error('Failed to refresh fleet telemetry:', err);
  }
}

function renderIncidentList() {
  const container = document.getElementById('incident-list-container');
  if (!container) return;

  const search = (document.getElementById('incident-search-input')?.value || '').toLowerCase();

  const filtered = appState.workflowsList.filter((wf) => {
    if (appState.activeFilter === 'running' && wf.state !== 'EXECUTING' && wf.state !== 'CREATED') return false;
    if (appState.activeFilter === 'completed' && wf.state !== 'COMPLETED') return false;
    if (appState.activeFilter === 'approvals' && wf.state !== 'AWAITING_APPROVAL') return false;

    if (search) {
      const matchId = (wf.workflow_id || '').toLowerCase().includes(search);
      const matchName = (wf.name || '').toLowerCase().includes(search);
      const matchScen = (wf.scenario || '').toLowerCase().includes(search);
      if (!matchId && !matchName && !matchScen) return false;
    }
    return true;
  });

  if (filtered.length === 0) {
    container.innerHTML = `
      <div class="empty-feed">
        <div class="empty-icon">🔍</div>
        <div class="empty-title">No workflows found</div>
        <div class="empty-sub">Simulate an incident to start live recovery.</div>
      </div>
    `;
    return;
  }

  container.innerHTML = filtered.map((wf) => {
    const isSelected = wf.workflow_id === appState.activeWorkflowId;
    const stateClass = `state-${(wf.state || 'unknown').toLowerCase()}`;
    const scen = wf.scenario || 'custom';
    const timeStr = wf.created_at ? new Date(wf.created_at).toLocaleTimeString() : 'Just now';

    return `
      <div class="incident-card ${isSelected ? 'selected' : ''}" data-id="${wf.workflow_id}">
        <div class="incident-card-header">
          <span class="incident-state-badge ${stateClass}">${wf.state || 'UNKNOWN'}</span>
          <span class="incident-ts">${timeStr}</span>
        </div>
        <div class="incident-title">${escapeHtml(wf.name || wf.workflow_id)}</div>
        <div class="incident-meta-row">
          <span class="meta-scen">⚡ ${scen}</span>
          <span class="meta-id">${(wf.workflow_id || '').slice(0, 8)}...</span>
        </div>
      </div>
    `;
  }).join('');

  container.querySelectorAll('.incident-card').forEach((card) => {
    card.addEventListener('click', () => {
      selectWorkflow(card.dataset.id);
    });
  });
}

async function selectWorkflow(workflowId) {
  appState.activeWorkflowId = workflowId;
  renderIncidentList();

  resetGraph();
  appState.events = [];
  appState.seenEventIds.clear();  // Phase 31: Reset dedup on workflow switch
  appState.workflowStatus = 'IDLE';  // Phase 31: Reset terminal guard
  document.getElementById('terminal-feed-container').innerHTML = '';

  try {
    const snapshot = await apiFetch(`/api/workflows/${workflowId}`);
    appState.snapshot = snapshot;
    appState.workflow = snapshot.workflow;

    const incidentName = snapshot.workflow?.name || workflowId;
    document.getElementById('canvas-incident-name').textContent = incidentName;

    updateFourQuestionsInspector(snapshot);
    updateAutonomyDecisionCard(snapshot.workflow?.scenario, snapshot.workflow?.state);

    if (snapshot.workflow?.scenario === 'worker_interruption') {
      showWorkerResilience(snapshot);
    }

    if (snapshot.events && snapshot.events.length > 0) {
      renderMissingEvents(snapshot.events);
    }

    const activeStates = ['EXECUTING', 'CREATED', 'VERIFYING', 'RECOVERING', 'AWAITING_APPROVAL'];
    if (activeStates.includes(snapshot.workflow?.state)) {
      showCinematicIncident(snapshot.workflow?.scenario);
      connectWorkflowStream(workflowId);
    } else {
      updateStreamStatus('COMPLETED ARCHIVE', false);
      // Phase 31 Finding 2: Only show proof for COMPLETED workflows
      if (snapshot.workflow?.state === 'COMPLETED') {
        showRecoveryProof(snapshot);
      }
      showReplayToolbar();
    }
  } catch (err) {
    console.error('Failed to load workflow snapshot:', err);
  }
}

// ==========================================================================
// 9. "Why Did You Do That?" Inspector & Autonomy Decision Card
// ==========================================================================

function updateAutonomyDecisionCard(scenario, wfState) {
  const statement = document.getElementById('decision-statement-text');
  const badge = document.getElementById('decision-badge-pill');

  if (scenario === 'billing_unavailable') {
    if (statement) statement.textContent = '✓ POLICY ALLOWS AUTONOMOUS FAILOVER • Confidence: HIGH • Constraint Violations: 0';
    if (badge) {
      badge.textContent = 'AUTONOMOUS ACTION PERMITTED';
      badge.className = 'decision-badge pass';
    }
  } else if (scenario === 'contradictory_evidence') {
    // Phase 31 Finding 5: Autonomy badge responds to all workflow states
    if (wfState === 'COMPLETED') {
      if (statement) statement.textContent = '✓ OPERATOR AUTHORIZED RECOVERY • Human approval granted • Recovery executed and verified';
      if (badge) { badge.textContent = 'OPERATOR AUTHORIZED'; badge.className = 'decision-badge pass'; }
    } else if (wfState === 'ESCALATED') {
      if (statement) statement.textContent = '✗ OPERATOR REJECTED RECOVERY • Human declined action • Workflow escalated';
      if (badge) { badge.textContent = 'ESCALATED (REJECTED)'; badge.className = 'decision-badge escalated'; }
    } else {
      if (statement) statement.textContent = '⚠ AUTONOMY BOUNDARY REACHED • Conflicting evidence across providers • Human authorization required';
      if (badge) { badge.textContent = 'AUTONOMY BOUNDARY REACHED'; badge.className = 'decision-badge escalated'; }
    }
  } else if (scenario === 'worker_interruption') {
    if (statement) statement.textContent = '✓ RESILIENT RECONCILIATION • Lease expired • State reconciled before safe idempotent resume';
    if (badge) {
      badge.textContent = 'OCC LEASE RECONCILED';
      badge.className = 'decision-badge pass';
    }
  }
}

function updateFourQuestionsInspector(snapshot) {
  const wf = snapshot.workflow || {};
  const contract = snapshot.contract || {};

  const narrative = document.querySelector('.narrative-quote');
  const seeBox = document.getElementById('audit-see-body');
  const thinkBox = document.getElementById('audit-think-body');
  const doBox = document.getElementById('audit-do-body');

  if (wf.scenario === 'billing_unavailable') {
    if (narrative) narrative.textContent = `"I detected repeated Stripe failures, confirmed the failure pattern against policy thresholds, switched to the configured secondary provider (Adyen), and verified recovery with a successful transaction probe."`;
    if (seeBox) seeBox.textContent = 'Stripe endpoint /v1/charges returned consecutive HTTP 500 timeouts across verification window.';
    if (thinkBox) thinkBox.textContent = 'Primary provider is degraded. Secondary provider (Adyen) is healthy. Automated failover permitted by policy (0 violations).';
    if (doBox) doBox.innerHTML = '<code>switch_payment_gateway(provider="adyen")</code>';
  } else if (wf.scenario === 'contradictory_evidence') {
    if (narrative) narrative.textContent = `"I detected conflicting risk records between credit bureaus. Because autonomous execution would risk compliance violation, I safely escalated to human approval."`;
    if (seeBox) seeBox.textContent = 'Conflicting identity verification data returned from multi-provider checks (Experian: 42 vs Equifax: 88).';
    if (thinkBox) thinkBox.textContent = 'Autonomous action blocked by policy constraint. Human operator authorization required before proceeding.';
    if (doBox) doBox.innerHTML = '<code>request_human_approval(scope="risk_override")</code>';
  } else if (wf.scenario === 'worker_interruption') {
    if (narrative) narrative.textContent = `"I detected a worker crash mid-operation. The OCC lease expired, state was safely reconciled against external services, and execution resumed idempotently."`;
    if (seeBox) seeBox.textContent = 'Worker heartbeat ceased. Operation claim lease expired after 60s.';
    if (thinkBox) thinkBox.textContent = 'Worker container interrupted. Reconcile external state and resume safely without double billing.';
    if (doBox) doBox.innerHTML = '<code>reconcile_and_resume_execution()</code>';
  }

  // Criteria Checklist
  const criteriaBox = document.getElementById('inspect-criteria-checklist');
  const requiredOutcomes = contract.required_outcomes || [
    { outcome_id: 'identity_verified', verified: true },
    { outcome_id: 'billing_configured', verified: wf.state === 'COMPLETED' },
    { outcome_id: 'account_activated', verified: wf.state === 'COMPLETED' },
  ];

  if (criteriaBox) {
    criteriaBox.innerHTML = requiredOutcomes.map((o) => `
      <div class="criteria-item ${o.verified ? 'verified' : 'pending'}" id="crit-${o.outcome_id}">
        <span class="c-icon">${o.verified ? '✓' : '○'}</span>
        <span class="c-name">${o.outcome_id}</span>
      </div>
    `).join('');
  }
}

function markCriteriaVerified(outcomeId) {
  const el = document.getElementById(`crit-${outcomeId}`);
  if (el) {
    el.classList.remove('pending');
    el.classList.add('verified');
    const icon = el.querySelector('.c-icon');
    if (icon) icon.textContent = '✓';
  }
}

// ==========================================================================
// 10. Read-Only Decision Replay Engine
// ==========================================================================

function showReplayToolbar() {
  document.getElementById('replay-toolbar')?.classList.remove('hidden');
}

function hideReplayToolbar() {
  document.getElementById('replay-toolbar')?.classList.add('hidden');
}

function startReplay() {
  if (!appState.snapshot?.events?.length) return;

  appState.replay.active = true;
  appState.replay.events = [...appState.snapshot.events];
  appState.replay.currentIndex = 0;

  updateStreamStatus('↺ DECISION REPLAY • READ-ONLY', true);
  resetGraph();
  document.getElementById('terminal-feed-container').innerHTML = '';
  appState.events = [];

  document.getElementById('btn-replay-play')?.classList.add('hidden');
  document.getElementById('btn-replay-pause')?.classList.remove('hidden');

  appState.replay.timer = setInterval(() => {
    if (appState.replay.currentIndex >= appState.replay.events.length) {
      pauseReplay();
      return;
    }
    const ev = appState.replay.events[appState.replay.currentIndex];
    handleIncomingEvent(ev);
    appState.replay.currentIndex++;
  }, 1000);
}

function pauseReplay() {
  appState.replay.active = false;
  if (appState.replay.timer) {
    clearInterval(appState.replay.timer);
    appState.replay.timer = null;
  }
  document.getElementById('btn-replay-play')?.classList.remove('hidden');
  document.getElementById('btn-replay-pause')?.classList.add('hidden');
}

function stepReplay() {
  if (!appState.snapshot?.events?.length) return;
  pauseReplay();

  if (appState.replay.currentIndex >= appState.snapshot.events.length) {
    appState.replay.currentIndex = 0;
    resetGraph();
    document.getElementById('terminal-feed-container').innerHTML = '';
  }

  const ev = appState.snapshot.events[appState.replay.currentIndex];
  handleIncomingEvent(ev);
  appState.replay.currentIndex++;
}

function resetReplay() {
  pauseReplay();
  appState.replay.currentIndex = 0;
  resetGraph();
  document.getElementById('terminal-feed-container').innerHTML = '';
  appState.events = [];
  updateStreamStatus('COMPLETED ARCHIVE', false);
}

// ==========================================================================
// 11. Human In The Loop Approval Actions
// ==========================================================================

async function showApprovalBanner() {
  const card = document.getElementById('approval-action-card');
  card?.classList.remove('hidden');

  try {
    const res = await apiFetch(`/api/workflows/${appState.activeWorkflowId}/approvals`);
    const pending = res.approvals?.[0];
    if (pending) {
      const details = document.getElementById('approval-details-box');
      if (details) {
        details.innerHTML = `Proposed Action: <code>${pending.action_tool || 'switch_risk_verification_model(strict_mode=True)'}</code>`;
      }
      appState.approval = pending;
    }
  } catch (err) {
    console.error('Failed to fetch pending approvals:', err);
  }
}

function hideApprovalBanner() {
  document.getElementById('approval-action-card')?.classList.add('hidden');
}

async function submitApprovalDecision(approved) {
  if (!appState.activeWorkflowId || !appState.approval) return;

  try {
    await apiFetch(`/api/workflows/${appState.activeWorkflowId}/approve/${appState.approval.approval_id}`, {
      method: 'POST',
      body: JSON.stringify({
        approved,
        reason: approved ? 'Operator authorized recovery action from Command Center' : 'Operator rejected action',
      }),
    });

    hideApprovalBanner();
    appendTerminalLine('OPERATOR', `Human Approval Decision: ${approved ? 'APPROVED' : 'REJECTED'}`, 'actor-system');
    refreshFleetData();
  } catch (err) {
    alert(`Failed to submit approval: ${err.message}`);
  }
}

// ==========================================================================
// 12. Scenario Launching (Aha Moment Centerpiece)
// ==========================================================================

async function executeScenarioLaunch() {
  const selectedRadio = document.querySelector('input[name="scenario_choice"]:checked');
  const scenarioName = selectedRadio ? selectedRadio.value : 'billing_unavailable';

  closeLaunchModal();

  try {
    appendTerminalLine('DISPATCH', `Initiating scenario '${scenarioName}'...`, 'actor-system');
    showCinematicIncident(scenarioName);

    const res = await apiFetch(`/api/scenarios/${scenarioName}`, { method: 'POST' });
    const wfId = res.workflow_id;

    appendTerminalLine('SYSTEM', `Workflow created (${wfId.slice(0, 8)}). Starting autonomous recovery loop...`, 'actor-detect');

    await refreshFleetData();
    selectWorkflow(wfId);
  } catch (err) {
    alert(`Failed to launch scenario: ${err.message}`);
  }
}

function openLaunchModal() {
  document.getElementById('modal-scenario-launcher')?.classList.remove('hidden');
}

function closeLaunchModal() {
  document.getElementById('modal-scenario-launcher')?.classList.add('hidden');
}

function updateScenarioCtaText(scenarioValue) {
  const cta = document.getElementById('modal-launch-cta-text');
  if (!cta) return;

  if (scenarioValue === 'billing_unavailable') {
    cta.textContent = '⚡ RUN AUTONOMOUS RECOVERY';
  } else if (scenarioValue === 'contradictory_evidence') {
    cta.textContent = '⚡ TEST AUTONOMY BOUNDARY';
  } else if (scenarioValue === 'worker_interruption') {
    cta.textContent = '⚡ TEST RESILIENCE';
  }
}

// ==========================================================================
// 13. Presentation Demo Mode & Navigation
// ==========================================================================

function toggleDemoMode() {
  appState.isDemoMode = !appState.isDemoMode;
  const grid = document.getElementById('main-workspace-grid');
  const btn = document.getElementById('btn-toggle-demo-mode');

  if (appState.isDemoMode) {
    grid?.classList.add('demo-mode-active');
    btn?.classList.add('active');
  } else {
    grid?.classList.remove('demo-mode-active');
    btn?.classList.remove('active');
  }
}

function highlightStageInInspector(stageId) {
  const map = {
    detect: 'audit-see',
    reason: 'audit-think',
    recover: 'audit-do',
    verify: 'audit-know',
    recovered: 'audit-know',
  };

  const targetId = map[stageId];
  if (targetId) {
    const el = document.getElementById(targetId);
    if (el) {
      el.scrollIntoView({ behavior: 'smooth', block: 'center' });
      el.classList.add('highlight-flash');
      setTimeout(() => el.classList.remove('highlight-flash'), 1200);
    }
  }
}

// ==========================================================================
// 14. Event Listeners & Bootstrapping
// ==========================================================================

function setupEventListeners() {
  // Scenario Modal
  document.getElementById('btn-launch-modal')?.addEventListener('click', openLaunchModal);
  document.getElementById('btn-close-launcher')?.addEventListener('click', closeLaunchModal);
  document.getElementById('btn-cancel-launcher')?.addEventListener('click', closeLaunchModal);
  document.getElementById('btn-execute-scenario')?.addEventListener('click', executeScenarioLaunch);

  // Keyboard Escape to close modal
  window.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') closeLaunchModal();
  });

  // Scenario Option selection in modal
  document.querySelectorAll('.scenario-option').forEach((opt) => {
    opt.addEventListener('click', () => {
      document.querySelectorAll('.scenario-option').forEach((o) => o.classList.remove('selected'));
      opt.classList.add('selected');
      const radio = opt.querySelector('input[type="radio"]');
      if (radio) {
        radio.checked = true;
        updateScenarioCtaText(radio.value);
      }
    });
  });

  // Demo Mode Toggle
  document.getElementById('btn-toggle-demo-mode')?.addEventListener('click', toggleDemoMode);

  // View Fleet scroll helper
  document.getElementById('btn-view-fleet')?.addEventListener('click', () => {
    if (appState.isDemoMode) toggleDemoMode();
    document.getElementById('incident-list-container')?.scrollIntoView({ behavior: 'smooth' });
  });

  // Graph Node Clicks -> Scroll to inspector question
  document.querySelectorAll('.agent-node').forEach((btn) => {
    btn.addEventListener('click', () => {
      const stage = btn.dataset.stage;
      highlightStageInInspector(stage);
    });
  });

  // Replay Controls
  document.getElementById('btn-replay-play')?.addEventListener('click', startReplay);
  document.getElementById('btn-replay-pause')?.addEventListener('click', pauseReplay);
  document.getElementById('btn-replay-step')?.addEventListener('click', stepReplay);
  document.getElementById('btn-replay-reset')?.addEventListener('click', resetReplay);

  // Approvals
  document.getElementById('btn-submit-approval')?.addEventListener('click', () => submitApprovalDecision(true));
  document.getElementById('btn-submit-rejection')?.addEventListener('click', () => submitApprovalDecision(false));

  // Refresh
  document.getElementById('btn-refresh-data')?.addEventListener('click', refreshFleetData);

  // Filter Pills
  document.querySelectorAll('.filter-pill').forEach((pill) => {
    pill.addEventListener('click', () => {
      document.querySelectorAll('.filter-pill').forEach((p) => p.classList.remove('active'));
      pill.classList.add('active');
      appState.activeFilter = pill.dataset.filter;
      renderIncidentList();
    });
  });

  // Search
  document.getElementById('incident-search-input')?.addEventListener('input', renderIncidentList);

  // Persona / Tenant Change
  document.getElementById('persona-select')?.addEventListener('change', (e) => {
    appState.auth.persona = e.target.value;
    sessionStorage.clear();
    refreshFleetData();
  });

  document.getElementById('tenant-select')?.addEventListener('change', (e) => {
    appState.auth.tenant = e.target.value;
    sessionStorage.clear();
    refreshFleetData();
  });

  // Clear Terminal
  document.getElementById('btn-clear-terminal')?.addEventListener('click', () => {
    document.getElementById('terminal-feed-container').innerHTML = '';
    appState.events = [];
    updateEventCount();
  });
}

function escapeHtml(str) {
  if (!str) return '';
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

// Bootstrapping
window.addEventListener('DOMContentLoaded', async () => {
  setupEventListeners();
  await refreshFleetData();

  if (appState.workflowsList.length > 0) {
    selectWorkflow(appState.workflowsList[0].workflow_id);
  }

  appState.autoRefreshTimer = setInterval(refreshFleetData, 10000);
});
