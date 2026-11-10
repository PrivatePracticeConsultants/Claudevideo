# Search / lookup responsiveness — review 10

Reviewed: MRF Explorer 0.7.3, HEAD `2bb81b8`, nothing in the repository was edited.
Everything here lives under `/tmp/mrfx_review10/search/` (scripts in `repros/`, raw results in `out/`).

**What was measured.** A real Chromium typed into the real search boxes (per-key delays of
120 ms = a normal typist and 400 ms = a hunt-and-peck typist), recording every `/api` request and
every change of the result areas, on two stores:

| store | size | where |
|---|---|---|
| "real" | 32.1M rates, 20,101 TINs, 9 payers, 1.35M rows on the TIN spine | a private copy of `perf/named_vis/store` (deleted after the runs — re-copy it to re-run) |
| "scale" | 99M rates, 194,000 TINs, 15 payers, 79.0M rows on the TIN spine | `scale4n/store`, config `scale.yaml` (port 8701) |

Each UI pass (39 actions) was run four times per store on the shipped code — twice straight after a
server start ("cold") and twice again on the same server ("warm") — and each URL was also timed
alone, twice, on two fresh server starts (`repros/bodies.py`). 4 cores, 15 GB RAM.

**How far to trust the numbers.** Other reviewers shared the machine: the 1-minute load average
was 1.2–9.5 during my runs and is printed beside every timing (`L…`). The isolated per-URL timings
(load 3.6–5.2, two fresh starts each) agree with each other to within 0.3 s and are the reliable
figures. UI numbers taken at load ≥ 6 are inflated by up to 2x (e.g. the Code page: 1.0 s at load
5, 2.4–2.9 s at load 9). The fixed waits (580 ms, 300 ms, 200 ms) are timers and do not depend on
load. The pile-up durations (finding 1) vary 21–123 s between runs because they depend on how many
scans collide; every run showed the pile-up. Disk on this machine was nearly full (1.5–3.2 GB
free), which caps DuckDB's spill space — the two "needs more memory" failures noted below would be
a slower success on a machine with free disk.

---

## The short answer

On the user's common path there are two things, and neither is the database being slow at one
query:

1. **Nothing is sent for 0.58 s after the last key** — two wait-for-typing-to-stop timers run one
   after the other. On the 32M real store that timer is 60–90% of the whole wait (a TIN search:
   0.64 s after the last key, of which everything but the timer is 0.06 s).
2. **A search that is superseded is never stopped.** Type slowly (more than 0.3 s between keys) and
   every keystroke starts two whole-store scans that run to the end for nobody. On the 99M store,
   typing `practice 12` at 400 ms a key put the answer on screen **26–123 s** after the first key
   (4 runs), once as an error message; with the fix prototyped below: **5.9–6.9 s**, same answer.

After those, the cost is real query work on a big store: a name or state search reads the 79M-row
spine twice per statement in four statements (finding 3), and the first screen after a start
recomputes things that only change when data does (finding 4).

| # | finding | severity | measured (shipped code) | with the prototype |
|---|---|---|---|---|
| 1 | Superseded searches are never cancelled; slow typing piles them up | **P0** | scale: 26.6–122.6 s (name), 21.1–53.4 s (TIN), 1 of 4 ended in an error | 5.9–6.9 s, 3.6–3.7 s |
| 2 | 580 ms of dead time before any typed search is sent | **P0** | both stores: request leaves 581–588 ms after the last key | 301–309 ms |
| 3 | Name / state / partial-ID search: 2 spine scans x 4 statements, names joined before the sort | P1 | scale: rates 1.84–3.0 s + summary 2.15–3.4 s alone; 4.7–8.1 s after the last key in the browser | rates 0.99–1.27 s, summary 1.38–2.19 s; 2.5–3.0 s in the browser |
| 4 | First screen after a start: header counts block the table; strip recomputed every start | P1 | scale: table at 3.9–6.8 s, strip at 11.7–13.7 s | table at 3.6–4.0 s (header no longer in front); strip not fixed here |
| 5 | One- and two-letter searches cost more than no search at all | P1 | scale: rates 6.9–7.7 s, summary 11.7–12.8 s (unfiltered: 1.4 s / 8.6 s) | rates 2.4 s; summary 9.7 s |
| 6 | "Stats are busy — retrying…" for the search the user actually meant | P1 | every slow-typing run, both stores: 7–9 refusals per search (scale), 1–4 (real) | 0 |
| 7 | Entity grain with an ID in the box (32M store): 10–15 s; NPI grain 1.6–3.4 s | P1 | real store | not fixed here |
| 8 | Unfiltered table: 1.1–1.5 s every time, never remembered; sorted by name 4.3–5.5 s | P2 | scale | not fixed here |
| 9 | Table is blanked on every fired search; header poll starves during a pile-up; smaller items | P2 | — | — |

All prototype answers are byte-identical to the shipped code's: 57 of 57 response bodies on each
store (table at the end).

---

## Finding 1 — Superseded searches are never cancelled; slow typing piles them up  (P0)

**What the user sees.** They type a practice name at a careful pace. The table goes blank on the
first letter and says "Loading rates"; the strip flashes "Stats are busy — retrying…". They finish
typing. On a big store nothing appears for half a minute to two minutes, and once in four runs
what finally appeared was "Could not load rates — This view needs more memory than the current
limit". While it is stuck, every other screen is slow too (the header's own 15-second refresh took
up to 108 s).

**Measured** (`out/ui_scale_*.json`, scenario "type partial name @400ms/key" = `practice 12`, 11
keys, 4.1 s of typing; "type 9-digit TIN @400ms/key" = `100000000`):

| run | load | name: first key -> result | requests | summary refused (503) | rates failed (503 memory) | TIN: first key -> result |
|---|---|---|---|---|---|---|
| scale cold 1 | 5.3 | 122.6 s | 40 | 8 | 0 | 53.4 s |
| scale cold 2 | 2.2 | 76.5 s | 32 | 9 | 2 | 39.4 s |
| scale warm 1 | 4.5 | 78.4 s — **ended in the memory error** | 36 | 8 | 4 (incl. the final one) | 34.1 s |
| scale warm 2 | 4.0 | 26.6 s | 28 | 9 | 6 | 21.1 s |
| real (4 runs) | 2.3–4.5 | 4.8–5.1 s (0.68–0.74 s after the last key) | 24 | 1–4 | 0 | 3.9–4.1 s |

The same text typed at 120 ms a key (one search fired, after typing stops): 6.6–9.3 s on scale.
So the slow typist waits 4–20x longer than the fast one for the same answer. The real store does
not pile up (each scan takes 0.1–0.5 s), but it still fires 20–24 requests and blanks the table 10
times for one search.

A second proof without a browser (`repros/abandoned.py`, `out/abandoned_scale_base.log`): six name
searches sent and hung up on within 1.9 s kept the server busy for **40 s** — an exact-TIN search,
normally 0.06 s, took 0.4–1.35 s until t+40 s. During the UI pile-up DuckDB's spill folder grew to
0.6–0.7 GB.

**Root cause.**
- `mrfx/web/app.js:1034-1038` — each typed box fires after a 300 ms pause, so any gap between
  keys longer than that starts a search for the partial text (`q=p`, `q=pr`, …).
- `mrfx/web/app.js:804-816` (`loadRates`) and `:885-889` (`loadSummary`) — the "ticket"
  (`app.js:98-106`) only stops an old answer from being *painted*. The old request is not aborted
  in the browser (only the Code page does that, `app.js:1360`), and nothing tells the server.
- `mrfx/api.py:1360` (`rates`) and `:1406` (`summary`) are plain synchronous handlers: once
  started, the DuckDB statements run to completion even if the browser has hung up.
- The early partial texts are the *most* expensive searches there are (finding 5: one letter
  matches every practice — 6.9 s + 12 s alone), so the pile starts with the heaviest scans, on 4
  cores, against a 6 GB memory budget.

**Repro.** `repros/ui_search.py http://localhost:8701 <label> scale_cfg.json`; `repros/show.py
out/ui_scale_cold1.json "partial name @400"` prints the request timeline; `repros/abandoned.py
8701 100000000`.

**Fix (prototyped: `out/protoA_appjs.diff`, `out/protoA_api.diff`).**
1. Browser: one in-flight request per view. `loadRates`/`loadSummary` abort the previous fetch
   (`AbortController`) and send a header naming the page and view (`X-Mrfx-View: <page id>:rates`).
2. Server: a small registry keyed by that header. A newer request under the same key calls
   `con.interrupt()` on the older request's DuckDB connection (each request has its own —
   `store.py:1989`), and the older handler answers 409 "superseded". A flag is checked between a
   handler's statements so an interrupt that lands between two statements still stops the second.
   Requests without the header (CLI, scripts, tests) behave exactly as before.
3. The summary's single-flight lock waits (bounded) for the holder it has just interrupted instead
   of refusing (finding 6).

| run (scale) | load | name @400 ms/key | TIN @400 ms/key | refused / failed |
|---|---|---|---|---|
| shipped, 4 runs | 2.2–5.3 | 26.6–122.6 s | 21.1–53.4 s | 8–15 per search, one final error |
| prototype A, 3 runs (1 cold) | 1.7–3.3 | 6.7–6.9 s | 3.70–3.75 s | 0 |
| prototype B (A + finding 3), cold / warm | 3.6 / 3.1 | 5.9 s / 5.9 s | 3.68 s / 3.65 s | 0 |

(4.1 s and 3.3 s of those are the typing itself: the answer lands 1.8–2.8 s / 0.4 s after the
last key.) Identity: the final page and strip are the same requests as before; all 57 bodies
byte-identical (below). The reads are interrupted, never a write — the single-writer rule is
untouched.

---

## Finding 2 — 580 ms of dead time before any typed search is sent  (P0)

**What the user sees.** Every search in the Explorer — name, TIN, NPI, state, city, rate bounds,
modifier boxes — sits for over half a second after they stop typing before anything happens. On a
small or medium store this *is* the delay.

