# Prompt for the next session, round 10 — fast searches, and every output says the same thing (written for Claude Opus 5.5)

> **STATUS (Nov 10, 2026, v0.7.4): Groups 1–4 closed except 1.5 (partly)
> and 1.9's page cache — below.** Code + tests: 23d19a2; release (docs,
> slow-test list, counts, this block): the commit after it. One code commit,
> not one per group: the groups share `api.py` / `app.js` / `index.html`
> hunk by hunk and a split leaves commits that do not import; its message
> is organised by group and names each test with its 2bb81b8 value.
> `tests/mrfx/test_review10.py`: 30 cases, 26 fail on 2bb81b8 (the 4
> bad-input cases are covered by the reviewer's fuzz instead, below). Full
> suite 1115 tests (the first run's 7 failures were tests pinning the old
> rules — each updated with the reason; those files re-run green); quick
> tier 549 tests; ruff 0; `tests/slow_tests.txt` regenerated.
>
> **Search, 99M-rate store, Chromium `search/ui_search.py`, quiet** (full
> table: `review10_repros/search/ui_table_r10.md`; cold = store evicted
> from the page cache, server started fresh, same store and driver):
>
> | Scenario | 0.7.3 cold | 0.7.4 cold | 0.7.3 warm (review) | 0.7.4 warm |
> |---|---|---|---|---|
> | First screen after a start | 11.3 s | **2.4 s** | 2.2 s | 1.1 s |
> | Name typed at 120 ms/key | 6.0 s | **3.8 s** | 6.1 s | 3.9 s |
> | Partial name at 400 ms/key (slow typist) | 45.2 s, 12 refused | **5.6 s**, 0 | 26.6 s, 15 | 5.7 s, 0 |
> | 9-digit TIN at 400 ms/key | 10.4 s, 12 refused | **3.6 s**, 0 | 21.1 s, 8 | 3.6 s, 0 |
> | Request after the last key → result | 41.1 s (slow name) | **1.5 s** | 22.5 s | 1.6 s |
> | State typed | 6.8 s | 3.4 s | 7.3 s | 3.4 s |
> | Clear all filters | 5.1 s | 2.7 s | 5.1 s | 2.7 s |
>
> First start of 0.7.4 on a store 0.7.3 built (no practice list, no stored
> strip, cold cache): 6.5 s. One earlier first start, right after a
> disk-heavy snapshot run, took 60.3 s (`/api/stats` 57.8 s); it did not
> reproduce under the same conditions — like the review's unreproduced
> 38 s `/api/stats`, worth instrumenting if a user reports it.
> Entity grain (32M store, A/B in process): default page 3.68 → 1.29 s,
> AL 2.32 → 0.90 s, sort by rate_max 3.09 → 1.28 s; 0 mismatches over
> 11 filters × 6 sorts × 3 offsets + counts.
>
> **Byte identity** (`review9_repros/outputs/snapshot.py` + `diff_snap.py`,
> 32M store, 125 endpoints, 2bb81b8 vs 0.7.4): 70 identical — including
> all 24 `/api/rates` pages at every grain, search, sort, page, state and
> the entity two-pass path, and the CSV exports; the 14 `/api/summary`
> diffs are only the half-up rounding (3.2); every other diff is a fix
> listed below (geography, city table, states list, scorecard, leads gaps,
> trend basis, overview, freshness, wording). Two 0.7.4 runs: 125/125
> identical (per-generation caches; invariant 7).
>
> **Bad input** (`consulting/p5_fuzz.py`): 2bb81b8 17 unique 500s in 4,501
> requests (contract-gaps `min_peers` 1e308, new-to-network `radius_miles`
> "NaN", trajectory and engagements/compare with a non-object market);
> 0.7.4 none in 4,525. The run found one class this round introduced
> (economics/visit with a non-object market, via the new subject rule);
> closed at `subject_tins_for_market` for every surface.
>
> **Live Chromium, 32M real store:** `consulting/ui10.py` (63 steps, every
> tab): 0 page errors, the same 2 expected refusals and 7 downloads as
> 0.7.3; one new "raw JSON" hit is a tested-catalog note on Sources quoting
> a manifest format (`{"mrfs": [...]}`) — prose, left as is. Fixed screens
> checked (`screens.json`): Explorer month "latest", no "busy" text while
> typing, the NPI hint, Markets ND ranks every state with ND marked, a
> picked month with nothing comparable says so, Sources WV carries the
> tested link, the Code page claim-type box, the evaluation-code overview.
>
> **Left, with the reason.**
> - 1.5: the 2-character minimum removes the one-letter case; serving the
>   cached unfiltered answer when a text matches every practice was not
>   built (the strip's per-generation cache cannot hold name searches — names
>   change as NPPES identification lands without a new generation).
> - 1.9: the unfiltered page's base rows are not remembered and the name
>   sort is unchanged (Clear all filters 5.1 → 2.7 s through the other
>   fixes). The rest of 1.9 is done.
> - Markets' state ranking still lists military mail codes (AE, 7
>   practices on the real store): they are NPPES practice-location values
>   in `tin_directory.states`. The "states with data" count and the state
>   pickers exclude them; filtering the ranking the same way is a one-line
>   follow-up (`market.geographic_rates`).
> - Not run on Windows: everything here ran on Linux; the Windows-specific
>   paths (launcher, updater) were not touched this round.

