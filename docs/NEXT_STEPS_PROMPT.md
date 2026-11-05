# Prompt for the next session (written for Claude Opus 5.5)

Copy everything below the line into a fresh session on this repo.

---

You are picking up MRF Explorer (`mrfx/`). Read `CLAUDE.md`, then
`docs/AI_HANDOFF.md` in full (Steps 3, 5 and 8 especially), then
`git log 77fb895..HEAD` — the last ten commits are a performance rewrite of
the ingest hot path plus a correctness audit of it. Your job is to make sure
that work is right, finish what it left open, and find what it missed. The
product is one non-technical user on a Windows PC; the numbers it reports are
sold, so **correctness of the rates beats speed every time**, and a change
that is "probably fine" is not done.

## What the last session did (so you don't redo it)

- `mrfx/fastscan.py` — a numpy byte-level splitter: finds top-level
  `in_network[]` / `provider_references[]` member boundaries (string- and
  escape-aware), decides per 8 MB chunk which members to skip in BULK, and
  hands only kept members to `json.loads` → the same `_handle_item`/`_add_ref`
  as the classic ijson path. Decompression runs in a prefetch thread.
- `mrfx/parser.py` — `parse_fast`, `skim_needed_ref_ids_fast`,
  `run_big_file_fast` (big files in ONE read: target items spooled verbatim to
  `.{key}.{pid}.spool.tmp`, references after `in_network` kept in-stream,
  references before it fetched by a second read that stops when the reference
  table closes), columnar `RowBatch` sink batches, `clean_digits` fast path.
- `mrfx/store.py` — `BackgroundParquetWriter`, Arrow-table inserts for
  `save_provider_refs` / `save_plan_index`, orphan sweep knows `.spool.tmp`.
- `mrfx/fetch.py` — content hash carried across resumes (no `.part` re-read),
  gzip-encoded bodies stored as sent (`_body_chunks`), segmented downloads
  give up when a server compresses each byte range.
- `mrfx/sniff.py` — uncompressed-size estimate = max(18×, size the gzip
  trailer / zip directory records), compression detected by magic bytes.
- `mrfx/fastcheck.py` + `mrfx fastcheck <files>` — runs a file through BOTH
  parsers and compares row multisets (digest first, EXCEPT ALL on mismatch).
- Tests: `tests/mrfx/test_fastscan.py` (84), `tests/mrfx/test_fastscan_e2e.py`
  (12), five anonymized real-vendor slices in `tests/mrfx/fixtures/real/` with
  golden counts. Safeguards were mutation-tested (each one deliberately broken
  must fail a test). Full suite: 620 passed. ruff: clean.
- Verified identical to the pre-change code (DuckDB `EXCEPT ALL` both ways +
  identical QA counters) on 21 real files from 8 sources (BCBS WY/MS/ND,
  Florida Blue, UHC, Moda, Tufts, Security Health; .gz and .zip; up to 42.4 GB
  uncompressed, up to 13.5M rows), 1.1×–10× faster; 42.4 GB file 61 min → 7.5
  min at 0.34 GB peak RSS. A 2.7 GB byte-exact slice of a 20 GB Aetna file
  (60,041,589 rows) also identical. The FULL Aetna file was only run on the
  new code (590,311,124 rows, 1,628 s, peak RSS 2.18 GB) — the original code
  never finished it in the time available.

## Non-negotiables for this session

1. Every claim needs a live run with numbers. Measure before and after; on
   real payer files where the claim is about real files.
2. Any change touching `fastscan.py` / `parser.py` must keep the fast path
   identical to the classic path. Prove it with `mrfx fastcheck` on at least
   three real files of different generators AND with the existing tests at
   every chunk size they use (1, 2, 3, 7, 64, 4096, 1 MiB).
3. When you add a safeguard, mutation-test it: break it on purpose and show
   which test fails. If none fails, write the test first.
4. Fix the class, not the instance; leave a regression test.
5. Commit style: what + why + verified-with-numbers. Push only to the branch
   named in your task. Trials live in the scratchpad, never pointed at a real
   store.
