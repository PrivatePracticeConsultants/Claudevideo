# Design: a 0–100 % loading bar for every output feature of the MRF Explorer dashboard

Repository: `/home/user/Claudevideo`, branch `claude/gracious-mccarthy-ao7vmo`, head `c8b80fc` (read-only; nothing was edited).
Every statement about current behaviour cites `file:line` at that head. Scratch artefacts (the duckdb
experiments) are under `/tmp/claude-0/-home-user-Claudevideo/4633d594-0139-5f8e-a935-ae9c76b72000/scratchpad/audit13/progress/`
(`duckdb_progress_probe.py`, `probe2.py`, `probe3.py`, `probe_store.duckdb` — a synthetic 8M-row store, not a real one).

Invariants this design must keep (CLAUDE.md): **3** a cosmetic helper never fails real work — if the
progress channel breaks, the report must still arrive; **4** never fabricate — a percentage must mean
something and be labelled for what it is; **7** determinism — progress is observation only and must never
change a query, its order or its result. Round-10 rules (docs/AI_HANDOFF.md:538-547): a superseded search
is interrupted via `X-Mrfx-View` + `con.interrupt()`, never queued; `/api/summary` waits ≤ 8 s on its
single-flight lock when the older holder was interrupted.

---

## 0. The one-paragraph answer

Keep every output on the request it uses today (JSON in place, HTML blob into a tab, file streamed into a
hidden frame) and add a **progress side-channel**: the page mints a token per run, sends it as
`X-Mrfx-Progress: <token>` (or `?progress=<token>` on the iframe GETs), and polls an `async`
`GET /api/progress/{token}` every 400 ms while its fetch is pending; `DELETE /api/progress/{token}` stops a
run. On the server a `Progress` object lives in a **contextvar** for the duration of the route; modules
reach it with `progress.current()` (a no-op when nothing set it, so the CLI and the 1,194 tests are
untouched), declare weighted stages in the user's words, and **DuckDB's `query_progress()`** — which the
experiment below shows is a free, monotonic 0–100 float readable from a side thread on duckdb 1.5.5 —
moves the bar *inside* each stage. `Store.connect()` registers each connection it opens with the current
`Progress`, which is how one stop request reaches the connection actually running the statement and how a
cancelled run refuses to open the next one. The two outputs that already run detached (monthly packets,
tracker import) keep their daemon threads and polls and gain the same `{pct, stage}` fields from the same
object. 100 % is set by the browser only when the result is in hand; the server never reports more than 99.

---

## 1. Inventory — every output the dashboard can trigger

Legend. **Delivery**: *JSON* = rendered in place by the view; *HTML tab* = `openReportWith()` (app.js:1938-1967:
`window.open("about:blank")` synchronously inside the click, `fetch` POST, `r.blob()` → object URL into the tab);
*frame* = `downloadInFrame()` (app.js:1920-1936: hidden `<iframe src=GET url>`, the browser streams to disk;
a refusal loads the frame as JSON and is alerted); *POST-blob* = `postDownload()` (app.js:3909-3923) or an
inline twin of it (`fetch` POST → `r.blob()` → `<a download>.click()`). **Busy today**: `spinner` =
`<div class="loading">…</div>` (styles.css:255-264, a CSS spinner after the text); `msg` = a `<span class="muted">`
word such as "pricing…"; `btn` = the button is disabled; `—` = nothing. **Abort/supersede today**: `ticket(view)`
(app.js:104-113) only stops an *older* answer painting over a newer one; `supersede(view)` (app.js:118-122) adds an
`AbortController` and the `X-Mrfx-View` header the server's `_view_query` registry (api.py:1594-1627) uses to
`con.interrupt()` the older request of the same view, which answers 409.

