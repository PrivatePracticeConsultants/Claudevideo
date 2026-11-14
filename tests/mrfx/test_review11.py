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
        self.starts: list[tuple] = []      # (path, Range) as each request ARRIVES
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def _go(self, head_only=False):
                outer.starts.append((self.path, self.headers.get("Range")))
                fn = outer.routes.get(self.path)
                if fn is None:
                    self.send_response(404)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    outer.log.append((self.path, 404, self.headers.get("Range"), 0))
                    return
                got = fn(self)
                status, headers, body, cut = got[:4]
                pace = got[4] if len(got) > 4 else 0.0
                self.send_response(status)
                for k, v in headers.items():
                    self.send_header(k, v)
                if "Content-Length" not in headers and "Transfer-Encoding" not in headers:
                    self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                out = body if cut is None else body[:cut]
                written = 0
                chunked = headers.get("Transfer-Encoding") == "chunked"
                if not head_only:
                    try:
                        step = (64 << 10) if (pace or chunked) else max(1, len(out))
                        for i in range(0, len(out), step):
                            piece = out[i:i + step]
                            if chunked:
                                self.wfile.write(f"{len(piece):x}\r\n".encode() + piece + b"\r\n")
                            else:
                                self.wfile.write(piece)
                            self.wfile.flush()
                            written += len(piece)
                            if pace:
                                time.sleep(pace)
                        if chunked:
                            self.wfile.write(b"0\r\n\r\n")
                    except OSError:
                        pass
                outer.log.append((self.path, status, self.headers.get("Range"), written))
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


# ------------------------------------- Group 2: states, sentences, silences --

def _index(base: str, n: int) -> bytes:
    return json.dumps({"reporting_entity_name": "Twin Health", "reporting_structure": [
        {"reporting_plans": [{"plan_name": "P"}],
         "in_network_files": [{"location": f"{base}/f{i}.json.gz"} for i in range(n)]}]}).encode()


def test_capped_index_can_be_retried_and_takes_the_rest(cfg, store, srv):
    """2.1 (queue/s3_toc A/B): "raise max_toc_files and press retry on this
    row" — there was no such button and the API refused. was: retry False;
    f3/f4 never loaded."""
    s = srv({})
    for i in range(5):
        body = gz(rate_doc(n=2, payer=f"P{i}"))
        s.routes[f"/f{i}.json.gz"] = lambda req, b=body: ranged(b, req)
    s.routes["/idx.json"] = lambda req: ranged(_index(s.base, 5), req)
    cfg.max_toc_files = 3
    fetch.add_urls(store, [s.base + "/idx.json"])
    drain(cfg, store)
    assert q(store, "SELECT count(*) FROM url_queue WHERE kind = 'in_network'")[0][0] == 3
    cfg.max_toc_files = 10
    idx = q(store, "SELECT id FROM url_queue WHERE kind = 'toc'")[0][0]
    assert store.set_url_status_by_id(idx, "queued") is True
    drain(cfg, store)
    assert q(store, "SELECT count(*) FROM url_queue WHERE kind = 'in_network' "
                    "AND status = 'done'")[0][0] == 5


def test_oversize_row_records_its_size_and_requeues_when_the_limit_rises(cfg, store, srv):
    """2.2 (queue/s11b_oversize_raise): the documented fix (raise
    confirm_over_gb, restart) never re-queued it. was: bytes_total 0, 0 re-queued."""
    body = gz(rate_doc(n=5, pad=3_000_000))
    s = srv({"/big.json.gz": lambda req: ranged(body, req)})
    cfg.confirm_over_gb = 0.000001
    fetch.add_urls(store, [s.base + "/big.json.gz"])
    drain(cfg, store)
    assert q(store, "SELECT status, bytes_total FROM url_queue")[0] == ("oversize", len(body))
    assert store.requeue_oversize_within(5 * 10**9) == 1


