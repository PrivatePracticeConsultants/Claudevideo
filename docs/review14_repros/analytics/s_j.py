"""Scenario J: the enrichment name refresh (bulk NPPES; every refresh held
30 s longer to widen the window) and the monthly re-check (a pasted index
whose child changed) running at the same time as fold-ins of an inbox drop
and a pasted link. Looks for stacking, starvation and any false 'current'."""
import json
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import h  # noqa: E402

S = Path("/tmp/claude-0/-home-user-Claudevideo/4633d594-0139-5f8e-a935-ae9c76b72000/scratchpad")
PORT, FPORT = 8478, 8479
D = h.ROOT / "J"


def toc(web, names):
    doc = {"reporting_entity_name": "Juliet Health", "reporting_entity_type": "issuer",
           "reporting_structure": [{
               "reporting_plans": [{"plan_name": "Juliet PPO", "plan_id_type": "EIN",
                                    "plan_id": "430000001", "plan_market_type": "group"}],
               "in_network_files": [{"description": n, "location": f"http://127.0.0.1:{FPORT}/{n}"}
                                    for n in names]}]}
    (web / "juliet_index.json").write_text(json.dumps(doc))


def main():
    if D.exists():
        shutil.rmtree(D)
    shutil.copytree(h.ROOT / "bases" / "base7_built", D)
    web = D / "web"
    web.mkdir()
    kids = [f"juliet_2026-11_part{i}.json.gz" for i in range(3)]
    for i, n in enumerate(kids):
        h.gen_mrf(web / n, "Juliet Health", 30000 + i * 1000, 150, seed=200 + i, rate_base=90)
    toc(web, kids)
    fs = h.FileServer(web, FPORT)
    res = {}
    # ---- phase 1: the index is loaded once (no re-check, no names)
    cfgp = h.write_cfg(D, PORT)
    srv = h.Serve(cfgp, PORT, D / "serve_1.log")
    srv.wait_ready()
    h.add_links(PORT, [fs.url("juliet_index.json")])
    t = time.time()
    while time.time() - t < 300:
        a = h.api(PORT, "/api/stats").get("analytics") or {}
        if a.get("done_files") == 10 and a.get("state") == "current":
            break
        time.sleep(1)
    res["phase1"] = a
    srv.stop()
    # ---- phase 2: the payer changed one child; names + re-check + 2 new files
    h.gen_mrf(web / kids[1], "Juliet Health", 31000, 150, seed=999, rate_base=300)
    staged = D / "staged"
    h.gen_mrf(staged / "kilo_rates.json.gz", "Kilo Benefit Trust", 40000, 120, seed=11, rate_base=55)
    h.gen_mrf(web / "lima_rates.json.gz", "Lima Care", 41000, 90, seed=12, rate_base=65)
    cfgp = h.write_cfg(D, PORT, monthly_recheck=True,
                       enrichment={"mode": "bulk", "auto_download": False,
                                   "nppes_dir": str(S / "nppes_live" / "nppes")})
    evp = D / "events.jsonl"
    env = {"PYTHONPATH": str(h.ROOT / "hooks"), "MRFX_AUDIT_HOOKS": "1",
           "MRFX_AUDIT_EVENTS": str(evp), "MRFX_AUDIT_DIR_SLEEP": "30"}
    srv = h.Serve(cfgp, PORT, D / "serve_2.log", env_extra=env)
    srv.wait_ready()
    T0 = time.time()
    poll = h.Poller(PORT, every=0.5, light=False)
    poll.start()
    time.sleep(20)                       # let names + the re-check get going
    shutil.copy(staged / "kilo_rates.json.gz", D / "inbox" / ".t_kilo")
    (D / "inbox" / ".t_kilo").rename(D / "inbox" / "kilo_rates.json.gz")
    t_kilo = time.time() - T0
    h.add_links(PORT, [fs.url("lima_rates.json.gz")])
    t_lima = time.time() - T0
    # wait: everything done AND covered AND names finished (or 20 min)
    end = None
    enr_last = None
    while time.time() - T0 < 1200:
        try:
            st = h.api(PORT, "/api/stats", timeout=60)
        except Exception:  # noqa: BLE001
            time.sleep(1)
            continue
        a = st.get("analytics") or {}
        enr_last = st.get("enrichment")
        q = h.api(PORT, "/api/urls", timeout=60).get("counts") or {}
        busy = any(q.get(k) for k in ("queued", "downloading", "fetched", "ingesting", "expanding"))
        if (a.get("state") == "current" and a.get("done_files") == a.get("covered_files")
                and (a.get("done_files") or 0) >= 12 and not busy and time.time() - T0 > 240):
            end = time.time() - T0
            break
        time.sleep(1)
    time.sleep(2)
    poll.stop()
    res["kilo_dropped_s"], res["lima_pasted_s"], res["all_current_s"] = round(t_kilo, 1), round(t_lima, 1), end and round(end, 1)
    res["enrichment_last"] = enr_last
    res["timeline"] = h.states_timeline(poll.rows, T0)
    gens = []
    for r in poll.rows:
        if r.get("gen") is not None and (not gens or gens[-1][1] != r["gen"]):
            gens.append((round(r["t"] - T0, 1), r["gen"]))
    res["generation_bumps"] = gens
    res["false_current"] = [(round(r["t"] - T0, 1), r["a"].get("covered_files"), r["a"].get("done_files"))
                            for r in poll.rows if (r.get("a") or {}).get("state") == "current"
                            and r["a"].get("covered_files") != r["a"].get("done_files")]
    ev = [json.loads(x) for x in evp.read_text().splitlines() if x.strip()] if evp.exists() else []
    res["events"] = [(round(e["t"] - T0, 1), e["ev"], e.get("ret"), e.get("took")) for e in ev]
    # overlap check: rollup kinds entering while another is inside
    inside, overlaps = 0, []
    for e in ev:
        if e["ev"] in ("incr_enter", "full_enter", "dir_enter"):
            inside += 1
            if inside > 1:
                overlaps.append((round(e["t"] - T0, 1), e["ev"]))
        elif e["ev"] in ("incr_exit", "full_exit", "dir_exit", "incr_raise", "full_raise"):
            inside -= 1
    res["concurrent_entries"] = overlaps
    srv.stop()
    fs.close()
    res["log"] = h.grep_log(D / "serve_2.log", "analytics", "rollup", "name", "skipp", "monthly", "re-check",
                            "recheck", "same name", "replac")
    res["files"] = None
    res["truth"] = h.short_truth(h.run_truth_subprocess(D / "store", hash_rows=False))
    h.save("j.json", res)
    print(json.dumps({k: res[k] for k in ("kilo_dropped_s", "lima_pasted_s", "all_current_s", "false_current",
                                          "concurrent_entries", "truth", "enrichment_last")}, default=str, indent=1)[:4000])
    shutil.rmtree(D / "store", ignore_errors=True)


if __name__ == "__main__":
    main()
