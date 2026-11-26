// Audit & approvals (admins): every decision made in DataTrust, as one decision log.
//   tiles     what is on record: waiting, approved, rejected, failed, self-approved (the filters do not change them)
//   waiting   requests still waiting for a decision, with where to decide them (only when there are any)
//   log       one table for every kind, with tabs (All / Access requests / Re-inserts / Incident fixes), one filter bar
//             and the same columns for every kind: When · Type · What · Decision · Outcome · Asked by · Decided by
// Read only: decisions are made on Access, Rejected records and Impact & decide. Depends on app.js (esc, num) and estate.js.
(function () {
  const PAGE = "estate-history";
  const TABS = { "": "All", access: "Access requests", reinsert: "Re-inserts", remediation: "Incident fixes" };
  const TYPE = {   // short name, chip colour, where the decision is made, what it is
    access: { name: "Access request", where: "estate-access", whereLabel: "Access", what: "someone asked for access to a database; an admin grants or refuses it" },
    reinsert: { name: "Re-insert", where: "estate-dq-rejected", whereLabel: "Rejected records", what: "someone corrected rows a quality rule held back and asked to load them" },
    remediation: { name: "Incident fix", where: "verify", whereLabel: "Impact & decide", what: "someone requested a fix for an incident and an admin approved or rejected it on Impact & decide (fixes before requests existed were approved directly)" },
  };
  const DECISIONS = { pending: "Waiting", approved: "Approved", rejected: "Rejected", failed: "Failed", closed: "Closed" };
  const EMPTY = { kind: "", decision: "", person: "", date_from: "", date_to: "", q: "" };
  let f = { ...EMPTY };

  const when = (t) => (t ? `<span style="white-space:nowrap">${esc(String(t).slice(0, 16).replace("T", " "))}</span>` : "–");
  const qs = (x) => new URLSearchParams(Object.entries(x).filter(([, v]) => v)).toString();
  const opt = (map, cur, all) => `<option value="">${all}</option>` + Object.entries(map)
    .map(([k, v]) => `<option value="${esc(k)}"${k === cur ? " selected" : ""}>${esc(v)}</option>`).join("");
  const took = (from, to) => {                                  // how long the decision took
    const ms = from && to ? new Date(String(to).replace(" ", "T")) - new Date(String(from).replace(" ", "T")) : NaN;
    if (!(ms >= 0)) return "";
    const m = Math.round(ms / 60000);
    return m < 1 ? "at once" : m < 60 ? `after ${m} min` : m < 2880 ? `after ${Math.round(m / 60)} h` : `after ${Math.round(m / 1440)} days`;
  };
  const tone = (d) => (d === "approved" ? "ok" : d === "rejected" || d === "failed" ? "bad" : d === "pending" ? "warn" : "grey");
  const decisionChip = (r) => `<span class="chip ${tone(r.decision)}" title="status ${esc(r.status || "")}">${esc(r.decision_label || DECISIONS[r.decision] || r.status || "")}</span>`;
  const typeChip = (k) => `<span class="ha-type ha-${esc(k)}">${esc(TYPE[k].name)}</span>`;
  // what happened after the decision; for an incident fix, also what became of the incident
  function outcome(r) {
    const x = r.result;
    const tip = x && x.state === "unknown" ? "Approved before DataTrust kept a life cycle for each incident, so what happened next is not on record."
      : "What became of the incident after the fix was approved";
    const now = x ? `<div class="ha-now"><span class="small muted">incident:</span> <span class="chip ${x.state === "verified" ? "ok" : x.state === "open" ? "warn" : "grey"}" title="${esc(tip)}">${esc(x.label)}</span>${x.at ? `<div class="small muted">${when(x.at)}</div>` : ""}</div>` : "";
    return `${r.outcome ? `<div class="small">${esc(r.outcome)}</div>` : r.decision === "pending" ? `<div class="small muted">waiting</div>` : ""}${now}`;
  }
  function link(r) {
    const t = TYPE[r.kind];
    const href = r.kind === "remediation" ? `#verify=${encodeURIComponent(r.ref)}` : `#${t.where}`;
    return `<a href="${href}" title="${r.kind === "remediation" ? "This incident in Verify → Resolved incidents" : "Where this is decided"}">${esc(t.whereLabel)} →</a>`;
  }

  function row(r) {
    const fix = r.kind === "remediation";
    return `<tr>
      <td class="small">${when(r.at)}</td>
      <td>${typeChip(r.kind)}<div class="small muted">${esc(r.ref)}</div></td>
      <td>${esc(r.what)}${r.detail ? `<div class="small muted ha-detail" title="${esc(r.detail)}">${esc(r.detail)}</div>` : ""}</td>
      <td>${decisionChip(r)}</td>
      <td>${outcome(r)}</td>
      <td class="small">${fix ? `<span class="muted" title="${esc(TYPE.remediation.what)}">no request</span>` : `${esc(r.requested_by || "–")}<div class="muted">${when(r.requested_at)}</div>`}</td>
      <td class="small">${r.decided_by ? `<b>${esc(r.decided_by)}</b>${r.self_approved ? ` <span class="chip warn" title="The same person asked and approved. A second admin would normally decide.">self-approved</span>` : ""}${fix ? "" : `<div class="muted">${esc(took(r.requested_at, r.decided_at))}</div>`}` : "<span class='muted'>not decided yet</span>"}</td>
      <td class="small" style="white-space:nowrap">${link(r)}</td></tr>`;
  }

  function tiles(t) {
    const w = t.pending_by_kind || {};
    const kpi = (cls, label, v, sub) => `<div class="kpi ${cls}"><div class="l">${label}</div><div class="v">${num(v)}</div><div class="small muted">${sub}</div></div>`;
    return `<div class="kpis k5">
      ${kpi(t.pending ? "warn" : "", "Waiting for a decision", t.pending, t.pending ? `${num(w.access || 0)} access · ${num(w.reinsert || 0)} re-insert · ${num(w.remediation || 0)} fix` : "nothing to decide")}
      ${kpi("", "Approved", t.approved, "requests and incident fixes")}
      ${kpi("", "Rejected", t.rejected, "turned down by an admin")}
      ${kpi(t.failed ? "bad" : "", "Failed", t.failed, "approved, but could not be applied")}
      ${kpi(t.self_approved ? "warn" : "", "Self-approved", t.self_approved, "requests asked and approved by one person")}</div>`;
  }

  function waitingCard(list) {
    if (!list.length) return "";
    return `<div class="card"><h3>Waiting for a decision <span class="chip warn">${list.length}</span></h3>
      <p class="small muted">Oldest first. Decisions are made where the request lives; this page only records them.</p>
      ${list.map((r) => `<div class="check ha-wait"><span class="grey">⏳</span><span class="grow">${typeChip(r.kind)} <b>${esc(r.what)}</b> <span class="small muted">${esc(r.ref)}</span>
        <div class="small muted">asked by ${esc(r.requested_by || "–")} · ${when(r.requested_at)}${r.detail ? ` · ${esc(r.detail)}` : ""}</div></span>
        <a class="btn light btn-sm" href="#${esc(r.link || TYPE[r.kind].where)}">Decide on ${esc(TYPE[r.kind].whereLabel)} →</a></div>`).join("")}</div>`;
  }

  function logCard(d) {
    const tab = (k) => `<button type="button" data-ha-tab="${k}" class="${f.kind === k ? "active" : ""}">${TABS[k]} <span class="ha-n">${num(k ? d.by_kind[k] || 0 : Object.values(d.by_kind).reduce((a, b) => a + b, 0))}</span></button>`;
    const what = f.kind ? `<p class="small muted ha-explain"><b>${esc(TYPE[f.kind].name)}:</b> ${esc(TYPE[f.kind].what)}.</p>`
      : `<p class="small muted ha-explain">${Object.values(TYPE).map((t) => `<b>${esc(t.name)}</b>: ${esc(t.what)}.`).join("<br>")}</p>`;
    return `<div class="card" id="ha-log">
      <div class="draft-head"><h3 style="margin:0">Decision log</h3><span class="small muted">every decision, newest first</span></div>
      <div class="seg ha-tabs" role="tablist">${Object.keys(TABS).map(tab).join("")}</div>
      ${what}
      ${d.errors.length ? `<div class="error-box">Some history could not be read: ${d.errors.map(esc).join("; ")}</div>` : ""}
      <div class="rj-filters">
        <label>Decision<select id="ha-decision">${opt(DECISIONS, f.decision, "Any decision")}</select></label>
        <label>Person<input id="ha-person" placeholder="asked or decided" value="${esc(f.person)}"></label>
        <label>From<input type="date" id="ha-from" value="${esc(f.date_from)}"></label>
        <label>To<input type="date" id="ha-to" value="${esc(f.date_to)}"></label>
        <label class="grow">Search<input id="ha-q" placeholder="number, incident id, table, user…" value="${esc(f.q)}"></label>
        <span class="rj-actions"><button class="btn btn-sm" id="ha-apply">Show</button><button class="btn light btn-sm" id="ha-clear">Clear</button>
        <a class="btn light btn-sm" href="/api/estate/history/approvals.csv?${qs(f)}" download>⬇ CSV</a></span></div>
      <div class="tablewrap" style="max-height:620px"><table class="ha-table"><thead><tr>
        <th>When</th><th>Type</th><th>What</th><th>Decision</th><th>Outcome</th><th>Asked by</th><th>Decided by</th><th></th></tr></thead>
        <tbody>${d.rows.map(row).join("") || `<tr><td colspan="8" class="muted">Nothing for these filters.</td></tr>`}</tbody></table></div></div>`;
  }

  // #estate-history=access | reinsert | remediation  (the "View all history" links on other pages) opens that tab
  function filter(arg) {
    f = { ...EMPTY, kind: TABS[arg] !== undefined ? arg : "" };
  }

  async function page() {
    if (est.user.role !== "admin") {
      return { html: `<div class="card"><h3>Admins only</h3><p class="muted">The approval history is visible to admins. Ask an admin if you need a record from it.</p></div>`, bind() {} };
    }
    const d = await eapi("/history/approvals?" + qs(f));
    return { html: tiles(d.totals) + waitingCard(d.waiting || []) + logCard(d), bind };
  }

  function bind() {
    if (!$e("ha-apply")) return;
    const v = (s) => $e(s).value.trim();
    const read = () => ({ kind: f.kind, decision: v("ha-decision"), person: v("ha-person"), date_from: v("ha-from"), date_to: v("ha-to"), q: v("ha-q") });
    const go = (x) => { f = x; renderEstate(PAGE); };
    document.querySelectorAll("[data-ha-tab]").forEach((b) => (b.onclick = () => go({ ...read(), kind: b.dataset.haTab })));
    $e("ha-apply").onclick = () => go(read());
    $e("ha-decision").onchange = () => go(read());
    ["ha-person", "ha-q"].forEach((s) => ($e(s).onkeydown = (e) => { if (e.key === "Enter") go(read()); }));
    $e("ha-clear").onclick = () => go({ ...EMPTY, kind: f.kind });
  }

  window.HISTORY = { page, filter };
})();
