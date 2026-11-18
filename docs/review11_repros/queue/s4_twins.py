"""Byte-identical twins: both lanes at once, in sequence, owner fails, owner
stopped by the user, owner forgotten, third copy — exactly one ingest?"""
from h import *  # noqa

if __name__ == "__main__":
    root, cfg, store = world("s4", parallel_downloads=2)
    logs = capture_logs()
    twin = rate_file_bytes("twin", n_rows=3)
    srv = Server({"/h1/x.json.gz": twin, "/h2/x.json.gz": twin, "/h3/x.json.gz": twin,
                  "/h4/x.json.gz": twin, "/h5/x.json.gz": {"status": 403, "body": b"x"}})
    base = srv.base

    banner("A: two twins queued together, 2 download slots")
    print(add_urls(store, [f"{base}/h1/x.json.gz", f"{base}/h2/x.json.gz"]))
    print(drain(cfg, store)); print(table(store))
    print("rates per file:", rates_per_file(store), " downloads:", downloads(cfg))

    banner("B: a third twin later (sequence)")
    print(add_urls(store, [f"{base}/h3/x.json.gz"]))
    print(drain(cfg, store)); print(table(store))
    print("rates per file:", rates_per_file(store), " downloads:", downloads(cfg))

    banner("C: owner's row fails on a RETRY (link now 403) — are the duplicates revived although the data is loaded?")
    owner = next(r for r in rows(store) if r["status"] == "done")
    # the user re-pastes h1's link under a now-dead signature: same dedup key
    srv.routes["/h1/x.json.gz"] = {"status": 403, "body": b"x"}
    from mrfx.ingest import forget_file
    # (retry is only possible after forget; forget flips done->skipped and nulls the twins' sha)
    print("forget owner:", forget_file(cfg, store, owner["filename"]))
    print(table(store))
    print("retry owner (now 403):", store.set_url_status_by_id(owner["id"], "queued"))
    print(drain(cfg, store)); print(table(store))
    print("rates per file:", rates_per_file(store), " files:", files_table(store))
    print("-> twins h2/h3 after the owner was forgotten + its retry failed: are they re-queued by anything?")
    print("recover_stuck_urls revive at next start:", store.recover_stuck_urls())
    print(table(store))

    banner("D: fresh store — owner STOPPED by the user mid-ingest; duplicate promised an auto-retry")
    root, cfg, store = world("s4d", parallel_downloads=2)
    import mrfx.fetch as mf
    import mrfx.ingest as mi
    real_ingest = mi.ingest_file
    stopped_once = {"n": 0}

    def stop_first(cfg_, store_, path, **kw):
        if stopped_once["n"] == 0:
            stopped_once["n"] += 1
            # what the "stop" button yields from ingest_file
            store_.upsert_file(path.name, status="stopped", error="stopped by you")
            return {"status": "stopped", "error": "stopped by you"}
        return real_ingest(cfg_, store_, path, **kw)
    mf.ingest_file = stop_first
    print(add_urls(store, [f"{base}/h1/x.json.gz", f"{base}/h2/x.json.gz"]))
    srv.routes["/h1/x.json.gz"] = twin
    print(drain(cfg, store)); print(table(store))
    print("rates:", rates_per_file(store), " downloads kept:", downloads(cfg))
    print("recover_stuck_urls revive:", store.recover_stuck_urls()); print(table(store))
    mf.ingest_file = real_ingest

    banner("E: fresh store — the 'done' twin check vs a twin that is 'skipped' by the user before ingest")
    root, cfg, store = world("s4e", parallel_downloads=1)
    print(add_urls(store, [f"{base}/h1/x.json.gz", f"{base}/h2/x.json.gz"]))
    r2 = next(r for r in rows(store) if "h2" in r["url"] or r["id"] == 2)
    print("user skips row 2 while queued:", store.set_url_status_by_id(2, "skipped"))
    print(drain(cfg, store)); print(table(store))
    print("user retries row 2:", store.set_url_status_by_id(2, "queued"))
    print(drain(cfg, store)); print(table(store))
    print("rates per file:", rates_per_file(store))
    print("\nINFO lines about twins:", [l for l in logs if "identical" in l or "re-queued" in l][:12])
    srv.stop()
