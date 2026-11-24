// Lineage map (4. Explain): datasets and the jobs that move data between them, across sources (/api/lineage/map).
// Drawn with Cytoscape.js + dagre (left to right), loaded on first use from /static/vendor (MIT, bundled: works offline).
// Health is the outline colour (green / amber / red), a node that lives in another source has a dashed outline,
// a link that crosses into another source is drawn in the brand colour. Uses app.js helpers: $, esc, api, sq, go.
(function () {
  const V = "/static/vendor/";
  let loading = null;
  function load() {
    if (window.cytoscape && window.cytoscapeDagre !== undefined) return Promise.resolve();
    if (loading) return loading;
    const add = (src) => new Promise((ok, fail) => {
      const s = document.createElement("script"); s.src = V + src; s.onload = ok; s.onerror = () => fail(new Error("could not load " + src));
      document.head.appendChild(s);
    });
    loading = add("cytoscape.min.js").then(() => add("dagre.min.js")).then(() => add("cytoscape-dagre.js"));
    return loading;
  }

  const KIND = { table: "Table", view: "View", file: "File", pipeline: "Pipeline", function: "Function", job: "Pipeline step" };
  const HEALTH = { bad: ["bad", "Problem"], warn: ["warn", "Warning"], ok: ["ok", "Checks pass"] };
  const COL = { line: "#CBD5E1", ink: "#0F1D2E", muted: "#64748B", brand: "#0672A1", ok: "#11805A", warn: "#C2410C", bad: "#C42B2B",
    job: "#EEF6FB", jobLine: "#9CC7DD", other: "#F8FAFC" };
  const M = { data: null, cy: null, focus: null, depth: 0, control: false, scope: null };

  function style() {
    return [
      { selector: "node", style: { shape: "round-rectangle", width: 214, height: 54, "background-color": "#fff", "border-width": 1.5,
          "border-color": COL.line, label: "data(text)", "text-wrap": "wrap", "text-max-width": 198, "font-size": 12.5, color: COL.ink,
          "text-valign": "center", "text-halign": "center", "font-family": "Inter, Segoe UI, system-ui, sans-serif", "line-height": 1.25 } },
      { selector: "node[kind = 'pipeline'], node[kind = 'job'], node[kind = 'function']", style: { "background-color": COL.job, "border-color": COL.jobLine } },
      { selector: "node[kind = 'file']", style: { shape: "cut-rectangle" } },
      { selector: "node[kind = 'view']", style: { "border-style": "double", "border-width": 3 } },
      { selector: "node[?other]", style: { "border-style": "dashed", "background-color": COL.other } },
      { selector: "node[health = 'ok']", style: { "border-color": COL.ok, "border-width": 2 } },
      { selector: "node[health = 'warn']", style: { "border-color": COL.warn, "border-width": 2.5 } },
      { selector: "node[health = 'bad']", style: { "border-color": COL.bad, "border-width": 3 } },
      { selector: "edge", style: { width: 1.5, "line-color": "#A8B4C4", "target-arrow-color": "#A8B4C4", "target-arrow-shape": "triangle",
          "arrow-scale": 0.9, "curve-style": "bezier" } },
      { selector: "edge[kind = 'runs']", style: { "line-style": "dotted", width: 1.5 } },
      { selector: "edge[?cross]", style: { "line-color": COL.brand, "target-arrow-color": COL.brand, width: 2.5 } },
      { selector: ".dim", style: { opacity: 0.18 } },
      { selector: "node.sel", style: { "border-color": COL.brand, "border-width": 3.5, "underlay-color": COL.brand, "underlay-opacity": 0.12, "underlay-padding": 6 } },
      { selector: "edge.path", style: { "line-color": COL.brand, "target-arrow-color": COL.brand, width: 2.5, opacity: 1 } },
    ];
  }
  const sourceOf = (n) => n.source_name || (n.seen_by && n.seen_by[0] && n.seen_by[0].name) || n.system || "";
  const label = (n) => `${n.label}\n${KIND[n.kind] || n.kind}${n.kind === "pipeline" || n.kind === "job" ? "" : " · " + (n.database || n.system || "")}`;

  function elements(d) {
    const ids = new Set(d.nodes.map((n) => n.id));
    const byId = Object.fromEntries(d.nodes.map((n) => [n.id, n]));
    return [
      ...d.nodes.map((n) => ({ data: { id: n.id, text: label(n), kind: n.kind, health: n.health || "", other: !!n.other_source } })),
      ...d.edges.filter((e) => ids.has(e.from) && ids.has(e.to)).map((e, i) => ({ data: { id: "e" + i, source: e.from, target: e.to, kind: e.kind,
        cross: !!(byId[e.from].scope && byId[e.to].scope && byId[e.from].scope !== byId[e.to].scope) } })),
    ];
  }

  // ------------------------------------------------------------------ focus: a node and n steps up / down
  function visibleIds() {
    if (!M.focus) return null;
    const keep = new Set([M.focus]);
    const walk = (dir) => {
      let frontier = [M.focus];
      for (let step = 0; M.depth === 0 || step < M.depth; step++) {
        const next = [];
        M.data.edges.forEach((e) => frontier.forEach((f) => {
          const n = dir === "down" ? (e.from === f ? e.to : null) : (e.to === f ? e.from : null);
          if (n && !keep.has(n)) { keep.add(n); next.push(n); }
        }));
        if (!next.length) break;
        frontier = next;
      }
    };
    walk("down"); walk("up");
    return keep;
  }

  function draw() {
    const el = $("lmGraph");
    if (M.cy) M.cy.destroy();
    const keep = visibleIds();
    const d = keep ? { nodes: M.data.nodes.filter((n) => keep.has(n.id)), edges: M.data.edges.filter((e) => keep.has(e.from) && keep.has(e.to)) } : M.data;
    if (!d.nodes.length) { el.innerHTML = `<p class="muted lm-empty">Nothing to draw for this source yet.</p>`; return; }
    el.innerHTML = "";
    M.cy = window.cytoscape({ container: el, elements: elements(d), style: style(), wheelSensitivity: 0.25, minZoom: 0.15, maxZoom: 2.2,
      layout: { name: "dagre", rankDir: "LR", nodeSep: 14, rankSep: 64, edgeSep: 8, fit: true, padding: 24 } });
    M.cy.on("tap", "node", (ev) => select(ev.target.id(), true));   // highlight in place, no jump
    M.cy.on("tap", (ev) => { if (ev.target === M.cy) select(null); });
    M.cy.on("mouseover", "node", () => (el.style.cursor = "pointer"));
    M.cy.on("mouseout", "node", () => (el.style.cursor = "default"));
    if (M.focus) select(M.focus, true);
    $("lmFocusBar").innerHTML = M.focus ? `Showing <b>${esc((M.data.nodes.find((n) => n.id === M.focus) || {}).label || "")}</b>
      and ${M.depth ? `${M.depth} step(s)` : "everything"} up and down. <button class="btn-link" id="lmShowAll" type="button">Show the whole map</button>` : "";
    if ($("lmShowAll")) $("lmShowAll").onclick = () => { M.focus = null; draw(); };
  }

  // ------------------------------------------------------------------ select: highlight its path, explain it in the panel
  function select(id, keepView) {
    const cy = M.cy;
    cy.elements().removeClass("dim sel path");
    if (!id) { $("lmPanel").innerHTML = panelHelp(); return; }
    const node = cy.getElementById(id);
    if (!node.length) return;
    const up = node.predecessors(), down = node.successors();
    cy.elements().not(node.union(up).union(down)).addClass("dim");
    node.addClass("sel");
    up.edges().union(down.edges()).addClass("path");
    if (!keepView) cy.animate({ center: { eles: node }, duration: 200 });
    $("lmPanel").innerHTML = panel(id, up.nodes().length, down.nodes().length);
    bindPanel(id);
  }

  function evidenceOf(id) {
    const out = [];
    M.data.edges.forEach((e) => {
      if (e.from !== id && e.to !== id) return;
      const other = M.data.nodes.find((n) => n.id === (e.from === id ? e.to : e.from));
      if (other) out.push({ dir: e.from === id ? "to" : "from", other, how: e.evidence.join("; "), kind: e.kind });
    });
    return out;
  }

  function panel(id, nUp, nDown) {
    const n = M.data.nodes.find((x) => x.id === id) || {};
    const h = HEALTH[n.health];
    const where = n.kind === "file" ? (n.system || "") : [n.system, n.database].filter(Boolean).join(" · ");
    const seen = (n.seen_by || []).map((s) => s.name).filter((v, i, a) => a.indexOf(v) === i);
    const ev = evidenceOf(id);
    const adf = /^job:adf:(\d+):(.+)$/.exec(id);
    const pipeName = adf ? n.label : null;
    return `<div class="lm-p-head"><span class="chip grey">${esc(KIND[n.kind] || n.kind)}</span>${h ? `<span class="chip ${h[0]}">${h[1]}</span>` : ""}</div>
      <h4 class="lm-p-title">${esc(n.label)}</h4>
      <dl class="lm-dl"><dt>Lives in</dt><dd>${esc(sourceOf(n))}${where ? `<div class="small muted">${esc(where)}</div>` : ""}</dd>
        ${seen.length > 1 ? `<dt>Seen by</dt><dd>${seen.map(esc).join(", ")}</dd>` : ""}
        ${n.dynamic_sql ? `<dt>Note</dt><dd class="small">Runs ${n.dynamic_sql} dynamic SQL statement(s) that cannot be read; what they touch is not shown.</dd>` : ""}</dl>
      <div class="lm-counts"><button type="button" class="btn light btn-sm" data-lm-focus="${esc(id)}">Focus here</button>
        <span class="small muted">${nUp} upstream · <b>${nDown} downstream</b></span></div>
      ${(n.incidents || []).length ? `<h5>Open incidents</h5><ul class="lm-list">${n.incidents.map((i) => `<li><span class="sev ${esc(i.severity)}">${esc(i.severity)}</span>
          <a href="#investigate=${encodeURIComponent(i.id)}">${esc(i.title)}</a></li>`).join("")}</ul>` : ""}
      ${(n.checks || []).length ? `<h5>Data checks</h5><ul class="lm-list">${n.checks.map((c) => `<li><span class="chip ${{ PASS: "ok", WARN: "warn" }[c.status] || "bad"}">${esc(c.status)}</span> ${esc(c.name)}</li>`).join("")}</ul>` : ""}
      ${pipeName ? `<p><a class="btn light btn-sm" href="#estate-adf-runs=${encodeURIComponent(`pipeline:${adf[1]}:${pipeName}`)}">Open the pipeline's runs</a></p>` : ""}
      ${ev.length ? `<h5>How DataTrust knows</h5><ul class="lm-list lm-ev">${ev.slice(0, 8).map((x) => `<li><span class="muted">${x.kind === "runs" ? (x.dir === "to" ? "starts" : "started by") : x.dir}</span>
          <b>${esc(x.other.label)}</b><div class="small muted">${esc(x.how)}</div></li>`).join("")}</ul>${ev.length > 8 ? `<p class="small muted">and ${ev.length - 8} more links</p>` : ""}` : ""}`;
  }
  const panelHelp = () => `<h4 class="lm-p-title">Click a table, view or pipeline</h4>
    <p class="small muted">Its path lights up: everything it reads from (upstream) and everything that depends on it (downstream). The panel shows where it lives, its open incidents and checks, and the evidence for each link.</p>
    <p class="small muted">Blue links cross into another source, for example a Data Factory pipeline loading a database table.</p>`;

  function bindPanel(id) {
    const b = document.querySelector("[data-lm-focus]");
    if (b) b.onclick = () => { M.focus = id; draw(); };
  }

  // ------------------------------------------------------------------ the page
  function sources(d) {
    const ago = (t) => { const m = Math.round((Date.now() - Date.parse(t)) / 60000); return m < 1 ? "just now" : m < 60 ? `${m} min ago` : `${Math.round(m / 60)} h ago`; };
    return d.sources.map((s) => s.ok ? `<span>${esc(s.name)} <span class="muted">read ${ago(s.built_at)}</span></span>`
      : `<span class="warn-text" title="${esc(s.error || "")}">${esc(s.name)}: could not be read</span>`).join(" · ");
  }

  async function render(body, opts = {}) {
    M.control = opts.control ?? M.control;
    body.innerHTML = `<div class="card"><p class="muted">Reading lineage…</p></div>`;
    let d;
    try {
      [d] = await Promise.all([api(`/api/lineage/map?control=${M.control}${opts.refresh ? "&refresh=true" : ""}&${sq()}`), load()]);
    } catch (e) { body.innerHTML = `<div class="card"><p class="bad-text">Lineage could not be read: ${esc(e.message)}</p></div>`; return; }
    if (M.scope !== d.scope.key) { M.focus = null; M.scope = d.scope.key; }
    M.data = d;
    const options = d.nodes.slice().sort((a, b) => a.label.localeCompare(b.label));
    body.innerHTML = `
      <div class="card lm-card">
        <div class="lm-tools">
          <label class="lm-find">Find <input id="lmFind" list="lmFindList" placeholder="table, view or pipeline" autocomplete="off"></label>
          <datalist id="lmFindList">${options.map((n) => `<option value="${esc(n.label)}">${esc(KIND[n.kind] || n.kind)} · ${esc(sourceOf(n))}</option>`).join("")}</datalist>
          <label>Steps <select id="lmDepth"><option value="0">All</option><option value="1">1</option><option value="2">2</option><option value="3">3</option></select></label>
          <label class="lm-check"><input type="checkbox" id="lmControl" ${M.control ? "checked" : ""}> Bookkeeping tables${d.hidden.control && !M.control ? ` (${d.hidden.control} hidden)` : ""}</label>
          <span class="grow"></span>
          <button class="btn light btn-sm" id="lmFit" type="button">Fit</button>
          <button class="btn light btn-sm" id="lmRefresh" type="button" title="Read every source again now">Read again</button>
        </div>
        <div id="lmFocusBar" class="small lm-focusbar"></div>
        <div class="lm-wrap"><div id="lmGraph" class="lm-graph" role="img" aria-label="Lineage map"></div><aside id="lmPanel" class="lm-panel">${panelHelp()}</aside></div>
        <div class="chart-legend lm-legend">
          <span><i class="lm-key"></i>Table</span><span><i class="lm-key view"></i>View</span><span><i class="lm-key job"></i>Pipeline / function</span>
          <span><i class="lm-key other"></i>Lives in another source</span><span><i class="line" style="background:${COL.brand}"></i>Link across sources</span>
          <span><i class="line dotted"></i>Starts / calls</span>
          <span><i class="lm-key" style="border-color:${COL.bad}"></i>Problem</span><span><i class="lm-key" style="border-color:${COL.warn}"></i>Warning</span><span><i class="lm-key" style="border-color:${COL.ok}"></i>Checks pass</span></div>
        <p class="small muted lm-sources">Read from: ${sources(d)}</p>
        <details class="lm-table"><summary>All ${d.edges.length} links as a table</summary><div class="tablewrap" style="max-height:none"><table>
          <thead><tr><th>From</th><th>To</th><th>How DataTrust knows</th></tr></thead><tbody>${d.edges.map((e) => {
            const f = M.data.nodes.find((n) => n.id === e.from), t = M.data.nodes.find((n) => n.id === e.to);
            return `<tr><td class="small">${esc(f ? f.label : e.from)}</td><td class="small">${esc(t ? t.label : e.to)}</td><td class="small muted">${esc(e.evidence.join("; "))}</td></tr>`; }).join("")}
          </tbody></table></div></details>
      </div>`;
    $("lmDepth").value = String(M.depth);
    $("lmDepth").onchange = () => { M.depth = Number($("lmDepth").value); if (M.focus) draw(); };
    $("lmControl").onchange = () => render(body, { control: $("lmControl").checked });
    $("lmFit").onclick = () => M.cy && M.cy.fit(undefined, 24);
    $("lmRefresh").onclick = () => render(body, { refresh: true });
    $("lmFind").onchange = () => {
      const n = M.data.nodes.find((x) => x.label.toLowerCase() === $("lmFind").value.trim().toLowerCase());
      if (n) { M.focus = n.id; if (!M.depth) { M.depth = 2; $("lmDepth").value = "2"; } draw(); }
    };
    draw();
    return d;
  }

  window.LineageMap = { render, state: M };
})();
