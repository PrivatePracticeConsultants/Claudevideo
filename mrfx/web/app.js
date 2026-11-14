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

// Below this many peer practices a peer median is directional only. Must track
// schedule._THIN_PEERS — the printable report flags the same cells, and screen
// and report must not disagree about which numbers carry a caveat.
const THIN_PEERS = 5;

// How long a download's byte counter may sit unchanged before we tell the user
// the payer's server has gone quiet. Comfortably longer than a normal poll gap
// so ordinary jitter never trips it.
const STALL_HINT_SECONDS = 25;
// url id -> {bytes, since}. Purely a display aid computed in the browser; the
// server is not asked to track this and the download is untouched either way.
const _stallSeen = new Map();

function stalledFor(id, bytes) {
  const now = Date.now();
  const prev = _stallSeen.get(id);
  if (!prev || prev.bytes !== bytes) {
    _stallSeen.set(id, { bytes, since: now });
    return 0;
  }
  return (now - prev.since) / 1000;
}

const state = {
  view: "explorer",
  grain: "tin",
  filters: {
    payers: [], cpts: [], disciplines: [], modifier: "", mod_has: "", mod_not: "",
    billing_class: "", pos: "", state: "", city: "", month: "latest", q: "",
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

// cents the way the server (rounding.half_up) and DuckDB round: half away from
// zero on the DOUBLE x*100. toLocaleString rounds the shortest decimal string
// instead — 70.695 printed $70.70 here and $70.69 on Markets, the same median
// one cent apart (review 10 item 3.2)
const roundCents = (x) => Math.sign(x) * Math.round(Math.abs(x) * 100) / 100;
const fmtMoney = (v) =>
  v == null ? "–" : roundCents(Number(v)).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
// dollar-prefixed money that degrades to a bare en-dash for null — a plain
// `"$" + fmtMoney(v)` rendered "$–" for a missing value (a code with no peers,
// an unfilled opportunity cell), which reads as a glitch. Negatives render as
// "-$50.00" (not "$-50.00") — the benchmark Gap column is negative whenever the
// subject is priced above target, and the reports / Negotiate tab already do
// this. Use for any money cell that can be empty and/or negative.
const money = (v) =>
  v == null ? "–" : v < 0 ? "-$" + fmtMoney(-v) : "$" + fmtMoney(v);
const fmtInt = (v) => (v == null ? "–" : Number(v).toLocaleString("en-US"));
// stored times are UTC ("2026-11-06 04:30:00.123"); show them on this PC's clock
const localTime = (s) => {
  if (!s) return "";
  const d = new Date(String(s).slice(0, 19).replace(" ", "T") + "Z");
  return isNaN(d) ? String(s).slice(0, 19) : d.toLocaleString();
};
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

/* ---------- progress bars (review 13 Group 2) ----------
   Every output keeps its request; with a bar it carries a per-run token
   (X-Mrfx-Progress, or ?progress= on an iframe download) and the page polls
   GET /api/progress/<token> while the request is pending. The percentage is
   the SERVER's (stage weights + DuckDB's own progress inside a stage) — the
   page never advances it on a clock; 100 % only once the result is in hand. */
const newToken = () => {
  const r = (globalThis.crypto && crypto.randomUUID) ? crypto.randomUUID()
    : Math.random().toString(36).slice(2) + Date.now().toString(36) + Math.random().toString(36).slice(2);
  return r.replace(/-/g, "").slice(0, 32).padEnd(16, "0");
};
function jobHost(bar) {
  const id = `jobbar-${bar.view}`;
  let host = document.getElementById(id);
  if (host) return host;
  host = document.createElement("div");
  host.className = "jobbar-host";
  host.id = id;
  const a = bar.anchor ? (typeof bar.anchor === "string" ? document.querySelector(bar.anchor) : bar.anchor) : null;
  const row = a ? a.parentElement : null;
  if (row && row.parentElement) row.parentElement.insertBefore(host, row.nextSibling);
  else (document.getElementById(`view-${bar.view}`) || document.querySelector("main") || document.body).prepend(host);
  return host;
}
const _btnRuns = new WeakMap();
function mountJobbar(bar, token) {
  const host = jobHost(bar);
  const el = document.createElement("div");
  el.className = "jobbar";
  el.innerHTML = `<div class="progress"><div class="progress-fill" style="width:0%"></div>` +
    `<div class="progress-label"></div></div><a href="#" class="jobbar-stop" title="stop this run — nothing half-built is delivered">Stop</a>`;
  host.appendChild(el);
  const fill = el.querySelector(".progress-fill"), lab = el.querySelector(".progress-label");
  lab.title = "weighted by how long each step typically takes on a store this size; the bar moves as DuckDB reports its progress";
  const btn = bar.anchor ? (typeof bar.anchor === "string" ? document.querySelector(bar.anchor) : bar.anchor) : null;
  // runs are COUNTED per button (review 14 item 2.10): a second run started
  // while the first was in flight (Enter twice) recorded "disabled" as the
  // state to restore, and the button stayed dead until a reload
  if (btn) {
    const rec = _btnRuns.get(btn) || { n: 0, was: btn.disabled };
    rec.n += 1;
    _btnRuns.set(btn, rec);
    btn.disabled = true;
  }
  let pct = 0, ended = false;
  // a cached answer arrives in a blink: no flicker under 250 ms
  const showTimer = setTimeout(() => el.classList.add("on"), 250);
  const say = (t) => { lab.textContent = t; if (bar.tab) { try { bar.tab.document.body.textContent = t; } catch (_) { /* cosmetic */ } } };
  say(`${bar.label} · starting…`);
  el.querySelector(".jobbar-stop").onclick = (ev) => {
    ev.preventDefault();
    if (ended) return;
    fetch(`/api/progress/${token}`, { method: "DELETE" }).catch(() => {});
    say(`${bar.label} · stopping…`);
  };
  const finish = (cls, text, keepMs) => {
    if (ended) return;
    ended = true;
    clearTimeout(showTimer);
    if (btn) {
      const rec = _btnRuns.get(btn);
      if (rec) {
        rec.n -= 1;
        if (rec.n <= 0) { btn.disabled = rec.was; _btnRuns.delete(btn); }
      } else btn.disabled = false;
    }
    el.classList.add(cls);
    if (text) say(text);
    if (cls === "done") { fill.style.width = "100%"; el.classList.add("on"); }
    setTimeout(() => el.remove(), keepMs);
  };
  return {
    update(s) {
      if (ended || !s) return;
      pct = Math.max(pct, Math.min(99, Number(s.pct) || 0));    // never backwards, never 100 here
      fill.style.width = pct + "%";
      el.classList.toggle("indeterminate", !!s.indeterminate);
      const where = s.stage_count ? ` (${s.stage_index} of ${s.stage_count})` : "";
      if (s.state === "waiting") say(`${bar.label} · ${s.stage || "waiting"} · ${Math.round((s.elapsed_ms || 0) / 1000)} s`);
      else say(`${bar.label} · ${s.stage || "working"}${where} · ${s.indeterminate ? "at least " : "about "}${pct} %`);
    },
    built() { finish("done", `${bar.label} · built — your browser is downloading it`, 4000); },
    done() { finish("done", `${bar.label} · done`, bar.keepMs || 600); },
    stopped() { finish("stopped", `${bar.label} · stopped`, 2500); },
    failed(msg) { finish("failed", `${bar.label} · ${msg || "failed"}`, 5000); },
  };
}
function startPoll(token, jb, bar, onTerminal) {
  let stopped = false, timer = null, seenOk = false, misses = 0;
  const tick = async () => {
    if (stopped) return;
    try {
      const r = await fetch(`/api/progress/${token}`);
      if (r.ok) {
        seenOk = true; misses = 0;
        const s = await r.json();
        jb.update(s);
        if (onTerminal && ["done", "failed", "stopped"].includes(s.state)) { onTerminal(s); return; }
      } else if (r.status === 404 && (seenOk || ++misses >= 25)) {
        // the server no longer knows this run: it restarted (review 14
        // Group 3 — an iframe download's bar stuck and Export stayed
        // disabled for 6 h). The bar ends and the button comes back.
        jb.failed("the app restarted — run it again");
        if (onTerminal) onTerminal({ state: "failed", error: "the app restarted — run it again" });
        return;
      }
    } catch (_) { /* the request's own outcome decides */ }
    const slow = document.hidden || (bar.view && state.view && bar.view !== state.view);
    timer = setTimeout(tick, slow ? 1500 : 400);
  };
  timer = setTimeout(tick, 400);
  return { stop() { stopped = true; clearTimeout(timer); } };
}
// run `work(opts)` (a fetch carrying the token in opts.headers) with a bar
async function withBar(bar, work) {
  const token = newToken();
  const jb = mountJobbar(bar, token);
  const poll = startPoll(token, jb, bar);
  try {
    const res = await work({ "X-Mrfx-Progress": token });
    poll.stop();
    jb.done();
    return res;
  } catch (e) {
    poll.stop();
    if (e && e.status === 409) jb.stopped();
    else if (e && e.name === "AbortError") jb.stopped();
    else jb.failed(e && e.message && /Failed to fetch|NetworkError/.test(e.message)
      ? "the server restarted — run it again" : (e && e.message) || "failed");
    throw e;
  }
}

async function api(path, opts) {
  if (opts && opts.bar) {
    const { bar, ...rest } = opts;
    return withBar(bar, (h) => api(path, { ...rest, headers: { ...(rest.headers || {}), ...h } }));
  }
  const r = await fetch(path, opts);
  if (!r.ok) {
    let msg = `HTTP ${r.status}`;
    try { const d = await r.json(); msg = d.detail || d.error || msg; } catch { /* keep */ }
    const err = new Error(msg);
    err.status = r.status;          // "busy" (503) is told apart from a refusal (422)
    throw err;
  }
  return r.json();
}

// One ticket per view: a slow answer to an OLDER request must never paint over
// the newer one (change a filter twice quickly and the first reply can land
// last). `const fresh = ticket("rates")` before the fetch; `if (!fresh()) return`
// after every await.
const _tickets = {};
function ticket(view) {
  const n = (_tickets[view] = (_tickets[view] || 0) + 1);
  return () => _tickets[view] === n;
}
// one in-flight request per view (review 10 1.1). A newer one ABORTS the older in the
// browser and tells the server which page view it belongs to, so the server
// interrupts the superseded scan instead of finishing it for nobody.
const VIEW_ID = Math.random().toString(36).slice(2) + Date.now().toString(36);
const _viewCtl = {};
function supersede(view) {
  if (_viewCtl[view]) _viewCtl[view].abort();
  const c = (_viewCtl[view] = new AbortController());
  return { signal: c.signal, headers: { "X-Mrfx-View": `${VIEW_ID}:${view}` } };
}
const postJson = (path, body, bar) =>
  api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
              ...(bar ? { bar } : {}) });

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
// Two menu entries each gather screens that answer one question (0.8.6):
// "My rates" (your rate card; how you sit against the market) and "Market
// rates" (browse every rate; one code; a code's market; a payer's clinics).
// Each member keeps its own screen; a tab strip moves between them.
const VIEW_GROUPS = {
  myrates: { label: "My rates", views: [["ratecard", "Rate card & visit value"], ["benchmark", "Benchmark vs the market"]] },
  market: { label: "Market rates", views: [["explorer", "Browse rates"], ["cpt", "By code"],
                                           ["markets", "A code's market"], ["roster", "A payer's clinics"]] },
};
const groupOf = (view) => Object.keys(VIEW_GROUPS).find((g) => VIEW_GROUPS[g].views.some(([v]) => v === view));
function renderSubtabs(view) {
  $$(".subtabs").forEach((el) => el.remove());
  const g = groupOf(view);
  if (!g) return;
  const sec = $(`#view-${view}`);
  if (!sec) return;
  const bar = document.createElement("div");
  bar.className = "subtabs";
  bar.innerHTML = VIEW_GROUPS[g].views.map(([v, label]) =>
    `<button data-subview="${v}" class="${v === view ? "active" : ""}">${esc(label)}</button>`).join("");
  bar.addEventListener("click", (e) => {
    const b = e.target.closest("[data-subview]");
    if (b && b.dataset.subview !== state.view) switchView(b.dataset.subview);
  });
  sec.prepend(bar);
}
$$("nav button").forEach((b) => b.addEventListener("click", () => {
  // a group's entry reopens the member last used
  const g = b.dataset.group;
  switchView(g && state.lastInGroup && state.lastInGroup[g] ? state.lastInGroup[g] : b.dataset.view);
}));
renderSubtabs(state.view);   // the screen the page opens on (no switchView ran)
function switchView(view) {
  state.view = view;
  // the address bar follows the screen (review 13 6.4); a link's own
  // subject/state stay while that link is being opened
  if (!_openingHash && parseHash().view !== view) setHash(view, undefined, true);
  const g = groupOf(view);
  if (g) (state.lastInGroup = state.lastInGroup || {})[g] = view;
  $$("nav button").forEach((b) => b.classList.toggle("active",
    b.dataset.view === view || (g && b.dataset.group === g)));
  $$(".view").forEach((v) => v.classList.toggle("active", v.id === `view-${view}`));
  renderSubtabs(view);
  const active = $$("nav button").find((b) => b.dataset.view === view || (g && b.dataset.group === g));
  const tt = $("#topbar-title");
  if (active && tt) {
    const member = g && VIEW_GROUPS[g].views.find(([v]) => v === view);
    tt.textContent = member ? `${VIEW_GROUPS[g].label} — ${member[1]}` : active.textContent;
  }
  clearInterval(state.filesTimer);
  if (view !== "files") stopAnalyticsPoll();
  // the tracker-import poll belongs to the Medicare tab; leaving it stops it
  if (view !== "medicare" && state.trackerPoll) {
    clearInterval(state.trackerPoll); state.trackerPoll = null;
  }
  if (view !== "clients" && state.packetPoll) {
    clearInterval(state.packetPoll); state.packetPoll = null;
  }
  if (view === "files") {
    loadFiles(); loadUrlQueue();
    state.filesTimer = setInterval(() => { loadFiles(); loadUrlQueue(); }, 4000);
  }
  if (view === "cpt" && !state.cptSelected) {
    // open on the first code that HAS rates (the summary strip's per-code
    // rows), not the catalog's first code — 90912 is priced in almost no
    // store, so the page opened empty (review 13 item 3.5)
    const withData = (state.lastByCode || []).map((r) => r.billing_code).filter((c) => state.catalog[c]);
    const first = withData.sort()[0] || Object.keys(state.catalog)[0];
    if (first) selectCpt(first);
  }
  // data landed while this view was closed: reload it now (P1.20)
  if (view === "explorer" || view === "cpt") reloadIfStale(view);
  if (view === "markets") initMarkets();
  if (view === "roster") initRoster();
  if (view === "benchmark") initBenchmark();
  if (view === "negotiate") initNegotiate();
  if (view === "ratecard") initRatecard();
  if (view === "medicare") initMedicare();
  if (view === "clients") initClients();
  if (view === "leads") initLeads();
  if (view === "changes") initChanges();
  // re-entry: keep whatever state was picked (else the cards blank out while the
  // dropdown still shows the state, and re-picking the same option fires no change)
  if (view === "sources") loadSources($("#src-state")?.value || "");
  if (view === "overview") initOverview();
}

/* ---------- bookmarkable links + the header search box (review 13 Group 6.4) ---------- */
// The address bar names the screen and, on the client screens, the practice
// and state: #benchmark?subject=ACME%20REHAB&state=MO opens the Benchmark
// with both filled and run — bookmark it, or send it to yourself. No new
// numbers: the link only fills the same boxes the user would.
const DEEP = {
  benchmark: { subject: "#b-subject", state: "#b-state", ready: "#b-month", run: () => runBenchmark() },
  negotiate: { subject: "#ng-subject", state: "#ng-state", payer: "#ng-payer", ready: "#ng-month", run: () => runNegotiate() },
  ratecard: { subject: "#rc-subject", state: "#rc-state", ready: "#rc-month", run: () => buildRatecard() },
  changes: { subject: "#ch-subject", ready: "#ch-month", run: () => runChanges() },
  medicare: { subject: "#md-subject", run: () => runMedicareOrg() },
};
function viewExists(v) { return !!groupOf(v) || $$("nav button").some((b) => b.dataset.view === v); }
function parseHash(h) {
  const raw = (h == null ? location.hash : h).replace(/^#/, "");
  const i = raw.indexOf("?");
  const view = decodeURIComponent(i < 0 ? raw : raw.slice(0, i));
  const params = {};
  new URLSearchParams(i < 0 ? "" : raw.slice(i + 1)).forEach((v, k) => { params[k] = v; });
  return { view, params };
}
// an SSN-pattern tax ID never goes into the address bar (review 14 item 2.5):
// the link is made to be bookmarked and sent
const ssnShaped = (v) => {
  const d = String(v ?? "").replace(/[\s-]/g, "");
  return /^[0-9]{9}$/.test(d) && SSN_PREFIXES.has(d.slice(0, 2));
};
function hashFor(view, params) {
  const q = new URLSearchParams();
  for (const [k, v] of Object.entries(params || {})) if (v && !ssnShaped(v)) q.set(k, v);
  const qs = q.toString();
  return "#" + view + (qs ? "?" + qs : "");
}
function setHash(view, params, push) {
  const h = hashFor(view, params);
  // a tab switch the user made is a history entry, so Back returns to the
  // previous tab instead of leaving the app (review 14 Group 3, P3); filling
  // a screen's boxes replaces its own entry
  if (location.hash !== h) (push ? history.pushState : history.replaceState).call(history, null, "", h);
}
// the screen's current boxes -> the address bar (on Run)
function hashFromInputs(view) {
  const d = DEEP[view];
  if (!d) { setHash(view); return; }
  const val = (sel) => (sel && $(sel) ? String($(sel).value || "").trim() : "");
  setHash(view, { subject: val(d.subject), state: val(d.state).toUpperCase(), payer: val(d.payer) });
}
let _openingHash = false;
async function openFromHash() {
  const { view, params } = parseHash();
  if (view === "analytics") { openAnalytics(); return; }
  if (!view || !viewExists(view)) return;
  _openingHash = true;
  try {
    switchView(view);
    const d = DEEP[view];
    if (!d || !params.subject) return;
    for (let i = 0; i < 40; i++) {           // the tab's pickers load on first visit
      const ok = (!d.ready || ($(d.ready) && $(d.ready).options.length > 0)) &&
                 (!d.payer || !params.payer || ($(d.payer) && $(d.payer).options.length > 0));
      if (ok) break;
      await new Promise((r) => setTimeout(r, 150));
    }
    if (d.state && params.state && $(d.state)) $(d.state).value = params.state;
    if (d.payer && params.payer && $(d.payer)) $(d.payer).value = params.payer;
    $(d.subject).value = params.subject;
    d.run();
  } finally { _openingHash = false; }
}
function initDeepLinks() {
  // the Run buttons write the link (the jump-throughs do it in clientJump)
  [["#b-run", "benchmark"], ["#ng-run", "negotiate"], ["#rc-run", "ratecard"]].forEach(([sel, v]) => {
    const b = $(sel);
    if (b) b.addEventListener("click", () => hashFromInputs(v));
  });
  // Back / Forward fire popstate (and hashchange); a pasted link fires
  // hashchange — follow each new address once
  const follow = () => {
    const { view } = parseHash();
    if (location.hash === state.lastFollowed) return;
    state.lastFollowed = location.hash;
    if (!_openingHash && view && (viewExists(view) || view === "analytics")) openFromHash();
  };
  window.addEventListener("hashchange", follow);
  window.addEventListener("popstate", follow);
}

// "Find a practice": a TIN, an NPI or a name -> the practice's drawer
async function findPractice(raw) {
  const v = String(raw || "").trim();
  const msg = $("#find-msg");
  if (msg) msg.textContent = "";
  if (!v) return;
  const digits = v.replace(/[\s-]/g, "");
  if (/^\d{9}$/.test(digits)) return openEntity("tin", digits);
  if (/^\d{10}$/.test(digits)) return openEntity("npi", digits);
  let d;
  try { d = await api(`/api/benchmark/subjects?q=${encodeURIComponent(v)}&limit=50`); }
  catch (e) { if (msg) msg.textContent = e.message; return; }
  const lc = v.toLowerCase();
  const ents = d.entities || [], tins = d.tins || [];
  const ent = ents.find((e) => e.toLowerCase() === lc);
  if (ent) return openEntity("entity", ent);
  const exact = tins.filter((t) => (t.display_name || "").toLowerCase() === lc);
  if (exact.length === 1) return openEntity("tin", exact[0].tin_value);
  if (exact.length > 1) return openEntity("entity", exact[0].display_name);
  const n = ents.length + tins.length;
  if (n === 1) return ents.length ? openEntity("entity", ents[0]) : openEntity("tin", tins[0].tin_value);
  if (msg) msg.textContent = n ? `${n}${n >= 50 ? "+" : ""} practices match — pick one from the list` : "no practice matches";
}
function initFindBox() {
  const input = $("#find-practice");
  if (!input) return;
  wireSubjectSearch("#find-practice", "#find-list");
  input.addEventListener("keydown", (ev) => { if (ev.key === "Enter") { ev.preventDefault(); findPractice(input.value); } });
}

/* ---------- header stats ---------- */
// Review 13 item 1.2: the counts above are what was LOADED; every screen reads
// the analytics, which can be behind (a billion-row rollup takes hours). When
// they are, the same line says how far: "Analytics cover 3 of 49 payers (3 of
// 615 files). Rolling up the rest — 41 %, about 2 h left", in the warn colour.
function coverageHtml(a) {
  if (!a || !a.state || a.state === "current") return "";
  const head = `Analytics cover <b>${fmtInt(a.covered_payers)}</b> of <b>${fmtInt(a.loaded_payers)}</b> ` +
    `payer${a.loaded_payers === 1 ? "" : "s"} (${fmtInt(a.covered_files)} of ${fmtInt(a.done_files)} files).`;
  const tail = esc(a.reason || "");
  return ` <span class="cov-line cov-${esc(a.state)}" id="coverage-line" title="Every screen, report and export answers from the analytics. Payers and files not in them yet are loaded and safe; they appear as the analytics catch up. Click to watch it happen.">${head} ${tail} <a href="#analytics" class="cov-files">details</a></span>`;
}
function wireCoverageLink() {
  const line = $("#coverage-line");
  if (line) line.onclick = (ev) => { ev.preventDefault(); openAnalytics(); };
  scheduleCoverageRefresh();
}

/* ---------- the Analytics activity panel (review 14 item 1.2) ---------- */
// Watch the newly loaded files go into the numbers every screen reads. Polls
// the async /api/analytics/activity every 2 s while open (it never waits
// behind a busy server); every number on it comes from the server's own
// state — nothing here is computed.
function stopAnalyticsPoll() {
  if (state.anTimer) { clearInterval(state.anTimer); state.anTimer = null; }
}
function openAnalytics() {
  if (state.view !== "files") switchView("files");
  const p = $("#analytics-panel");
  if (!p) return;
  p.style.display = "";
  if (parseHash().view !== "analytics") history.replaceState(null, "", "#analytics");
  refreshAnalytics();
  stopAnalyticsPoll();
  state.anTimer = setInterval(refreshAnalytics, 2000);
}
function closeAnalytics() {
  const p = $("#analytics-panel");
  if (p) p.style.display = "none";
  stopAnalyticsPoll();
  if (parseHash().view === "analytics") history.replaceState(null, "", "#files");
}
async function refreshAnalytics() {
  let d;
  try { d = await api("/api/analytics/activity"); }
  catch (e) {
    const b = $("#an-body");
    const down = /Failed to fetch|NetworkError|Load failed/.test(String(e && e.message));
    if (b) b.insertAdjacentHTML("afterbegin", `<p class="warn-text" id="an-down">${down
      ? "The app is not answering right now (restarting?) — this panel tries again every 2 seconds; nothing is lost."
      : esc(e.message)}</p>`);
    const old = document.querySelectorAll("#an-down");
    for (let i = 1; i < old.length; i++) old[i].remove();
    return;
  }
  state.anLast = d;
  renderAnalytics(d);
  // the header line follows the panel's answer (one source)
  if (d && d.analytics) updateCoverageLine(d.analytics);
}
function anTable(cols, rows) {
  if (!rows.length) return `<p class="muted">none</p>`;
  return `<table><thead><tr>${cols.map((c) => `<th class="${c.num ? "num" : ""}">${esc(c.h)}</th>`).join("")}</tr></thead><tbody>` +
    rows.map((r) => `<tr>${cols.map((c) => `<td class="${c.num ? "num" : ""}">${c.f(r)}</td>`).join("")}</tr>`).join("") +
    `</tbody></table>`;
}
function renderAnalytics(d) {
  const body = $("#an-body");
  if (!body || !d) return;
  const a = d.analytics || {};
  const pb = $("#an-pause");
  if (pb) pb.textContent = d.paused ? "Resume analytics" : "Pause analytics";
  let html = "";
  if (d.error) html += `<p class="warn-text">${esc(d.error)}</p>`;
  const head = `Analytics cover <b>${fmtInt(a.covered_payers)}</b> of <b>${fmtInt(a.loaded_payers)}</b> payers ` +
    `(${fmtInt(a.covered_files)} of ${fmtInt(a.done_files)} files).`;
  if (d.paused) html += `<p class="an-paused">Analytics paused — files keep loading, but nothing new reaches the numbers until you press Resume.</p>`;
  if (d.bar) {
    const pct = Math.max(0, Math.min(100, d.bar.pct || 0));
    html += `<div class="an-bar" title="${pct} %"><span style="width:${pct}%"></span></div>` +
      `<div class="an-words"><b>${pct} %</b> · ${esc(d.bar.words || "")}</div>`;
  } else if (a.state === "current") {
    html += `<p class="an-idle an-ok">✓ Every loaded file is in the analytics. ${head}</p>`;
  } else {
    const nxt = d.scheduler && d.scheduler.why && d.scheduler.code !== "next" && d.scheduler.code !== "settle"
      ? ` Next: ${esc(d.scheduler.why)}.` : "";
    // (the header's pointer "— Files tab" means this panel: not repeated here)
    const why = String(a.reason || "").replace(/ — Files tab$/, "");
    html += `<p class="an-idle">${head} ${esc(why)}${why && !/[.!]$/.test(why) ? "." : ""}${nxt}</p>`;
  }
  if (d.bar) html += `<p class="muted" style="margin:0">${head}</p>`;
  const w = d.waiting || { count: 0, rows: 0, files: [] };
  html += `<div class="an-sec"><h4>Waiting to be added: ${fmtInt(w.count)} file${w.count === 1 ? "" : "s"}, ${fmtInt(w.rows)} rows</h4>` +
    anTable([
      { h: "File", f: (r) => esc(r.file) },
      { h: "Payer", f: (r) => esc(r.payer || "") },
      { h: "Rows", num: true, f: (r) => fmtInt(r.rows) },
      { h: "Loaded", f: (r) => esc(r.loaded_text || "") },
      { h: "Why it is waiting", f: (r) => esc(r.why || "") },
    ], (w.files || []).slice(0, 200)) + `</div>`;
  html += `<div class="an-sec"><h4>Added recently</h4>` +
    anTable([
      { h: "File", f: (r) => esc(r.file) },
      { h: "Payer", f: (r) => esc(r.payer || "") },
      { h: "Rows", num: true, f: (r) => fmtInt(r.rows) },
      { h: "On screen since", f: (r) => esc(r.added_text || "") },
      { h: "Waited", f: (r) => esc(r.waited_text || "") },
      { h: "By", f: (r) => esc(r.kind || "") },
    ], d.added || []) + `</div>`;
  html += `<div class="an-sec"><h4>Per payer</h4>` +
    anTable([
      { h: "Payer", f: (r) => esc(r.payer) },
      { h: "In the analytics?", f: (r) => r.status === "covered" ? `<span class="an-ok">✓</span> ${esc(r.words)}` : esc(r.words) },
      { h: "Rows in the analytics", num: true, f: (r) => fmtInt(r.rows_in) },
      { h: "Rows loaded", num: true, f: (r) => fmtInt(r.rows_loaded) },
      { h: "Last added", f: (r) => esc(r.last_added || "") },
    ], d.payers || []) + `</div>`;
  if (d.problems) {
    const p = d.problems;
    html += `<div class="an-sec"><h4>Problem</h4><p class="warn-text">${esc(p.sentence || "")}` +
      `${p.at ? ` (at ${esc(p.at)})` : ""}${p.retry_at ? ` — it tries again at ${esc(p.retry_at)}` : ""}.</p>` +
      `${p.advice ? `<p>${esc(p.advice)}</p>` : ""}</div>`;
  }
  html += `<div class="an-mean"><b>What this means.</b> ${esc(d.what_this_means || "")}</div>`;
  body.innerHTML = html;
}
// the header's coverage line: refreshed from the cheap activity route every
// 5 s while the analytics are behind (the full header stats stay on 15 s)
function updateCoverageLine(a) {
  const wasBehind = state.analytics && state.analytics.state && state.analytics.state !== "current";
  state.analytics = Object.assign({}, state.analytics || {}, a);
  const el = $("#coverage-line");
  if (a.state === "current") {
    if (el) el.remove();
    if (wasBehind) loadStats();
    return;
  }
  const html = coverageHtml(a);
  if (el) el.outerHTML = html;
  else { const st = $("#dataset-stats"); if (st && !/loading/.test(st.textContent)) st.insertAdjacentHTML("beforeend", html); }
  const line = $("#coverage-line");
  if (line) line.onclick = (ev) => { ev.preventDefault(); openAnalytics(); };
}
function scheduleCoverageRefresh() {
  const behind = state.analytics && state.analytics.state && state.analytics.state !== "current";
  if (behind && !state.covTimer) {
    state.covTimer = setInterval(async () => {
      if (state.anTimer) return;          // the open panel already refreshes it
      try { const d = await api("/api/analytics/activity"); if (d && d.analytics) updateCoverageLine(d.analytics); }
      catch (e) { /* the 15 s header poll keeps going */ }
      if (!(state.analytics && state.analytics.state && state.analytics.state !== "current")) {
        clearInterval(state.covTimer); state.covTimer = null;
      }
    }, 5000);
  }
}
function initAnalyticsPanel() {
  const open = $("#btn-analytics");
  if (open) open.onclick = () => (($("#analytics-panel") || {}).style || {}).display === "none" ? openAnalytics() : closeAnalytics();
  const close = $("#an-close");
  if (close) close.onclick = closeAnalytics;
  const run = $("#an-run");
  if (run) run.onclick = async () => {
    run.disabled = true;
    try { await postJson("/api/analytics/run-now", {}); } catch (e) { alert(e.message); }
    finally { run.disabled = false; }
    refreshAnalytics();
  };
  const pause = $("#an-pause");
  if (pause) pause.onclick = async () => {
    const paused = !(state.anLast && state.anLast.paused);
    pause.disabled = true;
    try { await postJson("/api/analytics/pause", { paused }); } catch (e) { alert(e.message); }
    finally { pause.disabled = false; }
    refreshAnalytics();
  };
}
// one renderer for every payer chip list: a loaded payer the analytics do not
// hold yet is greyed, titled, and still pickable (it answers "no rows" plus why)
function payerChipsHtml(resp, on) {
  const payers = (resp && resp.payers) || [];
  const cov = resp && Array.isArray(resp.covered) ? new Set(resp.covered) : null;
  if (cov) state.coveredPayers = cov;
  const sel = on || new Set();
  return payers.map((p) => {
    const un = cov && !cov.has(p);
    return `<button class="chip${sel.has(p) ? " on" : ""}${un ? " uncovered" : ""}" data-payer="${esc(p)}"` +
      `${un ? ` title="loaded, not yet in analytics"` : ""}>${esc(p)}</button>`;
  }).join("");
}
function payerOptionsHtml(resp) {
  const cov = resp && Array.isArray(resp.covered) ? new Set(resp.covered) : null;
  return ((resp && resp.payers) || []).map((p) =>
    `<option value="${esc(p)}"${cov && !cov.has(p) ? ` label="${esc(p)} — loaded, not yet in analytics"` : ""}>`).join("");
}
function monthOptionsHtml(resp) {
  const cov = resp && Array.isArray(resp.covered) ? new Set(resp.covered) : null;
  return ((resp && resp.months) || []).map((m) => cov && !cov.has(m)
    ? `<option value="${esc(m)}" class="uncovered" title="loaded, not yet in analytics">${esc(m)} — loaded, not yet in analytics</option>`
    : `<option>${esc(m)}</option>`).join("");
}
// "no rows" for a picked payer the analytics do not hold yet says why
function uncoveredNote(payers) {
  if (!state.coveredPayers || !payers || !payers.length) return "";
  const un = payers.filter((p) => !state.coveredPayers.has(p));
  if (!un.length) return "";
  return `<p class="warn-text">${un.map(esc).join(", ")} ${un.length === 1 ? "is" : "are"} loaded but not in the analytics yet — ` +
    `${esc((state.analytics && state.analytics.sentence) || "they appear as the analytics catch up.")}</p>`;
}
async function loadStats(given) {
  try {
    const s = given || await api("/api/stats");
    let html =
      `<b>${fmtInt(s.rates)}</b> rate rows · <b>${fmtInt(s.tins)}</b> TINs · ` +
      `<b>${s.payers}</b> payer${s.payers === 1 ? "" : "s"} · <b>${fmtInt(s.files_done)}</b> files`;
    state.analytics = s.analytics || null;
    html += coverageHtml(s.analytics);
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
    // new data landed (an ingest's rollup): every payer/month picker and the
    // open Explorer/Code view were filled once at boot and went stale — a
    // new payer was missing from every chip list and the Explorer strip said
    // 51 rows while the store held 73 (round 7 P1.20)
    if (s.data_generation != null) {
      if (state.dataGen != null && s.data_generation !== state.dataGen) onDataUpdated();
      state.dataGen = s.data_generation;
    }
    $("#dataset-stats").innerHTML = html;
    wireCoverageLink();
  } catch { $("#dataset-stats").textContent = "API unreachable"; }
}

// Called when NPPES enrichment resolves more names since the last poll. Cheap
// and idempotent: refresh the state pickers everywhere and re-pull the data for
// whichever view is open so freshly-identified names/geo appear live.
function onNamesUpdated() {
  loadStateOptions();
  // at most once a minute: the 15 s poll re-ran the open view on every batch
  // of names for as long as enrichment ran (hours on a big book)
  const now = Date.now();
  if (state.namesRefreshedAt && now - state.namesRefreshedAt < 60000) return;
  state.namesRefreshedAt = now;
  const v = state.view;
  // the table shows the names; the strip's numbers do not depend on them
  if (v === "explorer") loadRates();
  // never abort a Code-page request still running to restart the same one
  else if (v === "cpt" && state.cptSelected && !cptCtl) selectCpt(state.cptSelected);
}

// Called when /api/stats reports a new data generation. Refills the pickers
// that were filled once (payer chips, months, the Markets/Roster datalists),
// keeping what the user picked, and reloads the open Explorer/Code view; a
// view not open now reloads when it is next opened.
async function onDataUpdated() {
  loadStateOptions();
  try {
    const [payersResp, monthsResp] = await Promise.all([
      api("/api/payers").catch(() => ({ payers: null })),
      api("/api/months").catch(() => ({ months: null })),
    ]);
    const { payers } = payersResp, { months } = monthsResp;
    if (payers) {
      const chips = $("#f-payer-chips");
      const on = new Set(state.filters.payers || []);
      if (chips) chips.innerHTML = payers.length
        ? payerChipsHtml(payersResp, on)
        : `<span class="muted">none ingested yet</span>`;
      const opts = payerOptionsHtml(payersResp);
      for (const id of ["#mk-payer-opts", "#ro-payer-opts"]) { const dl = $(id); if (dl) dl.innerHTML = opts; }
      // the other tabs' chip lists keep what is picked and refresh their greying
      for (const id of ["#b-payer-chips", "#rc-payer-chips", "#ld-payer-chips", "#ch-payer-chips"]) {
        const el = $(id);
        if (!el || !el.children.length) continue;
        const keep = new Set($$(".chip.on", el).map((c) => c.dataset.payer));
        el.innerHTML = payerChipsHtml(payersResp, keep);
      }
    }
    if (months) {
      const sel = $("#f-month");
      if (sel) {
        const keep = sel.value;
        sel.innerHTML = `<option value="latest">latest — each contract's newest file</option><option value="">all months (pooled)</option>` +
          monthOptionsHtml(monthsResp);
        sel.value = months.includes(keep) || keep === "" ? keep : "latest";
      }
    }
  } catch { /* the next generation retries */ }
  state.staleViews = new Set(["explorer", "cpt"]);
  reloadIfStale(state.view);
}

function reloadIfStale(v) {
  if (!state.staleViews || !state.staleViews.has(v)) return;
  if (v === "explorer") { state.staleViews.delete(v); loadRates(); loadSummary(); }
  else if (v === "cpt" && state.cptSelected && !cptCtl) { state.staleViews.delete(v); selectCpt(state.cptSelected); }
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
    // one request per action: clicking Refresh after typing a state fired the
    // box's "change" (blur) AND the click — the same overview twice
    let ovLast = { v: null, t: 0 };
    const ovLoad = (force) => {
      const v = $("#ov-state").value.trim();
      if (!force && v === ovLast.v) return;
      if (force && v === ovLast.v && Date.now() - ovLast.t < 1000) return;
      ovLast = { v, t: Date.now() };
      loadOverview(v);
    };
    $("#ov-refresh").addEventListener("click", () => ovLoad(true));
    $("#ov-state").addEventListener("change", () => ovLoad(false));
    loadStateOptions();
  }
  loadOverview($("#ov-state").value.trim());
}

