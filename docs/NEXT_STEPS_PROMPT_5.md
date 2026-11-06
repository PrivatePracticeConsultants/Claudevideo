# Prompt for the next session, round 5 — install robustness, idle cost, and what round 4 cracked open (written for Claude Opus 5.5)

Rounds 1–4 are `docs/NEXT_STEPS_PROMPT.md`, `_2.md`, `_3.md`, `_4.md`; each has
a status block of what it closed. This memo came out of a verification review
on Nov 6, 2026 that re-ran the earlier rounds' claims and then probed the
program from angles the earlier memos did not take: how it is installed and
updated on the user's PC, what it costs when nothing is happening, what the
Explorer actually opens into, how it behaves as the number of files grows,
and whether the database engine does what the code assumes. Every item below
carries the measurement or reproduction that found it; nothing is listed on
suspicion. Copy everything below the line into a fresh session on this repo.

---

You are picking up MRF Explorer (`mrfx/`). Read `CLAUDE.md`, then
`docs/AI_HANDOFF.md` in full (Steps 3, 5 and 8), then `git log aeced33..HEAD`
(rounds 3 and 4: modifier rules, the query-layer fixes, UTC, the stored
"latest" flag, the determinism fixes). The product: one non-technical user on
a Windows PC pastes payer links, then **searches by NPI to see what each payer
reimburses for PT/OT/SLP CPT codes**. The numbers are sold. Correctness of the
rates beats speed; "should work" is not done; every claim needs a live number.

## What the Nov 6 review verified as closed (do not redo)

- The full suite passes: **812 tests** (813 with the queue-order test), ruff
  clean, `pip-audit -r requirements.txt`: no known vulnerabilities (re-run on
  Nov 6).
- Round 4's numbers were re-checked the hard way: every dashboard endpoint's
  response body, old code vs new, on the real 32M-rate store and on a 99M-rate
  three-month store — 117/125 and 56/59 byte-identical, every difference a
  thread-order tie in the OLD code (listed in the round-4 status block). The
  stored `is_latest` flag equals the old per-request window on all 79,027,749
  spine rows after migration, after a payer delta, and after a full rebuild.
- Rounds 1–3 items hold under the suites that pin them: the fast parser's
  identity (`test_fastscan*`, `mrfx fastcheck`), known answers for modifiers,
  months, entity grain, practice basis, Medicare locality, SSN masking,
  export == screen, paused downloads, UTC timestamps. No regression in any.
- Carried, still open, unchanged since round 4: **nothing has ever been run on
  Windows by an agent**; P2.1 (COPY … PARTITION_BY rebuild, only needed before
  a spine bump); P2.2 (sorted parts); `.7z`/`.zst` (new packages); the
  download retry "as a browser" (owner decision, untouched); semi-joins for the
  state/city/therapy/name filters; leverage/dossier ZIP placement from raw rows.

## Non-negotiables (same as every round)

1. Numbers do not move: snapshot every endpoint before and after
   (`scratchpad/perf/snapshot.py` + `diff_snap.py`, both still there), run the
   known-answer suites and `test_determinism.py`.
2. No ROLLUP_SCHEMA_VERSION bump (an hour at 300M rates). Round 4 showed a
   column can be added in place in seconds; prefer that, side tables, or
   query-time changes.
3. Measure on a NAMED store (`scratchpad/perf/named_*`) and on the 99M store
   (`scratchpad/scale4n`; rebuild with `scratchpad/scale4/build.py` if gone),
   idle AND under ingest (`scratchpad/perf/load_test.py`).
4. Read plans (`EXPLAIN ANALYZE`), not code; leave a regression test per fix;
   commit what + why + before/after; push only to the branch you are told;
   never weaken TLS.

## Priority 0 — the install path (the user hit this on Nov 6)

