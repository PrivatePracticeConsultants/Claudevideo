# Prompt for the next session, round 2 (written for Claude Opus 5.5)

`docs/NEXT_STEPS_PROMPT.md` was the round-1 plan; its status block says what
round 1 closed. This is the round-2 plan. Copy everything below the line into
a fresh session on this repo.

> **STATUS (round 2 executed, Nov 2026).** What it closed, with numbers —
> details in the commits and AI_HANDOFF Step 8:
> - P0.1 `tests/mrfx/test_known_answer.py`: hand-computed files through ingest
>   -> rollups -> API -> CSV/ZIP. Found + fixed: the Code comparison page pooled
>   months ($40 May + $42 June shown as "$41", NPIs counted per month) — now
>   newest file per contract, and its export matches; exports were not
>   reproducible (any_value source file) — min() everywhere.
> - P0.2 independent Arrow+Python recount of 12 real files / 32M rows:
>   1,411,001 TIN groups, 0 differences; NPI rows 23.4M == 23.4M.
> - P0.3 month contract written down and pinned (explorer = all months with a
>   Month column; code view/reports = newest per contract).
> - P0.4/P0.5 export == screen at all grains; SSN-pattern TINs masked in every
>   displayed field and exported file (`unit_id`/`website_tins` stay raw as
>   machine keys, by design).
> - P1 on a synthetic 306M-row store: NPI search went from out-of-memory to
>   0.6-1.2 s; removals 1,441 s -> 165 s (scoped directory); summary 11.2 ->
>   5.2 GB; big-store guard for NPI/entity views; RAM-aware auto workers. Full
>   rebuild still 65 min (C1, design in Step 8). P1.5 (follow-up): QA runs in
>   the parse worker — main-process peak RSS 2.88 -> 1.35 GB, throughput
>   unchanged, QA output identical on 12 real files.
> - P2 Windows: NOT RUN — no Windows machine. Code review only (cleanup in a
>   `finally` could raise on an AV lock — fixed + test). HOW_TO_RUN.md Part 4 is
>   the user's first-run checklist.
> - P3: CLI NPPES progress + Ctrl-C (done), fair retry scheduling (done),
>   expired-link message names its index (done), cancel in-flight parse (done in a follow-up).
> - P4: AI_HANDOFF Step 8 reconciled; `pytest -m quick` exists. Follow-up
>   clean-up guided by full-suite coverage (fastscan 98%, parser 96%) + vulture:
>   only provably-unused code removed and the 6x-pasted JSON-string regex
>   factored (compiled patterns asserted byte-identical); parser output
>   fingerprints identical on 3 real files x both paths; fastcheck IDENTICAL.
>   The remaining uncovered lines are defensive branches — kept. The two
>   progress mechanisms (chunk tracker vs single-read percent) both serve
>   live paths with different UI semantics — kept.

---

You are picking up MRF Explorer (`mrfx/`). Read `CLAUDE.md`, then
`docs/AI_HANDOFF.md` in full, then the status block at the top of
`docs/NEXT_STEPS_PROMPT.md`, then `git log 77fb895..HEAD` (17 commits: a
rewrite of the ingest hot path, a correctness audit of it, and the fixes
from that audit). The product is one non-technical user on a Windows PC who
pastes payer links and then **searches by NPI to see what each payer
reimburses for PT/OT/SLP CPT codes**. The numbers are sold. Correctness of
the rates beats speed every time; "probably fine" is not done.

## Where things stand (so you don't redo it)

INGEST IS VERIFIED. The fast parser (`fastscan.py` + `parser.py`) produces
row sets identical to the pre-rewrite code on 22 real payer files from 8
generators (BCBS WY/MS/ND, Florida Blue, UHC, Moda, Aetna, Tufts, Security
Health, Highmark), up to 42 GB uncompressed and 590M rows, compared as
multisets with identical QA counters; it is 1.1×–10× faster; two SIGKILL
trials recovered exactly; the suite is 645 tests, mutation-tested; ruff and
pip-audit are clean. Do not re-audit the parser unless a new file shape
breaks it (then `mrfx fastcheck <file>` is the tool, and
`tests/mrfx/test_fastscan.py` the place for the regression test).

THREE THINGS CHANGED THE RISK PICTURE:
1. Nothing in this repo has ever been run on Windows by an agent. The user
   runs Windows.
2. Ingest is now fast enough that the user WILL build stores of 300M–1B
   rows (one Aetna file is 590M). The store, rollup, dashboard and export
   code has only been exercised at a fraction of that.
3. The last two sessions verified that rows reach the Parquet parts
   correctly. Nobody has independently verified that the **answer the
   dashboard gives for an NPI** is the right answer — that the dedup grain,
   the latest-month logic, TIN-only rows, percentage rows, modifiers,
   duplicate files across TOCs and the rollups all compose into the number
   the user reads off the screen.

## Non-negotiables

1. Every claim needs a live run with numbers; real payer files where the
   claim is about real files. Report what you could not verify.
2. Any change under `mrfx/` that touches numbers (store queries, rollups,
   API, export, benchmark) must keep `tests/mrfx/test_determinism.py` green
   and must come with a known-answer test: a tiny hand-built input whose
   correct output you computed by hand, asserted end to end.