Rounds 1–9 are `docs/NEXT_STEPS_PROMPT.md`, `_2.md` … `_9.md`. This memo
comes from a review on Nov 10, 2026 at HEAD 2bb81b8 (version 0.7.3). The
user, a non-technical consultant running `mrfx serve` on Windows, asked:
**"Is everything running as efficiently as possible on the outputs front?
It feels like some of the data takes a while to pop up after I search."**
and asked for every category to be tried and every change, addition and
removal double-checked.

Method — four independent passes, none by the author of the code under
review:
1. **Search latency** (`docs/review10_repros/search/`): a real Chromium
   typing into the real boxes at 120 ms and 400 ms per key, every `/api`
   request recorded, on a 99M-rate synthetic store (194,000 TINs, 79M-row
   spine) and a 32M-rate real-data store (20,101 TINs, 1.35M-row spine);
   EXPLAIN ANALYZE of every statement; two fixes prototyped in scratch copies
   with all 57 response bodies byte-identical to the shipped code.
2. **Rates and Practice tabs** (`docs/review10_repros/rates/`): Overview,
   Clients, Explorer (every grain, filter, sort, page, export), Code
   comparison, Markets, Rate changes, Sources — hand-worked fixtures, the
   real store, 63-step Chromium passes.
3. **Consulting, Growth and every client-facing document**
   (`docs/review10_repros/consulting/`, with its own `FINDINGS.md`) —
   hand-worked fixtures, the real store, printed PDFs, a 4,501-request fuzz.
4. **What changed 0.7.2 → 0.7.3**
   (`docs/review10_repros/SNAPSHOT_0.7.2_vs_0.7.3.md`): the body of every
   dashboard endpoint (125) on the real store under both builds. 109 are
   byte-identical; each of the 16 differences is an intended round-9 change
   (listed there). Nothing was removed; nothing regressed beyond noise.

Every item below was reproduced by a script that was run. Severity: **P0**
the user's common path is wrong or unusably slow · **P1** wrong or missing
on a real path · **P2** inconsistent or misleading · **P3** cosmetic.

## How to work

