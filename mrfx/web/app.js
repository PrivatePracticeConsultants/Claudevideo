/* MRF Explorer SPA — vanilla JS, no external dependencies. */
"use strict";

const $ = (sel, el = document) => el.querySelector(sel);
const $$ = (sel, el = document) => [...el.querySelectorAll(sel)];

// Default as-of choice for the report tabs: one CURRENT rate per contract
// across every payer (newest file each), instead of forcing a single calendar
// month. Value "latest" is understood by the benchmark/negotiate/leads/ratecard
// APIs; specific months stay selectable below it for a historical snapshot.
const LATEST_MONTH_OPT =
  '<option value="latest">Latest available — all payers, newest rates</option>';

const state = {
  view: "explorer",
  grain: "tin",
  filters: {
    payers: [], cpts: [], disciplines: [], modifier: "", mod_has: "", mod_not: "",
    billing_class: "", pos: "", state: "", city: "", month: "", q: "",
    dollar: true, hide_tin_npi: false, hide_outliers: false, therapy_only: false,
    rate_min: "", rate_max: "",
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
// dollar-prefixed money that degrades to a bare en-dash for null — a plain
// `"$" + fmtMoney(v)` rendered "$–" for a missing value (a code with no peers,
// an unfilled opportunity cell), which reads as a glitch. Negatives render as
// "-$50.00" (not "$-50.00") — the benchmark Gap column is negative whenever the
// subject is priced above target, and the reports / Negotiate tab already do
// this. Use for any money cell that can be empty and/or negative.
const money = (v) =>
  v == null ? "–" : v < 0 ? "-$" + fmtMoney(-v) : "$" + fmtMoney(v);
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
  if (view === "markets") initMarkets();
  if (view === "benchmark") initBenchmark();
  if (view === "negotiate") initNegotiate();
  if (view === "ratecard") initRatecard();
  if (view === "leads") initLeads();
  if (view === "changes") initChanges();
  // re-entry: keep whatever state was picked (else the cards blank out while the
  // dropdown still shows the state, and re-picking the same option fires no change)
  if (view === "sources") loadSources($("#src-state")?.value || "");
  if (view === "overview") initOverview();
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
        // On the slow per-NPI NPPES API with a big backlog, nudge toward the
        // one-pass bulk file — the difference between days and minutes. Only
        // when we're NOT already using it and the backlog is large enough to
        // matter (a small tail finishes on its own).
        if (!s.enrichment_bulk_active && e.remaining > 5000) {
          html += ` <span class="muted">— slow (NPPES web lookups). To finish ` +
                  `in one local pass, download the NPPES monthly file ` +
                  `(<a href="https://download.cms.gov/nppes/NPI_Files.html" target="_blank" rel="noopener">download.cms.gov</a>) ` +
                  `and set <code>bulk_csv_path</code> in <code>config/mrfx.yaml</code>, ` +
                  `then restart — or run <code>mrfx enrich --bulk &lt;file.zip&gt;</code>.</span>`;
        }
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
    const ov = $("#ov-state-list");
    if (ov) ov.innerHTML = (states || []).map((s) => `<option value="${esc(s)}">`).join("");
  } catch { /* filter still works as free text */ }
}

/* =======================================================================
   MARKET OVERVIEW (landing) — orient the user before they touch a filter
   ======================================================================= */
let overviewWired = false;
async function initOverview() {
  if (!overviewWired) {
    overviewWired = true;
    $("#ov-refresh").addEventListener("click", () => loadOverview($("#ov-state").value.trim()));
    $("#ov-state").addEventListener("change", () => loadOverview($("#ov-state").value.trim()));
    loadStateOptions();
  }
  loadOverview($("#ov-state").value.trim());
}

async function loadOverview(stateCode = "") {
  const out = $("#ov-out");
  out.innerHTML = `<div class="loading">Loading market overview</div>`;
  let d;
  try { d = await api(`/api/overview${stateCode ? `?state=${encodeURIComponent(stateCode)}` : ""}`); }
  catch (e) { out.innerHTML = `<div class="empty">Couldn't load the overview (${esc(e.message)}).</div>`; return; }
  if (!d.practices) {
    out.innerHTML = `<div class="empty"><h3>No rates${stateCode ? " in " + esc(stateCode) : ""} yet</h3>Ingest some payer files first, or clear the state filter.</div>`;
    return;
  }
  const scope = d.state ? `${esc(d.state)}` : "all loaded states";
  const cards = [
    ["Payers", fmtInt(d.payers)],
    ["Practices (TINs)", fmtInt(d.practices)],
    ["Codes", fmtInt(d.codes)],
    ["States with data", fmtInt(d.states)],
  ].map(([k, v]) => `<div class="stat"><b>${v}</b><span>${k}</span></div>`).join("");

  const disc = (d.by_discipline || []).length
    ? `<div class="ov-block"><h3>Typical rate by discipline</h3><div class="tablewrap"><table>
        <thead><tr><th>Discipline</th><th class="num">Median rate</th><th class="num">Practices</th></tr></thead>
        <tbody>${d.by_discipline.map((x) => `<tr><td>${esc(x.label)}</td><td class="num">${money(x.median)}</td><td class="num muted">${fmtInt(x.practices)}</td></tr>`).join("")}</tbody></table></div></div>`
    : "";

  // payer index: 1.00 = market; above = pays more, below = pays less. Color the bar.
  const idx = (d.payer_index || []);
  const payerBlock = idx.length
    ? `<div class="ov-block"><h3>Which payers pay above vs below market</h3>
       <div class="muted" style="margin-bottom:6px">Index = this payer's median rate ÷ the market median, across the codes it prices. <b>1.10 = pays ~10% above market</b>; 0.90 = ~10% below. Payers pricing under 3 codes are omitted.</div>
       <div class="tablewrap"><table>
        <thead><tr><th>Payer</th><th class="num">Index</th><th>vs market</th><th class="num">Codes</th></tr></thead>
        <tbody>${idx.map((p) => {
          const pct = Math.round((p.index - 1) * 100);
          const above = p.index >= 1;
          const w = Math.min(Math.abs(p.index - 1) * 100 * 2, 100);
          return `<tr><td>${esc(p.payer)}</td>
            <td class="num"><b>${p.index.toFixed(2)}</b></td>
            <td><span class="idxbar ${above ? "idx-up" : "idx-down"}" style="width:${Math.max(w, 3)}%"></span>
                <span class="pctlbl ${above ? "" : "warn-text"}">${pct > 0 ? "+" : ""}${pct}%</span></td>
            <td class="num muted">${fmtInt(p.codes)}</td></tr>`;
        }).join("")}</tbody></table></div></div>`
    : `<div class="ov-block muted">Not enough payer overlap yet to rank payers (need payers pricing ≥3 of the same codes). Ingest more files.</div>`;

  const topCodes = (d.top_codes || []).length
    ? `<div class="ov-block"><h3>Best-covered codes</h3><div class="tablewrap"><table>
        <thead><tr><th>Code</th><th>Description</th><th class="num">Practices</th><th class="num">Market median</th></tr></thead>
        <tbody>${d.top_codes.map((c) => `<tr><td>${esc(c.billing_code)}</td><td class="sub">${esc(c.description || "")}</td><td class="num">${fmtInt(c.practices)}</td><td class="num">${money(c.median)}</td></tr>`).join("")}</tbody></table></div></div>`
    : "";

  out.innerHTML = `<div class="rc-summary">Market snapshot — <b>${scope}</b>. ${payerBlock ? "The payer index below is the fastest read on where the reimbursement leverage is." : ""}</div>
    <div class="stats-row">${cards}</div>${payerBlock}${disc}${topCodes}
    <div class="bench-note">All figures use published dollar negotiated rates (base modifier, professional class). A published rate is directional market positioning, not proof a provider collects it.</div>`;
}