def test_clear_queue_pressed_while_an_index_expands_sticks(cfg, store, srv, monkeypatch):
    """2.3 (queue/s9_actions C). was: the index ended 'done' and all its files
    were queued and loaded."""
    s = srv({})
    for i in range(3):
        body = gz(rate_doc(n=2, payer=f"P{i}"))
        s.routes[f"/f{i}.json.gz"] = lambda req, b=body: ranged(b, req)
    s.routes["/idx.json"] = lambda req: ranged(_index(s.base, 3), req)
    real = fetch.expand_toc_with_plans

    def slow(*a, **k):
        out = real(*a, **k)
        store.clear_queued()          # the user presses "Clear queue" meanwhile
        return out
    monkeypatch.setattr(fetch, "expand_toc_with_plans", slow)
    fetch.add_urls(store, [s.base + "/idx.json"])
    drain(cfg, store)
    assert q(store, "SELECT status FROM url_queue") == [("skipped",)]
    assert q(store, "SELECT count(*) FROM rates")[0][0] == 0


def test_redirect_loop_is_a_plain_sentence(cfg, srv):
    """2.4 (download/c05 redirect_loop). was: httpx.TooManyRedirects escaped
    download() (the row read "unexpected download error", with a traceback)."""
    s = srv({"/loop": lambda req: (302, {"Location": "/loop"}, b"", None)})
    with pytest.raises(fetch.DownloadError, match="redirects in a loop"):
        fetch.download(cfg, s.base + "/loop", cfg.downloads_dir / "x.json.gz")


def test_a_parser_process_that_died_twice_reads_as_a_sentence():
    """2.5 (ingest/s7_pool exit). was: "BrokenProcessPool: A process in the
    process pool was terminated abruptly …"."""
    from concurrent.futures.process import BrokenProcessPool

    from mrfx.ingest import plain_parse_error
    msg = plain_parse_error(BrokenProcessPool("A process in the process pool was "
                                              "terminated abruptly"))
    assert msg.startswith("the parser process for this file was ended")
    assert "parallel_ingests" in msg


def test_a_user_retry_restores_the_auto_resume_budget(cfg, store):
    """2.6 + 4.1 (queue/s6_counters): every requeue resets through one
    fragment. was: auto_resumes 3, park_count 4, kind 'duplicate' kept."""
    fetch.add_urls(store, ["http://127.0.0.1:9/a.json.gz"])
    uid = q(store, "SELECT id FROM url_queue")[0][0]
    with store.connect() as con:
        con.execute("UPDATE url_queue SET status = 'failed', auto_resumes = 3, "
                    "park_count = 4, kind = 'duplicate', big_lane = true WHERE id = ?", [uid])
    assert store.set_url_status_by_id(uid, "queued")
    assert q(store, "SELECT auto_resumes, park_count, kind, big_lane FROM url_queue")[0] == \
        (0, 0, "unknown", False)


def test_failed_pasted_rows_stay_listed_past_500_links(cfg, store):
    """2.7 (queue/s9_actions B). was: the failed pasted row 1 not in list_urls()."""
    fetch.add_urls(store, [f"http://127.0.0.1:9/f{i}.json.gz" for i in range(600)])
    with store.connect() as con:
        con.execute("UPDATE url_queue SET status = 'done'")
        con.execute("UPDATE url_queue SET status = 'failed' WHERE id = 1")
    assert 1 in {r["id"] for r in store.list_urls(500)}


def test_small_file_slot_sizes_unsized_rows_and_takes_the_small_one(cfg, store, srv):
    """2.8 (queue/s7_lanes): sizes are unknown on a fresh queue, so the
    small-file slot took the first unsized big file. was: the small provider
    list was fetched after the big files."""
    big = rate_doc(n=20, pad=1_500_000)          # 1.5 MB, plain: over the 1 MB lane
    small = gz(rate_doc(n=2, payer="Small"))
    s = srv({})
    for i in range(3):
        s.routes[f"/big{i}.json.gz"] = lambda req: (*ranged(big, req), 0.05)
    s.routes["/s.json.gz"] = lambda req: ranged(small, req)
    cfg.parallel_downloads, cfg.small_file_downloads, cfg.small_file_mb = 3, 1, 1
    fetch.add_urls(store, [s.base + f"/big{i}.json.gz" for i in range(3)]
                   + [s.base + "/s.json.gz"])
    drain(cfg, store)
    full_gets = [p for p, r in s.starts if r is None]
    # the small-only slot sized big0 (one 1-byte request), handed it to a big
    # slot and took the small file while the big ones were still flowing
    assert full_gets.index("/s.json.gz") < 3
    assert q(store, "SELECT count(*) FROM url_queue WHERE bytes_total > 0")[0][0] == 4


