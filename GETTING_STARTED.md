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

The queue table under the box shows each link's plain-language status
(`queued` → `downloading` with a MB progress bar → `analyzing` → `done` with a
row count). Files process **one at a time in the background** — you can paste
a whole state's worth of links and walk away. Each file's download is deleted
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

  That queues 24 sources (Highmark's 15 hosted Blue plans, BCBS Mississippi,
  BCBS Tennessee, Cigna, BCBS South Carolina, Centene/Ambetter's all-states
  page, the Blue KC / BCBS Michigan / BCBS Louisiana hubs, and
  UnitedHealthcare's national portal). Each one
  expands on its own — expect thousands of files to queue and let it run.
  The same file lists the ~25 browser-only portals (Anthem, Aetna, Kaiser…)
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
| `mrfx export out.csv --cpt 97110 --state MO` | Export a filtered CSV + methodology sidecar |
| `mrfx outreach contacts.csv --state MO --cpt 97110,97140` | Contact/mail-merge CSV |
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

---

## Troubleshooting

- **`mrfx: command not found`** — the virtual environment isn't active. Re-run
  the `activate` line from Step 3 (you'll see `(.venv)` in the prompt), then try
  again.
- **`python3: command not found` on Windows** — use `python` instead of
  `python3`.
- **Port 8377 already in use** — change `port:` in `config/mrfx.yaml` to another
  number (e.g. 8400) and restart `mrfx serve`.
- **A pasted link says "download link has expired"** — many payers (all the
  Blue plans on `*.mrf.bcbs.com`) publish *signed* links that stop working
  after a few days. Go back to the payer's TOC/index page, paste **that** link
  instead, and the app will fetch fresh file links itself.
- **A pasted link says "this looks like a web page"** — some payer portals
  (e.g. Aetna's) build their file list with JavaScript, so there are no real
  links in the page for the app to lift. Open the page in your browser, click
  through to the actual `.json.gz` / TOC links, and paste those.
- **A link shows "allowed-amounts (no rates)"** — that file is the payer's
  out-of-network billed-charge report, which contains no negotiated rates.
  Skipping it is correct; look for the `in-network-rates` files instead.
- **A link shows "duplicate (already have it)"** — the downloaded file was
  byte-for-byte identical to one already loaded (Blue plans host copies of
  each other's national files). Nothing was lost; the data is already in your
  database under the first link.
- **The payer name doesn't match the state I pasted** — normal. State indexes
  list every file their members might need, including other Blue plans'
  national files. The app names each file by the payer written *inside* it,
  which is the accurate attribution.
- **A pasted file is huge and was refused** — files bigger than the safety
  limit (default 5 GB compressed) are held back so a typo can't fill your
  disk. If you really want it: open `config/mrfx.yaml`, change
  `confirm_over_gb: 5.0` to a number bigger than the file (e.g. `12`),
  save, and press **retry** on that row. Verified live on an 8.85 GB
  UnitedHealthcare file — after the retry it downloads and processes
  normally; just expect multi-GB files to take hours, not minutes (the
  progress bar shows exactly where it is).
- **Not enough disk space** — the app checks before downloading and tells
  you how much a file needs; free up space and press retry. Partial
  downloads are kept and **resume where they stopped**, so an interrupted
  8 GB download doesn't start over.
- **It's processing several files at once — is that OK?** Yes: that's
  `parallel_ingests` in `config/mrfx.yaml` (shipped as 3, automatically
  reduced on smaller machines). Set it to 1 if you want strictly one file
  at a time.
- **A file shows `NEEDS COMPANION`** — that payer split its provider list into a
  separate reference file; download and drop that in too, and the app
  re-processes automatically.
- **Names show as "TIN 12345…" instead of practice names** — name lookup
  (NPPES) runs in the background after ingest; give it a few minutes, or it may
  be off in config (`enrichment: api`).
- **It's slow / big file** — that's expected for multi-GB files; the Files tab
  progress bar shows it working through in chunks. You can keep using the
  dashboard on already-loaded data meanwhile.

---

## Important, honest caveats (these matter if numbers reach a client)

- A published negotiated **rate is per billing code, not per visit**, and not
  proof a practice actually collects it. The app keeps everything granular so
  you model rather than guess; benchmark reports state this.
- Payers publish "ghost" rates for codes a practice never bills — benchmarks
  stay within the therapy code set to reduce that, and say so in the footer.
- Outreach and benchmark reports are meant to go to **a practice about its own
  market position**, not to coordinate pricing between competitors.
