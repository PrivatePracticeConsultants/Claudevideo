"""S9: 0-row files and a later code-set change.
 config cpt_codes = ['97110']; the payer file holds only 97140.
 Link: done with 0 rows? fingerprinted? Then codes gain 97140: re-paste the
 link (no-op?), re-expand its index (no-op?), monthly refresh with the
 server unchanged (no-op?), forget + re-paste (works?).
 Inbox: re-drop the same file (same?), `mrfx ingest --force` (scan force=True)."""
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import gen  # noqa: E402
import h  # noqa: E402

BODY = gen.gzn(gen.doc(n_items=100, filler=20, code="97140"))


def serve(body):
    def fn(hh, head, entry):
        rng = h.parse_range(hh, len(body))
        if rng:
            a, b = rng
            h.send(hh, entry, 206, {"Content-Range": f"bytes {a}-{b}/{len(body)}", "ETag": '"z"'}, body[a:b + 1], head=head)
        else:
            h.send(hh, entry, 200, {"ETag": '"z"'}, body, head=head)
    return fn


def index(hh, head, entry):
    doc = {"reporting_entity_name": "Acme", "reporting_structure": [
        {"reporting_plans": [{"plan_name": "PPO"}], "in_network_files": [{"location": STATE["child"]}]}]}
    h.send(hh, entry, 200, {}, json.dumps(doc).encode(), head=head)


STATE: dict = {}


def state(store, label):
    rows = h.queue_rows(store, "id, status, kind, rows_emitted, substr(error,1,110) AS error")
    with store.connect() as con:
        files = con.execute("SELECT filename, status, rows_emitted, substr(json_extract_string(qa,'$.messages'),1,80) FROM files ORDER BY filename").fetchall()
        fps = con.execute("SELECT filename, full_sha IS NOT NULL FROM content_fingerprints").fetchall()
        rates = con.execute("SELECT count(*) FROM rates").fetchone()[0]
    print(f"\n-- {label}\n   urls: {rows}\n   files: {files}\n   fingerprints: {fps}\n   rates: {rates}")


def main():
    from mrfx import fetch
    from mrfx.ingest import forget_file, scan_inbox
    logging.basicConfig(level=logging.ERROR)
    cfg = h.scratch_cfg("s9", parallel_downloads=1, parallel_ingests=1)
    cfg.codes.cpt_codes = ["97110"]
    store = h.open_store(cfg)
    h.ROUTES.clear(); h.LOG.clear()
    h.ROUTES["/r.json.gz"] = serve(BODY); h.ROUTES["/index.json"] = index
    httpd, base = h.start_server()
    STATE["child"] = base + "/r.json.gz"
    fetch.add_urls(store, [base + "/index.json"]); h.drain(cfg, store, 60)
    state(store, "loaded with codes=[97110]; the file holds 97140 only")

    cfg.codes.cpt_codes = ["97110", "97140"]
    print("\n>>> the user adds 97140 to codes.cpt_codes")
    n = len(h.LOG)
    print("   re-paste the child link:", fetch.add_urls(store, [base + "/r.json.gz"])); h.drain(cfg, store, 30)
    print("   requests:", len(h.LOG) - n)
    n = len(h.LOG)
    print("   re-paste the index:", fetch.add_urls(store, [base + "/index.json"])); h.drain(cfg, store, 60)
    print("   requests:", [(p, s) for m, p, hd, w, s in h.LOG[n:]])
    state(store, "after re-pasting link and index (server unchanged)")
    n = len(h.LOG)
    print("   monthly refresh (requeue_urls index, revalidate=1):", store.requeue_urls([base + "/index.json"])); h.drain(cfg, store, 60)
    print("   requests:", [(p, s) for m, p, hd, w, s in h.LOG[n:]])
    state(store, "after the monthly refresh (server unchanged)")
    # the one path that works
    fname = h.queue_rows(store, "filename")[1]["filename"]
    print("   forget_file:", forget_file(cfg, store, fname))
    print("   re-paste child:", fetch.add_urls(store, [base + "/r.json.gz"])); h.drain(cfg, store, 60)
    state(store, "after forget + re-paste")
    httpd.shutdown()

    # inbox side
    cfg2 = h.scratch_cfg("s9_inbox", parallel_downloads=1, parallel_ingests=1)
    cfg2.codes.cpt_codes = ["97110"]
    store2 = h.open_store(cfg2)
    (cfg2.inbox_dir / "acme.json.gz").write_bytes(BODY)
    scan_inbox(cfg2, store2)
    state(store2, "INBOX: loaded with codes=[97110]")
    cfg2.codes.cpt_codes = ["97110", "97140"]
    (cfg2.inbox_dir / "acme.json.gz").write_bytes(BODY)
    scan_inbox(cfg2, store2)
    state(store2, "INBOX: codes gained 97140, same file re-dropped (plain scan)")
    (cfg2.inbox_dir / "acme.json.gz").write_bytes(BODY)
    scan_inbox(cfg2, store2, force=True)
    state(store2, "INBOX: re-dropped + scan force=True (mrfx ingest --force)")


if __name__ == "__main__":
    main()
