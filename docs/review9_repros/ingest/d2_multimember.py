"""Multi-member gzip: the tail's CRC covers only the LAST member. Two files:
same size, same first 1 MB, same last 1 MB, same 8 sampled windows, but one
rate differs in a middle member. A is loaded; B arrives under another link."""
import gzip, json, sys, zlib, hashlib
from dl import *
import mrfx.fetch as F


def member(obj_bytes):
    return gzip.compress(obj_bytes, mtime=0)


def build():
    # one JSON document split over several gzip members (concatenated gzip is
    # valid gzip: `cat a.gz b.gz`; pigz -i / bgzip / chunked writers produce it)
    import random
    rnd = random.Random(7)
    pad1 = base64.b64encode(rnd.randbytes(1_200_000))
    pad2 = base64.b64encode(rnd.randbytes(1_400_000))
    d = json.loads(doc(rate_mid=40.0, n=4000, pad_mb=0))
    target = d["in_network"][0]
    filler = lambda code, pad: {"negotiation_arrangement": "ffs", "billing_code_type": "CPT",
                                "billing_code": code, "description": pad.decode(), "negotiated_rates": []}
    d["in_network"] = [filler("99991", pad1), target, filler("99992", pad2)]
    body = json.dumps(d).encode()
    lo = body.index(b'"billing_code": "97110"'); hi = body.index(b'"billing_code": "99992"')
    cut = (lo + hi) // 2
    head, mid, tail = body[:cut - 30000], body[cut - 30000:cut + 30000], body[cut + 30000:]
    ga = gzip.compress(mid, mtime=0)
    # change ONE rate inside the middle member; try values until the member
    # compresses to the same length (so the file size is unchanged)
    idxs = [i for i in range(len(mid)) if mid.startswith(b'"negotiated_rate": 40.0', i)]
    for k in idxs:
        for val in (b"47.0", b"41.0", b"44.0", b"48.0", b"49.0", b"30.0", b"60.0", b"45.0", b"42.0", b"43.0", b"46.0", b"50.0"):
            mid2 = mid[:k] + b'"negotiated_rate": ' + val + mid[k + 23:]
            gb = gzip.compress(mid2, mtime=0)
            if len(gb) == len(ga) and gb != ga:
                parts = lambda m: [head[:1_500_000], head[1_500_000:], m, tail[:-1_500_000], tail[-1_500_000:]]
                pa, pb = parts(mid), parts(mid2)
                return float(val), [member(x) for x in pa], [member(x) for x in pb]
    raise SystemExit("no equal-length variant found")


if __name__ == "__main__":
    val, ma, mb = build()
    print("B's one different rate:", val, "| middle member compressed sizes:", len(ma[2]), len(mb[2]))
    A, B = b"".join(ma), b"".join(mb)
    assert len(A) == len(B) and A != B
    assert gzip.decompress(A) != gzip.decompress(B)
    json.loads(gzip.decompress(A)); json.loads(gzip.decompress(B))
    diff = [i for i in range(len(A)) if A[i] != B[i]]
    print(f"file size {len(A)} bytes; differing bytes: {len(diff)} at offsets {diff[0]}..{diff[-1]}")
    print("window offsets sampled:", F._win_offsets(len(A)))
    covered = any(o <= d < o + F._WIN for d in diff for o in F._win_offsets(len(A)))
    print("a sampled window covers a differing byte:", covered, "| in first 1 MB:", diff[0] < (1 << 20),
          "| in last 1 MB:", diff[-1] >= len(A) - (1 << 20))
    cfg, store = mk_cfg("d2")
    cfg.early_dedup_mb = 1
    srv = Srv({"/a/rates.json.gz": A, "/b/rates.json.gz": B})
    try:
        ua, ub = srv.base + "/a/rates.json.gz", srv.base + "/b/rates.json.gz"
        F.add_urls(store, [ua]); F.run_queue(cfg, store, drain=True)
        print("A:", row(store, ua)["status"], "rows", row(store, ua)["rows_emitted"])
        F.add_urls(store, [ub]); F.run_queue(cfg, store, drain=True)
        rb = row(store, ub)
        print("B:", rb["status"], rb["kind"], "|", (rb["error"] or "")[:230])
        print(f"bytes sent for B: {srv.sent.get('/b/rates.json.gz', 0)} of {len(B)}")
        print("rows at B's rate in the store (B's distinguishing rate):",
              q(store, "SELECT count(*) FROM rates WHERE negotiated_rate = ?", val)[0][0], "(expected 1)")
    finally:
        srv.close()
