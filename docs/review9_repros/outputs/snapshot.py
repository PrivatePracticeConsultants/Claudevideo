"""Round-4 baseline: capture the BODY of every dashboard endpoint (Rates,
Consulting, Growth, Practice) on the named 32M-rate store, plus its time.

usage: python snapshot.py <store-parent-dir> <out-dir> [--only substr]

Each response is normalized (volatile stamps removed — see VOLATILE) and
written to <out-dir>/<nn>_<name>.<ext>; timings to <out-dir>/_timings.json.
Diff two out-dirs with diff_snap.py. Read-only on the store (no POST that
writes: clients, peersets, engagements, packets are GET-only here).
"""
import io
import json
import re
import sys
import time
import zipfile
from pathlib import Path

VOLATILE_KEYS = re.compile(
    r"^(generated(_at|_utc)?|generation|data_generation|now|as_of_time|elapsed|took_s|"
    r"computed_at|server_time|cache_age_s|checked_at)$")
VOLATILE_TEXT = [
    (re.compile(r"Generated: [0-9T:\-+.]+"), "Generated: <T>"),
    (re.compile(r"\b20\d\d-\d\d-\d\d[T ]\d\d:\d\d(:\d\d(\.\d+)?)?([+-]\d\d:\d\d|Z)?\b"), "<TS>"),
    (re.compile(r"generated [A-Z][a-z]+ \d{1,2}, \d{4}[^<]*"), "generated <D>"),
]


def norm_json(o):
    if isinstance(o, dict):
        return {k: norm_json(v) for k, v in sorted(o.items()) if not VOLATILE_KEYS.match(k)}
    if isinstance(o, list):
        return [norm_json(v) for v in o]
    if isinstance(o, str):
        return norm_text(o)
    return o


def norm_text(s: str) -> str:
    for rx, rep in VOLATILE_TEXT:
        s = rx.sub(rep, s)
    return s