3. New safeguards get mutation-tested (break it on purpose; show the test
   that fails). If none fails, write the test first.
4. Fix the class, not the instance; leave a regression test. Commit style:
   what + why + verified-with-numbers. Push only to your task's branch.
   Trials live in the scratchpad with their own config, never pointed at a
   real store.
5. Honesty contract (CLAUDE.md invariant 4): never fabricate, never silently
   drop; SSN-pattern TINs masked everywhere; exports carry the methodology
   sidecar; 0-row files stay 0.

## Priority 0 — the answer layer: is the NPI number right?

This is the product. Audit it as an independent checker, not as the author.

**P0.1 Known-answer, end to end.** Build ONE small in-network file (hand
written, ~20 items) that exercises every rule in AI_HANDOFF Step 3 at once:
the same NPI under two TINs; a TIN-only group; a `tin.type == "npi"` group;
`[GP,59]` and `[59,GP]` for the same rate; a percentage row (`negotiated_type:
percentage`) with the same numeric value as a dollar row; a bundle item; two
`file_month`s for the same NPI/code with different rates; a byte-identical
twin file under a second name; a companion-referenced group. Work out by
hand what `/api/rates`, `/api/entity/npi/<npi>`, `/api/code/<code>`, the
CSV export and the ZIP export must say for that NPI (rate per code per
payer, latest month, median across TINs, modifier breakout). Ingest it
through the REAL path (`ingest_file` → rollups → API via the FastAPI test
client) and assert every number. Put it in `tests/mrfx/test_known_answer.py`.
If any number disagrees with your hand calculation, do not adjust the
expectation to match the code — decide which is right from the invariants
and fix the code.

**P0.2 Rollups vs raw spine on REAL data.** Ingest 3–4 real files (the
scratchpad still has them: `scratchpad/real*/`; the Wyoming and Mississippi
BCBS files, the small UHC one, the Security Health zip) into a fresh store.
Then, with your own DuckDB SQL written from the invariants (not copied from
`store.py`), compute for 50 random NPIs and 10 random TINs: the per-code
rate set, the latest-month rate, the TIN median. Compare with what
`/api/entity/...`, `rates_dedup` and `rates_by_tin` return. Every
disagreement is either a bug or an undocumented rule; resolve each.

**P0.3 Month logic.** `file_month` = header → filename → ingest month. Check
what the dashboard shows when the same payer has files from two months and
the NPI's rate CHANGED: is the default the latest month, is the older one
reachable, and does the trend endpoint (`/api/trend`) show both? Then the
nasty case: a payer re-publishes an UNCHANGED file next month under a new
name (same content-sha). Content dedup skips it — so the "latest month" for
that payer is the OLD month even though the rate is current. Decide what
the honest display is and pin it.

**P0.4 Export = screen.** For the same filter, the CSV, the ZIP's CSV, the
API JSON and the on-screen table must agree row for row, and the
methodology sidecar must describe the filter that was actually applied
(grain, months, `is_dollar_rate`, therapy-only toggle). Diff them
programmatically.

