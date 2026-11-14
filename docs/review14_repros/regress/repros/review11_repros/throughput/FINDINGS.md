# Review 11 — link → download → ingest throughput, and the Windows hazards

Baseline 1064203 (v0.7.4). Nothing under `mrfx/` or `tests/` was touched; every
hook lives in the harness process only (parser workers are spawned fresh and
run the real code). Linux, 4 cores, 15 GB RAM, scratch config under
`/tmp/claude-0/-home-user-Claudevideo/4633d594-0139-5f8e-a935-ae9c76b72000/scratchpad/audit14/regress/r11tmp/throughput`, local `http.server` in a separate process.
**Timing caveat:** other reviewers' kill-tests and pool tests ran on this box
during my runs (visible in `ps`); two identical default runs differed by 12 %
(146 s vs 164 s wall, the full rollup 68 s vs 85 s). Read every number as ±15 %.

Scripts (all under this folder): `gen_fixtures.py` (24 synthetic 5–60 MB gz
in-network files + 3 real payer files copied read-only), `queue_timeline.py`
(the instrumented `run_queue(drain=True)`), `trace_opens.py` (strace
summary), `throttle_server.py` + `shutdown_restart.py` (Popen'd `mrfx serve`,
SIGINT/SIGTERM/SIGKILL/supervise, restart catch-up). Raw outputs:
`results/timeline_<variant>.{md,json}`, `results/shutdown_restart.md`.

## 1. Measured timeline — default config, 27 files (753 MB gz, 32.0 M rows)

`queue_timeline.py default_warm` (store opened `keep_warm=True` exactly as
`mrfx serve` does; 3 parser workers = cores−1, 6 downloaders, lanes 3 small/3
big, early_dedup 15 MB, delete_raw on). Wall **163.6 s**; every row `done`.

| file | MB gz | dl start | dl s | wait fetched s | ing start | ing s | preflight s | fp s | finish_ok s | finalize s | rows |
|---|---|---|---|---|---|---|---|---|---|---|---|
| bcbsms_2026-10_320_33B0 (real) | 25.1 | 0.1 | 0.55 | 2.4 | 3.1 | 6.5 | 0.03 | 0.11 | 0.05 | 0.04 | 2,660,356 |
| synth_plan0 01 | 5.2 | 0.1 | 0.17 | 0.1 | 0.4 | 2.6 | 0.02 | 0.07 | 0.02 | 0.05 | 220,209 |
| synth_plan0 08 | 10.3 | 0.1 | 0.35 | 0.2 | 0.7 | 4.5 | 0.02 | 0.15 | 0.02 | 0.05 | 463,836 |
| synth_plan0 15 | 25.6 | 0.2 | 0.51 | 2.6 | 3.3 | 9.6 | 0.01 | 0.16 | 0.05 | 0.03 | 1,156,287 |
| synth_plan0 22 | 50.9 | 0.2 | 0.86 | 4.1 | 5.2 | 18.2 | 0.01 | 0.16 | 0.03 | 0.04 | 2,240,710 |
| synth_plan1 02 | 5.2 | 0.2 | 0.17 | 0.1 | 0.5 | 2.7 | 0.03 | 0.12 | 0.03 | 0.05 | 239,550 |
| synth_plan1 09 | 11.7 | 0.5 | 0.22 | 8.9 | 9.6 | 4.0 | 0.01 | 0.13 | 0.02 | 0.04 | 517,639 |
| synth_plan1 16 | 28.5 | 0.6 | 0.55 | 11.7 | 12.9 | 10.0 | 0.01 | 0.12 | 0.05 | 0.08 | 1,236,359 |
| synth_plan1 23 | 55.5 | 0.8 | 0.83 | 12.2 | 13.8 | 18.6 | 0.01 | 0.03 | 0.03 | 0.04 | 2,479,699 |
| synth_plan2 03 | 5.7 | 0.9 | 0.16 | 21.9 | 23.0 | 2.2 | 0.01 | 0.17 | 0.04 | 0.05 | 248,061 |
| synth_plan2 10 | 13.5 | 0.9 | 0.25 | 22.2 | 23.4 | 4.8 | 0.01 | 0.13 | 0.02 | 0.03 | 605,999 |
| synth_plan2 17 | 31.7 | 0.9 | 0.57 | 23.7 | 25.2 | 11.3 | 0.01 | 0.06 | 0.03 | 0.08 | 1,380,343 |
| synth_plan2 24 | 60.2 | 13.7 | 0.92 | 13.7 | 28.3 | 21.0 | 0.01 | 0.05 | 0.04 | 0.04 | 2,671,487 |
| synth_plan3 04 | 6.3 | 13.7 | 0.30 | 18.4 | 32.4 | 2.3 | 0.02 | 0.10 | 0.04 | 0.04 | 276,775 |
| synth_plan3 11 | 15.7 | 13.7 | 0.36 | 20.8 | 34.8 | 5.8 | 0.01 | 0.09 | 0.04 | 0.05 | 695,174 |
| synth_plan3 18 | 35.2 | 13.7 | 0.71 | 22.1 | 36.6 | 12.3 | 0.01 | 0.07 | 0.03 | 0.10 | 1,544,512 |
| synth_plan4 05 | 6.8 | 13.8 | 0.39 | 26.6 | 40.7 | 2.6 | 0.01 | 0.06 | 0.04 | 0.04 | 302,843 |
| synth_plan4 12 | 17.7 | 13.8 | 0.57 | 29.0 | 43.4 | 6.5 | 0.01 | 0.07 | 0.03 | 0.06 | 774,507 |
| synth_plan4 19 | 38.8 | 34.8 | 0.84 | 13.3 | 48.9 | 13.1 | 0.01 | 0.07 | 0.03 | 0.04 | 1,697,779 |
| synth_plan5 06 | 7.8 | 34.8 | 0.22 | 14.3 | 49.3 | 3.3 | 0.01 | 0.12 | 0.02 | 0.05 | 350,774 |
| synth_plan5 13 | 20.1 | 34.8 | 0.69 | 14.5 | 50.0 | 7.0 | 0.01 | 0.09 | 0.03 | 0.03 | 872,801 |
| synth_plan5 20 | 42.8 | 34.8 | 0.81 | 17.0 | 52.7 | 14.5 | 0.01 | 0.06 | 0.03 | 0.04 | 1,859,911 |
| synth_plan6 07 | 8.9 | 34.8 | 0.49 | 21.7 | 57.0 | 3.0 | 0.01 | 0.07 | 0.04 | 0.03 | 388,189 |
| synth_plan6 14 | 22.8 | 34.8 | 0.70 | 24.6 | 60.1 | 7.9 | 0.01 | 0.07 | 0.03 | 0.05 | 991,821 |
| synth_plan6 21 | 46.7 | 50.0 | 0.64 | 11.5 | 62.1 | 15.6 | 0.01 | 0.09 | 0.04 | 0.03 | 2,059,733 |
| uhc Empire CSP-377 (real) | 35.5 | 50.0 | 0.54 | 16.8 | 67.3 | 4.4 | 0.01 | 0.08 | 0.06 | 0.05 | 20,741 |
| wy_2026-11_710_26B0 (real) | 118.7 | 50.0 | 0.99 | 17.1 | 68.1 | 10.4 | 0.02 | 0.06 | 0.03 | 0.03 | 1,757,643 |

