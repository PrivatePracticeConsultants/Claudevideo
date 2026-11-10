# Prompt for the next session, round 11 — the link → download → ingest → skip protocol, made bulletproof (written for Claude Opus 5.5)

> **STATUS: not started.** Replace this block with what you closed, the
> numbers, and what you left (with the reason), as rounds 7–10 did.

Rounds 1–10 are `docs/NEXT_STEPS_PROMPT.md`, `_2.md` … `_10.md`. This memo
comes from a review on Nov 10, 2026 at HEAD 1064203 (version 0.7.4). The
user, a non-technical consultant running `mrfx serve` on Windows, asked for
a deep review of **the protocol for downloading, ingesting and skipping —
make it bulletproof and infallible while as efficient as feasible.**

Method — four independent passes, none by the author of the code under
review, each against a scriptable local HTTP server and scratch stores,
with real process kills (`subprocess.Popen` + `kill()` at random instants,
seed logged) and the real-data payer files on the box; plus the author's
own seam checks between the passes. Every item below was reproduced by a
script that was run; the script's name is on the item:

1. **Download transport** (`docs/review11_repros/download/`, its
   `FINDINGS.md`): `download()`, resume/Range, validators, stall / park /
   pause / cap clocks, segments, disk reservation, cancel, early dedup.
2. **Queue state machine** (`docs/review11_repros/queue/`, its
   `FINDINGS.md`): the real transition graph (derived by instrumenting every
   status-changing store method), twins, TOC expansion, lanes, counters,
   user actions, crash-resume (20 random kills, 2 claim-instant twin kills).
3. **Ingest and skip rules** (`docs/review11_repros/ingest/`, its
   `FINDINGS.md`): containers, same-name / same-content, the content claim,
   kills mid-parse, re-ingest, companions, the worker pool, oversize,
   bytes-read accounting, honesty.
4. **Throughput and Windows hazards** (`docs/review11_repros/throughput/`,
   its `FINDINGS.md`): the per-file timeline of a realistic queue, per-byte
   accounting, memory, shutdown/restart, lock stalls, and a code read for
   Windows-only breakage.
5. **Seam checks** (`docs/review11_repros/seams/`): what the passes could
   not see from inside one module — reuse of kept downloads after a
   republish, the refresh of a static index's children, the set-aside cap's
   queue position.

Severity: **P0** bytes wrong, double-loaded or lost, or a row stuck forever
· **P1** wrong state, silent non-load, or a slot wasted on a real path ·
**P2** measured waste or a rule the docs promise and the code does not keep
· **P3** cosmetic / counter hygiene / stale prose.

**No P0 was found.** Invariant 6 holds under every kill the reviewers
threw at it (queue: 19/19 valid trials — no row left in flight, every file
exactly its expected rows, parts == done rows, kept downloads reused;
twins: exactly one ingest in every ordering). What follows is the next
layer: states a user cannot get out of, files that silently never load,
and work the pipeline does twice.

## How to work

- Read `CLAUDE.md`, then `docs/AI_HANDOFF.md` in full (Step 3 invariants,
  Step 4 URL states — which this review corrects, see 2.0 — and the
  "Round 7 / Audit 8 / Round 9 / Round 10 additions"), then this file.
- Each fix: reproduce first (the script named in the item), fix the class,
  leave a regression test in `tests/mrfx/test_review11.py` that fails on
  1064203, re-run the script. Kill-tests belong in the suite too (the
  harness pattern: `queue/s5_driver.py` + `s5_crash.py`; Popen.kill on a
  driver you spawned, never a shell `kill`).
- **Every change to what gets loaded must keep the rows byte-identical for
  a file that loads today.** Prove it: ingest the three real files under
  120 MB in `scratchpad/real/` (or any three real payer files you have)
  before and after and compare the parts' row counts and a sorted hash of
  the rows; `mrfx fastcheck` for the parser paths.
- Repro scripts were written against `/tmp/mrfx_review11/<area>/…` and a
  harness per folder (`download/_harness.py`, `queue/h.py`, `ingest/h.py`,
  `throughput/gen_fixtures.py`); their first lines name the paths.
- Order: Group 1 (wrong bytes / silent non-load), then 2 (stuck or wrong
  states), 3 (waste), 4 (docs and hygiene). One commit per group (what +
  why + verified-with-numbers, each test named with the value it failed
  with on 1064203).