async function loadOverview(stateCode = "") {
  const out = $("#ov-out");
  out.innerHTML = `<div class="loading">Loading market overview</div>`;
  // the first overview after a start reads the whole store (10-20 s on a
  // 100M-rate store): say so, so a slow first paint is not read as a hang
  // (review 9 item 2.5)
  const slow = setTimeout(() => {
    if (out.querySelector(".loading"))
      out.innerHTML = `<div class="loading">Loading market overview — the first screen after a start reads the whole store and can take up to a minute on a large one; later screens are fast</div>`;
  }, 4000);
  let d;
  const isFresh = ticket("overview");
  try { d = await api(`/api/overview${stateCode ? `?state=${encodeURIComponent(stateCode)}` : ""}`, { bar: { view: "overview", label: "Market overview", anchor: "#ov-refresh" } }); }
  catch (e) {
    clearTimeout(slow);
    if (!isFresh()) return;
    out.innerHTML = `<div class="empty">Couldn't load the overview (${esc(e.message)}).</div>`; return;
  }
  clearTimeout(slow);
  if (!isFresh()) return;
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
    ? `<div class="ov-block"><h3>Typical evaluation rate by discipline</h3><div class="tablewrap"><table>
        <thead><tr><th>Discipline</th><th class="num">Median rate</th><th class="num">Practices</th></tr></thead>
        <tbody>${d.by_discipline.map((x) => `<tr><td>${esc(x.label)}</td><td class="num">${money(x.median)}</td><td class="num muted">${fmtInt(x.practices)}</td></tr>`).join("")}</tbody></table></div></div>`
    : "";

  // payer index: 1.00 = market; above = pays more, below = pays less. Color the bar.
  const idx = (d.payer_index || []);
  const payerBlock = idx.length
    ? `<div class="ov-block"><h3>Which payers pay above vs below market</h3>
       <div class="muted" style="margin-bottom:6px">Index = this payer's median rate ÷ the market median (all practices, all payers — the medians under "Best-covered codes"), across the codes it prices for at least 3 practices. <b>1.10 = pays ~10% above market</b>; 0.90 = ~10% below. Payers pricing under 3 such codes are omitted.</div>
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
    <div id="ov-quality"></div>
    <div class="bench-note">All figures use published dollar negotiated rates (base modifier, professional class). A published rate is directional market positioning, not proof a provider collects it.</div>`;
  renderDataQuality(stateCode);
}

// How solid is the data behind every number above: which payers re-published
// recently, and how much of the market is contracted vs payer-derived. Loaded
// after the snapshot so a slow/failed call never blanks it.
async function renderDataQuality(stateCode) {
  const el = $("#ov-quality");
  if (!el) return;
  const market = { month: "latest" };
  if (stateCode) market.state = stateCode;
  const isFresh = ticket("quality");
  const [fresh, types, posture] = await Promise.all([
    api("/api/quality/freshness").catch(() => null),
    postJson("/api/quality/types", { market }).catch(() => null),
    postJson("/api/quality/posture", { market }).catch(() => null),
  ]);
  if (!isFresh()) return;
  if (!fresh && !types && !posture) return;
  const rows = (fresh?.payers || []).map((p) => `<tr>
      <td>${esc(p.payer)}</td>
      <td class="num">${esc(p.published || "–")}</td>
      <td class="num ${p.stale ? "chg-cut" : ""}">${
        p.age_days == null ? (p.date_unreadable ? "unreadable" : "not stated")
                           : p.future_dated ? "dated in the future"
                           : p.age_days + " days"}</td>
      <td class="num muted">${esc(p.newest_file_month || "–")}</td>
      <td class="num muted">${fmtInt(p.rows)}</td></tr>`).join("");
  const mixRows = (types?.rows || []).map((r) => `<tr>
      <td>${esc(r.rate_type)}${r.contracted ? "" : ' <span class="muted">(not a contract)</span>'}</td>
      <td class="num">${r.pct}%</td>
      <td class="num muted">${fmtInt(r.n_practices)}</td>
      <td class="num">${money(r.median_rate)}</td></tr>`).join("");
  // Which payers actually do deals. The label is the payer's claim; the
  // spread is the evidence — both shown, because they can disagree.
  const postRows = (posture?.payers || []).map((p) => `<tr>
      <td>${esc(p.payer)}</td>
      <td>${p.winnable === true ? "<b>negotiates</b>"
            : p.winnable === false ? '<span class="muted">one schedule</span>'
            : '<span class="muted">too little data</span>'}</td>
      <td class="sub">${esc(p.claimed || "unstated")}</td>
      <td class="num muted">${p.pct_codes_varying == null ? "–" : p.pct_codes_varying + "%"}</td>
      <td class="sub">${p.conflict ? "<b>claim and behaviour disagree</b>" : ""}</td>
    </tr>`).join("");
  const postureBlock = postRows ? `<div class="ov-block"><h3>Which payers actually negotiate</h3>
    <div class="muted" style="margin-bottom:6px">${esc(posture.headline)}</div>
    <div class="tablewrap"><table><thead><tr><th>Payer</th><th>Read</th>
      <th>Says</th><th class="num">Codes priced differently</th><th></th></tr></thead>
      <tbody>${postRows}</tbody></table></div>
    <div class="muted" style="font-size:11.5px;margin-top:6px">${esc(posture.note)}</div></div>` : "";

  el.innerHTML = postureBlock + `<div class="ov-block"><h3>How solid is this data?</h3>
    ${types ? `<div class="muted" style="margin-bottom:6px">${esc(types.headline)}</div>` : ""}
    ${mixRows ? `<div class="tablewrap"><table>
      <thead><tr><th>Rate type</th><th class="num">Share</th><th class="num">Practices</th><th class="num">Median</th></tr></thead>
      <tbody>${mixRows}</tbody></table></div>` : ""}
    ${fresh ? `<div class="muted" style="margin:8px 0 6px">${esc(fresh.headline)}</div>` : ""}
    ${rows ? `<div class="tablewrap" style="max-height:32vh"><table>
      <thead><tr><th>Payer</th><th class="num">Published</th><th class="num">Age</th>
      <th class="num">Newest month</th><th class="num">Rows</th></tr></thead>
      <tbody>${rows}</tbody></table></div>` : ""}
    <div class="muted" style="font-size:11.5px;margin-top:6px">
      ${esc(types?.note || "")} ${esc(fresh?.note || "")}</div></div>`;
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
  market.peer_group = $("#mk-peer").value;
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
  const isFresh = ticket("markets");
  const [geo, neg, mcr, diff] = await Promise.all([
    postJson("/api/market/geography", body).catch((e) => ({ _err: e.message })),
    postJson("/api/market/negotiability", body).catch((e) => ({ _err: e.message })),
    postJson("/api/market/medicare", body).catch((e) => ({ _err: e.message })),
    postJson("/api/market/differential", body).catch((e) => ({ _err: e.message })),
  ]);
  if (!isFresh()) return;
  const desc = state.catalog[code]?.description || "";
  out.classList.remove("empty");
  out.innerHTML =
    `<div class="rc-summary">Market intelligence — <b>${esc(code)}</b>${desc ? " · " + esc(desc) : ""}. ` +
    `Latest rate per contract; thin cells (&lt;5 practices) suppressed.</div>` +
    renderMkMedicare(mcr) + renderMkNegotiability(neg) +
    renderMkGeography(geo) +
    `<div id="mk-local"></div><div id="mk-radius"></div><div id="mk-hhi"></div>` +
    renderMkDifferential(diff);
  renderMkLocal(code, market);
  renderMkRadius(code, market);
  renderMkConcentration(market, code);
}

// Inside ONE state, cities differ enough to matter to a client choosing where
// to open or arguing their location. Only runs when a state is set — pooling
// cities across states would bury the far larger state effect.
async function renderMkLocal(code, market) {
  const el = $("#mk-local");
  if (!el) return;
  if (!market.state) {
    el.innerHTML = mkEmpty("Inside one state (by city)",
      "Set a state above to break this code down by city.");
    return;
  }
  const d = await postJson("/api/territory/local",
                           { code, market, state: market.state }).catch((e) => ({ _err: e.message }));
  if (d._err) { el.innerHTML = mkErr(d, "Inside one state (by city)"); return; }
  if (!d.cities.length) {
    el.innerHTML = mkEmpty("Inside one state (by city)", esc(d.headline));
    return;
  }
  const rows = d.cities.map((c) => `<tr>
    <td>${esc(c.city.toLowerCase().replace(/\b\w/g, (m) => m.toUpperCase()))}</td>
    <td class="num">${money(c.median_rate)}</td>
    <td class="num ${c.vs_state_pct > 0 ? "" : "warn-text"}">${
      c.vs_state_pct == null ? "–" : (c.vs_state_pct > 0 ? "+" : "") + c.vs_state_pct + "%"}</td>
    <td class="num muted">${money(c.p25)} – ${money(c.p75)}</td>
    <td class="num muted">${fmtInt(c.n_practices)}</td></tr>`).join("");
  el.innerHTML = `<div class="ov-block"><h3>Inside ${esc(d.state)} (by city)</h3>
    <div class="muted" style="margin-bottom:6px">${esc(d.headline)}
      State median ${money(d.state_median)}.${
        d.suppressed_cities ? ` ${d.suppressed_cities} city/cities suppressed (under ${d.min_practices} practices).` : ""}</div>
    <div class="tablewrap"><table><thead><tr><th>City</th><th class="num">Median</th>
      <th class="num">vs state</th><th class="num">P25–P75</th><th class="num">Practices</th>
      </tr></thead><tbody>${rows}</tbody></table></div>
    <div class="muted" style="font-size:11.5px;margin-top:6px">${esc(d.note)}</div></div>`;
}

// The sharpest form of the sub-state question: same payer, same code, a short
// drive apart. City names cannot show it — a metro's edge is 40 miles from its
// centre — so this places every practice by its own ZIP's centroid.
async function renderMkRadius(code, market) {
  const el = $("#mk-radius");
  if (!el) return;
  const zip = ($("#mk-zip") && $("#mk-zip").value.trim()) || "";
  if (!/^\d{5}$/.test(zip)) {
    el.innerHTML = mkEmpty("By distance from a ZIP",
      "Enter a 5-digit ZIP above to see whether this code pays differently a short drive away.");
    return;
  }
  const d = await postJson("/api/territory/radius",
                           { code, market, zip, max_miles: 100 })
    .catch((e) => ({ _err: e.message }));
  if (d._err) { el.innerHTML = mkErr(d, "By distance from a ZIP"); return; }
  const rows = d.bands.map((b) => `<tr>
    <td>${esc(b.band)}</td>
    <td class="num">${b.thin ? "–" : money(b.median_rate)}</td>
    <td class="num ${b.vs_nearest_pct < 0 ? "warn-text" : ""}">${
      b.vs_nearest_pct == null ? "–" : (b.vs_nearest_pct > 0 ? "+" : "") + b.vs_nearest_pct + "%"}</td>
    <td class="num muted">${fmtInt(b.n_practices)}${b.thin && b.n_practices ? " (thin)" : ""}</td>
  </tr>`).join("");
  el.innerHTML = `<div class="ov-block"><h3>By distance from ${esc(d.zip)}</h3>
    <div class="muted" style="margin-bottom:6px">${esc(d.headline)}${
      d.unplaced ? ` ${d.unplaced} practice(s) had no ZIP centroid and are not placed.` : ""}</div>
    <div class="tablewrap"><table><thead><tr><th>Distance</th>
      <th class="num">Median</th><th class="num">vs nearest band</th>
      <th class="num">Practices</th></tr></thead><tbody>${rows}</tbody></table></div>
    <div class="muted" style="font-size:11.5px;margin-top:6px">${esc(d.note)}</div></div>`;
}