- Read `CLAUDE.md`, then `docs/AI_HANDOFF.md` in full (Step 3 invariants,
  the "Percentile conventions" row, "Round 7 / Audit 8 / Round 9
  additions"), then this file. Numbers correctness and reproducibility are
  the product: **every speed fix must leave every answer byte-identical**
  — prove it with `docs/review9_repros/outputs/snapshot.py` + `diff_snap.py`
  (before / after on the same store) in the commit message.
- Each fix: reproduce first (the script named in the item), fix the class,
  leave a regression test in `tests/mrfx/test_review10.py` that fails on
  2bb81b8, re-run the script.
- **Stores.** The two big stores of this review lived in a session scratch
  folder and may not exist in your container. The fixture builders
  (`docs/review10_repros/*/fx.py`, `fxm.py`, `fxf.py`, `c2_hypo.py`) build
  every hand-worked case from nothing. For timings, build a synthetic store
  of at least 30M rates (many TINs, several payers and months, real CPT
  codes — the shape of `snapshot.py`'s `calls_for_scale`, payers "Payer 00"
  …), keep it OUTSIDE the repo, and report numbers before/after on the same
  store and quiet machine. The figures quoted here are the reference.
- Repro scripts were written against `/tmp/mrfx_review10/<area>/…`; their
  first lines name the paths — point them at your scratch folder.
- Chromium: Playwright is installed; `executable_path='/opt/pw-browsers/chromium'`,
  args `--headless=new --no-sandbox`. Never run `playwright install`.
- Order: Group 1 (the user's question) first, then 2, 3, 4. One commit per
  group (what + why + verified-with-numbers, each test named with the value
  it failed with on 2bb81b8).

## Group 1 — searching: why results take a while to appear (P0/P1)

The short answer (`search/FINDINGS.md`, "The short answer"): on the common
path it is not one slow query. **(a)** Nothing is sent for 0.58 s after the
last key — two debounce timers in series; on the 32M store that is 60–90 %
of the whole wait. **(b)** A superseded search is never stopped: type slowly
(> 0.3 s between keys) and every key starts two whole-store scans that run
to the end for nobody. On the 99M store, `practice 12` typed at 400 ms/key
appeared **26.6–122.6 s** after the first key (4 runs; once as "needs more
memory"); with 1.1 + 1.2 + 1.3 prototyped: **5.9–6.9 s**, same answer. The
browser is not the problem: no main-thread task over 177 ms in 15 runs.

### 1.1 (P0) Superseded searches are never cancelled
- Browser: `mrfx/web/app.js:804-816` (`loadRates`), `:885-889`
  (`loadSummary`) — the "ticket" (`:98-106`) only stops an old answer being
  painted; the fetch is not aborted (only the Code page aborts, `:1360`).
  Server: `mrfx/api.py:1360` (`rates`), `:1406` (`summary`) are synchronous
  and run their DuckDB statements to completion after the browser hangs up.
  The first partial texts (`q=p`, `q=pr`) are the most expensive searches
  there are (1.5). `search/abandoned.py`: six searches abandoned within
  1.9 s kept the server busy 40 s; an exact-TIN search (0.06 s) took
  0.4–1.35 s meanwhile.
- Fix (prototyped: `search/protoA_appjs.diff`, `protoA_api.diff`):
  AbortController per view in the browser plus an `X-Mrfx-View` header; a
  server registry keyed by it — a newer request calls `con.interrupt()` on
  the older one's connection (each request has its own, `store.py:1989`),
  the older answers 409 "superseded", a flag checked between statements.
  Requests without the header (CLI, tests, scripts) behave as today. Reads
  only — the single-writer rule is untouched.
- Numbers to beat (99M store, 400 ms/key, `search/ui_search.py`): name
  26.6–122.6 s → 6.7–6.9 s; TIN 21.1–53.4 s → 3.70–3.75 s; 503s 8–15 per
  search → 0.
- Test: two overlapping `/api/rates` requests with the same view key — the
  first ends 409 within a bound, the second returns the same body as alone.

### 1.2 (P0) 580 ms of dead time before any typed search is sent
- `app.js:1034-1038` waits 300 ms, then `refreshFromFirstPage()` →
  `refreshSoon = debounce(refresh, 280)` (`:965-966`). Measured in all 8
  runs on both stores: request leaves 581–588 ms after the last key. Real
  store: a TIN search waits 0.64–0.74 s of which 0.06–0.16 s is the server.
- Fix (3 lines, in `protoA_appjs.diff`): typed inputs call `refresh()`
  directly with `page = 1`; chips keep `refreshSoon`. → 301–309 ms. Also
  consider a single click on a checkbox / dropdown (0.28 s timer, no reason).
- Test: a Playwright step that types a TIN and asserts the request leaves
  < 350 ms after the last key.

### 1.3 (P1) Name / state / partial-ID search: eight spine scans per search
- `_tin_rows_by_unit` (`api.py:174-198`) rebuilds the practice list
  (`SELECT DISTINCT tin_value, tin_is_really_npi FROM k`, `:193`) from the
  79M-row spine in each of four statements (HASH_GROUP_BY 2.2–3.2
  thread-s each), then probes the spine again; a search sets
  `uses_dim_cols`, which turns off the late name join (`:1262-1274`), so
  names are joined to every matching row before Top-N.
- Fix (prototyped: `search/protoB_api.diff`): the practice list as a
  194,000-row file per spine generation via `Store.spine_materialised`
  (0.58 s once, 1.1 MB, reused across restarts); at the TIN grain cut the
  page on the spine and attach names to the 100 kept rows. Numbers
  (`search/req_table.md`, 99M, alone): full name `/api/rates` 1.84 → 0.99 s,
  `/api/summary` 2.15 → 1.38 s; state 3.00 → 1.27 s / 3.38 → 2.19 s; one
  letter 6.92 → 2.40 s; in the browser 6.1–9.3 → 4.0–4.2 s. Not done yet: a
  small practice set read through zone maps with an inlined `IN (…)` list
  (0.14 s vs 0.33 s, `search/exp_in.py`; `OR` chains and UNION ALL are
  slower).

### 1.4 (P1) First screen after a start waits behind the header counts; the strip is recomputed every start
- `app.js:4667-4683` (`boot`) awaits `/api/stats` (whole-store counts,
  `store.py:4898`, `:4950`) before the first `refresh()`, only to read the
  default grain; payers and months load one after the other (`:990`,
  `:1019`). 99M: table painted 3.9–6.8 s, strip 11.7–13.7 s. The unfiltered
  strip is kept per data generation only in memory (`api.py:1398`, `:1425`):
  8.5–11.5 s again after every restart (exact quantiles of 79M values +
  4.8M practice × code groups).
- Fix: a one-field `/api/boot` (prototyped, 13 ms; first `/api/rates`
  leaves at 0.62–0.66 s instead of 1.47–2.12 s); payers + months together;
  the unfiltered strip (and the first unfiltered page's base rows) stored
  beside the store under `Store.spine_key()` exactly like the materialised
  medians, built in the background after a rollup / at start. Expected: strip
  ~0.7 s after a restart. Also: the very first `/api/stats` of one session
  took 38.0 s (not reproduced; 1.2–1.7 s reproducible) — instrument it.

### 1.5 (P1) One- and two-letter searches cost more than no search
- `q=p`: `/api/rates` 6.92–7.15 s, `/api/summary` 11.74–12.15 s vs 1.44 s /
  8.6 s unfiltered (then remembered). A short text matches every practice,
  removes nothing, but turns off the late join and the strip's generation
  cache (`api.py:1425`, `per_generation` false once `uses_dim_cols`).
- Fix: resolve the practices first (0.09–0.17 s on the 1.3 list); when the
  set is every practice, serve the unfiltered (cached) answer — the same
  rows. Consider a 2-character minimum for name text (not for digits).

### 1.6 (P1) "Stats are busy — retrying…" for the search the user meant
- `/api/summary` is single-flight (`api.py:1434-1441`): the newest request
  is refused while an older keystroke's scan holds the lock — 7–9 refusals
  per slow-typed search (99M), 1–4 (32M); the page retries once
  (`app.js:929-935`). Fix (in prototype A): the newer request interrupts the
  superseded holder and waits for the lock (bounded 8 s). 0 refusals.

### 1.7 (P1) Entity grain is re-aggregated from the whole spine for every page, sort and ID
- Entity view on the 32M store (`rates/timing_rates.py`): default page
  3.9–4.9 s (and 3.2–3.7 s WARM in the snapshot run — no cache), page 2
  7.3–13.0 s, sort by name 7.6–11.3 s, `state=AL` 2.3–2.9 s; the Entity
  button took 14.7 s to settle in Chromium. EXPLAIN (`rates/explain_entity_all.txt`,
  `rates/p1_entity_explain.py`): 1,350,747 spine rows → HASH_GROUP_BY into
  1,348,421 groups each carrying `list()` of TINs, rates, files, states,
  cities; then `list_sort(list_distinct(flatten(…)))` on every group before
  TOP_N 100 — only 2,326 rows actually merge.
- Fix (prototyped, `rates/p2_entity_proto.py`, identical 100 rows in all
  four cases): choose the page's group keys with only the median and
  `max(file_month)`, then build the list columns for those 100 groups —
  no filter page 1 5.44 → 1.64 s; offset 49,900 5.47 → 2.13 s; AL 3.22 →
  0.93 s. Sorts on list-derived columns need that column in the first pass.
- An exact ID at the entity grain is tested after the whole rollup
  (`_ident_clause`, `api.py:355-362`; `rel_sql` `:716` keeps an ID off the
  practices-first branch): Entity + an NPI in the box = 10.3–15.1 s, then
  "No rates match". Fix: resolve the ID to its entity key first (the TIN
  drawer does it in 0.06 s); for an NPI say "this NPI bills under TIN …".

### 1.8 (P1) NPI grain on a mid-size store: 503 after 5–16 s and collateral 503s
- `/api/rates?grain=npi` (no code): 503 in 5.4–9.0 s; with `state=AL`
  (which the message itself suggests) 503 in 9.8–16.2 s; `/api/summary?grain=npi`
  31.6–43.1 s. While one runs, a narrow request (normally 0.6–1.5 s) also got
  the 503 (`rates/r_npi_concurrent.py`, 1 of 12). The pager kept "page 1 of
  5,394 · 1,348,421 rows" from the previous view. Cause: `api.py:348`
  `NARROW_REQUIRED_ROWS = 40_000_000` — this 32M store is under the guard yet
  over what the 6 GB budget aggregates (`_require_narrowing`, `:1332`). Fix:
  derive the guard from the memory budget (≈15M here); require an exact
  NPI/TIN or a code (a code alone: 1.3–1.9 s); skip the summary when the
  table was refused; reset the pager on error.

### 1.9 (P2) Smaller, each measured
- The unfiltered table is recomputed on every return (1.43 s, 99M) and the
  name sort joins names to all 79M rows (4.60 s; "Clear all filters" keeps
  the sort: 4.6–5.5 s). Remember the late-join page's base rows per (filter,
  sort, page, generation); for the name sort, order the 194,000 practices
  first.
- `app.js:808` blanks the table the moment a search fires (10 times for one
  slow-typed search): keep the previous rows dimmed until the new ones land.
- Subject autocomplete is fine (17–86 ms) but a 10-digit NPI returns no
  suggestions although the box invites one (`api.py:3306` matches name and
  TIN prefix only).
- After picking a subject, `/api/quality/subject-types` is 0.76–2.5 s on
  EVERY call on the 99M store (not cached) and `/api/quality/size` 9.9 s on
  the first call after a start.
- `%` and `_` in the search and modifier boxes act as SQL wildcards (`q=_`,
  `modifier=%` return every row; the FilterSet `LIKE` clauses) — escape them.

## Group 2 — wrong numbers on real paths (P0/P1)

### 2.1 (P0) The market report puts other states' cities in a state's report
- Real store, Leads → market report, ND, 97110: "Within ND, Tampa pays
  8.1 % more than Tuscaloosa"; 10 of 12 city rows have no provider in ND
  (WY: 8 of 9 rows are Alabama cities; MA: 48 of 60). The same function
  feeds `/api/territory/local` (Markets). Cause: `mrfx/territory.py:106-115`
  (and `:141-148`, the suppressed count) explodes `tin_directory.cities` —
  every city of a TIN with any location in the state. Expected ND: Fargo
  (10 practices, $28.84), Grand Forks (5, $28.84). Fix (prototyped in
  `consulting/p12_market_cities.py`): a TIN's cities in the state = cities of
  its NPIs whose NPPES state is that state; ND 0.10 → 0.44 s (use the
  placement parquet to make it cheaper). Also `territory.py:153-165`
  compares the first city with the 60th when more than `limit` qualify (read,
  not run).

### 2.2 (P1) The Explorer cannot show the number every other tab shows
- The Explorer's month box offers "all months" (default) or one calendar
  month, never "latest — newest file per contract"; `filterQuery`
  (`app.js:792`) never sends `latest`, yet the By-code footnote says "the
  Benchmark's basis" (`app.js:951`). Fixture (`rates/c2_hypo.py`): By code
  97110 $48.01 vs $47.51 on Overview / Markets / Benchmark (median(48.01,
  47) = 47.505 newest-per-contract; pooled months weight a payer by how many
  months are loaded). Real store 97530: 2026-11 $28.80 / 2,646 practices,
  2026-10 $37.56 / 7,719, all months $37.56 / 13,704 — no setting gives the
  house basis. The API already supports `latest=1`.
- Fix: a "latest — newest file per contract" option, the default, sent as
  `latest=1` (exports too; the sidecar already describes it). Check the
  round-5 decision ("Explorer default", AI_HANDOFF) before changing the
  default and say in the commit why; at minimum the label must not claim the
  Benchmark's basis over pooled months.

### 2.3 (P1) Markets with a State: other states' medians computed from that state's practices, under "National median"
- Real store, 97110, State AL: "National median $28.84 across 5,647
  practices"; `TX $28.84 · 123`, `NY $28.84 · 59`, `MA $25.68 · 25`. Without a
  State: TX $26.17 (1,874), NY $24.95 (216), MA $22.51 (691), national $28.84
  across 10,227. Cause: `mrfx/market.py:67-106` `geographic_rates` filters
  practices by the state, then unnests each survivor's whole `states` list;
  the reference query (`:96-101`) uses the same filtered `where`. Label at
  `app.js:612`. Fix: drop `state`/`city` from the market before building
  `where` in this view (the state is its dimension) and return
  `selected_state` to highlight. Repro `rates/r_real_findings.py` §1,
  `rates/c2_hypo.py`.

### 2.4 (P1) Rate changes with a month picked drops payers and can print "No changes" when nothing was compared
- `mrfx/monitor.py:124-127`: `old_month` is the store's newest month before
  the new one, for every payer; `_diff` inner-joins the two months. Fixture:
  Beta publishes 04 and 06 and cuts A3 REHAB 47 → 40 (−14.9 %); New month 06
  + auto compares 05 → 06 and Beta's cut is absent and unnamed. Real store:
  "No changes, 2026-10 → 2026-11" though no payer has both months. Fix:
  per-payer pairs bounded by the picked month, as `_latest_changes`
  (`:150-176`) does, returning `compared` / `not_compared`; "needs two
  published months" when nothing compares. Also (P3) a one-cent move is
  dropped: `abs(c.rate - p.rate) >= 0.01` at `monitor.py:236` fails for
  40.01 − 40.0 — compare rounded rates; and `_diff` (`:206`) still uses
  `resolve_subject_tins` with the state-filtered relation (a KS practice with
  State MO → "No changes" while Benchmark shows its $63).

### 2.5 (P1) Row drawer chart medians over superseded months, modifiers and classes
- Fixture A1 REHAB: bar "97110 · Alpha Health $50.00" above a table whose
  rows are 2026-04 $50, 2026-05 $50, 2026-06 $55 (Benchmark and the latest
  Explorer row: $55.00). `api.py:1578-1586` `median(negotiated_rate)` over
  `GRAIN_REL[grain]` with only the dollar filters (also pools CQ/KX and
  institutional: 34, 40, 41, 100). Fix: house basis (newest month per
  contract, professional, base), titled "current rate"; add a POS column to
  the "All rates" table (two POS lines look identical). The chart draws only
  two payers (`app.js:1166`, `:1202`; known since round 7).

### 2.6 (P1) The rate card names the wrong "Best-paying payer" when a Medicare anchor is loaded
- `consulting/p8_worst_payer.py`: A REHAB — Alpha 97110 $45, 97140 $30,
  97530 $44; Beta 97110 $42 (the only shared code); Medicare 30 / 28 / 40.
  Rate card: "Best-paying payer: Beta"; Leads and visit economics on the
  same store say Beta is the worse payer. Cause `mrfx/schedule.py:214-294`:
  median % of Medicare over each payer's OWN codes. Also rank 3 Delta ($39,
  80 % of best) above rank 4 Alpha ($45, 92 %); with no anchor "Median rate"
  is a median across different codes. Fix: rank on shared codes as
  `leads._worst_payer` / `economics.visit_economics` do; show own-code
  figures unranked.

### 2.7 (P1) Monthly packets never contain "Market position"
- Clients → "Build monthly packets" and `mrfx packets` pass no state, so
  every packet omits the pitch with "pitch report requires a state … pass
  market.allow_national=true" — printed, with config instructions, in the
  client folder's branded `index.html`; the packet's rate card pools peers
  nationally. Cause `mrfx/packets.py:140-151`, `:296-299`; `api.py:3110`;
  `app.js:2499`. Fix: per client default `market["state"]` to
  `anchor_state(store, {}, tins)` (the dossier's rule) and say so; no setup
  text in client-facing pages. Repro `consulting/p11_packets.py`.

### 2.8 (P1) The size panel prints a different position from every other screen
- One payer, three ranked codes (mid-ranks 0, 12.5, 100): every screen p13;
  the size panel "is at p37.5". Real ND practice: p33 elsewhere, 52.2 in the
  panel. Cause `mrfx/quality.py:262`, `:336` (mean of payer × code cells),
  `:373-374`, `:397-398`. Round 9's test had two codes (mean = median) — and
  the 0.7.3 notes claim it matches. Fix: print `overall_position` and keep the
  within-payer mean as a separately named band statistic. A mapped group's
  size is `max(npi_count)` of its TINs (`quality.py:368`): BIG GROUP "solo".

### 2.9 (P1, rare trigger) Seven surfaces still resolve the subject their own way
- Trigger: a bare name used by practices in two states (10 of 8,062 names on
  the real store). Fixture "ACME REHAB" MO ($40) + KS ($60), market MO:
  remit check flags a false $5 shortfall (`remits.py:157`); leverage centred
  on Wichita, "no alternative" (`leverage.py:78`); "Fill from Medicare"
  opportunity $98,500 vs $26,500 by hand (`utilization.py:363`, `:445`,
  `:520`); visit economics $535,000 vs $175,000 (`economics.py:90`);
  weighted position p10.7 vs p12.2 (`economics.py:250`); tracker bundle both
  TINs, "Wichita, KS" (`orgprofile.py:46`); floors compare
  (`floors.py:249-275`, read). The Negotiate tab's leverage and "What a visit
  is worth" panels don't send the tab's State / class / POS
  (`app.js:2883-2888`, `:2945-2949`) — a "Gamma $66.00" row for a payer the
  MO practice lacks; the Leads-tab dossier form sends no state or month
  (`app.js:2978-2979`). Fix: `subject_tins_for_market` + `subject_market`
  everywhere; the panels send the tab's market. Related (P2): with a state,
  leverage drops alternatives across the state line (`leverage.py:81-83`; 10
  vs 13 at 64105) though `dossier.py:136` says cross-border ones are real.

### 2.10 (P1) Printed documents lose columns
- Chromium PDF, Letter: rate card with 4 payers + anchor is 994 px in an
  816 px page — the fourth payer column is cut; the pitch with the anchor
  913 px, clipped; the "Your position" bar is a CSS background and does not
  print by default. Cause: print CSS is only `body { margin: 0 }`
  (`schedule.py:489-492`, `benchmark.py:2224`). Fix: `@page { size:
  landscape }` or blocks of 3 payers; `print-color-adjust: exact`. Repro
  `consulting/p13_print.py`.

## Group 3 — consistency between surfaces (P2)

- **3.1 Code page month table vs payer table** (`api.py:1762-1781`
  `/api/trend` takes a raw line median incl. institutional and NPI-in-slot
  rows; the payer table uses `practice_basis`): Moda $25.38 (1,754) vs
  $26.17 (1,699) for the same single file. Apply `practice_basis` and one
  rate per unit per month; show a payer only with ≥ 2 months.
  `rates/r_real_findings.py` §3.
- **3.2 The browser rounds half-cents its own way**: Explorer 92526 $70.70
  vs Markets $70.69 (raw 70.695); 10 of 2,796 By-code cells. `/api/summary`
  by_code, `/api/code`, `/api/trend`, the drawer and `/api/rates` send
  unrounded doubles; `fmtMoney` (`app.js:59-60`) rounds the shortest decimal.
  Round with `half_up(x, 2)` on the server. `rates/r_js_rounding.py`.
- **3.3 Rate changes twin rows**: two "Alpha · A2 REHAB · 97110 · GP" rows
  (the POS 02 and POS 11 lines); 60.1 % of professional groups on the real
  store have > 1 line. Carry `billing_class` and `service_code_set` into the
  rows, table and CSV (`monitor.py:251-266`, `:558-573`; `app.js:4044-4052`).
- **3.4 "69 states with data"**: the state lists hold BRITISH COLUMBIA,
  RIYADH, MASSACHUSETTS, AA/AE/AP … (`store.py:5166-5182`
  `_compute_states`; `overview.py:134` ignores the asked state). Normalise
  with `states.state_code_or_none` when names are saved; count the states in
  scope. Check whether State = MA misses practices stored as
  "MASSACHUSETTS" (not counted in the review).
- **3.5 Overview "Typical rate by discipline"** is a code-mix artefact (OT
  $95.45 = the four OT evaluation codes only; the 42 shared codes are
  "unspecified" and in neither) — `overview.py:62-72`. One comparable service
  per discipline, or remove.
- **3.6 Freshness**: a 0-row file counts as a payer's publication (BCBS ND
  "2026-10-14, 27 days" while its 2.66M rates are from 2026-08-25); a
  future-dated header prints "-21 days" (`quality.py:154-163`, `:171-173`).
  Use files with `rows_emitted > 0`; "dated in the future".
- **3.7 Sources tab vs tested sources**: Sources → WV says "unverified —
  search for current URL" for links the Files tab lists as verified and
  auto-queueable; four tested payers have no Sources card (`/api/sources`
  registry vs `/api/known-sources`). Merge them.
- **3.8 New to network** lists practices that were in the payer's book two
  months earlier when the middle month is partly loaded
  (`contracts.py:151-200`; `consulting/p10_new_to_network.py`).
- **3.9 "Market median"** includes the practice itself in Leads and outreach
  (`leads.py:129`, `:141`; `outreach.py:204`, `:264-266`) but not on
  Benchmark ($47.50 vs $48.00 on a 6-practice market). One rule.
- **3.10 Two "worst payer" definitions** under near-identical labels (Leads:
  dollars on shared codes; dossier: lowest position among that payer's
  practices — `dossier.py:244-258`). Say which in each label.
- **3.11 Dossier states no month or market** (`dossier.py:336-338`); the
  market report states the month, not the market in words (`:489-490`);
  "Across N comparable codes" counts all-tied codes the position excludes
  (`:233`).
- **3.12 A payer-dropped practice stays a peer and tops Leads** (H REHAB,
  absent from the payer's two newer files; documented limit in
  `benchmark._rates_relation`). Decide: drop a contract line absent from all
  of the payer's newer COMPLETE files, or flag it on Leads.
- **3.13 Pinned documents** list later months' files under "Source files"
  unqualified (rate card, fee.csv: `schedule.py:332`); `/api/trajectory`
  pinned to 2023-06 returns `months` including 2024-06 (the series are
  bounded). The export sidecar prints the header's month, not `file_month`
  (`api.py:995`, `:959`), and its `Filters:` line is raw JSON.

## Group 4 — small, each reproduced (P3), and the release notes

- `dossier.py:384` prints "-0.0%" (`'%+.1f%%'` — the one signed % outside
  `signed_pct`); the market report footer has no market in words and calls
  itself "this dossier" (`dossier.py:468-478`).
- Twice-rounded figures: weighted position 37.7 vs 37.5 (`economics.py:264`,
  `:282`, rounded per-code positions, all-tied codes counted); hospital
  median multiple 3.15× vs 3.14× (`hospital.py:680-689`); floors 172.8 % vs
  172.7 % (`floors.py:335-344`); referral "Avg wait" unweighted 11.3 vs 11.6
  (`medicare.py:614`).
- API-only 500s: `/api/contract-gaps` `min_peers: 1e308`;
  `/api/contracts/new-to-network` `radius_miles: "NaN"`; `/api/trajectory`
  and `/api/engagements/compare` with a non-object `market`
  (`engagements.py:181`) — `consulting/p5_fuzz.py`.
- Trajectory cache vs the price index: `inflation.save_index_values`
  (`inflation.py:153-158`) does not call `Store.note_user_edit()`; after
  adding a CPI year the cached trajectory keeps "No real-terms figures".
- Rate changes / trajectory results don't name the state, payers or
  discipline they were computed for; after a MO trajectory the heading
  still reads "all 3 loaded months"; a payer with no matched panel shows a
  flat "▬ –"; the State box's suggestions fill only after the Leads tab was
  opened (`index.html:974`, `list="ld-states"`).
- Wording: floors "…at 148.2%. and 90.9% of…" (`floors.py:352`); "0 of 2
  payer(s) demonstrably price practices differently — those are the
  negotiations with room"; `fee.csv` prints 40.0 not 40.00; the Overview index
  note says "Most-priced codes" under "Best-covered codes" (`app.js:344`,
  `:360`); Overview Refresh sends the request twice (`app.js:295-296`);
  Markets "BIRMINGHAM" (title-case regex no-op, `app.js:523`), "Only 0
  distance band"; Clients intro "newest month vs the one before"
  (`index.html:75`); a never-matched client name is accepted and then
  "no longer matches" (`clients.py:131`); the Code page with "base rates
  only" ranks hospital institutional lines first ($870, $794 …) — add a
  class control; the Explorer pager stays stale after an error; the TIN
  strip calls TINs "Entities"; a dollar-only row shows "$0.01–$40.00";
  `outreach.py:12` docstring.
- API shape: `/api/report/dossier.json` `median_percentile` became an
  integer (51) in 0.7.3 while every other surface returns 51.0 — pick one.
  The Leads CSV's new `worst_payer_codes` column sits before `website`; a
  mail-merge mapped by position shifts — move it to the end, or say so.
- **GETTING_STARTED "What changed in version 0.7.3" omits** (both reviewers
  noticed): the Rate changes State box also filters "Find changes"; positions
  already quoted can move by a point (roster from p50) and Leads can name a
  different worst payer; the leaderboard's code count changed; sparkline %
  round differently; the negotiability order; and its claim that the size
  premium matches every screen is false (2.8). Fix the 0.7.3 section's wrong
  claim in place; describe the rest under 0.7.4.

## Checked and found correct — do not re-chase

- Every Explorer filter, paging (summary total = table total over 24
  view × sort combinations; pages disjoint under tie-heavy sorts at all
  grains), the CSV equals the screen's rows in order; entity rate = median of
  member TINs; SSN masking; latest decided over the whole contract.
- Markets negotiability / differential / concentration (HHI) / radius bands;
  Code page payer table at all grains; Overview counts and per-code medians;
  Clients add / remove / digest; Rate changes "latest" pairs and the round-9
  trajectory (bound, chain-linked streak, State narrows the panel).
- Benchmark, opportunity, one-pager, payer-compare, proposal, % of Medicare,
  floors and hospital per-row figures, real terms, Leads gaps, market
  sizing, referral leaders — hand-worked.
- One overall position agrees on Leads, leaderboard, roster, Negotiate,
  one-pager, dossier on every fixture and on 25 real ND practices (only the
  size panel differs, 2.8). The round-9 subject rule holds on Benchmark,
  compare, rate card, fee.csv, gaps, size, subject-types, dossier, pitch,
  proposal for every spelling of a subject.
- Determinism: 75 + 88 endpoints byte-identical across calls and server
  restarts; two fresh fixture builds give identical bodies.
- Chromium: no page errors or unexpected console errors on any tab of either
  store; no "undefined" / "NaN" / "null" / raw JSON on screen; every
  download fires; refusals show the server's sentence.
- Subject autocomplete (17–86 ms), exact TIN / NPI lookups at the TIN grain
  (0.02–0.11 s; the rest of the wait is 1.2), the TIN drawer, and the front
  end's rendering (no main-thread task over 177 ms).
- Every Consulting / Growth endpoint under ~2 s quiet on the 32M store
  except the unfiltered outreach ZIP (3.2 s).

## Deliverables

1. One commit per group, message = what + why + verified-with-numbers,
   naming each test in `tests/mrfx/test_review10.py` and the value it failed
   with on 2bb81b8. Every speed change carries a before/after
   `snapshot.py` + `diff_snap.py` run showing the bodies identical.
2. The search timings re-measured on your synthetic store, quiet, in the
   Chromium driver (`search/ui_search.py`): slow-typed name, TIN, state;
   request-leaves-after-last-key; first screen after a start — before and
   after, in the status block.
3. Version 0.7.4; GETTING_STARTED "What changed" in the user's words (lead
   with "searching is faster"), the 0.7.3 section's size-premium claim
   corrected; HOW_TO_RUN if a button or message changed (the Explorer month
   box); AI_HANDOFF "Round 10 additions" with the rule each fix must keep
   (e.g. a superseded request is interrupted; every per-generation cache is
   keyed by `spine_key`; every surface uses `overall_position`, the subject
   rule and the house basis).
4. Full suite green, ruff 0, `tests/slow_tests.txt` regenerated, CLAUDE.md
   counts; a live Chromium pass of the Explorer search at both typing speeds
   and of each P0/P1 screen fixed.
5. `git archive --format=zip --prefix=Claudevideo/ -o <scratch>/mrfx_project.zip HEAD`
   sent to the user with the update steps (close the app → drag the zip onto
   "Update MRF Explorer.bat" → start → Ctrl+Shift+R at
   http://localhost:8377).
6. The status block at the top of this file: what closed, the numbers, what
   is left and why, what could not be run on Windows.
