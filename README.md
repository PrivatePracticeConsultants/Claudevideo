# Payer MRF tooling: extraction pipeline + MRF Explorer dashboard

Two sibling tools in one repo:

1. **[MRF Explorer](#mrf-explorer--drop-in-payer-rate-dashboard)** (`mrfx`) — drop
   *any* payer's Transparency-in-Coverage in-network files into a folder and get a
   local analytics dashboard at the (code × TIN) grain: filters, sorting, CSV+methodology
   export, market benchmarks, opportunity model, and client-ready pitch reports.
2. **[BCBS-MO extraction pipeline](#bcbs-mo-mrf--bcbs-missouri-ptrehab-negotiated-rate-extraction)**
   (`run.py`) — targeted crawler/extractor for the two Missouri BCBS licensees:
   discovers their MRFs, filters to a target NPI set, writes Parquet.

> **AI agents / new engineers:** start with [`CLAUDE.md`](CLAUDE.md) (60-second
> orientation), then read [`docs/AI_HANDOFF.md`](docs/AI_HANDOFF.md) in full
> before making any substantive change — it is the authoritative module map,
> invariant list, and per-payer extension recipe, verified against the code.
> Non-technical setup lives in [`GETTING_STARTED.md`](GETTING_STARTED.md).

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
  │ pitch report / CSV    │ ← │ dashboard :8377      │ ← │ DuckDB/Parquet store │
  │ (+ methodology)       │   │ explore → benchmark  │   │ (code × TIN spine)   │
  └───────────────────────┘   └──────────────────────┘   └──────────────────────┘
```

## Quickstart

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt && .venv/bin/pip install -e .
.venv/bin/mrfx serve            # dashboard at http://localhost:8377 + inbox watcher + URL worker
# easiest path: paste payer links in the Files tab, or from the terminal:
.venv/bin/mrfx add https://tcr.bcbsms.com/Table_of_Contents/Local_TOC.json
# or drop files you already have:
cp ~/Downloads/2026-07-01_someplan_in-network.json.gz data/inbox/
```

Other commands: `mrfx add <url>… [--file links.txt] [--retry-failed]`,
`mrfx preflight <path>`, `mrfx ingest [path] [--force]`, `mrfx status`,
`mrfx export out.csv [--cpt 97110 --payer ... --grain tin]` (writes a
`*_methodology.txt` sidecar), `mrfx reset --confirm`. Config in `config/mrfx.yaml`.

## The grain: (billing_code × TIN), NPI for drill-down

TIN is the entity/practice grain that matters for M&A and negotiation — a practice
bills one TIN across many rendering NPIs, and payers contract at the TIN level.
NPPES does not expose tax IDs, so the MRF itself links NPI→TIN: the parser
preserves each provider group's NPI↔TIN pairing, `rates_by_tin` aggregates on
TIN, and the dashboard drills down to the NPIs underneath. Caveats handled:

- `tin.type == "npi"` means the published "TIN" is really an NPI — those rows are
  flagged `tin_is_really_npi` with a filter to hide them from entity rollups.
- SSN-pattern 9-digit TINs are masked (`MASKED-SSN`) in the UI and every export.
- A TIN showing multiple distinct rates for the same (code, modifier-set, class,
  POS, month) tuple is flagged (`rate_variants > 1`) — the shown rate is the
  median of the variants, never a silent pick.
- TIN display names roll up from the NPPES org names of the TIN's Type-2 NPIs;
  TINs with only individual NPIs are labeled `individual-billed`.
- **Entity grain rolls tax IDs up into organizations automatically**: switch the
  grain toggle to *Entity* and TINs whose providers resolve to the same NPPES
  organization name become one row — a chain billing under several tax IDs shows
  as a single entity, rate = median of its member-TIN rates, with the constituent
  TINs listed in the detail. No manual setup; it just needs the provider-name
  lookup to have run (an un-named TIN is its own entity). `config/entity_map.yaml`
  is the manual OVERRIDE on top — for grouping TINs NPPES names differently
  (pro/facility splits, legacy tax IDs, a consolidator's mixed brands). Editable
  from the API/UI. (Caveat: auto-grouping keys on the exact org name, so two
  genuinely-unrelated practices registered under the identical generic name would
  merge — use the manual map to split them if you hit that.)

## PT / OT / SLP code set & modifiers

The default code set ships all three disciplines (evals, treatment, modalities,
swallowing, AAC — see `mrfx/catalog.py`), each tagged with a short description,
its discipline(s), and **timed vs untimed** (most treatment codes are 15-minute
units). Shared codes (97110 appears for PT and OT alike) are attributed by the
**therapy discipline modifier** — GP (PT) / GO (OT) / GN (SLP) — never guessed:
an unmodified shared code is bucketed as *discipline unspecified*, a visible
filter value. Assistant modifiers **CQ/CO** (85% payment) are never collapsed
into base rates; KX and 59/XE/XS/XP/XU are preserved and filterable
(`mod_has=GP&mod_not=CQ` = clean PT base rate).

## Analytical safeguards (§7A)

- **Per-visit disclaimer** pinned in the UI: a negotiated rate is per code, not
  per visit (timed units, MPPR, CQ/CO reductions, sequestration, cost-share).
- **Per-file QA report** after every ingest, shown in the Files view: `$0`/`$0.01`
  placeholders, outliers (>5x / <0.2x the code's within-file median), non-dollar
  share, non-CPT/HCPCS types, multi-code fields, duplicate-explosion ratio.
- **Outlier hiding is opt-in** and labeled with its rule — rows are never dropped
  silently.
- **Point-in-time**: every row carries `file_month`; months accumulate rather than
  overwrite; a month picker pins figures to a publication, and the code view
  shows median-by-month trend.
- **Provenance**: every export row carries payer, source_files (one
  representative file per row — source_count gives the total number of files
  behind it), file_month, last_updated_on, schema_version; every export has a methodology sidecar
  (`.zip` from the dashboard, `*_methodology.txt` from the CLI) recording
  filters, grain, dedup rule, outlier setting, code set, and app version.
- **Validation cross-check** (Files view): paste a TIN/NPI + code + a rate you
  know from a remit and see the extracted values beside it.

## Benchmarking & pitch reports (§7B)

The **Benchmark** tab answers "where does this practice sit vs its market":

- subject (mapped entity or TIN) + market definition (payers, state/city from the
  NPPES locations of each TIN's NPIs, discipline, class, POS, as-of vintage —
  **latest available by default**, a pinned month optional) → per-code subject rate vs market p10–p90, percentile position strip,
  and the dollar gap to a target percentile (median default, p75 selectable).
- **Peer sets**: auto (all entities matching the market) or curated (named, saved
  TIN lists — "the consolidator clinics near the subject"); reports state which.
- **Opportunity model**: `(target rate − subject rate) × annual units`, using
  owner-supplied volumes only (paste `code, units` lines) — never invented —
  with a conservative band (p40) beside the target figure.
- **% of Medicare**: load a CMS MPFS extract (`code, locality, non_facility_rate`
  CSV) and every benchmark gains subject/market %-of-Medicare columns. Optional;
  never estimated.
- **Pitch report**: print-ready HTML per subject with the percentile table,
  strips, opportunity band, peer-set definition and a mandatory methodology
  footer (files/months, filters, dedup rule, caveats). It always states its
  as-of vintage (latest available by default, or the pinned month).
- **Payer-negotiation one-pager**: the same benchmark, split **per payer** — for
  each payer the subject actually contracts with, a table of the subject's rate
  versus *that payer's other providers only* (intra-payer peers, subject
  excluded), ordered weakest-position-first so the strongest renegotiation case
  leads. With volumes supplied it totals the annual dollar gap across payers.
  This is the deliverable a practice takes into a contract renewal.
- **State scope is mandatory on reports**: negotiated reimbursement varies by
  state, so both the pitch report and the negotiation one-pager **refuse to
  render without a State filter** (the exploratory Benchmark tab is unaffected).
  A stateless comparison pools every loaded state into one distribution — to run
  that deliberately you pass `market.allow_national=true` (the dashboard asks for
  a confirmation), and the report then carries a prominent "NATIONAL COMPARISON"
  banner so it can never pass as an in-state benchmark. Note state comes from
  NPPES enrichment, not the MRF, so a state filter only includes TINs whose NPIs
  have been resolved to that state.
- **Report-as-a-service branding**: both reports carry the consultant's brand
  name and, when `report_branding.logo_path` points at a readable image, their
  logo inlined as a data URI (reports stay single self-contained files — no
  external fetch when a client opens them offline). A missing logo silently
  falls back to the text brand; it never blocks a report.

## Reference points beyond the MRFs

A commercial rate means more with something to measure it against. Four
comparisons, each from a source the app does not invent and each carrying the
limit that makes it honest:

- **Plan / network scoping.** A payer publishes one rate FILE shared by many
  plans, and the plan name lives only in its table-of-contents — so a payer's
  narrow-network and broad-PPO books blend into one median that matches neither.
  Expanding a TOC now captures which plans each file serves, and the Benchmark
  tab's plan picker (shown only when a store carries plan tags) scopes every
  number to one network. An unknown plan refuses rather than quietly handing
  back the pooled market, and a plan-scoped report cites only that plan's files.
- **Medicaid (the floor) and workers' comp (usually the ceiling).** Load your
  state's schedules as a `code,rate` CSV. "Your commercial rate is 4% above
  Medicaid" ends an argument faster than a percentile does. A state is required
  — a fee schedule is a state document — and a code the schedule omits is left
  out, never counted as $0.
- **The hospital across town.** CMS requires every hospital to publish its
  negotiated rates; the outpatient department bills the same 97xxx codes to the
  same payer, usually at a multiple. Both CMS file shapes (tall CSV and JSON)
  import. Hospital rates live in their own store and **never** enter the
  practice rate spine — a facility payment is a different product, and mixing
  one in would move every practice median in the app. Matched on payer name and
  code only, reported as a ratio, never as an amount owed.
- **Inflation.** Every other number here is nominal dollars, so a payer holding
  a rate flat since 2022 read as "flat" when it is a 6.7% pay cut. The payer
  trajectory now restates each payer's change in constant dollars. A year the
  price index does not cover is a refusal naming the missing year, never an
  extrapolation; an explicit percent-per-year assumption is allowed and is
  labelled an assumption everywhere it appears.

## How solid is the data behind a number

Three properties of a rate that the rate itself doesn't state, surfaced where
they matter:

- **Contracted vs derived.** `negotiated_type` separates a real contracted
  amount from one the payer constructed where it has no contracted figure. The
  composition is reported for a market and for a client's own rates — reported,
  not filtered, because excluding derived rates by default would silently move
  numbers already quoted.
- **Freshness.** Payers are compared on the `last_updated_on` they published,
  with three states rather than two: fresh, stale, and unreadable-or-unstated (a
  date we cannot parse must not pass as fresh, and must not be called stale).
- **Does size explain the gap?** "The practices above me are bigger" is the
  commonest objection to a benchmark, and it is testable: practices are ranked
  within (payer, code), then compared across provider-count bands. A band with
  too few practices is marked thin rather than reported.

## Territory and growth

- **Inside one state, by city** — state medians hide the metro/small-town split
  that decides where a client competes. Thin cities are suppressed *and counted*,
  so a suppressed city never reads as "nothing there".
- **Payer concentration (HHI)** — who holds the contracts here, with the DOJ
  bands. Under five payers it refuses to assign a band at all: with three payers
  the index calls every market concentrated by construction.
- **Referrals going to someone else** — physicians who send therapy patients to
  other practices in a client's own city and little or nothing to the client,
  ranked by the gap. Every other lead surface finds practices; this one finds
  the relationship to go and win.
- **Openings and closures** — therapy NPIs newly issued (a practice with no
  contracts yet, the earliest possible lead) and newly deactivated and not since
  reactivated (a closed referral source is a hole in a client's volume; a closed
  competitor is room).

## Rate card & payer scorecard (§7C)

The **Rate card** tab reconstructs a single practice's own negotiated schedule
— zero assumptions, no volumes:

- **Fee schedule ("your rate card")**: every code the practice has a negotiated
  rate for, laid out by payer, as of a pinned month. The MRF *is* the fee
  schedule; most practices have never seen theirs written down. The best rate in
  each row is highlighted.
- **Payer scorecard ("who pays best")**: the practice's payers ranked by how
  generously they pay, normalized fairly — median **% of Medicare** when an MPFS
  anchor is loaded (absolute, comparable across payers), otherwise median **% of
  the best payer** over head-to-head codes (needs no Medicare data; a payer with
  no shared codes is listed but unranked).
- **Deliverables**: a branded, print-ready **rate card** (scorecard on top, full
  fee-schedule matrix below, methodology footer) and a **CSV** with the
  methodology as leading comment lines. Same base-modifier, dollar-rate,
  professional-class basis as the benchmark, so the numbers agree across tabs.

**Honesty caveats baked into UI and reports:** benchmarks compute over
dollar-rate, base-modifier rows by default (deviations are labeled toggles);
**ghost rates** are real (a published rate ≠ the peer bills that code — hence
discipline-scoped code sets and subject-supplied volumes); **a published rate is
not proof a peer collects it** (contract vintages, lesser-of clauses) — market
positioning is directional. MRF data is public data published for exactly this
kind of third-party analysis; reports go to the subject practice about its own
position, not to coordinate rates between competitors (not legal advice).

## Messy files are expected

Real payer files arrive with shuffled key order, missing fields, extra fields
and loose types. Ingestion is best-effort with cleaning, never silent:

- **Order-independent**: `reporting_entity_name` / `version` / `last_updated_on`
  and `provider_references` may appear anywhere in the byte stream (before or
  after `in_network`) — rows are stamped with the final header values and
  late-arriving references resolve via deferral.
- **Type cleaning**: numeric billing codes (`97110`, `97110.0`), string rates
  (`"$34.50"`, `"34.50 USD"`), scalar-where-array fields (a lone `"GP"`
  modifier, `"11"` service code), dirty identifiers (`"43-111 1111"`,
  int/float NPIs, duplicate NPIs within a group), cased enums (`"CPT"`/`"cpt"`,
  `"Professional"`) all normalize.
- **Missing fields**: a price without a readable `negotiated_rate` is skipped
  (never written as $0.00); a target code with no `billing_code_type` is
  accepted best-effort with the family inferred from the code shape; a
  9-digit TIN with no declared type is treated as an EIN. Unknown extra fields
  are ignored.
- **Every salvage decision is QA-counted** per file (unparseable rates, missing
  code types, invalid NPIs, bad reference ids) and shown in the Files view.

## Outreach export (contact-list cross-referencing / Brevo mail merge)

`Outreach CSV` (explorer button), `GET /api/export/outreach.csv`, or
`mrfx outreach contacts.csv [--payer ... --state MO --cpt 97110,97140 --month 2026-06]`
produces **one row per entity**, shaped for matching against your own contact
list and mass mail merge:

- **Join keys**: `ORG_NAME` plus geography from the NPPES enrichment of the
  entity's NPIs — `CITY`, `STATE`, `ZIP`, `ADDRESS`, `PHONE`. `WEBSITE` is
  included as an always-empty column for template consistency (neither MRFs nor
  NPPES publish websites — fill it from your own list).
- **Merge fields** per code (Brevo-attribute-safe names, letter-first
  UPPER_SNAKE): `C97110_RATE`, `C97110_MKT_MEDIAN`, `C97110_PCTL`,
  `C97110_GAP_TO_MEDIAN` — enough for a template like *"your 97110 rate sits at
  the {{C97110_PCTL}}th percentile in your market; the median practice gets
  ${{C97110_GAP_TO_MEDIAN}} more per unit."* Codes come from the active code
  filter (else the most-covered codes, capped at 10).
- Honors the explorer's current filters (payer, state/city, month, discipline,
  base-only), computes over dollar-rate rows, masks SSN-pattern TINs, writes a
  UTF-8 BOM for Excel/Brevo, and ships a methodology sidecar. Filter to one
  payer + one month for the cleanest per-market numbers.
- Same responsible-use note as benchmarks: these emails go to each practice
  about **its own** market position.

## How to get MRF files

Use the dashboard's **Sources** tab: pick a state → its BCBS licensee(s) (all of
them in multi-Blue states — CA/ID/KS/MO/NY/PA/VA/WA) plus the national payers,
each with its MRF entry point. Elevance/Anthem states share one national master
index — the tab says so. **Honesty contract**: `verified: true` entries were
confirmed live; `verified: false` render with an "unverified — confirm link"
badge and are never displayed as authoritative; confirming a URL persists it to
`config/registry_overrides.yaml` and flips the badge locally.

## Known sources: the app remembers where it's been

`config/known_sources.yaml` is a shipped catalog of every payer entry point
this app has been live-tested against — each entry carries its verification
date and what happened (files listed, rows ingested, quirks). 38 entries are
**auto-queueable** (Highmark's 15 hosted Blue plans incl. FL/AZ/ID/MN/LA/NE,
BCBS Mississippi's stable TOC, BCBS Tennessee's /tcr directory page,
Cigna's signed-manifest page, BCBS South Carolina's CloudFront indexes,
BCBS North Carolina's signed TOC page, CareFirst's Azure-blob indexes,
Molina Healthcare's all-states page, Kaiser Permanente's regional
indexes, Aetna's HealthSparq portal, Harvard Pilgrim's click-gated plan
list, Regence's JS-built page, and Oscar's file page (those six JS-built
pages via the headless-browser renderer; `config/starter_links.txt`
swaps Oscar for its bounded monthly S3 index, which needs no browser),
Anthem/Elevance's
14-state master index (10.5 GB — needs confirm_over_gb: 12),
Centene/Ambetter's all-states page, the Sapphire hubs of Blue KC, BCBS
Michigan, and BCBS Louisiana, UnitedHealthcare's national portal,
SelectHealth's per-plan index directory (UT/ID/NV), Moda Health's
first-of-month TOCs (OR/AK/TX), the First Health PPO rental network
via Healthgram's stable TOC, Tufts Health Public Plans (MA), and Security
Health Plan's per-employer zip-TOC listing (WI)); the
rest (34) are portals that need a browser click (Humana, HCSC,
BCBS AL/MA/RI/VT/KS, Wellmark, Horizon NJ,
Premera, Priority Health, HMSA, Blue Shield
of CA, Excellus, Capital BC, IBX, BCBS MN, UHS, plus a batch from the
mrfdatasolutions directory — Christus, HAP, Health New England, SummaCare,
Univera, ConnectiCare, CommunityCare OK, Network Health WI, Optima, UPMC,
Viva, Capital Health FL, Western Health, EmblemHealth — probed;
JavaScript-only or firewalled) with **step-by-step manual instructions in
each entry's Notes** (open in a browser, reach the file list, right-click the
`.json`/`.json.gz`/TOC links, Copy link address, paste into the Files tab). Monthly-dated URLs carry
a `{FIRST_OF_MONTH}` placeholder resolved at queue time so the catalog never
goes stale. Load them via `mrfx add --known`, the dashboard's **"Queue tested
payer indexes"** button, or browse with **"Show tested sources"**
(`GET /api/known-sources`, `POST /api/urls/known`). Your own history is
separate and automatic: everything you queue/ingest persists in the store's
`url_queue` and `files` tables across restarts.

## Paste links, get data (URL-drop ingestion)

You don't have to download files by hand. Paste links into the **Files** tab's
"Paste file links" box (or run `mrfx add <url>`), and the app handles the rest.
Three link kinds are auto-detected:

- **Direct rate file** (`…in-network-rates….json.gz`) — downloaded, ingested
  with the same chunked/streaming pipeline, raw download deleted afterwards
  (keep it with `delete_raw_after_ingest: false`).
- **Table of Contents / index JSON** (e.g.
  `https://tcr.bcbsms.com/Table_of_Contents/Local_TOC.json`) — expanded and
  every in-network file inside queued automatically (verified live: 466 files
  from the BCBS-MS TOC).
- **Plain file-listing page** (e.g. a folder on `https://mrfdata.hmhs.com`) —
  file links lifted from the HTML and queued, TOCs found there cascade too
  (verified live: a Highmark Delaware listing page fanned out to 8,000+
  queued files).
- **Sapphire/Gatsby MRF hubs** (`*.sapphiremrfhub.com` — Blue KC and other
  HealthSparq-hosted payers): the page itself is empty JavaScript, but the
  app fetches the hub's static data the way the browser would and queues
  every TOC it lists, honoring the payer's `is_suppressed` flags (verified
  live: Blue KC → 2 TOCs → 674 files → 2,573,672 rows ingested).
