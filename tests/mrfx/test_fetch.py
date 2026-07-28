"""URL-drop ingestion tests: direct file URLs, TOC auto-expansion, web-page
handling, dedup, retry, and the oversize guard — against a local HTTP server."""

import argparse
import gzip
import http.server
import json
import threading
from pathlib import Path

import pytest

from mrfx.fetch import PAGE_HELP, add_urls, dedup_key, filename_for, run_queue
from tests.mrfx.conftest import FIXTURES


@pytest.fixture(scope="module")
def http_root(tmp_path_factory):
    root = tmp_path_factory.mktemp("www")
    # a real in-network fixture, gzipped
    (root / "rates.json.gz").write_bytes(gzip.compress((FIXTURES / "innetwork_mixed.json").read_bytes()))
    (root / "companion.json").write_bytes((FIXTURES / "provider_reference_companion.json").read_bytes())
    (root / "big_header.bin").write_bytes(b"\x00" * 128)
    (root / "portal.html").write_text(
        "<!doctype html><html><body><h1>Portal</h1><script>app.boot()</script></body></html>"
    )
    return root


@pytest.fixture(scope="module")
def server(http_root):
    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **k):
            super().__init__(*a, directory=str(http_root), **k)

        def log_message(self, *a):
            pass

    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    # TOC that points at the rate file on this same server (mixed absolute + relative)
    toc = {
        "reporting_entity_name": "Testco Health Plans Inc",
        "reporting_entity_type": "health insurance issuer",
        "version": "2.0.0",
        "reporting_structure": [
            {"reporting_plans": [{"plan_name": "T", "plan_id_type": "EIN", "plan_id": "1"}],
             "in_network_files": [
                 {"description": "rates", "location": f"{base}/rates.json.gz"},
                 {"description": "dup (signed twice)", "location": f"{base}/rates.json.gz?Expires=1&Signature=x"},
                 {"description": "relative path", "location": "/companion.json"},
             ]},
        ],
    }
    (http_root / "toc.json").write_text(json.dumps(toc))
    (http_root / "listing.html").write_text(
        f'<html><body><a href="{base}/rates.json.gz">rates</a>'
        f'<a href="/toc.json">index</a></body></html>'
    )
    yield base
    httpd.shutdown()


def drain(cfg, store):
    return run_queue(cfg, store, drain=True)


def test_direct_file_url_downloads_and_ingests(cfg, store, server):
    assert add_urls(store, [f"{server}/rates.json.gz"]) == {"added": 1, "skipped": 0, "invalid": 0}
    drain(cfg, store)
    (rec,) = store.list_urls()
    assert rec["status"] == "done" and rec["kind"] == "in_network"
    assert rec["rows_emitted"] > 0
    with store.connect() as con:
        assert con.execute("SELECT count(*) FROM rates").fetchone()[0] == rec["rows_emitted"]
    # raw download cleaned up after success (Parquet kept)
    assert not any(cfg.downloads_dir.iterdir())


def test_toc_url_expands_and_children_ingest(cfg, store, server):
    add_urls(store, [f"{server}/toc.json"])
    drain(cfg, store)
    recs = {dedup_key(r["url"]): r for r in store.list_urls()}
    toc = recs[dedup_key(f"{server}/toc.json")]
    assert toc["status"] == "done" and toc["kind"] == "toc"
    assert toc["child_count"] == 2  # signed duplicate deduped; relative resolved
    child = recs[dedup_key(f"{server}/rates.json.gz")]
    assert child["status"] == "done" and child["parent_id"] == toc["id"]
    companion = recs[dedup_key(f"{server}/companion.json")]
    assert companion["kind"] == "provider_reference" and companion["status"] == "done"
    with store.connect() as con:
        assert con.execute("SELECT count(*) FROM rates").fetchone()[0] > 0


def test_listing_page_yields_file_links(cfg, store, server):
    add_urls(store, [f"{server}/listing.html"])
    drain(cfg, store)
    recs = {dedup_key(r["url"]): r for r in store.list_urls()}
    page = recs[dedup_key(f"{server}/listing.html")]
    assert page["status"] == "done" and page["kind"] == "page"
    assert page["child_count"] == 2  # rates.json.gz + toc.json lifted off the page
    assert recs[dedup_key(f"{server}/rates.json.gz")]["status"] == "done"


def test_portal_page_gets_friendly_guidance(cfg, store, server):
    add_urls(store, [f"{server}/portal.html"])
    drain(cfg, store)
    (rec,) = [r for r in store.list_urls() if "portal" in r["url"]]
    assert rec["status"] == "failed" and rec["kind"] == "page"
    assert "Copy link address" in rec["error"]
    assert rec["error"] == PAGE_HELP


def test_dedup_and_failed_retry(cfg, store, server):
    u = f"{server}/rates.json.gz"
    assert add_urls(store, [u, u, "not-a-url"]) == {"added": 1, "skipped": 1, "invalid": 1}
    add_urls(store, [f"{server}/missing.json.gz"])
    drain(cfg, store)
    missing = next(r for r in store.list_urls() if "missing" in r["url"])
    assert missing["status"] == "failed" and "404" in missing["error"]
    # re-adding a failed link re-queues the SAME row (no duplicates)
    assert add_urls(store, [f"{server}/missing.json.gz?Sig=new"])["added"] == 1
    assert sum(1 for r in store.list_urls() if "missing" in r["url"]) == 1


def test_pasted_link_stays_visible_after_big_expansion(cfg, store, server):
    # A big index queues hundreds of children; the row the user pasted must
    # not scroll out of the dashboard's window.
    add_urls(store, [f"{server}/toc.json"])
    drain(cfg, store)
    listed = store.list_urls(limit=1)  # window smaller than the child count
    toc = [r for r in listed if dedup_key(r["url"]) == dedup_key(f"{server}/toc.json")]
    assert toc and toc[0]["parent_id"] is None       # pasted link pinned first
    assert listed[0]["parent_id"] is None
    assert sum(1 for r in listed if r["parent_id"] is not None) == 1  # children still capped


def test_skipped_child_stays_visible_in_big_queue(cfg, store):
    # found live: a file forgotten mid-grind flips its queue row to skipped,
    # but on a 1,700-child queue that early row fell outside the newest-500
    # window — its retry button (the only path back) was unreachable.
    # Actionable rows must be pinned into the listing.
    parent = store.enqueue_url("https://x.example/toc.json", "https://x.example/toc.json")
    with store.write_lock, store.connect() as con:
        first = store._enqueue_one(con, "https://x.example/f0.json.gz",
                                   "https://x.example/f0.json.gz", parent)
    store.enqueue_urls(
        [(f"https://x.example/f{i}.json.gz", f"https://x.example/f{i}.json.gz")
         for i in range(1, 601)], parent_id=parent)
    assert store.set_url_status_by_id(first, "skipped")
    listed = store.list_urls(limit=500)
    assert any(r["id"] == first and r["status"] == "skipped" for r in listed)


def test_byte_identical_file_on_second_url_skipped(cfg, store, server, http_root):
    # Blue plans host copies of each other's national files: same bytes, many
    # domains. The second copy must be skipped, not re-ingested.
    (http_root / "mirror_rates.json.gz").write_bytes((http_root / "rates.json.gz").read_bytes())
    add_urls(store, [f"{server}/rates.json.gz"])
    drain(cfg, store)
    with store.connect() as con:
        rows_after_first = con.execute("SELECT count(*) FROM rates").fetchone()[0]
    add_urls(store, [f"{server}/mirror_rates.json.gz"])
    drain(cfg, store)
    mirror = next(r for r in store.list_urls() if "mirror" in r["url"])
    assert mirror["status"] == "skipped" and mirror["kind"] == "duplicate"
    assert "identical" in mirror["error"] and "rates.json.gz" in mirror["error"]
    with store.connect() as con:  # no duplicate rows entered the store
        assert con.execute("SELECT count(*) FROM rates").fetchone()[0] == rows_after_first
    assert not any(cfg.downloads_dir.iterdir())  # mirror download cleaned up


def test_duplicate_of_ingesting_twin_kept_and_revived(cfg, store, server, http_root):
    # A twin that is merely INGESTING can still fail. The duplicate's bytes
    # must be KEPT (not destroyed), and when the twin fails the duplicate is
    # re-queued and ingests from the kept download — no re-download (signed
    # URLs expire, so the kept bytes may be the only recoverable copy).
    # Driven through process_url_record directly: run_queue's crash recovery
    # would (correctly) re-queue the simulated mid-ingest row.
    import hashlib

    from mrfx.fetch import _meta_path, process_url_record

    mirror = http_root / "revive_rates.json.gz"
    mirror.write_bytes((http_root / "rates.json.gz").read_bytes())
    sha = hashlib.sha256(mirror.read_bytes()).hexdigest()

    # row A (lower id): simulate a parallel worker mid-ingest on identical bytes
    add_urls(store, [f"{server}/rates.json.gz?copy=a"])
    rec_a = next(r for r in store.list_urls() if "copy=a" in r["url"])
    store.update_url(rec_a["id"], status="ingesting", content_sha=sha)

    add_urls(store, [f"{server}/revive_rates.json.gz"])
    process_url_record(cfg, store, store.next_queued_url())
    rec_b = next(r for r in store.list_urls() if "revive" in r["url"])
    assert rec_b["status"] == "skipped" and rec_b["kind"] == "duplicate"
    assert "currently being processed" in rec_b["error"]
    dest = cfg.downloads_dir / filename_for(rec_b["url"])
    assert dest.exists() and _meta_path(dest).exists()  # bytes KEPT, not destroyed

    # twin A fails -> B revives and must ingest from the kept download:
    # remove the server file so any re-download attempt would 404
    store.update_url(rec_a["id"], status="failed", error="ingest crashed: boom")
    assert store.revive_skipped_duplicates(sha, rec_a["id"]) == 1
    mirror.unlink()
    rec = store.next_queued_url()
    assert rec and "revive" in rec["url"]
    process_url_record(cfg, store, rec)
    rec_b = next(r for r in store.list_urls() if "revive" in r["url"])
    assert rec_b["status"] == "done" and rec_b["rows_emitted"] > 0


