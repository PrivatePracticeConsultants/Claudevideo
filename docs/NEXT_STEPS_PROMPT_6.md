# Prompt for the next session, round 6 — every output, end to end (written for Claude Opus 5.5)

Rounds 1–5 are `docs/NEXT_STEPS_PROMPT.md`, `_2.md` … `_5.md`; each has a
status block of what it closed. This memo came out of a full audit on
Nov 6–7, 2026 of EVERY output the program produces — 131 API endpoints, every
dashboard tab, every report, export, packet and bundle — asked one question:
is each one correct, deterministic and as fast as the data allows? Method:
every endpoint timed twice on the named real-data store (1.4M spine rows) and
the 99M-rate synthetic store (79M spine rows); the named store's 125
snapshotted endpoints run twice in full and diffed byte for byte; four
independent line-by-line reads of every output module and of `app.js`, each
claim then checked by this reviewer against the code or reproduced in a
throw-away test. Every item below carries its reproduction or its file:line;
nothing is listed on suspicion, and what was checked and found fine is listed
at the end so it is not re-chased. Copy everything below the line into a
fresh session on this repo.

---

You are picking up MRF Explorer (`mrfx/`). Read `CLAUDE.md`, then
`docs/AI_HANDOFF.md` in full (Steps 3, 5 and 8), then `git log eda6d09..HEAD`
(round 5: launchers, signalled queue, the DuckDB visibility fix, the Explorer
filter rewrite, the Leads fixes, the standard-visit calculator). The product:
one non-technical user on a Windows PC pastes payer links, then searches by
NPI to see what each payer reimburses for PT/OT/SLP codes, and SELLS the
outputs — client packets, rate cards, dossiers, market reports, proposals.
Correctness of the numbers beats speed; "should work" is not done; every
claim needs a live number.

## Non-negotiables

1. Numbers do not move — EXCEPT where an item below says the current number
   is wrong. Then the new number is pinned by a hand-worked known-answer test
   (the style of `tests/mrfx/test_known_answer_leads.py`), and the
   before/after endpoint snapshot (`scratchpad/perf/snapshot.py` +
   `diff_snap.py`) differs ONLY on the endpoints that item names.
2. No ROLLUP_SCHEMA_VERSION / DIRECTORY_SCHEMA_VERSION bump unless an item
   cannot be done without one; say why if so (a rebuild is an hour at 300M).
3. Measure on the named store (`scratchpad/perf/named_orig`) AND the 99M
   store (`scratchpad/scale4n`), cold and warm; A/B old vs new code in the
   same run, alternating (machine noise this round was ±20%).
4. One regression test per fix; `ruff` stays at zero; full suite before every
   commit that touches numbers; commit what + why + before/after numbers.
   Push only to the branch you are told; never weaken TLS.

## Priority 0 — client deliverables that are empty, wrong or crash

