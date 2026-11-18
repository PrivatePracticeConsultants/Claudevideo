# Adversarial review of the round-12 diff (69d4fdb..c8b80fc, v0.7.6)

Read-only review of `git diff 69d4fdb..c8b80fc -- mrfx/` against the seven
CLAUDE.md invariants. Every CONFIRMED finding below has a script in this
folder (`/tmp/claude-0/-home-user-Claudevideo/4633d594-0139-5f8e-a935-ae9c76b72000/scratchpad/audit13/diff/`)
and its output pasted; anything established by reading alone is marked
PLAUSIBLE. Scratch stores only (`scratch_*` under this folder); the 99M-row
store was not touched. Interpreter: `/home/user/Claudevideo/.venv/bin/python`.
The shipped `tests/mrfx/test_review12.py`: **27 passed in 97 s** on this box
(`pytest_review12.out`).

Severity: P0 wrong number / data loss; P1 stuck state, crash, user action
undone; P2 waste or misleading message; P3 cosmetic.

**No P0, no P1.** The numbers path (`mrfx/visit_market.py`) held every
hand-worked case (34/34, below). One P2 regression against 0.7.5 in the
index re-check, one P2 waste, two P3s.

---

## P2 — CONFIRMED, regression vs 0.7.5: a 0.7.4-loaded child whose server cannot state a size is now silently left at the old month on the monthly refresh

**Where:** `mrfx/fetch.py:2922-2927` (`_refresh_loaded_children`):

```python
then_size = r["server_size"] or r.get("loaded_size")
verdict = _republished({"validator": r["server_validator"], "server_size": then_size}, now)
if verdict: changed.append(r["id"])
elif verdict is None and parent_reval == 1 and now is not None \
        and not r["server_validator"] and not then_size:
    unknown.append(r["id"])
```

Item 2.3 added `loaded_size` (`content_fingerprints.size`, `store.py:3615-3619`)
so a 0.7.4 row is compared by size instead of re-downloaded. But the guard for
the `unknown` branch was changed from "nothing recorded on the row" to
"nothing recorded AND no loaded size". When the 1-byte probe **answers but
states no size and no validator** — a Range-ignoring server streaming a
chunked 200 with no `Content-Length` (the shape round 11 added the
"one 64 KB chunk, then close" probe handling for, so it is inside the tested
envelope) — `_republished` returns `None` (`fetch.py:2615-2626`: `ts` set,
`ns` None), the row is no longer "unknown" (then_size is set), so it is
neither re-downloaded nor noted. 0.7.5 sent exactly this row to the monthly
re-download ("nothing was recorded to tell whether the payer changed it").
The republished file never loads; the row stays `done` with no message.

**Repro:** `p_g_loaded_size_unknown.py` — one index, one child loaded, the row
reset to what 0.7.4 left (`server_validator`/`server_size` NULL), the payer
republishes June, the server now answers the probe chunked with no size,
`revalidate = 1` (the monthly refresh), drain. Output (`p_g.out`):

```
loaded: [('2026-05',)] loaded_size: [(656,)]
requests: [('/index.json', 200, None), ('/files/r.json.gz', 200, 'bytes=0-0')]
   (1, 'done', 'toc', None)
   (2, 'done', 'in_network', None)
file months now: [('2026-05',)]
VERDICT: LEFT AT THE OLD MONTH — never re-downloaded, no note on the row
```

The shipped test for 2.3 (`test_refresh_of_a_child_loaded_by_0_7_4_compares_the_loaded_size`)
only covers the server that DOES answer with a size, so it cannot see this.

