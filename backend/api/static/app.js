/**
 * RecoveryOS Operator Control Plane — Frontend Application Logic
 * Zero-dependency, modern Vanilla JS ES Modules architecture.
 */

// Application State
const state = {
  activeTab: 'fleet',
  currentPersona: 'operator',
  currentTenant: 'tenant-default',
  workflows: [],
  totalWorkflows: 0,
  stuckWorkflows: [],
  auditLogs: [],
  pageLimit: 25,
  pageOffset: 0,
  activeWorkflowId: null,
  activeWorkflowSnapshot: null,
  eventSource: null,
  autoRefreshTimer: null,
  autoRefreshInterval: 15000,
};

// Persona Token Mapping
const PERSONAS = {
  operator: { user_id: 'operator-1', role: 'operator', name: 'Operator (operator-1)' },
  admin: { user_id: 'admin-1', role: 'admin', name: 'Administrator (admin-1)' },
  approver: { user_id: 'approver-1', role: 'approver', name: 'Approver (approver-1)' },
  viewer: { user_id: 'viewer-1', role: 'viewer', name: 'Auditor (viewer-1)' },
};

// ==========================================================================
// API Client & Token Management
// ==========================================================================

async function ensureAuthToken() {
  const p = PERSONAS[state.currentPersona] || PERSONAS.operator;
  const targetTenant = state.currentTenant === 'all' ? 'tenant-default' : state.currentTenant;
  const cacheKey = `token_${p.role}_${targetTenant}`;
  
  let token = sessionStorage.getItem(cacheKey);
  if (!token) {
    try {
      const res = await fetch('/api/auth/login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          username: p.user_id,
          role: p.role,
          tenant_id: targetTenant,
        }),
      });
      if (res.ok) {
        const data = await res.json();
        token = data.access_token;
        sessionStorage.setItem(cacheKey, token);
      }
    } catch (err) {
      console.warn('Failed to obtain signed token from backend, falling back to mock:', err);
    }
  }

  if (!token) {
    const header = btoa(JSON.stringify({ alg: "HS256", typ: "JWT" }));
    const payload = btoa(JSON.stringify({
      sub: p.user_id,
      role: p.role,
      tenant_id: targetTenant,
      exp: Math.floor(Date.now() / 1000) + 3600,
    }));
    token = `${header}.${payload}.dev_local_token`;
  }

  state.jwtToken = token;
  return token;
}

async function apiFetch(url, options = {}) {
  const token = await ensureAuthToken();
  const headers = {
    'Content-Type': 'application/json',
    'Authorization': `Bearer ${token}`,
    'X-Tenant-ID': state.currentTenant,
    ...(options.headers || {}),
  };

  try {
    const res = await fetch(url, { ...options, headers });
    if (!res.ok) {
      const errorData = await res.json().catch(() => ({ detail: res.statusText }));
      throw new Error(errorData.detail || `HTTP ${res.status}: ${res.statusText}`);
    }
    return await res.json();
  } catch (err) {
    console.error(`[API Error] ${url}:`, err);
    throw err;
  }
}
    console.error(`[API Error] ${url}:`, err);
    throw err;
  }
}

// ==========================================================================
// Toast Notifications
// ==========================================================================

function showToast(message, type = 'info') {
  const container = document.getElementById('toast-container');
  const toast = document.createElement('div');
  toast.className = `toast ${type}`;
  toast.innerHTML = `
    <span>${message}</span>
  `;
  container.appendChild(toast);
  setTimeout(() => {
    toast.remove();
  }, 4000);
}

// ==========================================================================
// Tab Switching
// ==========================================================================

function initTabs() {
  document.querySelectorAll('.tab-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      const targetTab = btn.dataset.tab;
      switchTab(targetTab);
    });
  });
}