def test_reexpansion_leaves_an_oversize_child_that_still_does_not_fit(cfg, store):
    """2.9 (queue/s11_oversize_reexpand). was: re-queued by every re-expansion."""
    store.enqueue_urls([("http://h/idx.json", fetch.dedup_key("http://h/idx.json"))])
    u = "http://h/big.json.gz"
    store.enqueue_urls([(u, fetch.dedup_key(u))], parent_id=1)
    with store.connect() as con:
        con.execute("UPDATE url_queue SET status = 'oversize', bytes_total = 9000000000 "
                    "WHERE url = ?", [u])
    assert store.enqueue_urls([(u, fetch.dedup_key(u))], parent_id=1,
                              oversize_limit=5 * 10**9) == 0
    assert q(store, "SELECT status FROM url_queue WHERE url = ?", u)[0][0] == "oversize"


def test_dedup_key_keeps_an_identity_param_beside_a_signature():
    """2.10 (queue/s2_dedup): two DIFFERENT files collapsed to one key — the
    second never queued. was: dl?token=FILE1&sig=a == dl?token=FILE2&sig=b."""
    k = fetch.dedup_key
    assert k("https://h/dl?token=FILE1&sig=a") != k("https://h/dl?token=FILE2&sig=b")
    assert k("https://h/r.json?st=NY&sig=a") != k("https://h/r.json?st=CA&sig=b")
    assert k("https://h/r?x-amz-meta-file=1") != k("https://h/r?x-amz-meta-file=2")
    # the signed shapes still dedup
    az = "https://a.blob.core.windows.net/c/f.gz?sv=2020-08-04&st=2026-11-0{}T00%3A00Z&se=2026-11-0{}T00%3A00Z&sr=b&sp=r&sig={}"
    assert k(az.format(1, 2, "A")) == k(az.format(5, 6, "B"))
    s3 = "https://b.s3.amazonaws.com/f.gz?X-Amz-Credential={}&X-Amz-Date={}&X-Amz-Signature={}"
    assert k(s3.format("x", "20261101", "aa")) == k(s3.format("y", "20261102", "bb"))


def test_a_set_aside_download_goes_behind_the_waiting_files(cfg, store, monkeypatch):
    """2.11 (seams/setaside_order): the cap re-claimed the slot it just freed.
    was: retries 0 — the same row claimed again at once."""
    fetch.add_urls(store, ["http://127.0.0.1:9/a.json.gz", "http://127.0.0.1:9/b.json.gz"])

    def capped(*a, **k):
        raise fetch.DownloadError("set aside", retryable=True, set_aside=True)
    monkeypatch.setattr(fetch, "download", capped)
    fetch.fetch_url_record(cfg, store, store.next_queued_url())
    assert store.next_queued_url()["url"].endswith("/b.json.gz")


def test_stop_during_the_retry_back_off_frees_the_slot_at_once(cfg, srv):
    """2.12 (download/c08 cancel_during_backoff). was: the slot was held for
    the whole back-off (>= 2 s here, up to ~93 s at production values)."""
    s = srv({"/f.json.gz": lambda req: (503, {}, b"", None)})
    cfg.download_retries = 3
    t0 = time.monotonic()
    with pytest.raises(fetch.DownloadError):
        fetch.download(cfg, s.base + "/f.json.gz", cfg.downloads_dir / "f.json.gz",
                       cancel_check=lambda: time.monotonic() - t0 > 0.3)
    assert time.monotonic() - t0 < 1.8


