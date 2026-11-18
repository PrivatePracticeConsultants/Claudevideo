"""After a LINK load fails (s4 case C shape, one link), the user drops the same
bytes in the inbox in the same process (what `mrfx serve`'s watcher does).
Does scan_inbox return, or wait on the failed link's content claim?"""
import sys, time, threading
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
import h, s4_claims

if __name__ == "__main__":
    import mrfx.ingest as ing
    from mrfx import fetch
    from mrfx.ingest import scan_inbox
    cfg, store, httpd, port = s4_claims.setup("C_inbox_wait")
    real = ing.run_in_network_parse
    st = {"n": 0}
    def flaky(*a, **k):
        st["n"] += 1
        if st["n"] == 1:
            raise RuntimeError("injected parser failure on the link load")
        return real(*a, **k)
    ing.run_in_network_parse = flaky
    fetch.add_urls(store, [f"http://127.0.0.1:{port}/tpa.json.gz"])
    h.drain(cfg, store, 30)
    s4_claims.report(store, "link failed once")
    print("   content claims still held:", len(store._content_claims))
    (cfg.inbox_dir / "tpa_copy.json.gz").write_bytes(s4_claims.GZ)
    out = {}
    th = threading.Thread(target=lambda: out.setdefault("r", scan_inbox(cfg, store)), daemon=True)
    t0 = time.monotonic(); th.start(); th.join(30)
    print(f"   scan_inbox returned: {not th.is_alive()} after {time.monotonic()-t0:.1f}s -> {out.get('r')}")
    httpd.shutdown()
