# Endpoint snapshot: 64a371c (0.7.2) vs 2bb81b8 (0.7.3), 32M-row real store

docs/review9_repros/outputs/snapshot.py on two copies of the same store
(scratchpad/perf/named_vis), each endpoint called twice; diff_snap.py.
Machine shared with three reviewers (timings ±~15 %).

125 endpoints: 109 byte-identical, 16 different — every difference traced to
an intended round-9 change:

| Endpoint(s) | Change | Expected? |
|---|---|---|
| benchmark_* (5), negotiate_compare | rows gain `in_position`, `subject_percentile_exact` | yes (4.3) — API-only fields |
| leads_AL / leads_all | lead rows gain `worst_payer_basis`, `worst_payer_codes` | yes (4.3) |
| leads_csv | new column `worst_payer_codes` inserted BEFORE `website` | yes, but a mail-merge mapped by column position shifts |
| roster_csv (WY) | 171 of 501 rows: median_percentile 72.0 → 73.0 (170), 44.0 → 38.0 (1); order unchanged | yes (4.3: all-tied codes no longer counted) |
| report_pitch / negotiation / ratecard / payer_compare, schedule_fee_csv, leads_csv | footer "Market definition:" JSON → words | yes (4.9) |
| dossier_json | `median_percentile` 51.0 → 51 (float → int) | type change only; other surfaces still return 51.0 |

Headline positions on this store did not move (the subject's codes all have
spread), and no report body changed beyond the footer.

Timings (cold → cold, warm → warm): nothing regressed beyond noise except
`report_market` cold 0.40 → 0.84 s and `overview` cold 0.75 → 1.18 s (both
warm ~0). Over 2 s on this store in BOTH versions:
`rates_entity_latest` (the Explorer at entity grain, default page) cold
4.5 s, WARM 3.2–3.7 s — no cache; `export_csv_all_latest` 2.8–3.9 s
(343 MB). Total of all cold calls 33.9 s → 37.5 s.
