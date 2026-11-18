"""Every output feature the dashboard can trigger, with the payload the UI
sends (defaults as in index.html/app.js), for the 99M scale store.
Each entry: (name, view, method, url, body, variant) where variant is
(url2, body2) for a third call with a different filter, or None."""
TIN = "100049428"            # PRACTICE 12428, MO, priced by Payer 01-04
NPI = "1000494280"
NAME = "PRACTICE 18"         # an NPPES name shared by TINs in OR/MA/IL
ENTITY = "PRACTICE 18 · OR"
CLIENT = "PRACTICE 0"        # the watchlist's one client (MO)
P3, P4, P7 = "Payer 03", "Payer 04", "Payer 07"
ZIP = "63101"                # a real St. Louis ZIP (the store's own ZIPs are synthetic 5-digit strings)
VOL = {"97110": 3000, "97140": 1500, "97530": 800}
M_MO = {"month": "latest", "state": "MO"}
BM = lambda **k: {"month": "latest", "payers": [], "billing_class": "professional",  # noqa: E731
                  "target_percentile": 50, "therapy_only": True, **k}
NG = lambda **k: {"month": "latest", "therapy_only": False, **k}  # noqa: E731
RC = lambda **k: {"month": "latest", "payers": [], "billing_class": "professional", **k}  # noqa: E731
LD = lambda **k: {"month": "latest", "payers": [], "therapy_only": True, **k}  # noqa: E731
REMIT = "code,payer,allowed_amount,units\n97110,Payer 03,28.50,1\n97140,Payer 03,26.00,1\n97530,Payer 03,31.00,2\n"


