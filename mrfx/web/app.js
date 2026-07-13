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
  lastRatecardPayload: null,
  lastLeadsPayload: null,
  lastChangesPayload: null,
  lastNamed: null,   // last NPPES "named" count seen, to refresh views as names land
};

const fmtMoney = (v) =>
  v == null ? "–" : Number(v).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const fmtInt = (v) => (v == null ? "–" : Number(v).toLocaleString("en-US"));
const esc = (s) =>
  String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
// client twin of the server's SSN-pattern TIN mask — for the few places that
// render a raw unit_id (drawer meta, code ranking) instead of the
// server-masked tin_value column
const SSN_PREFIXES = new Set(["00","07","08","09","17","18","19","28","29","49","69","70","78","79","89","96","97"]);
const maskTin = (t) => {
  const s = String(t ?? "");
  return /^[0-9]{9}$/.test(s) && SSN_PREFIXES.has(s.slice(0, 2)) ? "MASKED-SSN" : s;
};

async function api(path, opts) {
  const r = await fetch(path, opts);
  if (!r.ok) {
    let msg = `HTTP ${r.status}`;
    try { const d = await r.json(); msg = d.detail || d.error || msg; } catch { /* keep */ }
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
  if (view === "ratecard") initRatecard();
  if (view === "leads") initLeads();
  if (view === "changes") initChanges();
  if (view === "sources") loadSources();
}

/* ---------- header stats ---------- */
async function loadStats() {
  try {
    const s = await api("/api/stats");
    let html =
      `<b>${fmtInt(s.rates)}</b> rate rows · <b>${fmtInt(s.tins)}</b> TINs · ` +
      `<b>${s.payers}</b> payer${s.payers === 1 ? "" : "s"} · <b>${fmtInt(s.files_done)}</b> files`;
    const e = s.enrichment;
    if (e && e.total > 0) {
      // names + states come from NPPES enrichment, which runs in the
      // background; show how far along it is so a sparse state filter reads
      // as "still filling in" rather than "broken".
      if (s.enrichment_mode === "off") {
        html += ` · <span class="muted">names off</span>`;
      } else if (e.remaining > 0) {
        html += ` · <b>${fmtInt(e.named)}/${fmtInt(e.total)}</b> names ` +
                `<span class="muted">(identifying ${fmtInt(e.remaining)} more…)</span>`;
      } else {
        html += ` · <b>${fmtInt(e.named)}/${fmtInt(e.total)}</b> names`;
      }
      // Names arrive in the background (NPPES enrichment). When the resolved
      // count grows between polls, the display-name column, state-filter lists
      // and geographic benchmarks now have data they lacked a moment ago, so
      // refresh whatever the user is looking at instead of making them reload.
      if (state.lastNamed != null && e.named > state.lastNamed) onNamesUpdated();
      state.lastNamed = e.named;
    }
    $("#dataset-stats").innerHTML = html;
  } catch { $("#dataset-stats").textContent = "API unreachable"; }
}

// Called when NPPES enrichment resolves more names since the last poll. Cheap
// and idempotent: refresh the state pickers everywhere and re-pull the data for
// whichever view is open so freshly-identified names/geo appear live.
function onNamesUpdated() {
  loadStateOptions();
  const v = state.view;
  if (v === "explorer") { loadRates(); loadSummary(); }
  else if (v === "cpt" && state.cptSelected) selectCpt(state.cptSelected);
}

async function loadStateOptions() {
  try {
    const { states } = await api("/api/states");
    const dl = $("#state-options");
    if (dl) dl.innerHTML = (states || []).map((s) => `<option value="${esc(s)}">`).join("");
  } catch { /* filter still works as free text */ }
}

/* =======================================================================
   EXPLORER
   ======================================================================= */

function filterQuery(extra = {}) {
  const f = state.filters;
  const p = new URLSearchParams();
  p.set("grain", state.grain);
  // payer names are free text and frequently contain commas, so each goes as
  // its own repeated ?payer= param — never comma-joined (that split one name
  // into two and the filter matched nothing).
  f.payers.forEach((x) => p.append("payer", x));
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
// Sorting and page-size changes re-order/re-window the SAME filtered set, so
// the summary stats (medians, counts, benchmarks) are unchanged — reload only
// the table, skipping a heavy summary recompute that dominates on a big store.
const reloadTableFirstPage = () => { state.page = 1; loadRates(); };

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
    months.map((m) => `<option>${esc(m)}</option>`).join("");
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
    // Explorer chips ONLY — a document-wide ".chip.on" sweep also wiped the
    // BENCHMARK tab's payer selection (read from the DOM at run time), so a
    // Clear here silently turned a payer-scoped benchmark into all-payers
    $$("#f-payer-chips .chip.on, #f-cpt-chips .chip.on, #f-disc-chips .chip.on")
      .forEach((c) => c.classList.remove("on"));
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
      reloadTableFirstPage();
    }));

  $("#pg-prev").addEventListener("click", () => { state.page = Math.max(1, state.page - 1); loadRates(); });
  $("#pg-next").addEventListener("click", () => { state.page += 1; loadRates(); });
  $("#pg-size").addEventListener("change", (e) => { state.pageSize = +e.target.value; reloadTableFirstPage(); });
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
      <tr><td>${esc(t.tin_value_masked || t.tin_value)}</td><td>${esc(t.display_name || "")}</td>
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
  const canEditSite = d.website_tins && d.website_tins.length;
  const siteHtml = (d.website || d.website_lookup || canEditSite) ? `
    <div class="meta" style="margin-top:3px">Website:
      ${d.website
        ? `<a href="${esc(d.website)}" target="_blank" rel="noopener">${esc(d.website)}</a> <span class="ok-tag" title="you saved this as a verified URL">✓ verified</span>`
        : (d.website_lookup
            ? `<a href="${esc(d.website_lookup)}" target="_blank" rel="noopener">search for it ↗</a> <span class="muted">— no verified URL saved (NPPES has none)</span>`
            : "none")}
      ${canEditSite ? `<span style="display:inline-flex;gap:4px;margin-left:8px;vertical-align:middle">
        <input id="d-website" type="url" placeholder="paste a verified URL" value="${esc(d.website || "")}" style="width:210px;font-size:12px">
        <button class="btn" id="d-website-save" style="padding:1px 8px;font-size:11.5px">Save</button></span>` : ""}
    </div>` : "";
  drawer.innerHTML = `
    <button class="close" aria-label="close">×</button>
    <h2>${esc(d.display_name)}</h2>
    <div class="meta">${grain.toUpperCase()} · ${esc(maskTin(unitId))}${payers.length ? " · " + payers.map(esc).join(" / ") : ""}</div>
    ${siteHtml}
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
  const saveSite = $("#d-website-save", drawer);
  if (saveSite) saveSite.onclick = async () => {
    const url = $("#d-website", drawer).value.trim();
    try {
      const r = await fetch("/api/org-website", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ tins: d.website_tins, url }),
      });
      if (r.ok) openEntity(grain, unitId);  // reload so the verified link shows
      else alert((await r.json().catch(() => ({}))).detail || "could not save the website");
    } catch (e) { alert("could not save the website: " + e.message); }
  };
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
  // comparing rates across states is misleading (a MO rate vs a NY rate are
  // different markets) — let the user scope the comparison to one state
  const cptState = $("#cpt-state-filter");
  let t;
  cptState.addEventListener("input", () => {
    clearTimeout(t);
    t = setTimeout(() => state.cptSelected && selectCpt(state.cptSelected), 300);
  });
  api("/api/states").then((s) => {
    $("#cpt-state-opts").innerHTML = (s.states || []).map((x) => `<option value="${esc(x)}">`).join("");
  }).catch(() => {});
  $("#cpt-export").addEventListener("click", () => {
    if (!state.cptSelected) return;
    const p = new URLSearchParams({ cpt: state.cptSelected, view: `code_${state.cptSelected}`,
      grain: state.grain === "npi" ? "npi" : "tin", sort: "negotiated_rate", dir: "desc" });
    if ($("#cpt-base-only").checked) p.set("modifier", "base");
    if (cptState.value.trim()) p.set("state", cptState.value.trim());
    location.href = `/api/export.zip?${p}`;
  });
}

async function selectCpt(code) {
  state.cptSelected = code;
  $$("#cpt-chips .chip").forEach((c) => c.classList.toggle("on", c.dataset.cpt === code));
  const body = $("#cpt-body"), stateEl = $("#cpt-state");
  body.innerHTML = "";
  stateEl.innerHTML = `<div class="loading">Loading ${esc(code)}</div>`;
  const cptState = $("#cpt-state-filter").value.trim();
  const p = new URLSearchParams({ grain: "tin" });
  if ($("#cpt-base-only").checked) p.set("modifier", "base");
  if (cptState) p.set("state", cptState);
  const trendP = new URLSearchParams({ cpt: code, grain: "tin" });
  if ($("#cpt-base-only").checked) trendP.set("modifier", "base");
  if (cptState) trendP.set("state", cptState);
  let d, trend;
  try {
    [d, trend] = await Promise.all([
      api(`/api/code/${code}?${p}`),
      api(`/api/trend?${trendP}`),
    ]);
  } catch (e) { stateEl.innerHTML = `<div class="empty">${esc(e.message)}</div>`; return; }
  const info = state.catalog[code] || {};
  $("#view-cpt h2").textContent =
    `Which entities get paid most for ${code}${info.description ? " — " + info.description : ""}` +
    (cptState ? ` · ${cptState}` : "") +
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
      <td>${esc(r.display_name || "(name pending)")}<div class="sub">${esc(maskTin(r.unit_id))}</div></td>
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

// Repopulate a tab's subject datalist (and, when given, its month <select>)
// from the live API. Called on every re-entry to a benchmark-family tab so
// orgs newly identified by NPPES enrichment (fresh auto-grouped subjects) and
// newly-ingested months appear without a page reload. Never clears the month
// the user already picked; transient fetch errors leave the existing lists.
// Subject picker is a server-side TYPEAHEAD: the API returns at most ~50
// matches, so the whole (possibly huge) directory is never serialized. The
// datalist starts with a "largest practices" set and refills as the user types.
function subjectOptionsHtml(data) {
  return (data.entities || []).map((e) => `<option value="${esc(e)}"></option>`).join("") +
    (data.tins || []).map((t) => `<option value="${esc(t.tin_value)}">${esc(t.display_name)}</option>`).join("");
}
function wireSubjectSearch(inputSel, datalistSel) {
  const input = $(inputSel);
  if (!input || input.dataset.searchWired) return;   // wire once
  input.dataset.searchWired = "1";
  input.addEventListener("input", debounce(async () => {
    try {
      const data = await api(`/api/benchmark/subjects?q=${encodeURIComponent(input.value.trim())}`);
      const dl = $(datalistSel); if (dl) dl.innerHTML = subjectOptionsHtml(data);
    } catch { /* keep the current options on a transient failure */ }
  }, 200));
}

async function refreshSubjectPickers(subjSel, monthSels = []) {
  let subjects, months;
  try {
    [subjects, months] = await Promise.all([
      api("/api/benchmark/subjects"), api("/api/months"),
    ]);
  } catch { return; }
  if (subjSel) $(subjSel).innerHTML = subjectOptionsHtml(subjects);
  const opts = months.months.map((m) => `<option>${esc(m)}</option>`).join("");
  for (const ms of monthSels) {
    const el = $(ms.sel); if (!el) continue;
    const cur = el.value;
    el.innerHTML = (ms.prefix || "") + opts ||
      `<option value="">no data ingested</option>`;
    if (cur && months.months.includes(cur)) el.value = cur;
  }
}

let benchmarkInit = false;
async function initBenchmark() {
  refreshMpfsStatus();
  if (benchmarkInit) { refreshSubjectPickers("#b-subjects", [{ sel: "#b-month" }]); return; }
  benchmarkInit = true;
  // Independent loads: a single failing/slow call must not blank the required
  // As-of month (or the whole tab).
  const [subjects, months, payers, peersets] = await Promise.all([
    api("/api/benchmark/subjects").catch(() => null),
    api("/api/months").catch(() => null),
    api("/api/payers").catch(() => null),
    api("/api/peersets").catch(() => null),
  ]);
  if (subjects) $("#b-subjects").innerHTML = subjectOptionsHtml(subjects);
  wireSubjectSearch("#b-subject", "#b-subjects");
  fillMonthSelect("#b-month", months);
  if (payers) $("#b-payer-chips").innerHTML = payers.payers.map((p) =>
    `<button class="chip" data-payer="${esc(p)}">${esc(p)}</button>`).join("");
  $("#b-payer-chips").addEventListener("click", (ev) => {
    const b = ev.target.closest(".chip");
    if (b) b.classList.toggle("on");
  });
  refreshPeersets((peersets || {}).peer_sets);
  $("#ps-save").addEventListener("click", async () => {
    const name = $("#ps-name").value.trim();
    const tins = $("#ps-tins").value.split(/\n+/).map((t) => t.trim()).filter(Boolean);
    if (!name || !tins.length) { alert("peer set needs a name and at least one TIN"); return; }
    try {
      const r = await postJson("/api/peersets", { name, tins });
      refreshPeersets(r.peer_sets);
      $("#b-peerset").value = name;
    } catch (e) { alert(`couldn't save the peer set: ${e.message}`); }
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
  $("#b-report").addEventListener("click", async () => {
    try { await openPitchReport(); }
    catch (e) { alert(`could not build the report: ${e.message}`); }
  });
  $("#b-negotiation").addEventListener("click", async () => {
    try { await openNegotiationReport(); }
    catch (e) { alert(`could not build the report: ${e.message}`); }
  });
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
    // require a non-empty numeric second field: "97110," -> +"" === 0, which
    // would silently record 0 units instead of skipping the malformed line
    if (m.length >= 2 && m[0] && m[1] !== "" && !isNaN(+m[1])) volumes[m[0]] = +m[1];
  }
  return { subject, market, volumes };
}

