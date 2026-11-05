# How to run MRF Explorer yourself — plain-language steps

This is the short version. Every step says exactly what to click or type.
If anything here disagrees with what you see, the longer guide
(`GETTING_STARTED.md`, same folder) has more detail and a troubleshooting FAQ.

**What this program does:** you paste links to insurance companies' public
price files. It downloads them, keeps only the physical / occupational /
speech therapy codes (97110, 97140, 92507, …), and builds a small database on
your computer. Then you search by **NPI** (one provider), **TIN** (a practice's
tax ID) or practice name, and see what each insurer pays for each code.

Everything stays on your computer. Nothing is uploaded anywhere.

---

## Part 1 — One-time setup (about 15 minutes)

### Step 1. Install Python

1. Go to <https://www.python.org/downloads/> and click the big yellow
   **Download Python** button. (Any version 3.11, 3.12, 3.13 or 3.14 works.)
2. Open the file you downloaded.
3. **Important (Windows):** on the first screen, tick the box
   **"Add python.exe to PATH"** at the bottom. Then click **Install Now**.
4. When it says "Setup was successful", click **Close**.

### Step 2. Unzip the project

1. Find the zip file you were sent (`mrfx_project.zip`) in your Downloads.
2. Right-click it → **Extract All…** → **Extract**.
3. Move the folder it makes somewhere easy to find, for example your
   **Documents** folder. Don't rename anything inside it.

> Pick a drive with plenty of free space. The downloads are big (a single
> insurer file can be several gigabytes) even though the finished database is
> small. 100 GB free is comfortable.

### Step 3. Open a command window in that folder

- **Windows 11:** open the project folder, right-click an empty spot inside
  it → **Open in Terminal**.
- **Windows 10:** open the project folder, hold **Shift** and right-click an
  empty spot → **Open PowerShell window here**.
- **Mac:** open the **Terminal** app, type `cd ` (with a space after it), drag
  the project folder onto the Terminal window, and press **Enter**.

A window with a blinking cursor opens. You'll paste commands into it.
(To paste: right-click in the window on Windows, or Cmd-V on a Mac. Press
**Enter** after each one.)

### Step 4. Install the program (copy each line, paste, press Enter)

**Windows:**

```
py -3 -m venv .venv
```
```
.\.venv\Scripts\Activate.ps1
```
```
python -m pip install -r requirements.txt
```
```
python -m pip install -e .
```

**Mac:**

```
python3 -m venv .venv
```
```
source .venv/bin/activate
```
```
pip install -r requirements.txt
```
```
pip install -e .
```

The third command takes a few minutes and prints a lot of text — that's
normal. It's done when the blinking cursor comes back.

- If Windows says **"running scripts is disabled on this system"** after the
  second line, paste this once, press Enter, answer **Y**, then redo the
  second line:
  ```
  Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
  ```
- If you see **"Failed to build installable wheels"**, your Python is too new
  or too old. Install 3.13 from python.org, delete the `.venv` folder inside
  the project, and start Step 4 again.

### Step 5 (optional, 2 minutes). Check the install works

Still in the same window, paste:

```
python -m pytest tests/mrfx/test_known_answer.py -q
```

You should see **`16 passed`** (the exact number may be a little higher in
later versions; what matters is that it says *passed* and nothing says
*failed*). This test builds a tiny fake insurer file whose correct answers
were worked out by hand, runs it through the whole program, and checks every
number the dashboard and the CSV export show.

---

## Part 2 — Every time you use it

### Step 6. Start the program

**Easiest (Windows):** double-click **`Start MRF Explorer (auto-restart).bat`**
in the project folder. A black window opens and stays open — that's the
program running. If it ever stops unexpectedly, it restarts itself and picks
up where it left off.

**Or, from the command window** (Mac, or if you prefer):

1. Open a command window in the project folder (Step 3).
2. Turn the environment on — Windows: `.\.venv\Scripts\Activate.ps1`,
   Mac: `source .venv/bin/activate`. You'll see `(.venv)` at the start of the line.
3. Type `mrfx serve` and press Enter.

Then open your web browser and go to **<http://localhost:8377>**.

**To stop it:** click in the black window and press **Ctrl-C** (or just close
the window). Nothing is lost — next time you start it, it continues
exactly where it stopped, including half-finished downloads.

### Step 7. Load insurer data

1. In the dashboard, click the **Files & imports** tab.
2. Paste one or more insurer links into the big box (one per line). These are
   the "machine-readable file" links insurers publish — a "table of contents"
   link that lists many files is fine; the program expands it.
3. Click **Add links**.
   - Not sure where to start? Click **Queue tested payer indexes** — it queues
     insurer links this program has already been tested against.
4. Watch the list below: each file goes *queued → downloading → ingesting →
   done*. The bar shows progress. Large files take a while (see the table in
   `GETTING_STARTED.md` under "what to expect"); you can keep using the
   dashboard while they run.

**Leave the program running** while a long list works through. It's fine to
leave it overnight.

### Step 8. Look up what insurers pay a provider (search by NPI)

1. Click the **Explorer** tab.
2. Under the grain choice at the top, click **NPI** (one row per individual
   provider). Use **TIN** to see a whole practice, or **Entity** to roll a
   practice's several tax IDs into one organization.
