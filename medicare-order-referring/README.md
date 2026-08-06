# Medicare Order & Referring Tracker

A PowerShell desktop tool that downloads, keeps current, and lets you work with
the CMS **[Order and Referring](https://data.cms.gov/provider-characteristics/medicare-provider-supplier-enrollment/order-and-referring)**
public dataset — the roster of every provider currently eligible to order and
refer for Medicare beneficiaries (~2 million providers, refreshed by CMS
roughly **twice a week**).

> **Disclaimer.** This is an independent tool. It is **not affiliated with,
> endorsed by, or sponsored by CMS, any government agency, or CareSet
> Systems**. It works with **public** CMS data (provider NPIs and
> enrollment/eligibility — no patient data), the public NPPES registry, and —
> optionally — **DocGraph Hop Teaming** data the user licenses from CareSet
> Systems, provided **as-is with no warranty**. The free shared-patient
> (referral) data is CMS's newest public release (**2015**); imported CareSet
> years run through **2022**. Either way, treat it as market structure, not
> current volumes. CareSet's research releases carry a **CC BY-NC-SA 4.0
> non-commercial license** — commercial use requires a commercial license from
> CareSet. Always verify anything used for a billing, compliance, or
> contracting decision against the official source. Every export includes a
> `.methodology.txt` sidecar naming the exact data release it came from.

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

1. If you got this as a ZIP, **right-click it → Extract All…** first, then open
   the extracted folder. The tool will **not** run from inside the zip preview.
2. Copy this folder anywhere (e.g. `C:\OrderReferringTracker`).
3. Double-click **`Start Order and Referring Tracker.cmd`**.
4. Click **Check for updates** — the current CMS file (~70 MB) downloads and
   loads. The status bar shows the release date and provider count.

That's it. No installation; works with the PowerShell built into Windows
(and with PowerShell 7 if you have it).

### The nine tabs

| Tab | What it does |
| --- | --- |
| **Search providers** | Find providers by name and/or NPI prefix; optionally require eligibility flags (e.g. only Part B–eligible). Export results. |
| **Batch NPI check** | Paste any text containing NPIs, or load a `.txt`/`.csv` file — every 10-digit NPI is extracted, checksum-validated, and checked against the CMS list. Statuses: `ELIGIBLE (on CMS list)`, `NOT ON LIST`, `INVALID NPI`. Export results. |
| **What changed** | Compare any two downloaded snapshots: providers Added / Removed / Changed (eligibility flag flips) / Renamed (name change only, flags unchanged — the old name is shown in an OldName column). Export results. |
| **Referral map** | Enter a ZIP — with an optional **radius from 5 to 100 miles in 5-mile steps** that sweeps every ZIP whose Census centroid falls inside the circle and adds a DistanceMiles column — or a prefix like `630*`: every outpatient rehab provider there — including practices **registered elsewhere but treating locally** via an NPPES secondary practice location, practices whose **legal or DBA name says therapy** even though they registered no therapy taxonomy (both marked in a `Presence` column), and a `RosterSize` column showing each organization's Care Compare clinician count — is ranked by how many Medicare patients each source provider fed into them, per the active shared-patient dataset — the free CMS 2015 release, or any imported CareSet Hop Teaming year (2016–2022). The tab title shows the active year; an **Active data** dropdown switches instantly between everything on disk. Select a provider to see their referral sources by name/specialty/volume. Export both tables. |
| **Practice benchmark** | Search a practice by name (or NPI) in NPPES, then benchmark it against every outpatient rehab provider in its ZIP (or 3-digit region) on the active dataset: rank, inbound volume, share of the region's measured referral volume, a ranked competitor table with the practice marked, and **missed sources** — providers feeding competitors with no measured flow into the practice. Export both tables. |
| **Practice groups** | Enter a ZIP — or a **radius up to 100 miles in 5-mile steps** (wide sweeps use the local NPPES index from the Multi-site chains tab and run in seconds; each group row also lists its local members' **NPI numbers**, paste-ready for a combined Source analysis): the outpatient-rehab **practice groups** operating there, each with its therapist roster, ranked by local presence. Built from the CMS clinic-group reassignment file — **current** data, and it fills the gap where private-practice clinics were invisible in the referral map. The **referral benchmark** turns it into a leaderboard: groups ranked by their local therapists' rolled-up referral volume with market share, plus per-group views of every feeding source (with a members-fed depth signal), **group-level missed sources** (feeding competitors, not this group), **outbound destinations** (where the group sends patients onward), and a **source specialty mix**. A **Group trend** button rolls the selected group across every imported CareSet year. Export any view. |
| **Multi-site chains** | Carries the app's **local-data setup**: one button downloads and indexes the Care Compare clinician file and the Medicare enrollment file, and the monthly NPPES registry zip (no stable URL, so it cannot be auto-fetched) is **auto-discovered** from drive roots and Downloads and offered for one-click import, with a file-picker as the fallback — together these power the chain asterisks, multi-site detection, by-address breakdowns, offline discovery, and the market-capture layer, with a status line showing what's present. Type a chain's name (Ivy Rehab, ATI, Select, Athletico…) and break it into its parts two ways. **By NPI**: every organization NPI trading under that name, with its registered address, measured referral volume, distinct source count, and a `MultiSiteNPI` flag for the ones whose volume covers several clinics — plus a volume-by-state roll-up. **By ADDRESS**: per-*location* referral figures, reconstructed from the Care Compare roster — the clinicians listed at each street address bill under their own NPIs, so their measured volume sums to a real per-clinic number, with `Clinicians` / `CliniciansWithVolume` showing how much of each site's roster is actually visible, and `CliniciansAtOtherSites` / `OverlapPatients` quantifying the clinicians who work at more than one site. Volume billed under the organization's own NPI has no service address and is reported separately, never spread across the sites. Optional state and ZIP filters isolate a single location. Export either view with a methodology sidecar. |
| **Provider lookup** | The **Full report (one-stop)** button turns any NPI (or several combined) into a single self-contained HTML document: complete source analysis with metrics, embedded heat map with market-capture layers, automatic year-over-year trendlines when 2+ CareSet years are imported, Medicare eligibility, practice groups, outbound destinations, competitive landscape, findings, methodology, and CSV sidecars. Enter any NPI for a single-provider profile that composes every dataset: current eligibility + flags, specialty and location (NPPES), practice-group memberships, and referral activity from the active dataset — both who shared patients *into* them and who they shared patients *onward to*. With 2+ imported CareSet years, a **Referral trend** button builds a year-over-year table (inbound/outbound volume and top sources per year). A **Source analysis** button builds a client-ready, self-contained HTML report on the NPI's referral base — concentration metrics (top-1/5/10 dependence, HHI), specialty-mix donut, Pareto concentration curve, distance and referral-lag profiles, an embedded **referral-geography heat map** with two views — referral volume, and a **market-capture** view showing each ZIP's total area therapy volume and your share of it (red = open market, green = you own it), plus competitor-location and distance-ring overlays and click-through to the named providers in each ZIP, ranked source table, auto-written findings, and a **competitive landscape** — every outpatient PT/OT/speech provider within 10 miles (incl. subspecialty therapists, CORFs, and speech clinics; assistants and physicians excluded) ranked by inbound volume, with the practice's rank, share of area volume, top competitors, and a top-15 peer table. Paste **several NPIs at once** (org + therapists) to analyze them as one combined practice — the right move for smaller practices whose volume is split across NPIs. Tick **Include year-over-year** to add a performance section across every imported CareSet year: volume and source-count columns per year, source **retention** (kept / new / lost), a per-year metrics table, and named **biggest gains / declines** between the first and last year, with by-year and movers CSVs. A **Referral heat map** button aggregates ALL of an NPI's inbound pairs by each source's practice ZIP (US Census centroids, bundled) and writes an interactive HTML map — circles sized/colored by referral density, distance-from-practice in the table — plus a density CSV with methodology. Export everything. |
| **Watchlist** | Save your referring providers' NPIs once; after each bi-weekly CMS update, one click shows — for just your referrers — their current eligibility and what changed since the previous update (dropped, flags flipped, renamed). Ongoing monitoring instead of a one-time check. Export the report. |

The **Referral map** tab also has an **Export specialty mix** button: it breaks the
displayed referral sources down by specialty (e.g. "45% orthopedic surgery, 20%
primary care") — select a clinic first to profile just that clinic, or leave it
unselected for the whole ZIP.

## The Referral map tab: what it is and its limits

The referral map runs on either of two shared-patient datasets:

- **CMS Physician Shared Patient Patterns** (FOIA release) — the only *free
  public* provider-pair data; newest release covers **January–September 2015**.
  One-time ~356 MB download (~1.7 GB on disk).
- **DocGraph Hop Teaming** (CareSet Systems) — the same kind of data rebuilt
  annually by CareSet from 100% of Medicare FFS Part A+B claims, with releases
  through **2022**. The user obtains the delivery zip from CareSet and imports
  it with **Import CareSet file…** (or `Import-RmDataset`); each year needs
  **7–11 GB on disk**. Verified against the real 2016–2022 deliveries: all
  seven years share one CSV layout, and 2016 + 2022 were imported end-to-end
  (140.9M and 210.3M pairs respectively).

Either way this maps the *structure* of a referral market (who the high-volume
feeders are, which providers they feed) for the data year, **not current
volumes**. The **Active data** dropdown switches between everything on disk
instantly; the tab title, vintage flags, summaries, and every export's
methodology sidecar follow the active dataset. On CareSet data the sources
table gains **AvgDayWait** (mean days between the two visits — a short wait
looks like a referral, a months-long wait like co-occurring care) and drops the
CMS-only SameDay column. With two or more imported years, the Provider lookup
tab can build a **year-over-year referral trend** for any NPI (Hop Teaming
years only — the 2015 CMS file uses a different window and is deliberately
excluded from trends so the comparison stays honest; note that Medicare
Advantage growth also pulls patients out of FFS data over time).

How it works: your ZIP is swept against the live NPPES registry for PT/rehab
clinic organizations and individual PT/OT/SLP providers; the active pair file
(34.9M rows for CMS 2015, 140–210M rows for the CareSet years) is then
streamed for every pair where one of them saw a Medicare patient *after* the
source provider (within 30 days for the CMS file; CareSet's directed "hop"
method for Hop Teaming). Expect a scan to take a few minutes on the larger
years — the app stays responsive and says what it's doing.

Honest limitations (also written into every export's methodology sidecar):

- **Vintage.** Providers whose NPI was issued after the active data year are
  flagged (`ExistedInDataYear = No`) so their zeros aren't misread as "no
  referrals".
- **Org-NPI coverage differs by source.** In the CMS 2015 file,
  private-practice organization NPIs rarely appear (verified: 130 pre-2015
  PT-chain org NPIs matched 0 rows), so private clinics show up through their
  individual therapists. In the CareSet years, org NPIs — including
  private-practice LLCs — do appear (verified on real 2022 data), but a
  practice's volume can be SPLIT between its org NPI and its therapists'
  individual NPIs.
- **Shared-patient ≠ referral.** Labs, imaging, and hospitals appear as
  "sources" simply from co-occurring care. Interpret by specialty: an
  orthopedic surgeon feeding a PT is referral-like; a lab is not.
- **Pairs under 11 patients are excluded by CMS**, so low-volume referrers are
  invisible. (For 2015 that threshold is over an ~8-month window, not a year.)
- **SharedPatients on the clinics table is referral *volume*, not a headcount.**
  It sums each source's shared-patient count, so a patient sent by three sources
  is counted three times. Use it to rank/compare, not as "distinct patients."
- **SameDay is a partial signal.** CMS attributes same-day pairs to the lower
  NPI; this tool only captures rows where the clinic is the second provider, so
  same-day activity where the clinic holds the lower NPI is not counted.

For *current* referral flows, pair this with your own EMR/claims referral data
or a commercial license (CareSet, Trella Health, etc.) — see the discussion in
the repo history.

## The Practice groups tab: the current-data companion

Where the referral map is historical (2015–2022 depending on the active
dataset), this tab is **current** and organizes providers by their practice group. It uses
the CMS *Revalidation Clinic Group Practice Reassignment* file (updated ~monthly,
~510 MB one-time download), which records which individual therapists reassign
their Medicare benefits to which group practice. Joined with the live NPPES
registry, that answers: *which outpatient-rehab practices operate in this ZIP,
and who's on their therapist roster right now?* — a clean competitive-landscape
map with no vintage caveat.

Honest limits (also in every export sidecar):

- A "group" is a practice with a legal business name and 2+ therapist members.
  Solo/private-practice therapists reassign to themselves (blank business name)
  and are counted separately, not shown as groups.
- **RosterSize is nationwide.** A large roster with only a few in-ZIP members is
  a multi-site organization (e.g. a national rehab company with one local
  therapist), not a big local clinic — rank by `TherapistsInZip` for local size.
- This is enrollment/affiliation data, **not** referrals or claims. It shows who
  practices together, not who refers to whom.

### The bridge: the group referral benchmark

The **Add … referral benchmark** button (labeled with the active year) ties the
two ZIP tools together. It takes each practice group's *local* (in-ZIP)
therapists, rolls their inbound shared-patient volume up to the group, and
turns the groups table into a leaderboard: `Rank`, `LocalReferrals<year>`, and
`SharePct` of the groups' measured volume — so a private practice that never
appeared as an organization in the shared-patient file gets a referral
footprint through its therapists, and the region's top-receiving groups are
one click away. Per selected group, the **Show** dropdown flips the lower
table between the therapist roster, the full list of feeding sources (with
`MembersFed` — how many of the group's therapists that source feeds), and the
group's **missed sources** (feeding other groups, not this one). Every view
exports with a methodology sidecar. (Needs a Referral map dataset; the active
year's vintage and shared-patient caveats apply, and a group's organization
NPI can hold additional volume — benchmark it separately.)

## The Provider lookup tab: one NPI, every dataset

Enter any NPI and the app assembles a single profile from all four sources:

- **Order & Referring** — current eligibility and the Part B/DME/HHA/PMD/Hospice
  flags (from the roster loaded on the Search tab).
- **NPPES** — name, primary specialty, and practice city/state (live).
- **Practice groups** — which group(s) the NPI reassigns benefits to (if that
  dataset is downloaded).
- **Referral map (active dataset)** — inbound (who shared patients into them) and outbound
  (who they shared patients onward to), each ranked and name/specialty-enriched
  (if that dataset is downloaded).

Whatever optional datasets you've downloaded are folded in; the rest are noted
as unavailable. The inbound and outbound lists export with a methodology
sidecar. It's the fastest way to answer "tell me everything we know about this
provider" — a referrer, a competitor, or a prospect.

## Staying current automatically

CMS publishes on a ~3.5-day cycle. Two ways to stay current:

- The app checks whenever you click **Check for updates** (downloads only when
  CMS actually has a newer release — otherwise it's a no-op).
- Or register a daily background check (downloads only on real releases):

```powershell
Import-Module .\OrderReferring
Install-OrfUpdateTask -At 07:00   # Windows Scheduled Task; remove with Uninstall-OrfUpdateTask
```

If this returns **"Access is denied"**, run PowerShell as administrator
(right-click **Windows PowerShell** → **Run as administrator**) and retry —
registering a scheduled task can require elevation.

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

Import-Module .\ReferralMap
Import-RmDataset -Path .\DocGraph_2022_NonCommercial.zip   # add a CareSet year
Get-RmAvailableDatasets                     # everything on disk, active marked
Set-RmActiveDataset -Source hop-teaming -Year 2021         # instant switch
Get-RmReferralMap -Zip 85351                # map a ZIP on the active dataset
Get-RmProviderTrend -Npi 1234567893         # year-over-year (2+ imported years)

# Multi-site chains
Import-RmCareCompare -Path .\DAC_NationalDownloadableFile.csv   # one-time roster index
Get-RmProviderFamily -Name 'IVYREHAB'       # every org NPI in the chain
Get-RmLocationReferrals -Name 'IVYREHAB'    # per-STREET-ADDRESS referral volume
Get-RmLocationReferrals -Name 'IVYREHAB' -State NJ -Zip 07030   # one location
```

Data lives in `%LOCALAPPDATA%\OrderReferringTracker` (override with the
`ORF_DATA_DIR` environment variable). Downloads are atomic: a failed or
interrupted download can never corrupt the data you already have — the new
file is fully downloaded and structurally validated before it replaces
anything, and a validation failure keeps your existing snapshot untouched.

## Long jobs show their work

Scanning a multi-gigabyte shared-patient file takes minutes, and a window that
sits still for minutes reads as crashed. Every background job therefore dims
the app behind a working panel that names the running step, animates a
progress bar, and counts elapsed time, so "still working" is never confused
with "hung". It waits ~600 ms before appearing, so quick actions don't flash
it, and it is torn down unconditionally when a job ends — a job that fails
can never strand the app behind it.

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

The test suite (244 tests across three modules: download/update/validation/
idempotency, crash-recovery state repair, older-release and retention safety,
schema-drift tolerance, CSV formula-injection neutralization, download-URL and
zip-slip rejection, search, batch check, snapshot diff/rename detection,
referral-map discovery/paging/scan/enrichment/vintage-flags, CareSet Hop
Teaming import/format-detection/rejection, dataset switching, source-aware
vintage windows and methodology notes, multi-year trend, group referral
footprint, provider-360 inbound/outbound activity, practice-group Latin-1
loading/matching/roster/membership/export, chain breakdown by organization
NPI and by street address, exports) runs against local HTTP
test doubles of the CMS hosts and the NPPES API — no external traffic:

```powershell
Invoke-Pester .\tests -Output Detailed     # requires the Pester module
```

Verified end-to-end against the real CMS releases of 2026-07-14 and
2026-07-17 (2,011,708 → 2,014,209 providers; 2,662 added / 161 removed /
696 changed): full two-release download + diff in ~27 s, snapshot load ~3 s,
searches instant. The CareSet path was verified against the real 2016–2022
deliveries: every year's format validated, 2016 (140.9M pairs) and 2021/2022
(206.6M / 210.3M pairs) fully imported; a live ZIP map on 2022 data returned
46 clinics / 856 source relationships in ~6 minutes, and a two-year trend
(2021→2022) for a real clinic ran in ~3 minutes with the map and trend
agreeing on identical totals.
