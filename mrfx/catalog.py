"""Static code catalog (PT / OT / SLP) + modifier semantics.

Each entry: short description, the discipline(s) that bill it, and whether it
is a timed (15-minute-unit) code. Shared codes carry several disciplines —
attribution for those comes from the GP/GO/GN modifier, never the code
(§3.5/§7A.9): an unmodified shared code is "unspecified".
"""

from __future__ import annotations

PT, OT, SLP = "PT", "OT", "SLP"
UNSPECIFIED = "unspecified"

# discipline modifiers (plan-of-care attribution)
DISCIPLINE_MODIFIERS = {"GP": PT, "GO": OT, "GN": SLP}
# assistant-provided modifiers (statutory 85% payment)
ASSISTANT_MODIFIERS = {"CQ", "CO"}
# other recognized flags
CAP_EXCEPTION_MODIFIER = "KX"
DISTINCT_SERVICE_MODIFIERS = {"59", "XE", "XS", "XP", "XU"}

# code: (description, disciplines, timed)
CODE_CATALOG: dict[str, tuple[str, tuple[str, ...], bool]] = {
    # ---- PT evals (untimed) ----
    "97161": ("PT eval, low complexity", (PT,), False),
    "97162": ("PT eval, moderate complexity", (PT,), False),
    "97163": ("PT eval, high complexity", (PT,), False),
    "97164": ("PT re-evaluation", (PT,), False),
    # ---- OT evals (untimed) ----
    "97165": ("OT eval, low complexity", (OT,), False),
    "97166": ("OT eval, moderate complexity", (OT,), False),
    "97167": ("OT eval, high complexity", (OT,), False),
    "97168": ("OT re-evaluation", (OT,), False),
    # ---- shared PT/OT treatment (timed unless noted) ----
    "97110": ("Therapeutic exercises", (PT, OT), True),
    "97112": ("Neuromuscular re-education", (PT, OT), True),
    "97140": ("Manual therapy", (PT, OT), True),
    "97150": ("Group therapeutic procedures", (PT, OT), False),
    "97530": ("Therapeutic activities", (PT, OT), True),
    "97535": ("Self-care/home management training", (PT, OT), True),
    "97542": ("Wheelchair management training", (PT, OT), True),
    "97760": ("Orthotic management & training, initial", (PT, OT), True),
    "97761": ("Prosthetic training, initial", (PT, OT), True),
    "97763": ("Orthotic/prosthetic management, established", (PT, OT), True),
    # ---- shared PT/OT treatment (AOTA's own 2026 code list carries all three) ----
    "97113": ("Aquatic therapy w/ exercises", (PT, OT), True),
    "97116": ("Gait training", (PT, OT), True),
    "97124": ("Massage therapy", (PT, OT), True),
    "97750": ("Physical performance test", (PT, OT), True),
    # ---- cognitive / sensory / community reintegration ----
    # CMS MAC article A56566 (the JOINT PT-and-OT outpatient article) covers all
    # four, and states verbatim for 97129/97130 and 97533: "This service is
    # payable to speech-language pathologists under certain conditions."
    "97129": ("Cognitive function intervention, first 15 min", (PT, OT, SLP), True),
    "97130": ("Cognitive function intervention, each addl 15 min", (PT, OT, SLP), True),
    "97533": ("Sensory integrative techniques", (PT, OT, SLP), True),
    "97537": ("Community/work reintegration training", (PT, OT), True),
    # ---- modalities (PT-dominant; several are core hand therapy) ----
    # CMS marks every one of these "always therapy … regardless of who performs
    # them", i.e. OT billing is permitted throughout. The four widened to (PT,OT)
    # here are the ones AOTA's own published list carries, which is evidence OTs
    # actually bill them; the rest stay PT because widening on permission alone
    # would trade a mostly-right attribution for `unspecified` on every row.
    "97010": ("Hot/cold packs", (PT, OT), False),
    "97012": ("Mechanical traction", (PT,), False),
    "97014": ("Electrical stimulation (unattended)", (PT,), False),
    "97016": ("Vasopneumatic devices", (PT,), False),
    "97018": ("Paraffin bath", (PT, OT), False),
    "97022": ("Whirlpool", (PT, OT), False),
    "97026": ("Infrared therapy", (PT,), False),
    "97032": ("Electrical stimulation (manual)", (PT,), True),
    "97033": ("Iontophoresis", (PT, OT), True),
    "97035": ("Ultrasound therapy", (PT, OT), True),
    "97036": ("Hubbard tank", (PT,), True),
    "G0283": ("Electrical stimulation, non-wound (HCPCS)", (PT, OT), False),
    # ---- SLP evals (untimed) ----
    "92521": ("Evaluation of speech fluency", (SLP,), False),
    "92522": ("Evaluation of speech sound production", (SLP,), False),
    "92523": ("Speech sound production w/ language eval", (SLP,), False),
    "92524": ("Behavioral & qualitative analysis of voice and resonance", (SLP,), False),
    # ---- SLP treatment ----
    # 92507/92508 descriptions were the PRE-2014 wording ("speech/hearing
    # therapy", from the deleted 92506 era); CMS's 2026 descriptor is
    # "Tx sp lang voice comm indiv/group". 92526 is not swallowing-only — CMS
    # calls it "Oral function therapy", and the feeding half is exactly why
    # AOTA's own list carries it.
    "92507": ("Speech, language, voice & communication treatment, individual", (SLP,), False),
    "92508": ("Speech, language, voice & communication treatment, group", (SLP,), False),
    "92526": ("Swallowing/oral function for feeding treatment", (OT, SLP), False),
    # ---- swallowing studies (AOTA's 2026 list carries both: OT feeding therapy) ----
    "92610": ("Swallowing function evaluation", (OT, SLP), False),
    "92611": ("Motion fluoroscopic swallow study", (OT, SLP), False),
    "92612": ("Flexible endoscopic swallow eval (FEES)", (SLP,), False),
    "92613": ("FEES interpretation and report", (SLP,), False),
    "92614": ("Laryngeal sensory testing", (SLP,), False),
    "92615": ("Laryngeal sensory testing interpretation", (SLP,), False),
    "92616": ("FEES w/ laryngeal sensory testing", (SLP,), False),
    "92617": ("FEES w/ sensory testing interpretation", (SLP,), False),
    # ---- implanted-device auditory evaluation ----
    # NOT "auditory rehabilitation" — CPT 2020 revised the descriptor to
    # candidacy/postoperative status for a SURGICALLY IMPLANTED device, and CMS
    # classes these as AUDIOLOGY codes: they are absent from the CMS Therapy Code
    # List entirely and never take GN. Kept because SLPs do bill them in aural-
    # rehab settings, but read these rates as audiology, not therapy.
    "92626": ("Auditory function eval for implanted device, first hour", (SLP,), False),
    "92627": ("Auditory function eval for implanted device, each addl 15 min", (SLP,), True),
    # ---- AAC / voice ----
    "92597": ("Voice prosthetic evaluation", (SLP,), False),
    # the NON-speech-generating AAC family is on AOTA's list (OT assistive
    # technology); the speech-generating family below is SLP-only
    "92605": ("Non-speech-generating AAC device eval, first hour", (OT, SLP), False),
    "92606": ("Non-speech-generating AAC device services", (OT, SLP), False),
    "92607": ("Speech-generating AAC device eval, first hour", (SLP,), False),
    # timed=False on purpose: it IS time-based but the flag means "15-minute
    # unit" (and renders as 'timed 15-min' on reports) — 92608 is a 30-minute
    # add-on, so labeling it 15-min was factually wrong on deliverables
    "92608": ("Speech-generating AAC device eval, each addl 30 min", (SLP,), False),
    "92609": ("Speech-generating AAC device programming", (SLP,), False),
    "92618": ("Non-speech-generating AAC device services, each addl 30 min", (OT, SLP), False),
    # ---- modality gaps: these complete the 97012-97036 runs above, and all
    # three are "always therapy" on the CMS list. AOTA's list carries 97024.
    "97024": ("Diathermy (e.g. microwave)", (PT, OT), False),
    "97028": ("Ultraviolet therapy", (PT, OT), False),
    "97034": ("Contrast baths", (PT, OT), True),
    # ---- assessments not previously collected ----
    "97755": ("Assistive technology assessment", (PT, OT), True),
    # per HOUR, not a 15-minute unit — see the 92608 note above
    "96105": ("Assessment of aphasia", (SLP,), False),
    "96125": ("Standardized cognitive performance testing", (OT, SLP), False),
    "92520": ("Laryngeal function studies", (SLP,), False),
    # per DAY, not timed
    "95992": ("Canalith repositioning (Epley/Semont)", (PT, OT), False),
    # ---- pelvic health ----
    # biofeedback is the core of a fast-growing outpatient PT niche that was
    # entirely invisible without these two
    "90912": ("Biofeedback training, first 15 min", (PT,), True),
    "90913": ("Biofeedback training, each addl 15 min", (PT,), True),
    # ---- caregiver training (new 2024/2025; all three disciplines) ----
    # The "initial" codes are 30-MINUTE units and the group codes are untimed,
    # so only the each-additional-15 codes are is_timed — mislabeling the 30s as
    # "timed 15-min" would misstate the unit on a client's rate card.
    "97550": ("Caregiver training, initial 30 min", (PT, OT, SLP), False),
    "97551": ("Caregiver training, each addl 15 min", (PT, OT, SLP), True),
    "97552": ("Caregiver training, group", (PT, OT, SLP), False),
    "G0541": ("Caregiver training, direct care, initial 30 min (HCPCS)", (PT, OT, SLP), False),
    "G0542": ("Caregiver training, direct care, each addl 15 min (HCPCS)", (PT, OT, SLP), True),
    "G0543": ("Caregiver training, direct care, group (HCPCS)", (PT, OT, SLP), False),
}

