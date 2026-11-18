# Outputs review — what a private practice owner needs (Nov 14, 2026)

Written after 0.8.5, against a real store: BCBS Tennessee "Network P"
(34 files, 6,203,043 rates, 78 codes, 5,660 tax IDs, 49,663 NPIs named from the
November 2026 NPPES file, CMS Doctors and Clinicians file of 2026-10-30), plus
the Medicare Physician & Other Practitioners (by Provider and Service, 2024)
rows for 97110 in Tennessee.

## 1. What the MRF data can and cannot answer

A payer's in-network file says: *this payer pays this tax ID (and these NPIs)
this rate for this code, with these modifiers, in this place of service, as of
this month.* That supports exactly three kinds of answer well:

1. **Your rate vs. the market** — for each payer and code, where your rate
   sits among other practices the same payer prices (median, P25/P75, your
   percentile, the $ gap).
2. **Payer vs. payer** — which payer pays you best for the same care (rate
   card, payer scorecard, the value of one standard visit).
3. **Named evidence for a negotiation** — the specific practices near you that
   the same payer pays more for the same codes.

It does **not** contain volumes, collections, denials, plan mix or patient
counts. Everything that needs those (dollar opportunity, growth, market
sizing) leans on other files (Medicare volumes, Census) or on numbers the user
types — useful, but secondary and only as good as those inputs.

## 2. What the Tennessee data shows

| Finding | Number | Consequence |
|---|---|---|
| Tax IDs the payer prices for therapy codes | 5,660 | The "market" is mostly NOT therapy clinics |
| …classified today as therapy practices | 441 (7.8%) | the current `is_therapy` filter |
| …hospitals / SNF-type facilities | 89 / 257 | different rate levels (97110 median $30.46 at hospitals vs $24.23 at therapy practices) |
| …"other" (physician, multispecialty, chiro…) | 4,962 | pooled into the default comparison today |
| Tax IDs that list ANY organization NPI | 354 (6.3%) | payers mostly publish individual NPIs |
| …whose org NPI carries a PT/OT/SLP-clinic or therapist taxonomy (261QP2000X, 261QR0400X, 225…, 225X…, 235Z…) | 7 | an org-NPI taxonomy filter ALONE finds 7 clinics in Tennessee |
| Medicare 97110 rows in TN billed in the office vs. facility setting | 1,959 vs 1 | facility billing barely exists in this file (SNF / hospital therapy is on institutional claims), so "office vs facility" does not separate contract therapy — PRESENCE of private-practice professional billing does |
| Therapy-practice TINs with Medicare 97110 professional billing | 171 of 441 (39%) | strong positive evidence where present, silent otherwise |
| …hospital TINs / facility TINs with it | 6 of 89 / 1 of 168 | hospital and SNF therapists show little or no professional billing — as expected |

**Conclusion:** "outpatient rehab clinic" must be decided from several
signals together, each with its own coverage. No single one is enough.

## 3. The outputs today — verdict

The app has 14 views, ~40 panels and ~75 output routes, built over many
rounds largely for a *consultant* (clients, monthly packets, prospect leads,
market reports to sell). For a practice owner most of that is noise.

| View / panel | Verdict | Why |
|---|---|---|
| Rate card (rate card, payer scorecard, price a visit, visit value + ranked practices) | **Keep — core** ("My rates") | the owner's own rates, by payer, vs. the market |
| Benchmark (market benchmark, opportunity, weighted position) | **Merge into My rates** | same question as the rate card from another angle; two screens disagree on defaults |
| Negotiate: payer negotiation, rate proposal, payer comparison | **Keep — core** ("Negotiate") | the named-evidence screen; make "clinics paid more nearby" its center |
| Negotiate: walk-away leverage | **Keep, inside Negotiate** | needs real ZIPs; useful as one line, not a panel |
| Negotiate: hospital comparison, Medicaid / workers' comp floors | **Advanced** | need extra files most owners never load |
| Negotiate: what a visit is worth (economics) | **Merge** with Rate card's visit value | duplicate |
| Explorer, Code comparison, Markets, Payer roster | **Merge into one "Market rates" view** | four ways to browse the same rate table; owners need one: code + payer + city/state → ranked clinics |
| Rate changes | **Keep, small** (inside Market rates: "what changed since last month") | useful once several months are loaded |
| Overview | **Replace** by a one-screen "My practice" summary | today it is store statistics |
| Clients (what changed, packets, engagements, renewals) | **Advanced (consultant)** | consultant workflow |
| Leads (underpaid leads, new to network, new clinics, closures, referrals, dossier, market report, growth, sizing) | **Advanced (consultant)** | prospecting; not an owner's question |
| Medicare (referrals & eligibility, referral leaders, batch eligibility) | **Advanced** | separate product line (Tracker) |
| Files & imports, Sources | **Keep** ("Data") | plus the new classification data status |
| Remit check, validation cross-check | **Move into Data** | tools, not outputs |

"Advanced" = hidden behind one switch (Settings → "Show consultant tools"),
not deleted: nothing a current user relies on disappears, and the default
screen set drops from 14 views to 4.

## 4. The owner's screens (proposed)

1. **My rates** — pick your practice (name, tax ID or NPI) and geography
   (your city / your state). Per payer × code: your rate, the median / P25 /
   P75 of **outpatient rehab clinics** paid by that payer there, your
   percentile and $ gap; the payer scorecard; one standard visit valued per
   payer and against the market. One printable, one CSV.
2. **Negotiate** — pick a payer. The codes where you are below that payer's
   clinic median; **the named clinics in your city (else state) that the same
   payer pays more for the same codes**, ranked by the gap, with their rate,
   NPI count and city; the visit-value gap; dollars at stake from your
   volumes (Medicare-derived or typed). One printable "negotiation packet"
   and CSV.
3. **Market rates** — no practice needed: a code (or a visit) + payer(s) +
   city/state → the distribution among outpatient clinics and the ranked,
   named list; what changed since last month.
4. **Data** — files, sources, imports, classification coverage.

Every comparison defaults to **outpatient rehab clinics** with a visible
switch to "all practices".

## 5. Classifying outpatient rehab clinics

Per tax ID, from the evidence available, each recorded so the screen can say
*why* a practice is (or is not) counted:

| Signal | Source | Says |
|---|---|---|
| Org NPI taxonomy 261QP2000X (PT clinic), 261QR0400X (rehab clinic), or a GROUP NPI registered as 225100000X / 225X00000X / 235Z00000X | NPPES (have) | outpatient clinic |
| Org NPI taxonomy hospital (282N…), SNF / nursing (314…, 313M…), home health (251E…) | NPPES (have) | not an outpatient clinic |
| Share of the TIN's clinicians with PT / OT / SLP taxonomies | NPPES (have — today's `is_therapy`) | therapy practice vs. physician / multispecialty |
| Clinicians billing therapy codes to Medicare as "PT / OT in Private Practice", office setting | Medicare Physician & Other Practitioners, by provider and service — fetchable per code from the CMS API (97110: 83,127 rows nationally) | outpatient private practice |
| Therapists with NO professional Medicare billing while the TIN has a hospital / SNF org | same | hospital outpatient / contract therapy |
| Practice addresses matching CMS hospital or nursing-home addresses (most of them) | NPPES addresses + CMS Hospital General Information and Nursing Home Provider Information lists (small files) | not an outpatient clinic |

Result: `practice_type` = outpatient rehab clinic / hospital outpatient /
SNF-contract therapy / physician-multispecialty / unknown, with the signals
behind it, used as the default peer group everywhere and as a filter
("org NPI taxonomy", "practice type") on every list.
