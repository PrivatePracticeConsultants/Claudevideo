/* MRF Explorer SPA — vanilla JS, no external dependencies. */
"use strict";

const $ = (sel, el = document) => el.querySelector(sel);
const $$ = (sel, el = document) => [...el.querySelectorAll(sel)];

const state = {
  view: "explorer",
  grain: "tin",
  filters: {
    payers: [], cpts: [], disciplines: [], modifier: "", mod_has: "", mod_not: "",
    billing_class: "", pos: "", state: "", city: "", month: "", q: "",
    dollar: true, hide_tin_npi: false, hide_outliers: false, rate_min: "", rate_max: "",
  },
  sort: { col: "negotiated_rate", dir: "desc" },
  page: 1,
  pageSize: 100,
  catalog: {},
  cptSelected: null,
  filesTimer: null,
  lastBenchmarkPayload: null,
};

const fmtMoney = (v) =>
  v == null ? "–" : Number(v).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const fmtInt = (v) => (v == null ? "–" : Number(v).toLocaleString("en-US"));
const esc = (s) =>
  String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

async function api(path, opts) {
  const r = await fetch(path, opts);
  if (!r.ok) {
    let msg = `HTTP ${r.status}`;
    try { msg = (await r.json()).detail || msg; } catch { /* keep */ }
    throw new Error(msg);
  }
  return r.json();
}
const postJson = (path, body) =>
  api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });

function debounce(fn, ms) {
  let t;
  return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
}

/* ---------- tooltip ---------- */
const tip = $("#tooltip");
function showTip(html, ev) { tip.innerHTML = html; tip.style.display = "block"; moveTip(ev); }
function moveTip(ev) {
  const pad = 14;
  let x = ev.clientX + pad, y = ev.clientY + pad;
  const r = tip.getBoundingClientRect();
  if (x + r.width > innerWidth - 8) x = ev.clientX - r.width - pad;
  if (y + r.height > innerHeight - 8) y = ev.clientY - r.height - pad;
  tip.style.left = x + "px"; tip.style.top = y + "px";
}
function hideTip() { tip.style.display = "none"; }

/* ---------- nav ---------- */
$$("nav button").forEach((b) => b.addEventListener("click", () => switchView(b.dataset.view)));
function switchView(view) {
  state.view = view;
  $$("nav button").forEach((b) => b.classList.toggle("active", b.dataset.view === view));
  $$(".view").forEach((v) => v.classList.toggle("active", v.id === `view-${view}`));
  clearInterval(state.filesTimer);
  if (view === "files") {
    loadFiles(); loadUrlQueue();
    state.filesTimer = setInterval(() => { loadFiles(); loadUrlQueue(); }, 4000);
  }
  if (view === "cpt" && !state.cptSelected) {
    const first = Object.keys(state.catalog)[0];
    if (first) selectCpt(first);
  }
  if (view === "benchmark") initBenchmark();
  if (view === "sources") loadSources();
}

/* ---------- header stats ---------- */
async function loadStats() {
  try {
    const s = await api("/api/stats");
    $("#dataset-stats").innerHTML =
      `<b>${fmtInt(s.rates)}</b> rate rows · <b>${fmtInt(s.tins)}</b> TINs · ` +
      `<b>${s.payers}</b> payer${s.payers === 1 ? "" : "s"} · <b>${s.files_done}</b> files`;
  } catch { $("#dataset-stats").textContent = "API unreachable"; }
}

/* =======================================================================
   EXPLORER
   ======================================================================= */

function filterQuery(extra = {}) {
  const f = state.filters;
  const p = new URLSearchParams();
  p.set("grain", state.grain);
  if (f.payers.length) p.set("payer", f.payers.join(","));
  if (f.cpts.length) p.set("cpt", f.cpts.join(","));
  if (f.disciplines.length) p.set("discipline", f.disciplines.join(","));
  if (f.modifier) p.set("modifier", f.modifier);
  if (f.mod_has) p.set("mod_has", f.mod_has);
  if (f.mod_not) p.set("mod_not", f.mod_not);
  if (f.billing_class) p.set("billing_class", f.billing_class);
  if (f.pos) p.set("pos", f.pos);
  if (f.state) p.set("state", f.state);
  if (f.city) p.set("city", f.city);
  if (f.month) p.set("month", f.month);
  if (f.q) p.set("q", f.q);
  p.set("dollar_only", f.dollar ? "1" : "0");
  if (f.hide_tin_npi) p.set("hide_tin_npi", "1");
  if (f.hide_outliers) p.set("hide_outliers", "1");
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
  const q = filterQuery({ sort: state.sort.col, dir: state.sort.dir, page: state.page, page_size: state.pageSize });
  let data;
  try { data = await api(`/api/rates?${q}`); }
  catch (e) { stateEl.innerHTML = `<div class="empty"><h3>Could not load rates</h3>${esc(e.message)}</div>`; return; }
  if (!data.total) {
    stateEl.innerHTML = `<div class="empty"><h3>No rates match</h3>
      Drop MRF files into <code class="inline">data/inbox/</code> or loosen the filters.</div>`;
    $("#pg-label").textContent = "–";
    return;
  }
  stateEl.innerHTML = "";
  body.innerHTML = data.rows.map(rateRow).join("");
  $$("tr.clickable", body).forEach((tr) =>
    tr.addEventListener("click", () => openEntity(data.grain, tr.dataset.uid)));
  const pages = Math.max(1, Math.ceil(data.total / state.pageSize));
  $("#pg-label").textContent = `page ${data.page} of ${fmtInt(pages)} · ${fmtInt(data.total)} rows`;
  $("#pg-prev").disabled = data.page <= 1;
  $("#pg-next").disabled = data.page >= pages;
  updateSortArrows();
}

