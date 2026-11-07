# Prompt for the next session, round 7 — the whole program, end to end (written for Claude Opus 5.5)

Rounds 1–6 are `docs/NEXT_STEPS_PROMPT.md`, `_2.md` … `_6.md`; each has a
status block of what it closed. This memo came out of a whole-program audit on
Nov 7, 2026 (HEAD f5bb463): is everything running as it should — accuracy,
speed, efficiency, robustness, honesty? Method: every output timed twice on the
named real-data store (1.4M spine rows) and the 99M-rate synthetic store
(29.6M latest spine rows) at HEAD; the named store's 125 snapshotted endpoints
run twice and diffed byte for byte; per-SQL-statement profiles of the eight
slowest first calls; peak RSS of the dashboard process on both stores; a real
`mrfx serve` fed ten file shapes through its inbox; a real two-file ingest
timed with peak memory; the quick and full test suites timed; pip-audit; and
eight independent line-by-line reads (security and hostile input; parser and
ingest; the dashboard; the download queue; external-data imports; store.py;
CLI, updater, launchers and docs; and the numbers themselves, worked by hand),
each claim then checked by this reviewer against the code or reproduced in a
throw-away test. Every item below carries its reproduction or its file:line;
nothing is listed on suspicion, and what was checked and found fine is listed
at the end so it is not re-chased. Copy everything below the line into a fresh
session on this repo.

---

You are picking up MRF Explorer (`mrfx/`). Read `CLAUDE.md`, then
`docs/AI_HANDOFF.md` in full (Steps 3, 4, 5 and 8), then
`git log 929cd96..HEAD` (round 6: per-payer months, the 500s, tie orders, the
measured costs, the dashboard tickets, the P3 consistency fixes, download
parking, the Aetna brand listings). The product: one non-technical user on a
Windows PC pastes payer links, then searches by NPI to see what each payer
reimburses for PT/OT/SLP codes, and SELLS the outputs. Correctness of the
numbers beats speed; "should work" is not done; every claim needs a live
number.

## Non-negotiables

- Numbers do not move except where an item below says the current number is
  wrong. Such a change is pinned by a hand-worked test, and the snapshot diff
  (`scratchpad/perf/snapshot.py` + `diff_snap.py`, baseline = HEAD before
  your first commit) may differ only on the endpoints the item names.
- No schema-version bump of the rate spine (`ROLLUP_SCHEMA_VERSION`): a
  rebuild of a 300M-row store is hours. New materialised tables get their OWN
  version key and build lazily per data generation, like the placement file.
- Measure on the named store and the 99M store, A/B alternating on one
  machine, both numbers in the commit message. Timings taken while anything
  else runs on the box are not numbers (round-7 lesson: a 7.9 s call read
  82 s under a parallel test run).
- One regression test per fix; ruff stays at zero; the full suite before any
  commit that touches numbers; `tests/slow_tests.txt` regenerated at the end.
- Trials live in the scratchpad with their own config, never pointed at
  `data/`; never run `playwright install`; never weaken TLS; never present a
  browser identity by default.

## Order of work

Eight P0s and 24 P1s is more than one sitting. Take them in this order, one
commit per group, each with its before/after numbers: (1) P0.1-P0.8 — the
numbers that are wrong today, each pinned by a hand-worked test, with the
snapshot diff naming only the endpoints each item names; (2) P1.16-P1.19 —
the install, launcher, updater and cross-site holes a non-technical user
hits first; (3) P1.1-P1.3 — the structural cost and the cache staleness;
(4) P1.4-P1.12 — queue, ingest, store and external-data; (5) P1.13-P1.15
and P1.20-P1.24 — the remaining number labels and the dashboard; then P2
and P3 as time allows. Anything you leave, leave in the status block with
the reason.

## Measured baseline at HEAD (quiet machine)

Named store, first / second call: everything under 2 s except
`export.csv` all-latest 3.45 s (342 MB), `rates?grain=entity` 4.45 s,
`export.csv` entity 3.2 s; packets (5 clients) 2.5 s. 99M store, first
(second): `changes` latest all-market **23.9 s (17.6)**; `quality/size`
**12.3 (0.0)**; clients digest **10.7 (0.04)**; packets (5) **22.9 s**;
outreach ZIP one state 8.4; outreach CSV one state 7.9; new-to-network 5.5
(5.2); dossier 5.5 (3.9); leads all-market 5.1 (0.01); leaderboard 4.9 (0.0);
**benchmark/opportunity 3.9 (3.8)**; **contract-gaps 3.5 (3.5)**;
**schedule/fee 2.8 (2.4)**; report/market 2.3 (0.2); validate 1.7; Code page
with a state 0.72. Whole-store CSV exports on the 99M store were NOT timed:
they need ~20 GB of temp disk (see P1.1). Determinism: 125/125 named-store
endpoints byte-identical across two full runs. Peak RSS of the dashboard
process after the heavy first calls: **6.3 GB on the 99M store** (the
DuckDB pool at its auto cap on this 15 GB box), 0.5 GB on the named store.
Quick suite 515 tests in 3 min 48 s; full suite 929 in ~14 min. pip-audit:
setuptools 80.9.0 carries PYSEC-2026-3447 / GHSA-xmj3-9gf7-h52w (fixed in
84.1.0).

Profiles (99M, per SQL statement): benchmark 4.02 s of 4.77 is ONE
statement — `base`: median per (code, practice) over the latest spine;
contract-gaps 3.27 of 3.57 is the same statement; quality/size 8.43 of 12.2
is its per-payer twin; the digest's 4.70 is the same again, plus
`payer_months` (the `pm` CTE) run once PER CLIENT (5 × 0.3–0.5 s); the fee
schedule's 2.20 is the per-(payer, code) peer median; new-to-network 4.44;
`changes` 16.5 s is one whole-store two-month self-join. Every one of these
recomputes the same per-practice medians from 29.6M spine rows.

Measured for the fix (P1.2): the house-basis per-(payer, code, practice)
medians on the 99M store are **14,979,620 rows, built once in 6.7 s**; from
that table per-payer market statistics for every (payer, code) take 0.47 s;
named store 708,259 rows in 0.3 s.

Ingest at HEAD (in-process, `parallel_ingests: 1`, machine partly loaded by
parallel test runs so treat it as an upper bound): a real UHC River Valley
file (262 MB gz) and a real BCBS Wyoming shard (278 MB gz) — 2,008,635 +
5,352,298 rows, both `done`, 71.5 s for the two including the rollup
rebuild, peak RSS of the process 2.7 GB (that is the DuckDB rollup pool in
the same process, not the parser).

## Priority 0 — wrong, lost or silently missing

**P0.1 A payer's string rate `"nan"`/`"Infinity"`/`"1e999"` lands as NaN
and poisons the practice's median.** `parser.py:126-141` (`clean_rate`:
`float(v)` with no finiteness check; both parsers share it), `store.py:535`
(the rollup's `FILTER (… negotiated_rate > 0.01)` keeps NaN because DuckDB
evaluates `NaN > 0.01` as TRUE — verified: `SELECT 'nan'::DOUBLE > 0.01` →
true, `median(DISTINCT [50, NaN])` → NaN). Reproduced through the real ingest
(`scratchpad/audit_b/probe1.py`): a TIN that also published $50 shows
`rates_by_tin.negotiated_rate = nan, is_latest = TRUE`; with `"Infinity"` the
median survives but `rate_max = inf`. The existing pin
(`test_fastscan_e2e.py::test_nan_rate_fails_the_file_on_both_paths`) covers
only the bare `NaN` token. Fix: `if not math.isfinite(f): return None` in
`clean_rate` (counted under `unparseable_rates`), plus `isfinite(…)` in the
BY_TIN filter as belt and braces; no row-rules bump (the rows simply stop
existing). Test: both parsers, three spellings.

**P0.2 The payer-negotiation one-pager's total multiplies the practice's
WHOLE annual volume by every payer's gap and adds them up.**
`benchmark.py:791-810` (per payer: `compute_opportunity(bench, volumes, …)`
with the same practice-wide `volumes`, `total_target += …`), `:1536-1541`
("Across N payer(s), lifting every contract to pX is worth $T/yr"); the
volumes box says "Annual units per code (owner-supplied)" and "Fill from
Medicare" fills practice-wide units (`index.html:446-450`,
`utilization.py:346-377`). Hand-worked (`scratchpad/audit_h/test_p10_*`): a
practice bills 1,000 units of 97110 a year; payers P and Q each pay it $40
against a $50 peer median → the one-pager says **$20,000/yr**; the pitch
report from the same box says $10,000; no payer mix can make it more than
$10,000. Fix: value each payer only with that payer's units (a per-payer
units box, or a payer-share split), otherwise print the per-payer rows with
NO cross-payer total and the sentence "each payer valued as if every unit
were billed to it". The single-payer Negotiate/proposal "at your volumes"
carries the same caveat — print it.