- **UHC/Optum React portals** (`transparency-in-coverage.uhc.com` /
  `.optum.com`): pasting the portal root probes the platform's well-known
  file-listing API (`/api/v1/*/blobs/`) and expands the response like an
  index — in-network rate files queue first, allowed-amounts and drug-pricing
  (NDC / prescription-drugs) entries are never queued, and the cap is
  `max_toc_files` (verified live: 86,722 files listed; the UHC Missouri
  Provider Network file ingested 88,649 rows).
- **JavaScript-only portals** (Molina, HealthSparq-hosted Blues, and other
  pages whose file lists exist only after scripts run): with the optional
  Playwright install (`pip install playwright && playwright install
  chromium`) the app renders the page in headless Chromium and harvests
  links from the rendered DOM **and** from the JSON responses the page's
  own scripts fetch — and when nothing appears, it dismisses consent
  overlays and clicks the controls that look like they reveal the list
  ("View Plan List" — how Harvard Pilgrim works). The browser only
  renders; every network request is
  made by the app's normal HTTP stack (proxy- and CA-aware, TLS verified).
  Verified live: Molina's page → 19 state indexes → 3,431 files queued,
  first California files ingested. Without Playwright these pages fail
  with a message that includes the install command.

**Parallel processing**: `parallel_ingests` in `config/mrfx.yaml` (default
`0` = auto: your CPU cores minus one, capped at 8; a positive value is
honored but still clamped to cores−1) parses that many files at once —
each in its own OS process, while the database stays strictly single-writer
in the main process. `parallel_downloads` (default `0` = auto: 6, the per-host
connection limit a browser uses — deliberately NOT tied to core count, since
downloading waits on the network rather than the CPU) fetches that
many files concurrently; simultaneous downloads each reserve their
remaining bytes so together they can never overcommit the disk — the
disk-space guard counts other in-flight downloads' reservations against
free space before starting a new one. `download_segments` (default `1` = off)
splits ONE file across that many HTTP byte-range connections, for the other
shape of slow: a queue of a few multi-GB files on a CDN that throttles each
connection. `mrfx speedtest <url>` measures a real link on 1/2/4/8
connections and says which of the two situations you're in rather than
leaving it to guesswork. Verified: 2 workers ran the same 3 UHC files 1.5x faster
with byte-identical row counts; a parser worker killed mid-parse restarts
automatically and the file retries; killing the whole app mid-run resumes
cleanly on restart with no duplicate rows. DuckDB's memory for analytics
rebuilds is capped (40% of RAM, clamped to 2–12 GB) and — because the
rollups' DISTINCT aggregates cannot spill to disk — very large stores
rebuild in hash-partitioned slices inside one transaction, with per-connection
temp-disk capped at 80% of free space, so rebuilds can't balloon into the
OOM killer or fill the disk.

