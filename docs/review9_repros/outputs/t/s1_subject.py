"""One subject, many spellings, many surfaces: does 'your rate' for 97110 agree?"""
from fxm import *  # noqa
import json, re, sys, warnings
warnings.filterwarnings("ignore")


def short(v, n=300):
    s = json.dumps(v, default=str)
    return s if len(s) <= n else s[:n] + "…"


if __name__ == "__main__":
    cfg, store = build()
    c = client(cfg, store)
    MO = {"month": "latest", "state": "MO"}
    NAT = {"month": "latest", "allow_national": True}

    def bench(subj, m):
        r = c.post("/api/benchmark/market", json={"subject": subj, "market": m})
        if r.status_code != 200:
            return f"{r.status_code} {r.json().get('detail')[:90]}"
        d = r.json()
        return {"tins": d["subject_tins"],
                "rows": [(x["billing_code"], x["subject_rate"], x["p50"], x["subject_percentile"], x["n_peers"]) for x in d["rows"]]}

    def card(subj, m):
        r = c.post("/api/schedule/fee", json={"subject": subj, "market": m})
        if r.status_code != 200:
            return f"{r.status_code} {r.json().get('detail')[:90]}"
        d = r.json()
        return [(x["billing_code"], {p: v["rate"] for p, v in x["rates"].items()}) for x in d["fee_schedule"]["codes"]]

    def neg(subj, m):
        r = c.post("/api/report/negotiation", json={"subject": subj, "market": m})
        if r.status_code != 200:
            return f"{r.status_code} {r.text[:160]}"
        h = r.text
        return re.findall(r"<h2[^>]*>([^<]*)</h2>", h)[:8]

    def gaps(subj, m):
        r = c.post("/api/contract-gaps", json={"subject": subj, "market": m, "min_peers": 1})
        return short(r.json(), 400)

    def size(subj, m):
        r = c.post("/api/quality/size", json={"subject": subj, "market": m})
        return short(r.json().get("subject") if r.status_code == 200 else r.text, 300)

    def visit(subj, m):
        r = c.post("/api/economics/visit", json={"subject": subj, "market": m})
        return short(r.json(), 300)

    for subj in ("ACME REHAB", "acme rehab", "ACME", "ACME REHAB · MO", "ACME REHAB · KS", S1, S2,
                 "61-0000001", "1" + S1, "BIG GROUP", "big group", "NORTH CLINIC", "TWO STATE PT", "ZZZ NOPE"):
        for mname, m in (("MO", MO), ("NAT", NAT), ("KS", {"month": "latest", "state": "KS"})):
            print(f"--- subject={subj!r} market={mname}")
            print("  bench ", bench(subj, m))
            print("  card  ", card(subj, m))
        print("  neg(MO)", neg(subj, MO))
    print("=== explorer entity grain, 97110, latest month 2026-06")
    r = c.get("/api/rates?grain=entity&dollar_only=1&cpt=97110&month=2026-06&page_size=200")
    for x in r.json()["rows"]:
        print("  ", x.get("display_name"), x.get("unit_id"), x.get("payer"), x.get("modifier_set"), x.get("billing_class"),
              x.get("negotiated_rate"), x.get("rate_min"), x.get("rate_max"), x.get("tin_count"))
