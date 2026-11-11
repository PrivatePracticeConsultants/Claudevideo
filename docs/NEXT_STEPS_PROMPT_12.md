# Prompt for the next session, round 12 — the 0.7.5 zip audited end to end; what is wrong, what is stale, and one new deliverable (written for Claude Opus 5.5)

> **STATUS: not started.** Baseline for every number and line reference below is
> commit 68d9bc7 (version 0.7.5), the zip the user was handed. When you are
> done, replace this block with what closed, in which commit, with the numbers.

## How this round was audited (so you can trust, and re-run, the evidence)

The shipped zip — not the working tree — was extracted into a clean folder with
its own virtual environment and audited as the user would use it:

1. **Fresh install from the zip**: `mrfx 0.7.5`, 1164 tests collected (562
   quick), ruff 0, pip-audit clean, both `.bat` files CRLF, zip == committed
   tree file for file.
2. **The user's update path**: a 0.7.4 install (`git archive 1064203`) with
   three real files loaded (1,130,194 rows), an edited `config/mrfx.yaml`, then
   `python -m mrfx update <zip>`: 3.0 s, 121 files, config kept byte-identical
   plus `.new`, backup zip written, no `.updating` left; `mrfx status` and a
   full `mrfx export` identical before and after (139,600 sorted lines, same
   sha256); the dashboard served the updated store with no error. Scripts:
   `docs/review12_repros/mine/update_path.py`.
3. **Live paths**: a real UHC link (2 MB) pasted on the Files route → `done` in
   4 s, 252 rows (the same bytes through the 0.7.4 inbox: 252); the same link
   again → skipped; the same bytes dropped in the inbox → "identical to …";
   downloads folder empty afterwards. A byte-identical real file under a
   browser-style second name in the inbox → skipped with the sentence. All 14
   dashboard views walked in Chromium with a loaded store: 0 console errors,
   0 failed requests, 0 responses ≥ 400. `mrfx sources --check` live from this
   box: 106 OK · 9 NEEDS-BROWSER · 30 PORTAL · 0 CHANGED · 5 DEAD.
4. **Every earlier prompt re-read and re-verified** (rounds 2–10, three
   independent passes; `docs/review12_repros/digest_*.md`): 574 pinning tests
   from those rounds green on the shipped copy; every claimed fix that touches
   numbers, states, messages or install paths re-run by hand or by its test.
   **One regression of an old rule** (1.1 below) and a list of items each
   round left open that are still open.
5. **An adversarial review of the round-11 diff** (1064203..68d9bc7, about
   2,400 lines; `docs/review12_repros/diff_review.md` and `agent_diff/`): every
   seam the diff touched probed with a scriptable local HTTP server. **No P0**
   (nothing changes a parsed number, doubles or drops rows); **four P1s**, two
   of them regressions against 0.7.4 (1.2–1.5 below). Each was re-run by the
   session lead before it was written here.
6. **The full suite from the extracted zip**: see the line at the end of this
   section (filled in when the run finished).

Everything below carries the script that reproduces it and what it printed on
68d9bc7. Severities: **P0** wrong number / data loss; **P1** stuck state,
hang, or a user action silently undone; **P2** waste, a misleading message,
or a documented behaviour the code does not have; **P3** cosmetic.

Full suite on the extracted zip: **1164 passed, 0 failed, 28:34** (box shared with four reviewers, so the time is an upper bound; `docs/review12_repros/mine/full_suite.txt`). The quick tier on the repo head: 562 passed, 4:50. `tests/mrfx/test_review11.py`: 49 passed.

---

## Group 1 — wrong numbers and stuck states (ship first, one commit)

### 1.1 (P0 by the rubric, narrow trigger) The NPI drawer's "current rate" chart uses a per-modifier window, not the contract rule

- **Where:** `mrfx/api.py:2039-2042`. The NPI-grain chart partitions the
  "newest month" window by `payer, tin_value, billing_code, modifier_set,
  billing_class, service_code_set` — finer than the house rule (newest month
  per payer × provider × code, `_NPI_LATEST_QUALIFY` at `api.py:833-834`,
  `store.LATEST_PARTITION` at `store.py:628`) and without `tin_is_really_npi`.
  Added by review 10 item 2.5, whose own text says "newest month per contract".
