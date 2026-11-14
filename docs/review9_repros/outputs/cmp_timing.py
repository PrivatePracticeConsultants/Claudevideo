import json, sys
O = "/tmp/mrfx_review9/outputs"
if __name__ == "__main__":
    old = {r["label"].replace("Payer03", "Payer00"): r for r in json.load(open(f"{O}/timing_old.json"))}
    for extra in sys.argv[1:]:
        for r in json.load(open(extra)):
            old[r["label"]] = r
    new = json.load(open(f"{O}/timing_new.json"))
    pre = {r["label"].replace("Payer03", "Payer00"): r for r in json.load(open(f"{O}/timing_new_prerun.json"))}
    print("| endpoint | old first | old second | new first | new second | new (earlier pass) | flag |")
    print("|---|---|---|---|---|---|---|")
    for r in new:
        o = old.get(r["label"]) or old.get(r["label"].replace(" Payer00", ""))
        p = pre.get(r["label"])
        flag = []
        if o and o["code"] == r["code"] == 200:
            for k in ("first", "second"):
                if r[k] > 1.25 * o[k] and r[k] - o[k] > 1:
                    flag.append(f"SLOWER {k}")
        if r["code"] == 200 and max(r["first"], r["second"]) > 10:
            flag.append(">10s")
        if r["code"] != 200:
            flag.append(f"HTTP {r['code']}")
        if o and o["code"] != r["code"]:
            flag.append(f"old HTTP {o['code']}")
        if r.get("h1") != r.get("h2"):
            flag.append("body1!=body2")
        os_ = f"{o['first']:.2f} | {o['second']:.2f}" if o and o["code"] == 200 else (f"({o['code']}) | " if o else "– | –")
        ps = f"{p['first']:.2f}/{p['second']:.2f}" if p and p["code"] == 200 else "–"
        print(f"| {r['label']} | {os_} | {r['first']:.2f} | {r['second']:.2f} | {ps} | {' '.join(flag)} |")