async function runBenchmark() {
  const out = $("#b-out");
  // a failed/aborted run must not leave "Open pitch report" armed with the
  // PREVIOUS run's payload — a report that doesn't match what's on screen
  state.lastBenchmarkPayload = null;
  $("#b-report").disabled = true;
  $("#b-negotiation").disabled = true;
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
  $("#b-negotiation").disabled = false;
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
  // Explain flat percentiles instead of letting them look like a bug: within a
  // single payer the negotiated rate for a code is often ONE fee-schedule
  // amount across every provider, so P25=Median=P75. A tiny peer count does the
  // same. Detect it and add a plain-language note.
  const flat = bench.rows.filter((r) => r.p25 != null && r.p25 === r.p50 && r.p50 === r.p75).length;
  const thin = bench.rows.filter((r) => (r.n_peers || 0) < 5).length;
  const n = bench.rows.length || 1;
  const spreadNote =
    flat / n >= 0.5
      ? ` <b>Why are the percentiles the same?</b> For most codes here every peer reports the
          identical negotiated rate — a payer usually publishes one fee-schedule amount per code
          across all providers, so P25/Median/P75 collapse to that number. That is the data, not an
          error. You'll see real spread once you benchmark across multiple payers${
            thin / n >= 0.5 ? ", or once more peer files are ingested (many codes have very few peers so far)" : ""
          }.`
      : "";
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
    <div class="bench-note">${esc(bench.basis_note)}${spreadNote} Ghost rates: a published rate does not mean a
    peer bills that code — scoping the market to a discipline or code list mitigates this
    (the market definition above shows which filters were actually applied). A published rate
    is not proof a peer collects it — this is directional market positioning.</div>`;
}

// Client reports must be scoped to one state (reimbursement varies by state).
// If no state is set, warn prominently and require an explicit opt-in before
// running a pooled national comparison — so it can never slip into a
// deliverable by accident. Returns a payload to send, or null if cancelled.
function reportPayloadOrConfirmNational() {
  if (!state.lastBenchmarkPayload) return null;
  const p = state.lastBenchmarkPayload;
  const m = p.market || {};
  if (m.state && String(m.state).trim()) return p;
  const ok = confirm(
    "No state is set, so this report pools providers across ALL loaded states " +
    "into one distribution. Negotiated reimbursement varies by state — an " +
    "in-state comparison is almost always what you want for a client report.\n\n" +
    "Run a national comparison anyway? (The report will carry a 'national' warning banner.)");
  if (!ok) return null;
  return { ...p, market: { ...m, allow_national: true } };
}

async function openReportWith(url, payload) {
  if (!payload) return;  // e.g. national-confirm cancelled
  // Open the tab SYNCHRONOUSLY, still inside the click's user-activation window
  // (any confirm() before this call is synchronous, so activation is preserved).
  // If we waited until after the await, a slow server render would push
  // window.open past the activation window and the browser would silently
  // block the popup — the report would just never appear.
  const w = window.open("about:blank", "_blank");
  try {
    const r = await fetch(url, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (!r.ok) {
      if (w) w.close();
      alert("report failed: " + (await r.text()));
      return;
    }
    const objUrl = URL.createObjectURL(await r.blob());
    if (w) w.location = objUrl; else window.location = objUrl;  // popup blocked -> same tab
    setTimeout(() => URL.revokeObjectURL(objUrl), 60000);  // let the tab load, then free it
  } catch (e) {
    if (w) w.close();
    alert("report failed: " + e.message);
  }
}

async function openPitchReport() {
  await openReportWith("/api/report/pitch", reportPayloadOrConfirmNational());
}

async function openNegotiationReport() {
  await openReportWith("/api/report/negotiation", reportPayloadOrConfirmNational());
}

/* =======================================================================
   RATE CARD + PAYER SCORECARD
   ======================================================================= */

let ratecardInit = false;
async function initRatecard() {
  refreshRcMpfs();
  if (ratecardInit) { refreshSubjectPickers("#rc-subjects", [{ sel: "#rc-month" }]); return; }
  ratecardInit = true;
  const [subjects, months, payers] = await Promise.all([
    api("/api/benchmark/subjects").catch(() => null),
    api("/api/months").catch(() => null),
    api("/api/payers").catch(() => null),
  ]);
  if (subjects) $("#rc-subjects").innerHTML = subjectOptionsHtml(subjects);
  wireSubjectSearch("#rc-subject", "#rc-subjects");
  fillMonthSelect("#rc-month", months);
  if (payers) $("#rc-payer-chips").innerHTML = payers.payers.map((p) =>
    `<button class="chip" data-payer="${esc(p)}">${esc(p)}</button>`).join("");
  $("#rc-payer-chips").addEventListener("click", (ev) => {
    const b = ev.target.closest(".chip");
    if (b) b.classList.toggle("on");
  });
  $("#rc-run").addEventListener("click", buildRatecard);
  $("#rc-report").addEventListener("click", () =>
    openReportWith("/api/report/ratecard", state.lastRatecardPayload));
  $("#rc-csv").addEventListener("click", downloadRatecardCsv);
}

async function refreshRcMpfs() {
  try {
    const s = await api("/api/mpfs/status");
    $("#rc-mpfs-status").innerHTML = s.loaded
      ? `MPFS loaded: <b>${esc(s.loaded)}</b> — scorecard ranks by % of Medicare`
      : "MPFS: not loaded — scorecard ranks by % of best payer";
  } catch { /* ignore */ }
}

function ratecardPayload() {
  const subject = $("#rc-subject").value.trim();
  const month = $("#rc-month").value;
  if (!subject || !month) throw new Error("subject and as-of month are required");
  const market = {
    month,
    payers: $$("#rc-payer-chips .chip.on").map((c) => c.dataset.payer),
    billing_class: $("#rc-class").value,
  };
  if ($("#rc-disc").value) market.discipline = $("#rc-disc").value;
  return { subject, market };
}

async function buildRatecard() {
  const out = $("#rc-out");
  state.lastRatecardPayload = null;
  $("#rc-report").disabled = true;
  $("#rc-csv").disabled = true;
  let payload;
  try { payload = ratecardPayload(); }
  catch (e) { out.innerHTML = `<div class="empty">${esc(e.message)}</div>`; return; }
  out.innerHTML = `<div class="loading">Building rate card</div>`;
  let data;
  try { data = await postJson("/api/schedule/fee", payload); }
  catch (e) { out.innerHTML = `<div class="empty"><h3>Could not build</h3>${esc(e.message)}</div>`; return; }
  state.lastRatecardPayload = payload;
  $("#rc-report").disabled = false;
  $("#rc-csv").disabled = false;
  renderRatecard(out, data.fee_schedule, data.scorecard);
}

const pctOrDash = (v) => (v == null ? "–" : `${v}%`);

function renderRatecard(out, fs, sc) {
  const mp = fs.mpfs_loaded;
  if (!fs.codes.length) {
    out.innerHTML = `<div class="empty"><h3>No rates found</h3>No published rates for this practice as of ${esc(String(fs.month))} under the current filters.</div>`;
    return;
  }
  // order payer columns by scorecard rank (best first)
  const order = {};
  sc.rows.forEach((r, i) => { order[r.payer] = i; });
  const payers = fs.payers.slice().sort((a, b) => (order[a] ?? 1e9) - (order[b] ?? 1e9));
  const scHead = `<th class="num">Rank</th><th>Payer</th><th class="num">Codes</th>${
    mp ? '<th class="num">Med % MCR</th>' : ""}<th class="num">Med % of best</th><th class="num">Med rate</th>`;
  const scRows = sc.rows.map((r) => `<tr class="${r.rank === 1 ? "best-row" : ""}">
      <td class="num">${r.rank ?? "–"}</td><td>${esc(r.payer)}</td><td class="num">${r.n_codes}</td>
      ${mp ? `<td class="num">${pctOrDash(r.median_pct_medicare)}</td>` : ""}
      <td class="num">${pctOrDash(r.median_pct_of_best)}</td>
      <td class="num">$${fmtMoney(r.median_rate)}</td></tr>`).join("");
  const feeHead = `<th>Code</th>${payers.map((p) => `<th class="num">${esc(p)}</th>`).join("")}`;
  const feeRows = fs.codes.map((e) => {
    const vals = payers.map((p) => (e.rates[p] && e.rates[p].rate != null ? e.rates[p].rate : null));
    const present = vals.filter((v) => v != null);
    const best = present.length ? Math.max(...present) : null;
    const cells = payers.map((p) => {
      const v = e.rates[p];
      if (!v || v.rate == null) return `<td class="num">–</td>`;
      const top = best != null && v.rate === best ? " top-rate" : "";
      const mc = mp && v.pct_medicare != null ? `<div class="sub">${v.pct_medicare}% MC</div>` : "";
      return `<td class="num${top}">$${fmtMoney(v.rate)}${mc}</td>`;
    }).join("");
    return `<tr><td>${esc(e.billing_code)}<div class="sub">${esc(e.description || "")}</div></td>${cells}</tr>`;
  }).join("");
  const bestLine = sc.best_payer ? `Best-paying payer: <b>${esc(sc.best_payer)}</b>. ` : "";
  out.innerHTML = `
    <div class="rc-summary">${bestLine}Payers ranked by ${mp ? "% of Medicare" : "% of the best payer"}.</div>
    <h3>Payer scorecard — who pays best</h3>
    <div class="tablewrap"><table class="rc-table"><thead><tr>${scHead}</tr></thead><tbody>${scRows}</tbody></table></div>
    <h3 style="margin-top:18px">Fee schedule</h3>
    <div class="tablewrap"><table class="rc-table"><thead><tr>${feeHead}</tr></thead><tbody>${feeRows}</tbody></table></div>`;
}

async function downloadRatecardCsv() {
  if (!state.lastRatecardPayload) return;
  try {
    const r = await fetch("/api/schedule/fee.csv", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(state.lastRatecardPayload),
    });
    if (!r.ok) { alert("CSV export failed: " + (await r.text())); return; }
    const a = document.createElement("a");
    a.href = URL.createObjectURL(await r.blob());
    a.download = "rate_card.csv";
    a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 60000);
  } catch (e) { alert("CSV export failed: " + e.message); }
}

/* =======================================================================
   LEADS + RATE-CHANGE MONITORING
   ======================================================================= */

async function postDownload(url, payload, filename) {
  try {
    const r = await fetch(url, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (!r.ok) { alert("export failed: " + (await r.text())); return; }
    const a = document.createElement("a");
    a.href = URL.createObjectURL(await r.blob());
    a.download = filename;
    a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 60000);
  } catch (e) { alert("export failed: " + e.message); }
}

let leadsInit = false;
async function initLeads() {
  if (leadsInit) { refreshSubjectPickers("#ld-subjects", [{ sel: "#ld-month" }]); loadLdStates(); return; }
  leadsInit = true;
  // Load each picker INDEPENDENTLY: a single failing/slow call (e.g. the
  // subject list on a large store) must never leave the required As-of month
  // blank. A fail-fast Promise.all used to abort the whole tab and blank it.
  const [subjects, months, payers, states] = await Promise.all([
    api("/api/benchmark/subjects").catch(() => null),
    api("/api/months").catch(() => null),
    api("/api/payers").catch(() => null),
    api("/api/states").catch(() => null),
  ]);
  fillMonthSelect("#ld-month", months);
  if (payers) $("#ld-payer-chips").innerHTML = payers.payers.map((p) => `<button class="chip" data-payer="${esc(p)}">${esc(p)}</button>`).join("");
  $("#ld-payer-chips").addEventListener("click", (ev) => { const b = ev.target.closest(".chip"); if (b) b.classList.toggle("on"); });
  if (states) $("#ld-states").innerHTML = (states.states || []).map((s) => `<option value="${esc(s)}">`).join("");
  if (subjects) $("#ld-subjects").innerHTML = subjectOptionsHtml(subjects);
  wireSubjectSearch("#ld-exclude", "#ld-subjects");
  $("#ld-run").addEventListener("click", runLeads);
  $("#ld-csv").addEventListener("click", () => { if (state.lastLeadsPayload) postDownload("/api/leads.csv", state.lastLeadsPayload, "leads.csv"); });
}

// Populate a month <select> from an /api/months payload, tolerating a failed
// fetch (null) or a store with no dated months, so the "required" field always
// says something actionable instead of sitting silently empty.
function fillMonthSelect(sel, months, prefix = "") {
  const el = $(sel);
  if (!el) return;
  if (!months) { el.innerHTML = `<option value="">couldn't load months — reopen this tab</option>`; return; }
  const opts = (months.months || []).filter(Boolean).map((m) => `<option>${esc(m)}</option>`).join("");
  el.innerHTML = prefix + opts || `<option value="">no dated data yet — ingest a file first</option>`;
}