DEFAULT_CODE_SET = list(CODE_CATALOG.keys())

# NPPES Healthcare Provider Taxonomy codes for the provider TYPES this tool
# targets — PT / OT / SLP and outpatient therapy clinics. Used to tell an actual
# therapist/therapy practice from an MD/DO/NP who merely billed a 97xxx code (the
# MRF lists every provider with a rate for a code, not just therapists). Prefix
# match covers all specializations under a discipline. Extend these tuples if a
# payer's therapists carry a taxonomy not listed here.
THERAPY_TAXONOMY_PREFIXES = (
    "2251",   # Physical Therapist (all specializations)
    "2252",   # Physical Therapist Assistant
    "225X",   # Occupational Therapist (all specializations)
    "224Z",   # Occupational Therapy Assistant
    "235Z",   # Speech-Language Pathologist
    # Speech-Language ASSISTANT. Deliberately the 5-char prefix: bare "2355"
    # would also sweep in 2355A2700X (Audiology Assistant). Without this a
    # 1-SLP + 1-SLPA practice scored 50% and was hidden.
    "2355S",
)
# Org/clinic taxonomies for practices that HOUSE therapists. Verified against
# the authoritative NUCC code set (v25.1) — an earlier version of this list had
# 261QP2300X labelled "Clinic/Center - Physical Therapy"; that code is actually
# **Primary Care**, so every primary-care clinic in the book counted as a therapy
# practice. The real PT clinic code is 261QP2000X.
#
# NOTE there is NO Occupational-Therapy clinic/center code in NUCC (checked all
# 883 codes): an OT-only practice enumerates under the individual OT taxonomies
# (225X*/224Z*), or as Rehabilitation / Developmental Disabilities. That's why
# the OT side leans on the provider-share test rather than a clinic code.
THERAPY_TAXONOMY_CODES = (
    "261QP2000X",  # Clinic/Center - Physical Therapy
    "261QH0700X",  # Clinic/Center - Hearing and Speech (the speech-clinic code)
    "261QD1600X",  # Clinic/Center - Developmental Disabilities (pediatric PT/OT/ST)
    "261QR0401X",  # Clinic/Center - Rehabilitation, CORF (outpatient PT/OT/SLP by definition)
    "261QR0400X",  # Clinic/Center - Rehabilitation (AMBIGUOUS — numerator only, see below)
)
# UNAMBIGUOUS therapy-clinic org codes: a practice carrying one of these is a
# therapy clinic, so the code is strong enough evidence to qualify a TIN on a
# simple majority (a two-therapist clinic with one NP on staff is still a therapy
# clinic) and to stand in for enrichment at a small practice.
# Deliberately EXCLUDES 261QR0400X ("Clinic/Center - Rehabilitation"), which
# physiatry groups, multispecialty rehab and hospital outpatient departments also
# carry — that one only counts toward the share numerator and can never by itself
# qualify a TIN. Also excludes 261QR0404X (Cardiac rehab) and 261QR0405X
# (Substance-Use-Disorder rehab), which are not PT/OT/SLP at all.
THERAPY_CLINIC_STRONG_CODES = (
    "261QP2000X",  # Physical Therapy
    "261QH0700X",  # Hearing and Speech
    "261QD1600X",  # Developmental Disabilities (peds therapy)
    "261QR0401X",  # CORF
)

