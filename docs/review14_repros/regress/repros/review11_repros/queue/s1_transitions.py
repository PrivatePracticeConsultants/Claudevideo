"""Derive the REAL url_queue status graph by instrumenting every Store write
that changes `status`, while a mixed queue drains: rate file, TOC, HTML page,
allowed-amounts, unknown, 404, 403, a byte-identical twin pair, a TOC that
lists itself, and user actions in each terminal state."""
import threading
from h import *  # noqa

TRANS = []          # (from, to, who)
_lock = threading.Lock()


def instrument(store):
    import mrfx.store as ms
    import inspect

    def who():
        for fr in inspect.stack()[2:8]:
            if fr.function not in ("wrapper", "update_url", "_claim_next", "connect", "__enter__"):
                return fr.function
        return "?"

    def wrap(name, id_arg):
        real = getattr(store, name)

        def wrapper(*a, **k):
            ids = id_arg(a, k)
            before = {r["id"]: r["status"] for r in q(store, "SELECT id, status FROM url_queue")}
            out = real(*a, **k)
            after = {r["id"]: r["status"] for r in q(store, "SELECT id, status FROM url_queue")}
            with _lock:
                for i, st in after.items():
                    if before.get(i) != st:
                        TRANS.append((before.get(i, "<new>"), st, f"{name}<-{who()}"))
            return out
        setattr(store, name, wrapper)
    for name in ("update_url", "_claim_next", "claim_content_ingest", "park_url", "pause_url",
                 "park_waiting", "wake_parked_urls", "wake_paused_urls", "set_url_status_by_id",
                 "force_size_requeue", "requeue_urls", "requeue_failed", "clear_queued",
                 "cancel_all_downloading", "resume_canceled", "revive_skipped_duplicates",
                 "release_claim", "recover_stuck_urls", "requeue_oversize_within",
                 "requeue_url_for_file", "forget_file", "enqueue_url", "enqueue_urls",
                 "record_identity"):
        if hasattr(store, name):
            wrap(name, lambda a, k: None)


if __name__ == "__main__":
    root, cfg, store = world("s1", parallel_downloads=1)
    instrument(store)
    twin = rate_file_bytes("twin")
    srv = Server({
        "/a.json.gz": rate_file_bytes("a"),
        "/t1.json.gz": twin, "/t2.json.gz": twin,
        "/allowed.json": json.dumps({"reporting_entity_name": "X", "out_of_network": [
            {"name": "x", "billing_code": "97110", "billing_code_type": "CPT", "allowed_amounts": []}]}).encode(),
        "/unknown.json": b'{"hello": "world"}',
        "/forbidden.json.gz": {"status": 403, "body": b"no"},
        "/page.html": None,
        "/ref.json": (FIXTURES / "provider_reference_companion.json").read_bytes(),
    })
    srv.routes["/page.html"] = (f'<html><body><a href="{srv.url("/a.json.gz")}">a</a>'
                                f'<a href="/toc.json">toc</a></body></html>').encode()
    srv.routes["/toc.json"] = toc_bytes([srv.url("/t1.json.gz"), srv.url("/t2.json.gz"),
                                         srv.url("/toc.json"), srv.url("/ref.json")])
    urls = [srv.url(p) for p in ("/page.html", "/allowed.json", "/unknown.json",
                                 "/forbidden.json.gz", "/missing.json.gz")]
    print(add_urls(store, urls))
    print(drain(cfg, store, 120))
    banner("after first drain")
    print(table(store))
    # user actions from every terminal state
    st = {r["url"]: r for r in rows(store)}
    for frag, action in (("missing", "retry"), ("unknown.json", "skip"), ("forbidden", "retry"),
                         ("t2", "retry")):
        r = next(x for x in rows(store) if frag in x["url"])
        ok = store.set_url_status_by_id(r["id"], "queued" if action == "retry" else "skipped")
        print(f"user {action} on {frag} ({r['status']}/{r['kind']}): {ok}")
    done = next(x for x in rows(store) if x["url"] == "a.json.gz")
    print("user retry on done:", store.set_url_status_by_id(done["id"], "queued"),
          " skip on done:", store.set_url_status_by_id(done["id"], "skipped"),
          " force on done:", store.force_size_requeue(done["id"]))
    print("requeue_urls(done TOC):", store.requeue_urls([srv.url("/toc.json")]))
    print(drain(cfg, store, 120))
    banner("after second drain")
    print(table(store))
    # forget the loaded file -> done row flips
    from mrfx.ingest import forget_file
    print("forget:", forget_file(cfg, store, done["filename"]))
    print("retry forgotten:", store.set_url_status_by_id(done["id"], "queued"))
    print(drain(cfg, store, 120))
    banner("observed transitions (from -> to : writer)")
    seen = {}
    for a, b, w in TRANS:
        seen.setdefault((a, b), set()).add(w)
    for (a, b), ws in sorted(seen.items(), key=lambda x: (str(x[0][0]), str(x[0][1]))):
        print(f"  {a:>12} -> {b:<12} : {', '.join(sorted(ws))}")
    print("\ndownloads dir:", downloads(cfg))
    srv.stop()
