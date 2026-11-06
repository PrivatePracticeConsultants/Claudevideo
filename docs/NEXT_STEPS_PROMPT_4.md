# Prompt for the next session, round 4 — output speed (written for Claude Opus 5.5)

Rounds 1–3 are `docs/NEXT_STEPS_PROMPT.md`, `_2.md`, `_3.md` (each has a status
block). Round 4 is about ONE thing: every screen under **Rates, Consulting,
Growth and Practice** must answer as fast as the data allows, while every number
stays exactly what it is today. Copy everything below the line into a fresh
session on this repo.

---

You are picking up MRF Explorer (`mrfx/`). Read `CLAUDE.md`, then
`docs/AI_HANDOFF.md` in full, then `git log aeced33..HEAD` (round 3: the
modifier rules, the query-layer fixes, UTC timestamps, Medicare locality,
long-grind robustness, sources by state, the download pause). The product: one
non-technical user on a Windows PC pastes payer links, then **searches by NPI
to see what each payer reimburses for PT/OT/SLP CPT codes**. The numbers are
sold. This round you make the answers FAST — without changing a single one.

## What was just found, and what it teaches

On the real 32M-rate store (1,411,001 TIN-spine rows, 93,479 named NPIs) the
Explorer's default table took **158 s** and the Code page **10.9 s per click**,
while the same queries took 0.9 s and 0.13 s on the same store BEFORE practice
names were loaded. The cause was one join in `api._TIN_JOINS`:
`LEFT JOIN npi_directory nn ON t.tin_is_really_npi AND nn.npi = t.tin_value`.
The one-sided predicate in the ON clause made DuckDB plan a
**BLOCKWISE_NL_JOIN** (every spine row against every named provider — 529
thread-seconds in EXPLAIN ANALYZE). Rewritten as an equality on an expression
(`nn.npi = CASE WHEN t.tin_is_really_npi THEN t.tin_value END`) it hash-joins:

| endpoint (named 32M store, idle Linux box) | before | after |
|---|---|---|
| `/api/rates?grain=tin&latest=1` (Explorer table) | 158 s | 1.2 s |
| `/api/summary?grain=tin&latest=1` (summary strip, cold) | 98 s | 1.1 s |
| `/api/code/97110?grain=tin` (Code page) | 10.9 s | 0.17 s |
| `/api/trend?cpt=97110` | 2.5 s | 0.06 s |
| Benchmark / rate card / market report / leads / territory / posture / renewals | 0.1–0.9 s | unchanged |

Lessons that govern this round:
1. **Measure on a NAMED store.** Every previous timing was taken before names
   existed, so a join that only costs anything once `npi_directory` is
   populated was invisible for three rounds. `scratchpad/perf/named` is such a
   store (32M rows, names loaded); `scratchpad/perf/timing_reports.py` times
   every report endpoint cold and warm — run it first, keep its table.
2. **Read plans, not code.** `EXPLAIN ANALYZE` found this in one minute after
   code reading had missed it. For every endpoint you touch, keep the plan:
   no `BLOCKWISE_NL_JOIN`, no `CROSS_PRODUCT`, filters pushed into the
   `TABLE_SCAN` (`Filters:` lines), the spine scanned ONCE per request.
   `tests/mrfx/test_known_answer_entity.py::test_no_grain_relation_plans_a_nested_loop_join`
   pins the plan shape of the grain relations; extend that idea to every
   relation the report tabs build.
3. **The user's machine is never idle.** The screenshot that started this
   showed the Code page spinning on a 16M-row store while six downloads, three
   parser workers, name enrichment and rollup updates all ran. Measure every
   fix twice: idle, and with `mrfx ingest` of two real files + enrichment
   running (the `scratchpad/real` files; a scratch config, never the real
   store).

## Non-negotiables

1. **Numbers do not move.** Before anything else, snapshot every endpoint the
   dashboard calls on the named store (`scratchpad/perf/named`) — the JSON
   bodies of `/api/rates` (each grain, latest and pooled, a filtered page),
   `/api/summary`, `/api/code/<code>`, `/api/trend`, `/api/entity/*`,
   `/api/benchmark/market`, `/api/schedule/fee`, `/api/report/market`,
   `/api/leads`, `/api/territory/*`, `/api/quality/*`, `/api/contracts/*`,
   `/api/overview`, `/api/stats`, and the CSV/ZIP exports — and diff them
   byte-for-byte after every change (timestamps and generation stamps
   excepted). The known-answer suites (`tests/mrfx/test_known_answer*.py`) and
   `test_determinism.py` must stay green; the independent Arrow recount
   (`scratchpad/xcheck/check.py`) must still report 0 differences if any
   rollup query changes.
2. **A rollup-query change costs the user a 65-minute full rebuild at 300M
   rows** (ROLLUP_SCHEMA_VERSION). Prefer query-time changes, new SIDE tables
   maintained incrementally, or directory-only changes (minutes). If a spine
   change is unavoidable, batch into ONE bump, and do P2.1 of round 3 first
   (the COPY … PARTITION_BY rebuild) so the bump costs minutes, not an hour.
3. Every claim needs a live number from the named store (and from the 306M
   synthetic store in `scratchpad/bench` if it still exists — rebuild it with
   the script there if not). "Should be faster" is not done.
4. Fix the class, not the instance; leave a plan-shape or timing regression
   test for each.
5. Commit style: what + why + before/after numbers; push only to the branch
   you are told; never weaken TLS.

## Priority 0 — what still scales with the WHOLE store per click

