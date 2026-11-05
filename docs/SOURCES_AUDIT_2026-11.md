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
