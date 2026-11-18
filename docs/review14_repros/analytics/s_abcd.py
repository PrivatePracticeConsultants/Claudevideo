"""Scenarios A-D: fresh store + pasted links, then a new payer / a new file of
an existing payer while idle, then inbox drops (new payer, existing payer).
One real `mrfx serve`; every moment polled from outside."""
import json
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import h  # noqa: E402

PORT, FPORT = 8471, 8472


def files_done(port):
    d = h.api(port, "/api/files")
    rows = d.get("files") if isinstance(d, dict) else d
    return {r["filename"]: r for r in (rows or []) if r.get("status") == "done"}


def wait_cover(port, poll, want_files, want_payers, t_start, timeout=600, label=""):
    """Wait until /api/stats says want_files done AND covered, current."""
    t_done = None
    while time.time() - t_start < timeout:
        a = (h.api(port, "/api/stats").get("analytics") or {})
        if t_done is None and a.get("done_files", 0) >= want_files:
            t_done = time.time()
        if (a.get("state") == "current" and a.get("covered_files") == want_files
                and a.get("done_files") == want_files and a.get("covered_payers") == want_payers):
            return {"label": label, "done_at": round((t_done or time.time()) - t_start, 1),
                    "covered_at": round(time.time() - t_start, 1),
                    "done_to_covered_s": round(time.time() - (t_done or time.time()), 1),
                    "analytics": a}
        time.sleep(0.3)
    return {"label": label, "timeout": True, "analytics": a, "done_at": t_done and t_done - t_start}


def main():
    d = h.ROOT / "abcd"
    if d.exists():
        shutil.rmtree(d)
    web = d / "web"
    web.mkdir(parents=True)
    h.gen_mrf(web / "alpha_2026-10_a.json.gz", "Alpha Health Plan", 1000, 300, seed=1)
    h.gen_mrf(web / "bravo_2026-10_a.json.gz", "Bravo Insurance Co", 2000, 250, seed=2, rate_base=60)
    h.gen_mrf(web / "charlie_2026-10_a.json.gz", "Charlie Benefits", 3000, 200, seed=3, rate_base=120)
    h.gen_mrf(web / "alpha_2026-10_b.json.gz", "Alpha Health Plan", 5000, 150, seed=4, rate_base=200)
    staged = d / "staged"
    h.gen_mrf(staged / "delta_2026-10_a.json.gz", "Delta Care Network", 6000, 120, seed=5, rate_base=40)
    h.gen_mrf(staged / "bravo_2026-10_b.json.gz", "Bravo Insurance Co", 7000, 100, seed=6, rate_base=300)
    cfgp = h.write_cfg(d, PORT)
    fs = h.FileServer(web, FPORT)
    srv = h.Serve(cfgp, PORT, d / "serve.log")
    res = {"phases": []}
    try:
        res["ready_s"] = srv.wait_ready()
        poll = h.Poller(PORT, every=0.5, light=False)
        poll.start()
        T0 = time.time()
        res["before_A"] = h.snap(PORT)
        # ---- A: two payers pasted on a fresh store
        tA = time.time()
        res["add_A"] = h.add_links(PORT, [fs.url("alpha_2026-10_a.json.gz"), fs.url("bravo_2026-10_a.json.gz")])
        res["phases"].append(wait_cover(PORT, poll, 2, 2, tA, label="A"))
        res["after_A"] = h.snap(PORT, ["Alpha Health Plan", "Bravo Insurance Co", "Charlie Benefits"])
        time.sleep(5)
        # ---- B: idle, current; paste a NEW payer
        res["before_B"] = h.snap(PORT, ["Charlie Benefits"])
        tB = time.time()
        res["add_B"] = h.add_links(PORT, [fs.url("charlie_2026-10_a.json.gz")])
        res["phases"].append(wait_cover(PORT, poll, 3, 3, tB, label="B"))
        res["after_B"] = h.snap(PORT, ["Charlie Benefits"])
        time.sleep(5)
        # ---- C: new file of an EXISTING payer
        res["before_C"] = h.snap(PORT, ["Alpha Health Plan"])
        tC = time.time()
        res["add_C"] = h.add_links(PORT, [fs.url("alpha_2026-10_b.json.gz")])
        res["phases"].append(wait_cover(PORT, poll, 4, 3, tC, label="C"))
        res["after_C"] = h.snap(PORT, ["Alpha Health Plan"])
        time.sleep(5)
        # ---- D1: inbox drop, NEW payer
        res["before_D1"] = h.snap(PORT, ["Delta Care Network"])
        tD = time.time()
        shutil.copy(staged / "delta_2026-10_a.json.gz", d / "inbox" / ".delta.tmp")
        (d / "inbox" / ".delta.tmp").rename(d / "inbox" / "delta_2026-10_a.json.gz")
        res["phases"].append(wait_cover(PORT, poll, 5, 4, tD, label="D-new-payer"))
        res["after_D1"] = h.snap(PORT, ["Delta Care Network"])
        time.sleep(5)
        # ---- D2: inbox drop, EXISTING payer
        res["before_D2"] = h.snap(PORT, ["Bravo Insurance Co"])
        tD2 = time.time()
        shutil.copy(staged / "bravo_2026-10_b.json.gz", d / "inbox" / ".bravo.tmp")
        (d / "inbox" / ".bravo.tmp").rename(d / "inbox" / "bravo_2026-10_b.json.gz")
        res["phases"].append(wait_cover(PORT, poll, 6, 4, tD2, label="D-existing-payer"))
        res["after_D2"] = h.snap(PORT, ["Bravo Insurance Co"])
        time.sleep(3)
        poll.stop()
        res["timeline"] = h.states_timeline(poll.rows, T0)
        res["covered_vs_payers_mismatch"] = [
            {"dt": round(r["t"] - T0, 1), "covered": r.get("covered"), "a_cov": (r.get("a") or {}).get("covered_payers")}
            for r in poll.rows if r.get("covered") is not None and r.get("a")
            and len(r["covered"]) != r["a"].get("covered_payers")]
        res["files_api"] = {k: {kk: v.get(kk) for kk in ("rows_emitted", "finished_at", "payer")}
                            for k, v in files_done(PORT).items()}
    finally:
        srv.stop()
        fs.close()
    h.save("abcd.json", res)
    res["truth"] = h.short_truth(h.run_truth_subprocess(d / "store"))
    res["log_rollup"] = h.grep_log(d / "serve.log", "analytics", "rollup", "first analytics")
    h.save("abcd.json", res)
    print(json.dumps({k: res[k] for k in ("phases", "truth")}, indent=1, default=str))


if __name__ == "__main__":
    main()
