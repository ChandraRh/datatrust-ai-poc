// DataTrust AI web page. All data comes from /api/* (PostgreSQL gold + audit), nothing is hard-coded.
const state = { page: "observe", version: null, overview: null, incidents: null, charts: {}, history: [], incidentFilters: { query: "", severity: "" },
  scope: null, scopes: [] };
try { state.scope = localStorage.getItem("dt.scope") || null; } catch (e) {}
const $ = (id) => document.getElementById(id);
let versionCheck = null;
// every Observe → Verify number belongs to ONE source (picked at the top); the server resolves an unknown scope to the first source
const sq = () => (state.scope ? "scope=" + encodeURIComponent(state.scope) : "");
const isPoc = () => state.overview && state.overview.kind === "poc";
function scopeOfIncident(id) {
  const m = /^db(\d+):/.exec(id) || null, a = /^adf:(\d+):/.exec(id) || null;
  if (m) return `db:${m[1]}`;
  if (a) return `adf:${a[1]}`;
  const poc = state.scopes.find((s) => s.poc);
  return poc ? poc.key : state.scope;
}
async function initScopes() {
  try { state.scopes = (await api("/api/scopes")).scopes; } catch (e) { state.scopes = []; }
  let pick = $("scopePick");
  if (!pick) {
    const wrap = document.createElement("label");
    wrap.className = "scope-pick";
    wrap.innerHTML = `<span>Source</span><select id="scopePick" aria-label="Source shown on Observe to Verify"></select>`;
    document.querySelector(".topright").prepend(wrap);
    pick = $("scopePick");
    pick.addEventListener("change", () => setScope(pick.value));
  }
  const group = (kind, label) => {
    const items = state.scopes.filter((s) => s.kind === kind);
    return items.length ? `<optgroup label="${label}">${items.map((s) => `<option value="${esc(s.key)}">${esc(s.name)}${s.poc ? " (POC pipeline)" : ""} · ${esc(s.type)}</option>`).join("")}</optgroup>` : "";
  };
  pick.innerHTML = group("db", "Databases") + group("adf", "Data factories");
  if (state.scope) pick.value = state.scope;
}
async function setScope(key) {
  if (!key || key === state.scope) return;
  state.scope = key;
  try { localStorage.setItem("dt.scope", key); } catch (e) {}
  state.selected = null;
  state.version = null;
  if ($("scopePick")) $("scopePick").value = key;
  await checkVersion(true);
  if (state.page.startsWith("estate-dq-") && window.renderEstate) return renderEstate(state.page);   // these pages follow the source too
  if (state.page.startsWith("estate-")) toast(`Observe to Verify now show ${($("scopePick").selectedOptions[0] || {}).textContent || key}`);
}

// ------------------------------------------------------------------ helpers
async function api(path, options) {
  const r = await fetch(path, options);
  const body = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(body.error || body.detail || r.statusText);
  return body;
}
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const num = (v) => (v == null ? "–" : Number(v).toLocaleString("en-IN"));
const inr = (v) => (v == null ? "–" : "₹" + Number(v).toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 }));
const inrShort = (v) => {
  const n = Number(v || 0);
  if (Math.abs(n) >= 1e7) return "₹" + (n / 1e7).toFixed(2) + " Cr";
  if (Math.abs(n) >= 1e5) return "₹" + (n / 1e5).toFixed(2) + " L";
  return inr(n);
};
const tone = (score) => (score >= 90 ? "ok" : score >= 75 ? "warn" : "bad");
const color = { ok: "#11805A", warn: "#EF802C", bad: "#C42B2B" };

function showError(msg) {
  const box = $("errorBox");
  if (!msg) return box.classList.add("hidden");
  box.textContent = "Dashboard refresh failed: " + msg + ". Existing figures may be out of date; use Refresh to try again.";
  box.classList.remove("hidden");
}
function toast(msg) {
  const t = $("toast");
  t.textContent = msg;
  t.classList.remove("hidden");
  clearTimeout(t._h);
  t._h = setTimeout(() => t.classList.add("hidden"), 4000);
}

function csvCell(value) {
  const text = String(value ?? "");
  const safe = /^[\t\r\n ]*[=+\-@]/.test(text) ? "'" + text : text;
  return `"${safe.replace(/"/g, '""')}"`;
}

function exportIncidents(incidents) {
  if (!incidents.length) return;
  const columns = ["incident_id", "severity", "type", "title", "summary", "business_date",
    "approval_status", "approved_by", "metrics", "exported_at_utc"];
  const exportedAt = new Date().toISOString();
  const rows = incidents.map((incident) => [
    incident.id, incident.severity, incident.type, incident.title, incident.summary, incident.business_date,
    incident.approval?.status, incident.approval?.approved_by,
    incident.metrics ? JSON.stringify(incident.metrics) : "", exportedAt,
  ]);
  const csv = [columns, ...rows].map((row) => row.map(csvCell).join(",")).join("\r\n");
  const url = URL.createObjectURL(new Blob(["\uFEFF", csv], { type: "text/csv;charset=utf-8" }));
  const link = document.createElement("a");
  link.href = url;
  link.download = `datatrust-open-incidents-${new Date().toISOString().slice(0, 10)}.csv`;
  document.body.appendChild(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
  toast(`Exported ${incidents.length} matching incident${incidents.length === 1 ? "" : "s"} to CSV`);
}

// simple markdown: **bold**, _italic_, *italic*, "- " bullets, blank line = paragraph
function md(text) {
  const lines = esc(text).split("\n");
  let html = "", inList = false;
  for (const raw of lines) {
    let l = raw.replace(/\*\*(.+?)\*\*/g, "<b>$1</b>").replace(/(^|\s)_(.+?)_(?=\s|$|[.,)])/g, "$1<i>$2</i>")
      .replace(/(^|\s)\*(\S.+?)\*(?=\s|$|[.,)])/g, "$1<i>$2</i>");
    if (/^\s*- /.test(l)) {
      if (!inList) { html += "<ul>"; inList = true; }
      html += "<li>" + l.replace(/^\s*- /, "") + "</li>";
      continue;
    }
    if (inList) { html += "</ul>"; inList = false; }
    html += l.trim() ? "<p>" + l + "</p>" : "";
  }
  return html + (inList ? "</ul>" : "");
}

const MONEY = /amount|revenue|gross|net|refund|price|difference|value/i;
function tableHtml(t, maxRows = 500) {
  if (!t || !t.rows || !t.rows.length) return "";
  const money = t.columns.map((c) => MONEY.test(c) && !/payments|count|rows/i.test(c));
  const head = t.columns.map((c) => `<th>${esc(c)}</th>`).join("");
  const body = t.rows.slice(0, maxRows).map((r) => "<tr>" + r.map((v, i) => {
    if (money[i] && v !== null && v !== "" && !isNaN(Number(v))) v = Number(v);
    const isNum = typeof v === "number";
    const shown = v === null ? "" : typeof v === "boolean" ? (v ? "yes" : "no") : isNum ? (money[i] ? inr(v) : num(v)) : esc(v);
    return `<td class="${isNum ? "num" : ""}">${shown}</td>`;
  }).join("") + "</tr>").join("");
  return `<div class="tablewrap"><table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table></div>`;
}
const objTable = (rows, cols) => tableHtml(rows && rows.length ? { columns: cols || Object.keys(rows[0]), rows: rows.map((r) => (cols || Object.keys(rows[0])).map((c) => r[c])) } : null);

function gauge(el, score, label) {
  if (score == null) {                                           // a source with nothing measured yet
    $(el).innerHTML = `<svg width="190" height="190" viewBox="0 0 190 190" role="img" aria-label="No trust score yet">
      <circle cx="95" cy="95" r="70" fill="none" stroke="#EAEFF4" stroke-width="16"/>
      <text x="95" y="104" text-anchor="middle" font-size="36" font-weight="700" fill="#94A3B8">–</text></svg>`;
    return;
  }
  const t = tone(score), r = 70, c = 2 * Math.PI * r, filled = (c * score) / 100;
  $(el).innerHTML = `<svg width="190" height="190" viewBox="0 0 190 190" role="img" aria-label="Trust score ${score}">
    <circle cx="95" cy="95" r="${r}" fill="none" stroke="#EAEFF4" stroke-width="16"/>
    <circle cx="95" cy="95" r="${r}" fill="none" stroke="${color[t]}" stroke-width="16" stroke-linecap="round"
      stroke-dasharray="${filled} ${c}" transform="rotate(-90 95 95)"/>
    <text x="95" y="102" text-anchor="middle" font-size="44" font-weight="700" fill="#0F1D2E" letter-spacing="-1">${score}</text>
    <text x="95" y="128" text-anchor="middle" font-size="12.5" fill="#64748B">/ 100</text></svg>`;
}

