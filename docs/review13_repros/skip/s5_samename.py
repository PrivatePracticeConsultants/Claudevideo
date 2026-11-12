"""S5: same_name_verdict / hold_for_choice.
 A. the SAME file re-dropped (a copy: new mtime) -> 'same' by sha, skipped
 B. a corrected file of the SAME size under a loaded name -> held (needs_choice)
 C. another month under the loaded name -> loaded beside under a content name
 D. a file loaded BEFORE audit 8 (no content_fingerprints row) whose processed
    copy is gone: a different same-size file with an OLDER mtime -> ?
 E. .held after a 'restart' (a second scan loads the chosen file)
 F. "Keep both" naming
 G. what the user is told for a pre-audit-8 file (nothing marks it)"""
import logging
import os
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import gen  # noqa: E402
import h  # noqa: E402


def files(store):
    with store.connect() as con:
        return con.execute("SELECT filename, status, rows_emitted, substr(error,1,120) FROM files ORDER BY filename").fetchall()


def rates(store):
    with store.connect() as con:
        return con.execute("SELECT count(*), count(DISTINCT source_file), list(DISTINCT negotiated_rate ORDER BY negotiated_rate) FROM rates").fetchone()


def main():
    from mrfx.ingest import resolve_choice, scan_inbox
    logging.basicConfig(level=logging.ERROR)
    raw = gen.doc(n_items=100, filler=20, day="2026-05-01")
    raw_fix, _ = gen.change_rate_once(raw, 0)               # one rate 40 -> 47, same length
    raw_june = gen.doc(n_items=100, filler=20, day="2026-06-01", rate=45.0)
    v1, vfix, vjune = gen.gz0(raw), gen.gz0(raw_fix), gen.gz0(raw_june)
    assert len(v1) == len(vfix)
    cfg = h.scratch_cfg("s5", parallel_downloads=1, parallel_ingests=1)
    store = h.open_store(cfg)
    name = "acme_rates.json.gz"
    (cfg.inbox_dir / name).write_bytes(v1)
    scan_inbox(cfg, store)
    print("loaded:", files(store), rates(store))

    # A. the same bytes again (a copy: new mtime)
    (cfg.inbox_dir / name).write_bytes(v1)
    scan_inbox(cfg, store)
    print("\nA. same bytes re-dropped:", files(store), "inbox:", os.listdir(cfg.inbox_dir), "rates:", rates(store))

    # B. corrected same-size file under the loaded name
    (cfg.inbox_dir / name).write_bytes(vfix)
    r = scan_inbox(cfg, store)
    held = sorted(p.name for p in (cfg.inbox_dir / ".held").glob("*"))
    print("\nB. corrected same-size file:", r, "held:", held)
    for f in files(store):
        print("   ", f)
    # E. 'restart' before choosing: a plain scan must leave it held
    scan_inbox(cfg, store)
    print("   after another scan (restart): held still", sorted(p.name for p in (cfg.inbox_dir / ".held").glob("*")),
          [f for f in files(store) if f[1] == "needs_choice"])
    # choose Replace, then simulate a restart before the load: the scan picks up .replacing
    res = resolve_choice(cfg, store, held[0], "replace")
    print("   Replace chosen ->", res["path"].parent.name, res["filename"], "; row:", [f for f in files(store) if f[0] == name])
    scan_inbox(cfg, store)
    print("   after scan:", files(store), rates(store))

    # F. Keep both naming
    (cfg.inbox_dir / name).write_bytes(v1)                  # the original again: now 'different' from the replaced content
    scan_inbox(cfg, store)
    held = sorted(p.name for p in (cfg.inbox_dir / ".held").glob("*"))
    print("\nF. original re-dropped after Replace -> held:", held)
    if held:
        res = resolve_choice(cfg, store, held[0], "keep")
        scan_inbox(cfg, store)
        print("   Keep both ->", res["filename"], "; files:", files(store), rates(store))

    # C. another month under the loaded name
    (cfg.inbox_dir / name).write_bytes(vjune)
    scan_inbox(cfg, store)
    print("\nC. June under the May name:", files(store), rates(store))

    # D/G. a file loaded before audit 8: no content_fingerprints row, processed copy gone
    cfg2 = h.scratch_cfg("s5_D", parallel_downloads=1, parallel_ingests=1)
    store2 = h.open_store(cfg2)
    (cfg2.inbox_dir / name).write_bytes(v1)
    scan_inbox(cfg2, store2)
    with store2.write_lock, store2.connect() as con:
        con.execute("DELETE FROM content_fingerprints")
    for p in cfg2.processed_dir.glob("*"):
        p.unlink()
    fin = files(store2)[0]
    # a corrected same-size file whose mtime is OLDER than the load (cp -p from a backup / another PC)
    tgt = cfg2.inbox_dir / name
    tgt.write_bytes(vfix)
    old = time.time() - 30 * 86400
    os.utime(tgt, (old, old))
    scan_inbox(cfg2, store2)
    print("\nD. pre-audit-8 file (no identity row, processed copy gone); corrected same-size file with an OLD mtime dropped:")
    print("   files:", files(store2))
    print("   inbox now:", os.listdir(cfg2.inbox_dir), "processed:", os.listdir(cfg2.processed_dir))
    print("   rates:", rates(store2), " (47.0 present = the corrected file loaded; absent = silently treated as 'same')")
    # G. and with a NEW mtime
    tgt.write_bytes(vfix)
    scan_inbox(cfg2, store2)
    print("   same drop with a fresh mtime:", files(store2), "held:", sorted(p.name for p in (cfg2.inbox_dir / ".held").glob("*")) if (cfg2.inbox_dir / ".held").exists() else [])
    # what marks a pre-audit-8 file on the Files tab?
    from mrfx.api import create_app
    from fastapi.testclient import TestClient
    app = create_app(cfg2, store2)
    with TestClient(app) as c:
        js = c.get("/api/files").json()
        row = [r for r in (js.get("files") or js) if isinstance(r, dict) and r.get("filename") == name]
        print("   /api/files row keys mentioning identity/fingerprint:",
              [k for k in (row[0].keys() if row else []) if "sha" in k or "ident" in k or "finger" in k], "row error:", row[0].get("error") if row else None)
    shutil.rmtree(cfg2.inbox_dir / ".held", ignore_errors=True)


if __name__ == "__main__":
    main()
