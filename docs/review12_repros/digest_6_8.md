# Audit 12 — prompts 6, 7, 8 checked against the shipped 0.7.5 (68d9bc7)

Checked on the extracted zip at `scratchpad/audit12/unzipped/Claudevideo` (its `mrfx/`
is byte-identical to the repo head: `diff -rq` differs only in `__pycache__`).
Evidence runs (all from the extracted copy, `-p no:cacheprovider`, under a parallel
full-suite run, so timings are not numbers):

| batch | files | result |
|---|---|---|
| A | test_round7, test_round6_consistency, test_round6_inputs, test_known_answer_months, test_known_answer_leads, test_determinism | **102 passed** (339 s) |
| B | test_audit8_ingest, test_audit8_outputs, test_audit8_ui, test_early_dedup, test_updater, test_install_paths, test_known_sources | **73 passed** (231 s) |
| C | test_known_answer_modifiers, test_file_month, test_latest_flag, test_plans, test_outreach, test_floors, test_medicare, test_erosion, test_hospital, test_mpfs, test_quality | **112 passed** (108 s) |
| pip-audit -r requirements.txt | | `No known vulnerabilities found` |
| scratch scripts (`agent_6_8/checks1-3.py`, `roundscan.py`, `mpfs_probe.py`) | clean_rate, half_up vs DuckDB round, Python-vs-SQL masking parity, deflator, modifiers, AST scan for `round()`, live MPFS NaN probe | quoted inline below |

Verdicts: VERIFIED-HOLDS / REGRESSED / STILL-OPEN / DOC-STALE / NOT-CHECKABLE.
Line numbers are the 0.7.5 files. Test names are `tests/mrfx/<file>::<test>`.

---

## Prompt 6 (round 6 — every output, end to end)

### Claimed fixed

