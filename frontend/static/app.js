"use strict";

// --- Formatting helpers -----------------------------------------------------
function rupees(n) {
  return "\u20B9" + Math.round(Number(n)).toLocaleString("en-IN");
}
function pct(x) {
  return (Number(x) * 100).toFixed(1) + "%";
}
function titleCase(s) {
  return String(s || "").replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}
// Mask a customer id for display: keep first 4 + last 2 chars, e.g. CUST0042 -> CUST**42.
// The real (unmasked) value stays in the DB and is used for all lookups/joins.
function maskId(id) {
  const s = String(id == null ? "" : id);
  if (s.length <= 6) return s;
  return s.slice(0, 4) + "**" + s.slice(-2);
}
function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
}

async function getJSON(url) {
  const r = await fetch(url);
  if (!r.ok) throw new Error("Request failed: " + url + " (" + r.status + ")");
  return r.json();
}
async function postJSON(url) {
  const r = await fetch(url, { method: "POST" });
  if (!r.ok) throw new Error("Request failed: " + url + " (" + r.status + ")");
  return r.json();
}

function banner(msg, isErr) {
  const b = document.getElementById("status-banner");
  b.textContent = msg;
  b.className = "banner" + (isErr ? " err" : "");
  b.classList.remove("hidden");
}
function clearBanner() {
  document.getElementById("status-banner").classList.add("hidden");
}


// --- State ------------------------------------------------------------------
let baselineChart = null;
let cohortData = null;
let activeCohort = "tenure";

// --- KPIs + baseline chart --------------------------------------------------
function renderMetrics(data) {
  const a = data.agent;
  const b = data.baseline;
  document.getElementById("kpi-at-risk").textContent = rupees(a.amount_at_risk);
  document.getElementById("kpi-recovered").textContent = rupees(a.amount_recovered);
  document.getElementById("kpi-recovery-rate").textContent = pct(a.recovery_rate);
  document.getElementById("kpi-escalation-rate").textContent = pct(a.escalation_rate);

  // Only show the comparison once a run has actually completed. Before a run,
  // every case is still "New" so nothing has been recovered or escalated.
  const hasRun = (a.recovered_cases || 0) > 0 || (a.escalated_cases || 0) > 0;
  const upliftNote = document.getElementById("uplift-note");
  if (!hasRun) {
    upliftNote.innerHTML = "Run the agent to see this comparison.";
  } else {
    const diff = a.amount_recovered - b.amount_recovered;
    // Never show a negative number in the sentence: choose the word, show |diff|.
    const word = diff >= 0 ? "more" : "less";
    upliftNote.innerHTML =
      "The agent recovered <b>" + rupees(Math.abs(diff)) + " " + word + "</b> than the naive baseline (" +
      rupees(a.amount_recovered) + " vs " + rupees(b.amount_recovered) + ").";
  }

  const ctx = document.getElementById("baseline-chart").getContext("2d");
  if (baselineChart) baselineChart.destroy();
  baselineChart = new Chart(ctx, {
    type: "bar",
    data: {
      labels: ["Naive baseline", "Mandate Rescue agent"],
      datasets: [{
        label: "Amount recovered",
        data: [b.amount_recovered, a.amount_recovered],
        backgroundColor: ["#D5DAE4", "#0E9F6E"],
        borderRadius: 8,
        maxBarThickness: 90,
      }],
    },
    options: {
      plugins: { legend: { display: false },
        tooltip: { callbacks: { label: (c) => rupees(c.raw) } } },
      scales: { y: { beginAtZero: true, ticks: { callback: (v) => rupees(v) } } },
    },
  });
}


// --- ML model evaluation panel ----------------------------------------------
// Every value shown here is read directly from /api/ml-metrics (backed by
// backend/ml/metrics.json). Nothing is hardcoded; if the model is untrained the
// panel stays hidden.
function metricCard(label, value, sub) {
  const card = el("div", "ml-metric");
  card.appendChild(el("span", "ml-metric-label", label));
  card.appendChild(el("span", "ml-metric-value", value));
  if (sub) card.appendChild(el("span", "ml-metric-sub", sub));
  return card;
}

function fmtMetric(x) {
  return (Number(x)).toFixed(4);
}