def test_retry_after_is_honoured(cfg, srv):
    """2.13 (download/c05 r429_retry_after). was: the generic back-off
    (2-5 s) instead of the server's Retry-After: 1."""
    body = gz(rate_doc())
    seen = []

    def fn(req):
        seen.append(time.monotonic())
        if len(seen) == 1:
            return 429, {"Retry-After": "1"}, b"", None
        return ranged(body, req)
    s = srv({"/f.json.gz": fn})
    fetch.download(cfg, s.base + "/f.json.gz", cfg.downloads_dir / "f.json.gz")
    assert 0.9 <= seen[1] - seen[0] < 1.8


def test_refused_range_ignoring_200s_do_not_spend_the_retry_budget(cfg, srv, monkeypatch):
    """2.14 (download/c07 holey_restart_200): with download_retries <= 2 the
    accept-and-restart branch was unreachable. was: DownloadError "server
    ignored resume" — the file could never finish."""
    body = gz(rate_doc(n=200, pad=400_000))
    s = srv({"/f.json.gz": lambda req: (200, {}, body, None)})
    monkeypatch.setattr(fetch, "_KEEP_PART_ON_200_BYTES", 1000)
    cfg.download_retries = 1
    dest = cfg.downloads_dir / "f.json.gz"
    dest.with_suffix(".gz.part").write_bytes(body[:5000])
    got, _ = fetch.download(cfg, s.base + "/f.json.gz", dest)
    assert got == sha(body)


def test_an_index_leaves_no_download_record_behind(cfg, store, srv):
    """2.15 (queue s0/s1/s3/s5). was: '<hash>_idx.json.fetchmeta' left in downloads/."""
    s = srv({})
    body = gz(rate_doc(n=2))
    s.routes["/f0.json.gz"] = lambda req: ranged(body, req)
    s.routes["/idx.json"] = lambda req: ranged(_index(s.base, 1), req)
    fetch.add_urls(store, [s.base + "/idx.json"])
    drain(cfg, store)
    assert list(cfg.downloads_dir.glob("*.fetchmeta")) == []


def test_a_provider_list_arriving_for_a_file_that_is_gone_says_so(cfg, store):
    """2.16 (ingest/s6_companion B'). was: rate file 'done', 0 rows, error ''."""
    cfg.move_processed = False
    rate = {"reporting_entity_name": "Ref Health", "last_updated_on": "2026-05-01",
            "version": "2.0.0", "in_network": [
                {"negotiation_arrangement": "ffs", "billing_code_type": "CPT",
                 "billing_code": "97110", "negotiated_rates": [
                     {"provider_references": [7], "negotiated_prices": [
                         {"negotiated_type": "negotiated", "negotiated_rate": 40.0,
                          "billing_class": "professional", "service_code": ["11"]}]}]}]}
    p = cfg.inbox_dir / "ref_rates_2026-05.json"
    p.write_text(json.dumps(rate))
    scan_inbox(cfg, store)
    p.unlink()                                     # the user tidied the inbox
    (cfg.inbox_dir / "ref_refs_2026-05.json").write_text(json.dumps(
        {"reporting_entity_name": "Ref Health", "last_updated_on": "2026-05-01",
         "provider_groups": [{"provider_group_id": 7, "provider_groups": [
             {"npi": [1234567893], "tin": {"type": "ein", "value": "111111111"}}]}]}))
    scan_inbox(cfg, store)
    err = q(store, "SELECT error FROM files WHERE filename = 'ref_rates_2026-05.json'")[0][0]
    assert err and "drop this file in again" in err


# ------------------------------------------------ Group 3: work done twice --