# Hospital-CLASS taxonomies: any NPI carrying one of these marks its whole TIN
# as a hospital/health system, which the strict practice filter EXCLUDES — the
# outreach target is private outpatient practices, and a hospital that employs
# a few therapists (or whose outpatient rehab department carries the rehab-
# clinic code) is not one. Prefix match; extend if a payer's hospitals carry
# something not listed.
HOSPITAL_TAXONOMY_PREFIXES = (
    "282",  # Hospitals (general acute care, long term care, ...)
    "283",  # Special hospitals (rehabilitation 283X, psychiatric, children's)
    "284",  # Specialty hospitals
    "273",  # Hospital units (rehabilitation unit 273Y, psych unit, ...)
)

# NON-OUTPATIENT facility classes that also bill 97xxx therapy codes. The target
# is OUTPATIENT PT/OT/SLP practices, so a nursing home, a home-health agency or
# a residential/hospice program that employs therapists is not a prospect even
# when therapists are the majority of its identified NPIs. Excluded the same way
# hospitals are (any member NPI disqualifies the TIN).
NON_OUTPATIENT_TAXONOMY_PREFIXES = (
    "314",    # Skilled Nursing Facility
    "313M",   # Nursing Facility / Intermediate Care
    "315",    # Hospice / inpatient-care facility
    "310",    # Nursing & Custodial Care Facility (assisted living, ICF)
    "311",    # Custodial care (adult day, homemaker, group home)
    "251E",   # Home Health Agency
    "251J",   # Nursing Care (home) Agency
    "251G",   # Hospice care, community based
    "3336",   # Pharmacies (co-billing noise)
    "3416",   # Ambulance / transport
    # Residential treatment / group-living families (the classes the earlier
    # comment CLAIMED 310/311/315 covered but didn't):
    "320",    # Residential treatment (mental illness, dev. disabilities)
    "322",    # Residential treatment, chemical dependency
    "323",    # Residential treatment, intellectual/dev. disabilities
    "324",    # Residential treatment, physical disabilities
    "385H",   # Respite care
    # Agencies and schools that employ PT/OT/SLP but are not outpatient
    # practices — a school district is not a prospect even when therapists are
    # most of its identified providers.
    # NOT vetoed: 252Y (Early Intervention Provider Agency). The veto is
    # absolute — ONE member NPI disqualifies the whole TIN — and private
    # pediatric PT/OT/SLP clinics routinely hold an EI agency NPI alongside
    # their clinic NPI, so vetoing it deleted exactly the pediatric practices
    # this tool is meant to surface. An EI agency's own therapists now face the
    # ordinary clinician-share test like any other practice.
    "251C",   # Developmentally Disabled Services Day Training
    "2513",   # Local Education Agency (school districts)
    "251S",   # Community/Behavioral Health Agency
    "251K",   # Public Health / Welfare Agency
)