**What should happen:** when `now` carries neither a validator nor a total,
the row is exactly as unknown as it was in 0.7.5 — on the monthly refresh
(`parent_reval == 1`) send it to the re-download whatever `loaded_size`
says, i.e. test `not (now.get("validator") or now.get("total"))` rather than
`not then_size`; or at minimum write a note on the row ("the server could
not say whether this file changed"). A one-line test: the repro above must
end `2026-06`.

---

## P2 — PLAUSIBLE: `pooled_month_files` is a whole-spine aggregate that re-runs on every data generation, including name refreshes, on the Files tab's poll

**Where:** `mrfx/quality.py:161-191` (`@generation_cached` → keyed on
`(data_generation, user_edit_generation)`, `store.py:4940`), called from
`/api/files` (`api.py:2438-2441`, the dashboard's poll), `api.methodology_text`
and `benchmark.methodology_footer`. The spine query

```sql
SELECT payer, file_month, count(DISTINCT tin_value) FILTER (WHERE source_count > 1), ...
FROM rates_by_tin GROUP BY 1, 2
```

runs whenever the store has at least one payer × month loaded from more than
one file — which is always true for the BCBS-MS `01_of_10` user — and the
generation moved. `data_generation` is bumped not only by rollups but by the
enrichment name refresh (`store.py:4586`, `_bump_generation(spine=False)`),
which runs every `_DIRECTORY_REFRESH_SECONDS = 240 s` (`enrich.py:84`, adaptive
upward) while NPIs are being identified. The spine does not change on a name
refresh, so each of those recomputes gives an identical answer. `Store.cached`
is not single-flight: two Files polls landing after one bump both compute it.

**Measured (synthetic, `pooled_cost.py`, 4 DuckDB threads, in memory):**
`aggregate over 20M rows: 1.17 / 0.99 / 1.15 s` → roughly 5–6 s per 99M rows
from memory, more from parquet on the user's external drive. So: a ~5 s stall
on the Files poll (and in every export's methodology) up to every 4 minutes
during an enrichment run, for a line that cannot have changed. Not a wrong
number; waste. Labelled PLAUSIBLE because the 99M figure is an extrapolation,
not a run on the real store.

**What should happen:** key the cache on `spine_generation` (the aggregate
reads only the spine; `spine_materialised` already uses that key), or fold
the three counts into the rollup that builds the spine and read them from
`meta`.

---

## P3 — CONFIRMED: the new config comment gives the wrong numbers for the automatic memory rule

**Where:** `config/mrfx.yaml:209-211` says "room for the parser workers
(**about 1 GB each**) … **about 4 GB on a 16 GB laptop**, 12 GB on a 32 GB
desktop". The code (`store.py:98-108 auto_pool_gb`, `PARSER_WORKER_RAM_GB = 2.0`;
`fetch.py:2034-2046 _ram_worker_cap`) gives (`p_d.out`):

```
8 GiB:  pool 2 GB,  parser workers 1 (x 2.0 GB), OS 3.0 -> planned 7 of 8.6 GB
16 GiB: pool 6 GB,  parser workers 4 (x 2.0 GB), OS 3.0 -> planned 17 of 17.2 GB
32 GiB: pool 12 GB, parser workers 9 (x 2.0 GB), OS 3.0 -> planned 33 of 34.4 GB
this box: 16.87 GB total, 14.20 GB avail -> 6
```

A 16 GB laptop gets a **6 GB** pool (not 4), and each worker is budgeted at
**2 GB** (not 1). The 32 GB line is right. The rule itself is sound (and the
old shipped `duckdb_memory_gb: 8` beside the workers' budget is gone); every
reader of `cfg.duckdb_memory_gb` tolerates `None` (`store.py:1038/1060`,
`fetch.py:2045`; the others are message text). **What should happen:** the
comment says 6 GB / 2 GB each — or the two constants are quoted from the code.
(`GETTING_STARTED.md:176` still reasons from "the database's 8 GB".)

---

## P3 — CONFIRMED (documented Clear-queue semantics, noted so it is not mistaken for 1.5 regressing): a child claimed by a downloader in the instant after the re-queue finishes after "Clear queue"

**Where:** `store.py:3787-3803 clear_queued` flips `queued / fetched /
expanding / paused` and, by design, leaves `downloading` rows alone;
`requeue_republished` (`store.py:3621-3660`) puts republished children back
to `queued` while downloader threads are live.

**Repro:** `p_c_clear_window.py` presses Clear in the window AFTER
`requeue_republished` returned and BEFORE the index's final write (the one
window item 1.5's fix does not cover, because there is nothing left to
guard). Output (`p_c.out`):

```
cleared: [3]                       <- the index + two children; the third was already 'downloading'
   (1, 'skipped', 'canceled', 'cleared from the queue')
   (2, 'done', 'in_network', None)  <- finished its download and loaded June
   (3, 'skipped', 'canceled', ...)
   (4, 'skipped', 'canceled', ...)
file months: [('2026-05',), ('2026-06',)]
after a second drain: unchanged
```

Nothing is stuck and nothing comes back on the next drain (the fixed
behaviour of 1.5 holds — compare the shipped test, which presses Clear during
the probes). The residue is one payer at June while its siblings stay at May,
which is what "Stop downloads" exists for. Cosmetic; worth one sentence in
the Files tab's Clear tooltip ("a file already downloading finishes").

---

## Probed and held

### 1. `mrfx/visit_market.py` and `practice_medians_disc_payer_v1` — 34/34 hand-worked checks (`vm1_handworked.py`, output `vm1.out`)

Store: MO / PT / `ortho` (97110 + 97530 + 97140 + 97112 × 1 unit, so a visit
= the sum of four rates). Eleven TINs, three payers, two months, built
through `scan_inbox` + `save_npi` + `update_entity` (the UI's entity path) +
`rebuild_rollups`. What each piece tested and what came back:

| Rule | Fixture | Result |
|---|---|---|
| latest month per contract, modifier spelling change | T1: May bare (`unspecified`) $10, June GP $20 under Payer A | A counts T1 at $80 (June); pinned `2026-05` counts $40 |
| `tin_is_really_npi` excluded | T8 under `tin.type = "npi"` with all four codes | 4 spine rows flagged, not in any count (A = 7 practices, not 8) |
| non-dollar row | T9: 97140 as `percentage`, other codes dollar | incomplete (A `incomplete_excluded` 1) |
| discipline line filter | T11 priced on GO lines only | out of the PT market |
| TIN in two states | T7: one NPI in MO, one in KS (`states ['KS','MO']`, `primary_state KS`) | counted in MO |
| complete-basket rule | T2 missing 97140 under Payer B | excluded, B `incomplete_excluded` 1 |
| per-payer stats vs `quantile_cont` | A 80 100 120 140 160 180 200; B 120 140 160 180 200 200 | A (7, 140, 140, 110, 170); B (6, 166.67, 170, 145, 195) |
| suppression < 5 | Payer C: 2 practices | `(None, None, None, None, None, 0, True)`, listed in `suppressed_payers` |
| all-payers row = median of per-payer values, never a sum | T1 med(80,120,40)=80 … | (7, 141.43, 150, 105, 185) |
| one-TIN subject | T3 | A p33 of 7, Q2, −$20.00 (−14.3 %) vs both; B p20 of 6, Q1; All p33 of 7, Q2 — subject value 120 is the same number as T3's population value |
| v == median boundary | T4 in A (140 = median) | Q3, p50 of 7, ±$0 |
| mapped two-TIN subject (both in MO) | DUO REHAB = T5 + T6 | value = median of its TINs' rates (A $170, B $200, All $185); `others` exclude both TINs (p80 of 6 / p100 of 5 / p80 of 6); market still counts 7 practices |
| subject spanning two states (round-9 rule) | SPAN REHAB = T4 (MO) + T10 (KS) | A value $210 = median(35, 70) — the KS TIN is its own; others exclude T4 only → p100 of 7 |
| subject not in the market's state | T10 (KS) | ranked among 7 + itself: p100 of 8; Payer B `subject: None` |
| payers filter | `payers: ["Payer B"]` | rows `[Payer B, All payers]`; All == B; subject's all-payers value = its B value |
| month pinned | `2026-05` | rows `[Payer A, All payers]`, A (7, 134.29, 140, 110, 170), `as_of_rule "only the 2026-05 files"` |
| refusals | no state / no discipline / KS / SLP / unknown custom code | 422 sentences (`pick a state`, `pick a discipline`, `no payer prices this visit`, `99999 is not a therapy code this app knows`) |
| CSV == JSON == printable | DUO case incl. a suppressed payer with no visit, and T3's negatives | every cell equal; HTML prints `-$20.00 (-14.3%)` |
| determinism | two calls; after `rebuild_rollups` | byte-identical |
| entity-map edit invalidates the cache | T7 added to DUO via `update_entity` | A value 170 → 180, `others` shrink to 4 (p100 of 5), `subject_tins` 3 |
| spine change rebuilds the per-discipline table | July file: T3 under A $50 | T3's A value 120 → 200; A stats (7, 151.43, 160) |
| `_position` boundaries (pure) | v = p25 → Q2, v = median → Q3, v = p75 → Q4, p25 == median → Q3; ties 0 below + ½·3 over 4 = 37.5 → 38; no others → `None` | as SUBJECT_NOTE states |
| `MIN_PRACTICES == market._MIN_CELL` | | 5 == 5 |

Also by reading: the mid-rank formula in `_position` (`(below + 0.5·tied) /
len(others)`) is algebraically `leads.midrank_pct_sql` with the subject in
the partition (`(rank−1 + 0.5·(ties−1)) / (N−1)`); `_house_disc_table_sql`
orders by `(billing_code, tin_value, discipline, payer)` — a unique key, so
the materialised parquet is reproducible; the market dict is rebuilt from
`month/state/discipline/payers` only, so a Rate card's `city` / `therapy_only`
cannot leak in; `_visit_value` rounds rate → line → total exactly as
`price_visit` does (confirmed numerically: T3's row value equals its
population value).

One note, not a finding: a mapped multi-TIN practice is counted as N
practices in the market's `practices` / mean / P25 / P75 while the subject
is "counted once" for its percentile. `MARKET_NOTE` and AI_HANDOFF say "a
practice is a TIN", and the Benchmark's peers are TINs too, so it is
consistent — but `GETTING_STARTED.md:1033-1057` never says so, and the user's
own two-TIN clinic will read "7 practices … p80 of 6".

### 2. Group 1/2 protocol changes

- **0.7.5 `.val` without `kind` + a 0.7.5 `.part`, resumed by 0.7.6**
  (`p_a_val_compat.py`, `p_a.out`): Last-Modified-only, quoted ETag, unquoted
  ETag, unquoted ETag + Last-Modified — each **one 206 (`bytes=300000-`) and
  sha == truth**. `_val_kind` classifies `"abc"`, `W/"abc"`, `5d8c72a5edda8d6a:0`,
  `1731000000`, `20261103`, `abc` as `etag`, an HTTP date as `last_modified`,
  empty as `''`.
- **`_content_claim`'s orphan rule vs a LIVE same-process ingest**
  (`p_e_live_twin.py`, `p_e.out`): a row claimed by `next_fetched_url` is in
  `_live_url_ingests`; an inbox twin **waited** (still `processing` after
  6 s) and, when the link's load ended `done`, ended `skipped` as a twin
  ("identical to … already loaded"). The same row seen by a new `Store`
  (a new process) loaded in 1.4 s. `claim_content_ingest` (the link-side
  content claim) is tracked too. Cross-process: `cmd_serve` claims the port
  before opening the store (`cli.py:190-215`); `cmd_ingest`, `status`,
  `export`, … check the port first; `cmd_add` posts to a live server and
  opens the store only when none answers; `fastcheck` never opens the store
  — so `recover_stuck_urls` cannot reach a live server's rows.
- **Re-expanded index, a child the payer removed, fresh signatures**
  (`p_f_removed_child.py`, `p_f.out`): the removed child's `done` row and its
  rates untouched; the republished child probed and re-downloaded with
  `?sig=S2`; the unchanged child probed only; stored links are the index's
  current ones. `dedup_key` collision needs two different files that differ
  only in signature-class params (`_VOLATILE_ALWAYS`, `fetch.py:53-70`),
  i.e. the same path — not new to this diff.
- **Clear queue during the re-check**: the shipped test (Clear during the
  probes) plus the post-requeue window above; the final `done` / `failed` /
  `skipped` writes are conditional on `expanding`, and `expanding` is
  written at `fetch.py:3098` before any of them, so no path can reach them
  while still `downloading` (no stuck index).
- **Crash revival of skipped links** (`store.py:3541-3560`): revives only
  `skipped / kind = 'duplicate'` rows whose `duplicate_of` names a `files`
  row that is `failed` and has no live part; a user's own skip is
  `kind = 'canceled'` and never matches; a forgotten twin has no `files`
  row and never matches.
- **Re-paste URL refresh** (`store.py:2788-2795`): rows in
  `downloading / fetched / ingesting / expanding` keep their URL; everything
  else gets the newest (the shipped test pins queued → yes, downloading → no,
  paused → yes).
- **Size fallback, same size different bytes**: "unchanged", and the residual
  IS written down (`docs/AI_HANDOFF.md:636` "no validator, same size …").
  The gap is the no-size answer above.
- **Migration budget / background** (`store.py:1503-1701`): per-file atomic
  swap under `_claim_ingest`; `rows_update` reaches `/api/files` and the Files
  tab (`app.js` "Updating stored rows … k of N files"); the shipped test pins
  budget → pending → background finish → CLI finishes first. A second
  `serve` dies at the port probe before touching the store. (Not independently
  killed mid-background here.)
- **`content_fingerprints.filename` is a PRIMARY KEY** (`store.py:2321-2329`),
  so the new LEFT JOIN in `done_rate_rows` cannot duplicate rows.

### 3. The auto memory rule and the config change

Numbers above (P3). `cfg.duckdb_memory_gb` is `int | None` (`config.py:90`);
`Store.__init__` and `_ram_worker_cap` branch on `None`; no reader casts it.
The shipped test asserts `auto_pool_gb` ≤ RAM − 3 − 2×2 for 8–64 GB.

### 4. Locks across I/O, lost messages, per-row cost, ORDER BY

- No new lock across I/O: `update_url` (RETURNING), `requeue_republished`,
  `clear_queued`, `claim_inbox_content`, `_enqueue_one`'s URL refresh hold
  `write_lock` for SQL only; `_content_claim`'s progress note is one UPDATE
  per second; `_refresh_loaded_children` probes outside any lock and reads
  the parent's status per child without a lock.
- Per-row cost: `file_identity`'s progress check is one `time.monotonic()`
  per 1 MB block; `_identity_of`'s note fires once a second. Nothing new in
  the parse path.
- Messages: the set-aside sentence now reads the kept bytes from disk after
  the paths that drop the `.part` (shipped test); the MPFS refusal names the
  line; the "first analytics build" line replaced the misleading
  "incremental unavailable".
- `ORDER BY` tiebreakers: `_house_disc_table_sql` (code, tin, discipline,
  payer), `done_rate_rows` (`q.id`), `pooled_month_files` (Python sort on
  `(-lines_differing, payer, file_month)` — unique per payer × month),
  `market.geographic_rates` (`median_rate DESC, state`), `visit_market`
  rows by payer name then the all-payers row.

## Files in this folder

`vm1_handworked.py` / `vm1.out`; `p_a_val_compat.py` / `p_a.out`;
`p_c_clear_window.py` / `p_c.out`; `p_e_live_twin.py` / `p_e.out`;
`p_f_removed_child.py` / `p_f.out`; `p_g_loaded_size_unknown.py` / `p_g.out`;
`pooled_cost.py`; `p_d.out`; `pytest_review12.out`; scratch stores under
`scratch_*` and `valcompat_*` (safe to delete).
