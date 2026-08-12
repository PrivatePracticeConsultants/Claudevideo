# CLAUDE.md — orientation for an AI or engineer picking up this repo

**Read `docs/AI_HANDOFF.md` in full before making any substantive change.** It is
the authoritative deep-dive: purpose, module map, the invariants that must not
be violated, the URL-pipeline states, the measured performance envelope, how to
add a payer, and how to test. This file is the 60-second version and a pointer
to it.

## What this repo is

Two tools live here:

- **MRF Explorer (`mrfx/`)** — THE product. A local, single-user pipeline +
  dashboard that aggregates **negotiated commercial PT/OT/SLP therapy rates**
  from payers' Transparency-in-Coverage machine-readable files (MRFs) into a
  DuckDB/Parquet store, with filtering, benchmarking, and outreach exports. The
  user pastes payer links; the app downloads, filters to the target CPT set,
  deduplicates, and keeps a compact analytical database. Files run 1 MB–22 GB
  compressed (up to ~200 GB uncompressed) — the whole design is shaped by that.
- **`src/` + `run.py`** — an earlier, single-purpose BCBS-Missouri extractor.
  Reference only; **do not extend it.** All new work is in `mrfx/`.

The intended deployment is one non-technical user running `mrfx serve` on their
own Windows machine — not a public service. Optimize for their experience and
for correctness of the numbers.

## Build / run / test

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt && .venv/bin/pip install -e .
.venv/bin/mrfx serve                       # dashboard http://localhost:8377 + inbox watcher + URL worker
.venv/bin/python -m pytest tests/ -q       # the suite: real end-to-end drains against local HTTP servers
```

Other commands: `mrfx add <url>… [--file links.txt] [--known] [--retry-failed]`,
`mrfx preflight <path>`, `mrfx ingest [path] [--force]`, `mrfx status`,
`mrfx export out.csv [--cpt … --payer … --grain tin]`, `mrfx outreach`,
`mrfx forget <filename>`, `mrfx speedtest <url>`,
`mrfx orgreport <subject>` (one-practice bundle for the Medicare Order &
Referring Tracker), `mrfx medicare --from-tracker` (find the Tracker's data
folder and import everything new; `--eligibility/--referrals <path>` point at
files by hand) — the eligibility and referral halves MRFs cannot supply. The
dashboard's Medicare tab does the same import in-process, so the user never
stops the server; see `mrfx/tracker.py` for the discovery contract.
`mrfx backup <zip>` / `mrfx verify [zip]` (checksummed store backup + integrity check),
`mrfx reset --confirm`.
Config: `config/mrfx.yaml`.

Playwright/Chromium for JS portals is optional and auto-downloaded on first use
(`render_auto_install`); the environment ships it at `/opt/pw-browsers`, so do
NOT run `playwright install` here.

## The invariants you must not break (full detail in AI_HANDOFF Step 3)

1. **Streaming only** — never `json.load` an MRF; peak memory must not scale with
   file size (verified ~34 GB uncompressed at <1 GB RSS).
2. **Single DB writer** — only the main process touches DuckDB; parallel parser
   workers write `*.parquet.tmp` and hand back metadata.
3. **Fault isolation** — a bad file/URL marks ITS row failed with a plain-language
   message and the worker moves on; a typo in any user-editable file never kills
   the program; cosmetic helpers (progress bars, QA) never fail real work.
4. **Honesty contract** — never fabricate numbers or provenance; skip
   drug/allowed-amounts files with the reason; 0-row files stay 0; SSN-pattern
   TINs are masked everywhere; exports carry a methodology sidecar (ZIP/CLI).
5. **Numbers correctness IS the product** — bundle/capitation excluded; modifiers
   sorted+deduped; `file_month` = header→filename→ingest; untyped 10-digit TIN is
   an NPI-in-the-slot; entity rate = median of member-TIN rates.
6. **Idempotency / crash-resume** — kill at any instant; re-ingest atomically
   replaces a file's part (never duplicates rows); content-sha dedup skips
   byte-identical twins; one atomic content-claim means exactly one twin ingests.

## Ground rules

Commit messages: what + why + verified-with-numbers. Big claims require a live
run — "should work" is not done. When a stress test finds a failure, fix the
class and leave a regression test. Never weaken TLS. Develop and push only to the
branch named in your task instructions. Trials live OUTSIDE the repo (a
scratchpad) with their own config, never pointed at the user's real store; guard
driver scripts with `if __name__ == "__main__"` (spawned parser workers
re-import `__main__`).

For everything else — the module-by-module map, the exact pipeline states, the
per-payer extension recipe, the source catalog, and known limitations — see
**`docs/AI_HANDOFF.md`**. User-facing setup and troubleshooting live in
`GETTING_STARTED.md`; the product overview is in `README.md`.
