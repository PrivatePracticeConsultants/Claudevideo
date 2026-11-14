"""Byte-for-byte diff of two snapshot dirs (snapshot.py), plus their timings.

usage: python diff_snap.py <before-dir> <after-dir> [--timings]
"""
import difflib
import json
import re
import sys
from pathlib import Path


def by_name(d: Path) -> dict:
    return {re.sub(r"^\d+_", "", p.name): p for p in d.iterdir() if not p.name.startswith("_")}


if __name__ == "__main__":
    a, b = Path(sys.argv[1]), Path(sys.argv[2])
    A, B = by_name(a), by_name(b)
    same = diff = 0
    for n in sorted(set(A) | set(B)):
        if n not in A or n not in B:
            print(f"MISSING {n}: before={n in A} after={n in B}")
            diff += 1
            continue
        x, y = A[n].read_bytes(), B[n].read_bytes()
        # export file names carry the download's clock time (..._1601.csv)
        x, y = (re.sub(rb"(\d{4}-\d\d-\d\d)_\d{4}", rb"\1_HHMM", v) for v in (x, y))
        x, y = (re.sub(rb"MRF Explorer v\d+\.\d+\.\d+", rb"MRF Explorer vX", v) for v in (x, y))
        if x == y:
            same += 1
            continue
        diff += 1
        print(f"DIFF {n} ({len(x)} vs {len(y)} bytes)")
        xl = x.decode("utf-8", "replace").splitlines()
        yl = y.decode("utf-8", "replace").splitlines()
        for i, line in enumerate(difflib.unified_diff(xl, yl, lineterm="", n=1)):
            if i > 24:
                print("   ...")
                break
            print("   " + line[:200])
    print(f"\n{same} identical, {diff} different")
    if "--timings" in sys.argv:
        ta = json.loads((a / "_timings.json").read_text())
        tb = json.loads((b / "_timings.json").read_text())
        print(f"\n{'endpoint':40s} {'before cold':>11s} {'after cold':>10s} {'before warm':>11s} {'after warm':>10s}")
        for k in ta:
            if k in tb:
                print(f"{k:40s} {ta[k]['cold']:11.2f} {tb[k]['cold']:10.2f} "
                      f"{ta[k]['warm']:11.2f} {tb[k]['warm']:10.2f}")
    sys.exit(1 if diff else 0)
