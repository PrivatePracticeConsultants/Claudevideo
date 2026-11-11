# Getting started — running MRF Explorer on your computer

This walks you from a fresh machine to the dashboard running in your browser,
with real payer rate data loaded. No prior setup assumed. Two tools live in
this repo; this guide is for **MRF Explorer** (`mrfx`), the drop-in dashboard.

Pick your operating system and follow the blocks marked for it. Commands are
typed into a **terminal**:
- **macOS**: open the **Terminal** app (Cmd-Space, type "Terminal").
- **Windows**: open **PowerShell** (Start menu, type "PowerShell").
- **Linux**: your usual terminal.

---

## Step 1 — Install Python (one time)

**Any Python from 3.11 to 3.14 works.** Every package this app pins ships a
ready-made Windows/macOS installer for all of those, so nothing has to be
compiled. Check what you already have:

```
python --version
```

(On Windows, `py -0p` lists every version installed.) If it prints 3.11, 3.12,
3.13 or 3.14 you are done — skip to Step 2. Otherwise install **Python 3.13**
from <https://www.python.org/downloads/windows/> (macOS:
<https://www.python.org/downloads/macos/>); on Windows tick **"Add python.exe
to PATH"** on the first installer screen. **Not 3.15:** python.org's big
"Download Python" button now gives 3.15, and duckdb, pyarrow, numpy, ijson,
PyYAML and watchfiles publish no 3.15 installers yet, so the install fails
with "Failed to build installable wheels".

> **Historical note.** Older copies of this guide insisted on Python 3.12,
> because `duckdb` 1.3.2 shipped no build for anything newer and pip fell back
> to compiling it from source — the "Failed to build installable wheels" error.
> duckdb 1.5.5 covers 3.14, so that requirement is gone. If you still hit that
> error, you are on an old `requirements.txt`: update it and retry.

---

## Step 2 — Get the code

**Option A — you have the zip I sent** (`mrfx_project.zip`): double-click to
unzip it, which gives you a folder (e.g. `Claudevideo`). Move it to a short
local path that is NOT synced to the cloud, e.g. `C:\mrfx\`. Avoid Desktop,
Documents and OneDrive: on Windows 11 they sync to the cloud by default, so the
database and multi-GB downloads would be uploaded and locked mid-write.

**Option B — download with git** (if you use GitHub):

```
git clone --branch claude/gracious-mccarthy-ao7vmo <your-repo-url>
```

Either way, `cd` into the folder in your terminal:

```
cd path/to/Claudevideo
```

(Tip: type `cd ` with a trailing space, then drag the folder onto the terminal
window to fill in the path, then press Enter.)

---

## Step 3 — One-time setup (virtual environment + install)

This creates an isolated Python environment inside the project so nothing else
on your machine is touched.

**macOS / Linux:**
```
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install -e .
```

**Windows (PowerShell):**
```
py -3.13 -m venv .venv
.\.venv\Scripts\Activate.ps1
python --version
python -m pip install -r requirements.txt
python -m pip install -e .
```
`python --version` should print 3.11, 3.12, 3.13 or 3.14 once the environment
is active. (`py -0p` lists every Python installed; if you have several, pick
one with e.g. `py -3.13 -m venv .venv`.)

> **If you see `Failed to build installable wheels` (duckdb, pyarrow,
> watchfiles, pydantic-core):** pip found no ready-made installer for your
> Python and tried to compile one. That means either a Python NEWER than 3.14
> (python.org's default download is 3.15 since autumn 2026),
> or an old copy of `requirements.txt`. Don't install a compiler: delete the
> half-made `.venv` folder, use Python 3.11–3.14 (`py -3.13 -m venv .venv`),
> and re-run the lines above.

> If Windows blocks the activate script with a security error, run this once,
> then retry the activate line:
> `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`

You'll know the environment is active when your prompt shows `(.venv)` at the
start of the line. **Any time you open a new terminal to use the app, re-run
just the `activate` line** (the `source .venv/bin/activate` or
`.venv\Scripts\Activate.ps1` step) — you don't reinstall.

> **The headless-browser helper — now automatic.** Some payers
> (Molina, Kaiser, Aetna, Harvard Pilgrim, Regence, and Oscar's file page)
> build their file lists with JavaScript. The first time you paste one of those
> pages, the app downloads a private copy of the Chromium browser (~150 MB) for
> itself and retries automatically — you don't have to run anything. (If you'd
> rather pre-download it, or the automatic step ever fails because you were
> offline, you can still run it by hand:)
>
> ```
> playwright install chromium
> ```
>
> It downloads a private copy of the Chromium browser (~150 MB) used only for
> reading payer pages. Skip it if you don't need those payers — everything
> else works without it, and the app will tell you the exact command if a
> page turns out to need it.

---

## Step 4 — Start the app

```
mrfx serve
```

You'll see a line like:

```
  MRF Explorer  →  http://localhost:8377
  inbox: data/inbox  (drop .json / .json.gz / .zip here)
```

Open **http://localhost:8377** in your web browser. The dashboard is now
running. Leave this terminal window open — it's the server. Press **Ctrl-C** in
it to stop the app when you're done.

---

## Step 5 — Load payer data: just paste links (easiest)

You don't need to download anything by hand. Find a payer's transparency page
(the **Sources** tab in the dashboard lists them by state), copy a link, and
paste it into the app:

1. In the dashboard, open the **Files** tab.
2. In the **"Paste file links — the app does the rest"** box, paste one or
   more links (one per line) and click **Add links**.

Any of these link types work:

- **A direct rate file** — ends in `.json.gz` or `.json`
  (e.g. `..._in-network-rates_...json.gz`). Downloaded and analyzed.
- **A Table of Contents / index link** — e.g.
  `https://tcr.bcbsms.com/Table_of_Contents/Local_TOC.json`. The app opens it
  and automatically queues **every rate file listed inside** (often hundreds).
- **A plain file-listing page** — e.g. a folder page on
  `https://mrfdata.hmhs.com`. The app lifts every file link off the page and
  queues them all.

The queue table under the box shows each link's status
(`queued` → `downloading` with a MB progress bar → `fetched` (downloaded,
waiting for a free parser — normal on a busy grind) → `ingesting` → `done`
with a row count; index links show `expanding` while their file lists unpack;
a file bigger than the safety limit shows `too big` — see the disk section
below).
Files process **a few at a time in the background** — by default as many as
your computer has processor cores minus one, or fewer if your RAM can't hold
that many (about 2 GB each after the database's 8 GB: 2 on a 16 GB machine,
up to 7 on 32 GB; the
`parallel_ingests:` knob in `config/mrfx.yaml`, `0` = automatic), and **several files download at once**
to keep those workers fed (`parallel_downloads:`, `0` = automatic, capped at
6 — the same per-host limit a web browser uses; simultaneous downloads
coordinate so they can never fill your disk together) — you can paste a whole
state's worth of links and walk away. Each file's download is deleted
after its rates are extracted, so your disk doesn't fill up. If you close the
app mid-download, it picks up where it left off on restart.

Not every link is a rate file: **allowed-amounts** files (out-of-network
billed/allowed averages) contain no negotiated rates, and the app tells you so
and skips them rather than loading junk. The app also spots **duplicates**:
Blue plans publish copies of each other's national files, so when a link
downloads to the exact same bytes as a file another link already loaded, it's
skipped with a note saying which link it duplicates — your database stays
clean even if you paste every state's index.

- **The same link twice** is never downloaded again: once a link is done,
  pasting it again (even with a fresh "signed" ending) is ignored.
- **The same file under a different link** is caught EARLY when it can be
  proven a copy: after the first 15 MB arrive, the app compares them with
  every file you have already loaded of exactly the same size (links AND
  files you dropped into `data/inbox/`). If they match, it asks the server for
  the file's last 1 MB and 8 small samples from the middle; if all match AND
  the file is compressed (`.gz` or `.zip` — their last bytes carry a checksum
  of the whole file), it stops and skips the file as a duplicate. A plain
  `.json` can differ in a single rate in the middle while matching all of
  that, so a plain `.json` always downloads in full and the full-content check
  decides — nothing different is ever skipped. Change the 15 with
  `early_dedup_mb:` in `config/mrfx.yaml` (0 turns the early check off; if
  your `mrfx.yaml` has no such line, add it — `config/mrfx.yaml.new` from the
  update shows it). A file loaded before this check existed is caught after
  its first copy's full download — and that copy then fingerprints it, so
  every LATER copy is caught early.
- **Two copies downloading at the same time** (several states' indexes list
  the same national file): the later one, once its beginning matches the
  earlier one's, shows **"waiting: an identical-looking copy is already
  downloading"** and sits still — no reconnecting — until the earlier one is
  loaded (then it is skipped without downloading the rest) or fails (then it
  carries on by itself). Nothing is lost either way.
