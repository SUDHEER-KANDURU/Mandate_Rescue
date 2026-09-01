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
  const r = await fetch(url);
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

    // Why the model predicts this (SHAP) — ML validation layer, additive/non-decision.
    // Loaded separately so a slow/unavailable SHAP call never blocks the drawer.
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
  // Refresh per-view empty hints now that panels have (un)hidden themselves.
  if (typeof syncViewEmptyStates === "function") syncViewEmptyStates();
  // Keep the command palette's case list in sync with the freshly loaded data.
  cmdkCases = Array.isArray(cases) ? cases : [];
  cmdkCasesLoaded = true;
  // Refresh the Activity feed with the latest audit events.
  if (typeof loadActivity === "function") loadActivity();
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
  let streamError = null;

  let resp;
  try {
    resp = await fetch("/api/run-agent-stream", {
      headers: _apiKey ? { "X-API-Key": _apiKey } : {},
    });
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
const VIEW_IDS = ["overview", "cases", "compliance", "ml", "sandbox", "chaos", "reports"];

function showView(view) {
  if (!VIEW_IDS.includes(view)) view = "overview";
  document.querySelectorAll(".view").forEach((sec) => {
    sec.classList.toggle("active", sec.dataset.view === view);
  });
  document.querySelectorAll(".nav-item").forEach((item) => {
    item.classList.toggle("active", item.dataset.view === view);
  });
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
  document.querySelectorAll(".nav-item").forEach((item) => {
    item.addEventListener("click", () => showView(item.dataset.view));
  });
  // Restore from hash on load (default: overview).
  const initial = (window.location.hash || "").replace("#", "");
  showView(VIEW_IDS.includes(initial) ? initial : "overview");
}


// --- Chaos Suite view (Phase 1) ---------------------------------------------
// Triggers GET /api/chaos-test and renders the seven-scenario PASS/FAIL report.
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
  btn.textContent = "Running seven attacks\u2026";

  // Visible loading state: a spinner + a clear description of what is actually
  // happening. The request is a real HTTP round-trip that seeds 2000+ cases for
  // scenario 7 and runs seven real scenarios against isolated in-memory databases,
  // so it is NOT instant — the spinner makes that latency legible rather than making
  // a genuine run look fake/pre-baked.
  const placeholder = document.getElementById("chaos-placeholder");
  placeholder.classList.remove("hidden");
  placeholder.innerHTML = "";
  const loading = el("div", "chaos-loading");
  loading.appendChild(el("span", "spinner"));
  loading.appendChild(el("span", null,
    "Running 7 adversarial scenarios against an isolated test database\u2026"));
  placeholder.appendChild(loading);
  document.getElementById("chaos-results").classList.add("hidden");

  try {
    const report = await getJSON("/api/chaos-test");
    renderChaosResults(report);
  } catch (err) {
    errBox.textContent = "Chaos suite failed to run: " + err.message;
    errBox.classList.remove("hidden");
    placeholder.textContent =
      "Run the suite to attack the system with seven adversarial scenarios and see a PASS/FAIL report for each.";
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
  const icon = document.getElementById("theme-icon");
  if (icon) icon.textContent = currentTheme === "dark" ? "\u2600" : "\u263E"; // sun in dark, moon in light
  const btn = document.getElementById("btn-theme");
  if (btn) btn.title = currentTheme === "dark" ? "Switch to light theme" : "Switch to dark theme";
}

function toggleTheme() {
  applyTheme(currentTheme === "dark" ? "light" : "dark");
}

function initTheme() {
  const btn = document.getElementById("btn-theme");
  if (btn) btn.addEventListener("click", toggleTheme);
  applyTheme("light");
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
