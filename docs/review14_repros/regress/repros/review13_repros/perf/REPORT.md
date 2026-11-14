# MRF Explorer 0.7.6 — output-feature audit 13: speed and correctness

Repo `/home/user/Claudevideo`, branch `claude/gracious-mccarthy-ao7vmo`, HEAD `c8b80fc`, read-only. Date 2026-11-12.
Box: 4 cores, 15 GB RAM, ~7 GB free disk. All scratch under
`/tmp/claude-0/-home-user-Claudevideo/4633d594-0139-5f8e-a935-ae9c76b72000/scratchpad/audit13/perf/` (= `perf/` below).

## 0. Method and evidence files

* **99M store** (`scratchpad/scale4n/store`: 99,000,000 rates, 79,027,749 spine rows, 15 payers x 3 months, 194,000 TINs in 12 states, enrichment off) served by one real `mrfx serve --config scratchpad/ui7/scale.yaml` (port 8392) started by `perf/driver.py` (Popen; page cache dropped with `echo 3 > drop_caches` first, so COLD = cold disk + fresh process). **Start-up 9.55 s to `/api/boot`; no stored-row update/migration ran** (every `files.row_rules` was already 4; `serve.log` has no migration line). Every output route the dashboard can trigger (`perf/calls.py`, 135 calls, payloads = the defaults `index.html`/`app.js` send) was called three times through HTTP: cold, warm (identical), and a third call with a different filter. Results: `perf/timings.csv` (columns: name, view, method, url, payload, status, cold_ms, warm_ms, warm_status, bytes, same_bytes, variant_url, variant_payload, variant_ms, variant_status, bytes, load, err), bodies in `perf/bodies/`, server log `perf/serve.log`, run metadata `perf/run_meta.json` (server peak RSS **7.7 GB**, 2.0 GB at the end).
* **Chromium walk** (`perf/ui_walk.py`, Playwright, `/opt/pw-browsers/chromium`, `--no-sandbox`) of all 14 views against the same server, after the timing run: `perf/ui/ui_walk.json`, screenshots `perf/ui/benchmark.png`, `perf/ui/ratecard.png`.
* **In-process profiles** (`perf/prof.py`, TestClient over the same store AFTER the server stopped, page cache dropped again; statement-level DuckDB timing + cProfile): `perf/prof.json`, `perf/prof.out`.
* **Small real store** (`perf/real.yaml`, built with `mrfx ingest` + bulk NPPES enrichment from the three small real files: BCBS WY shard 05_of_09 699,174 rows, Moda TX 430,768 rows, UHC HealthScope 252 rows; 3,511 TINs, 12,929 NPIs named; ingest+enrich 1 min 13 s). Every number-bearing output fetched twice (`perf/real_fetch.py` -> `perf/realout_tx/`, `perf/realout_wy/`) and recomputed independently from raw `rates` (`perf/house.py` + `perf/check_real.py`, `perf/check_entity.py`). The store was deleted at the end (disk rule).
* **99M hand checks** from raw `rates` (`perf/spot99.py`, `perf/spot99_entity.py`), prototypes for the speed items (`perf/proto.py`).

## 1. Timing table — 99M store, through HTTP, as the browser calls it