6. Honesty contract: never fabricate rows, never silently drop them. A
   "faster" path that loses a rate under any valid JSON layout is a bug, not a
   trade-off. The ONE documented, deliberate difference from the classic
   parser (corrupt bytes inside a NON-target item no longer fail the whole
   file) is pinned by `test_corruption_inside_a_non_target_item_keeps_the_target_rows`.

## Priority 0 — known gaps the last session found but did not fix

Do these first; each is confirmed, none is speculative.

**P0.1 All-codes mode spools the whole file to disk.** In
`run_big_file_fast`, every `in_network` member is a "target" when
`cfg.code_set is None` (`codes.all_codes: true`), so the spool
(`.{key}.{pid}.spool.tmp`) becomes the ENTIRE uncompressed `in_network`
section — a 200 GB-uncompressed all-codes file writes ~200 GB of scratch next
to the store. The classic two-pass never touched disk. Options, in order of
preference: (a) when `code_set is None`, don't spool — process items inline
during the single read and only use the spool for the refs-before case, or
decline to the classic path for all-codes big files; (b) spool gzip-level-1
compressed and check free disk against the estimate up front, failing the
file honestly with a plain-language message instead of filling the drive.
Whatever you choose, add a test that proves a big all-codes file never writes
a spool larger than X, and that disk exhaustion fails ONLY that file.

**P0.2 Progress bar sits at ~50 % for most big files.** The big-file progress
tracker is sized for two passes (`_ProgressTracker(passes=2)`,
`chunks_total = file_chunks * 2` in `_parse_worker`), but the single-read path
usually does one pass (pass 2 only when references precede `in_network`, and
it stops early). Result: the dashboard shows ~50 % for the whole run, then
jumps to 100 % at `finish()`. Fix the sizing (one pass + an estimated short
second pass, or re-size when pass 2 is known to be skipped) so the bar is
honest. Cosmetic, but the user watches this bar for hours.

**P0.3 Stale README claim.** `README.md` "Extraction guarantees" still says
`location`-style remote reference files are fetched lazily. That describes
the retired `src/` extractor; `mrfx` does NOT fetch them (see
`parser.py::_add_ref`, the "file-driven app" comment) and counts the affected
groups as `ref_groups_skipped`. Correct the README, and decide whether to
build remote-reference fetching (see P2.1).

**P0.4 Full Aetna comparison never completed.** Finish an original-vs-new
comparison on the whole 20.4 GB Aetna file (1.54 GB gz, from the Moda index:
`2026-10-05_pl-3x-tr18_Aetna-Signature-Administrators.json.gz`). Run the
ORIGINAL code (`git worktree add <dir> 77fb895`) detached with `nohup`/`setsid`
so background-task time caps can't kill it; cache its Parquet; compare with
the digest method in `fastcheck._compare_row_sets` (EXCEPT ALL on 590M rows
will spill tens of GB — the previous session hit "No space left on device"
twice doing it the naive way). Report rows-only-in-each and QA equality.

## Priority 1 — correctness audits (angles the last review did not cover)

The last independent review found five real fast-vs-classic divergences in
unusual-but-valid JSON. Assume more exist. Angles not yet exercised:

- **UTF-16 / UTF-32 encoded files, and a BOM inside a zip member.**
  `open_stream` strips a UTF-8 BOM; what do both parsers do with UTF-16?
  Both should fail identically (ijson rejects it) — confirm, and make sure
  the fast path's "decline" vs "fail" choice matches (declining would re-run
  the classic parser needlessly on a multi-GB file).
- **Numbers at chunk edges in every position.** The `m.end() >= len(data)`
  guard covers a numeric code cut by a chunk. Check the same class for
  `provider_group_id` and for numeric `billing_code_type` (yes, payers emit
  numbers there) in the BULK path (`_ChunkKeys._string_values` /
  `refs_needing_a_look`), where a value can straddle the chunk end while its
  key is inside the chunk. Fuzz with chunk sizes that land inside numbers.