function switchTab(tabName) {
  state.activeTab = tabName;
  document.querySelectorAll('.tab-btn').forEach(b => {
    b.classList.toggle('active', b.dataset.tab === tabName);
  });
  document.querySelectorAll('.tab-pane').forEach(p => {
    p.classList.toggle('active', p.id === `tab-${tabName}`);
  });

  if (tabName === 'fleet') loadFleetOverview();
  else if (tabName === 'workflows') loadWorkflows();
  else if (tabName === 'stuck') loadStuckWorkflows();
  else if (tabName === 'audit') loadAuditLogs();
}

// ==========================================================================
// TAB 1: Fleet Overview
// ==========================================================================

async function loadFleetOverview() {
  try {
    const data = await apiFetch('/api/operator/overview');
    
    // KPI Updates
    document.getElementById('kpi-total-workflows').textContent = data.total_workflows || 0;
    document.getElementById('kpi-stuck-workflows').textContent = data.stuck_count || 0;
    document.getElementById('kpi-pending-approvals').textContent = data.pending_approvals_count || 0;
    
    const counts = data.counts_by_state || {};
    document.getElementById('kpi-escalated-workflows').textContent = counts.ESCALATED || 0;
    document.getElementById('kpi-completed-workflows').textContent = counts.COMPLETED || 0;

    // Badges
    document.getElementById('badge-workflow-count').textContent = data.total_workflows || 0;
    document.getElementById('badge-stuck-count').textContent = data.stuck_count || 0;

    // Distribution Bar
    const total = data.total_workflows || 1;
    const states = ['CREATED', 'EXECUTING', 'AWAITING_APPROVAL', 'RECOVERING', 'ESCALATED', 'COMPLETED'];
    const bar = document.getElementById('state-distribution-bar');
    const legend = document.getElementById('state-legend');
    
    bar.innerHTML = '';
    legend.innerHTML = '';

    states.forEach(st => {
      const count = counts[st] || 0;
      const pct = (count / total) * 100;
      if (pct > 0) {
        const seg = document.createElement('div');
        seg.className = `bar-segment state-${st.toLowerCase()}`;
        seg.style.width = `${pct}%`;
        seg.title = `${st}: ${count} (${Math.round(pct)}%)`;
        bar.appendChild(seg);
      }

      const item = document.createElement('div');
      item.className = 'legend-item';
      item.innerHTML = `
        <span class="legend-dot state-${st.toLowerCase()}"></span>
        <span>${st} (${count})</span>
      `;
      legend.appendChild(item);
    });

  } catch (err) {
    showToast(`Failed to load fleet overview: ${err.message}`, 'error');
  }
}

// ==========================================================================
// TAB 2: Workflow Explorer
// ==========================================================================

async function loadWorkflows() {
  const tbody = document.getElementById('workflows-table-body');
  tbody.innerHTML = '<tr><td colspan="8" class="empty-state">Loading workflows...</td></tr>';

  const stateFilter = document.getElementById('filter-state-select').value;
  const scenarioFilter = document.getElementById('filter-scenario-select').value;
  const stuckOnly = document.getElementById('filter-stuck-only').checked;
  const search = document.getElementById('workflow-search-input').value.trim();

  const params = new URLSearchParams({
    limit: state.pageLimit,
    offset: state.pageOffset,
  });
  if (stateFilter) params.append('state', stateFilter);
  if (scenarioFilter) params.append('scenario', scenarioFilter);
  if (stuckOnly) params.append('is_stuck', 'true');
  if (search) params.append('search', search);

  try {
    const data = await apiFetch(`/api/workflows?${params.toString()}`);
    state.workflows = data.workflows || [];
    state.totalWorkflows = data.total || state.workflows.length;

    renderWorkflowsTable();
    updatePagination();
  } catch (err) {
    tbody.innerHTML = `<tr><td colspan="8" class="empty-state error">Failed to load workflows: ${err.message}</td></tr>`;
  }
}

