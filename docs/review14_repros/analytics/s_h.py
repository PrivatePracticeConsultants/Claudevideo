"""Scenario H: the upgrade the user takes today. A 0.7.6 install (git archive
c8b80fc, its own venv) holding 8 real files of which 0.7.6's rollup covered
only 3 (its rollup made to fail for the rest -> 0.7.6 'gives up for this
run'), then `python -m mrfx update <0.7.7 zip>`, then start serve: does the
first start fold in everything, does the header go behind -> building ->
current, and do the pickers end with all payers covered?"""
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import h  # noqa: E402
from mk_base import FILES, LATE  # noqa: E402

S = Path("/tmp/claude-0/-home-user-Claudevideo/4633d594-0139-5f8e-a935-ae9c76b72000/scratchpad")
ZIP = S / "mrfx_project.zip"
PORT, FPORT = 8490, 8491
D = h.ROOT / "H"
INST = D / "install" / "Claudevideo"
VPY = str(INST / ".venv" / "bin" / "python")
MAIN_SITE = "/home/user/Claudevideo/.venv/lib/python3.13/site-packages"


def sh(cmd, **kw):
    r = subprocess.run(cmd, capture_output=True, text=True, **kw)
    return r.returncode, (r.stdout + r.stderr)[-4000:]


def setup():
    if D.exists():
        shutil.rmtree(D)
    (D / "install").mkdir(parents=True)
    arc = D / "old076.zip"
    sh(["git", "-C", "/home/user/Claudevideo", "archive", "--format=zip", "--prefix=Claudevideo/",
        "-o", str(arc), "c8b80fc"])
    sh(["unzip", "-q", str(arc), "-d", str(D / "install")])
    rc, out = sh([sys.executable, "-m", "venv", str(INST / ".venv")])
    assert rc == 0, out
    # the same pinned packages (requirements.txt is identical at c8b80fc and
    # 9617c59): shared read-only through a .pth instead of a 500 MB copy
    site = INST / ".venv" / "lib" / "python3.13" / "site-packages"
    (site / "zz_shared_deps.pth").write_text(MAIN_SITE + "\n")
    rc, out = sh([VPY, "-m", "pip", "install", "-q", "--no-build-isolation", "--no-deps", "-e", "."],
                 cwd=str(INST))
    rc2, ver = sh([VPY, "-c", "import mrfx,sys; print(mrfx.__version__, mrfx.__file__)"], cwd="/")
    import yaml
    cp = INST / "config" / "mrfx.yaml"
    cfg = yaml.safe_load(cp.read_text())
    cfg.update({"port": PORT, "monthly_recheck": False, "parallel_ingests": 2,
                "duckdb_memory_gb": 2, "render_js": False})
    cfg.setdefault("enrichment", {})["mode"] = "off"
    cp.write_text(yaml.safe_dump(cfg))
    return {"pip_e": (rc, out[-300:]), "version": ver.strip()}


def stats(port):
    try:
        return h.api(port, "/api/stats", timeout=30)
    except Exception as e:  # noqa: BLE001
        return {"err": repr(e)}


