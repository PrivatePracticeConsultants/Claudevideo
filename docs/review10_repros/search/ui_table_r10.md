# Round 10 search timings — 99M-rate synthetic store, Chromium (ui_search.py), quiet

ms from the first key/click to the last result on screen (ms after the last key in brackets). 'err' = refused or superseded requests the user waited behind (0.7.3: 'Stats are busy'). Cold = the store's files evicted from the page cache (evict.py) and the server started fresh. 0.7.3 = 2bb81b8, 0.7.4 = this build. scale_warm2 is the review's own warm 0.7.3 run on the same store.

| scenario | ab_073_cold | ab_074_cold | scale_warm2 | r10_warm |
|---|---|---|---|---|
| page load (Explorer default) | 11307 ms (11307); 11 req | 2438 ms (2438); 12 req | 2165 ms (2165); 11 req | 1133 ms (1133); 12 req |
| type full name @120ms/key | 5987 ms (4509); 4 req | 3769 ms (2276); 2 req | 6133 ms (4661); 2 req | 3892 ms (2377); 2 req |
| clear search box | 1692 ms (1684); 2 req | 1145 ms (1138); 2 req | 1688 ms (1681); 2 req | 1098 ms (1092); 2 req |
| type partial name @120ms/key | 6459 ms (5233); 2 req | 3795 ms (2560); 2 req | 6742 ms (5517); 4 req | 3775 ms (2547); 2 req |
| clear search box (2) | 1734 ms (1728); 4 req | 1120 ms (1114); 4 req | 1754 ms (1731); 2 req | 1111 ms (1105); 4 req |
| type partial name @400ms/key (slow typist) | 45169 ms (41105); 28 req, 12 err | 5586 ms (1501); 18 req | 26575 ms (22483); 28 req, 15 err | 5713 ms (1638); 18 req |
| clear search box (3) | 1752 ms (1747); 4 req | 1062 ms (1057); 2 req | 1797 ms (1791); 4 req | 1114 ms (1108); 2 req |
| type 9-digit TIN @120ms/key | 1774 ms (791); 2 req | 1525 ms (538); 2 req | 1812 ms (826); 2 req | 1473 ms (489); 2 req |
| clear search box (4) | 1684 ms (1677); 2 req | 1085 ms (1078); 4 req | 1671 ms (1666); 2 req | 1100 ms (1094); 2 req |
| type 9-digit TIN @400ms/key (slow typist) | 10369 ms (7106); 20 req, 12 err | 3640 ms (374); 18 req | 21147 ms (17894); 22 req, 8 err | 3642 ms (388); 20 req |
| click first row -> drawer | 392 ms (389); 1 req | 342 ms (340); 1 req | 434 ms (431); 1 req | 372 ms (370); 1 req |
| clear search box (5) | 1800 ms (1796); 4 req | 1268 ms (1260); 2 req | 1778 ms (1772); 2 req | 1249 ms (1243); 2 req |
| type 10-digit NPI @120ms/key (TIN grain) | 1822 ms (714); 2 req | 1578 ms (471); 2 req | 1820 ms (713); 2 req | 1557 ms (451); 2 req |
| switch grain -> NPI (NPI in box) | 2048 ms (2047); 2 req | 488 ms (487); 2 req | 527 ms (526); 4 req | 461 ms (460); 2 req |
| switch grain -> Entity (NPI in box) | 289 ms (289); 2 req, 2 err | 288 ms (288); 2 req, 2 err | 291 ms (291); 2 req, 2 err | 288 ms (288); 2 req, 2 err |
| switch grain -> TIN (NPI in box) | 325 ms (325); 2 req | 347 ms (347); 4 req | 336 ms (336); 2 req | 373 ms (373); 2 req |
| clear search box (6) | 1675 ms (1668); 2 req | 1081 ms (1075); 2 req | 1735 ms (1728); 2 req | 1321 ms (1316); 4 req |
| click CPT chip 97110 | 695 ms (693); 2 req | 431 ms (430); 2 req | 434 ms (432); 2 req | 424 ms (423); 2 req |
| CPT on: type partial name @120ms/key | 2634 ms (1400); 4 req | 2368 ms (1135); 2 req | 2752 ms (1509); 2 req | 2303 ms (1079); 2 req |
| CPT on + name: grain -> Entity | 290 ms (289); 2 req, 2 err | 294 ms (292); 2 req, 2 err | 290 ms (289); 2 req, 2 err | 296 ms (294); 2 req, 2 err |
| CPT on + name: grain -> NPI | 289 ms (289); 2 req, 2 err | 288 ms (288); 2 req, 2 err | 288 ms (288); 2 req, 2 err | 289 ms (289); 2 req, 2 err |
| CPT on + name: grain -> TIN | 598 ms (598); 2 req | 574 ms (574); 2 req | 640 ms (640); 4 req | 609 ms (609); 2 req |
| clear search box (7) | 718 ms (712); 2 req | 411 ms (405); 2 req | 723 ms (716); 2 req | 418 ms (412); 2 req |
| sort by Entity name (CPT on) | 334 ms (333); 1 req | 266 ms (264); 1 req | 394 ms (393); 1 req | 274 ms (273); 1 req |
| next page (CPT on) | 323 ms (323); 1 req | 255 ms (255); 3 req | 374 ms (374); 1 req | 289 ms (289); 3 req |
| page size 250 (CPT on) | 343 ms (343); 1 req | 288 ms (288); 1 req | 401 ms (401); 1 req | 278 ms (278); 1 req |
| Clear all filters | 5093 ms (5093); 4 req | 2690 ms (2690); 2 req | 5065 ms (5065); 2 req | 2716 ms (2716); 2 req |
| type state code @120ms/key | 6793 ms (6654); 2 req | 3418 ms (3278); 2 req | 7276 ms (7140); 4 req | 3402 ms (3264); 2 req |
| Clear all filters (2) | 4188 ms (4186); 4 req | 2890 ms (2887); 2 req | 5017 ms (5015); 2 req | 3050 ms (3048); 2 req |
| open Code comparison (auto first code) | 102 ms (102); 2 req | 108 ms (108); 4 req | 122 ms (122); 2 req | 124 ms (124); 4 req |
| Code page: click 97110 | 808 ms (808); 2 req | 1056 ms (1056); 2 req | 1020 ms (1020); 4 req | 1123 ms (1123); 2 req |
| Code page: click 97530 | 870 ms (870); 2 req | 1053 ms (1053); 2 req | 1128 ms (1128); 2 req | 1057 ms (1057); 2 req |
| open Benchmark tab | 40 ms (40); 6 req | 41 ms (41); 6 req | 48 ms (48); 6 req | 44 ms (44); 6 req |
| Benchmark: type subject name @120ms/key | 1369 ms (236); 1 req | 1374 ms (247); 1 req | 1375 ms (233); 1 req | 1388 ms (255); 1 req |
| Benchmark: clear + type partial @400ms/key | 4282 ms (236); 13 req | 4288 ms (243); 13 req | 4294 ms (236); 11 req | 4290 ms (246); 11 req |
| Benchmark: Run (first result screen) | 676 ms (612); 4 req | 678 ms (614); 4 req | 336 ms (274); 6 req | 318 ms (257); 6 req |
| Benchmark: Run again | 217 ms (217); 3 req | 214 ms (214); 3 req | 242 ms (242); 3 req | 214 ms (214); 3 req |

First start of 0.7.4 on a store 0.7.3 built (no practice list, no stored strip, cold cache): page load 6,504 ms (0.7.3 cold: 11,307 ms). One earlier first start, right after a disk-heavy snapshot run, took 60.3 s with /api/stats waiting 57.8 s; it did not reproduce under the same conditions (6.5 s).
