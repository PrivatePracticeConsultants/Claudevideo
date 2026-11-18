# Tested payer sources — live audit, 2026-11-05

Every entry in `config/known_sources.yaml` was re-checked live with the app's
own code (download → classify → expand index / lift page links / headless
render), the November dated URLs resolved, and 3 listed files per source
sampled. Replacement addresses came from web research and were re-tested the
same way before going in. Scripts: scratchpad `sources/probe.py`,
`fresh.py`, `verify_new.py` (not shipped).

**Result: 69 entries — 37 auto-queueable (all resolve), 32 open-in-browser.**

## What was wrong, and the fix

| Finding | Fix |
|---|---|
| 8 Highmark-hosted "indexes" named for Florida Blue, BCBS Nebraska, Arizona, Idaho, Minnesota, Louisiana, Independence National Alliance and Highmark PA were **one byte-identical file** (same SHA-256): Highmark BCBS PA's own index (184 plans, all "Highmark, Inc."). Queueing "Florida Blue" fetched Highmark PA's rates. | Kept Highmark BCBS PA; removed the 7 copies. Added each plan's own source where one exists: Blue Cross of Idaho hub (auto, 420 files), Florida Blue, BCBS Nebraska, BCBS Arizona (portals; Arizona unconfirmed). Louisiana, Minnesota, IBX already had own entries; their notes no longer claim the Highmark copy is their book. |
| 14 portal links dead (404 or a "404" page): Wellmark, Horizon, Priority Health, BCBS MA, RI, VT, Blue Shield CA, Capital Blue Cross, HAP, Health New England, Univera, ConnectiCare, CommunityCare, Network Health. 4 pointed only at a homepage: SummaCare, Optima/Sentara, UPMC, Viva. | New addresses for all. **Now auto-queueable:** Horizon (Sapphire hub, 1,724 files), Priority Health (18), BCBS Vermont (own index, 529 files, 30/30 sampled live), SummaCare (11 indexes), Sentara (stable monthly wrapper), UPMC (monthly-dated index, 8 files). The rest are browser portals (HealthSparq / script-built pages / reCAPTCHA). |
| Molina's page (19 state indexes in July) now 404s; per-state variants 404 too. | Left as a portal with instructions; new location not confirmed (site refuses some automated requests). |
| Anthem index grew to 11.5 GB (notes said 10.5 GB, "confirm_over_gb >= 12"). | Note says set ≥ 15 for headroom; with the shipped 5 GB the row shows "too big" + "download anyway". |
| First Health index still points at a 2026-06-05 file set. | Noted as stale on the payer's side (June rates). |
| Moda's November index lists 13 files, 5 of which 404. | Noted (payer-side; retry later in the month). |
| Network Health: index is invalid JSON; its in-network ZIP holds 350 nested ZIPs. | Portal with manual instructions (the app doesn't open nested ZIPs). |

## Could not verify from the test machine (dates left at July)

- **BCBS South Carolina** — HTTP 403 to automated requests (worked in July).
- **Western Health Advantage** — bot-check (HTTP 429).
- **BCBS Massachusetts** — new address found, but its TLS certificate chain did
  not validate here; the app never skips certificate checks. (Dated 2026-11-05
  for the new address, marked UNVERIFIED in its note.)

## Freshness (dates in the listed file names)

Highmark family, Wyoming, ND, Delaware, Michigan, KC, Louisiana, Tufts,
Security Health, Horizon, Idaho, Priority, UPMC, Sentara, SummaCare: 2026-11.
Mississippi, Centene, SelectHealth: 2026-10. CareFirst: mostly 2026-09.
First Health: 2026-06 (stale). UnitedHealthcare: 76,234 files listed for
2026-11 (7,476 in-network).

## Additions, same day (2026-11-05, second pass)

Five research passes (Northeast, Southeast, Midwest, South-central & West,
national TPAs / networks / hosting vendors) proposed ~110 candidates. Each
was run through the same live check (`verify_candidates.py`) and a second
check that the app can READ what the source lists (`ext_check.py`: file types
of the lifted links, and for a `.zip` its member names read with HTTP range
requests). Only sources that passed both went in as auto-queueable.

**Result: 145 entries — 104 auto-queueable (was 37), 41 open-in-browser.**

- **New auto-queueable (58):** Healthfirst, Fallon, NHPRI, Mass General
  Brigham HP, CDPHP, Geisinger, AmeriHealth NJ ×2 / PA, Independent Health,
  MVP; AvMed, Health First, Alliant, AmeriHealth Caritas Next, First Choice
  Next, The Health Plan, MedCost, Health Advantage ×3; HealthPartners, Medical
  Mutual of Ohio, Paramount, Quartz, Aspirus, McLaren ×2, Sanford, Avera,
  AultCare, Ohio Health Choice, PHP of N. Indiana, GHC-SCW, WPS; PacificSource,
  Mountain Health CO-OP, UHA, Sutter, U of Utah, Hometown Health, CHPW, Sharp,
  Baylor Scott & White, HMAA, Presbyterian; Meritain, Allied Benefit Systems,
  HealthComp/Personify, ACS, Zenith American, QuikTrip, Healthcare Highways,
  Luminare (Valenz), MultiPlan/PHCS (SISCO mirror), Curative, Gravie,
  Preferred Health Professionals.
- **Existing portals made auto-queueable (9):** BCBS Arizona, Excellus,
  BCBS Nebraska, Health New England, Capital Blue Cross, Wellmark, Univera,
  Blue Shield of CA, Premera — via HealthSparq's undated
  `mrf.healthsparq.com/<tenant>-egress.nophi.kyruushsq.com/prd/mrf/<INSURER_I>/<BRAND>/latest_metadata.json`,
  which lists current files directly (no browser needed).
- **New open-in-browser (18), each with the reason:** CareSource (ZIPs nested
  in ZIPs), MercyCare (.zst), UCare and 1199SEIU (invalid index JSON), Johns
  Hopkins EHP (month folders), GlobalHealth (403 to automated downloads),
  Community Health Options, Collective Health, L.A. Care, WebTPA, Imagine360,
  Providence, Sendero, Community Health Choice (links not liftable), Medica
  (feed stale since 2026-03), and UNVERIFIED from the test network: Jefferson
  (502), WellSense (port 8443), Midlands Choice (incomplete TLS chain).
- **Rejected:** MetroPlus (newest files 2024-02), Prominence (2026-04),
  Denver Health (stale since 2024), Healthgram regional networks (2026-02),
  Group Health Cooperative of Eau Claire (2024-06), Valenz/HealthSmart (2022)
  and HPI (dead links), MagnaCare (dead links), Sidecar (allowed amounts only),
  QualChoice / Fidelis / Health Net (already inside the Centene source).

**Full re-audit of all 145 entries after the additions:** 100 of 104
auto-queueable resolve to live, readable files; the other 4 behave exactly as
their notes say (Anthem asks before its 11.5 GB index; BCBS SC refuses this
test machine; Moda's own dead files; Avera's file host fails this tester's
quick HEAD but the app's downloader fetched its file). 37 of 41 portals load;
the other 4 are the ones marked UNVERIFIED / bot-walled.

App limits found along the way (not fixed here): no `.7z` or `.zst`
decompression, no ZIP-inside-ZIP, strict JSON for indexes, and a 2,000-file
cap per index (`max_toc_files`) that hides most of the big TPA hubs unless
raised.
