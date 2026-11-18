# Audit 12 — digest of NEXT_STEPS_PROMPT_2..5 against the shipped 0.7.5 build

Checked against the extracted zip at `scratchpad/audit12/unzipped/Claudevideo`
(identical to repo head 68d9bc7, `mrfx.__version__ == "0.7.5"`, duckdb 1.5.5,
Python 3.13). File:line references below are to that copy (== repo).
Evidence that was RUN (all from the extracted copy, `-p no:cacheprovider`):

- Batch 1 (numbers suites): `test_known_answer.py test_known_answer_modifiers.py
  test_known_answer_months.py test_known_answer_entity.py test_known_answer_basis.py
  test_modifiers.py test_timezones.py test_file_month.py test_medicare_locality.py`
  → **91 passed** in 235 s (`agent_2_5/run1.log`).
- Batch 2 (state/robustness suites): `test_determinism.py test_latest_flag.py
  test_rollup_visibility.py test_queue_signal.py test_install_paths.py
  test_unit_filters.py test_part_drift.py test_stop_parse.py test_known_answer_leads.py
  test_cli_enrich.py test_updater.py test_nppes_auto.py` → **113 passed** in 214 s
  (`agent_2_5/run2.log`).
- `agent_2_5/check_rules.py` (pure-function rules: modifiers, SSN mask SQL vs
  Python over all 100 prefixes, file_month rule) — output quoted inline below.
- `agent_2_5/npi_drawer_latest.py` — a two-month scratch store through the real
  ingest → rollups → FastAPI test client (found the one numbers regression).
- `agent_2_5/idle_cpu.py` — a real `mrfx serve` on an empty scratch config for
  60 s, CPU from `/proc/<pid>/stat`.

Verdict key: VERIFIED-HOLDS / REGRESSED / STILL-OPEN / DOC-STALE / NOT-CHECKABLE /
CLOSED-LATER (a left-open item a later round closed).

---

## Prompt 2 (round 2 — the answer layer)