async function loadLdStates() {
  try {
    const { states } = await api("/api/states");
    $("#ld-states").innerHTML = (states || []).map((s) => `<option value="${esc(s)}">`).join("");
  } catch { /* keep existing list */ }
}

function leadsPayload() {
  const month = $("#ld-month").value;
  if (!month) throw new Error("an as-of month is required");
  const market = { month, payers: $$("#ld-payer-chips .chip.on").map((c) => c.dataset.payer) };
  if ($("#ld-state").value.trim()) market.state = $("#ld-state").value.trim();
  if ($("#ld-disc").value) market.discipline = $("#ld-disc").value;
  const p = { market, threshold_percentile: +$("#ld-threshold").value, min_codes: +$("#ld-mincodes").value || 3 };
  if ($("#ld-exclude").value.trim()) p.exclude_subject = $("#ld-exclude").value.trim();
  return p;
}

async function runLeads() {
  const out = $("#ld-out");
  state.lastLeadsPayload = null;
  $("#ld-csv").disabled = true;
  let payload;
  try { payload = leadsPayload(); } catch (e) { out.innerHTML = `<div class="empty">${esc(e.message)}</div>`; return; }
  out.innerHTML = `<div class="loading">Finding leads</div>`;
  let data;
  try { data = await postJson("/api/leads", payload); } catch (e) { out.innerHTML = `<div class="empty"><h3>Search failed</h3>${esc(e.message)}</div>`; return; }
  state.lastLeadsPayload = payload;
  $("#ld-csv").disabled = false;
  renderLeads(out, data);
}