Phases: **downloads+parses t=0→78.6 s**; last ingest ends 78.5 s; then
`_rollup_due` fires at idle → `update_rollups_incremental` RAISES
("rollup tables were built by an older version (or never built)") →
**full `rebuild_rollups` 85.0 s (t=78.6→163.6)**. Sum download 14.3 s (6
threads), sum ingest 224.5 s (3 workers), sum wait-in-`fetched` 391.7 s.

CPU by phase (sampled /proc every 0.5 s): ingest phase — workers 2.79 cores
(0.93 per worker; the docs' 1.2–1.5 is not reached because the main process
takes 0.36 and the box has 4), main 0.36; rollup phase — main 3.67 cores,
workers 0. Idle gaps: parsers starved ≤ 1 s per run; downloaders idle with
queue pending (prefetch cap `workers+1` = 4 fetched files) 40–53 s per run —
harmless, the parsers are the bottleneck on a local server.

Rollup cadence (`_rollup_due`, fetch.py:2754–2766): fires only at idle or
after max(90 s, 4 × last duration). Measured rebuilds per file ingested:
2/27 = 0.07 (3 workers), 3/27 = 0.11 (1 worker: the 90 s timer fired
mid-grind at t=91 → full 65 s rebuild overlapped the parse → final
incremental 73.6 s). Every fresh store's FIRST rebuild is a full one.

### What moves (same queue, one knob each; wall / ingest-phase end / rollup)