| item | claim | verdict | evidence |
|---|---|---|---|
| P0.1 months per payer | packets / new-to-network / digest compute on each payer's own newest month, label only | VERIFIED-HOLDS | `packets.py:91-100` (`month="latest"`, vintage label from `subject_vintage` 65-80); `contracts.py:175-186` `payer_months(store, scope_payers)` per payer, `compared_months` 326-329; `clients.py:155-166` `compute_rate_changes(store, {"month":"latest"})` with per-payer `span`; `monitor.py:49 payer_months`; test_known_answer_months passed (A) |
| P0.2 new-to-network on house basis | both books through `_market_where` | VERIFIED-HOLDS | `contracts.py:224-232` (`_market_where({**market,"month":"latest",...})`), newcomer anti-join 243-250 |
| P0.3 remits `td` binder 500 | 422/answered with a state | VERIFIED-HOLDS | `remits.py:163-170` strips state/city/therapy for the own-rate query; test_round6_inputs::test_underpayment_check_with_a_state_is_answered |
| P0.3 `/api/changes` state name | normalised | VERIFIED-HOLDS | `monitor.py:410 normalize_market(...)`; ::test_a_state_name_finds_the_same_rate_changes_as_its_code |
| P0.3 hospital parity state | normalised after `normalize_market` | VERIFIED-HOLDS | `hospital.py:573-583` `state_code(state)`; ::test_hospital_parity_with_a_state_name |
| P0.3 proposal comparables / percentile / plan scope | list-checked, 422 not 500, `_plan_files` kept | VERIFIED-HOLDS | `api.py:1072 _comparables_arg`, used 3926-3964; `benchmark.py:1249` comment + `_plan_files` path 340/441/782; ::test_proposal_comparables_must_be_a_list, ::test_proposal_bad_percentile_is_a_refusal_not_a_500, ::test_proposal_pct_of_medicare_with_a_state_name, ::test_negotiate_compare_and_proposal_under_a_plan |
| P0.4 validate ORDER BY | unique key | VERIFIED-HOLDS | `api.py:2317` `ORDER BY payer, file_month, negotiated_rate, tin_value, npi, modifiers, …` |
| P0.4 outreach order / column cut | ORDER BY + deterministic cut | VERIFIED-HOLDS | `outreach.py:143 ORDER BY display_name, unit_id`, `:185 ORDER BY n DESC, billing_code`, `:72-76 mode(... ORDER BY ...)` |
| P0.4 floors newest year | newest year per (kind, state), labelled | VERIFIED-HOLDS | `floors.py:293-304` (`newest AS (SELECT kind, max(year) …)`), `schedule_years` 315-319, 377 |
| P0.4 Medicare referral-leader tie | no hash-order tie | VERIFIED-HOLDS | `medicare.py` ~803-834: `mode(struct_pack(...) ORDER BY struct_pack(...))`, `ORDER BY patients DESC NULLS LAST, practice, pkey`, `mode(tin_value ORDER BY tin_value)` |
| P0.4 quality / utilization / hospital orders | unique tiebreaks | VERIFIED-HOLDS | `quality.py:455 ORDER BY 1, 2, 3`; `utilization.py:590 … peer_units DESC, u.billing_code`; `hospital.py:653-654 ratio DESC NULLS LAST, p.billing_code, p.payer` |
| P0.4 drawer tins / `_ORDER_TIEBREAK` | ordered; includes `tin_is_really_npi` | VERIFIED-HOLDS | `api.py:2014 ORDER BY tin_value`; `api.py:989-991` tuple ends `… "tin_is_really_npi", "negotiated_rate"` |
| P0.4 `any_value` identity | one name rule | VERIFIED-HOLDS | `clients.py:210-222 subject_display_name` = `first(display_name ORDER BY npi_count DESC NULLS LAST, tin_value)`; same SQL `dossier.py:173`, `orgprofile.py:65`; `packets.py:123` uses it |
| P0.4 snapshot 125/125 | byte-identical double run | NOT-CHECKABLE here (no named store); test_determinism.py passed (A) | |
| P1.1 packets digest once | digest handed to every packet | VERIFIED-HOLDS | `packets.py:83-88` (`digest`, `gaps` params) |
| P1.2 Clients digest cached | per (generation, watchlist) | VERIFIED-HOLDS (key widened to `user_edit_generation` + Medicare status) | `clients.py:97-110` |
| P1.3 visit card subject-only | no peer medians | VERIFIED-HOLDS | `visit.py:235 compute_fee_schedule(..., peers=False)`; `schedule.py:59-60` |
| P1.4 outreach percentile via bisect | | VERIFIED-HOLDS | `outreach.py:18, 211, 256` |
| P1.5 quality scans cached per generation | | VERIFIED-HOLDS | `quality.py:86,150,219,433,501 @generation_cached` |
| P1.6 ZIP placement from a stamped parquet | rebuilt on part/directory change | VERIFIED-HOLDS (code present; stamp inputs not re-derived) | `store.py:5268-5282` `tin_zip_place.*.parquet`, previous stamp kept |
| P1.7 leads / leaderboard / roster cached | | VERIFIED-HOLDS | `leads.py:138, 338, 430` |
| P1.8 Code-page gate restored | `_tin_rows_by_unit` gated | VERIFIED-HOLDS (gate exists; the 0.54 s number NOT-CHECKABLE) | `api.py:176, 406, 563` |
| P1 timings (packets 26 s, digest 12 s, …) | | NOT-CHECKABLE (no 99M / named store in this environment) | |
| P2 strip "retrying…" dead compare | `.toString()` compare, retry live | VERIFIED-HOLDS | `app.js:932 const asked = filterQuery().toString()`, `:978 if (filterQuery().toString() === asked) loadSummary()` |
| P2 slow answers painting over newer | per-view AbortController / seq | VERIFIED-HOLDS (code) | `app.js:120 _viewCtl[view] = new AbortController()`, `:1422`, `:1549-1554 seq` |
| P2 Chromium checks (422 shown, 503 retries, one /api/stats) | | NOT-CHECKABLE (Chromium not re-run) | |
| P3 weighted position carries median | | VERIFIED-HOLDS | `economics.py:282-287 median_percentile`; test_round6_consistency::test_weighted_position_names_its_mean_and_carries_the_median |
| P3 steal-share therapy-only + CMS <11 | | VERIFIED-HOLDS | `territory.py:47, 68 (MIN_CELL suppression, "by NPPES taxonomy")` |
| P3 dossier geography gate + peer counts | | VERIFIED-HOLDS | `dossier.py:135-142, 213, 255 require_geographic_scope`; `:239 n_peers`; ::test_the_pitch_report_prints_its_peer_counts |
| P3 basis notes name every toggle | | VERIFIED-HOLDS | `benchmark.py:34 basis_note_for`, used `schedule.py:209`, `benchmark.py:974, 1444`; ::test_basis_note_names_the_toggles_the_query_used |
| P3 engagement `clean_volumes` | | VERIFIED-HOLDS | `engagements.py:191-192`; ::test_engagement_value_refuses_negative_units |
| P3 leverage echoes clamped radius | | VERIFIED-HOLDS | `leverage.py:236-242 _clamp_radius`, `:222 "radius_miles": radius`; ::test_leverage_summary_echoes_the_radius_it_measured |
| P3 leads limit 1,000 "everywhere" + `truncated` | | VERIFIED-HOLDS for leads/leaderboard (`leads.py:27,148,214,332,349`); roster clamps at **2000** (`leads.py:449`) — "everywhere" overstated (P3 wording) | ::test_a_cut_lead_list_says_so |
| P3 ONE masking rule (screen == export) | | VERIFIED-HOLDS | `store.py:347-371` prefix rule, `api.py:1007-1020` SQL twin; `checks2.py`: **0 mismatches** over all 100 two-digit prefixes + `"431234567; 961234567"`, NPI, `000/666/900`-prefixed, dashed (19 masked) |
| P3 concentration median per practice + HHI exact | | VERIFIED-HOLDS (code) | `territory.py:75 _hhi_band`; test_territory not re-run |
| P3 national "% of Medicare" labelled | | VERIFIED-HOLDS | `benchmark.py:938-942` ("the subject's state, applied to peers in every state of this national pool") |
| P3 Code-page export comment | | NOT-CHECKABLE (the "export == screen" comment no longer exists; nothing to be wrong) | `grep "export == screen" api.py` → none |

### Left open in round 6 → now

