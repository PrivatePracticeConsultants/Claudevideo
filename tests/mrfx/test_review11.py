"""Review 11: the link -> download -> ingest -> skip protocol. Every finding
was reproduced on 1064203 (v0.7.4) by a script under docs/review11_repros/;
each test below pins one, and its comment says what it got on 1064203
("was: ...")."""

from __future__ import annotations

import gzip
import hashlib
import http.server
import json
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from mrfx import fetch
from mrfx.api import create_app
from mrfx.ingest import resolve_choice, scan_inbox
from mrfx.store import Store


# --------------------------------------------------------------- fixtures --

def rate_doc(rate=40.0, n=60, payer="Twin Health", month="2026-05-01", pad=0):
    groups = [{"provider_groups": [{"npi": [1000000000 + i],
                                    "tin": {"type": "ein", "value": f"43{i:07d}"}}],
               "negotiated_prices": [{"negotiated_type": "negotiated", "negotiated_rate": rate,
                                      "billing_class": "professional", "service_code": ["11"],
                                      "billing_code_modifier": ["GP"]}]}
              for i in range(n)]
    d = {"reporting_entity_name": payer, "last_updated_on": month, "version": "2.0.0",
         "in_network": [{"negotiation_arrangement": "ffs", "billing_code_type": "CPT",
                         "billing_code": "97110", "negotiated_rates": groups}]}
    if pad:
        d["in_network"].append({"negotiation_arrangement": "ffs", "billing_code_type": "CPT",
                                "billing_code": "99991", "negotiated_rates": [],
                                "description": "x" * pad})
    return json.dumps(d).encode()


def gz(b: bytes) -> bytes:
    return gzip.compress(b, mtime=0)


class Srv:
    """A local HTTP server whose answer per path is a function of the request
    (the review harness's shape, cut down). routes[path] = callable(req) ->
    (status, headers, body, cut_after or None). `log` holds (path, status,
    Range header, bytes written)."""

    def __init__(self, routes: dict):
        self.routes = routes
        self.log: list[tuple] = []
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def _go(self, head_only=False):
                fn = outer.routes.get(self.path)
                if fn is None:
                    self.send_response(404)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    outer.log.append((self.path, 404, self.headers.get("Range"), 0))
                    return
                status, headers, body, cut = fn(self)
                self.send_response(status)
                for k, v in headers.items():
                    self.send_header(k, v)
                if "Content-Length" not in headers:
                    self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                out = body if cut is None else body[:cut]
                if not head_only:
                    try:
                        self.wfile.write(out)
                        self.wfile.flush()
                    except OSError:
                        pass
                outer.log.append((self.path, status, self.headers.get("Range"),
                                  0 if head_only else len(out)))
                if cut is not None:
                    self.close_connection = True

            def do_GET(self):
                self._go()

            def do_HEAD(self):
                self._go(head_only=True)

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.httpd.daemon_threads = True
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def hits(self, path):
        return [e for e in self.log if e[0] == path]


@pytest.fixture
def srv():
    servers = []

    def make(routes):
        s = Srv(routes)
        servers.append(s)
        return s
    yield make
    for s in servers:
        s.close()


def ranged(body: bytes, req, etag=None, lm=None, extra=None):
    """A correct server: 206 for a Range, 200 otherwise."""
    h = dict(extra or {})
    if etag:
        h["ETag"] = etag
    if lm:
        h["Last-Modified"] = lm
    rng = req.headers.get("Range")
    if rng:
        a, b = rng.split("=", 1)[1].split("-")
        a = int(a)
        b = int(b) if b else len(body) - 1
        h["Content-Range"] = f"bytes {a}-{b}/{len(body)}"
        return 206, h, body[a:b + 1], None
    return 200, h, body, None


def drain(cfg, store):
    fetch.run_queue(cfg, store, drain=True)


def q(store, sql, *a):
    with store.connect() as con:
        return con.execute(sql, list(a)).fetchall()


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


# ------------------------------------------------- Group 1: rows doubled --