function renderMlPanel(data) {
  const panel = document.getElementById("ml-panel");
  if (!data || !data.available) {
    panel.classList.add("hidden");
    return;
  }
  panel.classList.remove("hidden");

  const w = data.winner_metrics;
  const ds = data.dataset || {};

  document.getElementById("ml-winner-hint").textContent =
    "Best of 2 models by ROC-AUC: " + data.best_model;

  // Headline metrics (real, from the held-out test set).
  const grid = document.getElementById("ml-metric-grid");
  grid.innerHTML = "";
  grid.appendChild(metricCard("ROC-AUC", fmtMetric(w.roc_auc), "held-out test"));
  grid.appendChild(metricCard("Precision", fmtMetric(w.precision), "of predicted recoveries"));
  grid.appendChild(metricCard("Recall", fmtMetric(w.recall), "of actual recoveries"));
  grid.appendChild(metricCard("F1 score", fmtMetric(w.f1), "harmonic mean"));

  // Confusion matrix (2x2) for the winning model.
  const cm = w.confusion_matrix;
  const cmTable = document.getElementById("ml-cm-table");
  cmTable.innerHTML =
    "<thead><tr><th></th><th>pred: not recovered</th><th>pred: recovered</th></tr></thead>" +
    "<tbody>" +
    "<tr><th>actual: not recovered</th>" +
    "<td class='cm-tn'>" + cm.true_negatives + "</td>" +
    "<td class='cm-fp'>" + cm.false_positives + "</td></tr>" +
    "<tr><th>actual: recovered</th>" +
    "<td class='cm-fn'>" + cm.false_negatives + "</td>" +
    "<td class='cm-tp'>" + cm.true_positives + "</td></tr>" +
    "</tbody>";
  document.getElementById("ml-cm-cap").textContent =
    "Green cells are correct predictions on the " + (ds.test_size || "?") +
    "-row test set.";

  // Side-by-side model comparison table.
  const cmp = document.getElementById("ml-compare-table");
  let rows = "<thead><tr><th>Model</th><th>AUC</th><th>Precision</th>" +
    "<th>Recall</th><th>F1</th></tr></thead><tbody>";
  Object.entries(data.models || {}).forEach(([name, m]) => {
    const winCls = name === data.best_model ? " ml-win" : "";
    rows += "<tr class='" + winCls.trim() + "'><td>" + name +
      (name === data.best_model ? " \u2605" : "") + "</td>" +
      "<td class='num'>" + fmtMetric(m.roc_auc) + "</td>" +
      "<td class='num'>" + fmtMetric(m.precision) + "</td>" +
      "<td class='num'>" + fmtMetric(m.recall) + "</td>" +
      "<td class='num'>" + fmtMetric(m.f1) + "</td></tr>";
  });
  rows += "</tbody>";
  cmp.innerHTML = rows;

  document.getElementById("ml-dataset-note").textContent =
    "Trained on " + (ds.train_size || "?") + " rows, evaluated on " +
    (ds.test_size || "?") + " held-out rows (" +
    Math.round((ds.test_split_fraction || 0.2) * 100) + "% split, stratified, " +
    "random_state=" + (ds.random_state != null ? ds.random_state : "?") + "). " +
    (ds.source || "");
}

// --- Correctness audit panel ------------------------------------------------
// Renders the /api/audit-check report: a green "all passed" banner, or a red list
// of the specific failing rules with the offending case IDs.
function renderAuditPanel(report) {
  const panel = document.getElementById("audit-panel");
  const summary = document.getElementById("audit-summary");
  const checksBox = document.getElementById("audit-checks");
  if (!report || !report.checks) {
    panel.classList.add("hidden");
    return;
  }
  // Only meaningful once a run has completed (otherwise every case is still "new").
  if (!report.run_completed) {
    panel.classList.add("hidden");
    return;
  }
  panel.classList.remove("hidden");
  summary.innerHTML = "";
  checksBox.innerHTML = "";

  const passed = report.passed;
  const banner = el("div", "audit-banner " + (passed ? "ok" : "bad"));
  const icon = el("span", "audit-icon", passed ? "\u2713" : "\u2717");
  banner.appendChild(icon);
  const txt = el("span", "audit-banner-text",
    passed
      ? "All checks passed \u00B7 " + report.total_cases + " cases audited"
      : report.total_violations + " violation" + (report.total_violations === 1 ? "" : "s") +
        " found \u00B7 " + report.total_cases + " cases audited");
  banner.appendChild(txt);
  summary.appendChild(banner);

  report.checks.forEach((chk) => {
    const row = el("div", "audit-check " + (chk.passed ? "ok" : "bad"));
    const head = el("div", "audit-check-head");
    head.appendChild(el("span", "audit-check-icon", chk.passed ? "\u2713" : "\u2717"));
    head.appendChild(el("span", "audit-check-desc", chk.description));
    if (!chk.passed) {
      head.appendChild(el("span", "audit-check-count", String(chk.violation_count)));
    }
    row.appendChild(head);
    if (!chk.passed) {
      const list = el("ul", "audit-violations");
      chk.violations.forEach((v) => {
        const li = el("li");
        if (v.customer_id) {
          li.appendChild(el("span", "audit-cid num", maskId(v.customer_id)));
          li.appendChild(document.createTextNode(" " + v.detail));
        } else {
          li.textContent = v.detail;
        }
        list.appendChild(li);
      });
      row.appendChild(list);
    }
    checksBox.appendChild(row);
  });
}

