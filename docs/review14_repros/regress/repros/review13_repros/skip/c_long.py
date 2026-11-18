"""s4_claims case C with a drain that keeps the queue running 40 s after the rows look terminal."""
import sys, time, threading
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
import h, s4_claims

def long_drain(cfg, store, secs=60, settle=None):
    from mrfx.fetch import run_queue
    stop = threading.Event()
    th = threading.Thread(target=lambda: run_queue(cfg, store, stop=stop), daemon=True)
    th.start()
    t0 = time.monotonic()
    seen = []
    while time.monotonic() - t0 < 40:
        rows = h.queue_rows(store, "id, status, kind")
        snap = [(r["id"], r["status"], r["kind"]) for r in rows]
        if not seen or seen[-1][1] != snap:
            seen.append((round(time.monotonic() - t0, 1), snap))
        time.sleep(0.3)
    stop.set(); th.join(10)
    for s in seen:
        print("  t=%5.1fs %s" % s)

if __name__ == "__main__":
    h.drain = long_drain
    s4_claims.case_c()