- **Small files go first.** Of the 6 download slots, 3 take files of 100 MB
  or less (and files whose size isn't known yet), so small files are never
  stuck behind huge ones. One of those 3 never takes anything else; the
  other two help with big files whenever no small file is waiting. A small
  slot that finds a file is bigger (or the server won't say its size) hands
  it to a big slot at once, before saving any of it. Change with
  `small_file_downloads:` / `small_file_mb:` in `config/mrfx.yaml` (0 = no
  lanes; add the lines if your file doesn't have them).

**Don't want to hunt for links at all?** The app ships ready to go:

- **`mrfx add --known`** (or `mrfx add --known --states MO,KS` for just your
  states) — every queueable source in the tested-sources catalog
  (`config/known_sources.yaml`). `config/starter_links.txt` is the same list
  as plain text, generated from the catalog (109 links), for pasting a subset
  into the dashboard; to keep your own list, copy it and edit the copy (an
  update replaces the original):

  ```
  mrfx add --known --states MO,KS
  ```

  The whole list queues 109 automatic sources (audited live 2026-11-05 — see
  docs/SOURCES_AUDIT_2026-11.md): the Highmark-hosted Blue plans, about 20
  more Blue plans (Michigan, Kansas City, Louisiana, Idaho, Arizona,
  Nebraska, Vermont, Excellus, Premera, Wellmark, Blue Shield of CA…),
  Anthem's 14-state master index, UnitedHealthcare, Aetna, Cigna, Kaiser,
  Centene/Ambetter, regional plans across the country, and national
  administrators and networks (Meritain, Allied Benefit Systems, HealthComp,
  MedCost, MultiPlan/PHCS…). That is hundreds of thousands of files and many
  terabytes, a grind of weeks, so most people paste only the sources for
  their own states from **Show tested sources** instead. Some sources use the
  headless browser, which the app downloads for itself on first use. Anthem's
  index is 11.5 GB and asks for "download anyway" unless confirm_over_gb is
  15 or more. Each big index is capped at 2,000 files unless you raise
  max_toc_files (in `config/mrfx.yaml`, shown there with a `#` in front).
  The same file lists the remaining browser-only portals (Humana, HCSC, HAP…)
  with instructions in the comments. Click **"Show tested
sources"** on the Files tab to browse it, or **"Queue tested payer indexes"**
to load all the automatic ones — monthly-dated links are refreshed to the current month
automatically. The catalog lives in `config/known_sources.yaml` with each
source's verification date and test results.

Prefer the terminal? The same thing works there:

```
mrfx add https://tcr.bcbsms.com/Table_of_Contents/Local_TOC.json
mrfx add --file my_links.txt        # a text file of links, one per line
mrfx add --known                    # queue all tested payer indexes
```

If the dashboard is running, `mrfx add` hands the links to it; if not, it
downloads and processes them right in the terminal with a progress bar.

---

## Step 6 — Or load a file you already have

If you already downloaded MRF files yourself, two ways to load them:

- **Small files (under 1 GB):** drag the `.json.gz` onto the **Files** tab's
  drop zone in the dashboard.
- **Any size:** copy the file into the **`data/inbox/`** folder inside the
  project. The app watches that folder and picks up new files automatically.

  ```
  # example (macOS/Linux)
  cp ~/Downloads/2026-07-01_somepayer_in-network-rates.json.gz data/inbox/
  ```

`.json`, `.json.gz` and `.zip` all work. **A `.zip` holding several JSON files
is read for its LARGEST one only** — the others are ignored; if a payer's zip
packs several rate files, unzip it and drop each `.json` in.

Watch the **Files** tab: the file shows `processing` (with a progress bar for
large files) and then `done`, with a row count and a data-quality summary. Big
files can take several minutes — that's normal; the progress bar shows chunks
completing.

> Want to check a file *before* committing to a long parse? Stop the dashboard
> (Ctrl-C — while it runs it keeps the database open, so CLI commands politely
> refuse), then run `mrfx preflight data/inbox/<file>` — it reports the payer,
> size, and whether it's ready to ingest, in seconds. (Large files also park as
> "waiting for confirmation" on the Files tab, which shows the same facts.)

---

## Step 7 — Explore, benchmark, export

Once a file is `done`:

- **Explorer** tab: the rate table (one row per practice × code). Use the
  filters — payer, discipline (PT/OT/SLP), code chips, state/city — and sort by
  clicking column headers. Click a row to drill into a practice.
- **Code comparison** tab: pick a code → see which practices are paid most,
  with a distribution chart.
- **Benchmark** tab: pick a subject practice → see where its rates sit versus
  the market (percentiles), the dollar gap, and — with your own volume numbers —
  an opportunity estimate and a printable pitch report. You never have to pick
  an as-of month: it defaults to **Latest available** (each contract line at
  its newest publication); the dropdown is only for pinning a historical
  snapshot.
- **Export**: the **Export CSV** / **Export + methodology** buttons download the
  current filtered view. The **Outreach CSV** button gives one row per practice
  with name + address + phone + per-code rate/percentile columns — ready to
  cross-reference your contact list and mail-merge (e.g. in Brevo).
- **Clients** tab — your book in one screen. Save each client practice once;
  every refresh answers: whose rates moved this month (and the biggest move),
  who has contract gaps worth chasing, and which of their referral sources
  lost Medicare order/refer standing. Click any cell to land on the matching
  tab with that client already loaded and running.
- **Clients** tab also carries the consulting workflow: **monthly packets**
  (one click writes a branded folder per client — rate card, market position,
  what moved, gaps, Medicare alerts, and results-since-baseline), **engagement
  baselines** (snapshot a client at the start, then show before/after proof of
  what your work moved), and the **renewal radar** (contracts whose published
  end date is coming up — with the coverage figure shown first, because payers
  publish that field inconsistently).
- **Leads** tab adds **new to network** — practices that appear in a payer's
  book this month and were absent last month. A practice that just signed its
  first commercial contract is the strongest lead in this data.
- **Files** tab adds the **underpayment check** — paste remit lines and see
  every one paid below what that payer published for the practice, with the
  shortfall and a CSV. Read the caveats: MPPR, assistant modifiers and patient
  cost-share all legitimately sit between a published rate and a payment.
- **Rate card** tab → **One visit, by payer**: what one follow-up visit is
  worth from each payer at that practice's own contracted rates (type the
  practice name, TIN or an NPI, then **Build rate card** — the visit prices
  itself). The default visit is the four most-billed timed therapy codes
  (97110, 97530, 97140, 97112), 15 minutes each = 4 units; pick the version
  without the 97140 + 97530 pair, the gait visit, or type your own codes and
  minutes. Units always follow the CMS 8-minute rule. The dollar figure is
  the contracted value per visit — the ceiling on net revenue per visit
  (NRPV); real collections are lower (patient balances, denials, and some
  payers cut the 2nd+ therapy unit). A payer missing a rate for one of the
  codes shows "–" instead of a made-up total.
- **Negotiate** tab now also builds the **rate proposal** — pick a basis
  (payer's median or p75, best comparable rate, or % of Medicare), paste
  annual volumes, and print the proposal grid the payer receives. Proposed
  rates never fall below current, and every number's basis is stated.
- **Medicare** tab also ranks **referral leaders** — practices by shared
  Medicare patients received, searchable around a ZIP with a radius filter.
  Clicking a practice that has rates in your store opens every payer rate the
  MRF files price it at. **Coverage note:** the ranking sees only practices
  connected to your store (the referral import keeps pairs that touch your
  providers), so it grows with every payer you ingest — it is your market's
  referral view, not a census.
- **Medicare** tab (optional): who shares Medicare patients with a practice and
  whether each of those providers may still order/refer for Medicare — plus a
  batch NPI checker. If you use the **Medicare Order & Referring Tracker**,
  open this tab and it finds the tracker's downloads by itself: click
  **Import them now** and it loads them in place. Nothing is re-downloaded,
  and you don't have to stop the app. Each time a fresh eligibility snapshot
  is imported, the app reports who joined, dropped off, or lost Part B since
  the previous one, and flags any affected referral source.

  Once those layers are loaded, every practice you open anywhere in the app
  (click a row on Explorer, Leads, Payer roster…) shows its referral base and
  any recent loss of order/refer standing.

---

## Using it with the Medicare Order & Referring Tracker

The two tools know opposite halves of the same practice. The tracker knows
**who sends it patients** and **who may order/refer**; MRF Explorer knows
**what each commercial payer pays it**. They join on NPI, and the app does the
joining for you:

1. **Import.** Medicare tab → **Import them now**. The app looks in the
   tracker's own data folder (`%LOCALAPPDATA%\OrderReferringTracker` and the
   other places it uses) and imports whatever it has that you don't. If your
   tracker lives somewhere unusual, set `tracker_dir` in `config/mrfx.yaml`.
2. **Keep it current.** CMS refreshes the eligibility roster about twice a
   week. Whenever the tracker downloads a newer one, the tab offers it again —
   or set `tracker_auto_import: true` in `config/mrfx.yaml` to have the app
   pick up a newer roster at startup on its own. (Referral files are never
   auto-imported: they're several GB and may be licence-restricted.)
3. **Go the other way.** Rate card tab → **Tracker bundle (.zip)** packages one
   practice's NPIs and rates for pasting into the tracker's Provider lookup.

From the terminal instead: stop the server, then `mrfx medicare --from-tracker`
imports everything new in one go.

> **Licence.** DocGraph Hop Teaming years come from CareSet under CC BY-NC-SA
> 4.0 — **non-commercial**. If you use that data in paid consulting work,
> confirm your terms with CareSet. The CMS shared-patient file carries no such
> restriction.

---

## Provider names fill in automatically

The rates load with raw NPI numbers first, then the app fills in each
provider's **name, city/state, and taxonomy** in the background. You'll see a
banner like *"5,192 / 854,692 names (identifying 849,492 more…)"* while it
works.

**This is automatic.** The app keeps CMS's **NPPES full monthly file** (a
~1.1 GB `.zip`) in `data/nppes` by itself: it checks
<https://download.cms.gov/nppes/NPI_Files.html> at most once a day, downloads a
new month in the background, builds a small local lookup cache
(`nppes_cache.parquet` in your store folder) in one pass, and then identifies
every provider — the whole backlog at once, and anything new you ingest later —
in seconds, with no per-provider internet lookups. Measured on a 32-million-rate
store: a fresh install named all 93,479 providers in ~3½ minutes including the
download. When a new month arrives it also **refreshes providers whose name,
specialty or address changed**. The newest two monthly files are kept; older
ones are deleted.

If CMS can't be reached, it retries within the hour and meanwhile uses the file
it already has — or, on a brand-new install, looks names up one by one over the
internet (the slow way: rate-limited, can take days for a big book).

**Managing the file yourself instead** (optional): set `auto_download: false`
under `enrichment:` in `config/mrfx.yaml` and point `bulk_csv_path` at a full
monthly `.zip` you downloaded (keep it zipped; a **folder** also works — its
newest NPPES zip is used). On Windows keep the path in single quotes:

```yaml
enrichment:
  auto_download: false
  bulk_csv_path: 'E:\NPPES\'
```

You can also run it as a one-off command (stop the dashboard first):

```
mrfx enrich --bulk "E:\NPPES_Data_Dissemination_November_2026_V2.zip"
```

> The app only trusts the **full monthly** file for this (it's ~8–9 million
> providers). If you accidentally point it at a small *weekly* update, it
> notices the file is too small, keeps everyone's names instead of blanking
> them, and leaves the banner honestly above zero until you swap in the full
> file.

**Bonus once this is set up: the brand-new-clinic feed.** The same file carries
the date each NPI was *issued*. Leads tab → **Brand-new clinics** lists therapy
NPIs enumerated in the last few months near a ZIP — practices that have no payer
contracts yet, which is the earliest anyone can reach them. Nothing extra to
download; it reads the cache the step above already built. (If you set this up
before this version, the cache rebuilds itself once on the next enrichment pass
to add the date column.)

---

## More free files worth grabbing (optional, high value)

Both are public-domain US government downloads. Neither is required; each makes
a specific part of the app sharper. Load them on the **Files tab → Reference
data** by pasting the *path* you saved them to (they're large — the app reads
them off your disk rather than uploading through the browser).

### 1. Medicare volumes — fills in "annual units" for you

Every dollar figure the app produces (opportunity, proposal value, the win
report) needs annual units per code. Today you type those in — impossible for a
practice that isn't your client yet.

- **What to get:** *Medicare Physician & Other Practitioners — by Provider and
  Service*, one year, from <https://data.cms.gov/provider-summary-by-type-of-service>.
  Download the CSV (roughly 2 GB; keep it on a drive with room).
- **What you get:** a **Fill from Medicare** button next to every units box
  (Benchmark, Negotiate, Clients → results), and prospect sizing — you can see
  roughly how much therapy a practice bills before you ever call them.
- **The honest limit, stated on every result:** these are Medicare
  fee-for-service claims only — no commercial, Medicare Advantage, Medicaid or
  cash — and CMS hides any code with fewer than 11 patients. So the number is a
  **floor**, not the practice's book. There's a multiplier box if you know
  Medicare is (say) a third of a client's visits; whatever you scale by is
  printed as an assumption, never as data. The file also lags about two years.

### 2. Census ZIP demographics — tells you if a market is worth working

- **What to get:** an **ACS 5-year table by ZCTA** from
  <https://data.census.gov> — table **B01001** (sex by age, which carries the
  65+ buckets) and/or **B19013** (median household income). Choose geography
  "ZIP Code Tabulation Areas", then Download CSV. The app reads both the
  variable-id form (`B01001_020E`) and the human-labeled export, and sums the
  65+ buckets for you.
