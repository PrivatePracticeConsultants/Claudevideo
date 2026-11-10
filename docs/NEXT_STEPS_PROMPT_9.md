# Prompt for the next session, round 9 — make 0.7.2 run right (written for Claude Opus 5.5)

> **STATUS: not started.** Replace this block with what you closed, the
> numbers, and what you left (with the reason), as rounds 7 and 8 did.

Rounds 1–8 are `docs/NEXT_STEPS_PROMPT.md`, `_2.md` … `_8.md`. This memo came
out of a review on Nov 10, 2026 at HEAD 64a371c (version 0.7.2), asked for
after the user's first start of 0.7.1 sat silent after the "analytics memory
budget" line and looked hung (a one-time stored-row update that ran every
rule over every file before the dashboard opened; 0.7.2 made it one scan).
The question was: does everything shipped in audit 8 (commits 926633a …
64a371c: the early duplicate check, waiting copies, size lanes, file-local
provider references, the row-rules 3 update, the outputs and dashboard
changes) actually run well? Method: the start-up path timed and its disk
reads counted per step on a 99M-rate synthetic store, a 32M-row real-data
store and a 2,000-file store; a 0.7.0 install updated to 0.7.2 with the old
build's own updater and started; and two independent line-by-line reviews
with fresh reproductions — downloads + ingest, and outputs + dashboard + API —
neither done by the author of the code under review. Every item below was
reproduced by a script that was run; the most severe findings of each
review (1.2, 1.3, 1.4, 4.1, 4.2, 4.3, 4.4) were re-run a second time by the
reviewer who wrote this memo, and the quoted timings marked "quiet" were
re-taken with nothing else running. Nothing is
listed on suspicion (suspicions are in their own section, marked). What was
checked and found fine is at the end so it is not re-chased.

**The reproductions are in the repo**: `docs/review9_repros/` — `startup/`,
`ingest/`, `outputs/` (see its README: they write only under
`/tmp/mrfx_review9`, never inside the repo; run each from its own folder as
`PYTHONPATH=/home/user/Claudevideo /home/user/Claudevideo/.venv/bin/python <script>`).
Each item names its script as `<folder>/<script>`. Run the script first, see the wrong result, fix,
see the right one — then turn it into the regression test.

Copy everything below the line into a fresh session on this repo.

---