function renderWorkflowsTable() {
  const tbody = document.getElementById('workflows-table-body');
  if (state.workflows.length === 0) {
    tbody.innerHTML = '<tr><td colspan="8" class="empty-state">No workflows found matching criteria.</td></tr>';
    return;
  }

  tbody.innerHTML = state.workflows.map(wf => {
    const stateVal = (wf.state || 'UNKNOWN').toLowerCase();
    const age = formatAge(wf.created_at || wf.dispatched_at);
    const isStuck = wf.is_stuck;

    return `
      <tr class="${isStuck ? 'row-stuck' : ''}">
        <td class="cell-mono">
          <a href="#" class="wf-link" data-id="${wf.workflow_id}">${wf.workflow_id.slice(0, 8)}...</a>
        </td>
        <td><strong>${wf.scenario || wf.name || 'Default Workflow'}</strong></td>
        <td>
          <span class="status-badge ${stateVal}">${wf.state}</span>
        </td>
        <td class="cell-mono">v${wf.version || 1}</td>
        <td>${age}</td>
        <td class="cell-mono">${wf.tenant_id || 'default'}</td>
        <td>
          ${isStuck ? '<span class="tag-stuck">⚠ STUCK</span>' : '<span class="status-text">HEALTHY</span>'}
        </td>
        <td>
          <div class="drawer-action-bar">
            <button class="btn btn-secondary btn-sm btn-inspect" data-id="${wf.workflow_id}">Inspect</button>
            ${!['COMPLETED'].includes(wf.state) ? `<button class="btn btn-primary btn-sm btn-quick-rec" data-id="${wf.workflow_id}" data-version="${wf.version || 1}">Recover</button>` : ''}
          </div>
        </td>
      </tr>
    `;
  }).join('');

  // Attach event listeners
  tbody.querySelectorAll('.wf-link, .btn-inspect').forEach(el => {
    el.addEventListener('click', (e) => {
      e.preventDefault();
      openWorkflowDrawer(el.dataset.id);
    });
  });

  tbody.querySelectorAll('.btn-quick-rec').forEach(el => {
    el.addEventListener('click', () => {
      openRecoverModal(el.dataset.id, el.dataset.version);
    });
  });
}

function updatePagination() {
  const info = document.getElementById('pagination-info');
  const btnPrev = document.getElementById('btn-prev-page');
  const btnNext = document.getElementById('btn-next-page');

  const start = state.pageOffset + 1;
  const end = Math.min(state.pageOffset + state.pageLimit, state.totalWorkflows);

  info.textContent = `Showing ${state.totalWorkflows > 0 ? start : 0} – ${end} of ${state.totalWorkflows}`;
  btnPrev.disabled = state.pageOffset === 0;
  btnNext.disabled = end >= state.totalWorkflows;
}

// ==========================================================================
// TAB 3: Stuck & Recovery Hub
// ==========================================================================

async function loadStuckWorkflows() {
  const container = document.getElementById('stuck-workflows-container');
  container.innerHTML = '<div class="empty-state">Scanning for stuck workflows...</div>';

  try {
    const data = await apiFetch('/api/operator/stuck-workflows');
    state.stuckWorkflows = data.stuck_workflows || [];

    if (state.stuckWorkflows.length === 0) {
      container.innerHTML = `
        <div class="panel" style="grid-column: 1 / -1; text-align: center; padding: 40px;">
          <h3 style="color: var(--color-emerald); margin-bottom: 8px;">✓ All Workflows Healthy</h3>
          <p class="subtext">Zero stalled or lease-expired workflows detected in active tenant scope.</p>
        </div>
      `;
      return;
    }

    container.innerHTML = state.stuckWorkflows.map(item => `
      <div class="stuck-card">
        <div class="stuck-card-header">
          <div>
            <div class="stuck-card-id">${item.workflow_id}</div>
            <span class="status-badge ${item.state.toLowerCase()}">${item.state}</span>
          </div>
          <span class="badge-code">OCC v${item.version}</span>
        </div>

        <div class="stuck-reason-box">
          <strong>Stuck Reason:</strong> ${item.stuck_reason || 'Inactive beyond expected duration threshold.'}
        </div>

        <div style="font-size: 0.78rem; color: var(--text-muted); font-family: var(--font-mono);">
          Age: ${item.age_seconds}s • Events: ${item.event_count}
        </div>

        <div class="drawer-action-bar" style="margin-top: auto;">
          <button class="btn btn-secondary btn-sm btn-inspect" data-id="${item.workflow_id}">Inspect</button>
          <button class="btn btn-primary btn-sm btn-quick-rec" data-id="${item.workflow_id}" data-version="${item.version}">Recover Now</button>
        </div>
      </div>
    `).join('');

    container.querySelectorAll('.btn-inspect').forEach(el => {
      el.addEventListener('click', () => openWorkflowDrawer(el.dataset.id));
    });
    container.querySelectorAll('.btn-quick-rec').forEach(el => {
      el.addEventListener('click', () => openRecoverModal(el.dataset.id, el.dataset.version));
    });

  } catch (err) {
    container.innerHTML = `<div class="empty-state error">Error loading stuck workflows: ${err.message}</div>`;
  }
}

