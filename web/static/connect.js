// Connect: data owners directory + one-click Google Chat / Meet / Calendar / Gmail, and "Contact owner" buttons.
// Uses the company's own Google Workspace (no calling infrastructure of our own). Needs a portal login (e-mails are internal).
// Depends on app.js (esc, toast) and estate.js (eapi, post, est, renderEstate).
(function () {
  const COLORS = ["#0672A1", "#EF802C", "#11805A", "#7A4BC2", "#C2410C", "#0E7490", "#B4600B", "#4D5BD6"];
  const initials = (n) => n.split(/\s+/).filter(Boolean).slice(0, 2).map((w) => w[0].toUpperCase()).join("");
  const color = (n) => COLORS[[...n].reduce((a, c) => a + c.charCodeAt(0), 0) % COLORS.length];
  const first = (n) => n.split(/\s+/)[0];
  const open = (url) => window.open(url, "_blank", "noopener");
  const ICON = {
    chat: '<svg viewBox="0 0 24 24"><path d="M21 12a8 8 0 0 1-11.6 7.1L4 20l1.1-4.6A8 8 0 1 1 21 12z"/></svg>',
    meet: '<svg viewBox="0 0 24 24"><rect x="3" y="6" width="12" height="12" rx="2"/><path d="M15 10l6-3v10l-6-3"/></svg>',
    cal: '<svg viewBox="0 0 24 24"><rect x="3.5" y="5" width="17" height="15" rx="2"/><path d="M3.5 10h17M8 3v4M16 3v4"/></svg>',
    mail: '<svg viewBox="0 0 24 24"><rect x="3" y="5" width="18" height="14" rx="2"/><path d="M3.5 6.5L12 13l8.5-6.5"/></svg>',
    phone: '<svg viewBox="0 0 24 24"><path d="M5 4h4l2 5-2.5 1.5a11 11 0 0 0 5 5L15 13l5 2v4a2 2 0 0 1-2 2A16 16 0 0 1 3 6a2 2 0 0 1 2-2"/></svg>',
  };

  async function copy(text) {
    try { await navigator.clipboard.writeText(text); return true; } catch (e) { /* fall back below */ }
    try {
      const t = document.createElement("textarea"); t.value = text; t.style.position = "fixed"; t.style.opacity = "0";
      document.body.appendChild(t); t.select(); const ok = document.execCommand("copy"); t.remove(); return ok;
    } catch (e) { return false; }
  }
  const message = (p, context) => context
    ? `Hi ${first(p.name)}, about "${context}" in DataTrust AI (${location.origin}): could you take a look? Thanks!`
    : `Hi ${first(p.name)}, reaching out from DataTrust AI (${location.origin}).`;
  const calDate = (d) => `${d.getFullYear()}${String(d.getMonth() + 1).padStart(2, "0")}${String(d.getDate()).padStart(2, "0")}T${String(d.getHours()).padStart(2, "0")}${String(d.getMinutes()).padStart(2, "0")}00`;

  // ---------------------------------------------------------------- the four actions (Google Workspace)
  const act = {
    async chat(p, context) {
      const copied = await copy(message(p, context));
      open(p.chat_link || "https://chat.google.com/");
      toast(p.chat_link ? `Message ${copied ? "copied" : "ready"}: paste it in the chat with ${first(p.name)} (Ctrl+V).`
        : `Message ${copied ? "copied" : "ready"}. In Google Chat: New chat → ${p.email} → paste (Ctrl+V).`);
    },
    async meet(p, context) {
      const copied = await copy(p.email);
      open("https://meet.google.com/new");
      toast(`Google Meet opened. Click “Add others” and ${copied ? "paste" : "type"} ${p.email} to invite ${first(p.name)}.`);
    },
    schedule(p, context) {
      const start = new Date(); start.setMinutes(start.getMinutes() < 30 ? 30 : 60, 0, 0);
      const end = new Date(start.getTime() + 30 * 60000);
      const q = new URLSearchParams({ action: "TEMPLATE", text: `DataTrust: ${context || "quick sync"}`.slice(0, 120),
        details: message(p, context), add: p.email, dates: `${calDate(start)}/${calDate(end)}` });
      open("https://calendar.google.com/calendar/render?" + q.toString());
      toast("Google Calendar opened with the invite filled in. Check the time and click Save to send it.");
    },
    email(p, context) {
      const q = new URLSearchParams({ view: "cm", fs: "1", to: p.email, su: `DataTrust: ${context || "question"}`.slice(0, 150), body: message(p, context) });
      open("https://mail.google.com/mail/?" + q.toString());
    },
  };
  const actionButtons = (p, i) => `
    <button class="cbtn" data-act="chat" data-i="${i}" title="Google Chat: message copied, chat opens">${ICON.chat}Chat</button>
    <button class="cbtn" data-act="meet" data-i="${i}" title="Start a Google Meet now and invite them">${ICON.meet}Meet now</button>
    <button class="cbtn" data-act="schedule" data-i="${i}" title="Google Calendar invite, filled in">${ICON.cal}Schedule</button>
    <button class="cbtn" data-act="email" data-i="${i}" title="Gmail, filled in">${ICON.mail}Email</button>
    ${p.phone ? `<a class="cbtn" href="tel:${esc(p.phone.replace(/[^\d+]/g, ""))}" title="${esc(p.phone)}">${ICON.phone}Phone</a>` : ""}`;
  const bindActions = (root, people, context) => root.querySelectorAll("[data-act]").forEach((b) =>
    (b.onclick = (e) => { e.stopPropagation(); act[b.dataset.act](people[b.dataset.i], context); }));

  // ---------------------------------------------------------------- the Connect page (rendered by estate.js after login)
  let filterText = "";
  async function page() {
    const people = await eapi("/people");
    const admin = est.user.role === "admin";
    const teams = [...new Set(people.map((p) => p.team).filter(Boolean))].sort();
    const card = (p, i) => `
      <div class="pcard" data-search="${esc([p.name, p.email, p.team, p.title, ...(p.owns || [])].join(" ").toLowerCase())}">
        <div class="phead"><span class="avatar" style="background:${color(p.name)}">${esc(initials(p.name))}</span>
          <div class="pmeta"><b>${esc(p.name)}</b><span>${esc([p.title, p.team].filter(Boolean).join(" · ") || "—")}</span>
            <a href="mailto:${esc(p.email)}" class="pmail">${esc(p.email)}</a></div>
          ${admin ? `<div class="padmin"><button class="linkbtn" data-edit="${i}">Edit</button><button class="linkbtn danger" data-del="${p.person_id}" data-name="${esc(p.name)}">Remove</button></div>` : ""}</div>
        <div class="powns">${(p.owns || []).length ? p.owns.map((o) => `<span class="chip grey">${esc(o)}</span>`).join(" ") : `<span class="small muted">No data assigned yet</span>`}</div>
        <div class="pacts">${actionButtons(p, i)}</div>
      </div>`;
    return {
      html: `
        <div class="card connect-bar">
          <input id="cnSearch" placeholder="Search by name, team, e-mail or data (e.g. silver.payments)" value="${esc(filterText)}">
          <span class="small muted" id="cnCount"></span>
          ${admin ? `<button class="btn" id="cnAdd">+ Add person</button>` : ""}
        </div>
        ${admin ? `<div class="card hidden" id="cnForm"></div><div class="card alerts-card" id="cnAlerts"><p class="muted small">Loading alert settings…</p></div>` : ""}
        ${people.length ? `<div class="pgrid" id="cnGrid">${people.map(card).join("")}</div>`
          : `<div class="card"><h3>No people yet</h3><p class="muted">${admin ? "Add the data owners and stewards with <b>+ Add person</b>: their company e-mail and the data they own. Then anyone can reach the right person in one click, and incidents, lineage and the catalog show a <b>Contact owner</b> button."
              : "An admin has not added any people yet."}</p></div>`}
        <p class="small muted">Chat, Meet and Calendar open in your company's Google Workspace in a new tab, signed in as you. Nothing is sent until you press send there.
          ${teams.length ? ` Teams: ${teams.map(esc).join(", ")}.` : ""}</p>`,
      bind() {
        const grid = $e("cnGrid");
        const apply = () => {
          const q = filterText.trim().toLowerCase();
          let n = 0;
          document.querySelectorAll(".pcard").forEach((c) => { const on = !q || c.dataset.search.includes(q); c.classList.toggle("hidden", !on); n += on; });
          $e("cnCount").textContent = `${n} of ${people.length} people`;
        };
        $e("cnSearch").oninput = (e) => { filterText = e.target.value; apply(); };
        apply();
        if (grid) bindActions(grid, people, "");
        if (!admin) return;
        renderAlerts();
        const form = $e("cnForm");
        const showForm = (p) => {
          form.classList.remove("hidden");
          form.innerHTML = `<h3>${p ? "Edit " + esc(p.name) : "Add a person"}</h3>
            <div class="form-grid">
              <label>Full name *<input id="pfName" value="${esc(p ? p.name : "")}" placeholder="e.g. Priya Sharma"></label>
              <label>Company e-mail *<input id="pfEmail" value="${esc(p ? p.email : "")}" placeholder="name@company.com" ${p ? "readonly" : ""}></label>
              <label>Team<input id="pfTeam" value="${esc(p ? p.team || "" : "")}" placeholder="e.g. Payments data"></label>
              <label>Role / title<input id="pfTitle" value="${esc(p ? p.title || "" : "")}" placeholder="e.g. Data owner"></label>
              <label>Phone (optional)<input id="pfPhone" value="${esc(p ? p.phone || "" : "")}" placeholder="+91 ..."></label>
              <label>Google Chat link (optional)<input id="pfChat" value="${esc(p ? p.chat_link || "" : "")}" placeholder="https://chat.google.com/dm/..."></label>
            </div>
            <label class="wide-label">Data they own<input id="pfOwns" value="${esc(p ? (p.owns || []).join(", ") : "")}" placeholder="e.g. silver.payments, settlements, gold.*"></label>
            <p class="small muted">Comma separated. <code>silver.payments</code> = one table · <code>payments</code> = that data in every layer · <code>gold.*</code> = a whole schema.
              Google Chat link: open the chat with this person in Google Chat and copy the address from the browser; then <b>Chat</b> opens that chat directly.</p>
            <div id="pfErr"></div>
            <button class="btn" id="pfSave">Save</button> <button class="btn light" id="pfCancel">Cancel</button>`;
          form.scrollIntoView({ behavior: "smooth", block: "nearest" });
          $e("pfCancel").onclick = () => form.classList.add("hidden");
          $e("pfSave").onclick = async () => {
            try {
              await post("/people", { name: $e("pfName").value, email: $e("pfEmail").value, team: $e("pfTeam").value, title: $e("pfTitle").value,
                phone: $e("pfPhone").value, chat_link: $e("pfChat").value, owns: $e("pfOwns").value.split(",").map((x) => x.trim()).filter(Boolean) });
              toast(`${$e("pfName").value} saved`);
              renderEstate("estate-people");
            } catch (e) { $e("pfErr").innerHTML = `<div class="error-box">${esc(e.message)}</div>`; }
          };
        };
        $e("cnAdd").onclick = () => showForm(null);
        document.querySelectorAll("[data-edit]").forEach((b) => (b.onclick = () => showForm(people[b.dataset.edit])));
        document.querySelectorAll("[data-del]").forEach((b) => (b.onclick = async () => {
          if (!confirm(`Remove ${b.dataset.name} from Connect? (Only the directory entry; nothing else changes.)`)) return;
          try { await eapi(`/people/${b.dataset.del}`, { method: "DELETE" }); toast("Removed"); renderEstate("estate-people"); } catch (e) { toast(e.message); }
        }));
      },
    };
  }

  // ---------------------------------------------------------------- Google Chat alerts (admin card on Connect)
  async function renderAlerts() {
    const box = $e("cnAlerts");
    if (!box) return;
    let st;
    try { st = await eapi("/alerts/status"); } catch (e) { box.innerHTML = `<div class="error-box">${esc(e.message)}</div>`; return; }
    const recent = (st.recent || []).slice(0, 8);
    box.innerHTML = `<div class="af-head"><h3>Alerts to Google Chat</h3><span class="chip ${st.configured ? "ok" : "grey"}">${st.configured ? "on" : "off"}</span></div>
      ${st.configured
        ? `<p class="small muted">New incidents of severity <b>${esc(st.min_severity)}</b> and higher are posted to ${esc(st.space)} within ${st.interval_seconds} s; fixed ones get a “Resolved” reply.
            Last check: ${esc(st.last_check || "starting…")}${st.last_error ? ` · <span class="bad">${esc(st.last_error)}</span>` : ""}</p>
           <div class="pacts"><button class="cbtn" id="alTest">Send test message</button><button class="cbtn" id="alCheck">Check now</button></div>
           ${recent.length ? `<div class="tablewrap" style="margin-top:10px">${objTable(recent.map((r) => ({ incident: r.title, severity: r.severity, alerted: String(r.first_alerted_at || "").slice(0, 16), resolved: String(r.resolved_alerted_at || "").slice(0, 16) })))}</div>` : ""}`
        : `${st.problem ? `<div class="error-box small">${esc(st.problem)}</div>` : ""}
           <p class="small">Post every new incident to a Google Chat space, with the owner and a link, and a reply when it is resolved.</p>
           <ol class="small steps-list"><li>In Google Chat open (or create) a space, e.g. <b>DataTrust Alerts</b>.</li>
             <li>Space name → <b>Apps &amp; integrations</b> → <b>Webhooks</b> → add one named <b>DataTrust AI</b>, copy its URL.</li>
             <li>In <code>.env</code> add <code>GOOGLE_CHAT_WEBHOOK_URL=</code> that URL (keep it secret, like a password) and
                 <code>DATATRUST_PUBLIC_URL=</code> the address people open. Restart the web app.</li></ol>`}
      <div id="alResult"></div>`;
    const t = $e("alTest"), c = $e("alCheck");
    if (t) t.onclick = async () => {
      try { await post("/alerts/test"); $e("alResult").innerHTML = `<p class="small ok">Test message sent: check the space.</p>`; }
      catch (e) { $e("alResult").innerHTML = `<div class="error-box">${esc(e.message)}</div>`; }
    };
    if (c) c.onclick = async () => {
      try {
        const r = await post("/alerts/check");
        $e("alResult").innerHTML = `<p class="small">${r.baseline ? `Alerts switched on: summary of ${r.open} open incident(s) sent.` : `Sent ${r.new.length} new and ${r.resolved.length} resolved alert(s); ${r.open} incident(s) open.`}
          ${(r.errors || []).length ? `<span class="bad">${esc(r.errors.join("; "))}</span>` : ""}</p>`;
        setTimeout(renderAlerts, 1500);
      } catch (e) { $e("alResult").innerHTML = `<div class="error-box">${esc(e.message)}</div>`; }
    };
  }

  // ---------------------------------------------------------------- "Contact owner" popover (incidents, lineage, catalog)
  let pop = null;
  const closePop = () => { if (pop) { pop.remove(); pop = null; } };
  async function popover(anchor, context) {
    closePop();
    pop = document.createElement("div");
    pop.className = "cpop";
    pop.innerHTML = "<p class='muted small'>Finding the owner…</p>";
    document.body.appendChild(pop);
    const place = () => {
      const r = anchor.getBoundingClientRect(), w = pop.offsetWidth, h = pop.offsetHeight;
      let left = Math.min(Math.max(8, r.right - w), window.innerWidth - w - 8), top = r.bottom + 6;
      if (top + h > window.innerHeight - 8) top = Math.max(8, r.top - h - 6);
      pop.style.left = left + "px"; pop.style.top = top + "px";
    };
    place();
    const toConnect = () => { closePop(); document.querySelector('#tabs button[data-page="estate-people"]').click(); };
    try {
      const r = await fetch("/api/estate/people/match", { method: "POST", credentials: "same-origin", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text: context }) });
      if (!pop) return;
      if (r.status === 401) {
        pop.innerHTML = `<p class="small">Log in to the portal to see who owns this.</p><button class="btn btn-sm" id="cpGo">Open Connect</button>`;
      } else {
        const people = await r.json();
        pop.innerHTML = `<div class="cpop-head"><b>Contact owner</b><button class="lg-close" id="cpX" aria-label="Close">×</button></div>
          <p class="small muted cpop-ctx">${esc(context.slice(0, 140))}</p>
          ${people.length ? people.slice(0, 4).map((p, i) => `<div class="cpop-person"><span class="avatar sm" style="background:${color(p.name)}">${esc(initials(p.name))}</span>
              <div class="pmeta"><b>${esc(p.name)}</b><span>owns ${esc(p.matched.join(", "))}</span></div></div>
              <div class="pacts">${actionButtons(p, i)}</div>`).join("")
            : `<p class="small">No owner recorded for this yet.</p><button class="btn btn-sm" id="cpGo">Add one in Connect</button>`}`;
        bindActions(pop, people, context);
        const x = $e("cpX"); if (x) x.onclick = closePop;
      }
      const go = $e("cpGo"); if (go) go.onclick = toConnect;
      place();
    } catch (e) { if (pop) pop.innerHTML = `<p class="small bad">${esc(e.message)}</p>`; }
  }
  document.addEventListener("click", (e) => {
    const b = e.target.closest("[data-contact]");
    if (b) { e.preventDefault(); e.stopPropagation(); popover(b, b.dataset.contact); return; }
    if (pop && !pop.contains(e.target)) closePop();
  }, true);
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") closePop(); });
  window.addEventListener("resize", closePop);
  document.addEventListener("scroll", closePop, true);

  window.Connect = { page, popover };
})();
