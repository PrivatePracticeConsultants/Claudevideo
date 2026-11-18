"""Dump the same facts from every surface for one subject (consistency probe)."""
import csv, io, json, re, warnings
warnings.filterwarnings("ignore")

def J(c, url, body):
    r = c.post(url, json=body)
    try:
        return r.status_code, r.json()
    except Exception:
        return r.status_code, r.text

def surfaces(c, subject, market, payer=None, show=print, display=None):
    display = display or subject
    S = {"subject": subject, "market": market}
    out = {}
    code, d = J(c, "/api/benchmark/market", S)
    if code != 200:
        show("benchmark", code, d); return out
    out["bench"] = {r["billing_code"]: (r["subject_rate"], r["subject_percentile"], r["n_peers"], r["p50"]) for r in d["rows"]}
    show("benchmark tins", d["subject_tins"], "rows(code: rate, pctl, peers, p50)", out["bench"])
    code, d = J(c, "/api/schedule/fee", S)
    if code == 200:
        show("fee keys", list(d.keys()))
        out["fee"] = d
    else:
        show("fee", code, d)
    code, d = J(c, "/api/leads", {"market": market, "threshold_percentile": 99, "min_codes": 1, "limit": 500})
    out["leads"] = [x for x in d.get("leads", []) if x["display_name"] == display]
    for x in out["leads"]:
        show("leads", {k: x.get(k) for k in ("display_name", "median_percentile", "n_codes", "worst_payer", "worst_payer_rate", "avg_gap_to_median", "worst_payer_codes")})
    code, d = J(c, "/api/leaderboard", {"market": market, "min_codes": 1, "limit": 500})
    for x in d.get("practices", []):
        if x["display_name"] == display:
            show("leaderboard", {k: x.get(k) for k in ("median_percentile", "n_codes", "median_rate", "rank")})
    for p in ([payer] if payer else []):
        code, d = J(c, "/api/roster", {"market": market, "payer": p, "min_codes": 1, "limit": 500})
        for x in d.get("organizations", []) if code == 200 else []:
            if x["display_name"] == display:
                show("roster", p, {k: x.get(k) for k in ("median_percentile", "n_codes", "median_rate")})
        code, d = J(c, "/api/negotiate/compare", {**S, "payer": p})
        if code == 200:
            show("compare", p, d["summary"], {r["billing_code"]: (r["subject_rate"], r["subject_percentile"], r["n_peers"], r.get("other_payers_rate")) for r in d["rows"]})
        else:
            show("compare", code, d)
    h = c.post("/api/report/negotiation", json=S).text
    show("one-pager", re.findall(r"<h2[^>]*>([^<]+)</h2>|You sit at roughly <b>(p\d+)</b>", h)[:12])
    code, d = J(c, "/api/report/dossier.json", S)
    show("dossier", code, d.get("facts") if code == 200 else d, d.get("skipped") if code == 200 else "")
    code, d = J(c, "/api/quality/size", S)
    show("size", code, d.get("subject") if code == 200 else d)
    code, d = J(c, "/api/economics/visit", S)
    show("visit", code, json.dumps(d)[:700])
    code, d = J(c, "/api/economics/weighted-position", {**S, "volumes": {"97110": 100, "97140": 50}})
    show("weighted", code, json.dumps(d)[:700])
    code, d = J(c, "/api/contract-gaps", S)
    show("gaps", code, json.dumps(d)[:500])
    code, d = J(c, "/api/quality/subject-types", S)
    show("subject-types", code, json.dumps(d)[:300])
    return out
