# Prompt for the next session, round 3 (written for Claude Opus 5.5)

Rounds 1 and 2 are `docs/NEXT_STEPS_PROMPT.md` and `docs/NEXT_STEPS_PROMPT_2.md`
(each has a status block of what it closed). This is round 3. It was built
from (a) three independent fresh-eyes code reviews of `api.py`/`benchmark.py`,
`store.py`, and `fetch.py`/`ingest.py`/`nppes_auto.py`, and (b) data probes on
a real 32M-rate store of 12 payer files — every finding below carries the
evidence that found it. Copy everything below the line into a fresh session.

---

You are picking up MRF Explorer (`mrfx/`). Read `CLAUDE.md`, then
`docs/AI_HANDOFF.md` in full, then `git log 5ea57c3..HEAD` (17 commits:
round 2 — the answer-layer audit and its fixes, the 306M-row scale work, the
Stop button, worker-side QA, the parser clean-up, the live payer-source
audit that grew the catalog to 145 entries / 104 auto-queueable, automatic
NPPES downloads, and the updater). The product: one non-technical user on a
Windows PC pastes payer links, then **searches by NPI to see what each payer
reimburses for PT/OT/SLP CPT codes**. The numbers are sold. Correctness beats
speed; "probably fine" is not done.

## Where things stand

VERIFIED: parser output identical to the pre-rewrite code on 22 real files
(up to 42 GB, 590M rows); an independent Arrow recount of 1.41M practice-level
groups on 32M real rows matched the store exactly; the hand-computed
known-answer test (`tests/mrfx/test_known_answer.py`) pins ingest → rollups →
API → CSV/ZIP; a 306M-row synthetic store measured (NPI search 0.6 s, removal
165 s, full rebuild 65 min); 694 tests pass, `-m quick` runs in 3 min; ruff
and pip-audit clean. 145 payer sources were live-verified on 2026-11-05.
Practice names now come from CMS's NPPES file, which the app downloads by
itself (fresh install: all 93,479 providers of a 32M-rate store named in
216 s). `mrfx update <zip>` / `Update MRF Explorer.bat` installs a new build
without touching `data/` or settings (live-tested).

NOT DONE: nothing has ever been run on Windows by an agent (the user's first
run is the test). The findings below are the open defects — several are in
code written in round 2.

## Non-negotiables

1. Every claim needs a live run with numbers; real payer files where the
   question is about real data (scratchpad has 12 real files; the known-answer
   store for hand-checkable cases). Trials live OUTSIDE the repo with their own
   config; never a real store.
2. Any change that touches numbers (parser normalization, grain relations,
   rollup queries, FilterSet) gets: a known-answer case with the expected
   number worked out by hand, the independent recount re-run on the real
   store, and `mrfx fastcheck` on 3 real files when the parser moves.
3. New safeguards get mutation-tested (break it on purpose; show the test
   fail). Fix the class, not the instance. Commit style: what + why +
   verified-with-numbers; push only to the branch you are told.
4. Honesty contract: never fabricate, never silently drop; every rule the
   query applies is stated in the methodology sidecar in the words that match.
5. A rollup-query change needs a `ROLLUP_SCHEMA_VERSION` bump, which forces
   the 65-minute full rebuild on a 300M-row store. Batch such changes into ONE
   bump and say so in the commit; prefer parser-level fixes (re-ingest per
   file) or query-time fixes where exactness allows.

## Priority 0 — the numbers (each verified; fix in this order)

**P0.1 Modifier-field hygiene — one payer's whole book vanishes under
"base only".** Measured on the real store (`scratchpad/xcheck`, 32,144,220
rows, 9 payers):

| payer | distinct modifier sets | rows with a COMMA inside one modifier | rows whose "modifiers" are place-of-service codes or a generic all-modifier list | rows the `base` filter keeps |
|---|---|---|---|---|
| BCBS Alabama (13.5M rows) | 24 | 0 | 10,595,968 (`21\|22\|24\|31`, `10\|11\|12\|32`, …) | **0 of 13,485,563** |
| First Choice Health (13.1M) | 2 | 0 | 6,546,284 (`26\|NU\|RR\|TC\|UE`) | 6,546,284 |
| BCBS North Dakota (2.7M) | 4 | 775,236 (`52,53` as ONE element) | 0 | 775,236 |