Legend: cold = first call after the fresh start, warm = second identical call, 3rd = a different filter/subject (see `variant_*` in timings.csv). A 422/507 is a refusal sentence (expected for the synthetic store's missing layers / ZIPs / size); no 5xx anywhere.

| # | feature (route) | view | payload | cold ms | warm ms | 3rd call (variant) ms | bytes | status |
|---|---|---|---|---:|---:|---:|---:|---|
| 1 | `GET /api/boot` | boot | — | 3 | 2 | — | 23 | 200 |
| 2 | `GET /api/stats` | boot | — | 6823 | 8 | — | 252 | 200 |
| 3 | `GET /api/catalog` | boot | — | 2 | 2 | — | 7612 | 200 |
| 4 | `GET /api/states` | boot | — | 28 | 1 | — | 72 | 200 |
| 5 | `GET /api/payers` | boot | — | 55 | 2 | — | 177 | 200 |
| 6 | `GET /api/months` | boot | — | 234 | 1 | — | 42 | 200 |
| 7 | `GET /api/plans` | boot | — | 5 | 4 | — | 449 | 200 |
| 8 | `GET /api/peersets` | boot | — | 2 | 2 | — | 16 | 200 |
| 9 | `GET /api/entities/map` | boot | — | 2 | 2 | — | 15 | 200 |
| 10 | `GET /api/benchmark/subjects` | boot | — | 30 | 29 | 46 (200) | 7426 | 200 |
| 11 | `GET /api/mpfs/status` | boot | — | 3 | 2 | — | 15 | 200 |
| 12 | `GET /api/overview` | overview | — | 26900 | 2 | 1512 (200) | 3284 | 200 |
| 13 | `GET /api/quality/freshness` | overview | — | 482 | 2 | — | 3581 | 200 |
| 14 | `POST /api/quality/types` | overview | {"market": {"month": "latest"}} | 4444 | 2 | 1410 (200) | 1653 | 200 |
| 15 | `POST /api/quality/posture` | overview | {"market": {"month": "latest"}} | 7248 | 3 | 1211 (200) | 5702 | 200 |
| 16 | `GET /api/rates` | explorer | grain=tin&latest=1&dollar_only=1&page=1&page_size=100 | 1984 | 1138 | 1979 (200) | 71320 | 200 |
| 17 | `GET /api/summary` | explorer | grain=tin&latest=1&dollar_only=1 | 10 | 3 | 2077 (200) | 3509 | 200 |
| 18 | `GET /api/rates` | explorer | grain=tin&latest=1&dollar_only=1&payer=Payer%2003&cpt=97110&page=1&page_size=100 | 89 | 76 | 87 (200) | 71328 | 200 |
| 19 | `GET /api/summary` | explorer | grain=tin&latest=1&dollar_only=1&payer=Payer%2003&cpt=97110 | 141 | 2 | 67 (200) | 511 | 200 |
| 20 | `GET /api/rates` | explorer | grain=tin&latest=1&dollar_only=1&q=PRACTICE%20124&page=1&page_size=100 | 1438 | 986 | 1636 (200) | 71370 | 200 |
| 21 | `GET /api/summary` | explorer | grain=tin&latest=1&dollar_only=1&q=PRACTICE%20124 | 1600 | 3 | 1741 (200) | 3429 | 200 |
| 22 | `GET /api/rates` | explorer | grain=tin&latest=1&dollar_only=1&q=100049428&page=1&page_size=100 | 274 | 161 | 273 (200) | 71260 | 200 |
| 23 | `GET /api/rates` | explorer | grain=tin&month=2026-09&dollar_only=1&page=1&page_size=100 | 1256 | 1080 | 1278 (200) | 71337 | 200 |
| 24 | `GET /api/summary` | explorer | grain=tin&month=2026-09&dollar_only=1 | 5873 | 3 | 6169 (200) | 3510 | 200 |
| 25 | `GET /api/rates` | explorer | grain=tin&latest=1&dollar_only=1&state=MO&sort=display_name&dir=asc&page=2&page_size=100 | 1863 | 1744 | — | 70628 | 200 |
| 26 | `GET /api/rates` | explorer | grain=entity&latest=1&dollar_only=1&page=1&page_size=100 | 4 | 3 | 3 (422) | 190 | 422 |
| 27 | `GET /api/summary` | explorer | grain=entity&latest=1&dollar_only=1 | 2 | 2 | 2 (422) | 190 | 422 |
| 28 | `GET /api/rates` | explorer | grain=entity&latest=1&dollar_only=1&payer=Payer%2003&cpt=97110&page=1&page_size=100 | 409 | 243 | 392 (200) | 68403 | 200 |
| 29 | `GET /api/rates` | explorer | grain=npi&latest=1&dollar_only=1&payer=Payer%2003&cpt=97110&page=1&page_size=100 | 2856 | 814 | 5216 (200) | 67704 | 200 |
| 30 | `GET /api/summary` | explorer | grain=npi&latest=1&dollar_only=1&payer=Payer%2003&cpt=97110 | 963 | 3 | 959 (200) | 514 | 200 |
| 31 | `GET /api/rates` | explorer | grain=npi&latest=1&dollar_only=1&q=1000494280&page=1&page_size=100 | 1115 | 409 | 8941 (200) | 37639 | 200 |
| 32 | `GET /api/entity/tin/100049428` | explorer | — | 818 | 765 | 782 (200) | 407474 | 200 |
| 33 | `GET /api/entity/entity/PRACTICE%2018%20%C2%B7%20OR` | explorer | — | 30409 | 15911 | 4039 (409) | 441003 | 200 |
| 34 | `GET /api/entity/npi/1000494280` | explorer | — | 851 | 800 | 836 (200) | 44596 | 200 |
| 35 | `POST /api/validate` | explorer | {"code": "97110", "id": "100049428"} | 13890 | 2666 | 2753 (200) | 7982 | 200 |
| 36 | `GET /api/export.csv` | explorer | grain=tin&latest=1&dollar_only=1&payer=Payer%2003&cpt=97110 | 1191 | 569 | 480 (200) | 14575994 | 200 |
| 37 | `GET /api/export.zip` | explorer | grain=tin&latest=1&dollar_only=1&state=MO&cpt=97110 | 1020 | 1047 | 1067 (200) | 1216809 | 200 |
| 38 | `GET /api/export.csv` | explorer | grain=tin&latest=1&dollar_only=1 | 8212 | 8690 | — | 524 | 507 |
| 39 | `GET /api/export.zip` | explorer | grain=entity&latest=1&dollar_only=1&payer=Payer%2003&cpt=97110 | 1201 | 1192 | — | 898172 | 200 |
| 40 | `GET /api/export/outreach.zip` | explorer | grain=tin&latest=1&dollar_only=1&state=MO | 11555 | 11448 | 9628 (200) | 1546068 | 200 |
| 41 | `GET /api/export/outreach.csv` | explorer | grain=entity&latest=1&dollar_only=1&state=MO | 15577 | 13402 | — | 4249029 | 200 |
| 42 | `GET /api/export/outreach.zip` | explorer | grain=tin&latest=1&dollar_only=1 | 71110 | 72230 | — | 18549685 | 200 |
| 43 | `GET /api/code/97110` | cpt | grain=tin | 985 | 918 | 891 (200) | 123484 | 200 |
| 44 | `GET /api/trend` | cpt | cpt=97110&grain=tin | 489 | 497 | 478 (200) | 4869 | 200 |
| 45 | `GET /api/code/97110` | cpt | grain=tin&state=MO | 554 | 989 | 1103 (200) | 124257 | 200 |
| 46 | `GET /api/trend` | cpt | cpt=97110&grain=tin&state=MO | 441 | 403 | 529 (200) | 4826 | 200 |
| 47 | `GET /api/code/97110` | cpt | grain=tin&modifier=base&billing_class=professional | 1377 | 1351 | — | 123347 | 200 |
| 48 | `POST /api/market/geography` | markets | {"code": "97110", "market": {"month": "latest", "state": "MO"}} | 579 | 647 | 678 (200) | 1751 | 200 |
| 49 | `POST /api/market/negotiability` | markets | {"code": "97110", "market": {"month": "latest", "state": "MO"}} | 127 | 120 | 123 (200) | 2977 | 200 |
| 50 | `POST /api/market/medicare` | markets | {"code": "97110", "market": {"month": "latest", "state": "MO"}} | 235 | 280 | 313 (200) | 1836 | 200 |
| 51 | `POST /api/market/differential` | markets | {"code": "97110", "market": {"month": "latest", "state": "MO"}} | 281 | 219 | 173 (200) | 3553 | 200 |
| 52 | `POST /api/market/geography` | markets | {"code": "97110", "market": {"month": "latest"}} | 550 | 624 | — | 1751 | 200 |
| 53 | `POST /api/territory/local` | markets | {"code": "97110", "market": {"month": "latest", "state": "MO"}, "state": "MO"} | 9447 | 5327 | 5291 (200) | 3101 | 200 |
| 54 | `POST /api/territory/radius` | markets | {"code": "97110", "market": {"month": "latest", "state": "MO"}, "max_miles": 100, "zip"… | 580 | 128 | 461 (200) | 1356 | 200 |
| 55 | `POST /api/territory/concentration` | markets | {"code": "97110", "market": {"month": "latest", "state": "MO"}} | 967 | 2 | 903 (200) | 2325 | 200 |
| 56 | `POST /api/changes` | changes | {"market": {"month": "2026-10", "payers": [], "state": "MO"}} | 3095 | 136 | 2620 (200) | 1774452 | 200 |
| 57 | `POST /api/changes` | changes | {"market": {"month": "2026-10", "payers": ["Payer 03"], "prev_month": "2026-08", "state… | 1032 | 117 | — | 1541560 | 200 |
| 58 | `POST /api/changes` | changes | {"market": {"month": "2026-10", "payers": []}, "subject": "100049428"} | 332 | 2 | — | 6507 | 200 |
| 59 | `POST /api/changes.csv` | changes | {"market": {"month": "2026-10", "payers": [], "state": "MO"}} | 58 | 58 | — | 692235 | 200 |
| 60 | `POST /api/changes` | changes | {"market": {"month": "2026-10", "payers": []}} | 16626 | 142 | — | 1773714 | 200 |
| 61 | `POST /api/trajectory` | changes | {"market": {"payers": [], "state": "MO"}} | 3175 | 3 | 2772 (200) | 12095 | 200 |
| 62 | `POST /api/trajectory` | changes | {"market": {"allow_national": true, "payers": []}} | 27319 | 3 | — | 12164 | 200 |
| 63 | `POST /api/benchmark/market` | benchmark | {"market": {"billing_class": "professional", "month": "latest", "payers": [], "state": … | 850 | 328 | 290 (200) | 8830 | 200 |
| 64 | `POST /api/benchmark/market` | benchmark | {"market": {"billing_class": "professional", "month": "latest", "payers": [], "target_p… | 1282 | 1229 | — | 8835 | 200 |
| 65 | `POST /api/benchmark/opportunity` | benchmark | {"market": {"billing_class": "professional", "month": "latest", "payers": [], "state": … | 234 | 243 | 3310 (200) | 10192 | 200 |
| 66 | `POST /api/contract-gaps` | benchmark | {"market": {"billing_class": "professional", "month": "latest", "payers": [], "state": … | 313 | 262 | 1217 (200) | 821 | 200 |
| 67 | `POST /api/quality/size` | benchmark | {"market": {"billing_class": "professional", "month": "latest", "payers": [], "state": … | 2078 | 2 | 2102 (200) | 1384 | 200 |
| 68 | `POST /api/quality/subject-types` | benchmark | {"market": {"billing_class": "professional", "month": "latest", "payers": [], "state": … | 124 | 2 | 1059 (200) | 588 | 200 |
| 69 | `POST /api/report/pitch` | benchmark | {"market": {"billing_class": "professional", "month": "latest", "payers": [], "state": … | 3244 | 428 | 3602 (200) | 32002 | 200 |
| 70 | `POST /api/report/negotiation` | benchmark | {"market": {"billing_class": "professional", "month": "latest", "payers": [], "state": … | 712 | 793 | 266 (200) | 31878 | 200 |
| 71 | `POST /api/negotiate/compare` | negotiate | {"comparables": [], "market": {"month": "latest", "state": "MO", "therapy_only": false}… | 489 | 466 | 1107 (200) | 10844 | 200 |
| 72 | `POST /api/negotiate/proposal` | negotiate | {"comparables": [], "market": {"month": "latest", "state": "MO", "therapy_only": false}… | 505 | 479 | 1152 (200) | 5813 | 200 |
| 73 | `POST /api/report/proposal` | negotiate | {"comparables": [], "market": {"month": "latest", "state": "MO", "therapy_only": false}… | 491 | 475 | — | 12084 | 200 |
| 74 | `POST /api/report/payer-compare` | negotiate | {"comparables": [], "market": {"month": "latest", "state": "MO", "therapy_only": false}… | 490 | 484 | 1057 (200) | 16040 | 200 |
| 75 | `POST /api/leverage` | negotiate | {"market": {"month": "latest", "state": "MO", "therapy_only": false}, "payer": "Payer 0… | 56 | 56 | 60 (422) | 120 | 422 |
| 76 | `POST /api/leverage/summary` | negotiate | {"market": {"month": "latest", "state": "MO", "therapy_only": false}, "payer": "Payer 0… | 302 | 302 | 562 (200) | 1175 | 200 |
| 77 | `POST /api/economics/visit` | negotiate | {"market": {"month": "latest", "state": "MO", "therapy_only": false}, "subject": "10004… | 17 | 13 | 11 (200) | 667 | 200 |
| 78 | `POST /api/economics/weighted-position` | negotiate | {"market": {"month": "latest", "state": "MO", "therapy_only": false}, "subject": "10004… | 279 | 300 | — | 1005 | 200 |
| 79 | `POST /api/floors/compare` | negotiate | {"market": {"month": "latest", "therapy_only": false}, "payer": "Payer 03", "state": "M… | 5 | 6 | — | 821 | 200 |
| 80 | `POST /api/hospital/parity` | negotiate | {"market": {"month": "latest", "therapy_only": false}, "payer": "Payer 03", "state": "M… | 12 | 8 | — | 711 | 200 |
| 81 | `POST /api/remits/check` | negotiate | {"basis": "allowed", "market": {"month": "latest", "state": "MO"}, "subject": "10004942… | 174 | 168 | 174 (200) | 1377 | 200 |
| 82 | `POST /api/remits/check.csv` | negotiate | {"basis": "allowed", "market": {"month": "latest", "state": "MO"}, "subject": "10004942… | 195 | 171 | — | 910 | 200 |
| 83 | `POST /api/schedule/fee` | ratecard | {"market": {"billing_class": "professional", "discipline": "PT", "month": "latest", "pa… | 10174 | 441 | 1078 (200) | 8833 | 200 |
| 84 | `POST /api/schedule/fee` | ratecard | {"market": {"billing_class": "professional", "month": "latest", "payers": []}, "subject… | 873 | 873 | — | 12818 | 200 |
| 85 | `GET /api/schedule/visit/presets` | ratecard | — | 3 | 2 | — | 1503 | 200 |
| 86 | `POST /api/schedule/visit` | ratecard | {"market": {"billing_class": "professional", "discipline": "PT", "month": "latest", "pa… | 176 | 213 | 709 (200) | 4022 | 200 |
| 87 | `POST /api/schedule/visit/market` | ratecard | {"market": {"discipline": "PT", "month": "latest", "payers": [], "state": "MO"}, "prese… | 982 | 2 | 271 (422) | 7175 | 200 |
| 88 | `POST /api/schedule/visit/market.csv` | ratecard | {"market": {"discipline": "PT", "month": "latest", "payers": [], "state": "MO"}, "prese… | 2 | 2 | — | 5924 | 200 |
| 89 | `POST /api/report/visitvalue` | ratecard | {"market": {"discipline": "PT", "month": "latest", "payers": [], "state": "MO"}, "prese… | 2 | 2 | — | 11312 | 200 |
| 90 | `POST /api/report/ratecard` | ratecard | {"market": {"billing_class": "professional", "discipline": "PT", "month": "latest", "pa… | 1313 | 481 | 2641 (200) | 32416 | 200 |
| 91 | `POST /api/schedule/fee.csv` | ratecard | {"market": {"billing_class": "professional", "discipline": "PT", "month": "latest", "pa… | 310 | 280 | — | 12812 | 200 |
| 92 | `POST /api/report/org-bundle.zip` | ratecard | {"market": {"billing_class": "professional", "discipline": "PT", "month": "latest", "pa… | 787 | 732 | — | 4178 | 200 |
| 93 | `POST /api/leads` | leads | {"market": {"month": "latest", "payers": [], "state": "MO", "therapy_only": true}, "min… | 1977 | 5 | 1262 (200) | 36372 | 200 |
| 94 | `POST /api/leads` | leads | {"market": {"month": "latest", "payers": [], "therapy_only": true}, "min_codes": 3, "th… | 7214 | 5 | — | 36313 | 200 |
| 95 | `POST /api/leads.csv` | leads | {"market": {"month": "latest", "payers": [], "state": "MO", "therapy_only": true}, "min… | 1846 | 4 | — | 12350 | 200 |
| 96 | `POST /api/leaderboard` | leads | {"limit": 50, "market": {"month": "latest", "payers": [], "state": "MO", "therapy_only"… | 471 | 3 | 499 (200) | 12153 | 200 |
| 97 | `POST /api/leaderboard` | leads | {"limit": 50, "market": {"month": "latest", "payers": [], "therapy_only": true}, "min_c… | 4900 | 3 | — | 12138 | 200 |
| 98 | `POST /api/report/dossier.json` | leads | {"market": {"month": "latest", "state": "MO", "therapy_only": false}, "radius_miles": 2… | 1803 | 1713 | 2504 (200) | 953 | 200 |
| 99 | `POST /api/report/dossier` | leads | {"market": {"month": "latest", "state": "MO", "therapy_only": false}, "radius_miles": 2… | 1603 | 1649 | 2499 (200) | 5855 | 200 |
| 100 | `POST /api/report/market` | leads | {"code": "97110", "market": {"month": "latest", "therapy_only": false}, "state": "MO", … | 7299 | 5134 | 7615 (200) | 12269 | 200 |
| 101 | `POST /api/utilization/growth` | leads | {"subject": "100049428"} | 3 | 3 | — | 721 | 200 |
| 102 | `POST /api/utilization/service-lines` | leads | {"subject": "100049428"} | 11 | 10 | — | 634 | 200 |
| 103 | `GET /api/utilization/years` | leads | — | 3 | 3 | — | 12 | 200 |
| 104 | `GET /api/utilization/status` | leads | — | 6 | 3 | — | 361 | 200 |
| 105 | `POST /api/utilization/volumes` | leads | {"market": {"state": "MO"}, "multiplier": 1, "subject": "100049428"} | 11 | 11 | — | 157 | 422 |
| 106 | `POST /api/territory/steal-share` | leads | {"subject": "100049428"} | 469 | 444 | — | 791 | 200 |
| 107 | `GET /api/newclinics/status` | leads | — | 2 | 1 | — | 674 | 200 |
| 108 | `POST /api/newclinics` | leads | {"days": 90, "radius_miles": null, "state": "MO", "zip": null} | 2 | 1 | — | 568 | 200 |
| 109 | `GET /api/closures/status` | leads | — | 1 | 1 | — | 756 | 200 |
| 110 | `POST /api/closures` | leads | {"days": 90, "radius_miles": null, "state": "MO", "zip": null} | 2 | 1 | — | 650 | 200 |
| 111 | `GET /api/demographics/status` | leads | — | 4 | 3 | — | 415 | 200 |
| 112 | `POST /api/market/sizing` | leads | {"radius_miles": 25, "zip": "63101"} | 7018 | 8259 | — | 763 | 200 |
| 113 | `GET /api/clients` | clients | — | 6 | 3 | — | 26 | 200 |
| 114 | `GET /api/clients/digest` | clients | — | 17255 | 7 | — | 2856 | 200 |
| 115 | `GET /api/packets` | clients | — | 2 | 1 | — | 43 | 200 |
| 116 | `POST /api/contracts/renewals` | clients | {"market": {"month": "latest"}, "subjects": ["100049428", "PRACTICE 0"], "within_days":… | 1942 | 43 | 40 (200) | 450 | 200 |
| 117 | `GET /api/contracts/expiry-coverage` | clients | — | 2029 | 2 | — | 1291 | 200 |
| 118 | `POST /api/contracts/new-to-network` | clients | {"market": {"month": "latest"}, "radius_miles": null, "therapy_only": false, "zip": null} | 8043 | 9241 | 8252 (200) | 1796 | 200 |
| 119 | `GET /api/engagements` | clients | — | 6 | 4 | — | 16 | 200 |
| 120 | `POST /api/engagements/compare` | clients | {"subject": "100049428", "volumes": {"97110": 3000, "97140": 1500, "97530": 800}} | 2 | 4 | — | 122 | 422 |
| 121 | `GET /api/medicare/status` | medicare | — | 8 | 5 | — | 35 | 200 |
| 122 | `GET /api/medicare/tracker` | medicare | — | 4 | 4 | — | 265 | 200 |
| 123 | `POST /api/medicare/leaders` | medicare | {"dataset_id": null, "limit": 100, "radius_miles": null, "therapy_only": true, "zip": n… | 3 | 3 | — | 600 | 200 |
| 124 | `POST /api/medicare/org` | medicare | {"dataset_id": null, "limit": 500, "subject": "100049428"} | 697 | 441 | — | 1553 | 200 |
| 125 | `POST /api/medicare/eligibility` | medicare | {"text": "1000494280, 1000710640"} | 3 | 2 | — | 105 | 422 |
| 126 | `POST /api/roster` | roster | {"limit": 500, "market": {"month": "latest", "state": "MO"}, "min_codes": 1, "payer": "… | 291 | 13 | 262 (200) | 117011 | 200 |
| 127 | `POST /api/roster.csv` | roster | {"limit": 500, "market": {"month": "latest", "state": "MO"}, "min_codes": 1, "payer": "… | 8 | 6 | — | 31813 | 200 |
| 128 | `GET /api/files` | files | limit=500 | 11 | 9 | — | 21182 | 200 |
| 129 | `GET /api/urls` | files | — | 20 | 15 | — | 148 | 200 |
| 130 | `GET /api/known-sources` | files | — | 145 | 84 | — | 91040 | 200 |
| 131 | `GET /api/inflation/status` | files | — | 4 | 3 | — | 787 | 200 |
| 132 | `GET /api/floors/status` | files | — | 3 | 3 | — | 838 | 200 |
| 133 | `GET /api/hospital/status` | files | — | 5 | 5 | — | 766 | 200 |
| 134 | `GET /api/sources` | sources | — | 81 | 81 | 81 (200) | 21860 | 200 |
| 135 | `POST /api/packets/build` | clients | {} | 3 | 2 | — | 16 | 200 |


Notes on the table:
* Refusals are honest and expected: entity grain without payer+code (422 "store is large"), `leverage` (synthetic ZIP 45999 not a ZCTA), utilization/engagements/medicare layers not loaded (422 sentences), `export.csv` all-latest (507: 14.4 GB needed, 7.4 GB free), entity drawer variant `PRACTICE 12428` (409 "several practices are named…" — a bare name shared by states; the UI always sends the `NAME · ST` key).
* `export_csv_all_latest` and `outreach_zip_tin_ALL` are the only "bytes differ" cases besides `packets_build` (`{"started":true}` then "already running"): the 507 sentence quotes free space (7.4 vs 7.3 GB) and the ZIP member names carry a `HHMM` stamp that crossed a minute (the CSV content of the outreach export was byte-identical on both real-store runs). Everything else (133 routes) was byte-identical cold vs warm after stripping timestamps. Real store: 118/118 bodies identical across two calls (reports included).
* `schedule_fee` cold 10.2 s is the one-time build of the 0.7.6 table `practice_medians_disc_payer_v1` for this store (not on disk before this run); after it the rate card is 0.44 s. Documented behaviour (AI_HANDOFF round 12).
* Packets: `POST /api/packets/build` answers at once; the build for the 1 watch-listed client finished ~15 s later (serve.log 01:16:14), with the digest already cached.

### Ten slowest features (cold / warm, ms)

| # | feature | cold | warm | 3rd call | caching |
|---|---|---:|---:|---:|---|
| 1 | Outreach ZIP, whole store (`/api/export/outreach.zip?grain=tin`) | 71,110 | 72,230 | — | none |
| 2 | Entity drawer (`/api/entity/entity/PRACTICE 18 · OR`) | 30,409 | 15,911 | 4,039 (409) | none |
| 3 | Trajectory, national (`/api/trajectory` allow_national) | 27,319 | 3 | — | per generation |
| 4 | Overview, national (`/api/overview`) | 26,900 | 2 | 1,512 (MO) | per (state, generation) |
| 5 | Clients digest, 1 client (`/api/clients/digest`) | 17,255 | 7 | — | per generation |
| 6 | Rate changes, national 2026-10 (`/api/changes`) | 16,626 | 142 | — | per generation |
| 7 | Outreach CSV, entity grain, MO (`/api/export/outreach.csv`) | 15,577 | 13,402 | — | none |
| 8 | Validate (`/api/validate` TIN + 97110) | 13,890 | 2,666 | 2,753 | none |
| 9 | Outreach ZIP, TIN grain, MO | 11,555 | 11,448 | 9,628 (TX) | none |
| 10 | Rate card first build (`/api/schedule/fee`, state+PT) | 10,174 | 441 | 1,078 | table once per generation |
| 11 | City rate map (`/api/territory/local`) | 9,447 | 5,327 | 5,291 | none |
| 12 | Whole-store CSV refusal (`/api/export.csv` all latest, 507) | 8,212 | 8,690 | — | none |
| 13 | New to network (`/api/contracts/new-to-network`) | 8,043 | 9,241 | 8,252 | none |
| 14 | Market report (`/api/report/market` MO) | 7,299 | 5,134 | 7,615 | partly |
| 15 | Posture (`/api/quality/posture` national) | 7,248 | 3 | 1,211 | per (generation, market) |
| 16 | Leads, national | 7,214 | 5 | — | per generation |
| 17 | Market sizing (`/api/market/sizing`, no demographics loaded) | 7,018 | 8,259 | — | none |
| 18 | `/api/stats` (first start) | 6,823 | 8 | — | TTL + single-flight |

## 2. Stage breakdowns of the 8 slowest (in-process, statement-level; `perf/prof.json`)

In-process times differ from HTTP cold times (the disk cache was dropped again but the process had warmed DuckDB's catalog); the SHARES are what a progress bar needs.

1. **Outreach ZIP, whole store — 127.8 s in-process (71 s over HTTP; the profiler itself inflates Python-heavy code), SQL only 22.6 s of it.** Stages: (a) list the units + their TIN/payer/month strings over the filtered relation 6.9 s (5 %); (b) build `_outreach_units` + `_outreach_rates` temp tables from `practice_medians_v1` 3.6 s (3 %); (c) `_geo_by_tin` + `_tin_meta`: one raw `SELECT DISTINCT tin_value, npi FROM rates` scan 11.6 s + Python dict building 24.6 s + 24.9 s (48 %); (d) `build_outreach_rows` per-unit loop 11.5 s plus **11,461,266 calls of `rounding.half_up` = 40 s** (the per-code `sorted(half_up(v, 2) …)` and per-cell rounding over 194,000 units x 10 codes) (40 %); (e) `outreach_csv` formatting 16.5 s (13 %); (f) zip. Every stage is Python-side except (a)–(c)'s SQL. MO state (16.5 s in-process / 11.5 s HTTP): the same shape — tin_npi raw scan 5.3 s, units 2.0 s, `_geo_by_tin`/`_tin_meta` 7.5 s each (Python), half_up 955,542 calls 3.3 s.
2. **Entity drawer (name entity) — 14.2 s in-process, 30.4 s HTTP cold, 15.9 s HTTP warm.** Stages: `_resolve_entity_key` (`SELECT 1 FROM (entity-keyed whole spine) WHERE entity_key = ?`) 2.8 s; `SELECT * FROM (_ENTITY_REL) WHERE unit_id = ?` — the entity relation aggregates the WHOLE 79M-row spine into entities and only then filters 5.5 s; the member-TIN list query over the same keyed spine 2.8 s; the chart (`_entity_rel(_TIN_REL_LATEST)` again) 2.3 s; member NPIs from raw `rates` 0.6 s. Four whole-spine passes to open one practice. The TIN drawer takes 0.8 s because `unit_id = tin_value` prunes.
3. **Trajectory, national — 27.0 s, one statement (26.5 s):** the matched-panel window pass over the whole spine (`base AS MATERIALIZED (… median per payer/code/month/TIN …)`). Cached per generation; round 9 already measured this as the fastest variant. Cold-only cost; the MO scope is 3.2 s.
4. **Overview, national — 12.8 s in-process / 26.9 s HTTP cold:** temp table `__overview` (house-basis spine rows) 2.7 s (21 %); payer index CTE (per-TIN medians per payer x code, then per code) 7.0 s (55 %); top codes per-TIN medians 2.2 s (17 %); counts 0.6 s; discipline medians 0.3 s. Every stage recomputes per-practice medians that `practice_medians_v1` / `practice_medians_payer_v1` already hold.
5. **Clients digest (1 client) — 7.7 s in-process / 17.3 s HTTP cold, 20 statements:** per-payer "latest vs previous" rate-change diff for the client 2.7 s (35 %); `SELECT DISTINCT npi FROM rates WHERE tin_value IN (…)` — a raw 99M-row scan to list the client's NPIs 2.1 s (27 %); `DISTINCT tin_value, billing_code` over the latest spine (contract gaps book) 1.1 s (15 %); gaps from the materialised medians 0.7 s; `payer_months` twice 0.8 s. With N clients the first two stages repeat per client.
6. **Rate changes, national — 17.5 s in-process, one statement 15.9 s:** the two-month self-join over the spine (`cur`/`prev` CTEs, per-payer pairs). Cached per generation; MO 3.1 s.
7. **Outreach CSV, entity grain, MO — 19.2 s in-process / 15.6 s HTTP:** units over the entity relation 5.3 s; tin_npi raw scan 5.0 s; `_outreach_units` temp table (entity relation again) 4.1 s; `_geo_by_tin`/`_tin_meta` Python 10.4 s each (overlapping); CSV 0.8 s.
8. **Validate — 2.4 s in-process / 13.9 s HTTP cold / 2.7 s warm, one statement:** `FROM rates WHERE billing_code = ? AND (tin_value = ? OR npi = ?)` reads every raw parquet part (the OR defeats pruning; parts are not ordered by TIN).

Also measured (HTTP cold > 5 s): `territory/local` 9.4 s = two `base` CTE statements (3.2 + 2.3 s, each a `median per TIN over the latest spine` + the `exploded` CTE's `SELECT DISTINCT tin_value, npi FROM rates` raw scan) and runs again inside `report/market` (5.2 s of its 7.3 s); `contracts/new-to-network` one 6.4–10.6 s statement, uncached; `market/sizing` 6.6–8.4 s = `pairs AS (SELECT DISTINCT tin_value, npi FROM rates …)` raw scan BEFORE it discovers no demographics are loaded; `quality/posture` 6.9 s and `quality/types` 4.5 s whole-spine aggregates on the first Overview visit (cached after); `leads` national 8.3 s (ranked CTE 4.6 s + worst-payer 1.5 s + others-median 1.0 s); `leaderboard` national 5.5 s one statement; `report/pitch` cold 5.4 s = the trajectory `base` for the subject's payers 3.3 s (cached after); `/api/stats` 6.8 s HTTP cold = `count(DISTINCT tin_value), count(DISTINCT payer)` over the spine + two `rate_npi_set` counts, 0.54 s each warm disk; `export.csv` all-latest 507 after a 10.5 s `count(*)` over the full export select (documented: "7 s on a 99M store").

## 3. Chromium walk (14 views, `perf/ui/ui_walk.json`)

Initial load 4.81 s to network idle (the server had already served the timing run, so per-generation caches were warm). Then, per view, time to network idle: overview 0.11 s, clients 0.06, explorer 0.12, cpt 0.09, markets 0.06, changes 0.08, benchmark 0.06, negotiate 0.13, ratecard 0.07, leads 0.08, medicare 0.08, roster 0.05, files 0.07, sources 0.12. Benchmark run (TIN 100049428, MO) 1.2 s; rate card run (MO, PT) 0.48 s; "Show visit value in this market" 0.55 s. **0 page errors, 0 console errors, 0 failed requests, 0 responses >= 400.** Screenshots: `perf/ui/benchmark.png`, `perf/ui/ratecard.png` (the rate card shows the scorecard, fee schedule, visit pricing and the visit-value block for MO/PT). The cold numbers a first-time visitor pays per view are the route times in section 1 (Overview: 26.9 s + posture 7.2 s + types 4.4 s in parallel on a fresh start).

## 4. Accuracy

### 4.1 Independent recomputation (raw `rates`, house rules) — what agreed

`perf/check_real.py` recomputes from raw rows: newest file_month per (payer, TIN, npi-flag, code) over ALL the contract's rows; professional; dollar; > $0.01; no NPI-in-slot; base modifier sets; each variant = `median(DISTINCT rate)` over NPIs rounded to 4 dp; practice rate per (payer, code) = median over variants; entity rate = median of member TINs; mid-rank positions; `half_up`.

* **Real store, TX subject 760513962 (Coppell Spine, Moda), state TX: 734 checks, 0 differences**; WY subject 208741495 (Alliance PT, BCBS WY): 202 checks, 0 differences. Covered: Benchmark (subject_rate, n_peers, p25/p50/p75, exact percentile, in_position for all 70 codes; overall position 45 = median of the 29 ranking codes' exact mid-ranks = `quality/size` position = dossier `median_percentile`), within-payer average (48.9 = quality/size), fee schedule (every payer x code rate; market medians = median of OTHER practices, suppressed under 3 peers; peer counts), Negotiate compare (subject rate, p50, exact percentile), proposal (`proposed = max(current, p50)`), opportunity (floor at $0), Explorer summary strip by-code medians and entity counts, Code page payer ranking (median over practices, counts), Overview top codes and payer index (ratio of payer median to the market's per-practice median, >= 3 practices, >= 3 codes), Markets geography (state and national medians and counts), negotiability (p50/p90/n), payer concentration (practice counts, shares, HHI from exact shares), city rate map (cities of NPIs located in the state), leaderboard positions and median rates, roster position/rate for the subject, outreach merge fields (C<code>_RATE, _MKT_MEDIAN = median of the others, _PCTL = mid-rank among the others), visit value in this market (per payer: practices, mean, median, P25, P75, suppression under 5, the subject's value and percentile; "All payers" = each practice's median of its payer values).
* **Entity grain** (`/api/rates?grain=entity`): the two multi-TIN TX entities' rates = the median of their member TINs (2/2 rows); on the 99M store the entity drawer for `PRACTICE 18 · OR` (2 TINs): **637 rows, 0 mismatches** (per payer/code/modifiers/class/POS/month) and its chart (latest, base, professional per payer x code) 6/6.
* **99M store, MO / TIN 100049428** (`perf/spot99.py`, raw scan 20–27 s): Benchmark MO all 25 codes (subject rate, 15,9xx peers, p25/p50/p75, exact percentile), overall position 44 = quality/size = dossier, fee schedule (8 codes x 4 payers: rates, market medians, peer counts), Negotiate Payer 03 (8 codes), Leads MO top 3 (median percentile, code count, average gap to the others' median), **Rate changes Payer 03 MO 2026-10 vs 2026-08: 6 rows old/new/% exact from the two months' raw rows**, visit value MO/PT (5 payer rows: practices/mean/median/P25 and the subject's value & percentile; All payers 14,576 practices, median $263.01, mean $262.93), roster Payer 03 subject row — **0 differences**. Independent MO payer index: Payer 03 1.000, Payer 07 0.997 (the national overview body shows the same two at 1.0/0.997 ordering; the MO body was not saved).
* Cross-screen consistency on the 99M bodies: Benchmark overall p44 = size panel p44 = dossier facts 44 = `economics/weighted-position` median_percentile 44 (its "unweighted" 39.0 is the documented mean); Negotiate's headline p40 is the within-Payer-03 position (a different question, labelled as such); rate card payer rates (Payer 01 $46.29, 02 $67.15, 03 $46.01, 04 $20.33 for 97110) equal the visit calculator's units x rate lines and the fee CSV.

### 4.2 Findings, by severity

**P0 (wrong number): none found.** Every printed number checked matched the independent computation on both stores.

**P1**

* **P1-1 Visit-value block: the quartile label contradicts the percentile on the same row when the distribution is tied — real data.** TX / PT / ortho preset, subject 760513962, "All payers" row: P25 = $105.44, median = $105.44, P75 = $110.31; the practice's visit = $105.44, percentile **26** ("this practice included" mid-rank), quartile **Q3** (`perf/realout_tx/visit_market.json`, also in the printable and CSV). The written rule (`visit_market.SUBJECT_NOTE`: "Q1 below P25, Q2 below the median, Q3 below P75") is applied exactly (`_position`: `v < p25`? no; `v < median`? no; `v < p75` -> Q3), so a practice AT the median — and here at P25 too — is labelled third quartile while sitting at p26. Fee schedules tie heavily (1,668 TX practices, 4 distinct visit values), so this is the common case on real data, not an edge. A reader expects "Q2" for a p26 practice (or "at the median"). Fix without moving any other number: derive the quartile from the printed percentile (p < 25 -> Q1, < 50 -> Q2, < 75 -> Q3, else Q4), or print "= median" / "= P25" when the value equals the boundary. On the 99M store the same subject reads p13 / Q1 (consistent) because there are no ties.
* **P1-2 Two screens, one practice, one code: Benchmark "YOU" $46.29 vs the rate card's four payer rates whose median is $46.15 (97110, TIN 100049428).** `practice_medians_v1` is the median over ALL of the practice's base lines (documented), so Payer 01's two POS sets (`11`, `11|22`, both $46.29) count twice and Payer 02/03/04 once each: median(46.29, 46.29, 67.15, 46.01, 20.33) = 46.29, while one-rate-per-payer gives 46.15. Both are "written rules", neither is wrong, but the methodology text ("median per practice") does not say a payer with more POS variants weighs more. Either collapse to one rate per (payer, code) before the practice median (changes numbers — a decision), or add the sentence. Classified P1 because it is the number a client is quoted.

**P2 (slow: > 5 s cold or > 2 s warm, or wasteful) — see section 5 for the fix and the measured gain**

* P2-1 Entity drawer (name entity): 30.4 s cold / 15.9 s warm, four whole-spine passes (prototype 11x).
* P2-2 Outreach exports: whole store 71 s, one state 11.5–15.6 s, no cache, ~80 % Python.
* P2-3 Raw `rates` scans for TIN -> NPI pairs on every call: `territory/local` (9.4/5.3 s, also inside `report/market` 7.3/5.1 s), `market/sizing` (7.0/8.3 s even with nothing loaded), `clients/digest` (2.1 s per client), outreach (`tin_npi`), new-to-network, steal-share (prototype: 16x on the pair lookup).
* P2-4 `contracts/new-to-network` 8.0–10.6 s on every click, uncached (a generation-keyed answer like changes/trajectory).
* P2-5 Overview national first visit 26.9 s (+ posture 7.2 s + types 4.4 s) recomputes per-practice medians the materialised tables hold (prototype: payer index 7.0 s -> 2.4 s, top codes 2.2 s -> 0.5 s, identical numbers).
* P2-6 `/api/validate` 13.9 s cold / 2.7 s warm: an un-prunable raw scan (prototype for the TIN half 3x).
* P2-7 `/api/stats` 6.8 s on a fresh start (a WARNING is logged at > 5 s) — the whole-spine `count(DISTINCT tin_value)` and two `rate_npi_set` scans; a per-generation persisted value (like the summary strip) would make it 0.

**P3**

* P3-1 `leads.csv` recomputes the list the screen just cached (1.8 s after a 5 ms `leads` hit): the UI sends no `limit` to `/api/leads` (default 100) and the CSV route uses `LEADS_LIMIT` (1000), so the cache key differs.
* P3-2 "Build dossier" fires `/api/report/dossier` (1.6 s) AND `/api/report/dossier.json` (1.8 s) at once; each assembles the whole dossier.
* P3-3 The Code comparison view opens on the FIRST catalog code (`90912 Biofeedback`, `app.js:165`), which the 99M store (and most stores) has no rows for — an empty first screen; the first code with data (the summary strip's by-code list) would be the natural default.
* P3-4 Whole-store CSV refusal takes 8–11 s to say no (`count(*)` over the full export select, documented as "7 s"): a count over `rates_by_tin WHERE is_latest` with the same filters and no dimension joins is a cheaper upper bound.
* P3-5 Single-payer markets are slower than all-payer ones on the materialised path: `benchmark/opportunity` with `payers: ["Payer 03"]` 3.3 s vs 0.23 s (the per-payer table is read without the code-cluster benefit for the subject-only side); `contract-gaps`/`quality/size` for a name subject 1.2/2.1 s vs 0.3 s.
* P3-6 Outreach ZIP member names carry a `HHMM` stamp, so two downloads a minute apart differ in bytes while the CSV content is identical (the methodology text also carries a time). Cosmetic; a date-only stamp would make the ZIP reproducible.
* P3-7 Server RSS peaked at 7.7 GB during the run (DuckDB pool at its automatic 6 GB cap plus Python); round 7 measured 6.3 GB on the same store.

**Checked and found fine (do not re-chase):** no 500 on any of the 135 + 118 calls; all refusals are sentences; determinism 133/135 byte-identical (the two exceptions are stamps), real store 118/118; mid-rank positions, others-median markets, the subject rule for a shared name (`QUEST DIAGNOSTICS INCORPORATED` / NJ resolved to the NJ TIN only), entity = median of member TINs, rate changes per payer pair with its own previous month, half-up rounding everywhere printed, the 0.7.6 pooled-month note, the visit calculator's units and rounding, outreach merge fields on the house basis.

## 5. Speed opportunities (what, why, measured gain; no code changed)

1. **Entity drawer — resolve the member TINs first, then aggregate only their spine rows.** Cause: `GRAIN_REL['entity']` builds entities over the entire spine before `WHERE unit_id = ?` (DuckDB cannot push a predicate on the computed entity key through the GROUP BY), and `_resolve_entity_key` + the member-TIN query + the chart each repeat a keyed whole-spine pass. Prototype (`perf/proto.py` 1): `tins = SELECT tin_value FROM tin_directory WHERE display_name || ' · ' || primary_state = key` (plus entity_map names), then `_entity_rel(_TIN_REL over rates_by_tin WHERE tin_value IN tins)` — **637 identical rows, 9.89 s -> 0.87 s (11x)** for the main statement; the same narrowing applies to the other three statements, so the drawer should land near the TIN drawer's 0.8 s.
2. **A (tin_value, npi) side table keyed by spine generation** (built like `tin_zip_place`: one raw scan, 1,939,999 pairs in 8.1 s on this store). Readers: `territory.local_rate_map` (`exploded` CTE), `demographics.market_sizing` (`pairs`), `clients._build_digest` (`DISTINCT npi FROM rates`), `outreach._geo_by_tin`/`_tin_meta` (`tin_npi`), `contracts.new_to_network`, `territory.steal_share`, the entity drawer's member NPIs. Prototype (`proto.py` 4): the MO pair lookup **3.68 s -> 0.23 s (16x), identical set**; it removes 5.0–11.6 s of SQL from each outreach export, ~5 s from the city map / market report, ~6.5 s from market sizing, 2 s per client from the digest. `market/sizing` should also test `zip_demographics` BEFORE placing practices (its 7–8 s answer is "nothing loaded").
3. **Outreach exports: move the Python out.** 40 s of 128 s is `half_up` x 11.5 M; `_geo_by_tin`/`_tin_meta` 25 s each are per-TIN dicts built in Python from fetchall; the per-unit loop 11.5 s; CSV 16.5 s. The mid-ranks and gaps can be computed in SQL once per code (`leads.midrank_pct_sql` + `others_median_sql` over `_outreach_rates`, rounded once with `round()`), geo/meta joined in the same statement, and the CSV written by DuckDB `COPY` like the Explorer export. Expected: SQL 22 s (less with item 2) and no Python loop — whole store ~25 s instead of 71–128 s, one state ~5 s instead of 12–16 s. Numbers unchanged (the SQL rules are the ones the module quotes).
4. **Overview from the materialised medians.** `market_overview` builds `__overview` from the spine and recomputes per-TIN medians three times. `practice_medians_payer_v1` + `practice_medians_v1` give the payer index and top codes **identically (verified against the served body) in 2.4 s + 0.5 s instead of 7.0 s + 2.2 s + the 2.7 s temp table**; the counts and the discipline medians can come from the same tables (and `practice_medians_disc_payer_v1` for the discipline filter). First visit would drop from ~27 s to ~4 s cold; the state variant equally.
5. **New-to-network: cache per (generation, therapy_only, zip/radius)** through `store.cached` like changes/trajectory (the statement has no other inputs); and with item 2 its placement join stops reading raw rows. 8–10 s per click -> once per data generation.
6. **Validate: restrict the raw scan to the files that can hold the id.** Prototype (`proto.py` 2): the TIN's files from `rates_by_tin.source_files` (11 of 45 parts) then the raw scan of those files only — **identical 30 rows, 1.91 s -> 0.61 s (3x)**; the NPI half can use the same trick once `rate_npi_set` (part, npi) is consulted for the parts holding the NPI (it already exists — `WHERE part IN (SELECT part FROM rate_npi_set WHERE npi = ?)`).
7. **`/api/stats` on a fresh start:** persist the three whole-store counts per spine generation beside `summary_strips.v2.<spine_key>.json` (they only change when the spine or the NPI set changes) — 6.8 s -> ~0 on every restart; the `_log_if_slow` warning would stop firing.
8. **Smaller, same numbers:** `leads.csv` should reuse the screen's cached result (send the screen's `limit` or key the cache without it) — 1.8 s saved per export; the dossier button should build once and render twice (JSON facts from the HTML build) — 1.7 s saved per click; `territory/local`'s two `base` CTEs (3.2 + 2.3 s) are the same per-TIN median twice (state scope and the reference) — compute once into a temp table; the first-visit Overview trio (overview + posture + types) runs three whole-spine scans in parallel on a 4-core box — sequencing posture/types after the overview, or deriving `types`' per-payer counts from the spine once per generation at rollup time, would cut the cold first screen.

Not worth chasing (measured, cached, documented): trajectory national 27 s and changes national 16.6 s are once per data generation (round 9 chose the single window pass after measuring every alternative); the 507 refusal's 8–11 s count is documented; the one-time `practice_medians_disc_payer_v1` build (9.7 s) happens once per spine generation.

## 6. Housekeeping

* The repository was not modified (`git status` clean). One mistake during the run: `real_fetch.py` was first launched with a RELATIVE config path, so `mrfx serve` fell back to the built-in defaults, created `data/mrfx_store` and `data/nppes` under the repo (gitignored, 1.5 GB, including an NPPES download) and started enrichment; the process was stopped by the script's Popen handle and both folders were deleted (`data/` is back to its Nov 5 contents: empty downloads/failed/inbox/processed).
* Created and deleted at the end: `perf/realstore/` (the small real store, 458 MB) and `scratchpad/scale4n/packets/` (the packet build's output for the one watch-listed client; `packets_r6` from round 6 was left alone). Nothing under `scale4n/store` was written by this audit except what the served app writes itself (the new `practice_medians_disc_payer_v1.<key>.parquet` table, `summary_strips` cache, the exports temp dir, diagnostics).
* Kept for the record: `perf/timings.csv`, `perf/timing_table.md`, `perf/bodies/`, `perf/realout_tx/`, `perf/realout_wy/`, `perf/prof.json`, `perf/prof.out`, `perf/ui/`, `perf/serve.log`, the scripts.

## 7. After 0.7.7 (round 13) — the same method, the same store

Measured on the 99,000,000-rate store through HTTP with `perf/driver.py`
(cold = first call after a fresh `mrfx serve` with the page cache dropped,
warm = the same call again). "Before" is §1 (c8b80fc). "After" is the second
fresh start on the 0.7.7 code: the first start after a new sliced spine also
builds the practice-median tables once (Overview 12,343 ms, rate card
6,763 ms, `/api/stats` 8,886 ms that one time).

| feature (route) | before cold / warm ms | after cold / warm ms |
|---|---:|---:|
| outreach, whole store (`/api/export/outreach.zip`, all) | 71,110 / 72,230 | 37,247 / 36,914 |
| entity drawer by name (`/api/entity/entity/PRACTICE 18 · OR`) | 30,409 / 15,911 | 1,598 / 1,473 |
| Overview (`/api/overview`) | 26,900 / 2 | 2,303 / 2 |
| client digest (`/api/clients/digest`) | 17,255 | 4,962 |
| outreach CSV, entity grain, MO | 15,577 | 8,266 |
| validate a tax ID (`/api/validate`) | 13,890 / 2,666 | 550 / 549 |
| outreach, one state (MO) | 11,555 / 11,448 | 4,889 |
| fee schedule (`/api/schedule/fee`) | 10,174 | 215 (6,763 first start) |
| city rate map (`/api/territory/local`) | 9,447 / 5,327 | 201 / 188 |
| whole-store CSV refusal (`/api/export.csv`, 507) | 8,212 / 8,690 | 183 |
| new-to-network | 8,043 / 9,241 | 5,902 / 2 |
| market report (`/api/report/market`) | 7,299 / 5,134 | 2,500 / 221 |
| market sizing (`/api/market/sizing`) | 7,018 / 8,259 | 783 / 717 |
| header counts (`/api/stats`) | 6,823 / 8 | 1,025 (8,886 first start) |
| leads CSV after the leads screen | 1,846 | 3 |

Bodies: identical to §1's except the "Generated" minute and the version
string. A final fetch of all 135 bodies on the release code (r13exec
`perf_final`, under background load, so its timings are not quoted) differs
from c8b80fc's in 26 bodies, every one explained: the new response fields
(`/api/boot`, `/api/stats`, `/api/files`, `/api/payers`, `/api/months`,
`/api/urls`: `analytics`, `covered`, `reread`, `backup`, identity notes), the
version string in "Generated … v0.7.7" lines (CSV/ZIP members of the same
size), the free-space figure in the whole-store export refusal, the 4.1
quartile sentence (rate card, visit value, fee CSV), the 4.2 rule sentence
(pitch, negotiation), and the 6.2 lines ("Not included, and why — Medicaid
floor: … Hospital parity: …" in the negotiation one-pager, payer comparison,
proposal and dossier). `perf/spot99.py` on those bodies: 330 checks, 0
problems, its output identical to the same script on c8b80fc's bodies;
`spot99_entity.py`: 637 drawer rows, 0 mismatches. (spot99.py's own
newest-month entity loop prints "170 mismatches" on BOTH builds' bodies: it
compares the drawer's all-months rows with the newest month only; the
per-month check is spot99_entity.py.)

The analytics build (Group 1.3): 0.7.6 full rebuild 738.3 s, peak RSS
6.36 GB; 0.7.7 sliced build from an empty spine 363.1 s, 6.44 GB, 49 slices,
first payer on screen at 33 s; killed after slice 25 of 49 and restarted: it
resumed at slice 26 and ran 24 slices (186 s + 188 s); both builds
byte-identical to 0.7.6's (375/375 payer x code cells, 64/64 directory
buckets; 79,027,749 spine rows).

Accuracy on a fresh three-file real store (BCBS WY 05_of_09, Moda TX, UHC
HealthScope) with `perf/check_real.py`: TX 734 checks and WY 202 checks, 0
differences; the visit-value "All payers" row now reads p26 · Q2 (was Q3,
item 4.1); entity grain 2/2.