# How much of a TIN's IDENTIFIED providers must be PT/OT/SLP (or a therapy
# clinic) for the practice to count as a therapy practice. 75 = three quarters.
# A bare majority (>50%) admitted genuinely mixed groups — a chiropractic or
# physician office with a couple of therapists on staff read as a "therapy
# practice". Raise toward 100 for only-therapy purity; lower to ~60 to include
# more mixed rehab groups. Requires a directory rebuild to take effect (the
# ROLLUP_SCHEMA_VERSION bump handles that automatically on upgrade).
THERAPY_MIN_SHARE_PCT = 75

# Ceiling (in NPIs) for the "small clinic whose providers aren't identified yet"
# escape from the coverage test. A genuinely small therapy clinic carrying the
# unambiguous PT-clinic org code is real even before NPPES identifies its
# therapists; a 500-provider entity is not "small" and must earn the flag on
# identified providers instead.
THERAPY_SMALL_PRACTICE_NPIS = 10


def hospital_taxonomy_sql(col: str) -> str:
    """A SQL boolean: TRUE when `col` is a hospital-class NPPES taxonomy.
    Constants only — safe to inline, same contract as therapy_taxonomy_sql."""
    likes = " OR ".join(f"{col} LIKE '{p}%'" for p in HOSPITAL_TAXONOMY_PREFIXES)
    return f"({col} IS NOT NULL AND ({likes}))"