| # | Item (claim) | Verdict | Evidence |
|---|---|---|---|
| 2.1 | P0.1 known-answer test exists; Code comparison uses newest file per contract (was pooling $40 May + $42 June = "$41") | VERIFIED-HOLDS | `tests/mrfx/test_known_answer.py` present and green (batch 1). `api.py:2128-2134` `/api/code` sets `latest=1` unless a month is pinned. Repro (`npi_drawer_latest.py`, May '' $40 / June GP $42): Code page `payer_rank` median **42.0**, `ranked` 42.0. |
| 2.2 | P0.1 exports reproducible: `min(source_file)` not `any_value` | VERIFIED-HOLDS | `store.py:445`, `store.py:577` `min(source_file) AS source_files`. |
| 2.3 | P0.3 month contract: Explorer = all months pooled with a Month column; Code view / reports = newest per contract | DOC-STALE (behaviour changed on purpose in round 10) | 0.7.5 Explorer defaults to **latest**: `mrfx/web/index.html:180` (first option `value="latest"`), `app.js:1066-1068, 1123` (`$("#f-month").value = "latest"`), commit 23d19a2. `docs/AI_HANDOFF.md:1117-1118` still says "the Explorer shows EVERY ingested month … round 5 kept this default" and `:997` "the Explorer's pooled default". `HOW_TO_RUN.md:255` and `GETTING_STARTED.md:1103-1105` are correct. |
| 2.4 | P0.4 export == screen at every grain | VERIFIED-HOLDS | pinned by `test_known_answer.py` (green); `order_sql(src=)` `api.py:994-1004` sorts on raw columns so masked rows don't tie. |
| 2.5 | P0.5 SSN-pattern TINs masked in every displayed field and export; `unit_id`/`website_tins` stay raw as machine keys | VERIFIED-HOLDS (rule later tightened) | ONE rule: `store.py:347-378` (`_VALID_EIN_PREFIXES`, `looks_like_ssn`, `mask_tin`); SQL twin `api.py:1007-1019`. `check_rules.py`: SQL == Python for all 100 two-digit prefixes (0 mismatches); list form `"123456789; 001234567"` → `123456789; MASKED-SSN`; 10-digit NPI never masked; the hard-coded prefix list inside `TIN_DIRECTORY_QUERY` (`store.py:734-737`) == `SSN_PREFIXES` (17 values). Note: the "unit_id stays raw" design is superseded — the CSV export now masks `unit_id` too (`api.py:1107`); drawer JSON `unit_id` is still raw (`api.py:2079`). Not wrong, just older than the prompt text. |
| 2.6 | P1 big-store guard for NPI/entity views | VERIFIED-HOLDS | `api.py:1684-1720` `_require_narrowing`; called by `/api/rates` (1734), `/api/summary` (1844), `/api/code` (2139), `/api/trend` (2238). |
| 2.7 | P1.4 RAM-aware auto `parallel_ingests` | VERIFIED-HOLDS | `fetch.py:1965-2022` (`WORKER_RAM_GB=2`, `OS_RESERVE_GB=3`, pool = `duckdb_memory_gb` or `max(2,min(12,int(0.4*RAM)))`). GETTING_STARTED.md:583-585's "8 GB → 1, 16 GB → 3, 32 GB 8-core → 7" matches the formula exactly. |
| 2.8 | P1.5 per-file QA runs in the parse worker (private in-memory DuckDB) | VERIFIED-HOLDS | `ingest.py:277` docstring + implementation. |
| 2.9 | P1 scale numbers (NPI search 0.6-1.2 s, removal 165 s, summary 5.2 GB, rebuild 65 min on 306M rows) | NOT-CHECKABLE | no 306M store here (7 GB disk free). |
| 2.10 | P2 Windows: cleanup in a `finally` could raise on an AV lock — fixed + test | VERIFIED-HOLDS | `ingest.py:965-985` every delete in the `finally` is `_cleanup_unlink` / `rmtree(ignore_errors=True)`. |
| 2.11 | P3.1 `mrfx add` NPPES lookups: remaining count, progress, Ctrl-C skips | VERIFIED-HOLDS | `cli.py:465-529`; `test_cli_enrich.py` green (batch 2). |
| 2.12 | P3.2 fair retry scheduling in `_claim_next` | VERIFIED-HOLDS | `store.py:2716-2723` `ORDER BY {lane} coalesce(retries,0), id`; `retries` incremented at `store.py:3503, 3587`. |
| 2.13 | P3.3 expired signed link names its parent index | VERIFIED-HOLDS | `fetch.py:1990-2006` `_failure_message` (403 + parent_id → "This link came from the index … paste that index again"). |
| 2.14 | P3.4 cancel an in-flight parse | VERIFIED-HOLDS | `ingest.py:589-598` `ParseStopped`, `store.py:3658` `request_parse_stop`; `test_stop_parse.py` green. |
| 2.15 | P4 `pytest -m quick` exists | VERIFIED-HOLDS | `tests/conftest.py:40-53`; `tests/slow_tests.txt` 616 entries. |
| 2.L1 | LEFT OPEN: full rebuild 65 min (C1) | STILL-OPEN | prompt 11 status "3.1 single-pass rollup … Not built"; `AI_HANDOFF.md:1085`; no `PARTITION_BY` in `store.py`. Matters only before a `ROLLUP_SCHEMA_VERSION` bump — none since. |
| 2.L2 | LEFT OPEN: Windows run | STILL-OPEN | every later status block repeats "not run on Windows" (prompt 11 §Windows; `AI_HANDOFF.md:1077-1079`). Still the biggest unknown for the one real user. |
| 2.L3 | LEFT OPEN: P3.5 timing table measured on a 4-core Linux box | STILL-OPEN (honest) | `GETTING_STARTED.md:569-575, 606-608` still the Nov-2026 4-core Linux numbers, labelled "your PC will differ". |
| 2.L4 | LEFT OPEN: Aetna-generator fixture < 2 MB | STILL-OPEN (low value) | `tests/mrfx/fixtures/real/` has 5 files (WY, FloridaBlue, Moda, Tufts, UHC), none Aetna; parser frozen since, `fastcheck` covers real files. |

## Prompt 3 (round 3 — the numbers rules)

