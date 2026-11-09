/* DataTrust AI landing page: menus, tabs, flowing picture, Ask box, look options. No libraries. */
(function () {
  "use strict";
  var $ = function (s, r) { return (r || document).querySelector(s); };
  var $$ = function (s, r) { return Array.prototype.slice.call((r || document).querySelectorAll(s)); };
  var reduce = !!(window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches);
  var canHover = !!(window.matchMedia && window.matchMedia("(hover:hover)").matches);
  function getJSON(url, opts, ms) {
    return new Promise(function (resolve, reject) {
      var t = setTimeout(function () { reject(new Error("timeout")); }, ms || 6000);
      fetch(url, opts).then(function (r) { if (!r.ok) throw new Error("HTTP " + r.status); return r.json(); })
        .then(function (j) { clearTimeout(t); resolve(j); }, function (e) { clearTimeout(t); reject(e); });
    });
  }

  // ---- dropdown menus. Hover opens them; a short "intent" delay stops the wrong menu opening while the mouse
  //      travels to an item, and a short close delay stops the menu vanishing when the cursor drifts. Click also works.
  var dds = $$(".dd"), current = null, openTimer = null, closeTimer = null;
  function setOpen(d, on) {
    d.classList.toggle("open", on); $(".dd-btn", d).setAttribute("aria-expanded", String(on));
    if (on) current = d; else if (current === d) current = null;
  }
  function closeAll(except) { dds.forEach(function (d) { if (d !== except) setOpen(d, false); }); }
  dds.forEach(function (d) {
    var b = $(".dd-btn", d);
    b.addEventListener("click", function (e) { e.stopPropagation(); var on = !d.classList.contains("open"); closeAll(d); setOpen(d, on); });
    d.addEventListener("mouseenter", function () {
      if (!canHover) return;
      clearTimeout(closeTimer); clearTimeout(openTimer);
      var delay = current && current !== d ? 170 : 60;
      openTimer = setTimeout(function () { closeAll(d); setOpen(d, true); }, delay);
    });
    d.addEventListener("mouseleave", function () {
      if (!canHover) return;
      clearTimeout(openTimer);
      if (d.classList.contains("open")) { clearTimeout(closeTimer); closeTimer = setTimeout(function () { setOpen(d, false); }, 320); }
    });
  });
  document.addEventListener("click", function () { closeAll(null); });
  document.addEventListener("keydown", function (e) { if (e.key === "Escape") { closeAll(null); closeMob(); } });

  // ---- mobile menu
  var burger = $("#burger"), mob = $("#mob");
  function closeMob() { if (mob) { mob.hidden = true; if (burger) burger.setAttribute("aria-expanded", "false"); } }
  if (burger && mob) {
    burger.addEventListener("click", function (ev) { ev.stopPropagation(); var show = mob.hidden; mob.hidden = !show; burger.setAttribute("aria-expanded", String(show)); });
    $$("a", mob).forEach(function (a) { a.addEventListener("click", closeMob); });
  }

  // ---- tabs (arrow keys supported)
  var tabs = $$('[role="tab"]');
  function pick(t) {
    tabs.forEach(function (x) { var on = x === t; x.setAttribute("aria-selected", String(on)); x.tabIndex = on ? 0 : -1; $("#" + x.getAttribute("aria-controls")).hidden = !on; });
    t.focus();
  }
  tabs.forEach(function (t, i) {
    t.addEventListener("click", function () { pick(t); });
    t.addEventListener("keydown", function (e) {
      if (e.key === "ArrowRight") { e.preventDefault(); pick(tabs[(i + 1) % tabs.length]); }
      if (e.key === "ArrowLeft") { e.preventDefault(); pick(tabs[(i - 1 + tabs.length) % tabs.length]); }
    });
  });

  // ---- scroll reveal
  var items = $$(".rv");
  if ("IntersectionObserver" in window) {
    var io = new IntersectionObserver(function (es) { es.forEach(function (e) { if (e.isIntersecting) { e.target.classList.add("in"); io.unobserve(e.target); } }); }, { threshold: 0.1 });
    items.forEach(function (el) { io.observe(el); });
  } else { items.forEach(function (el) { el.classList.add("in"); }); }

  // ---- the flowing picture (hero): data streams run from source files into the trust ring and out as trusted reports.
  //      Now and then a red "bad" particle arrives, passes the ring and leaves green. The number is the live trust score.
  var cv = $("#orbCanvas");
  if (cv && cv.getContext) {
    var ctx = cv.getContext("2d"), W = 0, H = 0, geo = null, parts = [], pulse = 0, last = 0, raf = 0, visible = true, accStr = "0,200,149", frameNo = 0;
    var N = 11;
    var bez = function (p0, p1, p2, p3, t) {
      var u = 1 - t;
      return { x: u * u * u * p0.x + 3 * u * u * t * p1.x + 3 * u * t * t * p2.x + t * t * t * p3.x, y: u * u * u * p0.y + 3 * u * u * t * p1.y + 3 * u * t * t * p2.y + t * t * t * p3.y };
    };
    var at = function (s, u) { return u < 0.5 ? bez(s.a[0], s.a[1], s.a[2], s.a[3], u * 2) : bez(s.b[0], s.b[1], s.b[2], s.b[3], (u - 0.5) * 2); };
    var spawn = function (p, first) { p.s = (Math.random() * N) | 0; p.u = first ? Math.random() : 0; p.v = 0.0022 + Math.random() * 0.0026; p.bad = Math.random() < 0.14; p.fixed = false; return p; };
    var build = function () {
      var r = cv.getBoundingClientRect(); W = r.width; H = r.height; if (!W || !H) return false;
      var dpr = Math.min(2, window.devicePixelRatio || 1);
      cv.width = Math.round(W * dpr); cv.height = Math.round(H * dpr); ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      var cx = W / 2, cy = H / 2, R = Math.min(W, H) * 0.19; geo = { cx: cx, cy: cy, R: R, streams: [] };
      for (var i = 0; i < N; i++) {
        var y0 = H * (0.08 + 0.84 * i / (N - 1)), y1 = H * (0.1 + 0.8 * (((i * 4) % N) / (N - 1)));
        geo.streams.push({
          a: [{ x: 0, y: y0 }, { x: cx * 0.62, y: y0 }, { x: cx - R * 2.4, y: cy + (y0 - cy) * 0.12 }, { x: cx - R, y: cy }],
          b: [{ x: cx + R, y: cy }, { x: cx + R * 2.4, y: cy + (y1 - cy) * 0.12 }, { x: W - cx * 0.62, y: y1 }, { x: W, y: y1 }]
        });
      }
      if (!parts.length) for (var k = 0; k < 54; k++) parts.push(spawn({}, true));
      return true;
    };
    var draw = function (dt, now) {
      if (!geo) return;
      if ((frameNo++ % 40) === 0) { var v = getComputedStyle(document.documentElement).getPropertyValue("--acc").trim(); if (v) accStr = v; }
      var dark = document.documentElement.getAttribute("data-theme") !== "light";
      ctx.clearRect(0, 0, W, H);
      ctx.lineWidth = 1; ctx.strokeStyle = dark ? "rgba(255,255,255,.07)" : "rgba(11,27,43,.10)";
      geo.streams.forEach(function (s) {
        ctx.beginPath(); ctx.moveTo(s.a[0].x, s.a[0].y); ctx.bezierCurveTo(s.a[1].x, s.a[1].y, s.a[2].x, s.a[2].y, s.a[3].x, s.a[3].y);
        ctx.moveTo(s.b[0].x, s.b[0].y); ctx.bezierCurveTo(s.b[1].x, s.b[1].y, s.b[2].x, s.b[2].y, s.b[3].x, s.b[3].y); ctx.stroke();
      });
      ctx.globalCompositeOperation = dark ? "lighter" : "source-over"; ctx.lineCap = "round";
      parts.forEach(function (p) {
        p.u += p.v * dt;
        if (p.bad && !p.fixed && p.u >= 0.5) { p.fixed = true; p.bad = false; pulse = 1; }
        if (p.u >= 1) spawn(p, false);
        var s = geo.streams[p.s], col = p.bad ? "255,85,96" : accStr;
        for (var k = 0; k < 9; k++) {
          var u1 = p.u - (k + 1) * 0.010; if (u1 < 0) break;
          var a = at(s, p.u - k * 0.010), b = at(s, u1);
          ctx.strokeStyle = "rgba(" + col + "," + ((1 - k / 9) * 0.9).toFixed(3) + ")"; ctx.lineWidth = 3.2 * (1 - k / 10);
          ctx.beginPath(); ctx.moveTo(a.x, a.y); ctx.lineTo(b.x, b.y); ctx.stroke();
        }
        var h = at(s, p.u); ctx.fillStyle = "rgba(" + col + ",1)"; ctx.beginPath(); ctx.arc(h.x, h.y, 2.6, 0, 6.2832); ctx.fill();
      });
      ctx.globalCompositeOperation = "source-over";
      pulse *= Math.pow(0.94, dt);
      var R = geo.R * (1 + 0.07 * pulse), cx = geo.cx, cy = geo.cy;
      var glow = ctx.createRadialGradient(cx, cy, R * 0.9, cx, cy, R * 2.2);
      glow.addColorStop(0, "rgba(" + accStr + "," + (0.30 + 0.25 * pulse).toFixed(3) + ")"); glow.addColorStop(1, "rgba(" + accStr + ",0)");
      ctx.fillStyle = glow; ctx.beginPath(); ctx.arc(cx, cy, R * 2.2, 0, 6.2832); ctx.fill();
      ctx.globalCompositeOperation = "destination-out"; ctx.fillStyle = "#000"; ctx.beginPath(); ctx.arc(cx, cy, R - 1, 0, 6.2832); ctx.fill(); ctx.globalCompositeOperation = "source-over";
      var g = ctx.createLinearGradient(cx - R, cy - R, cx + R, cy + R); g.addColorStop(0, "rgb(" + accStr + ")"); g.addColorStop(1, "#2b7de9");
      ctx.strokeStyle = g; ctx.lineWidth = 5; ctx.beginPath(); ctx.arc(cx, cy, R, 0, 6.2832); ctx.stroke();
      ctx.setLineDash([2, 9]); ctx.lineDashOffset = -(now || 0) / 45; ctx.lineWidth = 1.6; ctx.strokeStyle = "rgba(" + accStr + ",.6)";
      ctx.beginPath(); ctx.arc(cx, cy, R * 1.22, 0, 6.2832); ctx.stroke(); ctx.setLineDash([]);
    };
    var frame = function (now) { raf = requestAnimationFrame(frame); var dt = Math.min(50, now - last) / 16.7; last = now; draw(dt, now); };
    var start = function () { if (reduce || raf || !visible || document.hidden) return; last = performance.now(); raf = requestAnimationFrame(frame); };
    var stop = function () { if (raf) { cancelAnimationFrame(raf); raf = 0; } };
    if (build()) { draw(0, 0); start(); }
    window.addEventListener("resize", function () { if (build()) draw(0, 0); });
    document.addEventListener("visibilitychange", function () { if (document.hidden) stop(); else start(); });
    if ("IntersectionObserver" in window) new IntersectionObserver(function (es) { visible = es[0].isIntersecting; if (visible) start(); else stop(); }, { threshold: 0 }).observe(cv);

    var scoreEl = $("#orbScore"), noteEl = $("#orbNote");
    var showUnavailable = function () {
      if (scoreEl) scoreEl.textContent = "-";
      if (noteEl) noteEl.textContent = "data unavailable";
    };
    var loadScore = function () {
      getJSON("/api/overview").then(function (o) {
        var rawScore = o && o.trust && o.trust.score;
        if (rawScore === null || rawScore === undefined || rawScore === "") { showUnavailable(); return; }
        var score = Number(rawScore);
        if (!Number.isFinite(score)) { showUnavailable(); return; }
        if (scoreEl) scoreEl.textContent = Math.round(score);
        if (noteEl) noteEl.textContent = "live from your data";
      }, showUnavailable);
    };
    loadScore(); setInterval(loadScore, 30000);
  }

  // ---- Ask DataTrust AI (real /api/ask)
  var form = $("#askForm"), input = $("#askInput"), log = $("#chatLog"), history = [], busy = false;
  function bubble(cls, text) {
    var d = document.createElement("div"); d.className = "msg " + cls;
    String(text).split(/(\*\*[^*]+\*\*)/).forEach(function (part) {      // only **bold** and line breaks; no server HTML is trusted
      if (/^\*\*[^*]+\*\*$/.test(part)) { var b = document.createElement("b"); b.textContent = part.slice(2, -2); d.appendChild(b); }
      else d.appendChild(document.createTextNode(part));
    });
    log.appendChild(d); log.scrollTop = log.scrollHeight; return d;
  }
  function ask(q) {
    q = (q || "").trim(); if (!q || busy) return;
    busy = true; input.value = ""; bubble("user", q);
    var typing = bubble("bot typing", "checking the data…");
    getJSON("/api/ask", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ question: q, history: history }) }, 60000)
      .then(function (r) {
        typing.remove();
        var d = bubble("bot", r.answer || "I could not find an answer.");
        var meta = document.createElement("small");
        meta.textContent = (r.sources && r.sources.length ? "Source: " + r.sources.join(", ") : "") + (r.mode && r.mode !== "rules" ? " · answered by " + r.mode : "");
        if (meta.textContent) d.appendChild(meta);
        var a = document.createElement("a"); a.className = "cont"; a.href = "/app#ask"; a.textContent = "Continue in the assistant, with charts →"; d.appendChild(a);
        history.push({ role: "user", content: q }, { role: "assistant", content: r.answer || "" });
        log.scrollTop = log.scrollHeight;
      }, function () { typing.remove(); bubble("bot", "The assistant is not reachable right now. Start the web app and try again, or open the dashboard."); })
      .then(function () { busy = false; });
  }
  if (form) form.addEventListener("submit", function (e) { e.preventDefault(); ask(input.value); });
  $$("#sugg button").forEach(function (b) { b.addEventListener("click", function () { ask(b.textContent); }); });

  // ---- polish: scroll progress bar, header shadow, cursor spotlight on cards
  var bar = $("#progress"), hdr = $("#hdr"), ticking = false, de = document.documentElement;
  function onScroll() {
    var max = de.scrollHeight - de.clientHeight, y = window.scrollY || de.scrollTop;
    if (bar) bar.style.transform = "scaleX(" + (max > 0 ? Math.min(1, y / max) : 0).toFixed(3) + ")";
    if (hdr) hdr.classList.toggle("scrolled", y > 8);
    ticking = false;
  }
  window.addEventListener("scroll", function () { if (!ticking) { ticking = true; requestAnimationFrame(onScroll); } }, { passive: true });
  onScroll();
  if (!reduce && canHover) {
    $$(".pcard, .svc, .ccard, .ccol, .six a, .tcard").forEach(function (el) {
      el.classList.add("spot");
      el.addEventListener("pointermove", function (e) { var r = el.getBoundingClientRect(); el.style.setProperty("--mx", (e.clientX - r.left) + "px"); el.style.setProperty("--my", (e.clientY - r.top) + "px"); });
    });
  }

  // ---- look options: mode (black is the default) and accent colour, remembered in this browser
  var btn = $("#custBtn"), panel = $("#custPanel");
  function get(k) { try { return localStorage.getItem(k); } catch (e) { return null; } }
  function put(k, v) { try { if (v === null) localStorage.removeItem(k); else localStorage.setItem(k, v); } catch (e) {} }
  if (btn && panel) {
    var modes = $$("[data-mode]"), accs = $$("[data-acc]");
    var look = function () { return { mode: de.getAttribute("data-theme") === "light" ? "light" : "dark", acc: de.getAttribute("data-acc") || "emerald" }; };
    var mark = function () {
      var c = look();
      modes.forEach(function (m) { m.setAttribute("aria-pressed", String(m.getAttribute("data-mode") === c.mode)); });
      accs.forEach(function (a) { a.setAttribute("aria-pressed", String(a.getAttribute("data-acc") === c.acc)); });
      var meta = document.querySelector('meta[name="theme-color"]'); if (meta) meta.setAttribute("content", c.mode === "light" ? "#0b1b2b" : "#05070a");
    };
    var setMode = function (m) { de.setAttribute("data-theme", m === "light" ? "light" : "dark"); put("dt-mode", m); mark(); };
    var setAcc = function (a) { if (a === "emerald") de.removeAttribute("data-acc"); else de.setAttribute("data-acc", a); put("dt-acc", a); mark(); };
    var toggle = function (open) { var show = typeof open === "boolean" ? open : panel.hidden; panel.hidden = !show; btn.setAttribute("aria-expanded", String(show)); };
    btn.addEventListener("click", function (e) { e.stopPropagation(); toggle(); });
    panel.addEventListener("click", function (e) { e.stopPropagation(); });
    document.addEventListener("click", function () { toggle(false); });
    document.addEventListener("keydown", function (e) { if (e.key === "Escape") toggle(false); });
    modes.forEach(function (m) { m.addEventListener("click", function () { setMode(m.getAttribute("data-mode")); }); });
    accs.forEach(function (a) { a.addEventListener("click", function () { setAcc(a.getAttribute("data-acc")); }); });
    var reset = $("#custReset");
    if (reset) reset.addEventListener("click", function () { de.setAttribute("data-theme", "dark"); de.removeAttribute("data-acc"); put("dt-mode", null); put("dt-acc", null); mark(); });
    mark();
  }
})();