// ---- charts: thin marks (2px lines, <=24px bars with a 4px rounded data end), hairline grid, labelled reference lines
const CH = { brand: "#0672A1", ok: "#11805A", bad: "#C42B2B", grey: "#94A3B8", ink2: "#3A4A5E", line: "#EDF1F5" };
if (window.Chart && !Chart.registry.plugins.get("dtThresholds")) {
  Chart.register({                                              // horizontal reference lines, e.g. Healthy 90 / At risk below 75
    id: "dtThresholds",
    afterDatasetsDraw(chart, _args, opts) {
      const lines = (opts && opts.lines) || [];
      const y = chart.scales.y, area = chart.chartArea, c = chart.ctx;
      if (!y || !lines.length) return;
      c.save();
      lines.forEach((l) => {
        const py = y.getPixelForValue(l.y);
        if (py < area.top - 1 || py > area.bottom + 1) return;
        c.strokeStyle = l.color || "#94A3B8"; c.lineWidth = 1;
        c.beginPath(); c.moveTo(area.left, py); c.lineTo(area.right, py); c.stroke();
        c.font = "600 11px Inter, 'Segoe UI', system-ui, sans-serif"; c.fillStyle = CH.ink2; c.textAlign = "right"; c.textBaseline = "bottom";
        const w = c.measureText(l.label).width + 8;
        c.fillStyle = "rgba(255,255,255,.85)"; c.fillRect(area.right - w, py - 15, w, 14);
        c.fillStyle = CH.ink2; c.fillText(l.label, area.right - 4, py - 2);
      });
      c.restore();
    },
  });
}
const TRUST_LINES = [{ y: 90, label: "Healthy from 90" }, { y: 75, label: "At risk below 75" }];
function chartBase(opts = {}) {
  return { responsive: true, maintainAspectRatio: false, interaction: { mode: "index", intersect: false },
    plugins: { legend: { display: !!opts.legend, position: "bottom", labels: { boxWidth: 10, boxHeight: 10, usePointStyle: true, pointStyle: "rectRounded" } },
      dtThresholds: { lines: opts.thresholds || [] },
      tooltip: { callbacks: { label: opts.tipLabel || ((c) => `${c.dataset.label}: ${opts.money ? inr(c.parsed.y) : num(c.parsed.y)}`),
        afterBody: opts.tipAfter ? (items) => opts.tipAfter(items[0].dataIndex) : undefined } } },
    scales: { x: { stacked: !!opts.stacked, grid: { display: false }, ticks: { maxRotation: 0, autoSkipPadding: 12 } },
      y: { stacked: !!opts.stacked, min: opts.min, max: opts.max, beginAtZero: opts.min == null, grid: { color: CH.line },
        ticks: { precision: opts.money ? undefined : 0, callback: (v) => (opts.money ? inrShort(v) : num(v)) } } } };
}
function shortDay(d) {                                          // 2026-10-27 -> 27 Oct (axis); the tooltip shows the full date
  const x = new Date(String(d).slice(0, 10) + "T00:00:00");
  return isNaN(x) ? d : x.toLocaleDateString(undefined, { day: "numeric", month: "short" });
}
function drawChart(id, type, labels, values, label, opts = {}) {
  if (!window.Chart) return;
  if (state.charts[id]) state.charts[id].destroy();
  const ctx = typeof id === "string" ? $(id) : id;
  if (!ctx) return;
  const colors = opts.colors || opts.pointColors || CH.brand;
  const ds = type === "bar"
    ? { label, data: values, backgroundColor: colors, maxBarThickness: 24, borderRadius: { topLeft: 4, topRight: 4 }, borderSkipped: "bottom" }
    : { label, data: values, borderColor: CH.brand, backgroundColor: "rgba(6,114,161,.10)", borderWidth: 2, tension: 0.15, fill: true,
        pointRadius: 4, pointHoverRadius: 6, pointBackgroundColor: colors, pointBorderColor: "#fff", pointBorderWidth: 2 };
  const shown = opts.dates ? labels.map(shortDay) : labels;
  const base = chartBase(opts);
  if (opts.dates) base.plugins.tooltip.callbacks.title = (items) => labels[items[0].dataIndex];
  state.charts[id] = new Chart(ctx, { type, data: { labels: shown, datasets: [ds] }, options: base });
}
function drawStacked(id, labels, series, opts = {}) {
  // series: [{label, color, data}] bottom to top; a 2px white top edge separates the blocks (no outline around marks)
  if (!window.Chart) return;
  if (state.charts[id]) state.charts[id].destroy();
  const ctx = $(id);
  if (!ctx) return;
  const base = chartBase({ ...opts, stacked: true, legend: true });
  base.plugins.tooltip.callbacks.title = (items) => labels[items[0].dataIndex];
  state.charts[id] = new Chart(ctx, { type: "bar", options: base, data: { labels: labels.map(shortDay), datasets: series.map((s) => ({
    label: s.label, data: s.data, backgroundColor: s.color, maxBarThickness: 24, borderColor: "#fff", borderWidth: { top: 2 }, borderSkipped: "bottom" })) } });
}

// ------------------------------------------------------------------ navigation
document.querySelectorAll("#tabs button").forEach((b) => b.addEventListener("click", () => go(b.dataset.page)));
function go(page, arg) {
  state.page = page;
  const navPage = page.startsWith("estate-checks") ? "estate-dq-rules" : page;   // a rule page belongs to DQ Rule Studio in the menu
  document.querySelectorAll("#tabs button").forEach((b) => b.classList.toggle("active", b.dataset.page === navPage));
  document.querySelectorAll(".page").forEach((p) => p.classList.add("hidden"));
  $("page-" + page).classList.remove("hidden");
  render(page, arg).catch((e) => showError(e.message));
  if (page === "ask") setTimeout(() => $("askInput").focus(), 50);
}

// pages that read the shared overview / incidents wait for the first load instead of failing when clicked early
let corePromise = null;
async function ensureCore() {
  if (state.overview && state.incidents) return;
  corePromise = corePromise || loadCore().finally(() => (corePromise = null));
  await corePromise;
}
async function render(page, arg) {
  if (!["ask", "lineage"].includes(page) && !page.startsWith("estate-")) await ensureCore();
  if (page === "observe") return renderObserve();
  if (page === "detect") return renderDetect();
  if (page === "investigate") return renderInvestigate(arg);
  if (page === "lineage") return renderLineage();
  if (page === "impact") return renderImpact(arg);
  if (page === "verify") return renderVerify();
  if (page === "pipeline") return renderPipeline();
  if (page === "ask") return renderAsk();
}

// ------------------------------------------------------------------ data loading + auto refresh
async function loadCore() {
  if (!state.scopes.length) await initScopes();
  const [ov, inc] = await Promise.all([api("/api/overview?" + sq()), api("/api/incidents?" + sq())]);
  state.overview = ov;
  state.incidents = inc;
  if (ov.scope && ov.scope.key !== state.scope) {               // unknown / removed source: the server picked the first one
    state.scope = ov.scope.key;
    try { localStorage.setItem("dt.scope", state.scope); } catch (e) {}
  }
  if ($("scopePick") && state.scope) $("scopePick").value = state.scope;
  const openIncidents = inc.open || [];
  const urgentIncidents = openIncidents.filter((incident) => ["CRITICAL", "HIGH"].includes(incident.severity)).length;
  const incidentBadge = $("detectNavCount");
  incidentBadge.textContent = openIncidents.length > 99 ? "99+" : String(openIncidents.length);
  incidentBadge.hidden = openIncidents.length === 0;
  incidentBadge.classList.toggle("has-urgent", urgentIncidents > 0);
  incidentBadge.classList.toggle("has-open", openIncidents.length > 0 && urgentIncidents === 0);
  $("tabs").querySelector('[data-page="detect"]').setAttribute(
    "aria-label",
    `2. Detect, ${openIncidents.length} open incident${openIncidents.length === 1 ? "" : "s"}${urgentIncidents ? `, ${urgentIncidents} high or critical` : ""}`
  );
  const t = ov.trust;
  $("topScore").textContent = t.score == null ? `Trust score – · ${t.label}` : `Trust score ${t.score} · ${t.label}`;
  $("topScore").title = `${(ov.scope || {}).name || ""}: ${t.basis ? "from its " + t.basis : "from its pipeline rules and data checks"}`;
  showError(null);
}

async function checkVersion(force = false) {
  if (versionCheck) return versionCheck;
  const check = async () => {
    const refresh = $("refreshBtn");
    refresh.disabled = true;
    $("refreshInfo").dataset.state = "checking";
    $("refreshInfo").textContent = "Checking data…";
    try {
      const v = await api("/api/version");
      const first = state.version === null;
      const changed = v.signature !== state.version;
      if (force || changed) {
        await loadCore();
        state.version = v.signature;
        if (changed && !first && !force) toast("New data detected: page refreshed");
        if (!state.page.startsWith("estate-") && (state.page !== "ask" || force)) await render(state.page);
      } else if (state.page === "pipeline") {
        await renderPipeline();
      }
      $("refreshInfo").dataset.state = "online";
      $("refreshInfo").textContent = isPoc()
        ? `Connected · last run ${(v.last_run || "–").slice(0, 16)} · checked ${new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}`
        : `Connected · checked ${new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}`;
    } catch (e) {
      $("refreshInfo").dataset.state = "error";
      $("refreshInfo").textContent = "Refresh failed · retrying automatically";
      showError(e.message);
    } finally {
      refresh.disabled = false;
    }
  };
  versionCheck = check();
  try { await versionCheck; } finally { versionCheck = null; }
}