// --- Cohort view ------------------------------------------------------------
function renderCohorts() {
  if (!cohortData) return;
  const rows = activeCohort === "tenure" ? cohortData.by_tenure : cohortData.by_category;
  const body = document.getElementById("cohort-body");
  body.innerHTML = "";
  rows.forEach((r) => {
    const wrap = el("div", "cohort-row");
    const top = el("div", "cohort-top");
    top.appendChild(el("span", null, titleCase(r.segment)));
    top.appendChild(el("span", "muted", pct(r.recovery_rate) + " \u00B7 " + r.recovered + "/" + r.total));
    const bar = el("div", "bar");
    const fill = el("span");
    fill.style.width = (r.recovery_rate * 100).toFixed(1) + "%";
    bar.appendChild(fill);
    wrap.appendChild(top);
    wrap.appendChild(bar);
    body.appendChild(wrap);
  });
}

// --- Cases table ------------------------------------------------------------
function scorePill(score) {
  const cls = score >= 75 ? "high" : (score < 45 ? "low" : "");
  const span = el("span", "score-pill " + cls, String(score));
  return span;
}

// Additive ML prediction pill (recovery probability). Purely informational — shown
// beside the rule-based score for comparison; never affects any decision. Shows a
// neutral dash when the model hasn't been trained (probability is null).
function mlProbPill(prob) {
  if (prob === null || prob === undefined) {
    return el("span", "ml-prob-pill na", "\u2014");
  }
  const p = Number(prob);
  const cls = p >= 0.75 ? "high" : (p < 0.45 ? "low" : "");
  return el("span", "ml-prob-pill " + cls, (p * 100).toFixed(0) + "%");
}

function complianceBadge(status) {
  if (status === "RBI-compliant") return { cls: "ok", text: "RBI-compliant" };
  if (status === "non-compliant") return { cls: "bad", text: "Non-compliant" };
  return { cls: "neutral", text: "N/A" };
}

function dunningIndicator(stage) {
  const wrap = el("span", "dunning");
  for (let i = 1; i <= 3; i++) {
    wrap.appendChild(el("span", "dot" + (i <= stage ? " on" : "")));
  }
  wrap.appendChild(el("span", "dlabel", stage > 0 ? stage + "/3" : "\u2014"));
  return wrap;
}

function healthBadge(score, band) {
  const cls = "health-" + (band || "at-risk");
  return el("span", "badge " + cls, score + " \u00B7 " + titleCase(band || ""));
}


function renderCases(cases) {
  const tbody = document.getElementById("cases-tbody");
  tbody.innerHTML = "";
  document.getElementById("cases-count").textContent = "(" + cases.length + ")";
  cases.forEach((c) => {
    const tr = el("tr");
    tr.addEventListener("click", () => openDrawer(c.customer_id));

    const tdScore = el("td");
    tdScore.appendChild(scorePill(c.score));
    tr.appendChild(tdScore);

    const tdMl = el("td");
    tdMl.appendChild(mlProbPill(c.ml_recovery_probability));
    tr.appendChild(tdMl);

    tr.appendChild(el("td", "num", maskId(c.customer_id)));

    const tdReason = el("td");
    tdReason.appendChild(el("span", "tag reason", titleCase(c.failure_reason)));
    tr.appendChild(tdReason);

    const tdAmt = el("td", "num", rupees(c.amount));
    if (c.over_limit) {
      tdAmt.appendChild(document.createTextNode(" "));
      tdAmt.appendChild(el("span", "badge warn", "over limit"));
    }
    tr.appendChild(tdAmt);

    tr.appendChild(el("td", "status " + c.case_status, titleCase(c.case_status)));

    const tdWin = el("td");
    tdWin.appendChild(el("span", "badge " + (c.salary_window_inferred ? "v2" : "neutral"),
      c.salary_window_inferred ? "v2 inferred" : "generic"));
    tr.appendChild(tdWin);

    const cb = complianceBadge(c.compliance_status);
    const tdComp = el("td");
    tdComp.appendChild(el("span", "badge " + cb.cls, cb.text));
    tr.appendChild(tdComp);

    const tdDun = el("td");
    tdDun.appendChild(dunningIndicator(c.dunning_stage || 0));
    tr.appendChild(tdDun);

    const tdHealth = el("td");
    tdHealth.appendChild(healthBadge(c.health_score, c.health_band));
    tr.appendChild(tdHealth);

    tbody.appendChild(tr);
  });
}