def test_inbox_ingest_reads_the_file_about_twice_not_three_times(cfg, store, monkeypatch):
    """3.2 (ingest/s9b_opens): the parse, then file_fingerprint and hash_file
    read it again. was: 2.49x on a 35 MB real file; 3.4x on this one."""
    import builtins
    import io
    import random
    pad = random.Random(3).randbytes(1_500_000)        # incompressible: ~3 MB gz
    p = cfg.inbox_dir / "reads.json.gz"
    p.write_bytes(gzip.compress(rate_doc(n=50)[:-1] + b', "zpad": "' + pad.hex().encode()
                                + b'"}', mtime=0))
    size = p.stat().st_size
    read = {"n": 0}
    real_open = builtins.open

    class Counting(io.FileIO):
        def readinto(self, b):
            n = super().readinto(b)
            read["n"] += n or 0
            return n

    def spy(file, mode="r", *a, **k):
        if str(file) == str(p) and "r" in mode and "b" in mode:
            return io.BufferedReader(Counting(file, "rb"))
        return real_open(file, mode, *a, **k)
    monkeypatch.setattr(builtins, "open", spy)
    from mrfx.ingest import ingest_file
    assert ingest_file(cfg, store, p)["status"] == "done"
    monkeypatch.setattr(builtins, "open", real_open)
    # the identity pass + the parse = 2x, plus the sniff's 1 MB header windows
    # (this hex filler inflates only ~2:1, so they cost ~0.8x here; 0.003x on
    # the 35 MB real file, where s9b_opens measures 2.49x -> 2.01x)
    assert read["n"] / size < 3.0, read["n"] / size


_WATCH_DRIVER = """
import os, sys, time
sys.path.insert(0, {root!r})
from mrfx.ingest import ParsePoolManager
if __name__ == "__main__":
    pool = ParsePoolManager(1)
    print(pool.submit(os.getpid).result(), flush=True)
    time.sleep(120)
"""


def test_parser_workers_end_when_their_parent_is_killed(tmp_path):
    """3.3 (ingest/s4b_orphans): an idle worker blocked on the pool queue
    forever after a hard kill of the server. was: alive (ppid 1) 15 s later."""
    import os
    import subprocess
    import sys
    drv = tmp_path / "drv.py"
    drv.write_text(_WATCH_DRIVER.format(root=str(Path(__file__).resolve().parents[2])))
    proc = subprocess.Popen([sys.executable, str(drv)], stdout=subprocess.PIPE, text=True)
    try:
        wpid = int(proc.stdout.readline())
        proc.kill()
        proc.wait()
        deadline = time.monotonic() + 15
        alive = True
        while time.monotonic() < deadline:
            try:
                os.kill(wpid, 0)
            except ProcessLookupError:
                alive = False
                break
            time.sleep(0.25)
        assert not alive
    finally:
        if proc.poll() is None:
            proc.kill()


def test_cli_drains_open_the_store_pinned(cfg, monkeypatch):
    """3.4: `mrfx ingest` / `mrfx add` (no dashboard) opened the store without
    keep_warm — 12.6 ms per connection, write_lock held 14.9 % of the wall.
    was: keep_warm absent."""
    import argparse

    from mrfx import cli
    seen = {}

    class Stop(Exception):
        pass

    def fake(*a, **k):
        seen.update(k)
        raise Stop
    monkeypatch.setattr(cli, "Store", fake)
    monkeypatch.setattr(cli, "_something_owns_the_port", lambda *a, **k: False)
    with pytest.raises(Stop):
        cli.cmd_ingest(cfg, argparse.Namespace(path=None, force=False))
    assert seen.get("keep_warm") is True


def test_a_part_swap_held_open_parks_instead_of_holding_the_lock(cfg, store, monkeypatch):
    """3.5: finalize_rates_part held write_lock through replace_with_retry's
    ~90 s loop while another program held the old part. was: ~85 s."""
    (cfg.inbox_dir / "a.json.gz").write_bytes(gz(rate_doc(n=3)))
    scan_inbox(cfg, store)
    real = Path.replace

    def locked(self, target):
        if str(target).endswith(".parquet") and not str(target).endswith(".tmp"):
            raise PermissionError(32, "being used by another process")
        return real(self, target)
    import shutil
    part = next(store.rates_dir.glob("*.parquet"))
    tmp = part.with_name(".replacement.parquet.tmp")
    shutil.copy(part, tmp)
    monkeypatch.setattr(Path, "replace", locked)
    t0 = time.monotonic()
    with store.write_lock:              # what finalize_rates_part holds
        store._swap_in_part("a.json.gz", tmp, part, 3)
    assert time.monotonic() - t0 < 5
    with store.connect() as con:
        assert json.loads(store._meta_get(con, "parked_parts") or "{}")


