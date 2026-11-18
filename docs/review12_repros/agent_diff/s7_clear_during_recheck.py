"""S7: "Clear queue" pressed while a re-expanded index is RE-CHECKING its loaded
children (_refresh_loaded_children: one request per loaded child, after the
only_if_expanding enqueue step). Does the user's clear stick, or does the
expansion finish, requeue republished children and write 'done' over the skip?"""
import json
import logging
import sys
import threading
import time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
import h  # noqa: E402

STATE = {"children": {}, "etag": '"v1"'}
N = 6


def index(hh, head, entry):
    doc = {"reporting_entity_name": "Acme", "reporting_structure": [
        {"reporting_plans": [{"plan_name": "PPO", "plan_id_type": "EIN", "plan_id": "1", "plan_market_type": "group"}],
         "in_network_files": [{"description": f"c{i}", "location": f"{STATE['base']}/c{i}.json.gz"} for i in range(N)]}]}
    h.send(hh, entry, 200, {}, json.dumps(doc).encode())


def child(hh, head, entry):
    i = int(hh.path.split("?")[0][2:-8])
    body = STATE["children"][i]
    rng = h.parse_range(hh, len(body))
    hdr = {"ETag": STATE["etag"]}
    if rng:
        a, b = rng
        hdr["Content-Range"] = f"bytes {a}-{b}/{len(body)}"
        h.send(hh, entry, 206, hdr, body[a:b + 1])
    else:
        h.send(hh, entry, 200, hdr, body)


def main():
    logging.basicConfig(level=logging.WARNING)
    cfg = h.scratch_cfg("s7", parallel_downloads=1, parallel_ingests=1)
    store = h.open_store(cfg)
    for i in range(N):
        STATE["children"][i] = h.rate_file_bytes(20, day="2026-05-01", payer=f"P{i}", tin0=400000000 + i * 1000)
        h.ROUTES[f"/c{i}.json.gz"] = child
    h.ROUTES["/index.json"] = index
    httpd, base = h.start_server()
    STATE["base"] = base
    from mrfx import fetch
    fetch.add_urls(store, [base + "/index.json"])
    h.drain(cfg, store)
    print("after first load:", sorted((r["status"], r["kind"]) for r in h.queue_rows(store)))
    # the payer republishes every child; the probe is slow (a far-away server: 1.5 s each)
    STATE["etag"] = '"v2"'
    for i in range(N):
        STATE["children"][i] = h.rate_file_bytes(30, day="2026-06-01", payer=f"P{i}", tin0=400000000 + i * 1000)
    real = fetch._server_now

    def slow(cfg_, url):
        time.sleep(1.5)
        return real(cfg_, url)
    fetch._server_now = slow
    store.requeue_urls([base + "/index.json"])
    stop = threading.Event()
    th = threading.Thread(target=lambda: fetch.run_queue(cfg, store, stop=stop), daemon=True)
    th.start()
    # wait until the index is 'expanding' and its re-check has started, then press Clear queue
    t0 = time.monotonic()
    while time.monotonic() - t0 < 30:
        st = h.queue_rows(store)[0]["status"]
        if st == "expanding":
            break
        time.sleep(0.05)
    time.sleep(1.0)
    print("Clear queue pressed at expanding:", h.queue_rows(store)[0]["status"], "->", store.clear_queued(), "rows cleared")
    print("  right after:", sorted((r["status"], r["kind"]) for r in h.queue_rows(store)))
    time.sleep(12)
    print("  12 s later:", sorted((r["status"], r["kind"]) for r in h.queue_rows(store)))
    print("  index row:", h.queue_rows(store, "id, status, error")[0])
    t1 = time.monotonic()
    while time.monotonic() - t1 < 40 and any(r["status"] in ("queued", "downloading", "fetched", "ingesting", "expanding") for r in h.queue_rows(store)):
        time.sleep(0.3)
    print("  settled:", sorted((r["status"], r["kind"]) for r in h.queue_rows(store)))
    with store.connect() as con:
        print("  file months:", sorted(con.execute("SELECT file_month, count(*) FROM files GROUP BY 1").fetchall()))
    stop.set(); th.join(10)
    httpd.shutdown()


if __name__ == "__main__":
    main()
