"""(F) The re-expanded index no longer lists one loaded child (the payer
removed it) and republishes another. Expect: the removed child's done row and
rates untouched; the republished one reloaded; the unchanged one left alone;
the stored links are the index's current ones."""
import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, "/home/user/Claudevideo")
from mrfx import fetch  # noqa: E402
from mrfx.config import MrfxConfig  # noqa: E402
from mrfx.store import Store  # noqa: E402
from tests.mrfx.test_review11 import Srv, drain, gz, q, ranged, rate_doc  # noqa: E402

ROOT = Path(__file__).parent / "scratch_pf"


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
    bodies = {i: gz(rate_doc(n=3 + i, payer=f"P{i}", month="2026-05-01")) for i in range(3)}
    etag = {i: '"v1"' for i in range(3)}
    sig = {"n": 1}
    for i in range(3):
        for n in (1, 2):
            s.routes[f"/c{i}.json.gz?sig=S{n}"] = lambda req, i=i: ranged(bodies[i], req, etag=etag[i])
    listing = {"ids": [0, 1, 2]}

    def index(req):
        return 200, {}, json.dumps({"reporting_entity_name": "Twin Health", "reporting_structure": [
            {"reporting_plans": [{"plan_name": "P"}],
             "in_network_files": [{"location": f"{s.base}/c{i}.json.gz?sig=S{sig['n']}"}
                                  for i in listing["ids"]]}]}).encode(), None
    s.routes["/idx.json"] = index
    fetch.add_urls(store, [s.base + "/idx.json"])
    drain(cfg, store)
    print("first drain:", q(store, "SELECT id, status, kind, rows_emitted FROM url_queue ORDER BY id"))
    # month 2: c2 removed from the listing, c0 republished, new signature on every link
    listing["ids"] = [0, 1]
    sig["n"] = 2
    bodies[0] = gz(rate_doc(n=9, payer="P0", month="2026-06-01"))
    etag[0] = '"v2"'
    before = len(s.log)
    store.requeue_urls([s.base + "/idx.json"])
    drain(cfg, store)
    rows = q(store, "SELECT id, status, kind, rows_emitted, url FROM url_queue ORDER BY id")
    for r in rows:
        print("  ", r)
    print("requests:", [(h[0], h[1], h[2]) for h in s.log[before:]])
    pm = q(store, "SELECT payer, file_month, count(*) FROM rates GROUP BY 1, 2 ORDER BY 1, 2")
    print("rates by payer/month:", pm)
    c2 = [r for r in rows if "/c2." in r[4]][0]
    c0 = [r for r in rows if "/c0." in r[4]][0]
    c1 = [r for r in rows if "/c1." in r[4]][0]
    ok = (c2[1] == "done" and c2[4].endswith("sig=S1") and ("P2", "2026-05", 5) in pm
          and c0[1] == "done" and c0[4].endswith("sig=S2") and ("P0", "2026-06", 9) in pm
          and c1[1] == "done" and c1[4].endswith("sig=S2") and ("P1", "2026-05", 4) in pm)
    print("RESULT:", "held" if ok else "PROBLEM")
    s.close()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
