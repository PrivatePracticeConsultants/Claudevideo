"""A/B two bodies_<label> runs: byte-identical bodies? and the timings side by side.
usage: ab.py <labelA> <labelB>"""
import json
import sys
from pathlib import Path

OUT = Path("/tmp/mrfx_review10/search/out")

if __name__ == "__main__":
    a, b = sys.argv[1:3]
    ra, rb = json.loads((OUT / f"bodies_{a}.json").read_text()), json.loads((OUT / f"bodies_{b}.json").read_text())
    same = 0
    print(f"| request | {a} first / second (s) | {b} first / second (s) | bytes | byte-identical |")
    print("|---|---|---|---|---|")
    for x, y in zip(ra, rb):
        ba = (OUT / f"bodies_{a}" / f"{x['i']:02d}.body").read_bytes()
        bb = (OUT / f"bodies_{b}" / f"{y['i']:02d}.body").read_bytes()
        ok = ba == bb and x["status"] == y["status"]
        same += ok
        print(f"| {x['title']} | {x['first_s']:.2f} / {x['second_s']:.2f} | {y['first_s']:.2f} / {y['second_s']:.2f} | {x['bytes']} | {'yes' if ok else 'NO'} |")
    print(f"\n{same} of {len(ra)} bodies byte-identical")