**P0.3 Trend sparklines ("Where each rate has been going") move when no
rate moved — a payer entering the month is read as a raise.**
`spark.py:103-148` (`series_by_code`: per code × month, median across the
practice's TINs AND across every payer in scope); `SPARK_NOTE` (:70-80)
promises "a month a payer did not publish is a gap, not a flat segment".
Printed on the rate card (`schedule.py:448-466`), the pitch report
(`benchmark.py:1763-1785`) and the packet's This-month page
(`packets.py:396-416`). Hand-worked (`test_p4_*`): payer A pays $40 in May
and June, payer B $50 in June only → the series reads $40 → $45 "+12.5 %"
with a green line and the packet prints "First → latest $40.00 → $45.00".
Fix: one series per (payer, code) — the rate card already has payer columns
— or restrict the series to payers present in every plotted month and say
so.

**P0.4 Leads, roster and leaderboard positions are `percent_rank` with the
practice included: an identical fee schedule makes EVERY practice an
"underpaid p0 lead".** `leads.py:88-98` (and `:242`, `:346`); `LEADS_RULE`
and the CSV header say only "median percentile at or below the threshold".
Hand-worked (`test_p1a_*`): five practices on one payer's flat schedule
($40/$35/$50 on three codes) → all five listed at p0 with "$0.00 below
median"; the Benchmark for any of them says p50 on every code. Payers DO
publish flat schedules (BCBS Alabama's 6,190 practices on one 97110 rate,
round 3), so this is the common case, not a corner. Fix: use the mid-rank
convention everywhere (strictly below + half the ties; a tie with everyone
is p50; the cheapest of two distinct practices is still p25, so thin
markets still yield leads — the stated reason for `percent_rank`), exclude
all-tied codes from the sweep, and write the convention into `LEADS_RULE`,
the roster note and the CSV header. The round-5 hand answers
(`test_known_answer_leads.py`) must be re-derived under the new rule, by
hand, in the docstring.

**P0.5 A user-entered inflation year is merged into the shipped CPI-U
series and the whole basis is relabelled.** `inflation.py:82-115`
(`load_index`: `values.update(user)`, `name = str(nm)` for every row),
`:146-193` (`deflator` divides `vals[y1]/vals[y0]` without checking both
came from the same index), via `mrfx inflation --index-name`
(`cli.py:1366-1369`) and `/api/inflation` (`api.py:3231-3249`). Reproduced
(`scratchpad/audit_e/scripts/exp_a.py`): `--year 2025 --value 160
--index-name MEI` (the docstring invites "or the Medicare Economic Index")
then a payer flat 2023-06 → 2025-06: factor 160/304.702, erosion line
"+0.0 % in dollars … **+90.4 % in real terms (MEI)** — this payer kept ahead
of costs" (CPI-U truth ≈ −5.6 %). Fix: `deflator` requires both endpoint
years to carry the same `index_name`/base and refuses otherwise ("2023 is
CPI-U, 2025 is MEI — load a full MEI series or CPI-U 2025"); `load_index`
never renames the basis because one row has another name.

**P0.6 Referral leaders are grouped by NPPES name alone.**
`medicare.py:772-786` (`coalesce(nullif(trim(n.org_name), ''), p.target_npi)
AS practice … GROUP BY 1`, with `mode(n.state)` and `mode(zip)` taken
independently). Reproduced: two NPI-1 therapists both named "JOHN SMITH"
(MO 63101 and KS 67202) with 100 and 50 shared patients → ONE row "JOHN
SMITH, KS, 63101, 150 patients, 2 NPIs", which also passes a 25-mile radius
around 63101 with the Kansas patients inside. The entity grain keys on name
+ primary_state (`store.py:669-676`) and never merges individuals. Fix:
group NPI-2 organisations by (name, state) and NPI-1 individuals by NPI;
take city/state/ZIP as one `arg_max(struct, count)` so a row is
self-consistent.

**P0.7 The Census ACS import over-counts seniors about 3× for the tables
data.census.gov shows first.** `demographics.py:57-74` (`_is_senior_col`
matches ANY label with 65–100 plus "year"/"over": the "65 years and over"
and "75 years and over" summary columns, Percent columns, Male/Female
sub-totals), `:154-177` sums every match. Reproduced: an S0101 export whose
true 65+ is 1,000 imports as **3,460**; DP05 as 3,010; B01001 (the table
`GETTING_STARTED.md:447-452` recommends) as 1,000. "Seniors per practice" in
market sizing and the market report is then ~3× wrong with a successful
import. Fix: when an explicit "65 years and over" ESTIMATE total exists
(S0101_C01_030E / DP05_0024E) use that one column; otherwise sum only
mutually exclusive 5-year buckets; exclude "percent", "!!male"/"!!female"
totals and "75 years and over"; or refuse tables other than
B01001/B01003/B19013 naming the supported ones.



**P0.8 The Benchmark tab's "Annual units per code" box drops thousands
separators and space-separated lines, so the opportunity is a fraction of
the truth.** `app.js:1614-1619` splits each line on a comma or tab only; the
Negotiate box (1924-1927) and the engagement box (`parseVolumes`, 2454)
accept comma/semicolon/tab/space and strip the thousands comma. Reproduced
in Chromium: typing `97110, 4,200` / `97140 1800` / `97112,500` and clicking
Run benchmark sent `{"97110": 4, "97112": 500}` — 4,200 became **4 units**
and the space-separated line vanished with no warning; the screen said
"Annual gross opportunity: conservative $56 · at target $72" for a line
worth about $75,600, and the same text in the Negotiate box gave
$84,000/yr. Fix: ONE shared `parseVolumes()` (the 2454 version) for all
three boxes, and show skipped/unparsed lines next to the button. Test: the
three lines above through all three boxes.

## Priority 1 — the user is misled, blocked or made to wait

**P1.1 Whole-store exports are built twice on the SYSTEM drive and die
with a 500 when it fills.** `api.py:1851-1876` (`_mktemp` =
`tempfile.mkstemp`, i.e. `%LOCALAPPDATA%\Temp` on Windows — the C: drive,
whatever drive the user put the store on): `COPY` writes the CSV there, then
a SECOND complete copy is written just to prepend the Excel BOM, and the
response streams from that. Reproduced on the 99M store: `_duckdb.IOException:
Could not write file "/tmp/tmp_tmp4g_ca7_g.csv": No space left on device` —
an unhandled 500 (`scratchpad/perf/r7_measure.log:125`); the all-latest
export of that store is ~20 GB, so ~40 GB of C: per click. Same `_mktemp` in
`export.zip` (1904, 1929), the outreach CSV/ZIP (2873, 2886). The store
already owns a temp directory on the data drive with a free-space check
(`store.py:894-908`, `1719`). Fix: build under `store._tmp_dir`; stream
`BOM + file` with a `StreamingResponse` instead of copying; before the COPY,
estimate the size (row count × the named store's measured 243 B/row) and
refuse with a plain sentence when free space is short; the `export.zip`
methodology sidecar stays. Pin with a test that fakes `shutil.disk_usage`.

**P1.2 Every benchmark-family call recomputes the same per-practice
medians from the whole spine.** The profile above: benchmark, negotiation
(once PER payer), contract gaps, quality/size, the digest, the fee schedule,
leads and the leaderboard each run "median per (code, practice[, payer]) over
29.6M latest rows" — 2–8 s each at 99M, and the dossier, every packet and
every negotiation report call several of them. Build, lazily per data
generation and stamped like the placement file (`Store.placement_relation`,
own version key, written outside the DB with `COPY` + `os.replace`), TWO
exact tables under the HOUSE basis (latest, dollar, professional, base
modifiers): (a) per (billing_code, tin_value): median over ALL that
practice's lines — exactly `compute_benchmark`'s `base` for an all-payer
market; (b) per (payer, billing_code, tin_value) — exactly the per-payer
`base` (negotiation, fee schedule, quality/size). A median of per-payer
medians is NOT the all-payer median, so (a) is not derivable from (b) —
build both. State/therapy/discipline filters are practice attributes applied
after (join `tin_directory`), so they read the tables; a pinned month, a
payer SUBSET, `include_assistant`, `include_non_dollar`, `base_only=false`
or `billing_class=''` fall back to the live query. Prove identical numbers:
the snapshot diff must be 125/125, plus a test that runs every reader both
ways on the known-answer fixtures. Report before/after on both stores for:
benchmark, negotiation report, gaps, quality/size, digest, packets, fee,
leads, leaderboard, dossier.

**P1.3 A saved website never reaches the cached Leads list.** Round 6 cached
`compute_leads`, `compute_leaderboard` and `compute_payer_roster` per data
generation; all three read `store.org_websites()` (`leads.py:166, 264,
363`), and `set_org_website` (`store.py:4426`) does not bump the generation.
Reproduced (`scratchpad/sources7/probe_website_cache.py` on the known-answer
Leads store): save `https://a-rehab.example` for A REHAB → `/api/leads` still
shows `website: null` until the next rollup. Websites are the one confirmed
case (no cached reader uses `entity_map`, peer sets travel in the market key,
the digest keys on the watchlist and Medicare imports); still, audit every
cached reader (`grep -n "generation_cached\|store.cached" mrfx/`) against
every user-edit writer and add ONE `Store.note_user_edit()` that bumps the
generation, called from `set_org_website` and any other writer a cached
reader depends on. Test: save a website, read leads, see it.

**P1.4 Segmented downloads can never finish a file that needs longer than
the wall-clock cap — and the bytes are thrown away each turn.**
`fetch.py:444-513`: `_halt()` returns "cap" after `download_max_seconds`,
`_SegmentationFailed` deletes the holey `.part`, the sequential fallback
(690) then sees an entry clock three hours old (639) and parks with
"nothing received yet" (702-703) — `progressed=False`, so the park streak
climbs; woken, `not part.exists()` → segmented again from byte 0. Reproduced
with a local server (`scratchpad/audit_d/probes.py p1`): 393 KB reported to
the dashboard, `.part` gone, `DownloadError(parked=True, "nothing received
yet")`. Six parks ≈ 18 h of downloading, then five 24 h pauses, then
failed — exactly the per-connection-throttled case `download_segments` is
recommended for. Fix: exempt the segmented path from the wall-clock cap (its
per-segment stall/read timeout already bounds it) or persist a segment map
so a holey `.part` resumes; after real progress never fall into park/stall
with the stale entry clock. "Stop downloads" on a segmented file also
discards every byte while the confirm text promises "partial files are kept"
(`app.js:4218`, `store.py:2742`) — say so or keep them.

**P1.5 A downloader or parser thread that dies leaves its claimed row
in flight until the next restart; a CLI drain never exits.**
`fetch.py:2546-2558` (`respawn_dead_threads` starts a replacement and logs
"re-queued at the next start"), `store.py:2532` (`recover_stuck_urls` runs
only at `run_queue` start). Reproduced (`probes.py p7`): row stays
`downloading` with no owner; in `mrfx add` the coordinator counts it as in
flight for ever. Fix: remember thread → claimed id after each claim and flip
it back to `queued`/`fetched` before starting the replacement.

**P1.6 An index cut at `max_toc_files` says `done` with no error.**
`fetch.py:2003-2021` builds the "(capped at max_toc_files=…)" note, logs it,
and writes `error=None`; `extract_links_from_text` (1397 `out[:max_files]`)
and `crawl_gatsby_hub` (1497) have no truncation signal at all. With the
default 2,000 and listings of 4,173 (Meritain), 32,397 (Allied) and 30,884
(Aetna ALICUNDER100) files, the user sees "found 2,000 files inside" and
nothing about the rest. Reproduced (`probes.py p5`). Fix: store the cap note
in `error` (amber, like oversize), return a `truncated` flag from every link
extractor, and make `child_count` the number LISTED, not newly queued
(after a monthly re-fetch it reads "found 0 files inside").

**P1.7 A rate group whose references are only PARTLY resolvable drops the
missing practices in silence, and the file is never re-ingested when the
companion lands.** `parser.py:786-801` (`missing_ref` set, but
`ref_groups_skipped` increments only when NO group resolved). Reproduced:
`provider_references: [7, 8]` with only 7 known → 2 rows, `ref_groups_skipped
= 0`, nothing on the Files tab, and `requeue_skipped` (`ingest.py:1229-1240`,
predicate `ref_groups_skipped > 0`) never re-runs the file. Fix: count
unresolved reference ids per group (`qa.missing_ref_ids`), show it, and
include it in `requeue_skipped`'s predicate.

**P1.8 A failure between "part swapped in" and "files row = done" leaves a
`failed` row whose rates are live in every number.** In-process path
`ingest.py:1155-1181` (the part goes live in `RatesPartWriter.__exit__`,
`store.py:4732-4756`; the done-upsert is a later step; the handler at
`ingest.py:1215-1219` marks `failed`, queues the orphan part's payers — so
the rollup aggregates it — and moves the source to `failed/`); pooled twin
`ingest.py:849-862`. Reproduced (done-upsert raising once): `files` =
`failed`, `SELECT count(*) FROM rates` = 1, `is_latest = TRUE` after the
rebuild, source in `failed/`. A crash at the same instant heals on the next
scan; the exception path is persistent. Fix: track "part swapped in this
attempt" and in the handler call `store.drop_rates_part(name)` (or retry
the upsert, then drop) so a non-`done` row never owns a live part; mirror
in `recover_stuck_files`.

**P1.9 `reset` and `forget` leave parked part replacements behind, and
the next open adopts them into the store with no `files` row.**
`store.py:4555-4570` (reset unlinks `rates/*.parquet` and the placement
files, never `.{key}.parked` nor meta `parked_parts` / `doomed_parts` /
`rollup_pending_payers` / `rollup_needs_full`), `:3043-3110` (`forget_file`
likewise; `drop_rates_part` :2952 the same), `:2976-3023`
(`_retry_pending_part_ops`, run at every open and before every rollup,
moves the park back in). Reproduced (`scratchpad/audit_f/probe_b.py` G, H):
after `reset()` → `rates=0, files=0`; after `Store(…)` → `rates=1,
files=0`; after `forget` + reopen → the "removed" row is back and cannot be
forgotten again (forget reads `files`). The park itself happens only on a
Windows `PermissionError`, i.e. on the user's machine. Fix: reset/forget/
drop unlink `.{key}.parked` and clear the meta keys; `_retry_pending_part_ops`
adopts a park only while a `files` row for that filename exists; `reset`
checks `unlink_with_retry`'s return value (`:264-271` says callers must).

**P1.10 NPPES weekly updates never reach the practice directory.**
`enrich.py:393-503` (`apply_weekly_update` merges only the cache parquet),
`:900-911` (`_refresh_changed_from_parquet` runs only when the `.sig` stamp
changed; a weekly never rewrites it), `cli.py:917-931`. Reproduced: the
monthly names NPI 1417594896 "OLD CLINIC NAME" 63101; the weekly renames it
"NEW CLINIC NAME" 63099; `apply_weekly_update` reports `updated_npis=1`; the
next `run_enrichment` leaves `npi_directory` = OLD/63101; the placement
stamp's three inputs are byte-identical, so the practice stays at the old
ZIP, while the new-clinics feed (which reads the cache) shows NEW/63099 —
two tabs disagree. Fix: after a successful merge call
`_refresh_changed_from_parquet(store, cache)` (0.7 s at 1M rows) and
`_mark_dirty()`; fold a merge counter into the `.sig` stamp.

**P1.11 Hospital standard-charge files: the CMS wide CSV imports one
payer's column as "Unknown payer", a tall file with the CPT in `code|2`
imports nothing, and hospital modifiers bypass the one modifier rule.**
`hospital.py:123-136` (first header containing "negotiated"/"dollar" wins;
only `code|1` is read; wide files have no payer column → `"Unknown
payer"` :345-347), `:288-290` (modifiers stored as `"|".join(...)` raw, no
sort/dedupe/POS move), `:518-548` (parity applies the house basis to the
practice side and `h.rate IS NOT NULL` to the hospital side). Reproduced:
`standard_charge|Aetna|PPO|negotiated_dollar = 88.50` and
`…|Anthem BCBS|HMO|negotiated_dollar = 132.75` → "rows 2, payers 1",
stored `('Unknown payer', 97110, 88.5)`; a tall file with `code|1` = CDM and
`code|2` = 97110 → 0 rows. Fix: detect `standard_charge|<payer>|<plan>|…`
headers and unpivot; scan `code|1..N` for the slot whose `code|N|type` is
CPT/HCPCS; run `modifiers.normalize` at import and `base_sql` on the
hospital side of the parity (or print "all hospital modifiers pooled").

**P1.12 The official MPFS import may refuse the real CMS files.**
`mpfs.py:62-84` / `:113-129` start `csv.DictReader` at line 1; CMS's
PPRRVU and GPCI CSVs have carried title rows above the header in past
years (`tests/mrfx/test_mpfs.py:13` starts at the header, so the suite
cannot see it). Verify against the 2026 files first (download them in the
scratchpad, not from memory); if they carry titles, scan forward to the
first row whose cells include "hcpcs" / "gpci". Nit: the PPRRVU file does
carry a CONV FACTOR column (`mpfs.py:14-15` and `AI_HANDOFF.md:51` say it
does not) — cross-check the typed CF against it.

**P1.13 Two rounding rules print two numbers for one fact.** Python
`round()` is half-to-even (`benchmark.py:631,633,815,997`,
`schedule.py:182,194`, `market.py:192`, `api.py:1285,1523`,
`economics.py:243-244`); DuckDB `round()` is half-up (`benchmark.py:585-588`,
`leads.py:98`). Hand-worked (`test_p12_*`, `test_p6_*`): $45.00 against an
MPFS $40.00 prints **112 %** (paper and SQL: 113 %); positions 0 and 25 →
headline `round(12.5)` = **p12** while the per-code SQL position prints
p13. A payer analyst re-doing the arithmetic gets the other number. One
rule: `Decimal` ROUND_HALF_UP in Python, or round in SQL everywhere.

**P1.14 The pitch report's "In real terms" block is the MARKET's median
trajectory, printed unlabelled inside the client's own report.**
`benchmark.py:1674-1715` (`_erosion_block` → `compute_payer_trajectory`
over the market minus the month; `monitor.py:332-343` medians over ALL
practices per month). Hand-worked (`test_p5_*`): the client's own 97110 went
$40 → $50 (+25 %) while three peers went $60 → $50 → the client's pitch
report says "Zed Health: −16.7 % in dollars … the cut is deeper than it
looks". Fix: compute erosion on the subject's own per-payer series, or head
the block "Market median (all practices in scope), not this practice".

**P1.15 Overview "Which payers pay above vs below market" divides by the
median of PAYER medians, not the market median the same page prints.**
`overview.py:81-101` (`mkt AS (SELECT billing_code, median(payer_med) …)`),
label `app.js:285` "this payer's median rate ÷ the market median". Hand-
worked (`test_p2_*`): payer A prices 10 practices at $50, payer B one
practice at $100 → the page's market median is $50, yet A reads **0.667
("~33 % below market")**. Divide by the per-code practice-grain median (and
gate on a minimum practice count — B has one).

**P1.16 Any web page the user visits can POST into the local app: drop
a file into the ingest pipeline, replace the Medicare anchor, queue the
whole catalog, wipe the queue.** `api.py:941-3522` (`create_app` adds no
Host/Origin check); reachable sinks: `/api/upload` (1820), `/api/mpfs/upload`
(3332), floors/hospital imports (2220, 2548) — all multipart — and the
body-less POSTs `/api/urls/known` (1753, no body = every catalog state),
`/api/urls/clear-queued`, `stop-downloads`, `retry-failed`,
`resume-stopped`, `/api/files/scan`, `/api/packets/build`, the per-row
retry/cancel/stop/force-size. A cross-site `<form enctype=multipart>` or
`fetch(…, {mode: "no-cors"})` is a CORS "simple request": the browser sends
it without preflight and the server executes it (only the response is
hidden). Reproduced (`scratchpad/audit_a/probe_a.py`): `POST /api/upload`
with `Origin: https://evil.example` → 200, the file landed in the inbox,
was scanned and reached `done`; `POST /api/mpfs/upload` → the Medicare
anchor replaced; `POST /api/urls/known` with no body → `{"added": 109}`;
`GET /api/stats` with `Host: attacker.example.com` → 200 (DNS rebinding
reads everything and is then same-origin). Because a re-ingest atomically
replaces a file's part BY FILENAME, an attacker-named upload overwrites a
real payer's rates with fabricated ones — silently, in the numbers that
are sold. Honest risk: the user must visit a hostile page while `mrfx
serve` runs (it always runs); current Chromium prompts before a public page
may reach localhost, Firefox does not; JSON endpoints are NOT reachable
cross-site (FastAPI 0.141 rejects a body without `application/json`,
verified). The rendered-portal path (`render.py:256-300`) fulfils every
request a portal's JS makes, Host stripped — the same surface while
`render_js` is on. Fix: one middleware that rejects any request whose
`Host` is not `localhost`/`127.0.0.1[:port]` and any POST/PUT/DELETE whose
`Origin` is present and not `http://localhost:<port>` /
`http://127.0.0.1:<port>` (or `Sec-Fetch-Site: cross-site`); optionally
require a custom header on mutations so the browser preflights. Tests: the
three reproductions above must return 403.

**P1.17 HOW_TO_RUN Step 1 installs a Python the pins cannot run on.**
`HOW_TO_RUN.md:21-22` ("click the big yellow Download Python button — any
version 3.11, 3.12, 3.13 or 3.14 works") and `GETTING_STARTED.md:26-27`
("install the current release"); python.org's default download on
2026-11-07 is **3.15.2**, and a pip dry-run for `win_amd64/cp315` finds no
wheel for ijson 3.5.1, duckdb 1.5.5, pyarrow 25.0.1, numpy 2.3.5, PyYAML
6.0.3 or watchfiles 1.2.0 (`requirements.txt:4-7, 11, 24`) — Step 4 then
ends in the exact "Failed to build installable wheels" error the docs
troubleshoot afterwards. Fix: point Step 1 at a specific 3.13.x installer
and make `py -3.13 -m venv .venv` the primary command (or bump the six pins
to cp315 wheels and re-verify 3.11).

**P1.18 The auto-restart launcher loops for ever when the app is already
running, or the config has a typo.** `cli.py:209-213` returns 1 on
port-in-use; `_supervise_serve` (1771-1787) restarts on anything but 0 —
its own comment ("the port was already in use — do NOT relaunch") is
wrong; a config error (2010-2012) also exits 1. Reproduced
(`scratchpad/audit_g/sup/port_loop.log`): double-click Start while a
dashboard is up → "port is already in use … Restarting in 5 s — your data
is safe …", 10 s, 20 s, 40 s, 60 s … until the window is closed. Fix: a
distinct do-not-restart exit code (3) from the port guard and the
config/OSError branches, honoured by the supervisor with a plain message;
and tee the child's output to `<store>/diagnostics/serve.log` (today
closing the window loses the last error, though `GETTING_STARTED.md:1086`
says to read it).

**P1.19 The updater writes the new program files BEFORE re-syncing
packages; if pip fails, the message says the app still runs — it may
not.** `updater.py:117-126` (write loop) precedes `PIP_STEPS` (134-145); on
failure it prints "The update itself is in place, and 'Start MRF Explorer'
runs this folder's program regardless". A new build with a bumped
requirement + an offline/proxy pip failure → the new code imports against
old packages → `ModuleNotFoundError` at the next start → P1.18's restart
loop. Also: a downgrade is applied silently (59-62, 80; trial: 0.1.0 over
0.6.0 → "updated 4 file(s) (version 0.6.0 -> 0.1.0)") though
`AI_HANDOFF.md:246` says never run an older copy against an upgraded
store; and a mid-update `OSError` (a Defender/indexer lock at
`tmp.replace`) escapes `apply_update` as a raw traceback with a
half-replaced tree (writes go alphabetically: `config/`, `docs/` done,
`mrfx/` not; `cmd_update` catches only `UpdateError`, `cli.py:1393-1397`).
Fix: pip from the zip's requirements first, then write; refuse `new < old`
without `--allow-downgrade`; catch `OSError`, restore from the backup zip
it already made (104-112) and say so.

**P1.20 After an ingest the pickers go stale and the Explorer/Code pages
never refetch.** `initFilters` fills payer chips and months once at boot
(`app.js:931-935, 960-962`), `initMarkets`/`initRoster` fill their
datalists once (384-392, 634-640), re-entry refreshers cover only subjects
and months (1454, 2032, 3643, 3792 — Negotiate alone refreshes payers),
`switchView` does nothing for explorer/cpt (131-167), `loadStats` has no
data-generation signal. Reproduced: uploaded a new payer's file on the
Files tab and waited for `done`; the header said "3 payers · 4 files" but
every payer chip list, `#f-month`, the roster and market datalists lacked
the new payer; re-entering Explorer made ZERO requests and the strip said
"ROWS 51" while `/api/summary` returned 73; the Code page the same. Fix:
return `data_generation` from `/api/stats`; when it changes, refresh every
payer/month picker and reload the open Explorer/Code view (reuse
`onNamesUpdated`). Also `cptCtl` (1303) is never reset after completion, so
the Code page's name-refresh gate at 225 is permanently false.

**P1.21 "Export CSV" / "Export + methodology" navigate the dashboard away
to raw JSON on a server refusal.** `app.js:1013-1018` set `location.href`;
`_require_narrowing` applies to exports (`api.py:1864-1866`: 422 on the
Entity/NPI grain without payer+code once the store has 40M rates or more)
and OOM becomes a JSON 503 (3461+). On the user's real stores "Grain:
Entity → Export CSV" replaces the whole app with `{"detail": …}` and the
user presses Back. Fix: fetch → `r.ok` → blob download (as
`downloadTrackerBundle` does, 2240-2257), else show the `detail`. Same
class: `postDownload` (3632) and `downloadRatecardCsv` (2272) `alert()` the
raw JSON body.

**P1.22 The summary by-code table says "Practices" for NPI counts on the
NPI grain** (`app.js:895`; the server counts `DISTINCT unit_id` per grain,
`api.py:1258`): on grain NPI the row reads "97110 … PRACTICES 17" for a
store of 7 practices while the strip above correctly says "NPIS 17" (836).
Header by grain, as the strip does.

**P1.23 "Open market report" with an invalid state throws an unhandled
rejection and shows nothing** (`app.js:2893-2901` calls `postDownloadHtml`,
which throws on a bad status, with no await/catch; the dossier's second
call at 2890 is not awaited inside its try). Reproduced: state `ZZ` →
`[pageerror] 'ZZ' is not a US state code`, `#mr-msg` empty. Await both
inside try/catch and write `e.message` to the message element; route both
through `openReportWith` so the popup is opened synchronously (today
`window.open` runs after the awaits, 2905-2916, and Chrome's activation
window expires on a slow store — the hazard the code documents at
1787-1791).

**P1.24 The validation cross-check prints non-dollar rows with a "$" and
a bogus delta** (`app.js:4348-4353` renders the rate through `fmtMoney` and
omits `negotiated_type`, which `api.py:1592-1595` returns for every row). A
percentage/per-diem row for the pasted TIN+code reads "$150.00". Add the
Type column; `money()` only for dollar rows.


## Priority 2 — cost and robustness

- **`changes` with no scope is 17–24 s at 99M and never cached**
  (`monitor.compute_rate_changes` is not `generation_cached`; the second
  call is as slow as the first). Cache it per generation (the market dict is
  the key), and run `payer_months` once per digest, not once per client
  (`clients.py:~100` loop; 5 × 0.3–0.5 s at 99M), cached per generation too
  — as is `SELECT payer, max(file_month) … GROUP BY payer` inside every
  `compute_benchmark` (0.44 s at 99M).
- **A .zip with several JSON files reads only the largest; the others are
  dropped in silence.** `sniff.py:~160-180` picks `max(file_size)` and logs
  one INFO line — which repeats eight times per file because the archive is
  opened eight times per ingest. Reproduced through a real `mrfx serve`:
  `d_zip_two_json.zip` (manifest.json + rates_big.json) → 40 rows from
  rates_big.json, nothing anywhere about manifest.json; `GETTING_STARTED.md`
  Step 6 says only "drop the .json.gz / .zip". Fix: ingest every JSON member
  as its own part (same source file, member name recorded), or at least put
  "zip held N JSON files; read X, ignored Y" on the Files-tab row and in
  Step 6; open the archive once per pass.
- **Peak RSS 6.3 GB at 99M is the DuckDB pool at its cap, not a leak** — but
  the cap is chosen without the parser workers (see P1.4), and nothing in the
  dashboard tells the user how much of the pool a query used. Log the pool's
  high-water mark with each slow (> 5 s) request.
- **Quick suite is 3 min 48 s, the docs say "~3 min"** (`CLAUDE.md`,
  `GETTING_STARTED.md`): update the number or move the five slowest "quick"
  tests to `slow_tests.txt`.

- **Resume guard misses a 206 from a server that ignores `If-Range` after a
  republish** (`fetch.py:776-779, 871`): the `.part` is spliced with the new
  version. Reproduced (`probes.py p3`): v1[:1 MB] + v2[1 MB:] returned with no
  error; downstream the gzip CRC fails and the file lands in `failed/` as
  "corrupt payer file", then a retry re-downloads from zero — a lost
  download and a misleading message, not a wrong number. Fix: on a 206
  compare the response's ETag/Last-Modified (and the `Content-Range` total,
  stored in `.val`) with what was saved; different → restart. Azure Blob,
  which several catalog payers use, does not honour `If-Range`.
- **416 on an already-complete `.part` deletes it and re-downloads the
  whole file** (`fetch.py:734-747`; a kill between the last write and
  `part.replace` opens the window; `probes.py p2`). Parse `Content-Range:
  bytes */N`; if the `.part` is N bytes, hash it and finish.
- **The "drop the validator, send a plain Range next time" branch is dead**
  (`fetch.py:817` unlinks `.val`, 856-865 re-saves the same validator;
  `probes.py p4`): a server answering `If-Range` with 200 restarts a small
  file from zero on every drop; a genuine republish of a ≥ 64 MB file is
  refused three times per turn as "server ignored resume" and never
  restarts when `download_retries` ≤ 2. Fix: compare the 200's validator with
  the stored one — equal → stop sending `If-Range` this call; different →
  restart at once.
- **`process_url_record`'s inline download path (`fetch.py:1899-1926`) has
  none of `fetch_url_record`'s park / pause / set-aside / disk-full
  handling** — a silent server there ends `failed` ("the payer's server
  stopped sending data … nothing received yet") with no auto-resume
  (`probes.py p13`). Extract the `DownloadError` policy into one helper used
  by both.
- **`Retry-After` is never honoured** (`fetch.py:41, 751-753, 1027-1035`):
  four requests in 11 s against `Retry-After: 60`, then "download failed
  after 4 attempts: HTTP 429" → the 24 h pause (`probes.py p12`); a 30 s
  outage (connection refused × 5) ends the same way. Parse it (delta or
  HTTP-date), sleep min(it, 15 min) without spending an attempt, and treat
  exhausted 429/5xx like a park (minutes), keeping the 24 h pause for the
  park limit.
- **Windows file locks around the final rename and the post-status
  unlinks**: `part.replace(dest)` (`fetch.py:950, 520`) is a bare
  `Path.replace` although `store.replace_with_retry` (`store.py:245`) exists
  for exactly this; a `PermissionError` (antivirus, indexer, an open export)
  is not a `DownloadError`, so the row ends `failed` "unexpected download
  error" with a complete `.part` left behind (then the 416 item above). The
  TOC path writes `done` and THEN unlinks (2021-2022): a held file flips a
  finished index to `failed`. Use the retrying helpers, or unlink before the
  terminal write.
- **`url_inflight_for_filename` omits `paused`** (`store.py:2501-2510`):
  "remove this file" is allowed while a paused row for it will wake by
  itself, re-download and re-ingest it (`probes.py p8`). Add `paused` (and
  make `forget_file` skip paused rows of that filename).
- **A re-paste with a fresh signature is dropped for `queued`/`paused`
  rows** (`store.py:2181-2203` refreshes `url` only for failed/oversize):
  a signed-CDN backlog outliving its signature 403s hours later even though
  the user already re-pasted (`probes.py p9`). Refresh `url` on non-terminal
  rows too.
- **Leads "Avg $ below median" uses a median that includes the practice;
  the Benchmark's gap excludes it** (`leads.py:90,101,140-156` vs
  `benchmark.py:556-563`): the known-answer practice shows $20.00 on the
  Leads list and $25.00 on its Benchmark for the same market. Leave-one-out,
  or label "gap to the market median (this practice included)".
- **Two files of one payer and month under different names are pooled,
  not superseded** (`store.py:514-560` groups by month, `source_file` is a
  `min()`): `payer_2026-05-01` at $50 and a corrected `payer_2026-05-15` at
  $60 → one line at $55, `rate_variants 2`, `is_latest`. Content-sha dedup
  covers only byte-identical twins. At least a quality warning "payer ×
  month has N files"; better a newest-file-per-contract supersede rule,
  stated in the methodology.
- **Any `negotiation_arrangement` other than `""`/`"ffs"` is dropped as a
  bundle** (`parser.py:743-748`): `"fee for service"` / `"fee-for-service"`
  sends a whole file to 0 rows under `bundled_items`. Exclude only
  `bundle`/`capitation`, normalise `fee[ -_]for[ -_]service`, count the rest
  as `unknown_arrangement`.
- **`negotiated_type` decides `is_dollar_rate` by exact match**
  (`parser.py:862-864`, `config.py:13` `DOLLAR_TYPES`): `"fee_schedule"`,
  `""` and a missing key all store `is_dollar_rate = FALSE`, i.e. out of
  every median, with only `non_dollar_share` as a hint. Normalise `[-_]+` →
  space; treat blank/missing as its own QA state.
- **An 8-digit EIN written as a JSON number (leading zero lost) is stored
  unpadded** (`parser.py:276-309`, `clean_digits` 146-160): the same
  practice from a payer that publishes strings is `'012345678'` → two
  directory rows, two entity members. Left-pad 8-digit NUMERIC ein/untyped
  values to 9 and count `padded_tins`; never pad strings.
- **The classic / two-pass fallback holds every target group in memory when
  `provider_references` comes after `in_network`** (`parser.py:766-773`
  `_DeferredGroup` per rate group until `_finish`; only `run_big_file_fast`
  spools to disk; the fallback runs in all-codes mode, under 2 GB free, on
  duplicate top-level arrays, `fast_scan: false`, or without numpy
  `parser.py:1096-1101, 1184-1194`). `AI_HANDOFF.md` Step 5 states a bound
  the fallback does not have. Spool deferred groups past a threshold, or
  document the bound.
- **Enrichment re-scans the raw rate store per batch**:
  `unenriched_npis` (`store.py:3977-4015`) is a whole-`rates` DISTINCT per
  200-NPI API batch and per 100k bulk page, although `rate_npi_set` already
  holds that population per part (`_rate_npis_relation` :4191-4252);
  `refresh_directory_incremental` (:3433-3440) joins every raw rate row
  every ≥ 240 s while enrichment runs. Read both from the per-part sets.
- **`medicare_anchor` loads the whole `mpfs` table into Python per call**
  (`store.py:4504-4553`, ~17k codes per locality, called per
  `compute_benchmark` — once per payer in a negotiation — and by the fee
  schedule, visit card and market report). Filter and median in SQL, or
  cache per MPFS load.
- **`Store.cached` deep-copies under the process-wide lock**
  (`store.py:3815-3818`): take the reference under the lock, copy outside.
  **`placement_relation` unlinks the previous stamp while a sibling request
  may still hold its `read_parquet('…')` string** (:4169-4176; leverage,
  radius and dossier reuse it across statements) → a one-off "No files
  found" 500 when a part lands mid-request. Keep the two newest stamps.
- **The bulk NPPES cache takes taxonomy slot 1, the API path takes the
  flagged primary** (`enrich.py:345,664,749,1010` vs `:58`): the displayed
  specialty can differ for one NPI by identification path (`is_therapy` is
  unaffected). Pick the slot whose `Primary Taxonomy Switch_N = 'Y'`.
- **Market sizing places a practice by "ZIP with the most NPIs"
  (`demographics.py:344-358`), placement by "ZIP with the most rate rows"
  (`sqlfn._PICK`)**; the docstring says they cannot disagree — they do
  (2 NPIs/2 rows vs 1 NPI/15 rows). Same per-call whole-`rates` scans in
  `utilization.py:285-287, 559-561` and `nppes.py:240-243, 359-362`. Count
  from `placement_relation()`; use `rate_npi_set` for `in_store`.
- **New-clinic / closure feeds apply the radius after a nationwide
  `LIMIT limit*5`** (`nppes.py:205-215, 323-333`): a 25-mile, 365-day search
  with no state looks only at the 1,000 most recent nationwide events.
  Filter on miles inside the SQL before the LIMIT (the leaders query does).
- **Leverage drops practices with no NPPES ZIP silently and its note says
  the error can only run toward MORE leverage** (`leverage.py:110-132`,
  `LEVERAGE_NOTE` :43-51): three alternatives, one unplaced → "only 2 other
  practices … thin". Carry `n_unplaced` into the headline. **New-to-network
  under a radius drops unplaced newcomers while its note says "kept and
  flagged, never dropped"** (`contracts.py:304-311` vs `:328-334`).
- **Peers are tax IDs while the subject is an entity**
  (`benchmark.py:551-563`; pitch "fewer than 5 peer practices", dossier
  "Peers"): a 3-TIN organisation is three peers. Say "peers are billing tax
  IDs" or aggregate by the entity key. **The footer's "median of its distinct
  published values" is implemented as a median over spine rows** (one per
  POS set / modifier variant): $40, $40, $30 → $40 shown, $35 by the footer's
  words (`benchmark.py:546-549` vs `:1655-1657`; same in `schedule.py`).
  Fix the words.
