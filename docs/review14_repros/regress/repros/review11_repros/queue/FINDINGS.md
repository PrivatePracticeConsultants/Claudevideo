# Review 11 — URL queue state machine (baseline 1064203, v0.7.4)

Scope: `mrfx/fetch.py` (`add_urls`, `fetch_url_record`, `process_url_record`, `run_queue`,
expanders, `monthly_refresh_tick`) and every `url_queue` method in `mrfx/store.py`.
Every finding below was produced by a script in this folder against a scratch store under
`/tmp/claude-0/-home-user-Claudevideo/4633d594-0139-5f8e-a935-ae9c76b72000/scratchpad/audit14/regress/r11tmp/queue/` (local `http.server`, port 0; never the repo's `data/`).
Run logs: `/tmp/claude-0/-home-user-Claudevideo/4633d594-0139-5f8e-a935-ae9c76b72000/scratchpad/audit14/regress/r11tmp/queue/*.out`. Interpreter: `.venv/bin/python <script>`.
Harness: `h.py` (scratch `MrfxConfig`, fixture generator, a configurable local server, DuckDB
inspection helpers, `drain()` with a deadline).

## 1. The real transition graph

Derived from the code and confirmed by instrumenting every status-changing Store method during
a mixed drain (`s1_transitions.py`, log `s1_transitions.out`), the kill trials (`s5_crash.py`)
and the action matrix (`s9_actions.py`). `AI_HANDOFF.md` Step 4 (line 602) says
`queued → downloading → fetched → (expanding|ingesting) → done|failed|skipped|oversize`, plus
`paused` off `downloading`. The actual graph:

```
<new> ──add_urls / _enqueue_children / _maybe_queue_prev_month──────────────► queued
queued ──next_queued_url (downloader claim)────────────────────────────────► downloading
downloading ──fetch_url_record ok (or kept dest+sidecar reused)────────────► fetched
downloading ──DownloadError terminal───────────────────────────────────────► failed | oversize
downloading ──disk full / set_aside / handoff / park-limit exceeded────────► queued   (same row, no slot)
downloading ──park_url (silence < stall) / pause_url (stall, auto_resumes)─► paused   (park_count>0 = parked)
downloading ──park_waiting (early-dedup "wait" on an EARLIER in-flight row)► paused   (waiting_on set)
downloading ──early-dedup "skip" (_apply_verdict)──────────────────────────► skipped/duplicate (duplicate_of)
downloading ──cancel_all_downloading (user "stop downloads")───────────────► skipped/canceled
fetched ──next_fetched_url (processor claim)───────────────────────────────► ingesting    <-- ALWAYS via ingesting,
ingesting ──process_url_record: TOC/blob listing───────────────────────────► expanding        never fetched→expanding
ingesting ──re-download when dest/sidecar vanished (stays 'ingesting')─────► ingesting (hidden download, s8 A)
ingesting ──done-twin fast path / claim_content_ingest lost────────────────► skipped/duplicate
ingesting ──allowed_amounts / ingest "stopped" by user─────────────────────► skipped
ingesting ──ingest done / page links found─────────────────────────────────► done
ingesting ──ingest failed / unknown shape / preflight error────────────────► failed
ingesting ──ingest pending_confirmation────────────────────────────────────► oversize
ingesting ──ingest "requeued" (pool restart)───────────────────────────────► fetched
expanding ──expansion ok───────────────────────────────────────────────────► done (kind toc)
expanding ──no in-network files / parse error──────────────────────────────► failed | skipped
paused ──wake_paused_urls (timer, 60 s coordinator; start) / wake_parked_urls (idle downloader)
       / user retry ("try now") / requeue_failed──────────────────────────► queued
paused ──user skip / clear_queued──────────────────────────────────────────► skipped
failed ──user retry / requeue_failed / re-paste / re-expand / force_size / requeue_urls
       / recover_stuck_urls revive (duplicates of a FAILED twin)───────────► queued
failed ──user skip─────────────────────────────────────────────────────────► skipped
skipped ──user retry / force_size / direct re-paste (never a re-expand) / resume_canceled
        (kind=canceled only) / revive_skipped_duplicates (kind=duplicate only)
        / record_identity (content under the twin's name changed)──────────► queued
oversize ──user retry / skip / force_size / re-paste / RE-EXPAND (!) / requeue_oversize_within► queued | skipped
done ──requeue_urls (monthly static refresh) / requeue_url_for_file (ref list arrived)──► queued
done ──forget_file──────────────────────────────────────────────────────────► skipped (content_sha kept)
downloading|fetched|expanding|ingesting ──recover_stuck_urls (start) / release_claim (dead thread)► queued
queued|fetched|expanding|paused ──clear_queued──────────────────────────────► skipped/canceled
```

Mismatches with the Step 4 prose (each a P3 doc finding, confirmed by `s1_transitions.out`):
- `fetched → expanding` never happens; a TOC goes `fetched → ingesting → expanding`.
- `paused` also arises from the early-dedup "wait" (not only "off downloading" by stall/park),
  and `queued` is re-entered from `downloading` on disk-full / set-aside without any user action.
- `done` is not untouchable: `requeue_urls`, `requeue_url_for_file` and `forget_file` leave it.
- "skip only from queued/failed/oversize" — `paused` can be skipped too (and `_USER_TRANSITIONS`
  at store.py:3225 says so).
- `set_url_status_by_id` guards match the matrix (`s9_actions.out` section A): the only states a
  user has NO per-row action on are `ingesting` (only the parse "stop" button, via the files
  table) and `done`. In-flight rows cannot be acted on, by design; nothing was found that leaves
  a row in-flight without an owner (see §3).

## 2. Findings

### F1 — P1: a capped index tells the user to "press retry on this row" — there is no such button and the API refuses; the files past the cap are silently never loaded
- `mrfx/fetch.py:1487` (`cap_note`), `:2493` (done TOC row carries the note), `mrfx/store.py:3225-3248`
  (`_USER_TRANSITIONS`: `queued` only from failed/skipped/oversize/paused), `mrfx/web/app.js:4405`
  (retry button only for failed/skipped/oversize).
- Script: `s3_toc.py` (log `s3_toc.out`, section A/B).
- Observed: index listing 5 files with `max_toc_files=3` → row `done`, note "...raise max_toc_files
  and press retry on this row"; `set_url_status_by_id(idx, "queued")` → `False`; re-pasting the
  index → `{'added': 0, 'skipped': 1}`; raising the cap to 10 and restarting → `n=0`, f3/f4 still
  missing. Only `requeue_urls` (the opt-in monthly refresh) re-expands it (then `child_count=6`).
- Expected: a done index/page row is re-fetchable (retry allowed from `done` for kind toc/page,
  or a direct re-paste re-queues a done TOC/page) — a TOC carries no rates, so no dedup anchor is
  at stake.
- Fix: in `_enqueue_one` and `set_url_status_by_id`, allow `done` → `queued` when `kind IN
  ('toc','page')`; render the retry button for those rows.

### F2 — P1: an `oversize` row records `bytes_total = 0`, so raising `confirm_over_gb` and restarting (the documented fix) never re-queues it
- `mrfx/fetch.py:856` (`too_big(total)` raised at the headers, before any `progress_cb` write),
  `:2267` (status write carries no `bytes_total`), `mrfx/store.py:3470-3490` (`requeue_oversize_within`
  requires `bytes_total > 0`); the error text at `fetch.py:639-646` and the FAQ say "raise
  confirm_over_gb in config/mrfx.yaml for all files".
- Script: `s11b_oversize_raise.py` (log `s11b.out`).
- Observed (sized AND chunked): limit 1 MB, 1.5 MB file → `oversize`, `bytes_total=0`; raise the
  limit to 5 GB and restart → still `oversize`, `requeue_oversize_within` re-queued 0, 1 GET total.
  Only per-row "download anyway" works. Combined with F6, a row beyond the list window has no way back.
- Expected: `requeue_oversize_within` re-queues a row whose known size now fits.
- Fix: write `bytes_total=total` on the oversize path (`e.total` is already carried for handoff;
  set it in `too_big` and persist in `fetch_url_record` / `process_url_record`).

### F3 — P1: "Clear queue" pressed while an index is expanding is silently undone — the expansion finishes, queues every child and overwrites the user's skip with `done`
- `mrfx/store.py:3384` (`clear_queued` flips `expanding` → skipped), `mrfx/fetch.py:2446-2493`
  (`_enqueue_children` then unconditional `update_url(status="done")`).
- Script: `s9_actions.py` section C (log `s9.out`).
- Observed: `clear_queued()` during expansion → `1` cleared, TOC row `skipped`; 3 s later the TOC
  row is `done` and its 4 children are `queued/downloading/fetched/done`; all 4 ingested
  (24 rows). The same unconditional terminal write applies to any in-flight row the user acts on.
- Expected: children are not enqueued (or are enqueued skipped) when the parent was cleared.
- Fix: after expansion re-read the parent's status; if it is no longer `expanding`, do not
  `_enqueue_children` (or enqueue them as `skipped/canceled`) and keep the user's status.

### F4 — P2: `small_file_downloads` lanes never help on a fresh queue — sizes are unknown at claim time, so slot 0 ("a small file arriving never waits") takes the first unsized big file
- `mrfx/store.py:2610-2628` (`next_queued_url`: unsized rows count as small), `mrfx/fetch.py:2979-2996`
  (lanes), `AI_HANDOFF.md` round-7/9 notes ("a small file arriving never waits").
- Script: `s7_lanes.py` (log `s7b.out`). 50 big (2.5 MB, paced) + 5 small, 6 slots, 3 small lanes.
- Observed: the 5 small files were requests 50–54 of 55 in both variants; sized: claimed at 23.9 s
  of a 29.5 s drain; chunked (no Content-Length, `bytes_total` stays 0 forever): claimed at 31.2 s
  of 40.5 s. `big_lane` was never set (the handoff path is unreachable from `run_queue`).
  Real-world scale: a 5 KB provider-reference file listed after 50 multi-GB shards waits hours.
- Expected: a small file is downloaded promptly.
- Fix: size rows before claiming (HEAD / `_probe_ranges` at expansion or a cheap HEAD in
  `next_queued_url` for unsized rows), or record `bytes_total` from the index where present.

### F5 — P2: `auto_resumes` is never reset — once a row has spent its `download_auto_resumes`, every later stall (including after the user's retry) is an instant `failed`, never again "paused (pause n of N)"
- `mrfx/store.py:3299-3307` (`pause_url` increments; no path resets it — `set_url_status_by_id`,
  `requeue_failed`, `force_size_requeue`, `requeue_urls`, `_enqueue_one` all leave it),
  `mrfx/fetch.py:2243` (the check).
- Script: `s6_counters.py` section A/B (log `s6.out`), `download_auto_resumes=1`.
- Observed: run 1 → `paused`, `auto_resumes=1`; timer wake → stall → `failed`; user retry →
  stall → `failed` in 3.6 s; retry again → `failed` (auto_resumes still 1). The row's promise
  "tries again by itself" is never honoured again for that link.
- Expected: a user retry (and a bulk retry) restores the auto-resume budget.
- Fix: `auto_resumes = 0` in `set_url_status_by_id`, `requeue_failed`, `force_size_requeue`.

### F6 — P2: `list_urls` drops actionable PASTED rows once more than 500 links were pasted
- `mrfx/store.py:2735-2760` (slices: `parent_id IS NULL ORDER BY id DESC LIMIT 500`; the
  "actionable" slice is `parent_id IS NOT NULL` only).
- Script: `s9_actions.py` section B (log `s9.out`).
- Observed: 600 pasted links, rows 1–3 failed, 4 oversize, 5 parked → none of them in
  `list_urls()` (500 rows returned). A `links.txt` of 600 direct file links is a normal CLI input.
  Bulk retry still reaches failed rows; nothing reaches an oversize pasted row (see F2).
- Fix: make the actionable slice status-based regardless of `parent_id`.

### F7 — P2: an `oversize` child is re-queued by every re-expansion of its index (monthly refresh) — one request per month per file, and up to `confirm_over_gb` bytes when the server sends no Content-Length
- `mrfx/store.py:2580` (`retryable` for children includes `"oversize"`), `mrfx/fetch.py:856/869`.
- Script: `s11_oversize_reexpand.py` (log `s11.out`).
- Observed: sized file: 1 GET per re-expansion (stops at headers), status flips oversize→queued→
  oversize each time; chunked file: each re-expansion downloads up to the limit again (a `.part`
  stays on disk, `bytes_done=65536` at the 1 MB test limit; at the default 5 GB that is 5 GB per
  month per file).
- Fix: do not revive `oversize` on a re-expansion unless the row's `bytes_total` now fits.

### F8 — P2: `dedup_key` collapses two DIFFERENT files to one key when their identity param is one of the "volatile-if-signed" names and a signature is present — the second file is never queued, silently
- `mrfx/fetch.py:53-80` (`_VOLATILE_ALWAYS`: expires/signature/policy/key-pair-id/x-amz-*/sig;
  `_VOLATILE_IF_SIGNED`: token/se/sp/sv/sr/st/... stripped when any signature param exists).
- Script: `s2_dedup.py` (log `s2_dedup.out`).
- Observed: `dl?token=FILE1&sig=a` vs `dl?token=FILE2&sig=b` → same key, `add_urls` →
  `added=1 skipped=1`; same for `rates.json?st=NY&sig=..` vs `st=CA`, and ALWAYS for
  `?policy=A` vs `?policy=B`, `?expires=…`, `?x-amz-meta-file=1` vs `=2` (no signature needed).
  The code comment claims a listing differing only in `?token=<file-id>` must not collapse; it
  holds only for unsigned URLs. (No `filename_for` collision across distinct keys was found.)
- Fix: strip volatile params only when the remaining key is otherwise non-empty AND identical
  across the signature set — or keep `token`/`st` unless they look like a SAS/JWT value.

### F9 — P2: every TOC / page / allowed-amounts / unknown row leaves its `.fetchmeta` sidecar in `downloads/` forever
- `mrfx/fetch.py:2495` (TOC: `dest.unlink` only), `:2500-2503` (allowed amounts), `:2512`, `:2565`,
  `:2579` (unknown/page paths) — the sidecar is unlinked only on the ingest and failure paths;
  the start-up sweep (`store.py:2599` `done_download_filenames`) keys on `filename`, which
  these rows never get.
- Scripts: `s0_smoke.py`, `s1_transitions.py`, `s3_toc.py`, `s8_processor.py` D, `s5_crash.py`
  (`downloads_left=['…toc.json.fetchmeta']` in all 19 valid trials).
- Observed: 4 orphans after `s1` (page, allowed, unknown, toc), 6 after `s3`; never reclaimed
  across restarts. ~150 B each; a national portal crawl leaves thousands.
- Fix: `_meta_path(dest).unlink(missing_ok=True)` next to each of those `dest.unlink` calls.

### F10 — P3: counter hygiene on the other requeue paths
- `mrfx/store.py:3266` (`force_size_requeue`), `:3281` (`requeue_urls`), `:2567` (`_enqueue_one`
  revive), `:3470` (`requeue_oversize_within`).
- Script: `s6_counters.py` section B (log `s6.out`, the table).
- Observed: `force_size_requeue`, `requeue_urls`, re-paste and re-expand keep `park_count`
  (4 → only 2 fruitless parks before the long pause), `waiting_on`, `retry_after`; no path resets
  `kind` (a user-skipped row that was once `duplicate` stays `kind='duplicate'` and is therefore
  eligible for `revive_skipped_duplicates` — "rows the user skipped must never resurrect");
  `requeue_oversize_within` keeps `force_size=true`; `set_url_status_by_id` writes no `finished_at`.
- Fix: one `_reset_for_requeue` SQL fragment used by every requeue path.

### F11 — P3: the re-download of a `fetched` row whose download vanished runs under status `ingesting` — invisible to "stop downloads" and shown as "processing"
- `mrfx/fetch.py:2336-2352`, `mrfx/store.py:3402` (`cancel_all_downloading` selects `downloading` only).
- Script: `s8_processor.py` A (log `s8.out`): statuses seen while re-downloading = `['ingesting', 'done']`,
  2 GETs, ingest correct. Cosmetic/controllability only.
- Fix: flip to `downloading` before the fallback `download()` and back to `ingesting` after.

### F12 — P3: serve mode runs one rollup rebuild per idle gap (5 files arriving one at a time → 4 rebuilds)
- `mrfx/fetch.py:2754` (`_rollup_due`: `in_flight == 0` bypasses the 90 s / 4× floor).
- Script: `s8_processor.py` C/C2 (log `s8.out`): a 30-file drain → 1 rebuild (good); 5 files
  with 1.5 s gaps → 4 rebuilds (one coalesced). Bounded by the rebuild's own duration (the
  coordinator runs it synchronously), so a big store self-throttles; documented as "final
  rebuild when idle" — note it is "a rebuild per idle period".
