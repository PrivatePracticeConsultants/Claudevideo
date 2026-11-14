"""Practice names and places, ranked (0.8.5) — ONE rule for every surface.

A practice is a tax ID (TIN). Its name and cities come from the NPIs it bills
under. The directory used to take, for a TIN with no organization NPI, the
"most common" name among its NPIs — and an individual NPI's name is the
clinician's own, each unique, so every TIN of individuals tied and the
ALPHABETICALLY FIRST clinician named the practice ("ALEC YATES"; ~26% of the
names in a BCBS Tennessee export began with "A"). Cities were the TIN's cities
sorted alphabetically, every state mixed.

The rule now, per TIN (`name_source` records which step named it):

1. ``nppes_org`` — the TIN has organization NPIs (NPPES entity type 2): their
   legal business name. Names are compared NORMALIZED (`norm_sql`: case,
   punctuation, "&"/AND, trailing LLC / INC / PC / PLLC … suffixes); the name
   shared by the most org NPIs wins; ties: the most of them in the state
   (the export's state, else the TIN's main state), then the name used by the
   most org NPIs in the whole directory, then the normalized name (a unique
   last key — invariant 7). The spelling shown is the group's most common.
2. ``cms_dac`` — no org NPI: the CMS Doctors and Clinicians National
   Downloadable File (`dac_orgs`, loaded by `mrfx dac`) gives each clinician's
   group ("Facility Name" / org_nm). The group named by the most of the TIN's
   matched NPIs wins (ties: most in the state, then the name); accepted only
   when it covers at least DAC_MIN_SHARE of the TIN's NPIs found in the file.
3. ``solo_npi`` — still no name, and the TIN has exactly ONE NPI: that
   provider's NPPES name (a true solo practice).
   ``unnamed_group`` — otherwise "Unnamed group — {n} clinicians ({top
   city})". An individual's name never names a group.

Cities: the TIN's NPIs' practice-location cities ranked by how many of its
NPIs are there — those in the state first (by their in-state count), then the
rest; `top_cities` is the in-state ones (all of them when none is in the
state), at most three.
"""

from __future__ import annotations

DAC_MIN_SHARE = 0.30
TOP_CITIES = 3

# trailing legal-form tokens (after punctuation became spaces): "P.C." is
# "P C", "L.L.C." is "L L C"
_SUFFIX = ("LLC|L L C|INC|INCORPORATED|PC|P C|PLLC|P L L C|PLC|PA|P A|LTD|LIMITED|"
           "CORP|CORPORATION|CO|COMPANY|LLP|L L P|LP|PSC|SC|PROFESSIONAL CORPORATION")


def norm_sql(col: str) -> str:
    """SQL: the comparison key of a business name (never shown)."""
    x = f"upper(coalesce({col}, ''))"
    x = f"replace({x}, '&', ' AND ')"
    x = f"regexp_replace({x}, '[^A-Z0-9 ]', ' ', 'g')"
    x = f"regexp_replace({x}, ' +', ' ', 'g')"
    x = f"trim({x})"
    # strip trailing suffix tokens, repeatedly ("… PLLC INC")
    x = f"trim(regexp_replace(' ' || {x}, '( ({_SUFFIX}))+$', ''))"
    return x


