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

// --- Internal API key bootstrap ---------------------------------------------
// Mutating endpoints (/api/reset, /api/seed, /api/run-agent-stream, /api/simulate)
// require an X-API-Key header (see backend/security.py) so an unauthenticated
// third party can't wipe/reseed the demo data or spend simulation compute. The
// dashboard fetches the current key once, same-origin, at page load — this is not
// itself a security boundary (anyone with same-origin JS access already has full
// UI access), it just keeps normal dashboard use working without a login step.
let _apiKey = null;
async function fetchApiKey() {
  try {
    const r = await fetch("/api/_client-key");
    const data = await r.json();
    _apiKey = data.api_key;
  } catch (err) {
    console.warn("Could not fetch API key; mutating actions will fail until this succeeds.", err);
  }
}

async function getJSON(url) {
  const r = await fetch(url, {
    headers: _apiKey ? { "X-API-Key": _apiKey } : {},
  });
  if (!r.ok) throw new Error("Request failed: " + url + " (" + r.status + ")");
  return r.json();
}
async function postJSON(url) {
  const r = await fetch(url, {
    method: "POST",
    headers: _apiKey ? { "X-API-Key": _apiKey } : {},
  });
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
let shapImportanceChart = null;

// --- Persisted UI state (localStorage) --------------------------------------
// Starred cases and saved "Ask the data" views used to be session-only (an
// in-memory Set/array, lost on refresh). They're now persisted to localStorage so
// they survive a page reload — genuinely useful for a judge/demo who navigates
// away and comes back, not just a cosmetic change. Purely client-side UI state;
// never sent to or read from the backend.
const LS_STARRED_KEY = "mandateRescue.starredCases";
const LS_SAVED_VIEWS_KEY = "mandateRescue.savedViews";

function loadPersistedUiState() {
  try {
    const rawStarred = localStorage.getItem(LS_STARRED_KEY);
    if (rawStarred) {
      JSON.parse(rawStarred).forEach((id) => starredCases.add(id));
      updateStarredCount();
    }
  } catch (err) {
    console.warn("Could not load persisted starred cases:", err);
  }
  try {
    const rawViews = localStorage.getItem(LS_SAVED_VIEWS_KEY);
    if (rawViews) {
      const parsed = JSON.parse(rawViews);
      if (Array.isArray(parsed)) {
        savedViews.length = 0;
        savedViews.push(...parsed);
        renderSavedViews();
      }
    }
  } catch (err) {
    console.warn("Could not load persisted saved views:", err);
  }
}

function persistStarredCases() {
  try {
    localStorage.setItem(LS_STARRED_KEY, JSON.stringify(Array.from(starredCases)));
  } catch (err) {
    console.warn("Could not persist starred cases:", err);
  }
}

function persistSavedViews() {
  try {
    localStorage.setItem(LS_SAVED_VIEWS_KEY, JSON.stringify(savedViews));
  } catch (err) {
    console.warn("Could not persist saved views:", err);
  }
}

// Starring: set of starred customer ids, persisted to localStorage (see above). The
// full loaded case list is cached so the All/Starred filter can re-render instantly
// without a refetch. activeCasesFilter is "all" | "starred".
const starredCases = new Set();
let allCasesCache = [];
let activeCasesFilter = "all";

// Saved views: list of { name, question, filter } saved from successful "Ask the
// data" queries, persisted to localStorage. Rendered as clickable chips that
// re-run the stored question. lastAskQuestion/lastAskData hold the most recent
// result so the "Save this view" button knows what to store.
const savedViews = [];
let lastAskQuestion = "";
let lastAskFilter = null;

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
  const dumb = data.dumb_persistence;
  if (!hasRun) {
    upliftNote.innerHTML = "Run the agent to see this comparison.";
  } else {
    const diff = a.amount_recovered - b.amount_recovered;
    // Never show a negative number in the sentence: choose the word, show |diff|.
    const word = diff >= 0 ? "more" : "less";
    let html =
      "The agent recovered <b>" + rupees(Math.abs(diff)) + " " + word + "</b> than the naive baseline (" +
      rupees(a.amount_recovered) + " vs " + rupees(b.amount_recovered) + ").";
    if (dumb) {
      // The sharper, defensible claim: how much the agent's actual strategy (scoring,
      // salary-window timing, staged dunning, promise handling) adds BEYOND simply
      // retrying more times at the same budget. See baseline.py's module docstring.
      const diff2 = a.amount_recovered - dumb.amount_recovered;
      const word2 = diff2 >= 0 ? "more" : "less";
      html += " Of that, <b>" + rupees(Math.abs(diff2)) + " " + word2 + "</b> is specifically " +
        "from the agent's scoring/timing/dunning strategy \u2014 a \u201Cdumb persistence\u201D " +
        "baseline using the SAME " + dumb.retry_cap + "-attempt budget but no strategy at all " +
        "only recovered " + rupees(dumb.amount_recovered) + ".";
    }
    upliftNote.innerHTML = html;
  }

  const ctx = document.getElementById("baseline-chart").getContext("2d");
  if (baselineChart) baselineChart.destroy();
  const labels = dumb
    ? ["Naive baseline\n(1 attempt)", "Dumb persistence\n(" + dumb.retry_cap + " attempts, no strategy)", "Mandate Rescue agent"]
    : ["Naive baseline", "Mandate Rescue agent"];
  const values = dumb
    ? [b.amount_recovered, dumb.amount_recovered, a.amount_recovered]
    : [b.amount_recovered, a.amount_recovered];
  const colors = dumb
    ? ["#D5DAE4", "#8FA3C4", "#0E9F6E"]
    : ["#D5DAE4", "#0E9F6E"];
  baselineChart = new Chart(ctx, {
    type: "bar",
    data: {
      labels: labels,
      datasets: [{
        label: "Amount recovered",
        data: values,
        backgroundColor: colors,
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

// --- Global SHAP feature importance (ML panel) ------------------------------
// Real SHAP values from /api/ml-feature-importance: mean absolute SHAP per feature
// across the held-out test set — which factors the model weighs most overall.
// Interpretation only; consistent with the "does not drive agent decisions" framing.
function renderShapImportance(data) {
  const box = document.getElementById("ml-shap-importance");
  if (!box) return;
  if (!data || !data.available || !Array.isArray(data.importance) || !data.importance.length) {
    box.classList.add("hidden");
    if (shapImportanceChart) { shapImportanceChart.destroy(); shapImportanceChart = null; }
    return;
  }
  box.classList.remove("hidden");

  document.getElementById("ml-shap-title").innerHTML =
    "Feature importance \u00B7 mean |SHAP| \u00B7 " + (data.explainer || "SHAP");
  document.getElementById("ml-shap-cap").textContent =
    "How much each feature moves the model's recovery prediction on average (mean " +
    "absolute SHAP value, log-odds space) across the held-out test set. Larger = more " +
    "influential to the model overall. This explains a non-decision prediction — it " +
    "does not drive any agent, scoring, or compliance decision.";

  // Sorted descending already; show as a horizontal bar chart.
  const labels = data.importance.map((d) => d.label || d.feature);
  const values = data.importance.map((d) => d.mean_abs_shap);
  const ctx = document.getElementById("ml-shap-chart").getContext("2d");
  if (shapImportanceChart) shapImportanceChart.destroy();
  shapImportanceChart = new Chart(ctx, {
    type: "bar",
    data: {
      labels: labels,
      datasets: [{
        label: "mean |SHAP|",
        data: values,
        backgroundColor: "#3E5FF5",
        borderRadius: 6,
        maxBarThickness: 26,
      }],
    },
    options: {
      indexAxis: "y",
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: { display: false },
        tooltip: { callbacks: { label: (c) => "mean |SHAP| " + Number(c.raw).toFixed(4) } },
      },
      scales: { x: { beginAtZero: true } },
    },
  });
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

// Real Razorpay-sourced cases (via the verified /api/webhooks/razorpay intake) get
// a distinct badge from the synthetic seeded demo data, so a viewer can visibly
// tell the two apart. Purely informational — never affects scoring/strategy.
function sourceBadge(source) {
  if (source === "razorpay_live") {
    return el("span", "badge razorpay-live", "\u26A1 Razorpay live");
  }
  return null;
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


// Star toggle cell for a case row. Clicking toggles the star without opening the
// drawer (stops propagation). Reflects the current starred state via a filled/hollow
// star and an "on" class for coloring.
function starCell(customerId) {
  const td = el("td", "td-star");
  const btn = el("button", "star-btn", "\u2606"); // hollow star by default
  const on = starredCases.has(customerId);
  if (on) { btn.textContent = "\u2605"; btn.classList.add("on"); }
  btn.setAttribute("aria-label", on ? "Unstar this case" : "Star this case");
  btn.setAttribute("aria-pressed", on ? "true" : "false");
  btn.title = on ? "Starred — click to remove" : "Star this case";
  btn.addEventListener("click", (e) => {
    e.stopPropagation(); // don't open the drawer
    toggleStar(customerId);
  });
  td.appendChild(btn);
  return td;
}

function toggleStar(customerId) {
  if (starredCases.has(customerId)) starredCases.delete(customerId);
  else starredCases.add(customerId);
  updateStarredCount();
  persistStarredCases();
  // Re-render the current view so the star state (and the Starred filter) update.
  renderCasesFiltered();
}

function updateStarredCount() {
  const el2 = document.getElementById("starred-count");
  if (el2) el2.textContent = String(starredCases.size);
}

// Entry point called after data loads: cache the full list, then render the view.
function renderCases(cases) {
  allCasesCache = Array.isArray(cases) ? cases : [];
  // Drop stars for cases that no longer exist (e.g. after a reset re-seed).
  const ids = new Set(allCasesCache.map((c) => c.customer_id));
  let pruned = false;
  Array.from(starredCases).forEach((id) => {
    if (!ids.has(id)) { starredCases.delete(id); pruned = true; }
  });
  if (pruned) persistStarredCases();
  updateStarredCount();
  renderCasesFiltered();
  renderRazorpayLiveCard(allCasesCache);
}

// Shows the "real Razorpay webhook intake" card only once at least one case with
// source === 'razorpay_live' exists (i.e. a real webhook was actually received).
function renderRazorpayLiveCard(cases) {
  const card = document.getElementById("razorpay-live-card");
  if (!card) return;
  const liveCases = cases.filter((c) => c.source === "razorpay_live");
  card.classList.toggle("hidden", liveCases.length === 0);
  const countEl = document.getElementById("rzp-live-count");
  if (countEl) countEl.textContent = String(liveCases.length);
}

// Render the cases table honoring the active All/Starred filter (no refetch).
function renderCasesFiltered() {
  const tbody = document.getElementById("cases-tbody");
  const emptyStarred = document.getElementById("cases-empty-starred");
  const cases = activeCasesFilter === "starred"
    ? allCasesCache.filter((c) => starredCases.has(c.customer_id))
    : allCasesCache;

  tbody.innerHTML = "";
  document.getElementById("cases-count").textContent = "(" + cases.length + ")";

  // Show a friendly hint when the Starred filter is empty.
  if (emptyStarred) {
    emptyStarred.classList.toggle("hidden",
      !(activeCasesFilter === "starred" && cases.length === 0));
  }

  cases.forEach((c) => {
    const tr = el("tr");
    tr.addEventListener("click", () => openDrawer(c.customer_id));

    tr.appendChild(starCell(c.customer_id));

    const tdScore = el("td");
    tdScore.appendChild(scorePill(c.score));
    tr.appendChild(tdScore);

    const tdMl = el("td");
    tdMl.appendChild(mlProbPill(c.ml_recovery_probability));
    tr.appendChild(tdMl);

    const tdId = el("td", "num", maskId(c.customer_id));
    const srcBadge = sourceBadge(c.source);
    if (srcBadge) { tdId.appendChild(document.createElement("br")); tdId.appendChild(srcBadge); }
    tr.appendChild(tdId);

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


// --- Per-case SHAP explanation (drawer) -------------------------------------
// Simplified SHAP force-plot: each top factor is a horizontal bar from a center
// zero-line. Positive contributions (pushed toward "recovered") extend RIGHT in
// teal; negative (toward "not recovered") extend LEFT in red. Bar length is scaled
// to the largest absolute contribution among the shown factors.
function renderShapExplanation(explanation) {
  const wrap = el("div", "shap-explain");

  const prob = explanation.predicted_probability;
  const head = el("div", "shap-head");
  head.appendChild(el("span", "shap-prob-label", "Model P(recover)"));
  head.appendChild(el("span", "shap-prob-value num", (prob * 100).toFixed(1) + "%"));
  wrap.appendChild(head);

  const factors = explanation.top_factors || [];
  const maxAbs = factors.reduce((m, f) => Math.max(m, Math.abs(f.impact)), 0) || 1;

  const bars = el("div", "shap-bars");
  factors.forEach((f) => {
    const positive = f.impact >= 0;
    const row = el("div", "shap-row");

    // Label + the case's actual value for this feature.
    const label = el("div", "shap-label");
    label.appendChild(el("span", "shap-feat", f.label || f.feature));
    label.appendChild(el("span", "shap-val", String(f.value)));
    row.appendChild(label);

    // Track with a center zero-line; bar grows left (neg) or right (pos).
    const track = el("div", "shap-track");
    const bar = el("div", "shap-bar " + (positive ? "pos" : "neg"));
    const widthPct = (Math.abs(f.impact) / maxAbs) * 50; // up to 50% each side
    bar.style.width = widthPct.toFixed(1) + "%";
    if (positive) { bar.style.left = "50%"; } else { bar.style.right = "50%"; }
    track.appendChild(bar);
    const zero = el("div", "shap-zero");
    track.appendChild(zero);
    row.appendChild(track);

    // Signed impact value.
    const imp = el("div", "shap-impact " + (positive ? "pos" : "neg"),
      (positive ? "+" : "\u2212") + Math.abs(f.impact).toFixed(2));
    row.appendChild(imp);

    bars.appendChild(row);
  });
  wrap.appendChild(bars);

  const legend = el("div", "shap-legend");
  legend.appendChild(el("span", "shap-legend-item pos", "\u25B6 pushes toward recovered"));
  legend.appendChild(el("span", "shap-legend-item neg", "\u25C0 pushes toward not recovered"));
  wrap.appendChild(legend);

  wrap.appendChild(el("div", "shap-note",
    "Real SHAP values from the ML validation layer (log-odds contributions; base " +
    explanation.base_value.toFixed(2) + " + all contributions reconstruct this " +
    "probability exactly). This explains the model's non-decision prediction and does " +
    "not drive any agent, scoring, or compliance decision."));
  return wrap;
}

async function openDrawer(customerId) {
  const overlay = document.getElementById("drawer-overlay");
  const drawer = document.getElementById("drawer");
  const body = document.getElementById("drawer-body");
  body.innerHTML = "Loading\u2026";
  overlay.classList.remove("hidden");
  drawer.classList.remove("hidden");

  try {
    // Load audit data and recovery jobs in parallel (Phase 4).
    const [data, jobsData] = await Promise.all([
      getJSON("/api/cases/" + encodeURIComponent(customerId) + "/audit"),
      getJSON("/api/cases/" + encodeURIComponent(customerId) + "/jobs").catch(() => ({ jobs: [] })),
    ]);
    const c = data.case;
    document.getElementById("drawer-title").textContent =
      maskId(c.customer_id) + " \u00B7 " + titleCase(c.failure_reason);
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

    // Phase 4: Recovery execution panel (real jobs with mode / outcome / Razorpay IDs).
    const jobs = (jobsData && jobsData.jobs) || [];
    if (jobs.length > 0) {
      body.appendChild(el("div", "section-title", "Recovery execution"));
      body.appendChild(renderExecutionPanel(jobs));
    }

    // Generated messages (R9)
    body.appendChild(el("div", "section-title", "Nudge message (with Hinglish variant)"));
    body.appendChild(renderMessages(data.messages));

    // Why the model predicts this (SHAP) — ML validation layer, additive/non-decision.
    const shapTitle = el("div", "section-title", "Why the model predicts this");
    shapTitle.appendChild(el("span", "section-tag", "ML validation layer"));
    body.appendChild(shapTitle);
    const shapSlot = el("div", "shap-slot");
    shapSlot.appendChild(el("div", "muted", "Computing SHAP explanation\u2026"));
    body.appendChild(shapSlot);
    getJSON("/api/cases/" + encodeURIComponent(customerId) + "/explain")
      .then((ex) => {
        shapSlot.innerHTML = "";
        if (ex && ex.available && ex.explanation) {
          shapSlot.appendChild(renderShapExplanation(ex.explanation));
        } else {
          shapSlot.appendChild(el("div", "muted",
            (ex && ex.message) || "SHAP explanation unavailable (model not trained)."));
        }
      })
      .catch(() => {
        shapSlot.innerHTML = "";
        shapSlot.appendChild(el("div", "muted", "Could not load the SHAP explanation."));
      });

    // Audit trail / explainability (R4/R7)
    body.appendChild(el("div", "section-title", "Audit trail \u00B7 the agent\u2019s reasoning, step by step"));
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

  // Remember this successful query so "Save this view" can store it.
  lastAskQuestion = data.question || "";
  lastAskFilter = data.filter || {};

  const summary = el("div", "ask-summary");
  summary.appendChild(el("span", "ask-count", String(data.count)));
  summary.appendChild(el("span", null, " " + data.summary));

  // "Save this view" — stores the query text + resulting filter as a re-runnable
  // chip. Session-only; disabled once this exact query is already saved.
  const saveBtn = el("button", "btn btn-ghost btn-sm ask-save-btn");
  const alreadySaved = savedViews.some((v) => v.question === lastAskQuestion);
  saveBtn.innerHTML = alreadySaved ? "\u2713 Saved" : "\u2605 Save this view";
  saveBtn.disabled = alreadySaved;
  saveBtn.title = alreadySaved
    ? "This query is already saved above"
    : "Save this query as a chip you can re-run instantly";
  saveBtn.addEventListener("click", () => {
    saveCurrentView();
    saveBtn.innerHTML = "\u2713 Saved";
    saveBtn.disabled = true;
  });
  summary.appendChild(saveBtn);
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


// --- Saved views (Phase 3) --------------------------------------------------
// Store the most recent successful query as a re-runnable chip. Session-only.
function shortLabel(question) {
  const q = String(question || "").trim();
  if (q.length <= 34) return q;
  return q.slice(0, 32).trimEnd() + "\u2026";
}

function saveCurrentView() {
  const q = (lastAskQuestion || "").trim();
  if (!q) return;
  if (savedViews.some((v) => v.question === q)) return; // de-dupe
  savedViews.push({ name: shortLabel(q), question: q, filter: lastAskFilter || {} });
  renderSavedViews();
  persistSavedViews();
}

function removeSavedView(question) {
  const i = savedViews.findIndex((v) => v.question === question);
  if (i !== -1) savedViews.splice(i, 1);
  renderSavedViews();
  persistSavedViews();
}

function renderSavedViews() {
  const row = document.getElementById("saved-views-row");
  const chips = document.getElementById("saved-views-chips");
  if (!row || !chips) return;
  chips.innerHTML = "";
  row.classList.toggle("hidden", savedViews.length === 0);

  savedViews.forEach((v) => {
    const chip = el("span", "saved-chip");
    const label = el("button", "saved-chip-label", v.name);
    label.title = "Re-run: " + v.question;
    label.addEventListener("click", () => runAsk(v.question));
    chip.appendChild(label);
    const rm = el("button", "saved-chip-remove", "\u00D7");
    rm.setAttribute("aria-label", "Remove saved view");
    rm.title = "Remove this saved view";
    rm.addEventListener("click", (e) => { e.stopPropagation(); removeSavedView(v.question); });
    chip.appendChild(rm);
    chips.appendChild(chip);
  });
}


// --- Cases All/Starred filter (Phase 3) -------------------------------------
function initCasesTabs() {
  document.querySelectorAll("#cases-tabs .tab").forEach((tab) => {
    tab.addEventListener("click", () => {
      document.querySelectorAll("#cases-tabs .tab").forEach((t) => t.classList.remove("active"));
      tab.classList.add("active");
      activeCasesFilter = tab.dataset.casesFilter;
      renderCasesFiltered();
    });
  });
}


// --- Update Run button label based on run state ----------------------------
function updateRunButtonLabel(metricsData) {
  const btn = document.getElementById("btn-run");
  if (!btn) return;
  const hasRun = metricsData && metricsData.agent &&
    ((metricsData.agent.recovered_cases || 0) > 0 ||
     (metricsData.agent.escalated_cases || 0) > 0);
  // Find the text node (skip SVG child)
  Array.from(btn.childNodes).forEach(n => {
    if (n.nodeType === Node.TEXT_NODE && n.textContent.trim().length > 0) {
      n.textContent = hasRun ? " Re-run agent" : " Run agent";
    }
  });
  btn.title = hasRun
    ? "Re-seed fresh data and run the recovery agent again"
    : "Run the recovery agent over all seeded cases";
}

// --- Load + orchestration ---------------------------------------------------
async function loadDashboard() {
  const [metricsData, cases, cohorts, exceptions, rejected, mlMetrics, auditReport, shapImportance] = await Promise.all([
    getJSON("/api/metrics"),
    getJSON("/api/cases"),
    getJSON("/api/cohorts"),
    getJSON("/api/exceptions"),
    getJSON("/api/rejected-webhooks"),
    getJSON("/api/ml-metrics").catch(() => ({ available: false })),
    getJSON("/api/audit-check").catch(() => null),
    getJSON("/api/ml-feature-importance").catch(() => ({ available: false })),
  ]);
  renderMetrics(metricsData);
  renderMlPanel(mlMetrics);
  renderShapImportance(shapImportance);
  renderAuditPanel(auditReport);
  cohortData = cohorts;
  renderCohorts();
  renderCases(cases);
  renderExceptions(exceptions);
  renderRejected(rejected);
  // Update Run button label — "Re-run agent" when a run already happened
  updateRunButtonLabel(metricsData);
  // Refresh per-view empty hints now that panels have (un)hidden themselves.
  if (typeof syncViewEmptyStates === "function") syncViewEmptyStates();
  // Keep the command palette's case list in sync with the freshly loaded data.
  cmdkCases = Array.isArray(cases) ? cases : [];
  cmdkCasesLoaded = true;
  // Refresh the Activity feed with the latest audit events.
  if (typeof loadActivity === "function") loadActivity();

  // Dispatch event so additive modules (funnel, webhook inspector) can render.
  try {
    const activityData = await getJSON("/api/activity").catch(() => null);
    document.dispatchEvent(new CustomEvent("mandateRescueDashboardLoaded", {
      detail: { metricsData, cases, activityData }
    }));
  } catch (_) {}
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
  const cidSpan = el("span", "fc-id", maskId(trace.customer_id));
  cidSpan.dataset.cid = trace.customer_id || "";
  const statusSpan = el("span", "fc-amt", rupees(trace.amount));
  statusSpan.dataset.finalStatus = trace.final_status || "";
  head.appendChild(cidSpan);
  head.appendChild(statusSpan);
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

  // If the agent has already run on this data, auto-reset first so cases aren't
  // all skipped as duplicates (which makes the pipeline appear stuck at 0).
  if (status.has_run) {
    banner("Re-seeding fresh data before run\u2026");
    try {
      await postJSON("/api/reset");
      document.getElementById("run-complete").classList.add("hidden");
      // Reset button label to "Run agent" during the run
      Array.from(runBtn.childNodes).forEach(n => {
        if (n.nodeType === Node.TEXT_NODE && n.textContent.trim().length > 0) {
          n.textContent = " Run agent";
        }
      });
    } catch (err) {
      banner("Could not reset: " + err.message, true);
      runBtn.disabled = false; resetBtn.disabled = false;
      return;
    }
    // Re-fetch status with the fresh seed count
    const freshStatus = await getJSON("/api/status");
    status.total_cases = freshStatus.total_cases;
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
  let streamError = null;

  let resp;
  try {
    // Obtain a one-use SSE token (gated by X-API-Key) before opening the stream.
    // The browser EventSource API cannot send custom headers, so the stream URL
    // carries a short-lived token instead of the master API key.
    const tokenResp = await fetch("/api/run-agent-stream-token", {
      method: "POST",
      headers: _apiKey ? { "X-API-Key": _apiKey } : {},
    });
    if (!tokenResp.ok) {
      banner("Authentication failed — check your API key.", true);
      live.classList.add("hidden");
      runBtn.disabled = false; resetBtn.disabled = false;
      return;
    }
    const { token } = await tokenResp.json();
    resp = await fetch(`/api/run-agent-stream?token=${encodeURIComponent(token)}`);
  } catch (err) {
    // The run request itself never connected. Surface it and re-enable the buttons
    // rather than leaving the UI stuck on "Running…".
    banner("Could not start the run: " + err.message, true);
    live.classList.add("hidden");
    runBtn.disabled = false; resetBtn.disabled = false;
    return;
  }
  const reader = resp.body.getReader();
  const decoder = new TextDecoder();
  let buf = "";

  // Reader loop: fill the queue as fast as the server streams.
  // CRITICAL: this must ALWAYS set streamDone=true when it ends — success, network
  // error, or a stream cut short (e.g. the dev server restarting mid-run). Previously
  // an error here threw out of the un-awaited async IIFE, streamDone stayed false, and
  // the drain loop below spun forever on `while (!streamDone || queue.length)` — which
  // is exactly why the pipeline appeared to freeze partway. The try/finally guarantees
  // the drain loop can always terminate.
  (async () => {
    try {
      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        buf += decoder.decode(value, { stream: true });
        let idx;
        while ((idx = buf.indexOf("\n\n")) !== -1) {
          const line = buf.slice(0, idx).trim();
          buf = buf.slice(idx + 2);
          if (line.startsWith("data:")) {
            let obj;
            try {
              obj = JSON.parse(line.slice(5).trim());
            } catch (parseErr) {
              // A truncated final chunk can leave an unparseable line; skip it rather
              // than aborting the whole run.
              continue;
            }
            if (obj.done) finalSummary = obj; else queue.push(obj);
          }
        }
      }
    } catch (err) {
      // Stream was interrupted (e.g. connection reset). Record it; the drain loop
      // will finish rendering whatever it already has and then reconcile the final
      // totals from /api/metrics so the run still visibly completes.
      streamError = err;
    } finally {
      streamDone = true;
    }
  })();

  // Drain loop: render one card per tick with pacing for the "watching it work" feel.
  // Pace speeds up if the queue grows so a 180-case run still finishes quickly.
  // The try/catch inside the loop ensures a per-case render error (e.g. a null DOM
  // element, an unexpected trace shape) never kills the whole run — we skip the
  // bad case and keep going rather than leaving the UI frozen mid-run.
  while (!streamDone || queue.length) {
    if (!queue.length) { await sleep(30); continue; }
    const trace = queue.shift();

    try {
      // Signature moment: sweep the blue pulse across Diagnosis → Triage →
      // Strategy → Communication and tick each stage's live counter.
      await animateCaseThroughPipeline(queue.length);

      const card = feedCard(trace);
      feed.insertBefore(card, feed.firstChild);
      while (feed.childNodes.length > 40) feed.removeChild(feed.lastChild);

      processed += 1;
      if (trace.final_status === "recovered") recovered += 1;
      else if (trace.final_status === "escalated" || trace.final_status === "broken_promise") escalated += 1;

      // Update the live recovery widget directly from the trace (avoids MutationObserver race).
      updateLiveRecoveryFromTrace(trace);

      // Update counters only when value changes to avoid spurious MutationObserver
      // callbacks on elements observed by additive modules.
      const lcProc = document.getElementById("lc-processed");
      const lcRec  = document.getElementById("lc-recovered");
      const lcEsc  = document.getElementById("lc-escalated");
      const lcCnt  = document.getElementById("lc-count");
      const lcFill = document.getElementById("lc-fill");
      if (lcProc) lcProc.textContent = processed;
      if (lcRec  && lcRec.textContent  !== String(recovered))  lcRec.textContent  = recovered;
      if (lcEsc  && lcEsc.textContent  !== String(escalated))  lcEsc.textContent  = escalated;
      if (lcCnt)  lcCnt.textContent  = processed + " / " + total;
      if (lcFill) lcFill.style.width = ((processed / total) * 100).toFixed(1) + "%";

      // The pipeline sweep already provides pacing, so keep the trailing pause
      // short and let it shrink further when the queue backs up.
      const delay = queue.length > 30 ? 0 : (queue.length > 10 ? 40 : 120);
      await sleep(delay);
    } catch (drainErr) {
      // Log the error but never let a single-case failure freeze the entire run.
      console.error("Drain loop error on case", trace && trace.customer_id, drainErr);
      processed += 1; // still count it so the total is accurate
    }
  }

  // Leave all four nodes lit but stop pulsing once the run finishes.
  document.querySelectorAll("#pipeline .pnode").forEach((n) => {
    n.classList.remove("pulsing");
    n.classList.add("active");
  });

  // If the stream ended without delivering the final {done} summary — e.g. the
  // connection was cut mid-run — reconcile the outcome from the backend, which has
  // still finished processing and persisted the results. This guarantees the UI shows
  // an accurate, complete result (all 180 processed) rather than freezing at the last
  // card that happened to arrive before the cut.
  if (!finalSummary) {
    try {
      const m = (await getJSON("/api/metrics")).agent;
      const rec = m.recovered_cases || 0;
      const esc = m.escalated_cases || 0;
      const proc = m.total_cases || total;
      // Backfill the live counters so they read as fully complete.
      document.getElementById("lc-processed").textContent = proc;
      document.getElementById("lc-recovered").textContent = rec;
      document.getElementById("lc-escalated").textContent = esc;
      document.getElementById("lc-count").textContent = proc + " / " + proc;
      document.getElementById("lc-fill").style.width = "100%";
      finalSummary = {
        processed: proc,
        status_counts: { recovered: rec, escalated: esc },
        reconciled: true,
      };
      if (streamError) {
        console.warn("run-agent stream was interrupted; reconciled final totals from " +
                     "/api/metrics.", streamError);
      }
    } catch (e) {
      // Even the reconcile failed; leave whatever the drain loop rendered in place.
      console.warn("Could not reconcile run summary after an interrupted stream.", e);
    }
  }

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
    // Reset button back to "Run agent"
    Array.from(runBtn.childNodes).forEach(n => {
      if (n.nodeType === Node.TEXT_NODE && n.textContent.trim().length > 0) {
        n.textContent = " Run agent";
      }
    });
    showEmptyState(false);
    await loadDashboard();
    banner("Fresh data seeded. Click \u201CRun agent\u201D to watch the pipeline work.");
  } catch (err) {
    banner(err.message, true);
  } finally {
    resetBtn.disabled = false; runBtn.disabled = false;
  }
}


// --- Policy Experimentation Sandbox -----------------------------------------
// Analysis tool only: gathers policy knobs, calls POST /api/simulate, and renders
// Monte Carlo results (mean +/- 95% CI) plus a paired comparison against the current
// default policy. Never changes the live agent's configuration.
let sandboxMode = "adaptive";

function sbWeightInputs() {
  return {
    success: document.getElementById("sb-w-success"),
    tenure: document.getElementById("sb-w-tenure"),
    retry: document.getElementById("sb-w-retry"),
    reason: document.getElementById("sb-w-reason"),
  };
}

function sbReadWeights() {
  const ins = sbWeightInputs();
  return {
    success: parseFloat(ins.success.value),
    tenure: parseFloat(ins.tenure.value),
    retry: parseFloat(ins.retry.value),
    reason: parseFloat(ins.reason.value),
  };
}

// Live-update the weight-sum indicator; return whether it is a valid 1.0 sum.
function sbUpdateWeightSum() {
  const w = sbReadWeights();
  const vals = [w.success, w.tenure, w.retry, w.reason];
  const anyNaN = vals.some((v) => Number.isNaN(v));
  const sum = anyNaN ? NaN : vals.reduce((a, b) => a + b, 0);
  const box = document.getElementById("sb-weight-sum");
  const ok = !anyNaN && Math.abs(sum - 1.0) <= 0.01;
  if (anyNaN) {
    box.innerHTML = "Sum: \u2014 (enter all four weights)";
    box.className = "sb-sub bad";
  } else {
    box.innerHTML = "Sum: " + sum.toFixed(2) + (ok ? " \u2713" : " \u2717 (must be 1.0)");
    box.className = "sb-sub " + (ok ? "ok" : "bad");
  }
  return ok;
}

function sbSetError(msg) {
  const box = document.getElementById("sb-error");
  if (!msg) { box.classList.add("hidden"); box.textContent = ""; return; }
  box.textContent = msg;
  box.classList.remove("hidden");
}

function sbResetDefaults() {
  document.getElementById("sb-retry-cap").value = "3";
  document.getElementById("sb-retry-cap-val").textContent = "3";
  const ins = sbWeightInputs();
  ins.success.value = "0.40"; ins.tenure.value = "0.20";
  ins.retry.value = "0.20"; ins.reason.value = "0.20";
  document.getElementById("sb-n-runs").value = "30";
  document.getElementById("sb-n-runs-val").textContent = "30";
  sandboxMode = "adaptive";
  document.querySelectorAll("#sb-mode-toggle .sb-toggle-btn").forEach((b) =>
    b.classList.toggle("active", b.dataset.mode === "adaptive"));
  sbUpdateWeightSum();
  sbSetError("");
}

// Format a rate metric summary as "78.2% ± 2.1% (95% CI)".
function fmtRateCI(m) {
  return pct(m.mean) + " \u00B1 " + (m.ci_margin * 100).toFixed(1) + "% (95% CI)";
}

// A simple horizontal bar showing where the CI sits on a 0-100% scale, with the
// mean marked and the CI band shaded.
function ciBar(m) {
  const wrap = el("div", "ci-bar");
  const lowPct = Math.max(0, Math.min(100, m.ci_low * 100));
  const highPct = Math.max(0, Math.min(100, m.ci_high * 100));
  const meanPct = Math.max(0, Math.min(100, m.mean * 100));
  const band = el("span", "ci-band");
  band.style.left = lowPct.toFixed(2) + "%";
  band.style.width = Math.max(0.5, highPct - lowPct).toFixed(2) + "%";
  const marker = el("span", "ci-mean");
  marker.style.left = meanPct.toFixed(2) + "%";
  wrap.appendChild(band);
  wrap.appendChild(marker);
  return wrap;
}

// One metric row: label, "mean ± margin (95% CI)" text, and the CI bar.
function sbMetricRow(label, m, kind) {
  const row = el("div", "sb-metric");
  row.appendChild(el("div", "sb-metric-label", label));
  const valText = kind === "money"
    ? rupees(m.mean) + " \u00B1 " + rupees(m.ci_margin) + " (95% CI)"
    : fmtRateCI(m);
  row.appendChild(el("div", "sb-metric-value", valText));
  if (kind !== "money") row.appendChild(ciBar(m));
  return row;
}

// Render the delta (modified - default) for one metric with its own CI, choosing
// "improves"/"reduces" wording and coloring by direction + significance.
// higherIsBetter flips the good/bad coloring for escalation rate.
function sbDeltaRow(label, d, kind, higherIsBetter) {
  const row = el("div", "sb-delta");
  row.appendChild(el("div", "sb-delta-label", label));
  const meanTxt = kind === "money"
    ? (d.mean >= 0 ? "+" : "\u2212") + rupees(Math.abs(d.mean))
    : (d.mean >= 0 ? "+" : "\u2212") + (Math.abs(d.mean) * 100).toFixed(1) + "%";
  const marginTxt = kind === "money"
    ? rupees(d.ci_margin)
    : (d.ci_margin * 100).toFixed(1) + "%";
  // Significant if the CI does not cross zero.
  const significant = (d.ci_low > 0) || (d.ci_high < 0);
  const improving = higherIsBetter ? (d.mean > 0) : (d.mean < 0);
  let cls = "neutral";
  if (significant) cls = improving ? "good" : "bad";
  const val = el("div", "sb-delta-value " + cls, meanTxt + " \u00B1 " + marginTxt);
  row.appendChild(val);
  const note = el("div", "sb-delta-note",
    significant
      ? (improving ? "significant improvement" : "significant regression")
      : "not statistically distinguishable from the default (CI crosses zero)");
  row.appendChild(note);
  return row;
}

function renderSandboxResults(data) {
  const box = document.getElementById("sb-results");
  document.getElementById("sb-placeholder").classList.add("hidden");
  box.classList.remove("hidden");
  box.innerHTML = "";

  const mod = data.modified.metrics;
  const def = data.default.metrics;
  const delta = data.delta;
  const isDefault = data.modified.policy && data.modified.policy.is_default;

  box.appendChild(el("div", "sb-results-head",
    "Monte Carlo \u00B7 " + data.n_runs + " runs per policy \u00B7 LLM skipped (analysis only)"));

  // Modified policy metrics.
  const modCard = el("div", "sb-result-card");
  modCard.appendChild(el("div", "sb-card-title",
    isDefault ? "Your policy (currently equals the default)" : "Your policy"));
  modCard.appendChild(sbMetricRow("Recovery rate", mod.recovery_rate, "rate"));
  modCard.appendChild(sbMetricRow("Escalation rate", mod.escalation_rate, "rate"));
  modCard.appendChild(sbMetricRow("Amount recovered", mod.amount_recovered, "money"));
  box.appendChild(modCard);

  // Default policy metrics (for reference).
  const defCard = el("div", "sb-result-card muted-card");
  defCard.appendChild(el("div", "sb-card-title", "Current default policy"));
  defCard.appendChild(sbMetricRow("Recovery rate", def.recovery_rate, "rate"));
  defCard.appendChild(sbMetricRow("Escalation rate", def.escalation_rate, "rate"));
  defCard.appendChild(sbMetricRow("Amount recovered", def.amount_recovered, "money"));
  box.appendChild(defCard);

  // Paired delta comparison.
  const cmpCard = el("div", "sb-result-card sb-compare");
  cmpCard.appendChild(el("div", "sb-card-title", "Change vs. default policy (paired, same seeds)"));
  cmpCard.appendChild(sbDeltaRow("Recovery rate", delta.recovery_rate, "rate", true));
  cmpCard.appendChild(sbDeltaRow("Escalation rate", delta.escalation_rate, "rate", false));
  cmpCard.appendChild(sbDeltaRow("Amount recovered", delta.amount_recovered, "money", true));
  box.appendChild(cmpCard);

  box.appendChild(el("div", "sb-footnote", data.note || ""));
}

async function runSandboxSimulation() {
  sbSetError("");
  if (!sbUpdateWeightSum()) {
    sbSetError("Score weights must sum to 1.0 before running. Adjust the four weights.");
    return;
  }
  const runBtn = document.getElementById("sb-run-btn");
  const nRuns = parseInt(document.getElementById("sb-n-runs").value, 10) || 30;
  const body = {
    n_runs: nRuns,
    retry_cap: parseInt(document.getElementById("sb-retry-cap").value, 10),
    score_weights: sbReadWeights(),
    salary_window_mode: sandboxMode,
  };

  runBtn.disabled = true;
  const originalLabel = runBtn.textContent;
  runBtn.textContent = "Running " + nRuns + " simulations\u2026";
  document.getElementById("sb-placeholder").classList.remove("hidden");
  document.getElementById("sb-placeholder").textContent =
    "Running " + nRuns + " simulations for your policy and the default\u2026 this takes a few seconds.";
  document.getElementById("sb-results").classList.add("hidden");

  try {
    const r = await fetch("/api/simulate", {
      method: "POST",
      headers: Object.assign(
        { "Content-Type": "application/json" },
        _apiKey ? { "X-API-Key": _apiKey } : {}
      ),
      body: JSON.stringify(body),
    });
    const data = await r.json();
    if (!r.ok || !data.ok) {
      sbSetError(data.message || "Simulation failed. Check the inputs and try again.");
      document.getElementById("sb-placeholder").textContent =
        "Set a policy and run the simulation to see results.";
      return;
    }
    renderSandboxResults(data);
  } catch (err) {
    sbSetError("Simulation request failed: " + err.message);
  } finally {
    runBtn.disabled = false;
    runBtn.textContent = originalLabel;
  }
}

function initSandbox() {
  const retry = document.getElementById("sb-retry-cap");
  if (!retry) return; // panel not present
  retry.addEventListener("input", () => {
    document.getElementById("sb-retry-cap-val").textContent = retry.value;
  });
  const nRuns = document.getElementById("sb-n-runs");
  nRuns.addEventListener("input", () => {
    document.getElementById("sb-n-runs-val").textContent = nRuns.value;
    const btn = document.getElementById("sb-run-btn");
    btn.textContent = "Run Simulation (" + nRuns.value + "x)";
  });
  Object.values(sbWeightInputs()).forEach((inp) =>
    inp.addEventListener("input", sbUpdateWeightSum));
  document.querySelectorAll("#sb-mode-toggle .sb-toggle-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      sandboxMode = btn.dataset.mode;
      document.querySelectorAll("#sb-mode-toggle .sb-toggle-btn").forEach((b) =>
        b.classList.toggle("active", b === btn));
    });
  });
  document.getElementById("sb-run-btn").addEventListener("click", runSandboxSimulation);
  document.getElementById("sb-reset-btn").addEventListener("click", sbResetDefaults);
  sbUpdateWeightSum();
}


// --- Sidebar navigation (Phase 1) -------------------------------------------
// Pure layout/navigation: shows exactly one <section class="view"> at a time and
// marks the matching sidebar item active. Does not touch any API call or data.
const VIEW_IDS = ["overview", "cases", "compliance", "ml", "sandbox", "chaos", "replay", "reports", "analytics", "learning"];

const VIEW_TITLES = {
  overview:   "Overview",
  cases:      "Cases",
  compliance: "Compliance",
  ml:         "ML Insights",
  sandbox:    "Policy Sandbox",
  chaos:      "Chaos Suite",
  replay:     "Case Replay",
  reports:    "Reports",
  analytics:  "Analytics",
  learning:   "Learning",
};

function showView(view) {
  if (!VIEW_IDS.includes(view)) view = "overview";
  // Hide any p7 view-content panels that may be visible
  document.querySelectorAll(".view-content").forEach((sec) => sec.classList.add("hidden"));
  document.querySelectorAll(".view").forEach((sec) => {
    sec.classList.toggle("active", sec.dataset.view === view);
  });
  document.querySelectorAll(".nav-item[data-view]").forEach((item) => {
    const isActive = item.dataset.view === view;
    item.classList.toggle("active", isActive);
    item.setAttribute("aria-current", isActive ? "page" : "false");
  });
  // Update header breadcrumb
  const headerTitle = document.getElementById("header-page-title");
  if (headerTitle) headerTitle.textContent = VIEW_TITLES[view] || "";
  // Update page <title>
  document.title = (VIEW_TITLES[view] ? VIEW_TITLES[view] + " — " : "") + "Mandate Rescue";
  // Keep the hash in sync so a section is deep-linkable / survives refresh.
  if (("#" + view) !== window.location.hash) {
    history.replaceState(null, "", "#" + view);
  }
  // Refresh the per-view empty hints (panels may have become (un)hidden).
  syncViewEmptyStates();
}

// Some views host panels that hide themselves before a run / when unavailable
// (audit, rejected webhooks, ML). Show a friendly hint so the section is never
// just blank. Read-only DOM inspection; no data changes.
function syncViewEmptyStates() {
  const auditHidden = document.getElementById("audit-panel").classList.contains("hidden");
  const rejectedHidden = document.getElementById("rejected-card").classList.contains("hidden");
  const complianceEmpty = document.getElementById("compliance-empty");
  if (complianceEmpty) {
    complianceEmpty.classList.toggle("hidden", !(auditHidden && rejectedHidden));
  }
  const mlHidden = document.getElementById("ml-panel").classList.contains("hidden");
  const mlEmpty = document.getElementById("ml-empty");
  if (mlEmpty) mlEmpty.classList.toggle("hidden", !mlHidden);
}

function initSidebar() {
  // Only attach showView() to nav items whose data-view is managed by app.js.
  // Recovery OS items (view-content sections) are handled exclusively by p7.js
  // via activateView(); attaching showView() to them as well would fall back to
  // "overview" and stomp the p7 navigation.
  document.querySelectorAll(".nav-item[data-view]").forEach((item) => {
    if (VIEW_IDS.includes(item.dataset.view)) {
      item.addEventListener("click", () => showView(item.dataset.view));
    }
  });
  // Wire global search input to open command palette on click/focus
  const globalSearch = document.getElementById("global-search");
  if (globalSearch) {
    globalSearch.addEventListener("click", () => openCmdk());
    globalSearch.addEventListener("keydown", (e) => {
      if (e.key !== "Tab") { e.preventDefault(); openCmdk(); }
    });
  }
  // Restore from hash on load (default: overview).
  const initial = (window.location.hash || "").replace("#", "");
  showView(VIEW_IDS.includes(initial) ? initial : "overview");
}


// --- Chaos Suite view (Phase 1) ---------------------------------------------
// Triggers GET /api/chaos-test and renders the ten-scenario PASS/FAIL report.
// The endpoint runs entirely in isolated in-memory databases (never the live DB),
// so this is safe to run at any time and changes no agent/scoring/compliance logic.
function renderChaosResults(report) {
  const box = document.getElementById("chaos-results");
  document.getElementById("chaos-placeholder").classList.add("hidden");
  box.classList.remove("hidden");
  box.innerHTML = "";

  const passed = !!report.passed;
  const overall = el("div", "chaos-overall " + (passed ? "ok" : "bad"));
  overall.appendChild(el("span", "audit-icon", passed ? "\u2713" : "\u2717"));
  const scenarioCount = (report.scenarios || []).length;
  const failedCount = (report.scenarios || []).filter((s) => !s.passed).length;
  overall.appendChild(el("span", "audit-banner-text",
    passed
      ? "All " + scenarioCount + " adversarial scenarios defended"
      : (report.total_failures || 0) + " failure" + ((report.total_failures === 1) ? "" : "s") +
        " across " + failedCount + " scenario" + (failedCount === 1 ? "" : "s")));
  box.appendChild(overall);

  (report.scenarios || []).forEach((sc) => {
    const row = el("div", "chaos-scenario " + (sc.passed ? "ok" : "bad"));
    const head = el("div", "chaos-scenario-head");
    head.appendChild(el("span", "chaos-scenario-icon", sc.passed ? "\u2713" : "\u2717"));
    head.appendChild(el("span", "chaos-scenario-id", sc.id));
    head.appendChild(el("span", "chaos-scenario-desc", sc.description));
    row.appendChild(head);
    if (!sc.passed && Array.isArray(sc.failures) && sc.failures.length) {
      const list = el("ul", "chaos-failures");
      sc.failures.forEach((f) => {
        const li = el("li");
        if (f.customer_id) {
          li.appendChild(el("span", "audit-cid num", maskId(f.customer_id)));
          li.appendChild(document.createTextNode(" " + f.detail));
        } else {
          li.textContent = f.detail;
        }
        list.appendChild(li);
      });
      row.appendChild(list);
    }
    box.appendChild(row);
  });
}

async function runChaosSuite() {
  const btn = document.getElementById("chaos-run-btn");
  const errBox = document.getElementById("chaos-error");
  errBox.classList.add("hidden");
  btn.disabled = true;
  const originalLabel = btn.textContent;
  btn.textContent = "Running ten attacks\u2026";

  // Visible loading state: a spinner + a clear description of what is actually
  // happening. The request is a real HTTP round-trip that seeds 2000+ cases for
  // scenario 7 and runs ten real scenarios against isolated in-memory databases,
  // so it is NOT instant — the spinner makes that latency legible rather than making
  // a genuine run look fake/pre-baked.
  const placeholder = document.getElementById("chaos-placeholder");
  placeholder.classList.remove("hidden");
  placeholder.innerHTML = "";
  const loading = el("div", "chaos-loading");
  loading.appendChild(el("span", "spinner"));
  loading.appendChild(el("span", null,
    "Running 10 adversarial scenarios against an isolated test database\u2026"));
  placeholder.appendChild(loading);
  document.getElementById("chaos-results").classList.add("hidden");

  try {
    const report = await getJSON("/api/chaos-test");
    renderChaosResults(report);
  } catch (err) {
    errBox.textContent = "Chaos suite failed to run: " + err.message;
    errBox.classList.remove("hidden");
    placeholder.textContent =
      "Run the suite to attack the system with ten adversarial scenarios and see a PASS/FAIL report for each.";
  } finally {
    btn.disabled = false;
    btn.textContent = originalLabel;
  }
}

function initChaos() {
  const btn = document.getElementById("chaos-run-btn");
  if (btn) btn.addEventListener("click", runChaosSuite);
}


// --- Command palette (Phase 2) ----------------------------------------------
// Cmd/Ctrl+K opens a centered modal that can (a) jump to any sidebar section,
// (b) jump to a specific case by customer-id search, and (c) trigger the key
// header actions (Run agent, Reset demo, Download summary). Every command maps
// to an existing view or button — no new API calls or business logic.
const CMDK_SECTIONS = [
  { view: "overview", title: "Overview", sub: "KPIs, live pipeline, baseline & cohorts", ico: "\u25A6" },
  { view: "cases", title: "Cases", sub: "Cases table, exceptions & ask the data", ico: "\u2630" },
  { view: "compliance", title: "Compliance", sub: "Correctness audit & rejected webhooks", ico: "\u2713" },
  { view: "ml", title: "ML Insights", sub: "Model metrics, SHAP & feature importance", ico: "\u25C8" },
  { view: "sandbox", title: "Policy Sandbox", sub: "Simulate policy changes", ico: "\u2699" },
  { view: "chaos", title: "Chaos Suite", sub: "Run the adversarial test suite", ico: "\u26A1" },
  { view: "reports", title: "Reports", sub: "Download the CSV summary", ico: "\u21A7" },
];
const CMDK_ACTIONS = [
  { id: "run", title: "Run agent", sub: "Run the four-agent recovery pipeline", ico: "\u25B6",
    run: () => document.getElementById("btn-run").click() },
  { id: "reset", title: "Reset demo", sub: "Re-seed fresh data and clear the previous run", ico: "\u21BB",
    run: () => document.getElementById("btn-reset").click() },
  { id: "export", title: "Download summary", sub: "Export the current run as CSV", ico: "\u2193",
    run: () => { window.location.href = "/api/export"; } },
];

let cmdkCases = [];           // cached case list for id search
let cmdkCasesLoaded = false;  // whether we've fetched them at least once
let cmdkCasesLoading = false; // a /api/cases fetch is currently in flight
let cmdkResults = [];         // current flattened result list (excludes group headers)
let cmdkActiveIdx = 0;

// Case-insensitive subsequence match (fuzzy), e.g. "plsb" matches "Policy Sandbox".
function cmdkFuzzy(query, text) {
  query = query.toLowerCase(); text = text.toLowerCase();
  if (!query) return true;
  let i = 0;
  for (let c = 0; c < text.length && i < query.length; c++) {
    if (text[c] === query[i]) i++;
  }
  return i === query.length;
}

// Highlight the first contiguous match of the query in a title (safe: text nodes only).
function cmdkHighlight(title, query) {
  const span = el("span");
  if (!query) { span.textContent = title; return span; }
  const idx = title.toLowerCase().indexOf(query.toLowerCase());
  if (idx === -1) { span.textContent = title; return span; }
  span.appendChild(document.createTextNode(title.slice(0, idx)));
  const mark = el("mark", null, title.slice(idx, idx + query.length));
  span.appendChild(mark);
  span.appendChild(document.createTextNode(title.slice(idx + query.length)));
  return span;
}

async function cmdkEnsureCases() {
  cmdkCasesLoading = true;
  try {
    const cases = await getJSON("/api/cases");
    cmdkCases = Array.isArray(cases) ? cases : [];
    cmdkCasesLoaded = true;
  } catch (e) {
    cmdkCases = [];
    cmdkCasesLoaded = true;
  } finally {
    cmdkCasesLoading = false;
  }
}

function cmdkBuildItems(query) {
  const q = query.trim();
  const groups = [];

  // Sections.
  const sections = CMDK_SECTIONS
    .filter((s) => cmdkFuzzy(q, s.title) || cmdkFuzzy(q, "go " + s.title))
    .map((s) => ({
      kind: "Go to", ico: s.ico, title: s.title, sub: s.sub,
      run: () => showView(s.view),
    }));
  if (sections.length) groups.push({ label: "Sections", items: sections });

  // Actions.
  const actions = CMDK_ACTIONS
    .filter((a) => cmdkFuzzy(q, a.title) || cmdkFuzzy(q, "run " + a.title))
    .map((a) => ({
      kind: "Action", ico: a.ico, title: a.title, sub: a.sub, run: a.run,
    }));
  if (actions.length) groups.push({ label: "Actions", items: actions });

  // Cases by customer id (only when there is a query; masked in display, real id used).
  if (q) {
    const matches = cmdkCases
      .filter((c) => String(c.customer_id).toLowerCase().includes(q.toLowerCase()))
      .slice(0, 8)
      .map((c) => ({
        kind: "Case", ico: "#", mono: true,
        title: maskId(c.customer_id),
        sub: titleCase(c.failure_reason) + " \u00B7 " + rupees(c.amount) + " \u00B7 " + titleCase(c.case_status),
        run: () => { showView("cases"); openDrawer(c.customer_id); },
      }));
    if (matches.length) groups.push({ label: "Cases", items: matches });
  }

  return groups;
}

function cmdkRender(query) {
  const list = document.getElementById("cmdk-list");
  const emptyBox = document.getElementById("cmdk-empty");
  const loadingBox = document.getElementById("cmdk-loading");
  list.innerHTML = "";
  cmdkResults = [];

  const groups = cmdkBuildItems(query);
  const total = groups.reduce((n, g) => n + g.items.length, 0);
  if (total === 0) {
    // Distinguish "genuinely no matches" from "cases still loading". A non-empty
    // query can match cases, so if a /api/cases fetch is in flight and we have no
    // cached cases to search yet, show a loading indicator rather than a premature
    // "No matches". We intentionally do NOT gate on cmdkCasesLoaded: a prior fetch
    // may have completed (or failed) while a fresh one is now in flight.
    const casesPending = query.trim() && cmdkCasesLoading && cmdkCases.length === 0;
    loadingBox.classList.toggle("hidden", !casesPending);
    emptyBox.classList.toggle("hidden", !!casesPending);
    return;
  }
  loadingBox.classList.add("hidden");
  emptyBox.classList.add("hidden");
  if (cmdkActiveIdx >= total) cmdkActiveIdx = 0;

  let flatIdx = 0;
  groups.forEach((g) => {
    const label = el("li", "cmdk-group", g.label);
    label.setAttribute("aria-hidden", "true");
    list.appendChild(label);
    g.items.forEach((item) => {
      const myIdx = flatIdx++;
      cmdkResults.push(item);
      const li = el("li", "cmdk-item");
      li.setAttribute("role", "option");
      if (myIdx === cmdkActiveIdx) { li.classList.add("active"); li.setAttribute("aria-selected", "true"); }

      const ico = el("span", "cmdk-ico", item.ico);
      li.appendChild(ico);

      const textWrap = el("span", "cmdk-text");
      const titleEl = el("span", "cmdk-title");
      const inner = cmdkHighlight(item.title, item.kind === "Case" ? "" : query.trim());
      if (item.mono) inner.classList.add("cmdk-mono");
      titleEl.appendChild(inner);
      textWrap.appendChild(titleEl);
      if (item.sub) textWrap.appendChild(el("span", "cmdk-sub", item.sub));
      li.appendChild(textWrap);

      li.appendChild(el("span", "cmdk-kind", item.kind));

      li.addEventListener("mousemove", () => {
        if (cmdkActiveIdx !== myIdx) { cmdkActiveIdx = myIdx; cmdkUpdateActive(); }
      });
      li.addEventListener("click", () => cmdkChoose(myIdx));
      list.appendChild(li);
    });
  });
}

function cmdkUpdateActive() {
  const items = document.querySelectorAll("#cmdk-list .cmdk-item");
  items.forEach((li, i) => {
    const on = i === cmdkActiveIdx;
    li.classList.toggle("active", on);
    if (on) { li.setAttribute("aria-selected", "true"); li.scrollIntoView({ block: "nearest" }); }
    else li.removeAttribute("aria-selected");
  });
}

function cmdkChoose(idx) {
  const item = cmdkResults[idx];
  if (!item) return;
  closeCmdk();
  // Defer so the modal is fully closed before a view swap / drawer opens.
  setTimeout(() => { try { item.run(); } catch (e) { /* no-op */ } }, 0);
}

function openCmdk() {
  const overlay = document.getElementById("cmdk-overlay");
  const input = document.getElementById("cmdk-input");
  overlay.classList.remove("hidden");
  input.value = "";
  cmdkActiveIdx = 0;
  cmdkRender("");
  input.focus();
  // Lazily (re)load cases so id-search is current with the latest run.
  cmdkEnsureCases().then(() => {
    // Only re-render if the palette is still open and a query needs cases.
    if (!overlay.classList.contains("hidden")) cmdkRender(input.value);
  });
}

function closeCmdk() {
  document.getElementById("cmdk-overlay").classList.add("hidden");
}

function cmdkIsOpen() {
  return !document.getElementById("cmdk-overlay").classList.contains("hidden");
}

function initCmdk() {
  const overlay = document.getElementById("cmdk-overlay");
  const input = document.getElementById("cmdk-input");

  // Pre-load cases so id-search is instant on first open (refreshed after runs).
  cmdkEnsureCases();

  // Global open shortcut: Cmd+K (mac) / Ctrl+K (win/linux).
  document.addEventListener("keydown", (e) => {
    if ((e.metaKey || e.ctrlKey) && (e.key === "k" || e.key === "K")) {
      e.preventDefault();
      if (cmdkIsOpen()) closeCmdk(); else openCmdk();
    }
  });

  // Click on the backdrop (outside the modal) closes it.
  overlay.addEventListener("mousedown", (e) => {
    if (e.target === overlay) closeCmdk();
  });

  input.addEventListener("input", () => { cmdkActiveIdx = 0; cmdkRender(input.value); });

  input.addEventListener("keydown", (e) => {
    if (e.key === "ArrowDown") {
      e.preventDefault();
      if (cmdkResults.length) { cmdkActiveIdx = (cmdkActiveIdx + 1) % cmdkResults.length; cmdkUpdateActive(); }
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      if (cmdkResults.length) { cmdkActiveIdx = (cmdkActiveIdx - 1 + cmdkResults.length) % cmdkResults.length; cmdkUpdateActive(); }
    } else if (e.key === "Enter") {
      e.preventDefault();
      cmdkChoose(cmdkActiveIdx);
    } else if (e.key === "Escape") {
      e.preventDefault();
      closeCmdk();
    }
  });
}


// --- Activity feed (Phase 4) ------------------------------------------------
// Renders the most recent audit_log events (from /api/activity) as a compact,
// scrollable feed, newest first, each with a one-line human-readable description.
// Read-only; updates after each run via loadDashboard().
function activityDescription(e) {
  const who = maskId(e.customer_id);
  const type = titleCase(e.event_type || "");
  const attempt = e.attempt_number ? " (attempt " + e.attempt_number + ")" : "";
  const status = titleCase(e.case_status_after || "");
  // Prefer the recorded action; fall back to a type-based sentence.
  const action = e.action_taken ? e.action_taken : type;
  return { who, line: action + attempt, meta: type + (status ? " \u2192 " + status : "") };
}

function outcomeClass(outcome) {
  if (outcome === "success") return "ok";
  if (outcome === "failure") return "bad";
  return "";
}

function renderActivity(data) {
  const feed = document.getElementById("activity-feed");
  if (!feed) return;
  const events = (data && data.events) || [];
  feed.innerHTML = "";
  if (!events.length) {
    feed.appendChild(el("div", "activity-empty muted",
      "Run the agent to see live activity here."));
    return;
  }
  events.forEach((e) => {
    const item = el("div", "activity-item " + outcomeClass(e.outcome));
    item.appendChild(el("span", "activity-dot"));
    const main = el("div", "activity-main");
    const d = activityDescription(e);
    const desc = el("div", "activity-desc");
    desc.appendChild(el("span", "activity-cid", d.who));
    desc.appendChild(document.createTextNode(" \u00B7 " + d.line));
    main.appendChild(desc);
    const meta = el("div", "activity-meta");
    const ts = (e.event_timestamp || "").replace("T", " ");
    meta.appendChild(el("span", null, d.meta));
    if (ts) { meta.appendChild(document.createTextNode(" \u00B7 ")); meta.appendChild(el("span", "num", ts)); }
    main.appendChild(meta);
    // Clicking an activity row opens that case's drawer.
    item.style.cursor = "pointer";
    item.addEventListener("click", () => openDrawer(e.customer_id));
    item.appendChild(main);
    feed.appendChild(item);
  });
}

async function loadActivity() {
  try {
    const data = await getJSON("/api/activity?limit=40");
    renderActivity(data);
  } catch (e) {
    // Non-fatal: leave the feed's current content in place.
  }
}


// --- Theme toggle (Phase 4) -------------------------------------------------
// Light/dark toggle that re-maps the existing token system. The choice is kept
// in-memory for the session (module variable); no persistence backend needed.
let currentTheme = "light";

function applyTheme(theme) {
  currentTheme = theme === "dark" ? "dark" : "light";
  const root = document.documentElement;
  if (currentTheme === "dark") root.setAttribute("data-theme", "dark");
  else root.removeAttribute("data-theme");
  // Update sidebar nav-label for the theme button
  const btn = document.getElementById("btn-theme");
  if (btn) {
    btn.title = currentTheme === "dark" ? "Switch to light theme" : "Switch to dark theme";
    const label = btn.querySelector(".nav-label");
    if (label) label.textContent = currentTheme === "dark" ? "Light mode" : "Dark mode";
    // Swap the SVG icon: sun = dark mode (switch to light), moon = light mode (switch to dark)
    const icon = document.getElementById("theme-icon");
    if (icon) {
      icon.innerHTML = currentTheme === "dark"
        ? '<svg width="16" height="16" viewBox="0 0 16 16" fill="none"><circle cx="8" cy="8" r="3.5" stroke="currentColor" stroke-width="1.5"/><path d="M8 1.5V3M8 13v1.5M1.5 8H3M13 8h1.5M3.4 3.4l1.06 1.06M11.54 11.54l1.06 1.06M3.4 12.6l1.06-1.06M11.54 4.46l1.06-1.06" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"/></svg>'
        : '<svg width="16" height="16" viewBox="0 0 16 16" fill="none"><path d="M12.5 9.5A5 5 0 1 1 6.5 3.5a3.5 3.5 0 1 0 6 6z" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round"/></svg>';
    }
  }
  // Persist preference
  try { localStorage.setItem("mr-theme", currentTheme); } catch (_) {}
}

function toggleTheme() {
  applyTheme(currentTheme === "dark" ? "light" : "dark");
}

function initTheme() {
  const btn = document.getElementById("btn-theme");
  if (btn) btn.addEventListener("click", toggleTheme);
  // Restore persisted preference or detect system preference
  let saved = "light";
  try { saved = localStorage.getItem("mr-theme") || "light"; } catch (_) {}
  if (!saved && window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches) {
    saved = "dark";
  }
  applyTheme(saved);
}


// --- Help overlay (Phase 4) -------------------------------------------------
function openHelp() { document.getElementById("help-overlay").classList.remove("hidden"); }
function closeHelp() { document.getElementById("help-overlay").classList.add("hidden"); }
function helpIsOpen() { return !document.getElementById("help-overlay").classList.contains("hidden"); }

function initHelp() {
  const overlay = document.getElementById("help-overlay");
  document.getElementById("btn-help").addEventListener("click", openHelp);
  document.getElementById("help-close").addEventListener("click", closeHelp);
  overlay.addEventListener("mousedown", (e) => { if (e.target === overlay) closeHelp(); });
}


// --- Keyboard shortcuts (Phase 4) -------------------------------------------
// g o / g c (two-key sequences), r (run agent), / (focus ask), ? (help).
// All shortcuts are ignored while typing in an input/textarea/select or when a
// modal (command palette / help) is open, so they never fight text entry.
let pendingG = false;
let pendingGTimer = null;

function isTypingTarget(t) {
  if (!t) return false;
  const tag = (t.tagName || "").toLowerCase();
  return tag === "input" || tag === "textarea" || tag === "select" || t.isContentEditable;
}

function focusAsk() {
  showView("cases");
  const input = document.getElementById("ask-input");
  if (input) { input.focus(); input.select(); }
}

function initShortcuts() {
  document.addEventListener("keydown", (e) => {
    // Never intercept modifier combos (leave Cmd/Ctrl+K etc. to their handlers).
    if (e.metaKey || e.ctrlKey || e.altKey) return;
    // Don't hijack typing.
    if (isTypingTarget(e.target)) return;
    // Don't act while the command palette is open (it has its own key handling).
    if (typeof cmdkIsOpen === "function" && cmdkIsOpen()) return;

    // "?" opens help (also closes it if already open).
    if (e.key === "?") {
      e.preventDefault();
      if (helpIsOpen()) closeHelp(); else openHelp();
      return;
    }
    // While help is open, ignore other shortcuts (Esc handled globally below).
    if (helpIsOpen()) return;

    // Two-key "g" sequences.
    if (pendingG) {
      if (e.key === "o") { e.preventDefault(); showView("overview"); }
      else if (e.key === "c") { e.preventDefault(); showView("cases"); }
      pendingG = false;
      if (pendingGTimer) { clearTimeout(pendingGTimer); pendingGTimer = null; }
      return;
    }
    if (e.key === "g") {
      pendingG = true;
      pendingGTimer = setTimeout(() => { pendingG = false; }, 900);
      return;
    }

    // Single-key shortcuts.
    if (e.key === "r") {
      e.preventDefault();
      document.getElementById("btn-run").click();
    } else if (e.key === "/") {
      e.preventDefault();
      focusAsk();
    }
  });

  // Global Escape: close help overlay (drawer + palette have their own Esc paths).
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && helpIsOpen()) closeHelp();
  });
}


// --- Init -------------------------------------------------------------------
document.addEventListener("DOMContentLoaded", async () => {
  // Fetch the internal API key first so every subsequent mutating call (Run agent,
  // Reset, Policy Sandbox) has it available immediately.
  await fetchApiKey();

  initSidebar();
  initChaos();
  initCmdk();
  initCasesTabs();
  initTheme();
  initHelp();
  initShortcuts();
  initSandbox();
  loadPersistedUiState();
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


// =============================================================================
// PRODUCTION UPGRADES — Recovery Funnel, Live Counter, Webhook Inspector,
// Case Replay. All additive; no existing functions modified.
// =============================================================================

// --- Recovery Funnel --------------------------------------------------------

function renderFunnel(metricsData, cases) {
  const a   = metricsData.agent;
  const hasRun = (a.recovered_cases || 0) > 0 || (a.escalated_cases || 0) > 0;
  const pre = document.getElementById("funnel-pre-run");
  if (!hasRun) {
    if (pre) pre.classList.remove("hidden");
    return;
  }
  if (pre) pre.classList.add("hidden");

  const total     = a.total_cases || 0;
  const recovered = a.recovered_cases || 0;
  const escalated = a.escalated_cases || 0;
  // "Diagnosed" = entered the pipeline (not rejected/invalid)
  const rejected  = (cases || []).filter(c => c.case_status === "rejected" || c.case_status === "invalid").length;
  const diagnosed = total - rejected;
  // "Strategy set" = has a non-new status (everything that got past diagnosis)
  const strategySet = (cases || []).filter(c =>
    !["new", "rejected", "invalid"].includes(c.case_status)).length;

  const amtAtRisk     = a.amount_at_risk     || 0;
  const amtRecovered  = a.amount_recovered   || 0;
  const amtEscalated  = (cases || [])
    .filter(c => c.case_status === "escalated" || c.case_status === "broken_promise")
    .reduce((sum, c) => sum + (c.amount || 0), 0);

  const set = (id, val) => { const el = document.getElementById(id); if (el) el.textContent = val; };
  set("fn-count-failed",    total);
  set("fn-amt-failed",      rupees(amtAtRisk));
  set("fn-count-diagnosed", diagnosed);
  set("fn-count-strategy",  strategySet);
  set("fn-count-recovered", recovered);
  set("fn-amt-recovered",   rupees(amtRecovered));
  set("fn-count-escalated", escalated);
  set("fn-amt-escalated",   rupees(amtEscalated));
}

// --- Live Recovery Counter --------------------------------------------------
// Accumulated during the SSE run stream.

let _lrAmount   = 0;
let _lrCases    = 0;
let _lrEscalated = 0;
let _lrRejected = 0;

function resetLiveRecoveryCounter() {
  _lrAmount = 0; _lrCases = 0; _lrEscalated = 0; _lrRejected = 0;
  const card = document.getElementById("live-recovery-card");
  if (card) card.classList.remove("hidden");
  _updateLiveRecovery();
}

function _updateLiveRecovery() {
  const set = (id, val) => { const el = document.getElementById(id); if (el) el.textContent = val; };
  set("lr-amount",    rupees(_lrAmount));
  set("lr-cases",     _lrCases);
  set("lr-escalated", _lrEscalated);
  set("lr-rejected",  _lrRejected);
}

// Call this once per trace event received from the SSE stream.
function updateLiveRecoveryFromTrace(trace) {
  if (!trace || trace.done) return;
  const caseData = allCasesCache.find(c => c.customer_id === trace.customer_id);
  const amount   = caseData ? (caseData.amount || 0) : 0;
  const status   = trace.final_status || "";
  if (status === "recovered") { _lrCases += 1; _lrAmount += amount; }
  else if (status === "escalated" || status === "broken_promise") { _lrEscalated += 1; }
  else if (status === "rejected" || status === "invalid") { _lrRejected += 1; }
  _updateLiveRecovery();
}

// Patch into the existing drain loop by intercepting per-trace updates.
// Direct call from the drain loop (via the try/catch block) replaces the
// MutationObserver approach which could fire spuriously on no-op textContent sets.
// The observer below is kept for safety but does nothing if the drain loop calls
// updateLiveRecoveryFromTrace directly (which it does when the card is built).
(function patchLiveCounter() {
  const target = document.getElementById("lc-recovered");
  if (!target) return;
  let _lastVal = "";
  const obs = new MutationObserver(() => {
    // Only act when the value actually changed (avoids spurious no-op triggers).
    const newVal = target.textContent;
    if (newVal === _lastVal) return;
    _lastVal = newVal;
    const firstCard = document.querySelector("#live-feed .feed-card");
    if (!firstCard) return;
    const cidEl = firstCard.querySelector("[data-cid]");
    if (!cidEl) return;
    const cid = cidEl.dataset.cid;
    if (!cid) return;
    const statusEl = firstCard.querySelector("[data-final-status]");
    if (!statusEl) return;
    const status = statusEl.dataset.finalStatus;
    updateLiveRecoveryFromTrace({ customer_id: cid, final_status: status });
  });
  obs.observe(target, { childList: true, characterData: true, subtree: true });
})();


// --- Webhook Inspector ------------------------------------------------------

function renderWebhookInspector(cases, auditData) {
  const tbody = document.getElementById("webhook-inspector-tbody");
  const wrap  = document.getElementById("webhook-inspector-table-wrap");
  const empty = document.getElementById("webhook-inspector-empty");
  if (!tbody || !wrap) return;

  // Build a combined list from audit_log events of webhook-related types.
  // Use /api/activity data if available, otherwise derive from cases.
  const webhookEventTypes = new Set(["webhook_received", "webhook_rejected", "webhook_duplicate", "webhook_invalid"]);
  let events = [];

  if (auditData && auditData.events) {
    events = auditData.events.filter(e => webhookEventTypes.has(e.event_type));
  }

  // If no audit activity data, fall back to deriving from cases.
  if (events.length === 0 && cases && cases.length > 0) {
    const processed = cases.filter(c => c.case_status !== "new");
    if (processed.length === 0) {
      if (empty) empty.classList.remove("hidden");
      wrap.classList.add("hidden");
      return;
    }
    // Show a summary row per case from the cases list.
    tbody.innerHTML = "";
    processed.slice(0, 80).forEach(c => {
      const isRejected = c.case_status === "rejected";
      const isReal     = c.source === "razorpay_live";
      const tr = document.createElement("tr");
      tr.innerHTML =
        `<td><code class="num" style="font-size:0.78rem">${titleCase(c.raw_event_type || "payment.failed")}</code></td>` +
        `<td class="num">${maskId(c.customer_id)}</td>` +
        `<td class="num">${rupees(c.amount)}</td>` +
        `<td>${isRejected
          ? '<span class="wi-badge wi-badge-bad">✗ Invalid</span>'
          : '<span class="wi-badge wi-badge-ok">✓ Verified</span>'}</td>` +
        `<td class="${isReal ? "wi-source-real" : "wi-source-synth"}">${isReal ? "Razorpay live" : "Synthetic"}</td>` +
        `<td>${badgeForStatus(c.case_status)}</td>` +
        `<td class="num" style="font-size:0.72rem;color:var(--text-secondary)">${c.failure_date || "—"}</td>`;
      tbody.appendChild(tr);
    });
    wrap.classList.remove("hidden");
    if (empty) empty.classList.add("hidden");
    return;
  }

  if (events.length === 0) {
    if (empty) empty.classList.remove("hidden");
    wrap.classList.add("hidden");
    return;
  }

  tbody.innerHTML = "";
  events.slice(0, 80).forEach(e => {
    const isRejected = e.event_type === "webhook_rejected";
    const isDuplicate = e.event_type === "webhook_duplicate";
    const caseData = (cases || []).find(c => c.customer_id === e.customer_id);
    const isReal = caseData && caseData.source === "razorpay_live";
    const tr = document.createElement("tr");
    tr.innerHTML =
      `<td><code class="num" style="font-size:0.78rem">${e.event_type}</code></td>` +
      `<td class="num">${maskId(e.customer_id)}</td>` +
      `<td class="num">${caseData ? rupees(caseData.amount) : "—"}</td>` +
      `<td>${isRejected
          ? '<span class="wi-badge wi-badge-bad">✗ Rejected</span>'
          : isDuplicate
          ? '<span class="wi-badge wi-badge-dup">↺ Duplicate</span>'
          : '<span class="wi-badge wi-badge-ok">✓ Verified</span>'
        }</td>` +
      `<td class="${isReal ? "wi-source-real" : "wi-source-synth"}">${isReal ? "Razorpay live" : "Synthetic"}</td>` +
      `<td>${badgeForStatus(e.case_status_after || "—")}</td>` +
      `<td class="num" style="font-size:0.72rem;color:var(--text-secondary)">${(e.event_timestamp || "").slice(0, 19)}</td>`;
    tbody.appendChild(tr);
  });
  wrap.classList.remove("hidden");
  if (empty) empty.classList.add("hidden");
}

function badgeForStatus(status) {
  const map = {
    recovered: ["accent-ok", "✓ Recovered"],
    escalated: ["accent-bad", "↑ Escalated"],
    rejected:  ["accent-bad", "✗ Rejected"],
    invalid:   ["accent-bad", "✗ Invalid"],
    new:       ["muted", "New"],
    promised:  ["accent", "Promised"],
    broken_promise: ["accent-bad", "Broken promise"],
  };
  const [cls, label] = map[status] || ["muted", titleCase(status)];
  return `<span class="${cls}" style="font-size:0.78rem;font-weight:600">${label}</span>`;
}


// --- Case Replay ------------------------------------------------------------

let replayAuditTrail  = [];
let replayCurrentStep = -1;
let replayAutoTimer   = null;

function initReplayView() {
  const searchInput = document.getElementById("replay-search");
  const searchBtn   = document.getElementById("replay-search-btn");
  if (!searchInput || !searchBtn) return;

  searchBtn.addEventListener("click", () => runReplaySearch());
  searchInput.addEventListener("keydown", e => { if (e.key === "Enter") runReplaySearch(); });

  document.getElementById("replay-prev")?.addEventListener("click", () => replayStep(-1));
  document.getElementById("replay-next")?.addEventListener("click", () => replayStep(+1));
  document.getElementById("replay-reset")?.addEventListener("click", () => resetReplay());
  document.getElementById("replay-auto")?.addEventListener("click", () => toggleReplayAuto());
}

function runReplaySearch() {
  const q = (document.getElementById("replay-search")?.value || "").trim().toLowerCase();
  const empty = document.getElementById("replay-empty");
  const listEl = document.getElementById("replay-case-list");

  if (!allCasesCache || allCasesCache.length === 0) {
    if (empty) { empty.textContent = "Run the agent first, then search for a case to replay."; empty.classList.remove("hidden"); }
    return;
  }

  // Filter cache; skip 'new' (no audit trail yet).
  const results = allCasesCache.filter(c => {
    if (c.case_status === "new") return false;
    if (!q) return true;
    return (c.customer_id || "").toLowerCase().includes(q) ||
           (c.failure_reason || "").toLowerCase().includes(q) ||
           (c.case_status || "").toLowerCase().includes(q) ||
           (c.merchant_category || "").toLowerCase().includes(q);
  });

  if (results.length === 0) {
    if (empty) { empty.textContent = "No cases found. Try a different search or run the agent first."; empty.classList.remove("hidden"); }
    if (listEl) listEl.classList.add("hidden");
    return;
  }
  if (empty) empty.classList.add("hidden");

  if (listEl) {
    listEl.innerHTML = "";
    results.slice(0, 40).forEach(c => {
      const item = document.createElement("div");
      item.className = "replay-case-item";
      item.innerHTML =
        `<div class="replay-case-item-left">` +
        `<span class="replay-case-id num">${maskId(c.customer_id)}</span>` +
        `<span class="replay-case-reason">${titleCase(c.failure_reason)} · ${rupees(c.amount)}</span>` +
        `</div>` +
        `<span class="replay-case-status ${c.case_status === "recovered" ? "accent-ok" : c.case_status === "escalated" ? "accent-bad" : ""}">${titleCase(c.case_status)}</span>`;
      item.addEventListener("click", () => loadReplayCase(c));
      listEl.appendChild(item);
    });
    listEl.classList.remove("hidden");
  }
}

async function loadReplayCase(caseData) {
  const listEl    = document.getElementById("replay-case-list");
  const timeline  = document.getElementById("replay-timeline");
  const empty     = document.getElementById("replay-empty");

  if (listEl) listEl.classList.add("hidden");
  if (timeline) timeline.classList.remove("hidden");
  if (empty)    empty.classList.add("hidden");
  stopReplayAuto();

  // Fetch the full audit trail.
  try {
    const resp = await getJSON(`/api/cases/${encodeURIComponent(caseData.customer_id)}/audit`);
    replayAuditTrail  = resp.audit || [];
    replayCurrentStep = 0;

    renderReplayCaseHeader(resp.case || caseData);
    renderReplaySteps();
    updateReplayControls();
  } catch (err) {
    if (empty) { empty.textContent = "Could not load audit trail: " + err.message; empty.classList.remove("hidden"); }
    if (timeline) timeline.classList.add("hidden");
  }
}

function renderReplayCaseHeader(c) {
  const el = document.getElementById("replay-case-header");
  if (!el) return;
  const statusCls = c.case_status === "recovered" ? "accent-ok" : c.case_status === "escalated" ? "accent-bad" : "";
  el.innerHTML =
    `<span class="rch-id num">${maskId(c.customer_id)}</span>` +
    `<span class="rch-reason">${titleCase(c.failure_reason)}</span>` +
    `<span class="rch-amount">${rupees(c.amount)}</span>` +
    `<span class="rch-status ${statusCls}">${titleCase(c.case_status)}</span>` +
    (c.score != null ? `<span class="muted" style="font-size:0.8rem">Score ${c.score}/100</span>` : "");
}

function renderReplaySteps() {
  const container = document.getElementById("replay-steps");
  if (!container) return;
  container.innerHTML = "";

  replayAuditTrail.forEach((step, idx) => {
    const div = document.createElement("div");
    div.className = "replay-step" + (idx < replayCurrentStep ? " done" : idx === replayCurrentStep ? " active" : "");
    div.dataset.step = idx;

    const outcomeCls = step.outcome === "recovered" ? "ok"
      : (step.outcome === "escalated" || step.outcome === "rejected" || step.outcome === "n/a" && step.event_type.includes("reject")) ? "bad"
      : "neu";

    div.innerHTML =
      `<div>` +
      `<div class="replay-step-event">${titleCase(step.event_type)}</div>` +
      `<div class="replay-step-action">${step.action_taken || "—"}</div>` +
      `<div class="replay-step-reason">${(step.reasoning_text || "").slice(0, 200)}</div>` +
      `<div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:4px">` +
      `<span class="replay-step-outcome ${outcomeCls}">${step.outcome || "—"}</span>` +
      (step.attempt_number > 0 ? `<span class="replay-step-outcome neu">Attempt ${step.attempt_number}</span>` : "") +
      `</div>` +
      `<div class="replay-step-ts">${(step.event_timestamp || "").slice(0, 19)}</div>` +
      `</div>`;
    container.appendChild(div);
  });

  // Scroll active step into view.
  const active = container.querySelector(".replay-step.active");
  if (active) active.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

function replayStep(delta) {
  const max = replayAuditTrail.length - 1;
  replayCurrentStep = Math.max(0, Math.min(max, replayCurrentStep + delta));
  renderReplaySteps();
  updateReplayControls();
}

function updateReplayControls() {
  const max    = replayAuditTrail.length - 1;
  const prev   = document.getElementById("replay-prev");
  const next   = document.getElementById("replay-next");
  const counter = document.getElementById("replay-step-counter");
  if (prev)    prev.disabled    = replayCurrentStep <= 0;
  if (next)    next.disabled    = replayCurrentStep >= max;
  if (counter) counter.textContent = `Step ${replayCurrentStep + 1} / ${max + 1}`;
}

function toggleReplayAuto() {
  const btn = document.getElementById("replay-auto");
  if (replayAutoTimer) {
    stopReplayAuto();
    if (btn) btn.textContent = "▶ Auto-play";
  } else {
    if (btn) btn.textContent = "⏸ Pause";
    replayAutoTimer = setInterval(() => {
      if (replayCurrentStep >= replayAuditTrail.length - 1) {
        stopReplayAuto();
        if (btn) btn.textContent = "▶ Auto-play";
        return;
      }
      replayStep(+1);
    }, 1200);
  }
}

function stopReplayAuto() {
  if (replayAutoTimer) { clearInterval(replayAutoTimer); replayAutoTimer = null; }
}

function resetReplay() {
  stopReplayAuto();
  replayAuditTrail  = [];
  replayCurrentStep = -1;
  const timeline = document.getElementById("replay-timeline");
  const empty    = document.getElementById("replay-empty");
  const listEl   = document.getElementById("replay-case-list");
  if (timeline) timeline.classList.add("hidden");
  if (listEl)   listEl.classList.add("hidden");
  if (empty) { empty.textContent = "Run the agent first, then search for a case to replay its recovery journey."; empty.classList.remove("hidden"); }
  const searchInput = document.getElementById("replay-search");
  if (searchInput) searchInput.value = "";
}


// --- Hook new features into loadDashboard -----------------------------------
// We patch in by appending to the DOMContentLoaded-registered call chain.
// The existing loadDashboard() fetches /api/activity, cases, metrics — we read
// what it already loaded from the DOM rather than making extra API calls.

const _origLoadDashboard = window._origLoadDashboard || null;

// Extend renderMetrics to also render the funnel.
const _origRenderMetrics = typeof renderMetrics === "function" ? renderMetrics : null;
if (_origRenderMetrics) {
  window.renderMetrics = function(data) {
    _origRenderMetrics.call(this, data);
    // Funnel rendered separately in loadDashboard patch below.
  };
}

// Listen for the loadDashboard cycle to fire the new renderers.
// We use a custom event dispatched after loadDashboard completes.
document.addEventListener("mandateRescueDashboardLoaded", (e) => {
  const { metricsData, cases, activityData } = e.detail || {};
  if (metricsData && cases) renderFunnel(metricsData, cases);
  if (cases) renderWebhookInspector(cases, activityData);
});

// --- Initialize new UI on DOMContentLoaded extension -----------------------
// (appended after the main DOMContentLoaded handler in this file)
document.addEventListener("DOMContentLoaded", () => {
  initReplayView();

  // Reset the live recovery counter whenever a run starts.
  // Hook by observing the live-panel becoming visible.
  const livePanel = document.getElementById("live-panel");
  if (livePanel) {
    new MutationObserver((mutations) => {
      mutations.forEach(m => {
        if (m.type === "attributes" && m.attributeName === "class") {
          if (!livePanel.classList.contains("hidden")) {
            resetLiveRecoveryCounter();
          }
        }
      });
    }).observe(livePanel, { attributes: true });
  }
});


// =============================================================================
// PHASE 4 — Execution panel renderer, execution status card, scheduler UI
// =============================================================================

// --- Job status badge -------------------------------------------------------
function jobStatusBadge(status) {
  const map = {
    scheduled:  ["neutral",  "Scheduled"],
    claimed:    ["info",     "Claimed"],
    executing:  ["info",     "Executing"],
    succeeded:  ["ok",       "Succeeded"],
    failed:     ["bad",      "Failed"],
    exhausted:  ["bad",      "Exhausted"],
    cancelled:  ["neutral",  "Cancelled"],
  };
  const [cls, label] = map[status] || ["neutral", titleCase(status)];
  return el("span", "badge " + cls, label);
}

// --- Execution mode badge ---------------------------------------------------
function execModeBadge(mode) {
  if (mode === "real_test") {
    const b = el("span", "badge badge-exec-real", "\u26A1 Razorpay Test Mode");
    b.title = "This attempt was executed against the real Razorpay Test API.";
    return b;
  }
  const b = el("span", "badge badge-exec-sim", "Simulation");
  b.title = "This attempt was executed via internal RNG simulation (synthetic / benchmark).";
  return b;
}

// --- Render recovery jobs panel (in drawer) --------------------------------
function renderExecutionPanel(jobs) {
  const wrap = el("div", "exec-panel");

  jobs.forEach((job, idx) => {
    const card = el("div", "exec-job-card");

    // Header row: attempt # + mode + status
    const header = el("div", "exec-job-header");
    header.appendChild(el("span", "exec-attempt-label",
      "Attempt " + job.attempt_number + " of " + job.max_retries));
    header.appendChild(execModeBadge(job.execution_mode));
    header.appendChild(jobStatusBadge(job.status));
    card.appendChild(header);

    // Timing row
    const timing = el("div", "exec-timing");
    if (job.scheduled_at) {
      timing.appendChild(el("span", "exec-timing-item",
        "Scheduled: " + job.scheduled_at.replace("T", " ").slice(0, 16)));
    }
    if (job.executed_at) {
      timing.appendChild(el("span", "exec-timing-item",
        "Executed: " + job.executed_at.replace("T", " ").slice(0, 16)));
    }
    if (timing.childNodes.length) card.appendChild(timing);

    // Outcome
    if (job.outcome) {
      const outcomeRow = el("div", "exec-outcome");
      const outcomeLabel = titleCase(job.outcome.replace(/_/g, " "));
      const outcomeCls = (job.status === "succeeded")
        ? "exec-outcome-ok"
        : (job.status === "failed" || job.status === "exhausted")
          ? "exec-outcome-bad"
          : "exec-outcome-neutral";
      outcomeRow.appendChild(el("span", outcomeCls, outcomeLabel));
      card.appendChild(outcomeRow);
    }

    // Real Razorpay IDs — only shown when present (real_test mode).
    if (job.razorpay_payment_id) {
      const idRow = el("div", "exec-id-row");
      idRow.appendChild(el("span", "exec-id-label", "Payment ID:"));
      idRow.appendChild(el("code", "exec-id-value", job.razorpay_payment_id));
      card.appendChild(idRow);
    }
    if (job.razorpay_payment_link_id) {
      const idRow = el("div", "exec-id-row");
      idRow.appendChild(el("span", "exec-id-label", "Payment link ID:"));
      idRow.appendChild(el("code", "exec-id-value", job.razorpay_payment_link_id));
      card.appendChild(idRow);
    }
    if (job.payment_link_url) {
      const linkRow = el("div", "exec-id-row");
      linkRow.appendChild(el("span", "exec-id-label", "Customer link:"));
      const a = el("a", "exec-link-url", job.payment_link_url);
      a.href = job.payment_link_url;
      a.target = "_blank";
      a.rel = "noopener noreferrer";
      a.title = "Opens Razorpay-hosted payment page (test mode)";
      linkRow.appendChild(a);
      card.appendChild(linkRow);
    }

    // Failure reason (for failed jobs only)
    if (job.failure_reason && job.status !== "succeeded") {
      const failRow = el("div", "exec-failure-reason");
      failRow.appendChild(el("span", "muted", job.failure_reason));
      card.appendChild(failRow);
    }

    wrap.appendChild(card);
  });

  return wrap;
}

// --- Execution status card (Overview) --------------------------------------
// Loaded once after dashboard loads. Shows credential status + job summary.
async function loadExecutionStatusCard() {
  const card = document.getElementById("execution-status-card");
  if (!card) return;
  try {
    const data = await getJSON("/api/execution/status");
    renderExecutionStatusCard(card, data);
    card.classList.remove("hidden");
  } catch (e) {
    card.classList.add("hidden");
  }
}

function renderExecutionStatusCard(card, data) {
  const rzp = data.razorpay || {};
  const jobs = data.jobs || {};
  card.innerHTML = "";

  // Header
  const head = el("div", "card-head");
  const h2 = el("h2", "card-title", "Recovery execution");
  head.appendChild(h2);

  // Credential status badge
  let credBadge;
  if (rzp.authenticated) {
    credBadge = el("span", "badge ok",
      "\u26A1 Razorpay Test Mode " + (rzp.mode === "test" ? "(test)" : ""));
    credBadge.title = "Real Razorpay Test API credentials are configured and verified.";
  } else if (rzp.configured) {
    credBadge = el("span", "badge warn", "Credentials not verified");
    credBadge.title = rzp.error || "Keys are set but could not be verified.";
  } else {
    credBadge = el("span", "badge neutral", "Simulation only");
    credBadge.title =
      "RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET not configured. " +
      "All recovery runs use the RNG simulation path.";
  }
  head.appendChild(credBadge);
  card.appendChild(head);

  // Job summary row
  if (jobs.total > 0) {
    const summary = el("div", "exec-summary-row");

    const addStat = (label, value, cls) => {
      const item = el("div", "exec-stat-item");
      item.appendChild(el("span", "exec-stat-value num " + (cls || ""), String(value)));
      item.appendChild(el("span", "exec-stat-label", label));
      summary.appendChild(item);
    };

    addStat("Total jobs", jobs.total);
    addStat("Scheduled", jobs.scheduled || 0);
    addStat("Succeeded", jobs.succeeded || 0, "accent-ok");
    if ((jobs.failed || 0) + (jobs.exhausted || 0) > 0) {
      addStat("Failed", (jobs.failed || 0) + (jobs.exhausted || 0), "accent-bad");
    }

    // Mode breakdown
    const byMode = jobs.by_mode || {};
    if (byMode.real_test > 0) {
      addStat("Real Test Mode", byMode.real_test || 0, "accent");
    }
    if (byMode.simulation > 0) {
      addStat("Simulation", byMode.simulation || 0);
    }

    card.appendChild(summary);
  } else {
    card.appendChild(el("p", "card-sub",
      "No recovery jobs yet. Run the agent to generate recovery attempts."));
  }

  // Limitation note when in simulation mode.
  if (!rzp.authenticated) {
    const note = el("div", "exec-limitation-note");
    note.innerHTML =
      "<strong>Simulation mode:</strong> All recovery attempts use the RNG simulation. " +
      "To enable real Razorpay Test Mode execution, set " +
      "<code>RAZORPAY_KEY_ID</code> and <code>RAZORPAY_KEY_SECRET</code> in <code>.env</code> " +
      "with your Razorpay test-mode API keys.";
    card.appendChild(note);
  }
}

// Wire execution status load into dashboard load cycle.
document.addEventListener("mandateRescueDashboardLoaded", () => {
  loadExecutionStatusCard();
});


// =============================================================================
// PHASE 5 — Revenue Intelligence, Risk, Anomaly, Analytics view
// =============================================================================

// ---------------------------------------------------------------------------
// Anomaly alerts card (Overview)
// ---------------------------------------------------------------------------
async function loadAnomalyAlerts() {
  const card = document.getElementById("anomaly-alerts-card");
  if (!card) return;
  try {
    const data = await getJSON("/api/anomalies");
    if (!data || !data.alerts || data.alerts.length === 0) {
      card.classList.add("hidden");
      return;
    }
    renderAnomalyAlertsCard(card, data);
    card.classList.remove("hidden");
  } catch (e) {
    card.classList.add("hidden");
  }
}

function severityBadge(sev) {
  const map = {
    critical: ["bad",  "Critical"],
    warning:  ["warn", "Warning"],
    info:     ["v2",   "Info"],
  };
  const [cls, label] = map[sev] || ["neutral", sev];
  return el("span", "badge " + cls, label);
}

function renderAnomalyAlertsCard(card, data) {
  card.innerHTML = "";

  const head = el("div", "card-head");
  const titleWrap = el("div");
  const h2 = el("h2", "card-title");
  h2.textContent = "Anomaly alerts";
  titleWrap.appendChild(h2);
  head.appendChild(titleWrap);
  const critCount = data.alerts.filter(a => a.severity === "critical").length;
  const badge = el("span", "badge " + (critCount > 0 ? "bad" : "warn"),
    data.total + " alert" + (data.total !== 1 ? "s" : ""));
  head.appendChild(badge);
  card.appendChild(head);

  const list = el("div", "anomaly-list");
  data.alerts.slice(0, 4).forEach(alert => {
    const row = el("div", "anomaly-row anomaly-" + alert.severity);
    const rowHead = el("div", "anomaly-row-head");
    rowHead.appendChild(severityBadge(alert.severity));
    rowHead.appendChild(el("span", "anomaly-title", alert.title));
    row.appendChild(rowHead);
    row.appendChild(el("p", "anomaly-desc", alert.description));
    if (alert.recommended_action) {
      const action = el("p", "anomaly-action");
      action.innerHTML = "<strong>Action:</strong> " + escapeHtml(alert.recommended_action);
      row.appendChild(action);
    }
    list.appendChild(row);
  });
  card.appendChild(list);

  if (data.total > 4) {
    const more = el("p", "muted", "+" + (data.total - 4) + " more alerts. See Analytics view for full details.");
    more.style.cssText = "margin:8px 0 0;font-size:12px;";
    card.appendChild(more);
  }
}

function escapeHtml(str) {
  return String(str)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

// ---------------------------------------------------------------------------
// Risk summary card (Overview)
// ---------------------------------------------------------------------------
async function loadRiskSummary() {
  const card = document.getElementById("risk-summary-card");
  if (!card) return;
  try {
    const data = await getJSON("/api/risk/summary?limit=5");
    if (!data || data.active_cases === 0) {
      card.classList.add("hidden");
      return;
    }
    renderRiskSummaryCard(card, data);
    card.classList.remove("hidden");
  } catch (e) {
    card.classList.add("hidden");
  }
}

function riskScoreBadge(score) {
  const cls = score >= 70 ? "bad" : score >= 45 ? "warn" : "v2";
  return el("span", "badge " + cls, score + " risk");
}

function renderRiskSummaryCard(card, data) {
  card.innerHTML = "";

  const head = el("div", "card-head");
  const titleWrap = el("div");
  const h2 = el("h2", "card-title", "Revenue at risk");
  titleWrap.appendChild(h2);
  const sub = el("p", "card-sub",
    rupees(data.total_amount_at_risk) + " across " + data.active_cases + " active cases. " +
    "Estimated unrecovered: " + rupees(data.expected_unrecovered) + " [ESTIMATE]");
  titleWrap.appendChild(sub);
  head.appendChild(titleWrap);
  head.appendChild(el("span", "badge badge-exec-sim", "Estimate"));
  card.appendChild(head);

  // Severity breakdown
  const sev = data.summary_by_severity || {};
  const sevRow = el("div", "risk-severity-row");
  const sevOrder = [["critical", "bad"], ["high", "bad"], ["medium", "warn"], ["low", "v2"]];
  sevOrder.forEach(([key, cls]) => {
    if (!sev[key]) return;
    const item = el("div", "risk-sev-item");
    item.appendChild(el("span", "badge " + cls, key));
    item.appendChild(el("span", "risk-sev-count num", String(sev[key].count)));
    item.appendChild(el("span", "risk-sev-amt muted", rupees(sev[key].amount)));
    sevRow.appendChild(item);
  });
  if (sevRow.childNodes.length) card.appendChild(sevRow);

  // Top risks table
  if (data.top_risks && data.top_risks.length > 0) {
    const tbl = el("table", "cases-table");
    const thead = el("thead");
    thead.innerHTML = "<tr><th>Customer</th><th>Failure reason</th><th>Amount</th><th>Risk</th><th>Intervention</th></tr>";
    tbl.appendChild(thead);
    const tbody = el("tbody");
    data.top_risks.forEach(r => {
      const tr = el("tr");
      tr.style.cursor = "pointer";
      tr.addEventListener("click", () => {
        showView("cases");
        setTimeout(() => openDrawer(r.customer_id), 100);
      });
      tr.appendChild(el("td", "num", maskId(r.customer_id)));
      const reasonTd = el("td");
      reasonTd.appendChild(el("span", "tag reason", titleCase(r.failure_reason)));
      tr.appendChild(reasonTd);
      tr.appendChild(el("td", "num", rupees(r.amount)));
      const riskTd = el("td");
      riskTd.appendChild(riskScoreBadge(r.risk_score));
      tr.appendChild(riskTd);
      tr.appendChild(el("td", "muted", r.intervention_window ? r.intervention_window.label : "\u2014"));
      tbody.appendChild(tr);
    });
    tbl.appendChild(tbody);
    const wrap = el("div", "table-wrap");
    wrap.style.marginTop = "12px";
    wrap.appendChild(tbl);
    card.appendChild(wrap);
  }
}

// ---------------------------------------------------------------------------
// Analytics view loader
// ---------------------------------------------------------------------------
async function loadAnalyticsView() {
  // Load all data in parallel
  const [failureData, strategyData, incrementalData, merchantData] = await Promise.allSettled([
    getJSON("/api/intelligence/by-failure-reason"),
    getJSON("/api/intelligence/by-strategy"),
    getJSON("/api/intelligence/incremental-revenue"),
    getJSON("/api/intelligence/merchant-learning"),
  ]);

  if (failureData.status === "fulfilled") renderByFailureReason(failureData.value);
  if (strategyData.status === "fulfilled") renderByStrategy(strategyData.value);
  if (incrementalData.status === "fulfilled") renderIncrementalRevenue(incrementalData.value);
  if (merchantData.status === "fulfilled") renderMerchantLearning(merchantData.value);
}

function renderByFailureReason(data) {
  const container = document.getElementById("by-failure-reason-body");
  if (!container) return;
  const rows = (data && data.by_failure_reason) || [];
  if (!rows.length) { container.innerHTML = "<div class='view-empty muted'>No data yet.</div>"; return; }

  const tbl = el("table", "cases-table");
  tbl.innerHTML =
    "<thead><tr>" +
    "<th>Failure reason</th><th>Cases</th><th>Recovered</th>" +
    "<th>Recovery rate</th><th>Model prior</th><th>Delta</th><th>Amount lost</th>" +
    "</tr></thead>";
  const tbody = el("tbody");
  rows.forEach(r => {
    const tr = el("tr");
    tr.style.cursor = "default";
    tr.appendChild(el("td", null, titleCase(r.segment)));
    tr.appendChild(el("td", "num", String(r.total)));
    tr.appendChild(el("td", "num", String(r.recovered)));

    // Recovery rate with bar
    const rateTd = el("td");
    const rateWrap = el("div");
    rateWrap.style.cssText = "display:flex;align-items:center;gap:8px;";
    rateWrap.appendChild(el("span", "num", (r.recovery_rate * 100).toFixed(1) + "%"));
    const bar = el("div", "bar");
    bar.style.width = "60px";
    const fill = el("span");
    fill.style.width = (r.recovery_rate * 100).toFixed(1) + "%";
    bar.appendChild(fill);
    rateWrap.appendChild(bar);
    rateTd.appendChild(rateWrap);
    tr.appendChild(rateTd);

    // Prior
    const priorTd = el("td", "muted num",
      r.recoverability_prior != null ? (r.recoverability_prior * 100).toFixed(0) + "%" : "\u2014");
    priorTd.title = r.prior_label || "";
    tr.appendChild(priorTd);

    // Delta
    const delta = r.prior_vs_actual_delta;
    const deltaTd = el("td");
    if (delta != null) {
      const span = el("span", "num " + (delta >= 0 ? "accent-ok" : "accent-bad"),
        (delta >= 0 ? "+" : "") + (delta * 100).toFixed(1) + "pp");
      deltaTd.appendChild(span);
    } else {
      deltaTd.appendChild(el("span", "muted", "n/a"));
    }
    tr.appendChild(deltaTd);

    tr.appendChild(el("td", "num accent-bad", rupees(r.amount_lost)));
    tbody.appendChild(tr);
  });
  tbl.appendChild(tbody);
  container.innerHTML = "";
  const wrap = el("div", "table-wrap");
  wrap.appendChild(tbl);
  container.appendChild(wrap);
}

function renderByStrategy(data) {
  const container = document.getElementById("by-strategy-body");
  if (!container) return;
  const rows = (data && data.by_strategy) || [];
  if (!rows.length) { container.innerHTML = "<div class='view-empty muted'>No data yet.</div>"; return; }

  const tbl = el("table", "cases-table");
  tbl.innerHTML =
    "<thead><tr>" +
    "<th>Strategy</th><th>Cases</th><th>Recovery rate</th><th>Amount recovered</th><th>Data</th>" +
    "</tr></thead>";
  const tbody = el("tbody");
  rows.forEach(r => {
    const tr = el("tr");
    tr.style.cursor = "default";
    tr.appendChild(el("td", null, r.strategy));
    tr.appendChild(el("td", "num", String(r.total)));

    // Rate with colour
    const rateCls = r.recovery_rate >= 0.8 ? "accent-ok" : r.recovery_rate >= 0.5 ? "" : "accent-bad";
    tr.appendChild(el("td", "num " + rateCls, (r.recovery_rate * 100).toFixed(1) + "%"));
    tr.appendChild(el("td", "num accent-ok", rupees(r.amount_recovered)));

    const qualTd = el("td");
    qualTd.appendChild(el("span",
      r.sufficient_sample ? "badge ok" : "badge neutral",
      r.sufficient_sample ? "Actual" : "Low sample"));
    tr.appendChild(qualTd);
    tbody.appendChild(tr);
  });
  tbl.appendChild(tbody);
  container.innerHTML = "";
  const wrap = el("div", "table-wrap");
  wrap.appendChild(tbl);
  container.appendChild(wrap);
}

function renderIncrementalRevenue(data) {
  const container = document.getElementById("incremental-revenue-body");
  if (!container) return;
  if (!data || !data.actual) {
    container.innerHTML = "<div class='view-empty muted'>No data yet.</div>";
    return;
  }

  const grid = el("div", "incremental-grid");

  const addCard = (label, amount, rate, badgeText, badgeCls, note) => {
    const card = el("div", "incremental-card");
    card.appendChild(el("div", "incremental-label", label));
    card.appendChild(el("div", "incremental-amount num", rupees(amount)));
    card.appendChild(el("div", "muted num", (rate * 100).toFixed(1) + "% recovery rate"));
    if (badgeText) {
      card.appendChild(el("span", "badge " + badgeCls, badgeText));
    }
    if (note) {
      const noteEl = el("p", "muted", note);
      noteEl.style.cssText = "font-size:11px;margin-top:6px;";
      card.appendChild(noteEl);
    }
    grid.appendChild(card);
  };

  addCard("Actual (Mandate Rescue agent)",
    data.actual.amount_recovered, data.actual.recovery_rate,
    "Actual", "ok", null);
  addCard("Dumb persistence baseline",
    data.dumb_persistence_baseline.amount_recovered,
    data.dumb_persistence_baseline.recovery_rate,
    "Estimate", "warn",
    data.dumb_persistence_baseline.label);
  addCard("Naive baseline (1 attempt)",
    data.naive_baseline_1_attempt.amount_recovered,
    data.naive_baseline_1_attempt.recovery_rate,
    "Estimate", "warn",
    data.naive_baseline_1_attempt.label);

  container.innerHTML = "";
  container.appendChild(grid);

  if (data.incremental) {
    const inc = data.incremental;
    const incCard = el("div", "incremental-summary");
    incCard.innerHTML =
      "<strong>Incremental vs dumb persistence:</strong> " +
      "<span class='num " + (inc.vs_dumb_persistence >= 0 ? "accent-ok" : "accent-bad") + "'>" +
      rupees(inc.vs_dumb_persistence) + "</span>" +
      " <span class='badge badge-exec-sim'>ESTIMATE — counterfactual</span>" +
      "<p class='muted' style='margin-top:6px;font-size:12px;'>" +
      escapeHtml(inc.interpretation) + "</p>";
    container.appendChild(incCard);
  }
}

function renderMerchantLearning(data) {
  const container = document.getElementById("merchant-learning-body");
  if (!container) return;
  const merchants = (data && data.merchants) || [];
  const withData = merchants.filter(m => m.sufficient_data);
  if (!withData.length) {
    container.innerHTML = "<div class='view-empty muted'>Insufficient data for merchant-specific recommendations yet.</div>";
    return;
  }

  const grid = el("div", "merchant-grid");
  withData.forEach(m => {
    const card = el("div", "merchant-card");
    const h3 = el("h3", null, titleCase(m.merchant_category));
    h3.style.cssText = "font-size:14px;margin:0 0 8px;";
    card.appendChild(h3);
    card.appendChild(el("p", "muted",
      m.total_cases + " cases \u00B7 best strategy:"));
    if (m.best_strategy) {
      const row = el("div");
      row.style.cssText = "display:flex;align-items:center;gap:8px;margin:4px 0 8px;";
      row.appendChild(el("span", "tag reason", m.best_strategy));
      row.appendChild(el("span", "num accent-ok",
        (m.best_strategy_recovery_rate * 100).toFixed(1) + "%"));
      row.appendChild(el("span", "muted", "n=" + m.best_strategy_sample));
      card.appendChild(row);
    }
    if (m.recommendation) {
      const rec = el("p", null);
      rec.style.cssText = "font-size:12px;color:var(--text-secondary);line-height:1.5;";
      rec.textContent = m.recommendation;
      card.appendChild(rec);
    }
    grid.appendChild(card);
  });
  container.innerHTML = "";
  container.appendChild(grid);
}

// ---------------------------------------------------------------------------
// Revenue Investigator
// ---------------------------------------------------------------------------
async function runInvestigate(question) {
  const result = document.getElementById("investigate-result");
  if (!result) return;
  result.classList.remove("hidden");
  result.innerHTML = "<div class='ask-loading'><span class='spinner'></span>Investigating\u2026</div>";

  try {
    const r = await fetch("/api/investigate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question }),
    });
    const data = await r.json();
    renderInvestigateResult(result, question, data);
  } catch (e) {
    result.innerHTML = "<div class='ask-empty'>Investigation failed: " + e.message + "</div>";
  }
}

function renderInvestigateResult(container, question, data) {
  container.innerHTML = "";

  if (!data.ok && data.question_type === "unknown") {
    container.appendChild(el("div", "ask-empty", data.answer));
    return;
  }

  // Summary answer
  const answerBlock = el("div", "investigate-answer");
  const answerText = el("p", null, data.answer);
  answerText.style.cssText = "font-size:14px;margin:0 0 8px;color:var(--text-primary);";
  answerBlock.appendChild(answerText);

  // Data type badge
  const dtBadge = data.data_type === "actual"
    ? el("span", "badge ok", "Actual data")
    : data.data_type === "mixed"
      ? el("span", "badge warn", "Mixed: actual + estimate")
      : el("span", "badge neutral", "Estimate");
  answerBlock.appendChild(dtBadge);
  container.appendChild(answerBlock);

  // Recommendation
  if (data.recommendation) {
    const recBlock = el("div", "investigate-recommendation");
    recBlock.innerHTML =
      "<strong>Recommended action:</strong> " + escapeHtml(data.recommendation);
    container.appendChild(recBlock);
  }

  // Evidence section (collapsible)
  if (data.evidence && Object.keys(data.evidence).length > 0) {
    const evSection = el("div", "investigate-evidence");
    const evTitle = el("div", "section-title", "Supporting evidence");
    evSection.appendChild(evTitle);

    // Render alert list if present
    const alerts = data.evidence.alerts;
    if (Array.isArray(alerts) && alerts.length > 0) {
      alerts.slice(0, 3).forEach(alert => {
        const row = el("div", "anomaly-row anomaly-" + alert.severity);
        row.style.marginBottom = "8px";
        const rh = el("div", "anomaly-row-head");
        rh.appendChild(severityBadge(alert.severity));
        rh.appendChild(el("span", "anomaly-title", alert.title));
        row.appendChild(rh);
        row.appendChild(el("p", "anomaly-desc", alert.description));
        evSection.appendChild(row);
      });
    }

    // Render tabular data if present
    const byReason = data.evidence.by_failure_reason;
    if (Array.isArray(byReason) && byReason.length > 0) {
      const tbl = el("table", "cases-table");
      tbl.innerHTML = "<thead><tr><th>Reason</th><th>Rate</th><th>Lost</th></tr></thead>";
      const tb = el("tbody");
      byReason.slice(0, 4).forEach(r => {
        const tr = el("tr");
        tr.style.cursor = "default";
        tr.appendChild(el("td", null, titleCase(r.segment || r.failure_reason || "")));
        tr.appendChild(el("td", "num", (r.recovery_rate * 100).toFixed(1) + "%"));
        tr.appendChild(el("td", "num accent-bad", rupees(r.amount_lost || 0)));
        tb.appendChild(tr);
      });
      tbl.appendChild(tb);
      const wrap = el("div", "table-wrap");
      wrap.style.marginTop = "8px";
      wrap.appendChild(tbl);
      evSection.appendChild(wrap);
    }

    container.appendChild(evSection);
  }

  // Results table if present (from filtered query)
  if (data.results && data.results.length > 0) {
    const sec = el("div");
    sec.appendChild(el("div", "section-title",
      data.results.length + " matching cases"));
    // Reuse the existing ask-result table style via a mini table
    const tbl = el("table", "cases-table");
    tbl.innerHTML =
      "<thead><tr><th>Customer</th><th>Amount</th><th>Status</th></tr></thead>";
    const tb = el("tbody");
    data.results.slice(0, 8).forEach(r => {
      const tr = el("tr");
      tr.addEventListener("click", () => openDrawer(r.customer_id));
      tr.appendChild(el("td", "num", maskId(r.customer_id)));
      tr.appendChild(el("td", "num", rupees(r.amount)));
      tr.appendChild(el("td", "status " + r.case_status, titleCase(r.case_status)));
      tb.appendChild(tr);
    });
    tbl.appendChild(tb);
    const wrap = el("div", "table-wrap");
    wrap.appendChild(tbl);
    sec.appendChild(wrap);
    container.appendChild(sec);
  }
}

// ---------------------------------------------------------------------------
// Wire Phase 5 into existing load cycle
// ---------------------------------------------------------------------------
document.addEventListener("mandateRescueDashboardLoaded", () => {
  // Overview cards — load in background after main dashboard data
  loadAnomalyAlerts();
  loadRiskSummary();
});

// Wire Investigator chips and input — added in Phase 5 init extension
document.addEventListener("DOMContentLoaded", () => {
  // Analytics nav click → lazy load
  document.querySelectorAll(".nav-item[data-view='analytics']").forEach(item => {
    item.addEventListener("click", () => {
      if (!_analyticsLoaded) { _analyticsLoaded = true; loadAnalyticsView(); }
    });
  });
  // Investigator input wiring
  const investigateBtn = document.getElementById("investigate-btn");
  const investigateInput = document.getElementById("investigate-input");
  if (investigateBtn && investigateInput) {
    investigateBtn.addEventListener("click", () => {
      const q = investigateInput.value.trim();
      if (q) runInvestigate(q);
    });
    investigateInput.addEventListener("keydown", e => {
      if (e.key === "Enter") {
        const q = investigateInput.value.trim();
        if (q) runInvestigate(q);
      }
    });
  }
  document.querySelectorAll("#investigate-chips .chip").forEach(chip => {
    chip.addEventListener("click", () => {
      if (investigateInput) investigateInput.value = chip.dataset.q;
      runInvestigate(chip.dataset.q);
    });
  });
});

// =============================================================================
// PHASE 6 — LEARNING DASHBOARD
// =============================================================================

let _learningLoaded = false;

// --- Utility helpers ---------------------------------------------------------

function provenanceBadge(prov) {
  const map = {
    REAL_TEST:   ["prov-real",  "Real Test"],
    SIMULATION:  ["prov-sim",   "Simulation"],
    HISTORICAL:  ["prov-hist",  "Historical"],
    ESTIMATE:    ["prov-est",   "Estimate"],
    FORECAST:    ["prov-est",   "Forecast"],
    real_test:   ["prov-real",  "Real Test"],
    simulation:  ["prov-sim",   "Simulation"],
    mixed:       ["prov-hist",  "Mixed"],
  };
  const [cls, label] = map[prov] || ["prov-none", prov || "Unknown"];
  return `<span class="prov-badge ${cls}">${label}</span>`;
}

function confidenceBadge(conf) {
  if (!conf) return "";
  const cls = `confidence-${conf.toLowerCase().replace(/ /g, "_")}`;
  return `<span class="confidence-badge ${cls}">${titleCase(conf)}</span>`;
}

function recStatusBadge(status) {
  return `<span class="rec-status-badge rec-status-${status}">${titleCase(status.replace(/_/g," "))}</span>`;
}

function histStatusBadge(status) {
  return `<span class="hist-version-status hist-status-${status}">${titleCase(status.replace(/_/g," "))}</span>`;
}

function rateBar(rate) {
  const pct = Math.min(100, Math.round((rate || 0) * 100));
  return `<span class="sp-rate-bar" title="${pct}%"><span class="sp-rate-fill" style="width:${pct}%"></span></span>`;
}

// --- Main loader -------------------------------------------------------------

async function loadLearningView() {
  try {
    const data = await getJSON("/api/learning/dashboard");
    renderLearningDashboard(data);
  } catch(err) {
    document.getElementById("learning-policy-body").innerHTML =
      `<p class="muted">Could not load learning dashboard: ${err.message}</p>`;
  }
}

// --- Master render -----------------------------------------------------------

function renderLearningDashboard(data) {
  // KPIs
  renderLearningKPIs(data);
  // Current policy
  renderCurrentPolicy(data.active_policy, data.performance_vs_previous);
  // Data provenance
  renderProvenance(data.attribution_summary, data.strategy_learning);
  // Strategy performance (from segment learning via full dashboard call)
  renderStrategyPerformanceSection(data);
  // Drift
  renderDrift(data.strategy_drift);
  // Experiments
  renderExperiments(data.recent_experiments || []);
  // Recommendations
  renderRecommendations(data.open_recommendations || []);
  // Policy history
  renderPolicyHistory(data.policy_history_recent || []);
}

// --- KPIs -------------------------------------------------------------------

function renderLearningKPIs(data) {
  const ap = data.active_policy || {};
  const perf = ap.measured_performance;
  const attr = data.attribution_summary || {};

  document.getElementById("lkpi-recovery-rate").textContent =
    perf ? pct(perf.recovery_rate) : "—";
  document.getElementById("lkpi-policy-version").textContent =
    ap.version_number ? "v" + ap.version_number : "Default";
  document.getElementById("lkpi-open-recs").textContent =
    (data.open_recommendations || []).length;
  document.getElementById("lkpi-real-outcomes").textContent =
    attr.real_test_outcomes != null ? attr.real_test_outcomes : "—";
}

// --- Current Policy ---------------------------------------------------------

function renderCurrentPolicy(policy, perfComparison) {
  const body = document.getElementById("learning-policy-body");
  const badge = document.getElementById("learning-policy-source-badge");
  if (!policy) { body.innerHTML = '<p class="muted">No active policy.</p>'; return; }

  if (policy.source === "rule_based_default") {
    badge.textContent = "Rule-based default";
    badge.className = "badge badge-neutral";
  } else {
    badge.textContent = "v" + policy.version_number;
    badge.className = "badge badge-ok";
  }

  const params = policy.strategy_params || {};
  const paramRows = Object.entries(params).map(([reason, strat]) =>
    `<tr><td><code>${reason}</code></td><td>${strat}</td></tr>`
  ).join("");

  body.innerHTML = `
    <table class="sp-table">
      <thead><tr><th>Failure reason</th><th>Strategy</th></tr></thead>
      <tbody>${paramRows}</tbody>
    </table>
    ${policy.reason ? `<p class="muted" style="margin-top:8px;font-size:12px;">Reason: ${escHtml(policy.reason)}</p>` : ""}
    ${policy.approved_by ? `<p class="muted" style="font-size:12px;">Approved by: ${escHtml(policy.approved_by)} · ${fmtDate(policy.activated_at)}</p>` : ""}
  `;

  const perfEl = document.getElementById("learning-policy-perf");
  if (perfComparison) {
    const delta = perfComparison.delta;
    const sign = delta >= 0 ? "+" : "";
    const cls = delta >= 0 ? "positive" : "negative";
    perfEl.innerHTML = `
      <div class="exp-diff" style="margin-top:8px;">
        <span class="exp-diff-val ${cls}">${sign}${pct(delta)}</span>
        <span>vs previous policy</span>
        <span class="muted" style="font-size:12px;">
          Before: ${pct(perfComparison.previous_recovery_rate)} ·
          After: ${pct(perfComparison.current_recovery_rate)}
          <br><em style="font-size:11px;">${escHtml(perfComparison.note || "")}</em>
        </span>
      </div>`;
  } else {
    perfEl.innerHTML = "";
  }
}

// --- Provenance -------------------------------------------------------------

function renderProvenance(attr, learning) {
  const body = document.getElementById("learning-provenance-body");
  if (!attr) { body.innerHTML = '<p class="muted">No attribution data yet. Run the agent then click Backfill.</p>'; return; }

  const prov = attr.provenance_breakdown || {};
  const realCount = attr.real_test_outcomes || 0;

  let rows = Object.entries(prov).map(([p, stats]) => `
    <tr>
      <td>${provenanceBadge(p)}</td>
      <td class="num">${(stats.attempts || 0).toLocaleString()}</td>
      <td class="num">${(stats.recoveries || 0).toLocaleString()}</td>
      <td class="num">${stats.attempts ? pct(stats.recoveries / stats.attempts) : "—"}</td>
      <td class="num">${rupees(stats.amount_recovered || 0)}</td>
    </tr>`).join("");

  if (!rows) rows = `<tr><td colspan="5" class="muted" style="text-align:center">No attribution data yet.</td></tr>`;

  body.innerHTML = `
    ${realCount === 0 ? `<div class="alert-note" style="padding:8px 12px;background:var(--status-warning-bg);border-radius:6px;font-size:12px;margin-bottom:10px;">
      <strong>No real Razorpay Test Mode observations yet.</strong> All performance data is from simulation or historical pipeline runs.
      Use real Razorpay Test Mode to collect authentic outcome data.</div>` : ""}
    <table class="provenance-table">
      <thead><tr><th>Source</th><th>Attempts</th><th>Recoveries</th><th>Rate</th><th>Amount recovered</th></tr></thead>
      <tbody>${rows}</tbody>
    </table>
    <p class="muted" style="font-size:11px;margin-top:6px;">${escHtml(attr.data_trust_note || "")}</p>
    <div style="margin-top:10px;display:flex;gap:8px;flex-wrap:wrap;">
      <button class="btn btn-ghost btn-sm" id="btn-backfill">Backfill attribution from audit log</button>
    </div>`;

  document.getElementById("btn-backfill")?.addEventListener("click", doBackfill);
}

// --- Strategy Performance ---------------------------------------------------

function renderStrategyPerformanceSection(data) {
  const body = document.getElementById("learning-strategy-body");
  // Fetch full segment learning data
  getJSON("/api/learning/segment-learning").then(d => {
    renderSegmentLearning(body, d);
    document.getElementById("learning-strategy-badge").textContent =
      d.real_test_observations > 0 ? "Actual" : "Historical/Simulation";
  }).catch(err => {
    body.innerHTML = `<p class="muted">Could not load: ${err.message}</p>`;
  });
}

function renderSegmentLearning(container, data) {
  if (!data || !data.dimensions || data.dimensions.length === 0) {
    container.innerHTML = '<p class="muted learning-empty">No strategy performance data yet. Run the agent first, then click Backfill.</p>';
    return;
  }

  // Group by dimension_key
  const byKey = {};
  for (const section of data.dimensions) {
    const k = section.dimension_key;
    if (!byKey[k]) byKey[k] = [];
    byKey[k].push(section);
  }

  let html = "";
  const keyLabels = { global: "Global", failure_reason: "By failure reason", merchant_category: "By merchant category" };
  for (const [key, sections] of Object.entries(byKey)) {
    html += `<h4 style="font-size:12px;font-weight:600;text-transform:uppercase;color:var(--text-muted);margin:14px 0 6px;">${keyLabels[key] || key}</h4>`;
    for (const section of sections) {
      html += `<div style="margin-bottom:10px;">
        <div style="font-size:12px;font-weight:500;margin-bottom:4px;">${escHtml(section.dimension_value)}</div>
        <table class="sp-table">
          <thead><tr><th>Strategy</th><th>Attempts</th><th>Rate</th><th>Recovered</th><th>Source</th><th></th></tr></thead>
          <tbody>`;
      for (const s of section.strategies) {
        const insuf = !s.sufficient ? `<span class="sp-insufficient-flag">low n</span>` : "";
        const provs = (s.provenance_mix || [section.provenance_mix || []]).flat();
        html += `<tr>
          <td>${escHtml(s.strategy || "")}</td>
          <td class="num">${s.attempts}</td>
          <td class="num">${rateBar(s.recovery_rate)} ${pct(s.recovery_rate)}${insuf}</td>
          <td class="num">${rupees(s.amount_recovered || 0)}</td>
          <td>${(section.provenance_mix || []).map(provenanceBadge).join(" ")}</td>
          <td>${s.strategy === section.best_strategy && section.has_sufficient_data ? '<span style="font-size:11px;color:var(--accent-ok);">★ best</span>' : ""}</td>
        </tr>`;
      }
      html += `</tbody></table></div>`;
    }
  }
  container.innerHTML = html;
}

// --- Strategy Drift ---------------------------------------------------------

function renderDrift(driftData) {
  const card = document.getElementById("learning-drift-card");
  const body = document.getElementById("learning-drift-body");
  if (!driftData || !driftData.alerts || driftData.alerts.length === 0) {
    card.classList.add("hidden");
    return;
  }
  card.classList.remove("hidden");
  body.innerHTML = driftData.alerts.map(a => `
    <div class="drift-alert drift-${a.severity}">
      <div class="drift-alert-title">${escHtml(a.title)}</div>
      <p style="font-size:12px;margin:4px 0;">${escHtml(a.description)}</p>
      <div class="drift-rates">
        <div class="drift-rate-item">
          <span class="drift-rate-label">Baseline (${a.baseline_n} cases)</span>
          <span class="drift-rate-value">${pct(a.baseline_rate)}</span>
        </div>
        <div class="drift-rate-item">
          <span class="drift-rate-label">Recent (${a.recent_n} cases)</span>
          <span class="drift-rate-value dropped">${pct(a.recent_rate)}</span>
        </div>
        <div class="drift-rate-item">
          <span class="drift-rate-label">Relative drop</span>
          <span class="drift-rate-value dropped">${pct(a.relative_drop)}</span>
        </div>
      </div>
      <p style="font-size:11px;color:var(--text-muted);margin-top:6px;">${escHtml(a.recommended_action)}</p>
    </div>`).join("");
}

// --- Experiments ------------------------------------------------------------

function renderExperiments(experiments) {
  const body = document.getElementById("learning-experiments-body");
  if (!experiments || experiments.length === 0) {
    body.innerHTML = '<p class="muted learning-empty">No experiments yet. Create one to start a controlled A/B comparison.</p>';
    return;
  }
  body.innerHTML = experiments.map(exp => renderExperimentCard(exp)).join("");
}

function renderExperimentCard(exp) {
  // exp may be a status dict or an evaluation dict
  const eid = exp.experiment_id;
  const name = escHtml(exp.name || eid);
  const status = exp.status || "unknown";
  const ctrl = escHtml(exp.control_strategy || "");
  const treat = escHtml(exp.treatment_strategy || "");

  let armsHtml = "";
  let diffHtml = "";
  let incrHtml = "";

  const ev = exp.evaluation || exp; // evaluation may be nested

  if (ev.sufficient_data === false) {
    const n_c = ev.control_arm ? ev.control_arm.sample_size : (exp.control_outcomes_recorded || 0);
    const n_t = ev.treatment_arm ? ev.treatment_arm.sample_size : (exp.treatment_outcomes_recorded || 0);
    armsHtml = `<div class="exp-insuf">${escHtml(ev.insufficient_data_explanation || `Collecting data… control: ${n_c} obs, treatment: ${n_t} obs, need ≥ ${ev.required_per_arm || 10} each.`)}</div>`;
  } else if (ev.control_arm && ev.treatment_arm) {
    const c = ev.control_arm;
    const t = ev.treatment_arm;
    armsHtml = `<div class="exp-arms">
      <div class="exp-arm control">
        <div class="exp-arm-label">Control · ${ctrl}</div>
        <div class="exp-arm-rate">${pct(c.recovery_rate)}</div>
        <div class="exp-arm-sub">${c.sample_size} obs · ${rupees(c.amount_recovered)} recovered</div>
        <div class="exp-arm-sub">${(c.real_test_count||0)} real / ${(c.simulation_count||0)} sim</div>
      </div>
      <div class="exp-arm treatment">
        <div class="exp-arm-label">Treatment · ${treat}</div>
        <div class="exp-arm-rate">${pct(t.recovery_rate)}</div>
        <div class="exp-arm-sub">${t.sample_size} obs · ${rupees(t.amount_recovered)} recovered</div>
        <div class="exp-arm-sub">${(t.real_test_count||0)} real / ${(t.simulation_count||0)} sim</div>
      </div>
    </div>`;

    if (ev.difference) {
      const diff = ev.difference;
      const rawDiff = diff.recovery_rate_diff || 0;
      const cls = rawDiff > 0.01 ? "positive" : rawDiff < -0.01 ? "negative" : "neutral";
      const verdict = diff.verdict === "treatment_better" ? "Treatment wins" :
                      diff.verdict === "control_better" ? "Control wins" : "No meaningful difference";
      diffHtml = `<div class="exp-diff">
        <span class="exp-diff-val ${cls}">${diff.recovery_rate_diff_pct || "—"}</span>
        <span>${verdict}</span>
        ${diff.confidence ? confidenceBadge(diff.confidence) : ""}
        ${diff.z_score != null ? `<span class="muted" style="font-size:12px;">z=${diff.z_score.toFixed(2)}</span>` : ""}
        ${provenanceBadge(ev.data_type)}
      </div>`;
    }
    if (ev.incremental_revenue) {
      const ir = ev.incremental_revenue;
      incrHtml = `<p style="font-size:12px;color:var(--text-muted);margin:6px 0 0;">${escHtml(ir.note || "")}</p>
        <p style="font-size:12px;margin:4px 0;">Estimated incremental: <strong>${rupees(ir.estimated_incremental_rs || 0)}</strong> [${escHtml(ir.data_type || "estimate")}]</p>`;
    }
  }

  const isPrelim = ev.is_preliminary ? '<span class="badge badge-neutral" style="font-size:10px;">Preliminary</span>' : "";

  return `<div class="exp-card">
    <div class="exp-card-head">
      <h3 class="exp-card-title">${name}</h3>
      <div style="display:flex;gap:6px;align-items:center;">
        ${isPrelim}
        <span class="badge badge-neutral">${escHtml(status)}</span>
        ${status === "active" ? `<button class="btn btn-ghost btn-sm" onclick="completeExperiment('${eid}')">Complete</button>` : ""}
      </div>
    </div>
    <div class="exp-card-meta">
      <span>${ctrl} vs ${treat}</span>
      ${exp.cohort ? `<span>Cohort: ${JSON.stringify(exp.cohort).replace(/[{}"]/g,"").replace(/null,?/g,"").trim() || "all"}</span>` : ""}
      ${exp.created_at ? `<span>Created: ${fmtDate(exp.created_at)}</span>` : ""}
    </div>
    ${armsHtml}${diffHtml}${incrHtml}
  </div>`;
}

// --- Recommendations --------------------------------------------------------

function renderRecommendations(recs) {
  const body = document.getElementById("learning-recs-body");
  if (!recs || recs.length === 0) {
    body.innerHTML = '<p class="muted learning-empty">No open recommendations. Backfill attribution data then click Refresh to generate.</p>';
    return;
  }
  body.innerHTML = recs.map(r => renderRecCard(r)).join("");
}

function renderRecCard(r) {
  const evidence = r.why_evidence_parsed || {};
  const rid = r.recommendation_id;
  const canApprove = r.status === "recommended" || r.status === "under_review";
  const canReject  = r.status === "recommended" || r.status === "under_review";

  const currentRate  = r.current_rate  != null ? pct(r.current_rate)  : "—";
  const recRate      = r.recommended_rate != null ? pct(r.recommended_rate) : "—";
  const improvement  = (r.current_rate != null && r.recommended_rate != null)
    ? `+${((r.recommended_rate - r.current_rate)*100).toFixed(1)}pp` : "—";

  return `<div class="rec-card" id="rec-card-${rid}">
    <div class="rec-card-head">
      <h3 class="rec-card-title">${escHtml(r.title)}</h3>
      <div style="display:flex;gap:6px;align-items:center;">
        ${recStatusBadge(r.status)}
        ${provenanceBadge(r.data_source)}
        ${confidenceBadge(r.confidence)}
      </div>
    </div>
    <div class="rec-what">${escHtml(r.what_changes)}</div>

    <div class="rec-evidence">
      <div class="rec-evidence-row"><span class="rec-evidence-label">Current strategy</span><span>${escHtml(r.current_strategy)}</span></div>
      <div class="rec-evidence-row"><span class="rec-evidence-label">Recommended</span><span>${escHtml(r.recommended_strategy)}</span></div>
      <div class="rec-evidence-row"><span class="rec-evidence-label">Current rate</span><span>${currentRate}</span></div>
      <div class="rec-evidence-row"><span class="rec-evidence-label">Expected rate</span><span>${recRate}</span></div>
      <div class="rec-evidence-row"><span class="rec-evidence-label">Improvement</span><span style="color:var(--accent-ok);font-weight:600;">${improvement}</span></div>
      <div class="rec-evidence-row"><span class="rec-evidence-label">Sample size</span><span>${r.sample_size} observations</span></div>
      ${evidence.provenance ? `<div class="rec-evidence-row"><span class="rec-evidence-label">Evidence type</span><span>${(evidence.provenance||[]).map(provenanceBadge).join(" ")}</span></div>` : ""}
    </div>

    <div class="rec-impact">
      <div class="rec-impact-item">
        <span class="rec-impact-label">Est. incremental</span>
        <span class="rec-impact-value positive">${rupees(r.estimated_incremental_rs || 0)}</span>
      </div>
      <div class="rec-impact-item">
        <span class="rec-impact-label">Confidence</span>
        <span class="rec-impact-value" style="font-size:14px;">${titleCase(r.confidence || "—")}</span>
      </div>
    </div>

    ${canApprove || canReject ? `<div class="rec-actions">
      ${canApprove ? `<button class="btn btn-primary btn-sm" onclick="approveRecommendation('${rid}')">Approve</button>` : ""}
      ${canReject  ? `<button class="btn btn-ghost btn-sm" onclick="promptRejectRecommendation('${rid}')">Reject</button>` : ""}
    </div>` : ""}

    <details style="margin-top:10px;">
      <summary style="font-size:11px;cursor:pointer;color:var(--text-muted);">Why did this recommendation appear?</summary>
      <div style="margin-top:8px;font-size:12px;">
        ${evidence.dimension ? `<p><strong>Dimension:</strong> ${escHtml(evidence.dimension)}</p>` : ""}
        ${evidence.improvement_pp != null ? `<p><strong>Observed improvement:</strong> ${evidence.improvement_pp.toFixed(2)}pp over default strategy</p>` : ""}
        ${evidence.all_strategies ? `<p><strong>All strategies observed:</strong></p><ul style="margin:4px 0 0 16px;">${
          evidence.all_strategies.map(s => `<li>${escHtml(s.strategy)}: ${pct(s.rate)} (n=${s.n})${s.sufficient?"":" — insufficient data"}</li>`).join("")
        }</ul>` : ""}
      </div>
    </details>
  </div>`;
}

// --- Policy History ----------------------------------------------------------

function renderPolicyHistory(history) {
  const body = document.getElementById("learning-history-body");
  if (!history || history.length === 0) {
    body.innerHTML = '<p class="muted learning-empty">No policy versions created yet. Approve a recommendation to create the first version.</p>';
    return;
  }
  body.innerHTML = `<div class="hist-table-wrap">${history.map(v => {
    const params = v.strategy_params || {};
    const impact = v.expected_impact || {};
    const perf = (v.performance_records || [])[0];
    return `<div class="hist-version-row">
      <span class="hist-version-num">v${v.version_number}</span>
      ${histStatusBadge(v.status)}
      <span style="font-size:12px;flex:1;">${escHtml(v.reason || "No reason recorded")}</span>
      ${perf ? `<span style="font-size:12px;">${pct(perf.recovery_rate)} recovery · ${perf.cases_observed} cases</span>` : ""}
      ${impact.recovery_rate_delta != null ? `<span style="font-size:12px;color:var(--accent-ok);">Expected +${pct(impact.recovery_rate_delta)}</span>` : ""}
      ${v.activated_at ? `<span style="font-size:11px;color:var(--text-muted);">${fmtDate(v.activated_at)}</span>` : ""}
      ${v.status === "deprecated" || v.status === "rolled_back" ? "" :
        v.status === "active" ? "" :
        `<button class="btn btn-ghost btn-sm" onclick="rollbackToVersion('${v.version_id}')">Rollback</button>`}
    </div>`;
  }).join("")}</div>`;
}

// --- Actions ----------------------------------------------------------------

async function doBackfill() {
  const btn = document.getElementById("btn-backfill");
  if (btn) { btn.textContent = "Backfilling…"; btn.disabled = true; }
  try {
    const result = await postJSON("/api/learning/backfill");
    banner(`Backfill complete: ${result.attribution_backfill?.attributed || 0} cases attributed.`);
    _learningLoaded = false;
    loadLearningView();
  } catch(err) {
    banner("Backfill failed: " + err.message, true);
  } finally {
    if (btn) { btn.textContent = "Backfill attribution from audit log"; btn.disabled = false; }
  }
}

async function generateRecommendations() {
  const btn = document.getElementById("btn-generate-recs");
  if (btn) { btn.textContent = "Generating…"; btn.disabled = true; }
  try {
    const result = await postJSON("/api/learning/recommendations/generate");
    const n = result.new_recommendations || 0;
    banner(n > 0 ? `Generated ${n} new recommendation(s).` : "No new recommendations — evidence thresholds not met yet.");
    _learningLoaded = false;
    loadLearningView();
  } catch(err) {
    banner("Could not generate recommendations: " + err.message, true);
  } finally {
    if (btn) { btn.textContent = "Refresh"; btn.disabled = false; }
  }
}

async function approveRecommendation(recId) {
  if (!confirm("Approve this recommendation and activate the new policy version?")) return;
  try {
    const r = await fetch("/api/learning/recommendations/approve", {
      method: "POST",
      headers: { "Content-Type": "application/json", ..._apiKey ? { "X-API-Key": _apiKey } : {} },
      body: JSON.stringify({ recommendation_id: recId, actor: "dashboard_user" }),
    });
    const data = await r.json();
    if (!r.ok || !data.ok) throw new Error(data.message || "Approval failed");
    banner(`Recommendation approved. Policy version ${data.version_id ? "v" + data.version_id.slice(0,8) : ""} activated.`);
    _learningLoaded = false;
    loadLearningView();
  } catch(err) {
    banner("Approval failed: " + err.message, true);
  }
}

async function promptRejectRecommendation(recId) {
  const reason = prompt("Reason for rejecting this recommendation:");
  if (!reason || !reason.trim()) return;
  try {
    const r = await fetch("/api/learning/recommendations/reject", {
      method: "POST",
      headers: { "Content-Type": "application/json", ..._apiKey ? { "X-API-Key": _apiKey } : {} },
      body: JSON.stringify({ recommendation_id: recId, actor: "dashboard_user", reason }),
    });
    const data = await r.json();
    if (!r.ok || !data.ok) throw new Error("Rejection failed");
    banner("Recommendation rejected.");
    _learningLoaded = false;
    loadLearningView();
  } catch(err) {
    banner("Rejection failed: " + err.message, true);
  }
}

async function completeExperiment(expId) {
  if (!confirm("Mark this experiment as completed?")) return;
  try {
    const r = await fetch("/api/learning/experiments/complete", {
      method: "POST",
      headers: { "Content-Type": "application/json", ..._apiKey ? { "X-API-Key": _apiKey } : {} },
      body: JSON.stringify({ experiment_id: expId }),
    });
    const data = await r.json();
    if (!r.ok || !data.ok) throw new Error(data.message || "Failed to complete experiment");
    banner(`Experiment completed. ${data.outcomes_recorded_this_sweep || 0} outcomes recorded.`);
    _learningLoaded = false;
    loadLearningView();
  } catch(err) {
    banner("Could not complete experiment: " + err.message, true);
  }
}

async function rollbackToVersion(versionId) {
  const reason = prompt("Reason for rollback:");
  if (!reason || !reason.trim()) return;
  try {
    const r = await fetch("/api/learning/policy/rollback", {
      method: "POST",
      headers: { "Content-Type": "application/json", ..._apiKey ? { "X-API-Key": _apiKey } : {} },
      body: JSON.stringify({ target_version_id: versionId, actor: "dashboard_user", reason }),
    });
    const data = await r.json();
    if (!r.ok || !data.ok) throw new Error(data.message || "Rollback failed");
    banner(`Rolled back to version ${versionId.slice(0,8)}. Current version deprecated.`);
    _learningLoaded = false;
    loadLearningView();
  } catch(err) {
    banner("Rollback failed: " + err.message, true);
  }
}

// --- New Experiment Form ----------------------------------------------------

function initNewExperimentForm() {
  const btnNew    = document.getElementById("btn-new-experiment");
  const btnCancel = document.getElementById("btn-cancel-experiment");
  const formCard  = document.getElementById("new-experiment-form-card");
  const form      = document.getElementById("new-experiment-form");

  if (btnNew) btnNew.addEventListener("click", () => formCard?.classList.remove("hidden"));
  if (btnCancel) btnCancel.addEventListener("click", () => formCard?.classList.add("hidden"));

  if (form) {
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const name    = document.getElementById("exp-name")?.value.trim();
      const ctrl    = document.getElementById("exp-control")?.value;
      const treat   = document.getElementById("exp-treatment")?.value;
      const merchant = document.getElementById("exp-merchant")?.value || null;
      const reason  = document.getElementById("exp-reason")?.value || null;
      const minN    = parseInt(document.getElementById("exp-min-sample")?.value || "10", 10);

      if (!name) { banner("Experiment name is required.", true); return; }
      if (ctrl === treat) { banner("Control and treatment must be different strategies.", true); return; }

      try {
        const r = await fetch("/api/learning/experiments/create", {
          method: "POST",
          headers: { "Content-Type": "application/json", ..._apiKey ? { "X-API-Key": _apiKey } : {} },
          body: JSON.stringify({
            name, control_strategy: ctrl, treatment_strategy: treat,
            merchant_category: merchant || undefined,
            failure_reason: reason || undefined,
            min_sample_size: minN,
          }),
        });
        const data = await r.json();
        if (!r.ok || !data.ok) throw new Error(data.message || "Create failed");
        banner(`Experiment created. ${data.assignment?.assigned || 0} cases assigned.`);
        form.reset();
        formCard?.classList.add("hidden");
        _learningLoaded = false;
        loadLearningView();
      } catch(err) {
        banner("Could not create experiment: " + err.message, true);
      }
    });
  }
}

// --- Utility: escape HTML ---------------------------------------------------

function escHtml(s) {
  return String(s == null ? "" : s)
    .replace(/&/g,"&amp;").replace(/</g,"&lt;")
    .replace(/>/g,"&gt;").replace(/"/g,"&quot;");
}

function fmtDate(iso) {
  if (!iso) return "—";
  try { return new Date(iso).toLocaleDateString("en-IN", { day:"2-digit", month:"short", year:"numeric" }); }
  catch { return iso; }
}

// --- Wire up Learning nav click ---------------------------------------------

document.addEventListener("DOMContentLoaded", () => {
  // Learning nav click → lazy load
  document.querySelectorAll(".nav-item[data-view='learning']").forEach(item => {
    item.addEventListener("click", () => {
      if (!_learningLoaded) {
        _learningLoaded = true;
        loadLearningView();
        initNewExperimentForm();
      }
    });
  });
  // Recommendations refresh button
  document.getElementById("btn-generate-recs")?.addEventListener("click", generateRecommendations);
});
