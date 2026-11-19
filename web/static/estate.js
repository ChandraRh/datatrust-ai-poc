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
  if (!r.ok) throw new Error(errorText(body) || r.statusText);
  return body;
}
// the server's reason in plain words; a form check (422) comes as a list: "password: String should have at least 12 characters"
function errorText(body) {
  const d = body.detail;
  if (Array.isArray(d)) return d.map((x) => `${(x.loc || []).filter((p) => p !== "body").join(".") || "input"}: ${x.msg || "not valid"}`).join("; ");
  return typeof d === "string" ? d : body.error || (d ? JSON.stringify(d) : "");
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
    await fetch("/api/auth/logout", { method: "POST", credentials: "same-origin" }).catch(() => {});
    location.replace("/login");                 // leave the dashboard; replace() so Back does not return to it
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

// ------------------------------------------------------------------ the source of every estate page
// One source at a time, like every other page: Connections (details), Catalog and Access show the source picked at the
// top, and its own database (the POC is datatrust_poc, not every database on its server). No second picker here.
function scopeConnId() { const m = /^db:(\d+)$/.exec(state.scope || ""); return m ? Number(m[1]) : null; }
async function contextBar(page) {
  if (!est.connections.length) est.connections = await eapi("/connections");
  const conn = est.connections.find((c) => c.connection_id === scopeConnId());
  if (!conn) { est.conn = null; est.overview = null; return { none: true, html: "" }; }
  if (est.conn !== conn.connection_id) { est.conn = conn.connection_id; est.overview = null; est.catalog = {}; est.privileges = null; }
  // re-read the source every 30 s (and on "Refresh"), so databases created in SSMS / pgAdmin show up without a restart
  if (!est.overview || Date.now() - (est.overviewAt || 0) > 30000) {
    est.overview = { cid: est.conn, ...(await eapi(`/connections/${est.conn}/overview`)) };
    est.overviewAt = Date.now();
  }
  const usable = (est.overview.databases || []).filter((d) => d.can_connect).map((d) => d.name);
  const db = conn.database || usable[0] || null;                  // a login saved without a database: its first one
  if (db !== est.db) { est.db = db; est.catalog = {}; }
  return { none: false, html: `<div class="ctx"><span><b>${esc(conn.name)}</b>${est.db ? ` · database <code>${esc(est.db)}</code>` : ""}
      <span class="small muted">· the source picked at the top of the page</span></span>
      ${est.overview.ok ? `<span class="chip ok">connected</span>` : `<span class="chip bad">not reachable</span>`}
      <button class="btn light btn-sm" id="ctxRefresh_${page}" title="Read this source again">↻ Refresh</button></div>` };
}
function bindContext(page) {
  const r = $e("ctxRefresh_" + page);
  if (r) r.onclick = () => { est.overview = null; est.connections = []; renderEstate(page); };
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
    if (page === "estate-users") { const body = await pageUsers(); el.innerHTML = body.html; body.bind(); return; }
    if (page === "estate-activity") { const body = await pageActivity(); el.innerHTML = body.html; body.bind(); return; }
    if (page === "estate-people" || page === "estate-history" || page.startsWith("estate-dq-") || page.startsWith("estate-adf-") || page.startsWith("estate-checks")) {   // no source to pick
      const body = await (page === "estate-people" ? window.Connect.page() : page === "estate-history" ? window.HISTORY.page()
        : page.startsWith("estate-adf-") ? window.ADF.page(page)
        : page.startsWith("estate-checks") ? window.CHECKS.page(page) : window.DQ.page(page));
      el.innerHTML = body.html;
      body.bind();
      return;
    }
    const ctx = await contextBar(page), bar = ctx.html;
    if (ctx.none && page !== "estate-connections") {
      el.innerHTML = `<div class="card"><h3>Pick a database at the top</h3><p class="muted">${page === "estate-catalog" ? "The catalog" : "Access"} shows one database source at a time:
        the one picked in <b>Source</b> at the top of the page. ${scopeConnId() ? "That source is not a connection any more." : "A Data Factory has no catalog or logins of its own here; see <a href=\"#estate-adf-overview\">ADF pipelines</a>."}</p></div>`;
      return;
    }
    if (!ctx.none && est.overview.limited && page !== "estate-connections") {
      el.innerHTML = bar + `<div class="card"><h3>${esc(est.overview.label)} source</h3>
        <p>This source is ${est.overview.ok ? "connected" : "added but not reachable"}. Browsing its catalog, changes and access arrives with the
        ${esc(est.overview.label)} connector, the next build step. PostgreSQL sources can be browsed today.</p></div>`;
      bindContext(page);
      return;
    }
    const body = await ({ "estate-connections": pageConnections, "estate-catalog": pageCatalog,
      "estate-access": pageAccess, "estate-ask": pageAsk })[page]();
    el.innerHTML = bar + body.html;
    bindContext(page);
    if (body.bind) body.bind();
  } catch (e) {
    if (e.login) return loginForm(el, page);
    el.innerHTML = `<div class="error-box">${esc(e.message)}</div>`;
  }
}
document.querySelectorAll("#tabs button").forEach((b) => b.addEventListener("click", (ev) => {
  if (b.dataset.page.startsWith("estate-adf-") && window.ADF) window.ADF.navigated(b.dataset.page);
  if (b.dataset.page.startsWith("estate-checks") && window.CHECKS) window.CHECKS.navigated(b.dataset.page, ev.isTrusted);
  if (b.dataset.page.startsWith("estate-")) renderEstate(b.dataset.page);
}));