/* =======================================================================
   MARKETS — single-code market intelligence (geography, negotiability,
   % of Medicare, assistant/telehealth differential)
   ======================================================================= */
let marketsWired = false;

async function initMarkets() {
  if (!marketsWired) {
    marketsWired = true;
    // code datalist from the catalog; state + payer lists loaded once
    $("#mk-code-opts").innerHTML = Object.entries(state.catalog)
      .map(([c, info]) => `<option value="${esc(c)}">${esc(info.description || "")}</option>`).join("");
    try {
      const [{ states }, { payers }] = await Promise.all([
        api("/api/states").catch(() => ({ states: [] })),
        api("/api/payers").catch(() => ({ payers: [] })),
      ]);
      $("#mk-state-opts").innerHTML = (states || []).map((s) => `<option value="${esc(s)}">`).join("");
      $("#mk-payer-opts").innerHTML = (payers || []).map((p) => `<option value="${esc(p)}">`).join("");
    } catch { /* free text still works */ }
    $("#mk-run").addEventListener("click", loadMarkets);
    $("#mk-code").addEventListener("keydown", (e) => { if (e.key === "Enter") loadMarkets(); });
    if (!$("#mk-code").value) {
      const first = Object.keys(state.catalog)[0];
      if (first) $("#mk-code").value = first;
    }
  }
}

function marketFromMk() {
  const market = { month: "latest" };
  const st = $("#mk-state").value.trim();
  const payer = $("#mk-payer").value.trim();
  if (st) market.state = st.toUpperCase();
  if ($("#mk-disc").value) market.discipline = $("#mk-disc").value;
  if (payer) market.payers = [payer];
  if ($("#mk-therapy").checked) market.therapy_only = true;
  return market;
}

async function loadMarkets() {
  const out = $("#mk-out");
  const code = $("#mk-code").value.trim();
  if (!code) { out.innerHTML = `<div class="empty">Pick a billing code first.</div>`; return; }
  const market = marketFromMk();
  const body = { code, market };
  out.innerHTML = `<div class="loading">Loading market intelligence for ${esc(code)}</div>`;
  // fetch all four views in parallel; each renders (or shows its own error) so
  // one failing section never blanks the others
  const [geo, neg, mcr, diff] = await Promise.all([
    postJson("/api/market/geography", body).catch((e) => ({ _err: e.message })),
    postJson("/api/market/negotiability", body).catch((e) => ({ _err: e.message })),
    postJson("/api/market/medicare", body).catch((e) => ({ _err: e.message })),
    postJson("/api/market/differential", body).catch((e) => ({ _err: e.message })),
  ]);
  const desc = state.catalog[code]?.description || "";
  out.innerHTML =
    `<div class="rc-summary">Market intelligence — <b>${esc(code)}</b>${desc ? " · " + esc(desc) : ""}. ` +
    `Latest rate per contract; thin cells (&lt;5 practices) suppressed.</div>` +
    renderMkMedicare(mcr) + renderMkNegotiability(neg) +
    renderMkGeography(geo) + renderMkDifferential(diff);
}

function mkErr(d, title) {
  return `<div class="ov-block"><h3>${title}</h3><div class="empty">${esc(d._err)}</div></div>`;
}
function mkEmpty(title, msg) {
  return `<div class="ov-block"><h3>${title}</h3><div class="muted">${msg}</div></div>`;
}

function renderMkGeography(d) {
  if (d._err) return mkErr(d, "Where it pays best (by state)");
  if (!d.states || !d.states.length)
    return mkEmpty("Where it pays best (by state)",
      "No state has ≥5 identified practices for this code yet (geography comes from NPPES enrichment — it fills in as NPIs are identified).");
  const rows = d.states.map((s) => `<tr>
    <td>${esc(s.state)}</td>
    <td class="num"><b>${money(s.median_rate)}</b></td>
    <td class="num muted">${money(s.p25)}–${money(s.p75)}</td>
    <td class="num muted">${fmtInt(s.n_practices)}</td></tr>`).join("");
  return `<div class="ov-block"><h3>Where it pays best — by state</h3>
    <div class="muted" style="margin-bottom:6px">National median ${money(d.national_median)} across ${fmtInt(d.national_practices)} practices. ${esc(d.geo_note)}</div>
    <div class="tablewrap"><table>
      <thead><tr><th>State</th><th class="num">Median</th><th class="num">P25–P75</th><th class="num">Practices</th></tr></thead>
      <tbody>${rows}</tbody></table></div></div>`;
}

function renderMkNegotiability(d) {
  if (d._err) return mkErr(d, "Which payers negotiate (rate spread)");
  if (!d.payers || !d.payers.length)
    return mkEmpty("Which payers negotiate (rate spread)",
      "No payer prices this code across ≥5 practices yet — a spread needs a real market.");
  const badge = { wide: "good", moderate: "", tight: "muted" };
  const rows = d.payers.map((p) => `<tr>
    <td>${esc(p.payer)}</td>
    <td><span class="disc-tag ${badge[p.negotiability] || ""}">${esc(p.negotiability)}</span></td>
    <td class="num">${p.spread_pct == null ? "–" : p.spread_pct + "%"}</td>
    <td class="num muted">${money(p.p10)}</td>
    <td class="num"><b>${money(p.p50)}</b></td>
    <td class="num muted">${money(p.p90)}</td>
    <td class="num muted">${fmtInt(p.n_practices)}</td></tr>`).join("");
  return `<div class="ov-block"><h3>Which payers negotiate — rate spread across practices</h3>
    <div class="muted" style="margin-bottom:6px">Spread = (p90 − p10) as a % of the payer's median. <b>Wide</b> ≥25% (clearly negotiates — a low-paid practice has room); <b>tight</b> &lt;10% (a fixed fee schedule). Ranked widest first.</div>
    <div class="tablewrap"><table>
      <thead><tr><th>Payer</th><th>Negotiability</th><th class="num">Spread</th><th class="num">P10</th><th class="num">Median</th><th class="num">P90</th><th class="num">Practices</th></tr></thead>
      <tbody>${rows}</tbody></table></div></div>`;
}

