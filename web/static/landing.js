/* DataTrust AI landing page: menus and scroll effects. No libraries. */
(function () {
  "use strict";
  var $ = function (s, r) { return (r || document).querySelector(s); };
  var $$ = function (s, r) { return Array.prototype.slice.call((r || document).querySelectorAll(s)); };
  var canHover = !!(window.matchMedia && window.matchMedia("(hover:hover)").matches);
  // ---- dropdown menus. Hover opens them; a short "intent" delay stops the wrong menu opening while the mouse
  //      travels to an item, and a short close delay stops the menu vanishing when the cursor drifts. Click also works.
  var dds = $$(".dd"), current = null, openTimer = null, closeTimer = null;
  function setOpen(d, on) {
    d.classList.toggle("open", on); $(".dd-btn", d).setAttribute("aria-expanded", String(on));
    if (on) current = d; else if (current === d) current = null;
  }
  function closeAll(except) { dds.forEach(function (d) { if (d !== except) setOpen(d, false); }); }
  dds.forEach(function (d) {
    $(".dd-btn", d).addEventListener("click", function (e) { e.stopPropagation(); var on = !d.classList.contains("open"); closeAll(d); setOpen(d, on); });
    d.addEventListener("mouseenter", function () {
      if (!canHover) return;
      clearTimeout(closeTimer); clearTimeout(openTimer);
      openTimer = setTimeout(function () { closeAll(d); setOpen(d, true); }, current && current !== d ? 170 : 60);
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

  // ---- scroll reveal
  var items = $$(".rv");
  if ("IntersectionObserver" in window) {
    var io = new IntersectionObserver(function (es) { es.forEach(function (e) { if (e.isIntersecting) { e.target.classList.add("in"); io.unobserve(e.target); } }); }, { threshold: 0.1 });
    items.forEach(function (x) { io.observe(x); });
  } else { items.forEach(function (x) { x.classList.add("in"); }); }

  // ---- scroll progress bar and header shadow
  var bar = $("#progress"), hdr = $("#hdr"), ticking = false, de = document.documentElement;
  function onScroll() {
    var max = de.scrollHeight - de.clientHeight, y = window.scrollY || de.scrollTop;
    if (bar) bar.style.transform = "scaleX(" + (max > 0 ? Math.min(1, y / max) : 0).toFixed(3) + ")";
    if (hdr) hdr.classList.toggle("scrolled", y > 8);
    ticking = false;
  }
  window.addEventListener("scroll", function () { if (!ticking) { ticking = true; requestAnimationFrame(onScroll); } }, { passive: true });
  onScroll();
})();
