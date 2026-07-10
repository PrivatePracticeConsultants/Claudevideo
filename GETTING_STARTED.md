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

## Step 1 — Install Python 3.11 or newer (one time)

Check whether you already have it:

```
python3 --version
```

If it prints `Python 3.11.x` or higher, skip to Step 2. Otherwise:

- **macOS**: install from <https://www.python.org/downloads/> (get the latest
  3.x installer) — or, if you use Homebrew, `brew install python@3.12`.
- **Windows**: install from <https://www.python.org/downloads/>. **On the first
  installer screen, tick "Add python.exe to PATH"** before clicking Install.
  On Windows the command is usually `python` (not `python3`).
- **Linux (Debian/Ubuntu)**: `sudo apt install python3 python3-venv python3-pip`.

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

**Windows (PowerShell):**
```
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
pip install -e .
```
> If Windows blocks the activate script with a security error, run this once,
> then retry the activate line:
> `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`

You'll know the environment is active when your prompt shows `(.venv)` at the
start of the line. **Any time you open a new terminal to use the app, re-run
just the `activate` line** (the `source .venv/bin/activate` or
`.venv\Scripts\Activate.ps1` step) — you don't reinstall.

> **Optional but recommended — the headless-browser helper.** Some payers
> (Molina, Kaiser, Aetna, Harvard Pilgrim, Regence, and Oscar's file page)
> build their file lists with JavaScript, and the app can read those pages
> automatically if you run this once after the install above:
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
row count; index links show `expanding` while their file lists unpack). Files
process **a few at a time in the background** (3 with the shipped settings —
the `parallel_ingests:` knob in `config/mrfx.yaml`) — you can paste a whole
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

  That queues 33 sources (Highmark's 15 hosted Blue plans, BCBS Mississippi,
  BCBS Tennessee, Cigna, BCBS South and North Carolina, CareFirst, Molina,
  Kaiser, Aetna, Harvard Pilgrim, Regence, Oscar, the 14-state Anthem
  master index,
  Centene/Ambetter's all-states page, the Blue KC / BCBS Michigan /
  BCBS Louisiana hubs, and UnitedHealthcare's national portal —
  Molina/Kaiser/Aetna/Harvard Pilgrim/Regence/Oscar need the one-time
  Playwright install from Step 3's note (the starter-links FILE swaps Oscar
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

> Want to check a file *before* committing to a long parse? In a second
> terminal (with the venv activated) run `mrfx preflight data/inbox/<file>` —
> it reports the payer, size, and whether it's ready to ingest, in seconds.

---

## Step 7 — Explore, benchmark, export

Once a file is `done`:

- **Explorer** tab: the rate table (one row per practice × code). Use the
  filters — payer, discipline (PT/OT/SLP), code chips, state/city — and sort by
  clicking column headers. Click a row to drill into a practice.
- **Code comparison** tab: pick a code → see which practices are paid most,
  with a distribution chart.
- **Benchmark** tab: pick a subject practice + an as-of month → see where its
  rates sit versus the market (percentiles), the dollar gap, and — with your
  own volume numbers — an opportunity estimate and a printable pitch report.
- **Export**: the **Export CSV** / **Export + methodology** buttons download the
  current filtered view. The **Outreach CSV** button gives one row per practice
  with name + address + phone + per-code rate/percentile columns — ready to
  cross-reference your contact list and mail-merge (e.g. in Brevo).

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

## Debug FAQ — what it says, what it means, what to do

Every message the app shows is designed to tell you the fix. This FAQ collects
them in one place, grouped by where you'll hit them.

### Setup problems

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
  found on it`** — the page builds its list with JavaScript. Fix: install
  the headless-browser helper
  once (`pip install playwright && playwright install chromium`, in your
  venv), then press **retry** on the row. If the message says the helper *is*
  installed but the browser isn't, run just `playwright install chromium`.
  Still nothing after that? The page needs multi-step human clicks — open it
  in your browser, right-click the real `.json/.json.gz/.zip` links, Copy
  Link Address, and paste those.
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
- **The queue is huge and slow** — expected for whole-payer grinds (UHC and
  Anthem queue thousands of files; parsing runs ~30s per uncompressed GB).
  Raise `parallel_ingests:` in `config/mrfx.yaml` (3 is a good default on a
  4-core machine), leave it running overnight, and skip rows you don't need.
- **Rates look doubled for one payer** — check the Files tab for two ingests
  of the same book under different filenames from before the dedup fix; if
  so, `mrfx reset --confirm` and re-queue (dedup now catches mirrors even in
  parallel).
- **The dashboard says analytics are stale / a rollup failed** — the raw
  data is safe; analytics refresh on the next successful rebuild (usually
  the next file). If it keeps failing, you're low on disk — free some.
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

### Getting help

- `mrfx status` summarizes everything the store knows.
- Each failed row's error text is written to be actionable — read it first.
- For anything beyond this FAQ, hand `docs/AI_HANDOFF.md` plus the row's
  error text to an AI assistant — that document tells it exactly how this
  codebase works and how to debug it safely.