def calls_for(c, con):
    """(name, method, url, body) for every report endpoint, with real keys."""
    if con.execute("SELECT count(*) FROM rates_by_tin WHERE payer = 'Payer 00'").fetchone()[0]:
        return calls_for_scale(c, con)
    tin = con.execute(
        "SELECT t.tin_value FROM rates_by_tin t JOIN tin_directory d USING (tin_value) "
        "WHERE t.payer = 'Blue Cross and Blue Shield of Alabama' AND NOT t.tin_is_really_npi "
        "AND t.billing_class = 'professional' AND list_contains(d.states, 'AL') AND d.is_therapy "
        "AND d.display_name IS NOT NULL "
        "GROUP BY 1 ORDER BY count(DISTINCT t.billing_code) DESC, max(d.npi_count) DESC, 1 "
        "LIMIT 1").fetchone()[0]
    tin_wy = con.execute("SELECT t.tin_value FROM rates_by_tin t JOIN tin_directory d USING (tin_value) "
                         "WHERE t.payer = 'Blue Cross Blue Shield of Wyoming' AND d.display_name IS NOT NULL "
                         "GROUP BY 1 ORDER BY count(DISTINCT file_month) DESC, count(*) DESC, 1 LIMIT 1"
                         ).fetchone()[0]
    npi = con.execute("SELECT min(npi) FROM rates WHERE tin_value = ? AND npi IS NOT NULL",
                      [tin]).fetchone()[0]
    name = con.execute("SELECT display_name FROM tin_directory WHERE tin_value = ?", [tin]).fetchone()[0]
    AL = "Blue Cross and Blue Shield of Alabama"
    WY = "Blue Cross Blue Shield of Wyoming"
    PRE = "Premera Blue Cross"
    ek = c.get(f"/api/rates?grain=entity&payer={AL}&cpt=97110&q={tin}&latest=1").json()["rows"]
    entity_key = ek[0]["unit_id"] if ek else name
    print("subject tin", tin, "wy tin", tin_wy, "npi", npi, "entity", entity_key, flush=True)
    M = {"payers": [AL], "state": "AL", "month": "latest"}
    MW = {"payers": [WY], "month": "latest"}
    g = []
    G = lambda n, u: g.append((n, "GET", u, None))           # noqa: E731
    P = lambda n, u, b: g.append((n, "POST", u, b))          # noqa: E731
    # ---- Practice
    G("stats", "/api/stats")
    G("overview", "/api/overview")
    G("overview_AL", "/api/overview?state=AL")
    G("months", "/api/months")
    G("states", "/api/states")
    G("payers", "/api/payers")
    G("clients", "/api/clients")
    G("clients_digest", "/api/clients/digest")
    G("quality_freshness", "/api/quality/freshness")
    P("quality_types", "/api/quality/types", {"market": M})
    P("quality_posture", "/api/quality/posture", {"market": M})
    P("contracts_renewals", "/api/contracts/renewals",
      {"subjects": [tin, tin_wy], "within_days": 120, "market": {"month": "latest"}})
    P("contracts_new_to_network", "/api/contracts/new-to-network",
      {"market": {"month": "latest", "payers": [WY]}, "zip": None, "radius_miles": 25})
    # ---- Rates: Explorer table + summary strip, each grain / latest / pooled / filters
    rates_q = [
        ("tin_latest", "grain=tin&latest=1"),
        ("tin_pooled", "grain=tin"),
        ("tin_latest_p2", "grain=tin&latest=1&page=2"),
        ("tin_latest_sort_name", "grain=tin&latest=1&sort=display_name&dir=asc"),
        ("tin_latest_code", "grain=tin&latest=1&cpt=97110"),
        ("tin_latest_payer_code", f"grain=tin&latest=1&payer={AL}&cpt=97110"),
        ("tin_latest_state", "grain=tin&latest=1&state=WY"),
        ("tin_latest_therapy_base", "grain=tin&latest=1&therapy_only=1&modifier=base&cpt=97110"),
        ("tin_latest_ratemax", f"grain=tin&latest=1&payer={WY}&rate_max=60"),
        ("tin_latest_q_tin", f"grain=tin&latest=1&q={tin_wy}"),
        ("tin_latest_q_name", "grain=tin&latest=1&q=therapy"),
        ("tin_latest_outliers", "grain=tin&latest=1&cpt=97110&hide_outliers=1"),
        ("tin_pooled_wy", f"grain=tin&payer={WY}"),
        ("tin_month", "grain=tin&month=2026-08"),
        ("tin_latest_hide_npi_disc", "grain=tin&latest=1&hide_tin_npi=1&discipline=PT&cpt=97530"),
        ("tin_latest_city", "grain=tin&latest=1&state=AL&city=Birmingham"),
        ("entity_latest", "grain=entity&latest=1"),
        ("entity_latest_payer_code", f"grain=entity&latest=1&payer={AL}&cpt=97110"),
        ("entity_pooled_wy", f"grain=entity&payer={WY}&cpt=97110"),
        ("entity_latest_state", "grain=entity&latest=1&state=WY&cpt=97110"),
        ("npi_payer_code", f"grain=npi&payer={AL}&cpt=97110"),
        ("npi_latest_payer_code", f"grain=npi&latest=1&payer={WY}&cpt=97110"),
        ("npi_ident", f"grain=npi&q={npi}"),
    ]
    for n, q in rates_q:
        G(f"rates_{n}", f"/api/rates?{q}&page_size=100")
        G(f"summary_{n}", f"/api/summary?{q}")
    # ---- Code page
    for n, q in [("tin", "grain=tin"), ("tin_base", "grain=tin&modifier=base"),
                 ("tin_state", "grain=tin&state=AL"), ("tin_pooled", "grain=tin&latest=0"),
                 ("entity", "grain=entity"), ("tin_therapy", "grain=tin&therapy_only=1")]:
        G(f"code_97110_{n}", f"/api/code/97110?{q}")
    G("code_92507", "/api/code/92507?grain=tin")
    G("code_97140_wy", f"/api/code/97140?grain=tin&payer={WY}")
    G("trend_97110", "/api/trend?cpt=97110&grain=tin")
    G("trend_97110_base", "/api/trend?cpt=97110&grain=tin&modifier=base")
    G("trend_wy", f"/api/trend?cpt=97110&grain=tin&payer={WY}")
    G("trend_entity", f"/api/trend?cpt=97110&grain=entity&payer={WY}")
    # ---- drawers
    G("entity_tin", f"/api/entity/tin/{tin}")
    G("entity_tin_wy", f"/api/entity/tin/{tin_wy}")
    G("entity_entity", f"/api/entity/entity/{entity_key}")
    G("entity_npi", f"/api/entity/npi/{npi}")
    # ---- Markets / changes
    for n, body in [("97110_AL", {"code": "97110", "market": {"state": "AL", "month": "latest"}}),
                    ("97110_all", {"code": "97110", "market": {"month": "latest"}})]:
        for ep in ("geography", "negotiability", "medicare", "differential"):
            P(f"market_{ep}_{n}", f"/api/market/{ep}", body)
    P("territory_local", "/api/territory/local", {"code": "97110", "market": M, "state": "AL"})
    P("territory_radius", "/api/territory/radius",
      {"code": "97110", "market": {"month": "latest"}, "zip": "35203", "max_miles": 100})
    P("territory_concentration", "/api/territory/concentration", {"market": M})
    P("territory_concentration_all", "/api/territory/concentration", {"market": {"month": "latest"}})
    P("changes_wy", "/api/changes", {"market": {"month": "2026-10", "prev_month": "2026-08",
                                                "payers": [WY]}})
    P("trajectory", "/api/trajectory", {"market": {"payers": [WY]}})
    # ---- Consulting
    G("benchmark_subjects", "/api/benchmark/subjects")
    G("benchmark_subjects_q", "/api/benchmark/subjects?q=therapy")
    P("benchmark_AL", "/api/benchmark/market", {"subject": tin, "market": M})
    P("benchmark_all", "/api/benchmark/market", {"subject": tin, "market": {"month": "latest"}})
    P("benchmark_wy_pooled", "/api/benchmark/market",
      {"subject": tin_wy, "market": {"payers": [WY], "month": "2026-08"}})
    P("benchmark_entity", "/api/benchmark/market", {"subject": entity_key, "market": M})
    P("benchmark_opportunity", "/api/benchmark/opportunity",
      {"subject": tin, "market": M, "volumes": {"97110": 1000, "97140": 400}})
    P("report_pitch", "/api/report/pitch", {"subject": tin, "market": M})
    P("report_negotiation", "/api/report/negotiation", {"subject": tin, "market": M})
    P("negotiate_compare", "/api/negotiate/compare",
      {"subject": tin, "payer": AL, "market": {"month": "latest", "state": "AL"}, "comparables": []})
    P("negotiate_proposal", "/api/negotiate/proposal",
      {"subject": tin, "payer": AL, "market": {"month": "latest", "state": "AL"}, "comparables": [],
       "target": {"kind": "p50"}})
    P("report_payer_compare", "/api/report/payer-compare",
      {"subject": tin, "payer": AL, "market": {"month": "latest", "state": "AL"}, "comparables": []})
    P("schedule_fee", "/api/schedule/fee", {"subject": tin, "market": M})
    P("schedule_fee_all", "/api/schedule/fee", {"subject": tin_wy, "market": {"month": "latest"}})
    P("schedule_fee_csv", "/api/schedule/fee.csv", {"subject": tin, "market": M})
    P("report_ratecard", "/api/report/ratecard", {"subject": tin, "market": M})
    P("contract_gaps", "/api/contract-gaps", {"subject": tin, "market": {"month": "latest"}})
    P("quality_size", "/api/quality/size", {"subject": tin, "market": M})
    P("quality_subject_types", "/api/quality/subject-types", {"subject": tin, "market": M})
    P("leverage", "/api/leverage", {"subject": tin, "payer": AL, "market": M})
    P("leverage_summary", "/api/leverage/summary", {"subject": tin, "market": M})
    P("economics_visit", "/api/economics/visit", {"subject": tin, "market": M})
    P("report_market", "/api/report/market", {"state": "AL", "code": "97110", "market": M})
    P("dossier_json", "/api/report/dossier.json",
      {"subject": tin, "radius_miles": 25, "market": {"month": "latest", "therapy_only": False}})
    # ---- Growth
    P("leads_AL", "/api/leads", {"market": M, "threshold_percentile": 25, "min_codes": 3})
    P("leads_all", "/api/leads", {"market": {"month": "latest"}, "threshold_percentile": 25,
                                  "min_codes": 3})
    P("leads_csv", "/api/leads.csv", {"market": M, "threshold_percentile": 25, "min_codes": 3})
    P("leaderboard", "/api/leaderboard", {"market": M, "min_codes": 3, "sort": "median", "limit": 50})
    P("leaderboard_all", "/api/leaderboard", {"market": {"month": "latest"}, "min_codes": 3,
                                              "sort": "median", "limit": 50})
    P("roster", "/api/roster", {"payer": AL, "market": {"month": "latest", "state": "AL"},
                                "min_codes": 1, "sort": "codes", "limit": 500})
    P("roster_csv", "/api/roster.csv", {"payer": WY, "market": {"month": "latest"},
                                        "min_codes": 1, "sort": "codes", "limit": 500})
    G("medicare_status", "/api/medicare/status")
    # ---- exports
    G("export_csv_latest_code", "/api/export.csv?grain=tin&latest=1&cpt=97110")
    G("export_csv_wy_pooled", f"/api/export.csv?grain=tin&payer={WY}")
    G("export_zip_entity", f"/api/export.zip?grain=entity&latest=1&payer={AL}&cpt=97110")
    G("export_csv_all_latest", "/api/export.csv?grain=tin&latest=1")
    return g