| # | Feature (tab) | Control | JS function | Route(s), in order | Delivery | Busy today | Abort/supersede today |
|---|---|---|---|---|---|---|---|
| 1 | Explorer table | any filter / sort / page (`#explorer-filters`) | `loadRates` app.js:835-886 | `GET /api/rates` (api.py:1754-1810) | JSON | spinner in `#rates-state` (:838) + old rows dimmed `.stale` (:841, styles.css:470); pager | `ticket("rates")` + `supersede("rates")` (:843-845) |
| 2 | Explorer summary strip + by-code | same | `loadSummary` :929-984 | `GET /api/summary` (api.py:1871-1991) | JSON | — (strip text replaced on arrival); "Stats are busy — retrying…" on 503, one retry after 1.4 s (:962-975) | `ticket("summary")` + `supersede("summary")` (:931-934); server waits ≤ 8 s on `_summary_lock` (api.py:1913) |
| 3 | Explorer: Export CSV | `#btn-export` | handler :1131-1133 | `GET /api/export.csv` (api.py:2815-2826) | frame | — | none (an iframe GET cannot be aborted from JS) |
| 4 | Explorer: Export + methodology | `#btn-export-zip` | :1134-1136 | `GET /api/export.zip` (api.py:2828-2858) | frame | — | none |
| 5 | Explorer: Outreach zip | `#btn-outreach` | :1137-1140 | `GET /api/export/outreach.zip` (api.py:3839-3854) | frame | — | none |
| 6 | Row drawer | click a row | `openEntity` :1167-1264 | `GET /api/entity/{grain}/{id}` (api.py:2015) then `POST /api/org-website` on demand (:1253) | JSON | drawer "loading" | `ticket("drawer")` (:1175) |
| 7 | Code page | chip / state box / base-only / class (`#cpt-chips` …) | `selectCpt` :1418-1493 | `GET /api/trend` (api.py:2269-2303) and `GET /api/code/{code}` (api.py:2157-2267) in parallel (:1457-1460) | JSON | spinners in `#cpt-state` and `#cpt-payer-rank` (:1440-1441) | own `AbortController` `cptCtl` (:1422-1424), no `X-Mrfx-View` → the server finishes the old query for nobody |
| 8 | Code page export | `#cpt-export` | :1399-1410 | `GET /api/export.zip?cpt=…` | frame | — | none |
| 9 | Overview | `#ov-refresh`, state box | `loadOverview` :327-398 then `renderDataQuality` :400-463 | `GET /api/overview` (api.py:4323-4353); then `GET /api/quality/freshness`, `POST /api/quality/types`, `POST /api/quality/posture` in parallel (:407-409) | JSON | spinner + a second sentence after 4 s ("can take up to a minute") (:333-336) | `ticket("overview")`, `ticket("quality")`; server: non-blocking `_overview_lock` → 503 "overview is building" (api.py:4342) |
| 10 | Markets (one code) | `#mk-run` / Enter | `loadMarkets` :499-531, then `renderMkLocal` :533, `renderMkRadius` :568, `renderMkConcentration` :599 | `POST /api/market/geography`, `/negotiability`, `/medicare`, `/differential` in parallel (:510-513); then `POST /api/territory/local`, `/territory/radius`, `/territory/concentration` (:528-530) — 7 routes | JSON | spinner (:506); the three follow-ups render into empty divs with no cue | `ticket("markets")` only on the first four |
| 11 | Roster | `#ro-run` | `loadRoster` :746-759 | `POST /api/roster` (api.py:4277-4286) | JSON | spinner (:752), `#ro-csv` disabled (:749) | none |
| 12 | Roster CSV | `#ro-csv` | :730 → `postDownload` | `POST /api/roster.csv` (api.py:4288-4300) — recomputes the roster | POST-blob | — | none |
| 13 | Benchmark | `#b-run` | `runBenchmark` :1746-1772 → `renderBenchmark` → `renderBenchmarkQuality` :1858-1891 | `POST /api/benchmark/opportunity` (with volumes) or `/api/benchmark/market` (:1761-1762); then `POST /api/quality/size` + `/api/quality/subject-types` in parallel (:1862-1865) | JSON | spinner "Computing benchmark" (:1756); `#b-report`/`#b-negotiation` disabled (:1751-1752) | `ticket("benchmark")` (:1758); nothing on the quality pair |
| 14 | Contract gaps | `#b-gaps-run` | `runContractGaps` :1650-1674 | `POST /api/contract-gaps` (api.py:4216-4225) | JSON | spinner (:1655) | none |
| 15 | Pitch report | `#b-report` | `openPitchReport` :1969-1971 | `POST /api/report/pitch` (api.py:3936-3949) — recomputes the benchmark | HTML tab | — (the new tab is blank until the blob lands) | none |
| 16 | Payer-negotiation one-pager | `#b-negotiation` | `openNegotiationReport` :1973-1975 | `POST /api/report/negotiation` (api.py:3951-3965) | HTML tab | — | none |
| 17 | Negotiate: compare | `#ng-run` | `runNegotiate` :2132-2185 | `POST /api/negotiate/compare` (api.py:3967-3976) | JSON | `<h3>Comparing…</h3>` (:2137), `#ng-report` disabled (:2139) | `ticket("negotiate")` (:2141) |
| 18 | Negotiate: printable report | `#ng-report` | :2052-2061 | `POST /api/report/payer-compare` (api.py:3978-3989) | HTML tab | — | none |
| 19 | Negotiate: proposal preview | `#ng-proposal-run` | `runProposal` :2079-2111 | `POST /api/negotiate/proposal` (api.py:3991-4001) | JSON | spinner (:2085) | none |
| 20 | Negotiate: printable proposal | `#ng-proposal-report` | :2042-2051 | `POST /api/report/proposal` (api.py:4003-4014) | HTML tab | — | none |
| 21 | Negotiate: leverage (this payer / every payer) | `#lv-run`, `#lv-all` | `initLeverage` :3071-3123 | `POST /api/leverage` (api.py:3350-3363) / `POST /api/leverage/summary` (:3365) | JSON | msg "measuring…" / "ranking payers…" (:3081, :3103) | none |
| 22 | Negotiate: value a visit | `#ec-run` | `initVisitEconomics` :3125-3159 | `POST /api/economics/visit` (api.py:3318) | JSON | msg "pricing…" (:3130) | none |
| 23 | Negotiate: floors compare | `#fl-run` (also after an import) | `runFloorCompare` :3021-3069 | `POST /api/floors/compare` (api.py:3202) | JSON | spinner (:3034) | none |
| 24 | Negotiate: hospital parity | `#hp-run` (also after an import) | `runHospitalParity` :3308-3345 | `POST /api/hospital/parity` (api.py:3534) | JSON | spinner (:3310) | none |
| 25 | Rate card | `#rc-run` | `buildRatecard` :2412-2444 → `priceStandardVisit` :2347-2380 | `POST /api/schedule/fee` (api.py:4016-4024); then `POST /api/schedule/visit` (:4032-4042) | JSON | spinner "Building rate card" (:2420); three buttons disabled (:2415-2417); `#sv-msg` "pricing…" (:2353) | `ticket("ratecard")`, `ticket("visit")` |
| 26 | Printable rate card | `#rc-report` | :2208-2209 | `POST /api/report/ratecard` (api.py:4093-4103) — recomputes the fee schedule + visit block | HTML tab | — | none |
| 27 | Rate card CSV | `#rc-csv` | `downloadRatecardCsv` :2534-2548 | `POST /api/schedule/fee.csv` (api.py:4105-4122) — recomputes | POST-blob | — | none |
| 28 | Tracker bundle | `#rc-bundle` | `downloadTrackerBundle` :2503-2532 | `POST /api/report/org-bundle.zip` (api.py:2860-2885) — recomputes the fee schedule | POST-blob | **btn text "Building…" + disabled** (:2507) — the only output with a per-button state | none |
| 29 | Visit value in this market | `#vv-run` | `showVisitMarket` :2244-2266 | `POST /api/schedule/visit/market` (api.py:4063-4072) | JSON | msg "pricing the market…" (:2252), two buttons disabled | `ticket("visit-market")` |
| 30 | Visit value printable / CSV | `#vv-report` / `#vv-csv` | :2214-2215 / `downloadVisitMarketCsv` :2308-2322 | `POST /api/report/visitvalue` (:4084) / `POST /api/schedule/visit/market.csv` (:4074) — recompute | HTML tab / POST-blob | — | none |
| 31 | Clients digest | `#cl-refresh`, tab open | `loadClientDigest` :2596-2680 | `GET /api/clients` then `GET /api/clients/digest` (api.py:3677-3683) | JSON | spinner "Building the digest for N clients" (:2607) | `ticket("digest")` |
| 32 | **Monthly packets** (detached) | `#pk-build` | `initPackets` :2684-2692, `pollPackets` :2694-2727 | `POST /api/packets/build` (api.py:3658-3675) starts a daemon thread; `GET /api/packets` (:3653-3656) polled every **1.5 s** (:2701) | files on disk; JSON result | `#pk-msg` "starting…" then spinner with the server's `message` ("building packet 2 of 5: …", packets.py:318-333) | none (409 if already running) |
| 33 | Engagement compare | `#en-compare` | `runEngagementCompare` :2762-2792 | `POST /api/engagements/compare` (api.py:3619) | JSON | msg "comparing…" (:2766) | none |
| 34 | Renewals | `#rn-run` | `initRenewals` :2794-2835 | `GET /api/clients` (opt.) then `POST /api/contracts/renewals` (api.py:3006) | JSON | msg "checking…" | none |
| 35 | New to network | `#nn-run` | `initNewToNetwork` :2837-2880 | `POST /api/contracts/new-to-network` (api.py:3026) | JSON | msg "looking…" | none |
| 36 | New clinics / Closures | `#nc-run` / `#cz-run` | :2956-2993 / :3347-3384 | `POST /api/newclinics` / `POST /api/closures` | JSON | msg "looking…" | none |
| 37 | Steal-share | `#ss-run` | `initStealShare` :3240-3272 | `POST /api/territory/steal-share` (api.py:3421) | JSON | msg "looking…" | none |
| 38 | **Prospect dossier** | `#ds-run` | `initDossier` :3161-3187 | `POST /api/report/dossier` (HTML, api.py:3230-3243) **and, concurrently,** `POST /api/report/dossier.json` (:3245-3258) — `build_prospect_dossier` runs **twice** for one click (:3174-3177) | HTML tab + JSON | msg "assembling…" (:3165) | none |
| 39 | **Market report** | `#mr-run` | :3188-3198 | `POST /api/report/market` (api.py:3260-3277) | HTML tab | msg "building…" | none |
| 40 | Growth / service lines | `#gr-run` / `#gr-lines` | `initGrowth` :3200-3238 | `POST /api/utilization/growth` / `/service-lines` | JSON | msg "looking…" | none |
| 41 | Market sizing | `#ms-run` | :3386-3417 | `POST /api/market/sizing` | JSON | msg "sizing…" | none |
| 42 | Underpayment check / CSV | `#rm-run` / `#rm-csv` | `runRemitCheck` :3440-3495 / :3427-3437 | `POST /api/remits/check` / `POST /api/remits/check.csv` (recomputes) | JSON / POST-blob | msg "checking…"; `#rm-csv` disabled | none |
| 43 | Medicare: practice lookup | `#md-run` / Enter | `runMedicareOrg` :3721-3742 | `POST /api/medicare/org` (api.py:3738-3804) | JSON | spinner (:3728); `#md-csv` disabled | none |
| 44 | Medicare: referral leaders | `#rl-run` | `runReferralLeaders` :3516-3570 | `POST /api/medicare/leaders` (api.py:3685) | JSON | msg "ranking…" | none |
| 45 | Medicare: eligibility batch | `#md-check` | `runMedicareBatchCheck` :3820-3847 | `POST /api/medicare/eligibility` | JSON | msg "checking…" | none |
| 46 | **Tracker import** (detached) | `#md-tracker-import` | `refreshTrackerCard` :3586-3662 | `POST /api/medicare/tracker/import` (api.py:2945-2986) → daemon thread; `GET /api/medicare/tracker` polled every **2 s** (:3601) | store write | btn "Starting…"; spinner with `job.message` ("importing X (i of n)…", api.py:2896-2899) | none (409 if running) |
| 47 | Leads / Leads CSV | `#ld-run` / `#ld-csv` | `runLeads` :4031-4048 / :3952 → `postDownload` | `POST /api/leads` (api.py:4124-4137) / `POST /api/leads.csv` (:4139-4155, recomputes at the 1,000 cap) | JSON / POST-blob | spinner "Finding leads" (:4037); `#ld-csv` disabled | `ticket("leads")` |
| 48 | Leaderboard | `#ld-board-run` | `runLeaderboard` :3955-3995 | `POST /api/leaderboard` (api.py:4227-4236) | JSON | spinner | `ticket("leaderboard")` |
| 49 | Rate changes / CSV | `#ch-run` / `#ch-csv` | `runChanges` :4175-4208 / :4101 | `POST /api/changes` (api.py:4157-4168) / `POST /api/changes.csv` (:4170-4185, recomputes) | JSON / POST-blob | spinner "Comparing months" (:4197); `#ch-csv` disabled | `ticket("changes")` |
| 50 | Payer trajectory | `#ch-traj-run` | `runTrajectory` :4105-4160 | `POST /api/trajectory` (api.py:4238-4248) | JSON | spinner with a "slowest view on a big store" sentence when national (:4113) | none |
| 51 | Fill volumes from Medicare | `#b-vol-medicare` etc. | `fillVolumesFromMedicare` :2929-2954 | `POST /api/utilization/volumes` | JSON into a textarea | msg "reading Medicare claims…" | none |

Not outputs (excluded on purpose): the Files tab's ingest/queue polls (`loadFiles`/`loadUrlQueue`, every 4 s,
app.js:162-165 — note **4 s**, not 2 s), uploads, imports (`/api/*/import`, `/api/mpfs/upload`), the subject
typeahead, and `/api/boot` / `/api/stats` (the header counts). The Files tab already has a real progress bar
for parsing and downloading (app.js:4367-4371, 4508-4509, CSS `.progress` at styles.css:422-427) — the new
component reuses that CSS so the two bars look the same.

Observations from the inventory that the implementation should fix on the way:

