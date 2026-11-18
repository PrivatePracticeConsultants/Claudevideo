"""TOC expansion: re-running an index, the max_toc_files cap, self/mutual
references, relative links after a redirect, a child also pasted directly,
one file under two plans, and whether a DONE index can ever be re-read."""
from h import *  # noqa

if __name__ == "__main__":
    root, cfg, store = world("s3", max_toc_files=3)
    logs = capture_logs()
    files = {f"/f{i}.json.gz": rate_file_bytes(f"f{i}") for i in range(6)}
    srv = Server(files)
    base = srv.base
    # index v1 lists f0..f4 (5 > cap 3), itself, and f0 again under a second plan
    srv.routes["/idx.json"] = json.dumps({
        "reporting_entity_name": "Testco Health", "version": "1",
        "reporting_structure": [
            {"reporting_plans": [{"plan_name": "P1", "plan_id_type": "EIN", "plan_id": "1"}],
             "in_network_files": [{"location": f"{base}/f{i}.json.gz"} for i in range(5)]
                                 + [{"location": f"{base}/idx.json"}]},
            {"reporting_plans": [{"plan_name": "P2", "plan_id_type": "EIN", "plan_id": "2"}],
             "in_network_files": [{"location": f"{base}/f0.json.gz"}]},
        ]}).encode()
    # redirect + relative locations
    srv.routes["/old/idx2.json"] = {"redirect": f"{base}/v2/idx2.json"}
    srv.routes["/v2/idx2.json"] = toc_bytes(["rel.json.gz", "/f5.json.gz"])
    srv.routes["/v2/rel.json.gz"] = rate_file_bytes("rel")
    # mutual TOCs
    srv.routes["/m1.json"] = toc_bytes([f"{base}/m2.json", f"{base}/f5.json.gz"])
    srv.routes["/m2.json"] = toc_bytes([f"{base}/m1.json", f"{base}/f5.json.gz"])
    srv.routes["/html_idx.json"] = toc_bytes([f"{base}/page.html"])
    srv.routes["/page.html"] = f'<html><body><a href="{base}/f5.json.gz">x</a></body></html>'.encode()

    banner("A: index over the cap (5 files, cap 3) + self-ref + f0 under two plans; f1 also pasted directly")
    print(add_urls(store, [f"{base}/f1.json.gz", f"{base}/idx.json"]))
    print(drain(cfg, store))
    print(table(store))
    idx = next(r for r in rows(store) if r["url"] == "idx.json")
    print("cap note on the index row:", idx["error"])
    print("retry allowed on the DONE index row (what the note tells the user to press)?",
          store.set_url_status_by_id(idx["id"], "queued"))
    print("re-pasting the same index:", add_urls(store, [f"{base}/idx.json"]))
    print("plan_index rows:", q(store, "SELECT plan_name, count(*) n FROM plan_index GROUP BY 1 ORDER BY 1"))
    lost = [f"f{i}" for i in range(5) if not any(r["url"] == f"f{i}.json.gz" for r in rows(store))]
    print("files listed but never queued:", lost)

    banner("B: raise the cap to 10 and restart (what the FAQ says) — does the index re-expand?")
    cfg.max_toc_files = 10
    print(drain(cfg, store))
    print("files still missing:", [f"f{i}" for i in range(5) if not any(r["url"] == f"f{i}.json.gz" for r in rows(store))])
    print("monthly-style requeue_urls on the index:", store.requeue_urls([f"{base}/idx.json"]))
    print(drain(cfg, store))
    print("files still missing after requeue_urls:", [f"f{i}" for i in range(5) if not any(r["url"] == f"f{i}.json.gz" for r in rows(store))])
    print("idx child_count now:", next(r for r in rows(store) if r["url"] == "idx.json")["child_count"])

    banner("C: redirect + relative locations; mutual TOCs; TOC -> HTML page")
    with store.connect() as con:
        con.execute("DELETE FROM url_queue")
    print(add_urls(store, [f"{base}/old/idx2.json", f"{base}/m1.json", f"{base}/html_idx.json"]))
    print(drain(cfg, store))
    print(table(store))
    print("requests:", sorted({p for p, _, _ in srv.log}))
    print("\nLOG warnings:", [l for l in logs if "WARNING" in l][:10])
    print("downloads left:", downloads(cfg))
    srv.stop()