3. Type the full 10-digit NPI (or a 9-digit TIN) in the search box (it says
   *"name, TIN or NPI…"*). Dashes and spaces are fine.
4. Optional: click a code chip (e.g. **97110**) to see just that code, or pick
   an insurer.

Each row is one insurer's rate for one code. The columns you'll use most:

| Column | What it means |
|---|---|
| Rate | The negotiated dollar amount for that code |
| Payer | The insurer |
| Code / Modifiers | The CPT code, and modifiers like GP (PT), GO (OT), GN (speech), CQ/CO (assistant) |
| Month | Which month's file the rate came from. **By default you see every month you've loaded**, so a rate that changed shows up once per month (click the Month heading to sort). Pick a month in the **Month** box above the table to see just one. |
| Variants | 1 = one clear rate. A number with ⚠ means the insurer published several different rates for the same thing; the Rate shown is their middle value — check before quoting it. |

**Once your database gets big** (over about 40 million rates — many insurers
loaded), the NPI and Entity views only answer narrow questions, so they stay
fast: a full NPI or TIN in the search box, or a payer **and** a code picked.
If you ask for more, a message tells you exactly what to add. To find a
practice by **name**, use the **TIN** view's search, then click the practice.

Things the program leaves out on purpose (so the numbers are real rates):
"bundled" package prices, percentage-of-charge rates (switch **dollar rates
only** off to see them), and $0 / $0.01 placeholder rates.

Click any row to open that provider's or practice's full profile.

### Step 9. Compare who pays most for one code

Click the **Code comparison** tab and pick a code. You'll see practices
ranked by rate, a chart of how rates spread out, and which insurer pays best.
This page uses each practice's **newest** file only (an old rate an insurer
has since replaced is not mixed in) — the heading says "newest file per
contract".

### Step 10. Export to Excel

On the Explorer tab, click **Export CSV** (opens in Excel) or **Export +
methodology** (a zip with the CSV plus a text file explaining exactly which
filters and rules produced the numbers — keep it with anything you send to
a client). The export contains exactly the rows the screen shows.

Social-Security-number-looking tax IDs (sole practitioners who use their SSN)
are always shown as **MASKED-SSN**, on screen and in every export.

### Step 11 (recommended). Make practice names fill in fast

Rates arrive with only ID numbers; names come from the federal NPI registry
(NPPES). By default the program looks them up one by one over the internet,
which is slow for big loads. The fast way is a one-time download:

1. Download the "NPPES Data Dissemination" file (full monthly, ~1 GB zip) from
   <https://download.cms.gov/nppes/NPI_Files.html>.
2. Open `config/mrfx.yaml` (in the project's `config` folder) with Notepad.
   Find the line near the `enrichment:` section that reads
   `# bulk_csv_path: 'E:\NPPES_Data_Dissemination_July_2026_V2.zip'`.
   Delete the `#` and the space after it (the line must start with two
   spaces, lined up with `mode: api` above it), and replace the path with
   where you saved the zip, keeping the single quotes, e.g.
   `  bulk_csv_path: 'C:\Users\you\Downloads\NPPES_Data_Dissemination_November_2026.zip'`
   (Keep it zipped — don't unzip it.)
3. Save, then stop and restart the program (Step 6).

(If you load files from the command line with `mrfx add`, it now tells you how
many names are left to look up, and **Ctrl-C** skips that step safely — your
rates are already saved.)

---

## Part 3 — Good habits

- **Back up your database** now and then: in a command window with the
  environment on (Step 6, items 1–2), run `mrfx backup my_backup.zip`. Check a
  backup with `mrfx verify my_backup.zip`.
- **Updating to a new version:** keep your old project folder's `data` folder
  and `config/mrfx.yaml`. Unzip the new version, copy those two into it, and
  redo Step 4 (it's quick the second time).
- **Does a number look wrong?** Note the insurer, NPI/TIN, code and month,
  then run `mrfx fastcheck "path\to\that_file.json.gz"` on the insurer file.
  It processes the file two independent ways and tells you if they disagree.

---

## Part 4 — What has and hasn't been tested (please read once)

Tested on Linux with real insurer files from 9+ insurers (BCBS of several
states, UnitedHealthcare, Aetna, Moda, Premera, Tufts, First Choice), files up
to 42 GB uncompressed, a 590-million-row Aetna file, crash-and-restart trials,
an independent recount of 1.4 million practice-level rates that matched the
program to the cent, and a 306-million-rate test database (NPI lookup about
1 second). **It has not been run on a Windows computer by the
person who built it** — the program was written for Windows and has
Windows-specific safeguards, but the first real Windows run is yours. On that
first run, please watch for these and note anything odd:

1. Step 4 finishes without red error text.
2. Step 5 says *passed*.
3. Load one small insurer link and one big one (several GB). Open **Task
   Manager** (Ctrl-Shift-Esc) while the big one runs: memory use should stay
   well under your computer's total.
4. While a big file is mid-way, close the program window, then start it again.
   The file should continue (or restart cleanly) and finish as **done**.
5. Afterwards run `mrfx verify` — it should report the store as OK.

If something fails, the black program window shows a plain-language message;
copy it (and the file name) and send it along.
