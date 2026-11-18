# Review 14: the interface and infrastructure half of round 13 (0.7.7)

Repo `/home/user/Claudevideo`, branch `claude/gracious-mccarthy-ao7vmo`, head `9617c59`. Read-only: nothing in the repo was edited.
Diff reviewed: `c8b80fc..9617c59 -- mrfx/`. Scope: progress bar (Group 2), hot backups (6.3), PDF (6.1), links and search (6.4), Group 3 plumbing, and the front end in Chromium.

Every finding has a repro under
`/tmp/claude-0/-home-user-Claudevideo/4633d594-0139-5f8e-a935-ae9c76b72000/scratchpad/audit14/diff_ui/`
(`rNN_*.py` drive the API or modules; `uiNN_*.py` drive Chromium; `NN.out` holds the captured output).
Screenshots are in `shots/`.

Stores used. All scratch, all with absolute paths:
- `h.py`: small hand-built stores, the same shapes as `tests/mrfx/test_review13.py`.
- `live/`: built by `build_live.py`, 1,372,066 rates. It has 4 synthetic payers × 600 TINs, with hostile names and SSN-pattern TINs, plus 3 real MRFs (moda, bcbsms 03_of_13, a UHC HealthScope file). It was served by `mrfx --config live/mrfx.yaml serve` on :8791 (enrichment off, monthly recheck off).
- `ui_harness_server.py`: the real `create_app` on the same store on :8792. It adds one cost: each connection opened by a run that has a token first runs a real, interruptible ~0.4 s DuckDB scan. On a small store the bars would otherwise be gone before Stop could be clicked. Token-less requests are not touched. This harness was used only for the UI Stop and restart tests (ui03, ui05).

The 99M store was not touched, `playwright install` was not run, and no PIDs were killed (only my own background tasks and Popen handles were stopped).

---

## P0

### P0-1: A hot backup taken while a file is being committed silently loses that file's rows, yet verifies "OK" and claims them (CONFIRMED)

`mrfx/backup.py:246` lists the store's files (`store_files(root, with_database=False)`) **before** `backup.py:266` takes `store.write_lock`. The backup waits for that lock whenever an ingest or rollup holds it. While it waits, a part swap can commit (`Store.finalize_rates_part` → `_swap_in_part` runs under `write_lock`, store.py:2602-2606). The committed database then refers to the new part, and `count(*) FROM rates` under the lock includes its rows. But the part file is not in `files`, so it is never hard-linked into the snapshot or written to the zip.

Repro: `r03_backup_lists_parts_before_lock.py`. An ingest is inside its part swap (the swap is slowed by 1 s, which stands in for the ~1.4 s `replace_with_retry` window under antivirus). The weekly backup fires at that moment. The zip is then extracted and opened as a store.

```
rates before: 640 parts: 4
live store rates after ingest: 840
backup: mrfx-backup-2026-11-12_0808.zip manifest counts: {'rates': 840, 'files_done': 4} verdict: backup OK: 5 files match their checksums; recorded 840 rate rows
mrfx verify: True backup OK: 5 files match their checksums; recorded 840 rate rows
RESTORED store: rates = 640 files done = 4 parts = 4
live parts: 5
ROWS LOST IN THE BACKUP: 200
```

The backup says "recorded 840 rate rows" and `mrfx verify` passes, but the restore has 640 rows. `verify_zip` checks only checksums, never the recorded counts against the restored parts. This is data loss that surfaces on the day the user restores.

**Should:** list the files (and size the space check) **inside** the `write_lock` block, after `COPY FROM DATABASE`. `verify_zip` should also cross-check `manifest.counts.rates` against the parts' parquet row counts.

### P0-2: An SSN-pattern TIN goes into the shareable address bar (new in 6.4) and into the new PDFs (CONFIRMED; the root is older)

The Clients box accepts any TIN found in `tin_directory`, including SSN-pattern ones (`clients.add_client`, clients.py:41-58; `build_live.py` saved `070000001` without complaint). Older surfaces already show it raw:
- the digest's sub-line (`c.subject`)
- the packet folder names, and the rate card `<title>`/`<h1>` "Rate card — 070000001"
- `mrfx packets` stdout ("building packet 1 of 5: 070000001", packets.py:336)