function renderLeads(out, data) {
  if (!data.leads.length) {
    out.innerHTML = `<div class="empty"><h3>No leads found</h3>No practices at or below p${data.threshold_percentile} that price at least ${data.min_codes} codes with a market. Widen the cutoff or lower the min codes.</div>`;
    return;
  }
  const rows = data.leads.map((l) => `<tr>
    <td>${esc(l.display_name || "")}<div class="sub">${esc(l.entity_kind || "")}${l.website ? ` · <a href="${esc(l.website)}" target="_blank" rel="noopener">site</a>` : ""}</div></td>
    <td class="sub">${esc([l.city, l.state].filter(Boolean).join(", "))}</td>
    <td class="num">${fmtInt(l.npi_count)}</td>
    <td class="num">${l.n_codes}</td>
    <td class="num">p${l.median_percentile}</td>
    <td class="num">$${fmtMoney(l.avg_gap_to_median)}</td>
    <td class="sub">${esc(l.tin_value)}</td></tr>`).join("");
  out.innerHTML = `<div class="rc-summary">${data.count} practice(s) at or below p${data.threshold_percentile}, most underpaid first. <span class="muted">Avg $ below median is a rate-level gap, not annual dollars.</span></div>
    <div class="tablewrap"><table class="rc-table"><thead><tr><th>Practice</th><th>Location</th><th class="num">Providers</th><th class="num">Codes</th><th class="num">Position</th><th class="num">Avg $ below median</th><th>Tax ID</th></tr></thead><tbody>${rows}</tbody></table></div>`;
}