function renderExceptions(items) {
  const tbody = document.getElementById("exceptions-tbody");
  tbody.innerHTML = "";
  document.getElementById("exceptions-count").textContent = "(" + items.length + ")";
  items.forEach((e) => {
    const tr = el("tr");
    tr.appendChild(el("td", "num", maskId(e.customer_id)));
    tr.appendChild(el("td", "num", rupees(e.amount)));
    tr.appendChild(el("td", null, titleCase(e.failure_reason)));
    tr.appendChild(el("td", null, titleCase(e.merchant_category)));
    tr.appendChild(el("td", "status " + e.case_status, titleCase(e.case_status)));
    tr.appendChild(el("td", "muted", e.why_unrecovered));
    tbody.appendChild(tr);
  });
}


function renderRejected(items) {
  const card = document.getElementById("rejected-card");
  const tbody = document.getElementById("rejected-tbody");
  tbody.innerHTML = "";
  document.getElementById("rejected-count").textContent = "(" + items.length + ")";
  // Hide the whole panel when there's nothing rejected (e.g. before a run).
  card.classList.toggle("hidden", items.length === 0);
  items.forEach((e) => {
    const tr = el("tr");
    tr.appendChild(el("td", "num", maskId(e.customer_id)));
    tr.appendChild(el("td", null, e.raw_event_type || "\u2014"));
    tr.appendChild(el("td", "num", e.amount == null ? "\u2014" : rupees(e.amount)));
    tr.appendChild(el("td", "muted num", (e.event_timestamp || "").replace("T", " ")));
    const tdWhy = el("td");
    tdWhy.appendChild(el("span", "badge bad", "Invalid signature"));
    tr.appendChild(tdWhy);
    tbody.appendChild(tr);
  });
}


// --- Case detail drawer -----------------------------------------------------
function detailRow(k, v) {
  const wrap = document.createDocumentFragment();
  wrap.appendChild(el("div", "k", k));
  const vEl = typeof v === "string" ? el("div", "v", v) : v;
  if (typeof v !== "string") vEl.classList.add("v");
  wrap.appendChild(vEl);
  return wrap;
}

function renderMessages(msgs) {
  const wrap = el("div");
  const toggle = el("div", "msg-toggle");
  const stdBtn = el("button", "tab active", "Standard");
  const hinBtn = el("button", "tab", "Hinglish");
  toggle.appendChild(stdBtn);
  toggle.appendChild(hinBtn);
  const box = el("div", "msg-box");
  box.appendChild(el("div", "chan", "Channel: " + msgs.channel + " \u00B7 also available: " + msgs.channels_available.join(", ")));
  const text = el("div", null, msgs.standard);
  box.appendChild(text);
  stdBtn.addEventListener("click", () => { text.textContent = msgs.standard; stdBtn.classList.add("active"); hinBtn.classList.remove("active"); });
  hinBtn.addEventListener("click", () => { text.textContent = msgs.hinglish; hinBtn.classList.add("active"); stdBtn.classList.remove("active"); });
  wrap.appendChild(toggle);
  wrap.appendChild(box);
  return wrap;
}

