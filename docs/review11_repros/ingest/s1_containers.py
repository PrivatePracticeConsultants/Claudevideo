"""Scenario 1: container matrix through scan_inbox (twice). For each file: status, rows,
message, where the file ended, and whether a second scan re-tries it."""
import threading
from h import *
from mrfx.ingest import scan_inbox

if __name__ == "__main__":
    cfg = mkcfg("s1")
    store = Store(cfg.store_dir)
    base = doc(n=3)
    raw = json.dumps(base).encode()
    gz = gzip.compress(raw)
    cases = {}
    cases["a_plain.json"] = raw
    cases["b_gz.json.gz"] = gz
    cases["c_zip_one.zip"] = zipped({"rates.json": raw})
    cases["d_zip_many.zip"] = zipped({"manifest.json": json.dumps({"files": ["rates.json"]}),
                                      "rates.json": json.dumps(doc(n=5, seed=1))})
    cases["e_zip_in_zip.zip"] = zipped({"readme.txt": "hi"}, inner_zip={"rates.json": raw})
    cases["f_plain_named_gz.json.gz"] = raw                      # .gz that is plain json
    cases["g_gzip_named_json.json"] = gz                         # .json that is gzip
    cases["h_gzip_wrong_ext.dat"] = gz                           # gz with wrong extension
    cases["i_zero_byte.json"] = b""
    cases["j_bom.json"] = b"\xef\xbb\xbf" + raw
    cases["k_crlf.json"] = json.dumps(base, indent=1).replace("\n", "\r\n").encode()
    cases["l_utf16.json"] = json.dumps(base).encode("utf-16")
    cases["m_truncated_1byte.json.gz"] = gz[:-1]
    cases["n_trailing_garbage.json.gz"] = gz + b"GARBAGEGARBAGE"
    cases["o_multimember.json.gz"] = gzip.compress(raw[:len(raw)//2]) + gzip.compress(raw[len(raw)//2:])
    cases["p_zip_no_json.zip"] = zipped({"readme.txt": "nothing"})
    cases["q_truncated_mid.json.gz"] = gz[: len(gz) // 2]
    cases["r_plain_truncated.json"] = raw[: len(raw) // 2]
    cases["s_bom_in_gz.json.gz"] = gzip.compress(b"\xef\xbb\xbf" + raw)
    cases["t_zip_gz_member.zip"] = zipped({"rates.json.gz": gz})
    cases["u_blob_listing.json"] = json.dumps({"blobs": [{"name": "x", "downloadUrl": "http://x"}]}).encode()
    cases["v_allowed.json"] = json.dumps({**HDR, "out_of_network": []}).encode()
    cases["w_json_array_top.json"] = json.dumps([base]).encode()
    cases["x_empty_object.json"] = b"{}"
    cases["y_header_only.json"] = json.dumps(HDR).encode()
    for name, data in cases.items():
        (cfg.inbox_dir / name).write_bytes(data)
    # a file still being copied in: grows during the scan
    grow = cfg.inbox_dir / "z_growing.json"
    stop = threading.Event()
    def writer():
        with open(grow, "wb") as f:
            for i in range(0, len(raw), 200):
                f.write(raw[i:i+200]); f.flush()
                if stop.is_set(): break
                time.sleep(0.05)
    t = threading.Thread(target=writer); t.start()
    r1 = scan_inbox(cfg, store)
    t.join()
    r2 = scan_inbox(cfg, store)
    r3 = scan_inbox(cfg, store)
    rows = {f: (s, ft, n, e) for f, s, ft, n, e in files_rows(store)}
    rc = rate_counts(store)
    print(f"{'file':34} {'status':22} {'type':20} {'rows':>5} {'rates':>5} {'where':10} message")
    for name in sorted(list(cases) + ["z_growing.json"]):
        s, ft, n, e = rows.get(name, ("(no row)", "", None, ""))
        print(f"{name:34} {s:22} {ft or '':20} {str(n):>5} {str(rc.get(name, 0)):>5} {where_is(cfg, name):10} {(e or '')[:110]}")
    print("\nscan1 results:", [(r['file'], r['status']) for r in r1])
    print("scan2 results:", [(r['file'], r['status']) for r in r2])
    print("scan3 results:", [(r['file'], r['status']) for r in r3])
    print("rows per file (expected 3 for the base doc, 5 for d):", rc)
    print("distinct (payer, code, tin, npi, rate) over all rates:",
          q(store, "SELECT count(*) FROM (SELECT DISTINCT payer, billing_code, tin_value, npi, negotiated_rate FROM rates)")[0][0])
    print("rollups_stale:", store.rollups_stale(), "| parts:", parts(store))
    print("inbox leftovers:", sorted(p.name for p in cfg.inbox_dir.iterdir()))
    print("failed dir:", sorted(p.name for p in cfg.failed_dir.iterdir()))