After the join fix the remaining costs are all "O(spine) per request" — fine
at 1.4M spine rows (≈1 s), not at the 15–20M spine rows a 300M-rate store has
(≈15 s per click, several clicks per screen).

**P0.1 "Latest" is a window over the entire spine on every request.**
`api.rel_sql` (latest branch) and `benchmark._rates_relation` run
`QUALIFY file_month = max(file_month) OVER (PARTITION BY payer, tin_value,
tin_is_really_npi, billing_code)` over every row the key/unit/ident filters
leave — with no filter (the Explorer's default view, the summary strip) that
is the whole spine, joined to three dimension tables first. Measured: the
WINDOW operator alone is 4.4 s and the projection above it 1.6 s on 1.41M rows;
`benchmark.py:185` documents the same growth (0.64 s → 1.96 s as the spine
went 800k → 3.2M). Design: a **materialized "current spine"** — the latest
row per (payer, tin_value, tin_is_really_npi, billing_code) as a table or a
boolean column `is_latest` on `rates_by_tin_tbl` — maintained by the SAME
incremental machinery that maintains the spine (payer-scoped delta: recompute
the flag for the touched payers only; the removal path already queues them).
Then `latest` is a `WHERE is_latest` that prunes in the scan, and the window
disappears from every request. Prove identity: for every (payer, unit, code)
the flagged rows equal the QUALIFY rows (a full-store SQL assertion on the
named store, plus the known-answer cases). If this needs a spine bump, see
non-negotiable 2.

**P0.2 One request scans the spine several times.** `code_detail` builds
`rel` once and runs it three times (ranked, histogram, payer leaderboard);
`/api/summary` runs it twice (strip + by-code); the Explorer page issues
`/api/rates` AND `/api/summary` AND `_filtered_count` (a third pass over the
same relation) for one view. Measure the share of each; then either compute
once into a `TEMP TABLE` per request and aggregate from it, or fold the
aggregates into one query (`GROUPING SETS` / several aggregates in one pass),
and return the count with the page. Keep the single-flight caches
(`_count_cache`, `_summary_cache`) keyed on `data_generation` — check they
actually hit during a live ingest (the generation bumps on every rollup
delta; measure the hit rate while files land).

**P0.3 The dimension joins run before the filter, for every row.**
`_TIN_REL` LEFT JOINs `tin_directory`, `entity_map` and `npi_directory` and
evaluates the therapy-taxonomy LIKE chain (`therapy_taxonomy_sql`, ~1 KB of
SQL per row) over the whole spine, then filters/paginates. The late-join path
(`_tin_late_join_ok`) exists for the pooled TIN table only; `latest`, every
name/geo filter, the entity grain, the Code page and the summary pay the full
join. Extend "filter and page first, join names onto the page" to the latest
path (once P0.1 makes `latest` a scan filter this is natural), and move
`is_therapy`/state/city predicates onto `tin_directory` keyed lookups
(semi-join on `tin_value`) instead of projecting them for every row.

**P0.4 The Code page's rows render AFTER the slowest query, and a second click
restarts everything.** `selectCpt` awaits `/api/code` and `/api/trend` together
and clears the table while waiting; the state-filter input and a re-click fire
another pair with no cancellation (the screenshot: leaderboard and histogram
from the previous response on screen, "Loading 95992" spinner for the new
one). Render the ranking as soon as `/api/code` returns, load the trend after,
abort superseded requests (AbortController), and show per-section skeletons —
no API change needed.

## Priority 1 — the whole-store screens

- `/api/overview` (Practice → Overview) does several whole-spine GROUP BYs
  behind a single-flight lock; measure it on the named store under load and
  move its aggregates onto the current spine (P0.1) or into per-generation
  cached rollups.
- `/api/stats` is now metadata + spine counts (round 3), but
  `enrichment_progress` still does `count(DISTINCT npi)` over the raw rates
  view behind its own 60 s cache; derive the total from the files' QA
  (`rows`/distinct facts) or a per-generation materialized count.
- Entity grain (`_entity_rel`) aggregates member TINs over the whole filtered
  relation; with P0.1 and P0.3 the input shrinks to the page's units — verify
  with the entity known-answer cases that nothing moves.
- Exports (`export_select`) stream the full relation ordered by `order_sql`'s
  9-column tiebreak; measure a 1M-row CSV and ZIP; the sort should run on the
  scan-pruned relation, not the joined one.

## Priority 2 — the store side of speed

- **P2.1 of round 3 (COPY … PARTITION_BY full rebuild)** — needed before any
  spine bump; 65 min today at 306M rows.
- **Sorted parts** (round 3 P2.2): sort each part by (payer, tin_value,
  billing_code) at write time so the NPI view's payer/code filters and the
  directory slices prune row groups; measure the NPI view "code only" (30–41 s
  at 306M) and "payer only" (51 s) before and after; parts written before the
  change stay unsorted — handle both.
- DuckDB has no query priorities: a rollup delta (4 threads, the memory pool)
  and a dashboard click compete. Measure dashboard latency DURING a rollup
  delta on the named store; if it doubles, consider running deltas with fewer
  threads while requests are in flight (a cheap "requests seen in the last 5 s"
  counter), never by starving the delta entirely.

## Report

End with: (1) the endpoint timing table — named store, idle AND under ingest,
before and after each change, plus the 306M store if available; (2) the plan
shape (operator list) of every relation you changed; (3) the byte-for-byte diff
of every endpoint snapshot (expected: empty) and the recount result if a
rollup changed; (4) which items you left and why; (5) what you could not
measure — say so rather than rounding up.