| # | Item (claim) | Verdict | Evidence |
|---|---|---|---|
| 3.1 | P0.1 `modifiers.py`: split `52,53`; POS lists moved to service_code; `[GP,59]==[59,GP]`; First Choice `26|NU|RR|TC|UE` kept; one base rule; existing stores fixed at open | VERIFIED-HOLDS | `check_rules.py`: `["52,53"]→['52','53']`; `["21","31","22","24"]+pos["01"]` → mods `[]`, pos `['01','21','22','24','31']`, `pos_moved=True`; `GP,59` both orders equal; First Choice list kept; bare `["22"]` left alone; idempotent. `base_sql()` = `'' | GN | GO | GP | pairs | GN|GO|GP` (`modifiers.py:87-92`). `ROW_RULES_VERSION = 4` / `_migrate_row_rules` `store.py:518, 1471`. `test_known_answer_modifiers.py`, `test_modifiers.py` green. |
| 3.2 | P0.2 "latest" decided before row filters; partition carries `tin_is_really_npi`; `/api/code` + `/api/trend` call `_require_narrowing` | VERIFIED-HOLDS for Explorer/Code/TIN+entity grains — **but see 3.2b** | `api.py:838-889` `rel_sql` (key/unit/ident first, row filters outside); `_NPI_LATEST_QUALIFY` `api.py:833-834` and `LATEST_PARTITION` `store.py:628` both `payer, tin_value, tin_is_really_npi, billing_code`. Repro: Explorer NPI grain latest = `[('97110','GP','2026-06',42.0)]` only; TIN grain same. |
| 3.2b | (new) the NPI **drawer chart** "current rate" | **REGRESSED (P0 by the rubric: a wrong number; narrow trigger)** | `api.py:2039-2042` (added in review 10 item 2.5, which itself specifies "newest month per contract") partitions the NPI-grain latest window by `payer, tin_value, billing_code, modifier_set, billing_class, service_code_set` — finer than the contract rule and without `tin_is_really_npi`. Repro `npi_drawer_latest.py` (one payer, one TIN/NPI, 97110; May no-modifier $40, June GP $42): `/api/entity/npi/<npi>` chart **median_rate 41.0** while `/api/entity/tin/<tin>` chart = 42.0, Explorer latest = 42.0, Code page = 42.0. The superseded May row survives because its modifier set has no June successor. Trigger: a contract whose base-modifier spelling changes between months ('' ↔ GP/GO/GN) or drops a base line. No test references the drawer `chart` (grep of `tests/mrfx/*.py` for `chart` → none). |
| 3.3 | P0.3 entity grain: each member at its own newest month; filters per member; key = org name + main state; person-named TINs stand alone; honest variant/NPI counts | VERIFIED-HOLDS | `api.py:236-252` `_ENTITY_KEY_SQL`; `api.py:276-333` (per-member latest input, `median`, `len(list_distinct(rates))`, `list_distinct` counts); `test_known_answer_entity.py` green. |
| 3.4 | P0.4 one practice basis for leaderboard / by-code / Benchmark | VERIFIED-HOLDS | `api.py:961-977` `practice_basis` (dollar, >0.01, NOT tin_is_really_npi, professional, `base_sql`); `test_known_answer_basis.py` green. |
| 3.5 | P0.5 deterministic names and ORDER BYs | VERIFIED-HOLDS | `store.py:685-725` `first(org_name ORDER BY c_org DESC, org_name)`, `min(r.tin_type)`; `api.py:989-991` 11-column `_ORDER_TIEBREAK` incl. `tin_value`, `negotiated_rate`, `billing_class`, `is_dollar_rate`; every remaining `mode()` carries `ORDER BY` (`orgprofile.py:73-77`, `medicare.py:803,844`, `demographics.py:402`); `test_determinism.py` green. |
| 3.6 | P0.6 future header → file-name month; `published_month` recorded | VERIFIED-HOLDS | `parser.py:294-316`; `check_rules.py`: header `2026-12-01` read 2026-11 → `('2026-11', True)`; header `2026-08-25` + name `2026-10-01` → `2026-08`; `published_month_of` → `2026-10`. Column `store.py:2339`; `test_file_month.py`, `test_known_answer_months.py` green. |
| 3.7 | P0.7 one UTC clock + one-time conversion | VERIFIED-HOLDS | `store.py:531-538` `_utcnow` naive UTC, `store.py:2048-2055` `SET GLOBAL TimeZone='UTC'`, `ingest.py:331-333`; `test_timezones.py` green. (Marker still compared as a string at `store.py:1910,1939,4187` — safe because every stamp is naive-UTC ISO, monotonic.) |
| 3.8 | P0.8 % of Medicare per state locality, refused on mismatch | VERIFIED-HOLDS | `store.py:5622-5657` `medicare_anchor` (refusal text names the loaded vs asked state); `test_medicare_locality.py` green. |
| 3.9 | P1.1 no silent thread death: nt `OpenProcess` fallback, worker raises RuntimeError not SystemExit, loops catch BaseException, coordinator respawns | VERIFIED-HOLDS | `store.py:297-328` `_nt_alive_without_handle` (only error 87 = dead); `ingest.py:686-688`; `fetch.py:3505, 3580, 3601`; `fetch.py:3631-3675` `respawn_dead_threads`. Windows branch itself never executed (no Windows). |
| 3.10 | P1.2 monthly refresh scoped to loaded sources, not-yet-posted grace | VERIFIED-HOLDS | `fetch.py:1862-1961` (`done` rows only, `NOT_POSTED_GRACE_DAYS = 10`, daily retry, month stamped only when done or past day 10). |
| 3.11 | P1.3 stop/cancel loose ends (a)-(e) | VERIFIED-HOLDS | (a) `ingest.py:1440-1446` `clear_parse_stop` in `finally`; (b) `ingest.py:982-985` qa-spill rmtree in parent finally; (c) `ingest.py:954-961` CancelledError → `PoolRestarted` → row `queued` (1430-1433); (d) `POST /api/urls/resume-stopped` `api.py:2475`; (e) `store.py:3690-3699` `bytes_total > 0`. `test_stop_parse.py` green. |
| 3.12 | P1.4 NPPES cross-process lock + in-memory last-check fallback; 59 s cache build | VERIFIED-HOLDS (code) / NOT-CHECKABLE (59 s) | `nppes_auto.py:112-140` (`O_EXCL` lock, read-only stamp fallback); `test_nppes_auto.py` green. |
| 3.13 | P1.6 (a,b,c,f,g) parts never drift from rollups | VERIFIED-HOLDS | (a) `store.py:1139-1141` + `_flag_quarantined_files` `1244-1263` (row → failed, `rollup_needs_full`); (b) `doomed_parts` `3857-3866`; (c) `ingest.py:1438` `requeue_part_payers` on failure; (f) `store.py:3612-3625` set populated inside `write_lock` before the UPDATE; (g) `parked_parts` `2434-2470`, adopted at open `1508-1512`. `test_part_drift.py` green. |
| 3.14 | P2.3 `/api/stats` header counts from metadata + spine | VERIFIED-HOLDS | `api.py:4322-4336` reads `files` rows/QA JSON, no raw scan. |
| 3.15 | P2.5 concatenated-JSON indexes, ZIP-in-ZIP, link cap keeps rate files first | VERIFIED-HOLDS | `fetch.py:1465-1467` `ijson.parse(multiple_values=True)`; `sniff.py:158-166` inner zip; `fetch.py:1710-1742` `_link_priority` sort. |
| 3.16 | P3 `states:` on all sources + "Queue these states"; `mrfx sources --check`; HealthSparq fixture | VERIFIED-HOLDS | `config/known_sources.yaml`: 150 entries / 150 `states:` (catalog grew from 145); `cli.py:860-871, 1977-1978`; `tests/mrfx/test_fetch.py:2525`. |
| 3.N1 | NOT done: P1.5 Windows | STILL-OPEN | see 2.L2. |
| 3.N2 | NOT done: P1.6(d) OOM ladder per-table completion; (e) `unenriched_npis` O(store) | STILL-OPEN | `AI_HANDOFF.md:1083-1084` lists both as open; `store.py:5079-5110` is still a live `SELECT DISTINCT npi` over `rates`. Performance only; matters in API-mode enrichment on a big store. |
| 3.N3 | NOT done: P2.1 single-pass rebuild | STILL-OPEN | prompt 11 status (3.1 "Not built"); only matters before a spine-schema bump. |
| 3.N4 | NOT done: P2.2 sorted parts; P2.4 Aetna worker-RSS re-measure | STILL-OPEN | no sort in the part writer (grep `RatesPartWriter` … `sort` → none); `AI_HANDOFF.md:1089-1090`. |
| 3.N5 | NOT done: `.7z` / `.zst` | STILL-OPEN (documented) | `fetch.py:3090` "unrecognized file (not MRF JSON)"; `ingest.py:1798` only recognises the `.zst` suffix for naming; `AI_HANDOFF.md:1091-1093`. Two catalog payers (Baylor Scott & White, MercyCare) stay unreadable. |