Every ingest is itself a **scan for your codes**: the parser streams the file
and extracts only the billing codes in `config/mrfx.yaml` (`codes.cpt_codes`),
so a file without them honestly records 0 rows. For huge two-pass files the
scan pass now **short-circuits**: if pass 1 finds none of your target codes
anywhere in the file, the extraction pass is skipped and the file is marked
done with "none of the target billing codes appear in this file" — half the
parse cost on every no-match multi-GB file.

The queue (`url_queue` in the store) processes **a bounded few files at a
time** (`parallel_ingests`, default auto = cores−1) in the
background, dedupes re-pasted links (signed-query variants included), and also
dedupes by **content**: every download is sha256-hashed, and a byte-identical
file arriving under a different domain is skipped, not re-ingested. This
matters for aggregation — Blue plans host copies of each other's national
files (verified live: the same "Arkansas BCBS" shard appears in the WV,
Nebraska, and Western-NY indexes under three domains; without content dedup a
4-state trial picked up ~13% duplicate rows). It shows a download-MB progress
bar per row, survives restarts (in-flight rows are re-queued on startup), and
gives plain-language errors: expired signed links
("HTTP 403 — access refused. Usual causes: a signed URL expired (re-copy a
fresh link, or re-add the TOC it came from)…"),
JavaScript-only portals (with instructions to click through and paste the real
links), allowed-amounts files ("no negotiated rates — skipped"), and an
oversize guard (`confirm_over_gb`, default 5 GB compressed) so a typo can't
fill the disk. TLS verification is never disabled; incomplete corporate cert
chains are repaired via AIA and verified against the system trust store.