// ------------------------------------------------------------------ 1. observe
const OBS_TITLE = { Healthy: "is healthy", "Needs attention": "needs attention", "At risk": "is at risk", "No data yet": "has nothing measured yet" };
function showPocCards(poc) {
  $("revCard").classList.toggle("hidden", !poc);
  $("estateCard").classList.toggle("hidden", !poc);
  $("itemsCard").classList.toggle("hidden", poc);
  $("trendEmpty").classList.add("hidden");
  $("trendChart").parentElement.classList.remove("hidden");
}
// ---- the source's briefing: what broke, what got fixed, what to watch (saved answer shown; a new one only on request)
async function aiBriefing(generate = false) {
  const box = $("briefCard");
  if (!box || !state.overview || state.overview.llm === "none") { if (box) box.innerHTML = ""; return; }
  const scopeAt = state.scope;
  if (generate) box.innerHTML = `<div class="card ai-card brief"><div class="ai-head"><span class="ai-badge">AI</span><b>Writing today's briefing…</b></div></div>`;
  let b;
  try { b = await api(`/api/ai/briefing?${sq()}${generate ? "&refresh=true" : "&cached_only=true"}`); }
  catch (e) { box.innerHTML = `<div class="card ai-card brief"><div class="error-box small">${esc(e.message)}</div></div>`; return; }
  if (scopeAt !== state.scope || state.page !== "observe") return;   // the user moved on meanwhile
  const admin = typeof est !== "undefined" && est.user && est.user.role === "admin";     // est: estate.js (signed-in user)
  const list = (xs) => (xs || []).length ? `<ul>${xs.map((x) => `<li>${esc(x)}</li>`).join("")}</ul>` : "";
  box.innerHTML = `<div class="card ai-card brief"><div class="ai-head"><span class="ai-badge">AI</span><b>Today's briefing</b>
      <span class="small muted">${b.available ? esc((b.model || "") + (b.generated_at ? " · " + String(b.generated_at).slice(0, 16) : "")) : b.not_generated ? "plain summary · not written by AI yet" : esc(b.reason || "")}</span>
      <span class="grow"></span><button class="btn light btn-sm" id="briefGo">${b.available ? "Rewrite" : "Write with AI"}</button>
      ${admin ? `<button class="btn light btn-sm" id="briefSend" title="Post to the Google Chat space of the alerts">Post to Google Chat</button>` : ""}</div>
    <p><b>${esc(b.headline || "")}</b> ${esc(b.summary || "")}</p>
    ${b.watch && b.watch.length ? `<div class="brief-cols"><div><h4>Watch today</h4>${list(b.watch)}</div>${b.good_news && b.good_news.length ? `<div><h4>Good news</h4>${list(b.good_news)}</div>` : ""}</div>` : ""}
    ${b.unverified && b.unverified.length ? `<div class="banner warn small">Check these numbers yourself: ${b.unverified.map(esc).join(", ")}</div>` : ""}</div>`;
  $("briefGo").onclick = () => aiBriefing(true);
  if ($("briefSend")) $("briefSend").onclick = async () => {
    $("briefSend").disabled = true;
    try {
      const r = await fetch(`/api/ai/briefing/send?${sq()}`, { method: "POST", credentials: "same-origin" });
      const body = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(body.detail || body.error || r.statusText);
      toast("Briefing posted to Google Chat");
    } catch (x) { toast(x.message); $("briefSend").disabled = false; }
  };
}

function renderObserve() {
  aiBriefing();
  if (!isPoc()) return renderObserveGeneric();
  showPocCards(true);
  $("obsSub").textContent = `${state.overview.scope ? state.overview.scope.name + ": " : ""}source files → Bronze → Silver → Gold, monitored after every pipeline run.`;
  $("trendTitle").textContent = "Trust score by business date";
  const { overview: o, trust: t, pipeline: p } = state.overview;
  const cov = o.coverage || {};
  $("obsTitle").textContent = "1. Observe: " + ({ Healthy: "the estate is healthy", "Needs attention": "the estate needs attention", "At risk": "the estate is at risk" }[t.label] || t.label);
  $("obsCoverage").textContent = cov.first_date ? `Data from ${cov.first_date} to ${cov.last_date} (${cov.days} business dates).` : "";
  gauge("gauge", t.score);
  $("gaugeLabel").innerHTML = `<span class="${tone(t.score)}">${t.label}</span>`;
  drawChart("trendChart", "line", t.trend.map((x) => x.business_date), t.trend.map((x) => x.score), "Trust score",
    { min: 50, max: 100, dates: true, thresholds: TRUST_LINES, pointColors: t.trend.map((x) => color[tone(x.score)]),
      tipLabel: (c) => `Trust score ${num(c.parsed.y)}`,
      tipAfter: (i) => {                                       // why that day lost points
        const d = t.trend[i], why = [];
        if (d.recon_status === "MISMATCH") why.push("settlement does not match (−15)");
        if (d.recon_status === "NO SETTLEMENT") why.push("no settlement file for this date");
        if (d.rejected) why.push(`${num(d.rejected)} row(s) rejected`);
        return why.length ? why : ["nothing lowered it"];
      } });
  const assets = o.assets.reduce((s, a) => s + a.n, 0);
  const kpis = [
    ["Data assets", assets, ""], ["Gross revenue", inrShort(o.totals.gross), ""], ["Open incidents", o.incidents.open, o.incidents.high ? "bad" : o.incidents.open ? "warn" : ""],
    ["Revenue gaps", o.incidents.money, o.incidents.money ? "bad" : ""], ["Quality issues", o.incidents.quality, o.incidents.quality ? "warn" : ""],
    ["Last run", (p.layers.every((l) => l.status === "SUCCESS") && p.layers.length) ? "SUCCESS" : "CHECK", p.layers.every((l) => l.status === "SUCCESS") ? "" : "bad"],
  ];
  $("obsKpis").innerHTML = kpis.map(([l, v, c]) => `<div class="kpi ${c}"><div class="l">${l}</div><div class="v">${typeof v === "number" ? num(v) : esc(v)}</div></div>`).join("");
  const d = t.dimensions;
  const freshnessLimit = t.rules.freshness_hours;
  const freshnessNotice = $("freshnessNotice");
  if (d.freshness_hours == null || d.freshness_hours > freshnessLimit) {
    const lastSuccess = d.freshness_hours == null
      ? "No successful pipeline run is recorded."
      : `The last successful pipeline run was ${num(d.freshness_hours)} hours ago.`;
    freshnessNotice.innerHTML = `<h2>Data may be stale</h2><p>${lastSuccess} The dashboard metrics may not reflect current source data; the freshness target is ${freshnessLimit} hours.</p>
      <button class="btn light btn-sm" type="button" onclick="go('pipeline')">View pipeline log</button>`;
    freshnessNotice.classList.remove("hidden");
  } else {
    freshnessNotice.classList.add("hidden");
    freshnessNotice.textContent = "";
  }
  const bars = [["Completeness (source → silver)", d.completeness], ["Validity (passed rules)", d.validity],
    ["Uniqueness (no duplicates)", d.uniqueness], ["Reconciliation (settlement match)", d.reconciliation]];
  $("dims").innerHTML = bars.map(([l, v]) => `<div class="bar"><div class="t"><span>${l}</span><b>${v ?? "–"}%</b></div>
    <div class="track"><div class="fill" style="width:${v || 0}%;background:${color[tone(v)]}"></div></div></div>`).join("") +
    `<div class="small muted">Last successful run: ${d.freshness_hours ?? "–"} h ago</div>`;
  $("reasons").innerHTML = t.reasons.length ? "<p><b>Why not 100:</b> " + t.reasons.map((r) => `−${r.points} ${esc(r.reason)}`).join(" · ") + "</p>" : "";
  // captured revenue per date, coloured by whether the payment gateway's settlement agrees (the trust question behind the money)
  const recon = Object.fromEntries((state.overview.settlement || []).map((r) => [r.business_date, r]));
  const cls = (d) => (!recon[d] ? "none" : recon[d].recon_status === "MISMATCH" ? "bad" : "ok");
  const REV = { ok: [CH.brand, "Settlement matches"], bad: [CH.bad, "Settlement mismatch"], none: [CH.grey, "No settlement file"] };
  const days = o.daily.map((x) => x.business_date);
  drawChart("revChart", "bar", days, o.daily.map((x) => x.gross_revenue), "Captured revenue",
    { money: true, dates: true, colors: days.map((d) => REV[cls(d)][0]),
      tipAfter: (i) => {
        const r = recon[days[i]];
        if (!r) return ["No settlement file for this date: the gateway total cannot be compared"];
        return r.recon_status === "MISMATCH"
          ? [`Gateway settled ${inr(r.settled_amount)} (${num(r.settled_payments)} payments)`, `Short by ${inr(r.amount_difference)} (${num(r.payment_difference)} payments)`]
          : ["Matches the gateway settlement"];
      } });
  const bad = days.filter((d) => cls(d) === "bad");
  const gap = bad.reduce((s, d) => s + Number(recon[d].amount_difference || 0), 0);
  $("revSub").textContent = bad.length
    ? `${bad.length} date(s) where the gateway settled more than reached silver: ${inr(gap)} short in total.`
    : "Every date with a settlement file matches the gateway.";
  $("revLegend").innerHTML = Object.entries(REV).filter(([k]) => days.some((d) => cls(d) === k))
    .map(([, [c, l]]) => `<span><i style="background:${c}"></i>${l}</span>`).join("");
  const sch = Object.fromEntries(o.assets.map((a) => [a.schema, a.n]));
  const st = (s) => (s === "SUCCESS" ? "ok" : s ? "bad" : "grey");
  const lay = Object.fromEntries(p.layers.map((l) => [l.layer, l.status]));
  $("estate").innerHTML = [
    ["Source files", "local / ADLS", "ok", "Connected"],
    ["Bronze", `${sch.bronze || 0} tables`, st(lay.BRONZE), lay.BRONZE || "no run"],
    ["Silver", `${sch.silver || 0} tables`, st(lay.SILVER), lay.SILVER || "no run"],
    ["Gold", `${sch.gold || 0} views`, st(lay.GOLD), lay.GOLD || "no run"],
    ["AI agent", state.overview.llm === "none" ? "built-in (offline)" : state.overview.llm, "ok", "Ready"],
  ].map(([n, d2, c, s]) => `<div class="item"><div>${n} <span class="small muted">${d2}</span></div><span class="chip ${c}">${esc(s)}</span></div>`).join("");
}

