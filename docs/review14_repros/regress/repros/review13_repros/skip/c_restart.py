"""s4 case C, then: (1) the same Store keeps running the queue 20 s more;
(2) a fresh Store (a restart) runs recover_stuck_urls and the queue 20 s.
Does the deferred twin ever load?"""
import sys, time, threading
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
import h, s4_claims

def run_for(cfg, store, secs):
    from mrfx.fetch import run_queue
    stop = threading.Event()
    th = threading.Thread(target=lambda: run_queue(cfg, store, stop=stop), daemon=True)
    th.start(); time.sleep(secs); stop.set(); th.join(15)

if __name__ == "__main__":
    import mrfx.ingest as ing
    from mrfx import fetch
    from mrfx.store import Store
    cfg, store, httpd, port = s4_claims.setup("C_restart")
    real = ing.run_in_network_parse
    st = {"n": 0}
    def flaky(*a, **k):
        st["n"] += 1
        if st["n"] == 1:
            raise RuntimeError("injected parser failure on the first loader")
        return real(*a, **k)
    ing.run_in_network_parse = flaky
    fetch.add_urls(store, [f"http://127.0.0.1:{port}/tpa.json.gz", f"http://localhost:{port}/tpa.json.gz"])
    run_for(cfg, store, 20)
    s4_claims.report(store, "after 20 s of the same process")
    print("   stale content claims held in memory:", dict(store._content_claims))
    ing.run_in_network_parse = real
    store2 = Store(cfg.store_dir)
    print("   restart: recover_stuck_urls ->", store2.recover_stuck_urls())
    run_for(cfg, store2, 20)
    s4_claims.report(store2, "after a restart + 20 s")
    httpd.shutdown()
