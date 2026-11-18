"""Smoke: one rate file + one TOC through run_queue(drain) on a scratch store."""
from h import *  # noqa
if __name__ == "__main__":
    root, cfg, store = world("s0")
    srv = Server({"/a.json.gz": rate_file_bytes("a")})
    srv.routes["/toc.json"] = toc_bytes([srv.url("/a.json.gz"), srv.url("/b.json.gz")])
    srv.routes["/b.json.gz"] = rate_file_bytes("b")
    print(add_urls(store, [srv.url("/toc.json")]))
    print(drain(cfg, store))
    print(table(store))
    print(files_table(store)); print(rates_per_file(store)); print(downloads(cfg)); print(parts(cfg))
    srv.stop()
