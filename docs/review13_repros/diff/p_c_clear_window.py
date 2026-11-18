"""(C) "Clear queue" pressed in the window AFTER the children were re-queued by
requeue_republished and BEFORE the index's final 'done' write. Expect: index
skipped/canceled, children skipped/canceled (nothing re-downloaded), no row in
flight, and a second drain changes nothing."""
import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, "/home/user/Claudevideo")
from mrfx import fetch  # noqa: E402
from mrfx.config import MrfxConfig  # noqa: E402
from mrfx.store import Store  # noqa: E402
from tests.mrfx.test_review11 import Srv, drain, gz, q, ranged, rate_doc  # noqa: E402

ROOT = Path(__file__).parent / "scratch_pc"


def main():
    if ROOT.exists():
        shutil.rmtree(ROOT)
    cfg = MrfxConfig(inbox_dir=ROOT / "inbox", processed_dir=ROOT / "processed",
                     failed_dir=ROOT / "failed", store_dir=ROOT / "store",
                     downloads_dir=ROOT / "downloads", entity_map_path=ROOT / "em.yaml")
    cfg.enrichment.mode = "off"
    cfg.ensure_dirs()
    store = Store(cfg.store_dir)
    s = Srv({})
    bodies = {i: gz(rate_doc(n=3, payer=f"P{i}", month="2026-05-01")) for i in range(3)}
    etag = {"v": '"v1"'}
    for i in range(3):
        s.routes[f"/c{i}.json.gz"] = lambda req, i=i: ranged(bodies[i], req, etag=etag["v"])
    idx = json.dumps({"reporting_entity_name": "Twin Health", "reporting_structure": [
        {"reporting_plans": [{"plan_name": "P"}],
         "in_network_files": [{"location": f"{s.base}/c{i}.json.gz"} for i in range(3)]}]}).encode()
    s.routes["/idx.json"] = lambda req: ranged(idx, req)
    fetch.add_urls(store, [s.base + "/idx.json"])
    drain(cfg, store)
    print("after first drain:", q(store, "SELECT id, status, kind FROM url_queue ORDER BY id"))
    for i in range(3):
        bodies[i] = gz(rate_doc(n=5, payer=f"P{i}", month="2026-06-01"))
    etag["v"] = '"v2"'
    real = store.requeue_republished
    pressed = []

    def requeue_then_clear(ids, revalidate, reason, parent_id=None, urls=None):
        n = real(ids, revalidate, reason, parent_id=parent_id, urls=urls)
        if n and not pressed:
            pressed.append(store.clear_queued())    # the user presses "Clear queue" NOW
        return n
    store.requeue_republished = requeue_then_clear
    store.requeue_urls([s.base + "/idx.json"])
    drain(cfg, store)
    rows = q(store, "SELECT id, status, kind, error FROM url_queue ORDER BY id")
    print("cleared:", pressed)
    for r in rows:
        print("  ", r)
    months = q(store, "SELECT DISTINCT file_month FROM rates ORDER BY 1")
    print("file months:", months)
    hits = [(h[0], h[1], h[2]) for h in s.log if h[0].startswith("/c")]
    print("child requests after the clear (probes only?):", hits[-6:])
    store.requeue_republished = real
    drain(cfg, store)
    rows2 = q(store, "SELECT id, status, kind FROM url_queue ORDER BY id")
    print("after a second drain:", rows2)
    ok = (rows[0][1:3] == ("skipped", "canceled") and all(r[1] == "skipped" for r in rows)
          and months == [("2026-05",)] and rows2 == [r[:3] for r in rows])
    print("RESULT:", "held" if ok else "PROBLEM")
    s.close()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
