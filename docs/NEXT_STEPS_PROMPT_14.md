# Prompt for the next session, round 14 — prove the analytics take in every new file and show it happening; close what round 13's own new code broke; no steps backwards (written for Claude Opus 5.5)

> **STATUS: not started.** Baseline for every number and line reference below is
> commit 9617c59 (version 0.7.7), the zip the user installed. When you are done,
> replace this block with what closed, in which commit, with the numbers.

## What the user asked for, in their words

1. "Audit the 0.7.7 zip deeply."
2. "Are we sure the analytics are actually adding newly loaded files? Add a
   loading screen for those so we can actually see it process the files."
3. "Make any other changes you feel are needed. **No steps backwards, only
   forwards.**"

The third sentence is the bar for this round: every fix keeps every earlier
fix. Round 13 shipped two regressions (2.1 and 2.3 below) because its own new
code broke an older guarantee and no test pinned the end-to-end case. Each
item here therefore ends with a test that fails on 9617c59 AND the repros of
rounds 11–13 re-run on the final code.

## How this round was audited

- **The 0.7.7 zip**: identical to 9617c59 (544 files), version, CRLF
  launchers; a fresh install; the 0.7.6 → 0.7.7 update on a loaded real store
  (2.1 s, settings kept, exported rows byte-identical); the 3-of-N stale-store
  scenario served by 0.7.7 (header, chips and rate-card footer truthful).
- **Full suite of the extracted zip: 1 failed, 1246 passed, 44:09** (box shared
  with four reviewers). The failure, `test_fetch.py::test_toc_url_expands_and_children_ingest`,
  passes alone (12/12) and was reproduced under load by the session lead: **1
  of 30** runs ends with the rate file `failed — "already being ingested by
  another worker"` — item 2.3, a race round 13's own fix introduced.
- **Four independent reviews**, every finding with a script and its output
  (`docs/review14_repros/`):
  - `analytics/ANALYTICS_REPORT.md` — the user's question, answered with live
    runs: links, inbox, kills mid-fold-in, failures, a busy queue, the 0.7.6 →
    0.7.7 upgrade with a stale spine, and a new payer added to the
    99,000,000-row store (Group 1).
  - `diff_data/REVIEW.md` — the round-13 rollup / identity / numbers code.
  - `diff_ui/REVIEW.md` — the progress bar, hot backups, PDF, links + search,
    the Group-3 plumbing, and every output clicked in Chromium.
  - `regress/REGRESSION_REPORT.md` — "no steps backwards": every repro of
    rounds 11–13 re-run against 0.7.7: **58 held, 5 changed on purpose, 1
    regressed** (2.1 below). Accuracy re-check on a real three-file store:
    **TX 734 / WY 202 checks, 0 differences**. Determinism: 200 output calls,
    byte-identical with and without a progress token (after the documented
    "Generated …" stamps). Config continuity: a 0.7.6 config with all 48 keys
    set to non-default values is read identically by 0.7.7, and 0.7.6's own
    updater carries it across.

Severities: **P0** wrong number, data loss, double rows, a real file never
loaded, a backup that does not restore, an SSN-pattern TIN shown; **P1** stuck
state, user action undone, two screens disagreeing; **P2** waste or a
misleading message; **P3** cosmetic.

---

## Group 1 — Do the analytics take in every newly loaded file? Prove it, fix it, and show it

### 1.1 The answer today (from `analytics/ANALYTICS_REPORT.md`)

(FILLED FROM THE REPORT — see the section inserted below.)

### 1.2 The analytics activity screen (the user's "loading screen") — build it

The header line round 13 added says *how much* is covered. The user wants to
*watch it process the files*. Build a screen that makes the fold-in visible,
truthful and boring to read:

