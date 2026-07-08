/* MRF Explorer SPA — vanilla JS, no external dependencies. */
"use strict";

const $ = (sel, el = document) => el.querySelector(sel);
const $$ = (sel, el = document) => [...el.querySelectorAll(sel)];

const state = {
  view: "explorer",
  filters: { payers: [], cpts: [], modifier: "", billing_class: "", q: "", dollar: true, rate_min: "", rate_max: "" },
  sort: { col: "negotiated_rate", dir: "desc" },
  page: 1,
  pageSize: 100,
  cptDescriptions: {},
  cptSelected: null,
  filesTimer: null,
};

const fmtMoney = (v) =>
  v == null ? "–" : Number(v).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const fmtInt = (v) => (v == null ? "–" : Number(v).toLocaleString("en-US"));
const esc = (s) =>
  String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

async function api(path) {
  const r = await fetch(path);
  if (!r.ok) throw new Error(`${path}: HTTP ${r.status}`);
  return r.json();
}

function debounce(fn, ms) {
  let t;
  return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
}

/* ---------- tooltip ---------- */
const tip = $("#tooltip");
function showTip(html, ev) {
  tip.innerHTML = html;
  tip.style.display = "block";
  moveTip(ev);
}
function moveTip(ev) {
  const pad = 14;
  let x = ev.clientX + pad, y = ev.clientY + pad;
  const r = tip.getBoundingClientRect();
  if (x + r.width > innerWidth - 8) x = ev.clientX - r.width - pad;
  if (y + r.height > innerHeight - 8) y = ev.clientY - r.height - pad;
  tip.style.left = x + "px";
  tip.style.top = y + "px";
}
function hideTip() { tip.style.display = "none"; }

/* ---------- nav ---------- */
$$("nav button").forEach((b) =>
  b.addEventListener("click", () => switchView(b.dataset.view))
);
function switchView(view) {
  state.view = view;
  $$("nav button").forEach((b) => b.classList.toggle("active", b.dataset.view === view));
  $$(".view").forEach((v) => v.classList.toggle("active", v.id === `view-${view}`));
  clearInterval(state.filesTimer);
  if (view === "files") {
    loadFiles();
    state.filesTimer = setInterval(loadFiles, 4000);
  }
  if (view === "cpt" && !state.cptSelected) {
    const first = Object.keys(state.cptDescriptions)[0];
    if (first) selectCpt(first);
  }
  if (view === "sources") loadSources();
}

/* ---------- header stats ---------- */
async function loadStats() {
  try {
    const s = await api("/api/stats");
    $("#dataset-stats").innerHTML =
      `<b>${fmtInt(s.rates)}</b> rate rows · <b>${fmtInt(s.orgs)}</b> NPIs · ` +
      `<b>${s.payers}</b> payer${s.payers === 1 ? "" : "s"} · <b>${s.files_done}</b> files`;
  } catch {
    $("#dataset-stats").textContent = "API unreachable";
  }
}

/* =======================================================================
   EXPLORER
   ======================================================================= */

function filterQuery(extra = {}) {
  const f = state.filters;
  const p = new URLSearchParams();
  if (f.payers.length) p.set("payer", f.payers.join(","));
  if (f.cpts.length) p.set("cpt", f.cpts.join(","));
  if (f.modifier) p.set("modifier", f.modifier);
  if (f.billing_class) p.set("billing_class", f.billing_class);
  if (f.q) p.set("q", f.q);
  p.set("dollar_only", f.dollar ? "1" : "0");
  if (f.rate_min !== "") p.set("rate_min", f.rate_min);
  if (f.rate_max !== "") p.set("rate_max", f.rate_max);
  for (const [k, v] of Object.entries(extra)) p.set(k, v);
  return p;
}