- **API enrichment can wedge on check-digit-invalid NPIs** (`store.py:
  3977-4005` admits any 10 digits; `enrich.py:39-43` treats an `Errors`
  body as "retry later"; `:293-319` aborts the cycle after 20+ consecutive
  failures): ≥ ~28 such ids at the front of the un-enriched set stall the
  book for ever. Apply the leading-digit + Luhn test (`medicare.is_valid_npi`)
  in `unenriched_npis` and in `_RATE_NPIS_SQL_T`; per-NPI errors must not
  feed the breaker. (Depends on NPPES answering such an id with an Errors
  body — confirm live once, read-only.)
- **The shipped config and its docs disagree in four places.** (a) The
  shipped `config/mrfx.yaml` SETS `duckdb_memory_gb: 8` while
  `GETTING_STARTED.md:1097` says "leave it unset to let the app pick a
  kernel-safe default" — so on every new install the auto rule
  (`store.py:969-980`: 40 % of RAM, ≤ 60 % of what is free, ≤ 12 GB) never
  runs, and the dashboard's pool is 8 GB beside up to 7 parser workers on a
  16 GB laptop; ship it commented out and make the auto rule subtract the
  workers' budget (~1.5 GB × `resolve_worker_count`). (b) `max_toc_files`
  (`GETTING_STARTED.md:211`, `README.md:407`, the new Aetna catalog notes)
  and `delete_raw_after_ingest` (763) are told to the user as keys to set
  "in config/mrfx.yaml" but are not in the shipped file at all, even as
  comments (19 other `MrfxConfig` fields are absent too: `download_retries`,
  `render_js`, `monthly_refresh`, `tracker_dir`, `report_branding`,
  `packets_dir`, …); `duckdb_temp_dir` and `therapy_only_ingest` are there
  as commented examples (206, 240), which is the right shape — do the same
  for every user-facing key, and add a test that each documented field
  appears in the shipped YAML. (c) The YAML comment shows `mode: off` for
  enrichment (217-220) and `GETTING_STARTED.md:758` says "on/off", but YAML
  reads a bare `off` as `false` and `config.py:30`'s `Literal` refuses it —
  "Input should be 'api', 'bulk' or 'off'" tells the user to type what they
  typed; add a before-validator mapping `False → "off"` and quote `'off'` in
  the comment. (d) `GETTING_STARTED.md:717` and `mrfx.yaml:183` say "`0`
  turns parking off" after listing three keys, but `download_park_limit: 0`
  is refused (`config.py:255`, `ge=1`) — say `download_park_seconds: 0`.