Round 13 adds two new places where it travels:
- **6.4:** `clientJump` writes the subject into the URL (`app.js:2949`, `hashFromInputs`), and that URL is designed to be bookmarked and sent.
- **6.1:** the PDFs are printed from those same HTML documents (`mrfx packets --pdf` printed 15 of 15, including the client folder named `070000001`).

Repro `ui04_ssn_in_url.py`:
```
digest rows: ['SOLE PROP ONE PT 070000001 needs 2 files of a payer none no referral data Rates ', ...]
after the client's 'Rates' button -> address bar: http://localhost:8791/#ratecard?subject=070000001
```
Screenshots: `shots/ui04_clients_digest_ssn.png`, `shots/ui04_ssn_in_address_bar.png`. The packet HTML files are under `live/packets/2026-06/070000001/` (e.g. `1_rate_card.html:2 <title>Rate card — 070000001</title>`).

Invariant 4 says SSN-pattern TINs are masked everywhere. The subject pickers already exclude them on purpose (api.py:4322-4326). **Should:** refuse an SSN-pattern subject in `add_client` (or store it masked or keyed), and never put one in `setHash`. The user had to type the number, but the app then spreads it to the URL, file names and PDFs.

---

## P1

### P1-1: Stop on the Clients digest poisons the digest cache; every later digest and the monthly packets say "stopped" for every client (CONFIRMED)

`Stopped` is an `Exception` subclass on purpose (progress.py:51-54), so "fault-isolated loops keep working". `_build_digest` catches it per client (`except Exception as e: row["error"] = str(e)`, clients.py:200-203). Then `client_digest` stores the result in `store._digest_cache` under the unchanged key (clients.py:107-108). The route answers 409, so the requester never sees the broken result, but the cache keeps it until the data generation, the watchlist or the Medicare layers change. `build_all_packets` reads the same cache (packets.py:329).

Repro `r01_digest_stop_poisons_cache.py`. One statement is slowed by 0.8 s, standing in for a big store; DELETE is sent at 0.4 s:
```
stopped run answered: 409 {"detail":"stopped"}
next plain digest: 200
   710001 error= stopped gaps= None
   710002 error= stopped gaps= None
   710003 error= stopped gaps= None
cache key unchanged; store._digest_cache rows with error: 3
monthly packets built after the stop:
  packet file mentions 'stopped': index.html ... What changed this month: stopped; Contract ga
```

A user action (Stop) is turned into a wrong client deliverable. **Should:** never cache a result built while `progress.current().cancelled` is set (check in `client_digest` and `Store.cached` before storing), or re-raise `Stopped` from fault-isolated loops.

### P1-2: The same class of bug in the generation caches: Stop leaves a degraded cached answer (CONFIRMED for size premium; PLAUSIBLE for the roster's freshness line)

`quality.size_premium` is `@generation_cached`. Its nested `compute_benchmark` is wrapped in `except Exception: overall = None` (quality.py:452-456). A Stop that lands inside it stores the answer with the practice's position missing.

`r02_size_premium_stop_cached.py`, using `tests/mrfx/test_quality._size_market`:
```
reference subject.position = 0.0
DELETE -> 200
stopped run answered: 409
next plain request: 200 subject.position = None
```

An AST scan of every `@generation_cached` function found one other broad except: `leads.compute_payer_roster`'s freshness block (leads.py:519-528). It has the same shape: a Stop while `payer_freshness` is cold caches a roster with no "last published" line. That is by reading only, so PLAUSIBLE. The fix is the same as P1-1: `Store.cached` (store.py:5501-5527) should not store when the current run is cancelled.

### P1-2b: The Run button stays disabled after two overlapping runs (Enter pressed twice): Roster and Medicare (CONFIRMED)

`mountJobbar` records `wasDisabled = btn.disabled` (app.js:126), disables the button (app.js:127), and restores the recorded value at the end (app.js:143). A second run on the same anchor, started while the first is still in flight, records `wasDisabled = true`. If the second run ends last, it restores "disabled" and the button stays dead until the page is reloaded.