def calls():
    c = []

    def G(name, view, url, var=None):
        c.append((name, view, "GET", url, None, var))

    def P(name, view, url, body, var=None):
        c.append((name, view, "POST", url, body, var))

    # ---- boot (what index.html fires on load)
    G("boot", "boot", "/api/boot")
    G("stats", "boot", "/api/stats")
    G("catalog", "boot", "/api/catalog")
    G("states", "boot", "/api/states")
    G("payers", "boot", "/api/payers")
    G("months", "boot", "/api/months")
    G("plans", "boot", "/api/plans")
    G("peersets", "boot", "/api/peersets")
    G("entities_map", "boot", "/api/entities/map")
    G("benchmark_subjects", "boot", "/api/benchmark/subjects", ("/api/benchmark/subjects?q=PRACTICE%20124", None))
    G("mpfs_status", "boot", "/api/mpfs/status")
    # ---- Overview
    G("overview", "overview", "/api/overview", ("/api/overview?state=MO", None))
    G("quality_freshness", "overview", "/api/quality/freshness")
    P("quality_types", "overview", "/api/quality/types", {"market": {"month": "latest"}},
      ("/api/quality/types", {"market": M_MO}))
    P("quality_posture", "overview", "/api/quality/posture", {"market": {"month": "latest"}},
      ("/api/quality/posture", {"market": M_MO}))
    # ---- Explorer (table + strip, default filters: grain tin, latest, dollar)
    base = "grain=tin&latest=1&dollar_only=1"
    G("rates_tin_default", "explorer", f"/api/rates?{base}&page=1&page_size=100",
      (f"/api/rates?{base}&state=MO&page=1&page_size=100", None))
    G("summary_tin_default", "explorer", f"/api/summary?{base}", (f"/api/summary?{base}&state=MO", None))
    G("rates_tin_payer_code", "explorer", f"/api/rates?{base}&payer=Payer%2003&cpt=97110&page=1&page_size=100",
      (f"/api/rates?{base}&payer=Payer%2007&cpt=97140&page=1&page_size=100", None))
    G("summary_tin_payer_code", "explorer", f"/api/summary?{base}&payer=Payer%2003&cpt=97110",
      (f"/api/summary?{base}&payer=Payer%2007&cpt=97140", None))
    G("rates_tin_q_name", "explorer", f"/api/rates?{base}&q=PRACTICE%20124&page=1&page_size=100",
      (f"/api/rates?{base}&q=PRACTICE%2018&page=1&page_size=100", None))
    G("summary_tin_q_name", "explorer", f"/api/summary?{base}&q=PRACTICE%20124",
      (f"/api/summary?{base}&q=PRACTICE%2018", None))
    G("rates_tin_q_tin", "explorer", f"/api/rates?{base}&q={TIN}&page=1&page_size=100",
      (f"/api/rates?{base}&q=100071064&page=1&page_size=100", None))
    G("rates_tin_month", "explorer", "/api/rates?grain=tin&month=2026-09&dollar_only=1&page=1&page_size=100",
      ("/api/rates?grain=tin&month=2026-08&dollar_only=1&page=1&page_size=100", None))
    G("summary_tin_month", "explorer", "/api/summary?grain=tin&month=2026-09&dollar_only=1",
      ("/api/summary?grain=tin&month=2026-08&dollar_only=1", None))
    G("rates_tin_sort_name_p2", "explorer", f"/api/rates?{base}&state=MO&sort=display_name&dir=asc&page=2&page_size=100")
    G("rates_entity_default", "explorer", "/api/rates?grain=entity&latest=1&dollar_only=1&page=1&page_size=100",
      ("/api/rates?grain=entity&latest=1&dollar_only=1&state=MO&page=1&page_size=100", None))
    G("summary_entity_default", "explorer", "/api/summary?grain=entity&latest=1&dollar_only=1",
      ("/api/summary?grain=entity&latest=1&dollar_only=1&state=MO", None))
    G("rates_entity_payer_code", "explorer", "/api/rates?grain=entity&latest=1&dollar_only=1&payer=Payer%2003&cpt=97110&page=1&page_size=100",
      ("/api/rates?grain=entity&latest=1&dollar_only=1&payer=Payer%2007&cpt=97140&page=1&page_size=100", None))
    G("rates_npi_payer_code", "explorer", "/api/rates?grain=npi&latest=1&dollar_only=1&payer=Payer%2003&cpt=97110&page=1&page_size=100",
      ("/api/rates?grain=npi&latest=1&dollar_only=1&payer=Payer%2007&cpt=97140&page=1&page_size=100", None))
    G("summary_npi_payer_code", "explorer", "/api/summary?grain=npi&latest=1&dollar_only=1&payer=Payer%2003&cpt=97110",
      ("/api/summary?grain=npi&latest=1&dollar_only=1&payer=Payer%2007&cpt=97140", None))
    G("rates_npi_q_npi", "explorer", f"/api/rates?grain=npi&latest=1&dollar_only=1&q={NPI}&page=1&page_size=100",
      ("/api/rates?grain=npi&latest=1&dollar_only=1&q=1000710640&page=1&page_size=100", None))
    G("entity_drawer_tin", "explorer", f"/api/entity/tin/{TIN}", ("/api/entity/tin/100071064", None))
    G("entity_drawer_entity", "explorer", "/api/entity/entity/PRACTICE%2018%20%C2%B7%20OR", ("/api/entity/entity/PRACTICE%2012428", None))
    G("entity_drawer_npi", "explorer", f"/api/entity/npi/{NPI}", ("/api/entity/npi/1000710640", None))
    P("validate", "explorer", "/api/validate", {"id": TIN, "code": "97110"}, ("/api/validate", {"id": NPI, "code": "97140", "expected_rate": 30}))
    G("export_csv_payer_code", "explorer", f"/api/export.csv?{base}&payer=Payer%2003&cpt=97110",
      (f"/api/export.csv?{base}&payer=Payer%2007&cpt=97140", None))
    G("export_zip_state_code", "explorer", f"/api/export.zip?{base}&state=MO&cpt=97110",
      (f"/api/export.zip?{base}&state=TX&cpt=97140", None))
    G("export_csv_all_latest", "explorer", f"/api/export.csv?{base}")
    G("export_zip_entity_payer_code", "explorer", "/api/export.zip?grain=entity&latest=1&dollar_only=1&payer=Payer%2003&cpt=97110")
    G("outreach_zip_tin_MO", "explorer", f"/api/export/outreach.zip?{base}&state=MO",
      (f"/api/export/outreach.zip?{base}&state=TX", None))
    G("outreach_csv_entity_MO", "explorer", "/api/export/outreach.csv?grain=entity&latest=1&dollar_only=1&state=MO")
    G("outreach_zip_tin_ALL", "explorer", f"/api/export/outreach.zip?{base}")
    # ---- Code comparison
    G("code_97110", "cpt", "/api/code/97110?grain=tin", ("/api/code/97140?grain=tin", None))
    G("trend_97110", "cpt", "/api/trend?cpt=97110&grain=tin", ("/api/trend?cpt=97140&grain=tin", None))
    G("code_97110_MO", "cpt", "/api/code/97110?grain=tin&state=MO", ("/api/code/97110?grain=tin&state=TX", None))
    G("trend_97110_MO", "cpt", "/api/trend?cpt=97110&grain=tin&state=MO", ("/api/trend?cpt=97110&grain=tin&state=TX", None))
    G("code_97110_base_prof", "cpt", "/api/code/97110?grain=tin&modifier=base&billing_class=professional")
    # ---- Markets
    mk = {"code": "97110", "market": {"month": "latest", "state": "MO"}}
    mk2 = {"code": "97140", "market": {"month": "latest", "state": "TX"}}
    for ep in ("geography", "negotiability", "medicare", "differential"):
        P(f"market_{ep}", "markets", f"/api/market/{ep}", mk, (f"/api/market/{ep}", mk2))
    P("market_geography_national", "markets", "/api/market/geography", {"code": "97110", "market": {"month": "latest"}})
    P("territory_local", "markets", "/api/territory/local", {"code": "97110", "market": M_MO, "state": "MO"},
      ("/api/territory/local", {"code": "97140", "market": {"month": "latest", "state": "TX"}, "state": "TX"}))
    P("territory_radius", "markets", "/api/territory/radius", {"code": "97110", "market": M_MO, "zip": ZIP, "max_miles": 100},
      ("/api/territory/radius", {"code": "97110", "market": {"month": "latest"}, "zip": "75201", "max_miles": 100}))
    P("territory_concentration", "markets", "/api/territory/concentration", {"market": M_MO, "code": "97110"},
      ("/api/territory/concentration", {"market": {"month": "latest", "state": "TX"}, "code": "97140"}))
    # ---- Rate changes
    P("changes_MO_2026-10", "changes", "/api/changes", {"market": {"month": "2026-10", "payers": [], "state": "MO"}},
      ("/api/changes", {"market": {"month": "2026-10", "payers": [], "state": "TX"}}))
    P("changes_MO_payer_prev", "changes", "/api/changes", {"market": {"month": "2026-10", "prev_month": "2026-08", "payers": [P3], "state": "MO"}})
    P("changes_subject", "changes", "/api/changes", {"market": {"month": "2026-10", "payers": []}, "subject": TIN})
    P("changes_csv_MO", "changes", "/api/changes.csv", {"market": {"month": "2026-10", "payers": [], "state": "MO"}})
    P("changes_national", "changes", "/api/changes", {"market": {"month": "2026-10", "payers": []}})
    P("trajectory_MO", "changes", "/api/trajectory", {"market": {"payers": [], "state": "MO"}},
      ("/api/trajectory", {"market": {"payers": [], "state": "TX"}}))
    P("trajectory_national", "changes", "/api/trajectory", {"market": {"payers": [], "allow_national": True}})
    # ---- Benchmark
    P("benchmark_market_MO", "benchmark", "/api/benchmark/market", {"subject": TIN, "market": BM(state="MO"), "volumes": {}},
      ("/api/benchmark/market", {"subject": NAME, "market": BM(state="OR"), "volumes": {}}))
    P("benchmark_market_national", "benchmark", "/api/benchmark/market", {"subject": TIN, "market": BM(), "volumes": {}})
    P("benchmark_opportunity_MO", "benchmark", "/api/benchmark/opportunity", {"subject": TIN, "market": BM(state="MO"), "volumes": VOL},
      ("/api/benchmark/opportunity", {"subject": TIN, "market": BM(state="MO", payers=[P3]), "volumes": VOL}))
    P("contract_gaps", "benchmark", "/api/contract-gaps", {"subject": TIN, "market": BM(state="MO")},
      ("/api/contract-gaps", {"subject": NAME, "market": BM(state="OR")}))
    P("quality_size", "benchmark", "/api/quality/size", {"subject": TIN, "market": BM(state="MO")},
      ("/api/quality/size", {"subject": NAME, "market": BM(state="OR")}))
    P("quality_subject_types", "benchmark", "/api/quality/subject-types", {"subject": TIN, "market": BM(state="MO")},
      ("/api/quality/subject-types", {"subject": NAME, "market": BM(state="OR")}))
    P("report_pitch", "benchmark", "/api/report/pitch", {"subject": TIN, "market": BM(state="MO"), "volumes": VOL},
      ("/api/report/pitch", {"subject": NAME, "market": BM(state="OR"), "volumes": VOL}))
    P("report_negotiation", "benchmark", "/api/report/negotiation", {"subject": TIN, "market": BM(state="MO"), "volumes": VOL},
      ("/api/report/negotiation", {"subject": TIN, "market": BM(state="MO", payers=[P3]), "volumes": VOL}))
    # ---- Negotiate
    ng = {"subject": TIN, "payer": P3, "market": NG(state="MO"), "comparables": []}
    ng2 = {"subject": TIN, "payer": P4, "market": NG(state="MO"), "comparables": ["PRACTICE 34064", "PRACTICE 11612"]}
    P("negotiate_compare", "negotiate", "/api/negotiate/compare", ng, ("/api/negotiate/compare", ng2))
    P("negotiate_proposal", "negotiate", "/api/negotiate/proposal", {**ng, "target": {"kind": "p50"}, "volumes": VOL},
      ("/api/negotiate/proposal", {**ng2, "target": {"kind": "p75"}, "volumes": VOL}))
    P("report_proposal", "negotiate", "/api/report/proposal", {**ng, "target": {"kind": "p50"}, "volumes": VOL})
    P("report_payer_compare", "negotiate", "/api/report/payer-compare", ng, ("/api/report/payer-compare", ng2))
    P("leverage", "negotiate", "/api/leverage", {"subject": TIN, "payer": P3, "radius_miles": 15, "market": NG(state="MO")},
      ("/api/leverage", {"subject": TIN, "payer": P4, "radius_miles": 25, "market": NG(state="MO")}))
    P("leverage_summary", "negotiate", "/api/leverage/summary", {"subject": TIN, "payer": P3, "radius_miles": 15, "market": NG(state="MO")},
      ("/api/leverage/summary", {"subject": NAME, "payer": "", "radius_miles": 15, "market": NG(state="OR")}))
    P("economics_visit", "negotiate", "/api/economics/visit", {"subject": TIN, "units_per_visit": None, "market": NG(state="MO")},
      ("/api/economics/visit", {"subject": TIN, "units_per_visit": 4, "market": NG(state="MO")}))
    P("economics_weighted_position", "negotiate", "/api/economics/weighted-position", {"subject": TIN, "market": NG(state="MO"), "volumes": VOL})
    P("floors_compare", "negotiate", "/api/floors/compare", {"subject": TIN, "payer": P3, "state": "MO", "market": NG()})
    P("hospital_parity", "negotiate", "/api/hospital/parity", {"subject": TIN, "payer": P3, "state": "MO", "market": NG()})
    P("remits_check", "negotiate", "/api/remits/check", {"subject": TIN, "text": REMIT, "basis": "allowed", "market": M_MO},
      ("/api/remits/check", {"subject": TIN, "text": REMIT, "basis": "paid", "market": {"month": "latest"}}))
    P("remits_check_csv", "negotiate", "/api/remits/check.csv", {"subject": TIN, "text": REMIT, "basis": "allowed", "market": M_MO})
    # ---- Rate card
    rc = {"subject": TIN, "market": RC(state="MO", discipline="PT"), "visit_market": True, "preset": "ortho"}
    rc2 = {"subject": NAME, "market": RC(state="OR", discipline="PT"), "visit_market": True, "preset": "ortho"}
    rc_nostate = {"subject": TIN, "market": RC()}
    P("schedule_fee", "ratecard", "/api/schedule/fee", rc, ("/api/schedule/fee", rc2))
    P("schedule_fee_nostate", "ratecard", "/api/schedule/fee", rc_nostate)
    G("visit_presets", "ratecard", "/api/schedule/visit/presets")
    P("schedule_visit", "ratecard", "/api/schedule/visit", rc, ("/api/schedule/visit", rc2))
    vm = {"market": {"month": "latest", "state": "MO", "discipline": "PT", "payers": []}, "preset": "ortho", "subject": TIN}
    vm2 = {"market": {"month": "latest", "state": "OR", "discipline": "OT", "payers": []}, "preset": "ortho", "subject": NAME}
    P("schedule_visit_market", "ratecard", "/api/schedule/visit/market", vm, ("/api/schedule/visit/market", vm2))
    P("schedule_visit_market_csv", "ratecard", "/api/schedule/visit/market.csv", vm)
    P("report_visitvalue", "ratecard", "/api/report/visitvalue", vm)
    P("report_ratecard", "ratecard", "/api/report/ratecard", rc, ("/api/report/ratecard", rc2))
    P("schedule_fee_csv", "ratecard", "/api/schedule/fee.csv", rc)
    P("org_bundle_zip", "ratecard", "/api/report/org-bundle.zip", rc)
    # ---- Leads
    ld = {"market": LD(state="MO"), "threshold_percentile": 25, "min_codes": 3}
    ld2 = {"market": LD(state="TX", payers=[P7]), "threshold_percentile": 25, "min_codes": 3}
    P("leads_MO", "leads", "/api/leads", ld, ("/api/leads", ld2))
    P("leads_national", "leads", "/api/leads", {"market": LD(), "threshold_percentile": 25, "min_codes": 3})
    P("leads_csv_MO", "leads", "/api/leads.csv", ld)
    P("leaderboard_MO", "leads", "/api/leaderboard", {"market": LD(state="MO"), "min_codes": 3, "sort": "median", "limit": 50},
      ("/api/leaderboard", {"market": LD(state="TX"), "min_codes": 3, "sort": "median", "limit": 50}))
    P("leaderboard_national", "leads", "/api/leaderboard", {"market": LD(), "min_codes": 3, "sort": "median", "limit": 50})
    ds = {"subject": TIN, "radius_miles": 25, "market": {"month": "latest", "therapy_only": False, "state": "MO"}}
    ds2 = {"subject": NAME, "radius_miles": 25, "market": {"month": "latest", "therapy_only": False, "state": "OR"}}
    P("report_dossier_json", "leads", "/api/report/dossier.json", ds, ("/api/report/dossier.json", ds2))
    P("report_dossier", "leads", "/api/report/dossier", ds, ("/api/report/dossier", ds2))
    P("report_market", "leads", "/api/report/market", {"state": "MO", "code": "97110", "zip": None, "market": {"month": "latest", "therapy_only": False}},
      ("/api/report/market", {"state": "TX", "code": "97140", "zip": None, "market": {"month": "latest", "therapy_only": False}}))
    P("utilization_growth", "leads", "/api/utilization/growth", {"subject": TIN})
    P("utilization_service_lines", "leads", "/api/utilization/service-lines", {"subject": TIN})
    G("utilization_years", "leads", "/api/utilization/years")
    G("utilization_status", "leads", "/api/utilization/status")
    P("utilization_volumes", "leads", "/api/utilization/volumes", {"subject": TIN, "multiplier": 1, "market": {"state": "MO"}})
    P("territory_steal_share", "leads", "/api/territory/steal-share", {"subject": TIN})
    G("newclinics_status", "leads", "/api/newclinics/status")
    P("newclinics", "leads", "/api/newclinics", {"zip": None, "radius_miles": None, "state": "MO", "days": 90})
    G("closures_status", "leads", "/api/closures/status")
    P("closures", "leads", "/api/closures", {"zip": None, "radius_miles": None, "state": "MO", "days": 90})
    G("demographics_status", "leads", "/api/demographics/status")
    P("market_sizing", "leads", "/api/market/sizing", {"zip": ZIP, "radius_miles": 25})
    # ---- Clients
    G("clients", "clients", "/api/clients")
    G("clients_digest", "clients", "/api/clients/digest")
    G("packets_list", "clients", "/api/packets")
    P("contracts_renewals", "clients", "/api/contracts/renewals", {"subjects": [TIN, CLIENT], "within_days": 120, "market": {"month": "latest"}},
      ("/api/contracts/renewals", {"subjects": [NAME], "within_days": 365, "market": {"month": "latest"}}))
    G("contracts_expiry_coverage", "clients", "/api/contracts/expiry-coverage")
    P("contracts_new_to_network", "clients", "/api/contracts/new-to-network",
      {"market": {"month": "latest"}, "zip": None, "radius_miles": None, "therapy_only": False},
      ("/api/contracts/new-to-network", {"market": {"month": "latest"}, "zip": ZIP, "radius_miles": 25, "therapy_only": True}))
    G("engagements_list", "clients", "/api/engagements")
    P("engagements_compare", "clients", "/api/engagements/compare", {"subject": TIN, "volumes": VOL})
    # ---- Medicare
    G("medicare_status", "medicare", "/api/medicare/status")
    G("medicare_tracker", "medicare", "/api/medicare/tracker")
    P("medicare_leaders", "medicare", "/api/medicare/leaders", {"zip": None, "radius_miles": None, "therapy_only": True, "limit": 100, "dataset_id": None})
    P("medicare_org", "medicare", "/api/medicare/org", {"subject": TIN, "dataset_id": None, "limit": 500})
    P("medicare_eligibility", "medicare", "/api/medicare/eligibility", {"text": f"{NPI}, 1000710640"})
    # ---- Payer roster
    ro = {"payer": P3, "market": {"month": "latest", "state": "MO"}, "min_codes": 1, "sort": "name", "limit": 500}
    P("roster", "roster", "/api/roster", ro, ("/api/roster", {**ro, "payer": P7, "market": {"month": "latest", "state": "TX"}, "sort": "paid"}))
    P("roster_csv", "roster", "/api/roster.csv", ro)
    # ---- Files & imports / Sources
    G("files", "files", "/api/files?limit=500")
    G("urls", "files", "/api/urls")
    G("known_sources", "files", "/api/known-sources")
    G("inflation_status", "files", "/api/inflation/status")
    G("floors_status", "files", "/api/floors/status")
    G("hospital_status", "files", "/api/hospital/status")
    G("sources", "sources", "/api/sources", ("/api/sources?state=MO", None))
    # ---- last: the packet build writes files under <store>/../packets
    P("packets_build", "clients", "/api/packets/build", {})
    return c


if __name__ == "__main__":
    for x in calls():
        print(x[0], x[2], x[3])
    print(len(calls()), "calls")