BCBS Alabama's 97110: **6,190 practices have rates; 0 appear under the
"base modifiers only" checkbox** (Code comparison) or in the Benchmark tab,
which "computes over base-modifier rows only". Its busiest TIN carries the
SAME five rates (20.19–36.97) under `21|22|24|31` AND `10|11|12|32`
(15,235 rows each) — these are place-of-service codes misfiled as modifiers,
each row duplicated per set. First Choice duplicates every row once with a
DME/radiology modifier list that cannot apply to 97110. Across all payers,
of 465,395 (payer, TIN, code) groups that carry more than one modifier set,
**249,138 (54%) have the identical rate under every set** — pure
fragmentation that shows one practice 2–3 times on the Code page.
`parser.py:815` only upper-cases, strips, dedups and sorts; it never splits
`52,53`, never recognizes POS codes, never recognizes a generic list.
Do: (1) measure per payer on the real files which pattern it is (POS codes
= numeric 01–99 with no GP/GO/GN/CQ/CO/KX/59 anywhere in the payer's therapy
rows; generic list = the same set on >95% of rows with rates equal to the
bare rows); (2) parser: split a modifier element on `,` `;` and whitespace;
move POS-looking sets to `service_code` when the payer's `service_code` is
empty/`01`; collapse a generic list to base; keep a QA counter per
transformation and name it in the Files tab; (3) decide and document what
"base" means for therapy (`''`, `GP`/`GO`/`GN`, `GP|KX`? `59|GP`?) and apply
it identically in FilterSet `modifier=base`, the Code page checkbox and
`benchmark._market_where`; (4) this is a RAW-row change: files re-ingest
(`mrfx ingest --force` / retry) — write the "your Alabama/First Choice/ND
rows need a re-ingest" message into the startup check, don't silently serve
the old rows next to the new; (5) known-answer cases for all three patterns
and a `fastcheck` run (the classic parser must change identically).

**P0.2 `latest` supersession is applied AFTER the user's filter.**
Reproduced on the known-answer store: `/api/code/97110?grain=tin` gives T1
`GP $42` (June); add `&rate_max=41` and T1 shows `GP $40` and `59|GP $40` —
the superseded May rate, while the sidecar says older months are
superseded. `api.rel_sql` wraps `WHERE {fs.where}` and THEN runs the
`QUALIFY file_month = max(file_month)` window, so any non-key filter
(rate_min/max, modifier, mod_has/mod_not, pos, dollar_only, hide_outliers,
state/city) can promote an old vintage. `benchmark._rates_relation`'s
docstring forbids exactly this. Fix: compute the window over a relation
filtered ONLY on partition-key columns (payer / unit / code / month), apply
the rest of the WHERE outside it; include `tin_is_really_npi` in the TIN
partition (an EIN and an npi-typed value that collide must not supersede
each other — `benchmark.py:215` already does this); make `/api/code` and
`/api/trend` call `_require_narrowing` (they skip it; `?grain=entity` on a
big store is the temp-disk 500 the guard prevents). Pin with known-answer
cases: filter + latest, two-TIN NPI + latest.

**P0.3 Entity grain disagrees with the Benchmark and with itself.** A
fresh-eyes probe (TIN A: June $45 / July $55; TIN B same org, June-only
$40): Explorer entity grain with `latest=1` = **$55, tin_count 1** (B is
dropped — `_ENTITY_REL` groups by file_month, then max(file_month) runs per
entity); `benchmark._entity_rates` for the same market = **$47.50**. Also:
state/city/therapy filters are applied AFTER aggregating member TINs
(`states` is a union, `is_therapy` a bool_or), so an entity with a MO TIN
($40) and a KS TIN ($60) shows $50 under `state=MO` while the Benchmark says
$40; the entity key is the bare NPPES name, so two unrelated same-named
practices in different states merge; `rate_variants` has mixed semantics
(`count(DISTINCT member medians)` if >1 else `max(rate_variants)`); and
`sum(npi_count)` / `sum(source_count)` double-count an NPI shared by member
TINs (the sidecar admits it; it should be `count(DISTINCT …)`). Fix: collapse
latest per (payer, tin_value, tin_is_really_npi, code) INSIDE the TIN
sub-relation; push state/city/therapy predicates into `s`; key entities on
(name, state) unless the entity_map says otherwise; split rate_variants into
two honest columns; distinct NPI counts. Known-answer cases for each.