- **A non-finite or negative MPFS rate poisons the summary strip and the
  Code page for that state until a new anchor is uploaded** (`api.py:3564`
  `float(rate_raw)` accepts nan/inf/negatives; `_with_anchor` 1274,
  `code_detail` 1529). Reproduced: upload `97110,00,nan,MO` → 200; then
  `/api/summary?state=MO` → 422 "cannot convert float NaN to integer",
  `/api/code/97140?state=MO` → "Out of range float values are not JSON
  compliant: inf". `/api/inflation` accepts `[[2025, Infinity]]` the same way
  (`inflation.py:166-175` checks `v <= 0` only). Refuse `not isfinite(v) or v
  <= 0` with a line-numbered 422; treat a non-finite anchor as "no anchor".
- **Market filter values of the wrong JSON type reach DuckDB and 500 on
  every market endpoint** (`benchmark.py:333-369` binds `billing_class`,
  `discipline`, `city`, `codes` as-is; `normalize_market` 94-135 coerces only
  month/payers/state). 288-request matrix, 73 × 5xx: `{"city": ["StL"]}` →
  `BinderException: No function matches 'upper(VARCHAR[])'`, `{"discipline":
  5}` → `ConversionException`, `{"codes": [{"a": 1}]}`, `{"billing_class":
  ["x"]}` … on /api/benchmark/market, /leads, /quality/types,
  /territory/concentration, /market/geography, /changes, /schedule/fee,
  /contract-gaps, /floors/compare, /hospital/parity, /remits/check,
  /trajectory; `/api/changes` also on `{"payers": [["Aetna"]]}` (monitor.py
  skips the payer coercion). Coerce scalars to `str().strip()`, refuse
  containers with a `BenchmarkError`, route `compute_rate_changes` through
  the normaliser; add the matrix to `test_adversarial.py`.