| item | status | matters for one Windows user? |
|---|---|---|
| Windows unmeasured | STILL-OPEN — rounds 7, 9, 10, 11 each say "not run on Windows" (P9 status, P10 "Not run on Windows", P11 "Not measurable") | Yes — launcher/updater/locks are Windows-only paths; nothing in 0.7.5 was ever executed there |
| NPPES-fed feeds / referral leaders not timed at full size | STILL-OPEN — no later round timed them; `nppes.py:205-215` still takes a nationwide `LIMIT limit*5` window and applies the radius in Python (`:224-236`), `capped` flag at `:215` | Moderate: a 25-mile search with no state sees only the newest 1,000 events nationwide (round-7 P2 item, still true) |
| segmented downloads do not park | SUPERSEDED — round 7 P1.4 took segments out of the wall-clock cap (`fetch.py:449-456`, test_round7::test_a_segmented_download_finishes_past_the_time_cap); round 11 left 3.6 (segment map) open; segments are off by default | Low (default `download_segments: 1`) |

---

## Prompt 7 (round 7 — the whole program)

### Claimed fixed — P0

| item | claim | verdict | evidence |
|---|---|---|---|
| P0.1 `"nan"/"Infinity"/"1e999"` rates | finite only, counted unparseable; `isfinite` in the rollup | VERIFIED-HOLDS | `parser.py:134-157`; `store.py:454, 587 isfinite(negotiated_rate)`; `checks1.py`: `{'nan': None, 'Infinity': None, '-inf': None, '1e999': None, True: None, '34.50': 34.5, '$34.50': 34.5, '2 x 34.50': None}`; test_round7::test_a_string_nan_or_infinity_rate_never_reaches_a_median, ::test_the_rollup_ignores_a_non_finite_rate_already_in_a_part, ::test_clean_rate_refuses_glued_or_boolean_values |
| P0.2 one-pager cross-payer total | no total unless additive; caveat | VERIFIED-HOLDS | `benchmark.py:1164-1177` (`total_at_target … if volumes and additive else None`); ::test_the_negotiation_one_pager_does_not_count_a_unit_once_per_payer |
| P0.3 sparkline moves when a payer enters | fixed (payer, TIN) panel | VERIFIED-HOLDS | `spark.py:110-137 _panel_series`; ::test_a_payer_entering_the_month_never_moves_the_trend |
| P0.4 leads `percent_rank` | mid-rank, all-tied codes excluded, rule written | VERIFIED-HOLDS | `leads.py:31-47 LEADS_RULE`, `quality.py:256-263 midrank_pct_sql`; ::test_a_flat_fee_schedule_puts_everyone_at_p50_and_yields_no_leads, ::test_a_practice_underpaid_where_rates_differ_is_still_a_lead; test_known_answer_leads passed |
| P0.5 user inflation year relabels the basis | per-year names; mixed indexes refused | VERIFIED-HOLDS | `inflation.py:82-128` (`names`, `sources` per year), `:190-199`; `checks2.py` → "2023 is CPI-U and 2025 is MEI — two different price indexes cannot be divided into one another…"; ::test_a_mei_year_is_never_divided_by_a_cpi_u_year, ::test_a_non_finite_index_value_is_refused |
| P0.6 referral leaders grouped by name only | org by (name, state), individual by NPI, one location | VERIFIED-HOLDS | `medicare.py:780-800 pkey = 'org:'||name||'|'||state / 'npi:'||target_npi`; ::test_two_therapists_with_one_name_are_two_practices |
| P0.7 ACS seniors ×3 | explicit 65+ total or exclusive buckets | VERIFIED-HOLDS | `demographics.py:77-114`; ::test_s0101_counts_seniors_once, ::test_dp05_counts_seniors_once, ::test_b01001_sex_by_age_still_sums_both_sexes |
| P0.8 Benchmark volumes box | one `parseVolumes()` for three boxes | VERIFIED-HOLDS (code; Chromium not re-run) | `app.js:1742, 2074, 2617, 2658`; ::test_every_volumes_box_reads_thousands_and_spaces_the_same |

### Claimed fixed — P1