- **Three ways to say "busy"** (spinner / muted word / nothing) and one button with its own text swap
  (#28). The HTML-tab reports and the frame downloads have **no feedback at all** between click and arrival;
  on a 99M-row store a pitch report or an export is tens of seconds of a blank new tab or nothing.
- **#38 builds the dossier twice** concurrently for one click (app.js:3174-3177). With a bar this would show two
  runs; the fix is to have the HTML route carry the omissions (e.g. `X-Mrfx-Skipped` header or a JSON
  `{html, skipped}` the client turns into the blob) and drop the second call.
- **Every "printable"/CSV twin recomputes** the on-screen result (#12, #15, #26, #27, #28, #30, #42, #47, #49):
  the bar will honestly show the same stages again. A result cache keyed by the payload is out of scope here
  but the stage plan makes the duplication visible.

---

## 2. Server-side phases per output route

### 2.1 How a request runs today (what the bar can and cannot see)

- All output routes are **sync `def`** and run in Starlette's threadpool: anyio's default **40** tokens,
  widened to **100** at startup (`_widen_threadpool`, api.py:4550-4560). The only `async def` endpoint is
  `/api/debug/stacks` (api.py:4497-4511), deliberately, so it answers even when the pool is jammed.
- Long detached work never uses `BackgroundTasks` (which would hold a pool token); it runs on daemon threads
  through `_bg_safe` (api.py:1401-1409): packets (`_run_packets`, api.py:3635-3651), the tracker import
  (api.py:2892-2924), inbox scans.
- **Every `store.connect()` is a fresh `duckdb.connect()`** (store.py:2106-2160) — a context manager the
  modules open themselves (`with store.connect() as con:`), one or several per feature. Only `/api/rates` and
  `/api/summary` open the connection in the route and hand it to `_view_query` (api.py:1810, 1909-1910); every
  other feature's statements run on connections the route never sees. **So today `con.interrupt()` can reach
  only those two routes.** Any cancel/progress design must hook `Store.connect()`, not the route.
- The heavy lifting is almost always **one large statement** per stage (a CTE pipeline ending in a GROUP BY /
  quantile / window / TOP-N). A stage that is one statement is indivisible from Python — unless DuckDB's own
  progress is read (next section).
- Per-generation caches short-circuit whole features: `Store.cached` / `@generation_cached`
  (store.py:4930-4956, 5972-5990) for leaderboard, quality panels, payer freshness; `_summary_cache` and the
  on-disk strip (api.py:1819-1864); `_overview_cache` (api.py:1823); the digest (`store._digest_cache`,
  clients.py:83-109); the materialised per-practice medians (`Store.spine_materialised`, store.py:4964-5012,
  used by `practice_base_sql`, benchmark.py:602-660). **A cached answer is 0 → 100 in one step** — the bar
  must be allowed to jump straight to done and must not pretend otherwise. Conversely the **first** call of a
  benchmark-family feature per spine generation pays an extra hidden stage: `spine_materialised` runs a
  `COPY (…) TO parquet` over the whole spine (store.py:4993-4996; "8–13 s first call" on 99M per the comment
  at :4985). That stage must be named ("Preparing the practice medians (first run after new data)").

### 2.2 The DuckDB progress experiment (duckdb 1.5.5, `.venv`, synthetic 8M-row store)

Scripts: `duckdb_progress_probe.py`, `probe2.py`, `probe3.py` in the scratch folder. Findings:

| Question | Result |
|---|---|
| API | `con.query_progress()` → **a bare `float`** (the docstring in 1.5.5: "Query progress of pending operation"); not an object. `-1.0` when the progress bar is disabled or no statement is running; `0.0–100.0` while one runs. |
| How to enable | Per connection: `SET enable_progress_bar = true; SET enable_progress_bar_print = false; SET progress_bar_time = 0`. **Session-local** — another connection on the same database still reads `false` (probe2). Nothing is printed to stderr with `print=false`. |
| From which thread | Readable from **another thread on the same connection object** while `execute()` blocks in the route thread. A `con.cursor()` (a duplicate connection) sees `-1` — progress is per connection, so the sampler must hold the very object that runs the statement. |
| Shape | Monotonic within a statement (191 samples at 10 ms, **0 decreases**); **resets to 0 at each new statement**; ~20 distinct values over a 2 s GROUP-BY/quantile query sampled at 50 ms (7 → 17 → 25 → 38 → 49 → 59 → … → 99.8 → 100). |
| Which statements move | GROUP BY + quantile_cont: smooth 0→100. Window (`percent_rank`): to 99. `COPY … TO parquet`: 0→100. `COPY (… ORDER BY …) TO csv` (the export path): 0→100 with plateaus (…50 → 62 → 76 → 100). A full `ORDER BY` has one ~280 ms plateau in 615 ms. `count(*)` on 8M rows: 41 ms, 3 samples. `ORDER BY … LIMIT 100`: 40 ms, one sample at 52 % — too fast to matter. |
| Cost | Within noise: the 2 s query ran 1,876–2,003 ms off vs 1,917–1,980 ms on; 300 short point queries 1,429–1,609 ms either way. Sampling at 10 ms from a side thread did not slow the query measurably. |
| Safety | 149 open/run/close cycles with a sampler hammering `query_progress()` at 1 ms: **no crash**; 4,354 good reads, 1,840 clean `ConnectionException: Connection already closed!` once the handle was closed. `interrupt()` on a closed connection raises the same clean exception. |
| Interrupt latency | `con.interrupt()` from another thread → `duckdb.InterruptException` in the route thread **~97 ms** later; the connection is reusable afterwards (`SELECT 1` works). |
| Subclassing | `class Stopped(duckdb.InterruptException)` works and is an `Exception` subclass. |

Conclusion: DuckDB progress is usable, free, and the right tool for intra-stage movement. It is **not** a
whole-feature percentage — a feature is 2–12 statements plus Python — hence the stage weights below.

### 2.3 Stages per route (expensive ones marked ★; "1 stmt" = one indivisible DuckDB statement, movable only by `query_progress()`)

Common sub-steps referenced below: **resolve** = `subject_tins_for_market` (benchmark.py:673-712; a small
`tin_directory` query at :698) after `resolve_subject_tins` (:715-805; up to 5 small queries); **base** =
`practice_base_sql` (benchmark.py:602-660) — on the house basis a `read_parquet` of the materialised medians
(cheap) but **the first call per spine generation materialises them** (★ hidden stage, store.py:4993-4996);
**anchor** = `store.medicare_anchor` (cheap); **stale** = `_stale_payers_for` (benchmark.py:1024, generation-cached).

| Route | Stages in order (module:line) | Dominant |
|---|---|---|
| `GET /api/rates` (api.py:1754-1810) | (1) page query — 1 stmt (:1803); (2) `_filtered_count` — 1 stmt, cached 30 s per filter (:1805, 1650-1720) | ★ both on an unfiltered entity/NPI grain; the count once per filter |
| `GET /api/summary` (:1871-1991) | (0) *wait* for `_summary_lock` ≤ 8 s (:1913); (1) strip quantiles — 1 stmt (:1930-1954); (2) by-code — 1 stmt (:1967-1982) | ★ (1) and (2) equal-ish; whole thing cached per generation / 30 s |
| `GET /api/code/{code}` (:2157-2267) | (1) ranked TOP-500 — 1 stmt (:2180-2204); (2) histogram — 1 stmt (:2211-2224); (3) payer rank — 1 stmt (:2234-2255) | ★ (1) (joins names) |
| `GET /api/trend` (:2269-2303) | 1 stmt (:2283-2300) | ★ |
| `GET /api/overview` (:4323-4353 → overview.py:35-135) | (0) *lock* (non-blocking → 503); (1) temp table of the market's rows — 1 stmt (overview.py:49-52); (2) counts (:54); (3) by-discipline (:65); (4) top codes (:77); (5) payer index (:98) | ★ (1) and (5); cached per (state, generation) |
| quality trio after Overview | `payer_freshness` (quality.py:206, 2 stmts, cached), `rate_type_mix` (:87, 2 stmts), `payer_posture` (:557, 1 stmt) | ★ posture |
| `POST /api/market/*` ×4 (api.py:4187-4214 → market.py) | geography 2 stmts (:104-110); negotiability 1 (:155); medicare 2 (:206-207); differential 1 (:305) | ★ geography |
| territory follow-ups | local 3 stmts (territory.py:139-156); radius 2 (:507-533); concentration 2 (:224-231) | ★ local |
| `POST /api/roster` (:4277 → leads.py:431-547) | resolve payer; base; 1 stmt (leads.py:492) | ★ the stmt |
| `POST /api/benchmark/market` / `opportunity` (:3914-3934 → benchmark.py:885-1022) | (1) resolve (+ plan scope, peer set); (2) base (★ hidden materialise on first call); (3) **the benchmark statement** — 1 stmt (:969-972: base CTE → subject → peers → market_stats → position); (4) anchor + per-row Python; (5) stale (cached). `opportunity` adds pure Python (`compute_opportunity`, :1040) | ★ (3) |
| Benchmark quality pair | `size_premium` (quality.py:275-487: base; 1 big stmt :336; subject stmt :380; presence :413; **another full `compute_benchmark`** :447) — note the comment at NEXT_STEPS_PROMPT_10.md:276 ("/api/quality/size 9.9 s on the first call"); `code_type_flag` (:489, 1 stmt) | ★ size_premium (two heavy stmts + a benchmark) |
| `POST /api/report/pitch` (:3936-3949) | (1-5) as benchmark; (6) `render_pitch_report` (benchmark.py:2164-2303): `require_subject_content` → `_subject_has_scoped_rates` 1 stmt (:1859-1878); `methodology_footer` files query (:2042-2051); `series_by_code` sparklines (spark.py, :2192-2196); **`_erosion_block` → `compute_payer_trajectory`** (:2091-2103, the single heaviest statement in the app when national) | ★ benchmark stmt, trajectory |
| `POST /api/report/negotiation` (:3951-3965 → `compute_payer_negotiation` benchmark.py:1140-1234) | (1) resolve; (2) `subject_payers` 1 stmt (:1109-1138); (3) **per payer: a full `compute_benchmark`** (:1162) — N payers × 1 heavy stmt; (4) render (same extras as pitch) | ★ the N-payer loop — a natural `tick(i/N)` |
| `POST /api/negotiate/compare` / `report/payer-compare` (:3967-3989 → benchmark.py:1256-1412) | (1) `compute_benchmark` for the one payer (:1268); (2) per comparable: resolve + `_entity_rates` 1 stmt (:1278-1279, :1236-1253); (3) cross-payer "others" 1 stmt (:1300); render | ★ (1), then (3) |
| `POST /api/negotiate/proposal` / `report/proposal` (:3991-4014 → :1662-1758) | `compute_payer_comparison` (all of the above) + anchor + Python | ★ as above |
| `POST /api/schedule/fee` (:4016-4024 → schedule.py:59-212) | (1) resolve; (2) subject rates — 1 stmt (:112-113); (3) peers per cell — base + 1 stmt (:141-159); (4) scorecard (pure, :214) | ★ (3) ("95 % of the call: 4.13 of 4.34 s on 99M", docstring :66-69) |
| `POST /api/schedule/visit` (:4032 → visit.py `price_visit`) | `compute_fee_schedule(peers=False)` (stages 1-2) + Python | ★ (2) |
| `POST /api/report/ratecard` / `fee.csv` (:4093-4122) | fee schedule (1-4); `_visit_market_or_refusal` → `market_visit_value` (visit_market.py:141-283: base per-payer 1 stmt :165; subject check :187; `price_visit` :192); `render_rate_card` (schedule.py:380: sparklines :467, `_methodology` files query :304-313) | ★ fee (3), visit market stmt |
| `POST /api/report/org-bundle.zip` (:2860-2885 → orgprofile.py:43-98) | `compute_fee_schedule` (1-4); 4 small stmts (:57-83); `org_bundle_files` (pure); zip | ★ fee (3) |
| `POST /api/schedule/visit/market` / `.csv` / `report/visitvalue` (:4063-4091) | base per-payer; 1 stmt (:165) + Python pairing; optional subject (2 small + `price_visit`) | ★ the stmt |
| `POST /api/leads` / `leads.csv` (:4124-4155 → leads.py:139-337) | (1) resolve exclude (opt.); (2) base; (3) `_lead_base` temp table — 1 stmt (:207); (4) `_lead_om` others-median — 1 stmt (:208); (5) ranking — 1 stmt (:211); (6) worst payer for the returned TINs — 1 stmt (:260); (7) dropped-from-newest — 1 stmt (:275); (8) websites + Python | ★ (3)(4)(5); (6) scans the live relation for ≤ 1,000 TINs |
| `POST /api/leaderboard` (:4227 → leads.py:339-429) | generation-cached; base; 1 stmt (:392) | ★ the stmt (then 0 → 100 from cache) |
| `POST /api/contract-gaps` (:4216 → benchmark.py:1414-1540) | resolve; base; market stats — 1 stmt (:1521); subject's priced codes — 1 stmt (:1529) | ★ market stats |
| `POST /api/changes` / `changes.csv` (:4157-4185 → monitor.py:80-377) | (1) `available_months` / `payer_months` (small); (2) resolve subject (opt.); (3) **the diff** — 1 stmt (:306-307: two month sides, medians, join, cap) | ★ (3) |
| `POST /api/trajectory` (:4238 → monitor.py:379-600) | (1) months; (2) **the matched-panel statement** — 1 stmt (:501-502, `MATERIALIZED` base over every month); (3) inflation index + Python | ★ (2) — "20 s national on 99M" (app.js:4109 comment) |
| `POST /api/report/dossier` (:3230 → dossier.py:101-358) | (0) resolve + existence check (:110-124); sections, each fault-isolated (:155-165): identity 1 stmt (:167), size → `size_premium` (:190), rates → **`compute_benchmark`** (:211-214), weakest → **`compute_payer_negotiation`** (N benchmarks, :253-256), leverage → `network_leverage` (5 stmts, leverage.py:94-152), referrals 1 stmt + Medicare (:295), market 1 stmt (:319); render | ★ weakest (N payers), then size, rates — a natural 7-stage plan |
| `POST /api/report/market` (:3260 → dossier.py:360-500) | sections: cities → `local_rate_map` (3 stmts), concentration (2), posture → `payer_posture` (1), openings → NPPES (2), demand → demographics; files/newest 2 stmts (:489-495); render | ★ cities, posture |
| `GET /api/clients/digest` (:3677 → clients.py:83-207) | cached on `_digest_cache`; else: (1) `payer_months`; (2) **`contract_gaps_book`** for the whole book (2 stmts); (3) **per client**: 2 small stmts + `compute_rate_changes` (1 heavy stmt) + `org_referrals`/`recent_losses` | ★ (2) and the per-client loop ("23 s for 5 clients on 99M", docstring :90) |
| `POST /api/packets/build` (:3658 → packets.py:287-363) — detached | (1) `subject_vintage` per client (small); (2) `contract_gaps_book` (:319); (3) `client_digest` (:329); (4) **per client `build_client_packet`** (:331-335 → :81-285): rate card (fee schedule + render), pitch (benchmark + render incl. trajectory), changes, gaps, wins, closed sources — each fault-isolated via `attempt()` (:128-135); (5) book index. **A `progress(msg)` callback already exists** (:290, 318-333) and the CLI prints it (cli.py:1162) | ★ (4): per client ≈ one rate card + one pitch report |
| `GET /api/export.csv` / `.zip` (:2815-2858 → `_export_payload` :2749-2796) | (1) FilterSet + narrowing guard; (2) disk-space estimate; (3) exact `count(*)` — 1 stmt, **only when the raw-row bound would not fit** (:2778-2780, "7 s for a whole 99M store"); (4) **`COPY (…) TO csv`** — 1 stmt (:2782); (5) methodology text (:2789); zip only: (6) stream the CSV into the zip member (:2845-2848), write sidecar; (7) `FileResponse` — the browser's own download follows | ★ (4) (experiment: `query_progress` moves through a sorted COPY) |
| `GET /api/export/outreach.zip` (:3839 → outreach.py:119-268) | (1) units — 1 stmt (:133); (2) `_geo_by_tin` + `_tin_meta` (2 stmts, :151-152); (3) base; units temp table (:174); (4) per-code rates temp table — 1 stmt (:178); (5) coverage + rates (:183, :194); (6) CSV + methodology; zip | ★ (1) and (4) |
| `POST /api/medicare/org` (:3738-3804) | resolve (:3757); NPIs 1 stmt (:3761); `org_referrals` in + out (2 stmts each, medicare.py:606-640) each followed by `npi_eligibility` + `recent_losses`; eligibility; status | ★ referrals in/out |
| `POST /api/medicare/leaders` (:3685 → medicare.py:722-860) | centroid; ranked 1 stmt (:822/:831); TIN map; totals 1 stmt (:851) | ★ ranked |
| `POST /api/remits/check` (:3569 → remits.py:145-255) | parse (pure); resolve; 1 stmt (:173) | ★ the stmt |
| leverage / economics / floors / hospital / engagements / renewals / new-to-network / newclinics / closures / sizing / steal-share / growth | 1–5 small-to-medium statements each; see the grep map (leverage.py:94-152, economics.py:96-129 → `compute_fee_schedule(peers=False)`, contracts.py:125-126 / 183-302, territory.py:329-413) | none dominant on a therapy store; **one indeterminate stage with `query_progress()` is enough** |

Where a stage is a single statement the engineer must **accept it as indivisible** and let DuckDB's
percentage move the bar inside it — splitting these CTE pipelines into separate statements to get finer
stages would change plans and risk the determinism tests; do not. The exceptions where splitting is *already*
the shape of the code and `tick()` is natural: the per-payer loop in `compute_payer_negotiation`, the
per-comparable loop in `compute_payer_comparison`, the per-client loops in the digest and the packets, the
per-section loops in the dossier and market report, and the N statements of leads / overview / outreach.

The other reviewer's millisecond table slots in as the weight column of §4 (one number per row of the tables
above, measured on the 99M store, cold and warm separately — the warm number is the one to use for weights,
with the cold-only "materialise" stage kept as its own row).

---

## 3. Architecture options and the recommendation

### 3.1 The seven situations every option must handle

| Situation | What must hold |
|---|---|
| S1 button pressed twice | at most one run per view (or one per report tab); the older is interrupted, not queued (Round 10 rule) |
| S2 view switched mid-run | the run continues (the user expects the report), the bar is simply out of sight and correct on return; the existing supersede must keep working untouched |
| S3 server restart mid-run | the page says so plainly; nothing hangs; no half result is shown |
| S4 two browser tabs | tab A's runs never move or cancel tab B's bars (`VIEW_ID` is per page, app.js:117) |
| S5 the 8 s summary lock (api.py:1913) and the other single-flight locks | a wait is shown as a wait ("waiting for an earlier summary"), never as a stuck percentage or a fake crawl |
| S6 invariant 3 | if anything in the progress path raises, the result still arrives and is byte-identical |
| S7 cancel releases DuckDB | a stop interrupts the running statement via `con.interrupt()` within ~100 ms and the run does not start another |

### 3.2 Option (a): a job model — POST starts a job, the client polls `/api/jobs/{id}`, then fetches the result

Server runs the feature on a thread; the result (JSON, HTML or file) is stored under the store's temp folder
(`_export_dir`, api.py:1326-1333, swept like exports at :1344-1358) and served once by `GET /api/jobs/{id}/result`.

- S1: the job registry can refuse or supersede a second identical job; S2: trivially fine — the job is
  independent of the page; S3: the job dies with the server, the poll 404s → "the server restarted"; S4: fine
  (job ids are random); S5: the job's worker thread waits on the lock like today, the status can say so; S6:
  a result stored on disk is robust, but a *progress* failure inside the worker must not kill the job — same
  care as any option; S7: the job owns its connections only if `Store.connect()` is hooked (see 3.6) — the
  job model does not solve this by itself.
- Costs: it rewrites the delivery of ~45 routes (two round-trips each; HTML tabs would load from a result URL,
  which actually *simplifies* `openReportWith`'s blob dance but changes every report route's return type);
  results must be stored and swept (an export is already a tmp file, a JSON answer is not); the request/CLI
  symmetry breaks (the CLI calls the compute functions directly, cli.py:811-892, 1151-1173); every test that
  calls a route and reads the body (dozens, e.g. tests/mrfx/test_determinism.py:75-408 use `TestClient(create_app(cfg, store))`)
  would need a polling helper. Its real benefit — a run that survives the browser going away — is already
  provided where it matters (packets, tracker import) by the daemon-thread pattern (api.py:3632-3675, 2890-2986).

### 3.3 Option (b): streaming progress on the same request (chunked / SSE / NDJSON)

A sync generator route can yield `{"stage":…}` lines and finally the result. It cannot serve `downloadInFrame`
(an iframe GET expects a file, not NDJSON) nor `openReportWith` as written (the tab needs an HTML blob; the
HTML would have to ride inside the last JSON line and the client reassemble it); a dropped stream mid-way
loses the result; the `Content-Length` the browser uses for its own download bar disappears; and a
`StreamingResponse` holds a pool token for the whole run exactly as today. Reject.

### 3.4 Option (c): a client-side estimate from the last measured duration

Store `(route, filter signature) → ms` in `localStorage`, run a clock. Honest only as "about 12 s last time",
dishonest as a percentage (it ignores caches, store growth, a cold materialise, a lock wait). It can be *no*
part of the number. The one acceptable use is the **elapsed** counter ("12 s") beside a stage of unknown
length, which is a clock and labelled as one.

### 3.5 Option (d): hybrid — real stages from the server, DuckDB progress within a stage **(recommended)**

Same request, same delivery; a side-channel keyed by a per-run token. Below, in full.

### 3.6 The recommendation in detail

#### 3.6.1 HTTP API

| Verb / path | Who | Behaviour |
|---|---|---|
| any output request + header `X-Mrfx-Progress: <token>` (POST/fetch) or query `progress=<token>` (iframe GETs: `/api/export.csv`, `/api/export.zip`, `/api/export/outreach.zip`) | page → route | `token` = 16–32 chars `[A-Za-z0-9_-]`, minted per run by the page (`crypto.randomUUID()`), validated by regex server-side. A request **without** a token behaves exactly as today (CLI, tests, scripts, older pages) — no `SET`s, no sampler, no registry entry. |
| `GET /api/progress/{token}` | page polls every 400 ms while its fetch is pending (1,500 ms when the view is hidden) | **`async def`** (like `/api/debug/stacks`, api.py:4497-4511: it must never queue behind a jammed pool, and it takes no pool token). 200 → `{"token", "state": "running"\|"waiting"\|"done"\|"failed"\|"stopped", "pct": 0-99 (int; 100 only in state done), "stage": "Finding this practice's peers", "stage_index": 2, "stage_count": 5, "stage_pct": 0-100 or null, "indeterminate": bool, "elapsed_ms", "message": optional}`. 404 → unknown token (never started, or swept ≥ 60 s after it ended; registry capped at 256 entries, oldest-finished first). |
| `DELETE /api/progress/{token}` | page "Stop" link; also used by `supersede()` for views that today only abort client-side | `async def`. Marks the run cancelled, `interrupt()`s every connection currently attached (ignoring `ConnectionException` on closed ones), returns `{"stopped": true}`; 404 unknown. Idempotent. |
| the output route itself, when cancelled | | answers **409 `{"detail": "stopped"}`** (the existing `_Superseded` → 409 path, api.py:1617-1621, with a distinct detail so the page stays quiet instead of showing an error). A cancelled run **never** delivers a body, even if the compute function returned something (see 3.6.4). |
| `GET /api/packets` (api.py:3653) and `GET /api/medicare/tracker` (api.py:2935) | existing polls | gain `"progress": {pct, stage, stage_index, stage_count, stage_pct, indeterminate, elapsed_ms}` from the job's `Progress`; `POST /api/packets/build` and `/tracker/import` accept nothing new. A `DELETE /api/packets` (new, optional) → cancel; the runner's `except` must handle a cancel and set `state: "done", message: "stopped"` so the button is never wedged (`_run_packets` catches `Exception` only, api.py:3647; `_bg_safe` too, :1408 — a cancel must be an `Exception`, see 3.6.4). |

Status codes used: 200, 404 (unknown token), 409 (stopped / superseded), 422 (bad token format). No new 5xx.

#### 3.6.2 The server helper: `mrfx/progress.py`

```python
# mrfx/progress.py  (sketch — names are the contract, bodies are the engineer's)
_current: contextvars.ContextVar["Progress | None"] = ContextVar("mrfx_progress", default=None)

def current() -> "Progress":            # never None: a shared _NOOP when nothing is running
    return _current.get() or _NOOP

class Progress:
    def __init__(self, token, plan: list[tuple[str, float]] | None, on_update=None): ...
    # plan = [("Finding this practice", 2), ("Preparing peers", 10), ("Comparing with peers", 70), ("Writing the page", 8)]
    def stage(self, name: str, weight: float | None = None, *, indeterminate=False) -> None: ...
    def tick(self, frac: float) -> None: ...        # 0..1 within the current stage; clamps; never backwards
    def waiting(self, what: str) -> ContextManager: ...   # state "waiting" + indeterminate while inside
    def attach(self, con) -> None: ...   # called by Store.connect(): enable progress on `con`, remember it
    def detach(self, con) -> None: ...   # best effort; closed handles are tolerated (ConnectionException)
    def cancel(self, reason="stopped") -> None: ...  # sets .cancelled, interrupt()s attached cons
    def snapshot(self) -> dict: ...      # what /api/progress returns; pct = min(99, …) until finish()
    def finish(self, state: "done|failed|stopped") -> None: ...
    @property cancelled -> bool
```

Rules the class enforces (and the tests in 3.6.7 pin):

- **Every public method is wrapped in `try/except Exception: log.debug(...)`** and returns normally. A
  `Progress` can never raise into a route or a module (invariant 3). The `_NOOP` instance has the same
  methods as empty bodies, so module code calls `progress.current().stage(...)` unconditionally.
- **Percentage** = sum of the weights of finished stages + weight(current) × stage_pct, over the plan's total
  weight; stages encountered that were not planned are appended with weight = the plan's median weight (the
  total grows, so earlier stages shrink proportionally — the bar can **slow** but it never moves backwards:
  `snapshot()` keeps `max(previous_pct, new_pct)`). Capped at **99** until `finish("done")`.