**P0.4 The Code page's payer leaderboard and the summary strip blend what
the Benchmark separates.** `payer_rank` (`api.py` ~1143) and `/api/summary`
`by_code` take per-practice medians grouped by (payer, unit_id) only —
professional and institutional rows, CQ/CO assistant rows (unless the
checkbox is on), NPI-in-TIN-slot rows and `discipline='unspecified'` rows
are blended into one practice median, while `benchmark._market_where`
excludes `tin_is_really_npi` and keys on billing class. Probe: peer with
professional $50 + institutional $100 → unit median $75 → Code page "payer
median $60 (n=5)" vs Benchmark p50 $55 on the same store/month; % of
Medicare inherits it. Also `FilterSet` `discipline IN (...)` excludes
`unspecified` rows while the benchmark includes them (PT $70 + unspecified
$65 → Explorer 70, Benchmark 67.5). Fix: one shared "practice rate" CTE
(billing_class default professional, NOT tin_is_really_npi, base-only rule
from P0.1, discipline rule stated) used by payer_rank, by_code, and the
benchmark; label the basis on-screen.

**P0.5 Determinism gaps (invariant 7).** Practice NAMES are not stable
across rebuilds: `TIN_DIRECTORY_QUERY` (`store.py` ~555–566) picks
`mode(org_name)`, `mode(discipline)` and `any_value(r.tin_type)`, all
thread-order dependent on ties — a reviewer's probe (4 threads, 2-way ties)
got 5 different results in 6 runs. A TIN whose NPI-2s split evenly between
a parent name and a per-site entity (common) gets a different
`display_name` on each rebuild — on a sold rate card. Fix: a counts CTE +
`QUALIFY row_number() OVER (PARTITION BY tin_value ORDER BY c DESC,
org_name) = 1`; `min(r.tin_type)`; pin with a 20-run rebuild test on a
tied fixture. In the API, `order_sql` (`api.py:516`) omits
`tin_value` and `negotiated_rate`: at NPI grain an NPI under two TINs with
the same payer/code/modifiers/month/class/POS ties on every key — the
reviewer's export came back …13 before …12 on one run; paged views can
duplicate or lose a row. `ranked` (Code page) lacks `billing_class`/
`is_dollar_rate` in its ORDER BY; `entity_detail` lacks `billing_class`/
`service_code_set`/`is_dollar_rate`. Fix the three ORDER BYs; add an
NPI-grain two-TIN case to `tests/mrfx/test_determinism.py` (20 runs, byte-
identical).

**P0.6 `file_month` means different things for different payers, and one
file is dated in the future.** Real store, header `last_updated_on` →
`file_month`: BCBS Mississippi's **October** index files carry `2026-08-25`
→ month `2026-08`; Highmark/Wyoming/Alabama **November** files carry
`2026-10-01…15` → `2026-10`; Tufts/Moda November → `2026-11`; a UHC
HEALTHSCOPE file carries `2026-12-01` → `2026-12` (the Months dropdown
offers December on November 6, and "latest" would prefer it). "Newest per
contract" is unaffected (it is per payer), but a pinned month compares
payers' different clocks, the Rate-changes tab's "newest month vs the one
before" and the Clients digest are affected, and a future header date is
simply wrong. Decide and document: keep `file_month` = header month (the
payer's statement of vintage) but ALSO record `published_month` (index /
filename date, else ingest month) per file and show both; clamp a header
date later than ingest to the publication month with a QA message. Pin.

