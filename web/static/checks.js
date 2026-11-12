// Data checks pages (sidebar group "Data checks"): the list with history, one check's evidence, and the add / edit form.
// Rendered by estate.js after login (pages estate-checks, estate-checks-new). Uses app.js (esc, num, toast) and estate.js (eapi, est, $e).
(function () {
  const C = { open: null, editing: null, filter: { conn: "", status: "" }, cache: {}, f: null, preview: null };
  const api = (p, o) => eapi("/checks" + p, o);
  const send = (p, method, data) => api(p, { method, body: JSON.stringify(data || {}) });
  const isAdmin = () => est.user && est.user.role === "admin";

  const parseT = (t) => (t ? new Date(String(t).replace(" ", "T").replace(/(\.\d{3})\d+/, "$1")) : null);
  const when = (t) => { const d = parseT(t); return d && !isNaN(d) ? d.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" }) : "–"; };
  const ago = (t) => {
    const d = parseT(t); if (!d || isNaN(d)) return "never";
    const s = (Date.now() - d) / 1000;
    return s < 60 ? "just now" : s < 3600 ? `${Math.round(s / 60)} min ago` : s < 86400 ? `${(s / 3600).toFixed(1)} h ago` : `${Math.round(s / 86400)} d ago`;
  };
  const n0 = (v) => (v == null ? "–" : Number(v).toLocaleString(undefined, { maximumFractionDigits: 2 }));
  const ST = { PASS: "ok", WARN: "warn", FAIL: "bad", ERROR: "bad" };
  const LABEL = { PASS: "Pass", WARN: "Warning", FAIL: "Fail", ERROR: "Error" };
  const chip = (s) => `<span class="chip ${ST[s] || "grey"}">${esc(LABEL[s] || (s ? s : "Not run"))}</span>`;
  const SEV = { CRITICAL: "bad", HIGH: "bad", MEDIUM: "warn", LOW: "grey" };
  const errBox = (e) => `<div class="error-box">${esc(e.message || e)}</div>`;
  const TYPES = {
    freshness: ["Freshness", "The newest date or time in a column is recent enough."],
    volume: ["Row volume", "The table has rows, and not far fewer than usual (learned from earlier runs)."],
    null_rate: ["Empty values", "A column is filled in (at most a % of empty values)."],
    uniqueness: ["Unique key", "No key value appears more than once."],
    schema: ["Schema change", "Columns were not removed, renamed or changed type since the accepted schema."],
    custom_sql: ["Custom SQL", "Your own read-only query: it must return no rows, or a number that meets a condition."],
    reconciliation: ["Reconciliation", "Two queries (same or different databases) must agree per key, e.g. source totals vs target totals."],
  };
  const TABLE_TYPES = ["freshness", "volume", "null_rate", "uniqueness", "schema"];
  const SCHEDULES = [[15, "every 15 min"], [60, "hourly"], [360, "every 6 h"], [1440, "daily"]];
  const where = (c) => c.check_type === "reconciliation"
    ? `${esc((c.config.left || {}).label || "left")} ↔ ${esc((c.config.right || {}).label || "right")}`
    : `${esc(c.connection_name || "connection " + c.connection_id)}${c.table_name ? ` · <code>${esc(c.schema_name)}.${esc(c.table_name)}</code>` : c.database ? ` · ${esc(c.database)}` : ""}`;
  const dots = (h) => `<span class="ck-dots" aria-label="last runs">${(h || []).map((s) => `<i class="${ST[s] || "grey"}" title="${esc(LABEL[s] || s)}"></i>`).join("")}</span>`;

  // ------------------------------------------------------------------ evidence of one result
  function evidence(c, r) {
    const d = r.details || {};
    const table = (rows) => rows && rows.length ? objTable(rows) : "";
    if (c.check_type === "reconciliation" && (d.mismatches || []).length) {
      const L = d.left_label || "left", R = d.right_label || "right";
      const tr = [];                                 // own table: values are shown exactly as measured (no money guessing)
      d.mismatches.forEach((m) => {
        if (m.problem !== "values differ") tr.push(`<tr><td>${esc(m.key)}</td><td class="muted">–</td><td colspan="3">${esc(m.problem)}</td></tr>`);
        else Object.entries(m.differences).forEach(([k, v]) => tr.push(`<tr><td>${esc(m.key)}</td><td>${esc(k)}</td><td class="num">${n0(v[L])}</td>
          <td class="num">${n0(v[R])}</td><td class="num"><b>${v.difference > 0 ? "+" : ""}${n0(v.difference)}</b></td></tr>`));
      });
      return `<h4>Differences <span class="small muted">(${d.mismatches.length} of ${d.compared_keys} ${esc(d.key_column || "keys")})</span></h4>
        <div class="tablewrap"><table><thead><tr><th>${esc(d.key_column || "key")}</th><th>Measure</th><th class="num">${esc(L)}</th><th class="num">${esc(R)}</th>
        <th class="num">Difference</th></tr></thead><tbody>${tr.join("")}</tbody></table></div>`;
    }
    if (c.check_type === "uniqueness" && (d.duplicates || []).length) return `<h4>Duplicate keys <span class="small muted">(first ${d.duplicates.length})</span></h4>${table(d.duplicates)}`;
    if (c.check_type === "custom_sql" && (d.rows || []).length) return `<h4>Rows returned <span class="small muted">(first ${d.rows.length})</span></h4>${table(d.rows)}`;
    if (c.check_type === "schema" && (d.removed || d.added || d.type_changed)) {
      const parts = [];
      if ((d.removed || []).length) parts.push(`<li><b>Removed:</b> ${d.removed.map((x) => `<code>${esc(x)}</code>`).join(", ")}</li>`);
      if ((d.type_changed || []).length) parts.push(`<li><b>Type changed:</b> ${d.type_changed.map((x) => `<code>${esc(x.column)}</code> ${esc(x.was)} → ${esc(x.now)}`).join(", ")}</li>`);
      if ((d.added || []).length) parts.push(`<li><b>Added:</b> ${d.added.map((x) => `<code>${esc(x)}</code>`).join(", ")}</li>`);
      return parts.length ? `<h4>Schema differences</h4><ul class="small">${parts.join("")}</ul>` : "";
    }
    return "";
  }
  function resultBox(c, r) {
    return `<div class="ck-result ck-${ST[r.status] || "grey"}">
        <div class="ck-result-head">${chip(r.status)} <b>${esc(r.message || "")}</b></div>
        <div class="small muted">${r.expected ? `Expected: ${esc(r.expected)}` : ""}${r.observed != null ? ` · measured ${n0(r.observed)}` : ""}${r.duration_ms != null ? ` · ${n0(r.duration_ms)} ms` : ""}</div>
        ${evidence(c, r)}
        ${r.sql || r.sql_text ? `<details><summary class="small">SQL that ran</summary><pre class="code">${esc(r.sql || r.sql_text)}</pre></details>` : ""}</div>`;
  }

  // ------------------------------------------------------------------ list
  async function pageList() {
    const [data, conns] = await Promise.all([api(C.filter.conn ? `?connection_id=${C.filter.conn}` : ""), eapi("/connections").catch(() => [])]);
    const all = data.checks, s = data.summary || {};
    const shown = all.filter((c) => !C.filter.status || (C.filter.status === "NOT RUN" ? !c.last_status : c.last_status === C.filter.status));
    const k = (label, v, cls, sub) => `<div class="kpi ${v && cls ? cls : ""}"><div class="l">${label}</div><div class="v">${n0(v || 0)}</div><div class="small muted">${sub}</div></div>`;
    const rows = shown.map((c) => `<tr class="${c.enabled ? "" : "muted"}">
        <td>${chip(c.last_status)}</td>
        <td><a href="#" data-open="${c.check_id}"><b>${esc(c.name)}</b></a><div class="small muted">${where(c)}</div></td>
        <td class="small">${esc((TYPES[c.check_type] || [c.check_type])[0])}<div><span class="chip ${SEV[c.severity]}">${esc(c.severity)}</span></div></td>
        <td class="small ck-msg">${esc(c.message || "–")}</td>
        <td class="small nowrap">${esc(ago(c.last_run_at))}<div class="muted">${c.enabled ? esc((SCHEDULES.find((x) => x[0] === c.schedule_minutes) || [0, `every ${c.schedule_minutes} min`])[1]) : "paused"}</div></td>
        <td>${dots(c.history)}</td>
        <td><button class="btn light btn-sm" data-run="${c.check_id}">Run now</button></td></tr>`).join("");
    const sched = data.scheduler || {};
    return {
      html: `<div class="kpis">
          ${k("Checks", all.length, "", `${all.filter((c) => c.enabled).length} active`)}
          ${k("Failing", s.FAIL, "bad", "rule broken now")}
          ${k("Errors", s.ERROR, "bad", "could not run")}
          ${k("Warnings", s.WARN, "warn", "worth a look")}
          ${k("Passing", s.PASS, "", "as expected")}
          ${k("Not run yet", s["NOT RUN"], "", "waiting for the scheduler")}</div>
        <div class="card"><div class="draft-head"><h3 style="margin:0">Checks</h3><span class="grow"></span>
          <label class="small">Source <select id="ckConn"><option value="">All</option>${conns.map((x) => `<option value="${x.connection_id}" ${String(C.filter.conn) === String(x.connection_id) ? "selected" : ""}>${esc(x.name)}</option>`).join("")}</select></label>
          <label class="small">Status <select id="ckStatus">${[["", "All"], ["FAIL", "Failing"], ["ERROR", "Error"], ["WARN", "Warning"], ["PASS", "Passing"], ["NOT RUN", "Not run"]].map(([v, l]) => `<option value="${v}" ${C.filter.status === v ? "selected" : ""}>${l}</option>`).join("")}</select></label>
          ${isAdmin() ? `<button class="btn btn-sm" id="ckAdd">+ Add check</button>` : ""}</div>
          ${shown.length ? `<div class="tablewrap ck-table"><table><thead><tr><th>Status</th><th>Check</th><th>Type</th><th>Latest result</th><th>Last run</th><th>Last 20 runs</th><th></th></tr></thead><tbody>${rows}</tbody></table></div>`
            : `<p class="muted">${all.length ? "No check matches these filters." : "No checks yet. Add one to start watching a table, a query or a reconciliation."}</p>`}
          <p class="small muted">The scheduler ${sched.running ? `runs due checks every ${sched.every_seconds} s` : "is not running in this server (CHECKS_IN_WEB=false)"}.
            Checks only read: PostgreSQL in a read-only session, SQL Server inside a transaction that is always rolled back.${sched.last_error ? ` Last scheduler error: ${esc(sched.last_error)}` : ""}</p></div>`,
      bind() {
        const el = $e("body-estate-checks");
        $e("ckConn").onchange = (e) => { C.filter.conn = e.target.value; renderEstate("estate-checks"); };
        $e("ckStatus").onchange = (e) => { C.filter.status = e.target.value; renderEstate("estate-checks"); };
        if ($e("ckAdd")) $e("ckAdd").onclick = () => goForm(null);
        el.querySelectorAll("[data-open]").forEach((a) => (a.onclick = (e) => { e.preventDefault(); C.open = Number(a.dataset.open); renderEstate("estate-checks"); }));
        el.querySelectorAll("[data-run]").forEach((b) => (b.onclick = async () => {
          b.disabled = true; b.textContent = "Running…";
          try { const r = await send(`/${b.dataset.run}/run`, "POST"); toast(`${LABEL[r.status] || r.status}: ${r.message}`); renderEstate("estate-checks"); }
          catch (x) { toast(x.message); b.disabled = false; b.textContent = "Run now"; }
        }));
      },
    };
  }

  // ------------------------------------------------------------------ one check
  async function pageDetail(id) {
    const { check: c, results } = await api(`/${id}`);
    const last = results[0];
    const facts = [["Type", (TYPES[c.check_type] || [c.check_type])[0]], ["Checks", where(c)], ["Severity if it fails", `<span class="chip ${SEV[c.severity]}">${esc(c.severity)}</span>`],
      ["Schedule", c.enabled ? esc((SCHEDULES.find((x) => x[0] === c.schedule_minutes) || [0, `every ${c.schedule_minutes} min`])[1]) : "paused"],
      ["Added by", `${esc(c.created_by || "–")}${c.origin === "starter" ? " (starter check)" : ""}`], ["Last run", esc(when(c.last_run_at))]];
    const cfg = c.config || {};
    const rule = c.check_type === "freshness" ? `newest <code>${esc(cfg.column)}</code> at most ${esc(cfg.max_age_hours || 24)} h old`
      : c.check_type === "volume" ? `at least ${esc(cfg.min_rows ?? 1)} rows; fail if ${esc(cfg.max_drop_pct || 50)}% below usual${cfg.max_growth_pct ? `; warn if ${esc(cfg.max_growth_pct)}% above` : ""}`
      : c.check_type === "null_rate" ? `<code>${esc(cfg.column)}</code> at most ${esc(cfg.max_null_pct || 0)}% empty`
      : c.check_type === "uniqueness" ? `<code>${esc((cfg.columns || []).join(" + "))}</code> unique`
      : c.check_type === "schema" ? `${Object.keys((c.state || {}).columns || {}).length} accepted columns`
      : c.check_type === "custom_sql" ? ((cfg.mode || "no_rows") === "no_rows" ? "the query returns no rows" : `the query's value ${esc(cfg.op)} ${esc(cfg.threshold)}`)
      : `per ${esc(cfg.match === "left" ? "key of the left query" : "key on either side")}, values equal${cfg.tolerance ? ` within ${esc(cfg.tolerance)}` : ""}`;
    return {
      html: `<div class="draft-head"><button class="btn light btn-sm" id="ckBack">← All checks</button><span class="grow"></span>
          <button class="btn light btn-sm" id="ckRun">Run now</button>
          ${isAdmin() ? `<button class="btn light btn-sm" id="ckEdit">Edit</button><button class="btn light btn-sm" id="ckPause">${c.enabled ? "Pause" : "Resume"}</button>
            ${c.check_type === "schema" && last && ["WARN", "FAIL"].includes(last.status) ? `<button class="btn btn-sm" id="ckAccept">Accept current schema</button>` : ""}
            <span id="ckDelBox"><button class="btn light btn-sm" id="ckDel">Remove</button></span>` : ""}</div>
        <div class="card"><h2 style="margin:0 0 4px">${esc(c.name)} ${chip(c.last_status)}</h2>
          ${c.description ? `<p class="muted" style="margin:0 0 10px">${esc(c.description)}</p>` : ""}
          <div class="ck-facts">${facts.map(([l, v]) => `<div><span class="l">${l}</span><b>${v}</b></div>`).join("")}<div class="wide"><span class="l">Rule</span><b>${rule}</b></div></div></div>
        ${last ? `<div class="card"><h3>Latest result <span class="small muted">${esc(when(last.run_at))} · ${esc(last.triggered_by || "")}</span></h3>${resultBox(c, last)}</div>` : `<div class="card"><p class="muted">Not run yet.</p></div>`}
        <div class="card"><h3>History</h3>${results.length ? `<div class="tablewrap ck-table"><table><thead><tr><th>Run</th><th>Status</th><th class="num">Measured</th><th>Result</th><th>By</th></tr></thead><tbody>
          ${results.map((r) => `<tr><td class="small nowrap">${esc(when(r.run_at))}</td><td>${chip(r.status)}</td><td class="num">${n0(r.observed)}</td><td class="small ck-msg">${esc(r.message || "")}</td><td class="small">${esc(r.triggered_by || "")}</td></tr>`).join("")}</tbody></table></div>` : "<p class='muted'>No runs yet.</p>"}</div>`,
      bind() {
        $e("ckBack").onclick = () => { C.open = null; renderEstate("estate-checks"); };
        $e("ckRun").onclick = async () => { $e("ckRun").disabled = true; try { await send(`/${id}/run`, "POST"); renderEstate("estate-checks"); } catch (x) { toast(x.message); $e("ckRun").disabled = false; } };
        if (!isAdmin()) return;
        $e("ckEdit").onclick = () => goForm(c);
        $e("ckPause").onclick = async () => { try { await send(`/${id}`, "PUT", { ...c, enabled: !c.enabled }); toast(c.enabled ? "Paused" : "Resumed"); renderEstate("estate-checks"); } catch (x) { toast(x.message); } };
        if ($e("ckAccept")) $e("ckAccept").onclick = async () => { try { await send(`/${id}/accept-schema`, "POST"); toast("Current columns accepted"); renderEstate("estate-checks"); } catch (x) { toast(x.message); } };
        $e("ckDel").onclick = () => {
          $e("ckDelBox").innerHTML = `<span class="small">Remove this check and its history? <button class="btn btn-sm" id="ckDelYes">Remove</button> <button class="btn light btn-sm" id="ckDelNo">Keep</button></span>`;
          $e("ckDelNo").onclick = () => renderEstate("estate-checks");
          $e("ckDelYes").onclick = async () => { await api(`/${id}`, { method: "DELETE" }); toast("Removed"); C.open = null; renderEstate("estate-checks"); };
        };
      },
    };
  }

  // ------------------------------------------------------------------ add / edit
  function goForm(c) {
    C.editing = c ? c.check_id : null;
    C.f = c ? JSON.parse(JSON.stringify({ name: c.name, check_type: c.check_type, connection_id: c.connection_id, database: c.database, schema_name: c.schema_name,
      table_name: c.table_name, config: c.config || {}, severity: c.severity, schedule_minutes: c.schedule_minutes, enabled: c.enabled, description: c.description })) : null;
    C.preview = null;
    document.querySelector('#tabs [data-page="estate-checks-new"]').click();
  }
  const blank = () => ({ name: "", check_type: "freshness", connection_id: "", database: "", schema_name: "", table_name: "", severity: "MEDIUM",
    schedule_minutes: 60, enabled: true, description: "", config: { left: { label: "source" }, right: { label: "target" }, match: "both" } });

  async function cached(key, fn) { if (!(key in C.cache)) C.cache[key] = await fn().catch((e) => ({ error: e.message })); return C.cache[key]; }
  const dbsOf = (cid) => cached(`db|${cid}`, () => eapi(`/connections/${cid}/overview`).then((o) => (o.ok ? o.databases.filter((d) => d.can_connect).map((d) => d.name) : { error: o.error })));
  const schemasOf = (cid, db) => cached(`s|${cid}|${db}`, () => eapi(`/connections/${cid}/db/${encodeURIComponent(db)}/schemas`).then((r) => r.map((s) => s.name)));
  const tablesOf = (cid, db, s) => cached(`t|${cid}|${db}|${s}`, () => eapi(`/connections/${cid}/db/${encodeURIComponent(db)}/schemas/${encodeURIComponent(s)}/objects`)
    .then((r) => r.filter((o) => ["TABLE", "VIEW", "MATERIALIZED VIEW"].includes(o.type)).map((o) => o.name)));
  const colsOf = (cid, db, s, t) => cached(`c|${cid}|${db}|${s}|${t}`, () => api(`/columns?connection_id=${cid}&database=${encodeURIComponent(db)}&schema=${encodeURIComponent(s)}&table=${encodeURIComponent(t)}`));

  const opt = (list, val, placeholder) => `<option value="">${esc(placeholder)}</option>` +
    (Array.isArray(list) ? list : []).map((x) => { const v = typeof x === "object" ? x.name : x; return `<option value="${esc(v)}" ${String(val ?? "") === String(v) ? "selected" : ""}>${esc(v)}</option>`; }).join("");
  const sel = (id, label, inner, help) => `<label class="field"><span>${label}</span><select id="${id}">${inner}</select>${help ? `<span class="small muted">${help}</span>` : ""}</label>`;
  const inp = (id, label, val, ph, help, type = "text") => `<label class="field"><span>${label}</span><input id="${id}" type="${type}" value="${esc(val ?? "")}" placeholder="${esc(ph || "")}" autocomplete="off">${help ? `<span class="small muted">${help}</span>` : ""}</label>`;
  const area = (id, label, val, ph) => `<label class="field wide"><span>${label}</span><textarea id="${id}" rows="5" spellcheck="false" placeholder="${esc(ph || "")}">${esc(val ?? "")}</textarea></label>`;

  async function sourcePickers(prefix, cid, db, conns, withTable) {
    const sqlConns = conns.filter((x) => ["postgresql", "sqlserver"].includes(x.source_type));
    const connOpts = `<option value="">Choose…</option>` + sqlConns.map((x) =>
      `<option value="${x.connection_id}" ${String(cid ?? "") === String(x.connection_id) ? "selected" : ""}>${esc(x.name)}</option>`).join("");
    let html = sel(`${prefix}Conn`, "Connection", connOpts, "PostgreSQL or SQL Server, from Data estate → Connections");
    if (!cid) return html;
    const dbs = await dbsOf(cid);
    const def = (sqlConns.find((x) => String(x.connection_id) === String(cid)) || {}).database;
    html += dbs.error ? `<div class="error-box small">${esc(dbs.error)}</div>` : sel(`${prefix}Db`, "Database", opt(dbs, db || def, "Choose…"));
    if (!withTable || dbs.error || !(db || def)) return html;
    const f = C.f, d = db || def;
    const schemas = await schemasOf(cid, d);
    html += sel("fSchema", "Schema", opt(schemas.error ? [] : schemas, f.schema_name, "Choose…"));
    if (f.schema_name) {
      const tables = await tablesOf(cid, d, f.schema_name);
      html += sel("fTable", "Table or view", opt(tables.error ? [] : tables, f.table_name, "Choose…"));
    }
    return html;
  }

  async function pageForm() {
    if (!isAdmin()) return { html: `<div class="card"><p class="muted">Only an admin can add or change checks.</p></div>`, bind() {} };
    const f = (C.f = C.f || blank());
    f.config = f.config || {};
    const conns = await eapi("/connections").catch(() => []);
    const t = f.check_type, c = f.config;
    let specific = "";
    if (TABLE_TYPES.includes(t)) {
      specific += await sourcePickers("f", f.connection_id, f.database, conns, true);
      const ready = f.connection_id && f.schema_name && f.table_name;
      const cols = ready ? await colsOf(f.connection_id, f.database || (conns.find((x) => String(x.connection_id) === String(f.connection_id)) || {}).database, f.schema_name, f.table_name) : [];
      const colOpts = (v, ph) => opt((cols.error ? [] : cols).map((x) => ({ name: x.name })), v, ph);
      if (cols.error) specific += `<div class="error-box small">${esc(cols.error)}</div>`;
      if (ready && t === "freshness") specific += sel("cCol", "Date / time column", colOpts(c.column, "Choose…"), "e.g. loaded_at, updated_at, business_date") + inp("cAge", "Max age (hours)", c.max_age_hours ?? 24, "24", "", "number");
      if (ready && t === "volume") specific += inp("cMin", "Minimum rows", c.min_rows ?? 1, "1", "", "number") + inp("cDrop", "Fail if below usual by (%)", c.max_drop_pct ?? 50, "50", "usual = median of recent runs", "number")
        + inp("cGrow", "Warn if above usual by (%)", c.max_growth_pct ?? "", "optional", "", "number");
      if (ready && t === "null_rate") specific += sel("cCol", "Column", colOpts(c.column, "Choose…")) + inp("cNull", "Max empty (%)", c.max_null_pct ?? 0, "0", "", "number");
      if (ready && t === "uniqueness") specific += `<label class="field"><span>Key column(s)</span><select id="cCols" multiple size="5">${(cols.error ? [] : cols).map((x) => `<option value="${esc(x.name)}" ${(c.columns || []).includes(x.name) ? "selected" : ""}>${esc(x.name)}</option>`).join("")}</select><span class="small muted">Ctrl+click for a key of several columns</span></label>`;
      if (ready && ["volume", "null_rate", "uniqueness", "freshness"].includes(t)) specific += inp("cWhere", "Only rows where (optional)", c.where || "", "e.g. business_date >= current_date - 7", "a SQL condition");
    } else if (t === "custom_sql") {
      specific += await sourcePickers("f", f.connection_id, f.database, conns, false);
      specific += area("cSql", "Query (read-only SELECT)", c.sql, "SELECT order_id FROM sales.orders WHERE amount < 0");
      specific += sel("cMode", "Passes when", `<option value="no_rows" ${(c.mode || "no_rows") === "no_rows" ? "selected" : ""}>it returns no rows</option><option value="value" ${c.mode === "value" ? "selected" : ""}>its first value meets a condition</option>`);
      if (c.mode === "value") specific += sel("cOp", "Condition", ["<", "<=", "=", "!=", ">=", ">"].map((o) => `<option ${(c.op || "=") === o ? "selected" : ""}>${o}</option>`).join("")) + inp("cThr", "Value", c.threshold ?? 0, "0", "", "number");
    } else {
      for (const side of ["left", "right"]) {
        const s = (c[side] = c[side] || {});
        specific += `<div class="ck-side wide"><h4>${side === "left" ? "First query (e.g. the source)" : "Second query (e.g. the target)"}</h4><div class="form-grid adf-form">
          ${inp(`${side}Label`, "Name", s.label, side === "left" ? "source" : "target")}
          ${await sourcePickers(side, s.connection_id, s.database, conns, false)}
          ${area(`${side}Sql`, "Query: first column = key, then the numbers to compare", s.sql, side === "left" ? "SELECT business_date, COUNT(*), SUM(amount) FROM src.payments GROUP BY business_date" : "SELECT business_date, COUNT(*), SUM(amount) FROM dw.payments GROUP BY business_date")}</div></div>`;
      }
      specific += sel("cMatch", "Compare", `<option value="both" ${c.match !== "left" ? "selected" : ""}>keys from both queries (a key missing on either side fails)</option><option value="left" ${c.match === "left" ? "selected" : ""}>only keys of the first query</option>`)
        + inp("cTol", "Tolerance", c.tolerance ?? 0, "0", "allowed difference, e.g. 0.01 for money", "number");
    }
    const html = `<div class="card" id="ckForm"><h3>${C.editing ? "Edit check" : "Add a check"}</h3>
        <p class="small muted">Checks only read. Pick what to watch; <b>Preview</b> runs it once without saving, so you see exactly what it would report.</p>
        <div class="form-grid adf-form">
          ${sel("fType", "Type", Object.entries(TYPES).map(([k, [l]]) => `<option value="${k}" ${t === k ? "selected" : ""}>${l}</option>`).join(""), esc(TYPES[t][1]))}
          ${inp("fName", "Name", f.name, "e.g. orders loaded today")}
          ${sel("fSev", "Severity if it fails", ["LOW", "MEDIUM", "HIGH", "CRITICAL"].map((s) => `<option ${f.severity === s ? "selected" : ""}>${s}</option>`).join(""))}
          ${sel("fSched", "Run", SCHEDULES.map(([m, l]) => `<option value="${m}" ${Number(f.schedule_minutes) === m ? "selected" : ""}>${l}</option>`).join(""))}
          ${specific}
          ${inp("fDesc", "Why it matters (optional)", f.description, "shown on the check and in incidents")}
        </div>
        <div class="pacts"><button class="btn light" id="ckPreview">Preview</button><button class="btn" id="ckSave">${C.editing ? "Save changes" : "Add check"}</button>
          <button class="btn light" id="ckCancel">Cancel</button>
          ${C.editing ? `<label class="small"><input type="checkbox" id="fEnabled" ${f.enabled ? "checked" : ""}> active</label>` : ""}</div>
        <div id="ckOut">${C.preview ? resultBox({ check_type: t }, C.preview) : ""}</div></div>`;
    return { html, bind: bindForm };
  }

  function readForm() {
    const f = C.f, c = f.config, v = (id) => ($e(id) ? $e(id).value : undefined);
    f.check_type = v("fType"); f.name = v("fName").trim(); f.severity = v("fSev"); f.schedule_minutes = Number(v("fSched")); f.description = v("fDesc");
    if ($e("fEnabled")) f.enabled = $e("fEnabled").checked;
    if (v("fConn") !== undefined) f.connection_id = v("fConn");
    if (v("fDb") !== undefined) f.database = v("fDb");
    if (v("fSchema") !== undefined) f.schema_name = v("fSchema");
    if (v("fTable") !== undefined) f.table_name = v("fTable");
    const num = (id) => (v(id) === "" || v(id) === undefined ? undefined : Number(v(id)));
    if (v("cCol") !== undefined) c.column = v("cCol");
    if (v("cAge") !== undefined) c.max_age_hours = num("cAge");
    if (v("cMin") !== undefined) c.min_rows = num("cMin");
    if (v("cDrop") !== undefined) c.max_drop_pct = num("cDrop");
    if (v("cGrow") !== undefined) c.max_growth_pct = num("cGrow");
    if (v("cNull") !== undefined) c.max_null_pct = num("cNull");
    if ($e("cCols")) c.columns = [...$e("cCols").selectedOptions].map((o) => o.value);
    if (v("cWhere") !== undefined) c.where = v("cWhere");
    if (v("cSql") !== undefined) c.sql = v("cSql");
    if (v("cMode") !== undefined) c.mode = v("cMode");
    if (v("cOp") !== undefined) c.op = v("cOp");
    if (v("cThr") !== undefined) c.threshold = num("cThr");
    if (v("cMatch") !== undefined) c.match = v("cMatch");
    if (v("cTol") !== undefined) c.tolerance = num("cTol");
    for (const side of ["left", "right"]) {
      if (v(`${side}Sql`) === undefined) continue;
      c[side] = { ...(c[side] || {}), label: v(`${side}Label`), sql: v(`${side}Sql`), connection_id: v(`${side}Conn`) };
      if (v(`${side}Db`) !== undefined) c[side].database = v(`${side}Db`);
    }
    if (!f.name) f.name = suggestName(f);
    return f;
  }
  function suggestName(f) {
    const tbl = f.table_name ? `${f.schema_name}.${f.table_name}` : "";
    const c = f.config;
    return { freshness: `${tbl} is fresh`, volume: `${tbl} row volume`, null_rate: `${c.column || "column"} filled in ${tbl}`,
      uniqueness: `${(c.columns || []).join(" + ") || "key"} unique in ${tbl}`, schema: `${tbl} columns unchanged`,
      custom_sql: "Custom check", reconciliation: `${(c.left || {}).label || "source"} matches ${(c.right || {}).label || "target"}` }[f.check_type];
  }
  function bindForm() {
    const redraw = () => { readForm(); renderEstate("estate-checks-new"); };
    const reset = (...keys) => () => { readForm(); keys.forEach((k) => (C.f[k] = "")); renderEstate("estate-checks-new"); };
    $e("fType").onchange = () => { readForm(); C.preview = null; renderEstate("estate-checks-new"); };
    if ($e("fConn")) $e("fConn").onchange = reset("database", "schema_name", "table_name");
    if ($e("fDb")) $e("fDb").onchange = reset("schema_name", "table_name");
    if ($e("fSchema")) $e("fSchema").onchange = reset("table_name");
    if ($e("fTable")) $e("fTable").onchange = redraw;
    if ($e("cMode")) $e("cMode").onchange = redraw;
    for (const side of ["left", "right"]) {
      if ($e(`${side}Conn`)) $e(`${side}Conn`).onchange = () => { readForm(); C.f.config[side].database = ""; renderEstate("estate-checks-new"); };
      if ($e(`${side}Db`)) $e(`${side}Db`).onchange = redraw;
    }
    const out = $e("ckOut");
    $e("ckPreview").onclick = async () => {
      const f = readForm();
      out.innerHTML = "<p class='muted'>Running the check once…</p>";
      try { C.preview = await send("/preview", "POST", f); out.innerHTML = resultBox({ check_type: f.check_type }, C.preview); }
      catch (x) { out.innerHTML = errBox(x); }
    };
    $e("ckSave").onclick = async () => {
      const f = readForm();
      try {
        const r = C.editing ? await send(`/${C.editing}`, "PUT", f) : await send("", "POST", f);
        toast(`${C.editing ? "Saved" : "Added"}: ${LABEL[r.result.status] || r.result.status} · ${r.result.message}`);
        C.open = C.editing || r.check_id; C.editing = null; C.f = null; C.preview = null;
        document.querySelector('#tabs [data-page="estate-checks"]').click();
      } catch (x) { out.innerHTML = errBox(x); }
    };
    $e("ckCancel").onclick = () => { C.f = null; C.preview = null; C.open = C.editing; C.editing = null; document.querySelector('#tabs [data-page="estate-checks"]').click(); };
  }

  window.CHECKS = {
    page: (page) => (page === "estate-checks-new" ? pageForm() : C.open ? pageDetail(C.open) : pageList()),
    navigated(page, fromClick) {                 // a sidebar click starts fresh; goForm() / save keep their state
      if (!fromClick) return;
      if (page === "estate-checks") C.open = null;
      if (page === "estate-checks-new") { C.editing = null; C.f = null; C.preview = null; }
    },
    open(id) { C.open = Number(id) || null; },
    async edit(id) {
      try { goForm((await api(`/${Number(id)}`)).check); }
      catch (x) { toast(x.message); }
    },
  };
})();