- **What you get:** Leads tab → **Market sizing**: population, seniors and
  median income inside a radius, against the therapy practices this store knows
  — i.e. *seniors per practice*. High means room for a new client or a rate
  increase; low means saturated.
- **The honest limits:** ACS figures are 5-year *estimates*, and ZCTAs aren't
  quite ZIP codes (PO-box ZIPs have none — those are reported as unmatched, never
  as zero people). Practice counts only include practices some ingested payer
  file priced, so "seniors per practice" is an upper bound.

### 3. Your state's Medicaid and workers'-comp fee schedules

The single fastest way to end an argument about whether a rate is defensible:
*"your commercial rate is 4% above Missouri Medicaid."* Workers' comp usually
runs the other way — above commercial — which turns "we can't pay more" into
"you already pay more, under a different schedule."

- **What to get:** your state Medicaid agency's therapy fee schedule and your
  state's workers'-comp medical fee schedule. Every state posts its own, in its
  own format. You need **any CSV with a code column and a rate column** — if
  yours arrives as a PDF or a spreadsheet, export the two columns you need.
- **Where it goes:** Negotiate tab → **Against Medicaid and workers' comp**.
  Pick the kind and the state, load the file, press Compare. (Or
  `mrfx floors mo_medicaid.csv --kind medicaid --state MO --year 2026`.)
- **The honest limits:** these are *your* uploaded numbers and the app never
  fetches or invents them — the label you give a schedule is what gets printed
  in reports. A Medicaid schedule is the fee-for-service maximum allowable, and
  managed-Medicaid plans commonly pay a percentage of it, so a practice's real
  floor can be lower. A code your schedule doesn't list is left out of the
  comparison entirely, never treated as $0.

### 4. The local hospital's price-transparency file

CMS requires every hospital to publish its negotiated rates. The hospital
outpatient department bills the same 97xxx codes to the same payers — usually
at a multiple of what a private practice gets.

- **What to get:** search the hospital's name plus "price transparency" — the
  file is on the hospital's own website, as CSV or JSON. Both work.
- **Where it goes:** Negotiate tab → **Hospital comparison**. (Or
  `mrfx hospital stmarys_standardcharges.csv --state MO`.)
- **The honest limit, printed on every result:** a hospital rate is a
  **facility** rate — it pays for the department, its space and its overhead,
  and the hospital often bills a separate professional line on top. Same CPT
  code, different economics. Use it as evidence of what a payer already pays in
  your market, never as an amount your client is owed.

### 5. NPPES weekly updates (if you already load the monthly file)

If you use the NPPES monthly file for provider names, the weekly incrementals
keep it current between monthly downloads — which matters most for the two
feeds built on those columns: brand-new clinics and closures, where a month of
lag is most of the signal. Download the weekly zip from the same NPPES page and
run `mrfx enrich --weekly npidata_pfile_...Weekly.zip`. It updates only the
NPIs that changed that week and leaves everything else alone.

---

## Making a big queue finish faster

The app already works in parallel: several downloads run at once to keep
several parser processes fed, and it auto-sizes both to your machine. When the
download queue starts working you'll see log lines like these (they appear
once it's running more than one of each; a single-worker machine won't show
them):

```
14:05:01 INFO    mrfx.fetch: parallel downloads: 3 fetcher thread(s)
14:05:01 INFO    mrfx.fetch: parallel ingest: 3 parser worker process(es)
```

Parsing is the slow part, and how slow depends on the kind of file. What to
expect per worker (measured on real payer files on a 4-core test machine in
Nov 2026 — your PC will differ, but the shape holds):

| Kind of file | Real example | Time per worker |
|---|---|---|
| Big file, few of your codes in it | UHC, 5.5 GB uncompressed | 35–50 s (6–9 s per uncompressed GB) |
| Very big file | BCBS Wyoming, 42 GB uncompressed | about 7½ minutes |
| None of your codes in it | BCBS North Dakota, 3 GB | about 1 minute (skipped quickly) |
| Huge number of providers per rate | Aetna, 20 GB → 590 million rows | 25–30 minutes — limited by the rows it writes, not the file size |

So a file that sits at the same percentage for a minute is normal; one that
shows **no movement for an hour** is not (the app flags that on its own). For
the many-rows kind, `therapy_only_ingest: true` in `config/mrfx.yaml` is the
big lever: on that Aetna data it cut the rows ~10× and the time roughly in
half. Read its comment in that file first — it **permanently** leaves out the
non-therapist providers (MDs, NPs…) who also bill these codes, and it needs
the NPPES provider data loaded before it can tell therapists apart.
Here's what moves the queue's total time, in order of impact:

1. **Use more of your CPU (if you have it).** Auto mode uses your core count
   minus one, stops at **8**, and also stops where your memory runs out at
   about 2 GB per worker next to the database: an 8 GB PC gets **1** worker,
   a 16 GB PC **3**, a 32 GB 8-core PC 7. If Task Manager →
   Performance → CPU shows more logical processors than the banner is using
   AND you have plenty of memory, set it explicitly in `config/mrfx.yaml`:

   ```yaml
   parallel_ingests: 11   # e.g. on a 12-core machine
   ```

   Rule of thumb: **allow ~2 GB of RAM per worker** and leave a few GB for the
   database and Windows itself. On a 16 GB machine, don't go past 5–6; on
   32 GB, 10–12 is fine. (If you've ever seen an out-of-memory error on this
   machine, stay at the auto setting.) Values above your core count are
   clamped — extra processes past the cores only fight each other. Each
   worker keeps a bit more than one core busy (about 1.2–1.5: its file
   reading and database writing run alongside the parsing), so you don't
   gain anything by going past your core count. Measured on a 4-core test
   machine: 1 worker 154 s for a test queue, 2 workers 87 s, 3 workers
   87 s — speed stops improving at about **your cores ÷ 1.4** workers
   (around 5 on an 8-core PC). If memory is tight, that number is a good
   place to stop; going higher only costs RAM.