// any source other than the POC pipeline: a database scored by its data checks, or a factory scored by its pipelines
const STATUS_CHIP = { PASS: ["ok", "Pass"], WARN: ["warn", "Warning"], FAIL: ["bad", "Fail"], ERROR: ["bad", "Error"] };
function renderObserveGeneric() {
  const o = state.overview, t = o.trust, s = o.scope || {};
  showPocCards(false);
  $("obsTitle").textContent = `1. Observe: ${s.name || "this source"} ${OBS_TITLE[t.label] || t.label}`;
  $("obsSub").textContent = s.kind === "adf"
    ? `Azure Data Factory ${s.name}: scored from its pipeline runs, failures, freshness and source / target checks.`
    : `${s.type} connection ${s.name}: scored from its data checks.`;
  $("obsCoverage").textContent = o.last_activity ? `Last ${s.kind === "adf" ? "sync" : "check run"} ${String(o.last_activity).slice(0, 16)}.` : "";
  $("freshnessNotice").classList.add("hidden");
  gauge("gauge", t.score);
  $("gaugeLabel").innerHTML = `<span class="${t.score == null ? "muted" : tone(t.score)}">${esc(t.label)}</span>`;
  $("trendTitle").textContent = o.runs_by_day ? "Pipeline runs per day, last 14 days" : o.trend_label || "Trust score by day";
  if (o.runs_by_day && o.runs_by_day.some((d) => d.Succeeded + d.Failed + d.Cancelled + d.Other)) {
    // a factory: how many runs, and how many failed, every day (a quiet day shows as zero, not as a gap)
    const R = o.runs_by_day, labels = R.map((d) => d.day);
    const series = [["Succeeded", CH.ok], ["Failed", CH.bad], ["Cancelled", CH.grey], ["Other", "#EF802C"]]
      .filter(([k]) => R.some((d) => d[k]))
      .map(([k, c]) => ({ label: k === "Other" ? "Running / queued" : k, color: c, data: R.map((d) => d[k]) }));
    drawStacked("trendChart", labels, series, { tipAfter: (i) => [`${num(R[i].Succeeded + R[i].Failed + R[i].Cancelled + R[i].Other)} run(s) in total`] });
  } else if (o.trend && o.trend.length) {
    drawChart("trendChart", "line", o.trend.map((x) => x.day), o.trend.map((x) => x.score), o.trend_label || "Score",
      { min: 0, max: 100, dates: true, thresholds: s.kind === "adf" ? [] : TRUST_LINES, pointColors: o.trend.map((x) => color[tone(x.score)]),
        tipLabel: (c) => `${s.kind === "adf" ? "Success rate" : "Trust score"} ${num(c.parsed.y)}${s.kind === "adf" ? "%" : ""}` });
  } else {
    if (state.charts.trendChart) { state.charts.trendChart.destroy(); delete state.charts.trendChart; }
    $("trendChart").parentElement.classList.add("hidden");
    $("trendEmpty").textContent = "No history yet: the chart fills in as checks and runs are recorded.";
    $("trendEmpty").classList.remove("hidden");
  }
  $("obsKpis").innerHTML = o.kpis.map((k) => `<div class="kpi ${k.tone && Number(k.value) ? k.tone : ""}"><div class="l">${esc(k.label)}</div>
    <div class="v">${typeof k.value === "number" ? num(k.value) : esc(k.value)}</div>${k.sub ? `<div class="small muted">${esc(k.sub)}</div>` : ""}</div>`).join("");
  $("dims").innerHTML = o.dimensions.length
    ? o.dimensions.map((d) => `<div class="bar"><div class="t"><span>${esc(d.label)}</span><b>${d.value}%</b></div>
        <div class="track"><div class="fill" style="width:${d.value}%;background:${color[tone(d.value)]}"></div></div></div>`).join("")
    : `<p class="muted">Nothing measured yet.</p>`;
  $("reasons").innerHTML = t.reasons.length ? "<p><b>Why not 100:</b> " + t.reasons.map((r) => `−${r.points} ${esc(r.reason)}`).join(" · ") + "</p>"
    : t.score != null ? "<p>Nothing is lowering the score.</p>" : "";
  $("itemsTitle").textContent = o.items_title || "What is watched";
  $("itemsBody").innerHTML = o.items.length
    ? o.items.map((i) => { const [c, l] = STATUS_CHIP[i.status] || ["grey", "Not run"];
        return `<div class="item"><div>${esc(i.name)} <span class="small muted">${esc(i.detail || "")}</span></div><span class="chip ${c}">${esc(i.label || l)}</span></div>`; }).join("")
    : `<p class="muted small">${esc(o.empty || "Nothing yet.")}</p>`
      + (s.kind === "db" ? `<button class="btn btn-sm" onclick="document.querySelector('#tabs [data-page=estate-checks-new]').click()">+ Add a rule</button>` : "");
}

// ------------------------------------------------------------------ 2. detect
function since(t) {
  if (!t) return "";
  const d = new Date(String(t).replace(" ", "T").replace(/(\.\d{3})\d+/, "$1"));
  const h = (Date.now() - d) / 36e5;
  return isNaN(h) ? "" : h < 1 ? "open < 1 h" : h < 48 ? `open ${Math.round(h)} h` : `open ${Math.round(h / 24)} days`;
}
function incidentRow(i) {
  const appr = i.approval ? `<span class="chip warn">remediation approved by ${esc(i.approval.approved_by)}</span>` : "";
  const ack = i.acknowledged_by ? `<span class="chip grey">acknowledged by ${esc(i.acknowledged_by)}</span>` : "";
  const age = since(i.first_seen);
  return `<div class="incident"><span class="sev ${i.severity}">${i.severity}</span>
    <div class="body"><div class="t">${esc(i.title)} ${appr} ${ack}</div><div class="small muted">${esc(i.summary)}${age ? ` · ${age}` : ""}</div></div>
    <div class="inc-acts">${i.acknowledged_by ? "" : `<button class="btn light" data-ack="${esc(i.id)}">Acknowledge</button>`}
    <button class="btn light" data-contact="${esc(i.title + " — " + i.summary)}">Contact owner</button>
    ${recordsButton(i)}<button class="btn light" onclick="go('investigate','${esc(i.id)}')">Investigate</button></div></div>`;
}
document.addEventListener("click", async (e) => {                // Acknowledge: "someone is on it" (needs a sign-in when sign-in is on)
  const b = e.target.closest("[data-ack]");
  if (!b) return;
  b.disabled = true;
  try {
    const r = await fetch(`/api/incidents/${encodeURIComponent(b.dataset.ack)}/ack`, { method: "POST", credentials: "same-origin" });
    const body = await r.json().catch(() => ({}));
    if (r.status === 401) throw new Error("Sign in first (Data estate → Connections), then acknowledge");
    if (!r.ok) throw new Error(body.detail || body.error || r.statusText);
    toast("Acknowledged");
    state.version = null;
    await checkVersion(true);
  } catch (x) { toast(x.message); b.disabled = false; }
});
// held-back rows behind an incident -> the Rejected records page, filtered to that date / table / rule
function recordsButton(i) {
  if (!window.DQ || !["REJECTED_ROWS", "SETTLEMENT_MISMATCH"].includes(i.type) || !i.business_date) return "";
  const m = i.metrics || {};
  const ent = i.type === "REJECTED_ROWS" ? m.entity : "payments", rule = i.type === "REJECTED_ROWS" ? m.rule_name : "";
  return `<button class="btn light" onclick="DQ.open('${esc(i.business_date)}','${esc(ent || "")}','${esc(rule || "")}')">See records</button>`;
}
function renderDetect() {
  const open = state.incidents.open;
  const top = open[0];
  $("detectBanner").innerHTML = top
    ? `<div class="banner bad"><h2>${top.severity} RISK DETECTED: ${esc(top.title)}</h2><div>${esc(top.summary)}</div></div>`
    : `<div class="banner ok"><h2>NO OPEN INCIDENTS</h2><div>Every check passed on the latest data.</div></div>`;
  $("incidentSearch").value = state.incidentFilters.query;
  $("incidentSeverity").value = state.incidentFilters.severity;
  const severityOptions = [
    { value: "", label: "All" },
    { value: "CRITICAL", label: "Critical" },
    { value: "HIGH", label: "High" },
    { value: "MEDIUM", label: "Medium" },
    { value: "LOW", label: "Low" },
  ];
  $("incidentSeveritySummary").innerHTML = severityOptions.map(({ value, label }) => {
    const count = value ? open.filter((i) => i.severity === value).length : open.length;
    return `<button type="button" class="incident-severity-filter" data-severity="${value}" aria-pressed="false" aria-label="${label}: ${count} open incident${count === 1 ? "" : "s"}">
      <span>${label}</span><strong>${count}</strong></button>`;
  }).join("");
  $("incidentSeveritySummary").querySelectorAll("button").forEach((button) => {
    button.onclick = () => {
      $("incidentSeverity").value = button.dataset.severity;
      $("incidentSeverity").dispatchEvent(new Event("change"));
    };
  });
  const applyFilters = () => {
    state.incidentFilters.query = $("incidentSearch").value.trim();
    state.incidentFilters.severity = $("incidentSeverity").value;
    $("incidentSeveritySummary").querySelectorAll("button").forEach((button) => {
      button.setAttribute("aria-pressed", String(button.dataset.severity === state.incidentFilters.severity));
    });
    const query = state.incidentFilters.query.toLowerCase();
    const filtered = open.filter((i) => {
      const matchesSeverity = !state.incidentFilters.severity || i.severity === state.incidentFilters.severity;
      const searchable = `${i.severity} ${i.title} ${i.summary} ${i.type}`.toLowerCase();
      return matchesSeverity && (!query || searchable.includes(query));
    });
    $("incidentCount").textContent = open.length
      ? `Showing ${filtered.length} of ${open.length} open`
      : "0 open";
    $("incidentExport").disabled = filtered.length === 0;
    $("incidentExport").title = `Download ${filtered.length} matching open incident${filtered.length === 1 ? "" : "s"} as CSV`;
    $("incidentExport").onclick = () => exportIncidents(filtered);
    $("incidentList").innerHTML = filtered.length
      ? filtered.map(incidentRow).join("")
      : open.length
        ? `<div class="incident-empty"><p>No open incidents match these filters.</p><button class="btn light btn-sm" id="incidentNoMatchClear" type="button">Clear filters</button></div>`
        : "<p class='muted'>Nothing open.</p>";
    const clear = $("incidentNoMatchClear");
    if (clear) clear.onclick = clearIncidentFilters;
  };
  $("incidentSearch").oninput = applyFilters;
  $("incidentSeverity").onchange = applyFilters;
  $("incidentFilterClear").onclick = clearIncidentFilters;
  applyFilters();
}

