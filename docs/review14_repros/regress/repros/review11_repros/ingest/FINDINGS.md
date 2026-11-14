# Review 11 — file ingest and skip/dedup rules (baseline 1064203, v0.7.4)

Adversarial review of `mrfx/ingest.py`, `mrfx/sniff.py` and the part/claim side of
`mrfx/store.py`. Every finding was reproduced by a script in this folder, run as
`/home/user/Claudevideo/.venv/bin/python <script>` from this folder (scratch under
`/tmp/claude-0/-home-user-Claudevideo/4633d594-0139-5f8e-a935-ae9c76b72000/scratchpad/audit14/regress/r11tmp/ingest/`, deleted after each run). Nothing under `mrfx/` or `tests/`
was changed. Scripts: `h.py` (harness), `dl.py` (local HTTP server, from review 9),
`s1_containers.py`, `s2_samename.py`, `s2b_keep_stuck.py`, `s3_claims.py`, `s4_kill.py`,
`s4b_orphans.py`, `s5_reingest.py`, `s6_companion.py`, `s6g_mem.py`, `s7_pool.py`
(+ `/tmp/claude-0/-home-user-Claudevideo/4633d594-0139-5f8e-a935-ae9c76b72000/scratchpad/audit14/regress/r11tmp/ingest/site/sitecustomize.py`, the worker-killing shim it describes),
`s8_oversize.py`, `s9_eff.py`, `s9b_opens.py`, `s10_honesty.py`, `s10b_fastcheck.py`.

Severity: P0 double/lost/wrong rows or stuck forever on a real path; P1 wrong state or
sentence on a real path; P2 waste measured; P3 cosmetic.

---

## F1 (P0) — The inbox loads byte-identical files under different names as many times as they are dropped

**Where:** `mrfx/ingest.py:1679-1719` (`scan_inbox`: a name the store does not hold goes
straight to preflight + ingest; `same_name_verdict` / `Store.done_file_with_sha` are consulted
ONLY when the NAME is already in `files`). The URL path has `claim_content_ingest`,
`find_url_with_same_content` and the early fingerprint check; the inbox path has no content
check at all. `/api/upload` (`mrfx/api.py:2534-2558`) compares against the same name only.

**Scripts:** `s2_samename.py` (A, A2, G), `s1_containers.py`, `s3_claims.py` (A, B2).

**Observed:**
- `s2` A: `acme_2026-05.json.gz` and `acme_2026-05 (1).json.gz` (the browser's second-download
  name, same bytes) in one scan → both `done`, 3 rows each; `SELECT payer, billing_code,
  tin_value, npi, negotiated_rate … HAVING count(*) > 1` → **3 contract lines held twice**,
  each with `count(DISTINCT source_file) = 2`. A2: a third copy in a LATER scan, when
  `store.done_file_with_sha(sha)` already answers `acme_2026-05 (1).json.gz`, loads a third time.
- `s1`: the same 3-row document under 12 container names → 42 rows for 8 distinct contract lines.
- `s3` A: a LINK to the bytes and an inbox copy of the same bytes processed concurrently
  (`run_queue(drain=True)` thread + `scan_inbox`) → url row `done 300`, inbox file `done 300`,
  **300 contract lines held twice**. B2: bytes loaded from a link first, then the same bytes
  dropped in the inbox under another name → loaded again (300 dup lines). B1 (inbox first,
  then the link) IS caught by the early fingerprint check (`skipped/duplicate`, 0 dup lines).

**Expected:** invariant 6 ("content-sha dedup skips byte-identical twins") on every path;
the rates table never holds two copies of one contract line. Every number that counts
sources or pools raw rows (source_count, QA duplicate ratios, per-visit value, anything
reading `rates` rather than the dedup view) is inflated by the copies.

**Fix:** in `scan_inbox`, for a name NOT in `files`, compute `_sha_of(p)` (already cached per
path/size/mtime, ≤ 4 GB) and when `store.done_file_with_sha(sha)` names a loaded file record
a `skipped` / "duplicate of X" row and move the copy to processed/ instead of ingesting
(`_finish_ok` records `full_sha` for inbox AND link loads, so both twins are found); the
same check in `/api/upload` before `unique_inbox_name`.

