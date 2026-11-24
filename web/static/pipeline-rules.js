// Pipeline rules (checks/pipeline_rules.py): your own conditions on a Data Factory pipeline or one of its steps, checked
// against the run history DataTrust already reads. The add / edit form, opened from DQ Rule Studio, a pipeline's page or
// a rule's Edit button (checks.js hands over). Three ways in: suggestions measured from the pipeline's own runs, a rule
// described in words (AI when a model is configured, else common phrasings), or the form. Every way previews the rule
// on today's data and replays it over the last 14 days before anything is saved.
// Uses app.js (esc, num, toast, state) and estate.js (eapi, $e, renderEstate).
(function () {
  const api = (p, o) => eapi("/checks" + p, o);
  const send = (p, method, data) => api(p, { method, body: JSON.stringify(data || {}) });
  const OFFSET = -new Date().getTimezoneOffset();                     // the viewer's time zone, e.g. 330 = UTC+05:30
  const tzLabel = (m) => `UTC${m >= 0 ? "+" : "-"}${String(Math.floor(Math.abs(m) / 60)).padStart(2, "0")}:${String(Math.abs(m) % 60).padStart(2, "0")}`;
  const DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
  const KINDS = {
    finishes_by: ["Finishes on time", "A successful run ends by a time of day, on the days you choose."],
    success_rate: ["Success rate", "At least a share of runs succeed over a period."],
    last_run: ["Latest run succeeds", "The most recent finished run did not fail or get cancelled."],
    rows: ["Rows moved", "The latest successful run copied a normal number of rows."],
    duration: ["Run time", "The latest run took no longer than a limit."],
    not_stuck: ["Not stuck", "No run stays in progress longer than a limit."],
  };
  const SEVS = [["CRITICAL", "Critical: at most 74"], ["HIGH", "High: at most 89"], ["MEDIUM", "Medium"], ["LOW", "Low"]];
  const SCHEDULES = [[15, "every 15 min"], [60, "hourly"], [360, "every 6 h"], [1440, "daily"]];
  const ST = { PASS: "ok", WARN: "warn", FAIL: "bad", ERROR: "bad" }, LABEL = { PASS: "Pass", WARN: "Warning", FAIL: "Fail", ERROR: "Error" };
  const chip = (s) => `<span class="chip ${ST[s] || "grey"}">${esc(LABEL[s] || s || "–")}</span>`;
  const P = { f: null, editing: null, opts: {}, suggest: null, words: null, preview: null, busy: false };

  const factories = () => (state.scopes || []).filter((s) => s.kind === "adf");
  const pickedFactory = () => { const m = /^adf:(\d+)$/.exec(state.scope || ""); return m ? Number(m[1]) : (factories()[0] || {}).id; };
  const blank = (prefill = {}) => ({ name: "", check_type: "pipeline", severity: "HIGH", schedule_minutes: 15, enabled: true, description: "",
    config: { source_id: pickedFactory(), pipeline: "", activity: null, kind: "finishes_by", time: "09:00", days: [0, 1, 2, 3, 4],
      utc_offset_min: OFFSET, ...prefill } });

  // the rule in words (the same sentence the server writes, so the form shows what will be saved)
  function describe(c) {
    const what = `${c.pipeline || "the pipeline"}${c.activity ? " step " + c.activity : ""}`;
    const days = (d) => (d || []).length === 7 ? "every day" : JSON.stringify(d) === "[0,1,2,3,4]" ? "Mon-Fri" : (d || []).map((x) => DAYS[x]).join(", ");
    switch (c.kind) {
      case "finishes_by": return `${what} succeeds by ${c.time} (${tzLabel(c.utc_offset_min || 0)}) on ${days(c.days)}`;
      case "success_rate": return `at least ${c.min_pct ?? 95}% of ${what}'s runs succeed over ${c.days ?? 7} days`;
      case "last_run": return `the latest run of ${what} succeeds`;
      case "rows": return c.mode === "range"
        ? `${c.metric === "rows_read" ? "rows read" : "rows written"} by ${what}'s latest successful run: ${[c.min != null && c.min !== "" ? "at least " + num(c.min) : "", c.max != null && c.max !== "" ? "at most " + num(c.max) : ""].filter(Boolean).join(" and ")}`
        : `rows written by ${what}'s latest successful run within ±${c.pct ?? 30}% of the average of the previous ${c.days ?? 14} days`;
      case "duration": return `${what} takes at most ${c.max_minutes ?? 60} min`;
      case "not_stuck": return `no run of ${what} stays in progress longer than ${c.max_minutes ?? 60} min`;
    }
    return what;
  }

  async function options(sid) {
    if (!P.opts[sid]) P.opts[sid] = await api(`/pipeline/options?source_id=${sid}`).catch((e) => ({ error: e.message, pipelines: [] }));
    return P.opts[sid];
  }

  // ------------------------------------------------------------------ result + 14-day replay
  function resultHtml(r) {
    if (!r) return "";
    const b = (r.details || {}).backtest;
    const runs = ((r.details || {}).rows || []).slice(0, 5);
    return `<div class="ck-result ck-${ST[r.status] || "grey"}"><div class="ck-result-head">${chip(r.status)} <b>${esc(r.message || "")}</b></div>
      ${r.expected ? `<div class="small muted">Expected: ${esc(r.expected)}</div>` : ""}
      ${b ? `<div class="pr-replay"><span class="small">Last ${b.days} days, checked at the end of each day:</span>
          <span class="pr-days">${b.by_day.map((d) => `<i class="${ST[d.status] || "grey"}" title="${esc(d.day)}: ${esc(LABEL[d.status] || d.status)}"></i>`).join("")}</span>
          <b class="small ${b.fail ? "bad-text" : ""}">${b.fail ? `would have failed on ${b.fail} day${b.fail === 1 ? "" : "s"}` : "would not have failed"}${b.warn ? `, ${b.warn} warning${b.warn === 1 ? "" : "s"}` : ""}</b></div>` : ""}
      ${runs.length ? `<details class="small"><summary>Runs it looked at</summary><div class="tablewrap"><table><thead><tr><th>Run</th><th>Status</th><th>Started</th><th class="num">Minutes</th><th class="num">Rows written</th></tr></thead><tbody>
        ${runs.map((x) => `<tr><td><a href="#estate-adf-runs=${encodeURIComponent(x.run_id)}">${esc(String(x.run_id).slice(0, 8))}…</a></td><td>${esc(x.status)}</td><td class="nowrap">${esc(x.started || "")}</td>
          <td class="num">${x.minutes ?? "–"}</td><td class="num">${x.rows_written != null ? num(x.rows_written) : "–"}</td></tr>`).join("")}</tbody></table></div></details>` : ""}</div>`;
  }

  // ------------------------------------------------------------------ the kind's own settings
  function settings(c) {
    const n = (id, v, label, attrs = "") => `<label>${label}<input id="${id}" type="number" value="${v ?? ""}" ${attrs}></label>`;
    switch (c.kind) {
      case "finishes_by": return `<div class="pr-row"><label>Succeeds by <input id="prTime" type="time" value="${esc(c.time || "09:00")}"></label>
          <span class="small muted">your time zone, ${tzLabel(c.utc_offset_min ?? OFFSET)}</span></div>
        <fieldset class="pr-days-pick"><legend class="small">On these days</legend>${DAYS.map((d, i) => `<label><input type="checkbox" data-day="${i}" ${(c.days || []).includes(i) ? "checked" : ""}> ${d}</label>`).join("")}</fieldset>`;
      case "success_rate": return `<div class="pr-row">${n("prPct", c.min_pct ?? 95, "At least (%)", 'min="1" max="100"')}${n("prDays", c.days ?? 7, "Over (days)", 'min="1" max="90"')}</div>`;
      case "last_run": return `<p class="small muted">No settings: fails as soon as the latest finished run did not succeed.</p>`;
      case "rows": return `<div class="pr-row"><label>Count <select id="prMetric"><option value="rows_written">rows written</option><option value="rows_read" ${c.metric === "rows_read" ? "selected" : ""}>rows read</option></select></label>
          <label>Compared with <select id="prMode"><option value="average">the recent average</option><option value="range" ${c.mode === "range" ? "selected" : ""}>fixed limits</option></select></label></div>
        ${c.mode === "range" ? `<div class="pr-row">${n("prMin", c.min, "At least")}${n("prMax", c.max, "At most")}</div>`
          : `<div class="pr-row">${n("prPct", c.pct ?? 30, "Within ± (%)", 'min="1"')}${n("prDays", c.days ?? 14, "Average of (days)", 'min="1" max="90"')}</div>`}`;
      case "duration": case "not_stuck": return `<div class="pr-row">${n("prMax", c.max_minutes ?? (c.kind === "duration" ? 30 : 120), "At most (minutes)", 'min="1"')}</div>`;
    }
    return "";
  }

  function read() {
    const f = P.f, c = f.config, v = (id) => ($e(id) ? $e(id).value : undefined);
    f.name = v("prName") ?? f.name;
    f.severity = v("prSev") || f.severity;
    f.schedule_minutes = Number(v("prSched") || f.schedule_minutes);
    f.description = v("prDesc") ?? f.description;
    c.source_id = Number(v("prFactory") || c.source_id);
    c.pipeline = v("prPipe") ?? c.pipeline;
    c.activity = v("prStep") || null;
    if (c.kind === "finishes_by") {
      c.time = v("prTime") || c.time;
      c.days = [...document.querySelectorAll("[data-day]")].filter((x) => x.checked).map((x) => Number(x.dataset.day));
      c.utc_offset_min = c.utc_offset_min ?? OFFSET;
    }
    if (c.kind === "success_rate") { c.min_pct = Number(v("prPct")); c.days = Number(v("prDays")); }
    if (c.kind === "rows") {
      c.metric = v("prMetric") || "rows_written"; c.mode = v("prMode") || "average";
      if (c.mode === "range") { c.min = v("prMin") === "" ? null : Number(v("prMin")); c.max = v("prMax") === "" ? null : Number(v("prMax")); }
      else { c.pct = Number(v("prPct")); c.days = Number(v("prDays")); }
    }
    if (c.kind === "duration" || c.kind === "not_stuck") c.max_minutes = Number(v("prMax"));
    return f;
  }
  const body = () => { const f = read(); return { ...f, name: (f.name || "").trim() || describe(f.config).slice(0, 100) }; };

  // ------------------------------------------------------------------ the page
  async function page(handover = {}) {
    if (!(est.user && est.user.role === "admin")) return { html: `<div class="card"><p class="muted">Only an admin can add or change rules.</p></div>`, bind() {} };
    if (!handover.f && P.editing) reset();                           // "Add rule" after an edit: start a new one
    if (handover.f && (!P.f || handover.editing !== P.editing)) { P.f = JSON.parse(JSON.stringify(handover.f)); P.editing = handover.editing; P.preview = null; P.suggest = null; P.words = null; }
    if (!P.f) { P.f = blank(P.prefill || {}); P.editing = null; P.prefill = null; }
    const f = P.f, c = f.config;
    if (!factories().length) return { html: `<div class="card"><p class="muted">Connect a Data Factory first (Connect ADF); pipeline rules check its runs.</p></div>`, bind() {} };
    const o = await options(c.source_id);
    const pipe = (o.pipelines || []).find((p) => p.name === c.pipeline);
    const html = `<div class="draft-head"><button class="btn light btn-sm" id="prBack">← DQ Rule Studio</button></div>
      <div class="card pr-card"><h2 style="margin:0 0 4px">${P.editing ? "Edit pipeline rule" : "New pipeline rule"}</h2>
        <p class="muted small" style="margin:0 0 12px">Checked against the runs DataTrust reads from the factory, every time it reads them. A failing rule opens an incident and counts in the factory's trust score.</p>
        <div class="pr-row">
          <label>Data factory <select id="prFactory">${factories().map((s) => `<option value="${s.id}" ${s.id === c.source_id ? "selected" : ""}>${esc(s.name)}</option>`).join("")}</select></label>
          <label>Pipeline <select id="prPipe"><option value="">Choose…</option>${(o.pipelines || []).map((p) => `<option ${p.name === c.pipeline ? "selected" : ""}>${esc(p.name)}</option>`).join("")}</select></label>
          <label>Step <select id="prStep"><option value="">The whole pipeline</option>${(pipe ? pipe.steps : []).map((s) => `<option value="${esc(s.name)}" ${s.name === c.activity ? "selected" : ""}>${esc(s.name)} (${esc(s.type)})</option>`).join("")}</select></label>
        </div>
        ${o.error ? `<div class="error-box">${esc(o.error)}</div>` : ""}
        ${c.pipeline && !P.editing ? `<div class="pr-ways">
          <div class="pr-way"><h4>Describe it in words</h4><div class="pr-row"><input id="prWords" placeholder="e.g. succeeds by 9 am on weekdays" value="${esc(P.wordsText || "")}">
            <button class="btn light btn-sm" id="prWordsGo" type="button">Turn into a rule</button></div><div id="prWordsOut">${wordsHtml()}</div></div>
          <div class="pr-way"><h4>Suggested from its history</h4><button class="btn light btn-sm" id="prSuggest" type="button">Suggest rules</button>
            <div id="prSuggestOut">${suggestHtml()}</div></div></div>` : ""}
        <h4 class="pr-h">What to check</h4>
        <div class="pr-kinds" role="radiogroup" aria-label="What to check">${Object.entries(KINDS).map(([k, [l, d]]) => `<label class="pr-kind ${c.kind === k ? "on" : ""}">
          <input type="radio" name="prKind" value="${k}" ${c.kind === k ? "checked" : ""}><b>${l}</b><span class="small muted">${d}</span></label>`).join("")}</div>
        <div id="prSettings" class="pr-settings">${settings(c)}</div>
        <p class="pr-sentence"><span class="small muted">The rule:</span> <b id="prSentence">${esc(describe(c))}</b></p>
        <div class="pr-row">
          <label class="grow">Name <input id="prName" value="${esc(f.name || "")}" placeholder="${esc(describe(c).slice(0, 100))}"></label>
          <label>If it fails <select id="prSev">${SEVS.map(([v, l]) => `<option value="${v}" ${f.severity === v ? "selected" : ""}>${l}</option>`).join("")}</select></label>
          <label>Check <select id="prSched">${SCHEDULES.map(([v, l]) => `<option value="${v}" ${f.schedule_minutes === v ? "selected" : ""}>${l}</option>`).join("")}</select></label>
        </div>
        <label class="pr-desc">Why it matters <span class="small muted">(optional, shown on the incident)</span><input id="prDesc" value="${esc(f.description || "")}" placeholder="e.g. the 09:30 sales report reads what this pipeline loads"></label>
        <div class="pr-actions"><button class="btn light" id="prPreview" type="button">Preview on past runs</button>
          <button class="btn" id="prSave" type="button">${P.editing ? "Save changes" : "Save rule"}</button></div>
        <div id="prResult">${resultHtml(P.preview)}</div></div>`;
    return { html, bind };
  }

  function wordsHtml() {
    const w = P.words;
    if (!w) return "";
    if (!w.ok) return `<p class="small bad-text">${esc(w.reason)}</p>${w.note ? `<p class="small muted">${esc(w.note)}</p>` : ""}`;
    return `<div class="pr-understood"><span class="small muted">Understood${w.source && w.source.startsWith("ai") ? " by AI" : ""}:</span> <b>${esc(w.understood || w.rule)}</b>
      ${(w.assumptions || []).length ? `<ul class="small muted">${w.assumptions.map((a) => `<li>${esc(a)}</li>`).join("")}</ul>` : ""}
      <p class="small muted">The form below now holds this rule; check it, then save.</p></div>`;
  }
  function suggestHtml() {
    if (!P.suggest) return "";
    if (P.suggest.error) return `<p class="small bad-text">${esc(P.suggest.error)}</p>`;
    if (!P.suggest.length) return `<p class="small muted">Not enough runs in the last 30 days to suggest anything.</p>`;
    return `<ul class="pr-sugg">${P.suggest.map((s, i) => `<li><div><b>${esc(s.rule)}</b><div class="small muted">${esc(s.why)}</div>
        <div class="small">${chip(s.preview.status)} now · ${s.preview.details && s.preview.details.backtest ? `${s.preview.details.backtest.fail} of 14 days would have failed` : ""}</div></div>
        <button class="btn light btn-sm" data-use="${i}" type="button">Use</button></li>`).join("")}</ul>`;
  }

  function bind() {
    const rerender = () => renderEstate("estate-checks-new");
    $e("prBack").onclick = () => { reset(); document.querySelector('#tabs [data-page="estate-dq-rules"]').click(); };
    $e("prFactory").onchange = () => { read(); Object.assign(P.f.config, { pipeline: "", activity: null }); P.suggest = null; P.words = null; P.preview = null; rerender(); };
    $e("prPipe").onchange = () => { read(); P.f.config.activity = null; P.suggest = null; P.words = null; P.preview = null; rerender(); };
    $e("prStep").onchange = () => { read(); P.suggest = null; P.preview = null; rerender(); };
    document.querySelectorAll('input[name="prKind"]').forEach((r) => (r.onchange = () => { read(); P.f.config.kind = r.value; P.preview = null; rerender(); }));
    const live = () => { try { $e("prSentence").textContent = describe(read().config); } catch (e) {} };
    $e("prSettings").addEventListener("input", live);
    $e("prSettings").addEventListener("change", (e) => { if (e.target.id === "prMode") { read(); rerender(); } else live(); });
    if ($e("prWordsGo")) $e("prWordsGo").onclick = async () => {
      read(); P.wordsText = $e("prWords").value;
      $e("prWordsGo").disabled = true; $e("prWordsOut").innerHTML = `<p class="small muted">Reading the sentence…</p>`;
      try {
        const c = P.f.config;
        P.words = await send("/pipeline/from-text", "POST", { source_id: c.source_id, pipeline: c.pipeline, activity: c.activity, text: P.wordsText, utc_offset_min: OFFSET });
        if (P.words.ok) { P.f.config = { ...P.words.config }; P.f.name = P.words.name; P.preview = P.words.preview; }
      } catch (x) { P.words = { ok: false, reason: x.message }; }
      rerender();
    };
    if ($e("prSuggest")) $e("prSuggest").onclick = async () => {
      read(); $e("prSuggest").disabled = true; $e("prSuggestOut").innerHTML = `<p class="small muted">Measuring the last 30 days of runs…</p>`;
      const c = P.f.config;
      try { P.suggest = (await api(`/pipeline/suggest?source_id=${c.source_id}&pipeline=${encodeURIComponent(c.pipeline)}${c.activity ? `&activity=${encodeURIComponent(c.activity)}` : ""}&utc_offset_min=${OFFSET}`)).suggestions; }
      catch (x) { P.suggest = { error: x.message }; }
      rerender();
    };
    document.querySelectorAll("[data-use]").forEach((b) => (b.onclick = () => {
      const s = P.suggest[Number(b.dataset.use)];
      Object.assign(P.f, { name: s.name, severity: s.severity }); P.f.config = { ...s.config }; P.preview = s.preview;
      rerender();
    }));
    $e("prPreview").onclick = async () => {
      $e("prPreview").disabled = true; $e("prResult").innerHTML = `<p class="small muted">Checking the run history…</p>`;
      try { P.preview = await send("/preview", "POST", body()); $e("prResult").innerHTML = resultHtml(P.preview); }
      catch (x) { $e("prResult").innerHTML = `<div class="error-box">${esc(x.message)}</div>`; }
      $e("prPreview").disabled = false;
    };
    $e("prSave").onclick = async () => {
      $e("prSave").disabled = true;
      try {
        const b = body();
        const r = P.editing ? await send(`/${P.editing}`, "PUT", b) : await send("", "POST", b);
        const id = P.editing || r.check_id;
        toast(P.editing ? "Rule saved" : `Rule added: ${(r.result && LABEL[r.result.status]) || "checked"} now`);
        reset();
        window.CHECKS.open(id);
        document.querySelector('#tabs [data-page="estate-checks"]').click();
      } catch (x) { toast(x.message); $e("prSave").disabled = false; }
    };
  }
  function reset() {
    P.f = null; P.editing = null; P.preview = null; P.suggest = null; P.words = null; P.wordsText = "";
    if (window.CHECKS && window.CHECKS.resetForm) window.CHECKS.resetForm();
  }

  window.PRULES = {
    page,
    describe,
    reset,
    newFor(sourceId, pipeline) {                                    // from a pipeline's page: the form, that pipeline chosen
      reset();
      P.prefill = { source_id: Number(sourceId), pipeline };
      P.f = blank(P.prefill);
      document.querySelector('#tabs [data-page="estate-checks-new"]').click();
    },
  };
})();
