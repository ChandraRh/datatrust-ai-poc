// Lineage graph (Databricks-pipeline style): source -> bronze -> silver -> gold -> external check.
// Pure HTML + SVG, no libraries. Uses the same data as the List view (/api/lineage) plus step timings (/api/pipeline).
// Only depends on app.js helpers esc() and num().
(function () {
  const W = 218, H = 94, GAP_X = 84, ROW = 128, PAD_X = 24, PAD_Y = 40;
  const COLS = ["Source", "Bronze", "Silver", "Gold", "External check"];
  const state = { scale: 1, tx: 0, ty: 0, moved: false, selected: null };

  const ICON = {
    ok: '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M8 12.5l2.7 2.7L16 9.8"/></svg>',
    warn: '<svg viewBox="0 0 24 24"><path d="M12 3.5l9 16H3z"/><path d="M12 10v4.2M12 17.2h.01"/></svg>',
    bad: '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M9 9l6 6M15 9l-6 6"/></svg>',
    idle: '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M8.5 12h7"/></svg>',
  };
  const KIND_ICON = {
    source: '<svg viewBox="0 0 24 24"><path d="M6 3h8l4 4v14H6z"/><path d="M14 3v4h4"/></svg>',
    stream: '<svg viewBox="0 0 24 24"><path d="M4 8h11M4 12h16M4 16h11"/><path d="M17 5l3 3-3 3"/></svg>',
    table: '<svg viewBox="0 0 24 24"><rect x="3.5" y="4.5" width="17" height="15" rx="2"/><path d="M3.5 9.5h17M9.5 9.5v10"/></svg>',
    view: '<svg viewBox="0 0 24 24"><rect x="3.5" y="4.5" width="17" height="15" rx="2"/><path d="M3.5 9.5h17"/><path d="M8 14h3M8 16.5h6"/></svg>',
    external: '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="8.5"/><path d="M3.5 12h17M12 3.5c2.5 2.6 2.5 14.4 0 17M12 3.5c-2.5 2.6-2.5 14.4 0 17"/></svg>',
  };
  const worst = (...s) => (s.includes("bad") ? "bad" : s.includes("warn") ? "warn" : s.every((x) => x === "idle") ? "idle" : "ok");
  const secs = (v) => (v == null ? "" : v < 1 ? "<1s" : v < 60 ? `${Math.round(v * 10) / 10}s` : `${Math.floor(v / 60)}m ${Math.round(v % 60)}s`);

  // ------------------------------------------------------------------ model: nodes + edges from the lineage data
  function build(lin, pipe) {
    const steps = {};
    ((pipe && pipe.steps) || []).forEach((s) => (steps[s.pipeline_id] = s));
    const stream = lin.load_mode === "STREAM";
    const nodes = [], edges = [], byId = {};
    const add = (n) => { nodes.push(n); byId[n.id] = n; return n; };
    const gold = {};

    lin.entities.forEach((e, row) => {
      const ent = e.entity.toUpperCase();
      const bStep = steps[`PL_BRONZE_${ent}`] || steps[`PL_BRONZE_KAFKA_${ent}`];
      const sStep = steps[`PL_SILVER_${ent}`];
      const notStreamed = stream && /not streamed/.test(e.source_file);
      const empty = !e.bronze_rows && !e.silver_loaded;

      const srcStatus = e.source_to_bronze_gap > 0 ? "bad" : notStreamed || e.source_rows == null ? "idle" : "ok";
      add({ id: "src:" + e.entity, col: 0, row, kind: "source", icon: stream && !notStreamed ? "stream" : "source",
        type: stream ? (notStreamed ? "Source (file, not in this batch)" : "Kafka topic") : "Source file",
        name: stream ? e.entity : e.source_file, status: srcStatus,                // type label already says "Kafka topic"
        sub: notStreamed ? "Not in this batch" : e.source_rows == null ? "Row count not available" : e.source_to_bronze_gap > 0 ? `${num(e.source_to_bronze_gap)} rows did not land` : "Delivered",
        metrics: e.source_rows == null ? [] : [{ v: e.source_rows, cls: "ok", t: "rows at the source" }],
        details: [["Entity", e.entity], ["Rows at source", num(e.source_rows)], ["Source → bronze gap", num(e.source_to_bronze_gap || 0)]] });

      const bStatus = bStep && bStep.status === "FAILED" ? "bad" : e.source_to_bronze_gap > 0 ? "warn" : empty ? "idle" : "ok";
      add({ id: "b:" + e.entity, col: 1, row, kind: "bronze", icon: "table", type: "Bronze · raw table", name: e.bronze_table, status: bStatus,
        sub: (bStep ? (bStep.status === "FAILED" ? "Failed" : "Completed") : empty ? "No new rows" : "Loaded") + (bStep ? " · " + secs(bStep.duration_sec) : ""),
        metrics: [{ v: e.bronze_rows, cls: "ok", t: "rows landed in bronze (this batch)" }],
        details: [["Rows landed (this batch)", num(e.bronze_rows)], ["Step", bStep ? bStep.pipeline_id : "–"], ["Duration", bStep ? secs(bStep.duration_sec) : "–"],
          ["Message", (bStep && bStep.message) || "–"]] });

      const held = (e.rejected || 0) + (e.duplicates || 0);
      const sStatus = (sStep && sStep.status === "FAILED") || e.unexplained_loss ? "bad" : held ? "warn" : empty ? "idle" : "ok";
      const sMetrics = [{ v: e.silver_loaded, cls: "ok", t: "rows loaded into silver" }];
      if (e.rejected) sMetrics.push({ v: e.rejected, cls: "warn", t: "rows rejected by quality rules" });
      if (e.duplicates) sMetrics.push({ v: e.duplicates, cls: "grey", t: "duplicates removed" });
      if (e.unexplained_loss) sMetrics.push({ v: e.unexplained_loss, cls: "bad", t: "rows lost without explanation" });
      add({ id: "s:" + e.entity, col: 2, row, kind: "silver", icon: "table", type: e.scd === 2 ? "Silver · SCD2 table" : "Silver · table",
        name: e.silver_table, status: sStatus,
        sub: (sStep ? (sStep.status === "FAILED" ? "Failed" : held ? "Completed with held-back rows" : "Completed") + " · " + secs(sStep.duration_sec) : empty ? "No new rows" : "Loaded"),
        metrics: sMetrics,
        details: [["Rows loaded (this batch)", num(e.silver_loaded)], ["Rejected by rules", num(e.rejected || 0)], ["Duplicates removed", num(e.duplicates || 0)],
          ...(e.scd === 2 ? [["Unchanged (SCD2, no new version)", num(e.scd2_unchanged || 0)]] : []),
          ["Unexplained loss", num(e.unexplained_loss || 0)], [e.scd === 2 ? "Current rows now" : "Rows now", num(e.silver_rows_now)],
          ["Step", sStep ? sStep.pipeline_id : "–"], ["Duration", sStep ? secs(sStep.duration_sec) : "–"]] });

      edges.push({ from: "src:" + e.entity, to: "b:" + e.entity, status: e.source_to_bronze_gap > 0 ? "bad" : "ok",
        label: e.source_to_bronze_gap > 0 ? `−${num(e.source_to_bronze_gap)}` : "" });
      edges.push({ from: "b:" + e.entity, to: "s:" + e.entity, status: e.unexplained_loss ? "bad" : held ? "warn" : "ok",
        label: e.unexplained_loss ? `−${num(e.unexplained_loss)} lost` : held ? `${num(held)} held back` : "" });
      (e.gold_views || []).forEach((v) => {
        (gold[v] = gold[v] || []).push({ entity: e.entity, row, status: sStatus });
        edges.push({ from: "s:" + e.entity, to: "g:" + v, status: sStatus === "bad" ? "bad" : sStatus === "warn" ? "warn" : "ok", label: "" });
      });
    });

    const gStep = steps.PL_GOLD_VIEWS;
    const recon = lin.settlement || [];
    const mismatches = recon.filter((r) => r.recon_status === "MISMATCH");
    Object.entries(gold).forEach(([v, parents]) => {
      const upstream = worst(...parents.map((p) => (p.status === "idle" ? "ok" : p.status)));
      const isRecon = v === "settlement_reconciliation";
      const status = gStep && gStep.status === "FAILED" ? "bad" : isRecon && mismatches.length ? "bad" : upstream === "bad" ? "bad" : upstream;
      add({ id: "g:" + v, col: 3, rowF: parents.reduce((a, p) => a + p.row, 0) / parents.length, kind: "gold", icon: "view", type: "Gold · view",
        name: "gold." + v, status,
        sub: (gStep ? (gStep.status === "FAILED" ? "Failed" : "Refreshed") + " · " + secs(gStep.duration_sec) : "View") +
          (status === "warn" ? " · upstream held back" : isRecon && mismatches.length ? " · mismatch" : ""),
        metrics: [{ v: parents.length, cls: "grey", t: "silver tables this view reads", suffix: parents.length === 1 ? " input" : " inputs" }],
        details: [["Reads from", parents.map((p) => "silver." + p.entity).join(", ")], ["Status", status === "ok" ? "Healthy" : status === "warn" ? "Built on data with held-back rows" : "Affected by a data problem"],
          ["Refresh step", gStep ? `${gStep.pipeline_id} · ${secs(gStep.duration_sec)}` : "–"]] });
    });
    if (byId["g:settlement_reconciliation"]) {
      const short = mismatches.reduce((a, r) => a + Number(r.payment_difference || 0), 0);
      add({ id: "x:gateway", col: 4, rowF: byId["g:settlement_reconciliation"].rowF, kind: "external", icon: "external", type: "External check",
        name: "Payment gateway settlement", status: mismatches.length ? "bad" : "ok",
        sub: mismatches.length ? `${mismatches.length} date(s) do not match` : `All ${recon.length} dates match`,
        metrics: [{ v: recon.length - mismatches.length, cls: "ok", t: "dates that match" }, ...(mismatches.length ? [{ v: mismatches.length, cls: "bad", t: "dates that do not match" }] : [])],
        details: [["Dates checked", num(recon.length)], ["Dates not matching", num(mismatches.length)], ["Payments short in total", num(short)],
          ...mismatches.slice(0, 5).map((r) => [r.business_date, `gateway ${num(r.settled_payments)} vs silver ${num(r.silver_payments)}`])] });
      edges.push({ from: "g:settlement_reconciliation", to: "x:gateway", status: mismatches.length ? "bad" : "ok", label: mismatches.length ? "mismatch" : "" });
    }
    layout(nodes);
    return { nodes, edges, byId };
  }

  // ------------------------------------------------------------------ layout: columns, gold views centred on their inputs
  function layout(nodes) {
    nodes.forEach((n) => { if (n.row != null) n.y = n.row * ROW; });
    ["gold", "external"].forEach((kind) => {
      const list = nodes.filter((n) => n.kind === kind).sort((a, b) => a.rowF - b.rowF);
      list.forEach((n) => (n.y = n.rowF * ROW));
      for (let i = 1; i < list.length; i++) list[i].y = Math.max(list[i].y, list[i - 1].y + H + 30);   // no overlaps (box + its label)
      if (list.length > 1) {                                                                              // keep the block centred
        const want = list.reduce((a, n) => a + n.rowF * ROW, 0) / list.length;
        const have = list.reduce((a, n) => a + n.y, 0) / list.length;
        const shift = Math.max(want - have, -list[0].y);
        list.forEach((n) => (n.y += shift));
      }
    });
    nodes.forEach((n) => { n.x = PAD_X + n.col * (W + GAP_X); n.y += PAD_Y; });
  }

  // ------------------------------------------------------------------ render
  function render(host, lin, pipe) {
    const g = build(lin, pipe);
    const usedCols = Math.max(...g.nodes.map((n) => n.col)) + 1;
    const cw = PAD_X * 2 + usedCols * W + (usedCols - 1) * GAP_X;
    const ch = Math.max(...g.nodes.map((n) => n.y)) + H + PAD_Y;
    const overall = worst(...g.nodes.map((n) => n.status));
    const total = ((pipe && pipe.steps) || []).reduce((a, s) => a + Number(s.duration_sec || 0), 0);
    const statusText = { ok: "Completed", warn: "Completed with warnings", bad: "Needs attention", idle: "No new data" }[overall];

    const edgePath = (a, b) => {
      const x1 = a.x + W, y1 = a.y + H / 2, x2 = b.x, y2 = b.y + H / 2, dx = Math.max(40, (x2 - x1) * 0.5);
      return { d: `M${x1},${y1} C${x1 + dx},${y1} ${x2 - dx},${y2} ${x2 - 6},${y2}`, mx: (x1 + x2) / 2, my: (y1 + y2) / 2 };
    };
    const svgEdges = g.edges.map((e, i) => {
      const p = edgePath(g.byId[e.from], g.byId[e.to]);
      e.mx = p.mx; e.my = p.my;
      return `<path class="lg-edge ${e.status}" data-i="${i}" d="${p.d}" marker-end="url(#lg-arrow-${e.status})"/>`;
    }).join("");
    const labels = g.edges.filter((e) => e.label).map((e) =>
      `<span class="lg-elabel ${e.status}" data-from="${esc(e.from)}" data-to="${esc(e.to)}" style="left:${e.mx}px;top:${e.my}px">${esc(e.label)}</span>`).join("");
    const colHeads = COLS.slice(0, usedCols).map((c, i) =>
      `<div class="lg-col" style="left:${PAD_X + i * (W + GAP_X)}px;width:${W}px">${c}</div>`).join("");
    const nodeHtml = g.nodes.map((n) => `
      <div class="lg-node ${n.status}" data-id="${esc(n.id)}" tabindex="0" role="button" aria-label="${esc(n.name)}, ${esc(n.sub)}"
           style="left:${n.x}px;top:${n.y}px;width:${W}px;height:${H}px">
        <div class="lg-type">${esc(n.type)}</div>
        <div class="lg-head"><span class="lg-kind">${KIND_ICON[n.icon]}</span><span class="lg-name" title="${esc(n.name)}">${esc(n.name)}</span>
          <span class="lg-st ${n.status}" title="${n.status}">${ICON[n.status]}</span></div>
        <div class="lg-sub" title="${esc(n.sub)}">${esc(n.sub)}</div>
        <div class="lg-metrics">${n.metrics.map((m) => `<span class="lg-m ${m.cls}" title="${esc(m.t)}"><i></i>${num(m.v)}${m.suffix || ""}</span>`).join("")}</div>
      </div>`).join("");

    host.innerHTML = `
      <div class="lg-bar">
        <div class="lg-run"><span class="lg-pill ${overall}">${ICON[overall]}${statusText}</span>
          <span>Batch <b>${esc(lin.batch_id || "–")}</b></span><span>${esc(lin.business_date || "")}</span>
          <span>${lin.load_mode === "STREAM" ? "Kafka stream" : "File load"}</span>${total ? `<span>${secs(total)} in total</span>` : ""}</div>
        <div class="lg-tools"><button class="btn light btn-sm" data-z="out" title="Zoom out" aria-label="Zoom out">−</button>
          <button class="btn light btn-sm" data-z="in" title="Zoom in" aria-label="Zoom in">+</button>
          <button class="btn light btn-sm" data-z="fit" title="Fit to screen">Fit</button></div>
      </div>
      <div class="lg-wrap" tabindex="-1">
        <div class="lg-stage" style="width:${cw}px;height:${ch}px">
          ${colHeads}
          <svg class="lg-svg" width="${cw}" height="${ch}" aria-hidden="true"><defs>
            ${["ok", "warn", "bad"].map((s) => `<marker id="lg-arrow-${s}" viewBox="0 0 10 10" refX="6" refY="5" markerWidth="7" markerHeight="7" orient="auto"><path d="M0,1 L8,5 L0,9 z" class="lg-arrowhead ${s}"/></marker>`).join("")}
          </defs>${svgEdges}</svg>
          ${labels}${nodeHtml}
        </div>
        <aside class="lg-panel hidden" aria-live="polite"></aside>
      </div>
      <div class="lg-legend"><span><i class="ok"></i>rows written</span><span><i class="warn"></i>held back (rejected by rules)</span>
        <span><i class="grey"></i>duplicates removed / inputs</span><span><i class="bad"></i>lost or not matching</span>
        <span class="muted">Drag to move · Ctrl + scroll to zoom · click a box for details</span></div>`;

    const wrap = host.querySelector(".lg-wrap"), stage = host.querySelector(".lg-stage"), panel = host.querySelector(".lg-panel");
    const apply = () => (stage.style.transform = `translate(${state.tx}px,${state.ty}px) scale(${state.scale})`);
    const fit = () => {
      const s = Math.min(1, (wrap.clientWidth - 24) / cw);
      state.scale = Math.max(0.72, s);                                   // stay readable; drag to see the rest
      state.tx = Math.max(12, (wrap.clientWidth - cw * state.scale) / 2); state.ty = 8; state.moved = false;
      wrap.style.height = Math.min(Math.max(ch * state.scale + 24, 360), window.innerHeight * 0.78) + "px";
      apply();
    };
    const zoom = (f, cx = wrap.clientWidth / 2, cy = wrap.clientHeight / 2) => {
      const s = Math.min(1.6, Math.max(0.35, state.scale * f));
      state.tx = cx - ((cx - state.tx) * s) / state.scale; state.ty = cy - ((cy - state.ty) * s) / state.scale;
      state.scale = s; state.moved = true; apply();
    };
    if (state.moved) { wrap.style.height = Math.min(Math.max(ch * state.scale + 24, 360), window.innerHeight * 0.78) + "px"; apply(); } else fit();
    host.querySelectorAll("[data-z]").forEach((b) => (b.onclick = () => (b.dataset.z === "fit" ? fit() : zoom(b.dataset.z === "in" ? 1.2 : 1 / 1.2))));
    wrap.addEventListener("wheel", (ev) => {
      if (!ev.ctrlKey && !ev.metaKey) return;                        // normal scrolling keeps scrolling the page
      ev.preventDefault();
      const r = wrap.getBoundingClientRect();
      zoom(ev.deltaY < 0 ? 1.1 : 1 / 1.1, ev.clientX - r.left, ev.clientY - r.top);
    }, { passive: false });
    let drag = null;
    wrap.addEventListener("pointerdown", (ev) => {
      if (ev.target.closest(".lg-node, .lg-panel")) return;
      drag = { x: ev.clientX, y: ev.clientY, tx: state.tx, ty: state.ty }; wrap.classList.add("dragging"); wrap.setPointerCapture(ev.pointerId);
    });
    wrap.addEventListener("pointermove", (ev) => {
      if (!drag) return;
      state.tx = drag.tx + ev.clientX - drag.x; state.ty = drag.ty + ev.clientY - drag.y; state.moved = true; apply();
    });
    const end = (ev) => {
      if (drag && ev && Math.abs(ev.clientX - drag.x) + Math.abs(ev.clientY - drag.y) < 4) showPanel(null);   // a click on empty space closes details
      drag = null; wrap.classList.remove("dragging");
    };
    wrap.addEventListener("pointerup", end); wrap.addEventListener("pointercancel", end);

    // hover: highlight everything upstream and downstream of a box
    const up = {}, down = {};
    g.edges.forEach((e) => { (down[e.from] = down[e.from] || []).push(e.to); (up[e.to] = up[e.to] || []).push(e.from); });
    const reach = (id, map, seen = new Set()) => { (map[id] || []).forEach((n) => { if (!seen.has(n)) { seen.add(n); reach(n, map, seen); } }); return seen; };
    const highlight = (id) => {
      if (!id) { stage.classList.remove("focus"); return; }
      const keep = new Set([id, ...reach(id, up), ...reach(id, down)]);
      stage.classList.add("focus");
      stage.querySelectorAll(".lg-node").forEach((el) => el.classList.toggle("on", keep.has(el.dataset.id)));
      stage.querySelectorAll(".lg-edge").forEach((el) => { const e = g.edges[el.dataset.i]; el.classList.toggle("on", keep.has(e.from) && keep.has(e.to)); });
      stage.querySelectorAll(".lg-elabel").forEach((el) => el.classList.toggle("on", keep.has(el.dataset.from) && keep.has(el.dataset.to)));
    };
    const showPanel = (n) => {
      state.selected = n ? n.id : null;
      stage.querySelectorAll(".lg-node").forEach((el) => el.classList.toggle("selected", n && el.dataset.id === n.id));
      if (!n) { panel.classList.add("hidden"); highlight(null); return; }
      highlight(n.id);
      panel.innerHTML = `<button class="lg-close" aria-label="Close">×</button>
        <div class="lg-type">${esc(n.type)}</div><h3>${esc(n.name)}</h3>
        <p><span class="lg-pill ${n.status}">${ICON[n.status]}${esc(n.sub)}</span></p>
        <table>${n.details.map(([k, v]) => `<tr><th>${esc(k)}</th><td>${esc(v)}</td></tr>`).join("")}</table>
        <button class="btn light btn-sm" data-contact="${esc(n.name + (n.status === "bad" || n.status === "warn" ? " — " + n.sub : ""))}">Contact owner</button>
        ${n.status === "bad" || n.status === "warn" ? `<button class="btn btn-sm" data-go="detect">See the incident →</button>` : ""}`;
      panel.classList.remove("hidden");
      panel.querySelector(".lg-close").onclick = () => showPanel(null);
      const goBtn = panel.querySelector("[data-go]");
      if (goBtn) goBtn.onclick = () => document.querySelector('#tabs button[data-page="detect"]').click();
    };
    stage.querySelectorAll(".lg-node").forEach((el) => {
      const n = g.byId[el.dataset.id];
      el.addEventListener("mouseenter", () => { if (!state.selected) highlight(n.id); });
      el.addEventListener("mouseleave", () => { if (!state.selected) highlight(null); });
      el.addEventListener("click", () => showPanel(state.selected === n.id ? null : n));
      el.addEventListener("keydown", (ev) => { if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); showPanel(n); } if (ev.key === "Escape") showPanel(null); });
    });
    if (state.selected && g.byId[state.selected]) showPanel(g.byId[state.selected]); else state.selected = null;
  }

  window.LineageGraph = { render };
})();