- **stage_pct** comes from (a) `tick()` when the module called it, else (b) the sampler: a daemon thread per
  live `Progress` that every 150 ms reads `query_progress()` of the most recently attached, still-open
  connection and maps `0..100` → `stage_pct` (holding the last value on `-1`: Python between statements).
  Several statements inside one stage: each resets to 0 — the sampler treats a drop as "next statement" and
  keeps the stage's running max, so the bar still does not go back (it will plateau; honest). (c) neither →
  `indeterminate: true` (the client draws a stripe in that stage's slot, the label shows the floor:
  "at least 40 %"). The sampler stops on `finish()` and tolerates `ConnectionException` (probe3: clean).
- **Weights** live in one table, `mrfx/progress_plans.py`: `PLANS = {"benchmark": [...], "pitch": [...], …}`
  filled from §4 and the other reviewer's measurements; a module asks `current().plan_from("benchmark")`
  once at its top (no-op if a plan is already set — the pitch report sets its own plan and the embedded
  benchmark's call is then a nested group, see next bullet).
- **Nesting**: `compute_benchmark` is called by the pitch report, the dossier, the negotiation one-pager (N×),
  `size_premium`, the packets… Its stages must become sub-stages of the caller's current stage, not a new
  plan. `Progress` keeps a stack: `with progress.current().group(weight_of_this_call):` — stages declared
  inside scale into that group's slot. A feature called standalone (the Benchmark button) has no group, so its
  plan is the whole bar. Implementation: `stage()` inside a group updates the group's internal fraction; the
  group's parent stage_pct is that fraction.
- **Stage names are in the user's words**, chosen by the module (it knows what it is doing), with the
  subject/payer in them where cheap: "Finding Aetna's other practices near you (3 of 7 payers)". No SQL words,
  no function names. `stage_index`/`stage_count` come from the plan so the client can print "(2 of 5)".
- **Determinism (invariant 7)**: the object reads; it never alters SQL, parameters, order or the result; the
  `SET enable_progress_bar …` triple is the only thing it writes to a connection and only when a token is
  present (test 3.6.7-T5 pins byte-identical results with and without a token).

**Threading it through with minimal signature churn.** No function signature changes:

- A decorator on the output routes (one line each): `@_progress_route("benchmark")` (closure inside
  `create_app`, next to `_view_query`). It reads the token from header or `progress=` query, creates
  `Progress(token, PLANS[name])`, stores it in the registry (`_progress: dict[str, Progress]` + lock, sibling
  of `_views`), **sets the contextvar inside the route's thread** (the decorator wraps the sync callable, so it
  runs in the threadpool thread — no reliance on anyio copying contexts), runs the endpoint, then
  `finish("done")`/`"failed"`/`"stopped"`, and resets the contextvar in `finally`. For routes that already use
  `_view_query`, unify: `_view_query(request, con)` becomes a thin wrapper that (i) still supersedes by view
  key but calls `old_progress.cancel("superseded")` instead of `old["con"].interrupt()`, and (ii) attaches
  `con`. One registry, two indexes (by token, by view key).