async function loadRates() {
  const body = $("#rates-body");
  const stateEl = $("#rates-state");
  stateEl.innerHTML = `<div class="loading">Loading rates</div>`;
  body.innerHTML = "";
  const q = filterQuery({
    sort: state.sort.col, dir: state.sort.dir,
    page: state.page, page_size: state.pageSize,
  });
  let data;
  try {
    data = await api(`/api/rates?${q}`);
  } catch (e) {
    stateEl.innerHTML = `<div class="empty"><h3>Could not load rates</h3>${esc(e.message)}</div>`;
    return;
  }
  if (!data.total) {
    stateEl.innerHTML = `<div class="empty"><h3>No rates match</h3>
      Drop MRF files into <code class="inline">data/inbox/</code> or loosen the filters.</div>`;
    $("#pg-label").textContent = "–";
    return;
  }
  stateEl.innerHTML = "";
  body.innerHTML = data.rows.map(rateRow).join("");
  $$("tr.clickable", body).forEach((tr) =>
    tr.addEventListener("click", () => openOrg(tr.dataset.npi))
  );
  const pages = Math.max(1, Math.ceil(data.total / state.pageSize));
  $("#pg-label").textContent = `page ${data.page} of ${fmtInt(pages)} · ${fmtInt(data.total)} rows`;
  $("#pg-prev").disabled = data.page <= 1;
  $("#pg-next").disabled = data.page >= pages;
  updateSortArrows();
}

function rateRow(r) {
  const mods = r.modifier_set
    ? r.modifier_set.split("|").map((m) => `<span class="mod-tag">${esc(m)}</span>`).join(" ")
    : `<span class="muted">—</span>`;
  const desc = state.cptDescriptions[r.billing_code];
  return `<tr class="clickable" data-npi="${esc(r.npi)}">
    <td>${r.org_name ? esc(r.org_name) : `<span class="muted">name pending…</span>`}
        <div class="sub">${esc(r.npi)}</div></td>
    <td>${esc(r.payer)}</td>
    <td>${esc(r.billing_code)}${desc ? `<div class="sub">${esc(desc)}</div>` : ""}</td>
    <td>${mods}</td>
    <td>${esc(r.billing_class || "—")}</td>
    <td class="num"><span class="rate">$${fmtMoney(r.negotiated_rate)}</span></td>
    <td>${esc(r.negotiated_type)}${r.is_dollar_rate ? "" : ` <span class="warn-text">(non-dollar)</span>`}</td>
    <td class="num">${fmtInt(r.source_count)}</td>
  </tr>`;
}

async function loadSummary() {
  const el = $("#summary-strip");
  try {
    const s = await api(`/api/summary?${filterQuery()}`);
    const stat = (k, v) => `<div class="stat"><div class="k">${k}</div><div class="v">${v}</div></div>`;
    el.innerHTML =
      stat("Rows", fmtInt(s.n)) +
      stat("Distinct orgs", fmtInt(s.orgs)) +
      stat("Min", "$" + fmtMoney(s.min)) +
      stat("P25", "$" + fmtMoney(s.p25)) +
      stat("Median", "$" + fmtMoney(s.median)) +
      stat("P75", "$" + fmtMoney(s.p75)) +
      stat("Max", "$" + fmtMoney(s.max));
  } catch {
    el.innerHTML = "";
  }
}

const refresh = () => { loadRates(); loadSummary(); };
const refreshFromFirstPage = () => { state.page = 1; refresh(); };

