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

## Step 1 — Install Python 3.12 (one time)

**Use Python 3.12** — not the newest release. The packages this app needs
(duckdb, pyarrow, and friends) ship ready-made installers for 3.11/3.12 but
often lag on brand-new Python versions, and without a ready-made installer the
setup tries to *compile* them and fails. 3.12 is the sweet spot: fully
supported everywhere, nothing to compile.

- **Windows**: install from
  <https://www.python.org/downloads/release/python-3120/> — scroll to
  "Files" and get **Windows installer (64-bit)**. **On the first installer
  screen, tick "Add python.exe to PATH"** before clicking Install. You can
  have 3.12 installed alongside a newer Python; Step 3 picks 3.12 explicitly
  with `py -3.12`.
- **macOS**: install 3.12 from <https://www.python.org/downloads/> — or, with
  Homebrew, `brew install python@3.12`.
- **Linux (Debian/Ubuntu)**: `sudo apt install python3.12 python3.12-venv python3-pip`
  (or your distro's 3.12 package).

Already have 3.11 or 3.12? You're set — skip to Step 2. (Check with
`python --version`, or on Windows `py -0p` to list every version installed.)

---

## Step 2 — Get the code

**Option A — you have the zip I sent** (`mrfx.zip`): double-click to unzip it,
which gives you a folder (e.g. `Claudevideo`). Move it somewhere easy like your
home folder or Desktop.

**Option B — download with git** (if you use GitHub):