def names_sql(tin_npis: str, state: str | None = None) -> str:
    """SQL for every TIN of `tin_npis` (a relation with tin_value, npi):
    tin_value, n_npi, practice_name, name_source, cities (ranked, all),
    top_cities (at most TOP_CITIES). `state`: a two-letter state the export
    is confined to (validated by the caller) — else each TIN's main state.
    Reads npi_directory, dac_orgs and org_name_freq (all may be empty)."""
    if state is not None:
        if not (len(state) == 2 and state.isalpha()):
            raise ValueError("state must be a two-letter code")
        st_cte = f"SELECT tin_value, '{state.upper()}' AS st FROM tot"
    else:
        st_cte = ("SELECT tin_value, first(state ORDER BY c DESC, state) AS st FROM "
                  "(SELECT tin_value, state, count(DISTINCT npi) AS c FROM j "
                  "WHERE state IS NOT NULL GROUP BY tin_value, state) GROUP BY tin_value")
    k_org, k_dac = norm_sql("r.org_name"), norm_sql("r.org_nm")
    return f"""
    WITH tn AS (SELECT DISTINCT tin_value, npi FROM ({tin_npis}) x
                WHERE tin_value IS NOT NULL AND npi IS NOT NULL),
    j AS (SELECT tn.tin_value, tn.npi, d.entity_type,
                 nullif(trim(d.org_name), '') AS org_name,
                 nullif(trim(d.state), '') AS state, nullif(trim(d.city), '') AS city
          FROM tn LEFT JOIN npi_directory d ON d.npi = tn.npi),
    tot AS (SELECT tin_value, count(DISTINCT npi) AS n_npi FROM j GROUP BY tin_value),
    st AS ({st_cte}),
    jr AS (SELECT j.*, coalesce(j.state = s.st, FALSE) AS ins
           FROM j LEFT JOIN st s ON s.tin_value = j.tin_value),
    -- 1. organization NPIs: the normalized name of the most org NPIs
    org_rows AS (SELECT r.tin_value, r.npi, r.org_name, r.ins, {k_org} AS k
                 FROM jr r WHERE r.entity_type = 'NPI-2' AND r.org_name IS NOT NULL),
    org_keys AS (SELECT tin_value, k, count(DISTINCT npi) AS c,
                        count(DISTINCT npi) FILTER (ins) AS c_in
                 FROM org_rows WHERE k <> '' GROUP BY tin_value, k),
    org_disp AS (SELECT tin_value, k, first(org_name ORDER BY n DESC, org_name) AS disp
                 FROM (SELECT tin_value, k, org_name, count(*) AS n FROM org_rows
                       GROUP BY tin_value, k, org_name) GROUP BY tin_value, k),
    org_pick AS (SELECT o.tin_value,
                        first(d.disp ORDER BY o.c DESC, o.c_in DESC,
                              coalesce(f.g, 0) DESC, o.k) AS name
                 FROM org_keys o JOIN org_disp d ON d.tin_value = o.tin_value AND d.k = o.k
                 LEFT JOIN org_name_freq f ON f.k = o.k
                 GROUP BY o.tin_value),
    -- 2. no org NPI: the CMS file's group of the most of its clinicians
    ind AS (SELECT DISTINCT tin_value, npi, ins FROM jr
            WHERE coalesce(entity_type, '') <> 'NPI-2'),
    dac_m AS (SELECT i.tin_value, count(DISTINCT i.npi) AS m FROM ind i
              WHERE i.npi IN (SELECT npi FROM dac_orgs) GROUP BY i.tin_value),
    dac_rows AS (SELECT i.tin_value, i.npi, r.org_nm,
                        coalesce(r.state = s.st, FALSE) AS ins, {k_dac} AS k
                 FROM ind i JOIN dac_orgs r ON r.npi = i.npi
                 LEFT JOIN st s ON s.tin_value = i.tin_value
                 WHERE nullif(trim(r.org_nm), '') IS NOT NULL),
    dac_keys AS (SELECT tin_value, k, count(DISTINCT npi) AS c,
                        count(DISTINCT npi) FILTER (ins) AS c_in
                 FROM dac_rows WHERE k <> '' GROUP BY tin_value, k),
    dac_disp AS (SELECT tin_value, k, first(org_nm ORDER BY n DESC, org_nm) AS disp
                 FROM (SELECT tin_value, k, org_nm, count(*) AS n FROM dac_rows
                       GROUP BY tin_value, k, org_nm) GROUP BY tin_value, k),
    dac_pick AS (SELECT o.tin_value,
                        first(d.disp ORDER BY o.c DESC, o.c_in DESC, o.k) AS name,
                        max(o.c) AS c
                 FROM dac_keys o JOIN dac_disp d ON d.tin_value = o.tin_value AND d.k = o.k
                 GROUP BY o.tin_value),
    -- 3. a true solo practice: its one provider's name
    solo AS (SELECT tin_value, min(org_name) AS name FROM j GROUP BY tin_value
             HAVING count(DISTINCT npi) = 1),
    -- cities: by how many of the TIN's NPIs practise there, in the state first
    cc AS (SELECT tin_value, city, count(DISTINCT npi) FILTER (ins) AS c_in,
                  count(DISTINCT npi) AS c
           FROM jr WHERE city IS NOT NULL GROUP BY tin_value, city),
    cities AS (SELECT tin_value,
                      list(city ORDER BY c_in DESC, c DESC, city) AS cities,
                      list(city ORDER BY c_in DESC, city) FILTER (c_in > 0) AS cities_in,
                      list(city ORDER BY c DESC, city) AS cities_any
               FROM cc GROUP BY tin_value)
    SELECT t.tin_value, t.n_npi,
           CASE WHEN op.name IS NOT NULL THEN op.name
                WHEN dp.name IS NOT NULL AND dp.c >= {DAC_MIN_SHARE} * dm.m THEN dp.name
                WHEN t.n_npi = 1 AND so.name IS NOT NULL THEN so.name
                ELSE 'Unnamed ' || CASE WHEN t.n_npi = 1 THEN 'practice' ELSE 'group' END
                     || ' — ' || t.n_npi || ' clinician' || CASE WHEN t.n_npi = 1 THEN '' ELSE 's' END
                     || coalesce(' (' || (CASE WHEN coalesce(len(ci.cities_in), 0) > 0 THEN ci.cities_in ELSE ci.cities_any END)[1] || ')', '')
           END AS practice_name,
           CASE WHEN op.name IS NOT NULL THEN 'nppes_org'
                WHEN dp.name IS NOT NULL AND dp.c >= {DAC_MIN_SHARE} * dm.m THEN 'cms_dac'
                WHEN t.n_npi = 1 AND so.name IS NOT NULL THEN 'solo_npi'
                ELSE 'unnamed_group' END AS name_source,
           coalesce(ci.cities, []::VARCHAR[]) AS cities,
           coalesce((CASE WHEN coalesce(len(ci.cities_in), 0) > 0 THEN ci.cities_in ELSE ci.cities_any END)[1:{TOP_CITIES}], []::VARCHAR[]) AS top_cities
    FROM tot t
    LEFT JOIN org_pick op ON op.tin_value = t.tin_value
    LEFT JOIN dac_pick dp ON dp.tin_value = t.tin_value
    LEFT JOIN dac_m dm ON dm.tin_value = t.tin_value
    LEFT JOIN solo so ON so.tin_value = t.tin_value
    LEFT JOIN cities ci ON ci.tin_value = t.tin_value
    """