def excluded_facility_taxonomy_sql(col: str) -> str:
    """A SQL boolean: TRUE when `col` is a hospital OR any other non-outpatient
    facility class (SNF, home health, residential, hospice…). One member NPI
    carrying any of these disqualifies the whole TIN from the strict
    outpatient-therapy-practice filter. Constants only — safe to inline."""
    prefixes = HOSPITAL_TAXONOMY_PREFIXES + NON_OUTPATIENT_TAXONOMY_PREFIXES
    likes = " OR ".join(f"{col} LIKE '{p}%'" for p in prefixes)
    return f"({col} IS NOT NULL AND ({likes}))"


def therapy_taxonomy_sql(col: str, prefixes=THERAPY_TAXONOMY_PREFIXES,
                         codes=THERAPY_TAXONOMY_CODES,
                         all_col: str | None = "auto") -> str:
    """A SQL boolean expression: TRUE when the provider is a PT/OT/SLP or
    outpatient-therapy-clinic taxonomy. Values are hard-coded identifiers
    (letters+digits), never user input, so inlining is safe.

    Tests the primary taxonomy AND the full list. NPPES lets a provider carry
    up to 15 taxonomies and only ONE is flagged primary — so a real therapy
    clinic can have its therapy code in a secondary slot. Measured against the
    live registry across five Missouri ZIPs: 10 of 109 providers carrying a
    therapy taxonomy (9%) do NOT carry it as primary, including a clinic named
    "APEX PHYSICAL THERAPY, LLC" whose primary is the generic 174400000X
    'Specialist'. Keying on the primary alone silently excluded all of them.

    `all_col` names the pipe-joined column: "auto" derives it from `col`
    (n.taxonomy_code -> n.taxonomy_codes), and None tests the primary alone —
    for a relation that genuinely has no such column (an NPPES cache written
    before it existed), where the rule must degrade to the OLD behaviour rather
    than binder-error. A store whose column exists but is NULL falls back to
    primary-only per row, which is likewise never worse than before."""
    likes = " OR ".join(f"{col} LIKE '{p}%'" for p in prefixes) or "FALSE"
    code_list = ", ".join(f"'{c}'" for c in codes) or "''"
    primary = f"({col} IS NOT NULL AND (({likes}) OR {col} IN ({code_list})))"
    if all_col == "auto":
        all_col = col + "s" if col.endswith("taxonomy_code") else None
    if not all_col:
        return primary
    # pipe-joined list, wrapped in delimiters here so a prefix match cannot
    # straddle two codes and an exact match cannot hit a longer code that
    # merely contains it
    allc = all_col
    padded = f"('|' || {allc} || '|')"
    any_prefix = " OR ".join(f"{padded} LIKE '%|{p}%'" for p in prefixes) or "FALSE"
    any_code = " OR ".join(f"{padded} LIKE '%|{c}|%'" for c in codes) or "FALSE"
    return f"({primary} OR ({allc} IS NOT NULL AND (({any_prefix}) OR ({any_code}))))"


