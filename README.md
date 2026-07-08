# Payer MRF tooling: extraction pipeline + MRF Explorer dashboard

Two sibling tools in one repo:

1. **[MRF Explorer](#mrf-explorer--drop-in-payer-rate-dashboard)** (`mrfx`) — drop
   *any* payer's Transparency-in-Coverage in-network files into a folder and get a
   local analytics dashboard: org × CPT × modifier rate table, filters, sorting,
   CSV export. Payer-agnostic and file-driven.
2. **[BCBS-MO extraction pipeline](#bcbs-mo-mrf--bcbs-missouri-ptrehab-negotiated-rate-extraction)**
   (`run.py`) — targeted crawler/extractor for the two Missouri BCBS licensees:
   discovers their MRFs, filters to a target NPI set, writes Parquet.

---

# MRF Explorer — drop-in payer rate dashboard

```
          Sources tab                    you                       mrfx
  ┌───────────────────────┐   ┌──────────────────────┐   ┌──────────────────────┐
  │ pick state → licensee │ → │ download MRF (.json/  │ → │ data/inbox/          │
  │ + national payer URLs │   │  .json.gz/.zip)       │   │  watcher/`mrfx ingest`│
  └───────────────────────┘   └──────────────────────┘   └──────────┬───────────┘
                                                                    │ preflight → parse
                                                                    ▼
  ┌───────────────────────┐   ┌──────────────────────┐   ┌──────────────────────┐
  │ CSV export (=view)    │ ← │ dashboard :8377      │ ← │ DuckDB/Parquet store │
  └───────────────────────┘   └──────────────────────┘   └──────────────────────┘
```

## Quickstart

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt && .venv/bin/pip install -e .
.venv/bin/mrfx serve            # dashboard at http://localhost:8377 + inbox watcher
# in another shell (or just drag files onto the dashboard's Files view):
cp ~/Downloads/2026-07-01_someplan_in-network.json.gz data/inbox/
```

Other commands: `mrfx preflight <path>` (inspect before a long parse),
`mrfx ingest [path] [--force]`, `mrfx status`, `mrfx export out.csv [--cpt 97110 --payer ...]`,
`mrfx reset --confirm`. Config in `config/mrfx.yaml` (code set, payer name
normalization, `move_processed`, enrichment mode, `confirm_over_gb`, port).

## How to get MRF files

Use the dashboard's **Sources** tab: pick a state → it shows that state's BCBS
licensee(s) (all of them in multi-Blue states — CA/ID/KS/MO/NY/PA/VA/WA) plus
the national payers (UnitedHealthcare, Aetna, Cigna, Centene, Humana, Kaiser),
each with its MRF entry point. Elevance/Anthem states share one national master
index — the tab says so instead of implying per-state downloads. Download the
in-network file(s) you care about and drop them in `data/inbox/`.

**The registry's honesty contract** (`config/payer_registry.yaml`): entries with
`verified: true` were confirmed pointing at a live MRF page; `verified: false`
entries render with an "unverified — confirm link" badge and a one-click search;
they are never displayed as authoritative. When you confirm a URL, paste it into
the card — it persists to `config/registry_overrides.yaml` and flips the badge
to "verified (locally)".

## Companion provider-reference files (read before dropping files in)

Many payers don't embed provider groups in the in-network file. Instead, rate
groups cite integer `provider_references` ids that resolve against a reference
table — either embedded at the top of the same file, or shipped as a **separate
provider-reference file**. If you ingest an in-network file without its
companion, every rate group whose ids can't be resolved is **counted and
surfaced** ("N rate groups skipped — missing provider reference file") in the
Files view and CLI — never silently dropped. Drop the companion in afterwards
and mrfx re-ingests the affected files automatically.

`mrfx preflight <file>` tells you *before* a multi-hour parse: file type
(rate / reference / TOC / unknown), payer, schema version, `last_updated_on`,
size + parse-time estimate, whether it uses provider references, and whether a
matching companion (same payer, same month) is already present. Verdicts:
`READY` / `NEEDS COMPANION` / `NOT A RATE FILE` / `UNREADABLE`. TOC/index files
are never parsed for rates — preflight points you to the in-network URLs they
list.

## Volume expectations

- In-network files are commonly **1–100+ GB uncompressed** (UHC Choice Plus
  ~86 GB; some Cigna files ~1 TB). mrfx streams with constant memory, but parse
  time is roughly proportional to size (~tens of MB/s) — preflight prints an
  estimate, and files above `confirm_over_gb` wait for explicit confirmation.
- With the default PT code set, even huge files usually yield modest row counts
  (thousands–millions). `codes.all_codes: true` ingests **every** billing code —
  expect orders of magnitude more rows and disk.
- Browser uploads are capped at 1 GB; bigger files go straight into `data/inbox/`.
- NPI → org-name enrichment runs in the background via the NPPES API (names fill
  in as they resolve), or point `enrichment: bulk` at a local NPPES
  Data Dissemination CSV for offline enrichment, or `off`.

Non-dollar rows (`negotiated_type` = percentage / per diem) are tagged
`is_dollar_rate = false` and excluded from rate stats by default — the
"dollar rates only" toggle includes them, clearly labeled. Modifiers survive
end-to-end into the CSV export. Exports mirror the active filter state exactly
(same SQL), UTF-8 BOM for Excel, arrays `;`-joined.

---

# bcbs-mo-mrf — BCBS Missouri PT/Rehab negotiated-rate extraction

Extracts commercial negotiated reimbursement rates for targeted physical-therapy /
rehab provider groups in Missouri from Blue Cross Blue Shield
Transparency-in-Coverage (TiC) machine-readable files (MRFs), into a queryable
Parquet dataset keyed by NPI, CPT/HCPCS code, modifier, plan, and rate.

Built against **CMS TiC schema v2.0**
([spec + examples](https://github.com/CMSgov/price-transparency-guide)). Files
declaring a `1.x` version are logged and skipped, never parsed.

## The two-licensee split

"BCBS in Missouri" is two separate licensees; both are handled:

| Licensee | Coverage | Entry point | Discovery |
|---|---|---|---|
| **Anthem Blue Cross and Blue Shield** (`anthem_mo`) | most of MO incl. St. Louis | [anthem.com/machine-readable-file/search](https://www.anthem.com/machine-readable-file/search) | **EIN / employer-group gated** — resolved per group at runtime (below) |
| **Blue Cross and Blue Shield of Kansas City** (`blue_kc`) | ~30 western-MO KC-metro counties | [bcbskc.sapphiremrfhub.com](https://bcbskc.sapphiremrfhub.com) | hub crawled for all listed TOC files |

### How discovery actually works (verified July 2026)

**Blue KC** — the Sapphire hub is a Gatsby site whose TOC listing is baked into
its static-query data. The crawler fetches
`/page-data/index/page-data.json` → `staticQueryHashes` →
`/page-data/sq/d/<hash>.json` → `data.allTocsJson.edges[].node.url`, then parses
each TOC (`reporting_structure[]` → `in_network_files[].location`), de-duplicating
in-network URLs across plans (a July 2026 crawl yields ~676 unique files from
61 TOCs). Nothing is hardcoded beyond the hub URL in `targets.yaml`.

**Anthem** — there is no flat state index in the UI. The pipeline reproduces the
portal's own search flow at runtime:

1. `GET {region}/status.json` on the primary S3 root to pick the live region
   (falls back to the secondary region exactly like the portal's `script.js`).
2. For a group given by **name**: fetch `{region}/namesearch/<first-letter>.json`
   and match against its `{name, ein}` entries.
3. For each resolved **EIN**: fetch `{region}/anthem/<9-digit-ein>.json`, which
   lists the group's `In-Network Negotiated Rates Files` and (optionally)
   `Blue Cross Blue Shield Association Out-of-Area Rates Files`.

A group's file list spans every state Anthem operates in;
`payers.anthem_mo.file_include_patterns` (default `["MO_", "anthembcbsmo"]`)
keeps only Missouri-relevant files. Groups that fail to resolve are logged and
skipped — the run never crashes on one bad group. If **no** employer groups are
configured, the Anthem branch prints guidance and exits gracefully.

### Finding an Anthem employer EIN

The EIN is the plan sponsor's federal tax ID (9 digits, e.g. `43-0653611`):

- Ask the employer's HR/benefits team, or read it off box `b` of any W-2 from
  that employer.
- Search the employer's **Form 5500** filing (every ERISA group health plan
  files one): [DOL EFAST2 5500 search](https://www.efast.dol.gov/5500search/) —
  the sponsor's EIN is on the first page.
- Nonprofits: the EIN is on their IRS Form 990
  ([ProPublica Nonprofit Explorer](https://projects.propublica.org/nonprofits/)).
- Or just put the employer's `name` in `targets.yaml` — the pipeline resolves
  name → EIN through the portal's own name-search index and warns when a name
  is ambiguous.

Then add it to `config/targets.yaml`:

```yaml
payers:
  anthem_mo:
    employer_groups:
      - ein: "43-0653611"
      - name: "washington university in st louis - ship"
```

## Setup & run

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

.venv/bin/python run.py --config config/targets.yaml            # full pipeline
.venv/bin/python run.py --config config/targets.yaml --stage npi     # target set only
.venv/bin/python run.py --config config/targets.yaml --stage toc     # + discovery only
.venv/bin/python run.py --config config/targets.yaml --limit-files 3 # smoke test
.venv/bin/python -m pytest tests/                                # test suite
```

Query the output:

```bash
.venv/bin/python -m src.query data/out rate-distribution   # min/p25/median/p75/max per code per payer
.venv/bin/python -m src.query data/out provider-rates 97110 97140
.venv/bin/python -m src.query data/out payer-comparison    # Anthem vs Blue KC, overlapping NPIs
.venv/bin/python -m src.query data/out modifier-breakout   # base vs CQ/GP/... rows
```

## NPI target set (NPPES)

`src/npi_resolver.py` queries the NPPES API
(`https://npiregistry.cms.hhs.gov/api/?version=2.1`) for `NPI-2` organizations in
MO. The API only accepts *description text* in `taxonomy_description` (raw codes
are rejected), so it queries by description and post-filters results against the
exact taxonomy codes/prefixes in `targets.yaml` (`2251*` PT + sub-specialties,
`261QP2000X` PT clinic, `261QR0400X` rehab clinic). Hand-curated
`explicit_providers` (NPI + optional TIN/org name) are unioned in and win on
conflict. Result is persisted to `data/raw/target_npis.parquet`.

**API ceiling:** NPPES returns max 200/page and caps `skip` at 1000, so one
distinct query yields at most ~1,200 rows. Queries that hit the cap emit a loud
truncation warning suggesting `cities` splits. (July 2026: the three statewide
MO queries return 758/427/153 results — comfortably under the cap — for a
target set of ~1,117 organizations.)

**Bulk-file fallback (not built, drop-in):** to remove the cap entirely, swap
the resolver to the NPPES monthly Data Dissemination CSV: keep rows where
`Entity Type Code = 2`, `Provider Business Practice Location Address State Name
= MO`, and any `Healthcare Provider Taxonomy Code_1..15` is in the taxonomy
set. Emit the same `{npi, tin, org_name}` parquet and nothing downstream
changes.

### Type-1 vs Type-2 NPIs (read this before trusting recall)

NPPES redacts organization EINs (`<UNAVAIL>`), so NPPES-derived targets carry
**no TINs**. Meanwhile, **Blue KC's `provider_references` enumerate individual
(Type-1) NPIs grouped under a business TIN** — verified against live files —
so an org-only NPI set can intersect nothing there. Two remedies, use either or
both:

- Supply TINs for the groups you care about via `explicit_providers` — the
  extractor matches **NPI first, TIN second**, and a TIN match emits every NPI
  billing under that TIN (verified live: real Blue KC rates extract this way).
- Set `npi_targets.include_individuals: true` to also resolve NPI-1
  practitioners (same taxonomy filter). The statewide NPI-1 pool is large, so
  add `cities` or expect truncation warnings.

## Extraction guarantees

- **Streaming only** — in-network files (often 100s of GB uncompressed) are
  parsed with `ijson` over a gzip-aware HTTP stream; `json.load()` is never
  called on them. Non-target billing codes are abandoned mid-stream, so they
  cost almost nothing.
- **`provider_references` are first-class** — both inline `provider_groups`
  and integer `provider_references` resolve to NPIs/TINs, including
  `location`-style remote reference files (fetched lazily) and reference
  arrays that appear *after* `in_network` in the payload (target-code rate
  groups defer and resolve at end-of-file). Real Blue KC files are ~100%
  reference-based, so this path is load-bearing.
- **Modifiers survive** — `billing_code_modifier` is emitted per price row
  (`|`-joined when a price lists several), enabling CQ/GP/GO/GN breakouts.
- **Resumable** — each source file gets a checkpoint under
  `data/raw/checkpoints/`; completed files are skipped on re-run
  (`--retry-failed` re-attempts failures). TOCs/EIN files are cached dated
  under `data/raw/tocs/`.
- **Polite & fault-isolated** — single-threaded, retry with exponential
  backoff on 429/5xx; 403/404, truncated gzip, and malformed JSON mark that
  file failed and the batch continues.
- G0283 (in the default code set) is HCPCS, so both `CPT` and `HCPCS`
  `billing_code_type` values are accepted for configured codes.

## Output

Partitioned Parquet, `data/out/payer=<payer>/date=<yyyy-mm>/part-<urlhash>.parquet`,
written with an explicit pyarrow schema (`src/writer.py`). `payer` and `date`
live in the hive partition path and materialize as columns on read
(`src/query.py` sets `hive_partitioning=1`). Columns:

`source_file_url, last_updated_on, npi, tin, org_name, billing_code,
billing_code_type, billing_code_modifier, negotiated_rate, negotiated_type,
billing_class, service_code[], plan_name, plan_id, extracted_at`
(+ `payer`, `date` from the partition path).