function renderMkMedicare(d) {
  if (d._err) return mkErr(d, "% of Medicare");
  // gate on the PER-CODE anchor (mpfs_rate), not the global mpfs_loaded flag: an
  // MPFS file can be loaded yet not contain this code, in which case every
  // %-of-Medicare is null and the note would read "null%".
  if (!d.mpfs_rate)
    return mkEmpty("% of Medicare",
      (d.mpfs_loaded
        ? "The loaded MPFS file has no non-facility rate for this code — can't anchor it to Medicare."
        : "No MPFS anchor loaded — load a Medicare Physician Fee Schedule CSV on the Benchmark tab to see rates as a % of Medicare.") +
      " (Market median: " + money(d.market_median) + " across " + fmtInt(d.market_practices) + " practices.)");
  if (!d.payers || !d.payers.length)
    return mkEmpty("% of Medicare", "No payer prices this code across ≥5 practices yet.");
  const rows = d.payers.map((p) => `<tr>
    <td>${esc(p.payer)}</td>
    <td class="num"><b>${money(p.median_rate)}</b></td>
    <td class="num">${p.pct_medicare == null ? "–" : p.pct_medicare + "%"}</td>
    <td class="num muted">${fmtInt(p.n_practices)}</td></tr>`).join("");
  return `<div class="ov-block"><h3>% of Medicare</h3>
    <div class="muted" style="margin-bottom:6px">Medicare (non-facility) = ${money(d.mpfs_rate)}. Market median ${money(d.market_median)} across ${fmtInt(d.market_practices)} practices = <b>${d.market_pct_medicare}% of Medicare</b> (P25–P75: ${d.market_p25_pct_medicare}%–${d.market_p75_pct_medicare}%). ${esc(d.medicare_note)}</div>
    <div class="tablewrap"><table>
      <thead><tr><th>Payer</th><th class="num">Median rate</th><th class="num">% of Medicare</th><th class="num">Practices</th></tr></thead>
      <tbody>${rows}</tbody></table></div></div>`;
}

