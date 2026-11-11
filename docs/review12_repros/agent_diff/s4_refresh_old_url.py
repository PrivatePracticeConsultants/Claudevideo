"""S4: a static index whose child links carry a per-visit signature (?sig=…,
volatile for dedup_key — the Azure/CloudFront shape) and whose server REFUSES
an old signature (403). The child is republished in place. The monthly
refresh re-expands the index: _refresh_loaded_children asks the server about
each loaded child — with WHICH url, the stored (old-signature) one or the one
the index just handed over? Variant B: the signature does not rotate (control)."""
import json
import logging
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
import h  # noqa: E402

STATE = {"child": b"", "etag": '"v1"', "sig": "S1", "index_sig": "S1"}


def index(hh, head, entry):
    doc = {"reporting_entity_name": "Acme", "reporting_structure": [
        {"reporting_plans": [{"plan_name": "PPO", "plan_id_type": "EIN", "plan_id": "1", "plan_market_type": "group"}],
         "in_network_files": [{"description": "rates", "location": f"{STATE['base']}/child.json.gz?sig={STATE['sig']}"}]}]}
    h.send(hh, entry, 200, {"Content-Type": "application/json"}, json.dumps(doc).encode())


def child(hh, head, entry):
    q = hh.path.split("?", 1)[1] if "?" in hh.path else ""
    if q != f"sig={STATE['sig']}":
        h.send(hh, entry, 403, {}, b"signature expired")
        return
    body = STATE["child"]
    rng = h.parse_range(hh, len(body))
    hdr = {"ETag": STATE["etag"]}
    if rng:
        a, b = rng
        hdr["Content-Range"] = f"bytes {a}-{b}/{len(body)}"
        h.send(hh, entry, 206, hdr, body[a:b + 1])
    else:
        h.send(hh, entry, 200, hdr, body)


def run(rotate: bool, forget_record: bool = False):
    cfg = h.scratch_cfg("s4_" + ("rotate" if rotate else "static") + ("_074" if forget_record else ""),
                        parallel_downloads=1, parallel_ingests=1)
    store = h.open_store(cfg)
    h.LOG.clear()
    STATE.update(child=h.rate_file_bytes(50, day="2026-05-01"), etag='"v1"', sig="S1")
    h.ROUTES["/index.json"] = index
    h.ROUTES["/child.json.gz"] = child
    httpd, base = h.start_server()
    STATE["base"] = base
    from mrfx import fetch
    fetch.add_urls(store, [base + "/index.json"])
    h.drain(cfg, store)
    with store.connect() as con:
        print(" after first load:", con.execute("SELECT filename, file_month, rows_emitted FROM files").fetchall())
        print(" child row:", con.execute("SELECT status, server_validator, server_size, url FROM url_queue WHERE parent_id IS NOT NULL").fetchall())
    # the payer republishes the child in place (June, new ETag); the index now hands out a new signature
    STATE.update(child=h.rate_file_bytes(80, day="2026-06-01", tin0=450000000), etag='"v2"')
    if rotate:
        STATE["sig"] = "S2"
    if forget_record:
        # a child loaded by 0.7.4: nothing recorded about the server, and the
        # server still serves the SAME bytes (nothing republished)
        STATE.update(child=h.rate_file_bytes(50, day="2026-05-01"), etag='"v1"')
        with store.connect() as con:
            con.execute("UPDATE url_queue SET server_validator = NULL, server_size = NULL WHERE parent_id IS NOT NULL")
    n = len(h.LOG)
    print(" requeue_urls(index):", store.requeue_urls([base + "/index.json"]))
    h.drain(cfg, store)
    print(" requests during refresh:", [(m, p, s) for m, p, r, w, s in h.requests_since(n)])
    with store.connect() as con:
        print(" after refresh:", con.execute("SELECT filename, file_month, rows_emitted FROM files").fetchall())
        print(" child row:", con.execute("SELECT status, error FROM url_queue WHERE parent_id IS NOT NULL").fetchall())
    httpd.shutdown()


def main():
    logging.basicConfig(level=logging.WARNING)
    print("== A: signature rotates per index visit (old sig -> 403)")
    run(True)
    print("== B: control, signature constant")
    run(False)
    print("== C: child loaded by 0.7.4 (no server record), server UNCHANGED, monthly refresh")
    run(False, forget_record=True)


if __name__ == "__main__":
    main()