- **Where**: clicking the header's coverage line (or "Analytics" in the Files
  tab) opens an **Analytics activity** panel — a full-width section at the top
  of the Files tab, also reachable as `#analytics` (Group 6.4's links).
- **What it shows, live (poll every 2 s while open, through the async
  progress channel from round 13 — never blocking behind the pool)**:
  1. A big **0–100 % bar** for the current fold-in with words: "Adding 37
     newly loaded files — payer *Blue Cross of X*, part 3 of 8 — 1.2 M rows/s —
     about 14 min left" (the plan's slice list, `next`, the slice label, rows
     done / total, measured rows/s, ETA — `Store.rollup_job()` and the plan).
  2. **Waiting to be added** — the list of files that are `done` but not in
     the analytics yet, newest first: file, payer, rows, loaded at, and *why it
     is waiting* in one phrase ("waiting for the queue to pause", "next in
     line", "after the full build finishes", "retrying after an out-of-memory
     stop at 10:42"). Count and total rows at the top.
  3. **Added recently** — the last 50 files taken into the analytics with the
     time each became visible on screen, how long it waited, and which kind of
     update did it (payer update / full build part / name refresh).
  4. **Per payer** — one row per loaded payer: covered ✓ / part-way (k of n
     parts) / waiting, rows in the analytics vs loaded, last updated.
  5. **Problems** — the last failure sentence, when it retries, and what the
     user can do (raise `duckdb_memory_gb`, free disk); a **Run now** button
     that asks the queue to fold in at the next safe moment even while
     downloads continue (the scheduler's idle rule must not be the only
     trigger — see 1.1), and a **Pause analytics** toggle for a laptop on
     battery (persisted; the header says "paused").
  6. A plain-language **"What this means"** box: "Files load in minutes; adding
     them to the numbers every screen reads takes longer on a big store. Until
     a file is added, its rates are stored safely but not in any report."
- **Server**: one route `GET /api/analytics/activity` (async-safe, cheap: the
  files table, the plan, the job, a ring buffer of the last 200 fold-in
  events kept in `meta` so it survives a restart) and `POST
  /api/analytics/run-now` / `POST /api/analytics/pause`. Record an event per
  file when it becomes covered (the moment the coverage marker or the plan's
  committed slices first include it) — this is also the evidence the tests
  read.
- **CLI parity**: `mrfx status --analytics` prints the same panel as text.
- **Honesty**: the "waiting because" phrase comes from the scheduler's actual
  state, never a guess; the ETA is labelled "about" and appears only after
  one measured slice; a number on the panel must equal what
  `/api/stats.analytics` says at the same moment (test).
- **Tests**: a file loaded while serve runs appears under "Waiting" within one
  poll and under "Added" once covered, with times; Run now folds in during a
  busy queue; Pause stops new fold-ins and the header says so; the panel
  survives a restart with its history; byte-identical outputs with the panel
  open.

---

## Group 2 — Round 13's new code broke older guarantees (P0/P1; "no steps backwards")

### 2.1 (P0, regression vs 0.7.6) A link whose ingest fails never releases its content claim

`Store._content_claims` (added in round 13 for 5.2.1) is released only on a
successful load (`ingest._finish_ok`, `ingest.py:420`; claim at
`store.py:3478`). After a failed load:
- the same document under a second link is `skipped/duplicate` "being loaded
  right now — will retry automatically" and never retries (0 rate rows; on
  0.7.6 it loaded);
- an inbox copy waits forever in `_content_claim`'s 2-second loop — the inbox
  watcher freezes (the same class as round 12's 1.3);
- only a restart clears it.
Repro (re-run by the session lead): `diff_data/scripts/s11_failed_link_claim.py`
→ `content claims held = {…: '43c69dd4_a.json.gz'}`, `rate rows: 0`, `inbox
copy: scan_inbox returned within 20 s: False`; also `regress/…/c_inbox_wait.py`,
`c_trace.py` (no release call after the failure). Round 13's own after-fix run
printed `rates: 0` for `skip/s4_claims.py` case C and nobody read it. Fix:
release in a `finally` on every exit path; revive the deferred twins on failure
(as the raw-sha path does at `ingest.py:1300-1310`); `_content_claim` must
never wait on a claim whose owner is not alive in this process. Pin the
end-to-end case (fail → twin loads; fail → inbox copy loads) — the existing
tests call the revive step directly while the first link is still loading,
which is why the suite missed it.

### 2.2 (P0) The content fingerprint protects none of the files loaded before 0.7.7

`backfill_identities` (`ingest.py:2060`) selects only rows with `full_sha IS
NULL`; every 0.7.6 file has a `full_sha` and no `content_sha`, so 5.2.1 covers
**none of the user's 615 files**: a re-encoded copy of an old file loads twice.
Repro (re-run by the session lead): `diff_data/scripts/s8_legacy_twin.py` →
`backfilled: 0`, `rate rows: 120 (one document = 60)`. Fix: backfill
`content_sha` too, from the processed copy or the kept download, in the
background, one file at a time, resumable, with a Files-tab line ("naming 412
older files by content — 37 %"); a file with no copy is marked so the panel
can say so. Measure the cost on the real files (round 13: +0.8–1.0 s per GB
inflated) and state the expected time for 615 files.

### 2.3 (P1, regression from 5.2.7) Two re-reads of one rate file collide; the file ends `failed`

When a provider list lands, the rate file's own `_reread_if_refs_landed`
(`ingest.py:1313-1360`) and the list's `requeue_skipped` pass can start the
same re-read at once; the loser's `_claim_ingest` returns `{"status":
"skipped", "error": "already being ingested by another worker"}`
(`ingest.py:1287`) and the link row ends **failed**. Reproduced by the
session lead under load: 1 of 30 runs of the index test (`test_toc_url_
expands_and_children_ingest`), child `failed 'already being ingested by
another worker'`. Fix: a lost claim on a RE-READ defers to the winner (no
status change, or wait for it), and one place owns the re-read decision. Pin
with a deterministic test that forces both re-read triggers at once, and run
the index test and `test_parallel_ingest_matches_serial` 50× each on the
final code with 0 failures.

