// Data quality -> Built-in checks: the checks DataTrust runs on its own for the selected source (the POC pipeline, ADF
// pipelines), with their severity, limits and on / off. ADF: one setting for all pipelines, plus one pipeline's own.
// The same setting drives the incident on Detect and the trust score. API: /api/builtin/settings (web/builtin_routes.py).
// Depends on app.js (esc, toast, state, ensureCore, sq) and estate.js (est, $e).
(function () {
  const isAdmin = () => est.user && est.user.role === "admin";
  const SUBJECT = { freshness: "Freshness", volume: "Volume", validity: "Validity", uniqueness: "Uniqueness", schema: "Schema",
    reconciliation: "Reconciliation", runs: "Pipeline runs" };
  const SEV_HELP = { CRITICAL: "caps the score at 74 (At risk)", HIGH: "caps the score at 89 (Needs attention)",
    MEDIUM: "lowers the mark, no cap", LOW: "lowers the mark a little, no cap" };
  const sevHelp = (caps, sev) => (caps ? SEV_HELP[sev] || "" : "never caps the score: the severity only sets how much it counts");
  const V = { data: null, adding: {} };                     // adding: check key -> pipeline being given its own setting

  async function bapi(method, path, body) {
    const r = await fetch("/api/builtin" + path, { method, credentials: "same-origin",
      headers: { "Content-Type": "application/json" }, body: body ? JSON.stringify(body) : undefined });
    const out = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(out.detail || out.error || r.statusText);
    return out;
  }

  const when = (t) => (t ? String(t).slice(0, 16).replace("T", " ") : "");
  const valueText = (c, v) => [v.enabled ? "On" : "Off", v.severity.toLowerCase(),
    ...c.params.map((p) => `${p.label.toLowerCase()} ${v[p.name]}${p.unit ? " " + p.unit : ""}`)].join(" · ");

  // the controls for one setting: the default (pipeline "") or one pipeline's own
  function form(c, v, pipeline, sevs) {
    const ro = isAdmin() ? "" : "disabled";
    const id = `${c.key}|${pipeline}`;
    return `<div class="bi-form" data-form="${esc(id)}">
      <label class="bi-on"><input type="checkbox" data-f="enabled" ${v.enabled ? "checked" : ""} ${ro}> On</label>
      <label>Severity <select data-f="severity" ${ro}>${sevs.map((s) => `<option value="${s}" ${s === v.severity ? "selected" : ""}>${s[0] + s.slice(1).toLowerCase()}</option>`).join("")}</select></label>
      ${c.params.map((p) => `<label title="${esc(p.hint || "")}">${esc(p.label)} <input type="number" data-param="${esc(p.name)}" value="${esc(v[p.name])}"
          min="${p.min}" max="${p.max}" step="1" ${ro}>${p.unit ? ` <span class="small muted">${esc(p.unit)}</span>` : ""}${p.hint ? ` <span class="small muted">(${esc(p.hint)})</span>` : ""}</label>`).join("")}
      ${isAdmin() ? `<button class="btn btn-sm" data-save="${esc(id)}" disabled>Save</button>` : ""}
      <span class="small muted bi-sev" data-caps="${c.caps ? 1 : 0}">${esc(sevHelp(c.caps, v.severity))}</span></div>`;
  }

  function card(c, d) {
    const changed = c.changed ? `<span class="small muted">Changed by ${esc(c.changed_by || "?")} ${esc(when(c.changed_at))}
        ${isAdmin() ? ` · <a href="#" data-reset="${esc(c.key)}|">Back to default</a> (${esc(valueText(c, c.default))})` : ""}</span>`
      : `<span class="small muted">Default</span>`;
    const head = `<div class="bi-head"><b>${esc(c.label)}</b><span class="cv-kind">${esc(SUBJECT[c.subject] || c.subject)}</span>
        ${c.value.enabled ? "" : `<span class="chip grey">Off</span>`}<span class="grow"></span>${changed}</div>
      <p class="small muted">${esc(c.what)}</p>`;
    let own = "";
    if (d.kind === "adf") {
      const taken = new Set(c.overrides.map((o) => o.pipeline));
      const free = d.pipelines.filter((p) => !taken.has(p));
      const adding = V.adding[c.key];
      own = `<div class="bi-own"><div class="small"><b>Pipelines with their own setting</b>${c.overrides.length ? "" : `<span class="muted"> · none: every pipeline uses the setting above</span>`}</div>
        ${c.overrides.map((o) => `<div class="bi-pipe"><div class="small"><b>${esc(o.pipeline)}</b> <span class="muted">changed by ${esc(o.changed_by || "?")} ${esc(when(o.changed_at))}</span>
            ${isAdmin() ? ` · <a href="#" data-reset="${esc(c.key)}|${esc(o.pipeline)}">Use the setting for all pipelines</a>` : ""}</div>${form(c, o.value, o.pipeline, d.severities)}</div>`).join("")}
        ${adding ? `<div class="bi-pipe"><div class="small"><b>${esc(adding)}</b> <span class="muted">own setting, not saved yet</span>
            · <a href="#" data-cancel="${esc(c.key)}">Cancel</a></div>${form(c, c.value, adding, d.severities)}</div>`
          : isAdmin() && free.length ? `<label class="small">Set for one pipeline <select data-add="${esc(c.key)}"><option value="">choose…</option>
              ${free.map((p) => `<option>${esc(p)}</option>`).join("")}</select></label>` : ""}</div>`;
    }
    return `<div class="card bi-card" data-key="${esc(c.key)}">${head}
      <div class="small"><b>${d.kind === "adf" ? "All pipelines" : "Setting"}</b></div>${form(c, c.value, "", d.severities)}${own}</div>`;
  }

  async function page() {
    await ensureCore();
    const s = state.overview.scope || {};
    if (!V.data || V.data.scope.key !== s.key) { V.data = await bapi("GET", "/settings?" + sq()); V.adding = {}; }
    const d = V.data;
    const sub = $e("dqBuiltinSub");
    if (sub) sub.textContent = d.kind === "adf" ? "The checks DataTrust runs on every pipeline of a factory: how serious each one is, its limits, and on or off, for all pipelines or one."
      : d.kind === "poc" ? "The checks DataTrust runs on the POC pipeline: how serious each one is, its limits, and on or off."
      : "Checks DataTrust runs on its own for a kind of source.";
    const note = `<div class="banner ok small dq-source"><b>${esc(s.name || "")}</b> (${esc(s.type || "")}) · Change the source with <b>Source</b> at the top.</div>`;
    if (!d.kind) {
      return { html: note + `<div class="card"><p>This database has <b>no built-in checks</b>: DataTrust cannot see a pipeline here, only its tables.
        Everything it checks on this source is a <b>rule</b>, and rules are set in DQ Rule Studio.</p>
        <button class="btn btn-sm" id="biToRules">Open DQ Rule Studio</button></div>`,
        bind() { const b = $e("biToRules"); if (b) b.onclick = () => document.querySelector('#tabs [data-page="estate-dq-rules"]').click(); } };
    }
    const intro = `<div class="card small"><p>These checks run on their own; you do not add them. Here you decide <b>how serious</b> each one is
      (Critical caps the trust score at 74, High at 89), its <b>limits</b>, and whether it runs at all. A change applies to Detect and the trust
      score within a minute.${isAdmin() ? "" : " <b>Only an admin can change them.</b>"}</p></div>`;
    return { html: note + intro + d.checks.map((c) => card(c, d)).join(""), bind };
  }

  function read(formEl) {
    const out = { enabled: formEl.querySelector('[data-f="enabled"]').checked, severity: formEl.querySelector('[data-f="severity"]').value, params: {} };
    formEl.querySelectorAll("[data-param]").forEach((i) => (out.params[i.dataset.param] = i.value === "" ? null : Number(i.value)));
    return out;
  }

  function bind() {
    const box = $e("body-estate-dq-builtin");
    if (!box) return;
    box.querySelectorAll("[data-form]").forEach((f) => {
      const b = f.querySelector("[data-save]");
      const hint = f.querySelector(".bi-sev");
      f.querySelectorAll("input, select").forEach((i) => (i.oninput = i.onchange = () => {
        if (b) b.disabled = false;
        if (hint) hint.textContent = sevHelp(hint.dataset.caps === "1", f.querySelector('[data-f="severity"]').value);
      }));
      if (b) b.onclick = async () => {
        const [key, pipeline] = f.dataset.form.split("|");
        b.disabled = true; b.textContent = "Saving…";
        try {
          await bapi("PUT", "/settings", { key, ...read(f), ...(pipeline ? { pipeline, source_id: V.data.scope.id } : {}) });
          toast("Saved: Detect and the trust score use it from the next refresh");
          delete V.adding[key]; V.data = null; rerender();
        } catch (e) { toast(e.message); b.disabled = false; b.textContent = "Save"; }
      };
    });
    box.querySelectorAll("[data-reset]").forEach((a) => (a.onclick = async (e) => {
      e.preventDefault();
      const [key, pipeline] = a.dataset.reset.split("|");
      try {
        await bapi("DELETE", `/settings?key=${encodeURIComponent(key)}` + (pipeline ? `&pipeline=${encodeURIComponent(pipeline)}&source_id=${V.data.scope.id}` : ""));
        toast(pipeline ? `${pipeline} uses the setting for all pipelines again` : "Back to the default");
        V.data = null; rerender();
      } catch (x) { toast(x.message); }
    }));
    box.querySelectorAll("[data-add]").forEach((sel) => (sel.onchange = () => { if (sel.value) { V.adding[sel.dataset.add] = sel.value; rerender(); } }));
    box.querySelectorAll("[data-cancel]").forEach((a) => (a.onclick = (e) => { e.preventDefault(); delete V.adding[a.dataset.cancel]; rerender(); }));
  }

  async function rerender() {
    const box = $e("body-estate-dq-builtin");
    if (!box || box.closest(".page.hidden")) return;
    try { const body = await page(); box.innerHTML = body.html; body.bind(); }
    catch (e) { box.innerHTML = `<div class="error-box">${esc(e.message)}</div>`; }
  }

  window.BUILTIN = { page };
})();