def test_forgotten_file_does_not_resurrect_its_duplicate_twin(cfg, store, server, http_root):
    # A mirror link deferred to the file the user later FORGETS. Forget flips the
    # ingested row's queue anchor done->skipped; recover_stuck_urls must NOT then
    # revive the duplicate (which would silently re-download + re-ingest the very
    # bytes the user erased). Auto-revive fires only for an actually-FAILED twin.
    import hashlib
    from mrfx.fetch import process_url_record

    mirror = http_root / "forget_rates.json.gz"
    mirror.write_bytes((http_root / "rates.json.gz").read_bytes())
    sha = hashlib.sha256(mirror.read_bytes()).hexdigest()

    add_urls(store, [f"{server}/rates.json.gz?copy=keep"])
    a = next(r for r in store.list_urls() if "copy=keep" in r["url"])
    store.update_url(a["id"], status="ingesting", content_sha=sha)
    add_urls(store, [f"{server}/forget_rates.json.gz"])   # the deferring twin
    process_url_record(cfg, store, store.next_queued_url())
    b = next(r for r in store.list_urls() if "forget_rates" in r["url"])
    assert b["status"] == "skipped" and b["kind"] == "duplicate"

    # A finishes, then the user forgets it: mirror forget_file's queue flip
    store.update_url(a["id"], status="done")
    with store.write_lock, store.connect() as con:
        con.execute("UPDATE url_queue SET status='skipped' WHERE id=?", [a["id"]])

    store.recover_stuck_urls()
    b2 = next(r for r in store.list_urls() if r["id"] == b["id"])
    assert b2["status"] == "skipped"   # NOT resurrected behind the user's back

    # but a genuinely FAILED twin still triggers the promised auto-retry
    with store.write_lock, store.connect() as con:
        con.execute("UPDATE url_queue SET status='failed' WHERE id=?", [a["id"]])
    store.recover_stuck_urls()
    b3 = next(r for r in store.list_urls() if r["id"] == b["id"])
    assert b3["status"] == "queued"


def test_higher_id_ingesting_twin_still_dedups_low_id_retry(cfg, store, server, http_root):
    # Regression: the cheap pre-ingest check uses queue-id order, so a LOWER-id
    # row (e.g. one that failed transiently and got re-queued) would slip past a
    # HIGHER-id twin that is already ingesting and ingest the same bytes twice.
    # The atomic claim right before ingest must catch it regardless of id order.
    import hashlib

    from mrfx.fetch import process_url_record

    mirror = http_root / "lowid_rates.json.gz"
    mirror.write_bytes((http_root / "rates.json.gz").read_bytes())
    sha = hashlib.sha256(mirror.read_bytes()).hexdigest()

    # low-id row is the one we process (simulating a late retry); the twin that
    # is already ingesting was queued AFTER it, so it has a HIGHER id.
    add_urls(store, [f"{server}/lowid_rates.json.gz"])
    add_urls(store, [f"{server}/rates.json.gz?copy=hi"])
    hi = next(r for r in store.list_urls() if "copy=hi" in r["url"])
    lo = next(r for r in store.list_urls() if "lowid" in r["url"])
    assert lo["id"] < hi["id"]
    store.update_url(hi["id"], status="ingesting", content_sha=sha)

    process_url_record(cfg, store, store.next_queued_url())
    lo = next(r for r in store.list_urls() if "lowid" in r["url"])
    assert lo["status"] == "skipped" and lo["kind"] == "duplicate"  # deferred, not ingested
    # and the store never got a second copy of the rows
    with store.connect() as con:
        assert con.execute("SELECT count(*) FROM url_queue WHERE status='ingesting'").fetchone()[0] == 1


def test_disk_reservations_block_concurrent_overcommit(cfg, server, monkeypatch):
    # With N downloads in flight, bytes not yet written are invisible to
    # disk_usage — the up-front guard must count OTHER downloads' reservations
    # against free space or concurrent fetches could collectively fill the disk.
    import collections

    import mrfx.fetch as F

    Usage = collections.namedtuple("Usage", "total used free")
    monkeypatch.setattr(F.shutil, "disk_usage", lambda p: Usage(100 << 30, 90 << 30, 10 << 30))
    dest = cfg.downloads_dir / "resv_rates.json.gz"
    foreign = 424242  # another thread's in-flight download
    try:
        # a sibling download has promised 9.5 of the 10 free GB -> this small
        # file (needs ~2GB headroom) must be refused, and say why
        F._set_reservation(foreign, int(9.5 * 2**30))
        with pytest.raises(F.DownloadError, match="promised to downloads in progress"):
            F.download(cfg, f"{server}/rates.json.gz", dest)
        assert not dest.exists()
        # sibling finishes -> same download now succeeds, and its own
        # reservation is released on the way out
        F._clear_reservation(foreign)
        sha, _ = F.download(cfg, f"{server}/rates.json.gz", dest)
        assert dest.exists() and len(sha) == 64
        assert F._disk_reservations == {}  # nothing leaked
    finally:
        F._clear_reservation(foreign)
        dest.unlink(missing_ok=True)


def test_sidecar_survives_ingest_failure_for_kill_free_reuse(cfg, store, server, monkeypatch):
    # The .fetchmeta sidecar must outlive the ingest: unlinking it up front
    # meant a kill (or crash) mid-ingest re-queued the row WITHOUT its reuse
    # ticket — the retry re-downloaded from byte zero, and an expired signed
    # URL then 403'd terminally and DELETED the only complete copy.
    import mrfx.fetch as F

    add_urls(store, [f"{server}/rates.json.gz?case=sidecar"])
    rec = store.next_queued_url()
    assert F.fetch_url_record(cfg, store, rec) is True  # downloaded + sidecar
    dest = cfg.downloads_dir / F.filename_for(rec["url"])
    assert dest.exists() and F._meta_path(dest).exists()

    def boom(*a, **k):
        raise RuntimeError("simulated mid-ingest death")

    monkeypatch.setattr(F, "ingest_file", boom)
    rec2 = store.next_fetched_url()
    F.process_url_record(cfg, store, rec2)
    row = next(r for r in store.list_urls() if r["id"] == rec["id"])
    assert row["status"] == "failed"
    # both the bytes AND the reuse ticket survive -> a retry ingests from the
    # kept download instead of re-downloading a possibly-expired URL
    assert dest.exists() and F._meta_path(dest).exists()


def test_preflight_failure_revives_deferred_twins(cfg, store, server, monkeypatch):
    # A twin that deferred ('skipped, will retry automatically if that one
    # fails') must be revived when the winner dies in PREFLIGHT too — this
    # failure path used to skip _revive_twins and strand the content forever.
    import mrfx.fetch as F

    add_urls(store, [f"{server}/rates.json.gz?tw=a", f"{server}/rates.json.gz?tw=b"])
    rows = store.list_urls()
    a = next(r for r in rows if "tw=a" in r["url"])
    b = next(r for r in rows if "tw=b" in r["url"])
    sha = "beef" * 16
    store.update_url(a["id"], status="ingesting", content_sha=sha)
    store.update_url(b["id"], status="ingesting", content_sha=sha)
    # B races into the atomic claim first and defers to in-flight A
    assert store.claim_content_ingest(sha, b["id"], "in_network", "b.json.gz") is not None

    # A's downloaded bytes + sidecar are on disk; its preflight then dies
    dest = cfg.downloads_dir / F.filename_for(a["url"])
    dest.write_bytes(b"payload")
    F._write_meta(dest, sha, a["url"])

    def bad_preflight(*args, **kwargs):
        raise OSError("simulated unreadable file")

    monkeypatch.setattr(F, "preflight", bad_preflight)
    a_rec = {**a, "content_sha": sha}
    F.process_url_record(cfg, store, a_rec)
    a2 = next(r for r in store.list_urls() if r["id"] == a["id"])
    b2 = next(r for r in store.list_urls() if r["id"] == b["id"])
    assert a2["status"] == "failed"
    assert b2["status"] == "queued"  # the promise held: B revived, content not stranded