// Who holds the contracts here. The caveat is louder than the number: this is
// the store's own payer coverage, not market share of covered lives.
async function renderMkConcentration(market, code) {
  const el = $("#mk-hhi");
  if (!el) return;
  const d = await postJson("/api/territory/concentration", { market, code })
    .catch((e) => ({ _err: e.message }));
  if (d._err) { el.innerHTML = mkErr(d, "Payer concentration"); return; }
  if (!d.payers.length) { el.innerHTML = ""; return; }
  const rows = d.payers.map((p) => `<tr>
    <td>${esc(p.payer)}</td>
    <td class="num">${p.share_pct}%</td>
    <td class="num muted">${fmtInt(p.n_practices)}</td>
    <td class="num">${money(p.median_rate)}</td></tr>`).join("");
  el.innerHTML = `<div class="ov-block"><h3>Payer concentration${
      d.band ? ` — ${esc(d.band)}` : ""}</h3>
    <div class="muted" style="margin-bottom:6px">${esc(d.headline)}</div>
    <div class="tablewrap"><table><thead><tr><th>Payer</th>
      <th class="num">Share of contracted practices</th><th class="num">Practices</th>
      <th class="num">${d.median_code ? `Median ${esc(d.median_code)} rate` : "Median rate, all codes"}</th></tr></thead><tbody>${rows}</tbody></table></div>
    <div class="muted" style="font-size:11.5px;margin-top:6px">${esc(d.note)}</div></div>`;
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
  const rows = d.states.map((s) => `<tr${s.state === d.selected_state ? ' class="sel-row" style="font-weight:600;background:var(--hl,#fff7d6)"' : ""}>
    <td>${esc(s.state)}${s.state === d.selected_state ? " ◂ your state" : ""}</td>
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
   PAYER ROSTER — every organization contracted with one payer
   ======================================================================= */
let rosterWired = false;
let lastRosterBody = null;

async function initRoster() {
  if (!rosterWired) {
    rosterWired = true;
    try {
      const [{ payers }, { states }] = await Promise.all([
        api("/api/payers").catch(() => ({ payers: [] })),
        api("/api/states").catch(() => ({ states: [] })),
      ]);
      $("#ro-payer-opts").innerHTML = (payers || []).map((p) => `<option value="${esc(p)}">`).join("");
      $("#ro-state-opts").innerHTML = (states || []).map((s) => `<option value="${esc(s)}">`).join("");
    } catch { /* free text still works */ }
    $("#ro-run").addEventListener("click", loadRoster);
    $("#ro-payer").addEventListener("keydown", (e) => { if (e.key === "Enter") loadRoster(); });
    $("#ro-csv").addEventListener("click", () => { if (lastRosterBody) postDownload("/api/roster.csv", lastRosterBody, "payer_roster.csv", { view: "roster", label: "Roster CSV", anchor: "#ro-csv" }); });
  }
}

function rosterBody() {
  const payer = $("#ro-payer").value.trim();
  const market = { month: "latest" };
  if ($("#ro-state").value.trim()) market.state = $("#ro-state").value.trim().toUpperCase();
  if ($("#ro-disc").value) market.discipline = $("#ro-disc").value;
  market.peer_group = $("#ro-peer").value;
  // send an explicit limit so the CSV contains exactly the list on screen —
  // the endpoints' own defaults differ (500 vs 2000), which made the "showing
  // the first 500" note wrong for the export
  return { payer, market, min_codes: +$("#ro-mincodes").value || 1,
           sort: $("#ro-sort").value, limit: 500 };
}

async function loadRoster() {
  const out = $("#ro-out");
  const body = rosterBody();
  $("#ro-csv").disabled = true;
  lastRosterBody = null;
  if (!body.payer) { out.innerHTML = `<div class="empty">Pick a payer first.</div>`; return; }
  out.innerHTML = `<div class="loading">Finding organizations contracted with ${esc(body.payer)}</div>`;
  let d;
  try { d = await postJson("/api/roster", body, { view: "roster", label: "Payer roster", anchor: "#ro-run" }); }
  catch (e) { out.innerHTML = `<div class="empty"><h3>Couldn't load the roster</h3>${esc(e.message)}</div>`; return; }
  lastRosterBody = body;
  $("#ro-csv").disabled = false;
  renderRoster(out, d);
}

function renderRoster(out, d) {
  // .empty styles the placeholder (centred, muted, big padding) — drop it before
  // rendering a real table, or the table inherits placeholder framing
  out.classList.toggle("empty", !d.organizations.length);
  if (!d.organizations.length) {
    out.innerHTML = `<div class="empty"><h3>No organizations found</h3>No practices publish therapy rates with ${esc(d.payer)} under the current filters. ${$("#ro-peer").value !== "all" ? "\"Compare against\" is limited to " + $("#ro-peer").selectedOptions[0].textContent + " — choose \"all practices\", or let NPI identification finish, to see more." : "Widen the state/discipline or lower min codes."}</div>`;
    return;
  }
  const rows = d.organizations.map((o, i) => `<tr>
    <td class="num muted">${i + 1}</td>
    <td>${esc(o.display_name || "(name pending)")}<div class="sub">${esc(o.entity_kind || "")}${o.website ? ` · <a href="${esc(o.website)}" target="_blank" rel="noopener">site</a>` : ""}</div></td>
    <td class="sub">${esc([o.city, o.state].filter(Boolean).join(", "))}${o.multi_state ? ` <span class="muted" title="${esc((o.states || []).join(", "))}">(+${(o.states || []).length - 1})</span>` : ""}</td>
    <td class="num">${fmtInt(o.npi_count)}</td>
    <td class="num">${fmtInt(o.n_codes)}</td>
    <td class="num">${o.median_percentile == null ? "<span class='muted'>–</span>" : "p" + o.median_percentile}</td>
    <td class="num">${money(o.median_rate)}</td>
    <td class="sub">${esc(o.tin_value)}</td></tr>`).join("");
  const cap = d.truncated ? ` <span class="warn-text">Showing the first ${fmtInt(d.limit)} — narrow by state/discipline to see the rest.</span>` : "";
  // How old the book being described actually is. A roster of a payer that
  // last re-published 14 months ago is a roster of what WAS true.
  const f = d.freshness;
  const fresh = !f ? ""
    : f.published == null
      ? ` <span class="muted">This payer's files state no publication date.</span>`
      : f.stale
        ? ` <span class="warn-text">Last published ${esc(f.published)}${
            f.age_days != null && f.age_days >= 0 ? ` — ${fmtInt(f.age_days)} days ago` : ""}; these rates have not been refreshed recently.</span>`
        : ` <span class="muted">Last published ${esc(f.published)}${
            f.future_dated ? " (dated in the future)"
            : f.age_days != null ? ` (${fmtInt(f.age_days)} days ago)` : ""}.</span>`;
  out.innerHTML = `<div class="rc-summary"><b>${fmtInt(d.count)}${d.truncated ? "+" : ""}</b> organization${d.count === 1 ? "" : "s"} contracted with <b>${esc(d.payer)}</b>${d.market.state ? " in " + esc(d.market.state) : ""}${d.market.discipline ? " · " + esc(d.market.discipline) : ""}.${cap}${fresh}</div>
    <div class="muted" style="font-size:11.5px;margin:-6px 0 10px">${esc(d.note)}</div>
    <div class="tablewrap" style="max-height:calc(100vh - 220px)"><table>
      <thead><tr><th>#</th><th>Organization</th><th>Location</th><th class="num">Providers</th><th class="num">Codes</th><th class="num" title="position among this payer's other practices">Position</th><th class="num" title="median across whichever codes that practice prices with this payer — not comparable row-to-row unless the practices price the same codes">Median rate*</th><th>TIN</th></tr></thead>
      <tbody>${rows}</tbody></table></div>`;
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
  // "latest" = each contract's newest file: the house basis every report tab
  // uses (review 10 item 2.2 — the Explorer could not show it, and its By-code
  // table said "the Benchmark's basis" over pooled months)
  if (f.month === "latest") p.set("latest", "1");
  else if (f.month) p.set("month", f.month);
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
  // the previous rows stay, dimmed, until the new ones land: blanking them on
  // every search left the user looking at an empty table (review 10 1.9)
  body.classList.add("stale");
  const q = filterQuery({ sort: state.sort.col, dir: state.sort.dir, page: state.page, page_size: state.pageSize });
  const fresh = ticket("rates");
  let data;
  try { data = await api(`/api/rates?${q}`, { ...supersede("rates"), bar: { view: "explorer", label: "Rates table", keepMs: 300 } }); }
  catch (e) {
    if (!fresh()) return;
    body.innerHTML = "";
    body.classList.remove("stale");
    // the pager must not keep describing the previous view (review 10 1.8)
    $("#pg-label").textContent = "–";
    $("#pg-prev").disabled = true;
    $("#pg-next").disabled = true;
    stateEl.innerHTML = `<div class="empty"><h3>Could not load rates</h3>${esc(e.message)}</div>`; return;
  }
  if (!fresh()) return;
  if (!data.total) {
    body.innerHTML = "";
    body.classList.remove("stale");
    const why = uncoveredNote(state.filters.payers);
    stateEl.innerHTML = data.hint
      ? `<div class="empty"><h3>No rates match</h3>${esc(data.hint)}${why}</div>`
      : `<div class="empty"><h3>No rates match</h3>${why ||
      `Drop MRF files into <code class="inline">data/inbox/</code> or loosen the filters.`}</div>`;
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
  body.classList.remove("stale");
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
    ? `<span class="variant-flag" title="this entity shows ${r.rate_variants} different rates for this tuple ($${fmtMoney(r.rate_min)}–$${fmtMoney(r.rate_max)})${Number(r.rate_min) <= 0.01 ? " — the low end is a $0/$0.01 placeholder, which the shown rate leaves out" : ""}; the shown rate is their median">${r.rate_variants} ⚠</span>`
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
    <td class="sub">${esc(r.file_month || "—")}</td>
    <td class="num">${variants}</td>
    <td>${esc(r.negotiated_type)}${r.is_dollar_rate ? "" : ` <span class="warn-text">(non-dollar)</span>`}</td>
    <td class="num">${fmtInt(r.source_count)}</td>
  </tr>`;
}
// referenced above at the rate cell: use money() so a non-dollar row (null
// rate when "dollar rates only" is unchecked) renders "–", not "$–".

async function loadSummary() {
  const el = $("#summary-strip");
  const fresh = ticket("summary");
  const asked = filterQuery().toString();
  try {
    const s = await api(`/api/summary?${asked}`, { ...supersede("summary"), bar: { view: "explorer", label: "Summary strip", keepMs: 300 } });
    if (!fresh()) return;
    const stat = (k, v) => `<div class="stat"><div class="k">${k}</div><div class="v">${v}</div></div>`;
    el.innerHTML =
      stat("Rows", fmtInt(s.n)) +
      stat(state.grain === "npi" ? "NPIs" : state.grain === "tin" ? "Tax IDs" : "Entities", fmtInt(s.entities)) +
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
    if (Array.isArray(s.by_code) && s.by_code.length) state.lastByCode = s.by_code;
    renderByCode($("#summary-bycode"), s.by_code, s.mpfs_loaded, s.by_code_basis,
                 s.medicare_anchor, s.medicare_refusal);
  } catch (e) {
    if (!fresh()) return;
    // Say what actually happened. "Busy" is a 503 from the single-flight lock
    // (the answer is seconds away); a memory refusal needs a filter; any other
    // refusal (e.g. the 422 "your store is large — pick a payer and a code")
    // is shown as the server wrote it. Every error used to read "busy".
    const memory = /memory/i.test(e.message || "");
    const busy = e.status === 503 && !memory;
    const msg = busy
      ? "Stats are busy — retrying…"
      : memory
        ? "Stats need more memory than the current limit — add a filter (payer, code, or state), or raise duckdb_memory_gb in config and restart."
        : `Summary unavailable: ${e.message}`;
    el.innerHTML = `<div class="stat" style="flex:1"><div class="k">Summary</div><div class="v" style="font-size:0.8rem;font-weight:normal">${esc(msg)}</div></div>`;
    const bc = $("#summary-bycode"); if (bc) bc.innerHTML = "";
    // "busy" means another scan holds the single-flight lock, so the answer is
    // seconds away. Keep the promise instead of leaving a dead strip the user
    // has to poke a filter to clear — but only once, and only if the filter
    // hasn't moved on since (a later refresh supersedes this retry).
    if (busy && !loadSummary._retrying) {
      loadSummary._retrying = true;
      setTimeout(() => {
        loadSummary._retrying = false;
        // compare the query STRINGS: two URLSearchParams objects are never
        // ===, so this retry never fired and the strip said "retrying…" forever
        if (filterQuery().toString() === asked) loadSummary();
      }, 1400);
    }
  }
}

// Per-code breakdown of the current filter — the strip's one blended median
// mixes different services (97110 vs 97530), so give a real median + spread PER
// code, with a below-Medicare flag when an MPFS anchor is loaded.
function renderByCode(el, rows, mpfs, basis, anchor, refusal) {
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
      <thead><tr><th>Code</th><th class="num">Median</th><th class="num">P25–P75</th>${mpfs ? `<th class="num">% of Medicare</th>` : ""}<th class="num">${state.grain === "npi" ? "NPIs" : "Practices"}</th></tr></thead>
      <tbody>${body}</tbody></table></div>
    <div class="muted" style="margin-top:4px">Per code: each ${state.grain === "npi" ? "NPI's" : "practice's"} own median first, then the spread across practices${state.filters.month === "latest" ? " — the Benchmark's basis" : state.filters.month ? ` — the published month ${esc(state.filters.month)} only` : " — POOLED over every loaded month (each payer weighted by how many of its months are loaded; pick \u201clatest\u201d for the Benchmark's basis)"}: ${esc(basis || "")}.${anchor ? ` % of Medicare vs ${esc(anchor)}.` : ""}${refusal ? ` <span class="warn-text">${esc(refusal)}</span>` : ""}</div></div>`;
}

const refresh = () => { loadRates(); loadSummary(); };
// Ticking three payer chips in a row used to fire three full scans. On a big
// store each one is heavy enough that the later ones hit the summary's
// single-flight lock and land on "Stats are busy" — for a filter the user is
// still in the middle of choosing. Coalesce a burst into the one query they
// actually meant. (Entry and init still refresh immediately, below.)
const refreshSoon = debounce(refresh, 280);
const refreshFromFirstPage = () => { state.page = 1; refreshSoon(); };
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
  // both pickers at once (they were fetched one after the other)
  const monthsP = api("/api/months").catch(() => null);
  const payersResp = await api("/api/payers").catch(() => null);
  const payers = payersResp?.payers ?? [];
  const payerChips = $("#f-payer-chips");
  payerChips.innerHTML = payers.length
    ? payerChipsHtml(payersResp)
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

  const monthsResp = await monthsP;
  $("#f-month").innerHTML = `<option value="latest">latest — each contract's newest file</option><option value="">all months (pooled)</option>` +
    monthOptionsHtml(monthsResp);
  $("#f-month").value = state.filters.month;
  $("#f-month").addEventListener("change", (e) => { state.filters.month = e.target.value; state.page = 1; refresh(); });

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
      const v = transform(e.target.type === "checkbox" ? e.target.checked : e.target.value);
      // nothing changed (a 1-letter name, a trailing space): no request at all
      if (v === state.filters[key] || (v === "" && !state.filters[key])) return;
      state.filters[key] = v;
      // review 10 1.2: a typed box already waited 300 ms for the typing to
      // stop, and one click needs no wait — no second 280 ms timer behind it
      // (two timers in series held every search 0.58 s before it was sent)
      state.page = 1;
      refresh();
    }, ev === "input" ? 300 : 0));
  bind("#f-modifier", "modifier");
  bind("#f-mod-has", "mod_has", "input");
  bind("#f-mod-not", "mod_not", "input");
  bind("#f-class", "billing_class");
  bind("#f-pos", "pos");
  bind("#f-state", "state", "input", (v) => v.trim());
  bind("#f-city", "city", "input", (v) => v.trim());
  bind("#f-q", "q", "input", (v) => {
    const t = v.trim();
    return t.length < 2 && !/^\d+$/.test(t) ? "" : t;
  });
  bind("#f-rate-min", "rate_min", "input");
  bind("#f-rate-max", "rate_max", "input");
  bind("#f-dollar", "dollar");
  bind("#f-hidetinnpi", "hide_tin_npi");
  bind("#f-outliers", "hide_outliers");
  bind("#f-therapy", "therapy_only");

  $("#f-clear").addEventListener("click", () => {
    state.filters = { payers: [], cpts: [], disciplines: [], modifier: "", mod_has: "", mod_not: "",
      billing_class: "", pos: "", state: "", city: "", month: "latest", q: "",
      dollar: true, hide_tin_npi: false, hide_outliers: false, therapy_only: false,
      rate_min: "", rate_max: "" };
    // Explorer chips ONLY — a document-wide ".chip.on" sweep also wiped the
    // BENCHMARK tab's payer selection (read from the DOM at run time), so a
    // Clear here silently turned a payer-scoped benchmark into all-payers
    $$("#f-payer-chips .chip.on, #f-cpt-chips .chip.on, #f-disc-chips .chip.on")
      .forEach((c) => c.classList.remove("on"));
    ["#f-modifier", "#f-mod-has", "#f-mod-not", "#f-class", "#f-pos", "#f-state", "#f-city",
     "#f-q", "#f-rate-min", "#f-rate-max"].forEach((id) => ($(id).value = ""));
    $("#f-month").value = "latest";
    $("#f-dollar").checked = true;
    $("#f-hidetinnpi").checked = false;
    $("#f-outliers").checked = false;
    $("#f-therapy").checked = false;
    refreshFromFirstPage();
  });
  $("#btn-export").addEventListener("click", () => {
    downloadInFrame(`/api/export.csv?${filterQuery({ sort: state.sort.col, dir: state.sort.dir, view: "explorer" })}`, { view: "explorer", label: "Export CSV", anchor: "#btn-export" });
  });
  $("#btn-export-zip").addEventListener("click", () => {
    downloadInFrame(`/api/export.zip?${filterQuery({ sort: state.sort.col, dir: state.sort.dir, view: "explorer" })}`, { view: "explorer", label: "Export + methodology", anchor: "#btn-export-zip" });
  });
  $("#btn-outreach").addEventListener("click", () => {
    // .zip: the outreach CSV + its methodology sidecar together (comment lines
    // inside the CSV itself would break the mail-merge import it exists for)
    downloadInFrame(`/api/export/outreach.zip?${filterQuery()}`, { view: "explorer", label: "Outreach file", anchor: "#btn-outreach" });
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
  const isFresh = ticket("drawer");
  try { d = await api(`/api/entity/${grain}/${encodeURIComponent(unitId)}`, { bar: { view: "explorer", label: "Practice details", keepMs: 300 } }); }
  catch (e) {
    if (!isFresh()) return;
    drawer.innerHTML = `<button class="close">×</button><div class="empty">${esc(e.message)}</div>`;
    $(".close", drawer).onclick = closeDrawer;
    return;
  }
  if (!isFresh()) return;
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
    ${medicareGlanceHtml(d.medicare)}
    <h3>Current rate by code${payers.length > 1 ? " and payer" : ""} <span class="muted" style="font-size:12px;font-weight:400">each contract's newest file · professional · base modifiers</span></h3>
    ${payers.length > 1 ? `<div class="legend">${payers.slice(0, 2).map((p, i) =>
      `<span><span class="sw" style="background:var(--accent${i ? "-2" : ""})"></span>${esc(p)}</span>`).join("")}</div>` : ""}
    <div id="entity-chart"></div>
    ${tinsHtml}
    ${npisHtml}
    <h3>All rates</h3>
    <div class="tablewrap" style="max-height:36vh">
      <table>
        <thead><tr><th>Code</th><th>Payer</th><th>Mods</th><th>Class</th><th>POS</th><th>Month</th><th>Type</th><th class="num">Rate</th><th class="num">Var.</th></tr></thead>
        <tbody>${d.rates.map((r) => `
          <tr>
            <td>${esc(r.billing_code)}<div class="sub">${esc(state.catalog[r.billing_code]?.description || "")}</div></td>
            <td>${esc(r.payer)}</td>
            <td>${modTags(r.modifier_set)}</td>
            <td>${esc(r.billing_class || "—")}</td>
            <td class="sub">${esc(String(r.service_code_set || "—").replaceAll("|", ", "))}</td>
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
// The Medicare layers travel WITH the practice: whoever opens one anywhere in
// the app sees its referral base without changing tabs. Absent (null) unless
// those layers were imported — an empty box would read as "no referrals".
function medicareGlanceHtml(m) {
  if (!m) return "";
  const warn = m.lost_standing
    ? ` · <span class="warn-text">${fmtInt(m.lost_standing)} lost order/refer standing in the latest roster</span>`
    : "";
  return `<div class="rc-summary" style="margin:10px 0">
    <b>Medicare referral base:</b> ${fmtInt(m.sources)} source${m.sources === 1 ? "" : "s"} ·
    ${fmtInt(m.patients)} shared patients${warn}
    <div class="muted" style="font-size:11.5px;margin-top:2px">${esc(m.dataset || "")}
      ${esc(m.data_year || "")} — shared-patient data is not a referral record; full detail and
      caveats on the Medicare tab.</div></div>`;
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
  // the gutter fits the widest count label: a fixed 40 px cut "10,000" to
  // "0,000" (text-anchor end at x = 34 runs off the left edge)
  const ticks = niceTicks(maxN, 3);
  const widest = Math.max(1, ...ticks.map((t) => fmtInt(t).length));
  const left = Math.max(40, 14 + widest * 7), bottom = 26, top = 12, h = 190;
  const bw = (w - left - 16) / hist.length;
  const y = (n) => top + (1 - n / maxN) * (h - top - bottom);
  let svg = `<svg class="chart" viewBox="0 0 ${w} ${h}" role="img" aria-label="rate distribution for ${esc(code)}" style="max-width:${w}px">`;
  for (const t of ticks) {
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
  $("#cpt-class").addEventListener("change", () => state.cptSelected && selectCpt(state.cptSelected));
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
      grain: "tin", sort: "negotiated_rate", dir: "desc", latest: "1" });
    if ($("#cpt-base-only").checked) p.set("modifier", "base");
    if ($("#cpt-class").value) p.set("billing_class", $("#cpt-class").value);
    if (cptState.value.trim()) p.set("state", cptState.value.trim().toUpperCase());
    downloadInFrame(`/api/export.zip?${p}`, { view: "cpt", label: "Code export", anchor: "#cpt-export" });
  });
}

// The Code page's in-flight request. A newer click (another code, the state
// box, the base-only toggle) aborts it: the old answer can never land under
// the new code's heading, and the browser stops waiting on it at once.
let cptCtl = null;

async function selectCpt(code) {
  state.cptSelected = code;
  $$("#cpt-chips .chip").forEach((c) => c.classList.toggle("on", c.dataset.cpt === code));
  if (cptCtl) cptCtl.abort();
  const ctl = new AbortController();
  cptCtl = ctl;
  const current = () => cptCtl === ctl;
  // a FINISHED request releases the gate (it stayed set for ever, so the
  // Code page never refreshed when names or data arrived)
  const done = () => { if (cptCtl === ctl) cptCtl = null; };
  const body = $("#cpt-body"), stateEl = $("#cpt-state");
  const cptState = $("#cpt-state-filter").value.trim();
  const info = state.catalog[code] || {};
  const heading = (asOf) =>
    `Which entities get paid most for ${code}${info.description ? " — " + info.description : ""}` +
    (cptState ? ` · ${cptState}` : "") +
    (info.timed ? " (timed 15-min units)" : "") +
    (asOf === "latest" ? " · newest file per contract" : (asOf ? ` · ${asOf}` : ""));
  // every section says it is loading THIS code — nothing from the previous
  // code stays on screen under the new heading while the answer is on its way
  $("#view-cpt h2").textContent = heading(null);
  body.innerHTML = "";
  stateEl.innerHTML = `<div class="loading">Loading ${esc(code)}</div>`;
  $("#cpt-payer-rank").innerHTML = `<div class="loading">Payers for ${esc(code)}</div>`;
  $("#cpt-hist").innerHTML = "";
  $("#cpt-trend").innerHTML = "";
  const p = new URLSearchParams({ grain: "tin" });
  if ($("#cpt-base-only").checked) p.set("modifier", "base");
  // one claim type at a time: with "base rates only" the ranking led with
  // hospital institutional lines ($870, $794 …) above every practice
  const cls = $("#cpt-class").value;
  if (cls) p.set("billing_class", cls);
  if (cptState) p.set("state", cptState);
  const trendP = new URLSearchParams({ cpt: code, grain: "tin" });
  if ($("#cpt-base-only").checked) trendP.set("modifier", "base");
  if (cls) trendP.set("billing_class", cls);
  if (cptState) trendP.set("state", cptState);
  // both start now; the ranking renders the moment it lands and the
  // month-by-month table fills in after — never one waiting on the other
  const trendReq = api(`/api/trend?${trendP}`, { signal: ctl.signal }).catch(() => null);
  let d;
  try {
    d = await api(`/api/code/${code}?${p}`, { signal: ctl.signal, bar: { view: "cpt", label: "Code comparison", keepMs: 300 } });
  } catch (e) {
    if (!current()) return;            // superseded by a newer click (it owns the gate)
    $("#cpt-payer-rank").innerHTML = "";
    stateEl.innerHTML = `<div class="empty">${esc(e.message)}</div>`;
    return done();
  }
  if (!current()) return;
  $("#view-cpt h2").textContent = heading(d.as_of);
  renderPayerRank($("#cpt-payer-rank"), d.payer_rank, d.mpfs_rate, d.payer_rank_basis,
                  d.medicare_anchor, d.medicare_refusal);
  renderHistogram($("#cpt-hist"), d.histogram, code);
  trendReq.then((trend) => { if (current() && trend) renderTrend($("#cpt-trend"), trend.rows); });
  if (!d.ranked.length) {
    stateEl.innerHTML = `<div class="empty"><h3>No rates for ${esc(code)} under these filters</h3></div>`;
    return done();
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
  done();
}

// Payer leaderboard for one code: who pays best, with spread + confidence.
function renderPayerRank(el, rows, mpfsRate, basis, anchor, refusal) {
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
    <div class="muted" style="margin-top:4px">One row per payer — the median across the distinct practices it prices for this code (each practice's own median first; same basis as the Benchmark: ${esc(basis || "")}).${anchor ? ` Medicare = ${esc(anchor)}.` : ""}${refusal ? ` <span class="warn-text">${esc(refusal)}</span>` : ""} ⚠ marks a thin sample (&lt;5 practices).</div></div>`;
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
  const [subjects, months, payers, peersets, plans] = await Promise.all([
    api("/api/benchmark/subjects").catch(() => null),
    api("/api/months").catch(() => null),
    api("/api/payers").catch(() => null),
    api("/api/peersets").catch(() => null),
    api("/api/plans").catch(() => null),
  ]);
  if (subjects) $("#b-subjects").innerHTML = subjectOptionsHtml(subjects);
  wireSubjectSearch("#b-subject", "#b-subjects");
  fillMonthSelect("#b-month", months, LATEST_MONTH_OPT);
  if (payers) $("#b-payer-chips").innerHTML = payerChipsHtml(payers);
  state.plans = plans;
  renderPlanField();
  $("#b-payer-chips").addEventListener("click", (ev) => {
    const b = ev.target.closest(".chip");
    if (b) { b.classList.toggle("on"); renderPlanField(); }
  });
  $("#b-plan").addEventListener("change", renderPlanField);
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
    // CF + state ride as query params: required for the official CMS RVU
    // files (locality-adjusted), ignored for the simple code,rate CSV
    const q = new URLSearchParams({
      cf: $("#mpfs-cf").value.trim(),
      state: $("#mpfs-state").value.trim(),
      locality: $("#mpfs-locality").value.trim(),
    });
    try {
      await api(`/api/mpfs/upload?${q}`, { method: "POST", body: fd });
      refreshMpfsStatus();
      refreshRcMpfs();
    } catch (err) { alert(err.message); }
    e.target.value = "";   // allow re-selecting the same file after a fix
  });
  $("#b-run").addEventListener("click", runBenchmark);
  $("#b-vol-medicare").addEventListener("click", () =>
    fillVolumesFromMedicare("#b-subject", "#b-volumes", "#b-vol-msg", "#b-vol-mult", "#b-state"));
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
  try { d = await postJson("/api/contract-gaps", { subject: payload.subject, market: payload.market }, { view: "benchmark", label: "Contract gaps", anchor: "#b-gaps-run" }); }
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

// The plan picker exists ONLY when the store has plan tags. A payer that
// publishes several plans is one whose pooled median blends networks that may
// price very differently, so say so where the user is about to quote a number.
function renderPlanField() {
  const field = $("#b-plan-field");
  const sel = $("#b-plan");
  if (!field || !sel) return;
  const cov = state.plans;
  if (!cov || !cov.loaded) { field.hidden = true; return; }
  field.hidden = false;
  const chosen = $$("#b-payer-chips .chip.on").map((c) => c.dataset.payer);
  // with payers selected, offer only THEIR plans; with none, offer all
  const shown = cov.plans.filter((p) => !chosen.length || chosen.includes(p.payer));
  const keep = sel.value;
  sel.innerHTML = `<option value="">all plans (pooled)</option>` +
    shown.map((p) => `<option value="${esc(p.plan_name)}">${esc(p.plan_name)}` +
      `${chosen.length === 1 ? "" : ` — ${esc(p.payer || "unknown payer")}`}</option>`).join("");
  // a plan that is no longer offered must not stay silently selected
  sel.value = shown.some((p) => p.plan_name === keep) ? keep : "";
  const blended = (cov.by_payer || []).filter(
    (p) => p.blended && (!chosen.length || chosen.includes(p.payer)));
  const note = $("#b-plan-note");
  if (sel.value) {
    note.textContent = `Scoped to ${sel.value} — only the files that plan is ` +
      `listed on. Rates a file shares with other plans are included, because ` +
      `the payer publishes one file for all of them.`;
  } else if (blended.length) {
    note.textContent = `${blended.map((p) => `${p.payer} (${p.n_plans} plans)`).join(", ")}` +
      ` publish more than one plan, so the pooled median blends networks that ` +
      `may price differently. Pick a plan to see one network on its own.`;
  } else {
    note.textContent = "";
  }
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
  market.peer_group = $("#b-peer").value;
  if ($("#b-disc").value) market.discipline = $("#b-disc").value;
  if ($("#b-state").value.trim()) market.state = $("#b-state").value.trim();
  if ($("#b-city").value.trim()) market.city = $("#b-city").value.trim();
  if ($("#b-pos").value) market.pos = $("#b-pos").value;
  if ($("#b-peerset").value) market.peer_set = $("#b-peerset").value;
  if ($("#b-plan") && $("#b-plan").value) market.plan = $("#b-plan").value;
  const volumes = parseVolumes("#b-volumes", "#b-vol-msg") || {};
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
  const isFresh = ticket("benchmark");
  try {
    data = Object.keys(payload.volumes).length
      ? await postJson("/api/benchmark/opportunity", payload, { view: "benchmark", label: "Benchmark", anchor: "#b-run" })
      : { benchmark: await postJson("/api/benchmark/market", payload, { view: "benchmark", label: "Benchmark", anchor: "#b-run" }) };
  } catch (e) {
    if (!isFresh()) return;
    out.innerHTML = `<div class="empty"><h3>Benchmark failed</h3>${esc(e.message)}</div>`; return;
  }
  if (!isFresh()) return;
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
    <div id="b-quality"></div>
    <div class="bench-note">${esc(bench.basis_note)}${bench.mpfs_source ? ` % of Medicare vs ${esc(bench.mpfs_source)}.` : ""}${bench.mpfs_refusal ? ` <span class="warn-text">${esc(bench.mpfs_refusal)}</span>` : ""}${spreadNote} Ghost rates: a published rate does not mean a
    peer bills that code — scoping the market to a discipline or code list mitigates this
    (the market definition above shows which filters were actually applied). A published rate
    is not proof a peer collects it — this is directional market positioning.</div>`;
  // use the market the SERVER echoed back, not the form: it is the scope the
  // numbers above were actually computed under
  renderBenchmarkQuality({ market: bench.market, subject: bench.subject });
}

// Two questions a client asks the moment they see a benchmark: "are the
// practices above me just bigger?" and "is this even a real contracted rate?"
// Both are answerable from the same data, so answer them here rather than
// leaving the consultant to field them unarmed.
async function renderBenchmarkQuality(payload) {
  const el = $("#b-quality");
  if (!el || !payload) return;
  const [size, types] = await Promise.all([
    postJson("/api/quality/size", { market: payload.market, subject: payload.subject })
      .catch(() => null),
    postJson("/api/quality/subject-types",
             { market: payload.market, subject: payload.subject }).catch(() => null),
  ]);
  if (!size && !types) return;
  const bands = (size?.bands || []).filter((b) => !b.thin);
  const bandRows = bands.map((b) => `<tr>
      <td>${esc(b.band)}</td>
      <td class="num">p${b.avg_percentile}</td>
      <td class="num">${money(b.median_rate)}</td>
      <td class="num muted">${fmtInt(b.n_practices)}</td></tr>`).join("");
  const typeWarn = types?.flag
    ? `<div class="warn-text" style="margin-top:8px">${esc(types.headline)}</div>`
    : types && types.derived_pct != null
      ? `<div class="muted" style="margin-top:8px">${esc(types.headline)}</div>` : "";
  el.innerHTML = `<div class="ov-block" style="margin-top:12px">
    <h3>"Aren't the practices above us just bigger?"</h3>
    <div class="muted" style="margin-bottom:6px">${esc(size?.headline || "")}</div>
    ${bandRows ? `<div class="tablewrap"><table>
      <thead><tr><th>Practice size</th><th class="num" title="the median, across the practices of that size, of each one's average position within each payer (a different measure from a practice's overall position)">Typical within-payer position</th>
      <th class="num">Median rate</th><th class="num">Practices</th></tr></thead>
      <tbody>${bandRows}</tbody></table></div>` : ""}
    ${typeWarn}
    <div class="muted" style="font-size:11.5px;margin-top:6px">
      ${esc(size?.note || "")}</div></div>`;
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

// The detail sentence of a refused request (FastAPI {"detail": …}), never
// the raw JSON wrapper.
async function refusalText(r) {
  const raw = await r.text();
  try { const j = JSON.parse(raw); return j.detail || j.error || raw; } catch (_) { return raw; }
}

// A GET export (CSV/ZIP, possibly many GB): the browser still streams it
// straight to disk — no in-memory blob — through a hidden frame. A download
// never "loads" the frame; a REFUSAL does (a JSON page), and its sentence is
// shown here. `location.href` replaced the whole dashboard with
// {"detail": …} and the user had to press Back (round 7 P1.21).
function downloadInFrame(url, bar) {
  const f = document.createElement("iframe");
  f.style.display = "none";
  if (bar) {
    // the page cannot see a frame's bytes arrive: the bar ends at "built —
    // your browser is downloading it" when the server finished the file
    const token = newToken();
    url += (url.includes("?") ? "&" : "?") + "progress=" + token;
    const jb = mountJobbar(bar, token);
    const poll = startPoll(token, jb, bar, (s) => {
      poll.stop();
      if (s.state === "done") jb.built(); else if (s.state === "stopped") jb.stopped(); else jb.failed("not built");
    });
    setTimeout(() => poll.stop(), 6 * 3600 * 1000);
  }
  f.addEventListener("load", () => {
    let text = "";
    try { text = (f.contentDocument && f.contentDocument.body) ? f.contentDocument.body.innerText : ""; }
    catch (_) { text = ""; }
    if (text && text.trim()) {
      let msg = text;
      try { const j = JSON.parse(text); msg = j.detail || j.error || text; } catch (_) { /* plain text */ }
      // the user pressed Stop: the bar already says "stopped" — no alert
      // (review 14 Group 3, P3)
      if (String(msg).trim().toLowerCase() !== "stopped") alert("The export was not built.\n\n" + msg);
    }
    setTimeout(() => f.remove(), 1000);
  });
  f.src = url;
  document.body.appendChild(f);
}

// "Save as PDF" (review 13 Group 6.1): every printable button gets a twin
// that runs the SAME handler with this set, so the PDF is built from exactly
// the payload the printable page would be — openReportWith then downloads
// `?format=pdf` instead of opening a tab. Set and cleared around a
// synchronous .click(): every handler reaches openReportWith before its
// first await. A window property, not a module `let`: openReportWith is
// also lifted alone into test pages (tests/mrfx/test_round7.py).
window.mrfxPdfBase = null;
const PDF_REPORTS = [
  ["#b-report", "pitch_report"], ["#b-negotiation", "negotiation_one_pager"],
  ["#ng-report", "payer_comparison"], ["#ng-proposal-report", "rate_proposal"],
  ["#rc-report", "rate_card"], ["#vv-report", "visit_value"],
  ["#ds-run", "practice_profile"], ["#mr-run", "market_report"],
];
function initPdfButtons() {
  for (const [sel, base] of PDF_REPORTS) {
    const main = $(sel);
    if (!main || document.getElementById(main.id + "-pdf")) continue;
    const b = document.createElement("button");
    b.className = "btn pdf-twin"; b.id = main.id + "-pdf"; b.type = "button";
    // the twin must not squeeze its row into three-line buttons: the row
    // wraps and both buttons keep their words on one line
    const row = main.parentElement;
    if (row && getComputedStyle(row).display === "flex") row.style.flexWrap = "wrap";
    main.style.whiteSpace = "nowrap";
    b.textContent = "Save as PDF";
    b.title = "The same report as a PDF file (printed by the built-in browser)";
    b.disabled = main.disabled;
    main.insertAdjacentElement("afterend", b);
    new MutationObserver(() => { b.disabled = main.disabled; })
      .observe(main, { attributes: true, attributeFilter: ["disabled"] });
    b.addEventListener("click", () => {
      window.mrfxPdfBase = base;
      try { main.click(); } finally { window.mrfxPdfBase = null; }
    });
  }
}

async function openReportWith(url, payload, onError, bar) {
  if (!payload) return;  // e.g. national-confirm cancelled
  if (window.mrfxPdfBase) {
    const name = `${window.mrfxPdfBase}_${new Date().toISOString().slice(0, 10)}.pdf`;
    const pb = bar ? { ...bar, label: bar.label + " (PDF)" } : bar;
    await postDownload(url + (url.includes("?") ? "&" : "?") + "format=pdf", payload, name, pb);
    return null;
  }
  const fail = (msg) => (onError ? onError(msg) : alert("Can't build this report.\n\n" + msg));
  // Open the tab SYNCHRONOUSLY, still inside the click's user-activation window
  // (any confirm() before this call is synchronous, so activation is preserved).
  // If we waited until after the await, a slow server render would push
  // window.open past the activation window and the browser would silently
  // block the popup — the report would just never appear.
  const w = window.open("about:blank", "_blank");
  let jb = null, poll = null;
  const headers = { "Content-Type": "application/json" };
  if (bar) {
    const token = newToken();
    headers["X-Mrfx-Progress"] = token;
    const b = { ...bar, tab: w };
    jb = mountJobbar(b, token);
    poll = startPoll(token, jb, b);
  }
  try {
    const r = await fetch(url, {
      method: "POST", headers,
      body: JSON.stringify(payload),
    });
    if (poll) poll.stop();
    if (!r.ok) {
      if (jb) { if (r.status === 409) jb.stopped(); else jb.failed("not built"); }
      if (w) w.close();
      if (r.status === 409) return null;
      // The refusals here are deliberate, plain-language guards (no state
      // scope, no rates for this subject) that the user is meant to ACT on —
      // so show the message, not the raw {"detail": …} JSON wrapper.
      fail(await refusalText(r));
      return;
    }
    const objUrl = URL.createObjectURL(await r.blob());
    if (jb) jb.done();                // 100 %: the page is in hand
    if (w) w.location = objUrl; else window.location = objUrl;  // popup blocked -> same tab
    setTimeout(() => URL.revokeObjectURL(objUrl), 60000);  // let the tab load, then free it
    return r;
  } catch (e) {
    if (poll) poll.stop();
    if (jb) jb.failed("the server restarted — run it again");
    if (w) w.close();
    fail("report failed: " + e.message);
  }
}

async function openPitchReport() {
  await openReportWith("/api/report/pitch", reportPayloadOrConfirmNational(), null, { view: "benchmark", label: "Pitch report", anchor: "#b-report" });
}

async function openNegotiationReport() {
  await openReportWith("/api/report/negotiation", reportPayloadOrConfirmNational(), null, { view: "benchmark", label: "Negotiation one-pager", anchor: "#b-negotiation" });
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
    refreshSubjectPickers("#ng-subjects", [{ sel: "#ng-month", prefix: LATEST_MONTH_OPT }])
      .then(() => { $("#ng-comps").innerHTML = $("#ng-subjects").innerHTML; });
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
  initHospital();      // the hospital comparison card lives on this tab
  initFloors();
  initLeverage();
  initVisitEconomics();
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
  $("#ng-vol-medicare").addEventListener("click", () =>
    fillVolumesFromMedicare("#ng-subject", "#ng-volumes", "#ng-vol-msg", null, "#ng-state"));
  $("#ng-target").addEventListener("change", () => {
    $("#ng-target-pct").style.display =
      $("#ng-target").value === "pct_medicare" ? "" : "none";
  });
  $("#ng-proposal-run").addEventListener("click", runProposal);
  $("#ng-proposal-report").addEventListener("click", () => {
    let p;
    try { p = proposalPayload(); }
    catch (e) { alert(e.message); return; }
    if (!p.market.state) {   // same national-scope confirm as the other reports
      if (!confirm("No state set — the proposal will pool every loaded state into one national comparison. Continue?")) return;
      p.market.allow_national = true;
    }
    openReportWith("/api/report/proposal", p, null, { view: "negotiate", label: "Printable proposal", anchor: "#ng-proposal-report" });
  });
  $("#ng-report").addEventListener("click", () => {
    if (!state.lastNegotiatePayload) return;
    const p = { ...state.lastNegotiatePayload,
                market: { ...state.lastNegotiatePayload.market } };
    if (!p.market.state) {
      if (!confirm("No state set — the report will pool every loaded state into one national comparison. Continue?")) return;
      p.market.allow_national = true;  // on the COPY: next click re-confirms
    }
    openReportWith("/api/report/payer-compare", p, null, { view: "negotiate", label: "Printable report", anchor: "#ng-report" });
  });
}

function renderCompChips() {
  $("#ng-comp-chips").innerHTML = state.ngComps.map((c) =>
    `<button class="chip on" data-comp="${esc(c)}" title="remove">${esc(c)} ✕</button>`).join("");
}

function proposalPayload() {
  const p = negotiatePayload();
  p.target = { kind: $("#ng-target").value };
  if (p.target.kind === "pct_medicare") p.target.value = Number($("#ng-target-pct").value);
  // "97110, 4200" per line — the same reader as every other volumes box
  const vols = parseVolumes("#ng-volumes", "#ng-vol-msg");
  if (vols) p.volumes = vols;
  return p;
}

async function runProposal() {
  const out = $("#ng-out");
  let payload;
  try { payload = proposalPayload(); }
  catch (e) { out.innerHTML = `<div class="empty">${esc(e.message)}</div>`; return; }
  out.classList.remove("empty");
  out.innerHTML = `<div class="loading">Building the proposal</div>`;
  let d;
  try { d = await postJson("/api/negotiate/proposal", payload, { view: "negotiate", label: "Proposal", anchor: "#ng-proposal-run" }); }
  catch (e) { out.innerHTML = `<div class="empty"><h3>Could not build</h3>${esc(e.message)}</div>`; return; }
  const s = d.summary;
  const rows = d.rows.map((r) => `<tr>
    <td>${esc(r.billing_code)}<div class="sub">${esc(r.description || "")}</div></td>
    <td class="num">${money(r.current)}</td>
    <td class="num"><b>${r.no_reference ? "—" : money(r.proposed)}</b></td>
    <td class="num">${r.delta == null ? "" : r.kept ? '<span class="muted">keep</span>'
      : `<span class="peer-up">+${fmtMoney(r.delta)} (${r.delta_pct > 0 ? "+" : ""}${r.delta_pct}%)</span>`}</td>
    <td class="num">${r.units == null ? "" : fmtInt(r.units)}</td>
    <td class="num">${r.annual_value == null ? "" : money(r.annual_value)}</td>
  </tr>`).join("");
  out.innerHTML = `
    <div class="rc-summary">Proposal basis: <b>${esc(d.target.label)}</b> — ${s.n_raised} of ${s.n_codes}
      codes raised, ${s.n_kept} kept${s.n_no_reference ? `, ${s.n_no_reference} with no reference rate` : ""}.
      ${s.total_annual_value != null ? `At your volumes this ask is worth <b>${money(s.total_annual_value)}</b>/yr <span class="muted">(every unit counted as billed to ${esc(d.payer)} — scale by its share of your volume)</span>.` : ""}
      Proposed rates never fall below current.</div>
    <div class="tablewrap"><table class="rc-table"><thead><tr>
      <th>Code</th><th class="num">Current</th><th class="num">Proposed</th>
      <th class="num">Change</th><th class="num">Units/yr</th><th class="num">Value/yr</th>
    </tr></thead><tbody>${rows}</tbody></table></div>`;
}

// the Negotiate tab's market for every panel on it — the leverage and "what a
// visit is worth" panels sent month + therapy only, so a name used in two
// states was the wrong practice there (review 10 item 2.9)
function ngMarket() {
  const market = { month: $("#ng-month").value || "latest", peer_group: $("#ng-peer").value };
  if ($("#ng-state").value.trim()) market.state = $("#ng-state").value.trim().toUpperCase();
  if ($("#ng-disc").value) market.discipline = $("#ng-disc").value;
  return market;
}

function negotiatePayload() {
  const subject = $("#ng-subject").value.trim();
  const payer = $("#ng-payer").value;
  const month = $("#ng-month").value || "latest"; // empty = newest rate per contract
  if (!subject) throw new Error("enter your practice (subject)");
  if (!payer) throw new Error("pick the payer you're negotiating with");
  const market = { month, peer_group: $("#ng-peer").value };
  if ($("#ng-state").value.trim()) market.state = $("#ng-state").value.trim().toUpperCase();
  if ($("#ng-disc").value) market.discipline = $("#ng-disc").value;
  return { subject, payer, market, comparables: state.ngComps.slice() };
}

// The named clinics this payer pays more for the same codes (0.8.6) — the
// evidence a rate request cites. City first (5+ comparable clinics), else state.
async function loadPaidMore(payload) {
  const box = $("#ng-paidmore");
  // a newer Compare (another payer) wins: a slow answer for the previous
  // one must not land under the new results
  const seq = (state.paidMoreSeq = (state.paidMoreSeq || 0) + 1);
  box.innerHTML = `<div class="loading">Finding the clinics ${esc(payload.payer)} pays more</div>`;
  let d;
  try { d = await postJson("/api/negotiate/paid-more", payload); }
  catch (e) {
    if (seq === state.paidMoreSeq) box.innerHTML = `<div class="muted">Clinics paid more: ${esc(e.message)}</div>`;
    return;
  }
  if (seq !== state.paidMoreSeq) return;
  const codes = d.codes.map((c) => `<tr><td><b>${esc(c.code)}</b> <span class="sub">${esc(c.description || "")}</span></td>
      <td class="num">${money(c.your_rate)}</td><td class="num">${c.median == null ? "–" : money(c.median)}</td>
      <td class="num">${c.highest == null ? "–" : money(c.highest)}</td>
      <td class="num">${fmtInt(c.paid_more)} <span class="sub">of ${fmtInt(c.practices)}</span></td></tr>`).join("");
  const rows = d.clinics.map((x) => {
    const where = (x.cities || []).join(", ");
    const tag = NAME_SOURCE_TAG[x.name_source] ? ` <span class="sub">· ${esc(NAME_SOURCE_TAG[x.name_source])}</span>` : "";
    // the biggest gaps first; the CSV carries every code
    const paid = x.rates.filter((r) => r.gap > 0).sort((a, b) => b.gap - a.gap || (a.code < b.code ? -1 : 1));
    const rates = paid.slice(0, 8)
      .map((r) => `${esc(r.code)} ${money(r.rate)} <span class="peer-up">+${money(r.gap)}</span>`).join(" · ")
      + (paid.length > 8 ? ` · <i>+${paid.length - 8} more in the CSV</i>` : "");
    return `<tr><td class="num">${x.rank}</td>
      <td style="white-space:normal"><b title="${esc(x.type_reason || "")}">${esc(x.name)}</b>${tag}<div class="sub">${esc(where)}</div>
        ${rates ? `<div class="sub" style="margin-top:3px">Paid more: ${rates}</div>` : ""}</td>
      <td>${esc(x.tin || "")}</td>
      <td class="num">${fmtInt(x.codes_paid_more)} <span class="sub">of ${fmtInt(x.codes_compared)}</span></td>
      <td class="num">${x.median_gap_pct == null ? "–" : "+" + x.median_gap_pct + "%"}</td></tr>`;
  }).join("");
  box.innerHTML = `<div class="validate-card">
    <h3>Clinics ${esc(d.payer)} pays more — ${esc(d.where)}</h3>
    <div class="rc-summary">${esc(d.headline)}</div>
    <div class="muted" style="margin:6px 0">Compared with ${esc(d.compared_with)} in ${esc(d.where)}${
      d.geography === "state" && d.city ? ` (fewer than 5 in ${esc(d.city)}, so the whole state)` : ""}.
      <button class="btn" id="ng-paidmore-csv" style="margin-left:8px">Download CSV</button></div>
    <div class="tablewrap" style="max-height:40vh"><table class="rc-table"><thead><tr><th>Your code</th>
      <th class="num">Your rate</th><th class="num">Their median</th><th class="num">Highest</th>
      <th class="num">Clinics paid more</th></tr></thead><tbody>${codes}</tbody></table></div>
    ${d.clinics.length ? `<div class="tablewrap" style="max-height:60vh;margin-top:10px"><table class="rc-table">
      <thead><tr><th class="num">#</th><th>Clinic</th><th>Tax ID</th><th class="num">Codes paid more</th>
      <th class="num">Median gap</th></tr></thead>
      <tbody>${rows}</tbody></table></div>` : ""}
    ${d.clinics.length < d.clinics_paid_more ? `<div class="muted" style="margin-top:4px">Showing the first
      ${fmtInt(d.clinics.length)} of ${fmtInt(d.clinics_paid_more)} — Download CSV for every one.</div>` : ""}
    <div class="muted" style="font-size:11.5px;margin-top:6px">${esc(d.note)}</div></div>`;
  $("#ng-paidmore-csv").addEventListener("click", async () => {
    try {
      const r = await fetch("/api/negotiate/paid-more.csv", { method: "POST",
        headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
      if (!r.ok) { alert("CSV export failed.\n\n" + await refusalText(r)); return; }
      const a = document.createElement("a");
      a.href = URL.createObjectURL(await r.blob());
      a.download = `paid_more_${String(d.payer).replace(/[^A-Za-z0-9]+/g, "_")}.csv`;
      a.click();
      setTimeout(() => URL.revokeObjectURL(a.href), 60000);
    } catch (e) { alert("CSV export failed: " + e.message); }
  });
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
  const isFresh = ticket("negotiate");
  try { d = await postJson("/api/negotiate/compare", payload, { view: "negotiate", label: "Comparison", anchor: "#ng-run" }); }
  catch (e) {
    if (!isFresh()) return;
    out.innerHTML = `<div class="empty"><h3>Couldn't compare</h3>${esc(e.message)}</div>`; return;
  }
  if (!isFresh()) return;
  state.lastNegotiatePayload = payload;
  $("#ng-report").disabled = false;
  loadPaidMore(payload);
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
  const [subjects, months, payers, states] = await Promise.all([
    api("/api/benchmark/subjects").catch(() => null),
    api("/api/months").catch(() => null),
    api("/api/payers").catch(() => null),
    api("/api/states").catch(() => null),
  ]);
  if (subjects) $("#rc-subjects").innerHTML = subjectOptionsHtml(subjects);
  if (states) $("#rc-states").innerHTML = (states.states || []).map((s) => `<option value="${esc(s)}">`).join("");
  wireSubjectSearch("#rc-subject", "#rc-subjects");
  fillMonthSelect("#rc-month", months, LATEST_MONTH_OPT);
  if (payers) $("#rc-payer-chips").innerHTML = payerChipsHtml(payers);
  $("#rc-payer-chips").addEventListener("click", (ev) => {
    const b = ev.target.closest(".chip");
    if (b) b.classList.toggle("on");
  });
  $("#rc-run").addEventListener("click", buildRatecard);
  $("#rc-report").addEventListener("click", () =>
    openReportWith("/api/report/ratecard", state.lastRatecardPayload, null, { view: "ratecard", label: "Printable rate card", anchor: "#rc-report" }));
  $("#rc-csv").addEventListener("click", downloadRatecardCsv);
  $("#rc-bundle").addEventListener("click", downloadTrackerBundle);
  initStandardVisit();
  $("#vv-run").addEventListener("click", showVisitMarket);
  $("#vv-report").addEventListener("click", () =>
    openReportWith("/api/report/visitvalue", state.lastVisitMarketPayload, null, { view: "ratecard", label: "Visit value report", anchor: "#vv-report" }));
  $("#vv-csv").addEventListener("click", downloadVisitMarketCsv);
  initVisitPracticeLists();
}

/* ---- visit value in this market (mrfx/visit_market.py) ------------------ */
// The visit (preset or edited lines) the visit calculator shows.
function visitBasket() {
  const key = $("#sv-preset") && $("#sv-preset").value;
  if (key) return { preset: key };
  const lines = ($("#sv-lines") && $("#sv-lines").value.trim()) || "";
  return lines ? { lines } : { preset: "ortho" };
}

// Confined to a State and a Discipline (both required); the practice is
// optional — the market numbers are the same with or without one.
function visitMarketPayload() {
  const st = $("#rc-state").value.trim().toUpperCase();
  const disc = $("#rc-disc").value;
  if (!st) throw new Error("pick a State above — a visit's value is a market figure, and a market is one state");
  if (!disc) throw new Error("pick a Discipline above (PT, OT or SLP) — a visit is priced on one discipline's rates");
  const market = { month: $("#rc-month").value || "latest", state: st, discipline: disc,
                   payers: $$("#rc-payer-chips .chip.on").map((c) => c.dataset.payer),
                   peer_group: $("#rc-peer").value };
  const subject = $("#rc-subject").value.trim();
  return { market, ...visitBasket(), ...(subject ? { subject } : {}) };
}

const signedMoney = (v) => (v == null ? "–" : (v > 0 ? "+" : v < 0 ? "-" : "±") + "$" + fmtMoney(Math.abs(v)));
const signedPct1 = (v) => (v == null ? "–" : `${v < 0 ? "-" : "+"}${Math.abs(v).toFixed(1)}%`);

async function showVisitMarket() {
  const out = $("#vv-out");
  state.lastVisitMarketPayload = null;
  $("#vv-report").disabled = true;
  $("#vv-csv").disabled = true;
  let payload;
  try { payload = visitMarketPayload(); }
  catch (e) { $("#vv-msg").textContent = ""; out.innerHTML = `<div class="muted">${esc(e.message)}</div>`; return; }
  $("#vv-msg").textContent = "pricing the market…";
  let d;
  const isFresh = ticket("visit-market");
  try { d = await postJson("/api/schedule/visit/market", payload, { view: "ratecard", label: "Visit value in this market", anchor: "#vv-run" }); }
  catch (e) {
    if (!isFresh()) return;
    $("#vv-msg").textContent = ""; out.innerHTML = `<div class="muted">${esc(e.message)}</div>`; return;
  }
  if (!isFresh()) return;
  $("#vv-msg").textContent = "";
  state.lastVisitMarketPayload = payload;
  $("#vv-report").disabled = false;
  $("#vv-csv").disabled = false;
  out.innerHTML = renderVisitMarket(d);
}

function renderVisitMarket(d) {
  const subj = !!d.subject;
  const head = `<th>Payer</th><th class="num">Practices</th><th class="num">Average (mean)</th>
    <th class="num">Median</th><th class="num">P25</th><th class="num">P75</th>
    <th class="num" title="practice-payer pairs left out because the payer does not price every code of the visit">Partial visits left out</th>${
    subj ? `<th class="num">This practice</th><th class="num" title="mid-rank among the payer's practices, this practice included">Percentile</th>
    <th class="num">Quartile</th><th class="num">vs average</th><th class="num">vs median</th>` : ""}<th></th>`;
  const listBtn = (payer) => `<td><button class="btn vv-list-btn" data-vv-payer="${esc(payer)}" aria-expanded="false"
      title="list every practice in this row, highest visit value first">▸ Practices</button></td>`;
  const row = (r, all) => {
    const s = r.subject;
    let subjCells = "";
    if (subj) {
      if (!s) subjCells = `<td class="num muted" colspan="5">no complete visit with this payer</td>`;
      else if (s.percentile == null) subjCells = `<td class="num">${money(s.value)}</td><td class="num muted" colspan="4">not ranked — too few practices</td>`;
      else subjCells = `<td class="num"><b>${money(s.value)}</b></td>
        <td class="num">p${s.percentile} <span class="sub">of ${fmtInt(s.ranked_among)}</span></td>
        <td class="num">${esc(s.quartile)}</td>
        <td class="num"><span class="${s.vs_mean > 0 ? "peer-up" : s.vs_mean < 0 ? "peer-down" : ""}">${signedMoney(s.vs_mean)}</span> <span class="sub">${signedPct1(s.vs_mean_pct)}</span></td>
        <td class="num"><span class="${s.vs_median > 0 ? "peer-up" : s.vs_median < 0 ? "peer-down" : ""}">${signedMoney(s.vs_median)}</span> <span class="sub">${signedPct1(s.vs_median_pct)}</span></td>`;
    }
    const excl = r.incomplete_excluded == null ? "" : fmtInt(r.incomplete_excluded);
    if (r.suppressed)
      return `<tr${all ? ' class="best-row"' : ""}><td>${esc(r.payer)}</td><td class="muted" colspan="5">fewer than ${d.min_practices} practices — not shown</td><td class="num">${excl}</td>${subjCells}<td></td></tr>`;
    return `<tr${all ? ' class="best-row"' : ""}><td>${all ? "<b>All payers</b>" : esc(r.payer)}</td>
      <td class="num">${fmtInt(r.practices)}</td><td class="num">${money(r.mean)}</td>
      <td class="num"><b>${money(r.median)}</b></td><td class="num">${money(r.p25)}</td>
      <td class="num">${money(r.p75)}</td><td class="num">${excl}</td>${subjCells}${listBtn(all ? "All payers" : r.payer)}</tr>`;
  };
  const body = d.rows.map((r) => row(r, false)).join("") + row(d.all_payers, true);
  return `<div class="rc-summary">${esc(d.headline)}</div>
    <div class="muted" style="margin:6px 0">${esc(d.state)} · ${esc(d.discipline)} · as of ${esc(d.as_of)}${d.as_of_rule ? ` (${esc(d.as_of_rule)})` : ""} ·
      ${esc(d.label)} — ${esc(d.basket)} = <b>${d.units} units</b>${
      d.payers_filter.length ? ` · payers: ${esc(d.payers_filter.join(", "))}` : ""}</div>
    ${(d.flags || []).map((f) => `<div class="warn-text" style="margin-top:4px">${esc(f)}</div>`).join("")}
    <div class="tablewrap" style="max-height:75vh;margin-top:8px"><table class="rc-table vv-table"><thead><tr>${head}</tr></thead>
      <tbody>${body}</tbody></table></div>
    <div class="muted" style="font-size:11.5px;margin-top:6px">${fmtInt(d.incomplete_excluded)} practice-payer pair(s) left out as partial visits${
      d.suppressed_payers.length ? `; not shown (under ${d.min_practices} practices): ${esc(d.suppressed_payers.join(", "))}` : ""}.
      ${esc(d.note)}${d.subject_note ? " " + esc(d.subject_note) : ""} Basis: ${esc(d.basis_note)}</div>`;
}

async function downloadVisitMarketCsv() {
  if (!state.lastVisitMarketPayload) return;
  try {
    const r = await fetch("/api/schedule/visit/market.csv", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(state.lastVisitMarketPayload),
    });
    if (!r.ok) { alert("CSV export failed.\n\n" + await refusalText(r)); return; }
    const a = document.createElement("a");
    a.href = URL.createObjectURL(await r.blob());
    a.download = "visit_value.csv";
    a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 60000);
  } catch (e) { alert("CSV export failed: " + e.message); }
}

