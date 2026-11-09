// Data Estate module: connections, catalog, changes & failures, access, estate assistant.
// Separate from app.js; it only reuses app.js's small helpers (esc, md, tableHtml, objTable, toast).
const est = { user: null, conn: null, db: null, connections: [], privileges: null, catalog: {} };
const $e = (id) => document.getElementById(id);
const fmtBytes = (b) => (b == null ? "" : b > 1e9 ? (b / 1e9).toFixed(1) + " GB" : b > 1e6 ? (b / 1e6).toFixed(1) + " MB" : b > 1e3 ? (b / 1e3).toFixed(0) + " KB" : b + " B");

async function eapi(path, options = {}) {
  const r = await fetch("/api/estate" + path, { credentials: "same-origin", ...options,
    headers: { "Content-Type": "application/json", ...(options.headers || {}) } });
  const body = await r.json().catch(() => ({}));
  if (r.status === 401) { est.user = null; showUserBadge(); throw Object.assign(new Error("please sign in"), { login: true }); }
  if (!r.ok) throw new Error(body.detail || body.error || r.statusText);
  return body;
}
const post = (path, data) => eapi(path, { method: "POST", body: JSON.stringify(data || {}) });
const srcType = () => ((est.connections || []).find((c) => c.connection_id === est.conn) || {}).source_type;

// ------------------------------------------------------------------ login + user badge
function showUserBadge() {
  let b = $e("estUser");
  if (!b) {
    b = document.createElement("span");
    b.id = "estUser";
    b.className = "est-user";
    document.querySelector(".topright").prepend(b);
  }
  b.innerHTML = est.user ? `${esc(est.user.username)} <span class="chip ${est.user.role === "admin" ? "warn" : "grey"}">${est.user.role}</span> <a href="#" id="estLogout">log out</a>` : "";
  const lo = $e("estLogout");
  if (lo) lo.onclick = async (e) => {
    e.preventDefault();
    await fetch("/api/auth/logout", { method: "POST", credentials: "same-origin" });
    est.user = null;
    showUserBadge();
    renderEstate(state.page);
  };
}

function loginForm(el, page) {
  el.innerHTML = `<div class="card login-card"><h3>Sign in to Data Estate</h3>
    <p class="small muted">Sign in to browse connections, catalog, activity, and access settings.</p>
    <div class="field"><input id="estU" type="text" placeholder="username or email address" autocomplete="username"></div>
    <div class="field"><input id="estP" type="password" placeholder="password" autocomplete="current-password"></div>
    <button class="btn" id="estLogin" style="width:100%">Sign in</button><p id="estLoginErr" class="small bad"></p></div>`;
  const field = (id) => el.querySelector("#" + id);
  const submit = async () => {
    const button = field("estLogin");
    button.disabled = true;
    try {
      const response = await fetch("/api/auth/login", {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email: field("estU").value.trim(), password: field("estP").value }),
      });
      const body = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(body.detail || "Sign in failed.");
      est.user = body.user;
      showUserBadge();
      renderEstate(page);
    } catch (error) {
      field("estLoginErr").textContent = error.message;
    } finally {
      button.disabled = false;
    }
  };
  field("estLogin").onclick = submit;
  field("estP").onkeydown = (event) => { if (event.key === "Enter") submit(); };
}

// ------------------------------------------------------------------ connection / database picker (top of every estate page)
async function contextBar(page) {
  if (!est.connections.length) est.connections = await eapi("/connections");
  if (!est.conn && est.connections.length) est.conn = est.connections[0].connection_id;
  // re-read the source every 30 s (and on "Refresh"), so databases created in SSMS / pgAdmin show up without a restart
  if (!est.overview || est.overview.cid !== est.conn || Date.now() - (est.overviewAt || 0) > 30000) {
    est.overview = { cid: est.conn, ...(await eapi(`/connections/${est.conn}/overview`)) };
    est.overviewAt = Date.now();
    const own = est.connections.find((c) => c.connection_id === est.conn);
    est.db = (est.overview.databases || []).some((d) => d.name === est.db) ? est.db : own ? own.database : null;
    est.catalog = {};
  }
  const conns = est.connections.map((c) => `<option value="${c.connection_id}" ${c.connection_id === est.conn ? "selected" : ""}>${esc(c.name)}</option>`).join("");
  const dbs = (est.overview.databases || []).filter((d) => d.can_connect).map((d) => `<option ${d.name === est.db ? "selected" : ""}>${esc(d.name)}</option>`).join("");
  return `<div class="ctx"><label>Source <select id="ctxConn_${page}">${conns}</select></label>
          <label>Database <select id="ctxDb_${page}">${dbs}</select></label>
          ${est.overview.ok ? `<span class="chip ok">connected</span>` : `<span class="chip bad">not reachable</span>`}
          <button class="btn light btn-sm" id="ctxRefresh_${page}" title="Read the database list again">↻ Refresh</button></div>`;
}
function bindContext(page) {
  const c = $e("ctxConn_" + page), d = $e("ctxDb_" + page);
  if (c) c.onchange = () => { est.conn = Number(c.value); est.overview = null; renderEstate(page); };
  if (d) d.onchange = () => { est.db = d.value; est.catalog = {}; renderEstate(page); };
  const r = $e("ctxRefresh_" + page);
  if (r) r.onclick = () => { est.overview = null; renderEstate(page); };
}