// ==========================================================================
// TAB 4: Security & Audit Logs
// ==========================================================================

async function loadAuditLogs() {
  const tbody = document.getElementById('audit-table-body');
  tbody.innerHTML = '<tr><td colspan="8" class="empty-state">Loading audit trail...</td></tr>';

  const eventType = document.getElementById('filter-audit-event-type').value;
  const search = document.getElementById('audit-search-input').value.trim();

  const params = new URLSearchParams({ limit: 50, offset: 0 });
  if (eventType) params.append('event_type', eventType);

  try {
    const data = await apiFetch(`/api/audit/logs?${params.toString()}`);
    let logs = data.audit_logs || [];

    if (search) {
      const q = search.toLowerCase();
      logs = logs.filter(l => 
        (l.actor_id && l.actor_id.toLowerCase().includes(q)) ||
        (l.action && l.action.toLowerCase().includes(q)) ||
        (l.workflow_id && l.workflow_id.toLowerCase().includes(q))
      );
    }

    if (logs.length === 0) {
      tbody.innerHTML = '<tr><td colspan="8" class="empty-state">No audit logs recorded yet.</td></tr>';
      return;
    }

    tbody.innerHTML = logs.map(l => {
      const outcomeClass = l.outcome === 'SUCCESS' ? 'color: var(--color-emerald);' : 'color: var(--color-rose);';
      return `
        <tr>
          <td class="cell-mono">${l.timestamp ? l.timestamp.replace('T', ' ').slice(0, 19) : '-'}</td>
          <td><strong>${l.event_type}</strong></td>
          <td class="cell-mono">${l.actor_id}</td>
          <td>${l.role}</td>
          <td><code>${l.action || '-'}</code></td>
          <td class="cell-mono">${l.workflow_id ? `<a href="#" class="wf-link" data-id="${l.workflow_id}">${l.workflow_id.slice(0, 8)}...</a>` : '-'}</td>
          <td><strong style="${outcomeClass}">${l.outcome}</strong></td>
          <td>${l.reason || '-'}</td>
        </tr>
      `;
    }).join('');

    tbody.querySelectorAll('.wf-link').forEach(el => {
      el.addEventListener('click', (e) => {
        e.preventDefault();
        openWorkflowDrawer(el.dataset.id);
      });
    });

  } catch (err) {
    tbody.innerHTML = `<tr><td colspan="8" class="empty-state error">Failed to load audit logs: ${err.message}</td></tr>`;
  }
}

// ==========================================================================
// Workflow Detail Drawer & Live SSE Streaming
// ==========================================================================

async function openWorkflowDrawer(workflowId) {
  state.activeWorkflowId = workflowId;
  const drawer = document.getElementById('workflow-detail-drawer');
  drawer.classList.add('open');

  document.getElementById('drawer-workflow-id').textContent = workflowId;
  document.getElementById('drawer-events-terminal').innerHTML = '<div class="terminal-line system">Fetching workflow state...</div>';

  try {
    const snapshot = await apiFetch(`/api/workflows/${workflowId}`);
    state.activeWorkflowSnapshot = snapshot;
    renderDrawerDetails(snapshot);
    connectEventStream(workflowId);
  } catch (err) {
    showToast(`Failed to open workflow: ${err.message}`, 'error');
  }
}