### 2.4 (P0) The hot backup can lose the rows of a file committed during it, and still verifies OK

`backup.py:246` lists the store's files before taking `store.write_lock` at
`backup.py:266`; a part swapped in between is in the copied database (its
files row, its rows in `count(*)`) but not in the zip. Repro (re-run by the
session lead): `diff_ui/r03_backup_lists_parts_before_lock.py` → manifest
"recorded 840 rate rows", `mrfx verify` OK, **restored store 640 rows, 200
lost**. Fix: list the parts inside the lock (or re-list and compare after
taking it), and make `verify` restore-check the row count from the parts in
the zip, not from the manifest. Also from the same review: a stopped or failed
backup leaves `<name>.zip.part` on the backup drive forever (P2-2); a
`backup_dir` inside the store folder makes every backup contain all earlier
ones (P2-5 — refuse it); a store on a drive that cannot hard-link copies the
whole store under the write lock and never checks the store drive's free space
(P2-6); the lock is held ~25 s per GB of database file (P2-8 — say it in the
docs and the Files-tab line).

### 2.5 (P0) An SSN-pattern TIN reaches the shareable address bar and the new PDFs

The Clients box accepts SSN-pattern TINs (`clients.add_client`,
`clients.py:41-58`) and older surfaces show them raw (digest, packet folder
names, titles); round 13 added the address bar (`#ratecard?subject=070000001`,
`app.js:2949`) and the PDFs. Invariant 4: masked everywhere. Refuse an
SSN-pattern subject in `add_client` (or key it internally and show it masked),
never put one in `setHash`, file names, titles or PDFs; a test enumerates
every surface. Repro: `diff_ui/` P0-2.

### 2.6 (P1) A full plan clears a "needs full" raised during it

`store.py:5415`: a full plan that starts with `rollup_needs_full` set clears
the flag at its end, including a needs-full raised mid-plan by an unscopable
forget — the forgotten file's rows stay in the spine (114 extra rows), state
"current". The same rule was in 0.7.6's one-shot build; the plan keeps the
window open across restarts and days. Repro `diff_data/scripts/s2_midplan.py`
(d). Fix: a generation counter on the flag; clear only the generation the plan
started with.

### 2.7 (P1) During a full plan over an existing spine the screens disagree

Header and every document sentence: "Analytics cover 0 of 4 payers (8 of 8
files)… the files loaded since are not in the analytics yet" — nothing was
loaded since; the payer chips mark 3 of 4 covered; all 4 payers answer
`/api/rates` (`store.py:6560` vs `api.py:4979`). Repro
`diff_data/scripts/s3_names_plan_and_header.py`. One coverage function feeds
the header, the chips, the panel (1.2) and the documents; during a rebuild of
an EXISTING spine the old rows are still there and the sentence must say
"being rebuilt", not "not in the analytics".

### 2.8 (P1) `monthly_recheck` (on by default) downloads every NEW file a re-fetched index lists

With `monthly_refresh` off, round 13 decided the re-check only probes loaded
files; it actually queues and downloads the new children too
(`diff_data/scripts/s13_recheck_new_children.py`), contradicting the decision
and the config comment. Either honour the decision (probe only) or make it the
documented behaviour — decide with the user's bar (a re-check that silently
starts downloading next month's 20 GB is not what "check" means).

### 2.9 (P1) Stop poisons caches