function renderMkDifferential(d) {
  if (d._err) return mkErr(d, "Assistant & telehealth differentials");
  if (!d.payers || !d.payers.length)
    return mkEmpty("Assistant & telehealth differentials",
      "No payer publishes a paired assistant (CQ/CO) or telehealth line for this code — nothing to compare. (That itself is informative: assistants/telehealth are billed at the base rate or simply not published.)");
  const mc = d.min_cell || 5;
  const thin = (n) => (n && n < mc ? " <span class='timed-tag' title='fewer than " + mc + " practices back this — directional, not a market fact'>thin</span>" : "");
  const rows = d.payers.map((p) => `<tr>
    <td>${esc(p.payer)}</td>
    <td class="num">${p.asst_pairs && p.asst_pct_of_base != null ? p.asst_pct_of_base + "%" + thin(p.asst_pairs) : "<span class='muted'>not published</span>"}</td>
    <td class="num muted">${p.asst_pairs ? money(p.asst_base_med) + " → " + money(p.asst_med) + " · " + fmtInt(p.asst_pairs) : "–"}</td>
    <td class="num">${p.tele_pairs && p.tele_pct_of_office != null ? p.tele_pct_of_office + "%" + thin(p.tele_pairs) : "<span class='muted'>not published</span>"}</td>
    <td class="num muted">${p.tele_pairs ? money(p.office_med) + " → " + money(p.tele_med) + " · " + fmtInt(p.tele_pairs) : "–"}</td></tr>`).join("");
  return `<div class="ov-block"><h3>Assistant (CQ/CO) &amp; telehealth differentials</h3>
    <div class="muted" style="margin-bottom:6px">${esc(d.diff_note)}</div>
    <div class="tablewrap"><table>
      <thead><tr><th>Payer</th><th class="num">Assistant % of base</th><th class="num">base→asst · pairs</th><th class="num">Telehealth % of office</th><th class="num">office→tele · pairs</th></tr></thead>
      <tbody>${rows}</tbody></table></div></div>`;
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
  if (f.therapy_only) p.set("therapy_only", "1");
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
    $("#pg-prev").disabled = true;   // stale-enabled buttons on the old branch
    $("#pg-next").disabled = true;   // let a click page a nonexistent table
    return;
  }
  // total shrank under us (a file was removed mid-browse): an out-of-range page
  // renders an empty body with "page 99 of 1" — clamp back and refetch instead
  if (!data.rows.length && data.page > 1) {
    state.page = Math.max(1, Math.ceil(data.total / state.pageSize));
    loadRates();
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
    <td class="num"><span class="rate">${money(r.negotiated_rate)}</span></td>
    <td class="num">${variants}</td>
    <td>${esc(r.negotiated_type)}${r.is_dollar_rate ? "" : ` <span class="warn-text">(non-dollar)</span>`}</td>
    <td class="num">${fmtInt(r.source_count)}</td>
  </tr>`;
}
// referenced above at the rate cell: use money() so a non-dollar row (null
// rate when "dollar rates only" is unchecked) renders "–", not "$–".

async function loadSummary() {
  const el = $("#summary-strip");
  try {
    const s = await api(`/api/summary?${filterQuery()}`);
    const stat = (k, v) => `<div class="stat"><div class="k">${k}</div><div class="v">${v}</div></div>`;
    el.innerHTML =
      stat("Rows", fmtInt(s.n)) +
      stat(state.grain === "npi" ? "NPIs" : "Entities", fmtInt(s.entities)) +
      stat("Codes", fmtInt(s.codes)) +
      stat("Min", money(s.min)) +
      stat("P25", money(s.p25)) +
      stat("Median", money(s.median)) +
      stat("P75", money(s.p75)) +
      stat("Max", money(s.max));
    // all five rate stats blend every matched code (97110 with 97530 etc.) —
    // say so once, so P25/Median/P75 aren't read as one service's spread
    const note = $("#summary-strip-note");
    if (note) note.textContent =
      "Min · P25 · Median · P75 · Max span every matched code together; per-code medians are in the table below.";
    renderByCode($("#summary-bycode"), s.by_code, s.mpfs_loaded);
  } catch (e) {
    // Don't blank silently: a 503 here is almost always the summary's exact
    // aggregates hitting the memory cap on a big unfiltered view. Say so and
    // what to do — the same guidance loadRates gives — instead of a mystery
    // empty strip.
    const msg = /memory/i.test(e.message || "")
      ? "Stats need more memory than the current limit — add a filter (payer, code, or state), or raise duckdb_memory_gb in config and restart."
      : "Stats are busy — they'll appear on the next refresh.";
    el.innerHTML = `<div class="stat" style="flex:1"><div class="k">Summary</div><div class="v" style="font-size:0.8rem;font-weight:normal">${esc(msg)}</div></div>`;
    const bc = $("#summary-bycode"); if (bc) bc.innerHTML = "";
  }
}

// Per-code breakdown of the current filter — the strip's one blended median
// mixes different services (97110 vs 97530), so give a real median + spread PER
// code, with a below-Medicare flag when an MPFS anchor is loaded.
function renderByCode(el, rows, mpfs) {
  if (!el) return;
  if (!rows || rows.length < 2) { el.innerHTML = ""; return; }  // one code: strip is enough
  const body = rows.map((c) => `<tr>
    <td>${esc(c.billing_code)}</td>
    <td class="num"><span class="rate">${money(c.median)}</span></td>
    <td class="num muted">${money(c.p25)}–${money(c.p75)}</td>
    ${mpfs ? `<td class="num ${c.below_medicare ? "warn-text" : ""}">${c.pct_medicare != null ? c.pct_medicare + "%" : "–"}${c.below_medicare ? " ⚠" : ""}</td>` : ""}
    <td class="num muted">${fmtInt(c.entities)}</td></tr>`).join("");
  el.innerHTML = `<div class="ov-block"><h3>By code${mpfs ? " · ⚠ = below Medicare" : ""}</h3>
    <div class="tablewrap" style="max-height:34vh"><table>
      <thead><tr><th>Code</th><th class="num">Median</th><th class="num">P25–P75</th>${mpfs ? `<th class="num">% of Medicare</th>` : ""}<th class="num">Practices</th></tr></thead>
      <tbody>${body}</tbody></table></div></div>`;
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
  // Resilient loads (same class as the Leads-tab fix): a single failed boot
  // fetch — server mid-restart, transient DB lock — must NOT abort this
  // function before the export/pagination/sort listeners below are wired,
  // which left a filterless, unpaginable Explorer until a manual reload.
  const payers = (await api("/api/payers").catch(() => null))?.payers ?? [];
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

  const months = (await api("/api/months").catch(() => null))?.months ?? [];
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
  bind("#f-therapy", "therapy_only");

  $("#f-clear").addEventListener("click", () => {
    state.filters = { payers: [], cpts: [], disciplines: [], modifier: "", mod_has: "", mod_not: "",
      billing_class: "", pos: "", state: "", city: "", month: "", q: "",
      dollar: true, hide_tin_npi: false, hide_outliers: false, therapy_only: false,
      rate_min: "", rate_max: "" };
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
    $("#f-therapy").checked = false;
    refreshFromFirstPage();
  });
  $("#btn-export").addEventListener("click", () => {
    location.href = `/api/export.csv?${filterQuery({ sort: state.sort.col, dir: state.sort.dir, view: "explorer" })}`;
  });
  $("#btn-export-zip").addEventListener("click", () => {
    location.href = `/api/export.zip?${filterQuery({ sort: state.sort.col, dir: state.sort.dir, view: "explorer" })}`;
  });
  $("#btn-outreach").addEventListener("click", () => {
    // .zip: the outreach CSV + its methodology sidecar together (comment lines
    // inside the CSV itself would break the mail-merge import it exists for)
    location.href = `/api/export/outreach.zip?${filterQuery()}`;
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
  // generic text on purpose: unitId can be an SSN-pattern TIN, and this
  // pre-load flash was the one surface that painted it unmasked
  drawer.innerHTML = `<div class="loading">Loading provider detail</div>`;
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
            <td class="num"><span class="rate">${money(r.negotiated_rate)}</span></td>
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
  if (!chart || !chart.length) { el.innerHTML = `<div class="muted">no dollar rates</div>`; return; }
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
    // the on-screen ranking is ALWAYS TIN grain (selectCpt hardcodes it), so the
    // export must be too — inheriting the Explorer tab's grain toggle emitted
    // NPI-grain rows that didn't match the table the user was looking at.
    const p = new URLSearchParams({ cpt: state.cptSelected, view: `code_${state.cptSelected}`,
      grain: "tin", sort: "negotiated_rate", dir: "desc" });
    if ($("#cpt-base-only").checked) p.set("modifier", "base");
    if (cptState.value.trim()) p.set("state", cptState.value.trim().toUpperCase());
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
  renderPayerRank($("#cpt-payer-rank"), d.payer_rank, d.mpfs_rate);
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

// Payer leaderboard for one code: who pays best, with spread + confidence.
function renderPayerRank(el, rows, mpfsRate) {
  if (!el) return;
  if (!rows || !rows.length) { el.innerHTML = ""; return; }
  const mp = rows.some((r) => r.pct_medicare != null);
  const body = rows.map((r) => {
    const thin = (r.n_entities || 0) < 5;
    return `<tr>
      <td>${esc(r.payer)}</td>
      <td class="num"><span class="rate">${money(r.median_rate)}</span></td>
      <td class="num muted">${money(r.p25)}–${money(r.p75)}</td>
      ${mp ? `<td class="num ${r.pct_medicare != null && r.pct_medicare < 100 ? "warn-text" : ""}">${r.pct_medicare != null ? r.pct_medicare + "%" : "–"}</td>` : ""}
      <td class="num ${thin ? "muted" : ""}" title="${thin ? "thin sample — read with caution" : ""}">${fmtInt(r.n_entities)}${thin ? " ⚠" : ""}</td>
    </tr>`;
  }).join("");
  el.innerHTML = `<div class="ov-block"><h3>Which payer pays best for this code${mpfsRate ? ` · Medicare $${fmtMoney(mpfsRate)}` : ""}</h3>
    <div class="tablewrap" style="max-height:34vh"><table>
      <thead><tr><th>Payer</th><th class="num">Median</th><th class="num">P25–P75</th>${mp ? `<th class="num">% of Medicare</th>` : ""}<th class="num">Practices</th></tr></thead>
      <tbody>${body}</tbody></table></div>
    <div class="muted" style="margin-top:4px">One row per payer — the median across the distinct practices it prices for this code. ⚠ marks a thin sample (&lt;5 practices).</div></div>`;
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
  let seq = 0;  // a slow OLD response must not overwrite a newer query's options
  input.addEventListener("input", debounce(async () => {
    const mine = ++seq;
    try {
      const data = await api(`/api/benchmark/subjects?q=${encodeURIComponent(input.value.trim())}`);
      if (mine !== seq) return;  // stale — a newer request already resolved
      const dl = $(datalistSel); if (dl) dl.innerHTML = subjectOptionsHtml(data);
    } catch { /* keep the current options on a transient failure */ }
  }, 200));
}

async function refreshSubjectPickers(subjSel, monthSels = []) {
  // independent fetches: one failing must not freeze the other's pickers
  const [subjects, months] = await Promise.all([
    api("/api/benchmark/subjects").catch(() => null),
    api("/api/months").catch(() => null),
  ]);
  if (subjSel && subjects) $(subjSel).innerHTML = subjectOptionsHtml(subjects);
  for (const ms of monthSels) {
    const el = $(ms.sel); if (!el) continue;
    // transient /api/months failure: a select that already has a working list
    // (tab re-entry) keeps it — rewriting would drop a pinned month and
    // silently reset the vintage. Only fill when there's nothing usable yet.
    if (!months && el.options.length > 0) continue;
    const cur = el.value;
    fillMonthSelect(ms.sel, months, ms.prefix || "");
    // keep the user's pick across the refresh ("latest" included)
    if (cur && (cur === "latest" || (months?.months || []).includes(cur))) el.value = cur;
  }
}

let benchmarkInit = false;
async function initBenchmark() {
  refreshMpfsStatus();
  if (benchmarkInit) { refreshSubjectPickers("#b-subjects", [{ sel: "#b-month", prefix: LATEST_MONTH_OPT }]); return; }
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
  fillMonthSelect("#b-month", months, LATEST_MONTH_OPT);
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
  $("#b-gaps-run").addEventListener("click", runContractGaps);
}

async function runContractGaps() {
  const el = $("#b-gaps");
  let payload;
  try { payload = benchmarkPayload(); }
  catch (e) { el.innerHTML = `<div class="empty">${esc(e.message)}</div>`; return; }
  el.innerHTML = `<div class="loading">Finding contract gaps</div>`;
  let d;
  try { d = await postJson("/api/contract-gaps", { subject: payload.subject, market: payload.market }); }
  catch (e) { el.innerHTML = `<div class="empty">Couldn't find gaps (${esc(e.message)}).</div>`; return; }
  if (!d.count) {
    el.innerHTML = `<div class="ov-block"><h3>Contract gaps</h3><div class="muted">No codes found that ≥${d.min_peers} peers price and ${esc(d.subject)} doesn't — the subject is priced on everything its peers are (under this market basis).</div></div>`;
    return;
  }
  const rows = d.gaps.map((g) => `<tr>
    <td>${esc(g.billing_code)}</td>
    <td class="sub">${esc(g.description || "")}${g.is_timed ? " <span class='timed-tag'>timed</span>" : ""}</td>
    <td class="num muted">${fmtInt(g.n_peers)}</td>
    <td class="num"><b>${money(g.peer_median)}</b></td>
    <td class="num muted">${money(g.peer_p25)}–${money(g.peer_p75)}</td></tr>`).join("");
  el.innerHTML = `<div class="ov-block"><h3>Contract gaps — codes peers price that ${esc(d.subject)} doesn't (${d.count})</h3>
    <div class="muted" style="margin-bottom:6px">${esc(d.gap_note)}</div>
    <div class="tablewrap"><table>
      <thead><tr><th>Code</th><th>Description</th><th class="num">Peers pricing it</th><th class="num">Peer median</th><th class="num">P25–P75</th></tr></thead>
      <tbody>${rows}</tbody></table></div></div>`;
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
  const month = $("#b-month").value || "latest"; // empty = newest rate per contract
  if (!subject) throw new Error("pick a subject practice first");
  const market = {
    month,
    payers: $$("#b-payer-chips .chip.on").map((c) => c.dataset.payer),
    billing_class: $("#b-class").value,
    target_percentile: +$("#b-target").value,
  };
  if ($("#b-therapy").checked) market.therapy_only = true;
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
  if (!bench || !bench.rows || !bench.rows.length) {
    // a header-only empty table reads as broken; say why there's nothing
    out.innerHTML = `<div class="empty"><h3>No market rates for this subject</h3>` +
      `No peer-priced codes matched this market definition. Widen the filters ` +
      `(payer, discipline, state/city, place of service), or wait for more peer ` +
      `files to finish ingesting.</div>`;
    return;
  }
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
      <td class="num"><span class="rate">${money(r.subject_rate)}</span></td>
      <td class="num">${money(r.p25)}</td>
      <td class="num">${money(r.p50)}</td>
      <td class="num">${money(r.p75)}</td>
      <td class="num">${money(r.target_rate)}</td>
      <td class="num ${r.gap_to_target > 0 ? "warn-text" : ""}">${money(r.gap_to_target)}</td>
      ${mp ? `<td class="num">${r.subject_pct_medicare != null ? r.subject_pct_medicare + "%" : "–"}</td>
              <td class="num">${r.median_pct_medicare != null ? r.median_pct_medicare + "%" : "–"}</td>` : ""}
      <td class="num muted">${fmtInt(r.n_peers)}</td>
      <td>${pstrip(r.subject_percentile)}</td></tr>`).join("");
  const oppHtml = opp ? `
    <div class="opp-band">Annual gross opportunity:
      conservative (p${opp.conservative_percentile}) <b>${money(opp.total_at_conservative)}</b>
      · at target (p${opp.target_percentile}) <b>${money(opp.total_at_target)}</b></div>
    <div class="tablewrap"><table>
      <thead><tr><th>Code</th><th class="num">Annual units</th><th class="num">Your rate</th>
      <th class="num">Target</th><th class="num">Opportunity (conservative)</th><th class="num">Opportunity (target)</th></tr></thead>
      <tbody>${opp.rows.map((o) => `
        <tr><td>${esc(o.billing_code)}</td><td class="num">${fmtInt(o.annual_units)}</td>
        <td class="num">${money(o.subject_rate)}</td><td class="num">${money(o.target_rate)}</td>
        <td class="num">${money(o.opportunity_at_conservative)}</td>
        <td class="num"><b>${money(o.opportunity_at_target)}</b></td></tr>`).join("")}
      </tbody></table></div>
    <div class="bench-note">${esc(opp.assumptions)}</div>` : "";
  out.innerHTML = `
    <h2 style="font-size:15px;margin:0 0 4px">${esc(bench.subject)} vs market — as of ${esc(bench.market.month === "latest" ? "latest available" : bench.market.month)}</h2>
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
/* ---------- Negotiate: one payer, named comparables ---------- */
async function initNegotiate() {
  if (state.ngInited) {
    // re-entry: refresh pickers like every sibling tab — a payer/month/org
    // ingested since the first open must show up without a page reload
    refreshSubjectPickers("#ng-subjects", [{ sel: "#ng-month", prefix: LATEST_MONTH_OPT }]);
    refreshSubjectPickers("#ng-comps", []);
    api("/api/payers").then((p) => {
      const el = $("#ng-payer"); const cur = el.value;
      el.innerHTML = `<option value="">— pick a payer —</option>` +
        (p.payers || []).map((x) => `<option>${esc(x)}</option>`).join("");
      if (cur && (p.payers || []).includes(cur)) el.value = cur;
    }).catch(() => {});
    return;
  }
  state.ngInited = true;
  state.ngComps = [];
  const [subjects, months, payers] = await Promise.all([
    api("/api/benchmark/subjects").catch(() => null),
    api("/api/months").catch(() => null),
    api("/api/payers").catch(() => null),
  ]);
  if (subjects) {           // a failed boot fetch must not brick the tab —
    $("#ng-subjects").innerHTML = subjectOptionsHtml(subjects);  // listeners
    $("#ng-comps").innerHTML = subjectOptionsHtml(subjects);     // below still wire
  }
  fillMonthSelect("#ng-month", months, LATEST_MONTH_OPT);
  $("#ng-payer").innerHTML = `<option value="">— pick a payer —</option>` +
    ((payers && payers.payers) || []).map((p) => `<option>${esc(p)}</option>`).join("");
  wireSubjectSearch("#ng-subject", "#ng-subjects");
  wireSubjectSearch("#ng-comp", "#ng-comps");
  const addComp = () => {
    const v = $("#ng-comp").value.trim();
    if (!v || state.ngComps.includes(v)) return;
    state.ngComps.push(v);
    $("#ng-comp").value = "";
    renderCompChips();
    if (state.lastNegotiatePayload) runNegotiate();   // live column add
  };
  $("#ng-add").addEventListener("click", addComp);
  $("#ng-comp").addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); addComp(); } });
  $("#ng-comp-chips").addEventListener("click", (ev) => {
    const x = ev.target.closest(".chip");
    if (!x) return;
    state.ngComps = state.ngComps.filter((c) => c !== x.dataset.comp);
    renderCompChips();
    if (state.lastNegotiatePayload) runNegotiate();
  });
  $("#ng-run").addEventListener("click", runNegotiate);
  $("#ng-report").addEventListener("click", () => {
    if (!state.lastNegotiatePayload) return;
    const p = { ...state.lastNegotiatePayload,
                market: { ...state.lastNegotiatePayload.market } };
    if (!p.market.state) {
      if (!confirm("No state set — the report will pool every loaded state into one national comparison. Continue?")) return;
      p.market.allow_national = true;  // on the COPY: next click re-confirms
    }
    openReportWith("/api/report/payer-compare", p);
  });
}

