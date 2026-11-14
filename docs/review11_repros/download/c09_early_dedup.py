"""Case 9 — early duplicate check:
 A) CONSTRUCT two single-member gzips, same size, same first 1 MiB, same last
    1 MiB, same 8 windows, same gzip trailer (CRC-32 forced equal by solving a
    32-bit GF(2) system over 40 '@'/'A' positions in the middle) that differ
    in a RATE in the middle. Load A, then B: is B skipped as "identical"?
 B) COST of the check per download: requests and bytes with (i) no same-size
    candidate, (ii) a same-size candidate with a matching head; and the CPU
    time of the check itself (early_duplicate_check wrapped in this process).
"""
import gzip
import json
import random
import sys
import time
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _harness import Srv, mk_cfg, quiet_logs, sha, row, q  # noqa: E402

import mrfx.fetch as F  # noqa: E402
from mrfx.fetch import add_urls, file_fingerprint, run_queue  # noqa: E402


def grp(i, r):
    return {"provider_groups": [{"npi": [1000000000 + i], "tin": {"type": "ein", "value": f"43{i:07d}"}}],
            "negotiated_prices": [{"negotiated_type": "negotiated", "negotiated_rate": r,
                                   "billing_class": "professional", "service_code": ["11"],
                                   "billing_code_modifier": ["GP"]}]}


def item(code, rates):
    return {"negotiation_arrangement": "ffs", "billing_code_type": "CPT", "billing_code_type_version": "2026",
            "billing_code": code, "negotiated_rates": rates}


def payload(rate_b: float, knob: str) -> bytes:
    """~4.4 MB JSON: 97110 early, 1.6 MB filler, 97140 (rate_b) + a 40-char
    knob string, then 2.5 MB filler. `knob` is where the CRC is forced."""
    rnd = random.Random(7)
    fill1 = "".join(rnd.choice("abcdefghijklmnopqrstuvwxyz0123456789") for _ in range(1_600_000))
    rnd2 = random.Random(8)
    fill2 = "".join(rnd2.choice("abcdefghijklmnopqrstuvwxyz0123456789") for _ in range(2_500_000))
    d = {"reporting_entity_name": "Collide Health", "reporting_entity_type": "issuer",
         "last_updated_on": "2026-06-01", "version": "2.0.0",
         "in_network": [item("97110", [grp(i, 40.0) for i in range(10)]),
                        {**item("99991", []), "description": fill1},
                        item("97140", [grp(i, rate_b) for i in range(10)]),
                        {**item("99992", []), "description": knob + fill2}]}
    return json.dumps(d).encode()


def force_crc(target_bytes: bytes, variant: bytes, knob_off: int, npos: int = 40) -> bytes:
    """Return `variant` with its 40 knob bytes set to '@'/'A' so that
    crc32(result) == crc32(target_bytes)."""
    base = bytearray(variant)
    for i in range(npos):
        base[knob_off + i] = ord("@")
    base = bytes(base)
    c0 = zlib.crc32(base)
    goal = zlib.crc32(target_bytes) ^ c0
    deltas = []
    for i in range(npos):
        b = bytearray(base)
        b[knob_off + i] = ord("A")
        deltas.append(zlib.crc32(bytes(b)) ^ c0)
    # Gaussian elimination over GF(2): find subset of deltas xor-ing to goal
    rows = [(d, 1 << i) for i, d in enumerate(deltas)]
    pivots = {}
    for d, m in rows:
        for bit in range(31, -1, -1):
            if not (d >> bit) & 1:
                continue
            if bit in pivots:
                pd, pm = pivots[bit]
                d ^= pd; m ^= pm
            else:
                pivots[bit] = (d, m)
                break
    x, mask = goal, 0
    for bit in range(31, -1, -1):
        if (x >> bit) & 1:
            if bit not in pivots:
                raise RuntimeError("deltas do not span: try more positions")
            pd, pm = pivots[bit]
            x ^= pd; mask ^= pm
    out = bytearray(base)
    for i in range(npos):
        if (mask >> i) & 1:
            out[knob_off + i] = ord("A")
    out = bytes(out)
    assert zlib.crc32(out) == zlib.crc32(target_bytes)
    return out