## Companion provider-reference files

Many payers publish rate groups that cite integer `provider_references` resolved
against a reference table — embedded in the same file or shipped separately. If
the companion is missing, affected groups are **counted and surfaced** ("N rate
groups skipped — missing provider reference file"), never silently dropped;
dropping the companion in later re-ingests the affected files automatically.
`mrfx preflight <file>` reports type / payer / schema / month / size / parse
estimate and the companion verdict (`READY` / `NEEDS COMPANION` /
`NOT A RATE FILE` / `UNREADABLE`) from the header alone.

## Volume expectations

In-network files run 1–100+ GB uncompressed (some ~1 TB), and one licensee's
data is usually split across many shards (codes divided across files). mrfx
streams with **constant memory** — the parser flushes rows to the Parquet part
in 50k-row batches and never holds the whole file, so a 275 MB shard that
expands to 2.1M extracted rows parses in ~130 MB of Python memory (verified on
a live Blue Cross Blue Shield of North Dakota file). DuckDB spills its rollup
work to a temp dir under the store, so ingestion won't OOM regardless of file
size or machine RAM. Parse time ≈ tens of MB/s (preflight estimates it). The
default code set keeps row counts modest; `codes.all_codes: true` ingests
everything — expect orders of magnitude more. Browser uploads cap at 1 GB;
bigger files go straight into `data/inbox/`. NPI enrichment runs in the
background via NPPES (or a local bulk CSV, or off). Because codes are sharded
across files, drop **all** of a licensee's in-network shards to see a code that
isn't in the first one.

### Very large files: chunked two-pass ingest + progress bar

Some payers embed a **giant `provider_references` table** in the in-network
file — one real Anthem Colorado shard carries 4.2M reference entries / 18.5M
provider groups. Holding that whole table in memory to resolve references would
need ~16 GB and OOM. For files past ~1.5 GB uncompressed, mrfx switches to a
**two-pass chunked ingest**:

1. **Pass 1 (skim)** streams the file and records only the reference ids the
   *target* codes actually cite — a small therapist-relevant subset.
2. **Pass 2 (extract)** keeps only that subset in memory while streaming rows to
   the Parquet part in batches.

The file is worked through in 64 MB compressed **chunks**, and a **progress bar**
(chunk N/total, both passes) shows in the CLI (`mrfx ingest`) and live in the
dashboard's Files view. Verified end-to-end on the 8 GB (uncompressed) Anthem CO
shard: **1.06M rows in ~6.5 min at ~1.2 GB peak** (down from a 16 GB OOM), with
the progress bar advancing to 100%.

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