- Stop on the Clients digest caches the "stopped" per-client errors: every
  later digest and the monthly packets say "stopped" for every client until the
  data changes (`clients.py:200`, `:108`; `diff_ui/r01`).
- Same class: size premium cached as missing after a Stop (`quality.py:455`;
  `diff_ui/r02`); the roster freshness line (PLAUSIBLE).
Rule: a result computed under a cancelled Progress is never cached. Enforce it
in `Store.cached` / `generation_cached` (check `progress.current().cancelled`
before storing) and pin it with a test that Stops every cached output once.

### 2.10 (P1) Run buttons stay disabled after two overlapping runs

Enter pressed twice leaves Roster's Run disabled every time and Medicare's in
1 of 2 attempts, until a reload (`app.js:126/143`; `diff_ui/` P1-2b, Chromium).
A superseded run must re-enable the button it disabled.

### 2.11 (P1) `files.read_identity` is consulted only by the same-name check

After a kill between the done commit and the identity row (1427445's window),
a byte-identical or decoded copy under another name loads twice
(`diff_data/scripts/s10_killwindow_twin.py`, 120 rows for 60). Make every
claim path consult it.

---

## Group 3 — Waste, wording and honesty (P2/P3; one commit)

- **Names-only plan killed → whole-store full rebuild** (`s3_…` (a): a names
  plan at 1 of 17 became a full plan of 287 slices); the header calls a names
  plan "A full analytics build". Resume a names plan as a names plan.
- **5.2.1 skips a twin whose FILE NAME carries a different month** and the
  month is lost when it comes from the name (header without a date, or dated
  in the future): c8b80fc loaded 2026-05 and 2026-06, 0.7.7 only 2026-05
  (`s7_month_twin.py`). Byte-identical twins already did this in 0.7.6. Decide:
  when two copies of the same content disagree on the month their names imply,
  load once and record both months? or refuse with a sentence asking which?
  Never silently drop a month.
- **The 4.2 rule sentence** is missing from the leads CSV, the market report,
  the dossier, the export sidecar and the rate card / fee CSV (`s5`) —
  round 13 promised "every all-payer figure".
- **A hospital file loaded without `--state`** never reaches a document and
  the reason blames payer-name matching (`s6`); a second hospital with the
  same name overwrites the first.
- **Stop during an export's "Compressing" stage** is ignored until the zip is
  done, then 409, and the zip stays in temp for 6 h (`diff_ui/r06`).
- **PDF**: Stop does nothing while Chromium prints (18 s), a second PDF waits
  38 s behind it, a very large report is a bare 500 and the print triggered an
  out-of-memory kill (`r07`, `r08`): print in a child process with a timeout
  and a page budget, Stop kills it, a refusal sentence above the budget. `mrfx
  packets --pdf`: one failing print aborts the batch with a traceback (`r09`
  family) — skip that document with a sentence and continue.
- **An iframe download's bar sticks** and Export stays disabled for 6 h after a
  server restart mid-build (polling 404s) — a 404 on the poll ends the bar with
  "the app restarted — run it again".
- P3: Stop on a CSV/ZIP export pops "not built: stopped" as an alert; a Stop on
  a finished run answers `{"stopped": true}`; the CLI bar on redirected output
  prints a line per inner step; inner-step wording ("Finding this practice")
  flashes inside the dossier; Back leaves the app instead of the previous tab;
  the "Re-read N files" count includes files with no copy and no link;
  `visit_market.py:42` docstring still describes the old quartile rule; a
  dropped plan from another rules version can leave an orphan `__next` table.
- **Docs now false** (from the regression sweep): `AI_HANDOFF.md:318` "Rebuilds
  give up after 3 failures" (they no longer do; line 815 says so);
  `GETTING_STARTED.md:176-177` "the database's 8 GB: 2 on a 16 GB machine"
  (code: 6 GB and 3–4 workers; lines 609-610 say 3); `AI_HANDOFF.md:1159` "the
  suite (670 tests)" (1247); `AI_HANDOFF.md:1152` a dated quick-tier count;
  comments `config.py:127` "one HEAD per file", `cli.py:128` and
  `fetch.py:1986` "OPT-IN"; GETTING_STARTED's "plain sentences" claim holds
  for the inbox only — a damaged gz via a link still shows "CRC check failed
  0x…"; a multi-link paste is not atomic (a kill during `add_urls` drops the
  rest of the list); an enrichment log line says "4,007 of 3,511 TINs
  affected".

---

## Group 4 — Hardening that keeps the next round from shipping a regression (one commit)