- `Store.connect()` (store.py:2106-2126) gains three lines after `_connect_with_retry()`:
  `p = progress.current(); if p.cancelled: con.close(); raise progress.Stopped(...)`; `p.attach(con)`. No proxy
  around the connection is needed: the sampler holds the raw handle and tolerates its being closed under it
  (probe3: 149 cycles, no crash, clean exceptions). `attach` on the `_NOOP` is a no-op, so the CLI, tests and
  every token-less request pay nothing (not even the `SET`s).
- Modules: `from . import progress` and `progress.current().stage("…")` at the natural points (§4 lists them);
  loops call `.tick(i / n)`. ~40 lines across benchmark.py, schedule.py, leads.py, monitor.py, dossier.py,
  packets.py, clients.py, outreach.py, overview.py, api.py's export path. The existing `progress=` callback of
  `build_all_packets` (packets.py:290) is kept for the CLI's prints and its calls become `stage()` calls too
  (one source of truth: the callback is fed *from* the `Progress`, see CLI below).
- Lock waits: `with progress.current().waiting("Waiting for an earlier summary to finish"):` around the
  `_summary_lock.acquire(timeout=8.0)` (api.py:1913) and the `_count_lock` block (api.py:1682-1690); the
  `_overview_lock` is non-blocking (503) so nothing to show there except the page's own retry.
