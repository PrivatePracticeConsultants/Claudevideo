"""G1b: while the injected-OOM failure holds, compare the header's payer count
with /api/payers' covered list and the actual spine (via /api/rates per payer)."""
import json, shutil, sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent)); import h
from mk_base import LATE
if __name__ == "__main__":
    d = h.ROOT / "G1b"
    if d.exists(): shutil.rmtree(d)
    shutil.copytree(h.ROOT / "bases" / "base7_built", d)
    cfgp = h.write_cfg(d, 8485)
    web = d / "web"; web.mkdir(); shutil.copy(LATE, web / LATE.name)
    fs = h.FileServer(web, 8486)
    flag = d / "FAIL"; flag.write_text("1")
    env = {"PYTHONPATH": str(h.ROOT / "hooks"), "MRFX_AUDIT_HOOKS": "1", "MRFX_AUDIT_FAIL_FLAG": str(flag),
           "MRFX_AUDIT_EVENTS": str(d / "ev.jsonl")}
    srv = h.Serve(cfgp, 8485, d / "serve.log", env_extra=env); srv.wait_ready()
    before = h.snap(8485, ["Moda", "Blue Cross Blue Shield of North Dakota"])
    h.add_links(8485, [fs.url(LATE.name)])
    t = time.time()
    while time.time() - t < 120 and (h.api(8485, "/api/stats").get("analytics") or {}).get("state") != "failed":
        time.sleep(0.5)
    time.sleep(4)
    s = h.snap(8485, ["Moda", "Blue Cross Blue Shield of North Dakota"])
    from mrfx.store import Store  # noqa (not opened: server owns the DB)
    files = h.api(8485, "/api/files").get("analytics")
    out = {"before": {k: before.get(k) for k in ("rates_total", "rates_by_payer", "covered")},
           "during_failure": {"analytics": s.get("analytics"), "payers_api_covered": s.get("covered"),
                              "payers_api_loaded": s.get("payers"), "rates_total": s.get("rates_total"),
                              "rates_by_payer": s.get("rates_by_payer"), "code_payer_rank": s.get("code_payer_rank")},
           "files_tab_analytics": files,
           "export_note": None}
    # what a document prints right now: the methodology note in a CSV sidecar
    try:
        import httpx
        r = httpx.get("http://127.0.0.1:8485/api/export.zip?grain=tin&payer=Moda", timeout=60, trust_env=False)
        import io, zipfile
        z = zipfile.ZipFile(io.BytesIO(r.content))
        meth = [n for n in z.namelist() if "method" in n.lower()]
        txt = z.read(meth[0]).decode() if meth else ""
        out["export_note"] = [ln for ln in txt.splitlines() if "Analytics cover" in ln]
        out["export_files"] = z.namelist()
    except Exception as e:  # noqa
        out["export_note"] = repr(e)[:300]
    srv.kill(); fs.close()
    h.save("g1b.json", out)
    print(json.dumps(out, indent=1, default=str)[:4000])
    shutil.rmtree(d / "store", ignore_errors=True)
