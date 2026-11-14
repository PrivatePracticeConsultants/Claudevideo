"""download_segments > 1 (a documented setting): does a copy of a loaded file
still stop after the first MB? (Segment floor lowered so a 2.7 MB file segments.)"""
from dl import *
import mrfx.fetch as F

if __name__ == "__main__":
    for segs in (1, 4):
        cfg, store = mk_cfg(f"d7_{segs}")
        cfg.early_dedup_mb = 1
        cfg.download_segments = segs
        F._SEGMENT_MIN_BYTES = 1 << 20; F._SEGMENT_MIN_PIECE = 512 << 10
        A = gz(doc(n=300, pad_mb=3.5))
        srv = Srv({"/a/r.json.gz": A, "/b/r.json.gz": A})
        try:
            ua, ub = srv.base + "/a/r.json.gz", srv.base + "/b/r.json.gz"
            F.add_urls(store, [ua]); F.run_queue(cfg, store, drain=True)
            sent0 = srv.sent.get("/b/r.json.gz", 0)
            F.add_urls(store, [ub]); F.run_queue(cfg, store, drain=True)
            rb = row(store, ub)
            body_gets = [(r, s) for _, m, r, s, _ in srv.hits("/b/r.json.gz")]
            print(f"download_segments={segs}: A {row(store, ua)['status']} | B {rb['status']}/{rb['kind']} "
                  f"early={'same first' in (rb['error'] or '')} | B requests {len(body_gets)}: {body_gets[:6]}")
            print("    B error:", (rb["error"] or "")[:110])
        finally:
            srv.close()