You are picking up MRF Explorer (`mrfx/`). Read `CLAUDE.md`, then
`docs/AI_HANDOFF.md` in full (Step 3 and its "Round 7 additions" and "Audit 8
additions", Steps 4, 5 and 8), then `git log 556cbf4..HEAD` and
`docs/NEXT_STEPS_PROMPT_8.md`. The product: one non-technical user on a
Windows PC (the install is on an external `E:` drive, in a folder whose name
contains a space) pastes payer links, then looks up what each payer
reimburses for PT/OT/SLP codes, and SELLS the outputs. Correctness of the
numbers beats speed; "should work" is not done; every claim needs a live
number. The user cannot debug: anything that sits silent for more than a few
seconds reads as "it's broken".

## Non-negotiables

- **Do not trust the existing tests of audit 8 as proof.** They were written
  by the author of the code and passed while every defect below existed.
  Each fix here gets a regression test that FAILS on HEAD 64a371c (run it
  against the unfixed code and paste the failure line in the commit message)
  and passes after.
- Numbers do not move except where an item says the current number is wrong;
  such a change is pinned by a hand-worked test, and the endpoint snapshot
  (`outputs/snapshot.py <store-parent-dir> <out-dir>` before your first
  commit and after each group, compared with `outputs/diff_snap.py`) may
  differ only on the endpoints that item names.
- No `ROLLUP_SCHEMA_VERSION` bump (a spine rebuild of a 300M-row store is
  hours). A `ROW_RULES_VERSION` bump is allowed ONLY together with item
  1.1's guarantees: bounded, logged, and never stamping a file it did not
  check.
- **No start-up step may be silent or unbounded.** Anything that touches
  every stored file or every row before the dashboard answers must (a) print
  a line before it starts, (b) print progress at least every 15 s, and (c)
  be measured cold on the three stores named in "Start-up budget" below,
  with the disk READ-CALL count in the commit message (the hardware-
  independent number: the user's drive may be a spinning USB disk at ~10 ms
  per read, so 6,800 reads is over a minute even though it is 3 s here).
- Timings taken while anything else runs on the box are not numbers (round
  7: a 7.9 s call read 82 s under a parallel test run; this review: a 0.5 s
  scan read 56 s while a reviewer was generating files). Measure quiet, A/B
  alternating, both numbers in the commit message.
- Windows cannot be tested here. Where a fix depends on path spelling, file
  locks or rename semantics, make it correct BY CONSTRUCTION (compare
  basenames, never whole path strings; treat "could not prove" as "not
  done") and say in the status block that it was not run on Windows.
- One regression test per fix; ruff stays at zero; the full suite before any
  commit that touches numbers; `tests/slow_tests.txt` regenerated at the
  end; CLAUDE.md test counts updated.
- Trials live outside the repo with their own config, never pointed at
  `data/`; guard driver scripts with `if __name__ == "__main__"`; never run
  `playwright install`; never weaken TLS; never present a browser identity
  by default. Develop and push only on the branch named in your task
  instructions; no pull request unless asked.

## Test stores

The stores this review measured on lived in a session scratchpad and are
gone. Rebuild what you need OUTSIDE the repo:
- the 2,000-part store: `startup/mig_scale.py 2000` (seeds one small file,
  copies its part 2,000 times, registers the files at rules 2);
- a real-data store: ingest a handful of real payer files from the tested-
  sources catalog (the "named" 32M-row store was 12 Blue-plan shards);
- a large synthetic store for the timings of 4.6 and the start-up table:
  AI_HANDOFF Step 5 describes the 99M-rate, three-month, 45-part store —
  write parts straight from `RATES_SCHEMA` (about 2.2M rows each, three
  months, ~194,000 TINs, a few thousand of them sharing names across
  states), register them in `files`, and run one full rollup. Record how
  you built it in the status block so the next round can.
- `startup/r9_steps.py <store_dir> pin|nopin <rules|keep>` times every
  start-up step with its read calls; `outputs/timing.py` is the endpoint
  timer (point it at your store's config and port).

## Order of work

One commit per group, each with its before/after numbers and the failing-
then-passing test names. (1) Group 1 — data that is wrong or duplicated
today, including two defects in code shipped in 0.7.1/0.7.2; (2) Group 2 —
start-up and the Files tab, what the user sees first; (3) Group 3 — ingest
robustness; (4) Group 4 — outputs and dashboard; (5) Group 5 — downloads
efficiency and the P3 list. Then version 0.7.3, GETTING_STARTED "What
changed", AI_HANDOFF "Round 9 additions" (the rule each fix must keep), the
full suite, the zip, and the status block at the top of this file. Anything
you leave, leave in the status block with the reason.

## Group 1 — wrong or duplicated data (P0)

### 1.1 The row-rules update can stamp a file "done" without fixing it, and can block every file on one bad part (shipped in 0.7.2)

`store._migrate_types_only` (store.py ~1470-1520) asks DuckDB for
`SELECT DISTINCT filename, negotiated_type, is_dollar_rate FROM
read_parquet(?, filename = true, union_by_name = true)` over all parts and
maps each returned `filename` back to its file by exact string
(`by_path.get(str(f)) or by_path.get(str(Path(f)))`). Then EVERY name is
stamped `row_rules = 3` (`ok.append(name)` → `_finish_rules`).

- **(a) A part whose path comes back spelled differently is stamped
  unfixed.** `startup/f2_path_mismatch.py` simulates DuckDB answering with
  another letter case in the directory (a case-insensitive Windows path, a
  `\\?\` prefix or a drive-letter case change would do the same): the part
  gets no entry in `types`, so no rewrite, and the file is stamped. Observed:
  stored rows `[('fee_schedule', False)]`, `files.row_rules [(3,)]`.
  Expected: `('fee schedule', True)`, or the file left at rules 2. Whether
  Windows DuckDB really re-spells the path could not be tested — which is
  exactly why the code must not depend on it. The user's store has already
  been through this code.
- **(b) One unreadable part blocks all of them, and the log lies.**
  `ingest/n7_migrate.py bad2`: four files at rules 2, one part with valid
  `PAR1` magic and a damaged footer. The single scan raises; the warning
  prints; then "stored rows are up to date: 4 file(s) checked, 0 row(s) in 0
  file(s) rewritten". All four stay at rules 2 with `fee_schedule`/false,
  again at every later start. At rules 1 (the per-file path) the two good
  files are fixed and only the bad one waits (`n7_migrate.py bad1`).
- **(c) The rewrite join can double rows** if one stored spelling appears
  with both `is_dollar_rate` values in a part: `_typefix` then holds two
  rows for one `o` and the `LEFT JOIN … ON r.negotiated_type = t.o` matches
  twice (`ingest/n7_migrate.py dup`: 2 rows became 4; produced only by
  tampering with a part, but the join should not be able to).

Fix, all three:
- Map results by the part's BASENAME (`Path(f).name` — the `file_key` stem is
  unique), never the whole path; drop `union_by_name` (the `rates` view
  itself is a plain `read_parquet(glob)`).
- A part that exists and has no entry in the scan result is NOT stamped: it
  goes through the per-part check (`_normalize_part`), and if that cannot
  read it either it stays at its old rules with one warning naming it.
- If the batch scan raises, fall back to per-part scans; never print "up to
  date" while any file remains below the current rules — say how many are
  left and that they will be retried.
- `_typefix` keyed on (old type, old is_dollar) or deduplicated on `o`.
- Bump `ROW_RULES_VERSION` to 4 with rule 4 = "rule 3, verified": every
  store stamped by 0.7.2 is re-checked once, on the fast path, under this
  group's start-up rules (item 2.1). Pin with: the `f2` simulation (a
  proxied connection that re-spells `filename`), the `bad2` case, the `dup`
  case, and a 0.7.2-stamped store with a leftover `fee_schedule` part.

### 1.2 The same file is not recognised as itself: reloaded, and with `move_processed: false` reloaded forever (shipped in 0.7.1)

`ingest.same_as_loaded` (ingest.py ~1366-1388) recomputes the head hash over
`max(1, head_bytes >> 20)` MiB — a FLOORED MiB count — while the stored
fingerprint hashed `head_bytes` exactly (the whole file, for anything under
`early_dedup_mb`). Any file between 1 MiB and 15 MiB that is not a whole
number of MiB never matches its own fingerprint.

- `ingest/n1_samename.py`: a 2,415,107-byte file, `move_processed=False`:
  each further scan renames and reloads it (`acme_2026-05-e54fe6.json`,
  `…-e54fe6-e54fe6.json`, …): 4 files rows and 28,000 rate rows for a
  7,000-row file. With `move_processed=True`, re-dropping the same file
  three times loads three more copies. A 207,107-byte file is left alone
  (the floor rounds to the 1 MiB minimum, which covers it).
- `ingest/n1b_watcher_loop.py`, the real `cli._watcher_loop` with
  `move_processed=False`: each rename wakes the watcher, which scans again:
  11 files rows and 70,001 rate rows after 30 s, still growing. The default
  is `move_processed: true`, but `_finish_file` deliberately leaves a file
  in the inbox when the move fails (a Windows antivirus lock) — the same
  loop.
- `ingest/n9_upload.py` part A: the same 2.3 MiB body uploaded twice is
  queued under a new name the second time.

And the "same file" proof itself is too weak for local files:
- `ingest/n8b_same_size.py`: a corrected file of the SAME byte size under
  the same name (one rate 40.0 → 55.0) is declared identical — by size alone
  when the loaded twin predates fingerprints (1.0 MB case), by the sampled
  head/tail/windows when the change sits between them (21.4 MB case). Scan
  result `[]`, 0 rows at 55.0, and the new file is moved over the old one in
  `processed/`. This is the weakness the download side closed in audit 8 F3,
  reintroduced for the inbox.
- `ingest/n9d_double_upload.py`: the same 0.69 MB body posted twice 0.2 s
  apart loads twice (2 files rows, 4,000 rows for 2,000): while the first is
  `processing` there is nothing to compare against.
- `ingest/n8_redrop.py all`: a file replaced by rename while it is being
  parsed — `_finish_ok` fingerprints and moves whatever is at the path
  AFTER the parse, so the store holds the old rows, the loaded fingerprint
  is the new file's, and dropping the new file again is "already loaded"
  (POSIX rename semantics; Windows normally refuses the rename — P2).
- `unique_inbox_name` mangles a name with a known suffix in the middle:
  `rates.json.2026-05.json` → `rates.json.20-abc123.json.json`
  (`ingest/n9_upload.py` part B).

Fix: for a LOCAL file "the same" means the full-content sha256 is equal —
the file is on disk, hashing it is one sequential read. Record the full sha
(and size, mtime_ns) in `_finish_ok` from the bytes that were parsed (hash
while parsing or re-check size/mtime/inode at finish and refuse to
fingerprint or move if they changed). A loaded twin with no stored sha is
"cannot prove same" → the newcomer is loaded under a new name, never
skipped. An upload is compared by full sha against loaded AND in-flight
files of that name. A rename happens at most once per content (tag the new
name with the content sha so a second scan finds its own file `done` under
that name and stops). `unique_inbox_name` strips only TRAILING known
suffixes. Tests: the existing same-name tests re-run with a 2.3 MiB body;
the watcher loop with files count pinned at 1; same-size-different-content
with and without a stored fingerprint (the 55.0 row must exist); the double
upload; the mid-parse replace.

### 1.3 A rate file is matched to ANOTHER month's standalone provider list

`store.load_provider_refs(payer)` (store.py ~4462-4507) serves, per ref id,
the payer's newest vintage — whatever month the rate file being ingested is
from. Callers: ingest.py ~686 (pooled) and ~1133. `requeue_skipped`
re-ingests only files with skipped/missing counters, so a stale match is
never corrected. (Older than audit 8, but it contradicts the "provider
references are file-local" rule audit 8 wrote down.)

`ingest/n3_vintage.py`, all three cases reproduced twice:
- April's companion is loaded; May's rate file arrives before May's
  companion → 2 rows on April's TINs (111111111 / 222222222),
  `ref_groups_skipped=0`, `missing_ref_ids=0`. May's companion lands:
  `requeued: []`; the rows stay on April's practices. Expected 555555555 /
  666666666.
- Both companions loaded, April's rate file re-ingested → it moves to May's
  practices.
- One inbox scan holding April's rates and both companions → April's rate
  file gets May's practices.

Fix: a rate file resolves companion ids only from standalone reference files
of ITS OWN vintage. Define vintage once (suggested: the publication month,
by the same header → filename → ingest rule `file_month` uses; exact
`last_updated_on` equality first if both carry one) and write it in
AI_HANDOFF. No companion of that vintage → the id is missing and counted, so
`requeue_skipped` picks the file up when the right companion lands — and
make that path real for URL downloads too (item 3.2). The ambiguity rule of
audit 8 then applies within a vintage. Tests: the three cases with exact TIN
assertions, plus "a newer-vintage companion never changes an older month's
rows on re-ingest".

### 1.4 Provider-reference ids above 2^53 collapse into a neighbour

`parser.clean_ref_id` (parser.py ~182-191) and its copy `fastscan._ref_int`
(fastscan.py ~190-197) go through `float()`. `clean_ref_id(1234567890123456789)`
and `clean_ref_id(1234567890123456790)` both return 1234567890123456768.
`ingest/n5_bigids.py`: with fast_scan on and off, code 97110 (cites the
first id, TIN 111111111) is stored on TIN 222222222 with `bad_ref_ids=0`,
`missing_ref_ids=0`; a standalone reference file with the two ids stores 1
id, not 2. Payers do publish long numeric ids.

Fix: integral ids are parsed exactly — an `int` stays an `int`, a digit
string goes through `int()`, `"12.0"` / `12.0` through `Decimal` and only
when integral; anything else is `bad_ref_ids` as today. One function, used
by the parser, fastscan, the skim and the reference-file parser. Check the
`provider_refs.ref_id` column type holds 19 digits (BIGINT does; an id past
int64 must be refused and counted, never wrapped). Test: the `n5` document
on all five parse paths and the reference-file parser.

### 1.5 A different multi-member gzip file is skipped as "already loaded"

The early skip (fetch.py ~1809 `_PROVABLE`, ~1899-1906 `_proves`) treats a
gzip file's tail as a checksum of its whole content. In a multi-member gzip
(bgzip, concatenated shards — valid gzip, read transparently by Python) the
trailer covers only the LAST member. `ingest/d2_multimember.py`: two valid
5-member files of 2,653,091 bytes; 1,333 bytes differ at offsets
1,224,370–1,225,858 (one rate, 40.0 vs 47.0), outside the first 1 MB, the
last 1 MB and all 8 sampled windows. A loads 4,000 rows; B ends
`skipped/duplicate` — "identical to …, already loaded … skipped after 1.0
MB" — and the store has 0 rows at 47.0 (expected 1). The skip message
states the false claim in words.

Fix: a gzip copy is skipped early only against a loaded twin that was
VERIFIED single-member when it was ingested (the ingest reads it end to end
anyway: record `gz_members = 1` in `content_fingerprints`; unknown = not
provable). For zip the proof is the central directory (a CRC per member),
which sits at the end: keep zip provable only when the whole central
directory lies inside the compared tail — check that from the end-of-
central-directory record, else not provable. Otherwise download in full and
let the full sha decide. Also: a segmented
download (`download_segments > 1`) currently skips the early check entirely
(`ingest/d7_segments.py`, fetch.py ~696-714: a copy is fetched in full, 5
range requests) — run the check before the segments start. Tests: the `d2`
pair (B must be ingested), a single-member twin (still skipped early), a
zip pair differing in one member.

## Group 2 — start-up and the Files tab (P1): what the user sees first

### Start-up budget (measured at HEAD, cold page cache, per step)

`startup/r9_steps.py <store_dir> pin|nopin <rules|keep>` wraps every step of
`Store.__init__` and prints seconds, read calls and MB read from
`/proc/self/io` after dropping the page cache. Results (pinned connection):

| Store | Step | Seconds | Read calls | MB read |
|---|---|---|---|---|
| 2,000 tiny parts, rules-3 update pending | `_quarantine_corrupt_parts` | 0.10 | 4,004 | 17.0 |
| | `_migrate_types_only` | 0.64 | 16,242 | 6.5 |
| | total open | 0.8 | | |
| 2,000 tiny parts, nothing pending | `_quarantine_corrupt_parts` | 0.09 | 4,004 | 17.0 |
| | total open | 0.2 | | |
| 32M real rows, 12 parts, update pending | `_migrate_types_only` | 0.34 | 6,817 | 10.6 |
| | total open | 0.4 | | |
| 99M synthetic rows, 45 parts, update pending | `_migrate_types_only` | 0.53 | 5,185 | 12.7 |
| | total open | 0.6 | | |

For comparison, 0.7.1's code on the 2,000-part store: 93.8 s, silent
(`startup/mig_scale.py`). The seconds are a quiet machine with a fast SSD.
The READ CALLS are what transfer to the user's machine: at ~10 ms per read
on a spinning USB drive, the 32M-row scan (6,817 reads) is over a minute,
a 300M-row store several minutes, and the every-start integrity check of a
few thousand parts is 40–60 s — and neither prints anything while it runs.
(The same 0.34 s scan read 3.3 s, and one first open 56 s, while a reviewer
was generating files on the same disk.)

Server start on the 99M store, quiet: banner 0.6 s after launch. First
requests after a cold start: `/api/stats` 1.2 s, the Explorer's default
`/api/rates` 1.1 s, `/api/summary` 9.3 s, `/api/overview` 13.9 s.

### 2.1 The one-time update prints one line and then nothing while its scan runs

`_migrate_row_rules` logs "one-time update of N stored file(s)…" and its
15-second progress ticks fire only AFTER the single scan returns (store.py
~1486-1520). On a slow drive the user sees the same silence that started
this round. Fix:
- Scan in batches of parts (say 25–50 per query) with a tick between
  batches, so the progress line is real.
- Read less: `is_dollar_rate` is a function of the type in every version
  (verify, then read `negotiated_type` alone — half the reads); consider
  pruning row groups whose min = max is an already-normalised type via
  `parquet_metadata()` (0.03 s and one read per part here) and scanning only
  the rest. Report read calls before/after on the 32M-row store.
- Decide, and write down in AI_HANDOFF, the rule for ALL future stored-row
  updates: the dashboard must open first. Either the update runs in the
  background with a visible status (a banner on every tab and a line on the
  Files tab: "updating stored rows: k of N files") and the numbers it
  affects are labelled until it finishes, or it stays before start-up with a
  hard budget (e.g. 30 s) after which it continues in the background. The
  current comment ("BEFORE the rollup check: a full rebuild it may trigger
  must see the fixed rows") is the constraint to preserve.
- Print one line BEFORE the store opens ("opening the store at <path> …")
  and one when the dashboard is answering, so "nothing printed" can only
  mean "nothing is happening".

### 2.2 Every start reads from every stored file before anything is logged

`Store._quarantine_corrupt_parts` (store.py ~1108-1160) opens each part and
reads its first and last 4 bytes at EVERY start: 2 reads per part (4,004
read calls / 17 MB for 2,000 parts), before the first line after "analytics
memory budget". Keep the protection, lose the cost: remember each verified
part's (name, size, mtime_ns) in `meta` (or a small sidecar) and re-check
only parts that are new or changed; log a line when more than ~200 parts
need checking. Pin with a read-call assertion on Linux (`/proc/self/io`),
skipped elsewhere.

### 2.3 Freeze diagnostics start after the part most likely to stall

`cli.cmd_serve` opens the store at ~line 218 and starts
`_start_stack_recorder` at ~258. During a start-up stall
`diagnostics/stacks_latest.txt` — the file the banner tells the user to
send "if the app ever freezes" — still holds the PREVIOUS session's
snapshot. Start the recorder before `Store(...)` (it needs only
`cfg.store_dir`), and have GETTING_STARTED's "it won't start" entry point at
that file and at `serve.log`.

### 2.4 Files tab: rows that need the user fall off the list (regression from d977e72)

`/api/files` (api.py ~1787-1810) now returns the newest 500 rows ordered by
`coalesce(finished_at, started_at) DESC NULLS LAST`. A file waiting for
"ingest anyway" (`pending_confirmation`) or `queued` has neither timestamp
and sorts LAST; an old `failed` row sorts by its date.
`startup/f1_files_page.py`: 600 done files + one pending_confirmation + one
queued + one failed from 2020 → `total 603 shown 500`, and none of the
three is on the page. The user cannot see or press "ingest anyway".

Fix: rows that need attention or are in progress (processing, queued,
pending_confirmation, failed, quarantined, stopped) are ALWAYS returned,
first, outside the page limit (bounded by their own generous cap, with a
count if exceeded); the page limit applies to `done` rows, newest first,
with a unique tiebreaker. The "showing N of M" line says so. Test: the `f1`
store; assert the three rows are present and first.

### 2.5 First paint after a cold start

On the 99M store the Overview takes 13.9 s and the summary 9.3 s on their
first call after a start (17–24 s and 12–15 s under load; the same before
audit 8 — an old cost, not a regression). The Overview is the first thing
the user looks at. If a profile shows a cheap win, take it; otherwise show
"loading the store — the first screen can take a moment" in the header
until the first answer arrives, so a slow first paint is not read as a
hang. Do not add a warm-up that competes with a running parse for the disk.

## Group 3 — ingest robustness (P1/P2)

### 3.1 A big standalone provider list becomes a 0-row rate file off the single-read path

Audit 8 P1-2 was fixed only where `run_big_file_fast` runs. On the two-pass
path "no target codes" short-circuits before anyone asks whether the file
has an `in_network` array at all: ingest.py ~1177-1199 (in-process) and
~632-636 (the pool worker returns `short_circuit` without `refs_only` when
the skim ran). Triggers: `fast_scan: false`, **free disk under 2 GB
(`parser.SPOOL_FREE_FLOOR`) on the default config**, all-codes mode, a
numpy-less install. `ingest/n2_bigref_fastoff.py` (and `… lowdisk`): a
20,000-id reference file on the big path → `classic_big`,
`classic_big_pooled` and `fast_big_lowdisk` record it `in_network / done /
0 rows` ("scanned: none of the target billing codes appear"), 0 ids stored,
the waiting rate file stays at 0 rows; `fast_big`, `fast_big_pooled`,
`classic_small` are right. Fix: the skim (both `skim_needed_ref_ids` and
the fast one) reports whether an `in_network` array and a
`provider_references` array were seen; no `in_network` + a table → 
`_ingest_reference_file`, on every path. Test: parametrise the existing
big-reference tests with `fast=False` and with `_free_disk` patched low,
in-process and pooled.

### 3.2 A rate file from a LINK that loads before its provider list is never re-read

`requeue_skipped` (ingest.py ~1335-1359) looks for the source only in
`processed/` and `inbox/`. URL downloads live in `downloads/` and are
deleted after ingest by default (fetch.py ~2528-2537), and the URL queue
does not order reference files first. `ingest/d6_url_companion_late.py`,
with `delete_raw_after_ingest` both True and False: the rate link ends
`done, rows 0, error None`; the reference link ends `done, 2 ids`; rates
rows 0 (expected 2); one GET for the rate file. Links are how this user
loads everything. Fix: when references land, every `done` in-network file
of that payer AND VINTAGE (item 1.3) with skipped / missing / ambiguous ids
is re-ingested from `downloads/` if the file is still there, otherwise its
queue row is re-queued (one re-download); until then the link's note says
"waiting for this payer's provider reference file" instead of a bare 0.
Consider claiming reference-kind rows ahead of rate files of the same
index. Test: `d6` asserting 2 rows, both settings.

### 3.3 Files that rely on a companion now hold every target group in memory

Since 37cd705 a group citing an id is deferred until the file's own table
is complete (parser.py ~861-865); a file with no table completes only at
end of file. Invariant 1 says memory must not scale with file size.
`ingest/n6_mem.py <head|old> <layout> <mb> <mode>` (peak RSS; no own table,
1,000-id companion, ~5,200 rows per MB):

| File | Path | HEAD | 556cbf4 |
|---|---|---|---|
| 100 MB | small, fast | 835 MB | 419 MB |
| 201 MB | small, fast | 1,329 MB | 427 MB |
| 401 MB | small, fast | 2,289 MB | 415 MB |
| 401 MB | small, classic | 2,824 MB | — |
| 401 MB | big two-pass (fast_scan off) | 2,847 MB | 315 MB |
| 401 MB | big single-read | 357 MB, flat | — |

About 5 MB of RSS per MB of file; the small path is capped by the 1.5 GB
big-file threshold (per parser worker, and three run at once), the big
two-pass path is not capped at all. (A table AFTER `in_network` was already
2,259 MB before audit 8 and is 2,264 MB now — the same class, older.)
Fix, either: (a) spool deferred groups to disk as `run_big_file_fast`
spools items, replaying after the table is known; or (b) on two-pass paths
have pass 1 report which ids the file's own table defines (and whether it
has one), so pass 2 emits at once; for the single-pass small path (a) is
the only option, or route no-table / table-last files above a modest size
to the single-read path. Test: a no-table file of N items with
`len(parser._deferred)` bounded, and the `n6` RSS numbers in the commit
message for 100 / 200 / 400 MB on each path (flat).

### 3.4 Companion entries that are empty or remote drop rate groups with no counter

`save_provider_refs` stores an empty entry as a placeholder group
(store.py ~4428), so it loads as "resolved" with nothing in it;
`parse_provider_reference_file` ignores `location` in standalone files
(parser.py ~1358). `ingest/n11_misc_refs.py` case 1: 3 groups cited, 1 row
stored, `ref_groups_skipped=0`, `missing_ref_ids=0`, `remote_ref_groups=0`
— the same two ids in the file's OWN table are counted. Fix: an empty
companion entry is missing; a `location` entry is remote; both counted and
shown on the Files tab exactly as the own-table cases are.

### 3.5 Smaller, each reproduced

- An own table split around `in_network` (a duplicate top-level
  `provider_references` key): the id defined in the second part resolves
  from the companion (TIN 888888888 instead of 222222222) or counts as
  missing; all paths agree, the single-read path declines
  (`ingest/n4_paths.py`). Rule: the file's table is every
  `provider_references` array it contains.
- In-flight fingerprint rows are left behind after `claim_content_ingest`
  skips a row and after `cancel_all_downloading` (store.py ~2837-2841,
  ~3163; 1–2 rows left in `ingest/d1_wait.py` runs; no wrong outcome seen).

## Group 4 — outputs and dashboard (P1/P2)

No wrong-number-for-sale P0 was found in the audit-8 output changes: the
round-8 reproductions t1–t14 all give their hand-worked answers at HEAD
(`outputs/t/`), `half_up` equals DuckDB's `round()` on 60,000 doubles, and
43 endpoints were byte-identical across two server processes. What follows
is where two surfaces disagree, mislead, got slower, or fall over.

### 4.1 A report pinned to an older month prints a market trajectory that runs past it

`benchmark._erosion_block` drops the month before calling
`compute_payer_trajectory` (benchmark.py ~1858), and the trajectory forces
`latest` (monitor.py ~341). `outputs/t/s4_pinned_erosion.py`: P Health pays
$50 in 2022-06 and 2023-06, then $40 in 2024-06. The pitch report headed
"As of 2023-06" prints "Market — P Health: -20.0% in dollars over 2022-06 to
2024-06, and -25.4% after inflation"; its own sparkline on the same page
reads "2022-06 $50.00 → 2023-06 $50.00 (+0.0%)". The "Publication age" line
of that report also cites "last published 2024-06-01". Expected: as of
2023-06 the change is 0.0 % over 2022-06 → 2023-06 (the rule audit 8 gave
the sparkline). Fix: a pinned month is an upper bound (`file_month <=
pinned`) for the trajectory, the publication-age line and anything else in
a pinned document; sweep the report builders for other month-free reads.
Test: three months with the cut in the last; a pitch pinned to the middle
month contains neither the last month's label nor the cut.

### 4.2 "Cut streak" reports a cut that never happened

monitor.py ~426-430 compares consecutive monthly index points, but a middle
month's point covers only the panel pairs priced THAT month.
`outputs/t/s3_traj.py`: Alpha's 97110 panel is 5 practices; only C (40→50)
and D (50→50) are priced in the middle month; in the last month D goes to
55. Series 0.0, +12.5 (2 of 5 practices), 0.0 → the table shows "▬ 0 % …
Cut streak 1" while `/api/changes` for the same two months says `n_cuts 0,
n_increases 1`. Expected streak 0. Fix: each month-to-month step is
computed on the pairs priced in BOTH adjacent months (chain-linked), or a
month where the whole panel is not priced is not a point; say which in the
note. Test: this fixture, `cut_streak == 0`.

### 4.3 One practice's "median position" depends on which screen you read

Two different rules are in use (`outputs/t/s7_position.py`; nine MO
practices, one payer; A is at 12.5 on 97110 and 50 on 97140; by hand
median(12.5, 50) = 31.25 → p31):

| Surface | No tied code | With an all-tied 97530 |
|---|---|---|
| Leads | p31 | p31 |
| Leaderboard | p31 | p31 |
| Payer roster | p31 | **p50** |
| Negotiate headline / one-pager | **p32** | **p50** |
| Dossier ("median position is") | **p32** | **p50** |
| Size premium | 31.3 | 37.5 |

Causes: (a) rounding order — Negotiate and the dossier take the median of
per-code positions ALREADY rounded to 0 dp (benchmark.py ~960-963, ~1164;
dossier.py ~219); Leads rounds the median of unrounded positions. (b) the
"≥ 2 distinct rates" code set went only into Leads and the leaderboard; the
roster (leads.py ~354-357) and everything built from Benchmark rows still
count an all-tied code at p50. Also: Leads and the leaderboard show
different `n_codes` for the same practice (2 vs 3 — the leaderboard's
`min_codes` gate counts all-tied codes), and Leads' worst payer still
averages each payer over ITS OWN codes (`outputs/t/s6_leads.py`: Alpha
(8+2)/2 = 5.0 vs Beta 6.0 → "worst: Beta Health $6.00" although Alpha pays
less on the only code both price — the flaw audit 8 fixed in visit
economics). Fix: ONE helper for a practice's overall position — unrounded
per-code mid-ranks, one code set, rounded once — used by Leads, the
leaderboard, the roster, Negotiate, the dossier and the size premium; the
worst payer ranked on the codes the compared payers share (or shown with
its code count and not ranked). Tests: the `s7` fixture with every surface
equal; the `s6` fixture.

### 4.4 The "subject rule" of audit 8 reached only three surfaces

`subject_tins_for_market` + `subject_market` are used by Benchmark,
Negotiate and the rate card. Still on the old rule: contract gaps (the
subject is read from the state-filtered `base`, benchmark.py ~1268-1275),
`_subject_has_scoped_rates` (~1634, whose docstring still says it mirrors
the old CTE), the size premium and the rate-type flag (quality.py ~235,
~300-312, ~374-378), hospital parity's subject rate (hospital.py ~586-591),
the dossier header (dossier.py ~108), and a partial name (benchmark.py
~515). `outputs/t/s9_ks_subject.py`, `s1_subject.py`, `s13_dossier.py`:
- An out-of-state subject ("ACME REHAB · KS", its TIN, or "K1 REHAB")
  against MO peers: Benchmark shows "your rate 97110 $60.00, 97140 $36.00";
  the contract-gaps panel ON THE SAME TAB lists 97110 and 97140 among the
  codes the practice is not paid for; the size premium answers 422 "no
  rates under this market scope"; the rate-type flag is empty.
- "ACME REHAB" with state MO resolves to the MO TIN: $41.00, p10. Typing
  "ACME" (a partial name that identifies the same name) resolves to both
  TINs: $50.50, p70. Hand: MO TIN = median(40, 42) = 41.
- The dossier header for "ACME REHAB" + MO reads "2 provider NPI(s), Town
  (KS, MO)" above a rate table of the MO TIN only; on the 99M store
  "PRACTICE 0 — 60 provider NPI(s) … (MO, ND, TX)" above MO-only rates.
- A mapped entity's dossier is titled with ONE member's NPPES name
  (dossier.py ~170, clients.py ~197): subject "BIG GROUP" (NORTH CLINIC +
  SOUTH CLINIC) prints "Practice profile — NORTH CLINIC" over the group's
  number ($48.50 = median(45, 52)).
- `resolve_subject_tins` has no ORDER BY (benchmark.py ~547-560): the
  "Subject tax IDs" line of a rate card or pitch report came back in three
  different orders in six calls on the 99M store (invariant 7; numbers
  identical).

Fix: every subject-side query goes through `subject_tins_for_market` +
`subject_market` (grep for `resolve_subject_tins(` and for subject reads of
a peer-filtered relation); a partial name that resolves to one name takes
the same state rule; a mapped entity is titled with its mapped name;
`resolve_subject_tins` returns a sorted list. Tests: an out-of-state
subject with "Benchmark priced codes ∩ contract-gap codes = ∅"; the
partial-name pair equal; the header's NPI/state line consistent with the
table; a multi-TIN name in `test_determinism.py`.

### 4.5 A different file under an existing name is loaded BESIDE the old one, and every number becomes the median of both

This is audit 8 P1-B's own behaviour, seen from the numbers
(`outputs/ui_files.py`, real Chromium): `sigma_2026-06.json` prices 97110
at $61 / $62; a second, different `sigma_2026-06.json` prices the same TINs
at $71 / $72. The UI says "a file named sigma_2026-06.json is already in
MRF Explorer — this one is loaded as sigma_2026-06-a66cad.json", and
`/api/rates` then shows **$66.00 and $67.00** — numbers neither file
contains. A corrected re-publication is the common reason to drop a file
with the same name. Fix (with 1.2): when the newcomer has the same payer
and publication month as the loaded file of that name, ASK — "Replace the
loaded file" (default) or "Keep both" — on the upload path, and for an
inbox drop load it as a replacement candidate that the Files tab shows with
those two buttons; never blend silently. At the very least the note must
say "both files now count toward every number; remove the old one to use
only this". Test: upload A (61) then a different A (71) under the same
name; the TIN rate is 71 after "replace", and nothing is blended without an
explicit choice.

### 4.6 Slower than before audit 8 (99M store)

Quiet machine, first / second call, old = 556cbf4:

| Call | Old | New |
|---|---|---|
| `/api/trajectory`, national | 11.8 / 12.7 s | 20.0 / 21.0 s |
| `/api/trajectory`, MO | 1.4 / 1.3 s | 2.3 / 1.9 s |

Under load (load average 3–7; only differences that repeated across passes
are listed; `outputs/timing.py`, `cmp_timing.py`): pitch report national
17.7 / 18.0 → 26.2 / 26.0 s (it embeds the trajectory); `outreach.zip`,
TIN grain, no filter (the button's default) 62–64 → 73–86 s with RSS near
6 GB (outreach.py ~161-177 passes all 194,000 TINs as a parameter and
builds a ~4.85M-entry (TIN, code) dict in Python). Outreach at entity grain
with a state filter got FASTER (30–44 → 18–32 s).

Over 10 s at all, old and new alike: overview first call 17–24 s (13.9 s
quiet); summary first call 12–15 s (9.3 s quiet); dossier for a NAME
subject 13–32 s on every call; changes national first call 30–36 s (and the
Rate changes default view rendered 612k characters in 30 s in Chromium);
negotiation one-pager first call 8.5–13 s; `outreach.zip` entity grain with
no filter answers 503 "needs more memory than the current limit (6 GB)"
after 13–17 s.

Fix: the trajectory is cached per (data generation, market) like its
neighbours (`generation_cached`) and its panel query profiled (the `base`
CTE is read four times — materialise it once); the Rate changes tab's
trajectory button gets the tab's state control so it is not always the
national call; outreach computes per-unit medians in SQL (join the units to
the practice-median relation; only the wanted codes; no 194k-element
parameter); the dossier's name path is profiled and brought to its TIN
path's cost (0.7 s). Also (P3): the materialised practice medians are keyed
by a random per-process tag (store.py ~1024, ~4308, ~4327), so they are
rebuilt on every server start (part of the 8–13 s first call) and other
instances' files are deleted only after 6 hours — five restarts left ~490 MB
beside the store. Key them by spine generation + content, reuse across
restarts, and delete superseded ones at open. Numbers quiet, A/B, in the
commit message; a timing guard in `slow_tests` on a mid-size store.

### 4.7 Wrong-typed input answers 500 with raw database text

`normalize_market` (benchmark.py ~104-145) coerces only month, payers and
state; the endpoints catch `BenchmarkError`, `ValueError`, `TypeError` but
not `duckdb.Error`. Live on a tiny store: `{"market": {"billing_class":
["professional"]}}`, `{"discipline": {"a": 1}}`, `{"city": 5}` → 500 with a
body like `{"error":"ConversionException: … can't be cast to the
destination type VARCHAR[] … LINE 3: …"}` (a SQL fragment) on
`/api/benchmark/market`, `/api/report/pitch`, `/api/report/ratecard`,
`/api/report/negotiation`, `/api/trajectory`, `/api/leads`,
`/api/leaderboard`, `/api/quality/size`, `/api/schedule/fee`,
`/api/negotiate/compare`, `/api/leverage`, and per the fuzz log
`/api/economics/weighted-position`, `/api/contracts/renewals`,
`/api/contracts/new-to-network`, `/api/territory/local`,
`/api/report/org-bundle.zip` (`outputs/t/fuzz.py`; the POST matrix reached
about a third of the routes — assume the rest behave the same). Also:
- `/api/files?offset=99999999999999999999` → 500 (INT64 overflow, api.py
  ~1805: `offset` has no upper bound — added in audit 8);
- `/api/org-website` `{"tins": 5}` (api.py ~1785), `/api/medicare/tracker/
  import` `{"paths": 5}` (~2291) → 500 TypeError;
- `/api/upload` with a 300-character file name → 500 "OSError: File name
  too long: '<full inbox path>'" (leaks a filesystem path; Windows limits
  bite earlier); `/api/utilization/import`, `/api/floors/import`,
  `/api/demographics/import` the same with a 3,000-character path;
- a client can send the PRIVATE key `market._plan_files`:
  `["alpha_2026-06.json"]` silently scopes Benchmark to one file (subject
  $40.00 with 9 peers instead of $41.00 with 10) and the echoed market does
  not show it; `"\u0000"` → 500 ParserException (quotes are doubled — no
  injection was possible);
- `/api/contracts/renewals` with `subjects` as a string iterates per
  character (contracts.py ~112-116): 1,000 characters took 1.95 s on a
  68-row store, 20,000 stalled it for minutes.

Fix: `normalize_market` drops every key starting with `_` and coerces or
rejects EVERY key by type (a refusal is a `BenchmarkError` sentence); one
app-level `duckdb.Error` → 422 handler that logs the detail and returns a
plain sentence with no SQL and no path; bound `offset`; type-check `tins`,
`paths`, `subjects`; cap file-name length with a sentence. Test: a
parametrised wrong-type market over EVERY POST route from `/openapi.json`,
asserting no 5xx and no "LINE " / drive-or-slash path in any body.

### 4.8 The outreach ZIP's methodology contradicts its own columns

api.py ~3135 reuses the Explorer's `methodology_text` and appends
`OUTREACH_COLUMNS_NOTE`. `outputs/t/s5_outreach.py` prints the sidecar: it
says "Grain: entity (one row per payer x billing_code x entity x
modifier-set x class x POS-set x month)" and "As-of month: ALL months
pooled … these medians/counts blend every vintage", then the appended note
says the C-columns are on "the as-of month (latest by default)". The file
is one row per organization and the numbers are latest-only; a
`billing_class=institutional` filter is listed as a filter while the
columns stay professional. Fix: an outreach-specific methodology — the
filters choose WHICH organizations are listed; the basis of the numbers is
stated once. Test: the sidecar never contains "ALL months pooled" when the
columns are latest, and says "one row per organization".

### 4.9 Smaller, each reproduced

- Sparkline % uses Python formatting (spark.py ~78): $80.00 → $81.00 reads
  "(+1.2%)" in the pitch / rate-card sparkline and "+1.3" in `/api/changes`
  (`outputs/t/s8_gaps_spark.py`). `half_up(…, 1)`. (No other Python
  `round()` on a printed number was found.)
- "-0.0%" printed (inflation.py ~253, benchmark.py ~1871): $40.00 → $39.99
  gives "Q Health: -0.0% in dollars … but -2.9% in real terms". Normalise
  negative zero in one formatter.
- Raw JSON in every client-facing report footer: `Market definition:
  {"month": "latest", "billing_class": "professional", …}` (pitch,
  negotiation, rate card, proposal). Print it in words.
- `include_non_dollar` (API only): Benchmark and compare pool a percentage
  80 with $40 and print $60.00; the rate card prints $40.00
  (`outputs/t/s10_flags.py`). A percentage is never pooled with dollars.
- The Sources tab is silently empty when `serve` is started from another
  directory with an absolute `--config`: `known_sources_path` is relative
  (config.py ~274) and is not anchored like the other paths;
  `/api/known-sources` returned `{"sources": []}`.
- Cosmetic: "1 codes · 5 practices"; the Rate changes summary box ends with
  a line starting ". Biggest cut -10%."; after a duplicate upload the
  dropzone still says "Uploaded — processing begins shortly"; the proposal
  table prints "(+146%)" beside "(+142.1%)".

### 4.10 Suspicions — read, not run

- Picking the same path twice in the Files tab's file picker probably does
  nothing: the hidden `<input>` is never reset, so no `change` fires.
- Hospital parity groups by `hospital_name` only (hospital.py ~628): two
  hospitals with one name and no state filter would be pooled.
- Negotiability is ordered by the unrounded spread but labelled with a
  spread computed from rounded p10 / p50 / p90 (market.py ~136-145):
  adjacent rows could look inverted by a point.
- `/api/medicare/org` uses `any_value(display_name)` (api.py ~3081): a
  multi-TIN subject's name could vary between calls.
- Packets (the async build), floors compare, and visit economics with three
  or more payers or an empty common basket were not hand-checked.

## Group 5 — downloads efficiency (P2) and suspicions

### 5.1 Size lanes cost a queue of big files a second request per file and throughput

Unsized rows are claimed by small slots first and handed back at the
headers (a second GET later); slot 0 never takes a big file (store.py
~2449-2467, fetch.py ~2841-2845, ~854). `ingest/d4_lanes.py
<sized|chunked> [lanes_off]`, ~2.7 MB files throttled to ~2.5 s each:

| Slots | Files | Lanes on | Lanes off |
|---|---|---|---|
| 6 (default) | 24 | 19.4 s, 45 GETs, avg 4.1 busy | 14.2 s, 24 GETs, avg 5.3 busy |
| 4 | 16 | 19.7 s, 31 GETs | 13.4 s, 16 GETs |
| 2 | 24 | 66.1 s, avg 1.0 of 2 busy | 33.6 s, avg 1.9 busy |

On a 2,000-file index of big files that is ~2,000 extra aborted GETs
against the payer's CDN (politeness matters: these servers throttle). The
user asked for the lanes ("3 for ≤100 MB, 3 for larger") — keep the
feature, remove its cost: learn a row's size without a throw-away GET (a
HEAD, or keep the connection the small slot opened and let THAT download
continue as a big-lane download instead of aborting it — the bytes are
already flowing); let slot 0 take a big file when no small or unsized row
is queued; with 2 or fewer slots, no lanes. Numbers for the three rows of
the table in the commit message; GETs per file must be 1.

### 5.2 Suspicions — reasoned from the code, NOT reproduced; settle each with a test either way

- `waiting_on` is not cleared by `clear_queued`, `requeue_failed` or
  `set_url_status_by_id` (seen: a row `queued … waiting_on=1` after a
  clear/resume). A later stall-park of that row would be held by
  `_NOT_WAITING` while the old twin is in flight.
- A copy can be skipped as "already loaded" against a loaded fingerprint
  whose own link is mid-refetch with new content; once the twin is
  replaced the copy's content is nowhere in the store.
- A parked replacement that cannot be adopted at open would be discarded by
  the row-rules update's own `_swap_in_part` while it rewrites the old part.
- The classic reader still fails a file on a NaN / 20-digit number in a
  non-target item where the fast reader does not (documented in round 8;
  `e4`, `e4b`).

## Checked and found fine (do not re-chase)

- **Start-up at 0.7.2:** `Store()` with the rules-3 update pending — 99M
  synthetic rows / 45 parts 0.6 s (5,185 read calls); 32M real rows / 12
  parts 0.4 s (6,817 read calls); 2,000 tiny parts 0.8 s; 0.7.1's code on
  the same 2,000-part store 93.8 s. `mrfx serve` on the 99M store: banner
  0.6 s after launch; cold first `/api/stats` 1.2 s, default Explorer page
  1.1 s. (Quiet machine, fast SSD — see 2.1 for why that is not the user's
  drive.)
- **The update path:** a 0.7.0 install in a folder with a space in its
  name, updated with the OLD build's own `mrfx update` and the 0.7.2 zip: 55
  files updated, the user's `config/mrfx.yaml` (with a hand-written note and
  a custom port) kept, `mrfx.yaml.new` written, data untouched; the first
  start logs the one-time update, rewrites the one aged part
  (`'fee_schedule' -> 'fee schedule'`), catches the rollup up in the
  background, and the rows read `('fee schedule', True)`
  (`startup/seed_old.py`, `age_store.py`). The supervisor has no start-up
  watchdog, so a slow start is never killed and restarted in a loop.
- **Waiting copies** (`ingest/d1_wait.py`, gzip): twin loads → copy skipped
  early; twin's ingest fails → copy resumes with `bytes=1048576-` and loads;
  three copies; `clear_queued` then `resume_canceled`; `requeue_failed`;
  `cancel_all_downloading` on the twin; stop and restart — every row
  reaches a terminal state, rows stored once, at most 11 GETs per copy.
- **kill -9** (`ingest/d5_kill.py`): killed after A's part went live but
  before its `done`, with C mid-download: on restart A re-ingests from the
  kept download (1 GET in total), C resumes with `bytes=393216-`, a later
  copy B skips early; 300 + 200 rows, no temp leftovers.
- **Range mishandling** (`ingest/d3_range_odd.py`): Range ignored (200), a
  wrong Content-Range span, a wrong total, Content-Encoding gzip on the
  range reply — no early skip, a full download, a sha duplicate; a
  different file loads.
- **Parse-path parity:** 48 `ingest_file` runs (json / gz / zip × fast
  on/off × small/big × in-process/pooled, two reference shapes) give
  identical rows and QA (`ingest/n10_matrix.py`); 25 further shapes agree on
  all five parser paths (`ingest/n4_paths.py`; its one DIFF line,
  `ws_and_bom`, is the harness bypassing `open_stream`).
- **References:** three same-month companions with one disagreeing → that
  id ambiguous, an id only one defines resolves; an empty table; an empty
  `in_network` plus a table (a 0-row rate file); the INBOX requeue when the
  companion arrives in a later scan, `move_processed` on and off
  (`ingest/n11_misc_refs.py`).
- **Row-rules update** from rules 0, 1 and 2: `Fee_Schedule`,
  ` Negotiated `, `fee-schedule` normalised and dollars; `percentage`
  untouched; NULL stays NULL/false; a 0-row file and a missing part are
  stamped; `rollups_stale()` is True afterwards and the next incremental
  update carries the fix into `rates_by_tin` (`ingest/n7_migrate.py`,
  `n7b_rollup.py`).
- **Round-8 reproductions:** ingest e2, e2b, e3, e5, e6, e7, e8 no longer
  show their defects; downloads r1 (1 request, was 35), r2, r5, r10, r11
  fine; outputs t1–t14 give their hand-worked answers (t1 by the NEW rule:
  $40.00 on Benchmark, Negotiate and the rate card).
- **Numbers:** `half_up` = DuckDB `round()` on 60,000 doubles at 0/1/2 dp
  and on 40,193 half-cent two-TIN medians (`outputs/t/r1_round.py`); JS
  `toFixed` / `Math.round` touch only server-rounded values or progress
  bars; Benchmark percentiles and p50 by hand on the messy fixture (MO, KS,
  national, pinned month; CQ / KX / institutional / percentage / $0.01
  lines excluded; a two-state TIN counted in both states); a subject given
  as name, lower case, "NAME · ST", TIN, dashed TIN or NPI gives the same
  Benchmark and rate-card numbers; a mapped entity = median of member TINs
  on Benchmark, card, Explorer and outreach; the flags `include_assistant`,
  `base_only=false`, `billing_class`, city, `therapy_only`, codes, pinned
  month, discipline, POS agree across Benchmark, compare, rate card, roster
  and Leads; outreach TIN-grain PCTL equals the Benchmark percentile; the
  $48.00 market median agrees on Markets, the Medicare index, Overview, the
  market report and territory; trajectory edge cases (a payer adding codes
  or practices, skipping the middle month, present in one month only, a
  non-overlapping panel); the client digest and its cache.
- **Determinism:** 43 endpoints byte-identical across two server processes
  and across first and second calls on the 99M store, apart from footer
  timestamps and the TIN-list order of 4.4 (DuckDB thread count not varied).
- **Dashboard in Chromium**, tiny and 99M stores (`outputs/ui_drive.py`,
  `ui_extra.py`, `ui_files.py`): zero page errors and zero console errors
  apart from the deliberate 503 "summary is busy" retries and export
  refusals; no "undefined" / "NaN" / "null" on any tab; a new upload loads,
  the same file again says "already loaded"; all 11 download-queue statuses
  render with the right badge and buttons; the trajectory table renders its
  matched-panel column; pitch, negotiation, rate card, proposal, dossier
  and market report open in a new tab; CSV and ZIP downloads fire; refused
  exports show the server's sentence and leave no temp file.
- **API fuzz:** the single-key GET matrix on every route found only the
  `offset` overflow; path tricks in `{filename}`, `{unit_id}`, `{code}` gave
  no 5xx; `page=10**30` → 422; `{"urls": 5}` → 422.

## Deliverables

1. One commit per group, message = what + why + verified-with-numbers,
   naming each regression test and the line it failed with on 64a371c.
2. Version 0.7.3 (`mrfx/__init__.py`, so the updater sees a newer build); GETTING_STARTED "What changed" in the user's words — lead with
   start-up ("it now says what it is doing"), re-dropping a file, and the
   provider-list month rule; HOW_TO_RUN if a button or message changed;
   AI_HANDOFF "Round 9 additions" with the rule each fix must keep;
   `docs/NEXT_STEPS_PROMPT_10.md` only if something is left.
3. Full suite green, ruff 0, `tests/slow_tests.txt` regenerated, CLAUDE.md
   counts updated; a live Chromium pass over the Files tab (> 500 files with
   one waiting for "ingest anyway"), the Rate changes trajectory and one
   pinned pitch report.
4. The start-up table of Group 2 re-measured quiet at your HEAD, with read
   calls, in the status block; the A/B timings of 4.6.
5. `git archive --format=zip --prefix=Claudevideo/ -o <scratch>/mrfx_project.zip HEAD`
   sent to the user with the update steps in plain words (close the app →
   drag the zip onto "Update MRF Explorer.bat" → start → Ctrl+Shift+R at
   http://localhost:8377), and what they will see on the first start.
6. The status block at the top of this file: what closed, the numbers, what
   is left and why, and what could not be run on Windows.
