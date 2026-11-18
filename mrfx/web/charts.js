/* Vendored from the 0.8.7 design handoff (charts.js): window.MRFX —
   formatters, one tooltip, sparkline, line, bullet, histogram, heat grid,
   dot matrix, ring, radar, calendar, state tiles, city map, count-up.
   Hand-drawn SVG / Canvas; no dependencies, no network. */
/* MRF Explorer — chart & UI primitives. Hand-drawn SVG (Canvas for >2,000 cells).
   One classic script; assigns window.MRFX. No dependencies, no network. */
(function () {
  "use strict";
  var NS = "http://www.w3.org/2000/svg";
  var reduce = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  var drawn = Object.create(null); // charts animate only on their first draw
  function firstDraw(key) { if (reduce || !key) return false; if (drawn[key]) return false; drawn[key] = 1; return true; }

  /* ── formatting ───────────────────────────────────── */
  var fmt = {
    usd: function (v, dp) { if (v == null || isNaN(v)) return "—"; dp = dp == null ? 2 : dp; return (v < 0 ? "−$" : "$") + Math.abs(v).toLocaleString("en-US", { minimumFractionDigits: dp, maximumFractionDigits: dp }); },
    int: function (v) { return v == null ? "—" : Math.round(v).toLocaleString("en-US"); },
    pct: function (v, dp) { return v == null ? "—" : (v * 100).toFixed(dp == null ? 0 : dp) + "%"; },
    signedPct: function (v, dp) { if (v == null) return "—"; var s = (Math.abs(v) * 100).toFixed(dp == null ? 1 : dp); return (v > 0.0005 ? "+" : v < -0.0005 ? "−" : "±") + s + "%"; },
    signedUsd: function (v) { if (v == null) return "—"; return (v > 0.004 ? "+" : v < -0.004 ? "−" : "±") + "$" + Math.abs(v).toFixed(2); },
    ord: function (n) { n = Math.round(n); var s = ["th", "st", "nd", "rd"], v = n % 100; return n + (s[(v - 20) % 10] || s[v] || s[0]); },
    compact: function (v) { if (v >= 1e6) return (v / 1e6).toFixed(v >= 1e7 ? 1 : 2).replace(/\.0+$/, "") + "M"; if (v >= 1e3) return (v / 1e3).toFixed(v >= 1e4 ? 0 : 1).replace(/\.0$/, "") + "k"; return String(Math.round(v)); },
    esc: function (s) { return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) { return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]; }); }
  };
  var esc = fmt.esc;
  var MIN_N = 5; // markets with fewer practices are "not enough data", never a number

  /* ── tooltip (one element, delegated) ─────────────── */
  var tipEl, tipReg = Object.create(null), tipSeq = 0, pinned = null;
  function tipKey(html) { var k = "t" + (++tipSeq); tipReg[k] = html; return k; }
  function basis(b) {
    if (!b) return "";
    var parts = [];
    if (b.n != null) parts.push("n = " + fmt.int(b.n) + " practice" + (b.n === 1 ? "" : "s"));
    if (b.asOf) parts.push("as of " + b.asOf);
    if (b.group) parts.push("vs " + b.group);
    var thin = b.n != null && b.n < MIN_N ? '<div class="thin" style="margin-top:4px">! Not enough data — fewer than 5 practices</div>' : "";
    return '<div class="tip-basis">' + esc(parts.join(" · ")) + thin + "</div>";
  }
  function tipHTML(title, rows, b) {
    var h = title ? '<div class="tip-title">' + title + "</div>" : "";
    (rows || []).forEach(function (r) { h += '<div class="tip-row"><span>' + (r.c ? '<i style="background:' + r.c + '"></i>' : "") + esc(r[0]) + "</span><b>" + r[1] + "</b></div>"; });
    return h + basis(b);
  }
  function ensureTip() { if (!tipEl) { tipEl = document.createElement("div"); tipEl.className = "tip"; tipEl.setAttribute("role", "tooltip"); tipEl.id = "mrfx-tip"; document.body.appendChild(tipEl); } return tipEl; }
  function showTip(html, x, y) {
    var t = ensureTip(); t.innerHTML = html; t.classList.add("show");
    var w = t.offsetWidth, h = t.offsetHeight, vw = window.innerWidth, vh = window.innerHeight;
    var left = x + 14, top = y + 14;
    if (left + w > vw - 8) left = x - w - 14;
    if (left < 8) left = 8;
    if (top + h > vh - 8) top = y - h - 14;
    if (top < 8) top = 8;
    t.style.left = left + "px"; t.style.top = top + "px";
  }
  function hideTip() { if (tipEl) tipEl.classList.remove("show"); pinned = null; }
  function tipFor(el) { var k = el.getAttribute("data-tip"); if (k && tipReg[k]) return tipReg[k]; var b = el.getAttribute("data-basis"); if (b) { try { var o = JSON.parse(b); return tipHTML(o.title || "", o.rows || [], o); } catch (e) {} } return null; }
  document.addEventListener("pointerover", function (e) {
    if (e.pointerType === "touch") return;
    var el = e.target.closest && e.target.closest("[data-tip],[data-basis]"); if (!el) return;
    var h = tipFor(el); if (h) showTip(h, e.clientX, e.clientY);
  });
  document.addEventListener("pointermove", function (e) {
    if (e.pointerType === "touch" || !tipEl || !tipEl.classList.contains("show") || pinned) return;
    var el = e.target.closest && e.target.closest("[data-tip],[data-basis],[data-crosshair]"); if (!el) { hideTip(); return; }
    if (el.hasAttribute("data-crosshair")) return;
    showTip(tipEl.innerHTML, e.clientX, e.clientY);
  });
  document.addEventListener("pointerout", function (e) {
    if (e.pointerType === "touch") return;
    var el = e.target.closest && e.target.closest("[data-tip],[data-basis]");
    if (el && !(e.relatedTarget && el.contains(e.relatedTarget))) hideTip();
  });
  // touch: tap toggles a pinned tooltip; tap elsewhere closes
  document.addEventListener("click", function (e) {
    var el = e.target.closest && e.target.closest("[data-tip],[data-basis]");
    if (!el) { if (pinned) hideTip(); return; }
    if (e.pointerType === "mouse") return;
    var h = tipFor(el); if (!h) return;
    var r = el.getBoundingClientRect();
    if (pinned === el) { hideTip(); return; }
    showTip(h, r.left + r.width / 2, r.top + r.height / 2); pinned = el;
  });
  document.addEventListener("focusin", function (e) {
    var el = e.target, fb = el.getAttribute && el.getAttribute("data-focus-basis");
    if (fb) { try { var ob = JSON.parse(fb), rr = el.getBoundingClientRect(); showTip(tipHTML(esc(ob.title || ""), ob.rows || [], ob), rr.left + 40, rr.bottom); } catch (er) {} return; }
    if (!el.matches || !el.matches("[data-tip],[data-basis]")) return;
    var h = tipFor(el); if (!h) return; var r = el.getBoundingClientRect(); showTip(h, r.right, r.top);
  });
  document.addEventListener("focusout", function () { if (!pinned) hideTip(); });
  window.addEventListener("scroll", function () { if (tipEl && tipEl.classList.contains("show")) hideTip(); }, { passive: true, capture: true });

  /* ── helpers ──────────────────────────────────────── */
  function scale(d0, d1, r0, r1) { var k = (r1 - r0) / ((d1 - d0) || 1); return function (v) { return r0 + (v - d0) * k; }; }
  function nice(max) { var e = Math.pow(10, Math.floor(Math.log10(max || 1))), m = max / e; var n = m <= 1 ? 1 : m <= 2 ? 2 : m <= 2.5 ? 2.5 : m <= 5 ? 5 : 10; return n * e; }
  function ticks(min, max, count) { var step = nice((max - min) / (count || 4)); var t = [], v = Math.ceil(min / step) * step; for (; v <= max + 1e-9; v += step) t.push(+v.toFixed(6)); return t; }
  // monotone cubic path through points (smooth, never overshoots)
  function smooth(pts) {
    var n = pts.length; if (n < 2) return ""; if (n === 2) return "M" + pts[0][0] + "," + pts[0][1] + "L" + pts[1][0] + "," + pts[1][1];
    var dx = [], dy = [], m = [], t = [];
    for (var i = 0; i < n - 1; i++) { dx[i] = pts[i + 1][0] - pts[i][0]; dy[i] = pts[i + 1][1] - pts[i][1]; m[i] = dy[i] / dx[i]; }
    t[0] = m[0]; t[n - 1] = m[n - 2];
    for (i = 1; i < n - 1; i++) t[i] = m[i - 1] * m[i] <= 0 ? 0 : (3 * (dx[i - 1] + dx[i])) / ((2 * dx[i] + dx[i - 1]) / m[i - 1] + (dx[i] + 2 * dx[i - 1]) / m[i]);
    var d = "M" + pts[0][0].toFixed(1) + "," + pts[0][1].toFixed(1);
    for (i = 0; i < n - 1; i++) {
      var h = dx[i] / 3;
      d += "C" + (pts[i][0] + h).toFixed(1) + "," + (pts[i][1] + h * t[i]).toFixed(1) + " " + (pts[i + 1][0] - h).toFixed(1) + "," + (pts[i + 1][1] - h * t[i + 1]).toFixed(1) + " " + pts[i + 1][0].toFixed(1) + "," + pts[i + 1][1].toFixed(1);
    }
    return d;
  }
  var uid = 0; function id(p) { return (p || "g") + (++uid); }
  function cssVar(name) { return getComputedStyle(document.documentElement).getPropertyValue(name).trim(); }
  function width(el, fallback) { var w = el.clientWidth || (el.parentNode && el.parentNode.clientWidth) || fallback || 600; return Math.max(240, w); }
  function setLen(svg) { svg.querySelectorAll(".draw-line").forEach(function (p) { var l = p.getTotalLength ? p.getTotalLength() : 1000; p.style.setProperty("--len", Math.ceil(l)); }); }

  /* ── keyboard: one tab stop per chart, arrows move between its marks ── */
  function rove(root, cols) {
    var items = Array.prototype.slice.call(root.querySelectorAll(".focusable")); if (!items.length) return;
    var svg = root.querySelector("svg"); if (svg) svg.setAttribute("role", "group");
    items.forEach(function (el, i) {
      el.setAttribute("tabindex", i ? "-1" : "0");
      var k = el.getAttribute("data-tip"); if (k && tipReg[k]) { var d = document.createElement("div"); d.innerHTML = tipReg[k].replace(/<\/span><b>/g, ": </span><b>").replace(/<\/div>/g, ". </div>"); el.setAttribute("aria-label", d.textContent.replace(/\s+/g, " ").trim()); el.setAttribute("role", "img"); }
    });
    root.addEventListener("keydown", function (e) {
      var i = items.indexOf(document.activeElement); if (i < 0) return;
      var step = { ArrowRight: 1, ArrowLeft: -1, ArrowDown: cols || 1, ArrowUp: -(cols || 1), Home: -i, End: items.length - 1 - i }[e.key];
      if (step == null) return; e.preventDefault();
      var j = Math.max(0, Math.min(items.length - 1, i + step)); items[i].setAttribute("tabindex", "-1"); items[j].setAttribute("tabindex", "0"); items[j].focus();
    });
  }

  /* ── sparkline ────────────────────────────────────── */
  function spark(values, opt) {
    opt = opt || {}; var w = opt.w || 112, h = opt.h || 36, c = opt.color || "var(--accent)";
    var min = Math.min.apply(null, values), max = Math.max.apply(null, values); if (max === min) { max += 1; min -= 1; }
    var x = scale(0, values.length - 1, 1, w - 3), y = scale(min, max, h - 3, 3);
    var pts = values.map(function (v, i) { return [x(i), y(v)]; });
    var g = id("sp"), line = smooth(pts);
    var anim = firstDraw(opt.key);
    return '<svg class="spark" viewBox="0 0 ' + w + " " + h + '" preserveAspectRatio="none" aria-hidden="true"><defs><linearGradient id="' + g + '" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="' + c + '" stop-opacity=".16"/><stop offset="1" stop-color="' + c + '" stop-opacity="0"/></linearGradient></defs>' +
      '<path d="' + line + "L" + (w - 3) + "," + h + "L1," + h + 'Z" fill="url(#' + g + ')"' + (anim ? ' class="fade-in"' : "") + "/>" +
      '<path d="' + line + '" fill="none" stroke="' + c + '" stroke-width="1.6" vector-effect="non-scaling-stroke" stroke-linecap="round"/>' +
      '<circle cx="' + pts[pts.length - 1][0] + '" cy="' + pts[pts.length - 1][1] + '" r="2.5" fill="' + c + '"/></svg>';
  }

  /* ── line / area chart with crosshair ─────────────── */
  function line(el, o) {
    var W = width(el), H = o.height || 260, m = { t: 12, r: 14, b: 28, l: 52 };
    var all = []; o.series.forEach(function (s) { s.values.forEach(function (v) { if (v != null) all.push(v); }); });
    var min = o.min != null ? o.min : Math.min.apply(null, all), max = Math.max.apply(null, all);
    var pad = (max - min) * 0.12 || 1; min = o.min != null ? o.min : Math.max(0, min - pad); max += pad;
    var yt = ticks(min, max, 4); min = Math.min(min, yt[0]); max = Math.max(max, yt[yt.length - 1]);
    var n = o.labels.length, x = scale(0, n - 1, m.l, W - m.r), y = scale(min, max, H - m.b, m.t);
    var anim = firstDraw(o.key), g = id("lg"), yf = o.yfmt || fmt.compact;
    var s = '<svg viewBox="0 0 ' + W + " " + H + '" role="img" aria-label="' + esc(o.label || "") + '"><defs>';
    s += '<linearGradient id="' + g + '" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="' + o.series[0].color + '" stop-opacity=".18"/><stop offset=".85" stop-color="' + o.series[0].color + '" stop-opacity="0"/></linearGradient></defs>';
    yt.forEach(function (t) { s += '<line class="gridline" x1="' + m.l + '" x2="' + (W - m.r) + '" y1="' + y(t) + '" y2="' + y(t) + '"/><text x="' + (m.l - 10) + '" y="' + (y(t) + 4) + '" text-anchor="end">' + yf(t) + "</text>"; });
    var every = Math.max(1, Math.ceil(n / Math.max(3, Math.floor((W - m.l) / 72))));
    o.labels.forEach(function (l, i) { if (i % every === 0 || i === n - 1) { if (i !== n - 1 && x(n - 1) - x(i) < 64) return; s += '<text x="' + x(i) + '" y="' + (H - 8) + '" text-anchor="' + (i === 0 ? "start" : i === n - 1 ? "end" : "middle") + '">' + esc(l) + "</text>"; } });
    o.series.forEach(function (ser, si) {
      var pts = []; ser.values.forEach(function (v, i) { if (v != null) pts.push([x(i), y(v)]); });
      var d = smooth(pts);
      if (si === 0 && o.area !== false) s += '<path d="' + d + "L" + pts[pts.length - 1][0] + "," + (H - m.b) + "L" + pts[0][0] + "," + (H - m.b) + 'Z" fill="url(#' + g + ')"' + (anim ? ' class="fade-in"' : "") + "/>";
      s += '<path d="' + d + '" fill="none" stroke="' + ser.color + '" stroke-width="' + (si === 0 ? 1.75 : 1.5) + '"' + (ser.dash ? ' stroke-dasharray="4 4"' : "") + ' stroke-linecap="round"' + (anim ? ' class="draw-line"' : "") + "/>";
    });
    s += '<line class="xh" x1="0" x2="0" y1="' + m.t + '" y2="' + (H - m.b) + '" stroke="rgba(255,255,255,.25)" stroke-width="1" style="display:none"/>';
    o.series.forEach(function (ser, si) { s += '<circle class="xd" data-i="' + si + '" r="4" fill="' + ser.color + '" stroke="var(--card)" stroke-width="2" style="display:none"/>'; });
    s += '<rect class="hit" data-crosshair x="' + m.l + '" y="' + m.t + '" width="' + (W - m.l - m.r) + '" height="' + (H - m.t - m.b) + '" tabindex="0" aria-label="Chart data — use arrow keys"/></svg>';
    el.innerHTML = s; var svg = el.querySelector("svg"); if (anim) setLen(svg);
    var hit = svg.querySelector(".hit"), xh = svg.querySelector(".xh"), dots = svg.querySelectorAll(".xd"), cur = n - 1;
    function at(i, cx, cy) {
      i = Math.max(0, Math.min(n - 1, i)); cur = i;
      xh.setAttribute("x1", x(i)); xh.setAttribute("x2", x(i)); xh.style.display = "";
      o.series.forEach(function (ser, si) { var v = ser.values[i]; if (v == null) { dots[si].style.display = "none"; return; } dots[si].setAttribute("cx", x(i)); dots[si].setAttribute("cy", y(v)); dots[si].style.display = ""; });
      var rows = o.series.map(function (ser) { return { 0: ser.name, 1: ser.values[i] == null ? "—" : (o.tipfmt || yf)(ser.values[i]), c: ser.color }; });
      if (o.extraRows) rows = rows.concat(o.extraRows(i));
      var r = svg.getBoundingClientRect(), sx = r.width / W;
      showTip(tipHTML(esc(o.labels[i]), rows, o.basis ? o.basis(i) : null), cx != null ? cx : r.left + x(i) * sx, cy != null ? cy : r.top + m.t * sx);
    }
    function fromEvt(e) { var r = svg.getBoundingClientRect(), px = (e.clientX - r.left) * (W / r.width); return Math.round((px - m.l) / ((W - m.l - m.r) / (n - 1 || 1))); }
    hit.addEventListener("pointermove", function (e) { at(fromEvt(e), e.clientX, e.clientY); });
    hit.addEventListener("pointerdown", function (e) { at(fromEvt(e), e.clientX, e.clientY); });
    hit.addEventListener("pointerleave", function () { xh.style.display = "none"; dots.forEach(function (d) { d.style.display = "none"; }); hideTip(); });
    hit.addEventListener("keydown", function (e) { if (e.key === "ArrowLeft" || e.key === "ArrowRight") { e.preventDefault(); at(cur + (e.key === "ArrowRight" ? 1 : -1)); } });
    hit.addEventListener("focus", function () { at(cur); });
    hit.addEventListener("blur", function () { xh.style.display = "none"; hideTip(); });
  }

  /* ── bullet (one row; returns svg string) ─────────── */
  // bullet: HTML, not SVG, so the "you" dot stays round at any width
  function bullet(r, opt) {
    opt = opt || {};
    if (r.n < MIN_N) return '<div class="bullet thin-data" aria-hidden="true"><span class="b-range" style="left:0;width:100%"></span></div>';
    var lo = Math.min(r.p10, r.you == null ? Infinity : r.you) * 0.97, hi = Math.max(r.p90, r.you == null ? 0 : r.you) * 1.02;
    var x = function (v) { return ((v - lo) / ((hi - lo) || 1) * 100).toFixed(2); }, anim = firstDraw(opt.key);
    var s = '<div class="bullet" aria-hidden="true">';
    s += '<span class="b-range" style="left:' + x(r.p10) + '%;width:' + (x(r.p90) - x(r.p10)).toFixed(2) + '%"></span>';
    s += '<span class="b-band' + (anim ? " grow-x" : "") + '" style="left:' + x(r.p25) + '%;width:' + (x(r.p75) - x(r.p25)).toFixed(2) + '%"></span>';
    s += '<span class="b-med" style="left:' + x(r.median) + '%"></span>';
    if (r.you != null) {
      var a = Math.min(+x(r.median), +x(r.you)), w = Math.abs(x(r.you) - x(r.median));
      s += '<span class="b-gap ' + (r.you >= r.median ? "up" : "down") + '" style="left:' + a + "%;width:" + w.toFixed(2) + '%"></span>';
      s += '<span class="b-you" style="left:' + x(r.you) + '%"></span>';
    }
    return s + "</div>";
  }
  function bulletTip(r, b) {
    if (r.n < MIN_N) return tipKey(tipHTML(esc(r.code + " · " + r.desc), [["Market", "Not enough data"]], b));
    var rows = [["Your rate", fmt.usd(r.you)], ["p10", fmt.usd(r.p10)], ["p25", fmt.usd(r.p25)], ["Median", fmt.usd(r.median)], ["p75", fmt.usd(r.p75)], ["p90", fmt.usd(r.p90)], ["Your percentile", fmt.ord(r.pctile)]];
    if (r.you != null) rows.splice(1, 0, ["vs median", '<span class="' + (r.you >= r.median ? "up" : "down") + '">' + (r.you >= r.median ? "▲ " : "▼ ") + fmt.signedPct(r.you / r.median - 1) + "</span>"]);
    return tipKey(tipHTML(esc(r.code + " · " + r.desc), rows, b));
  }

  /* ── histogram ────────────────────────────────────── */
  function histogram(el, o) {
    var W = width(el), H = o.height || 240, m = { t: 26, r: 12, b: 30, l: 40 };
    var bins = o.bins, maxC = Math.max.apply(null, bins.map(function (b) { return b.count; }));
    var x = scale(bins[0].x0, bins[bins.length - 1].x1, m.l, W - m.r), y = scale(0, nice(maxC), H - m.b, m.t), anim = firstDraw(o.key);
    var s = '<svg viewBox="0 0 ' + W + " " + H + '" role="img" aria-label="' + esc(o.label || "Rate distribution") + '">';
    s += '<rect x="' + x(o.p25) + '" y="' + m.t + '" width="' + (x(o.p75) - x(o.p25)) + '" height="' + (H - m.t - m.b) + '" fill="var(--accent-soft)" rx="4"/>';
    ticks(0, nice(maxC), 3).forEach(function (t) { s += '<line class="gridline" x1="' + m.l + '" x2="' + (W - m.r) + '" y1="' + y(t) + '" y2="' + y(t) + '"/><text x="' + (m.l - 8) + '" y="' + (y(t) + 4) + '" text-anchor="end">' + t + "</text>"; });
    bins.forEach(function (b, i) {
      var bx = x(b.x0) + 1, bw = Math.max(1, x(b.x1) - x(b.x0) - 2), by = y(b.count);
      var inBand = b.x1 > o.p25 && b.x0 < o.p75;
      var k = tipKey(tipHTML(fmt.usd(b.x0) + " – " + fmt.usd(b.x1), [["Practices", fmt.int(b.count)], ["Share", fmt.pct(b.count / o.n, 1)]], o.basis));
      s += '<rect class="focusable' + (anim ? " grow-y" : "") + '" style="animation-delay:' + (anim ? i * 12 : 0) + 'ms" x="' + bx + '" y="' + by + '" width="' + bw + '" height="' + Math.max(0, H - m.b - by) + '" rx="' + Math.min(2, bw / 2) + '" fill="' + (inBand ? "var(--accent)" : "var(--seq-3)") + '" data-tip="' + k + '" tabindex="-1"/>';
      s += '<rect class="hit" x="' + bx + '" y="' + m.t + '" width="' + bw + '" height="' + (H - m.t - m.b) + '" data-tip="' + k + '"/>';
    });
    s += '<line class="baseline" x1="' + m.l + '" x2="' + (W - m.r) + '" y1="' + (H - m.b) + '" y2="' + (H - m.b) + '"/>';
    ticks(bins[0].x0, bins[bins.length - 1].x1, Math.max(3, Math.floor(W / 110))).forEach(function (t) { if (x(t) < m.l || x(t) > W - m.r) return; s += '<text x="' + x(t) + '" y="' + (H - 10) + '" text-anchor="middle">$' + t + "</text>"; });
    var mx = x(o.median); s += '<line x1="' + mx + '" x2="' + mx + '" y1="' + m.t + '" y2="' + (H - m.b) + '" stroke="var(--ink-2)" stroke-width="1.5" stroke-dasharray="3 3"/>';
    if (o.you != null) {
      var yx = x(o.you), anchor = yx > W - 80 ? "end" : "start", off = anchor === "end" ? -6 : 6;
      var k2 = tipKey(tipHTML("You", [["Your rate", fmt.usd(o.you)], ["Percentile", fmt.ord(o.youPct)], ["vs median", fmt.signedPct(o.you / o.median - 1)]], o.basis));
      s += '<line x1="' + yx + '" x2="' + yx + '" y1="' + (m.t - 4) + '" y2="' + (H - m.b) + '" stroke="var(--accent)" stroke-width="2.5"/>';
      s += '<circle cx="' + yx + '" cy="' + (m.t - 4) + '" r="5" fill="var(--accent)" stroke="var(--card)" stroke-width="2" data-tip="' + k2 + '" tabindex="0" class="focusable"/>';
      s += '<text class="lbl-strong" x="' + (yx + off) + '" y="' + (m.t + 10) + '" text-anchor="' + anchor + '">You ' + fmt.usd(o.you) + "</text>";
    }
    el.innerHTML = s + "</svg>"; rove(el, 0);
  }

  /* ── diverging heat grid (SVG ≤ 2,000 cells; Canvas above) ── */
  var DIV = ["--div-down-3", "--div-down-2", "--div-down-1", "--div-mid", "--div-up-1", "--div-up-2", "--div-up-3"];
  var DIV_EDGES = [0.85, 0.95, 0.99, 1.01, 1.05, 1.15];
  function divIdx(ratio) { for (var i = 0; i < DIV_EDGES.length; i++) if (ratio < DIV_EDGES[i]) return i; return 6; }
  function divColor(ratio) { return "var(" + DIV[divIdx(ratio)] + ")"; }
  function divScaleLegend(lowLabel, highLabel) {
    return '<div class="heat-scale"><span>' + (lowLabel || "▼ Below market") + '</span><span class="sw">' + DIV.map(function (v) { return '<i style="background:var(' + v + ')"></i>'; }).join("") + "</span><span>" + (highLabel || "Above market ▲") + '</span><span class="muted">· hatched = below · ▢ = not enough data</span></div>';
  }
  function hatchDefs(pid) {
    return '<pattern id="' + pid + '" width="6" height="6" patternUnits="userSpaceOnUse" patternTransform="rotate(45)"><line x1="0" y1="0" x2="0" y2="6" stroke="rgba(255,255,255,.16)" stroke-width="2"/></pattern>';
  }
  function heatGrid(el, o) {
    var rows = o.rows, cols = o.cols;
    if (rows.length * cols.length > 2000) return heatCanvas(el, o);
    var W = width(el), lw = o.labelW || 150, top = 34, ch = o.cellH || 40, gap = 3;
    var cw = Math.max(34, (W - lw) / cols.length);
    W = lw + cw * cols.length; var H = top + rows.length * (ch + gap);
    var pid = id("hatch"), anim = firstDraw(o.key);
    var s = '<svg viewBox="0 0 ' + W + " " + H + '" role="img" aria-label="' + esc(o.label || "") + '"><defs>' + hatchDefs(pid) + "</defs>";
    cols.forEach(function (c, ci) { s += '<text x="' + (lw + ci * cw + cw / 2) + '" y="18" text-anchor="middle" class="lbl-ink">' + esc(c.label || c) + "</text>"; });
    rows.forEach(function (r, ri) {
      var yy = top + ri * (ch + gap);
      s += '<text x="0" y="' + (yy + ch / 2 + 4) + '" class="lbl-ink">' + esc(r.label || r) + "</text>";
      cols.forEach(function (c, ci) {
        var d = o.cell(r, c), xx = lw + ci * cw + gap / 2, w = cw - gap;
        if (!d || d.n < MIN_N) {
          var kt = tipKey(tipHTML(esc((r.label || r) + " · " + (c.label || c)), d ? [["Market", "Not enough data"]] : [["Your rate", "None on file — you don't bill this code with this payer"]], d ? d.basis : null));
          s += '<rect x="' + xx + '" y="' + yy + '" width="' + w + '" height="' + ch + '" rx="3" fill="none" stroke="var(--line-strong)" stroke-dasharray="3 3" data-tip="' + kt + '" tabindex="0" class="focusable"/>';
          s += '<text x="' + (xx + w / 2) + '" y="' + (yy + ch / 2 + 4) + '" text-anchor="middle">—</text>';
          return;
        }
        var ratio = d.ratio, below = ratio < 0.99, above = ratio > 1.01;
        var k = tipKey(tipHTML(esc((r.label || r) + " · " + (c.label || c)), [["Your rate", fmt.usd(d.you)], ["Market median", fmt.usd(d.median)], ["You vs median", '<span class="' + (below ? "down" : above ? "up" : "") + '">' + (below ? "▼ " : above ? "▲ " : "") + Math.round(ratio * 100) + "%</span>"]], d.basis));
        s += '<rect class="cell focusable' + (anim ? " fade-in" : "") + '" style="animation-delay:' + (anim ? (ri * cols.length + ci) * 8 : 0) + 'ms" x="' + xx + '" y="' + yy + '" width="' + w + '" height="' + ch + '" rx="3" fill="' + divColor(ratio) + '" data-tip="' + k + '" tabindex="0"/>';
        if (below) s += '<rect x="' + xx + '" y="' + yy + '" width="' + w + '" height="' + ch + '" rx="3" fill="url(#' + pid + ')" pointer-events="none"/>';
        if (w >= 46) s += '<text x="' + (xx + w / 2) + '" y="' + (yy + ch / 2 + 4) + '" text-anchor="middle" pointer-events="none" style="fill:' + (divIdx(ratio) === 6 ? "var(--on-accent)" : "var(--ink)") + ';font-weight:500">' + (below ? "▼" : above ? "▲" : "") + Math.round(ratio * 100) + "</text>";
        else s += '<text x="' + (xx + w / 2) + '" y="' + (yy + ch / 2 + 4) + '" text-anchor="middle" pointer-events="none" style="fill:var(--ink)">' + (below ? "▼" : above ? "▲" : "•") + "</text>";
      });
    });
    el.innerHTML = s + "</svg>" + divScaleLegend(o.lowLabel, o.highLabel); rove(el, cols.length);
  }
  function heatCanvas(el, o) {
    var rows = o.rows, cols = o.cols, W = width(el), lw = o.labelW || 0, top = 22;
    var cw = (W - lw) / cols.length, ch = o.cellH || 4, H = top + rows.length * ch;
    var dpr = Math.min(2, window.devicePixelRatio || 1);
    el.innerHTML = '<div class="chart" style="position:relative"><canvas class="heat-canvas" role="img" aria-label="' + esc(o.label || "") + '"></canvas></div>' + divScaleLegend(o.lowLabel, o.highLabel);
    var cv = el.querySelector("canvas"); cv.width = W * dpr; cv.height = H * dpr; cv.style.height = H + "px";
    var ctx = cv.getContext("2d"); ctx.scale(dpr, dpr);
    var pal = DIV.map(cssVar), mid = cssVar("--raised");
    ctx.font = "500 11px " + cssVar("--font-sans"); ctx.fillStyle = cssVar("--ink-2"); ctx.textAlign = "center";
    cols.forEach(function (c, ci) { ctx.fillText(c.label || c, lw + ci * cw + cw / 2, 14); });
    rows.forEach(function (r, ri) {
      cols.forEach(function (c, ci) {
        var d = o.cell(r, c); var xx = lw + ci * cw, yy = top + ri * ch;
        ctx.fillStyle = !d || d.n < MIN_N ? mid : pal[divIdx(d.ratio)];
        ctx.fillRect(xx + 0.5, yy, cw - 1, Math.max(1, ch - (ch > 3 ? 1 : 0)));
      });
    });
    cv.addEventListener("pointermove", function (e) {
      var rc = cv.getBoundingClientRect(), px = (e.clientX - rc.left) * (W / rc.width), py = (e.clientY - rc.top) * (H / rc.height);
      var ci = Math.floor((px - lw) / cw), ri = Math.floor((py - top) / ch);
      if (ci < 0 || ri < 0 || ci >= cols.length || ri >= rows.length) { hideTip(); return; }
      var r = rows[ri], c = cols[ci], d = o.cell(r, c);
      var title = esc((r.label || r) + " · " + (c.label || c));
      if (!d || d.n < MIN_N) showTip(tipHTML(title, [["Market", "Not enough data"]], d && d.basis), e.clientX, e.clientY);
      else showTip(tipHTML(title, [["Rate", fmt.usd(d.you)], ["Market median", fmt.usd(d.median)], ["vs median", (d.ratio < 0.99 ? "▼ " : d.ratio > 1.01 ? "▲ " : "") + Math.round(d.ratio * 100) + "%"]], d.basis), e.clientX, e.clientY);
    });
    cv.addEventListener("pointerleave", hideTip);
  }

  /* ── dot-matrix: code × percentile band, circle area = practices ── */
  function dotMatrix(el, o) {
    var W = width(el), lw = 70, top = 26, rh = 30, bands = o.bands, rows = o.rows;
    var cw = (W - lw) / bands.length, H = top + rows.length * rh + 6, maxN = 0;
    rows.forEach(function (r) { r.counts.forEach(function (c) { if (c > maxN) maxN = c; }); });
    var rmax = Math.min(cw, rh) / 2 - 2, anim = firstDraw(o.key);
    var s = '<svg viewBox="0 0 ' + W + " " + H + '" role="img" aria-label="Where you sit for each code by percentile">';
    bands.forEach(function (b, bi) { s += '<text x="' + (lw + bi * cw + cw / 2) + '" y="14" text-anchor="middle">' + esc(b) + "</text>"; });
    rows.forEach(function (r, ri) {
      var cy = top + ri * rh + rh / 2;
      s += '<text x="0" y="' + (cy + 4) + '" class="lbl-ink" style="font-family:var(--font-mono)">' + esc(r.code) + "</text>";
      s += '<line class="gridline" x1="' + lw + '" x2="' + W + '" y1="' + cy + '" y2="' + cy + '"/>';
      if (r.n < MIN_N) { s += '<text x="' + (lw + 6) + '" y="' + (cy + 4) + '" style="fill:var(--warn)">! Not enough data (n = ' + r.n + ")</text>"; return; }
      r.counts.forEach(function (c, bi) {
        var cx = lw + bi * cw + cw / 2, rad = c ? Math.max(2, rmax * Math.sqrt(c / maxN)) : 1.5, me = bi === r.youBand;
        var k = tipKey(tipHTML(esc(r.code + " · " + bands[bi] + " percentile band"), [["Practices in band", fmt.int(c)], ["Rate range", bi === 0 ? "under " + fmt.usd(r.edges[1]) : bi === bands.length - 1 ? "over " + fmt.usd(r.edges[bi]) : fmt.usd(r.edges[bi]) + " – " + fmt.usd(r.edges[bi + 1])]].concat(me ? [["You", fmt.usd(r.you) + " (" + fmt.ord(r.pctile) + ")"]] : []), o.basis(r)));
        s += '<circle class="focusable' + (anim ? " fade-in" : "") + '" style="animation-delay:' + (anim ? ri * 30 + bi * 10 : 0) + 'ms" cx="' + cx + '" cy="' + cy + '" r="' + rad.toFixed(1) + '" fill="' + (me ? "var(--accent)" : c ? "var(--seq-3)" : "var(--faint)") + '" data-tip="' + k + '" tabindex="' + (me ? 0 : -1) + '"/>';
        if (me) s += '<circle cx="' + cx + '" cy="' + cy + '" r="' + (rad + 4).toFixed(1) + '" fill="none" stroke="var(--accent-strong)" stroke-width="1.5" pointer-events="none"/>';
      });
    });
    el.innerHTML = s + "</svg>"; rove(el, bands.length);
  }

  /* ── ring gauge ───────────────────────────────────── */
  function ring(value, o) {
    o = o || {}; var r = 46, c = 2 * Math.PI * r, v = Math.max(0, Math.min(1, value)), anim = firstDraw(o.key);
    var col = o.color || "var(--accent)";
    return '<svg viewBox="0 0 112 112" role="img" aria-label="' + esc(o.label || "") + " " + Math.round(v * 100) + '%"><circle cx="56" cy="56" r="' + r + '" fill="none" stroke="var(--raised)" stroke-width="7"/>' +
      '<circle cx="56" cy="56" r="' + r + '" fill="none" stroke="' + col + '" stroke-width="7" stroke-linecap="round" transform="rotate(-90 56 56)" stroke-dasharray="' + c.toFixed(1) + '" stroke-dashoffset="' + (c * (1 - v)).toFixed(1) + '"' + (anim ? ' style="transition:stroke-dashoffset 900ms var(--ease)" data-from="' + c.toFixed(1) + '"' : "") + "/>" +
      '<text x="56" y="60" text-anchor="middle" style="fill:var(--ink);font-size:21px;font-weight:500">' + Math.round(v * 100) + '<tspan style="font-size:13px;fill:var(--muted)">%</tspan></text></svg>';
  }
  function animateRings(root) {
    (root || document).querySelectorAll("circle[data-from]").forEach(function (cEl) {
      var to = cEl.getAttribute("stroke-dashoffset"); cEl.setAttribute("stroke-dashoffset", cEl.getAttribute("data-from")); cEl.removeAttribute("data-from");
      requestAnimationFrame(function () { requestAnimationFrame(function () { cEl.setAttribute("stroke-dashoffset", to); }); });
    });
  }

  /* ── radar (≤ 3 series: the all-pairs-safe slots) ── */
  function radar(el, o) {
    var W = Math.min(width(el), 440), H = W * 0.86, cx = W / 2, cy = H / 2 + 6, R = Math.min(W, H) / 2 - 36;
    var axes = o.axes, n = axes.length, min = o.min, max = o.max, anim = firstDraw(o.key);
    function pt(i, v) { var a = -Math.PI / 2 + (i * 2 * Math.PI) / n, rr = R * (v - min) / (max - min); return [cx + rr * Math.cos(a), cy + rr * Math.sin(a)]; }
    var s = '<svg viewBox="0 0 ' + W + " " + H + '" role="img" aria-label="' + esc(o.label || "") + '" style="max-width:' + W + 'px;margin:0 auto">';
    o.rings.forEach(function (rv) { var p = axes.map(function (a, i) { return pt(i, rv).join(","); }).join(" "); s += '<polygon points="' + p + '" fill="none" stroke="' + (rv === 1 ? "rgba(255,255,255,.28)" : "var(--grid)") + '" stroke-dasharray="' + (rv === 1 ? "none" : "1 4") + '"/>'; });
    axes.forEach(function (a, i) { var e = pt(i, max), l = pt(i, max + (max - min) * 0.13); s += '<line x1="' + cx + '" y1="' + cy + '" x2="' + e[0] + '" y2="' + e[1] + '" stroke="var(--grid)"/><text x="' + l[0] + '" y="' + (l[1] + 4) + '" text-anchor="middle" class="lbl-ink" style="font-family:var(--font-mono)">' + esc(a) + "</text>"; });
    o.series.forEach(function (ser) {
      var p = ser.values.map(function (v, i) { return pt(i, Math.max(min, Math.min(max, v))).join(","); }).join(" ");
      s += '<polygon points="' + p + '" fill="' + ser.color + '" fill-opacity=".10" stroke="' + ser.color + '" stroke-width="1.75" stroke-linejoin="round"' + (anim ? ' class="fade-in"' : "") + "/>";
      ser.values.forEach(function (v, i) { var q = pt(i, Math.max(min, Math.min(max, v))); var k = tipKey(tipHTML(esc(ser.name + " · " + axes[i]), [["Rate as % of median", (v < 0.99 ? "▼ " : v > 1.01 ? "▲ " : "") + Math.round(v * 100) + "%"]], o.basis)); s += '<circle cx="' + q[0] + '" cy="' + q[1] + '" r="3.5" fill="' + ser.color + '" stroke="var(--card)" stroke-width="1.5" data-tip="' + k + '"/><circle cx="' + q[0] + '" cy="' + q[1] + '" r="11" class="hit" data-tip="' + k + '"/>'; });
    });
    el.innerHTML = s + "</svg>";
  }

  /* ── calendar heat map: payer × month, % change ──── */
  function calendar(el, o) {
    var W = width(el), lw = o.labelW || 150, top = 26, ch = 30, gap = 4;
    var cw = Math.max(24, (W - lw) / o.cols.length); W = lw + cw * o.cols.length;
    var H = top + o.rows.length * (ch + gap), pid = id("hatch"), anim = firstDraw(o.key);
    var s = '<svg viewBox="0 0 ' + W + " " + H + '" role="img" aria-label="' + esc(o.label || "") + '"><defs>' + hatchDefs(pid) + "</defs>";
    o.cols.forEach(function (c, ci) { s += '<text x="' + (lw + ci * cw + cw / 2) + '" y="14" text-anchor="middle">' + esc(c) + "</text>"; });
    o.rows.forEach(function (r, ri) {
      var yy = top + ri * (ch + gap);
      s += '<text x="0" y="' + (yy + ch / 2 + 4) + '" class="lbl-ink">' + esc(r.label) + "</text>";
      r.values.forEach(function (v, ci) {
        var xx = lw + ci * cw + gap / 2, w = cw - gap;
        if (v == null) { var kn = tipKey(tipHTML(esc(r.label + " · " + o.cols[ci]), [["File", "No file loaded for this month"]], null)); s += '<rect class="focusable" x="' + xx + '" y="' + yy + '" width="' + w + '" height="' + ch + '" rx="3" fill="none" stroke="var(--line-strong)" stroke-dasharray="2 3" data-tip="' + kn + '"/>'; return; }
        var ratio = 1 + v.chg * 4; // ±3.75% maps across the diverging arms
        var k = tipKey(tipHTML(esc(r.label + " · " + o.cols[ci]), [["Median rate change", '<span class="' + (v.chg < -0.0025 ? "down" : v.chg > 0.0025 ? "up" : "") + '">' + (v.chg < -0.0025 ? "▼ " : v.chg > 0.0025 ? "▲ " : "") + fmt.signedPct(v.chg, 1) + "</span>"], ["Codes changed", fmt.int(v.codes)], ["Practices affected", fmt.int(v.n)]], { n: v.n, asOf: o.cols[ci], group: o.group }));
        s += '<rect class="cell focusable' + (anim ? " fade-in" : "") + '" x="' + xx + '" y="' + yy + '" width="' + w + '" height="' + ch + '" rx="3" fill="' + divColor(ratio) + '" data-tip="' + k + '" tabindex="0"/>';
        if (v.chg < -0.0025) s += '<rect x="' + xx + '" y="' + yy + '" width="' + w + '" height="' + ch + '" rx="3" fill="url(#' + pid + ')" pointer-events="none"/>';
        if (w >= 30) s += '<text x="' + (xx + w / 2) + '" y="' + (yy + ch / 2 + 4) + '" text-anchor="middle" pointer-events="none" style="fill:var(--ink);font-size:10.5px">' + (v.chg < -0.0025 ? "▼" : v.chg > 0.0025 ? "▲" : "·") + "</text>";
      });
    });
    el.innerHTML = s + "</svg>" + divScaleLegend("▼ Cut", "Raised ▲"); rove(el, o.cols.length);
  }

  /* ── US state tile map (equal-area tiles, sequential) ── */
  var TILES = { AK: [0, 0], ME: [11, 0], VT: [10, 1], NH: [11, 1], WA: [1, 2], ID: [2, 2], MT: [3, 2], ND: [4, 2], MN: [5, 2], WI: [6, 2], MI: [8, 2], NY: [9, 2], MA: [10, 2], RI: [11, 2], OR: [1, 3], NV: [2, 3], WY: [3, 3], SD: [4, 3], IA: [5, 3], IL: [6, 3], IN: [7, 3], OH: [8, 3], PA: [9, 3], NJ: [10, 3], CT: [11, 3], CA: [1, 4], UT: [2, 4], CO: [3, 4], NE: [4, 4], MO: [5, 4], KY: [6, 4], WV: [7, 4], VA: [8, 4], MD: [9, 4], DE: [10, 4], AZ: [2, 5], NM: [3, 5], KS: [4, 5], AR: [5, 5], TN: [6, 5], NC: [7, 5], SC: [8, 5], DC: [9, 5], OK: [4, 6], LA: [5, 6], MS: [6, 6], AL: [7, 6], GA: [8, 6], HI: [0, 7], TX: [4, 7], FL: [9, 7] };
  function seqIdx(t) { return Math.max(1, Math.min(7, 1 + Math.round(t * 6))); }
  function seqColor(t) { return "var(--seq-" + seqIdx(t) + ")"; }
  function stateTiles(el, o) {
    var W = Math.min(width(el), 620), cols = 12, sz = (W - 2) / cols, gap = 3, H = sz * 8;
    var vals = Object.keys(o.data).map(function (k) { return o.data[k].median; }), lo = Math.min.apply(null, vals), hi = Math.max.apply(null, vals);
    var s = '<svg viewBox="0 0 ' + W + " " + H + '" role="img" aria-label="Median typical-visit value by state" style="max-width:' + W + 'px">';
    Object.keys(TILES).forEach(function (st) {
      var p = TILES[st], x = p[0] * sz + gap / 2, y = p[1] * sz + gap / 2, d = o.data[st], sel = st === o.selected;
      if (!d) { s += '<rect x="' + x + '" y="' + y + '" width="' + (sz - gap) + '" height="' + (sz - gap) + '" rx="4" fill="none" stroke="var(--line-strong)"/><text x="' + (x + (sz - gap) / 2) + '" y="' + (y + (sz - gap) / 2 + 4) + '" text-anchor="middle" style="fill:var(--muted)">' + st + "</text>"; return; }
      var t = (d.median - lo) / ((hi - lo) || 1), k = tipKey(tipHTML(esc(d.name), [["Median typical visit", d.n < MIN_N ? "Not enough data" : fmt.usd(d.median)], ["Practices", fmt.int(d.n)]], { n: d.n, asOf: o.asOf, group: o.group }));
      s += '<rect class="focusable" x="' + x + '" y="' + y + '" width="' + (sz - gap) + '" height="' + (sz - gap) + '" rx="4" fill="' + seqColor(t) + '"' + (sel ? ' stroke="var(--ink)" stroke-width="2"' : "") + ' data-tip="' + k + '" tabindex="0" data-state="' + st + '" style="cursor:pointer"/>';
      s += '<text x="' + (x + (sz - gap) / 2) + '" y="' + (y + (sz - gap) / 2 + 4) + '" text-anchor="middle" pointer-events="none" style="fill:' + (seqIdx(t) >= 5 ? "var(--on-accent)" : "var(--ink)") + ';font-weight:600">' + st + "</text>";
    });
    el.innerHTML = s + '</svg><div class="heat-scale"><span>' + fmt.usd(lo) + '</span><span class="sw">' + [1, 2, 3, 4, 5, 6, 7].map(function (i) { return '<i style="background:var(--seq-' + i + ')"></i>'; }).join("") + "</span><span>" + fmt.usd(hi) + '</span><span class="muted">· outlined = not loaded</span></div>'; rove(el, 0);
  }

  /* ── state outline + city bubbles ─────────────────── */
  // Tennessee outline, simplified to ~40 vertices (lat, lon) — shipped inline, no tiles or map service.
  var TN = [[36.50, -89.53], [36.50, -88.07], [36.64, -88.05], [36.68, -87.85], [36.64, -87.0], [36.63, -86.5], [36.62, -86.0], [36.62, -85.5], [36.61, -85.0], [36.60, -84.5], [36.59, -83.68], [36.59, -83.0], [36.59, -82.3], [36.59, -81.68], [36.40, -81.72], [36.25, -81.93], [36.12, -82.03], [36.05, -82.40], [35.95, -82.55], [35.80, -82.95], [35.70, -83.10], [35.56, -83.50], [35.52, -83.88], [35.30, -84.04], [35.20, -84.30], [34.99, -84.32], [34.99, -85.6], [34.99, -86.8], [34.99, -88.2], [34.99, -89.0], [34.99, -90.31], [35.20, -90.07], [35.45, -90.15], [35.70, -89.95], [35.95, -89.70], [36.10, -89.68], [36.25, -89.55], [36.38, -89.53]];
  var OUTLINES = { TN: TN };
  function cityMap(el, o) {
    var poly = OUTLINES[o.state] || TN, lats = poly.map(function (p) { return p[0]; }), lons = poly.map(function (p) { return p[1]; });
    var la0 = Math.min.apply(null, lats), la1 = Math.max.apply(null, lats), lo0 = Math.min.apply(null, lons), lo1 = Math.max.apply(null, lons);
    var kx = Math.cos(((la0 + la1) / 2) * Math.PI / 180), W = width(el), pad = 26;
    var sx = (W - pad * 2) / ((lo1 - lo0) * kx), H = (la1 - la0) * sx + pad * 2 + 10;
    function P(lat, lon) { return [pad + (lon - lo0) * kx * sx, pad + (la1 - lat) * sx]; }
    var d = poly.map(function (p, i) { var q = P(p[0], p[1]); return (i ? "L" : "M") + q[0].toFixed(1) + "," + q[1].toFixed(1); }).join("") + "Z";
    var meds = o.cities.filter(function (c) { return c.n >= MIN_N; }).map(function (c) { return c.median; }), lo = Math.min.apply(null, meds), hi = Math.max.apply(null, meds);
    var maxN = Math.max.apply(null, o.cities.map(function (c) { return c.n; })), anim = firstDraw(o.key);
    var s = '<svg viewBox="0 0 ' + W + " " + H + '" role="img" aria-label="Practices and median visit value by city">';
    s += '<path d="' + d + '" fill="var(--raised)" stroke="var(--line-strong)" stroke-width="1.2" stroke-linejoin="round"/>';
    var labels = [];
    o.cities.slice().sort(function (a, b) { return b.n - a.n; }).forEach(function (c, i) {
      var q = P(c.lat, c.lon), r = 5 + 22 * Math.sqrt(c.n / maxN), thin = c.n < MIN_N;
      var t = thin ? 0 : (c.median - lo) / ((hi - lo) || 1);
      var k = tipKey(tipHTML(esc(c.name), [["Median typical visit", thin ? "Not enough data" : fmt.usd(c.median)], ["Practices", fmt.int(c.n)]], { n: c.n, asOf: o.asOf, group: o.group }));
      s += '<circle class="focusable' + (anim ? " fade-in" : "") + '" style="animation-delay:' + (anim ? i * 30 : 0) + 'ms" cx="' + q[0].toFixed(1) + '" cy="' + q[1].toFixed(1) + '" r="' + r.toFixed(1) + '" fill="' + (thin ? "none" : seqColor(t)) + '" fill-opacity=".9" stroke="' + (thin ? "var(--warn)" : "var(--card)") + '" stroke-width="' + (thin ? 1.5 : 2) + '"' + (thin ? ' stroke-dasharray="3 2"' : "") + ' data-tip="' + k + '" tabindex="0"/>';
      labels.push({ name: c.name, x: q[0] + r + 4, y: q[1] + 4, n: c.n });
    });
    // labels: biggest cities first, skip any that would collide with one already placed
    var placed = [];
    labels.forEach(function (l) {
      var w = l.name.length * 6.2, box = [l.x, l.y - 11, l.x + w, l.y + 3];
      if (box[2] > W - 2) return; // would run off the right edge
      if (placed.some(function (b) { return !(box[2] < b[0] || box[0] > b[2] || box[3] < b[1] || box[1] > b[3]); })) return;
      placed.push(box); s += '<text x="' + l.x.toFixed(1) + '" y="' + l.y.toFixed(1) + '" class="lbl-ink" pointer-events="none">' + esc(l.name) + "</text>";
    });
    el.innerHTML = s + '</svg><div class="heat-scale"><span>Bubble = practices</span><span class="muted">·</span><span>' + fmt.usd(lo) + '</span><span class="sw">' + [1, 2, 3, 4, 5, 6, 7].map(function (i) { return '<i style="background:var(--seq-' + i + ')"></i>'; }).join("") + "</span><span>" + fmt.usd(hi) + ' median visit</span><span class="muted">· dashed amber = fewer than 5 practices</span></div>'; rove(el, 0);
  }

  /* ── count-up (once per key) ──────────────────────── */
  var counted = Object.create(null);
  function countUp(el, to, f, key) {
    f = f || fmt.int;
    if (reduce || (key && counted[key])) { el.textContent = f(to); return; }
    if (key) counted[key] = 1;
    var t0 = performance.now(), dur = 900;
    (function step(t) { var p = Math.min(1, (t - t0) / dur), e = 1 - Math.pow(1 - p, 3); el.textContent = f(to * e); if (p < 1) requestAnimationFrame(step); })(t0);
  }

  window.MRFX = { fmt: fmt, MIN_N: MIN_N, tip: { html: tipHTML, key: tipKey, show: showTip, hide: hideTip, basis: basis },
    spark: spark, line: line, bullet: bullet, bulletTip: bulletTip, histogram: histogram, heatGrid: heatGrid, heatCanvas: heatCanvas,
    divColor: divColor, divScaleLegend: divScaleLegend, dotMatrix: dotMatrix, ring: ring, animateRings: animateRings, radar: radar,
    calendar: calendar, stateTiles: stateTiles, cityMap: cityMap, countUp: countUp, smooth: smooth };
})();