Round 13's executor ran its repros and recorded the output, but one output
(`rates: 0`) showed a regression nobody read. Make the evidence check itself:

- `tests/mrfx/test_repros.py` (slow tier): runs the round-11–14 repro scripts
  that finish under ~30 s in a scratch dir and asserts the "after the fix"
  line each round recorded (a small table in the test: script → expected
  substring). The 58 "held" scripts of the regression sweep are the starting
  list (`regress/REGRESSION_REPORT.md` has them with their outputs).
- A stress mode for the two race tests (2.3 and round 13's 5.2.7): a pytest
  marker that runs them N times with a CPU hog beside them, in the slow tier.
- The full-suite line in every status block is from a run AFTER the last code
  change and lists any test that needed more than one attempt (none may).

---

## Carry-overs (do, or write why not)

Round 11's 3.6 segment map; 4.4(h) monotonic clock through laptop sleep;
4.4(c) filename cap; 6.5 Excel and 6.6 expected payment stay decided as round
13 decided them unless something changed; Windows has never been run — if a
Windows box is available, the install → start → paste → update → Ctrl-C path
plus a hot backup on an exFAT USB drive is the most valuable hour.

## Checked and correct in 0.7.7 (do not spend time here)

(From the four reviews.) The zip and the update path; the sliced build
byte-identical to 0.7.6's one-shot build on the same parts (spine 1729/1729,
directory 36/36 at four slice sizes up to 287 slices, with shared TINs,
NPI-in-the-TIN-slot, npi-typed TIN vs EIN collisions, SSN TINs, quoted payer
names, months split over files, `is_latest`, placeholders, hash halves);
mid-plan land / forget heal to identical tables (except 2.6); outreach (tin
and entity grain, five filter sets) and Overview (four states) byte-identical
old vs new on a store with ties, negative gaps, placeholder-only practices,
masked TINs and an entity map; the 4.1 quartile at every tied boundary; the
5.2.4 re-read replaces the part atomically; four orderings of the provider-list
race (round 13's fix) except the double re-read of 2.3; fingerprints of zip,
multi-member and truncated gzip; new ORDER BYs; the progress plumbing (no
context leak, no sampler threads left after 200 runs and 50 Stops, registry
capped at 256, settings on their own connection, poll and Stop ~3 ms under a
saturated pool, background threads never cancelled by a Stop, 19 routes
byte-identical with and without a token, the token never in an export, Stop →
409 on 14 outputs with no page errors); no XSS through hostile org names, the
search box or the hash; caches after a forget; side files bounded; CLI on
cp1252; PDF basics (logo, Letter landscape, identical except the dates, a
sentence without Chromium); backup retention and schedule across restarts;
58 earlier repros held; accuracy 0 differences; determinism 200/200; config
continuity 48/48 keys.

## Deliverables

1. One commit per group in order 1 → 2 → 3 → 4 (Group 2 may be split where
   items touch different modules); each message: what, why, the numbers, the
   items it closes.
2. `tests/mrfx/test_review14.py`: one test per item in Groups 1–3, each
   FAILING on 9617c59 with the value in its docstring (prove it on a `git
   archive 9617c59` copy — strip the editable install's fallback so the old
   copy imports only itself, as round 13's executor had to).
3. Group 1's live scenarios re-run on the final code (the analytics report's
   scenario table, before/after), including the 99M-store new-payer fold-in
   and the 0.7.6 → 0.8 upgrade with a stale spine; screenshots of the
   Analytics activity panel during a real fold-in on the 99M store, looked at.
4. All round-11–14 repros re-run (Group 4's test does it); rows unchanged
   through ingest (BCBS-MS, UHC-NY, Tufts via inbox and links; `fastcheck`
   3/3); the round-11 kill harnesses once; accuracy `check_real.py` 0
   differences; determinism with and without a token.
5. Version **0.8.0** (the analytics panel and the verified fold-in are a
   visible step for the user); GETTING_STARTED "What changed in 0.8.0" in the
   user's words with a description of the panel; AI_HANDOFF updated (the
   fold-in contract: when a file is added, by which path, how the panel knows;
   the claim-release rule; the cache-under-Stop rule; the backup consistency
   rule); CLAUDE.md counts; `tests/slow_tests.txt` from a quiet full run.
6. Full suite green after the last code change with no retried test; ruff 0;
   pip-audit clean; the zip built and checked (version, CRLF, identical to the
   commit); this status block filled in.