Ways to start a second run without clicking the (disabled) button:
- Enter in `#ro-payer` (app.js:1011)
- Enter in `#md-subject` (app.js:3867)
- re-entering the Overview or Clients tab (`initOverview`/`initClients` call the loader again)
- `runNegotiate` from the live column add

Repro `ui02_wedged_buttons.py`. Real Chromium against :8791; two Enter keydowns are dispatched together:
```
== attempt 1
roster: #ro-run disabled after both runs ended = True | rows shown: 500
medicare: #md-run disabled after both runs ended = False
== attempt 2
roster: #ro-run disabled after both runs ended = True | rows shown: 500
medicare: #md-run disabled after both runs ended = True
after reload: #ro-run disabled = False
```
Screenshot: `shots/ui02_roster_wedged.png` (results shown, Run greyed out).

**Should:** count runs per anchor (re-enable when the count reaches 0), or never let an anchored run start while its button is disabled.

---

## P2

### P2-1: Stop during an export's "Compressing" stage is ignored until the end; then 409, and the finished ZIP is left in the temp folder (CONFIRMED)

The compress loop in `export_zip` (api.py:3150-3160) ticks the bar but never calls `check_cancelled`. A Stop therefore waits for the whole file to be compressed (5 % of the bar, but the long tail on a big export). Then the decorator raises 409 (api.py:1684-1685). The `FileResponse` and its `background.add_task(out.unlink)` (api.py:3168) are discarded, so the ZIP stays in `<store tmp>/exports` until the 6-hour sweep (api.py:1374-1387). The outreach ZIP has the same shape: its "Writing the CSV and methodology" loop (outreach.py) and its compress (api.py:4262-4270) never check, and its unlink is at api.py:4270 (PLAUSIBLE by the same code).

Repro `r06_export_zip_stop_orphan.py` (live :8791, `full=true`):
```
DELETE during Compressing: (200, b'{"stopped":true}') pct was 95
stages seen: [('Checking the filters and the disk space', 'running'), ('Writing the rows to a file', 'running'), ('Writing the methodology', 'running'), ('Compressing', 'running')]
export answered: 409 b'{"detail":"stopped"}'
exports folder after: [('mrfx-export-gwtvyykt.zip', 1949956)]
```

On a 99M store this is a multi-GB orphan per Stop, and it counts against `_check_export_space` for the next export. **Should:** call `check_cancelled()` in the compress and write loops, and unlink the output in the decorator's stopped path (or before raising).

### P2-2: A stopped or failed hot backup leaves `<name>.zip.part` on the backup drive forever, and its "stopped" status is never recorded (CONFIRMED)

`write_zip` writes to `dest.zip.part` (backup.py:84-96) with no cleanup on an exception. The start-of-run sweep removes only `.mrfx-backup-*.tmp` (backup.py:244-245). After a Stop, `hot_backup` tries to record "stopped before it finished" (backup.py:217-219). But `_record` → `meta_set` → `store.connect()` raises `Stopped` again for the cancelled run (store.py:2136-2139). The record is lost and a traceback is logged ("could not record the backup result").

Repro `r04_backup_part_left_behind.py`. Stop through `/api/backup` while writing, then a simulated EIO (USB drive pulled) on the 3rd file, then a good run:
```
could not record the backup result
Traceback ... mrfx.progress.Stopped: stopped
stopped backup answered: (409, '{"detail":"stopped"}')
backup dir after Stop: [('mrfx-backup-2026-11-12_0809.zip.part', 3427261)]
EIO backup refused: it failed (OSError: [Errno 5] Input/output error)
backup dir after a good run: [('mrfx-backup-2026-11-12_0809.zip.part', 3427261), ('mrfx-backup-2026-11-13_0900.zip.part', 3427261), ('mrfx-backup-2026-11-14_0900.zip', 3449168)]
```

Each orphan is about the size of the whole store. Over time they eat the free space that the space guard (backup.py:251-257) needs, until backups are refused. **Should:** unlink `.part` in `write_zip`'s exception path and sweep `mrfx-backup-*.zip.part` at the start. Record the status with the progress context cleared (or using a connection that bypasses the cancelled-run check).

### P2-3: PDF: Stop does nothing while Chromium prints, a second PDF waits behind it, and a big report is a bare 500 (CONFIRMED)