if __name__ == "__main__":
    quiet_logs(level=60)
    knob = "@" * 40
    pa = payload(40.0, knob)
    pb_raw = payload(47.0, knob)
    assert len(pa) == len(pb_raw)
    knob_off = pb_raw.index(b'"description": "@@@@') + len(b'"description": "')
    pb = force_crc(pa, pb_raw, knob_off)
    assert pb != pa and len(pb) == len(pa)
    A = gzip.compress(pa, compresslevel=0, mtime=0)
    B = gzip.compress(pb, compresslevel=0, mtime=0)
    print(f"[construct] uncompressed {len(pa)} B, gz {len(A)} B each; crc32 equal {zlib.crc32(pa) == zlib.crc32(pb)}; "
          f"gz bytes identical {A == B}; A/B trailers identical {A[-8:] == B[-8:]}; "
          f"first byte that differs at {next(i for i in range(len(A)) if A[i] != B[i]) / 1e6:.2f} MB")
    import tempfile
    with tempfile.TemporaryDirectory(dir="/tmp/mrfx_review11/download") as td:
        pa_ = Path(td) / "a.gz"; pb_ = Path(td) / "b.gz"
        pa_.write_bytes(A); pb_.write_bytes(B)
        fa, fb = file_fingerprint(pa_, 1), file_fingerprint(pb_, 1)
        same = {k: fa[k] == fb[k] for k in fa}
        print(f"    fingerprint(head 1 MiB) fields equal: {same}")
        # also at the production head of 15 MiB (whole file is the head then -> head_sha differs)
        fa15, fb15 = file_fingerprint(pa_, 15), file_fingerprint(pb_, 15)
        print(f"    at early_dedup_mb=15 (file < 15 MiB, head = whole file): head_sha equal {fa15['head_sha'] == fb15['head_sha']}")

    cfg, store = mk_cfg("c09_collision", early_dedup_mb=1)
    srv = Srv({"/a/r.json.gz": A, "/b/r.json.gz": B}, {})
    ua, ub = srv.base + "/a/r.json.gz", srv.base + "/b/r.json.gz"
    try:
        add_urls(store, [ua]); run_queue(cfg, store, drain=True)
        add_urls(store, [ub]); run_queue(cfg, store, drain=True)
    finally:
        srv.close()
    ra, rb = row(store, ua), row(store, ub)
    print(f"[collision via queue] A {ra['status']} rows {ra['rows_emitted']} | B {rb['status']}/{rb['kind']} "
          f"err={(rb['error'] or '')[:100]!r}")
    print(f"    B requests: {srv.describe('/b/r.json.gz')} (bytes sent {srv.sent_bytes('/b/r.json.gz')} of {len(B)})")
    print(f"    rates for 97140 in store: {q(store, 'SELECT negotiated_rate, count(*) FROM rates WHERE billing_code = %s GROUP BY 1' % repr('97140'))} "
          f"(A says 40.0, B says 47.0 — B's 47.0 is lost if B was skipped)")
    import shutil; shutil.rmtree(cfg.store_dir.parent, ignore_errors=True)

    # ---- B) cost of the check, measured on the collision run above and on a
    # download with a same-size candidate whose head differs ----
    timings = []
    real_edc = F.early_duplicate_check

    def timed_edc(*a, **k):
        chk = real_edc(*a, **k)

        def wrapped(*aa, **kk):
            t0 = time.perf_counter()
            try:
                return chk(*aa, **kk)
            finally:
                timings.append(time.perf_counter() - t0)
        wrapped.has_twin = chk.has_twin
        return wrapped
    F.early_duplicate_check = timed_edc
    C = gzip.compress(payload(55.0, "C" * 40), compresslevel=0, mtime=0)   # same size, head differs? no: head same
    # a same-size file whose FIRST MiB differs: change the early 97110 rates
    pc = payload(40.0, "C" * 40).replace(b'"negotiated_rate": 40.0', b'"negotiated_rate": 41.0', 3)
    C = gzip.compress(pc, compresslevel=0, mtime=0)
    assert len(C) == len(A)
    cfg, store = mk_cfg("c09_cost", early_dedup_mb=1)
    srv = Srv({"/a/r.json.gz": A, "/c/r.json.gz": C, "/d/r.json.gz": A}, {"/d/r.json.gz": {"delay": 0.02}})
    ua, uc, ud = (srv.base + f"/{p}/r.json.gz" for p in "acd")
    try:
        add_urls(store, [ua]); run_queue(cfg, store, drain=True)
        timings.clear()
        add_urls(store, [uc]); run_queue(cfg, store, drain=True)
        rc = row(store, uc)
        print(f"[cost: same-size candidate, head differs] c {rc['status']} | requests {srv.describe('/c/r.json.gz')} | "
              f"check calls {len(timings)} took {sum(timings) * 1e3:.1f} ms")
        timings.clear()
        add_urls(store, [ud]); run_queue(cfg, store, drain=True)     # true copy, served at ~3 MB/s
        rd = row(store, ud)
        print(f"[cost: true copy, throttled server] d {rd['status']}/{rd['kind']} | requests {len(srv.hits('/d/r.json.gz'))}: "
              f"{srv.describe('/d/r.json.gz')[:3]}... | bytes sent {srv.sent_bytes('/d/r.json.gz')} of {len(A)} | "
              f"check calls {len(timings)} took {sum(timings) * 1e3:.1f} ms")
    finally:
        srv.close()
        F.early_duplicate_check = real_edc
        import shutil; shutil.rmtree(cfg.store_dir.parent, ignore_errors=True)