/* filter wiring */
async function initFilters() {
  const payers = (await api("/api/payers")).payers;
  const payerChips = $("#f-payer-chips");
  payerChips.innerHTML = payers.length
    ? payers.map((p) => `<button class="chip" data-payer="${esc(p)}">${esc(p)}</button>`).join("")
    : `<span class="muted">none ingested yet</span>`;
  payerChips.addEventListener("click", (ev) => {
    const b = ev.target.closest(".chip");
    if (!b) return;
    b.classList.toggle("on");
    state.filters.payers = $$(".chip.on", payerChips).map((x) => x.dataset.payer);
    refreshFromFirstPage();
  });

  const chips = $("#f-cpt-chips");
  chips.innerHTML = Object.entries(state.cptDescriptions)
    .map(([c, d]) => `<button class="chip" data-cpt="${c}" title="${esc(d)}">${c}</button>`)
    .join("");
  chips.addEventListener("click", (ev) => {
    const b = ev.target.closest(".chip");
    if (!b) return;
    b.classList.toggle("on");
    state.filters.cpts = $$(".chip.on", chips).map((x) => x.dataset.cpt);
    refreshFromFirstPage();
  });

  $("#f-modifier").addEventListener("change", (e) => { state.filters.modifier = e.target.value; refreshFromFirstPage(); });
  $("#f-class").addEventListener("change", (e) => { state.filters.billing_class = e.target.value; refreshFromFirstPage(); });
  $("#f-q").addEventListener("input", debounce((e) => { state.filters.q = e.target.value.trim(); refreshFromFirstPage(); }, 300));
  $("#f-rate-min").addEventListener("input", debounce((e) => { state.filters.rate_min = e.target.value; refreshFromFirstPage(); }, 400));
  $("#f-rate-max").addEventListener("input", debounce((e) => { state.filters.rate_max = e.target.value; refreshFromFirstPage(); }, 400));
  $("#f-dollar").addEventListener("change", (e) => { state.filters.dollar = e.target.checked; refreshFromFirstPage(); });
  $("#f-clear").addEventListener("click", () => {
    state.filters = { payers: [], cpts: [], modifier: "", billing_class: "", q: "", dollar: true, rate_min: "", rate_max: "" };
    $$(".chip.on", payerChips).forEach((c) => c.classList.remove("on"));
    $$(".chip.on", chips).forEach((c) => c.classList.remove("on"));
    $("#f-modifier").value = ""; $("#f-class").value = ""; $("#f-q").value = "";
    $("#f-rate-min").value = ""; $("#f-rate-max").value = ""; $("#f-dollar").checked = true;
    refreshFromFirstPage();
  });
  $("#btn-export").addEventListener("click", () => {
    location.href = `/api/export.csv?${filterQuery({ sort: state.sort.col, dir: state.sort.dir, view: "explorer" })}`;
  });

  /* sorting */
  $$("#rates-table thead th").forEach((th) =>
    th.addEventListener("click", () => {
      const col = th.dataset.sort;
      if (state.sort.col === col) state.sort.dir = state.sort.dir === "desc" ? "asc" : "desc";
      else state.sort = { col, dir: col === "negotiated_rate" || col === "source_count" ? "desc" : "asc" };
      refreshFromFirstPage();
    })
  );

  /* pagination */
  $("#pg-prev").addEventListener("click", () => { state.page = Math.max(1, state.page - 1); loadRates(); });
  $("#pg-next").addEventListener("click", () => { state.page += 1; loadRates(); });
  $("#pg-size").addEventListener("change", (e) => { state.pageSize = +e.target.value; refreshFromFirstPage(); });
}

function updateSortArrows() {
  $$("#rates-table thead th").forEach((th) => {
    const base = th.textContent.replace(/[▲▼]\s*$/, "").trim();
    th.innerHTML = esc(base) + (th.dataset.sort === state.sort.col
      ? ` <span class="arrow">${state.sort.dir === "desc" ? "▼" : "▲"}</span>` : "");
  });
}

/* =======================================================================
   ORG DETAIL DRAWER
   ======================================================================= */

async function openOrg(npi) {
  const drawer = $("#drawer"), overlay = $("#overlay");
  drawer.innerHTML = `<div class="loading">Loading NPI ${esc(npi)}</div>`;
  drawer.classList.add("open"); overlay.classList.add("open");
  overlay.onclick = closeOrg;
  let d;
  try {
    d = await api(`/api/org/${npi}`);
  } catch (e) {
    drawer.innerHTML = `<button class="close">×</button><div class="empty">${esc(e.message)}</div>`;
    $(".close", drawer).onclick = closeOrg;
    return;
  }
  const dir = d.directory || {};
  const payers = [...new Set(d.rates.map((r) => r.payer))];
  drawer.innerHTML = `
    <button class="close" aria-label="close">×</button>
    <h2>${esc(dir.org_name || "Unenriched NPI")}</h2>
    <div class="meta">NPI ${esc(npi)}${dir.city ? ` · ${esc(dir.city)}, ${esc(dir.state)}` : ""}${dir.taxonomy_desc ? ` · ${esc(dir.taxonomy_desc)}` : ""}</div>
    <h3>Median dollar rate by code${payers.length > 1 ? " and payer" : ` — ${esc(payers[0] ?? "")}`}</h3>
    ${payers.length > 1 ? `<div class="legend">${payers.slice(0, 2).map((p, i) =>
      `<span><span class="sw" style="background:var(--accent${i ? "-2" : ""})"></span>${esc(p)}</span>`).join("")}</div>` : ""}
    <div id="org-chart"></div>
    <h3>All rates${payers.length > 1 ? " · side-by-side payers" : ""}</h3>
    <div class="tablewrap" style="max-height:44vh">
      <table>
        <thead><tr><th>Code</th><th>Payer</th><th>Modifiers</th><th>Class</th><th>Type</th><th class="num">Rate</th><th class="num">Sources</th></tr></thead>
        <tbody>${d.rates.map((r) => `
          <tr>
            <td>${esc(r.billing_code)}<div class="sub">${esc(state.cptDescriptions[r.billing_code] || "")}</div></td>
            <td>${esc(r.payer)}</td>
            <td>${r.modifier_set ? r.modifier_set.split("|").map((m) => `<span class="mod-tag">${esc(m)}</span>`).join(" ") : '<span class="muted">—</span>'}</td>
            <td>${esc(r.billing_class || "—")}</td>
            <td>${esc(r.negotiated_type)}${r.is_dollar_rate ? "" : ' <span class="warn-text">(non-dollar)</span>'}</td>
            <td class="num"><span class="rate">$${fmtMoney(r.negotiated_rate)}</span></td>
            <td class="num">${fmtInt(r.source_count)}</td>
          </tr>`).join("")}
        </tbody>
      </table>
    </div>`;
  $(".close", drawer).onclick = closeOrg;
  renderOrgChart($("#org-chart"), d.chart, payers.slice(0, 2));
}
function closeOrg() {
  $("#drawer").classList.remove("open");
  $("#overlay").classList.remove("open");
}
document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeOrg(); });