`cancel()` only interrupts DuckDB connections (progress.py:266-278). `html_to_pdf` (pdf.py:54-71) holds `pdf._LOCK` through `set_content` and `page.pdf` with no deadline. That differs from `render.py`, which has a 240 s watchdog for exactly this reason (render.py:56-63). `_html_or_pdf` catches only `PdfUnavailable` (api.py:1700-1704), so any Playwright error becomes the generic 500.

Repro `r08_pdf_stop_and_timeout.py`. The pitch report's HTML is replaced by a 40,000-row table, standing in for a state-wide report:
```
Stop pressed while 'Printing the PDF': {'stopped': True}
stopped PDF answered (code, seconds since its start, type): (409, 18.7, 'application/json') | seconds after Stop: 18.4
a second PDF requested at the same moment answered (code, s): (200, 38.0)
API error on /api/report/pitch
playwright._impl._errors.Error: Page.pdf: Protocol error (Page.printToPDF): Printing failed
150k-row report as PDF: HTTP 500 after 28 s: {"error":"Something went wrong answering this (Error). The details are in the server log."}
```
The 150k-row print also made the kernel OOM-kill a process (the Chromium printer); the dashboard survived.

**Should:**
- Turn any print failure into a sentence ("too large to print here; use Open printable → Print"), as invariant 3 requires.
- Add a deadline or watchdog like render.py's.
- Have the bar say "printing — can't be stopped" instead of offering a Stop that does nothing.

### P2-4: `mrfx packets --pdf`: one document that fails to print aborts the batch with a traceback (CONFIRMED with an injected failure of the type seen live in P2-3)

`html_files_to_pdf` catches only `PdfUnavailable` (pdf.py:96). `cmd_packets` has no handler around the call (cli.py:1202-1207).

`r16_packets_pdf_one_bad_doc.py` makes the 2nd of 3 documents raise the Playwright `Error` that r08 saw for real:
```
html_files_to_pdf RAISED Error Page.pdf: Protocol error (Page.printToPDF): Printing failed
PDFs on disk: ['1_doc.pdf'] — doc 3 never attempted
```
**Should:** handle the error per document (mark it as skipped with the reason and continue), which is the fault-isolation rule.

### P2-5: `backup_dir` inside the store folder: every backup contains all the earlier ones (CONFIRMED)

`store_files` (backup.py:55-63) excludes tmp/part, duckdb_tmp, diagnostics and the snapshot, but not the configured `backup_dir`. "Next to the data" (`data/mrfx_store/backups`) is a natural choice and nothing refuses it.

`r11_backup_dir_inside_store.py`, `backup_keep: 2`:
```
backup 1: ... 3,446,890 bytes; carries earlier backups: []
backup 2: ... 6,895,584 bytes; carries earlier backups: ['store/backups/mrfx-backup-2026-11-01_0900.zip']
backup 3: ... 13,793,291 bytes; carries earlier backups: [... 11-01, ... 11-02]; deleted: ['mrfx-backup-2026-11-01_0900.zip']
backup 4: ... 24,140,330 bytes; carries earlier backups: [... 11-02, ... 11-03]; deleted: [...11-02...]
```
The 4th backup is 7× the store. **Should:** refuse a `backup_dir` inside the store (or exclude it from `store_files`).

### P2-6: A store on a disk that cannot hard-link (exFAT/FAT32 external drive): the whole store is copied under the write lock, and the store drive's free space is never checked (CONFIRMED by simulation)

When `os.link` fails, `backup.py:286-290` copies every part into `<store>/.backup_snapshot` while holding `store.write_lock`. The space guard only queries the backup drive (backup.py:251).