- **`config/starter_links.txt` is called the paste-ready export and
  "about 100 automatic sources" (`GETTING_STARTED.md:198`,
  `AI_HANDOFF.md:37`) but holds 38 URLs; 80 of the 109 queueable catalog
  entries are absent from it and 9 of its lines are not queueable sources.
  Regenerate it from the catalog or point the doc at `mrfx add --known`.
  It is also called "editable" (`GETTING_STARTED.md:732`) but is not in the
  updater's PROTECTED list, so in-place edits are overwritten on update.
- **`codes.cpt_codes` editing has three traps** (`config.py:24`): an empty
  list loads and `serve` records 0 rows for every file with no warning
  (only `mrfx status` warns); following the YAML's "delete this whole list"
  but leaving the `cpt_codes:` key → "Input should be a valid list"; a
  hand-added unquoted `- 97110` → "Input should be a valid string". A
  before-validator (`None → default`, scalars → `str()`), and a refusal or a
  startup warning on an empty list.
- **Docs steer a Windows 11 user into a OneDrive-synced folder**
  (`HOW_TO_RUN.md:32-33` "Documents", `GETTING_STARTED.md:41-42` "home folder
  or Desktop" — both OneDrive known-folder targets by default), so the
  DuckDB file, parts and multi-GB downloads sync to the cloud —
  contradicting "Nothing is uploaded anywhere" (`HOW_TO_RUN.md:13`); the code
  already names the failure (`nppes_auto.py:112`). Recommend `C:\mrfx\`
  and warn at startup when `store_dir` contains `OneDrive`.
- **`HOW_TO_RUN.md:177-181`'s "paused after 10 minutes" paragraph predates
  parking** — a silent file now shows *parked* after 4 minutes; add the
  paragraph `GETTING_STARTED.md:701-717` already has.
- **`CLAUDE.md:34` says pip-audit is "now clean"** — it is not (setuptools,
  see P3) — fix the sentence with the bump.
- Dashboard (verified live, `scratchpad/ui/audit6/`): the entity drawer
  chart draws only the first two payers under "Median dollar rate by code
  and payer" (`app.js:1067, 1107-1108, 1143` `slice(0, 2)`; a four-payer TIN
  showed two). Benchmark enables "Open pitch report" and the one-pager for a
  subject with no priced rows (1645-1648; the table shows "–" in every "You"
  cell, the click ends in a 422 alert) — gate like the rate card (2166-2169).
  Code comparison and Markets default to the first catalog code
  (90912, no data; 150-153, 396-399) — default to the best-covered code.
  Referral leaders say "No practices found in the referral data — try a
  wider radius" when no dataset is imported (3248-3250; `medicare.py:733`
  returns `dataset: null`). The Files table re-renders every 4 s and
  re-enables a button whose DELETE is still running (3989-4022 vs 4025,
  4035, 4047) → a second click → 404/409. Polling never pauses on a hidden
  tab (no `visibilitychange`; `/api/stats` + `/api/states` every 15 s for
  ever, Files polls every 4 s and calls `loadStats()` each time — 3× stats
  in 13 s). Enter is wired on 6 inputs only (`#mk-code`, `#ro-payer`,
  `#ng-comp`, `#cl-subject`, `#md-subject`, `#rl-zip`); typing a subject in
  `#b-subject` and pressing Enter does nothing. A failed client removal is
  swallowed (2394-2397). No UI to undo an upload although the server offers
  DELETE for peer sets, hospital files, floors and baselines. Accessibility:
  102 of 132 controls have no programmatic label, the drawer is
  `aria-hidden="true"` and never toggled with focus left on BODY, no
  `aria-current`, no `h1`, pointer cursor on 50 non-sortable table headers.

