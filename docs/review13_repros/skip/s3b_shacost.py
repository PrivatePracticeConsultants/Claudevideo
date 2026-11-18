"""S3b: what a sha256 over the INFLATED stream costs, measured cleanly:
each variant run twice (page cache warm after the first pass), best of two.
Also sha256 throughput on in-memory bytes (the pure hashing cost)."""
import gzip
import hashlib
import os
import time
from pathlib import Path

REAL = Path("/tmp/claude-0/-home-user-Claudevideo/4633d594-0139-5f8e-a935-ae9c76b72000/scratchpad/real")
FILES = ["uhc_2026-11-01_UnitedHealthcare-Insurance-Company-of-New-York_Third-Party-Administrator_Empire_CSP-377-A351_in-network-rates.json.gz",
         "wy_2026-11_710_26B0_in-network-rates.json.gz",
         "uhc_2026-11-01_UnitedHealthcare-Insurance-Company-of-the-River-Valley_Insurer_Heritage-EPO_669_in-network-rates.json.gz"]


def inflate(p, with_sha):
    hh = hashlib.sha256() if with_sha else None
    n = 0
    with gzip.open(p, "rb") as g:
        for blk in iter(lambda: g.read(4 << 20), b""):
            n += len(blk)
            if hh:
                hh.update(blk)
    return n


def main():
    buf = os.urandom(256 << 20)
    t0 = time.perf_counter(); hashlib.sha256(buf).hexdigest(); dt = time.perf_counter() - t0
    print(f"sha256 on 256 MB in memory: {dt:.3f}s = {256 / dt:,.0f} MB/s")
    for fn in FILES:
        p = REAL / fn
        if not p.exists():
            continue
        best = {}
        for label, ws in (("inflate", False), ("inflate+sha", True), ("inflate", False), ("inflate+sha", True)):
            t0 = time.perf_counter()
            n = inflate(p, ws)
            dt = time.perf_counter() - t0
            best[label] = min(best.get(label, 1e9), dt)
        extra = best["inflate+sha"] - best["inflate"]
        print(f"{fn[:48]}…: {p.stat().st_size / 1e6:,.0f} MB gz -> {n / 1e6:,.0f} MB; inflate {best['inflate']:.2f}s, "
              f"inflate+sha {best['inflate+sha']:.2f}s; extra {extra:+.2f}s = {extra / (n / 1e9):+.2f} s/GB inflated "
              f"({100 * extra / best['inflate']:+.0f}% of the inflate alone; the parse itself is far slower than the inflate)")


if __name__ == "__main__":
    main()