function clearIncidentFilters() {
  state.incidentFilters = { query: "", severity: "" };
  $("incidentSearch").value = "";
  $("incidentSeverity").value = "";
  $("incidentSearch").dispatchEvent(new Event("input"));
}

// ------------------------------------------------------------------ 3. investigate
let investigationRequest = 0;
function fillSelect(sel, chosen) {
  const open = state.incidents.open;
  $(sel).innerHTML = open.map((i) => `<option value="${i.id}" ${i.id === chosen ? "selected" : ""}>${i.severity} · ${esc(i.title)}</option>`).join("");
  return chosen && open.some((i) => i.id === chosen) ? chosen : open[0] && open[0].id;
}
$("invSelect").addEventListener("change", (e) => renderInvestigate(e.target.value));
async function renderInvestigate(id) {
  const request = ++investigationRequest;
  id = fillSelect("invSelect", id || state.selected);
  state.selected = id;
  if (!id) {
    $("invBody").innerHTML = "<div class='banner ok'><h2>Nothing to investigate</h2>No open incidents. When an issue is detected, you can review its evidence here.</div>";
    return;
  }
  $("invBody").innerHTML = `<div class="investigation-loading" role="status">Checking the evidence for this incident…</div>`;
  let inv;
  try {
    inv = await api("/api/investigate/" + encodeURIComponent(id) + "?" + sq());
  } catch (error) {
    if (request !== investigationRequest) return;
    $("invBody").innerHTML = `<div class="banner warn" role="alert"><h2>Could not load investigation</h2>
      <p>${esc(error.message || "The evidence service is unavailable.")}</p>
      <button class="btn light btn-sm" id="invRetry" type="button">Try again</button></div>`;
    $("invRetry").addEventListener("click", () => renderInvestigate(id));
    return;
  }
  if (request !== investigationRequest) return;
  if (!inv.found) {
    $("invBody").innerHTML = `<div class="banner warn"><h2>Incident no longer open</h2>
      <p>This issue is no longer in the active incident list.</p>
      <button class="btn light btn-sm" id="invBack" type="button">Return to Detect</button></div>`;
    $("invBack").addEventListener("click", () => go("detect"));
    return;
  }
  const incident = inv.incident;
  const checks = Array.isArray(inv.checks) ? inv.checks : [];
  const confirmed = checks.filter((check) => check.ok === true).length;
  const needsReview = checks.filter((check) => check.ok === false).length;
  const unavailable = checks.filter((check) => check.ok == null).length;
  const evidenceTone = needsReview ? "bad" : unavailable ? "warn" : "ok";
  const evidenceSummary = checks.length
    ? `${confirmed} facts confirmed · ${needsReview} need review · ${unavailable} unavailable`
    : "No evidence checks returned";
  const impact = inv.impact || {};
  const goldViews = Array.isArray(impact.gold_views) ? impact.gold_views : [];
  const metrics = [
    ["Business date", impact.business_date || incident.business_date || "—"],
    [impact.views_label || "Gold views affected", goldViews.length],
    ["Revenue at risk", impact.amount_at_risk == null ? "—" : inr(impact.amount_at_risk)],
  ].filter(([label, value]) => isPoc() || value !== "—");      // other sources have no business date / revenue: no empty tiles
  $("invBody").innerHTML = `
    <div class="investigation-head">
      <div class="investigation-title">
        <span class="sev ${esc(incident.severity)}">${esc(incident.severity)}</span>
        <span class="chip grey">${esc(String(incident.type || "incident").replace(/_/g, " "))}</span>
        <h2>${esc(incident.title)}</h2>
        <p>${esc(incident.summary)}</p>
      </div>
      <div class="investigation-actions">
        <button class="btn light btn-sm" id="invBack" type="button">← Detect</button>
        ${inv.check_id ? `<button class="btn light btn-sm" id="invOpenCheck" type="button">Open the rule</button>` : ""}
        ${inv.run_id ? `<button class="btn light btn-sm" id="invOpenRun" type="button">Open the ADF run</button>` : ""}
        <button class="btn btn-sm" id="invImpact" type="button">See impact &amp; decide →</button>
      </div>
    </div>
    <div class="investigation-metrics">${metrics.map(([label, value]) => `<div class="investigation-metric"><span>${esc(label)}</span><b>${esc(value)}</b></div>`).join("")}</div>
    <div class="investigation-root">
      <div><p class="investigation-kicker">ROOT CAUSE</p><h3>${esc(inv.root_cause || "The available evidence does not identify a root cause yet.")}</h3></div>
      <div class="evidence-summary ${evidenceTone}">
        <b>${esc(evidenceSummary)}</b>
        <small>Review checks below; unavailable data is not treated as a match.</small>
      </div>
    </div>
    <div class="grid g-2">
      <div class="card">
        <div class="investigation-section-head"><div><h3>Evidence checks</h3><p class="small muted">Each check includes the data source used.</p></div>
          <span class="chip ${evidenceTone}">${esc(evidenceSummary)}</span></div>
        ${checks.length ? checks.map((check) => `<div class="check">
          <span class="${check.ok === true ? "ok" : check.ok === false ? "bad" : "grey"}"
            aria-label="${check.ok === true ? "Corroborated" : check.ok === false ? "Needs review" : "Unavailable"}">
            ${check.ok === true ? "✓" : check.ok === false ? "!" : "–"}</span>
          <span>${esc(check.text)}</span><div class="src">${esc(check.source || "Source not specified")}</div></div>`).join("")
          : `<p class="muted">No evidence checks were returned for this incident.</p>`}
      </div>
      <div class="card"><h3>Evidence path</h3><ol class="chain">${(Array.isArray(inv.chain) ? inv.chain : []).map((step) => `<li>${esc(step)}</li>`).join("")}</ol></div>
    </div>
    ${Array.isArray(inv.sample) && inv.sample.length ? `<div class="card"><h3>Affected records <span class="small muted">(${inv.sample.length} sample rows)</span></h3>${objTable(inv.sample)}</div>` : ""}
    ${goldViews.length ? `<div class="card"><h3>${esc(impact.views_label || "Downstream Gold views")}</h3><div class="investigation-views">${goldViews.map((view) => `<code>${esc(view)}</code>`).join("")}</div></div>` : ""}`;
  $("invBack").addEventListener("click", () => go("detect"));
  $("invImpact").addEventListener("click", () => go("impact", id));
  if ($("invOpenCheck")) $("invOpenCheck").onclick = () => { CHECKS.open(inv.check_id); document.querySelector('#tabs [data-page="estate-checks"]').click(); };
  if ($("invOpenRun")) $("invOpenRun").onclick = () => { ADF.open(inv.run_id); document.querySelector('#tabs [data-page="estate-adf-runs"]').click(); };
  if (inv.sql) $("invBody").insertAdjacentHTML("beforeend", `<div class="card"><details><summary>SQL the check ran</summary><pre class="code">${esc(inv.sql)}</pre></details></div>`);
  $("invBody").querySelector(".investigation-root").insertAdjacentHTML("afterend", `<div id="aiExplain"></div>`);
  aiExplain(id, "aiExplain", "investigate");
}

