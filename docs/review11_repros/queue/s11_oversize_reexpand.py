"""An 'oversize' child (too big for confirm_over_gb) is re-queued every time
its index is re-expanded (monthly refresh / requeue_urls): how many requests
does that cost, and does it stop at the headers?"""
from h import *  # noqa

if __name__ == "__main__":
    root, cfg, store = world("s11", confirm_over_gb=0.001)   # 1 MB ceiling
    big = rate_file_bytes("big", n_rows=1, pad=3_000_000)
    for chunked in (False, True):
        root, cfg, store = world(f"s11_{'chunked' if chunked else 'sized'}", confirm_over_gb=0.001)
        srv = Server({"/big.json.gz": {"body": big, "chunked": chunked, "piece": 256 << 10},
                      "/small.json.gz": rate_file_bytes("small")})
        srv.routes["/toc.json"] = toc_bytes([srv.url("/big.json.gz"), srv.url("/small.json.gz")])
        add_urls(store, [srv.url("/toc.json")]); drain(cfg, store, 120)
        r = status_of(store, "big.json.gz")
        print(f"[{'chunked' if chunked else 'sized'}] first pass: big -> {r['status']}; GETs on big: {len(srv.hits('/big.json.gz'))}; bytes_total={next(x for x in rows(store) if x['url']=='big.json.gz')['bytes_total']}")
        for month in (1, 2, 3):
            store.requeue_urls([srv.url("/toc.json")]); drain(cfg, store, 120)
            r = status_of(store, "big.json.gz")
            part = [p for p in downloads(cfg) if p.endswith(".part")]
            print(f"   re-expansion {month}: big -> {r['status']}; GETs on big so far: {len(srv.hits('/big.json.gz'))}; .part on disk: {part}")
        print("   restart with the limit unchanged:", drain(cfg, store, 60)["finished"], "GETs:", len(srv.hits('/big.json.gz')))
        srv.stop()