**Measured.** In all 8 shipped-code UI runs, on both stores, the request left the browser 581–588 ms
after the last key (`out/ui_requests_real.md`, `out/ui_requests_scale.md`, "sent … ms after last
key"). Real store, load 2.1–5.9:

| search (real store) | last key -> result, shipped (4 runs) | of which waiting to send | the rest (the two requests) | prototype (2 runs) |
|---|---|---|---|---|
| 9-digit TIN | 0.64–0.74 s | 0.58 s | 0.06–0.16 s | 0.36–0.37 s |
| 10-digit NPI | 0.63–0.70 s | 0.58 s | 0.05–0.12 s | 0.35 s |
| full practice name | 0.82–0.97 s | 0.58 s | 0.24–0.39 s | 0.43 s |
| partial name | 0.81–1.04 s | 0.58 s | 0.23–0.46 s | 0.43–0.46 s |
| clearing the box | 0.67–0.84 s | 0.58 s | 0.09–0.26 s | 0.36–0.43 s |
| state code | 1.01–1.26 s | 0.59 s | 0.42–0.67 s | 0.51–0.52 s |

**Root cause.** Two debounces in series: `app.js:1034-1038` waits 300 ms and then calls
`refreshFromFirstPage()`, which (`app.js:965-966`) is `refreshSoon = debounce(refresh, 280)` —
another 280 ms. The second timer exists for payer/code chips (three clicks in a row); a typed box
has already waited.

**Fix (prototyped, 3 lines, `out/protoA_appjs.diff`).** In `bind`, typed inputs set
`state.page = 1` and call `refresh()` directly; chips keep `refreshSoon`. Requests then leave
301–309 ms after the last key (all 5 prototype runs). No server change, no change to any answer.
(Checkboxes and dropdowns still take the 280 ms chip timer — a single click needs no wait either;
measured 0.34–0.55 s for a chip click of which 0.28 s is the timer.)

---

## Finding 3 — A name / state / partial-ID search reads the whole spine twice per statement, in four statements  (P1)

**What the user sees.** On a big store a name search answers in 5–8 s after they stop typing; a
state filter in 7–15 s (measured with the table sorted by name: 7.3–8.7 s at load 5, 15.1 s at
load 8).

**Measured, each statement alone** (`repros/explain_search.py` -> `out/explain_scale.log`, plans
in `out/plans/`; in-process, the exact SQL the API builds; load 6–9, so wall times are high — the
operator ranking is what matters), and the same requests timed alone over HTTP at load 4–5
(`out/req_table.md`):

| scale store | rates page | count for paging | strip | by-code rows | HTTP: `/api/rates` | HTTP: `/api/summary` |
|---|---|---|---|---|---|---|
| no search | 2.1–2.8 s | 0.11–0.13 s | 5.2–6.1 s | 7.6–8.7 s | 1.44 / 1.43 s | 8.61 / 8.45 s (then remembered) |
| full name (56 practices, 24,603 rows) | 2.2–2.4 s | 1.25–1.35 s | 1.46–1.50 s | 4.6–4.7 s | 1.84 / 1.86 s | 2.15 / 2.16 s |
| partial name (5,666 practices) | 4.2–4.8 s | 1.72 s | 2.7–3.0 s | 2.6–3.1 s | 2.24 / 2.30 s | 2.68 / 2.72 s |
| state MO (6.6M rows) | 4.0–4.1 s | 1.54–1.59 s | 1.8–2.2 s | 2.07 s | 3.00 / 3.15 s | 3.38 / 3.48 s |
| exact 9-digit TIN | 0.06 s | 0.04 s | 0.05 s | 0.05 s | 0.11 / 0.11 s | 0.10 / 0.10 s |

What EXPLAIN ANALYZE shows for the name search (thread-seconds; 4 threads):
- `HASH_GROUP_BY rows=194000` **2.2–3.2 s in every one of the four statements** — this is
  `SELECT DISTINCT tin_value, tin_is_really_npi FROM k` (`api.py:193`): the list of practices is
  rebuilt from the 79M-row spine each time, only to be filtered down to 56.
- then a second `SEQ_SCAN` of the spine (79.0M rows, 1.2–2.6 s) for the SEMI JOIN; no index exists
  on the spine (`duckdb_indexes()` is empty) and none is needed for an exact ID (zone maps on the
  `(billing_code, tin_value)`-sorted table answer it in 0.06 s) — but a name is not a spine column.
- on the table page, the three name/geo LEFT JOINs run over every matching row *before* the Top-N
  (2.49M rows for the partial name; 79M rows for a one-letter search: `HASH_JOIN LEFT` 6.0 s),
  because a search sets `uses_dim_cols` and that turns the late join off (`api.py:1262-1274`).
- so one search = 8 spine scans. `COUNT(*)` is about a third of `/api/rates` for a name search
  (1.25–1.72 s of 3.5–6.5 s) and 5% for the unfiltered table (0.11 s of 2.2 s). It is cached for
  30 s (`api.py:1260`), so paging and sorting do not pay it again.

**Root cause.** `_tin_rows_by_unit` (`api.py:174-198`) is already "practices first", but takes
the practice list from the spine itself; and the page/count/strip are computed on the joined
relation although none of them needs a joined column once the practices are known.

**Fix (prototyped: `out/protoB_api.diff`).**
- F4: the practice list is a 194,000-row file built once per spine generation with the existing
  `Store.spine_materialised` (0.58 s once, 1.1 MB, reused across restarts). It is a superset of
  `k`'s practices, so the SEMI JOIN keeps exactly the same rows.
- F7: for the practices-first branch at the TIN grain, the page is cut on the spine
  (`k SEMI JOIN u … ORDER BY … LIMIT`) and names are attached to the 100 kept rows afterwards —
  the same late join the unfiltered table already uses; the count and the strip read the same
  spine-only relation.

A/B, each URL alone, first call after a fresh start / asked again (`out/req_table.md`; shipped =
two fresh starts, load 4.1–5.2; prototype load 3.4–3.7):

| scale store | shipped `/api/rates` | prototype | shipped `/api/summary` | prototype | identical |
|---|---|---|---|---|---|
| full name | 1.84, 1.86 / 1.05 s | 0.99 / 0.69 s | 2.15, 2.16 s | 1.38 s | yes |
| partial name | 2.24, 2.30 / 1.48 s | 1.11 / 0.77 s | 2.68, 2.72 s | 1.78 s | yes |
| partial digits | 1.65, 1.75 / 1.06 s | 0.85 / 0.65 s | 2.02, 2.08 s | 1.42 s | yes |
| state | 3.00, 3.15 / 2.19 s | 1.27 / 0.95 s | 3.38, 3.48 s | 2.19 s | yes |
| state + city | 1.07, 1.08 / 0.57 s | 0.17 / 0.09 s | 1.01, 1.03 s | 0.16 s | yes |
| state + partial name | 1.93, 1.97 / 1.17 s | 1.01 / 0.67 s | 2.30, 2.24 s | 1.47 s | yes |
| one letter | 6.92, 7.15 / 5.46 s | 2.40 / 1.78 s | 12.15, 11.74 s | 9.70 s | yes |
| export.csv, full name | 2.21, 2.34 / 1.94 s | 1.48 / 1.18 s | — | — | yes |

In the browser (first key -> both the table and the strip painted; typing takes 1.5 / 1.2 s of it):

| scale store | shipped, 4 runs (load) | prototype B cold / warm (load) |
|---|---|---|
| full name @120 ms/key | 7.6 (4.3), 6.2 (2.2), 6.4 (5.0), 6.1 (4.1) s | 4.0 (4.2) / 4.1 (3.6) s |
| partial name @120 ms/key | 9.3 (4.2), 6.6 (2.4), 7.7 (4.6), 6.7 (4.2) s | 4.2 (3.9) / 4.2 (3.7) s |

SQL-level check of the practice-list step alone (`repros/exp_units.py`,
`out/exp_units_scale.log`, load 3.2): page 1.09–1.35 -> 0.68 s, count 0.69 -> 0.33 s, strip 0.69 ->
0.37 s, by-code 1.5 -> 1.16 s; rows equal in order for all 12 statement x search pairs.

What is left (not prototyped): the strip and its by-code rows still read the whole spine once each
(1.4–1.8 s together for a name). For a small practice set the spine can be read through zone
maps instead: `tin_value IN (56 literals)` reads the 24,603 rows in **0.14 s** vs 0.33 s for the
hash probe (`repros/exp_in.py`; note `OR` of equalities is 1.9 s and a UNION ALL 3.6 s — only the
IN list or a SEMI JOIN on a literal list is pushed down). That needs the practices resolved in a
first statement and inlined, for sets up to a few hundred.

---

## Finding 4 — First screen after a start: the table waits behind the header counts, and the strip is recomputed every start  (P1)

**What the user sees.** They start the program and open the dashboard: on a big store the table
appears after 4–7 s and the summary strip after 12–14 s. (The Overview tab already warns about
this; the Explorer, which is the default tab, does not.)

**Measured** (`repros/show.py out/ui_<run>.json "page load"`):

| page-load step (scale) | shipped cold 1 (load 4.0) | shipped cold 2 (load 1.8) | warm reloads (load 5.0 / 4.1) | real store, cold 1 / cold 2 |
|---|---|---|---|---|
| `/api/catalog` | 0.07 s | 0.02 s | 0.05 / 0.01 s | 0.03 / 0.02 s |
| `/api/stats` — **awaited before anything else** | 1.70 s | 1.20 s | 0.03 / 0.81 s (its 60 s memory had lapsed) | 0.15 / 0.26 s |
| `/api/payers` then `/api/months`, one after the other | 0.08 + 0.23 s | 0.06 + 0.14 s | 0.01 + 0.01 s | 0.01 + 0.02–0.03 s |
| first `/api/rates` leaves the browser at | 2.12 s | 1.47 s | 0.14 / 0.90 s | 0.33 / 0.42 s |
| `/api/rates` (unfiltered Top-100 of 79M rows) | 4.68 s | 2.44 s | 1.54 / 1.30 s | 0.15 / 0.30 s |
| `/api/summary` (unfiltered) | 11.55 s | 10.25 s | 0.10 / 0.02 s (remembered) | 0.37 / 0.72 s |
| table painted / strip painted | 6.8 s / 13.7 s | 3.9 s / 11.7 s | 1.6 / 2.2 s (table) | 0.48 / 0.70 s and 0.68 / 1.14 s |

The very first `/api/stats` of the session (minutes after this machine booted) took **38.0 s**; I
could not reproduce it — evicting the store's 3.5 GB from the page cache and restarting
(`repros/evict.py`, `repros/first_calls.sh`, `out/first_calls_scale_evicted_1.log`) gave 1.44 s.
Treat 38 s as one unexplained observation, 1.2–1.7 s as the reproducible figure.

**Root cause.**
- `app.js:4667-4683` (`boot`): `await api("/api/stats")` runs before `initFilters()` and the first
  `refresh()`. It is awaited only for `default_grain`; the counts it also computes
  (`store.py:4898` `store_stats`, `:4950` `enrichment_progress`) are whole-store work.
- `app.js:990` and `:1019`: payers and months are fetched one after the other.
- The unfiltered strip is kept per data generation but only in memory (`api.py:1398`,
  `:1425`), so every restart pays 8.5–11.5 s again. Its two statements (strip 5.2–6.1 s:
  `UNGROUPED_AGGREGATE` 9.0 thread-s for exact quantiles of 79M values; by-code 7.6–8.7 s:
  `HASH_GROUP_BY` 16.7 thread-s over 4.8M practice x code groups) run one after the other.
- The unfiltered table page is never remembered (finding 8).

**Fix.**
- Prototyped (`out/protoA_appjs.diff`, `/api/boot` in `out/protoA_api.diff`): the page asks a
  one-field `/api/boot` for the default grain (13 ms) and loads the header counts beside the
  table; payers and months together. First `/api/rates` leaves at **0.62–0.66 s** instead of
  1.47–2.12 s; table painted at 3.6–4.0 s (2 cold runs, load 1.7–3.8) vs 3.9–6.8 s. The `/api/rates`
  call itself got slower there (2.96–3.37 s vs 2.44 s) because it now shares the 4 cores with the
  header counts and the strip — the saving is the wait in front, not the query.
- Not prototyped, the bigger win: keep the unfiltered strip (and the first unfiltered page's base
  rows) beside the store, named by `Store.spine_key()` exactly like the materialised medians, and
  build it in the background after a rollup / at start. The strip is a pure function of the spine
  (the code's own comment at `api.py:1419-1424`), so a stored copy is the same numbers; the
  Medicare columns are already applied per request (`_with_anchor`). Expected: strip at ~0.7 s
  after a restart instead of 11.7–13.7 s.

---

## Finding 5 — One- and two-letter searches cost more than no search at all  (P1)

**Measured** (scale, alone, two fresh starts): `q=p` — `/api/rates` 6.92 / 7.15 s, `/api/summary`
12.15 / 11.74 s; `q=pr` — 7.71 / 7.18 s and 12.75 / 12.20 s. The unfiltered view: 1.44 s and
8.6 s (then remembered for the generation). Real store: 0.27–0.31 s vs 0.11 s.

**Root cause.** A short text matches (nearly) every practice — all 194,000 here — so the search
removes nothing, but it switches the table off the late-join path (names joined to 79M rows:
`HASH_JOIN LEFT` 6.0 s, `SEMI` 3.5 s) and switches the strip off its per-generation memory
(`api.py:1425`: `per_generation` is false once `uses_dim_cols` is set), so the 79M-value quantiles
are recomputed, and again after 30 s. These are also exactly the requests a slow typist fires
first (finding 1).

**Fix.** Prototype B already brings the table to 2.40 / 1.78 s (identical body). For the strip:
resolve the practices first (0.09–0.17 s on the practice list, measured in `exp_units.py`); when
the set is *every* practice the filter is a no-op, so serve the unfiltered answer from the
generation cache — the same rows, the same numbers. With finding 1 fixed these early requests are
interrupted after 0.4 s anyway; this matters when the user really does stop at two letters.

---

## Finding 6 — "Stats are busy — retrying…" for the search the user actually meant  (P1)

**What the user sees.** While they type, the strip is replaced by "Stats are busy — retrying…"
(3 times per slow-typed search in every run), and the by-code table disappears.

**Measured.** `/api/summary` answered 503 for 7–9 of a slow-typed search's 10–11 summary requests on
the scale store and 1–4 on the real store (every one of the 8 shipped-code runs). The page retries once,
1.4 s later (`app.js:929-935`), and only if the filter has not moved.

**Root cause.** The summary is single-flight (`api.py:1434-1441`): while the scan for an *older*
keystroke holds the lock, the newest one is refused. The lock protects the machine from stacked
scans; it has no notion of which request is still wanted.

**Fix (in prototype A).** With finding 1's interruption, the holder is the request that has just
been superseded: the newer one interrupts it and waits (bounded, 8 s) for the lock it is about to
release. 0 refusals in all 5 prototype runs on scale and both on the real store. A request without
the view header keeps today's refuse-when-busy behaviour.

---

## Finding 7 — Entity grain with an ID in the box, and the NPI grain, on a mid-size store  (P1)

**What the user sees** (32M real store; a 99M store refuses these views with a clear sentence
instead, `api.py:1332`): an NPI is in the search box, they click **Entity** — 10–15 s of "Loading
rates", then "No rates match". Clicking **NPI** takes 1.6–3.4 s.

**Measured** (real store): grain -> Entity with a 10-digit NPI in the box: 12.6, 13.7, 10.3, 15.1 s
(load 2.1–4.9; `/api/rates` 12.3 s and `/api/summary` 10.1 s side by side). Alone
(`out/real_entity_npi_grain.log`, load 3.5): entity grain, no filter 3.3–3.9 s; + NPI 2.7–4.0 s;
+ TIN 3.0–3.7 s; + partial name 2.7–4.4 s. NPI grain + NPI 0.25–1.1 s alone (1.6–3.4 s in the
browser with its strip). NPI grain with a name, or with nothing, ran 5.6–6.1 s and then failed
with the "needs more memory" message — on this machine's nearly full disk (spill capped at
1.8 GiB); with free disk expect a longer run rather than a failure.

**Root cause.** At the entity grain an exact ID is tested *after* the whole-store entity rollup
(`_ident_clause`, `api.py:355-362`: `list_contains(string_split(tin_value, '; '), ?)` over the
aggregated rows), and `rel_sql` keeps an exact ID off the practices-first branch
(`api.py:716`: `fs.has("unit") or not fs.ident_kind`). So every entity is built to find one. A
10-digit NPI can only match an NPI-in-the-TIN-slot at this grain, hence 0 rows after 10 s.

**Fix (not prototyped).** Resolve the ID to its entity first: `tin_value = ?` on the spine finds
the TIN's entity key in milliseconds (the TIN drawer does this in 0.06 s); then build only that
entity's member TINs. For an NPI, look it up in the rate rows' TIN (`rates … WHERE npi = ?`, the
NPI grain's 0.3 s query) and say "this NPI bills under TIN …" rather than "No rates match".

---

## Finding 8 — The unfiltered table is recomputed every time; sorted by name it takes 4–5 s  (P2)

**Measured** (scale). Unfiltered `/api/rates`: 1.44 / 1.43 s first, 1.08 / 1.06 s again, every
time — it is what "clear the search box" costs (1.7–2.5 s in the browser, 0.58 s of it finding
2), and it reruns on every return to a cleared view. `TOP_N` 3.9 thread-s + `SEQ_SCAN` 2.3
thread-s over 79M rows; the count is 0.11 s. Sorted by **Entity name**: 4.60 / 4.65 s (names
joined to all 79M rows before the sort: `HASH_JOIN LEFT` 6.5 thread-s); "Clear all filters" keeps
the sort, so with that sort left on, Clear takes 4.6–5.5 s (12.7 s at load 7.4).

**Root cause.** No cache on the page (`api.py:1360-1390`; only the count and the strip are
cached); `display_name` is a joined column so that sort cannot use the late join
(`api.py:1273`).

**Fix (not prototyped).** Remember the late-join page's *base* rows per (filter, sort, page, data
generation) — names are attached per request, so enrichment landing between generations still
shows. For the name sort: order the 194,000 practices by name first (the practice list of finding
3), then read only the first practices' rows.

---

## Finding 9 — Smaller things  (P2)

- **The table is emptied the moment a search fires** (`app.js:808`, `body.innerHTML = ""`), so
  for the whole wait the user looks at a blank table and "Loading rates"; with slow typing it
  blanks 10 times. Keeping the previous rows dimmed until the new ones arrive costs nothing.
- **The header poll starves during a pile-up.** `/api/stats` + `/api/states` every 15 s
  (`app.js:4683`): during the 122 s pile-up eight polls queued for 3–108 s each. Fixed by
  finding 1.
- **Code page.** It aborts its superseded request in the browser (`app.js:1360`) but the server
  finishes the scan (same handlers as finding 1) — cheap here (0.8–1.0 s on scale, 0.06 s real)
  because the code filter prunes the spine. `/api/code` and `/api/trend` already start together.
- **Subject autocomplete** (Benchmark / Negotiate / Rate card / Changes / Leads / Clients /
  Medicare) is fine: one request per 200 ms pause (`app.js:1484-1491`), 17–86 ms in the browser on
  either store (10–30 ms asked alone); a sequence number drops stale answers; nothing is aborted but there
  is nothing worth aborting. At 400 ms/key it fires on every key (11–13 requests for 11 keys).
  One gap: the box says "entity name, TIN, or NPI…" but a 10-digit NPI returns no suggestions
  (`api.py:3306`: matches name and TIN prefix only) — `…/subjects?q=1000000000` -> `{"entities":[],"tins":[]}`.
- **First result screen after picking a subject**: the main answer (`/api/benchmark/market`) is
  0.05–0.13 s on the real store (0.61 s once, first after a start) and 0.27–1.14 s on scale. The
  two panels under it then start together: `/api/quality/size` took 9.9 s and 2.1 s the first
  time after a start on scale (6–63 ms after that) and `/api/quality/subject-types` 0.76–2.5 s
  **every** time on scale (0.04–0.10 s real) — it is not remembered between calls. Worth a look,
  outside the search path proper.
- **Test artefact, not a product fault:** my "Code page: type state" step sent no request —
  Playwright's click landed on the 56-px box's drop-down arrow. Focusing the box and typing fires
  `/api/code` + `/api/trend` as designed (`repros/dbg_cptstate.py`); that row is void in the
  tables.

## What is not the problem

- **The browser.** No main-thread task over 177 ms in any of the 15 UI runs (13 of them: none over 82 ms); 100–250
  rows are painted in the same millisecond the response is read. Responses are 35–100 KB
  (177–206 KB at 250 rows).
- **Exact TIN / NPI lookups at the TIN grain:** 0.02–0.11 s per request on both stores. All of
  the 0.63–0.81 s the user waits is finding 2.
- **The row drawer:** 0.14–0.27 s real, 0.39–0.50 s scale (three statements; the member-NPI query
  on the raw rates is 0.19 s at 99M).
- **Paging, page size, sorting on a filtered view:** 0.07–0.19 s real, 0.30–0.42 s scale (1.1–1.4 s at
  load 7); the count is reused.
- **Code comparison:** 0.13–0.24 s real; 0.93–1.1 s scale (2.4–2.9 s at load 9).

## The questions, answered

| question | answer |
|---|---|
| Requests per keystroke / per search | Explorer: 2 per fire (`/api/rates` + `/api/summary`), in parallel. Keys < 300 ms apart: one fire after typing stops. Keys > 300 ms apart: one fire **per key** (20–40 requests per search measured). Autocomplete: 1 per 200 ms pause. |
| Is there a debounce | Yes — two in series for typed boxes: 300 ms + 280 ms (finding 2). Autocomplete 200 ms; Code-page state 300 ms. |
| Are stale requests cancelled / can old answers overwrite new | Old answers cannot overwrite (tickets; a sequence number for autocomplete). Nothing is cancelled except the Code page's fetch, and the server never stops a scan (finding 1). |
| Serial calls that could be parallel | Boot: catalog -> stats -> payers -> months -> first refresh (finding 4). Inside `/api/summary`: strip then by-code. Inside `/api/rates`: page then count. |
| Which SQL dominates | Unfiltered: exact quantiles and the practice x code medians of the strip (5–9 s). Name/state search: rebuilding the practice list from the spine (2.2–3.2 thread-s x 4) and the second spine scan. Short text: names joined to all rows (finding 5). |
| Does `q=` use an index or the directory first | Neither. An exact 9/10-digit ID is a zone-map-pruned spine predicate (0.06 s). Anything else lists practices from the spine itself, then scans the spine again (finding 3). No index exists. |
| Is `COUNT(*)` the slow part | No for the unfiltered table (0.11 s of ~2 s); about a third of `/api/rates` for a name search; cached 30 s by (grain, filter, generation). |
| First call after a start vs later | Table per request above. Big differences: unfiltered strip 8.5–11.5 s -> 0.00 s; `/api/stats` 1.4–1.7 s -> 0.004 s; a filtered strip 2–3.5 s -> 0.00 s for 30 s only. |
| What is cached, keyed how | Count: 30 s, (grain, where, params, hide_outliers, latest, data_generation). Strip: same key; per generation at the TIN grain with no name/geo filter, else 30 s. `Store.cached` / `generation_cached`: (name, args) per (data_generation, user_edit_generation), in memory. `spine_materialised`: Parquet beside the store named by `spine_key()`, survives restarts. Header counts and state list: 60 s. The rates page: nothing. |
| Recomputed per request that could be per generation | The practice list (finding 3), the unfiltered strip across restarts (finding 4), the unfiltered first page (finding 8), all-practice short-text searches (finding 5). |
| Does the front end re-render slowly | No (see above). |

## Prototype — what it is and how to re-run the A/B

Two scratch copies of the package, never the repository: `proto/` (A: findings 1, 2, 6, the boot
order, the practice list) and `proto2/` (B: A + the late join for practices-first views). Diffs
against HEAD: `out/protoA_appjs.diff` (83 lines), `out/protoA_api.diff` (165), `out/protoB_api.diff`
(263; `protoB_appjs.diff` equals A's).

```
cd /tmp/mrfx_review10/search
repros/serve.sh start scale                                   # shipped code, port 8701
python3 repros/bodies.py 8701 scale_base_r1 scale_cfg.json     # 57 URLs, bodies saved
repros/serve.sh stop scale
MRFX_SRC=$PWD/proto2 repros/serve.sh start scale               # the scratch copy
python3 repros/bodies.py 8701 scale_proto2 scale_cfg.json
/home/user/Claudevideo/.venv/bin/python repros/ui_search.py http://localhost:8701 scale_p2 scale_cfg.json
repros/serve.sh stop scale
python3 repros/ab.py scale_base_r1 scale_proto2                # byte comparison
python3 repros/req_table.py                                    # the tables below
```

Identity proof: the 57 URLs cover the unfiltered view, name / partial / one- and two-letter / TIN /
NPI / partial-digit searches, state, city, payer, code, latest, therapy-only, hide-outliers,
dollar-off, three sorts, page 2, page size 250, the NPI and entity grains, the Code page, trend,
both drawers, the subject list and a 4.6 MB CSV export. **57 of 57 bodies are byte-identical on the
scale store and 57 of 57 on the real store** (and the shipped code's own two runs agree with each
other on every URL). Prototype A alone was checked the same way on the scale store (57 of 57,
`out/ab_bodies_scale.md`).

Not covered by the prototype and to check before shipping it: the interruption registry under two
browser tabs (each has its own page id, so they do not interrupt each other — reasoned, not
tested); `hide_outliers` and name-sorted searches still take the old path by design; the 8 s lock
wait should be revisited with a rollup running; the practice-list file is skipped while rollups
are stale (`store.rollups_stale()`), which I did not exercise; the test suite was not run against
the scratch copies.

Side effects on the shared scale store: the shipped app itself replaced
`practice_medians_payer_v1.258d5191-0.parquet` with `…c170e9456cf42fce.parquet` (its normal
re-keying) when I ran a Benchmark; the prototype's `spine_units_v1.*.parquet` was removed; a 71 KB
`mrfx.duckdb.wal` is left. No data was ingested or deleted.

---

## Appendix A — every search-path request, cold and warm, both stores (each URL alone)

`repros/bodies.py` against the shipped code on two fresh server starts per store ("run 1 / run 2"), then against prototype B. 'again' for `/api/summary` is 0.00 s because the strip is remembered (for 30 s when a name/geo filter is on, for the data generation otherwise); 'again' for `/api/rates` reuses only the count. Load average during these runs: real 3.3–3.8, scale 3.4–5.2.

### real store — seconds; 'first' = first time that URL is asked after a server start, 'again' = asked again at once

| request | base first (run 1 / run 2) | base again (run 1 / run 2) | prototype first / again | body identical to base | load (run 1) |
|---|---|---|---|---|---|
| rates — default | 0.11 / 0.12 | 0.07 / 0.07 | 0.11 / 0.08 | yes | 3.75 |
| summary — default | 0.31 / 0.29 | 0.00 / 0.00 | 0.30 / 0.00 | yes | 3.75 |
| rates — q full name | 0.11 / 0.12 | 0.07 / 0.07 | 0.11 / 0.05 | yes | 3.75 |
| summary — q full name | 0.11 / 0.10 | 0.00 / 0.00 | 0.07 / 0.00 | yes | 3.75 |
| rates — q partial name | 0.11 / 0.14 | 0.07 / 0.08 | 0.09 / 0.06 | yes | 3.75 |
| summary — q partial name | 0.10 / 0.11 | 0.00 / 0.00 | 0.07 / 0.00 | yes | 3.75 |
| rates — q one letter | 0.27 / 0.31 | 0.17 / 0.20 | 0.13 / 0.11 | yes | 3.75 |
| summary — q one letter | 0.30 / 0.31 | 0.00 / 0.00 | 0.22 / 0.00 | yes | 3.75 |
| rates — q two letters | 0.16 / 0.18 | 0.11 / 0.11 | 0.13 / 0.08 | yes | 3.75 |
| summary — q two letters | 0.16 / 0.16 | 0.00 / 0.00 | 0.11 / 0.00 | yes | 3.75 |
| rates — q 9-digit TIN | 0.04 / 0.04 | 0.02 / 0.03 | 0.05 / 0.03 | yes | 3.75 |
| summary — q 9-digit TIN | 0.03 / 0.03 | 0.00 / 0.00 | 0.03 / 0.00 | yes | 3.75 |
| rates — q 10-digit NPI | 0.03 / 0.02 | 0.01 / 0.01 | 0.03 / 0.02 | yes | 3.75 |
| summary — q 10-digit NPI | 0.02 / 0.02 | 0.00 / 0.00 | 0.02 / 0.00 | yes | 3.75 |
| rates — q partial digits | 0.12 / 0.11 | 0.06 / 0.07 | 0.07 / 0.05 | yes | 3.75 |
| summary — q partial digits | 0.10 / 0.10 | 0.00 / 0.00 | 0.05 / 0.00 | yes | 3.75 |
| rates — state | 0.15 / 0.16 | 0.09 / 0.10 | 0.11 / 0.08 | yes | 3.75 |
| summary — state | 0.14 / 0.13 | 0.00 / 0.00 | 0.09 / 0.00 | yes | 3.75 |
| rates — state + partial name | 0.12 / 0.12 | 0.09 / 0.07 | 0.11 / 0.07 | yes | 3.75 |
| summary — state + partial name | 0.11 / 0.10 | 0.00 / 0.00 | 0.07 / 0.00 | yes | 3.75 |
| rates — cpt | 0.03 / 0.04 | 0.03 / 0.03 | 0.04 / 0.03 | yes | 3.75 |
| summary — cpt | 0.01 / 0.02 | 0.00 / 0.00 | 0.02 / 0.00 | yes | 3.75 |
| rates — cpt + partial name | 0.06 / 0.06 | 0.04 / 0.04 | 0.07 / 0.04 | yes | 3.75 |
| summary — cpt + partial name | 0.04 / 0.04 | 0.00 / 0.00 | 0.04 / 0.00 | yes | 3.75 |
| rates — payer + partial name | 0.09 / 0.07 | 0.04 / 0.04 | 0.09 / 0.05 | yes | 3.75 |
| summary — payer + partial name | 0.06 / 0.06 | 0.00 / 0.00 | 0.07 / 0.00 | yes | 3.75 |
| rates — latest + full name | 0.11 / 0.12 | 0.06 / 0.07 | 0.08 / 0.05 | yes | 3.75 |
| summary — latest + full name | 0.09 / 0.09 | 0.00 / 0.00 | 0.06 / 0.00 | yes | 3.75 |
| rates — therapy only + partial name | 0.12 / 0.12 | 0.07 / 0.07 | 0.10 / 0.07 | yes | 3.75 |
| summary — therapy only + partial name | 0.10 / 0.12 | 0.00 / 0.00 | 0.07 / 0.00 | yes | 3.75 |
| rates — hide outliers + full name | 0.11 / 0.12 | 0.06 / 0.06 | 0.10 / 0.06 | yes | 3.75 |
| summary — hide outliers + full name | 0.13 / 0.13 | 0.00 / 0.00 | 0.07 / 0.00 | yes | 3.75 |
| rates — dollar off + full name | 0.12 / 0.09 | 0.06 / 0.05 | 0.08 / 0.06 | yes | 3.75 |
| summary — dollar off + full name | 0.09 / 0.09 | 0.00 / 0.00 | 0.06 / 0.00 | yes | 3.77 |
| rates — city + state | 0.15 / 0.16 | 0.10 / 0.09 | 0.11 / 0.07 | yes | 3.77 |
| summary — city + state | 0.12 / 0.13 | 0.00 / 0.00 | 0.10 / 0.00 | yes | 3.77 |
| rates — NPI grain, NPI | 1.14 / 0.51 | 0.26 / 0.25 | 0.52 / 0.26 | yes | 3.77 |
| summary — NPI grain, NPI | 0.61 / 0.63 | 0.00 / 0.00 | 0.61 / 0.00 | yes | 3.77 |
| rates — NPI grain, TIN | 0.20 / 0.21 | 0.11 / 0.12 | 0.20 / 0.11 | yes | 3.77 |
| summary — NPI grain, TIN | 0.17 / 0.15 | 0.00 / 0.00 | 0.16 / 0.00 | yes | 3.77 |
| rates — entity grain payer+cpt+name | 0.04 / 0.03 | 0.03 / 0.02 | 0.04 / 0.02 | yes | 3.77 |
| summary — entity grain payer+cpt+name | 0.03 / 0.04 | 0.00 / 0.00 | 0.03 / 0.00 | yes | 3.77 |
| rates — entity grain payer+cpt+state | 0.04 / 0.04 | 0.02 / 0.03 | 0.04 / 0.02 | yes | 3.77 |
| summary — entity grain payer+cpt+state | 0.04 / 0.03 | 0.00 / 0.00 | 0.03 / 0.00 | yes | 3.77 |
| rates — q full name, page 2 | 0.07 / 0.07 | 0.07 / 0.07 | 0.04 / 0.04 | yes | 3.77 |
| rates — q full name, sort name | 0.07 / 0.07 | 0.06 / 0.06 | 0.05 / 0.05 | yes | 3.77 |
| rates — q partial, sort payer asc | 0.07 / 0.07 | 0.07 / 0.06 | 0.06 / 0.06 | yes | 3.77 |
| rates — default sort name | 0.23 / 0.20 | 0.21 / 0.20 | 0.21 / 0.21 | yes | 3.77 |
| code page | 0.07 / 0.06 | 0.07 / 0.06 | 0.06 / 0.06 | yes | 3.77 |
| code page + state | 0.07 / 0.06 | 0.07 / 0.06 | 0.06 / 0.06 | yes | 3.77 |
| trend | 0.01 / 0.01 | 0.01 / 0.01 | 0.01 / 0.01 | yes | 3.77 |
| drawer tin | 0.11 / 0.11 | 0.11 / 0.10 | 0.11 / 0.11 | yes | 3.77 |
| drawer npi | 0.47 / 0.46 | 0.47 / 0.47 | 0.44 / 0.43 | yes | 3.79 |
| subjects '' | 0.01 / 0.02 | 0.01 / 0.01 | 0.01 / 0.01 | yes | 3.79 |
| subjects partial | 0.01 / 0.01 | 0.01 / 0.01 | 0.01 / 0.01 | yes | 3.79 |
| subjects TIN | 0.01 / 0.01 | 0.01 / 0.01 | 0.01 / 0.01 | yes | 3.79 |
| export.csv full name | 0.16 / 0.17 | 0.12 / 0.11 | 0.13 / 0.09 | yes | 3.79 |

57 of 57 response bodies byte-identical between base and prototype (real store).

### scale store — seconds; 'first' = first time that URL is asked after a server start, 'again' = asked again at once

| request | base first (run 1 / run 2) | base again (run 1 / run 2) | prototype first / again | body identical to base | load (run 1) |
|---|---|---|---|---|---|
| rates — default | 1.44 / 1.43 | 1.08 / 1.06 | 1.40 / 1.09 | yes | 5.23 |
| summary — default | 8.61 / 8.45 | 0.00 / 0.00 | 9.12 / 0.00 | yes | 4.74 |
| rates — q full name | 1.84 / 1.86 | 1.05 / 1.08 | 0.99 / 0.69 | yes | 4.76 |
| summary — q full name | 2.15 / 2.16 | 0.00 / 0.00 | 1.38 / 0.00 | yes | 4.76 |
| rates — q partial name | 2.24 / 2.30 | 1.48 / 1.48 | 1.11 / 0.77 | yes | 4.70 |
| summary — q partial name | 2.68 / 2.72 | 0.00 / 0.00 | 1.78 / 0.00 | yes | 4.70 |
| rates — q one letter | 6.92 / 7.15 | 5.46 / 5.71 | 2.40 / 1.78 | yes | 4.62 |
| summary — q one letter | 12.15 / 11.74 | 0.00 / 0.00 | 9.70 / 0.00 | yes | 4.30 |
| rates — q two letters | 7.71 / 7.18 | 6.09 / 5.56 | 2.43 / 1.90 | yes | 4.23 |
| summary — q two letters | 12.75 / 12.20 | 0.00 / 0.00 | 9.68 / 0.00 | yes | 4.18 |
| rates — q 9-digit TIN | 0.11 / 0.11 | 0.07 / 0.06 | 0.11 / 0.06 | yes | 4.18 |
| summary — q 9-digit TIN | 0.10 / 0.10 | 0.01 / 0.00 | 0.10 / 0.00 | yes | 4.18 |
| rates — q 10-digit NPI | 0.06 / 0.06 | 0.04 / 0.04 | 0.07 / 0.04 | yes | 4.18 |
| summary — q 10-digit NPI | 0.06 / 0.06 | 0.00 / 0.00 | 0.06 / 0.00 | yes | 4.18 |
| rates — q partial digits | 1.65 / 1.75 | 1.06 / 1.10 | 0.85 / 0.65 | yes | 4.18 |
| summary — q partial digits | 2.02 / 2.08 | 0.00 / 0.00 | 1.42 / 0.00 | yes | 4.17 |
| rates — state | 3.00 / 3.15 | 2.19 / 2.25 | 1.27 / 0.95 | yes | 4.15 |
| summary — state | 3.38 / 3.48 | 0.00 / 0.00 | 2.19 / 0.00 | yes | 4.15 |
| rates — state + partial name | 1.93 / 1.97 | 1.17 / 1.20 | 1.01 / 0.67 | yes | 4.14 |
| summary — state + partial name | 2.30 / 2.24 | 0.00 / 0.00 | 1.47 / 0.00 | yes | 4.14 |
| rates — cpt | 0.11 / 0.11 | 0.10 / 0.10 | 0.11 / 0.10 | yes | 4.14 |
| summary — cpt | 0.36 / 0.38 | 0.00 / 0.00 | 0.35 / 0.00 | yes | 4.14 |
| rates — cpt + partial name | 0.47 / 0.52 | 0.32 / 0.33 | 0.47 / 0.29 | yes | 4.13 |
| summary — cpt + partial name | 0.38 / 0.38 | 0.00 / 0.00 | 0.36 / 0.00 | yes | 4.13 |
| rates — payer + partial name | 1.04 / 1.04 | 0.62 / 0.61 | 0.63 / 0.37 | yes | 4.13 |
| summary — payer + partial name | 0.94 / 0.91 | 0.00 / 0.00 | 0.55 / 0.00 | yes | 4.13 |
| rates — latest + full name | 1.93 / 1.84 | 1.13 / 1.09 | 0.98 / 0.68 | yes | 4.12 |
| summary — latest + full name | 1.94 / 1.95 | 0.00 / 0.00 | 1.22 / 0.00 | yes | 4.12 |
| rates — therapy only + partial name | 2.37 / 2.26 | 1.51 / 1.48 | 1.10 / 0.81 | yes | 4.11 |
| summary — therapy only + partial name | 2.82 / 2.68 | 0.00 / 0.00 | 1.74 / 0.00 | yes | 4.10 |
| rates — hide outliers + full name | 2.16 / 2.22 | 1.08 / 1.12 | 1.50 / 0.71 | yes | 4.10 |
| summary — hide outliers + full name | 2.64 / 2.54 | 0.00 / 0.00 | 1.83 / 0.00 | yes | 4.09 |
| rates — dollar off + full name | 1.75 / 1.75 | 1.09 / 1.07 | 0.93 / 0.65 | yes | 4.09 |
| summary — dollar off + full name | 2.20 / 2.25 | 0.00 / 0.00 | 1.35 / 0.00 | yes | 4.08 |
| rates — city + state | 1.07 / 1.08 | 0.57 / 0.57 | 0.17 / 0.09 | yes | 4.08 |
| summary — city + state | 1.01 / 1.03 | 0.00 / 0.00 | 0.16 / 0.00 | yes | 4.08 |
| rates — NPI grain, NPI | 0.10 / 0.11 | 0.05 / 0.06 | 0.11 / 0.06 | yes | 4.08 |
| summary — NPI grain, NPI | 0.13 / 0.15 | 0.00 / 0.00 | 0.16 / 0.00 | yes | 4.08 |
| rates — NPI grain, TIN | 0.39 / 0.42 | 0.22 / 0.24 | 0.44 / 0.24 | yes | 4.08 |
| summary — NPI grain, TIN | 0.34 / 0.36 | 0.00 / 0.00 | 0.38 / 0.00 | yes | 4.08 |
| rates — entity grain payer+cpt+name | 0.85 / 0.65 | 0.48 / 0.41 | 0.86 / 0.50 | yes | 4.08 |
| summary — entity grain payer+cpt+name | 0.61 / 0.50 | 0.00 / 0.00 | 0.63 / 0.00 | yes | 4.08 |
| rates — entity grain payer+cpt+state | 0.30 / 0.28 | 0.18 / 0.18 | 0.30 / 0.18 | yes | 4.08 |
| summary — entity grain payer+cpt+state | 0.25 / 0.25 | 0.00 / 0.00 | 0.26 / 0.00 | yes | 4.08 |
| rates — q full name, page 2 | 1.83 / 1.80 | 1.12 / 1.15 | 0.96 / 0.65 | yes | 4.07 |
| rates — q full name, sort name | 1.13 / 1.14 | 1.16 / 1.11 | 0.74 / 0.72 | yes | 4.07 |
| rates — q partial, sort payer asc | 2.37 / 2.31 | 1.46 / 1.50 | 1.20 / 0.93 | yes | 4.06 |
| rates — default sort name | 4.60 / 4.65 | 4.34 / 4.66 | 4.55 / 4.33 | yes | 4.05 |
| code page | 0.84 / 0.96 | 0.91 / 0.90 | 0.78 / 0.84 | yes | 4.05 |
| code page + state | 0.48 / 0.48 | 0.45 / 0.49 | 0.43 / 0.43 | yes | 4.05 |
| trend | 0.15 / 0.17 | 0.15 / 0.17 | 0.14 / 0.14 | yes | 4.05 |
| drawer tin | 0.32 / 0.34 | 0.29 / 0.30 | 0.29 / 0.32 | yes | 4.05 |
| drawer npi | 0.09 / 0.12 | 0.09 / 0.12 | 0.11 / 0.11 | yes | 4.05 |
| subjects '' | 0.02 / 0.03 | 0.02 / 0.02 | 0.03 / 0.02 | yes | 4.05 |
| subjects partial | 0.02 / 0.03 | 0.02 / 0.03 | 0.02 / 0.02 | yes | 4.05 |
| subjects TIN | 0.02 / 0.02 | 0.02 / 0.02 | 0.02 / 0.02 | yes | 4.05 |
| export.csv full name | 2.21 / 2.34 | 1.94 / 1.97 | 1.48 / 1.18 | yes | 4.05 |

57 of 57 response bodies byte-identical between base and prototype (scale store).

## Appendix B — browser runs, first action -> last result painted

Each cell: seconds from the first key or click to the last change of the result area (in brackets: from the LAST key); requests fired; refusals; `L` = 1-minute load average when the step began. Typing time is included in the first figure (full name: 4.1 s real / 1.5 s scale; partial name 1.2 s; slow typist 4.1 s; TIN 1.0 s / 3.3 s slow; NPI 1.1 s). cold = first pass after a server start, warm = second pass on the same server. `proto_cold`, `proto1`, `proto2` = prototype A; `p2_cold`, `p2_warm` = prototype B. "refused: store is large" is the product's own guard for the Entity/NPI views on a store over 40M rates.

### real store (32M rates)

| scenario | cold1 | cold2 | warm1 | warm2 | p2_cold | p2_warm |
|---|---|---|---|---|---|---|
| page load (Explorer default) | 0.68 s (0.68); 11 req; L2.06 | 1.14 s (1.14); 11 req; L4.45 | 0.43 s (0.43); 11 req; L4.35 | 0.38 s (0.38); 11 req; L4.94 | 0.62 s (0.62); 12 req; L3.27 | 0.28 s (0.28); 12 req; L1.70 |
| type full name @120ms/key | 4.93 s (0.82); 2 req; L2.61 | 5.15 s (0.97); 2 req; L4.50 | 5.08 s (0.87); 2 req; L4.40 | 4.97 s (0.84); 2 req; L4.94 | 4.53 s (0.43); 2 req; L3.27 | 4.51 s (0.43); 2 req; L1.64 |
| clear search box | 0.71 s (0.70); 2 req; L2.48 | 0.71 s (0.70); 2 req; L4.46 | 0.74 s (0.72); 2 req; L4.13 | 0.77 s (0.75); 2 req; L4.70 | 0.42 s (0.41); 2 req; L2.84 | 0.44 s (0.42); 2 req; L1.51 |
| type partial name @120ms/key | 2.04 s (0.81); 2 req; L2.48 | 2.33 s (1.04); 2 req; L4.46 | 2.21 s (0.92); 2 req; L4.13 | 2.19 s (0.95); 2 req; L4.49 | 1.67 s (0.43); 2 req; L2.84 | 1.70 s (0.46); 2 req; L1.51 |
| clear search box (2) | 0.71 s (0.70); 2 req; L2.28 | 0.78 s (0.78); 2 req; L4.50 | 0.79 s (0.77); 2 req; L4.12 | 0.76 s (0.74); 2 req; L4.49 | 0.40 s (0.39); 2 req; L2.84 | 0.40 s (0.40); 2 req; L1.39 |
| type partial name @400ms/key (slow typist) | 4.79 s (0.68); 24 req, 1 refused; L2.28 | 5.09 s (0.74); 24 req, 4 refused; L4.50 | 4.85 s (0.74); 24 req, 1 refused; L4.12 | 4.80 s (0.71); 24 req, 2 refused; L4.21 | 4.50 s (0.41); 24 req; L2.69 | 4.48 s (0.37); 24 req; L1.39 |
| clear search box (3) | 0.68 s (0.67); 2 req; L2.50 | 0.77 s (0.77); 2 req; L4.94 | 0.80 s (0.78); 2 req; L4.51 | 0.77 s (0.75); 2 req; L4.27 | 0.41 s (0.40); 2 req; L2.56 | 0.41 s (0.40); 2 req; L1.28 |
| type 9-digit TIN @120ms/key | 1.63 s (0.64); 2 req; L2.30 | 1.80 s (0.74); 2 req; L4.87 | 1.77 s (0.70); 2 req; L4.55 | 1.68 s (0.68); 2 req; L4.27 | 1.35 s (0.36); 2 req; L2.56 | 1.35 s (0.37); 2 req; L1.28 |
| clear search box (4) | 0.71 s (0.70); 2 req; L2.30 | 0.75 s (0.74); 2 req; L4.87 | 0.72 s (0.71); 2 req; L4.55 | 0.71 s (0.70); 2 req; L4.09 | 0.42 s (0.42); 2 req; L2.35 | 0.43 s (0.42); 2 req; L1.17 |
| type 9-digit TIN @400ms/key (slow typist) | 3.88 s (0.62); 20 req, 1 refused; L2.28 | 3.97 s (0.66); 20 req, 4 refused; L4.96 | 3.97 s (0.66); 20 req, 1 refused; L4.50 | 4.05 s (0.71); 20 req, 2 refused; L4.09 | 3.65 s (0.34); 18 req; L2.35 | 3.65 s (0.36); 18 req; L1.17 |
| click first row -> drawer | 0.14 s (0.14); 1 req; L2.09 | 0.26 s (0.26); 1 req; L4.88 | 0.19 s (0.19); 1 req; L4.46 | 0.27 s (0.27); 1 req; L4.64 | 0.17 s (0.17); 1 req; L2.16 | 0.19 s (0.19); 1 req; L1.48 |
| clear search box (5) | 0.71 s (0.71); 2 req; L2.09 | 0.76 s (0.75); 2 req; L4.88 | 0.70 s (0.69); 2 req; L4.46 | 0.77 s (0.75); 2 req; L4.75 | 0.40 s (0.39); 4 req; L2.16 | 0.42 s (0.41); 4 req; L1.48 |
| type 10-digit NPI @120ms/key (TIN grain) | 1.74 s (0.63); 2 req; L2.09 | 1.84 s (0.70); 2 req; L4.88 | 1.74 s (0.63); 2 req; L4.46 | 1.88 s (0.69); 2 req; L4.75 | 1.46 s (0.35); 2 req; L2.16 | 1.46 s (0.35); 2 req; L1.36 |
| switch grain -> NPI (NPI in box) | 2.44 s (2.44); 2 req; L1.93 | 2.45 s (2.45); 2 req; L4.81 | 1.64 s (1.64); 2 req; L4.10 | 3.42 s (3.42); 2 req; L4.75 | 1.51 s (1.51); 2 req; L1.99 | 1.54 s (1.54); 2 req; L1.36 |
| switch grain -> Entity (NPI in box) | 12.57 s (12.57); 4 req; L2.09 | 13.72 s (13.72); 4 req; L4.74 | 10.30 s (10.30); 4 req; L4.10 | 15.05 s (15.05); 4 req; L4.85 | 7.50 s (7.50); 2 req; L1.99 | 7.25 s (7.25); 2 req; L1.25 |
| switch grain -> TIN (NPI in box) | 0.32 s (0.32); 2 req; L3.08 | 0.33 s (0.33); 2 req; L5.37 | 0.32 s (0.32); 2 req; L4.19 | 0.33 s (0.33); 2 req; L5.75 | 0.31 s (0.31); 4 req; L2.53 | 0.31 s (0.31); 4 req; L1.55 |
| clear search box (6) | 0.73 s (0.72); 2 req; L3.31 | 0.81 s (0.80); 2 req; L5.37 | 0.72 s (0.71); 2 req; L4.19 | 0.85 s (0.84); 4 req; L5.75 | 0.44 s (0.43); 2 req; L2.53 | 0.42 s (0.42); 2 req; L1.43 |
| click CPT chip 97110 | 0.41 s (0.41); 2 req; L3.31 | 0.55 s (0.54); 4 req; L5.37 | 0.37 s (0.37); 2 req; L4.19 | 0.39 s (0.39); 2 req; L5.75 | 0.38 s (0.38); 2 req; L2.32 | 0.34 s (0.34); 2 req; L1.43 |
| CPT on: type partial name @120ms/key | 2.04 s (0.76); 4 req; L3.31 | 2.18 s (0.79); 2 req; L5.37 | 2.12 s (0.78); 2 req; L4.19 | 2.03 s (0.72); 2 req; L5.75 | 1.65 s (0.41); 2 req; L2.32 | 1.64 s (0.39); 2 req; L1.43 |
| CPT on + name: grain -> Entity | 0.65 s (0.65); 2 req; L3.37 | 0.64 s (0.64); 2 req; L5.74 | 0.60 s (0.60); 2 req; L4.50 | 0.73 s (0.73); 2 req; L5.69 | 0.52 s (0.52); 2 req; L2.32 | 0.49 s (0.49); 2 req; L1.31 |
| CPT on + name: grain -> NPI | 1.43 s (1.43); 2 req; L3.37 | 1.54 s (1.54); 2 req; L5.74 | 1.14 s (1.14); 4 req; L4.50 | 1.87 s (1.87); 2 req; L5.69 | 0.92 s (0.92); 2 req; L2.14 | 0.97 s (0.97); 2 req; L1.31 |
| CPT on + name: grain -> TIN | 0.36 s (0.36); 2 req; L3.42 | 0.40 s (0.40); 2 req; L5.68 | 0.35 s (0.35); 2 req; L4.70 | 0.36 s (0.36); 2 req; L5.95 | 0.36 s (0.36); 2 req; L2.14 | 0.34 s (0.34); 2 req; L1.31 |
| clear search box (7) | 0.69 s (0.68); 2 req; L3.42 | 0.71 s (0.70); 2 req; L5.68 | 0.66 s (0.65); 2 req; L4.70 | 0.70 s (0.69); 2 req; L5.95 | 0.37 s (0.36); 2 req; L2.14 | 0.37 s (0.36); 2 req; L1.21 |
| sort by Entity name (CPT on) | 0.14 s (0.14); 1 req; L3.42 | 0.11 s (0.11); 1 req; L5.62 | 0.08 s (0.08); 1 req; L4.70 | 0.16 s (0.16); 1 req; L5.87 | 0.07 s (0.07); 1 req; L1.97 | 0.08 s (0.07); 1 req; L1.21 |
| next page (CPT on) | 0.13 s (0.13); 1 req; L3.42 | 0.19 s (0.19); 1 req; L5.62 | 0.10 s (0.10); 1 req; L4.40 | 0.11 s (0.11); 3 req; L5.87 | 0.07 s (0.07); 1 req; L1.97 | 0.08 s (0.08); 1 req; L1.21 |
| page size 250 (CPT on) | 0.12 s (0.12); 1 req; L3.54 | 0.16 s (0.16); 3 req; L5.62 | 0.11 s (0.11); 1 req; L4.40 | 0.16 s (0.16); 1 req; L5.87 | 0.10 s (0.10); 3 req; L1.97 | 0.10 s (0.10); 3 req; L1.21 |
| Clear all filters | 0.77 s (0.77); 2 req; L3.54 | 0.92 s (0.92); 2 req; L5.62 | 0.62 s (0.62); 2 req; L4.40 | 0.78 s (0.78); 2 req; L5.87 | 0.56 s (0.56); 2 req; L1.97 | 0.55 s (0.55); 2 req; L1.11 |
| type state code @120ms/key | 1.20 s (1.03); 4 req; L3.54 | 1.42 s (1.26); 2 req; L5.81 | 1.16 s (1.01); 2 req; L4.40 | 1.25 s (1.09); 2 req; L5.97 | 0.66 s (0.52); 2 req; L2.37 | 0.65 s (0.51); 2 req; L1.11 |
| Clear all filters (2) | 0.74 s (0.73); 2 req; L3.74 | 0.94 s (0.94); 2 req; L5.81 | 0.97 s (0.96); 2 req; L4.21 | 0.67 s (0.66); 2 req; L5.97 | 0.53 s (0.53); 2 req; L2.37 | 0.54 s (0.54); 2 req; L1.11 |
| open Code comparison (auto first code) | 0.16 s (0.16); 2 req; L3.74 | 0.15 s (0.15); 2 req; L6.23 | 0.22 s (0.22); 4 req; L4.21 | 0.22 s (0.22); 2 req; L5.57 | 0.11 s (0.11); 2 req; L2.37 | 0.11 s (0.11); 2 req; L1.02 |
| Code page: click 97110 | 0.17 s (0.17); 2 req; L3.74 | 0.17 s (0.17); 2 req; L6.23 | 0.13 s (0.13); 2 req; L3.95 | 0.14 s (0.14); 2 req; L5.57 | 0.15 s (0.15); 2 req; L2.18 | 0.12 s (0.12); 2 req; L1.02 |
| Code page: click 97530 | 0.24 s (0.24); 2 req; L3.76 | 0.17 s (0.17); 2 req; L6.23 | 0.15 s (0.15); 2 req; L3.95 | 0.16 s (0.16); 2 req; L5.57 | 0.15 s (0.15); 2 req; L2.18 | 0.15 s (0.15); 2 req; L1.02 |
| open Benchmark tab | 0.07 s (0.07); 6 req; L3.76 | 0.07 s (0.07); 6 req; L6.05 | 0.04 s (0.04); 6 req; L3.95 | 0.04 s (0.04); 6 req; L5.57 | 0.04 s (0.04); 6 req; L2.18 | 0.04 s (0.04); 6 req; L0.94 |
| Benchmark: type subject name @120ms/key | 5.87 s (0.22); 3 req; L3.76 | 5.79 s (0.22); 3 req; L6.05 | 5.83 s (0.23); 1 req; L3.95 | 5.71 s (0.22); 3 req; L5.20 | 5.68 s (0.22); 3 req; L2.18 | 5.67 s (0.22); 3 req; L0.94 |
| Benchmark: clear + type partial @400ms/key | 4.33 s (0.22); 11 req; L3.95 | 4.29 s (0.22); 11 req; L5.65 | 4.30 s (0.23); 13 req; L4.35 | 4.27 s (0.22); 11 req; L4.86 | 4.27 s (0.22); 11 req; L1.84 | 4.27 s (0.22); 11 req; L0.86 |
| Benchmark: Run (first result screen) | 0.67 s (0.59); 4 req; L3.95 | 0.23 s (0.18); 4 req; L5.19 | 0.21 s (0.15); 4 req; L4.41 | 0.23 s (0.17); 4 req; L4.64 | 0.21 s (0.17); 4 req; L1.70 | 0.20 s (0.15); 4 req; L0.79 |
| Benchmark: Run again | 0.06 s (0.06); 3 req; L3.95 | 0.06 s (0.06); 3 req; L4.94 | 0.06 s (0.06); 3 req; L4.41 | 0.06 s (0.06); 3 req; L4.64 | 0.04 s (0.04); 3 req; L1.70 | 0.04 s (0.04); 3 req; L0.79 |

### scale store (99M rates)

| scenario | cold1 | cold2 | warm1 | warm2 | proto_cold | proto1 | proto2 | p2_cold | p2_warm |
|---|---|---|---|---|---|---|---|---|---|
| page load (Explorer default) | 13.68 s (13.68); 11 req; L3.95 | 11.71 s (11.71); 11 req; L1.80 | 1.65 s (1.65); 11 req; L4.98 | 2.17 s (2.17); 11 req; L4.11 | 10.83 s (10.83); 12 req; L1.72 | 2.32 s (2.32); 12 req; L3.44 | 1.39 s (1.39); 12 req; L1.87 | 10.72 s (10.72); 12 req; L3.83 | 1.35 s (1.35); 12 req; L3.37 |
| type full name @120ms/key | 7.61 s (6.11); 4 req; L4.29 | 6.21 s (4.70); 4 req; L2.21 | 6.43 s (4.94); 2 req; L4.98 | 6.13 s (4.66); 2 req; L4.10 | 4.33 s (2.83); 4 req; L1.77 | 4.39 s (2.91); 2 req; L3.44 | 4.51 s (3.02); 2 req; L1.80 | 4.03 s (2.52); 4 req; L4.15 | 4.06 s (2.56); 2 req; L3.58 |
| clear search box | 2.15 s (2.14); 2 req; L4.51 | 1.70 s (1.69); 2 req; L2.35 | 2.06 s (2.05); 2 req; L4.62 | 1.69 s (1.68); 2 req; L4.18 | 1.43 s (1.42); 2 req; L2.03 | 1.57 s (1.55); 2 req; L3.06 | 1.63 s (1.61); 2 req; L2.06 | 1.45 s (1.44); 2 req; L4.22 | 1.48 s (1.46); 2 req; L3.70 |
| type partial name @120ms/key | 9.33 s (8.07); 4 req; L4.23 | 6.64 s (5.41); 2 req; L2.35 | 7.67 s (6.37); 4 req; L4.62 | 6.74 s (5.52); 4 req; L4.16 | 4.91 s (3.67); 2 req; L2.03 | 4.95 s (3.71); 4 req; L3.06 | 5.13 s (3.89); 4 req; L2.06 | 4.25 s (3.02); 2 req; L3.88 | 4.20 s (2.96); 2 req; L3.70 |
| clear search box (2) | 2.46 s (2.46); 2 req; L5.28 | 1.69 s (1.69); 4 req; L2.39 | 2.03 s (2.02); 2 req; L4.60 | 1.75 s (1.73); 2 req; L4.31 | 1.49 s (1.47); 2 req; L1.79 | 1.50 s (1.49); 2 req; L3.22 | 1.47 s (1.46); 2 req; L1.74 | 1.40 s (1.39); 2 req; L3.57 | 1.49 s (1.48); 4 req; L3.40 |
| type partial name @400ms/key (slow typist) | 122.64 s (118.50); 40 req, 8 refused; L5.34 | 76.46 s (72.36); 32 req, 11 refused; L2.20 | 78.42 s (74.33); 36 req, 14 refused **ended in an error**; L4.47 | 26.57 s (22.48); 28 req, 15 refused; L3.96 | 6.71 s (2.59); 24 req; L1.79 | 6.78 s (2.72); 22 req; L3.28 | 6.85 s (2.77); 22 req; L1.74 | 5.88 s (1.79); 24 req; L3.57 | 5.85 s (1.78); 22 req; L3.13 |
| clear search box (3) | 2.68 s (2.66); 2 req; L9.51 | 1.80 s (1.79); 2 req; L6.53 | 3.21 s (3.21); 2 req; L8.71 | 1.80 s (1.79); 4 req; L5.89 | 2.45 s (2.44); 2 req; L2.28 | 2.41 s (2.40); 4 req; L3.50 | 2.73 s (2.71); 4 req; L2.31 | 2.25 s (2.24); 2 req; L3.68 | 2.29 s (2.29); 2 req; L3.28 |
| type 9-digit TIN @120ms/key | 1.82 s (0.81); 4 req; L9.39 | 1.76 s (0.78); 4 req; L6.33 | 2.09 s (1.03); 2 req; L8.73 | 1.81 s (0.83); 2 req; L5.42 | 1.53 s (0.54); 2 req; L2.28 | 1.51 s (0.52); 2 req; L3.62 | 1.53 s (0.54); 2 req; L2.12 | 1.51 s (0.52); 2 req; L3.71 | 1.50 s (0.52); 4 req; L3.41 |
| clear search box (4) | 1.85 s (1.84); 2 req; L8.80 | 1.69 s (1.68); 2 req; L6.33 | 2.87 s (2.86); 4 req; L8.51 | 1.67 s (1.67); 2 req; L5.42 | 1.47 s (1.46); 2 req; L2.18 | 1.46 s (1.45); 2 req; L3.33 | 1.65 s (1.64); 2 req; L2.12 | 1.48 s (1.46); 2 req; L3.41 | 1.52 s (1.51); 2 req; L3.54 |
| type 9-digit TIN @400ms/key (slow typist) | 53.44 s (50.12); 26 req, 7 refused; L8.80 | 39.44 s (36.18); 24 req, 8 refused; L5.82 | 34.12 s (30.84); 24 req, 7 refused; L8.51 | 21.15 s (17.89); 22 req, 8 refused; L5.38 | 3.71 s (0.44); 20 req; L2.18 | 3.70 s (0.44); 18 req; L3.33 | 3.75 s (0.47); 18 req; L1.95 | 3.68 s (0.40); 20 req; L3.41 | 3.65 s (0.39); 18 req; L3.54 |
| click first row -> drawer | 0.39 s (0.39); 1 req; L8.48 | 0.38 s (0.38); 1 req; L7.11 | 0.50 s (0.49); 1 req; L8.49 | 0.43 s (0.43); 1 req; L6.42 | 0.44 s (0.43); 1 req; L2.48 | 0.41 s (0.41); 1 req; L3.46 | 0.41 s (0.40); 1 req; L1.80 | 0.40 s (0.39); 1 req; L3.62 | 0.41 s (0.40); 1 req; L3.66 |
| clear search box (5) | 2.08 s (2.08); 2 req; L7.88 | 1.82 s (1.81); 2 req; L7.11 | 1.91 s (1.91); 2 req; L8.49 | 1.78 s (1.77); 2 req; L6.42 | 1.49 s (1.47); 2 req; L2.29 | 1.57 s (1.56); 4 req; L3.46 | 1.61 s (1.61); 4 req; L1.80 | 1.45 s (1.44); 2 req; L3.62 | 1.45 s (1.44); 2 req; L3.37 |
| type 10-digit NPI @120ms/key (TIN grain) | 1.83 s (0.71); 2 req; L7.88 | 1.82 s (0.71); 2 req; L6.86 | 1.97 s (0.78); 4 req; L8.13 | 1.82 s (0.71); 2 req; L5.91 | 1.53 s (0.42); 2 req; L2.29 | 1.56 s (0.45); 2 req; L3.19 | 1.55 s (0.44); 2 req; L1.80 | 1.55 s (0.44); 2 req; L3.65 | 1.54 s (0.44); 4 req; L3.37 |
| switch grain -> NPI (NPI in box) | 1.63 s (1.63); 4 req; L7.25 | 0.55 s (0.55); 2 req; L6.86 | 0.84 s (0.84); 2 req; L8.13 | 0.53 s (0.53); 4 req; L5.91 | 0.57 s (0.57); 2 req; L2.10 | 0.55 s (0.55); 2 req; L2.93 | 0.54 s (0.54); 2 req; L1.65 | 0.51 s (0.51); 2 req; L3.65 | 0.51 s (0.51); 2 req; L3.18 |
| switch grain -> Entity (NPI in box) | 0.29 s (0.29); 2 req, 2 refused (refused: store is large); L7.25 | 0.29 s (0.29); 2 req, 2 refused (refused: store is large); L6.87 | 0.30 s (0.30); 2 req, 2 refused (refused: store is large); L8.13 | 0.29 s (0.29); 2 req, 2 refused (refused: store is large); L5.43 | 0.29 s (0.29); 2 req, 2 refused (refused: store is large); L2.10 | 0.30 s (0.30); 2 req, 2 refused (refused: store is large); L2.93 | 0.29 s (0.29); 2 req, 2 refused (refused: store is large); L1.65 | 0.29 s (0.29); 2 req, 2 refused (refused: store is large); L3.76 | 0.29 s (0.29); 2 req, 2 refused (refused: store is large); L3.18 |
| switch grain -> TIN (NPI in box) | 0.35 s (0.35); 2 req; L6.67 | 0.35 s (0.35); 2 req; L6.87 | 0.39 s (0.39); 2 req; L7.80 | 0.34 s (0.34); 2 req; L5.43 | 0.34 s (0.34); 2 req; L2.10 | 0.34 s (0.34); 2 req; L2.93 | 0.35 s (0.35); 2 req; L1.52 | 0.33 s (0.33); 2 req; L3.76 | 0.34 s (0.34); 2 req; L3.18 |
| clear search box (6) | 1.74 s (1.73); 2 req; L6.67 | 1.74 s (1.74); 4 req; L6.87 | 3.27 s (3.27); 2 req; L7.80 | 1.74 s (1.73); 2 req; L5.43 | 1.48 s (1.47); 4 req; L1.93 | 1.59 s (1.58); 2 req; L2.93 | 1.54 s (1.53); 2 req; L1.52 | 1.45 s (1.44); 2 req; L3.76 | 1.44 s (1.43); 2 req; L3.18 |
| click CPT chip 97110 | 0.70 s (0.70); 2 req; L6.67 | 0.74 s (0.74); 2 req; L6.64 | 0.56 s (0.56); 2 req; L7.82 | 0.43 s (0.43); 2 req; L5.32 | 0.72 s (0.72); 2 req; L1.93 | 0.45 s (0.45); 2 req; L2.70 | 0.42 s (0.41); 2 req; L1.52 | 0.78 s (0.78); 4 req; L3.62 | 0.41 s (0.41); 2 req; L2.92 |
| CPT on: type partial name @120ms/key | 3.38 s (2.15); 2 req; L6.21 | 2.75 s (1.51); 2 req; L6.64 | 4.02 s (2.77); 4 req; L7.82 | 2.75 s (1.51); 2 req; L5.32 | 2.46 s (1.21); 2 req; L1.93 | 2.52 s (1.28); 4 req; L2.70 | 2.44 s (1.20); 4 req; L1.40 | 2.42 s (1.18); 2 req; L3.62 | 2.48 s (1.24); 2 req; L2.92 |
| CPT on + name: grain -> Entity | 0.29 s (0.29); 2 req, 2 refused (refused: store is large); L6.36 | 0.29 s (0.29); 2 req, 2 refused (refused: store is large); L6.67 | 0.30 s (0.30); 2 req, 2 refused (refused: store is large); L7.35 | 0.29 s (0.29); 2 req, 2 refused (refused: store is large); L5.29 | 0.29 s (0.29); 2 req, 2 refused (refused: store is large); L1.78 | 0.29 s (0.29); 2 req, 2 refused (refused: store is large); L2.48 | 0.29 s (0.29); 2 req, 2 refused (refused: store is large); L1.40 | 0.29 s (0.29); 2 req, 2 refused (refused: store is large); L3.73 | 0.29 s (0.29); 2 req, 2 refused (refused: store is large); L2.69 |
| CPT on + name: grain -> NPI | 0.29 s (0.29); 4 req, 2 refused (refused: store is large); L6.36 | 0.29 s (0.29); 2 req, 2 refused (refused: store is large); L6.67 | 0.30 s (0.30); 2 req, 2 refused (refused: store is large); L7.35 | 0.29 s (0.29); 2 req, 2 refused (refused: store is large); L5.29 | 0.29 s (0.29); 2 req, 2 refused (refused: store is large); L1.78 | 0.29 s (0.29); 2 req, 2 refused (refused: store is large); L2.48 | 0.29 s (0.29); 2 req, 2 refused (refused: store is large); L1.28 | 0.29 s (0.29); 2 req, 2 refused (refused: store is large); L3.73 | 0.29 s (0.29); 2 req, 2 refused (refused: store is large); L2.69 |
| CPT on + name: grain -> TIN | 0.60 s (0.60); 2 req; L6.36 | 0.63 s (0.63); 2 req; L6.67 | 1.24 s (1.24); 2 req; L7.08 | 0.64 s (0.64); 4 req; L5.29 | 0.65 s (0.65); 2 req; L1.64 | 0.66 s (0.66); 2 req; L2.28 | 0.59 s (0.59); 2 req; L1.28 | 0.61 s (0.61); 2 req; L3.73 | 0.62 s (0.62); 4 req; L2.69 |
| clear search box (7) | 0.71 s (0.71); 2 req; L5.85 | 0.73 s (0.71); 2 req; L6.22 | 1.03 s (0.93); 2 req; L7.08 | 0.72 s (0.72); 2 req; L5.19 | 0.43 s (0.43); 2 req; L1.64 | 0.49 s (0.44); 2 req; L2.28 | 0.45 s (0.44); 2 req; L1.28 | 0.45 s (0.44); 2 req; L3.43 | 0.45 s (0.44); 2 req; L2.87 |
| sort by Entity name (CPT on) | 0.31 s (0.31); 1 req; L5.85 | 0.39 s (0.39); 3 req; L6.22 | 1.14 s (1.14); 1 req; L7.32 | 0.39 s (0.39); 1 req; L5.19 | 0.33 s (0.33); 1 req; L1.64 | 0.41 s (0.41); 1 req; L2.28 | 0.36 s (0.35); 1 req; L1.18 | 0.38 s (0.38); 1 req; L3.43 | 0.35 s (0.35); 1 req; L2.87 |
| next page (CPT on) | 0.30 s (0.30); 1 req; L5.85 | 0.36 s (0.36); 1 req; L6.22 | 1.41 s (1.41); 1 req; L7.32 | 0.37 s (0.37); 1 req; L5.19 | 0.35 s (0.35); 3 req; L1.64 | 0.38 s (0.38); 1 req; L2.10 | 0.33 s (0.33); 1 req; L1.18 | 0.38 s (0.38); 1 req; L3.43 | 0.38 s (0.38); 1 req; L2.87 |
| page size 250 (CPT on) | 0.36 s (0.36); 1 req; L5.85 | 0.41 s (0.41); 1 req; L5.72 | 1.36 s (1.36); 3 req; L7.37 | 0.40 s (0.40); 1 req; L4.85 | 0.37 s (0.37); 1 req; L1.91 | 0.42 s (0.42); 1 req; L2.10 | 0.37 s (0.37); 1 req; L1.18 | 0.39 s (0.39); 1 req; L3.43 | 0.39 s (0.39); 1 req; L2.64 |
| Clear all filters | 4.85 s (4.85); 2 req; L5.54 | 4.92 s (4.92); 2 req; L5.72 | 12.68 s (12.68); 2 req; L7.37 | 5.07 s (5.07); 2 req; L4.85 | 4.95 s (4.95); 2 req; L1.91 | 5.20 s (5.20); 4 req; L2.10 | 4.88 s (4.88); 4 req; L1.18 | 5.71 s (5.71); 4 req; L3.32 | 5.03 s (5.03); 2 req; L2.64 |
| type state code @120ms/key | 8.65 s (8.50); 4 req; L5.42 | 7.39 s (7.25); 4 req; L5.58 | 15.11 s (14.96); 6 req; L7.89 | 7.28 s (7.14); 4 req; L4.78 | 5.26 s (5.12); 4 req; L1.91 | 5.36 s (5.22); 2 req; L2.25 | 5.45 s (5.31); 2 req; L1.29 | 4.51 s (4.36); 2 req; L3.45 | 4.40 s (4.26); 4 req; L2.75 |
| Clear all filters (2) | 5.46 s (5.46); 4 req; L5.51 | 4.57 s (4.56); 2 req; L5.41 | 11.68 s (11.67); 4 req; L8.21 | 5.02 s (5.01); 2 req; L4.82 | 4.79 s (4.79); 2 req; L2.08 | 4.92 s (4.91); 2 req; L2.38 | 5.08 s (5.08); 2 req; L1.59 | 4.50 s (4.50); 4 req; L3.58 | 4.74 s (4.73); 2 req; L2.93 |
| open Code comparison (auto first code) | 0.13 s (0.13); 2 req; L5.39 | 0.12 s (0.12); 2 req; L5.30 | 0.25 s (0.25); 2 req; L8.64 | 0.12 s (0.12); 2 req; L4.75 | 0.12 s (0.12); 2 req; L2.23 | 0.12 s (0.12); 4 req; L2.51 | 0.13 s (0.13); 4 req; L1.78 | 0.12 s (0.12); 2 req; L3.61 | 0.12 s (0.12); 2 req; L3.02 |
| Code page: click 97110 | 1.01 s (1.01); 2 req; L4.96 | 1.00 s (1.00); 2 req; L5.30 | 2.94 s (2.94); 2 req; L8.64 | 1.02 s (1.02); 4 req; L4.75 | 0.99 s (0.99); 2 req; L2.23 | 0.95 s (0.95); 2 req; L2.51 | 0.93 s (0.93); 2 req; L1.78 | 0.96 s (0.96); 2 req; L3.61 | 0.98 s (0.98); 2 req; L2.78 |
| Code page: click 97530 | 0.95 s (0.95); 2 req; L4.96 | 0.97 s (0.97); 2 req; L4.87 | 2.37 s (2.37); 2 req; L8.99 | 1.13 s (1.13); 2 req; L4.45 | 0.90 s (0.90); 2 req; L2.05 | 0.83 s (0.83); 2 req; L2.31 | 0.88 s (0.88); 2 req; L1.96 | 0.78 s (0.78); 2 req; L3.72 | 0.90 s (0.90); 2 req; L2.78 |
| open Benchmark tab | 0.05 s (0.05); 6 req; L4.64 | 0.05 s (0.05); 8 req; L4.87 | 0.07 s (0.07); 6 req; L9.39 | 0.05 s (0.05); 6 req; L4.45 | 0.06 s (0.06); 6 req; L2.05 | 0.05 s (0.05); 6 req; L2.31 | 0.04 s (0.04); 6 req; L1.96 | 0.03 s (0.03); 6 req; L3.72 | 0.07 s (0.07); 6 req; L2.55 |
| Benchmark: type subject name @120ms/key | 1.38 s (0.24); 1 req; L4.64 | 1.38 s (0.23); 1 req; L4.87 | 1.50 s (0.26); 1 req; L9.39 | 1.38 s (0.23); 1 req; L4.09 | 1.37 s (0.23); 3 req; L2.05 | 1.37 s (0.23); 1 req; L2.31 | 1.36 s (0.23); 1 req; L1.81 | 1.36 s (0.23); 1 req; L3.42 | 1.39 s (0.25); 1 req; L2.55 |
| Benchmark: clear + type partial @400ms/key | 4.28 s (0.23); 13 req; L4.64 | 4.29 s (0.23); 11 req; L4.48 | 4.43 s (0.27); 13 req; L8.88 | 4.29 s (0.24); 11 req; L4.09 | 4.29 s (0.23); 11 req; L2.05 | 4.30 s (0.24); 11 req; L2.12 | 4.30 s (0.24); 11 req; L1.81 | 4.28 s (0.24); 13 req; L3.42 | 4.29 s (0.23); 11 req; L2.55 |
| Benchmark: Run (first result screen) | 1.21 s (1.14); 4 req; L4.27 | 0.71 s (0.63); 4 req; L4.12 | 0.67 s (0.62); 4 req; L8.81 | 0.34 s (0.27); 6 req; L3.85 | 0.70 s (0.61); 4 req; L2.04 | 0.73 s (0.66); 6 req; L2.03 | 0.39 s (0.32); 6 req; L1.66 | 0.69 s (0.63); 4 req; L3.23 | 0.35 s (0.29); 4 req; L2.43 |
| Benchmark: Run again | 0.50 s (0.50); 5 req; L6.14 | 0.23 s (0.23); 3 req; L4.11 | 0.58 s (0.58); 3 req; L8.81 | 0.24 s (0.24); 3 req; L3.54 | 0.24 s (0.24); 3 req; L1.96 | 0.25 s (0.25); 3 req; L1.87 | 0.33 s (0.33); 3 req; L1.85 | 0.25 s (0.25); 3 req; L3.37 | 0.25 s (0.25); 3 req; L2.55 |

Per-request detail of every cell (when each request left the browser and how long it took): `out/ui_requests_real.md`, `out/ui_requests_scale.md`; full timelines: `repros/show.py out/ui_<run>.json <scenario text>`.

## Appendix C — files

| file | what |
|---|---|
| `repros/ui_search.py` | the Chromium driver: types into the real boxes, logs every request, DOM change and long task |
| `repros/show.py`, `repros/table.py`, `repros/req_in_ui.py` | print a scenario's timeline / the run tables |
| `repros/bodies.py`, `repros/ab.py`, `repros/req_table.py` | 57 search-path URLs timed alone, bodies saved, byte comparison |
| `repros/explain_search.py` | EXPLAIN ANALYZE of the exact SQL each endpoint builds (server stopped) |
| `repros/exp_units.py`, `repros/exp_in.py` | the practice-list experiment and how DuckDB reads a small practice set |
| `repros/abandoned.py` | abandoned requests keep the server busy |
| `repros/evict.py`, `repros/first_calls.sh` | first calls after a restart, with the store dropped from the page cache |
| `repros/serve.sh` | start/stop a server on `real.yaml` (8702) or `scale.yaml` (8701); `MRFX_SRC=` runs a scratch copy |
| `repros/chain_scale.sh`, `repros/chain2.sh` | the unattended run order used for the A/B |
| `proto/`, `proto2/` | prototype A and B (scratch copies of `mrfx/`); `out/protoA_*.diff`, `out/protoB_*.diff` against HEAD |
| `out/ui_*.json`, `out/bodies_*`, `out/explain_scale.log`, `out/plans/`, `out/serve_*.log` | raw results |

The real-store copy (354 MB) was deleted to free disk; to re-run the real-store steps copy `perf/named_vis/store` to `/tmp/mrfx_review10/search/real/store` again (`real.yaml` points there).
