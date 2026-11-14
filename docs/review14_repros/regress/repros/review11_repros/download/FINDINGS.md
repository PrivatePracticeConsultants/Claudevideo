# Round 11 — adversarial review of the HTTP download layer (`mrfx/fetch.py`)

Baseline: commit `1064203` (v0.7.4). Read-only review; nothing under `mrfx/` or
`tests/` was changed. Every claim below was produced by a script in this folder
run against a scriptable hostile HTTP server on 127.0.0.1 (`_harness.py`:
threading `HTTPServer` on port 0, per-path rules for Range/chunked/cut/ETag/
delay, a request log with status, Range header and bytes actually sent) and a
scratch `MrfxConfig` under `/tmp/claude-0/-home-user-Claudevideo/4633d594-0139-5f8e-a935-ae9c76b72000/scratchpad/audit14/regress/r11tmp/download/runs/<case>` (never the
repo's `data/`). Run any case standalone:

    /home/user/Claudevideo/.venv/bin/python docs/review11_repros/download/c01_truncate_resume.py

Scripts: `c01_truncate_resume.py`, `c02_range_odd.py`, `c02b_json_splice.py`,
`c03_chunked.py`, `c04_corrupt_gzip.py`, `c05_http_errors.py`,
`c05b_bad_encoding.py`, `c06_slow_drip.py`, `c07_segments.py [variant…]`,
`c08_cancel_sidecar.py`, `c08b_wrong_sidecar_json.py`, `c09_early_dedup.py`,
`c10_kill.py [trials seed lo hi]` + `c10_driver.py`, `c11_efficiency.py`.
"truth" = sha256 of the bytes the server was configured to serve.

Severity key: P0 wrong bytes marked done / stuck forever · P1 wrong state or a
slot wasted on a real path · P2 measured inefficiency / narrow-path wrong state ·
P3 cosmetic.

---

## P0

### P0-1 A resume splices CHANGED content into the kept `.part` and the row ends `done` with mixed rates
- **Where:** `mrfx/fetch.py:812-815` (a 206 is accepted after parsing only the
  *total* out of `Content-Range`), `:903` (`.val` written only when the first
  response carried a strong ETag / Last-Modified; nothing else about the first
  response is remembered), `:999` (the only completeness check is
  `done < total`). The 206's own `ETag`/`Last-Modified` is never compared with
  the stored validator.
- **Scripts:** `c02b_json_splice.py` (plain `.json`, queue path),
  `c02_range_odd.py` (gz variants).
- **Observed (c02b):** server serves v1 (97110=40.0, 97140=40.0), closes at
  300 KB; the resume `bytes=262144-` is answered from v2 (97110=55.0,
  97140=55.0, same size, no validator). Row: `status=done kind=in_network
  rows=20`, `content_sha` equals **neither** v1 nor v2; store holds
  `97110 → 40.0 ×10` and `97140 → 55.0 ×10`. One file, two vintages, no
  warning. Requests: `[(200,'-',307200), (206,'bytes=262144-',665367)]`.
- **Observed (c02, strong ETag):** first response `ETag:"v1"`; the resume
  sends `If-Range: "v1"` (verified in the request log) and the server answers
  **206 with `ETag: "v2"`** (ignores If-Range, as plenty of CDNs do). The
  client appends anyway: `download()` returns OK with a sha matching neither
  version. Same for "no validator, different size" (1,580,067 B file whose
  first 393 KB are v1). For `.json.gz` the gzip CRC catches it later: row
  `failed`, error `'CRC check failed 0xa27095a4 != 0x59de80d0'`, download
  deleted, a retry then succeeds — so for gz this is a P1 state + a wasted
  download; for plain `.json` it is wrong numbers marked done.
- **Expected:** a 206 whose validator differs from the one we asked with, or
  whose total differs from the first response's `Content-Length`, must
  discard the `.part` and restart (the RFC-correct-server path, `ifrange_rfc`
  in c02, already does the right thing: 200 → restart → sha == v2).
- **Fix:** persist `{validator, total}` from the first response in `.val`; on
  a 206 require `Content-Range` *start* == `resume_from`, *total* ==
  remembered total, and (when the 206 carries an ETag/Last-Modified) that it
  equals the stored validator — else unlink the `.part` and go round again.
  Honest residual after the fix: a server with **no** validator that
  republishes a **same-size** file is undetectable by any client; say so in
  the methodology rather than pretend otherwise.

## P1

### P1-1 A 206 whose body starts at byte 0 (plausible `Content-Range`) is accepted — file 29 % bigger than truth
- **Where:** `fetch.py:812-815` (start of `Content-Range` ignored), `:999`
  (`done > total` is not an error).
- **Script:** `c01_truncate_resume.py` (`cr_start_wrong`).
- **Observed:** truth 911,726 B; cut at 300 KB; the resume is answered
  `206 Content-Range: bytes 262144-911725/911726` but the body is the whole
  file. `download()` returns OK; `dest` is **1,173,870 B** (prefix duplicated),
  sha ≠ truth. Through the queue a gz fails at ingest with a raw CRC message;
  a plain `.json` fails to parse. Either way a full download was wasted and the
  user sees a riddle.
- **Fix:** parse the `Content-Range` start and refuse a 206 whose start ≠
  `resume_from`; treat `done != total` (not just `<`) as "connection delivered
  the wrong bytes" and restart from 0.

### P1-2 A 206 with no `Content-Range` and a short (consistent) body is accepted as complete
- **Where:** `fetch.py:812-813` (`total = None` when the header is missing),
  `:999` (no check when `total is None`).
- **Script:** `c01_truncate_resume.py` (`cr_absent_short`).
- **Observed:** truth 911,726 B; after the cut the resume is answered
  `206`, no `Content-Range`, `Content-Length: 102400`. `download()` returns OK
  with a **364,544 B** file (sha ≠ truth). The first response's
  `Content-Length: 911726` was known and dropped.
- **Fix:** same sidecar as P0-1 — remember the total from the first response
  and require `done == total` on every path (`total` must never become
  `None` once known).

### P1-3 A stale `.fetchmeta` sidecar is trusted blindly; a wrong sha gets recorded and a true copy is ingested twice
- **Where:** `fetch.py:2150-2165` (reuse fast path: `dest.exists() and
  keep_p.exists()` → row `fetched` with the sidecar's sha, no size/hash
  check); the kill window between `part.replace(dest)` (`:1004`) and
  `_write_meta` (`:2281`); and the **orphaned sidecars** that make a stale
  one likely: every failed ingest leaves `<file>.fetchmeta` behind
  (`:2661` path never unlinks it; `ingest._finish_file` moved the file to
  `failed_dir`) — seen in `c04` (all 4 corrupt variants), `c05` (html_200,
  zero_byte, redirect_to_html), `c08` (short_dest).
- **Scripts:** `c08_cancel_sidecar.py` (`wrong_sidecar`),
  `c08b_wrong_sidecar_json.py`, `c04_corrupt_gzip.py` (the orphans).
- **Observed (c08b, plain json):** dest = correct bytes, sidecar
  `{"sha": "000…0"}` → A: `done`, 0 GETs, `content_sha = 000…`,
  `content_fingerprints.full_sha = 000…`. B (byte-identical bytes under
  another URL): `done`, 300 more rows → **600 rows for one file's content**.
  (c08 shows the gz case is rescued only because the *early* fingerprint
  check — not the sha — catches the twin.)
- **Fix:** unlink the sidecar wherever `dest` is unlinked/moved on failure
  (`:2380`, `:2428`, `:2437`, `:2616`, `:2661`, ingest's `_finish_file(ok=False)`);
  write `size` into the sidecar and refuse a sidecar whose size ≠
  `dest.stat().st_size` or whose mtime predates `dest`'s.

### P1-4 Non-transport `httpx` errors escape `download()` as "unexpected download error" with a traceback
- **Where:** `fetch.py:1027` catches only `httpx.TransportError` /
  `httpx.TimeoutException`. `httpx.TooManyRedirects` and `httpx.DecodingError`
  are `RequestError`s (checked: MRO in httpx 0.28.1), not `TransportError`s.
- **Scripts:** `c05_http_errors.py` (`redirect_loop`), `c05b_bad_encoding.py`.
- **Observed:** 302 → itself: 21 requests, then the downloader thread's
  `except BaseException` logs a full traceback and the row reads
  `failed / 'unexpected download error'`. `Content-Encoding: deflate` with a
  garbage body: same. The reservation is released (the `finally` holds), so
  nothing is stuck — but the message is the one reserved for bugs and the
  user cannot tell a looping redirect from a crash.
- **Fix:** add `httpx.HTTPError` to the except at `:1027` and make
  `TooManyRedirects` / `DecodingError` terminal with plain text ("this link
  redirects in a loop" / "the server sent an unreadable compressed body").

## P2

### P2-1 "Stop downloads" is not honoured during the retry back-off sleep
- **Where:** `fetch.py:1084-1088` (`time.sleep(pause)`, pause up to
  90 s + 3 s jitter; `park_budget` only clamps it to the park window).
- **Scripts:** `c08_cancel_sidecar.py` (`cancel_during_backoff*`),
  `c06_slow_drip.py` (`cancel_during_backoff`).
- **Observed:** cancel raised at t=0.5 s during a zero-progress retry; the
  slot was released at **4.5–4.9 s** (retries=2–4). With production values a
  stop pressed during the 90 s tier holds the slot ~93 s (the row already
  reads `skipped`, so the user sees "stopped" while the thread is asleep).
- **Fix:** sleep in ≤1 s slices and poll `cancel_check` between them.

### P2-2 Segmented download throws away every finished segment when one segment fails; a "start-only" range server doubles the bytes
- **Where:** `fetch.py:529` (`_SegmentationFailed` → holey `.part` deleted →
  sequential restart from 0), `:477` (a segment reads its body to EOF, no
  cap at `seg_len`).
- **Script:** `c07_segments.py` (`seg_fails`, `seg_200`, `start_only`).
- **Observed:** 220.2 MB file, 4 segments: segment 3 answers 500 ×4 →
  **367.0 MB sent** (2 good segments = 146.8 MB discarded), 15.1 s instead of
  1.2 s; segment 2 answers 200 to its Range → the same 367.0 MB. A server
  that honours only the start of `bytes=a-b`: sha correct (bytes land at
  their true offsets) but **440.4 MB sent for 220.2 MB**.
- **Fix:** stop reading a segment at `seg_len` (close the response); on a
  segment failure fall back to a sequential fetch of the *missing* ranges
  (keep a small segment map next to the `.part`) instead of unlinking.

### P2-3 `Retry-After` is ignored; the 503/429 give-up message blames "closing the connection early"
- **Where:** `fetch.py:783` (retryable status → generic back-off),
  `:1099-1106` (stall message hard-codes the early-close diagnosis;
  `last_exc` is not shown).
- **Script:** `c05_http_errors.py` (`r429_retry_after`, `r503_forever`).
- **Observed:** 429 + `Retry-After: 2` ×3 → requests at +2, +4, +8 s (jitter
  included: 16.2 s total; a `Retry-After: 60` would be hit 4 times inside
  it). 503 on every request: 5 requests in 42.8 s, then (stall) `failed`
  with `"gave up after 30s with no download progress (no data received over
  5 tries) — this server keeps closing the connection early and won't
  resume…"` — every answer was HTTP 503. At production values this row
  pauses 24 h on the same wrong text.
- **Fix:** honour `Retry-After` (cap e.g. 10 min, counts as one attempt);
  append `last_exc` to the stall message ("last answer: HTTP 503").

### P2-4 Range-ignoring server + `.part` ≥ 64 MB + `download_retries ≤ 2` can never finish
- **Where:** `fetch.py:377` (`_KEEP_200_MAX_REFUSALS = 3`) vs the consecutive
  no-progress budget (`attempt`, `:1070`): each refused 200 counts as a
  no-progress attempt, so with ≤ 3 attempts the "accept the 200 and restart"
  branch (`:838`) is unreachable.
- **Script:** `c07_segments.py holey_restart_200` (first run used
  `download_retries=1`, the second `=4`).
- **Observed:** retries=1: 3 × `(200, 'bytes=220200960-')` →
  `DownloadError retryable: download failed after 3 attempts: server ignored
  resume; kept partial download`, 220 MB `.part` kept — and the next
  `download()` starts with `kept_200_refusals = 0`, so each 24 h pause
  repeats the same three refusals until `download_auto_resumes` is spent.
  retries=4: the 4th 200 is accepted, file completes, sha correct.
- **Fix:** don't count a refused 200 against `attempt` (it is our choice,
  not the server's failure), or clamp `_KEEP_200_MAX_REFUSALS` to
  `min(3, download_retries)`.

### P2-5 Unknown-length (chunked) copies skip the early duplicate check and download in full
- **Where:** `fetch.py:918` (`next_early and total and …`): no `Content-Length`
  → no check; `_handoff` also sends every unsized file to a big slot.
- **Script:** `c03_chunked.py` (`early_dedup chunked twin`).
- **Observed:** A loaded; B = same bytes served chunked: **2,275,193 of
  2,275,193 B** downloaded, then skipped by the full sha
  (`'identical to a file already ingested…'`). A 1 GB chunked twin costs 1 GB.
- **Fix:** when `Content-Length` is absent, learn the size with one
  `Range: bytes=0-0` probe (the segment path already has `_probe_ranges`).

### P2-6 The early skip can be defeated by a constructed CRC-32 collision (F3's "trailer proves the whole content" is 32 bits of proof)
- **Where:** `fetch.py:1960-1972` (`_proves`: size + head + tail + 8 windows +
  single-member gzip), `identity.py` docstring ("its trailer carries the
  CRC-32 and length of everything").
- **Script:** `c09_early_dedup.py` (part A).
- **Observed:** two single-member level-0 gzips, 4,106,459 B each, first
  differing byte at 1.60 MB, identical first 1 MiB, last 1 MiB, all 8
  windows and the trailer (CRC-32 forced equal by solving a 32-bit GF(2)
  system over 40 `@`/`A` bytes in a non-target string; ISIZE equal by
  construction). A: `done`, 20 rows. B (97140 = **47.0** instead of 40.0):
  `skipped/duplicate "identical to … same size, same first 1 MB, same last
  1 MB and 8 samples between…"`; the store keeps `97140 → 40.0`. B's rates
  are never loaded. `file_fingerprint(A) == file_fingerprint(B)` in every
  field.
- **Honest risk:** an *accidental* same-size republish with the same
  ends/windows AND the same CRC-32 is ~2⁻³² — negligible; a *deliberate*
  collision is cheap (this script makes one in <1 s) but requires an
  attacker who controls a payer's published bytes. Severity P2 by
  construction, not a field bug. Fix if wanted: make the skip text say
  "checksum-equal" rather than "identical", or spend one more range on a
  sparse 1 % sample so a differing middle needs both a CRC and a sample
  collision.

## P3

- **Orphaned `.fetchmeta` after every failed row** (`c04`, `c05`, `c08`):
  149-byte litter per failure in `data/downloads/`, and the enabler for P1-3.
  `fetch.py:2661` and the other failure paths unlink `dest` but not
  `_meta_path(dest)`.
- **`DOWNLOAD_IDENTITY` entries leak** for rows whose ingest fails
  (`fetch.py:1844`; only a successful `_finish_ok` pops them) — c04 shows the
  key present after each failed variant. Bytes, not a bug: ~200 B per failed
  row, overwritten on retry.
- **Raw exception text is the user-facing error** for a damaged download:
  `'CRC check failed 0x36bafc05 != 0xc94503fa'`, `'Compressed file ended
  before the end-of-stream marker was reached'`, `'unreadable: BadZipFile:
  File is not a zip file'` (`c04`). The download was deleted, so "press
  retry" is the right action — the message does not say so.
- **0-byte 200** → `failed/unknown 'no in_network / provider_references /
  reporting_structure key found'` (`c05 zero_byte`); "the server returned an
  empty file" would be truthful.
- **Size-limit message on an unknown-length body** reports bytes-so-far as
  the size: `'file is 0.0 GB — larger than the confirm_over_gb safety limit
  (0.0 GB)'` (`c03 size_limit`; at production it reads "file is 5.0 GB —
  larger than … (5.0 GB)").
- **Stall deadline overshoot:** budget 12 s fired at 17.3–19.8 s (`c06
  silent_forever_stall`) because the loop-top check runs only after the read
  timeout + back-off sleep; at production values a silent server can hold
  the slot up to `600 + 120 + 90` s. Park (6 s → 7.0 s) and max cap
  (20 s → 21.0 s) are tight.
- **Steady-but-slow is reported as "stopped sending data"** with a rounded
  count: 100 KB/s for 6.4 s → `'under 1 MB in 6 s; 1 MB kept'` while the
  `.part` holds 720,896 B (`c06 steady_100KBs_parks`). By design below
  1 MB/park-window; the wording and the rounding are not.
- **Up to one 64 KB chunk is re-downloaded on every reconnect** (`c01`:
  server sent 307,200 B, `.part` had 262,144 B; every `c10` resume:
  +65,536 B per file) — `iter_raw(chunk_size=64 KB)` drops the partial chunk
  when h11 raises. Trivial per reconnect; `c01 ignore_big` (Range-ignoring
  server, 66 MB `.part`) cost 143.2 MB sent for a 70 MB file, as designed.
- **Dashboard progress lags the `.part`**: `bytes_done = 65536` while the
  `.part` holds 1.5–5.1 MB (`c10`, every trial) — the first callback waits
  for 8 MB or 10 s (`fetch.py:987-989`). Cosmetic, but on a 1 MB/s link the
  row reads "64 KB" for the first 10 s of every file.
- **Early check hashes its own head before asking whether a candidate
  exists** (`fetch.py:2017` before `fingerprint_candidates`): `c11` shows
  15.7 MB of `_sha_range` per download with zero same-size candidates
  (~30 ms/file).
- **A holey `.part` left by a kill mid-segmented download restarts correctly
  (416 → from 0, 220.2 MB, sha ok: `c07 holey_restart_416`) but on ONE
  connection**: the segmented branch is evaluated once, before the loop,
  with `not part.exists()` (`fetch.py:~700`), so the file that most needed
  segments loses them after a crash.

---

## Measured: slot time per scenario (`c06_slow_drip.py`; timeout 3 s, park 6 s, stall 12 s, max 20 s, retries 2)

| Scenario | Slot held | Outcome | Requests |
|---|---|---|---|
| silence > timeout, then a working resume | 4.1 s | OK, sha ok | 200 cut, 206 `bytes=262144-` |
| steady 256 KB/s, 3 MB (≥ 1 MB per park window) | 11.8 s | OK, no clock fired | 1 |
| steady 100 KB/s (< 1 MB per window) | 6.4 s | parked, 720 KB kept | 1 |
| silent after 200 KB, server ignores Range | 7.0 s | parked | 2 |
| same, park off | 17.3–19.8 s | stall ("gave up after 12s") | 3 |
| 256 KB/s on 8 MB, max 20 s | 21.0 s | set aside, 5.3 MB `.part` kept | 1 |
| server cuts every 512 KB (progress each time) | 5.1 s | OK via 6 connections (retries reset) | 6 |
| headers then 0 bytes, every time | 6.1 s | parked | 2 |
| cancel at 0.5 s during back-off | 4.5 s | canceled (P2-1) | 1 |
| connection accepted, no headers | 8.3 s | parked | 2 |

## Measured: efficiency on real payer files from localhost (`c11_efficiency.py`)

| Stage | Requests | Bytes sent / size | Full-file hash passes | Extra reads | Progress callbacks |
|---|---|---|---|---|---|
| download, 118.7 MB `wy_…710_26B0` | 1 | 1.00× | 1 (ContentHasher, streamed) | `_sha_range` 32.5 MB (15 MiB early-check head + 15 MiB head + 1 MiB tail for the in-flight fingerprint), `gz_members` 1 | 2 × 25.5 ms (DuckDB UPDATE) |
| download, 24.4 MB at ~1 MB/s | 1 | 1.00× | 1 | 32.5 MB | 4 × 21 ms over 22.5 s |
| ingest, 24.4 MB (`process_url_record`) | 0 | — | 0 (identity reused from `DOWNLOAD_IDENTITY`) | `_sha_range` 16.8 MB (`_finish_ok` fingerprint) | 0 |

So a URL file is hashed in full exactly once; the fingerprint head/tail is
read 2–3 times (≈ 48 MB of reads per file at `early_dedup_mb=15`), and each
progress write costs ~20–25 ms under the write lock at ≤ 1 per 1.5 s per
download (`fetch.py:987`). Early-check cost (`c09` part B): a same-size
candidate whose head differs → 0 extra requests, 63 ms; a true copy → 9 range
requests (1 MiB tail + 8 × 64 KiB) = 1.5 MiB and 0.56 s, after which the
stream is abandoned (on a throttled 4.1 MB file the server had already
pushed 2.8 MB into the socket before the stop).

## Measured: kill tests (`c10_kill.py`; seed 39616 × 12 trials over 0.3–9 s, seed 4242 × 8 trials over 6.0–8.5 s)

Driver: `run_queue(drain=True)`, 2 downloader threads, in-process ingest, two
5.3 MB gz rate files at ~0.9 MB/s, `proc.kill()` at a uniform random instant,
restart, drain. **20/20 OK.** Kills during download (12): both rows
`downloading` with `.part` + `.val` on disk; after restart `recover_stuck_urls`
re-queued them and the resume was issued from the exact `.part` size
(`bytes=1507328-`, `bytes=4653056-`, …, with `If-Range`), 0.13 MB re-downloaded
in total (one 64 KB chunk per file). Kills during ingest (6: rows
`ingesting`/`fetched`/`done`): the kept download + `.fetchmeta` were reused
with **0 requests** after restart. In every trial `content_sha == truth` for
both files, `files` has each name once, `rates` = 600 exactly, no row left
`downloading`/`ingesting`.

---

## Checked and found correct (do not re-chase)

- Truncated body with a correct `Content-Length` → h11 raises, `.part` kept,
  resume from the `.part` size, final sha == truth (`c01 honoured`,
  `no_accept_ranges`: an absent `Accept-Ranges` header changes nothing).
- Server ignores Range on a small `.part` → clean restart, sha ok
  (`c01 ignore_small`); on a ≥ 64 MB `.part` → 3 refusals then restart, sha ok
  (`c01 ignore_big`, 143 MB sent for 70 MB — bounded as designed).
- 416 on resume → `.part` discarded, fresh download of the new content, sha
  == new truth (`c02 416_on_resume`).
- RFC-correct `If-Range` server (200 on mismatch) → restart, sha == v2
  (`c02 ifrange_rfc`).
- Chunked bodies: progress carries `total=None`, early close resumes with a
  206 and completes (sha ok), `confirm_over_gb` trips on bytes-so-far with the
  `.part` kept, unsized rows hand off to a big slot, a silent chunked stream
  parks then (park_limit) fails cleanly (`c03`).
- A chunked body terminated early *cleanly* (`0\r\n\r\n` after 800 KB) is
  accepted — no client can know; the gzip trailer / JSON parse catches it at
  ingest (`c03 clean_short`). Not a finding.
- Corrupt gzip (bad CRC, flipped middle, truncated) and a truncated zip: 0
  rows land (`rates` = 0, `files.status = failed`), no fingerprint/identity
  is recorded for the bad file, the download is removed so a retry refetches
  (`c04`). Only the message quality (P3) is wrong.
- HTML 200 / redirect-to-login for a `.json.gz` URL → `failed/page` with the
  `PAGE_HELP` text; redirect that changes the extension → saved under the
  original name (`1d85a187_download.json`) but sniffed by magic and ingested
  `done` (`c05`).
- 403 on a resume (expired signed URL) → terminal with the signed-URL text,
  `.part` kept for a fresh link (`c05 r403_on_resume`); the browser-UA retry
  fires once.
- Segments: boundaries contiguous and exact (`0-73400319`, `73400320-…`,
  `146800640-220200959`), 1 probe + 4 GETs, 220.2 MB sent for 220.2 MB,
  assembled sha == truth (`c07 normal`); a Range-compressing or 200-answering
  server falls back to one connection and still finishes with the right
  bytes.
- `_reserve_disk_or_raise` is atomic: 200 trials × 2 simultaneous 2 GB
  reservations on a (faked) 5 GB free disk → exactly one accepted per trial
  (`c07 reserve_race`); reservations shrink as bytes land and are released in
  `download()`'s `finally` (the redirect-loop crash path included).
- Cancel before the first byte → immediate, nothing on disk; cancel
  mid-stream → immediate, `.part` + `.val` kept; `cancel_all_downloading` →
  row `skipped/canceled`, `.part` kept, `resume_canceled` resumes with a 206
  from the `.part` size and completes (`c08`).
- `download_retries` semantics: any byte of progress resets the consecutive
  count (6 connections for 5 cuts with retries=2); zero-progress attempts
  escalate 2/4/8 s with jitter and are bounded by park/stall (`c06`).
- Early duplicate check: a same-size candidate whose head differs costs no
  range request; a true copy is stopped after the head + 9 range requests
  (`c09` part B); a wrong `.fetchmeta` on a gz twin is still caught by the
  fingerprint (`c08 wrong_sidecar`).
- Kill at any instant during download or ingest (20 trials) → resume from
  the exact offset or reuse of the kept download, one ingest, correct sha
  (`c10`).
- `url_progress` is non-blocking on the write lock (skips the tick) and
  cheap enough at its cadence (`c11`).
