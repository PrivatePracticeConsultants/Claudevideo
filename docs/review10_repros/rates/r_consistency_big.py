"""One fact, every surface (32M copy served on 8710): a code's market median and practice count on
Overview, Explorer 'By code' (all months / latest / each month), Code page (sum over payers n/a),
Markets %-of-Medicare block and geography's national line."""
import json, urllib.request, urllib.parse
B = "http://localhost:8710"
def get(u): return json.load(urllib.request.urlopen(B + u))
def post(u, b):
    r = urllib.request.Request(B + u, data=json.dumps(b).encode(), headers={"content-type": "application/json"})
    return json.load(urllib.request.urlopen(r))

def main():
    ov = {c["billing_code"]: c for c in get("/api/overview")["top_codes"]}
    months = get("/api/months")["months"]
    views = {"all months": "", "latest(api only)": "&latest=1", **{m: "&month=" + m for m in months}}
    by = {k: {c["billing_code"]: c for c in get("/api/summary?grain=tin&dollar_only=1" + v)["by_code"]} for k, v in views.items()}
    print("code   | Overview        | Markets (medicare block) | Markets geography 'national' | Explorer By code: " + " | ".join(views))
    bad = 0
    for code in list(ov)[:15]:
        mk = post("/api/market/medicare", {"code": code, "market": {"month": "latest"}})
        geo = post("/api/market/geography", {"code": code, "market": {"month": "latest"}})
        cells = []
        for k in views:
            c = by[k].get(code)
            cells.append(f"{c['median']:.2f}/{c['entities']}" if c else "-")
        same = cells[0].split("/")[0] == f"{ov[code]['median']:.2f}"
        bad += not same
        print(f"{code}  | {ov[code]['median']:.2f}/{ov[code]['practices']} | {mk['market_median']:.2f}/{mk['market_practices']} | {geo['national_median']:.2f}/{geo['national_practices']} | " + " | ".join(cells) + ("" if same else "   <-- Explorer default differs"))
    print("codes whose Explorer default ('all months') By-code median differs from Overview:", bad, "of 15")

if __name__ == "__main__":
    main()