def test_unknown_length_download_checks_disk_mid_stream(cfg, monkeypatch):
    # Chunked responses carry no Content-Length, so no up-front reservation is
    # possible — the stream must re-check free space periodically or N such
    # downloads could quietly fill the drive together.
    import collections
    import http.server as hs
    import threading as th

    import mrfx.fetch as F

    class Chunked(hs.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            blob = b"x" * 1024
            for _ in range(64):  # 64 KB total, no Content-Length
                self.wfile.write(f"{len(blob):X}\r\n".encode() + blob + b"\r\n")
            self.wfile.write(b"0\r\n\r\n")

        def log_message(self, *a):
            pass

    httpd = hs.ThreadingHTTPServer(("127.0.0.1", 0), Chunked)
    th.Thread(target=httpd.serve_forever, daemon=True).start()
    Usage = collections.namedtuple("Usage", "total used free")
    monkeypatch.setattr(F.shutil, "disk_usage", lambda p: Usage(100 << 30, 100 << 30, 1 << 30))
    monkeypatch.setattr(F, "_UNKNOWN_LEN_CHECK_BYTES", 4096)  # check every 4KB
    try:
        with pytest.raises(F.DownloadError, match="disk space"):
            F.download(cfg, f"http://127.0.0.1:{httpd.server_address[1]}/x.json",
                       cfg.downloads_dir / "chunked.json")
    finally:
        httpd.shutdown()


def test_startup_sweeps_downloads_stranded_after_done(cfg, store, server):
    # kill between the 'done' write and _cleanup_raw leaves the raw download
    # on disk forever (the row is terminal — nothing else reclaims it); the
    # queue-start sweep must reap it when delete_raw_after_ingest is on
    import mrfx.fetch as F

    add_urls(store, [f"{server}/rates.json.gz?sweep=1"])
    rec = store.next_queued_url()
    name = F.filename_for(rec["url"])
    stranded = cfg.downloads_dir / name
    stranded.write_bytes(b"leftover bytes")
    F._write_meta(stranded, "dead" * 16, rec["url"])
    store.update_url(rec["id"], status="done", filename=name)

    assert cfg.delete_raw_after_ingest is True
    run_queue(cfg, store, drain=True)  # queue empty; sweep runs at start
    assert not stranded.exists() and not F._meta_path(stranded).exists()


def test_unknown_length_download_holds_provisional_reservation(cfg, monkeypatch):
    # an unknown-length (chunked) download can't reserve its true size, but it
    # must hold a PROVISIONAL reservation mid-stream so a concurrent
    # known-length download's disk math can see it (else they overcommit).
    import collections
    import http.server as hs
    import threading as th

    import mrfx.fetch as F

    class Chunked(hs.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            blob = b"x" * 1024
            for _ in range(64):
                self.wfile.write(f"{len(blob):X}\r\n".encode() + blob + b"\r\n")
            self.wfile.write(b"0\r\n\r\n")

        def log_message(self, *a):
            pass

    httpd = hs.ThreadingHTTPServer(("127.0.0.1", 0), Chunked)
    th.Thread(target=httpd.serve_forever, daemon=True).start()
    Usage = collections.namedtuple("Usage", "total used free")
    seen = {}

    def fake_usage(p):
        if not seen:  # snapshot reservations the first time the stream re-checks disk
            seen.update(F._disk_reservations)
        return Usage(100 << 30, 50 << 30, 50 << 30)  # plenty free -> no raise

    monkeypatch.setattr(F.shutil, "disk_usage", fake_usage)
    monkeypatch.setattr(F, "_UNKNOWN_LEN_CHECK_BYTES", 4096)
    try:
        F.download(cfg, f"http://127.0.0.1:{httpd.server_address[1]}/x.json",
                   cfg.downloads_dir / "prov.json")
    finally:
        httpd.shutdown()
    assert seen and max(seen.values()) >= (2 << 30)  # provisional headroom was held
    assert F._disk_reservations == {}                # released on completion


def test_flaky_truncating_server_with_weak_etag_still_completes(cfg):
    # A server that (a) resets the connection every 50 KB and (b) advertises a
    # WEAK ETag. Without the fixes: the weak ETag in If-Range makes the server
    # answer ranges with a full 200, so every retry restarts at byte 0 and the
    # fixed retry budget is exhausted. With the fixes: resume falls back to
    # Last-Modified (a valid If-Range validator) so the server honors the range
    # (206), and progress-making connections don't count against the budget, so
    # the ~120 KB file completes over several truncated connections.
    import collections
    import http.server as hs
    import threading as th

    import mrfx.fetch as F

    # multiple 1 MB read-chunks per connection so httpx yields (and we persist)
    # full chunks before the truncation — as happens in a real 34 MB-of-300 MB
    # drop; a sub-chunk body would be discarded by httpx and never reach .part.
    body = bytes(i % 251 for i in range(3_000_000))

    class Flaky(hs.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        weak_etag = 'W/"v1"'
        last_mod = "Wed, 21 Oct 2026 07:28:00 GMT"
        saw_weak_ifrange = False

        def do_GET(self):
            rng = self.headers.get("Range")
            ifr = self.headers.get("If-Range")
            offset = int(rng.split("=")[1].split("-")[0]) if rng and "=" in rng else 0
            honor = bool(rng)
            if ifr is not None and ifr.strip() == self.weak_etag:
                Flaky.saw_weak_ifrange = True  # a weak validator must NOT be sent here
                honor = False                  # RFC: weak If-Range -> full 200
            if not honor:
                offset = 0
            remaining = len(body) - offset
            send = min(1_200_000, remaining)
            self.send_response(206 if honor else 200)
            self.send_header("ETag", self.weak_etag)
            self.send_header("Last-Modified", self.last_mod)
            self.send_header("Accept-Ranges", "bytes")
            if honor:
                self.send_header("Content-Range", f"bytes {offset}-{len(body)-1}/{len(body)}")
            self.send_header("Content-Length", str(remaining))  # claim the full remainder
            self.end_headers()
            try:
                self.wfile.write(body[offset:offset + send])
            except Exception:
                return
            if send < remaining:
                self.close_connection = True  # truncate: close before the rest

        def log_message(self, *a):
            pass

    httpd = hs.ThreadingHTTPServer(("127.0.0.1", 0), Flaky)
    th.Thread(target=httpd.serve_forever, daemon=True).start()
    cfg.download_retries = 1  # only the progress-aware resets let this finish
    dest = cfg.downloads_dir / "flaky.json"
    try:
        sha, _ = F.download(cfg, f"http://127.0.0.1:{httpd.server_address[1]}/f", dest)
    finally:
        httpd.shutdown()
    assert dest.read_bytes() == body           # fully + correctly reassembled
    assert Flaky.saw_weak_ifrange is False     # never sent the weak ETag in If-Range


def test_reserve_is_atomic_check_and_set(cfg, monkeypatch):
    # Two same-size downloads racing into a disk that fits only one: the FIRST
    # reservation must be visible to the second check even though neither has
    # streamed a byte yet — check-then-reserve as two steps would let both
    # pass (a freshly expanded TOC hands the downloader threads a batch at
    # exactly the same moment). free=12GB; each needs 10GB + 2GB headroom.
    import collections

    import mrfx.fetch as F

    Usage = collections.namedtuple("Usage", "total used free")
    monkeypatch.setattr(F.shutil, "disk_usage", lambda p: Usage(100 << 30, 88 << 30, 12 << 30))
    a, b = 111, 222
    try:
        F._reserve_disk_or_raise(a, cfg.downloads_dir, total=10 << 30, resume_from=0)
        assert F._disk_reservations[a] == 10 << 30
        with pytest.raises(F.DownloadError, match="promised to downloads in progress"):
            F._reserve_disk_or_raise(b, cfg.downloads_dir, total=10 << 30, resume_from=0)
        assert b not in F._disk_reservations  # loser reserved nothing
        # winner finishes -> the same second download now fits
        F._clear_reservation(a)
        F._reserve_disk_or_raise(b, cfg.downloads_dir, total=10 << 30, resume_from=0)
        assert F._disk_reservations[b] == 10 << 30
    finally:
        F._clear_reservation(a)
        F._clear_reservation(b)


def test_segmented_download_assembles_correct_bytes(cfg, monkeypatch):
    # a range-supporting server: the file is pulled in parallel byte-range
    # segments and must assemble byte-identical with the right sha
    import hashlib
    import http.server as hs
    import os as _os
    import mrfx.fetch as F

    payload = _os.urandom(300_000)
    expected = hashlib.sha256(payload).hexdigest()

    class Ranged(hs.BaseHTTPRequestHandler):
        def do_GET(self):
            rng = self.headers.get("Range", "")
            if rng.startswith("bytes="):
                a, b = rng[6:].split("-")
                start = int(a); end = int(b) if b else len(payload) - 1
                end = min(end, len(payload) - 1)
                body = payload[start:end + 1]
                self.send_response(206)
                self.send_header("Content-Range", f"bytes {start}-{end}/{len(payload)}")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers(); self.wfile.write(body)
            else:
                self.send_response(200)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers(); self.wfile.write(payload)

        def log_message(self, *a):
            pass

    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Ranged)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        monkeypatch.setattr(F, "_SEGMENT_MIN_BYTES", 1000)     # let a tiny file segment
        monkeypatch.setattr(F, "_SEGMENT_MIN_PIECE", 50_000)   # 300 KB / 50 KB -> 4 pieces
        cfg.download_segments = 4
        url = f"http://127.0.0.1:{httpd.server_address[1]}/f.json.gz"
        dest = cfg.downloads_dir / F.filename_for(url)
        sha, _final = F.download(cfg, url, dest)
        assert sha == expected
        assert dest.read_bytes() == payload                    # every segment stitched right
        assert not dest.with_suffix(dest.suffix + ".part").exists()
    finally:
        httpd.shutdown()


def test_segmentation_falls_back_when_server_ignores_range(cfg, monkeypatch):
    # a server that answers 200 to a Range probe -> no segmentation, the
    # single-connection path still downloads the file correctly
    import hashlib
    import http.server as hs
    import os as _os
    import mrfx.fetch as F

    payload = _os.urandom(200_000)

    class NoRange(hs.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)                            # ignores Range
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers(); self.wfile.write(payload)

        def log_message(self, *a):
            pass

    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), NoRange)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        monkeypatch.setattr(F, "_SEGMENT_MIN_BYTES", 1000)
        monkeypatch.setattr(F, "_SEGMENT_MIN_PIECE", 50_000)
        cfg.download_segments = 4
        url = f"http://127.0.0.1:{httpd.server_address[1]}/f.json.gz"
        dest = cfg.downloads_dir / F.filename_for(url)
        sha, _final = F.download(cfg, url, dest)
        assert sha == hashlib.sha256(payload).hexdigest()
    finally:
        httpd.shutdown()


def test_resolve_download_count_modes(cfg, monkeypatch):
    import mrfx.fetch as F

    # auto = the politeness cap (6, the browser per-host connection limit),
    # INDEPENDENT of core count: downloading is network-bound, so sizing it by
    # cores throttled the exact case it should help — a 4-core laptop waiting on
    # a slow payer CDN got 3 fetches in flight instead of 6.
    monkeypatch.setattr(F.os, "cpu_count", lambda: 9)
    cfg.parallel_downloads = 0
    assert F.resolve_download_count(cfg) == 6
    monkeypatch.setattr(F.os, "cpu_count", lambda: 4)
    assert F.resolve_download_count(cfg) == 6
    monkeypatch.setattr(F.os, "cpu_count", lambda: 2)
    assert F.resolve_download_count(cfg) == 6
    monkeypatch.setattr(F.os, "cpu_count", lambda: 1)
    assert F.resolve_download_count(cfg) == 6
    # explicit values are honored; 1 = the old sequential downloader
    cfg.parallel_downloads = 6
    assert F.resolve_download_count(cfg) == 6
    cfg.parallel_downloads = 1
    assert F.resolve_download_count(cfg) == 1


def test_parallel_downloads_drain_all_files(cfg, store, http_root, server):
    # several distinct files + 3 concurrent downloaders: every row must land
    # 'done' exactly once (atomic claims — no double-fetch, no starvation)
    import json as _json

    urls = []
    for i in range(4):
        doc = {
            "reporting_entity_name": f"Par DL Payer {i}", "reporting_entity_type": "issuer",
            "last_updated_on": "2026-06-01", "version": "1.0.0",
            "in_network": [{
                "negotiation_arrangement": "ffs", "name": "PT", "billing_code_type": "CPT",
                "billing_code_type_version": "2026", "billing_code": "97110",
                "negotiated_rates": [{
                    "provider_groups": [{"npi": [1234567893], "tin": {"type": "ein", "value": f"12345678{i}"}}],
                    "negotiated_prices": [{"negotiated_type": "negotiated", "negotiated_rate": 40.0 + i,
                                           "expiration_date": "2027-01-01", "service_code": ["11"],
                                           "billing_class": "professional"}],
                }],
            }],
        }
        (http_root / f"pardl_{i}.json").write_text(_json.dumps(doc))
        urls.append(f"{server}/pardl_{i}.json")
    add_urls(store, urls)
    cfg.parallel_downloads = 3
    try:
        drain(cfg, store)
    finally:
        cfg.parallel_downloads = 0
    rows = {r["url"]: r for r in store.list_urls() if r["url"] in urls}
    assert len(rows) == 4
    assert all(r["status"] == "done" and r["rows_emitted"] >= 1 for r in rows.values()), \
        {u: (r["status"], r["error"]) for u, r in rows.items()}


def test_racing_twins_cannot_mutually_defer(cfg, store, server):
    # Both twins are mid-flight ('ingesting') with identical bytes and race
    # into the atomic claim. The DEFER must land inside the locked step: the
    # first claimer flips itself to skipped right there, so the second claimer
    # no longer sees an in-flight twin and PROCEEDS. A caller-side flip let
    # both defer to each other — both skipped, nobody ingests, and the revive
    # hook never fires because neither twin ever fails.
    add_urls(store, [f"{server}/rates.json.gz?copy=x", f"{server}/rates.json.gz?copy=y"])
    rows = store.list_urls()
    a = next(r for r in rows if "copy=x" in r["url"])
    b = next(r for r in rows if "copy=y" in r["url"])
    sha = "feed" * 16
    store.update_url(a["id"], status="ingesting", content_sha=sha)
    store.update_url(b["id"], status="ingesting", content_sha=sha)

    # A claims first: defers to in-flight B AND is flipped skipped atomically
    twin = store.claim_content_ingest(sha, a["id"], "in_network", "a.json.gz")
    assert twin is not None and twin[1] == "ingesting"
    a2 = next(r for r in store.list_urls() if r["id"] == a["id"])
    assert a2["status"] == "skipped" and a2["kind"] == "duplicate"

    # B claims second: A is no longer in-flight, so B WINS and ingests
    assert store.claim_content_ingest(sha, b["id"], "in_network", "b.json.gz") is None
    b2 = next(r for r in store.list_urls() if r["id"] == b["id"])
    assert b2["status"] == "ingesting"  # exactly one twin proceeds

    # and if B later fails, skipped A revives (the kept-bytes retry path)
    store.update_url(b["id"], status="failed", error="ingest crashed: boom")
    assert store.revive_skipped_duplicates(sha, b["id"]) == 1