- The hidden materialise stage: `Store.spine_materialised` (store.py:4964-5012) calls
  `progress.current().stage("Preparing the practice medians (first run since new data)")` immediately before
  its `COPY` (:4993) — the sampler then moves through the COPY (experiment: parquet COPY 0→100).

**CLI parity.** `mrfx packets`, `mrfx export`, `mrfx outreach`, `mrfx orgreport` (cli.py:1151, 811, 855, 1620)
wrap their work in `with progress.text_bar():` — a `Progress` subclass whose `on_update` redraws one line on
stderr (`[#####.....]  48 %  Comparing with peers (3 of 5)`; plain lines when stderr is not a TTY) and prints a
newline at `finish`. The same stages, the same weights, the same words as the web; `mrfx export` additionally
gets the COPY's DuckDB percentage for free (it has no feedback today, cli.py:826-828). `mrfx packets` keeps
its `progress=lambda m: print(...)` output as the stage line (cli.py:1162).

#### 3.6.3 The JS component

One helper, one host element per view, the Files-tab bar's CSS:

```js
// app.js — next to supersede()
function runWithProgress(view, hostSel, label, work /* (opts) => Promise<result> */) {
  const token = crypto.randomUUID().replace(/-/g, "");
  const bar = mountJobbar(view, hostSel, label, () => fetch(`/api/progress/${token}`, {method: "DELETE"}));
  const opts = { headers: { "X-Mrfx-Progress": token } };   // merged into supersede(view).headers by the caller
  const poll = startPoll(token, bar);          // 400 ms; pauses to 1500 ms when the view is not active
  return work(opts).then((res) => { poll.stop(); bar.done(); return res; },
                         (err) => { poll.stop(); err.status === 409 ? bar.stopped() : bar.failed(err.message); throw err; });
}
```

