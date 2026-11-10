"""Lanes and starvation: 50 big + 5 small; sized vs unsized (chunked)."""
import threading
from h import *  # noqa

def run(variant, chunked):
    root, cfg, store = world(f"s7_{variant}", parallel_downloads=6, small_file_downloads=3,
                             small_file_mb=1, parallel_ingests=1, download_timeout_seconds=30)
    logs = capture_logs(logging.WARNING)
    srv = Server({})
    big = rate_file_bytes("big", n_rows=1, pad=2_500_000)
    for i in range(50):
        srv.routes[f"/big{i}.json.gz"] = {"body": big, "piece": 64 << 10, "pace": 0.03, "chunked": chunked}
    for i in range(5):
        srv.routes[f"/small{i}.json.gz"] = {"body": rate_file_bytes(f"s{i}"), "chunked": chunked}
    add_urls(store, [srv.url(f"/big{i}.json.gz") for i in range(50)] + [srv.url(f"/small{i}.json.gz") for i in range(5)])
    first = {}   # id -> {status: t}
    stop = threading.Event()

    def poll():
        while not stop.is_set():
            for r in q(store, "SELECT id, status, url FROM url_queue"):
                first.setdefault(r["id"], {}).setdefault(r["status"], time.monotonic())
            time.sleep(0.05)
    th = threading.Thread(target=poll, daemon=True); th.start()
    t0 = time.monotonic()
    d = drain(cfg, store, 400)
    stop.set(); th.join()
    srv.stop()
    order = [p.split("/")[-1].split(".")[0] for p, _, _ in srv.log]
    small_pos = [order.index(f"small{i}") for i in range(5)]
    small_rows = [r for r in rows(store) if r["url"].startswith("small")]
    ids = {r["url"]: r["id"] for r in rows(store)}
    print(f"[{variant}] drain {d}; total requests {len(order)}; small files' request positions (of {len(order)}): {small_pos}")
    for i in range(5):
        f = first.get(ids[f"small{i}.json.gz"], {})
        print(f"   small{i}: claimed @{f.get('downloading', 0) - t0:5.1f}s fetched @{f.get('fetched', 0) - t0:5.1f}s done @{f.get('done', 0) - t0:5.1f}s")
    bigs = [first[ids[f'big{i}.json.gz']].get('done', 0) - t0 for i in range(50)]
    print(f"   big files done between {min(bigs):.1f}s and {max(bigs):.1f}s; statuses: {dict((s, sum(1 for r in rows(store) if r['status']==s)) for s in ('done','failed','queued','skipped'))}")
    print("   big_lane flags set:", sum(1 for r in rows(store) if r["big_lane"]), " bytes_total known on big rows:", sum(1 for r in rows(store) if r["url"].startswith("big") and r["bytes_total"]))
    disk = [l for l in logs if "disk" in l]
    print("   disk-guard log lines:", len(disk), disk[:2])
    print("   errors on rows:", {r["error"][:60] for r in rows(store) if r["error"]})

if __name__ == "__main__":
    import logging
    run("sized", chunked=False)
    run("chunked", chunked=True)