| variant | wall s | ingests done at | rollups | note |
|---|---|---|---|---|
| default (keep_warm=False, the CLI-drain store) | 146.1 | 78.4 | 68.3 (full) | write_lock held 14.9 % of wall, url_progress 236 ms each — see finding 3 |
| default_warm (serve's store) | 163.6 | 78.6 | 85.0 (full) | the baseline above |
| parallel_ingests=1 | 313.9 | 156.0 (+ a 65 s full rebuild overlapped at t=91) | 64.9 full + 73.6 incr | serial parse 238 s ≈ sum ingest; incremental at the end ≈ as slow as full |
| parallel_ingests=2 | 191.4 | 90.5 | 85.6 full + 14.8 incr | |
| parallel_downloads=1 | 143.2 | 78.1 | 65.1 | downloads are never the bottleneck here |
| parallel_downloads=2 | 136.8 | 71.1 | 65.8 | |
| small_file_downloads=0 (no lanes) | 139.3 | 72.5 | 66.8 | ±noise |
| early_dedup_mb=0 | 137.1 | 71.5 | 65.6 | main rchar 5.1 GB vs 6.2 GB (−1 GB of fingerprint re-reads) |
| delete_raw_after_ingest=false | 132.4 | 70.4 | 62.1 | ±noise; downloads dir keeps 753 MB |
| wave2: 14 new-payer files (339 MB) onto the finished store | 80.6 | 39.5 | 41.1 **incremental, no fallback** | 7 payers never seen before: precondition is "prior full build", not "known payer" |
| wave2 with ROLLUP_MAX_STALE_SECONDS=20 (cadence experiment) | 76.7 | 47.8 | 27.4 + 27.4 incr, mid-grind | wall −5 %, but sum ingest 104.7 → 134.3 s (**parsers −28 %** while a rebuild overlaps); main peak RSS 3.1 → 4.4 GB |

Conclusion: on a parse-bound queue only `parallel_ingests` moves the ingest
phase (156 → 90 → 78 s for 1/2/3 workers; the 4-core plateau), and the
rollup is a serial tail of equal size. Download-side knobs are within noise.

## 2. Per-byte accounting — one real 118.7 MB gz (wy_710, 1.11 GB inflated, 1,757,643 rows)

`queue_timeline.py bytes_wy710 only=wy_2026-11_710 parallel_ingests=2
parallel_downloads=1` run under `strace -f --seccomp-bpf -e
trace=openat,unlink*,rename*,fsync,fdatasync`; summary via `trace_opens.py`.
Wall 17.5 s: download 4.75 s (local; 64 KB chunks, sha256 inline), wait 0.1 s,
ingest 9.8 s, rollup 2.5 s on a 1-file store.

| stage (thread/process) | opens of the file | bytes | notes |
|---|---|---|---|
| download (prefetch thread) | `.part` W×1, `.val` W×1 | 119 MB socket read → 119 MB written | sha256 of every chunk as it lands (no re-read); **4** `url_progress` UPDATEs for the whole 119 MB (8 MB & 1.5 s / 10 s throttle), 0.29 s total incl. lock wait |
| early duplicate check at 15 MB (`early_duplicate_check`) | `.part` R×2 | 15 MB head re-read | — |
| `_write_meta` + `_record_download_fingerprint` + `_remember_identity` | dest R×5, `.fetchmeta.tmp` W×1, rename ×1 | 15 MB head + 1 MB tail + 8×64 KB windows + gz trailer ≈ 17 MB | |
| preflight (`sniff.preflight`, processor thread) | dest R×~3 | header + trailer (KB) | 0.03 s |
| parse (worker process) | dest R×2 | **119 MB read once, inflated once** (fast path, single pass; file < 1.5 GB); own parquet re-read 16 MB for QA | worker rchar 184 MB, wchar 16 MB, peak RSS 759 MB |
| `finalize_rates_part` | rename `.parquet.tmp` → part | — | 0.03 s under write_lock |
| `_finish_ok` fingerprint again | dest R×3 | ≈ 17 MB | identical to the post-download fingerprint |
| cleanup | unlink dest, `.fetchmeta`, `.val` | — | |
| DuckDB | 83 fsync (fd 3/4 = .duckdb/.wal) | WAL max **1.4 MB** (1.8 MB max over any run) | DuckDB auto-checkpoints at 16 MB; no checkpoint stall seen in any run; **the app itself never fsyncs** |

Totals per file: disk reads ≈ 119 (worker) + 17 + 17 + 15 (three head
fingerprints) + 16 (QA) ≈ **184 MB = 1.55× the file**; writes 119 + 16 MB
parquet + DuckDB. Main process rchar 260 MB (119 of it is the socket). No
wasted full pass: the uncompressed bytes are produced exactly once.

Contrast with the 27-file run: main rchar **6.2–7.7 GB** for 191 MB of
parquet parts — the full rollup re-reads the store ~30–35× (AI_HANDOFF C1:
~1 slice per hash partition, each scanning every part). That, not the
per-file passes, is where the bytes go.

## 3. Memory

| process | file | VmHWM |
|---|---|---|
| worker | synthetic 5–60 MB gz (≤2.7 M rows each) | 548–786 MB |
| worker | wy_710 (119 MB gz, 1.11 GB) | 759 MB |
| worker | UHC Heritage-EPO (262 MB gz, ~5 GB inflated, big-file single read + spool) | 748 MB |
| worker | wy_590_10P0 (90 MB gz, 11,551 rows) | 240 MB |
| main (serve-style store) | 27-file grind incl. full rollup | 3.2–3.6 GB (DuckDB cap auto = 6 GB on this 15 GB box) |
| main | rollup overlapping ingest (cadence experiment) | 4.4 GB |

`_ram_worker_cap` (fetch.py:1703–1712) computed with `_total_ram_bytes`
mocked: 8 GB → **1** worker, 16 GB → **3**, 32 GB/8 cores → 7 (cores−1
binds), 64 GB → 7; `duckdb_memory_gb: 6` on 16 GB → 3. So yes, the cap
reduces an 8-core 16 GB laptop from 7 to 3 workers as the code comment
says. GETTING_STARTED "Making a big queue finish faster" item 1 still
describes auto as "core count minus one … stops at 8" — wrong for any ≤16 GB
machine (finding 10). Measured worker peaks (≤0.8 GB) leave the 2 GB/worker
budget conservative on these files; the 1.92 GB Aetna figure in the docs
remains the sizing case.

## 4. Shutdown / restart (`shutdown_restart.py`, real `mrfx serve` via Popen, paced 3 MB/s server, 15-file queue)

| case | queue at signal | signal → exit | leftovers | notes |
|---|---|---|---|---|
| A SIGINT (Ctrl-C) | 3 ingesting, 2 downloading, 5 fetched | **0.36 s**, exit 0 | 4 parts, 20 downloads, 2 `.part`; workers gone in 1 s | **2 tracebacks + "ERROR ingest failed"** printed (finding 8) |
| B SIGTERM | 3 ingesting, 3 downloading | 0.33 s, −15 | 3 `.parquet.tmp` + 3 `.progress` orphans (swept next start) | clean log |
| C SIGKILL | 3 ingesting, 3 downloading | 0.03 s | same + **3 idle workers orphaned for good** (PPID 1, ~220 MB RSS each, still alive 10+ min later) | finding 7 |
| C restart | — | dashboard ready **2.4 s**; "recovered 3 file(s) left mid-parse", "resumed 12 URL(s)"; catch-up thread ran a FULL rebuild in the background (fresh store, never built); queue re-drained at 50.6 s; final incremental 15 s → all 15 `done` at 65.6 s; no duplicate rows (15 parts) | | the catch-up cost on a real store = one full rebuild if the store was never fully built, else one incremental |
| D `serve --supervise`, SIGINT to the process group | 3 ingesting, 3 downloading | 0.51 s, exit 0 | 2 `.parquet.tmp` | supervisor stops cleanly ("exited normally") |
| idle (queue drained, rollup finished) then SIGINT, ×6 | 0 busy | 0.31–0.58 s | 0 orphans in 6/6 | |

## 5. The write_lock question (coordinator's item)

Instrumented by replacing `store.write_lock` with a proxy recording every
acquire wait and hold, attributed to thread and to `store._rollup_active`.
The premise in `Store.url_progress`'s docstring (store.py:2680–2688: "a
multi-minute rollup rebuild holds write_lock the whole time") is **not what
the code does**: rebuilds serialize on `rollup_lock` (store.py:951–961) and
take `write_lock` only around `_register_views` / the view DDL
(store.py:3949, 4112, 4295, 4371).

| run | write_lock held while a rollup was active | held total (% wall) | waits > 2 ms, total across 9 threads | per downloader thread | longest wait |
|---|---|---|---|---|---|
| default_warm (full 85 s) | **0.30 s** | 6.9 s (4.2 %) | 396 / 9.7 s | 1.2–1.5 s of 164 s (≈1 %) | — |
| ing1 (full 65 s + incr 74 s) | 1.70 s | 8.2 s (2.6 %) | 222 / 5.5 s | 0.6–1.1 s | — |
| ing2 (full 86 s + incr 15 s) | 0.77 s | 5.1 s (2.7 %) | 265 / 5.6 s | 0.8–0.9 s | — |
| wave2 (incr 41 s) | 0.07 s | 3.0 s (3.7 %) | 306 / 5.1 s | 0.6–0.8 s | 79 ms |
| wave2_cadence20 (2 × incr 27 s, mid-grind) | 1.85 s | 3.4 s (4.5 %) | 337 / 7.7 s | 0.9–1.4 s | — |
| default, keep_warm=False (CLI-drain store) | 0.41 s | **21.8 s (14.9 %)** | 485 / **57.4 s** | **7.3–8.7 s (6 %)** | 433 ms |

Incremental → full fallbacks: on every FRESH store the first attempt raises
"rollup tables were built by an older version (or never built)" (1 of 2
attempts in each fresh run; the serve-start catch-up does the same). On a
store with one prior full build, 0 fallbacks in 3 attempts even with 7
brand-new payers. Lock-stalled share of wall: **≤ 1 % per thread** with the
store `mrfx serve` uses; the rollup's lock footprint is < 2 s per run. The
rollup's real cost is CPU/disk, not the lock (finding 1). Severity for the
lock itself: P3 (docstring drift) — except the CLI path, finding 3.

## Findings

**1. P1 — the analytics rollup doubles a fresh grind and reads the store ~35×.**
fetch.py:2820–2870 (`rebuild_now`), store.py:4181–4510 (`rebuild_rollups`),
store.py:3907 (`update_rollups_incremental` preconditions). Measured: 27
files parse in 78 s on 3 workers, then a full rebuild of 32 M rows takes
62–85 s (≈2.3 s per M rows) as a serial tail with the parsers idle — 45–52 %
of wall in every fresh-store run; main rchar 6.2–7.7 GB for 191 MB of parts.
Every new store (and every `rollup_needs_full`/schema bump) pays this; with
1 worker the end-of-run incremental for all-new payers (73.6 s) costs the
same as the full one. Mid-grind overlap recovers only 5 % of wall and slows
parsing 28 %. Fix: the already-designed single pass (COPY … PARTITION_BY
then aggregate per partition, AI_HANDOFF C1) — O(2 × rows) instead of
O(slices × rows); and let the first build on an empty store go straight to
`rebuild_rollups` instead of raising through the incremental path.

**2. P1 (Windows) — the download's final rename has no lock tolerance, and
its failure mode throws the complete `.part` away.** fetch.py:1004
`part.replace(dest)`, :1005 `val_p.unlink`, :1798 `tmp.replace(meta_p)`
(`_write_meta`). A `PermissionError` (WinError 32/5: another handle open
without FILE_SHARE_DELETE — Defender/indexer scanning the just-closed file;
the code's own comments at ingest.py:396–401 and store.py:246–250 call this
"routine") is neither `DownloadError` nor an httpx error, so it escapes
`_download_reserved`, `fetch_url_record` (fetch.py:2168–2176 catches only
`DownloadError`) and lands in the downloader's `except BaseException`
(fetch.py:2896–2906) → row `failed: unexpected download error`, needs a
manual retry. On that retry the `.part` is complete, so `Range:
bytes=<total>-` → 416 → fetch.py:762–769 **unlinks the .part and restarts
from byte 0** (≥64 MB files on a Range-ignoring host: three 200-refusals then
the same restart). One AV hiccup on a 20 GB BCBS file = the whole download
again. Fix: `replace_with_retry`/`unlink_with_retry` (store.py:245–280) at
fetch.py:1004–1005 and :1798, and treat a complete-size `.part` on 416 as
done (hash it) instead of discarding it. Confidence: high on the code path,
medium on how often Windows AV produces the lock at that instant.

**3. P2 — the CLI drain path pays a 12.6 ms DuckDB connect on every queue
write.** cli.py:415 (`mrfx add` when no dashboard is running), :543 (`mrfx
ingest`) open `Store(...)` without `keep_warm=True`; `Store.connect()`
(store.py:1948) then builds a fresh connection per call: measured 12.6 ms
per `connect()`, `url_progress` 19.3 ms (vs 1.2 ms pinned), `url_queue_counts`
14 ms. On the 27-file drain this is write_lock held 14.9 % of wall, 57 s of
lock waits (8.7 s per downloader thread), `url_progress` 236 ms per call.
`serve` (cli.py:225) is pinned and unaffected. Fix: `keep_warm=True` for
the two drain commands (one line each).

**4. P2 (Windows) — `finalize_rates_part` holds `write_lock` through a
~90 s retry loop.** store.py:2370–2375 takes `write_lock` and calls
`_swap_in_part` → `replace_with_retry` (store.py:245–262: 12 attempts,
sleeps 0.2 → 15 s, ≈90 s budget) and `unlink_with_retry` (≈1.2 s). While a
reader holds the old part (a running export, AV), every `next_queued_url`,
`next_fetched_url`, `update_url`, `claim_content_ingest` blocks for up to
90 s: no download can start or be marked fetched, the Files tab's writes
stall, `url_progress`/`update_progress` give up after 1 s (bars freeze).
Measured on Linux (no lock): finalize 0.03–0.22 s. Fix: do the retry loop
outside the lock (take the lock only for the final `replace`), or park after
~2 s and let `_retry_pending_part_ops` finish it (it already exists).

**5. P2 (Windows) — outreach CSVs are written with doubled line endings.**
`outreach_csv` (outreach.py:280–285) builds text with `csv.DictWriter`'s
default `\r\n`; cli.py:846 and api.py:3701 then `Path.write_text(...)`, which
on Windows translates each `\n` to `\r\n` (Python `open()` newline=None), so
every row ends `\r\r\n` — Excel and mail-merge imports show a blank line
between rows. The other CSV builders return `PlainTextResponse` (no
translation) or use `newline=""` (cli.py:881) and are fine. Fix:
`write_bytes(text.encode("utf-8"))` or `open(..., newline="")`.

**6. P2 (Windows) — payer filenames can cross MAX_PATH.** `filename_for`
(fetch.py:81–90) keeps up to 120 chars of basename + 9-char hash; with
`.part.val` the downloads path is up to 138 chars past `downloads_dir`.
Measured with a real UHC name (124 chars after the hash): a project folder
of ≥ ~120 chars (`C:\Users\<name>\OneDrive - <Company>\Documents\MRF
Explorer\<folder>` is 91) crosses 260 → `FileNotFoundError [WinError 206]`
/`[WinError 3]` on `open()` unless `LongPathsEnabled`. Python's installer
offers "Disable path length limit" only on its LAST screen; HOW_TO_RUN
Step 1 (lines 21–34) never mentions it, GETTING_STARTED warns only about
OneDrive syncing. Fix: document the installer option, or cap the base at
~80 chars (the hash prefix already makes names unique).

**7. P2 — parser workers survive a hard kill of the server and never exit.**
After SIGKILL (case C) the 3 idle workers stayed alive with PPID 1 and ~220
MB RSS each (still alive 10 minutes later). `_parse_worker` probes the parent
only inside `report_chunk` (ingest.py:605–626), i.e. only while parsing; an
idle worker blocks on the executor's call queue, whose write end its
siblings also hold, so it never sees EOF. On Windows the same happens after
Task Manager "End task" on the server or the supervisor's `proc.terminate()`
(cli.py:1822) — closing the console window is the one path that ends them
(CTRL_CLOSE_EVENT reaches every attached process). Clean SIGINT/SIGTERM and
`--supervise` shutdowns terminated the workers in 6/6 + 3 runs. Fix: a
parent-liveness watchdog thread in the worker (or `prctl(PR_SET_PDEATHSIG)`
on Linux / a Job Object with KILL_ON_JOB_CLOSE on Windows). Note: 3 further
PPID-1 workers appeared during the test window that I could not attribute
(another reviewer ran pool tests concurrently); the six orphans
(PIDs 11431, 11437, 11446, 12459, 12465, 12477) are still running — I was
not permitted to kill them.

**8. P3 — Ctrl-C on a busy grind prints tracebacks and "ERROR ingest
failed".** cmd_serve's `finally` (cli.py:320–328) calls
`pool.shutdown_now()` (terminates workers) while processor threads are still
in `dispatch_and_wait`; they get `BrokenProcessPool` → `heal()` → re-submit
→ `RuntimeError: cannot schedule new futures after interpreter shutdown`
(ingest.py:874–879, :447) — two tracebacks per in-flight file on the user's
console at every stop (case A). State is unharmed (rows stay 'ingesting',
recovered at the next start) but it reads as a crash. Fix: set a
"shutting down" flag before `shutdown_now()` and have `dispatch_and_wait`
return `requeued` on it instead of healing.

**9. P3 — updater run while the server is still up gives a wrong hint.**
updater.py:150–165 runs `pip install -r requirements.txt` first; on Windows a
loaded extension (`duckdb…pyd`, pyarrow DLLs) cannot be overwritten →
pip fails → the message says "usually no internet connection". No port probe
(cmd_serve has one, cli.py:193–211). Fix: probe the port first and say
"close MRF Explorer first".

**10. P3 — doc/comment drift found by the measurements.** (a) GETTING_STARTED
"Making a big queue finish faster" item 1: auto workers = "core count minus
one … stops at 8" — actually RAM-capped (16 GB → 3, 8 GB → 1; fetch.py:1703).
(b) store.py:2680–2688 `url_progress` docstring: rollups do not hold
write_lock (measured ≤ 0.4 s per full rebuild). (c) fetch.py:870 "throttled
by the 1 MB chunk size" — `BODY_CHUNK_BYTES` is 64 KB (fetch.py:580).
(d) HOW_TO_RUN/GETTING_STARTED say nothing about Defender real-time scanning
re-reading a multi-GB `.part` at every reconnect (closed and reopened each
time); an exclusion for `data/downloads` and `data/mrfx_store` is the usual
advice (confidence: medium — standard Defender behaviour, not measured here).

**11. P3 — fingerprint head re-read three times per file.** early check
(fetch.py:912–916), post-download (`_record_download_fingerprint`,
fetch.py:2122) and post-ingest (`_finish_ok`, ingest.py:369) each read the
15 MB head + tail + windows: ≈ 35 MB extra reads per file (1 GB over the
27-file run, `early_dedup_mb=0` cut main rchar 6.2 → 5.1 GB). Trivial next
to the rollup; the post-ingest one could reuse the post-download result.

**12. P3 (Windows, low confidence) — laptop sleep and the stall clocks.**
`time.monotonic()` on Windows (GetTickCount64 / QPC in 3.13) keeps counting
through S3 sleep, unlike Linux CLOCK_MONOTONIC; after a lid-close longer than
`download_park_seconds` (240) the first chunk after wake trips `_park`
(fetch.py:963–968, a benign 10-minute park) or, past 600 s, the stall error
for that attempt (kept `.part`, auto-pause). The 3600 s worker hard-kill
(ingest.py:818–840) cannot fire from a sleep alone (its reference resets on
the first complaint). The docs already say "set sleep to Never".

**13. P3 (info) — knobs that do nothing on a parse-bound queue.**
`parallel_downloads` 1/2/6: 143/137/146–164 s; lanes off, early-dedup off,
keep-raw: ±5 s (noise). The prefetch cap idles the downloaders 40–53 s per
run by design. Where the docs send the user (raise `parallel_downloads` when
parsers starve) is right; here parsers starved ≤ 1 s.

## Windows read (no run) — each item: what, documented behaviour, consequence, confidence

(a) Rename/unlink of files that may be held open (Windows refuses
`MoveFileEx`/`DeleteFile` while any handle without FILE_SHARE_DELETE is
open; Python's `open()` never requests it):
- fetch.py:1004–1005 `part.replace(dest)`, `val_p.unlink` — plain, no retry →
  finding 2 (P1, confidence high/medium).
- fetch.py:1798 `_write_meta` `tmp.replace(meta_p)` — plain; failure = row
  failed + the next retry re-downloads from 0 because `dest` exists without
  its sidecar and the `.part` is gone (fetch.py:2145–2158) (P1, same class).
- fetch.py:536–537 segmented path, same.
- fetch.py:2428–2429, 2437–2438, 2459, 2495, 2502, 2512, 2563, 2579,
  2602–2603, 2639–2640 `dest.unlink(missing_ok=True)` after the row was
  already set to skipped/done/failed: a lock raises `PermissionError` out of
  `process_url_record` into the processor's `except BaseException`
  (fetch.py:3047–3058) which OVERWRITES the row to `failed: unexpected worker
  error` — e.g. a duplicate correctly skipped at :2424 becomes "failed" (P2,
  confidence medium: needs AV to hold `dest` right then).
- fetch.py:2278–2279 (failure path) and :2679–2682 `_cleanup_raw` (caught;
  download left behind, swept at the next start) — fine.
- ingest.py:632 progress `os.replace` (caught), :404/:407 `shutil.move`
  (caught), :556–563 `_cleanup_unlink` (retry, never raises), store.py:2379–
  2418 part swap (retry + parking + `_retry_pending_part_ops`), store.py:
  1916–1940 orphan sweep (caught), store.py:4583–4597 and :5050 spine/
  placement parquet (`except OSError`), api.py:1761/2562 (caught) — correct.
- Lock hold during the retry: finding 4 (P2, high).

(b) POSIX assumptions: none found that break — no `fcntl`, no `/dev/shm`, no
`fork` (spawn is explicit, ingest.py:443), no `os.kill` outside the POSIX
branch of `_pid_alive` (store.py:291–330, Windows branch uses
OpenProcess/WaitForSingleObject and treats ACCESS_DENIED as alive — correct),
`os.getppid` handled (ingest.py:619–621). No `fsync` anywhere in the app:
`_write_meta`, the `.val`, the progress JSON and the parquet part are
renamed without flushing — a power loss (the .bat's "power blip" case) can
leave a 0-byte `.fetchmeta` (handled: corrupt sidecar → re-download) or a
zero-length/truncated `.parquet` part under a `done` files row (NOT handled:
the rates view then fails on that part). Process kills are safe (measured);
power loss is not covered by the "kill at any instant" invariant. P3,
confidence medium (NTFS metadata vs data ordering).

(c) Path length: finding 6. Store-side names are short (`file_key` sha1[:16],
store.py:81; spool/QA-spill ≤ 175 chars past the base).

(d) Case: `dedup_key` lower-cases host only (fetch.py:63–79, S3 keys are
case-sensitive — correct); `filename_for` prefixes an 8-char hash so two
URLs differing only in path case get different local names (no NTFS
collision); `file_key` hashes the full name; `updater.SKIP_DIRS/PROTECTED`
compare lower-cased (updater.py:37–41, correct); `scan_inbox` globs `*` and
skips by suffix (ingest.py:1646–1651) — a `.JSON.GZ` upper-case drop is
offered to `preflight`, which sniffs bytes, so fine. `same_name_verdict`
(ingest.py:1486) keys on the exact name the watcher reports — NTFS returns
the stored case, so no mismatch. Checked, correct.

(e) Console encoding: the .bat launchers set `PYTHONUTF8=1` and the
supervisor passes `PYTHONUTF8`/`PYTHONIOENCODING` to the child (cli.py:1805);
an interactive console uses the UTF-16 console API (PEP 528). The only hole:
`mrfx serve` typed in PowerShell with output REDIRECTED to a file without
those variables → `print("  MRF Explorer  →  …")` (cli.py:298) raises
`UnicodeEncodeError` (cp1252) after the store is open and threads started.
Logging's StreamHandler never raises (prints "--- Logging error ---"
instead), and the messages are full of "—". P3, confidence high.

(f) `multiprocessing` spawn: `python -m mrfx` → `mrfx/__main__.py` has the
`if __name__ == "__main__"` guard; the `mrfx.exe` console shim is guarded by
pip. Pickled per submit: `cfg` (pydantic, fine), `external_refs` (can be
large — known), strings; never the store. `ParsePoolManager.heal/force_heal`
use `shutdown(wait=False)` + `proc.terminate()` → `TerminateProcess` on
Windows, fine. Ctrl-C in the .bat window is a CTRL_C_EVENT to every process
on that console, parser workers included: a `KeyboardInterrupt` inside a
worker is returned as the call's result by `_process_worker`, so `fut.result()`
raises `KeyboardInterrupt` in the processor thread (ingest.py:793) → the
thread dies by `except KeyboardInterrupt: raise` (fetch.py:3034) — harmless
during shutdown. Windows spawn start cost (import of duckdb/pyarrow/numpy
per worker, typically 2–4 s each with Defender) is paid once per pool and
again on every heal; not measured here. Orphans after a hard kill: finding
7. Confidence high.

(g) Sleeps under `write_lock`: only `replace_with_retry`/`unlink_with_retry`
inside `_swap_in_part` (finding 4) and `_retry_pending_part_ops`
(store.py:3689–3713, same helpers, at every rollup start/open). The
60 s "disk full" sleep (fetch.py:2208) and all back-off sleeps run outside
the lock. `url_progress`/`update_progress` use `acquire(timeout=1.0)` and
skip — correct.

(h) Updater vs a running server: program files (.py) are not held open by a
running Python, so `tmp.replace(target)` succeeds; the `.bat` is handled
(parenthesized block, written last); `.venv` is skipped, so `pip install -r`
is the step that hits loaded `.pyd` files → finding 9. Rollback on a failed
write is implemented (updater.py:98–111, 190–205) — correct.

Also checked: `SO_EXCLUSIVEADDRUSE` port probe (cli.py:193–201, correct for
Windows), `_total_ram_bytes` Windows branch (store.py:92–117),
`_auto_spill_base` PowerShell probe is best-effort with a timeout, CRLF: every
text input (`links.txt`, yaml, `.val`, sidecars) is read with `splitlines`/
`strip`/json and is CRLF-safe; the CSV outputs are finding 5.

## Checked and found correct

- Streaming invariant: worker VmHWM ≤ 0.8 GB on a ~5 GB-inflated real file
  and on 1.1 GB files; the compressed file is read once and inflated once
  (strace: 1 open by the worker for the parse + 1 for its own QA parquet).
- Single DB writer: workers open no DuckDB file (their QA uses an in-memory
  instance spilling to a pid-named folder); every store write is in the main
  process; finalize is one rename under the lock (0.03 s).
- Crash-resume: SIGKILL mid-grind (3 parsing, 3 downloading) → restart in
  2.4 s, 3 `processing` files and 12 in-flight URLs recovered, orphan
  `.parquet.tmp`/`.progress` swept, every file ended `done` exactly once
  (15 parts for 15 files), catch-up rollup ran in the background.
- Clean shutdown is fast: 0.3–0.6 s for SIGINT/SIGTERM/supervise with
  downloads and parses in flight; workers are terminated on every clean path
  (9/9 runs).
- WAL/checkpoint: `.wal` never exceeded 1.8 MB across all runs (DuckDB's own
  16 MB auto-checkpoint); 83 fsyncs per file are DuckDB's; no checkpoint
  stall observed.
- `url_progress` throttle (8 MB & 1.5 s, or 10 s) → 2 UPDATEs per typical
  file, 54 per 27-file run, ≤ 2 s total on the pinned store; `acquire(timeout
  =1.0)` means a busy lock never stalls the socket loop.
- Prefetch cap bounds disk: `fetched` never exceeded workers+1 = 4 files;
  `delete_raw_after_ingest` left `downloads/` at 4 KB after every run;
  the stranded-download sweep at `run_queue` start works (case C restart).
- Incremental rollup precondition is "prior full build + current schema",
  not "payer seen before": 14 files from 7 new payers took the incremental
  path (41 s) with no fallback.
- Lanes, early dedup, keep-raw: no measurable cost or benefit on this queue
  (all within ±5 s noise); the early duplicate check reads 15 MB of the
  `.part` once and never blocks the stream.
- `_ram_worker_cap` arithmetic matches its comment (16 GB → 3 workers next
  to a 6 GB pool).
- `_pid_alive` Windows branch, `_nt_alive_without_handle`, progress/temp
  cleanup tolerance, `_finish_file` move tolerance, updater rollback and
  lower-cased protected paths, SO_EXCLUSIVEADDRUSE, UTF-8 env for the
  supervised child — all as documented.
