"""Case 8b — a stale .fetchmeta with the WRONG sha next to a complete plain
.json (no early gz skip possible): is the sha trusted, and is a later true
copy of the same bytes then ingested a SECOND time?"""
import json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
from _harness import Srv, doc, mk_cfg, quiet_logs, row, q, sha, brief
from mrfx.fetch import add_urls, filename_for, run_queue, _meta_path

if __name__ == "__main__":
    quiet_logs(level=60)
    body = doc(n=300, pad_mb=0.5)
    cfg, store = mk_cfg("c08b_wrong_sidecar_json")
    srv = Srv({"/a/f.json": body, "/b/f.json": body}, {})
    ua, ub = srv.base + "/a/f.json", srv.base + "/b/f.json"
    dest = cfg.downloads_dir / filename_for(ua)
    dest.write_bytes(body)
    _meta_path(dest).write_text(json.dumps({"sha": "0" * 64, "final_url": ua, "trailer_proves": None}))
    try:
        add_urls(store, [ua]); run_queue(cfg, store, drain=True)
        ra = row(store, ua)
        print(f"[wrong_sidecar_json] A: {brief(ra)} | GETs to A: {len(srv.hits('/a/f.json'))} | sha==truth {ra['content_sha'] == sha(body)}")
        add_urls(store, [ub]); run_queue(cfg, store, drain=True)
        rb = row(store, ub)
        print(f"    B (byte-identical to A): {brief(rb)}")
        print(f"    rates rows: {q(store, 'SELECT count(*) FROM rates')[0][0]} (300 if deduped, 600 if loaded twice) | "
              f"files: {q(store, 'SELECT filename, status, rows_emitted FROM files')}")
    finally:
        srv.close()
    import shutil; shutil.rmtree(cfg.store_dir.parent, ignore_errors=True)