2. **More simultaneous downloads if the parsers are starving.** On the Files
   tab, if parsers sit idle while links crawl (common with Blue-plan CDNs that
   stall and back off), raise:

   ```yaml
   parallel_downloads: 6   # max 8
   ```

   This is the right lever when **many files** are queued: it fetches more of
   them at once.

   **If the queue is only a few VERY LARGE files, that knob does nothing** —
   there is nothing to run alongside. What limits you then is usually not your
   internet speed but the payer's CDN, which throttles *each connection*: one
   5 GB file trickles in at a fraction of your line rate no matter how fast
   your connection is. Split that single file across several connections
   instead (byte-range requests, the way a download manager works):

   ```yaml
   download_segments: 4    # 1 = off, max 8
   parallel_downloads: 2   # lower this when you raise segments
   ```

   Lower `parallel_downloads` at the same time, because the two multiply
   (4 segments x 2 files = 8 open connections) and a flaky CDN starts dropping
   connections when you open too many. If a server refuses range requests the
   app notices and quietly uses one connection, so turning this on is safe.

   **If downloads look frozen rather than slow** — the MB counter sitting on
   the same number for minutes — that's a payer CDN that accepted the
   connection and then went quiet. It happens constantly with the big Blue and
   UHC file hosts. The app waits `download_timeout_seconds` (120 by default)
   for the next piece of data, then hangs up, reconnects, and carries on from
   exactly where it stopped — nothing is re-downloaded. The Files tab now says
   so on the row instead of leaving you staring at a stuck bar.

   If a particular payer drops out constantly, lowering it makes the app give
   up on each dead connection sooner:

   ```yaml
   download_timeout_seconds: 60
   ```

   Don't raise it. It is a limit on *silence*, not on how long a file may take,
   so a slow-but-steady download is never affected by it — but every second of
   it is a second a dead connection occupies one of your download slots.

   **Don't guess — measure it.** Copy one of the big links that's holding your
   queue up and run:

   ```
   mrfx speedtest "https://…the-big-file.json.gz"
   ```

   It downloads a slice of that file on 1, 2, 4 and 8 connections (a different
   slice each time, so a server-side cache can't skew the result) and tells you
   which case you're in — either the exact `download_segments` line to paste in,
   or that extra connections don't help and the file really is just that big.
   Takes a couple of minutes and changes nothing on your machine.

3. **Don't ingest what you don't need.** The single biggest cost is all-codes
   mega-files. The app already skips files that contain none of your CPT codes
   (it detects that on a fast first pass and stops), and re-adding a file you
   already have costs nothing (skipped by content). But when a payer offers
   both one national everything-file and smaller per-plan/per-state files,
   paste the smaller ones for the states you actually work in.

   The list of codes it collects lives at `codes.cpt_codes` in
   `config/mrfx.yaml`, and each line is labelled with its discipline and plain
   description so you can see what you're keeping. It covers the full PT, OT
   and SLP set — evaluations, treatment, modalities, swallowing studies, AAC,
   biofeedback and caregiver training. Trim it if you only care about a handful
   of codes and want smaller, faster ingests.

   **Adding a code only affects files ingested afterwards.** That list is the
   extraction filter, so a code that wasn't in it was never pulled out of the
   files you already loaded — no later update recovers it. To pick up new codes
   in files you already have, re-add those links (the app re-downloads and
   re-extracts them).

   **Want proof the fast reader gives the same numbers?** The app reads big
   files with a fast scanner that skips every code you didn't ask for without
   parsing it. It was checked against the original reader on real payer files
   (identical rates), but you can check it on YOUR files any time — it touches
   nothing in your data:

   ```
   mrfx fastcheck "C:\path\to\some-payer-file.json.gz"
   ```

   It reads the file both ways and prints `IDENTICAL` (plus how much faster the
   fast way was) or `DIFFERENT` with what differed. If you ever see
   `DIFFERENT`, set `fast_scan: false` in `config/mrfx.yaml` to go back to the
   original reader, and send that file's name along with the report.

4. **Keep the machine awake.** Windows sleep pauses everything mid-queue. For
   an overnight grind: plug in, Settings → System → Power → set "Put my device
   to sleep" to **Never** (screen off is fine).

5. **Fast-disk scratch — now automatic.** The heavy step at the end of each
   batch (the "rollup") writes a lot of temporary scratch data, and if that
   lands on a spinning hard drive (HDD) every parser worker sits idle waiting
   for it — you'll see the CPU graph stuck well below what your cores could do
   while the disk light pins. **You don't have to do anything about this
   anymore:** when your store is on an HDD and you have an SSD with room, the
   app detects it on startup and sends the scratch to the SSD by itself. You'll
   see a line like

   ```
   rollup spill → C:\mrfx_spill\spill-1a2b3c4d5e  [auto-selected (store is on a slower disk)]
   ```

   confirming it did (it makes its own subfolder, so nothing else on that drive
   is touched). If your store is already on an SSD, there's nothing to do and
   you won't see that line.

   To override the auto-choice — force a specific drive, or point it at a disk
   the detector didn't pick — set it yourself in `config/mrfx.yaml`; it only
   needs a few GB free:

   ```yaml
   duckdb_temp_dir: "D:\\mrfx_spill"
   ```

   And if the SSD has room for your whole book, putting the store itself there
   is best of all — `store_dir: "C:\\mrfxdata\\mrfx_store"` (move your existing
   store folder there first so you keep your data).

Changes to `config/mrfx.yaml` take effect on the next `mrfx serve` start — stop
it (Ctrl-C), edit, start again; the queue resumes where it left off.

> Why not just crank it to 100? Each parser worker is a real CPU process
> chewing a real file; past your core count they only fight for the same
> cores, and past your RAM they crash the machine into the swap file. The
> caps above are where more truly stops helping.

### When a payer's server stops answering

Payer servers often accept a download and then go quiet for minutes. The app
no longer lets that hold one of your download slots: after **4 minutes with no
data** the file is **parked**. Its slot goes to the next file in the queue
straight away, and everything it had downloaded so far is kept. The parked
file goes back in line after 10 minutes, or immediately if nothing else is
waiting, and picks up exactly where it stopped. If the next file's server also
goes quiet, it is parked the same way and the slot moves on again. So
something is always downloading while you wait on a slow payer.

In the Files & imports list a parked file shows an amber **parked** badge and
the time it goes back in line; **try now** sends it back at once. If a server
sends nothing at all six times in a row, it is treated as down: the file is
paused for 24 hours (the longer pause above) and then tries again by itself.
All of this is in `config/mrfx.yaml` (`download_park_seconds: 240`,
`download_park_minutes: 10`, `download_park_limit: 6`; `download_park_seconds: 0`
turns parking off).

---

## Command-line reference (optional)

All of these run in a terminal with the venv activated (`(.venv)` in the
prompt). They're an alternative to the dashboard buttons.

| Command | What it does |
|---|---|
| `mrfx serve` | Start the dashboard + folder watcher (Step 4) |
| `mrfx add <url> [<url>…]` | Paste links from the terminal: rate files, TOC/index links, or listing pages (Step 5) |
| `mrfx add --file links.txt` | Queue a whole text file of links (one per line) |
| `mrfx add --known` | Queue every tested payer index from `config/known_sources.yaml` |
| `mrfx add --file config/starter_links.txt` | Same list as plain text (copy it to make your own; an update replaces the original) |
| `mrfx add --retry-failed` | Re-queue every link that previously failed |
| `mrfx preflight <path>` | Inspect a file before ingesting |
| `mrfx ingest [path]` | Ingest a file or the whole inbox (shows a progress bar) |
| `mrfx status` | List ingested files and totals |
| `mrfx export out.csv --cpt 97110 --payer "Aetna"` | Export a filtered CSV + methodology sidecar (state filtering lives in the dashboard and `mrfx outreach`) |
| `mrfx outreach contacts.csv --state MO --cpt 97110,97140` | Contact/mail-merge CSV |
| `mrfx enrich` | Fill in provider names/geography now (downloads this month's NPPES file first if needed — see "Provider names fill in automatically" above); `--bulk <zip>` uses a file you choose |
| `mrfx packets` | Write this month's packet for every watchlist client |
| `mrfx renewals --clients` | Contracts with a real published end date, soonest first |
| `mrfx backup E:\backups\mrfx.zip` | Copy the whole store into one zip with a checksum manifest (stop the server first) |
| `mrfx verify [backup.zip]` | Check a backup against its manifest, or (no argument) the live store's integrity |
| `mrfx medicare --from-tracker` | Find the Order & Referring Tracker's folder and import everything new from it (stop the server first; the dashboard's Import button needs no stopping) |
| `mrfx medicare --eligibility <csv>` | Import the CMS Order & Referring roster the tracker downloaded (stop the server first) |
| `mrfx medicare --referrals <csv>` | Import CMS shared-patient / Hop Teaming pair data (stop the server first) |
| `mrfx utilization <csv> [--year 2023]` | Import a CMS Physician & Other Practitioners file — gives every practice its Medicare units per code (stop the server first) |
| `mrfx demographics <csv>` | Import a Census ACS ZCTA table — population, seniors and income for market sizing (stop the server first) |
| `mrfx newclinics --zip 63103 --radius 25` | Therapy NPIs issued recently near a ZIP — practices with no contracts yet |
| `mrfx orgreport <practice>` | One-practice bundle (NPIs + rates + Medicare layers) to pair with the tracker |
| `mrfx forget <filename> [more…]` | Erase chosen files' rates + raw copies (names from `mrfx status`) |
| `mrfx reset --confirm` | Clear the analyzed data (keeps your downloaded files) |

---

## Where things live in the project folder

- `config/mrfx.yaml` — settings (which codes to extract, port, enrichment on/off).
  Edit with any text editor; defaults are fine to start.
- `data/inbox/` — drop MRF files here.
- `data/downloads/` — where pasted links download to while processing. Each
  file is deleted automatically once its rates are extracted (turn that off
  with `delete_raw_after_ingest: false` in `config/mrfx.yaml` — the line is
  there with a `#` in front; remove the `#` — if you want to keep the raw
  files).
- `data/processed/` — files that finished (if `move_processed` is on).
- `data/failed/` — files that couldn't be read, with a reason in the Files tab.
- `data/mrfx_store/` — the analyzed database. Delete this folder (or run
  `mrfx reset --confirm`) to start clean.

### Keeping disk usage small

The big payer files themselves never accumulate: each download is deleted
automatically the moment its rates are extracted. What persists is only the
compact analyzed database in `data/mrfx_store/` — roughly **15–20 GB for
~350 million rate rows** (the raw files behind those rows were several
terabytes). Three ways to control it:

1. **Choose what goes in.** You don't have to run the whole starter list —
   paste only the payers you care about, or copy `config/starter_links.txt`
   and delete lines. On the Sources tab, add payers one at a time.
2. **Erase per file, whenever you like.** Every row in the Files tab has a
   **remove** button: it erases that file's rates from the database and
   deletes any raw copies on disk, and tells you how much it freed. Same
   thing from the terminal: `mrfx forget <filename>` (get exact filenames
   from `mrfx status`). Nothing is lost forever — re-paste the link or
   re-drop the file and it re-ingests.
3. **Peak-usage note for very large files.** While a big file processes, the
   download AND its extracted rows exist at once, so free space needs to
   roughly cover the file's size plus headroom — the app checks this before
   downloading and refuses with a clear message rather than filling the disk.

---

## Updating to a new version without losing your data

Your **data and the program are separate**: everything you've ingested —
rates, identified provider names, the link queue, entity groupings, peer sets,
backups — lives in `data/`, which is **never** part of a code download. Your
settings live in `config/mrfx.yaml`.

**The easy way (this version and later): `Update MRF Explorer.bat`.**
Close the app, then drag the new build's `.zip` onto `Update MRF Explorer.bat`
in the project folder (or run `mrfx update path\to\mrfx_project.zip`). It:

- refuses while the app is running (the database must be closed);
- checks the zip is a complete MRF Explorer build, and refuses an OLDER build
  than the one you have (an older program must not run against data a newer
  one has upgraded — `mrfx update <zip> --allow-downgrade` if you really mean it);
- installs the new build's components FIRST (needs internet for a few
  seconds). If that fails, nothing is changed and it says so — run it again
  once you are online;
