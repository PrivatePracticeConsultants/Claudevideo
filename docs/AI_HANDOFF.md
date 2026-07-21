# AI Handoff — MRF Explorer (`mrfx`)

Step-by-step orientation for an AI (or engineer) picking this codebase up to
review it, extend it, or debug it. Everything stated here was verified against
live payer infrastructure; dates and numbers come from real runs.

## Step 1 — Understand what this is

A local, single-user pipeline + dashboard that aggregates **negotiated
commercial rates** for PT/OT/SLP therapy codes from payers' federally mandated
Transparency-in-Coverage machine-readable files (MRFs), into one queryable
DuckDB/Parquet store with benchmarking, QA, and outreach exports on top.

The user's goal: paste payer links → the app downloads, filters to their CPT
code set, deduplicates, and keeps a compact analytical database. Files are
1 MB–22 GB compressed (up to ~200+ GB uncompressed); the entire design is
shaped by that.

Two codebases live in this repo:
- `mrfx/` — the product (this document).
- `src/` + `run.py` — an earlier, single-purpose BCBS-Missouri pipeline
  ("bcbs-mo"). Reference only; do not extend it. Some crawling knowledge
  (Blue KC Gatsby hub) was later generalized into `mrfx/fetch.py`.

## Step 2 — Learn the module map

| Module | Role |
|---|---|
| `mrfx/config.py` | Pydantic config (`config/mrfx.yaml`). Key knobs: `codes.cpt_codes` (the target set; `all_codes: true` disables filtering), `confirm_over_gb` (download size guard, default 5), `max_toc_files` (children per index, default 2000), `parallel_ingests` (parser processes; **0 = auto = cores−1 capped 8**, positive value clamped to cores−1 — via `resolve_worker_count`), `parallel_downloads` (concurrent fetchers; **0 = auto = min(workers, 4), floor 2** — via `resolve_download_count`; `1` = strictly sequential), `delete_raw_after_ingest`, `render_js` / `render_auto_install` (JS-portal headless browser + auto-download of Chromium), `enrichment.mode` (`api`/`bulk`/`off`) + `enrichment.api_concurrency` (default 8). Any typo raises one friendly `ConfigFileError`; unknown keys warn. |
| `mrfx/sniff.py` | Header-window (1 MB) classification of any file: `in_network / provider_reference / toc / allowed_amounts / blob_listing / unknown`; `open_stream` (gzip/zip/plain + byte-progress); preflight verdicts. |
| `mrfx/parser.py` | Streaming ijson parser. `InNetworkParser` (event-level, constant memory, batches rows to a sink), `skim_needed_ref_ids` (pass 1 of big files: returns target-cited ref ids + count of target-code items), provider-reference file parser, GP/GO/GN modifier-first discipline attribution, TIN/NPI pairing rules. |
| `mrfx/ingest.py` | `ingest_file` orchestration: quarantines non-rate files, two-pass chunked ingest for files ≥1.5 GB uncompressed (`LARGE_FILE_UNCOMPRESSED_BYTES`), scan short-circuit when pass 1 finds zero target codes, chunk progress (`_ProgressTracker`), QA report, parallel worker (`_parse_worker`, `ParsePoolManager`) — workers are PURE (no DB access). |
| `mrfx/store.py` | DuckDB + Parquet parts (zstd-compressed via `PARQUET_COMPRESSION`; mixed snappy/zstd parts coexist, no migration). One parquet part per source file (re-ingest = atomic replace = idempotent). Materialized rollups: `rates_by_tin_tbl` (the spine), `tin_directory_tbl`; `rates_dedup` is a LIVE VIEW (see invariant 6). `url_queue` table drives URL ingestion. `enrichment_progress` (cached 60s) + `available_states` back the dashboard's name/state UI. SSN masking. All writes behind one in-process lock. |
| `mrfx/fetch.py` | URL-drop pipeline: `download` (retry/backoff, atomic `.part`, HTTP-Range resume, sha256 while streaming, disk-space guard, TLS incl. AIA chain repair), `dedup_key` (volatile signed-params stripped), `expand_toc`, `expand_blobs_listing` (UHC/Optum), `extract_links_from_page`, `crawl_gatsby_hub` (Sapphire hubs), `probe_blobs_api`, `process_url_record` (route one URL), `run_queue` (N downloader threads — `parallel_downloads`, 0=auto capped 4, concurrent fetches reserve remaining bytes so they never collectively overcommit the disk — + N processor threads + rollup batching). |
| `mrfx/render.py` | OPTIONAL headless-Chromium fallback for JavaScript-only portals: renders the page, harvests links from the DOM + captured JSON responses, and when nothing appears dismisses consent overlays and clicks MRF-looking controls ('View Plan List' — Harvard Pilgrim) with a re-harvest; the browser never networks itself — every request is fetched by httpx (proxy/CA-aware, TLS verified) and fulfilled into the page. Degrades to a help message without Playwright. |
| `mrfx/known_sources.py` + `config/known_sources.yaml` | The catalog of live-verified payer entry points with results; `{FIRST_OF_MONTH}` placeholder resolution. `config/starter_links.txt` is the paste-ready export of it. |
| `mrfx/api.py` | FastAPI JSON API + static SPA. `/api/rates` (tin/npi/entity grains, server-side paging), benchmarks, `/api/schedule/*` + `/api/report/*`, exports (+methodology sidecars), outreach CSV, `/api/urls*`, `/api/known-sources`. |
| `mrfx/benchmark.py` | Market percentiles, opportunity model, payer-negotiation one-pager, pitch report; shared `_market_where` / `resolve_subject_tins` / `_brand_header`. Reports refuse without a pinned month + state (or explicit `allow_national`). |
| `mrfx/schedule.py` | Fee-schedule reconstruction ("your rate card") + payer scorecard ("who pays best"), reusing benchmark's market filter so numbers agree. Scorecard ranks by median % of Medicare (MPFS loaded) else median % of best payer over head-to-head codes. |
| `mrfx/web/` | Vanilla-JS SPA (no CDN deps): Explorer, Code comparison, Benchmark, Rate card, Files (paste-links card + queue), Sources. |
| `mrfx/cli.py` | `mrfx serve / add / preflight / ingest / status / export / outreach / forget / reset`. `add` hands URLs to a running server, else drains locally. `forget <filename>` (or DELETE `/api/files/{name}`, the Files-tab remove button) erases one file's rates + raw copies and flips its done url_queue row to 'skipped' so the dedup anchor never outlives the data; rollups rebuild after. |