/* ---- the practices behind one Visit-value row, ranked (visit_market.py) -- */
const VV_PAGE = 50;

// where a practice's name came from (mrfx/naming.py name_source)
const NAME_SOURCE_TEXT = {
  nppes_org: "the legal business name of its organization NPIs (NPPES)",
  cms_dac: "the group most of its clinicians are enrolled with (CMS Doctors and Clinicians file)",
  solo_npi: "its one provider (a solo practice)",
  unnamed_group: "no organization name found — labelled by clinician count and main city",
  no_npi: "no NPI is published under this tax ID",
  pending: "loaded since names were last resolved — refreshed within minutes",
};
const NAME_SOURCE_TAG = { cms_dac: "name: CMS file", solo_npi: "solo provider",
  unnamed_group: "no org name found", pending: "name pending" };

function vvPracticeRow(x, perPayer) {
  const where = [(x.cities || []).join(", "), x.state].filter(Boolean).join(" · ");
  // one NPI on the row (the organisation's own when it has one — listed
  // first by the server), the others on hover; the CSV carries them all
  const list = x.npis || [];
  const others = list.length - 1 + (x.npis_more || 0);
  const tip = list.slice(1).map((n) => `${n.npi}${n.name ? " " + n.name : ""}`).join("\n")
    + (x.npis_more ? `\n+${x.npis_more} more (in the CSV)` : "");
  const npis = list.length ? `<span style="font-family:ui-monospace,monospace">${esc(list[0].npi)}</span>${
    list[0].name ? ` <span class="sub">${esc(list[0].name)}</span>` : ""}${
    others > 0 ? ` <span class="sub" title="${esc(tip)}" style="cursor:help;text-decoration:underline dotted">+${fmtInt(others)} more</span>` : ""}` : "";
  const made = perPayer
    ? (x.rates || []).map((r) => `${esc(r.code)}${r.units > 1 ? "×" + r.units : ""} ${money(r.rate)}`).join(" · ")
    : `median of ${x.payers} payer${x.payers === 1 ? "" : "s"} (${money(x.low)} – ${money(x.high)})`;
  const cls = x.vs_median > 0 ? "peer-up" : x.vs_median < 0 ? "peer-down" : "";
  return `<tr class="${x.is_subject ? "best-row" : ""}">
    <td class="num">${fmtInt(x.rank)}</td>
    <td><b title="named by ${esc(NAME_SOURCE_TEXT[x.name_source] || x.name_source || "")}">${esc(x.name)}</b>${
      x.is_subject ? ' <span class="sub">(this practice)</span>' : ""}${
      NAME_SOURCE_TAG[x.name_source] ? ` <span class="sub" title="${esc(NAME_SOURCE_TEXT[x.name_source])}">· ${esc(NAME_SOURCE_TAG[x.name_source])}</span>` : ""}${
      where ? `<div class="sub">${esc(where)}</div>` : ""}</td>
    <td>${esc(x.tin || "")}</td>
    <td>${npis || '<span class="sub">–</span>'}</td>
    <td class="num">${x.therapy_npi_count == null ? "–" : fmtInt(x.therapy_npi_count)} <span class="sub">of ${x.npi_count == null ? "–" : fmtInt(x.npi_count)}</span></td>
    <td class="num"><b>${money(x.value)}</b></td>
    <td class="num"><span class="${cls}">${signedMoney(x.vs_median)}</span> <span class="sub">${signedPct1(x.vs_median_pct)}</span></td>
    <td class="num">${x.percentile == null ? "–" : "p" + x.percentile}</td>
    <td class="sub">${made}</td></tr>`;
}

async function vvLoadPractices(box, append) {
  const offset = append ? Number(box.dataset.shown || 0) : 0;
  const payload = { ...state.lastVisitMarketPayload, payer: box.dataset.payer, offset,
                    limit: VV_PAGE, order: box.dataset.order || "desc" };
  if (!append) box.innerHTML = `<div class="loading">Listing practices</div>`;
  let d;
  try { d = await postJson("/api/schedule/visit/market/practices", payload); }
  catch (e) { box.innerHTML = `<div class="muted">${esc(e.message)}</div>`; return; }
  const perPayer = d.payer !== "All payers";
  const rows = d.rows.map((x) => vvPracticeRow(x, perPayer)).join("");
  const shown = offset + d.rows.length;
  box.dataset.shown = String(shown);
  const more = shown < d.total
    ? `<button class="btn" data-vv-more>Show ${fmtInt(Math.min(VV_PAGE, d.total - shown))} more</button>` : "";
  const foot = `<span class="muted">${fmtInt(shown)} of ${fmtInt(d.total)} shown</span> ${more}`;
  if (append) {
    box.querySelector("tbody").insertAdjacentHTML("beforeend", rows);
    box.querySelector(".vv-plist-foot").innerHTML = foot;
    return;
  }
  const desc = d.order !== "asc";
  box.innerHTML = `<div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin:4px 0 6px">
      <b>${fmtInt(d.total)} practice${d.total === 1 ? "" : "s"} — ${esc(d.payer)}</b>
      <span class="muted">${esc(d.basket)} · median ${money(d.median)}</span>
      <button class="btn${desc ? " primary" : ""}" data-vv-order="desc">Highest first</button>
      <button class="btn${desc ? "" : " primary"}" data-vv-order="asc">Lowest first</button>
      <button class="btn" data-vv-csv>Download this list (CSV)</button></div>
    <div class="tablewrap" style="max-height:60vh"><table class="rc-table">
      <thead><tr><th class="num">#</th><th>Practice</th><th>Tax ID</th><th>NPIs</th>
        <th class="num" title="therapy NPIs (PT/OT/SLP taxonomy) of all the NPIs this tax ID bills under">Therapists</th>
        <th class="num">Visit value</th><th class="num">vs median</th>
        <th class="num" title="mid-rank among the other practices in this row">Percentile</th>
        <th>${perPayer ? "Rates in this visit" : "Across payers"}</th></tr></thead>
      <tbody>${rows}</tbody></table></div>
    <div class="vv-plist-foot" style="margin-top:6px">${foot}</div>
    <div class="muted" style="font-size:11.5px;margin-top:4px">${esc(d.note)}</div>`;
}

async function vvTogglePractices(btn) {
  const tr = btn.closest("tr");
  const next = tr.nextElementSibling;
  if (next && next.classList.contains("vv-detail")) {
    next.remove();
    btn.textContent = "▸ Practices";
    btn.setAttribute("aria-expanded", "false");
    return;
  }
  const cols = tr.closest("table").tHead.rows[0].cells.length;
  const det = document.createElement("tr");
  det.className = "vv-detail";
  det.innerHTML = `<td colspan="${cols}"><div class="vv-plist" data-order="desc"></div></td>`;
  const box = det.querySelector(".vv-plist");
  box.dataset.payer = btn.dataset.vvPayer;
  // held to the visible width of the table's scroll box and pinned at its
  // left edge, so rank, practice and visit value stay in view when the
  // payer table itself is wider than the panel (the subject's columns)
  const wrap = tr.closest(".tablewrap");
  if (wrap) {
    box.style.position = "sticky";
    box.style.left = "0";
    box.style.width = `${Math.max(320, wrap.clientWidth - 32)}px`;
  }
  tr.after(det);
  btn.textContent = "▾ Practices";
  btn.setAttribute("aria-expanded", "true");
  await vvLoadPractices(det.querySelector(".vv-plist"), false);
}

async function vvDownloadPractices(box) {
  if (!state.lastVisitMarketPayload) return;
  try {
    const r = await fetch("/api/schedule/visit/market/practices.csv", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ...state.lastVisitMarketPayload, payer: box.dataset.payer }),
    });
    if (!r.ok) { alert("CSV export failed.\n\n" + await refusalText(r)); return; }
    const a = document.createElement("a");
    a.href = URL.createObjectURL(await r.blob());
    a.download = `visit_value_practices_${box.dataset.payer.replace(/[^A-Za-z0-9]+/g, "_")}.csv`;
    a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 60000);
  } catch (e) { alert("CSV export failed: " + e.message); }
}

function initVisitPracticeLists() {
  $("#vv-out").addEventListener("click", (e) => {
    const toggle = e.target.closest("[data-vv-payer]");
    if (toggle) { vvTogglePractices(toggle); return; }
    const box = e.target.closest(".vv-plist");
    if (!box) return;
    const order = e.target.closest("[data-vv-order]");
    if (order) {
      if (box.dataset.order !== order.dataset.vvOrder) {
        box.dataset.order = order.dataset.vvOrder;
        vvLoadPractices(box, false);
      }
      return;
    }
    if (e.target.closest("[data-vv-more]")) { vvLoadPractices(box, true); return; }
    if (e.target.closest("[data-vv-csv]")) vvDownloadPractices(box);
  });
}

/* ---- one standard visit, priced per payer (mrfx/visit.py) --------------- */
const visitLinesText = (lines) => lines.map((l) => `${l.code}, ${l.minutes}`).join("\n");

async function initStandardVisit() {
  const d = await api("/api/schedule/visit/presets").catch(() => null);
  if (!d) return;
  state.visitPresets = Object.fromEntries(d.presets.map((p) => [p.key, p]));
  $("#sv-preset").innerHTML = d.presets.map((p) =>
    `<option value="${esc(p.key)}"${p.key === d.default ? " selected" : ""}>${esc(p.label)}</option>`).join("") +
    '<option value="">Custom — edit the codes and minutes below</option>';
  const fill = () => {
    const p = state.visitPresets[$("#sv-preset").value];
    if (p) $("#sv-lines").value = visitLinesText(p.lines);
  };
  fill();
  $("#sv-preset").addEventListener("change", fill);
  $("#sv-lines").addEventListener("input", () => {
    const p = state.visitPresets[$("#sv-preset").value];
    if (p && $("#sv-lines").value.trim() !== visitLinesText(p.lines)) $("#sv-preset").value = "";
  });
  $("#sv-run").addEventListener("click", priceStandardVisit);
}

async function priceStandardVisit() {
  const out = $("#sv-out");
  let payload;
  try { payload = ratecardPayload(); }
  catch (e) { $("#sv-msg").textContent = e.message; return; }
  const key = $("#sv-preset").value;
  if (key) payload.preset = key; else payload.lines = $("#sv-lines").value;
  $("#sv-msg").textContent = "pricing…";
  let d;
  const isFresh = ticket("visit");
  try { d = await postJson("/api/schedule/visit", payload); }
  catch (e) {
    if (!isFresh()) return;
    $("#sv-msg").textContent = ""; out.innerHTML = `<div class="muted">${esc(e.message)}</div>`; return;
  }
  if (!isFresh()) return;
  $("#sv-msg").textContent = "";
  const lines = d.lines.map((l) => `<b>${esc(l.code)}</b> ${esc(l.description)} — ${
      l.timed ? `${l.minutes} min → ${l.units} unit${l.units === 1 ? "" : "s"}` : "untimed, 1 unit"}`).join("<br>");
  const rows = d.rows.map((r, i) => `<tr class="${i === 0 && r.complete ? "best-row" : ""}">
      <td>${esc(r.payer)}</td>
      <td class="num"><b>${r.complete ? money(r.visit_value) : "–"}</b></td>
      <td class="sub">${r.parts.map((p) => `${esc(p.code)} ${p.units}×${money(p.rate)}`).join(" · ")}</td>
      <td class="sub">${r.complete ? "every code priced" :
        `<span class="warn-text">no published rate for ${esc(r.missing_codes.join(", "))}</span> — ${
          money(r.priced_value)} covers ${r.priced_units} of ${d.units} units`}</td></tr>`).join("");
  out.innerHTML = `<div class="rc-summary">${esc(d.headline)}</div>
    <div class="muted" style="margin:6px 0">${esc(d.label)} — ${d.total_timed_minutes} timed minutes →
      <b>${d.units} units</b> (CMS 8-minute rule).<br>${lines}</div>
    ${d.flags.map((f) => `<div class="warn-text" style="margin-top:4px">${esc(f)}</div>`).join("")}
    ${d.rows.length ? `<div class="tablewrap" style="max-height:40vh;margin-top:8px"><table><thead><tr>
      <th>Payer</th><th class="num">Per visit</th><th>Units × rate</th><th>Status</th></tr></thead>
      <tbody>${rows}</tbody></table></div>` : ""}
    <div class="muted" style="font-size:11.5px;margin-top:6px">Visit: ${esc(d.basis)}. ${esc(d.units_rule)} ${esc(d.note)}</div>`;
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
  // scope the per-cell peer comparison (blank = pooled, and the printable card
  // prints a national-comparison banner saying so)
  if ($("#rc-state").value.trim()) market.state = $("#rc-state").value.trim().toUpperCase();
  market.peer_group = $("#rc-peer").value;
  // the printable card and its CSV carry "Visit value in this market" when
  // its two required scopes are set (review 12 Group 3)
  const vm = market.state && market.discipline ? { visit_market: true, ...visitBasket() } : {};
  return { subject, market, ...vm };
}

async function buildRatecard() {
  const out = $("#rc-out");
  state.lastRatecardPayload = null;
  $("#rc-report").disabled = true;
  $("#rc-csv").disabled = true;
  $("#rc-bundle").disabled = true;
  let payload;
  try { payload = ratecardPayload(); }
  catch (e) { out.innerHTML = `<div class="empty">${esc(e.message)}</div>`; return; }
  out.innerHTML = `<div class="loading">Building rate card</div>`;
  let data;
  const isFresh = ticket("ratecard");
  try { data = await postJson("/api/schedule/fee", payload, { view: "ratecard", label: "Rate card", anchor: "#rc-run" }); }
  catch (e) {
    if (!isFresh()) return;
    out.innerHTML = `<div class="empty"><h3>Could not build</h3>${esc(e.message)}</div>`; return;
  }
  if (!isFresh()) return;
  state.lastRatecardPayload = payload;
  // Only offer the client-facing deliverables when there IS a card. With zero
  // codes the server now refuses to render one, so enabling the buttons would
  // only produce an error alert — and before that guard existed it produced a
  // branded, empty rate card the user could have sent to a client.
  const hasCard = ((data.fee_schedule || {}).codes || []).length > 0;
  $("#rc-report").disabled = !hasCard;
  $("#rc-csv").disabled = !hasCard;
  $("#rc-bundle").disabled = !hasCard;
  renderRatecard(out, data.fee_schedule, data.scorecard);
  if (hasCard && $("#sv-preset").options.length) priceStandardVisit();
  else $("#sv-out").innerHTML = "";
}

const pctOrDash = (v) => (v == null ? "–" : `${v}%`);

function renderRatecard(out, fs, sc) {
  out.classList.remove("empty");  // real table, not the placeholder
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
      // peer comparison: what THIS payer pays your peers for this code
      let peer = "";
      if (v.market_median != null) {
        const vs = v.vs_market_pct;
        const tag = vs == null ? "" : vs === 0 ? " · even"
          : ` · <span class="${vs > 0 ? "peer-up" : "peer-down"}">${vs > 0 ? "+" : ""}${vs}% vs peers</span>`;
        // The peer count and the thin flag must be VISIBLE, not tucked into a
        // tooltip: the printable report shows both, so a screenshotted dashboard
        // otherwise presents the same number with none of its caveats.
        const thin = v.n_peers < THIN_PEERS ? ` <span class="thin">thin</span>` : "";
        peer = `<div class="sub" title="what ${esc(p)} pays your peers for this code (you excluded)">peer $${fmtMoney(v.market_median)}${tag} · ${fmtInt(v.n_peers)} peer${v.n_peers === 1 ? "" : "s"}${thin}</div>`;
      }
      return `<td class="num${top}">$${fmtMoney(v.rate)}${mc}${peer}</td>`;
    }).join("");
    return `<tr><td>${esc(e.billing_code)}<div class="sub">${esc(e.description || "")}</div></td>${cells}</tr>`;
  }).join("");
  const bestLine = sc.best_payer ? `Best-paying payer: <b>${esc(sc.best_payer)}</b>. ` : "";
  out.innerHTML = `
    <div class="rc-summary">${bestLine}Payers ranked by % of the best payer's rate on ${(sc.shared_codes || []).length ? `the ${sc.shared_codes.length} code(s) every payer prices` : "the codes at least two of them price"}${mp ? " (% of Medicare is shown, not ranked: each payer's own codes differ)" : ""}. Each cell shows your rate, then <b>peer $</b> — what that payer pays your peers for the same code — and how you compare.</div>
    <h3>Payer scorecard — who pays best</h3>
    <div class="tablewrap"><table class="rc-table"><thead><tr>${scHead}</tr></thead><tbody>${scRows}</tbody></table></div>
    <h3 style="margin-top:18px">Fee schedule</h3>
    <div class="tablewrap"><table class="rc-table"><thead><tr>${feeHead}</tr></thead><tbody>${feeRows}</tbody></table></div>`;
}

// The Order & Referring Tracker is NPI-native and knows the referral side;
// this app is TIN-grained and knows the contract side. The bundle leads with a
// paste-ready NPI list so the two can be joined on one practice.
async function downloadTrackerBundle() {
  if (!state.lastRatecardPayload) return;
  const btn = $("#rc-bundle");
  const label = btn.textContent;
  btn.disabled = true; btn.textContent = "Building…";
  try {
    const r = await fetch("/api/report/org-bundle.zip", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(state.lastRatecardPayload),
    });
    if (!r.ok) {
      const raw = await r.text();
      let msg = raw;
      try { msg = JSON.parse(raw).detail || raw; } catch (_) { /* not JSON */ }
      alert("Couldn't build the bundle.\n\n" + msg);
      return;
    }
    const cd = r.headers.get("Content-Disposition") || "";
    const m = cd.match(/filename="?([^";]+)"?/);
    const a = document.createElement("a");
    a.href = URL.createObjectURL(await r.blob());
    a.download = m ? m[1] : "mrfx_org_bundle.zip";
    a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 60000);
  } catch (e) {
    alert("Couldn't build the bundle: " + e.message);
  } finally {
    btn.disabled = false; btn.textContent = label;
  }
}

async function downloadRatecardCsv() {
  if (!state.lastRatecardPayload) return;
  try {
    const r = await fetch("/api/schedule/fee.csv", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(state.lastRatecardPayload),
    });
    if (!r.ok) { alert("CSV export failed.\n\n" + await refusalText(r)); return; }
    const a = document.createElement("a");
    a.href = URL.createObjectURL(await r.blob());
    a.download = "rate_card.csv";
    a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 60000);
  } catch (e) { alert("CSV export failed: " + e.message); }
}

/* =======================================================================
   CLIENTS — the consultant's book: per saved client, what changed.
   Every cell reuses its own tab's function server-side, so a digest number
   can never disagree with the drill-down the click lands on.
   ======================================================================= */

let clientsInit = false;
async function initClients() {
  if (clientsInit) { refreshSubjectPickers("#cl-subjects", []); loadClientDigest(); return; }
  clientsInit = true;
  const subjects = await api("/api/benchmark/subjects").catch(() => null);
  if (subjects) $("#cl-subjects").innerHTML = subjectOptionsHtml(subjects);
  wireSubjectSearch("#cl-subject", "#cl-subjects");
  $("#cl-add").addEventListener("click", addClient);
  $("#cl-subject").addEventListener("keydown", (e) => { if (e.key === "Enter") addClient(); });
  $("#cl-refresh").addEventListener("click", loadClientDigest);
  initPackets();
  initEngagements();
  initRenewals();
  loadClientDigest();
}

