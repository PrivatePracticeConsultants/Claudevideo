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
