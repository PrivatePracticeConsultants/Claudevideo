"""J3 (= J2 with ONLY the inbox drop): files arriving WHILE the name refresh holds the rollup lock (bulk NPPES
enrichment; every name refresh held 60 s longer). An inbox drop and a pasted
link land inside that window: are they skipped, retried, how long, any false
'current'?"""
import json, shutil, sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent)); import h
S = Path("/tmp/claude-0/-home-user-Claudevideo/4633d594-0139-5f8e-a935-ae9c76b72000/scratchpad")
PORT, FPORT = 8493, 8494
if __name__ == "__main__":
    D = h.ROOT / "J3"
    if D.exists(): shutil.rmtree(D)
    shutil.copytree(h.ROOT / "bases" / "base7_built", D)
    web = D / "web"; web.mkdir()
    h.gen_mrf(D / "staged" / "kilo_rates.json.gz", "Kilo Benefit Trust", 40000, 120, seed=11, rate_base=55)
    h.gen_mrf(web / "lima_rates.json.gz", "Lima Care", 41000, 90, seed=12, rate_base=65)
    fs = h.FileServer(web, FPORT)
    cfgp = h.write_cfg(D, PORT, enrichment={"mode": "bulk", "auto_download": False,
                                            "nppes_dir": str(S / "nppes_live" / "nppes")})
    evp = D / "events.jsonl"
    env = {"PYTHONPATH": str(h.ROOT / "hooks"), "MRFX_AUDIT_HOOKS": "1", "MRFX_AUDIT_EVENTS": str(evp),
           "MRFX_AUDIT_DIR_SLEEP": "60"}
    srv = h.Serve(cfgp, PORT, D / "serve.log", env_extra=env); srv.wait_ready()
    T0 = time.time()
    poll = h.Poller(PORT, every=0.5, light=False); poll.start()
    ev = lambda: [json.loads(x) for x in evp.read_text().splitlines()] if evp.exists() else []
    while time.time() - T0 < 600 and not any(e["ev"] == "names_hold" for e in ev()):
        time.sleep(0.3)
    t_hold = time.time() - T0
    time.sleep(3)
    shutil.copy(D / "staged" / "kilo_rates.json.gz", D / "inbox" / ".t"); (D / "inbox" / ".t").rename(D / "inbox" / "kilo_rates.json.gz")
    t_kilo = time.time() - T0
    t_lima = -1
    t_cur = None
    while time.time() - T0 < 900:
        a = (poll.rows[-1].get("a") if poll.rows else None) or {}
        if a.get("done_files") == 8 and a.get("covered_files") == 8 and a.get("state") == "current":
            t_cur = time.time() - T0; break
        time.sleep(0.5)
    time.sleep(1); poll.stop()
    rows = poll.rows
    def first(pred):
        for r in rows:
            if pred(r.get("a") or {}): return round(r["t"] - T0, 1)
    res = {"names_hold_at": round(t_hold, 1), "kilo_at": round(t_kilo, 1), "lima_at": round(t_lima, 1),
           "both_done_at": first(lambda a: a.get("done_files") == 9), "all_covered_at": t_cur and round(t_cur, 1),
           "false_current": [(round(r["t"] - T0, 1), r["a"].get("covered_files"), r["a"].get("done_files")) for r in rows
                             if (r.get("a") or {}).get("state") == "current" and r["a"].get("covered_files") != r["a"].get("done_files")],
           "timeline": h.states_timeline(rows, T0),
           "events": [(round(e["t"] - T0, 1), e["ev"], e.get("ret")) for e in ev() if e["ev"] != "sql"]}
    srv.stop(); fs.close()
    res["log"] = h.grep_log(D / "serve.log", "analytics", "skipp", "name", "rebuild already")
    res["truth"] = h.short_truth(h.run_truth_subprocess(D / "store", hash_rows=False))
    h.save("j3.json", res)
    print(json.dumps({k: v for k, v in res.items() if k != "log"}, indent=1, default=str)[:5000])
    for l in res["log"]: print(l[:220])
    shutil.rmtree(D / "store", ignore_errors=True)