async function addClient() {
  const s = $("#cl-subject").value.trim();
  if (!s) return;
  try { await postJson("/api/clients", { subject: s }); }
  catch (e) { alert("Couldn't add: " + e.message); return; }
  $("#cl-subject").value = "";
  loadClientDigest();
}

// jump-throughs: land on the tab with the client prefilled AND already running.
// Tab init is async (month/payer pickers load on first visit), so wait for the
// tab's required select to be populated before firing — otherwise a first-ever
// jump to Changes would run with no month and bounce off the 422.
async function clientJump(view, inputSel, runFn, subject, readySel) {
  switchView(view);
  for (let i = 0; i < 30; i++) {
    const ready = !readySel || ($(readySel) && $(readySel).options.length > 0);
    if (ready) break;
    await new Promise((r) => setTimeout(r, 150));
  }
  const input = $(inputSel);
  if (input) { input.value = subject; hashFromInputs(view); runFn(); }
}

async function loadClientDigest() {
  const out = $("#cl-out");
  const isFresh = ticket("digest");
  const have = await api("/api/clients").catch(() => null);
  if (!isFresh()) return;
  if (!have || !have.clients.length) {
    out.classList.add("empty");
    out.innerHTML = `<h3>No clients saved yet</h3>Add the practices you work for. Each refresh
      answers: whose rates moved, who has contract gaps, and which of their referral sources
      lost Medicare standing.`;
    return;
  }
  out.classList.remove("empty");
  out.innerHTML = `<div class="loading">Building the digest for ${have.clients.length} client${have.clients.length === 1 ? "" : "s"}</div>`;
  let d;
  try { d = await api("/api/clients/digest", { bar: { view: "clients", label: "Client digest", anchor: "#cl-refresh" } }); }
  catch (e) {
    if (!isFresh()) return;
    out.innerHTML = `<div class="empty"><h3>Digest failed</h3>${esc(e.message)}</div>`; return;
  }
  if (!isFresh()) return;
  const rows = d.clients.map((c) => {
    const name = `<b>${esc(c.display_name || c.subject)}</b>${
      c.display_name && c.display_name !== c.subject ? `<div class="sub">${esc(c.subject)}</div>` : ""}`;
    if (c.error) {
      return `<tr><td>${name}</td><td colspan="3"><span class="warn-text">${esc(c.error)}</span></td>
        <td><button class="btn" data-cl-remove="${esc(c.subject)}">remove</button></td></tr>`;
    }
    let chg;
    if (!d.can_diff) chg = `<span class="muted" title="rate changes need two loaded months of the same payer">needs 2 months</span>`;
    else if (!c.changes) chg = `<span class="muted">–</span>`;
    else if (c.changes.error) chg = `<span class="muted">${esc(c.changes.error)}</span>`;
    else if (c.changes.month === "latest" && !(c.changes.compared || []).length)
      chg = `<span class="muted" title="none of this client's payers has two files loaded">needs 2 files of a payer</span>`;
    else if (!c.changes.n) chg = `<span class="muted" title="${esc(c.changes.span || "")}">no moves</span>`;
    else {
      const b = c.changes.biggest_pct;
      chg = `<a href="#" ${c.jump === null ? `data-no-jump="1" title="saved by a Social-Security-shaped tax ID — re-add this client by name to open its screens"` : `data-cl-changes="${esc(c.jump || c.subject)}"`}><b>${fmtInt(c.changes.n)}</b> line${c.changes.n === 1 ? "" : "s"} moved</a>
        <div class="sub">biggest: <span class="${b > 0 ? "peer-up" : "peer-down"}">${b > 0 ? "+" : ""}${b}%</span>
        on ${esc(c.changes.biggest_code || "")} (${esc(c.changes.biggest_payer || "")}${
          c.changes.biggest_span ? `, ${esc(c.changes.biggest_span)}` : ""})</div>`;
    }
    let gaps;
    if (!c.gaps) gaps = `<span class="muted">–</span>`;
    else if (c.gaps.error) gaps = `<span class="muted">${esc(c.gaps.error)}</span>`;
    else if (!c.gaps.n) gaps = `<span class="muted">none</span>`;
    else gaps = `<a href="#" ${c.jump === null ? `data-no-jump="1" title="saved by a Social-Security-shaped tax ID — re-add this client by name to open its screens"` : `data-cl-gaps="${esc(c.jump || c.subject)}"`}><b>${fmtInt(c.gaps.n)}</b> code${c.gaps.n === 1 ? "" : "s"}</a>
      <div class="sub">${c.gaps.top.map(esc).join(", ")}${c.gaps.n > 3 ? "…" : ""}</div>`;
    let med;
    if (!d.have_medicare) med = `<span class="muted" title="import the Medicare layers on the Medicare tab to light this up">no referral data</span>`;
    else if (!c.lost_referrers) med = `<span class="muted">–</span>`;
    else if (!c.lost_referrers.length) med = `<span class="ok-tag">all sources in good standing</span>`;
    else med = `<a href="#" ${c.jump === null ? `data-no-jump="1" title="saved by a Social-Security-shaped tax ID — re-add this client by name to open its screens"` : `data-cl-med="${esc(c.jump || c.subject)}"`}><span class="warn-text"><b>${c.lost_referrers.length}</b> source${c.lost_referrers.length === 1 ? "" : "s"} lost standing</span></a>
      <div class="sub">${c.lost_referrers.slice(0, 3).map((l) =>
        `${esc(l.name || l.npi)} (${l.kind === "removed" ? "off roster" : "lost Part B"}, ${fmtInt(l.patients)} pts)`).join("; ")}</div>`;
    return `<tr><td>${name}</td><td>${chg}</td><td>${gaps}</td><td>${med}</td>
      <td style="white-space:nowrap">
        <button class="btn" ${c.jump === null ? `data-no-jump="1" title="saved by a Social-Security-shaped tax ID — re-add this client by name to open its screens"` : `data-cl-rc="${esc(c.jump || c.subject)}"`} title="rate card">Rates</button>
        <button class="btn" data-cl-remove="${esc(c.subject)}">remove</button>
      </td></tr>`;
  }).join("");
  out.innerHTML = `
    <div class="muted" style="margin-bottom:8px">Rate moves compare each payer's newest file with
      that payer's previous one, per contract line.</div>
    <div class="tablewrap"><table><thead><tr>
      <th>Client</th><th>Rate moves</th><th>Contract gaps</th><th>Referral alerts</th><th></th>
    </tr></thead><tbody>${rows}</tbody></table></div>`;
  $$("[data-cl-remove]", out).forEach((b) => b.addEventListener("click", async () => {
    await postJson("/api/clients/remove", { subject: b.dataset.clRemove }).catch(() => null);
    loadClientDigest();
  }));
  $$("[data-cl-rc]", out).forEach((b) => b.addEventListener("click", () =>
    clientJump("ratecard", "#rc-subject", buildRatecard, b.dataset.clRc)));
  $$("a[data-cl-changes]", out).forEach((a) => a.addEventListener("click", (ev) => {
    ev.preventDefault();
    clientJump("changes", "#ch-subject", runChanges, a.dataset.clChanges, "#ch-month");
  }));
  $$("a[data-cl-gaps]", out).forEach((a) => a.addEventListener("click", (ev) => {
    ev.preventDefault();
    clientJump("benchmark", "#b-subject", runContractGaps, a.dataset.clGaps);
  }));
  $$("a[data-cl-med]", out).forEach((a) => a.addEventListener("click", (ev) => {
    ev.preventDefault();
    clientJump("medicare", "#md-subject", runMedicareOrg, a.dataset.clMed);
  }));
}

/* ---- monthly packets: the documents you send ---------------------------- */
async function initPackets() {
  $("#pk-build").addEventListener("click", async () => {
    $("#pk-msg").textContent = "starting…";
    try { await postJson("/api/packets/build", {}); }
    catch (e) { $("#pk-msg").textContent = ""; alert("Couldn't start: " + e.message); return; }
    pollPackets();
  });
  pollPackets(true);
}

async function pollPackets(quiet = false) {
  let d;
  try { d = await api("/api/packets"); } catch { return; }
  const msg = $("#pk-msg"), out = $("#pk-out");
  if (d.state === "running") {
    msg.textContent = "";
    out.innerHTML = `<div class="loading">${esc(d.message || "building")}</div>`;
    if (!state.packetPoll) state.packetPoll = setInterval(() => pollPackets(true), 1500);
    return;
  }
  if (state.packetPoll) { clearInterval(state.packetPoll); state.packetPoll = null; }
  msg.textContent = "";
  if (d.state === "done" && d.result) {
    const r = d.result;
    out.innerHTML = `<div class="rc-summary"><b>${fmtInt(r.clients)}</b> client packet(s),
      <b>${fmtInt(r.documents)}</b> document(s) for <b>${esc(r.month)}</b> →
      <code class="inline" style="overflow-wrap:anywhere">${esc(r.dir)}</code></div>
      <div class="tablewrap"><table><thead><tr><th>Client</th><th class="num">Documents</th>
      <th>Omitted (and why)</th></tr></thead><tbody>${r.packets.map((p) => `<tr>
        <td>${esc(p.display_name)}</td><td class="num">${p.written.length}</td>
        <td class="sub">${esc((p.skipped || []).join("; "))}</td></tr>`).join("")}</tbody></table></div>
      <div class="muted" style="margin-top:6px">Open the folder above and attach the files, or
      open each client's <code class="inline">index.html</code> to review.</div>`;
  } else if (d.state === "done" && d.message && !quiet) {
    out.innerHTML = `<div class="muted">${esc(d.message)}</div>`;
  }
}

/* ---- engagement baselines: before/after proof --------------------------- */
// ONE reader for every "Annual units per code" box (Benchmark, Negotiate,
// engagements). "97110, 4,200" / "97110 4200" / "97110\t4200" / "97110: 4200"
// all read 4200. The Benchmark box had its own split-on-comma reader that
// turned "4,200" into 4 units and dropped "97140 1800" without a word, so its
// opportunity was a fraction of the truth (round 7 P0.8). A line that is not
// a code and a number is NOT guessed at: it is listed under the box.
function parseVolumes(sel, msgSel) {
  const vols = {};
  const skipped = [];
  ($(sel).value || "").split(/\r?\n/).forEach((line) => {
    if (!line.trim()) return;
    const m = line.match(/^\s*([A-Za-z0-9]+)\s*[,;:\t ]\s*(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)\s*$/);
    const n = m ? Number(m[2].replace(/,/g, "")) : NaN;
    if (m && Number.isFinite(n)) vols[m[1].toUpperCase()] = n;
    else skipped.push(line.trim());
  });
  if (msgSel && $(msgSel)) {
    $(msgSel).textContent = skipped.length
      ? `Not read (expected "code, units"): ${skipped.slice(0, 3).join(" · ")}${skipped.length > 3 ? ` (+${skipped.length - 3} more)` : ""}`
      : "";
  }
  return Object.keys(vols).length ? vols : null;
}

function initEngagements() {
  $("#en-vol-medicare").addEventListener("click", () =>
    fillVolumesFromMedicare("#en-subject", "#en-volumes", "#en-vol-msg"));
  $("#en-save").addEventListener("click", async () => {
    const subject = $("#en-subject").value.trim();
    if (!subject) { $("#en-msg").textContent = "pick a practice first"; return; }
    $("#en-msg").textContent = "saving…";
    try {
      const r = await postJson("/api/engagements/baseline", { subject });
      $("#en-msg").textContent = `${r.replaced ? "replaced" : "saved"} — ${r.codes} codes as of ${r.month}`;
    } catch (e) { $("#en-msg").textContent = ""; alert(e.message); }
  });
  $("#en-compare").addEventListener("click", runEngagementCompare);
}

async function runEngagementCompare() {
  const out = $("#en-out");
  const subject = $("#en-subject").value.trim();
  if (!subject) { $("#en-msg").textContent = "pick a practice first"; return; }
  $("#en-msg").textContent = "comparing…";
  let d;
  try {
    d = await postJson("/api/engagements/compare",
                       { subject, volumes: parseVolumes("#en-volumes", "#en-vol-msg") });
  } catch (e) { $("#en-msg").textContent = ""; out.innerHTML = `<div class="muted">${esc(e.message)}</div>`; return; }
  $("#en-msg").textContent = "";
  const s = d.summary;
  const rows = d.rows.map((r) => `<tr>
    <td>${esc(r.billing_code)}<div class="sub">${esc(r.description || "")}</div></td>
    <td class="num">${money(r.before)}</td><td class="num">${money(r.after)}</td>
    <td class="num ${r.delta > 0 ? "peer-up" : r.delta < 0 ? "peer-down" : ""}">${
      r.delta > 0 ? "+" : ""}${fmtMoney(r.delta)}${r.delta_pct == null ? "" : ` (${r.delta_pct > 0 ? "+" : ""}${r.delta_pct}%)`}</td>
    <td class="num">${r.pctile_before == null ? "–" : "p" + Math.round(r.pctile_before)} →
      ${r.pctile_after == null ? "–" : "p" + Math.round(r.pctile_after)}</td>
    <td class="num">${r.annual_value == null ? "" : money(r.annual_value)}</td></tr>`).join("");
  out.innerHTML = `<div class="rc-summary"><b>${s.n_improved}</b> of ${s.n_codes} code(s)
    improved since ${esc(String(d.baseline_month))}${
      s.median_pctile_before != null && s.median_pctile_after != null
        ? `; median position p${s.median_pctile_before} → <b>p${s.median_pctile_after}</b>` : ""}${
      s.total_annual_value != null ? `. Worth <b>${money(s.total_annual_value)}</b>/yr at your volumes` : ""}.</div>
    <div class="tablewrap" style="max-height:40vh"><table class="rc-table"><thead><tr>
      <th>Code</th><th class="num">Before</th><th class="num">After</th><th class="num">Change</th>
      <th class="num">Percentile</th><th class="num">Value/yr</th></tr></thead><tbody>${rows}</tbody></table></div>
    <div class="muted" style="font-size:11.5px;margin-top:6px">${esc(d.note)}</div>`;
}

/* ---- contract renewals -------------------------------------------------- */
function initRenewals() {
  $("#rn-run").addEventListener("click", async () => {
    const out = $("#rn-out");
    $("#rn-msg").textContent = "checking…";
    let subjects = null;
    if ($("#rn-clients").checked) {
      const c = await api("/api/clients").catch(() => null);
      subjects = (c && c.clients.length) ? c.clients : null;
      if (!subjects) {
        $("#rn-msg").textContent = "";
        out.innerHTML = `<div class="muted">No clients saved yet — add some above, or untick "my clients only".</div>`;
        return;
      }
    }
    let d;
    try {
      d = await postJson("/api/contracts/renewals",
        { subjects, within_days: Number($("#rn-within").value), market: { month: "latest" } });
    } catch (e) { $("#rn-msg").textContent = ""; out.innerHTML = `<div class="muted">${esc(e.message)}</div>`; return; }
    $("#rn-msg").textContent = "";
    const cov = d.coverage;
    const head = `<div class="muted" style="margin-bottom:6px">Expiration dates published on
      <b>${fmtInt(cov.with_expiry)}</b> of ${fmtInt(cov.rows)} contract lines
      (<b>${cov.pct}%</b>).</div>`;
    if (!d.rows.length) {
      out.innerHTML = head + `<div class="muted">No contracts with a usable published end date
        in the next ${d.within_days} days.</div>
        <div class="muted" style="font-size:11.5px;margin-top:6px">${esc(cov.note)}</div>`;
      return;
    }
    const rows = d.rows.map((r) => `<tr>
      <td>${esc(r.expires)}</td>
      <td class="num ${r.expired ? "warn-text" : r.days < 90 ? "peer-down" : ""}">${r.expired ? "expired" : fmtInt(r.days)}</td>
      <td>${esc(r.practice)}</td><td>${esc(r.payer)}</td>
      <td class="num">${fmtInt(r.codes)}</td><td class="num">${money(r.median_rate)}</td></tr>`).join("");
    out.innerHTML = head + `<div class="tablewrap" style="max-height:40vh"><table><thead><tr>
      <th>Expires</th><th class="num">Days</th><th>Practice</th><th>Payer</th>
      <th class="num">Codes</th><th class="num">Median rate</th></tr></thead><tbody>${rows}</tbody></table></div>
      <div class="muted" style="font-size:11.5px;margin-top:6px">${esc(cov.note)}</div>`;
  });
}

/* ---- new to network ----------------------------------------------------- */
function initNewToNetwork() {
  $("#nn-run").addEventListener("click", async () => {
    const out = $("#nn-out");
    $("#nn-msg").textContent = "looking…";
    let d;
    try {
      d = await postJson("/api/contracts/new-to-network", {
        market: { month: "latest" },
        zip: $("#nn-zip").value.trim() || null,
        radius_miles: $("#nn-zip").value.trim() ? Number($("#nn-radius").value) : null,
        therapy_only: $("#nn-therapy").checked,
      });
    } catch (e) { $("#nn-msg").textContent = ""; out.innerHTML = `<div class="muted">${esc(e.message)}</div>`; return; }
    $("#nn-msg").textContent = "";
    if (d.reason) { out.innerHTML = `<div class="muted">${esc(d.reason)}</div>`; return; }
    // the months actually compared, per payer — "latest" is each payer's own
    // newest file against its own previous one
    const span = (d.compared_months || []).map((c) =>
      `${esc(c.payer)} ${esc(c.prev_month)} → ${esc(c.month)}`).join("; ");
    const when = d.month === "latest" ? "each payer's newest file" : esc(String(d.month));
    if (!d.rows.length) {
      out.innerHTML = `<div class="muted">No practice newly appeared in a payer's book
        (${when})${d.zip ? ` within ${d.radius_miles} miles of ${esc(d.zip)}` : ""}.
        <div class="sub">Compared: ${span || "–"}</div></div>`;
      return;
    }
    const rows = d.rows.map((r, i) => `<tr>
      <td class="num">${i + 1}</td><td>${esc(r.practice)}${r.unidentified
        ? ' <span class="muted" title="clinicians not yet in the NPI directory">(not yet identified)</span>' : ""}</td>
      <td>${esc([...(r.cities || []), ...(r.states || [])].join(", "))}</td>
      <td>${esc(r.zip || "")}</td>
      <td class="num">${r.miles == null ? "–" : r.miles}</td>
      <td>${esc(r.payer)}</td><td class="sub">${esc(r.prev_month)} → ${esc(r.month || d.month)}</td>
      <td class="num">${fmtInt(r.codes)}</td><td class="num">${money(r.median_rate)}</td></tr>`).join("");
    out.innerHTML = `<div class="muted" style="margin-bottom:6px">${fmtInt(d.total)} newly
      contracted practice(s), ${when}.<div class="sub">Compared: ${span}</div></div>
      <div class="tablewrap" style="max-height:40vh"><table><thead><tr>
        <th>#</th><th>Practice</th><th>Location</th><th>ZIP</th><th class="num">Miles</th>
        <th>Payer</th><th>Months</th><th class="num">Codes</th><th class="num">Median</th>
      </tr></thead><tbody>${rows}</tbody></table></div>
      <div class="muted" style="font-size:11.5px;margin-top:6px">${esc(d.note || "")}</div>`;
  });
}

/* ---- reference datasets: Medicare volumes, ZIP demographics ------------- */
async function refreshRefData() {
  const u = await api("/api/utilization/status").catch(() => null);
  if (u) {
    $("#util-status").textContent = u.loaded
      ? `Medicare utilization: ${u.years.map((y) => y.year).join(", ")}`
      : "Medicare utilization: not loaded";
  }
  const d = await api("/api/demographics/status").catch(() => null);
  if (d) {
    $("#demo-status").textContent = d.loaded
      ? `ZIP demographics: ${fmtInt(d.zctas)} ZCTAs`
      : "ZIP demographics: not loaded";
  }
}

function initRefData() {
  refreshRefData();
  const msg = () => $("#refdata-msg");
  $("#util-import").addEventListener("click", async () => {
    const path = $("#util-path").value.trim();
    if (!path) { msg().textContent = "paste the path to the CSV you downloaded"; return; }
    msg().textContent = "importing — a national file takes a minute…";
    try {
      const r = await postJson("/api/utilization/import", { path, year: $("#util-year").value.trim() });
      msg().innerHTML = `${esc(String(r.year))}: <b>${fmtInt(r.rows)}</b> therapy rows over
        ${fmtInt(r.providers)} providers. ${r.warning ? `<b>${esc(r.warning)}</b> ` : ""}
        <span class="muted">${esc(r.note)}</span>`;
    } catch (e) { msg().textContent = e.message; }
    refreshRefData();
  });
  $("#demo-import").addEventListener("click", async () => {
    const path = $("#demo-path").value.trim();
    if (!path) { msg().textContent = "paste the path to the ACS CSV you downloaded"; return; }
    msg().textContent = "importing…";
    try {
      const r = await postJson("/api/demographics/import", { path, vintage: $("#demo-vintage").value.trim() });
      msg().innerHTML = `<b>${fmtInt(r.zctas)}</b> ZCTAs from ${esc(r.source)}.
        ${r.problems && r.problems.length ? `${r.problems.length} line(s) skipped. ` : ""}
        <span class="muted">${esc(r.note)}</span>`;
    } catch (e) { msg().textContent = e.message; }
    refreshRefData();
  });
}

/* Fill a volumes box from the practice's Medicare claims. The units are a
   FLOOR (Medicare only), so the message says so every time — a pre-filled box
   the user forgets came from Medicare is how a wrong dollar figure ships. */
async function fillVolumesFromMedicare(subjectSel, boxSel, msgSel, multSel, stateSel) {
  const subject = $(subjectSel).value.trim();
  const out = $(msgSel);
  if (!subject) { out.textContent = "pick a practice first"; return; }
  // validate here: NaN survives Number() but JSON.stringify turns it into
  // null, which the server would silently read as "no multiplier" (=1) —
  // the user typed SOMETHING and must be told it didn't take
  const mult = multSel ? Number($(multSel).value || 1) : 1;
  if (!isFinite(mult) || mult <= 0) {
    out.textContent = "the multiplier must be a positive number (1 = Medicare only)";
    return;
  }
  out.textContent = "reading Medicare claims…";
  let d;
  try {
    const st = stateSel && $(stateSel) ? $(stateSel).value.trim() : "";
    d = await postJson("/api/utilization/volumes",
                       { subject, multiplier: mult, market: st ? { state: st.toUpperCase() } : null });
  } catch (e) { out.textContent = e.message; return; }
  const lines = Object.entries(d.volumes).map(([c, u]) => `${c}, ${u}`);
  $(boxSel).value = lines.join("\n");
  out.innerHTML = `${lines.length} code(s) from Medicare ${esc(String(d.year))}
    ${d.multiplier !== 1 ? `×${d.multiplier}` : ""} — a <b>floor</b>, edit before quoting.`;
  out.title = d.note;
}

/* ---- brand-new clinics (NPPES enumeration dates) ------------------------ */
function initNewClinics() {
  $("#nc-run").addEventListener("click", async () => {
    const out = $("#nc-out");
    $("#nc-msg").textContent = "looking…";
    let d;
    try {
      d = await postJson("/api/newclinics", {
        zip: $("#nc-zip").value.trim() || null,
        radius_miles: $("#nc-zip").value.trim() ? Number($("#nc-radius").value) : null,
        state: $("#nc-state").value.trim() || null,
        days: Number($("#nc-days").value),
      });
    } catch (e) { $("#nc-msg").textContent = ""; out.innerHTML = `<div class="muted">${esc(e.message)}</div>`; return; }
    $("#nc-msg").textContent = "";
    if (d.reason) { out.innerHTML = `<div class="muted">${esc(d.reason)}</div>`; return; }
    if (!d.rows.length) {
      out.innerHTML = `<div class="muted">No therapy NPI was issued in the last ${d.days} days
        ${d.zip ? `within ${d.radius_miles} miles of ${esc(d.zip)}` : ""}.</div>`;
      return;
    }
    const rows = d.rows.map((r, i) => `<tr>
      <td class="num muted">${i + 1}</td>
      <td>${esc(r.org_name || r.npi)}<div class="sub">${esc(r.taxonomy || "")}</div></td>
      <td>${esc(r.enumerated)}</td>
      <td class="sub">${esc([r.city, r.state].filter(Boolean).join(", "))} ${esc(r.zip || "")}</td>
      <td class="num">${r.miles == null ? "–" : r.miles}</td>
      <td class="sub">${esc(r.phone || "")}</td>
      <td>${r.in_store ? '<span class="muted">has published rates</span>' : "<b>no contracts seen</b>"}</td>
    </tr>`).join("");
    out.innerHTML = `<div class="muted" style="margin-bottom:6px">${fmtInt(d.total)} therapy NPI(s)
      issued since ${esc(d.since)}${d.unplaced ? ` · ${d.unplaced} skipped (ZIP has no Census centroid)` : ""}.</div>
      <div class="tablewrap" style="max-height:40vh"><table><thead><tr>
        <th>#</th><th>Practice</th><th>Enumerated</th><th>Location</th><th class="num">Miles</th>
        <th>Phone</th><th>Status</th></tr></thead><tbody>${rows}</tbody></table></div>
      <div class="muted" style="font-size:11.5px;margin-top:6px">${esc(d.note)}</div>`;
  });
}

/* ---- Medicaid / workers-comp floors ------------------------------------- */
function initFloors() {
  const msg = (t) => { $("#fl-msg").textContent = t; };
  $("#fl-file").addEventListener("change", async (e) => {
    const f = e.target.files[0];
    if (!f) return;
    if (!$("#fl-state").value.trim()) { msg("enter the state first — a fee schedule is a state document"); e.target.value = ""; return; }
    msg("loading…");
    const fd = new FormData();
    fd.append("file", f);
    const q = new URLSearchParams({
      kind: $("#fl-kind").value, state: $("#fl-state").value.trim(),
      year: $("#fl-year").value.trim(),
    });
    try {
      const r = await api(`/api/floors/import?${q}`, { method: "POST", body: fd });
      msg(`${esc(r.label)}: ${r.codes} code(s) loaded.`);
      await runFloorCompare();
    } catch (err) { msg(`couldn't load: ${err.message}`); }
    e.target.value = "";
  });
  $("#fl-run").addEventListener("click", runFloorCompare);
  api("/api/floors/status").then((st) => {
    if (st && st.loaded) msg(st.schedules.map((s) => s.label).join(" · "));
  }).catch(() => {});
}

async function runFloorCompare() {
  const out = $("#fl-out");
  // ask for the state before spending a round trip on a request that cannot
  // succeed: a fee schedule is a state document, so there is nothing to compare
  const st = $("#fl-state").value.trim() || $("#ng-state").value.trim();
  if (!st) {
    $("#fl-msg").textContent =
      "enter the state first — Medicaid and workers'-comp schedules are state documents";
    out.innerHTML = "";
    return;
  }
  $("#fl-msg").textContent = "";
  out.innerHTML = `<div class="loading">Comparing</div>`;
  let d;
  try {
    d = await postJson("/api/floors/compare", {
      subject: $("#ng-subject").value.trim() || null,
      payer: $("#ng-payer").value || null,
      state: st,
      market: { month: $("#ng-month").value || "latest",
                peer_group: $("#ng-peer").value },
    });
  } catch (e) { out.innerHTML = `<div class="muted">${esc(e.message)}</div>`; return; }
  if (!d.loaded || !d.rows.length) {
    out.innerHTML = `<div class="muted">${esc(d.reason || "nothing to compare yet")}</div>`;
    return;
  }
  const pct = (v) => (v == null ? "–" : v + "%");
  const rows = d.rows.map((r) => `<tr>
    <td>${esc(r.billing_code)}<div class="sub">${esc(r.description || "")}</div></td>
    <td class="num">${r.subject_rate == null ? "–" : money(r.subject_rate)}</td>
    <td class="num">${money(r.commercial_median)}</td>
    <td class="num">${money(r.medicaid)}</td>
    <td class="num"><b>${pct(r.subject_pct_of_medicaid ?? r.pct_of_medicaid)}</b></td>
    <td class="num">${money(r.workers_comp)}</td>
    <td class="num">${pct(r.subject_pct_of_workers_comp ?? r.pct_of_workers_comp)}</td>
  </tr>`).join("");
  out.innerHTML = `<div class="muted" style="margin-bottom:6px">${esc(d.headline)}</div>
    <div class="tablewrap" style="max-height:40vh"><table><thead><tr>
      <th>Code</th><th class="num">Your rate</th><th class="num">Commercial median</th>
      <th class="num">Medicaid</th><th class="num">% of Medicaid</th>
      <th class="num">Work comp</th><th class="num">% of WC</th>
    </tr></thead><tbody>${rows}</tbody></table></div>
    ${d.codes_not_in_schedule.length ? `<div class="muted" style="margin-top:6px">
      Not in the schedule (left out, not counted as zero):
      ${esc(d.codes_not_in_schedule.join(", "))}</div>` : ""}
    <div class="muted" style="font-size:11.5px;margin-top:6px">${esc(d.note)}</div>`;
}

/* ---- walk-away leverage + per-visit economics (Negotiate tab) ----------- */
function initLeverage() {
  const show = (html) => { $("#lv-out").innerHTML = html; };
  const params = () => ({
    subject: $("#ng-subject").value.trim(),
    payer: $("#ng-payer").value,
    radius_miles: Number($("#lv-radius").value),
    market: ngMarket(),
  });
  $("#lv-run").addEventListener("click", async () => {
    const p = params();
    if (!p.subject || !p.payer) { $("#lv-msg").textContent = "pick a practice and a payer"; return; }
    $("#lv-msg").textContent = "measuring…";
    let d;
    try { d = await postJson("/api/leverage", p, { view: "negotiate", label: "Leverage", anchor: "#lv-run" }); }
    catch (e) { $("#lv-msg").textContent = ""; show(`<div class="muted">${esc(e.message)}</div>`); return; }
    $("#lv-msg").textContent = "";
    const rows = d.alternatives.map((a, i) => `<tr>
      <td class="num muted">${i + 1}</td>
      <td>${esc(a.display_name)}</td>
      <td class="num">${a.miles == null ? "–" : a.miles}</td>
      <td class="sub">${esc(a.zip || "")}</td>
      <td class="num muted">${fmtInt(a.npi_count)}</td></tr>`).join("");
    const cls = d.band === "thin" ? "chg-up" : d.band === "crowded" ? "chg-cut" : "";
    show(`<div class="rc-summary ${cls}">${esc(d.headline)}</div>
      ${rows ? `<div class="tablewrap" style="max-height:34vh"><table><thead><tr>
        <th>#</th><th>Their other contracted practices</th><th class="num">Miles</th>
        <th>ZIP</th><th class="num">Providers</th></tr></thead>
        <tbody>${rows}</tbody></table></div>` : ""}
      <div class="muted" style="font-size:11.5px;margin-top:6px">${esc(d.note)}</div>`);
  });
  $("#lv-all").addEventListener("click", async () => {
    const p = params();
    if (!p.subject) { $("#lv-msg").textContent = "pick a practice"; return; }
    $("#lv-msg").textContent = "ranking payers…";
    let d;
    try { d = await postJson("/api/leverage/summary", p, { view: "negotiate", label: "Every payer's leverage", anchor: "#lv-all" }); }
    catch (e) { $("#lv-msg").textContent = ""; show(`<div class="muted">${esc(e.message)}</div>`); return; }
    $("#lv-msg").textContent = "";
    const rows = d.payers.map((x) => `<tr>
      <td>${esc(x.payer)}</td>
      <td class="num">${fmtInt(x.n_alternatives)}</td>
      <td>${x.band === "thin" ? "<b>thin — open here</b>" : esc(x.band)}</td>
      <td class="num muted">${x.seniors_per_alternative == null ? "–" : fmtInt(x.seniors_per_alternative)}</td>
      </tr>`).join("");
    show(`<div class="rc-summary">${esc(d.headline)}</div>
      <div class="tablewrap"><table><thead><tr><th>Payer</th>
        <th class="num">Local alternatives</th><th>Network</th>
        <th class="num">Seniors each</th></tr></thead><tbody>${rows}</tbody></table></div>
      ${(d.skipped || []).length ? `<div class="muted" style="margin-top:6px">
        Not ranked: ${esc(d.skipped.map((s) => s.payer).join(", "))}.</div>` : ""}
      <div class="muted" style="font-size:11.5px;margin-top:6px">${esc(d.note)}</div>`);
  });
}