## Group 1 — silently wrong or missing data (P1)

_(download and ingest passes — pending)_

### 1.1 (P1, Windows) The download's final rename has no lock tolerance, and its failure path discards the complete `.part` (throughput `FINDINGS.md` finding 2 — code read, confidence high on the path, medium on how often AV holds the handle)
- `fetch.py:1004-1005` (`part.replace(dest)`), `:1798` (`_write_meta`'s
  rename). A `PermissionError` (antivirus / indexer holding the file
  without FILE_SHARE_DELETE — the code itself calls this "routine" at
  `ingest.py:396-401` and `store.py:246-250`) escapes
  `_download_reserved` / `fetch_url_record` (`:2168-2176`) to the
  downloader's `except BaseException` (`:2896-2906`) → "failed: unexpected
  download error", a manual retry; the retry sends `Range: bytes=<total>-`
  → 416 → `fetch.py:762-769` unlinks the .part and restarts from byte 0
  (and a Range-ignoring host restarts from 0 after three refusals).
- Fix: `replace_with_retry` / `unlink_with_retry` (the store already has
  them) at both renames; on a 416 for a `.part` whose size equals the
  known total, treat the file as complete (hash it) instead of discarding
  it. Then the same for the raw `dest.unlink` calls listed in Group 4.4(a):
  a locked file there raises into the processor's `except BaseException`
  (`fetch.py:3047-3058`), which overwrites a correct `skipped`/`done` row
  with "failed: unexpected worker error".

### 1.x A republished file under the same URL is never fetched again (seams `stale_reuse.py`, `refresh_static_children.py`)
- `fetch_url_record` reuses `dest` + `.fetchmeta` without asking the server
  anything ("NEVER re-download here"), and `store._enqueue_one` returns
  None for a `done` child whatever its parent's re-expansion found. Nothing
  records the server's validator at load time, so nothing can tell a
  republish from a repeat.
- `stale_reuse.py` (`delete_raw_after_ingest: false`): May loaded (50 rows,
  file_month 2026-05); server now serves June; `requeue_urls([url])` → **0
  HTTP requests**, row done, still 50 rows / 2026-05.
- `refresh_static_children.py` (DEFAULT config): static index + static
  child URL, both republished in place; the monthly refresh's
  `requeue_urls([index])` → requests `[GET /index.json]` only; the child
  stays done, 2026-05, 50 rows. The June vintage never loads for any payer
  whose rate-file URLs are stable (which payers those are is unknown — the
  real files on this box are all month-dated; the refresh is opt-in).
- Fix (one design): record ETag / Last-Modified / Content-Length per loaded
  URL (in the sidecar and in `url_queue` or the identity table). On reuse
  of a kept download for a row that was *requeued* (`requeue_urls`,
  `requeue_url_for_file`, a user retry of a done row) send one conditional
  HEAD; on mismatch download fresh. When a parent index is re-expanded,
  HEAD each `done` child once and requeue those whose validator changed —
  one request per child per month, no re-download when nothing changed.
  A server with no validator: compare Content-Length, else re-download on
  the monthly refresh only.

## Group 2 — states the user cannot leave, and silent non-loads (P1/P2)

### 2.0 The real transition graph (queue `FINDINGS.md` §1) — update AI_HANDOFF Step 4
Derived by instrumenting every status-changing store method. Differences
from the Step 4 prose, each confirmed in `s1_transitions.out`:
`fetched → expanding` never happens (a TOC goes fetched → ingesting →
expanding); `paused` also arises from the early-dedup "wait"; `queued` is
re-entered from `downloading` on disk-full / set-aside / park-limit;
`done` is left by `requeue_urls`, `requeue_url_for_file`, `forget_file`;
`paused` can be skipped. Replace the prose with the graph in the queue
FINDINGS (it is the one place the whole machine is written down).

### 2.1 (P1) A capped index tells the user to "press retry on this row" — there is no such button and the API refuses; files past `max_toc_files` silently never load (`queue/s3_toc.py` A/B)
- `fetch.py:1487` `cap_note`, `:2493` (done TOC row carries the note);
  `store.py:3225-3248` `_USER_TRANSITIONS` (queued only from
  failed/skipped/oversize/paused); `web/app.js:4405` (retry button only for
  failed/skipped/oversize).
