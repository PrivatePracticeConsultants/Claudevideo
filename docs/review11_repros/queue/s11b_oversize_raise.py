"""The FAQ fix for 'too big': raise confirm_over_gb and restart. Does the
restart re-queue a row that was stopped at the Content-Length header?"""
from h import *  # noqa

if __name__ == "__main__":
    big = rate_file_bytes("big", n_rows=1, pad=3_000_000)      # ~1.5 MB gz
    for chunked in (False, True):
        root, cfg, store = world(f"s11b_{'chunked' if chunked else 'sized'}", confirm_over_gb=0.001)
        srv = Server({"/big.json.gz": {"body": big, "chunked": chunked, "piece": 256 << 10}})
        add_urls(store, [srv.url("/big.json.gz")]); drain(cfg, store, 60)
        r = rows(store)[0]
        print(f"[{'chunked' if chunked else 'sized'}] limit 1 MB: status={r['status']} bytes_total={r['bytes_total']} bytes_done={q(store,'SELECT bytes_done FROM url_queue')[0]['bytes_done']}")
        cfg.confirm_over_gb = 5.0        # the user raised the limit and restarts
        d = drain(cfg, store, 60)
        r = rows(store)[0]
        print(f"   after raising to 5 GB + restart: status={r['status']} rows={r['rows_emitted']} GETs={len(srv.hits('/big.json.gz'))} "
              f"(requeue_oversize_within re-queued it: {r['status'] != 'oversize'})")
        print("   'download anyway' then works:", store.force_size_requeue(r["id"]), drain(cfg, store, 60)["finished"], rows(store)[0]["status"])
        srv.stop(); shutil.rmtree(root, ignore_errors=True)
