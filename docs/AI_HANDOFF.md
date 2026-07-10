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
| `mrfx/config.py` | Pydantic config (`config/mrfx.yaml`). Key knobs: `codes.cpt_codes` (the target set; `all_codes: true` disables filtering), `confirm_over_gb` (download size guard, default 5), `max_toc_files` (children per index, default 2000), `parallel_ingests` (parser processes, shipped 3, clamped to cores−1), `delete_raw_after_ingest`. |
| `mrfx/sniff.py` | Header-window (1 MB) classification of any file: `in_network / provider_reference / toc / allowed_amounts / blob_listing / unknown`; `open_stream` (gzip/zip/plain + byte-progress); preflight verdicts. |
| `mrfx/parser.py` | Streaming ijson parser. `InNetworkParser` (event-level, constant memory, batches rows to a sink), `skim_needed_ref_ids` (pass 1 of big files: returns target-cited ref ids + count of target-code items), provider-reference file parser, GP/GO/GN modifier-first discipline attribution, TIN/NPI pairing rules. |
| `mrfx/ingest.py` | `ingest_file` orchestration: quarantines non-rate files, two-pass chunked ingest for files ≥1.5 GB uncompressed (`LARGE_FILE_UNCOMPRESSED_BYTES`), scan short-circuit when pass 1 finds zero target codes, chunk progress (`_ProgressTracker`), QA report, parallel worker (`_parse_worker`, `ParsePoolManager`) — workers are PURE (no DB access). |
| `mrfx/store.py` | DuckDB + Parquet parts. One parquet part per source file (re-ingest = atomic replace = idempotent). Materialized rollups: `rates_by_tin_tbl` (the spine), `tin_directory_tbl`; `rates_dedup` is a LIVE VIEW (see invariant 6). `url_queue` table drives URL ingestion. SSN masking. All writes behind one in-process lock. |
| `mrfx/fetch.py` | URL-drop pipeline: `download` (retry/backoff, atomic `.part`, HTTP-Range resume, sha256 while streaming, disk-space guard, TLS incl. AIA chain repair), `dedup_key` (volatile signed-params stripped), `expand_toc`, `expand_blobs_listing` (UHC/Optum), `extract_links_from_page`, `crawl_gatsby_hub` (Sapphire hubs), `probe_blobs_api`, `process_url_record` (route one URL), `run_queue` (downloader thread + N processor threads + rollup batching). |
| `mrfx/render.py` | OPTIONAL headless-Chromium fallback for JavaScript-only portals: renders the page, harvests links from the DOM + captured JSON responses, and when nothing appears dismisses consent overlays and clicks MRF-looking controls ('View Plan List' — Harvard Pilgrim) with a re-harvest; the browser never networks itself — every request is fetched by httpx (proxy/CA-aware, TLS verified) and fulfilled into the page. Degrades to a help message without Playwright. |
| `mrfx/known_sources.py` + `config/known_sources.yaml` | The catalog of live-verified payer entry points with results; `{FIRST_OF_MONTH}` placeholder resolution. `config/starter_links.txt` is the paste-ready export of it. |
| `mrfx/api.py` | FastAPI JSON API + static SPA. `/api/rates` (tin/npi/entity grains, server-side paging), benchmarks, exports (+methodology sidecars), outreach CSV, `/api/urls*`, `/api/known-sources`. |
| `mrfx/web/` | Vanilla-JS SPA (no CDN deps): Explorer, Code comparison, Benchmark, Files (paste-links card + queue), Sources. |
| `mrfx/cli.py` | `mrfx serve / add / preflight / ingest / status / export / outreach / reset`. `add` hands URLs to a running server, else drains locally. |

## Step 3 — Internalize the invariants (violating these breaks real users)

1. **Streaming only.** No `json.load` of an MRF, ever. Peak memory must not
   scale with file size (verified: ~34 GB uncompressed at <1 GB RSS).
2. **Single DB writer.** Only the main process touches DuckDB. Parallel
   workers write their own `*.parquet.tmp` and hand back metadata. Temp files
   must never match the `*.parquet` view glob.
3. **Fault isolation.** A bad file/URL marks ITS row failed with a
   plain-language message and the worker moves on. Worker loops survive
   transient store errors (lock conflicts retry ~6s in `Store.connect`).
   A dead parser process heals (`ParsePoolManager`) and the file retries once.
