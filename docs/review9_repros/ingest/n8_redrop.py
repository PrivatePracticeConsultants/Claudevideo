"""A DIFFERENT file is dropped under the same name while the first is still
being parsed. (i) replaced by rename (copy-to-temp + rename, rsync, most
download tools); (ii) overwritten in place."""
import os, sys, threading, time
from h import *
import mrfx.ingest as ing
from mrfx.ingest import scan_inbox

def doc(rate, n=2000):
    return {**HDR, "in_network": [item("97110", groups=[grp([str(1000000000 + i)], f"{100000000+i}")], rate=rate)
                                  for i in range(n)]}

def run(mode, move=True):
    cfg = mkcfg(f"n8_{mode}_{move}", move_processed=move); st = Store(cfg.store_dir)
    p = put(cfg, "acme_2026-05.json", doc(40.0))
    v2 = json.dumps(doc(55.0, n=2100)).encode()
    real_open = ing.open_stream
    started = threading.Event()
    def slow_open(path, progress_cb=None):
        s = real_open(path, progress_cb=progress_cb)
        if Path(path).name == "acme_2026-05.json" and not started.is_set():
            s.peek(1)                 # the parse has the file open and has begun reading
            started.set(); time.sleep(4)
        return s
    ing.open_stream = slow_open
    try:
        out = {}
        t = threading.Thread(target=lambda: out.setdefault("r", scan_inbox(cfg, st)))
        t.start(); started.wait(20); time.sleep(0.5)
        if mode == "rename":
            tmp = cfg.inbox_dir / "acme_2026-05.json.tmp"; tmp.write_bytes(v2); os.replace(tmp, p)
        else:
            with open(p, "r+b") as f:
                f.truncate(0); f.write(v2)
        r2 = scan_inbox(cfg, st)      # the watcher's scan for the drop event
        print(f"[{mode}, move_processed={move}] scan during the parse ->", r2)
        t.join()
        print("   first scan ->", [(x["file"], x["status"], x.get("rows"), (x.get("error") or "")[:60]) for x in out["r"]])
    finally:
        ing.open_stream = real_open
    r3 = scan_inbox(cfg, st)
    print("   scan after it finished ->", [(x["file"], x["status"], x.get("rows")) for x in r3])
    print("   files:", q(st, "SELECT filename, status, rows_emitted FROM files ORDER BY 1"))
    print("   rates by rate:", q(st, "SELECT negotiated_rate, count(*) FROM rates GROUP BY 1 ORDER BY 1"),
          "(the NEW file is 2100 rows at 55.0; the old one 2000 at 40.0)")
    for d in ("inbox", "processed", "failed"):
        dd = getattr(cfg, d + "_dir")
        print(f"   {d}:", [(x.name, "NEW (55.0)" if b"55.0" in x.read_bytes()[:400000] else "old (40.0)") for x in dd.iterdir()])
    if mode == "rename" and move:
        (cfg.inbox_dir / "acme_2026-05.json").write_bytes(v2)
        print("   the user notices and drops the NEW file again -> scan:", scan_inbox(cfg, st),
              "| rows at 55.0:", q(st, "SELECT count(*) FROM rates WHERE negotiated_rate = 55.0")[0][0], "(2100 expected)",
              "| inbox now:", [x.name for x in cfg.inbox_dir.iterdir()])
    fp = st.loaded_fingerprint("acme_2026-05.json")
    print("   loaded fingerprint size:", fp and fp["size"], "| old file size:", len(json.dumps(doc(40.0)).encode()), "| new file size:", len(v2))

if __name__ == "__main__":
    run("rename")
    if "all" in sys.argv: run("inplace"); run("rename", move=False)