function renderTimeline(audit) {
  const ul = el("ul", "timeline");
  audit.forEach((e) => {
    const cls = e.outcome === "success" ? "ok" : (e.outcome === "failure" ? "fail" : "");
    const li = el("li", cls);
    const head = el("div", "ev-head");
    head.appendChild(el("span", "ev-type", titleCase(e.event_type) +
      (e.attempt_number ? " \u00B7 attempt " + e.attempt_number : "")));
    if (e.outcome && e.outcome !== "n/a") {
      const badgeCls = e.outcome === "success" ? "ok" : (e.outcome === "failure" ? "bad" : "neutral");
      head.appendChild(el("span", "badge " + badgeCls + " ev-out", e.outcome));
    }
    li.appendChild(head);
    li.appendChild(el("div", "ev-action", e.action_taken));
    li.appendChild(el("div", "ev-why", e.reasoning_text));
    ul.appendChild(li);
  });
  return ul;
}


async function openDrawer(customerId) {
  const overlay = document.getElementById("drawer-overlay");
  const drawer = document.getElementById("drawer");
  const body = document.getElementById("drawer-body");
  body.innerHTML = "Loading\u2026";
  overlay.classList.remove("hidden");
  drawer.classList.remove("hidden");

  try {
    const data = await getJSON("/api/cases/" + encodeURIComponent(customerId) + "/audit");
    const c = data.case;
    document.getElementById("drawer-title").textContent = maskId(c.customer_id) + " \u00B7 " + titleCase(c.failure_reason);
    body.innerHTML = "";

    // Summary grid
    const grid = el("div", "detail-grid");
    grid.appendChild(detailRow("Recoverability score", scorePill(c.score)));
    grid.appendChild(detailRow("Amount", rupees(c.amount)));
    grid.appendChild(detailRow("Status", el("span", "status " + c.case_status, titleCase(c.case_status))));
    grid.appendChild(detailRow("Merchant category", titleCase(c.merchant_category)));
    const cb = complianceBadge(c.compliance_status);
    grid.appendChild(detailRow("RBI compliance", el("span", "badge " + cb.cls, cb.text)));
    grid.appendChild(detailRow("Dunning stage", dunningIndicator(c.dunning_stage || 0)));
    grid.appendChild(detailRow("Salary window",
      el("span", "badge " + (c.salary_window_inferred ? "v2" : "neutral"), c.salary_window_label)));
    grid.appendChild(detailRow("Subscription health",
      healthBadge(c.health_score, c.health_band)));
    grid.appendChild(detailRow("Triggered by", el("span", "tag reason", c.raw_event_type || "\u2014")));
    if (c.over_limit) {
      grid.appendChild(detailRow("Mandate limit",
        el("span", "badge warn", rupees(c.mandate_limit) + " (exceeded)")));
    } else {
      grid.appendChild(detailRow("Mandate limit", rupees(c.mandate_limit)));
    }
    body.appendChild(grid);

    // Generated messages (R9)
    body.appendChild(el("div", "section-title", "Nudge message (with Hinglish variant)"));
    body.appendChild(renderMessages(data.messages));

    // Audit trail / explainability (R4/R7)
    body.appendChild(el("div", "section-title", "Audit trail \u00B7 the agent's reasoning, step by step"));
    body.appendChild(renderTimeline(data.audit));
  } catch (err) {
    body.textContent = "Failed to load case: " + err.message;
  }
}

function closeDrawer() {
  document.getElementById("drawer-overlay").classList.add("hidden");
  document.getElementById("drawer").classList.add("hidden");
}


// --- Empty state ------------------------------------------------------------
function showEmptyState(show) {
  document.getElementById("empty-state").classList.toggle("hidden", !show);
  document.getElementById("dashboard").classList.toggle("hidden", show);
}

// --- Natural-language query -------------------------------------------------
function statusText(s) {
  return titleCase(s);
}