ORG_NAME_FREQ_SQL = (
    "SELECT k, count(*) AS g FROM (SELECT " + norm_sql("org_name") + " AS k "
    "FROM npi_directory WHERE entity_type = 'NPI-2' AND nullif(trim(org_name), '') IS NOT NULL) "
    "WHERE k <> '' GROUP BY k")

NAME_SOURCES = {
    "nppes_org": "the legal business name of the tax ID's organization NPIs (NPPES)",
    "cms_dac": "the group most of its clinicians are enrolled with (CMS Doctors and "
               "Clinicians file)",
    "solo_npi": "the one provider billing under this tax ID (a solo practice)",
    "unnamed_group": "no organization name found — labelled by clinician count and city",
    "no_npi": "no NPI is published under this tax ID",
    "pending": "loaded since the names were last resolved — refreshed within minutes",
}


# ---------------------------------------------------------------- practice type
#
# Which practices are OUTPATIENT REHAB CLINICS (0.8.6) — the default peer group
# of every comparison. Evidence per tax ID, each recorded (type_reason):
#   * organization NPI taxonomies (NPPES): a therapy clinic (261QP2000X …) or a
#     GROUP registered as a therapist (2251…, 225X…, 235Z…) -> clinic;
#     hospital (282…) -> hospital; SNF / home health / residential -> facility
#   * practice ADDRESSES (NPPES) at CMS-listed hospitals / nursing homes
#     (cms_facilities): most of them -> hospital / facility
#   * the directory's clinician mix (is_therapy: PT/OT/SLP share, facility veto)
#   * Medicare professional billing of therapy codes (utilization): clinicians
#     billing as "Therapist in Private Practice" -> outpatient private practice
# Ordered rules: hospital, then facility, then clinic, then physician/other,
# else unknown — so a therapy group at a hospital address is hospital-based.

