"""The 'every search-path request, cold and warm, both stores' table from bodies_*_base_r1/r2
(+ the proto2 column and the byte-identity check). usage: req_table.py"""
import json
from pathlib import Path

OUT = Path("/tmp/mrfx_review10/search/out")


def rd(label):
    p = OUT / f"bodies_{label}.json"
    return json.loads(p.read_text()) if p.exists() else None


def same(a, b, i):
    pa, pb = OUT / f"bodies_{a}" / f"{i:02d}.body", OUT / f"bodies_{b}" / f"{i:02d}.body"
    return pa.exists() and pb.exists() and pa.read_bytes() == pb.read_bytes()


if __name__ == "__main__":
    for store in ("real", "scale"):
        r1, r2, p2 = rd(f"{store}_base_r1"), rd(f"{store}_base_r2"), rd(f"{store}_proto2")
        if not r1:
            continue
        print(f"\n### {store} store — seconds; 'first' = first time that URL is asked after a server start, "
              f"'again' = asked again at once\n")
        print("| request | base first (run 1 / run 2) | base again (run 1 / run 2) | prototype first / again | body identical to base | load (run 1) |")
        print("|---|---|---|---|---|---|")
        n_same = n = 0
        for k, x in enumerate(r1):
            y = r2[k] if r2 else None
            z = p2[k] if p2 else None
            ident = ""
            if z:
                ok = same(f"{store}_base_r1", f"{store}_proto2", x["i"]) and x["status"] == z["status"]
                ident = "yes" if ok else "NO"
                n += 1; n_same += ok
            base_stable = "" if not y or same(f"{store}_base_r1", f"{store}_base_r2", x["i"]) else " (base r1≠r2!)"
            print(f"| {x['title'].replace(' | ', ' — ')}{' [' + str(x['status']) + ']' if x['status'] != 200 else ''} | {x['first_s']:.2f} / {y['first_s'] if y else float('nan'):.2f} | "
                  f"{x['second_s']:.2f} / {y['second_s'] if y else float('nan'):.2f} | "
                  f"{(str(format(z['first_s'], '.2f')) + ' / ' + format(z['second_s'], '.2f')) if z else '–'} | {ident}{base_stable} | {x['load'].split(',')[0]} |")
        if n:
            print(f"\n{n_same} of {n} response bodies byte-identical between base and prototype ({store} store).")