def test_oversize_guard(cfg, store, server):
    cfg.confirm_over_gb = 64 / 1e9  # 64 bytes — the 128-byte file trips it
    add_urls(store, [f"{server}/big_header.bin"])
    drain(cfg, store)
    rec = next(r for r in store.list_urls() if "big_header" in r["url"])
    # over-size is its OWN state, not 'failed' (so it never inflates the
    # failure count or buries a real error)
    assert rec["status"] == "oversize"
    assert "confirm_over_gb" in rec["error"]
    # "retry all failed" must NOT re-trigger over-size rows (they'd just re-fail)
    assert store.requeue_failed() == 0
    assert next(r for r in store.list_urls() if "big_header" in r["url"])["status"] == "oversize"


def test_download_anyway_overrides_size_guard(cfg, store, server):
    # the dashboard "download anyway" button: a file stopped ONLY by the
    # confirm_over_gb ceiling ingests when the per-row override is set
    cfg.confirm_over_gb = 100 / 1e9  # 100 bytes — trips on the real rate file
    add_urls(store, [f"{server}/rates.json.gz"])
    drain(cfg, store)
    rec = next(r for r in store.list_urls() if "rates.json.gz" in r["url"])
    assert rec["status"] == "oversize" and "download anyway" in rec["error"]
    assert store.force_size_requeue(rec["id"]) is True  # the button
    drain(cfg, store)
    rec = next(r for r in store.list_urls() if "rates.json.gz" in r["url"])
    assert rec["status"] == "done" and rec["rows_emitted"] > 0
    assert rec["force_size"] is True
    assert store.force_size_requeue(rec["id"]) is False  # done row rejected


def test_force_size_is_one_shot_not_inherited_by_later_retries(cfg, store, server):
    # the "download anyway" override must NOT survive into a later generic
    # retry — otherwise a transient failure after forcing would silently keep
    # bypassing the size ceiling with no fresh confirmation
    cfg.confirm_over_gb = 100 / 1e9
    add_urls(store, [f"{server}/rates.json.gz"])
    drain(cfg, store)
    rec = next(r for r in store.list_urls() if "rates.json.gz" in r["url"])
    assert rec["status"] == "oversize"
    store.force_size_requeue(rec["id"])            # user forces it
    assert next(r for r in store.list_urls() if r["id"] == rec["id"])["force_size"] is True
    # simulate a transient failure of the forced attempt (not oversize)
    store.update_url(rec["id"], status="failed", error="connection reset")
    # plain retry / retry-all-failed / re-paste must each clear the override
    store.set_url_status_by_id(rec["id"], "queued")
    assert next(r for r in store.list_urls() if r["id"] == rec["id"])["force_size"] is False
    store.update_url(rec["id"], status="failed", error="x")
    store.force_size_requeue(rec["id"]); store.update_url(rec["id"], status="failed", error="x")
    store.requeue_failed()
    assert next(r for r in store.list_urls() if r["id"] == rec["id"])["force_size"] is False


def test_gatsby_hub_page_crawled(cfg, store, tmp_path):
    # Sapphire/HealthSparq-style hubs (Blue KC): empty HTML page, file list in
    # Gatsby static-query JSON. Suppressed entries must be respected.
    root = tmp_path / "hub"
    (root / "page-data" / "index").mkdir(parents=True)
    (root / "page-data" / "sq" / "d").mkdir(parents=True)
    # real Gatsby builds ship framework .json links in the HTML — these must
    # be filtered out, not queued as "files" (they'd short-circuit the crawl)
    (root / "index.html").write_text(
        '<!doctype html><html><head>'
        '<link rel="manifest" href="/manifest.json">'
        '<link as="fetch" rel="preload" href="/page-data/index/page-data.json">'
        '</head><body><div id="app"></div></body></html>'
    )
    (root / "rates.json.gz").write_bytes(gzip.compress((FIXTURES / "innetwork_mixed.json").read_bytes()))

    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **k):
            super().__init__(*a, directory=str(root), **k)

        def log_message(self, *a):
            pass

    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    (root / "page-data" / "index" / "page-data.json").write_text(json.dumps({"staticQueryHashes": ["abc123"]}))
    (root / "page-data" / "sq" / "d" / "abc123.json").write_text(json.dumps({
        "data": {"allTocsJson": {"edges": [
            {"node": {"url": f"{base}/rates.json.gz", "is_suppressed": False}},
            {"node": {"url": f"{base}/pulled.json.gz", "is_suppressed": True}},
        ]}}}))
    try:
        add_urls(store, [base])
        drain(cfg, store)
        recs = {dedup_key(r["url"]): r for r in store.list_urls()}
        hub = recs[dedup_key(base)]
        assert hub["status"] == "done" and hub["kind"] == "page"
        assert hub["child_count"] == 1  # suppressed entry NOT queued
        child = recs[dedup_key(f"{base}/rates.json.gz")]
        assert child["status"] == "done" and child["rows_emitted"] > 0
        assert dedup_key(f"{base}/pulled.json.gz") not in recs
    finally:
        httpd.shutdown()


def test_blobs_listing_api_expands_rate_files_first(cfg, store, server, http_root):
    # UHC/Optum-style portal API: {"blobs": [{name, downloadUrl}, ...]}.
    # Rate files queue first; allowed-amounts and drug-pricing (NDC /
    # prescription-drugs) entries are not queued at all.
    (http_root / "blobs.json").write_text(json.dumps({"blobs": [
        {"name": "2026-07-01_x_allowed-amounts.json.gz", "downloadUrl": f"{server}/nope.json.gz"},
        {"name": "2026-07-01_p_PPO-NDC_in-network-rates.json.gz", "downloadUrl": f"{server}/drugs.json.gz"},
        {"name": "2026-07-01_q_prescription-drugs_in-network.json.gz", "downloadUrl": f"{server}/drugs2.json.gz"},
        {"name": "2026-07-01_employer_index.json", "downloadUrl": f"{server}/companion.json"},
        {"name": "2026-07-01_net_in-network-rates.json.gz", "downloadUrl": f"{server}/rates.json.gz"},
    ]}))
    add_urls(store, [f"{server}/blobs.json"])
    drain(cfg, store)
    recs = {dedup_key(r["url"]): r for r in store.list_urls()}
    listing = recs[dedup_key(f"{server}/blobs.json")]
    assert listing["status"] == "done" and listing["kind"] == "toc"
    assert listing["child_count"] == 2  # allowed-amounts entry never queued
    assert recs[dedup_key(f"{server}/rates.json.gz")]["status"] == "done"
    assert dedup_key(f"{server}/nope.json.gz") not in recs
    assert dedup_key(f"{server}/drugs.json.gz") not in recs    # NDC file not queued
    assert dedup_key(f"{server}/drugs2.json.gz") not in recs   # prescription-drugs not queued
    # in-network child was queued (lower id) before the index child
    assert recs[dedup_key(f"{server}/rates.json.gz")]["id"] < recs[dedup_key(f"{server}/companion.json")]["id"]


def test_react_portal_root_found_via_blobs_probe(cfg, store, http_root):
    # a React portal page: no links in HTML, but /api/v1/uhc/blobs/ exists
    root = http_root / "portal2"
    (root / "api" / "v1" / "uhc" / "blobs").mkdir(parents=True)
    (root / "index.html").write_text("<!doctype html><html><body><div id=root></div></body></html>")
    (root / "rates.json.gz").write_bytes(gzip.compress((FIXTURES / "innetwork_mixed.json").read_bytes()))

    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **k):
            super().__init__(*a, directory=str(root), **k)

        def log_message(self, *a):
            pass

    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    (root / "api" / "v1" / "uhc" / "blobs" / "index.html").write_text(json.dumps({"blobs": [
        {"name": "n_in-network-rates.json.gz", "downloadUrl": f"{base}/rates.json.gz"},
    ]}))
    try:
        add_urls(store, [base])
        drain(cfg, store)
        recs = {dedup_key(r["url"]): r for r in store.list_urls()}
        portal = recs[dedup_key(base)]
        assert portal["status"] == "done" and portal["kind"] == "page"
        assert recs[dedup_key(f"{base}/rates.json.gz")]["status"] == "done"
        assert recs[dedup_key(f"{base}/rates.json.gz")]["rows_emitted"] > 0
    finally:
        httpd.shutdown()


def test_parallel_ingest_matches_serial(cfg, store, server, http_root, tmp_path):
    # parallel_ingests=2: two parser processes, DB writes stay in this process.
    # Outputs must match the serial path exactly.
    import gzip as _gzip
    import json as _json

    doc = _json.loads((FIXTURES / "innetwork_mixed.json").read_text())
    doc["reporting_entity_name"] = "Second Payer LLC"
    (http_root / "rates_b.json.gz").write_bytes(_gzip.compress(_json.dumps(doc).encode()))
    cfg.parallel_ingests = 2
    add_urls(store, [f"{server}/rates.json.gz", f"{server}/rates_b.json.gz",
                     f"{server}/companion.json"])
    drain(cfg, store)
    recs = {dedup_key(r["url"]): r for r in store.list_urls()}
    a = recs[dedup_key(f"{server}/rates.json.gz")]
    b = recs[dedup_key(f"{server}/rates_b.json.gz")]
    assert a["status"] == "done" and b["status"] == "done"
    assert a["rows_emitted"] == b["rows_emitted"] > 0  # same fixture -> same rows
    with store.connect() as con:
        total = con.execute("SELECT count(*) FROM rates").fetchone()[0]
        payers = {r[0] for r in con.execute("SELECT DISTINCT payer FROM rates").fetchall()}
        dedup = con.execute("SELECT count(*) FROM rates_dedup").fetchone()[0]
    assert total == a["rows_emitted"] + b["rows_emitted"]
    assert "Second Payer LLC" in payers and dedup > 0
    assert not any(f.name.endswith(".progress") for f in cfg.downloads_dir.iterdir())


def test_rollups_rebuilt_once_queue_drains(cfg, store, server):
    # queue ingests defer the (expensive, full) rollup rebuild; it must still
    # run when the queue goes idle so dashboards see the new data
    add_urls(store, [f"{server}/rates.json.gz", f"{server}/companion.json"])
    drain(cfg, store)
    with store.connect() as con:
        raw = con.execute("SELECT count(*) FROM rates").fetchone()[0]
        dedup = con.execute("SELECT count(*) FROM rates_dedup").fetchone()[0]
    assert raw > 0 and dedup > 0  # rollup view populated after the drain