function renderAskResult(data) {
  const box = document.getElementById("ask-result");
  box.classList.remove("hidden");
  box.innerHTML = "";

  if (!data.ok) {
    const msg = el("div", "ask-empty", data.message || "Couldn't understand that one.");
    box.appendChild(msg);
    return;
  }

  const summary = el("div", "ask-summary");
  summary.appendChild(el("span", "ask-count", String(data.count)));
  summary.appendChild(el("span", null, " " + data.summary));
  box.appendChild(summary);

  // Show how the question was interpreted (transparency).
  const chips = el("div", "ask-filter-chips");
  Object.entries(data.filter || {}).forEach(([k, v]) => {
    chips.appendChild(el("span", "filter-chip", titleCase(k) + ": " + v));
  });
  if (chips.childNodes.length) box.appendChild(chips);

  if (data.count === 0) return;

  const wrap = el("div", "table-wrap");
  const table = el("table", "cases-table");
  const thead = el("thead");
  thead.innerHTML = "<tr><th>Score</th><th>Customer</th><th>Reason</th><th>Amount</th>" +
    "<th>Status</th><th>Compliance</th><th>Health</th></tr>";
  table.appendChild(thead);
  const tbody = el("tbody");
  data.results.forEach((c) => {
    const tr = el("tr");
    tr.addEventListener("click", () => openDrawer(c.customer_id));
    const tdScore = el("td");
    tdScore.appendChild(scorePill(c.score));
    tr.appendChild(tdScore);
    tr.appendChild(el("td", "num", maskId(c.customer_id)));
    const tdReason = el("td");
    tdReason.appendChild(el("span", "tag reason", titleCase(c.failure_reason)));
    tr.appendChild(tdReason);
    const tdAmt = el("td", "num", rupees(c.amount));
    if (c.over_limit) { tdAmt.appendChild(document.createTextNode(" ")); tdAmt.appendChild(el("span", "badge warn", "over limit")); }
    tr.appendChild(tdAmt);
    tr.appendChild(el("td", "status " + c.case_status, statusText(c.case_status)));
    const cb = complianceBadge(c.compliance_status);
    const tdComp = el("td"); tdComp.appendChild(el("span", "badge " + cb.cls, cb.text)); tr.appendChild(tdComp);
    const tdHealth = el("td");
    tdHealth.appendChild(el("span", "badge health-" + (c.health_band || "at-risk"), titleCase(c.health_band || "")));
    tr.appendChild(tdHealth);
    tbody.appendChild(tr);
  });
  table.appendChild(tbody);
  wrap.appendChild(table);
  box.appendChild(wrap);
}

async function runAsk(question) {
  const input = document.getElementById("ask-input");
  const btn = document.getElementById("ask-btn");
  const box = document.getElementById("ask-result");
  if (question) input.value = question;
  const q = input.value.trim();
  if (!q) return;

  btn.disabled = true;
  box.classList.remove("hidden");
  box.innerHTML = "";
  const spin = el("div", "ask-loading");
  spin.appendChild(el("span", "spinner"));
  spin.appendChild(el("span", null, "Interpreting your question\u2026"));
  box.appendChild(spin);

  try {
    const r = await fetch("/api/ask", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question: q }),
    });
    const data = await r.json();
    renderAskResult(data);
  } catch (err) {
    box.innerHTML = "";
    box.appendChild(el("div", "ask-empty", "Something went wrong. Try one of the examples."));
  } finally {
    btn.disabled = false;
  }
}


// --- Load + orchestration ---------------------------------------------------
async function loadDashboard() {
  const [metricsData, cases, cohorts, exceptions, rejected, mlMetrics, auditReport] = await Promise.all([
    getJSON("/api/metrics"),
    getJSON("/api/cases"),
    getJSON("/api/cohorts"),
    getJSON("/api/exceptions"),
    getJSON("/api/rejected-webhooks"),
    getJSON("/api/ml-metrics").catch(() => ({ available: false })),
    getJSON("/api/audit-check").catch(() => null),
  ]);
  renderMetrics(metricsData);
  renderMlPanel(mlMetrics);
  renderAuditPanel(auditReport);
  cohortData = cohorts;
  renderCohorts();
  renderCases(cases);
  renderExceptions(exceptions);
  renderRejected(rejected);
}

// Live pipeline run via Server-Sent Events, with visual pacing per case.
function sleep(ms) { return new Promise((res) => setTimeout(res, ms)); }

function resetLiveCounters(total) {
  document.getElementById("lc-processed").textContent = "0";
  document.getElementById("lc-recovered").textContent = "0";
  document.getElementById("lc-escalated").textContent = "0";
  document.getElementById("lc-count").textContent = "0 / " + (total || "?");
  document.getElementById("lc-fill").style.width = "0%";
  document.getElementById("live-feed").innerHTML = "";
}