## Step 3 — Internalize the invariants (violating these breaks real users)

1. **Streaming only.** No `json.load` of an MRF, ever. Peak memory must not
   scale with file size (verified: ~34 GB uncompressed at <1 GB RSS).
2. **Single DB writer.** Only the main process touches DuckDB. Parallel
   workers write their own `*.parquet.tmp` and hand back metadata. Temp files
   must never match the `*.parquet` view glob.
3. **Fault isolation.** A bad file/URL marks ITS row failed with a
   plain-language message and the worker moves on. Worker loops survive
   transient store errors (lock conflicts retry ~6s in `Store.connect`),
   including a store failure INSIDE a failure handler (the recovery write is
   itself guarded — a dead processor thread would stall the queue forever).
   A dead parser process heals (`ParsePoolManager`) and the file retries once.
   The inbox watcher re-arms with 15s backoff if the watch itself dies
   (deleted inbox dir). A typo in ANY user-editable file must never kill the
   program: config/mrfx.yaml errors print one actionable `config problem:`
   line and exit 1 (ConfigFileError; unknown keys warn; numeric bounds and
   enrichment.mode Literal enforced); per-entry garbage in entity_map /
   payer_registry / registry_overrides / known_sources is skipped with a
   warning; a malformed mpfs_path CSV degrades instead of stopping serve.
3b. **Cosmetic never fails real work.** `store.update_progress` /
   `store.url_progress` are best-effort BY CONTRACT (swallow + debug-log) —
   they run inside download/parse loops and must not fail them. `qa_report`'s
   DuckDB aggregates degrade with a note (the parse already succeeded; its
   parquet is durable). The CLI progress bar is dropped on error, not fatal.
   Parser workers exit at the next chunk boundary if their parent dies
   (`os.getppid` check) — a SIGTERM'd server must not leave orphans burning
   CPU on parses nobody will collect. `_finish_file` (the post-success move
   of a source out of the inbox) is HOUSEKEEPING and never raises — on
   Windows, antivirus/indexer locks routinely throw there, and letting that
   escape flipped a fully-successful ingest to 'failed'. When Playwright's
   Chromium is missing, `_launch` auto-downloads it once
   (`python -m playwright install chromium`; `render_auto_install: false`
   or PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD opts out) and retries before
   surfacing the manual fix; the missing-browser message match is loose on
   purpose (Playwright rewords it across versions).