**P0.5 Masking and leakage.** Grep every output path (API JSON, CSV, ZIP,
dossier/market/org-bundle reports, logs) for a TIN that matches the SSN
pattern after ingesting a file that contains one. `mask_tin` exists; prove
nothing bypasses it (rollup tables, `source_files`, error messages, the
Files tab's QA JSON).

## Priority 1 — scale: the store the user will actually have

Build a synthetic store of ≥300M rows in the scratchpad (generate Parquet
parts directly with the real `RATES_SCHEMA`; ~15 payers, 200k TINs, 2M
NPIs, 3 months) and measure, with peak RSS and wall-clock:

- **P1.1 `update_rollups_incremental`** after adding one 5M-row file. The
  ghost-prune anti-join (`store.py` ~L2727, `DELETE … WHERE NOT EXISTS
  (SELECT 1 FROM rates …)`) scans the whole store on every incremental
  update; the round-1 audit believed it is redundant on that path (replaced
  or shrunk parts switch to the full-directory branch). Confirm against the
  tests, then scope or remove it. Also the directory slice's `tin_value IN
  __delta_tins` cannot prune Parquet row groups (parts aren't sorted by
  TIN) — measure, then decide: sort parts by (payer, tin_value) at write
  time (cheap, one sort per part in `RatesPartWriter`) so the statistics
  prune, or keep as is with numbers that justify it.
- **P1.2 Full rebuild** (`rebuild_rollups`) and **`forget`** at that size:
  time, peak RSS, spill volume; AI_HANDOFF Step 8 calls these "hours" at
  300M rows — measure it and either accept it with a progress indicator the
  user can see, or scope `forget` as Step 8 designs.
- **P1.3 Dashboard latency at scale**: `/api/summary`, `/api/rates` with a
  payer+code filter, `/api/entity/npi/<x>`, the Files tab, while an ingest
  is running in the background (reader parallelism under the global SET
  threads). Anything over ~2 s on a cold cache needs a reason or a fix.
- **P1.4 Memory ceiling**: `duckdb_memory_gb` default + 5 parser workers
  (each up to ~2 GB on a reference-heavy payer) + the OS, on a 16 GB and a
  32 GB box. Write down the budget; make the auto `parallel_ingests` respect
  RAM, not just cores (round 1 measured 1.2–1.5 cores per worker and a
  throughput plateau at ~cores/1.4 — so fewer workers is free).
- **P1.5 Per-file QA** (`ingest._qa_aggregates`): three scans of the new
  part on the processor thread, ~5 s per 13.5M rows, so ~4 min on the Aetna
  file while that worker slot idles. Fold into one scan where exact; move
  it into the worker (private in-memory DuckDB, store untouched — invariant 2
  holds).

## Priority 2 — Windows

Everything so far is Linux. If you have no Windows runner, say so plainly
and write the checklist for the user instead; do not guess.

- Clean venv on Python 3.11 and 3.14: `pip install -r requirements.txt`,
  `pip install -e .`, full suite, `ruff`, `pip-audit`.
- `mrfx serve`, paste one real TOC, let 10+ files drain including one
  ≥1.5 GB uncompressed; watch Task Manager; then `mrfx verify`.
- The new threads vs Windows file semantics: `BackgroundParquetWriter`
  close before `os.replace`; `.spool.tmp` deletion while Defender has it
  open (`unlink_with_retry` exists — is it used for the spool?);
  `_pid_alive` for the sweep (the Linux zombie fix reads `/proc`, which
  doesn't exist on Windows — confirm the fallback is "alive = keep");
  console encoding of the new log lines (em dashes, arrows).
- Force-kill the console at each stage (download, scan, spool replay,
  Parquet write, rollup); restart; `data/store/rates/` holds only real
  parts; `data/downloads/` has no stray `.part` for a done row.
- The `.bat` launcher's auto-restart loop with the new exit paths.

## Priority 3 — operations and UX of a long grind

- **P3.1 `mrfx add` without the dashboard** runs NPPES API lookups after
  the drain (one request per new NPI); on a few million rows that is >15
  min after "finished:" with only "looking up practice names (NPPES)..."
  printed. Show how many remain, let Ctrl-C skip it cleanly, and point at
  `enrichment.mode: bulk`.
- **P3.2 Fair scheduling** (Step 8): `_claim_next` is `ORDER BY id`, so a
  cluster of flaky low-id links is retried before fresh files. Measure on a
  queue with 3 dead links + 10 good ones; deprioritize rows that have burned
  connections.
- **P3.3 Signed-URL expiry**: a TOC's children 403 after a long pause;
  there is no auto re-expand. Build it, or make the Files tab's message say
  exactly which TOC to re-paste.
- **P3.4 Cancel in-flight**: a wrong 22 GB paste must run its course. The
  download loop already checks `cancel_check`; wire a cancel for the PARSE
  (the worker's progress callback is the natural hook — it already raises
  SystemExit when the parent dies).
- **P3.5 Timing table honesty**: GETTING_STARTED now has a what-to-expect
  table measured on a 4-core Linux box. Re-measure on the user's class of
  machine (or the Windows runner) and replace the numbers; a wrong table is
  worse than none.

## Priority 4 — code health and docs

- `mrfx/fastscan.py` is 939 lines and `parser.py` 1,209 after two rewrites.
  Run `/simplify`-style review on them: dead helpers (`_feed`'s peek path is
  only reachable for chunk-straddling members now; `_Member.peek` limits),
  duplicated whitespace/key regexes, the two progress mechanisms in
  `ingest.py` (`_ProgressTracker` chunks vs `SingleReadProgress` percent).
  Only remove what a test proves unreachable; keep behaviour identical
  (`fastcheck` on 3 real files before and after).
- AI_HANDOFF Step 8 still carries entries round 1 partly addressed
  ("Provider-reference memory amplification": strings now shared, but the
  per-file `load_provider_refs` pickle is still per worker). Reconcile each
  bullet with the code; delete what's fixed, sharpen what remains.
- The test suite is 9–15 min. Mark the real-fixture and kill-trial-style
  tests so a `-m quick` run under 3 min exists for the inner loop, without
  dropping anything from the default run.
- Add an Aetna-generator fixture if you can make one under 2 MB: its PT
  items cite thousands of references each; a slice that keeps ONE item and
  ~50 of its references, member bytes verbatim and anonymized with the
  scratchpad's `build_fixtures.anonymize`, would do.

## Deliberately out of scope (don't spend time unless asked)

Remote `location` reference fetching (no payer seen using it); a compiled
scanner (measured <2×); the segmented-download re-hash (opt-in, ~1 min per
20 GB); the legacy `src/` tool.

## How to report

End with: (1) the known-answer file and every number it pins; (2) each
rollup-vs-raw disagreement found on real data, with the resolution;
(3) the scale table (rows, operation, wall-clock, peak RSS) before/after;
(4) the Windows results or the explicit statement that none were run;
(5) every bug, with repro and test; (6) what you left alone and why; (7) what
you could not verify — say so plainly rather than rounding up to "done".