// ------------------------------------------------------------------ router
async function renderEstate(page) {
  const el = $e("body-" + page);
  if (!el) return;
  try {
    if (!est.user) {
      est.user = (await eapi("/session")).user;
      showUserBadge();
      if (!est.user) return loginForm(el, page);
    }
    el.innerHTML = "<p class='muted'>Loading…</p>";
    if (page === "estate-people" || page.startsWith("estate-dq-")) {   // Connect / data quality: no source to pick
      const body = await (page === "estate-people" ? window.Connect.page() : window.DQ.page(page));
      el.innerHTML = body.html;
      body.bind();
      return;
    }
    const bar = await contextBar(page);
    if (est.overview.limited && page !== "estate-connections") {
      el.innerHTML = bar + `<div class="card"><h3>${esc(est.overview.label)} source</h3>
        <p>This source is ${est.overview.ok ? "connected" : "added but not reachable"}. Browsing its catalog, changes and access arrives with the
        ${esc(est.overview.label)} connector, the next build step. PostgreSQL sources can be browsed today.</p></div>`;
      bindContext(page);
      return;
    }
    const body = await ({ "estate-connections": pageConnections, "estate-catalog": pageCatalog, "estate-activity": pageActivity,
      "estate-access": pageAccess, "estate-ask": pageAsk })[page]();
    el.innerHTML = bar + body.html;
    bindContext(page);
    if (body.bind) body.bind();
  } catch (e) {
    if (e.login) return loginForm(el, page);
    el.innerHTML = `<div class="error-box">${esc(e.message)}</div>`;
  }
}
document.querySelectorAll("#tabs button").forEach((b) => b.addEventListener("click", () => {
  if (b.dataset.page.startsWith("estate-")) renderEstate(b.dataset.page);
}));

// ------------------------------------------------------------------ 1. connections
async function pageConnections() {
  const o = est.overview, admin = est.user.role === "admin";
  if (!est.types) est.types = await eapi("/source-types");
  const typeLabel = (t) => (est.types[t] ? est.types[t].label : t);
  const rows = (o.databases || []).map((d) => `<tr><td>${esc(d.name)}</td><td>${esc(d.owner)}</td><td class="num">${fmtBytes(d.size_bytes)}</td>
      <td>${d.can_connect ? "yes" : "no"}</td>
      <td>${d.tracking ? `<span class="chip ok">tracking on</span>` : d.tracking === false ? (admin ? `<button class="btn light btn-sm" data-track="${esc(d.name)}">Enable tracking</button>` : `<span class="chip grey">off</span>`) : ""}</td></tr>`).join("");
  const list = est.connections.map((c) => `<tr><td>${esc(c.name)}</td><td><span class="chip grey">${esc(typeLabel(c.source_type))}</span></td>
      <td>${c.username ? esc(c.username) : "Windows login"}@${esc(c.host)}${c.source_type === "snowflake" || !c.port ? "" : ":" + c.port}${c.database ? "/" + esc(c.database) : ""}</td>
      <td>${admin ? esc(c.password_env || "") : "•••"}</td><td>${esc(c.created_by || "")}</td>
      <td>${admin && est.connections.length > 1 ? `<button class="btn light btn-sm" data-del="${c.connection_id}">Remove</button>` : ""}</td></tr>`).join("");
  const thisSource = o.limited
    ? `<p>${o.ok ? `<span class="chip ok">connected</span> ${esc(o.version || "")}<br><span class="small muted">as <b>${esc(o.login_user || "")}</b></span>` : `<span class="bad">${esc(o.error)}</span>`}</p>
       <p class="small muted">Browsing ${esc(o.label)} (catalog, changes, access) arrives with its connector, the next build step.</p>`
    : `${o.ok ? `<p>${esc(o.version)}<br><span class="small muted">connected as <b>${esc(o.login_user)}</b></span></p>` : `<p class="bad">${esc(o.error)}</p>`}
       <div class="tablewrap"><table><thead><tr><th>Database</th><th>Owner</th><th>Size</th><th>Can connect</th><th>Who-changed-what</th></tr></thead><tbody>${rows}</tbody></table></div>
       <p class="small muted">${o.tracking_note ? esc(o.tracking_note) : "Tracking installs schema <code>dt_audit</code> and two event triggers in that database, so every CREATE / ALTER / DROP / GRANT is logged with user and time. Needs a superuser login."}</p>`;
  return {
    html: `<div class="grid g-2">
      <div class="card"><h3>This source</h3>${thisSource}</div>
      <div class="card"><h3>Connected sources</h3><div class="tablewrap"><table><thead><tr><th>Name</th><th>Type</th><th>Login</th><th>Password from .env</th><th>Added by</th><th></th></tr></thead><tbody>${list}</tbody></table></div>
        ${admin ? `<button class="btn" id="wzOpen" style="margin-top:14px">+ Add a source</button>` : ""}</div></div>
      ${admin ? `<div class="card hidden" id="wizard"></div>` : ""}`,
    bind() {
      document.querySelectorAll("[data-track]").forEach((b) => (b.onclick = async () => {
        b.disabled = true;
        try { await post(`/connections/${est.conn}/tracking/${encodeURIComponent(b.dataset.track)}`); toast("Change tracking enabled"); est.overview = null; renderEstate("estate-connections"); }
        catch (e) { toast(e.message); b.disabled = false; }
      }));
      document.querySelectorAll("[data-del]").forEach((b) => (b.onclick = async () => {
        if (!confirm("Remove this source from the portal? (nothing is changed on the source itself)")) return;
        await eapi(`/connections/${b.dataset.del}`, { method: "DELETE" });
        est.connections = []; est.conn = null; est.overview = null; renderEstate("estate-connections");
      }));
      const open = $e("wzOpen");
      if (open) open.onclick = () => { wizardStep1(); $e("wizard").scrollIntoView({ behavior: "smooth" }); };
    },
  };
}

