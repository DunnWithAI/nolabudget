/* Progressive enhancement only. Every page reads fully without this file:
   - figure view switches (General Fund / all funds; dollars / adjusted / per resident)
   - chart tooltips and crosshair (mouse, touch and arrow keys)
   - the department finder on the home page
   - motion: pictures that draw themselves on scroll, and the headline count-up
   - split-bar hover: highlight one part and show its name and amount */
(function () {
  "use strict";

  // ---------------------------------------------------------------- figure views
  function initFigure(fig) {
    var controls = fig.querySelector(".fig-controls");
    if (!controls) return;
    var state = { fund: null, unit: null };
    var views = fig.querySelectorAll(".view[data-fund]");
    if (views.length) {
      state.fund = views[0].getAttribute("data-fund");
      state.unit = views[0].getAttribute("data-unit");
    }
    controls.hidden = false;
    function show() {
      views.forEach(function (v) {
        v.hidden = !(v.getAttribute("data-fund") === state.fund && v.getAttribute("data-unit") === state.unit);
      });
      controls.querySelectorAll("button[data-set]").forEach(function (b) {
        b.setAttribute("aria-checked", String(state[b.getAttribute("data-set")] === b.getAttribute("data-value")));
      });
    }
    controls.addEventListener("click", function (e) {
      var b = e.target.closest("button[data-set]");
      if (!b) return;
      state[b.getAttribute("data-set")] = b.getAttribute("data-value");
      show();
    });
    controls.addEventListener("keydown", function (e) {
      var b = e.target.closest("button[data-set]");
      if (!b || (e.key !== "ArrowRight" && e.key !== "ArrowLeft")) return;
      var group = Array.prototype.slice.call(b.parentNode.querySelectorAll("button"));
      var i = group.indexOf(b) + (e.key === "ArrowRight" ? 1 : -1);
      var next = group[(i + group.length) % group.length];
      next.focus();
      next.click();
      e.preventDefault();
    });
  }

  // ---------------------------------------------------------------- tooltips
  function initChart(svg) {
    var tips;
    try { tips = JSON.parse(svg.getAttribute("data-tips") || "[]"); } catch (err) { return; }
    if (!tips.length) return;
    var plot = svg.closest(".plot");
    var tip = plot && plot.querySelector(".tip");
    var cross = svg.querySelector(".crosshair");
    if (!tip || !cross) return;
    var vb = svg.viewBox.baseVal;
    var current = -1;
    svg.setAttribute("tabindex", "0");

    function render(i) {
      if (i < 0 || i >= tips.length) return;
      current = i;
      var t = tips[i];
      cross.setAttribute("x1", t.x);
      cross.setAttribute("x2", t.x);
      cross.setAttribute("visibility", t.rows.length ? "visible" : "hidden");
      tip.textContent = "";
      var y = document.createElement("div");
      y.className = "tip-year";
      y.textContent = String(t.year);
      tip.appendChild(y);
      if (!t.rows.length) {
        var none = document.createElement("div");
        none.textContent = "No figure printed";
        tip.appendChild(none);
      }
      t.rows.forEach(function (r) {
        var row = document.createElement("div");
        row.className = "tip-row";
        var k = document.createElement("i");
        k.className = "tip-key " + r[0];
        var v = document.createElement("b");
        v.textContent = r[2];
        var l = document.createElement("span");
        l.textContent = r[1];
        row.appendChild(k); row.appendChild(v); row.appendChild(l);
        tip.appendChild(row);
      });
      tip.hidden = false;
      var rect = svg.getBoundingClientRect();
      var prect = plot.getBoundingClientRect();
      var px = rect.left - prect.left + (t.x / vb.width) * rect.width;
      var w = tip.offsetWidth;
      var left = px + 12;
      if (left + w > prect.width) left = px - w - 12;
      tip.style.left = Math.max(0, left) + "px";
      tip.style.top = "8px";
    }
    function hide() {
      tip.hidden = true;
      cross.setAttribute("visibility", "hidden");
      current = -1;
    }
    function nearest(clientX) {
      var rect = svg.getBoundingClientRect();
      var x = ((clientX - rect.left) / rect.width) * vb.width;
      var best = 0, d = Infinity;
      tips.forEach(function (t, i) {
        var dd = Math.abs(t.x - x);
        if (dd < d) { d = dd; best = i; }
      });
      return best;
    }
    svg.addEventListener("pointermove", function (e) { render(nearest(e.clientX)); });
    svg.addEventListener("pointerdown", function (e) { render(nearest(e.clientX)); });
    svg.addEventListener("pointerleave", hide);
    svg.addEventListener("blur", hide);
    svg.addEventListener("focus", function () { render(current >= 0 ? current : tips.length - 1); });
    svg.addEventListener("keydown", function (e) {
      if (e.key === "ArrowRight" || e.key === "ArrowLeft") {
        var i = (current < 0 ? tips.length - 1 : current) + (e.key === "ArrowRight" ? 1 : -1);
        render(Math.max(0, Math.min(tips.length - 1, i)));
        e.preventDefault();
      } else if (e.key === "Escape") {
        hide();
      }
    });
  }

  // ---------------------------------------------------------------- finder
  function initFinder(box) {
    var section = box.closest("section");
    var input = box.querySelector("input");
    var items = section.querySelectorAll("li[data-search]");
    var groups = section.querySelectorAll(".dept-group");
    var empty = section.querySelector(".finder-empty");
    box.hidden = false;
    input.addEventListener("input", function () {
      var q = input.value.trim().toLowerCase();
      var any = false;
      items.forEach(function (li) {
        var hit = !q || li.getAttribute("data-search").indexOf(q) !== -1;
        li.hidden = !hit;
        if (hit) any = true;
      });
      groups.forEach(function (g) {
        g.hidden = !g.querySelector("li[data-search]:not([hidden])");
      });
      if (empty) empty.hidden = any;
    });
  }


  // ---------------------------------------------------------------- motion
  var calm = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  // Add .is-in when a picture scrolls into view. CSS animates only from that class, so
  // without this script (or with reduced motion) everything is simply shown.
  function initReveal() {
    var els = document.querySelectorAll("[data-split], [data-receipt], [data-animate], .fig");
    if (!("IntersectionObserver" in window)) {
      els.forEach(function (el) { el.classList.add("is-in"); });
      return;
    }
    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (e) {
        if (e.isIntersecting) { e.target.classList.add("is-in"); io.unobserve(e.target); }
      });
    }, { rootMargin: "0px 0px -12% 0px", threshold: 0.15 });
    els.forEach(function (el) { io.observe(el); });
    // A page opened in a background tab may never report intersections; show it anyway.
    setTimeout(function () { els.forEach(function (el) {
      var r = el.getBoundingClientRect();
      if (r.top < window.innerHeight) el.classList.add("is-in");
    }); }, 2500);
  }

  // Count the headline figure up from zero. The final text is already in the HTML.
  function initCount(el) {
    if (calm) return;
    var target = parseFloat(el.getAttribute("data-count"));
    var final = el.textContent;
    if (!(target > 0)) return;
    var billions = target >= 1e9;
    var dur = 1100, t0 = null;
    function fmt(v) {
      return billions ? "$" + (v / 1e9).toFixed(2) + " billion" : "$" + Math.round(v / 1e6) + " million";
    }
    function step(t) {
      if (t0 === null) t0 = t;
      var p = Math.min(1, (t - t0) / dur);
      var eased = 1 - Math.pow(1 - p, 3);
      el.textContent = p < 1 ? fmt(target * eased) : final;
      if (p < 1) requestAnimationFrame(step);
    }
    requestAnimationFrame(step);
  }

  // ---------------------------------------------------------------- split bars
  function initSplit(fig) {
    var parts = fig.querySelectorAll(".split-part");
    var keys = fig.querySelectorAll(".split-key li");
    var tip = fig.querySelector(".split-tip");
    var bar = fig.querySelector(".split-bar");
    function on(i, part) {
      fig.classList.add("is-hovering");
      parts.forEach(function (p) { p.classList.toggle("is-on", p.getAttribute("data-i") === i); });
      keys.forEach(function (k) { k.classList.toggle("is-on", k.getAttribute("data-i") === i); });
      if (!tip || !part) return;
      tip.textContent = "";
      var b = document.createElement("b");
      b.textContent = part.getAttribute("data-name");
      tip.appendChild(b);
      tip.appendChild(document.createTextNode(part.getAttribute("data-amount") + " · " + part.getAttribute("data-detail")));
      tip.hidden = false;
      var fr = fig.getBoundingClientRect(), pr = part.getBoundingClientRect();
      var x = pr.left - fr.left + pr.width / 2;
      var half = tip.offsetWidth / 2;
      x = Math.max(half, Math.min(fr.width - half, x));
      tip.style.left = x + "px";
      tip.style.top = (pr.top - fr.top - 6) + "px";
    }
    function off() {
      fig.classList.remove("is-hovering");
      parts.forEach(function (p) { p.classList.remove("is-on"); });
      keys.forEach(function (k) { k.classList.remove("is-on"); });
      if (tip) tip.hidden = true;
    }
    parts.forEach(function (p) {
      p.addEventListener("pointerenter", function () { on(p.getAttribute("data-i"), p); });
      p.addEventListener("pointerdown", function () { on(p.getAttribute("data-i"), p); });
    });
    keys.forEach(function (k) {
      k.addEventListener("pointerenter", function () { on(k.getAttribute("data-i"), parts[+k.getAttribute("data-i")]); });
      k.addEventListener("pointerleave", off);
    });
    if (bar) bar.addEventListener("pointerleave", off);
  }

  document.querySelectorAll("[data-figure]").forEach(initFigure);
  document.querySelectorAll("svg.chart[data-tips]").forEach(initChart);
  document.querySelectorAll("[data-finder]").forEach(initFinder);
  document.querySelectorAll("[data-split]").forEach(initSplit);
  document.querySelectorAll("[data-count]").forEach(initCount);
  initReveal();
})();
