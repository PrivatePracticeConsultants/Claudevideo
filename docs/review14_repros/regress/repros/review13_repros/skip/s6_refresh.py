"""S6: the monthly refresh / re-expansion. A static index with one child;
first load; then one of several server changes; then the monthly refresh
(requeue_urls(index) + revalidate=1 as monthly_refresh_tick does). For each:
requests made, child row status + error, the months in `rates`.
 A. 0.7.4-loaded child (no server record), server republished, probe answers
    chunked with NO size and no validator          (the round-13 diff finding)
 B. same size, DIFFERENT bytes, no validator at all
 C. different bytes AND size, but ETag UNCHANGED (stale validator)
 D. child removed from the index
 E. child URL (path) changed, bytes unchanged
 F. static index whose children are month-dated: a new month appears
 G. children carry a GCS-style signature that rotates per visit (never stripped)
 H. validator unchanged, size unchanged, bytes changed (the honest residual) — said?
 I. requeue of the CHILD link directly (fetch_url_record's revalidate path), B-shape"""
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import gen  # noqa: E402
import h  # noqa: E402

STATE: dict = {}


def index(hh, head, entry):
    doc = {"reporting_entity_name": "Acme", "reporting_structure": [
        {"reporting_plans": [{"plan_name": "PPO", "plan_id_type": "EIN", "plan_id": "1", "plan_market_type": "group"}],
         "in_network_files": [{"description": "rates", "location": u} for u in STATE["children"]]}]}
    h.send(hh, entry, 200, {"Content-Type": "application/json"}, json.dumps(doc).encode(), head=head)


def child(hh, head, entry):
    key = hh.path.split("?")[0]
    spec = STATE["files"][key]
    body = spec["body"]
    mode = spec.get("mode", "ranged")
    hdr = {}
    if spec.get("etag"):
        hdr["ETag"] = spec["etag"]
    if mode == "chunked_nosize":
        hh.send_response(200)
        hh.send_header("Transfer-Encoding", "chunked")
        hh.end_headers()
        entry[4] = 200
        if not head:
            for i in range(0, len(body), 64 << 10):
                piece = body[i:i + (64 << 10)]
                hh.wfile.write(f"{len(piece):x}\r\n".encode() + piece + b"\r\n")
                entry[3] += len(piece)
            hh.wfile.write(b"0\r\n\r\n")
        return
    rng = h.parse_range(hh, len(body))
    if rng:
        a, b = rng
        hdr["Content-Range"] = f"bytes {a}-{b}/{len(body)}"
        h.send(hh, entry, 206, hdr, body[a:b + 1], head=head)
    else:
        h.send(hh, entry, 200, hdr, body, head=head)


def start(name, children, files, early_mb=15):
    cfg = h.scratch_cfg("s6_" + name, parallel_downloads=1, parallel_ingests=1, early_dedup_mb=early_mb)
    store = h.open_store(cfg)
    h.ROUTES.clear(); h.LOG.clear()
    h.ROUTES["/index.json"] = index
    for k in files:
        h.ROUTES[k] = child
    httpd, base = h.start_server()
    STATE.update(base=base, children=[base + c for c in children], files=files)
    from mrfx import fetch
    fetch.add_urls(store, [base + "/index.json"])
    h.drain(cfg, store, 90)
    return cfg, store, httpd, base


def show(store, title, since):
    rows = h.queue_rows(store, "id, status, kind, revalidate, server_validator, server_size, substr(error,1,120) AS error, url")
    with store.connect() as con:
        months = con.execute("SELECT file_month, count(*) FROM rates GROUP BY 1 ORDER BY 1").fetchall()
        files = con.execute("SELECT filename, rows_emitted FROM files ORDER BY filename").fetchall()
    print(f"\n   -- {title}")
    print("   requests:", [(m, p.split('?')[0] + ('?…' if '?' in p else ''), hd.get('Range'), s) for m, p, hd, w, s in h.LOG[since:]])
    for r in rows:
        print(f"   row {r['id']} {r['status']}/{r['kind']} reval={r['revalidate']} val={r['server_validator']} size={r['server_size']} err={r['error']!r}")
    print(f"   rates by month: {months}; files: {files}")
    return rows, months


def refresh(cfg, store, base):
    """What monthly_refresh_tick does for a loaded static index."""
    n = len(h.LOG)
    store.requeue_urls([base + "/index.json"])
    h.drain(cfg, store, 90)
    return n


def v1(**k):
    return gen.gzn(gen.doc(n_items=50, filler=20, day="2026-05-01", **k))


def v2(**k):
    return gen.gzn(gen.doc(n_items=80, filler=20, day="2026-06-01", rate=45.0, tin0=450000000, **k))


def case_a():
    files = {"/r.json.gz": {"body": v1(), "etag": '"v1"'}}
    cfg, store, httpd, base = start("A_074_nosize", ["/r.json.gz"], files)
    with store.write_lock, store.connect() as con:      # what a 0.7.4 load left
        con.execute("UPDATE url_queue SET server_validator = NULL, server_size = NULL WHERE kind = 'in_network'")
    files["/r.json.gz"] = {"body": v2(), "mode": "chunked_nosize"}
    n = refresh(cfg, store, base)
    rows, months = show(store, "A. 0.7.4 child, republished, probe gives no size/validator", n)
    print("   VERDICT A:", "re-downloaded" if ("2026-06",) in [m[:1] for m in months] else "LEFT AT THE OLD MONTH, no note on the row")
    httpd.shutdown()