- Fix (optional): apply the floor to idle-triggered rebuilds too, except on shutdown/drain exit.

## 3. Checked and found correct (each with a run)

- **Crash-resume** (`s5_crash.py`, seed 4242, 20 kills at 0.8–14.5 s into an 18.7 s drain of
  1 index + 6 children + 2 direct files + a twin pair; log `s5_crash.out`): in 19 valid trials
  (trial 2 was killed before `add_urls` ran — harness artifact) the restart left no row in
  downloading/fetched/expanding/ingesting, every file had exactly its expected rows (6/6/6/6/6/6,
  9, 9, twin 6 once), `files` done count == parquet parts (9 == 9) every time, no `.parquet.tmp`
  or `.part` left; kept downloads were reused (restart 1.2–7.9 s). The only leftover is F9.
- **Twin kill at the claim instant** (`s4k_twin_kill.py`, kill between `claim_content_ingest`
  winning and the ingest, with and without the other twin having deferred): after restart
  A `done`, B `skipped/duplicate`, 6 rows once, downloads cleaned.
- **Twins in both lanes / in sequence / third copy** (`s4_twins.py` A, B): exactly one ingest,
  the other rows name the right twin, bytes removed. **Owner stopped by the user mid-ingest**:
  the deferred twin proceeds and loads the content (D). **Forgotten owner whose retry then 403s**:
  twins keep the honest "removed by the user — press retry" message and are not auto-revived (C,
  by design). **User-skipped twin retried later**: skipped as duplicate again (E).
