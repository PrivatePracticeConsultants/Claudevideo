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

## Step 5 — Get a payer MRF file to analyze

You need at least one machine-readable file (MRF) from a payer. The app helps
you find one:

1. In the dashboard, click the **Sources** tab.
2. Pick a state → it shows that state's Blue Cross licensee(s) and the national
   payers, each with a link to where their MRF files live.
3. Click through, and download an **in-network rates** file (they end in
   `.json.gz`). Start with a smaller one to try things out.

A known, easy source to test with is Highmark's portal at
<https://mrfdata.hmhs.com> — pick a state, then a `..._in-network-rates_...json.gz`
file. (Files are big; grab a small one first.)

---

## Step 6 — Load the file

Two ways:

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
