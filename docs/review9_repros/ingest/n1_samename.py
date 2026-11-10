"""same_as_loaded: a file of 1-15 MB that is NOT a whole number of MB, left in
the inbox (move_processed off) or re-dropped. Is the SAME file seen as the same?"""
import json, shutil, sys
from h import *
from mrfx.ingest import ingest_file, scan_inbox, same_as_loaded

def doc(n, rate=40.0):
    return {**HDR, "in_network": [item("97110", groups=[grp([str(1000000000 + i)], f"{100000000+i}")], rate=rate)
                                  for i in range(n)]}

def run(move, n, label):
    cfg = mkcfg(f"n1_{label}", move_processed=move)
    st = Store(cfg.store_dir)
    p = put(cfg, "acme_2026-05.json", doc(n))
    size = p.stat().st_size
    keep = p.read_bytes()
    r = scan_inbox(cfg, st)
    print(f"[{label}] size={size} bytes ({size/2**20:.3f} MiB) move_processed={move} first scan ->",
          [(x['file'], x['status'], x.get('rows')) for x in r])
    print("   loaded fingerprint head_bytes:", st.loaded_fingerprint("acme_2026-05.json")["head_bytes"])
    for i in range(3):
        if move:   # the user drops the very same file again
            (cfg.inbox_dir / "acme_2026-05.json").write_bytes(keep)
        r = scan_inbox(cfg, st)
        print(f"   scan {i+2} ->", [(x['file'], x['status'], x.get('rows')) for x in r])
    print("   files:", q(st, "SELECT filename, status, rows_emitted FROM files ORDER BY 1"))
    print("   rates rows:", q(st, "SELECT count(*), count(DISTINCT source_file) FROM rates")[0],
          "(one file's worth is", n, ")")
    print("   inbox:", sorted(x.name for x in cfg.inbox_dir.iterdir()))

if __name__ == "__main__":
    run(False, 600, "small_keep")      # < 1 MiB
    run(False, 7000, "mid_keep")       # ~2.x MiB, not a whole MiB
    run(True, 7000, "mid_redrop")
