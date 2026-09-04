/**
 * p7.js - Phase 7 Revenue Recovery OS
 * Handles all Phase 7 views: Command Center, Revenue Journey, Recovery Cases,
 * Checkout Recovery, B2B Receivables, Promises, Policy Center, Copilot, Demo.
 *
 * DATA TYPE LABELS displayed in every view:
 *   ACTUAL    - counts and realized values from the database
 *   ESTIMATED - model-derived probabilities and expected values
 *   SIMULATED - synthetic / demo data
 */

window.P7 = (() => {
  /* ---- utilities ---- */
  const fmt_rs  = n => n == null ? '—' : 'Rs ' + Number(n).toLocaleString('en-IN', {maximumFractionDigits: 0});
  const fmt_pct = n => n == null ? '—' : Number(n).toFixed(1) + '%';
  const el  = id  => document.getElementById(id);
  const set = (id, v) => { const e = el(id); if (e) e.textContent = v; };

  function statusBadge(s) {
    return `<span class="status-badge status-${s}">${(s || '').replace(/_/g, ' ')}</span>`;
  }
  function priorityBadge(p) {
    return `<span class="queue-badge ${p}">${p}</span>`;
  }

  /* ---- view activation ---- */
  function activateView(name) {
    document.querySelectorAll('.view-content').forEach(s => s.classList.add('hidden'));
    // Also deactivate any app.js .view section so they don't show behind p7 panels
    document.querySelectorAll('.view').forEach(s => s.classList.remove('active'));
    const v = document.getElementById('view-' + name);
    if (v) v.classList.remove('hidden');
    document.querySelectorAll('.nav-item[data-view]').forEach(b => {
      b.classList.toggle('active', b.dataset.view === name);
    });
    const loaders = {
      'command-center':   loadCommandCenter,
      'revenue-journey':  loadRevenueJourney,
      'recovery-cases':   loadRecoveryCases,
      'checkout-recovery':loadCheckout,
      'b2b-receivables':  loadB2B,
      'promises':         loadPromises,
      'recovery-policy':  loadPolicy,
      'copilot':          () => {},
      'p7-demo':          () => {},
    };
    if (loaders[name]) loaders[name]();
  }

  document.addEventListener('DOMContentLoaded', () => {
    document.querySelectorAll('.nav-item[data-view]').forEach(btn => {
      btn.addEventListener('click', () => activateView(btn.dataset.view));
    });
  });

  /* ---- API helpers ---- */
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
    const demo = el('rc-demo-only') && el('rc-demo-only').checked ? '1' : '0';
    const [port, queue, approvals] = await Promise.all([
      apiGet(`/api/v2/portfolio?demo=${demo}`),
      apiGet(`/api/v2/priority-queue?limit=15&demo=${demo}`),
      apiGet('/api/v2/approvals?status=pending'),
    ]);
    const d = port.data || {};
    set('cc-revenue-at-risk',   fmt_rs(d.revenue_at_risk));
    set('cc-recoverable',       fmt_rs(d.recoverable_revenue));
    set('cc-recovered',         fmt_rs(d.recovered_revenue));
    set('cc-rate',              fmt_pct(d.recovery_rate));
    set('cc-checkout',          d.checkout_abandoned ?? '—');
    set('cc-overdue',           fmt_rs(d.overdue_receivables_amount));
    set('cc-missed-promises',   d.missed_promises ?? '—');
    set('cc-active-cases',      d.active_cases ?? '—');

    /* priority queue */
    const qEl = el('cc-queue');
    if (qEl) {
      const cases = queue.data || [];
      if (!cases.length) {
        qEl.innerHTML = '<div class="empty-state">No open cases. Seed data or run the demo.</div>';
      } else {
        qEl.innerHTML = cases.map(c => `
          <div class="queue-card" onclick="P7.openCase('${c.case_id}')">
            <div>
              ${priorityBadge(c.priority)}
              <div class="queue-scenario">${c.scenario_type.replace(/_/g, ' ')}</div>
            </div>
            <div>
              <div class="queue-what">${c.what_happened || ''}</div>
              <div class="queue-why">${c.why_it_matters || ''}</div>
              <div class="queue-next">${c.what_next || ''}</div>
            </div>
            <div>
              <div class="queue-amount">${fmt_rs(c.amount)}</div>
              <div class="queue-ev">${fmt_rs(c.expected_recovery_value)} EV
                <span class="muted">[EST]</span></div>
            </div>
          </div>`).join('');
      }
    }

    /* approvals */
    const apEl = el('cc-approvals-list');
    if (apEl) {
      const aps = approvals.data || [];
      apEl.innerHTML = aps.length === 0
        ? '<div class="empty-state muted">No pending approvals.</div>'
        : aps.map(a => `
            <div class="approval-card">
              <div class="approval-info">
                <div class="approval-title">${a.title}</div>
                <div class="approval-desc">${a.description}</div>
                <div class="approval-ev">EV: ${fmt_rs(a.expected_value)} [ESTIMATED]</div>
              </div>
              <div style="display:flex;gap:.5rem">
                <button class="btn btn-primary btn-sm"
                  onclick="P7.decideApproval('${a.request_id}','approved')">Approve</button>
                <button class="btn btn-ghost btn-sm"
                  onclick="P7.decideApproval('${a.request_id}','rejected')">Reject</button>
              </div>
            </div>`).join('');
    }
  }

  async function decideApproval(reqId, decision) {
    await apiPost(`/api/v2/approvals/${reqId}`, { decision });
    loadCommandCenter();
  }

  /* ================================================================
     REVENUE JOURNEY
     ================================================================ */
  async function loadRevenueJourney() {
    const d = await apiGet('/api/v2/revenue-journey');
    const stages = d.stages || [];
    const container = el('rj-journey');
    if (!container) return;
    container.innerHTML = stages.map((s, i) => `
      <div class="journey-stage">
        <div style="font-weight:700;font-size:.9rem">${s.stage}</div>
        <div style="font-size:.75rem;color:var(--muted);margin-top:2px">${fmt_rs(s.value_rs)}</div>
        <div style="font-size:.65rem;color:var(--muted)">${s.count} cases</div>
      </div>
      ${i < stages.length - 1 ? '<div class="journey-arrow">&darr;</div>' : ''}`
    ).join('');
  }

  /* ================================================================
     RECOVERY CASES
     ================================================================ */
  async function loadRecoveryCases() {
    const status   = (el('rc-filter-status')   || {}).value || '';
    const scenario = (el('rc-filter-scenario') || {}).value || '';
    const demo     = el('rc-demo-only') && el('rc-demo-only').checked ? '1' : '0';
    let url = `/api/v2/cases?limit=50&demo=${demo}`;
    if (status)   url += `&status=${status}`;
    if (scenario) url += `&scenario_type=${scenario}`;
    const d    = await apiGet(url);
    const wrap = el('rc-table-wrap');
    if (!wrap) return;
    const cases = d.data || [];
    if (!cases.length) {
      wrap.innerHTML = '<div class="empty-state">No cases found. Seed demo data to see recovery cases.</div>';
      return;
    }
    wrap.innerHTML = `
      <table class="p7-table">
        <thead><tr>
          <th>Case</th><th>Scenario</th><th>Status</th><th>Priority</th>
          <th>Amount</th><th>EV [Est]</th><th>P(Rec)</th><th>Source</th><th></th>
        </tr></thead>
        <tbody>${cases.map(c => `
          <tr>
            <td class="mono" style="font-size:.72rem">${c.case_id.substring(0, 8)}&hellip;</td>
            <td>${c.scenario_type.replace(/_/g, ' ')}</td>
            <td>${statusBadge(c.status)}</td>
            <td>${priorityBadge(c.priority)}</td>
            <td>${fmt_rs(c.amount)}</td>
            <td>${fmt_rs(c.expected_recovery_value)}</td>
            <td>${c.recovery_probability
                  ? (c.recovery_probability * 100).toFixed(0) + '%' : '—'}</td>
            <td><span class="badge ${c.source === 'REAL' ? 'badge-real' : 'badge-simulated'}">
              ${c.source || 'SIM'}</span></td>
            <td><button class="btn btn-ghost btn-xs"
                  onclick="P7.openCase('${c.case_id}')">View</button></td>
          </tr>`).join('')}
        </tbody>
      </table>`;
  }

  /* ---- unified case detail modal ---- */
  async function openCase(caseId) {
    el('p7-case-modal')    && el('p7-case-modal').classList.remove('hidden');
    el('p7-modal-overlay') && el('p7-modal-overlay').classList.remove('hidden');
    const body = el('p7-case-modal-body');
    if (body) body.innerHTML = '<div class="empty-state">Loading&hellip;</div>';
    const d  = await apiGet(`/api/v2/cases/${caseId}`);
    const c  = d.case     || {};
    const tl = d.timeline || [];
    if (body) {
      body.innerHTML = `
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:1rem;margin-bottom:1rem">
          <div><div class="kpi-label">Scenario</div>
               <div>${(c.scenario_type || '').replace(/_/g, ' ')}</div></div>
          <div><div class="kpi-label">Status</div>
               <div>${statusBadge(c.status || '')}</div></div>
          <div><div class="kpi-label">Amount</div>
               <div style="font-weight:700">${fmt_rs(c.amount)}</div></div>
          <div><div class="kpi-label">Priority</div>
               <div>${priorityBadge(c.priority || 'low')}</div></div>
          <div><div class="kpi-label">Risk Score</div>
               <div>${c.risk_score ? c.risk_score + '/100' : '—'}</div></div>
          <div><div class="kpi-label">P(Recovery)</div>
               <div>${c.recovery_probability
                 ? (c.recovery_probability * 100).toFixed(0) + '% [EST]' : '—'}</div></div>
          <div><div class="kpi-label">Expected Value</div>
               <div>${fmt_rs(c.expected_recovery_value)}
                 <span class="badge badge-estimated">ESTIMATED</span></div></div>
          <div><div class="kpi-label">Realized</div>
               <div>${fmt_rs(c.realized_value)}
                 <span class="badge badge-real">ACTUAL</span></div></div>
          <div><div class="kpi-label">Recommended Action</div>
               <div>${c.recommended_action || '—'}</div></div>
          <div><div class="kpi-label">Channel</div>
               <div>${c.preferred_channel || '—'}</div></div>
          <div><div class="kpi-label">Source</div>
               <div><span class="badge ${c.source === 'REAL' ? 'badge-real' : 'badge-simulated'}">
                 ${c.source || 'SIMULATED'}</span></div></div>
          <div><div class="kpi-label">Approval</div>
               <div>${c.approval_status || 'not_required'}</div></div>
        </div>
        ${c.ai_explanation
          ? `<div class="section-card" style="padding:.75rem;margin-bottom:.75rem">
               <strong>AI Explanation:</strong> ${c.ai_explanation}</div>` : ''}
        <h3 style="font-size:.9rem;margin:1rem 0 .5rem">Case Timeline</h3>
        ${tl.length === 0 ? '<div class="muted">No events yet.</div>'
          : tl.map(e => `
              <div class="timeline-event">
                <div>
                  <div class="timeline-type">${e.event_type}</div>
                  <div class="timeline-ts">${e.occurred_at || ''}</div>
                  <div class="timeline-ts">
                    <span class="badge ${
                      e.data_type === 'REAL'      ? 'badge-real'      :
                      e.data_type === 'ESTIMATED' ? 'badge-estimated' : 'badge-simulated'}">
                      ${e.data_type || 'SIM'}</span>
                  </div>
                </div>
                <div>${e.description || ''}</div>
              </div>`).join('')}
        <div style="margin-top:1rem;display:flex;gap:.5rem;flex-wrap:wrap">
          <button class="btn btn-primary btn-sm"
            onclick="P7.executeCase('${caseId}')">Execute Action [SIMULATED]</button>
          <button class="btn btn-ghost btn-sm"
            onclick="P7.scoreCase('${caseId}')">Re-score</button>
        </div>`;
    }
    set('p7-case-modal-title',
        `Case: ${caseId.substring(0, 8)}… — ${(c.scenario_type || '').replace(/_/g, ' ')}`);
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
    const [funnel, sessions] = await Promise.all([
      apiGet('/api/v2/checkout/funnel'),
      apiGet('/api/v2/checkout/sessions'),
    ]);
    const f = funnel.data || {};
    set('co-abandoned', f.abandoned_sessions ?? '—');
    set('co-recovered', f.recovered_sessions ?? '—');
    set('co-rate',      fmt_pct(f.recovery_rate_pct));
    set('co-value',     fmt_rs(f.abandoned_value_rs));
    set('co-opp',       fmt_rs(f.opportunity_rs));
    const tEl = el('co-sessions-table');
    if (!tEl) return;
    const ss = sessions.data || [];
    tEl.innerHTML = ss.length === 0
      ? '<div class="empty-state">No abandoned sessions. Click "Seed Demo" to create sample data.</div>'
      : `<table class="p7-table">
           <thead><tr>
             <th>Session</th><th>Amount</th><th>Stage</th>
             <th>Status</th><th>Email</th><th></th>
           </tr></thead>
           <tbody>${ss.map(s => `
             <tr>
               <td class="mono" style="font-size:.72rem">${s.session_id.substring(0, 8)}&hellip;</td>
               <td>${fmt_rs(s.amount)}</td>
               <td>${s.stage_reached || '—'}</td>
               <td>${statusBadge(s.status)}</td>
               <td>${s.customer_email || '—'}</td>
               <td><button class="btn btn-ghost btn-xs"
                     onclick="P7.recoverCheckout('${s.session_id}')">Mark Recovered</button></td>
             </tr>`).join('')}
           </tbody>
         </table>`;
  }

  async function recoverCheckout(sid) {
    await apiPost(`/api/v2/checkout/sessions/${sid}/recover`, {});
    loadCheckout();
  }

  /* ================================================================
     B2B RECEIVABLES
     ================================================================ */
  async function loadB2B() {
    const status = (el('b2b-filter') || {}).value || '';
    const [aging, invoices] = await Promise.all([
      apiGet('/api/v2/b2b/aging'),
      apiGet(`/api/v2/b2b/invoices${status ? '?status=' + status : ''}`),
    ]);
    const a = (aging.data || {}).buckets || {};
    set('b2b-total',   fmt_rs((aging.data || {}).total_outstanding));
    set('b2b-0-30',    fmt_rs((a['0_30']   || {}).amount));
    set('b2b-31-60',   fmt_rs((a['31_60']  || {}).amount));
    set('b2b-61-90',   fmt_rs((a['61_90']  || {}).amount));
    set('b2b-90plus',  fmt_rs((a['90_plus'] || {}).amount));
    const tEl = el('b2b-table');
    if (!tEl) return;
    const invs = invoices.data || [];
    tEl.innerHTML = invs.length === 0
      ? '<div class="empty-state">No invoices. Seed demo data to see B2B receivables.</div>'
      : `<table class="p7-table">
           <thead><tr>
             <th>Invoice</th><th>Customer</th><th>Amount</th>
             <th>Due</th><th>Days Overdue</th><th>Status</th><th>Priority</th><th>Actions</th>
           </tr></thead>
           <tbody>${invs.map(i => `
             <tr>
               <td>${i.invoice_number || i.invoice_id.substring(0, 8)}</td>
               <td>${i.customer_name}</td>
               <td>${fmt_rs(i.amount)}</td>
               <td>${(i.due_at || '').substring(0, 10)}</td>
               <td>${i.overdue_days || 0}</td>
               <td>${statusBadge(i.status)}</td>
               <td>${priorityBadge(i.priority)}</td>
               <td style="display:flex;gap:.3rem;flex-wrap:wrap">
                 <button class="btn btn-ghost btn-xs"
                   onclick="P7.remindInvoice('${i.invoice_id}')">Remind</button>
                 <button class="btn btn-ghost btn-xs"
                   onclick="P7.escalateInvoice('${i.invoice_id}')">Escalate</button>
                 <button class="btn btn-primary btn-xs"
                   onclick="P7.payInvoice('${i.invoice_id}')">Mark Paid</button>
               </td>
             </tr>`).join('')}
           </tbody>
         </table>`;
  }

  async function remindInvoice(id)   { await apiPost(`/api/v2/b2b/invoices/${id}/remind`,   {}); loadB2B(); }
  async function escalateInvoice(id) { await apiPost(`/api/v2/b2b/invoices/${id}/escalate`, {}); loadB2B(); }
  async function payInvoice(id)      { await apiPost(`/api/v2/b2b/invoices/${id}/paid`,     {}); loadB2B(); }

  /* ================================================================
     PROMISES
     ================================================================ */
  async function loadPromises() {
    const status = (el('prom-filter') || {}).value || '';
    const [sum, proms] = await Promise.all([
      apiGet('/api/v2/promises/summary'),
      apiGet(`/api/v2/promises${status ? '?status=' + status : ''}`),
    ]);
    const s = sum.data || {};
    set('prom-total',  s.total_promises    ?? '—');
    set('prom-paid',   s.paid_promises     ?? '—');
    set('prom-missed', s.missed_promises   ?? '—');
    set('prom-due',    s.due_today         ?? '—');
    set('prom-conv',   fmt_pct(s.conversion_rate_pct));
    const tEl = el('prom-table');
    if (!tEl) return;
    const ps = proms.data || [];
    tEl.innerHTML = ps.length === 0
      ? '<div class="empty-state">No promises. Seed demo data to see payment promises.</div>'
      : `<table class="p7-table">
           <thead><tr>
             <th>Customer</th><th>Amount</th><th>Promised Date</th>
             <th>Status</th><th>Confidence</th><th>Actions</th>
           </tr></thead>
           <tbody>${ps.map(p => `
             <tr>
               <td>${p.customer_name || p.customer_ref || '—'}</td>
               <td>${fmt_rs(p.promised_amount)}</td>
               <td>${(p.promised_date || '').substring(0, 10)}</td>
               <td>${statusBadge(p.status)}</td>
               <td><span class="badge badge-estimated">${p.confidence || 'medium'}</span></td>
               <td style="display:flex;gap:.3rem">
                 <button class="btn btn-primary btn-xs"
                   onclick="P7.payPromise('${p.promise_id}')">Paid</button>
                 <button class="btn btn-ghost btn-xs"
                   onclick="P7.missPromise('${p.promise_id}')">Missed</button>
               </td>
             </tr>`).join('')}
           </tbody>
         </table>`;
  }

  async function payPromise(id)  { await apiPost(`/api/v2/promises/${id}/paid`,   {}); loadPromises(); }
  async function missPromise(id) { await apiPost(`/api/v2/promises/${id}/missed`,  {}); loadPromises(); }

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
    const body = {
      max_retries:              parseInt(el('pol-max-retries').value),
      retry_cooldown_hours:     parseInt(el('pol-cooldown').value),
      max_messages_per_week:    parseInt(el('pol-max-msgs').value),
      preferred_channel:        el('pol-channel').value,
      preferred_language:       el('pol-language').value,
      working_hours_start:      parseInt(el('pol-start').value),
      working_hours_end:        parseInt(el('pol-end').value),
      min_expected_value_rs:    parseFloat(el('pol-min-ev').value),
      approval_threshold_rs:    parseFloat(el('pol-approval').value),
      checkout_recovery_enabled:parseInt(el('pol-checkout').value),
      b2b_recovery_enabled:     parseInt(el('pol-b2b').value),
      voice_recovery_enabled:   parseInt(el('pol-voice').value),
    };
    const d   = await apiPatch('/api/v2/policy', body);
    const msg = el('policy-save-msg');
    if (msg) {
      msg.textContent = d.ok ? '✓ Policy saved.' : 'Error: ' + JSON.stringify(d.errors || d.error);
      msg.className   = 'save-msg ' + (d.ok ? 'ok' : 'err');
      msg.classList.remove('hidden');
      setTimeout(() => msg.classList.add('hidden'), 3000);
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
    const inp = el('copilot-input');
    if (!inp) return;
    const q = inp.value.trim();
    if (!q) return;
    inp.value = '';
    const hist = el('copilot-history');
    if (hist) {
      hist.innerHTML += `<div class="copilot-msg user"><strong>You:</strong> ${q}</div>`;
      hist.innerHTML += `<div class="copilot-msg system" id="copilot-typing">Thinking&hellip;</div>`;
      hist.scrollTop  = hist.scrollHeight;
    }
    const d      = await apiPost('/api/v2/copilot/ask', { question: q });
    const typing = document.getElementById('copilot-typing');
    if (typing) typing.remove();
    if (hist) {
      hist.innerHTML += `<div class="copilot-msg answer"><strong>Copilot:</strong> ${
        (d.answer || 'No answer returned.').replace(/\n/g, '<br>')
      }</div>`;
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
    if (stepsEl) stepsEl.innerHTML = '<div class="empty-state">Running demo scenario&hellip;</div>';
    const d = await apiPost('/api/v2/demo/run', {});
    if (!d.ok) {
      if (stepsEl) stepsEl.innerHTML =
        `<div class="empty-state" style="color:red">Error: ${d.error || 'unknown'}</div>`;
      return;
    }
    const steps = d.demo_steps || [];
    if (stepsEl) {
      stepsEl.innerHTML = steps.map(s => `
        <div class="demo-step-card">
          <div class="demo-step-num">${s.step}</div>
          <div>
            <div class="demo-step-title">${s.title}
              <span class="demo-step-badge">SIMULATED</span></div>
            <div class="demo-step-desc">${s.description}</div>
          </div>
        </div>`).join('');
    }
    if (portEl && d.portfolio_summary) {
      portEl.classList.remove('hidden');
      const p = d.portfolio_summary;
      el('demo-portfolio-body').innerHTML = `
        <div class="kpi-grid">
          <div class="kpi-card">
            <div class="kpi-label">Total Cases</div>
            <div class="kpi-value">${p.total_cases}</div>
          </div>
          <div class="kpi-card success">
            <div class="kpi-label">Recovered Revenue</div>
            <div class="kpi-value">${fmt_rs(p.recovered_revenue)}</div>
            <div class="kpi-sub">ACTUAL (SIMULATED data)</div>
          </div>
          <div class="kpi-card critical">
            <div class="kpi-label">Still at Risk</div>
            <div class="kpi-value">${fmt_rs(p.revenue_at_risk)}</div>
            <div class="kpi-sub">ACTUAL</div>
          </div>
          <div class="kpi-card">
            <div class="kpi-label">Recovery Rate</div>
            <div class="kpi-value">${fmt_pct(p.recovery_rate)}</div>
            <div class="kpi-sub">ACTUAL</div>
          </div>
        </div>
        <p class="muted" style="font-size:.8rem;margin-top:.5rem">
          ${d.isolation_note || 'Demo data is isolated from real merchant data (is_demo=1).'}
        </p>`;
    }
  }

  async function resetDemo() {
    await apiPost('/api/v2/demo/reset', {});
    const stepsEl = el('demo-steps');
    if (stepsEl) stepsEl.innerHTML =
      '<div class="empty-state">Demo reset. Click "Run Full Demo" to start again.</div>';
    const portEl = el('demo-portfolio');
    if (portEl) portEl.classList.add('hidden');
  }

  function loadDemo() { activateView('p7-demo'); }

  async function seedDemoData() {
    const d = await apiPost('/api/v2/demo/seed-checkouts', {});
    const msg = [
      d.checkouts  != null ? `${d.checkouts} checkouts`  : '',
      d.invoices   != null ? `${d.invoices} invoices`    : '',
      d.promises   != null ? `${d.promises} promises`    : '',
    ].filter(Boolean).join(', ');
    alert(`Seeded: ${msg} [SIMULATED — is_demo=1, isolated from real data]`);
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
  };
})();
