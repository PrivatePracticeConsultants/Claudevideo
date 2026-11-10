"""Half-cent medians: the server rounds with rounding.half_up (DuckDB's rule), the Explorer 'By code'
table / Code page / drawer get the UNROUNDED number and the browser rounds it (toLocaleString).
Find real cells where the two rules print different cents (32M copy on 8710)."""
import json, sys, urllib.request, urllib.parse
from decimal import Decimal, ROUND_HALF_UP
sys.path.insert(0, "/home/user/Claudevideo")
from mrfx.rounding import half_up
B = "http://localhost:8710"
def get(u): return json.load(urllib.request.urlopen(B + u))
def post(u, b):
    r = urllib.request.Request(B + u, data=json.dumps(b).encode(), headers={"content-type": "application/json"})
    return json.load(urllib.request.urlopen(r))
def js(x):   # Chrome's Number.toLocaleString(…, 2 dp): half-up on the shortest decimal
    return float(Decimal(repr(float(x))).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))

def main():
    hits, cells = [], 0
    for st in ["", "AL", "WA", "ND", "WY", "OR", "MA", "AK", "TX", "ID", "MT", "NY"]:
        s = get("/api/summary?grain=tin&dollar_only=1" + (f"&state={st}" if st else ""))
        for c in s["by_code"]:
            for k in ("median", "p25", "p75"):
                v = c.get(k)
                if v is None: continue
                cells += 1
                if js(v) != half_up(v, 2):
                    hits.append((st or "all", c["billing_code"], k, repr(v), js(v), half_up(v, 2)))
    print("cells checked", cells, "| browser cents != server cents:", len(hits))
    for h in hits[:12]: print("  state %s code %s %s raw %s -> browser $%.2f, server rule $%.2f" % h)
    # the same fact on a server-rounded surface
    for st, code, k, raw, a, b in [h for h in hits if h[2] == "median"][:5]:
        mk = post("/api/market/medicare", {"code": code, "market": {"month": "latest", **({"state": st} if st != "all" else {})}})
        print(f"  -> Markets prints market median for {code} {st}: ${mk['market_median']:.2f} ({mk['market_practices']} practices); Explorer By-code prints ${a:.2f}")

if __name__ == "__main__":
    main()
