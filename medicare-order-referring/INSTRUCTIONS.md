# How to use the Medicare Order & Referring Tracker — plain-English guide

*No technical knowledge needed. Total setup time: about 10 minutes, most of it
waiting for downloads.*

---

## 1. One-time setup

1. **Get the folder onto your computer.** You need the whole
   `medicare-order-referring` folder (the one this file is in). Put it
   anywhere you like — your Desktop or `C:\` is fine. Don't move or rename
   the files inside it.
2. That's it. There is nothing to install. The app uses PowerShell, which is
   already part of Windows.

> **If Windows shows a blue "Windows protected your PC" box** the first time
> you run it: click **More info**, then **Run anyway**. That warning appears
> for any program Windows hasn't seen before.

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

## 4. The four tabs, in plain English

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
**Compare**, and see exactly who was **Added**, **Removed**, or **Changed**
(eligibility flags flipped) between them. Exportable like everything else.

### Tab 4 — Referral map (2015)
*"Who sends patients to the rehab providers in my area?"*

One-time setup for this tab: click **Download CMS dataset** (~356 MB — the
newest public CMS provider-to-provider data; takes a few minutes).

Then:
1. Type a ZIP code — or a prefix like `630*` to cover a wider area — and
   click **Map referral sources**.
2. **Top table:** every outpatient rehab provider in that area, ranked by how
   many Medicare patients flowed into them.
3. **Click any row** and the bottom table shows *who fed them those
   patients* — names, specialties, cities, and patient counts.
4. Export either table with the buttons.

**Read this before trusting the numbers:**
- This data is from **2015** — the newest CMS ever released publicly. It
  shows the *structure* of your referral market (who the big referrers are
  and whom they historically fed), **not this year's volumes**.
- A provider marked **"No (NPI issued 2019)"** in the last column didn't
  exist yet in 2015 — their zero means "too new," not "no referrals."
- Private clinics show up under their **individual therapists' names**
  (that's how CMS built the file); hospital rehab departments show up as
  organizations.
- Labs and hospitals sometimes appear as "sources" just because patients
  visited them around the same time. Judge sources by specialty: an
  orthopedic surgeon feeding a physical therapist is a real referral
  pattern; a lab is not.

---

## 5. Keeping everything current automatically (optional)

If you'd rather never click "Check for updates": open PowerShell in the
`medicare-order-referring` folder (right-click the folder while holding
Shift → "Open PowerShell window here") and paste:

```powershell
Import-Module .\OrderReferring; Install-OrfUpdateTask -At 07:00
```

Windows will now quietly check for a new Medicare list every morning at 7:00
and download it only when there actually is one. To turn it off later:

```powershell
Import-Module .\OrderReferring; Uninstall-OrfUpdateTask
```

---

## 6. Where your files live

- **Downloaded data** is stored in `C:\Users\<you>\AppData\Local\OrderReferringTracker`
  — you never need to touch it. Deleting that folder just means re-downloading.
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
