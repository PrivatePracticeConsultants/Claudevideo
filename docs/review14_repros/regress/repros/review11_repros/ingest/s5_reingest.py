"""Scenario 5: re-ingest of a done file (force) is an atomic part replacement — a reader on
a second connection must never see 0 rows or 2x rows for that file during the swap; forget
then re-drop; forget while ingesting (in-process AND pooled)."""
import threading, random
from h import *
from mrfx.ingest import scan_inbox, ingest_file, forget_file, ParsePoolManager, set_parse_pool
import mrfx.ingest as ing
import mrfx.parser as par

if __name__ == "__main__":
    cfg = mkcfg("s5"); store = Store(cfg.store_dir)
    name = "acme_2026-05.json.gz"
    p = put(cfg, name, doc(n=400), gz=True)
    ingest_file(cfg, store, p); p = cfg.processed_dir / name
    print("v1 rows:", rate_counts(store))

    # reader on its own connection, hammering count(*) for this file during repeated re-ingests
    seen = {}
    stop = threading.Event()
    anomalies = []
    def reader():
        with store.connect() as con:
            while not stop.is_set():
                try:
                    n = con.execute("SELECT count(*) FROM rates WHERE source_file = ?", [name]).fetchone()[0]
                    seen[n] = seen.get(n, 0) + 1
                except Exception as e:  # a transient glob error is an anomaly too
                    anomalies.append(repr(e)[:120])
    t = threading.Thread(target=reader, daemon=True); t.start()
    for i in range(12):
        # alternate 400 / 500 row versions, same filename, different bytes -> force path (ingest_file directly)
        put(cfg, name, doc(n=400 if i % 2 == 0 else 500, seed=i), gz=True)
        r = ingest_file(cfg, store, cfg.inbox_dir / name)
        assert r["status"] == "done", r
    stop.set(); t.join()
    print("row counts a concurrent reader observed during 12 re-ingests:", dict(sorted(seen.items())))
    print("reader errors:", len(anomalies), anomalies[:3])
    print("parts on disk:", parts(store), "| rows now:", rate_counts(store), "| rollups_stale:", store.rollups_stale())

    # scan_inbox(force=True) over a processed/done file left in the inbox
    put(cfg, name, doc(n=7, seed=99), gz=True)
    print("scan force=True:", scan_inbox(cfg, store, force=True), "| rows:", rate_counts(store))
    put(cfg, name, doc(n=7, seed=99), gz=True)
    print("scan force=True again, identical bytes:", scan_inbox(cfg, store, force=True), "| rows:", rate_counts(store))

    print("\n== forget then re-drop")
    info = forget_file(cfg, store, name)
    print("forget:", info, "| files:", files_rows(store), "| rates:", rate_counts(store), "| parts:", parts(store),
          "| fingerprints:", q(store, "SELECT count(*) FROM content_fingerprints"))
    put(cfg, name, doc(n=7, seed=99), gz=True)
    print("re-drop same bytes after forget:", scan_inbox(cfg, store), "| rows:", rate_counts(store))
    print("rollups_stale:", store.rollups_stale())

    print("\n== forget while ingesting (in-process): sink sleeps, forget called from another thread")
    orig = par.InNetworkParser._flush
    gate = threading.Event()
    def slow_flush(self, final=False):
        gate.set(); time.sleep(1.5); return orig(self, final)
    par.InNetworkParser._flush = slow_flush
    put(cfg, name, doc(n=9, seed=5), gz=True)
    res = {}
    th = threading.Thread(target=lambda: res.update(r=ingest_file(cfg, store, cfg.inbox_dir / name))); th.start()
    gate.wait(10)
    try:
        print("forget during parse ->", forget_file(cfg, store, name))
    except RuntimeError as e:
        print("forget during parse -> refused:", e)
    th.join(); par.InNetworkParser._flush = orig
    print("ingest result:", res["r"]["status"], "| rows:", rate_counts(store), "| files:", [(f, s, n) for f, s, _, n, _ in files_rows(store)])

    print("\n== forget while a POOLED ingest runs (claim is per filename in the main process)")
    pool = ParsePoolManager(1); set_parse_pool(pool)
    try:
        put(cfg, name, doc(n=9, seed=6, pad_mb=2.0), gz=True)
        res = {}
        th = threading.Thread(target=lambda: res.update(r=ingest_file(cfg, store, cfg.inbox_dir / name))); th.start()
        t0 = time.time()
        while not ing.ingest_in_progress(name) and time.time() - t0 < 5: time.sleep(0.01)
        try:
            print("forget during pooled parse ->", forget_file(cfg, store, name))
        except RuntimeError as e:
            print("forget during pooled parse -> refused:", e)
        th.join()
        print("ingest result:", res["r"]["status"], "| rows:", rate_counts(store))
        # after the ingest finished, forget works and leaves nothing
        print("forget after:", forget_file(cfg, store, name), "| rows:", rate_counts(store), "| parts:", parts(store))
    finally:
        set_parse_pool(None); pool.shutdown()

    print("\n== re-ingest to 0 rows (same name, new bytes have no target codes) drops the old part")
    put(cfg, name, doc(n=3), gz=True); ingest_file(cfg, store, cfg.inbox_dir / name)
    put(cfg, name, {**HDR, "in_network": [item("99213", groups=[grp(["1234567893"], "123456789")])]}, gz=True)
    r = ingest_file(cfg, store, cfg.inbox_dir / name)
    print("result:", r["status"], r.get("rows"), "| rows:", rate_counts(store), "| files:", [(f, s, n) for f, s, _, n, _ in files_rows(store)],
          "| parts:", parts(store), "| rollups_stale:", store.rollups_stale())