function modTags(modset) {
  return modset
    ? modset.split("|").map((m) => `<span class="mod-tag">${esc(m)}</span>`).join(" ")
    : `<span class="muted">—</span>`;
}

function discTag(d) {
  if (!d || d === "unspecified")
    return `<span class="disc-tag unspec" title="shared code with no GP/GO/GN modifier">unspec.</span>`;
  return `<span class="disc-tag">${esc(d)}</span>`;
}

function rateRow(r) {
  const info = state.catalog[r.billing_code] || {};
  const sub = [];
  if (r.tin_value) sub.push(`TIN ${esc(r.tin_value)}`);
  if (state.grain === "entity" && r.tin_count > 1) sub.push(`${r.tin_count} TINs`);
  if (r.npi_count) sub.push(`${fmtInt(r.npi_count)} NPI${r.npi_count === 1 ? "" : "s"}`);
  if (r.tin_is_really_npi) sub.push(`<span class="warn-text" title="tin.type == 'npi': the published TIN is really an NPI">TIN=NPI</span>`);
  const variants = (r.rate_variants || 1) > 1
    ? `<span class="variant-flag" title="this entity shows ${r.rate_variants} different rates for this tuple ($${fmtMoney(r.rate_min)}–$${fmtMoney(r.rate_max)}); the shown rate is their median">${r.rate_variants} ⚠</span>`
    : `<span class="muted">1</span>`;
  return `<tr class="clickable" data-uid="${esc(r.unit_id)}">
    <td>${esc(r.display_name || "(name pending)")}<div class="sub">${sub.join(" · ")}</div></td>
    <td>${esc(r.payer)}</td>
    <td>${esc(r.billing_code)}${info.timed ? `<span class="timed-tag" title="timed 15-minute-unit code">15-min</span>` : ""}
        ${info.description ? `<div class="sub">${esc(info.description)}</div>` : ""}</td>
    <td>${discTag(r.discipline)}</td>
    <td>${modTags(r.modifier_set)}</td>
    <td>${esc(r.billing_class || "—")}</td>
    <td class="sub">${esc((r.service_code_set || "").replaceAll("|", ", ") || "—")}</td>
    <td class="num"><span class="rate">$${fmtMoney(r.negotiated_rate)}</span></td>
    <td class="num">${variants}</td>
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
      stat(state.grain === "npi" ? "NPIs" : "Entities", fmtInt(s.entities)) +
      stat("Codes", fmtInt(s.codes)) +
      stat("Min", "$" + fmtMoney(s.min)) +
      stat("P25", "$" + fmtMoney(s.p25)) +
      stat("Median", "$" + fmtMoney(s.median)) +
      stat("P75", "$" + fmtMoney(s.p75)) +
      stat("Max", "$" + fmtMoney(s.max));
  } catch { el.innerHTML = ""; }
}

const refresh = () => { loadRates(); loadSummary(); };
const refreshFromFirstPage = () => { state.page = 1; refresh(); };

function codeChipsHtml() {
  const groups = { PT: [], OT: [], SLP: [] };
  for (const [code, info] of Object.entries(state.catalog)) {
    const d = info.disciplines?.[0] || "PT";
    (groups[d] || groups.PT).push([code, info]);
  }
  return Object.entries(groups).map(([d, codes]) =>
    codes.map(([c, info]) =>
      `<button class="chip" data-cpt="${c}" title="${esc(info.description)} (${info.disciplines.join("/")}${info.timed ? ", timed" : ""})">${c}<span class="disc">${esc(info.disciplines.join("/"))}</span></button>`
    ).join("")
  ).join("");
}

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

  $("#f-grain").addEventListener("click", (ev) => {
    const b = ev.target.closest("button");
    if (!b) return;
    $$("#f-grain button").forEach((x) => x.classList.toggle("on", x === b));
    state.grain = b.dataset.g;
    refreshFromFirstPage();
  });

  $("#f-disc-chips").addEventListener("click", (ev) => {
    const b = ev.target.closest(".chip");
    if (!b) return;
    b.classList.toggle("on");
    state.filters.disciplines = $$("#f-disc-chips .chip.on").map((x) => x.dataset.d);
    refreshFromFirstPage();
  });

  const months = (await api("/api/months")).months;
  $("#f-month").innerHTML = `<option value="">all months</option>` +
    months.map((m) => `<option>${m}</option>`).join("");
  $("#f-month").addEventListener("change", (e) => { state.filters.month = e.target.value; refreshFromFirstPage(); });

  const chips = $("#f-cpt-chips");
  chips.innerHTML = codeChipsHtml();
  chips.addEventListener("click", (ev) => {
    const b = ev.target.closest(".chip");
    if (!b) return;
    b.classList.toggle("on");
    state.filters.cpts = $$(".chip.on", chips).map((x) => x.dataset.cpt);
    refreshFromFirstPage();
  });

  const bind = (id, key, ev = "change", transform = (v) => v) =>
    $(id).addEventListener(ev, debounce((e) => {
      state.filters[key] = transform(e.target.type === "checkbox" ? e.target.checked : e.target.value);
      refreshFromFirstPage();
    }, ev === "input" ? 300 : 0));
  bind("#f-modifier", "modifier");
  bind("#f-mod-has", "mod_has", "input");
  bind("#f-mod-not", "mod_not", "input");
  bind("#f-class", "billing_class");
  bind("#f-pos", "pos");
  bind("#f-state", "state", "input", (v) => v.trim());
  bind("#f-city", "city", "input", (v) => v.trim());
  bind("#f-q", "q", "input", (v) => v.trim());
  bind("#f-rate-min", "rate_min", "input");
  bind("#f-rate-max", "rate_max", "input");
  bind("#f-dollar", "dollar");
  bind("#f-hidetinnpi", "hide_tin_npi");
  bind("#f-outliers", "hide_outliers");

  $("#f-clear").addEventListener("click", () => {
    state.filters = { payers: [], cpts: [], disciplines: [], modifier: "", mod_has: "", mod_not: "",
      billing_class: "", pos: "", state: "", city: "", month: "", q: "",
      dollar: true, hide_tin_npi: false, hide_outliers: false, rate_min: "", rate_max: "" };
    $$(".chip.on").forEach((c) => c.classList.remove("on"));
    ["#f-modifier", "#f-mod-has", "#f-mod-not", "#f-class", "#f-pos", "#f-state", "#f-city",
     "#f-q", "#f-rate-min", "#f-rate-max", "#f-month"].forEach((id) => ($(id).value = ""));
    $("#f-dollar").checked = true;
    $("#f-hidetinnpi").checked = false;
    $("#f-outliers").checked = false;
    refreshFromFirstPage();
  });
  $("#btn-export").addEventListener("click", () => {
    location.href = `/api/export.csv?${filterQuery({ sort: state.sort.col, dir: state.sort.dir, view: "explorer" })}`;
  });
  $("#btn-export-zip").addEventListener("click", () => {
    location.href = `/api/export.zip?${filterQuery({ sort: state.sort.col, dir: state.sort.dir, view: "explorer" })}`;
  });
  $("#btn-outreach").addEventListener("click", () => {
    location.href = `/api/export/outreach.csv?${filterQuery()}`;
  });

  $$("#rates-table thead th[data-sort]").forEach((th) =>
    th.addEventListener("click", () => {
      const col = th.dataset.sort;
      if (state.sort.col === col) state.sort.dir = state.sort.dir === "desc" ? "asc" : "desc";
      else state.sort = { col, dir: ["negotiated_rate", "source_count", "rate_variants"].includes(col) ? "desc" : "asc" };
      refreshFromFirstPage();
    }));

  $("#pg-prev").addEventListener("click", () => { state.page = Math.max(1, state.page - 1); loadRates(); });
  $("#pg-next").addEventListener("click", () => { state.page += 1; loadRates(); });
  $("#pg-size").addEventListener("change", (e) => { state.pageSize = +e.target.value; refreshFromFirstPage(); });
}

function updateSortArrows() {
  $$("#rates-table thead th[data-sort]").forEach((th) => {
    const base = th.textContent.replace(/[▲▼]\s*$/, "").trim();
    th.innerHTML = esc(base) + (th.dataset.sort === state.sort.col
      ? ` <span class="arrow">${state.sort.dir === "desc" ? "▼" : "▲"}</span>` : "");
  });
}

/* =======================================================================
   ENTITY DETAIL DRAWER
   ======================================================================= */

async function openEntity(grain, unitId) {
  const drawer = $("#drawer"), overlay = $("#overlay");
  drawer.innerHTML = `<div class="loading">Loading ${esc(unitId)}</div>`;
  drawer.classList.add("open"); overlay.classList.add("open");
  overlay.onclick = closeDrawer;
  let d;
  try { d = await api(`/api/entity/${grain}/${encodeURIComponent(unitId)}`); }
  catch (e) {
    drawer.innerHTML = `<button class="close">×</button><div class="empty">${esc(e.message)}</div>`;
    $(".close", drawer).onclick = closeDrawer;
    return;
  }
  const payers = [...new Set(d.rates.map((r) => r.payer))];
  const tinsHtml = d.tins.length ? `
    <h3>Constituent TIN${d.tins.length === 1 ? "" : "s"}</h3>
    <table><thead><tr><th>TIN</th><th>Display name</th><th>Kind</th><th class="num">NPIs</th><th>States</th><th>Discipline</th></tr></thead>
    <tbody>${d.tins.map((t) => `
      <tr><td>${esc(t.tin_value)}</td><td>${esc(t.display_name || "")}</td>
      <td>${esc(t.entity_kind || "")}</td><td class="num">${fmtInt(t.npi_count)}</td>
      <td>${esc((t.states || []).join(", "))}</td><td>${esc(t.primary_discipline || "—")}</td></tr>`).join("")}
    </tbody></table>` : "";
  const npisHtml = d.npis.length ? `
    <h3>NPIs rolled under this ${grain === "npi" ? "record" : "entity"} (${d.npis.length})</h3>
    <div class="tablewrap" style="max-height:26vh">
    <table><thead><tr><th>NPI</th><th>Name</th><th>Type</th><th>City</th><th>State</th><th>Taxonomy</th></tr></thead>
    <tbody>${d.npis.map((n) => `
      <tr><td>${esc(n.npi)}</td><td>${esc(n.org_name || "pending…")}</td><td>${esc(n.entity_type || "")}</td>
      <td>${esc(n.city || "")}</td><td>${esc(n.state || "")}</td><td>${esc(n.taxonomy_desc || "")}</td></tr>`).join("")}
    </tbody></table></div>` : "";
  const varianceNote = d.variants
    ? `<div class="disclaimer">⚠ ${d.variants} rate tuple${d.variants === 1 ? "" : "s"} show multiple distinct rates within this entity (see Variants column) — check contract splits before quoting a single number.</div>`
    : "";
  drawer.innerHTML = `
    <button class="close" aria-label="close">×</button>
    <h2>${esc(d.display_name)}</h2>
    <div class="meta">${grain.toUpperCase()} · ${esc(unitId)}${payers.length ? " · " + payers.map(esc).join(" / ") : ""}</div>
    ${varianceNote}
    <h3>Median dollar rate by code${payers.length > 1 ? " and payer" : ""}</h3>
    ${payers.length > 1 ? `<div class="legend">${payers.slice(0, 2).map((p, i) =>
      `<span><span class="sw" style="background:var(--accent${i ? "-2" : ""})"></span>${esc(p)}</span>`).join("")}</div>` : ""}
    <div id="entity-chart"></div>
    ${tinsHtml}
    ${npisHtml}
    <h3>All rates</h3>
    <div class="tablewrap" style="max-height:36vh">
      <table>
        <thead><tr><th>Code</th><th>Payer</th><th>Mods</th><th>Class</th><th>Month</th><th>Type</th><th class="num">Rate</th><th class="num">Var.</th></tr></thead>
        <tbody>${d.rates.map((r) => `
          <tr>
            <td>${esc(r.billing_code)}<div class="sub">${esc(state.catalog[r.billing_code]?.description || "")}</div></td>
            <td>${esc(r.payer)}</td>
            <td>${modTags(r.modifier_set)}</td>
            <td>${esc(r.billing_class || "—")}</td>
            <td class="sub">${esc(r.file_month || "")}</td>
            <td>${esc(r.negotiated_type)}${r.is_dollar_rate ? "" : ' <span class="warn-text">(non-dollar)</span>'}</td>
            <td class="num"><span class="rate">$${fmtMoney(r.negotiated_rate)}</span></td>
            <td class="num">${(r.rate_variants || 1) > 1 ? `<span class="variant-flag">${r.rate_variants}</span>` : "1"}</td>
          </tr>`).join("")}
        </tbody>
      </table>
    </div>`;
  $(".close", drawer).onclick = closeDrawer;
  renderBarChart($("#entity-chart"), d.chart, payers.slice(0, 2));
}
function closeDrawer() {
  $("#drawer").classList.remove("open");
  $("#overlay").classList.remove("open");
}
document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeDrawer(); });

/* ---------- charts (dataviz-skill mark specs) ---------- */

function renderBarChart(el, chart, payers) {
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
  for (const t of niceTicks(maxV, 4)) {
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
   CODE COMPARISON
   ======================================================================= */

function initCptView() {
  const chips = $("#cpt-chips");
  chips.innerHTML = codeChipsHtml();
  chips.addEventListener("click", (ev) => {
    const b = ev.target.closest(".chip");
    if (b) selectCpt(b.dataset.cpt);
  });
  $("#cpt-base-only").addEventListener("change", () => state.cptSelected && selectCpt(state.cptSelected));
  $("#cpt-export").addEventListener("click", () => {
    if (!state.cptSelected) return;
    const p = new URLSearchParams({ cpt: state.cptSelected, view: `code_${state.cptSelected}`,
      grain: state.grain === "npi" ? "npi" : "tin", sort: "negotiated_rate", dir: "desc" });
    if ($("#cpt-base-only").checked) p.set("modifier", "base");
    location.href = `/api/export.zip?${p}`;
  });
}

async function selectCpt(code) {
  state.cptSelected = code;
  $$("#cpt-chips .chip").forEach((c) => c.classList.toggle("on", c.dataset.cpt === code));
  const body = $("#cpt-body"), stateEl = $("#cpt-state");
  body.innerHTML = "";
  stateEl.innerHTML = `<div class="loading">Loading ${esc(code)}</div>`;
  const p = new URLSearchParams({ grain: "tin" });
  if ($("#cpt-base-only").checked) p.set("modifier", "base");
  let d, trend;
  try {
    [d, trend] = await Promise.all([
      api(`/api/code/${code}?${p}`),
      api(`/api/trend?cpt=${code}&grain=tin${$("#cpt-base-only").checked ? "&modifier=base" : ""}`),
    ]);
  } catch (e) { stateEl.innerHTML = `<div class="empty">${esc(e.message)}</div>`; return; }
  const info = state.catalog[code] || {};
  $("#view-cpt h2").textContent =
    `Which entities get paid most for ${code}${info.description ? " — " + info.description : ""}` +
    (info.timed ? " (timed 15-min units)" : "");
  renderHistogram($("#cpt-hist"), d.histogram, code);
  renderTrend($("#cpt-trend"), trend.rows);
  if (!d.ranked.length) {
    stateEl.innerHTML = `<div class="empty"><h3>No rates for ${esc(code)} under these filters</h3></div>`;
    return;
  }
  stateEl.innerHTML = "";
  body.innerHTML = d.ranked.map((r, i) => `
    <tr class="clickable" data-uid="${esc(r.unit_id)}">
      <td class="num muted">${i + 1}</td>
      <td>${esc(r.display_name || "(name pending)")}<div class="sub">${esc(r.unit_id)}</div></td>
      <td>${esc(r.payer)}</td>
      <td>${modTags(r.modifier_set)}</td>
      <td>${esc(r.billing_class || "—")}</td>
      <td class="num">${fmtInt(r.npi_count)}</td>
      <td class="num"><span class="rate">$${fmtMoney(r.median_rate)}</span></td>
      <td class="num">$${fmtMoney(r.min_rate)}</td>
      <td class="num">$${fmtMoney(r.max_rate)}</td>
    </tr>`).join("");
  $$("tr.clickable", body).forEach((tr) => tr.addEventListener("click", () => openEntity("tin", tr.dataset.uid)));
}

function renderTrend(el, rows) {
  if (!rows || rows.length < 1) { el.innerHTML = ""; return; }
  const months = [...new Set(rows.map((r) => r.file_month))].sort();
  if (months.length < 2) { el.innerHTML = ""; return; }
  const payers = [...new Set(rows.map((r) => r.payer))];
  const byKey = Object.fromEntries(rows.map((r) => [`${r.payer}|${r.file_month}`, r.median_rate]));
  el.innerHTML = `<div class="trendwrap"><h3 style="font-size:12px;color:var(--muted);text-transform:uppercase">Median by month</h3>
  <table style="max-width:640px"><thead><tr><th>Payer</th>${months.map((m) => `<th class="num">${esc(m)}</th>`).join("")}</tr></thead>
  <tbody>${payers.map((p) => `<tr><td>${esc(p)}</td>${months.map((m) =>
    `<td class="num">${byKey[`${p}|${m}`] != null ? "$" + fmtMoney(byKey[`${p}|${m}`]) : "–"}</td>`).join("")}</tr>`).join("")}
  </tbody></table></div>`;
}

/* =======================================================================
   BENCHMARK
   ======================================================================= */

let benchmarkInit = false;
async function initBenchmark() {
  refreshMpfsStatus();
  if (benchmarkInit) return;
  benchmarkInit = true;
  const [subjects, months, payers, peersets] = await Promise.all([
    api("/api/benchmark/subjects"), api("/api/months"), api("/api/payers"), api("/api/peersets"),
  ]);
  $("#b-subjects").innerHTML =
    subjects.entities.map((e) => `<option value="${esc(e)}">`).join("") +
    subjects.tins.map((t) => `<option value="${esc(t.tin_value)}">${esc(t.display_name)}</option>`).join("");
  $("#b-month").innerHTML = months.months.map((m) => `<option>${m}</option>`).join("") ||
    `<option value="">no data ingested</option>`;
  $("#b-payer-chips").innerHTML = payers.payers.map((p) =>
    `<button class="chip" data-payer="${esc(p)}">${esc(p)}</button>`).join("");
  $("#b-payer-chips").addEventListener("click", (ev) => {
    const b = ev.target.closest(".chip");
    if (b) b.classList.toggle("on");
  });
  refreshPeersets(peersets.peer_sets);
  $("#ps-save").addEventListener("click", async () => {
    const name = $("#ps-name").value.trim();
    const tins = $("#ps-tins").value.split(/\n+/).map((t) => t.trim()).filter(Boolean);
    if (!name || !tins.length) { alert("peer set needs a name and at least one TIN"); return; }
    const r = await postJson("/api/peersets", { name, tins });
    refreshPeersets(r.peer_sets);
    $("#b-peerset").value = name;
  });
  $("#mpfs-file").addEventListener("change", async (e) => {
    const f = e.target.files[0];
    if (!f) return;
    const fd = new FormData();
    fd.append("file", f);
    try {
      await api("/api/mpfs/upload", { method: "POST", body: fd });
      refreshMpfsStatus();
    } catch (err) { alert(err.message); }
  });
  $("#b-run").addEventListener("click", runBenchmark);
  $("#b-report").addEventListener("click", openPitchReport);
}

function refreshPeersets(sets) {
  $("#b-peerset").innerHTML = `<option value="">auto — all entities in market</option>` +
    Object.keys(sets || {}).map((n) => `<option>${esc(n)}</option>`).join("");
}

async function refreshMpfsStatus() {
  try {
    const s = await api("/api/mpfs/status");
    $("#mpfs-status").innerHTML = s.loaded
      ? `MPFS loaded: <b>${esc(s.loaded)}</b> — %-of-Medicare columns active`
      : "MPFS: not loaded (optional — enables % of Medicare)";
  } catch { /* ignore */ }
}

function benchmarkPayload() {
  const subject = $("#b-subject").value.trim();
  const month = $("#b-month").value;
  if (!subject || !month) throw new Error("subject and as-of month are required");
  const market = {
    month,
    payers: $$("#b-payer-chips .chip.on").map((c) => c.dataset.payer),
    billing_class: $("#b-class").value,
    target_percentile: +$("#b-target").value,
  };
  if ($("#b-disc").value) market.discipline = $("#b-disc").value;
  if ($("#b-state").value.trim()) market.state = $("#b-state").value.trim();
  if ($("#b-city").value.trim()) market.city = $("#b-city").value.trim();
  if ($("#b-pos").value) market.pos = $("#b-pos").value;
  if ($("#b-peerset").value) market.peer_set = $("#b-peerset").value;
  const volumes = {};
  for (const line of $("#b-volumes").value.split(/\n+/)) {
    const m = line.split(/[,\t]/).map((s) => s.trim());
    if (m.length >= 2 && m[0] && !isNaN(+m[1])) volumes[m[0]] = +m[1];
  }
  return { subject, market, volumes };
}

async function runBenchmark() {
  const out = $("#b-out");
  let payload;
  try { payload = benchmarkPayload(); }
  catch (e) { out.innerHTML = `<div class="empty">${esc(e.message)}</div>`; return; }
  out.innerHTML = `<div class="loading">Computing benchmark</div>`;
  let data;
  try {
    data = Object.keys(payload.volumes).length
      ? await postJson("/api/benchmark/opportunity", payload)
      : { benchmark: await postJson("/api/benchmark/market", payload) };
  } catch (e) { out.innerHTML = `<div class="empty"><h3>Benchmark failed</h3>${esc(e.message)}</div>`; return; }
  state.lastBenchmarkPayload = payload;
  $("#b-report").disabled = false;
  renderBenchmark(out, data.benchmark, data.opportunity);
}

function pstrip(pct) {
  if (pct == null) return "";
  return `<span class="pstrip"><span class="you" style="left:${Math.min(Math.max(pct, 0), 100)}%"></span></span>
          <span class="pctlbl">P${Math.round(pct)}</span>`;
}

function renderBenchmark(out, bench, opp) {
  const mp = bench.mpfs_loaded;
  const t = bench.target_percentile;
  const rows = bench.rows.map((r) => `
    <tr><td>${esc(r.billing_code)}${r.is_timed ? '<span class="timed-tag">15-min</span>' : ""}
      <div class="sub">${esc(r.description || "")}</div></td>
      <td class="num"><span class="rate">${r.subject_rate != null ? "$" + fmtMoney(r.subject_rate) : "–"}</span></td>
      <td class="num">$${fmtMoney(r.p25)}</td>
      <td class="num">$${fmtMoney(r.p50)}</td>
      <td class="num">$${fmtMoney(r.p75)}</td>
      <td class="num">${r.target_rate != null ? "$" + fmtMoney(r.target_rate) : "–"}</td>
      <td class="num ${r.gap_to_target > 0 ? "warn-text" : ""}">${r.gap_to_target != null ? "$" + fmtMoney(r.gap_to_target) : "–"}</td>
      ${mp ? `<td class="num">${r.subject_pct_medicare != null ? r.subject_pct_medicare + "%" : "–"}</td>
              <td class="num">${r.median_pct_medicare != null ? r.median_pct_medicare + "%" : "–"}</td>` : ""}
      <td class="num muted">${fmtInt(r.n_peers)}</td>
      <td>${pstrip(r.subject_percentile)}</td></tr>`).join("");
  const oppHtml = opp ? `
    <div class="opp-band">Annual gross opportunity:
      conservative (p${opp.conservative_percentile}) <b>$${fmtMoney(opp.total_at_conservative)}</b>
      · at target (p${opp.target_percentile}) <b>$${fmtMoney(opp.total_at_target)}</b></div>
    <div class="tablewrap"><table>
      <thead><tr><th>Code</th><th class="num">Annual units</th><th class="num">Your rate</th>
      <th class="num">Target</th><th class="num">Opportunity (conservative)</th><th class="num">Opportunity (target)</th></tr></thead>
      <tbody>${opp.rows.map((o) => `
        <tr><td>${esc(o.billing_code)}</td><td class="num">${fmtInt(o.annual_units)}</td>
        <td class="num">$${fmtMoney(o.subject_rate)}</td><td class="num">${o.target_rate != null ? "$" + fmtMoney(o.target_rate) : "–"}</td>
        <td class="num">$${fmtMoney(o.opportunity_at_conservative)}</td>
        <td class="num"><b>$${fmtMoney(o.opportunity_at_target)}</b></td></tr>`).join("")}
      </tbody></table></div>
    <div class="bench-note">${esc(opp.assumptions)}</div>` : "";
  out.innerHTML = `
    <h2 style="font-size:15px;margin:0 0 4px">${esc(bench.subject)} vs market — as of ${esc(bench.market.month)}</h2>
    <div class="muted" style="margin-bottom:10px">${esc(bench.peer_set)} · target p${t}</div>
    <div class="tablewrap"><table>
      <thead><tr><th>Code</th><th class="num">You</th><th class="num">P25</th><th class="num">Median</th>
      <th class="num">P75</th><th class="num">Target</th><th class="num">Gap</th>
      ${mp ? `<th class="num">% Mcare (you)</th><th class="num">% Mcare (mkt)</th>` : ""}
      <th class="num">Peers</th><th>Position</th></tr></thead>
      <tbody>${rows}</tbody></table></div>
    ${oppHtml}
    <div class="bench-note">${esc(bench.basis_note)} Ghost rates: a published rate does not mean a
    peer bills that code; benchmarks stay within the discipline-scoped code set. A published rate
    is not proof a peer collects it — this is directional market positioning.</div>`;
}

async function openPitchReport() {
  if (!state.lastBenchmarkPayload) return;
  const r = await fetch("/api/report/pitch", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(state.lastBenchmarkPayload),
  });
  if (!r.ok) { alert("report failed: " + (await r.text())); return; }
  const blob = await r.blob();
  window.open(URL.createObjectURL(blob), "_blank");
}

/* =======================================================================
   FILES + VALIDATION
   ======================================================================= */

function qaLine(qa) {
  if (!qa || typeof qa !== "object") return "";
  const bits = [];
  if (qa.outlier_rates) bits.push(`<span class="warn">${fmtInt(qa.outlier_rates)} outliers</span>`);
  if (qa.zero_rates) bits.push(`<span class="warn">${fmtInt(qa.zero_rates)} $0/$0.01</span>`);
  if (qa.non_dollar_rows) bits.push(`${fmtInt(qa.non_dollar_rows)} non-dollar`);
  if (qa.billing_type_other) bits.push(`${fmtInt(qa.billing_type_other)} non-CPT/HCPCS`);
  if (qa.multi_code_fields) bits.push(`<span class="bad">${fmtInt(qa.multi_code_fields)} multi-code fields</span>`);
  if (qa.tin_is_really_npi_rows) bits.push(`${fmtInt(qa.tin_is_really_npi_rows)} TIN=NPI`);
  if (qa.duplicate_explosion_ratio > 1.5) bits.push(`<span class="warn">${qa.duplicate_explosion_ratio}x dup</span>`);
  return bits.length
    ? `<span class="qa-line" title="${esc(qa.outlier_rule || "")}">${bits.join(" · ")}</span>`
    : `<span class="qa-line muted">clean</span>`;
}

async function loadFiles() {
  const body = $("#files-body"), stateEl = $("#files-state");
  let d;
  try { d = await api("/api/files"); }
  catch (e) { stateEl.innerHTML = `<div class="empty">${esc(e.message)}</div>`; return; }
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
    const inflight = (f.status === "processing" || f.status === "queued") && f.chunks_total > 0;
    const progressBar = inflight
      ? `<div class="progress" title="working through the file in chunks">
           <div class="progress-fill" style="width:${Math.round(f.progress || 0)}%"></div>
           <span class="progress-label">chunk ${fmtInt(f.chunks_done)}/${fmtInt(f.chunks_total)} · ${Math.round(f.progress || 0)}%</span>
         </div>`
      : "";
    return `<tr>
      <td>${esc(f.filename)}</td>
      <td>${esc(f.payer || "?")}</td>
      <td>${esc(f.file_type || "?")}${f.schema_version && !String(f.schema_version).startsWith("2.") ? ` <span class="warn-text">v${esc(f.schema_version)}</span>` : ""}</td>
      <td><span class="badge ${esc(f.status)}">${esc(f.status)}</span>${statusExtra}${progressBar}</td>
      <td class="num">${fmtInt(f.rows_emitted)}</td>
      <td>${qaLine(f.qa)}</td>
      <td>${warn}${f.error ? `<div class="err-text">${esc(f.error)}</div>` : ""}</td>
      <td class="sub">${f.finished_at ? esc(f.finished_at.slice(0, 19)) : ""}</td>
    </tr>`;
  }).join("");
  $$("[data-confirm]", body).forEach((b) =>
    b.addEventListener("click", async () => {
      b.disabled = true;
      await fetch(`/api/files/${encodeURIComponent(b.dataset.confirm)}/confirm`, { method: "POST" });
      loadFiles();
    }));
  loadStats();
}

const KIND_LABEL = {
  toc: "index / table of contents",
  page: "web page",
  in_network: "rate file",
  provider_reference: "provider list",
  allowed_amounts: "allowed-amounts (no rates)",
  unknown: "?",
};

async function loadUrlQueue() {
  let d;
  try { d = await api("/api/urls"); } catch { return; }
  const wrap = $("#url-wrap"), body = $("#url-body");
  if (!d.urls.length) { wrap.style.display = "none"; return; }
  wrap.style.display = "block";
  body.innerHTML = d.urls.map((u) => {
    const short = u.url.split("?")[0].replace(/^https?:\/\//, "");
    const shown = short.length > 78 ? short.slice(0, 38) + "…" + short.slice(-37) : short;
    let statusCell = `<span class="badge ${esc(u.status === "done" ? "done" : u.status === "failed" ? "failed" : "processing")}">${esc(u.status)}</span>`;
    if (u.status === "downloading" && u.bytes_total > 0) {
      statusCell += `<div class="progress"><div class="progress-fill" style="width:${Math.round(u.progress)}%"></div>
        <span class="progress-label">${(u.bytes_done / 1e6).toFixed(0)} / ${(u.bytes_total / 1e6).toFixed(0)} MB</span></div>`;
    } else if (u.status === "ingesting") {
      statusCell += ` <span class="muted">(see file row below for chunk progress)</span>`;
    }
    if (u.status === "failed") {
      statusCell += ` <button class="btn" style="padding:1px 8px;font-size:11.5px" data-url-retry="${u.id}">retry</button>`;
    } else if (u.status === "queued") {
      statusCell += ` <button class="btn" style="padding:1px 8px;font-size:11.5px" data-url-cancel="${u.id}">skip</button>`;
    }
    let notes = "";
    if (u.kind === "toc" && u.status === "done") notes = `found ${fmtInt(u.child_count)} files inside — queued below`;
    else if (u.kind === "page" && u.status === "done") notes = `found ${fmtInt(u.child_count)} file links on the page`;
    else if (u.error) notes = `<span class="err-text">${esc(u.error)}</span>`;
    return `<tr>
      <td title="${esc(u.url)}">${esc(shown)}</td>
      <td>${esc(KIND_LABEL[u.kind] || u.kind || "…")}</td>
      <td>${statusCell}</td>
      <td class="num">${u.rows_emitted ? fmtInt(u.rows_emitted) : ""}</td>
      <td style="max-width:420px">${notes}</td>
    </tr>`;
  }).join("");
  $$("[data-url-retry]", body).forEach((b) =>
    b.addEventListener("click", async () => {
      b.disabled = true;
      await fetch(`/api/urls/${b.dataset.urlRetry}/retry`, { method: "POST" });
      loadUrlQueue();
    }));
  $$("[data-url-cancel]", body).forEach((b) =>
    b.addEventListener("click", async () => {
      b.disabled = true;
      await fetch(`/api/urls/${b.dataset.urlCancel}/cancel`, { method: "POST" });
      loadUrlQueue();
    }));
}

function initFilesView() {
  $("#url-add").addEventListener("click", async () => {
    const raw = $("#url-input").value.trim();
    if (!raw) return;
    const msg = $("#url-msg");
    msg.textContent = "adding…";
    try {
      const r = await postJson("/api/urls", { urls: raw });
      msg.textContent = `${r.added} queued` +
        (r.skipped ? `, ${r.skipped} already known` : "") +
        (r.invalid ? `, ${r.invalid} not valid links` : "");
      if (r.added) $("#url-input").value = "";
    } catch (e) {
      msg.textContent = "could not add: " + e.message;
    }
    loadUrlQueue();
  });

  $("#btn-scan").addEventListener("click", async () => {
    await fetch("/api/files/scan", { method: "POST" });
    setTimeout(loadFiles, 800);
  });
  const dz = $("#dropzone"), input = $("#file-input");
  dz.addEventListener("click", () => input.click());
  input.addEventListener("change", () => uploadFiles([...input.files]));
  ["dragenter", "dragover"].forEach((t) =>
    dz.addEventListener(t, (e) => { e.preventDefault(); dz.classList.add("drag"); }));
  ["dragleave", "drop"].forEach((t) =>
    dz.addEventListener(t, (e) => { e.preventDefault(); dz.classList.remove("drag"); }));
  dz.addEventListener("drop", (e) => uploadFiles([...e.dataTransfer.files]));

  $("#v-run").addEventListener("click", async () => {
    const id = $("#v-id").value.trim(), code = $("#v-code").value.trim();
    const expected = $("#v-expected").value;
    if (!id || !code) { alert("TIN/NPI and code required"); return; }
    const r = await postJson("/api/validate", {
      id, code, ...(expected ? { expected_rate: +expected } : {}),
    });
    const verdict = $("#v-verdict");
    if (!r.rows.length) verdict.innerHTML = `<span class="v-bad">no extracted rows for that id + code</span>`;
    else if (r.match === true) verdict.innerHTML = `<span class="v-ok">✓ expected rate found in extraction</span>`;
    else if (r.match === false) verdict.innerHTML = `<span class="v-bad">✗ expected rate NOT found (see Δ)</span>`;
    else verdict.textContent = `${r.rows.length} extracted rows`;
    $("#v-wrap").style.display = r.rows.length ? "block" : "none";
    $("#v-body").innerHTML = r.rows.map((row) => `
      <tr><td>${esc(row.payer)}</td><td>${esc(row.tin_value || "")}</td><td>${esc(row.npi)}</td>
      <td>${esc(row.billing_code)}</td><td>${modTags(row.modifiers)}</td>
      <td class="num">$${fmtMoney(row.negotiated_rate)}</td>
      <td class="num">${row.delta_vs_expected != null ? fmtMoney(row.delta_vs_expected) : "–"}</td>
      <td class="sub">${esc(row.file_month || "")}</td><td class="sub">${esc(row.source_file)}</td></tr>`).join("");
  });
}

async function uploadFiles(files) {
  for (const f of files) {
    if (f.size > 1 << 30) { alert(`${f.name} is over 1 GB — drop it into data/inbox/ instead.`); continue; }
    const fd = new FormData();
    fd.append("file", f);
    $("#dropzone").innerHTML = `<div class="loading">Uploading ${esc(f.name)}</div>`;
    try {
      const r = await fetch("/api/upload", { method: "POST", body: fd });
      if (!r.ok) alert(`upload failed: ${(await r.json()).detail || r.status}`);
    } catch (e) { alert(`upload failed: ${e.message}`); }
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
    }));
}

/* =======================================================================
   BOOT
   ======================================================================= */

(async function boot() {
  state.catalog = await api("/api/catalog").catch(() => ({}));
  const stats = await api("/api/stats").catch(() => null);
  if (stats?.default_grain && stats.default_grain !== "tin") {
    state.grain = stats.default_grain;
    $$("#f-grain button").forEach((b) => b.classList.toggle("on", b.dataset.g === state.grain));
  }
  loadStats();
  await initFilters();
  initCptView();
  initFilesView();
  refresh();
  setInterval(loadStats, 15000);
})();
