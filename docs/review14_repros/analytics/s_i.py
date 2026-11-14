"""Scenario I: forget / replace a file -> its rows leave the spine and the
coverage stays truthful. Real 7-file store (built) + a synthetic payer.
I1: dashboard remove (DELETE /api/files/<f>) of one of BCBS Wyoming's TWO files
I2: dashboard remove of Moda's ONLY file
I3: same-name different-content inbox drop -> needs_choice -> "replace"
I4: `mrfx forget` with the server stopped, then restart (no other activity)"""
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).parent))
import h  # noqa: E402

PORT = 8477
D = h.ROOT / "I"
WY = "wy_2026-10-27_000005300_in-network-rates_05_of_09.json.gz"
MODA = "moda_ModaTXPriceTrans_20261101.json.gz"
SEC = "security_networx_PRG000049.zip"


def stats():
    return h.api(PORT, "/api/stats", timeout=60).get("analytics") or {}


def wait_current(want_done, timeout=300):
    t = time.time()
    while time.time() - t < timeout:
        a = stats()
        if a.get("state") == "current" and a.get("done_files") == want_done == a.get("covered_files"):
            return round(time.time() - t, 1), a
        time.sleep(0.3)
    return None, stats()


def step(label, payers, action, want_done, res):
    before = h.snap(PORT, payers)
    poll = h.Poller(PORT, every=0.3, light=False)
    poll.start()
    t0 = time.time()
    out = action()
    took, a = wait_current(want_done)
    time.sleep(1)
    poll.stop()
    after = h.snap(PORT, payers)
    # any moment where the header said 'current' while payers/covered disagreed
    lies = [r for r in poll.rows if (r.get("a") or {}).get("state") == "current"
            and r.get("covered") is not None and r.get("payers") is not None
            and set(r["covered"]) != set(r["payers"])]
    res[label] = {"action_out": out, "to_current_s": took, "final": a,
                  "timeline": h.states_timeline(poll.rows, t0),
                  "before": {k: before.get(k) for k in ("rates_total", "rates_by_payer", "payers", "covered", "code_payer_rank")},
                  "after": {k: after.get(k) for k in ("rates_total", "rates_by_payer", "payers", "covered", "code_payer_rank")},
                  "current_but_pickers_disagree": len(lies)}
    print(label, json.dumps({k: res[label][k] for k in ("to_current_s", "before", "after")}, default=str)[:1500], flush=True)


def main():
    if D.exists():
        shutil.rmtree(D)
    shutil.copytree(h.ROOT / "bases" / "base7_built", D)
    cfgp = h.write_cfg(D, PORT)
    res = {}
    srv = h.Serve(cfgp, PORT, D / "serve_1.log")
    srv.wait_ready()
    step("I1_remove_one_of_two", ["Blue Cross Blue Shield of Wyoming"],
         lambda: h.api(PORT, f"/api/files/{quote(WY)}", method="DELETE"), 6, res)
    step("I2_remove_only_file", ["Moda"],
         lambda: h.api(PORT, f"/api/files/{quote(MODA)}", method="DELETE"), 5, res)
    # I3: a synthetic payer, then the same NAME with different content
    staged = D / "staged"
    h.gen_mrf(staged / "v1" / "zeta_rates.json.gz", "Zeta Mutual", 9000, 100, seed=7, rate_base=70)
    h.gen_mrf(staged / "v2" / "zeta_rates.json.gz", "Zeta Mutual", 9500, 40, seed=8, rate_base=140)

    def drop(src):
        shutil.copy(src, D / "inbox" / ".t_zeta")
        (D / "inbox" / ".t_zeta").rename(D / "inbox" / "zeta_rates.json.gz")
        return "dropped"
    step("I3a_add_zeta_v1", ["Zeta Mutual"], lambda: drop(staged / "v1" / "zeta_rates.json.gz"), 6, res)

    def replace():
        drop(staged / "v2" / "zeta_rates.json.gz")
        t = time.time()
        st = None
        while time.time() - t < 120:
            fl = h.api(PORT, "/api/files")
            st = {r["filename"]: r.get("status") for r in fl.get("files") or []}
            if "needs_choice" in st.values():
                break
            time.sleep(0.5)
        held = [k for k, v in (st or {}).items() if v == "needs_choice"]
        mid = stats()
        r = h.api(PORT, f"/api/files/{quote(held[0])}/choose", method="POST", json={"action": "replace"}) if held else None
        return {"held": held, "header_while_held": mid.get("sentence") or mid.get("state"), "choose": r}
    step("I3b_replace_zeta", ["Zeta Mutual"], replace, 6, res)
    srv.stop()
    res["truth_after_I3"] = h.short_truth(h.run_truth_subprocess(D / "store", hash_rows=False))
    # I4: the CLI forget with the server stopped, then a restart with nothing else
    r = subprocess.run([h.PY, "-m", "mrfx", "--config", str(cfgp), "forget", SEC],
                       capture_output=True, text=True, cwd=str(D))
    res["I4_forget_cli"] = (r.returncode, (r.stdout + r.stderr)[-800:])
    t_mid = h.run_truth_subprocess(D / "store", hash_rows=False)
    res["I4_truth_before_restart"] = h.short_truth(t_mid) | {"meta": t_mid.get("meta")}
    srv = h.Serve(cfgp, PORT, D / "serve_2.log")
    srv.wait_ready()
    t0 = time.time()
    took, a = wait_current(5)
    res["I4_restart"] = {"to_current_s": took, "final": a, "payers": h.api(PORT, "/api/payers")}
    srv.stop()
    res["truth_final"] = h.short_truth(h.run_truth_subprocess(D / "store", hash_rows=False))
    res["log"] = h.grep_log(D / "serve_1.log", "analytics", "remov", "forget", "replace") + \
        h.grep_log(D / "serve_2.log", "analytics", "catch")
    h.save("i.json", res)
    print(json.dumps({k: res[k] for k in ("truth_after_I3", "I4_forget_cli", "I4_truth_before_restart", "I4_restart", "truth_final")},
                     default=str, indent=1)[:5000])
    shutil.rmtree(D / "store", ignore_errors=True)


if __name__ == "__main__":
    main()