function initVisitEconomics() {
  $("#ec-run").addEventListener("click", async () => {
    const out = $("#ec-out");
    const subject = $("#ng-subject").value.trim();
    if (!subject) { $("#ec-msg").textContent = "pick a practice"; return; }
    $("#ec-msg").textContent = "pricing…";
    const upv = $("#ec-upv").value.trim();
    let d;
    try {
      d = await postJson("/api/economics/visit", {
        subject, units_per_visit: upv ? Number(upv) : null,
        market: ngMarket(),
      });
    } catch (e) { $("#ec-msg").textContent = ""; out.innerHTML = `<div class="muted">${esc(e.message)}</div>`; return; }
    $("#ec-msg").textContent = "";
    if (!d.loaded) { out.innerHTML = `<div class="muted">${esc(d.reason)}</div>`; return; }
    const rows = d.rows.map((r) => `<tr>
      <td>${esc(r.payer)}</td>
      <td class="num"><b>${r.value_per_visit == null ? money(r.value_per_billed_unit) : money(r.value_per_visit)}</b></td>
      <td class="num">${money(r.value_per_billed_unit)}</td>
      <td class="num muted">${r.mix_coverage_pct}%</td>
      <td class="num muted">${money(r.annual_value_at_this_rate)}</td></tr>`).join("");
    const mix = d.mix.map((m) => `${esc(m.billing_code)} ${m.share_pct}%`).join(" · ");
    out.innerHTML = `<div class="rc-summary">${esc(d.headline)}</div>
      <div class="tablewrap"><table><thead><tr><th>Payer</th>
        <th class="num">Per ${d.units_per_visit ? "visit" : "unit"}</th>
        <th class="num">Per billed unit</th><th class="num">Mix covered</th>
        <th class="num">Annual at this rate</th></tr></thead>
        <tbody>${rows}</tbody></table></div>
      <div class="muted" style="margin-top:6px">Their mix (${esc(String(d.mix_year || ""))}): ${mix}</div>
      ${d.assumption_note ? `<div class="warn-text" style="margin-top:6px">${esc(d.assumption_note)}</div>` : ""}
      <div class="muted" style="font-size:11.5px;margin-top:6px">${esc(d.note)}</div>`;
  });
}

/* ---- dossier, market report, growth & service lines (Leads tab) --------- */
function initDossier() {
  $("#ds-run").addEventListener("click", async () => {
    const subject = $("#ds-subject").value.trim();
    if (!subject) { $("#ds-msg").textContent = "pick a practice"; return; }
    $("#ds-msg").textContent = "assembling…";
    // the Leads tab's state and month (review 10 item 2.9: the form sent
    // neither — "4 provider NPI(s), Town, Wichita (KS, MO)")
    const dsMarket = { month: $("#ld-month").value || "latest", therapy_only: false };
    if ($("#ld-state").value.trim()) dsMarket.state = $("#ld-state").value.trim().toUpperCase();
    const body = { subject, radius_miles: Number($("#ds-radius").value), market: dsMarket };
    // the tab opens NOW, inside the click (after the awaits Chrome's
    // activation window has expired on a slow store and the popup is
    // blocked); the HTML report's refusal is caught and shown (P1.23)
    // ONE build: the omitted sections ride in a response header (the button
    // used to build the whole dossier twice, review 13)
    $("#ds-out").innerHTML = "";
    const r = await openReportWith("/api/report/dossier", body,
                                   (msg) => { $("#ds-msg").textContent = msg; },
                                   { view: "leads", label: "Prospect dossier", anchor: "#ds-run" });
    if ($("#ds-msg").textContent === "assembling…") $("#ds-msg").textContent = "";
    if (r) {
      let skipped = [];
      try { skipped = JSON.parse(decodeURIComponent(r.headers.get("X-Mrfx-Skipped") || "%5B%5D")); } catch (_) { skipped = []; }
      $("#ds-out").innerHTML = skipped.length
        ? `<div class="muted">Built with ${skipped.length} section(s) omitted: ${esc(skipped.join("; "))}</div>` : "";
    }
  });
  $("#mr-run").addEventListener("click", async () => {
    const st = $("#mr-state").value.trim();
    if (!st) { $("#mr-msg").textContent = "enter a state"; return; }
    $("#mr-msg").textContent = "building…";
    // an invalid state ("ZZ") threw an unhandled rejection and showed
    // nothing (P1.23): the refusal now lands in the message line
    await openReportWith("/api/report/market", {
      state: st, code: $("#mr-code").value.trim() || "97110",
      zip: $("#mr-zip").value.trim() || null,
      market: { month: "latest", therapy_only: false } },
      (msg) => { $("#mr-msg").textContent = msg; }, { view: "leads", label: "Market report", anchor: "#mr-run" });
    if ($("#mr-msg").textContent === "building…") $("#mr-msg").textContent = "";
  });
}

function initGrowth() {
  const run = async (route, render) => {
    const subject = $("#gr-subject").value.trim();
    if (!subject) { $("#gr-msg").textContent = "pick a practice"; return; }
    $("#gr-msg").textContent = "looking…";
    let d;
    try { d = await postJson(route, { subject }); }
    catch (e) { $("#gr-msg").textContent = ""; $("#gr-out").innerHTML = `<div class="muted">${esc(e.message)}</div>`; return; }
    $("#gr-msg").textContent = "";
    $("#gr-out").innerHTML = d.loaded === false
      ? `<div class="muted">${esc(d.reason)}</div>` : render(d);
  };
  $("#gr-run").addEventListener("click", () => run("/api/utilization/growth", (d) => {
    const rows = d.rows.map((r) => `<tr><td>${esc(r.billing_code)}
      <div class="sub">${esc(r.description || "")}</div></td>
      <td class="num">${fmtInt(r.units_from)}</td><td class="num">${fmtInt(r.units_to)}</td>
      <td class="num ${r.delta < 0 ? "chg-cut" : "chg-up"}">${r.delta > 0 ? "+" : ""}${fmtInt(r.delta)}</td>
      <td class="num">${r.pct_change == null ? "–" : (r.pct_change > 0 ? "+" : "") + r.pct_change + "%"}</td></tr>`).join("");
    return `<div class="rc-summary">${esc(d.headline)}</div>
      <div class="tablewrap"><table><thead><tr><th>Code</th>
        <th class="num">${esc(d.from_year)}</th><th class="num">${esc(d.to_year)}</th>
        <th class="num">Change</th><th class="num">%</th></tr></thead>
        <tbody>${rows}</tbody></table></div>
      <div class="muted" style="font-size:11.5px;margin-top:6px">${esc(d.note)}</div>`;
  }));
  $("#gr-lines").addEventListener("click", () => run("/api/utilization/service-lines", (d) => {
    if (!d.rows.length) return `<div class="muted">${esc(d.reason)}</div>`;
    const rows = d.rows.map((r) => `<tr><td>${esc(r.billing_code)}
      <div class="sub">${esc(r.description || "")}</div></td>
      <td class="num">${fmtInt(r.n_peers)}</td>
      <td class="num muted">${fmtInt(r.median_units_each)}</td></tr>`).join("");
    return `<div class="rc-summary">${esc(d.headline)}</div>
      <div class="tablewrap"><table><thead><tr><th>Code they don't bill</th>
        <th class="num">Nearby practices billing it</th>
        <th class="num">Median units each</th></tr></thead><tbody>${rows}</tbody></table></div>
      <div class="muted" style="font-size:11.5px;margin-top:6px">${esc(d.note)}</div>`;
  }));
}

/* ---- steal-share: referrals going to a competitor ----------------------- */
function initStealShare() {
  $("#ss-run").addEventListener("click", async () => {
    const out = $("#ss-out");
    const subject = $("#ss-subject").value.trim();
    if (!subject) { $("#ss-msg").textContent = "pick a client first"; return; }
    $("#ss-msg").textContent = "looking…";
    let d;
    try { d = await postJson("/api/territory/steal-share", { subject }); }
    catch (e) { $("#ss-msg").textContent = ""; out.innerHTML = `<div class="muted">${esc(e.message)}</div>`; return; }
    $("#ss-msg").textContent = "";
    if (!d.rows.length) { out.innerHTML = `<div class="muted">${esc(d.reason || "nothing to target")}</div>`; return; }
    const rows = d.rows.map((r, i) => `<tr>
      <td class="num muted">${i + 1}</td>
      <td>${esc(r.name || r.npi)}<div class="sub">${esc(r.specialty || "")}</div></td>
      <td class="num"><b>${fmtInt(r.patients_elsewhere)}</b></td>
      <td class="num">${fmtInt(r.patients_to_me)}</td>
      <td class="num">${r.share_missed_pct == null ? "–" : r.share_missed_pct + "%"}</td>
      <td class="num">${r.miles == null ? "–" : r.miles}</td>
      <td class="sub">${esc([r.city, r.state].filter(Boolean).join(", "))}</td>
      <td class="sub">${esc(r.phone || "")}</td>
      <td>${r.already_a_partner ? '<span class="muted">sends you some</span>' : "<b>sends you none</b>"}</td>
    </tr>`).join("");
    out.innerHTML = `<div class="muted" style="margin-bottom:6px">${esc(d.headline)}
      Release: ${esc(d.dataset || "")}.</div>
      <div class="tablewrap" style="max-height:40vh"><table><thead><tr>
        <th>#</th><th>Referral source</th><th class="num">To others</th>
        <th class="num">To your client</th><th class="num">Share missed</th>
        <th class="num">Miles</th><th>Location</th><th>Phone</th><th>Status</th>
        </tr></thead><tbody>${rows}</tbody></table></div>
      <div class="muted" style="font-size:11.5px;margin-top:6px">${esc(d.note)}</div>`;
  });
}

/* ---- hospital price transparency (parity vs practice rates) ------------- */
function initHospital() {
  const msg = (t) => { $("#hp-msg").textContent = t; };
  $("#hp-file").addEventListener("change", async (e) => {
    const f = e.target.files[0];
    if (!f) return;
    msg("importing… (a large hospital file can take a minute)");
    const fd = new FormData();
    fd.append("file", f);
    const q = new URLSearchParams({
      state: $("#hp-state").value.trim(),
      city: $("#hp-city").value.trim(),
      hospital_name: $("#hp-name").value.trim(),
    });
    try {
      const r = await api(`/api/hospital/import?${q}`, { method: "POST", body: fd });
      msg(r.rows
        ? `${esc(r.hospital)}: ${fmtInt(r.rows)} therapy rate(s) across ${r.codes} code(s).`
        : `${esc(r.hospital)}: ${esc(r.note || "no therapy rates found")}`);
      await runHospitalParity();
    } catch (err) { msg(`couldn't import: ${err.message}`); }
    e.target.value = "";
  });
  $("#hp-run").addEventListener("click", runHospitalParity);
  refreshHospitalStatus();
}

async function refreshHospitalStatus() {
  const st = await api("/api/hospital/status").catch(() => null);
  if (st && st.loaded) {
    $("#hp-msg").textContent =
      `${st.n_hospitals} hospital(s) loaded · ${fmtInt(st.rows)} therapy rate(s)`;
  }
}

async function runHospitalParity() {
  const out = $("#hp-out");
  out.innerHTML = `<div class="loading">Comparing</div>`;
  let d;
  try {
    d = await postJson("/api/hospital/parity", {
      subject: $("#ng-subject").value.trim() || null,
      payer: $("#ng-payer").value || null,
      state: $("#hp-state").value.trim() || $("#ng-state").value.trim() || null,
      market: { month: $("#ng-month").value || "latest",
                peer_group: $("#ng-peer").value },
    });
  } catch (e) { out.innerHTML = `<div class="muted">${esc(e.message)}</div>`; return; }
  if (!d.loaded || !d.rows.length) {
    out.innerHTML = `<div class="muted">${esc(d.reason || "nothing to compare yet")}</div>`;
    return;
  }
  const rows = d.rows.map((r) => `<tr>
    <td>${esc(r.billing_code)}<div class="sub">${esc(r.description || "")}</div></td>
    <td>${esc(r.payer)}</td>
    <td class="num">${r.subject_rate == null ? "–" : money(r.subject_rate)}</td>
    <td class="num">${money(r.practice_median)}<div class="sub">${r.n_practices} practices</div></td>
    <td class="num">${money(r.hospital_median)}<div class="sub">${r.n_hospitals} hospital(s)</div></td>
    <td class="num"><b>${r.subject_multiple ?? r.hospital_multiple ?? "–"}${
      (r.subject_multiple ?? r.hospital_multiple) == null ? "" : "×"}</b></td>
  </tr>`).join("");
  out.innerHTML = `<div class="muted" style="margin-bottom:6px">
      ${d.median_hospital_multiple
        ? `Across ${d.count} code(s), this payer pays the hospital outpatient department a
           median of <b>${d.median_hospital_multiple}×</b> the private-practice median.`
        : `${d.count} code(s) matched.`}</div>
    <div class="tablewrap" style="max-height:40vh"><table><thead><tr>
      <th>Code</th><th>Payer</th><th class="num">Your rate</th>
      <th class="num">Practice median</th><th class="num">Hospital median</th>
      <th class="num">Multiple</th></tr></thead><tbody>${rows}</tbody></table></div>
    <div class="muted" style="font-size:11.5px;margin-top:6px">${esc(d.caveat)} ${esc(d.note)}</div>`;
}

/* ---- closures (NPPES deactivation dates) -------------------------------- */
function initClosures() {
  $("#cz-run").addEventListener("click", async () => {
    const out = $("#cz-out");
    $("#cz-msg").textContent = "looking…";
    let d;
    try {
      d = await postJson("/api/closures", {
        zip: $("#cz-zip").value.trim() || null,
        radius_miles: $("#cz-zip").value.trim() ? Number($("#cz-radius").value) : null,
        state: $("#cz-state").value.trim() || null,
        days: Number($("#cz-days").value),
      });
    } catch (e) { $("#cz-msg").textContent = ""; out.innerHTML = `<div class="muted">${esc(e.message)}</div>`; return; }
    $("#cz-msg").textContent = "";
    if (d.reason) { out.innerHTML = `<div class="muted">${esc(d.reason)}</div>`; return; }
    if (!d.rows.length) {
      out.innerHTML = `<div class="muted">No therapy NPI was deactivated in the last ${d.days} days
        ${d.zip ? `within ${d.radius_miles} miles of ${esc(d.zip)}` : ""}.</div>`;
      return;
    }
    const rows = d.rows.map((r, i) => `<tr>
      <td class="num muted">${i + 1}</td>
      <td>${esc(r.org_name || r.npi)}<div class="sub">${esc(r.taxonomy || "")}</div></td>
      <td>${esc(r.deactivated)}</td>
      <td class="sub">${esc([r.city, r.state].filter(Boolean).join(", "))} ${esc(r.zip || "")}</td>
      <td class="num">${r.miles == null ? "–" : r.miles}</td>
      <td class="sub">${esc(r.phone || "")}</td>
      <td>${r.in_store ? "<b>had published rates</b>" : '<span class="muted">no rates in store</span>'}</td>
    </tr>`).join("");
    out.innerHTML = `<div class="muted" style="margin-bottom:6px">${fmtInt(d.total)} therapy NPI(s)
      deactivated since ${esc(d.since)}${d.unplaced ? ` · ${d.unplaced} skipped (ZIP has no Census centroid)` : ""}.</div>
      <div class="tablewrap" style="max-height:40vh"><table><thead><tr>
        <th>#</th><th>Practice</th><th>Deactivated</th><th>Location</th><th class="num">Miles</th>
        <th>Phone</th><th>Was it in your data?</th></tr></thead><tbody>${rows}</tbody></table></div>
      <div class="muted" style="font-size:11.5px;margin-top:6px">${esc(d.note)}</div>`;
  });
}

/* ---- market sizing (Census ACS) ---------------------------------------- */
function initMarketSizing() {
  $("#ms-run").addEventListener("click", async () => {
    const out = $("#ms-out");
    if (!$("#ms-zip").value.trim()) { $("#ms-msg").textContent = "enter a ZIP"; return; }
    $("#ms-msg").textContent = "sizing…";
    let d;
    try {
      d = await postJson("/api/market/sizing", {
        zip: $("#ms-zip").value.trim(), radius_miles: Number($("#ms-radius").value) });
    } catch (e) { $("#ms-msg").textContent = ""; out.innerHTML = `<div class="muted">${esc(e.message)}</div>`; return; }
    $("#ms-msg").textContent = "";
    if (!d.loaded) { out.innerHTML = `<div class="muted">${esc(d.reason)}</div>`; return; }
    const zips = d.top_zips.map((z) => `<tr><td>${esc(z.zip)}</td>
      <td class="num">${fmtInt(z.population)}</td><td class="num">${fmtInt(z.pop_65_plus)}</td>
      <td class="num">${z.pct_65_plus == null ? "–" : z.pct_65_plus + "%"}</td>
      <td class="num">${z.median_income == null ? "–" : money(z.median_income)}</td>
      <td class="num">${z.miles}</td></tr>`).join("");
    out.innerHTML = `<div class="rc-summary">Within ${d.radius_miles} miles of ${esc(d.zip)}:
      <b>${fmtInt(d.population)}</b> people, <b>${fmtInt(d.pop_65_plus)}</b> aged 65+
      (${d.pct_65_plus == null ? "–" : d.pct_65_plus + "%"}), median household income
      ${d.median_income == null ? "–" : money(d.median_income)}. This store knows
      <b>${fmtInt(d.practices)}</b> therapy practice(s) there${d.seniors_per_practice
        ? ` — <b>${fmtInt(d.seniors_per_practice)}</b> seniors per practice` : ""}.
      ${d.zctas_unmatched ? `<span class="muted">${d.zctas_unmatched} ZIP(s) in radius have no Census row.</span>` : ""}</div>
      <div class="tablewrap" style="max-height:32vh"><table><thead><tr>
        <th>ZIP</th><th class="num">Population</th><th class="num">65+</th><th class="num">% 65+</th>
        <th class="num">Median income</th><th class="num">Miles</th>
      </tr></thead><tbody>${zips}</tbody></table></div>
      <div class="muted" style="font-size:11.5px;margin-top:6px">${esc(d.note)}</div>`;
  });
}

/* ---- underpayment check ------------------------------------------------- */
function initRemits() {
  api("/api/benchmark/subjects").then((s) => {
    if (s) $("#rm-subjects").innerHTML = subjectOptionsHtml(s);
  }).catch(() => null);
  wireSubjectSearch("#rm-subject", "#rm-subjects");
  $("#rm-run").addEventListener("click", runRemitCheck);
  $("#rm-csv").addEventListener("click", async () => {
    if (!state.lastRemitPayload) return;
    const r = await fetch("/api/remits/check.csv", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(state.lastRemitPayload),
    });
    if (!r.ok) { alert((await r.json().catch(() => ({}))).detail || "export failed"); return; }
    const a = document.createElement("a");
    a.href = URL.createObjectURL(new Blob([await r.text()], { type: "text/csv" }));
    a.download = "underpayment_check.csv";
    a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 60000);
  });
}

async function runRemitCheck() {
  const out = $("#rm-out");
  const payload = {
    subject: $("#rm-subject").value.trim(),
    text: $("#rm-text").value,
    basis: $("#rm-basis").value,
    market: { month: "latest", ...($("#rm-state").value.trim()
      ? { state: $("#rm-state").value.trim().toUpperCase() } : {}) },
  };
  state.lastRemitPayload = null;
  $("#rm-csv").disabled = true;
  if (!payload.subject || !payload.text.trim()) {
    $("#rm-msg").textContent = payload.subject
      ? "paste the remit lines first"
      : "pick a practice, then paste the remit lines";
    out.innerHTML = "";
    return;
  }
  $("#rm-msg").textContent = "checking…";
  let d;
  try { d = await postJson("/api/remits/check", payload); }
  catch (e) { $("#rm-msg").textContent = ""; out.innerHTML = `<div class="muted">${esc(e.message)}</div>`; return; }
  $("#rm-msg").textContent = "";
  state.lastRemitPayload = payload;
  $("#rm-csv").disabled = false;
  const s = d.summary;
  const flagged = d.flagged.map((r) => `<tr>
    <td class="num">${r.line}</td><td>${esc(r.billing_code)}</td>
    <td>${esc(r.matched_payer || "")}</td><td class="num">${fmtInt(r.units)}</td>
    <td class="num">${money(r.amount)}</td><td class="num">${money(r.expected)}</td>
    <td class="num peer-down">${money(r.shortfall)}${r.pct_short == null ? "" : ` (${r.pct_short}%)`}</td></tr>`).join("");
  const unmatched = d.unmatched.length ? `<h3 style="margin-top:14px">Could not price (${d.unmatched.length})</h3>
    <div class="tablewrap" style="max-height:22vh"><table><thead><tr><th class="num">Line</th>
    <th>Code</th><th>Payer on remit</th><th>Why</th></tr></thead><tbody>${
      d.unmatched.map((r) => `<tr><td class="num">${r.line}</td><td>${esc(r.billing_code)}</td>
      <td>${esc(r.payer || "")}</td><td class="sub">${esc(r.reason)}</td></tr>`).join("")}</tbody></table></div>` : "";
  const problems = d.problems.length ? `<div class="muted" style="margin-top:6px">
    ${d.problems.length} line(s) could not be read: ${esc(d.problems.slice(0, 3).join("; "))}</div>` : "";
  out.innerHTML = `
    <div class="rc-summary">${s.flagged ? `<b>${fmtInt(s.flagged)}</b> of ${fmtInt(s.lines_read)} line(s)
      paid below the published contract rate — <b>${money(s.total_shortfall)}</b> total shortfall on this batch.`
      : `No line was paid below the published contract rate (${fmtInt(s.ok)} checked).`}
      ${s.unmatched ? ` ${fmtInt(s.unmatched)} line(s) could not be priced.` : ""}</div>
    <div class="disclaimer">${esc(s.basis_note)} ${esc(d.caveat)}</div>
    ${s.flagged ? `<div class="tablewrap" style="max-height:34vh"><table class="rc-table"><thead><tr>
      <th class="num">Line</th><th>Code</th><th>Payer</th><th class="num">Units</th>
      <th class="num">Paid</th><th class="num">Published</th><th class="num">Short by</th>
    </tr></thead><tbody>${flagged}</tbody></table></div>` : ""}
    ${unmatched}${problems}`;
}

/* =======================================================================
   MEDICARE — the two layers MRFs cannot supply (imported via `mrfx medicare`):
   Order & Referring eligibility, and shared-patient referral structure.
   ======================================================================= */

let medicareInit = false;
async function initMedicare() {
  // tracker card FIRST: it decides whether the "nothing imported yet" banner
  // would be repeating a button the user can already see
  await refreshTrackerCard();
  refreshMedicareStatus();
  if (medicareInit) { refreshSubjectPickers("#md-subjects", []); return; }
  medicareInit = true;
  const subjects = await api("/api/benchmark/subjects").catch(() => null);
  if (subjects) $("#md-subjects").innerHTML = subjectOptionsHtml(subjects);
  wireSubjectSearch("#md-subject", "#md-subjects");
  $("#md-run").addEventListener("click", runMedicareOrg);
  $("#md-subject").addEventListener("keydown", (e) => { if (e.key === "Enter") runMedicareOrg(); });
  $("#md-check").addEventListener("click", runMedicareBatchCheck);
  $("#md-csv").addEventListener("click", downloadMedicareCsv);
  $("#md-check-csv").addEventListener("click", downloadMedicareCheckCsv);
  $("#rl-run").addEventListener("click", runReferralLeaders);
  $("#rl-zip").addEventListener("keydown", (e) => { if (e.key === "Enter") runReferralLeaders(); });
}

async function runReferralLeaders() {
  const out = $("#rl-out");
  const msg = $("#rl-msg");
  msg.textContent = "ranking…";
  let d;
  try {
    d = await postJson("/api/medicare/leaders", {
      zip: $("#rl-zip").value.trim() || null,
      radius_miles: $("#rl-zip").value.trim() ? Number($("#rl-radius").value) : null,
      therapy_only: $("#rl-therapy").checked,
      limit: 100,
      dataset_id: $("#md-year").value || null,
    });
  } catch (e) { msg.textContent = ""; out.innerHTML = `<div class="muted">${esc(e.message)}</div>`; return; }
  msg.textContent = "";
  if (!d.rows.length) {
    out.innerHTML = `<div class="muted">No practices found${d.zip ? ` within ${d.radius_miles} miles of ${esc(d.zip)}` : ""} in ${esc(d.dataset || "the referral data")} — try a wider radius, or untick "therapy practices only".</div>`;
    return;
  }
  const scope = d.zip ? `within ${d.radius_miles} miles of ${esc(d.zip)}` : "everywhere in the data";
  // honesty notes: how much of the market the list covers, and how many
  // practices could not be placed on the map at all
  const notes = [`${esc(d.dataset)} ${esc(d.data_year)} · ${scope} · showing ${d.rows.length} of ${fmtInt(d.total)} practices`];
  if (d.unplaced) notes.push(`<span class="warn-text">${fmtInt(d.unplaced)} practice${d.unplaced === 1 ? "" : "s"} have no mappable ZIP and are not in radius results</span>`);
  if (d.unidentified) notes.push(`${fmtInt(d.unidentified)} not yet identified (NPPES lookup still filling in — they stay listed rather than being dropped)`);
  // the coverage boundary rides VISIBLY with the ranking (a tooltip would not
  // survive a screenshot): absence here can mean "not connected to your
  // store", never only "no referrals"
  const coverage = d.coverage_note
    ? `<div class="muted" style="font-size:11.5px;margin:4px 0 6px">${esc(d.coverage_note)}</div>` : "";
  const body = d.rows.map((r, i) => {
    const ident = r.unidentified
      ? ` <span class="muted" title="this NPI has not been identified by the background NPPES lookup yet — it stays listed (unknown is not non-therapy) and will name itself once identification reaches it">not yet identified</span>` : "";
    const name = r.in_store && r.tin
      ? `<a href="#" data-rl-tin="${esc(r.tin)}" title="open every payer rate the MRF files price this practice at">${esc(r.practice)}</a>${ident}`
      : `${esc(r.practice)}${ident} <span class="muted" title="no ingested payer file prices this practice — it may still have contracts; your store just hasn't seen them">no MRF rates in store</span>`;
    return `<tr>
      <td class="num">${i + 1}</td>
      <td>${name}</td>
      <td>${esc([r.city, r.state].filter(Boolean).join(", "))}</td>
      <td>${esc(r.zip || "")}</td>
      <td class="num">${r.miles == null ? "–" : r.miles}</td>
      <td class="num">${fmtInt(r.patients)}</td>
      <td class="num">${fmtInt(r.sources)}</td>
      <td class="num">${fmtInt(r.npis)}</td>
    </tr>`;
  }).join("");
  out.innerHTML = `<div class="muted" style="margin-bottom:2px">${notes.join(" · ")}</div>
    ${coverage}
    <div class="tablewrap" style="max-height:44vh"><table><thead><tr>
      <th>#</th><th>Practice</th><th>City</th><th>ZIP</th><th class="num">Miles</th>
      <th class="num" title="shared Medicare patients received — see the caveat above; not a referral count">Patients in</th>
      <th class="num">Sources</th><th class="num">NPIs</th>
    </tr></thead><tbody>${body}</tbody></table></div>`;
  $$("a[data-rl-tin]", out).forEach((a) => a.addEventListener("click", (ev) => {
    ev.preventDefault();
    // TIN grain, not entity: an individual therapist's NPPES name is not an
    // entity key, but the TIN their rates bill under always resolves
    openEntity("tin", a.dataset.rlTin);   // the drawer: every payer rate
  }));
}

// the roster is refreshed by CMS ~twice a week; past this age the tab calls
// the loaded snapshot stale (mirror of medicare.STALE_ROSTER_DAYS)
const STALE_ROSTER_DAYS = 45;

/* ---- the merge: the tracker's downloads, imported in place ----------------
   The two tools own disjoint halves of the same practice, and the files that
   join them are already on this machine. So the app finds them and imports
   them itself — the server owns the database, so nothing has to be stopped. */
async function refreshTrackerCard() {
  const el = $("#md-tracker");
  let d;
  try { d = await api("/api/medicare/tracker"); }
  catch { el.style.display = "none"; state.trackerOffering = false; return; }
  state.trackerJob = d.job;
  // whether THIS card is already showing a one-click import, so the
  // "nothing imported yet" banner can stay quiet instead of repeating it
  state.trackerOffering = !!(d.found && (d.pending || []).length)
    || (d.job && d.job.state === "running");

  if (d.job && d.job.state === "running") {
    el.style.display = "";
    el.innerHTML = `<b>Importing from the Order &amp; Referring Tracker…</b>
      <div class="loading" style="margin-top:6px">${esc(d.job.message || "working")}</div>
      <div class="muted" style="margin-top:4px">A referral year is several GB — this can take
      a few minutes. You can keep using the rest of the app; this card updates itself.</div>`;
    if (!state.trackerPoll) state.trackerPoll = setInterval(refreshTrackerCard, 2000);
    return;
  }
  if (state.trackerPoll) { clearInterval(state.trackerPoll); state.trackerPoll = null; }

  // just-finished report, shown once
  let finished = "";
  if (d.job && d.job.state === "done" && (d.job.done || []).length && !state.trackerReported) {
    state.trackerReported = true;
    finished = `<div style="margin-bottom:8px">${d.job.done.map((x) =>
      `<div>${x.ok ? "<b>Imported</b>" : '<span class="warn-text">Could not import</span>'}
        ${esc(x.label)} — ${esc(x.detail)}</div>`).join("")}</div>`;
    refreshMedicareStatus();
  }

  if (!d.found) {
    // Only worth saying when there is nothing loaded at all; a user who has
    // imported already does not need to be told where the tracker isn't.
    if (d.status && !d.status.eligibility && !(d.status.referrals || []).length) {
      el.style.display = "";
      el.innerHTML = `${finished}<b>Order &amp; Referring Tracker not found.</b>
        If it is installed somewhere unusual, set <code class="inline">tracker_dir</code>
        in <code class="inline">config/mrfx.yaml</code> to its data folder and restart.
        Looked in:<div class="muted" style="margin-top:4px">${
          d.searched.map((s) => `<div><code class="inline">${esc(s)}</code></div>`).join("")}</div>`;
    } else el.style.display = "none";
    return;
  }
  if (!d.pending.length) {
    el.style.display = finished ? "" : "none";
    if (finished) el.innerHTML = finished +
      `<span class="muted">Everything the tracker has downloaded is imported.</span>`;
    return;
  }
  const licence = d.pending.some((p) => p.non_commercial)
    ? `<div class="muted" style="margin-top:6px"><b>Licence note:</b> DocGraph Hop Teaming is
       CareSet's research release under CC BY-NC-SA 4.0 — <b>non-commercial</b>. If you use it
       in paid consulting work, confirm your terms with CareSet. The CMS shared-patient file
       carries no such restriction.</div>` : "";
  el.style.display = "";
  // break-anywhere: a data folder path has no spaces and is long enough to
  // push the whole page into a horizontal scroll if it is allowed to run on
  el.innerHTML = `${finished}<b>Found the Order &amp; Referring Tracker.</b>
    <span class="muted" style="overflow-wrap:anywhere">${esc(d.data_dir)}</span>
    <div style="margin:8px 0">It has ${d.pending.length} file${d.pending.length === 1 ? "" : "s"}
      this app hasn't imported yet:</div>
    <ul style="margin:0 0 8px 18px">${d.pending.map((p) =>
      `<li>${esc(p.label)} <span class="muted">— ${esc(p.why)}, ${fmtInt(Math.round(p.size_mb))} MB</span></li>`).join("")}</ul>
    <button class="btn primary" id="md-tracker-import">Import ${d.pending.length === 1 ? "it" : "them"} now</button>
    <span class="muted" style="margin-left:8px">Nothing is re-downloaded, and you don't have to stop the app.</span>
    ${licence}`;
  $("#md-tracker-import").addEventListener("click", async (ev) => {
    ev.target.disabled = true;
    ev.target.textContent = "Starting…";
    state.trackerReported = false;
    try { await postJson("/api/medicare/tracker/import", {}); }
    catch (e) { alert("Could not start the import.\n\n" + e.message); }
    refreshTrackerCard();
  });
}