`r12_backup_no_hardlinks.py` (`os.link` raises EPERM; the store drive reports 0.2 GB free; 0.4 GB of parts):
```
backup OK: backup OK: 6 files match their checksums; recorded 320 rate rows
disk_usage was asked about: ['.../t12_g2e9ls2m/bk'] (never the store drive)
write_lock spans while backing up (s): [0.29]
```
The 0.29 s is a page-cache copy. On a USB 2/3 HDD the same copy of a 100+ GB store would hold the write lock for many minutes, and every ingest status write, claim and part swap waits that long. It also needs a second full copy on a drive the guard never looked at. **Should:** check `disk_usage(store.dir)` when linking fails (probe one link first), and copy outside the lock (copy, then verify the parts' signatures under the lock).

### P2-7: An iframe download's bar sticks and the Export button stays disabled after a server restart mid-build (CONFIRMED)

`startPoll` ignores non-OK answers and re-polls forever (app.js:166-179). `downloadInFrame`'s only end is the 6-hour timer (app.js:2218). After a restart the token is unknown (404). The fetch-based outputs end at "the server restarted"; the iframe ones never end.

`ui05_iframe_bar_after_restart.py` (harness :8792, restarted through its own Popen handle):
```
bar before restart: ['Export CSV · Writing the rows to a file (3 of 5) · about 13 %\nStop']
bar 20 s after the restart: ['Export CSV · Writing the rows to a file (3 of 5) · about 13 %\nStop'] | #btn-export disabled: True
404 polls after the restart: 51
```
Screenshot: `shots/ui05_export_bar_after_restart.png`. **Should:** treat 404 after a 200 (or N consecutive 404s) as "the server restarted — run it again".

### P2-8: A hot backup holds the writer lock about 25 s per GB of database file (MEASURED, as information)

`r10_backup_cost.py`: a scratch copy of the live store plus a filler table, giving a 0.33 GB `mrfx.duckdb`.
```
backup took 10.1 s; write_lock held by it for [8.29, 0.07] s
status-write latency: before median 0.045 s; during backup max 8.32 s
dashboard read latency: before median 0.037 s; during backup median 0.024 s, max 0.19 s
```
Dashboard reads are not blocked; writers are, for CHECKPOINT plus `COPY FROM DATABASE`. The stage text "new data waits a moment" is honest for small databases. On a store whose rollup tables make `mrfx.duckdb` several GB, the wait is minutes. The size of the 99M store's database file was not measured, as instructed.

---

## P3

- **Stop on an iframe export shows an alert** "The export was not built.\n\nstopped" (app.js:2221-2229). The POST downloads stay silent on 409 (app.js:4283-4285). Seen in `ui03.out`: `"dialogs": ["The export was not built.\n\nstopped"]` for `#btn-export`, `#btn-export-zip` and `#btn-outreach`. The user pressed Stop, so the alert is noise. CONFIRMED.
- **DELETE on a run that already finished answers `{"stopped": true}`** (api.py:1721-1731). The first attempt of r06 (a 926k-row export, with DELETE 0.3 s after "Compressing" appeared) printed `DELETE during Compressing: (200, b'{"stopped":true}')` and then `export answered: 200 b'PK\x03\x04…'`. The document was delivered after a Stop that the server reported as accepted. Harmless, but it misleads; it should answer `{"stopped": false, "state": "done"}`. CONFIRMED.
- **The CLI text bar on a non-TTY prints one line per nested stage.** `cli_packets.stderr` shows about 10 lines per client stuck at the same "35 % … (4 of 5)", with the benchmark's inner words ("Finding this practice", "Adding Medicare and freshness"). The cause is progress.py:477, which prints whenever the words change. The web bar has the same word swapping inside groups: the dossier poll showed "Prospect dossier · Finding this practice" at 42–62 %. CONFIRMED. The cp1252 redirect itself held (see below).
- **Back/Forward:** tab switches use `history.replaceState` (app.js:336-339), so Back leaves the app instead of returning to the previous tab. The spec does not ask for history, so this is an observation only (ui01: "after Back: view = changes" — the last hash my script set — not the previous tab).

---

## Probed and held

Progress bar:
- **Contextvar.** It never leaks into the next request served by the same worker thread, including after a route that raises. r05: 200 token-less probes interleaved with 200 token runs and 200 failing token runs gave `{'clean': 200}`. anyio runs each sync route in a copied context.
- **Sampler threads.** One per run, and gone on every exit path. r05: 0 alive after 200 runs plus 200 failing runs; 0 after 50 Stops (all 50 answered 409); active-thread delta 0.
- **DuckDB SETs.** `enable_progress_bar`/`progress_bar_time` stay per connection. r05: another connection still reads `False`/`2000`.
- **Registry.** Bounded at 256 (r05). Tokens are 32 hex characters from `crypto.randomUUID` (a secure context on localhost); two tabs cannot collide in practice.
- **Wrong work cancelled.** Background threads (rollup, enrichment, backup loop, packets, tracker) are plain `threading.Thread`s, which do not inherit the context on Python 3.13, so a Stop cannot reach them. Detached jobs carry their own token-less Progress. What a Stop can interrupt is shared lazy builds run in the request thread (`spine_materialised`, `pairs_relation`). A request waiting on the same lock then rebuilds. That is waste, not wrong output (code reading, not measured).
- **Async poll and stop under a saturated pool.** r14: 110 requests parked in a 100-thread pool, with the run itself parked too, gave `poll ... 200 running in 0.003 s; DELETE 200 in 0.002 s`, and the run answered 409.
- **The bar never changes a result byte.** r09 covered 19 output routes with and without a token. All were byte-identical (after date normalisation) except the outreach methodology's `Generated:` timestamp; r09b shows two token-less runs differ the same way. r15: the `?progress=` token never appears in, or changes, an export's CSV or methodology.
- **Stop → 409, never a partial document.** In the UI (ui03, harness) this held for 14 outputs (exports, benchmark, gaps, rate card, leads, leaderboard, dossier, market report, changes, trajectory, roster, Medicare). No page errors, no stuck bars, no wedged buttons in the single-click runs, and the report tabs show the report on finish.
- **The 99 % cap and never-backwards** held in every poll trace collected (also pinned by test_t1). The "waiting" state is pinned by test_t9 and was not re-probed.
- **CLI on a non-TTY with `PYTHONIOENCODING=cp1252`.** `mrfx export` and `mrfx packets --pdf` exited 0. The stage text is ASCII; names with 東京/RTL were replaced, not crashed (`cli_*.stdout/.stderr`).

Hot backups:
- **Retention** keeps N, deletes only its own names, and only after the new backup verifies (r11).
- **The weekly schedule** survives a restart (r11: a fresh Store gives `due 3 h after: False | due 8 days after: True`).
- **The disk-space guard** reads the backup drive (but see P2-6).
- **No restore was taken mid-WAL:** the CHECKPOINT under the lock makes the copy consistent. The part-list race (P0-1) is the gap.

PDF:
- The brand logo is embedded (a data URI survives the abort-all route): `/Subtype /Image` ×1.
- Page size is US Letter landscape (`MediaBox 0 0 792 612`).
- Output is byte-identical except `/CreationDate` and `/ModDate` (2 bytes).
- With no Chromium (empty browsers path, auto-install off) the answer is the `PdfUnavailable` sentence, not a 500.
- Two concurrent PDFs are serialised (1.2 s and 2.2 s).
- The PDF lock and the portal-render lock are separate, so neither starves the other (but two Chromiums can run at once).
- `mrfx packets --pdf` printed 15 of 15 (r07, the CLI run).

Links and search:
- **No XSS** through payer-file org names (`<img onerror>`, `"><svg onload>`, `</script><script>`), a payer named `Gamma <b>Health</b>`, the header search box, or hash routes with quotes, encoded markup, unicode or malformed percent-escapes. `window.__xss` stayed 0 throughout ui01, with no page errors.
- **LIKE wildcards** (`%`, `_`) are escaped.
- **SSN-pattern TINs** stay out of the pickers and the search list (but see P0-2).

Group 3 plumbing:
- **Caches after a forget** (r13). Stats, payers, overview, summary, leaderboard, leads, freshness and benchmark all drop the forgotten payer, both in-process and in a fresh process, so the on-disk `spine_counts`/`summary_strips` are keyed correctly.
- **Side files are bounded.** `tin_npi_pairs.*` keeps 2, `spine_counts`/`summary_strips` keep 1, superseded practice medians are deleted, and the export temps are swept at 6 h.
- **Rebuild trigger.** These side files are keyed by the parts' signatures or the spine content, not by `data_generation`, so an enrichment name refresh does not rebuild them.

Not covered:
- Search-box cost on a 2M-TIN practice list. No big store was available; the query is one `LIKE` over `tin_directory`, `LIMIT 50`.
- The backup cost on a multi-GB database. It was measured at 0.33 GB only, given about 6 GB free.