| item | claim | verdict | evidence |
|---|---|---|---|
| P1.1 exports on the system drive, 500 when full | store temp dir, streamed BOM, 243 B/row estimate, 507 sentence | VERIFIED-HOLDS | `api.py:1282 EXPORT_BYTES_PER_ROW = 243`, `:1299 Path(store._tmp_dir)/"exports"`, `:1344 shutil.disk_usage`, `:2696 HTTPException(507, …)`, `:2762 StreamingResponse`; ::test_exports_live_on_the_store_drive_and_a_full_drive_is_a_sentence |
| P1.2 materialised per-practice medians | two tables, own version, identical numbers | VERIFIED-HOLDS (identity by test; timings NOT-CHECKABLE) | `benchmark.py:547-548 practice_medians_v1 / practice_medians_payer_v1`, `:600`; reset unlinks them `store.py:5685`; ::test_every_benchmark_reader_is_identical_from_the_materialised_medians, ::test_the_materialised_medians_follow_the_spine_not_a_name_refresh |
| P1.3 website never reaches cached Leads | `note_user_edit()` | VERIFIED-HOLDS | `store.py:4844`, callers `:5527, :5557`, `inflation.py:160`; ::test_a_saved_website_shows_in_the_cached_leads_at_once |
| P1.4 segmented downloads vs wall-clock cap | exempt; stall-bounded; stop text honest | VERIFIED-HOLDS | `fetch.py:449-456`; stop confirm `app.js:4508` ("parallel connections … starts over"); ::test_a_segmented_download_finishes_past_the_time_cap |
| P1.5 dead thread leaves row in flight | row handed back before respawn | VERIFIED-HOLDS | `fetch.py:3634-3658 store.release_claim(rid)`; ::test_a_dead_downloader_thread_hands_its_row_back |
| P1.6 capped index says `done` silently | cap note in `error`, `truncated` flags, `child_count` = listed | VERIFIED-HOLDS | `fetch.py:2988-3011` (`error=cap_note(...) if truncated`, `child_count=len(child_urls)`), extractors return `truncated` `:1436-1610`; ::test_a_capped_index_says_so_and_counts_what_it_listed |
| P1.7 partly-resolvable reference group | `missing_ref_ids` counted, requeued | VERIFIED-HOLDS | `parser.py:975-987`; `ingest.py:1549` predicate; ::test_a_partly_resolved_reference_group_is_counted_and_refilled |
| P1.8 failed done-upsert leaves a live part | retry, then `drop_rates_part` | VERIFIED-HOLDS | `ingest.py:1048-1058`; ::test_a_failed_done_upsert_withdraws_the_part |
| P1.9 reset/forget leave `.parked` | unlinked + meta keys cleared | VERIFIED-HOLDS | `store.py:5679-5704`; ::test_reset_and_forget_never_bring_a_parked_part_back |
| P1.10 NPPES weekly never reaches the directory | refresh after merge | VERIFIED-HOLDS | `enrich.py:491-503 _refresh_changed_from_parquet(store, cache)`; ::test_a_weekly_update_renames_an_identified_provider_everywhere |
| P1.11 hospital wide CSV / `code|2` / raw modifiers | unpivot, slot scan, `normalize` | VERIFIED-HOLDS | `hospital.py:144-179, 433`; ::test_a_wide_hospital_file_imports_every_payer, ::test_a_tall_file_with_the_cpt_in_code_2_and_raw_modifiers; test_hospital passed (C) |
| P1.12 MPFS title rows / CONV FACTOR | scan to header; CF cross-check; docstring fixed | VERIFIED-HOLDS | `mpfs.py:16, 70-75, 100`; ::test_the_real_2026_cms_layout_imports; test_mpfs passed (C) |
| P1.13 two rounding rules | `half_up` = DuckDB `round()`, no Python `round()` on printed numbers | VERIFIED-HOLDS | `rounding.py:33-45`; `checks2.py`: 0 mismatches vs `SELECT round(x, n)` on 10 tie cases incl. 12.5, 112.5, 2.675, 1.005, −0.5; `roundscan.py` (AST): **zero** `round(` calls in 26 output modules; ::test_half_up_is_the_rule_a_person_and_sql_use, ::test_a_rate_at_112_5_pct_of_medicare_prints_113_everywhere |
| P1.14 erosion block is the market's | headed as such | VERIFIED-HOLDS | `benchmark.py:2075 "<h2>In real terms — the market, not this practice</h2>"`; ::test_the_real_terms_block_is_labelled_as_the_market |
| P1.15 Overview payer index divisor | practice-grain market median, min practices | VERIFIED-HOLDS | `overview.py:98-128` (`practice`/`mkt` CTEs, `_MIN_INDEX_PRACTICES`); ::test_the_payer_index_divides_by_the_practice_market_median |
| P1.16 cross-site POST / DNS rebinding | Host + Origin middleware → 403 | VERIFIED-HOLDS | `api.py:1400-1433 LocalOnlyMiddleware` (`TRUSTED_HOSTS`, Origin / `Sec-Fetch-Site: cross-site` on unsafe methods); ::test_a_foreign_web_page_cannot_change_or_read_the_store |
| P1.17 docs install Python 3.15 | 3.13 named, `py -3.13` | VERIFIED-HOLDS | `HOW_TO_RUN.md:23-26, 80, 111-112, 129-131`; `GETTING_STARTED.md:26-30` |
| P1.18 supervisor loops on port-in-use / bad config | exit 3 honoured; `serve.log` | VERIFIED-HOLDS | `cli.py:210-213 return EXIT_NO_RESTART`, `:1798-1819 _serve_log_path / serve.log(.1)`, `:1898-1903`; ::test_supervisor_stops_on_port_in_use_and_restarts_only_real_crashes |
| P1.19 updater writes before pip / silent downgrade / OSError | pip first, downgrade refused, restore | VERIFIED-HOLDS | `updater.py:122` (`nv < ov and not allow_downgrade`), `:196-201 _restore`, `:222, 251 PIP_STEPS` requirements first; ::test_updater_refuses_a_downgrade_and_rolls_back_a_failed_write; test_updater passed (B) |
| P1.20 stale pickers after ingest | `data_generation` in `/api/stats`, refresh | VERIFIED-HOLDS | `api.py:4346`; `app.js:228-230, 257-271 onDataUpdated`; ::test_stats_carries_the_data_generation_and_it_moves_with_a_rollup |
| P1.21 export navigates away on refusal | hidden frame + sentence | VERIFIED-HOLDS | `app.js:1919-1936 downloadInFrame`, `:1910 refusalText`; ::test_a_refused_export_and_report_show_their_sentence |
| P1.22 "Practices" on the NPI grain | header by grain | VERIFIED-HOLDS | `app.js:939, 998`; ::test_the_by_code_header_and_the_validation_type_column |
| P1.23 market report unhandled rejection | through `openReportWith` | VERIFIED-HOLDS (code) | `app.js:1938, 3079` |
| P1.24 validation prints non-dollar as "$" | Type column, `$` only for dollar | VERIFIED-HOLDS | `app.js:4629-4630` |