**P0.7 Timestamps are stored in the PC's local time — rows can go missing
from the rollups.** `store.py` has no `SET TimeZone`; DuckDB follows the OS
zone. `finished_at` (`FINISHED_NOW`, `update_url`, `ingest._now()`) is
stamped with an AWARE UTC datetime, which DuckDB converts to local
wall-clock on the way into a `TIMESTAMP` column, while `save_npis_bulk`
stamps a NAIVE UTC one. Verified here: with `TZ=America/New_York`, aware
`06:30Z` is stored as `01:30:00`, naive `06:30` as `06:30:00`. This Linux
box runs on UTC, so no test can see it; the user's Windows PC will. Effects:
(a) the rollup coverage marker `rollup_covered_through` is compared as a
STRING of local time — on the DST fall-back night (or any clock set-back) a
file finishing 30 real minutes after the marker stamps an EARLIER string,
never satisfies `>= covered`, and its rows are permanently absent from the
spine with `rollups_stale()` False; (b) `save_npi` (API lookups, aware) and
`save_npis_bulk` (bulk, naive) differ by 5–8 h, so after a bulk pass sets
`directory_names_through` the later API saves land "earlier" and
`refresh_directory_incremental` never materializes their names/states/
is_therapy until a full names rebuild. Fix: stamp everything naive UTC
(`.replace(tzinfo=None)`) or `SET TimeZone='UTC'` on every connection AND
compare markers as timestamps, not strings; a one-time backfill; a test that
runs the marker logic under `TZ=America/New_York` across 2026-11-01 01:xx.

**P0.8 % of Medicare uses one national median regardless of the
practice's locality.** `api.py:947,1173`, `benchmark.py:535` and
`schedule.py`/`market.py` take `median(non_facility_rate) GROUP BY code`
over whatever MPFS extract was loaded (e.g. "REST OF MISSOURI") and apply
it to a Kansas client; only the Benchmark footer discloses the locality,
the Explorer summary and Code page show a bare percentage. Fix: store
locality/state on `mpfs`; pick by market/practice state (CMS locality ↔
state/county table); badge the anchor on every % shown; refuse (not
estimate) when no locality matches.

## Priority 1 — robustness on a Windows PC left running for weeks

**P1.1 A worker that thinks its parent died kills a processor thread —
and on Windows it may think so wrongly.** `store._pid_alive` (nt branch,
line ~296): `OpenProcess(SYNCHRONIZE)` returning no handle → `return False`
("dead") — the docstring says "unsure → True". Under EDR / an elevation
mismatch, access-denied returns no handle, so EVERY parse's first progress
tick concludes the parent is dead, the worker raises `SystemExit`
(`ingest.py` `report_chunk`), `fut.result()` re-raises it in the parent as a
BaseException, it passes every `except Exception` (ingest, process_url_record,
the processor loop at `fetch.py` ~2277), and that processor thread dies
silently with the url row left `ingesting` and the files row `processing`
until restart. After `parallel_ingests` files the queue stops with `fetched`
rows piling up and nothing logged (reproduced with a ProcessPoolExecutor
probe). Fix: nt branch — call `GetLastError()`; `ERROR_INVALID_PARAMETER` →
dead, anything else → alive; worker raises `RuntimeError("parent gone")` not
`SystemExit` (and returns nothing the parent would adopt); processor and
downloader loops catch `BaseException` (re-raise `KeyboardInterrupt`); the
coordinator respawns a dead processor thread and logs it. Test with a fake
`_pid_alive` returning False.

**P1.2 Month-rollover gaps in the automatic refresh.** `_maybe_queue_prev_month`
falls back to last month's URL only if that URL is NOT already in the queue —
but after a month of use it is (`done`), so nothing is queued, the "not posted
yet" hint isn't written, and `monthly_refresh` (opt-in, `cli.py:127`) stamps
the month done at its first hourly tick after midnight on the 1st: every
`{FIRST_OF_MONTH}` source 404s and stays `failed` all month unless a human
presses "retry failed". Also: with the catalog now at 104 auto-queueable
sources, an opted-in refresh re-queues ALL of them (hundreds of thousands of
files), not the ones the user chose. Fix: treat a 404 / HTML-interstitial on a
current-month URL during the first ~10 days as "not posted yet" (row stays
queued with a not-before time, or the refresh loop re-adds it daily and stamps
the month only after success or day 10); scope `monthly_refresh` to sources
that already have a `done` row (what the user actually loaded).

