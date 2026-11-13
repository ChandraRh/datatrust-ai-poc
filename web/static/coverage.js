// Data quality → Coverage: which tables of the selected source are watched, and Start monitoring for the rest.
// Databases: every table with the kinds of rule it has. Factories: the tables its Copy steps load, watched between loads.
// Depends on app.js (esc, toast, state, ensureCore, sq) and estate.js (eapi, post, est, $e, renderEstate).
(function () {
  const cvapi = (p) => eapi("/checks" + p);
  const isAdmin = () => est.user && est.user.role === "admin";
  const BATCH = 5;                                    // tables per request: the page shows progress between batches
  const KINDS = { freshness: "fresh", volume: "volume", schema: "schema", null_rate: "empty values", uniqueness: "unique key",
    custom_sql: "own SQL", reconciliation: "reconciliation" };
  const STATUS = { PASS: ["ok", "Passing"], FAIL: ["bad", "Failing"], ERROR: ["bad", "Could not run"], WARN: ["warn", "Warning"] };
  const V = { data: null, sel: new Set(), q: "", onlyOpen: false, log: null, busy: false };

  const statusChip = (r) => {
    if (!r.monitored) return `<span class="chip grey">Not watched</span>`;
    const [c, l] = STATUS[r.status] || ["grey", "Not run yet"];
    return `<span class="chip ${c}">${l}</span>`;
  };
  const kindChips = (checks) => Object.keys(checks || {}).map((k) => `<span class="cv-kind">${esc(KINDS[k] || k)}</span>`).join("");
  const key = (r) => `${r.schema}.${r.table}`;

  // ---------------------------------------------------------------- databases
  function dbPage(d) {
    const tot = d.totals || { tables: 0, monitored: 0 };
    const pct = tot.tables ? Math.round((100 * tot.monitored) / tot.tables) : 0;
    const q = V.q.toLowerCase();
    const rows = d.tables.filter((r) => (!q || key(r).toLowerCase().includes(q)) && (!V.onlyOpen || !r.starter_done));
    const notWatched = d.tables.filter((r) => !r.starter_done);
    const head = `<div class="card cv-head">
        <div class="cv-figure"><b>${tot.monitored}</b> of ${tot.tables} tables watched</div>
        <div class="cv-bar" role="img" aria-label="${pct}% of tables watched"><span style="width:${pct}%"></span></div>
        <p class="small muted">${esc(d.connection.name)} · ${esc(d.connection.type)} · database ${esc(d.connection.database || "default")}${tot.truncated ? " · showing the first 500 tables" : ""}</p>
        <p class="small"><b>Start monitoring</b> measures each chosen table once (read-only) and adds three rules that fit it:
          <b>fresh</b> (new rows still arrive), <b>volume</b> (a normal amount arrives) and <b>schema</b> (no column dropped, renamed or retyped).
          Limits come from what the table shows today. Rules a table already has are kept. They run within the hour and appear in DQ Rule Studio.</p>
        ${isAdmin() ? "" : `<p class="small muted">Only an admin can start monitoring.</p>`}</div>`;
    const tools = `<div class="cv-tools">
        <input id="cvSearch" type="search" placeholder="Filter tables" value="${esc(V.q)}" aria-label="Filter tables">
        <label class="small"><input type="checkbox" id="cvOnlyOpen" ${V.onlyOpen ? "checked" : ""}> only tables missing a starter rule</label>
        <span class="grow"></span>
        ${isAdmin() ? `<button class="btn light btn-sm" id="cvSelAll" ${notWatched.length ? "" : "disabled"}>Select the ${notWatched.length} not fully watched</button>
        <button class="btn btn-sm" id="cvStart" ${V.sel.size && !V.busy ? "" : "disabled"}>Start monitoring${V.sel.size ? ` (${V.sel.size})` : ""}</button>` : ""}</div>`;
    const list = rows.length ? `<div class="cv-list">${rows.map((r) => `<label class="cv-row">
        ${isAdmin() ? `<input type="checkbox" data-cv="${esc(key(r))}" ${V.sel.has(key(r)) ? "checked" : ""} ${r.starter_done ? "disabled" : ""}>` : ""}
        <span class="cv-name"><b>${esc(r.schema)}.</b>${esc(r.table)} <span class="small muted">${r.columns} columns${r.has_time_column ? "" : " · no date column"}</span></span>
        <span class="cv-kinds">${kindChips(r.checks)}</span>${statusChip(r)}</label>`).join("")}</div>`
      : `<p class="muted">${d.tables.length ? "No table matches the filter." : "No tables found in this database."}</p>`;
    return head + (V.log ? logCard() : "") + `<div class="card">${tools}${list}</div>`;
  }

  function logCard() {
    const L = V.log;
    const rows = L.results.map((r) => `<div class="cv-res"><b>${esc(r.schema)}.${esc(r.table)}</b>
        ${r.error ? `<span class="chip bad">${esc(r.error)}</span>` : ""}
        ${r.added.length ? `<div class="small">Added: ${r.added.map((a) => `<a href="#estate-checks=${a.check_id}">${esc(a.name)}</a>`).join(", ")}</div>` : ""}
        ${r.kept.length ? `<div class="small muted">Already had: ${r.kept.map((k) => esc(KINDS[k] || k)).join(", ")}</div>` : ""}
        ${(r.skipped || []).map((s) => `<div class="small muted">Not added (${esc(KINDS[s.check_type] || s.check_type)}): ${esc(s.why)}</div>`).join("")}</div>`).join("");
    return `<div class="card cv-log" aria-live="polite"><h3>${L.done ? `Monitoring started: ${L.added} rule(s) on ${L.results.filter((r) => r.added.length).length} table(s)` : `Measuring tables… ${L.results.length} of ${L.total}`}</h3>
      ${L.done ? `<p class="small muted">The new rules run within the hour; results appear here, in DQ Rule Studio and on Observe.</p>` : ""}${rows}</div>`;
  }

  async function startSelected(connId) {
    const all = [...V.sel];
    V.busy = true;
    V.log = { total: all.length, results: [], added: 0, done: false };
    rerender();
    for (let i = 0; i < all.length; i += BATCH) {
      const part = all.slice(i, i + BATCH).map((k) => { const at = k.indexOf("."); return { schema_name: k.slice(0, at), table_name: k.slice(at + 1) }; });
      try {
        const r = await post("/checks/coverage/start", { connection_id: connId, tables: part });
        V.log.results.push(...r.results);
        V.log.added += r.added;
      } catch (e) {
        part.forEach((p) => V.log.results.push({ schema: p.schema_name, table: p.table_name, added: [], kept: [], skipped: [], error: e.message }));
      }
      rerender();
    }
    V.log.done = true;
    V.busy = false;
    V.sel.clear();
    V.data = null;                                    // reload coverage with the new rules
    toast(`Monitoring started: ${V.log.added} rule(s) added`);
    rerender();
  }

  // ---------------------------------------------------------------- factories
  function adfPage(d) {
    const tot = d.totals;
    const list = d.targets.length ? d.targets.map((r) => `<div class="cv-row">
        <span class="cv-name"><span class="small muted">${esc(r.pipeline)} → </span>${r.table ? `<b>${esc(r.schema)}.</b>${esc(r.table)}` : "<i>table set at run time</i>"}
          <span class="small muted">${r.connection ? "in " + esc(r.connection.name) : esc(r.host || "")}${r.from_lookup && r.table ? " · named by the pipeline's Lookup in recent runs" : ""}</span>
          ${r.note ? `<div class="small muted">${esc(r.note)}</div>` : ""}</span>
        <span class="cv-kinds">${kindChips(r.checks)}</span>
        ${r.connection ? statusChip(r) : `<span class="chip grey">Cannot watch yet</span>`}
        ${r.connection && !r.starter_done && isAdmin() ? `<button class="btn btn-sm" data-cvt="${r.connection.id}|${esc(r.schema)}|${esc(r.table)}">Monitor this table</button>` : ""}</div>`).join("")
      : `<p class="muted">No Copy step of this factory writes into a PostgreSQL or SQL Server table that DataTrust can name.</p>`;
    return `<div class="card cv-head"><div class="cv-figure"><b>${tot.monitored}</b> of ${tot.tables} tables this factory loads are watched between loads</div>
        <p class="small">DataTrust already checks each target table right after a run (on <b>ADF runs</b>). Watching it here adds rules that also run
          <b>between</b> loads, on their own schedule: they catch a load that never happened (trigger off), a load that was too small, and a table
          that was changed or emptied after the load.</p></div>
      ${V.log ? logCard() : ""}<div class="card cv-list">${list}</div>`;
  }

  async function monitorTarget(btn) {
    const [cid, schema, table] = btn.dataset.cvt.split("|");
    btn.disabled = true;
    btn.textContent = "Measuring…";
    try {
      const r = await post("/checks/coverage/start", { connection_id: Number(cid), tables: [{ schema_name: schema, table_name: table }] });
      V.log = { total: 1, results: r.results, added: r.added, done: true };
      toast(`${r.added} rule(s) added to ${schema}.${table}`);
    } catch (e) { toast(e.message); }
    V.data = null;
    rerender();
  }

  // ---------------------------------------------------------------- page
  let lastScope = null;
  async function page() {
    await ensureCore();
    const s = state.overview.scope || {};
    if (lastScope !== s.key) { V.data = null; V.sel.clear(); V.log = null; V.q = ""; lastScope = s.key; }
    if (!V.data) V.data = await cvapi("/coverage?" + sq());
    const d = V.data;
    const sub = $e("dqCoverageSub");
    if (sub) sub.textContent = d.kind === "adf" ? "The tables this factory loads, and whether DataTrust watches them between loads."
      : "Which tables of this database DataTrust watches, and Start monitoring for the rest.";
    const note = `<div class="banner ok small dq-source"><b>${esc(s.name || "")}</b> (${esc(s.type || "")}) · Change the source with <b>Source</b> at the top.</div>`;
    if (d.error) return { html: note + `<div class="error-box">${esc(d.error)}</div>`, bind() {} };
    const html = note + (d.kind === "adf" ? adfPage(d) : dbPage(d));
    return { html, bind: () => bind(d) };
  }

  function bind(d) {
    const box = $e("body-estate-dq-coverage");
    if (!box) return;
    box.querySelectorAll("[data-cv]").forEach((c) => (c.onchange = () => {
      c.checked ? V.sel.add(c.dataset.cv) : V.sel.delete(c.dataset.cv);
      const b = $e("cvStart");
      if (b) { b.disabled = !V.sel.size || V.busy; b.textContent = `Start monitoring${V.sel.size ? ` (${V.sel.size})` : ""}`; }
    }));
    const s = $e("cvSearch");
    if (s) s.oninput = () => { V.q = s.value; const pos = s.selectionStart; rerender().then(() => { const n = $e("cvSearch"); if (n) { n.focus(); n.setSelectionRange(pos, pos); } }); };
    const o = $e("cvOnlyOpen");
    if (o) o.onchange = () => { V.onlyOpen = o.checked; rerender(); };
    const all = $e("cvSelAll");
    if (all) all.onclick = () => { d.tables.filter((r) => !r.starter_done).forEach((r) => V.sel.add(key(r))); rerender(); };
    const st = $e("cvStart");
    if (st) st.onclick = () => startSelected(d.connection.id);
    box.querySelectorAll("[data-cvt]").forEach((b) => (b.onclick = () => monitorTarget(b)));
  }

  async function rerender() {
    const box = $e("body-estate-dq-coverage");
    if (!box || box.closest(".page.hidden")) return;
    try {
      const body = await page();
      box.innerHTML = body.html;
      body.bind();
    } catch (e) { box.innerHTML = `<div class="error-box">${esc(e.message)}</div>`; }
  }

  window.COVERAGE = { page };
})();
