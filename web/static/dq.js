// Data quality: Rejected records explorer + DQ Rule Studio. Rendered by estate.js after login (pages estate-dq-*).
// Depends on app.js (esc, num, inr, toast) and estate.js (eapi, post, est, renderEstate).
(function () {
  const dqapi = (p, o) => eapi("/dq" + p, o);
  const dqpost = (p, d) => post("/dq" + p, d);
  const isAdmin = () => est.user && est.user.role === "admin";
  const ENT_LABEL = { payments: "Payments", payment_orders: "Orders", refunds: "Refunds", settlements: "Settlements",
    customers: "Customers", products: "Products", categories: "Categories" };
  const when = (t) => (t ? String(t).slice(0, 16).replace("T", " ") : "");
  const statusChip = (s) => `<span class="chip ${s === "OPEN" ? "bad" : s === "IN REVIEW" ? "warn" : s === "RESOLVED" ? "ok" : "grey"}">${esc(s)}</span>`;
  const reqChip = (s) => `<span class="chip ${s === "APPLIED" ? "ok" : s === "QUEUED" || s === "PENDING" ? "warn" : s === "REJECTED" || s === "FAILED" ? "bad" : "grey"}">${esc(s)}</span>`;

  // ================================================================ REJECTED RECORDS
  const rj = { f: { view: "open", date_from: "", date_to: "", entity: "", rule: "", q: "" }, rows: [], sel: new Set(), open: null };
  const qs = () => {
    const p = new URLSearchParams();
    ["date_from", "date_to", "entity", "rule", "q"].forEach((k) => rj.f[k] && p.set(k, rj.f[k]));
    if (rj.f.view === "history") p.set("status", "history");
    return p.toString();
  };
  const itemOf = (r) => ({ record_key: r.record_key, business_date: r.business_date, rule_name: r.rule_name });

  async function pageRejected() {
    const [sum, list, reqs] = await Promise.all([dqapi(`/rejected-summary?${qs()}`), dqapi(`/rejected?${qs()}&limit=300`), dqapi("/reprocess")]);
    rj.rows = list.rows;
    rj.sel = new Set([...rj.sel].filter((i) => i < rj.rows.length));
    const history = rj.f.view === "history";
    const held = sum.by_date.filter((r) => r.entity === "payments").reduce((a, r) => a + Number(r.amount || 0), 0);
    const pending = reqs.filter((r) => r.status === "PENDING");
    const opt = (vals, cur, label) => `<option value="">${label}</option>` + vals.map((v) => `<option ${v === cur ? "selected" : ""} value="${esc(v)}">${esc(ENT_LABEL[v] || v)}</option>`).join("");
    const groups = sum.by_date.map((g) => `<button class="rj-group ${rj.f.date_from === g.business_date && rj.f.entity === g.entity && rj.f.rule === g.rule_name ? "on" : ""}"
        data-g='${esc(JSON.stringify([g.business_date, g.entity, g.rule_name]))}'><b>${esc(g.business_date || "no date")}</b>
        <span>${esc(ENT_LABEL[g.entity] || g.entity)} · ${esc(g.rule_name)}</span><span class="rj-n">${num(g.n)} row${g.n === 1 ? "" : "s"}${g.amount ? " · " + inr(g.amount) : ""}</span></button>`).join("");
    const rowsHtml = rj.rows.map((r, i) => {
      const val = r.column ? (r.raw[r.column] ?? "") : "";
      return `<tr class="clickable ${rj.open === i ? "sel" : ""}" data-i="${i}">
        ${history ? "" : `<td class="cb"><input type="checkbox" data-sel="${i}" ${rj.sel.has(i) ? "checked" : ""} ${r.record_key ? "" : "disabled"} aria-label="select"></td>`}
        <td>${esc(r.business_date || "")}</td><td>${esc(ENT_LABEL[r.entity] || r.entity)}</td><td class="mono">${esc(r.record_key || "(no key)")}</td>
        <td><b>${esc(r.rule_name)}</b>${r.rule_text ? `<div class="small muted">${esc(r.rule_text)}</div>` : ""}</td>
        ${history ? `<td class="small">${esc(r.batch_id)}<div class="muted">${when(r.rejected_at)}</div></td><td>${statusChip(r.status)}${r.resolution ? `<div class="small muted">${esc(r.resolution)}</div>` : ""}</td>`
          : `<td class="mono small">${r.column ? `${esc(r.column)} = <span class="bad">${val === "" ? "empty" : esc(val)}</span>` : ""}</td>
        <td class="num">${r.amount != null && r.amount !== "" ? inr(r.amount) : ""}</td>
        <td class="small">${r.times_seen > 1 ? `<b>${r.times_seen}×</b> since ${when(r.first_seen).slice(0, 10)}` : when(r.rejected_at).slice(0, 10)}</td>
        <td>${statusChip(r.status)}</td>`}</tr>`;
    }).join("");
    const reqRow = (r) => `<div class="check"><div class="draft-head"><b>#${r.request_id} Re-insert ${r.items.length} ${esc(ENT_LABEL[r.entity] || r.entity)} record(s)</b> ${reqChip(r.status)}
        <span class="small muted">by ${esc(r.requested_by)} · ${when(r.requested_at)}${r.note ? " · " + esc(r.note) : ""}</span></div>
        <div class="small">${Object.keys(r.changes).length ? "Change: " + Object.entries(r.changes).map(([k, v]) => `<code>${esc(k)}</code> → <b>${esc(v)}</b>`).join(", ") : "No value changes (passes the current rules)"}
        · keys: <span class="mono">${esc(r.items.slice(0, 6).map((x) => x.record_key).join(", "))}${r.items.length > 6 ? " …" : ""}</span></div>
        ${r.result ? `<div class="small muted">${esc(r.result)}${r.decided_by ? ` · ${esc(r.decided_by)} ${when(r.decided_at)}` : ""}</div>` : ""}
        ${r.status === "PENDING" ? (isAdmin() ? `<div class="pacts"><button class="btn btn-sm" data-approve="${r.request_id}">Approve &amp; re-insert</button> <button class="btn light btn-sm" data-reject="${r.request_id}">Reject</button></div>` : `<div class="small muted">Waiting for an admin.</div>`) : ""}</div>`;
    return {
      html: `<div class="kpis rj-kpis">
          <div class="kpi ${sum.open ? "bad" : ""}"><div class="l">Held back now</div><div class="v">${num(sum.open)}</div><div class="small muted">rows not in silver / gold</div></div>
          <div class="kpi ${held ? "warn" : ""}"><div class="l">Payments held back</div><div class="v">${inr(held)}</div><div class="small muted">money not in revenue yet</div></div>
          <div class="kpi ${sum.pending ? "warn" : ""}"><div class="l">Waiting approval</div><div class="v">${num(sum.pending)}</div><div class="small muted">re-insert requests</div></div>
          <div class="kpi"><div class="l">Re-inserted</div><div class="v">${num(sum.reinserted)}</div><div class="small muted">fixed via this page</div></div>
          <div class="kpi"><div class="l">Won't fix</div><div class="v">${num(sum.reviewed)}</div><div class="small muted">reviewed, with a note</div></div>
          <div class="kpi"><div class="l">Dates affected</div><div class="v">${num(sum.dates.length)}</div><div class="small muted">${esc(sum.dates.slice(0, 3).join(", "))}</div></div></div>
        ${pending.length ? `<div class="card"><h3>Re-insert requests waiting for approval</h3>${pending.map(reqRow).join("")}</div>` : ""}
        <div class="card"><div class="draft-head"><h3 style="margin:0">Held back by date</h3><span class="small muted">click a group to see its records</span></div>
          <div class="rj-groups">${groups || `<p class="muted">Nothing is held back${rj.f.date_from || rj.f.date_to ? " in these dates" : ""}. Every row passed the rules.</p>`}</div></div>
        <div class="card"><div class="rj-filters">
            <div class="seg" role="tablist"><button data-view="open" class="${history ? "" : "active"}">Held back now</button><button data-view="history" class="${history ? "active" : ""}">All rejections (history)</button></div>
            <label>From<input type="date" id="rjFrom" value="${esc(rj.f.date_from)}"></label>
            <label>To<input type="date" id="rjTo" value="${esc(rj.f.date_to)}"></label>
            <label>Table<select id="rjEnt">${opt(Object.keys(ENT_LABEL), rj.f.entity, "All tables")}</select></label>
            <label>Rule<select id="rjRule">${opt([...new Set([...sum.rules, rj.f.rule].filter(Boolean))], rj.f.rule, "All rules")}</select></label>
            <label class="grow">Search<input id="rjQ" placeholder="key, customer, any value…" value="${esc(rj.f.q)}"></label>
            <button class="btn btn-sm" id="rjApply">Show</button><button class="btn light btn-sm" id="rjClear">Clear</button>
            <a class="btn light btn-sm" id="rjCsv" href="/api/estate/dq/rejected.csv?${qs()}" download>⬇ CSV</a></div>
          <div class="draft-head"><span class="small muted">${num(list.total)} record${list.total === 1 ? "" : "s"}${list.total > rj.rows.length ? `, first ${rj.rows.length} shown (narrow the dates or download the CSV)` : ""}</span>
            ${history ? "" : `<span id="rjBulk" class="rj-bulk ${rj.sel.size ? "" : "hidden"}"><b id="rjSelN">${rj.sel.size}</b> selected
              <button class="btn btn-sm" id="rjFixSel">Fix &amp; re-insert…</button>${isAdmin() ? `<button class="btn light btn-sm" id="rjIgnoreSel">Mark won't fix…</button>` : ""}</span>`}</div>
          <div class="tablewrap"><table class="rj-table"><thead><tr>${history ? "" : `<th class="cb"><input type="checkbox" id="rjAll" aria-label="select all"></th>`}
            <th>Date</th><th>Table</th><th>Record key</th><th>Rule (cause)</th>${history ? "<th>Run</th><th>What happened</th>" : "<th>Failing value</th><th>Amount</th><th>Seen</th><th>Status</th>"}</tr></thead>
            <tbody>${rowsHtml || `<tr><td colspan="9" class="muted">No records for these filters.</td></tr>`}</tbody></table></div>
          <div id="rjPanel"></div></div>
        ${reqs.filter((r) => r.status !== "PENDING").length ? `<div class="card"><h3>Recent re-insert requests</h3>${reqs.filter((r) => r.status !== "PENDING").slice(0, 8).map(reqRow).join("")}</div>` : ""}`,
      bind: bindRejected,
    };
  }

  function bindRejected() {
    const root = $e("body-estate-dq-rejected");
    const read = () => Object.assign(rj.f, { date_from: $e("rjFrom").value, date_to: $e("rjTo").value, entity: $e("rjEnt").value,
      rule: $e("rjRule").value, q: $e("rjQ").value.trim() });
    const reload = () => { rj.sel.clear(); rj.open = null; renderEstate("estate-dq-rejected"); };
    $e("rjApply").onclick = () => { read(); reload(); };
    $e("rjQ").onkeydown = (e) => { if (e.key === "Enter") { read(); reload(); } };
    $e("rjClear").onclick = () => { Object.assign(rj.f, { date_from: "", date_to: "", entity: "", rule: "", q: "" }); reload(); };
    root.querySelectorAll("[data-view]").forEach((b) => (b.onclick = () => { rj.f.view = b.dataset.view; reload(); }));
    root.querySelectorAll("[data-g]").forEach((b) => (b.onclick = () => {
      const [d, e, r] = JSON.parse(b.dataset.g);
      const same = rj.f.date_from === d && rj.f.entity === e && rj.f.rule === r;
      Object.assign(rj.f, same ? { date_from: "", date_to: "", entity: "", rule: "" } : { date_from: d, date_to: d, entity: e, rule: r, view: "open" });
      reload();
    }));
    const updateBulk = () => {
      const b = $e("rjBulk");
      if (!b) return;
      b.classList.toggle("hidden", !rj.sel.size);
      $e("rjSelN").textContent = rj.sel.size;
    };
    root.querySelectorAll("[data-sel]").forEach((c) => {
      c.onclick = (e) => e.stopPropagation();
      c.onchange = () => { c.checked ? rj.sel.add(+c.dataset.sel) : rj.sel.delete(+c.dataset.sel); updateBulk(); };
    });
    const all = $e("rjAll");
    if (all) all.onchange = () => {
      root.querySelectorAll("[data-sel]:not([disabled])").forEach((c) => { c.checked = all.checked; all.checked ? rj.sel.add(+c.dataset.sel) : rj.sel.delete(+c.dataset.sel); });
      updateBulk();
    };
    root.querySelectorAll("tr[data-i]").forEach((tr) => (tr.onclick = () => openRecord(+tr.dataset.i)));
    if ($e("rjFixSel")) $e("rjFixSel").onclick = () => bulkFix();
    if ($e("rjIgnoreSel")) $e("rjIgnoreSel").onclick = () => bulkIgnore();
    bindDecisions(root, "estate-dq-rejected");
  }

  function bindDecisions(root, page) {
    root.querySelectorAll("[data-approve]").forEach((b) => (b.onclick = async () => {
      b.disabled = true; b.textContent = "Re-inserting…";
      try { const r = await dqpost(`/reprocess/${b.dataset.approve}/approve`); toast(r.result); if (typeof loadCore === "function") loadCore().catch(() => {}); renderEstate(page); }
      catch (e) { toast(e.message); b.disabled = false; b.textContent = "Approve & re-insert"; }
    }));
    root.querySelectorAll("[data-reject]").forEach((b) => (b.onclick = async () => {
      try { await dqpost(`/reprocess/${b.dataset.reject}/reject`); toast("Request rejected"); renderEstate(page); } catch (e) { toast(e.message); }
    }));
  }

  const selected = () => {
    const rows = [...rj.sel].map((i) => rj.rows[i]).filter(Boolean);
    const ents = [...new Set(rows.map((r) => r.entity))];
    return { rows, ents };
  };

  // one record: raw values (editable), why it fails, its history, re-insert
  async function openRecord(i) {
    const r = rj.rows[i];
    rj.open = i;
    document.querySelectorAll(".rj-table tr.sel").forEach((t) => t.classList.remove("sel"));
    const tr = document.querySelector(`.rj-table tr[data-i="${i}"]`);
    if (tr) tr.classList.add("sel");
    const panel = $e("rjPanel");
    if (!r.record_key) {
      panel.innerHTML = `<div class="rj-detail"><h3>Record without a key</h3><p>This row arrived without its ${esc(r.rule_name.replace("_REQUIRED", "").toLowerCase())}, so it cannot be matched or re-inserted. The source team has to send it again.</p>
        <pre class="code">${esc(JSON.stringify(r.raw, null, 2))}</pre></div>`;
      return panel.scrollIntoView({ behavior: "smooth", block: "nearest" });
    }
    panel.innerHTML = `<div class="rj-detail"><p class="muted">Loading ${esc(r.record_key)}…</p></div>`;
    const d = await dqapi(`/record?entity=${r.entity}&key=${encodeURIComponent(r.record_key)}${r.business_date ? "&business_date=" + r.business_date : ""}`);
    if (!d.open) {
      panel.innerHTML = `<div class="rj-detail"><h3>${esc(r.record_key)}</h3><p>Not held back any more.</p>${historyHtml(d)}</div>`;
      return;
    }
    const failCols = new Set(d.check.failing.map((f) => f.column));
    const fields = d.editable.map((k) => {
      const v = d.raw[k] ?? "";
      const bad = failCols.has(k) || (k === "id" && failCols.has(d.key_column)) || (k === "status" && failCols.has("payment_status"));
      return `<label class="${bad ? "bad-field" : ""}"><span>${esc(k)}${bad ? " ⚠" : ""}</span><input data-f="${esc(k)}" value="${esc(v)}" data-orig="${esc(v)}" ${k === d.key_column ? "disabled" : ""}></label>`;
    }).join("");
    panel.innerHTML = `<div class="rj-detail">
      <div class="draft-head"><h3 style="margin:0">${esc(ENT_LABEL[d.entity] || d.entity)} · <span class="mono">${esc(d.record_key)}</span></h3>${statusChip(r.status)}
        <span class="small muted">business date ${esc(d.business_date || "–")} · batch ${esc(d.batch_id)} · rejected ${when(d.rejected_at)}</span>
        <button class="btn light btn-sm" data-contact="silver.${esc(d.entity)}" data-context="${esc(d.entity + " " + d.record_key + " rejected by " + d.rule_name)}">Contact owner</button></div>
      <div class="rj-why"><b>Why it was held back:</b> ${d.check.failing.map((f) => `<span class="chip bad">${esc(f.rule_name)}</span> ${esc(f.text)} — the value is <b>${f.value == null ? "empty" : esc(f.value)}</b>`).join("<br>") || "it passes the current rules now (a rule was changed): it can be re-inserted as it is."}</div>
      ${r.reviewed ? `<p class="small muted">Marked won't fix by ${esc(r.reviewed.by)}: “${esc(r.reviewed.note)}”</p>` : ""}
      <p class="small muted">Correct the values below as they should have arrived from the source (empty fields are values the source did not send). The record is re-checked with the current rules first; an admin approves before anything is loaded.${d.scd2 ? " This table keeps history (SCD2): an approved fix is loaded by the next silver run." : ""}</p>
      <div class="rj-fields">${fields}</div>
      <div class="askform"><input id="rjNote" placeholder="Why is this correct? e.g. confirmed with the gateway team (shown to the approver)"></div>
      <div class="pacts"><button class="btn light btn-sm" id="rjCheck">Check with the rules</button><button class="btn btn-sm" id="rjSend">Request re-insert</button></div>
      <div id="rjResult"></div>${historyHtml(d)}</div>`;
    const changes = () => {
      const c = {};
      panel.querySelectorAll("[data-f]").forEach((inp) => { if (inp.value !== inp.dataset.orig) c[inp.dataset.f] = inp.value; });
      return c;
    };
    const items = [itemOf(r)];
    $e("rjCheck").onclick = async () => {
      try { showPreview(await dqpost("/reprocess/preview", { entity: d.entity, items, changes: changes() })); } catch (e) { $e("rjResult").innerHTML = `<div class="error-box">${esc(e.message)}</div>`; }
    };
    $e("rjSend").onclick = async () => {
      try {
        const res = await dqpost("/reprocess", { entity: d.entity, items, changes: changes(), note: $e("rjNote").value });
        toast(`Request #${res.request_id} sent${isAdmin() ? ": approve it at the top of the page" : " to the admins"}`);
        rj.open = null; renderEstate("estate-dq-rejected");
      } catch (e) { $e("rjResult").innerHTML = `<div class="error-box">${esc(e.message)}</div>`; }
    };
    panel.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }

  function historyHtml(d) {
    const h = (d.history || []).map((x) => `<tr><td>${esc(x.business_date || "")}</td><td>${esc(x.rule_name)}</td><td>${esc(x.batch_id)}</td><td>${when(x.rejected_at)}</td></tr>`).join("");
    const q = (d.requests || []).map((x) => `<tr><td>#${x.request_id}</td><td>${reqChip(x.status)}</td><td>${esc(Object.entries(x.changes || {}).map(([k, v]) => k + " → " + v).join(", ") || "as is")}</td><td>${esc(x.requested_by)} ${when(x.requested_at)}</td><td>${esc(x.decided_by || "")} ${when(x.decided_at)}</td></tr>`).join("");
    return `<div class="grid g-2 rj-hist"><div><h4>Every time it was rejected</h4><div class="tablewrap"><table><thead><tr><th>Date</th><th>Rule</th><th>Run</th><th>When</th></tr></thead><tbody>${h || `<tr><td colspan="4" class="muted">–</td></tr>`}</tbody></table></div></div>
      <div><h4>Re-insert requests</h4><div class="tablewrap"><table><thead><tr><th>#</th><th>Status</th><th>Change</th><th>Requested</th><th>Decided</th></tr></thead><tbody>${q || `<tr><td colspan="5" class="muted">none yet</td></tr>`}</tbody></table></div></div></div>`;
  }

  function showPreview(p) {
    const box = $e("rjResult");
    box.innerHTML = `<div class="draft"><div class="draft-head"><b>${p.ready} of ${p.items.length} record(s) pass the current rules</b>
        ${p.ready ? `<span class="chip ok">ready</span>` : `<span class="chip bad">blocked</span>`}<span class="small muted">${esc(p.how)}</span></div>
      ${p.items.map((x) => `<div class="small">${x.ok ? "✔" : "✖"} <span class="mono">${esc(x.record_key)}</span> — ${esc(x.reason)}${x.warnings && x.warnings.length ? ` <span class="chip warn">warning ${esc(x.warnings.join(", "))}</span>` : ""}</div>`).join("")}</div>`;
  }

  // several records at once: one change for all of them (e.g. currency = INR for all payments of 8 Oct)
  function bulkFix() {
    const { rows, ents } = selected();
    const panel = $e("rjPanel");
    if (ents.length !== 1) return toast("Select records of one table at a time.");
    const entity = ents[0];
    const cols = [...new Set(rows.map((r) => r.column).filter(Boolean))];
    panel.innerHTML = `<div class="rj-detail"><h3>Fix &amp; re-insert ${rows.length} ${esc(ENT_LABEL[entity] || entity)} record(s)</h3>
      <p class="small muted">Set one field to the same value for all of them, or leave the value empty to re-insert them as they are (after a rule was changed). Every record is re-checked; an admin approves before anything is loaded.</p>
      <div class="form-grid"><label>Field<input id="bfCol" list="bfCols" value="${esc(cols[0] || "")}" placeholder="e.g. currency"><datalist id="bfCols">${Object.keys(rows[0].raw).concat(cols).map((c) => `<option>${esc(c)}</option>`).join("")}</datalist></label>
        <label>New value<input id="bfVal" placeholder="e.g. INR"></label></div>
      <div class="askform"><input id="bfNote" placeholder="Why is this correct? (shown to the approver)"></div>
      <div class="pacts"><button class="btn light btn-sm" id="bfCheck">Check with the rules</button><button class="btn btn-sm" id="bfSend">Request re-insert</button></div><div id="rjResult"></div></div>`;
    const body = () => {
      const col = $e("bfCol").value.trim(), val = $e("bfVal").value;
      return { entity, items: rows.map(itemOf), changes: col && val !== "" ? { [col]: val } : {}, note: $e("bfNote").value };
    };
    $e("bfCheck").onclick = async () => { try { showPreview(await dqpost("/reprocess/preview", body())); } catch (e) { $e("rjResult").innerHTML = `<div class="error-box">${esc(e.message)}</div>`; } };
    $e("bfSend").onclick = async () => {
      try {
        const r = await dqpost("/reprocess", body());
        toast(`Request #${r.request_id}: ${r.records} record(s)${r.skipped ? `, ${r.skipped} skipped (still failing)` : ""}`);
        rj.sel.clear(); renderEstate("estate-dq-rejected");
      } catch (e) { $e("rjResult").innerHTML = `<div class="error-box">${esc(e.message)}</div>`; }
    };
    panel.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }

  function bulkIgnore() {
    const { rows, ents } = selected();
    if (ents.length !== 1) return toast("Select records of one table at a time.");
    const panel = $e("rjPanel");
    panel.innerHTML = `<div class="rj-detail"><h3>Mark ${rows.length} record(s) as won't fix</h3>
      <p class="small muted">They stay out of silver and stay listed here, marked as reviewed with your note (e.g. test data, cancelled at the source).</p>
      <div class="askform"><input id="igNote" placeholder="Reason, e.g. test orders from the sandbox"><button id="igGo">Save</button></div><div id="rjResult"></div></div>`;
    $e("igGo").onclick = async () => {
      try { await dqpost("/review", { entity: ents[0], items: rows.map(itemOf), note: $e("igNote").value }); toast("Marked as reviewed"); rj.sel.clear(); renderEstate("estate-dq-rejected"); }
      catch (e) { $e("rjResult").innerHTML = `<div class="error-box">${esc(e.message)}</div>`; }
    };
  }

  // ================================================================ DQ RULE STUDIO
  const rs = { data: null, form: null, entity: "payments" };
  const PARAMS = {
    RANGE: [["min", "Minimum", "number"], ["max", "Maximum", "number"]],
    IN_LIST: [["values", "Allowed values (comma separated)", "text"]],
    REGEX: [["pattern", "Pattern (regular expression)", "text"]],
    CONTAINS: [["text", "Must contain", "text"]],
    MAX_LENGTH: [["max", "Max characters", "number"]],
  };

  async function pageRules() {
    const d = (rs.data = await dqapi("/rules"));
    const admin = isAdmin();
    const score = d.scorecard.map((s) => `<button class="tile rs-tile ${rs.entity === s.entity ? "on" : ""}" data-ent="${s.entity}">
        <div class="l">${esc(ENT_LABEL[s.entity] || s.entity)}</div><div class="v ${s.pass_rate < 99 ? "bad" : s.pass_rate < 100 ? "warn" : "ok"}">${s.pass_rate}%</div>
        <div class="small muted">${num(s.silver_rows)} loaded · ${num(s.rejected)} held back · ${s.rules} rule${s.rules === 1 ? "" : "s"}</div></button>`).join("");
    const rules = d.rules.filter((r) => r.entity === rs.entity);
    const rows = rules.map((r) => `<tr class="${r.active ? "" : "off"}"><td><b>${esc(r.rule_name)}</b>${r.built_in ? ` <span class="chip grey">built-in</span>` : ""}<div class="small muted">${esc(r.description || "")}</div></td>
        <td>${esc(r.text)}</td><td><span class="chip ${r.action === "REJECT" ? "bad" : "warn"}">${r.action === "REJECT" ? "Reject row" : "Warn only"}</span></td>
        <td class="num">${r.action === "REJECT" ? num(r.rejected_now) : num(r.warned_last_run)}<div class="small muted">${r.action === "REJECT" ? "held back now" : "warned last run"}</div></td>
        <td>${admin ? `<label class="switch"><input type="checkbox" data-toggle="${r.rule_id}" ${r.active ? "checked" : ""}><span>${r.active ? "On" : "Off"}</span></label>` : (r.active ? "On" : "Off")}</td>
        <td class="nowrap"><button class="btn light btn-sm" data-edit="${r.rule_id}">${admin ? "Edit" : "View"}</button>${admin && !r.built_in ? ` <button class="btn light btn-sm" data-del="${r.rule_id}">Delete</button>` : ""}</td></tr>`).join("");
    const ch = d.changes.map((c) => `<tr><td>${when(c.changed_at)}</td><td>${esc(ENT_LABEL[c.entity] || c.entity)}</td><td>${esc(c.rule_name)}</td><td>${esc(c.change)}</td><td>${esc(c.changed_by)}</td></tr>`).join("");
    return {
      html: `<div class="tiles rs-tiles">${score}</div>
        <div class="grid rs-layout">
          <div class="card"><div class="draft-head"><h3 style="margin:0">Rules for ${esc(ENT_LABEL[rs.entity] || rs.entity)}</h3><span class="small muted">checked in this order on every silver load; the first rejecting rule a row fails is its cause</span></div>
            <div class="tablewrap rs-wrap"><table class="rs-table"><thead><tr><th>Rule</th><th>Check</th><th>Action</th><th>Rows</th><th>Active</th><th></th></tr></thead><tbody>${rows || `<tr><td colspan="6" class="muted">No rules for this table.</td></tr>`}</tbody></table></div>
            <div class="pacts"><button class="btn btn-sm" id="rsNew">+ New rule</button><button class="btn light btn-sm" id="rsSuggest">✨ Suggest rules from the data</button></div>
            <div id="rsSuggestions"></div></div>
          <div class="card" id="rsEditor"></div>
        </div>
        <div class="card"><h3>Rule changes</h3><p class="small muted">Every change is recorded (audit.dq_rule_changes). Changes are used from the next silver run.</p>
          <div class="tablewrap"><table><thead><tr><th>When</th><th>Table</th><th>Rule</th><th>Change</th><th>By</th></tr></thead><tbody>${ch || `<tr><td colspan="5" class="muted">No changes yet. The 17 built-in rules are the ones the pipeline always had.</td></tr>`}</tbody></table></div></div>`,
      bind: bindRules,
    };
  }

  function bindRules() {
    const root = $e("body-estate-dq-rules");
    root.querySelectorAll("[data-ent]").forEach((b) => (b.onclick = () => { rs.entity = b.dataset.ent; rs.form = null; renderEstate("estate-dq-rules"); }));
    root.querySelectorAll("[data-edit]").forEach((b) => (b.onclick = () => {
      editor(rs.data.rules.find((r) => r.rule_id === +b.dataset.edit));
      $e("rsEditor").scrollIntoView({ behavior: "smooth", block: "start" });
    }));
    root.querySelectorAll("[data-toggle]").forEach((c) => (c.onchange = async () => {
      try { await dqpost(`/rules/${c.dataset.toggle}/${c.checked ? "on" : "off"}`); toast(`Rule switched ${c.checked ? "on" : "off"}: used from the next silver run`); renderEstate("estate-dq-rules"); }
      catch (e) { toast(e.message); c.checked = !c.checked; }
    }));
    root.querySelectorAll("[data-del]").forEach((b) => (b.onclick = async () => {
      if (b.dataset.sure !== "1") { b.dataset.sure = "1"; b.textContent = "Sure? Delete"; return; }
      try { await dqapi(`/rules/${b.dataset.del}`, { method: "DELETE" }); toast("Rule deleted"); renderEstate("estate-dq-rules"); } catch (e) { toast(e.message); }
    }));
    $e("rsNew").onclick = () => { editor(null); $e("rsEditor").scrollIntoView({ behavior: "smooth", block: "start" }); };
    $e("rsSuggest").onclick = async () => {
      const box = $e("rsSuggestions");
      box.innerHTML = `<p class="muted small">Profiling silver.${esc(rs.entity)}…</p>`;
      try {
        const s = await dqapi(`/rules-suggest?entity=${rs.entity}`);
        box.innerHTML = s.length ? `<h4>Ideas from today's data</h4>` + s.map((x, i) => `<div class="check rs-sugg"><div><b>${esc(x.text)}</b> <span class="chip ${x.action === "REJECT" ? "bad" : "warn"}">${x.action === "REJECT" ? "reject" : "warn"}</span>
            <div class="small muted">${esc(x.why)}</div></div><button class="btn light btn-sm" data-use="${i}">Use</button></div>`).join("")
          : `<p class="muted small">No new ideas: the existing rules already cover what the data shows.</p>`;
        box.querySelectorAll("[data-use]").forEach((b) => (b.onclick = () => {
          editor(Object.assign({}, s[+b.dataset.use], { description: s[+b.dataset.use].why }));
          $e("rsEditor").scrollIntoView({ behavior: "smooth", block: "start" });
        }));
      } catch (e) { box.innerHTML = `<div class="error-box">${esc(e.message)}</div>`; }
    };
    editor(rs.form);
  }

  function editor(rule) {
    const d = rs.data, admin = isAdmin();
    rs.form = rule;
    const r = Object.assign({ entity: rs.entity, rule_type: "NOT_NULL", column_name: "", params: {}, action: "REJECT", active: true, rule_name: "", description: "" }, rule || {});
    const box = $e("rsEditor");
    const cols = d.columns[r.entity] || [];
    const pfields = (t, p) => (PARAMS[t] || []).map(([k, lbl, typ]) => `<label>${lbl}<input data-p="${k}" type="${typ}" step="any" value="${esc(Array.isArray(p[k]) ? p[k].join(", ") : p[k] ?? "")}"></label>`).join("")
      + (t !== "NOT_NULL" ? `<label class="inline"><input type="checkbox" data-p="allow_empty" ${p.allow_empty ? "checked" : ""}> Empty value is OK</label>` : "");
    box.innerHTML = `<h3>${r.rule_id ? (admin ? "Edit rule" : "Rule") : "New rule"}</h3>
      <div class="askform"><input id="rsText" placeholder="Describe it, e.g. “fee must be between 0 and 500” or “method must be one of UPI, CARD”"><button id="rsDraft">Draft</button></div>
      <p class="small muted" id="rsDraftMsg">Plain words are turned into a rule (the AI model helps when the words are unclear). Nothing is saved until you click Save.</p>
      <div class="form-grid">
        <label>Table<select id="rsEnt" ${r.rule_id ? "disabled" : ""}>${d.entities.map((e) => `<option value="${e}" ${e === r.entity ? "selected" : ""}>${esc(ENT_LABEL[e] || e)}</option>`).join("")}</select></label>
        <label>Column<select id="rsCol">${cols.map((c) => `<option ${c === r.column_name ? "selected" : ""}>${esc(c)}</option>`).join("")}</select></label>
        <label>Check<select id="rsType">${Object.entries(d.types).map(([k, v]) => `<option value="${k}" ${k === r.rule_type ? "selected" : ""}>${esc(v)}</option>`).join("")}</select></label>
        <label>When a row fails<select id="rsAct"><option value="REJECT" ${r.action === "REJECT" ? "selected" : ""}>Reject it (hold back from silver)</option><option value="WARN" ${r.action === "WARN" ? "selected" : ""}>Load it, but warn</option></select></label>
        <div id="rsParams" class="rs-params">${pfields(r.rule_type, r.params || {})}</div>
        <label>Rule name<input id="rsName" value="${esc(r.rule_name)}" placeholder="e.g. FEE_IN_RANGE" ${r.built_in ? "disabled" : ""}></label>
        <label class="wide">Description<input id="rsDesc" value="${esc(r.description || "")}" placeholder="what it protects, for the people reading the rejected list"></label></div>
      <div class="pacts"><button class="btn light btn-sm" id="rsTest">Test on today's data</button>${admin ? `<button class="btn btn-sm" id="rsSave">${r.rule_id ? "Save changes" : "Save rule"}</button>` : `<span class="small muted">Only an admin can save rules.</span>`}</div>
      <div id="rsResult"></div>`;
    const val = () => {
      const params = {};
      box.querySelectorAll("[data-p]").forEach((i) => (params[i.dataset.p] = i.type === "checkbox" ? i.checked : i.value));
      return { rule_id: r.rule_id || null, entity: $e("rsEnt").value, column_name: $e("rsCol").value, rule_type: $e("rsType").value, action: $e("rsAct").value,
        params, rule_name: $e("rsName").value.trim() || `${$e("rsCol").value.toUpperCase()}_${$e("rsType").value}`, description: $e("rsDesc").value, active: r.active !== false };
    };
    $e("rsType").onchange = () => ($e("rsParams").innerHTML = pfields($e("rsType").value, {}));
    $e("rsEnt").onchange = () => editor(Object.assign(val(), { entity: $e("rsEnt").value, column_name: "" }));
    const draft = async () => {
      const t = $e("rsText").value.trim();
      if (!t) return;
      $e("rsDraftMsg").textContent = "Drafting…";
      try {
        const x = await dqpost("/rules-draft", { entity: $e("rsEnt").value, text: t });
        editor(Object.assign({}, x, { rule_id: r.rule_id, built_in: r.built_in, rule_name: r.built_in ? r.rule_name : x.rule_name, description: r.description || t }));
        $e("rsDraftMsg").innerHTML = `Drafted (${esc(x.drafted_by)}): <b>${esc(x.text)}</b>. Check the fields, test it, then save.`;
      } catch (e) { $e("rsDraftMsg").innerHTML = `<span class="bad">${esc(e.message)}</span>`; }
    };
    $e("rsDraft").onclick = draft;
    $e("rsText").onkeydown = (e) => { if (e.key === "Enter") draft(); };
    $e("rsTest").onclick = async () => {
      const out = $e("rsResult");
      out.innerHTML = `<p class="muted small">Testing…</p>`;
      try {
        const t = await dqpost("/rules-test", val());
        out.innerHTML = `<div class="draft"><div class="draft-head"><b>${esc(t.rule)}</b><span class="chip ${t.failing ? (t.action === "REJECT" ? "bad" : "warn") : "ok"}">${num(t.failing)} of ${num(t.total)} rows fail</span><span class="small muted">pass rate ${t.pass_rate}%</span></div>
          ${t.failing ? `<div class="small">${t.action === "REJECT" ? "These rows would be <b>held back</b> on the next run" : "These rows would be loaded with a <b>warning</b>"}: ${t.examples.map((x) => `<span class="mono">${esc(x.record_key)}</span> (${x.value == null ? "empty" : esc(x.value)})`).join(", ")}${t.failing > t.examples.length ? " …" : ""}</div>
            ${t.by_date.length ? `<div class="small muted">By date: ${t.by_date.map((x) => `${esc(x.business_date)}: ${num(x.n)}`).join(" · ")}</div>` : ""}` : `<div class="small">No row in silver fails it today.</div>`}
          ${t.held_back_would_pass ? `<div class="small ok">With this rule, ${num(t.held_back_would_pass)} row(s) held back today would pass: re-insert them from Rejected records after saving.</div>` : ""}
          ${t.failing / Math.max(t.total, 1) > 0.05 && t.action === "REJECT" ? `<div class="small bad">Careful: more than 5% of the rows would be rejected. Consider “Load it, but warn” first.</div>` : ""}
          <div class="small muted">${esc(t.note)}</div></div>`;
      } catch (e) { out.innerHTML = `<div class="error-box">${esc(e.message)}</div>`; }
    };
    if ($e("rsSave")) $e("rsSave").onclick = async () => {
      try {
        const res = await dqpost("/rules", val());
        toast(`Saved: ${res.text}. Used from the next silver run.`);
        rs.entity = $e("rsEnt").value; rs.form = null;
        const rc = await dqapi(`/recheck?entity=${rs.entity}`).catch(() => null);
        await renderEstate("estate-dq-rules");
        if (rc && rc.count) $e("rsResult").innerHTML = `<div class="draft"><b>${num(rc.count)} held-back ${esc(ENT_LABEL[rs.entity] || rs.entity)} row(s) now pass all rules.</b>
          <button class="btn btn-sm" id="rsGoRej">Re-insert them from Rejected records</button></div>`;
        if ($e("rsGoRej")) $e("rsGoRej").onclick = () => { Object.assign(rj.f, { entity: rs.entity, rule: "", view: "open" }); document.querySelector('#tabs button[data-page="estate-dq-rejected"]').click(); };
      } catch (e) { $e("rsResult").innerHTML = `<div class="error-box">${esc(e.message)}</div>`; }
    };
  }

  window.DQ = {
    page: (page) => (page === "estate-dq-rules" ? pageRules() : pageRejected()),
    open(date, entity, rule) {                                 // from Detect / Investigate / a link
      this.showRejected({ date_from: date || "", date_to: date || "", entity: entity || "", rule: rule || "" });
      document.querySelector('#tabs button[data-page="estate-dq-rejected"]').click();
    },
    showRejected(f) { Object.assign(rj.f, { date_from: "", date_to: "", entity: "", rule: "", q: "", view: "open" }, f || {}); },
  };
})();