def main():
    res = {"setup": setup()}
    print(res["setup"], flush=True)
    cfgp = INST / "config" / "mrfx.yaml"
    evp = D / "events076.jsonl"
    flag = D / "FAIL"
    env = {"PYTHONPATH": str(h.ROOT / "hooks076"), "MRFX_AUDIT_HOOKS": "1",
           "MRFX_AUDIT_EVENTS": str(evp), "MRFX_AUDIT_FAIL_FLAG": str(flag)}
    # ---- 0.7.6 serve: 3 files in through the inbox, rolled up
    srv = h.Serve(cfgp, PORT, D / "serve076.log", python=VPY, cwd=INST, env_extra=env)
    srv.wait_ready()
    inbox = INST / "data" / "inbox"
    first = [FILES[2], FILES[5], FILES[3]]     # moda, highmark wv, tufts
    for f in first:
        shutil.copy(f, inbox / (".t" + f.name))
        (inbox / (".t" + f.name)).rename(inbox / f.name)
    t = time.time()
    while time.time() - t < 300:
        st = stats(PORT)
        if st.get("files_done") == 3 or (isinstance(st.get("files_done"), dict)):
            break
        time.sleep(1)
    time.sleep(8)
    res["076_after_first"] = {k: stats(PORT).get(k) for k in ("files_done", "payers", "rates", "tins")}
    res["076_payers_first"] = h.api(PORT, "/api/payers")
    # ---- then the rollup starts failing; 5 more files arrive as LINKS
    flag.write_text("1")
    web = D / "web"
    web.mkdir()
    rest = [FILES[0], FILES[1], FILES[4], FILES[6], LATE]
    for f in rest:
        shutil.copy(f, web / f.name)
    fs = h.FileServer(web, FPORT)
    h.add_links(PORT, [fs.url(f.name) for f in rest])
    t = time.time()
    while time.time() - t < 900:
        q = h.api(PORT, "/api/urls", timeout=30).get("counts") or {}
        if q.get("done", 0) >= 5 and not any(q.get(k) for k in ("queued", "downloading", "fetched", "ingesting")):
            break
        time.sleep(2)
    # let 0.7.6 try (and fail) its rollups, as on the user's machine
    t = time.time()
    while time.time() - t < 120:
        if "giving up" in (D / "serve076.log").read_text(errors="replace"):
            break
        time.sleep(2)
    res["076_queue"] = h.api(PORT, "/api/urls", timeout=30).get("counts")
    res["076_payers_end"] = h.api(PORT, "/api/payers")
    res["076_stats_end"] = {k: stats(PORT).get(k) for k in ("files_done", "payers", "rates")}
    srv.stop()
    fs.close()
    flag.unlink()
    res["076_log"] = h.grep_log(D / "serve076.log", "rollup", "analytics", "giving up")
    t076 = h.run_truth_subprocess(INST / "data" / "mrfx_store", hash_rows=False)
    res["076_truth"] = h.short_truth(t076) | {"meta": t076.get("meta")}
    print("076 state:", json.dumps(res["076_truth"], default=str)[:800], flush=True)
    # ---- the update
    t = time.time()
    rc, out = sh([VPY, "-m", "mrfx", "update", str(ZIP)], cwd=str(INST))
    res["update"] = {"rc": rc, "out": out[-2500:], "s": round(time.time() - t, 1)}
    rc2, ver = sh([VPY, "-c", "import mrfx; print(mrfx.__version__, mrfx.__file__)"], cwd="/")
    res["version_after"] = ver.strip()
    print("update rc", rc, ver, flush=True)
    # ---- first 0.7.7 start: no new activity
    srv = h.Serve(cfgp, PORT, D / "serve077.log", python=VPY, cwd=INST)
    T0 = time.time()
    first_answer = None
    while time.time() - T0 < 300:
        try:
            b = h.api(PORT, "/api/stats", timeout=10)
            first_answer = {"dt": round(time.time() - T0, 1), "analytics": b.get("analytics")}
            break
        except Exception:  # noqa: BLE001
            time.sleep(0.2)
    poll = h.Poller(PORT, every=0.5, light=False)
    poll.start()
    t = time.time()
    while time.time() - t < 900:
        a = (poll.rows[-1].get("a") if poll.rows else None) or {}
        if a.get("state") == "current" and a.get("covered_files") == a.get("done_files") == 8:
            break
        time.sleep(0.5)
    time.sleep(2)
    poll.stop()
    res["077_first_answer"] = first_answer
    res["077_timeline"] = h.states_timeline(poll.rows, T0)
    res["077_payers"] = h.api(PORT, "/api/payers")
    res["077_code_97110"] = h.snap(PORT).get("code_payer_rank")
    res["077_to_current_s"] = round(time.time() - T0, 1)
    srv.stop()
    res["077_log"] = h.grep_log(D / "serve077.log", "rollup", "analytics", "catch", "rows", "update")
    res["077_truth"] = h.short_truth(h.run_truth_subprocess(INST / "data" / "mrfx_store", hash_rows=False))
    h.save("h.json", res)
    print(json.dumps({k: res[k] for k in ("077_first_answer", "077_timeline", "077_to_current_s", "077_truth")},
                     default=str, indent=1)[:5000])


if __name__ == "__main__":
    main()