## Priority 3 — honesty, drift and nits

- `mrfx` has no `--version` (only `-v`); `pyproject.toml` says 0.1.0 while
  `mrfx/__init__.py` says 0.6.0, so `pip show mrfx` reports 0.1.0. Make
  `pyproject` read the package version and add `mrfx --version`.
- `requirements.txt:18` pins setuptools 80.9.0 (two advisories); it is a
  build-time dependency only — bump to ≥ 84.1.0 or drop the pin.
- `AETNACVS` and `TEXASFI` Aetna brand listings answered `{}` on
  2026-11-07; re-check them in the monthly `mrfx sources --check` and
  catalogue them when they publish.

- Queue polish (all `fetch.py`/`store.py`, from the round-7 queue read):
  the idle wake returns every parked row at once and the thread that just
  parked a file re-claims it immediately when nothing else is queued — bounded
  (≥ 240 s per cycle, then the 24 h pause) but not what the row text says;
  wake only rows parked ≥ 60 s ago. `park_count` survives `clear_queued →
  resume_canceled` and the kept-download reuse path; `clear_queued` flips an
  in-flight `expanding` row to skipped, which the processor then overwrites
  with `done`. The park clock starts at `download()` entry (639), before
  `_probe_ranges` and the first connect, so with the defaults the old stall
  branch (1041-1052) is unreachable. `mrfx add`'s summary and `mrfx status`
  never name parked rows. Non-gzip `Content-Encoding` (br/deflate) bodies
  are decoded while Range offsets count encoded bytes — disable resume for
  those. `run_queue`'s `finally` uses `pool.shutdown()` (wait=True) on the
  drain path, so Ctrl-C in `mrfx add` waits for the in-flight parse
  (`shutdown_now` exists and `serve` uses it).