- **Where it renders.** A `<div class="jobbar" id="jobbar-<view>">` directly under each view's button row
  (the form side of the `bench-grid` layouts: after `#b-run`'s row index.html:467-471, `#rc-run` :662-667,
  `#ld-run` :804-807, `#ch-run` :1002-1005, `#ng-run` :515-517, `#mk-run` :398, `#ro-run` :366-367, `#ov-refresh`
  :148, `#pk-build` :89-90, `#md-run` :729, `#ds-run`/`#mr-run` :922/:937, and for the Explorer an
  `#jobbar-explorer` beside `#rates-state` :288 for the table/strip and one under the export buttons :253-256).
  Several concurrent runs in one view (benchmark + its quality pair; Markets' seven routes) stack as rows
  inside the host, each with its own label; the host collapses when empty. `tests/mrfx/test_web_page_ids.py`
  (ids unique, every `$("#…")` exists) covers the new ids automatically.
- **What it says.** `<label> · <stage> (<i> of <n>) · about <pct> %` → "Pitch report · Comparing with peers
  (3 of 5) · about 48 %"; stage unknown-length → "… · at least 40 %" with a striped segment; state `waiting`
  → "Waiting for an earlier summary to finish · 12 s" (elapsed, a clock, labelled); cached answer → the bar
  appears and completes in one frame (no fake crawl; if the fetch resolves before the first poll the bar is
  shown already full for 400 ms then removed — or not shown at all when < 250 ms, to avoid flicker).
  A tooltip on the percentage: "weighted by how long each step typically takes on a store this size; the bar
  moves as DuckDB reports its progress".
- **The 0 → 100 rule.** The server never sends 100 (cap 99). The client sets 100 and the "done" style **only
  when its own fetch has resolved with the body in hand** (JSON parsed; blob received for HTML tabs and
  POST-blobs). For **iframe downloads** the page cannot see the bytes arrive: the bar reaches "Built — your
  browser is downloading it" at the server's `done` (the file is complete on disk at `FileResponse` time,
  api.py:2857; the browser's download shelf shows the transfer) — labelled exactly so, not as 100 % of a
  download. **Never backwards**: `pct = Math.max(prev, server.pct)`.
- **Buttons.** The run button is disabled while its bar is live and re-enabled on done/failed/stopped
  (generalising what `#rc-bundle` does alone today, app.js:2507, 2530); a "Stop" link on the bar calls the
  DELETE. Pressing a report button while its report builds is thus a no-op (S1); for the search-type views
  (Explorer, Code) the existing `supersede(view)` stays the mechanism — the new token is simply added to the
  same headers object, and `_view_query` cancels the older *Progress* (which interrupts the older connection
  exactly as today).
- **The report tab.** `openReportWith` keeps opening the tab synchronously (app.js:1946, the popup-blocker
  reason). Optionally write a one-line "Building your pitch report…" + the same stage text into the blank
  tab (`w.document.body.textContent = …` on each poll — it is same-origin `about:blank`), so the user who
  switched to the new tab also sees progress. Cosmetic; wrapped in try/catch.
- **The existing spinners stay** as the result-area placeholder (they are the "something is coming" cue if
  the progress poll 404s — S6); the muted "pricing…" words are replaced by the bar.
- **View switch (S2)**: the poll interval drops to 1.5 s while `state.view !== view` and the bar is simply
  hidden with its section; on return it is current. Nothing is cancelled by a tab switch.
- **Server restart (S3)**: the fetch rejects (network error) → bar `failed("the server restarted — run it
  again")`; a poll 404 on a token that was running → same text; the registry is in-process so a restart
  forgets every token (no stale "running").
- **Two tabs (S4)**: tokens are per run, `VIEW_ID` per page; no cross-talk. Both tabs' summaries can still
  contend for `_summary_lock`; the loser's bar shows `waiting`.

#### 3.6.4 Cancellation path (S7)

1. `DELETE /api/progress/{token}` (or a superseding request under the same `X-Mrfx-View`) →
   `Progress.cancel()`: `cancelled = True`; for each attached, open connection: `con.interrupt()`
   (~97 ms to take effect, experiment).
2. The running statement raises `duckdb.InterruptException` in the route thread. Modules with fault-isolated
   loops (`dossier.section` dossier.py:155-165, `packets.attempt` packets.py:128-135, the digest's per-client
   `except Exception` clients.py:200-203, `_bg_safe`) will **catch it and move on to the next unit** — that is
   fine *because* the next `store.connect()` raises `progress.Stopped` (a `duckdb.InterruptException`
   subclass — works, probe3 — so it is still an `Exception` and still caught by those loops; each remaining
   unit then costs one raised exception and no query).
3. The route decorator, after the endpoint returns or raises, checks `progress.cancelled` **first**: if set,
   it discards any value and raises `HTTPException(409, "stopped")`. A cancelled run therefore never delivers
   a partial document (invariant 4: a dossier with every section "omitted" must not ship).
4. Detached runners (`_run_packets`, `_run_tracker_import`) check `progress.cancelled` after their function
   returns and set `state: "done", message: "stopped by you — nothing was written for the remaining clients"`;
   files already written by finished clients stay (they are complete documents).
5. Why not `BaseException` for `Stopped`: it would skip `_run_packets`' and `_bg_safe`'s `except Exception`
   and leave `packet_job.state == "running"` forever (api.py:3647, 1408). Keeping it an `Exception` and
   checking `cancelled` after the fact is the robust choice.
6. The 8 s summary rule is preserved: `_view_query`'s supersede → `cancel()` → the older holder's statement
   is interrupted → it releases `_summary_lock` in its `finally` (api.py:1991) → the newer request's
   `acquire(timeout=8.0)` proceeds, now visibly as a `waiting` stage.

#### 3.6.5 Executors and the polls

- Attached runs stay in the request threadpool (100 tokens after startup, api.py:4550-4560). A single user
  cannot exhaust it with outputs: each view allows one run (plus the small follow-ups). Do **not** add a
  second executor for them — it would re-introduce the "hand the result back across threads" complexity of
  the job model for no measured need.
- Detached runs stay on `_bg_safe` daemon threads (never `BackgroundTasks`, api.py:1401-1409).
- The progress poll endpoints are `async` and read a dict under a lock held for microseconds: they cost no
  pool token, so the Files tab's 4 s poll (`/api/files`, sync, paged) and a 400 ms progress poll cannot
  starve each other. Poll volume: 2.5 req/s per live bar, only while a run is live.
- Budget: a `Progress` is one dict, one daemon sampler thread for the run's duration (sleeps 150 ms), and
  three `SET`s per connection the run opens (≈ 0.1 ms each). Registry cap 256; entries swept 60 s after finish.

#### 3.6.6 What changes where (so the engineer can size it)

| File | Change |
|---|---|
| `mrfx/progress.py` (new) | `Progress`, `_NOOP`, `current()`, `Stopped`, the sampler, `text_bar()` |
| `mrfx/progress_plans.py` (new) | `PLANS` — the stage tables of §4 with the reviewer's weights |
| `mrfx/store.py:2106-2126` | 3 lines in `connect()`; 1 line before the COPY in `spine_materialised` (:4993) |
| `mrfx/api.py` | `_progress_route` decorator + registry next to `_view_query` (:1594-1627); `_view_query` → cancel-through-Progress; `GET/DELETE /api/progress/{token}` (async); `waiting()` around :1913 and :1682; decorate ~45 output routes; `progress` fields on `/api/packets` and `/api/medicare/tracker`; `_run_packets`/`_run_tracker_import` cancel handling; stages in `_export_payload` (:2749-2796) and the zip step (:2840-2850) |
| modules | `stage()`/`tick()`/`group()` calls per §4 (benchmark, schedule, visit_market, leads, monitor, dossier, packets, clients, outreach, overview, quality.size_premium, orgprofile, medicare org route) |
| `mrfx/cli.py` | `with progress.text_bar():` in `cmd_export`, `cmd_outreach`, `cmd_packets`, `cmd_orgreport` |
| `mrfx/web/app.js` | `runWithProgress`, `mountJobbar`, `startPoll`; call sites in the 51 inventory rows (each becomes `runWithProgress(view, host, label, (opts) => postJson(url, body, opts))` — `postJson` (app.js:123-124) gains an optional third argument merged into its headers; `downloadInFrame` appends `&progress=<token>`; `openReportWith`/`postDownload` take `opts`) |
| `mrfx/web/index.html` / `styles.css` | `jobbar-*` hosts; `.jobbar` reusing `.progress/.progress-fill/.progress-label` (styles.css:422-427) + a striped `.indeterminate` modifier |
| `docs/AI_HANDOFF.md` | a "Round 13" rule block: token-less requests are unchanged; `Store.connect()` consults the current `Progress`; a cancelled run answers 409 and never delivers |

#### 3.6.7 Tests to pin (tests/mrfx/test_progress.py; fixtures `cfg`/`store` from conftest.py:19-38, `TestClient(create_app(cfg, store))` as in test_determinism.py:75)

| # | Test | Asserts |
|---|---|---|
| T1 | `test_bar_reaches_100_only_with_the_result` | run `POST /api/benchmark/market` with a token on a thread; poll `/api/progress/{t}` at 20 ms; every `pct` seen before the response completes is ≤ 99 and non-decreasing; after completion state `done`; the response body equals a token-less run's body |
| T2 | `test_stop_interrupts_the_query_within_a_bound` | a route running a slow statement (a `range(10**9)` aggregate on a scratch table, or a monkeypatched `compute_benchmark` that runs one) + token; `DELETE` after 200 ms; the route answers **409 within 1 s** (experiment: 97 ms) and the server thread is free (the next request answers in < 100 ms) |
| T3 | `test_cancelled_run_never_delivers` | dossier with 3 sections; cancel during section 1; assert 409, and `build_prospect_dossier`'s remaining sections each raised `Stopped` without opening a connection (count `duckdb.connect` calls via monkeypatch) |
| T4 | `test_progress_failure_never_fails_the_report` | monkeypatch `Progress.stage`, `.attach`, the sampler and the registry to raise; `POST /api/report/ratecard` with a token → 200 and byte-identical HTML to the clean run; `/api/progress/{t}` → 404 or a sane snapshot, never 5xx |
| T5 | `test_results_identical_with_and_without_a_token` | leads JSON, changes CSV, ratecard HTML, export.zip CSV member: byte-equal with/without `X-Mrfx-Progress` (invariant 7 + "observation only") |
| T6 | `test_no_token_means_no_overhead` | monkeypatch `duckdb.DuckDBPyConnection.execute` to record statements; a token-less request issues no `SET enable_progress_bar` |
| T7 | `test_every_output_route_carries_the_decorator` | walk `app.routes`; every route in the §1 list has the marker attribute; new output routes fail this until decorated |
| T8 | `test_supersede_still_interrupts_and_409s` | two overlapping `/api/rates` with the same `X-Mrfx-View` and different tokens: the first ends 409 "superseded", the second's body equals a run alone; the first token's snapshot ends `stopped` |
| T9 | `test_waiting_is_shown_as_waiting` | hold `_summary_lock` from the test; `/api/summary` with a token reports `state: waiting` within 200 ms; release → `done` |
| T10 | `test_detached_jobs_report_progress_and_survive_a_stop` | packets with 2 clients: `/api/packets` shows `progress.pct` rising and the per-client stage text; `DELETE /api/packets` mid-run → state `done`, message says stopped, button not wedged (a second build can start) |
| T11 | `test_poll_endpoints_are_async_and_tokenless` | `inspect.iscoroutinefunction` on both; they answer while every threadpool token is held by sleeping requests (spawn 100 sleeping sync requests first) |
| T12 | `test_cli_prints_the_same_stages` | `capsys`: `mrfx packets`/`mrfx export`/`mrfx outreach` print the plan's stage names in order and end at 100 %; the names equal `PLANS[...]` |
| T13 | `test_sampler_tolerates_connection_churn` | 200 token-bearing requests of a 5-statement route in a loop; no exception leaks, no thread left alive after 1 s |
| T14 | `test_first_run_names_the_materialise_stage` | fresh spine generation; a benchmark's snapshots include "Preparing the practice medians" once; the second run does not |

Add to `tests/slow_tests.txt` only T2/T10 if they exceed 0.5 s; the rest belong in the quick lane.

---

## 4. Per-feature stage plans (the heaviest ~18)

Column **w** is the placeholder for the other reviewer's warm measurement on the 99M store (ms); the ★
marks my expectation of the dominant stage from the code; **Split?** says whether the engineer can/should
split anything (almost always: no — accept the statement, let `query_progress()` move it).

**4.1 Benchmark (`#b-run` → `/api/benchmark/market|opportunity`, then the quality pair)**

| i | Stage (user wording) | Code point | w | Split? |
|---|---|---|---|---|
| 1 | Finding this practice | `subject_tins_for_market` benchmark.py:888 (+ plan scope :887, peer set :889) | | no — several tiny lookups |
| 2 | Preparing the practice medians (first run since new data) | `spine_materialised` COPY store.py:4993 via `practice_base_sql` :892 | cold only | indivisible; DuckDB % |
| 3 | Comparing with peers ★ | the one statement :969-972 | | **indivisible**; DuckDB % moves through base → peers → quantiles → position |
| 4 | Adding Medicare and freshness | anchor :977, loop :989-1004, `_stale_payers_for` :1021 | | no |
| 5 | Checking whether size explains it (opportunity view extras) | `size_premium` quality.py:336 (★ second heaviest), :380, :447 nested benchmark; `code_type_flag` :512 — a second bar row in the host | | the nested benchmark is a `group()` |

**4.2 Pitch report (`/api/report/pitch`)** — stages 1–4 as a `group(60)`, then: 5 "Checking the practice has
rates in this market" (benchmark.py:1872); 6 "Drawing the rate trend lines" (`series_by_code` :2192);
7 "Measuring real-terms erosion" ★ (`_erosion_block` → `compute_payer_trajectory` :2103 — one indivisible
statement, the slowest in the app when national; name it so the user understands the wait); 8 "Listing the
source files" (:2042-2051); 9 "Writing the page" (string work). Split? no.

**4.3 Payer-negotiation one-pager (`/api/report/negotiation`)** — 1 "Finding this practice's payers"
(:1151); 2 "Benchmarking against Aetna's other practices (1 of 7)" … **`tick(i/N)` per payer** ★ (:1162 —
each is a nested benchmark group of equal weight); 3 "Writing the page". Split? already split by payer; no
further.

**4.4 Rate card (`/api/schedule/fee`; the printable / CSV / bundle reuse the plan + "Pricing a standard
visit in this market" + "Writing the card/CSV/zip")** — 1 "Finding this practice" (schedule.py:73);
2 "Reading its own rates" (stmt :112); 3 "What each payer pays its peers for the same codes" ★ (stmt :159,
"95 %" per the docstring); 4 "Ranking the payers" (pure :214). Printable adds 5 "Pricing a standard visit
across the state" (visit_market.py:165) and 6 "Drawing trends and listing sources" (:467, :304-313).
Bundle adds "Collecting NPIs and contact details" (orgprofile.py:57-83) and "Zipping" (api.py:2876-2879).
Split? no.

**4.5 Leads (`/api/leads`, `.csv`)** — 1 "Setting the market" (resolve exclude :158, base :169);
2 "One rate per practice and code" ★ (`_lead_base` :207); 3 "The market's median without each practice" ★
(`_lead_om` :208); 4 "Ranking the underpaid" ★ (:211); 5 "Naming the worst payer for each lead" (:260);
6 "Checking who dropped out of the newest files" (:275); 7 "Adding websites" (:289). Split? the three ★
are already separate statements — perfect stages; no further.

**4.6 Leaderboard (`/api/leaderboard`)** — generation-cached: 1 "Preparing the market" (base :357);
2 "Ranking the practices" ★ (:392). A cached hit is 0 → done in one step. Split? no.

**4.7 Rate changes (`/api/changes`, `.csv`)** — 1 "Pairing each payer's months" (`payer_months` monitor.py:49-78);
2 "Finding this practice" (opt., :241); 3 "Comparing the two months line by line" ★ (:306-307, indivisible);
4 "Sorting the moves" (Python :312+). Split? no.

**4.8 Payer trajectory (`/api/trajectory`)** — 1 "Listing the months" (:397); 2 "Tracing every payer across
all months" ★ (:501-502, indivisible — `MATERIALIZED` base then panel/index CTEs; DuckDB % will plateau
between pipelines: say "this is the slowest view on a big store" in the label as app.js:4113 already does);
3 "Applying the price index" (:504+). Split? no.

**4.9 Prospect dossier (`/api/report/dossier`)** — 0 "Finding the practice" (:110-124); then one stage per
section in the order of dossier.py:339-345: "Who they are" (:167), "How big they are vs the market" ★
(`size_premium`), "Where their rates sit" ★ (nested benchmark), "Their weakest payer" ★★ (nested negotiation,
N payers → `tick`), "Who else the payers contract nearby" (leverage), "Who refers to them" (:295),
"The market around them" (:319); last "Writing the page". Also: **stop building it twice** (§1, #38).
Split? already per section; no.

**4.10 Market report (`/api/report/market`)** — one stage per section (dossier.py:475-479): "Rates by city" ★
(3 stmts), "Who holds the contracts", "Which payers negotiate" ★ (`payer_posture`), "Openings and closures",
"Local demand", then "Listing the source files" (:489-495), "Writing the page". Split? no.

**4.11 Monthly packets (detached, `/api/packets`)** — 1 "Finding each client's newest month" (packets.py:303);
2 "Finding contract gaps for the whole book" ★ (:319); 3 "Building the book's digest" ★ (:329, see 4.13);
4 "Building Maria's packet (2 of 5): rate card / market position / what moved / gaps / wins / closed sources"
— per client `tick(i/N)`, per section a nested group (:143-285) ★★; 5 "Writing the index". The existing
`progress(msg)` callback (:290) is the stage line. Split? already split.

**4.12 Client digest (`/api/clients/digest`)** — cached; else 1 "Pairing each payer's months";
2 "Finding contract gaps for the whole book" ★ (clients.py:129); 3 "Checking Maria's rate moves (2 of 5)" ★ per
client `tick` (:137-190: 2 small stmts + a full `compute_rate_changes` + Medicare); Split? per client already.

**4.13 Explorer / Code export (`/api/export.csv|zip`)** — 1 "Checking the filters and the disk space"
(:2770-2781); 2 "Counting the rows" (only when not roomy, :2778-2780 — indivisible count); 3 "Writing the rows
to a file" ★★ (`COPY … TO csv` :2782 — DuckDB % moves through it, experiment: 0 → 100 with plateaus while the
sort spills); 4 "Writing the methodology" (:2789); zip: 5 "Compressing" (`copyfileobj` :2845-2848 —
`tick(bytes_copied / size)` is natural); 6 "Built — your browser is downloading it" (done). Split? no; stage 5
is the only one with a free exact fraction.

**4.14 Outreach zip (`/api/export/outreach.zip`)** — 1 "Listing the organizations in this view" ★ (units
stmt outreach.py:133); 2 "Looking up addresses and contacts" (:151-152); 3 "One rate per organization and code"
★ (:174-178); 4 "Choosing the code columns" (:183-199); 5 "Writing the CSV and methodology"; 6 "Compressing".
Split? no.

**4.15 Overview (`/api/overview` + quality trio)** — 1 "Reading this market's rows" ★ (temp table
overview.py:49); 2 "Counting payers, codes and practices" (:54); 3 "Typical evaluation rate per discipline" (:65);
4 "Best-covered codes" (:77); 5 "Payer index" ★ (:98); then a second row: "How solid is the data" (freshness,
mix, posture). Cached per state/generation. Split? already five statements.

**4.16 Explorer table + strip (`/api/rates`, `/api/summary`)** — rates: 1 "Finding the rows" (:1803), 2 "Counting
them" (:1805, cached 30 s). summary: 0 "Waiting for an earlier summary to finish" (lock :1913, `waiting`),
1 "Summarising the rates" (:1930), 2 "Per-code medians" (:1967). These are the views where supersede matters;
the token rides in the same `supersede(view)` headers. Split? no.

**4.17 Markets (seven routes)** — one bar row per route in the order fired (app.js:510-513, 528-530), each a
single indeterminate stage moved by DuckDB %; label "Market intelligence · 4 of 7 answers in". Split? no.

**4.18 Medicare practice lookup (`/api/medicare/org`)** — 1 "Finding the practice's NPIs" (:3757-3761);
2 "Who shares patients into it" ★ (`org_referrals` in + eligibility, :3787); 3 "Where it shares patients onward"
★ (:3788); 4 "Checking Medicare standing" (:3795-3798). Split? already.

Everything else in §1 (leverage, economics, floors, hospital, engagements, renewals, new-to-network, new
clinics, closures, steal-share, growth, sizing, remits, leaders, eligibility, roster, contract gaps, visit
value, negotiate compare/proposal) gets the decorator with a one- or two-stage plan ("Working it out" moved by
DuckDB %, then "Writing") — correct, cheap, and upgradeable to a fuller plan when the measurements say a
route is worth it.

---

## 5. What NOT to do

1. **No bar that creeps to 90 % and waits.** The percentage is only ever (weights of finished stages +
   DuckDB's own fraction of the current one); when neither is known the bar shows a stripe and "at least N %",
   never a time-based crawl. (Invariant 4.)
2. **No client-clock percentage.** The browser never advances `pct` on its own; the only client-side number
   is an *elapsed seconds* counter, shown as such, during `waiting`/indeterminate stages. A "last time this
   took 12 s" hint may be printed as text, never drawn as progress.
3. **Do not block the event loop.** Routes stay sync in the threadpool (100 tokens, api.py:4550-4560); the two
   new poll endpoints are `async` and must stay free of store access and locks held for more than
   microseconds (follow `/api/debug/stacks`, api.py:4497-4511). Do not move output work onto `BackgroundTasks`
   (api.py:1401-1409 explains why); do not add a second executor for attached runs (§3.6.5).
4. **Do not duplicate the abort machinery.** One registry; `supersede(view)` keeps its `AbortController` and
   `X-Mrfx-View`; the token is one more header on the same object; `_view_query`'s older-request interrupt
   becomes `old_progress.cancel()`; the 8 s summary wait is unchanged. No second "cancel" header, no second
   client-side controller.
5. **Do not split DuckDB statements to manufacture stages.** The CTE pipelines (benchmark :969, leads :211,
   diff :306, trajectory :501, export COPY :2782) are indivisible on purpose; splitting them changes plans,
   memory behaviour and — through tie-ordering — risks invariant 7. Use `query_progress()` inside them.
6. **Do not let the helper touch results.** `Progress` reads; it must never change SQL, parameters,
   ordering, rounding or payloads (T5 pins byte-equality with and without a token).
7. **Do not enable the DuckDB progress bar globally** (`SET GLOBAL`), on the pinned connection, or on
   token-less connections: it is session-local by design (probe2), and the rollup/ingest connections must
   stay exactly as they are (store.py:2160-2228 — the `SET threads` deadlock history shows why settings on
   shared connections are not casual).
8. **Do not make `Stopped` a `BaseException`** (§3.6.4 item 5) and do not let a cancelled run return a body
   (§3.6.4 item 3).
9. **Do not report 100 from the server**, and do not call an iframe download "100 % downloaded" — say
   "built; your browser is downloading it".
10. **Do not poll faster than 400 ms or when nothing runs**; stop the poll on the fetch's settle and on
    `finish`; back off to 1.5 s when the view is not the active one.

---

## Appendix A — raw experiment output (abridged)

```
duckdb 1.5.5 | rows 8000000
enable_progress_bar=False: query 1876 ms  samples all -1.0
enable_progress_bar=True : query 1980 ms  distinct pct: -1, 7.2, 17.1, 25.2, 37.8, 49.5, 58.6, 62.8, 65.5, 68.5,
                           70.6, 74.2, 76.5, 79.0, 83.2, 86.7, 90.1, 92.7, 95.9, 99.8, 100.0   (after completion: -1.0)
interrupt latency: InterruptException 97 ms after interrupt(); total 397 ms; connection reusable: True
cursor() sees parent progress? distinct values: ['-1.0']
  count(*)            41 ms  distinct%=[-1, 49, 100]
  order by limit      40 ms  distinct%=[-1, 52]
  window             514 ms  distinct%=[-1, 5, 10, 15, 21, 25, 39, 47, 53, 55, 60, 67]... max=99.3
  COPY to parquet    327 ms  distinct%=[-1, 12, 25, 34, 45, 57, 63, 77, 86, 95, 98, 100]
scope: a = True | b (untouched) = False ; print flag a = False
monotonic check: 191 samples, 0 decreases ; per-statement reset: [(0.0,100.0),(0.0,100.0),(0.0,100.0)]
300 short point queries: off 1479/1537 ms, on 1609/1428 ms ; idle value: -1.0
open/run/close cycles: 149; sampler: ok 4354, 'ConnectionException: Connection already closed!' 1840; no crash
closed connection: query_progress()/interrupt() raise ConnectionException cleanly
subclass of duckdb.InterruptException works; is Exception: True
COPY (ORDER BY) TO csv, 8M rows: 2983 ms, 138 samples, distinct% 0,5,9,16,20,23,28 … 50,62,76,100
full ORDER BY: 615 ms, longest plateau ~280 ms, max 100
```

## Appendix B — docs this design leans on

- CLAUDE.md invariants 3, 4, 7 and the test/lint commands.
- docs/AI_HANDOFF.md:538-547 (Round 10: `X-Mrfx-View`, `con.interrupt()`, 409, the ≤ 8 s summary wait);
  :38 (summary-strip caching by `data_generation`, 30 s TTL for joined views).
- docs/NEXT_STEPS_PROMPT_10.md:151-172 (1.1: why superseded searches are interrupted, the per-request
  connection at store.py `connect()`), :228-233 (1.6: the summary lock wait), :267-282 (1.9: `/api/quality/size`
  9.9 s cold, `subject-types` uncached).