function renderCompChips() {
  $("#ng-comp-chips").innerHTML = state.ngComps.map((c) =>
    `<button class="chip on" data-comp="${esc(c)}" title="remove">${esc(c)} ✕</button>`).join("");
}

function negotiatePayload() {
  const subject = $("#ng-subject").value.trim();
  const payer = $("#ng-payer").value;
  const month = $("#ng-month").value || "latest"; // empty = newest rate per contract
  if (!subject) throw new Error("enter your practice (subject)");
  if (!payer) throw new Error("pick the payer you're negotiating with");
  const market = { month, therapy_only: $("#ng-therapy").checked };
  if ($("#ng-state").value.trim()) market.state = $("#ng-state").value.trim().toUpperCase();
  if ($("#ng-disc").value) market.discipline = $("#ng-disc").value;
  return { subject, payer, market, comparables: state.ngComps.slice() };
}

async function runNegotiate() {
  const out = $("#ng-out");
  let payload;
  try { payload = negotiatePayload(); }
  catch (e) { out.innerHTML = `<div class="empty"><h3>${esc(e.message)}</h3></div>`; return; }
  out.innerHTML = `<div class="empty"><h3>Comparing…</h3></div>`;
  state.lastNegotiatePayload = null;
  $("#ng-report").disabled = true;
  let d;
  try { d = await postJson("/api/negotiate/compare", payload); }
  catch (e) { out.innerHTML = `<div class="empty"><h3>Couldn't compare</h3>${esc(e.message)}</div>`; return; }
  state.lastNegotiatePayload = payload;
  $("#ng-report").disabled = false;
  const mny = (v) => (v == null ? "–" : v < 0 ? `-$${fmtMoney(-v)}` : `$${fmtMoney(v)}`);
  const s = d.summary;
  const cards = [
    s.headline_percentile != null
      ? `<div class="stat"><b>p${s.headline_percentile}</b><span>your position among ${esc(d.payer)}'s providers</span></div>` : "",
    `<div class="stat"><b>${s.n_below_median}/${s.n_codes}</b><span>codes below this payer's median</span></div>`,
    s.avg_gap_to_median_pct != null
      ? `<div class="stat"><b>${s.avg_gap_to_median_pct > 0 ? "+" : ""}${s.avg_gap_to_median_pct}%</b><span>avg uplift to reach the median</span></div>` : "",
    s.avg_other_payer_diff_pct != null
      ? `<div class="stat"><b>${s.avg_other_payer_diff_pct > 0 ? "+" : ""}${s.avg_other_payer_diff_pct}%</b><span>your other payers pay this much more</span></div>` : "",
  ].filter(Boolean).join("");
  const compHeads = d.comparables.map((c) => `<th class="num" title="what this payer pays them">${esc(c.name)}</th>`).join("");
  const rows = d.rows.map((r) => {
    const compCells = r.comp_rates.map((v) =>
      `<td class="num${v != null && r.subject_rate != null && v > r.subject_rate ? " warn-text" : ""}">${mny(v)}</td>`).join("");
    return `<tr><td>${esc(r.billing_code)}<div class="sub">${esc(r.description || "")}</div></td>
      <td class="num"><b>${mny(r.subject_rate)}</b></td>
      <td class="num">${mny(r.other_payers_rate)}<div class="sub">${r.n_other_payers || 0} payer(s)</div></td>
      <td class="num">${mny(r.p25)}</td><td class="num">${mny(r.p50)}</td><td class="num">${mny(r.p75)}</td>
      <td class="num">${r.subject_percentile != null ? "p" + Math.round(r.subject_percentile) : "–"}</td>
      <td class="num ${r.gap_to_median > 0 ? "warn-text" : ""}">${mny(r.gap_to_median)}${r.gap_to_median_pct != null ? `<div class="sub">${r.gap_to_median_pct > 0 ? "+" : ""}${r.gap_to_median_pct}%</div>` : ""}</td>
      ${compCells}<td class="num sub">${r.n_peers || 0}</td></tr>`;
  }).join("");
  const compNotes = d.comparables.filter((c) => c.n_shared_codes).map((c) =>
    `<li><b>${esc(c.name)}</b>: paid more than you on ${c.n_paid_more}/${c.n_shared_codes} shared codes` +
    (c.median_premium_pct != null ? ` (median ${c.median_premium_pct > 0 ? "+" : ""}${c.median_premium_pct}% vs your rate)` : "") + `</li>`).join("");
  out.innerHTML = `
    <div class="stats-row">${cards}</div>
    ${compNotes ? `<ul class="note" style="margin:8px 0 12px">${compNotes}</ul>` : ""}
    <div style="overflow-x:auto"><table><thead><tr>
      <th>Code</th><th class="num">You</th><th class="num">Your other payers</th>
      <th class="num">P25</th><th class="num">Median</th><th class="num">P75</th>
      <th class="num" title="the subject's percentile position among this payer's other providers">Position</th><th class="num">Gap to median</th>${compHeads}
      <th class="num">Peers</th></tr></thead><tbody>${rows}</tbody></table></div>
    <p class="note">${esc(d.basis_note)} Peer stats exclude your own TIN(s). Use "Printable report" for the payer-ready document with full methodology.</p>`;
}