- **Repro:** `docs/review12_repros/agent_2_5/npi_drawer_latest.py` — one payer,
  one TIN/NPI, 97110; May no-modifier $40, June GP $42 (the same contract,
  the base-modifier spelling changed between months). On 68d9bc7:
  `/api/entity/npi/<npi>` chart **median_rate 41.0**; `/api/entity/tin/<tin>`
  chart **42.0**; Explorer (latest) **42.0**; Code page **42.0**. The
  superseded May row survives because its modifier set has no June successor.
- **Fix:** build `chart_rel` for the NPI grain from `_NPI_LATEST_QUALIFY`
  (payer, unit_id, tin_value, tin_is_really_npi, billing_code) and THEN apply
  the house-basis filter — the same order `rel_sql` uses. Sweep every other
  `QUALIFY file_month` outside `rel_sql` / `_TIN_REL_LATEST` (grep: `api.py:833,
  885, 2041`, `benchmark.py:394`; only 2041 deviates today) and pin them with
  one known-answer test that asserts the drawer chart, the TIN drawer, the
  Explorer and the Code page print the SAME number for this case. No test
  references the drawer `chart` today.

### 1.2 (P1, regression) An unquoted ETag makes every resume "the server's file changed": the download never finishes and the slot churns

- **Where:** `mrfx/fetch.py:655` `_same_validator`, used at `:994` (the 206
  check) and in the 416 path. `_write_val` (`:1096-1101`) stores
  `validator = strong ETag or Last-Modified` without recording WHICH it was;
  `_same_validator` guesses from a leading `"` or `W/`. An ETag sent without
  quotes (non-compliant but real: IIS-style `5d8c72a5edda8d6a:0`, some proxies
  and custom origins) is compared against the 206's `Last-Modified` date,
  never matches, the `.part` is unlinked and the download restarts from byte 0
  on every reconnect. The `bad` path resets the stall clock, so the loop is
  bounded only by `download_max_seconds` (3 h) and 2,500 connections; the
  set-aside then requeues the row and the next turn repeats it.
- **Repro:** `docs/review12_repros/agent_diff/s1_unquoted_etag.py` (unquoted
  ETag + Last-Modified, Range honoured, every connection cut after 300 KB,
  1.3 MB file, `download_max_seconds=45`). 68d9bc7: **20 requests, 5.9 MB
  served, FAILED** "paused after 45 s and no data downloaded … nothing is
  re-downloaded" (both halves false: 5.9 MB were served, everything was
  re-downloaded, the `.part` was deleted). 1064203: **OK in 5 requests, 4.1 s.**