def test_dedup_key_keeps_identity_params_drops_signature_params():
    # signed CDN re-pastes collapse to one row...
    a = dedup_key("https://x.mrf.bcbs.com/f.json.gz?&Expires=1&Signature=abc&Key-Pair-Id=K1")
    b = dedup_key("https://x.mrf.bcbs.com/f.json.gz?&Expires=2&Signature=xyz&Key-Pair-Id=K2")
    assert a == b
    # ...but download endpoints serving DIFFERENT files via query stay distinct
    assert dedup_key("https://p.com/dl?file=a.json") != dedup_key("https://p.com/dl?file=b.json")
    # Azure SAS params are volatile too
    assert dedup_key("https://x.blob.core.windows.net/c/f.json?sv=1&se=2&sp=r&sig=q") == \
           dedup_key("https://x.blob.core.windows.net/c/f.json?sv=9&se=8&sp=r&sig=z")
    # ...but the short generic names (st, se, token, …) are stripped ONLY on a
    # provably-signed URL: a listing whose files differ only in ?st=NY or
    # ?token=<file-id> must NOT collapse to one key (silent under-ingestion)
    assert dedup_key("https://p.com/rates?st=NY") != dedup_key("https://p.com/rates?st=MO")
    assert dedup_key("https://p.com/dl?token=fileA") != dedup_key("https://p.com/dl?token=fileB")
    # with a signature present, st IS a SAS param again and still dedups
    assert dedup_key("https://x.net/f.json?st=2026-01-01&sig=a") == \
           dedup_key("https://x.net/f.json?st=2026-02-02&sig=b")


def test_urls_api_never_splits_on_commas(cfg, store):
    from fastapi.testclient import TestClient
    from mrfx.api import create_app

    client = TestClient(create_app(cfg, store))
    url = "https://p.com/mrf/2026-07-01_Payer,-Inc._in-network.json.gz?ids=1,2"
    r = client.post("/api/urls", json={"urls": url + "\n"}).json()
    assert r == {"added": 1, "skipped": 0, "invalid": 0}
    (rec,) = store.list_urls()
    assert rec["url"] == url  # comma intact, one row


def test_retry_failed_is_bulk_and_unbounded(cfg, store, server):
    # failures beyond any display window must still be retried
    for i in range(4):
        add_urls(store, [f"{server}/missing_{i}.json.gz"])
    drain(cfg, store)
    assert store.url_queue_counts().get("failed") == 4
    assert store.requeue_failed() == 4
    assert store.url_queue_counts().get("queued") == 4


def test_user_transition_guards(cfg, store, server):
    add_urls(store, [f"{server}/rates.json.gz"])
    drain(cfg, store)
    (rec,) = store.list_urls()
    assert rec["status"] == "done"
    # done rows anchor duplicate detection — user actions must not touch them
    assert store.set_url_status_by_id(rec["id"], "skipped") is False
    assert store.set_url_status_by_id(rec["id"], "queued") is False
    add_urls(store, [f"{server}/missing.json.gz"])
    drain(cfg, store)
    failed = next(r for r in store.list_urls() if "missing" in r["url"])
    assert store.set_url_status_by_id(failed["id"], "queued") is True  # retry a failure


def test_reexpanding_toc_does_not_undo_user_skip(cfg, store, server):
    add_urls(store, [f"{server}/toc.json"])
    drain(cfg, store)
    child = next(r for r in store.list_urls() if dedup_key(r["url"]) == dedup_key(f"{server}/rates.json.gz"))
    # user skips the (done) child? not allowed; simulate skipping a queued one:
    # force it back to skipped state directly to model an explicit user cancel
    with store.write_lock, store.connect() as con:
        con.execute("UPDATE url_queue SET status='skipped' WHERE id = ?", [child["id"]])
    # re-adding the TOC re-queues the TOC row itself but must NOT revive the
    # skipped child during expansion
    with store.write_lock, store.connect() as con:
        con.execute("UPDATE url_queue SET status='failed' WHERE dedup_key = ?",
                    [dedup_key(f"{server}/toc.json")])
    add_urls(store, [f"{server}/toc.json"])
    drain(cfg, store)
    child_after = next(r for r in store.list_urls() if r["id"] == child["id"])
    assert child_after["status"] == "skipped"
    # but pasting the child's URL DIRECTLY revives it
    add_urls(store, [f"{server}/rates.json.gz"])
    child_after = next(r for r in store.list_urls() if r["id"] == child["id"])
    assert child_after["status"] == "queued"


def test_add_urls_expands_month_placeholder(cfg, store):
    import datetime as dt

    add_urls(store, ["https://x.com/{FIRST_OF_MONTH}_payer_index.json"])
    (rec,) = store.list_urls()
    first = dt.date.today().replace(day=1).isoformat()
    assert rec["url"] == f"https://x.com/{first}_payer_index.json"


def test_filename_for_is_safe_and_distinct():
    a = filename_for("https://x.com/payerA/2026-06_in-network-rates_1_of_2.json.gz?sig=1")
    b = filename_for("https://x.com/payerB/2026-06_in-network-rates_1_of_2.json.gz")
    assert a != b                      # same basename, different path -> distinct
    assert a.endswith(".json.gz")
    c = filename_for("https://x.com/app/public/")
    assert c.endswith(".json")         # page-ish URL still gets a usable name


def test_store_connect_retries_transient_lock(cfg, store, monkeypatch):
    # a user opening the .duckdb file read-only (CLI/BI tool) can hold the
    # file lock for an instant; connect() must ride it out, not crash a run
    import duckdb as _duckdb

    real_connect = _duckdb.connect
    fails = {"n": 2}

    def flaky(path, *a, **k):
        if isinstance(path, str) and path.endswith("mrfx.duckdb") and fails["n"] > 0:
            fails["n"] -= 1
            raise _duckdb.IOException("IO Error: Could not set lock on file (simulated)")
        return real_connect(path, *a, **k)

    monkeypatch.setattr("mrfx.store.duckdb.connect", flaky)
    assert store.url_queue_counts() is not None  # survives two failed attempts
    assert fails["n"] == 0


def test_download_resumes_partial_file(cfg, tmp_path):
    # a .part left by a crash/network drop resumes via HTTP Range instead of
    # re-downloading; the final hash must equal the full file's
    import hashlib
    import http.server as hs

    payload = gzip.compress((FIXTURES / "innetwork_mixed.json").read_bytes()) * 3
    served_ranges = []

    class RangeHandler(hs.BaseHTTPRequestHandler):
        def do_GET(self):
            rng = self.headers.get("Range")
            if rng:
                start = int(rng.split("=")[1].rstrip("-"))
                served_ranges.append(start)
                body = payload[start:]
                self.send_response(206)
                self.send_header("Content-Range", f"bytes {start}-{len(payload)-1}/{len(payload)}")
            else:
                body = payload
                self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), RangeHandler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        from mrfx.fetch import download, filename_for

        url = f"http://127.0.0.1:{httpd.server_address[1]}/f.json.gz"
        dest = cfg.downloads_dir / filename_for(url)
        cut = len(payload) // 3
        Path(str(dest) + ".part").write_bytes(payload[:cut])  # simulate the crash
        sha, _ = download(cfg, url, dest)
        assert served_ranges == [cut]                          # resumed, not restarted
        assert sha == hashlib.sha256(payload).hexdigest()      # bytes identical
        assert dest.read_bytes() == payload
    finally:
        httpd.shutdown()


def test_download_disk_space_guard(cfg, server, monkeypatch):
    import collections
    import shutil as _shutil

    from mrfx.fetch import DownloadError, download, filename_for

    fake = collections.namedtuple("usage", "total used free")(100 << 30, 100 << 30, 1 << 30)
    monkeypatch.setattr("mrfx.fetch.shutil.disk_usage", lambda p: fake)  # 1GB free
    url = f"{server}/rates.json.gz"
    dest = cfg.downloads_dir / filename_for(url)
    with pytest.raises(DownloadError) as ei:
        download(cfg, url, dest)
    assert "disk space" in str(ei.value)


def test_404_on_current_month_url_queues_previous_month(cfg, store, server):
    import datetime as dt

    today = dt.date.today()
    cur = today.replace(day=1).isoformat()
    prev = (today.replace(day=1) - dt.timedelta(days=1)).replace(day=1).isoformat()
    add_urls(store, [f"{server}/{cur}_payer_index.json"])   # 404s on the test server
    drain(cfg, store)
    recs = store.list_urls()
    cur_row = next(r for r in recs if cur in r["url"])
    assert cur_row["status"] == "failed"
    assert "last month's version" in cur_row["error"]
    prev_row = next(r for r in recs if prev in r["url"])    # fallback was queued
    assert prev_row["status"] == "failed"                    # (also 404s here — fine)
    assert "last month" not in (prev_row["error"] or "")     # no infinite fallback chain


def test_unknown_json_link_container_lifts_urls(cfg, store, server, http_root):
    # BCBST-style custom wrapper: {"TOC_Files": [...]} — not HTML, not a CMS
    # shape, but it lists MRF links; the app must follow them
    (http_root / "directory.json").write_text(json.dumps(
        {"TOC_Files": [f"{server}/toc.json"], "note": "custom payer wrapper"}))
    add_urls(store, [f"{server}/directory.json"])
    drain(cfg, store)
    recs = {dedup_key(r["url"]): r for r in store.list_urls()}
    wrapper = recs[dedup_key(f"{server}/directory.json")]
    assert wrapper["status"] == "done" and wrapper["child_count"] == 1
    assert recs[dedup_key(f"{server}/toc.json")]["status"] == "done"        # TOC expanded
    assert recs[dedup_key(f"{server}/rates.json.gz")]["status"] == "done"   # rates landed


def test_page_lifts_data_attr_links_with_spaces(cfg):
    # CareFirst-style: the real URL sits in data-key="..." behind an
    # href="javascript:void(0)" button, and the filename contains spaces —
    # both must be handled (space -> %20) or the index is invisible
    from mrfx.fetch import extract_links_from_page

    page = cfg.inbox_dir / "carefirst_like.html"
    page.write_text(
        '<html><body><table><tr><td><a class="dwd" href="javascript:void(0);" '
        'data-key="https://blobs.example.com/mrf-files/2026-07-06_carefirst ppo_index.json">'
        "Download</a></td></tr></table></body></html>"
    )
    links = extract_links_from_page(page, "https://individual.carefirst.com/x.page", 50)
    assert links == ["https://blobs.example.com/mrf-files/2026-07-06_carefirst%20ppo_index.json"]


def test_page_lifts_quoted_relative_json_config_paths(cfg, store, server, http_root):
    # Cigna-style: the page embeds its manifest as a quoted JS string, not a
    # link — "/static/mrf/latest.json" must still be found and followed
    (http_root / "static").mkdir(exist_ok=True)
    (http_root / "static" / "latest.json").write_text(json.dumps(
        {"mrfs": [{"kind": "TOC", "files": [{"url": f"{server}/toc.json?Expires=9&Policy=abc&Signature=x"}]}]}))
    (http_root / "cigna_like.html").write_text(
        '<!doctype html><html><body><script>settings={"cigna_mrf":'
        '{"manifest":"/static/latest.json"}}</script></body></html>')
    add_urls(store, [f"{server}/cigna_like.html"])
    drain(cfg, store)
    recs = {dedup_key(r["url"]): r for r in store.list_urls()}
    page = recs[dedup_key(f"{server}/cigna_like.html")]
    assert page["status"] == "done" and page["child_count"] == 1
    manifest = recs[dedup_key(f"{server}/static/latest.json")]
    assert manifest["status"] == "done" and manifest["child_count"] == 1  # signed TOC lifted
    assert recs[dedup_key(f"{server}/rates.json.gz")]["status"] == "done"  # cascade to rates


