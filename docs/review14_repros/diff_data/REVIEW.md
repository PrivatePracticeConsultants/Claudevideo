# Round 13 (0.7.7) adversarial review — DATA AND NUMBERS half

Repo `/home/user/Claudevideo`, branch `claude/gracious-mccarthy-ao7vmo`, head `9617c59`, read-only.
Old code: `git archive c8b80fc mrfx` → `diff_data/old/` (run with `PYTHONPATH` pointing there; every
old-code run prints the `mrfx.__file__` it imported). Scripts: `diff_data/scripts/`, outputs:
`diff_data/out/*.log`, scratch stores `diff_data/w_*` (all hand-made, never a user store; scale4n untouched).
Run any script with `/home/user/Claudevideo/.venv/bin/python scripts/<name>.py` from `diff_data/`.

Severity: P0 wrong number / double rows / a real file skipped · P1 stuck, crash, user action undone,
two screens disagree · P2 waste / misleading message · P3 cosmetic.

---------------------------------------------------------------------------------------------------

## P0

### P0-1  A store loaded by 0.7.6 has no content identity, and the backfill never adds one: a re-encoded twin of ANY pre-upgrade file loads twice — CONFIRMED
`mrfx/ingest.py:2060-2064` (`backfill_identities`) selects only `c.filename IS NULL OR c.full_sha IS NULL`.
Every file 0.7.6 loaded HAS `full_sha` and has NO `content_sha`, so none of them is backfilled.
`_same_content_locked` (`mrfx/store.py:3481`) matches only `content_fingerprints.content_sha`, so the
5.2.1 protection does not cover any of the user's 615 files that were loaded before 0.7.7.

Repro `scripts/s8_legacy_twin.py`: c8b80fc loads `twin_in-network-rates.json.gz`. The new code then
runs `backfill_identities` (its kept copy is in processed/), and the browser-decoded copy
`twin_in-network-rates (1).json` is dropped in:
```
backfilled: 0
fingerprint of the 0.7.6 file: [('twin_in-network-rates.json.gz', True, None)]
files: [('twin_in-network-rates (1).json', 'done'), ('twin_in-network-rates.json.gz', 'done')]
rate rows: 120 (one document = 60)
```
What should happen: backfill `content_sha` for rows where it is NULL (from the kept copy, which is the
same read). If no copy is kept, compute it the first time either twin is seen. The new suite has no
test for the case where the first copy was loaded by a version before 0.7.7.

### P0-2  A link whose ingest fails keeps its content claim: the same document under another link is skipped for good, and an inbox copy blocks the inbox watcher — CONFIRMED
`mrfx/store.py:3478` (`claim_content_ingest` sets `_content_claims[csha]`) is released only by
`_finish_ok` (`mrfx/ingest.py:420`), which runs on success. The fetch path's failure branches
(`mrfx/fetch.py` after `ingest_file`: "ingest crashed", status failed/stopped/requeued) never release it.
After the failure:
- (a) a second link with the same content and other bytes is flipped to `skipped/duplicate` with
  "being loaded right now — will retry automatically … if that one fails". It never retries:
  `_revive_twins` matches the RAW sha, and the twin's raw sha differs.
- (b) an inbox copy waits in `_content_claim`'s `while True … sleep(2.0)` (`mrfx/ingest.py:1887-1909`)
  until a restart or a manual stop. That freezes `scan_inbox` for every later drop.

Repro `scripts/s11_failed_link_claim.py`. Link A's first ingest raises once (a transient error). Then
link B (the same document at another gzip level) is added, then a decoded inbox copy:
```
after link A failed: content claims held = {'92856b6a…': '0e029f3e_a.json.gz'}
queue: [(…/a.json.gz, 'failed', …, 'ingest crashed: simulated transient failure …'),
        (…/b.json.gz, 'skipped', 'duplicate', 'the same content as 0e029f3e_a.json.gz, which is being loaded right now — will retry automatically from the kept download if that one fails')]
rate rows: 0 (the document = 60)
inbox copy: scan_inbox returned within 20 s: False None
```
What should happen: release the content claim on every terminal outcome of the link's ingest, as the
inbox path does in `ingest_file`'s `finally`. On failure, revive `duplicate_of = <this file>` /
same-`content_sha2` twins, not only same-raw-sha twins. The stuck inbox wait is P1 on its own.

