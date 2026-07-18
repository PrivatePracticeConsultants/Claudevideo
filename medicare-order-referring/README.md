# Medicare Order & Referring Tracker

A PowerShell desktop tool that downloads, keeps current, and lets you work with
the CMS **[Order and Referring](https://data.cms.gov/provider-characteristics/medicare-provider-supplier-enrollment/order-and-referring)**
public dataset — the roster of every provider currently eligible to order and
refer for Medicare beneficiaries (~2 million providers, refreshed by CMS
roughly **twice a week**).

## Read this first: what this data can and cannot tell you

The Order & Referring file is an **eligibility roster**. For each NPI it lists
the provider's name and five Y/N flags: eligible to order/refer for **Part B**
(which covers outpatient PT/OT/SLP therapy), **DME**, **Home Health**, **PMD**,
and **Hospice**. That is all it contains.

It has **no claims and no referral relationships**. No public CMS dataset
currently links individual referring physicians to the specific outpatient
rehab providers they refer to — the last public shared-patient data ended in
2015, and current referral-pair data is only available commercially or from
your own records. So this tool cannot — and honestly, no tool built on this
dataset can — show "Dr. X refers to Clinic Y."

What it *can* do, accurately:

- **Verify your own referral sources.** Export the referring-provider NPIs
  from your EMR/billing system, run the **Batch NPI check**, and instantly see
  which of your referrers are (still) Medicare-eligible to order/refer — and
  with which flags. A referrer who drops off this list is a claims-denial risk
  for Medicare therapy episodes.
- **Look up any provider** by name or NPI and see their current eligibility.
- **Track changes over time.** Every time CMS publishes (about twice a week),
  the tool keeps the new snapshot and writes a change log: who was added,
  removed, or had flags flip. Great for spotting a referrer losing eligibility
  before claims start bouncing.
- **Export everything** to CSV — every export ships with a `.methodology.txt`
  sidecar recording the exact CMS release date, row count, and file hash the
  numbers came from.

## Quick start (Windows)

1. Copy this folder anywhere (e.g. `C:\OrderReferringTracker`).
2. Double-click **`Start Order and Referring Tracker.cmd`**.
3. Click **Check for updates** — the current CMS file (~70 MB) downloads and
   loads. The status bar shows the release date and provider count.

That's it. No installation; works with the PowerShell built into Windows
(and with PowerShell 7 if you have it).

### The three tabs

| Tab | What it does |
| --- | --- |
| **Search providers** | Find providers by name and/or NPI prefix; optionally require eligibility flags (e.g. only Part B–eligible). Export results. |
| **Batch NPI check** | Paste any text containing NPIs, or load a `.txt`/`.csv` file — every 10-digit NPI is extracted, checksum-validated, and checked against the CMS list. Statuses: `ELIGIBLE (on CMS list)`, `NOT ON LIST`, `INVALID NPI`. Export results. |
| **What changed** | Compare any two downloaded snapshots: providers Added / Removed / Changed (flag flips). Export results. |

## Staying current automatically

CMS publishes on a ~3.5-day cycle. Two ways to stay current:

- The app checks whenever you click **Check for updates** (downloads only when
  CMS actually has a newer release — otherwise it's a no-op).
- Or register a daily background check (downloads only on real releases):

```powershell
Import-Module .\OrderReferring
Install-OrfUpdateTask -At 07:00   # Windows Scheduled Task; remove with Uninstall-OrfUpdateTask
```

Old snapshots are kept (default: the 8 most recent, ~70 MB each) so the
change-tracking always has history; adjust with `Set-OrfConfig -KeepSnapshots N`.

## Command-line use (optional)

Everything the app does is also scriptable:

```powershell
Import-Module .\OrderReferring

Update-OrfData                              # download if CMS has a newer release
Get-OrfStatus -CheckOnline                  # local release vs. CMS release
Search-OrfProvider -Name 'smith john' -RequireFlag PARTB
Test-OrfNpi -Path .\my-referrers.csv        # batch-verify your referral list
Compare-OrfSnapshot                         # diff the two newest snapshots
Search-OrfProvider -RequireFlag HHA | Export-OrfResult -Path hha-eligible.csv
```

Data lives in `%LOCALAPPDATA%\OrderReferringTracker` (override with the
`ORF_DATA_DIR` environment variable). Downloads are atomic: a failed or
interrupted download can never corrupt the data you already have — the new
file is fully downloaded and structurally validated before it replaces
anything, and a validation failure keeps your existing snapshot untouched.

## Accuracy guarantees

- The file is discovered live from the official CMS catalog
  (`https://data.cms.gov/data.json`) — no hard-coded file URLs to go stale.
- Every download is validated against the expected schema
  (`NPI, LAST_NAME, FIRST_NAME, PARTB, DME, HHA, PMD, HOSPICE`); if CMS ever
  changes the layout the tool refuses the file with a clear message instead of
  producing wrong numbers.
- NPIs are validated with the real NPI check-digit algorithm (Luhn with the
  80840 prefix), so typos in your referral lists are flagged as `INVALID NPI`
  rather than being reported as "not enrolled".
- Exports always carry a methodology sidecar naming the exact CMS release,
  row count, and SHA-256 of the source file.

## Tests

The test suite (18 tests: download/update/validation/idempotency, search,
batch check, snapshot diff, exports, retention) runs against a local HTTP
server with fixture files — no CMS traffic:

```powershell
Invoke-Pester .\tests -Output Detailed     # requires the Pester module
```

Verified end-to-end against the real CMS releases of 2026-07-14 and
2026-07-17 (2,011,708 → 2,014,209 providers; 2,662 added / 161 removed /
696 changed): full two-release download + diff in ~27 s, snapshot load ~3 s,
searches instant.