## Prompt 4 (round 4 — output speed)

| # | Item (claim) | Verdict | Evidence |
|---|---|---|---|
| 4.1 | P0.1 `rates_by_tin_tbl.is_latest` added in place at open, written by every build slice, identical to the old window | VERIFIED-HOLDS | `store.py:628-631` `LATEST_PARTITION`; `store.py:1349-1379` `_migrate_latest_flag` (one transaction, rollback on failure); `api.py:235` `_TIN_REL_LATEST`; `test_latest_flag.py` green (sliced-build exactness). |
| 4.2 | P0.2/P0.3 `stat_rel` off the bare spine; page-first late join covers latest; header counts per generation; rate-NPI set per parts signature | VERIFIED-HOLDS | `api.py:920` `stat_rel`, `api.py:176` `_tin_rows_by_unit`; `store.py:2194, 2241, 5301-5315` `rate_npi_parts`/`rate_npi_set`. |
| 4.3 | P0.4 Code page renders ranking first, aborts superseded clicks | VERIFIED-HOLDS | `mrfx/web/app.js:120` (`_viewCtl[view] = new AbortController()`), `app.js:1422`. |
| 4.4 | "Found on the way": `mode()`, ranked lists, rate-change cap, float averages now identical bytes per call | VERIFIED-HOLDS | `test_determinism.py` green; see 3.5 for the `mode()` audit. |
| 4.5 | Timings (158 s → 1.2 s etc.) | NOT-CHECKABLE | no named 32M / 99M store on this box. |
| 4.L1 | Left: P2.1, P2.2 | STILL-OPEN | see 3.N3 / 3.N4. |
| 4.L2 | Left: unit-filter semi-joins | CLOSED-LATER (partly) | round 5 `_tin_rows_by_unit` for state/city/name/partial-ID (`api.py:850-880`); "therapy only" alone deliberately keeps the join (`api.py:852-857`, AI_HANDOFF.md:38). |
| 4.L3 | Left: leverage/dossier ZIP placement from raw rows (3.8 / 5.2 s) | CLOSED-LATER | prompt 6 status: placement from a stamped parquet, leverage 7.3 → 0.5 s named, dossier 6.2 → 1.6 s. |

