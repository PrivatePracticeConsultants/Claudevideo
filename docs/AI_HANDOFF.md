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
| `mrfx/config.py` | Pydantic config (`config/mrfx.yaml`). Key knobs: `codes.cpt_codes` (the target set; `all_codes: true` disables filtering), `confirm_over_gb` (download size guard, default 5), `max_toc_files` (children per index, default 2000), `parallel_ingests` (parser processes; **0 = auto = cores−1 capped 8**, positive value clamped to cores−1 — via `resolve_worker_count`), `parallel_downloads` (concurrent fetchers; **0 = auto = 6, independent of core count** — downloads are network-bound, so sizing them by cores throttled the slow-CDN case; via `resolve_download_count`; `1` = strictly sequential), `download_segments` (byte-range lanes within ONE file; 1 = off, max 8 — for a queue of a few very large files on a per-connection-throttling CDN), `download_timeout_seconds` (**max SILENCE between bytes, not a total cap; default 120**, was 900 — measured: time-to-detect a CDN that accepts a connection then goes quiet tracks this value exactly, so 900 parked a scarce downloader slot for 15 min per episode; on expiry it reconnects and resumes from the .part via Range, losing nothing), `delete_raw_after_ingest`, `render_js` / `render_auto_install` (JS-portal headless browser + auto-download of Chromium), `enrichment.mode` (`api`/`bulk`/`off`) + `enrichment.api_concurrency` (default 8). Any typo raises one friendly `ConfigFileError`; unknown keys warn. |
| `mrfx/sniff.py` | Header-window (1 MB) classification of any file: `in_network / provider_reference / toc / allowed_amounts / blob_listing / unknown`; `open_stream` (gzip/zip/plain + byte-progress); preflight verdicts. |
| `mrfx/parser.py` | Streaming ijson parser. `InNetworkParser` (event-level, constant memory, batches rows to a sink), `skim_needed_ref_ids` (pass 1 of big files: returns target-cited ref ids + count of target-code items), provider-reference file parser, GP/GO/GN modifier-first discipline attribution, TIN/NPI pairing rules. |
| `mrfx/modifiers.py` | THE modifier-field rule, shared by the parser (both paths), the one-time rewrite of older parts (`Store._migrate_row_rules`, keyed on `files.row_rules` / `store.ROW_RULES_VERSION`), the Explorer's `modifier=base` filter and `benchmark._market_where`. Measured round 3: BCBS Alabama publishes place-of-service codes in the modifier field (`21|22|24|31`, `10|11|12|32`, with `service_code: ["01"]`) — an all-2-digit list containing a number no CPT modifier uses is moved to `service_code` (union, nothing dropped); BCBS ND writes `"52,53"` as one element — split on `,` `;` whitespace; First Choice's `26|NU|RR|TC|UE` lines carry DIFFERENT rates (60/61/80 vs bare 28–67) so they stay as published and simply aren't base. Base = `''` or only GP/GO/GN (+CQ/CO with include_assistant); KX/59/X{EPSU}/52/22/26/TC are not base. A rule change bumps `store.ROW_RULES_VERSION`; the rewrite is a pure function of the stored columns, verified row-identical to a fresh re-ingest on 32.1M real rows. |
| `mrfx/ingest.py` | `ingest_file` orchestration: quarantines non-rate files, two-pass chunked ingest for files ≥1.5 GB uncompressed (`LARGE_FILE_UNCOMPRESSED_BYTES`), scan short-circuit when pass 1 finds zero target codes, chunk progress (`_ProgressTracker`), QA report, parallel worker (`_parse_worker`, `ParsePoolManager`) — workers are PURE (no DB access). |
| `mrfx/store.py` | DuckDB + Parquet parts (zstd-compressed via `PARQUET_COMPRESSION`; mixed snappy/zstd parts coexist, no migration). One parquet part per source file (re-ingest = atomic replace = idempotent). Materialized rollups: `rates_by_tin_tbl` (the spine: `BY_TIN_QUERY` + `is_latest`, TRUE on each contract's newest month — `LATEST_PARTITION` = payer, tin_value, tin_is_really_npi, billing_code. Each build slice statement (full build or payer delta) derives its own contracts' newest month in a CTE — exact because no slice splits a contract; full rebuild at 99M rates 577 s -> 619 s, a payer delta unchanged. **DuckDB 1.5.5/1.5.6 bug the build now avoids (round 5):** after an EMPTY `CREATE TABLE … AS (… WHERE FALSE)`, rows an `INSERT … ORDER BY` of more than one row group (~123k) adds in the same transaction are invisible to every connection, even after COMMIT, until a CHECKPOINT; the build's CHECKPOINT fails whenever another connection has a write open, and the rebuilt spine then read EMPTY on every screen. The sliced build now creates the table from explicit columns (`DESCRIBE`) — `tests/mrfx/test_rollup_visibility.py` (0 of 308,104 rows visible before the fix). Fixed upstream in DuckDB 2.0.0. An older spine gains the column in place at open (`_migrate_latest_flag` → `_mark_latest`, on committed rows; 79M rows in 5.6 s); a store with no table yet serves `SPINE_LIVE_QUERY`, whose flag is the window itself; a spine missing the column gets it from the VIEW per query), `tin_directory_tbl`; `rates_dedup` is a LIVE VIEW (see invariant 6). `url_queue` table drives URL ingestion. `enrichment_progress` (cached 60s; the rate-NPI population is kept PER PART in `rate_npi_set`/`rate_npi_parts` — a new file costs one scan of that file, outside the writer lock; round 4's whole-store `rate_npis` rebuilt under the lock, 80 s at 99M rates, and is gone) + `store_stats` (spine counts held per `data_generation`) + `available_states` back the dashboard's name/state UI. SSN masking. All writes behind one in-process lock. |
| `mrfx/fetch.py` | URL-drop pipeline: `download` (retry/backoff, atomic `.part`, HTTP-Range resume, sha256 while streaming, disk-space guard, TLS incl. AIA chain repair), `dedup_key` (volatile signed-params stripped), `expand_toc`, `expand_blobs_listing` (UHC/Optum), `extract_links_from_page`, `crawl_gatsby_hub` (Sapphire hubs), `probe_blobs_api`, `process_url_record` (route one URL), `run_queue` (N downloader threads — `parallel_downloads`, 0=auto=6, concurrent fetches reserve remaining bytes so they never collectively overcommit the disk — + N processor threads + rollup batching). |
| `mrfx/render.py` | OPTIONAL headless-Chromium fallback for JavaScript-only portals: renders the page, harvests links from the DOM + captured JSON responses, and when nothing appears dismisses consent overlays and clicks MRF-looking controls ('View Plan List' — Harvard Pilgrim) with a re-harvest; the browser never networks itself — every request is fetched by httpx (proxy/CA-aware, TLS verified) and fulfilled into the page. Degrades to a help message without Playwright. |
| `mrfx/known_sources.py` + `config/known_sources.yaml` | The catalog of live-verified payer entry points with results; `{FIRST_OF_MONTH}` placeholder resolution. `config/starter_links.txt` is the paste-ready export of it. |
| `mrfx/api.py` | FastAPI JSON API + static SPA. `/api/rates` (tin/npi/entity grains, server-side paging), benchmarks, `/api/schedule/*` + `/api/report/*`, exports (+methodology sidecars), outreach CSV, `/api/urls*`, `/api/known-sources`. Round 3 rules: `FilterSet` puts every clause in a bucket (key / unit / ident / row) and `rel_sql` picks "latest" over key+unit+ident-filtered rows only, row filters after (a rate/modifier filter must never resurrect a superseded month); the latest partition carries `tin_is_really_npi`. Entity grain: each member TIN at ITS newest month, state/city/therapy filters per member, key = manual map, else organization (NPI-2) name + main state (`tin_directory.primary_state`), else the TIN itself (a TIN named after a clinician is its own entity — real store: 462 person-names shared by 1,008 TINs). `practice_basis()` = the Benchmark's basis for the payer leaderboard and by-code strip; the Explorer discipline filter includes untagged (`unspecified`) lines like the Benchmark. Round 4 (speed, no number moved): TIN/entity "latest" is the spine's stored `is_latest` (`_TIN_REL_LATEST`; only the NPI grain keeps a window); `stat_rel()` feeds the aggregate views (summary strip + by-code, Code-page histogram + payer leaderboard, trend) straight off the bare spine when no filter needs a joined column (`uses_dim_cols`); the table's page-first late join (`_tin_late_join_ok`) now covers "latest"; `_entity_rel` takes its DISTINCT counts/lists from one `list()` per column (four DISTINCT aggregates ran the whole-spine entity view out of memory); `order_sql(src="src.")` makes the export sort on the RAW columns (the masked-TIN alias made SSN-pattern rows tie). Round 5: a SELECTIVE practice-attribute filter (state, city, a name or partial-ID search — "therapy only" alone keeps most of a therapy store and stays a join filter, `FilterSet.selective_dims`) at the TIN/entity grain no longer joins the three dimension tables onto every spine row before testing them — `_tin_rows_by_unit` computes the practice columns once per (tin_value, tin_is_really_npi) from `_UNIT_COLS` (the SAME expressions the projection uses), keeps the passing practices, and SEMI JOINs their rows (`tests/mrfx/test_unit_filters.py` pins it against the old join-then-filter form; an exact 9/10-digit ID alone stays a plain spine predicate). The summary strip off the bare spine is cached for its `data_generation` (no TTL; the Medicare % is applied per request by `_with_anchor`), a joined view keeps the 30 s `SUMMARY_TTL_SECONDS`. |
| `mrfx/sqlfn.py` | Shared SQL pieces. "The ZIP a practice sits at" (`modal_zip_by_tin`, `modal_zip_of`, `zip_counts_by_tin`): the ZIP its NPIs' rate rows most often carry, ties to the LOWEST ZIP — DuckDB's `mode()` broke ties by thread arrival (a practice split over four ZIPs got a different 15-mile market per call); counted per (TIN, ZIP) then picked, the cost of plain `mode()` (an ordered `mode(x ORDER BY x)` doubled it). Used by leverage, territory, dossier; other `mode()` calls carry `ORDER BY` (lint in `test_output_speed.py`). |
| `mrfx/benchmark.py` | Market percentiles, opportunity model, payer-negotiation one-pager, pitch report; shared `_market_where` / `resolve_subject_tins` / `_brand_header`. Reports refuse without a pinned month + state (or explicit `allow_national`). |
| **Percentile conventions** (read before touching any position number) | The Benchmark tab's `subject_percentile` is MID-RANK — strictly below plus HALF the ties — computed in `benchmark.py`'s `position` CTE, and both comparisons round the peer rate to 2dp first (subject_rate is already rounded; an exact `=` misses real ties like 33.325 vs a displayed 33.33). Mid-rank is the only convention consistent with the `quantile_cont` p10..p90 columns shown on the same row: a subject whose rate equals the peer median scores exactly 50. It replaced at-or-below (`<=`), which counted the subject's own ties in full and OVERSTATED position by up to ~25 points on real tie-heavy data (payers publish identical fee schedules to many practices) — always upward, i.e. making clients look better paid and shrinking the negotiating gap. Leads, the leaderboard, the size premium (`quality.py`, partitioned by payer x code) and the outreach PCTL merge field use the SAME mid-rank among the OTHER practices (`leads.MIDRANK_PCT` / `midrank_pct_sql(partition)`): the cheapest of a thin market still sits at 0 (so the finder still surfaces it) and a flat fee schedule reads p50 everywhere. Leads and the leaderboard count only codes with ≥2 practices AND ≥2 distinct rates (an all-tied code ranks no one); the worst-payer gap uses the same code set (audit 8). `percent_rank()` is no longer used for any printed position. |
| **Plan / network dimension** (`plan_index` + `spine_relation`) | A payer's rate FILE never names a plan; the mapping lives only in its table-of-contents, so `expand_toc_with_plans` harvests `reporting_plans[]` per structure item and `store.save_plan_index` records it keyed on the URL's `dedup_key` (many-to-many on purpose — one file commonly serves many plans). A plan scope is NOT a WHERE clause: the prebuilt spine has already aggregated ACROSS files, so a TIN priced 28 in the narrow book and 40 in the broad one is stored once at their median (34) with only a representative `source_file`, and no filter over that row can recover 28. `benchmark.spine_relation` therefore rebuilds the spine from the raw rates of the plan's files using `store.BY_TIN_QUERY` verbatim — same aggregation, smaller input — so a plan-scoped number is computed exactly like the pooled one (verified: narrow 28.00, broad 40.00, pooled 34.00 on the same seed). Every `_market_where` caller must take its relation from `spine_relation`/`_rates_relation` or the scope is silently ignored; `monitor.py`'s two literal `rates_by_tin` reads were converted for this. An unknown plan REFUSES rather than falling back to the pooled market, `public_market()` keeps the resolved file list out of every report, and a plan-scoped methodology footer lists only that plan's files. `plan_coverage()` flags payers publishing more than one plan — the blend the dimension exists to expose — and the Benchmark tab's picker appears only when the store carries plan tags. |
| `mrfx/schedule.py` | Fee-schedule reconstruction ("your rate card") + payer scorecard ("who pays best"), reusing benchmark's market filter so numbers agree. Scorecard ranks by median % of Medicare (MPFS loaded) else median % of best payer over head-to-head codes. |
| `mrfx/web/` | Vanilla-JS SPA (no CDN deps): Overview, Explorer, Code comparison, Markets, Benchmark, Negotiate, Rate card, Medicare (eligibility + referral structure + the ZIP/radius referral-leaders ranking, once `mrfx medicare` has imported them), Clients (watchlist digest), Leads, Payer roster, Changes, Files (paste-links card + queue), Sources. |
| `mrfx/medicare.py` | The two CMS layers MRFs cannot supply: Order & Referring eligibility (a snapshot REPLACES, in one transaction, and is diffed against the previous one — `medicare_orf_lost` drives the "lost Part B" flag) and shared-patient referral pairs (streamed by DuckDB to `referrals/<dataset_id>.parquet`, keeping only pairs touching this store's NPIs — which is also the referral-leaders ranking's coverage boundary, stated on every result via LEADERS_COVERAGE_NOTE: a practice with no store connection is absent, not ranked low). **`dataset_id` = format+year+day-window is the vintage identity; never blend two of them** — CMS publishes one year at several windows, and `active_dataset` refuses an ambiguous `year=`. `is_valid_npi` (Luhn over the 80840 prefix) keeps phone numbers out of the batch check. |
| `mrfx/contracts.py` | Renewal radar + new-to-network. `expiration_date` is published inconsistently (blank / 9999 / 2099 / already-past), so `expiration_coverage` is the honesty gate every radar answer carries — a contract with no USABLE date is absent, never "no renewal". New-to-network compares each payer only against months THAT payer actually published, so an un-ingested month cannot fake a wave of new contracts. |
| `mrfx/remits.py` | Underpayment check: remit lines vs the payer's published rate for that practice. Parses headered CSV/TSV or loose lines; unreadable lines and unpriceable codes are REPORTED, never silently dropped or counted as fine. Every result states whether the pasted amounts were ALLOWED or PAYER-PAID (the most common misreading) and carries the MPPR / assistant-modifier / cost-share caveat: a flag is a question for the payer, not a proven underpayment. |
| `mrfx/engagements.py` | Win-tracking. A baseline is a STORED snapshot (never recomputed — that would let a later ingest rewrite history); the comparison drops the baseline's month but keeps its market basis, so a changed filter can't masquerade as a win, and peer medians travel alongside so a rising tide isn't sold as negotiation. |
| `mrfx/packets.py` | Monthly client packets. Fault-isolated per client AND per section; an omitted section is listed with its reason (an empty branded document is worse than none). Pins a CONCRETE month, never "latest", so a packet is unambiguous months later. |
| `mrfx/clients.py` | Client watchlist + digest: composes compute_rate_changes / contract_gaps / org_referrals+recent_losses per saved client, fault-isolated per client AND per section. Never computes new numbers — a digest cell must equal its drill-down. |
| `mrfx/mpfs.py` | Locality-adjusted MPFS import from the official CMS RVU bundle (PPRRVU+GPCI zip): (work x GPCIw + nonfacPE x GPCIpe + MP x GPCImp) x CF, status-A unmodified rows only, columns matched by tokens (names drift by year). The conversion factor is a REQUIRED validated input (it is not in the files and changes some years mid-year) and the locality + CF are stored in the source string every methodology prints. Simple code,rate CSV path unchanged. |
| `mrfx/utilization.py` | CMS Physician & Other Practitioners PUF import (per NPI x HCPCS Medicare volumes). DuckDB streams the ~10M-row national CSV, keeping only the therapy code catalog; columns are token-matched because CMS renamed every one of them between the 2013 and 2020 layouts. Supplies `suggested_volumes()` — what the "Fill from Medicare" buttons put in the volumes boxes — and practice sizing for prospects. HONESTY: Medicare FFS only, CMS suppresses <11-beneficiary rows, ~2-year lag, so every answer carries the FLOOR note; an optional multiplier is echoed back as an explicit assumption. Office place-of-service only by default (a facility row isn't what the non-facility contract prices). |
| `mrfx/demographics.py` | Census ACS 5-year ZCTA import (population, 65+, median household income) + `market_sizing()` (seniors per therapy practice inside a radius). Reads both the variable-id export (`B01001_020E`) and the human-labeled one, summing the 65+ buckets; parses the `86000US63103` GEO_ID form (taking the first 5-digit run would file every row under ZIP "86000"). Suppression markers (`-`, `(X)`, negative annotations) are NULL, never 0. Practice counts come from the same TIN-grain directory the ZIP search uses, so the ratio is an upper bound and says so. |
| `mrfx/catalog.py` (therapy classification) | `therapy_taxonomy_sql` tests the PRIMARY taxonomy **and** the pipe-joined `taxonomy_codes` list. NPPES allows 15 taxonomies and flags only one primary, so a real therapy clinic can carry its therapy code in a secondary slot — measured against the LIVE registry across five Missouri ZIPs, 10 of 109 therapy providers (9%) did not carry it as primary, including a clinic named "APEX PHYSICAL THERAPY, LLC" whose primary is the generic 174400000X 'Specialist'. Both enrichment paths (API: all `taxonomies[]`; bulk: `Healthcare Provider Taxonomy Code_1..15`) populate the list, and the NPPES cache schema was bumped so an existing cache rebuilds once. `all_col=None` degrades to primary-only for a relation that genuinely lacks the column (an old cache) — never a binder error. |
| `mrfx/nppes.py` | Brand-new-clinic feed: therapy NPIs ISSUED in the last N days, read from the NPPES bulk cache `enrich.py` already builds (enumeration_date was added to `_NPPES_CACHE_COLS`; `_NPPES_CACHE_SCHEMA` forces a one-time rebuild so an unchanged download doesn't keep serving a column-less cache). Practices already carrying published rates are flagged `in_store` (later-stage lead), unparseable dates EXCLUDE a row from a recency filter rather than passing as "new", and ZIPs without a Census centroid are reported as `unplaced`. |
| `tests/mrfx/test_determinism.py` | Two invariants that only show up under repetition. **(a) Tied ORDER BY.** DuckDB scans in parallel, so a ranked list whose sort key ties comes back in whichever order finished first — the payer concentration table, the rate-type mix, the payer posture card and therefore the MARKET REPORT reordered between two generations off an unchanged store, which is unacceptable in a document the user sells. Every ranked query now carries a unique tiebreaker; the test seeds three payers on identical rates and two cities on one median and asserts four identical answers. **(b) The `td` alias rule.** `_market_where` emits `list_contains(td.states, ?)` for a state filter, so any relation it is pasted into MUST join `tin_directory` as exactly `td` — `expiration_coverage` joined nothing and `renewal_radar` joined it as `d`, so a state-filtered renewal radar raised a DuckDB BinderException (a 500, not a refusal). Reachable via the API/CLI; the dashboard sends no state on that call. **(c) Round 4, found by running every endpoint twice on the real store:** `mode()` breaks a tie by thread arrival (4 answers in 8 runs) — a practice's ZIP/city/state placement moved between calls; every `mode(x)` is now `mode(x ORDER BY x)` (`test_output_speed.py` lints for it). Also the subject picker's starter list, new-to-network, leverage and renewals lists, and the export's masked-alias sort. |
| Tooling gates (`pyproject.toml`, `requirements.txt`) | The repo had no linter, no type checker, no coverage and no CVE scan; correctness was defended entirely by the end-to-end suite and the audit harnesses. Two of those gaps are now closed. **`ruff`** is configured NARROWLY on purpose: the first full run produced 278 findings and exactly ONE real defect (dead assignments in `engagements.py`), so the selection is E9/F/PLE + three targeted bugbear rules and is expected to stay at zero — every excluded rule is listed in pyproject WITH the reason it was rejected (T20: the CLI's prints ARE its interface; B008: FastAPI's `Body(...)` default idiom; B905: a DuckDB cursor's `zip(cols, row)` cannot mismatch; B023: all five flagged closures were checked and never escape their iteration; S110: try/except/pass around cosmetic work IS invariant 3). **`pip-audit`** found 25 known CVEs across 5 pinned packages that had never been refreshed — `cryptography` (TLS, and this app fetches from third-party CDNs), `starlette` + `python-multipart` (the web layer and its upload parser), `pyarrow`, `pytest`. All cleared; `starlette` is now pinned EXPLICITLY rather than left to fastapi's resolution, since the web layer's CVEs land there. The starlette 1.x upgrade also forced migrating off `@app.on_event("startup")` (deprecated, slated for removal) to a `lifespan` handler — verified under uvicorn, not just TestClient, because a silently-skipped startup task is worse than the warning it replaced. NOT closed: no type checker and no coverage measurement. |
| `mrfx/states.py` | The single answer to "what state is this?". Every state-scoped feature used to do `str(v).strip().upper()[:2]` and then check the length — which TRUNCATES instead of validating, so "MISSOURI" passed as "MI" and a user who typed the name out would have been shown **Michigan's** schedules, hospitals and peers under a Missouri heading. `state_code` accepts a USPS code or a full state name (the person using this types "Missouri") and RAISES on anything else — an unrecognised state is refused, never guessed at, because a wrong answer that looks right is the one failure this app must not produce. `state_code_or_none` is the lenient twin for row data, where one bad cell must not stop a load. `normalize_market` runs it, so the dashboard's state box is normalised once for every downstream feature. |
| `mrfx/floors.py` + `mrfx/spark.py` | **Floors/ceilings**: state Medicaid (the floor — "your commercial rate is 4% above Medicaid" ends an argument) and workers' comp (usually the ceiling). No national machine-readable source exists, so the USER loads a code/rate CSV labelled with state + kind + year, and that label IS the provenance printed with every derived number. A two-letter state is REQUIRED to load and to compare — a fee schedule is a state document, so a national commercial median cannot be compared to one state's Medicaid (refuses). A code the schedule omits is ABSENT from the comparison, never zero (a zero would report an infinite ratio and make a rate look heroic); a code listed twice keeps the MAXIMUM allowable so the import is repeatable. Managed-Medicaid-pays-a-percentage and WC-has-its-own-rules caveats travel with every result. **Sparklines** (`spark.py`) draw a rate's path as inline SVG in the printed deliverables — no library, no CDN, because a report is a self-contained file emailed to a client. Fewer than two points draws NO line (a lone dot styled as a trend is an unsupported claim); direction sets the colour only when there IS a direction; `series_by_code` is cosmetic-tier and returns `{}` on any error rather than costing a deliverable. |
| NPPES weekly incrementals (`enrich.apply_weekly_update`) | NPPES publishes a full file monthly and small incrementals weekly; without them names, taxonomies, enumeration and deactivation dates run up to a month stale — worst for the two feeds built on exactly those columns. A weekly REPLACES the cached row for each NPI it contains and leaves every other row untouched: it is authoritative for what it contains and says nothing about what it omits. Reuses `_write_nppes_parquet` (via its `src` parameter) so a weekly row and a monthly row can never be parsed by two slightly different readers, merges in DuckDB, and swaps in via `replace_with_retry` only once the merged file is complete. Refuses rather than half-applying: no cache to merge into, or a file yielding no NPI rows, both leave the existing cache intact. `mrfx enrich --weekly <path>`. |
| `mrfx/leverage.py` | **Walk-away leverage** — the question that actually decides a negotiation: what happens to the PAYER if this practice leaves. Combines three things that sat on three tabs and were never joined: the payer's roster (its other contracted practices), the ZCTA centroids (how far away they are), and ACS seniors (the demand they'd absorb). `network_leverage` places every practice at the ZIP most of its NPIs share and counts the payer's alternatives inside a radius; `leverage_summary` ranks every payer thinnest-first, fault-isolated per payer. THE RAIL THAT MATTERS: a published rate is a CONTRACT, not proof a practice is open or taking patients, so the count is an UPPER bound on the payer's real fallback — the error runs toward understating the client's leverage, and the note says so in that direction. Explicitly not a network-adequacy finding. |
| `mrfx/visit.py` | **One standard visit, priced per payer** (Rate card tab, `/api/schedule/visit`). A fixed basket of timed codes + minutes; units by the CMS 8-minute rule exactly as Pub. 100-04 ch. 5 sec. 20.2 writes it (total minutes fix the units, whole 15-minute units first, the rest by most minutes left — its five worked examples are tests), with the per-code "rule of eights" count beside it and flagged when it differs. Priced from `compute_fee_schedule` (the rate card's rates); a payer missing any code gets NO total (never $0-priced). Flags the 97140+97530 pair (NCCI ch. XI sec. P.2: different timed intervals + 59/XU). The default basket = the four most-billed timed codes on 2023 all-payer claims, one unit each; presets in `PRESETS`. The number is the CONTRACTED value per visit — the NRPV ceiling; no Medicare total (MPPR needs the PE RVU split the `mpfs` table does not keep). `tests/mrfx/test_standard_visit.py`. |
| `mrfx/visit_market.py` | **Visit value in this market** (Rate card tab, `/api/schedule/visit/market`, `.csv`, `/api/report/visitvalue`, and inside the printable rate card / its CSV when State and Discipline are set). One visit basket priced at every practice (TIN) of ONE state and ONE discipline per payer: practices, mean, median, P25, P75; all payers = each practice's median of its own payer values; an optional subject placed in each spread. Reads the per-discipline materialised medians (`benchmark.DISC_PAYER_PRACTICE_MEDIANS`). Rules in "Round 12 additions"; known answer in `tests/mrfx/test_review12.py`. |
| `mrfx/economics.py` | **Per-visit value + volume-weighted position.** `visit_economics` prices the practice's OWN Medicare code mix per payer, turning "$44 for 97110" into "an Aetna visit is worth $115.54" — the unit an owner's P&L speaks. The mix is RE-NORMALIZED over the codes each payer actually prices, so a payer that simply doesn't publish a code is never made to look cheap by pricing it at zero (`mix_coverage_pct` exposes a thin overlap). Medicare counts SERVICES, not visits, so a per-visit figure needs a units-per-visit divisor that is the USER's assumption, validated (0.5–20) and echoed as one; with none supplied, figures stay per-unit rather than being invented. `weighted_position` weights the headline percentile by the practice's own volumes — and REFUSES below `MIN_WEIGHT_COVERAGE_PCT` (50%) coverage, because weighting a fragment misrepresents the whole. |
| `mrfx/dossier.py` | **Prospect dossier + market report.** Pure recombination: identity, Medicare size, rate position, weakest payer, leverage, referral sources and market demand on ONE branded page for a cold call; and the metro-level story (city rate map, concentration, payer posture, openings/closures, demand) as a document sellable before having a client there. Both are section-fault-isolated and LIST what they could not build with the reason — a branded page with a silently missing section reads as a complete picture when it is not. The dossier REFUSES a subject that is not really in the store (`resolve_subject_tins` falls through to the raw string, so a typo would otherwise render a fully-omitted page a user might send). |
| Growth, service lines, cash anchors | `utilization.practice_growth` compares a practice's Medicare volume across two PUF years, counting ONLY codes present in BOTH — CMS suppression can create or destroy a code line with nothing changing in the practice, so those are listed apart and never booked as movement. `utilization.service_line_gaps` finds codes nearby therapy practices BILL that this one does not (billing behaviour, distinct from contract gaps' published-rate question). `hospital.cash_anchors` surfaces the discounted-cash and gross columns the importer already stored but nothing displayed — a local self-pay anchor for a practice setting its own cash rate, with the reminder that a hospital's facility price should sit ABOVE a private practice's. |
| **Parameter-abuse class + weekly dedupe (second audit round)** | A follow-up audit asked "is everything actually audited" and answered it with a coverage sweep instead of a claim. It found a CLASS the 1,586-call malformed-body fuzzer structurally misses: WELL-FORMED bodies with count-like params at hostile values. Three live 500s (`min_practices<=0` indexed an empty list in `size_premium`; negative `limit` reached DuckDB as a binder error in `local_rate_map` and `steal_share`) plus the same latent bug masked in `hospital_parity`, and a fabrication path (`min_codes<=0` let a zero-rankable payer be verdicted "one rate for everyone"). Class fix: `_bounded_int` at every wire site in api.py AND clamps inside each compute function (CLI callers covered too); `scratchpad/adv_param_abuse.py` now sweeps every POST route from the app's own route table (2,964 calls, 0 5xx) so a new endpoint is attacked without editing the harness. Separately: `apply_weekly_update` trusted the weekly file to be internally unique — a duplicated NPI left TWO cache rows and every join double-counted that provider. The merge now dedupes the weekly side keeping the file's LATER occurrence (`file_row_number`, later row = later update), and the summary counts NPIs, not raw rows. Also `sparkline()` drops non-finite values before building the SVG path (NaN/inf wrote literal "nan" into `d=` — a broken picture in a client report). All regression-tested. |
| **Gap closures against the original nine-item analysis** | Six items were delivered as engines but not at the surface the ask named, and one reversed it. **(2) Payer posture** — `quality.payer_posture` splits `negotiated` from `fee schedule` and badges each payer "negotiates" vs "one schedule for everyone". The first cut had folded both into `CONTRACTED_TYPES`, erasing the exact distinction: those two values are both contracted amounts but say OPPOSITE things about whether a negotiation is winnable. It reports the payer's CLAIM (the field) beside the OBSERVATION (does that payer actually price practices differently), and when they disagree it says so instead of picking one; under `min_codes` shared codes it refuses to judge at all. **(3) Freshness** now rides on the payer roster and, as `benchmark["stale_payers"]`, into the pitch report as a publication-age banner. **(5) Real-terms** reaches the pitch report (`_erosion_block`) and `compare_to_baseline`'s `real_terms` (a win restated in baseline dollars). **(6) Sparklines** reach the rate card and the packet's month page, not just the pitch report. **(7) `radius_rate_map`** answers the ask's actual question — same payer, same code, 30 miles apart — by placing each practice at its own ZIP's centroid (city names cannot: a metro's edge is 40 miles from its centre). Band edges are CLIPPED AND EXTENDED to the requested radius and `unbanded` is asserted to be 0, after a test caught practices inside the radius but past the last fixed edge silently vanishing from the table. **(9) `_add_miles`** puts distance on every steal-share row so a call list is a route, not just a ranking. |
| `mrfx/territory.py` | Three market-structure answers below the state line. **`local_rate_map`** — one code's median by CITY inside ONE state (a state is REQUIRED: pooling cities across states buries the far larger state effect), same `MIN_CELL` suppression as the state map, and suppressed cities are COUNTED so a thin city doesn't read as "nothing there". **`payer_concentration`** — HHI over each payer's share of contracted practice relationships, with DOJ bands. The caveat outranks the number: this is the share of PUBLISHED RELATIONSHIPS IN THIS STORE, not covered lives, so it is only as complete as the files ingested — under 5 payers it returns `thin: true` and NO band, because with three payers the minimum possible HHI is 3333 and the index would call every market concentrated by construction. **`steal_share`** — physicians who send therapy patients to other practices in a client's own cities and little or nothing to the client, ranked by the gap; `already_a_partner` separates "sends you some" from "sends you none". Shared-patient counts are a proxy for referral, not a record of one, and every surface says so. |
| `mrfx/quality.py` | Three questions a rate doesn't answer about itself. **`rate_type_mix`** — how much of a market is a contracted amount (`negotiated` / `fee schedule`) vs one the payer DERIVED where it has no contracted figure. Reported, never silently filtered: excluding derived rates by default would move every number the user has already seen, so composition is the finding and acting on it is their call; `code_type_flag` does the same for a client's OWN rates, where "the payer derived your rate" changes what the ask is. **`payer_freshness`** — per payer `last_updated_on` age, with three distinct states (fresh / stale / **unreadable-or-unstated**): a date we cannot parse must not pass as fresh and must not be called stale either. **`size_premium`** — answers the standard objection "the practices above me are bigger" by ranking each practice with the mid-rank (`leads.midrank_pct_sql`, the one position rule) WITHIN (payer, code) — so a payer's overall generosity can't masquerade as a size effect — then comparing coarse provider-count bands. A band under `min_practices` is marked `thin` with null figures rather than reported, and a subject with no rankable cell gets a "can't say" reason, distinguished from a typo (which refuses) by an explicit presence check. |
| `mrfx/hospital.py` | CMS hospital price-transparency import + parity. Reads both v2.x shapes — the tall CSV (2-line identity preamble above the real header, columns token-matched because vendor templates drift) and the JSON (streamed with ijson; these reach several GB). Keeps ONLY therapy codes, outpatient/both settings, and DOLLAR amounts: a percent-of-charges term or an algorithm description is not a price, $0 is a placeholder, and an `estimated_amount` is stored separately and labelled an estimate. **Hospital rates live in `hospital/*.parquet` behind the `hospital_rates` view — NEVER in `rates`**: a facility (HOPD) payment is a different product under a different mandate, and letting one into the spine would move every practice median in the app (regression-tested). `hospital_parity` matches on payer name AND billing code only — no fuzzy payer matching, because inventing a match invents the comparison — and reports a RATIO, never a subtraction presented as money owed. Every surface carries the facility-vs-professional caveat. A file we cannot read is refused with what was wrong; a valid file with no therapy line reports that and writes no part. |
| `mrfx/inflation.py` | Real-terms erosion — the one place the app says that a rate held flat is a pay CUT. CPI-U annual averages (BLS CUUR0000SA0, 1982-84=100) ship in `DEFAULT_VALUES`; the user extends them per published year with `mrfx inflation --year --value` or `POST /api/inflation`, stored in `inflation_index` (user years win). **A year with no index value REFUSES with a reason and a fix — never an extrapolation**; an explicit `assume_inflation_pct` is allowed and is echoed as an ASSUMPTION everywhere it is used, and it is validated even when the index makes it unnecessary. Real change is the compounding ratio `(1+nominal)/(1+inflation)-1`, not `nominal - inflation` — subtraction drifts over multi-year spans and drifts in the direction that makes a cut look smaller. Every output carries the CPI-is-a-consumer-basket caveat. `compute_payer_trajectory` gains `real_cumulative_pct` / `erosion_line` per payer. |
| Closure feed (`nppes.closures`) | The mirror of the new-clinic feed, off the same bulk cache: therapy NPIs DEACTIVATED recently and **not since reactivated** (NPPES deactivates for paperwork lapses as often as closures, so a reactivated NPI never closed). `NPI Deactivation Date` / `NPI Reactivation Date` joined `_NPPES_CACHE_COLS` and `_NPPES_CACHE_SCHEMA` went to 4, so an existing cache rebuilds once. Two consulting uses: a closed REFERRAL SOURCE is a hole in a client's inbound volume, and a closed COMPETITOR is room — `in_store` marks the ones that carried published rates. Packet section 6 intersects a client's own inbound referral partners with the feed. Every surface says a deactivated NPI is a call to make, never a fact to print. |
| `mrfx/tracker.py` | READ-ONLY discovery of a Medicare Order & Referring Tracker install (`ORF_DATA_DIR` → `%LOCALAPPDATA%\OrderReferringTracker` → `~/.order-referring-tracker`, or `tracker_dir` in config): its `snapshots/OrderReferring_<date>.csv`, its `referral-map/` datasets + `dataset-meta.json`, and which of those this store has already imported. Never writes to the tracker's folders — that data belongs to the other program, which has its own retention. The dashboard imports what discovery offered **in-process** (`POST /api/medicare/tracker/import`, daemon thread + polled job state): the CLI must refuse while `mrfx serve` holds the DB, but the server importing its own store is the one process that may — which is why the merge needs no stopping. Only paths discovery just returned are importable, so the endpoint can't be pointed at an arbitrary file. |
| `mrfx/rollplan.py` + `Store._run_plan` | **The durable sliced analytics build** (round 13 Group 1). A full rebuild is a PLAN kept in `meta('rollup_plan')`: directory hash slices, one prune slice, then spine slices per payer x billing-code range `[lo, hi)` (a single heavy code is cut into hash halves). Each slice DELETEs and re-INSERTs exactly its rows and advances the plan pointer in ONE transaction, so a kill resumes at the last committed slice; an out-of-memory slice is halved (kept in the plan) and retried; a plan made under other rule / schema versions restarts; columns that differ build in `__next` shadow tables. Measured on 99M rates: 738 s (0.7.6 one-shot) -> 363 s from an empty spine; kill after 25 of 49 slices resumes at 26; byte-identical (375/375 payer x code cells, 64/64 directory buckets). |
| `mrfx/progress.py` + `mrfx/progress_plans.py` | **The 0-100 % bar on every output** (round 13 Group 2). A `Progress` in a contextvar (the no-op default costs nothing); stages from the plan tables in the user's words, ticks, nested `group()`s, `waiting()`; DuckDB's own `query_progress()` sampled from a side thread on every connection the run opens (`Store.connect` attaches it); Stop = interrupt every attached connection and refuse the next `connect()` (`Stopped` subclasses `duckdb.InterruptException`, so fault-isolated loops still work). API: `ProgressMiddleware` (`X-Mrfx-Progress` header or `?progress=`), `_progress_route(plan)` on every output route, async `GET/DELETE /api/progress/{token}`; a superseded view query is cancelled. CLI: `TextBar`. |
| `mrfx/pdf.py` | "Save as PDF" (round 13 6.1): prints a report's own HTML with the Chromium `render._launch` finds or installs, every request refused; `?format=pdf` on the eight report routes (`api._html_or_pdf`), `mrfx packets --pdf`. No browser = `PdfUnavailable` -> a 422 sentence, never a broken report. |
| `mrfx/anchors.py` | Medicaid-floor and hospital-parity lines (round 13 6.2) in the negotiation one-pager, payer comparison, proposal and dossier. The practice's rate on a line is the rate the document already prints (passed in, never recomputed); Medicaid = newest loaded year of the state's schedule; hospitals = one rate per hospital under `hospital.hospital_filters` (the parity table's rule). A half with nothing loaded is omitted WITH its reason. |
| `mrfx/backup.py` | Store backups: `store_files`, `write_zip` / `verify_zip` (one format and one check for `mrfx backup`, `mrfx verify` and the hot backup) and `hot_backup` — `serve`'s weekly backup (round 13 6.3; off until `backup_dir` is set). Under the write lock: CHECKPOINT, `ATTACH … COPY FROM DATABASE` into a staging file on the backup drive, hard links pinning every part in `<store>/.backup_snapshot`; the zip is written outside the lock, verified, and only then are the oldest `mrfx-backup-*.zip` beyond `backup_keep` deleted. Disk guard: store + database copy + 1 GB free, else `BackupRefused` with the sentence (recorded in `meta('last_backup')` for the Files tab). |
| `mrfx/foldin.py` | **The analytics fold-in scheduler** (round 14 Group 1). ONE `FoldIn` per serving process owns every analytics update: whenever `rollups_stale()`, at a bounded cadence (3 s after the last file on a quiet queue; max(60 s, 3 x the last fold-in), at most 30 min, on a busy one), Run now / Pause, a back-off of 2..60 min after a failure that retries the SAME update. `fold_in()` is the one decision tree (resume a names plan as names, a full plan as full, first build in full, else the incremental; a full build only on `RollupNeedsFull`). `decide()` / `when_text()` are what the header and the panel say. See "Round 14 additions". |
| `mrfx/activity.py` | **The Analytics activity panel** (round 14 item 1.2): one dict (`build`) for `GET /api/analytics/activity` and `mrfx status --analytics` (`text`) — the live bar, files waiting and why, files added recently (the fold-in event ring in `meta`), per payer, problems, "What this means". Its headline numbers are `Store.analytics_coverage()`'s. |
| `mrfx/cli.py` | `mrfx serve / add / preflight / ingest / status / export / outreach / forget / enrich / speedtest / reset`. `speedtest <url>` pulls a slice of one real link on 1/2/4/8 connections (a DIFFERENT slice each time, so a CDN cache can't flatter the later runs) and either prints the `download_segments` line to paste or says extra connections don't help — it exists because "downloads are slow" has two opposite fixes and only a measurement on the user's own network distinguishes them. `add` hands URLs to a running server, else drains locally. `forget <filename>` (or DELETE `/api/files/{name}`, the Files-tab remove button) erases one file's rates + raw copies and flips its done url_queue row to 'skipped' so the dedup anchor never outlives the data; rollups rebuild after. |

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
   LATE-HEADER CONTRACT (header keys AFTER in_network; real files put them
   first): streaming paths (classic parse / parse_fast) stamp rows already
   flushed before the header arrived with the PREFLIGHT seed (preflight reads
   the first 1 MB) and later rows with the real header; the big-file single
   read (run_big_file_fast) emits every row after the full read, so EVERY row
   carries the real header. Identical whenever all rows fit one batch.
   Pinned by test_fastscan.py::test_late_header_contract.
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
   downloads resume via HTTP Range guarded by an If-Range validator AND a
   check of every 206 against what was asked (`.val` = the first response's
   validator + size; an unvouched join is sampled — Round 11 additions);
   in-flight
   queue rows AND files-table 'processing' rows recover on restart
   (`recover_stuck_urls` / `recover_stuck_files` — the latter runs at CLI
   startup, before any worker thread, so it never races a live ingest);
   re-ingest atomically replaces that file's part — never duplicates rows.
   Content-sha dedup skips byte-identical files from other domains AND from
   the inbox (one content claim for every path — Round 11 additions). The
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
   Mid-grind refreshes are purely TIME-based (since 0.8.0 in the analytics
   scheduler, `mrfx/foldin.py` — `_rollup_due` and the 4x/90 s rule are gone):
   one per max(60 s, 3 x the last fold-in), at most 30 min apart, counted from
   the fold-in's COMPLETION, and a few seconds after the last file when the
   queue is quiet. There is deliberately NO count-based ("N files
   pending") trigger: on a big store one rebuild outlasts any batch of fresh
   ingests, so an unconditioned batch check fired the instant each rebuild
   finished — rebuilds ran back-to-back for the whole grind, monopolized the
   store's disk, and extraction slowed to "chunks frozen, CPU busy" (the
   measured July 2026 field failure). Counting the interval from the start
   instead of completion would likewise degenerate into rebuild-per-file.
   `scan_inbox` batches to ONE rebuild per pass for the same reason.
   Rebuilds never give up (round 13: a failure backs off 2..60 min and is
   retried; since 0.8.0 the analytics scheduler, mrfx/foldin.py, owns the
   timing — see "Round 14 additions") and never run per-file during queue
   grinds.
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

### Round 7 additions (Nov 7, 2026) — rules a change must keep

- **Request guard** (`api.LocalOnlyMiddleware`): only Host `localhost` /
  `127.0.0.1` / `::1` (any port) is answered, and an unsafe method whose
  `Origin` is foreign or whose `Sec-Fetch-Site` is `cross-site` gets 403. A
  web page the user visits could otherwise upload a file (a re-ingest under a
  real payer's filename REPLACES its rates), replace the Medicare anchor or
  queue the catalog. Tests add `"testserver"` to `api.TRUSTED_HOSTS`
  (`tests/mrfx/conftest.py`); a scratch harness built on TestClient must too.
- **Materialised practice medians** (`Store.spine_materialised`,
  `benchmark.practice_base_sql`): `practice_medians_v1` per (code, TIN) and
  `practice_medians_payer_v1` per (payer, code, TIN), house basis, built
  lazily once per `spine_generation` (bumped by every spine write, NOT by a
  directory name refresh), parquet next to the DB, previous generation
  removed. Readers get them only when `_house_eligible(market)`; the
  practice/code filters come after via `_practice_attr_where` (the same
  clauses `_market_where` ends with). Change the table SQL → bump the name's
  `_vN`; never ROLLUP_SCHEMA_VERSION. A new reader of "median per practice"
  must go through `practice_base_sql`, and the both-ways test in
  `test_round7.py` must cover it.
- **User edits vs the cache**: `Store.note_user_edit()` (a separate
  `user_edit_generation`) invalidates every `Store.cached` answer without a
  rebuild. Any new user-edited table a cached reader reads must call it.
- **Exports** are built in `<spill dir>/exports` (never the system temp
  folder), once, streamed with the BOM; the size check estimates rows x 243 B
  (x1.25 ZIP, + the sort spill when it cannot sort in memory) and answers
  507 with a sentence. DuckDB's COPY writes `tmp_<name>` and renames: clean
  both on failure.
- **Rounding**: every Python round of a displayed number goes through
  `mrfx/rounding.half_up` (half away from zero on the printed decimal — what
  DuckDB's `round()` and a person do). Python's `round()` is half-to-even and
  printed 112 % where SQL printed 113 %.
- **Supervisor**: `serve` exits `EXIT_NO_RESTART` (3) for port-in-use and a
  broken config; the supervisor stops on it. The child's output is teed to
  `<store>/diagnostics/serve.log`. A test of the supervisor must stub
  `cli._serve_log_path` (or `_run_child_teed`): with a log path the child is
  a real `Popen` of the repo's config.
- **Updater order**: the new build's requirements install FIRST (from the zip,
  in a temp dir); a failure changes nothing. Then the backup, the writes
  (rolled back from the backup on an OSError), then `pip install -e .` only.
  An older build is refused without `--allow-downgrade`.
- **Ingest commit**: `ingest._commit_done` — after a part goes live, the
  done-upsert is retried once, then the part is withdrawn; a non-done row
  never owns the part it just wrote. Parked parts are adopted only while a
  `files` row for them exists; forget/drop/reset discard them.
- **Downloads**: segments are bound by no-progress, not the wall-clock cap;
  a dead worker thread's claimed row goes back via `Store.release_claim`;
  every link extractor reports truncation (`cap_note`), `child_count` is the
  number listed.
- **Early duplicate check** (`fetch.early_duplicate_check`, config
  `early_dedup_mb`, default 15). LOADED fingerprints (`content_fingerprints`:
  size, sha of the first N MB, of the last 1 MB, of 8 x 64 KB windows, and
  the container `magic`) are written ONLY by `ingest._finish_ok` (and
  backfilled from a copy caught by the full-content sha). IN-FLIGHT ones live
  apart in `inflight_fingerprints`, one per queue row, dropped when the row
  ends — a re-fetch under an old file name never passes for that file (audit
  8 F2). A download's beginning is compared once N MB arrived; only on a
  same-size beginning match are its end + windows fetched by range requests
  (with the download's own UA / TLS context). A SKIP needs a loaded twin, a
  full match and a COMPRESSED file (gzip/zip: the end carries a CRC of the
  whole content) — a plain .json can differ in one middle rate with equal
  size/head/tail/windows (audit 8 F3), so it never skips early. A match
  against an EARLIER in-flight row parks the copy with `waiting_on` set; the
  wake functions never release it while that row is downloading / fetched /
  ingesting (no reconnect loop, audit 8 F1), `release_mismatched_waiters`
  frees it when the twin's full fingerprint differs, and on wake the verdict
  is taken from local data before any request (`_local_duplicate_verdict`,
  so an expired link never matters, F6). Early-skipped rows carry
  `duplicate_of`; forget rewrites them.
- **Download lanes** (`small_file_downloads`, `small_file_mb`): small slots
  take small/unsized rows first and help with big ones when none wait, big
  slots take big rows first then anything. Since round 9 the downloader
  never hands a started row back (it cost every big file a second GET);
  `fetch_url_record(lane_max=…)` still can, for callers that want it.
  `url_progress` never replaces a known size with 0.
- **External files**: hospital CSVs read every `code|N` slot and the CMS wide
  layout (`standard_charge|<payer>|<plan>|negotiated_dollar`); hospital
  modifiers go through `modifiers.normalize`. The MPFS importer finds the
  header under title rows (`_read_rows(text, key)`), prefers the non-QPP
  PPRRVU and cross-checks the file's CONV FACTOR.

### Round 9 additions (Nov 10, 2026, v0.7.3) — rules a change must keep

- **File identity** (`content_fingerprints`: `size`, `mtime_ns`, `full_sha`,
  `trailer_proves`; `ingest.same_name_verdict` -> same / replace / new):
  "same" needs the recorded size+mtime, an equal full sha256
  (`Store.done_file_with_sha`) or the legacy fallback — never size alone. A
  different file for the same payer and month is HELD (`inbox/.held`,
  status `needs_choice`) until `POST /api/files/{name}/choose`
  (replace -> `inbox/.replacing`, keep -> a content-tagged name). A file that
  changed while read is not recorded (`_INGEST_STAT`). `record_identity`
  re-queues copies skipped against the OLD content when the content under a
  name changes.
- **Provider lists by vintage**: `Store.companion_files_for(payer,
  last_updated_on, month)`; no vintage -> the newest. `requeue_skipped`
  looks in downloads/ and otherwise re-queues the link
  (`Store.requeue_url_for_file`). Ids are exact (`refid.clean_ref_id`;
  floats only below 2^53). Empty companion entries are missing, `location`
  ones are `REMOTE_REF` — both counted. Deferred groups spill past
  `DEFER_MEMORY_GROUPS` (`parser._DeferredSpool`). The skims report the
  file's `shape`; a table with no in_network is a reference file on every
  path.
- **Row rules 4**: a part is stamped only after it was read
  (`_TYPE_SCAN_BATCH`, mapped by basename); a file with a parked
  replacement waits for a later start. `.verified_parts.json` lets the
  start-up check skip unchanged parts.
- **One overall position** (`benchmark.overall_position` and the same rule
  in Leads / leaderboard / roster SQL / size premium): the median of
  UNROUNDED per-code mid-ranks over codes whose rates differ, rounded once;
  p50 when every compared code is all-tied. Benchmark rows carry
  `subject_percentile_exact` and `in_position`. Leads' worst payer is
  compared on the codes all its payers share (`leads._worst_payer`).
- **The subject rule everywhere**: every subject reader uses
  `subject_tins_for_market` (partial names too; ids unchanged) and reads the
  subject's own rows through `subject_market`; `resolve_subject_tins` is
  sorted; a mapped entity is titled `mapped_entity_name`.
- **Trajectory**: bounded by a pinned month; cut streak from chain-linked
  steps (`step`); ONE window pass (measured faster than every self-join /
  list / integer-key / hash-CTE variant on the 99M store), generation-cached.
- **Materialised medians** are named by `Store.spine_key()` (a `spine_token`
  each spine writer stores IN its own transaction + the parts' signatures +
  rollup bookkeeping) and reused across restarts. A new spine writer must
  write the token in its transaction.
- **Input**: `benchmark._typed_market` — `_` keys and `curated_tins` are
  never taken from the wire; each key typed or refused; `include_non_dollar`
  is refused (no pooling of % with $). DuckDB conversion/binder/parser
  errors -> 422 sentence; no SQL or path in any body (`api._plain`).
  `test_no_wrong_typed_market_reaches_the_database` fuzzes every POST route.
- **Printing**: `rounding.signed_pct` for every signed %; footers use
  `benchmark.market_in_words`.
- **Lanes are claim preferences only**: the downloader never hands a started
  download back (one body GET per file — since 0.7.5 a small-file slot may send
  a 1-byte size probe first, so 1-2 requests per file; never "restore" one
  GET); slot 0 helps with big files; no lanes
  with <= 2 slots. User actions and parks clear `waiting_on`.

### Audit 8 additions (Nov 10, 2026) — rules a change must keep

- **Provider references are file-local** (`parser._resolve_refs`). An id the
  file's OWN `provider_references` table defines resolves only from it — a
  remote `location` entry or an empty one is missing (counted
  `remote_ref_groups`), never filled from a companion. Only an id the file
  does not define falls back to the payer's standalone files. Groups defer
  until the table is complete (`_refs_complete`), companion or not. Two
  same-vintage companions that define an id differently map it to `None` in
  `load_provider_refs` (unresolved, `qa.ambiguous_ref_ids`). All four parse
  paths (classic, fast, two-pass, single-read big) and the pool worker are
  pinned by `tests/mrfx/test_audit8_ingest.py`.
- **A reference-only file the sniff misread** (`parser.reference_only`: a
  table, no `in_network`) is stored via `ingest._ingest_reference_file`
  (batched, `REF_BATCH`), on every path — never a 0-row rate file.
- **A part swap supersedes earlier pending ops** (`store._swap_in_part`): it
  discards this file's parked replacement and removes its doomed entry first.
- **Row rules 3** (`ROW_RULES_VERSION`): `parser.normalize_negotiated_type` is
  the one spelling rule for the parser and the stored-row migration; files
  that lost "bundled" items under the old arrangement rule get
  `qa.reingest_suggested`.
- **The subject of a report** (`benchmark.subject_tins_for_market` +
  `subject_market`): the peer-only filters (`PEER_ONLY_KEYS`: state, city,
  therapy_only) never touch the subject's own rows; a bare NPPES name shared
  by practices in several states means the one in the market's state (the
  Explorer's "NAME · ST" entity), a mapped entity / TIN / NPI keeps all its
  TINs. Benchmark, Negotiate (comparables and cross-payer context), the rate
  card and its sparklines all go through it.
- **Trajectory** (`monitor.compute_payer_trajectory`): a matched panel per
  payer — (code, practice) pairs priced at both of its endpoint months, each
  against its own first-month rate, median per code then across codes. Never
  one median pooled over codes or over a shifting panel.
- **Positions**: `leads.midrank_pct_sql(partition)` everywhere a position is
  printed (Benchmark's CTE is the same rule). Leads, the leaderboard and the
  worst-payer gap count only codes with ≥2 practices AND ≥2 distinct rates
  (the leaderboard puts an all-flat practice at p50 rather than dropping it).
- **Outreach merge fields** are on the house basis (`practice_base_sql` at
  the dashboard's month and payers), mid-rank among the OTHER organizations,
  defined in `OUTREACH_COLUMNS_NOTE` (the ZIP's methodology).
- **Rounding**: `rounding.half_up` is DuckDB's `round()` (half away from zero
  on the double `x * 10**d`), checked equal on 70,000 values; no Python
  `round()` on a printed number (store/ingest/nppes keep theirs for internal
  values).
- **Per-visit value** compares payers on the codes every compared payer
  prices; a payer's own-codes value is shown, never ranked.
- **Inflation**: every index year keeps its own source (`load_index()["sources"]`);
  a deflator reports its endpoint years' name and source.
- **Same-name files**: `ingest.same_as_loaded` (fingerprint vs the loaded
  one) — an identical copy is left alone / reported as a duplicate on upload,
  a different file is loaded under `unique_inbox_name`. Never silently skipped.
- **The Files poll** (`/api/files`) is paged (`limit`/`offset`, newest first)
  and carries no `preflight`; the dashboard never stacks polls.
- **`config/starter_links.txt` is generated** from the catalog
  (`known_sources.starter_links_text`); a test pins them equal.

### Round 10 additions (Nov 10, 2026, v0.7.4) — rules a change must keep

Pinned by `tests/mrfx/test_review10.py` (26 of its 30 cases fail on 2bb81b8).

- **A superseded search is interrupted, never queued.** The page sends every
  Explorer request with `X-Mrfx-View: <view>` and an AbortController
  (`app.js supersede()`); the server's `_view_query` registry calls
  `con.interrupt()` on the older request of the same view, which answers 409
  "superseded by a newer search". `/api/summary` waits for the single-flight
  lock (≤ 8 s) when the older holder was interrupted instead of refusing.
  One debounce per typed box (`bind()`); a name needs ≥ 2 characters.
- **Every per-generation cache is keyed by `Store.spine_key()`**: the
  practice list (`spine_units_v1`, `FilterSet.units_provider`, set per app in
  `_fs_app` — never a module global), the persisted unfiltered strip
  (`summary_strips.v2.<spine_key>.json`; bump the `v2` when the strip's shape
  or rounding changes). `mrfx reset` deletes both.
- **Late name join**: a practices-first page reads spine rows by unit
  (`_spine_rows_by_unit`) and joins names after the cut (`spine_by_unit_ok`).
- **Entity grain is two-pass** (`entity_two_pass_ok`, `entity_light_rel`,
  `entity_page_sql`): a light aggregate (median, newest month, AND the TIN
  list — it is an order tiebreak) picks / filters / counts / pages the groups;
  the list columns (`_ENTITY_HEAVY`) are built for the kept groups only.
  Any new entity column that can be sorted or filtered on must be in the
  light pass. A/B: 0 mismatches over 11 filters × 6 sorts × 3 offsets.
- **NPI-grain guard follows the memory budget** (`NPI_ROWS_PER_GB`); below
  `NARROW_REQUIRED_ROWS` a code alone is narrow enough.
- **LIKE text is literal**: `_lit()` escapes `\ % _`, every LIKE carries
  `ESCAPE '\'`.
- **The house basis everywhere a rate is summarised** — newest file per
  contract, professional, base modifiers, no NPI-in-slot rows, one rate per
  practice first: the drawer chart, `/api/trend` (one rate per unit per
  month; a payer needs ≥ 2 months), the overview (one comparable service per
  discipline — the moderate evaluation codes), the Explorer's default month
  ("latest"). The user's own class/modifier filter wins (`practice_basis`).
- **Server-side rounding**: every median the API sends for display is
  `round(x, 2)` in SQL / `half_up` in Python (summary strip, by-code, Code
  page ranking); figures derived from several values are computed from the
  UNROUNDED inputs and rounded once (weighted position, hospital multiple,
  floors percentages).
- **One market median**: the median of the OTHER practices
  (`leads.others_median_sql` / `leads.median_of_others`) — Benchmark, Leads,
  the worst-payer gap and the outreach file. Exact, without per-row lists.
- **One subject rule** on every surface (remits, leverage, orgprofile,
  economics, floors, utilization added this round):
  `subject_tins_for_market`, and the subject's own rows through
  `subject_market`.
- **Geography is the dimension, not a filter**, on Markets' state ranking
  (`geographic_rates` drops state/city and returns `selected_state`) and on
  leverage alternatives (the radius is the geography). A state's city table
  explodes only the NPIs located in that state (`territory.local_rate_map`).
- **Rate changes for a picked month** pair each payer's file with ITS OWN
  previous file (`monitor.payer_months`); `compared` / `not_compared` are
  always returned and an empty comparison is a refusal, never "No changes".
- **Ranking payers** (rate-card scorecard, Leads' worst payer, per-visit
  value) is on the codes every compared payer prices; % of Medicare is shown,
  not ranked on.
- **Pinned documents stop at their month**: source-file lists, trajectory
  `months`, sparklines. Sidecars print `file_month` per file and filters in
  words (`api.filters_in_words`).
- **States are two-letter codes**: `store._state_cell` normalises names on
  save; `_compute_states` keeps `STATE_CODES` only.
- **Wire inputs**: `_as_float` refuses NaN/inf, `_as_int` refuses overflow,
  radii go through `_radius_arg`; a non-object `market` is a 422 everywhere.
- **User edits that cached answers read call `Store.note_user_edit()`** —
  now including `inflation.save_index_values`.
- **Clients**: `clients.add_client` refuses a name that matches no practice;
  `_save_client` is the raw writer.

### Round 11 additions (Nov 11, 2026, v0.7.5) — rules a change must keep

Pinned by `tests/mrfx/test_review11.py` (all 49 cases fail on 1064203); the
reproducing scripts are under `docs/review11_repros/`.

- **One content claim for every path.** The bytes a file holds are claimed
  before they load, under ONE write_lock: a link's queue row
  (`Store.claim_content_ingest`, url_queue `ingesting` + `content_sha`) and an
  inbox / upload / "Keep both" file (`Store.claim_inbox_content`, the
  in-memory `_inbox_claims`, called by `ingest_file` itself through
  `ingest._content_claim`). Each sees the other's in-flight claims AND every
  loaded file's `content_fingerprints.full_sha`. A copy is recorded `skipped`
  ("identical to X … not loaded twice", `qa.duplicate_of` / `duplicate_sha`)
  and moved out; a twin loading right now is waited for (inbox) or deferred
  to and revived on failure (link). A re-load of a name with a live part is
  not checked (it replaces its own part). Over 4 GB the fingerprint proof the
  link path trusts (`_proves`) stands in for the sha. Never add an ingest
  path that bypasses `ingest_file`.
- **One identity read.** `fetch.file_identity` = full sha + gzip members +
  the early-dedup fingerprint from one sequential read; `_IDENT_SEEN` keeps
  it from the claim to `_finish_ok`; a link's fingerprint rides in
  `DOWNLOAD_IDENTITY["fp"]`. A new consumer of a file's sha or fingerprint
  reads the cache, not the file.
- **A 206 is checked against what was asked.** `.val` is JSON `{validator,
  total}` from the FIRST response (`_read_val` / `_write_val`; a 0.7.4 plain
  validator still reads). A 206 must start at `resume_from`, describe the
  remembered total and carry no other validator; `done != total` restarts
  (raw bodies); `total` never becomes None once known. A resume no
  validator vouched for (`vouched`) is proven by `_splice_agrees`: the kept
  bytes before each join and the first 64 KB equal what the server serves now
  (2 small range requests). The honest residual — no validator, same size,
  and a change only inside unsampled bytes — is stated in GETTING_STARTED.
- **A kept download is validated before reuse.** `_kept_meta`: a sidecar is
  trusted only when it records the size and mtime of the file on disk (0.7.4
  sidecars are re-checked); otherwise the file is named from its own bytes
  (no request). Every path that removes or moves a download removes its
  sidecar; `run_queue` sweeps sidecars without a download. A re-queued LOADED
  row (`url_queue.revalidate`: 1 refresh, 2 reload) asks the server once
  (`_server_now`, a 1-byte ranged GET) and compares with what was recorded at
  download (`server_validator`, `server_size`); unchanged on a refresh -> done
  again, nothing downloaded; changed -> the kept copy is dropped. A
  re-expanded index asks about its loaded children
  (`_refresh_loaded_children`).
- **No live part is read across a swap without a second chance.** DuckDB
  reads a part's footer at plan time and re-opens it to scan; a swap in
  between raises "Parquet file is likely corrupted". The dashboard asks again
  once (`api.SwapRetryMiddleware`, `_part_swap_race`; a second miss answers
  503 in words). Generation-named parts (`<key>.<gen>.parquet`) would remove
  the race at the source — not done (every part-path reader changes).
- **Every requeue resets through one fragment.** `store.REQUEUE_RESET`
  (progress, retry_after, park_count, waiting_on, auto_resumes, big_lane,
  finished_at, duplicate_of, kind duplicate/canceled -> unknown) on every path
  that puts a finished / stopped / paused row back in the queue; `error`,
  `retries` and `force_size` are the caller's. A new requeue path uses it.
  Crash recovery (`recover_stuck_urls`, `release_claim`) and the timer wakes
  do not reset (the auto-resume budget is theirs).
- **A worker dies with its parent.** `ingest._worker_watch_parent` is the
  pool initializer: a thread polls the parent every `PARENT_WATCH_SECONDS` and
  `os._exit(0)`s. Never PR_SET_PDEATHSIG (it fires when the spawning THREAD
  ends). `ParsePoolManager.shutting_down` turns a BrokenProcessPool during
  shutdown into `PoolRestarted` (no heal + resubmit).
- **Children are queued only while their index is still expanding**
  (`enqueue_urls(only_if_expanding=True)`, checked in the same locked step) —
  a user's "Clear queue" is never undone by a finishing expansion. A done
  toc/page row is re-fetchable (user retry, direct re-paste; never a
  re-expansion — mutual indexes would loop). An oversize child is revived by
  a re-expansion only when its recorded size fits.
- **Lanes size before they claim.** A small-file slot sizes an unsized row
  with one 1-byte request and hands a big one to a big slot (`big_lane`)
  before any body is fetched; provider-reference names are claimed first.
- **Locks on Windows.** Every rename/unlink of a download goes through
  `replace_with_retry` / `unlink_with_retry`; a `.part` answered 416 whose size
  is the remembered total is finished from disk. The part swap under
  write_lock tries ~1.4 s then parks; the parked-part adoption tries briefly.
  Parts are fsynced before the rename that makes them live.
- **Sentences.** `plain_parse_error` maps a dead parser pool, damaged gzip /
  zip data; `sniff._unknown_reason` names empty and UTF-16 files; httpx
  `TooManyRedirects` / `DecodingError` are terminal plain text; a 429/503's
  `Retry-After` (<= 10 min) replaces the generic back-off and the give-up text
  names the last HTTP status; the back-off sleeps in 1 s slices so "stop
  downloads" frees the slot at once; a refused range-ignoring 200 does not
  spend the retry budget.

### Round 12 additions (Nov 11, 2026, v0.7.6) — rules a change must keep

Pinned by `tests/mrfx/test_review12.py` (every Group 1-3 and 4.1 a-f case
fails on 68d9bc7); the reproducing scripts are under `docs/review12_repros/`.

- **Several files of one payer and one month are POOLED, never superseded**
  (item 2.5, decided with real files). The spine groups by (payer, TIN, code,
  modifiers, class, POS, month) and pools every file of that month: a practice
  priced in two of them gets the median of the rates they give. Measured:
  Tufts' 2026-11 subsidized and non-subsidized books are two files with the
  same date; 43,810 of 46,149 contract lines are in both, 1,350 at different
  rates — two plan books, not a correction, and no file is "newer". A newest-
  file-per-contract rule would drop one book at random. What the app does
  instead is SAY it, with numbers: `quality.pooled_month_files` (one spine
  aggregate per data generation) feeds the Files tab ("Pooled months: …") and
  every methodology (`api.methodology_text`, `benchmark.methodology_footer`).
  A payer that republishes a corrected file under a new name in the same month
  is pooled with the file it corrects — the user removes the old one. The plan
  dimension (`plan_index`, `spine_relation`) is the tool that separates books.
- **The dashboard opens first; a stored-row update never sits in front of
  it** (item 2.9, decided). `_migrate_row_rules(budget_s)`: `mrfx serve` opens
  the store with `migrate_budget_s=MIGRATION_BUDGET_S` (30 s), checked between
  slices of 200 files / between parts; the files not reached are returned and
  `Store.finish_row_rules_in_background` updates them one at a time under the
  per-file claim an ingest takes (`ingest._claim_ingest`; a file being loaded
  is skipped — its new part is written under the current rules), then runs the
  incremental rollup; `Store.rows_update` feeds the Files tab line "Updating
  stored rows … k of N files — numbers from the files not yet updated can still
  change". Every CLI command opens without a budget (an export or a report
  must never mix rule versions). A new stored-row rule bumps
  `ROW_RULES_VERSION` and inherits this.
- **Visit value in this market** (item 3.2; `mrfx/visit_market.py`):
  confined to one state and one discipline, both required (a sentence, never
  an empty table, without them); payers optional; population = the
  Benchmark's market on the house basis, a practice = a TIN located in the
  state; a practice x payer pair counts only when the payer prices EVERY code
  of the basket (partial pairs excluded and counted); visit value = units x
  rate rounded as the visit calculator rounds (rate, line, total to the cent);
  all payers = each practice's median of its own payer values, never a
  cross-payer sum; a cell under 5 practices (`market._MIN_CELL`) prints no
  number and no count; the subject's value is `price_visit`'s (the rate card's
  rates, round 9's subject rule), its percentile the mid-rank among the payer's
  practices with the subject counted once in place of its own TINs ("this
  practice included"), its $ / % from the unrounded mean and median rounded
  once, its quartile from the printed P25 / median / P75; every printed number
  through `half_up`; rows by payer, all payers last; screen, printable and CSV
  render one dict (`block_html`, `to_csv`, the JSON); generation-cached; no
  per-request scan — the per-discipline table `practice_medians_disc_payer_v1`
  (built once per spine generation; `practice_base_sql` routes any per-payer
  house market with a discipline through it, and `test_round7`'s both-ways
  test covers it).
- **A `.val` records its validator's KIND** (item 1.2): `{"validator",
  "kind": "etag"|"last_modified", "total"}`; an older `.val` is classified by
  shape (quoted / `W/` = ETag, an HTTP date = Last-Modified, anything else an
  unquoted ETag). A resume compares like with like (`_same_validator(started,
  resp)`); a response with no header of that kind proves nothing. Every path
  that drops the `.part` also clears `splices` and counts a restart; the
  set-aside sentence reads what is kept from disk.
- **An 'ingesting' url row is an owner only while THIS process holds it**
  (item 1.3): `Store._live_url_ingests` (processor claims and won content
  claims). An inbox copy never waits on an orphan; its wait also ends on the
  Files tab's stop and on shutdown. Every entry point that opens the store
  with no server (`serve`, `ingest`, `add`) runs `recover_stuck_urls` beside
  `recover_stuck_files`.
- **A re-expanded index works with the links it hands over NOW** (item 1.4):
  probes and re-downloads use the current child link (keyed by `dedup_key`);
  `_enqueue_one` keeps the newest link on any row not in flight (also a
  re-pasted queued / paused row, item 2.4); `_server_now` retries a 403 as a
  browser and repairs an incomplete chain (never weakened), 20 s timeout, and
  the whole re-check stops after `RECHECK_BUDGET_S` (300 s). A child loaded
  without a server record is compared by the size its load recorded
  (`content_fingerprints.size`, item 2.3).
- **Nothing an index does after "Clear queue" sticks** (item 1.5): the
  re-check stops asking, `requeue_republished(parent_id=…)` and the final
  done / failed / skipped writes (`update_url(only_if_status="expanding")`)
  happen only while the index is still 'expanding', in the same locked step.
- **One automatic memory rule** (item 2.6): `store.auto_pool_gb` = 40 % of RAM,
  2-12 GB, never more than RAM - 3 GB (OS) - 2 x 2 GB (parser workers); the
  shipped config leaves `duckdb_memory_gb` unset, and `fetch._ram_worker_cap`
  sizes the workers from the same pool. Every config key a guide tells the
  user to set is in `config/mrfx.yaml`, at least commented (tested).
- **Feeds filter on distance before the limit** (item 2.7, `nppes._feed_page`).
  **A proven-identical inbox twin is deleted** when its original is kept
  unchanged in `data/processed` (item 2.8). **A Medicare anchor is a dollar
  amount above zero** (item 2.1: refused at upload; a stored non-finite rate
  is no anchor).
- **`serve` treats SIGTERM as Ctrl-C** on POSIX (item 4.1h): the `finally`
  runs, the pool is shut before the URL worker is stopped, and its queues are
  closed (`ingest._release_pool_resources`) — no "leaked semaphore" line.
  `/api/stats` and `/api/boot` log any call over 5 s (`api._log_if_slow`).
  httpx2 stays out of `requirements.txt` with starlette pinned (decided,
  item 4.1j; `test_review12` fails if starlette moves without it).

### Round 13 additions (Nov 12, 2026, v0.7.7) — rules a change must keep

Pinned by `tests/mrfx/test_review13.py` (every case fails on c8b80fc, the
value it got there in its docstring) and `tests/mrfx/test_progress.py` (the 14
progress tests); the evidence is under `docs/review13_repros/`.

- **The coverage contract** (item 1.2). The dashboard counts what is LOADED
  (`files`), every screen answers from the ANALYTICS (the spine); the two can
  differ for hours on a billion rows, and the app must always say by how
  much. `Store.analytics_coverage()` -> `{state: current|behind|building|failed,
  covered_files, done_files, covered_payers, loaded_payers, reason, pct, eta,
  sentence}` from the files table, the coverage markers, the build plan and
  the per-PART sets `part_payer_set` / `part_tin_set` (one scan of a new part,
  never of the store). Carried by `/api/stats`, `/api/boot`, `/api/files`;
  `/api/payers` and `/api/months` list what is loaded with `covered` = what
  the analytics hold (uncovered chips greyed, still pickable, "no rows" says
  why). Every methodology footer / sidecar prints `rollplan.coverage_sentence`
  ("Analytics cover N of M payers (a of b files). … Every number here comes
  from those") while not current. A new output must print it too
  (`store.analytics_note()`). A payer counts as covered only when ALL its
  slices of a part-way full build are committed (`Store.payers_part_way`):
  a half-built payer has rows in the spine but is not "covered".
- **The durable rollup plan** (item 1.3; `mrfx/rollplan.py`). Never a
  one-statement rebuild of the whole store again. Slices commit with their
  plan pointer; the spine generation advances per slice, so payers appear as
  they commit. `update_rollups_incremental` refuses while a plan is
  outstanding (the plan finishes first); a failure is noted with its sentence
  (`rollup_failure`) and the queue retries after a 2..60 min back-off — it
  never gives up for the run; an idle `serve` picks up a part-way plan
  (`STALE_CHECK_SECONDS`). Any change to spine columns or rules must bump the
  versions the plan checks, or a resumed plan mixes rules. Extrapolated: a
  1.16 B-rate store builds in about 2-4 hours on a 4-core laptop (0.7.6's
  one-shot grew faster than linearly: ~28 h on this box), the dashboard
  usable throughout.
- **The progress helper** (Group 2): an output route gets
  `@_progress_route("<plan>")` and stages in its module
  (`progress.current().stage(...)`); a stage not in the plan is appended at the
  plan's median weight (the bar slows, never goes back); 100 % only with the
  body in hand; a Stop answers 409 and never a partial document. Without a
  token the route returns the same bytes as before (pinned). Never call a
  progress method in a way that can raise into the work (invariant 3).
- **Measured slow paths** (Group 3): `Store.pairs_relation()` is THE source
  of (TIN, NPI) pairs (built once per part signature; NULLs kept so a
  DISTINCT over it equals the same over `rates`) — never `SELECT DISTINCT
  tin_value, npi FROM rates` in a request path; `parts_holding(id)` narrows
  `/api/validate` to the parts that hold a TIN / NPI; spine counts persist per
  spine key on disk; the leads screen and its CSV are one computation; the
  entity drawer aggregates only its member TINs' spine rows.
- **4.2 decided: disclose.** A practice's all-payer rate for a code is the
  median over its published base LINES — a payer listing the code under two
  place-of-service sets counts twice (median(46.29, 46.29, 67.15, 46.01, 20.33)
  = 46.29, one-per-payer would be 46.15). Measured: the other rule would move
  52 of 144,859 practice x code rates on real files (0.04 %, max $47.24).
  `benchmark.PRACTICE_RATE_RULE` is printed with every all-payer figure.
  Changing it is a numbers change: re-run every known-answer test and say so.
- **Visit-value quartile** (4.1) is read from the PRINTED percentile (<25 Q1,
  <50 Q2, <75 Q3, else Q4), never from P25 / median / P75 (ties).
- **5.1 / 5.2.1 content identity.** Identity is the sha256 of the raw bytes
  AND, beside it, the sha256 of the CONTENT (`identity._ContentHash`: gzip
  inflated member by member, a plain file as is, a leading BOM dropped, a zip
  through its chosen member; `content_fingerprints.content_sha/content_size`).
  Both content claims (`claim_content_ingest`, `claim_inbox_content`) check
  both; the skip says "the same content as X — the same document compressed
  differently (or saved decompressed)". The 4 GB local-hash limit is gone.
- **5.2.4 code sets.** `files.code_set` records the billing codes each file
  was read for; after codes are ADDED, `/api/files` counts the files read
  without them and `POST /api/files/reread` reads them again (kept copy, else
  the link downloaded again, else the row asks for the file); pasting a loaded
  link again re-reads it when the list grew. Removing codes re-reads nothing
  (the rows are filtered at read time).
- **5.2.7 a provider list landing during its payer's ingest** is applied:
  `Store.refs_epochs()` (bumped in `finish_provider_refs`) is snapshotted at
  the start of `ingest_file` and compared at its end; a change re-reads the
  file and returns that read's result. Deterministic tests force both
  orderings with hooks; the original race test ran 30/30.
- **Group 6.** PDFs are the HTML printed, nothing recomputed; anchors lines
  use the document's own rates; the hot backup copies the database under
  the write lock with DuckDB's own copy and pins parts with hard links,
  verifies before deleting anything, and deletes only files it named;
  bookmarkable links (`#view?subject=…&state=…`) only fill the boxes the user
  would. 6.5 (xlsx) and 6.6 (expected payment) were not built — see the
  round-13 prompt's status block.

### Round 14 additions (Nov 12, 2026, v0.8.0) — rules a change must keep

Pinned by `tests/mrfx/test_review14.py` (every Group 1-3 case fails on
9617c59, the value it got there in its docstring), `tests/mrfx/test_repros.py`
(the repro scripts of rounds 11-14 against their recorded "after" lines) and
`tests/mrfx/test_stress.py` (the two race tests beside a CPU hog,
`MRFX_STRESS_N`). The evidence is under `docs/review14_repros/`.

- **The fold-in contract** (Group 1; `mrfx/foldin.py`). A file reaches the
  analytics by exactly one road: whatever loaded it (the queue, the inbox
  watcher, an upload, a re-read, a Keep-both) commits its part and its 'done'
  row, then pokes the store's scheduler (`foldin.poke`; `ingest_file` does it
  on every 'done'). ONE `FoldIn` per serving process (started by `serve`, or
  by `run_queue` for its own lifetime when none runs — a CLI drain, a test)
  decides when: whenever `rollups_stale()` and the analytics lock is free and
  the write lock is free at that instant; a quiet queue after `IDLE_SETTLE_S`
  (3 s) from the last poke, a busy one (`note_queue` from run_queue's
  coordinator, or any in-process ingest claim) after max(`BUSY_FLOOR_S` 60 s,
  `BUSY_MULTIPLE` 3 x the last fold-in), capped at `BUSY_CAP_S` 30 min,
  counted from the END of the last fold-in. Never "when the queue is idle"
  again: one long download starved a part-way plan for 20 min (F3). The
  decision tree is `foldin.fold_in` for every caller (CLI ones too): a
  part-way `names` plan resumes as names, a `full` one as full, a never-built
  store builds in full, else the payer-scoped incremental — and a full build
  ONLY when the incremental raises `RollupNeedsFull` (a failed precondition,
  logged with which); any other failure (out of memory at the finest
  slicing, disk) is noted (`_note_rollup_failure`) and the SAME update is
  retried after 2, 4 … 60 min. Pause (`meta('analytics_paused')`) holds
  everything, a running plan stops between two committed parts
  (`RollupPaused`, not a failure); Run now bypasses the cadence (and a pause,
  for that one run). The incremental recomputes the spine for (payer, the new
  files' TINs) only — every spine row and is_latest contract lives inside one
  payer x TIN — and the directory reads only the parts holding the delta TINs
  (`part_tin_set`); a removal keeps the payer-wide spine.
- **How the panel knows** (1.2; `mrfx/activity.py`, `GET /api/analytics/
  activity` — async, single-flight, its own 2-token limiter). One coverage
  rule, `Store._coverage_facts`: a file is covered when the marker or a
  plan's committed slices include it; a payer when it is in the spine and not
  part-way. The header, `/api/payers`, the panel, `analytics_note()` and the
  document sentence all read it (they disagreed during a rebuild: 0 vs 3 vs
  4). Every commit point that can make a file visible (`update_rollups_
  incremental`, each full-plan spine slice, `_finish_plan`) calls
  `_note_fold_ins(kind)`, which appends one event per newly covered
  (filename, finished_at) to a 200-entry ring in `meta('analytics_events')`
  (`analytics_known_covered` is the set it diffs against; seeded without
  events on the first 0.8.0 run). "Why it is waiting" comes from
  `FoldIn.decide()`, never a guess; the ETA only after one measured unit.
- **The claim-release rule** (2.1). A content claim
  (`Store._content_claims`) is released on EVERY exit of the load that took
  it (`process_url_record`'s `finally`, `ingest_file`'s `finally`), records
  its owner (`_content_claim_owner`: the url id, or None for an inbox file),
  and a claim whose link is no longer loading in this process
  (`update_url` drops the id from `_live_url_ingests` on any non-loading
  status) is no owner: nobody ever waits on it. A failed load revives the
  rows that deferred to its BYTES (raw sha) and to its CONTENT
  (`duplicate_of` = its name, "will retry automatically").
- **Content identity of older files** (2.2): `backfill_identities` also names
  every file with a raw sha and no content sha from its kept copy (one read,
  recorded only when the raw sha equals the loaded one), one at a time,
  resumable; files with no copy are listed in `meta('content_backfill_no_
  copy')` and the Files tab says what that means. Every claim path also reads
  `files.read_identity` (sha AND csha since 0.8.0) when a kill skipped the
  identity row (2.11). Two copies of one document are twins only within one
  month (`_SAME_MONTH`, `fetch._same_name_month`; decided — the month a file
  name gives is never dropped).
- **Re-reads** (2.3): `requeue_skipped` leaves a file whose claim is held to
  its own end-of-read check, and a re-read that loses the claim
  (`CLAIM_LOST`) returns None — the first read's 'done' stands.
- **The cache-under-Stop rule** (2.9): a result computed while
  `progress.current().cancelled` is never stored — `Store.cached`,
  `spine_cached`, the client digest, the Overview (`store._run_cancelled`).
  A new cache with a fault-isolated compute must check it.
- **The backup consistency rule** (2.4): the parts a hot backup carries are
  listed UNDER the write lock, after the database copy (the pre-lock list is
  only a size estimate); `verify_zip` checks that the zip's parts hold the
  row count its database recorded (parquet footers, no inflate); a failed or
  stopped write leaves no `.zip.part`; a `backup_dir` inside the store is
  refused; a drive that cannot hard-link copies OUTSIDE the lock, checks the
  data drive's free space first, and refuses if a part changed meanwhile.
  The writer lock is held ~25 s per GB of database file (the Files tab says
  how long for this store).
- **SSN-pattern subjects** (2.5): `add_client` refuses one; a client saved
  by one before 0.8.0 is shown as `subject_label` (MASKED-SSN-<hash>) in the
  digest, folder names, titles and stdout, its jump buttons are disabled,
  every report page / PDF / packet document goes through `mask_ssn_text`,
  and `hashFor` never writes one into the address bar.
- **needs-full generations** (2.6): `rollup_needs_full` holds '1' (pre-0.8.0)
  or '1:<id>' (a new id per raise); a build clears only the token it started
  with. **Monthly re-check** (2.8, decided): probe only — a file an index
  lists for the first time is named on the index row, downloaded only by a
  re-paste or `monthly_refresh`.
- **Group 3**: PDFs print in a child process (`python -m mrfx.pdf`) that a
  Stop or `PRINT_TIMEOUT_S` kills, refused above `PAGE_BUDGET` pages with a
  sentence; `packets --pdf` skips a document that fails. A Stop during an
  export's compress removes the zip; a Stop on a finished run answers
  `{"stopped": false}`. The progress text bar prints one line per OUTER step
  on redirected output; a nested step reads "Outer: inner". A multi-link
  paste is durable (`meta('pending_paste')`, `resume_pending_paste`). A
  hospital of an already-loaded name from another file is refused.

### 0.8.2 additions (Nov 13, 2026) — the directory source in pieces

Field log (the same 1.18 B-rate store, `duckdb_memory_gb: 8`, a 0.8.0 plan
resumed by 0.8.1 at directory part 59 of 74): 0.8.1's `_dir_source` ran
DIR_SOURCE_SELECT over the whole store as ONE statement — out of memory
after ~11-13 min ("could not allocate block … 7.4 GiB/7.4 GiB used") — and,
because a failure was not remembered, again before EVERY remaining
directory part (~16 min per part instead of ~5). Pinned by
`tests/mrfx/test_v082.py` (4 of 5 fail on d962004; the 5th is a
no-step-backwards pin).

- **Pieces** (`Store._dir_source_chunks`): payer x code ranges cut by
  `rollplan.spine_slices` with a budget of `_rollup_partition_rows x
  DIR_ROWS_FACTOR` rows, from the plan's own spine slices (deduped by
  payer/lo/hi/null_code — hash copies share a range) or, for a names-only
  plan, from a `payer, billing_code` count. Each piece is one INSERT
  committed WITH the resume pointer, so a kill never doubles a count
  (`sum(n)` must equal the rows exactly). An out-of-memory piece is replaced
  by `rollplan.halve(piece)` and retried; `halve` returning None is a
  failure. The union is the plan's snapshot (the same rule as the spine:
  a later file is healed by the next update).
- **`meta('rollup_dir_source')`**: the plan id alone = ready (0.8.1's
  format, so a source 0.8.1 finished is reused); JSON `{plan, chunks, next}`
  while building; JSON `{plan, failed: true}` after a failure — the plan's
  directory parts then read the raw store and the source is NOT tried again.
  A `duckdb.InterruptException` (Ctrl+C) is re-raised, never recorded as a
  failure.
- **Measured** on the 99M store at `memory_limit = 600MB` (the field ratio):
  0.8.1 out of memory; 0.8.2 15 pieces, 28 s, 17,349,651 source rows,
  sum(n) = 99,000,000 = every rate with a TIN.

### 0.8.1 additions (Nov 13, 2026) — the field store's analytics; rules a change must keep

Found on the user's store (1.18 B rates, 50 payers, 465 files, a Windows
laptop): the analytics sat at "3 of 50 payers" all night. Pinned by
`tests/mrfx/test_v081.py` (8 of its 10 cases fail on 7f60026 with the value
in their docstrings; the other 2 are no-step-backwards pins).

- **Cost rule for an update** (`Store._apply_rollup_delta`). The payer-scoped
  incremental is ONE transaction cut into hash partitions, and every
  partition re-reads ALL the affected rows (a hash predicate never skips a
  row group): cost = partitions x affected rows, nothing visible until the
  end, everything lost on a restart. When that exceeds one pass over the
  whole store it raises `RollupNeedsFull("a large update …")` before writing
  anything, and `foldin.fold_in` runs the durable plan instead. The field
  case: 456 files, 1.135 B rows, 296 partitions (~336 B rows read). On the
  99M store: 0.8.0 was at partition 12 of 33 after 240 s, nothing visible;
  0.8.1 handed over and finished in 406 s with payers visible from ~12 s.
  A small update (one partition) is unchanged. Never route a large delta
  through hash partitions again; a payer-scoped PLAN (spine slices for the
  affected payers only) would be the next improvement.
- **Payers first** (`Store._new_plan`): a new full plan is spine slices, then
  the prune, then the directory — the directory is independent of the spine
  and a payer is on screen when its spine slices commit. A plan stored by
  0.8.0 (directory first) resumes in ITS order; `_plan_versions` did not
  change, so a running build is never discarded by this release.
- **The directory in one pass** (`DIR_SOURCE_SELECT`, `Store._dir_source`,
  `_dir_query_over`). The directory reads the raw rates three ways — (TIN,
  NPI) pairs, rows per (TIN, discipline), (TIN, TIN type) — all exactly
  derivable from ONE aggregate grouped by (tin_value, npi,
  tin_is_really_npi, tin_type, discipline) with count(*) AS n. A plan with
  k > 1 directory slices builds that aggregate once (table
  `rollup_dir_source`, `meta('rollup_dir_source')` = the plan id; kept across
  a kill, dropped by `_finish_plan` and by `_new_plan`), and each slice reads
  it. Byte-identical: real files (spine 190,652 / directory 3,511 rows) and
  the 99M store (79,027,749 / 194,000, fingerprints equal across 0.8.0, 0.8.1
  and a kill-and-resume). A file landing after the source was built reaches
  the directory through the next update (its TINs are recomputed from every
  part holding them — the coverage snapshot rule). `_dir_query_over` falls
  back to the raw query unless every read was rewritten exactly once: a
  change to TIN_DIRECTORY_QUERY's reads must update `_DIR_READS`.
- **The header never waits** (`/api/stats`): its body is computed by ONE
  background thread at a time; a request waits at most `api.STATS_WAIT_S`
  (8 s) and otherwise answers with the last body (or, before any, a minimal
  one from the files table). A part-swap error in that computation is asked
  again once (the SwapRetryMiddleware rule, review 11 1.8). `store_stats`
  never runs a raw count(DISTINCT) over `rates` (payers from the files table,
  TINs from the per-part sets, never below the last shown);
  `analytics_coverage` is single-flight; `spine_payers` recounts at most every
  30 s while a build runs. Measured on the 99M store during a build: median
  0.03 s, p95 0.21 s, max 6.8 s (the field log: 5 s .. 55 min).

#### The skip protocol in one page (amended in 0.7.7; rules 4, 7, 8, 9 changed)

1. **A LINK is one queue row per `dedup_key`** = `scheme://host(lower)/path(as is)?sorted query minus signature params` (always: `expires signature policy key-pair-id awsaccesskeyid sig x-amz-<signing>`, GCS V4 `x-goog-*`, Akamai `__token__`, a lone JWT `token`; beside a signature param also the Azure SAS names and `token/st/policy` only when the value LOOKS like a signature part — JWT, date, epoch, `%`/`=`/`+`/`/`, 40+ hex). `http`/`https`, `:443`, trailing slash, percent-encoding and host aliases are DIFFERENT rows; content decides later. A paste of an existing key refreshes the row's url (not in flight) and revives `failed` / `skipped` / `oversize` / a done index; a known duplicate pasted again is answered from what is loaded, no byte downloaded. **A done RATE row is re-read by a paste only when billing codes were added since it was read**; otherwise only forget + paste, the monthly re-check, `requeue_url_for_file`, or a re-expansion that finds it republished.
2. **Children**: `expand_toc` takes every http(s) location (no name filter, deduped by key, capped and SAID); the UHC/Optum `blobs` listing drops allowed-amounts and drug files by WHOLE name tokens and the listing row says what it left out; a child the re-expanded index no longer lists says so on its row; page links are only RANKED by name.
3. **Early skip (15 MB)**: size + head equal to a LOADED file → fetch tail + 8 windows by Range; SKIP only when gz/zip on both sides and the twin's `trailer_proves = 1`; an earlier in-flight match WAITS. Plain `.json`, multi-member gz, and anything unprovable download in full. The row says "checksum-equal to <url> … skipped after X MB", `duplicate_of` set.
4. **Identity = sha256 of the RAW bytes AND sha256 of the CONTENT** (both hashed while downloading / in one read of an inbox file, any size). The same content re-encoded (another gzip level, `.json` vs `.json.gz`, a BOM) is ONE file: the second is `skipped/duplicate` "the same content as X".
5. **One content claim for every path** (`claim_content_ingest` / `claim_inbox_content`, one lock, raw sha OR content sha): a loaded twin → `skipped/duplicate` with the partner named; a twin loading now → defer (link) or wait (inbox), revived on the twin's failure, at start-up, or by `record_identity` when the twin's content is replaced. Exactly-once holds under concurrency and failure.
6. **Same name in the inbox**: 'same' = recorded size+mtime (the identity row, or — after a kill between the 'done' commit and that row — `files.read_identity`, which the commit itself carries), or equal full sha, or sha-equal to the processed copy. A file with no identity row and no copy is never 'same' on size and age: it is HELD for Replace / Keep both (`/api/files` marks such files; `serve` names them from their copies in the background). Different bytes, same payer+month → HELD; another month → loaded beside as `name-<sha8>`.
7. **Re-check of a loaded link** = one 1-byte ranged GET comparing EVERY signal: a different validator OR a different size is a republication → re-download; same validator and size → unchanged; a probe that states neither → "could not tell" (re-downloaded on the monthly re-check). Every probed child row says what was checked and how ("checked <date>: unchanged by the same version tag and size").
8. **Monthly re-check is ON** (`monthly_recheck`): once a month every LOADED index — catalog or pasted by the user — is fetched again and its loaded files probed (rule 7); only changed files download; the index row says "last checked … · next check …". The opt-in `monthly_refresh` still queues NEW dated months of catalog sources.
9. **0-row files** are done, fingerprinted and anchor dedup like any file; files read before billing codes were ADDED are counted on the Files tab and re-read by its button (`files.code_set`); `mrfx ingest --force` still re-reads everything.
10. **Kind** is decided by content (first 1 MB of keys), never by the file name; the zip member read is the largest `.json`.
11. **Forget** keeps the url row as `skipped` (sha kept, twins neutralised) so a re-paste reloads; the early/sha skip text always names the partner; a provider list that lands while its payer's rate file is being read is applied (5.2.7).

## Step 4 — Know the URL pipeline states

The whole machine, derived by instrumenting every status-changing store
method (review 11, `docs/review11_repros/queue/FINDINGS.md` §1, re-checked
after the round-11 fixes). Keep it written down ONCE, here.

```
<new> ──add_urls / _enqueue_children / _maybe_queue_prev_month─────────────► queued
queued ──next_queued_url (downloader claim)────────────────────────────────► downloading
queued ──small-file slot sized it big (1-byte probe) ─────────────────────► queued (big_lane)
downloading ──fetch_url_record ok (or a validated kept download reused)───► fetched
downloading ──re-queued loaded row, server unchanged (revalidate=1)───────► done
downloading ──DownloadError terminal──────────────────────────────────────► failed | oversize (bytes_total kept)
downloading ──disk full / set aside (retries+1) / handoff / park limit────► queued   (same row, no slot)
downloading ──park_url (silence < stall) / pause_url (stall, auto_resumes)► paused   (park_count>0 = parked)
downloading ──park_waiting (early-dedup "wait" on an EARLIER in-flight row)► paused  (waiting_on set)
downloading ──early-dedup "skip" (_apply_verdict, checksum-equal)─────────► skipped/duplicate (duplicate_of)
downloading ──cancel_all_downloading ("stop downloads")───────────────────► skipped/canceled
fetched ──next_fetched_url (processor claim)──────────────────────────────► ingesting   (never fetched→expanding)
ingesting ──the download vanished: downloaded again────────────────────────► downloading → (continues)
ingesting ──TOC / blob listing─────────────────────────────────────────────► expanding
ingesting ──done twin / claim lost (link OR inbox twin, loaded or loading)─► skipped/duplicate
ingesting ──allowed_amounts / parse "stopped" by the user─────────────────► skipped
ingesting ──ingest done / page links found─────────────────────────────────► done
ingesting ──ingest failed / unknown shape / preflight error───────────────► failed
ingesting ──ingest pending_confirmation────────────────────────────────────► oversize
ingesting ──ingest "requeued" (pool restart / shutdown)───────────────────► fetched
expanding ──expansion ok, row still 'expanding'────────────────────────────► done (kind toc; children queued)
expanding ──the user cleared it meanwhile (enqueue, re-check, or the final
          write — each checks 'expanding' in its locked step)─────────────► (stays skipped; nothing queued)
expanding ──no in-network files / parse error─────────────────────────────► failed | skipped
paused ──wake_paused_urls (timer, 60 s coordinator, start) / wake_parked_urls
       (idle downloader) / user retry ("try now") / requeue_failed─────────► queued
paused ──user skip / clear_queued──────────────────────────────────────────► skipped
failed ──user retry / requeue_failed / re-paste / re-expand / force_size /
       requeue_urls / recover_stuck_urls revive (duplicates of a FAILED twin)► queued
failed ──user skip─────────────────────────────────────────────────────────► skipped
skipped ──user retry / force_size / direct re-paste (never a re-expand) /
        resume_canceled (kind canceled) / revive_skipped_duplicates (kind
        duplicate; a link or INBOX twin failed) / record_identity /
        recover_stuck_urls (kind duplicate deferred to an inbox file whose
        load did not finish, no live part under its name)─────────────────► queued
oversize ──user retry / force_size / re-paste / requeue_oversize_within
         (size known and now fits) / re-expand (only if it now fits)───────► queued
oversize ──user skip───────────────────────────────────────────────────────► skipped
done ──requeue_urls (monthly refresh, revalidate=1) / requeue_url_for_file
     (provider list arrived, revalidate=2) / a re-expanded index found it
     republished (_refresh_loaded_children) / user retry or re-paste of a
     done toc / page────────────────────────────────────────────────────────► queued
done ──forget_file─────────────────────────────────────────────────────────► skipped (content_sha kept)
downloading|fetched|expanding|ingesting ──recover_stuck_urls (start) /
       release_claim (dead thread)─────────────────────────────────────────► queued
queued|fetched|expanding|paused ──clear_queued────────────────────────────► skipped/canceled
```

`kind`: `toc / page / in_network / provider_reference / allowed_amounts /
duplicate / canceled / unknown`. A `paused` row with `park_count > 0` is
PARKED: its server sent < 1 MB for `download_park_seconds` (240); it rejoins
when the cool-down (`download_park_minutes`, 10) ends or at once when nothing
else is queued; `download_park_limit` (6) fruitless parks in a row fall
through to the long pause (`pause_url`, `download_pause_hours`, one of
`download_auto_resumes` — reset by every user / bulk requeue). `oversize` is a
distinct terminal state ("too big", amber) with "download anyway"
(`force_size_requeue`, a one-shot flag). User actions (`_USER_TRANSITIONS`):
retry from failed / skipped / oversize / paused (+ done toc/page); skip from
queued / failed / oversize / paused. A done rate row is touched only by the
refresh / reload / republished paths above (it anchors content dedup).
`list_urls` pins every in-progress row, every pasted link and every
actionable row (failed / skipped / oversize / paused, whatever its parent).

The `files` table (inbox side): `processing → done | failed | quarantined |
stopped | skipped (a copy of a loaded file) | pending_confirmation |
needs_choice (a different file under a loaded name)`; `queued` is a promise
of a load in THIS process ("Keep both", Replace, "ingest anyway", a pool
restart) — at start `recover_stuck_files` turns an unowned one into `failed`
(or back to `done` when its rates are live) and the scan loads it from the
inbox, `inbox/.held/` or `inbox/.replacing/`.

## Step 5 — Performance envelope (measured)

- CLASSIC path (ijson; `fast_scan: false` or a declined file): parse ≈ 30–35
  s per uncompressed GB per pass. The ijson floor is the per-token yield
  (~25 s/GB with an empty loop body). The bullets below about dead ends
  apply to THIS path; the fast path (further down) is what normally runs.
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
- **Fast path (Nov 2026, `mrfx/fastscan.py`, `fast_scan: true` default)** —
  the new data that re-opened the "dead ends" above: the ijson floor is the
  per-TOKEN yield (~25 s/GB measured with an empty loop body), so the fix is
  to not tokenize non-target bytes at all. A numpy splitter finds top-level
  `in_network[]`/`provider_references[]` member boundaries (string- and
  escape-aware), regex-peeks each member's OWN billing_code/type/ref-id, and
  `json.loads` only target members into the SAME `_handle_item`/`_add_ref`.
  Memory envelope unchanged (target items were already built whole). Second
  fix: rows go to the sink as a columnar `RowBatch` (one dict per PRICE, not
  per row) — `from_pylist` over 20-key row dicts was ~52 s/GB on its own at
  ~12M rows/GB. Synthetic 0.63 GB row-dense file, two-pass total:
  165.5 → 44.0 s/GB (3.8x); output Parquet identical to the pre-change code
  (7,530,087 rows). Escape-heavy variant: 77.7 s/GB, also identical. Pinned by
  `tests/mrfx/test_fastscan.py` (hostile shapes at 1-byte..4 KB chunks + seeded
  fuzz). Declines (non-object top level / empty) fall back to ijson up front;
  a missing numpy degrades to ijson with one warning.
  - **Bulk skip** (`_ChunkKeys`): per 8 MB chunk, own-level `billing_code` /
    `billing_code_type` / `provider_group_id` keys are located with numpy at
    the quote positions, values read straight from the byte array, targets
    tested with `np.isin`. Members that are provably "skip, no side effect"
    never reach Python: a plain non-target code (+ plain CPT/HCPCS type or
    none), a plain other-system type (MS-DRG/HIPPS/RC — its exact
    `billing_type_other` count is computed in bulk), a plain ref id outside
    the kept set. ANYTHING else (target, numeric/escaped/padded code, dupe
    keys, backslashes, members straddling a chunk edge) takes the exact
    per-member path, which replays every own-level key in stream order like
    the ijson loop (and hands \u-escaped KEYS to ijson itself). Real Florida
    Blue file (2M tiny items, 1.6 GB): 1.0x -> 1.9x after this.
  - **Strictness parity**: anything the classic parser REJECTS inside a member
    we decode fails the file on the fast path too — NaN/Infinity/overflowing
    numbers (never a NaN rate in a median), bytes after the document end
    (doubled download), bad UTF-8 in a decoded member. Lone \uD800 escapes
    become '?' like classic. ONE DELIBERATE difference: corrupt bytes inside a
    NON-target item (never decoded) no longer fail the whole file — the valid
    PT rows are kept (pinned by test_corruption_inside_a_non_target_item...).
  - **Verified on real files (Nov 2026)**: 15 files from BCBS WY/MS, Florida
    Blue, UHC, Moda and Tufts (.json.gz and .zip, 0.01–10 GB uncompressed,
    up to 13.5M rows) produce row sets IDENTICAL to the pre-change code
    (DuckDB EXCEPT ALL both ways) with identical QA counters; 1.2x–4.6x
    faster end to end. `mrfx fastcheck <files>` repeats that check on the
    user's own files. Five anonymized real-shape slices live in
    `tests/mrfx/fixtures/real/` with golden counts.
  - **Testing discipline that found real bugs**: the suite was mutation-
    tested (each safeguard deliberately broken; every mutation must fail a
    test). Several tests only compare fast vs classic — a bug in code they
    SHARE (RowBatch, the EOF restamp) breaks both identically, so those
    have absolute golden expectations too. This caught a pre-existing bug:
    header-at-EOF rows could flush with the seed payer during deferred-ref
    emission (now restamped first).
- **Peak memory scales with the largest `in_network` ITEM, not the file**
  (review 11 `ingest/s6g_mem.py`): invariant 1 holds for file size, but one
  item whose groups are all deferred (every group cites a companion) is held
  whole — 5 items of 150k–300k groups: 100 MB raw -> 577 MB fast / 327 MB
  classic, 200 MB raw -> 909 / 516 MB (baseline ~250); the same groups as
  7,500–15,000 items of 100 -> ~300 MB flat. One code carrying every provider
  of a national payer therefore costs ~0.6 GB per worker on the fast path,
  x `parallel_ingests` — the sizing case beside Aetna's 1.92 GB.
- **Per file, after round 11** (`ingest/s9b_opens.py`, 35.5 MB real UHC gz,
  inbox): the file is read twice — one identity pass (sha + fingerprint) and
  the parse — 71.3 MB = 2.01x, 7 opens (was 88.6 MB = 2.49x, 10 opens). A link
  is hashed while it downloads; its fingerprint is carried to `_finish_ok`.
- Big files (≥1.5 GB uncompressed, estimate = max(18x compressed, the size
  the gzip trailer / zip directory records)) — fast path: `run_big_file_fast`
  reads the file ONCE: non-target members skipped in bulk, TARGET in_network
  members spooled verbatim to `.{key}.{pid}.spool.tmp` (deleted after; swept
  as an orphan if killed), cited ref ids collected, references that come
  AFTER in_network kept in the same read. References that came BEFORE it
  need a second read that STOPS when provider_references closes (never
  re-scans in_network). Then the spool is replayed through `_handle_item`.
  Memory = kept refs + one item. Measured: 42.4 GB real BCBS file 3,676 s
  (original code) -> 434 s, peak RSS 0.32 GB; 5.5 GB UHC 423 s -> 48 s.
  Classic path (fast_scan off / declined): pass 1 skims target-cited refs
  (80k ids on an 8 GB Anthem shard vs 4.2M total), pass 2 extracts.
- Inside one read: decompression runs in a prefetch thread (zlib releases
  the GIL); the scan core is one `bytes.translate` + one `nonzero` on a
  bool view + a parity cumsum (2x the compare/gather/searchsorted version
  on string-dense UHC data); keys are found with one C regex pass per chunk;
  Parquet encoding + zstd runs in `BackgroundParquetWriter` (it was 48% of
  a row-dense file's time). What remains per chunk is numpy over every byte.
  Measured (5.5 GB UHC, quiet 4-core box): decompression alone 1.4 s/GB;
  decompression + full byte scan with nothing kept 6.9 s/GB; full single read
  with rows 34.6–48 s total. A compiled (numba) scanner was EVALUATED, not
  built: numba has Windows wheels for cp311–cp314, but removing most of the
  30 s scan projects to ~1.9x on scan-bound files and ~1.05x on row-bound
  ones (Aetna) — under the ">=2x on real files" bar. Revisit only with a
  measured prototype.
- Per worker: 1.2–1.5 cores busy (scanner + decompression thread + Parquet
  writer thread): UHC 1.22, row-dense WY 1.53, Aetna slice 1.38. The auto
  `parallel_ingests` (cores-1, cap 8) therefore slightly oversubscribes.
  Measured curve, 8 real files over local HTTP on a 4-core box (enrichment
  off): 1 worker 153.5 s, 2 -> 87.0 s, 3 -> 87.2 s, 4 (clamped to 3) ->
  87.9 s. Plateau at ~cores/1.4; extra workers cost memory, not speed. The
  default was NOT changed — it loses nothing measurable, and an 8-core curve
  (expected plateau ~5) is still unmeasured (NEXT_STEPS_PROMPT P2.3).
- Row production (RowBatch) is factored by provider group: one entry per
  distinct group + one (price, group) pair per fan-out, expanded with numpy +
  Arrow take; each group (therapy filter, TIN-only) is decided once per item,
  not per price. Real files: therapy-only Aetna slice 56.5 -> 48.3 s;
  default mode ~4%. Kept reference tables share one string object per
  distinct NPI/TIN: Aetna's 7.4M NPI slots / 2.3M distinct, 606 -> 408 MB.
- Big-file single read DECLINES (before any row; falls back to the no-spool
  two-pass) in all-codes mode — the spool would be the whole in_network
  section — and whenever free disk is under 2 GB (checked up front and every
  64 MB of spool). Its progress bar has three phases (scan 0-80%, reference
  re-read 80-85%, replay 85-99%), since a row-heavy file spends most of its
  time replaying.
- ROW VOLUME, not bytes, bounds some payers. A real Aetna-generated file
  (1.54 GB gz / 20.4 GB JSON) yields 590,311,124 PT/OT/SLP rows across
  1.92M distinct NPIs — every specialty that bills a 97xxx code, fanned out
  over provider groups. Full-file comparison with the ORIGINAL code (Nov
  2026): IDENTICAL rows (digest, both directions), QA counters, item counts
  and header; 4,071 s -> 1,082 s (3.8x); peak RSS 3.19 GB -> 1.92 GB. The
  scan is minutes; the rest is producing/writing rows. For such books
  `therapy_only_ingest` (needs the NPPES cache) is the lever: it drops
  non-therapist NPIs at parse time, before the row is ever built (slice:
  60M -> 6.3M rows, 87 -> 48 s, 2.0 -> 0.9 GB).
- Downloads: the content hash carries across reconnects (it used to re-read
  the whole .part on every resume); gzip-encoded bodies are stored as sent
  (decoding them corrupted Range resumes). Companion reference files and
  TOC plan indexes insert via one Arrow table (was ~420 rows/s under the
  write lock).
- Parallel: N worker processes (`parallel_ingests`), N downloader threads
  (`parallel_downloads`, 0 = auto = 6) prefetching
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

- `.venv/bin/python -m pytest tests/ -q -m quick` — the inner loop (CLAUDE.md
  has the current count and time; the >= 0.5 s tests are listed in
  `tests/slow_tests.txt` — regenerate it from a `--durations=0` run; tests
  marked `slow` / `stress` are never quick).
  Run the full suite before every commit that touches numbers.
- `tests/mrfx/test_repros.py` (slow tier) RUNS the repro scripts of rounds
  11-14 and asserts their recorded "after the fix" lines; add a round's
  fast repros to its CASES table when you close its items.
  `MRFX_STRESS_N=50 pytest tests/mrfx/test_stress.py` runs the two race
  tests 50 times each beside a CPU hog (the release check).
- `tests/mrfx/test_known_answer.py` — the hand-computed end-to-end answer
  check (ingest -> rollups -> API -> CSV/ZIP, masking, month rules). If a
  change moves one of its numbers, the change is wrong or the rule changed —
  say which in the commit.
- `.venv/bin/python -m pytest tests/ -q` — the suite (CLAUDE.md has the count) runs real
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

`config/known_sources.yaml` holds 150 entries — 109 auto-queueable sources +
41 probed portals (README's counts are tested against the file), each with verified dates and real row counts — treat it as ground
truth for "does this source work and what should it yield". The largest
verified single-file ingests: Cigna CHLIC 243.4M rows, Aetna CA 83.7M, UHC
Charter 617 64.0M (8.85 GB gz), NC PPO 23.7M, upper-midwest zip 19.8M.
Guards verified live: 35.2 GB refused by confirm_over_gb, 25.1 GB by the
disk-space guard, both with actionable messages.

## Step 8 — Known limitations / next frontiers

**Measure on a NAMED store, and read the plan.** The round-3 close-out found
that `api._TIN_JOINS` joined `npi_directory` with a one-sided predicate in its
ON clause (`ON t.tin_is_really_npi AND nn.npi = t.tin_value`); DuckDB can only
run that as a BLOCKWISE_NL_JOIN, so once practice names were loaded the
Explorer table took 158 s and the Code page 10.9 s on the 32M-row store (0.9 s /
0.13 s unnamed — every earlier timing had been taken before names existed).
Rewritten as `nn.npi = CASE WHEN t.tin_is_really_npi THEN t.tin_value END`
(same rows, hash join): 1.2 s / 0.17 s. Rules: time endpoints on
`scratchpad/perf/named` (or any store with `npi_directory` populated), keep
`EXPLAIN ANALYZE` output for every relation you change, and never write a join
condition that isn't an equality between the two sides
(`test_no_grain_relation_plans_a_nested_loop_join` pins the grain relations).
`docs/NEXT_STEPS_PROMPT_4.md` was the output-speed plan (executed; status block);
`docs/NEXT_STEPS_PROMPT_5.md` was the round-5 plan (install robustness, idle
polling, the DuckDB visibility bug, the Explorer's then-pooled default (round 10
made "latest" the default), joined-column
filters — executed; status block at its top). `docs/NEXT_STEPS_PROMPT_6.md` was
the round-6 audit of every output (executed; status block at its top): each
payer's own months for packets, digest, rate changes and new-to-network; the
500s and state-name misses; tie orders; and the per-visit costs.
`docs/NEXT_STEPS_PROMPT_7.md` is the current plan: a whole-program audit
(Nov 7, 2026 — measurements at HEAD on both stores, determinism, profiles,
peak RSS, a real inbox run, and eight independent line-by-line reads). Its
P0s are wrong numbers on sold outputs (NaN string rates, the negotiation
one-pager's cross-payer total, cross-payer sparklines, tied-schedule leads,
a merged inflation basis, name-only referral grouping, ACS senior
over-count); its P1s are the shared per-practice-median cost (materialise
once per generation), exports on the system drive, round-6 cache staleness
on user edits, queue/updater/launcher holes, and the cross-site POST
surface.

**Round 6 lessons.** *A store-wide "newest month" is a bug, not a
simplification:* one payer's file dated two months ahead (HEALTHSCOPE, 76 rows
in 2026-12) emptied every other payer's "this month". Months are per payer
(`monitor.payer_months`). *Cache by data generation, hand out copies:*
`Store.cached` / `@generation_cached` keep whole-store answers until the next
rollup; a caller that decorates its result gets a deep copy. *Placement is a
file, stamped by its inputs:* `Store.placement_relation()` writes
`tin_zip_place.<sha of part signatures + NPI-directory size/ZIPs/last
enrichment>.parquet`, so any change to either yields a new file on the next
request and never a stale one — and it is built per (TIN, NPI) before the
directory join (39 s -> 6 s at 99M: the therapy-taxonomy LIKEs had run on every
rate row). *Count the newcomers before taking medians* (new-to-network 10.7 ->
4.4 s). *Every client document is scoped to one state* — the dossier now
defaults to the practice's own state, labelled; leverage keeps the caller's
market because its geography is the radius. *Playwright's sync API delivers
route callbacks and events only inside Playwright calls:* wait with
`page.wait_for_timeout`, never `time.sleep`, and fire a promise you mean to
hold with `evaluate("() => { f(); }")` — `evaluate("f()")` awaits it.

**Round 5 lessons.** *Every panel writes to its own box:* the Leads tab's
closures panel and the Clients panel both used `id="cl-out"`, so "Find
closures" rendered into the Clients digest on another tab and the Leads tab
showed nothing (`tests/mrfx/test_web_page_ids.py` now fails on any duplicate id
or any `$("#x")` with no element). *Read the key the producer writes:* the
dossier's weakest-payer table read `benchmark.summary.*`, which only the
one-payer comparison builds — every dossier printed "–" in both columns. *A
label states what was counted:* leverage said "therapy practices" over a scope
that included physician offices; a payer table under "What 97110 pays" showed
each payer's all-code median. `tests/mrfx/test_known_answer_leads.py` works
every Leads output out by hand (underpaid list + CSV, leaderboards, new to
network, new clinics, closures, dossier, leverage, market report).

**Round 4 (output speed, docs/NEXT_STEPS_PROMPT_4.md status block).** Every
dashboard endpoint is snapshotted by `scratchpad/perf/snapshot.py` (bodies,
normalized for clocks) and diffed byte for byte against the previous code on
the named 32M store AND a 99M-rate, three-month, named synthetic store
(`scratchpad/scale4/build.py`; its 79M-row spine is denser than a real 300M
store's). What it taught:
- *"Latest" as a per-request window was the O(store) cost.* It is now the
  spine's stored `is_latest` (see the store row above). Never re-introduce a
  `QUALIFY ... OVER` over `rates_by_tin` on a request path; the NPI grain
  keeps one only because it is always narrowed to one ID or a payer+code.
- *Test the path the big store takes.* Builds above ~15M rows run in hash
  slices; every test store is one-shot. The first stored-flag build set the
  flag with an UPDATE after the slice INSERTs — invisible rows, 0 of 79M
  flagged, all tests green. Force `_rollup_partition_rows` small in a test.
- *Removing a pipeline breaker can change memory, not just time.* Without the
  window, the entity aggregation's four DISTINCT aggregates ran out of memory
  (the window had happened to feed them clustered rows). One `list()` per
  column + `list_distinct` gives the same values without a hash table per
  DISTINCT. Re-run the whole-store views after any plan change.
- *Joins only where a column is read.* `api.stat_rel` feeds the aggregate
  views off the bare spine when no filter needs a joined column; the table's
  page-first late join now covers "latest". Still joined: any state / city /
  therapy / name filter (a semi-join rewrite of those unit filters is the next
  lever — measured 2.4-3.9 s at 79M spine rows, see the report).
- *Running every endpoint twice finds invariant-7 bugs nothing else does:*
  `mode()` ties, the subject picker's LIMIT, new-to-network, leverage,
  renewals, the 5,000-row rate-change cap, the export's masked-alias sort.
- *Cache by what the answer depends on.* The header's spine counts are held
  per `data_generation`; the rate-NPI population per part file
  (`rate_npi_set`) — both were recounted every minute the dashboard was open.
  (Round 4 first kept it as one whole-store table rebuilt under the writer
  lock: 80 s at 99M rates per file landing. Never scan the store under
  `write_lock`; scan outside, apply in a short transaction.)

Round 3 (docs/NEXT_STEPS_PROMPT_3.md, status block) — still open after it:
- Windows has STILL never been run by an agent (P1.5). The round-3 Windows
  fixes (`_nt_alive_without_handle`, parked part swaps, UTC timestamps under a
  non-UTC clock) are tested on Linux with simulated conditions only.
- P1.6(d) the rollup OOM ladder re-runs tables it already committed; (e)
  `unenriched_npis` is O(store) per call in API mode. Performance, not numbers.
- P2.1 (COPY ... PARTITION_BY full rebuild) is needed before the next
  ROLLUP_SCHEMA_VERSION bump — round 3 avoided one (the modifier and month
  rules are row rules: `store.ROW_RULES_VERSION` rewrites only affected parts
  at open; the directory change is a DIRECTORY_SCHEMA_VERSION bump, minutes).
  P2.2 (sorted parts) and P2.4 (re-measure worker RSS on the full 590M-row
  Aetna file) not done.
- `.7z` (Baylor Scott & White) and `.zst` (MercyCare) need py7zr / zstandard —
  not added (new dependencies on the user's machine); the queue says
  "unrecognized file" for them.
- The download retry "as a browser" (fetch.BROWSER_UA after a 403 or repeated
  early closes) and the JS-portal renderer present a desktop-Chrome
  User-Agent. That is pre-existing behaviour for legally public MRF data;
  `mrfx sources --check` deliberately does NOT use it (it asks only as MRF
  Explorer and reports NEEDS-BROWSER instead). Whether the product should keep
  the fallback is a decision for the owner.
- Entity key = organization (NPI-2) name + `tin_directory.primary_state`; a
  TIN named only after a clinician is its own entity. A chain that operates
  one TIN per state therefore shows one entity per state (by design — a
  market is a state); merge them with entity_map.yaml if wanted.

Open after the Nov 2026 performance/correctness pass (docs/NEXT_STEPS_PROMPT.md
lists the full audit plan; these are what it left open):
- Remote `location` provider references are NOT downloaded. The groups that
  depend on them are skipped, counted (`qa.remote_ref_groups`) and named as
  such in the Files tab / `mrfx ingest`. None of the 22 real files scanned
  in full (8 payers/generators, up to 42 GB) used them; build a fetcher only
  when a target payer does.
- `mrfx add` (CLI) name lookups after the drain: FIXED in round 2 — it now
  prints how many NPIs remain, a progress line every 15 s with an ETA, points
  at the bulk NPPES file for big books, and Ctrl-C skips the rest cleanly
  (`tests/mrfx/test_cli_enrich.py`).
- Month semantics (round 2, pinned by `tests/mrfx/test_known_answer.py`):
  the Explorer's default is "latest" since round 10 (each contract's newest
  month; picking "all months" shows every ingested month, each row carrying
  its Month, and the sidecar says "ALL months pooled" — round 5 had kept that
  as the default and made its
  strip cost once per data generation instead); Code comparison and its export use each
  contract's NEWEST month (`FilterSet.latest_only`, `latest=1`), as do the
  report tabs. A payer republishing a byte-identical file under a new name is
  skipped by the URL queue's content-sha dedup, so that payer's "latest" stays
  the OLD month label — honest, because the file's own header date is
  unchanged; when the month came from the FILENAME (no header date) the label
  lags the republication. Inbox drops have no content dedup: a twin IS
  ingested and merges in dedup (source_count rises, numbers unchanged).
- A worker notices its parent died only at its next progress report; one
  that dies in a small file's last seconds may finish and leave its temps for
  the next start's sweep (swept then; big files report through replay).
- Not measured here (Linux, 4 cores): the parallel_ingests throughput curve
  on an 8-core box, and ANYTHING on Windows — no agent has run this repo on
  Windows. Reviewed by reading only: `_pid_alive` uses OpenProcess there
  (the /proc zombie check is POSIX-only; unsure -> "alive" = keep the file);
  the parent's temp/spool/progress cleanup is retry-tolerant and can no longer
  raise out of a `finally` (round 2 fix + test). HOW_TO_RUN.md Part 4 is the
  user's first-Windows-run checklist.

Deferred from the July 2026 comprehensive audit (real findings, consciously
not yet built — pick these up before adding features):
- Signed-URL re-expansion: a TOC's children hold signed URLs captured at
  expansion; after a long pause they 403. The .part is now KEPT on 403 and a
  fresh link resumes it, but there is no auto-re-expand of the parent TOC on a
  child 403, no re-expand action on a 'done' TOC row, and revive-on-re-add
  does not update a child row's stored URL. A wall of 403s after a multi-hour
  pause on mrf.bcbs.com/Anthem still requires re-pasting links — since round 2
  each such row's error names the exact index URL to paste again.
- Per-file QA aggregates (outliers, duplicate ratio, TIN=NPI rows): computed
  by the parse WORKER on its own part (`ingest.qa_aggregate_counts`, the one
  definition; private in-memory DuckDB, 2 threads / 1 GB, spill in a
  pid-named `.qa-spill` folder swept at start). Main-process fallback on any
  worker failure; in-process parses compute it in the main process. Measured
  on 12 real files / 32M rows, 3 workers: main-process peak RSS 2.88 ->
  1.35 GB, wall clock unchanged within noise, QA output identical.
- Cancel an in-flight PARSE: DONE (round 2 follow-up). Files-tab "stop" on a
  processing file / an ingesting link -> `Store.request_parse_stop(filename)`
  (in-memory flag). In-process parses check it on every ~1 MB read
  (`ingest._stop_aware` wraps open_stream's progress callback); a worker
  process can't see the flag, so the parent's wait loop relays it as a
  `<progress>.stop` sentinel the worker checks in `report_chunk` ahead of the
  throttle. `ParseStopped` -> files row `stopped` (plain message; an inbox file
  moves to data/failed so the watcher doesn't restart it), url row `skipped`
  with the download + sidecar KEPT so retry re-parses without re-downloading.
  An earlier version of a re-ingested file is untouched. Live trial, real
  277 MB file: in-process stop 2.7 s, worker stop 0.4 s, retry finished
  5,352,298 rows in 17 s with no new download. Tests: tests/mrfx/test_stop_parse.py
  (+ the link flow in test_fetch.py), all three hooks mutation-checked.
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
  (`test_wall_clock_cap_sets_aside_a_too_slow_download`). The preserve-partial
  refusal of a range-ignored 200 is BOUNDED (`_KEEP_200_MAX_REFUSALS`): a server
  that NEVER supports Range only ever sends 200, so after a few refusals we
  accept it and restart from 0 — otherwise such a file could never complete
  (`test_no_range_server_completes_after_bounded_200_refusals`). Fair
  scheduling FIXED in round 2: `url_queue.retries` counts bulk "retry failed"
  rounds and `_claim_next` orders by (retries, id), so re-queued dead/flaky
  links wait behind fresh files (`test_bulk_retried_rows_wait_behind_fresh_files`).
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
- Full-store scale — MEASURED in round 2 on a synthetic 306M-row store built
  through the real part-adoption API (15 payers x 3 months, 4 cores, 6 GB
  DuckDB budget, 15 GB RAM box; scratchpad `scale/` scripts). The synthetic
  data is PESSIMISTIC for rollups: ~1 raw row per TIN group (real files
  collapse ~23:1), so rates_by_tin is nearly as big as raw.

  | operation | wall-clock | peak RSS |
  |---|---|---|
  | full `rebuild_rollups()` (C1) | 65 min | 6.6 GB |
  | incremental after a new 5M-row file | 168 s | 6.4 GB |
  | re-ingest of a done file, OLD code (full directory) | 1,441 s | — |
  | same re-ingest, round-2 scoped directory | 165 s | 6.4 GB |
  | `forget` + its incremental | 3.5 s + 124 s | 6.5 GB |
  | NPI search `/api/rates?grain=npi&q=<NPI>` OLD | OOM (temp disk) at 30 s | — |
  | same, round 2 (single-column pushdown) | 1.2 s cold / 0.6 s warm | 0.8 GB |
  | `/api/summary` OLD / round 2 | 38 s, 11.2 GB / 21 s, 5.2 GB | |
  | `/api/stats` cold (cached 60 s after) | 35-42 s | 4.7 GB |
  | `/api/entity/npi/<x>` | 4.0 s cold / 1.3 s warm | |
  | `/api/code/97110` (TIN grain) | 5.9 s | |
  | TIN view, payer+code filter | 0.26 s | |

  - **C1 — a FULL rollup rebuild** is still O(slices x total rows): ~103
    hash slices, each re-reading the whole store (the hash predicate can't
    prune parquet). 65 min at 306M rows on 4 cores. Only fires on an explicit
    rebuild, a schema-version bump, or `rollup_needs_full` — round 2 did NOT
    bump ROLLUP_SCHEMA_VERSION. Fix design if it matters: one pass that
    writes the rows hash-partitioned to temp parquet (COPY … PARTITION_BY),
    then aggregate each partition file — O(2 x rows) instead of O(slices x rows).
  - **C2 — removal** is FIXED in round 2: the removed part's TINs are recorded
    (`rollup_pending_tins`) and only those directory rows recompute (each
    directory row depends only on its own TIN's rows). An incremental slice
    still scans the whole store once per directory slice for `tin_value IN
    delta`, because parts aren't sorted by TIN (~8 s/slice at 306M); sorting
    parts by (payer, tin_value) at write time would let stats prune it.
  - NPI and entity views are live aggregations: on stores over
    `api.NARROW_REQUIRED_ROWS` (min(40M, duckdb memory GB x 2.5M) since 0.7.4 —
    NPI_ROWS_PER_GB) they need a full NPI/TIN search (NPI view)
    or payer AND code (both), else a 422 says what to type. Measured: payer-
    only/code-only NPI view 30-51 s; name search in the NPI view, entity
    code-only and entity TIN search ran out of temp disk. The TIN view (the
    default) and the NPI profile page are unaffected.


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