### P0-3  A full plan that starts with `rollup_needs_full` set erases a needs-full raised during the plan: a forgotten file's rows stay in the spine and the analytics say "current" — CONFIRMED (rare trigger; the same rule existed in 0.7.6's one-shot build, but a plan now stays part-way across restarts and days)
`mrfx/store.py:5413-5416` (`_finish_plan`): `if plan.get("needs_full"): DELETE … rollup_needs_full`.
The flag is a bare "1", so a second unscopable removal during the plan (an unreadable part at forget
time, `_queue_removed_payers(scope=None)`, or a quarantined corrupt part) cannot be told from the first.

Repro `scripts/s2_midplan.py`, scenario d. A complete build exists and `rollup_needs_full=1`. The plan
is killed after 9 of 55 slices (Alpha's slices done). Then `forget_file("p0_f1.json")` runs while the
part's scope cannot be read. The plan resumes, then the queue's catch-up runs:
```
== d_unscopable_forget_while_needs_full_plan: plan part-way 9/55, payers touched ['Alpha']
   [plan finished] state=current payers 4/4 files 7/7 …
   stale after plan: False | needs_full: None
   RESULT spine identical to fresh build: False (1634 vs 1575); directory identical: True
   extra rows in spine: 114 missing: 55 payers of extras: ['Alpha']
```
Control (scenario e, no prior flag): the flag survives and the catch-up heals it (identical).
What should happen: a generation counter for needs-full, like `rollup_pending_payers.gen`; clear it
only if it is unchanged since the plan's snapshot.

---------------------------------------------------------------------------------------------------

## P1

### P1-1  During a full plan over an existing spine, the header and every document say "0 of N payers", the payer chips say N-1, and every payer is answering — CONFIRMED
`mrfx/store.py:6556-6560` keeps only payers whose slices are done in this plan (`p not in last or p in
plan_done`), and every payer is in `last`. So while a full plan runs IN PLACE over a complete prior
build, `covered_payers` is 0, even though the old rows of every not-yet-rebuilt payer still serve every
screen. `/api/payers` (`mrfx/api.py:4979`, `payers_part_way`) uses a different rule, so the two disagree.
The document sentence also claims "the files loaded since are not in the analytics yet" when none were.
This path is reachable: any refused incremental (rows with no payer, a removal that cannot be scoped,
an incremental OOM at max split, a quarantined part) runs the full fallback, and so does P2-1.

Repro `scripts/s3_names_plan_and_header.py`, S3b. The prior build is complete, then a full plan is
killed after 4 of 55 parts:
```
S3b header: Analytics cover 0 of 4 payers (8 of 8 files). A full analytics build is part-way (4 of 55 parts saved) — …
   /api/payers covered: ['Beta', "O'Neil Health", 'Zeta']
   /api/rates payer='Alpha': total=492   … 'Beta': 489 … "O'Neil Health": 494 … 'Zeta': 182
   document sentence: ['Analytics cover 0 of 4 payers (8 of 8 files). Every number here comes from those; the files loaded since are not in the analytics yet, …']
```
What should happen: a payer counts as covered when it is not part-way, i.e. the `/api/payers` rule,
so its old complete rows count while untouched. Use one rule for the header, the chips and
`analytics_note`. A plan with nothing loaded since `cover_ts` should not print the "loaded since" clause.

### P1-2  `monthly_recheck` (now ON by default) queues and downloads every NEW file a re-fetched index lists, the bandwidth the decision kept opt-in — CONFIRMED
`mrfx/fetch.py:2026-2055` re-queues every loaded pasted index (`toc`/`page`) monthly, and the
re-expansion (`mrfx/fetch.py:3219`, `_enqueue_children`) queues every child it has not seen. That
contradicts the 5.2.5 decision ("queueing NEW dated months spends bandwidth and disk, so it stays the
user's choice") and `config.py:130-135` ("only changed files download"). A payer index that adds next
month's file names (the usual layout) means a full download of that month for every loaded index,
with `monthly_refresh: false`.

Repro `scripts/s13_recheck_new_children.py`. The index lists the Nov file, gets loaded, and next month
also lists the Dec file. One tick runs with the defaults:
```
defaults: monthly_refresh = False | monthly_recheck = True
tick: {… 'static': 1, 'requeued_static': 1, 'done': True}
requests during the re-check: [('/index.json', 200, None, 402), ('/2026-11-01_…', 206, 'bytes=0-0', 1),
                               ('/2026-12-01_…', 200, None, 476)]      <- the NEW file downloaded in full
queue: […, ('2026-12-01_twin_in-network-rates.json.gz', 'done', '')]
```
What should happen: on a recheck-only refetch (`revalidate` from the recheck, `monthly_refresh` off),
probe the loaded children and only record the new ones ("N new files listed — paste the index again
or turn on monthly_refresh"). Do not queue them.

### P1-3  The read identity in the 'done' commit (1427445) covers only the same-name check: after a kill in that window, a copy of the same bytes under another name loads twice — CONFIRMED (kill-dependent; the double rows would be P0)
`files.read_identity` is read only by `Store.loaded_identity` (`mrfx/store.py:3206-3230`). Twin
detection (`_loaded_or_claimed_locked` `:3506`, `_same_content_locked` `:3481`) reads only
`content_fingerprints`, which the kill skipped. The status block measured this window at 2 of 25 kill
trials (the analytics update runs inside it).

Repro `scripts/s10_killwindow_twin.py`. It recreates the state the round's own
`test_5_2_6_a_kill_after_the_commit…` builds (no identity row), then drops a twin:
```
byte-identical copy: read_identity={"size": 656, … "sha": "a7cd3...  files=[('k (1).json.gz','done'), ('k.json.gz','done')] rate rows=120 (one document = 60)
decoded copy:        … files=[('k (1).json','done'), ('k.json.gz','done')] rate rows=120 (one document = 60)
```
What should happen: write the fingerprint row (full sha and content sha) in the same transaction as
the 'done' upsert, or have both twin queries fall back to `files.read_identity`, and carry `csha` in it.

---------------------------------------------------------------------------------------------------

## P2

### P2-1  A killed names-only (directory) build escalates to a WHOLE-STORE full rebuild at the next idle gap — CONFIRMED
`mrfx/store.py:4576-4578`: the incremental refuses whenever any plan is part-way ("it continues as a
full build"). Both callers (`fetch.rebuild_now`, `cli._catch_up`) then call `rebuild_rollups()` (full),
and `_run_plan` drops a `names` plan when a full build is asked for (`:5283`). On the 1.16 B-row store
that turns minutes into 2-4 h, and P1-1's "0 of 49 payers" shows the whole time.

Repro `scripts/s3_names_plan_and_header.py`, S3a:
```
S3a names plan part-way: names 1/17 | rollups_stale(): True | reason: an analytics build is part-way (1 of 17 parts saved) …
   incremental refused: an analytics build is part-way — it continues as a full build -> rebuild_rollups() (FULL)
   plans created by the catch-up (kind, slices, spine slices): [('full', 287, 269)]
```
What should happen: when the plan is `names`, resume it with `rebuild_rollups(names_only=True)` (or let
the incremental resume it), then run the incremental. Also, the header's reason
(`store.py:6589`) says "A full analytics build is part-way" for a names plan.

### P2-2  5.2.1 skips a re-encoded twin even when its name carries a different month, so a month is lost when file_month comes from the name — CONFIRMED (narrow trigger; byte-identical twins already did this in 0.7.6)
`file_month_for` (`mrfx/parser.py:294`) takes the month from the file name when the header has no date,
or when the header is dated in the future (the measured UHC HEALTHSCOPE case). Two files with equal
content and names `twin_2026-05-01_…json` / `twin_2026-06-01_…json.gz` hold two months on c8b80fc and
one on 0.7.7. Repro `scripts/s7_month_twin.py`:
```
old  files: …2026-05… done, …2026-06….json.gz done      months: [['2026-05', 20], ['2026-06', 20]]
new  files: …2026-05… done, …2026-06….json.gz skipped "the same content as …"   months: [['2026-05', 20]]
```
What should happen: when the twin's derived file_month (or plan tags) differs from the loaded file's,
do not treat it as a twin, or record it as the same data published for that month. At minimum the
skip message should name the month that is not loaded. Related, pre-existing and not changed by this
round: `files_for_plan` (`store.py:2798`) joins plan tags only through the file's own done row, so a
plan whose only file was skipped as a twin answers "no ingested files are tagged with the plan".

### P2-3  4.2's rule sentence is not "printed with every all-payer figure" — CONFIRMED
`PRACTICE_RATE_RULE` appears only in `benchmark.methodology_footer` (`mrfx/benchmark.py:2117`) and the
outreach note. Several outputs built on all-payer per-practice medians (`practice_base_sql` without
`per_payer`) do not carry it. Repro `scripts/s5_rule_sentence.py`:
```
200 /api/report/pitch                      rule printed: True
200 /api/report/market                     rule printed: False   (city medians, payer medians)
200 /api/leads.csv                         rule printed: False   (all-payer practice positions, "every payer")
200 /api/report/dossier                    rule printed: False
200 /api/export.zip?grain=tin&state=MO     rule printed: False   (sidecar)
200 /api/export/outreach.zip?state=MO      rule printed: True
200 /api/report/ratecard /schedule/fee.csv rule printed: False   (mostly per-payer; its peer columns are all-payer)
```
(The visit-value endpoints returned 422 on this tiny market, so they were not checked.) What should
happen: add the sentence wherever an all-payer practice rate is printed (leads CSV header, market
report, dossier, export sidecar), or narrow the status block's claim.

### P2-4  A hospital file loaded without `--state` (the flag is optional) never reaches a negotiation document, and the reason given is wrong — CONFIRMED
`hospital_lines` filters `h.state = <document state>` (`mrfx/anchors.py`, through `hospital_filters`),
and the import stores `state=None` when none is given. The omitted-line reason reads "no loaded
hospital file in MO lists a rate from this practice's payers … (payer names must match exactly …)".
Repro `scripts/s6_anchors.py`:
```
hospital without state -> {'lines': [], 'reason': "no loaded hospital file in MO lists a rate from this practice's payers for these codes (payer names must match exactly between the two sources)"}
```
What should happen: when loaded hospital files have no state, say so ("N hospital file(s) have no
state — reload with --state").
Pre-existing, noted only: `import_hospital_file` names the part `hospital/<slug(name)>.parquet`, so a
second hospital with the same name (another city) REPLACES the first. Same script: two MO "Mercy
Hospital" files (80.00 and 200.00) give "pays Mercy Hospital $200.00".

---------------------------------------------------------------------------------------------------

## P3

- **P3-1 Stale docstring.** `mrfx/visit_market.py:42-43` still says the quartile "is read from the
  printed P25 / median / P75", which contradicts 4.1 (the code and SUBJECT_NOTE use the printed
  percentile).
- **P3-2 Re-read button counts files it cannot re-read.** After "Re-read N files", a file with no copy
  and no link stays in `files_missing_codes()` (s9 shows `missing after: [b.json]`). The button keeps
  offering N files, one of which it cannot read; it should count only re-readable ones and list the rest.
- **P3-3 Medicaid "below" test uses unrounded values.** A subject rate of 29.996 prints "pays you $30.00
  (below Medicaid)" beside "Medicaid pays $30.00" (`scripts/s6_anchors.py`). This cannot be reached
  from today's documents, because their subject rates are already `round(…, 2)`. It can be reached from
  a schedule with sub-cent rates.
- **P3-4 Leftover `__next` table.** A plan started by another rules version is dropped without
  dropping its `<table>__next` target if the new plan builds in place (`_run_plan` / `_new_plan`): the
  orphan table stays on disk.

---------------------------------------------------------------------------------------------------

## Probed and held

- **Sliced spine and directory byte-identical to the OLD one-shot build (c8b80fc's `rebuild_rollups`
  run on a copy of the same parts).** `scripts/s1_identity.py`. Shapes: four payers, 6 TINs shared by
  every payer, an untyped 10-digit TIN (`tin_is_really_npi`), a 9-digit npi-typed TIN that collides
  with an EIN string, an SSN-pattern TIN, a payer name with a quote, codes `0001U … abc`, months split
  over two files per payer, contracts missing from the newest month (`is_latest`), $0.01 placeholders,
  and one payer with a single huge code (hash halves). At 10M / 400 / 60 / 7 rows per slice (up to
  287 slices, 286 hash-split): spine 1729/1729 and directory 36/36 identical every time.
- **Files landing or forgotten mid-plan** (`scripts/s2_midplan.py`, scenarios a-c, e): a new file for
  a payer that is already done, a new file for a payer still pending, a forget of a done payer's file,
  and an unscopable forget with no prior flag. Each is not covered while part-way, `behind` after the
  plan, and healed by the incremental: spine and directory identical to a fresh build, coverage files
  and rows correct.
- **Crash between a slice's commit and the plan pointer:** they are one transaction (`_run_slice`). OOM
  halving keeps `next` and is exact (hash `% 2k ∈ {i, i+k}`). Versions mismatch → a new plan. The
  directory slices run first and read raw rows across payers, so no directory row is built from a
  partial payer set without the incremental's TIN delta healing it. Per-slice generation bumps
  invalidate `cached`, `spine_cached` and `spine_materialised`.
- **Outreach rewrite (`_Rounder`, `csv.writer`, pairs relation) and the Overview from materialised
  medians: old vs new byte for byte** (`scripts/s4_old_vs_new.py`). Tie block (3 TINs at exactly
  $50), 134 negative gaps, a placeholder-only practice, two SSN-masked TINs, an entity map that joins
  four TINs, tin and entity grain × {all, MO, KS+pt, cpt list, one payer}, Overview × {all, MO, KS, TX}:
  14/14 IDENTICAL. `_Rounder` key collisions (1 vs 1.0, -0.0 vs 0.0) cannot change a printed value
  here: GAP is `med - rate`, never -0.0.
- **4.1 quartile** (`scripts/s12_quartile.py`, brute force over tied markets): the quartile always
  equals the printed percentile's band. It differs from the printed P25/median/P75 band only when
  the value sits ON a tied boundary, which is the intended decision. No case puts a value strictly
  below the printed P25 in Q2+.
- **5.2.4 re-read** (`scripts/s9_reread_codes.py`): reads the kept copy and replaces the part
  atomically (10 + 10 rows, not doubled). The analytics follow (spine 97110 = 10, not stale). A file
  with no copy and no link gets its sentence. Removing then re-adding a code re-reads nothing that
  already has it, and a file read in between is offered.
- **5.2.7 orderings:** list done before the read (refs complete), during (epoch compare), after
  (requeue_skipped), and while the rate file is still DOWNLOADING (no difference: the epoch snapshot is
  taken at `ingest_file` start, after the download). Read-only check: the epoch is per process, so a
  CLI ingest next to `serve` is not covered (edge).
- **Zip / multi-member gzip / truncated fingerprints:** the zip content is the member the parser reads
  (`open_stream`'s largest json), so equal content means equal loaded data. Multi-member gzip hashes
  every member. A truncated gzip hashes the partial output, but that file fails its parse, so it can
  never cause a false skip.
- **Overview / outreach / medicaid ORDER BYs added this round** all end on a unique key. No lock held
  across part I/O in `_part_sets_current` (scans unlocked, applies under a 3 s try-lock).