def test_a_chunked_copy_of_a_loaded_file_is_skipped_early(cfg, store, srv):
    """3.7 + 3.8 (download/c03 early_dedup chunked twin): no Content-Length
    meant no early check. was: downloaded in full (every byte), then skipped
    by the full sha."""
    import random
    pad = random.Random(5).randbytes(1_500_000).hex().encode()
    body = gzip.compress(rate_doc(n=50)[:-1] + b', "zpad": "' + pad + b'"}', mtime=0,
                         compresslevel=1)
    cfg.early_dedup_mb = 1
    s = srv({"/a.json.gz": lambda req: ranged(body, req),
             "/b.json.gz": lambda req: (
                 ranged(body, req) if req.headers.get("Range")
                 else (200, {"Transfer-Encoding": "chunked"}, body, None, 0.01))})
    (cfg.inbox_dir / "a_loaded.json.gz").write_bytes(body)
    scan_inbox(cfg, store)
    fetch.add_urls(store, [s.base + "/b.json.gz"])
    drain(cfg, store)
    row = q(store, "SELECT status, kind, error FROM url_queue")[0]
    assert row[:2] == ("skipped", "duplicate") and "checksum-equal" in row[2]
    assert "instead of downloading the whole file" in row[2]


def test_a_fresh_store_goes_straight_to_the_full_rollup(cfg, store, srv, monkeypatch):
    """3.1 (throughput default_warm): every fresh store's first rebuild raised
    through update_rollups_incremental first. was: incremental attempted."""
    body = gz(rate_doc())
    s = srv({"/f.json.gz": lambda req: ranged(body, req)})
    calls = []
    real = store.update_rollups_incremental
    monkeypatch.setattr(store, "update_rollups_incremental",
                        lambda *a, **k: calls.append(1) or real(*a, **k))
    fetch.add_urls(store, [s.base + "/f.json.gz"])
    drain(cfg, store)
    assert calls == [] and store.rollups_ever_built()


# ------------------------------------------- Group 4: hygiene and sentences --

def test_a_row_the_user_skipped_is_never_revived_as_a_duplicate(cfg, store):
    """4.1 (queue/s6_counters B): a once-'duplicate' row the user skipped kept
    kind 'duplicate' and was revived behind their back. was: 1 revived."""
    fetch.add_urls(store, ["http://127.0.0.1:9/a.json.gz"])
    uid = q(store, "SELECT id FROM url_queue")[0][0]
    with store.connect() as con:
        con.execute("UPDATE url_queue SET status = 'queued', kind = 'duplicate', "
                    "content_sha = 'abc' WHERE id = ?", [uid])
    assert store.set_url_status_by_id(uid, "skipped")
    assert store.revive_skipped_duplicates("abc", -1) == 0


def test_a_vanished_download_is_downloaded_again_as_a_download(cfg, store, srv, monkeypatch):
    """4.2 (queue/s8_processor A): the re-download ran under 'ingesting' —
    invisible to "stop downloads". was: status 'ingesting' during it."""
    body = gz(rate_doc())
    s = srv({"/f.json.gz": lambda req: ranged(body, req)})
    fetch.add_urls(store, [s.base + "/f.json.gz"])
    rec = store.next_queued_url()
    store.update_url(rec["id"], status="fetched", content_sha="x")
    rec = store.next_fetched_url()            # its download is not on disk
    seen = []
    real = fetch.download

    def spy(*a, **k):
        seen.append(q(store, "SELECT status FROM url_queue")[0][0])
        return real(*a, **k)
    monkeypatch.setattr(fetch, "download", spy)
    fetch.process_url_record(cfg, store, rec)
    assert seen == ["downloading"]
    assert q(store, "SELECT status FROM url_queue")[0][0] == "done"


