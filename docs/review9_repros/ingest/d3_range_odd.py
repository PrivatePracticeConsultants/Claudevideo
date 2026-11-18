"""A is loaded. B (identical gz) and B2 (same size + same first 1 MB, different
after) come from servers that mishandle the tail/sample Range requests."""
import gzip as _gz, time
from dl import *
import mrfx.fetch as F

def variant(name):
    cfg, store = mk_cfg("d3_" + name)
    cfg.early_dedup_mb = 1
    A = gz(doc(n=300, pad_mb=3.5))
    # B2: same size, same first ~1.2 MB, different later bytes (other pad tail + one rate)
    d2 = doc(n=300, pad_mb=3.5, rate_mid=47.0)
    B2 = gz(d2)
    srv = Srv({"/a/r.json.gz": A, "/b/r.json.gz": A, "/c/r.json.gz": B2})
    def odd(h, body, rng):
        if not rng or rng.endswith("-") and name != "ignore":
            return False
        a, b = rng[6:].split("-"); a = int(a); b = int(b) if b else len(body) - 1
        if name == "ignore":            # Range ignored: 200 + the whole body
            if not rng: return False
            srv.log.append((h.path, "GET", rng, 200, time.time()))
            h.send_response(200); h.send_header("Content-Length", str(len(body))); h.end_headers()
            h.wfile.write(body); return True
        if name == "wrong_cr":          # 206 but Content-Range describes another span
            srv.log.append((h.path, "GET", rng, 206, time.time()))
            piece = body[0:b - a + 1]
            h.send_response(206); h.send_header("Content-Range", f"bytes 0-{b-a}/{len(body)}")
            h.send_header("Content-Length", str(len(piece))); h.end_headers(); h.wfile.write(piece); return True
        if name == "gz_on_top":         # correct span, but Content-Encoding: gzip applied on top
            srv.log.append((h.path, "GET", rng, 206, time.time()))
            piece = _gz.compress(body[a:b + 1])
            h.send_response(206); h.send_header("Content-Range", f"bytes {a}-{b}/{len(body)}")
            h.send_header("Content-Encoding", "gzip")
            h.send_header("Content-Length", str(len(piece))); h.end_headers(); h.wfile.write(piece); return True
        if name == "wrong_total":       # 206, right span, total size off by one
            srv.log.append((h.path, "GET", rng, 206, time.time()))
            piece = body[a:b + 1]
            h.send_response(206); h.send_header("Content-Range", f"bytes {a}-{b}/{len(body)+1}")
            h.send_header("Content-Length", str(len(piece))); h.end_headers(); h.wfile.write(piece); return True
        return False
    try:
        ua, ub, uc = (srv.base + f"/{x}/r.json.gz" for x in "abc")
        F.add_urls(store, [ua]); F.run_queue(cfg, store, drain=True)
        srv.behave["/b/r.json.gz"] = odd; srv.behave["/c/r.json.gz"] = odd
        t0 = time.time()
        F.add_urls(store, [ub, uc]); F.run_queue(cfg, store, drain=True)
        rb, rc = row(store, ub), row(store, uc)
        print(f"[{name}] sizes equal: {len(A) == len(B2)}; drain {time.time()-t0:.1f} s | A {row(store, ua)['status']}"
              f" | B(copy) {rb['status']}/{rb['kind']} early={'same first' in (rb['error'] or '')}"
              f" | B2(different) {rc['status']}/{rc['kind']} rows {rc['rows_emitted']} | rows @47.0: "
              f"{q(store, 'SELECT count(*) FROM rates WHERE negotiated_rate = 47.0')[0][0]} (1 expected)"
              f" | GETs B {len(srv.hits('/b/r.json.gz'))} B2 {len(srv.hits('/c/r.json.gz'))}")
    finally:
        srv.close()

if __name__ == "__main__":
    for v in ("normal", "ignore", "wrong_cr", "gz_on_top", "wrong_total"):
        variant(v)
