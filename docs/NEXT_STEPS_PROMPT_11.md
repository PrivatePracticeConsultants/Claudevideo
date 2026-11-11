# Prompt for the next session, round 11 — the link → download → ingest → skip protocol, made bulletproof (written for Claude Opus 5.5)

> **STATUS: not started.** Replace this block with what you closed, the
> numbers, and what you left (with the reason), as rounds 7–10 did.

Rounds 1–10 are `docs/NEXT_STEPS_PROMPT.md`, `_2.md` … `_10.md`. This memo
comes from a review on Nov 10–11, 2026 at HEAD 1064203 (version 0.7.4).
The user, a non-technical consultant running `mrfx serve` on Windows,
asked for a deep review of **the protocol for downloading, ingesting and
skipping — make it bulletproof and infallible while as efficient as
feasible.**

Method — four independent passes, none by the author of the code under
review, each against a scriptable local HTTP server and scratch stores
(never the repo's `data/`), with real process kills (`subprocess.Popen` +
`kill()` at random instants, seed logged) and the real payer files on the
box; plus the author's own checks of the seams between the passes. Every
item below was reproduced by a script that was run; the script's name is
on the item and each folder has a `FINDINGS.md` with the full evidence:

1. **Download transport** — `docs/review11_repros/download/`: `download()`,
   resume / Range / validators, stall / park / pause / cap clocks,
   segments, disk reservation, cancel, early dedup, 20 kill trials.
2. **Queue state machine** — `docs/review11_repros/queue/`: the real
   transition graph (derived by instrumenting every status-changing store
   method), twins, index expansion, lanes, counters, user actions, 20
   random kills + 2 claim-instant twin kills.
3. **Ingest and skip rules** — `docs/review11_repros/ingest/`: 26
   container shapes, same-name / same-content, the content claim, 20 kills
   mid-parse on real files, re-ingest, companions, the worker pool,
   oversize, bytes-read accounting, honesty, `fastcheck` on 3 real files.
4. **Throughput and Windows hazards** — `docs/review11_repros/throughput/`:
   the per-file timeline of a realistic 27-file queue, per-byte accounting,
   memory, shutdown / restart, lock stalls, a code read for Windows-only
   breakage.
5. **Seams** — `docs/review11_repros/seams/`: kept-download reuse after a
   republish, a static index's children on refresh, the set-aside cap's
   queue position.

Severity: **P0** rows wrong, doubled or lost, or a row stuck forever, on a
real path · **P1** wrong state, silent non-load, or a slot wasted on a
real path · **P2** measured waste, or a rule the docs promise and the code
does not keep · **P3** cosmetic / hygiene / stale prose.

**Three P0s, all on paths a careful user walks:** (1.1) the inbox has no
content check — a byte-identical file under a second name, or a link plus
an inbox copy of the same bytes, loads twice and every raw count doubles;
(1.2) a resume splices a republished file's bytes into the kept `.part`
and marks the row done; (1.3) "Keep both" then a restart leaves a file
`queued` forever with no button that works. Invariant 6 otherwise holds
under every kill the reviewers threw at it: queue 19/19 valid trials,
download 20/20, ingest 20/20 on real files (one `done` row per file with
the clean run's row counts, no leftovers after the start-up sweep, no
double rows), twins exactly one ingest in every ordering. After the P0s
the layer is: states a user cannot leave, files that silently never load,
responses the client accepts without checking, and work done two to four
times.

## How to work

- Read `CLAUDE.md`, then `docs/AI_HANDOFF.md` in full (Step 3 invariants,
  Step 4 URL states — which this review corrects, 2.0 — and the "Round 7 /
  Audit 8 / Round 9 / Round 10 additions"), then this file, then the five
  `FINDINGS.md` files.
- Each fix: reproduce first (the script named in the item), fix the class,
  leave a regression test in `tests/mrfx/test_review11.py` that fails on
  1064203, re-run the script. Kill tests belong in the suite too (harness
  patterns: `queue/s5_driver.py` + `s5_crash.py`, `download/c10_driver.py`
  + `c10_kill.py`, `ingest/s4_kill.py`; `Popen.kill` on a driver you
  spawned — never a shell `kill`).
- **Every change to what gets loaded must keep the rows byte-identical for
  a file that loads today.** Prove it on the three real files under 120 MB
  in the session scratch `real/` folder (or any three real payer files you
  have): ingest before and after, compare each part's row count and a
  sorted hash of its rows; `mrfx fastcheck` for the parser paths. Nothing
  in this round changes a parsed number; if a diff appears, the change is
  wrong.
- Repro scripts were written against `/tmp/mrfx_review11/<area>/…`, each
  folder with its harness (`download/_harness.py`, `queue/h.py`,
  `ingest/h.py` + `dl.py`, `throughput/gen_fixtures.py`,
  `seams/harness.py`); their first lines name the paths — point them at
  your scratch folder. The session's big stores and real files may not
  exist in your container; every repro builds its fixtures from nothing
  except the three throughput real-file rows and `fastcheck`.
- Chromium: Playwright is installed; `executable_path='/opt/pw-browsers/chromium'`,
  args `--headless=new --no-sandbox`. Never run `playwright install`.
- Order: Group 1 first (the three P0s before anything else), then 2, 3, 4.
  One commit per group (what + why + verified-with-numbers, each test
  named with the value it failed with on 1064203).

## Group 1 — rows wrong, doubled, lost, or a file stuck (P0/P1)

### 1.1 (P0) The inbox loads byte-identical files under different names as many times as they are dropped — and a link plus an inbox copy of the same bytes both load (`ingest/s2_samename.py` A/A2/G, `s1_containers.py`, `s3_claims.py` A/B2)
- `ingest.py:1679-1719` (`scan_inbox`: a name the store does not hold goes
  straight to preflight + ingest; `same_name_verdict` /
  `Store.done_file_with_sha` are consulted only when the NAME is already in
  `files`); `api.py:2534-2558` (`/api/upload`: same-name only). The link
  path has `claim_content_ingest`, `find_url_with_same_content` and the
  early fingerprint check; the inbox path has no content check at all.
- `acme_2026-05.json.gz` and `acme_2026-05 (1).json.gz` (the browser's
  second-download name, same bytes) in one scan → both `done`, 3 rows each;
  `… HAVING count(*) > 1` → **3 contract lines held twice**, each with
  `count(DISTINCT source_file) = 2`; a third copy in a later scan, when
  `done_file_with_sha` already answers, loads a third time. The same 3-row
  document under 12 container names → 42 rows for 8 distinct lines. A
  LINK to the bytes and an inbox copy processed concurrently → url row
  `done 300`, inbox file `done 300`, 300 lines held twice; link first then
  the inbox copy → loaded again. (Inbox first, then the link, IS caught by
  the early fingerprint check.) The instructions on every 403 message —
  "download it in your browser and drop the file into data/inbox/" — walk
  the user straight into this.
- Expected: invariant 6 on every path; the rates table never holds two
  copies of one contract line. Everything that counts sources or pools raw
  rows (source_count, QA duplicate ratios, per-visit value, anything
  reading `rates` rather than the dedup view) is inflated by the copies.
- Fix: in `scan_inbox`, for a name NOT in `files`, compute `_sha_of(p)`
  (cached per path/size/mtime, ≤ 4 GB) and when `store.done_file_with_sha`
  names a loaded file record a `skipped` / "duplicate of X" files row and
  move the copy to processed/ instead of ingesting (`_finish_ok` records
  `full_sha` for inbox AND link loads, so both twins are found); the same
  check in `/api/upload` before `unique_inbox_name`; for files over 4 GB
  use the fingerprint (size + head + tail + windows + trailer) the link
  path already trusts. And make the content claim one mechanism for both
  paths: `claim_content_ingest` keyed on sha, called by `ingest_file`
  itself, so a link and an inbox copy racing can never both win.

### 1.2 (P0) A resume splices CHANGED content into the kept `.part`, and the row ends `done` with mixed rates (`download/c02b_json_splice.py`, `c02_range_odd.py`)
- `fetch.py:812-815` (a 206 is accepted after parsing only the TOTAL out
  of `Content-Range`), `:903` (`.val` is written only when the first
  response carried a strong ETag / Last-Modified; nothing else about the
  first response is remembered), `:999` (the only completeness check is
  `done < total`). The 206's own ETag / Last-Modified is never compared
  with the validator we sent.
- c02b (plain `.json`, through the queue): v1 (97110 = 40.0, 97140 = 40.0)
  cut at 300 KB; the resume `bytes=262144-` answered from v2 (both 55.0,
  same size, no validator) → row `done`, 20 rows, `content_sha` equal to
  NEITHER version; the store holds `97110 → 40.0 ×10` and `97140 → 55.0
  ×10`. One file, two vintages, no warning.
- c02 (strong ETag): the resume sends `If-Range: "v1"` and the server
  answers 206 with `ETag: "v2"` (ignores If-Range, as CDNs do) — appended
  anyway, sha matches neither. For `.json.gz` the CRC catches it at ingest:
  row `failed` with `'CRC check failed 0x… != 0x…'`, download deleted, a
  retry succeeds — a P1 state and a wasted download.
- Fix: persist `{validator, total}` from the first response in `.val`; on
  a 206 require `Content-Range` START == `resume_from`, TOTAL == the
  remembered total, and (when the 206 carries a validator) equality with
  the stored one — else unlink the `.part` and restart. The RFC-correct
  path (200 on mismatch → restart, `c02 ifrange_rfc`) already does the
  right thing. Honest residual: a server with NO validator that republishes
  a SAME-size file is undetectable by any client — say so in the
  methodology rather than pretend otherwise.

### 1.3 (P0) "Keep both" chosen, then a restart before the load: the file is stuck forever with no way out (`ingest/s2b_keep_stuck.py`, `s2_samename.py` C2)
- `ingest.py:1630-1632` (`resolve_choice('keep')` sets the files row
  `queued` and leaves the file in `inbox/.held/`), `:1671-1677` (start-up
  re-reads only `inbox/.replacing/`; `.held/` is never scanned),
  `api.py:2351-2355` and `cli.py:908` (forget refuses a `queued` row),
  `Store.recover_stuck_files` (flips only `processing`).
- After `resolve_choice(held, "keep")` (what `POST /api/files/{name}/choose`
  does before starting its ingest thread) and a restart: row `queued`, file
  at `inbox/.held/<name>`; three scans return `[]`; `DELETE` → 409 "this
  file is being processed right now"; `/choose` → 409 "not waiting for a
  choice"; `/confirm` → 404. The Files tab shows a `queued` row for the rest
  of the store's life that cannot be removed, retried or re-chosen. The
  same state results if the choice's background thread dies (`_bg_safe`
  only logs). The Replace branch is fine (`.replacing/` is picked up:
  `done`, 4 rows).