def test_403_for_tool_ua_retries_as_browser(cfg):
    # some CDNs fronting public MRF data (BCBS South Carolina's CloudFront)
    # 403 any non-browser user agent: the download must fall back to a
    # browser UA instead of failing
    import hashlib
    import http.server as hs

    payload = gzip.compress(b'{"in_network": []}')
    agents = []

    class PickyHandler(hs.BaseHTTPRequestHandler):
        def do_GET(self):
            ua = self.headers.get("User-Agent", "")
            agents.append(ua)
            if "Chrome" not in ua:
                self.send_response(403)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *a):
            pass

    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), PickyHandler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        from mrfx.fetch import download, filename_for

        url = f"http://127.0.0.1:{httpd.server_address[1]}/index.json"
        dest = cfg.downloads_dir / filename_for(url)
        sha, _ = download(cfg, url, dest)
        assert sha == hashlib.sha256(payload).hexdigest()
        assert "mrf-explorer" in agents[0]      # we identified honestly first
        assert "Chrome" in agents[-1]           # then fell back to a browser UA
    finally:
        httpd.shutdown()


def test_truncated_download_resumes_and_completes(cfg):
    # a server that drops the connection halfway must not yield a corrupt
    # file: the short read is detected, and the retry resumes from the .part
    import hashlib
    import http.server as hs
    import os as _os

    # must be bigger than download()'s 1MB stream chunk: bytes only reach the
    # .part in whole chunks, and the resume picks up from what was flushed
    payload = _os.urandom(3 << 20)
    calls = []

    class FlakyHandler(hs.BaseHTTPRequestHandler):
        def do_GET(self):
            rng = self.headers.get("Range")
            start = int(rng.split("=")[1].rstrip("-")) if rng else 0
            calls.append(start)
            body = payload[start:]
            if not rng:  # first request: advertise full size, send only half
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body[: len(body) // 2])
                self.wfile.flush()
                self.connection.close()
                return
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{len(payload)-1}/{len(payload)}")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FlakyHandler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        from mrfx.fetch import download, filename_for

        url = f"http://127.0.0.1:{httpd.server_address[1]}/f.json.gz"
        dest = cfg.downloads_dir / filename_for(url)
        sha, _ = download(cfg, url, dest)
        assert sha == hashlib.sha256(payload).hexdigest()   # complete + intact
        assert calls[0] == 0 and len(calls) >= 2 and calls[1] > 0  # resumed mid-file
    finally:
        httpd.shutdown()


def test_js_rendered_portal_links_harvested(cfg):
    # JavaScript-only portal: the served HTML has NO links; the page's script
    # fetches an API and injects some into the DOM, while another lives only
    # inside the API response body. The headless render must find both.
    pytest.importorskip("playwright.sync_api")
    import http.server as hs

    class Portal(hs.BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/portal":
                body = (b"<html><body><div id='f'></div><script>"
                        b"fetch('/api/list').then(r=>r.json()).then(d=>{"
                        b"const el=document.getElementById('f');"
                        b"d.dom_files.forEach(u=>{const a=document.createElement('a');"
                        b"a.href=u;a.textContent=u;el.appendChild(a);});});"
                        b"</script></body></html>")
                ctype = "text/html"
            elif self.path == "/api/list":
                port = self.server.server_address[1]
                body = json.dumps({
                    "dom_files": ["/files/visible_in-network-rates.json.gz"],
                    "reporting": [{"location":
                        f"http://127.0.0.1:{port}/files/api-only_in-network-rates.json.gz"}],
                }).encode()
                ctype = "application/json"
            else:
                self.send_response(404); self.end_headers(); return
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Portal)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        from mrfx.render import render_page_links

        base = f"http://127.0.0.1:{httpd.server_address[1]}"
        links = render_page_links(cfg, f"{base}/portal", 50)
        assert f"{base}/files/visible_in-network-rates.json.gz" in links      # DOM-injected
        assert f"{base}/files/api-only_in-network-rates.json.gz" in links     # response-only
    finally:
        httpd.shutdown()


def test_allowed_amounts_only_toc_skipped_not_failed(cfg, store):
    # TPA employer-group TOCs (Excellus/HealthSparq) often list ONLY an
    # allowed_amount_file — that's the payer's choice, not an error: the row
    # must read 'skipped' with a plain-language reason
    import http.server as hs

    toc = json.dumps({
        "reporting_entity_name": "Some TPA",
        "reporting_structure": [{
            "reporting_plans": [{"plan_name": "X", "plan_id_type": "EIN", "plan_id": "1"}],
            "allowed_amount_file": {"description": "aa", "location": "https://x.example/aa.json.zip"},
        }],
        "version": "2.0.0",
    }).encode()

    class H(hs.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", str(len(toc)))
            self.end_headers()
            self.wfile.write(toc)

        def log_message(self, *a):
            pass

    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{httpd.server_address[1]}/group_index.json"
        add_urls(store, [url])
        run_queue(cfg, store, drain=True)
        (rec,) = store.list_urls()
        assert rec["status"] == "skipped" and rec["kind"] == "toc"
        assert "allowed-amounts" in rec["error"] and "no negotiated-rate" in rec["error"]
    finally:
        httpd.shutdown()


def test_resume_restarts_when_content_changed(cfg):
    # a payer republishing DIFFERENT bytes under the same URL between
    # attempts must NOT get spliced into the old .part: the saved ETag rides
    # an If-Range header, the server answers 200 (full new content), and the
    # download restarts clean
    import hashlib
    import http.server as hs
    import os as _os

    old = _os.urandom(3 << 20)
    new = _os.urandom(2 << 20)
    state = {"content": old, "etag": '"v1"', "if_range_seen": []}

    class ChangingHandler(hs.BaseHTTPRequestHandler):
        def do_GET(self):
            body = state["content"]
            if not self.headers.get("Range"):  # first attempt: truncate at half
                self.send_response(200)
                self.send_header("ETag", state["etag"])
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body[: len(body) // 2])
                self.wfile.flush()
                self.connection.close()
                state["content"], state["etag"] = new, '"v2"'  # republish!
                return
            state["if_range_seen"].append(self.headers.get("If-Range"))
            # validator no longer matches -> full 200 with the NEW content
            self.send_response(200)
            self.send_header("ETag", state["etag"])
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), ChangingHandler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        from mrfx.fetch import download, filename_for

        url = f"http://127.0.0.1:{httpd.server_address[1]}/f.json.gz"
        dest = cfg.downloads_dir / filename_for(url)
        sha, _ = download(cfg, url, dest)
        assert state["if_range_seen"] == ['"v1"']          # validator was sent
        assert sha == hashlib.sha256(new).hexdigest()      # new content, no splice
        assert dest.read_bytes() == new
    finally:
        httpd.shutdown()


def test_stall_deadline_fails_fast_and_keeps_partial(cfg):
    # a CDN that closes the connection early on EVERY attempt and won't resume
    # must not hold a downloader slot for an hour (download_retries resets on any
    # byte progress, so only this wall-clock stall deadline bounds it). It should
    # give up within ~the deadline, keep the bytes it got, and say so plainly so
    # the OTHER queued files keep flowing.
    import http.server as hs
    import os as _os
    import time as _time

    cfg.download_stall_seconds = 1.0   # tiny deadline for the test
    cfg.download_retries = 6           # would loop long without the deadline
    payload = _os.urandom(4 << 20)
    calls = []

    class EarlyClose(hs.BaseHTTPRequestHandler):
        def do_GET(self):
            calls.append(1)
            # always advertise the FULL size but send only a few KB then hang up,
            # and ignore Range (answer 200) — the un-resumable early-close class
            self.send_response(200)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload[:4096])
            self.wfile.flush()
            self.connection.close()

        def log_message(self, *a):
            pass

    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), EarlyClose)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        from mrfx.fetch import DownloadError, download, filename_for

        url = f"http://127.0.0.1:{httpd.server_address[1]}/f.json.gz"
        dest = cfg.downloads_dir / filename_for(url)
        part = dest.with_suffix(dest.suffix + ".part")
        t0 = _time.monotonic()
        with pytest.raises(DownloadError) as ei:
            download(cfg, url, dest)
        elapsed = _time.monotonic() - t0
        # fails FAST (deadline + at most one attempt's worth), not 75 min
        assert elapsed < 60, f"stall deadline did not fire ({elapsed:.0f}s)"
        assert "no download progress" in str(ei.value)
        assert getattr(ei.value, "retryable", False) is True   # kept, not dead
        assert part.exists()   # .part retained for a later retry, not discarded
    finally:
        httpd.shutdown()


def test_clear_queued_and_cancel_downloading(store):
    """The two queue-control buttons: clear-queued bulk-skips the waiting backlog
    (retryable, nothing destroyed); cancel-downloading flips in-flight rows and
    records their ids so the running downloader threads abort."""
    from mrfx.fetch import add_urls

    add_urls(store, ["https://a.example/1.json.gz", "https://b.example/2.json.gz",
                     "https://c.example/3.json.gz"])
    # simulate one row mid-download (next_queued_url flips it to 'downloading')
    dl = store.next_queued_url()
    assert {u["url"]: u["status"] for u in store.list_urls()}[dl["url"]] == "downloading"

    cleared = store.clear_queued()
    assert cleared == 2            # the two still-queued rows
    stopped = store.cancel_all_downloading()
    assert stopped == 1
    assert store.is_download_canceled(dl["id"]) is True

    rows = {u["url"]: u for u in store.list_urls()}
    assert all(rows[u]["status"] == "skipped" for u in rows)   # all set aside
    # the cancel flag clears once honored, so a retry isn't instantly re-canceled
    store.clear_download_cancel(dl["id"])
    assert store.is_download_canceled(dl["id"]) is False


def test_download_aborts_mid_stream_on_cancel(cfg):
    """A cancel_check that flips True mid-download must abort promptly, keep the
    partial (retryable), and raise a canceled DownloadError — so 'stop downloads'
    frees the slot instead of waiting for a huge file to finish."""
    import http.server as hs
    import time as _time

    payload_sent = {"n": 0}

    class Slow(hs.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", str(50 << 20))
            self.end_headers()
            try:
                for _ in range(200):
                    self.wfile.write(b"x" * (512 << 10)); self.wfile.flush()
                    payload_sent["n"] += 1
                    _time.sleep(0.05)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, *a):
            pass

    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Slow)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        from mrfx.fetch import DownloadError, download, filename_for

        url = f"http://127.0.0.1:{httpd.server_address[1]}/f.json.gz"
        dest = cfg.downloads_dir / filename_for(url)
        part = dest.with_suffix(dest.suffix + ".part")
        state = {"cancel": False}
        # cancel after a couple of chunks land
        def check():
            return payload_sent["n"] >= 2
        t0 = _time.monotonic()
        with pytest.raises(DownloadError) as ei:
            download(cfg, url, dest, cancel_check=check)
        assert _time.monotonic() - t0 < 20
        assert getattr(ei.value, "canceled", False) is True
        assert part.exists()   # partial kept for a later resume
    finally:
        httpd.shutdown()


