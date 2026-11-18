"""Case 10 — kill tests. For each trial: fresh scratch store; spawn c10_driver
(run_queue drain=True, 2 downloader threads, in-process ingest) against a
throttled local server serving two ~6 MB gz rate files; proc.kill() at a
random instant (seed logged); restart the driver and let it drain; assert:
no row left 'downloading'/'ingesting', both rows done exactly once, content
sha == truth, rates count == 600, and report the .part size at the kill,
the Range offsets after the restart, and the bytes re-downloaded."""
import random
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _harness import ROOT, Srv, doc, gz, mk_cfg, sha, q, rows  # noqa: E402

PY = "/home/user/Claudevideo/.venv/bin/python"
DRIVER = str(Path(__file__).parent / "c10_driver.py")


def main(trials=12, seed=None, lo=0.3, hi=9.0):
    seed = seed if seed is not None else int(time.time()) % 100000
    rnd = random.Random(seed)
    print(f"[kill] seed={seed} trials={trials}")
    fa = gz(doc(n=300, pad_mb=7.0, code="97110", seed=1, payer="Kill A"))
    fb = gz(doc(n=300, pad_mb=7.0, code="97140", seed=2, payer="Kill B"))
    truth = {"/a/r.json.gz": fa, "/b/r.json.gz": fb}
    srv = Srv(truth, {p: {"delay": 0.07, "etag": f'"{p[1]}"'} for p in truth})   # ~0.9 MB/s per file
    urls = [srv.base + p for p in truth]
    bad = 0
    try:
        for t in range(trials):
            tag = f"c10_t{t}"
            cfg, store = mk_cfg(tag)             # fresh store
            del store
            root = str(ROOT / "runs" / tag)
            n0 = len(srv.log)
            kill_at = rnd.uniform(lo, hi)
            p = subprocess.Popen([PY, DRIVER, root] + urls, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL)
            time.sleep(kill_at)
            # snapshot what was on disk at the kill instant (approximate: read right after)
            p.kill(); p.wait()
            parts = sorted((f.name, f.stat().st_size) for f in cfg.downloads_dir.iterdir())
            try:
                from mrfx.store import Store
                st = Store(cfg.store_dir)
                at_kill = [(r["status"], r["bytes_done"]) for r in rows(st)]
                del st
            except Exception as e:  # noqa: BLE001
                at_kill = f"store unreadable: {type(e).__name__}"
            n1 = len(srv.log)
            p2 = subprocess.run([PY, DRIVER, root] + urls, capture_output=True, text=True, timeout=300)
            st = Store(cfg.store_dir)
            final = rows(st)
            statuses = [(r["status"], r["kind"]) for r in final]
            shas_ok = all(r["content_sha"] == sha(truth["/" + r["url"].split("/", 3)[3]]) for r in final
                          if r["status"] == "done")
            nrates = q(st, "SELECT count(*) FROM rates")[0][0]
            nfiles = q(st, "SELECT filename, status FROM files")
            with srv.lock:
                after = [(x["path"][1], x["status"], x["range"] or "-", x["sent"]) for x in srv.log[n1:]]
                before = [(x["path"][1], x["status"], x["range"] or "-", x["sent"]) for x in srv.log[n0:n1]]
            sent_total = sum(x[3] for x in before + after)
            redl = sent_total - sum(len(v) for v in truth.values())
            ok = (all(s == "done" for s, _ in statuses) and len(statuses) == 2 and shas_ok and nrates == 600
                  and len(nfiles) == 2 and "DRIVER_DONE" in p2.stdout)
            bad += 0 if ok else 1
            print(f"  trial {t:2d} kill@{kill_at:4.1f}s | at kill: rows {at_kill} parts {parts}")
            print(f"           before kill: {before}")
            print(f"           after restart: {after}")
            print(f"           final: {statuses} sha_ok={shas_ok} rates={nrates} files={nfiles} "
                  f"re-downloaded {redl / 1e6:.2f} MB -> {'OK' if ok else 'FAIL'}")
            if not ok:
                print("           driver stderr tail:", p2.stderr[-800:])
            import shutil
            shutil.rmtree(root, ignore_errors=True)
    finally:
        srv.close()
    print(f"[kill] {trials - bad}/{trials} trials OK")


if __name__ == "__main__":
    main(trials=int(sys.argv[1]) if len(sys.argv) > 1 else 12,
         seed=int(sys.argv[2]) if len(sys.argv) > 2 else None,
         lo=float(sys.argv[3]) if len(sys.argv) > 3 else 0.3,
         hi=float(sys.argv[4]) if len(sys.argv) > 4 else 9.0)