/* Horizontal grouped bar chart: median rate by billing code (≤2 payer series). */
function renderOrgChart(el, chart, payers) {
  if (!chart.length) { el.innerHTML = `<div class="muted">no dollar rates</div>`; return; }
  const codes = [...new Set(chart.map((c) => c.billing_code))].sort();
  const byKey = Object.fromEntries(chart.map((c) => [`${c.billing_code}|${c.payer}`, c.median_rate]));
  const maxV = Math.max(...chart.map((c) => c.median_rate));
  const barH = 14, gap = 2, groupPad = 10, left = 58, right = 74, width = 640;
  const rows = payers.length || 1;
  const groupH = rows * barH + (rows - 1) * gap + groupPad;
  const height = codes.length * groupH + 26;
  const x = (v) => left + (v / maxV) * (width - left - right);

  let svg = `<svg class="chart" viewBox="0 0 ${width} ${height}" role="img" aria-label="median rate by billing code">`;
  const ticks = niceTicks(maxV, 4);
  for (const t of ticks) {
    svg += `<line class="grid-line" x1="${x(t)}" y1="6" x2="${x(t)}" y2="${height - 20}"/>` +
           `<text class="lbl" x="${x(t)}" y="${height - 6}" text-anchor="middle">$${fmtInt(t)}</text>`;
  }
  svg += `<line class="baseline" x1="${left}" y1="6" x2="${left}" y2="${height - 20}"/>`;
  codes.forEach((code, gi) => {
    const gy = gi * groupH + 8;
    svg += `<text x="${left - 8}" y="${gy + (groupH - groupPad) / 2 + 4}" text-anchor="end">${esc(code)}</text>`;
    (payers.length ? payers : [""]).forEach((p, si) => {
      const v = byKey[`${code}|${p}`];
      if (v == null) return;
      const y = gy + si * (barH + gap);
      const w = Math.max(x(v) - left, 2);
      svg += `<path class="bar ${si ? "s2" : ""}" d="${roundedRight(left, y, w, barH, 4)}"
        data-tip="${esc(code)} · ${esc(p || "median")}: $${fmtMoney(v)}"/>`;
      svg += `<text class="dlabel" x="${x(v) + 6}" y="${y + barH - 3}">$${fmtMoney(v)}</text>`;
    });
  });
  svg += "</svg>";
  el.innerHTML = svg;
  hookBarTips(el);
}

