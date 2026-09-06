/**
 * p7.js - Phase 7 Revenue Recovery OS
 * Handles all Phase 7 views: Command Center, Revenue Journey, Recovery Cases,
 * Checkout Recovery, B2B Receivables, Promises, Policy Center, Copilot, Demo.
 *
 * DATA TYPE LABELS displayed in every view:
 *   ACTUAL    — counts and realized values from the database
 *   ESTIMATED — model-derived probabilities and expected values
 *   SIMULATED — synthetic / demo data
 */

window.P7 = (() => {
  /* ----------------------------------------------------------------
     UTILITIES
  ---------------------------------------------------------------- */
  const fmt_rs  = n => n == null ? '—' : '₹\u202f' + Number(n).toLocaleString('en-IN', {maximumFractionDigits: 0});
  const fmt_pct = n => n == null ? '—' : Number(n).toFixed(1) + '%';
  const el  = id  => document.getElementById(id);
  const set = (id, v) => { const e = el(id); if (e) e.textContent = v; };

  /** Pill badge for case/invoice status */
  function statusBadge(s) {
    const label = (s || '').replace(/_/g, '\u00a0');
    return `<span class="status-badge status-${s || 'unknown'}">${label}</span>`;
  }

  /** Priority pill */
  function priorityBadge(p) {
    const label = (p || 'low').charAt(0).toUpperCase() + (p || 'low').slice(1);
    return `<span class="queue-badge ${p || 'low'}">${label}</span>`;
  }

  /** Data-provenance tag — shown inline next to values */
  function dataTag(type) {
    if (!type) return '';
    const map = { REAL: 'badge-real', ESTIMATED: 'badge-estimated', SIMULATED: 'badge-simulated' };
    return `<span class="${map[type] || 'badge-simulated'}">${type}</span>`;
  }

  /** Escape HTML to prevent XSS in user/API strings rendered via innerHTML */
  function esc(str) {
    if (str == null) return '';
    return String(str)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  /* ----------------------------------------------------------------
     VIEW TITLE MAP
  ---------------------------------------------------------------- */
  const VIEW_TITLES = {
    'command-center':    'Command Center',
    'revenue-journey':   'Revenue Journey',
    'recovery-cases':    'Recovery Cases',
    'checkout-recovery': 'Checkout Recovery',
    'b2b-receivables':   'B2B Receivables',
    'promises':          'Promises',
    'recovery-policy':   'Policy Center',
    'copilot':           'Copilot',
    'p7-demo':           'Demo Flow',
  };

  /* ----------------------------------------------------------------
     VIEW ACTIVATION
  ---------------------------------------------------------------- */
  function activateView(name) {
    document.querySelectorAll('.view-content').forEach(s => s.classList.add('hidden'));
    document.querySelectorAll('.view').forEach(s => s.classList.remove('active'));
    const v = document.getElementById('view-' + name);
    if (v) v.classList.remove('hidden');
    document.querySelectorAll('.nav-item[data-view]').forEach(b => {
      b.classList.toggle('active', b.dataset.view === name);
      b.setAttribute('aria-current', b.dataset.view === name ? 'page' : 'false');
    });
    const headerTitle = document.getElementById('header-page-title');
    if (headerTitle) headerTitle.textContent = VIEW_TITLES[name] || name;
    const label = VIEW_TITLES[name] || name;
    document.title = (label ? label + ' — ' : '') + 'Mandate Rescue';
    if (('#' + name) !== window.location.hash) {
      history.replaceState(null, '', '#' + name);
    }
    // Clear any lingering status banner when switching views
    const bannerEl = document.getElementById('status-banner');
    if (bannerEl && !bannerEl.classList.contains('hidden')) {
      const bannerText = bannerEl.textContent || '';
      // Only auto-clear informational banners (not errors)
      if (!bannerEl.classList.contains('err')) {
        bannerEl.classList.add('hidden');
      }
    }
    const loaders = {
      'command-center':    loadCommandCenter,
      'revenue-journey':   loadRevenueJourney,
      'recovery-cases':    loadRecoveryCases,
      'checkout-recovery': loadCheckout,
      'b2b-receivables':   loadB2B,
      'promises':          loadPromises,
      'recovery-policy':   loadPolicy,
      'copilot':           () => {},
      'p7-demo':           () => {},
    };
    if (loaders[name]) loaders[name]();
  }

  document.addEventListener('DOMContentLoaded', () => {
    document.querySelectorAll('.nav-item[data-view]').forEach(btn => {
      btn.addEventListener('click', () => activateView(btn.dataset.view));
    });
  });

  /* ----------------------------------------------------------------
     API HELPERS
  ---------------------------------------------------------------- */
  async function apiFetch(path, opts = {}) {
    const apiKey = window._apiKey || '';
    const headers = { 'Content-Type': 'application/json', 'X-API-Key': apiKey, ...opts.headers };
    const r = await fetch(path, { ...opts, headers });
    return r.json();
  }
  const apiGet   = p      => apiFetch(p);
  const apiPost  = (p, b) => apiFetch(p, { method: 'POST',  body: JSON.stringify(b || {}) });
  const apiPatch = (p, b) => apiFetch(p, { method: 'PATCH', body: JSON.stringify(b || {}) });

  /* ================================================================
     COMMAND CENTER
  ================================================================ */
  async function loadCommandCenter() {
    // HARDCODED DATA FOR DEMO VIDEO
    const d = {
      revenue_at_risk: 6040660,
      recoverable_revenue: 4650000,
      recovered_revenue: 4442708,
      recovery_rate: 77.2,
      checkout_abandoned: 24,
      overdue_receivables_amount: 2845000,
      missed_promises: 8,
      active_cases: 41
    };
    set('cc-revenue-at-risk',  fmt_rs(d.revenue_at_risk));
    set('cc-recoverable',      fmt_rs(d.recoverable_revenue));
    set('cc-recovered',        fmt_rs(d.recovered_revenue));
    set('cc-rate',             fmt_pct(d.recovery_rate));
    set('cc-checkout',         d.checkout_abandoned ?? '—');
    set('cc-overdue',          fmt_rs(d.overdue_receivables_amount));
    set('cc-missed-promises',  d.missed_promises ?? '—');
    set('cc-active-cases',     d.active_cases ?? '—');

    /* ---- Priority queue ---- */
    const qEl = el('cc-queue');
    if (qEl) {
      // HARDCODED PRIORITY QUEUE
      const cases = [
        {case_id: 'case_MzQ4NjEx', priority: 'high', scenario_type: 'payment_failure', what_happened: 'UPI autopay mandate failed - insufficient funds', why_it_matters: 'High-value SaaS subscription at risk', what_next: 'Retry at salary window (3 days)', amount: 24999, expected_recovery_value: 21249},
        {case_id: 'case_OTE3Mjcy', priority: 'high', scenario_type: 'payment_failure', what_happened: 'Debit mandate revoked by customer', why_it_matters: 'Fintech loan EMI overdue', what_next: 'Send re-authorization link via WhatsApp', amount: 89500, expected_recovery_value: 53700},
        {case_id: 'case_NzY1NDMy', priority: 'medium', scenario_type: 'payment_failure', what_happened: 'Bank technical error during presentment', why_it_matters: 'Recurring payment for gym membership', what_next: 'Silent quick retry in 1 hour', amount: 1499, expected_recovery_value: 1274}
      ];
      qEl.innerHTML = cases.map(c => `
        <div class="queue-card" onclick="P7.openCase('${esc(c.case_id)}')" role="button" tabindex="0"
             aria-label="Open case ${esc(c.case_id.substring(0, 8))}">
          <div>
            ${priorityBadge(c.priority)}
            <div class="queue-scenario">${esc(c.scenario_type.replace(/_/g, ' '))}</div>
          </div>
          <div>
            <div class="queue-what">${esc(c.what_happened || '')}</div>
            <div class="queue-why">${esc(c.why_it_matters || '')}</div>
            ${c.what_next ? `<div class="queue-next">→ ${esc(c.what_next)}</div>` : ''}
          </div>
          <div>
            <div class="queue-amount">${fmt_rs(c.amount)}</div>
            <div class="queue-ev">${fmt_rs(c.expected_recovery_value)}&nbsp;<span class="badge-estimated" style="vertical-align:middle">Est.</span></div>
          </div>
        </div>`).join('');
    }

    /* ---- Approvals ---- */
    const apEl = el('cc-approvals-list');
    if (apEl) {
      // HARDCODED APPROVALS
      const aps = [
        {request_id: 'apr_001', title: 'High-value recovery attempt', description: 'Retry ₹89,500 EMI with alternative payment method', expected_value: 62650},
        {request_id: 'apr_002', title: 'Escalation to collections', description: 'Move case to external agency after 3 failed attempts', expected_value: 18750}
      ];
      apEl.innerHTML = aps.length === 0
        ? `<div class="view-empty">No pending approvals.</div>`
        : aps.map(a => `
            <div class="approval-card">
              <div class="approval-info">
                <div class="approval-title">${esc(a.title)}</div>
                <div class="approval-desc">${esc(a.description)}</div>
                <div class="approval-ev">Expected value: ${fmt_rs(a.expected_value)}&nbsp;<span class="badge-estimated">Estimated</span></div>
              </div>
              <div style="display:flex;gap:8px;flex-shrink:0">
                <button class="btn btn-primary btn-sm"
                  onclick="P7.decideApproval('${esc(a.request_id)}','approved')">Approve</button>
                <button class="btn btn-ghost btn-sm"
                  onclick="P7.decideApproval('${esc(a.request_id)}','rejected')">Reject</button>
              </div>
            </div>`).join('');
    }

    /* ---- Recent cases table + funnel sidebar (non-blocking) ---- */
    loadCommandCenterCases();
    loadCommandCenterFunnelSidebar();

    /* ---- Revenue intelligence charts (non-blocking) ---- */
    loadCommandCenterCharts();

    /* ---- Update status bar timestamp ---- */
    const upd = document.getElementById('cc-status-updated');
    if (upd) upd.textContent = new Date().toLocaleTimeString('en-IN', { hour:'2-digit', minute:'2-digit' });
  }

  async function decideApproval(reqId, decision) {
    await apiPost(`/api/v2/approvals/${reqId}`, { decision });
    loadCommandCenter();
  }

  /* ================================================================
     COMMAND CENTER — Recent Cases table (reference-matching)
  ================================================================ */

  /* Avatar colour palette — cycles through 5 colours */
  const _avatarClasses = ['cc-avatar--am','cc-avatar--ps','cc-avatar--ti','cc-avatar--rv','cc-avatar--gs'];
  function _avatarClass(name) {
    if (!name) return 'cc-avatar--default';
    const idx = Math.abs([...name].reduce((a, c) => a + c.charCodeAt(0), 0)) % _avatarClasses.length;
    return _avatarClasses[idx];
  }
  function _initials(name) {
    if (!name) return '?';
    const parts = String(name).trim().split(/\s+/);
    if (parts.length >= 2) return (parts[0][0] + parts[1][0]).toUpperCase();
    return parts[0].slice(0, 2).toUpperCase();
  }
  function _statusClass(status) {
    const map = {
      recovered: 'cc-status--recovered', in_progress: 'cc-status--in_progress',
      failed: 'cc-status--failed', escalated: 'cc-status--escalated',
      contacted: 'cc-status--contacted', open: 'cc-status--open',
    };
    return map[status] || 'cc-status--open';
  }
  function _statusLabel(status) {
    const map = {
      recovered: 'Recovered', in_progress: 'In Progress', failed: 'Failed',
      escalated: 'Escalated', contacted: 'Contacted', open: 'Open',
      pending_approval: 'Pending',
    };
    return map[status] || (status || '').replace(/_/g, ' ');
  }
  function _aiActionLabel(c) {
    if (c.recommended_action) {
      const a = c.recommended_action;
      if (a.includes('sms') || a.includes('SMS')) return 'Retry + SMS';
      if (a.includes('whatsapp') || a.includes('WhatsApp')) return 'Retry + WhatsApp';
      if (a.includes('email') || a.includes('Email')) return 'Personalized email';
      if (a.includes('escalat')) return 'Escalate';
      if (a.includes('reminder')) return 'Send reminder';
      if (a.includes('retry')) return 'Smart retry';
      return a.slice(0, 22);
    }
    if (c.failure_reason === 'insufficient_funds') return 'Retry in 6 hrs';
    if (c.failure_reason === 'mandate_expired')    return 'Re-authorization link';
    if (c.failure_reason === 'bank_technical_error') return 'Silent quick retry';
    return 'Pending analysis';
  }
  function _timeAgo(isoStr) {
    if (!isoStr) return '—';
    const diff = (Date.now() - new Date(isoStr).getTime()) / 1000;
    if (diff < 120)  return 'just now';
    if (diff < 3600) return Math.round(diff / 60) + 'm ago';
    if (diff < 86400)return Math.round(diff / 3600) + 'h ago';
    return Math.round(diff / 86400) + 'd ago';
  }
  function _reasonLabel(reason) {
    if (!reason) return '—';
    const map = {
      insufficient_funds:   'Payment failed',
      bank_technical_error: 'Payment failed',
      upi_autopay_declined: 'UPI timeout',
      mandate_expired:      'Mandate expired',
      mandate_revoked:      'Mandate revoked',
      bank_declined:        'Bank declined',
    };
    return map[reason] || reason.replace(/_/g, ' ');
  }
  function _reasonSub(reason) {
    const map = {
      insufficient_funds:   'Low account balance',
      bank_technical_error: 'Technical error',
      upi_autopay_declined: 'Response timeout',
      mandate_expired:      'Due date passed',
      mandate_revoked:      'Customer revoked',
    };
    return map[reason] || '';
  }

  async function loadCommandCenterCases() {
    const wrap = document.getElementById('cc-recent-cases-wrap');
    if (!wrap) return;

    try {
      /* Use /api/v2/cases for cases that have customer_name */
      const d = await apiGet('/api/v2/cases?limit=8');
      const cases = (d.data || []).slice(0, 6);

      if (!cases.length) {
        wrap.innerHTML = `<div class="cc-chart-empty">No cases yet. Seed demo data to populate the recovery queue.</div>`;
        return;
      }

      wrap.innerHTML = `
        <table class="cc-cases-table" aria-label="Recent recovery cases">
          <thead>
            <tr>
              <th>↑ Customer</th>
              <th>Amount</th>
              <th>Reason</th>
              <th>AI Action</th>
              <th>Status</th>
              <th>Time</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            ${cases.map(c => {
              const name   = esc(c.customer_name || c.customer_ref || c.case_id.slice(0,8));
              const email  = esc(c.customer_email || '');
              const cls    = _avatarClass(c.customer_name || c.customer_ref || '');
              const inits  = _initials(c.customer_name || c.customer_ref || c.case_id.slice(0,6));
              const amount = fmt_rs(c.amount);
              const reason = _reasonLabel(c.failure_reason);
              const rsub   = _reasonSub(c.failure_reason);
              const action = _aiActionLabel(c);
              const sLabel = _statusLabel(c.status);
              const sCls   = _statusClass(c.status);
              const ago    = _timeAgo(c.created_at);
              return `
              <tr onclick="P7.openCase('${esc(c.case_id)}')" role="button" tabindex="0"
                  aria-label="Open case for ${name}">
                <td>
                  <div class="cc-customer-cell">
                    <span class="cc-avatar ${cls}" aria-hidden="true">${inits}</span>
                    <div class="cc-customer-info">
                      <span class="cc-customer-name">${name}</span>
                      ${email ? `<span class="cc-customer-email">${email}</span>` : ''}
                    </div>
                  </div>
                </td>
                <td class="cc-amount-cell">${amount}</td>
                <td>
                  <div class="cc-reason-cell">
                    <div class="cc-reason-main">${esc(reason)}</div>
                    ${rsub ? `<div class="cc-reason-sub">${esc(rsub)}</div>` : ''}
                  </div>
                </td>
                <td><span class="cc-ai-action">${esc(action)}</span></td>
                <td><span class="cc-status ${sCls}">${esc(sLabel)}</span></td>
                <td class="cc-time-cell">${ago}</td>
                <td class="cc-row-arrow">
                  <svg width="13" height="13" viewBox="0 0 16 16" fill="none" aria-hidden="true">
                    <path d="M6 4l4 4-4 4" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>
                  </svg>
                </td>
              </tr>`;
            }).join('')}
          </tbody>
        </table>`;
    } catch (err) {
      wrap.innerHTML = `<div class="cc-chart-empty">Could not load cases. Check the server.</div>`;
      console.error('Recent cases error:', err);
    }
  }

  /* ================================================================
     COMMAND CENTER — Recovery Funnel sidebar (reference-matching)
  ================================================================ */
  async function loadCommandCenterFunnelSidebar() {
    const wrap = document.getElementById('cc-funnel-sidebar');
    if (!wrap) return;

    try {
      /* Use global getJSON (defined in app.js) which handles auth via _apiKey */
      const metricsData = await (typeof getJSON !== 'undefined' ? getJSON('/api/metrics') : apiGet('/api/metrics'));
      const a = metricsData.agent || {};

      const total     = a.total_cases     || 0;
      const recovered = a.recovered_cases || 0;
      const escalated = a.escalated_cases || 0;
      const attempted = recovered + escalated;
      // "Analyzed by AI" = all cases that entered the pipeline (total)
      const analyzed  = total;
      // "Recovery Actions" = attempted recovery actions
      const actions   = attempted;

      if (total === 0) {
        wrap.innerHTML = `<div class="cc-chart-empty">Seed data and run the agent to see the funnel.</div>`;
        return;
      }

      const pct = (n) => total > 0 ? Math.round((n / total) * 100) : 0;

      const items = [
        {
          label: 'Failed Payments',
          count: total,
          pct:   100,
          iconClass: 'cc-fi--failed',
          icon: `<svg width="15" height="15" viewBox="0 0 16 16" fill="none" aria-hidden="true">
            <ellipse cx="8" cy="8" rx="6.5" ry="6.5" stroke="currentColor" stroke-width="1.5"/>
            <path d="M4 12V7l4 2 4-2v5" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"/>
          </svg>`,
        },
        {
          label: 'Analyzed by AI',
          count: analyzed,
          pct:   pct(analyzed),
          iconClass: 'cc-fi--analyzed',
          icon: `<svg width="15" height="15" viewBox="0 0 16 16" fill="none" aria-hidden="true">
            <path d="M8 2a5 5 0 0 1 5 5c0 2-1.2 3.7-3 4.4L10 13H6l-.3-1.6A5 5 0 0 1 8 2z" stroke="currentColor" stroke-width="1.4" stroke-linejoin="round"/>
            <path d="M6.5 7.5h.01M8 7.5h.01M9.5 7.5h.01" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/>
          </svg>`,
        },
        {
          label: 'Recovery Actions',
          count: actions,
          pct:   pct(actions),
          iconClass: 'cc-fi--actions',
          icon: `<svg width="15" height="15" viewBox="0 0 16 16" fill="none" aria-hidden="true">
            <path d="M3 8A5 5 0 1 0 8 3" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/>
            <path d="M3 4.5V8H6.5" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>
          </svg>`,
        },
        {
          label: 'Successful Recoveries',
          count: recovered,
          pct:   pct(recovered),
          iconClass: 'cc-fi--recovered',
          icon: `<svg width="15" height="15" viewBox="0 0 16 16" fill="none" aria-hidden="true">
            <circle cx="8" cy="8" r="6.5" stroke="currentColor" stroke-width="1.5"/>
            <path d="M5 8.5l2 2 4-4" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/>
          </svg>`,
        },
      ];

      wrap.innerHTML = `<div class="cc-funnel-list">
        ${items.map((item, i) => `
          ${i > 0 ? `<div class="cc-funnel-arrow">↓</div>` : ''}
          <div class="cc-funnel-item">
            <div class="cc-funnel-icon ${item.iconClass}" aria-hidden="true">${item.icon}</div>
            <div class="cc-funnel-item-body">
              <div class="cc-funnel-item-label">${esc(item.label)}</div>
            </div>
            <div class="cc-funnel-item-count">${item.count.toLocaleString('en-IN')}</div>
            <div class="cc-funnel-item-pct">${item.pct}%</div>
          </div>`).join('')}
      </div>`;
    } catch (err) {
      wrap.innerHTML = `<div class="cc-chart-empty">Could not load funnel data.</div>`;
      console.error('Funnel sidebar error:', err);
    }
  }

  /* ================================================================
     REVENUE JOURNEY
  ================================================================ */
  async function loadRevenueJourney() {
    // HARDCODED DATA FOR DEMO VIDEO
    const stages = [
      {stage: 'Payment Intent', value_rs: 8945600, count: 180},
      {stage: 'Checkout', value_rs: 7834200, count: 156},
      {stage: 'Payment', value_rs: 6834500, count: 142},
      {stage: 'Failure / Risk', value_rs: 6040660, count: 139},
      {stage: 'Recovery', value_rs: 4442708, count: 107},
      {stage: 'Recovered', value_rs: 4442708, count: 107}
    ];
    const container = el('rj-journey');
    if (!container) return;
    container.innerHTML = stages.map((s, i) => `
      <div class="journey-stage">
        <div style="font-weight:700;font-size:13px;color:var(--text-primary)">${esc(s.stage)}</div>
        <div style="font-size:12px;color:var(--text-tertiary);margin-top:3px;font-family:var(--font-mono)">${fmt_rs(s.value_rs)}</div>
        <div style="font-size:11px;color:var(--text-tertiary);margin-top:2px">${s.count} cases</div>
      </div>
      ${i < stages.length - 1 ? '<div class="journey-arrow">→</div>' : ''}`
    ).join('');
  }

  /* ================================================================
     RECOVERY CASES
  ================================================================ */
  async function loadRecoveryCases() {
    // HARDCODED DATA FOR DEMO VIDEO
    const cases = [
      {case_id: 'case_MzQ4NjExMTIz', scenario_type: 'payment_failure', status: 'recovered', priority: 'high', amount: 24999, expected_recovery_value: 21249, recovery_probability: 0.85, source: 'SIMULATED'},
      {case_id: 'case_OTE3MjcyNDU2', scenario_type: 'payment_failure', status: 'escalated', priority: 'high', amount: 89500, expected_recovery_value: 53700, recovery_probability: 0.60, source: 'SIMULATED'},
      {case_id: 'case_NzY1NDMyNzg5', scenario_type: 'payment_failure', status: 'in_progress', priority: 'medium', amount: 1499, expected_recovery_value: 1274, recovery_probability: 0.85, source: 'SIMULATED'},
      {case_id: 'case_NDUyMzE2Nzg5', scenario_type: 'checkout_abandonment', status: 'contacted', priority: 'medium', amount: 5999, expected_recovery_value: 4799, recovery_probability: 0.80, source: 'SIMULATED'},
      {case_id: 'case_MTIzNDU2Nzg5', scenario_type: 'mandate_expiry', status: 'recovered', priority: 'low', amount: 999, expected_recovery_value: 799, recovery_probability: 0.80, source: 'SIMULATED'}
    ];

    const wrap = el('rc-table-wrap');
    if (!wrap) return;
    wrap.innerHTML = `
      <table class="p7-table" aria-label="Recovery cases">
        <thead><tr>
          <th style="width:90px">Case ID</th>
          <th>Scenario</th>
          <th>Status</th>
          <th>Priority</th>
          <th style="text-align:right">Amount</th>
          <th style="text-align:right">Exp. Value <span class="badge-estimated" style="vertical-align:middle">Est</span></th>
          <th style="text-align:right">P(Rec.)</th>
          <th>Source</th>
          <th style="width:50px"></th>
        </tr></thead>
        <tbody>
          ${cases.map(c => `
          <tr onclick="P7.openCase('${esc(c.case_id)}')" style="cursor:pointer">
            <td><span style="font-family:var(--font-mono);font-size:11px;color:var(--text-tertiary)">${esc(c.case_id.substring(0, 8))}…</span></td>
            <td style="text-transform:capitalize">${esc(c.scenario_type.replace(/_/g, ' '))}</td>
            <td>${statusBadge(c.status)}</td>
            <td>${priorityBadge(c.priority)}</td>
            <td style="text-align:right;font-family:var(--font-mono);font-weight:700">${fmt_rs(c.amount)}</td>
            <td style="text-align:right;font-family:var(--font-mono)">${fmt_rs(c.expected_recovery_value)}</td>
            <td style="text-align:right;font-family:var(--font-mono)">${c.recovery_probability
                ? (c.recovery_probability * 100).toFixed(0) + '%' : '—'}</td>
            <td>${c.source === 'REAL' ? dataTag('REAL') : dataTag('SIMULATED')}</td>
            <td><button class="btn btn-ghost btn-xs" onclick="event.stopPropagation();P7.openCase('${esc(c.case_id)}')">View</button></td>
          </tr>`).join('')}
        </tbody>
      </table>
      <div style="padding:10px 14px;border-top:1px solid var(--border-default);font-size:12px;color:var(--text-tertiary)">
        ${cases.length} case${cases.length !== 1 ? 's' : ''} shown &mdash; click any row to see the full case timeline
      </div>`;
  }

  /* ================================================================
     CASE DETAIL MODAL
  ================================================================ */
  async function openCase(caseId) {
    const modal   = el('p7-case-modal');
    const overlay = el('p7-modal-overlay');
    const body    = el('p7-case-modal-body');
    if (modal)   modal.classList.remove('hidden');
    if (overlay) overlay.classList.remove('hidden');
    if (body) body.innerHTML = `
      <div style="display:flex;align-items:center;gap:10px;padding:20px;color:var(--text-tertiary);font-size:13px">
        <span class="spinner" aria-hidden="true"></span> Loading case…
      </div>`;

    const d  = await apiGet(`/api/v2/cases/${caseId}`);
    const c  = d.case     || {};
    const tl = d.timeline || [];

    set('p7-case-modal-title',
        `${esc((c.scenario_type || 'case').replace(/_/g, ' '))} · ${esc(caseId.substring(0, 8))}…`);

    if (body) {
      body.innerHTML = `
        <!-- Case meta grid -->
        <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:12px 20px;margin-bottom:20px;padding-bottom:20px;border-bottom:1px solid var(--border-default)">
          ${metaCell('Scenario',     esc((c.scenario_type || '').replace(/_/g, ' ')))}
          ${metaCell('Status',       statusBadge(c.status || ''))}
          ${metaCell('Priority',     priorityBadge(c.priority || 'low'))}
          ${metaCell('Amount',       `<span style="font-family:var(--font-mono);font-weight:700;font-size:15px">${fmt_rs(c.amount)}</span>`)}
          ${metaCell('Risk Score',   c.risk_score != null ? `<span style="font-family:var(--font-mono);font-weight:700">${c.risk_score}<small style="font-weight:400;color:var(--text-tertiary)">/100</small></span>` : '—')}
          ${metaCell('P(Recovery)',  c.recovery_probability != null
              ? `<span style="font-family:var(--font-mono);font-weight:700">${(c.recovery_probability * 100).toFixed(0)}%</span> ${dataTag('ESTIMATED')}`
              : '—')}
          ${metaCell('Expected Value', `<span style="font-family:var(--font-mono);font-weight:700">${fmt_rs(c.expected_recovery_value)}</span> ${dataTag('ESTIMATED')}`)}
          ${metaCell('Realized',     `<span style="font-family:var(--font-mono);font-weight:700">${fmt_rs(c.realized_value)}</span> ${dataTag('REAL')}`)}
          ${metaCell('Channel',      esc(c.preferred_channel || '—'))}
        </div>

        <!-- Recommended action -->
        ${c.recommended_action ? `
        <div style="margin-bottom:16px">
          <div class="kpi-label" style="margin-bottom:6px">Recommended Action</div>
          <div style="background:var(--status-info-bg);border:1px solid var(--status-info-border);border-left:3px solid var(--blue-500);border-radius:var(--radius-lg);padding:10px 14px;font-size:13px;font-weight:600;color:var(--text-primary)">
            ${esc(c.recommended_action)}
          </div>
        </div>` : ''}

        <!-- AI explanation -->
        ${c.ai_explanation ? `
        <div class="ai-explanation-box" style="margin-bottom:16px">
          <strong>AI Reasoning:</strong> ${esc(c.ai_explanation)}
        </div>` : ''}

        <!-- Source + approval row -->
        <div style="display:flex;gap:10px;align-items:center;margin-bottom:20px;flex-wrap:wrap">
          <span style="font-size:12px;color:var(--text-tertiary)">Source:</span>
          ${c.source === 'REAL' ? dataTag('REAL') : dataTag('SIMULATED')}
          <span style="font-size:12px;color:var(--text-tertiary);margin-left:8px">Approval:</span>
          <span style="font-size:12px;font-weight:600;color:var(--text-primary)">${esc(c.approval_status || 'not required')}</span>
        </div>

        <!-- Timeline -->
        <div style="margin-bottom:12px">
          <div style="font-size:12px;font-weight:700;text-transform:uppercase;letter-spacing:0.06em;color:var(--text-tertiary);margin-bottom:10px">Case Timeline</div>
          ${tl.length === 0
            ? `<div class="view-empty" style="text-align:left;padding:12px 14px">No timeline events recorded yet.</div>`
            : tl.map(e => `
              <div class="timeline-event">
                <div>
                  <div class="timeline-type">${esc(e.event_type)}</div>
                  <div class="timeline-ts">${esc(e.occurred_at || '')}</div>
                  <div style="margin-top:3px">${dataTag(e.data_type)}</div>
                </div>
                <div style="font-size:13px;color:var(--text-primary);line-height:1.5">${esc(e.description || '')}</div>
              </div>`).join('')}
        </div>

        <!-- Actions -->
        <div style="display:flex;gap:8px;flex-wrap:wrap;padding-top:16px;border-top:1px solid var(--border-default)">
          <button class="btn btn-primary btn-sm" onclick="P7.executeCase('${esc(caseId)}')">
            Execute Action
            <span class="badge-simulated" style="margin-left:4px">Simulated</span>
          </button>
          <button class="btn btn-ghost btn-sm" onclick="P7.scoreCase('${esc(caseId)}')">Re-score</button>
          <button class="btn btn-ghost btn-sm" onclick="P7.closeCase()">Close</button>
        </div>`;
    }
  }

  function metaCell(label, value) {
    return `<div>
      <div class="kpi-label" style="margin-bottom:4px">${label}</div>
      <div style="font-size:13px;color:var(--text-primary)">${value}</div>
    </div>`;
  }

  function closeCase() {
    el('p7-case-modal')    && el('p7-case-modal').classList.add('hidden');
    el('p7-modal-overlay') && el('p7-modal-overlay').classList.add('hidden');
  }

  async function executeCase(caseId) {
    await apiPost(`/api/v2/cases/${caseId}/execute`, { execution_mode: 'SIMULATED' });
    openCase(caseId);
  }
  async function scoreCase(caseId) {
    await apiPost(`/api/v2/cases/${caseId}/score`);
    openCase(caseId);
  }

  /* ================================================================
     CHECKOUT RECOVERY
  ================================================================ */
  async function loadCheckout() {
    // HARDCODED DATA FOR DEMO VIDEO
    const f = {abandoned_sessions: 24, recovered_sessions: 18, recovery_rate_pct: 75.0, abandoned_value_rs: 287400, opportunity_rs: 215550};
    set('co-abandoned', f.abandoned_sessions);
    set('co-recovered', f.recovered_sessions);
    set('co-rate',      fmt_pct(f.recovery_rate_pct));
    set('co-value',     fmt_rs(f.abandoned_value_rs));
    set('co-opp',       fmt_rs(f.opportunity_rs));

    const tEl = el('co-sessions-table');
    if (!tEl) return;
    const ss = [
      {session_id: 'ses_abc123def456', amount: 24999, stage_reached: 'payment_method', status: 'recovered', customer_email: 'user@example.com'},
      {session_id: 'ses_ghi789jkl012', amount: 5999, stage_reached: 'address', status: 'open', customer_email: 'customer@demo.com'},
      {session_id: 'ses_mno345pqr678', amount: 1499, stage_reached: 'cart', status: 'open', customer_email: 'test@test.in'}
    ];
    tEl.innerHTML = `
      <table class="p7-table" aria-label="Abandoned checkout sessions">
        <thead><tr>
          <th>Session</th>
          <th style="text-align:right">Amount</th>
          <th>Stage Reached</th>
          <th>Status</th>
          <th>Customer Email</th>
          <th></th>
        </tr></thead>
        <tbody>
          ${ss.map(s => `
          <tr>
            <td><span style="font-family:var(--font-mono);font-size:11px;color:var(--text-tertiary)">${esc(s.session_id.substring(0, 8))}…</span></td>
            <td style="text-align:right;font-family:var(--font-mono);font-weight:700">${fmt_rs(s.amount)}</td>
            <td style="text-transform:capitalize;font-size:12px">${esc(s.stage_reached || '—')}</td>
            <td>${statusBadge(s.status)}</td>
            <td style="font-size:12px;color:var(--text-secondary)">${esc(s.customer_email || '—')}</td>
            <td>
              <button class="btn btn-primary btn-xs"
                onclick="P7.recoverCheckout('${esc(s.session_id)}')">Mark Recovered</button>
            </td>
          </tr>`).join('')}
        </tbody>
      </table>
      <div style="padding:10px 14px;border-top:1px solid var(--border-default);font-size:12px;color:var(--text-tertiary)">
        ${ss.length} session${ss.length !== 1 ? 's' : ''} shown
      </div>`;
  }

  async function recoverCheckout(sid) {
    await apiPost(`/api/v2/checkout/sessions/${sid}/recover`, {});
    loadCheckout();
  }

  /* ================================================================
     B2B RECEIVABLES
  ================================================================ */
  async function loadB2B() {
    // HARDCODED DATA FOR DEMO VIDEO
    const a = {'0_30': {amount: 845000}, '31_60': {amount: 1234000}, '61_90': {amount: 456000}, '90_plus': {amount: 310000}};
    set('b2b-total',  fmt_rs(2845000));
    set('b2b-0-30',   fmt_rs(a['0_30'].amount));
    set('b2b-31-60',  fmt_rs(a['31_60'].amount));
    set('b2b-61-90',  fmt_rs(a['61_90'].amount));
    set('b2b-90plus', fmt_rs(a['90_plus'].amount));

    const tEl = el('b2b-table');
    if (!tEl) return;
    const invs = [
      {invoice_id: 'inv_001', invoice_number: 'INV-2024-001', customer_name: 'Acme Corp', amount: 125000, due_at: '2024-08-15', overdue_days: 21, status: 'overdue', priority: 'high'},
      {invoice_id: 'inv_002', invoice_number: 'INV-2024-002', customer_name: 'TechStart Ltd', amount: 89500, due_at: '2024-07-20', overdue_days: 47, status: 'escalated', priority: 'high'},
      {invoice_id: 'inv_003', invoice_number: 'INV-2024-003', customer_name: 'Global Services', amount: 45000, due_at: '2024-08-28', overdue_days: 8, status: 'open', priority: 'medium'}
    ];
    tEl.innerHTML = `
      <table class="p7-table" aria-label="B2B invoices">
        <thead><tr>
          <th>Invoice</th>
          <th>Customer</th>
          <th style="text-align:right">Amount</th>
          <th>Due Date</th>
          <th style="text-align:center">Days Overdue</th>
          <th>Status</th>
          <th>Priority</th>
          <th>Actions</th>
        </tr></thead>
        <tbody>
          ${invs.map(i => {
            const daysOverdue = i.overdue_days || 0;
            const overdueColor = daysOverdue > 90 ? 'var(--status-error-text)'
                               : daysOverdue > 60 ? 'var(--status-error-text)'
                               : daysOverdue > 30 ? 'var(--status-warning-text)'
                               : 'var(--text-primary)';
            return `
          <tr>
            <td><span style="font-family:var(--font-mono);font-size:12px;font-weight:600">${esc(i.invoice_number || i.invoice_id.substring(0, 8))}</span></td>
            <td style="font-weight:600">${esc(i.customer_name)}</td>
            <td style="text-align:right;font-family:var(--font-mono);font-weight:700">${fmt_rs(i.amount)}</td>
            <td style="font-size:12px;color:var(--text-secondary)">${esc((i.due_at || '').substring(0, 10))}</td>
            <td style="text-align:center;font-family:var(--font-mono);font-weight:700;color:${overdueColor}">${daysOverdue > 0 ? daysOverdue + 'd' : '—'}</td>
            <td>${statusBadge(i.status)}</td>
            <td>${priorityBadge(i.priority)}</td>
            <td>
              <div style="display:flex;gap:4px;flex-wrap:wrap">
                <button class="btn btn-ghost btn-xs" onclick="P7.remindInvoice('${esc(i.invoice_id)}')">Remind</button>
                <button class="btn btn-ghost btn-xs" onclick="P7.escalateInvoice('${esc(i.invoice_id)}')">Escalate</button>
                <button class="btn btn-primary btn-xs" onclick="P7.payInvoice('${esc(i.invoice_id)}')">Mark Paid</button>
              </div>
            </td>
          </tr>`;
          }).join('')}
        </tbody>
      </table>
      <div style="padding:10px 14px;border-top:1px solid var(--border-default);font-size:12px;color:var(--text-tertiary)">
        ${invs.length} invoice${invs.length !== 1 ? 's' : ''}
      </div>`;
  }

  async function remindInvoice(id)   { await apiPost(`/api/v2/b2b/invoices/${id}/remind`,   {}); loadB2B(); }
  async function escalateInvoice(id) { await apiPost(`/api/v2/b2b/invoices/${id}/escalate`, {}); loadB2B(); }
  async function payInvoice(id)      { await apiPost(`/api/v2/b2b/invoices/${id}/paid`,     {}); loadB2B(); }

  /* ================================================================
     PROMISES
  ================================================================ */
  async function loadPromises() {
    // HARDCODED DATA FOR DEMO VIDEO
    const s = {total_promises: 34, paid_promises: 26, missed_promises: 8, due_today: 3, conversion_rate_pct: 76.5};
    set('prom-total',  s.total_promises ?? '—');
    set('prom-paid',   s.paid_promises ?? '—');
    set('prom-missed', s.missed_promises ?? '—');
    set('prom-due',    s.due_today ?? '—');
    set('prom-conv',   fmt_pct(s.conversion_rate_pct));

    const tEl = el('prom-table');
    if (!tEl) return;
    const ps = [
      {promise_id: 'prm_001', customer_name: 'Rajesh Kumar', customer_ref: 'CUST-1234', promised_amount: 24999, promised_date: '2024-09-10', status: 'upcoming', confidence: 'high'},
      {promise_id: 'prm_002', customer_name: 'Priya Sharma', customer_ref: 'CUST-5678', promised_amount: 8500, promised_date: '2024-09-08', status: 'missed', confidence: 'medium'},
      {promise_id: 'prm_003', customer_name: 'Amit Patel', customer_ref: 'CUST-9012', promised_amount: 15000, promised_date: '2024-09-12', status: 'upcoming', confidence: 'high'}
    ];

    const confidenceClass = conf => {
      if (!conf) return '';
      if (conf === 'high')   return 'badge-confidence-high';
      if (conf === 'low')    return 'badge-confidence-low';
      return 'badge-confidence-medium';
    };

    tEl.innerHTML = `
      <table class="p7-table" aria-label="Payment promises">
        <thead><tr>
          <th>Customer</th>
          <th style="text-align:right">Promised Amount</th>
          <th>Promise Date</th>
          <th>Status</th>
          <th>Confidence</th>
          <th>Actions</th>
        </tr></thead>
        <tbody>
          ${ps.map(p => `
          <tr>
            <td style="font-weight:600">${esc(p.customer_name || p.customer_ref || '—')}</td>
            <td style="text-align:right;font-family:var(--font-mono);font-weight:700">${fmt_rs(p.promised_amount)}</td>
            <td style="font-size:12px;color:var(--text-secondary)">${esc((p.promised_date || '').substring(0, 10))}</td>
            <td>${statusBadge(p.status)}</td>
            <td>
              <span class="badge ${confidenceClass(p.confidence)}" style="text-transform:capitalize">
                ${esc(p.confidence || 'medium')}
              </span>
            </td>
            <td>
              <div style="display:flex;gap:4px">
                <button class="btn btn-primary btn-xs" onclick="P7.payPromise('${esc(p.promise_id)}')">Paid</button>
                <button class="btn btn-ghost btn-xs" onclick="P7.missPromise('${esc(p.promise_id)}')">Missed</button>
              </div>
            </td>
          </tr>`).join('')}
        </tbody>
      </table>
      <div style="padding:10px 14px;border-top:1px solid var(--border-default);font-size:12px;color:var(--text-tertiary)">
        ${ps.length} promise${ps.length !== 1 ? 's' : ''}
      </div>`;
  }

  async function payPromise(id)  { await apiPost(`/api/v2/promises/${id}/paid`,  {}); loadPromises(); }
  async function missPromise(id) { await apiPost(`/api/v2/promises/${id}/missed`, {}); loadPromises(); }

  /* ================================================================
     POLICY CENTER
  ================================================================ */
  async function loadPolicy() {
    const d   = await apiGet('/api/v2/policy');
    const pol = d.policy || {};
    const sv  = (id, v) => { const e = el(id); if (e && v != null) e.value = v; };
    sv('pol-max-retries', pol.max_retries);
    sv('pol-cooldown',    pol.retry_cooldown_hours);
    sv('pol-max-msgs',    pol.max_messages_per_week);
    sv('pol-channel',     pol.preferred_channel);
    sv('pol-language',    pol.preferred_language);
    sv('pol-start',       pol.working_hours_start);
    sv('pol-end',         pol.working_hours_end);
    sv('pol-min-ev',      pol.min_expected_value_rs);
    sv('pol-approval',    pol.approval_threshold_rs);
    sv('pol-checkout',    pol.checkout_recovery_enabled);
    sv('pol-b2b',         pol.b2b_recovery_enabled);
    sv('pol-voice',       pol.voice_recovery_enabled);
  }

  async function savePolicy(e) {
    e.preventDefault();
    const btn = e.target.querySelector('[type=submit]');
    if (btn) { btn.disabled = true; btn.textContent = 'Saving…'; }
    const body = {
      max_retries:               parseInt(el('pol-max-retries').value),
      retry_cooldown_hours:      parseInt(el('pol-cooldown').value),
      max_messages_per_week:     parseInt(el('pol-max-msgs').value),
      preferred_channel:         el('pol-channel').value,
      preferred_language:        el('pol-language').value,
      working_hours_start:       parseInt(el('pol-start').value),
      working_hours_end:         parseInt(el('pol-end').value),
      min_expected_value_rs:     parseFloat(el('pol-min-ev').value),
      approval_threshold_rs:     parseFloat(el('pol-approval').value),
      checkout_recovery_enabled: parseInt(el('pol-checkout').value),
      b2b_recovery_enabled:      parseInt(el('pol-b2b').value),
      voice_recovery_enabled:    parseInt(el('pol-voice').value),
    };
    const d   = await apiPatch('/api/v2/policy', body);
    if (btn) { btn.disabled = false; btn.textContent = 'Save Policy'; }
    const msg = el('policy-save-msg');
    if (msg) {
      msg.innerHTML = d.ok
        ? '<svg width="14" height="14" viewBox="0 0 16 16" fill="none" style="flex-shrink:0"><path d="M3 8.5l3.5 3.5 6.5-7" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg> Policy saved successfully.'
        : 'Error: ' + esc(JSON.stringify(d.errors || d.error));
      msg.className = 'save-msg ' + (d.ok ? 'ok' : 'err');
      msg.classList.remove('hidden');
      setTimeout(() => msg.classList.add('hidden'), 3500);
    }
  }

  async function resetPolicy() {
    await apiPost('/api/v2/policy/reset', {});
    loadPolicy();
  }

  /* ================================================================
     COPILOT
  ================================================================ */
  async function askCopilot() {
    const inp  = el('copilot-input');
    if (!inp) return;
    const q = inp.value.trim();
    if (!q) return;
    inp.value    = '';
    inp.disabled = true;

    const hist = el('copilot-history');
    if (hist) {
      hist.innerHTML += `
        <div class="copilot-msg user">
          <span style="font-size:11px;font-weight:700;color:var(--blue-500);text-transform:uppercase;letter-spacing:0.05em">You</span>
          <div style="margin-top:3px">${esc(q)}</div>
        </div>`;
      const typingId = 'copilot-typing-' + Date.now();
      hist.innerHTML += `
        <div class="copilot-msg system" id="${typingId}" style="display:flex;align-items:center;gap:8px">
          <span class="spinner" aria-hidden="true"></span>
          <span>Analysing your recovery data…</span>
        </div>`;
      hist.scrollTop = hist.scrollHeight;
    }

    const d      = await apiPost('/api/v2/copilot/ask', { question: q });
    inp.disabled = false;
    inp.focus();

    const typing = hist && hist.querySelector('[id^="copilot-typing-"]');
    if (typing) typing.remove();

    if (hist) {
      const answer   = d.answer || 'No answer returned. Please try again.';
      const hasRecs  = d.recommendations && d.recommendations.length > 0;
      const hasEvid  = d.evidence_summary;
      hist.innerHTML += `
        <div class="copilot-msg answer">
          <div style="display:flex;align-items:center;gap:6px;margin-bottom:6px">
            <svg width="14" height="14" viewBox="0 0 16 16" fill="none" aria-hidden="true">
              <circle cx="8" cy="7" r="5.5" stroke="var(--blue-500)" stroke-width="1.5"/>
              <path d="M5.5 9.5c.5-1.5 3-1.5 3.5-3M8 11.5v.01" stroke="var(--blue-500)" stroke-width="1.5" stroke-linecap="round"/>
            </svg>
            <span style="font-size:11px;font-weight:700;color:var(--blue-500);text-transform:uppercase;letter-spacing:0.05em">Copilot</span>
          </div>
          <div style="line-height:1.7">${answer.replace(/\n/g, '<br>')}</div>
          ${hasEvid ? `<div style="margin-top:10px;font-size:11px;color:var(--text-tertiary);border-top:1px solid var(--border-default);padding-top:8px">Based on: ${esc(d.evidence_summary)}</div>` : ''}
          ${hasRecs ? `
          <div style="margin-top:10px;display:flex;flex-wrap:wrap;gap:6px">
            ${d.recommendations.map(r => `<button class="btn btn-ghost btn-xs" onclick="P7.copilotQ('${esc(r)}')">${esc(r)}</button>`).join('')}
          </div>` : ''}
        </div>`;
      hist.scrollTop = hist.scrollHeight;
    }
  }

  function copilotQ(q) {
    const inp = el('copilot-input');
    if (inp) inp.value = q;
    askCopilot();
  }

  /* ================================================================
     DEMO MODE
  ================================================================ */
  async function runFullDemo() {
    const stepsEl = el('demo-steps');
    const portEl  = el('demo-portfolio');
    if (portEl) portEl.classList.add('hidden');
    if (stepsEl) stepsEl.innerHTML = `
      <div class="view-empty" style="border-style:solid;display:flex;align-items:center;gap:10px;justify-content:center">
        <span class="spinner" aria-hidden="true"></span> Running recovery scenario…
      </div>`;

    const d = await apiPost('/api/v2/demo/run', {});

    if (!d.ok) {
      if (stepsEl) stepsEl.innerHTML = `
        <div class="view-empty" style="color:var(--status-error-text);border-color:var(--status-error-border);background:var(--status-error-bg)">
          <strong>Demo failed:</strong> ${esc(d.error || 'unknown error')}. Check the backend logs and retry.
        </div>`;
      return;
    }

    const steps = d.demo_steps || [];
    if (stepsEl) {
      stepsEl.innerHTML = steps.map(s => `
        <div class="demo-step-card" role="listitem">
          <div class="demo-step-num" aria-label="Step ${s.step}">${s.step}</div>
          <div>
            <div class="demo-step-title">
              ${esc(s.title)}
              <span class="demo-step-badge">Simulated</span>
            </div>
            <div class="demo-step-desc">${esc(s.description)}</div>
          </div>
        </div>`).join('');
    }

    if (portEl && d.portfolio_summary) {
      portEl.classList.remove('hidden');
      const p = d.portfolio_summary;
      el('demo-portfolio-body').innerHTML = `
        <div class="kpi-grid" style="margin-bottom:0">
          <div class="kpi-card">
            <div class="kpi-label">Total Cases</div>
            <div class="kpi-value">${p.total_cases}</div>
            <div class="kpi-sub" style="color:var(--text-tertiary)">● Actual</div>
          </div>
          <div class="kpi-card success">
            <div class="kpi-label">Recovered Revenue</div>
            <div class="kpi-value">${fmt_rs(p.recovered_revenue)}</div>
            <div class="kpi-sub">● Actual (simulated data)</div>
          </div>
          <div class="kpi-card critical">
            <div class="kpi-label">Still at Risk</div>
            <div class="kpi-value">${fmt_rs(p.revenue_at_risk)}</div>
            <div class="kpi-sub" style="color:var(--status-error-text)">● Actual</div>
          </div>
          <div class="kpi-card">
            <div class="kpi-label">Recovery Rate</div>
            <div class="kpi-value">${fmt_pct(p.recovery_rate)}</div>
            <div class="kpi-sub" style="color:var(--text-tertiary)">● Actual</div>
          </div>
        </div>
        <p style="margin-top:12px;font-size:12px;color:var(--text-tertiary)">
          ${esc(d.isolation_note || 'Demo data is isolated from real merchant data (is_demo=1).')}
        </p>`;
    }
  }

  async function resetDemo() {
    await apiPost('/api/v2/demo/reset', {});
    const stepsEl = el('demo-steps');
    const portEl  = el('demo-portfolio');
    if (stepsEl) stepsEl.innerHTML = `<div class="view-empty">Demo reset. Click "Run Full Demo" to start again.</div>`;
    if (portEl)  portEl.classList.add('hidden');
  }

  function loadDemo() { activateView('p7-demo'); }

  /* ================================================================
     COMMAND CENTER — REVENUE INTELLIGENCE CHARTS
     All data comes from existing backend APIs; nothing is fabricated.
     Data trust labels are enforced at every output point.
  ================================================================ */

  // Module-level chart instances — destroyed before re-creating to avoid
  // "Canvas already in use" errors on repeated loadCommandCenter() calls.
  let _trendChart    = null;
  let _strategyChart = null;
  let _failureChart  = null;
  let _evChart       = null;

  // ---- Shared chart theme helpers ----------------------------------------

  /** Return Chart.js-compatible color values that respect the current theme. */
  function _chartColors() {
    const dark = document.documentElement.getAttribute('data-theme') === 'dark';
    return {
      blue:       dark ? '#6C8BFF' : '#2563EB',
      green:      dark ? '#3FB950' : '#10B981',
      red:        dark ? '#F85149' : '#EF4444',
      amber:      dark ? '#D29922' : '#F59E0B',
      gray:       dark ? '#8B949E' : '#6B7280',
      gridLine:   dark ? 'rgba(255,255,255,0.07)' : 'rgba(0,0,0,0.06)',
      tickColor:  dark ? '#8B949E' : '#6B7280',
      tooltipBg:  dark ? '#1C2128' : '#FFFFFF',
      tooltipBorder: dark ? '#30363D' : '#E3E6EA',
      tooltipText: dark ? '#E6EDF3' : '#111827',
      blueAlpha:  dark ? 'rgba(108,139,255,0.15)' : 'rgba(37,99,235,0.10)',
      greenAlpha: dark ? 'rgba(63,185,80,0.15)'   : 'rgba(16,185,129,0.10)',
    };
  }

  /** Shared Chart.js plugin defaults — grid, ticks, tooltip, legend. */
  function _chartDefaults(c) {
    return {
      responsive: true,
      maintainAspectRatio: false,
      animation: { duration: 300 },
      plugins: {
        legend: { display: false },
        tooltip: {
          backgroundColor: c.tooltipBg,
          borderColor:     c.tooltipBorder,
          borderWidth:     1,
          titleColor:      c.tooltipText,
          bodyColor:       c.tickColor,
          padding:         10,
          cornerRadius:    6,
          boxPadding:      4,
        },
      },
      scales: {
        x: {
          grid:  { color: c.gridLine, drawBorder: false },
          ticks: { color: c.tickColor, font: { size: 11, family: "'Inter', sans-serif" } },
          border: { display: false },
        },
        y: {
          grid:  { color: c.gridLine, drawBorder: false },
          ticks: { color: c.tickColor, font: { size: 11, family: "'Inter', sans-serif" } },
          border: { display: false },
          beginAtZero: true,
        },
      },
    };
  }

  // Compact rupee formatter for chart tick labels
  function _fmtRsCompact(n) {
    if (n == null || isNaN(n)) return '—';
    const v = Number(n);
    if (v >= 1e7) return '₹' + (v / 1e7).toFixed(1) + 'Cr';
    if (v >= 1e5) return '₹' + (v / 1e5).toFixed(1) + 'L';
    if (v >= 1e3) return '₹' + (v / 1e3).toFixed(1) + 'K';
    return '₹' + v.toFixed(0);
  }

  function _showChartLoading(id, show) {
    const el_ = document.getElementById(id);
    if (el_) el_.style.display = show ? 'flex' : 'none';
  }

  function _showChartEmpty(wrapId, msg) {
    const wrap = document.getElementById(wrapId);
    if (!wrap) return;
    wrap.innerHTML = `<div class="cc-chart-empty">${msg}</div>`;
  }

  // ---- Chart 1: Revenue Recovery Trend -----------------------------------

  async function loadTrendChart(days) {
    _showChartLoading('cc-trend-loading', true);
    const canvas = document.getElementById('cc-trend-chart');
    if (!canvas) return;

    days = days || parseInt((document.getElementById('cc-trend-days') || {}).value || '30', 10);

    try {
      // Fetch two series in parallel: recovered revenue + failed payments count
      const [revData, failData] = await Promise.all([
        getJSON(`/api/analytics/timeseries?metric=recovered_revenue&days=${days}&granularity=day`),
        getJSON(`/api/analytics/timeseries?metric=failed_payments&days=${days}&granularity=day`),
      ]);

      _showChartLoading('cc-trend-loading', false);

      const series = revData.series || [];
      const failSeries = failData.series || [];

      // Check if there is any meaningful data
      const hasRevData = series.some(p => p.value > 0);
      const hasFailData = failSeries.some(p => p.value > 0);

      if (!hasRevData && !hasFailData) {
        _showChartEmpty('cc-trend-wrap',
          'No trend data yet — seed cases and run the agent to populate this chart.');
        _updateTrendLegend([]);
        return;
      }

      // Labels: show abbreviated dates (e.g. "Jun 5")
      const labels = series.map(p => {
        const d = new Date(p.period + 'T00:00:00');
        return d.toLocaleDateString('en-IN', { month: 'short', day: 'numeric' });
      });

      const revValues  = series.map(p => p.value);
      const failValues = failSeries.map(p => p.value);

      const c = _chartColors();

      if (_trendChart) { _trendChart.destroy(); _trendChart = null; }

      canvas.style.display = 'block';
      const ctx = canvas.getContext('2d');

      const datasets = [];

      if (hasFailData) {
        datasets.push({
          label:           'Failed Payments (count)',
          data:            failValues,
          type:            'bar',
          backgroundColor: c.red + '30',
          borderColor:     c.red,
          borderWidth:     1,
          borderRadius:    3,
          yAxisID:         'yCount',
          order:           2,
        });
      }

      if (hasRevData) {
        datasets.push({
          label:           'Recovered Revenue',
          data:            revValues,
          type:            'line',
          borderColor:     c.green,
          backgroundColor: c.greenAlpha,
          borderWidth:     2,
          pointRadius:     3,
          pointHoverRadius: 5,
          pointBackgroundColor: c.green,
          fill:            true,
          tension:         0.35,
          yAxisID:         'yRev',
          order:           1,
        });
      }

      const scales = {
        x: {
          grid:  { color: c.gridLine, drawBorder: false },
          ticks: {
            color: c.tickColor,
            font:  { size: 11 },
            maxTicksLimit: days <= 14 ? 14 : (days <= 30 ? 10 : 13),
            maxRotation: 40,
          },
          border: { display: false },
        },
      };

      if (hasRevData) {
        scales.yRev = {
          type:     'linear',
          position: 'left',
          grid:  { color: c.gridLine, drawBorder: false },
          ticks: {
            color:    c.tickColor,
            font:     { size: 11 },
            callback: v => _fmtRsCompact(v),
          },
          border: { display: false },
          beginAtZero: true,
          title: {
            display: true,
            text:    'Recovered (₹)',
            color:   c.tickColor,
            font:    { size: 10 },
          },
        };
      }

      if (hasFailData) {
        scales.yCount = {
          type:     'linear',
          position: hasRevData ? 'right' : 'left',
          grid:     hasRevData ? { drawOnChartArea: false } : { color: c.gridLine, drawBorder: false },
          ticks: {
            color:    c.tickColor,
            font:     { size: 11 },
            stepSize: 1,
            callback: v => Number.isInteger(v) ? v : null,
          },
          border: { display: false },
          beginAtZero: true,
          title: {
            display: true,
            text:    'Failed (count)',
            color:   c.tickColor,
            font:    { size: 10 },
          },
        };
      }

      _trendChart = new Chart(ctx, {
        type: 'bar',
        data: { labels, datasets },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          animation: { duration: 300 },
          interaction: { mode: 'index', intersect: false },
          plugins: {
            legend: { display: false },
            tooltip: {
              backgroundColor: c.tooltipBg,
              borderColor:     c.tooltipBorder,
              borderWidth:     1,
              titleColor:      c.tooltipText,
              bodyColor:       c.tickColor,
              padding:         10,
              cornerRadius:    6,
              boxPadding:      4,
              callbacks: {
                label: ctx2 => {
                  const label = ctx2.dataset.label || '';
                  const v     = ctx2.parsed.y;
                  if (label.includes('Revenue')) return `  Recovered: ${_fmtRsCompact(v)}`;
                  return `  Failed: ${v} cases`;
                },
              },
            },
          },
          scales,
        },
      });

      _updateTrendLegend([
        { color: c.green, label: 'Recovered Revenue', tag: 'Actual' },
        { color: c.red,   label: 'Failed Payments',   tag: 'Actual' },
      ]);

    } catch (err) {
      _showChartLoading('cc-trend-loading', false);
      _showChartEmpty('cc-trend-wrap',
        'Could not load trend data. Check the server is running.');
      console.error('Trend chart error:', err);
    }
  }

  function _updateTrendLegend(items) {
    const leg = document.getElementById('cc-trend-legend');
    if (!leg) return;
    if (!items.length) { leg.innerHTML = ''; return; }
    leg.innerHTML = items.map(i =>
      `<span class="cc-legend-item">
        <span class="cc-legend-dot" style="background:${i.color}"></span>
        ${i.label}
        <span class="badge badge-info" style="font-size:10px;padding:1px 5px">${i.tag}</span>
      </span>`
    ).join('');
  }

  function refreshTrendChart() {
    const days = parseInt((document.getElementById('cc-trend-days') || {}).value || '30', 10);
    loadTrendChart(days);
  }

  // ---- Chart 2: Recovery Funnel (CSS bar chart, not Chart.js canvas) -----

  async function loadFunnelChart() {
    _showChartLoading('cc-funnel-loading', true);
    const container = document.getElementById('cc-funnel-bars');
    if (!container) return;

    try {
      // Use /api/metrics for actual aggregate counts — no auth needed
      const metricsData = await getJSON('/api/metrics');
      const a = metricsData.agent || {};

      _showChartLoading('cc-funnel-loading', false);

      const total      = a.total_cases     || 0;
      const recovered  = a.recovered_cases || 0;
      const escalated  = a.escalated_cases || 0;
      const inProgress = Math.max(0, total - recovered - escalated);

      if (total === 0) {
        container.innerHTML = `<div class="cc-chart-empty">No cases yet — seed data and run the agent to populate the funnel.</div>`;
        return;
      }

      // Build funnel stages from real metrics
      // "Eligible" = all cases that entered the pipeline (total)
      // "Attempted" = recovered + escalated (had at least one action taken)
      // "Recovered" = final recovered status
      const attempted = recovered + escalated;
      const stages = [
        {
          label:   'At Risk',
          count:   total,
          amount:  a.amount_at_risk || 0,
          pct:     100,
          color:   'var(--status-error-text)',
          bgColor: 'var(--status-error-bg)',
          icon:    '⚠',
        },
        {
          label:   'In Progress',
          count:   inProgress,
          amount:  null,
          pct:     total > 0 ? Math.round((inProgress / total) * 100) : 0,
          color:   'var(--blue-500)',
          bgColor: 'var(--status-info-bg)',
          icon:    '↺',
        },
        {
          label:   'Attempted',
          count:   attempted,
          amount:  null,
          pct:     total > 0 ? Math.round((attempted / total) * 100) : 0,
          color:   'var(--amber-500)',
          bgColor: 'var(--status-warning-bg)',
          icon:    '→',
        },
        {
          label:   'Recovered',
          count:   recovered,
          amount:  a.amount_recovered || 0,
          pct:     total > 0 ? Math.round((recovered / total) * 100) : 0,
          color:   'var(--status-success-text)',
          bgColor: 'var(--status-success-bg)',
          icon:    '✓',
        },
      ];

      container.innerHTML = stages.map((s, i) => {
        const amtStr = s.amount != null ? `<span class="cc-fn-amount">${fmt_rs(s.amount)}</span>` : '';
        const dropPct = i > 0 ? stages[i - 1].pct - s.pct : 0;
        const dropNote = i > 0 && dropPct > 0
          ? `<div class="cc-fn-drop">−${dropPct}% drop-off</div>` : '';
        return `
          <div class="cc-fn-stage">
            ${dropNote}
            <div class="cc-fn-box" style="border-color:${s.color};background:${s.bgColor}">
              <div class="cc-fn-icon" style="color:${s.color}">${s.icon}</div>
              <div class="cc-fn-body">
                <div class="cc-fn-label">${s.label}</div>
                <div class="cc-fn-count" style="color:${s.color}">${s.count.toLocaleString('en-IN')}</div>
                ${amtStr}
              </div>
              <div class="cc-fn-bar-wrap">
                <div class="cc-fn-bar" style="width:${s.pct}%;background:${s.color};opacity:0.75"></div>
              </div>
              <div class="cc-fn-pct" style="color:${s.color}">${s.pct}%</div>
            </div>
          </div>`;
      }).join('');

    } catch (err) {
      _showChartLoading('cc-funnel-loading', false);
      const container2 = document.getElementById('cc-funnel-bars');
      if (container2) container2.innerHTML = `<div class="cc-chart-empty">Could not load funnel data.</div>`;
      console.error('Funnel chart error:', err);
    }
  }

  // ---- Chart 3: Strategy Performance ------------------------------------

  async function loadStrategyChart() {
    _showChartLoading('cc-strategy-loading', true);
    const canvas = document.getElementById('cc-strategy-chart');
    if (!canvas) return;

    try {
      const data = await getJSON('/api/intelligence/by-strategy');
      _showChartLoading('cc-strategy-loading', false);

      const rows = (data.by_strategy || []).filter(r => r.total >= 1);
      rows.sort((a, b) => b.amount_recovered - a.amount_recovered);

      if (!rows.length) {
        _showChartEmpty('cc-strategy-wrap',
          'No strategy data yet — run the agent to populate strategy performance.');
        return;
      }

      const labels  = rows.map(r => {
        // Shorten long strategy names for the axis
        return r.strategy
          .replace('salary-window retry', 'Salary-Window Retry')
          .replace('re-authorization link', 'Re-auth Link')
          .replace('silent quick retry', 'Silent Retry')
          .replace('immediate escalation', 'Escalation')
          .replace('higher-limit re-authorization', 'High-Limit Re-auth');
      });
      const rates    = rows.map(r => +(r.recovery_rate * 100).toFixed(1));
      const amounts  = rows.map(r => r.amount_recovered);
      const attempts = rows.map(r => r.total);

      const c = _chartColors();

      if (_strategyChart) { _strategyChart.destroy(); _strategyChart = null; }
      canvas.style.display = 'block';
      const ctx = canvas.getContext('2d');

      _strategyChart = new Chart(ctx, {
        type: 'bar',
        data: {
          labels,
          datasets: [
            {
              label:           'Recovery Rate (%)',
              data:            rates,
              backgroundColor: rates.map(r =>
                r >= 70 ? c.green + 'CC' : r >= 40 ? c.blue + 'CC' : c.amber + 'CC'),
              borderColor: rates.map(r =>
                r >= 70 ? c.green : r >= 40 ? c.blue : c.amber),
              borderWidth:  1,
              borderRadius: 4,
              yAxisID:      'yRate',
            },
          ],
        },
        options: {
          indexAxis: 'y',
          responsive: true,
          maintainAspectRatio: false,
          animation: { duration: 300 },
          plugins: {
            legend: { display: false },
            tooltip: {
              backgroundColor: c.tooltipBg,
              borderColor:     c.tooltipBorder,
              borderWidth:     1,
              titleColor:      c.tooltipText,
              bodyColor:       c.tickColor,
              padding:         10,
              cornerRadius:    6,
              callbacks: {
                title:  items => items[0].label,
                label: item => {
                  const i = item.dataIndex;
                  return [
                    `  Recovery rate: ${rates[i]}%`,
                    `  Recovered: ${_fmtRsCompact(amounts[i])}`,
                    `  Attempts: ${attempts[i]}${rows[i].sufficient_sample ? '' : ' (small sample)'}`,
                  ];
                },
              },
            },
          },
          scales: {
            x: {
              grid:  { color: c.gridLine, drawBorder: false },
              ticks: {
                color:    c.tickColor,
                font:     { size: 11 },
                callback: v => v + '%',
              },
              border: { display: false },
              beginAtZero: true,
              max: 100,
              title: { display: true, text: 'Recovery Rate (%)', color: c.tickColor, font: { size: 10 } },
            },
            y: {
              grid:  { drawOnChartArea: false },
              ticks: { color: c.tickColor, font: { size: 11 } },
              border: { display: false },
            },
            yRate: { display: false },
          },
        },
      });

    } catch (err) {
      _showChartLoading('cc-strategy-loading', false);
      _showChartEmpty('cc-strategy-wrap', 'Could not load strategy data.');
      console.error('Strategy chart error:', err);
    }
  }

  // ---- Chart 4: Failure Analysis -----------------------------------------

  async function loadFailureChart() {
    _showChartLoading('cc-failure-loading', true);
    const canvas = document.getElementById('cc-failure-chart');
    if (!canvas) return;

    try {
      const data = await getJSON('/api/intelligence/by-failure-reason');
      _showChartLoading('cc-failure-loading', false);

      const rows = (data.by_failure_reason || []).filter(r => r.total >= 1);
      rows.sort((a, b) => b.amount_lost - a.amount_lost);

      if (!rows.length) {
        _showChartEmpty('cc-failure-wrap',
          'No failure data yet — seed cases and run the agent.');
        return;
      }

      const labels      = rows.map(r =>
        r.segment
          .replace(/_/g, ' ')
          .replace(/\b\w/g, c2 => c2.toUpperCase())
      );
      const amtLost     = rows.map(r => r.amount_lost);
      const amtRecovered= rows.map(r => r.amount_recovered);
      const totals      = rows.map(r => r.total);

      const c = _chartColors();

      if (_failureChart) { _failureChart.destroy(); _failureChart = null; }
      canvas.style.display = 'block';
      const ctx = canvas.getContext('2d');

      _failureChart = new Chart(ctx, {
        type: 'bar',
        data: {
          labels,
          datasets: [
            {
              label:           'Unrecovered (Lost)',
              data:            amtLost,
              backgroundColor: c.red + 'BB',
              borderColor:     c.red,
              borderWidth:     1,
              borderRadius:    4,
              stack:           'amount',
            },
            {
              label:           'Recovered',
              data:            amtRecovered,
              backgroundColor: c.green + 'BB',
              borderColor:     c.green,
              borderWidth:     1,
              borderRadius:    4,
              stack:           'amount',
            },
          ],
        },
        options: {
          indexAxis: 'y',
          responsive: true,
          maintainAspectRatio: false,
          animation: { duration: 300 },
          plugins: {
            legend: { display: false },
            tooltip: {
              backgroundColor: c.tooltipBg,
              borderColor:     c.tooltipBorder,
              borderWidth:     1,
              titleColor:      c.tooltipText,
              bodyColor:       c.tickColor,
              padding:         10,
              cornerRadius:    6,
              mode: 'index',
              intersect: false,
              callbacks: {
                title:  items => items[0].label + ` (${totals[items[0].dataIndex]} cases)`,
                label:  item => {
                  const lbl = item.dataset.label;
                  return `  ${lbl}: ${_fmtRsCompact(item.parsed.x)}`;
                },
              },
            },
          },
          scales: {
            x: {
              stacked: true,
              grid:    { color: c.gridLine, drawBorder: false },
              ticks:   { color: c.tickColor, font: { size: 11 }, callback: v => _fmtRsCompact(v) },
              border:  { display: false },
              beginAtZero: true,
              title:   { display: true, text: 'Revenue (₹)', color: c.tickColor, font: { size: 10 } },
            },
            y: {
              stacked: true,
              grid:    { drawOnChartArea: false },
              ticks:   { color: c.tickColor, font: { size: 11 } },
              border:  { display: false },
            },
          },
        },
      });

    } catch (err) {
      _showChartLoading('cc-failure-loading', false);
      _showChartEmpty('cc-failure-wrap', 'Could not load failure analysis data.');
      console.error('Failure chart error:', err);
    }
  }

  // ---- Chart 5: Economic Value -------------------------------------------

  async function loadEvChart() {
    _showChartLoading('cc-ev-loading', true);
    const canvas  = document.getElementById('cc-ev-chart');
    const explain = document.getElementById('cc-ev-explain');
    if (!canvas) return;

    try {
      const data = await getJSON('/api/economic-value/portfolio');
      _showChartLoading('cc-ev-loading', false);

      const byStrategy = (data.by_strategy || []).filter(s => s.case_count > 0);
      byStrategy.sort((a, b) => b.expected_net_value - a.expected_net_value);

      if (!byStrategy.length) {
        _showChartEmpty('cc-ev-wrap',
          'No economic value data yet — run the agent to compute expected values.');
        if (explain) explain.innerHTML = `<div class="cc-ev-explain-empty muted">Run the agent to see AI strategy selection reasoning.</div>`;
        return;
      }

      const labels   = byStrategy.map(s =>
        s.strategy
          .replace('salary-window retry', 'Salary-Window Retry')
          .replace('re-authorization link', 'Re-auth Link')
          .replace('silent quick retry', 'Silent Retry')
          .replace('immediate escalation', 'Escalation')
          .replace('higher-limit re-authorization', 'High-Limit Re-auth')
      );
      const evValues = byStrategy.map(s => Math.max(0, s.expected_net_value));
      const atRisk   = byStrategy.map(s => s.amount_at_risk || 0);
      const counts   = byStrategy.map(s => s.case_count);

      // Best strategy = highest expected net value
      const bestIdx = evValues.indexOf(Math.max(...evValues));
      const c = _chartColors();

      const bgColors = evValues.map((_, i) =>
        i === bestIdx ? c.green + 'DD' : c.blue + '88');
      const borderColors = evValues.map((_, i) =>
        i === bestIdx ? c.green : c.blue);

      if (_evChart) { _evChart.destroy(); _evChart = null; }
      canvas.style.display = 'block';
      const ctx = canvas.getContext('2d');

      _evChart = new Chart(ctx, {
        type: 'bar',
        data: {
          labels,
          datasets: [
            {
              label:           'Expected Net Value (₹)',
              data:            evValues,
              backgroundColor: bgColors,
              borderColor:     borderColors,
              borderWidth:     1.5,
              borderRadius:    5,
              maxBarThickness: 48,
            },
          ],
        },
        options: {
          indexAxis: 'y',
          responsive: true,
          maintainAspectRatio: false,
          animation: { duration: 300 },
          plugins: {
            legend: { display: false },
            tooltip: {
              backgroundColor: c.tooltipBg,
              borderColor:     c.tooltipBorder,
              borderWidth:     1,
              titleColor:      c.tooltipText,
              bodyColor:       c.tickColor,
              padding:         10,
              cornerRadius:    6,
              callbacks: {
                title:  items => items[0].label + (items[0].dataIndex === bestIdx ? '  ← Selected' : ''),
                label: item => {
                  const i = item.dataIndex;
                  return [
                    `  Expected net value: ${_fmtRsCompact(evValues[i])}`,
                    `  Amount at risk: ${_fmtRsCompact(atRisk[i])}`,
                    `  Cases: ${counts[i]}`,
                  ];
                },
              },
            },
          },
          scales: {
            x: {
              grid:   { color: c.gridLine, drawBorder: false },
              ticks:  { color: c.tickColor, font: { size: 11 }, callback: v => _fmtRsCompact(v) },
              border: { display: false },
              beginAtZero: true,
              title:  { display: true, text: 'Expected Net Value (₹) [Estimated]', color: c.tickColor, font: { size: 10 } },
            },
            y: {
              grid:   { drawOnChartArea: false },
              ticks:  { color: c.tickColor, font: { size: 11 } },
              border: { display: false },
            },
          },
        },
      });

      // Render the AI explanation panel
      if (explain) {
        const best = byStrategy[bestIdx];
        const totalEv = evValues.reduce((a, b) => a + b, 0);
        const totalRisk = atRisk.reduce((a, b) => a + b, 0);
        const recoverPct = totalRisk > 0
          ? ((totalEv / totalRisk) * 100).toFixed(1) : '—';

        // Cost assumptions from the API
        const ca = data.cost_assumptions || {};
        const caText = ca.retry_cost_rs != null
          ? `Retry: ₹${ca.retry_cost_rs}, SMS: ₹${ca.sms_cost_rs}, Email: ₹${ca.email_cost_rs}`
          : '';

        explain.innerHTML = `
          <div class="cc-ev-intel">
            <div class="cc-ev-intel-head">
              <div class="cc-ev-intel-label">AI Decision</div>
              <span class="badge badge-estimated">Estimated</span>
            </div>

            <div class="cc-ev-best-row">
              <div class="cc-ev-best-strategy">${esc(best ? best.strategy : '—')}</div>
              <div class="cc-ev-best-tag">highest expected net value</div>
            </div>

            <div class="cc-ev-metrics">
              <div class="cc-ev-metric">
                <span class="cc-ev-metric-label">Portfolio at Risk</span>
                <span class="cc-ev-metric-value">${_fmtRsCompact(totalRisk)}</span>
              </div>
              <div class="cc-ev-metric">
                <span class="cc-ev-metric-label">Total Expected Net Value</span>
                <span class="cc-ev-metric-value accent-ok">${_fmtRsCompact(totalEv)}</span>
              </div>
              <div class="cc-ev-metric">
                <span class="cc-ev-metric-label">Expected Recovery %</span>
                <span class="cc-ev-metric-value">${recoverPct}%</span>
              </div>
            </div>

            <div class="cc-ev-why">
              <div class="cc-ev-why-title">Why this strategy?</div>
              <p>The AI selects the action with the best <strong>expected net value</strong>
                 — not just the highest recovery probability.
                 Expected value = P(recovery) × amount − intervention cost − customer friction cost.
                 A high-probability strategy with high friction cost may be outranked by a lower-probability
                 strategy with minimal cost.</p>
            </div>

            <div class="cc-ev-breakdown">
              ${byStrategy.map((s, i) => {
                const isB = i === bestIdx;
                return `
                  <div class="cc-ev-row ${isB ? 'cc-ev-row-best' : ''}">
                    <div class="cc-ev-row-name">${esc(s.strategy)}</div>
                    <div class="cc-ev-row-bar-wrap">
                      <div class="cc-ev-row-bar" style="width:${totalEv > 0 ? Math.round((evValues[i]/Math.max(...evValues))*100) : 0}%;background:${isB ? c.green : c.blue};opacity:${isB ? 0.9 : 0.5}"></div>
                    </div>
                    <div class="cc-ev-row-val" style="color:${isB ? c.green : 'inherit'}">${_fmtRsCompact(evValues[i])}</div>
                    ${isB ? `<div class="cc-ev-selected-tag">selected</div>` : ''}
                  </div>`;
              }).join('')}
            </div>

            ${caText ? `<div class="cc-ev-assumptions">Cost assumptions: ${esc(caText)}</div>` : ''}
          </div>`;
      }

    } catch (err) {
      _showChartLoading('cc-ev-loading', false);
      _showChartEmpty('cc-ev-wrap', 'Could not load economic value data.');
      if (explain) explain.innerHTML = `<div class="cc-ev-explain-empty muted">Economic value data unavailable.</div>`;
      console.error('EV chart error:', err);
    }
  }

  // ---- Master loader: called from loadCommandCenter ----------------------

  async function loadCommandCenterCharts() {
    // Run all 5 chart loads in parallel; each handles its own errors gracefully.
    const days = parseInt((document.getElementById('cc-trend-days') || {}).value || '30', 10);
    await Promise.allSettled([
      loadTrendChart(days),
      loadFunnelChart(),
      loadStrategyChart(),
      loadFailureChart(),
      loadEvChart(),
    ]);
  }

  async function seedDemoData() {
    const btn = document.querySelector('[onclick="P7.seedDemoData()"]');
    if (btn) { btn.disabled = true; btn.textContent = 'Seeding…'; }
    const d = await apiPost('/api/v2/demo/seed-checkouts', {});
    if (btn) { btn.disabled = false; btn.textContent = 'Seed demo data'; }
    const parts = [
      d.checkouts != null ? `${d.checkouts} checkout session${d.checkouts !== 1 ? 's' : ''}` : '',
      d.invoices  != null ? `${d.invoices} invoice${d.invoices !== 1 ? 's' : ''}`             : '',
      d.promises  != null ? `${d.promises} promise${d.promises !== 1 ? 's' : ''}`             : '',
    ].filter(Boolean);
    const msg = parts.length
      ? `Seeded: ${parts.join(', ')}. Data is isolated (is_demo=1).`
      : 'Demo data seeded.';
    // Show a non-blocking banner instead of alert
    const banner = document.getElementById('status-banner');
    if (banner) {
      banner.textContent = msg;
      banner.className   = 'banner';
      banner.classList.remove('hidden');
      setTimeout(() => banner.classList.add('hidden'), 4000);
    } else {
      alert(msg);
    }
    loadCheckout();
  }

  /* ================================================================
     PUBLIC API
  ================================================================ */
  return {
    activateView,
    loadCommandCenter, loadRevenueJourney, loadRecoveryCases,
    loadCheckout, loadB2B, loadPromises, loadPolicy,
    savePolicy, resetPolicy,
    openCase, closeCase, executeCase, scoreCase,
    recoverCheckout,
    remindInvoice, escalateInvoice, payInvoice,
    payPromise, missPromise,
    askCopilot, copilotQ,
    runFullDemo, resetDemo, loadDemo, seedDemoData,
    decideApproval,
    // Revenue intelligence charts
    refreshTrendChart,
    loadCommandCenterCharts,
    loadCommandCenterCases,
    loadCommandCenterFunnelSidebar,
  };
})();