// ---- "Add a source" wizard: 1 pick the type -> 2 details -> 3 test -> 4 save
const TYPE_NOTES = { postgresql: "Databases, schemas, tables, views, procedures", snowflake: "Cloud data warehouse",
  sqlserver: "Microsoft SQL Server / Azure SQL (the database behind SSMS)", saphana: "SAP HANA / HANA Cloud" };
function wizardSteps(n) {
  return `<div class="steps">${["Choose source", "Connection details", "Test", "Save"].map((t, i) =>
    `<span class="step ${i + 1 === n ? "now" : i + 1 < n ? "done" : ""}">${i + 1}. ${t}</span>`).join("")}</div>`;
}
function wizardStep1() {
  const w = $e("wizard");
  w.classList.remove("hidden");
  w.innerHTML = `<h3>Add a source</h3>${wizardSteps(1)}<div class="tiles">${Object.entries(est.types).map(([k, t]) =>
    `<button class="tile" data-type="${k}"><b>${esc(t.label)}</b><span class="small muted">${esc(TYPE_NOTES[k] || "")}</span>
      <span class="chip ${t.browse ? "ok" : "grey"}">${t.browse ? "browse + access" : "connect + test"}</span></button>`).join("")}</div>
    <p class="small muted">More types (Oracle, MySQL, Databricks…) can be added the same way.</p>
    <button class="btn light" id="wzCancel">Cancel</button>`;
  w.querySelectorAll(".tile").forEach((b) => (b.onclick = () => wizardStep2(b.dataset.type)));
  $e("wzCancel").onclick = () => w.classList.add("hidden");
}
function wizardStep2(type) {
  const t = est.types[type], w = $e("wizard");
  const envName = "CONN_" + t.label.toUpperCase().replace(/[^A-Z0-9]+/g, "_") + "_PASSWORD";
  w.innerHTML = `<h3>Add a ${esc(t.label)} source</h3>${wizardSteps(2)}
    <div class="form-grid">
      <label>Name shown in the portal<input id="wzName" placeholder="e.g. Finance ${esc(t.label)}"></label>
      ${t.fields.map((f) => `<label>${esc(f.label)}${f.required ? " *" : ""}<input data-key="${f.key}" value="${esc(f.default || "")}" placeholder="${esc(f.placeholder || "")}"></label>`).join("")}
      <label>Password variable (.env or cloud secret) *<input id="wzPwEnv" value="${envName}"></label>
    </div>
    <p class="small muted">Laptop: put the password in <code>.env</code> as <code>${envName}=...</code> (no restart needed). Cloud: DevOps adds it as a secret
      environment variable with that name. The portal stores only the variable name, never the password.
      Driver: <code>${esc(t.driver)}</code></p>
    <div id="wzResult"></div>
    <button class="btn light" id="wzBack">Back</button> <button class="btn" id="wzTest">Test connection</button> <button class="btn" id="wzSave" disabled>Save source</button>`;
  const collect = () => {
    const body = { source_type: type, name: $e("wzName").value.trim() || t.label, password_env: $e("wzPwEnv").value.trim(), settings: {} };
    w.querySelectorAll("input[data-key]").forEach((i) => {
      const k = i.dataset.key, v = i.value.trim();
      if (["host", "port", "database", "username"].includes(k)) body[k] = k === "port" ? (v ? Number(v) : null) : v;
      else body.settings[k] = v;
    });
    const missing = t.fields.filter((f) => f.required && !w.querySelector(`input[data-key=${f.key}]`).value.trim()).map((f) => f.label);
    if (!body.password_env) missing.push(".env variable");
    return { body, missing };
  };
  $e("wzBack").onclick = wizardStep1;
  $e("wzTest").onclick = async () => {
    const { body, missing } = collect();
    if (missing.length) { $e("wzResult").innerHTML = `<div class="error-box">Please fill in: ${missing.map(esc).join(", ")}</div>`; return; }
    w.querySelector(".steps").outerHTML = wizardSteps(3);
    $e("wzResult").innerHTML = "<p class='muted'>Connecting…</p>";
    const r = await post("/connections/test", body).catch((e) => ({ ok: false, error: e.message }));
    $e("wzResult").innerHTML = r.ok
      ? `<div class="banner ok"><h2>Connection works</h2>${esc(r.version || "")} · logged in as <b>${esc(r.login_user || "")}</b>${r.database ? " · database " + esc(r.database) : ""}</div>`
      : `<div class="error-box">Could not connect: ${esc(r.error)}</div>`;
    $e("wzSave").disabled = false;
    $e("wzSave").textContent = r.ok ? "Save source" : "Save anyway (fix it later)";
  };
  $e("wzSave").onclick = async () => {
    const { body, missing } = collect();
    if (missing.length) return;
    try {
      const r = await post("/connections", body);
      w.querySelector(".steps").outerHTML = wizardSteps(4);
      toast(`${t.label} source saved` + (r.overview.ok ? "" : " (not reachable yet)"));
      est.connections = []; est.conn = r.connection_id; est.overview = null;
      renderEstate("estate-connections");
    } catch (e) { $e("wzResult").innerHTML = `<div class="error-box">${esc(e.message)}</div>`; }
  };
}