- saves the program files it will replace to `data/update_backups/`;
- writes only the files that changed — **never** `data/`, `.venv/`, or your
  `config/mrfx.yaml` / `entity_map.yaml` / `registry_overrides.yaml` (if the new
  build ships a different `mrfx.yaml`, it's saved beside yours as
  `mrfx.yaml.new` so you can see new options). If a file can't be written
  (antivirus holding it), every file already written is put back and it says
  so — close other programs and run it again;
- re-points the environment at this folder (`pip install -e .`).

Then start the app and press **Ctrl-Shift-R** once in the dashboard tab
(browsers cache the dashboard's code; a hard-refresh loads the new page).

**Updating a copy that predates the updater (one time):** close the app, copy
`config/mrfx.yaml` somewhere safe, extract the new zip over the old folder
(Windows: Extract All to the folder that *contains* the project folder →
"Replace the files in the destination"), put your `mrfx.yaml` back, then run
`python -m pip install -r requirements.txt` and `python -m pip install -e .`
with the environment active. HOW_TO_RUN.md Part 5 has it click by click.

On the first restart after an update the app may notice its summary tables
are from an older version and **rebuild them once** — a one-time wait
proportional to your store size. It only recomputes the compact analytics from
your existing rates; it does **not** re-download, re-parse, or re-identify
anything.

**Adding new payer files** then works exactly as always — re-adding a file you
already have is automatically **skipped, not duplicated**.

---

## What changed in the November 2026 (round 3) update

- **Modifier field read correctly.** BCBS Alabama publishes place-of-service
  codes (11 office, 22 outpatient hospital, …) in the modifier field; read as
  modifiers, every Alabama rate failed "base modifiers only" (97110: 0 of 6,190
  practices). They are now moved to place of service. "52,53" is split into
  52 and 53. One rule for "base" everywhere: no modifier, or only GP/GO/GN.
  Rates already in your store are corrected once at startup (no re-download).
- **"Latest" means the payer's current rate**, even when you also filter by
  rate or modifier; the Entity view combines each practice at its own newest
  month and keys organizations on name + main state (a TIN named after a
  therapist is its own entity).
- **% of Medicare** uses the Medicare locality of the view's state (or the
  practice's state). Load each state's locality (Benchmark tab → Load MPFS);
  with no matching locality the percentage is not shown — it is never
  borrowed from another state.
- **Months**: a file dated in the future is filed under its real month; the
  Files tab shows each file's rates month and published month.
- **Sources**: every tested source lists its states; "Queue these states"
  queues just yours. `mrfx sources --check` re-checks the whole list live
  (read-only) — run it monthly.
- **Speed with names loaded**: once practice names arrive, the Explorer
  table took 158 s and the Code page 10 s on a 32M-rate store (a join DuckDB
  could only run as a nested loop); fixed — 1.2 s and 0.17 s on the same store.
- **Speed on a big store (0.4)**: "newest file per contract" is now stored
  with the rates instead of being worked out on every click. On a 99M-rate
  test store the Explorer's opening view went from "needs more memory" to
  about 1 second, the summary strip from 40 s to 5 s, the Code page from
  2.5 s to under 1 s, the Overview from 25 s to 10 s (then instant until new
  data lands). The first start after updating spends a few seconds marking
  the newest months (log: "marked each contract's newest month") — no long
  rebuild. Every number is the same as before. On the Code page the ranking
  now appears as soon as it is ready and the month-by-month table fills in
  after; clicking another code cancels the previous one.
- **Download list order (Files & imports)**: what is moving now first —
  downloading, then processing — then what needs you (too big, paused,
  failed), then done and skipped, and the queue last, in the order it will
  download. The files downloading right now always appear, even in a queue
  of thousands.
- **Long runs**: a download whose server stalls is **paused** (slot freed,
  bytes kept, automatic retry in 24 h, up to 5 times — `download_pause_hours`
  / `download_auto_resumes`); Resume stopped; the monthly refresh (if on)
  retries a not-yet-posted monthly file daily until the 10th; NPPES name file
  built in about a minute.

## What changed in the November 7, 2026 (round 6) update

- **Downloads never wait on a silent payer.** A file whose server goes quiet
  for 4 minutes is parked and the next file starts (see "When a payer's server
  stops answering" above).
- **Client packets and the monthly digest compare each payer's own newest file
  with that payer's previous one.** On real data one payer's late file used to
  make "this month" empty for everyone else; the packet now says which months
  it compared, payer by payer.
- **Much faster on big stores**, same numbers: client packets about 3×, the
  Clients tab digest about 2× (instant on a repeat visit), the outreach export
  about 10×, the visit calculator about 6×, the radius map and leverage
  summary 4-20×, and the Overview's quality panels instant on a repeat visit.
- **The prospect dossier compares a practice within its own state** (it used
  to pool every state you had loaded) and every position now shows how many
  peer practices stand behind it; the printed benchmark marks positions resting
  on fewer than 5 peers as *thin*.
- **Smaller honesty fixes:** steal-share counts only therapy practices as
  competitors and says CMS hides referral pairs under 11 patients; tax IDs
  that look personal are masked the same way on screen and in every export;
  the Leads list says when it stops at 1,000; payer concentration uses one
  rate per practice; notes on every report name exactly which rows were used.
- **The dashboard never shows a stale answer**: change a filter twice quickly
  and the slower, older reply can no longer paint over the newer one.

## What changed in the November 7, 2026 (round 7) update — version 0.7.0

Numbers that were wrong, now right (each checked by hand in a test):

- **"nan" / "Infinity" in a payer file** can no longer turn a practice's rate
  into "nan"; "2 x 34.50" is no longer read as $234.50.
- **The negotiation one-pager no longer adds payers together.** Each payer's
  figure counts all your units as if they were billed to that payer, so two
  payers worth $10,000 each are alternatives, not $20,000. The page now names
  the largest single-payer opportunity.
- **Trend lines follow the same contracts every month**: a payer that starts
  publishing no longer moves the line.
- **Leads, the leaderboard and the payer roster use the same "position" as
  the Benchmark.** A market where every practice is paid the same fee schedule
  is no longer a list of "p0 underpaid" leads.
- **Inflation:** a year entered under another index name (e.g. MEI) is never
  divided by a CPI-U year.
- **Referral leaders:** two therapists with the same name are two rows.
- **Census 65+ counts** are no longer double or triple counted.
- **Annual-units boxes** read "97110, 1,200" as 1,200 everywhere and say which
  lines they could not read.
- **Overview "Which payers pay above vs below market"** divides by the market
  median the page shows, and leaves out a payer's code when it prices fewer
  than 3 practices for it.
- **One rounding rule:** 112.5 % of Medicare prints as 113 % everywhere.
- **The pitch report's "In real terms" block** says it is the market's
  trajectory, not your client's own contracts.

Safety and install:

- **A web page you visit can no longer send files or commands to the
  dashboard** (it only answers its own page on this computer).
- **Install Python 3.13** (python.org's big button now gives 3.15, which some
  components don't support yet) and put the folder on a short local path like
  `C:\mrfx\`, not Documents/Desktop/OneDrive.
- **Double-clicking Start while the app is already running** now says so and
  stops, instead of retrying forever.
- **The updater** installs components before changing anything, refuses an
  older build, and puts everything back if a file is locked.

Speed and big stores:

- **Exports are built on the drive that holds your data**, once (not twice),
  and a drive that is too full gets a plain sentence instead of an error page.
- **Benchmark, negotiation, contract gaps, leads, leaderboard, fee schedule,
  digest and packets** reuse one pre-computed table per data refresh: on a
  99-million-rate store the benchmark went 3.3 s → 1.0 s, contract gaps
  3.0 s → 0.6 s, 5 client packets 21 s → 11 s. Same numbers to the cent.
- **A website you save** shows on the Leads list at once.
- **The dashboard notices new data**: after an ingest the payer and month
  pickers and the open Explorer / Code page refresh by themselves.
- **Rate changes ("What changed")** are remembered until the next data refresh.

Downloads and files:

- **A big file downloaded in several parallel pieces** (`download_segments`)
  can now take as long as it needs; the time cap no longer restarts it.
- **An index listing more than `max_toc_files` files** says so in amber on its
  row (and the count shows how many it listed).
- **A rate group whose provider list is only partly in the file** is counted
  on the Files tab and re-read when the payer's reference file arrives.
- **NPPES weekly updates** now rename/move providers in the practice
  directory too.
- **Hospital price files**: the CMS "wide" layout (one column per payer) and
  files with the CPT in a second code column now import correctly.
- **The official 2026 Medicare RVU zip from cms.gov now imports** (its title
  rows used to make it refuse); the non-QPP rates are used, and a conversion
  factor that doesn't match the file's own is refused.
- **"fee for service" / "fee_schedule"** spelled differently by a payer no
  longer drop rates.

## What changed in version 0.7.6 (review 12)

**New: what a visit is worth in a market.** On the **Rate card** tab, under
"One visit, by payer", there is a new box: **Visit value in this market**.
Pick a **State** and a **Discipline** above (both are needed — a visit's value
is a market figure, and a market is one state and one discipline), choose the
visit in the visit box (the default is one 15-minute unit each of 97110,
97530, 97140 and 97112), and press **Show visit value in this market**. For
every payer you see how many practices in that state it pays for the whole
visit, the **average (mean)**, the **median**, and the middle half (**P25** to
**P75**) of what one visit is worth to those practices — and the same over
**all payers**, where each practice counts once, at the middle of its own
payers' values (payers are never added together). Picking a practice is
optional; when you do, its own visit value with each payer is placed in that
payer's spread: its percentile, its quartile, and how many dollars and percent
it sits above or below the average and the median. **An example:** a
Missouri PT clinic paid $30 by Payer A on all four codes has a $120.00 visit
with A; if A's six Missouri practices are worth $80, $100, $120, $140, $160
and $180 a visit, the box shows 6 practices, average $130.00, median $130.00,
P25 $105.00, P75 $155.00, and the clinic at p40, second quartile, $10.00
(7.7%) below both the average and the median. A practice counts for a payer
only if that payer has a rate for **every** code of the visit (a half-priced
visit is left out, and the box says how many were); a payer with fewer than 5
practices is not shown. **Open printable** and **Download CSV** give the same
numbers, and the printable rate card and its CSV include the box when State
and Discipline are set.

The rest of this round fixed what an end-to-end check of the 0.7.5 zip found:

- **The current-rate chart in an NPI's drawer** now shows the same number as
  the Explorer and the Code page when a payer changed how it spelled the base
  modifier between months (it showed an average of the old and new rates).
- **A download from a server that sends an unusual "ETag"** (without quotes)
  no longer starts over after every dropped connection; and when a download
  is set aside, the message now says truthfully how much of it is kept.
- **`mrfx ingest` no longer waits forever** for a link the program was
  loading when it was last closed; that link goes back in the queue.
- **A monthly refresh of an index whose links carry a signature** (a long
  `?sig=…` part) now finds a republished file, and pressing **Clear queue**
  while an index is being checked now sticks.
- **Re-pasting a list of links** with fresh signatures now updates the links
  still waiting in the queue, so they no longer fail with an expired link.
- **The first monthly refresh after updating** no longer downloads every file
  again when nothing changed.
- **A Medicare fee schedule with a "nan" or a zero rate** is refused with the
  line number, instead of breaking every screen that shows % of Medicare.
- **A link that waited for the same file in the inbox** retries by itself if
  the program was closed before that file finished loading.
- **Two files of one payer for the same month** (for example two plan books,
  or a corrected file under a new name) are **pooled**: a practice priced in
  both gets the middle of the two rates. The Files tab and every methodology
  now say so, with how many practices are priced in more than one file. If a
  payer re-posts a corrected file under a new name, remove the old one on the
  Files tab.
- **The memory setting is now automatic** (`duckdb_memory_gb` is left out of
  `config/mrfx.yaml`, leaving room for the file readers). The settings the
  guides mention (`max_toc_files`, `delete_raw_after_ingest`,
  `monthly_refresh`, …) are now in `config/mrfx.yaml` with a `#` in front —
  remove the `#` to change one. Your own `config/mrfx.yaml` is kept by
  `mrfx update`; the new one arrives beside it as `mrfx.yaml.new`.
- **New clinics / closures within a radius** now list the nearest ones even
  when many newer ones exist far away.
- **A second copy of a file you already loaded** is deleted from the inbox
  (its original stays in `data/processed`), instead of being kept twice.
- **After an update that has to adjust stored rates**, the dashboard opens
  within about 30 seconds and the rest of the adjustment continues behind it;
  the Files tab shows how far it is.
- Smaller things: the Markets state list no longer shows military mail codes
  as states; `mrfx status` prints its memory line once; a big file dropped in
  the inbox shows "reading the file once …, N of M MB" while it is first
  read; stopping the server on Mac/Linux no longer prints a "leaked
  semaphore" warning; Mass General Brigham Health Plan's address in the
  catalog was updated.

## What changed in version 0.7.5 (review 11)

This round went over how links are downloaded, how files are loaded and when
a file is skipped — with real files, a deliberately badly behaved test
server and the program killed at random moments. What you would have seen
before, and what happens now:

- **A file dropped in twice no longer counts twice.** The browser's second
  download (`rates (1).json.gz`), a copy, or a file you downloaded in your
  browser after its link had already loaded, used to be loaded again — every
  contract line was then held twice and every count that adds files up was
  inflated. The same bytes under a second name are now recognised and shown
  as **skipped — "identical to …, which is already loaded"**. The upload
  button says the same right away.
- **A capped index now loads the rest.** An index with more files than
  `max_toc_files` says so; raise the setting and press **retry** on that
  index's row — the button is there now — and the files past the cap are
  queued (files already loaded are not downloaded again).
- **An over-size file re-queues when the limit is raised.** Raising
  `confirm_over_gb` and restarting now picks up the "too big" rows whose size
  is known; "download anyway" still works per row.
- **"Clear queue" sticks.** Pressed while an index was being read, the index
  used to finish, queue all its files and load them anyway.
- **"Keep both" can no longer get stuck.** If the program stopped before a
  file you chose to keep was loaded, it sat "queued" for good and could not be
  removed. It now loads at the next start or scan, and such a row can be
  removed.
- **A resumed download is checked.** When a download is interrupted and
  resumed, the server's answer is now compared with what was asked; if the
  insurer replaced the file in the meantime, the download starts over instead
  of joining two versions into one file. (One case no program can detect: a
  server that gives no version information AND replaces a file with a
  different one of exactly the same size between two connections — the app
  then checks a sample of the beginning, where the file's date lives.)
- **A re-checked link asks the server first.** The monthly refresh (and a
  provider list arriving later) used to reuse a kept download or skip a
  loaded file without asking whether the insurer had published a new version
  under the same address. It now asks once; unchanged files are not
  downloaded again, changed ones are.
- **Plain sentences instead of program errors.** A link that redirects in a
  loop, a damaged or cut-off download, an empty file, a file saved as UTF-16,
  a parser that ran out of memory twice, an insurer's "come back later"
  answer, and a file with none of your billing codes now each say what
  happened and what to do. A dashboard question that coincides with a file
  finishing is asked again instead of failing.
- **Small files no longer wait behind big ones to start downloading**, and stopping downloads
  during a retry pause frees the slot at once. A program that is closed hard
  no longer leaves its helper processes running in the background.
- **Less reading of each file**: a file dropped in the inbox is read twice
  instead of two and a half times; outreach CSVs no longer get a blank line
  between rows in Excel on Windows.
- **Settings:** `mode: off` for name lookups works as written (it used to stop
  every command with a config error unless written `"off"`).

Nothing in this round changes a number: three real insurer files load to
exactly the same rows as in 0.7.4.

## What changed in version 0.7.4 (review 10)

**Searching is faster.** Typing in the Explorer's search box now starts the
search as soon as you pause (it waited over half a second before), and each
new letter cancels the search before it — the old ones no longer queue up
behind each other, so the answer arrives sooner and is always for what is in
the box. While it loads, the previous rows stay on screen, dimmed, instead of
the table going blank. A name needs at least two letters. The first screen
after a start shows up without waiting for the header counts, the summary
strip is remembered across restarts, the Entity view pages and sorts in
about a third of the time, and the NPI view now answers a code on its own on
a mid-size database instead of refusing. "Stats are busy — retrying…" no
longer appears for the search you meant. Typing an NPI in the TIN or Entity
view says which tax ID it bills under; an NPI typed into a practice box
suggests its practice. `%` and `_` in a search are plain characters.

**The Explorer shows the same numbers as every other tab.** Its Month box now
starts on **latest — each contract's newest file** (the basis Benchmark,
Markets and Leads use); **all months (pooled)** is still there.

**Numbers that were wrong.**
- The market report's city table lists only the state's own cities (a practice
  with one clinician across the border put that city in the report).
- Markets with a State picked ranks every state on its own practices and
  highlights the picked one (other states were computed from the picked
  state's practices).
- Rate changes for a picked month compare each insurer's file with that
  insurer's own previous file, list any insurer that could not be compared,
  and say "nothing was compared" instead of "No changes" when that is the case.
  Each row now shows its claim type and place of service.
- The rate card's best-paying insurer is judged on the codes the insurers
  both price (a Medicare anchor could crown an insurer that pays less on the
  same code).
- Monthly packets include "Market position" again (it was always left out).
- The row details chart, the Code page's month-by-month table and the
  overview use the same basis as everything else (newest file, practice
  claims, base rates); the overview's "by discipline" block compares one
  service — the moderate evaluation — for each discipline.
- Leads, the outreach file and Benchmark use one market median: the median
  of the OTHER practices (Leads included the practice itself — $47.50 on
  Leads against $48.00 on Benchmark). Positions are unchanged; "average
  below median" and the worst-payer gap can each move by a few dollars.
- Leads flags a practice that an insurer's newest file no longer lists (its
  rate there is from an older file).
- New-to-network means absent from every earlier file of that insurer.
- Printed documents fit their columns on a landscape page.
- Half-cents are rounded the same way everywhere ($70.70 on one screen and
  $70.69 on another).
- "States with data" counts states only (foreign provinces and spelled-out
  names are folded or dropped).
- Weighted position, the hospital multiple, the floors percentages and the
  referral wait are rounded once, at the end.

**Clearer wording.** The dossier and market report state their month and
market; Leads' "underpaid most by" and the dossier's "weakest payer" say
which measure they are; the Sources tab shows the links the Files tab has
tested; freshness ignores empty files and says "dated in the future"; the
Code page has a claim-type box (practices vs facilities); a client name that
matches no practice is refused when you add it; the Leads CSV keeps its old
column order (new columns are at the end) and money in CSVs has two decimals.

**Positions already quoted may move.** Because Leads now measures the gap to
the median of the other practices, a Leads "average below median" figure
and its worst-payer gap can differ from 0.7.3 by a few dollars; positions
(the p-numbers) do not change.

## What changed in version 0.7.3 (review 9)

**Starting the program.** The window now says what it is doing from the first
second: `opening the store at …`, the one-time update if there is one (with
progress), and finally `the dashboard is ready — open http://localhost:8377`.
The check of your stored files at start-up only reads files that are new or
changed since the last start, so a big store opens in about the same time
however many files it holds. If one stored file can't be read, the others are
still updated and the window says which one was left.

**Dropping or uploading a file again.** The program now recognises a file it
already has by its contents, not just its name or size:
- the *same* file again is reported as already loaded and is not read twice
  (before, a 2–3 MB file could be loaded twice and every number became the
  median of the two copies);
- a *different* file under a loaded name, for the same insurer and month, is
  held with two buttons on the Files tab — **Replace** or **Keep both** —
  instead of being blended in silently;
- a file that changes while it is being read is not recorded as loaded; the
  next scan offers the new version.

**Provider lists follow their month.** A rate file now uses the provider list
the insurer published for the *same* month (or the newest one when it can't
tell). Before, a June rate file could pick up March's list. A rate file that
loaded from a link before its provider list arrived is read again (or
downloaded again, once) when the list lands; until then its note says
*waiting for this payer's provider reference file*. Very large provider lists
are stored correctly on every path, and files that rely on a provider list no
longer use memory in proportion to their size.

**Numbers you sell.**
- A report pinned to a month ("as of 2023-06") no longer shows rate changes
  from after that month.
- The "cut streak" counts only real month-to-month cuts on the same
  practices and codes.
- A practice's overall position is the same number on Leads, the
  leaderboard, the payer roster, Negotiate, the one-pager and the dossier.
  (Correction: the size-premium panel still printed a different figure — its
  within-payer band average — until 0.7.4, which shows the overall position
  there too.) Leads' "worst payer" compares payers on the codes they both
  price, so Leads can name a different worst payer than 0.7.2 did.
- Positions already quoted can move by a point (the payer roster now starts
  from the same p50 rule), the leaderboard's code count counts the codes in
  the position, sparkline percentages round half-up, and the negotiability
  list can come back in a different order.
- The Rate changes tab's State box filters "Find changes" too, not only the
  trajectory.
- A practice name typed in part ("ACME"), a practice outside the market's
  state, and a mapped group are treated the same way on every screen; the
  dossier is titled with the group's own name.
- Report footers describe the market in words, percentages never read
  "-0.0%", and the outreach file explains its own columns.

**Faster.** The Rate changes tab's trajectory is remembered until new data
arrives (instant on the second click) and has a State box; the outreach
export is about twice as fast; the medians behind Benchmark and Leads are kept
across restarts instead of being rebuilt each time.

**Downloads.** Each file's body is downloaded once (a big file found by a
small-file slot keeps downloading instead of being started again; since
0.7.5 a small-file slot may first ask for 1 byte to learn the file's size),
so a queue
of big files finishes sooner and asks the insurer's server for less.

**Safer input.** A wrong value sent to the dashboard gets a plain sentence
back, never a database error; very long file names are refused with a
sentence.

## Version 0.7.2 — start-up fix

0.7.1 updated the stored rows of every file once, at the first start, before
the dashboard opened — and printed nothing while it worked. On a big store it
sat after the "analytics memory budget" line for a long time, which looked
like a hang. 0.7.2 does the same update in about a second for most stores
(measured: 2,000 files, 94 s → 0.9 s), only rewrites the files whose rows
actually change, and prints what it is doing:
`one-time update of N stored file(s)…`, a progress line every 15 seconds,
and `stored rows are up to date…` when it is done.

## What changed in the November 2026 (audit 8) update — version 0.7.1

Downloads:

- **Copies of a file you already have are recognised early — and only when
  that is proven.** After the first 15 MB (`early_dedup_mb`), a download whose
  size, first 15 MB, last 1 MB and 8 samples in between match a loaded file is
  stopped and marked a duplicate. Only compressed files (.gz / .zip) are ever
  skipped this way: their last bytes carry a checksum of the whole content. A
  plain .json is always downloaded in full (two plain files can match at both
  ends and still differ in the middle).
- **A copy of a file that is downloading right now "waits"** (grey badge,
  counted separately) instead of downloading twice, and is checked when the
  other finishes.
- **Size lanes**: up to 3 download slots take files of 100 MB or less first
  (`small_file_downloads`, `small_file_mb`); the line above the download list
  shows the current numbers.

Files:

- **A rate file whose provider list is IN the file is never matched to
  another file's provider list.** If a payer also had a separate provider
  list, rates could attach to the wrong practices on three of the four ways a
  file is read. Two provider lists from the same month that disagree are no
  longer guessed between (the Files tab says so).
- **A big standalone provider list (over 1 MB)** is stored as a provider list
  instead of a rate file with 0 rows.
- **A file dropped in (or uploaded) under a name that is already loaded** is
  no longer silently ignored: the same file is reported as already loaded; a
  different one is loaded under a new name.
- Rows stored by older versions with "fee_schedule" spelled that way now count
  as dollar rates (fixed once, at the first start). Files that older versions
  may have trimmed say **re-ingest suggested** on the Files tab.

Numbers:

- **Payer trajectory / "In real terms"**: measured on the same practices and
  codes in both months. A payer adding a cheaper code no longer reads as a cut.
- **A practice with offices in two states** has one rate on every page
  (Benchmark, Negotiate, rate card): the market's state filter picks the
  peers, not which of the practice's own rates count.
- **Leads' "worst payer"**, the leaderboard and the size premium use the same
  position and the same codes as the Benchmark.
- **Per-visit value** compares payers on the codes they all price.
- **Outreach merge fields** (C97110_RATE, _PCTL, …) are on the same basis as
  the Benchmark (latest month, base modifiers, professional) with the same
  position rule; the methodology file defines each column.
- **Rounding**: every printed number rounds exactly as the database does
  (45.125 → 45.13 everywhere).
- **Sparklines** stop at a report's as-of month; **hospital parity** takes one
  rate per hospital first; the **market report** states its as-of month,
  basis and source files; the **client digest** follows entity-map edits.

## Debug FAQ — what it says, what it means, what to do

Every message the app shows is designed to tell you the fix. This FAQ collects
them in one place, grouped by where you'll hit them.

### Setup problems

- **`Failed to build installable wheels for some pyproject.toml based
  projects` (duckdb, pyarrow, watchfiles, pydantic-core)** — your Python is
  newer than these packages ship ready-made installers for (anything past
  3.14), or `requirements.txt` is an old copy, so pip tried to compile them
  from source and failed. **Don't install a compiler — use Python 3.11–3.14.**
  On Windows: `py -0p` lists what's installed; delete the half-made `.venv`
  folder, then run the Step-3 sequence starting with e.g.
  `py -3.13 -m venv .venv`.
- **`mrfx: command not found`** — the virtual environment isn't active. Re-run
  the `activate` line from Step 3 (you'll see `(.venv)` in the prompt).
- **`python3: command not found` on Windows** — use `python` instead.
- **Port 8377 already in use** — change `port:` in `config/mrfx.yaml` (e.g.
  8400) and restart `mrfx serve`.
- **`config problem: …`** — a typo in `config/mrfx.yaml` (bad YAML, a
  non-number port, an unknown `enrichment.mode`, an out-of-range value). The
  message names the exact setting; fix that line and re-run. Deleting the
  file entirely runs with safe defaults. Misspelled setting NAMES don't stop
  the app — they're ignored with a "unknown setting(s)" warning, so check the
  startup log if a change seems to have no effect.
- **`a server on port … answered, but it doesn't look like the mrfx
  dashboard`** — some other program is using that port. Stop it, or change
  `port:` in `config/mrfx.yaml` and re-run.

### Pasted links: statuses you'll see and what to do

- **`HTTP 403 — access refused. Usual causes: a signed URL expired…`** —
  Blue plans publish *signed* links that die after days. Don't paste file
  links from those hosts; paste the payer's **index/TOC or page link** (the
  starter list has them) — the app fetches fresh signed links itself.
- **`This link is a web page, not a data file, and no file links could be
  found on it`** — the page builds its list with JavaScript. The app reads
  those pages with a headless browser it downloads for itself the first time
  it needs it, so usually just press **retry** on the row. If the message says
  it *tried to download the browser but couldn't* (you were offline, or the
  disk was full), get back online and press retry — or pre-install it yourself
  in your venv with `playwright install chromium`. Still nothing after that?
  The page needs multi-step human clicks — open it in your browser, right-click
  the real `.json/.json.gz/.zip` links, Copy Link Address, and paste those.
- **`file is X GB — larger than the confirm_over_gb safety limit`** — a
  legitimately huge file. If you want just this one, press **"download
  anyway"** on its row. If you want all of them: raise `confirm_over_gb:` in
  `config/mrfx.yaml` (e.g. `12`) and restart — every stopped file that now
  fits the new limit re-queues **automatically**, and kept partial downloads
  resume rather than restart.
- **`not enough free disk space for this file`** — the file needs more room
  than you have. The queue doesn't fail on this: it **pauses and re-checks
  every minute**, so just free space (empty `data/processed/`, old downloads)
  and it resumes by itself.
- **`connection closed early (X of Y MB) — retrying from where it stopped`**
  — a flaky network or server. The app retries and resumes automatically; if
  it ultimately fails, press retry later — it continues from the same byte.
- **`this TOC lists only out-of-network allowed-amounts files`** (skipped) —
  that payer publishes no negotiated rates at this link (e.g. Excellus).
  Correct behavior; nothing to fix.
- **`identical to a file already ingested … skipped as duplicate`** — Blue
  plans host copies of each other's national files. Your data already has
  it; nothing was lost. When it says **`same size, same first 15 MB and same
  last 1 MB — skipped after N MB`**, the copy was recognised early and the
  rest of it was never downloaded.
- **`out-of-network allowed-amounts file (no negotiated rates) — skipped`** —
  the link was a billed-charges report, not a rate file. Normal.
- **`scanned: none of the target billing codes appear in this file`** — the
  file is real but contains none of your CPT codes (payers slice files by
  specialty). Normal — the queue moves on. If you expected codes, check
  `codes.cpt_codes` in `config/mrfx.yaml`.
- **A row shows 0 rows but says `done`** — same reason as above, or the
  file's slice has no therapy codes. The Files tab QA panel shows what WAS
  in the file.
- **The payer name doesn't match the state I pasted** — normal and correct.
  Blue plans host copies of each other's national files (an Idaho index can
  carry an Arkansas book); the app attributes every file by the payer named
  INSIDE it, which is the truthful label.
- **`HTTP 404 — file not found`** — monthly links go stale. If it's a dated
  URL early in the month, the payer may not have posted yet — retry in a few
  days (the app already tries the previous month automatically). Otherwise
  re-open the payer's page from the Sources tab for the current link.
- **`HTTP 403 — access refused` even as a browser** — a few sites (UHS)
  firewall all automation. Download the file in your browser and drop it
  into `data/inbox/` — the app takes it from there.
- **`unexpected worker error`** — a genuine bug or a shape the app has never
  seen. The row's error text and `mrfx serve`'s terminal output have the
  details; press retry once, and if it repeats, keep that URL aside and
  report it (see the AI handoff doc — an assistant can debug from exactly
  that output).

### Files you dropped into the inbox

- **`This file uses provider references. You must also drop in the matching
  provider-reference file`** — the payer splits NPIs into a companion file.
  Find it next to the rate file on the payer's page (usually named
  `provider-reference` or similar), drop it in the inbox too, and the app
  re-processes automatically.