def test_add_urls_accepts_bare_domain_and_rejects_garbage(store):
    """A user pasting a portal address from the browser bar without the scheme
    ('transparency-in-coverage.uhc.com') must be accepted (https:// assumed),
    not rejected as 'not a valid link'. Real garbage / bare filenames still fail."""
    from mrfx.fetch import _normalize_url, add_urls

    assert _normalize_url("transparency-in-coverage.uhc.com") == \
        "https://transparency-in-coverage.uhc.com"
    assert _normalize_url("example.com/mrf/latest.json") == \
        "https://example.com/mrf/latest.json"
    assert _normalize_url("//cdn.example.com/x.json") == "https://cdn.example.com/x.json"
    assert _normalize_url("rates.json") is None        # bare filename, not a host
    assert _normalize_url("just some text") is None

    r = add_urls(store, ["transparency-in-coverage.uhc.com", "not a url", ""])
    assert r["added"] == 1 and r["invalid"] == 1        # blank line isn't counted
    (row,) = [u for u in store.list_urls()
              if u["url"] == "https://transparency-in-coverage.uhc.com"]
    assert row["status"] == "queued"


def test_no_range_server_completes_after_bounded_200_refusals(cfg, monkeypatch):
    """A server that NEVER supports Range (always answers a resume with a full
    200) must still COMPLETE. The preserve-partial guard refuses a range-ignored
    200 for a large .part to protect progress, but bounded: after
    _KEEP_200_MAX_REFUSALS it accepts the 200 and restarts, so the file finishes
    instead of failing forever (audit HIGH-1 regression)."""
    import hashlib
    import http.server as hs
    import os as _os

    import mrfx.fetch as F
    monkeypatch.setattr(F, "_KEEP_PART_ON_200_BYTES", 1000)  # trip with a small part

    payload = _os.urandom(5000)
    calls = []

    class NoRange(hs.BaseHTTPRequestHandler):
        def do_GET(self):
            calls.append(self.headers.get("Range"))
            # ALWAYS ignore Range → full 200, every time
            self.send_response(200)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *a):
            pass

    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), NoRange)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        from mrfx.fetch import download, filename_for

        url = f"http://127.0.0.1:{httpd.server_address[1]}/f.json.gz"
        dest = cfg.downloads_dir / filename_for(url)
        part = dest.with_suffix(dest.suffix + ".part")
        part.parent.mkdir(parents=True, exist_ok=True)
        part.write_bytes(payload[:2000])   # a large (>threshold) prior partial

        sha, _ = download(cfg, url, dest)
        assert sha == hashlib.sha256(payload).hexdigest()   # COMPLETED, intact
        # it refused the 200 a bounded number of times, then accepted the restart
        assert len(calls) >= 2
    finally:
        httpd.shutdown()


def test_wall_clock_cap_sets_aside_a_too_slow_download(cfg):
    """A download that keeps trickling in (advancing, so the no-progress stall
    deadline never fires) must not hold a slot for hours. The hard wall-clock cap
    sets it aside — keeping its partial — so other files can ingest."""
    import http.server as hs
    import time as _time

    cfg.download_stall_seconds = 999   # isolate the CAP: never trip the stall
    cfg.download_max_seconds = 2       # tiny cap for the test
    cfg.download_retries = 6

    class SlowTrickle(hs.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", str(50 << 20))  # claim 50 MB
            self.end_headers()
            # dribble forever: advances the .part (no stall) but never finishes
            try:
                for _ in range(1000):
                    self.wfile.write(b"x" * (256 << 10))
                    self.wfile.flush()
                    _time.sleep(0.2)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, *a):
            pass

    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), SlowTrickle)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        from mrfx.fetch import DownloadError, download, filename_for

        url = f"http://127.0.0.1:{httpd.server_address[1]}/f.json.gz"
        dest = cfg.downloads_dir / filename_for(url)
        part = dest.with_suffix(dest.suffix + ".part")
        t0 = _time.monotonic()
        with pytest.raises(DownloadError) as ei:
            download(cfg, url, dest)
        elapsed = _time.monotonic() - t0
        assert elapsed < 30, f"cap did not fire ({elapsed:.0f}s)"
        assert getattr(ei.value, "retryable", False) is True
        assert part.exists()   # partial kept for resume
        # flagged so the caller REQUEUES it rather than parking it in 'failed':
        # this file was still progressing and only yielded its slot, so it must
        # resume by itself instead of waiting for a human to press retry
        assert getattr(ei.value, "set_aside", False) is True
        assert "resumes from here automatically" in str(ei.value)
        assert "press retry" not in str(ei.value).lower()
    finally:
        httpd.shutdown()


def test_dribbling_resume_trips_stall_not_the_wall_cap(cfg):
    """A CDN that RESUMES (206) but dribbles only a few KB per connection before
    closing used to reset the stall clock on every tiny advance, so the download
    stayed 'progressing' and held its slot until the multi-hour wall-clock cap.
    Meaningful-progress gating must make it trip the fast stall deadline instead
    (the UHC-giant-file class: 0.4 GB / 15.3 GB, inching nowhere)."""
    import http.server as hs
    import time as _time

    cfg.download_stall_seconds = 2      # fast stall deadline
    cfg.download_max_seconds = 60       # cap is the SLOW backstop — must NOT be what fires
    cfg.download_retries = 6

    class DribbleResume(hs.BaseHTTPRequestHandler):
        def do_GET(self):
            total = 50 << 20
            rng = self.headers.get("Range", "")
            start = int(rng.split("=")[1].split("-")[0]) if rng.startswith("bytes=") else 0
            self.send_response(206 if start else 200)
            if start:
                self.send_header("Content-Range", f"bytes {start}-{total - 1}/{total}")
            self.send_header("Content-Length", str(total - start))
            self.end_headers()
            try:                                  # 8 KB, well under the 1 MB floor
                self.wfile.write(b"x" * 8192)
                self.wfile.flush()
                self.connection.close()
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, *a):
            pass

    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), DribbleResume)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        from mrfx.fetch import DownloadError, download, filename_for

        url = f"http://127.0.0.1:{httpd.server_address[1]}/f.json.gz"
        dest = cfg.downloads_dir / filename_for(url)
        part = dest.with_suffix(dest.suffix + ".part")
        t0 = _time.monotonic()
        with pytest.raises(DownloadError) as ei:
            download(cfg, url, dest)
        elapsed = _time.monotonic() - t0
        assert elapsed < 30, f"stall deadline did not fire ({elapsed:.0f}s) — cap fired instead"
        assert "no download progress" in str(ei.value)   # stall, not the wall cap
        assert part.exists()                              # dribbled bytes kept for resume
    finally:
        httpd.shutdown()


def test_range_ignored_200_keeps_large_partial(cfg, monkeypatch):
    # a stray full-file 200 answering our RESUME request must not truncate a
    # large .part back to zero (one such response near the end of a multi-GB
    # download used to wipe everything and then fail). Above the keep threshold
    # we skip the 200 and retry for a real 206 resume, preserving the bytes.
    import hashlib
    import http.server as hs
    import os as _os

    import mrfx.fetch as F
    monkeypatch.setattr(F, "_KEEP_PART_ON_200_BYTES", 1000)  # trip with a small part

    payload = _os.urandom(5000)
    calls = []
    state = {"served_200": False}

    class Handler(hs.BaseHTTPRequestHandler):
        def do_GET(self):
            rng = self.headers.get("Range")
            start = int(rng.split("=")[1].rstrip("-")) if rng else 0
            calls.append(start)
            if rng and not state["served_200"]:
                # first RESUME: ignore the range, answer a full 200 (the trap)
                state["served_200"] = True
                self.send_response(200)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return
            if rng:  # later resume: honor it -> completes from the kept bytes
                body = payload[start:]
                self.send_response(206)
                self.send_header("Content-Range", f"bytes {start}-{len(payload)-1}/{len(payload)}")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            # no prior .part in this test path
            self.send_response(200)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *a):
            pass

    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        from mrfx.fetch import download, filename_for

        url = f"http://127.0.0.1:{httpd.server_address[1]}/f.json.gz"
        dest = cfg.downloads_dir / filename_for(url)
        part = dest.with_suffix(dest.suffix + ".part")
        # seed a 2000-byte partial (correct prefix) so a resume is attempted
        part.parent.mkdir(parents=True, exist_ok=True)
        part.write_bytes(payload[:2000])

        sha, _ = download(cfg, url, dest)
        assert sha == hashlib.sha256(payload).hexdigest()   # completed, intact
        # the 200 was skipped WITHOUT truncating: the resume that completed it
        # started from 2000 (the kept bytes), never from 0
        assert 0 not in calls, f"restarted from byte 0 (progress wiped): {calls}"
        assert 2000 in calls
    finally:
        httpd.shutdown()


def test_js_portal_click_through_reveals_links(cfg):
    # Harvard Pilgrim-class portal: the served page has NO links and no
    # auto-fired API call — the list appears only after clicking "View Plan
    # List" (behind a consent overlay). The renderer must click through.
    pytest.importorskip("playwright.sync_api")
    import http.server as hs

    class Portal(hs.BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/portal":
                body = (b"<html><body>"
                        b"<div id='consent' style='position:fixed;inset:0;background:#fff'>"
                        b"<button onclick=\"document.getElementById('consent').remove()\">"
                        b"Accept all cookies</button></div>"
                        b"<button id='v' onclick=\"fetch('/api/plans').then(r=>r.json())"
                        b".then(d=>{const el=document.createElement('div');"
                        b"d.files.forEach(u=>{const a=document.createElement('a');"
                        b"a.href=u;a.textContent=u;el.appendChild(a);});"
                        b"document.body.appendChild(el);})\">View Plan List</button>"
                        b"</body></html>")
                ctype = "text/html"
            elif self.path == "/api/plans":
                body = json.dumps({"files": ["/mrf/2026-07-01_hp_in-network-rates_index.json"]}).encode()
                ctype = "application/json"
            else:
                self.send_response(404); self.end_headers(); return
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Portal)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        from mrfx.render import render_page_links

        base = f"http://127.0.0.1:{httpd.server_address[1]}"
        links = render_page_links(cfg, f"{base}/portal", 50)
        assert f"{base}/mrf/2026-07-01_hp_in-network-rates_index.json" in links
    finally:
        httpd.shutdown()


def test_framework_asset_paths_not_lifted(cfg):
    # the relative-path pattern must lift payer listings (HealthSparq
    # filePath) but NOT web-app plumbing quoted all over rendered pages
    from mrfx.fetch import extract_links_from_text

    text = """
      {"filePath": "2026-07-01/tableOfContents/2026-07-01_x_index.json.gz"}
      "locales/en.json" "i18n/en-US.json" "/wp-content/plugins/a/b.json"
      "/etc.clientlibs/settings/x.json" "/_next/static/chunks/pages.json"
      "https://cdn.example.com/node_modules/pkg/package.json"
      "/static/mrf/latest.json"
    """
    links = extract_links_from_text(text, "https://mrf.example.com/prd/mrf/X/latest_metadata.json", 50)
    assert "https://mrf.example.com/prd/mrf/X/2026-07-01/tableOfContents/2026-07-01_x_index.json.gz" in links
    assert "https://mrf.example.com/static/mrf/latest.json" in links  # Cigna-style kept
    assert not any("locales" in l or "i18n" in l or "wp-content" in l or
                   "clientlibs" in l or "_next" in l or "node_modules" in l
                   for l in links)