/* Vertical histogram for the CPT view. */
function renderHistogram(el, hist, code) {
  if (!hist.length) { el.innerHTML = ""; return; }
  const maxN = Math.max(...hist.map((b) => b.n));
  const w = Math.min(1200, Math.max(560, hist.length * 44));
  const left = 40, bottom = 26, top = 12, h = 190;
  const bw = (w - left - 16) / hist.length;
  const y = (n) => top + (1 - n / maxN) * (h - top - bottom);
  let svg = `<svg class="chart" viewBox="0 0 ${w} ${h}" role="img" aria-label="rate distribution for ${esc(code)}" style="max-width:${w}px">`;
  for (const t of niceTicks(maxN, 3)) {
    svg += `<line class="grid-line" x1="${left}" y1="${y(t)}" x2="${w - 10}" y2="${y(t)}"/>` +
           `<text class="lbl" x="${left - 6}" y="${y(t) + 4}" text-anchor="end">${fmtInt(t)}</text>`;
  }
  hist.forEach((b, i) => {
    const bx = left + i * bw;
    const bh = (h - top - bottom) * (b.n / maxN);
    svg += `<path class="bar" d="${roundedTop(bx + 1, h - bottom - bh, Math.max(bw - 2, 2), Math.max(bh, 1), 4)}"
      data-tip="$${fmtMoney(b.bucket)}+ · ${fmtInt(b.n)} rate${b.n === 1 ? "" : "s"}"/>`;
    if (i % Math.ceil(hist.length / 8) === 0)
      svg += `<text class="lbl" x="${bx + bw / 2}" y="${h - 8}" text-anchor="middle">$${fmtMoney(b.bucket)}</text>`;
  });
  svg += `<line class="baseline" x1="${left}" y1="${h - bottom}" x2="${w - 10}" y2="${h - bottom}"/></svg>`;
  el.innerHTML = svg;
  hookBarTips(el);
}

function hookBarTips(el) {
  $$(".bar", el).forEach((b) => {
    b.addEventListener("mouseenter", (ev) => showTip(esc(b.dataset.tip), ev));
    b.addEventListener("mousemove", moveTip);
    b.addEventListener("mouseleave", hideTip);
  });
}

/* rounded far-end bar paths (data end rounded, baseline end square) */
function roundedRight(x, y, w, h, r) {
  r = Math.min(r, w, h / 2);
  return `M${x},${y} h${w - r} a${r},${r} 0 0 1 ${r},${r} v${h - 2 * r} a${r},${r} 0 0 1 -${r},${r} h-${w - r} z`;
}
function roundedTop(x, y, w, h, r) {
  r = Math.min(r, h, w / 2);
  return `M${x},${y + h} v-${h - r} a${r},${r} 0 0 1 ${r},-${r} h${w - 2 * r} a${r},${r} 0 0 1 ${r},${r} v${h - r} z`;
}
function niceTicks(max, n) {
  if (!isFinite(max) || max <= 0) return [];
  const raw = max / n;
  const mag = Math.pow(10, Math.floor(Math.log10(raw)));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => s >= raw) || mag * 10;
  const out = [];
  for (let v = step; v <= max; v += step) out.push(+v.toFixed(2));
  return out;
}

/* =======================================================================
   CPT COMPARISON
   ======================================================================= */

function initCptView() {
  const chips = $("#cpt-chips");
  chips.innerHTML = Object.keys(state.cptDescriptions)
    .map((c) => `<button class="chip" data-cpt="${c}" title="${esc(state.cptDescriptions[c])}">${c}</button>`)
    .join("");
  chips.addEventListener("click", (ev) => {
    const b = ev.target.closest(".chip");
    if (b) selectCpt(b.dataset.cpt);
  });
  $("#cpt-base-only").addEventListener("change", () => state.cptSelected && selectCpt(state.cptSelected));
  $("#cpt-export").addEventListener("click", () => {
    if (!state.cptSelected) return;
    const p = new URLSearchParams({ cpt: state.cptSelected, view: `cpt_${state.cptSelected}`, sort: "negotiated_rate", dir: "desc" });
    if ($("#cpt-base-only").checked) p.set("modifier", "base");
    location.href = `/api/export.csv?${p}`;
  });
}