- **`File is not a zip file` / `unreadable`** — the download is corrupt or
  isn't really an MRF. Re-download it; if it persists, the payer's file is
  bad (it happens) — note it and move on.
- **Stuck at `processing` after a crash/restart** — the app now flips those
  to `failed` on startup and re-ingests on the next scan automatically. If
  you see one frozen while the app is running, that file is genuinely being
  parsed (big files take minutes to hours — the progress bar shows chunks).
- **`interrupted by a restart — will re-ingest on the next scan`** — exactly
  what it says; no action needed.

### The queue and long runs

- **The dashboard is completely EMPTY after an update or restart** — your data
  is almost certainly fine; the app most likely opened a **brand-new store in
  the wrong folder** because the terminal wasn't in the project folder when
  you ran `mrfx serve`. Check the startup text: the `store:` line names the
  exact folder it opened and how many files are in it — if it isn't
  `E:\Claudevideo\data\mrfx_store` (or says `0 file(s)` when you know you have
  a book), close it, `cd E:\Claudevideo`, and start again. Newer builds also
  find the project's config automatically no matter where you start from, so
  this mostly can't happen anymore. (If the store path is right and it's still
  empty, check that `data\mrfx_store` still exists and has large files — and
  restore your `data\` backup if you replaced the whole project folder during
  an update instead of just the `mrfx` folder.)
- **I pressed Ctrl-C — is my work lost?** No. Restart `mrfx serve` (or re-run
  `mrfx add`): downloads resume mid-file, interrupted files re-queue, and
  nothing is double-ingested.
- **A file's chunk progress hasn't moved for a long time** — two different
  things look identical here, and newer builds now tell you which in the log:
  (1) **The dashboard bar is frozen but the parse is fine.** While a big
  analytics rebuild runs, progress-bar updates are skipped on purpose (they'd
  otherwise wait on the same lock) — the bar catches up when the rebuild
  finishes. (2) **The parse itself has gone quiet.** The log now prints
  `no parse progress for N minutes (stuck at chunk X)` every 15 minutes when
  that happens. Some quiet is legitimate (one enormous section inside the
  file, a saturated disk — check Task Manager → Performance → Disk). If it
  repeats for hours *and* the disk shows no activity, press Ctrl-C and start
  again: the file re-queues, downloads resume mid-file, and nothing is lost.
- **The app is frozen and you want to know why (or report it)** — three
  channels, in order of preference: (1) open
  `http://localhost:8377/api/debug/stacks` in the browser — it answers even
  when the rest of the dashboard is jammed; (2) if the browser can't reach
  the app at all, open `data\mrfx_store\diagnostics\stacks_latest.txt` in
  Notepad — the server rewrites it every 30 seconds with the same diagnosis,
  and the timestamp on its first line tells you whether the process is even
  alive; (3) if both the counter and the extracted-MB number freeze for ~30
  minutes, the log prints the full diagnosis by itself. Paste whichever you
  got into a bug report — it names the exact line every part of the program
  is sitting on.
