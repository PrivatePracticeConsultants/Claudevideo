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
    # ---- PT-leaning treatment ----
    "97113": ("Aquatic therapy w/ exercises", (PT,), True),
    "97116": ("Gait training", (PT,), True),
    "97124": ("Massage therapy", (PT,), True),
    "97750": ("Physical performance test", (PT, OT), True),
    # ---- OT-specific ----
    "97129": ("Cognitive function intervention, first 15 min", (OT, SLP), True),
    "97130": ("Cognitive function intervention, each addl 15 min", (OT, SLP), True),
    "97533": ("Sensory integrative techniques", (OT,), True),
    "97537": ("Community/work reintegration training", (OT,), True),
    # ---- PT modalities ----
    "97010": ("Hot/cold packs", (PT, OT), False),
    "97012": ("Mechanical traction", (PT,), False),
    "97014": ("Electrical stimulation (unattended)", (PT,), False),
    "97016": ("Vasopneumatic devices", (PT,), False),
    "97018": ("Paraffin bath", (PT, OT), False),
    "97022": ("Whirlpool", (PT,), False),
    "97026": ("Infrared therapy", (PT,), False),
    "97032": ("Electrical stimulation (manual)", (PT,), True),
    "97033": ("Iontophoresis", (PT, OT), True),
    "97035": ("Ultrasound therapy", (PT,), True),
    "97036": ("Hubbard tank", (PT,), True),
    "G0283": ("Electrical stimulation, non-wound (HCPCS)", (PT,), False),
    # ---- SLP evals (untimed) ----
    "92521": ("Evaluation of speech fluency", (SLP,), False),
    "92522": ("Evaluation of speech sound production", (SLP,), False),
    "92523": ("Speech sound production w/ language eval", (SLP,), False),
    "92524": ("Behavioral analysis of voice and resonance", (SLP,), False),
    # ---- SLP treatment ----
    "92507": ("Speech/hearing therapy, individual", (SLP,), False),
    "92508": ("Speech/hearing therapy, group", (SLP,), False),
    "92526": ("Swallowing dysfunction treatment", (SLP,), False),
    # ---- swallowing studies ----
    "92610": ("Swallowing function evaluation", (SLP,), False),
    "92611": ("Motion fluoroscopic swallow study", (SLP,), False),
    "92612": ("Flexible endoscopic swallow eval (FEES)", (SLP,), False),
    "92613": ("FEES interpretation and report", (SLP,), False),
    "92614": ("Laryngeal sensory testing", (SLP,), False),
    "92615": ("Laryngeal sensory testing interpretation", (SLP,), False),
    "92616": ("FEES w/ laryngeal sensory testing", (SLP,), False),
    "92617": ("FEES w/ sensory testing interpretation", (SLP,), False),
    # ---- auditory rehab ----
    "92626": ("Auditory function evaluation, first hour", (SLP,), False),
    "92627": ("Auditory function evaluation, each addl 15 min", (SLP,), True),
    # ---- AAC / voice ----
    "92597": ("Voice prosthetic evaluation", (SLP,), False),
    "92605": ("Non-speech-generating AAC device eval, first hour", (SLP,), False),
    "92606": ("Non-speech-generating AAC device services", (SLP,), False),
    "92607": ("Speech-generating AAC device eval, first hour", (SLP,), False),
    # timed=False on purpose: it IS time-based but the flag means "15-minute
    # unit" (and renders as 'timed 15-min' on reports) — 92608 is a 30-minute
    # add-on, so labeling it 15-min was factually wrong on deliverables
    "92608": ("Speech-generating AAC device eval, each addl 30 min", (SLP,), False),
    "92609": ("Speech-generating AAC device programming", (SLP,), False),
}

DEFAULT_CODE_SET = list(CODE_CATALOG.keys())

# NPPES Healthcare Provider Taxonomy codes for the provider TYPES this tool
# targets — PT / OT / SLP and outpatient therapy clinics. Used to tell an actual
# therapist/therapy practice from an MD/DO/NP who merely billed a 97xxx code (the
# MRF lists every provider with a rate for a code, not just therapists). Prefix
# match covers all specializations under a discipline. Extend these tuples if a
# payer's therapists carry a taxonomy not listed here.
THERAPY_TAXONOMY_PREFIXES = (
    "2251",  # Physical Therapist (all specializations)
    "2252",  # Physical Therapist Assistant
    "225X",  # Occupational Therapist (all specializations)
    "224Z",  # Occupational Therapy Assistant
    "235Z",  # Speech-Language Pathologist
)
# full org/clinic taxonomies for practices that HOUSE therapists
THERAPY_TAXONOMY_CODES = (
    "261QP2300X",  # Clinic/Center - Physical Therapy
    "261QR0400X",  # Clinic/Center - Rehabilitation
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


def hospital_taxonomy_sql(col: str) -> str:
    """A SQL boolean: TRUE when `col` is a hospital-class NPPES taxonomy.
    Constants only — safe to inline, same contract as therapy_taxonomy_sql."""
    likes = " OR ".join(f"{col} LIKE '{p}%'" for p in HOSPITAL_TAXONOMY_PREFIXES)
    return f"({col} IS NOT NULL AND ({likes}))"


def therapy_taxonomy_sql(col: str, prefixes=THERAPY_TAXONOMY_PREFIXES,
                         codes=THERAPY_TAXONOMY_CODES) -> str:
    """A SQL boolean expression: TRUE when `col` (an NPPES taxonomy_code) is a
    PT/OT/SLP or outpatient-therapy-clinic taxonomy. Values are hard-coded
    identifiers (letters+digits), never user input, so inlining is safe."""
    likes = " OR ".join(f"{col} LIKE '{p}%'" for p in prefixes) or "FALSE"
    code_list = ", ".join(f"'{c}'" for c in codes) or "''"
    return f"({col} IS NOT NULL AND (({likes}) OR {col} IN ({code_list})))"


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
            rows = con.execute(
                f"SELECT npi FROM read_parquet('{pth}') "
                f"WHERE {therapy_taxonomy_sql('taxonomy_code')}").fetchall()
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
