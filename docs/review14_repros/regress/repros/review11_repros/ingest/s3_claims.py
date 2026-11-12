"""Scenario 3: the content claim.
A. the URL queue and the inbox load the SAME bytes concurrently (run_queue drain thread + scan_inbox
   thread): how many copies land?
B. a link to bytes already loaded from the inbox (and the reverse): caught?
C. claims left behind by a crash: url_queue row 'ingesting' + inflight fingerprint, files row
   'processing' -> who releases them at restart, and is the file then loaded exactly once?
D. in-flight fingerprint rows left behind after claim-skip / cancel_all_downloading (review 9 3.5)."""
import threading
from dl import *
import mrfx.fetch as F
import mrfx.ingest as I
from mrfx.ingest import scan_inbox


def rates_by_file(store):
    return dict(q(store, "SELECT source_file, count(*) FROM rates GROUP BY 1 ORDER BY 1"))


def dup_lines(store):
    return q(store, "SELECT count(*) FROM (SELECT payer, billing_code, tin_value, npi, negotiated_rate FROM rates GROUP BY ALL HAVING count(*) > 1)")[0][0]


if __name__ == "__main__":
    body = gz(doc(n=300, pad_mb=2.0))
    print("== A. link + inbox copy of the same bytes, concurrently")
    cfg, store = mk_cfg("s3a", early_dedup_mb=1)
    srv = Srv({"/a/r.json.gz": body}); srv.delay["/a/r.json.gz"] = 0.02
    ua = srv.base + "/a/r.json.gz"
    F.add_urls(store, [ua])
    (cfg.inbox_dir / "r_inbox.json.gz").write_bytes(body)
    t = threading.Thread(target=lambda: F.run_queue(cfg, store, drain=True)); t.start()
    time.sleep(0.5); scan_inbox(cfg, store); t.join(120)
    print("  url rows:", q(store, "SELECT status, kind, rows_emitted, substr(error,1,70) FROM url_queue"))
    print("  files:", q(store, "SELECT filename, status, rows_emitted FROM files ORDER BY 1"))
    print("  rates per file:", rates_by_file(store), "| contract lines held twice:", dup_lines(store))
    srv.close(); store.close()

    print("== B1. bytes loaded from the inbox FIRST, then the same bytes via a link (different name)")
    cfg, store = mk_cfg("s3b1", early_dedup_mb=1)
    (cfg.inbox_dir / "r_inbox.json.gz").write_bytes(body); scan_inbox(cfg, store)
    srv = Srv({"/b/other.json.gz": body}); ub = srv.base + "/b/other.json.gz"
    F.add_urls(store, [ub]); F.run_queue(cfg, store, drain=True)
    print("  url rows:", q(store, "SELECT status, kind, rows_emitted, substr(error,1,90) FROM url_queue"))
    print("  rates per file:", rates_by_file(store), "| dup lines:", dup_lines(store), "| GETs:", len(srv.hits("/b/other.json.gz")))
    srv.close(); store.close()

    print("== B2. bytes loaded via a link FIRST, then the same bytes dropped in the inbox under another name")
    cfg, store = mk_cfg("s3b2", early_dedup_mb=1)
    srv = Srv({"/b/other.json.gz": body}); ub = srv.base + "/b/other.json.gz"
    F.add_urls(store, [ub]); F.run_queue(cfg, store, drain=True)
    (cfg.inbox_dir / "r_inbox.json.gz").write_bytes(body); print("  scan:", scan_inbox(cfg, store))
    print("  rates per file:", rates_by_file(store), "| dup lines:", dup_lines(store))
    srv.close(); store.close()

    print("== C. claims left behind by a crash")
    cfg, store = mk_cfg("s3c", early_dedup_mb=1)
    srv = Srv({"/c/r.json.gz": body}); uc = srv.base + "/c/r.json.gz"
    F.add_urls(store, [uc])
    # simulate: downloaded, claimed 'ingesting', in-flight fingerprint recorded, files row 'processing', then crash
    F.run_queue(cfg, store, drain=True)
    before = q(store, "SELECT status, rows_emitted FROM url_queue")[0]
    with store.write_lock, store.connect() as con:
        con.execute("UPDATE url_queue SET status = 'ingesting'")
        con.execute("UPDATE files SET status = 'processing'")
        con.execute("INSERT OR REPLACE INTO inflight_fingerprints (url_id, size, head_bytes, head_sha, tail_bytes, tail_sha, win_sha, magic, recorded_at) "
                    "VALUES (1, ?, 1, 'x', 1, 'y', 'z', 'gz', current_timestamp)", [len(body)])
    print("  staged:", q(store, "SELECT status FROM url_queue"), q(store, "SELECT status FROM files"), "inflight rows:", q(store, "SELECT count(*) FROM inflight_fingerprints"))
    store.close(); store = Store(cfg.store_dir)
    print("  restart: recover_stuck_urls ->", store.recover_stuck_urls(), "| recover_stuck_files ->", store.recover_stuck_files())
    print("  rows now:", q(store, "SELECT status, kind FROM url_queue"), q(store, "SELECT status, substr(error,1,60) FROM files"))
    print("  download kept?", sorted(p.name for p in cfg.downloads_dir.iterdir()), "(delete_raw_after_ingest default True)")
    F.run_queue(cfg, store, drain=True)
    print("  after drain:", q(store, "SELECT status, kind, rows_emitted FROM url_queue"), q(store, "SELECT status, rows_emitted FROM files"))
    print("  rates per file:", rates_by_file(store), "| dup lines:", dup_lines(store), "| GETs:", len(srv.hits("/c/r.json.gz")),
          "| inflight rows:", q(store, "SELECT count(*) FROM inflight_fingerprints"))
    srv.close(); store.close()

    print("== C2. inbox file row left 'processing' (crash) -> restart -> re-ingested once?")
    cfg, store = mk_cfg("s3c2")
    (cfg.inbox_dir / "r_inbox.json.gz").write_bytes(body); scan_inbox(cfg, store)
    with store.write_lock, store.connect() as con:
        con.execute("UPDATE files SET status = 'processing'")
    # the raw copy was moved to processed/ by the finished ingest; a crash mid-parse would have left it in the inbox
    shutil.move(cfg.processed_dir / "r_inbox.json.gz", cfg.inbox_dir / "r_inbox.json.gz")
    store.close(); store = Store(cfg.store_dir)
    print("  recover_stuck_files:", store.recover_stuck_files(), "| row:", q(store, "SELECT status, substr(error,1,60) FROM files"))
    print("  scan:", [(r['file'], r['status'], r.get('rows')) for r in scan_inbox(cfg, store)], "| scan again:", scan_inbox(cfg, store))
    print("  rates per file:", rates_by_file(store), "| dup lines:", dup_lines(store))
    store.close()

    print("== D. inflight fingerprint rows after a claim-skip and after cancel_all_downloading")
    cfg, store = mk_cfg("s3d", early_dedup_mb=1)
    srv = Srv({"/d/one.json.gz": body, "/d/two.json.gz": body}); srv.delay["/d/one.json.gz"] = 0.03
    F.add_urls(store, [srv.base + "/d/one.json.gz", srv.base + "/d/two.json.gz"])
    F.run_queue(cfg, store, drain=True)
    print("  url rows:", q(store, "SELECT id, status, kind, substr(error,1,60) FROM url_queue ORDER BY id"))
    print("  inflight rows left:", q(store, "SELECT url_id FROM inflight_fingerprints"))
    F.add_urls(store, [srv.base + "/d/three.json.gz"])
    srv.files["/d/three.json.gz"] = body; srv.delay["/d/three.json.gz"] = 0.2
    stop = threading.Event()
    t = threading.Thread(target=lambda: F.run_queue(cfg, store, stop=stop)); t.start()
    time.sleep(2.0)
    if hasattr(store, "cancel_all_downloading"):
        print("  cancel_all_downloading ->", store.cancel_all_downloading())
    stop.set(); t.join(60)
    print("  url rows:", q(store, "SELECT id, status, kind FROM url_queue ORDER BY id"), "| inflight rows left:", q(store, "SELECT url_id FROM inflight_fingerprints"))
    srv.close(); store.close()