def test_speedtest_recommends_segments_on_a_per_connection_throttle(cfg, capsys, monkeypatch):
    """`mrfx speedtest` answers the question a user cannot answer by guessing:
    is this link slow because the payer throttles each connection (splitting it
    helps) or because their own line is full (nothing to tune)? Here the server
    throttles per connection, so it must recommend segments."""
    import http.server as hs
    import os as _os
    import threading as _th
    import time as _time

    from mrfx.cli import cmd_speedtest

    payload = _os.urandom(24_000_000)

    class Throttled(hs.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self):
            rng = self.headers.get("Range", "")
            if not rng.startswith("bytes="):
                self.send_response(200)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers(); self.wfile.write(payload); return
            a, b = rng[6:].split("-")
            start = int(a); end = min(int(b) if b else len(payload) - 1, len(payload) - 1)
            body = payload[start:end + 1]
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{len(payload)}")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            # Hard per-connection rate cap (~3 MB/s a lane), so N lanes really
            # is ~N x and the measured ratio is unambiguous rather than sitting
            # near the command's "is this just noise?" threshold.
            for i in range(0, len(body), 65_536):
                self.wfile.write(body[i:i + 65_536])
                _time.sleep(0.02)

        def log_message(self, *a):
            pass

    httpd = hs.ThreadingHTTPServer(("127.0.0.1", 0), Throttled)
    _th.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{httpd.server_address[1]}/big.json.gz"
        rc = cmd_speedtest(cfg, argparse.Namespace(url=url, mb=6))
        out = capsys.readouterr().out
    finally:
        httpd.shutdown()
    assert rc == 0
    assert "range requests supported" in out
    assert "1 connection" in out and "2 connections" in out
    assert "download_segments:" in out, out          # actionable recommendation
    assert "throttles each connection" in out
    # It recommends a real multi-lane value. WHICH one (2/4/8) depends on where
    # the loopback server's own threading tops out, so don't pin it — the
    # contract under test is "detects the throttle and names a setting > 1".
    assert any(f"download_segments: {n}" in out for n in (2, 4, 8)), out
    assert "download_segments: 1" not in out
    # it must not read the SAME bytes every attempt (a CDN cache would then
    # flatter the later, multi-connection runs and fake a speedup)
    assert "different" in out and "slice per attempt" in out


def test_speedtest_says_so_when_the_server_refuses_ranges(cfg, capsys):
    """No range support means one connection is the only option — say that
    plainly instead of recommending a setting that cannot take effect."""
    import http.server as hs
    import os as _os
    import threading as _th

    from mrfx.cli import cmd_speedtest

    payload = _os.urandom(50_000)

    class NoRange(hs.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self):
            self.send_response(200)          # 200 to a Range == no range support
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers(); self.wfile.write(payload)

        def log_message(self, *a):
            pass

    httpd = hs.ThreadingHTTPServer(("127.0.0.1", 0), NoRange)
    _th.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        rc = cmd_speedtest(cfg, argparse.Namespace(
            url=f"http://127.0.0.1:{httpd.server_address[1]}/f.json.gz", mb=6))
        out = capsys.readouterr().out
    finally:
        httpd.shutdown()
    assert rc == 0
    assert "does not support range requests" in out
    assert "download_segments" in out and "no effect" in out


def test_speedtest_refuses_to_judge_a_file_too_small_to_measure(cfg, capsys):
    """A 1.17x difference over half a megabyte is noise, and acting on it would
    open extra connections for nothing. Decline instead of inventing a verdict."""
    import http.server as hs
    import os as _os
    import threading as _th

    from mrfx.cli import cmd_speedtest

    payload = _os.urandom(400_000)

    class Ranged(hs.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self):
            rng = self.headers.get("Range", "")
            a, b = rng[6:].split("-")
            start = int(a); end = min(int(b) if b else len(payload) - 1, len(payload) - 1)
            body = payload[start:end + 1]
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{len(payload)}")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers(); self.wfile.write(body)

        def log_message(self, *a):
            pass

    httpd = hs.ThreadingHTTPServer(("127.0.0.1", 0), Ranged)
    _th.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        rc = cmd_speedtest(cfg, argparse.Namespace(
            url=f"http://127.0.0.1:{httpd.server_address[1]}/f.json.gz", mb=40))
        out = capsys.readouterr().out
    finally:
        httpd.shutdown()
    assert rc == 0
    assert "too small to measure" in out
    assert "VERDICT" not in out          # no verdict invented from noise


def test_speedtest_names_a_dead_link_instead_of_blaming_range_support(cfg, capsys):
    """A 404 fails the range probe too. Reporting "no range support" there would
    send the user tuning a setting when the real answer is "your link expired" —
    payer links are signed and die after days, so this is the common case."""
    import http.server as hs
    import threading as _th

    from mrfx.cli import cmd_speedtest

    class Gone(hs.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self):
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *a):
            pass

    httpd = hs.ThreadingHTTPServer(("127.0.0.1", 0), Gone)
    _th.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        rc = cmd_speedtest(cfg, argparse.Namespace(
            url=f"http://127.0.0.1:{httpd.server_address[1]}/expired.json.gz", mb=6))
        out = capsys.readouterr().out
    finally:
        httpd.shutdown()
    assert rc == 1
    assert "HTTP 404" in out and "expire" in out
    assert "range requests" not in out          # don't misdiagnose it


def test_silent_server_is_detected_fast_and_the_download_resumes(cfg, tmp_path):
    """Payer CDNs (Anthem's, UHC's) routinely accept a connection and then go
    SILENT mid-file. Time-to-detect equals download_timeout_seconds exactly —
    it was 900s, so every such episode parked a scarce downloader slot for 15
    minutes doing nothing, which is what "downloads stall out" actually was.
    Detection must be fast AND lossless: reconnect, resume from the .part via
    Range, keep every byte already on disk."""
    import hashlib
    import http.server as hs
    import os as _os
    import threading as _th
    import time as _time

    from mrfx.fetch import download

    payload = _os.urandom(3_000_000)
    expect = hashlib.sha256(payload).hexdigest()
    state = {"n": 0, "starts": []}

    class SilentThenGood(hs.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self):
            rng = self.headers.get("Range", "")
            start = int(rng[6:].split("-")[0]) if rng.startswith("bytes=") else 0
            state["n"] += 1
            state["starts"].append(start)
            body = payload[start:]
            self.send_response(206 if start else 200)
            if start:
                self.send_header("Content-Range",
                                 f"bytes {start}-{len(payload) - 1}/{len(payload)}")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if state["n"] == 1:                 # first attempt: partial, then hang
                # MUST exceed the 1 MB read chunk — a connection that dies with
                # less than one chunk buffered never yields it, so nothing
                # reaches the .part and there is no partial to resume from
                self.wfile.write(body[:1_500_000])
                try:
                    self.wfile.flush()
                except Exception:               # noqa: BLE001 — client hung up
                    pass
                _time.sleep(60)
                return
            self.wfile.write(body)

    httpd = hs.ThreadingHTTPServer(("127.0.0.1", 0), SilentThenGood)
    _th.Thread(target=httpd.serve_forever, daemon=True).start()
    cfg.download_timeout_seconds = 3.0
    dest = tmp_path / "big.json"
    try:
        t0 = _time.monotonic()
        sha, _url = download(cfg, f"http://127.0.0.1:{httpd.server_address[1]}/f.json",
                             dest)
        elapsed = _time.monotonic() - t0
    finally:
        httpd.shutdown()

    assert sha == expect                        # every byte, correct order
    assert dest.stat().st_size == len(payload)
    # it RESUMED rather than restarting: the retry asked for a non-zero offset
    assert state["n"] >= 2 and max(state["starts"]) > 0, state["starts"]
    # and it noticed the silence on the timeout, not after some fixed long wait
    assert elapsed < 30, f"took {elapsed:.1f}s — silence detection is not tracking the timeout"


def test_download_timeout_default_is_short_enough_to_free_a_stalled_slot():
    """Guards the value itself. This is a max GAP BETWEEN BYTES, not a cap on
    total download time, so a slow-but-streaming file is unaffected — but every
    minute of it is a minute a dead connection holds a downloader slot."""
    from mrfx.config import MrfxConfig

    assert MrfxConfig().download_timeout_seconds <= 180, (
        "a silent CDN holds a downloader slot for this long; keep it small")


def test_slow_big_file_requeues_itself_instead_of_waiting_for_a_human(cfg, store, tmp_path):
    """The wall-clock cap exists so ONE slow file can't hog a downloader slot —
    it steps aside so others get a turn. But it marked the row 'failed', so the
    file only advanced when a human noticed and pressed retry: one cap-length
    slice per check-in, which is how a large file turned into "3 files every
    couple of days". A file that was still PROGRESSING must go back in the queue
    and resume by itself."""
    import http.server as hs
    import os as _os
    import threading as _th
    import time as _time

    from mrfx.fetch import fetch_url_record

    payload = _os.urandom(4_000_000)
    seen_ranges = []

    class Slow(hs.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self):
            rng = self.headers.get("Range", "")
            start = int(rng[6:].split("-")[0]) if rng.startswith("bytes=") else 0
            seen_ranges.append(start)
            body = payload[start:]
            self.send_response(206 if start else 200)
            if start:
                self.send_header("Content-Range",
                                 f"bytes {start}-{len(payload) - 1}/{len(payload)}")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            for i in range(0, len(body), 1 << 20):     # steady, but slow
                self.wfile.write(body[i:i + (1 << 20)])
                _time.sleep(0.5)

        def log_message(self, *a):
            pass

    httpd = hs.ThreadingHTTPServer(("127.0.0.1", 0), Slow)
    _th.Thread(target=httpd.serve_forever, daemon=True).start()
    cfg.download_max_seconds = 1.0        # cap fires almost immediately
    cfg.download_stall_seconds = 0        # isolate the CAP, not the stall path
    url = f"http://127.0.0.1:{httpd.server_address[1]}/huge.json.gz"
    try:
        store.enqueue_url(url, dedup_key=url)
        rec = store.next_queued_url()
        assert rec is not None
        fetch_url_record(cfg, store, rec)
        row = [u for u in store.list_urls() if u["id"] == rec["id"]][0]
    finally:
        httpd.shutdown()

    # requeued for another pass — NOT parked awaiting a human
    assert row["status"] == "queued", row
    assert "resumes from here automatically" in (row["error"] or "")
    assert "press retry" not in (row["error"] or "").lower()

    # and the bytes it did fetch are kept, so the next pass resumes rather
    # than starting over (that is what makes repeated passes converge)
    parts = list(cfg.downloads_dir.glob("*.part"))
    assert parts and parts[0].stat().st_size > 0, "partial bytes must survive"
