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
| `mrfx/ingest.py` | `ingest_file` orchestration: quarantines non-rate files, two-pass chunked ingest for files ≥1.5 GB uncompressed (`LARGE_FILE_UNCOMPRESSED_BYTES`), scan short-circuit when pass 1 finds zero target codes, chunk progress (`_ProgressTracker`), QA report, parallel worker (`_parse_worker`, `ParsePoolManager`) — workers are PURE (no DB access). |
| `mrfx/store.py` | DuckDB + Parquet parts (zstd-compressed via `PARQUET_COMPRESSION`; mixed snappy/zstd parts coexist, no migration). One parquet part per source file (re-ingest = atomic replace = idempotent). Materialized rollups: `rates_by_tin_tbl` (the spine), `tin_directory_tbl`; `rates_dedup` is a LIVE VIEW (see invariant 6). `url_queue` table drives URL ingestion. `enrichment_progress` (cached 60s) + `available_states` back the dashboard's name/state UI. SSN masking. All writes behind one in-process lock. |
| `mrfx/fetch.py` | URL-drop pipeline: `download` (retry/backoff, atomic `.part`, HTTP-Range resume, sha256 while streaming, disk-space guard, TLS incl. AIA chain repair), `dedup_key` (volatile signed-params stripped), `expand_toc`, `expand_blobs_listing` (UHC/Optum), `extract_links_from_page`, `crawl_gatsby_hub` (Sapphire hubs), `probe_blobs_api`, `process_url_record` (route one URL), `run_queue` (N downloader threads — `parallel_downloads`, 0=auto=6, concurrent fetches reserve remaining bytes so they never collectively overcommit the disk — + N processor threads + rollup batching). |
| `mrfx/render.py` | OPTIONAL headless-Chromium fallback for JavaScript-only portals: renders the page, harvests links from the DOM + captured JSON responses, and when nothing appears dismisses consent overlays and clicks MRF-looking controls ('View Plan List' — Harvard Pilgrim) with a re-harvest; the browser never networks itself — every request is fetched by httpx (proxy/CA-aware, TLS verified) and fulfilled into the page. Degrades to a help message without Playwright. |
| `mrfx/known_sources.py` + `config/known_sources.yaml` | The catalog of live-verified payer entry points with results; `{FIRST_OF_MONTH}` placeholder resolution. `config/starter_links.txt` is the paste-ready export of it. |
| `mrfx/api.py` | FastAPI JSON API + static SPA. `/api/rates` (tin/npi/entity grains, server-side paging), benchmarks, `/api/schedule/*` + `/api/report/*`, exports (+methodology sidecars), outreach CSV, `/api/urls*`, `/api/known-sources`. |
| `mrfx/benchmark.py` | Market percentiles, opportunity model, payer-negotiation one-pager, pitch report; shared `_market_where` / `resolve_subject_tins` / `_brand_header`. Reports refuse without a pinned month + state (or explicit `allow_national`). |
| **Percentile conventions** (read before touching any position number) | The Benchmark tab's `subject_percentile` is MID-RANK — strictly below plus HALF the ties — computed in `benchmark.py`'s `position` CTE, and both comparisons round the peer rate to 2dp first (subject_rate is already rounded; an exact `=` misses real ties like 33.325 vs a displayed 33.33). Mid-rank is the only convention consistent with the `quantile_cont` p10..p90 columns shown on the same row: a subject whose rate equals the peer median scores exactly 50. It replaced at-or-below (`<=`), which counted the subject's own ties in full and OVERSTATED position by up to ~25 points on real tie-heavy data (payers publish identical fee schedules to many practices) — always upward, i.e. making clients look better paid and shrinking the negotiating gap. `leads.py` deliberately keeps a DIFFERENT convention (`percent_rank()`, strictly-below, market-wide incl. self) because the lead finder must still surface the cheapest practice in a thin market; the divergence is intentional and documented at both sites. |
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
| `tests/mrfx/test_determinism.py` | Two invariants that only show up under repetition. **(a) Tied ORDER BY.** DuckDB scans in parallel, so a ranked list whose sort key ties comes back in whichever order finished first — the payer concentration table, the rate-type mix, the payer posture card and therefore the MARKET REPORT reordered between two generations off an unchanged store, which is unacceptable in a document the user sells. Every ranked query now carries a unique tiebreaker; the test seeds three payers on identical rates and two cities on one median and asserts four identical answers. **(b) The `td` alias rule.** `_market_where` emits `list_contains(td.states, ?)` for a state filter, so any relation it is pasted into MUST join `tin_directory` as exactly `td` — `expiration_coverage` joined nothing and `renewal_radar` joined it as `d`, so a state-filtered renewal radar raised a DuckDB BinderException (a 500, not a refusal). Reachable via the API/CLI; the dashboard sends no state on that call. |
| Tooling gates (`pyproject.toml`, `requirements.txt`) | The repo had no linter, no type checker, no coverage and no CVE scan; correctness was defended entirely by the end-to-end suite and the audit harnesses. Two of those gaps are now closed. **`ruff`** is configured NARROWLY on purpose: the first full run produced 278 findings and exactly ONE real defect (dead assignments in `engagements.py`), so the selection is E9/F/PLE + three targeted bugbear rules and is expected to stay at zero — every excluded rule is listed in pyproject WITH the reason it was rejected (T20: the CLI's prints ARE its interface; B008: FastAPI's `Body(...)` default idiom; B905: a DuckDB cursor's `zip(cols, row)` cannot mismatch; B023: all five flagged closures were checked and never escape their iteration; S110: try/except/pass around cosmetic work IS invariant 3). **`pip-audit`** found 25 known CVEs across 5 pinned packages that had never been refreshed — `cryptography` (TLS, and this app fetches from third-party CDNs), `starlette` + `python-multipart` (the web layer and its upload parser), `pyarrow`, `pytest`. All cleared; `starlette` is now pinned EXPLICITLY rather than left to fastapi's resolution, since the web layer's CVEs land there. The starlette 1.x upgrade also forced migrating off `@app.on_event("startup")` (deprecated, slated for removal) to a `lifespan` handler — verified under uvicorn, not just TestClient, because a silently-skipped startup task is worse than the warning it replaced. NOT closed: no type checker and no coverage measurement. |
| `mrfx/states.py` | The single answer to "what state is this?". Every state-scoped feature used to do `str(v).strip().upper()[:2]` and then check the length — which TRUNCATES instead of validating, so "MISSOURI" passed as "MI" and a user who typed the name out would have been shown **Michigan's** schedules, hospitals and peers under a Missouri heading. `state_code` accepts a USPS code or a full state name (the person using this types "Missouri") and RAISES on anything else — an unrecognised state is refused, never guessed at, because a wrong answer that looks right is the one failure this app must not produce. `state_code_or_none` is the lenient twin for row data, where one bad cell must not stop a load. `normalize_market` runs it, so the dashboard's state box is normalised once for every downstream feature. |
| `mrfx/floors.py` + `mrfx/spark.py` | **Floors/ceilings**: state Medicaid (the floor — "your commercial rate is 4% above Medicaid" ends an argument) and workers' comp (usually the ceiling). No national machine-readable source exists, so the USER loads a code/rate CSV labelled with state + kind + year, and that label IS the provenance printed with every derived number. A two-letter state is REQUIRED to load and to compare — a fee schedule is a state document, so a national commercial median cannot be compared to one state's Medicaid (refuses). A code the schedule omits is ABSENT from the comparison, never zero (a zero would report an infinite ratio and make a rate look heroic); a code listed twice keeps the MAXIMUM allowable so the import is repeatable. Managed-Medicaid-pays-a-percentage and WC-has-its-own-rules caveats travel with every result. **Sparklines** (`spark.py`) draw a rate's path as inline SVG in the printed deliverables — no library, no CDN, because a report is a self-contained file emailed to a client. Fewer than two points draws NO line (a lone dot styled as a trend is an unsupported claim); direction sets the colour only when there IS a direction; `series_by_code` is cosmetic-tier and returns `{}` on any error rather than costing a deliverable. |
| NPPES weekly incrementals (`enrich.apply_weekly_update`) | NPPES publishes a full file monthly and small incrementals weekly; without them names, taxonomies, enumeration and deactivation dates run up to a month stale — worst for the two feeds built on exactly those columns. A weekly REPLACES the cached row for each NPI it contains and leaves every other row untouched: it is authoritative for what it contains and says nothing about what it omits. Reuses `_write_nppes_parquet` (via its `src` parameter) so a weekly row and a monthly row can never be parsed by two slightly different readers, merges in DuckDB, and swaps in via `replace_with_retry` only once the merged file is complete. Refuses rather than half-applying: no cache to merge into, or a file yielding no NPI rows, both leave the existing cache intact. `mrfx enrich --weekly <path>`. |
| `mrfx/leverage.py` | **Walk-away leverage** — the question that actually decides a negotiation: what happens to the PAYER if this practice leaves. Combines three things that sat on three tabs and were never joined: the payer's roster (its other contracted practices), the ZCTA centroids (how far away they are), and ACS seniors (the demand they'd absorb). `network_leverage` places every practice at the ZIP most of its NPIs share and counts the payer's alternatives inside a radius; `leverage_summary` ranks every payer thinnest-first, fault-isolated per payer. THE RAIL THAT MATTERS: a published rate is a CONTRACT, not proof a practice is open or taking patients, so the count is an UPPER bound on the payer's real fallback — the error runs toward understating the client's leverage, and the note says so in that direction. Explicitly not a network-adequacy finding. |
| `mrfx/economics.py` | **Per-visit value + volume-weighted position.** `visit_economics` prices the practice's OWN Medicare code mix per payer, turning "$44 for 97110" into "an Aetna visit is worth $115.54" — the unit an owner's P&L speaks. The mix is RE-NORMALIZED over the codes each payer actually prices, so a payer that simply doesn't publish a code is never made to look cheap by pricing it at zero (`mix_coverage_pct` exposes a thin overlap). Medicare counts SERVICES, not visits, so a per-visit figure needs a units-per-visit divisor that is the USER's assumption, validated (0.5–20) and echoed as one; with none supplied, figures stay per-unit rather than being invented. `weighted_position` weights the headline percentile by the practice's own volumes — and REFUSES below `MIN_WEIGHT_COVERAGE_PCT` (50%) coverage, because weighting a fragment misrepresents the whole. |
| `mrfx/dossier.py` | **Prospect dossier + market report.** Pure recombination: identity, Medicare size, rate position, weakest payer, leverage, referral sources and market demand on ONE branded page for a cold call; and the metro-level story (city rate map, concentration, payer posture, openings/closures, demand) as a document sellable before having a client there. Both are section-fault-isolated and LIST what they could not build with the reason — a branded page with a silently missing section reads as a complete picture when it is not. The dossier REFUSES a subject that is not really in the store (`resolve_subject_tins` falls through to the raw string, so a typo would otherwise render a fully-omitted page a user might send). |
| Growth, service lines, cash anchors | `utilization.practice_growth` compares a practice's Medicare volume across two PUF years, counting ONLY codes present in BOTH — CMS suppression can create or destroy a code line with nothing changing in the practice, so those are listed apart and never booked as movement. `utilization.service_line_gaps` finds codes nearby therapy practices BILL that this one does not (billing behaviour, distinct from contract gaps' published-rate question). `hospital.cash_anchors` surfaces the discounted-cash and gross columns the importer already stored but nothing displayed — a local self-pay anchor for a practice setting its own cash rate, with the reminder that a hospital's facility price should sit ABOVE a private practice's. |
| **Parameter-abuse class + weekly dedupe (second audit round)** | A follow-up audit asked "is everything actually audited" and answered it with a coverage sweep instead of a claim. It found a CLASS the 1,586-call malformed-body fuzzer structurally misses: WELL-FORMED bodies with count-like params at hostile values. Three live 500s (`min_practices<=0` indexed an empty list in `size_premium`; negative `limit` reached DuckDB as a binder error in `local_rate_map` and `steal_share`) plus the same latent bug masked in `hospital_parity`, and a fabrication path (`min_codes<=0` let a zero-rankable payer be verdicted "one rate for everyone"). Class fix: `_bounded_int` at every wire site in api.py AND clamps inside each compute function (CLI callers covered too); `scratchpad/adv_param_abuse.py` now sweeps every POST route from the app's own route table (2,964 calls, 0 5xx) so a new endpoint is attacked without editing the harness. Separately: `apply_weekly_update` trusted the weekly file to be internally unique — a duplicated NPI left TWO cache rows and every join double-counted that provider. The merge now dedupes the weekly side keeping the file's LATER occurrence (`file_row_number`, later row = later update), and the summary counts NPIs, not raw rows. Also `sparkline()` drops non-finite values before building the SVG path (NaN/inf wrote literal "nan" into `d=` — a broken picture in a client report). All regression-tested. |
| **Gap closures against the original nine-item analysis** | Six items were delivered as engines but not at the surface the ask named, and one reversed it. **(2) Payer posture** — `quality.payer_posture` splits `negotiated` from `fee schedule` and badges each payer "negotiates" vs "one schedule for everyone". The first cut had folded both into `CONTRACTED_TYPES`, erasing the exact distinction: those two values are both contracted amounts but say OPPOSITE things about whether a negotiation is winnable. It reports the payer's CLAIM (the field) beside the OBSERVATION (does that payer actually price practices differently), and when they disagree it says so instead of picking one; under `min_codes` shared codes it refuses to judge at all. **(3) Freshness** now rides on the payer roster and, as `benchmark["stale_payers"]`, into the pitch report as a publication-age banner. **(5) Real-terms** reaches the pitch report (`_erosion_block`) and `compare_to_baseline`'s `real_terms` (a win restated in baseline dollars). **(6) Sparklines** reach the rate card and the packet's month page, not just the pitch report. **(7) `radius_rate_map`** answers the ask's actual question — same payer, same code, 30 miles apart — by placing each practice at its own ZIP's centroid (city names cannot: a metro's edge is 40 miles from its centre). Band edges are CLIPPED AND EXTENDED to the requested radius and `unbanded` is asserted to be 0, after a test caught practices inside the radius but past the last fixed edge silently vanishing from the table. **(9) `_add_miles`** puts distance on every steal-share row so a call list is a route, not just a ranking. |
| `mrfx/territory.py` | Three market-structure answers below the state line. **`local_rate_map`** — one code's median by CITY inside ONE state (a state is REQUIRED: pooling cities across states buries the far larger state effect), same `MIN_CELL` suppression as the state map, and suppressed cities are COUNTED so a thin city doesn't read as "nothing there". **`payer_concentration`** — HHI over each payer's share of contracted practice relationships, with DOJ bands. The caveat outranks the number: this is the share of PUBLISHED RELATIONSHIPS IN THIS STORE, not covered lives, so it is only as complete as the files ingested — under 5 payers it returns `thin: true` and NO band, because with three payers the minimum possible HHI is 3333 and the index would call every market concentrated by construction. **`steal_share`** — physicians who send therapy patients to other practices in a client's own cities and little or nothing to the client, ranked by the gap; `already_a_partner` separates "sends you some" from "sends you none". Shared-patient counts are a proxy for referral, not a record of one, and every surface says so. |
| `mrfx/quality.py` | Three questions a rate doesn't answer about itself. **`rate_type_mix`** — how much of a market is a contracted amount (`negotiated` / `fee schedule`) vs one the payer DERIVED where it has no contracted figure. Reported, never silently filtered: excluding derived rates by default would move every number the user has already seen, so composition is the finding and acting on it is their call; `code_type_flag` does the same for a client's OWN rates, where "the payer derived your rate" changes what the ask is. **`payer_freshness`** — per payer `last_updated_on` age, with three distinct states (fresh / stale / **unreadable-or-unstated**): a date we cannot parse must not pass as fresh and must not be called stale either. **`size_premium`** — answers the standard objection "the practices above me are bigger" by ranking each practice with `percent_rank()` WITHIN (payer, code) — so a payer's overall generosity can't masquerade as a size effect — then comparing coarse provider-count bands. A band under `min_practices` is marked `thin` with null figures rather than reported, and a subject with no rankable cell gets a "can't say" reason, distinguished from a typo (which refuses) by an explicit presence check. |
| `mrfx/hospital.py` | CMS hospital price-transparency import + parity. Reads both v2.x shapes — the tall CSV (2-line identity preamble above the real header, columns token-matched because vendor templates drift) and the JSON (streamed with ijson; these reach several GB). Keeps ONLY therapy codes, outpatient/both settings, and DOLLAR amounts: a percent-of-charges term or an algorithm description is not a price, $0 is a placeholder, and an `estimated_amount` is stored separately and labelled an estimate. **Hospital rates live in `hospital/*.parquet` behind the `hospital_rates` view — NEVER in `rates`**: a facility (HOPD) payment is a different product under a different mandate, and letting one into the spine would move every practice median in the app (regression-tested). `hospital_parity` matches on payer name AND billing code only — no fuzzy payer matching, because inventing a match invents the comparison — and reports a RATIO, never a subtraction presented as money owed. Every surface carries the facility-vs-professional caveat. A file we cannot read is refused with what was wrong; a valid file with no therapy line reports that and writes no part. |
| `mrfx/inflation.py` | Real-terms erosion — the one place the app says that a rate held flat is a pay CUT. CPI-U annual averages (BLS CUUR0000SA0, 1982-84=100) ship in `DEFAULT_VALUES`; the user extends them per published year with `mrfx inflation --year --value` or `POST /api/inflation`, stored in `inflation_index` (user years win). **A year with no index value REFUSES with a reason and a fix — never an extrapolation**; an explicit `assume_inflation_pct` is allowed and is echoed as an ASSUMPTION everywhere it is used, and it is validated even when the index makes it unnecessary. Real change is the compounding ratio `(1+nominal)/(1+inflation)-1`, not `nominal - inflation` — subtraction drifts over multi-year spans and drifts in the direction that makes a cut look smaller. Every output carries the CPI-is-a-consumer-basket caveat. `compute_payer_trajectory` gains `real_cumulative_pct` / `erosion_line` per payer. |
| Closure feed (`nppes.closures`) | The mirror of the new-clinic feed, off the same bulk cache: therapy NPIs DEACTIVATED recently and **not since reactivated** (NPPES deactivates for paperwork lapses as often as closures, so a reactivated NPI never closed). `NPI Deactivation Date` / `NPI Reactivation Date` joined `_NPPES_CACHE_COLS` and `_NPPES_CACHE_SCHEMA` went to 4, so an existing cache rebuilds once. Two consulting uses: a closed REFERRAL SOURCE is a hole in a client's inbound volume, and a closed COMPETITOR is room — `in_store` marks the ones that carried published rates. Packet section 6 intersects a client's own inbound referral partners with the feed. Every surface says a deactivated NPI is a call to make, never a fact to print. |
| `mrfx/tracker.py` | READ-ONLY discovery of a Medicare Order & Referring Tracker install (`ORF_DATA_DIR` → `%LOCALAPPDATA%\OrderReferringTracker` → `~/.order-referring-tracker`, or `tracker_dir` in config): its `snapshots/OrderReferring_<date>.csv`, its `referral-map/` datasets + `dataset-meta.json`, and which of those this store has already imported. Never writes to the tracker's folders — that data belongs to the other program, which has its own retention. The dashboard imports what discovery offered **in-process** (`POST /api/medicare/tracker/import`, daemon thread + polled job state): the CLI must refuse while `mrfx serve` holds the DB, but the server importing its own store is the one process that may — which is why the merge needs no stopping. Only paths discovery just returned are importable, so the endpoint can't be pointed at an arbitrary file. |
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
  (`test_wall_clock_cap_sets_aside_a_too_slow_download`). The preserve-partial
  refusal of a range-ignored 200 is BOUNDED (`_KEEP_200_MAX_REFUSALS`): a server
  that NEVER supports Range only ever sends 200, so after a few refusals we
  accept it and restart from 0 — otherwise such a file could never complete
  (`test_no_range_server_completes_after_bounded_200_refusals`). Still open: no
  fair scheduling —
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