function renderDrawerDetails(snapshot) {
  const wf = snapshot.workflow || {};
  const stateVal = (wf.state || 'UNKNOWN').toLowerCase();

  const stateBadge = document.getElementById('drawer-state-badge');
  stateBadge.className = `status-badge ${stateVal}`;
  stateBadge.textContent = wf.state || 'UNKNOWN';

  document.getElementById('drawer-occ-version').textContent = wf.version || 1;
  document.getElementById('drawer-tenant-id').textContent = wf.tenant_id || 'tenant-default';

  // HITL Approval button visibility
  const btnApprove = document.getElementById('btn-drawer-approve');
  const pendingApprovals = snapshot.approvals?.filter(a => a.status === 'PENDING') || [];
  btnApprove.style.display = pendingApprovals.length > 0 ? 'inline-flex' : 'none';

  // Outcomes List
  const outcomesContainer = document.getElementById('drawer-outcomes-list');
  const outcomes = wf.contract?.required_outcomes || [];
  if (outcomes.length === 0) {
    outcomesContainer.innerHTML = '<div class="empty-state">No outcome contract specified.</div>';
  } else {
    outcomesContainer.innerHTML = outcomes.map(o => `
      <div class="outcome-item">
        <div>
          <strong>${o.description || o.outcome_id}</strong>
          <div style="font-size: 0.72rem; color: var(--text-muted);">${o.verification_method || 'contract check'}</div>
        </div>
        <span class="status-badge ${wf.state === 'COMPLETED' ? 'completed' : 'executing'}">
          ${wf.state === 'COMPLETED' ? 'VERIFIED' : 'PENDING'}
        </span>
      </div>
    `).join('');
  }

  // Steps List
  const stepsContainer = document.getElementById('drawer-steps-list');
  const steps = snapshot.steps || [];
  if (steps.length === 0) {
    stepsContainer.innerHTML = '<div class="empty-state">No steps executed yet.</div>';
  } else {
    stepsContainer.innerHTML = steps.map(s => `
      <div class="step-item">
        <div>
          <strong>${s.tool_name || s.name}</strong>
          <div style="font-size: 0.72rem; color: var(--text-muted); font-family: var(--font-mono);">${s.step_id}</div>
        </div>
        <span class="status-badge ${(s.status || 'UNKNOWN').toLowerCase()}">${s.status}</span>
      </div>
    `).join('');
  }

  // Initial Events in Terminal
  const terminal = document.getElementById('drawer-events-terminal');
  const events = snapshot.events || [];
  terminal.innerHTML = events.map(e => `
    <div class="terminal-line event">
      [${e.timestamp ? e.timestamp.slice(11, 19) : ''}] <strong>${e.event_type || e.type}</strong>: ${e.detail || e.title || ''}
    </div>
  `).join('');
}

async function connectEventStream(workflowId) {
  if (state.eventSource) {
    state.eventSource.close();
  }

  const terminal = document.getElementById('drawer-events-terminal');
  const pulse = document.getElementById('stream-pulse-indicator');
  pulse.style.background = 'var(--color-cyan)';

  try {
    // Request single-use SSE ticket (eliminating JWT exposure in URLs)
    const ticketData = await apiFetch('/api/auth/sse-ticket', {
      method: 'POST',
      body: JSON.stringify({ workflow_id: workflowId }),
    });

    state.eventSource = new EventSource(`/api/workflows/${workflowId}/events/stream?ticket=${encodeURIComponent(ticketData.ticket)}`);

    state.eventSource.onmessage = (e) => {
    try {
      const ev = JSON.parse(e.data);
      const line = document.createElement('div');
      line.className = 'terminal-line event';
      line.innerHTML = `[${new Date().toISOString().slice(11, 19)}] <strong>${ev.event_type || 'EVENT'}</strong>: ${ev.detail || ev.state || JSON.stringify(ev)}`;
      terminal.appendChild(line);
      terminal.scrollTop = terminal.scrollHeight;

      if (ev.event_type === 'STREAM_END') {
        pulse.style.background = 'var(--text-muted)';
        state.eventSource.close();
      }
    } catch (err) {
      console.warn('Error parsing SSE event:', err);
    }
  };

    state.eventSource.onerror = () => {
      pulse.style.background = 'var(--text-muted)';
      if (state.eventSource) state.eventSource.close();
    };
  } catch (err) {
    console.error('Failed to obtain SSE ticket or connect stream:', err);
    pulse.style.background = 'var(--text-muted)';
  }
}