- Fix: have `scan_inbox` load `inbox/.held/*` whose files row is `queued`
  exactly like `.replacing/*` (or have `resolve_choice('keep')` move the
  file into the inbox under its tagged name so the normal scan loads it);
  have `recover_stuck_files` flip a `queued` files row that no live claim
  owns to `failed — press retry`; let forget remove a `queued` row.

### 1.4 (P1) A 206 whose body starts at byte 0 under a plausible `Content-Range` is accepted — the file comes out 29 % bigger than truth (`download/c01_truncate_resume.py cr_start_wrong`)
- `fetch.py:812-815` (the START of `Content-Range` is ignored), `:999`
  (`done > total` is not an error). Truth 911,726 B; resume answered
  `206 Content-Range: bytes 262144-911725/911726` with the WHOLE file as
  body → `download()` returns OK, dest 1,173,870 B (prefix duplicated). A
  gz fails at ingest with a raw CRC message, a `.json` fails to parse — a
  full download wasted and a riddle for the user.
- Fix: refuse a 206 whose start ≠ `resume_from`; treat `done != total` (not
  only `<`) as wrong bytes and restart from 0. (Same `.val` as 1.2.)

### 1.5 (P1) A 206 with no `Content-Range` and a short consistent body is accepted as complete (`download/c01_truncate_resume.py cr_absent_short`)
- `fetch.py:812-813` (`total = None` when the header is missing), `:999`.
  Truth 911,726 B; the resume answered 206, no `Content-Range`,
  `Content-Length: 102400` → OK with a 364,544 B file. The first response's
  `Content-Length: 911726` was known and dropped.
- Fix: remember the total (1.2) and require `done == total` on every path;
  `total` must never become None once known.

### 1.6 (P1) A stale `.fetchmeta` sidecar is trusted blindly: a wrong sha is recorded and a true copy of the file ingests twice (`download/c08b_wrong_sidecar_json.py`, `c08_cancel_sidecar.py wrong_sidecar`, `c04_corrupt_gzip.py`)
- `fetch.py:2150-2165` (reuse: `dest.exists() and keep_p.exists()` → row
  `fetched` with the sidecar's sha, no size or hash check). What makes a
  stale sidecar likely: every failed ingest leaves `<file>.fetchmeta`
  behind (`:2661` and the other failure paths unlink `dest` only; ingest's
  `_finish_file` moves the file to `failed_dir`) — seen in c04 (all 4
  corrupt variants), c05 (html_200, zero_byte, redirect_to_html), c08.
- c08b: dest = correct bytes, sidecar `{"sha": "000…0"}` → A `done`, 0
  GETs, `content_sha = 000…`, `content_fingerprints.full_sha = 000…`; B
  (the same bytes under another URL) → `done`, 300 more rows: **600 rows
  for one file's content**. (For a gz the EARLY fingerprint check, not the
  sha, happens to catch the twin.)
- Fix: unlink the sidecar wherever `dest` is unlinked or moved on failure
  (`:2380, 2428, 2437, 2616, 2661`, `_finish_file(ok=False)`); write `size`
  into the sidecar and refuse one whose size ≠ `dest.stat().st_size` or
  whose mtime predates dest's. (Same root as 2.15.)

### 1.7 (P1) A republished file under the same URL is never fetched again — kept-download reuse, and a static index's children (`seams/stale_reuse.py`, `seams/refresh_static_children.py`)
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
  real files on the box are all month-dated; the refresh is opt-in).
