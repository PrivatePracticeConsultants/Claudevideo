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
4. **Important:** inside that folder is ONE more folder called
   **`Claudevideo`**. That inner `Claudevideo` folder is the program — it
   holds `requirements.txt`, `HOW_TO_RUN.md` and the
   `Start MRF Explorer (auto-restart).bat` file. Every step below happens
   **inside `Claudevideo`** (for example `E:\mrfx_project\Claudevideo`).

> Pick a drive with plenty of free space. The downloads are big (a single
> insurer file can be several gigabytes) even though the finished database is
> small. 100 GB free is comfortable.

### Step 3. Open a command window in the `Claudevideo` folder

- **Windows 11:** open the **`Claudevideo`** folder (the inner one from
  Step 2), right-click an empty spot inside it → **Open in Terminal**.
- **Windows 10:** open the **`Claudevideo`** folder, hold **Shift** and
  right-click an empty spot → **Open PowerShell window here**.
- **Mac:** open the **Terminal** app, type `cd ` (with a space after it), drag
  the **`Claudevideo`** folder onto the Terminal window, and press **Enter**.

Check you're in the right place by pasting `dir requirements.txt` (Mac:
`ls requirements.txt`) and pressing Enter: it must list the file. If it says
"cannot find" / "No such file", you're one folder too high — type
`cd Claudevideo`, press Enter, and check again.

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
- If you see **"Could not open requirements file … No such file or
  directory"**, the window is in the outer folder, not `Claudevideo` — type
  `cd Claudevideo`, press Enter, and start Step 4 again from the first line.
  (The ".venv" folder the first try made in the outer folder is harmless;
  delete it whenever you like.)
- A yellow **"A new release of pip is available"** line is just a notice,
  not an error — ignore it.
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
3. Type `python -m mrfx serve` and press Enter. (`mrfx serve` also works, but
   only while the folder keeps the name and place it was installed with.)

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
   - Not sure where to start? Click **Show tested sources**: it lists about
     145 insurer and administrator links this program has been tested against
     (each with the states it covers, the date it was checked and what to
     expect). Type **your state(s)** in the box above the list (for example
     `MO KS`) — the list narrows to those states plus the national insurers —
     and click **Queue these states**. That is the easy way to load the
     insurers that matter to you.
   - **Queue tested payer indexes** queues ALL of the roughly 100 automatic
     ones at once. That is hundreds of thousands of files and many terabytes,
     a grind of weeks, so only use it if you really want everything. (Each big
     index is capped at 2,000 files unless you raise `max_toc_files` in
     `config/mrfx.yaml`.)
4. Watch the list below: each file goes *queued → downloading → ingesting →
   done*. The bar shows progress. Large files take a while (see the table in
   `GETTING_STARTED.md` under "what to expect"); you can keep using the
   dashboard while they run.

**A file says "paused"?** Its insurer's server stopped sending data part-way
(the bar sat still for 10 minutes). The program set it aside so the other
files keep going, kept what it had, and will try again by itself in 24 hours
— up to 5 times, then it's marked failed. **Try now** retries it immediately;
**skip** forgets it.

**Too much at once?** **Clear queue** sets aside everything still waiting and
**Stop downloads** pauses the ones downloading; **Resume stopped** puts all of
them back in one click (partial downloads continue where they stopped).

**Pasted the wrong link?** While a file is being processed its row has a
**stop** button. Nothing from the stopped run is saved. A file from a link
keeps its download, so **retry** later processes it without downloading
again; a file you dropped in the inbox is moved to `data/failed`.

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

### Step 11. Practice names fill in by themselves

Rates arrive with only ID numbers; names, specialties and addresses come from
the federal provider registry (NPPES). **You don't need to do anything:** the
program downloads CMS's monthly provider file (about 1.1 GB) on its own the
first time it runs, in the background, and then names every provider from it
in one fast pass. Measured on a database of 32 million rates: all 93,479
providers named in about 3½ minutes, download included.

After that it checks once a day for a newer month. When CMS publishes one
(early each month), it downloads it, uses it, and updates any provider whose
name, specialty or address changed. The file is kept in `data\nppes` (the last
two months; older ones are deleted for you).

- The first time, the banner at the top of the dashboard shows names starting
  to fill in after a few minutes.
- No internet, or CMS is down? It quietly tries again in an hour and uses what
  it already has meanwhile.