PRACTICE_TYPES = {
    "outpatient_clinic": "Outpatient rehab clinic",
    "hospital": "Hospital / hospital outpatient",
    "facility": "Nursing home or other facility (SNF, home health …)",
    "physician_therapy": "Physician-owned therapy (e.g. an orthopedic group with PT)",
    "physician_other": "Physician / multispecialty / other",
    "unknown": "Not enough information to tell",
}


def profile_sql(tin_npis: str, *, has_util: bool, has_provider_type: bool) -> str:
    """SQL per TIN of `tin_npis`: org_taxonomies, address counts at CMS
    hospitals / nursing homes, Medicare therapy-billing evidence."""
    from .catalog import (HOSPITAL_TAXONOMY_PREFIXES, NON_OUTPATIENT_TAXONOMY_PREFIXES,
                          THERAPY_CLINIC_STRONG_CODES, THERAPY_TAXONOMY_PREFIXES)
    from .cmsref import addr_key_sql

    def any_prefix(var: str, prefixes) -> str:
        return " OR ".join(f"starts_with({var}, '{p}')" for p in prefixes)
    clinic_codes = ", ".join(f"'{c}'" for c in THERAPY_CLINIC_STRONG_CODES)
    if has_util:
        is_pp = ("(u.provider_type ILIKE '%therapist in private practice%' "
                 "OR u.provider_type ILIKE '%speech language patholog%')")
        ptpp = (f"count(DISTINCT u.npi) FILTER ({is_pp})" if has_provider_type else "NULL::BIGINT")
        # by SERVICES, not people: a physician who bills a handful of 97110s
        # under their own NPI must not outvote the therapists who bill
        # thousands (a 52%-therapist ortho group read 47 of 95 "people")
        pp_share = (f"sum(u.services) FILTER ({is_pp}) / nullif(sum(u.services), 0)"
                    if has_provider_type else "NULL::DOUBLE")
        mc = f"""mc AS (SELECT j2.tin_value, count(DISTINCT u.npi) AS mc_npis, {ptpp} AS mc_ptpp,
                        {pp_share} AS mc_ptpp_share,
                        sum(u.services) FILTER (upper(u.place_of_service) = 'O')
                          / nullif(sum(u.services), 0) AS mc_office_share
                 FROM (SELECT DISTINCT tin_value, npi FROM j) j2
                 JOIN utilization u ON u.npi = j2.npi
                 WHERE u.data_year = (SELECT max(data_year) FROM utilization)
                 GROUP BY j2.tin_value)"""
    else:
        mc = ("mc AS (SELECT NULL::VARCHAR AS tin_value, NULL::BIGINT AS mc_npis, "
              "NULL::BIGINT AS mc_ptpp, NULL::DOUBLE AS mc_ptpp_share, "
              "NULL::DOUBLE AS mc_office_share WHERE FALSE)")
    return f"""
    WITH tn AS (SELECT DISTINCT tin_value, npi FROM ({tin_npis}) x
                WHERE tin_value IS NOT NULL AND npi IS NOT NULL),
    j AS (SELECT tn.tin_value, tn.npi, d.entity_type,
                 coalesce(nullif(d.taxonomy_codes, ''), d.taxonomy_code) AS taxes,
                 {addr_key_sql('d.address', 'd.zip')} AS ak
          FROM tn LEFT JOIN npi_directory d ON d.npi = tn.npi),
    fac AS (SELECT addr_key, bool_or(kind = 'hospital') AS hosp,
                   bool_or(kind = 'nursing_home') AS snf
            FROM cms_facilities WHERE addr_key IS NOT NULL GROUP BY addr_key),
    addr AS (SELECT j.tin_value, count(DISTINCT j.ak) AS n_addr,
                    count(DISTINCT j.ak) FILTER (f.hosp) AS n_hosp_addr,
                    count(DISTINCT j.ak) FILTER (f.snf) AS n_snf_addr
             FROM j LEFT JOIN fac f ON f.addr_key = j.ak GROUP BY j.tin_value),
    orgs AS (SELECT tin_value,
                    list_sort(list_distinct(flatten(list(string_split(taxes, '|'))))) AS org_taxonomies
             FROM j WHERE entity_type = 'NPI-2' AND nullif(taxes, '') IS NOT NULL
             GROUP BY tin_value),
    {mc}
    SELECT a.tin_value, coalesce(o.org_taxonomies, []::VARCHAR[]) AS org_taxonomies,
           len(list_filter(coalesce(o.org_taxonomies, []::VARCHAR[]),
               x -> x IN ({clinic_codes}) OR {any_prefix('x', THERAPY_TAXONOMY_PREFIXES)})) > 0 AS org_clinic,
           len(list_filter(coalesce(o.org_taxonomies, []::VARCHAR[]),
               x -> {any_prefix('x', HOSPITAL_TAXONOMY_PREFIXES)})) > 0 AS org_hospital,
           len(list_filter(coalesce(o.org_taxonomies, []::VARCHAR[]),
               x -> {any_prefix('x', NON_OUTPATIENT_TAXONOMY_PREFIXES)})) > 0 AS org_facility,
           a.n_addr, a.n_hosp_addr, a.n_snf_addr,
           m.mc_npis, m.mc_ptpp, m.mc_ptpp_share, m.mc_office_share
    FROM addr a LEFT JOIN orgs o ON o.tin_value = a.tin_value
    LEFT JOIN mc m ON m.tin_value = a.tin_value
    """