// ------------------------------------------------------------------ 2. catalog
async function pageCatalog() {
  const schemas = await eapi(`/connections/${est.conn}/db/${encodeURIComponent(est.db)}/schemas`);
  const tree = schemas.map((s) => `<div class="tree-schema"><button class="tree-toggle" data-schema="${esc(s.name)}">▸ ${esc(s.name)}
      <span class="small muted">${s.tables} tbl · ${s.views} view · ${s.routines} proc</span></button><div class="tree-items" id="tree-${esc(s.name)}"></div></div>`).join("");
  return {
    html: `<div class="catalog"><div class="card tree"><input id="catSearch" placeholder="Search tables, views, columns…">
        <div id="catResults"></div><div id="catTree">${tree}</div></div>
      <div class="card" id="catDetail"><p class="muted">Pick a table, view, function or procedure on the left.</p></div></div>`,
    bind() {
      document.querySelectorAll(".tree-toggle").forEach((b) => (b.onclick = () => toggleSchema(b)));
      let t;
      $e("catSearch").oninput = (e) => {
        clearTimeout(t);
        const text = e.target.value.trim();
        t = setTimeout(async () => {
          if (text.length < 2) { $e("catResults").innerHTML = ""; return; }
          const hits = await eapi(`/connections/${est.conn}/db/${encodeURIComponent(est.db)}/search?text=${encodeURIComponent(text)}`);
          $e("catResults").innerHTML = hits.map((h) => `<button class="tree-item" data-s="${esc(h.schema)}" data-n="${esc(h.name.split("  (")[0])}" data-t="${h.type === "COLUMN" ? "TABLE" : esc(h.type)}">${esc(h.schema)}.${esc(h.name)} <span class="small muted">${esc(h.type.toLowerCase())}</span></button>`).join("") || "<p class='small muted'>No match</p>";
          bindItems($e("catResults"));
        }, 300);
      };
    },
  };
}
async function toggleSchema(b) {
  const s = b.dataset.schema, box = $e("tree-" + s);
  if (box.innerHTML) { box.innerHTML = ""; b.firstChild.textContent = "▸ " + s; return; }
  b.firstChild.textContent = "▾ " + s;
  const objs = await eapi(`/connections/${est.conn}/db/${encodeURIComponent(est.db)}/schemas/${encodeURIComponent(s)}/objects`);
  box.innerHTML = objs.map((o) => `<button class="tree-item" data-s="${esc(s)}" data-n="${esc(o.name)}" data-t="${esc(o.type)}"><span class="otype">${esc(o.type.replace("MATERIALIZED VIEW", "MVIEW").slice(0, 5))}</span>${esc(o.name)}</button>`).join("") || "<p class='small muted'>empty</p>";
  bindItems(box);
}
function bindItems(box) {
  box.querySelectorAll(".tree-item").forEach((i) => (i.onclick = () => {
    document.querySelectorAll(".tree-item.active").forEach((x) => x.classList.remove("active"));
    i.classList.add("active");
    showObject(i.dataset.s, i.dataset.n, i.dataset.t);
  }));
}
async function showObject(schema, name, type) {
  const el = $e("catDetail");
  el.innerHTML = "<p class='muted'>Loading…</p>";
  try {
    const d = await eapi(`/connections/${est.conn}/db/${encodeURIComponent(est.db)}/object?schema=${encodeURIComponent(schema)}&name=${encodeURIComponent(name)}&type=${encodeURIComponent(type)}`);
    const sec = (title, inner) => (inner ? `<h3 style="margin-top:18px">${title}</h3>${inner}` : "");
    el.innerHTML = `<div class="obj-head"><span class="chip grey">${esc(d.type)}</span><h2>${esc(d.name)}</h2>
        ${srcType() === "postgresql" && ["TABLE", "VIEW", "MATERIALIZED VIEW"].includes(d.type) ? `<button class="btn light btn-sm obj-contact" id="blastBtn">What breaks if this changes?</button>` : ""}
        <button class="btn light btn-sm ${srcType() === "postgresql" ? "" : "obj-contact"}" data-contact="${esc(d.name)}">Contact owner</button></div>
      <div id="blastBox"></div>
      <p class="small muted">owner <b>${esc(d.owner)}</b>${d.row_count != null ? ` · ${num(d.row_count)} rows` : ""}${d.size_bytes != null ? ` · ${fmtBytes(d.size_bytes)}` : ""}${d.language ? ` · ${esc(d.language)}` : ""}${d.returns ? ` · returns ${esc(d.returns)}` : ""}${d.comment ? ` · ${esc(d.comment)}` : ""}</p>
      ${sec("Columns", objTable(d.columns))}
      ${sec("Indexes", objTable(d.indexes))}${sec("Constraints", objTable(d.constraints))}
      ${d.uses.length || d.used_by.length ? sec("Lineage", `<p>${d.uses.length ? `Reads from: ${d.uses.map((u) => `<span class="chip grey">${esc(u)}</span>`).join(" ")}` : ""}
          ${d.used_by.length ? `<br>Used by: ${d.used_by.map((u) => `<span class="chip warn">${esc(u)}</span>`).join(" ")}` : ""}</p>`) : ""}
      ${sec("Who has access", objTable(d.grants))}
      ${sec("Definition", d.definition ? `<pre class="code">${esc(d.definition)}</pre>` : "")}
      ${sec("Recent changes", d.changes.length ? objTable(d.changes, ["event_time", "username", "command_tag", "client_addr"]) : "<p class='small muted'>None recorded (or change tracking is off for this database).</p>")}`;
    const bb = $e("blastBtn");
    if (bb) bb.onclick = async () => {
      const box = $e("blastBox");
      box.innerHTML = "<p class='muted small'>Working out the blast radius…</p>";
      try {
        const r = await eapi(`/connections/${est.conn}/db/${encodeURIComponent(est.db)}/blast?schema=${encodeURIComponent(schema)}&name=${encodeURIComponent(name)}`);
        box.innerHTML = `<div class="blast ${r.downstream.length ? "bad" : "ok"}"><h3>${r.downstream.length ? `${r.downstream.length} thing(s) depend on ${esc(r.object)}` : `Nothing depends on ${esc(r.object)}`}</h3>
          ${r.downstream.length ? `<ul>${r.downstream.map((x) => `<li><b>${esc(x.object)}</b> <span class="chip grey">${esc(x.kind)}</span> <span class="small muted">${esc(x.how)}</span></li>`).join("")}</ul>` : ""}
          <p class="small">${r.owners.length ? `Owners to inform: ${r.owners.map((o) => `<b>${esc(o.name)}</b>`).join(", ")} <button class="btn light btn-sm" data-contact="${esc(r.object + " (planned change)")}">Contact owners</button>`
            : "No owner recorded in Connect for these objects."}</p>
          <p class="small muted">Dropping, renaming or changing a column of this object breaks everything listed. The schema-change guard raises an incident automatically if it happens.</p></div>`;
      } catch (e) { box.innerHTML = `<div class="error-box">${esc(e.message)}</div>`; }
    };
  } catch (e) { el.innerHTML = `<div class="error-box">${esc(e.message)}</div>`; }
}