def calls_for_scale(c, con):
    """The same screens on the synthetic multi-month scale store (scale4)."""
    tin = con.execute(
        "SELECT t.tin_value FROM rates_by_tin t JOIN tin_directory d USING (tin_value) "
        "WHERE t.payer = 'Payer 03' AND list_contains(d.states, 'MO') "
        "GROUP BY 1 ORDER BY count(DISTINCT t.billing_code) DESC, count(*) DESC, 1 LIMIT 1").fetchone()[0]
    npi = con.execute("SELECT min(npi) FROM rates WHERE tin_value = ? AND payer = 'Payer 03'",
                      [tin]).fetchone()[0]
    name = con.execute("SELECT display_name FROM tin_directory WHERE tin_value = ?", [tin]).fetchone()[0]
    P3, P4 = "Payer 03", "Payer 04"
    M = {"payers": [P3], "state": "MO", "month": "latest"}
    print("scale subject tin", tin, "npi", npi, "name", name, flush=True)
    g = []
    G = lambda n, u: g.append((n, "GET", u, None))           # noqa: E731
    P = lambda n, u, b: g.append((n, "POST", u, b))          # noqa: E731
    G("stats", "/api/stats")
    G("overview", "/api/overview")
    G("overview_MO", "/api/overview?state=MO")
    G("months", "/api/months")
    for n, q in [("tin_latest", "grain=tin&latest=1"), ("tin_pooled", "grain=tin"),
                 ("tin_latest_code", "grain=tin&latest=1&cpt=97110"),
                 ("tin_latest_payer_code", f"grain=tin&latest=1&payer={P3}&cpt=97110"),
                 ("tin_latest_state", "grain=tin&latest=1&state=MO"),
                 ("tin_latest_ratemax", f"grain=tin&latest=1&payer={P3}&rate_max=60"),
                 ("tin_latest_q_tin", f"grain=tin&latest=1&q={tin}"),
                 ("tin_latest_q_name", "grain=tin&latest=1&q=PRACTICE 123"),
                 ("tin_month", "grain=tin&month=2026-09"),
                 ("entity_latest_payer_code", f"grain=entity&latest=1&payer={P3}&cpt=97110"),
                 ("npi_payer_code", f"grain=npi&latest=1&payer={P3}&cpt=97110"),
                 ("npi_ident", f"grain=npi&q={npi}")]:
        G(f"rates_{n}", f"/api/rates?{q}&page_size=100")
        G(f"summary_{n}", f"/api/summary?{q}")
    for n, q in [("tin", "grain=tin"), ("tin_base", "grain=tin&modifier=base"),
                 ("tin_state", "grain=tin&state=MO"), ("tin_pooled", "grain=tin&latest=0"),
                 ("entity_payer", f"grain=entity&payer={P3}")]:
        G(f"code_97110_{n}", f"/api/code/97110?{q}")
    G("trend_97110", "/api/trend?cpt=97110&grain=tin")
    G("trend_payer", f"/api/trend?cpt=97110&grain=tin&payer={P3}")
    G("entity_tin", f"/api/entity/tin/{tin}")
    G("entity_npi", f"/api/entity/npi/{npi}")
    body = {"code": "97110", "market": {"state": "MO", "month": "latest"}}
    for ep in ("geography", "negotiability", "medicare", "differential"):
        P(f"market_{ep}", f"/api/market/{ep}", body)
    P("territory_concentration", "/api/territory/concentration", {"market": M})
    P("changes", "/api/changes", {"market": {"month": "2026-10", "prev_month": "2026-09",
                                             "payers": [P3]}})
    P("trajectory", "/api/trajectory", {"market": {"payers": [P3, P4]}})
    G("benchmark_subjects", "/api/benchmark/subjects")
    P("benchmark_MO", "/api/benchmark/market", {"subject": tin, "market": M})
    P("benchmark_all", "/api/benchmark/market", {"subject": tin, "market": {"month": "latest"}})
    P("benchmark_name", "/api/benchmark/market", {"subject": name, "market": {"month": "latest", "state": "MO"}})
    P("schedule_fee", "/api/schedule/fee", {"subject": tin, "market": {"month": "latest"}})
    P("negotiate_compare", "/api/negotiate/compare",
      {"subject": tin, "payer": P3, "market": {"month": "latest"}, "comparables": []})
    P("contract_gaps", "/api/contract-gaps", {"subject": tin, "market": {"month": "latest"}})
    P("report_market", "/api/report/market", {"state": "MO", "code": "97110", "market": M})
    P("leads_MO", "/api/leads", {"market": M, "threshold_percentile": 25, "min_codes": 3})
    P("leaderboard", "/api/leaderboard", {"market": {"month": "latest", "state": "MO"}, "min_codes": 3,
                                          "sort": "median", "limit": 50})
    P("roster", "/api/roster", {"payer": P3, "market": {"month": "latest", "state": "MO"},
                                "min_codes": 1, "sort": "codes", "limit": 500})
    P("contracts_renewals", "/api/contracts/renewals",
      {"subjects": [tin], "within_days": 120, "market": {"month": "latest"}})
    P("quality_posture", "/api/quality/posture", {"market": M})
    G("export_csv_latest_code", f"/api/export.csv?grain=tin&latest=1&cpt=97110&payer={P3}")
    G("export_zip_state", "/api/export.zip?grain=tin&latest=1&state=MO&cpt=97110")
    return g


