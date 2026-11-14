"""S4: the content claim at ingest. Small files (below early_dedup_mb) so
every copy downloads in full and the sha decides.
 A. two links, same bytes, two hosts (two payers' indexes listing one TPA file)
 B. link + inbox, both orders
 C. the first loader FAILS mid-parse: does the deferred twin load afterwards?
 D. two links claimed concurrently (2 download + 2 processor threads)."""
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import gen  # noqa: E402
import h  # noqa: E402

BODY = gen.doc(n_items=200, filler=50, payer="Shared TPA")
GZ = gen.gzn(BODY)


def serve(body):
    def fn(hh, head, entry):
        rng = h.parse_range(hh, len(body))
        if rng:
            a, b = rng
            h.send(hh, entry, 206, {"Content-Range": f"bytes {a}-{b}/{len(body)}", "ETag": '"t"'}, body[a:b + 1], head=head)
        else:
            h.send(hh, entry, 200, {"ETag": '"t"'}, body, head=head)
    return fn


def report(store, title):
    rows = h.queue_rows(store, "id, status, kind, duplicate_of, substr(error,1,110) AS error, content_sha IS NOT NULL AS has_sha")
    with store.connect() as con:
        files = con.execute("SELECT filename, status, rows_emitted, substr(error,1,90) FROM files ORDER BY filename").fetchall()
        rates = con.execute("SELECT count(*) FROM rates").fetchone()[0]
        srcs = con.execute("SELECT count(DISTINCT source_file) FROM rates").fetchone()[0]
    print(f"\n== {title}")
    for r in rows:
        print("   url:", r)
    for f in files:
        print("   file:", f)
    print(f"   rates: {rates} from {srcs} source file(s) (one copy = 200)")


def setup(name, **over):
    cfg = h.scratch_cfg("s4_" + name, parallel_downloads=1, parallel_ingests=1, **over)
    store = h.open_store(cfg)
    h.ROUTES.clear(); h.LOG.clear()
    h.ROUTES["/tpa.json.gz"] = serve(GZ)
    httpd, base = h.start_server()
    port = base.rsplit(":", 1)[1]
    return cfg, store, httpd, port


def case_a():
    from mrfx import fetch
    cfg, store, httpd, port = setup("A_two_links")
    fetch.add_urls(store, [f"http://127.0.0.1:{port}/tpa.json.gz", f"http://localhost:{port}/tpa.json.gz"])
    h.drain(cfg, store, 60)
    report(store, "A. two links, same bytes, two hosts")
    httpd.shutdown()


def case_b():
    from mrfx import fetch
    from mrfx.ingest import scan_inbox
    cfg, store, httpd, port = setup("B1_link_then_inbox")
    fetch.add_urls(store, [f"http://127.0.0.1:{port}/tpa.json.gz"])
    h.drain(cfg, store, 60)
    (cfg.inbox_dir / "tpa_copy.json.gz").write_bytes(GZ)
    scan_inbox(cfg, store)
    report(store, "B1. link then inbox copy")
    httpd.shutdown()
    cfg, store, httpd, port = setup("B2_inbox_then_link")
    (cfg.inbox_dir / "tpa_copy.json.gz").write_bytes(GZ)
    scan_inbox(cfg, store)
    fetch.add_urls(store, [f"http://127.0.0.1:{port}/tpa.json.gz"])
    h.drain(cfg, store, 60)
    report(store, "B2. inbox then link")
    httpd.shutdown()


def case_c():
    """First loader fails: make the FIRST parse of this content raise."""
    import mrfx.ingest as ing
    from mrfx import fetch
    cfg, store, httpd, port = setup("C_first_fails")
    real = ing.run_in_network_parse
    state = {"calls": 0}

    def flaky(*a, **k):
        state["calls"] += 1
        if state["calls"] == 1:
            raise RuntimeError("injected parser failure on the first loader")
        return real(*a, **k)
    ing.run_in_network_parse = flaky
    try:
        fetch.add_urls(store, [f"http://127.0.0.1:{port}/tpa.json.gz", f"http://localhost:{port}/tpa.json.gz"])
        h.drain(cfg, store, 90)
        report(store, "C. two links; the first loader's parse fails once")
        print(f"   parse calls: {state['calls']}; downloads dir: {sorted(p.name for p in cfg.downloads_dir.iterdir())}")
    finally:
        ing.run_in_network_parse = real
        httpd.shutdown()


def case_c_inbox():
    """link defers to an INBOX load that fails."""
    import mrfx.ingest as ing
    from mrfx import fetch
    from mrfx.ingest import scan_inbox
    cfg, store, httpd, port = setup("C2_inbox_fails_link_waits")
    real = ing.run_in_network_parse
    state = {"calls": 0}

    def flaky(*a, **k):
        state["calls"] += 1
        if state["calls"] == 1:
            raise RuntimeError("injected parser failure on the inbox loader")
        return real(*a, **k)
    ing.run_in_network_parse = flaky
    try:
        (cfg.inbox_dir / "tpa_copy.json.gz").write_bytes(GZ)
        scan_inbox(cfg, store)              # fails
        fetch.add_urls(store, [f"http://127.0.0.1:{port}/tpa.json.gz"])
        h.drain(cfg, store, 60)
        report(store, "C2. inbox load FAILED, then the link")
    finally:
        ing.run_in_network_parse = real
        httpd.shutdown()


def case_d():
    from mrfx import fetch
    cfg = h.scratch_cfg("s4_D_concurrent", parallel_downloads=2, parallel_ingests=1)
    store = h.open_store(cfg)
    h.ROUTES.clear(); h.LOG.clear()
    h.ROUTES["/tpa.json.gz"] = serve(GZ)
    httpd, base = h.start_server()
    port = base.rsplit(":", 1)[1]
    urls = [f"http://127.0.0.1:{port}/tpa.json.gz", f"http://localhost:{port}/tpa.json.gz",
            f"http://127.0.0.1:{port}/tpa.json.gz?v=2", f"http://localhost:{port}/tpa.json.gz?v=2"]
    fetch.add_urls(store, urls)
    h.drain(cfg, store, 90)
    report(store, "D. four links, same bytes, two concurrent downloaders")
    httpd.shutdown()


if __name__ == "__main__":
    logging.basicConfig(level=logging.ERROR)
    case_a(); case_b(); case_c(); case_c_inbox(); case_d()