function closeDrawer() {
  document.getElementById('workflow-detail-drawer').classList.remove('open');
  if (state.eventSource) {
    state.eventSource.close();
    state.eventSource = null;
  }
}

// ==========================================================================
// Modals & Action Hub
// ==========================================================================

async function openDiagnosticsModal(workflowId) {
  const modal = document.getElementById('modal-diagnostics');
  const body = document.getElementById('diagnostics-modal-body');
  modal.classList.add('open');
  body.innerHTML = '<div class="loading-spinner"></div>';

  try {
    const diag = await apiFetch(`/api/workflows/${workflowId}/diagnostics`);
    body.innerHTML = `
      <div class="callout ${diag.is_stuck ? 'callout-warning' : 'callout-info'}">
        <strong>Status:</strong> ${diag.is_stuck ? 'STUCK / STALLED' : 'HEALTHY'}
        ${diag.stuck_reason ? `<div style="margin-top: 6px;">${diag.stuck_reason}</div>` : ''}
      </div>

      <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 12px; font-size: 0.82rem;">
        <div><strong>Current State:</strong> ${diag.state}</div>
        <div><strong>OCC Version:</strong> v${diag.version}</div>
        <div><strong>Age:</strong> ${diag.age_seconds} seconds</div>
        <div><strong>Event Count:</strong> ${diag.event_count}</div>
      </div>

      ${diag.operation_claim ? `
        <div class="panel" style="margin-top: 12px; padding: 12px;">
          <h4 style="font-size: 0.8rem; margin-bottom: 6px;">Distributed Operation Claim Lease</h4>
          <div style="font-size: 0.75rem; font-family: var(--font-mono);">
            Owner Worker: ${diag.operation_claim.owner_worker_id || 'none'}<br>
            Status: ${diag.operation_claim.status}<br>
            Expires At: ${diag.operation_claim.lease_expires_at || 'n/a'}
          </div>
        </div>
      ` : ''}
    `;

    document.getElementById('btn-diagnostics-quick-recover').onclick = () => {
      closeModals();
      openRecoverModal(workflowId, diag.version);
    };
  } catch (err) {
    body.innerHTML = `<div class="callout callout-warning">Error fetching diagnostics: ${err.message}</div>`;
  }
}

function openRecoverModal(workflowId, version) {
  state.activeWorkflowId = workflowId;
  const modal = document.getElementById('modal-recover');
  document.getElementById('modal-recover-version').textContent = `v${version || 1}`;
  document.getElementById('recover-reason-input').value = '';
  modal.classList.add('open');
}

async function handleRecoverSubmit() {
  const reason = document.getElementById('recover-reason-input').value.trim();
  const force = document.getElementById('recover-force-checkbox')?.checked || false;

  if (!reason) {
    showToast('Please provide a justification reason for the audit trail.', 'warning');
    return;
  }

  try {
    const res = await apiFetch(`/api/workflows/${state.activeWorkflowId}/recover`, {
      method: 'POST',
      body: JSON.stringify({ reason, force }),
    });

    showToast(`Recovery dispatched successfully for ${state.activeWorkflowId}`, 'success');
    closeModals();
    refreshCurrentView();
  } catch (err) {
    showToast(`Recovery failed: ${err.message}`, 'error');
  }
}