- **Log says `rollup … partition 15/15` and then goes quiet for minutes** —
  normal, not stuck. Each partition line prints *before* that slice runs, and
  after the last one the whole result is written into the database file — the
  slowest disk step of the cycle, several minutes on a hard drive. Newer
  builds print `writing N table(s) to the database file … not stuck` and then
  `rollup rebuild finished in Ns` so you can see it working; if you're unsure,
  Task Manager → Performance → your data disk will show heavy activity while
  it writes. Only worry if there's total silence *and* zero disk activity for
  15+ minutes.
- **The queue is huge and slow** — expected for whole-payer grinds (UHC and
  Anthem queue thousands of files; parsing runs ~30s per uncompressed GB).
  The app already uses (your cores − 1) parsers automatically (fewer if RAM is tight)
  (`parallel_ingests: 0`); on a big multi-core machine that's the fastest
  safe setting, so just leave it running overnight and skip rows you don't
  need. (If the startup log warns that ijson is running its "pure-Python
  backend", that makes parsing ~10× slower — reinstall as the log suggests.)
- **Rates look doubled for one payer** — check the Files tab for two ingests
  of the same book under different filenames from before the dedup fix; if
  so, `mrfx reset --confirm` and re-queue (dedup now catches mirrors even in
  parallel).
- **First start after updating the app feels slow** — expected, once: the
  log prints "full analytics rebuild: the slow whole-store pass (one-time
  after an upgrade…)". Let it finish (downloads and extraction keep running,
  just slower while they share the disk). Every update after that is
  payer-sized and fast. Related warning: after upgrading, do NOT run an
  older copy of the app against the same data folder — the app will detect
  it and redo the full pass on the next start to keep the numbers right.