// ------------------------------------------------------------------ 1. connections
async function pageConnections() {
  const o = est.overview, admin = est.user.role === "admin";
  if (!est.types) est.types = await eapi("/source-types");
  const typeLabel = (t) => (est.types[t] ? est.types[t].label : t);
  const rows = ((o && o.databases) || []).map((d) => `<tr><td>${esc(d.name)}</td><td>${esc(d.owner)}</td><td class="num">${fmtBytes(d.size_bytes)}</td>
      <td>${d.can_connect ? "yes" : "no"}</td>
      <td>${d.tracking ? `<span class="chip ok">tracking on</span>` : d.tracking === false ? (admin ? `<button class="btn light btn-sm" data-track="${esc(d.name)}">Enable tracking</button>` : `<span class="chip grey">off</span>`) : ""}</td></tr>`).join("");
  const list = est.connections.map((c) => `<tr><td>${esc(c.name)}</td><td><span class="chip grey">${esc(typeLabel(c.source_type))}</span></td>
      <td>${c.username ? esc(c.username) : "Windows login"}@${esc(c.host)}${c.source_type === "snowflake" || !c.port ? "" : ":" + c.port}${c.database ? "/" + esc(c.database) : ""}</td>
      <td title="Hidden">••••••••</td><td>${esc(c.created_by || "")}</td>
      <td class="nowrap">${admin ? `<button class="btn light btn-sm" data-edit="${c.connection_id}">Edit</button>` : ""}
        ${admin && est.connections.length > 1 ? `<button class="btn light btn-sm" data-del="${c.connection_id}">Remove</button>` : ""}</td></tr>`).join("");
  const thisSource = !o ? `<p class="muted">Pick a database in <b>Source</b> at the top to see its databases and turn on change tracking.</p>`
    : o.limited
    ? `<p>${o.ok ? `<span class="chip ok">connected</span> ${esc(o.version || "")}<br><span class="small muted">as <b>${esc(o.login_user || "")}</b></span>` : `<span class="bad">${esc(o.error)}</span>`}</p>
       <p class="small muted">Browsing ${esc(o.label)} (catalog, changes, access) arrives with its connector, the next build step.</p>`
    : `${o.ok ? `<p>${esc(o.version)}<br><span class="small muted">connected as <b>${esc(o.login_user)}</b></span></p>` : `<p class="bad">${esc(o.error)}</p>`}
       <div class="tablewrap"><table><thead><tr><th>Database</th><th>Owner</th><th>Size</th><th>Can connect</th><th>Who-changed-what</th></tr></thead><tbody>${rows}</tbody></table></div>
       <p class="small muted">${o.tracking_note ? esc(o.tracking_note) : "Tracking installs schema <code>dt_audit</code> and two event triggers in that database, so every CREATE / ALTER / DROP / GRANT is logged with user and time. Needs a superuser login."}</p>`;
  return {
    html: `<div class="card"><h3>Connected sources</h3><div class="tablewrap"><table><thead><tr><th>Name</th><th>Type</th><th>Login</th><th>Password</th><th>Added by</th><th></th></tr></thead><tbody>${list}</tbody></table></div>
        ${admin ? `<button class="btn" id="wzOpen" style="margin-top:14px">+ Add a source</button>` : ""}</div>
      <div class="card"><h3>${o ? esc((est.connections.find((c) => c.connection_id === est.conn) || {}).name || "This source") : "This source"}</h3>${thisSource}</div>
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
        if (typeof initScopes === "function") initScopes();            // and leaves the Source picker at the top
        est.connections = []; est.conn = null; est.overview = null; renderEstate("estate-connections");
      }));
      const open = $e("wzOpen");
      if (open) open.onclick = () => { wizardStep1(); $e("wizard").scrollIntoView({ behavior: "smooth" }); };
      document.querySelectorAll("[data-edit]").forEach((b) => (b.onclick = () => {      // same form, filled in; saved in place
        const c = est.connections.find((x) => String(x.connection_id) === b.dataset.edit);
        if (!c) return;
        $e("wizard").classList.remove("hidden");
        wizardStep2(c.source_type, c);
        $e("wizard").scrollIntoView({ behavior: "smooth" });
      }));
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
const EYE = `<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"
  stroke-linejoin="round" aria-hidden="true"><path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12z"/><circle cx="12" cy="12" r="3"/></svg>`;
const EYE_OFF = `<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"
  stroke-linejoin="round" aria-hidden="true"><path d="M17.9 17.9A10.4 10.4 0 0 1 12 19c-6.5 0-10-7-10-7a18 18 0 0 1 4.1-5.1M9.9 5.2A9.6 9.6 0 0 1 12 5c6.5 0 10 7 10 7a18 18 0 0 1-2.2 3.2M14.1 14.1a3 3 0 1 1-4.2-4.2M2 2l20 20"/></svg>`;
function wizardStep2(type, existing = null) {
  // existing: a connection row -> edit it in place (same id, so its rules, coverage and history stay); the type stays
  const t = est.types[type], w = $e("wizard");
  const current = (k, dflt) => {
    if (!existing) return dflt || "";
    const v = ["host", "port", "database", "username"].includes(k) ? existing[k] : (existing.settings || {})[k];
    return v == null || (k === "port" && !v) ? "" : String(v);
  };
  w.innerHTML = `<h3>${existing ? `Edit ${esc(existing.name)}` : `Add a ${esc(t.label)} source`}</h3>${existing ? "" : wizardSteps(2)}
    ${existing ? `<p class="small muted">${esc(t.label)} source. Its rules, coverage and history stay with it.</p>` : ""}
    <div class="form-grid">
      <label>Name shown in the portal<input id="wzName" value="${esc(existing ? existing.name : "")}" placeholder="e.g. Finance ${esc(t.label)}"></label>
      ${t.fields.map((f) => `<label>${esc(f.label)}${f.required ? " *" : ""}<input data-key="${f.key}" value="${esc(current(f.key, f.default))}" placeholder="${esc(f.placeholder || "")}"></label>`).join("")}
      <label>${esc(t.password_label || "Password")}${existing ? "" : " *"}<span class="pw-field"><input id="wzPw" type="password"
        autocomplete="new-password" placeholder="${existing ? "Leave empty to keep the current password" : ""}"><button type="button"
        class="pw-eye" id="wzPwEye" aria-label="Show password" title="Show password">${EYE}</button></span></label>
    </div>
    ${t.password_hint ? `<p class="small muted">${esc(t.password_hint)}</p>` : ""}
    <div id="wzResult"></div>
    <button class="btn light" id="wzBack">${existing ? "Cancel" : "Back"}</button> <button class="btn" id="wzTest">Test connection</button>
    <button class="btn" id="wzSave" ${existing ? "" : "disabled"}>${existing ? "Save changes" : "Save source"}</button>`;
  const collect = () => {
    // the password goes to the server only; on edit an empty box keeps the saved one (password_env says which, for Test)
    const body = { source_type: type, name: $e("wzName").value.trim() || t.label, password: $e("wzPw").value,
                   password_env: existing ? existing.password_env || "" : "", settings: {} };
    w.querySelectorAll("input[data-key]").forEach((i) => {
      const k = i.dataset.key, v = i.value.trim();
      if (["host", "port", "database", "username"].includes(k)) body[k] = k === "port" ? (v ? Number(v) : null) : v;
      else body.settings[k] = v;
    });
    const missing = t.fields.filter((f) => f.required && !w.querySelector(`input[data-key=${f.key}]`).value.trim()).map((f) => f.label);
    const noPassword = (type === "sqlserver" && !body.username) || (type === "snowflake" && body.settings.private_key_file);
    if (!existing && !body.password && !noPassword) missing.push("Password");
    return { body, missing };
  };
  $e("wzPwEye").onclick = () => {
    const i = $e("wzPw"), show = i.type === "password";
    i.type = show ? "text" : "password";
    $e("wzPwEye").innerHTML = show ? EYE_OFF : EYE;
    $e("wzPwEye").setAttribute("aria-label", show ? "Hide password" : "Show password");
    $e("wzPwEye").title = show ? "Hide password" : "Show password";
  };
  $e("wzBack").onclick = existing ? () => w.classList.add("hidden") : wizardStep1;
  $e("wzTest").onclick = async () => {
    const { body, missing } = collect();
    if (missing.length) { $e("wzResult").innerHTML = `<div class="error-box">Please fill in: ${missing.map(esc).join(", ")}</div>`; return; }
    if (w.querySelector(".steps")) w.querySelector(".steps").outerHTML = wizardSteps(3);
    $e("wzResult").innerHTML = "<p class='muted'>Connecting…</p>";
    const r = await post("/connections/test", body).catch((e) => ({ ok: false, error: e.message }));
    $e("wzResult").innerHTML = r.ok
      ? `<div class="banner ok"><h2>Connection works</h2>${esc(r.version || "")} · logged in as <b>${esc(r.login_user || "")}</b>${r.database ? " · database " + esc(r.database) : ""}</div>`
      : `<div class="error-box">Could not connect: ${esc(r.error)}</div>`;
    $e("wzSave").disabled = false;
    $e("wzSave").textContent = existing ? (r.ok ? "Save changes" : "Save changes anyway") : r.ok ? "Save source" : "Save anyway (fix it later)";
  };
  $e("wzSave").onclick = async () => {
    const { body, missing } = collect();
    if (missing.length) { $e("wzResult").innerHTML = `<div class="error-box">Please fill in: ${missing.map(esc).join(", ")}</div>`; return; }
    if (existing) {
      try {
        const r = await eapi(`/connections/${existing.connection_id}`, { method: "PUT", body: JSON.stringify(body) });
        toast(`${body.name} saved` + (r.overview.ok ? "" : " (not reachable yet)"));
        est.connections = []; est.overview = null;
        if (typeof initScopes === "function") await initScopes();    // the Source picker at the top shows the new name
        renderEstate("estate-connections");
      } catch (e) { $e("wzResult").innerHTML = `<div class="error-box">${esc(e.message)}</div>`; }
      return;
    }
    try {
      const r = await post("/connections", body);
      w.querySelector(".steps").outerHTML = wizardSteps(4);
      toast(`${t.label} source saved` + (r.overview.ok ? "" : " (not reachable yet)"));
      est.connections = []; est.overview = null;
      if (typeof initScopes === "function") await initScopes();      // and appears in the Source picker at the top
      if (typeof setScope === "function" && (state.scopes || []).some((x) => x.key === `db:${r.connection_id}`)) await setScope(`db:${r.connection_id}`);
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
// Schema changes: who created, changed or dropped which table or view in the source picked at the top, and how.
// DataTrust's own housekeeping is hidden unless asked for; repeats in the same minute are one row (estate/schema_changes.py).
const act = { days: 30, internal: false, q: "", user: "" };
const ACT_DAYS = [[7, "7 days"], [30, "30 days"], [90, "90 days"], [365, "a year"]];
async function pageActivity() {
  const scope = (/^db:(\d+)$/.exec(state.scope || "") || [])[1];
  const none = (msg) => ({ html: `<div class="card"><h3>Schema changes</h3><p class="muted">${msg}</p></div>`, bind() {} });
  if (!scope) return none(`Schema changes are recorded for databases. Pick a database at the top of the page.
    For a Data Factory, see <a href="#estate-adf-runs">ADF runs</a>: each run lists its steps and what changed in the target.`);
  const conn = (await eapi("/connections")).find((c) => String(c.connection_id) === scope);
  if (!conn) return none("This source is not a connection any more. Pick another source at the top.");
  if (!conn.database) return none(`<b>${esc(conn.name)}</b> has no database of its own set. Edit it under <a href="#estate-connections">Connections</a> and choose one.`);
  const p = new URLSearchParams({ days: act.days, internal: act.internal });
  if (act.q) p.set("object_name", act.q);
  if (act.user) p.set("username", act.user);
  const d = await eapi(`/connections/${scope}/db/${encodeURIComponent(conn.database)}/schema-changes?${p}`);
  const where = `<b>${esc(d.source)}</b> · database <code>${esc(d.database)}</code>`;
  if (d.unreachable) {
    return { html: `<div class="card"><h3>Can't reach this source</h3><p>DataTrust could not connect to ${where}, so its schema changes can't be read right now.
      Check that the server is running and reachable, then <button class="btn light btn-sm" id="scRetry">Try again</button></p>
      <details class="small"><summary>The error</summary><pre class="code small">${esc(d.unreachable)}</pre></details></div>`,
      bind() { $e("scRetry").onclick = () => renderEstate("estate-activity"); } };
  }
  if (!d.tracking) {
    return { html: `<div class="card"><h3>Change tracking is off</h3><p>${where} does not record schema changes yet.
      ${est.user && est.user.role === "admin" ? `Turn it on under <a href="#estate-connections">Connections</a> (<b>Enable tracking</b> next to the database).`
        : "An admin can turn it on under Connections."}</p>
      <p class="small muted">Tracking adds a small log (schema <code>dt_audit</code>) and a trigger to that database. From then on every CREATE, ALTER and DROP
        is recorded with who, when, from which application and the statement itself.</p></div>`, bind() {} };
  }
  const c = d.counts || {};
  const kpi = (cls, l, v, sub) => `<div class="kpi ${cls}"><div class="l">${l}</div><div class="v">${num(v)}</div><div class="small muted">${sub}</div></div>`;
  const when = (t) => `<span style="white-space:nowrap">${esc(String(t).slice(0, 16))}</span>`;
  const rows = d.rows.map((r, i) => `<tr>
      <td class="small">${when(r.at)}${r.times > 1 ? `<div class="muted">×${r.times} in that minute</div>` : ""}</td>
      <td>${esc(r.who || "–")}</td>
      <td><span class="chip ${r.tone}">${esc(r.action)}</span>${r.datatrust ? ` <span class="chip grey" title="DataTrust's own tables">DataTrust</span>` : ""}</td>
      <td><code>${esc(r.object)}</code></td>
      <td class="small muted">${esc(r.app || (r.client && !/^(::1|127\.0\.0\.1)/.test(r.client) ? r.client : "this server"))}</td>
      <td>${r.sql ? `<details class="sc-sql"><summary class="small">statement</summary><pre class="code small">${esc(r.sql)}</pre></details>` : ""}</td></tr>`).join("");
  return {
    html: `<div class="card sc-head"><div>${where}<div class="small muted">The source picked at the top of the page.</div></div>
        <div class="seg" role="tablist">${ACT_DAYS.map(([n, l]) => `<button type="button" data-days="${n}" class="${act.days === n ? "active" : ""}">${l}</button>`).join("")}</div></div>
      <div class="kpis" style="grid-template-columns:repeat(4,1fr)">
        ${kpi("", "Changes", c.changes, `in the last ${ACT_DAYS.find(([n]) => n === act.days)[1]}`)}
        ${kpi("", "Tables and views", c.objects, "changed")}
        ${kpi(c.dropped ? "bad" : "", "Dropped or emptied", c.dropped, "data may be gone")}
        ${kpi("", "People", c.people, "made these changes")}</div>
      <div class="card"><div class="draft-head"><h3 style="margin:0">Who changed what</h3><span class="small muted">newest first</span></div>
        <div class="rj-filters">
          <label class="grow">Table or view<input id="scObj" placeholder="e.g. silver.payments" value="${esc(act.q)}"></label>
          <label>Person<input id="scUser" placeholder="exact login name" value="${esc(act.user)}"></label>
          <label class="sc-check"><input type="checkbox" id="scInt" ${act.internal ? "checked" : ""}> Show DataTrust's own changes${d.hidden_internal ? ` (${num(d.hidden_internal)} hidden)` : ""}</label>
          <span class="rj-actions"><button class="btn btn-sm" id="scGo">Show</button><button class="btn light btn-sm" id="scClear">Clear</button></span></div>
        ${d.truncated ? `<p class="small muted">Only the newest entries of the log were read. Narrow the period or search for a table.</p>` : ""}
        <div class="tablewrap" style="max-height:620px"><table class="sc-table"><thead><tr><th>When</th><th>Who</th><th>What</th><th>Table or view</th><th>From</th><th></th></tr></thead>
          <tbody>${rows || `<tr><td colspan="6" class="muted">No schema changes ${act.q || act.user ? "match these filters" : "in this period"}.${!act.internal && d.hidden_internal ? " DataTrust's own housekeeping is hidden." : ""}</td></tr>`}</tbody></table></div>
        <p class="small muted">DataTrust's own housekeeping (its tables in portal, monitor, app and telemetry, adjusted at every start) is hidden unless you ask for it.
          A schema change that breaks a rule also appears on <a href="#detect">Detect</a>. Pipeline failures are on <a href="#pipeline">Pipeline log</a> and <a href="#estate-adf-runs">ADF runs</a>.</p></div>
      ${d.jobs && d.jobs.length ? `<div class="card"><h3>Failed SQL Agent jobs</h3><p class="small muted">Jobs on this SQL Server that failed. No other page shows these.</p>${objTable(d.jobs)}</div>` : ""}`,
    bind() {
      const read = () => { act.q = $e("scObj").value.trim(); act.user = $e("scUser").value.trim(); act.internal = $e("scInt").checked; };
      const go = () => { read(); renderEstate("estate-activity"); };
      document.querySelectorAll("[data-days]").forEach((b) => (b.onclick = () => { read(); act.days = Number(b.dataset.days); renderEstate("estate-activity"); }));
      $e("scGo").onclick = go; $e("scInt").onchange = go;
      ["scObj", "scUser"].forEach((id) => ($e(id).onkeydown = (e) => { if (e.key === "Enter") go(); }));
      $e("scClear").onclick = () => { Object.assign(act, { q: "", user: "", internal: false }); renderEstate("estate-activity"); };
    },
  };
}

// ------------------------------------------------------------------ Administration: portal users
// Who can sign in to DataTrust itself (not to the databases it watches). Admins add people, change a role, switch someone
// off, or set a new password; a role change or switching off takes effect at once (the session is checked on every call).
const PU = { edit: null };
async function pageUsers() {
  if (est.user.role !== "admin") return { html: `<div class="card"><h3>Admins only</h3><p class="muted">Ask an admin to add or change portal users.</p></div>`, bind() {} };
  const users = await eapi("/portal-users");
  const me = (est.user.username || "").toLowerCase(), e = PU.edit && users.find((u) => u.username === PU.edit);
  const admins = users.filter((u) => u.role === "admin" && u.active).length;
  const rows = users.map((u) => `<tr class="${u.active ? "" : "pu-off"}">
      <td><b>${esc(u.username)}</b>${u.username.toLowerCase() === me ? ` <span class="chip grey">you</span>` : ""}</td>
      <td><span class="chip ${u.role === "admin" ? "warn" : "grey"}">${esc(u.role)}</span></td>
      <td>${u.active ? `<span class="chip ok">active</span>` : `<span class="chip bad">switched off</span>`}</td>
      <td class="small">${u.email_verified ? "verified" : `<span class="muted">not verified yet</span>`}${u.google ? " · Google" : ""}</td>
      <td class="small muted">${esc(u.created_by || "–")}<div>${esc(String(u.created_at || "").slice(0, 10))}</div></td>
      <td class="nowrap">${u.username.includes("@") ? `<button class="btn light btn-sm" data-pu-edit="${esc(u.username)}">Change</button>` : `<span class="small muted" title="Sign-in names without @ come from .env">from .env</span>`}</td></tr>`).join("");
  return {
    html: `<div class="kpis" style="grid-template-columns:repeat(3,1fr)">
        <div class="kpi"><div class="l">People</div><div class="v">${num(users.filter((u) => u.active).length)}</div><div class="small muted">can sign in</div></div>
        <div class="kpi ${admins < 2 ? "warn" : ""}"><div class="l">Admins</div><div class="v">${num(admins)}</div><div class="small muted">${admins < 2 ? "add a second admin, so approvals don't depend on one person" : "can approve changes"}</div></div>
        <div class="kpi"><div class="l">Switched off</div><div class="v">${num(users.filter((u) => !u.active).length)}</div><div class="small muted">kept for the audit trail</div></div></div>
      <div class="card" id="puForm"><h3>${e ? `Change ${esc(e.username)}` : "Add a person"}</h3>
        <div class="form-grid pu-grid">
          <label class="field"><span>Email</span><input id="puName" type="email" autocomplete="off" placeholder="name@company.com" value="${esc(e ? e.username : "")}" ${e ? "readonly" : ""}></label>
          <label class="field"><span>Role</span><select id="puRole"><option value="viewer" ${e && e.role === "viewer" ? "selected" : ""}>viewer: can look and ask</option>
            <option value="admin" ${e && e.role === "admin" ? "selected" : ""}>admin: can also connect sources and approve</option></select></label>
          <label class="field"><span>${e ? "New password (optional)" : "Password"}</span><input id="puPw" type="password" autocomplete="new-password"
            placeholder="${e ? "leave empty to keep the current one" : "at least 12 characters"}"></label>
          ${e ? `<label class="field"><span>Can sign in</span><select id="puActive"><option value="true" ${e.active ? "selected" : ""}>yes</option><option value="false" ${e.active ? "" : "selected"}>no, switch off</option></select></label>` : ""}
        </div>
        <div class="pacts"><button class="btn" id="puSave">${e ? "Save changes" : "Add person"}</button>${e ? `<button class="btn light" id="puCancel">Cancel</button>` : ""}</div>
        <div id="puMsg"></div>
        <p class="small muted">${e ? "A new password signs the person out everywhere else. Changing the role or switching someone off takes effect at once."
          : "People added here can sign in straight away with this email and password, or with Google if it is set up. Share the password with them directly."}</p></div>
      <div class="card"><h3>Portal users</h3><p class="small muted">Who can sign in to DataTrust. Access to the databases themselves is on <a href="#estate-access">Access</a>.</p>
        <div class="tablewrap"><table><thead><tr><th>Email</th><th>Role</th><th>Status</th><th>Sign-in</th><th>Added by</th><th></th></tr></thead><tbody>${rows}</tbody></table></div></div>`,
    bind() {
      document.querySelectorAll("[data-pu-edit]").forEach((b) => (b.onclick = () => { PU.edit = b.dataset.puEdit; renderEstate("estate-users").then(() => $e("puForm").scrollIntoView({ behavior: "smooth" })); }));
      if ($e("puCancel")) $e("puCancel").onclick = () => { PU.edit = null; renderEstate("estate-users"); };
      $e("puSave").onclick = async () => {
        const email = $e("puName").value.trim(), pw = $e("puPw").value, msg = (t) => ($e("puMsg").innerHTML = `<div class="error-box small">${esc(t)}</div>`);
        if (!/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(email)) return msg("Enter the person's email address.");
        if ((!e || pw) && pw.length < 12) return msg(e ? "A new password needs at least 12 characters, or leave it empty." : "The password needs at least 12 characters.");
        $e("puSave").disabled = true;
        try {
          await post("/portal-users", { username: email, password: pw, role: $e("puRole").value, active: e ? $e("puActive").value === "true" : true });
          toast(e ? `${email} saved` : `${email} added`); PU.edit = null; renderEstate("estate-users");
        } catch (x) { msg(x.message); $e("puSave").disabled = false; }
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
    html: `<div class="card" id="axReview"><h3>Who has access</h3><p class="small muted">Reading the logins and roles of ${esc(est.db)}…</p></div>
      <div class="card ask-access"><h3>Describe what you need <span class="chip grey">no SQL needed</span></h3>
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
      <div class="card"><div class="draft-head"><h3 style="margin:0">Request history</h3>${admin ? `<a class="small" style="margin-left:auto" href="#estate-history=access">View all history →</a>` : ""}</div>${objTable(history.map((r) => ({ id: r.request_id, type: REQ_LABEL[r.request_type] || r.request_type, grantee: r.grantee, database: r.database, status: r.status, requested_by: r.requested_by, decided_by: r.decided_by, decided_at: r.decided_at, error: r.error })))
        || "<p class='muted'>No requests yet.</p>"}</div>
      ${admin ? `<p class="small muted">Who can sign in to DataTrust itself is managed under <a href="#estate-users">Administration → Portal users</a>.</p>` : ""}`,
    bind: bindAccess,
  };
}
// "Who has access": read-only review of this source's logins and roles, risks first (estate/access_review.py)
const AX_RISK = { superuser: ["bad", "Superuser", "Can do anything on this server, including dropping tables."],
  writes_gold: ["bad", "Can change gold", "Can change the gold tables the reports read. Usually only the pipeline should."],
  no_expiry: ["warn", "Never expires", "A login whose password never expires."] };
async function loadAccessReview() {
  const el = $e("axReview");
  if (!el) return;
  const conn = est.conn, db = est.db;
  let d;
  try { d = await eapi(`/connections/${conn}/db/${encodeURIComponent(db)}/access-review`); }
  catch (e) { if ($e("axReview")) el.innerHTML = `<h3>Who has access</h3><div class="error-box">${esc(e.message)}</div>`; return; }
  if (!$e("axReview") || est.conn !== conn || est.db !== db) return;     // the page moved on meanwhile
  if (!d.available) { el.innerHTML = `<h3>Who has access</h3><p class="muted">${esc(d.note)}</p>`; return; }
  const c = d.counts, list = (a) => (a.length ? a.map((x) => `<code>${esc(x)}</code>`).join(" ") : `<span class="muted">–</span>`);
  const kpi = (cls, l, v, sub) => `<div class="kpi ${cls}"><div class="l">${l}</div><div class="v">${num(v)}</div><div class="small muted">${sub}</div></div>`;
  const rows = d.people.map((p) => `<tr>
      <td><b>${esc(p.name)}</b><div class="small muted">${esc(p.kind)}${p.member_of.length ? ` · member of ${p.member_of.map(esc).join(", ")}` : ""}</div></td>
      <td class="small">${list(p.reads)}</td><td class="small">${list(p.writes)}</td>
      <td>${p.risks.map((k) => `<span class="chip ${AX_RISK[k][0]}" title="${esc(AX_RISK[k][2])}">${AX_RISK[k][1]}</span>`).join(" ") || `<span class="small muted">–</span>`}</td></tr>`).join("");
  el.innerHTML = `<div class="draft-head"><h3 style="margin:0">Who has access</h3><span class="small muted">${esc(d.database)} · read-only review, nothing is changed</span></div>
    <div class="kpis" style="grid-template-columns:repeat(4,1fr);margin-top:12px">
      ${kpi(c.superusers ? "bad" : "", "Superusers", c.superusers, "can do anything")}
      ${kpi(c.write_gold ? "bad" : "", "Can change gold", c.write_gold, "besides superusers")}
      ${kpi(c.public_schemas ? "warn" : "", "Open to everyone", c.public_schemas, "schemas granted to PUBLIC")}
      ${kpi(c.no_expiry ? "warn" : "", "Never expire", c.no_expiry, "logins without an expiry date")}</div>
    ${d.public.length ? `<div class="banner warn small"><b>Granted to everyone (PUBLIC):</b> every login on this server has these rights.
      ${d.public.map((x) => `<code>${esc(x.schema)}</code> ${esc(x.privileges.join(", ").toLowerCase())}`).join(" · ")}</div>` : ""}
    <div class="tablewrap"><table class="ax-table"><thead><tr><th>Login or role</th><th>Can read</th><th>Can change</th><th>Worth a look</th></tr></thead>
      <tbody>${rows || `<tr><td colspan="4" class="muted">No logins or roles found.</td></tr>`}</tbody></table></div>
    <p class="small muted">Schemas where the login or role has its own rights (or owns objects); <code>(all)</code> means every schema. Rights that come through a group role
      show on that group's row. Click a name under <b>Users and roles</b> below for every table.${d.truncated ? " Only the first 60 logins and roles are reviewed." : ""}</p>`;
}
function bindAccess() {
  loadAccessReview();
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