// ---- AI explanation (Investigate + Impact): written from the evidence pack; numbers not found in the evidence are flagged
const AI_CONF = { high: "ok", medium: "warn", low: "bad" };
async function aiExplain(id, boxId, where, generate = false) {
  const box = $(boxId);
  if (!box) return null;
  const off = state.overview && state.overview.llm === "none";
  if (off) { box.innerHTML = ""; return null; }
  if (generate) box.innerHTML = `<div class="card ai-card"><div class="ai-head"><span class="ai-badge">AI</span><b>Reading the evidence…</b></div><p class="muted small">Usually 5 to 20 seconds.</p></div>`;
  let r;
  try { r = await api(`/api/ai/explain/${encodeURIComponent(id)}?${sq()}${generate ? "&refresh=" + (generate === "again") : "&cached_only=true"}`); }
  catch (e) { box.innerHTML = `<div class="card ai-card"><div class="error-box">${esc(e.message)}</div></div>`; return null; }
  if (!$(boxId)) return r;
  if (!r.available) {
    box.innerHTML = `<div class="card ai-card"><div class="ai-head"><span class="ai-badge">AI</span><b>${where === "impact" ? "Draft a fix plan with AI" : "Explain this incident with AI"}</b><span class="grow"></span>
        <button class="btn btn-sm" id="${boxId}Go">${where === "impact" ? "Draft fix plan" : "Explain with AI"}</button></div>
      <p class="muted small">${r.not_generated ? "The model reads only this incident's evidence above (checks, rows, SQL, impact) and writes the root cause and fix steps. Numbers it writes are checked against the evidence."
        : esc(r.reason || "")}</p></div>`;
    $(boxId + "Go").onclick = () => aiExplain(id, boxId, where, true);
    return r;
  }
  const list = (xs) => (xs || []).length ? `<ul>${xs.map((x) => `<li>${esc(x)}</li>`).join("")}</ul>` : "";
  const ol = (xs) => (xs || []).length ? `<ol>${xs.map((x) => `<li>${esc(x)}</li>`).join("")}</ol>` : "";
  box.innerHTML = `<div class="card ai-card">
      <div class="ai-head"><span class="ai-badge">AI</span><b>${esc(r.headline || "AI explanation")}</b>
        <span class="chip ${AI_CONF[r.confidence] || "grey"}">${esc(r.confidence || "?")} confidence</span><span class="grow"></span>
        <button class="btn light btn-sm" id="${boxId}Again" title="Ask the model again">Regenerate</button></div>
      ${where === "investigate" ? `<p>${esc(r.root_cause || "")}</p>${r.because && r.because.length ? `<h4>Because</h4>${list(r.because)}` : ""}` : ""}
      ${r.business_impact ? `<p class="small"><b>Impact:</b> ${esc(r.business_impact)}</p>` : ""}
      ${where === "impact" || !r.because ? `<h4>Fix plan</h4>${ol(r.fix_steps)}` : `<details><summary>Fix plan (${(r.fix_steps || []).length} steps)</summary>${ol(r.fix_steps)}</details>`}
      ${r.prevent && r.prevent.length ? `<h4>Stop it happening again</h4>${list(r.prevent)}` : ""}
      ${r.unknowns && r.unknowns.length ? `<p class="small muted"><b>Not shown by the evidence:</b> ${r.unknowns.map(esc).join(" · ")}</p>` : ""}
      ${r.unverified && r.unverified.length ? `<div class="banner warn small">Check these numbers yourself: ${r.unverified.map(esc).join(", ")} do not appear in the evidence.</div>` : ""}
      <p class="small muted">Written by ${esc(r.model || "the AI model")} from this incident's evidence only${r.generated_at ? ` · ${esc(String(r.generated_at).slice(0, 16))}` : ""}. Review before acting.</p></div>`;
  $(boxId + "Again").onclick = () => aiExplain(id, boxId, where, "again");
  if (where === "impact") {                                     // the AI steps can be added to the approval, unticked by default
    const acts = document.querySelector("#impBody .actions");
    if (acts) {
      acts.querySelectorAll("label.ai-action").forEach((l) => l.remove());
      const words = (t) => new Set(String(t).toLowerCase().replace(/[^a-z0-9 ]/g, " ").split(/\s+/).filter((w) => w.length > 2));
      const known = [...acts.querySelectorAll("input:not([value^='AI: '])")].map((i) => words(i.value));
      const same = (a, b) => { const n = [...a].filter((w) => b.has(w)).length; return n / Math.max(1, Math.min(a.size, b.size)) >= 0.7; };
      const fresh = (r.fix_steps || []).filter((s) => !known.some((k) => same(words(s), k)));   // only steps that add something
      acts.insertAdjacentHTML("beforeend", fresh.map((s) =>
        `<label class="ai-action"><input type="checkbox" value="${esc("AI: " + s)}"> <span class="ai-badge sm">AI</span> ${esc(s)}</label>`).join(""));
    }
  }
  return r;
}

// ------------------------------------------------------------------ 4. lineage
let linView = "graph";
try { linView = localStorage.getItem("dt.linView") || "graph"; } catch (e) {}
document.querySelectorAll("#linToggle button").forEach((b) => b.addEventListener("click", () => {
  linView = b.dataset.view;
  try { localStorage.setItem("dt.linView", linView); } catch (e) {}
  document.querySelectorAll("#linToggle button").forEach((x) => x.classList.toggle("active", x === b));
  $("linBody").innerHTML = "<p class='muted'>Loading…</p>";
  renderLineage();
}));
async function renderLineage() {
  const lin = await api("/api/lineage?" + sq());
  $("linToggle").classList.toggle("hidden", lin.kind !== "poc");
  if (lin.kind !== "poc") {                                     // another source: its own flows (factory copy steps, or watched tables -> views)
    const C = { PASS: "ok", WARN: "warn", FAIL: "bad", ERROR: "bad" };
    $("linSub").textContent = lin.note || "";
    $("linBody").innerHTML = `<div class="card"><h3>${esc(lin.title)}</h3>${lin.flows.length ? `<div class="tablewrap" style="max-height:none"><table>
      <thead><tr><th>From</th><th>Through</th><th>To</th><th>Status</th></tr></thead><tbody>${lin.flows.map((f) => `<tr><td class="small">${esc(f.from)}</td>
      <td class="small">${esc(f.via)}</td><td class="small">${esc(f.to)}</td><td><span class="chip ${C[f.status] || "grey"}">${esc({ PASS: "OK", WARN: "Warning", FAIL: "Problem", ERROR: "Error" }[f.status] || "Not checked")}</span></td></tr>`).join("")}
      </tbody></table></div>` : `<p class="muted">${esc(lin.note)}</p>`}</div>`;
    return;
  }
  $("linSub").textContent = `Latest batch ${lin.batch_id} (${lin.business_date}). Gold views come from the PostgreSQL catalog (information_schema.view_table_usage).`;
  document.querySelectorAll("#linToggle button").forEach((b) => b.classList.toggle("active", b.dataset.view === linView));
  if (linView === "graph" && window.LineageGraph) {             // graph view (lineage-graph.js); List below is unchanged
    const pipe = await api("/api/pipeline").catch(() => null);
    const bad = lin.settlement.filter((r) => r.recon_status === "MISMATCH");
    $("linBody").innerHTML = `
      <div class="banner ${bad.length ? "bad" : "ok"}"><h2>External target: payment gateway settlement</h2>
        ${bad.length ? bad.map((r) => `${r.business_date}: gateway settled ${num(r.settled_payments)} payments, silver has ${num(r.silver_payments)} → ${r.payment_difference} payments / ${inr(r.amount_difference)} short`).join("<br>") : "All dates match."}</div>
      <div class="card lg-card"><div id="linGraph"></div></div>
      <div class="card"><h3>Settlement reconciliation by date</h3>${objTable(lin.settlement)}</div>`;
    window.LineageGraph.render($("linGraph"), lin, pipe);
    return;
  }
  const node = (name, count, sub, cls) => `<div class="node ${cls}"><div class="n">${esc(name)}</div><div class="c">${count}</div><div class="s">${sub}</div></div>`;
  const arrow = (txt) => `<div class="arrow">${txt || ""}</div>`;
  const rows = lin.entities.map((e) => {
    const srcCls = e.source_to_bronze_gap ? "bad" : "";
    const silCls = e.unexplained_loss ? "bad" : e.rejected || e.duplicates ? "warn" : "";
    const held = [e.rejected ? `${e.rejected} rejected` : "", e.duplicates ? `${e.duplicates} duplicates` : "", e.scd2_unchanged ? `${e.scd2_unchanged} unchanged (SCD2)` : ""].filter(Boolean).join(", ");
    return `<div class="card"><h3>${esc(e.entity)} <span class="chip ${e.status === "OK" ? "ok" : e.status === "MISSING" ? "bad" : "warn"}">${e.status.replace("_", " ")}</span></h3>
      <div class="flow">
        ${node(e.source_file, e.source_rows == null ? "n/a" : num(e.source_rows) + " rows", "source", srcCls)}
        ${arrow(e.source_to_bronze_gap ? `−${e.source_to_bronze_gap}` : "")}
        ${node(e.bronze_table, num(e.bronze_rows) + " rows", "landed as delivered", srcCls)}
        ${arrow(e.rejected || e.duplicates ? "held back" : "")}
        ${node(e.silver_table, num(e.silver_loaded) + " loaded", held || "clean", silCls)}
        ${arrow()}
        ${node("gold", e.gold_views.length + " view(s)", e.gold_views.join(", ") || "–", silCls)}
      </div></div>`;
  }).join("");
  const recon = lin.settlement;
  const bad = recon.filter((r) => r.recon_status === "MISMATCH");
  $("linBody").innerHTML = `
    <div class="banner ${bad.length ? "bad" : "ok"}"><h2>External target: payment gateway settlement</h2>
      ${bad.length ? bad.map((r) => `${r.business_date}: gateway settled ${num(r.settled_payments)} payments, silver has ${num(r.silver_payments)} → ${r.payment_difference} payments / ${inr(r.amount_difference)} short`).join("<br>") : "All dates match."}</div>
    ${rows}
    <div class="card"><h3>Settlement reconciliation by date</h3>${objTable(recon)}</div>`;
}

