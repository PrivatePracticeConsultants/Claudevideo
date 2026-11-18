# Are the analytics actually adding newly loaded files? Live audit of MRF Explorer 0.7.7

Round 14 · Nov 12, 2026 · repo `/home/user/Claudevideo` at 9617c59 (branch claude/gracious-mccarthy-ao7vmo), **read-only**: nothing in the repo was edited, committed or pushed.
Scratch: `/tmp/claude-0/-home-user-Claudevideo/4633d594-0139-5f8e-a935-ae9c76b72000/scratchpad/audit14/analytics/` (called `AN/` below).

Every scenario drove the real 0.7.7 program: `mrfx --config <ABSOLUTE path> serve` as a subprocess, plus `mrfx update`, `mrfx forget`, and the in-process `ingest_file` only to build the base stores. All checks were made from outside:
- polling `/api/stats.analytics`, `/api/payers`, `/api/rates?grain=tin`, `/api/code/97110` and `/api/files` every 0.3–1 s;
- after each stop, a read-only **truth check** (`h.truth`): the spine (`rates_by_tin_tbl`) compared row for row (`EXCEPT ALL`, both ways) with `SPINE_QUERY` rebuilt from the raw parquet parts; for every done file, how many of its grain keys are missing from the spine, and `files.rows_emitted` against its part's rows; the spine's payers against the raw payers; and an md5 of the sorted export of `rates_by_tin_tbl` and `tin_directory_tbl` (a DuckDB set-hash on the 79 M-row table).

**Timings are upper bounds.** The 0.7.7 zip's full test suite and other agents' jobs were running on the same 4-core, 15 GB box the whole time (load average 3–4).

Real payer files used: BCBS (ND/WY books), Moda TX, Tufts subsidized zip, Highmark WV, Security Health Plan zip, UHC/HealthScope. The base store holds 6,768,604 rates, 444,992 spine rows and 7 payers. Synthetic payers (`h.gen_mrf`) were used where a test needed a payer I could control.

## 1. The plain answer

**Yes. Every file that finished loading in these runs reached the analytics: exactly once, completely, and with no stray rows.** That held for 20+ live scenarios: pasted links, inbox drops, kills at five different moments, a failing rollup, the 0.7.6 → 0.7.7 upgrade, removals, a replaced file, and the 99 M-rate store. Once the header said "current", the truth check found 0 missing and 0 extra spine rows, and the payers in the spine matched the payers loaded. After a kill-and-resume the spine was byte-identical to an uninterrupted build. On the upgrade you will take today, a 0.7.6 store stuck at "3 of 7 payers" folded everything in on the first 0.7.7 start, with no clicks, in 32 s. The header never said "current" while a file was missing.

What is *not* fine:
- **It can be slow to fold in, and it doesn't tell you when.** While the queue is busy, new files go in only every 4 × (last fold-in time), at least 90 s. The header meanwhile says they will be "added when the queue is idle".
- **It can stall completely in one case.** A part-way or failed analytics build waits for the queue to go idle, so one long or stalled download blocks it. I watched it sit for 20 minutes.
- **During a full rebuild the header and every document's methodology note under-state coverage** ("0 of 7 payers" while 6 are fully there).
- **The progress bar sits at 0 % for the whole of an ordinary fold-in.**

None of these loses or invents a number.

## 2. Scenario table

"Done→covered" = from the file showing *done* in `/api/files` (or `/api/stats.done_files`) to `/api/stats.analytics` reading current with every done file covered. The coverage object is cached for 3 s, so values under 3 s are at the resolution of that cache.

