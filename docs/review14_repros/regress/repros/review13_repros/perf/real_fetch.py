"""Start `mrfx serve` on the SMALL REAL store (Popen), fetch every number-
bearing output for accuracy checks, save bodies to realout/, stop the server.
usage: real_fetch.py <real.yaml> <port> <subject_tin> <state> <payer> [<name_subject> <state2>]"""
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
PY = "/home/user/Claudevideo/.venv/bin/python"


def call(base, method, url, body=None, timeout=600):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + url, data=data, method=method,
                                 headers={"Content-Type": "application/json"} if data else {})
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read(), time.perf_counter() - t0
    except urllib.error.HTTPError as e:
        return e.code, e.read(), time.perf_counter() - t0


def main():
    cfgp, port, tin, st, payer = sys.argv[1:6]
    name, st2 = (sys.argv[6], sys.argv[7]) if len(sys.argv) > 7 else (None, None)
    base = f"http://localhost:{port}"
    import os; out = HERE / os.environ.get("REALOUT", "realout")
    out.mkdir(exist_ok=True)
    VOL = {"97110": 3000, "97140": 1500, "97530": 800}
    BM = {"month": "latest", "payers": [], "billing_class": "professional", "target_percentile": 50,
          "therapy_only": True, "state": st}
    BM_nt = {**BM, "therapy_only": False}
    NG = {"month": "latest", "therapy_only": False, "state": st}
    RC = {"month": "latest", "payers": [], "billing_class": "professional", "state": st, "discipline": "PT"}
    c = [
        ("overview", "GET", "/api/overview", None),
        (f"overview_{st}", "GET", f"/api/overview?state={st}", None),
        ("summary_tin", "GET", "/api/summary?grain=tin&latest=1&dollar_only=1", None),
        ("summary_tin_payer_97110", "GET", f"/api/summary?grain=tin&latest=1&dollar_only=1&payer={urllib.parse.quote(payer)}&cpt=97110", None),
        ("rates_tin_97110", "GET", f"/api/rates?grain=tin&latest=1&dollar_only=1&cpt=97110&page=1&page_size=1000", None),
        ("rates_tin_subject", "GET", f"/api/rates?grain=tin&latest=1&dollar_only=1&q={tin}&page=1&page_size=1000", None),
        ("rates_entity_97110", "GET", f"/api/rates?grain=entity&latest=1&dollar_only=1&cpt=97110&page=1&page_size=1000", None),
        ("rates_npi_97110_payer", "GET", f"/api/rates?grain=npi&latest=1&dollar_only=1&cpt=97110&payer={urllib.parse.quote(payer)}&page=1&page_size=1000", None),
        ("code_97110", "GET", "/api/code/97110?grain=tin", None),
        (f"code_97110_{st}", "GET", f"/api/code/97110?grain=tin&state={st}", None),
        ("trend_97110", "GET", "/api/trend?cpt=97110&grain=tin", None),
        ("entity_tin", "GET", f"/api/entity/tin/{tin}", None),
        ("market_geography", "POST", "/api/market/geography", {"code": "97110", "market": {"month": "latest", "state": st}}),
        ("market_differential", "POST", "/api/market/differential", {"code": "97110", "market": {"month": "latest", "state": st}}),
        ("market_negotiability", "POST", "/api/market/negotiability", {"code": "97110", "market": {"month": "latest", "state": st}}),
        ("territory_local", "POST", "/api/territory/local", {"code": "97110", "market": {"month": "latest", "state": st}, "state": st}),
        ("territory_concentration", "POST", "/api/territory/concentration", {"market": {"month": "latest", "state": st}, "code": "97110"}),
        ("changes_latest", "POST", "/api/changes", {"market": {"month": "latest", "payers": []}}),
        ("trajectory", "POST", "/api/trajectory", {"market": {"payers": [], "allow_national": True}}),
        ("benchmark", "POST", "/api/benchmark/market", {"subject": tin, "market": BM, "volumes": {}}),
        ("benchmark_nt", "POST", "/api/benchmark/market", {"subject": tin, "market": BM_nt, "volumes": {}}),
        ("benchmark_national", "POST", "/api/benchmark/market", {"subject": tin, "market": {k: v for k, v in BM_nt.items() if k != "state"}, "volumes": {}}),
        ("opportunity", "POST", "/api/benchmark/opportunity", {"subject": tin, "market": BM_nt, "volumes": VOL}),
        ("contract_gaps", "POST", "/api/contract-gaps", {"subject": tin, "market": BM_nt}),
        ("quality_size", "POST", "/api/quality/size", {"subject": tin, "market": BM_nt}),
        ("negotiate_compare", "POST", "/api/negotiate/compare", {"subject": tin, "payer": payer, "market": NG, "comparables": []}),
        ("negotiate_proposal", "POST", "/api/negotiate/proposal", {"subject": tin, "payer": payer, "market": NG, "comparables": [], "target": {"kind": "p50"}, "volumes": VOL}),
        ("report_payer_compare", "POST", "/api/report/payer-compare", {"subject": tin, "payer": payer, "market": NG, "comparables": []}),
        ("report_negotiation", "POST", "/api/report/negotiation", {"subject": tin, "market": BM_nt, "volumes": VOL}),
        ("report_pitch", "POST", "/api/report/pitch", {"subject": tin, "market": BM_nt, "volumes": VOL}),
        ("schedule_fee", "POST", "/api/schedule/fee", {"subject": tin, "market": RC, "visit_market": True, "preset": "ortho"}),
        ("schedule_fee_nodisc", "POST", "/api/schedule/fee", {"subject": tin, "market": {k: v for k, v in RC.items() if k != "discipline"}}),
        ("schedule_fee_csv", "POST", "/api/schedule/fee.csv", {"subject": tin, "market": RC, "visit_market": True, "preset": "ortho"}),
        ("schedule_visit", "POST", "/api/schedule/visit", {"subject": tin, "market": RC, "preset": "ortho"}),
        ("visit_market", "POST", "/api/schedule/visit/market", {"market": {"month": "latest", "state": st, "discipline": "PT", "payers": []}, "preset": "ortho", "subject": tin}),
        ("visit_market_csv", "POST", "/api/schedule/visit/market.csv", {"market": {"month": "latest", "state": st, "discipline": "PT", "payers": []}, "preset": "ortho", "subject": tin}),
        ("report_visitvalue", "POST", "/api/report/visitvalue", {"market": {"month": "latest", "state": st, "discipline": "PT", "payers": []}, "preset": "ortho", "subject": tin}),
        ("report_ratecard", "POST", "/api/report/ratecard", {"subject": tin, "market": RC, "visit_market": True, "preset": "ortho"}),
        ("leads", "POST", "/api/leads", {"market": {"month": "latest", "payers": [], "therapy_only": False, "state": st}, "threshold_percentile": 25, "min_codes": 3}),
        ("leads_csv", "POST", "/api/leads.csv", {"market": {"month": "latest", "payers": [], "therapy_only": False, "state": st}, "threshold_percentile": 25, "min_codes": 3}),
        ("leaderboard", "POST", "/api/leaderboard", {"market": {"month": "latest", "payers": [], "therapy_only": False, "state": st}, "min_codes": 3, "sort": "median", "limit": 50}),
        ("roster", "POST", "/api/roster", {"payer": payer, "market": {"month": "latest", "state": st}, "min_codes": 1, "sort": "name", "limit": 500}),
        ("outreach_csv", "GET", f"/api/export/outreach.csv?grain=tin&latest=1&dollar_only=1&state={st}", None),
        ("outreach_csv_entity", "GET", f"/api/export/outreach.csv?grain=entity&latest=1&dollar_only=1&state={st}", None),
        ("dossier_json", "POST", "/api/report/dossier.json", {"subject": tin, "radius_miles": 25, "market": {"month": "latest", "therapy_only": False, "state": st}}),
        ("dossier", "POST", "/api/report/dossier", {"subject": tin, "radius_miles": 25, "market": {"month": "latest", "therapy_only": False, "state": st}}),
        ("report_market", "POST", "/api/report/market", {"state": st, "code": "97110", "zip": None, "market": {"month": "latest", "therapy_only": False}}),
        ("leverage_summary", "POST", "/api/leverage/summary", {"subject": tin, "payer": payer, "radius_miles": 15, "market": NG}),
        ("leverage", "POST", "/api/leverage", {"subject": tin, "payer": payer, "radius_miles": 15, "market": NG}),
        ("economics_visit", "POST", "/api/economics/visit", {"subject": tin, "units_per_visit": 4, "market": NG}),
        ("clients", "GET", "/api/clients", None),
        ("clients_digest", "GET", "/api/clients/digest", None),
        ("new_to_network", "POST", "/api/contracts/new-to-network", {"market": {"month": "latest"}, "zip": None, "radius_miles": None, "therapy_only": False}),
        ("quality_freshness", "GET", "/api/quality/freshness", None),
        ("quality_posture", "POST", "/api/quality/posture", {"market": {"month": "latest"}}),
        ("quality_types", "POST", "/api/quality/types", {"market": {"month": "latest"}}),
        ("export_csv_97110", "GET", "/api/export.csv?grain=tin&latest=1&dollar_only=1&cpt=97110", None),
        ("stats", "GET", "/api/stats", None),
    ]
    if name:
        c += [
            ("benchmark_name", "POST", "/api/benchmark/market", {"subject": name, "market": {**BM_nt, "state": st2}, "volumes": {}}),
            ("schedule_fee_name", "POST", "/api/schedule/fee", {"subject": name, "market": {**RC, "state": st2}}),
            ("rates_entity_name", "GET", f"/api/rates?grain=entity&latest=1&dollar_only=1&q={urllib.parse.quote(name)}&page=1&page_size=1000", None),
            ("entity_entity_name", "GET", f"/api/entity/entity/{urllib.parse.quote(name + ' · ' + st2)}", None),
        ]
    proc = subprocess.Popen([PY, "-m", "mrfx", "--config", cfgp, "serve"], stdout=open(out / "serve.log", "w"),
                            stderr=subprocess.STDOUT, cwd="/home/user/Claudevideo")
    try:
        for _ in range(600):
            try:
                urllib.request.urlopen(base + "/api/boot", timeout=3).read()
                break
            except Exception:
                time.sleep(0.5)
        # a second call of each for determinism (bytes compared by the checker)
        timings = {}
        for n, m, u, b in c:
            s1, b1, t1 = call(base, m, u, b)
            s2, b2, t2 = call(base, m, u, b)
            ext = "json" if b1[:1] in (b"{", b"[") else ("html" if b1[:1] == b"<" else "txt")
            (out / f"{n}.{ext}").write_bytes(b1)
            (out / f"{n}.2.{ext}").write_bytes(b2)
            timings[n] = {"status": s1, "s1": round(t1, 3), "s2": round(t2, 3), "bytes": len(b1), "url": u, "body": b,
                          "same": b1 == b2}
            print(f"{n:28s} {s1} {t1:6.2f}s {t2:6.2f}s {len(b1):>8} same={b1 == b2}", flush=True)
        (out / "_timings.json").write_text(json.dumps(timings, indent=1))
    finally:
        proc.terminate()
        proc.wait(60)


if __name__ == "__main__":
    import urllib.parse  # noqa: E402
    main()