### Claimed fixed — P2/P3 subset

| item | verdict | evidence |
|---|---|---|
| `changes` and `payer_months` cached per generation | VERIFIED-HOLDS | `monitor.py:48, 79, 378 @generation_cached` |
| `Store.cached` copies outside its lock | VERIFIED-HOLDS | `store.py:4749-4758` |
| previous placement stamp kept one generation | VERIFIED-HOLDS | `store.py:5272-5282` |
| `paused` rows block forget | VERIFIED-HOLDS | `store.py:3284-3287`; ::test_paused_rows_block_forget_and_the_caches_and_version |
| "fee for service" / "fee_schedule" spellings, unknown arrangements counted | VERIFIED-HOLDS | `parser.py:909-923 unknown_arrangement`; ::test_arrangement_and_type_spellings_are_normalised; test_audit8_ingest::test_stored_fee_schedule_rows_migrate_to_dollars |
| `mrfx --version`; pyproject reads the package version | VERIFIED-HOLDS | `./.venv/bin/mrfx --version` → `mrfx 0.7.5`; `pyproject.toml:3 dynamic = ["version"]`, `:60 version = {attr = "mrfx.__version__"}` |
| setuptools 84.2.1, pip-audit clean | VERIFIED-HOLDS | `requirements.txt:18`; pip-audit → `No known vulnerabilities found` (so `CLAUDE.md:34` "now clean" is true again) |

### Left open in round 7 → now

