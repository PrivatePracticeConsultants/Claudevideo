"""Size lanes with a queue of BIG files whose size is unknown up front (index
children are queued unsized). 4 download slots, small_file_downloads=3,
small_file_mb=1; 16 files of ~2.7 MB, each connection throttled.
usage: d4_lanes.py sized|chunked [lanes_off]"""
import sys, threading, time
from dl import *
import mrfx.fetch as F

if __name__ == "__main__":
    mode = sys.argv[1]; lanes_on = "lanes_off" not in sys.argv
    cfg, store = mk_cfg(f"d4_{mode}_{lanes_on}_" + __import__("os").environ.get("SLOTS", "4"))
    import os
    cfg.parallel_downloads = int(os.environ.get("SLOTS", "4"))
    cfg.small_file_mb = 1
    cfg.early_dedup_mb = 0
    cfg.download_segments = 1 if hasattr(cfg, "download_segments") else None
    if not lanes_on:
        cfg.small_file_downloads = 0
    N = 24
    files = {f"/big{i:02d}.json.gz": gz(doc(n=50, pad_mb=3.5, seed=100 + i, payer=f"Plan {i}")) for i in range(N)}
    srv = Srv(files, chunked=(mode == "chunked"), delay_per_chunk=0.06)   # ~2.5 s per file per connection
    real_proc = F.process_url_record
    F.process_url_record = lambda cfg_, store_, rec, *a, **k: (store_.update_url(rec["id"], status="done"), True)[1]
    urls = [srv.base + p for p in files]
    F.add_urls(store, urls)
    stop = threading.Event()
    t = threading.Thread(target=F.run_queue, args=(cfg, store, stop), daemon=True)
    t0 = time.time(); t.start()
    peak = 0; samples = []
    while time.time() - t0 < 120:
        c = store.url_queue_counts()
        peak = max(peak, c.get("downloading", 0))
        samples.append((round(time.time() - t0, 1), c.get("downloading", 0), c.get("queued", 0)))
        if c.get("done", 0) + c.get("failed", 0) == N:
            break
        time.sleep(0.25)
    took = time.time() - t0
    stop.set(); t.join(30)
    F.process_url_record = real_proc
    gets = [x for x in srv.log if x[1] == "GET"]; heads = [x for x in srv.log if x[1] == "HEAD"]
    per = {}
    for x in gets: per[x[0]] = per.get(x[0], 0) + 1
    first2 = sorted(x[4] - t0 for x in gets)
    print(f"[{mode}, lanes={'on' if lanes_on else 'off'}] {N} files, all done in {took:.1f} s; counts {store.url_queue_counts()}")
    print(f"   GET requests: {len(gets)} ({len(gets)/N:.1f} per file), HEAD: {len(heads)}; per file min/max {min(per.values())}/{max(per.values())}")
    print(f"   GETs issued in the first 2 s: {sum(1 for x in first2 if x < 2.0)} | in the first 5 s: {sum(1 for x in first2 if x < 5.0)}")
    sent = sum(srv.sent.values()); size = sum(len(b) for b in files.values())
    print(f"   bytes sent by the server: {sent/1e6:.1f} MB for {size/1e6:.1f} MB of files (overhead {100*(sent-size)/size:.0f} %)")
    busy = [s for s in samples if s[2] > 0]
    print(f"   while files were still queued: downloading slots min {min((s[1] for s in busy), default='-')} / "
          f"avg {sum(s[1] for s in busy)/max(len(busy),1):.1f} / max {max((s[1] for s in busy), default='-')} of {F.resolve_download_count(cfg)}")
    print("   big_lane flags:", q(store, "SELECT count(*) FILTER (WHERE big_lane), count(*) FROM url_queue")[0])
    srv.close()