- Index of 5 files, `max_toc_files=3` → row done with the note;
  `set_url_status_by_id(idx, "queued")` → False; re-paste → added 0,
  skipped 1; raise the cap and restart → f3/f4 still missing. Only the
  opt-in monthly refresh's `requeue_urls` re-expands it.
- Fix: allow done → queued for `kind IN ('toc', 'page')` in `_enqueue_one`
  and `set_url_status_by_id` (an index carries no rates, so it anchors no
  dedup); render the button; re-expansion then queues the missing children.

### 2.2 (P1) An `oversize` row records `bytes_total = 0`, so raising `confirm_over_gb` and restarting — the documented fix — never re-queues it (`queue/s11b_oversize_raise.py`)
- `fetch.py:856` (`too_big(total)` raised at the headers before any progress
  write), `:2267` (the status write carries no bytes_total);
  `store.py:3470-3490` (`requeue_oversize_within` needs bytes_total > 0);
  the message (`fetch.py:639-646`) says "raise confirm_over_gb for all files".
- Limit 1 MB, 1.5 MB file → oversize, bytes_total 0; raise to 5 GB + restart
  → still oversize, 0 re-queued. Only the per-row "download anyway" works,
  and with 2.5 a pasted row beyond the window has no way back at all.
- Fix: persist `bytes_total = total` on the oversize path (carry it on the
  DownloadError as `e.total`, as the handoff path already does).

### 2.3 (P1) "Clear queue" pressed while an index is expanding is silently undone (`queue/s9_actions.py` C)
- `store.py:3384` (`clear_queued` flips expanding → skipped);
  `fetch.py:2446-2493` (`_enqueue_children` then an unconditional
  `update_url(status="done")`).
- `clear_queued()` mid-expansion → 1 cleared, TOC skipped; 3 s later the TOC
  is done and 4 children are queued / downloading / fetched / done; all 4
  ingest (24 rows). The user's one bulk-stop is overwritten.
- Fix: after expansion re-read the parent's status; if it is no longer
  `expanding`, do not enqueue children (or enqueue them as skipped/canceled)
  and keep the user's status. Same check after a page's link harvest.

### 2.4 (P2) `auto_resumes` is never reset — once spent, every later stall, including after the user's own retry, is an instant `failed` (`queue/s6_counters.py` A/B)
- `store.py:3299-3307` (`pause_url` increments; no path resets);
  `fetch.py:2243` (the check).
- `download_auto_resumes=1`: run 1 → paused (1 of 1); timer wake → stall →
  failed; user retry → stall → failed in 3.6 s; retry again → failed,
  auto_resumes still 1. The "paused … tries again by itself" behaviour is
  gone for good for that file.
- Fix: `auto_resumes = 0` in `set_url_status_by_id`, `requeue_failed`,
  `force_size_requeue` (see 4.1 for the one reset fragment).

### 2.5 (P2) `list_urls` hides actionable pasted rows once more than 500 links were pasted (`queue/s9_actions.py` B)
- `store.py:2735-2760`: the top-level slice is `parent_id IS NULL … LIMIT
  500`; the actionable slice covers `parent_id IS NOT NULL` only.
- 600 pasted links, rows 1–3 failed, 4 oversize, 5 parked → none of them in
  `list_urls()`. Bulk retry reaches the failed ones; nothing reaches the
  oversize pasted row (with 2.2, no way back).
- Fix: make the actionable slice status-based regardless of `parent_id`.

### 2.6 (P2) `small_file_downloads` lanes never help on a fresh queue — sizes are unknown at claim time, so the "small-only" slot takes the first unsized big file (`queue/s7_lanes.py`)
- `store.py:2610-2628` (unsized rows count as small); `fetch.py:2979-2996`.
- 50 big (2.5 MB, paced) + 5 small, 6 slots, 3 small lanes: the 5 small
  files were requests 50–54 of 55 in both variants — claimed at 23.9 s of a
  29.5 s drain (sized) and 31.2 s of 40.5 s (chunked, where `bytes_total`
  stays 0 forever). `big_lane` is never set: the handoff is unreachable from
  `run_queue` (review 9 removed it on purpose). At real scale a 5 KB
  provider-reference file behind 50 multi-GB shards waits hours — and the
  rate files loaded meanwhile skip their groups (round 9 3.2).