async function openApproveModal(workflowId) {
  state.activeWorkflowId = workflowId;
  const modal = document.getElementById('modal-approve');
  const body = document.getElementById('modal-approve-body');
  modal.classList.add('open');

  try {
    const data = await apiFetch(`/api/workflows/${workflowId}/approvals`);
    const pending = (data.approvals || []).find(a => a.status === 'PENDING');
    if (!pending) {
      body.innerHTML = '<div class="empty-state">No pending approvals found.</div>';
      return;
    }

    state.activeApprovalId = pending.approval_id;
    body.innerHTML = `
      <div class="callout callout-info">
        <strong>Plan Description:</strong> ${pending.plan_description || 'Proposed recovery action requiring human authorization.'}
      </div>
      <div style="font-size: 0.82rem; margin-top: 10px;">
        <strong>Target Action:</strong> <code>${pending.action_tool || 'tool_execution'}</code>
      </div>
    `;
  } catch (err) {
    body.innerHTML = `<div class="callout callout-warning">Failed to load approval: ${err.message}</div>`;
  }
}

async function handleApproveSubmit(approved) {
  try {
    await apiFetch(`/api/workflows/${state.activeWorkflowId}/approve/${state.activeApprovalId}`, {
      method: 'POST',
      body: JSON.stringify({ approved, reason: approved ? 'Operator approved plan' : 'Operator rejected plan' }),
    });

    showToast(`Approval decision recorded (${approved ? 'APPROVED' : 'REJECTED'})`, 'success');
    closeModals();
    refreshCurrentView();
  } catch (err) {
    showToast(`Approval submission failed: ${err.message}`, 'error');
  }
}

function openCancelModal(workflowId) {
  state.activeWorkflowId = workflowId;
  document.getElementById('cancel-reason-input').value = '';
  document.getElementById('modal-cancel').classList.add('open');
}

async function handleCancelSubmit() {
  const reason = document.getElementById('cancel-reason-input').value.trim();
  if (!reason) {
    showToast('Mandatory cancellation reason required.', 'warning');
    return;
  }

  try {
    await apiFetch(`/api/workflows/${state.activeWorkflowId}/cancel`, {
      method: 'POST',
      body: JSON.stringify({ reason }),
    });

    showToast(`Workflow ${state.activeWorkflowId} transitioned to ESCALATED`, 'success');
    closeModals();
    refreshCurrentView();
  } catch (err) {
    showToast(`Cancellation failed: ${err.message}`, 'error');
  }
}

async function handleLaunchScenario() {
  const scenario = document.getElementById('scenario-select').value;
  try {
    const res = await apiFetch(`/api/scenarios/${scenario}`, { method: 'POST' });
    showToast(`Scenario launched: ${res.workflow_id || 'OK'}`, 'success');
    closeModals();
    refreshCurrentView();
  } catch (err) {
    showToast(`Launch failed: ${err.message}`, 'error');
  }
}

function closeModals() {
  document.querySelectorAll('.modal').forEach(m => m.classList.remove('open'));
}

function refreshCurrentView() {
  if (state.activeTab === 'fleet') loadFleetOverview();
  else if (state.activeTab === 'workflows') loadWorkflows();
  else if (state.activeTab === 'stuck') loadStuckWorkflows();
  else if (state.activeTab === 'audit') loadAuditLogs();

  if (state.activeWorkflowId && document.getElementById('workflow-detail-drawer').classList.contains('open')) {
    openWorkflowDrawer(state.activeWorkflowId);
  }
}

// ==========================================================================
// Helpers & Utilities
// ==========================================================================

function formatAge(dateStr) {
  if (!dateStr) return '-';
  try {
    const diff = Math.floor((Date.now() - new Date(dateStr).getTime()) / 1000);
    if (diff < 60) return `${diff}s ago`;
    if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
    return `${Math.floor(diff / 3600)}h ago`;
  } catch {
    return dateStr;
  }
}

