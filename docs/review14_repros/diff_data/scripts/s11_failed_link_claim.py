"""5.2.1: a link's CONTENT claim (Store._content_claims) is released only by
_finish_ok (success). A link whose ingest FAILS (here: one transient crash)
keeps the claim; afterwards (a) the same document re-encoded under another
link is skipped as 'being loaded right now' and (b) an inbox copy waits."""
import gzip, shutil, sys, threading, time
from pathlib import Path
D = Path(__file__).resolve().parent.parent
sys.path.insert(0, "/home/user/Claudevideo")

if __name__ == "__main__":
    import logging; logging.disable(logging.INFO)
    from mrfx import fetch
    import mrfx.ingest as ING
    from mrfx.config import MrfxConfig
    from mrfx.store import Store
    from tests.mrfx.test_review11 import Srv, drain, ranged, rate_doc
    work = D / "w_s11"; shutil.rmtree(work, ignore_errors=True); work.mkdir()
    cfg = MrfxConfig(inbox_dir=work / "inbox", processed_dir=work / "p", failed_dir=work / "f",
                     store_dir=work / "store", downloads_dir=work / "dl", entity_map_path=work / "em.yaml")
    cfg.ensure_dirs(); cfg.enrichment.mode = "off"
    s = Store(cfg.store_dir)
    body = rate_doc(n=60)
    a, b = gzip.compress(body, 1, mtime=0), gzip.compress(body, 9, mtime=0)
    srv = Srv({})
    real = ING._ingest_file_locked
    fired = {"n": 0}
    def flaky(*args, **kw):
        if fired["n"] == 0:
            fired["n"] += 1
            raise OSError("simulated transient failure (disk briefly unavailable)")
        return real(*args, **kw)
    ING._ingest_file_locked = flaky
    try:
        srv.routes["/a.json.gz"] = lambda req: ranged(a, req, etag='"a"')
        srv.routes["/b.json.gz"] = lambda req: ranged(b, req, etag='"b"')
        fetch.add_urls(s, [srv.base + "/a.json.gz"])
        drain(cfg, s)
        print("after link A failed: content claims held =", dict(s._content_claims))
        fetch.add_urls(s, [srv.base + "/b.json.gz"])
        drain(cfg, s)
    finally:
        srv.close()
    with s.connect() as con:
        print("queue:", con.execute("SELECT url, status, kind, error FROM url_queue ORDER BY id").fetchall())
        print("rate rows:", con.execute("SELECT count(*) FROM rates").fetchone()[0], "(the document = 60)")
    # (b) an inbox copy of the same content
    (cfg.inbox_dir / "copy.json").write_bytes(body)
    out = {}
    t = threading.Thread(target=lambda: out.setdefault("r", ING.scan_inbox(cfg, s)), daemon=True)
    t.start(); t.join(20)
    print("inbox copy: scan_inbox returned within 20 s:", not t.is_alive(), out.get("r"))