- **Dashboard numbers aren't updating during a big run** — they catch up in
  batches, and each catch-up now recomputes only the slice for the payers
  that just landed (log line: "analytics update … payer slice, not a full
  rebuild"), which usually takes minutes even on a huge store. Mid-grind the
  refresh runs at most once per a self-chosen interval (about 4× however
  long the last one took — the log prints "next mid-grind refresh in ~N
  min"), and once more when the queue finishes. In between, the disk belongs
  to downloading and extraction; the Files tab still updates live. If numbers
  stay stale long past the printed schedule, look at the `mrfx serve` terminal
  window: a "rollup rebuild failed" line there usually means low disk — free
  some space; the raw data is safe and analytics catch up on the next
  successful rebuild.
- **I saw an "Out of Memory" line and the rebuild slowed to a crawl** — on a
  very large store (tens of millions of rate rows) the analytics rebuild can hit
  its memory cap. It recovers on its own: it caps how many CPU cores it uses for
  the rebuild and sizes each pass to fit, and if a pass still doesn't fit it
  retries in progressively smaller slices — so it always finishes, just more
  slowly, and your data is never at risk. If your machine has plenty of spare
  RAM and you want it faster, raise the cap in `config/mrfx.yaml`, e.g.
  `duckdb_memory_gb: 8` on a 16 GB box, and restart. (Leave it unset to let the
  app pick a kernel-safe default automatically.)
- **The whole machine ran out of disk mid-grind** — deletes still work:
  clear `data/processed/`, old exports, anything large; the app's guards
  keep 2 GB headroom and cap its own temp usage, and every interrupted
  piece resumes.
- **The database itself is bigger than I want** — remove the payer files you
  don't need: Files tab → **remove** button on any row (or
  `mrfx forget <filename>`). It reports the space freed, the dashboards
  update, and re-adding the link later brings the data back.
- **`this file is being processed right now` / `…link is being retried right
  now`** when removing — a file can't be erased mid-parse or mid-retry (the
  running work would quietly bring it back). Wait for the row to reach
  `done` or `failed`, then remove it. Similarly, `mrfx forget` and
  `mrfx reset` refuse while the dashboard is running — use the Files tab's
  remove button instead, or stop the server first.

### Provider names / identification

- **Names are identifying very slowly — the banner crawls up a few thousand a
  day, or is stuck at a few thousand.** That means the app is looking providers
  up one at a time over the internet instead of using the local NPPES file it
  normally downloads by itself (see **"Provider names fill in automatically"**
  above). Check the `mrfx serve` window for a line starting `NPPES:` — it says
  why: no internet / CMS unreachable (it retries within the hour), a download
  that stopped (it resumes), or `auto_download: false` in `config/mrfx.yaml`
  (then point `bulk_csv_path` at a file you downloaded). If you set
  `bulk_csv_path` yourself and it's still slow, the path is almost certainly
  wrong — the window logs a warning naming the missing file. Fix the path (mind
  the Windows quotes/slashes) and restart.
- **The banner never reaches zero.** A handful of NPIs in payer files are
  deactivated or malformed and simply aren't in NPPES — those are marked
  "identified" (no name found) so the count settles just short of 100% instead
  of hanging forever. That's expected, not a stall.
- **I switched to bulk mode but nothing changed / it says the file is too
  small.** You likely pointed it at an NPPES *weekly* update, not the *full
  monthly* file. The app refuses to trust a partial file (it would wrongly
  blank real providers) and keeps everyone's names. Download the full monthly
  `.zip` from <https://download.cms.gov/nppes/NPI_Files.html> and point
  `bulk_csv_path` at that. Also check the path is right — on Windows keep it in
  single quotes or use forward slashes.

### Getting help

- `mrfx status` summarizes everything the store knows.
- Each failed row's error text is written to be actionable — read it first.
- Closed the server window before reading its last message? Started with
  `Start MRF Explorer (auto-restart).bat`, everything it showed is also saved
  in `data/mrfx_store/diagnostics/serve.log` (the newest lines are at the end).
- `mrfx --version` prints which version you are running.
- For anything beyond this FAQ, hand `docs/AI_HANDOFF.md` plus the row's
  error text to an AI assistant — that document tells it exactly how this
  codebase works and how to debug it safely.
