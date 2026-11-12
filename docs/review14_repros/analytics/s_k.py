"""Scenario K: one small real payer file into the 99,000,000-rate store through
the inbox while serve runs; time to covered, what the incremental did, peak
RSS; then `mrfx forget` it with the server stopped and confirm the spine is
back to its previous content (set hash)."""
import json
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import h  # noqa: E402

S = Path("/tmp/claude-0/-home-user-Claudevideo/4633d594-0139-5f8e-a935-ae9c76b72000/scratchpad")
CFG = S / "ui7" / "scale.yaml"
STORE = S / "scale4n" / "store"
INBOX = S / "scale4n" / "in"
MODA = S / "real" / "moda_ModaTXPriceTrans_20261101.json.gz"
PORT = 8392


def khash():
    r = subprocess.run([h.PY, str(h.ROOT / "k_hash.py"), str(STORE / "mrfx.duckdb")],
                       capture_output=True, text=True)
    return json.loads(r.stdout.strip().splitlines()[-1])


def main():
    res = {}
    evp = h.ROOT / "out" / "k_events.jsonl"
    evp.unlink(missing_ok=True)
    env = {"PYTHONPATH": str(h.ROOT / "hooks"), "MRFX_AUDIT_HOOKS": "1",
           "MRFX_AUDIT_EVENTS": str(evp), "MRFX_AUDIT_TRACE_SQL": "1"}
    log = h.ROOT / "out" / "k_serve.log"
    srv = h.Serve(CFG, PORT, log, env_extra=env, cwd=h.ROOT)
    t_start = time.time()
    srv.wait_ready(600)
    res["ready_s"] = round(time.time() - t_start, 1)
    rss = {"peak_total": 0.0, "peak_main": 0.0, "samples": []}
    halt = threading.Event()

    def sample():
        while not halt.is_set():
            a, m = srv.rss_mb(), srv.main_rss_mb()
            rss["peak_total"] = max(rss["peak_total"], a)
            rss["peak_main"] = max(rss["peak_main"], m)
            rss["samples"].append((round(time.time() - t_start, 1), round(m), round(a)))
            halt.wait(0.5)
    threading.Thread(target=sample, daemon=True).start()
    time.sleep(5)
    res["before"] = h.snap(PORT, ["Moda"])
    res["rss_idle_mb"] = round(srv.main_rss_mb())
    poll = h.Poller(PORT, every=0.5)
    poll.start()
    T0 = time.time()
    shutil.copy(MODA, INBOX / (".t_" + MODA.name))
    (INBOX / (".t_" + MODA.name)).rename(INBOX / MODA.name)
    t_done = None
    while time.time() - T0 < 1800:
        a = (poll.rows[-1].get("a") if poll.rows else None) or {}
        if t_done is None and (a.get("done_files") or 0) >= 46:
            t_done = time.time() - T0
        if a.get("state") == "current" and a.get("covered_files") == 46 and a.get("done_files") == 46:
            break
        time.sleep(0.5)
    t_cov = time.time() - T0
    time.sleep(2)
    poll.stop()
    res["drop_to_done_s"] = t_done and round(t_done, 1)
    res["drop_to_covered_s"] = round(t_cov, 1)
    res["timeline"] = h.states_timeline(poll.rows, T0)
    res["after"] = h.snap(PORT, ["Moda"])
    halt.set()
    res["rss_peak_main_mb"] = round(rss["peak_main"])
    res["rss_peak_total_mb"] = round(rss["peak_total"])
    srv.stop()
    ev = [json.loads(x) for x in evp.read_text().splitlines() if x.strip()]
    res["events"] = [(round(e["t"] - T0, 2), e["ev"], e.get("s"), e.get("took")) for e in ev]
    res["log"] = h.grep_log(log, "analytics", "rollup", "moda", "Moda")
    res["hash_with_moda"] = khash()
    # ---- forget it with the server stopped
    t = time.time()
    r = subprocess.run([h.PY, "-m", "mrfx", "--config", str(CFG), "forget", MODA.name],
                       capture_output=True, text=True, cwd=str(h.ROOT))
    res["forget"] = {"rc": r.returncode, "out": (r.stdout + r.stderr)[-1500:], "s": round(time.time() - t, 1)}
    res["hash_after_forget"] = khash()
    h.save("k.json", res)
    print(json.dumps({k: v for k, v in res.items() if k not in ("events", "log", "before", "after")},
                     default=str, indent=1)[:4000])


if __name__ == "__main__":
    main()