| item | status | matters? |
|---|---|---|
| 206 after a republish / 416 on a complete `.part` / dead validator branch | CLOSED-LATER (round 11 group 1: status block `c02b_json_splice`, `c02_range_odd`, `c01_truncate_resume`; code `fetch.py:897-914` parses `Content-Range: bytes */N` on 416 and compares validators). Not re-run here. | — |
| `Retry-After` | CLOSED-LATER (`fetch.py:705-708 _retry_after_seconds`, `:943`, `:1344`; round 11 "c05 … 429 Retry-After honoured") | — |
| inline-download policy helper (`process_url_record`) | NOT-CHECKED (`fetch.py:2820`; no shared policy helper found by grep) | low |
| Windows rename locks | CLOSED in code (`replace_with_retry` at `fetch.py:565, 920, 1244`), never executed on Windows | yes, unverified |
| re-paste with a fresh signature for `queued`/`paused` rows | **STILL-OPEN** — `store.py:2631-2663 _enqueue_one` revives only `failed`/`skipped`/`oversize` (and a done toc/page); a queued/paused row returns `None` with its old `url` | P2: a signed-CDN backlog outliving its signature still 403s after the user re-pasted |
| multi-JSON zips read the largest member | STILL-OPEN by design, documented (`GETTING_STARTED.md:287`; `sniff.py:157-170`) | low |
| two files of one payer+month under different names are pooled | **STILL-OPEN** — `store.py:574-576` (`rate_variants`, `source_count`), no supersede rule and no per-(payer, month) multi-file warning found (`quality.py:157 n_files` is per payer only) | P2 touching numbers: a corrected re-publication averages with the file it corrects |
| 8-digit numeric EIN padding | STILL-OPEN (no padding in `parser.py`; a deliberate "decide with the user" item) | low |
| deferred-group memory of the classic fallback | CLOSED-LATER in substance (round 9 3.3: 401 MB file 2.3–2.8 GB → 0.3 GB; round 11 3.10 documents the residual: ≈0.6 GB/worker worst case); spool not built | low |
| enrichment per-batch raw scans | STILL-OPEN — `store.py:5093-5097 unenriched_npis` still `SELECT npi FROM rates` per batch | P3 waste |
| `medicare_anchor` per call | STILL-OPEN — `store.py:5622-5637` loads the whole `mpfs` table into Python on every call; 6 callers (`api.py:1967, 2218`, `benchmark.py:936, 1660`, `market.py:205`, `schedule.py:117`) | P3 waste |
| bulk-cache taxonomy slot | STILL-OPEN (no primary-switch handling in `enrich.py`) | P3 cosmetic |
| ZIP placement in market sizing | NOT-CHECKED | low |
| radius applied after a nationwide LIMIT (new clinics / closures) | **STILL-OPEN** — `nppes.py:205-236` | P2 for a radius search without a state |
| leverage drops unplaced practices silently | STILL-OPEN — `leverage.py:125-140` (no `n_unplaced`) | P3 |
| leave-one-out Leads gap | CLOSED by label (`leads.py:39` "this practice included") | — |
| one-cent move lost (`monitor.py`) | CLOSED-LATER (`monitor.py:281-293 round(c.rate,2) <> round(p.rate,2)`) | — |
| `page=1e20` → 500 | CLOSED-LATER (`api.py:1728 le=10_000_000`) | — |
| OneDrive guidance | docs CLOSED (`HOW_TO_RUN.md:42-43`, `GETTING_STARTED.md:46-47` recommend `C:\mrfx\`); the startup warning when `store_dir` contains `OneDrive` was NOT built (grep in cli/config/store: none) | P3 |
| `PYTHONUTF8=1` in the .bat files | CLOSED-LATER (`Start MRF Explorer (auto-restart).bat:16`, `Update MRF Explorer.bat:13`) | — |
| `mode: off` YAML boolean | CLOSED-LATER (`config.py:32-37` before-validator; yaml comment quotes `"off"` at `config/mrfx.yaml:232`) | — |
| `codes.cpt_codes` traps (empty list / `None` / unquoted ints) | STILL-OPEN — `config.py:24` plain `list[str]`, no before-validator; only the nested-typo warning (`config.py:395-410`) | P3 |
| missing config → cwd-relative defaults | STILL-OPEN (`config.py:358-365` returns `MrfxConfig()` with a warning) | P3 (only if the user deletes `config/mrfx.yaml`) |
| non-finite / negative MPFS rate poisons the strip | **STILL-OPEN, reproduced** — `api.py:4525 float(rate_raw)` with no `isfinite`; `mpfs_probe.py` on a scratch store: `POST /api/mpfs/upload` (`97110,00,nan`) → **200** `{"rows":1}`; then `/api/summary` → **422 "cannot convert float NaN to integer"**, `/api/code/97110` → 422 same, `/api/benchmark/market` → 422 "Out of range float values are not JSON compliant: nan" — until a new anchor is uploaded | P2 (user-visible stuck state; needs a literal `nan`/`inf` token in the CSV) |
| Luhn test in `unenriched_npis` | STILL-OPEN (no `is_valid_npi` in store/enrich) | P3 |
| floors / hospital upload size caps | NOT-CHECKED | low |
| shipped `duckdb_memory_gb: 8` vs "leave it unset" | **STILL-OPEN** — `config/mrfx.yaml:212 duckdb_memory_gb: 8`; `GETTING_STARTED.md:1477` "Leave it unset to let the app pick a kernel-safe default" | P2 on a 16 GB laptop: the auto rule never runs on a fresh install |
| `max_toc_files` / `delete_raw_after_ingest` absent from the shipped yaml | STILL-OPEN — 0 occurrences in `config/mrfx.yaml`; `GETTING_STARTED.md:252, 810, 1045`, `README.md:388, 407` tell the user to set them "in config/mrfx.yaml" (adding the key works) | P3 |
| "`0` turns parking off" | STILL-OPEN wording — `config/mrfx.yaml:196`, `GETTING_STARTED.md:764` list `download_park_limit: 6` then say `0` turns parking off; `config.py:279 download_park_limit … ge=1` refuses 0 (`download_park_seconds: 0` works, `:277 ge=0`) | P3 |
| Windows / full-size NPPES / Hop Teaming | STILL-OPEN (rounds 9–11 repeat "not run on Windows") | yes |

---

## Prompt 8 (audit 8, 0.7.1)

### Claimed done

| item | verdict | evidence |
|---|---|---|
| F1 waiting copy never reconnects; freed when twin differs | VERIFIED-HOLDS | test_early_dedup::test_a_waiting_copy_never_reconnects_and_is_skipped_without_its_link, ::test_the_processor_path_parks_a_waiting_copy (B) |
| F3 early skip needs size + head + tail + windows AND a gz/zip container; plain `.json` never skipped early | VERIFIED-HOLDS | `fetch.py:323 verdict`, `:2191, 2384-2444`; ::test_a_compressed_copy_is_skipped_after_its_first_mb, ::test_a_plain_json_differing_only_in_the_middle_is_never_skipped, ::test_the_default_early_check_is_15_mb |
| Range checks use the download's identity; fingerprint written by a successful ingest; `duplicate_of`; lanes | VERIFIED-HOLDS | ::test_the_end_check_uses_the_identity_the_download_needed, ::test_an_original_without_a_fingerprint_is_fingerprinted_by_its_first_copy, ::test_a_refetch_under_its_old_name_is_not_a_loaded_twin, ::test_small_slots_take_small_files_and_hand_big_ones_over, ::test_lanes_drain_a_mixed_queue |
| P0-1 file-local provider references on all parse paths | VERIFIED-HOLDS | test_audit8_ingest::test_own_reference_table_wins_on_every_parse_path, ::test_remote_reference_is_not_filled_from_a_companion, ::test_file_without_a_table_still_uses_the_companion, ::test_two_same_month_companions_disagreeing_attach_neither, ::test_newer_companion_vintage_wins_without_ambiguity, ::test_pooled_worker_paths_agree |
| P0-2 later run supersedes a parked/doomed part | VERIFIED-HOLDS | ::test_earlier_parked_part_is_not_adopted_over_a_later_run, ::test_earlier_doomed_part_does_not_delete_a_later_run |
| P1-1 row rules 3: stored "fee_schedule" → dollars, "re-ingest suggested" | VERIFIED-HOLDS (rules now at 4, `store.py:518`) | ::test_stored_fee_schedule_rows_migrate_to_dollars, ::test_rule3_migration_rewrites_only_what_changes_and_says_so |
| P1-2 >1 MB standalone reference file stored as references | VERIFIED-HOLDS | `ingest.py:1501-1528 _ingest_reference_file`; ::test_big_standalone_reference_file_is_stored_as_references, ::test_reference_file_streams_in_batches |
| P3-1 malformed values fail in words | VERIFIED-HOLDS | ::test_malformed_value_fails_in_words |
| P0 trajectory on a matched panel | VERIFIED-HOLDS | `monitor.py:541-555`; test_audit8_outputs::test_trajectory_adding_a_code_is_not_a_cut, ::test_trajectory_measures_a_real_move_on_the_matched_panel |
| P1-2 one subject rule | VERIFIED-HOLDS | ::test_multi_state_subject_rate_is_one_number |
| P1-3 worst payer / leaderboard on the lead finder's code set | VERIFIED-HOLDS | `leads.py:232, 368`; ::test_worst_payer_counts_only_ranked_codes |
| P1-4 per-visit value on a common basket | VERIFIED-HOLDS | ::test_visit_value_compares_payers_on_one_basket |
| P1-5 size premium mid-rank | VERIFIED-HOLDS | `quality.py:256-263`; ::test_size_premium_uses_mid_rank |
| P1-6 outreach merge fields house basis + mid-rank + definitions | VERIFIED-HOLDS | `outreach.py:12, 112, 211`; ::test_outreach_merge_fields_are_on_the_house_basis, ::test_outreach_flat_code_is_p50_and_median_matches_sql; test_outreach passed (C) |
| P1-7 sparkline ends at the as-of month, labelled a contract trend | VERIFIED-HOLDS | `spark.py:144-153`; ::test_sparkline_stops_at_the_pinned_month |
| P1-8 inflation source per year | VERIFIED-HOLDS | `inflation.py:92-110`; ::test_index_year_keeps_its_own_source |
| P2-9 `half_up` = DuckDB round; no Python `round()` on printed numbers | VERIFIED-HOLDS | ::test_half_up_is_duckdbs_round; AST scan above |
| P2-10 digest cache follows entity-map edits | VERIFIED-HOLDS | `clients.py:100-104 user_edit_generation`; ::test_digest_follows_an_entity_map_edit |
| P2-11 hospital per-hospital medians | VERIFIED-HOLDS | ::test_hospital_median_is_per_hospital |
| P2-12 market report states as-of / basis / sources | VERIFIED-HOLDS | ::test_market_report_states_as_of_basis_and_sources |
| P1-B same-name inbox drop / upload never silently ignored | VERIFIED-HOLDS | test_audit8_ui::test_inbox_file_under_a_loaded_name, ::test_upload_under_a_loaded_name |
| P1-C Files poll paged, no stacked polls | VERIFIED-HOLDS | ::test_files_poll_is_paged_and_light |
| P1-D download settings shown on the Files tab | VERIFIED-HOLDS | `api.py:2490-2492 _download_settings` |
| P2-E waiting copies counted/badged | VERIFIED-HOLDS | `app.js:4335`; ::test_waiting_copies_are_counted_on_their_own |
| P2-F refusals read `detail` or `error` | VERIFIED-HOLDS | `app.js:1910-1913, 4651`; ::test_bad_input_is_refused_in_words |
| P2-G export names sanitised; temp files swept | VERIFIED-HOLDS | `api.py:1312 _sweep_stale_exports`, `:2675-2684`; ::test_export_view_name_is_sanitised |
| P2-H starter list generated from the catalog (109), HOW_TO_RUN parked text, README browser note | VERIFIED-HOLDS | `grep -c '^https\?://' config/starter_links.txt` → **109**; ::test_starter_links_is_the_catalog; `HOW_TO_RUN.md:196-198`; `README.md:409-412` |

### Not done in audit 8 → now

| item | status | matters? |
|---|---|---|
| 1 settings screen | STILL-OPEN (no `/api/settings`; Files tab shows the values) | low |
| 2 sparkline at the printed grain | unchanged by design (`spark.py:110-137`) | low |
| 3 memory of deferred groups | reduced in round 9 (3.3) and bounded/documented in round 11 (3.10); spill not built | low |
| 4 classic reader on malformed non-target values | STILL (round 9 status: "still cannot pass over NaN / Infinity / 20-digit numbers; it now says so in words") | low (fast reader is the default) |
| 5 re-time outreach.zip on the 99M store | CLOSED-LATER (round 9 status: 69–103 s → 50–51 s, CSV byte-identical) | — |
| 6 same-name detection for pre-audit-8 files is size-only | STILL (by design; `--force`) | low |

---

## Doc text that is now false or self-contradictory (DOC-STALE)

1. `docs/AI_HANDOFF.md:69` — says `size_premium` ranks "with `percent_rank()` WITHIN (payer, code)"; the code uses `midrank_pct_sql` (`quality.py:256-263`) and `AI_HANDOFF.md:41` itself says "`percent_rank()` is no longer used for any printed position". P3.
2. `docs/AI_HANDOFF.md:949` — "the ~3-min inner loop" vs `CLAUDE.md:32` "~4.5 min (562 tests)" and the round-11 status (4.7 min). P3.
3. `README.md:345` "110 entries … auto-queueable" and `README.md:363` "the rest (34) are portals" vs `config/known_sources.yaml`: 150 entries, **109** queueable, **41** portals (round-7 P3 called this out; still there). P3.
4. `GETTING_STARTED.md:1477` "Leave it unset to let the app pick a kernel-safe default" vs the shipped `config/mrfx.yaml:212 duckdb_memory_gb: 8`. P2 (see above).
5. `config/mrfx.yaml:196` and `GETTING_STARTED.md:764` "`0` turns parking off" right after `download_park_limit: 6` — `download_park_limit: 0` is refused (`config.py:279 ge=1`). P3.
6. `GETTING_STARTED.md:252, 810, 1045` and `README.md:388, 407` tell the user to set `max_toc_files` / `delete_raw_after_ingest` "in config/mrfx.yaml"; neither key appears in the shipped file, even commented. P3.
7. Prompt-6 status "leads limit 1,000 everywhere" — the payer roster clamps at 2,000 (`leads.py:449`). P3 (status-block wording only).

No REGRESSED item was found among the claimed-fixed behaviours that could be checked: every round-6/7/8 pin that touches numbers, states, messages or install paths passed on the shipped copy (287 tests across the three batches) and the code sites match the claims.

---

## What is wrong in 0.7.5 (sorted by severity)

- **P2 — non-finite MPFS rate accepted, then every anchored view 422s.** `api.py:4525` `float(rate_raw)` has no `isfinite` check; reproduced: upload `97110,00,nan` → 200, then `/api/summary`, `/api/code/97110`, `/api/benchmark/market` all 422 ("cannot convert float NaN to integer" / "Out of range float values") until a new anchor is uploaded. Round-7 P2 item, never closed.
- **P2 — two files of one payer + month under different names are pooled, not superseded** (`store.py:574-576`); no warning anywhere. A corrected re-publication averages with the file it corrects. Round-7 "left" item, still open; touches sold numbers.
- **P2 — shipped `config/mrfx.yaml:212` sets `duckdb_memory_gb: 8`** while `GETTING_STARTED.md:1477` says to leave it unset; the kernel-safe auto rule never runs on a fresh install (8 GB pool beside parser workers on a 16 GB laptop). Round-7 P2(a), still open.
- **P2 — re-pasting a link with a fresh signature does not refresh a `queued`/`paused` row's URL** (`store.py:2631-2663`); a signed-CDN backlog still 403s hours later. Round-7 "left", still open.
- **P2 — new-clinic / closure feeds apply the radius after a nationwide `LIMIT limit*5`** (`nppes.py:205-236`); a radius search with no state sees only the newest 1,000 events nationwide (flagged `capped`, not fixed). Round-7 P2, still open.
- **P3 — doc/config drift:** AI_HANDOFF.md:69 `percent_rank` text; AI_HANDOFF.md:949 "~3-min"; README.md:345/363 catalog counts (110/34 vs 109/41); "`0` turns parking off" (`mrfx.yaml:196`, GETTING_STARTED.md:764); `max_toc_files` / `delete_raw_after_ingest` documented as yaml keys but absent from the shipped file.
- **P3 — waste / silent drops left from round 7:** `medicare_anchor` loads the whole `mpfs` table per call (`store.py:5622-5637`, 6 callers); `unenriched_npis` re-scans raw `rates` per batch (`store.py:5093-5097`); leverage silently drops practices with no NPPES ZIP (`leverage.py:125-140`); bulk-cache taxonomy slot; no Luhn gate before API enrichment; `codes.cpt_codes` has no before-validator; a missing config falls back to cwd-relative paths (`config.py:358-365`); no startup warning for a OneDrive `store_dir`; payer roster limit 2,000 vs "1,000 everywhere".
- **Not measurable here (unchanged since round 6):** nothing in 0.7.5 has run on Windows (launcher, updater, rename locks, held-file folders, `.bat` under cp1252), and the NPPES-fed feeds / referral leaders were never timed against a full-size cache or release.

## Still open and worth doing (for one non-technical Windows user)

1. Refuse `nan`/`inf`/`<= 0` MPFS rates at upload with a line-numbered 422, and treat a non-finite stored anchor as "no anchor" (one `math.isfinite` at `api.py:4525` + a guard in `_with_anchor`).
2. Decide the same-(payer, month) multi-file rule: at least a quality warning "payer × month has N files" on the Files tab and in the methodology; better newest-file-per-contract supersede.
3. Ship `duckdb_memory_gb` commented out (and make the auto rule subtract the workers' budget), and add the documented keys (`max_toc_files`, `delete_raw_after_ingest`, …) as commented examples in `config/mrfx.yaml`, with a test that every documented key appears there.
4. Refresh `url` on re-paste for `queued`/`paused` rows (`_enqueue_one`).
5. Filter new-clinic / closure feeds on miles inside the SQL before the LIMIT (the leaders query already does).
6. Fix the six doc lines above (AI_HANDOFF:69/949, README:345/363, parking "0", yaml keys).
7. One real Windows pass of the install → Start → update → double-launch → Ctrl-C path; it is the user's only platform and has never been executed.