**P0.1 "The month" is the store's newest month, not each payer's — three
sellable outputs are blind on real data.** On the named store one small payer
(HEALTHSCOPE BENEFITS, 76 rows, 3 TINs) published a file dated 2026-12; every
other payer's newest month is 2026-08/10/11. Code that takes
`SELECT max(file_month) FROM rates_by_tin` therefore asks the whole book about
2026-12:
- **Monthly client packets are empty.** `packets.py:73-77` pins
  `market["month"] = max(file_month)` and `_market_where` then filters
  `t.file_month = '2026-12'` (not `is_latest`). Reproduced: a 5-client build
  on the named store (practices with rates from 3–4 payers each) wrote five
  packets that each say "Rate card: no published rates for this practice …",
  "Market position: no benchmarkable codes", "Contract gaps: none". Fix:
  compute on "latest" (each contract's newest month) and pin only the LABEL —
  `engagements._concrete_month` already does exactly that.
- **New to network finds nothing while claiming 8 comparisons.**
  `contracts.py:163-166` uses the store max; the per-payer `now` set
  (`file_month = '2026-12'`) is empty for every payer but Healthscope. Named
  store: 0 rows, `compared` lists 8 payers (e.g. BCBS Wyoming 2026-10 — whose
  real 2026-08 → 2026-10 book was never compared). Fix: per payer, its own
  newest month vs its own previous published month; `compared` must list the
  months actually compared.
- **Client digest "rate changes" see one payer.** `clients.py:70-75` diffs the
  store's two newest months (2026-12 vs 2026-11) for every client; a client's
  payers that did not publish in exactly those months show "no change", and
  `packets.py:319-322` prints "No published rate changed for this practice".
  Fix: per payer × contract, newest vs its own previous publication
  (`monitor.compute_rate_changes` takes explicit months today — give it a
  per-payer mode, and say which months each payer's line compares).
- Also store-wide: `packets.py:255` (the packet folder month) and
  `engagements.py:56` (labels only — fine as a label, keep it a label).
Test: a known-answer store where a tiny payer publishes a month ahead of
everyone (exactly the named store's shape) — packets, new-to-network and the
digest must still show every other payer's real answer.

**P0.2 New to network ignores the house basis.** `contracts.py:211-231` reads
`rates_by_tin` with only `payer` and `file_month`: no `is_dollar_rate`, no
`> 0.01`, no base-modifier rule, no professional class, no
`NOT tin_is_really_npi`, and the market's state/payers are not applied (the
module docstring says "Both reuse the house market basis"). Reproduced: a new
practice whose only line is an INSTITUTIONAL $900 97110 row is listed as
"newly contracted, median rate $900.00". Fix: build both month sets and the
median through `_market_where`; known-answer test with an institutional-only,
a placeholder-only and a CQ-only newcomer.

**P0.3 Requests that 500 or silently answer a different question.**
- `/api/remits/check` with a state, city or therapy filter: `remits.py:163-169`
  has no `LEFT JOIN tin_directory td`, `_market_where` emits `td.*` →
  DuckDB `Binder Error: Referenced table "td" not found`; the endpoint catches
  only `BenchmarkError` (`api.py:2594`) → HTTP 500. Reproduced.
- A full state NAME is not normalized in two places: `/api/changes`
  (`monitor.py:48-57` never calls `normalize_market`) — reproduced on the
  Leads fixture: `state: "MO"` → 1 change, `state: "Missouri"` → 0 changes,
  no error; and `hospital_parity` (`hospital.py:491-492` sets the raw state
  after normalizing) → an empty table whose reason blames the payer name.
- Proposal endpoints (`api.py:2994-3012`): `comparables` is not list-checked
  (a number → `TypeError` at `benchmark.py:864` → 500, reproduced; a string is
  iterated character by character); only `BenchmarkError` is caught, so a bad
  `target_percentile` is a 500 too. And `compute_rate_proposal`
  (`benchmark.py:1235-1236`) resolves the Medicare anchor from the RAW market
  (`"Missouri"` → `"MISSOURI"`), so "% of Medicare" can be refused in the
  proposal while the compare view, which normalizes, shows it.
- A plan-scoped market fails in Negotiate compare, the payer-compare report
  and both proposal endpoints: `benchmark.py:879` builds the "other payers"
  market with `public_market(...)`, which drops `_plan_files`, and
  `_market_where` refuses (`benchmark.py:272`) with a message telling the
  user to use … Negotiate. Reproduced with `test_plans._seed_two_networks`.
Fix each with a test that posts the exact body above.

**P0.4 Outputs that change between two identical requests (invariant 7).**
Found live: `/api/validate` on the 99M store returned different bytes on two
back-to-back calls — `api.py:1572` orders by `payer, file_month,
negotiated_rate`, which ties across TIN/NPI/modifier/class/source rows.
By reading (each a non-unique key feeding something the user sees):
- outreach export: `outreach.py:121-128` has no ORDER BY, so the dict order
  breaks the `-len` tie in `outreach.py:145` — which codes become the 10
  merge columns can change at the cut; `any_value(display_name)` per unit.
- floors: `floors.py:280-296` groups by `(kind, code, rate)` with no year; two
  loaded years of one state's Medicaid schedule both come back, the last row
  wins per code and the label is the last row's (reproduced: 2025 $20 + 2026
  $26 loaded → one is shown, chosen by row order, under one year's label).
  Pick the newest year per (kind, state) and say which.
- Medicare referral leaders: `medicare.py:812`
  `max(set(tins), key=tins.count)` — a tie is broken by Python's per-process
  string-hash order, so the drawer TIN can change after a restart.
- `quality.py:384` `ORDER BY 1, 2` under `[:200]`; `utilization.py:580`
  `ORDER BY n_peers DESC, peer_units DESC` (no code; feeds the headline);
  `hospital.py:539-541` `ORDER BY ratio, billing_code` (no payer) under LIMIT;
  the entity drawer's `tins` list has no ORDER BY (`api.py:1313-1316`) and
  `tins[0]` picks the website-lookup city; `_ORDER_TIEBREAK`
  (`api.py:764-766`) omits `tin_is_really_npi`, which is a grouping key.
- `any_value(...)` identity for multi-TIN subjects — the name can come from
  one TIN and the NPI count from another: `dossier.py:142-145`,
  `clients.py:87-90`, `packets.py:85-88`, `orgprofile.py:62`.
Extend `tests/mrfx/test_determinism.py` (tied fixtures) to every endpoint in
this list, and add validate/outreach/floors/medicare-leaders/hospital to
`scratchpad/perf/snapshot.py` so the double-run covers them. (The named
store's existing 125 snapshotted endpoints were byte-identical across two full
runs this round.)

## Priority 1 — measured costs on screens the user opens again and again

All numbers: 99M store unless marked "named"; first call / second call.

**P1.1 Client packets are O(N²) in clients.** `packets.py:131` calls
`client_digest(store)` — the WHOLE book's digest — once per client, and each
packet also re-runs the full-market benchmark, contract gaps, fee schedule
and baseline compare. 5 clients: 11.1 s named, **175 s at 99M**. Build the
digest (and each month's market base) once per build and hand it to every
packet. Report the 5- and 30-client times.

**P1.2 The Clients tab recomputes the whole digest on every visit.**
`app.js:2225` runs `loadClientDigest()` on each tab entry; the digest runs,
per client, a NATIONAL `contract_gaps` (`clients.py:120`: no state — the whole
latest market every time), rate changes and referrals. 5 clients: 1.7 s
named, **23.4 s at 99M per visit**. Cache per (data_generation, watchlist),
scope each client's gaps to its own state, compute the market base once.

**P1.3 The visit card pays for peer medians it never shows.**
`visit.price_visit` calls `compute_fee_schedule`, whose peer-median query is
**4.13 s of 4.34 s** (95%) at 99M; the subject-only lookup is 0.22 s. The card
runs right after "Build rate card" computed the same schedule, so one click
pays twice (2.6 + 2.7 s). Give `compute_fee_schedule` a subject-only mode
(or reuse the card's result); the visit must stay byte-identical.

**P1.4 Outreach export: a quadratic percentile in Python.** `outreach.py:198`
`sum(1 for v in vals if v <= rate)` per unit × code: one state (AL, named)
takes **11.6 s**, 244M generator steps under the profiler. Sort once per code
and `bisect` (identical numbers; note the column is inclusive "at or below",
see P3).

**P1.5 Whole-store quality scans, uncached, on every Overview visit and
inside every benchmark.** Each Overview entry (`app.js:297-301`) runs
`/api/quality/freshness` + `types` + `posture`: 0.45 + 3.9 + 0.7 s at 99M,
every visit. `quality_size` is 14.0 s. `payer_freshness` (a whole-spine
`GROUP BY payer`, `quality.py:161-163`) runs inside EVERY `compute_benchmark`
via `_stale_payers_for` (`benchmark.py:629`) — once per payer in a
negotiation, dossier or packet. All change only per data_generation: cache
them per (generation, market).

**P1.6 ZIP placement of whole books from raw rates, per request.** The
leverage chain (`leverage.py:113-136`), the radius map (`territory.py:481-497`)
and new-to-network (`contracts.py:243-252`) place EVERY practice a payer (or a
code) prices by its NPIs' ZIPs, reading raw `rates` each call; leverage-by-
payer repeats it per payer. Named: leverage 4.3–11.2 s, leverage summary 6 s,
dossier 5.9 s. 99M: radius map **61.7 s / 11.4 s**, dossier 12 s,
new-to-network 8.2 s (plus a per-payer query loop, `contracts.py:210`).
Round 5 deferred a TIN→ZIP side table because placement reads `npi_directory`
live and enrichment changes it between rebuilds. Decide now: (a) a placement
table refreshed with the directory's names-only rebuild, with the lag stated,
or (b) a cache keyed on (data_generation, enrichment's named-NPI count). Keep
the sqlfn tie rule (most rows, then lowest ZIP); prove identical placements.

**P1.7 Leads, leaderboard and roster recompute whole-market windows per
request.** `leads.py:55-79` plus a SECOND full market aggregation for the
worst payer (`leads.py:121-129`): all-market leads 8.6 s, leaderboard 5.2 s,
state leads 1.8 s at 99M. Compute the market base once per call (one CTE) and
cache per (generation, market).

**P1.8 A round-5 regression to undo.** The P1.4 rewrite (practices first)
made the Code page with a state filter slower: A/B on the same machine,
alternating, `/api/code/97110?grain=tin&state=MO` **0.54 → 0.72 s**; the
export and the table were within noise. When the key filter already narrows
to one code, the joined path was cheaper — gate `_tin_rows_by_unit` on that,
A/B again, keep `test_unit_filters.py` green.

## Priority 2 — the dashboard's own behaviour (verify each in Playwright)

- **The summary strip's "retrying…" never retries, and every error reads
  "busy".** `app.js:830-834` compares `filterQuery() === q` — two different
  `URLSearchParams` objects, never equal — so the retry is dead and the strip
  stays on "Stats are busy — retrying…". `app.js:819` labels every
  non-memory error "busy", including the 422 "your store is large, pick a
  payer and a code" guidance the user needs to read.
- **Slow answers paint over newer ones.** Only the Code page and the subject
  typeahead carry a request token/abort. `loadRates`/`loadSummary`, the
  drawer, Overview, Markets, Benchmark, Negotiate, Rate card, the visit card,
  Leads, Changes and the Leads-tab panels do not: change a filter twice
  quickly and the first response can land last.
- **During enrichment the open view reloads itself every 15 s**
  (`app.js:202-207`, whenever the named-NPI count grew): the Explorer re-runs
  table + strip; the Code page aborts its own in-flight request each time.
- Duplicate fetches: Negotiate re-entry calls `refreshSubjectPickers` twice
  (`app.js:1787-1788`, each = subjects + months); boot fetches `/api/stats`
  twice; `/api/months` and `/api/payers` are whole-spine DISTINCTs on every
  tab entry, uncached (cache per generation).

## Priority 3 — basis and honesty consistency (fix, or state the rule)

- `economics.weighted_position`: the "unweighted" figure is a MEAN
  (`economics.py:231`); the house headline is the median (negotiation, Leads,
  dossier since round 5).
- Steal-share: "rivals" are every NPI in the client's cities with no
  therapy filter (`territory.py:344-349`) while the headline says "therapy
  practices"; "and none to this one" can be a CMS-suppressed 1–10.
- The prospect dossier and market report have no geography gate (the pitch,
  negotiation, compare and proposal reports call
  `require_geographic_scope`; `dossier.py` never does) and show positions
  with no peer counts; only the rate card hides peer stats under 3 peers
  (`schedule.py:52,183`) — the Benchmark/pitch print p10–p90 over 1 peer.
- `basis_note` is hard-coded in the fee schedule (`schedule.py:205`) and
  contract gaps (`benchmark.py:1071`) — it claims "base modifiers,
  professional" even when the toggles turned those off
  (`compute_benchmark` appends them correctly at `benchmark.py:611-624`).
- Engagement compare accepts negative / non-finite units
  (`engagements.py:172-177` skips `clean_volumes`) → negative "annual value".
- Leverage summary prints the requested radius (`leverage.py:268-269`) though
  `network_leverage` clamped it to 250 miles; Leads silently clamps `limit`
  to 1000 (`leads.py:38`) while the endpoint accepts 5000; the p25 sweep
  tests the ROUNDED median position (`leads.py:84,94`).
- Masking: the Python rule (`store.py:347-350`, prefix + SSN pattern) and the
  SQL rule (`api.py:99-107`, prefix only) disagree for 96x/97x/000-style
  TINs — raw on screen, masked in exports. Not an SSN leak; screen ≠ export.
- The Code-page export is the spine rows (one per POS set, rate-sorted)
  while the screen is grouped medians, although `api.py:1410` says
  "export == screen" — fix the comment or the export.
- Minor: outreach `C*_PCTL` is inclusive "at or below" (not mid-rank);
  payer-concentration HHI is computed from rounded shares and its
  median_rate is over raw spine rows; the market report's
  `median_pct_medicare` under a national market divides by one state's anchor.

## Checked in this audit and found fine — do not re-chase

- 125 dashboard endpoints byte-identical across two full runs (named store).
- Leads list / leaderboard / roster / radar / contracts ORDER BYs are unique;
  the worst-payer pick is `row_number() … ORDER BY avg_gap DESC, payer`.
- Benchmark position = mid-rank, subject excluded, peers rounded to 2 dp;
  `compute_fee_schedule` narrows by TIN and by payer + code and binds in SQL
  text order; percentiles never fabricated for unpriced codes.
- Overview cached per (state, generation); stats/states TTL + single-flight;
  summary strip per generation (round 5).
- Exports stream via `COPY` (no whole result in memory) and carry their
  methodology in the ZIP; formula defusing covers every third-party text
  column; masking applied in every export path.
- Closures/new-clinic notes, inflation refusal of missing years, tracker
  discovery (read-only), the sqlfn ZIP tie rule, the round-5 histogram,
  the visit calculator's units (CMS sec. 20.2 examples 1–5).
- No N+1 query loops except those named above (`compute_payer_negotiation`
  per payer is inherent; its per-call extras are P1.5).

## Report

End with: (1) for each P0 item, the reproduction before and after; (2) the
snapshot diff (expected: only the endpoints each fix names); (3) every P1
number before and after, both stores, A/B alternating; (4) the P1.6 decision
with its staleness statement; (5) the Playwright checks for P2; (6) what you
left and why; (7) what you could not measure — Windows is still unmeasured,
and the NPPES-fed feeds (new clinics, closures) and Medicare referral leaders
were not timed against a real full-size NPPES cache or referral release.