let changesInit = false;
async function initChanges() {
  if (changesInit) {
    refreshSubjectPickers("#ch-subjects",
      [{ sel: "#ch-month" }, { sel: "#ch-prev", prefix: `<option value="">auto — the previous month present</option>` }]);
    return;
  }
  changesInit = true;
  const [subjects, months, payers] = await Promise.all([
    api("/api/benchmark/subjects").catch(() => null),
    api("/api/months").catch(() => null),
    api("/api/payers").catch(() => null),
  ]);
  fillMonthSelect("#ch-month", months);
  fillMonthSelect("#ch-prev", months, `<option value="">auto — the previous month present</option>`);
  if (payers) $("#ch-payer-chips").innerHTML = payers.payers.map((p) => `<button class="chip" data-payer="${esc(p)}">${esc(p)}</button>`).join("");
  $("#ch-payer-chips").addEventListener("click", (ev) => { const b = ev.target.closest(".chip"); if (b) b.classList.toggle("on"); });
  if (subjects) $("#ch-subjects").innerHTML = subjectOptionsHtml(subjects);
  wireSubjectSearch("#ch-subject", "#ch-subjects");
  $("#ch-run").addEventListener("click", runChanges);
  $("#ch-csv").addEventListener("click", () => { if (state.lastChangesPayload) postDownload("/api/changes.csv", state.lastChangesPayload, "rate_changes.csv"); });
}