- `monitor.py:229` `abs(c.rate - p.rate) >= 0.01` loses a one-cent move
  (35.01 − 35.00 = 0.00999…): compare `round(c.rate - p.rate, 2) <> 0`.
  Negotiability is ranked by absolute p90 − p10 but classified by relative
  spread (`market.py:133` vs `:139-144`). The concentration headline "across
  the N payers you have loaded" counts payers in SCOPE (`territory.py:
  253-255`; `payers_with_files` is in the payload).
- Parser nits (`parser.py`): Excel-artifact identifiers `"123456789.0"` →
  an NPI-typed 10-digit TIN (146-160; `entities._clean` already strips
  `.0`); `clean_rate`'s salvage concatenates non-adjacent digits
  (`"2 x 34.50"` → 234.5) and `True` → 1.0 (130-136); a mixed modifier/POS
  list `["GP","11"]` is neither base nor POS-moved while `["21","31","22",
  "24"]` moves (`modifiers.py:114-118`, documented); `service_code` not
  zero-padded (`["1","01"]` → two POS sets, 856); non-ISO header dates
  (`05/01/2026`, `20260501`) fall silently to the ingest month (238-262);
  `month_in` can match inside fetch's 8-hex-char filename prefix
  (`ab3c2024_05_…` → 2024-05). `_rewrite_part` overwrites
  `qa.modifier_split_rows`/`pos_in_modifier_rows` with only that rewrite's
  rows (`store.py:1497-1508`). An unquoted octal-looking TIN in
  `entity_map.yaml` loads as an int (`entities.py:50-60`) — warn.
- Store nits: `_normalize_part`'s `months` fallback is non-deterministic on
  late-header parts (:1431-1449); incremental markers are two autocommits
  (:3356-3362) where the full build uses one txn; the marker txn and
  `_queue_removed_payers(scope=None)` can collide on `rollup_needs_full`
  (`TransactionException` at COMMIT → a finished rebuild reported failed,
  retried as full; rare); the 512-entry cache cap wipes the whole generation
  instead of evicting; `_enqueue_one` runs 3 statements per URL under
  `write_lock` (~6,000 for a 2,000-child TOC); `_migrate_row_rules` runs
  synchronously in `Store.__init__` for every CLI command; `cmd_backup`
  opens the store (and may migrate) AFTER zipping, so the counts describe a
  different state than the zip; `reset()` keeps `plan_index`,
  `hospital_files`, `inflation_index`, `floor_schedules` and the downloads
  dir — state the rule. `LATEST_PARTITION` (`store.py:590`) omits
  `billing_class`, so an institutional-only newer month would hide a
  contract's professional line; measured on the real store: **0 of 709,688**
  professional contracts affected — decide and write the rule down, no
  change to the spine.
- External-data nits: floors `_pick_columns` prefers "Facility Rate" over
  "Non-Facility Rate" (`floors.py:98-109`); hospital imports are keyed on
  the name slug (`hospital.py:368, 393-396`) — two hospitals of one name
  replace each other; % of Medicare with `include_assistant` compares CQ/CO
  lines to the unreduced MPFS amount without saying so; inflation deflates
  by calendar year of the file month (documented in code, not in the erosion
  line) and the shipped table ends 2024 — ship 2025; `ZIP5`'s `lpad` turns a
  user-mangled `21151234` into 21151; `utilization.medicare_allowed` is an
  unweighted average of per-row averages (316, 335); stale timing text
  `nppes_auto.py:5-6` (164 s) vs `enrich.py:589-594` (61 s arrow / 164 s
  python). MPFS year is never compared with the rates' months (a 2025 anchor
  under 2026 rates prints an unflagged %); renewal / new-to-network "Median"
  is a cross-code median of one contract with no tooltip on the leaderboard
  header; `rate_type_mix.median_rate` is still over raw spine rows;
  `payer_freshness.published` counts a 0-therapy-row file that
  `newest_file_month` does not; `_wins_html` prints "$-500.00" and the
  engagement band does not say "net".
- Security nits (`scratchpad/audit_a/`): container-typed fields on the
  small write endpoints are iterated blindly — `{"tins": "431234567"}` on
  `/api/org-website` writes nine one-character TIN rows, `{"tins": "abc"}`
  on `/api/peersets` saves `["a","b","c"]`, `{"key": 5}` on
  `/api/sources/override` writes an int key into `registry_overrides.yaml`,
  `{"add_tins": [{"a": 1}]}` persists TIN "1" to `entity_map.yaml`, and
  `{"urls": 5}` / `{"states": 5}` / `{"paths": 5}` 500 — one `_str_list`
  helper next to `_comparables_arg` (api.py:836). `GET /api/rates?page=1e20`
  → 500 (`page` has `ge=1`, no `le`). `crawl_gatsby_hub` (`fetch.py:1485,
  1492`) and the chain repair's AIA download (`:225`) read bodies unbounded
  — cap at a few MB. The `view` query parameter flows unsanitised into zip
  entry names and the download filename (`api.py:1908-1909`:
  `?view=..%2F..%2Fevil` → entry `mrfx_../../evil_…`); slug it. The
  updater's `data/` / PROTECTED filter is case-sensitive, so on NTFS a zip
  member `Data/inbox/x.json` or `Config/mrfx.yaml` passes (`updater.py:
  28-30, 84, 94`); compare lower-cased. A subject typed as a bare
  SSN-pattern TIN is echoed raw in the rate-card title (`schedule.py:472`),
  the org bundle's "Subject as entered" (`orgprofile.py:198`), the packet
  folder name (`packets.py:41`) and ~20 JSON `.subject` echoes — mask it for
  display. The floors and hospital uploads (`api.py:2220-2245, 2548-2575`)
  have no size cap (the inbox upload has 1 GB).
- Operator nits (`scratchpad/audit_g/`): no `mrfx --version` and three
  version strings (`pyproject` 0.1.0 = `pip show`, `__init__` 0.6.0 = the
  methodology sidecar, `HOW_TO_RUN.md:350` "from version 0.5 on") — add the
  flag and show it in `/api/stats`. A missing config returns unanchored
  relative defaults (`config.py:321-330`), so `store_dir` is relative to the
  terminal's cwd though `GETTING_STARTED.md:927` promises "safe defaults".
  `mrfx status` with the dashboard up waits 12.5 s (two store opens with
  lock retries before the port probe, `cli.py:720-724`) and prints the
  memory-budget line three times. `serve` with redirected output under
  cp1252 crashes on the `→` in its banner (`cli.py:293`; `UnicodeEncodeError`,
  then the supervisor loop) — set `PYTHONUTF8=1` in both .bat files. Ctrl-C
  wording ("twice", `cli.py:1758`) vs `HOW_TO_RUN.md:149` once, and cmd.exe's
  "Terminate batch job (Y/N)?" is never mentioned. `HOW_TO_RUN.md:46-70`
  assumes PowerShell (`Activate.ps1`) — add the Command Prompt line. Doc
  drift: "mrfx.zip" vs "mrfx_project.zip"; the startup banner in
  `GETTING_STARTED.md:130` omits the `store:` line; "Files tab" vs the label
  "Files & imports"; `confirm_over_gb` 12 vs 15; "~30 s per uncompressed GB"
  (1061) vs "6–9 s" (529); README's catalog counts (110/34 vs 109 queueable /
  41 portals); README/config still say `pip install playwright && playwright
  install chromium` though it is pinned and auto-installed;
  `AI_HANDOFF.md:74` lists 11 of 27 subcommands. The updater refuses a zip
  lacking `mrfx/__init__.py` with the wrong reason when `tests/mrfx/` exists;
  `mrfx sources` exits 0 with "0 source(s)" on an unreadable catalog; `mrfx
  update` checks the HTTP port but not a running CLI drain, and prints no
  rebuild notice; `pyproject` declares no dependencies; the Python
  installer's "Disable path length limit" option is never mentioned
  (`filename_for` + a OneDrive Documents path ≈ 215 chars); `orgreport`
  without `--out` writes to the cwd.
- Dashboard nits: CSV buttons double-fire on a double-click (`#ro-csv`,
  `#ld-csv`; 644, 3668, 3810, 2053); queue-row buttons are 21 px tall
  (`padding:1px 8px; font-size:11.5px`), `.forget-btn` 23 px — under the
  24 px minimum; the strip says "Entities" for the TIN count while Overview
  says "Practices (TINs)"; the proposal "Change" column prints `+20.00
  (+66.7%)` without `$` beside `$` columns (1948); the Code page heading
  echoes the state as typed ("· mo") while the export uppercases it;
  `fetch` without try/catch on `#btn-scan`, "retry all failed" (which also
  stays disabled on error) and the source override; `loadRates`' error path
  leaves the pager enabled; dead code — `fillMonthSelect`'s no-prefix branch,
  `mny` duplicating `money`, `.pstrip .marks`, undefined `--pos`/`--neg`
  (dark mode ignores them), unreferenced `#explorer-filters`.

## Checked in this audit and found fine — do not re-chase

- Determinism at HEAD: 125/125 endpoints identical across two full runs.
- Every data-generation bump: full rebuild (`store.py:3795`), incremental
  rollup (3370), incremental directory refresh (3464), reset (4582);
  names-only rebuilds go through `rebuild_rollups`, so the round-6 caches see
  new names. (User edits are the gap — P1.3.)
- `fetch.py:184` `CERT_NONE` is leaf-certificate discovery for the chain
  repair only; the assembled chain is verified against certifi roots and the
  hostname before anything is trusted (`_repair_incomplete_chain`).
- `/api/files/{filename}` DELETE → `forget_file` rejects a non-plain name
  (400); `/confirm` requires a `files` row in `pending_confirmation`
  (basenames only); `/api/org-website` accepts http(s) only.
- The `DELETE FROM entity_map` seen in every profile is `create_app`'s
  one-time sync (`api.py:1008`), not per request.