function feedCard(trace) {
  const card = el("div", "feed-card");
  const statusCls = trace.final_status === "recovered" ? "ok"
    : (trace.final_status === "escalated" || trace.final_status === "broken_promise" ? "bad" : "");
  card.classList.add(statusCls);
  const head = el("div", "fc-head");
  head.appendChild(el("span", "fc-id", maskId(trace.customer_id)));
  head.appendChild(el("span", "fc-amt", rupees(trace.amount)));
  card.appendChild(head);
  const flow = el("div", "fc-flow");
  flow.appendChild(el("span", "fc-step", titleCase(trace.diagnosis)));
  flow.appendChild(el("span", "fc-arrow", "\u2192"));
  flow.appendChild(el("span", "fc-step", "score " + trace.score));
  flow.appendChild(el("span", "fc-arrow", "\u2192"));
  flow.appendChild(el("span", "fc-step", trace.strategy));
  flow.appendChild(el("span", "fc-arrow", "\u2192"));
  flow.appendChild(el("span", "fc-step " + statusCls, statusText(trace.final_status)));
  card.appendChild(flow);
  return card;
}

// --- Four-agent pipeline visualization -------------------------------------
const PIPELINE_STAGES = ["diagnosis", "triage", "strategy", "communication"];
const prefersReducedMotion =
  window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

function resetPipeline() {
  document.querySelectorAll("#pipeline .pnode").forEach((node) => {
    node.classList.remove("active", "pulsing");
    const c = node.querySelector(".pnode-count");
    if (c) c.textContent = "0";
  });
}

// Mark stages up to (and including) the current one as active; pulse the current.
function setPipelineStage(stageIndex) {
  const nodes = document.querySelectorAll("#pipeline .pnode");
  nodes.forEach((node, i) => {
    node.classList.toggle("active", i <= stageIndex);
    node.classList.toggle("pulsing", i === stageIndex && !prefersReducedMotion);
  });
}

function bumpStageCount(stage) {
  const c = document.querySelector('#pipeline .pnode-count[data-count="' + stage + '"]');
  if (c) c.textContent = String((parseInt(c.textContent, 10) || 0) + 1);
}

// Sweep the blue pulse across all four nodes once, incrementing each stage's counter.
// With reduced motion the sweep is instantaneous.
async function animateCaseThroughPipeline(queueLen) {
  // Sweep faster as the backlog grows so a 180-case run still finishes quickly.
  const stepMs = prefersReducedMotion ? 0 : (queueLen > 30 ? 12 : (queueLen > 10 ? 35 : 70));
  for (let i = 0; i < PIPELINE_STAGES.length; i++) {
    setPipelineStage(i);
    bumpStageCount(PIPELINE_STAGES[i]);
    if (stepMs) await sleep(stepMs);
  }
}

async function runAgentLive() {
  const runBtn = document.getElementById("btn-run");
  const resetBtn = document.getElementById("btn-reset");
  runBtn.disabled = true;
  resetBtn.disabled = true;
  document.getElementById("run-complete").classList.add("hidden");

  const status = await getJSON("/api/status");
  if (!status.seeded) {
    banner("No data to run yet. Click \u201CReset demo\u201D to seed cases first.", true);
    runBtn.disabled = false; resetBtn.disabled = false;
    return;
  }

  const live = document.getElementById("live-panel");
  live.classList.remove("hidden");
  resetLiveCounters(status.total_cases);
  resetPipeline();
  banner("Running the four-agent pipeline\u2026");

  const feed = document.getElementById("live-feed");
  let processed = 0, recovered = 0, escalated = 0;
  const total = status.total_cases;

  // Queue of traces arriving from the stream; drained with visual pacing.
  const queue = [];
  let streamDone = false;
  let finalSummary = null;

  const resp = await fetch("/api/run-agent-stream");
  const reader = resp.body.getReader();
  const decoder = new TextDecoder();
  let buf = "";

  // Reader loop: fill the queue as fast as the server streams.
  (async () => {
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      let idx;
      while ((idx = buf.indexOf("\n\n")) !== -1) {
        const line = buf.slice(0, idx).trim();
        buf = buf.slice(idx + 2);
        if (line.startsWith("data:")) {
          const obj = JSON.parse(line.slice(5).trim());
          if (obj.done) finalSummary = obj; else queue.push(obj);
        }
      }
    }
    streamDone = true;
  })();

  // Drain loop: render one card per tick with pacing for the "watching it work" feel.
  // Pace speeds up if the queue grows so a 180-case run still finishes quickly.
  while (!streamDone || queue.length) {
    if (!queue.length) { await sleep(30); continue; }
    const trace = queue.shift();

    // Signature moment: sweep the blue pulse across Diagnosis → Triage →
    // Strategy → Communication and tick each stage's live counter.
    await animateCaseThroughPipeline(queue.length);

    const card = feedCard(trace);
    feed.insertBefore(card, feed.firstChild);
    while (feed.childNodes.length > 40) feed.removeChild(feed.lastChild);

    processed += 1;
    if (trace.final_status === "recovered") recovered += 1;
    else if (trace.final_status === "escalated" || trace.final_status === "broken_promise") escalated += 1;
    document.getElementById("lc-processed").textContent = processed;
    document.getElementById("lc-recovered").textContent = recovered;
    document.getElementById("lc-escalated").textContent = escalated;
    document.getElementById("lc-count").textContent = processed + " / " + total;
    document.getElementById("lc-fill").style.width = ((processed / total) * 100).toFixed(1) + "%";

    // The pipeline sweep already provides pacing, so keep the trailing pause
    // short and let it shrink further when the queue backs up.
    const delay = queue.length > 30 ? 0 : (queue.length > 10 ? 40 : 120);
    await sleep(delay);
  }

  // Leave all four nodes lit but stop pulsing once the run finishes.
  document.querySelectorAll("#pipeline .pnode").forEach((n) => {
    n.classList.remove("pulsing");
    n.classList.add("active");
  });

  await loadDashboard();
  clearBanner();
  live.classList.add("hidden");
  showRunComplete(finalSummary);
  runBtn.disabled = false;
  resetBtn.disabled = false;
}

