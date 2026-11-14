"""Case 5b — Content-Encoding the client decodes (deflate) with a garbage body:
httpx raises DecodingError (a RequestError, NOT a TransportError). Through the
queue: what message does the row get?"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
from _harness import Srv, mk_cfg, quiet_logs, row, brief
from mrfx.fetch import add_urls, run_queue

if __name__ == "__main__":
    quiet_logs(level=60)
    cfg, store = mk_cfg("c05b_bad_deflate")
    srv = Srv({"/file.json": b"\x00garbage-not-deflate" * 1000},
              {"/file.json": {"headers": {"Content-Encoding": "deflate"}}})
    url = srv.base + "/file.json"
    try:
        add_urls(store, [url]); run_queue(cfg, store, drain=True)
    finally:
        srv.close()
    print(f"[bad_deflate] {brief(row(store, url))} | requests {len(srv.hits('/file.json'))}")
    import shutil; shutil.rmtree(cfg.store_dir.parent, ignore_errors=True)