async function selectCpt(code) {
  state.cptSelected = code;
  $$("#cpt-chips .chip").forEach((c) => c.classList.toggle("on", c.dataset.cpt === code));
  const body = $("#cpt-body"), stateEl = $("#cpt-state");
  body.innerHTML = "";
  stateEl.innerHTML = `<div class="loading">Loading ${esc(code)}</div>`;
  const p = new URLSearchParams();
  if ($("#cpt-base-only").checked) p.set("modifier", "base");
  let d;
  try {
    d = await api(`/api/cpt/${code}?${p}`);
  } catch (e) {
    stateEl.innerHTML = `<div class="empty">${esc(e.message)}</div>`;
    return;
  }
  $("#view-cpt h2").textContent = `Who gets paid most for ${code}${d.description ? " — " + d.description : ""}`;
  renderHistogram($("#cpt-hist"), d.histogram, code);
  if (!d.ranked.length) {
    stateEl.innerHTML = `<div class="empty"><h3>No dollar rates for ${esc(code)}</h3></div>`;
    return;
  }
  stateEl.innerHTML = "";
  body.innerHTML = d.ranked.map((r, i) => `
    <tr class="clickable" data-npi="${esc(r.npi)}">
      <td class="num muted">${i + 1}</td>
      <td>${r.org_name ? esc(r.org_name) : '<span class="muted">name pending…</span>'}<div class="sub">${esc(r.npi)}</div></td>
      <td>${esc(r.payer)}</td>
      <td>${r.modifier_set ? r.modifier_set.split("|").map((m) => `<span class="mod-tag">${esc(m)}</span>`).join(" ") : '<span class="muted">—</span>'}</td>
      <td>${esc(r.billing_class || "—")}</td>
      <td class="num"><span class="rate">$${fmtMoney(r.median_rate)}</span></td>
      <td class="num">$${fmtMoney(r.min_rate)}</td>
      <td class="num">$${fmtMoney(r.max_rate)}</td>
      <td class="num">${fmtInt(r.n)}</td>
    </tr>`).join("");
  $$("tr.clickable", body).forEach((tr) => tr.addEventListener("click", () => openOrg(tr.dataset.npi)));
}

/* =======================================================================
   FILES
   ======================================================================= */

async function loadFiles() {
  const body = $("#files-body"), stateEl = $("#files-state");
  let d;
  try {
    d = await api("/api/files");
  } catch (e) {
    stateEl.innerHTML = `<div class="empty">${esc(e.message)}</div>`;
    return;
  }
  if (!d.files.length) {
    body.innerHTML = "";
    stateEl.innerHTML = `<div class="empty"><h3>No files yet</h3>
      Drop an MRF into <code class="inline">data/inbox/</code> or use the upload zone above.</div>`;
    return;
  }
  stateEl.innerHTML = "";
  body.innerHTML = d.files.map((f) => {
    let warn = "";
    if (f.ref_groups_skipped > 0)
      warn = `<span class="warn-text">⚠ ${fmtInt(f.ref_groups_skipped)} rate groups skipped — missing provider reference file</span>`;
    let statusExtra = "";
    if (f.status === "pending_confirmation")
      statusExtra = ` <button class="btn" style="padding:2px 8px;font-size:12px" data-confirm="${esc(f.filename)}">ingest anyway</button>`;
    return `<tr>
      <td>${esc(f.filename)}</td>
      <td>${esc(f.payer || "?")}</td>
      <td>${esc(f.file_type || "?")}${f.schema_version && !String(f.schema_version).startsWith("2.") ? ` <span class="warn-text">v${esc(f.schema_version)}</span>` : ""}</td>
      <td><span class="badge ${esc(f.status)}">${esc(f.status)}</span>${statusExtra}</td>
      <td class="num">${fmtInt(f.rows_emitted)}</td>
      <td>${warn}${f.error ? `<div class="err-text">${esc(f.error)}</div>` : ""}</td>
      <td class="sub">${f.finished_at ? esc(f.finished_at.slice(0, 19)) : ""}</td>
    </tr>`;
  }).join("");
  $$("[data-confirm]", body).forEach((b) =>
    b.addEventListener("click", async () => {
      b.disabled = true;
      await fetch(`/api/files/${encodeURIComponent(b.dataset.confirm)}/confirm`, { method: "POST" });
      loadFiles();
    })
  );
  loadStats();
}

function initFilesView() {
  $("#btn-scan").addEventListener("click", async () => {
    await fetch("/api/files/scan", { method: "POST" });
    setTimeout(loadFiles, 800);
  });
  const dz = $("#dropzone"), input = $("#file-input");
  dz.addEventListener("click", () => input.click());
  input.addEventListener("change", () => uploadFiles([...input.files]));
  ["dragenter", "dragover"].forEach((t) =>
    dz.addEventListener(t, (e) => { e.preventDefault(); dz.classList.add("drag"); })
  );
  ["dragleave", "drop"].forEach((t) =>
    dz.addEventListener(t, (e) => { e.preventDefault(); dz.classList.remove("drag"); })
  );
  dz.addEventListener("drop", (e) => uploadFiles([...e.dataTransfer.files]));
}