**P0.1 The launchers and the updater depend on the editable install's absolute
path.** The user's `Start MRF Explorer (auto-restart).bat` ended in
`ModuleNotFoundError: No module named 'mrfx'` from
`E:\mrfx_project 11.5.26\Claudevideo\.venv\Scripts\mrfx.exe\__main__.py`.
Cause, reproduced here: `pip install -e .` writes a finder into the venv
(`__editable___bcbs_mo_mrf_0_1_0_finder.py`, `MAPPING = {'mrfx': '<absolute
path>/mrfx', …}`), so the `mrfx` console command imports the package from the
folder it was installed in. Rename or move the folder (the user's now carries a
date) and `mrfx` finds nothing, while `python -m mrfx` run from the folder
works (verified: from a copied tree with no venv, `python -m mrfx --help` runs
and `mrfx.__file__` is the copy's; from any other cwd the import resolves to
the install path). Both `.bat` files run the console command; the supervisor's
child already uses `[sys.executable, "-m", "mrfx", …]` (cli.py:1738), so only
the first hop breaks. Second finding: the updater's repair step
(`pip install -e .`) needs PyPI every time — the venv has no `setuptools`
(`import setuptools` → ModuleNotFoundError) and build isolation downloads it;
offline it fails with "Failed to build … when installing build dependencies"
(verified in a fresh venv with `--no-index`). So on a PC without PyPI reach the
update leaves a stale mapping and the next start fails as above.
Do:
- Both `.bat` files call `".venv\Scripts\python.exe" -m mrfx …` (serve
  `--supervise`, update), after `cd /d "%~dp0"`; print a plain sentence if
  `.venv\Scripts\python.exe` is missing ("the program's environment is not
  here — see HOW_TO_RUN Step 4").
- `mrfx update`: after writing files, run `python -m pip install
  --no-build-isolation -e .` with `setuptools` pinned in `requirements.txt`
  (so no download is needed), and VERIFY with `python -c "import mrfx,
  pathlib; assert pathlib.Path(mrfx.__file__).parent == root/'mrfx'"`; say in
  plain words if it could not be repaired and that the Start file still works.
- `mrfx serve` at startup: if `mrfx.__file__` is not under the folder it runs
  from, log one line (a stale install mapping would otherwise run OLD code
  silently after a copy-to-new-folder update).
- HOW_TO_RUN Part 5: one sentence that renaming/moving the folder is fine from
  this version on.
- Tests: the `.bat` files reference no `mrfx` console script (string test); a
  subprocess test that `python -m mrfx --help` works from a copied tree with
  the real venv while `PYTHONPATH` is empty. Windows cannot be run here; say so.

**P0.2 (found in this review and FIXED in the same commit as this memo —
verify, do not redo).** Round 4's `/api/stats` name-progress count rebuilt a
whole-store NPI table **under the single writer lock** whenever a part changed:
80 s at 99M rates, during which every file finishing had to wait for its
bookkeeping (`finalize_rates_part`, `upsert_file`, queue status writes). Now
kept per part (`rate_npi_set`/`rate_npi_parts`): a new 2.2M-row part costs
1.3 s with 0 ms of writer wait; the first build after the update scans every
part outside the lock (85 s at 99M, longest writer wait 1.7 s for the one
apply); steady poll 0.76 s (`count(DISTINCT npi)` over 17.9M kept rows).
Verify under a live grind that no writer waits; if the steady 0.76 s matters
at 300M, keep a per-generation distinct count instead of recounting.

## Priority 1 — measured costs that hinder a long grind

**P1.1 Idle polling: 7% of a core and ~30 DuckDB queries per second with an
empty queue.** `mrfx serve` with no browser open, named store, 60 s: 422
jiffies = 7.0% of one core, 24 threads. `py-spy` shows where: the 6 downloader
threads each run `url_queue_counts()` + `next_queued_url()` every 0.5 s and the
3 processor threads `next_fetched_url()` every 0.5 s (`fetch.py` `downloader`
/ `processor` loops); every claim takes `write_lock` and opens a connection.
That is 30 queries/s and 18 lock acquisitions/s doing nothing, for weeks, on
the user's PC. Do: a `threading.Condition` on the store that `enqueue_url(s)`,
`requeue_failed`, `wake_paused_urls`, `set_url_status_by_id`,
`force_size_requeue`, `pause_url` and the fetch→fetched transition notify;
workers wait on it with a long fallback (30 s), and `url_queue_counts()` only
when a claim is about to be made. Prove: idle CPU before/after (the same 60-s
measurement), and the under-load numbers from round 4 unchanged or better.

**P1.2 Pin down the DuckDB 1.5.5 transaction-visibility behaviour the store now
depends on avoiding.** Round 4 found that an `UPDATE … FROM (SELECT … FROM t)`
issued after the slice `INSERT`s in the same transaction marked **0 of 79M
rows** (`store.py`, comment above `SPINE_QUERY`; pinned by
`test_flag_is_exact_through_sliced_builds`). A 16-variant matrix on Nov 6
(explicit transaction × ORDER BY × empty-CTAS × preserve_insertion_order) could
NOT reproduce any row loss for plain `INSERT … SELECT` + `SELECT count(*)`;
so the trigger is narrower than "insert then read". Do: a minimal standalone
repro of the UPDATE case (CTAS WHERE FALSE, slice INSERTs, UPDATE … FROM a
subquery over the same table, one transaction); grep the store for any other
statement that reads a table it appended to in the same transaction
(`_apply_rollup_delta_sliced`, `refresh_directory_incremental`,
`_migrate_latest_flag`); run the store's own suite on `duckdb==1.5.6` (PyPI
has 1.5.6 and 2.0.0; 2.0.0 is a major — do not jump blind) and report whether
the behaviour changed. If it reproduces on 1.5.6, file it upstream with the
repro. This is engine behaviour under the numbers; it deserves an answer, not
a workaround note.