// ------------------------------------------------------------------ 3. changes & failures
async function pageActivity() {
  const base = `/connections/${est.conn}/db/${encodeURIComponent(est.db)}`;
  const [ch, f] = await Promise.all([eapi(base + "/changes"), eapi(base + "/failures")]);
  const h = f.health || {};
  return {
    html: `<div class="kpis" style="grid-template-columns:repeat(4,1fr)">
        <div class="kpi"><div class="l">Changes logged</div><div class="v">${ch.tracking ? num(ch.changes.length) : "off"}</div></div>
        <div class="kpi ${f.pipeline.length ? "bad" : ""}"><div class="l">Pipeline failures</div><div class="v">${num(f.pipeline.length)}</div></div>
        ${h.rollbacks == null && h.lock_timeouts != null
          ? `<div class="kpi ${h.lock_timeouts ? "warn" : ""}"><div class="l">Lock timeouts</div><div class="v">${num(h.lock_timeouts)}</div></div>`
          : `<div class="kpi"><div class="l">Rollbacks</div><div class="v">${num(h.rollbacks)}</div></div>`}
        <div class="kpi ${h.deadlocks ? "bad" : ""}"><div class="l">Deadlocks</div><div class="v">${num(h.deadlocks)}</div></div></div>
      <div class="card"><h3>Who changed what</h3>
        ${ch.tracking ? `<div class="field"><input id="chObj" placeholder="filter: object, e.g. silver.payments"><input id="chUser" placeholder="filter: user"><button class="btn light" id="chGo">Filter</button></div>
        <div id="chTable">${objTable(ch.changes, ["event_time", "username", "command_tag", "object_type", "object_identity", "client_addr", "application"]) || "<p class='muted'>No changes recorded yet.</p>"}</div>`
        : `<p>Change tracking is off for <b>${esc(est.db)}</b>. An admin can enable it on the <b>Connections</b> page.</p>`}</div>
      <div class="card"><h3>What failed</h3>${objTable(f.pipeline) || "<p class='muted'>No failures recorded in this database.</p>"}
        <p class="small muted">Source: ${f.note ? esc(f.note) : "DataTrust pipeline log (audit.error_log) when the database has it, plus PostgreSQL counters"} (counters since ${esc(h.stats_reset || "server start")}).</p></div>
      ${f.jobs && f.jobs.length ? `<div class="card"><h3>Failed SQL Agent jobs</h3>${objTable(f.jobs)}</div>` : ""}
      ${f.warnings && f.warnings.length ? `<div class="card"><h3>Data quality warnings</h3>${objTable(f.warnings)}</div>` : ""}`,
    bind() {
      const g = $e("chGo");
      if (g) g.onclick = async () => {
        const r = await eapi(`${base}/changes?object_name=${encodeURIComponent($e("chObj").value)}&username=${encodeURIComponent($e("chUser").value)}`);
        $e("chTable").innerHTML = objTable(r.changes, ["event_time", "username", "command_tag", "object_type", "object_identity", "client_addr", "application"]) || "<p class='muted'>No match.</p>";
      };
    },
  };
}

// ------------------------------------------------------------------ 4. access
const REQ_LABEL = { GRANT: "Grant", REVOKE: "Revoke", CREATE_USER: "Add team member", CREATE_ROLE: "Create role",
  GRANT_ROLE: "Add to role", REVOKE_ROLE: "Remove from role", DROP_PRINCIPAL: "Remove user / role" };
const ASK_EXAMPLES = (mssql) => mssql
  ? ["Give test_nalyst read access to the gold schema", "Give test_nalyst read access to the whole database", "Create user audit_bot without login",
     "Create role reporting_readers and add test_nalyst", "Remove test_nalyst from role reporting_readers"]
  : ["Give test_nalyst read access to the gold schema", "Give test_nalyst read access to gold.daily_revenue", "Create user audit_bot without login",
     "Create role reporting_readers and add test_nalyst", "Revoke read access on the gold schema from test_nalyst"];
