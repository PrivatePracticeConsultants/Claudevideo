"""1427445: the 'done' commit carries the read identity — but only the
same-name check reads it. A kill between the 'done' commit and the identity
row (the analytics update runs in that window), then a copy of the same bytes
under another name (a browser's 'name (1).json'): loaded twice?"""
import json, shutil, sys
from pathlib import Path
D = Path(__file__).resolve().parent.parent
sys.path.insert(0, "/home/user/Claudevideo")

if __name__ == "__main__":
    import logging; logging.disable(logging.INFO)
    import gzip
    from mrfx.config import MrfxConfig
    from mrfx.store import Store
    from mrfx.ingest import scan_inbox
    from tests.mrfx.test_review11 import rate_doc
    for variant in ("byte-identical copy", "decoded copy"):
        work = D / "w_s10"; shutil.rmtree(work, ignore_errors=True); work.mkdir()
        cfg = MrfxConfig(inbox_dir=work / "inbox", processed_dir=work / "p", failed_dir=work / "f",
                         store_dir=work / "store", downloads_dir=work / "dl", entity_map_path=work / "em.yaml")
        cfg.ensure_dirs()
        s = Store(cfg.store_dir)
        raw = gzip.compress(rate_doc(n=60), mtime=0)
        (cfg.inbox_dir / "k.json.gz").write_bytes(raw)
        scan_inbox(cfg, s)
        with s.connect() as con:          # the state a kill after the 'done' commit leaves
            ri = con.execute("SELECT read_identity FROM files").fetchone()[0]
            con.execute("DELETE FROM content_fingerprints")
        twin = raw if variant.startswith("byte") else rate_doc(n=60)
        (cfg.inbox_dir / ("k (1).json.gz" if variant.startswith("byte") else "k (1).json")).write_bytes(twin)
        scan_inbox(cfg, s)
        with s.connect() as con:
            print(f"{variant}: read_identity={ri[:60]}... files={con.execute('SELECT filename, status FROM files ORDER BY 1').fetchall()} "
                  f"rate rows={con.execute('SELECT count(*) FROM rates').fetchone()[0]} (one document = 60)")