**P1.3 The Explorer opens in POOLED mode, and that is its slowest strip.** The
Explorer tab sends no `latest`; its month select defaults to "all months"
(`index.html` `#f-month`, `app.js` `filterQuery`), per the round-2 month
contract. Measured at 99M rates (79M spine rows), cold: pooled table 1.45 s +
pooled summary strip 9.1 s; the strip's warm split is quantile_cont over 79M
values 3.3 s + the by-code per-practice medians 4.0 s (the two
`count(DISTINCT)`s are 0.3 s). On the real 32M store it is 0.38 s. At a real
300M store (15–20M spine rows) expect ~2 s. Round 4's table labelled the
`latest` view "default"; that is true of the Code page, not the Explorer.
Decide, do not drift: either (a) the Explorer defaults to "latest" like every
report tab (a product change; the month contract in AI_HANDOFF and the
methodology text must follow), or (b) keep pooled and make its strip
per-generation: the unfiltered strip is the same answer until data lands, and
the summary cache already keys on generation — the miss is only on the first
look after each file. State the choice and the numbers.

**P1.4 Carried with numbers: filters on joined columns.** At 99M rates the
state filter costs 2.9 s and a name search 3.9 s (0.23 / 0.25 s on the real
store) because the TIN relation still LEFT JOINs three dimension tables over
every spine row before filtering. The semi-join rewrite (filter `tin_value` by
a keyed lookup on `tin_directory` first) must reproduce the
`coalesce(td.…, nn.…)` fallbacks per column exactly — prove with the
round-4 relation dumps (`scratchpad/perf/rel_dump.py` + `rel_cmp.py`).

## Priority 2 — known costs, decide or document

- Leverage 3.8 s / dossier 5.2 s on the real store: both place every
  practice by its NPIs' ZIPs from the raw rows each call. A TIN→ZIP side
  table (the same per-(TIN, ZIP) counts `mrfx/sqlfn.py` computes, maintained
  with the directory) makes them directory lookups. Numbers must not move
  (`sqlfn` tie rule: lowest ZIP).
- Exact quantiles hold every value in memory (round 2's 11.2 GB finding, now
  bounded by the single list-form call). At 79M pooled rows the strip is
  3.3 s of quantile; if (b) in P1.3 is chosen this is what the per-generation
  cache hides.
- `pytest -m quick` is drifting: `tests/slow_tests.txt` (411 entries) predates
  the round-4 tests (`test_latest_flag.py` sliced build ≈ 17 s is not listed);
  regenerate the list the way round 3 did and state the new quick time.

## Priority 3 — small, verified

- Code-page histogram: the y-axis labels are clipped at the left edge
  (screenshot in the Nov 6 session: "0,000" for 10,000) — `left = 40` px with
  `text-anchor:end` at x = 34; widen the gutter to the label width.
- Test-only dependency drift: `starlette.testclient` in the pinned 1.6.0
  imports `httpx2` and falls back to `httpx` with a deprecation warning (seen
  on every suite run). Before any starlette bump, add `httpx2` to the test
  requirements or the suite stops importing.

## Checked in this review and found fine — do not re-chase

- Third-party strings in the dashboard: every `innerHTML` template escapes
  payer / practice / URL / error text (`esc(...)`); the only raw `href`s are
  the user's own saved website URLs.
- Raw downloads are deleted after ingest by default
  (`delete_raw_after_ingest`); `data/processed/` holds only inbox drops.
- `/api/urls` on a 20,000-link queue: 0.10 s, 0.65 MB, 1,506 rows (the capped
  slices), polled every 4 s on the Files tab — fine on localhost.
- Number of part files: the real store's 32M rows re-cut into 6,352 files
  (from 10): metadata count 0.01 → 0.29 s, exact NPI lookup 0.04 → 0.39 s,
  full DISTINCT 0.19 → 0.63 s — ~50 µs per file per raw query; a 300M store
  has a few hundred parts. Not a bottleneck; sorted/compacted parts (P2.2)
  remain a row-group pruning question, not a file-count one.
- The supervisor backs off between restarts (no boot hot-loop).
- `pip-audit` clean; all requirements pinned with Windows wheels (3.11–3.14).

## Report

End with: (1) the launcher/updater proof (the moved-folder subprocess test,
and what `mrfx update` now does offline); (2) idle CPU and queries/s before
and after P1.1, plus the round-4 under-load table re-run; (3) the DuckDB repro
result on 1.5.5 and 1.5.6, with the exact statement shapes that do and do not
reproduce; (4) the Explorer default decision with the strip timings on both
stores; (5) byte-for-byte snapshot diffs (expected: empty) for every endpoint
touched; (6) what you left and why; (7) what you could not measure — say so
rather than rounding up. Windows is still unmeasured; say that too.