// ------------------------------------------------------------------ 5. impact
$("impSelect").addEventListener("change", (e) => renderImpact(e.target.value));
async function renderImpact(id) {
  id = fillSelect("impSelect", id || state.selected);
  state.selected = id;
  if (!id) { $("impBody").innerHTML = "<div class='banner ok'><h2>Nothing to decide</h2>No open incidents.</div>"; return; }
  const inv = await api("/api/investigate/" + encodeURIComponent(id) + "?" + sq());
  if (!inv.found) { $("impBody").innerHTML = "<p>That incident is no longer open.</p>"; return; }
  const im = inv.impact, inc = inv.incident;
  const tiles = [[im.views_label || "Gold views affected", im.gold_views.length], ["Business date", im.business_date || "–"],
    ["Rows affected", inc.metrics.payment_difference ?? inc.metrics.n ?? inc.metrics.duplicates ?? "–"],
    ["Revenue at risk", im.amount_at_risk != null ? inr(im.amount_at_risk) : "–"]];
  $("impBody").innerHTML = `
    <div class="kpis" style="grid-template-columns:repeat(4,1fr)">${tiles.map(([l, v], i) => `<div class="kpi ${i === 3 && im.amount_at_risk ? "bad" : ""}"><div class="l">${l}</div><div class="v">${esc(typeof v === "number" ? num(v) : v)}</div></div>`).join("")}</div>
    <div class="grid g-2">
      <div class="card"><h3>What it touches</h3>
        <p><b>${esc(inc.title)}</b></p>
        <p>${esc(im.views_label || "Gold views reading")} <code>${esc(im.entity || "")}</code>: ${im.gold_views.map((v) => `<span class="chip warn">${im.views_label ? "" : "gold."}${esc(v)}</span>`).join(" ") || "–"}</p>
        <p class="small muted">${im.views_label ? "Everything listed reads the affected data, so it shows the problem too until the fix is loaded." : "Revenue at risk is the exact amount of the gap in the data, not an estimate. Reports built on the views above show the reduced numbers until the fix is loaded."}</p></div>
      <div class="card"><h3>Recommended remediation</h3>
        <div class="actions">${im.actions.map((a) => `<label><input type="checkbox" checked value="${esc(a)}"> ${esc(a)}</label>`).join("")}</div>
        <div class="field"><input id="approver" placeholder="Approved by (your name)"></div>
        ${inc.approval ? `<p class="small">Already approved by <b>${esc(inc.approval.approved_by)}</b> at ${esc(inc.approval.approved_at)}. Waiting for the next pipeline run to verify.</p>` : ""}
        <button class="btn" id="approveBtn" style="width:100%">Approve remediation</button></div>
    </div>
    <div id="autofixBox"></div>`;
  $("impBody").querySelector(".kpis").insertAdjacentHTML("afterend", `<div id="aiFix"></div>`);
  aiExplain(id, "aiFix", "impact");
  renderAutofix(id);
  $("approveBtn").onclick = async () => {
    const actions = [...document.querySelectorAll(".actions input:checked")].map((x) => x.value);
    const by = $("approver").value.trim() || "web user";
    $("approveBtn").disabled = true;
    try {
      await api("/api/approve", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ incident_id: id, title: inc.title, actions, approved_by: by }) });
      toast("Approved and logged in audit.remediation_log");
      await loadCore();
      go("verify");
    } catch (e) { showError(e.message); $("approveBtn").disabled = false; }
  };
}

// ---- auto-fix with approval (5. Impact): evidence-based correction of rejected rows
let autofixSeq = 0;
async function renderAutofix(id) {
  const my = ++autofixSeq;                       // the Impact page can be drawn twice quickly: only the newest wins
  const box = $("autofixBox");
  if (!box) return;
  box.innerHTML = "<div class='card'><h3>Auto-fix</h3><p class='muted'>Checking whether a safe fix is available…</p></div>";
  let p;
  try {
    p = await api("/api/autofix/" + encodeURIComponent(id));
  } catch (e) {
    if (my !== autofixSeq) return;
    box.innerHTML = `<div class="card"><h3>Auto-fix</h3><div class="error-box">Could not load the fix proposal: ${esc(e.message)}</div>
      <button class="btn light btn-sm" id="afRetry">Try again</button></div>`;
    box.querySelector("#afRetry").onclick = () => renderAutofix(id);
    return;
  }
  if (my !== autofixSeq) return;
  if (!p.available) {
    box.innerHTML = p.reason && !/no automatic fix exists/i.test(p.reason) ? `<div class="card"><h3>Auto-fix</h3><p class="muted">${esc(p.reason)}</p></div>` : "";
    return;
  }
  const e = p.effect || {};
  const effect = e.settled_payments != null
    ? `<div class="af-effect"><div><span class="l">Silver vs gateway now</span><b class="bad">${num(e.silver_now)} / ${num(e.settled_payments)}</b><span class="small">gap ${inr(e.amount_gap_now)}</span></div>
        <div class="af-arrow">→</div><div><span class="l">After the fix</span><b class="ok">${num(e.silver_after)} / ${num(e.settled_payments)}</b><span class="small">gap ${inr(e.amount_gap_after)}</span></div></div>` : "";
  box.innerHTML = `<div class="card autofix">
      <div class="af-head"><h3>Auto-fix with approval</h3><span class="chip ok">evidence-based</span></div>
      <p class="big">${esc(p.title)}</p>
      <p class="muted">Every value below comes from your own data; nothing is guessed. Applying runs in one transaction, records each change in
        <code>audit.dq_corrections</code> and the next pipeline runs keep the fix. Rows without clear evidence stay for the source team.</p>
      ${effect}
      <div class="tablewrap"><table><thead><tr><th>Record</th><th>Field</th><th>Now</th><th>Fixed value</th><th>Evidence</th></tr></thead><tbody>
        ${p.rows.map((r) => `<tr><td>${esc(r.record_key)}</td><td>${esc(r.column)}</td><td class="bad">${r.current == null ? "(missing)" : esc(r.current)}</td>
          <td class="ok"><b>${esc(r.proposed)}</b></td><td class="small">${esc(r.evidence)}</td></tr>`).join("")}</tbody></table></div>
      ${p.skipped.length ? `<p class="small warn">${p.skipped.length} row(s) cannot be fixed automatically and stay rejected.</p>` : ""}
      <details class="af-sql"><summary>What will run</summary><pre class="code">${esc(p.sql_preview)}</pre></details>
      <div class="af-actions"><button class="btn" id="afApply">Approve &amp; apply fix (${p.rows.length} rows)</button>
        <span class="small muted">Needs a portal admin login (Data estate). Resolves: ${p.resolves.map(esc).join(", ")}.</span></div>
      <div id="afResult"></div></div>`;
  const applyBtn = box.querySelector("#afApply");
  if (!applyBtn) return;
  applyBtn.onclick = async () => {
    if (!confirm(`Apply the fix to ${p.rows.length} ${p.entity} row(s) of ${p.business_date}?\n\nThe corrected rows are loaded into silver now; every change is recorded.`)) return;
    applyBtn.disabled = true;
    applyBtn.textContent = "Applying…";
    try {
      const r = await fetch(`/api/estate/autofix/${encodeURIComponent(id)}/apply`, { method: "POST", credentials: "same-origin" });
      const body = await r.json().catch(() => ({}));
      if (r.status === 401 || r.status === 403) {
        $("afResult").innerHTML = `<div class="error-box">Only a portal admin can apply fixes. Log in under <b>Data estate → Connections</b> as an admin, then come back and click again.</div>`;
        applyBtn.disabled = false; applyBtn.textContent = `Approve & apply fix (${p.rows.length} rows)`; return;
      }
      if (!r.ok) throw new Error(body.detail || body.error || r.statusText);
      toast(`Fix applied: ${body.fixed} row(s) corrected`);
      state.version = null;                       // reload everything: the data just changed
      await loadCore();
      go("verify");
    } catch (err) { $("afResult").innerHTML = `<div class="error-box">${esc(err.message)}</div>`; applyBtn.disabled = false; applyBtn.textContent = `Approve & apply fix (${p.rows.length} rows)`; }
  };
}

// ------------------------------------------------------------------ 6. verify
async function renderVerify() {
  const { open, resolved } = state.incidents;
  const t = state.overview.trust, p = state.overview.pipeline;
  const waiting = open.filter((i) => i.approval);
  $("verifyBody").innerHTML = `
    ${resolved.length ? `<div class="banner ok"><h2>INCIDENT(S) RESOLVED</h2>${resolved.map((r) => `✓ ${esc(r.incident_title)}${r.approved_by ? ` (approved by ${esc(r.approved_by)})` : ""}: no longer present in the latest data${r.resolved_at ? ` (since ${esc(String(r.resolved_at).slice(0, 16))})` : ""}.`).join("<br>")}</div>` : ""}
    <div class="grid g-2">
      <div class="card"><h3>Remediation log</h3>
        ${waiting.map((i) => `<div class="check"><span class="warn">⏳</span> <b>${esc(i.title)}</b><div class="small muted">Approved by ${esc(i.approval.approved_by)} at ${esc(i.approval.approved_at)}. Still present in the data: fix the source and re-run the pipeline.</div></div>`).join("")}
        ${resolved.map((r) => `<div class="check"><span class="ok">✓</span> <b>${esc(r.incident_title)}</b><div class="small muted">${esc(r.actions)}</div></div>`).join("")}
        ${!waiting.length && !resolved.length ? "<p class='muted'>Nothing approved or resolved in the last 7 days. Approve a fix on page 5; it shows as resolved once the data no longer has the problem.</p>" : ""}
      </div>
      <div class="card center"><div id="gauge3"></div><div class="gauge-label"><span class="${t.score == null ? "muted" : tone(t.score)}">${esc(t.label)}</span></div>
        <div class="small muted">${esc((state.overview.scope || {}).name || "")} · open incidents ${open.length}${p ? ` · latest run ${esc(p.batch.batch_id || "–")}: ${p.layers.map((l) => l.layer + " " + l.status).join(", ")}` : ""}</div></div>
    </div>`;
  gauge("gauge3", t.score);
}