```
git clone --branch claude/bcbs-mo-mrf-pipeline-o0qwq4 <your-repo-url>
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

**Windows (PowerShell):** — build the environment with **Python 3.12
specifically** (see the note below on why):
```
py -0p
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python --version
python -m pip install -r requirements.txt
python -m pip install -e .
```
The first line (`py -0p`) lists the Python versions installed on your machine
and their paths — you should see a `3.12` entry. If you don't, install Python
3.12 from <https://www.python.org/downloads/release/python-3120/> (tick "Add
python.exe to PATH"), then re-run the lines above. `python --version` should
print `Python 3.12.x` once the environment is active.

> **Why 3.12 and not the newest Python?** This is the single most common
> install snag. If you see **`Failed to build installable wheels for some
> pyproject.toml based projects` (duckdb, pyarrow, watchfiles, pydantic-core)**,
> it means your Python is *newer* than those packages ship ready-made
> installers for, so pip tried to *compile* them from source — which needs a
> C/Rust build toolchain you don't have. The fix is not to install a compiler;
> it's to use Python **3.12**, which has ready-made installers for everything
> here. Delete the half-made `.venv` folder if one was created, then run the
> `py -3.12 -m venv .venv` sequence above.

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
(`queued` → `downloading` with a MB progress bar → `ingesting` → `done` with a
row count; index links show `expanding` while their file lists unpack; a file
bigger than the safety limit shows `too big` — see the disk section below).
Files process **a few at a time in the background** — by default as many as
your computer has processor cores minus one (the `parallel_ingests:` knob in
`config/mrfx.yaml`, `0` = automatic), and **several files download at once**
to keep those workers fed (`parallel_downloads:`, `0` = automatic, capped at
4 to stay polite to payer servers; simultaneous downloads coordinate so they
can never fill your disk together) — you can paste a whole
state's worth of links and walk away. Each file's download is deleted
after its rates are extracted, so your disk doesn't fill up. If you close the
app mid-download, it picks up where it left off on restart.

Not every link is a rate file: **allowed-amounts** files (out-of-network
billed/allowed averages) contain no negotiated rates, and the app tells you so
and skips them rather than loading junk. The app also spots **duplicates**:
Blue plans publish copies of each other's national files, so when a link
downloads to the exact same bytes as a file another link already loaded, it's
skipped with a note saying which link it duplicates — your database stays
clean even if you paste every state's index. (Duplicate detection compares
link downloads to each other; files you copy into `data/inbox/` by hand
aren't part of that comparison.)

**Don't want to hunt for links at all?** The app ships ready to go:

- **`config/starter_links.txt`** — every payer source verified in testing,
  one per line, ready to load in a single command:

  ```
  mrfx add --file config/starter_links.txt
  ```

  That queues 38 sources (Highmark's 15 hosted Blue plans, BCBS Mississippi,
  BCBS Tennessee, Cigna, BCBS South and North Carolina, CareFirst, Molina,
  Kaiser, Aetna, Harvard Pilgrim, Regence, Oscar, the 14-state Anthem
  master index,
  Centene/Ambetter's all-states page, the Blue KC / BCBS Michigan /
  BCBS Louisiana hubs, UnitedHealthcare's national portal, SelectHealth
  (Utah/Idaho/Nevada), Moda Health (Oregon/Alaska/Texas), the First
  Health PPO rental network, Tufts Health Public Plans (Massachusetts),
  and Security Health Plan (Wisconsin) —
  Molina/Kaiser/Aetna/Harvard Pilgrim/Regence/Oscar need the headless
  browser, which the app now downloads for itself on first use (the
  starter-links FILE swaps Oscar
  for a browser-free monthly index), and Anthem needs confirm_over_gb: 12).
  Each one
  expands on its own — expect thousands of files to queue and let it run.
  The same file lists the remaining browser-only portals (Humana, HCSC, Premera…)
  with instructions in the comments. Click **"Show tested
sources"** on the Files tab to browse it, or **"Queue tested payer indexes"**
to load them all — monthly-dated links are refreshed to the current month
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
  an as-of month: it defaults to **Latest available** (every payer at its
  newest rates); the dropdown is only for pinning a historical snapshot.
- **Export**: the **Export CSV** / **Export + methodology** buttons download the
  current filtered view. The **Outreach CSV** button gives one row per practice
  with name + address + phone + per-code rate/percentile columns — ready to
  cross-reference your contact list and mail-merge (e.g. in Brevo).

---

## Make provider names fill in fast (recommended)

The rates load with raw NPI numbers first, then the app fills in each
provider's **name, city/state, and taxonomy** in the background. You'll see a
banner like *"5,192 / 854,692 names (identifying 849,492 more…)"* while it
works.

Out of the box that runs in **`api` mode** — it looks each NPI up one at a
time over the internet against the public NPPES service, which is heavily
rate-limited. For a big book (hundreds of thousands of providers) that can
take **days**. If your banner is climbing only a few thousand per day, this is
why.

**The fast way — point it at the NPPES bulk file (one local pass, minutes):**

1. Download the **NPPES full monthly file** (a ~1 GB `.zip`) from
   <https://download.cms.gov/nppes/NPI_Files.html>. Keep it as the `.zip` — you
   do **not** need to unzip it.
2. In `config/mrfx.yaml`, set `bulk_csv_path` under the `enrichment:` block:

   ```yaml
   enrichment:
     mode: bulk
     bulk_csv_path: 'E:\NPPES_Data_Dissemination_July_2026_V2.zip'
   ```

   Use the real path to *your* download. On Windows, **keep the single quotes**
   around the path (or write it with forward slashes,
   `E:/NPPES_Data_Dissemination_July_2026_V2.zip`) so the backslashes are read
   correctly.

   > **You only really need the `bulk_csv_path` line.** Whenever that file is
   > present the app uses it automatically — even if `mode` is left at the
   > default `api`. Setting `mode: bulk` is just the explicit form. (So if names
   > were "stuck", the usual cause is a `bulk_csv_path` that's misspelled or
   > points at a file that isn't there — the app can't use a file it can't find,
   > and falls back to the slow API. Double-check the path.)
3. Restart `mrfx serve`.

The first time it runs, it reads the file **once** (a few minutes) and builds a
small local lookup cache (`nppes_cache.parquet` in your store folder). After
that, every provider — the whole backlog at once, and anything new you ingest
later — is identified in seconds, with no internet lookups.

**Already have a backlog?** You have two options:

- **Easiest:** just set `bulk_csv_path` (step 2 above) and restart `mrfx serve`
  — the running app picks the file up on its own and clears the backlog in one
  pass. Nothing else to run.
- **Or run it as a one-off command** — stop the dashboard first (Ctrl-C in its
  window; while it runs it keeps the database open, so this command will
  politely refuse), then:

  ```
  mrfx enrich --bulk "E:\NPPES_Data_Dissemination_July_2026_V2.zip"
  ```

  It prints `identified N name(s)` when done; start the dashboard again after.

> The app only trusts the **full monthly** file for this (it's ~8–9 million
> providers). If you accidentally point it at a small *weekly* update, it
> notices the file is too small, keeps everyone's names instead of blanking
> them, and leaves the banner honestly above zero until you swap in the full
> file.

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

Parsing is the slow part (roughly 30 seconds per uncompressed GB *per worker*,
and big payer files are 10–200 GB uncompressed), so the wall-clock for a long
queue is basically `total uncompressed GB ÷ workers`. Here's what actually
moves that number, in order of impact:

1. **Use more of your CPU (if you have it).** Auto mode uses your core count
   minus one, but stops at **8** workers to stay safe on RAM. If Task Manager →
   Performance → CPU shows more logical processors than the banner is using
   AND you have plenty of memory, set it explicitly in `config/mrfx.yaml`:

   ```yaml
   parallel_ingests: 11   # e.g. on a 12-core machine
   ```

   Rule of thumb: **allow ~2 GB of RAM per worker** and leave a few GB for the
   database and Windows itself. On a 16 GB machine, don't go past 5–6; on
   32 GB, 10–12 is fine. (If you've ever seen an out-of-memory error on this
   machine, stay at the auto setting.) Values above your core count are
   clamped — extra processes past the cores only fight each other.

2. **More simultaneous downloads if the parsers are starving.** On the Files
   tab, if parsers sit idle while links crawl (common with Blue-plan CDNs that
   stall and back off), raise:

   ```yaml
   parallel_downloads: 6   # max 8
   ```

3. **Don't ingest what you don't need.** The single biggest cost is all-codes
   mega-files. The app already skips files that contain none of your CPT codes
   (it detects that on a fast first pass and stops), and re-adding a file you
   already have costs nothing (skipped by content). But when a payer offers
   both one national everything-file and smaller per-plan/per-state files,
   paste the smaller ones for the states you actually work in.

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
| `mrfx add --file config/starter_links.txt` | Same, from the editable ready-to-go list |
| `mrfx add --retry-failed` | Re-queue every link that previously failed |
| `mrfx preflight <path>` | Inspect a file before ingesting |
| `mrfx ingest [path]` | Ingest a file or the whole inbox (shows a progress bar) |
| `mrfx status` | List ingested files and totals |
| `mrfx export out.csv --cpt 97110 --payer "Aetna"` | Export a filtered CSV + methodology sidecar (state filtering lives in the dashboard and `mrfx outreach`) |
| `mrfx outreach contacts.csv --state MO --cpt 97110,97140` | Contact/mail-merge CSV |
| `mrfx enrich --bulk "E:\NPPES…zip"` | Fill in provider names/geography now, in one fast local pass from the NPPES bulk file (see "Make provider names fill in fast" above) |
| `mrfx forget <filename> [more…]` | Erase chosen files' rates + raw copies (names from `mrfx status`) |
| `mrfx reset --confirm` | Clear the analyzed data (keeps your downloaded files) |

---

## Where things live in the project folder

- `config/mrfx.yaml` — settings (which codes to extract, port, enrichment on/off).
  Edit with any text editor; defaults are fine to start.
- `data/inbox/` — drop MRF files here.
- `data/downloads/` — where pasted links download to while processing. Each
  file is deleted automatically once its rates are extracted (turn that off
  with `delete_raw_after_ingest: false` in `config/mrfx.yaml` if you want to
  keep the raw files).
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

Your **data and the program are separate folders**, so you can drop in a newer
build and keep everything you've already ingested — rates, identified provider
names, the link queue, entity groupings, peer sets. All of that lives in the
`data/` folder, which is **never** part of a code download, so a code update
cannot touch it. The one file a new build *does* replace is your
`config/mrfx.yaml` (your settings), so protect that.

Steps (a few minutes, no re-ingesting):

1. **Stop the app** — press Ctrl-C in the `mrfx serve` window. While it runs it
   keeps the database open, so don't update code mid-run.
2. **Save your settings** — copy `config/mrfx.yaml` somewhere safe (or just note
   your `bulk_csv_path` line and any other edits).
3. **(Recommended) back up your store** — copy the whole `data/` folder
   somewhere first. It may be several GB, but it's your entire book of work, and
   then even a mistake is fully recoverable.
4. **Put the new code in.** The simplest safe way: open the new build's `.zip`,
   and copy its **`mrfx` folder** over your existing `mrfx` folder (choose
   replace/overwrite). That single folder *is* the whole program — this updates
   everything while leaving `data/`, `config/`, and `.venv/` untouched, so
   there's nothing to restore. (If you'd rather unzip the whole thing over the
   folder, that works too — just put your saved `config/mrfx.yaml` back after.)
5. **Restart `mrfx serve`.** You do **not** need to reinstall anything — your
   virtual environment already has what it needs.
6. **Hard-refresh the dashboard in your browser** — press **Ctrl-Shift-R** (or
   Ctrl-F5) once on the dashboard tab. Browsers cache the dashboard's code, and
   after a build swap they can keep running the *old* page against the new app —
   the tell-tale sign is a control that suddenly shows **blank** (e.g. the
   "As-of month" dropdown). A hard-refresh loads the new page. (New builds also
   ask the browser not to cache the dashboard, so this gets less necessary over
   time, but it's the instant fix if anything looks off right after updating.)

On that first restart the app notices its summary tables are from the older
version and **rebuilds them once** — a one-time wait proportional to your store
size. This only recomputes the compact analytics from your existing rates; it
does **not** re-download, re-parse, or re-identify anything. After it finishes,
the new features are live and every number is preserved.

**Adding new payer files** then works exactly as always — drop them in the inbox
or paste links; they ingest alongside what's already there. Re-adding a file you
already have is automatically **skipped, not duplicated**, so your numbers can't
get double-counted.

---

## Debug FAQ — what it says, what it means, what to do

Every message the app shows is designed to tell you the fix. This FAQ collects
them in one place, grouped by where you'll hit them.

### Setup problems

- **`Failed to build installable wheels for some pyproject.toml based
  projects` (duckdb, pyarrow, watchfiles, pydantic-core)** — your Python is
  newer than these packages ship ready-made installers for, so pip tried to
  compile them from source and failed. **Don't install a compiler — use Python
  3.12.** On Windows: `py -0p` to confirm 3.12 is installed (if not, get it
  from <https://www.python.org/downloads/release/python-3120/>), delete the
  half-made `.venv` folder, then run the Step-3 sequence starting with
  `py -3.12 -m venv .venv`. `python --version` inside the active environment
  must read `3.12.x` before you install.
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
  legitimately huge file. If you want it: raise `confirm_over_gb:` in
  `config/mrfx.yaml` (e.g. `12`) and press retry — the partial download was
  kept, so it resumes rather than restarting.
- **`not enough free disk space for this file`** — the file needs more room
  than you have. Free space (empty `data/processed/`, other downloads) and
  press retry.
- **`connection closed early (X of Y MB) — retrying from where it stopped`**
  — a flaky network or server. The app retries and resumes automatically; if
  it ultimately fails, press retry later — it continues from the same byte.
- **`this TOC lists only out-of-network allowed-amounts files`** (skipped) —
  that payer publishes no negotiated rates at this link (e.g. Excellus).
  Correct behavior; nothing to fix.
- **`identical to a file already ingested … skipped as duplicate`** — Blue
  plans host copies of each other's national files. Your data already has
  it; nothing was lost.
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

- **I pressed Ctrl-C — is my work lost?** No. Restart `mrfx serve` (or re-run
  `mrfx add`): downloads resume mid-file, interrupted files re-queue, and
  nothing is double-ingested.
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
  The app already uses (your cores − 1) parsers automatically
  (`parallel_ingests: 0`); on a big multi-core machine that's the fastest
  safe setting, so just leave it running overnight and skip rows you don't
  need. (If the startup log warns that ijson is running its "pure-Python
  backend", that makes parsing ~10× slower — reinstall as the log suggests.)
- **Rates look doubled for one payer** — check the Files tab for two ingests
  of the same book under different filenames from before the dedup fix; if
  so, `mrfx reset --confirm` and re-queue (dedup now catches mirrors even in
  parallel).
- **Dashboard numbers aren't updating during a big run** — the analytics
  tables refresh in batches (every ~10 files and at least every ~90 seconds),
  so brief lag mid-grind is normal. If numbers stay frozen for many minutes,
  look at the `mrfx serve` terminal window: a "rollup rebuild failed" line
  there usually means low disk — free some space; the raw data is safe and
  analytics catch up on the next successful rebuild.
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
  day, or is stuck at a few thousand.** You're being served by the slow `api`
  path, looking each provider up one at a time over the internet (rate-limited,
  so a big book takes days and can appear stuck once NPPES throttles you). Point
  the app at the local NPPES bulk file — see **"Make provider names fill in fast
  (recommended)"** above. One local pass identifies the whole backlog in
  minutes. Quick version: set `bulk_csv_path` in `config/mrfx.yaml` and restart
  (that alone switches to the fast local path — `mode` can stay `api`; the
  one-off `mrfx enrich --bulk` command does the same but needs the dashboard
  stopped first). **If you
  already set `bulk_csv_path` and it's still slow, the path is almost certainly
  wrong** — a misspelled path or one pointing at a file that isn't there is
  silently un-usable, so the app falls back to the API. Check the `mrfx serve`
  window: it now logs a warning naming the missing file. Fix the path (mind the
  Windows quotes/slashes) and restart.
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
- For anything beyond this FAQ, hand `docs/AI_HANDOFF.md` plus the row's
  error text to an AI assistant — that document tells it exactly how this
  codebase works and how to debug it safely.
