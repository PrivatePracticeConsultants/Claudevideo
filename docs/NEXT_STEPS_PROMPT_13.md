# Prompt for the next session, round 13 — analytics that keep up with a billion rows, a progress bar on every output, the measured slow paths, the skip protocol, and the features worth adding (written for Claude Opus 5.5)

> **STATUS (Nov 12, 2026, v0.7.7): Groups 1–5 closed (every item, plus the
> lead's 5.2.7); Group 6: 6.1–6.4 built, 6.5 and 6.6 decided against (reasons
> below); the carry-overs are listed with what happened to each.**
> Commits: groups 1+2 1a33475 (one commit: they share `Store.connect`, the
> rollup's progress object and the same api.py / app.js lines — the message
> says which item landed where), group 3 95e98b4, group 4 0b09eb5, group 5
> 935f419, group 5 follow-up 2f25ae9 (two tests the full suite caught), group
> 6 8df8452, the real-Chromium fixes 3228216 (a half-built payer counted as
> covered; the coverage sentence cut off at the header's edge; the PDF twins
> squeezing their rows), the kill-window fix 1427445 (below), and the release
> (docs, version, this block): the commit after 1427445.
> `tests/mrfx/test_review13.py`: **39 cases; 37 fail on c8b80fc** (run from a
> `git archive` of it with the editable install's fall-through to this
> checkout's new modules removed — without that, one more passed by
> importing the new `mrfx/pdf.py`; the value each got is in its docstring).
> The two that pass there: `test_5_2_7_a_list_landing_first_or_last_still_works`,
> a guard (the orderings that always worked), and
> `test_5_2_6_a_kill_after_the_commit…`, the regression test for this
> round's own 5.2.6 change (c8b80fc had the size-and-age guess it replaces). `tests/mrfx/test_progress.py`: the 14 tests of
> DESIGN §3.6.7. Full suite **1247 passed, 0 failed, 36:00** on a quiet box after the last code
> change; quick tier 470 tests, 470 passed, 4:18; ruff 0; pip-audit clean (no new dependency:
> the PDF uses the Playwright already pinned). `tests/slow_tests.txt`
> regenerated from that quiet run (>= 0.5 s: 788 of 1242 timed tests).
> Existing tests changed on purpose, each with the reason in the test:
> `test_hardening` — the clustering test checks clustering within each
> payer's block (the spine is now built payer by payer), the async-route
> whitelist adds `/api/progress/{token}` (the poll must answer while every
> worker is busy), and `test_scan_inbox_rebuilds_once_per_pass`'s second file
> is one byte different (its decompressed copy is now — correctly — the same
> content, 5.2.1); `test_round7` — the version pin 0.7.6 -> 0.7.7.
>
> **Decisions.**
> - **4.2: disclose, do not change.** Measured what one-rate-per-payer would
>   move: 52 of 144,859 practice x code rates on the real 8-file store (0.04 %,
>   52 of 6,246 practices, largest $47.24); 2,286,166 of 4,774,313 (47.9 %) on
>   the 99M synthetic store (its POS variants are random). A payer's two POS
>   lines are two contracted lines, and the other rule would move numbers the
>   user has sold; `benchmark.PRACTICE_RATE_RULE` is printed with every
>   all-payer figure and written in AI_HANDOFF.
> - **5.2.4: remember the code list per file, offer one button.**
>   `files.code_set`; after codes are ADDED the Files tab says "N loaded files
>   were read before you added billing code(s) …" with "Re-read N files for
>   the new codes" (kept copy, else the link downloaded again, else the row
>   asks for the file); a loaded link pasted again re-reads it. Removing codes
>   re-reads nothing (rows are filtered at read time). GETTING_STARTED and
>   `mrfx status` said "re-add those links" (it did nothing: "0 added, 1
>   already known") — both now describe this.
> - **5.2.5: `monthly_recheck` on by default, `monthly_refresh` stays opt-in.**
>   The re-check costs one 1-byte request per loaded file per month and
>   downloads only what changed; queueing NEW dated months spends bandwidth
>   and disk, so it stays the user's choice.
> - **5.2.7 (the lead's P0).** A provider list that finished while its payer's
>   rate file was being read (or still held its per-file claim) was never
>   applied. `Store.refs_epochs()` is snapshotted at the start of `ingest_file`
>   and compared at its end; a list that landed meanwhile re-reads the file.
>   `test_parallel_ingest_matches_serial`: **before 3 of 12 failed on 0.7.6
>   (c8b80fc) and 4 of 12 on 0.7.5 (68d9bc7)** (the lead's runs; failing runs
>   ~5.3 s vs ~6.9 s); **after: 30 of 30 passed on the final code** (30/30 on the release code, 6.6-9.9 s each; also 30/30 before the kill-window fix).
>   Deterministic tests force both orderings with hooks (no sleeps): was 4
>   rows and "1 rate groups skipped", now 6 / 0.
> - **Kill window found by the round-11 harness.** `ingest/s4_kill.py` on this
>   round's code first gave 23/25: killed after the 'done' commit and before
>   the identity row (the analytics update runs in between), the very file
>   still in the inbox came back as "a different file with the same name"
>   (5.2.6 had removed the size-and-age guess that covered this). The 'done'
>   upsert now carries the read identity (`files.read_identity`) and the code
>   list in the same write: **25/25 trials correct, 0/25 pool workers left alive 45 s after the kill** (seed 65474; 23/25 before the fix, seed 59200 — the two window trials held the same file as needs_choice).
> - **6.5 Excel workbook: not built.** The exports were not rewritten to
>   DuckDB COPY (Group 3 kept the Python writers), so an xlsx would be a
>   second writer over six outputs, each needing its own number-format and
>   sheet tests, plus a new dependency (openpyxl) on the user's PC; every CSV
>   already opens in Excel with the methodology beside it. Worth doing as one
>   generic writer over the export's row iterator, with a byte-level test that
>   the xlsx and the CSV carry the same cells.
> - **6.6 Expected payment per visit: not built.** No commercial payer in the
>   store publishes its multiple-procedure or assistant reduction (the MRF
>   schema has no field for either), so the only stateable rules are
>   Medicare's; applying them to a commercial rate would print an assumption
>   as a number. It stays the written caveat.
> - **Carry-overs.** Round 11's 3.1 single-pass rollup: superseded by 1.3 (the
>   per-slice durable build). Round 10's 1.5 / 1.9 residue: measured on the
>   99M store in the audit's table — a name search 1.4 s / 1.0 s (rows) and
>   1.6 s (strip), the unfiltered page 2.0 s / 1.1 s: under the P2 bar, left
>   as it is. Round 11's 3.6 segment map, 4.4(h) monotonic clock through sleep
>   and 4.4(c) filename cap (documented): not done this round — the P0s took
>   the time; still open. Windows has still never been run (no Windows box
>   here).
>
> **Group 1 numbers (99,000,000 rates, 79,027,749 spine rows, 4 cores, 6 GB
> pool).** 0.7.6 full rebuild 738.3 s, peak RSS 6.36 GB; 0.7.7 sliced build
> from an EMPTY spine 363.1 s, 6.44 GB, 49 slices, first payer on screen at
> 33 s; `kill -9` after slice 25 of 49 and restart: resumed at slice 26, ran
> 24 slices (186 s + 188 s; 0.7.6 starts over). Byte identity: 375/375
> (payer, code) cells and 64/64 directory buckets identical to the 0.7.6
> build, for the uninterrupted build AND the kill-then-resume one; the real
> 8-file store 278/278 at 8 and at 125 forced slices. Extrapolated to 1.16 B
> rates: about 2–4 hours on a 4-core laptop (0.7.6's one-shot: ~28 h on this
> box), the dashboard usable throughout. Seen in Chromium on the 99M store
> with a build killed after 6 slices: "Analytics cover 1 of 15 payers (3 of
> 45 files). Rolling up the rest — 25 %, about 19 min left (Payer 01 ·
> 92507–97161)", greyed chips, the outreach bar "Listing the organizations in
> this view (1 of 5)" and its Stop ("stopped"), a `#benchmark?subject=…&state=MO`
> link filled and run, "Save as PDF" -> pitch_report_2026-11-12.pdf (%PDF-,
> 103 KB), the search box opening the drawer; 0 page errors.
>
> **Speed (99M store, quiet, cold / warm ms, before -> after):** outreach
> whole store 71,110 / 72,230 -> 37,247 / 36,914 (target ~25 s not reached);
> entity drawer by name 30,409 / 15,911 -> 1,598 / 1,473; Overview 26,900 ->
> 2,303; client digest 17,255 -> 4,962; outreach entity CSV MO 15,577 ->
> 8,266; validate 13,890 / 2,666 -> 550 / 549; outreach MO 11,555 -> 4,889;
> fee schedule 10,174 -> 215; city rate map 9,447 / 5,327 -> 201 / 188;
> whole-store CSV refusal 8,212 -> 183; new-to-network 8,043 / 9,241 ->
> 5,902 / 2; market report 7,299 / 5,134 -> 2,500 / 221; market sizing
> 7,018 / 8,259 -> 783 / 717; `/api/stats` 6,823 -> 1,025; leads CSV 1,846 ->
> 3 (`perf/REPORT.md` §7).
>
> **Accuracy: 0 differences.** Fresh three-file real store,
> `perf/check_real.py`: TX 734 checks, WY 202, 0 differences (the "All payers"
> visit-value row now p26 · Q2, was Q3 — item 4.1); entity grain 2/2.
> `perf/spot99.py` on 134 bodies fetched from the release code: 330 checks,
> 0 problems, output identical to the same script on c8b80fc's bodies;
> `spot99_entity.py` 637 drawer rows, 0 mismatches. The 26 bodies that differ
> from c8b80fc's are all explained (`perf/REPORT.md` §7): new fields, the
> version string, the 4.1 and 4.2 sentences, the 6.2 "Not included, and why"
> lines; no number moved.
>
> **Rows: nothing loaded changed.** BCBS-MS (2,660,356 rows), UHC-NY
> (20,741), Tufts zip (118,895), through the inbox AND through links (local
> server): each part's row count and the sha256 of its sorted rows IDENTICAL
> on c8b80fc and on this code. `fastcheck` 3/3 IDENTICAL (ND HMHS, UHC-NY,
> BCBS-MS).
>
> **Kill harnesses on the final code** (copies under the scratchpad, paths
> retargeted; s4_kill's `os.kill` of late orphans replaced by a report):
> queue `s5_crash.py` seed 4242 x 20: no row stuck and no row-count mismatch in
> 20/20, files == parts 9/9 in 19 (trial 2 dies before `add_urls`, as in
> rounds 11–12), downloads left: none in 20/20 (the "PROBLEM" tag is the
> harness counting `.verified_parts.json`, as before); download
> `c10_kill.py` x 12 seed 39616: **12/12 OK**; ingest `s4_kill.py`: **25/25 trials correct, 0/25 pool workers left alive 45 s after the kill** (seed 65474; 23/25 before the fix, seed 59200 — the two window trials held the same file as needs_choice).
>
> **Repro scripts after the fixes.** skip/s1_dedupkey: 4 keys differing from
> the stated expectation -> 0; s3_identity A1–A4 (gzip levels, .json vs .gz,
> link then inbox): each was 2 done / 600 rates -> 1 done + 1 "the same content
> as …" / 300 (case C still loads twice: it lowers the hash limit to 1 MB on
> purpose, a limit that no longer exists); s10_known: a pasted index after
> the monthly tick was 'done' (never re-checked) -> 'queued'; s7 C, the
> re-paste of an unchanged done link, still costs 0 requests (right: the code
> list did not grow); diff/p_g: left at 2026-05 -> re-downloaded, 2026-06;
> mine/stale_pickers: no field anywhere -> `analytics` in /api/stats and
> /api/boot, /api/payers {3 payers, covered 1}; diff/p_c (Clear-queue window)
> unchanged by design (the confirm text now says it); diff/vm1_handworked: its
> four quartile expectations encode the OLD rule — the 4.1 change, on purpose.
>
> **Zip:** built from the release commit with `git archive --format=zip --prefix=Claudevideo/` to the session scratchpad (`mrfx_project.zip`), extracted into a fresh folder and checked there — version 0.7.7, both .bat launchers CRLF, every committed file identical; the results are in the session report.