def test_inbox_identical_bytes_under_two_names_load_once(cfg, store):
    """1.1 (s2_samename A): the browser's second download "x (1).json.gz".
    was: both files 'done', 60 rows each — every contract line held twice."""
    body = gz(rate_doc())
    (cfg.inbox_dir / "acme_2026-05.json.gz").write_bytes(body)
    (cfg.inbox_dir / "acme_2026-05 (1).json.gz").write_bytes(body)
    scan_inbox(cfg, store)
    assert q(store, "SELECT count(*) FROM rates")[0][0] == 60
    rows = dict(q(store, "SELECT status, count(*) FROM files GROUP BY 1"))
    assert rows == {"done": 1, "skipped": 1}
    msg = q(store, "SELECT error FROM files WHERE status = 'skipped'")[0][0]
    assert "identical to" in msg and "not loaded twice" in msg
    # a third copy in a LATER scan, when the store already knows the sha
    (cfg.inbox_dir / "acme_copy.json.gz").write_bytes(body)
    scan_inbox(cfg, store)
    assert q(store, "SELECT count(*) FROM rates")[0][0] == 60
    assert not any(p.name == "acme_copy.json.gz" for p in cfg.inbox_dir.iterdir())


def test_link_then_inbox_copy_of_the_same_bytes_loads_once(cfg, store, srv):
    """1.1 (s3_claims B2): the 403 advice "download it in your browser and
    drop it into data/inbox/" — after the link had loaded. was: 120 rows."""
    body = gz(rate_doc())
    s = srv({"/r.json.gz": lambda req: ranged(body, req)})
    fetch.add_urls(store, [s.base + "/r.json.gz"])
    drain(cfg, store)
    (cfg.inbox_dir / "r_from_browser.json.gz").write_bytes(body)
    out = scan_inbox(cfg, store)
    assert out[0]["status"] == "skipped"
    assert q(store, "SELECT count(*) FROM rates")[0][0] == 60


def test_one_content_claim_for_link_and_inbox(cfg, store):
    """1.1: a link and an inbox copy racing can never both win — the inbox
    claim and the link claim are one mechanism under one lock.
    was: AttributeError (no inbox claim existed; both loaded, 300 lines twice)."""
    s = "a" * 64
    assert store.claim_inbox_content(s, "inbox_copy.json.gz") is None
    fetch.add_urls(store, ["http://127.0.0.1:9/x.json.gz"])
    uid = q(store, "SELECT id FROM url_queue")[0][0]
    twin = store.claim_content_ingest(s, uid, "in_network", "abc_x.json.gz")
    assert twin == ("inbox_copy.json.gz", "ingesting")
    assert q(store, "SELECT status, kind FROM url_queue")[0] == ("skipped", "duplicate")
    store.release_inbox_claim(s, "inbox_copy.json.gz")
    # the inbox file failed: the deferred link copy gets its automatic retry
    store.update_url(uid, content_sha=s)
    assert store.revive_skipped_duplicates(s, -1) == 1


def test_upload_of_loaded_bytes_under_another_name_is_a_duplicate(cfg, store):
    """1.1 (/api/upload compared the NAME only). was: status 'queued', loaded twice."""
    body = gz(rate_doc())
    (cfg.inbox_dir / "first.json.gz").write_bytes(body)
    scan_inbox(cfg, store)
    c = TestClient(create_app(cfg, store))
    r = c.post("/api/upload", files={"file": ("first (1).json.gz", body)})
    assert r.status_code == 200 and r.json()["status"] == "duplicate"
    time.sleep(0.5)
    assert q(store, "SELECT count(*) FROM rates")[0][0] == 60


def _v1_v2(n=2000):
    v1 = rate_doc(rate=40.0, n=n)
    v2 = rate_doc(rate=55.0, n=n)
    assert len(v1) == len(v2)
    return v1, v2


def _cut_then(v1, v2, cut, second):
    """First GET: v1 headers, cut after `cut` bytes. Later GETs: second(req, v2)."""
    state = {"n": 0}

    def fn(req):
        state["n"] += 1
        if state["n"] == 1:
            return 200, {}, v1, cut
        return second(req, v2)
    return fn