async function initRatecard() {
  refreshRcMpfs();
  if (ratecardInit) { refreshSubjectPickers("#rc-subjects", [{ sel: "#rc-month", prefix: LATEST_MONTH_OPT }]); return; }
  ratecardInit = true;
  const [subjects, months, payers] = await Promise.all([
    api("/api/benchmark/subjects").catch(() => null),
    api("/api/months").catch(() => null),
    api("/api/payers").catch(() => null),
  ]);
  if (subjects) $("#rc-subjects").innerHTML = subjectOptionsHtml(subjects);
  wireSubjectSearch("#rc-subject", "#rc-subjects");
  fillMonthSelect("#rc-month", months, LATEST_MONTH_OPT);
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
  const month = $("#rc-month").value || "latest"; // empty = newest rate per contract
  if (!subject) throw new Error("pick a subject practice first");
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
  sc = sc || { rows: [] };  // never assume a scorecard came back — guard the forEach
  if (!fs.codes.length) {
    out.innerHTML = `<div class="empty"><h3>No rates found</h3>No published rates for this practice as of ${esc(fs.month === "latest" ? "latest available" : String(fs.month))} under the current filters.</div>`;
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
  if (leadsInit) { refreshSubjectPickers("#ld-subjects", [{ sel: "#ld-month", prefix: LATEST_MONTH_OPT }]); loadLdStates(); return; }
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
  fillMonthSelect("#ld-month", months, LATEST_MONTH_OPT);
  if (payers) $("#ld-payer-chips").innerHTML = payers.payers.map((p) => `<button class="chip" data-payer="${esc(p)}">${esc(p)}</button>`).join("");
  $("#ld-payer-chips").addEventListener("click", (ev) => { const b = ev.target.closest(".chip"); if (b) b.classList.toggle("on"); });
  if (states) $("#ld-states").innerHTML = (states.states || []).map((s) => `<option value="${esc(s)}">`).join("");
  if (subjects) $("#ld-subjects").innerHTML = subjectOptionsHtml(subjects);
  wireSubjectSearch("#ld-exclude", "#ld-subjects");
  $("#ld-run").addEventListener("click", runLeads);
  $("#ld-csv").addEventListener("click", () => { if (state.lastLeadsPayload) postDownload("/api/leads.csv", state.lastLeadsPayload, "leads.csv"); });
  $("#ld-board-run").addEventListener("click", runLeaderboard);
}

async function runLeaderboard() {
  const el = $("#ld-board");
  const payload = leadsPayload();  // reuse the same market scope
  const sort = $("#ld-board-sort").value;
  el.innerHTML = `<div class="loading">Building market leaderboard</div>`;
  let d;
  try {
    d = await postJson("/api/leaderboard", {
      market: payload.market, min_codes: payload.min_codes, sort, limit: 50 });
  } catch (e) { el.innerHTML = `<div class="empty">Couldn't build the leaderboard (${esc(e.message)}).</div>`; return; }
  if (!d.count) {
    el.innerHTML = `<div class="ov-block"><h3>Market leaderboard</h3><div class="muted">No practices price at least ${d.min_codes} codes with a real market under this scope.</div></div>`;
    return;
  }
  const label = { size: "biggest (by NPIs)", paid: "best-paid", footprint: "widest footprint" }[d.sort] || d.sort;
  const rows = d.practices.map((p, i) => `<tr>
    <td class="num muted">${i + 1}</td>
    <td>${esc(p.display_name || "")}<div class="sub">${esc(p.entity_kind || "")}${p.website ? ` · <a href="${esc(p.website)}" target="_blank" rel="noopener">site</a>` : ""}</div></td>
    <td class="sub">${esc([p.city, p.state].filter(Boolean).join(", "))}${p.multi_state ? ` <span class="muted" title="${esc((p.states || []).join(", "))}">(+${(p.states || []).length - 1})</span>` : ""}</td>
    <td class="num">${fmtInt(p.npi_count)}</td>
    <td class="num">${fmtInt(p.n_codes)}</td>
    <td class="num">p${p.median_percentile}</td>
    <td class="num">${money(p.median_rate)}</td></tr>`).join("");
  el.innerHTML = `<div class="ov-block"><h3>Market leaderboard — ${esc(label)} (${d.count})</h3>
    <div class="muted" style="margin-bottom:6px">${esc(d.note)}</div>
    <div class="tablewrap"><table>
      <thead><tr><th>#</th><th>Practice</th><th>Location</th><th class="num">NPIs</th><th class="num">Codes</th><th class="num">Position</th><th class="num">Median rate</th></tr></thead>
      <tbody>${rows}</tbody></table></div></div>`;
}

// Populate a month <select> from an /api/months payload, tolerating a failed
// fetch (null) or a store with no dated months. With a Latest prefix (the
// report tabs), "Latest available" is ALWAYS the first option and the default —
// it needs no month list, so the tab stays fully usable even when /api/months
// failed or nothing dated is ingested yet; the hint rides along as a
// secondary, non-default option. Without a prefix (the Changes tab, which
// needs real months), the old actionable-hint behavior stands.
function fillMonthSelect(sel, months, prefix = "") {
  const el = $(sel);
  if (!el) return;
  const opts = months
    ? (months.months || []).filter(Boolean).map((m) => `<option>${esc(m)}</option>`).join("")
    : "";
  const note = !months
    ? `<option value="" disabled>couldn't load months — reopen this tab</option>`
    : `<option value="" disabled>no dated data yet — ingest a file first</option>`;
  if (prefix) {
    el.innerHTML = prefix + (opts || note);
  } else {
    el.innerHTML = opts || note.replace(" disabled>", ">");
  }
}

async function loadLdStates() {
  try {
    const { states } = await api("/api/states");
    $("#ld-states").innerHTML = (states || []).map((s) => `<option value="${esc(s)}">`).join("");
  } catch { /* keep existing list */ }
}

function leadsPayload() {
  const month = $("#ld-month").value || "latest"; // empty = newest rate per contract
  const market = { month, payers: $$("#ld-payer-chips .chip.on").map((c) => c.dataset.payer) };
  if ($("#ld-state").value.trim()) market.state = $("#ld-state").value.trim();
  if ($("#ld-disc").value) market.discipline = $("#ld-disc").value;
  if ($("#ld-therapy").checked) market.therapy_only = true;
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
    const therapyOn = $("#ld-therapy") && $("#ld-therapy").checked;
    out.innerHTML = `<div class="empty"><h3>No leads found</h3>No practices at or below p${data.threshold_percentile} that price at least ${data.min_codes} codes with a market. Widen the cutoff or lower the min codes.${therapyOn ? ` <br><span class="muted">"Therapy practices only" is on — it keeps practices whose identified providers are MOSTLY PT/OT/SLP and excludes hospital systems and physician groups (and practices not yet identified). Uncheck it, or let NPI identification finish, to see more.</span>` : ""}</div>`;
    return;
  }
  const rows = data.leads.map((l) => `<tr>
    <td>${esc(l.display_name || "")}<div class="sub">${esc(l.entity_kind || "")}${l.website ? ` · <a href="${esc(l.website)}" target="_blank" rel="noopener">site</a>` : ""}</div></td>
    <td class="sub">${esc([l.city, l.state].filter(Boolean).join(", "))}${l.multi_state ? ` <span class="muted" title="${esc((l.states || []).join(", "))}">(+${(l.states || []).length - 1} more state${(l.states || []).length - 1 === 1 ? "" : "s"})</span>` : ""}</td>
    <td class="num">${fmtInt(l.npi_count)}</td>
    <td class="num">${l.n_codes}</td>
    <td class="num">p${l.median_percentile}</td>
    <td class="num">${money(l.avg_gap_to_median)}</td>
    <td>${l.worst_payer ? `${esc(l.worst_payer)}<div class="sub">${money(l.worst_payer_gap)}/code under mkt</div>` : "<span class='muted'>–</span>"}</td>
    <td class="sub">${esc(l.tin_value)}</td></tr>`).join("");
  out.innerHTML = `<div class="rc-summary">${data.count} practice(s) at or below p${data.threshold_percentile}, most underpaid first. <span class="muted">"Underpaid by" is the payer with the biggest average shortfall vs the market median — your outreach hook. Avg $ below median is a rate-level gap, not annual dollars.</span></div>
    <div class="tablewrap"><table class="rc-table"><thead><tr><th>Practice</th><th>Location</th><th class="num">Providers</th><th class="num">Codes</th><th class="num">Position</th><th class="num">Avg $ below median</th><th>Underpaid most by</th><th>Tax ID</th></tr></thead><tbody>${rows}</tbody></table></div>`;
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
  $("#ch-traj-run").addEventListener("click", runTrajectory);
}