- Inbox: with a real `mrfx serve`, the watcher ingested `.json`, `.json.gz`,
  `.zip` (and a zip inside a zip), gzip bytes under a `.json` name and a file
  with no extension without any button press; `.csv`, `.txt` and a corrupt
  `.gz` were quarantined with a plain reason and moved to `failed/`;
  "Rescan inbox" (`POST /api/files/scan`) picked up a second batch; processed
  files moved to `processed/`; one rollup per batch.
- Parking (commit 2c929dd): 6 tests, including one download slot with the
  stalled file first in line; the queue is still stuck after 120 s with
  parking off.
- The Aetna catalog additions (f5bb463): five HealthSparq listings verified
  live with the app's own checker (all OK) and sampled files (HTTP 200/206).

- Queue (verified line by line and by probe, `scratchpad/audit_d/`): a
  parked file cannot be woken while its downloader is still inside
  `download()`; disk reservations are released on every exit including a
  park; a user's stop is never overwritten by the worker's result; row claims
  and every `url_queue` write are under `write_lock`; no DuckDB connection is
  shared across threads; no lost wake-up (epoch read before the query);
  `If-Range` on every resuming reconnect, weak ETags never stored, 200-on-Range
  truncates and re-saves the validator; gzip bodies saved as sent and sniffed
  by magic (no double decode); the browser identity only after a 403 or two
  no-progress attempts, never by default; `done` rows untouchable by user
  actions; kill/restart re-queues in-flight rows and keeps `.part`/`.val`;
  the monthly refresh never re-queues paused rows; `filename_for` ≤ 129
  chars; `serve` shuts the parse pool down with `shutdown_now`.
- Ingest (verified by probe, `scratchpad/audit_b/`): fast and classic
  parsers produced byte-identical rows on 40+ hostile shapes (string / int /
  float codes and TINs, dashed TINs, `"97110.0"`, type spellings, scalar /
  list / null modifiers, mixed POS lists, partial refs, duplicate items and
  groups, two-member gzip + BOM + CRLF, non-ISO headers, `true` rates); one
  modifier rule in one module used by both parsers and the migration;
  SSN-pattern TINs stored raw, masked only on output; `file_month` header →
  filename → ingest with the overlap case right; same-filename re-ingest is
  an atomic replace and a 0-row re-ingest removes the old part; tmp parts
  never match the view glob; dead-pid sweep; corrupt-part quarantine;
  `claim_content_ingest` atomic; `forget_file` ordered unlink → views → one
  txn with the files row last; no `json.load` or whole-body read anywhere;
  0-row files `done`/0; allowed-amounts / TOC / drug files quarantined or
  counted with the reason; percentage/per-diem rows kept but out of dollar
  medians; bundles excluded and counted; placeholders ≤ $0.01 kept as rows
  and out of medians.
- Store (verified by probe, `scratchpad/audit_f/`): the spine grain and its
  `median(DISTINCT) FILTER` with placeholder coalesce; hash-slice boundaries
  never split a contract (incremental == full after additive, month-change
  and last-file-removal cases); pending-payer bookkeeping; directory
  `first(... ORDER BY count DESC, value)` choices; lock order rollup_lock →
  write_lock only, every other lock a leaf; rebuild atomicity (no instant
  with a missing table); connection retry; `ADD COLUMN IF NOT EXISTS …
  DEFAULT 0` fills old rows and every reader coalesces `park_count`;
  backup/verify sha256 over every file; the `_PICK` tie rule.
- Numbers (verified by probe, `scratchpad/audit_h/`): benchmark position
  mid-rank with the subject excluded and peer rates rounded first; missing
  MPFS code → "–" never 0; anchor = market state else the subject's;
  `compute_opportunity` floors at $0 per code and refuses negative/NaN
  units; payer comparison's two-level medians; contract gaps exact; the fee
  schedule's subject unaffected by peer scope, `_MIN_PEERS` = 3 suppression;
  the visit calculator's 8-minute rule on the manual's examples and a 4-code
  remainder case, 97140+97530 flagged, MPPR explicitly not applied and said;
  weighted vs unweighted both means with the median beside them; monitor's
  per-payer months and contract-line grain; inflation compounding and
  refusal; expiry sentinel years excluded; concentration median per practice
  and exact HHI; radius bands inclusive and covering; summary-strip
  cross-code quantiles labelled; unique export tiebreak; per-element masking.
- External data (verified by probe where marked RUN, `scratchpad/audit_e/`):
  NPIs and ZIPs are strings end to end with leading zeros kept; state codes
  through `state_code`; deactivation/reactivation logic; the API path's
  error handling, 429 retry, breaker and cancel; `enriched_at` stamped under
  the lock and keyed by the incremental refresh; Medicare importers' exact
  header refusals, transactional replace, dataset identity, direction and
  self-pair exclusion, <11 suppression stated; the Tracker discovery is
  read-only; MPFS CF bounded, locality disclosed, cross-state anchor refused;
  utilization streamed and idempotent (1M rows in 1.3 s); demographics
  suppression markers → None; floors state-required; hospital JSON streamed
  with ijson; inflation refusal and formula; synthetic 1M-row NPPES → cache
  in 3.2 s (arrow), 200,000 NPIs resolved in 12.5 s, weekly merge 0.7 s.
- Security (verified by probe, `scratchpad/audit_a/`): `filename_for`
  neutralises `..`, encoded slashes, device names and 300-char names; the
  inbox upload uses `Path(name).name`, a pid+uuid temp and a 1 GB cap; packet
  folders are slugged (`../../x` stays inside); the tracker import accepts
  only paths discovery just enumerated; zips are read in memory, never
  extracted (`cmd_verify` too); `_normalize_url` rejects `file:`, `data:`,
  localhost and 127.0.0.1; TLS only ever ADDS CAs, no `verify=False`
  anywhere; download bodies capped by `confirm_over_gb` + reservations,
  headers at 1 MB, pages at 64 MB, rendered bodies at 20 MB; every
  `FilterSet` clause parameterised, `order_sql` whitelisted, interpolated
  identifiers constant or validated; 18 top-level NaN/inf/huge numeric cases
  → 200/422; a full-route sweep with normal bodies on a hostile store → zero
  5xx; formula defusing positive in all eight CSV exports; masking in every
  JSON/CSV/HTML/packet output; `html.escape` in all five report renderers
  plus schedule, dossier, packets, spark; 144 `app.js` interpolations into
  HTML sinks all pass `esc()`; hrefs only from server-validated http(s) URLs
  or `encodeURIComponent`; tracker import and packet build single-flight;
  `page_size ≤ 1000` and every limit clamped; the user agent is MRF
  Explorer's own, the browser UA only after a 403; outbound traffic = pasted
  origins, the NPPES API, the certificate AIA URL, the Chromium auto-install.
- Operator surface (verified live, `scratchpad/audit_g/`): every command
  and flag named in the docs exists with that spelling; every config key
  named in the docs exists in `config.py` with the stated default; every UI
  label quoted in the docs exists in `index.html`/`app.js`; the round-6
  "What changed" claims match the code; a 27-case config matrix (unknown
  keys, BOM, CRLF, Windows paths, wrong types, empty file, relative/absolute
  paths) produced zero tracebacks — one `config problem:` line and exit 1;
  `mrfx.yaml.new` is written when the shipped file differs; the updater
  refuses zip-slip, drive letters, backslashes, non-zips and zips without
  `requirements.txt` before any write, never touches `data/`, `.venv/` or
  `config/mrfx.yaml`, backs up to `data/update_backups`, writes the .bat
  last, and refuses while the dashboard answers; both .bat files quote
  `%~dp0`/`%~1`, run the folder's own `python.exe`, and SIGINT to a healthy
  supervised server exits 0 cleanly; status/verify/backup/forget/update/
  packets refuse while the app is up; every pin has a win_amd64 wheel for
  cp311–cp314; 20 CLI commands run against a scratch store with no
  traceback, outputs only under the scratch paths, non-zero exit on every
  failure (two exceptions in P3).
- Dashboard (every tab and primary button driven live in Chromium with
  console/pageerror/HTTP capture, `scratchpad/ui/audit6/`): the round-6
  tickets and the Code page's AbortController hold; every init is guarded
  against re-entry and tab-scoped intervals are cleared; every `$("#…")` id
  exists or is created before use; all 144 server-string interpolations
  into HTML pass `esc()`, hrefs only from server-validated URLs; masking on
  every surface and the typeahead never offers an SSN-pattern TIN;
  lower-case states accepted everywhere; `money()` handles null/negatives,
  non-dollar rows render "–"; no "undefined/NaN/null" token in any rendered
  view; "latest" vs pinned-month labels correct on Benchmark, Rate card,
  Code page, Changes, New-to-network; `openReportWith` opens synchronously
  and shows the refusal; all six reports contain `@media print`; the pitch
  report refuses an unpriced subject; all client-side guards (NaN
  multiplier, empty peer set, non-numeric validation, Changes month order,
  page clamp, Clear scoped to Explorer) behaved as intended; 479 client-read
  keys resolved against server source.

## Report

End with: (1) for each P0/P1 item, the reproduction before and after; (2)
the snapshot diff (expected: 125/125 identical unless an item names the
endpoint); (3) every timing before and after, both stores, A/B alternating,
quiet machine, plus peak RSS at 99M; (4) the P1.2 proof that the
materialised tables give the live query's numbers on both stores; (5) what
you left and why; (6) what you could not measure — Windows is still
unmeasured, and the NPPES-fed feeds (new clinics, closures) and Medicare
referral leaders were not timed against a real full-size NPPES cache or
referral release.