// ==========================================================================
// App Initialization
// ==========================================================================

document.addEventListener('DOMContentLoaded', () => {
  initTabs();

  // Persona & Tenant Selectors
  document.getElementById('persona-select').addEventListener('change', (e) => {
    state.currentPersona = e.target.value;
    showToast(`Switched persona to ${PERSONAS[state.currentPersona].name}`, 'info');
    refreshCurrentView();
  });

  document.getElementById('tenant-select').addEventListener('change', (e) => {
    state.currentTenant = e.target.value;
    state.pageOffset = 0;
    refreshCurrentView();
  });

  // Manual & Auto-refresh
  document.getElementById('btn-manual-refresh').addEventListener('click', () => refreshCurrentView());
  document.getElementById('auto-refresh-select').addEventListener('change', (e) => {
    const val = parseInt(e.target.value, 10);
    state.autoRefreshInterval = val;
    if (state.autoRefreshTimer) clearInterval(state.autoRefreshTimer);
    if (val > 0) {
      state.autoRefreshTimer = setInterval(refreshCurrentView, val);
    }
  });

  // Start initial auto-refresh timer
  if (state.autoRefreshInterval > 0) {
    state.autoRefreshTimer = setInterval(refreshCurrentView, state.autoRefreshInterval);
  }

  // Filters & Search
  document.getElementById('filter-state-select').addEventListener('change', () => { state.pageOffset = 0; loadWorkflows(); });
  document.getElementById('filter-scenario-select').addEventListener('change', () => { state.pageOffset = 0; loadWorkflows(); });
  document.getElementById('filter-stuck-only').addEventListener('change', () => { state.pageOffset = 0; loadWorkflows(); });
  document.getElementById('workflow-search-input').addEventListener('input', () => { state.pageOffset = 0; loadWorkflows(); });
  document.getElementById('filter-audit-event-type').addEventListener('change', loadAuditLogs);
  document.getElementById('audit-search-input').addEventListener('input', loadAuditLogs);

  // Pagination
  document.getElementById('btn-prev-page').addEventListener('click', () => {
    state.pageOffset = Math.max(0, state.pageOffset - state.pageLimit);
    loadWorkflows();
  });
  document.getElementById('btn-next-page').addEventListener('click', () => {
    state.pageOffset += state.pageLimit;
    loadWorkflows();
  });

  // Drawer Controls
  document.getElementById('btn-close-drawer').addEventListener('click', closeDrawer);
  document.getElementById('btn-drawer-diagnose').addEventListener('click', () => openDiagnosticsModal(state.activeWorkflowId));
  document.getElementById('btn-drawer-recover').addEventListener('click', () => {
    openRecoverModal(state.activeWorkflowId, state.activeWorkflowSnapshot?.workflow?.version || 1);
  });
  document.getElementById('btn-drawer-approve').addEventListener('click', () => openApproveModal(state.activeWorkflowId));
  document.getElementById('btn-drawer-cancel').addEventListener('click', () => openCancelModal(state.activeWorkflowId));

  // Modal Closers
  document.querySelectorAll('[data-close]').forEach(btn => {
    btn.addEventListener('click', closeModals);
  });

  // Modal Action Buttons
  document.getElementById('btn-confirm-recover').addEventListener('click', handleRecoverSubmit);
  document.getElementById('btn-confirm-approval').addEventListener('click', () => handleApproveSubmit(true));
  document.getElementById('btn-reject-approval').addEventListener('click', () => handleApproveSubmit(false));
  document.getElementById('btn-confirm-cancel').addEventListener('click', handleCancelSubmit);
  document.getElementById('btn-open-launch').addEventListener('click', () => document.getElementById('modal-launch').classList.add('open'));
  document.getElementById('btn-confirm-launch').addEventListener('click', handleLaunchScenario);
  document.getElementById('btn-refresh-stuck').addEventListener('click', loadStuckWorkflows);

  // Initial Load
  loadFleetOverview();
});