---

## F2 (P0) — "Keep both" chosen, then a restart before the load: the file is stuck forever with no way out

**Where:** `mrfx/ingest.py:1630-1632` (`resolve_choice('keep')` sets the row `queued` and
leaves the file in `inbox/.held/`), `mrfx/ingest.py:1671-1677` (start-up re-reads only
`inbox/.replacing/`; `.held/` is never scanned), `mrfx/api.py:2351-2355` and `mrfx/cli.py:908`
(forget refuses a `queued` row), `Store.recover_stuck_files` (flips only `processing`).

**Script:** `s2b_keep_stuck.py` (first half); also `s2_samename.py` C2.

**Observed:** after `resolve_choice(held, "keep")` (what `POST /api/files/{name}/choose` does
before starting its ingest thread) and a restart: row `queued`, file at `inbox/.held/<name>`;
three `scan_inbox` passes return `[]`; `DELETE /api/files/<name>` → 409 "this file is being
processed right now — wait for it to finish, then remove it"; `POST …/choose` → 409 "this
file is not waiting for a choice"; `POST …/confirm` → 404. The Files tab shows a `queued` row
for the rest of the store's life that cannot be removed, retried or re-chosen. The same state
results if the choice's background thread dies (`_bg_safe` only logs). The Replace branch
is fine (`.replacing/` is picked up at the next scan — verified in `s2` C1: `done`, 4 rows).

**Fix:** have `scan_inbox` load `inbox/.held/*` whose files row is `queued` exactly like
`.replacing/*` (or have `resolve_choice('keep')` move the file into the inbox under its tagged
name so the normal scan loads it), and have `recover_stuck_files` flip a `queued` files row
that no live claim owns to `failed — press retry`.

---

## F3 (P1) — A `blobs` file listing dropped in the inbox is recorded `done`, 0 rows, with no message

**Where:** `mrfx/sniff.py:293-297` classifies `{"blobs": […]}` as `blob_listing`, verdict
NOT A RATE FILE, message "the files it lists will be queued automatically" (true only on the
URL path); `mrfx/ingest.py:1152-1176` quarantines `toc`, `allowed_amounts`, `unknown`, handles
`provider_reference`, and lets every other type fall through to the in-network parse.

**Scripts:** `s1_containers.py` (`u_blob_listing.json`), `s10_honesty.py`.

**Observed:** files row `done / blob_listing / 0 rows / error NULL`, file moved to processed/.
A TOC or an allowed-amounts file dropped the same way is `quarantined` with its sentence.