4. **Honesty contract.** Never fabricate: unverifiable registry links are
   badged unverified; drug/allowed-amounts files are skipped with the reason;
   0-row files stay 0 honestly; per-file payer attribution comes from inside
   the file (Blues cross-host each other's files — expected, documented);
   every export carries a methodology sidecar; SSN-pattern TINs are masked.
5. **Idempotency / crash-resume.** Kill anything at any time: `.part`
   downloads resume via HTTP Range guarded by an If-Range validator (`.val`
   sidecar — republished content restarts instead of splicing); in-flight
   queue rows AND files-table 'processing' rows recover on restart
   (`recover_stuck_urls` / `recover_stuck_files` — the latter runs at CLI
   startup, before any worker thread, so it never races a live ingest);
   re-ingest atomically replaces that file's part — never duplicates rows.
   Content-sha dedup skips byte-identical files from other domains; the
   tie-break is asymmetric on row id, so two twins racing each other
   resolve one-proceeds/one-skips. A skip against a still-*ingesting* twin
   keeps its downloaded bytes and is auto-revived (skipped→queued) if that
   twin later fails. Known residual: manually retrying a *failed* lower-id
   row while its higher-id twin is mid-ingest can double-parse — wasted
   work, not duplicated rows, because per-file parts replace atomically.
   `mrfx reset` clears the url_queue with the data — done-row anchors must
   not outlive the store they anchor.
6. **Rollup scalability.** `rates_by_tin` / `tin_directory` are materialized
   (few groups); `rates_dedup` MUST remain a live view — at NPI×rate grain a
   30M-row store means a ~30M-group aggregation whose spill exceeded 27 GB of
   disk when it was materialized. No `string_agg(DISTINCT …)` in rollups
   (cannot spill); DuckDB `memory_limit` is 40% RAM clamped [2,12] GB.
   Rollup rebuilds are batched (`ROLLUP_BATCH_FILES`), give up after 3
   failures (raw data is safe), and never run per-file during queue grinds.
   Above ~15M raw rows they build in hash-partitioned slices (the partition
   column is in every GROUP BY key — slice-union ≡ single shot) inside ONE
   transaction: a mid-slice failure rolls back to the previous complete
   tables, never a partial one. Each connection caps `max_temp_directory_
   size` at 80% of free disk so a rollup can't starve unrelated work.
   A rollup failure after the parquet part is durable does NOT fail the
   file (`_rebuild_rollups_best_effort`).

## Step 4 — Know the URL pipeline states

`url_queue.status`: `queued → downloading → fetched → (expanding|ingesting) →
done | failed | skipped`. `kind`: `toc / page / in_network /
provider_reference / allowed_amounts / duplicate / unknown`.
User actions are guarded: retry only from failed/skipped; skip only from
queued/failed (a done row anchors content-sha dedup and is untouchable).
`dedup_key` strips signature/expiry query params but keeps identity params.

## Step 5 — Performance envelope (measured)

- Parse ≈ 30–35 s per uncompressed GB per pass (ijson C backend; this IS the
  bottleneck — 95% of processing; isal/gzip and buffer tuning were measured
  and rejected as noise).
- Two-pass for ≥1.5 GB uncompressed; pass 1 skims target-cited refs (80k ids
  on an 8 GB Anthem shard vs 4.2M total); short-circuits pass 2 if zero
  target codes.
- Parallel: N worker processes (`parallel_ingests`), downloads prefetch
  (`fetched` state) so the network overlaps parsing. 3 files, serial 124 s →
  2 workers 83 s, byte-identical outputs.
- Reference yields: UHC MO network 30 MB → 88,649 rows; Oxford 0.58 GB →
  5.59M; Heritage 1.7 GB → 9.83M; PS1-77 3.39 GB → 29.69M (70 s rollup at
  6.3 GB peak after the live-view fix); BCBSLA 2.8 GB unc → 14.6M.

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

- `.venv/bin/python -m pytest tests/ -q` — the suite (116+ tests) runs real
  end-to-end drains against local HTTP servers, including parallel mode,
  kill-recovery semantics, dedup, guards, and messy-file parser cases
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

`config/known_sources.yaml` holds 33 auto-queueable sources + ~20 probed
portals, each with verified dates and real row counts — treat it as ground
truth for "does this source work and what should it yield". The largest
verified single-file ingests: Cigna CHLIC 243.4M rows, Aetna CA 83.7M, UHC
Charter 617 64.0M (8.85 GB gz), NC PPO 23.7M, upper-midwest zip 19.8M.
Guards verified live: 35.2 GB refused by confirm_over_gb, 25.1 GB by the
disk-space guard, both with actionable messages.

## Step 8 — Known limitations / next frontiers

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