- **Whitespace/formatting extremes inside members.** Jackson pretty-printed
  files (Tufts) are tested; try `:` with newlines on both sides, tabs,
  `\r\n`, and >4 whitespace bytes between key and value — `_keys` skips at
  most 2 before `:` and 4 after and otherwise returns `vs = -1` (routes to
  the exact path — confirm that really happens and is counted correctly).
- **Duplicate `negotiated_rates` / nested `bundled_codes` with their own
  `provider_references`.** The skim collects ref ids from
  `negotiated_rates[].provider_references`; the classic `skim_needed_ref_ids`
  reads an ijson prefix. Prove both see exactly the same ids for a bundle
  item whose nested array also has `provider_references`.
- **`therapy_only_ingest: true` end to end** with a real NPPES cache: the
  parse-time drop lives in `_emit_group`, which was rewritten around
  `RowBatch`. Build the cache (`mrfx enrich`), ingest a real file both ways
  (`fast_scan` on/off), compare stores. Unit tests mock the cache; nothing
  has run the real one through the new code.
- **Worker-pool path under real concurrency.** `parallel_ingests: 3`, a
  queue of 6+ real URLs of mixed size, kill the server mid-parse (SIGKILL),
  restart, confirm: no duplicate rows, no orphan `.spool.tmp` / `.parquet.tmp`
  (note: a spool's pid is the PARENT's pid — if only the worker dies, the
  parent is alive and the sweep keeps the spool; check `ParsePoolManager`
  failure handling deletes it), progress rows sane, every file ends `done` or
  `failed` with a plain message.
- **`fastcheck` itself.** It compares fast vs classic. Make sure it also
  catches a difference in the SPOOL replay (e.g. plant a bug in
  `run_big_file_fast` that drops the last spooled item; the test
  `test_fastcheck_reports_identical_and_catches_a_planted_difference` only
  plants one in `_want_item`).
- **Header semantics.** `_set_header` fires for every `reporting_entity_name`
  seen; for a header BEFORE `in_network` in a file with >BATCH_ROWS rows,
  both paths now agree. For a header AFTER `in_network` the single-read path
  stamps the late header on all rows while the classic two-pass keeps the
  preflight seed on already-flushed rows (the review called this "fast is
  more correct"). Decide the contract, document it in AI_HANDOFF, and pin it.

## Priority 2 — performance angles still open

Measure with `scratchpad/bench`-style scripts; report s/GB uncompressed and
rows/s, plus peak RSS, on the same real files the last session used (listed
in AI_HANDOFF Step 5). Do NOT trust microbenchmarks alone — the last session
got a 2× in a microbenchmark that was 1.15× on the real file.

- **P2.1 Per-byte cost is now the floor (~9–10 s/GB on string-dense files).**
  It is numpy over every byte: `translate` + `nonzero` + parity `cumsum` +
  per-chunk key regex. The next step change needs a compiled kernel. Evaluate
  an OPTIONAL numba (or Cython/Rust via pyo3) scanner behind a config flag
  with the numpy path as the fallback, same tests, same `fastcheck` gate.
  Only ship it if it is ≥2× on real files AND the install story on the
  user's Windows Python 3.11–3.14 is clean (wheels exist, no compiler).
- **P2.2 Row production is the bound for Aetna-style books.** 590M rows for
  78 codes: `_emit_group` + `RowBatch.add` + `to_table` + Parquet. Profile
  it (`cProfile` on the Aetna slice). Ideas: emit per-group NPI tuples
  without the per-row Python lists (`np.repeat` of base indices);
  `pa.array` from numpy; dictionary-encode `tin_value`/`npi`; larger
  `BATCH_ROWS` for row-dense files; measure `PARQUET_COMPRESSION` zstd level
  vs snappy on write time AND the store's read time (the dashboard reads
  these parts constantly — don't trade read speed for write speed without
  measuring both).
- **P2.3 `parallel_ingests` auto-sizing with the new threads.** Each worker
  now has a prefetch thread and a Parquet-writer thread. On the user's 8-core
  box with 7 workers that is 21 busy threads. Measure total throughput
  (rows/s summed) at `parallel_ingests` 2, 4, 6, 8 on a queue of real files
  and re-derive the auto value; document in GETTING_STARTED.
