// Audit & approvals (admins): every approval decision, in two blocks with their own filters and CSV export:
//   1. requests: access requests and re-inserts of rejected rows
//   2. incident fixes: there are many of these, so they get a block of their own
// Read only. Depends on app.js (esc) and estate.js (eapi, est, renderEstate).
(function () {
  const PAGE = "estate-history";
  const REQUEST_KINDS = { access: "Access requests", reinsert: "Re-insert rejected rows" };
  const DECISIONS = { pending: "Waiting", approved: "Approved", rejected: "Rejected", failed: "Failed", closed: "Closed" };
  const LINK_LABEL = { "estate-access": "Access", "estate-dq-rejected": "Rejected records", verify: "Verify" };
  const EMPTY = { kind: "", decision: "", person: "", date_from: "", date_to: "", q: "" };
  const BLOCKS = {
    req: { title: "Requests", sub: "Access requests and re-inserts of rejected rows: who asked, who decided and when.",
           kinds: REQUEST_KINDS, all: "access,reinsert", search: "request number, table, user…" },
    inc: { title: "Incident fixes", sub: "Fixes approved by an admin, newest first. An admin approves the fix directly, so they both asked and decided.",
           kinds: null, all: "remediation", search: "incident id or title…" },
  };
  let f = { req: { ...EMPTY }, inc: { ...EMPTY } };
  const when = (t) => (t ? `<span style="white-space:nowrap">${esc(String(t).slice(0, 16).replace("T", " "))}</span>` : "–");
  const qs = (x) => new URLSearchParams(Object.entries(x).filter(([, v]) => v)).toString();
  const opt = (map, cur, all) => `<option value="">${all}</option>` + Object.entries(map)
    .map(([k, v]) => `<option value="${esc(k)}"${k === cur ? " selected" : ""}>${esc(v)}</option>`).join("");
  const chip = (d, status) => `<span class="chip ${d === "approved" ? "ok" : d === "rejected" || d === "failed" ? "bad" : d === "pending" ? "warn" : "grey"}">${esc(status || "")}</span>`;
  const query = (b) => ({ ...f[b], kind: f[b].kind || BLOCKS[b].all });

  // #estate-history=access | reinsert | remediation  (the "View all history" links on other pages)
  function filter(arg) {
    f = { req: { ...EMPTY, kind: REQUEST_KINDS[arg] ? arg : "" }, inc: { ...EMPTY } };
    if (arg === "remediation") setTimeout(() => $e("ha-inc") && $e("ha-inc").scrollIntoView({ block: "start" }), 300);
  }

  function requestRow(r) {
    return `<tr>
      <td class="small">${when(r.at)}</td>
      <td><b>${esc(r.kind_label)}</b><div class="small muted">${esc(r.ref)}</div></td>
      <td>${esc(r.what)}${r.detail ? `<div class="small muted">${esc(r.detail)}</div>` : ""}</td>
      <td>${chip(r.decision, r.status)}</td>
      <td class="small">${esc(r.requested_by || "–")}<div class="muted">${when(r.requested_at)}</div></td>
      <td class="small">${r.decided_by ? `<b>${esc(r.decided_by)}</b><div class="muted">${when(r.decided_at)}</div>` : "<span class='muted'>not decided yet</span>"}</td>
      <td class="small" style="white-space:nowrap"><a href="#${esc(r.link)}">${esc(LINK_LABEL[r.link] || "Open")} →</a></td></tr>`;
  }
  function incidentRow(r) {
    return `<tr>
      <td class="small">${when(r.at)}</td>
      <td><b>${esc(r.ref)}</b></td>
      <td>${esc(r.what)}${r.detail ? `<div class="small muted">${esc(r.detail)}</div>` : ""}</td>
      <td>${chip(r.decision, r.status)}</td>
      <td class="small">${esc(r.decided_by || r.requested_by || "–")}</td>
      <td class="small" style="white-space:nowrap"><a href="#${esc(r.link)}">${esc(LINK_LABEL[r.link] || "Open")} →</a></td></tr>`;
  }

  function block(b, d) {
    const B = BLOCKS[b], x = f[b], id = (s) => `ha-${b}-${s}`;
    const head = b === "req"
      ? "<th>When</th><th>Type</th><th>What</th><th>Decision</th><th>Asked by</th><th>Decided by</th><th></th>"
      : "<th>When</th><th>Incident</th><th>What</th><th>Decision</th><th>Approved by</th><th></th>";
    const cols = b === "req" ? 7 : 6;
    const rows = d.rows.map(b === "req" ? requestRow : incidentRow).join("");
    return `<div class="card" id="ha-${b}">
      <h3>${B.title} <span class="chip grey">${d.rows.length}</span></h3>
      <p class="small muted">${B.sub}</p>
      ${d.errors.length ? `<div class="error-box">Some history could not be read: ${d.errors.map(esc).join("; ")}</div>` : ""}
      <div class="rj-filters">
        ${B.kinds ? `<label>Type<select id="${id("kind")}">${opt(B.kinds, x.kind, "All types")}</select></label>` : ""}
        <label>Decision<select id="${id("decision")}">${opt(DECISIONS, x.decision, "Any decision")}</select></label>
        <label>Person<input id="${id("person")}" placeholder="${b === "req" ? "asked or decided" : "approved by"}" value="${esc(x.person)}"></label>
        <label>From<input type="date" id="${id("from")}" value="${esc(x.date_from)}"></label>
        <label>To<input type="date" id="${id("to")}" value="${esc(x.date_to)}"></label>
        <label class="grow">Search<input id="${id("q")}" placeholder="${B.search}" value="${esc(x.q)}"></label>
        <button class="btn btn-sm" id="${id("apply")}">Show</button><button class="btn light btn-sm" id="${id("clear")}">Clear</button>
        <a class="btn light btn-sm" href="/api/estate/history/approvals.csv?${qs(query(b))}" download>⬇ CSV</a></div>
      <div class="tablewrap" style="max-height:520px"><table><thead><tr>${head}</tr></thead>
        <tbody>${rows || `<tr><td colspan="${cols}" class="muted">Nothing for these filters.</td></tr>`}</tbody></table></div></div>`;
  }

  async function page() {
    if (est.user.role !== "admin") {
      return { html: `<div class="card"><h3>Admins only</h3><p class="muted">The approval history is visible to admins. Ask an admin if you need a record from it.</p></div>`, bind() {} };
    }
    const [req, inc] = await Promise.all(["req", "inc"].map((b) => eapi("/history/approvals?" + qs(query(b)))));
    return { html: block("req", req) + block("inc", inc), bind };
  }

  function bind() {
    ["req", "inc"].forEach((b) => {
      const id = (s) => `ha-${b}-${s}`, v = (s) => ($e(id(s)) ? $e(id(s)).value.trim() : "");
      if (!$e(id("apply"))) return;
      const go = () => {
        f[b] = { kind: v("kind"), decision: v("decision"), person: v("person"), date_from: v("from"), date_to: v("to"), q: v("q") };
        renderEstate(PAGE);
      };
      $e(id("apply")).onclick = go;
      ["kind", "decision"].forEach((s) => $e(id(s)) && ($e(id(s)).onchange = go));
      ["person", "q"].forEach((s) => ($e(id(s)).onkeydown = (e) => { if (e.key === "Enter") go(); }));
      $e(id("clear")).onclick = () => { f[b] = { ...EMPTY }; renderEstate(PAGE); };
    });
  }

  window.HISTORY = { page, filter };
})();