function showRunComplete(summary) {
  if (!summary) return;
  const counts = summary.status_counts || {};
  const processed = summary.processed || 0;
  const rec = counts.recovered || 0;
  const esc = counts.escalated || 0;
  document.getElementById("rc-processed").textContent = processed;
  document.getElementById("rc-recovered").textContent = rec;
  document.getElementById("rc-escalated").textContent = esc;
  document.getElementById("rc-recovery-rate").textContent =
    processed ? ((rec / processed) * 100).toFixed(1) + "%" : "\u2014";
  document.getElementById("run-complete").classList.remove("hidden");
}

async function resetDemo() {
  const resetBtn = document.getElementById("btn-reset");
  const runBtn = document.getElementById("btn-run");
  resetBtn.disabled = true; runBtn.disabled = true;
  try {
    banner("Resetting \u2014 re-seeding fresh data\u2026");
    await postJSON("/api/reset");
    document.getElementById("run-complete").classList.add("hidden");
    document.getElementById("live-panel").classList.add("hidden");
    document.getElementById("ask-result").classList.add("hidden");
    showEmptyState(false);
    await loadDashboard();
    banner("Fresh data seeded. Click \u201CRun agent\u201D to watch the pipeline work.");
  } catch (err) {
    banner(err.message, true);
  } finally {
    resetBtn.disabled = false; runBtn.disabled = false;
  }
}


// --- Init -------------------------------------------------------------------
document.addEventListener("DOMContentLoaded", () => {
  document.getElementById("btn-reset").addEventListener("click", resetDemo);
  document.getElementById("btn-run").addEventListener("click", runAgentLive);
  document.getElementById("btn-seed-empty").addEventListener("click", resetDemo);
  document.getElementById("rc-dismiss").addEventListener("click", () =>
    document.getElementById("run-complete").classList.add("hidden"));
  document.getElementById("drawer-close").addEventListener("click", closeDrawer);
  document.getElementById("drawer-overlay").addEventListener("click", closeDrawer);
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeDrawer(); });

  // Ask feature
  document.getElementById("ask-btn").addEventListener("click", () => runAsk());
  document.getElementById("ask-input").addEventListener("keydown", (e) => {
    if (e.key === "Enter") runAsk();
  });
  document.querySelectorAll("#ask-chips .chip").forEach((chip) => {
    chip.addEventListener("click", () => runAsk(chip.dataset.q));
  });

  document.querySelectorAll(".cohort-tabs .tab").forEach((tab) => {
    tab.addEventListener("click", () => {
      document.querySelectorAll(".cohort-tabs .tab").forEach((t) => t.classList.remove("active"));
      tab.classList.add("active");
      activeCohort = tab.dataset.cohort;
      renderCohorts();
    });
  });

  // Decide between empty state and dashboard on load.
  getJSON("/api/status").then((status) => {
    if (!status.seeded) {
      showEmptyState(true);
      return;
    }
    showEmptyState(false);
    return loadDashboard();
  }).catch((err) => {
    banner("Could not load data yet. Click \u201CReset demo\u201D to generate cases. (" + err.message + ")", true);
  });
});
