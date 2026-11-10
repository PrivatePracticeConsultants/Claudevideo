"""Processor side: a fetched row whose dest/sidecar vanished; a downloader
thread that dies holding a row; rollup rebuild count per N files; the
delete_raw_after_ingest=false path and its orphans."""
import logging
import threading
from h import *  # noqa
import mrfx.fetch as mf

if __name__ == "__main__":
    logs = capture_logs(logging.INFO)
    banner("A: fetched row, then dest / sidecar / both vanish before the processor claims it")
    root, cfg, store = world("s8a")
    srv = Server({"/a.json.gz": rate_file_bytes("a"), "/b.json.gz": rate_file_bytes("b"), "/c.json.gz": rate_file_bytes("c")})
    for name, kill in (("a", ("dest", "meta")), ("b", ("meta",)), ("c", ("dest",))):
        add_urls(store, [srv.url(f"/{name}.json.gz")])
        rec = store.next_queued_url()
        assert mf.fetch_url_record(cfg, store, rec)
        dest = cfg.downloads_dir / filename_for(rec["url"])
        if "dest" in kill:
            dest.unlink()
        if "meta" in kill:
            mf._meta_path(dest).unlink()
        rec2 = store.next_fetched_url()
        st_during = {}
        def watch():
            while True:
                s = q(store, "SELECT status, bytes_done FROM url_queue WHERE id = ?", [rec2["id"]])[0]
                st_during[s["status"]] = s["bytes_done"]
                if s["status"] in ("done", "failed"):
                    return
                time.sleep(0.01)
        th = threading.Thread(target=watch, daemon=True); th.start()
        ok = mf.process_url_record(cfg, store, rec2)
        th.join(5)
        r = next(x for x in rows(store) if x["id"] == rec2["id"])
        print(f"  {name}: vanished={kill} -> ingested={ok} status={r['status']} rows={r['rows_emitted']} GETs={len(srv.hits(f'/{name}.json.gz'))} "
              f"statuses seen while re-downloading={list(st_during)}; cancel_all_downloading would see it: {'downloading' in st_during}")
    print("  downloads left:", downloads(cfg))

    banner("B: a downloader thread dies (KeyboardInterrupt escapes) while holding a row")
    root, cfg, store = world("s8b")
    srv.routes["/d.json.gz"] = rate_file_bytes("d")
    real = mf.fetch_url_record
    n = {"c": 0}
    def dying(cfg_, store_, rec, **kw):
        n["c"] += 1
        if n["c"] == 1:
            raise KeyboardInterrupt
        return real(cfg_, store_, rec, **kw)
    mf.fetch_url_record = dying
    add_urls(store, [srv.url("/d.json.gz")])
    d = drain(cfg, store, 60)
    mf.fetch_url_record = real
    print("  drain:", d, "row:", [(r["status"], r["rows_emitted"]) for r in rows(store)])
    print("  log:", [l for l in logs if "stopped unexpectedly" in l or "goes back" in l][:3])

    banner("C: rollup rebuilds per N files (drain of 30 small files)")
    root, cfg, store = world("s8c", parallel_downloads=3)
    for i in range(30):
        srv.routes[f"/r{i}.json.gz"] = rate_file_bytes(f"r{i}")
    add_urls(store, [srv.url(f"/r{i}.json.gz") for i in range(30)])
    before = len(logs)
    d = drain(cfg, store, 300)
    rl = [l for l in logs[before:] if "updating analytics rollups" in l or "analytics refresh took" in l]
    print(f"  drain {d}; rebuild log lines: {len(rl)}: {rl}")
    print("  done rows:", sum(1 for r in rows(store) if r["status"] == "done"))
    banner("C2: serve mode — files arriving one at a time with idle gaps: a rebuild per file?")
    root, cfg, store = world("s8c2")
    stop, th = serve(cfg, store)
    before = len(logs)
    for i in range(5):
        add_urls(store, [srv.url(f"/r{i}.json.gz")])
        wait_for(lambda: status_of(store, f"r{i}.json.gz")["status"] == "done", 30)
        time.sleep(1.5)
    stop.set(); th.join(30)
    rl = [l for l in logs[before:] if "updating analytics rollups" in l]
    print(f"  5 files one at a time -> {len(rl)} rebuilds")

    banner("D: delete_raw_after_ingest=false — what stays on disk, and does forget / re-paste reuse it?")
    root, cfg, store = world("s8d", delete_raw_after_ingest=False)
    srv.routes["/toc.json"] = toc_bytes([srv.url("/a.json.gz")])
    add_urls(store, [srv.url("/toc.json")])
    drain(cfg, store, 60)
    print("  after ingest, downloads/:", downloads(cfg))
    from mrfx.ingest import forget_file
    a = next(r for r in rows(store) if r["url"] == "a.json.gz")
    forget_file(cfg, store, a["filename"])
    print("  after forget, downloads/:", downloads(cfg), " row:", status_of(store, "a.json.gz")["status"])
    hits = len(srv.hits("/a.json.gz"))
    print("  re-paste a.json.gz:", add_urls(store, [srv.url("/a.json.gz")]), drain(cfg, store, 60)["finished"],
          " GETs added:", len(srv.hits("/a.json.gz")) - hits, " status:", status_of(store, "a.json.gz")["status"])
    # provider list arrives later -> requeue_url_for_file reuses the kept download?
    hits = len(srv.hits("/a.json.gz"))
    print("  requeue_url_for_file:", store.requeue_url_for_file(a["filename"], "ref arrived"), drain(cfg, store, 60)["finished"],
          " GETs added:", len(srv.hits("/a.json.gz")) - hits, " rows:", rates_per_file(store))
    print("  downloads/ at end:", downloads(cfg))
    srv.stop()
