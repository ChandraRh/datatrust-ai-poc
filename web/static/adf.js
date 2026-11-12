// Azure Data Factory pages (sidebar group "Azure Data Factory"): pipelines, issues, runs (+ run / pipeline detail), connect.
// Rendered by estate.js after login (pages estate-adf-*). Uses app.js (esc, num, toast) and estate.js (eapi, post, est, fmtBytes).
(function () {
  const A = { src: "", runId: null, pipe: null, f: { pipeline: "", status: "", days: 30, q: "" }, sig: null, charts: {}, editing: null };
  const api = (p, o) => eapi("/adf" + p, o);
  const isAdmin = () => est.user && est.user.role === "admin";
  const qs = (o) => new URLSearchParams(Object.entries(o).filter(([, v]) => v !== "" && v != null)).toString();

  // ------------------------------------------------------------------ formatting
  const parseT = (t) => (t ? new Date(String(t).replace(" ", "T").replace(/(\.\d{3})\d+/, "$1")) : null);
  const when = (t) => { const d = parseT(t); return d && !isNaN(d) ? d.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" }) : "–"; };
  const ago = (t) => {
    const d = parseT(t); if (!d || isNaN(d)) return "never";
    const s = (Date.now() - d) / 1000;
    return s < 60 ? "just now" : s < 3600 ? `${Math.round(s / 60)} min ago` : s < 86400 ? `${(s / 3600).toFixed(1)} h ago` : `${Math.round(s / 86400)} d ago`;
  };
  const dur = (ms) => {
    if (ms == null) return "–";
    const s = ms / 1000;
    if (s < 10) return `${s.toFixed(1)} s`;
    if (s < 60) return `${Math.round(s)} s`;
    if (s < 3600) return `${Math.floor(s / 60)}m ${String(Math.round(s % 60)).padStart(2, "0")}s`;
    return `${Math.floor(s / 3600)}h ${String(Math.round((s % 3600) / 60)).padStart(2, "0")}m`;
  };
  const n0 = (v) => (v == null ? "–" : Number(v).toLocaleString());
  const ST = { Succeeded: "ok", Failed: "bad", Cancelled: "grey", Cancelling: "grey", InProgress: "warn", Queued: "warn", "Not run": "grey",
    Skipped: "grey", PASS: "ok", INFO: "grey", WARN: "warn", FAIL: "bad", UNKNOWN: "grey" };
  const LABEL = { InProgress: "Running", Queued: "Queued", PASS: "Pass", INFO: "Info", WARN: "Difference", FAIL: "Fail", UNKNOWN: "Not checked" };
  const chip = (s) => `<span class="chip ${ST[s] || "grey"}">${esc(LABEL[s] || s || "–")}</span>`;
  const SEV = { CRITICAL: "bad", HIGH: "bad", MEDIUM: "warn", LOW: "grey" };
  const sevChip = (s) => `<span class="chip ${SEV[s]}">${esc(s)}</span>`;
  const HEALTH = { Healthy: "ok", "Needs attention": "warn", "At risk": "bad", Failing: "bad", "No runs yet": "grey" };
  const healthChip = (h) => `<span class="chip ${HEALTH[h] || "grey"}">${esc(h)}</span>`;
  const CHECK = { SOURCE_FILES: "Source files", SOURCE_ROWS: "Source row count", SKIPPED_ROWS: "Skipped rows", TARGET_TABLE: "Target table",
    TARGET_ROWS: "Target row count", SCHEMA_MATCH: "File vs table columns" };
  const errBox = (e) => `<div class="error-box">${esc(e.message || e)}</div>`;

  // ------------------------------------------------------------------ shared bits
  async function sourcesBar(page) {
    const srcs = await api("/sources");
    A.sources = srcs;
    if (A.src && !srcs.some((s) => String(s.source_id) === String(A.src))) A.src = "";
    const shown = A.src ? srcs.filter((s) => String(s.source_id) === String(A.src)) : srcs;
    const status = shown.map((s) => {
      const st = !s.enabled ? `<span class="chip grey">paused</span>` : s.last_sync_status === "RUNNING" ? `<span class="chip warn">syncing…</span>`
        : s.last_sync_status === "FAILED" ? `<span class="chip bad">sync failing</span>` : s.last_sync_status === "PARTIAL" ? `<span class="chip warn">partly read</span>`
        : s.last_success_at ? `<span class="chip ok">connected</span>` : `<span class="chip grey">waiting for first sync</span>`;
      return `<span class="adf-src">${srcs.length > 1 ? `<b>${esc(s.name)}</b> ` : ""}${st} <span class="small muted">synced ${ago(s.last_success_at)}</span></span>`;
    }).join("");
    return {
      none: !srcs.length,
      html: !srcs.length ? "" : `<div class="ctx adf-ctx">${srcs.length > 1 ? `<label>Factory <select id="adfSrc_${page}"><option value="">All factories</option>${srcs.map((s) =>
        `<option value="${s.source_id}" ${String(s.source_id) === String(A.src) ? "selected" : ""}>${esc(s.name)}</option>`).join("")}</select></label>` : ""}
        ${status}<span class="grow"></span>
        ${srcs.length ? `<button class="btn light btn-sm" id="adfSync_${page}" title="Read the factory now instead of waiting for the next automatic sync">↻ Sync now</button>` : ""}</div>`,
      bind() {
        const sel = $e("adfSrc_" + page);
        if (sel) sel.onchange = () => { A.src = sel.value; renderEstate(page); };
        const b = $e("adfSync_" + page);
        if (b) b.onclick = async () => {
          b.disabled = true;
          try {
            await Promise.all(shown.filter((s) => s.enabled).map((s) => post(`/adf/sources/${s.source_id}/sync`)));
            toast("Sync started: new runs appear in a few seconds");
            setTimeout(() => renderEstate(page), 4000);
          } catch (e) { toast(e.message); b.disabled = false; }
        };
      },
    };
  }
  const emptyState = () => `<div class="card adf-empty"><h3>No Azure Data Factory connected yet</h3>
    <p>Connect a factory and DataTrust reads its pipelines, every run and every step, the errors and their causes, and checks the
    source files and target tables the pipelines load.</p>
    <button class="btn" data-go="estate-adf-setup">Connect a Data Factory</button></div>`;
  const bindGo = (el) => el.querySelectorAll("[data-go]").forEach((b) => (b.onclick = () => go2(b.dataset.go)));
  const go2 = (page) => document.querySelector(`#tabs button[data-page="${page}"]`).click();
  function openRun(id) { A.runId = id; A.pipe = null; A.keep = true; go2("estate-adf-runs"); }
  function openPipe(sid, name) { A.pipe = { sid, name }; A.runId = null; A.keep = true; go2("estate-adf-runs"); }
  function bindLinks(root) {
    root.querySelectorAll("[data-run]").forEach((x) => (x.onclick = (e) => { e.preventDefault(); openRun(x.dataset.run); }));
    root.querySelectorAll("[data-pipe]").forEach((x) => (x.onclick = (e) => { e.preventDefault(); openPipe(Number(x.dataset.sid), x.dataset.pipe); }));
    bindGo(root);
  }
  const runLink = (id, text) => `<a href="#" data-run="${esc(id)}" class="mono">${esc(text || id.slice(0, 8))}</a>`;
  const pipeLink = (sid, name) => `<a href="#" data-pipe="${esc(name)}" data-sid="${sid}"><b>${esc(name)}</b></a>`;
  const spark = (runs) => `<span class="adf-spark" aria-label="last runs">${(runs || []).map((r) =>
    `<i class="${ST[r.s] || "grey"}" title="${esc(r.s)} · ${esc(when(r.t))} · ${dur(r.d)}${r.rows != null ? " · " + n0(r.rows) + " rows" : ""}" data-run="${esc(r.id)}"></i>`).join("")}</span>`;

  function chart(id, config) {
    if (A.charts[id]) A.charts[id].destroy();
    const c = $e(id);
    if (c && window.Chart) A.charts[id] = new Chart(c, config);
  }

  function issueCard(i, compact) {
    const c = i.cause;
    return `<div class="adf-issue sev-${esc(i.severity)}">
      <div class="draft-head">${sevChip(i.severity)} <b>${esc(i.title)}</b>
        ${i.system ? `<span class="chip grey">${esc(i.system)}</span>` : ""}
        <span class="grow"></span>${i.run_id ? `<button class="btn light btn-sm" data-run="${esc(i.run_id)}">Open run</button>` : ""}
        ${i.pipeline && !compact ? `<button class="btn light btn-sm" data-pipe="${esc(i.pipeline)}" data-sid="${i.source_id}">Pipeline</button>` : ""}</div>
      <div class="small">${esc(i.summary)}</div>
      ${!compact && c ? `<div class="small adf-why"><b>Why:</b> ${esc(c.explanation)}</div>` : ""}
      ${!compact && i.evidence && i.evidence.filter(Boolean).length ? `<pre class="adf-err">${esc(i.evidence.filter(Boolean).join("\n"))}</pre>` : ""}
      ${!compact && i.fixes && i.fixes.length ? `<div class="small"><b>What to do:</b><ol class="adf-fixes">${i.fixes.map((f) => `<li>${esc(f)}</li>`).join("")}</ol></div>` : ""}
      ${A.sources && A.sources.length > 1 ? `<div class="small muted">${esc(i.factory)}</div>` : ""}</div>`;
  }

  // ================================================================ OVERVIEW
  async function pageOverview() {
    const bar = await sourcesBar("estate-adf-overview");
    if (bar.none) return { html: bar.html + emptyState(), bind: () => bindGo($e("body-estate-adf-overview")) };
    const ov = await api("/overview?" + qs({ source_id: A.src }));
    const k = ov.kpis;
    const tone = (v) => (v == null ? "" : v >= 90 ? "" : v >= 75 ? "warn" : "bad");
    const pipes = ov.pipelines.map((p) => {
      const lr = p.last_run;
      return `<tr>
        <td>${pipeLink(p.source_id, p.name)}${p.folder ? `<div class="small muted">${esc(p.folder)}</div>` : ""}${p.removed ? `<div class="small warn">no longer in the factory</div>` : ""}</td>
        <td>${healthChip(p.health)}${p.score != null ? ` <span class="small muted">${p.score}</span>` : ""}</td>
        <td>${lr ? `${chip(lr.status)}<div class="small muted">${runLink(lr.run_id, ago(lr.run_start))}</div>` : `<span class="muted">–</span>`}</td>
        <td class="num">${lr ? dur(lr.duration_ms) : "–"}<div class="small muted">usual ${dur(p.median_duration_ms)}</div></td>
        <td class="num">${lr ? n0(lr.rows_written) : "–"}<div class="small muted">usual ${n0(p.median_rows)}</div></td>
        <td class="num">${p.success_rate != null ? p.success_rate + "%" : "–"}<div class="small muted">${n0(p.runs_30d)} runs / 30 d</div></td>
        <td>${spark(p.spark)}</td>
        <td class="small">${p.triggers.length ? p.triggers.map((t) => `${esc(t.type)} ${esc(t.when)} ${t.state === "Started" ? "" : `<span class="chip grey">${esc(t.state)}</span>`}`).join("<br>") : `<span class="muted">manual / called by another pipeline</span>`}</td>
        <td class="num">${p.issues ? sevChip(p.worst) + ` <span class="small">${p.issues}</span>` : `<span class="small muted">none</span>`}</td></tr>`;
    }).join("");
    return {
      html: bar.html + `<div class="kpis">
          <div class="kpi ${tone(k.trust)}"><div class="l">Pipeline trust</div><div class="v">${k.trust ?? "–"}</div><div class="small muted">average of ${k.pipelines} pipeline(s)</div></div>
          <div class="kpi ${k.issues ? "bad" : ""}"><div class="l">Open issues</div><div class="v">${n0(k.issues)}</div><div class="small muted">failures, stale data, checks</div></div>
          <div class="kpi ${k.failed_24h ? "bad" : ""}"><div class="l">Runs, 24 h</div><div class="v">${n0(k.runs_24h)}</div><div class="small muted">${n0(k.failed_24h)} failed · ${n0(k.running)} running</div></div>
          <div class="kpi ${tone(k.success_rate_7d)}"><div class="l">Success rate, 7 d</div><div class="v">${k.success_rate_7d != null ? k.success_rate_7d + "%" : "–"}</div><div class="small muted">${n0(k.ok_7d)} of ${n0(k.runs_7d)} runs</div></div>
          <div class="kpi"><div class="l">Rows loaded, 7 d</div><div class="v">${n0(k.rows_7d)}</div><div class="small muted">${k.bytes_7d ? fmtBytes(k.bytes_7d) : "–"} written</div></div>
          <div class="kpi ${k.skipped_7d ? "warn" : ""}"><div class="l">Rows skipped, 7 d</div><div class="v">${n0(k.skipped_7d || 0)}</div><div class="small muted">not loaded (fault tolerance)</div></div></div>
        <div class="grid g-2">
          <div class="card"><h3>Runs per day</h3><div class="chartbox"><canvas id="adfDays"></canvas></div></div>
          <div class="card"><div class="draft-head"><h3 style="margin:0">Needs attention</h3><span class="grow"></span>
            ${ov.issues.length ? `<button class="btn light btn-sm" data-go="estate-adf-issues">All ${ov.issues.length} issues</button>` : ""}</div>
            ${ov.issues.slice(0, 4).map((i) => issueCard(i, true)).join("") || `<p class="ok">Nothing open. Every pipeline's newest run succeeded and passed its checks.</p>`}</div></div>
        <div class="card"><h3>Pipelines</h3><div class="tablewrap adf-table"><table><thead><tr><th>Pipeline</th><th>Health</th><th>Last run</th>
          <th class="num">Duration</th><th class="num">Rows loaded</th><th class="num">Success</th><th>Last 20 runs</th><th>Schedule</th><th class="num">Issues</th></tr></thead>
          <tbody>${pipes || `<tr><td colspan="9" class="muted">No pipelines read yet: the first sync is running or the factory has none.</td></tr>`}</tbody></table></div></div>`,
      bind() {
        bar.bind();
        const el = $e("body-estate-adf-overview");
        bindLinks(el);
        const days = [...new Set(ov.by_day.map((d) => d.day))];
        const series = [["Succeeded", "#11805A"], ["Failed", "#C42B2B"], ["Cancelled", "#94A3B8"], ["InProgress", "#EF802C"]];
        chart("adfDays", { type: "bar", data: { labels: days.map((d) => d.slice(5)), datasets: series.map(([s, c]) => ({ label: LABEL[s] || s, backgroundColor: c,
          data: days.map((d) => (ov.by_day.find((x) => x.day === d && x.status === s) || {}).n || 0) })).filter((d) => d.data.some(Boolean)) },
          options: { maintainAspectRatio: false, scales: { x: { stacked: true, grid: { display: false } }, y: { stacked: true, beginAtZero: true, ticks: { precision: 0 } } },
            plugins: { legend: { position: "bottom", labels: { boxWidth: 10 } } } } });
      },
    };
  }

  // ================================================================ ISSUES
  async function pageIssues() {
    const bar = await sourcesBar("estate-adf-issues");
    if (bar.none) return { html: bar.html + emptyState(), bind: () => bindGo($e("body-estate-adf-issues")) };
    const list = await api("/issues?" + qs({ source_id: A.src }));
    const counts = ["CRITICAL", "HIGH", "MEDIUM", "LOW"].map((s) => [s, list.filter((i) => i.severity === s).length]);
    return {
      html: bar.html + `<div class="card"><div class="draft-head"><h3 style="margin:0">${list.length} open issue${list.length === 1 ? "" : "s"}</h3>
          <span class="grow"></span>${counts.filter(([, c]) => c).map(([s, c]) => `${sevChip(s)} <span class="small">${c}</span>`).join(" ")}</div>
          <p class="small muted">Each issue closes by itself when the next run no longer shows it. Causes come from the error codes ADF reports and from DataTrust's own checks of the source files and target tables.</p>
          ${list.map((i) => issueCard(i, false)).join("") || `<p class="ok">No open issues.</p>`}</div>`,
      bind() { bar.bind(); bindLinks($e("body-estate-adf-issues")); },
    };
  }

  // ================================================================ RUNS (list / run detail / pipeline detail)
  async function pageRuns() {
    if (A.runId) return runDetail(A.runId);
    if (A.pipe) return pipelineDetail(A.pipe.sid, A.pipe.name);
    const bar = await sourcesBar("estate-adf-runs");
    if (bar.none) return { html: bar.html + emptyState(), bind: () => bindGo($e("body-estate-adf-runs")) };
    const [rows, ov] = await Promise.all([api("/runs?" + qs({ source_id: A.src, ...A.f })), api("/overview?" + qs({ source_id: A.src }))]);
    const names = [...new Set(ov.pipelines.map((p) => p.name))];
    const opt = (vals, cur, all) => `<option value="">${all}</option>` + vals.map((v) => `<option ${v === cur ? "selected" : ""} value="${esc(v)}">${esc(LABEL[v] || v)}</option>`).join("");
    const body = rows.map((r) => `<tr class="clickable" data-run="${esc(r.run_id)}">
        <td>${chip(r.status)}</td><td><b>${esc(r.pipeline_name)}</b>${r.parent_run_id ? `<div class="small muted">called by another pipeline</div>` : ""}</td>
        <td class="nowrap">${esc(when(r.run_start))}</td><td class="num">${dur(r.duration_ms)}</td>
        <td class="num">${n0(r.rows_written)}${r.rows_skipped ? `<div class="small warn">${n0(r.rows_skipped)} skipped</div>` : ""}</td>
        <td class="num">${n0(r.steps)}${r.steps_failed ? ` <span class="bad small">(${r.steps_failed} failed)</span>` : ""}</td>
        <td class="small">${esc(r.invoked_by_type || "")}${r.invoked_by_name ? `<div class="muted">${esc(r.invoked_by_name)}</div>` : ""}</td>
        <td class="small adf-msg">${r.cause ? `<b class="bad">${esc(r.cause)}</b>` : ""}<div class="adf-clamp muted">${esc(r.message || "")}</div></td></tr>`).join("");
    return {
      html: bar.html + `<div class="card"><div class="rj-filters">
          <label>Pipeline<select id="arPipe">${opt(names, A.f.pipeline, "All pipelines")}</select></label>
          <label>Status<select id="arStatus">${opt(["Succeeded", "Failed", "InProgress", "Queued", "Cancelled"], A.f.status, "Any status")}</select></label>
          <label>Period<select id="arDays">${[1, 7, 30, 45].map((d) => `<option value="${d}" ${Number(A.f.days) === d ? "selected" : ""}>Last ${d} day${d > 1 ? "s" : ""}</option>`).join("")}</select></label>
          <label class="grow">Search<input id="arQ" placeholder="run ID, error text, parameter value…" value="${esc(A.f.q)}"></label>
          <button class="btn btn-sm" id="arApply">Show</button></div>
          <div class="small muted" style="margin:6px 0">${rows.length} run${rows.length === 1 ? "" : "s"}${rows.length >= 300 ? " (newest 300; narrow the filters)" : ""} · click a run for its steps, errors and checks</div>
          <div class="tablewrap adf-table tall"><table><thead><tr><th>Status</th><th>Pipeline</th><th>Started</th><th class="num">Duration</th><th class="num">Rows loaded</th>
          <th class="num">Steps</th><th>Started by</th><th>Message</th></tr></thead>
          <tbody>${body || `<tr><td colspan="8" class="muted">No runs for these filters.</td></tr>`}</tbody></table></div></div>`,
      bind() {
        bar.bind();
        const el = $e("body-estate-adf-runs");
        bindLinks(el);
        const apply = () => { Object.assign(A.f, { pipeline: $e("arPipe").value, status: $e("arStatus").value, days: $e("arDays").value, q: $e("arQ").value.trim() }); renderEstate("estate-adf-runs"); };
        $e("arApply").onclick = apply;
        $e("arQ").onkeydown = (e) => { if (e.key === "Enter") apply(); };
        ["arPipe", "arStatus", "arDays"].forEach((id) => ($e(id).onchange = apply));
      },
    };
  }

  function backBar(label) {
    return `<div class="adf-back"><button class="btn light btn-sm" id="adfBack">← ${esc(label)}</button></div>`;
  }
  function bindBack() { const b = $e("adfBack"); if (b) b.onclick = () => { A.runId = null; A.pipe = null; renderEstate("estate-adf-runs"); }; }

  function assetHtml(a) {
    if (!a) return `<span class="muted">–</span>`;
    const title = a.kind === "storage" ? `${esc(a.system)} · ${esc(a.account || "?")}` : a.kind === "postgres" ? `${esc(a.system)} · ${esc(a.host || "server not readable")}` : esc(a.system || a.kind || "");
    const name = a.kind === "storage" ? `${esc(a.container || "?")}/${esc(a.path || "")}${a.wildcard_file ? ` <span class="muted">(${esc(a.wildcard_folder || "")}/${esc(a.wildcard_file)})</span>` : ""}`
      : a.kind === "postgres" ? `${esc(a.database || "?")}.<b>${esc(a.schema)}.${esc(a.table || "?")}</b>` : esc(a.name || "");
    return `<div class="adf-asset ${esc(a.kind)}"><div class="small muted">${title}</div><div class="mono">${name}</div>
      <div class="small muted">dataset ${esc(a.dataset || "–")}${a.write && a.write.method ? ` · write: ${esc(a.write.method)}` : ""}${a.write && a.write.pre_copy_script ? " · pre-copy script" : ""}</div>
      ${a.unresolved && a.unresolved.length ? `<div class="small warn">depends on runtime values: ${esc(a.unresolved.join(", "))}</div>` : ""}</div>`;
  }

  function lineageHtml(edges) {
    if (!edges.length) return `<p class="muted">No data movement found in the definition (no Copy activities).</p>`;
    return edges.map((e) => e.type === "Copy" ? `<div class="adf-flow">${assetHtml(e.source)}<div class="adf-arrow"><div>${esc(e.activity)}</div>${e.status ? chip(e.status) : ""}<span>→</span></div>${assetHtml(e.target)}</div>`
      : e.type === "ExecutePipeline" ? `<div class="adf-flow small"><span class="adf-arrow"><div>${esc(e.activity)}</div>${e.status ? chip(e.status) : ""}</span> runs pipeline <b>${esc(e.child_pipeline || "?")}</b></div>`
      : `<div class="adf-flow small"><span class="adf-arrow"><div>${esc(e.activity)} (${esc(e.type)})</div>${e.status ? chip(e.status) : ""}</span>${e.touches ? assetHtml(e.touches) : e.dataflow ? `data flow <b>${esc(e.dataflow)}</b>` : ""}</div>`).join("");
  }

  function stepsHtml(steps, withStatus = true) {
    if (!steps.length) return `<p class="muted">Pipeline definition not read (yet).</p>`;
    return `<ol class="adf-steps">${steps.map((s) => `<li class="${s.parent ? "nested" : ""}">${withStatus ? chip(s.status) + " " : ""}<b>${esc(s.name)}</b> <span class="small muted">${esc(s.type)}${s.runs > 1 ? ` · ${s.runs} runs${s.failed ? `, ${s.failed} failed` : ""}` : ""}${s.state === "Inactive" ? " · deactivated" : ""}</span>
      ${s.depends_on && s.depends_on.length ? `<div class="small muted">after ${s.depends_on.map((d) => `${esc(d.activity)} <i>${esc(d.conditions.join("/").toLowerCase())}</i>`).join(", ")}</div>` : ""}
      ${s.parent ? `<div class="small muted">inside ${esc(s.parent)}${s.branch && s.branch !== "activities" ? ` (${esc(s.branch)})` : ""}</div>` : ""}</li>`).join("")}</ol>`;
  }

  async function runDetail(runId) {
    const d = await api(`/runs/${encodeURIComponent(runId)}`);
    const r = d.run;
    const total = Math.max(r.duration_ms || 0, ...d.tasks.map((t) => (t.offset_ms || 0) + (t.duration_ms || 0)), 1);
    const gantt = d.tasks.map((t) => {
      const left = Math.max(0, (t.offset_ms || 0) / total * 100), width = Math.max(0.6, (t.duration_ms || 0) / total * 100);
      const q = t.queue_ms && t.duration_ms ? Math.min(100, t.queue_ms / t.duration_ms * 100) : 0;
      return `<div class="adf-g"><div class="adf-g-name" title="${esc(t.activity_type)}">${esc(t.activity_name)}<span class="small muted"> ${esc(t.activity_type)}</span></div>
        <div class="adf-g-track"><div class="adf-g-bar ${ST[t.status] || "grey"}" style="left:${left}%;width:${Math.min(width, 100 - left)}%"
          title="${esc(t.status)} · ${dur(t.duration_ms)}${t.queue_ms ? ` (queued ${dur(t.queue_ms)})` : ""}">${q ? `<i style="width:${q}%"></i>` : ""}</div></div>
        <div class="adf-g-dur small">${dur(t.duration_ms)}</div></div>`;
    }).join("");
    const stepRows = d.tasks.map((t) => `<tr><td>${chip(t.status)}</td><td><b>${esc(t.activity_name)}</b><div class="small muted">${esc(t.activity_type)}</div></td>
        <td class="num">${n0(t.rows_read)}</td><td class="num">${n0(t.rows_written)}</td><td class="num ${t.rows_skipped ? "warn" : ""}">${n0(t.rows_skipped)}</td>
        <td class="num">${t.data_read != null ? fmtBytes(t.data_read) : "–"} / ${t.data_written != null ? fmtBytes(t.data_written) : "–"}</td>
        <td class="num">${dur(t.duration_ms)}${t.queue_ms ? `<div class="small muted">queue ${dur(t.queue_ms)}</div>` : ""}</td>
        <td class="small">${t.source_type ? `${esc(t.source_type)} → ${esc(t.sink_type)}` : ""}${t.diu ? `<div class="muted">${t.diu} DIU${t.parallel ? ` · ${t.parallel} parallel` : ""}</div>` : ""}${t.throughput_kbps ? `<div class="muted">${Number(t.throughput_kbps).toFixed(1)} KB/s</div>` : ""}</td>
        <td class="small adf-msg">${t.error_message ? `<span class="bad">${esc(t.error_code || "")}</span> ${esc(t.error_message.slice(0, 140))}${t.error_message.length > 140 ? "…" : ""}` : ""}</td></tr>`).join("");
    const roots = d.root_causes.map((c) => `<div class="adf-root">
        <div class="adf-path">${c.path.map((p) => `<span>${esc(p)}</span>`).join("<b>›</b>")}</div>
        <h3>${esc(c.title)} <span class="chip grey">${esc(c.system)}</span> ${c.retry_helps ? `<span class="chip ok">re-run may fix it</span>` : `<span class="chip warn">needs a fix before re-running</span>`}</h3>
        <p>${esc(c.explanation)}</p>
        ${c.message ? `<details open><summary class="small">Error from Azure${c.error_code ? ` (code ${esc(c.error_code)}${c.failure_type ? ", " + esc(c.failure_type) : ""})` : ""}</summary><pre class="adf-err">${esc(c.message)}</pre></details>` : ""}
        ${c.details && Object.keys(c.details).length ? `<div class="small muted">Named in the error: ${Object.entries(c.details).filter(([k]) => k !== "inner_codes").map(([k, v]) => `${esc(k)} <code>${esc(v)}</code>`).join(" · ")}${c.details.inner_codes ? ` · codes <code>${esc(c.details.inner_codes.join(", "))}</code>` : ""}</div>` : ""}
        <div><b>What to do</b><ol class="adf-fixes">${c.fixes.map((f) => `<li>${esc(f)}</li>`).join("")}</ol></div>
        ${c.evidence && c.evidence.length ? `<div class="small"><b>DataTrust checked:</b> ${c.evidence.map((e) => `${chip(e.status)} ${esc(CHECK[e.check_type] || e.check_type)}: ${esc((e.detail || "").split("\n")[0])}`).join("<br>")}</div>` : ""}</div>`).join("");
    const checks = d.checks.map((c) => `<tr><td>${chip(c.status)}</td><td>${esc(c.side === "source" ? "Source" : "Target")}</td><td><b>${esc(CHECK[c.check_type] || c.check_type)}</b><div class="small muted">${esc(c.activity_name || "")}</div></td>
        <td class="small mono">${esc(c.asset || "")}</td><td class="small">${c.expected ? `expected ${esc(c.expected)}<br>` : ""}${c.observed ? `found ${esc(c.observed)}` : ""}</td>
        <td class="small adf-msg">${esc(c.detail || "").replace(/\n/g, "<br>")}<div class="muted">checked ${esc(ago(c.checked_at))}</div></td></tr>`).join("");
    const cmp = d.compare;
    const vs = (v, m, f) => (v != null && m ? `<div class="small ${v > 2 * m || v < 0.5 * m ? "warn" : "muted"}">usual ${f(m)}</div>` : "");
    const params = Object.entries(r.parameters || {});
    return {
      html: backBar("All runs") + `<div class="card adf-runhead"><div class="draft-head"><h2 style="margin:0">${esc(r.pipeline_name)}</h2> ${chip(r.status)}<span class="grow"></span>
          <button class="btn light btn-sm" data-pipe="${esc(r.pipeline_name)}" data-sid="${d.source_id}">Pipeline history</button>
          ${d.adf_link ? `<a class="btn light btn-sm" href="${esc(d.adf_link)}" target="_blank" rel="noopener">Open in ADF Monitor ↗</a>` : ""}</div>
          <div class="adf-facts">
            <div><span class="l">Started</span>${esc(when(r.run_start))}</div><div><span class="l">Duration</span>${dur(r.duration_ms)}${vs(r.duration_ms, cmp.median_duration_ms, dur)}</div>
            <div><span class="l">Rows loaded</span>${n0(r.rows_written)}${vs(r.rows_written, cmp.median_rows, n0)}</div>
            <div><span class="l">Rows read</span>${n0(r.rows_read)}</div><div><span class="l">Skipped</span><span class="${r.rows_skipped ? "warn" : ""}">${n0(r.rows_skipped || 0)}</span></div>
            <div><span class="l">Started by</span>${esc(r.invoked_by_type || "–")}<div class="small muted">${esc(r.invoked_by_name || "")}</div></div>
            <div><span class="l">Factory</span>${esc(d.factory || "")}</div><div><span class="l">Run ID</span><span class="mono small">${esc(r.run_id)}</span></div></div>
          ${d.parent ? `<div class="small">Called by ${runLink(d.parent.run_id, d.parent.pipeline_name)} ${chip(d.parent.status)}</div>` : ""}
          ${d.children.length ? `<div class="small">Runs these pipelines: ${d.children.map((c) => `${runLink(c.run_id, c.pipeline_name)} ${chip(c.status)}`).join(" ")}</div>` : ""}
          ${d.reruns.length ? `<div class="small muted">Same run group (re-runs): ${d.reruns.map((c) => `${runLink(c.run_id)} ${chip(c.status)}`).join(" ")}</div>` : ""}
          ${params.length ? `<div class="small adf-params"><b>Parameters</b> ${params.map(([k, v]) => `<code>${esc(k)} = ${esc(typeof v === "object" ? JSON.stringify(v) : v)}</code>`).join(" ")}</div>` : ""}</div>
        ${roots ? `<div class="card adf-rootcard"><h3>Why it ${r.status === "Cancelled" ? "stopped" : "failed"}</h3>${roots}</div>` : ""}
        <div class="card"><h3>Steps over time</h3>${gantt ? `<div class="adf-gantt">${gantt}</div><div class="small muted">Bars are placed on the run's timeline (0 → ${dur(total)}); the lighter part is time spent waiting in the queue.</div>` : `<p class="muted">No step details yet${["InProgress", "Queued"].includes(r.status) ? " (run still in progress)" : ""}.</p>`}</div>
        <div class="card"><h3>Steps: rows, data and errors</h3><div class="tablewrap adf-table"><table><thead><tr><th>Status</th><th>Step</th><th class="num">Rows read</th><th class="num">Rows written</th>
          <th class="num">Skipped</th><th class="num">Data read / written</th><th class="num">Duration</th><th>Engine</th><th>Error</th></tr></thead><tbody>${stepRows}</tbody></table></div></div>
        <div class="card"><h3>Source and target checks</h3><p class="small muted">DataTrust looks at the files and tables itself; it does not rely only on what ADF reports.</p>
          ${checks ? `<div class="tablewrap adf-table"><table><thead><tr><th>Result</th><th>Side</th><th>Check</th><th>Asset</th><th>Values</th><th>Details</th></tr></thead><tbody>${checks}</tbody></table></div>`
            : `<p class="muted">No checks for this run (only Copy steps of each pipeline's newest runs are checked).</p>`}</div>
        <div class="card"><h3>Data moved in this run</h3>${lineageHtml(d.lineage)}</div>
        <div class="card"><h3>Pipeline steps and order</h3>${stepsHtml(d.steps)}</div>`,
      bind() { bindBack(); bindLinks($e("body-estate-adf-runs")); },
    };
  }

  async function pipelineDetail(sid, name) {
    const p = await api(`/pipelines/${sid}/${encodeURIComponent(name)}`);
    const finished = p.runs.filter((r) => ["Succeeded", "Failed", "Cancelled"].includes(r.status)).slice(0, 40).reverse();
    const params = Object.entries(p.parameters || {});
    return {
      html: backBar("All runs") + `<div class="card adf-runhead"><div class="draft-head"><h2 style="margin:0">${esc(p.name)}</h2> ${healthChip(p.health)}
          ${p.score != null ? `<span class="small muted">trust ${p.score}/100</span>` : ""}<span class="grow"></span>
          ${p.adf_link ? `<a class="btn light btn-sm" href="${esc(p.adf_link)}" target="_blank" rel="noopener">ADF Monitor ↗</a>` : ""}</div>
          ${p.description ? `<p>${esc(p.description)}</p>` : ""}
          <div class="small muted">${esc(p.factory)}${p.folder ? " · folder " + esc(p.folder) : ""}${p.removed ? " · <span class='warn'>no longer in the factory</span>" : ""}</div>
          <div class="small">${p.triggers.length ? p.triggers.map((t) => `<b>${esc(t.name)}</b>: ${esc(t.type)} ${esc(t.when)} <span class="chip ${t.state === "Started" ? "ok" : "grey"}">${esc(t.state || "")}</span>`).join(" · ") : "No trigger: started manually or by another pipeline."}</div>
          ${params.length ? `<div class="small adf-params"><b>Parameters</b> ${params.map(([k, v]) => `<code>${esc(k)}: ${esc(v.type || "")}${v.defaultValue != null ? " = " + esc(typeof v.defaultValue === "object" ? JSON.stringify(v.defaultValue) : v.defaultValue) : ""}</code>`).join(" ")}</div>` : ""}</div>
        ${p.issues.length ? `<div class="card"><h3>Open issues</h3>${p.issues.map((i) => issueCard(i, false)).join("")}</div>` : ""}
        <div class="grid g-2"><div class="card"><h3>Duration per run</h3><div class="chartbox"><canvas id="adfDur"></canvas></div></div>
          <div class="card"><h3>Rows loaded per run</h3><div class="chartbox"><canvas id="adfRows"></canvas></div></div></div>
        <div class="card"><h3>Data it moves</h3><p class="small muted">With the parameters' default values; each run page shows the exact paths of that run.</p>${lineageHtml(p.lineage)}</div>
        <div class="card"><h3>Steps and order</h3>${stepsHtml(p.activities, false)}</div>
        <div class="card"><h3>Recent runs</h3><div class="tablewrap adf-table"><table><thead><tr><th>Status</th><th>Started</th><th class="num">Duration</th><th class="num">Rows</th><th>Message</th></tr></thead>
          <tbody>${p.runs.map((r) => `<tr class="clickable" data-run="${esc(r.run_id)}"><td>${chip(r.status)}</td><td class="nowrap">${esc(when(r.run_start))}</td>
            <td class="num">${dur(r.duration_ms)}</td><td class="num">${n0(r.rows_written)}</td><td class="small adf-msg">${esc((r.message || "").replace(/^Operation on target .*? failed: /, "").slice(0, 200))}</td></tr>`).join("") || `<tr><td colspan="5" class="muted">No runs in the history yet.</td></tr>`}</tbody></table></div></div>`,
      bind() {
        bindBack();
        bindLinks($e("body-estate-adf-runs"));
        const labels = finished.map((r) => when(r.run_start));
        const colors = finished.map((r) => (r.status === "Succeeded" ? "#11805A" : r.status === "Failed" ? "#C42B2B" : "#94A3B8"));
        const opts = { maintainAspectRatio: false, plugins: { legend: { display: false } }, scales: { x: { ticks: { display: false }, grid: { display: false } }, y: { beginAtZero: true } },
          onClick: (e, els) => { if (els.length) openRun(finished[els[0].index].run_id); } };
        chart("adfDur", { type: "bar", data: { labels, datasets: [{ label: "minutes", data: finished.map((r) => (r.duration_ms || 0) / 60000), backgroundColor: colors }] },
          options: { ...opts, plugins: { legend: { display: false }, tooltip: { callbacks: { label: (c) => dur(finished[c.dataIndex].duration_ms) + " · " + finished[c.dataIndex].status } } } } });
        chart("adfRows", { type: "line", data: { labels, datasets: [{ label: "rows", data: finished.map((r) => r.rows_written), borderColor: "#0672A1", backgroundColor: "rgba(6,114,161,.1)", fill: true, tension: .25, pointBackgroundColor: colors }] }, options: opts });
      },
    };
  }

  // ================================================================ CONNECT / SETUP
  async function pageSetup() {
    const srcs = await api("/sources");
    A.sources = srcs;
    const admin = isAdmin();
    const conns = admin ? await eapi("/connections").catch(() => []) : [];
    const list = await Promise.all(srcs.map(async (s) => {
      const log = await api(`/sources/${s.source_id}/sync-log`).catch(() => []);
      const c = s.config || {};
      return `<div class="card"><div class="draft-head"><h3 style="margin:0">${esc(s.name)}</h3>
          ${!s.enabled ? `<span class="chip grey">paused</span>` : s.last_sync_status === "FAILED" ? `<span class="chip bad">sync failing</span>` : s.last_success_at ? `<span class="chip ok">connected</span>` : `<span class="chip warn">first sync pending</span>`}
          <span class="grow"></span><button class="btn light btn-sm" data-sync="${s.source_id}">↻ Sync now</button>
          ${admin ? `<button class="btn light btn-sm" data-edit="${s.source_id}">Edit</button><button class="btn light btn-sm" data-hook="${s.source_id}">Instant updates…</button>
          <button class="btn light btn-sm" data-del="${s.source_id}">Remove</button>` : ""}</div>
        <div class="small muted">${esc(c.factory_name || "")} · resource group ${esc(c.resource_group || "")} · ${c.auth_mode === "service_principal" ? `service principal ${esc(c.client_id || "")}` : "default Azure credential"}
          · history ${esc(c.lookback_days || 30)} d · ${n0(s.pipelines)} pipelines · ${n0(s.runs)} runs stored</div>
        <div class="small">Last sync ${esc(ago(s.last_sync_finished))}${s.last_success_at ? ` · last success ${esc(ago(s.last_success_at))}` : ""}${s.has_webhook ? " · instant updates on" : ""}</div>
        ${s.last_sync_error ? `<div class="${s.last_sync_status === "FAILED" ? "error-box" : "banner warn"} small">${esc(s.last_sync_error)}</div>` : ""}
        <div id="adfHook_${s.source_id}"></div>
        <details><summary class="small">Sync history</summary><div class="tablewrap adf-table"><table><thead><tr><th>Started</th><th>By</th><th>Result</th><th class="num">Runs</th><th class="num">Steps</th><th class="num">Checks</th><th>Error</th></tr></thead><tbody>
          ${log.map((l) => `<tr><td class="nowrap small">${esc(when(l.started_at))}</td><td class="small">${esc(l.started_by)}</td><td>${chip(l.status === "SUCCEEDED" ? "Succeeded" : l.status === "FAILED" ? "Failed" : l.status === "RUNNING" ? "InProgress" : "WARN")}</td>
            <td class="num">${n0(l.pipeline_runs)}</td><td class="num">${n0(l.task_runs)}</td><td class="num">${n0(l.checks)}</td><td class="small adf-msg">${esc(l.error || "")}</td></tr>`).join("")}</tbody></table></div></details></div>`;
    }));
    const e = A.editing ? (srcs.find((s) => s.source_id === A.editing) || {}) : {};
    const c = e.config || { auth_mode: "service_principal", lookback_days: 30, client_secret_env: "ADF_CLIENT_SECRET" };
    const field = (id, label, val, ph, help) => `<label class="field"><span>${label}</span><input id="${id}" value="${esc(val ?? "")}" placeholder="${esc(ph || "")}" autocomplete="off">${help ? `<span class="small muted">${help}</span>` : ""}</label>`;
    const form = admin ? `<div class="card" id="adfForm"><h3>${A.editing ? `Edit ${esc(e.name)}` : "Connect a Data Factory"}</h3>
        <p class="small muted">DataTrust only reads. It never changes pipelines, triggers or data. Secrets are not stored here: you enter the <b>name</b> of an environment variable (.env locally, an App Service setting / Key Vault reference in Azure).</p>
        <div class="form-grid adf-form">
          ${field("afName", "Display name", e.name, "Sales ADF (prod)")}
          ${field("afSub", "Subscription ID", c.subscription_id, "00000000-0000-0000-0000-000000000000", "Azure portal → your Data Factory → Overview")}
          ${field("afRg", "Resource group", c.resource_group, "rg-data-prod")}
          ${field("afFac", "Data Factory name", c.factory_name, "adf-sales-prod")}
          <label class="field"><span>Sign in to Azure as</span><select id="afAuth">
            <option value="service_principal" ${c.auth_mode === "service_principal" ? "selected" : ""}>App registration (service principal)</option>
            <option value="default" ${c.auth_mode === "default" ? "selected" : ""}>Managed identity / az login (default credential)</option></select></label>
          <span></span>
          <div class="sp ${c.auth_mode === "service_principal" ? "" : "hidden"}">${field("afTenant", "Tenant ID", c.tenant_id, "Directory (tenant) ID")}</div>
          <div class="sp ${c.auth_mode === "service_principal" ? "" : "hidden"}">${field("afClient", "Client ID", c.client_id, "Application (client) ID")}</div>
          <div class="sp ${c.auth_mode === "service_principal" ? "" : "hidden"}">${field("afSecret", "Client secret variable", c.client_secret_env, "ADF_CLIENT_SECRET", "name of the variable that holds the secret")}</div>
          ${field("afStorage", "Storage connection variable (optional)", c.storage_connection_env, "e.g. ADF_STORAGE_CONN", "only if the storage checks should not use the identity above")}
          <label class="field"><span>Target database for checks (optional)</span><select id="afTarget"><option value="">Match by host name automatically</option>
            ${conns.filter((x) => x.source_type === "postgresql").map((x) => `<option value="${x.connection_id}" ${c.target_connection_id === x.connection_id ? "selected" : ""}>${esc(x.name)} (${esc(x.host)})</option>`).join("")}</select>
            <span class="small muted">Data estate connection used to check the target tables</span></label>
          ${field("afDays", "History to read (days)", c.lookback_days || 30, "30", "ADF keeps 45 days")}
          ${field("afFresh", "Max hours between successful runs (optional)", c.freshness_hours || "", "e.g. 24", "for pipelines without a schedule trigger")}
        </div>
        <div class="pacts"><button class="btn light" id="afTest">Test connection</button> <button class="btn" id="afSave">${A.editing ? "Save changes" : "Connect"}</button>
          ${A.editing ? `<label class="small"><input type="checkbox" id="afEnabled" ${e.enabled ? "checked" : ""}> sync enabled</label> <button class="btn light" id="afCancel">Cancel</button>` : ""}</div>
        <div id="afResult"></div></div>` : `<div class="card"><p class="muted">Only an admin can connect or change a Data Factory.</p></div>`;
    const guide = `<div class="card"><h3>What DataTrust needs in Azure</h3><ol class="small adf-guide">
        <li><b>An identity.</b> Create an app registration (Entra ID → App registrations → New) and a client secret, or run DataTrust with a managed identity.
          Put the secret in <code>.env</code> as <code>ADF_CLIENT_SECRET=…</code> (in Azure: an app setting / Key Vault reference).</li>
        <li><b>Read access to the factory.</b> Data Factory → Access control (IAM) → Add role assignment → <b>Data Factory Contributor</b> for that identity
          (or the read-only custom role in <code>docs/ADF_CONNECTOR.md</code>).<br><code>az role assignment create --assignee &lt;client-id&gt; --role "Data Factory Contributor" --scope &lt;factory resource ID&gt;</code></li>
        <li><b>Read access to the source storage</b> (for file and row checks): role <b>Storage Blob Data Reader</b> on the storage account.</li>
        <li><b>Read access to the target PostgreSQL</b> (for table and row checks): add it under Data estate → Connections with a read-only login, using the same host name as the ADF linked service.</li>
        <li>Optional: <b>instant updates</b>. Add a Web activity at the end of each pipeline that calls DataTrust (button "Instant updates…" above), so new runs show up in seconds instead of at the next automatic sync.</li></ol></div>`;
    return {
      html: list.join("") + form + guide,
      bind() {
        const el = $e("body-estate-adf-setup");
        el.querySelectorAll("[data-sync]").forEach((b) => (b.onclick = async () => { b.disabled = true; try { await post(`/adf/sources/${b.dataset.sync}/sync`); toast("Sync started"); setTimeout(() => renderEstate("estate-adf-setup"), 4000); } catch (x) { toast(x.message); } }));
        el.querySelectorAll("[data-edit]").forEach((b) => (b.onclick = () => { A.editing = Number(b.dataset.edit); renderEstate("estate-adf-setup").then(() => $e("adfForm").scrollIntoView({ behavior: "smooth" })); }));
        el.querySelectorAll("[data-del]").forEach((b) => (b.onclick = () => {
          b.outerHTML = `<span class="small">Remove it and all its collected history? <button class="btn btn-sm" id="adfDelYes">Remove</button> <button class="btn light btn-sm" id="adfDelNo">Keep</button></span>`;
          $e("adfDelNo").onclick = () => renderEstate("estate-adf-setup");
          $e("adfDelYes").onclick = async () => { await eapi(`/adf/sources/${b.dataset.del}`, { method: "DELETE" }); toast("Removed"); A.editing = null; renderEstate("estate-adf-setup"); };
        }));
        el.querySelectorAll("[data-hook]").forEach((b) => (b.onclick = async () => {
          const box = $e("adfHook_" + b.dataset.hook);
          box.innerHTML = `<div class="banner warn small">A new token replaces the old one (pipelines using the old one stop notifying). <button class="btn btn-sm" id="hookYes">Create token</button> <button class="btn light btn-sm" id="hookNo">Cancel</button></div>`;
          $e("hookNo").onclick = () => (box.innerHTML = "");
          $e("hookYes").onclick = async () => {
            const t = await post(`/adf/sources/${b.dataset.hook}/webhook-token`);
            const activity = { name: "Notify DataTrust", type: "WebActivity", dependsOn: [], policy: { timeout: "0.00:01:00", retry: 2, secureInput: true },
              typeProperties: { method: "POST", url: t.url, headers: { "Content-Type": "application/json", "X-DataTrust-Token": t.token },
                body: { value: "{\"pipeline\":\"@{pipeline().Pipeline}\",\"runId\":\"@{pipeline().RunId}\"}", type: "Expression" } } };
            box.innerHTML = `<div class="draft small"><b>Shown once - copy it now.</b> Add this Web activity as the last step of each pipeline, connected with
              <i>Completion</i> (so it runs after success and failure). Keep the token secret: put it in Key Vault for production.
              The site must be reachable from Azure (${esc(t.url)}).<pre class="adf-err">${esc(JSON.stringify(activity, null, 2))}</pre></div>`;
          };
        }));
        if (!admin) return;
        $e("afAuth").onchange = () => el.querySelectorAll(".adf-form .sp").forEach((x) => x.classList.toggle("hidden", $e("afAuth").value !== "service_principal"));
        const body = () => ({ name: $e("afName").value.trim(), enabled: $e("afEnabled") ? $e("afEnabled").checked : true, config: {
          subscription_id: $e("afSub").value.trim(), resource_group: $e("afRg").value.trim(), factory_name: $e("afFac").value.trim(),
          auth_mode: $e("afAuth").value, tenant_id: $e("afTenant").value.trim(), client_id: $e("afClient").value.trim(),
          client_secret_env: $e("afSecret").value.trim(), storage_connection_env: $e("afStorage").value.trim(),
          target_connection_id: $e("afTarget").value || null, lookback_days: Number($e("afDays").value) || 30,
          freshness_hours: Number($e("afFresh").value) || null } });
        const out = $e("afResult");
        $e("afTest").onclick = async () => {
          out.innerHTML = "<p class='muted'>Testing… (sign-in, factory, pipelines, run history)</p>";
          try {
            const r = await post("/adf/test", body());
            out.innerHTML = `<div class="${r.ok ? "draft" : "error-box"}">${r.steps.map((s) => `<div>${s.ok ? "✔" : "✖"} <b>${esc(s.step)}</b>: ${esc(s.detail)}</div>`).join("")}</div>`;
          } catch (x) { out.innerHTML = errBox(x); }
        };
        $e("afSave").onclick = async () => {
          try {
            if (A.editing) await eapi(`/adf/sources/${A.editing}`, { method: "PUT", body: JSON.stringify(body()) });
            else await post("/adf/sources", body());
            toast(A.editing ? "Saved" : "Connected: first sync started");
            A.editing = null;
            renderEstate("estate-adf-setup");
          } catch (x) { out.innerHTML = errBox(x); }
        };
        if ($e("afCancel")) $e("afCancel").onclick = () => { A.editing = null; renderEstate("estate-adf-setup"); };
      },
    };
  }

  // ------------------------------------------------------------------ auto refresh: re-draw when something new was collected
  setInterval(async () => {
    if (document.hidden || !est.user || !state.page.startsWith("estate-adf-") || state.page === "estate-adf-setup") return;
    if (document.activeElement && ["INPUT", "SELECT", "TEXTAREA"].includes(document.activeElement.tagName)) return;
    try {
      const v = await api("/version");
      if (A.sig && v.signature !== A.sig) renderEstate(state.page);
      A.sig = v.signature;
    } catch (e) { /* not signed in / offline: next tick */ }
  }, 15000);

  window.ADF = {
    page: (page) => ({ "estate-adf-overview": pageOverview, "estate-adf-issues": pageIssues, "estate-adf-runs": pageRuns, "estate-adf-setup": pageSetup })[page](),
    navigated(page) {                                   // a sidebar click shows the list; links from issues / overview keep the detail
      if (!A.keep && page === "estate-adf-runs") { A.runId = null; A.pipe = null; }
      if (page !== "estate-adf-setup") A.editing = null;
      A.keep = false;
    },
    open(arg) {                                         // "<run id>" or "pipeline:<factory id>:<pipeline name>"
      const m = String(arg).match(/^pipeline:(\d+):(.+)$/);
      if (m) { A.pipe = { sid: Number(m[1]), name: m[2] }; A.runId = null; } else { A.runId = arg; A.pipe = null; }
      A.keep = true;
    },
  };
})();
