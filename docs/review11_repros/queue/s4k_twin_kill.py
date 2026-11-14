"""Kill the process between claim_content_ingest (twin A won, twin B deferred)
and the end of A's ingest; restart; check both rows and the row count."""
import json
import subprocess
import time
from h import *  # noqa
from s5_crash import inspect, run_driver, PY, HERE

if __name__ == "__main__":
    twin = rate_file_bytes("tk", n_rows=2, pad=400_000)
    srv = Server({"/a/x.json.gz": {"body": twin, "piece": 64 << 10, "pace": 0.02},
                  "/b/x.json.gz": {"body": twin, "piece": 64 << 10, "pace": 0.02}})
    for variant in ("A", "B"):
        root = SCRATCH / f"s4k_{variant}"
        if root.exists():
            shutil.rmtree(root)
        root.mkdir(parents=True)
        (root / "kill_after_claim").write_text("1")
        p = run_driver(root, [srv.url("/a/x.json.gz"), srv.url("/b/x.json.gz")])
        t0 = time.monotonic()
        while not (root / "claimed").exists() and time.monotonic() - t0 < 60:
            time.sleep(0.05)
        if variant == "B":
            time.sleep(1.0)   # let the other twin reach its own claim (deferred) before the kill
        p.kill(); p.wait()
        st = inspect(root)
        print(f"[{variant}] after kill (claimed row {(root/'claimed').read_text() if (root/'claimed').exists() else '?'}):",
              [(r["id"], r["status"], r["kind"]) for r in st["queue"]], "files:", st["files"], "downloads:", st["downloads"])
        (root / "kill_after_claim").unlink()
        run_driver(root, (), wait=120)
        fin = inspect(root)
        print(f"[{variant}] after restart:", [(r["id"], r["status"], r["kind"], (r["error"] or "")[:60]) for r in fin["queue"]])
        print(f"[{variant}] rates:", fin["rates"], "files:", fin["files"], "downloads:", fin["downloads"])
    srv.stop()