| # | Scenario | Expected | Observed (live) | Done→covered | Header truthful? | Verdict |
|---|---|---|---|---|---|---|
| A | Fresh store, 2 payers pasted (local HTTP) | first full build, both covered | behind → building → current; sliced first build (4 slices) ran on the drain; `/api/rates` 0→2,200; Alpha 1,200 / Bravo 1,000 rows | 0.9 s | Yes. One 0.5 s sample read "2 of 2 payers (0 of 2 files)" mid-build (P3-4) | PASS |
| B | Idle and current; paste a NEW payer | payer-slice incremental | "recomputing 1 payer slice"; Charlie 0→800 rows; picker 2→3 covered | ≤1.1 s | Yes | PASS |
| C | New file of an EXISTING payer (link) | that payer's slice only | Alpha 1,200→1,800 rows; header behind 0.5 s → current | 0.7 s | Yes | PASS |
| D | Inbox drop while serving: new payer, then existing payer | watcher → incremental | Delta 0→480; Bravo 1,000→1,400; one incremental per scan | ≤3 s each | Yes | PASS |
| E1 | Kill after "done", before the fold-in (inbox; and link) | restart folds it in with no activity | at the kill: "behind, 6 of 7 payers (7 of 8 files)". Restart: the catch-up thread ran the incremental | 1.4 s (inbox) / 1.9 s (link) after restart | Yes | PASS. Byte-identical to ref8 (inbox); link run 0 missing / 0 extra, directory identical, spine differs only by the `6d82a214_` filename prefix the URL path gives `source_files` |
| E2 | Kill inside an incremental (after its DELETE, before COMMIT) | rolled back, redone | "building … Adding the newly loaded files" at the kill; restart redid it | 1.0 s after restart | Yes | PASS. Byte-identical to ref8 |
| E3 | Kill a sliced full build at 31 % of slices (18/58, 55 % of work) | resume at slice 19 | log "resuming at part 19 of 58"; payers appear one by one (0→1→4→5→6→7) | 9.1 s after restart | Yes | PASS. Byte-identical to ref8 |
| E4 | Kill a sliced full build at 71 % (41/58) | resume at slice 42 | "resuming at part 42 of 58" | 6.1 s after restart | Yes | PASS. Byte-identical to ref8 |
| F1 | 25 links, one every 10 s; downloads 14 s each; a 30 s fold-in emulated (the queue never idle for 6 min) | folded in during the grind | fold-ins at 88 s, 240 s (= 4 × 31 s after the previous one ended), then 336 s at idle. Per-file gaps 33–177 s (median ~95 s) | max 177 s | Partly: header said "N newly loaded files … added when the queue is idle", but they were added mid-grind | PASS on data; P1-2 / P3-1 on wording |
| F2 | Part-way full plan (killed at 41 %) + the same busy trickle | plan continues while busy | the start-up catch-up resumed it at once: 34 slices in 40 s alongside the trickle. Files that finished during the plan went in at the next refresh (90 s floor) | 1.5–77 s | Yes | PASS |
| F3 | Part-way plan whose start-up catch-up FAILS, then one slow / stalling download | retry when the failure clears | the failure cleared at 0 s; **the plan never resumed in 20 min** (download parked and re-woken 3×); spine 72,398 of 444,992 rows, 2 of 7 payers | never (20 min, run ended) | Says "failed … retries when the queue is idle"; true, but the queue never idles | **P1-1** |
| G1 | OOM injected in every slice; cleared after 2 failed attempts | failed + sentence; back-off; recovers; nothing lost | failed at 7.8 s with "ran out of memory at 2 GB — it retries…"; retries at +120 s and +240 s (back-off 2, 4 min); current at 381 s. 0 missing / 0 extra; directory = ref8. The incremental OOM turned into a **whole-store full plan** (14 → 24 slices after halving) | 253 s after the failure stopped (back-off) | State yes; **payer count wrong**: "0 of 7 payers" while `/api/payers` listed 6 covered (P2-1) | PASS on data; P2-1, P2-2 |
| G2 | `duckdb_memory_gb: 1` (config floor), never-built 6.77 M-rate store | build fits or says why | no real OOM; sliced build 12.7 s, peak RSS 852 MB; byte-identical to ref8 | 12.7 s | Yes | PASS (OOM could not be provoked at this size) |
| H | **0.7.6 install** (git archive c8b80fc, own venv), 8 real files, 0.7.6 rollup failing → "giving up for this run"; then `python -m mrfx update mrfx_project.zip`; then serve | first 0.7.7 start folds everything in | 0.7.6 left the spine at **3 of 7 payers, 190,445 of 444,992 rows** (the user's state). Update rc 0, 110 files, settings kept. First answer at 4.0 s: "building — 3 of 7 payers (3 of 8 files)"; current at 28.9 s; picker 7/7 covered; code 97110 now ranks 4 payers | 31.6 s from start | Yes, but the bar sat at 0 % the whole 28 s (P3-2) | **PASS** |
| H2 | 0.7.6 leftover `rollup_needs_full=1` + 1 uncovered file, first 0.7.7 start | sliced full build, current | 14-slice full build, 15.5 s; byte-identical to ref8 | 15.5 s | Under-states: "0 of 7 payers (7 of 8 files)" at the start of the build (P2-1) | PASS on data; P2-1 |
| I1 | Remove 1 of BCBS WY's 2 files (dashboard DELETE) | its rows leave, the payer stays | BCBS WY 133,072 → 76,095 rows; 7→6 files; payers unchanged | 0.1 s after the DELETE returned (~3 s inside it) | Yes ("building … Adding the newly loaded files" during the removal; P3-3) | PASS |
| I2 | Remove Moda's only file | Moda leaves the pickers and the code page | Moda 82,546 → 0; dropped from `/api/payers` and from 97110's payer rank | 0.1 s | Yes | PASS |
| I3 | Same-name, different-content inbox drop → needs_choice → "replace" | old rows out, new rows in | held as `zeta_rates-526e6e72.json.gz` (header current meanwhile); after replace, Zeta 400 → 160 rows and median 79.09 → 148.61 | ~2 s | Yes | PASS. 0 missing / 0 extra |
| I4 | `mrfx forget` with the server stopped, then restart | spine updated by the CLI itself | the CLI ran the payer-slice update (1 s); restart found nothing to do | — | Yes | PASS |
| J | Bulk NPPES enrichment (47,565 NPIs) + monthly re-check (a pasted index, one child changed) + inbox drop + pasted link | no stacking, no starvation, no false current | the re-check re-queued 1 changed child → folded in 0.5 s; both new files covered in ≤4 s; names rebuilt at 115 s; 0 false-current samples; 0 missing / 0 extra | ≤4 s | Yes | PASS |
| J2 | Inbox drop + pasted link INSIDE a name refresh held 60 s | coalesce, then fold in | the inbox refresh **skipped** (log: "the running one covers it", which is not true); the link's credit folded in both right after the name build | 55 s (= the hold) | Yes | PASS |
| J3 | Same with ONLY the inbox drop | fold in after the name refresh | covered **117 s after the name refresh ended** (the idle stale probe, `STALE_CHECK_SECONDS=120`) | 171 s | Yes ("behind … added when the queue is idle") | P1-1 (same class as F3) |
| K | 99 M-rate store (45 files, 79,027,749 spine rows): Moda TX (430,768 rates) dropped in the inbox while serving; then `mrfx forget` with the server stopped | payer slice + scoped directory; forget restores the spine | done 5.0 s, current 14.6–17.5 s. Incremental 11.6 s = spine (Moda) 1.2 s + **directory 10.3 s** (scan of all 99 M rows for 1,781 TINs). RSS 1,463 → **2,010 MB** peak. Forget 8.9 s (payer slice 7 s) → 45 files, 99,000,000 rates, **spine and directory set-hash identical to before** | 12.5 s | Yes | PASS |

"ref8" = the same 8 parts built in one uninterrupted `rebuild_rollups()`. Spine `444992:4505bcc2…`, directory `12370:08b638c1…`.

## 3. Findings by severity

### P0: none found
In every run: 0 missing / 0 extra rows once current; `files.rows_emitted` = part rows for every done file; spine payers = raw payers; and no sample with `state == current` while `covered_files < done_files` (J/J2/J3 `false_current: []`, I: 0, A–D: 0). Nothing was lost, nothing came in twice, and no "covered" was claimed falsely.

### P1-1: work that has no "credit" waits for an IDLE queue, so one long or stalled download starves it (CONFIRMED mechanism; PLAUSIBLE for hours on the user's store)
A part-way or failed full plan, or an inbox file whose best-effort refresh was skipped, has no queue credit. The only thing that picks it up is the idle probe at `fetch.py` ~3741, which requires `pend == 0 and in_flight == 0`. A parked download is re-woken at once (`wake_parked_urls(only_if_idle=True)`), so in practice the queue never reads idle.
- **F3**: the catch-up's failure cleared at t=0. A single stalling download (parked "1 of 6", "2 of 6", "3 of 6" at 08:48 / 08:55 / 09:01) kept `downloading: 1`. **The plan did not resume in 20 min.** The header said "Analytics cover 1 of 7 payers (1 of 8 files). stopped: ran out of memory … retries when the queue is idle" throughout. Spine at the end: 72,398 / 444,992 rows.
- **J3**: an inbox file landing during a 60 s name refresh was skipped ("a rebuild is already running — skipping this best-effort refresh (the running one covers it)"). It went in 117 s after the refresh ended, and that was with an idle queue. With a 22 GB download in flight it would wait for that download.
- Bound: any file that *finishes* gives a credit, and the credit path retries mid-grind. So the wait lasts until the next completion. On the user's queue of multi-GB files that can be hours.
- Repro: `AN/s_f.py f3`; `AN/s_j3.py`.

### P1-2: mid-grind fold-in cadence grows with store size, and the header gives the wrong reason (CONFIRMED here; PLAUSIBLE hours on the user's store)
- **F1** (fold-in emulated at 30 s): refreshes at 88 s, then 240 s (= 4 × 31 s after the previous one ended), then at idle. File-done→covered gaps reached 177 s.
- The header the whole time said "*N newly loaded files not in the analytics yet — added when the queue is idle*". The queue never went idle, and the files were added mid-grind.
- The real next-refresh time (`rollup_min_interval`) is a local variable in `run_queue` and is never exposed. It is logged only when a refresh takes over 60 s.
- Extrapolated from K (§5): an existing-payer fold-in on 1.16 B rates is roughly 3–14 min on this box, more on a laptop. That makes the interval 12–56+ min and the worst gap about 5 × that, with the header saying "when the queue is idle" on a queue that may run for days.
- Repro: `AN/s_f.py f1`.

### P2-1: during a full build over an already-built spine, the header and every document's methodology note say "0 of N payers" (CONFIRMED)
`_analytics_coverage` drops every payer the plan has not re-sliced yet (`covered_list = [p … if p not in last or p in plan_done]`). Those payers' previous rows are still complete in the spine. `/api/payers` uses `payers_part_way()` instead and lists them as covered.
- **G1b** while failed: header "Analytics cover **0 of 7** payers (7 of 8 files)", while `/api/payers.covered` held 6 payers and the code page priced 4 of them.
- A **Moda-only CSV export** (Moda fully covered) carried in its methodology: "Analytics cover 0 of 7 payers (7 of 8 files). **Every number here comes from those** …". A deliverable under-states its own provenance.
- **H2** (the `rollup_needs_full` upgrade path): "0 of 7 payers" at the start of the rebuild. On a 1.16 B store that is 2–4 h of "0 of 49 payers" on every screen and sidecar.
- Repro: `AN/s_g1b.py`, `AN/s_g.py h2`.

### P2-2: an out-of-memory error in a ONE-file incremental escalates to a whole-store full plan, and the halving stays in the plan (CONFIRMED)
- G1: a 252-row HealthScope file's incremental OOM'd. `fetch.rebuild_now` fell back to `rebuild_rollups()`, which planned **14 slices over 6,768,604 rows**. Halving grew that to 24.
- Every later attempt had to run the full plan ("an analytics build is part-way — it continues as a full build"), because `update_rollups_incremental` refuses while a plan exists.
- On the user's store that turns a minutes-long payer slice into a 2–4 h full build. The header's "0 of N payers" (P2-1) shows for the whole of it.

### P3: wording and visibility
1. "added when the queue is idle" while a mid-grind refresh is scheduled (F1); see P1-2.
2. **An incremental shows 0 % for its whole duration.** `rollup_job` for `kind=update` has `work_total=1, work_done=0`. H: 28 s at 0 %, while the log printed "partition 1/7 … 7/7".
3. "Adding the newly loaded files to the analytics" during a **removal** (I1/I2) and during a scoped **name refresh** (`refresh_directory_incremental` sets `_rollup_active` without a job; J at 114.8 s).
4. Mid first build: "Analytics cover 2 of 2 payers (**0 of 2 files**)" for one 0.5 s sample (A at 3.5 s): the payer count and the file count disagree.
5. Logs that are not true: "inbox scan: a rebuild is already running … (the running one covers it)". It did not: J2/J3, the name build did not include the new file. And at start-up, with a part-way first plan: "incremental analytics catch-up unavailable (rollup tables were built by an older version (or never built))".
6. Header stats poll every 15 s (`app.js` 5364), so "current" can show up to 15 s late on the header (the Files tab polls every 4 s).

Audit artifact, not a product bug: my first J2 run blocked instead of skipping, because my hook hid `skip_if_busy` from `ingest._skip_kw`'s `inspect.signature`. It was fixed with `functools.wraps` and re-run; the bad run is kept as `out/j2_blocking_hook_artifact.json`. No other scenario had lock contention on the inbox path, so none was affected.

## 4. Loading-screen data inventory ("watch it process the files")

Live information about a fold-in, and where it can be read:

| Information | Where it lives | Exposed? |
|---|---|---|
| state (current / behind / building / failed), covered / done files, covered / loaded payers, covered / loaded rows, reason, sentence | `Store.analytics_coverage()` | Yes: `/api/stats`, `/api/boot`, `/api/files` `.analytics` (header polls every 15 s, Files tab every 4 s) |
| pct, ETA text | `rollup_job()` → coverage | Yes, but only meaningful for **full / names** builds; an incremental is always 0 % (P3-2) |
| running build: `kind`, `slice`, `slices`, `label` (payer · code range), `eta_s` | `Store.rollup_job()` | **Partly**: label and pct are folded into the `reason` string; `slice`/`slices`/`eta_s`/`kind` are not separate fields |
| measured rate (plan work units per second) | `_rollup_job["rate"]` | **No** |
| the plan: all slices (payer, codes, rows, work), `next`, `work_done`, `cover_ts`, created | `Store.rollup_plan()` (meta `rollup_plan`) | **No**: only "N of M parts saved" in the sentence, and only when not building |
| per-file "covered?" | `_analytics_coverage.covered(fn, ts)` | **No**: computed for every file, only counted. `/api/files` rows have no covered flag |
| covered / loaded payer **lists** | `_analytics_coverage` `covered` / `loaded` | Dropped by `_analytics_public`. `/api/payers` recomputes them a different way (the source of P2-1's disagreement) |
| half-built payers | `Store.payers_part_way()` | Used only inside `/api/payers` |
| which payers the running incremental is recomputing; its partition i/N | locals in `update_rollups_incremental` / `_apply_rollup_delta_sliced` | **Log only** ("recomputing 2 payer slice(s)", "partition 3/7"); payer names not even logged |
| last failure `{at, sentence}` | `Store.rollup_failure()` | Sentence only (in `reason`); `at` not exposed |
| why stale | `Store.rollup_stale_reason()` | Logged at start only |
| queue credits waiting for a fold-in, the next mid-grind refresh time, the back-off retry time, consecutive failures, last fold-in duration | `run_queue` locals: `ingests_pending_rollup`, `rollup_min_interval` + `last_rebuild_at`, `retry_rollup_at`, `rollup_failures`; `Store.last_rollup_build_seconds` | **No**: none of these leave `run_queue` (the duration is only on the Store) |
| when a file was folded in | not recorded anywhere (the marker stores the file's own `finished_at`, not the fold-in time) | **Missing** |
| file done time; queue counts | `files.finished_at`; `url_queue_counts()` | Yes: `/api/files`, `/api/urls` |
| log lines | "updating analytics rollups (N newly ingested file(s))", "analytics update: N new file(s), K queued slice(s) — recomputing P payer slice(s)", "partition i/N", "analytics build: part i of N done (label, Ns)", "analytics update finished in Ns", back-off line, "next mid-grind refresh in ~N min" (only if over 60 s) | Log file / console only |

To show "*file X folded in at 10:42, 37 files waiting, now doing payer Y slice 3 of 8 at N rows/s, ETA*", the following is **missing**:
- a per-file `folded_in_at` (and a `covered` flag in `/api/files`);
- the waiting count (done − covered exists, but not "waiting for the next refresh at HH:MM");
- the incremental's own progress (payer names, partition i/N, rows read) in `rollup_job`;
- the rate in rows/s;
- the plan's slice list;
- the run_queue scheduling state: next refresh, back-off, why it is waiting (idle-only vs interval).

Also, the header would need to poll faster than 15 s while the state is building.

## 5. Scale (K) and extrapolation to the user's store

**Measured on the 99 M-rate store** (Moda, a new payer, 430,768 rates):
- spine slice: 1.2 s for 133,599 spine rows;
- directory: 10.3 s. It recomputes the 1,781 TINs in the new file, but `TIN_DIRECTORY_QUERY` reads `rates` filtered by that TIN set, so it scans the whole store;
- total incremental: 11.6 s;
- peak RSS: +550 MB (2.0 GB), with the 6 GB auto memory budget.

**A new 2 GB file of an EXISTING payer on the 1.16 B-rate store** rebuilds:
1. **That payer's entire spine slice**: every raw row of the payer, not only the new file's. That is about 23.7 M rates for an average payer (1.16 B / 49) and 100–300 M for a big one. At the ~270 k rates/s this box sustained (99 M full build in 363 s; H's 6.3 M-row 4-payer update in 22 s), that is about 90 s for an average payer and 6–18 min for a big one.
2. **The directory rows of the file's TINs**: one filtered scan of all 1.16 B rates. That is 10.3 s × 11.7 ≈ 2 min here.
3. **One count scan of the payer's rows** (`n_rows`).

**Expected cost:** about 3 min (average payer) to about 20 min (large payer) on this box; 2–3× that on a 4-core laptop with a slower disk. **Consequence for P1-2:** the mid-grind interval becomes 4× that (12 min to over 1 h). The worst file-done→covered gap during a long grind is about 5× the fold-in time. The forget path is the same payer-scoped work (8.9 s on 99 M). Byte-identity after add and forget held at 99 M.

## 6. Repro scripts (all in `AN/`; run with `/home/user/Claudevideo/.venv/bin/python`, each guarded by `if __name__ == "__main__":`)
- `h.py`: harness (fixture generator, absolute-path config writer, `Serve` Popen wrapper with `stop`/`kill`, poller, `truth()`, sorted/set hashes).
- `hooks/sitecustomize.py`: opt-in instrumentation for a serve subprocess (`PYTHONPATH=AN/hooks MRFX_AUDIT_HOOKS=1`). It does three things:
  - widens windows: `MRFX_AUDIT_DELAY_ROLLUP`, `_HOLD_DELETE` (holds an incremental's open transaction), `_SLICE_SLEEP`, `_DIR_SLEEP`;
  - forces slicing: `MRFX_AUDIT_PARTITION_ROWS` (the module ceiling the tests use);
  - injects OOM while a flag file exists: `MRFX_AUDIT_FAIL_FLAG`; `MRFX_AUDIT_TRACE_SQL` times each statement.

  It changes no number. `hooks076/` holds the 0.7.6 counterpart (OOM in its rollups).
- `mk_base.py`: base stores `bases/base8_unbuilt`, `bases/base7_built` and the reference `bases/ref8`. Deleted after the runs, to save disk; this script recreates them.
- `s_abcd.py` (A–D) · `s_e.py e1i e1u e2 e30 e70` (E) · `s_f.py f1 f2 f3` + `thr_server.py` (F) · `s_g.py g1 g2 h2`, `s_g1b.py` (G, H2, P2-1) · `s_h.py` (H; 0.7.6 install at `AN/H/install/Claudevideo`, its venv shares the identical pinned packages through a `.pth`, since `requirements.txt` is unchanged c8b80fc → 9617c59) · `s_i.py` (I) · `s_j.py`, `s_j2.py`, `s_j3.py` (J) · `s_k.py` + `k_hash.py` (K; the 99 M store was returned to 45 files / 99,000,000 rates, with identical spine and directory hashes).
- Raw results: `AN/out/*.json` and logs. Trial stores were deleted after each truth check, to keep disk free; the scripts recreate them.
