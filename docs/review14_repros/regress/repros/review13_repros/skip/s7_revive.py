"""S7: _enqueue_one revive rules on a direct re-paste.
 A. a skipped/duplicate link re-pasted: revived? bytes downloaded again?
    (early_dedup_mb=1 with a 3.5 MB gz, and =15 with the same file)
 B. a user-cancelled row re-pasted
 C. a DONE rate link re-pasted (the GETTING_STARTED advice for new codes):
    no-op? then the path that works (forget + re-paste)
 D. an oversize row: direct re-paste vs re-expansion
 E. a queued / paused row's url is refreshed by a re-paste; a downloading one is not"""
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import gen  # noqa: E402
import h  # noqa: E402

BIG = gen.gz0(gen.doc(n_items=1500, filler=2000))
SMALL = gen.gzn(gen.doc(n_items=100, filler=20))


def serve(body):
    def fn(hh, head, entry):
        rng = h.parse_range(hh, len(body))
        if rng:
            a, b = rng
            h.send(hh, entry, 206, {"Content-Range": f"bytes {a}-{b}/{len(body)}", "ETag": '"t"'}, body[a:b + 1], head=head)
        else:
            h.send(hh, entry, 200, {"ETag": '"t"'}, body, head=head)
    return fn


def rows(store):
    return h.queue_rows(store, "id, status, kind, duplicate_of, bytes_done, substr(error,1,100) AS error, url")


def sent(path, since):
    return sum(w for m, p, hd, w, s in h.LOG[since:] if p.split("?")[0] == path)


def case_a(early_mb):
    from mrfx import fetch
    cfg = h.scratch_cfg(f"s7_A_dup_repaste_{early_mb}", parallel_downloads=1, parallel_ingests=1, early_dedup_mb=early_mb)
    store = h.open_store(cfg)
    h.ROUTES.clear(); h.LOG.clear(); h.ROUTES["/big.json.gz"] = serve(BIG)
    httpd, base = h.start_server(); port = base.rsplit(":", 1)[1]
    fetch.add_urls(store, [f"http://127.0.0.1:{port}/big.json.gz"]); h.drain(cfg, store, 90)
    n = len(h.LOG)
    fetch.add_urls(store, [f"http://localhost:{port}/big.json.gz"]); h.drain(cfg, store, 90)
    first = sent("/big.json.gz", n)
    print(f"\n== A (early_dedup_mb={early_mb}): twin link first time -> {rows(store)[1]['status']}/{rows(store)[1]['kind']}, bytes sent {first:,} of {len(BIG):,}")
    n = len(h.LOG)
    r = fetch.add_urls(store, [f"http://localhost:{port}/big.json.gz"]); h.drain(cfg, store, 90)
    print(f"   re-paste of the skipped/duplicate link: add_urls -> {r}; bytes sent again {sent('/big.json.gz', n):,}; row now {rows(store)[1]['status']}/{rows(store)[1]['kind']} err={rows(store)[1]['error']!r}")
    httpd.shutdown()


def case_b():
    from mrfx import fetch
    cfg = h.scratch_cfg("s7_B_canceled", parallel_downloads=1, parallel_ingests=1)
    store = h.open_store(cfg)
    h.ROUTES.clear(); h.LOG.clear(); h.ROUTES["/s.json.gz"] = serve(SMALL)
    httpd, base = h.start_server()
    fetch.add_urls(store, [base + "/s.json.gz"])
    print("\n== B: clear_queued ->", store.clear_queued(), rows(store))
    r = fetch.add_urls(store, [base + "/s.json.gz"]); h.drain(cfg, store, 60)
    print("   re-paste of the canceled row:", r, rows(store))
    httpd.shutdown()


def case_c():
    from mrfx import fetch
    from mrfx.ingest import forget_file
    cfg = h.scratch_cfg("s7_C_done_repaste", parallel_downloads=1, parallel_ingests=1)
    store = h.open_store(cfg)
    h.ROUTES.clear(); h.LOG.clear(); h.ROUTES["/s.json.gz"] = serve(SMALL)
    httpd, base = h.start_server()
    fetch.add_urls(store, [base + "/s.json.gz"]); h.drain(cfg, store, 60)
    n = len(h.LOG)
    r = fetch.add_urls(store, [base + "/s.json.gz"]); h.drain(cfg, store, 20)
    print(f"\n== C: done rate link re-pasted: add_urls -> {r}; requests since: {len(h.LOG) - n}; row: {rows(store)}")
    print("   GETTING_STARTED.md:688-690 says: 'To pick up new codes in files you already have, re-add those links (the app re-downloads and re-extracts them).'")
    # the path that works: forget, then re-paste
    fname = rows(store)[0]["url"] and h.queue_rows(store, "filename")[0]["filename"]
    print("   forget_file ->", forget_file(cfg, store, fname))
    print("   row after forget:", rows(store))
    n = len(h.LOG)
    r = fetch.add_urls(store, [base + "/s.json.gz"]); h.drain(cfg, store, 60)
    print(f"   re-paste after forget: add_urls -> {r}; requests {len(h.LOG) - n}; row: {rows(store)}")
    with store.connect() as con:
        print("   rates:", con.execute("SELECT count(*) FROM rates").fetchone())
    # a done TOC re-pasted: is it re-fetched, and do its done children move?
    httpd.shutdown()


def case_d():
    from mrfx import fetch
    cfg = h.scratch_cfg("s7_D_oversize", parallel_downloads=1, parallel_ingests=1, confirm_over_gb=0.001)
    store = h.open_store(cfg)
    h.ROUTES.clear(); h.LOG.clear(); h.ROUTES["/big.json.gz"] = serve(BIG)
    httpd, base = h.start_server()
    fetch.add_urls(store, [base + "/big.json.gz"]); h.drain(cfg, store, 60)
    print("\n== D: first:", [(r['status'], r['kind'], r['error']) for r in rows(store)])
    r = fetch.add_urls(store, [base + "/big.json.gz"])
    print("   direct re-paste of the oversize row:", r, [(r['status']) for r in rows(store)])
    h.drain(cfg, store, 60)
    print("   after drain:", [(r['status'], r['error']) for r in rows(store)])
    httpd.shutdown()


def case_e():
    from mrfx import fetch
    cfg = h.scratch_cfg("s7_E_url_refresh", parallel_downloads=1, parallel_ingests=1)
    store = h.open_store(cfg)
    fetch.add_urls(store, ["https://cdn.example.com/r.json.gz?sig=OLD&se=2026-01-01"])
    fetch.add_urls(store, ["https://cdn.example.com/r.json.gz?sig=NEW&se=2026-02-01"])
    print("\n== E: queued row after re-paste with a new signature:", rows(store)[0]["url"])
    with store.write_lock, store.connect() as con:
        con.execute("UPDATE url_queue SET status = 'downloading'")
    fetch.add_urls(store, ["https://cdn.example.com/r.json.gz?sig=NEWER&se=2026-03-01"])
    print("   downloading row after re-paste:", rows(store)[0]["url"])
    with store.write_lock, store.connect() as con:
        con.execute("UPDATE url_queue SET status = 'done', kind = 'in_network'")
    fetch.add_urls(store, ["https://cdn.example.com/r.json.gz?sig=NEWEST&se=2026-04-01"])
    print("   done row after re-paste (url kept fresh for the next re-check?):", rows(store)[0]["url"], rows(store)[0]["status"])


if __name__ == "__main__":
    logging.basicConfig(level=logging.ERROR)
    case_a(1); case_a(15); case_b(); case_c(); case_d(); case_e()