function changesPayload() {
  const month = $("#ch-month").value;
  if (!month) throw new Error("the new month is required");
  const market = { month, payers: $$("#ch-payer-chips .chip.on").map((c) => c.dataset.payer) };
  if ($("#ch-prev").value) market.prev_month = $("#ch-prev").value;
  if ($("#ch-disc").value) market.discipline = $("#ch-disc").value;
  const p = { market };
  if ($("#ch-subject").value.trim()) p.subject = $("#ch-subject").value.trim();
  if ($("#ch-minpct").value.trim()) p.min_pct = +$("#ch-minpct").value;
  return p;
}

async function runChanges() {
  const out = $("#ch-out");
  state.lastChangesPayload = null;
  $("#ch-csv").disabled = true;
  let payload;
  try { payload = changesPayload(); } catch (e) { out.innerHTML = `<div class="empty">${esc(e.message)}</div>`; return; }
  out.innerHTML = `<div class="loading">Comparing months</div>`;
  let data;
  try { data = await postJson("/api/changes", payload); } catch (e) { out.innerHTML = `<div class="empty"><h3>Could not compare</h3>${esc(e.message)}</div>`; return; }
  state.lastChangesPayload = payload;
  $("#ch-csv").disabled = false;
  renderChanges(out, data);
}