**P1.3 Stop/cancel loose ends.** (a) `clear_parse_stop(name)` runs only on
the `ParseStopped` path — a stop pressed after the last read (worker QA,
finalize, rollup) leaves the filename flagged for the server's life and the
NEXT ingest of that filename dies at its first read as "stopped by you"; put
it in `ingest_file`'s `finally`. (b) A worker killed during QA leaves
`.{key}.{pid}.qa-spill/` (GBs) that the sweep keeps while the parent lives —
`rmtree` it in the parent's `finally`. (c) `force_heal(cancel_futures=True)`
turns other threads' PENDING parses into `failed` rows needing a manual retry,
contrary to the log's "re-queue automatically" — on `CancelledError` set the
row back to `queued` (download + sidecar are on disk). (d) "Stop downloads" /
"Clear queue" park rows as `skipped kind=canceled` with no bulk revive —
add "resume stopped". (e) `requeue_oversize_within` treats `bytes_total=0`
(chunked transfer, unknown length) as "fits" → a chunked 8 GB file re-
downloads 5 GB and trips oversize on every restart; require `bytes_total > 0`.

**P1.4 `nppes_auto` edges.** No cross-process guard: serve's enrichment
loop and a CLI command can both see a stale stamp and append to the same
`.part` (on Windows the second `replace` fails or lands an interleaved zip
that the validator discards — two 1.1 GB downloads wasted hourly); take an
`O_EXCL` lock file (or touch the stamp) BEFORE downloading. If the stamp
can't be written (OneDrive-locked / read-only `data/`), `_stamp_fresh` is
always False and api-mode polls CMS every 20 s — keep an in-memory last-check
fallback. Measure the cache build alternatives: the pure-Python build is
164 s / 0.26 GB RSS; pyarrow's CSV reader streaming from the zip
single-threaded is **72 s** with byte-identical output (hash verified) but
**2.9 GB RSS**; from an extracted CSV 51 s but extraction costs 110 s. Pick
(smaller `block_size` may cut the RSS); never let it run while parse workers
are near the RAM budget.

**P1.5 Windows, first run.** Still never done by an agent. If you have a
Windows runner: clean venv on 3.11 and 3.14, full suite, `mrfx serve`, paste
3 sources (one Sapphire hub, one HealthSparq `latest_metadata.json`, one
Highmark index), let 10+ files drain including one ≥1.5 GB uncompressed,
Task Manager memory, force-kill at each stage and restart, `mrfx verify`,
`Update MRF Explorer.bat` with a next build, the `.bat` auto-restart loop,
console encoding of the new log lines. If not: say so plainly; do not guess.

**P1.6 Rollups can silently drift from the raw parts (store review).**
(a) `_quarantine_corrupt_parts` removes a truncated part from the glob but
leaves its `files` row `done` and older than the marker: the spine keeps
serving its rows, the NPI grain (live view) doesn't, `rollups_stale()` is
False — match `file_key` back to `files`, flip it to failed with the reason,
set `rollup_needs_full`. (b) A failed `unlink_with_retry` of an old part
(`drop_rates_part`, 0-row re-ingest during a long export on Windows) is
ignored or only logged; the part stays in the glob forever (the sweep
ignores `.parquet`) while the files row says 0 rows and the pending payers
get consumed — record doomed part names in meta, retry at open and before
each rollup, hold `rollup_needs_full` until it succeeds. (c) A re-ingest that
fails/stops/crash-recovers leaves the OLD part live with the row `failed`,
which the delta ignores — if the marker advanced past the file meanwhile its
rows never roll up; in the failure handlers and `recover_stuck_files`, queue
the surviving part's payers/TINs. (d) The OOM ladder re-runs every table at
the finer split including ones already committed (a directory OOM re-does
the 65-min spine) — track per-table completion; size the directory slice
from `count(*) WHERE tin_pred`, not the payer's row count. (e)
`unenriched_npis` is O(store) per call and API mode calls it per 200 NPIs —
materialize the backlog once per `data_generation`. (f)
`cancel_all_downloading` adds ids to the in-memory set AFTER the commit — a
download finishing in that window overwrites the user's skip and the flag
lingers for the next retry; populate the set inside `write_lock` first.
(g) `finalize_rates_part`'s `replace_with_retry` PermissionError (export
> 90 s) fails a multi-hour parse while the complete tmp is kept but never
adopted — catch it, park the tmp under a recorded name, adopt at next open.
Verified OK by the same review: the pending-payer generation counter, the
coverage-marker snapshots and same-tick tie-break, single-writer discipline,
lock nesting, `_claim_next`'s total order, deterministic BY_TIN/DEDUP picks.