- **Early-dedup "waiting" copy vs the CLI drain exit** (`s12_waiting_drain.py`, 6 trials):
  never left `paused`; the idle downloader's `wake_parked_urls` wins the race with the
  coordinator's exit; the woken copy decides from local data (A 1 GET; B 1–3 GETs, the extra
  ones are the early check's own range requests).
- **TOC expansion** (`s3_toc.py`): self-referencing index not re-queued; mutual indexes expand
  once each; relative locations resolve against the post-redirect URL (`/v2/rel.json.gz`); a TOC
  pointing at an HTML page yields a `page` child; a file under two plans is queued once with
  both plan rows; a child also pasted directly stays the pasted row (parent NULL, one row).
- **Monthly refresh** (`s10_monthly.py`, `s10c_dead_payer.py`): never queues a URL already in
  the queue; a not-yet-posted dated index is retried once per day through day 10 (4–5 GETs/month),
  then the month is marked done; the prev-month fallback does not revive a loaded month; a dead
  payer whose previous month is also missing costs 2 GETs/day for 10 days (bounded), then stops;
  the static index is re-fetched exactly once per month.
- **Parked rows and lanes** (`s6_counters.py` C): a parked big row woken by an idle `small_only`
  slot is claimable by that slot (every lane falls back to big rows) — no stranding.