# parser worker cache: {(path, mtime, size): frozenset[npi]} so the (nationally
# ~hundreds-of-thousands-strong) therapy-NPI set is read from the NPPES parquet
# ONCE per worker, not per file. Keyed on the file signature so a rebuilt cache
# reloads.
_therapy_npi_cache: dict[tuple, frozenset] = {}
# A cache built from the full NPPES monthly has ~8-9M rows; a weekly/partial file
# has far fewer. Filtering ingest against a partial cache would drop REAL
# therapists (they just aren't in that small file), so below this we decline to
# filter and keep everything. Matches enrich._BULK_MIN_FULL_ROWS.
_THERAPY_CACHE_MIN_ROWS = 2_000_000


def therapy_npi_set(cache_parquet) -> frozenset[str] | None:
    """The set of NPIs that are PT/OT/SLP or a therapy clinic, read from the
    NPPES fast-lookup parquet. Returns None when the cache is missing/unreadable
    OR looks partial (too few rows to be the full NPPES) — the caller then keeps
    every row rather than silently dropping real therapists. Used by the parser
    when config.therapy_only_ingest is on."""
    from pathlib import Path
    p = Path(cache_parquet)
    try:
        st = p.stat()
    except OSError:
        return None  # not built yet; don't cache — it may appear later
    key = (str(p), int(st.st_mtime), st.st_size)
    hit = _therapy_npi_cache.get(key)
    if hit is not None:
        return hit
    try:
        import duckdb
        pth = str(p).replace("'", "''")
        con = duckdb.connect()
        try:
            total = con.execute(f"SELECT count(*) FROM read_parquet('{pth}')").fetchone()[0]
            if total < _THERAPY_CACHE_MIN_ROWS:
                return None  # partial/weekly file — don't trust it to classify therapists
            # A cache written before the all-taxonomies column existed has only
            # `taxonomy_code`. Match on whatever it actually carries rather than
            # binder-erroring into "keep everything": the multi-slot rule is an
            # improvement, and its absence must degrade to the OLD behaviour,
            # never to none at all. (The cache rebuilds itself on the next
            # enrichment pass because the schema stamp changed.)
            cols = {r[0] for r in con.execute(
                f"DESCRIBE SELECT * FROM read_parquet('{pth}')").fetchall()}
            expr = therapy_taxonomy_sql(
                "taxonomy_code",
                all_col="taxonomy_codes" if "taxonomy_codes" in cols else None)
            rows = con.execute(
                f"SELECT npi FROM read_parquet('{pth}') WHERE {expr}").fetchall()
        finally:
            con.close()  # don't leak a connection per file on the long grind
        result = frozenset(r[0] for r in rows if r[0])
        if not result:
            # A full-size file that matches ZERO therapy taxonomies means the
            # taxonomy column is unusable (all-NULL / wrong layout / renamed) —
            # a real full NPPES has hundreds of thousands of therapists. Trust
            # nothing here and keep every row rather than silently wiping the
            # store on a "successful"-looking ingest.
            return None
    except Exception:  # noqa: BLE001 — unreadable cache -> keep all rows
        return None
    _therapy_npi_cache[key] = result
    return result


def code_info(code: str) -> tuple[str, tuple[str, ...], bool]:
    return CODE_CATALOG.get(code, ("", (), False))


def resolve_discipline(code: str, modifiers: list[str]) -> str:
    """Modifier-first discipline attribution (§3.5).

    GP/GO/GN wins. Without one, a single-discipline code attributes by code;
    a shared code is honestly 'unspecified' — never guessed.
    """
    for m in modifiers:
        if m in DISCIPLINE_MODIFIERS:
            return DISCIPLINE_MODIFIERS[m]
    _, disciplines, _ = code_info(code)
    if len(disciplines) == 1:
        return disciplines[0]
    return UNSPECIFIED


def is_timed(code: str) -> bool:
    return code_info(code)[2]


def catalog_json() -> dict:
    """code -> {description, disciplines, timed} for the dashboard."""
    return {
        c: {"description": d, "disciplines": list(ds), "timed": t}
        for c, (d, ds, t) in CODE_CATALOG.items()
    }