## What the user asked for, in their words

1. "Review the file again to make sure it's running as perfect as possible."
2. "A loading bar when running any of the output features … load from 0–100."
3. "Review the output features again. Are there any things that are running
   slower than expected or generating inaccurate data? Is there any way to have
   it move faster?"
4. "Think deeply about any features we should add on the output side … I don't
   want change just for the sake of change."
5. From a screenshot of their live store: the header says **1,161,961,086 rate
   rows · 574,173 TINs · 49 payers · 615 files**, and the Leads tab's payer
   chips offer **three** payers.
6. "Take a deep dive through the existing skip protocol … things aren't
   accidentally skipped that are real files and vice versa."

## How this round was audited

- **The 0.7.6 zip re-verified**: version, launchers CRLF, every file identical
  to c8b80fc; a fresh install; the 0.7.5 → 0.7.6 update on a loaded store
  (exports byte-identical before and after); `tests/mrfx/test_review12.py` 27/27
  and the quick tier 465/465 run from the extracted zip.
- **Every output feature timed on the 99,000,000-row store** (15 payers, 45
  files, 194,000 TINs, 12 states) through the real server as the browser calls
  it: 135 routes cold / warm / variant (`docs/review13_repros/perf/timings.csv`),
  in-process statement-level profiles of the eight slowest
  (`perf/prof.json`), a Chromium walk of all 14 views, and **936 independent
  recomputations from raw rows** on a real three-file store (TX and WY
  subjects) plus hand checks on the 99M store (`perf/REPORT.md` §4). **No wrong
  number.** 133 of 135 routes byte-identical cold vs warm (the two exceptions
  are timestamps). No 500. 0 console errors.