**Fix:** add `blob_listing` to the quarantine branch ("a portal file listing — paste its link
on the Links tab instead"), or branch on `pf.verdict == "NOT A RATE FILE"`.

---

## F4 (P1) — A parser worker that dies leaves the file `failed` with a Python exception as its sentence

**Where:** `mrfx/ingest.py:875-880` (BrokenProcessPool → `heal()` → one retry → the second
exception propagates) and `mrfx/ingest.py:1343-1365` (`plain_parse_error` passes it through).

**Script:** `s7_pool.py` mode `exit` (the shim calls `os._exit(1)` in the worker's first
parquet write — a segfault / OOM-kill stand-in; the flag file limits it to file A).

**Observed:** `files.error = "BrokenProcessPool: A process in the process pool was terminated
abruptly while the future was running or pending."`. Everything else is right: the pool heals,
the next file loads on it (`B → done 25`), A retried from failed/ loads (`done 20`), no temp
leftovers, no orphan worker (only `multiprocessing.resource_tracker` survives `shutdown_now`,
which is normal). The hung-worker path (mode `hang`, parent clock accelerated 2500x) ends in a
plain sentence — "parser worker wedged (no progress for 60 min) — worker killed; press retry to
try this file again" — and heals the same way; the kill lands after two 15-minute warnings plus
the 60-minute both-frozen window, i.e. one processor thread is blocked ≈ 2 h 50 min per wedged
worker (`stall_warn` 900 s / `stall_kill` 3600 s, `ingest.py:764-770`).

**Fix:** map `BrokenProcessPool` in `plain_parse_error` to "the parser process for this file
was ended by the system twice (usually out of memory) — press retry; if it happens again,
lower parallel_ingests".

---

## F5 (P1) — A query that runs across a part swap can fail with "Parquet file is likely corrupted"

**Where:** `mrfx/store.py:2401` (`_swap_in_part`: `replace_with_retry(tmp, path)` over the LIVE
part name) with `mrfx/store.py:2325` (`rates` = `read_parquet('rates/*.parquet')`; DuckDB reads
a part's footer at bind and re-opens the path to scan).

**Script:** `s5_reingest.py` (a reader thread on its own connection loops `SELECT count(*) FROM
rates WHERE source_file = ?` while the same name is re-ingested 12 times with 400- and 500-row
versions).

**Observed:** 1,753 reads returned exactly 400 or 500 — never 0, never 900 (no window of missing
or doubled rows); **3 reads raised `Error('Parquet file is likely corrupted, dictionary offset
out of range')`** — the footer of one version with the bytes of the other. Rows are never
wrong; a dashboard request can fail once, mid-swap, and on a multi-GB re-ingest the swap of a
big part is the same window.

**Fix:** never rewrite a live part path in place: write `<key>.<generation>.parquet`,
re-register the view, then delete the previous generation (Linux readers keep the old inode;
on Windows the old file joins the existing doomed/parked mechanism). Or retry readers once on
that error class.

---

## F6 (P2) — Parse-pool workers outlive a killed server forever (idle, ~100 MB each)

**Where:** `mrfx/ingest.py:598-625` (`report_chunk`: the parent-liveness check runs only on a
chunk/percent change — for a file under 64 MB compressed `chunk` stays 0 and the check never
runs after the first report), `ParsePoolManager._make` (`ingest.py:412-418`: no
`initializer`), and `concurrent.futures.process._process_worker`, whose idle loop blocks on
`call_queue.get()` with no parent check.

**Scripts:** `s4b_orphans.py`, `s4_kill.py` (`orphan_life`).

**Observed:** parent (2-worker pool, one file) killed 1.25 s into the parse: both workers alive
at t+5 s and **still alive, ppid 1, at t+240 s** for the 118 MB big-path file AND the 2 MB
file; their CPU stops growing within 5 s (the active worker DOES stop — ParentGone works —
and then idles in the pool loop; the idle worker never had a reason to look). All 20 `s4`
trials: 2/2 workers alive 45 s after the kill. Independent corroboration: during this review
two 3-worker pools from another session's tests were sitting at ppid 1 for 11+ minutes.
A crashed/killed `mrfx serve` therefore leaves `parallel_ingests` processes per crash, each
holding a Python + duckdb + pyarrow image, until the user logs off.

**Fix:** give the pool an `initializer` that starts a daemon thread in every worker polling
`os.getppid()` (or `_pid_alive(parent)` on Windows) every few seconds and calling
`os._exit(0)` when the parent is gone; this covers idle and active workers regardless of
chunk size.

---

## F7 (P2) — A rate file whose raw copy is gone is never re-read when its provider list lands, and nothing says so

**Where:** `mrfx/ingest.py:1409-1456` (`requeue_skipped`: processed/ → inbox/ → downloads/ →
`requeue_url_for_file`, which returns False when the file has no queue row; nothing is
written to the files row in that case).

**Script:** `s6_companion.py` B' (`move_processed: false`, the user deleted the raw file).

**Observed:** reference file `done 3 ids`; rate file stays `done / 0 rows / ref_groups_skipped 2
/ missing_ref_ids 3 / error ''`. With the default `move_processed: true` the copy is in
processed/ and the inbox path re-reads it (B: `done 3 rows, skipped 0` on the second scan; a
third scan loads nothing twice). A (reference first), C (never), D (empty list → 3 missing,
counted), E (remote-only list → remote_ref_groups 2, counted) are right.

**Fix:** when neither a raw copy nor a queue row exists, write the files row's `error` ("its
provider list has now arrived — drop this file in again to pick up N rate groups").

---

## F8 (P2) — Each inbox ingest reads the file 2.5 times; 1.5 of them are avoidable

**Where:** `mrfx/ingest.py:353-385` (`_finish_ok`: `file_fingerprint` reads the first
`early_dedup_mb` = 15 MB + last 1 MB + 8 windows; `hash_file` then reads the whole file again
for the identity sha, files ≤ 4 GB), after the parse already streamed every byte.

**Scripts:** `s9b_opens.py` (every `open()` of the source attributed to its call site, bytes
read per handle; 35.5 MB real UHC file, in-process fast path), `s9_eff.py` (strace/rchar).

**Observed (per handle):** preflight 0.1 MB (+ three 4-byte opens), parse 35.5 MB,
`file_fingerprint` 15.7 + 1.0 + 0.5 MB, `hash_file` 35.5 MB, `gz_members` 0.1 MB — **88.6 MB
= 2.49x the file, 10 opens**. `s9_eff.py` (22 MB file, rchar of the main process): in-process
fast 3.5x, classic 3.5x, pooled 4.1x in the MAIN process alone (which never parses: the worker
reads the file once more), URL path 11.1x / 16 opens (includes the loopback download and the
fetch-side checks; not attributed further — the download side was review 9's). On a 4 GB local
drop the identity hash alone is a second 4 GB read; above 4 GB it is skipped and the file's
identity is size+mtime only (`identity.LOCAL_HASH_MAX_BYTES`).

**Fix:** hash the stream inside the parse (`_CountingRaw` already sees every compressed byte;
feed a `ContentHasher` like the downloader does) and take the 15 MB head digest from the same
pass; `_finish_ok` then reads only the 1 MB tail.

---

## F9 (P2) — Peak memory scales with the largest `in_network` item, not with the file (flat across file size)

**Where:** `mrfx/parser.py` (an item is materialised whole on the classic path; the fast path
holds the item's bytes); `_DeferredSpool` (`parser.py:394-470`) correctly spills deferred
groups past 10,000.

**Script:** `s6g_mem.py` (fresh subprocess per run, `VmHWM`; a rate file with NO own table whose
groups all cite a 1,000-id companion, so every group is deferred to end of file).

**Observed:** `python s6g_mem.py` (5 items, one per code, 150k-300k groups each): 100 MB raw →
peak 577 MB fast / 327 MB classic; 200 MB raw → **909 MB fast / 516 MB classic** (baseline ≈
250 MB). `python s6g_mem.py 100` (same groups as 7,500-15,000 items of 100 groups): 100 MB →
296 / 283 MB; 200 MB → **311 / 284 MB** — flat. So invariant 1 holds for file size; a single
item of 300k provider groups (one code with every provider of a national payer) costs ≈ 0.6 GB
on the fast path per worker, ×3 workers. Worth a line in the handoff's performance envelope.

---

## F10 (P3) — Smaller wording / state items (each reproduced)

- `s10_honesty.py`: a small-path file with none of the target codes is `done 0` with
  `qa.messages = NULL`; only the big-file short-circuit writes "scanned: none of the target
  billing codes appear in this file". The Files tab shows a bare 0.
- `s1_containers.py` `l_utf16.json`: a UTF-16 file (Windows "Unicode" save) is quarantined as
  "no in_network / provider_references / reporting_structure key found"; `i_zero_byte.json`
  gets the same sentence instead of "the file is empty (0 bytes)".
- `s1` `n_trailing_garbage.json.gz`: non-zero bytes after the gzip member →
  "unreadable: BadGzipFile: Not a gzipped file (b'GA')" (CPython refuses trailing garbage;
  zero padding is skipped). `s8_oversize.py` (a): zero padding is consumed ONE BYTE AT A TIME
  by CPython's `_GzipReader._read_eof`: 0.13 s for 1 MB, 0.93 s for 8 MB (≈ 0.12 s/MB), so a
  pre-allocated/sparse copy with gigabytes of zeros costs ~12 min per 6 GB in preflight and
  again in the parse. Not the app's code; a sentence would help.
- `s1` `m_truncated_1byte.json.gz`: caught by the 1 MB preflight only because the fixture is
  small; for a real file the truncation is met at the END of the full parse, the rows read so
  far are discarded (honest) and the sentence is `EOFError: Compressed file ended before the
  end-of-stream marker was reached` — technical.
- `s8` (b): after `/confirm` the files row's `error` is `''` rather than NULL.
- `s2_samename.py` E: a file named `beta_2026-05` whose header says `2026-07-01` is recorded
  `file_month 2026-07, published_month 2026-05` (header wins, by the rule); with another header
  month it loads beside as `beta_2026-05-f9ce5ee2.json.gz` — the content tag hides the month the
  user would look for on the Files tab; a `-2026-08` tag would read better.
  `unique_inbox_name(".json.gz")` → `.json-t.gz` (a dot-file; harmless).
- YAML trap (`/tmp/claude-0/-home-user-Claudevideo/4633d594-0139-5f8e-a935-ae9c76b72000/scratchpad/audit14/regress/r11tmp/ingest/logs/yaml.out`): `config/mrfx.yaml:232-233` says
  "`mode: off` = don't identify names" but an unquoted `off` is YAML's boolean False, so
  `mrfx status` exits 1 with "config problem: … enrichment.mode: Input should be 'api', 'bulk'
  or 'off'" — the shipped comment recommends a spelling the loader refuses (`"off"` works).

---

## Checked and found correct

- **Container matrix** (`s1_containers.py`, 26 shapes, 3 scans): .json / .json.gz / .zip of
  one / .zip of many (largest member) / zip-in-zip / `.gz` that is plain / `.json` that is
  gzip / gz with a wrong extension / BOM (plain and inside gz) / CRLF / multi-member gz / zip
  with a .json.gz member all `done` with the right rows, moved to processed/. 0-byte, UTF-16,
  1-byte-truncated gz, half gz, trailing garbage, zip with no json, top-level array, `{}`,
  header-only and allowed-amounts are quarantined (a plain truncated JSON is `failed` with
  "file ended mid-structure — truncated download or not valid JSON"), moved to failed/, and
  never retried by scans 2 and 3. A file still growing is skipped by scan 1 and loaded by
  scan 2 (0.3 s double stat). `rollups_stale()` False; no inbox leftovers.
- **Same name** (`s2_samename.py`): an identical re-drop under its own name (new mtime) is
  recognised by sha and moved out, not reloaded; a different file for the same payer+month is
  held (`needs_choice`) and not re-held by later scans; Replace chosen, then a restart before
  the load → the next scan loads it from `.replacing/` (`done` 4 rows, old part replaced, no
  leftovers); another month under the same name loads beside under a content-tagged name;
  `unique_inbox_name` increments past a directory AND a store collision and keeps a middle
  `.json`.
- **Confirm route ordering** (`s2b_keep_stuck.py`, second half): `/confirm` starts its ingest
  thread BEFORE its `upsert(status='queued')`; 30 confirmed 0.5 MB files (threshold 100 KB) all
  ended `done` — the inversion exists in the code but never lost the race in 30 tries.
- **Content claim** (`s3_claims.py`): a link whose bytes are already loaded from the inbox is
  skipped by the early check (0 dup lines). A crash leaving url_queue `ingesting` + files
  `processing` + an `inflight_fingerprints` row: at restart `recover_stuck_urls` re-queues 1,
  `recover_stuck_files` flips 1 to `failed — interrupted by a restart`; the drain loads the file
  exactly once (done 300, 0 dup lines, 0 inflight rows). An inbox row left `processing` with its
  file in the inbox is re-ingested exactly once by the next scan. Review 9's "in-flight
  fingerprint rows left behind" is fixed: 0 rows after a claim-skip and after
  `cancel_all_downloading`.
- **Crash mid-ingest** (`s4_kill.py`, seeds 40465 and 41123, two real files — BCBS-MS 25 MB /
  2,660,356 rows and UHC-NY 35 MB / 20,741 rows — through a 2-worker pool; `Popen.kill` at 15
  random instants over the 11 s clean run plus 5 window kills: before/after
  `finalize_rates_part`, before/after `_commit_done`, before `_finish_ok`): **20/20 trials**
  restart to exactly one `done` row per file with the clean-run row counts, no `.parquet.tmp` /
  spool / progress leftovers after the restart's sweep (a `.parquet.tmp` of the live pid is
  correctly present right after the kill and swept at the next open), within-file repeated
  lines equal to the clean store's 416,826 (no doubling), `rollups_stale()` False after the
  restart drain, store readable every time. After-commit kills leave the raw file in the inbox
  with a `done` row; the next scan recognises it (`same_name_verdict` size+mtime≤finished_at
  fallback) and moves it instead of reloading.
- **Re-ingest / forget** (`s5_reingest.py`): 12 alternating re-ingests of one name leave one
  part and exactly the last version's rows (never 0, never doubled — F5 is the transient read
  error); `scan_inbox(force=True)` reloads a done file in place (by design); forget removes the
  part, files row, fingerprint and every raw copy; a re-drop of the same bytes loads again;
  forget during an in-process parse AND during a pooled parse is refused with the sentence, the
  parse finishes with its rows, forget afterwards leaves nothing; a re-ingest to 0 rows (same
  name, no target codes) drops the old part (`done 0`, parts `[]`, rollups not stale).
- **Companions** (`s6_companion.py`): reference-first; reference-later (the INBOX path re-reads
  the rate file from processed/ — review 9 fixed the LINK path, the inbox path is right too, and
  a third scan loads nothing twice); never; empty list (3 missing, counted); remote-only list
  (2 remote groups, counted); re-dropping the identical reference does nothing; a DIFFERENT
  reference for the same payer+month is held for the user's choice like a rate file.
- **Worker pool** (`s7_pool.py`): dead worker → heal → retry once → `failed` (F4 wording),
  next file loads on the healed pool, the retry loads, no temp files, no orphan workers; hung
  worker → the both-frozen watchdog kills it, `failed` with a plain sentence, pool serves on.
- **Oversize** (`s8_oversize.py`): over `confirm_over_gb` → `pending_confirmation` with the
  sentence, kept in the inbox, re-parked by the next scan and after a restart; `/confirm` →
  `queued` → `done 5 rows` → processed/; `/confirm` again → 404; pending → DELETE → file and
  row gone, next scan clean; a gzip whose trailer claims 4 GiB-1 on a 1 KB body is routed by
  the larger trailer estimate and quarantined by gzip's own length check ("Incorrect length of
  data produced") — never a wrong estimate reaching the parse.
- **Honesty** (`s10_honesty.py`, `s10b_fastcheck.py`): a 0-row file stays `done 0`, is moved to
  processed/ and never retried over 3 scans; a file with no `reporting_entity_name` loads as
  payer "Unknown payer" (rows and files row agree), and such a file citing references counts
  its 1 skipped group; TOC and allowed-amounts in the inbox are quarantined with their
  sentences; a file the fast path declines (numeric billing code, duplicated/unsorted modifiers
  and service codes) gives identical rows fast vs classic (modifiers `['CQ','GP']`, service
  codes `['02','11']`); `mrfx fastcheck` on 3 real files (ND HMHS 22 MB / 0 rows, UHC-NY 35 MB /
  20,741 rows, BCBS-MS 25 MB / 2,660,356 rows): **3 of 3 IDENTICAL** — same rows, same QA
  counters, same header; fast 2.3-4.7x faster.
- **Regressions of round-9 Group 3:** none found — 3.1 (big reference file on the two-pass
  path) is covered by the existing tests and `s6` shapes; 3.2 inbox path verified above; 3.3
  flat memory across file size (F9 is the per-item residue); 3.4 empty/remote companion entries
  counted; 3.5 in-flight fingerprint rows cleaned.
