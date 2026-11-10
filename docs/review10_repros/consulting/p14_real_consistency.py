"""One real practice's overall position on every screen (32M store copy, served on 8720)."""
import json, urllib.request, sys
B = "http://localhost:8720"
def P(url, body):
    r = urllib.request.Request(B + url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    try: return json.load(urllib.request.urlopen(r, timeout=300))
    except urllib.error.HTTPError as e: return {"ERR": e.read().decode()[:200]}
if __name__ == "__main__":
    PAY = "Blue Cross Blue Shield of North Dakota"; M = {"month": "latest", "state": "ND"}
    lb = P("/api/leaderboard", {"market": M, "min_codes": 1, "limit": 1000, "sort": "size"})["practices"]
    ro = {x["tin_value"]: x for x in P("/api/roster", {"market": M, "payer": PAY, "min_codes": 1, "limit": 2000})["organizations"]}
    ld = {x["tin_value"]: x for x in P("/api/leads", {"market": M, "threshold_percentile": 99, "min_codes": 1, "limit": 1000})["leads"]}
    n = bad = 0
    for x in lb[:25]:
        t = x["tin_value"]
        if "MASK" in t: continue
        b = P("/api/benchmark/market", {"subject": t, "market": M})
        rows = [r for r in b.get("rows", []) if r.get("subject_percentile") is not None]
        import statistics
        ranked = [r["subject_percentile_exact"] for r in rows if r["in_position"]]
        hand = (int(statistics.median(ranked) + 0.5) if ranked else 50) if rows else None
        d = P("/api/report/dossier.json", {"subject": t, "market": M})
        c = P("/api/negotiate/compare", {"subject": t, "payer": PAY, "market": M})
        s = P("/api/quality/size", {"subject": t, "market": M})
        got = {"leaderboard": x["median_percentile"], "leads": (ld.get(t) or {}).get("median_percentile"), "dossier": (d.get("facts") or {}).get("median_percentile"),
               "roster(payer)": (ro.get(t) or {}).get("median_percentile"), "compare(payer)": (c.get("summary") or {}).get("headline_percentile"),
               "size": (s.get("subject") or {}).get("avg_percentile")}
        n += 1
        ok = got["leaderboard"] == hand and got["dossier"] == hand and (got["leads"] in (None, hand)) and got["roster(payer)"] == got["compare(payer)"]
        bad += not ok
        if not ok or n <= 4: print(x["display_name"][:28], "hand(bench rows)", hand, got)
    print("practices checked", n, "inconsistent (excluding size)", bad)