async function runTrajectory() {
  const el = $("#ch-traj");
  // trajectory spans all months, so it ignores the two-month picker; it reuses
  // only the payer/discipline scope from the changes market
  const market = { payers: $$("#ch-payer-chips .chip.on").map((c) => c.dataset.payer) };
  if ($("#ch-disc").value) market.discipline = $("#ch-disc").value;
  el.innerHTML = `<div class="loading">Tracing payer trajectories across all months</div>`;
  let d;
  try { d = await postJson("/api/trajectory", { market }); }
  catch (e) { el.innerHTML = `<div class="empty">Couldn't build the trajectory (${esc(e.message)}).</div>`; return; }
  if (!d.count) {
    el.innerHTML = `<div class="ov-block"><h3>Payer trajectory</h3><div class="muted">No payer has ≥2 months of data under this scope. Re-ingest the payers' newer MRFs (a later file month).</div></div>`;
    return;
  }
  const arrow = { down: "▼", up: "▲", flat: "▬" };
  const rows = d.payers.map((p) => {
    const cls = p.direction === "down" ? "chg-cut" : p.direction === "up" ? "chg-up" : "";
    const pct = p.cumulative_pct == null ? "–" : (p.cumulative_pct > 0 ? "+" : "") + p.cumulative_pct + "%";
    return `<tr>
      <td>${esc(p.payer)}</td>
      <td class="num muted">${esc(p.first_month)}→${esc(p.last_month)} (${p.n_months})</td>
      <td class="num">${money(p.first_rate)} → <b>${money(p.last_rate)}</b></td>
      <td class="num ${cls}">${arrow[p.direction] || ""} ${pct}</td>
      <td class="num muted">${p.cut_streak || 0}</td></tr>`;
  }).join("");
  el.innerHTML = `<div class="ov-block"><h3>Payer rate trajectory — all ${d.months.length} loaded months</h3>
    <div class="muted" style="margin-bottom:6px">${esc(d.note)} Most-eroding first.</div>
    <div class="tablewrap"><table>
      <thead><tr><th>Payer</th><th class="num">Span</th><th class="num">First → last median</th><th class="num">Cumulative</th><th class="num">Cut streak</th></tr></thead>
      <tbody>${rows}</tbody></table></div></div>`;
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
  // by-payer direction: is a payer systematically cutting across the market?
  const bp = (d.by_payer || []).filter((p) => p.n >= 2);
  const byPayer = bp.length
    ? `<div class="ov-block"><h3>By payer — is a payer cutting across the market?</h3>
       <div class="tablewrap" style="max-height:30vh"><table class="rc-table">
        <thead><tr><th>Payer</th><th class="num">Median move</th><th class="num">Cuts</th><th class="num">Increases</th><th class="num">Lines</th></tr></thead>
        <tbody>${bp.map((p) => `<tr><td>${esc(p.payer)}</td>
          <td class="num ${p.median_pct_change < 0 ? "chg-cut" : p.median_pct_change > 0 ? "chg-up" : ""}">${p.median_pct_change > 0 ? "+" : ""}${p.median_pct_change}%</td>
          <td class="num">${fmtInt(p.n_cuts)}</td><td class="num">${fmtInt(p.n_increases)}</td>
          <td class="num muted">${fmtInt(p.n)}</td></tr>`).join("")}</tbody></table></div>
       <div class="muted" style="margin-top:4px">Median % move across each payer's changed contract lines (≥2 lines), most-cutting first.</div></div>`
    : "";
  const capNote = d.truncated
    ? ` <span class="warn-text">Showing the ${fmtInt(d.cap)} largest moves — the cut/increase counts and by-payer stats cover only these. Narrow the market (payer/state/discipline) to see the rest.</span>`
    : "";
  out.innerHTML = `<div class="rc-summary"><b>${d.n_cuts}</b> cut(s), <b>${d.n_increases}</b> increase(s) from ${esc(d.prev_month)} → ${esc(d.new_month)}.${d.biggest_cut_pct != null ? ` Biggest cut ${d.biggest_cut_pct}%.` : ""}${capNote}</div>
    ${byPayer}
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
  canceled: "cleared by you",
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

  $("#url-clear-queued").addEventListener("click", async () => {
    const msg = $("#url-msg");
    if (!confirm("Cancel everything WAITING to download?\n\nFiles already " +
      "downloaded or being processed are untouched, and cleared links become " +
      "'skipped' so you can retry them later. This lets ingesting catch up.")) return;
    msg.textContent = "clearing queue…";
    try {
      const r = await postJson("/api/urls/clear-queued", {});
      msg.textContent = `cleared ${r.cleared} waiting link${r.cleared === 1 ? "" : "s"}`;
    } catch (e) { msg.textContent = "could not clear: " + e.message; }
    loadUrlQueue();
  });

  $("#url-stop-dl").addEventListener("click", async () => {
    const msg = $("#url-msg");
    if (!confirm("Stop the downloads in progress right now?\n\nPartial files " +
      "are kept, so a retry resumes them. The freed slots let ingesting catch up.")) return;
    msg.textContent = "stopping downloads…";
    try {
      const r = await postJson("/api/urls/stop-downloads", {});
      msg.textContent = `stopped ${r.stopped} download${r.stopped === 1 ? "" : "s"}`;
    } catch (e) { msg.textContent = "could not stop: " + e.message; }
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
    const expected = $("#v-expected").value.trim();
    if (!id || !code) { alert("TIN/NPI and code required"); return; }
    // "$85.00" → NaN → JSON null → the server treats it as "no expected rate"
    // and shows a neutral verdict — the user believes their check ran. Catch it.
    if (expected && isNaN(+expected)) {
      alert("expected rate must be a plain number, like 85.00 (no $ or commas)");
      return;
    }
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