- If you'd rather manage the file yourself, see the `enrichment:` section in
  `config\mrfx.yaml`.

---

## Part 3 — Good habits

- **Back up your database** now and then: in a command window with the
  environment on (Step 6, items 1–2), run `mrfx backup my_backup.zip`. Check a
  backup with `mrfx verify my_backup.zip`.
- **Updating to a new version:** see Part 5 — drag the new zip onto
  `Update MRF Explorer.bat`; your data and settings are kept.
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

---

## Part 5 — Updating to a new version (your data is kept)

Everything the program has found — the rates, provider names, the link queue,
your saved backups — lives in the **`data`** folder. Your settings live in
**`config\mrfx.yaml`**. An update replaces the *program*, never those.

### From now on: drag and drop (about a minute)

1. **Close MRF Explorer** — close its black window (or click in it and press
   **Ctrl-C**). The update refuses to run while the program is open.
2. **Download the new `mrfx_project.zip`.** Leave it zipped.
3. Open your program folder (`…\Claudevideo`) and **drag the zip file onto
   `Update MRF Explorer.bat`**. A black window opens, shows what it updated,
   and ends with **"Done."** Press any key to close it.
4. **Start MRF Explorer** as usual (Step 6), and in the browser press
   **Ctrl-Shift-R** once on the dashboard so it loads the new screens.

What the updater does for you: it keeps the `data` folder, the `.venv`
folder and your `config\mrfx.yaml` exactly as they are; replaces the program,
the tested-payer list and the guides; saves the old program files in
`data\update_backups\` (so nothing is ever lost); and re-installs any new
components. If the new version comes with a changed settings file, yours is
kept and the new one is saved next to it as `config\mrfx.yaml.new` — you only
need it if you want to look at new options.

On the first start after an update, the program may spend a few minutes
"rebuilding analytics" (shown in the window). That's normal and happens once;
nothing is re-downloaded.

**The November 2026 update also fixes stored data once, by itself.** On its
first start it may say it "updated … stored row(s) … to the current rules"
(about half a minute per 15 million rates) and "stored times converted once
from this PC's time zone". That corrects three things in rates you already
have — no re-download needed: some insurers (BCBS Alabama is the big one) put
place-of-service codes where modifiers belong, which hid ALL of their rates
from "base modifiers only" and the Benchmark; two modifiers written as one
("52,53") are split; and a file dated in the future is filed under its real
month. The **Files** tab shows a note on each file that was corrected. For
about a minute after that first start the dashboard may still show the old
numbers while it refreshes its summaries in the background (the window says
"analytics update finished" when done) — then press **Ctrl-Shift-R**.

**Renaming or moving the program folder is fine** from version 0.5 on: the
Start and Update files run the folder's own program wherever it lives. (Before
0.5, a renamed folder made Start fail with "No module named 'mrfx'". If you
see that on an older copy, open a command window in the folder and run
`.\.venv\Scripts\python.exe -m pip install -e .` once.)

### The very first time (your current copy has no updater yet)

Do this once; after it, use the drag-and-drop steps above.

1. **Close MRF Explorer.**
2. **Save your settings:** in `…\Claudevideo\config`, copy `mrfx.yaml` to your
   Desktop. (If you never edited it, you can skip this and step 5.)
3. Right-click the new `mrfx_project.zip` → **Extract All…** → in the
   "destination" box choose **the same folder you extracted to last time**
   (the one that *contains* `Claudevideo` — e.g. `E:\mrfx_project`) →
   **Extract**.
4. Windows asks about files that already exist → choose **"Replace the files
   in the destination"**. (Your `data` folder isn't in the zip, so it can't be
   replaced.)
5. Copy the `mrfx.yaml` from your Desktop back into `…\Claudevideo\config`
   and choose **Replace**.
6. Open a command window in `…\Claudevideo` (Step 3) and paste, one at a time:
   ```
   .\.venv\Scripts\Activate.ps1
   ```
   ```
   python -m pip install -r requirements.txt
   ```
   ```
   python -m pip install -e .
   ```
7. **Start MRF Explorer** (Step 6) and press **Ctrl-Shift-R** in the browser.

**Want an extra safety net first?** Before any update you can copy the whole
`data` folder somewhere else (it may be large), or run `mrfx backup
E:\mrfx_backup.zip` with the program closed.
