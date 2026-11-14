"""Early-dedup 'waiting' copy vs the CLI drain exit: twin A loads, copy B is
parked waiting_on A (counted as 'waiting', not 'paused'). Does the drain
leave B behind as paused-forever?"""
from h import *  # noqa

if __name__ == "__main__":
    twin = rate_file_bytes("w", n_rows=2, pad=2_600_000)
    left = 0
    for trial in range(6):
        root, cfg, store = world(f"s12_{trial}", parallel_downloads=2, early_dedup_mb=1)
        srv = Server({"/a/x.json.gz": {"body": twin, "piece": 64 << 10, "pace": 0.02},
                      "/b/x.json.gz": {"body": twin, "piece": 64 << 10, "pace": 0.02}})
        add_urls(store, [srv.url("/a/x.json.gz"), srv.url("/b/x.json.gz")])
        d = drain(cfg, store, 120)
        rs = rows(store)
        print(f"trial {trial}: {d['secs']}s", [(r["id"], r["status"], r["kind"], r["waiting_on"], (r["error"] or "")[:40]) for r in rs],
              "GETs:", len(srv.hits("/a/x.json.gz")), len(srv.hits("/b/x.json.gz")), "rates:", rates_per_file(store), "dl:", downloads(cfg))
        if any(r["status"] == "paused" for r in rs):
            left += 1
            print("   -> a second drain:", drain(cfg, store, 60)["secs"], [(r["id"], r["status"]) for r in rows(store)])
        srv.stop(); shutil.rmtree(root, ignore_errors=True)
    print("drains that exited with a waiting copy left 'paused':", left, "of 6")