- Fix: size rows before claiming — record `Content-Length` from the index
  expansion when the TOC carries it, else one HEAD per unsized row in
  `next_queued_url` for the small lanes (or at enqueue, batched); route
  provider-reference files first by name.

### 2.7 (P2) An `oversize` child is re-queued by every re-expansion of its index (`queue/s11_oversize_reexpand.py`)
- `store.py:2580` (child `retryable` includes "oversize"), `fetch.py:856/869`.
- Sized: one GET per re-expansion, oversize → queued → oversize each month.
  Chunked (no Content-Length): downloads up to the limit again each time —
  `confirm_over_gb` bytes (5 GB) per month per such file.
- Fix: do not revive `oversize` on re-expansion unless `bytes_total` (2.2)
  now fits the limit.

### 2.8 (P2) `dedup_key` collapses two DIFFERENT files to one key when the identity param is a "volatile-if-signed" name and a signature is present — the second is never queued, silently (`queue/s2_dedup.py`)
- `fetch.py:53-80` (`_VOLATILE_ALWAYS`: expires / signature / policy /
  key-pair-id / x-amz-* / sig; `_VOLATILE_IF_SIGNED`: token / se / sp / sv /
  sr / st / … when any signature param exists).
- `dl?token=FILE1&sig=a` vs `dl?token=FILE2&sig=b` → one key, add_urls
  added 1 skipped 1; same for `?st=NY&sig=` vs `st=CA`; always for
  `?policy=A/B`, `?expires=…`, `?x-amz-meta-file=1/2`. The code comment
  claims the `?token=<file-id>` listing shape is protected; it holds only
  unsigned. No `filename_for` collision across distinct keys.
- Fix: strip a volatile param only when what remains still differs per
  file; keep `token` / `st` unless the value looks like a SAS / JWT (length,
  charset); add the listed real shapes to the test.

### 2.9 (P2) The set-aside cap re-claims the slot it just freed (seams `setaside_order.py`)
- `fetch_url_record`'s set-aside branch requeues with no `retries` bump;
  `_claim_next` orders by `coalesce(retries, 0), id`.
- One slot, cap 3 s, two 1.5 MB drips at 300 KB/s: claims `(0.1 s, a)
  (4.2 s, a) (6.2 s, b) (10.5 s, b)` — b waited for a to finish; the cap
  only added a reconnect (`Range: bytes=917504-`). The message "so other
  files get a turn" is false; with N slots it is the same slot churning.
- Fix: bump `retries` (the fair tier) or set `retry_after = now + 30 s` on
  set-aside.

### 2.10 (P2) Every TOC / page / allowed-amounts / unknown row leaves its `.fetchmeta` sidecar in `downloads/` forever (queue s0, s1, s3, s8 D, s5: every one of 19 trials)
- `fetch.py:2495, 2500-2503, 2512, 2565, 2579` (`dest.unlink` only); the
  start-up sweep (`store.py:2599` `done_download_filenames`) keys on
  `filename`, which these rows never get.
- Fix: `_meta_path(dest).unlink(missing_ok=True)` beside each of those
  `dest.unlink` calls; sweep `*.fetchmeta` without a `dest` at start.

## Group 3 — waste measured (P2)

_(download and ingest passes — pending)_

**Where a realistic queue spends its time** (throughput `FINDINGS.md` §1,
`throughput/queue_timeline.py default_warm`; 27 files, 753 MB gz, 32.0 M
rows, 3 parser workers, 6 downloaders; ±15 % — other reviewers' tests ran
on the same 4-core box): wall 163.6 s. Downloads + parses t = 0 → 78.6 s
(sum download 14.3 s, sum ingest 224.5 s over 3 workers, parsers starved
≤ 1 s); then the idle-triggered rollup: `update_rollups_incremental`
raises on a fresh store ("built by an older version (or never built)") →
full `rebuild_rollups` **85.0 s** (t = 78.6 → 163.6). The main process
read 6.2–7.7 GB for 191 MB of parquet parts. What moves: only
`parallel_ingests` (ingests done at 156 / 90 / 78 s for 1 / 2 / 3
workers); `parallel_downloads` 1 / 2 / 6, lanes, `early_dedup_mb`,
`delete_raw_after_ingest` are all within noise. A second wave of 14 files
from 7 never-seen payers onto the finished store: 80.6 s, incremental
rollup 41.1 s, no fallback.