- **P2.4 Per-worker memory × workers.** Aetna peaked at 2.18 GB in one
  process (its kept reference table: 131k groups with long NPI lists). × 8
  workers is 17 GB on a 32 GB box, plus DuckDB. Either bound the kept-refs
  structure (store PGroups as arrays, intern TIN strings) or make the pool
  admission memory-aware (don't start a ref-heavy big file while N others
  run). AI_HANDOFF Step 8 already lists the related
  `load_provider_refs` pickle amplification — fix both together.
- **P2.5 Per-file QA** (`ingest._qa_aggregates`): three serial DuckDB scans
  of the new part on the processor thread (~5 s per 13.5M rows, dominated by
  an exact 7-column DISTINCT). Fold into one scan where exact; consider
  `approx_count_distinct` for the duplicate ratio ONLY if the QA report
  labels it approximate (honesty contract).
- **P2.6 Rollup refresh scales with the store, not the file** (store.py
  ghost-prune anti-join, directory slice unprunable by TIN). Verify the
  ghost-prune claim from the audit against the tests, then scope it. Measure
  at book scale: build a 300M-row synthetic store and time one incremental
  refresh before/after.
- **P2.7 Segmented downloads re-hash the stitched file** (only with
  `download_segments > 1`). Trailing hasher thread over the completed
  prefix.
- **P2.8 Remote `location` provider references.** Not fetched today
  (P0.3). Decide: build a lazy fetch with the same resume/dedup/disk-guard
  discipline as `download()`, or document clearly in GETTING_STARTED which
  payers need their companion file dropped in by hand. Either way the Files
  tab must say which it is.

## Priority 3 — platform and operations

Everything above was measured on Linux. The user runs Windows.

- Run the full suite on Windows (3.11 and 3.14 at least). Watch for: the
  prefetch/writer threads vs `os.replace` of a file another thread holds;
  `.spool.tmp` deletion while an AV scanner has it open (`replace_with_retry`
  / `unlink_with_retry` exist — are they used on the spool?); `_pid_alive`
  for the spool sweep; console encoding of the new log lines.
- `pip install -r requirements.txt` on a clean Windows venv for 3.11–3.14:
  numpy 2.3.5 wheels exist for all four, but confirm, and run `pip-audit`
  (the gate must stay clean with numpy added).
- A full `mrfx serve` session: paste a real TOC, let the queue drain 10+
  files including one ≥1.5 GB, watch memory/CPU/disk in Task Manager, then
  `mrfx verify`. Report what the user would actually see.
- Disk hygiene: after a forced kill at every stage (download, pass 1, spool
  replay, Parquet write, rollup), restart and confirm nothing is left in
  `data/store/rates/` but real parts, and `data/downloads/` has no stray
  `.part` for a done row.

## Priority 4 — docs and hygiene

- `AI_HANDOFF.md` Step 5: fold the numbers from P0.4 and P2 in; remove
  anything that is now wrong (it still describes the ijson floor as 25 s/GB
  as if it were current).
- `GETTING_STARTED.md`: a short "what to expect" table — s/GB and rows/s on
  a typical PC — so the user can tell a slow file from a stuck one.
- `tests/mrfx/fixtures/real/`: add a slice from any NEW generator you touch
  (Anthem's `antm-pt-prod-dataz…` index was reachable but never sampled;
  Cigna/Aetna/Centene portals are JS-gated — see Step 8), using the
  anonymizer approach from the last session (same-length look-alike digits,
  placeholder business names, member bytes verbatim). Golden counts come
  from the CLASSIC parser.

## How to report

End with: (1) a table of every real file you ran, both paths, rows, time,
peak RSS, IDENTICAL/DIFFERENT; (2) every bug found, with a minimal repro and
the test that now pins it; (3) every speedup, before/after on real files;
(4) what you deliberately left alone and why; (5) anything you could not
verify — say so plainly rather than rounding up to "done".