// ------------------------------------------------------------------ pipeline log
async function renderPipeline() {
  await ensureCore();
  if (!isPoc()) {                                               // the pipeline log is the POC pipeline's own diary
    const s = state.overview.scope || {};
    $("pipeBody").innerHTML = `<div class="card"><h3>${esc(s.name || "This source")} has no POC pipeline log</h3>
      <p class="muted">The pipeline log shows the POC pipeline's own runs. ${s.kind === "adf" ? "For this factory, every run and step is on <b>ADF runs</b>."
        : "For this database, every rule and its runs are in <b>DQ Rule Studio</b>."}</p>
      <button class="btn btn-sm" onclick="document.querySelector('#tabs [data-page=${s.kind === "adf" ? "estate-adf-runs" : "estate-dq-rules"}]').click()">Open ${s.kind === "adf" ? "ADF runs" : "DQ Rule Studio"}</button></div>`;
    return;
  }
  const p = await api("/api/pipeline");
  const batches = (p.adls_batches || []).map((b) => {
    const cls = b.status === "SUCCEEDED" ? "ok" : b.status === "FAILED" ? "bad" : "warn";
    const action = b.status === "FAILED"
      ? `<button class="btn btn-sm" data-batch-action="retry" data-batch-id="${esc(b.batch_id)}">Retry failed batch</button>`
      : "";
    return `<div class="card">
      <div class="draft-head"><h3>${esc(b.batch_name)} <span class="chip ${cls}">${esc(b.status)}</span></h3>${action}</div>
      <div class="small muted">${esc(b.batch_folder)} · Attempt ${num(b.attempt_count)} · First seen ${esc(b.first_seen_at || "–")}</div>
      ${b.started_at ? `<div class="small muted">Started ${esc(b.started_at)}${b.finished_at ? ` · Finished ${esc(b.finished_at)}` : ""}</div>` : ""}
      ${b.pipeline_batch_id ? `<div class="small muted">Pipeline run ${esc(b.pipeline_batch_id)}</div>` : ""}
      ${b.retry_requested_by ? `<div class="small muted">Last run request by ${esc(b.retry_requested_by)}</div>` : ""}
      ${b.last_error ? `<div class="error-box">${esc(b.last_error)}</div>` : ""}
    </div>`;
  }).join("");
  $("pipeBody").innerHTML = `
    <div class="card"><h3>ADLS batch runs</h3>
      <p class="muted">Status is stored in PostgreSQL. Admins can retry failed batches using the files currently in ADLS.</p>
      ${batches || "<p class='muted'>No ADLS batches have been detected yet.</p>"}</div>
    <div class="card"><h3>Latest batch ${esc(p.batch.batch_id || "–")}: every step</h3>${objTable(p.steps, ["layer", "pipeline_id", "status", "rows_in", "rows_out", "rows_rejected", "duration_sec", "message"]) || "<p class='muted'>No step log yet (run --setup and the pipeline after the step-log update).</p>"}</div>
    <div class="card"><h3>Recent batches</h3>${objTable(p.history)}</div>
    <div class="card"><h3>Error log (failures and warnings)</h3>${objTable(p.errors, ["logged_at", "batch_id", "layer", "pipeline_id", "status", "error_type", "error_message"]) || "<p class='muted'>Empty.</p>"}</div>`;
  $("pipeBody").querySelectorAll("[data-batch-action]").forEach((button) => {
    button.addEventListener("click", async () => {
      if (!window.confirm("Retry this failed batch using the files currently in ADLS?")) return;
      button.disabled = true;
      try {
        const result = await api(`/api/pipeline/batches/${encodeURIComponent(button.dataset.batchId)}/retry`, { method: "POST" });
        toast(`Retry queued for ${result.batch_name}`);
        await renderPipeline();
      } catch (e) {
        button.disabled = false;
        toast(`Could not retry batch: ${e.message}`);
      }
    });
  });
}

// ------------------------------------------------------------------ ask
const EXAMPLES = ["What is the total revenue?", "Revenue last 7 days by day", "Revenue this month by payment method",
  "Revenue last month", "Revenue last quarter", "Revenue by region", "Is any data missing from source to target?",
  "Why is 2026-10-27 a mismatch?", "Which rows were rejected?", "What is the trust score?",
  "Did the last pipeline run succeed?", "Top 5 customers this month", "Show history of customer C00781"];
const EXAMPLES_DB = ["Which data checks are failing, and why?", "What is the worst open incident?", "Which tables are watched?",
  "What tables are in this database?", "How many rows were loaded today?", "What should I check first?"];
const EXAMPLES_ADF = ["Did any pipeline fail in the last day, and why?", "Which pipeline is the least reliable?", "How many rows were loaded this week?",
  "Is any pipeline's data stale?", "What does the newest failed run say?", "What should I check first?"];
async function renderAsk() {
  await ensureCore();
  const mode = state.overview ? state.overview.llm : "none";
  const s = (state.overview && state.overview.scope) || {};
  const ex = isPoc() ? EXAMPLES : s.kind === "adf" ? EXAMPLES_ADF : EXAMPLES_DB;
  $("askMode").textContent = `Asking about ${s.name || "the selected source"} (change it with Source at the top). ` +
    (mode === "none" ? "Mode: built-in agent (free, offline)." : `Mode: ${mode} model with read-only DataTrust tools.`);
  $("examples").innerHTML = ex.map((q) => `<button>${esc(q)}</button>`).join("");
  $("examples").querySelectorAll("button").forEach((b) => (b.onclick = () => askQuestion(b.textContent)));
  if (state.askScope !== state.scope) {                         // a new source starts a new conversation
    $("chatLog").innerHTML = "";
    state.history = [];
    state.askScope = state.scope;
  }
  if (!$("chatLog").children.length) addBot({ answer: isPoc()
    ? "Hi! Ask me anything about your revenue, data quality, missing data or pipeline runs. The answers come from the gold layer and refresh after every pipeline run."
    : `Hi! Ask me about **${s.name}**: ${s.kind === "adf" ? "its pipelines, runs, failures and loaded rows" : "its data checks, incidents and tables"}. I only answer from what DataTrust measured or can read, never from guesses.`,
    suggestions: ex.slice(0, 4) });
}
$("askForm").addEventListener("submit", (e) => { e.preventDefault(); const q = $("askInput").value.trim(); if (q) askQuestion(q); });

function addUser(q) {
  $("chatLog").insertAdjacentHTML("beforeend", `<div class="msg user"><div class="bubble">${esc(q)}</div></div>`);
}
let chartSeq = 0;
function addBot(r) {
  const id = "askChart" + ++chartSeq;
  const sugg = (r.suggestions || []).map((s) => `<button>${esc(s)}</button>`).join("");
  const html = `<div class="msg bot"><div class="bubble">${md(r.answer || "")}
      ${r.chart ? `<div class="chartbox"><canvas id="${id}"></canvas></div>` : ""}
      ${tableHtml(r.table, 200)}
      ${r.sources && r.sources.length ? `<div class="meta">Source: ${r.sources.map(esc).join(", ")}${r.mode && r.mode !== "rules" ? " · answered by " + esc(r.mode) : ""}</div>` : ""}
      ${sugg ? `<div class="sugg">${sugg}</div>` : ""}</div></div>`;
  $("chatLog").insertAdjacentHTML("beforeend", html);
  const last = $("chatLog").lastElementChild;
  last.querySelectorAll(".sugg button").forEach((b) => (b.onclick = () => askQuestion(b.textContent)));
  if (r.chart) {
    const money = /revenue/i.test(r.chart.label);
    drawChart(id, r.chart.type, r.chart.labels, r.chart.values, r.chart.label,
      r.chart.label === "Trust score" ? { min: 50, max: 100 } : { money });
  }
  $("chatLog").scrollTop = $("chatLog").scrollHeight;
}
async function askQuestion(q) {
  if (state.page !== "ask") go("ask");
  $("askInput").value = "";
  addUser(q);
  $("chatLog").insertAdjacentHTML("beforeend", `<div class="msg bot typing" id="typing">checking the data…</div>`);
  $("chatLog").scrollTop = $("chatLog").scrollHeight;
  try {
    const r = await api("/api/ask", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question: q, history: state.history, scope: state.scope }) });
    $("typing").remove();
    addBot(r);
    state.history.push({ role: "user", content: q }, { role: "assistant", content: r.answer });
  } catch (e) {
    $("typing").remove();
    addBot({ answer: "Sorry, that failed: " + e.message });
  }
}

// ------------------------------------------------------------------ start
(async function start() {
  await checkVersion();                      // loads the data and draws page 1
  setInterval(() => { if (!document.hidden) checkVersion(); }, 15000);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) checkVersion(); });
  $("refreshBtn").addEventListener("click", () => checkVersion(true));
  openFromLink();
})();
// links from alerts / e-mails: #investigate=<incident id>, #impact=<id>, #verify, #estate-people ...
async function openFromLink() {
  const m = decodeURIComponent(location.hash.slice(1)).match(/^([\w-]+)(?:=(.+))?$/);
  if (!m || !document.getElementById("page-" + m[1])) return;
  if (["investigate", "impact"].includes(m[1]) && m[2]) {        // an alert link: show the incident's own source first
    const sc = scopeOfIncident(m[2]);
    if (sc && sc !== state.scope) await setScope(sc);
  }
  if (m[2] && /^(db|adf):\d+$/.test(m[2])) {                     // #observe=db:3 / #detect=adf:3: a page of a given source
    if (m[2] !== state.scope) await setScope(m[2]);
    go(m[1]);
    history.replaceState(null, "", location.pathname);
    return;
  }
  const btn = document.querySelector(`#tabs button[data-page="${m[1]}"]`);
  if (m[1] === "estate-adf-runs" && m[2] && window.ADF) { ADF.open(m[2]); btn.click(); }   // #estate-adf-runs=<ADF run ID>
  else if (m[1] === "estate-dq-rejected" && m[2] && window.DQ) DQ.open(m[2]);   // #estate-dq-rejected=2026-10-08
  else if (m[1] === "estate-checks" && m[2] && window.CHECKS) { CHECKS.open(m[2]); btn.click(); }        // #estate-checks=<check id>
  else if (m[1] === "estate-checks-new" && m[2] && window.CHECKS) CHECKS.edit(m[2]);                     // #estate-checks-new=<id>: edit
  else if (m[2]) go(m[1], m[2]); else if (btn) btn.click();
  history.replaceState(null, "", location.pathname);
}
window.addEventListener("hashchange", openFromLink);
