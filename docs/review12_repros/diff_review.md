# Adversarial review of the round-11 diff (1064203..68d9bc7, v0.7.5)

Scope: `git diff 1064203..68d9bc7 -- mrfx/` read hunk by hunk against the seven
invariants, then every seam the diff touches probed with a scriptable local
HTTP server and scratch stores. Scripts and their outputs live in
`/tmp/claude-0/-home-user-Claudevideo/4633d594-0139-5f8e-a935-ae9c76b72000/scratchpad/audit12/agent_diff/`
(`h.py` is the harness; `old_1064203/` is `git archive 1064203 mrfx`, run with
`MRFX_SRC=` for before/after comparisons; the scratch stores were deleted
afterwards for disk). Every CONFIRMED item below was reproduced by the named
script with the pasted output; PLAUSIBLE items come from reading alone.
Nothing in the repo was edited.

Nothing found changes a parsed number, doubles a row, or loses a loaded row
(no P0). Four findings are P1 (a download that can never finish, a CLI hang,
a fix that misses the real-world case, a user action silently undone), the
rest are waste or wording.

---

## P1 — CONFIRMED: an unquoted ETag makes every resume "the server's file changed": the file never finishes and the slot churns (regression)

**Where:** `mrfx/fetch.py:655` `_same_validator`, used at `:994` (the 206
check) and in the 416 path. `_write_val` at `:1096-1101` stores
`validator = strong ETag or Last-Modified` without recording WHICH it was;
`_same_validator` guesses: a stored value that starts with `"` or `W/` is an
ETag, anything else is compared with the response's `Last-Modified`.

**Bug:** an ETag sent without quotes (non-compliant but real: IIS-style
`5d8c72a5edda8d6a:0`, some proxies/custom origins) is stored as the validator
and then compared against the 206's `Last-Modified` date. They never match,
so every resume is rejected, the `.part` is unlinked and the download
restarts from byte 0. The `bad` path resets `last_progress_t`, so the stall
clock never fires; the loop is bounded only by `download_max_seconds`
(3 h default) and `_MAX_DOWNLOAD_CONNECTIONS` (2,500). The set-aside then
requeues the row and the next turn repeats it. 0.7.4 accepted the same
resume and finished.

**Reproduce:** `s1_unquoted_etag.py` — server sends `ETag: 5d8c72a5edda8d6a:0`
(unquoted) + `Last-Modified`, honours Range, cuts every connection after
300 KB (a flaky CDN); 1.3 MB file; `download_retries=3`,
`download_max_seconds=45`.

```
0.7.5 (repo HEAD):
  WARNING ... network error: peer closed connection ... (received 307200 bytes, expected 1300000)
  WARNING ... the server's file changed since the download started — starting this file again from the beginning (attempt 1/4)
  ... (the pair repeats; the first, uncapped run was still churning after 9 min)
  FAILED: DownloadError paused after 45 s and no data downloaded so other files get a turn — it goes back in the queue and resumes from here automatically, nothing is re-downloaded ...
  requests: 20 bytes served: 5419008        part left: []
  ('GET', None, 200) ('GET', 'bytes=262144-', 206) ('GET', None, 200) ('GET', 'bytes=262144-', 206) ...

1064203 (MRFX_SRC=old_1064203):
  OK sha 8262102207da size 1300000 truth 1300000 in 4.1s
  requests: 5 bytes served: 1480224
  ('GET', None) ('GET', 'bytes=262144-') ('GET', 'bytes=524288-') ('GET', 'bytes=786432-') ('GET', 'bytes=1048576-')
```

Also note the message: "no data downloaded" while 5.4 MB were served and
"nothing is re-downloaded" while everything was — the set-aside text assumes
a kept `.part`, but the `bad` path deleted it.

**Should happen:** record the validator's KIND in `.val`
(`{"etag": ..., "last_modified": ...}` or a `"kind"` field) and compare like
with like; when the response carries no header of the stored kind, prove
nothing (as the docstring already says). A regression test: unquoted ETag +
Last-Modified + a cut connection must finish with one resume.

---

## P1 — CONFIRMED: `mrfx ingest` hangs forever when a link row is left 'ingesting' by a killed server and the same bytes are dropped in the inbox

