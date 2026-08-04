# How to use the Medicare Order & Referring Tracker — plain-English guide

*No technical knowledge needed. Total setup time: about 10 minutes, most of it
waiting for downloads.*

> **Please note:** this is an independent tool, **not affiliated with CMS or
> CareSet Systems**. It uses public CMS data plus, optionally, DocGraph Hop
> Teaming data licensed from CareSet (no patient information in either), and
> is provided as-is. The free referral data is from 2015; imported CareSet
> years run through 2022. CareSet's standard research license is
> **non-commercial (CC BY-NC-SA 4.0)** — if you use the data in paid
> consulting work, confirm your license terms with CareSet. Double-check
> anything you'll use for a billing or compliance decision against the
> official source.

---

## 1. One-time setup

1. **Extract the ZIP first.** If you received this as a ZIP file, right-click
   the downloaded ZIP, choose **Extract All…**, and then open the extracted
   folder. **The tool will NOT run from inside the zip** — Windows opens zips
   in a read-only preview, and the app can't create its data files there.
2. **Get the folder onto your computer.** You need the whole
   `medicare-order-referring` folder (the one this file is in). Put it
   anywhere you like — your Desktop or `C:\` is fine. Don't move or rename
   the files inside it.
3. That's it. There is nothing to install. The app uses PowerShell, which is
   already part of Windows.

> **If Windows shows a blue "Windows protected your PC" box** the first time
> you run it: click **More info**, then **Run anyway**. That warning appears
> for any program Windows hasn't seen before. If instead you see a smaller
> **"Open File - Security Warning"** box, just click **Run**.

---

## 2. Starting the app

1. Open the `medicare-order-referring` folder.
2. Double-click **`Start Order and Referring Tracker.cmd`**.
3. A window opens titled **Medicare Order & Referring Tracker**. A black text
   window may also appear behind it — that's normal; just ignore it (closing
   it closes the app).

---

## 3. First run: download the data

1. In the top-right of the window, click **Check for updates**.
2. Wait 1–3 minutes. The app downloads the current Medicare
   eligible-to-order/refer list (about 70 MB, roughly 2 million providers).
3. When the top bar shows a date and a provider count (for example
   *"Data release: 2026-07-17 | 2,014,209 providers"*), you're ready.

Click **Check for updates** whenever you want the newest list — Medicare
publishes a new one about twice a week. If nothing new exists, the app just
tells you you're up to date. It never breaks your existing data.

---

## 4. The tabs, in plain English

### Tab 1 — Search providers
*"Is Dr. Smith allowed to refer Medicare patients?"*

- Type a name (and/or an NPI number) and click **Search**.
- Check a box like **Part B** to only show providers eligible for that
  benefit — Part B is the one that covers outpatient therapy.
- The Y/N columns show what each provider may order/refer for:
  Part B, DME (equipment), Home Health, PMD (power wheelchairs), Hospice.
- **Export results…** saves what you see as a spreadsheet (CSV) file.

### Tab 2 — Batch NPI check
*"Are ALL my referring doctors still eligible?" — the most useful tab.*

1. Get a list of your referring providers' NPI numbers from your EMR or
   billing system (any messy format is fine — a column pasted from Excel,
   a whole report, anything).
2. Paste it into the big box (or click **Load from file…**).
3. Click **Run check**. Every 10-digit number found is checked. Each one
   comes back as:
   - **ELIGIBLE (on CMS list)** — good; the Y/N columns show for what.
   - **NOT ON LIST** — this provider is *not* currently eligible to
     order/refer for Medicare. Referrals from them are a claims-denial risk.
   - **INVALID NPI** — the number itself is mistyped (fails the checksum).
4. **Export results…** to save the report.

> Tip: run your full referral list after every data update. A referrer who
> drops off the list is worth a phone call *before* claims start bouncing.

### Tab 3 — What changed
*"Who got added or removed since last time?"*

Every data download is kept as a dated snapshot. Pick two snapshots, click
**Compare**, and see exactly who was **Added**, **Removed**, **Changed**
(eligibility flags flipped), or **Renamed** (name changed but eligibility did
not — the previous name is shown so you can tell it apart from a real
eligibility change). Exportable like everything else.

### Tab 4 — Referral map
*"Who sends patients to the rehab providers in my area?"*

This tab can run on **two kinds of data** — the tab's title shows which year
is active:

- **Free CMS data (2015):** click **Download CMS dataset** (~356 MB one-time).
  The newest data CMS gives away publicly.
- **Newer CareSet data (2016–2022, licensed):** if you have DocGraph Hop
  Teaming files from CareSet Systems, click **Import CareSet file…**, pick the
  downloaded `.zip` (no need to unzip it), and wait a few minutes. Each year
  is large — roughly **7–11 GB of disk space per imported year**.

> **Keep your CareSet zips safe.** The download links CareSet emails are
> personal order links that can expire. Save the original zips on an external
> drive or backup — each zip contains its own checksum file (`.csv.md5`) so
> you can always verify a copy is intact.

**Switching years:** the **Active data** dropdown lists every dataset you've
downloaded or imported. Switching is instant — nothing re-downloads — and the
tab title, footprint button, and all exports follow the active year. Any
results on screen are cleared so numbers from different years can't get mixed
up.

Then:
1. Type a ZIP code and pick a **Radius** (up to 50 miles) — every ZIP whose
   center falls inside the circle gets swept, and the results gain a
   **DistanceMiles** column so you can see how far each provider sits from
   your center ZIP. "Exact ZIP" searches just the one ZIP, and a prefix like
   `630*` still works for whole-prefix areas. Wide radii in metro areas sweep
   dozens of ZIPs, so give those a few extra minutes. Click **Map referral
   sources**.
2. **Top table:** every outpatient rehab provider in that area, ranked by
   referral *volume* (the SharedPatients number adds up each source's patients,
   so treat it as a ranking score, not a count of distinct people).
3. **Click any row** and the bottom table shows *who fed them those
   patients* — names, specialties, cities, and patient counts.
4. Export either table with the buttons.

**Read this before trusting the numbers:**
- Radius distances are straight-line miles between ZIP-area centers (US
  Census), not driving distance; ZIPs that are PO-box-only aren't swept.
- The data shows the *structure* of your referral market **for its data
  year** (who the big referrers are and whom they fed), **not this year's
  volumes**. Every export's methodology file states the exact year and source.
- A provider marked **"No (NPI issued …)"** in the last column didn't exist
  yet in the data year — their zero means "too new," not "no referrals."
- On CareSet data, the **AvgDayWait** column is the average days between the
  source visit and the clinic visit: a few days-to-weeks looks like a real
  referral; several months looks like loosely-related care.
- The CareSet years cover Medicare **fee-for-service only** — Medicare
  Advantage patients (a large share in most markets) are not in the data, so
  volumes understate the total flow.
- Labs and hospitals sometimes appear as "sources" just because patients
  visited them around the same time. Judge sources by specialty: an
  orthopedic surgeon feeding a physical therapist is a real referral
  pattern; a lab is not.
- Pairs sharing fewer than 11 patients in the window are excluded (a CMS
  privacy rule in every year), so small referrers are invisible.

**Tip — Export specialty mix:** the **Export specialty mix** button breaks your
referral sources into a specialty profile ("45% orthopedic surgery, 20% primary
care…"). Select a clinic first to profile just that clinic, or leave it
unselected for the whole ZIP — handy for seeing where a practice's funnel comes
from.

### Tab 5 — Practice benchmark
*"How does this clinic stack up against everyone else around it?"*

Works on whichever dataset is active on the Referral map tab.

1. Type part of a **practice name** (or a therapist's last name, or paste a
   full 10-digit NPI), optionally a 2-letter state, and click **Search NPPES**.
2. **Pick the practice** from the results. One thing to know: solo practices
   usually live under the owner's *Individual* NPI, while clinics and chains
   are *Organization* NPIs — if you see both for the same practice, benchmark
   both (their referral volume is split between them).
3. Click **Benchmark selected**. The app finds every outpatient rehab provider
   in that practice's ZIP (tick **Wider area** for the whole 3-digit ZIP
   region) and scans the referral data. You get:
   - **The bottom-line sentence:** its rank in the region, its inbound
     volume, and its share of the region's measured referral volume.
   - **Region ranking table:** every competitor ranked, with a **>> YOU**
     marker on your practice's row.
   - **Missed sources:** the providers who feed patients to your competitors
     but have *no* measured flow into this practice — sorted by how many
     patients they send elsewhere. For a consultant, this is the outreach
     call list.
4. Export both tables; the methodology file spells out the caveats (the
   biggest: "missed" can also mean the relationship exists but fell under
   the 11-patient privacy floor).

### Tab 6 — Practice groups
*"Which rehab practices operate in my area, and who's on their team — right now?"*

One-time setup: click **Download CMS dataset** (~510 MB; this tab needs no other
data). Then type a ZIP and click **Find practice groups**.

1. **Top table:** every multi-therapist rehab practice group operating in that
   ZIP, ranked by how many of their therapists practice locally
   (`TherapistsInZip`), with the group's total nationwide roster size.
2. **Click any group** to see its full therapist roster below — names,
   specialties, and which ones are in your ZIP.
3. Export either table.

Unlike the referral map, this is **current** data (updated monthly), and it
shows the private-practice clinics the referral map can't. Two things to know:
`RosterSize` counts the group's therapists *nationwide*, so a huge roster with
only one or two local therapists is a multi-site chain, not a big local clinic —
rank by `TherapistsInZip` for local size. And this is *who practices together*,
not *who refers to whom*.

**The group leaderboard — "Add referral benchmark":** if you have a dataset on
the Referral map tab, this button rolls every group's *local* therapists'
referral volume up to the group and turns the top table into a **leaderboard**:
each group gets a `Rank`, a `LocalReferrals<year>` patient count, and a
`SharePct` (its slice of all the groups' measured volume). This directly
answers *"which provider groups receive the most referrals here?"*

Then use the **Show** dropdown to flip the lower table, per selected group:

- **Therapist roster** — who's on the team (the default view).
- **Referral sources** — every provider feeding that group, with patients and
  `MembersFed` (how many of the group's therapists that source feeds — several
  means a deep relationship, not one friendly doctor).
- **Missed sources** — providers feeding *other* groups in the ZIP but not
  this one, ranked by the volume they send elsewhere. Pick your own group and
  this is your outreach list.
- **Sent patients to (outbound)** — where the group's therapists sent
  patients *onward*: the physicians, imaging centers, and hospitals that own
  the post-therapy hand-offs (`MembersSending` = how many therapists send
  there). Destinations inside the same group are internal continuity of
  care, not a referral out.
- **Source specialty mix** — the group's referral funnel by specialty
  ("62% orthopedic surgery, 18% primary care…"), with each specialty's share
  of the group's volume.

**Group trend… :** with a group selected and **two or more** CareSet years
imported, this button builds the group's year-over-year story — inbound
patients, distinct sources, and top feeders per year, rolled up from its
local therapists. Minutes per year (it re-scans each file), and one caveat
baked into the export: it applies *today's* roster to every year, so a
therapist who joined recently contributes zeros in earlier years.

**Export view…** saves whatever the lower table currently shows, with the
methodology file. Two honest caveats: a group's *organization* NPI can carry
extra volume not counted here (benchmark it on the Practice benchmark tab),
and "missed" can also mean the relationship exists but fell under the
11-patient privacy floor.

### Tab 7 — Provider lookup
*"Tell me everything about this one provider."*

Type any 10-digit NPI and click **Look up provider**. The app pulls together
everything it knows into one profile:

- Current Medicare eligibility and flags (from the Search tab's data).
- Name, specialty, and city/state.
- Which practice group(s) they belong to (if you downloaded that dataset).
- Their 2015 referral activity, in two tables: who sent them patients, and who
  they sent patients to — each with names and specialties. (Needs the Referral
  map dataset.)

Both referral tables export. Whatever optional datasets you haven't downloaded
are simply noted as unavailable — the rest still show. It's the quickest way to
size up a referrer, a competitor, or a prospect.

**Referral heat map:** click **Referral heat map…** to see *where* an NPI's
referrals come from geographically. The app scans every inbound referral pair,
looks up each source provider's practice ZIP, and shows a density table
(patients per ZIP, % of volume, miles from the practice, top source). Then
**Save map (HTML)…** writes an interactive map — one circle per ZIP, sized and
colored by referral volume, your practice pinned — and offers to open it in
your browser. The file is **self-contained** (the map software is built in),
so you can email it to a client as-is; only the street background needs an
internet connection, and the page says so plainly if it can't load. A density spreadsheet and methodology file are saved next
to it. First run on a big practice takes a few minutes (one registry lookup
per source — cached, so the second run is fast). One caveat: locations are
today's NPPES addresses, so a source that moved is drawn where it is now.

**Source analysis (the client-ready report):** click **Source analysis…** for
an in-depth look at one organization's referral base, then **Save report
(HTML)…** for a polished, fully self-contained report you can open, print, or
email to a client: key metrics up top (total patients, source count, top-source
and top-5 dependence, and an HHI concentration score that flags when a practice
is dangerously dependent on a few relationships), auto-written findings, a
top-sources chart, the specialty mix of the referral base, a concentration
curve, volume by distance from the practice, an embedded **referral-geography
heat map** (one circle per source ZIP, sized and colored by volume, your
practice pinned, with a top-ZIP table — the map software is built into the
file; only the street background needs an internet connection, and the page
says so plainly if it can't load), and — on CareSet data — a
referral-lag profile that separates true referral flow from co-occurring care.
The report also includes a **competitive landscape**: every outpatient rehab
provider within 10 miles of the practice, ranked by inbound Medicare referral
volume, with the analyzed practice's rank and share of area volume, its top
competitors, and a top-15 table (the practice's own row highlighted — and
always shown, even when it ranks below the top 15; equal volumes share a
rank). A full ranked source spreadsheet and methodology file are saved next
to it.

**Getting all your years in:** the year-over-year section compares whatever
CareSet years are in your data folder, so import each year once (Referral map
tab → **Import CareSet file**, one file at a time). Import them all and the
analysis spans 2016–2022; import two and it compares those two. Keep the
original downloads somewhere safe — CareSet download links expire, and once a
link goes dead the only copy is the one you saved.

**Year-over-year performance:** tick **Include year-over-year** before clicking
**Source analysis…** and the report gains a performance section covering every
CareSet year you've imported: a column chart of referral volume per year with
the distinct-source count tracked over it, a **source-retention** chart showing
how many referrers were kept, gained, and lost each year, a per-year table
(volume, sources, concentration, top-5 dependence, retention, largest source),
and **biggest gains / biggest declines** tables naming the referrers that grew
or fell away between your first and last year. Two extra spreadsheets are saved
beside the report (`.by-year.csv` and `.movers.csv`). This repeats the full
scan once per year, so it adds several minutes per year — it's off by default.
Only CareSet years are compared; the 2015 CMS file is deliberately excluded
because its shorter window would fake a trend. Read declines carefully: a
referrer can vanish simply by falling under the 11-patient floor, and Medicare
Advantage growth moves patients out of this data entirely.

**Keep every supporting file on your own machine.** Government download links
move and expire (the 2016–2020 CareSet links already did). One command
downloads what it can and writes a manifest listing every file, its size, a
SHA-256 checksum, its source URL, and what it's for:

```powershell
Import-Module .\ReferralMap
Save-RmLocalResources -Destination "C:\MedicareData\resources" -Verbose
```

Read `MANIFEST.txt` in that folder afterwards: anything marked **MANUAL**
needs a click (the NPPES monthly file has no fixed link — its name carries a
date), and everything else is already downloaded. Keep that folder backed up
alongside your CareSet year files, and the app never depends on a URL again.
Small reference data (ZIP centroids, ZIP→county crosswalk, taxonomy names,
and the offline map library) is already **bundled inside the app** — nothing
to download for those.

**Optional power-ups (three big free CMS files):** one-time imports from
PowerShell (open PowerShell in the app folder first):

- **Care Compare clinician file** — lets the Source analysis automatically
  list the other therapy clinicians in your practice group, so you know
  exactly which NPIs to paste together for a combined analysis. Download the
  "National Downloadable File" CSV from
  https://data.cms.gov/provider-data/dataset/mj5m-pzi6 (~800 MB), then run:
  `Import-Module .\ReferralMap; Import-RmCareCompare -Path "C:\path\DAC_NationalDownloadableFile.csv"`
- **NPPES bulk file** — the biggest single upgrade. Provider lookups run
  locally and instantly, and **competitor sweeps become complete**: the live
  registry caps each query at 1,200 results and searches by description
  phrase, while the local file is swept in full. A real 10-mile sweep around
  Sun City took **11 seconds instead of ~11 minutes**. Download the monthly
  "NPPES Data Dissemination" zip from
  https://download.cms.gov/nppes/NPI_Files.html (~1 GB; do NOT unzip it),
  then run: `Import-RmNppesBulk -Path "C:\path\NPPES_Data_Dissemination_....zip"`
  Re-import each month to stay current. The live registry stays as a fallback
  for any area the monthly file doesn't cover, so a stale file never makes a
  real ZIP look empty.
- **Medicare Monthly Enrollment** — makes county market context (beneficiary
  counts and the Medicare Advantage share) work with no internet. Download
  the CSV from https://data.cms.gov/dataset/d7fabe1e-d19b-4333-9eff-e80e0643f2fd
  then run: `Import-RmEnrollment -Path "C:\path\Medicare Monthly Enrollment Data.csv"`

**Smaller practices — combine your NPIs:** a practice's Medicare volume is
often split between its **organization NPI** and its therapists' **individual
NPIs**, and pairs under 11 patients are excluded from the data entirely — both
hit small practices hardest. To get the full picture, paste **several NPIs
into the NPI box** (separated by spaces or commas — e.g. the org NPI plus each
therapist's NPI) and click **Source analysis…**: the volumes are combined into
one practice, a source feeding several of your NPIs is counted once with
summed volume, and patient flows *between* your own NPIs are excluded as
internal. The report header shows how many NPIs were combined.

**Who counts as a "rehab provider":** the provider sweep is scoped to
outpatient PT, OT, and speech therapy — PT/OT/SLP individual providers
**including board-certified subspecialties** (orthopedic, hand, pediatric,
sports…), PT clinics, rehabilitation clinics, outpatient CORFs, and hearing &
speech clinics. Deliberately excluded: PT/OT/speech **assistants**,
physiatrists (physicians — they're referral *sources*), cardiac and
substance-use rehab, and inpatient rehab units/hospitals. Every export's
methodology file states this scope.

**Referral trend (multi-year):** if you've imported **two or more** CareSet
years on the Referral map tab, this button builds a year-by-year table for the
NPI — how many sources fed them patients each year, total inbound and outbound
volume, and each year's top sources. It's how you see a clinic growing,
shrinking, or losing a key referrer over time. Fair warning: it re-scans every
year's file, so it takes **several minutes per imported year** — start it and
get a coffee. Two honest caveats baked into the export: a source dropping to
zero may just mean the pair fell under the 11-patient privacy floor, and
Medicare Advantage growth pulls patients out of this data over time, which can
look like decline.

### Tab 8 — Watchlist
*"Did any of MY referrers change in the latest update?"*

Paste your referring providers' NPIs into the box and click **Save watchlist**
(it's remembered between sessions). Then, after each update, click **Check now**:
you'll see just your referrers, each with their current eligibility and a
**ChangeSinceLast** column — dropped from the list, eligibility flags flipped,
renamed, or unchanged. It turns the one-time Batch check into ongoing
monitoring, so a referrer who loses eligibility is a phone call, not a surprise
denial. Export the report.

---

## 5. Keeping everything current automatically (optional)

If you'd rather never click "Check for updates": open PowerShell in the
`medicare-order-referring` folder (right-click the folder while holding
Shift → "Open PowerShell window here") and paste:

```powershell
Import-Module .\OrderReferring; Install-OrfUpdateTask -At 07:00
```

Windows will now quietly check for a new Medicare list every morning at 7:00
and download it only when there actually is one.

> **If you see "Access is denied"** when running that command, close PowerShell,
> then reopen it as administrator (right-click **Windows PowerShell** →
> **Run as administrator**) and paste the command again. Creating a scheduled
> task sometimes needs administrator rights.

To turn it off later:

```powershell
Import-Module .\OrderReferring; Uninstall-OrfUpdateTask
```

---

## 6. Where your files live

- **Downloaded data** is stored in `C:\Users\<you>\AppData\Local\OrderReferringTracker`
  — you never need to touch it. Deleting that folder just means re-downloading
  (or re-importing from your saved CareSet zips). Imported CareSet years are
  the big items in there: **7–11 GB per year** — if disk space gets tight,
  delete an imported year's `hop_teaming_<year>.csv` from the `referral-map`
  subfolder and re-import it later from your archived zip.
- **Exports** go wherever you choose in the save dialog. Every export comes
  with a small `.methodology.txt` companion file that records exactly which
  data release the numbers came from — keep it with the spreadsheet so the
  numbers stay defensible later.

## 7. If something goes wrong

- Every error appears in plain English in a pop-up; your existing data is
  never damaged by a failed download — the app only swaps in a new file
  after it has fully arrived and passed validation.
- **"Could not reach the CMS data catalog / NPPES registry"** — it's your
  internet connection or the CMS site being down. Wait and retry.
- Searches feel slow right after startup? The app is loading the 2-million-row
  file into memory (a few seconds); the status bar says so.
- Worst case: close the app and start it again. Nothing you do in the app can
  corrupt the downloaded data.
