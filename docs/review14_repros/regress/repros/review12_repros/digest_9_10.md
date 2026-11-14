# Audit 12 — digest of rounds 9 and 10 against the shipped 0.7.5 (68d9bc7)

Verified on the extracted zip at `audit12/unzipped/Claudevideo` (identical to the repo head apart from
`egg-info`), its own venv, scratch stores only under `audit12/agent_9_10/` (all deleted afterwards; the
scripts stay: `build_store.py`, `latency.py`, `entity_ab.py`, `startup_2000.py`, `files_page.py`, `ae_check.py`).
Timings were taken while the full suite was running on the same box, so they are upper bounds, not quiet numbers.

Regression files of both rounds, run from the extracted copy (`-p no:cacheprovider`):
- `tests/mrfx/test_review9.py`: **53 passed in 270 s** (`agent_9_10/review9.log`)
- `tests/mrfx/test_review10.py`: **30 passed in 95 s** (`agent_9_10/review10.log`)

Verdict key: VERIFIED-HOLDS / REGRESSED / STILL-OPEN / DOC-STALE / NOT-CHECKABLE.

## Round 9 (`docs/NEXT_STEPS_PROMPT_9.md`, 0.7.3) — claimed closed: every item of Groups 1–5

| Item | Claim | Verdict | Evidence |
|---|---|---|---|
| 1.1a | Row-rules update maps parts by BASENAME, never stamps an unmatched part | VERIFIED-HOLDS | `mrfx/store.py:1593-1683` (`by_base`, case-insensitive fallback, unmatched part → count check → `failed`, "never stamp it on faith"); `test_update_never_stamps_a_part_it_could_not_match` passes |
| 1.1b | One unreadable part does not block the batch; the log never says "up to date" while files are left | VERIFIED-HOLDS | `store.py:1621-1633` per-part retry; `:1564-1577` two different log sentences; `test_one_bad_part_does_not_block_the_others_and_the_log_says_so` passes |
| 1.1c | `_typefix` keyed on (type, is_dollar) — no doubled rows | VERIFIED-HOLDS | `store.py:1810-1811` `ON r.negotiated_type = t.o AND r.is_dollar_rate IS NOT DISTINCT FROM t.od`; `test_type_rewrite_never_doubles_rows` passes |
| 1.1 | `ROW_RULES_VERSION` = 4, `union_by_name` dropped | VERIFIED-HOLDS | `store.py:518`; `:1612-1614` plain `read_parquet(?, filename = true)` |
| 1.2 | Same file = full sha; 2.3 MiB file never loaded twice; watcher does not loop; `unique_inbox_name` strips only trailing suffixes; double upload is a duplicate; mid-read change not recorded | VERIFIED-HOLDS | `ingest.py:1793-1812`; 7 tests (`test_the_same_2_3_mib_file_is_never_loaded_twice[*]`, `test_the_watcher_does_not_loop_on_a_kept_file`, `test_unique_name_keeps_a_middle_suffix`, `test_the_same_upload_twice_while_the_first_is_loading_is_a_duplicate`, `test_a_file_changed_while_it_is_read_is_not_recorded_as_loaded`, `test_another_month_under_the_same_name_loads_once_under_a_content_name`) pass on 0.7.5 even after round 11's identity rewrite (`identity.py`) |
| 1.3 | A rate file takes only its own vintage's provider list | VERIFIED-HOLDS | `store.py:5007-5030` (`load_provider_refs(payer, last_updated_on, …)`), `ingest.py:1489`; `test_a_rate_file_never_takes_another_months_provider_list` passes |
| 1.4 | Reference ids exact; > int64 refused | VERIFIED-HOLDS | `mrfx/refid.py:30`; live: `1234567890123456789`/`…790` stay distinct, `"12.0"`→12, `12.5`→None, `2**63`→None, both `clean_ref_id` and `fastscan._ref_int`; `test_ids_above_2_53_stay_distinct[*]` pass |
| 1.5 | Multi-member gzip not skipped early; zip proof = central directory in tail; segmented download runs the early check | VERIFIED-HOLDS | `fetch.py:2090-2112` (`trailer_proves_for`, `_zip_cd_in_tail`), `fetch.py:841-846` (`twin_possible` before segments); `test_trailer_proof_is_recorded_per_container`, `test_a_segmented_download_runs_the_early_check_when_a_twin_may_exist` pass |
| 2.1 | Update scans in batches of 40 with a progress tick; "opening the store at …" and "the dashboard is ready" lines | VERIFIED-HOLDS | `store.py:522` `_TYPE_SCAN_BATCH = 40`; `cli.py:224`, `cli.py:312`; 2,000-part store, rules 2 → 4: `_migrate_types_only 0.93 s, 16,012 read calls` (claim 1.43 s / 16,220) (`agent_9_10/startup_a.log`) |
| 2.1 (3rd bullet) | "Decide, and write down in AI_HANDOFF, the rule for ALL future stored-row updates: dashboard first or a hard budget" | STILL-OPEN | No such rule in AI_HANDOFF "Round 9 additions" (lines 440-479); `_migrate_row_rules` has no time budget or background continuation (grep `budget|Thread(` in `store.py:1486-1578` → none). It prints progress, but a big store on a slow drive still opens the dashboard only after the scan. P2 |
| 2.2 | Start-up integrity check reads only new/changed parts (4 reads on the 2nd start) | VERIFIED-HOLDS | `store.py:1180` `.verified_parts.json`; 2nd and 3rd starts of the 2,000-part store: `_quarantine_corrupt_parts 0.02 s, read_calls=4`, total open 0.1-0.2 s / 67 reads (`startup_b.log`, `startup_c.log`); `test_the_start_up_check_reads_only_new_or_changed_parts` passes |
| 2.3 | Freeze recorder starts before `Store()` | VERIFIED-HOLDS | `cli.py:223` before `:225`; `test_freeze_diagnostics_start_before_the_store_opens` passes |
| 2.4 | Files tab: rows needing the user always first, outside the page limit | VERIFIED-HOLDS | `api.py:2371-2400`; live 600 done + pending_confirmation + queued + failed(2020) + needs_choice: all four on page 1 at indexes 0-3, `attention 34`, `offset=1e20 → 422`, `limit` capped at 5000 (`files_page.py`) |
| 2.5 | First paint says it is loading instead of sitting blank | VERIFIED-HOLDS | `web/app.js:335` "Loading market overview — the first screen after a start reads the whole store and can take up to a minute…" |
| 3.1 | Big standalone provider list stored on the two-pass path | VERIFIED-HOLDS | `test_a_big_reference_file_is_stored_on_the_two_pass_path[fast_scan_off|low_disk]` pass |
| 3.2 | Linked rate file re-read / re-downloaded when its list lands; note "waiting for this payer's provider reference file" | VERIFIED-HOLDS | `ingest.py:1532-1583`, `fetch.py:3149`; `test_a_linked_rate_file_is_downloaded_again_when_its_list_lands` passes. Round 11 changed the inbox wording to "drop this file in again to pick up …" (`ingest.py:1583`) — the link note is unchanged |
| 3.3 | Deferred groups spill to disk (memory flat) | VERIFIED-HOLDS (code+test); RSS NOT re-measured | `parser.py:402-422` `_DeferredSpool`, `:662`, `:953`; `test_deferred_groups_spill_to_disk_and_are_all_emitted` passes. The 100/200/400 MB RSS table was not re-run (would need ~1 GB of fixtures) |
| 3.4 | Empty companion entry = missing; `location` = remote; both counted | VERIFIED-HOLDS | `parser.py:857-861`, `:993`, `:1483-1486`; `test_empty_or_remote_companion_entries_are_counted` passes |
| 3.5 | Split own table is all the file's own; no in-flight fingerprint rows left | VERIFIED-HOLDS | `test_a_table_split_around_in_network_is_all_the_files_own`, `test_a_stopped_download_keeps_no_inflight_fingerprint` pass |
| 4.1 / 4.2 | Pinned month bounds the trajectory; chain-linked cut streak | VERIFIED-HOLDS | `test_a_pinned_report_does_not_show_a_cut_from_after_its_month`, `test_a_cut_streak_is_measured_on_the_same_pairs_month_to_month` pass |
| 4.3 | One `overall_position` on every screen; worst payer on shared codes | VERIFIED-HOLDS | `benchmark.py:827`, 6 call sites; `test_one_practice_has_one_position_on_every_screen[*]`, `test_the_worst_payer_is_judged_on_the_codes_its_payers_share` pass (round 10's size-panel exception was closed in 0.7.4, `test_size_panel_prints_the_one_overall_position` passes) |
| 4.4 | Subject rule everywhere; `resolve_subject_tins` sorted; mapped entity titled with its name | VERIFIED-HOLDS | `benchmark.py:686` `return sorted(...)`; `test_an_out_of_state_subjects_priced_codes_are_not_gaps`, `test_a_partial_name_takes_the_market_state_like_the_full_name`, `test_the_dossier_names_the_mapped_entity_and_the_practice_it_prices` pass |
| 4.5 | Different file under a loaded name is HELD with Replace / Keep both, never blended | VERIFIED-HOLDS | `ingest.py:1815` `hold_for_choice`; `test_a_corrected_file_of_the_same_size_is_held_then_replaces`, `test_keep_both_loads_the_held_file_beside` pass (round 11 fixed the "Keep both after a restart" stuck case on top) |
| 4.6 | Trajectory generation-cached; outreach medians in SQL; materialised medians keyed by spine generation, reused across restarts, superseded ones deleted | VERIFIED-HOLDS | `store.py:4771` `spine_materialised`, `:4821` `spine_key`, `:5775` `generation_cached`; live trajectory MO 143 ms → 6 ms second call; `test_outreach_medians_are_computed_in_the_database`, `test_practice_medians_survive_a_restart_and_superseded_ones_go` pass |
| 4.6 numbers | Trajectory national 25 s first / 0 after; outreach zip 50 s on the 99M store | NOT-CHECKABLE | No 99M store here (7 GB disk); only the cache behaviour was verified (above) |
| 4.7 | Wrong-typed input → 422 sentence, no SQL/path; `_` keys dropped; `offset` bounded; `tins`/`subjects` typed; long file names refused | VERIFIED-HOLDS | live: `billing_class: [..]` → 422 "must be text, not a list"; `market: 5` → 422; `tins: 5` → 422; `_plan_files` not echoed; 305-char upload → 422 "that file name is 305 characters long — rename it to under 150"; `/api/files?offset=1e20` → 422; no "LINE " or path in any body (`latency.py` output). `api.py:769` `MAX_NAME_CHARS = 150`, `:1443` duckdb.Error handler. `test_no_wrong_typed_market_reaches_the_database`, `test_a_private_market_key_is_never_taken_from_the_wire`, `test_long_names_and_bad_paths_are_refused_in_words` pass. Note `{"city": 5}` is coerced to `"5"` (200), which the item allowed ("coerces or rejects") |
| 4.8 | Outreach sidecar says "one row per organization", never "ALL months pooled" with latest columns | VERIFIED-HOLDS | `test_the_outreach_file_describes_itself` passes |
| 4.9 | half-up sparkline %, no "-0.0%", market in words, no %-with-$ pooling, catalogs found from another cwd | VERIFIED-HOLDS | `rounding.py:23-31` `signed_pct` (`if v == 0: v = 0.0`); `config.py:316` anchors `known_sources_path`; the 4 tests (`test_printed_percentages_…`, `test_report_footers_…`, `test_percentages_are_never_pooled_with_dollars`, `test_shipped_catalogs_are_found_…`) pass |
| 5.1 | Lanes: 1 GET per file in every run; slot 0 takes big files; no lanes ≤ 2 slots | DOC-STALE (behaviour changed on purpose in 0.7.5) | Round 11 item 2.8 added a 1-byte size probe from small-file slots: `test_a_queue_of_big_files_costs_one_request_per_file` now asserts `1 <= n <= 2` requests per file (`tests/mrfx/test_review9.py:913-916`). The round-9 status block ("1 GET per file in every run"), `docs/AI_HANDOFF.md:477` ("one GET per file") and `GETTING_STARTED.md:1211` ("Each file is requested once") still state the old rule; `AI_HANDOFF.md:671, 695` describe the probe. P3 |
| 5.2 | Suspicions settled: `waiting_on` cleared by user actions; copy skipped against replaced content is re-queued; rules update leaves a parked replacement alone; classic reader says what it cannot read | VERIFIED-HOLDS | `store.py:3429, 3530, 3548, 3572, 3607` clear `waiting_on`; the 4 tests pass |
| Status: "Not run on Windows" | held-file folders under a lock, superseded-part delete, spinning-drive reads | STILL-OPEN (by construction) | Nothing in round 11 ran on Windows either (its status block says so). Still matters: it is the user's only platform |

## Round 10 (`docs/NEXT_STEPS_PROMPT_10.md`, 0.7.4) — claimed closed: Groups 1–4 except 1.5 (partly) and 1.9's page cache

Scratch store for the latency numbers: 768,000 rates (12,000 TINs × 4 payers × 8 codes × 2 months, NPPES names/states seeded,
spine 768,000 rows) — about 1/130 of the 99M store and 1/40 of the 32M store the claims were measured on; box under load.

| Item | Claim | Verdict | Evidence |
|---|---|---|---|
| 1.1 | A newer request under the same `X-Mrfx-View` interrupts the older one → 409 "superseded"; the newer returns normally | VERIFIED-HOLDS | `api.py:1552-1593` (`_view_query`, `con.interrupt()`); live two overlapping `/api/rates` with the same header: first **409 in 53 ms**, second 200 in 156 ms; same for `/api/summary` (409 / 200, no 503) (`latency.py`). `web/app.js:120-121` AbortController + header |
| 1.2 | Typed box: one 300 ms debounce, then `refresh()` directly (request leaves < 350 ms after the last key) | VERIFIED-HOLDS (code) | `web/app.js:1082-1094` (`debounce(…, ev === "input" ? 300 : 0)` → `refresh()`); clicks 0 ms. Not re-measured in Chromium |
| 1.3 | Practice list materialised per spine generation; TIN page cut on the spine then names attached | VERIFIED-HOLDS | `spine_units_v1.<spine_key>.parquet` (59 KB) written beside the store; name search `q=PRACTICE 12` 126 ms, full name 117 ms, state 182 ms vs unfiltered 84-95 ms (claims on 99M: 0.99 s vs 1.44 s) |
| 1.4 | `/api/boot` one field; unfiltered strip stored under `spine_key` across restarts | VERIFIED-HOLDS | `api.py:4348`; live `/api/boot` **30 ms** (claim 13 ms); `summary_strips.v2.<spine_key>.json` beside the store; `test_boot_answers_the_first_screen_in_one_request` passes |
| 1.4 | "the very first `/api/stats` of one session took 38 s / 60 s — instrument it" | STILL-OPEN | No timing/log instrumentation in the `/api/stats` handler (grep `monotonic|took|log.` in `api.py` `def stats` → none). Low priority unless it recurs; it would show as "the first screen hangs" to the user |
| 1.5 | One-/two-letter name searches: 2-character minimum for text (digits exempt) | VERIFIED-HOLDS (partial, as documented) | `web/app.js:1102` `t.length < 2 && !/^\d+$/.test(t) ? "" : t`. The "serve the cached unfiltered answer when the text matches every practice" half was left open and still is: `q=PR` rates 206 ms / summary 240 ms vs unfiltered 84 ms / 4 ms (cached) — 2.5× / 60× on this store |
| 1.6 | No "Stats are busy" refusals: the newer summary interrupts the holder and waits (8 s bound) | VERIFIED-HOLDS | `api.py:1880-1889` (`acquire(timeout=8.0)`); live: 0 refusals in the overlap test |
| 1.7 | Entity grain two-pass: same rows, faster; an NPI at the entity/TIN grain says "bills under TIN …" | VERIFIED-HOLDS | A/B on the scratch store (`entity_ab.py`): two-pass 782-936 ms vs one-pass 1,223-1,259 ms default page, identical first rows and totals; `state=AL` 250 vs 301 ms (claim 32M: 3.68 → 1.29 s). Hint live: `NPI 1300001234 bills under tax ID 300001234 — search that, or switch to the NPI view.`; `test_entity_two_pass_page_is_the_full_aggregate`, `test_a_ten_digit_npi_suggests_…` pass |
| 1.8 | NPI guard derived from the memory budget; a code alone is narrow enough; pager reset on error | VERIFIED-HOLDS | `api.py:472, 478, 1684-1724` (`min(NARROW_REQUIRED_ROWS, gb * NPI_ROWS_PER_GB)`, `code_alone_ok`); `web/app.js:846-851` pager reset in the catch. Under the guard on this store (npi default 2.7 s, cpt-only 0.5 s) |
| 1.9 | Table dimmed not blanked; NPI autocomplete; subject-types cached; `%`/`_` literal | VERIFIED-HOLDS | `web/app.js:839-841` (`stale` class); live `q=__` → 0 rows, `modifier=%` → 0 rows; subject-types 15 ms → 4 ms on the second call; `test_percent_and_underscore_are_literal_in_searches` passes |
| 1.9 left | Unfiltered page's base rows not remembered; name sort unchanged | STILL-OPEN (as documented) | rates default 2nd call 84 ms (no cache); `sort=display_name` 194 ms vs 84 ms. Round 11 did not touch it. Matters little below ~30M rows; on the 99M store it was 1.4 s / 4.6 s per return |
| 2.1 | Market report / territory cities are the state's own | VERIFIED-HOLDS | `test_market_report_cities_are_the_states_own` passes |
| 2.2 | Explorer month box offers "latest — each contract's newest file" as the default | VERIFIED-HOLDS | `web/app.js:42` `month: "latest"`, `:1066` option list |
| 2.3 | Markets with a State: every state ranked on its own practices | VERIFIED-HOLDS | `market.py:67-83`; `test_markets_with_a_state_ranks_every_state_on_its_own_practices` passes |
| 2.4 | Rate changes with a picked month: per-payer pairs; "needs two published months" | VERIFIED-HOLDS | `test_rate_changes_for_a_picked_month_compare_each_payer_with_its_own_previous_file` passes |
| 2.5-2.10 | Drawer chart basis, rate-card best payer on shared codes, packets get a state, size panel = overall position, subject rule on 7 more surfaces, landscape print CSS | VERIFIED-HOLDS | `test_size_panel_prints_the_one_overall_position`, `test_monthly_packet_contains_market_position`, `test_printed_reports_are_landscape`, `test_wording_posture_and_floors` and the rest of the 30 pass |
| 3.1-3.13 | Code page trend basis, half-up on the server, claim-line columns in Rate changes, state codes only, freshness ignores 0-row files, new-to-network, Leads market median = Benchmark's, pinned documents stop at their month, sidecar in words | VERIFIED-HOLDS | `rounding.half_up`; `states.state_code_or_none("MASSACHUSETTS") → MA`, `("RIYADH") → None`; `store.py:5409-5410` filters the state list to `STATE_CODES`; the 11 corresponding tests pass |
| 4 | Bad-input 500s (`min_peers` 1e308, `radius_miles` NaN, non-object market); CPI save refreshes caches; Leads CSV column order; `fee.csv` cents | VERIFIED-HOLDS | live: `min_peers: 1e308` → 200, `market: 5` → 422 sentence; `test_bad_input_is_a_sentence_not_a_500[*]` (4), `test_saving_an_index_year_refreshes_cached_answers`, `test_leads_csv_keeps_its_column_order_and_fee_csv_prints_cents` pass |
| Left: AE | Markets' state ranking still lists military mail codes (AE…) | STILL-OPEN | `market.py:86-100` unnests `tin_directory.states` with no `STATE_CODES` filter; `store.py:5772` `_state_cell` keeps an unknown code "as NPPES wrote it"; `states.state_code_or_none("AE") → None` but `"AE" not in STATE_CODES` only filters the *state list* (`store.py:5409`), not this ranking. Live: an NPI saved with state AE lands in `tin_directory.states = ['AE']` (`ae_check.py`); it was hidden from the ranking only by `min_practices`, so ≥ `_MIN_CELL` such practices (the real store had 7) would list "AE". Round 11 status block does not mention it. P3, one-line fix |
| Left: 1.5 cached-unfiltered | serve the cached unfiltered answer when a text matches everything | STILL-OPEN | see 1.5 above; not in round 11 |
| Left: Windows | launcher/updater untouched | STILL-OPEN (by construction) | round 11 also Linux-only |

## Documentation that now describes the old behaviour

- `docs/NEXT_STEPS_PROMPT_9.md` status block ("5.1 … 1 GET per file in every run") and item 5.1 ("GETs per file must be 1"),
  `docs/AI_HANDOFF.md:477` ("the downloader never hands a started download back (one GET per file)"),
  `GETTING_STARTED.md:1211` ("Each file is requested once"): since 0.7.5 a small-file slot sends a 1-byte size probe first
  (`AI_HANDOFF.md:671, 695`; `tests/mrfx/test_review9.py:913-916` allows 1-2 requests per file). DOC-STALE, P3 — the 0.7.3
  "what changed" section is historical, but AI_HANDOFF's "rules a change must keep" now contradicts itself.
- `docs/NEXT_STEPS_PROMPT_9.md` item 2.1's third bullet asks for a written rule ("dashboard first or a hard budget") and the status
  block says every item closed; no such rule exists in AI_HANDOFF and the code has no budget (see table). The status claim is
  overstated for that bullet.
- `docs/AI_HANDOFF.md:1257` still says NPI view needs a full search above `api.NARROW_REQUIRED_ROWS` (40M) — true but incomplete:
  since 0.7.4 the NPI grain's limit is `min(40M, memory_gb × 2.5M)` (`api.py:478, 1702`), i.e. 15M at the default 6 GB.
  `AI_HANDOFF.md:560` has the newer rule. P3.

## What is wrong in 0.7.5 (severity-sorted)

No P0 or P1 regression of a round-9 or round-10 fix was found: all 83 regression tests of the two rounds pass on the shipped
copy, and every behaviour re-run by hand (start-up read counts, Files tab ordering, 409 interruption, bad-input sentences,
ref-id exactness, entity two-pass identity, boot) matches the claim.

- **P2 — the one-time stored-row update still blocks the dashboard with no budget** (round 9 2.1, third bullet). Progress is
  printed (batches of 40, `store.py:522`) but on a slow external drive a large store opens only after the whole scan; the rule the
  prompt asked to be written in AI_HANDOFF was never written. Only bites on a future `ROW_RULES_VERSION` bump.
- **P3 — Markets' state ranking can list military mail codes (AE/AA/AP)** (round 10 left-open, still open): `market.py:86-100`
  lacks the `STATE_CODES` filter that the state list has (`store.py:5409`).
- **P3 — stale "one request per file" rule** in `AI_HANDOFF.md:477` and `GETTING_STARTED.md:1211` after round 11's 1-byte probe.
- **P3 — `/api/stats` first-call stall (38 s / 60 s, seen twice, never reproduced) is still uninstrumented** (round 10 1.4).

## Still open and worth doing (for one non-technical Windows user)

1. The AE/AA/AP filter in `market.geographic_rates` (one line; a wrong-looking row in a ranking the user may paste into a report).
2. A time budget / background continuation for `_migrate_row_rules`, written down as the rule for future row-rule bumps — the
   user's install is on an external drive and "sits silent" was what started round 9; progress lines alone do not open the dashboard.
3. Round 10 1.5/1.9 residue (serve the cached unfiltered page when a 2-letter text matches every practice; remember the unfiltered
   page; order the name sort on the practice list) — only matters on stores past ~30M rows, which this user is heading toward.
4. Fix the two stale doc lines (AI_HANDOFF:477, GETTING_STARTED:1211) so the next round does not "restore" one-GET behaviour.
5. Log the duration of `/api/stats` / `/api/boot` when over ~5 s so a recurrence of the 38-60 s first call leaves a trace in `serve.log`.
6. Nothing from rounds 9-10 has been run on Windows; the held-file folders (`inbox/.held`, `.replacing`) under an antivirus lock
   remain the most likely place for a surprise.