**Where:** `mrfx/ingest.py:1659-1695` `_content_claim` (`while True: ...
time.sleep(2.0)` when the twin's status is `'ingesting'`);
`mrfx/store.py:3205` `claim_inbox_content` reads `url_queue ... status IN
('done','ingesting')`. `recover_stuck_urls` (`store.py:3342`), the only
thing that flips an orphaned 'ingesting' url row back, runs ONLY inside
`run_queue` (`fetch.py:3306`). `cmd_ingest` (`cli.py:614-623`) calls
`recover_stuck_files()` only.

**Bug:** the 403 message tells the user to download the file in a browser
and drop it in `data/inbox/`. If the server was killed while that link was
mid-ingest (status 'ingesting', `content_sha` set), `mrfx ingest` — or
`mrfx ingest <path>` — waits on a twin nobody owns, with no timeout and no
stop check. The scan never returns; every other inbox file behind it is
never loaded. In `mrfx serve` the race is bounded (the URL worker's
`recover_stuck_urls` runs within ~2 s of the watcher's first scan), but
every inbox file whose bytes equal a link that is LEGITIMATELY mid-parse
also blocks the single watcher thread for the whole parse (hours on a
national file) — by design per the commit, but worth knowing.

**Reproduce:** `s2_cli_ingest_wait.py` — one url row set to
`ingesting` + `content_sha = sha(body)`, the same bytes in the inbox, then
exactly what `cmd_ingest` does (fresh Store, `recover_stuck_files`,
`scan_inbox`) in a thread with a 20 s join:

```
INFO mrfx.ingest acme_2026-06 (1).json.gz: the same bytes are being loaded right now (x_acme_2026-06.json.gz) — waiting for that load instead of loading them twice
scan returned: False after 20.0s None
url row: [{'id': 1, 'status': 'ingesting', 'content_sha': '7e29b869...'}]
files rows: [('acme_2026-06 (1).json.gz', 'processing', None)]
```

**Should happen:** `cmd_ingest`/`cmd_add` (any CLI path that opens the store
with no server) call `recover_stuck_urls()` beside `recover_stuck_files()`;
and `_content_claim` should wait only while the twin is provably alive in
THIS process (a url row 'ingesting' with no processor thread holding it —
`holding`/`_ACTIVE_INGESTS` — is an orphan: load, don't wait), with a
`stop`/shutdown check in the loop.

---

## P1 — CONFIRMED: the re-expansion re-check (1.7) asks the server with the child's OLD stored URL, so any index whose child links carry a per-visit signature never detects a republish

**Where:** `mrfx/fetch.py:2767-2781` `_refresh_loaded_children` →
`store.done_rate_rows(...)` (`store.py:3433`, returns the row's stored
`url`) → `_server_now(cfg, r["url"])`. `_enqueue_one` (`store.py:2664`)
returns None for a `done` child without updating its `url`, so the fresh
link the index just handed over (same `dedup_key`, new `sig=`/`se=`/
`X-Amz-Signature=`) is dropped on the floor.

**Bug:** the Azure-SAS / CloudFront / S3-presigned shape — the one
`dedup_key` was built for — is exactly the shape where the stored URL is
dead by next month. The probe gets a 403, `_republished` returns None,
nothing is requeued, and the row is set back to `done` with no note. The
June vintage never loads for those payers; the 1.7 fix is effective only for
unsigned static links.

**Reproduce:** `s4_refresh_old_url.py` A (index hands out `?sig=S2`, server
answers 403 to the old `S1`, child republished with a new ETag) vs B (control,
signature constant):

```
== A: signature rotates per index visit (old sig -> 403)
 child row: [('done', '"v1"', 792, 'http://127.0.0.1:42743/child.json.gz?sig=S1')]
 requests during refresh: [('GET', '/index.json', 200), ('GET', '/child.json.gz?sig=S1', 403)]
 after refresh: [('e8532a48_child.json.gz', '2026-05', 50)]        <- still May
== B: control, signature constant
 requests during refresh: [('GET', '/index.json', 200), ('GET', '/child.json.gz?sig=S1', 206), ('GET', '/child.json.gz?sig=S1', 200)]
 after refresh: [('316dd496_child.json.gz', '2026-06', 80)]        <- June loaded
```

**Should happen:** `_refresh_loaded_children` receives the index's CURRENT
urls (it already has `urls`) and probes/re-downloads with those, keyed by
`dedup_key`; `_enqueue_one` should refresh `url` on a done child too (a
revived row already gets `url = ?`). The same applies to the `unknown`
branch and to `requeue_republished` (the re-download also uses the stored
url).

---

## P1 — CONFIRMED: "Clear queue" pressed during the re-check window is undone — the index flips back to `done` and every republished child is queued and loaded

**Where:** `mrfx/fetch.py:2977` (`_refresh_loaded_children` runs AFTER the
`only_if_expanding` enqueue step, one request per loaded child, 8 threads,
seconds to minutes against a real host) and `:3009` (unconditional
`store.update_url(url_id, status="done", kind="toc", ...)`).
`requeue_republished` (`store.py:3447`) also requeues `done` children
regardless of the parent's status.

**Bug:** the 2.3 fix covers only the enqueue step. On the first expansion of
a NEW index the window is nil (no loaded children), but on the monthly
refresh or a re-paste of a 1,000-child index the processor sits in the
re-check for a long time and a clear pressed then is reverted: parent
`skipped/canceled` → `done`, republished children queued → downloaded →
loaded.

**Reproduce:** `s7_clear_during_recheck.py` — 6 loaded children, all
republished, `_server_now` slowed to 1.5 s, `clear_queued()` 1 s into
`expanding`:

```
Clear queue pressed at expanding: expanding -> 1 rows cleared
  right after: [... 6 × ('done', 'in_network'), ('skipped', 'canceled')]
  12 s later:  [... 6 × ('done', 'in_network'), ('done', 'toc')]
  index row: {'id': 1, 'status': 'done', 'error': None}
  file months: [('2026-06', 6)]            <- all six re-downloaded and reloaded after the clear
```

**Should happen:** re-read the parent's status after `_refresh_loaded_children`
(or make `requeue_republished` conditional on the parent still `expanding`)
and keep the user's status, as the enqueue step now does; same for the
`cancel_all_downloading` case.

---

## P2 — CONFIRMED: the first monthly refresh after upgrading re-downloads and re-parses EVERY child loaded by 0.7.4, even when the server says nothing changed

**Where:** `mrfx/fetch.py:2788-2800` (`unknown` branch: `parent_reval == 1
and now is not None and not server_validator and not server_size` →
`requeue_republished(... "nothing was recorded to tell whether the payer
changed it")`).

**Bug:** a 0.7.4 row has no `server_validator`/`server_size`, but the
store DOES know the loaded file's size (`content_fingerprints.size`, written
by `_finish_ok` for every load) and the probe returned `total`. Comparing
those would settle the common case (same size → unchanged) with no download.
As shipped, a user with the opt-in refresh and N children loaded before 0.7.5
pays N full downloads + N full parses once — hundreds of GB on a national
book — and the spec's own numbers say the parse is the expensive half.

**Reproduce:** `s4_refresh_old_url.py` C (child loaded, then
`server_validator/server_size` NULLed to mimic 0.7.4; the server still
serves the SAME bytes/ETag):

```
== C: child loaded by 0.7.4 (no server record), server UNCHANGED, monthly refresh
 requests during refresh: [('GET', '/index.json', 200), ('GET', '/child.json.gz?sig=S1', 206), ('GET', '/child.json.gz?sig=S1', 200)]
 after refresh: [('0a80f60c_child.json.gz', '2026-05', 50)]      <- re-downloaded, re-parsed, same rows
```

**Should happen:** fall back to `content_fingerprints.size` (and the
download sidecar's `size`) as the "then" size before declaring the row
unknown; only rows with neither go to the full re-download.

---

## P2 — CONFIRMED: after a crash, a link that deferred to an inbox load stays `skipped` with a false "being loaded from the inbox right now" and no automatic revival

**Where:** `mrfx/store.py:3372` `revive_pred` (needs a FAILED url twin),
`store.py:3161-3180` (the link is skipped with `duplicate_of = <inbox
name>`), `ingest.py:1203-1215` (revival happens only in the inbox load's
`finally`, i.e. only if THAT process lives to finish).

**Bug:** inbox file A is loading, link B defers to it ("will retry
automatically from the kept download if that one fails"); the process dies.
On restart `recover_stuck_files` marks A `failed`; `recover_stuck_urls`
revives nothing (its predicate looks for a failed url twin, not a failed
files row). If A is still in the inbox the next scan reloads it and B is a
correct duplicate (message stale); if the user removed A, B's content is
never loaded and the row says a load is in progress forever. The user can
press retry (allowed), but nothing tells them to.

**Reproduce:** `s5_crash_inbox_claim.py`:

```
B's claim -> ('A.json.gz', 'ingesting')  [... 'skipped', 'duplicate', 'identical to A.json.gz, which is being loaded from the inbox right now — will retry automatically ...']
recover_stuck_files: 1 recover_stuck_urls: 0
files: [('A.json.gz', 'failed')]
B after restart: [{'status': 'skipped', 'kind': 'duplicate', 'duplicate_of': 'A.json.gz', 'error': 'identical to A.json.gz, which is being loaded from the inbox right now ...'}]
user retry allowed: True   kept download still there: True
```

**Should happen:** `recover_stuck_urls` (or `recover_stuck_files`) revives
`skipped/duplicate` url rows whose `duplicate_of` names a files row that is
not `done` and not in flight — the in-memory claim is gone with the process,
so at start no inbox load can be "right now".

---

## P2 — PLAUSIBLE (read only): the re-check's per-child probe uses a fresh `httpx.Client` per URL and no UA/verify override

**Where:** `mrfx/fetch.py:2494` `_server_now` (`_client(cfg)` — default UA,
default TLS context) and `:2781` (one client per child, 8 threads).

A host that needs the browser UA or the repaired chain (both levers exist in
`_download_reserved`) answers the probe with 403/TLS error → `None` →
"can't be told" → silently nothing. Cost: one TLS handshake per child per
month (1,000 children ≈ 1,000 handshakes; fine on a good host, ~1 h of a
blocked processor thread against a host that black-holes connections, since
`connect=30 s` × 1,000 / 8). Not reproduced beyond S4-A; noted so the UA
fallback is added when the URL fix above is made.

---

## P3 — CONFIRMED: stale `splices` offsets survive a restart-from-zero and cost extra sample requests

**Where:** `mrfx/fetch.py:1010` (`splices.append`), the restart paths at
`:1001-1008` (206 mismatch) and `:1215-1224` (over-length) clear
`highwater`, `sha_carry` and the `.part` but not `splices`; `:1226` then
samples the FRESH download at offsets that belonged to the discarded
attempt. Cannot give a false "changed" (the fresh bytes are the server's
current bytes), only waste.

**Reproduce:** `s8_stale_splices.py` (no validator; resume answered from
byte 0 under a plausible `Content-Range`, caught by the over-length check):

```
OK True
   GET None -> 200 307200
   GET bytes=262144- -> 206 900000
   GET None -> 200 900000                     <- the fresh, single-connection download
   GET bytes=0-65535 -> 206 65536             <- two splice samples for a splice that no longer exists
   GET bytes=196608-262143 -> 206 65536
```

**Should happen:** `splices = []` wherever the `.part` is unlinked.

---

## P3 — PLAUSIBLE (read only): the set-aside message after the churn in finding 1 says "no data downloaded … nothing is re-downloaded"

`mrfx/fetch.py:1386-1394` builds the set-aside sentence from `highwater`,
which the `bad` path zeroes; the `.part` was deleted on that path so the
"resumes from here" promise is also false. Seen verbatim in the S1 output
above. Cosmetic once finding 1 is fixed; until then it hides the churn.

---

## P3 — PLAUSIBLE (read only): `.part.val.tmp` left behind by a kill between write and rename

`mrfx/fetch.py:650-652` `_write_val` writes `<name>.part.val.tmp` then
`replace_with_retry`. No sweep names `*.val.tmp` (the start-up sweep handles
`.fetchmeta` orphans and `.part`s). Bytes-trivial; on Windows it adds 4
characters past the documented 138 (`MAX_PATH` note in the prompt, 4.4c).

---

## P3 — PLAUSIBLE (read only): `_identity_of` now reads a ≤ 4 GB inbox file in full BEFORE the parse

`mrfx/ingest.py:1624` + `:1683`. Total reads per inbox file are down
(2.49× → 2.01×, pinned), but the full identity read moved from after the
parse to before it: for a 4 GB drop on a USB/HDD that is 1–3 minutes of
`processing` with no progress bar before the first row is parsed. Only the
log line at `:1631` says why. A progress note on the files row (or feeding
the hash from the parse stream, as 3.2 proposed) would close it.

---

## Probed and held (so the next person does not redo it)

- **1-byte size probe (`_server_now` / `_probe_ranges`) against a server
  that ignores Range (200 + whole-file Content-Length), a chunked 200, a 416,
  and a 200 with `Content-Encoding: gzip`** — `s3_probe_bytes.py`: the client
  reads ONE 64 KB chunk then closes (server wrote 65,536 of 25,165,824 B in
  0.01–0.03 s); the Range-ignoring 200 still yields the right size from
  Content-Length; 416 → None; gzip 200 → Content-Length taken (raw body
  kept, consistent with `_body_chunks`).
- **Exceptions escaping the small-lane probe (it runs before `holding[]` is
  set)** — `s6_probe_escape.py`: every URL shape that `_normalize_url`
  accepts returns None from `_server_now`; the one shape that raises
  `httpx.InvalidURL` (`http://[::1/…`) is refused by `_normalize_url` and
  never reaches the queue. Unreachable.
- **"Stop downloads" during the probe** (read): `update_url(status="queued",
  big_lane=True)` does overwrite the user's `skipped/canceled`, but the id
  stays in `_canceled_downloads`, so the large lane's `download()` is
  cancelled at its first poll and the row ends `skipped/canceled` again.
  Churn, not a wrong state.
- **Small-slot starvation** (read): `small_only` claims only `NOT big`;
  `small` lanes fall back to big files; `large` lanes claim anything with
  big first. No lane can starve; unsized rows that a server refuses to size
  behave exactly as in 0.7.4.
- **Retry-After huge / HTTP-date** (read): capped at 600 s, then clipped to
  the park budget (240 s default) → the row parks and resumes; an HTTP-date
  without a zone makes `parsedate_to_datetime` naive → TypeError → None →
  generic back-off. A past date → 0 s.
- **Parent watchdog** (read + pinned test): armed by the pool initializer
  only where a pool exists (`run_queue`); `mrfx ingest` has no pool, so
  nothing to arm and nothing to kill. `os.getppid()` changes only when the
  parent dies; a legitimate long parse is never killed. Heal creates a new
  pool with the same parent pid.
- **Part-swap retry** (read): `SwapRetryMiddleware` is inside
  `LocalOnlyMiddleware` and `ServerErrorMiddleware`, outside
  `ExceptionMiddleware`, so it sees the raw `duckdb.Error` before any 500 is
  sent; bodies ≤ 1 MB only, multipart excluded. Replaying a POST whose side
  effect already happened (forget → a later read raising "no such file")
  is possible but needs a duckdb.Error after the write; not pursued.
- **Content claim under concurrency** (read + pinned
  `test_one_content_claim_for_link_and_inbox`): both claims are under
  `write_lock` and symmetric (url_queue done/ingesting ↔
  content_fingerprints JOIN files done + `_inbox_claims`); identity is
  recorded before the claim is released; one Store instance is shared by
  the watcher, URL worker and API in `serve` (`cli.py:225`).
- **`recover_stuck_files` flipping `queued`** (read + pinned
  `test_keep_both_then_restart_loads_the_kept_file`): Keep-both → failed →
  loaded from `.held`; Replace → done (part live) → loaded from
  `.replacing`; a held file that fails for real is moved to `failed/` by
  `_finish_file`, so the `.held` glob cannot loop on it.
- **Oversize re-queue** (pinned): `bytes_total` recorded from `e.total`;
  chunked (unknown size) stays 0 by design.
- **Done-children re-check when a probe fails transiently**: `None` →
  neither requeued nor failed (S4-A shows the 403 case).
- **Claims/locks across I/O**: `claim_inbox_content`, `enqueue_urls
  (only_if_expanding)`, `recover_stuck_files` do DB work only under
  `write_lock`; `_kept_meta` hashing, `_server_now`, `_splice_agrees`,
  `_refresh_loaded_children` run outside any store lock; `_fsync_path`
  runs before the lock in `finalize_rates_part`.
- **Determinism**: every new ranked read has a unique tiebreaker
  (`ORDER BY c.filename`, `(status='done') DESC, id`).
- **Per-row/per-file efficiency**: nothing new per row; per file: two
  cheap `_kept_meta` stat+json reads, one identity pass replacing
  `hash_file` + `file_fingerprint`, one `done_rate_rows` query per
  expansion. `_SHA_SEEN` is still unbounded (pre-existing); `_IDENT_SEEN`
  is capped at 2,000.