def test_a_finished_part_is_flushed_to_disk_before_it_goes_live(cfg, store, monkeypatch):
    """4.4b: no fsync anywhere — a power loss could leave a truncated part
    under a 'done' row. was: os.fsync never called."""
    import os
    calls = []
    real = os.fsync
    monkeypatch.setattr(os, "fsync", lambda fd: calls.append(fd) or real(fd))
    (cfg.inbox_dir / "a.json.gz").write_bytes(gz(rate_doc(n=3)))
    scan_inbox(cfg, store)
    assert calls


def test_outreach_csv_rows_end_once_on_windows(cfg, store, monkeypatch):
    """4.4d: the CSV ends rows with \\r\\n and Path.write_text translated each
    \\n again on Windows. was: b'\\r\\r\\n' (a blank line between rows)."""
    real = Path.write_text

    def windows_text(self, data, *a, **k):         # what text mode does on Windows
        return real(self, data.replace("\n", "\r\n"), *a, **k)
    monkeypatch.setattr(Path, "write_text", windows_text)
    c = TestClient(create_app(cfg, store))
    r = c.get("/api/export/outreach.csv")
    assert r.status_code == 200 and b"\r\r\n" not in r.content


def test_cli_output_redirected_without_utf8_does_not_crash(tmp_path):
    """4.4g: `mrfx … > log.txt` on a non-UTF-8 console raised
    UnicodeEncodeError on the first "—". was: exit 1."""
    import os
    import subprocess
    import sys
    env = dict(os.environ, PYTHONIOENCODING="ascii")
    env.pop("PYTHONUTF8", None)
    r = subprocess.run([sys.executable, "-m", "mrfx", "--help"], capture_output=True,
                       env=env, cwd=str(Path(__file__).resolve().parents[2]), timeout=120)
    assert r.returncode == 0, r.stderr[-300:]


def test_a_damaged_download_reads_as_a_sentence():
    """4.5 (download/c04): raw library text was the user's sentence.
    was: "BadGzipFile: CRC check failed 0x36bafc05 != 0xc94503fa"."""
    import gzip as _gz

    from mrfx.ingest import plain_parse_error
    msg = plain_parse_error(_gz.BadGzipFile("CRC check failed 0x36bafc05 != 0xc94503fa"))
    assert msg.startswith("the file is damaged or incomplete") and "press retry" in msg
    msg = plain_parse_error(EOFError("Compressed file ended before the end-of-stream "
                                     "marker was reached"))
    assert msg.startswith("the file is damaged or incomplete")


def test_empty_and_utf16_files_say_what_they_are(cfg, store):
    """4.5 / 4.6 (download/c05 zero_byte, ingest/s1 l_utf16): both read "no
    in_network / provider_references / reporting_structure key found"."""
    (cfg.inbox_dir / "empty.json").write_bytes(b"")
    (cfg.inbox_dir / "u16.json").write_bytes(json.dumps({"in_network": []}).encode("utf-16"))
    scan_inbox(cfg, store)
    err = dict(q(store, "SELECT filename, error FROM files"))
    assert "the file is empty" in err["empty.json"]
    assert "UTF-16" in err["u16.json"]


def test_a_small_file_with_none_of_the_codes_says_so(cfg, store):
    """4.6 (ingest/s10_honesty): only the big-file short-circuit wrote the
    sentence. was: 'done', 0 rows, qa.messages NULL."""
    d = json.loads(rate_doc(n=2))
    d["in_network"][0]["billing_code"] = "99213"
    (cfg.inbox_dir / "office.json").write_text(json.dumps(d))
    scan_inbox(cfg, store)
    qa = json.loads(q(store, "SELECT qa FROM files")[0][0])
    assert any("none of the target billing codes" in m for m in qa.get("messages") or [])


def test_unquoted_off_in_the_config_means_off():
    """4.6: `mode: off` (the shipped comment's spelling) is YAML False.
    was: ConfigFileError "Input should be 'api', 'bulk' or 'off'"."""
    import yaml

    from mrfx.config import MrfxConfig
    c = MrfxConfig(enrichment=yaml.safe_load("mode: off"))
    assert c.enrichment.mode == "off"