**Per byte, one real 118.7 MB gz** (wy_710, 1.11 GB inflated, 1.76 M rows,
under strace; §2): download 4.75 s, ingest 9.8 s, rollup 2.5 s; the file
is read once and inflated once by the worker (rchar 184 MB = 1.55× the
file; the rest is head/tail/window fingerprinting, done twice — 3.4); 4
`url_progress` UPDATEs per download; 83 fsyncs, WAL ≤ 1.8 MB, no
checkpoint stall. Memory: workers 548–786 MB VmHWM (single-read big file
748 MB); main 3.2–3.6 GB during the full rollup, 4.4 GB when a rollup
overlaps ingest. Invariants 1 and 2 hold.

### 3.1 (P1) The analytics rollup doubles a fresh grind and reads the store ~35× (throughput finding 1)
- `fetch.py:2820-2870` (`rebuild_now`), `store.py:3907`
  (`update_rollups_incremental` precondition: a prior full build),
  `store.py:4181-4510` (the build).
- Numbers above: 78 s of parsing, then a 62–85 s serial tail (45–52 % of
  wall) at ≈ 2.3 s per million rows, nothing else running; with 1 worker
  the end-of-grind incremental for all-new payers (73.6 s) costs as much
  as a full one. Overlapping rebuilds mid-grind (`ROLLUP_MAX_STALE_SECONDS
  = 20` experiment) cut wall only 5 % and slowed the parsers 28 %.
- Fix: the designed single-pass `COPY … PARTITION_BY` build (AI_HANDOFF
  C1); on an EMPTY store skip the incremental attempt and go straight to
  the full build; measure s/M rows before and after on this fixture.

### 3.2 (P2) The CLI drain path opens the store without `keep_warm`: 12.6 ms per connection, `write_lock` held 14.9 % of wall (throughput finding 3)
- `cli.py:415` (`mrfx add` with no dashboard), `:543` (`mrfx ingest`).
  `url_progress` 19.3 ms vs 1.2 ms pinned (236 ms each under contention);
  57 s of lock waits over the 27-file drain (6 % per downloader thread;
  longest wait 433 ms) vs ≤ 1 % and 79 ms in serve mode.
- Fix: `keep_warm=True` on every long-running CLI path.

### 3.3 (P2, Windows) `finalize_rates_part` holds `write_lock` through `replace_with_retry`'s ~90 s loop (throughput finding 4)
- `store.py:2370-2375` + `:245-262`: while an export or antivirus holds the
  old part, every claim and status write blocks for up to 90 s.
- Fix: retry outside the lock, or park the swap after ~2 s
  (`_retry_pending_part_ops` already exists for that).

### 3.4 (P3) The fingerprint head is read three times per file (throughput finding 11)
- `_record_download_fingerprint` after the download and `_finish_ok` after
  the ingest both fingerprint the same bytes (≈ 17 MB each; ~35 MB/file).
  `early_dedup_mb=0` cut the main process's reads 6.2 → 5.1 GB on the
  27-file run. Fix: carry the download's fingerprint to `_finish_ok`
  (`DOWNLOAD_IDENTITY` already carries the sha).

### 3.5 (P2) Parser workers survive a hard kill of the main process and never exit (throughput finding 7, §4 C)
- `_parse_worker` probes the parent only inside `report_chunk`
  (`ingest.py:605-626`): after SIGKILL of `mrfx serve`, 3 idle workers
  stayed alive at PPID 1, ~220 MB each, 10+ min and counting. On Windows:
  Task Manager "End task" and the supervisor's `proc.terminate()`
  (`cli.py:1822`) leave them too; only closing the console ends them. Clean
  shutdowns terminated workers 9/9.
- Fix: a parent-liveness watchdog thread in the worker (`prctl
  PR_SET_PDEATHSIG` on Linux, a Job Object with KILL_ON_JOB_CLOSE on
  Windows, else poll `os.getppid()` every few seconds).