def case_b():
    b1 = v1()
    raw2, _ = gen.change_rate_once(gen.doc(n_items=50, filler=20, day="2026-05-01"), 0)
    b2 = gen.gz0(gen.doc(n_items=50, filler=20, day="2026-05-01"))      # stored so sizes can be matched
    # make v1 stored too so the only difference is the one rate
    files = {"/r.json.gz": {"body": gen.gz0(gen.doc(n_items=50, filler=20, day="2026-05-01"))}}
    cfg, store, httpd, base = start("B_same_size_no_validator", ["/r.json.gz"], files)
    files["/r.json.gz"] = {"body": gen.gz0(raw2)}
    assert len(files["/r.json.gz"]["body"]) == len(b2)
    n = refresh(cfg, store, base)
    show(store, "B. same size, one rate changed, NO validator ever", n)
    with store.connect() as con:
        print("   rates distinct values:", con.execute("SELECT DISTINCT negotiated_rate FROM rates ORDER BY 1").fetchall())
    httpd.shutdown()
    del b1


def case_c():
    files = {"/r.json.gz": {"body": v1(), "etag": '"same-forever"'}}
    cfg, store, httpd, base = start("C_stale_etag", ["/r.json.gz"], files)
    files["/r.json.gz"] = {"body": v2(), "etag": '"same-forever"'}
    n = refresh(cfg, store, base)
    rows, months = show(store, "C. new bytes AND new size, ETag unchanged", n)
    print("   VERDICT C:", "re-downloaded" if ("2026-06",) in [m[:1] for m in months] else "LEFT AT THE OLD MONTH (validator wins over size), no note")
    httpd.shutdown()


def case_d():
    files = {"/r.json.gz": {"body": v1(), "etag": '"v1"'}, "/s.json.gz": {"body": v1(payer="Beta"), "etag": '"s1"'}}
    cfg, store, httpd, base = start("D_child_removed", ["/r.json.gz", "/s.json.gz"], files)
    STATE["children"] = [base + "/r.json.gz"]
    n = refresh(cfg, store, base)
    show(store, "D. /s.json.gz removed from the index", n)
    httpd.shutdown()


def case_e():
    files = {"/r.json.gz": {"body": v1(), "etag": '"v1"'}, "/r2.json.gz": {"body": v1(), "etag": '"v1"'}}
    cfg, store, httpd, base = start("E_url_changed_same_bytes", ["/r.json.gz"], files)
    STATE["children"] = [base + "/r2.json.gz"]
    n = refresh(cfg, store, base)
    show(store, "E. child's URL changed, bytes unchanged", n)
    httpd.shutdown()


def case_f():
    files = {"/2026-05/r.json.gz": {"body": v1(), "etag": '"m5"'}, "/2026-06/r.json.gz": {"body": v2(), "etag": '"m6"'}}
    cfg, store, httpd, base = start("F_month_dated_children", ["/2026-05/r.json.gz"], files)
    STATE["children"] = [base + "/2026-05/r.json.gz", base + "/2026-06/r.json.gz"]
    n = refresh(cfg, store, base)
    show(store, "F. static index now lists May AND June (month-dated children)", n)
    httpd.shutdown()


def case_g():
    big = gen.gz0(gen.doc(n_items=1500, filler=2000, day="2026-05-01"))
    files = {"/r.json.gz": {"body": big, "etag": '"g"'}}
    cfg, store, httpd, base = start("G_gcs_signature_rotates", ["/r.json.gz?X-Goog-Signature=S1&X-Goog-Date=20260501T000000Z"], files, early_mb=1)
    STATE["children"] = [base + "/r.json.gz?X-Goog-Signature=S2&X-Goog-Date=20260601T000000Z"]
    n = refresh(cfg, store, base)
    show(store, "G. GCS-style signed child, signature rotated, bytes unchanged", n)
    print("   bytes the server sent for the child during the refresh:",
          sum(w for m, p, hd, w, s in h.LOG[n:] if p.startswith("/r.json.gz")))
    httpd.shutdown()


def case_h():
    raw = gen.doc(n_items=50, filler=20, day="2026-05-01")
    raw2, _ = gen.change_rate_once(raw, 0)
    files = {"/r.json.gz": {"body": gen.gz0(raw), "etag": '"same"'}}
    cfg, store, httpd, base = start("H_residual", ["/r.json.gz"], files)
    files["/r.json.gz"] = {"body": gen.gz0(raw2), "etag": '"same"'}
    n = refresh(cfg, store, base)
    show(store, "H. same size, same ETag, one rate changed (the honest residual)", n)
    httpd.shutdown()


def case_i():
    raw = gen.doc(n_items=50, filler=20, day="2026-05-01")
    raw2, _ = gen.change_rate_once(raw, 0)
    files = {"/r.json.gz": {"body": gen.gz0(raw)}}
    cfg, store, httpd, base = start("I_child_requeue_no_validator", ["/r.json.gz"], files)
    files["/r.json.gz"] = {"body": gen.gz0(raw2)}
    n = len(h.LOG)
    print("   requeue_urls(child):", store.requeue_urls([base + "/r.json.gz"]))
    h.drain(cfg, store, 60)
    show(store, "I. the CHILD link re-queued (revalidate=1), same size, no validator, bytes changed", n)
    httpd.shutdown()


if __name__ == "__main__":
    logging.basicConfig(level=logging.ERROR)
    for c in (case_a, case_b, case_c, case_d, case_e, case_f, case_g, case_h, case_i):
        print(f"\n== {c.__name__}")
        try:
            c()
        except Exception as e:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            print("   CASE CRASHED:", e)