async function refreshMedicareStatus() {
  let st;
  try { st = await api("/api/medicare/status"); }
  catch { $("#md-status").textContent = "status unavailable"; return; }
  const parts = [];
  const el = st.eligibility;
  if (el) {
    let line = `Order &amp; Referring: <b>${fmtInt(el.providers)}</b> providers (release ${esc(el.release || "?")})`;
    if (el.age_days != null && el.age_days > STALE_ROSTER_DAYS) {
      line += ` <span class="warn-text">— ${el.age_days} days old (stale). CMS refreshes ~twice a week; re-download in the tracker and re-import.</span>`;
    }
    parts.push(line);
  }
  (st.referrals || []).forEach((r) => parts.push(`${esc(r.label)} ${esc(r.year)}: <b>${fmtInt(r.pairs)}</b> pairs`));
  $("#md-status").innerHTML = parts.join(" · ") || "nothing imported yet";
  // movement between the two most recent roster snapshots — losses are the
  // part a consultant acts on, so they get the warning color
  const chg = $("#md-changes");
  const lc = el && el.last_change;
  if (lc) {
    const lost = (lc.removed || 0) + (lc.partb_lost || 0);
    chg.style.display = "";
    chg.innerHTML = `Since release ${esc(lc.prev_release)}: +${fmtInt(lc.added)} joined the roster · ` +
      `${fmtInt(lc.removed)} dropped off · ` +
      `<span class="${lc.partb_lost ? "warn-text" : ""}">${fmtInt(lc.partb_lost)} lost Part B</span> · ` +
      `${fmtInt(lc.partb_gained)} regained it` +
      (lost ? ` — affected referral sources are flagged in the tables below.` : ``);
  } else chg.style.display = "none";
  // dataset picker: one option per loaded referral release, keep the pick across refreshes
  // Keyed by dataset_id, not year: CMS publishes one year at several windows
  // (30/60/90/180/365-day), which are different measurements and must stay
  // separately selectable.
  const yearSel = $("#md-year");
  const keep = yearSel.value;
  yearSel.innerHTML = `<option value="">newest loaded</option>` +
    (st.referrals || []).map((r) =>
      `<option value="${esc(r.dataset_id)}">${esc(r.label)} ${esc(r.year)}</option>`).join("");
  if ([...yearSel.options].some((o) => o.value === keep)) yearSel.value = keep;
  const nod = $("#md-nodata");
  if (!st.eligibility && !(st.referrals || []).length) {
    nod.style.display = "";
    // When the tracker card is already offering a one-click import, this
    // banner would repeat it and bury the button in prose. Say it once.
    if (state.trackerOffering) { nod.style.display = "none"; return; }
    nod.innerHTML = `<b>Nothing imported yet.</b> This tab reads two public CMS datasets that
      machine-readable files cannot supply: who may order/refer for Medicare, and who shares
      patients with whom. <b>If you have the Order &amp; Referring Tracker, use the Import
      button above</b> — the app finds its downloads and loads them in place, with nothing
      re-downloaded and nothing to stop.
      <div class="muted" style="margin-top:6px">Prefer the terminal, or the files live elsewhere?
      Stop the server first (imports need the database), then run
      <code class="inline">mrfx medicare --from-tracker</code>, or point at files by hand with
      <code class="inline">mrfx medicare --eligibility &lt;OrderReferring_&lt;date&gt;.csv&gt;</code> /
      <code class="inline">--referrals &lt;shared-patient or Hop Teaming .csv&gt;</code>.</div>`;
  } else nod.style.display = "none";
}

async function runMedicareOrg() {
  const out = $("#md-out");
  state.lastMedicareOrg = null;
  $("#md-csv").disabled = true;
  const subject = $("#md-subject").value.trim();
  if (!subject) { out.innerHTML = `<div class="empty"><h3>Pick a practice</h3>Type an entity name, TIN, or NPI above.</div>`; return; }
  out.classList.remove("empty");
  out.innerHTML = `<div class="loading">Looking up Medicare layers</div>`;
  let d;
  // limit 500 (the API max) so the table and its CSV carry everything up to
  // the cap — and total_partners says when even that truncates
  try { d = await postJson("/api/medicare/org", { subject, dataset_id: $("#md-year").value || null, limit: 500 }, { view: "medicare", label: "Practice lookup", anchor: "#md-run" }); }
  catch (e) { out.innerHTML = `<div class="empty"><h3>Could not look up</h3>${esc(e.message)}</div>`; return; }
  state.lastMedicareOrg = d;
  $("#md-csv").disabled = !(d.referrals_in.rows.length || d.referrals_out.rows.length);
  renderMedicareOrg(out, d);
}

// Y/N/– cell for an eligibility flag: null = the NPI isn't on the roster at all
const flagCell = (v) => v == null ? `<td class="num">–</td>`
  : v ? `<td class="num"><span class="ok-tag">Y</span></td>`
      : `<td class="num"><span class="warn-text">N</span></td>`;

function medicareEligTable(rows) {
  const body = rows.map((e) => `<tr>
      <td>${esc(e.npi)}<div class="sub">${esc(e.name || "")}</div></td>
      <td>${e.on_list ? '<span class="ok-tag">on list</span>' : '<span class="muted">not on list</span>'}</td>
      ${flagCell(e.partb)}${flagCell(e.dme)}${flagCell(e.hha)}${flagCell(e.pmd)}${flagCell(e.hospice)}
    </tr>`).join("");
  return `<div class="tablewrap"><table><thead><tr>
      <th>NPI</th><th>Order &amp; Referring</th><th class="num">Part B</th>
      <th class="num">DME</th><th class="num">HHA</th><th class="num">PMD</th><th class="num">Hospice</th>
    </tr></thead><tbody>${body}</tbody></table></div>`;
}

function medicareRefTable(ref, heading, emptyMsg) {
  // no silent caps: when the row limit truncates, the heading says "top N of
  // M" — otherwise a 500-row table reads as the whole referral base
  const total = ref.total_partners ?? ref.rows.length;
  const countNote = ref.rows.length < total
    ? ` · <span class="warn-text">top ${fmtInt(ref.rows.length)} of ${fmtInt(total)} — the CSV carries the same ${fmtInt(ref.rows.length)}</span>`
    : ` · ${fmtInt(total)} partner${total === 1 ? "" : "s"}`;
  const h = `<h3 style="margin-top:18px">${heading}${ref.dataset
    ? ` <span class="muted" style="font-weight:400">— ${esc(ref.dataset)} ${esc(ref.data_year)}${countNote}</span>` : ""}</h3>`;
  if (!ref.rows.length) return `${h}<div class="muted">${emptyMsg}</div>`;
  const body = ref.rows.map((r, i) => `<tr>
      <td class="num">${i + 1}</td>
      <td>${esc(r.name || "(name not identified)")}<div class="sub">${esc(r.npi)}</div></td>
      <td>${esc(r.specialty || "")}</td>
      <td class="num">${fmtInt(r.patients)}</td>
      <td class="num">${fmtInt(r.transactions)}</td>
      <td class="num">${r.avg_day_wait == null ? "–" : r.avg_day_wait}</td>
      <td>${r.on_orf ? (r.partb ? '<span class="ok-tag">eligible</span>'
                                : '<span class="warn-text" title="on the roster but Part B says N — claims ordered/referred by this provider are at denial risk">NOT eligible</span>')
                     : '<span class="muted" title="not on the Order & Referring roster — normal for facilities, labs and organizations; a denial risk only for a physician who refers">not on list</span>'}${
        r.recent_change ? `<div class="sub warn-text" title="changed between the two most recent roster snapshots you imported — the freshest possible warning this data can give">${
          r.recent_change === "removed" ? "dropped off the roster in the latest snapshot" : "lost Part B in the latest snapshot"}</div>` : ""}</td>
    </tr>`).join("");
  return `${h}<div class="tablewrap"><table><thead><tr>
      <th>#</th><th>Provider</th><th>Specialty</th><th class="num">Shared patients</th>
      <th class="num">Transactions</th><th class="num" title="average days between their visit and this practice's — Hop Teaming only">Avg wait (d)</th>
      <th>Part B order/refer</th>
    </tr></thead><tbody>${body}</tbody></table></div>`;
}

function renderMedicareOrg(out, d) {
  const st = d.status || {};
  const hasElig = !!st.eligibility;
  const hasRefs = (st.referrals || []).length > 0;
  const caveat = (d.referrals_in && d.referrals_in.caveat) || "";
  const parts = [`<div class="rc-summary"><b>${esc(d.display_name)}</b> —
      ${d.npis.length} NPI${d.npis.length === 1 ? "" : "s"} billing under ${d.tins.length}
      tax ID${d.tins.length === 1 ? "" : "s"} in this store.</div>`];

  parts.push(`<h3>This practice's providers — Order &amp; Referring status</h3>`);
  if (!hasElig) {
    parts.push(`<div class="muted">No Order &amp; Referring roster imported
      (<code class="inline">mrfx medicare --eligibility …</code>).</div>`);
  } else {
    parts.push(medicareEligTable(d.eligibility));
    parts.push(`<div class="muted" style="font-size:12px;margin-top:4px">Therapists and organizations
      are <b>not on this roster by design</b> — "not on list" here is expected, not a finding.
      The roster matters for the REFERRING physicians below.</div>`);
  }

  if (!hasRefs) {
    parts.push(`<h3 style="margin-top:18px">Referral structure</h3>
      <div class="muted">No shared-patient data imported
      (<code class="inline">mrfx medicare --referrals …</code>).</div>`);
  } else {
    parts.push(medicareRefTable(d.referrals_in, "Who shares patients INTO this practice",
      "No pairs found for this practice's NPIs in the selected dataset. CMS suppresses pairs under 11 patients, so a practice with a small Medicare book can legitimately show nothing."));
    parts.push(medicareRefTable(d.referrals_out, "Where its patients go next",
      "No outbound pairs found in the selected dataset."));
    parts.push(`<div class="disclaimer" style="margin-top:14px">${esc(caveat)}</div>`);
  }
  out.innerHTML = parts.join("");
}

async function runMedicareBatchCheck() {
  const outEl = $("#md-check-out");
  const msg = $("#md-check-msg");
  state.lastMedicareCheck = null;
  $("#md-check-csv").disabled = true;
  if (!$("#md-npis").value.trim()) {   // nothing pasted — ask, don't round-trip
    msg.textContent = "paste some NPIs first (one per line, or comma-separated)";
    outEl.innerHTML = "";
    return;
  }
  msg.textContent = "checking…";
  let d;
  try { d = await postJson("/api/medicare/eligibility", { text: $("#md-npis").value }); }
  catch (e) { msg.textContent = ""; outEl.innerHTML = `<div class="muted">${esc(e.message)}</div>`; return; }
  msg.textContent = "";
  state.lastMedicareCheck = d;
  $("#md-check-csv").disabled = false;
  const skipped = d.ignored_non_npi
    ? ` <span title="10-digit numbers that fail the NPI check digit — usually phone numbers">(${fmtInt(d.ignored_non_npi)} other 10-digit number${d.ignored_non_npi === 1 ? "" : "s"} skipped — not valid NPIs)</span>`
    : "";
  outEl.innerHTML = `<div class="muted" style="margin-bottom:6px"><b>${fmtInt(d.on_list)}</b> of
    <b>${fmtInt(d.checked)}</b> NPIs are on the Order &amp; Referring list.${skipped}</div>` +
    medicareEligTable(d.rows);
}

/* client-side CSV for the Medicare tables. Mirrors the server exports' two
   safety habits: spreadsheet-formula defusing (a value starting with = + @
   would execute in Excel) and the caveat traveling WITH the data as NOTE
   rows, exactly like the bundle's referral CSVs. */
function csvCell(v) {
  let s = String(v ?? "");
  if (/^[=+@]/.test(s) || (s.startsWith("-") && isNaN(Number(s)))) s = "'" + s;
  return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
}
function csvDownload(filename, rows) {
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob(
    [rows.map((r) => r.map(csvCell).join(",")).join("\r\n") + "\r\n"],
    { type: "text/csv" }));
  a.download = filename;
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 60000);
}

function downloadMedicareCsv() {
  const d = state.lastMedicareOrg;
  if (!d) return;
  const rows = [["direction", "rank", "npi", "name", "specialty", "taxonomy",
                 "shared_patients", "transactions", "avg_day_wait",
                 "still_eligible_partb", "recent_change", "dataset", "data_year"]];
  for (const [dir, ref] of [["source", d.referrals_in], ["destination", d.referrals_out]]) {
    (ref.rows || []).forEach((r, i) => rows.push([
      dir, i + 1, r.npi, r.name, r.specialty || "", r.taxonomy || "",
      r.patients, r.transactions, r.avg_day_wait ?? "",
      r.on_orf ? (r.partb ? "Y" : "N") : "", r.recent_change || "",
      ref.dataset || "", ref.data_year || ""]));
  }
  rows.push([]);
  for (const [dir, ref] of [["sources", d.referrals_in], ["destinations", d.referrals_out]]) {
    if ((ref.rows || []).length < (ref.total_partners ?? 0)) {
      rows.push([`NOTE: ${dir} truncated — this file carries the top ${ref.rows.length} ` +
                 `of ${ref.total_partners} partners by shared patients.`]);
    }
  }
  rows.push([`NOTE: ${d.referrals_in.caveat || ""}`]);
  const safe = (d.display_name || "practice").replace(/[^A-Za-z0-9]+/g, "_").slice(0, 48);
  csvDownload(`medicare_referrals_${safe}.csv`, rows);
}

function downloadMedicareCheckCsv() {
  const d = state.lastMedicareCheck;
  if (!d) return;
  const rows = [["npi", "on_order_referring_list", "name",
                 "partb", "dme", "hha", "pmd", "hospice", "release"]];
  const yn = (v) => (v == null ? "" : v ? "Y" : "N");
  d.rows.forEach((e) => rows.push([
    e.npi, e.on_list ? "Y" : "N", e.name || "",
    yn(e.partb), yn(e.dme), yn(e.hha), yn(e.pmd), yn(e.hospice), e.release || ""]));
  rows.push([]);
  rows.push(["NOTE: therapists and organizations are not on the Order & Referring "
    + "roster at all, so N is expected for them and is not a finding. Absence "
    + "matters for physicians who order or refer."]);
  csvDownload("medicare_npi_check.csv", rows);
}

/* =======================================================================
   LEADS + RATE-CHANGE MONITORING
   ======================================================================= */

async function postDownload(url, payload, filename, bar) {
  let jb = null, poll = null;
  const headers = { "Content-Type": "application/json" };
  if (bar) {
    const token = newToken();
    headers["X-Mrfx-Progress"] = token;
    jb = mountJobbar(bar, token);
    poll = startPoll(token, jb, bar);
  }
  try {
    const r = await fetch(url, {
      method: "POST", headers,
      body: JSON.stringify(payload),
    });
    if (poll) poll.stop();
    if (!r.ok) {
      if (jb) { if (r.status === 409) { jb.stopped(); return; } jb.failed("not built"); }
      alert("The export was not built.\n\n" + await refusalText(r)); return;
    }
    const blob = await r.blob();
    if (jb) jb.done();
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = filename;
    a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 60000);
  } catch (e) { if (poll) poll.stop(); if (jb) jb.failed("failed"); alert("export failed: " + e.message); }
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
  if (payers) $("#ld-payer-chips").innerHTML = payerChipsHtml(payers);
  $("#ld-payer-chips").addEventListener("click", (ev) => { const b = ev.target.closest(".chip"); if (b) b.classList.toggle("on"); });
  if (states) $("#ld-states").innerHTML = (states.states || []).map((s) => `<option value="${esc(s)}">`).join("");
  if (subjects) $("#ld-subjects").innerHTML = subjectOptionsHtml(subjects);
  wireSubjectSearch("#ld-exclude", "#ld-subjects");
  $("#ld-run").addEventListener("click", runLeads);
  initNewToNetwork();
  initNewClinics();
  initClosures();
  initStealShare();
  initDossier();
  initGrowth();
  initMarketSizing();
  $("#ld-csv").addEventListener("click", () => { if (state.lastLeadsPayload) postDownload("/api/leads.csv", state.lastLeadsPayload, "leads.csv", { view: "leads", label: "Leads CSV", anchor: "#ld-csv" }); });
  $("#ld-board-run").addEventListener("click", runLeaderboard);
}

async function runLeaderboard() {
  const el = $("#ld-board");
  const payload = leadsPayload();  // reuse the same market scope
  const sort = $("#ld-board-sort").value;
  el.innerHTML = `<div class="loading">Building market leaderboard</div>`;
  let d;
  const isFresh = ticket("leaderboard");
  try {
    d = await postJson("/api/leaderboard", {
      market: payload.market, min_codes: payload.min_codes, sort, limit: 50 }, { view: "leads", label: "Leaderboard", anchor: "#ld-board-run" });
  } catch (e) {
    if (!isFresh()) return;
    el.innerHTML = `<div class="empty">Couldn't build the leaderboard (${esc(e.message)}).</div>`; return;
  }
  if (!isFresh()) return;
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
    ? monthOptionsHtml({ months: (months.months || []).filter(Boolean), covered: months.covered })
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
  market.peer_group = $("#ld-peer").value;
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
  const isFresh = ticket("leads");
  try { data = await postJson("/api/leads", payload, { view: "leads", label: "Leads", anchor: "#ld-run" }); } catch (e) {
    if (!isFresh()) return;
    out.innerHTML = `<div class="empty"><h3>Search failed</h3>${esc(e.message)}</div>`; return;
  }
  if (!isFresh()) return;
  state.lastLeadsPayload = payload;
  $("#ld-csv").disabled = false;
  renderLeads(out, data);
}

function renderLeads(out, data) {
  if (!data.leads.length) {
    const therapyOn = $("#ld-peer") && $("#ld-peer").value !== "all";
    out.innerHTML = `<div class="empty"><h3>No leads found</h3>No practices at or below p${data.threshold_percentile} that price at least ${data.min_codes} codes with a market. Widen the cutoff or lower the min codes.${therapyOn ? ` <br><span class="muted">"Compare against" is limited to ${esc($("#ld-peer").selectedOptions[0].textContent)} — it excludes hospital systems, facilities and physician groups (and practices not yet identified). Choose "all practices", or let NPI identification finish, to see more.</span>` : ""}</div>`;
    return;
  }
  const rows = data.leads.map((l) => `<tr>
    <td>${esc(l.display_name || "")}<div class="sub">${esc(l.entity_kind || "")}${l.website ? ` · <a href="${esc(l.website)}" target="_blank" rel="noopener">site</a>` : ""}</div>${(l.not_in_newest || []).length ? `<div class="sub warn-text" title="its rate here is from an older file of that payer; the payer may have dropped it, or its newest month is only partly loaded">⚠ not in ${esc(l.not_in_newest.map((d) => `${d.payer}'s newest file (${d.payer_newest}; last seen ${d.last_seen})`).join(", "))}</div>` : ""}</td>
    <td class="sub">${esc([l.city, l.state].filter(Boolean).join(", "))}${l.multi_state ? ` <span class="muted" title="${esc((l.states || []).join(", "))}">(+${(l.states || []).length - 1} more state${(l.states || []).length - 1 === 1 ? "" : "s"})</span>` : ""}</td>
    <td class="num">${fmtInt(l.npi_count)}</td>
    <td class="num">${l.n_codes}</td>
    <td class="num">p${l.median_percentile}</td>
    <td class="num">${money(l.avg_gap_to_median)}</td>
    <td>${l.worst_payer ? `${esc(l.worst_payer)}<div class="sub">${money(l.worst_payer_gap)}/code under mkt${l.worst_payer_codes ? ` · ${l.worst_payer_codes} ${l.worst_payer_basis === "shared" ? "shared " : ""}code${l.worst_payer_codes === 1 ? "" : "s"}` : ""}</div>` : "<span class='muted'>–</span>"}</td>
    <td class="sub">${esc(l.tin_value)}</td></tr>`).join("");
  const cut = data.truncated
    ? ` <span class="warn-text">The list stops at ${fmtInt(data.limit)} — the market holds more. Narrow it (state, payer) or raise the min codes to see the rest.</span>`
    : "";
  out.innerHTML = `<div class="rc-summary">${data.count} practice(s) at or below p${data.threshold_percentile}, most underpaid first.${cut} <span class="muted">"Underpaid by" is the payer with the biggest average shortfall vs the market median — your outreach hook. Avg $ below median is a rate-level gap, not annual dollars.</span></div>
    <div class="tablewrap"><table class="rc-table"><thead><tr><th>Practice</th><th>Location</th><th class="num">Providers</th><th class="num">Codes</th><th class="num">Position</th><th class="num" title="average dollars under the median of the other practices, over the codes with a market">Avg $ below median</th><th title="the payer whose rates sit the most DOLLARS under the market on the codes all its payers price (the dossier's 'weakest payer' is a different measure: their lowest position among a payer's practices)">Underpaid most by ($ on shared codes)</th><th>Tax ID</th></tr></thead><tbody>${rows}</tbody></table></div>`;
}

let changesInit = false;
async function initChanges() {
  if (changesInit) {
    refreshSubjectPickers("#ch-subjects",
      [{ sel: "#ch-month", prefix: `<option value="latest">Latest — each payer's newest file vs its previous one</option>` },
       { sel: "#ch-prev", prefix: `<option value="">auto — the previous month present</option>` }]);
    return;
  }
  changesInit = true;
  const [subjects, months, payers] = await Promise.all([
    api("/api/benchmark/subjects").catch(() => null),
    api("/api/months").catch(() => null),
    api("/api/payers").catch(() => null),
  ]);
  fillMonthSelect("#ch-month", months, `<option value="latest">Latest — each payer's newest file vs its previous one</option>`);
  fillMonthSelect("#ch-prev", months, `<option value="">auto — the previous month present</option>`);
  if (payers) $("#ch-payer-chips").innerHTML = payerChipsHtml(payers);
  // the State box's suggestions (list="ld-states") were filled only once the
  // Leads tab had been opened
  if (!$("#ld-states").options.length) {
    api("/api/states").then((d) => {
      if (!$("#ld-states").options.length)
        $("#ld-states").innerHTML = (d.states || []).map((x) => `<option value="${esc(x)}">`).join("");
    }).catch(() => {});
  }
  $("#ch-payer-chips").addEventListener("click", (ev) => { const b = ev.target.closest(".chip"); if (b) b.classList.toggle("on"); });
  if (subjects) $("#ch-subjects").innerHTML = subjectOptionsHtml(subjects);
  wireSubjectSearch("#ch-subject", "#ch-subjects");
  $("#ch-run").addEventListener("click", runChanges);
  $("#ch-csv").addEventListener("click", () => { if (state.lastChangesPayload) postDownload("/api/changes.csv", state.lastChangesPayload, "rate_changes.csv", { view: "changes", label: "Rate changes CSV", anchor: "#ch-csv" }); });
  $("#ch-traj-run").addEventListener("click", runTrajectory);
}

async function runTrajectory() {
  const el = $("#ch-traj");
  // trajectory spans all months, so it ignores the two-month picker; it reuses
  // the payer / discipline / STATE scope of the changes market (it was always
  // the national call: 20 s on a 99M store where one state takes 2 s)
  const market = { payers: $$("#ch-payer-chips .chip.on").map((c) => c.dataset.payer) };
  if ($("#ch-disc").value) market.discipline = $("#ch-disc").value;
  const st = $("#ch-state").value.trim();
  if (st) market.state = st;
  else market.allow_national = true;
  el.innerHTML = `<div class="loading">Tracing payer trajectories across all months${st ? ` in ${esc(st.toUpperCase())}` : " in every state (the slowest view on a big store)"}</div>`;
  let d;
  try { d = await postJson("/api/trajectory", { market }, { view: "changes", label: "Payer trajectory", anchor: "#ch-traj-run" }); }
  catch (e) { el.innerHTML = `<div class="empty">Couldn't build the trajectory (${esc(e.message)}).</div>`; return; }
  if (!d.count) {
    el.innerHTML = `<div class="ov-block"><h3>Payer trajectory</h3><div class="muted">No payer has ≥2 months of data under this scope. Re-ingest the payers' newer MRFs (a later file month).</div></div>`;
    return;
  }
  const arrow = { down: "▼", up: "▲", flat: "▬" };
  const pctTxt = (v) => (v == null ? "–" : (v > 0 ? "+" : "") + v + "%");
  const rows = d.payers.map((p) => {
    const cls = p.direction === "down" ? "chg-cut" : p.direction === "up" ? "chg-up" : "";
    // real terms is the point of this table: a payer flat in dollars is still
    // cutting pay, so colour it by the REAL sign, not the nominal one
    const realCls = p.real_cumulative_pct == null ? ""
      : p.real_cumulative_pct < 0 ? "chg-cut" : "chg-up";
    return `<tr>
      <td>${esc(p.payer)}</td>
      <td class="num muted">${esc(p.first_month)}→${esc(p.last_month)} (${p.n_months})</td>
      <td class="num muted" title="the codes and practices this payer priced in both its first and last month — the change is measured on these only">${fmtInt(p.panel_codes || 0)} code${p.panel_codes === 1 ? "" : "s"} · ${fmtInt(p.panel_practices || 0)} practice${p.panel_practices === 1 ? "" : "s"}</td>
      <td class="num ${cls}">${p.cumulative_pct == null ? "<span class='muted' title='no practice was priced on the same code in its first and last month'>no matched panel</span>" : `${arrow[p.direction] || ""} ${pctTxt(p.cumulative_pct)}`}</td>
      <td class="num ${realCls}" title="${esc(p.erosion_line || p.real_reason || "")}">${pctTxt(p.real_cumulative_pct)}</td>
      <td class="num muted">${p.cut_streak || 0}</td></tr>`;
  }).join("");
  const inf = d.inflation || {};
  const eroding = d.payers.filter((p) => p.erosion_line &&
    p.real_cumulative_pct != null && p.real_cumulative_pct < 0);
  // the sentence a consultant reads aloud in a renewal meeting
  const lines = eroding.length
    ? `<ul class="plain">${eroding.slice(0, 6).map((p) => `<li>${esc(p.erosion_line)}</li>`).join("")}</ul>`
    : "";
  const basis = inf.basis === "index"
    ? `Real terms use ${esc(inf.index_name || "an index")} (${esc(inf.source || "")}).`
    : inf.basis === "assumption"
      ? `Real terms use YOUR assumption of ${inf.assumed_pct_per_year}%/yr — an assumption, not a measurement.`
      : `<b>No real-terms figures:</b> ${esc(inf.reason || "no index year covers this span.")}`;
  // the scope it was computed for (state · payers · discipline), and the
  // months the series cover — "all 3 loaded months" stayed after a MO run
  el.innerHTML = `<div class="ov-block"><h3>Payer rate trajectory — ${d.months.length} month${d.months.length === 1 ? "" : "s"} (${esc(d.months[0] || "")} → ${esc(d.months[d.months.length - 1] || "")})${changesScopeText() || " — every state"}</h3>
    <div class="muted" style="margin-bottom:6px">${esc(d.note)} Most-eroding first.</div>
    ${lines}
    <div class="tablewrap"><table>
      <thead><tr><th>Payer</th><th class="num">Span</th><th class="num">Matched panel</th><th class="num">Cumulative</th><th class="num" title="the same change restated in constant dollars — a flat rate is a real cut">Real terms</th><th class="num">Cut streak</th></tr></thead>
      <tbody>${rows}</tbody></table></div>
    <div class="muted" style="margin-top:6px">${basis} ${esc(inf.caveat || "")}</div></div>`;
}

function changesPayload() {
  const month = $("#ch-month").value;
  if (!month) throw new Error("the new month is required");
  const market = { month, payers: $$("#ch-payer-chips .chip.on").map((c) => c.dataset.payer) };
  if ($("#ch-prev").value) market.prev_month = $("#ch-prev").value;
  if ($("#ch-disc").value) market.discipline = $("#ch-disc").value;
  if ($("#ch-state").value.trim()) market.state = $("#ch-state").value.trim();
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
  // both dropdowns offer every loaded month, so the pair can be chosen out of
  // order. Say which way round it goes here rather than spending a round trip
  // to be told — the server still validates.
  const mNew = $("#ch-month").value, mPrev = $("#ch-prev").value;
  if (mNew === "latest" && mPrev) {
    out.innerHTML = `<div class="empty"><h3>"Latest" compares each payer with its own previous file</h3>
      Leave "compare to" on auto, or pick a specific new month to compare against ${esc(mPrev)}.</div>`;
    return;
  }
  if (mPrev && mNew && mNew !== "latest" && mPrev >= mNew) {
    out.innerHTML = `<div class="empty"><h3>Pick the earlier month to compare against</h3>
      ${esc(mPrev === mNew
        ? `Both boxes are set to ${mNew}. Choose an earlier month in "compare to".`
        : `${mPrev} is not earlier than ${mNew} — a rate move only has a direction when the two months are in order.`)}</div>`;
    return;
  }
  out.innerHTML = `<div class="loading">Comparing months</div>`;
  let data;
  const isFresh = ticket("changes");
  try { data = await postJson("/api/changes", payload, { view: "changes", label: "Rate changes", anchor: "#ch-run" }); } catch (e) {
    if (!isFresh()) return;
    out.innerHTML = `<div class="empty"><h3>Could not compare</h3>${esc(e.message)}</div>`; return;
  }
  if (!isFresh()) return;
  state.lastChangesPayload = payload;
  $("#ch-csv").disabled = false;
  renderChanges(out, data);
}

function changesScopeText() {
  const bits = [];
  const st = ($("#ch-state") || {}).value;
  if (st && st.trim()) bits.push(esc(st.trim().toUpperCase()));
  const payers = $$("#ch-payer-chips .chip.on").map((c) => esc(c.dataset.payer));
  if (payers.length) bits.push(payers.join(", "));
  const disc = ($("#ch-disc") || {}).value;
  if (disc) bits.push(esc(disc));
  return bits.length ? ` — ${bits.join(" · ")}` : "";
}