# the ordered rules over a profile row p and its directory row td
PRACTICE_TYPE_SQL = """CASE
    WHEN p.org_hospital OR coalesce(td.has_hospital, FALSE)
         OR (p.n_addr > 0 AND p.n_hosp_addr * 2 > p.n_addr) THEN 'hospital'
    WHEN p.org_facility OR coalesce(td.has_facility, FALSE)
         OR (p.n_addr > 0 AND p.n_snf_addr * 2 > p.n_addr) THEN 'facility'
    WHEN p.org_clinic OR coalesce(td.is_therapy, FALSE)
         -- Medicare: most of its therapy SERVICES billed by therapists in
         -- private practice, and therapists are at least half of its
         -- identified clinicians (a 4,511-clinician health system with 124
         -- therapists is not a clinic)
         OR (coalesce(p.mc_ptpp_share, 0) >= 0.5
             AND (coalesce(td.classified_npi_count, 0) = 0
                  OR td.therapy_npi_count >= 0.5 * td.classified_npi_count)) THEN 'outpatient_clinic'
    -- an orthopedic / physician group whose therapists bill most of its
    -- therapy (25-50% therapists): BCBS TN paid one $88.04 for 97110 against
    -- $24.23 at clinics — physician contracts, not a clinic's peer
    WHEN coalesce(p.mc_ptpp_share, 0) >= 0.5
         AND td.therapy_npi_count >= 0.25 * td.classified_npi_count THEN 'physician_therapy'
    WHEN coalesce(td.classified_npi_count, 0) > 0 THEN 'physician_other'
    ELSE 'unknown' END"""

TYPE_REASON_SQL = """array_to_string(list_filter([
    CASE WHEN p.org_hospital THEN 'a hospital organization NPI' END,
    CASE WHEN p.n_hosp_addr > 0 THEN p.n_hosp_addr || ' of ' || p.n_addr
         || ' practice addresses are CMS-listed hospitals' END,
    CASE WHEN p.org_facility THEN 'a facility organization NPI (SNF, home health …)' END,
    CASE WHEN p.n_snf_addr > 0 THEN p.n_snf_addr || ' of ' || p.n_addr
         || ' practice addresses are CMS-listed nursing homes' END,
    CASE WHEN p.org_clinic THEN 'an organization NPI registered as a therapy clinic or therapist group' END,
    CASE WHEN coalesce(td.classified_npi_count, 0) > 0 THEN td.therapy_npi_count || ' of '
         || td.classified_npi_count || ' identified clinicians are PT/OT/SLP' END,
    CASE WHEN coalesce(p.mc_npis, 0) > 0 THEN coalesce(p.mc_ptpp, 0) || ' of ' || p.mc_npis
         || ' clinicians billing therapy to Medicare bill as therapists in private practice'
         || coalesce(', ' || round(100 * p.mc_ptpp_share)::INT || '% of the services', '')
         || coalesce(' (' || round(100 * p.mc_office_share)::INT || '% in the office)', '') END
  ], x -> x IS NOT NULL), '; ')"""