- **The round-12 diff reviewed adversarially** (`docs/review13_repros/diff/diff_review_12.md`):
  no P0, no P1; the visit-value block held 34 hand-worked checks; two P2s and
  two P3s below.
- **A design for the progress bar**, grounded in how each output runs today and
  in a DuckDB experiment (`docs/review13_repros/progress/DESIGN.md`, 691 lines).
- **The skip protocol enumerated decision by decision** with a repro per
  decision (`docs/review13_repros/skip/SKIP_REVIEW.md`) — Group 5.
- **The 0.7.6 zip's full suite, run by the session lead:** 1 failed, 1193
  passed, 26:48 (log `scratchpad/audit13/full_suite_076_zip.log`). The
  failure is a real pre-existing race, not noise:
  `test_parallel_ingest_matches_serial` (parallel_ingests=2, two rate files
  + the first payer's companion provider list) expects (6, 4) rows and the
  failing runs give (4, 4) — the list finished while its payer's rate file
  was being read and was never applied. Measured on pristine copies, 12 runs
  each: 0.7.6 (c8b80fc) 3 failed, 0.7.5 (68d9bc7) 4 failed; failing runs
  take ~5.3 s against ~6.9 s. Added as Group 5 item 5.2.7 (P0: rows silently
  short until the user acts).
- **The user's screenshot reproduced** on a scratch store
  (`docs/review13_repros/mine/stale_pickers.py`): header 3 payers, chips 1,
  Explorer counting 82,546 rows against 1,130,194 in the header, the store
  itself reporting "2 newly ingested file(s) not yet rolled up", and **no
  field in `/api/stats` or `/api/boot` that says so** — Group 1.

Severities: **P0** wrong number / data loss / the dashboard answering from a
fraction of the store without saying so; **P1** stuck or misleading state,
two screens disagreeing; **P2** slow (> 5 s cold or > 2 s warm) or wasteful;
**P3** cosmetic.

---

## Group 1 — Analytics that keep up with the store, and say where they stand (P0; ship first)

### 1.1 What is happening on the user's machine

The header counts payers and rows from the **files table** (what was loaded).
Every picker and every screen reads the **analytics spine**
(`rates_by_tin_tbl`, built by the rollup): `/api/payers` is
`SELECT DISTINCT payer FROM rates_by_tin_tbl` (`api.py:4469-4491`); the
Explorer, Benchmark, Leads, Rate card and every report go through the spine
or the tables derived from it. On the user's store the spine holds three
payers; the other 46 payers' files are `done` with their row counts in the
header and **absent from every number on every screen**, and nothing on screen
says so. The store knows (`rollups_stale()` / `rollup_stale_reason()`,
`store.py:2022-2075`) — that knowledge reaches only `serve.log`
(`app.js` has no banner; grep "stale" finds only the table-dimming class).

Why the rollup falls behind at this size: the full build is a whole-store pass
(documented 65 min at 306 M rows on this box, `AI_HANDOFF.md:1333`; the user
has 1.16 B rows and a laptop); the queue worker **gives up after three failed
attempts for the run** (`fetch.py:3568-3577`, "raw data is safe, analytics
will refresh on the next successful rebuild"); a full rebuild that does not
finish is thrown away, not resumed (`rollup_needs_full` stays set "until it
completes once", `store.py:2055`); a rebuild runs at most once per adaptive
interval and once at idle (`_rollup_due`, `fetch.py:3438`), and round 11's 4.3
(one rollup per idle gap) is still open; the incremental path needs a prior
full build and a recorded payer and otherwise falls back to the full pass
(`fetch.py:3553-3563`). Repro of the symptom: `mine/stale_pickers.py`.

### 1.2 Fix, part A — the dashboard tells the truth about coverage (P0, do first, small)

- Add to `/api/stats` (and to `/api/boot` so the first paint has it):
  `analytics: {covered_files, done_files, covered_payers, loaded_payers,
  covered_rows, loaded_rows, state: "current" | "behind" | "building" |
  "failed", reason, pct, eta_text}` from `rollup_covered_names`,
  `rollup_needs_full`, `rollup_pending_payers`, the rollup's own progress (see
  1.3) and the last failure's sentence.
- The header shows it whenever `state != "current"`: **"Analytics cover 3 of
  49 payers (3 of 615 files). Rolling up the rest — 41 %, about 2 h left"**, or
  **"… stopped: ran out of memory at 8 GB — Files tab"**, in the same line as
  the counts, in the warn colour. Every report's methodology footer and every
  CSV sidecar carries the same sentence when the store is behind (invariant 4:
  a rate card built from 3 of 49 payers must say so).
- Pickers list what is **loaded**, not what is rolled up: `/api/payers`
  returns `{payers: [...all 49 from files...], covered: [...3...]}`; chips for
  uncovered payers are rendered greyed with the title "loaded, not yet in
  analytics"; the same for `/api/states` and `/api/months` where they read the
  spine. Choosing an uncovered payer returns the normal "no rows" answer plus
  the coverage sentence.
- Test (fails on c8b80fc): the `stale_pickers.py` scenario through TestClient —
  header and chips disagree today; after the fix `/api/stats.analytics.state ==
  "behind"`, `covered_payers == 1`, `loaded_payers == 3`, `/api/payers.payers`
  lists three, `.covered` one, and the pitch report's footer contains the
  sentence.

### 1.3 Fix, part B — a rollup that finishes on a billion rows and never silently gives up (P0)

The full rebuild must become a **sequence of per-payer (or per-payer × hash)
slices, each committed on its own**, so that:

- progress persists across a kill, a restart, a memory failure, the 3-failure
  give-up and the "one per idle gap" rule — a restart continues from the last
  committed slice instead of starting over (the store already slices the build
  by `hash(tin_value)` and by payer for removals; what is missing is
  per-slice durability and a resumable plan: `meta.rollup_plan = {generation,
  slices:[…], done:[…]}`);
- the spine is **queryable while it is being built**: finished payers appear
  (coverage goes 3 → 4 → … → 49 and the header line moves), the swap is per
  slice, and the per-generation caches key on the covered set so a screen
  cached at 3 payers is not served at 49;
- a memory failure in one slice halves that slice and retries it (the ladder
  exists per build; make it per slice), and the row-rule / schema versions are
  checked per slice so an upgrade mid-build resumes correctly;
- the "give up after 3 failures" becomes "mark `state: failed` with the
  sentence, keep the committed slices, retry on the next idle gap with the
  smaller slice", and the Files tab shows it;
- new files take the incremental path by payer slice whenever the spine
  exists for that payer, and the full path only for payers not yet in the
  spine (today a single `rollup_needs_full` flag forces the whole store);
- the rollup reports progress through the same `Progress` object as Group 2
  (slice i of n, rows done, ETA from the measured rows/s), visible in the
  header line and the Files tab.

Measure before and after on the 99M store (`perf/` method): time to full
coverage from an empty spine, peak RSS, and the wall time of a kill at 50 %
followed by a restart (today: starts over; after: resumes). Extrapolate to
1.16 B rows and write the number in GETTING_STARTED ("about N hours on a
laptop, the dashboard usable throughout, the header shows how far it is").
Pin: a kill mid-build then restart ends with the same spine bytes as an
uninterrupted build (`test_determinism` style); coverage after slice k equals
the set of committed payers; a slice OOM does not lose earlier slices.

### 1.4 The two round-12 regressions found by the diff review (P2, same commit)

- `fetch.py:2922-2927`: a 0.7.4-loaded child whose server states **no size
  and no validator** (chunked 200 to the 1-byte probe) is now silently left at
  its old month on the monthly refresh — 0.7.5 re-downloaded it once. Repro
  `diff/p_g_loaded_size_unknown.py`. Treat a probe with neither validator nor
  total as unknown regardless of `loaded_size`, or note it on the row.
- `quality.py:161-191` `pooled_month_files` is a whole-spine aggregate keyed
  on `data_generation`, which the enrichment name refresh bumps every 240 s —
  about 5–6 s per 99M rows on every Files poll after each refresh, for an
  identical answer. Key it on the spine generation or fold the counts into the
  rollup.

---

## Group 2 — A 0–100 % progress bar on every output (the user's request; one commit)

Implement `docs/review13_repros/progress/DESIGN.md` as written; it was
produced against this exact code and includes the inventory of all 51 output
features (control id, JS function, routes in order, how the result is
delivered, what feedback exists today — for HTML reports opened in a tab and
frame downloads: none), the server-side stage list per route, the DuckDB
experiment, the rejected options with reasons, and 14 tests. The essentials,
so they are not re-derived:

- **Hybrid**: every output keeps the request it uses today; the page mints a
  token per run, sends `X-Mrfx-Progress: <token>` (or `?progress=` on the three
  iframe GET exports), polls an **async** `GET /api/progress/{token}` every
  400 ms while its fetch is pending, `DELETE` stops. No token → byte-identical
  behaviour (CLI, tests, old pages).
- **Server**: `mrfx/progress.py` with a `Progress` in a contextvar;
  `progress.current().stage("Comparing with peers", weight)` / `.tick(frac)` /
  `.group()`; a `_NOOP` default so no signature changes anywhere; one decorator
  per output route; `Store.connect()` attaches each new connection to the
  current `Progress` (that is how Stop reaches the connection actually running
  the statement — today only `/api/rates` and `/api/summary` can be
  interrupted) and refuses to open one once cancelled. `Stopped` subclasses
  `duckdb.InterruptException` so fault-isolated loops keep working; the route
  answers 409 "stopped" and never delivers a partial document.
- **Percent** = weights of finished stages + DuckDB `query_progress()` inside
  the current stage (measured: a free, monotonic 0–100 float readable from a
  side thread on duckdb 1.5.5; session-local; cost within noise); capped at
  99 server-side; 100 only when the browser has the body; never backwards;
  unknown length → stripe + "at least N %"; lock waits shown as "waiting".
  Weights come from one table `progress_plans.py`, filled from the measured
  stage shares in `perf/REPORT.md` §2 (for example the whole-store outreach
  ZIP: units 5 %, temp tables 3 %, geo/meta 48 %, rows 40 %, CSV 13 %, zip —
  after Group 3's rewrite re-measure and re-weight).
- **JS**: one `runWithProgress()` helper, one bar host under each view's
  button row reusing the Files tab's `.progress` CSS, label like "Pitch report
  · Comparing with peers (3 of 5) · about 48 %", run button disabled + Stop;
  frame downloads end at "Built — your browser is downloading it"; the
  existing `supersede()` / `X-Mrfx-View` machinery is kept and cancels the
  older Progress instead of calling `interrupt()` itself.
- **Detached jobs** (monthly packets, tracker import) keep their daemon
  threads and polls and gain `progress: {pct, stage, …}`; the rollup of Group
  1.3 reports through the same object. **CLI parity**: `mrfx packets`,
  `export`, `outreach`, `orgreport` print the same stages as a text bar.
- **Never**: a bar that creeps to 90 % and waits; a client-clock percentage
  (an elapsed counter is allowed); a second abort mechanism; blocking the
  event loop (long jobs get their own executor so the Files poll stays
  responsive).
- Tests (from DESIGN §3.6.7): bar ≤ 99 until the body is in hand and the
  body byte-equal with and without a token; Stop → 409 within 1 s and the
  connection interrupted; a progress failure never fails the report; no token
  → no `SET`s; every output route decorated (a test enumerates the routes);
  supersede still 409s; the waiting state; a detached-job stop never wedges
  the button; CLI prints the same stages.

Found on the way and fixed in this commit: the dossier button builds the
dossier twice at once (`app.js:3174-3177` fires `/api/report/dossier` and
`/api/report/dossier.json`); every printable / CSV twin recomputes the
on-screen result (route them through the same cached compute).

---

## Group 3 — The measured slow paths, each with a prototype that kept the numbers (P2; one commit)

All numbers are HTTP cold / warm on the 99M store unless said; prototypes in
`perf/proto.py` produced identical rows. Rank by user pain: the drawer, the
exports, the first screen.

### 3.1 Entity drawer by name: 30.4 s cold / 15.9 s warm → ~1 s

`GRAIN_REL['entity']` aggregates the whole 79 M-row spine into entities and
only then filters `WHERE unit_id = ?`; `_resolve_entity_key`, the member-TIN
list and the chart each repeat a keyed whole-spine pass (four passes to open
one practice; the TIN drawer takes 0.8 s because `unit_id = tin_value` prunes).
Prototype: resolve the member TINs first (`tin_directory` display name + main
state, plus entity-map names), then aggregate only their spine rows — **637
identical rows, 9.89 s → 0.87 s** for the main statement; apply to all four.

### 3.2 A `(tin_value, npi)` side table per spine generation → removes 5–12 s of raw scans from seven features

`SELECT DISTINCT tin_value, npi FROM rates` runs on every call in
`territory.local_rate_map` (9.4 / 5.3 s, and again inside `report/market` 7.3
/ 5.1 s), `demographics.market_sizing` (7–8 s **before it discovers no
demographics are loaded** — test that first), `clients._build_digest` (2.1 s
per client), outreach (`tin_npi`), `contracts.new_to_network`,
`territory.steal_share`, the entity drawer's member NPIs. Build it like
`tin_zip_place` (one raw scan, 1,939,999 pairs in 8.1 s on this store) keyed
by spine generation. Prototype: the MO pair lookup **3.68 s → 0.23 s,
identical set**.

### 3.3 Outreach exports: 71 s whole store, 11.5–15.6 s one state → ~25 s / ~5 s, same bytes

In-process 127.8 s of which SQL 22.6 s: `half_up` called **11,461,266 times
(40 s)**, `_geo_by_tin` / `_tin_meta` per-TIN dicts 25 s each, the per-unit
loop 11.5 s, CSV formatting 16.5 s. Compute the mid-ranks and gaps in SQL once
per code (`leads.midrank_pct_sql` + `others_median_sql` over
`_outreach_rates`, `round()` once), join geo/meta in the same statement, write
the CSV with DuckDB `COPY` like the Explorer export. Pin byte-identical CSV
content before/after (the existing outreach known-answer tests plus a
whole-file hash on the real store). Also: the ZIP member names carry `HHMM`,
so two downloads a minute apart differ in bytes (P3-6) — date only.

### 3.4 Overview, first visit: 26.9 s (+ posture 7.2 s + types 4.4 s in parallel) → ~4 s

`market_overview` builds `__overview` from the spine and recomputes per-TIN
medians three times; `practice_medians_payer_v1` + `practice_medians_v1`
already hold them. Prototype: payer index **7.0 s → 2.4 s**, top codes **2.2
s → 0.5 s**, identical numbers against the served body; counts and discipline
medians from the same tables (`practice_medians_disc_payer_v1` for the
discipline filter). Sequence posture/types after the overview or derive
`types`' per-payer counts once per generation at rollup time.

### 3.5 Others, measured

- `contracts/new-to-network` 8.0–10.6 s on every click, uncached → cache per
  (generation, therapy_only, zip/radius) via `store.cached` like changes /
  trajectory; with 3.2 its placement join stops reading raw rows.
- `/api/validate` 13.9 s cold / 2.7 s warm: `WHERE billing_code = ? AND
  (tin_value = ? OR npi = ?)` reads every part. Prototype: the TIN's files from
  `rates_by_tin.source_files` then the raw scan of those only — **identical
  30 rows, 1.91 s → 0.61 s**; the NPI half via `rate_npi_set (part, npi)`.
- `/api/stats` 6.8 s on every fresh start (it logs its own slow warning):
  persist the three whole-store counts per spine generation beside the
  summary strip → ~0.
- `leads.csv` recomputes the list the screen just cached (the UI sends no
  `limit` to `/api/leads`, the CSV route uses `LEADS_LIMIT`; keys differ) —
  1.8 s per export.
- `territory/local` computes the same per-TIN median twice (state scope and
  the reference, 3.2 + 2.3 s) — once into a temp table.
- Single-payer markets 3–10× slower than all-payer on the materialised path
  (`benchmark/opportunity` with one payer 3.3 s vs 0.23 s; `contract-gaps` /
  `quality/size` for a name subject 1.2 / 2.1 s vs 0.3 s): the per-payer table
  is read without the code-cluster benefit for the subject-only side.
- Whole-store CSV refusal takes 8–11 s to say no (a `count(*)` over the full
  export select): a count over `rates_by_tin WHERE is_latest` with the same
  filters and no dimension joins is a cheaper upper bound.
- Code comparison opens on the first catalog code (`90912`, `app.js:165`),
  which most stores have no rows for — an empty first screen; open on the
  first code with data from the summary strip.
- Server RSS peaked at 7.7 GB during the run (DuckDB at its automatic 6 GB
  cap plus Python; round 7 measured 6.3 GB on the same store) — record it,
  investigate only if Group 1.3 changes it.

Not worth chasing (measured, cached, documented): trajectory national 27 s and
rate changes national 16.6 s are once per data generation; the one-time
per-discipline table build (9.7 s) is once per spine generation.

---

## Group 4 — Numbers and wording to decide or disclose (P1/P3; one commit)

### 4.1 (P1) Visit-value quartile label contradicts the percentile on the same row when the market ties — real data

TX / PT / ortho, "All payers": P25 = median = $105.44, P75 = $110.31; a
practice at $105.44 prints **percentile 26 and quartile Q3**
(`visit_market._position`: `v < p25`? no; `v < median`? no; `v < p75` → Q3).
The written rule is applied exactly, but fee schedules tie heavily (1,668 TX
practices, four distinct visit values), so this is the common real case. Fix
without moving any other number: derive the quartile from the printed
percentile (< 25 → Q1, < 50 → Q2, < 75 → Q3, else Q4), or print "= median" /
"= P25" at a boundary; update `SUBJECT_NOTE`; pin with the tied case.

### 4.2 (P1) Benchmark "YOU" vs the rate card for one code: $46.29 vs a payer median of $46.15 (97110, TIN 100049428)

`practice_medians_v1` is the median over **all** of the practice's base lines,
so Payer 01's two POS sets (`11`, `11|22`, both $46.29) count twice and the
other three payers once: median(46.29, 46.29, 67.15, 46.01, 20.33) = 46.29,
while one rate per payer gives 46.15. Both are written rules; the methodology
("median per practice") does not say a payer with more POS variants weighs
more. **Decide**: collapse to one rate per (payer, code) before the practice
median (changes numbers — then re-run every known-answer test and say so in
"What changed"), or add the sentence to the methodology and the AI_HANDOFF
rule. Either way one rule, written down, pinned.

### 4.3 (P3) Small

- `config/mrfx.yaml:209-211`: the comment says ~4 GB on a 16 GB laptop and
  ~1 GB per worker; the code gives 6 GB and budgets 2 GB per worker (8 GiB →
  2 GB pool / 1 worker; 16 GiB → 6 GB / 4 workers; 32 GiB → 12 GB / 9 workers).
- GETTING_STARTED's visit-value paragraph never says a "practice" is a TIN, so
  a user's own two-TIN clinic reads "7 practices … p80 of 6"; one sentence.
- A child a downloader claimed in the instant after a re-queue finishes after
  "Clear queue" (documented semantics; `diff/p_c_clear_window.py`) — say it in
  the Clear-queue confirm text.

---

## Group 5 — The skip protocol, decision by decision (the user's question; one commit)

> Filled from `docs/review13_repros/skip/SKIP_REVIEW.md` — see that file for
> the decision table (rule, evidence, false-skip risk, false-load risk, what
> the user sees) and the repro per decision. The items below are the ones that
> need code; the "protocol in one page" at the end of that file goes into
> AI_HANDOFF verbatim once the items are closed.

### 5.1 Two leads the session lead confirmed in the code before the review ran

- **Identity is the sha256 of the raw bytes** (`identity.py`). The same JSON
  re-compressed differently (a payer re-gzips with another level; the browser
  saves a `.gz` the server sent with `Content-Encoding` decoded while the app
  downloaded the raw `.gz`; `.json` vs `.json.gz` of one content) has two
  identities and **loads twice**. Decide and build: a second, decompressed
  content fingerprint taken from the parse stream (the parser already reads
  every decompressed byte — one more hash is free) stored beside the raw sha
  and consulted by the same claim; a twin by content fingerprint is skipped
  with the sentence naming its partner.
- **`LOCAL_HASH_MAX_BYTES = 4 GB`**: an inbox file above 4 GB keeps size +
  mtime identity only, so a 5 GB twin under a second name, or the same 5 GB
  bytes once via a link (hashed while downloading) and once via the inbox,
  is not recognised and loads twice. Hash from the parse stream (above) and
  the limit disappears.

### 5.2 What the review found (17 decision points, D1–D17; every row has a repro under `skip/`)

The shipped pins (14 tests) pass; the early check, the content claims under
concurrency and failure, the same-name hold, month-dated children, sniff by
content and the catalog all held (`SKIP_REVIEW.md` §2 "RIGHT"). What is wrong:

**P0 — data doubled or a real file silently stale**

- **5.2.1 Re-encoded same content loads twice** (the 5.1 lead, now measured):
  gz level 1 vs 9, `.json` vs `.json.gz`, a browser-decoded copy vs the raw
  download, link then inbox — every pair ends with both rows `done`, 600 rates
  for 300, no note (`skip/s3_identity.py` A1–A4, re-run by the session lead).
  A decompressed-content sha costs **+0.8–1.0 s per GB inflated** (sha ≈ 1.3
  GB/s; +4.2 s on the 262 MB → 5.5 GB UHC file; `s3b_shacost.py`): take it on
  the download (inflate beside `ContentHasher`) and on the inbox identity /
  parse stream, store it beside the raw sha in `content_fingerprints`, and
  make the content claim check both. A content-twin is skipped with the
  partner named. Pin A1–A4 → one `done`, one `skipped/duplicate`, 300 rates.
- **5.2.2 A republished child is silently left at the old month** in three
  shapes (`skip/s6_refresh.py`): (A) the 0.7.4-loaded child whose probe
  returns no size and no validator (`fetch.py:2930-2936`, also 1.4 above);
  (C) **new bytes and a new size behind an unchanged ETag** — `_republished`
  returns on validators and never compares size (`fetch.py:2615`); (B/H/I)
  same size, bytes changed, no or same validator → "unchanged". Rule to
  write: compare EVERY signal the probe gives (validator, size, and the first
  64 KB sample where available) and treat any disagreement as republished;
  when nothing can be told, say so on the row ("could not tell whether the
  payer changed this file — re-check next month / re-paste to force") instead
  of silence; the child-requeue sentence "the payer still publishes the same
  file" must not be printed on size alone.

**P1 — the user must act and nothing tells them**

- **5.2.3 Files above 4 GB in the inbox** (the 5.1 lead, proven with the
  threshold lowered to 1 MB; `s3` C1/C3): inbox + inbox twin under a second
  name loads twice; inbox then LINK loads twice; only link then inbox is
  caught, because `_finish_ok` records `trailer_proves = None` and no sha for
  a big inbox file. Closed by 5.2.1's stream hash (the limit disappears); a
  same-name copy above 4 GB is today held as "a different file" — fix the
  sentence too.
- **5.2.4 After the user adds billing codes, nothing re-reads loaded files**
  (`s9_zero.py`, `s7` C): re-pasting a done rate link → "0 added, 1 already
  known", 0 requests; re-pasting the index or the monthly refresh → probe
  only; re-dropping the file → 'same'. `GETTING_STARTED.md:688-690` and
  `cli.py:717-719` ("re-add those links … re-downloads and re-extracts") are
  **wrong**. What works today: forget + re-paste, or `mrfx ingest --force`.
  Build the real thing: record the code set (a hash of the active CPT list)
  per loaded file; when the active set gains codes, the Files tab offers
  "re-read N files for the new codes" (a re-parse from the kept download or
  the processed copy, a re-download only when neither exists), and `mrfx add`
  / the refresh treat a done row whose code set is older as re-parseable.
  Fix the two documents either way.
- **5.2.5 The monthly refresh is opt-in and catalog-scoped** (`s10_known.py`):
  a user-pasted static index is never re-checked, and no "last checked" is
  shown anywhere. Extend the refresh to every done index the user pasted
  (one probe per child per month, as 1.7 built), show "last checked / next
  check" on the index row, and say in GETTING_STARTED that it is on.
- **5.2.6 Pre-audit-8 files** (no identity row) whose processed copy is gone:
  a corrected same-size file with an older mtime is silently 'same'
  (`ingest.py:1887-1897`); `/api/files` has no field marking such files
  (`s5` D). Mark them ("loaded before identities were kept — a same-size
  re-drop cannot be told apart; use Replace") and prefer the full sha when
  the file is on disk.

**P2 — waste**

- GCS V4 `X-Goog-*`, Akamai `__token__` and a lone JWT `token` are not
  stripped by `dedup_key` → a new row per visit, an early-check download each
  month, rows accumulate (`s1_dedupkey.py`, `s6` G); and `?token=<id ≥ 20
  chars>&sig=` collapses two different files to one key (second reported
  "already known"). Decide the rule per parameter from real indexes.
- Re-pasting a skipped/duplicate link re-downloads it (the full file under
  15 MB or plain `.json`; 15 MB + 1.5 MB of ranges for a big gz) though the
  docs say it "costs nothing" (`s7_revive.py` A): a known duplicate by sha
  should answer from the identity table without a byte.
- The UHC/Optum listing name filter drops employers whose name contains
  "allowed" as a substring or an `_NDC` / `-NDC` token ("Hallowed Health",
  "NDC Corp"), log-only (`s8_sniff.py`): match whole words and say it on the
  Files tab.
- A child removed from a re-expanded index: nothing said (`s6` D).

**P3** — the size-only "same file" sentence; no note on an unchanged or
unknown probed child; the list of log-only skips in `SKIP_REVIEW.md` §2.

### 5.3 Deliverable for this group

`SKIP_REVIEW.md` §3 "the protocol in one page" (11 rules) goes into
AI_HANDOFF, amended by what this group changes (rules 4, 7, 8, 9 change), and
GETTING_STARTED gets a short "when is a file skipped, and when is it loaded
again" box in the user's words. Every decision in the table keeps a test.

---

## Group 6 — Features worth adding (decide each with the user's bar: only if it makes the product better)

What exists already was mapped against README, GETTING_STARTED and rounds 2–12
so nothing below duplicates a shipped feature. Ranked by value to one
consultant selling rate work from this app.

### 6.1 One-click PDF of any report (high value, low risk)

Every report is a self-contained HTML page the user prints from the browser.
Clients receive PDFs. The optional Chromium that `mrfx/render.py` already
installs on first use for JS portals can print any report to PDF
(`page.pdf()` from the HTML string, landscape where the print CSS says so,
the brand header kept). A **"Save as PDF"** button beside every "Open
printable" (pitch, negotiation one-pager, proposal, payer compare, dossier,
market report, rate card, visit value, packets' documents) and `--pdf` on
`mrfx packets`. Falls back to the HTML with a sentence when Chromium is not
available (invariant 3); the PDF is byte-stable except the date; named like
the HTML. Progress bar stage "Printing the PDF".

### 6.2 Medicaid floors and hospital parity inside the negotiation documents (high value; data already loaded)

`mrfx floors` and `mrfx hospital` load state Medicaid / workers'-comp
schedules and hospital standard charges, but neither reaches the negotiation
one-pager, the proposal or the dossier (grep: `floor`/`parity` appear in
benchmark.py and dossier.py only as caveat words). When loaded, add one line
per code: "Medicaid pays $X for this code in MO; Payer A pays you $Y (below
Medicaid)" and "the same payer pays Hospital H $Z for this code" — both are
the arguments a practice takes into a renewal. Sections omitted with the
reason when nothing is loaded (the dossier's existing rule). Known-answer
test with hand-worked rows.

### 6.3 Automatic backups from inside the app (high value for a billion-row store that took weeks to build)

`mrfx backup` exists but requires the server stopped. Add a weekly (configurable)
hot backup from `serve`: DuckDB `CHECKPOINT` then a consistent copy of
`mrfx.duckdb` plus the parts' manifest (or `ATTACH … COPY FROM DATABASE`),
to a folder the user names in config (an external drive), with retention
(keep the last N), a disk-space guard (never fill the drive: refuse with a
sentence), a Files-tab line "last backup: date, size, where", and `mrfx
verify` run on the result. Off by default until the folder is set; the
config comment explains it in the user's words.

### 6.4 Bookmarkable links and one search box (medium)

`clientJump` already carries a subject from the Clients tab to a view; expose
the same as the URL (`#benchmark?subject=…&state=MO`) so a client's benchmark
can be bookmarked and sent to oneself; and one header search box ("find a
practice") that opens the drawer or the dossier for a TIN, NPI or name
(reusing the materialised practice list and the NPI autocomplete from round
10). No new numbers.

### 6.5 Excel workbook deliverables (medium; one dependency)

Consultants hand clients spreadsheets. The rate card, fee schedule, visit
value, leads, roster and outreach exports as one `.xlsx` with the methodology
on its own sheet, numbers as numbers, headers frozen — `openpyxl`
(pure-Python, pip-audit clean). CSVs stay as they are. Decide after Group 3:
if the export path is rewritten to DuckDB `COPY`, the xlsx is a second writer
over the same rows.

### 6.6 Decide, do not assume: an "expected payment per visit" toggle

Owners ask what they will actually collect, not the contracted ceiling: the
Medicare multiple-procedure reduction on the second and later therapy units,
the assistant-modifier reduction (CQ/CO), patient cost share. The visit
calculators say today that real collections are lower. A toggle that applies
*stated* rules (Medicare's MPPR percentages, the 15 % assistant cut) with the
assumption printed in the headline would be useful **only if** the rules can
be stated per payer honestly; commercial payers vary. If this round cannot
source the rules per payer, leave it as the written caveat it is.

Not recommended (considered and rejected): a map view (needs internet tiles,
breaks the single-file offline report rule); a settings screen (audit 8 left
it open; the Files tab shows the values); alerts by e-mail (single-user local
app; the Clients digest and packets already carry "what moved").

---

## Carry-overs still open (do, or write why not — never drop silently)

Round 11's 3.1 single-pass rollup is **superseded by Group 1.3** (the
per-slice durable build is the same work done right); 3.6 segment map; 4.4(h)
monotonic clock through sleep; 4.4(c) filename cap (documented); round 10's
1.5 / 1.9 residue (past ~30 M rows — this user is at 1.16 B, so measure it on
the 99M store and decide); Windows has still never been run — if a Windows
box is available, the install → start → paste a link → update → Ctrl-C path is
the most valuable hour.

## Checked and correct in 0.7.6 (do not spend time here)

The zip (identical to c8b80fc, both launchers CRLF, fresh install, 0.7.5 →
0.7.6 update with a loaded store and byte-identical exports); every printed
number on two stores (936 + 99M hand checks: Benchmark percentiles and
positions, fee schedule and market medians, Negotiate and proposal, Explorer
strip, Code page ranking, Overview payer index and top codes, Markets
geography / negotiability / concentration, city map, leaderboard, roster,
outreach merge fields, visit value, entity drawer 637 rows, rate changes per
payer pair); determinism 133/135 byte-identical (two stamps); no 500 on 253
calls; 0 console errors across 14 views; the visit-value block's 34
hand-worked rules; round 12's protocol changes (0.7.5 `.val` and `.part`
resumed by 0.7.6; orphan claim vs a live ingest; current signed links stored;
`expanding` written before the conditional final writes; no new lock across
I/O; unique tiebreakers); `mrfx sources --check` 106 OK; pip-audit clean.

## Deliverables

1. One commit per group, in order 1 → 2 → 3 → 4 → 5 → 6 (6 may be split per
   feature); each message says what, why and the numbers that proved it, and
   names the items it closes.
2. `tests/mrfx/test_review13.py`: one test per item in Groups 1, 3, 4 and 5
   and per feature in 6, each FAILING on c8b80fc with the value it fails with
   in its docstring; the 14 progress tests of DESIGN §3.6.7.
3. Re-measure on the 99M store after Groups 1–3 with the same method
   (`docs/review13_repros/perf/driver.py`): the table in `perf/REPORT.md` §1
   before / after for every route touched; time to full coverage from an empty
   spine, kill-at-50 %-then-restart, peak RSS. Re-run the accuracy checks
   (`perf/check_real.py` on a three-file real store; `perf/spot99.py`) — **0
   differences** is the bar; where 4.2 changes a number, say which and why.
4. Rows unchanged through ingest (BCBS-MS, UHC-NY, Tufts via inbox and links;
   `fastcheck` 3/3), the round-11 kill harnesses once on the final code, and
   the round-12 and round-13 repro scripts re-run with what they print now.
5. Version 0.7.7; GETTING_STARTED "What changed in 0.7.7" in the user's words
   (the coverage line and the progress bar each get a paragraph with a
   picture of what they will see; the backup setting; the PDF button);
   AI_HANDOFF: the coverage contract, the durable rollup plan, the progress
   helper, the skip protocol in one page, the rules decided in 4.2 and 5.1;
   CLAUDE.md counts; `tests/slow_tests.txt` from a quiet full run.
6. Full suite green, ruff 0, pip-audit clean; the zip built and checked
   (version, CRLF, identical to the commit); this status block filled in.