def test_resume_from_a_republished_file_without_validator_restarts(cfg, srv):
    """1.2 (c02b_json_splice): no ETag / Last-Modified, same size; the resume
    is answered from the NEW version. was: sha equal to neither version (40.0
    for 97110 rows, 55.0 for the rest — two vintages under one 'done' row)."""
    v1, v2 = _v1_v2()
    # cut past the first 64 KB chunk: a partial chunk is dropped, so a cut
    # inside it leaves nothing to resume
    s = srv({"/f.json": _cut_then(v1, v2, 200_000, lambda req, b: ranged(b, req))})
    dest = cfg.downloads_dir / "f.json"
    got, _ = fetch.download(cfg, s.base + "/f.json", dest)
    assert got == sha(v2) and dest.read_bytes() == v2


def test_resume_answered_with_another_etag_restarts(cfg, srv):
    """1.2 (c02_range_odd, strong ETag): the server ignores If-Range and
    answers 206 with ETag "v2". was: appended — sha of neither version."""
    v1, v2 = _v1_v2()
    st = {"n": 0}

    def fn(req):
        st["n"] += 1
        if st["n"] == 1:
            return 200, {"ETag": '"v1"'}, v1, 200_000
        return ranged(v2, req, etag='"v2"')
    s = srv({"/f.json": fn})
    got, _ = fetch.download(cfg, s.base + "/f.json", cfg.downloads_dir / "f.json")
    assert got == sha(v2)