- **User actions during the 60 s disk-full sleep and during a park** (`s9_actions.py` D, E):
  the row is `queued` during the sleep; skip and retry both take effect and are not overwritten
  when the sleep ends; a skipped parked row is not woken by either wake function.
- **Dead downloader thread** (`s8_processor.py` B): the row is handed back
  (`release_claim`), a replacement thread downloads it, logged at ERROR.
- **Vanished dest/sidecar before processing** (`s8_processor.py` A): re-downloaded and ingested
  in all three cases (dest, sidecar, both).
- **`delete_raw_after_ingest: false`** (`s8_processor.py` D): forget removes the raw copy and its
  sidecar; a re-paste re-downloads; `requeue_url_for_file` reuses the kept bytes (0 GETs).
- **`dedup_key` misses** (`s2_dedup.py`): http/https, trailing slash, path case, `%`-encoding
  case, default port and dot-segments give two rows (the content sha then dedups the second —
  a wasted download, not a loss); param order, host case, empty query, fragment, S3/Azure/
  CloudFront signatures dedup correctly; no `filename_for` collision across distinct keys.
- **Fault isolation of the status matrix** (`s9_actions.py` A): no action moved `done` except
  the monthly requeue; `recover_stuck_urls` touches only in-flight rows; `resume_canceled`
  touches only `kind='canceled'`.