### 3.x (P3) A kill between `part.replace(dest)` and `_write_meta` re-downloads a complete file (seams `dest_no_sidecar.py`)
- Complete `dest`, no sidecar → one full GET, 22,632 of 22,632 bytes. The
  window is microseconds; fix cheaply: if `dest` exists without a sidecar
  and its size equals the server's Content-Length (HEAD), hash it locally.

## Group 4 — hygiene and prose (P3)

### 4.1 Counter hygiene on every requeue path (`queue/s6_counters.py` B)
- `store.py:3266` (`force_size_requeue`), `:3281` (`requeue_urls`), `:2567`
  (`_enqueue_one` revive), `:3470`: `park_count` kept (4 → only 2 fruitless
  parks before the long pause), `waiting_on` and `retry_after` kept, `kind`
  never reset (a user-skipped row once `duplicate` stays eligible for
  `revive_skipped_duplicates`), `requeue_oversize_within` keeps
  `force_size = true`, `set_url_status_by_id` writes no `finished_at`.
- Fix: one `_reset_for_requeue` SQL fragment used by every requeue path
  (also 2.4).

### 4.2 The re-download of a `fetched` row whose download vanished runs under `ingesting` (`queue/s8_processor.py` A)
- `fetch.py:2336-2352`: invisible to "stop downloads", shown as
  "processing". Fix: flip to `downloading` around the fallback `download()`.

### 4.3 Serve mode runs one rollup rebuild per idle gap (`queue/s8_processor.py` C/C2)
- `fetch.py:2754` `_rollup_due`: `in_flight == 0` bypasses the 90 s / 4×
  floor; 5 files arriving one at a time with 1.5 s gaps → 4 rebuilds (a
  30-file drain → 1). Bounded by the rebuild's own duration. Optional: apply
  the floor to idle-triggered rebuilds except on drain exit / shutdown.

### 4.4 Windows read (throughput `FINDINGS.md` "Windows read", each with its confidence)
- (a) Raw `dest.unlink` / rename after a row is already terminal:
  `fetch.py:2428-2429, 2437-2438, 2459, 2495, 2502, 2512, 2563, 2579,
  2602-2603, 2639-2640` — a locked file raises into the processor's
  `except BaseException` and overwrites a correct row (see 1.1). Handled
  correctly already: `ingest.py:632, 404/407, 556-563`, `store.py:2379-2418,
  1916-1940, 4583-4597, 5050`, `api.py:1761/2562`, `fetch.py:2278-2279,
  2679-2682`.
- (b) No `fsync` anywhere: a power loss (not a process kill) can leave a
  truncated `.parquet` part under a `done` row — P3, medium confidence.
  Fix: `os.fsync` the part before the rename that makes it live.
- (c) `MAX_PATH`: `filename_for` keeps 120 chars + the 9-char hash; with
  `.part.val` that is 138 past `downloads_dir`; a real UHC name under a
  ≥ ~120-char project path (`OneDrive\Documents\…` is 91) crosses 260 →
  WinError 206/3 unless `LongPathsEnabled`. HOW_TO_RUN never mentions the
  Python installer's "Disable path length limit". Fix: cap the base at
  ~80 and document the installer option.
- (d) Outreach CSVs get doubled line endings on Windows: `outreach.py:280-285`
  emits `\r\n`, `cli.py:846` and `api.py:3701` write via `Path.write_text`
  → `\r\r\n` (a blank line between rows in Excel / a mail merge). Fix:
  `write_bytes` or `newline=""`.
- (e) Updater while the server runs: pip over a loaded `.pyd` fails and
  `updater.py:150-165` blames "no internet"; no port probe. Fix: probe the
  port first and say "close MRF Explorer first".