async function uploadFiles(files) {
  for (const f of files) {
    if (f.size > 1 << 30) {
      alert(`${f.name} is over 1 GB — drop it into data/inbox/ instead.`);
      continue;
    }
    const fd = new FormData();
    fd.append("file", f);
    $("#dropzone").innerHTML = `<div class="loading">Uploading ${esc(f.name)}</div>`;
    try {
      const r = await fetch("/api/upload", { method: "POST", body: fd });
      if (!r.ok) alert(`upload failed: ${(await r.json()).detail || r.status}`);
    } catch (e) {
      alert(`upload failed: ${e.message}`);
    }
  }
  location.reload();
}

/* =======================================================================
   SOURCES
   ======================================================================= */

let sourcesLoaded = false;
async function loadSources(stateCode = "") {
  const d = await api(`/api/sources${stateCode ? `?state=${stateCode}` : ""}`);
  if (!sourcesLoaded) {
    const sel = $("#src-state");
    sel.innerHTML = `<option value="">— pick a state —</option>` +
      d.states.map((s) => `<option>${s}</option>`).join("");
    sel.addEventListener("change", () => loadSources(sel.value));
    $("#src-national-cards").innerHTML = d.national.map(sourceCard).join("");
    hookOverrideForms($("#src-national-cards"));
    sourcesLoaded = true;
  }
  const banner = $("#src-elevance");
  const cards = $("#src-state-cards");
  if (stateCode && d.state.length) {
    cards.innerHTML = d.state.map(sourceCard).join("");
    hookOverrideForms(cards);
    const anyElevance = d.state.some((c) => (c.parent || "").includes("Elevance"));
    banner.style.display = anyElevance && d.elevance_note ? "block" : "none";
    banner.textContent = d.elevance_note || "";
    if (d.state.length > 1) {
      cards.insertAdjacentHTML("afterbegin",
        `<div class="note-banner" style="grid-column:1/-1">This state has <b>${d.state.length} BCBS licensees</b> —
         the right one depends on where the target practice sits within the state.</div>`);
    }
  } else {
    cards.innerHTML = stateCode ? `<div class="muted">no registry entry for ${esc(stateCode)}</div>` : "";
    banner.style.display = "none";
  }
}

function sourceCard(c) {
  const verified = c.verified;
  const badge = verified
    ? `<span class="badge done">verified${c.verified_locally ? " (locally)" : ""}</span>`
    : `<span class="badge pending_confirmation">unverified — confirm link</span>`;
  const search = `https://www.google.com/search?q=${encodeURIComponent(c.name + " machine readable files")}`;
  let linkHtml;
  if (c.mrf_url && verified) {
    linkHtml = `<a href="${esc(c.mrf_url)}" target="_blank" rel="noopener">${esc(c.mrf_url)}</a>`;
  } else if (c.mrf_url) {
    linkHtml = `<span class="muted">best-known (unconfirmed):</span><br>
      <a href="${esc(c.mrf_url)}" target="_blank" rel="noopener">${esc(c.mrf_url)}</a>`;
  } else {
    linkHtml = `<a href="${search}" target="_blank" rel="noopener">search for current URL ↗</a>`;
  }
  const paste = verified ? "" : `
    <div class="paste">
      <input type="url" placeholder="paste confirmed https:// URL…" data-key="${esc(c.key)}">
      <button class="btn" data-save="${esc(c.key)}">Confirm</button>
    </div>`;
  return `<div class="source-card">
    <h4>${esc(c.name)} ${badge}</h4>
    <div class="parent">${esc(c.parent || "")}</div>
    ${linkHtml}
    ${c.notes ? `<div class="notes">${esc(c.notes)}</div>` : ""}
    ${paste}
  </div>`;
}

function hookOverrideForms(root) {
  $$("[data-save]", root).forEach((btn) =>
    btn.addEventListener("click", async () => {
      const input = $(`input[data-key="${CSS.escape(btn.dataset.save)}"]`, root);
      const url = input.value.trim();
      if (!url.startsWith("https://")) { alert("paste a full https:// URL"); return; }
      const r = await fetch("/api/sources/override", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ key: btn.dataset.save, mrf_url: url }),
      });
      if (r.ok) { sourcesLoaded = false; loadSources($("#src-state").value); }
      else alert("could not save override");
    })
  );
}

/* =======================================================================
   BOOT
   ======================================================================= */

(async function boot() {
  state.cptDescriptions = await api("/api/cpt_descriptions").catch(() => ({}));
  loadStats();
  await initFilters();
  initCptView();
  initFilesView();
  refresh();
  setInterval(loadStats, 15000);
})();