def body_of(r):
    ct = r.headers.get("content-type", "")
    if "zip" in ct or r.content[:4] == b"PK\x03\x04":
        z = zipfile.ZipFile(io.BytesIO(r.content))
        parts = []
        for n in sorted(z.namelist()):
            parts.append(f"=== {n}\n" + norm_text(z.read(n).decode("utf-8", "replace")))
        return "txt", "\n".join(parts)
    if "json" in ct:
        return "json", json.dumps(norm_json(r.json()), indent=1, sort_keys=True, default=str)
    return "txt", norm_text(r.text)


if __name__ == "__main__":
    import mrfx.api as api
    from fastapi.testclient import TestClient
    from mrfx.config import MrfxConfig
    from mrfx.store import Store
    d = Path(sys.argv[1])
    out = Path(sys.argv[2])
    only = sys.argv[sys.argv.index("--only") + 1] if "--only" in sys.argv else None
    out.mkdir(parents=True, exist_ok=True)
    cfg = MrfxConfig(inbox_dir=d / "in", processed_dir=d / "p", failed_dir=d / "f",
                     store_dir=d / "store", downloads_dir=d / "dl", entity_map_path=d / "em.yaml")
    cfg.ensure_dirs()
    st = Store(cfg.store_dir)
    # a 500 (e.g. out of memory) is RECORDED, never aborts the run
    api.TRUSTED_HOSTS.add("testserver")
    c = TestClient(api.create_app(cfg, st), raise_server_exceptions=False)
    with st.connect() as con:
        calls = calls_for(c, con)
    timings = {}
    for i, (name, method, url, body) in enumerate(calls):
        if only and only not in name:
            continue
        ts = []
        for _ in range(1 if "--once" in sys.argv else 2):
            t = time.perf_counter()
            r = c.post(url, json=body) if method == "POST" else c.get(url)
            ts.append(time.perf_counter() - t)
        ext, text = body_of(r)
        (out / f"{i:03d}_{name}.{ext}").write_text(f"HTTP {r.status_code}\n{text}\n")
        timings[name] = {"status": r.status_code, "cold": round(ts[0], 3), "warm": round(ts[-1], 3),
                         "bytes": len(r.content), "url": url}
        print(f"{ts[0]:7.2f}s {ts[-1]:7.2f}s {r.status_code} {len(r.content):>9} {name}", flush=True)
    (out / "_timings.json").write_text(json.dumps(timings, indent=1))