def test_resume_whose_body_starts_at_byte_zero_restarts(cfg, srv):
    """1.4 (c01 cr_start_wrong): Content-Range says the asked offset, the body
    is the whole file. was: OK with a file 29 % bigger than truth."""
    body = rate_doc(n=3000)

    def second(req, b):
        if not req.headers.get("Range"):
            return ranged(b, req)
        a = int(req.headers["Range"].split("=")[1].rstrip("-"))
        return 206, {"Content-Range": f"bytes {a}-{len(b) - 1}/{len(b)}",
                     "Content-Length": str(len(b))}, b, None
    s = srv({"/f.json": _cut_then(body, body, len(body) // 3, second)})
    dest = cfg.downloads_dir / "f.json"
    got, _ = fetch.download(cfg, s.base + "/f.json", dest)
    assert got == sha(body) and dest.stat().st_size == len(body)


def test_resume_without_content_range_and_short_body_is_not_complete(cfg, srv):
    """1.5 (c01 cr_absent_short): 206, no Content-Range, a short consistent
    body. was: accepted — a file a third of its size marked complete."""
    body = rate_doc(n=3000)
    st = {"n": 0}

    def fn(req):
        st["n"] += 1
        if st["n"] == 1:
            return 200, {}, body, len(body) // 3
        if st["n"] == 2:
            a = int(req.headers["Range"].split("=")[1].rstrip("-"))
            return 206, {}, body[a:a + 1000], None
        return ranged(body, req)
    s = srv({"/f.json": fn})
    dest = cfg.downloads_dir / "f.json"
    got, _ = fetch.download(cfg, s.base + "/f.json", dest)
    assert got == sha(body)


# ------------------------------------------------- Group 1: stuck / stale --

def test_keep_both_then_restart_loads_the_kept_file(cfg, store):
    """1.3 (s2b_keep_stuck): "Keep both" chosen, then a restart before the
    load. was: row 'queued' forever, scans [], DELETE 409, choose 409."""
    (cfg.inbox_dir / "acme_2026-05.json.gz").write_bytes(gz(rate_doc(n=3)))
    scan_inbox(cfg, store)
    (cfg.inbox_dir / "acme_2026-05.json.gz").write_bytes(gz(rate_doc(rate=41.0, n=5)))
    held = scan_inbox(cfg, store)
    assert held[0]["status"] == "needs_choice"
    name = q(store, "SELECT filename FROM files WHERE status = 'needs_choice'")[0][0]
    resolve_choice(cfg, store, name, "keep")          # the thread never runs: a restart
    store2 = Store(cfg.store_dir)
    store2.recover_stuck_files()
    out = scan_inbox(cfg, store2)
    assert [r["status"] for r in out] == ["done"]
    assert dict(q(store2, "SELECT filename, rows_emitted FROM files"))[name] == 5


def test_a_queued_row_nobody_loads_can_be_removed(cfg, store):
    """1.3: forget refused every 'queued' row. was: 409 "being processed"."""
    store.upsert_file("ghost.json.gz", status="queued")
    c = TestClient(create_app(cfg, store))
    assert c.delete("/api/files/ghost.json.gz").status_code == 200


def test_stale_sidecar_is_not_trusted(cfg, store, srv):
    """1.6 (c08b_wrong_sidecar_json): a complete download with a sidecar
    naming another sha. was: content_sha 000…, then a true copy under
    another link loaded again (600 rows for one file)."""
    body = rate_doc(n=60, pad=200_000)
    s = srv({"/a/f.json": lambda req: ranged(body, req),
             "/b/f.json": lambda req: ranged(body, req)})
    ua, ub = s.base + "/a/f.json", s.base + "/b/f.json"
    dest = cfg.downloads_dir / fetch.filename_for(ua)
    dest.write_bytes(body)
    fetch._meta_path(dest).write_text(json.dumps(
        {"sha": "0" * 64, "final_url": ua, "trailer_proves": None}))
    fetch.add_urls(store, [ua])
    drain(cfg, store)
    assert q(store, "SELECT content_sha FROM url_queue")[0][0] == sha(body)
    fetch.add_urls(store, [ub])
    drain(cfg, store)
    assert q(store, "SELECT count(*) FROM rates")[0][0] == 60


def test_requeued_loaded_link_downloads_a_republished_file(cfg, store, srv):
    """1.7 (seams/stale_reuse, delete_raw_after_ingest false): the server now
    serves June. was: 0 requests, still May's 60 rows / file_month 2026-05."""
    cfg.delete_raw_after_ingest = False
    cur = {"b": gz(rate_doc(month="2026-05-01")), "lm": "Fri, 01 May 2026 00:00:00 GMT"}
    s = srv({"/latest.json.gz": lambda req: ranged(cur["b"], req, lm=cur["lm"])})
    url = s.base + "/latest.json.gz"
    fetch.add_urls(store, [url])
    drain(cfg, store)
    assert q(store, "SELECT DISTINCT file_month FROM rates")[0][0] == "2026-05"
    cur.update(b=gz(rate_doc(rate=44.0, n=80, month="2026-06-01")),
               lm="Mon, 01 Jun 2026 00:00:00 GMT")
    assert store.requeue_urls([url]) == 1
    drain(cfg, store)
    assert q(store, "SELECT DISTINCT file_month FROM rates") == [("2026-06",)]
    assert q(store, "SELECT count(*) FROM rates")[0][0] == 80


def test_unchanged_loaded_link_is_not_downloaded_again(cfg, store, srv):
    """1.7: the refresh asks once; an unchanged file is not re-downloaded or
    re-loaded (one ranged request of 1 byte)."""
    body = gz(rate_doc())
    s = srv({"/same.json.gz": lambda req: ranged(body, req, etag='"e1"')})
    url = s.base + "/same.json.gz"
    fetch.add_urls(store, [url])
    drain(cfg, store)
    n0 = len(s.log)
    store.requeue_urls([url])
    drain(cfg, store)
    assert [e[2] for e in s.log[n0:]] == ["bytes=0-0"]
    assert q(store, "SELECT status FROM url_queue")[0][0] == "done"


def test_static_index_refresh_reloads_a_child_republished_in_place(cfg, store, srv):
    """1.7 (seams/refresh_static_children, default config): the monthly
    refresh re-expands a static index. was: requests [GET /index.json] only;
    the child stayed 2026-05 / 60 rows."""
    child = {"b": gz(rate_doc(month="2026-05-01")), "lm": "Fri, 01 May 2026 00:00:00 GMT"}
    s = srv({"/files/rates.json.gz": lambda req: ranged(child["b"], req, lm=child["lm"])})
    idx = json.dumps({"reporting_entity_name": "Twin Health", "reporting_structure": [
        {"reporting_plans": [{"plan_name": "P"}],
         "in_network_files": [{"location": s.base + "/files/rates.json.gz"}]}]}).encode()
    s.routes["/index.json"] = lambda req: ranged(idx, req)
    fetch.add_urls(store, [s.base + "/index.json"])
    drain(cfg, store)
    assert q(store, "SELECT count(*) FROM rates")[0][0] == 60
    child.update(b=gz(rate_doc(rate=45.0, n=80, month="2026-06-01")),
                 lm="Mon, 01 Jun 2026 00:00:00 GMT")
    store.requeue_urls([s.base + "/index.json"])
    drain(cfg, store)
    assert q(store, "SELECT DISTINCT file_month FROM rates") == [("2026-06",)]
    assert q(store, "SELECT count(*) FROM rates")[0][0] == 80


def test_dashboard_read_that_meets_a_part_swap_is_asked_again(cfg, store, monkeypatch):
    """1.8 (s5_reingest): a read across a part swap raised "Parquet file is
    likely corrupted, dictionary offset out of range". was: 422 "One of the
    values sent could not be used" (and a failed request every time)."""
    import duckdb
    real = store.store_stats
    calls = {"n": 0}

    def flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise duckdb.InvalidInputException(
                "Invalid Input Error: Parquet file is likely corrupted, dictionary "
                "offset out of range")
        return real(*a, **k)
    monkeypatch.setattr(store, "store_stats", flaky)
    c = TestClient(create_app(cfg, store), raise_server_exceptions=False)
    r = c.get("/api/stats")
    assert r.status_code == 200 and calls["n"] == 2


def test_complete_part_answered_416_is_finished_not_downloaded_again(cfg, srv):
    """1.9: the final rename failed on Windows (a lock), the row failed, and
    the retry's Range: bytes=<total>- got 416. was: the complete .part was
    deleted and the whole file downloaded again."""
    body = gz(rate_doc(n=3000))
    s = srv({"/f.json.gz": lambda req: (
        (416, {"Content-Range": f"bytes */{len(body)}"}, b"", None)
        if req.headers.get("Range") else (200, {}, body, None))})
    dest = cfg.downloads_dir / "f.json.gz"
    part = dest.with_suffix(dest.suffix + ".part")
    part.write_bytes(body)
    Path(str(part) + ".val").write_text(json.dumps({"validator": "", "total": len(body)}))
    got, _ = fetch.download(cfg, s.base + "/f.json.gz", dest)
    assert got == sha(body) and dest.read_bytes() == body
    assert [e[1] for e in s.log] == [416]


def test_download_rename_tolerates_a_transient_windows_lock(cfg, srv, monkeypatch):
    """1.9: part.replace(dest) had no lock tolerance — one antivirus hiccup
    failed the row ("unexpected download error"). was: PermissionError."""
    body = gz(rate_doc())
    s = srv({"/f.json.gz": lambda req: ranged(body, req)})
    real = Path.replace
    seen = {"n": 0}

    def locked_once(self, target):
        if str(self).endswith(".part") and seen["n"] == 0:
            seen["n"] += 1
            raise PermissionError(32, "being used by another process")
        return real(self, target)
    monkeypatch.setattr(Path, "replace", locked_once)
    dest = cfg.downloads_dir / "f.json.gz"
    got, _ = fetch.download(cfg, s.base + "/f.json.gz", dest)
    assert got == sha(body) and seen["n"] == 1


def test_blob_listing_dropped_in_the_inbox_is_quarantined_with_a_sentence(cfg, store):
    """1.10 (s1_containers u_blob_listing.json). was: 'done', 0 rows, no message."""
    (cfg.inbox_dir / "listing.json").write_text(json.dumps(
        {"blobs": [{"name": "a.json.gz", "downloadUrl": "https://x/a.json.gz"}]}))
    out = scan_inbox(cfg, store)
    assert out[0]["status"] == "quarantined"
    assert "Links tab" in q(store, "SELECT error FROM files")[0][0]