- (f) Ctrl-C on a busy grind prints tracebacks: `cmd_serve`'s finally
  (`cli.py:320-328`) terminates workers while processors are in
  `dispatch_and_wait` → BrokenProcessPool → heal → resubmit → "cannot
  schedule new futures after interpreter shutdown" (`ingest.py:874-879,
  447`); state recovers on restart. Fix: a shutting-down flag → return
  `requeued`.
- (g) Console encoding: the .bat sets PYTHONUTF8 and the supervisor passes
  it; only `mrfx serve` with REDIRECTED output and no env var crashes on
  the "→" banner (`cli.py:298`) — P3.
- (h) Monotonic clocks count through sleep on Windows (low confidence): a
  lid close > 240 s trips a benign park on wake, > 600 s the stall error
  for that attempt; the 3600 s cap cannot fire from sleep alone.
- Correct as read: no fcntl / /dev/shm / fork; `_pid_alive` Windows branch
  and `getppid` handling; `multiprocessing` spawn with a guarded
  `__main__`, cfg/strings only pickled, `terminate()` on heal; `dedup_key`
  lower-cases the host only; `filename_for`'s hash prefix avoids NTFS
  case collisions; `SO_EXCLUSIVEADDRUSE`; the updater's rollback; the
  `.bat` UTF-8 env.

### 4.5 Stale prose
- `Store.url_progress` docstring: "a multi-minute rollup rebuild holds
  write_lock the whole time" — it does not; rollups serialize on
  `rollup_lock` and take `write_lock` only around view DDL
  (`store.py:3949, 4112, 4295, 4371`); measured held 0.07–1.85 s per
  rebuild in serve mode (throughput §5).
- GETTING_STARTED "Making a big queue finish faster" item 1 ("cores − 1,
  stops at 8"): `_ram_worker_cap` gives 1 worker on 8 GB and 3 on 16 GB;
  say so. `fetch.py:870` says "1 MB chunk"; it is 64 KB (`:580`). No
  Defender-exclusion advice for `data/` (medium confidence that it
  matters).
- AI_HANDOFF Step 4: replace with the graph (2.0).

## Checked and found correct — do not re-chase

_(the union of the four passes' lists — pending)_

- Queue (`queue/FINDINGS.md` §3): crash-resume over 20 random kills; twin
  kills at the claim instant; twins in both lanes, in sequence, a third
  copy, owner stopped mid-ingest, forgotten owner whose retry 403s (kept
  honest, by design), user-skipped twin retried; the early-dedup "waiting"
  copy vs a CLI drain exit; self-referencing and mutual indexes, relative
  locations after a redirect, a file under two plans, a child also pasted
  directly; the monthly refresh never double-queues, dated indexes retried
  once a day through day 10, a dead payer costs 2 GETs/day for 10 days then
  stops; a parked big row woken by an idle small-only slot is claimable;
  user actions during the disk-full sleep and during a park stick; a dead
  downloader thread hands its row back; vanished dest/sidecar re-downloads;
  `delete_raw_after_ingest: false` + forget removes the raw copy;
  `dedup_key` misses (http/https, trailing slash, path case, %-case, default
  port, dot-segments) cost a wasted download, never a loss; nothing moves
  `done` except the monthly requeue.
- Seams: `Content-Encoding: deflate` bodies decode correctly (`done >
  total` passes; `content_encoding.py`); `parallel_downloads: 1` is
  honoured; `_reserve_disk_or_raise` and the mid-stream disk message both
  match `fetch_url_record`'s disk-full branch; `cmd_add` hands links to a
  running dashboard and 20 CLI commands refuse to open a live server's store.

## Deliverables

1. One commit per group, message = what + why + verified-with-numbers,
   naming each test in `tests/mrfx/test_review11.py` and the value it failed
   with on 1064203. Every change to what gets loaded carries a before/after
   row-identity check on real files (How to work).
2. Re-run the kill harnesses after the fixes (`queue/s5_crash.py` 20 trials,
   `queue/s4k_twin_kill.py`, the ingest pass's mid-parse kills) and put the
   counts in the status block.
3. Re-measure the throughput pass's timeline on the same fixture queue
   (`throughput/queue_timeline.py`) and put before/after in the status block.
4. Version 0.7.5; GETTING_STARTED "What changed" in the user's words (lead
   with what a user would have seen: a capped index now loads the rest, an
   oversize file re-queues when the limit is raised, "Clear queue" sticks);
   AI_HANDOFF Step 4 replaced by the graph and a "Round 11 additions" list
   with the rule each fix must keep.
5. Full suite green, ruff 0, `tests/slow_tests.txt` regenerated, CLAUDE.md
   counts; `git archive --format=zip --prefix=Claudevideo/ -o <scratch>/mrfx_project.zip HEAD`
   sent to the user with the update steps (close the app → drag the zip onto
   "Update MRF Explorer.bat" → start → Ctrl+Shift+R at http://localhost:8377).
6. The status block at the top of this file.