4. **Honesty contract.** Never fabricate: unverifiable registry links are
   badged unverified; drug/allowed-amounts files are skipped with the reason;
   0-row files stay 0 honestly; per-file payer attribution comes from inside
   the file (Blues cross-host each other's files — expected, documented);
   the export ZIP and every CLI export carry a methodology sidecar (whose
   source list is labeled as store inventory, NOT the filtered export's
   provenance; source_files is one representative file, source_count the
   total); the dashboard's plain-CSV buttons return just the CSV — use the
   ZIP for anything that needs provenance attached. SSN-pattern TINs are
   masked PER ELEMENT of joined lists and in the no-name fallback labels
   ('TIN MASKED-SSN'), server-side and in the SPA's raw-unit_id sinks
   (maskTin in app.js). Every CSV export formula-defuses third-party text
   (leading =+-@ etc. get a quote prefix — `_defuse_sql` in api.py,
   `_defuse` in outreach.py); pitch HTML html-escapes third-party names.
   Benchmark basis_note/assumptions echo the toggles actually applied
   (include_assistant, base_only=false, dropped billing_class); the
   %-of-Medicare footer discloses the median-across-localities anchor.
4a. **Numbers-correctness rules (the product IS the numbers).**
   `negotiation_arrangement != ffs` items (bundle/capitation) are EXCLUDED —
   a bundle price is not a per-code rate (QA-counted as bundled_items).
   Modifier and service-code arrays are sorted+deduped at parse time so
   publication order can never split the dedup grain. file_month = validated
   header month, else the filename's stamped date, else ingestion month.
   An untyped 10-digit tin.value is an NPI-in-the-TIN-slot (flagged), not an
   EIN. A provider group with a TIN but NO NPIs (a TIN-only rate) emits one
   row at `npi = NULL` (QA-counted as tin_only_rows) so the rate reaches the
   TIN/entity grains; those rows are filtered OUT of the NPI grain
   (DEDUP_QUERY `WHERE npi IS NOT NULL`) so they never show as a phantom NPI.
   $0/$0.01/negative DOLLAR rates are payer placeholders: kept as rows (the
   hide-outliers toggle masks them) but excluded from the TIN median
   (BY_TIN_QUERY FILTER, coalesce fallback for placeholder-only TINs) and from
   the benchmark market. `load_provider_refs` serves only each ref_id's NEWEST vintage —
   two companion files must not union stale provider lists. Rollups rebuild
   after enrichment lands (geographic benchmarks join tin_directory's
   states/cities). Entity-grain rate = median of member-TIN rates — the
   methodology text says exactly that, never "distinct published values".
   MPFS codes are normalized like billing codes before matching. Payer
   names are free text WITH COMMAS ("..., a Division of HCSC") — the
   `?payer=` filter travels as repeated params (`_qp` + FilterSet's
   `multi()`), never comma-joined/split, or one name shatters into two
   that match nothing.
4b. **NPPES poisoning guard.** Only a genuine HTTP-200 body WITH a "results"
   key (empty list) may mark an NPI dead. Non-200s AND 200-wrapped error
   bodies ({"Errors": [...]}) leave the NPI un-enriched for retry; a
   persistent failure run (20 + workers) trips the circuit breaker and
   pauses until the next run. Lookups run CONCURRENTLY
   (`enrichment.api_concurrency`, default 8) so names/states fill fast;
   `unenriched_npis` serves only well-formed 10-digit ids so junk from
   messy files can't wedge the loop. `/api/stats` exposes enrichment
   progress (named/total/remaining) and `/api/states` the filterable
   states — the dashboard shows both so an incomplete state filter reads
   as "still identifying", not "broken".
5. **Idempotency / crash-resume.** Kill anything at any time: `.part`
   downloads resume via HTTP Range guarded by an If-Range validator (`.val`
   sidecar — republished content restarts instead of splicing); in-flight
   queue rows AND files-table 'processing' rows recover on restart
   (`recover_stuck_urls` / `recover_stuck_files` — the latter runs at CLI
   startup, before any worker thread, so it never races a live ingest);
   re-ingest atomically replaces that file's part — never duplicates rows.
   Content-sha dedup skips byte-identical files from other domains. The
   cheap pre-ingest check is a done-only FAST PATH (skip before wasting a
   preflight); ALL in-flight twin arbitration happens in
   `claim_content_ingest` — an atomic check-and-claim under the write lock
   right before ingest whose DEFER also lands inside the lock (loser flipped
   to skipped/duplicate in the same step). Both halves being atomic is what
   guarantees exactly one twin per content ingests: an unlocked defer let
   two racing twins each see the other 'ingesting' and BOTH skip — nobody
   ingests, and the revive hook never fires because neither twin fails. A
   skip against a still-*ingesting* twin keeps its downloaded bytes and is
   auto-revived (skipped→queued) if that twin later fails.
   Auto-revival touches ONLY kind='duplicate' skipped rows — rows the user
   skipped or forgot share the sha but must never resurrect behind their
   back. Recovered 'fetched' rows whose download + .fetchmeta sidecar
   survived are reused as-is, never re-downloaded (expired signed URLs
   would 403 terminally and destroy the only copy); sidecars are written
   atomically. `mrfx reset` clears the url_queue with the data — done-row
   anchors must not outlive the store they anchor. User erasure
   (`forget`/DELETE/remove button) refuses 'processing'/'queued' files (a
   live parse would silently resurrect the data), deletes raw copies BEFORE
   the DB rows (the watcher must not re-ingest mid-erase), and orders
   parquet-unlink-first / files-row-last-in-one-transaction so a partial
   failure leaves forget retryable instead of orphaning unreachable rates.
   A second `mrfx serve` claims the port BEFORE touching the store (running
   crash-recovery against a live server's rows corrupted in-flight parses);
   `mrfx ingest`, `forget`, and `reset` probe for a running server the same
   way (timeouts count as "owned" — fail closed). IN-process, one ingest per
   filename at a time (`_claim_ingest`): watcher, upload scans, confirm
   double-clicks, and requeue_skipped share pid-suffixed temp paths and
   would corrupt the live part racing each other; forget takes the same
   claim, and additionally refuses while the file's QUEUE row is in flight
   (a retry keeps the files row at its old terminal status for the whole
   download). Uploads stream to `*.uploading` and rename when complete;
   scan_inbox skips working suffixes and size-growing files (a half-copied
   file must not be quarantined and moved out from under its writer);
   parse-progress sidecars live under the store, never the watched inbox.
   The DuckDB temp-disk cap and SET threads are GLOBAL to the process's
   shared instance: the cap is computed once per Store (per-connection
   recomputation strangled running rebuilds), and the OOM retry's
   single-thread mode is RESET afterward.
6. **Rollup scalability.** `rates_by_tin` / `tin_directory` are materialized
   (few groups); `rates_dedup` MUST remain a live view — at NPI×rate grain a
   30M-row store means a ~30M-group aggregation whose spill exceeded 27 GB of
   disk when it was materialized. No `string_agg(DISTINCT …)` in rollups
   (cannot spill); DuckDB `memory_limit` is 40% RAM clamped [2,12] GB.
   NO recurring whole-store scans may exist: the enrichment name-refresh
   goes through `refresh_directory_incremental()` (TINs of NPIs with
   `enriched_at` newer than meta `directory_names_through`; >= re-includes
   boundary NPIs rather than ever missing one), falling back to the
   names_only full rebuild (first build, drift, >25%-of-TINs affected).
   Removals (forget / re-ingest-to-0-rows / replaced parts) queue their old
   payers in meta `rollup_pending_payers` (generation-counted; consumers
   subtract only what they processed and only if the generation didn't move
   — see `_queue_removed_payers`/`_consume_pending_payers`); an unreadable
   doomed part sets `rollup_needs_full` instead. `rollups_stale()` returns
   True for ANY of: marker-behind (with same-microsecond filename
   tie-break via `rollup_covered_names`), pending payers, or needs_full.
   Coverage markers are SNAPSHOTS taken before a build reads anything —
   never stamped from post-build state (files finishing mid-build must
   stay newer than the marker). Done-upserts stamp `finished_at` INSIDE
   the write lock via the `FINISHED_NOW` sentinel (a pre-lock timestamp
   can land behind an advanced marker). All rollup-side catalog writes
   (view registration) take write_lock briefly — two connections'
   CREATE OR REPLACE VIEW race raises TransactionException and once could
   mark a finished multi-hour parse failed. Store provenance: every build
   of this version writes `rollup_covered_names`; if the version marker
   matches but that key is absent at open, an OLDER app copy may have
   rebuilt the tables after a rollback — `_migrate_stale_rollups` sets
   `rollup_needs_full` for one background full rebuild. NEVER run an older
   app copy against an upgraded store expecting correct analytics.
   REMOVALS (forget / re-ingest-to-fewer-rows / replaced part) recompute the
   rate spine payer-scoped BUT the tin_directory in FULL: a removal can change
   a directory row for any TIN that lost a subset of its NPIs while keeping
   rates from other files (a shared TIN), and the directory has no payer
   column to scope by (regression-tested via the hospital+therapy shared-TIN
   case). Removals are rare (never in a first-time grind), so this is correct
   and cheap in practice; additive updates keep both tables payer/TIN-scoped.
   The dashboard's /api/stats (whole-store count(DISTINCT tin_value/payer)) and
   /api/states are cached + single-flight (`store_stats`/`available_states`,
   like `enrichment_progress`): uncached on the 15s poll they stacked
   concurrent whole-store HDD scans and starved the parser workers — the
   long-hunted "starts strong then stalls, CPU busy, chunks frozen" report.
   Ingest-driven refreshes go through `update_rollups_incremental()` first:
   it recomputes ONLY the payer slices of `rates_by_tin_tbl` (grain includes
   payer, so groups never cross payers — payer scoping also heals re-ingests
   whose file_month changed) and only the delta files' TINs in
   `tin_directory_tbl`. The delta = done files newer than the
   `rollup_covered_through` marker; affected payers come from the delta
   files' ROWS (never `files.payer` — multi-licensee books stamp per-row
   payers). Median(DISTINCT) is not mergeable, so slices are re-aggregated
   exactly from raw rows: results are bit-identical to a full rebuild
   (equivalence-tested), measured 104s full vs 9.3s incremental on a
   2.45M-row 3-payer store. Any precondition failure (no prior full build,
   schema migration, no rows) raises and the caller falls back to
   `rebuild_rollups()`. `forget` ALWAYS uses the full rebuild (a removal
   leaves no delta to key on). The done-upsert must precede the rollup call
   (the incremental finds its work via the marker; regression-tested).
   CLUSTERING (do not remove): both materialized rollups are built with a
   trailing `ORDER BY` — `rates_by_tin_tbl` by `(billing_code, tin_value)`,
   `tin_directory_tbl` by `tin_value` (in BY_TIN_QUERY / TIN_DIRECTORY_QUERY, so
   it flows through the full build, every hash slice, AND the incremental
   delta). This is LOAD-BEARING, not cosmetic: DuckDB keeps per-row-group
   min/max zonemaps, so a code-filtered query (nearly every real summary /
   benchmark / leads / `/api/code` call carries a `billing_code IN` filter)
   skips the row groups that can't match instead of scanning the whole spine —
   measured 20-60x on a code-filtered aggregate over 30M rows (80ms→2ms).
   Clustering the directory by tin_value gives the point lookups that dominate
   it (entity-detail, the per-row LEFT JOINs from rates_by_tin / monitor /
   outreach / benchmark) the same zonemap pruning an ART index would, but
   carried in the build query so it survives every CREATE OR REPLACE with no
   index to maintain and no INSERT slowdown on the delta path. The ORDER BY is a
   SPILLABLE operator over the (already grouped, smaller) output, so it never
   breaks the bounded-memory slice build; each hash slice sorts independently
   and still prunes because pruning is per-row-group, not global. Rows are
   byte-identical as a SET — only physical order changes, and every reader
   re-aggregates. Regression-guarded (`test_rollup_tables_are_clustered_*`);
   incremental deltas append a small unsorted tail that a full rebuild
   re-clusters. `/api/payers` reads DISTINCT payer from the materialized spine
   (small native table) rather than a DISTINCT scan across every raw parquet
   part on the dashboard-load path.
   Mid-grind refreshes are purely TIME-based (`_rollup_due`): one per
   adaptive interval of max(90s, `ROLLUP_INTERVAL_MULTIPLE` (4x) × the last
   rebuild's duration), counted from rebuild COMPLETION, plus a final rebuild
   when the queue goes idle. There is deliberately NO count-based ("N files
   pending") trigger: on a big store one rebuild outlasts any batch of fresh
   ingests, so an unconditioned batch check fired the instant each rebuild
   finished — rebuilds ran back-to-back for the whole grind, monopolized the
   store's disk, and extraction slowed to "chunks frozen, CPU busy" (the
   measured July 2026 field failure). Counting the interval from the start
   instead of completion would likewise degenerate into rebuild-per-file.
   `scan_inbox` batches to ONE rebuild per pass for the same reason.
   Rebuilds give up after 3 failures (raw data is safe) and never run
   per-file during queue grinds.
   COALESCING (skip_if_busy): the PERIODIC triggers — the ingest worker's
   `rebuild_now` and the enrichment name refresh `_maybe_refresh_directory` —
   now pass `skip_if_busy=True` to `update_rollups_incremental` /
   `rebuild_rollups` / `refresh_directory_incremental`. `_acquire_rollup_lock`
   then does a NON-BLOCKING acquire and returns the `ROLLUP_SKIPPED` sentinel
   instead of queueing. Before this, both triggers blocking-queued behind one
   in-flight rebuild and then each ran its OWN multi-hour pass in turn — on a
   300M-row store that meant a full rebuild finishing only to have the queued
   name-refresh start its own whole-store pass, logged as "analytics rebuild:
   waiting for the previous rebuild to finish (388 min so far)" from TWO
   different waiters at once, which reads as an infinite loop. With coalescing at
   most one rebuild runs; a skipped trigger keeps its work pending (ingest
   credits / the names-dirty flag) and the next cadence retries once the lock
   frees. The one-shot CLI enrichment passes `force=True` → `skip_if_busy=False`
   so it still runs immediately (no concurrent rebuild exists there). A slow
   FULL rebuild being CHOSEN over the fast incremental is a separate signal:
   `update_rollups_incremental` raises (and `rebuild_now` falls back to a full
   rebuild, LOGGING the reason) when `rollup_needs_full` is set — a removal that
   couldn't be scoped, or a prior full rebuild that never completed to clear it.
   The flag clears only on a SUCCESSFUL full build (rate spine included), so a
   full rebuild that keeps failing, or a re-ingest whose old part is transiently
   unreadable and re-latches the flag, keeps forcing fulls. If the field log
   shows "incremental analytics update unavailable (a data removal could not be
   scoped — full rebuild required)", that is the cause.
   Above ~15M raw rows they build in hash-partitioned slices (the partition
   column is in every GROUP BY key — slice-union ≡ single shot) inside ONE
   transaction: a mid-slice failure rolls back to the previous complete
   tables, never a partial one. Each connection caps `max_temp_directory_
   size` at 80% of free disk so a rollup can't starve unrelated work.
   A rollup failure after the parquet part is durable does NOT fail the
   file (`_rebuild_rollups_best_effort`).

## Step 4 — Know the URL pipeline states

`url_queue.status`: `queued → downloading → fetched → (expanding|ingesting) →
done | failed | skipped | oversize`. `kind`: `toc / page / in_network /
provider_reference / allowed_amounts / duplicate / unknown`.
`oversize` is a distinct terminal state for files over confirm_over_gb — NOT
'failed' (so one big payer's shards don't inflate the failure count or bury a
real error); it shows amber "too big" in the UI with a "download anyway"
button (`force_size_requeue` sets a one-shot `force_size` flag consumed by
`_size_limit_for`; the flag is reset on every generic retry/re-queue so the
override never silently carries into a later bulk retry). User actions are
guarded: retry only from failed/skipped/oversize; skip only from
queued/failed/oversize (a done row anchors content-sha dedup and is
untouchable). `dedup_key` strips signature/expiry query params but keeps
identity params. `list_urls` pins actionable rows into the window (top-level
pastes first, then failed/skipped/oversize children, then newest others,
deduped by id) — a file forgotten mid-grind on a 2,000-row queue keeps its
retry button reachable.

## Step 5 — Performance envelope (measured)

- Parse ≈ 30–35 s per uncompressed GB per pass (ijson C backend; this IS the
  bottleneck — 95% of processing; isal/gzip and buffer tuning were measured
  and rejected as noise).
- Faster-parse dead ends, measured (don't re-litigate without new data): the
  Python event loop is ~55% of parse; a tight drain-loop for skipped items
  gains ~0% (the C generator's yield is the cost, not the loop body);
  `ijson.items` C-building every item is ~1.4x end-to-end BUT materializes
  every NON-target item as Python dicts — an all-codes UHC file can carry a
  single non-target item large enough to spike a worker by GBs, which the
  current skip path never builds. Rejected: breaks the peak-memory envelope
  on exactly the worst files. Larger `buf_size` to ijson measured SLOWER
  (64 KB default wins). The remaining honest lever is process count
  (`parallel_ingests`), documented for the user in GETTING_STARTED
  "Making a big queue finish faster".
- Two-pass for ≥1.5 GB uncompressed; pass 1 skims target-cited refs (80k ids
  on an 8 GB Anthem shard vs 4.2M total); short-circuits pass 2 if zero
  target codes.
- Parallel: N worker processes (`parallel_ingests`), N downloader threads
  (`parallel_downloads`, 0 = auto = min(workers, 4), floor 2) prefetching
  into the `fetched` state so the network overlaps parsing and many-small-
  file payers don't starve the parsers behind one link. 3 files, serial
  124 s → 2 workers 83 s, byte-identical outputs. Disk safety with
  concurrent fetches: each download reserves its remaining bytes
  (`_disk_reservations`) and the up-front guard counts everyone else's
  reservations against free space — written bytes shrink the reservation as
  they land in `disk_usage`, so nothing is double-counted and the fleet can
  never collectively overcommit the drive.
- Reference yields: UHC MO network 30 MB → 88,649 rows; Oxford 0.58 GB →
  5.59M; Heritage 1.7 GB → 9.83M; PS1-77 3.39 GB → 29.69M (70 s rollup at
  6.3 GB peak after the live-view fix); BCBSLA 2.8 GB unc → 14.6M.
- Spill placement: DuckDB spills big rollups to `temp_directory`. On a spinning
  HDD that stalls every parser worker (observed: 8 workers, CPU stuck ~39%).
  `duckdb_temp_dir` overrides the location; when UNSET, `_auto_spill_base`
  (Windows only) queries `Get-PhysicalDisk`+`Get-Partition` once (cached, 10s
  timeout) and routes spill to the roomiest SSD (≥20 GB free) IF the store is
  on a confirmed HDD — else stays in-store. Every step is best-effort: any
  failure → in-store default, never blocks store open. Each store gets its own
  `spill-<sha1(store)[:10]>` subfolder so two stores sharing a base can't clash.
  Pure decision (`_choose_spill_drive`) and PS parsing (`_parse_media_output`)
  are unit-tested; the Windows probe itself is not runnable off-Windows.

## Step 6 — Extending to a new payer (the usual task)

1. Probe: is it a static page with `.json`/`.zip` hrefs or quoted config
   paths (works — Centene, Cigna's /static manifest, BCBS NC's signed TOC,
   CareFirst's data-key attributes), a CMS TOC/index URL (works — Highmark,
   BCBS-MS, Anthem's 10.5 GB master index), a Sapphire/Gatsby hub (works —
   `*.sapphiremrfhub.com`), a blobs-API React portal (works — UHC/Optum), a
   JS-rendered page (works WITH Playwright — Molina, Kaiser, Aetna/health1,
   Harvard Pilgrim's click-gated list, Regence, Oscar; `mrfx/render.py`
   renders, clicks consent/"view list" controls, and harvests DOM + API
   responses), or genuinely interactive/firewalled (Premera, HCSC, UHS —
   catalog as `portal` with honest notes)?
2. If a new *platform* pattern: add a probe/expander in `fetch.py` following
   `crawl_gatsby_hub` / `probe_blobs_api` as templates — wire into
   `process_url_record`'s unknown-HTML branch (extract → gatsby → blobs →
   PAGE_HELP order), never bypass `dedup_key`/`_enqueue_children`.
3. Verify LIVE against the real payer (bounded: expand index, ingest 1–2
   children, check rows + payer attribution), add a fixture-based regression
   test mirroring the live shape, and record the result in
   `config/known_sources.yaml` (queueable only if truly hands-free; honest
   notes with verified date and numbers).
4. Update `config/starter_links.txt`, README counts, and GETTING_STARTED if
   user-visible.

## Step 7 — Test and verify like the history did

- `.venv/bin/python -m pytest tests/ -q` — the suite (167+ tests) runs real
  end-to-end drains against local HTTP servers, including parallel mode,
  kill-recovery semantics (crash-flip of stuck files/urls is tested
  directly), the forget HTTP flow, dedup, guards, and messy-file parser cases
  (`tests/mrfx/test_messy_files.py` documents payer quirks: header-at-EOF,
  refs-after-in_network, multi-code fields, junk types).
- For anything touching ingest correctness, also do a live bounded run and
  compare row counts to the catalog's recorded numbers (e.g. UHC Missouri
  file must produce exactly 88,649 rows with the default code set).
- Trials live OUTSIDE the repo (scratchpad) with their own `mrfx_*.yaml`;
  never point a trial at the user's real store. Background long runs with
  `nohup`, guard driver scripts with `if __name__ == "__main__"` (spawn
  workers re-import `__main__`).

## Step 7b — The source catalog is the map

`config/known_sources.yaml` holds 38 auto-queueable sources + 34 probed
portals, each with verified dates and real row counts — treat it as ground
truth for "does this source work and what should it yield". The largest
verified single-file ingests: Cigna CHLIC 243.4M rows, Aetna CA 83.7M, UHC
Charter 617 64.0M (8.85 GB gz), NC PPO 23.7M, upper-midwest zip 19.8M.
Guards verified live: 35.2 GB refused by confirm_over_gb, 25.1 GB by the
disk-space guard, both with actionable messages.

## Step 8 — Known limitations / next frontiers

Deferred from the July 2026 comprehensive audit (real findings, consciously
not yet built — pick these up before adding features):
- Signed-URL re-expansion: a TOC's children hold signed URLs captured at
  expansion; after a long pause they 403. The .part is now KEPT on 403 and a
  fresh link resumes it, but there is no auto-re-expand of the parent TOC on a
  child 403, no re-expand action on a 'done' TOC row, and revive-on-re-add
  does not update a child row's stored URL. A wall of 403s after a multi-hour
  pause on mrf.bcbs.com/Anthem still requires re-pasting links.
- No cancel for in-flight downloads/parses (only queued/failed/oversize rows
  have actions); a wrong 22 GB paste must run its course or the server be
  restarted.
- Download slot starvation is bounded by a STALL DEADLINE, not just retries.
  `download_retries` resets on ANY byte progress, so a flaky CDN that
  dribbles/truncates bytes could once hold one of the few (2-4) downloader slots
  for 75+ min (only `_MAX_DOWNLOAD_CONNECTIONS=2500` x the 900s read timeout
  bounded it) — a handful of such files parked every slot and NOTHING reached
  the parsers. `download_stall_seconds` (default 600) gives up when there is no
  NET forward progress (tracked by a `highwater` mark, so a truncate-and-rewrite
  of the same bytes counts as a stall) for that long; the row fails fast with a
  plain-language message, its `.part` is kept for a later retry, and the slot
  frees for good files. A steadily-advancing large download keeps refreshing the
  clock and is never killed. RELATEDLY: a `Range`-ignored full `200` answering a
  resume no longer truncates a large `.part` back to 0 (>`_KEEP_PART_ON_200_BYTES`
  = 64 MB it is skipped and we retry for a real 206) — one stray 200 near the end
  of a multi-GB download used to wipe everything and then fail. Regression-tested
  (`test_stall_deadline_fails_fast_and_keeps_partial`,
  `test_range_ignored_200_keeps_large_partial`). The stall deadline only fires on
  NO net progress, so a download TRICKLING in at tens of KB/s advances forever
  and never trips it — yet holds a slot for hours ("694 min downloading, still
  blocking ingestion"). `download_max_seconds` (default 3h) is a HARD wall-clock
  cap on one download, enforced at the retry loop top AND mid-stream (a single
  slow attempt stays inside the iter_bytes loop for hours and would never reach
  the loop-top check): past it the file is set aside with its .part kept
  (retryable) so the slot frees. Regression-tested
  (`test_wall_clock_cap_sets_aside_a_too_slow_download`). Still open: no fair
  scheduling —
  `_claim_next` is plain `ORDER BY id`, so a cluster of flaky low-id rows is
  re-tried before fresh files; deprioritizing rows that have already burned
  connections would let good files jump the queue.
- Orphaned .part files: skipping a row whose download had started leaves its
  .part in data/downloads forever (no sweep ties .parts to skipped rows).
- The Files tab's actionable child window is the newest 500 rows; oversize
  rows beyond it are reachable only via the auto-requeue-on-raised-limit path.
- Threadpool: tokens raised to 100 and hour-long background work moved off
  the request pool, but there is still no per-endpoint bound on concurrent
  heavy computes, and during a rollup the global SET threads squeezes reader
  parallelism (slow polls can still stack under extreme load).
- Windows unlink/replace vs long readers: replace budget is ~90s and forget
  now fails HONESTLY when a reader holds the part; the full fix (persistent
  pending-delete tombstones + rates-view anti-join) is designed but unbuilt.
- Provider-reference memory amplification: `load_provider_refs(payer)` is
  materialized per-file in the main process AND pickled into each worker, so a
  ref-heavy payer (UHC/Highmark) parsed by 8 concurrent workers holds up to
  ~16 copies of a large refs dict — a swap risk on a 32 GB box. The parse
  itself streams (bounded); only the refs dict amplifies. Mitigation today:
  lower `parallel_ingests` for a ref-heavy grind. Fix (share one refs dict
  across the main-process threads; scope to files that actually cite refs) is
  deferred. A hung-but-alive worker is now force-killed after 60 min
  both-frozen (`ParsePoolManager.force_heal`), so this can no longer become a
  permanent processor-thread stall.
- Auto-spill relocates rollup scratch off an HDD only on Windows WITH a roomy
  SSD (PowerShell Get-PhysicalDisk probe). On an all-HDD box the rollup and the
  parser reads contend on one spindle — SLOW during a rebuild, not a stall.
- Full-store scale (300M+ rate rows) — TIME, not correctness or memory. Two
  paths are O(partitions × total_rows) because the hash-partition predicate is
  not pushed into the scan, so every slice re-reads the whole table:
  - **C1 — a FULL rollup rebuild** (`rebuild_rollups()` / `rollup_needs_full`
    drift / a schema migration) re-aggregates all rows per partition. At ~300M
    rows this is hours, not minutes. It is NOT hit on the normal grind: new
    files take the payer-scoped incremental path (`update_rollups_incremental`),
    whose per-slice memory and time are bounded by the SLICE, invariant to total
    store size. C1 only fires on an explicit rebuild, a >25%-of-TINs drift, or a
    version bump — all rare and all logged. Memory stays capped the whole time
    (the OOM ladder subdivides 1→64); it is slow, never a crash. (The clustering
    ORDER BY adds a bounded, spillable sort per slice — modest extra wall-clock
    on this already-slow path, and it makes every subsequent dashboard query
    prune, so it pays for itself many times over.)
  - **C2 — `forget` / a removal** keeps the rate spine payer-scoped and fast but
    rebuilds the tin_directory in FULL, because a removed file can change which
    practices are shared across the survivors (a directory row is correct only
    against the whole surviving store). At 300M rows that directory rebuild is a
    long operation (tens of minutes to hours), logged honestly at start
    ("refreshing the full name directory"). Removals are rare by design. The
    scoped-forget fix (recompute only the directory rows for TINs the removed
    file actually touched, then re-check just those for lost sharing) is designed
    but unbuilt — build it if removals become routine at book scale.
  Both are graceful (bounded memory, honest logs, correct results); the cost is
  wall-clock on operations that don't happen during a normal ingest grind.


- Renderer click-through handles single-click gates ("View Plan List");
  multi-step forms (state pickers, searches) are still browser-only —
  Premera, HCSC, IBX, Capital, several state Blues. Excellus publishes
  allowed-amounts only (no negotiated rates) — cataloged, not a bug.
- The UHC blobs listing caps at `max_toc_files` (alphabetical) — deeper reach
  needs a raised cap or targeted pastes; state network files follow the
  `*-Provider-Network_*EXGN_in-network` naming.
- `source_files` in rollups is a representative filename + `source_count`
  (full list is derivable from raw `rates` on demand) — a deliberate
  scalability trade, documented in the commit history.
- Monthly refresh: indexes are dated; re-adding them re-queues only new
  content (URL + content dedup). A scheduler (cron `mrfx add --known`) is the
  natural next feature.
- NPPES enrichment is best-effort/background; TIN display names roll up from
  NPI org names and can lag ingest.

## Step 9 — Ground rules carried from the build

Commit style: what + why + verified-with-numbers. Never weaken TLS. Never
push to a branch other than the designated one. Big claims require a live
run; "should work" is not done. When a stress test finds a failure, fix the
class, not the instance, and leave a regression test behind.