## Prompt 5 (round 5 — install robustness, idle cost)

| # | Item (claim) | Verdict | Evidence |
|---|---|---|---|
| 5.1 | P0.1 both `.bat` files run `.venv\Scripts\python.exe -m mrfx` from their own folder with a plain sentence if the venv is missing; `mrfx update` re-links with `--no-build-isolation` (setuptools pinned) and verifies the import path; `serve` warns when running another folder's code | VERIFIED-HOLDS | `Start MRF Explorer (auto-restart).bat` lines 13-31, `Update MRF Explorer.bat` lines 9-27; `updater.py:248-266` (`pip install -q --no-build-isolation --no-deps -e .` + `mrfx.__file__` check); `requirements.txt:18` `setuptools==84.2.1`; `cli.py:157-188` `_warn_if_running_another_folders_code` called from `cmd_serve`. `test_install_paths.py`, `test_updater.py` green. (Windows `.bat` semantics themselves untested — no Windows.) |
| 5.2 | P1.1 idle `mrfx serve` 0.4 % of a core, queue `Condition`, new link claimed in 17 ms | VERIFIED-HOLDS (measured) | `idle_cpu.py` on an empty scratch store, 60 s after a 20 s warm-up: **27 jiffies = 0.45 % of one core, 24 threads** (claim: 24 jiffies / 0.4 %, 24 threads). Wiring: `store.py:968` Condition, `2793-2808` `queue_epoch/queue_changed/wait_queue_change`, `5725-5752` decorator over 7 "always" + 20 "if done" writers; waiters `fetch.py:3462-3476, 3571-3589`. `test_queue_signal.py` green (fails if a writer is missing from the list). |
| 5.3 | P1.2 DuckDB 1.5.5/1.5.6 empty-CTAS visibility bug avoided with explicit DDL | VERIFIED-HOLDS (workaround) | `store.py:4636-4654`; `test_rollup_visibility.py` green. `requirements.txt:6` still pins `duckdb==1.5.5`, i.e. the affected engine with the workaround — the upstream fix (2.0.0) has not been adopted. |
| 5.4 | P1.3 decision (b): Explorer keeps pooled default; strip cached per generation | DOC-STALE (decision reversed in round 10) | see 2.3: default is now `latest`. The per-generation strip cache still exists (`api.py:893-895`, AI_HANDOFF.md:38). Prompt 5's status block and `AI_HANDOFF.md:997, 1117-1118` describe the old default. |
| 5.5 | P1.4 selective filters find practices first (`_tin_rows_by_unit`), identical relation | VERIFIED-HOLDS | `api.py:176, 850-880`; `test_unit_filters.py` green. |
| 5.6 | Leads tab known-answer fixes | VERIFIED-HOLDS | `test_known_answer_leads.py` green. |
| 5.7 | P2 TIN→ZIP side table NOT built | CLOSED-LATER | round 6 built the stamped placement parquet (prompt 6 status). |
| 5.8 | Quick list regenerated | VERIFIED-HOLDS | `tests/slow_tests.txt` 616 entries (regenerated again in rounds 7, 9, 10, 11). |
| 5.9 | P3 histogram gutter; `httpx2` recorded not installed | VERIFIED-HOLDS | `app.js:1325-1329` `left = Math.max(40, 14 + widest*7)`; `requirements.txt:22-25`. Consequence still visible: `StarletteDeprecationWarning: Using httpx with starlette.testclient is deprecated` printed on every TestClient import (seen in my repro and both batches' "2 warnings"). Test-only. |
| 5.10 | Not measured: Windows; P1.1 under a 20,000-link queue; 300M real store | STILL-OPEN | unchanged through round 11. |

---

## What is wrong in 0.7.5 (sorted by severity)

1. **P0 (wrong number, narrow trigger) — NPI drawer "current rate" chart uses a per-modifier/class/POS latest window instead of the contract rule.** `api.py:2039-2042`. Repro `agent_2_5/npi_drawer_latest.py`: same NPI, same contract, May no-modifier $40 → June GP $42: NPI drawer chart **$41.00**; TIN drawer chart, Explorer (latest), Code page all **$42.00**. Introduced by review 10 item 2.5 (whose own text says "newest month per contract"); the partition also omits `tin_is_really_npi`. No test covers the drawer `chart`. Fix: use `_NPI_LATEST_QUALIFY` (payer, unit_id, tin_value, tin_is_really_npi, billing_code) and then apply the house-basis filter; add a known-answer case.
2. **P2 (stale documentation) — the AI handoff still documents the Explorer's pooled default** that round 10 replaced with "latest": `docs/AI_HANDOFF.md:997` and `:1117-1118` ("the Explorer shows EVERY ingested month … round 5 kept this default"); `docs/NEXT_STEPS_PROMPT_5.md` status block P1.3 (b). An engineer following AI_HANDOFF would describe the wrong default to the user. (`HOW_TO_RUN.md:255`, `GETTING_STARTED.md:1103-1105` are right.)
3. **P3 — GETTING_STARTED's what-to-expect timing table is still the 4-core Linux measurement** (`GETTING_STARTED.md:569-575, 606-608`), labelled as such; round 2 P3.5 asked for the user's machine class. Honest but unfulfilled.
4. **P3 (test-only) — `httpx2` deprecation warning on every test run** (`requirements.txt:22-25` records it; not installed). Will become an import failure on the next starlette bump, as the note says.

Everything else claimed closed in rounds 2-5 that I could check holds in 0.7.5 (204 tests from the pinning suites green; the pure-function rules, masking equivalence, month rule, idle CPU re-measured).

## Still open and worth doing (for one non-technical user on Windows)

- **Windows first run by an agent** — open since round 2, repeated in every status block through round 11. The round-3 Windows-only branches (`_nt_alive_without_handle`, parked-part swaps, rename locks, the `.bat` launchers) have only Linux simulations.
- **Regression test for the NPI drawer chart** (item 1 above) and a sweep of any other "latest" window written outside `rel_sql`/`_TIN_REL_LATEST` (grep `QUALIFY file_month` → `api.py:833, 885, 2041`, `benchmark.py:394`; only `api.py:2041` deviates).
- **duckdb 1.5.5 → 2.0.0 evaluation** — the store carries a workaround for an engine bug the pinned version has; the upstream fix exists.
- **P2.1 single-pass rebuild** before the next `ROLLUP_SCHEMA_VERSION` bump (none needed yet; 65 min at 300M rows if one comes).
- **P2.2 sorted parts** — the NPI view's payer-only / code-only filters at 300M rows (30-51 s measured in round 3) are unaddressed; the big-store guard hides it by refusing.
- **`unenriched_npis` O(store) per call in API-mode enrichment; OOM-ladder per-table completion** (`AI_HANDOFF.md:1083-1084`).
- **`.7z` / `.zst`** — two catalog payers unreadable; decide on the two dependencies or drop the entries from the catalog with the reason.
- Low value now: the Aetna fixture (parser frozen, `fastcheck` covers it); re-measuring the timing table.