function renderChanges(out, d) {
  if (!d.changes.length) {
    out.innerHTML = `<div class="empty"><h3>No changes</h3>No rate moves from ${esc(d.prev_month)} to ${esc(d.new_month)} under the current filters.</div>`;
    return;
  }
  const rows = d.changes.map((c) => {
    const cls = c.direction === "cut" ? "chg-cut" : "chg-up";
    return `<tr><td>${esc(c.payer)}</td>
      <td>${esc(c.display_name || "")}<div class="sub">${esc(c.tin_value)}</div></td>
      <td>${esc(c.billing_code)}<div class="sub">${esc(c.description || "")}${c.modifier_set ? ` · ${esc(c.modifier_set)}` : ""}</div></td>
      <td class="num">$${fmtMoney(c.old_rate)}</td><td class="num">$${fmtMoney(c.new_rate)}</td>
      <td class="num ${cls}">${c.delta < 0 ? "−" : "+"}$${fmtMoney(Math.abs(c.delta))}</td>
      <td class="num ${cls}">${c.pct_change > 0 ? "+" : ""}${c.pct_change}%</td></tr>`;
  }).join("");
  out.innerHTML = `<div class="rc-summary"><b>${d.n_cuts}</b> cut(s), <b>${d.n_increases}</b> increase(s) from ${esc(d.prev_month)} → ${esc(d.new_month)}.${d.biggest_cut_pct != null ? ` Biggest cut ${d.biggest_cut_pct}%.` : ""}</div>
    <div class="tablewrap"><table class="rc-table"><thead><tr><th>Payer</th><th>Practice</th><th>Code</th><th class="num">Was</th><th class="num">Now</th><th class="num">Δ</th><th class="num">%</th></tr></thead><tbody>${rows}</tbody></table></div>`;
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
      <td>${warn}${f.error ? `<div class="${f.status === "pending_confirmation" ? "warn-text" : "err-text"}">${esc(f.error)}</div>` : ""}</td>
      <td class="sub">${f.finished_at ? esc(f.finished_at.slice(0, 19)) : ""}</td>
      <td>${f.status === "processing" || f.status === "queued" ? "" : `<button class="btn forget-btn" title="erase this file's rates and free its disk space (re-add its link or file to get it back)" data-forget="${esc(f.filename)}">remove</button>`}</td>
    </tr>`;
  }).join("");
  $$("[data-confirm]", body).forEach((b) =>
    b.addEventListener("click", async () => {
      b.disabled = true;
      try {
        const r = await fetch(`/api/files/${encodeURIComponent(b.dataset.confirm)}/confirm`, { method: "POST" });
        if (!r.ok) alert((await r.json().catch(() => ({}))).detail || `confirm failed (HTTP ${r.status})`);
      } catch (e) { alert("confirm failed: " + e.message); b.disabled = false; return; }
      loadFiles();
    }));
  $$("[data-forget]", body).forEach((b) =>
    b.addEventListener("click", async () => {
      const name = b.dataset.forget;
      if (!confirm(`Remove ${name}?\n\nThis erases its rates from the database and deletes its raw copies to free disk space. You can get it back any time by re-adding its link or file.`)) return;
      b.disabled = true;
      try {
        const r = await fetch(`/api/files/${encodeURIComponent(name)}`, { method: "DELETE" });
        if (!r.ok) {
          alert((await r.json().catch(() => ({}))).detail || `remove failed (HTTP ${r.status})`);
          b.disabled = false;
          return;
        }
      } catch (e) { alert("remove failed: " + e.message); b.disabled = false; return; }
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
  duplicate: "duplicate (already have it)",
  unknown: "?",
};

async function loadUrlQueue() {
  let d;
  try { d = await api("/api/urls"); } catch { return; }
  const wrap = $("#url-wrap"), body = $("#url-body"), countsEl = $("#url-counts");
  if (!d.urls.length) { wrap.style.display = "none"; countsEl.textContent = ""; return; }
  wrap.style.display = "block";
  const c = d.counts || {};
  const total = Object.values(c).reduce((a, b) => a + b, 0);
  const parts = ["queued", "downloading", "fetched", "expanding", "ingesting", "done", "skipped"]
    .filter((k) => c[k]).map((k) => k === "skipped"
      // skips are the app working as designed (duplicates of files already
      // ingested, files without the target codes) — say so, or a healthy
      // grind reads as mass failure
      ? `${fmtInt(c[k])} skipped <span class="muted">(duplicates &amp; non-matches — normal)</span>`
      : `${fmtInt(c[k])} ${k}`);
  // over-size files are their OWN category, not failures — otherwise one big
  // payer's shards make the failure count look alarming and bury real errors
  if (c.oversize) parts.push(`<span class="warn-text">${fmtInt(c.oversize)} too big (needs your OK)</span>`);
  if (c.failed) parts.push(`<span class="err-text">${fmtInt(c.failed)} failed</span>`);
  countsEl.innerHTML = parts.join(" · ") +
    (total > d.urls.length ? ` <span class="muted">(showing the newest ${fmtInt(d.urls.length)} of ${fmtInt(total)} links)</span>` : "");
  if (c.failed) {
    const btn = document.createElement("button");
    btn.className = "btn";
    btn.style.cssText = "padding:1px 8px;font-size:11.5px;margin-left:8px";
    btn.textContent = "retry all failed";
    btn.addEventListener("click", async () => {
      btn.disabled = true;
      await fetch("/api/urls/retry-failed", { method: "POST" });
      loadUrlQueue();
    });
    countsEl.appendChild(btn);
  }
  body.innerHTML = d.urls.map((u) => {
    const short = u.url.split("?")[0].replace(/^https?:\/\//, "");
    const shown = short.length > 78 ? short.slice(0, 38) + "…" + short.slice(-37) : short;
    const badgeClass = u.status === "done" ? "done"
      : u.status === "failed" ? "failed"
      : u.status === "skipped" ? "skipped"
      : u.status === "oversize" ? "pending_confirmation"  // amber, not red
      : "processing";
    const badgeLabel = u.status === "oversize" ? "too big" : u.status;
    let statusCell = `<span class="badge ${esc(badgeClass)}">${esc(badgeLabel)}</span>`;
    if (u.status === "downloading" && u.bytes_total > 0) {
      statusCell += `<div class="progress"><div class="progress-fill" style="width:${Math.round(u.progress)}%"></div>
        <span class="progress-label">${(u.bytes_done / 1e6).toFixed(0)} / ${(u.bytes_total / 1e6).toFixed(0)} MB</span></div>`;
    } else if (u.status === "ingesting") {
      statusCell += ` <span class="muted">(see file row below for chunk progress)</span>`;
    }
    if (u.status === "failed" || u.status === "skipped" || u.status === "oversize") {
      statusCell += ` <button class="btn" style="padding:1px 8px;font-size:11.5px" data-url-retry="${u.id}">retry</button>`;
    }
    // over the size ceiling? offer a one-click "download anyway"
    // (the disk-space guard still protects the drive)
    if (u.status === "oversize" ||
        ((u.status === "failed" || u.status === "skipped") && /confirm_over_gb|safety limit/i.test(u.error || ""))) {
      statusCell += ` <button class="btn" style="padding:1px 8px;font-size:11.5px" title="download and ingest this file even though it is over the size limit — the disk-space check still applies" data-url-force="${u.id}">download anyway</button>`;
    }
    if (u.status === "queued" || u.status === "failed" || u.status === "oversize") {
      statusCell += ` <button class="btn" style="padding:1px 8px;font-size:11.5px" data-url-cancel="${u.id}">skip</button>`;
    }
    let notes = "";
    if (u.kind === "toc" && u.status === "done") notes = `found ${fmtInt(u.child_count)} files inside — queued below`;
    else if (u.kind === "page" && u.status === "done") notes = `found ${fmtInt(u.child_count)} file links on the page`;
    // Color the note by what it actually is: red is reserved for real
    // failures. Over-size is amber ("your OK needed"), and SKIPPED rows are
    // neutral — a duplicate of an already-ingested file, a file with none of
    // the target codes, or an allowed-amounts-only TOC is the app working as
    // designed, and on a big Blues aggregation duplicates can be MOST rows.
    // Painting those red made a healthy run read as "the vast majority of
    // files are showing errors".
    else if (u.error) notes = `<span class="${
      u.status === "oversize" ? "warn-text" : u.status === "skipped" ? "muted" : "err-text"
    }">${esc(u.error)}</span>`;
    return `<tr>
      <td title="${esc(u.url)}">${esc(shown)}</td>
      <td>${esc(KIND_LABEL[u.kind] || u.kind || "…")}</td>
      <td>${statusCell}</td>
      <td class="num">${u.rows_emitted ? fmtInt(u.rows_emitted) : ""}</td>
      <td style="max-width:420px">${notes}</td>
    </tr>`;
  }).join("");
  const urlAction = (b, path, label) => async () => {
    b.disabled = true;
    try {
      const r = await fetch(path, { method: "POST" });
      if (!r.ok) alert((await r.json().catch(() => ({}))).detail || `${label} failed (HTTP ${r.status})`);
    } catch (e) { alert(`${label} failed: ` + e.message); b.disabled = false; return; }
    loadUrlQueue();
  };
  $$("[data-url-retry]", body).forEach((b) =>
    b.addEventListener("click", urlAction(b, `/api/urls/${b.dataset.urlRetry}/retry`, "retry")));
  $$("[data-url-cancel]", body).forEach((b) =>
    b.addEventListener("click", urlAction(b, `/api/urls/${b.dataset.urlCancel}/cancel`, "skip")));
  $$("[data-url-force]", body).forEach((b) =>
    b.addEventListener("click", async () => {
      if (!confirm("Download and ingest this oversized file?\n\nIt's larger than the safety limit, so it may take a long time and use a lot of disk. The disk-space check still applies, so it won't fill your drive.")) return;
      await urlAction(b, `/api/urls/${b.dataset.urlForce}/force-size`, "could not start")();
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

  $("#url-known").addEventListener("click", async () => {
    const msg = $("#url-msg");
    if (!confirm(
      "Queue every payer index this app has been tested against?\n\n" +
      "That's a lot of data: each index lists hundreds of rate files, and " +
      "they download and process several at a time in the background, scaled " +
      "to your machine (waiting rows can be skipped any time). Files " +
      "identical to ones already loaded are skipped automatically."
    )) return;
    msg.textContent = "queueing tested sources…";
    try {
      const r = await postJson("/api/urls/known", {});
      msg.textContent = `${r.added} tested indexes queued` +
        (r.skipped ? `, ${r.skipped} already known` : "") +
        (r.portals ? ` (${r.portals} portal-only sources need a browser — see "Show tested sources")` : "");
    } catch (e) {
      msg.textContent = "could not queue: " + e.message;
    }
    loadUrlQueue();
  });

  $("#url-known-list").addEventListener("click", async () => {
    const wrap = $("#known-wrap");
    if (wrap.style.display !== "none") { wrap.style.display = "none"; return; }
    let d;
    try { d = await api("/api/known-sources"); }
    catch (e) { $("#url-msg").textContent = `couldn't load the source list: ${e.message}`; return; }
    $("#known-body").innerHTML = (d.sources || []).map((s) => `
      <tr>
        <td><a href="${esc(s.url)}" target="_blank" rel="noopener">${esc(s.name)}</a></td>
        <td>${s.queueable ? "auto-queueable" : "portal (open in browser)"}</td>
        <td>${esc(s.verified || "")}</td>
        <td style="max-width:480px">${esc(s.notes || "")}</td>
      </tr>`).join("");
    wrap.style.display = "";
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
    let r;
    try {
      r = await postJson("/api/validate", {
        id, code, ...(expected ? { expected_rate: +expected } : {}),
      });
    } catch (e) { alert(`validation lookup failed: ${e.message}`); return; }
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
  // refresh in place — a full page reload dumped the user back on the
  // Explorer tab right after they dropped a file on the Files tab
  $("#dropzone").innerHTML = `<div class="muted">Uploaded — processing begins shortly.
    Drop more files here.</div>`;
  loadFiles();
}

/* =======================================================================
   SOURCES
   ======================================================================= */

let sourcesLoaded = false;
async function loadSources(stateCode = "") {
  let d;
  try {
    d = await api(`/api/sources${stateCode ? `?state=${stateCode}` : ""}`);
  } catch (e) {
    // a failed fetch used to leave a silently blank tab with no explanation
    $("#src-state-cards").innerHTML =
      `<div class="empty">Couldn't load the sources list (${esc(e.message)}) — ` +
      `is the server still running? Switch tabs and back to retry.</div>`;
    return;
  }
  if (!sourcesLoaded) {
    const sel = $("#src-state");
    sel.innerHTML = `<option value="">— pick a state —</option>` +
      d.states.map((s) => `<option>${esc(s)}</option>`).join("");
    // assignment, not addEventListener: an override-confirm reload re-runs
    // this block, and stacked listeners fired N requests per change
    sel.onchange = () => loadSources(sel.value);
    sel.value = stateCode;  // keep the picked state across override reloads
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
  loadStateOptions();
  // one failed boot fetch must not brick the page: without this guard an
  // /api/payers hiccup at load left the Code and Files tabs never wired
  try { await initFilters(); }
  catch (e) { console.error("filter init failed — refresh to retry:", e); }
  try { initCptView(); } catch (e) { console.error(e); }
  try { initFilesView(); } catch (e) { console.error(e); }
  refresh();
  setInterval(() => { loadStats(); loadStateOptions(); }, 15000);
})();