function renderChanges(out, d) {
  // per payer: which two of ITS files were compared, and which payers have
  // only one file loaded (nothing to compare yet)
  const pairsText = (d.compared || []).map((c) =>
    `${esc(c.payer)} ${esc(c.prev_month)} → ${esc(c.new_month)}`).join("; ");
  const span = d.per_payer
    ? (d.new_month && d.new_month !== "latest"
        ? `each payer's ${esc(d.new_month)} file vs its own previous one (${pairsText})`
        : `each payer's newest file vs its previous one (${pairsText})`)
    : `${esc(d.prev_month)} → ${esc(d.new_month)}`;
  // what the result was computed for (review 10, P3: it did not say)
  const scope = changesScopeText();
  // a block AFTER the sentence: inside it, the sentence's "." and "Biggest
  // cut" started a new line (". Biggest cut -10%.")
  const notCompared = (d.not_compared || []).length
    ? `<div class="sub">Not compared (no file for one of the two months): ${
        d.not_compared.map((c) => esc(c.payer)).join(", ")}</div>` : "";
  if (!d.changes.length) {
    out.innerHTML = `<div class="empty"><h3>No changes</h3>No rate moves, ${span}${scope}.${notCompared}</div>`;
    return;
  }
  const rows = d.changes.map((c) => {
    const cls = c.direction === "cut" ? "chg-cut" : "chg-up";
    return `<tr><td>${esc(c.payer)}</td>
      <td>${esc(c.display_name || "")}<div class="sub">${esc(c.tin_value)}</div></td>
      <td>${esc(c.billing_code)}<div class="sub">${esc(c.description || "")}${c.modifier_set ? ` · ${esc(c.modifier_set)}` : ""}${
        c.billing_class && c.billing_class !== "professional" ? ` · ${esc(c.billing_class)}` : ""}${
        c.service_code_set ? ` · POS ${esc(String(c.service_code_set).replaceAll("|", ", "))}` : ""}</div></td>
      <td class="num">$${fmtMoney(c.old_rate)}</td><td class="num">$${fmtMoney(c.new_rate)}</td>
      <td class="num ${cls}">${c.delta < 0 ? "−" : "+"}$${fmtMoney(Math.abs(c.delta))}</td>
      <td class="num ${cls}">${c.pct_change > 0 ? "+" : ""}${c.pct_change}%</td>${
        d.per_payer ? `<td class="sub">${esc(c.prev_month)} → ${esc(c.new_month)}</td>` : ""}</tr>`;
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
  out.innerHTML = `<div class="rc-summary"><b>${d.n_cuts}</b> cut(s), <b>${d.n_increases}</b> increase(s), ${span}.${d.biggest_cut_pct != null ? ` Biggest cut ${d.biggest_cut_pct}%.` : ""}${capNote}${notCompared}</div>
    ${byPayer}
    <div class="tablewrap"><table class="rc-table"><thead><tr><th>Payer</th><th>Practice</th><th>Code</th><th class="num">Was</th><th class="num">Now</th><th class="num">Δ</th><th class="num">%</th>${d.per_payer ? "<th>Months</th>" : ""}</tr></thead><tbody>${rows}</tbody></table></div>`;
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
  if (qa.pos_in_modifier_rows) bits.push(`<span title="This payer put place-of-service codes (11 office, 22 outpatient hospital, ...) in the modifier field. MRF Explorer moved them to place of service, so these rates count as base (no modifier).">${fmtInt(qa.pos_in_modifier_rows)} POS codes moved out of modifiers</span>`);
  if (qa.future_header_date) bits.push(`<span class="warn" title="The payer dated this file ${esc(qa.future_header_date)} — later than the day it was read — so it is filed under the month in its name (or the month it was read) instead.">dated in the future (${esc(String(qa.future_header_date).slice(0, 10))})</span>`);
  if (qa.modifier_split_rows) bits.push(`<span title="This payer wrote two modifiers as one (e.g. &quot;52,53&quot;). MRF Explorer split them.">${fmtInt(qa.modifier_split_rows)} combined modifiers split</span>`);
  if (qa.duplicate_explosion_ratio > 1.5) bits.push(`<span class="warn">${qa.duplicate_explosion_ratio}x dup</span>`);
  return bits.length
    ? `<span class="qa-line" title="${esc(qa.outlier_rule || "")}">${bits.join(" · ")}</span>`
    : `<span class="qa-line muted">clean</span>`;
}

let filesInFlight = false;
async function loadFiles() {
  // the 4 s poll must not stack requests behind a slow store, nor run while
  // the browser tab is hidden (audit 8 P1-C)
  if (filesInFlight || document.hidden) return;
  filesInFlight = true;
  try { await loadFilesNow(); } finally { filesInFlight = false; }
}

async function loadFilesNow() {
  const body = $("#files-body"), stateEl = $("#files-state");
  let d;
  try { d = await api(`/api/files?limit=${state.filesLimit || 500}`); }
  catch (e) { stateEl.innerHTML = `<div class="empty">${esc(e.message)}</div>`; return; }
  if (!d.files.length) {
    body.innerHTML = "";
    stateEl.innerHTML = `<div class="empty"><h3>No files yet</h3>
      Drop an MRF into <code class="inline">data/inbox/</code> or use the upload zone above.</div>`;
    return;
  }
  // the one-time stored-row update still running behind the dashboard, and
  // payer x month cells pooled from several files (review 12 items 2.9, 2.5)
  const ru = d.rows_update;
  const ruLine = ru && (ru.running || ru.pending)
    ? `<div class="warn-text">Updating stored rows to this version's rules: ${fmtInt(ru.done || 0)} of ${fmtInt(ru.total || 0)} files — numbers from the files not yet updated can still change.</div>`
    : "";
  const pm = d.pooled_months || [];
  const pmLine = pm.length
    ? `<div class="muted" title="${esc(d.pooled_note || "")}">Pooled months: ${pm.slice(0, 4).map((r) =>
        `${esc(r.payer)} ${esc(r.file_month || "")} has ${fmtInt(r.files)} files, ${fmtInt(r.practices_in_several)} practices priced in more than one (${fmtInt(r.lines_differing)} lines at different rates)`).join("; ")}${pm.length > 4 ? `; and ${fmtInt(pm.length - 4)} more` : ""} — their rates are pooled (median), not one file replacing another.</div>`
    : "";
  const an = d.analytics;
  const anLine = an && an.state && an.state !== "current"
    ? `<div class="warn-text">${an.state === "failed" ? "Analytics update stopped — " : an.state === "building" ? "Analytics: " : "Analytics behind — "}${esc(an.sentence || "")}${an.state === "building" && an.pct != null
        ? ` <span class="progress" style="display:inline-block;width:160px;vertical-align:middle"><span class="progress-fill" style="width:${Math.max(0, Math.min(100, an.pct))}%"></span></span>`
        : ""}</div>`
    : "";
  // files read before the user ADDED billing codes (review 13 item 5.2.4)
  const rr = d.reread;
  const rrLine = rr && rr.count
    ? `<div class="warn-text">${fmtInt(rr.count)} loaded file${rr.count === 1 ? " was" : "s were"} read before ` +
      `you added billing code${rr.added.length === 1 ? "" : "s"} ${esc(rr.added.join(", "))} — ` +
      (rr.running ? "re-reading them now…" :
        `<button class="btn" id="files-reread" style="padding:2px 8px;font-size:12px">Re-read ${fmtInt(rr.count)} file${rr.count === 1 ? "" : "s"} for the new codes</button>`) +
      (rr.cannot_count ? ` <span class="muted">${fmtInt(rr.cannot_count)} more ha${rr.cannot_count === 1 ? "s" : "ve"} no kept copy and no link — drop ${rr.cannot_count === 1 ? "it" : "them"} in again: ${esc((rr.cannot || []).join(", "))}</span>` : "") +
      `</div>`
    : (rr && rr.cannot_count ? `<div class="muted">${fmtInt(rr.cannot_count)} loaded file${rr.cannot_count === 1 ? " was" : "s were"} read before you added billing codes and ha${rr.cannot_count === 1 ? "s" : "ve"} no kept copy and no link — drop ${rr.cannot_count === 1 ? "it" : "them"} in again to read the new codes: ${esc((rr.cannot || []).join(", "))}</div>` : "");
  // automatic backups (review 13 Group 6.3): off, or the last one taken
  const bk = d.backup;
  const bkLine = bk && bk.sentence
    ? `<div class="${bk.on && bk.last && bk.last.error ? "warn-text" : "muted"}" id="files-backup">${esc(bk.sentence)}` +
      (bk.on ? ` <button class="btn" id="files-backup-now" style="padding:2px 8px;font-size:12px">Back up now</button>` : "") +
      `</div>`
    : "";
  // older files being named by content (review 14 item 2.2)
  const cb = d.content_backfill;
  const cbLine = cb && cb.sentence ? `<div class="muted">${esc(cb.sentence)}</div>` : "";
  stateEl.innerHTML = anLine + rrLine + ruLine + pmLine + bkLine + cbLine + (d.total > d.files.length
    ? `<div class="muted">Showing the newest ${fmtInt(d.files.length)} of ${fmtInt(d.total)} files.
        <button class="btn" id="files-more" style="padding:2px 8px;font-size:12px">show ${fmtInt(Math.min(d.total, (state.filesLimit || 500) * 4))}</button></div>`
    : "");
  const rrBtn = document.getElementById("files-reread");
  if (rrBtn) rrBtn.onclick = async () => {
    rrBtn.disabled = true;
    try { await postJson("/api/files/reread", {}); } catch (e) { alert(e.message); }
    loadFiles();
  };
  const bkBtn = document.getElementById("files-backup-now");
  if (bkBtn) bkBtn.onclick = async () => {
    bkBtn.disabled = true;
    try {
      const r = await postJson("/api/backup", {}, { view: "files", label: "Backup", anchor: "#files-backup" });
      if (r && r.verdict) alert(`Backup written: ${r.path}\n${r.verdict}`);
    } catch (e) { alert(e.message); }
    loadFiles();
  };
  const more = $("#files-more");
  if (more) more.onclick = () => { state.filesLimit = Math.min(5000, (state.filesLimit || 500) * 4); loadFiles(); };
  body.innerHTML = d.files.map((f) => {
    let warn = f.identity_note ? `<span class="muted" title="${esc(f.identity_note)}">ⓘ loaded before identities were kept</span> ` : "";
    if (f.ref_groups_skipped > 0) {
      const remote = (f.qa && f.qa.remote_ref_groups) || 0;
      warn = remote >= f.ref_groups_skipped
        ? `<span class="warn-text" title="The payer published these provider lists as separate web links (location URLs). MRF Explorer does not download those, and dropping the linked files in will not fill them.">⚠ ${fmtInt(f.ref_groups_skipped)} rate groups skipped — their provider lists are remote links MRF Explorer doesn't download</span>`
        : remote > 0
          ? `<span class="warn-text">⚠ ${fmtInt(f.ref_groups_skipped)} rate groups skipped — ${fmtInt(remote)} point at remote provider lists MRF Explorer doesn't download; the rest need the payer's provider reference file</span>`
          : `<span class="warn-text">⚠ ${fmtInt(f.ref_groups_skipped)} rate groups skipped — missing provider reference file</span>`;
      const amb = (f.qa && f.qa.ambiguous_ref_ids) || 0;
      if (amb > 0 && remote < f.ref_groups_skipped)
        warn = `<span class="warn-text" title="This file has no provider list of its own, and two of the payer's provider reference files from the same month define the same reference ids differently (for example a PPO and an HMO list). MRF Explorer cannot tell which one this file means, so it attached neither rather than guess.">⚠ ${fmtInt(f.ref_groups_skipped)} rate groups skipped — ${fmtInt(amb)} reference id(s) defined differently by two of the payer's reference files; neither used</span>`;
    }
    // a group whose references were only PARTLY found kept the practices it
    // could name and dropped the rest — say so (round 7 P1.7)
    const missingIds = (f.qa && f.qa.missing_ref_ids) || 0;
    if (missingIds > 0 && !(f.ref_groups_skipped > 0)) {
      warn = `<span class="warn-text" title="Some rate groups listed several provider references and only some were in the file. The practices behind the missing ones are not in the store yet; dropping the payer's provider reference file in re-ingests this file automatically.">⚠ ${fmtInt(missingIds)} provider reference(s) not found — some practices missing; needs the payer's provider reference file</span>`;
    }
    // rule-3 migration: an older version may have dropped spelled-out
    // "fee for service" items as bundled — only a re-ingest can tell
    if (!warn && f.qa && f.qa.reingest_suggested)
      warn = `<span class="warn-text" title="${esc(f.qa.reingest_suggested)}">⚠ re-ingest suggested — ${fmtInt(f.qa.bundled_items || 0)} item(s) were left out as bundled by an older version</span>`;
    let statusExtra = "";
    if (f.status === "pending_confirmation")
      statusExtra = ` <button class="btn" style="padding:2px 8px;font-size:12px" data-confirm="${esc(f.filename)}">ingest anyway</button>`;
    // a different file under a loaded name, same payer and month: the user
    // decides — never blended into every number silently (review 9 4.5)
    if (f.status === "needs_choice")
      statusExtra = ` <button class="btn" style="padding:2px 8px;font-size:12px" title="use only this file: the loaded one's rates are replaced by this one's" data-choose="replace" data-file="${esc(f.filename)}">replace</button>`
        + ` <button class="btn" style="padding:2px 8px;font-size:12px" title="load this file too: both files' rates count toward every number" data-choose="keep" data-file="${esc(f.filename)}">keep both</button>`;
    if (f.status === "processing")
      statusExtra = ` <button class="btn" style="padding:2px 8px;font-size:12px" title="stop processing this file now — nothing from this run is saved; a downloaded link can be retried without downloading again" data-stop-file="${esc(f.filename)}">stop</button>`;
    const inflight = (f.status === "processing" || f.status === "queued") && f.chunks_total > 0;
    const progressBar = inflight
      ? `<div class="progress" title="working through the file in chunks">
           <div class="progress-fill" style="width:${Math.round(f.progress || 0)}%"></div>
           <span class="progress-label">chunk ${fmtInt(f.chunks_done)}/${fmtInt(f.chunks_total)} · ${Math.round(f.progress || 0)}%</span>
         </div>`
      : "";
    return `<tr>
      <td>${esc(f.filename)}</td>
      <td>${esc(f.payer || "?")}${f.file_month ? `<div class="sub" title="Rates month = the payer's own date in the file (last_updated_on). Published = the date in the file's name. Payers' clocks differ, so these can disagree.">rates month ${esc(f.file_month)}${f.published_month && f.published_month !== f.file_month ? ` · published ${esc(f.published_month)}` : ""}</div>` : ""}</td>
      <td>${esc(f.file_type || "?")}${f.schema_version && !String(f.schema_version).startsWith("2.") ? ` <span class="warn-text">v${esc(f.schema_version)}</span>` : ""}</td>
      <td><span class="badge ${esc(f.status)}">${esc(f.status)}</span>${statusExtra}${progressBar}</td>
      <td class="num">${fmtInt(f.rows_emitted)}</td>
      <td>${qaLine(f.qa)}</td>
      <td>${warn}${f.error ? `<div class="${f.status === "pending_confirmation" ? "warn-text" : f.status === "skipped" || f.status === "done" ? "muted" : "err-text"}">${esc(f.error)}</div>` : ""}</td>
      <td class="sub">${f.finished_at ? esc(localTime(f.finished_at)) : ""}</td>
      <td>${f.status === "processing" || f.status === "queued" ? "" : `<button class="btn forget-btn" title="erase this file's rates and free its disk space (re-add its link or file to get it back)" data-forget="${esc(f.filename)}">remove</button>`}</td>
    </tr>`;
  }).join("");
  $$("[data-choose]", body).forEach((b) =>
    b.addEventListener("click", async () => {
      b.disabled = true;
      await chooseHeld(b.dataset.file, b.dataset.choose);
      loadFiles();
    }));
  $$("[data-confirm]", body).forEach((b) =>
    b.addEventListener("click", async () => {
      b.disabled = true;
      try {
        const r = await fetch(`/api/files/${encodeURIComponent(b.dataset.confirm)}/confirm`, { method: "POST" });
        if (!r.ok) alert((await r.json().catch(() => ({}))).detail || `confirm failed (HTTP ${r.status})`);
      } catch (e) { alert("confirm failed: " + e.message); b.disabled = false; return; }
      loadFiles();
    }));
  $$("[data-stop-file]", body).forEach((b) =>
    b.addEventListener("click", async () => {
      if (!confirm(`Stop processing ${b.dataset.stopFile}?\n\nNothing from this run is saved. If it came from a link, its download is kept and "retry" processes it again without re-downloading.`)) return;
      b.disabled = true;
      b.textContent = "stopping…";
      try {
        const r = await fetch(`/api/files/${encodeURIComponent(b.dataset.stopFile)}/stop`, { method: "POST" });
        if (!r.ok) alert((await r.json().catch(() => ({}))).detail || `stop failed (HTTP ${r.status})`);
      } catch (e) { alert("stop failed: " + e.message); }
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
  // the same order as the table below (store.URL_LIST_ORDER): what is moving
  // now, then what needs you, then what is finished, then what waits
  const countLabel = {
    // skips are the app working as designed (duplicates of files already
    // ingested, files without the target codes) — say so, or a healthy
    // grind reads as mass failure
    skipped: (n) => `${n} skipped <span class="muted">(duplicates &amp; non-matches — normal)</span>`,
    // over-size files are their OWN category, not failures — otherwise one big
    // payer's shards make the failure count look alarming and bury real errors
    oversize: (n) => `<span class="warn-text">${n} too big (needs your OK)</span>`,
    waiting: (n) => `${n} waiting <span class="muted">(copies of a file already downloading — skipped for free if it matches)</span>`,
    paused: (n) => `<span class="warn-text">${n} paused <span class="muted">(their servers stopped sending — parked ones rejoin the line within minutes, the rest retry by themselves)</span></span>`,
    failed: (n) => `<span class="err-text">${n} failed</span>`,
  };
  const parts = ["downloading", "expanding", "ingesting", "fetched", "oversize", "paused",
                 "waiting", "failed", "done", "skipped", "queued"]
    .filter((k) => c[k]).map((k) => (countLabel[k] || ((n) => `${n} ${k}`))(fmtInt(c[k])));
  const ds = d.settings || {};
  state.monthlyRecheck = ds.monthly_recheck !== false;
  const setLine = ds.downloads
    ? `<div class="sub muted" style="margin-top:4px">Downloads: ${fmtInt(ds.downloads)} at a time`
      + (ds.small_file_downloads ? ` (${fmtInt(ds.small_file_downloads)} kept for files of ${fmtInt(ds.small_file_mb)} MB or less)` : "")
      + (ds.early_dedup_mb ? ` · copies of a loaded file are recognised after the first ${fmtInt(ds.early_dedup_mb)} MB` : " · early duplicate check off")
      + ` · change these in ${esc(ds.config_file || "config/mrfx.yaml")} (parallel_downloads, small_file_downloads, small_file_mb, early_dedup_mb), then restart</div>`
    : "";
  countsEl.innerHTML = setLine + parts.join(" · ") +
    (total > d.urls.length ? ` <span class="muted">(showing ${fmtInt(d.urls.length)} of ${fmtInt(total)} links — every active, too-big, paused and failed one; the newest finished; the next queued)</span>` : "");
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
  // a row that left "downloading" (parked, finished, handed back) must not
  // carry its old stall timer into its next download (audit 8 P2-E)
  for (const u of d.urls) if (u.status !== "downloading") _stallSeen.delete(u.id);
  body.innerHTML = d.urls.map((u) => {
    const short = u.url.split("?")[0].replace(/^https?:\/\//, "");
    const shown = short.length > 78 ? short.slice(0, 38) + "…" + short.slice(-37) : short;
    const badgeClass = u.status === "done" ? "done"
      : u.status === "failed" ? "failed"
      : u.status === "skipped" ? "skipped"
      : u.status === "paused" && u.waiting_on != null ? "skipped"                 // neutral grey
      : u.status === "oversize" || u.status === "paused" ? "pending_confirmation"  // amber, not red
      : "processing";
    // a PARKED download (its server went quiet for a few minutes) stepped
    // aside so the next file could use the slot; it rejoins within minutes
    const parked = u.status === "paused" && (u.park_count || 0) > 0;
    // a copy WAITING on an identical-looking download is the app saving a
    // download, not a stall: neutral grey, its own word (audit 8 P2-E)
    const waiting = u.status === "paused" && u.waiting_on != null;
    const badgeLabel = u.status === "oversize" ? "too big" : waiting ? "waiting"
      : parked ? "parked" : u.status;
    let statusCell = `<span class="badge ${esc(badgeClass)}">${esc(badgeLabel)}</span>`;
    if (u.status === "downloading" && u.bytes_total > 0) {
      statusCell += `<div class="progress"><div class="progress-fill" style="width:${Math.round(u.progress)}%"></div>
        <span class="progress-label">${(u.bytes_done / 1e6).toFixed(0)} / ${(u.bytes_total / 1e6).toFixed(0)} MB</span></div>`;
      // A payer CDN that goes silent mid-file leaves the bar frozen at the same
      // MB with no explanation, which reads as "the app has hung". It hasn't —
      // it's waiting out the no-data timeout and will reconnect and resume from
      // exactly here. Say so, so a normal (if annoying) CDN hiccup isn't
      // mistaken for a crash.
      const held = stalledFor(u.id, u.bytes_done);
      if (held >= STALL_HINT_SECONDS) {
        statusCell += `<div class="sub muted">no data for ${Math.round(held)}s —`
          + ` waiting on the payer's server, will reconnect and resume</div>`;
      }
    } else if (u.status === "queued" && u.big_lane) {
      // handed back at the headers: a big file waits for a big-file slot
      // while the small-file slots keep moving (audit 8 P1-D)
      statusCell += `<div class="sub muted">big file — waiting for a big-file download slot</div>`;
    } else if (u.status === "ingesting") {
      statusCell += ` <span class="muted">(see file row below for chunk progress)</span>`
        + ` <button class="btn" style="padding:1px 8px;font-size:11.5px" title="stop processing this file — the download is kept; retry processes it again without re-downloading" data-url-stop="${u.id}">stop</button>`;
    }
    if (u.status === "failed" || u.status === "skipped" || u.status === "oversize") {
      statusCell += ` <button class="btn" style="padding:1px 8px;font-size:11.5px" data-url-retry="${u.id}">retry</button>`;
    }
    if (u.status === "paused") {
      statusCell += ` <button class="btn" style="padding:1px 8px;font-size:11.5px" title="don't wait — try this download again right now" data-url-retry="${u.id}">try now</button>`;
    }
    // a finished index / page can be read again (it holds no rates): how a
    // capped list takes the files past the cap once max_toc_files is raised
    if (u.status === "done" && (u.kind === "toc" || u.kind === "page")) {
      statusCell += ` <button class="btn" style="padding:1px 8px;font-size:11.5px" title="read this list again and queue any files not taken yet (files already loaded are not downloaded again)" data-url-retry="${u.id}">retry</button>`;
    }
    // over the size ceiling? offer a one-click "download anyway"
    // (the disk-space guard still protects the drive)
    if (u.status === "oversize" ||
        ((u.status === "failed" || u.status === "skipped") && /confirm_over_gb|safety limit/i.test(u.error || ""))) {
      statusCell += ` <button class="btn" style="padding:1px 8px;font-size:11.5px" title="download and ingest this file even though it is over the size limit — the disk-space check still applies" data-url-force="${u.id}">download anyway</button>`;
    }
    if (u.status === "queued" || u.status === "failed" || u.status === "oversize" || u.status === "paused") {
      statusCell += ` <button class="btn" style="padding:1px 8px;font-size:11.5px" data-url-cancel="${u.id}">skip</button>`;
    }
    let notes = "";
    if (u.kind === "toc" && u.status === "done") notes = `found ${fmtInt(u.child_count)} files inside — queued below`;
    else if (u.kind === "page" && u.status === "done") notes = `found ${fmtInt(u.child_count)} file links on the page`;
    // a list cut at max_toc_files says so, in amber (round 7 P1.6)
    if (notes && u.error) notes += ` — <span class="warn-text">${esc(u.error)}</span>`;
    // a loaded index says when it was last checked and when it is next
    // (review 13 item 5.2.5: nothing said whether a file was current)
    if (notes && u.parent_id == null && u.finished_at) {
      const nx = new Date(); nx.setDate(1); nx.setMonth(nx.getMonth() + 1);
      notes += ` · <span class="muted">last checked ${esc(localTime(u.finished_at))}` +
        `${state.monthlyRecheck ? ` · next check ${esc(nx.toLocaleDateString())}` : " · monthly re-check off"}</span>`;
    }
    // Color the note by what it actually is: red is reserved for real
    // failures. Over-size is amber ("your OK needed"), and SKIPPED rows are
    // neutral — a duplicate of an already-ingested file, a file with none of
    // the target codes, or an allowed-amounts-only TOC is the app working as
    // designed, and on a big Blues aggregation duplicates can be MOST rows.
    // Painting those red made a healthy run read as "the vast majority of
    // files are showing errors".
    else if (u.error) notes = `<span class="${
      u.status === "oversize" || u.status === "paused" || u.status === "done" ? "warn-text" : u.status === "skipped" ? "muted" : "err-text"
    }">${esc(u.error)}${u.status === "paused" && u.retry_after
      ? (parked ? ` Back in line about ${esc(localTime(u.retry_after))}.`
                : ` Resumes about ${esc(localTime(u.retry_after))}.`) : ""}</span>`;
    return `<tr>
      <td title="${esc(u.url)}">${esc(shown)}</td>
      <td>${esc((!u.kind || u.kind === "unknown") && ["queued", "downloading", "fetched", "paused"].includes(u.status)
        ? "…"   // not read yet: "?" read as "unrecognised" (audit 8 P3)
        : KIND_LABEL[u.kind] || u.kind || "…")}</td>
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
  $$("[data-url-stop]", body).forEach((b) =>
    b.addEventListener("click", urlAction(b, `/api/urls/${b.dataset.urlStop}/stop`, "stop")));
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
      "'skipped' so you can retry them later. This lets ingesting catch up.\n\n" +
      "A file a downloader picked up in the instant before this click still " +
      "finishes and loads.")) return;
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
      "are kept, so a retry resumes them — except a file being fetched over several " +
      "parallel connections (download_segments above 1), which starts over. The " +
      "freed slots let ingesting catch up.")) return;
    msg.textContent = "stopping downloads…";
    try {
      const r = await postJson("/api/urls/stop-downloads", {});
      msg.textContent = `stopped ${r.stopped} download${r.stopped === 1 ? "" : "s"}`;
    } catch (e) { msg.textContent = "could not stop: " + e.message; }
    loadUrlQueue();
  });

  $("#url-resume").addEventListener("click", async () => {
    const msg = $("#url-msg");
    msg.textContent = "resuming…";
    try {
      const r = await postJson("/api/urls/resume-stopped", {});
      msg.textContent = `resumed ${r.resumed} link${r.resumed === 1 ? "" : "s"}`;
    } catch (e) { msg.textContent = "could not resume: " + e.message; }
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
    const renderKnown = () => {
      const want = ($("#known-states").value || "").toUpperCase().split(/[\s,]+/).filter(Boolean);
      const rows = (d.sources || []).filter((s) => !want.length ||
        (s.states || []).some((x) => x === "US" || want.includes(x)));
      $("#known-body").innerHTML = rows.map((s) => `
      <tr>
        <td><a href="${esc(s.url)}" target="_blank" rel="noopener">${esc(s.name)}</a></td>
        <td>${esc((s.states || []).join(" ") === "US" ? "national" : (s.states || []).join(" "))}</td>
        <td>${s.queueable ? "auto-queueable" : "portal (open in browser)"}</td>
        <td>${esc(s.verified || "")}</td>
        <td style="max-width:480px">${esc(s.notes || "")}</td>
      </tr>`).join("");
    };
    $("#known-states").oninput = renderKnown;
    renderKnown();
    wrap.style.display = "";
  });

  $("#known-queue-states").addEventListener("click", async () => {
    const msg = $("#url-msg");
    const states = ($("#known-states").value || "").toUpperCase().split(/[\s,]+/).filter(Boolean);
    if (!states.length) { msg.textContent = "type your state(s) first, e.g. MO KS"; return; }
    msg.textContent = `queueing tested sources for ${states.join(", ")}…`;
    try {
      const r = await postJson("/api/urls/known", { states });
      msg.textContent = `${r.added} indexes queued for ${states.join(", ")} + national payers` +
        (r.skipped ? `, ${r.skipped} already known` : "") +
        (r.portals ? ` (${r.portals} portal-only sources for these states need a browser)` : "");
    } catch (e) { msg.textContent = "could not queue: " + e.message; }
    loadUrlQueue();
  });

  $("#btn-scan").addEventListener("click", async () => {
    await fetch("/api/files/scan", { method: "POST" });
    setTimeout(loadFiles, 800);
  });
  const dz = $("#dropzone"), input = $("#file-input");
  dz.addEventListener("click", () => input.click());
  // reset after reading: picking the SAME file again fires no change event
  // while the input still holds it (review 9 item 4.10)
  input.addEventListener("change", () => { const fs = [...input.files]; input.value = ""; uploadFiles(fs); });
  ["dragenter", "dragover"].forEach((t) =>
    dz.addEventListener(t, (e) => { e.preventDefault(); dz.classList.add("drag"); }));
  ["dragleave", "drop"].forEach((t) =>
    dz.addEventListener(t, (e) => { e.preventDefault(); dz.classList.remove("drag"); }));
  dz.addEventListener("drop", (e) => uploadFiles([...e.dataTransfer.files]));

  initRemits();
  initRefData();
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
      <td>${esc(row.negotiated_type || "")}</td>
      <td class="num">${isDollarType(row.negotiated_type) ? "$" + fmtMoney(row.negotiated_rate) : esc(String(row.negotiated_rate))}</td>
      <td class="num">${row.delta_vs_expected != null ? fmtMoney(row.delta_vs_expected) : "–"}</td>
      <td class="sub">${esc(row.file_month || "")}</td><td class="sub">${esc(row.source_file)}</td></tr>`).join("");
  });
}

// a percentage / per-diem row is not dollars: no "$", no delta (P1.24)
function isDollarType(t) {
  const k = String(t || "").trim().toLowerCase();
  return !k || ["negotiated", "fee schedule", "derived"].includes(k);
}

async function chooseHeld(name, action) {
  try {
    const r = await fetch(`/api/files/${encodeURIComponent(name)}/choose`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action }) });
    if (!r.ok) alert(refusalTextOf(await r.text()) || `could not apply the choice (HTTP ${r.status})`);
  } catch (e) { alert("could not apply the choice: " + e.message); }
}

function refusalTextOf(raw) {
  try { const j = JSON.parse(raw); return j.detail || j.error || raw; } catch (_) { return raw; }
}

async function uploadFiles(files) {
  const notes = [];   // "already loaded" / "loaded under a new name" (audit 8 P1-B)
  for (const f of files) {
    if (f.size > 1 << 30) { alert(`${f.name} is over 1 GB — drop it into data/inbox/ instead.`); continue; }
    const fd = new FormData();
    fd.append("file", f);
    $("#dropzone").innerHTML = `<div class="loading">Uploading ${esc(f.name)}</div>`;
    try {
      const r = await fetch("/api/upload", { method: "POST", body: fd });
      const raw = await r.text();
      let body = {};
      try { body = JSON.parse(raw); } catch (_) { /* not JSON */ }
      if (!r.ok) alert(`upload failed: ${body.detail || body.error || raw || r.status}`);
      else if (body.status === "needs_choice") {
        const replace = confirm(`${body.replaces} is already loaded, and ${f.name} is a DIFFERENT file for the same payer and month.\n\nOK = Replace: use only this new file.\nCancel = Keep both: both files count toward every number.`);
        await chooseHeld(body.filename, replace ? "replace" : "keep");
        notes.push(replace ? `${body.replaces} is being replaced by this file.`
                           : `Both files are kept; both count toward every number. You can take either one out on the Files tab.`);
      } else if (body.status === "duplicate") notes.push(body.note || `${f.name} is already loaded.`);
      else if (body.note) notes.push(body.note);
    } catch (e) { alert(`upload failed: ${e.message}`); }
  }
  // refresh in place — a full page reload dumped the user back on the
  // Explorer tab right after they dropped a file on the Files tab
  const anyNew = !notes.length || notes.some((n) => !/already (loaded|waiting)/.test(n));
  $("#dropzone").innerHTML = `<div class="muted">${anyNew ? "Uploaded — processing begins shortly." : "Nothing new to load."}
    Drop more files here.</div>${notes.map((n) => `<div class="warn-text">${esc(n)}</div>`).join("")}`;
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
  // a source the tested-sources catalog (Files tab) has checked: say so,
  // with its date and whether Files can queue it (review 10 item 3.7)
  if (c.tested) {
    const t = c.tested;
    const how = t.queueable
      ? "Files tab → <b>Queue tested payer indexes</b> (or <b>Show tested sources</b> → your state) queues it."
      : "A portal or nested download — see the note for the manual steps.";
    return `<div class="source-card">
      <h4>${esc(c.name)} <span class="badge done">tested${t.verified ? " " + esc(t.verified) : ""}</span></h4>
      <div class="parent">${esc(c.parent || "")}</div>
      <a href="${esc(t.url)}" target="_blank" rel="noopener">${esc(t.url)}</a>
      <div class="notes">${how}${t.notes ? " " + esc(t.notes) : ""}</div>
    </div>`;
  }
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
  // review 10 1.4: the header counts load beside the table, not in front of it
  const statsP = api("/api/stats").catch(() => null);
  const stats = await api("/api/boot").catch(() => null);
  if (stats?.default_grain && stats.default_grain !== "tin") {
    state.grain = stats.default_grain;
    $$("#f-grain button").forEach((b) => b.classList.toggle("on", b.dataset.g === state.grain));
  }
  if (stats && stats.analytics && stats.analytics.state && stats.analytics.state !== "current") {
    state.analytics = stats.analytics;
    const el = $("#dataset-stats");
    if (el && /loading/.test(el.textContent)) { el.innerHTML = coverageHtml(stats.analytics); wireCoverageLink(); }
  }
  statsP.then((s) => loadStats(s || undefined));
  loadStateOptions();
  // one failed boot fetch must not brick the page: without this guard an
  // /api/payers hiccup at load left the Code and Files tabs never wired
  try { await initFilters(); }
  catch (e) { console.error("filter init failed — refresh to retry:", e); }
  try { initCptView(); } catch (e) { console.error(e); }
  try { initFilesView(); } catch (e) { console.error(e); }
  try { initAnalyticsPanel(); } catch (e) { console.error(e); }
  try { initPdfButtons(); } catch (e) { console.error(e); }
  try { initDeepLinks(); initFindBox(); } catch (e) { console.error(e); }
  refresh();
  // a bookmarked link opens its screen (and runs it) once the page is wired
  if (parseHash().view && parseHash().view !== "explorer") openFromHash().catch((e) => console.error(e));
  setInterval(() => { loadStats(); loadStateOptions(); }, 15000);
})();