async function pageAccess() {
  const admin = est.user.role === "admin";
  if (!est.privileges || est.privilegesFor !== est.conn) {        // privilege names differ per source type
    est.privileges = await eapi(`/privileges?connection_id=${est.conn}`);
    est.privilegesFor = est.conn;
  }
  const mssql = srcType() === "sqlserver";
  const [roles, pending, history, schemas] = await Promise.all([eapi(`/connections/${est.conn}/roles?database=${encodeURIComponent(est.db || "")}`), eapi("/access-requests?status=PENDING"),
    eapi("/access-requests"), eapi(`/connections/${est.conn}/db/${encodeURIComponent(est.db)}/schemas`)]);
  est.roles = roles;
  const roleRows = roles.map((r) => `<tr class="clickable" data-role="${esc(r.name)}"><td>${esc(r.name)}</td><td>${r.can_login ? "user" : "group role"}</td>
      <td>${r.superuser ? `<span class="chip bad">superuser</span>` : ""}</td><td>${esc((r.member_of || []).join(", "))}</td><td>${esc(r.valid_until || "")}</td></tr>`).join("");
  const reqRow = (r) => `<div class="check"><b>#${r.request_id} ${esc(REQ_LABEL[r.request_type] || r.request_type)} · ${esc(r.grantee)}</b> <span class="small muted">by ${esc(r.requested_by)} at ${esc(String(r.requested_at).slice(0, 16))}${r.reason ? " · " + esc(r.reason) : ""}</span>
      <pre class="code">${esc(r.sql_preview)}</pre>${admin ? `<button class="btn btn-sm" data-approve="${r.request_id}">Approve &amp; apply</button> <button class="btn light btn-sm" data-reject="${r.request_id}">Reject</button>` : `<span class="small muted">waiting for an admin</span>`}</div>`;
  const typeOpts = Object.keys(est.privileges).map((t) => `<option>${t}</option>`).join("");
  const schemaOpts = schemas.map((s) => `<option>${esc(s.name)}</option>`).join("");
  const grantees = (admin ? roles.map((r) => r.name).concat(["PUBLIC"]) : [est.user.username]).map((n) => `<option>${esc(n)}</option>`).join("");
  return {
    html: `<div class="card ask-access"><h3>Describe what you need <span class="chip grey">no SQL needed</span></h3>
        <p class="small muted">Write it in plain words. The portal prepares the request and shows the exact SQL; nothing runs until an admin approves.</p>
        <div class="askform ask-access-form"><input id="aaText" autocomplete="off" placeholder="e.g. Give test_nalyst read access to the gold schema"><button id="aaGo">Prepare request</button></div>
        <div class="sugg">${ASK_EXAMPLES(mssql).map((q) => `<button data-aa="${esc(q)}">${esc(q)}</button>`).join("")}</div>
        <div id="aaResult"></div></div>
      <div class="grid g-2">
      <div class="card"><h3>Users and roles ${mssql ? `in ${esc(est.db)}` : "on this source"}</h3><p class="small muted">Click a row to see what it can access in ${esc(est.db)}.</p>
        <div class="tablewrap"><table><thead><tr><th>Name</th><th>Kind</th><th></th><th>Member of</th><th>Valid until</th></tr></thead><tbody>${roleRows}</tbody></table></div>
        <div id="roleAccess"></div></div>
      <div class="card"><h3>Request access</h3>
        <div class="form-grid">
          <label>Action<select id="arType"><option value="GRANT">Grant</option>${admin ? `<option value="REVOKE">Revoke</option>
            <option value="CREATE_USER">Add team member / user</option><option value="CREATE_ROLE">Create role</option>
            <option value="GRANT_ROLE">Add to a role</option><option value="REVOKE_ROLE">Remove from a role</option>
            <option value="DROP_PRINCIPAL">Remove user / role</option>` : ""}</select></label>
          <label id="lblGrantee">User / role<select id="arGrantee">${grantees}</select></label>
          <label id="lblNewUser" class="hidden"><span id="arNewLbl">New user name</span><input id="arNewUser" placeholder="${mssql ? "e.g. analyst_priya, or DOMAIN\\name for a Windows account" : "e.g. analyst_priya"}"></label>
          <label id="lblLogin" class="hidden">Login<select id="arLogin">${mssql
            ? `<option value="sql">SQL login (password shown once)</option><option value="windows">Windows account (DOMAIN\\name)</option><option value="none">Database user only, no login</option>`
            : `<option value="sql">Login with password (shown once)</option><option value="none">No login (cannot sign in)</option>`}</select></label>
          <label id="lblRole" class="hidden"><span id="arRoleLbl">Role</span><select id="arRole"><option value="">(none)</option>${roles.filter((r) => !r.can_login).map((r) => `<option>${esc(r.name)}</option>`).join("")}</select></label>
          <label id="lblOtype">Object type<select id="arOtype">${typeOpts}</select></label>
          <label id="lblSchema">Schema<select id="arSchema">${schemaOpts}</select></label>
          <label id="lblObject">Object<select id="arObject"></select></label>
        </div>
        <div id="arPrivs" class="privs"></div>
        <div class="field"><input id="arReason" placeholder="Reason (e.g. monthly revenue report)"></div>
        <button class="btn" id="arSubmit">Preview &amp; submit request</button>
        <p class="small muted">Nothing changes on the source until an admin approves. ${mssql
          ? "SQL Server: a person needs a user in this database before anything can be granted (Add team member creates the login and the user)."
          : "Object grants automatically add CONNECT on the database and USAGE on the schema."}</p>
        <div id="arResult"></div></div></div>
      <div class="card"><h3>Pending approval (${pending.length})</h3>${pending.map(reqRow).join("") || "<p class='muted'>Nothing waiting.</p>"}</div>
      <div class="card"><h3>Request history</h3>${objTable(history.map((r) => ({ id: r.request_id, type: REQ_LABEL[r.request_type] || r.request_type, grantee: r.grantee, database: r.database, status: r.status, requested_by: r.requested_by, decided_by: r.decided_by, decided_at: r.decided_at, error: r.error })))
        || "<p class='muted'>No requests yet.</p>"}</div>
      ${admin ? `<div class="card"><h3>Portal users</h3><div id="puList"></div>
        <div class="form-grid"><input id="puName" type="email" placeholder="user email"><input id="puPw" type="password" placeholder="password (min 12)">
          <select id="puRole"><option>viewer</option><option>admin</option></select><button class="btn light" id="puSave">Add / update portal user</button></div>
        <p class="small muted">Portal users sign in with email. Email sign-ups require verification; admin-provisioned accounts are marked verified.</p></div>` : ""}`,
    bind: bindAccess,
  };
}
function bindAccess() {
  const admin = est.user.role === "admin";
  document.querySelectorAll("tr[data-role]").forEach((tr) => (tr.onclick = async () => {
    const a = await eapi(`/connections/${est.conn}/db/${encodeURIComponent(est.db)}/roles/${encodeURIComponent(tr.dataset.role)}/access`);
    $e("roleAccess").innerHTML = `<h3 style="margin-top:16px">${esc(tr.dataset.role)} in ${esc(est.db)}</h3>
      <p>Connect: <b>${a.database.connect ? "yes" : "no"}</b> · create schemas: <b>${a.database.create_schemas ? "yes" : "no"}</b></p>
      ${objTable(a.schemas) || ""}${objTable(a.objects) || "<p class='small muted'>No table or view grants.</p>"}
      ${a.owns.length ? `<p class="small muted">Owns ${a.owns.length} object(s) (owners have every right).</p>` : ""}`;
  }));
  const type = $e("arType"), otype = $e("arOtype"), schema = $e("arSchema");
  const show = (id, on) => $e(id).classList.toggle("hidden", !on);
  let seq = 0, lastType = null;
  const refresh = async () => {
    const t = type.value, ot = otype.value, my = ++seq;
    const objectLevel = ["GRANT", "REVOKE"].includes(t);
    const creating = ["CREATE_USER", "CREATE_ROLE"].includes(t);
    show("lblGrantee", !creating); show("lblNewUser", creating); show("lblLogin", t === "CREATE_USER");
    show("lblRole", ["GRANT_ROLE", "REVOKE_ROLE", "CREATE_USER"].includes(t));
    $e("arNewLbl").textContent = t === "CREATE_ROLE" ? "New role name" : "New user name";
    $e("arRoleLbl").textContent = t === "CREATE_USER" ? "Also add to role (optional)" : "Role";
    if (t !== lastType) {                    // new action: "(none)" for a new user, the first real role otherwise
      $e("arRole").selectedIndex = t === "CREATE_USER" || $e("arRole").options.length < 2 ? 0 : 1;
      lastType = t;
    }
    show("lblOtype", objectLevel); show("lblSchema", objectLevel && ot !== "DATABASE"); show("lblObject", objectLevel && ["TABLE", "VIEW", "FUNCTION", "PROCEDURE"].includes(ot));
    $e("arPrivs").innerHTML = objectLevel ? est.privileges[ot].map((p, i) => `<label><input type="checkbox" value="${p}" ${i === 0 ? "checked" : ""}> ${p}</label>`).join("") : "";
    if (objectLevel && ["TABLE", "VIEW", "FUNCTION", "PROCEDURE"].includes(ot)) {
      const objs = await eapi(`/connections/${est.conn}/db/${encodeURIComponent(est.db)}/schemas/${encodeURIComponent(schema.value)}/objects`);
      if (my !== seq) return;                         // a newer selection is already loading
      const want = { TABLE: ["TABLE"], VIEW: ["VIEW", "MATERIALIZED VIEW"], FUNCTION: ["FUNCTION"], PROCEDURE: ["PROCEDURE"] }[ot];
      $e("arObject").innerHTML = objs.filter((o) => want.includes(o.type)).map((o) => `<option>${esc(o.name)}</option>`).join("") || "<option value=''>(none in this schema)</option>";
    }
  };
  [type, otype, schema].forEach((x) => (x.onchange = refresh));
  refresh();

  // ---- "Describe what you need": plain words -> request + SQL preview -> submit for approval
  const prepare = async (text) => {
    const box = $e("aaResult");
    if (!text.trim()) return;
    box.innerHTML = "<p class='muted'>Preparing the request…</p>";
    try {
      const r = await post("/access-requests/draft", { connection_id: est.conn, database: est.db, text });
      if (!r.ok) {
        box.innerHTML = `<div class="error-box">${esc(r.message)}</div>${(r.examples || []).length ? `<div class="sugg">${r.examples.slice(0, 6).map((q) => `<button data-aa="${esc(q)}">${esc(q)}</button>`).join("")}</div>` : ""}`;
        box.querySelectorAll("[data-aa]").forEach((b) => (b.onclick = () => { $e("aaText").value = b.dataset.aa; prepare(b.dataset.aa); }));
        return;
      }
      const q = r.request;
      box.innerHTML = `<div class="draft"><div class="draft-head"><span class="chip ${q.request_type === "DROP_PRINCIPAL" || q.request_type === "REVOKE" ? "warn" : "ok"}">${esc(REQ_LABEL[q.request_type] || q.request_type)}</span>
          <b>${esc(r.summary)}</b></div>
        <pre class="code">${esc(r.sql_preview)}</pre>
        <p class="small muted">Prepared by the ${r.mode === "built-in" ? "built-in parser" : esc(r.mode) + " AI model"}; every name was checked against ${esc(est.db)}.
          The SQL above is what will run after approval.</p>
        <button class="btn" id="aaSubmit">Submit for approval</button> <button class="btn light" id="aaCancel">Cancel</button></div>`;
      $e("aaCancel").onclick = () => (box.innerHTML = "");
      $e("aaSubmit").onclick = async () => {
        try {
          const s2 = await post("/access-requests", { connection_id: est.conn, database: est.db, request_type: q.request_type,
            grantee: q.grantee, details: q.details, reason: text });
          box.innerHTML = `<div class="banner ok"><h2>Request #${s2.request_id} submitted</h2>${admin ? "Approve it below under <b>Pending approval</b>." : "An admin will review it."}</div>`;
          setTimeout(() => renderEstate("estate-access"), 2500);
        } catch (e) { box.insertAdjacentHTML("beforeend", `<div class="error-box">${esc(e.message)}</div>`); }
      };
    } catch (e) { box.innerHTML = `<div class="error-box">${esc(e.message)}</div>`; }
  };
  $e("aaGo").onclick = () => prepare($e("aaText").value);
  $e("aaText").onkeydown = (e) => { if (e.key === "Enter") prepare($e("aaText").value); };
  document.querySelectorAll(".ask-access .sugg [data-aa]").forEach((b) => (b.onclick = () => { $e("aaText").value = b.dataset.aa; prepare(b.dataset.aa); }));
  $e("arSubmit").onclick = async () => {
    const t = type.value;
    const body = { connection_id: est.conn, database: est.db, request_type: t, reason: $e("arReason").value, details: {},
      grantee: ["CREATE_USER", "CREATE_ROLE"].includes(t) ? $e("arNewUser").value.trim() : $e("arGrantee").value };
    if (t === "CREATE_USER") body.details = { login_type: $e("arLogin").value, member_of: $e("arRole").value ? [$e("arRole").value] : [] };
    if (t === "CREATE_ROLE") body.details = { members: [] };
    if (["GRANT_ROLE", "REVOKE_ROLE"].includes(t)) body.details.role = $e("arRole").value;
    if (["GRANT", "REVOKE"].includes(t)) {
      body.details = { object_type: otype.value, schema: schema.value, object: $e("arObject").value,
        privileges: [...document.querySelectorAll("#arPrivs input:checked")].map((x) => x.value) };
    }
    try {
      const r = await post("/access-requests", body);
      $e("arResult").innerHTML = `<div class="banner ok"><h2>Request #${r.request_id} submitted</h2>It will run these statements after approval:<pre class="code">${esc(r.sql_preview)}</pre></div>`;
      setTimeout(() => renderEstate("estate-access"), 2500);
    } catch (e) { $e("arResult").innerHTML = `<div class="error-box">${esc(e.message)}</div>`; }
  };
  const decide = async (id, d) => {
    if (d === "approve" && !confirm(`Approve request #${id}? The statements run on the source now.`)) return;   // shown for every approval
    try {
      const r = await post(`/access-requests/${id}/${d}`);
      if (r.password) {
        alert(`Request #${id} ${r.status}.\n\nTemporary password for the new user (shown only once, copy it now):\n\n${r.password}`);
      } else toast(`Request #${id}: ${r.status}${r.error ? " — " + r.error : ""}`);
      renderEstate("estate-access");
    } catch (e) { toast(e.message); }
  };
  document.querySelectorAll("[data-approve]").forEach((b) => (b.onclick = () => decide(b.dataset.approve, "approve")));
  document.querySelectorAll("[data-reject]").forEach((b) => (b.onclick = () => decide(b.dataset.reject, "reject")));
  if (admin) {
    eapi("/portal-users").then((u) => ($e("puList").innerHTML = objTable(u, ["username", "role", "active", "created_by", "created_at"])));
    $e("puSave").onclick = async () => {
      try { await post("/portal-users", { username: $e("puName").value, password: $e("puPw").value, role: $e("puRole").value }); toast("Portal user saved"); renderEstate("estate-access"); }
      catch (e) { toast(e.message); }
    };
  }
}