- Fix (one design, shared with 1.2's `.val`): record ETag / Last-Modified /
  Content-Length per loaded URL (sidecar + `url_queue` or the identity
  table). On reuse of a kept download for a REQUEUED row (`requeue_urls`,
  `requeue_url_for_file`, a user retry of a done row) send one conditional
  HEAD; on mismatch download fresh. When a parent index is re-expanded,
  HEAD each `done` child once and requeue those whose validator changed —
  one request per child per month, no re-download when nothing changed; no
  validator → compare Content-Length, else re-download on the monthly
  refresh only.

### 1.8 (P1) A query that runs across a part swap can fail with "Parquet file is likely corrupted" (`ingest/s5_reingest.py`)
- `store.py:2401` (`_swap_in_part`: `replace_with_retry(tmp, path)` over the
  LIVE part name) with `store.py:2325` (`rates` = `read_parquet('rates/*.parquet')`;
  DuckDB reads a part's footer at bind and re-opens the path to scan).
- A reader thread looping `SELECT count(*) FROM rates WHERE source_file = ?`
  while the same name is re-ingested 12 times (400- and 500-row versions):
  1,753 reads returned exactly 400 or 500 — never 0, never 900 — and
  **3 reads raised `Parquet file is likely corrupted, dictionary offset out
  of range`** (one version's footer with the other's bytes). Rows are never
  wrong; a dashboard request fails once mid-swap, and a multi-GB re-ingest
  widens the window.
- Fix: never rewrite a live part path in place — write
  `<key>.<generation>.parquet`, re-register the view, then delete the
  previous generation (Linux readers keep the old inode; on Windows the old
  file joins the existing doomed/parked mechanism); or retry readers once
  on that error class.

### 1.9 (P1, Windows) The download's final rename has no lock tolerance, and its failure path discards the complete `.part` (throughput finding 2 — code read; confidence high on the path, medium on how often AV holds the handle)
- `fetch.py:1004-1005` (`part.replace(dest)`), `:1798` (`_write_meta`'s
  rename). A `PermissionError` (antivirus / indexer holding the file
  without FILE_SHARE_DELETE — the code itself calls this "routine" at
  `ingest.py:396-401` and `store.py:246-250`) escapes
  `_download_reserved` / `fetch_url_record` (`:2168-2176`) to the
  downloader's `except BaseException` (`:2896-2906`) → "failed: unexpected
  download error", a manual retry; the retry sends `Range: bytes=<total>-`
  → 416 → `fetch.py:762-769` unlinks the .part and restarts from byte 0
  (a Range-ignoring host restarts from 0 after three refusals).
- Fix: `replace_with_retry` / `unlink_with_retry` (the store has them) at
  both renames; on a 416 for a `.part` whose size equals the known total,
  treat the file as complete (hash it) instead of discarding it. Then the
  raw `dest.unlink` calls listed in 4.4(a): a locked file there raises into
  the processor's `except BaseException` (`fetch.py:3047-3058`), which
  overwrites a correct `skipped` / `done` row with "failed: unexpected
  worker error".

### 1.10 (P1) A `blobs` file listing dropped in the inbox is recorded `done`, 0 rows, with no message (`ingest/s1_containers.py u_blob_listing.json`, `s10_honesty.py`)
- `sniff.py:293-297` classifies `{"blobs": […]}` as `blob_listing`, verdict
  NOT A RATE FILE, message "the files it lists will be queued
  automatically" (true only on the link path); `ingest.py:1152-1176`
  quarantines `toc`, `allowed_amounts`, `unknown`, and lets every other
  type fall through to the in-network parse → files row `done /
  blob_listing / 0 rows / error NULL`, moved to processed/.
- Fix: branch on `pf.verdict == "NOT A RATE FILE"` ("a portal file listing —
  paste its link on the Links tab instead").

## Group 2 — states the user cannot leave, silent non-loads, wrong sentences (P1/P2)

### 2.0 The real transition graph (queue `FINDINGS.md` §1) — replace AI_HANDOFF Step 4 with it
Derived by instrumenting every status-changing store method. Differences
from the Step 4 prose, each confirmed in `s1_transitions.out`:
`fetched → expanding` never happens (a TOC goes fetched → ingesting →
expanding); `paused` also arises from the early-dedup "wait"; `queued` is
re-entered from `downloading` on disk-full / set-aside / park-limit;
`done` is left by `requeue_urls`, `requeue_url_for_file`, `forget_file`;
`paused` can be skipped. The queue FINDINGS has the whole machine written
down once; keep it that way.

### 2.1 (P1) A capped index tells the user to "press retry on this row" — there is no such button and the API refuses; files past `max_toc_files` silently never load (`queue/s3_toc.py` A/B)
- `fetch.py:1487` `cap_note`, `:2493` (done TOC row carries the note);
  `store.py:3225-3248` `_USER_TRANSITIONS`; `web/app.js:4405` (retry button
  only for failed/skipped/oversize). Index of 5 files, `max_toc_files=3` →
  done with the note; `set_url_status_by_id(idx, "queued")` → False;
  re-paste → added 0, skipped 1; raise the cap and restart → f3/f4 still
  missing. Only the opt-in monthly refresh's `requeue_urls` re-expands it.
- Fix: allow done → queued for `kind IN ('toc', 'page')` in `_enqueue_one`
  and `set_url_status_by_id` (an index carries no rates, so it anchors no
  dedup); render the button; re-expansion then queues the missing children.

### 2.2 (P1) An `oversize` row records `bytes_total = 0`, so raising `confirm_over_gb` and restarting — the documented fix — never re-queues it (`queue/s11b_oversize_raise.py`)
- `fetch.py:856` (`too_big(total)` raised at the headers before any progress
  write), `:2267` (the status write carries no bytes_total);
  `store.py:3470-3490` (`requeue_oversize_within` needs bytes_total > 0);
  the message (`fetch.py:639-646`) says "raise confirm_over_gb for all
  files". Limit 1 MB, 1.5 MB file → oversize, bytes_total 0; raise to 5 GB
  + restart → still oversize, 0 re-queued. Only the per-row "download
  anyway" works, and with 2.7 a pasted row beyond the window has no way
  back at all.
- Fix: persist `bytes_total = total` on the oversize path (carry it on the
  DownloadError as `e.total`, as the handoff path does).

### 2.3 (P1) "Clear queue" pressed while an index is expanding is silently undone (`queue/s9_actions.py` C)
- `store.py:3384` (`clear_queued` flips expanding → skipped);
  `fetch.py:2446-2493` (`_enqueue_children` then an unconditional
  `update_url(status="done")`). `clear_queued()` mid-expansion → 1 cleared,
  TOC skipped; 3 s later the TOC is done and 4 children are queued /
  downloading / fetched / done; all 4 ingest (24 rows).
- Fix: after expansion (and after a page's link harvest) re-read the
  parent's status; if it is no longer `expanding`, do not enqueue children
  (or enqueue them as skipped/canceled) and keep the user's status.

### 2.4 (P1) Non-transport `httpx` errors escape `download()` as "unexpected download error" with a traceback (`download/c05_http_errors.py redirect_loop`, `c05b_bad_encoding.py`)
- `fetch.py:1027` catches only `httpx.TransportError` / `TimeoutException`;
  `httpx.TooManyRedirects` and `DecodingError` are `RequestError`s (httpx
  0.28.1 MRO checked). A 302 to itself: 21 requests, then the downloader's
  `except BaseException` logs a traceback and the row reads `failed /
  'unexpected download error'`; `Content-Encoding: deflate` with a garbage
  body: the same. The reservation is released (nothing stuck), but the
  message is the one reserved for bugs.
- Fix: catch `httpx.HTTPError`; make the two terminal with plain text
  ("this link redirects in a loop" / "the server sent an unreadable
  compressed body").

### 2.5 (P1) A parser worker that dies leaves the file `failed` with a Python exception as its sentence (`ingest/s7_pool.py exit`)
- `ingest.py:875-880` (BrokenProcessPool → `heal()` → one retry → the second
  exception propagates), `:1343-1365` (`plain_parse_error` passes it
  through). `files.error = "BrokenProcessPool: A process in the process
  pool was terminated abruptly while the future was running or pending."`
  Everything else is right: the pool heals, the next file loads on it, the
  retry loads, no leftovers. (The hung-worker path ends in a plain
  sentence but only after ≈ 2 h 50 min per wedged worker: `stall_warn`
  900 s / `stall_kill` 3600 s, `ingest.py:764-770` — consider halving.)
- Fix: map `BrokenProcessPool` in `plain_parse_error` to "the parser
  process for this file was ended by the system twice (usually out of
  memory) — press retry; if it happens again, lower parallel_ingests".

### 2.6 (P2) `auto_resumes` is never reset — once spent, every later stall, including after the user's own retry, is an instant `failed` (`queue/s6_counters.py` A/B)
- `store.py:3299-3307` (`pause_url` increments; no path resets);
  `fetch.py:2243`. `download_auto_resumes=1`: run 1 → paused (1 of 1);
  timer wake → stall → failed; user retry → stall → failed in 3.6 s; retry
  again → failed, auto_resumes still 1.
- Fix: `auto_resumes = 0` on every requeue (4.1's one reset fragment).

### 2.7 (P2) `list_urls` hides actionable pasted rows once more than 500 links were pasted (`queue/s9_actions.py` B)
- `store.py:2735-2760`: the top-level slice is `parent_id IS NULL … LIMIT
  500`; the actionable slice covers `parent_id IS NOT NULL` only. 600
  pasted links, rows 1–3 failed, 4 oversize, 5 parked → none in
  `list_urls()`.
- Fix: make the actionable slice status-based regardless of `parent_id`.

### 2.8 (P2) `small_file_downloads` lanes never help on a fresh queue — sizes are unknown at claim time, so the "small-only" slot takes the first unsized big file (`queue/s7_lanes.py`)
- `store.py:2610-2628` (unsized rows count as small); `fetch.py:2979-2996`.
  50 big (2.5 MB, paced) + 5 small, 6 slots, 3 small lanes: the 5 small
  files were requests 50–54 of 55 — claimed at 23.9 s of a 29.5 s drain
  (sized) and 31.2 s of 40.5 s (chunked, where `bytes_total` stays 0
  forever). `big_lane` is never set (the handoff is unreachable from
  `run_queue`; review 9 removed it on purpose). At real scale a 5 KB
  provider-reference file behind 50 multi-GB shards waits hours, and the
  rate files loaded meanwhile skip their groups.
- Fix: size rows before claiming — record Content-Length from the index
  expansion when the TOC carries it, else one HEAD per unsized row (in
  `next_queued_url` for the small lanes, or batched at enqueue); route
  provider-reference files first by name.

### 2.9 (P2) An `oversize` child is re-queued by every re-expansion of its index (`queue/s11_oversize_reexpand.py`)
- `store.py:2580` (child `retryable` includes "oversize"). Sized: one GET
  per re-expansion, oversize → queued → oversize each month. Chunked (no
  Content-Length): downloads up to the limit again each time —
  `confirm_over_gb` bytes (5 GB) per month per such file.
- Fix: do not revive `oversize` on re-expansion unless `bytes_total` (2.2)
  now fits.

### 2.10 (P2) `dedup_key` collapses two DIFFERENT files to one key when the identity param is a "volatile-if-signed" name and a signature is present — the second is never queued, silently (`queue/s2_dedup.py`)
- `fetch.py:53-80` (`_VOLATILE_ALWAYS`: expires / signature / policy /
  key-pair-id / x-amz-* / sig; `_VOLATILE_IF_SIGNED`: token / se / sp / sv /
  sr / st / … when any signature param exists). `dl?token=FILE1&sig=a` vs
  `dl?token=FILE2&sig=b` → one key, added 1 skipped 1; same for
  `?st=NY&sig=` vs `st=CA`; always for `?policy=A/B`, `?expires=…`,
  `?x-amz-meta-file=1/2`. The code comment claims the `?token=<file-id>`
  listing shape is protected; it holds only unsigned. No `filename_for`
  collision across distinct keys.
- Fix: strip a volatile param only when what remains still differs per
  file; keep `token` / `st` unless the value looks like a SAS / JWT
  (length, charset); pin the listed real shapes in the test.

### 2.11 (P2) The set-aside cap re-claims the slot it just freed (`seams/setaside_order.py`)
- `fetch_url_record`'s set-aside branch requeues with no `retries` bump;
  `_claim_next` orders by `coalesce(retries, 0), id`. One slot, cap 3 s,
  two 1.5 MB drips at 300 KB/s: claims `(0.1 s, a) (4.2 s, a) (6.2 s, b)
  (10.5 s, b)` — b waited for a to finish; the cap only added a reconnect
  (`Range: bytes=917504-`). The message "so other files get a turn" is
  false; with N slots it is the same slot churning.
- Fix: bump `retries` (the fair tier) or set `retry_after = now + 30 s`.

### 2.12 (P2) "Stop downloads" is not honoured during the retry back-off sleep (`download/c08_cancel_sidecar.py cancel_during_backoff*`, `c06_slow_drip.py`)
- `fetch.py:1084-1088` (`time.sleep(pause)`, up to 90 s + jitter). Cancel
  at 0.5 s during a zero-progress retry → slot released at 4.5–4.9 s; at
  production values a stop pressed during the 90 s tier holds the slot
  ~93 s while the row already reads "stopped".
- Fix: sleep in ≤ 1 s slices and poll `cancel_check` between them.

### 2.13 (P2) `Retry-After` is ignored, and the 503/429 give-up message blames "closing the connection early" (`download/c05_http_errors.py r429_retry_after`, `r503_forever`)
- `fetch.py:783` (retryable status → the generic back-off), `:1099-1106`
  (the stall text hard-codes the early-close diagnosis; `last_exc` is not
  shown). 429 + `Retry-After: 2` ×3 → requests at +2, +4, +8 s. 503 on
  every request: 5 requests in 42.8 s, then `failed` with "this server
  keeps closing the connection early and won't resume" — every answer was
  HTTP 503; at production values the row pauses 24 h on the wrong text.
- Fix: honour `Retry-After` (cap 10 min, one attempt); append the last
  answer to the stall message ("last answer: HTTP 503").

### 2.14 (P2) A Range-ignoring server + a `.part` ≥ 64 MB + `download_retries ≤ 2` can never finish (`download/c07_segments.py holey_restart_200`)
- `fetch.py:377` (`_KEEP_200_MAX_REFUSALS = 3`) vs the consecutive
  no-progress budget (`attempt`, `:1070`): each refused 200 counts as a
  no-progress attempt, so with ≤ 3 attempts the "accept the 200 and
  restart" branch (`:838`) is unreachable. retries=1: three
  `(200, 'bytes=220200960-')` → retryable failure, 220 MB `.part` kept —
  and the next `download()` starts with `kept_200_refusals = 0`, so every
  24 h pause repeats the same three refusals until `download_auto_resumes`
  is spent. retries=4: the 4th 200 is accepted, sha correct.
- Fix: a refused 200 is our choice, not the server's failure — don't count
  it against `attempt`; or clamp the refusals to `min(3, download_retries)`.

### 2.15 (P2) Every TOC / page / allowed-amounts / unknown row, and every failed row, leaves its `.fetchmeta` sidecar in `downloads/` forever (queue s0, s1, s3, s8 D, s5: 19 of 19 trials; download c04 / c05 / c08)
- `fetch.py:2495, 2500-2503, 2512, 2565, 2579, 2661` (`dest.unlink` only);
  the start-up sweep (`store.py:2599` `done_download_filenames`) keys on
  `filename`, which these rows never get. The enabler for 1.6.
- Fix: `_meta_path(dest).unlink(missing_ok=True)` beside each of those
  calls; sweep `*.fetchmeta` without a `dest` at start.

### 2.16 (P2) A rate file whose raw copy is gone is never re-read when its provider list lands, and nothing says so (`ingest/s6_companion.py` B')
- `ingest.py:1409-1456` (`requeue_skipped`: processed/ → inbox/ →
  downloads/ → `requeue_url_for_file`, which returns False when the file
  has no queue row; nothing is written to the files row). `move_processed:
  false`, user deleted the raw file: reference `done 3 ids`; rate file
  stays `done / 0 rows / ref_groups_skipped 2 / missing_ref_ids 3 / error
  ''`. (Default config: the copy is in processed/ and IS re-read.)
- Fix: when neither a raw copy nor a queue row exists, write the files
  row's `error` ("its provider list has now arrived — drop this file in
  again to pick up N rate groups").

## Group 3 — work done two to four times (P1/P2)

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
file; the rest is fingerprinting, done twice — 3.5); 4 `url_progress`
UPDATEs per download; 83 fsyncs, WAL ≤ 1.8 MB, no checkpoint stall.
Memory: workers 548–786 MB VmHWM (single-read big file 748 MB); main
3.2–3.6 GB during the full rollup, 4.4 GB when a rollup overlaps ingest.
Invariants 1 and 2 hold; `write_lock` is held ≤ 1 % of wall in serve mode
(the `url_progress` docstring is wrong, 4.7).

### 3.1 (P1) The analytics rollup doubles a fresh grind and reads the store ~35× (throughput finding 1)
- `fetch.py:2820-2870` (`rebuild_now`), `store.py:3907`
  (`update_rollups_incremental` precondition: a prior full build),
  `store.py:4181-4510` (the build). 78 s of parsing, then a 62–85 s serial
  tail (45–52 % of wall) at ≈ 2.3 s per million rows, nothing else
  running; with 1 worker the end-of-grind incremental for all-new payers
  (73.6 s) costs as much as a full one. Overlapping rebuilds mid-grind
  (`ROLLUP_MAX_STALE_SECONDS = 20` experiment) cut wall only 5 % and slowed
  the parsers 28 %.
- Fix: the designed single-pass `COPY … PARTITION_BY` build (AI_HANDOFF
  C1); on an EMPTY store skip the incremental attempt and go straight to
  the full build; measure s/M rows before and after on this fixture.

### 3.2 (P2) Each inbox ingest reads the file 2.5 times, and each link ingest fingerprints the same head three times (`ingest/s9b_opens.py`, `s9_eff.py`; throughput finding 11)
- `ingest.py:353-385` (`_finish_ok`: `file_fingerprint` reads the first 15 MB
  + last 1 MB + 8 windows; `hash_file` then reads the WHOLE file again for
  the identity sha, files ≤ 4 GB) after the parse already streamed every
  byte. Per handle on a 35.5 MB real UHC file: preflight 0.1 MB, parse
  35.5, fingerprint 17.2, `hash_file` 35.5 — **88.6 MB = 2.49× the file, 10
  opens**; pooled 4.1× in the MAIN process alone. Link path: the head is
  fingerprinted after the download (`_record_download_fingerprint`) and
  again after the ingest (≈ 17 MB each); `early_dedup_mb=0` cut the main
  process's reads 6.2 → 5.1 GB on the 27-file run. On a 4 GB local drop the
  identity hash alone is a second 4 GB read; above 4 GB identity is
  size+mtime only.
- Fix: hash the stream inside the parse (`_CountingRaw` already sees every
  compressed byte; feed a `ContentHasher` like the downloader does) and take
  the head digest from the same pass; carry the download's fingerprint to
  `_finish_ok` (`DOWNLOAD_IDENTITY` already carries the sha); `_finish_ok`
  then reads only the 1 MB tail. This also makes 1.1's sha free.

### 3.3 (P2) Parser workers outlive a killed main process forever (ingest `s4b_orphans.py`, `s4_kill.py orphan_life`; throughput finding 7, §4 C)
- `ingest.py:598-625` (`report_chunk`: the parent-liveness check runs only
  on a chunk/percent change — for a file under 64 MB compressed `chunk`
  stays 0 and it never runs after the first report), `ParsePoolManager._make`
  (`:412-418`: no `initializer`); the idle pool loop blocks on
  `call_queue.get()`. Parent killed 1.25 s into a parse: both workers alive
  at ppid 1 at t+240 s for the 118 MB file AND the 2 MB file; all 20 kill
  trials: 2/2 alive 45 s later; the throughput pass's SIGKILL of `mrfx
  serve` left 3 idle workers at ~220 MB each for 10+ minutes (six such
  orphans from the reviews are still running on the review box). On
  Windows: Task Manager "End task" and the supervisor's `proc.terminate()`
  (`cli.py:1822`) leave them too. Clean shutdowns terminated workers 9/9.
- Fix: a pool `initializer` that starts a daemon thread in every worker
  polling `os.getppid()` (or `_pid_alive(parent)` on Windows) every few
  seconds and `os._exit(0)` when the parent is gone (`PR_SET_PDEATHSIG` on
  Linux / a Job Object with KILL_ON_JOB_CLOSE on Windows where available).

### 3.4 (P2) The CLI drain path opens the store without `keep_warm`: 12.6 ms per connection, `write_lock` held 14.9 % of wall (throughput finding 3)
- `cli.py:415` (`mrfx add` with no dashboard), `:543` (`mrfx ingest`).
  `url_progress` 19.3 ms vs 1.2 ms pinned (236 ms each under contention);
  57 s of lock waits over the 27-file drain vs ≤ 1 % in serve mode.
- Fix: `keep_warm=True` on every long-running CLI path.

### 3.5 (P2, Windows) `finalize_rates_part` holds `write_lock` through `replace_with_retry`'s ~90 s loop (throughput finding 4)
- `store.py:2370-2375` + `:245-262`: while an export or antivirus holds the
  old part, every claim and status write blocks for up to 90 s.
- Fix: retry outside the lock, or park the swap after ~2 s
  (`_retry_pending_part_ops` exists for that) — and 1.8's generation
  naming removes the in-place rewrite altogether.

### 3.6 (P2) A segmented download throws away every finished segment when one segment fails; a "start-only" range server doubles the bytes (`download/c07_segments.py seg_fails`, `seg_200`, `start_only`)
- `fetch.py:529` (`_SegmentationFailed` → holey `.part` deleted →
  sequential restart from 0), `:477` (a segment reads its body to EOF, no
  cap at `seg_len`). 220.2 MB, 4 segments: segment 3 answers 500 ×4 →
  **367.0 MB sent** (146.8 MB of good segments discarded), 15.1 s instead
  of 1.2 s; a segment answered 200 → the same. A server honouring only the
  start of `bytes=a-b`: sha correct but 440.4 MB sent for 220.2 MB. A holey
  `.part` left by a kill restarts on ONE connection (the segmented branch
  is evaluated once, before the loop, with `not part.exists()`).
- Fix: stop reading a segment at `seg_len`; on a segment failure fetch the
  MISSING ranges (a small segment map beside the `.part`) instead of
  unlinking; allow segments on a restart.

### 3.7 (P2) Unknown-length (chunked) copies skip the early duplicate check and download in full (`download/c03_chunked.py early_dedup chunked twin`)
- `fetch.py:918` (`next_early and total and …`). A loaded; B = the same
  bytes served chunked → 2,275,193 of 2,275,193 B downloaded, then skipped
  by the full sha. A 1 GB chunked twin costs 1 GB.
- Fix: with no `Content-Length`, learn the size with one `Range: bytes=0-0`
  probe (`_probe_ranges` exists); this also sizes rows for 2.8.

### 3.8 (P2, by construction) The early skip is 32 bits of proof: a constructed CRC-32 collision passes as "identical" (`download/c09_early_dedup.py` A)
- `fetch.py:1960-1972` (`_proves`: size + head + tail + 8 windows + a
  single-member gzip trailer). Two level-0 gzips, 4,106,459 B each, first
  differing byte at 1.60 MB, identical ends, windows and trailer (CRC-32
  forced equal by solving a 32-bit GF(2) system over 40 bytes of a
  non-target string; built in < 1 s): A done (20 rows); B (97140 = 47.0
  instead of 40.0) `skipped/duplicate "identical to …"`; B's rates never
  load. An ACCIDENTAL collision is ~2⁻³² — negligible; a deliberate one
  needs control of a payer's published bytes.
- Fix if wanted: say "checksum-equal" rather than "identical" in the skip
  text, or spend one more range on a sparse 1 % sample.

### 3.9 (P3) A kill between `part.replace(dest)` and `_write_meta` re-downloads a complete file (`seams/dest_no_sidecar.py`)
- Complete `dest`, no sidecar → one full GET, 22,632 of 22,632 bytes. The
  window is microseconds; fix cheaply alongside 1.6: if `dest` exists
  without a sidecar and its size equals the server's Content-Length (HEAD),
  hash it locally.

### 3.10 (info for the handoff) Peak memory scales with the largest `in_network` item, not the file (`ingest/s6g_mem.py`)
- A file with no own table whose groups all cite a 1,000-id companion
  (every group deferred): 5 items of 150k–300k groups → 100 MB raw 577 MB
  fast / 327 classic; 200 MB raw **909 / 516 MB** (baseline ≈ 250). The
  same groups as 7,500–15,000 items of 100 → 296 / 283 and 311 / 284 MB —
  flat. Invariant 1 holds for file size; one code with every provider of a
  national payer costs ≈ 0.6 GB per worker on the fast path, × 3 workers.
  Put the line in AI_HANDOFF Step 5.

## Group 4 — hygiene and prose (P3)

### 4.1 Counter hygiene on every requeue path (`queue/s6_counters.py` B)
- `store.py:3266` (`force_size_requeue`), `:3281` (`requeue_urls`), `:2567`
  (`_enqueue_one` revive), `:3470`: `park_count` kept (4 → only 2 fruitless
  parks before the long pause), `waiting_on` and `retry_after` kept,
  `auto_resumes` never reset (2.6), `kind` never reset (a user-skipped row
  once `duplicate` stays eligible for `revive_skipped_duplicates`),
  `requeue_oversize_within` keeps `force_size = true`,
  `set_url_status_by_id` writes no `finished_at`.
- Fix: one `_reset_for_requeue` SQL fragment used by every requeue path.

### 4.2 The re-download of a `fetched` row whose download vanished runs under `ingesting` (`queue/s8_processor.py` A)
- `fetch.py:2336-2352`: invisible to "stop downloads", shown as
  "processing". Fix: flip to `downloading` around the fallback `download()`.

### 4.3 Serve mode runs one rollup rebuild per idle gap (`queue/s8_processor.py` C/C2)
- `fetch.py:2754` `_rollup_due`: `in_flight == 0` bypasses the 90 s / 4×
  floor; 5 files arriving one at a time with 1.5 s gaps → 4 rebuilds (a
  30-file drain → 1). Bounded by the rebuild's own duration. Optional:
  apply the floor to idle-triggered rebuilds except on drain exit /
  shutdown.

### 4.4 Windows read (throughput `FINDINGS.md` "Windows read"; each with its confidence)
- (a) Raw `dest.unlink` / rename after a row is already terminal:
  `fetch.py:2428-2429, 2437-2438, 2459, 2495, 2502, 2512, 2563, 2579,
  2602-2603, 2639-2640` — a locked file raises into the processor's
  `except BaseException` and overwrites a correct row (1.9). Handled
  correctly already: `ingest.py:632, 404/407, 556-563`, `store.py:2379-2418,
  1916-1940, 4583-4597, 5050`, `api.py:1761/2562`, `fetch.py:2278-2279,
  2679-2682`.
- (b) No `fsync` anywhere: a power loss (not a process kill) can leave a
  truncated `.parquet` part under a `done` row — medium confidence. Fix:
  `os.fsync` the part before the rename that makes it live.
- (c) `MAX_PATH`: `filename_for` keeps 120 chars + the 9-char hash; with
  `.part.val` that is 138 past `downloads_dir`; a real UHC name under a
  ≥ ~120-char project path (`OneDrive\Documents\…` is 91) crosses 260 →
  WinError 206/3 unless `LongPathsEnabled`; HOW_TO_RUN never mentions the
  Python installer's "Disable path length limit". Fix: cap the base at ~80
  and document the option.
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
- (g) Console encoding: only `mrfx serve` with REDIRECTED output and no
  PYTHONUTF8 crashes on the "→" banner (`cli.py:298`).
- (h) Monotonic clocks count through sleep on Windows (low confidence): a
  lid close > 240 s trips a benign park on wake, > 600 s the stall error
  for that attempt.
- Correct as read: no fcntl / /dev/shm / fork; `_pid_alive` Windows branch
  and `getppid` handling; spawn with a guarded `__main__`, cfg/strings only
  pickled, `terminate()` on heal; `dedup_key` lower-cases the host only;
  `filename_for`'s hash prefix avoids NTFS case collisions;
  `SO_EXCLUSIVEADDRUSE`; the updater's rollback; the `.bat` UTF-8 env.

### 4.5 Download-layer small items (download `FINDINGS.md` P3, each with its script)
- Raw exception text is the user-facing error for a damaged download:
  `'CRC check failed 0x36bafc05 != 0xc94503fa'`, `'Compressed file ended
  before the end-of-stream marker was reached'`, `'unreadable: BadZipFile:
  File is not a zip file'` (c04); the download was deleted so "press retry"
  is the right action — say so. A 0-byte 200 reads `'no in_network /
  provider_references / reporting_structure key found'` (c05) — "the
  server returned an empty file" is the truth.
- The size-limit message on an unknown-length body reports bytes-so-far as
  the size: "file is 5.0 GB — larger than … (5.0 GB)" (c03).
- Stall deadline overshoot: budget 12 s fired at 17.3–19.8 s (c06) because
  the loop-top check runs only after the read timeout + back-off; at
  production values a silent server can hold a slot 600 + 120 + 90 s. Park
  (6 → 7.0 s) and the max cap (20 → 21.0 s) are tight.
- Steady-but-slow is reported as "stopped sending data (under 1 MB in 6 s;
  1 MB kept)" with the count rounded up (720,896 B on disk) (c06).
- Up to one 64 KB chunk is re-downloaded per reconnect (`iter_raw` drops
  the partial chunk when h11 raises) — trivial. Dashboard progress lags
  the `.part` by up to 8 MB / 10 s (first-callback rule, `fetch.py:987`):
  "64 KB" for the first 10 s of every file on a 1 MB/s link.
- The early check hashes its own 15 MiB head before asking whether a
  same-size candidate exists (`fetch.py:2017`): ~30 ms and 15.7 MB of
  reads per download for nothing (c11). `DOWNLOAD_IDENTITY` entries leak
  for rows whose ingest fails (`fetch.py:1844`).

### 4.6 Ingest-layer small items (`ingest/s10_honesty.py`, `s1_containers.py`, `s8_oversize.py`, `s2_samename.py` E)
- A small-path file with none of the target codes is `done 0` with
  `qa.messages = NULL`; only the big-file short-circuit writes "scanned:
  none of the target billing codes appear in this file". The Files tab
  shows a bare 0.
- A UTF-16 file (Windows "Unicode" save) and a 0-byte file are both
  quarantined as "no in_network / provider_references / reporting_structure
  key found" — say "saved as UTF-16; save as UTF-8" / "the file is empty".
- Non-zero bytes after the gzip member → "unreadable: BadGzipFile: Not a
  gzipped file (b'GA')"; zero padding is consumed one byte at a time by
  CPython (≈ 0.12 s/MB — a sparse copy with gigabytes of zeros costs ~12
  min per 6 GB in preflight and again in the parse). A sentence would help.
- A 1-byte-truncated gz on a real file is met at the END of the full parse;
  the rows read so far are discarded (honest) and the sentence is
  `EOFError: Compressed file ended before the end-of-stream marker was
  reached`.
- After `/confirm` the files row's `error` is `''` rather than NULL. A
  second month under an already-loaded name is tagged with a content hash
  (`beta_2026-05-f9ce5ee2.json.gz`) rather than its month; `unique_inbox_name(".json.gz")`
  → `.json-t.gz` (a dot-file; harmless).
- **Config trap (P2):** `config/mrfx.yaml:232-233` says "`mode: off` =
  don't identify names", but an unquoted `off` is YAML's boolean False, so
  the loader refuses it ("enrichment.mode: Input should be 'api', 'bulk'
  or 'off'") — the shipped comment recommends a spelling that breaks every
  command. Quote it in the comment and accept False as "off" in the
  validator.

### 4.7 Stale prose
- `Store.url_progress` docstring: "a multi-minute rollup rebuild holds
  write_lock the whole time" — it does not; rollups serialize on
  `rollup_lock` and take `write_lock` only around view DDL
  (`store.py:3949, 4112, 4295, 4371`); measured 0.07–1.85 s held per
  rebuild in serve mode (throughput §5).
- GETTING_STARTED "Making a big queue finish faster" item 1 ("cores − 1,
  stops at 8"): `_ram_worker_cap` gives 1 worker on 8 GB and 3 on 16 GB;
  say so. `fetch.py:870` says "1 MB chunk"; it is 64 KB (`:580`). No
  Defender-exclusion advice for `data/` (medium confidence that it
  matters). The 403 message's "drop the file into data/inbox/" is only safe
  after 1.1.
- AI_HANDOFF Step 4: replace with the graph (2.0); Step 5: add 3.10.

## Measured reference (download `FINDINGS.md`)

Slot time per scenario (`c06_slow_drip.py`; timeout 3 s, park 6 s, stall
12 s, max 20 s, retries 2): silence then a working resume 4.1 s (sha ok,
one 206); steady 256 KB/s 11.8 s, no clock fired; steady 100 KB/s 6.4 s →
parked (720 KB kept); silent after 200 KB on a Range-ignoring server 7.0 s
→ parked (17.3–19.8 s with park off → stall); 256 KB/s on 8 MB with max
20 s → 21.0 s set aside, 5.3 MB kept; a server cutting every 512 KB → OK
in 5.1 s over 6 connections; headers then nothing 6.1 s → parked; cancel at
0.5 s during back-off 4.5 s (2.12); accepted with no headers 8.3 s → parked.

Per download on a real 118.7 MB gz from localhost (`c11_efficiency.py`):
1 request, 1.00× bytes, the full-file hash exactly once (streamed), 32.5 MB
of `_sha_range` reads, 2 progress UPDATEs of ~25 ms; the ingest re-reads
16.8 MB for `_finish_ok`'s fingerprint and hashes nothing (identity
reused). Early check on a true copy: 9 range requests (1 MiB + 8 × 64
KiB), 0.56 s, then the stream is abandoned.

Kill tests: download (`c10_kill.py`, 20 trials, two seeds): 12 kills
mid-download → resume from the exact `.part` size with `If-Range`, 0.13 MB
re-downloaded in total; 8 kills mid-ingest → kept download + sidecar
reused with 0 requests; every trial `content_sha == truth`, each file
once, `rates` exactly 600. Queue (`s5_crash.py`, seed 4242, 20 kills over
an 18.7 s drain of 1 index + 6 children + 2 direct files + a twin pair):
19 valid trials, no row left in flight, every file its expected rows,
parts == done rows, kept downloads reused, restart 1.2–7.9 s. Ingest
(`s4_kill.py`, seeds 40465 / 41123, BCBS-MS 25 MB / 2,660,356 rows and
UHC-NY 35 MB / 20,741 rows through a 2-worker pool, 15 random instants + 5
window kills around `finalize_rates_part` / `_commit_done` / `_finish_ok`):
20/20 — one `done` row per file with the clean counts, no `.parquet.tmp` /
spool / progress leftovers after the sweep, within-file repeated lines
equal to the clean store's 416,826, `rollups_stale()` False, store readable.

## Checked and found correct — do not re-chase

- **Download** (`download/FINDINGS.md`): truncated body with a correct
  Content-Length resumes from the `.part` size and matches truth (an absent
  `Accept-Ranges` changes nothing); Range ignored on a small `.part` →
  clean restart, on a ≥ 64 MB `.part` → 3 refusals then restart (143 MB
  sent for 70 MB, bounded as designed); 416 → `.part` discarded, new
  content fetched; an RFC-correct If-Range server → restart, sha == v2;
  chunked bodies resume, trip `confirm_over_gb` on bytes-so-far, hand off
  to a big slot, park then fail cleanly when silent; a chunked body ended
  cleanly early is accepted (no client can know; the trailer / parse
  catches it); corrupt gzip / truncated zip → 0 rows, `failed`, no
  fingerprint or identity recorded, download removed so a retry refetches;
  HTML 200 and redirect-to-login → `failed/page` with PAGE_HELP; a redirect
  that changes the extension is sniffed by magic and ingests; 403 on a
  resume → terminal signed-URL text, `.part` kept; segments are contiguous
  and exact (1 probe + 4 GETs, 220.2 MB for 220.2 MB); a Range-compressing
  or 200-answering server falls back to one connection and finishes;
  `_reserve_disk_or_raise` is atomic (200 trials × 2 simultaneous 2 GB
  reservations on a faked 5 GB disk → exactly one accepted); cancel before
  the first byte / mid-stream / `cancel_all_downloading` →
  `skipped/canceled`, `.part` kept, `resume_canceled` resumes with a 206;
  any byte of progress resets the retry count; a same-size candidate whose
  head differs costs no range request; `url_progress` never blocks the
  socket.
- **Queue** (`queue/FINDINGS.md` §3): crash-resume over 20 random kills;
  twin kills at the claim instant (A done, B skipped/duplicate, rows once);
  twins in both lanes, in sequence, a third copy, owner stopped mid-ingest
  (the deferred twin loads), forgotten owner whose retry 403s (kept
  honest, by design), user-skipped twin retried (skipped again); the
  early-dedup "waiting" copy vs a CLI drain exit (6 trials, never left
  paused); self-referencing and mutual indexes, relative locations after a
  redirect, a TOC → HTML page, a file under two plans (queued once, both
  plan rows), a child also pasted directly; the monthly refresh never
  double-queues, dated indexes retried once a day through day 10, a dead
  payer costs 2 GETs/day for 10 days then stops, a static index re-fetched
  once a month; a parked big row woken by an idle small-only slot is
  claimable; user actions during the disk-full sleep and during a park
  stick; a dead downloader thread hands its row back; vanished
  dest/sidecar re-downloads; `delete_raw_after_ingest: false` + forget
  removes the raw copy; `dedup_key` misses (http/https, trailing slash,
  path case, %-case, default port, dot-segments) cost a wasted download,
  never a loss; nothing moves `done` except the monthly requeue.
- **Ingest** (`ingest/FINDINGS.md`): 26 container shapes over 3 scans —
  every valid shape `done` with the right rows and moved; every broken one
  quarantined (a plain truncated JSON `failed` with "file ended
  mid-structure"), moved to failed/, never retried; a growing file skipped
  by scan 1, loaded by scan 2; an identical re-drop under its own name
  recognised by sha and moved out; a different file for the same
  payer+month held (`needs_choice`) and not re-held; Replace then a
  restart loads from `.replacing/`; another month under the same name
  loads beside; `/confirm` ordering never lost its race in 30 tries; a
  crash leaving url `ingesting` + files `processing` + an in-flight
  fingerprint row recovers to exactly one load and 0 in-flight rows
  (review 9 3.5 fixed); 12 alternating re-ingests leave one part with the
  last version's rows; forget removes part, row, fingerprint and raw
  copies, refuses during a parse, and a re-drop loads again; a re-ingest
  to 0 rows drops the old part; companions reference-first / later (inbox
  path re-reads from processed/) / never / empty / remote-only all counted
  right; a different reference for the same payer+month is held; dead
  worker → heal → retry → next file loads, no leftovers; hung worker →
  killed by the both-frozen watchdog with a plain sentence; oversize →
  `pending_confirmation` kept and re-parked across restarts, `/confirm` →
  `done`, DELETE → gone; a gzip trailer claiming 4 GiB on a 1 KB body is
  caught by gzip's own length check; a 0-row file stays 0 and is never
  retried; a file with no `reporting_entity_name` loads as "Unknown
  payer"; fast vs classic identical on a declined shape and on 3 real
  files (`fastcheck` 3/3 IDENTICAL, fast 2.3–4.7× faster); no regression of
  round 9 Group 3.
- **Throughput**: streaming (worker ≤ 0.8 GB on ~5 GB inflated; file read
  and inflated once); single DB writer (workers open no DuckDB file;
  finalize = one rename, 0.03 s); crash-resume after SIGKILL of `mrfx
  serve` (3 files + 12 URLs recovered, orphans swept, 15 parts for 15
  files, catch-up in background); clean shutdown 0.3–0.6 s with workers
  terminated 9/9; WAL ≤ 1.8 MB, no checkpoint stall; `url_progress`
  throttle (54 UPDATEs / 27 files); prefetch cap (fetched ≤ 4) and the
  delete_raw sweep; the incremental rollup's precondition is "prior full
  build", not "payer seen" (7 new payers → incremental, 41 s);
  `_ram_worker_cap` arithmetic; the updater's rollback.
- **Seams**: `Content-Encoding: deflate` bodies decode correctly (`done >
  total` passes; `content_encoding.py`); `parallel_downloads: 1` is
  honoured; `_reserve_disk_or_raise` and the mid-stream disk message both
  match `fetch_url_record`'s disk-full branch; `cmd_add` hands links to a
  running dashboard and 20 CLI commands refuse to open a live server's
  store.

## Deliverables

1. One commit per group, message = what + why + verified-with-numbers,
   naming each test in `tests/mrfx/test_review11.py` and the value it
   failed with on 1064203. Every change to what gets loaded carries the
   before/after row-identity check on real files (How to work).
2. Re-run the kill harnesses after the fixes (`queue/s5_crash.py` 20
   trials, `queue/s4k_twin_kill.py`, `download/c10_kill.py` 20 trials,
   `ingest/s4_kill.py` 20 trials) and put the counts in the status block;
   re-run every repro script named on a closed item and say what it prints
   now.
3. Re-measure the throughput timeline on the same fixture queue
   (`throughput/queue_timeline.py default_warm`) and the per-file bytes
   read (`ingest/s9b_opens.py`) before and after; put them in the status
   block.
4. Version 0.7.5; GETTING_STARTED "What changed" in the user's words (lead
   with what a user would have seen: a file dropped in twice no longer
   counts twice, a capped index now loads the rest, an oversize file
   re-queues when the limit is raised, "Clear queue" sticks, "Keep both"
   can no longer get stuck, plain sentences instead of exceptions);
   AI_HANDOFF Step 4 replaced by the graph, Step 5 with 3.10, and a
   "Round 11 additions" list with the rule each fix must keep (one content
   claim for every path; a 206 is checked against what was asked; a kept
   download is validated before reuse; no live part is rewritten in place;
   every requeue resets through one fragment; a worker dies with its
   parent).
5. Full suite green, ruff 0, `tests/slow_tests.txt` regenerated, CLAUDE.md
   counts; `git archive --format=zip --prefix=Claudevideo/ -o <scratch>/mrfx_project.zip HEAD`
   sent to the user with the update steps (close the app → drag the zip
   onto "Update MRF Explorer.bat" → start → Ctrl+Shift+R at
   http://localhost:8377).
6. The status block at the top of this file.
