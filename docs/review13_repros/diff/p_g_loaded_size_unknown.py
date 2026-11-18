"""(G) A child loaded by 0.7.4 (no server_validator / server_size) behind a
server that answers the 1-byte probe with a chunked 200 and NO size (ignores
Range, no Content-Length, no validator). The payer then republishes the file.
0.7.5: the monthly refresh re-downloaded it ("nothing was recorded to tell").
0.7.6: loaded_size exists, the probe gives no size -> _republished None -> is
the row still sent to the monthly re-download, or left at the old month?"""
import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, "/home/user/Claudevideo")
from mrfx import fetch  # noqa: E402
from mrfx.config import MrfxConfig  # noqa: E402
from mrfx.store import Store  # noqa: E402
from tests.mrfx.test_review11 import Srv, drain, gz, q, ranged, rate_doc  # noqa: E402

ROOT = Path(__file__).parent / "scratch_pg"


def chunked_no_size(body):
    def route(req):
        # a server that ignores Range and streams without a length or validator
        return 200, {"Transfer-Encoding": "chunked"}, body, None
    return route


def main():
    if ROOT.exists():
        shutil.rmtree(ROOT)
    cfg = MrfxConfig(inbox_dir=ROOT / "inbox", processed_dir=ROOT / "processed",
                     failed_dir=ROOT / "failed", store_dir=ROOT / "store",
                     downloads_dir=ROOT / "downloads", entity_map_path=ROOT / "em.yaml")
    cfg.enrichment.mode = "off"
    cfg.ensure_dirs()
    store = Store(cfg.store_dir)
    child = {"b": gz(rate_doc(month="2026-05-01"))}
    s = Srv({})
    # first load: a normal server (0.7.4 had no probe; the row is reset below)
    s.routes["/files/r.json.gz"] = lambda req: ranged(child["b"], req, etag='"v1"')
    idx = json.dumps({"reporting_entity_name": "Twin Health", "reporting_structure": [
        {"reporting_plans": [{"plan_name": "P"}],
         "in_network_files": [{"location": s.base + "/files/r.json.gz"}]}]}).encode()
    s.routes["/index.json"] = lambda req: ranged(idx, req)
    fetch.add_urls(store, [s.base + "/index.json"])
    drain(cfg, store)
    with store.write_lock, store.connect() as con:      # what a 0.7.4 load left behind
        con.execute("UPDATE url_queue SET server_validator = NULL, server_size = NULL "
                    "WHERE kind = 'in_network'")
    print("loaded:", q(store, "SELECT DISTINCT file_month FROM rates"),
          "loaded_size:", q(store, "SELECT size FROM content_fingerprints"))
    # the payer republishes; the server now answers chunked with no size / validator
    child["b"] = gz(rate_doc(rate=45.0, n=80, month="2026-06-01"))
    s.routes["/files/r.json.gz"] = chunked_no_size(child["b"])
    before = len(s.log)
    store.requeue_urls([s.base + "/index.json"])
    with store.write_lock, store.connect() as con:      # the monthly refresh
        con.execute("UPDATE url_queue SET revalidate = 1 WHERE id = 1")
    drain(cfg, store)
    print("requests:", [(h[0], h[1], h[2]) for h in s.log[before:]])
    rows = q(store, "SELECT id, status, kind, error FROM url_queue ORDER BY id")
    for r in rows:
        print("  ", r)
    months = q(store, "SELECT DISTINCT file_month FROM rates")
    print("file months now:", months)
    print("VERDICT:", "re-downloaded (0.7.5 behaviour)" if months == [("2026-06",)]
          else "LEFT AT THE OLD MONTH — never re-downloaded, no note on the row")
    s.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