## Priority 2 — efficiency (measured on the 306M-row synthetic store)

- **P2.1 Full rebuild 65 min** (C1): O(slices × rows) because the hash
  predicate can't prune parquet. Design: one `COPY … (PARTITION_BY
  hash_bucket)` pass to temp parquet, then aggregate each bucket file —
  O(2 × rows). Needed before any `ROLLUP_SCHEMA_VERSION` bump (P0.1/P0.3
  will want one).
- **P2.2 Sort parts by (payer, tin_value, billing_code) at write time**
  (`RatesPartWriter`, one sort per part): directory slices currently re-scan
  the whole store per slice (~8 s/slice × 7–11), and the NPI view with a
  payer-only or code-only filter takes 30–51 s because nothing prunes. Measure
  before/after on the synthetic store; parts written before the change stay
  unsorted — handle both.
- **P2.3 `/api/stats` cold 35–42 s** (count DISTINCT npi/tin over raw):
  maintain from the rollups (`tin_directory` count, `npi_directory` + the
  per-file QA) instead of rescanning.
- **P2.4 Worker memory through parse + in-worker QA**: measured 1.48 GB peak
  on a 13.5M-row file (fits the 2 GB/worker budget). Re-measure on the full
  Aetna file (590M rows: the DISTINCT in QA spills) before trusting it.
- **P2.5 Formats the queue can't read today** (found in the source audit):
  `.7z` (Baylor Scott & White), `.zst` (MercyCare), ZIP-inside-ZIP
  (CareSource), extension-less index endpoints, and indexes that are
  concatenated JSON objects (UCare, 1199SEIU, Network Health) — a lenient
  "several top-level objects" reader covers three payers. `max_toc_files`
  (2,000, alphabetical) hides most of Allied's 32,397 group indexes and
  Meritain's 4,173 files — prefer in-network/TOC entries first and let the
  user page/raise it from the UI.

## Priority 3 — sources & long-grind UX

- Add a `states:` field to `config/known_sources.yaml` entries and a
  state/payer picker to "Show tested sources" so the user queues their states
  instead of all 104; ship the audit scripts (`probe.py`, `verify_candidates.py`)
  as `mrfx sources --check` so the catalog can be re-audited monthly with one
  command (payer pages move constantly: 18 of 72 were dead after four months).
- HealthSparq `latest_metadata.json` is an undocumented endpoint used by 16
  entries — add a fixture test for its shape and a plain message when it
  changes. CHPW's links expire in 24 h — queue its files first. Moda's index
  lists 5 files Moda hasn't posted (payer-side; retry later message exists).
- Expired signed children now name their parent index; the next step is to
  re-expand that parent automatically once per failure wave.

## Priority 4 — tests & docs

- Extend `test_known_answer.py` to the Benchmark, Rate card and Markets tabs
  (hand-computed percentiles, base-only rule, latest supersession) and to the
  three modifier patterns of P0.1.
- Regenerate `tests/slow_tests.txt` after the new tests; keep `-m quick` under
  3 min.
- AI_HANDOFF Step 8: fold in the round-2 status, the modifier findings, and
  the `file_month` decision; GETTING_STARTED's timing table is still from a
  4-core Linux box — replace with the user's Windows numbers when they exist.

## Deliberately out of scope

Remote `location` provider references (no payer uses them); a compiled
scanner (<2× measured); the legacy `src/` tool; re-running the 22-file
parser equivalence unless the parser changes (then `mrfx fastcheck`).

## How to report

End with: (1) the per-payer modifier findings and the rule you implemented,
with before/after counts for Alabama 97110 under "base only"; (2) each
known-answer case added and its hand-computed number; (3) the independent
recount result after any raw-row change; (4) the scale table before/after
for anything you touched in P2; (5) Windows results or the explicit
statement that none were run; (6) every bug with repro and test; (7) what
you left alone and why; (8) what you could not verify — say so rather than
rounding up to "done".