// ------------------------------------------------------------------ 5. estate assistant
const ESTATE_EXAMPLES = ["List databases", "Tables in schema silver", "Describe silver.payments", "Recent changes", "Who changed silver.customers",
  "What failed", "Who has access to gold.daily_revenue", "List users", "Pending access requests"];
async function pageAsk() {
  return {
    html: `<div class="ask-layout"><div class="card chat"><div id="estChat" class="chatlog">${est.chatHtml || ""}</div>
        <form id="estForm" class="askform"><input id="estQ" autocomplete="off" placeholder="e.g. who changed silver.customers?"><button type="submit">Ask</button></form></div>
      <div class="card"><h3>Try asking</h3><div class="examples">${ESTATE_EXAMPLES.map((q) => `<button>${esc(q)}</button>`).join("")}</div>
        <p class="small muted">Answers come live from the selected source and database.</p></div></div>`,
    bind() {
      const ask = async (q) => {
        const log = $e("estChat");
        log.insertAdjacentHTML("beforeend", `<div class="msg user"><div class="bubble">${esc(q)}</div></div><div class="msg bot typing" id="estTyping">checking the source…</div>`);
        log.scrollTop = log.scrollHeight;
        try {
          const r = await post("/ask", { connection_id: est.conn, database: est.db, question: q });
          $e("estTyping").remove();
          log.insertAdjacentHTML("beforeend", `<div class="msg bot"><div class="bubble">${md(r.answer)}${tableHtml(r.table, 200)}
            ${r.sources.length ? `<div class="meta">Source: ${r.sources.map(esc).join(", ")} · ${esc(est.db)}</div>` : ""}</div></div>`);
        } catch (e) { $e("estTyping").remove(); log.insertAdjacentHTML("beforeend", `<div class="msg bot"><div class="bubble">${esc(e.message)}</div></div>`); }
        est.chatHtml = log.innerHTML;
        log.scrollTop = log.scrollHeight;
      };
      $e("estForm").onsubmit = (e) => { e.preventDefault(); const q = $e("estQ").value.trim(); if (q) { $e("estQ").value = ""; ask(q); } };
      document.querySelectorAll("#body-estate-ask .examples button").forEach((b) => (b.onclick = () => ask(b.textContent)));
    },
  };
}

// show who is logged in (if a session cookie exists) as soon as the page opens
eapi("/session").then((s) => { est.user = s.user; showUserBadge(); }).catch(() => {});
