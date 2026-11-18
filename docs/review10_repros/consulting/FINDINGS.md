# Review 10 — Consulting and Growth tabs, client-facing documents

Reviewed: MRF Explorer 0.7.3, HEAD 2bb81b8, on Nov 10 2026. Reviewer only: nothing in the repository was edited or committed.
Everything is under `/tmp/mrfx_review10/consulting/`. Run a repro as
`cd /tmp/mrfx_review10/consulting/repros && PYTHONPATH=/home/user/Claudevideo /home/user/Claudevideo/.venv/bin/python <script>`.

Stores used:
- fixtures built by `repros/fx.py`, `fxm.py` (copies of the round-9 builders, output dir moved here) and `fxf.py` (fxm + Medicare anchor, PUF utilization, Medicaid / workers'-comp schedules, a hospital file, ACS);
- a copy of the 32M-row real store. **The copy was deleted when the review ended (disk).** `p12`, `p14`, `timing_real.py` and `ui10.py … real` need it re-copied to `/tmp/mrfx_review10/consulting/store` (yaml: `real.yaml`, port 8720).

Not run: Windows; the 99M store (another reviewer's); DuckDB thread count was not varied; closures / new clinics (no NPPES bulk file in either store); the proposal buttons in Chromium (the driver could not click them — the proposal was checked through the API and as a printed PDF only).

Severity: P0 wrong number / broken on a common path · P1 wrong or missing on a real path · P2 inconsistent or misleading · P3 cosmetic.

## Findings

### F1 (P0) — "Therapy market report — ND" lists Tampa, Birmingham and Tuscaloosa as North Dakota cities
- **Seen:** real store, Leads tab → market report, state ND, code 97110. Headline: "Within ND, Tampa pays 8.1% more than Tuscaloosa for 97110." 12 city rows; 10 of them have no provider located in ND. WY: 8 of 9 rows are Alabama cities. MA: 48 of 60 rows (Miami, Seattle, Spokane, …). The same function feeds `/api/territory/local` (Markets tab).
- **Expected:** only cities where the practice has a provider in that state. ND → Fargo (10 practices, $28.84) and Grand Forks (5, $28.84).
- **Cause:** `mrfx/territory.py:106-115` (and `:141-148` for the suppressed count) explodes `tin_directory.cities`, which is every city of a TIN that has any location in the state.
- **Repro:** `repros/p12_market_cities.py`.
- **Fix (prototyped in the script, not in the repo):** a TIN's cities in the state = cities of its NPIs whose NPPES state is that state (`rates` DISTINCT tin, npi ⨝ `npi_directory` WHERE state = ?). A/B on the real store, load ~7.6: ND 12 cities → 2, 0.10 s → 0.44 s; WY 9 → 1, 0.08 → 0.41 s; MA 60 → 60 (all in MA), 0.11 → 0.86 s. The placement parquet would make it cheaper.

### F2 (P1) — The rate card names the wrong "Best-paying payer" when a Medicare anchor is loaded
- **Seen:** A REHAB: Alpha pays 97110 $45, 97140 $30, 97530 $44; Beta pays 97110 $42 (the only code both price). Medicare 30 / 28 / 40. Rate card: "Best-paying payer: Beta Health"; scorecard rank 1 Beta 140 %, rank 2 Alpha 110 %; worst = Alpha.
- **Expected (like-for-like, the rule Leads and per-visit economics already apply):** on 97110 Alpha $45 > Beta $42, so Beta is the worse payer. On the same store Leads says "underpaid most by Beta Health ($4.00, 1 shared code)" and per-visit economics says best Alpha, worst Beta.
- **Cause:** `mrfx/schedule.py:214-294` — `rank_metric` is the median % of Medicare over each payer's OWN codes (Alpha: median(150, 107, 110)).
- **Also visible in the same table:** in the fxf packet's rate card, rank 3 Delta ($39.00, 80 % of best) sits above rank 4 Alpha ($45.00, 92 % of best). With no anchor, the "Median rate" column is a median across different codes: P Health $41.50 ranked above Q Health $45.00 (`out/pinned/ratecard.html`).
- **Repro:** `repros/p8_worst_payer.py`.
- **Fix:** rank on the codes every ranked payer prices (or head-to-head pairs), as `leads._worst_payer` and `economics.visit_economics` do; show own-code figures unranked; drop or relabel "Median rate".

### F3 (P1) — Monthly packets never contain the "Market position" document
- **Seen:** Clients tab → "Build monthly packets" (and `mrfx packets`, which has no state option): every client's packet omits Market position with "pitch report requires a state … pass market.allow_national=true". The UI and GETTING_STARTED promise that document. The same sentence, and "set enrichment.mode: bulk … in config/mrfx.yaml, or run `mrfx enrich --bulk <NPPES zip>`", are printed in the client folder's branded `index.html`. The packet's rate card pools peers nationally under a "NATIONAL PEER COMPARISON" warning.
- **Expected:** the packet scoped to the client's own main state, as the dossier already does.
- **Cause:** `mrfx/packets.py:140-151` requires a state; `packets.py:296-299` and `api.py:3110` / `app.js:2499` never supply one.
- **Repro:** `repros/p11_packets.py`.
- **Fix:** per client, default `market["state"]` to `anchor_state(store, {}, tins)` (dossier.py's rule) and say so in the document; keep setup instructions out of the client-facing index.

### F4 (P1) — The size panel prints a different overall position from every other screen
- **Seen:** fixture with one payer and three ranked codes (mid-ranks 0, 12.5, 100): Leads, leaderboard, roster, Negotiate, dossier all p13; the Benchmark tab's size panel: "This practice … is at p37.5". Real store, ND: UNIVERSITY OF ALABAMA HEALTH p33 on leaderboard / Leads / dossier, 52.2 in the size panel; CHEYENNE VAMC p63 vs 40.0. Multi-payer fixture (ACME): p5 everywhere, "p3.3" in the size panel, in the same tab as the P10 / P0 / P50 table.
- **Expected:** median(0, 12.5, 100) = 12.5 → p13. The 0.7.3 notes list the size premium among the screens that show the same number.
- **Cause:** `mrfx/quality.py:262` and `:336` take `avg()` of (payer × code) cells; `:373-374` round to 1 dp; `:397-398` print it as "is at p…". Round 9's own test fixture has two codes, where mean = median.
- **Also:** a mapped group's size is `max(npi_count)` of its member TINs ("solo (1 provider)" for BIG GROUP, whose dossier says 2 NPIs) — `quality.py:368`.
- **Repro:** `repros/p4_size.py`, `repros/p14_real_consistency.py`.
- **Fix:** either print the one overall position (median, whole points) and keep the within-payer score as a differently named band statistic, or label it "average within-payer position" and stop calling it the practice's position.

### F5 (P1, rare trigger) — Seven surfaces still resolve the subject their own way
Trigger: a bare NPPES name used by practices in two states (10 of 8,062 organization names on the real store — `repros/q_real_names.py`). Fixture: "ACME REHAB" = an MO practice (Alpha pays $40) and a KS practice in Wichita (Alpha pays $60); market state MO. Benchmark, Negotiate compare and the rate card correctly use the MO practice.

| Surface | Printed for "ACME REHAB" + MO | By hand (MO practice) | Cause |
|---|---|---|---|
| Remit check | Alpha paid $45 → "published $50.00, shortfall $5.00" | contract is $40; no shortfall | `remits.py:157` |
| Leverage (API, and the dossier's leverage section) | centred on Wichita 67202: "NO other therapy practice" | 64105: 10 alternatives | `leverage.py:78` |
| "Fill from Medicare" → Benchmark opportunity | 97110 12,000 units; opportunity at target **$98,500** | 3,000 units; **$26,500** | `utilization.py:363` (also `:445`, `:520`) |
| Per-visit economics | 13,500 units, Alpha annual $535,000 | 4,500 units, $175,000 | `economics.py:90` |
| Weighted position (Medicare volumes) | p10.7 | p12.2 | `economics.py:250` |
| Tracker bundle | Tax IDs both, "Location: Wichita, KS" | MO TIN only | `orgprofile.py:46` |
| Floors comparison | subject read through the peer state filter — read in the code, not run | subject_market rule | `floors.py:249-275` |

Also in the dashboard itself: the Negotiate tab's leverage and "What a visit is worth" panels do not send the tab's State (or its billing class / POS) — `mrfx/web/app.js:2883-2888`, `:2945-2949`. Same tab, same subject: compare table "Alpha $40.00"; visit panel "Alpha Health $50.00 per billed unit" and a "Gamma Health $66.00" row for a payer the MO practice does not have (`ui/shots_fix/negotiate_economics_upv.txt`). The Leads-tab dossier form sends no state or month (`app.js:2978-2979`): header "4 provider NPI(s), Town, Wichita (KS, MO)".
- **Repro:** `repros/p7_subject_gaps.py`, `repros/p3_subject.py`, `repros/ui10.py`.
- **Fix:** route these through `subject_tins_for_market` + `subject_market`; have the two Negotiate panels and the dossier form send the tab's market.
- **Related (P2):** when a state IS sent, leverage drops alternatives across the state line (S1 at 64105: 10 with state MO, 13 without — three Kansas City KS practices 2 miles away). `leverage.py:81-83` applies the peer state filter to a radius question; `dossier.py:136` comments that cross-border alternatives are real ones.

### F6 (P1) — Printed rate card loses a payer column; the position bar does not print
- **Seen (Chromium PDF, Letter):** rate card with 4 payers and a Medicare anchor: table 994 px in an 816 px page; the fourth payer's column is cut off (`pdf/ratecard_fx.pdf`). Pitch report with the anchor (12 columns): 913 px, right edge clipped (`pdf/pitch_fx.pdf`). The "Your position" bar is CSS background, so with the browser's default print settings the cell is empty apart from "P30". All eleven real-store documents fit (no anchor, fewer payers).
- **Cause:** `mrfx/schedule.py:489-492`, `mrfx/benchmark.py:2224` — print CSS is only `body { margin: 0 }`; no `@page`, no landscape, no `print-color-adjust: exact`, no column wrapping.
- **Repro:** `repros/p13_print.py`.
- **Fix:** `@page { size: landscape }` or split the fee schedule into blocks of 3 payers; `print-color-adjust: exact` on the strip and highlights.

### F7 (P2) — "New to network — just-signed practices" lists practices that were in the payer's book two months earlier
- **Seen:** Alpha publishes A–E in April, only C and D in May (one shard loaded), A–E in June. The panel lists A, B, E as newly contracted (2026-05 → 2026-06). Its note says a month you did not ingest "cannot fake a wave of new contracts"; a partly loaded month can.
- **Expected:** 0 rows — all three were in April's book.
- **Cause:** `mrfx/contracts.py:151-200` compares with the previous published month only.
- **Repro:** `repros/p10_new_to_network.py`. **Fix:** require absence from every earlier loaded month of that payer, and flag a previous book much smaller than the current one.

### F8 (P2) — "Market median" includes the practice itself in Leads and outreach, excludes it on Benchmark
- **Seen (6 practices, A lowest):** Benchmark 97110 median $48.00, gap $8.00; 97140 $33.00, gap $3.00 (average $5.50). Outreach `C97110_MKT_MEDIAN` 47.5, `GAP_TO_MEDIAN` 7.5; 97140 32.5 / 2.5. Leads "Avg $ below median" $5.00. The outreach percentile is among the OTHER practices, the median is not. Negligible on large markets, visible on thin ones — and the mail-merge letter is followed by the pitch report.
- **Cause:** `mrfx/leads.py:129`, `:141`; `mrfx/outreach.py:204`, `:264-266`.
- **Repro:** `repros/p9_gap.py`. **Fix:** one rule (median of the other practices) or name the difference in both notes.

### F9 (P2) — Two "worst payer" definitions under near-identical labels
- ACME REHAB: Leads "Underpaid most by Alpha Health" (dollars on the shared code); dossier "Which payer is weakest for them: Beta Health" (lowest position among that payer's practices). `leads.py:60` vs `dossier.py:244-258`. Both are defensible; the labels do not say they differ. Repro: `repros/p1_fxm.py`.

### F10 (P2) — The dossier never states its month or market
- A dossier pinned to 2023-06 prints no "as of" and no market definition; its footer is `DOSSIER_NOTE` only (`dossier.py:336-338`). The market report prints "As of" but not the market in words (`dossier.py:489-490`). Round 9's rule was `market_in_words` in every footer. Also "Across N comparable code(s) their median position is…" counts all-tied codes that the position excludes (`dossier.py:233`).
- Repro: `repros/p2_pinned.py` → `out/pinned/dossier.html`.

### F11 (P2, documented limit) — A practice the payer dropped stays a peer and tops Leads
- H REHAB is in Alpha's April file and absent from May and June. It is still a peer at $20 (it sets the subject's P10) and is the #1 lead ("p0, $28.00 below median, underpaid most by Alpha Health"). `benchmark._rates_relation` documents the limit ("a code line absent from ALL of a payer's newer files lingers"). Recorded because the Leads list ranks it first. Repro: `repros/p1_fxm.py`, `ui/shots_fix/leads.txt`.

### F12 (P3) — Smaller, each reproduced
- **Rounded twice** (the class round 9 fixed for position): weighted position averages per-code positions already rounded to whole points and counts all-tied codes — 37.7 printed, 37.5 by hand (`economics.py:264`, `:282`; `p4_size.py`). Hospital "median multiple" is the median of rounded multiples — 3.15× printed, 3.14× by hand (`hospital.py:680-689`). Floors "172.8 % of MO Medicaid" — 172.7 by hand (`floors.py:335-344`). Both in `p6_full.py`.
- **Referral "Avg wait (d)"** is an unweighted mean of per-pair means: 11.3 printed for pairs of 500 and 300 transactions at 12.5 and 10 days; 11.6 weighted (`medicare.py:614`).
- **Pinned documents:** every footer lists rate files from later months ("…p_2024-06-01.json") in a report as of 2023-06 — qualified in the pitch ("the benchmark draws on the subset…"), unqualified "Source files:" on the rate card and `fee.csv` (`schedule.py:332`; pitch `benchmark.py:2015`). "Publication age" cites "last published 2024-06-01" (a deliberate call in round 9; the date is not the publication date of the rates in the document). `/api/trajectory` pinned to 2023-06 returns `months` including 2024-06 (series are bounded correctly).
- **500 instead of a sentence (API only):** `/api/contract-gaps` `min_peers: 1e308`; `/api/contracts/new-to-network` `radius_miles: "NaN"`; `/api/trajectory` and `/api/engagements/compare` with a non-object `market` (`engagements.py:181`). `repros/p5_fuzz.py` (4,501 requests; these four classes only).
- **Wording:** floors headline "…this practice at 148.2%. and 90.9% of workers' comp." (`floors.py:352`); "+0.0% in dollars"; market report "0 of 2 payer(s) demonstrably price practices differently — those are the negotiations with room"; `fee.csv` prints 40.0, not 40.00; roster counts 3 codes where Leads counts 2 for the same practice; `outreach.py:12` docstring still says "share of entities at or below".

### Release notes (GETTING_STARTED "What changed in version 0.7.3") — noticed by a user, not mentioned
- The Rate changes table itself (not only the trajectory) now follows the new State box.
- Positions already quoted to clients can move (Negotiate / dossier by a point; roster from p50) and Leads can name a different worst payer — the notes say the screens now agree, not that earlier numbers may differ.
- The leaderboard's "codes" count changed (codes in the position, not codes compared).
- Sparkline percentages round differently (+1.2 % → +1.3 %).
- The note "same number on every screen (… size premium)" is not true (F4).

## Checked and found correct
- **Hand-worked numbers:** Benchmark rate, P25 / median / P75, position, peers, opportunity at p40 and p50 (pinned 2023-06 fixture: $48.00, p20, $9,600 / $12,000); one-pager per-payer sections and "largest single-payer opportunity"; payer-compare uplift 11.1 % / 13.2 % and "other payers 5.0 % more"; proposal +$10.00 (+25.0 %), $33,750; % of Medicare 137 / 163; floors per-row percentages; hospital per-row multiples; real-terms −4.0 % (CPI-U 2022 → 2023); leaderboard p45 / p78; Leads worst-payer gap $8.00 and $4.00; market sizing 8,200 seniors, 1,640 per alternative; referral leaders 213 patients / 3 sources.
- **One overall position:** Leads, leaderboard, roster, Negotiate headline, one-pager, dossier agree on every fixture and on 25 real ND practices (leaderboard = dossier = median of unrounded Benchmark mid-ranks; roster = compare per payer).
- **Subject rule where round 9 applied it:** full name, partial name, lower case, "NAME · ST", TIN, dashed TIN, NPI give identical Benchmark, compare, rate card, fee.csv, gaps, size, subject-types, dossier, pitch, proposal (`p3_subject.py`); out-of-state K1 REHAB is priced on all of them; mapped entity titled "BIG GROUP".
- **Pinned month:** no later-month rate, practice or trajectory point in any document or JSON pinned to 2023-06 (apart from F12); sparklines and erosion stop at the month.
- **Determinism:** 88 endpoint calls byte-identical between first and second call and across a server restart (timestamps normalised); org-bundle and outreach ZIP members identical except the "Generated" line. Ranked lists read for tiebreakers: Leads, leaderboard, roster, negotiation sections.
- **Printing rules:** no Python `round()`, no `:+.1f`, no "-0.0%" on a printed number in the area; no "None" / "nan" / raw JSON in any rendered document.
- **Chromium:** 0 page errors on both stores; console errors only the two expected 422s (no utilization / no roster on the real store); no "undefined" / "NaN" / "null" on any Consulting or Growth view; six reports open; CSV and ZIP downloads fire.
- **Refusals in words:** typo subject → dossier refused; no hospital / floors / utilization / roster loaded → a sentence naming the step.

## Endpoint timings — 32M real store, subject REHABAUTHORITY, LLC (ND), seconds, first / second call
Run 1: load average 1.0–2.1 (first calls after server start). Run 2: after a restart, load 7.5–9.6 (other reviewers). Nothing in the area exceeds ~2 s on a quiet machine except the unfiltered outreach ZIP (3.2 s quiet, 4.3–5.2 s loaded; 1.2 MB, one row per TIN for 20,101 TINs), so no EXPLAIN ANALYZE was needed. `[tin]` variants were within 0.03 s of `[name]`.

| Endpoint | Run 1 (load ~1–2) | Run 2 (load ~8–9) |
|---|---|---|
| benchmark ND [name] | 0.04 / 0.02 | 0.08 / 0.04 |
| benchmark national [name] | 0.11 / 0.11 | 0.32 / 0.31 |
| opportunity [name] | 0.02 / 0.02 | 0.05 / 0.04 |
| pitch report [name] | 0.09 / 0.04 | 0.21 / 0.12 |
| negotiation one-pager [name] | 0.03 / 0.03 | 0.09 / 0.05 |
| negotiate compare [name] | 0.04 / 0.04 | 0.08 / 0.07 |
| payer-compare report [name] | 0.04 / 0.04 | 0.11 / 0.12 |
| proposal [name] | 0.04 / 0.04 | 0.11 / 0.10 |
| proposal report [name] | 0.06 / 0.07 | 0.11 / 0.08 |
| fee schedule [name] | 0.04 / 0.03 | 0.05 / 0.06 |
| fee.csv [name] | 0.03 / 0.03 | 0.05 / 0.05 |
| rate card [name] | 0.05 / 0.05 | 0.10 / 0.13 |
| contract gaps [name] | 0.02 / 0.03 | 0.07 / 0.08 |
| visit economics [name] | 0.01 / 0.00 | 0.02 / 0.01 |
| weighted position [name] | 0.02 / 0.02 | 0.05 / 0.04 |
| size premium [name] | 0.05 / 0.00 | 0.08 / 0.00 |
| subject-types [name] | 0.01 / 0.01 | 0.04 / 0.03 |
| leverage [name] | 0.59 / 0.08 | 0.10 / 0.08 |
| leverage summary [name] | 0.09 / 0.08 | 0.12 / 0.13 |
| dossier [name] | 0.59 / 0.64 | 1.55 / 1.45 |
| dossier.json [name] | 0.62 / 0.62 | 1.60 / 1.63 |
| org bundle zip [name] | 0.21 / 0.11 | 0.34 / 0.28 |
| medicare org [name] | 0.06 / 0.06 | 0.15 / 0.21 |
| hospital parity [name] | 0.01 / 0.01 | 0.02 / 0.01 |
| floors compare [name] | 0.00 / 0.00 | 0.00 / 0.00 |
| changes subject [name] | 0.05 / 0.00 | 0.06 / 0.00 |
| visit price [name] | 0.00 / 0.00 | 0.00 / 0.00 |
| leads ND | 0.07 / 0.00 | 0.17 / 0.00 |
| leads national | 0.48 / 0.00 | 1.25 / 0.01 |
| leads.csv ND | 0.09 / 0.01 | 0.22 / 0.01 |
| leaderboard ND | 0.03 / 0.00 | 0.06 / 0.00 |
| leaderboard national | 0.40 / 0.00 | 0.98 / 0.01 |
| roster ND | 0.04 / 0.01 | 0.10 / 0.02 |
| roster national | 0.04 / 0.01 | 0.07 / 0.02 |
| roster.csv ND | 0.05 / 0.01 | 0.09 / 0.01 |
| posture ND | 0.04 / 0.00 | 0.12 / 0.00 |
| posture national | 0.30 / 0.00 | 1.10 / 0.01 |
| types ND | 0.05 / 0.00 | 0.14 / 0.01 |
| size ND (no subject) | 0.03 / 0.00 | 0.07 / 0.01 |
| size national | 0.52 / 0.00 | 1.19 / 0.00 |
| freshness | 0.01 / 0.00 | 0.02 / 0.00 |
| trajectory ND | 0.06 / 0.00 | 0.13 / 0.00 |
| trajectory national | 0.56 / 0.00 | 1.75 / 0.00 |
| inflation status | 0.00 / 0.00 | 0.00 / 0.00 |
| market report ND | 0.10 / 0.03 | 0.27 / 0.06 |
| renewals | 0.05 / 0.02 | 0.17 / 0.06 |
| renewals subject | 0.02 / 0.02 | 0.05 / 0.04 |
| expiry coverage | 0.05 / 0.00 | 0.13 / 0.00 |
| new-to-network ND | 0.02 / 0.01 | 0.04 / 0.03 |
| market sizing 58103 | 0.39 / 0.39 | 2.05 / 0.99 |
| medicare status | 0.00 / 0.00 | 0.00 / 0.00 |
| medicare leaders | 0.00 / 0.00 | 0.00 / 0.00 |
| packets | 0.00 / 0.00 | 0.00 / 0.00 |
| clients | 0.00 / 0.00 | 0.00 / 0.00 |
| clients digest | 0.02 / 0.00 | 0.31 / 0.01 |
| hospital status | 0.00 / 0.00 | 0.01 / 0.00 |
| floors status | 0.00 / 0.00 | 0.00 / 0.01 |
| outreach.csv entity ND | 0.94 / 0.77 | 2.42 / 2.31 |
| outreach.zip entity ND | 0.77 / 0.83 | 1.84 / 1.52 |
| outreach.csv tin ND | 0.68 / 0.77 | 1.19 / 1.25 |
| outreach.zip tin ALL (button) | 3.23 / 2.98 | 4.29 / 5.24 |