- **Fix:** record the validator's KIND in `.val` (`{"etag": …,
  "last_modified": …}` or a `kind` field) and compare like with like; when the
  response carries no header of the stored kind, prove nothing (as the
  docstring already says). Treat an unquoted ETag as an ETag. Make the
  set-aside sentence read `highwater` AFTER the path that zeroed it, or say
  "starting over" when the `.part` is gone (`fetch.py:1386-1394`). Regression
  test: unquoted ETag + Last-Modified + one cut connection finishes with one
  resume; the 0.7.4 count (one resume per cut) is the bound.

### 1.3 (P1) `mrfx ingest` hangs forever when a link row was left `ingesting` by a killed server and the same bytes are in the inbox

- **Where:** `mrfx/ingest.py:1659-1695` `_content_claim` (`while True: …
  time.sleep(2.0)` while the twin's url row is `ingesting`, no timeout, no stop
  check); `store.py:3205` `claim_inbox_content` treats `url_queue` rows in
  `('done','ingesting')` as owners. `recover_stuck_urls` (`store.py:3342`),
  the only thing that flips an orphaned `ingesting` url row back, runs only
  inside `run_queue` (`fetch.py:3306`); `cmd_ingest` (`cli.py:614-623`) calls
  `recover_stuck_files()` only. The 403 message sends the user exactly here
  ("download it in your browser and drop it into data/inbox/").
- **Repro:** `docs/review12_repros/agent_diff/s2_cli_ingest_wait.py`: one url
  row `ingesting` with `content_sha` = sha(body), the same bytes in the inbox,
  then what `cmd_ingest` does. 68d9bc7: "the same bytes are being loaded right
  now … waiting"; **scan never returns** (20 s join), url row still
  `ingesting`, inbox file stuck `processing`; every inbox file behind it is
  never loaded. (In `serve` the race is bounded at ~2 s by the URL worker.)
- **Fix:** every CLI path that opens the store without a server
  (`cmd_ingest`, `cmd_add` local mode, `fastcheck` if it claims) calls
  `recover_stuck_urls()` beside `recover_stuck_files()`; `_content_claim`
  waits only while the twin is provably alive in THIS process (a url row
  `ingesting` that no processor thread holds is an orphan: load, do not wait)
  and checks a stop/shutdown flag in its loop. Test: the repro ends `done`
  within seconds.

### 1.4 (P1) The re-expansion re-check (round 11 item 1.7) probes the child's OLD stored URL, so an index whose child links carry a per-visit signature never detects a republish

- **Where:** `mrfx/fetch.py:2767-2781` `_refresh_loaded_children` →
  `store.done_rate_rows(...)` (`store.py:3433`, returns the stored `url`) →
  `_server_now(cfg, r["url"])`. `_enqueue_one` (`store.py:2664`) returns None
  for a `done` child without refreshing its `url`, so the fresh link the index
  just handed over (same `dedup_key`, new `sig=` / `se=` / `X-Amz-Signature=`)
  is dropped. The Azure-SAS / CloudFront / S3-presigned shape — the one
  `dedup_key` was built for — is exactly the shape where the stored URL is
  dead by next month: the probe gets a 403, `_republished` returns None,
  nothing is requeued, the row goes back to `done` with no note.
- **Repro:** `docs/review12_repros/agent_diff/s4_refresh_old_url.py` A (index
  hands out `?sig=S2`, the server answers 403 to the old `S1`, child
  republished) vs B (signature constant). 68d9bc7 A: requests
  `[GET /index.json 200, GET /child.json.gz?sig=S1 403]`, **after refresh
  still 2026-05 / 50 rows**; B: 206 + 200, 2026-06 / 80 rows.
- **Fix:** `_refresh_loaded_children` already has the index's CURRENT `urls`:
  probe and re-download with those, keyed by `dedup_key`; `_enqueue_one`
  refreshes `url` on a `done` child too (a revived row already gets
  `url = ?`); the `unknown` branch and `requeue_republished` use the current
  url as well. While there: `_server_now` (`fetch.py:2494`) opens a fresh
  default-UA client per child with no browser-UA / chain-repair fallback (the
  two levers `_download_reserved` has) and no cap on total time — 1,000
  children against a black-holing host is ~1 h of a blocked processor thread
  (`connect=30 s` × 1,000 / 8). Reuse the download client settings and bound
  the whole re-check. Test: variant A loads June.

### 1.5 (P1) "Clear queue" pressed during the re-check window is undone — the index flips back to `done` and every republished child is downloaded and loaded

- **Where:** `mrfx/fetch.py:2977` (`_refresh_loaded_children` runs AFTER the
  `only_if_expanding` enqueue step that round 11 item 2.3 protected; one
  request per loaded child, seconds to minutes against a real host) and
  `:3009` (unconditional `store.update_url(url_id, status="done",
  kind="toc", …)`); `requeue_republished` (`store.py:3447`) requeues `done`
  children regardless of the parent's status.
- **Repro:** `docs/review12_repros/agent_diff/s7_clear_during_recheck.py`: six
  loaded children, all republished, the probe slowed to 1.5 s, `clear_queued()`
  1 s into `expanding`. 68d9bc7: right after the clear the index is
  `skipped/canceled`; **12 s later it is `done`** and all six children were
  re-downloaded and reloaded (`file months: [('2026-06', 6)]`).
- **Fix:** re-read the parent's status after `_refresh_loaded_children` and
  before any requeue or the final `done` write; make `requeue_republished`
  conditional on the parent still `expanding`; the same for the
  `cancel_all_downloading` case. Test: the repro ends with the index
  `skipped/canceled` and the children untouched.

---

## Group 2 — waste, misleading states and open decisions (P2; one commit)

### 2.1 A Medicare fee-schedule upload with a non-finite rate is accepted, then every anchored screen fails with a 422 until a new anchor is uploaded

- `mrfx/api.py:4525` `float(rate_raw)` with no `isfinite`. Repro
  `docs/review12_repros/agent_6_8/mpfs_probe.py <scratch dir>` (run from the
  repo root): `POST /api/mpfs/upload` with `97110,00,nan` → **200
  {"rows":1}**; then `/api/summary` → **422 "cannot convert float NaN to
  integer"**, `/api/code/97110` → 422 the same, `/api/benchmark/market` →
  **422 "Out of range float values are not JSON compliant: nan"**. Round-7
  P2 item, never closed. Fix: refuse `nan` / `inf` / `<= 0` rates at upload
  with a line-numbered sentence, and treat a non-finite stored anchor as "no
  anchor" in `_with_anchor`. Test both.

### 2.2 After a crash, a link that deferred to an inbox load stays `skipped` with a false "being loaded from the inbox right now" and nothing revives it

- `store.py:3372` `revive_pred` needs a FAILED url twin; `store.py:3161-3180`
  skips the link with `duplicate_of = <inbox name>`; `ingest.py:1203-1215`
  revives only in the inbox load's `finally`, i.e. only if that process lives.
  Repro `agent_diff/s5_crash_inbox_claim.py`: after the restart
  `recover_stuck_files` marks the inbox file `failed`, `recover_stuck_urls`
  revives 0, the link row still says "being loaded from the inbox right now —
  will retry automatically", the kept download is still there, nothing
  retries. If the user removed the inbox file, that content never loads.
  Fix: at start-up revive `skipped/duplicate` url rows whose `duplicate_of`
  names a files row that is not `done` and not in flight (the in-memory claim
  died with the process, so no inbox load can be "right now").

### 2.3 The first monthly refresh after the upgrade re-downloads and re-parses every child loaded by 0.7.4, even when the server says nothing changed

- `fetch.py:2788-2800`, the `unknown` branch: a 0.7.4 row has no
  `server_validator` / `server_size`, so it is sent to a full re-download —
  although `content_fingerprints.size` (written by `_finish_ok` for every
  load) and the probe's `total` are both known. Repro
  `agent_diff/s4_refresh_old_url.py` C: same bytes, same ETag on the server;
  the refresh does `206 + 200` and re-parses to the same 50 rows. On a
  national book that is hundreds of GB and the parse is the expensive half.
  Fix: fall back to `content_fingerprints.size` (and the sidecar's size) as the
  "then" size before declaring a row unknown; only rows with neither go to the
  full re-download. Test: variant C makes one probe and no download.

### 2.4 Re-pasting a link with a fresh signature does not refresh a `queued` / `paused` row's URL

- `store.py:2631-2663` `_enqueue_one` revives (and rewrites `url`) only for
  `failed` / `skipped` / `oversize` rows and a done index; a `queued` or
  `paused` row returns None with its old url. A signed-CDN backlog that
  outlives its signature (the user pasted 400 links, the last 200 start
  tomorrow) still 403s after the user re-pastes the fresh list. Round-7 item,
  still open. Fix: on a direct paste, `UPDATE url_queue SET url = ?` for any
  row that is not in flight (`downloading` / `ingesting` / `expanding`),
  whatever its status; a row already downloading finishes with the url it
  started with. Test: a queued row's url changes on re-paste; a downloading
  row's does not.

### 2.5 Two files of one payer and one month under different names are pooled, not superseded — decide the rule and say it

- `store.py:574-576`: the spine groups by (payer, tin, code, modifiers, class,
  …) and pools every file of that month (`source_count`, `rate_variants`);
  there is no supersede rule and no warning. A payer that republishes a
  corrected file under a new name in the same month averages with the file it
  corrects. Legitimate same-month multi-file payers (BCBS-MS `01_of_10` …) must
  keep pooling. Round-7 item, still open; touches sold numbers. Decide with
  evidence from the real files on this box: at minimum a Files-tab and
  methodology warning "payer × month has N files, M practices priced in more
  than one" (`quality.py:157` is per payer only today); better, newest file
  per contract when the same (payer, tin, code, modifiers, class) appears in
  two files of one month with different rates — written as ONE rule in
  AI_HANDOFF and pinned.

### 2.6 The shipped config caps DuckDB at 8 GB while the docs say to leave it unset

- `config/mrfx.yaml:212` `duckdb_memory_gb: 8` (the comment says "sweet spot on
  a 32 GB machine"); `GETTING_STARTED.md:1477` "Leave it unset to let the app
  pick a kernel-safe default". On a 16 GB laptop the auto rule never runs on a
  fresh install, and 8 GB sits beside the parser workers' budget. Ship the key
  commented out, make the auto rule subtract the workers' budget, and add the
  documented keys that are absent from the shipped file (`max_toc_files`,
  `delete_raw_after_ingest`, `monthly_refresh`, …) as commented examples,
  with a test that every key the docs tell the user to set appears in
  `config/mrfx.yaml` at least commented.

### 2.7 New-clinic and closure feeds apply the radius after a nationwide `LIMIT limit*5`

- `mrfx/nppes.py:205-236`: `window = limit * 5` rows ordered by date
  nationwide, THEN the haversine filter in Python. A 25-mile search with no
  state sees only the newest 1,000 events in the country and flags `capped`
  instead of answering. Round-7 item. Fix: filter on miles inside the SQL
  before the LIMIT (the referral-leaders query already does); pin with a
  fixture where the nearest events are older than 1,000 distant ones.

### 2.8 A skipped twin keeps its bytes in `data/processed/`

- `ingest.py` ~1955 → `_finish_file(cfg, p, ok=True)` moves the byte-identical
  inbox file to `processed/` beside the original (seen live: `moda … (1).json.gz`
  next to `moda ….json.gz`). For a 20 GB twin that is 20 GB kept for nothing;
  `processed/` is never pruned. Fix: when the twin is identical (same sha) to a
  file already in `processed/`, delete the twin's bytes and say so in the
  skipped sentence; otherwise keep today's move. Test the sentence and the
  folder.

### 2.9 The one-time stored-row migration has no time budget (round 9 item 2.1, third bullet, marked closed but not done)

- `store.py:1486-1578` `_migrate_row_rules` prints progress in batches of 40
  but opens the dashboard only after the whole scan; no budget, no background
  continuation, and no written rule in AI_HANDOFF. Only bites on the next
  `ROW_RULES_VERSION` bump, on the user's external drive — which is where
  "it sits silent" came from in round 9. Decide the rule (dashboard first,
  then migrate in the background with a Files-tab line; or a hard budget with
  a resume), write it in AI_HANDOFF, implement it, pin it.

---

## Group 3 — the new deliverable: visit value on the Rate card, by state and discipline (one commit)

The user asked for this in round 12. Build it on the existing pieces: the
standard-visit baskets in `mrfx/visit.py` (`PRESETS`, `define_visit`,
`price_visit`), the practice basis in `benchmark.practice_basis`, the entity
rule (a practice's rate for a code = median across its member TINs), the
mid-rank percentile in `quality.midrank_pct_sql` / `leads.py`, `rounding.half_up`,
and the Rate card tab (`index.html:632-670`, `app.js` `initRatecard`,
`/api/report/ratecard`, the CSV route).

### 3.1 What it shows

A **"Visit value in this market"** block on the Rate card tab, in the printable
card and in the CSV:

- **Inputs:** State (required) and Discipline (PT / OT / SLP, required) — the
  block is confined to those two and nothing else; the visit basket (the
  existing presets, default `ortho`, or the custom lines the Consulting
  visit card already accepts); as-of month (default "latest"); payers
  (optional, limit to these). **The subject practice is optional.**
- **Population:** every practice in the state with the discipline, on the
  house basis (dollar rates, professional, base modifiers, each contract at
  its newest month, `tin_is_really_npi` excluded) — the same population the
  Benchmark's market uses. A practice × payer pair counts only when the payer
  prices that practice on **every code of the basket** (a partial basket is
  not a visit); report how many pairs were excluded as incomplete.
- **Numbers, per payer and for all payers:** number of practices, **average
  (mean)**, **median**, **P25**, **P75** of the per-practice visit value
  (Σ units × the practice's rate for the code). The all-payers row is a
  distribution over practices where each practice contributes the median of
  its per-payer visit values — never a sum across payers (round 7 P0.2:
  cross-payer totals are not additive). Cells with fewer practices than the
  house minimum (`_MIN_CELL`) are suppressed with the usual note.
- **With a subject:** the subject's visit value per payer (from `price_visit`),
  then against that payer's distribution: percentile (mid-rank, subject
  included, labelled "this practice included" as Leads does), dollars and %
  versus the mean and versus the median, and which quartile it falls in; plus
  the same against the all-payers row.
- **Labels (honesty):** state, discipline, as-of rule, basket definition and
  units, basis note (`benchmark.basis_note_for`), practice counts and
  exclusions, "mean" spelled out as such next to "median". Refuse (a sentence,
  not an empty table) without a state or a discipline, and when no payer
  reaches the minimum.

### 3.2 Rules it must keep

Every printed number through `half_up`; every ranked or listed row with a
unique tiebreaker (payer, then practice key); the same numbers on screen, in
the printable card and in the CSV; the subject rule of round 9 (whoever the
user named, out-of-state TINs are the subject's own); generation caching like
the other Benchmark readers; no new per-request scan of raw rates — build the
per-practice visit values from the materialised practice medians
(`benchmark.py:547` `practice_medians_v1`) or a sibling table keyed by spine
generation.

### 3.3 Tests

A known-answer case with hand-worked numbers: three practices, two payers, the
`ortho` basket, one practice missing one code for one payer (excluded there),
a subject at a known percentile; assert mean, median, P25, P75, n, the
exclusion count, the subject's percentile / quartile / % vs mean and median;
CSV == screen == printable; determinism (two generations byte-identical);
refusal without state or discipline. Add the block to
`tests/mrfx/test_determinism.py`'s snapshot set.

---

## Group 4 — cosmetic, documentation, catalog, carry-overs (one commit)

### 4.1 Small code items (P3)

- (a) Markets' state ranking can list military mail codes: `market.py:86-100`
  unnests `tin_directory.states` with no `STATE_CODES` filter (the state list
  at `store.py:5409` has it). One line; pin with an NPI saved under `AE`.
- (b) Stale `splices` survive a restart-from-zero (`fetch.py:1001-1008`,
  `:1215-1224` clear `highwater` and the `.part` but not `splices`) → extra
  sample requests on the fresh download (`agent_diff/s8_stale_splices.py`).
  `splices = []` wherever the `.part` is unlinked.
- (c) `.part.val.tmp` left by a kill between write and rename
  (`fetch.py:650-652`); the start-up sweep names `.fetchmeta` and `.part` but
  not `*.val.tmp`.
- (d) `_identity_of` (`ingest.py:1624`, `:1683`) now reads a ≤ 4 GB inbox file
  in full BEFORE the parse: minutes of `processing` with no progress on a USB
  drop. Put a progress note on the files row ("reading the file's identity,
  N of M MB") or feed the hash from the parse stream.
- (e) `mrfx status` opens the store four times (`cli.py:740-747` plus the
  code-coverage, Medicare and reference printers at `:695`, `:725`): the
  "analytics memory budget" INFO line prints 3× after the table. Open once,
  pass the Store.
- (f) A complete download without its sidecar is accepted with no server
  check and the row shows **0 of 0 bytes** (`r11/dest_no_sidecar.py`: fetched,
  requests `[]`, bytes_done 0, bytes_total 0). Rebuild the sidecar from the
  file (size, sha) at that moment.
- (g) The fresh-store log still says "incremental analytics update unavailable
  (first analytics build of this store) — running a full rebuild" although
  round 11 item 3.10 goes straight to the full build; say that.
- (h) On POSIX every `serve` stop prints "resource_tracker: There appear to be
  5 leaked semaphore objects" (the pool's queues); close them on shutdown.
  Not visible on Windows.
- (i) `/api/stats` and `/api/boot`: log the duration when over ~5 s so the
  38–60 s first call seen twice in round 10 leaves a trace in `serve.log`.
- (j) `httpx2` is still not installed (`requirements.txt:22-25` records it):
  a `StarletteDeprecationWarning` on every TestClient import, an import
  failure on the next starlette bump. Test-only; decide now.

### 4.2 Catalog (`config/known_sources.yaml`), from the live `--check`

- Mass General Brigham Health Plan: HTTP 404 (URL moved) — find the new page
  or mark it with the reason. Midlands Choice: TLS chain incomplete on their
  server (`CERTIFICATE_VERIFY_FAILED: unable to get local issuer certificate`)
  — never weaken TLS; say in the entry's detail that the user must open it in
  a browser. BCBS South Carolina 403 and Western Health Advantage 429 were
  from this box's address (bot gates) — re-check from the user's network
  before changing anything; Jefferson 502 was the proxy.
- `README.md:345/363` say 110 auto-queueable / 34 portals; the catalog has
  150 entries, 109 queueable, 41 portals. Generate those two numbers from
  the catalog in the test that already checks `starter_links.txt`.

### 4.3 Documentation that now describes the old behaviour (each a one-line fix; cite the line in the commit)

- `docs/AI_HANDOFF.md:477` "one GET per file" and `GETTING_STARTED.md:1211`
  "Each file is requested once" — since 0.7.5 a small-file slot sends a 1-byte
  size probe first (`AI_HANDOFF.md:671, 695` describe it; `test_review9.py:913-916`
  allows 1–2). The next round must not "restore" one-GET behaviour.
- `docs/AI_HANDOFF.md:997` and `:1117-1118` still describe the Explorer's
  pooled-months default; round 10 made "latest" the default
  (`index.html:180`, `app.js:1066-1068, 1123`). `HOW_TO_RUN.md:255` and
  `GETTING_STARTED.md:1103-1105` are right.
- `docs/AI_HANDOFF.md:69` says `size_premium` uses `percent_rank()`; the code
  uses `midrank_pct_sql` (`quality.py:256-263`) and `AI_HANDOFF.md:41` says
  percent_rank is no longer used anywhere.
- `docs/AI_HANDOFF.md:949` "~3-min inner loop" vs `CLAUDE.md` 4.5 min / the
  round-11 status 4.7 min.
- `docs/AI_HANDOFF.md:1257` states the NPI-view guard as a flat 40 M rows;
  since 0.7.4 it is `min(40M, memory_gb × 2.5M)` (`api.py:478, 1702`; line
  560 has the newer rule).
- `config/mrfx.yaml:196` and `GETTING_STARTED.md:764` "`0` turns parking off"
  after `download_park_limit: 6` — `download_park_limit` is `ge=1`
  (`config.py:279`); `download_park_seconds: 0` is the switch.
- `GETTING_STARTED.md:252, 810, 1045` and `README.md:388, 407` tell the user to
  set `max_toc_files` / `delete_raw_after_ingest` "in config/mrfx.yaml";
  neither key is in the shipped file (see 2.6).
- `GETTING_STARTED.md:569-575, 606-608` timing table is still the 4-core Linux
  measurement, labelled so; leave it or re-measure on a box like the user's.
- `docs/NEXT_STEPS_PROMPT_9.md` status block claims 2.1's third bullet closed
  (see 2.9) and "1 GET per file in every run"; note the correction in its
  status block rather than rewriting history.
- The `url_progress` docstring in `store.py` still says "a multi-minute rollup
  rebuild holds write_lock the whole time"; the heavy build runs unlocked
  (`_rebuild_rollups_locked` takes the lock only for `_register_views` and the
  swap).

### 4.4 Carry-overs from round 11's own "left open" list (decide, do, or write why not — do not drop them silently)

- **3.1 single-pass rollup build** (`COPY … PARTITION_BY`): the rollup is still
  ~85–89 s per 32 M rows after each queue, half the wall time of a fresh
  store. Needs its own equivalence run (byte-identical spine and directory on
  the 32 M and 99 M stores, the OOM ladder, the slice transaction). If this
  round cannot carry it, say so in the status block with the measured number.
- **3.6 segment map** (keep finished segments when one fails; segments on a
  restart after a kill). Off by default (`download_segments: 1`).
- **4.3 one rollup per idle gap in serve.** **4.4(h)** monotonic clock through
  laptop sleep. **4.4(c)** the filename cap — the Windows long-path option is
  documented instead; keep that unless a real path fails.
- **Round 10 1.5 / 1.9 residue** (serve the cached unfiltered page when a
  two-letter text matches every practice; remember the unfiltered page; order
  the name sort on the practice list) — only past ~30 M rows, which this user
  is heading toward.
- **Still never run on Windows** (every round since 2): the launcher, the
  updater, `replace_with_retry` under an antivirus lock, `inbox/.held` and
  `.replacing` folders, the `.bat` files under cp1252, `_nt_alive_without_handle`.
  It is the user's only platform. If a Windows box is available to the next
  session, the install → Start → paste a link → update → Ctrl-C path is the
  single most valuable hour of this round; if not, say so again.

---

## Checked and correct in 0.7.5 (do not spend time here)

The fresh install, the 0.7.4 → 0.7.5 update with a loaded store (exports
byte-identical), a live UHC link through the queue, the same link re-pasted,
the same bytes in the inbox, a real byte-identical twin under a second name,
all 14 dashboard views in Chromium, the live catalog audit; the modifier
rules, the SSN mask (SQL == Python over all 100 prefixes), the month rule, the
UTC clock, the Medicare locality refusal, "latest" before filters at the
Explorer / Code / TIN / entity grains, the practice basis, the determinism
tiebreakers (every `mode()` ordered), the `is_latest` column and migration,
`half_up` == DuckDB `round()` with zero Python `round()` in 26 output modules,
one masking rule on every surface, per-payer months in packets / digest /
new-to-network, the Files-tab ordering, the 409 "superseded" interruption,
`/api/boot`, the entity two-pass, ref ids above 2^53, wrong-typed input →
422 sentences with no SQL or path, the idle CPU (0.45 % of a core), the
start-up read counts on a 2,000-part store, the lanes (no starvation), the
1-byte probe against Range-ignoring / chunked / 416 / gzip servers (one 64 KB
chunk, then close), the parent watchdog (never fires on a live parent, not
armed under `mrfx ingest`), the content claim's symmetry under one lock, the
part-swap retry middleware's position, Retry-After cap / clip / date handling,
`recover_stuck_files` flipping `queued`, the oversize re-queue, no lock held
across I/O in the new code, no new per-row cost, and 574 + 83 pinning tests
from rounds 2–10 green on the shipped copy. Scripts under
`docs/review12_repros/`.

---

## Deliverables

1. One commit per group, in order 1 → 2 → 3 → 4; each message says what, why
   and the numbers that proved it, and names the items it closes.
2. `tests/mrfx/test_review12.py`: one test per item in Groups 1–3 (and 4.1 a–f),
   each FAILING on 68d9bc7 with the value it fails with in its docstring; the
   known-answer visit-value case of 3.3.
3. Re-run the repro scripts under `docs/review12_repros/` after the fixes and
   record what they print now in the status block. Re-run round 11's kill
   harnesses (`docs/review11_repros/queue/s5_crash.py`,
   `ingest/s4_kill.py`, `download/c10_kill.py`) once on the final code.
4. Before / after rows unchanged: BCBS-MS, UHC-NY and the Tufts zip through
   the inbox and through links, each part's row count and the sha256 of its
   sorted rows identical (`docs/review11_repros/…/rowid.py` pattern);
   `fastcheck` 3/3 identical.
5. Version 0.7.6; `GETTING_STARTED.md` "What changed in 0.7.6" in the user's
   words (the visit-value block gets its own paragraph with an example);
   AI_HANDOFF: the rules from 2.5, 2.9 and 3.2 written down; the stale lines
   in 4.3 fixed; CLAUDE.md test counts; `tests/slow_tests.txt` regenerated on a
   quiet box.
6. Full suite green, ruff 0, pip-audit clean; the zip built and handed over
   with the three-step update instruction; this status block filled in.
