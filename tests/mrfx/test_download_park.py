"""Stalled-download hand-off ("park"). A payer server that stops sending data
used to hold a download slot for the whole stall window (10 min) before the
file was paused for a day. Now, after download_park_seconds of silence, the
file steps aside: its slot goes to the next file in the queue at once, its
bytes are kept, and it rejoins the line later (at once when nothing else is
waiting), resuming where it stopped."""

from __future__ import annotations

import gzip
import hashlib
import http.server as hs
import json
import os
import threading
import time

import pytest

from mrfx.fetch import DownloadError, add_urls, download, filename_for
from tests.mrfx.test_fetch import _drain_with_deadline

GOOD = gzip.compress(json.dumps({
    "reporting_entity_name": "Parkco", "last_updated_on": "2026-05-01",
    "version": "2.0.0", "in_network": []}).encode())


def _world(silent_after: int = 0, recover: threading.Event | None = None):
    """A server with a payer that goes silent and a file that works.

    /stalls.json.gz: sends `silent_after` bytes of a 4 MB body, then holds the
    connection open and sends nothing. Once `recover` is set, a RESUMED
    request (Range) is answered in full - the payer's server came back.
    /good.json.gz: a small valid file, served at once."""
    body = os.urandom(4 << 20)
    release = threading.Event()
    seen = {"ranges": [], "order": []}

    class H(hs.BaseHTTPRequestHandler):
        def do_GET(self):
            name = self.path.rsplit("/", 1)[-1]
            seen["order"].append((name, time.monotonic()))
            if name == "good.json.gz":
                self.send_response(200)
                self.send_header("Content-Length", str(len(GOOD)))
                self.end_headers()
                self.wfile.write(GOOD)
                return
            rng = self.headers.get("Range")
            seen["ranges"].append(rng)
            if rng and recover is not None and recover.is_set():
                start = int(rng.split("=")[1].split("-")[0])
                self.send_response(206)
                self.send_header("Content-Range", f"bytes {start}-{len(body) - 1}/{len(body)}")
                self.send_header("Content-Length", str(len(body) - start))
                self.end_headers()
                self.wfile.write(body[start:])
                return
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Accept-Ranges", "bytes")
            self.end_headers()
            if silent_after:
                self.wfile.write(body[:silent_after])
                self.wfile.flush()
            release.wait(60)          # silence: connection open, no data

        def log_message(self, *a):
            pass
    httpd = hs.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"

    def stop():
        release.set()
        httpd.shutdown()
    return base, body, seen, stop


@pytest.fixture
def parking(cfg):
    cfg.download_timeout_seconds = 0.5     # a silent read is noticed fast
    cfg.download_park_seconds = 1.5        # ...and the file parks soon after
    cfg.download_stall_seconds = 600.0     # the old window, never reached here
    cfg.download_park_minutes = 10.0
    cfg.download_park_limit = 2
    cfg.download_retries = 6
    cfg.parallel_downloads = 1             # ONE slot: the hard case
    return cfg


def test_a_silent_server_parks_the_download_fast(parking):
    base, _, _, stop = _world()
    try:
        url = f"{base}/stalls.json.gz"
        t0 = time.monotonic()
        with pytest.raises(DownloadError) as ei:
            download(parking, url, parking.downloads_dir / filename_for(url))
        took = time.monotonic() - t0
    finally:
        stop()
    assert ei.value.parked and ei.value.retryable and not ei.value.progressed
    assert took < 10, f"parked after {took:.1f} s, not ~the 1.5 s park window"
    assert "stopped sending data" in str(ei.value)


def test_silence_mid_file_parks_and_keeps_the_bytes(parking):
    """2 MB arrive, then the server goes quiet with the connection open."""
    base, body, _, stop = _world(silent_after=2 << 20)
    try:
        url = f"{base}/stalls.json.gz"
        dest = parking.downloads_dir / filename_for(url)
        with pytest.raises(DownloadError) as ei:
            download(parking, url, dest)
    finally:
        stop()
    assert ei.value.parked and ei.value.progressed, "2 MB is real progress this turn"
    part = dest.with_suffix(dest.suffix + ".part")
    assert part.read_bytes() == body[:2 << 20], "the bytes so far are kept for the resume"
    assert "2 MB kept" in str(ei.value)


def test_one_slot_a_stalled_payer_first_in_line_and_the_next_file_still_lands(parking, store):
    """The scenario asked for: the payer's server stops answering, so the next
    file starts instead of everything waiting on it. With ONE download slot
    and the stalled file FIRST in the queue, the good file must download while
    the stalled one is parked; once nothing else waits, the slot goes back to
    the stalled file; after download_park_limit fruitless parks it takes the
    long pause (bytes kept, back by itself in download_pause_hours)."""
    base, _, seen, stop = _world()
    try:
        add_urls(store, [f"{base}/stalls.json.gz", f"{base}/good.json.gz"])
        assert _drain_with_deadline(parking, store, 120), "the queue never finished"
    finally:
        stop()
    rows = {u["url"].rsplit("/", 1)[-1]: u for u in store.list_urls()}
    assert rows["good.json.gz"]["status"] == "done"
    order = [n for n, _ in seen["order"]]
    assert order[0] == "stalls.json.gz", "the stalled file had the slot first"
    assert order.index("good.json.gz") == 1, "the next file got the slot as soon as it parked"
    assert order.count("stalls.json.gz") == 3, "went back to it: 2 parks + the turn that escalated"
    p = rows["stalls.json.gz"]
    assert p["status"] == "paused" and p["park_count"] == 0 and p["auto_resumes"] == 1
    assert "stopped sending data" in p["error"]


def test_a_parked_file_resumes_where_it_stopped_when_the_server_comes_back(parking, store):
    back = threading.Event()
    base, body, seen, stop = _world(silent_after=2 << 20, recover=back)
    try:
        add_urls(store, [f"{base}/stalls.json.gz"])
        url = f"{base}/stalls.json.gz"
        dest = parking.downloads_dir / filename_for(url)
        rec = store.next_queued_url()
        from mrfx.fetch import fetch_url_record
        assert fetch_url_record(parking, store, rec) is False          # parked
        row = store.list_urls()[0]
        assert row["status"] == "paused" and row["park_count"] == 1
        assert "parked:" in row["error"] and "goes back in line in 10 min" in row["error"]
        assert store.parked_count() == 1
        back.set()                                                     # the server recovers
        assert store.wake_parked_urls(only_if_idle=True) == 1         # nothing else waiting
        rec = store.next_queued_url()
        assert fetch_url_record(parking, store, rec) is True           # the server came back
    finally:
        stop()
    # (a resume no validator vouched for is then checked against a 64 KB
    # sample of what was kept — review 11 item 1.2 — so it is not the last)
    assert f"bytes={2 << 20}-" in seen["ranges"], "resumed from the kept 2 MB, not byte 0"
    assert hashlib.sha256(dest.read_bytes()).digest() == hashlib.sha256(body).digest()
    row = store.list_urls()[0]
    assert row["status"] == "fetched" and row["park_count"] == 0


def test_parked_files_wait_their_turn_when_fresh_files_are_queued(store):
    from mrfx.fetch import dedup_key
    a = store.enqueue_url("https://x/a.json.gz", dedup_key("https://x/a.json.gz"))
    store.enqueue_url("https://x/b.json.gz", dedup_key("https://x/b.json.gz"))
    store.park_url(a, 10, 1, "parked: test")
    assert store.wake_parked_urls(only_if_idle=True) == 0, "a fresh file is still waiting"
    assert store.next_queued_url()["url"].endswith("b.json.gz")
    assert store.wake_parked_urls(only_if_idle=True) == 1
    assert store.next_queued_url()["url"].endswith("a.json.gz")


def test_park_is_off_when_it_could_never_fire_first(cfg):
    """A park window at or past the stall window would never be reached; the
    stall rule then decides alone (the old behaviour, unchanged)."""
    from mrfx.config import MrfxConfig
    assert MrfxConfig().download_park_seconds == 240.0
    assert MrfxConfig().download_park_seconds < MrfxConfig().download_stall_seconds


# ---------------------------------------------------------------------------
# A TRICKLING server (field report, 0.9.1): the connection stays open and a
# few bytes arrive every moment — never silent long enough for the read
# timeout, never a full 64 KB read buffer. The park / stall / time-cap checks
# ran only when a full buffer was handed over, so the row sat at "0 MB,
# downloading — no data for 10846 s" and never parked or paused.


def _trickle_world(total: int = 4 << 20, ranges: bool = False):
    """/trickle.json.gz announces `total` bytes, then sends 10 bytes every
    0.1 s (inside the 0.5 s read timeout) until stopped. With `ranges`, a
    Range request is answered 206 the same way (so segments engage)."""
    release = threading.Event()
    seen = {"order": []}

    class H(hs.BaseHTTPRequestHandler):
        def do_GET(self):
            name = self.path.rsplit("/", 1)[-1]
            seen["order"].append((name, time.monotonic()))
            if name == "good.json.gz":
                self.send_response(200)
                self.send_header("Content-Length", str(len(GOOD)))
                self.end_headers()
                self.wfile.write(GOOD)
                return
            rng = self.headers.get("Range")
            if ranges and rng:
                a, b = rng.split("=")[1].split("-")
                start, end = int(a), (int(b) if b else total - 1)
                self.send_response(206)
                self.send_header("Content-Range", f"bytes {start}-{end}/{total}")
                self.send_header("Content-Length", str(end - start + 1))
                self.end_headers()
                if end - start + 1 <= 1:
                    self.wfile.write(b"x")
                    return
            else:
                self.send_response(200)
                self.send_header("Content-Length", str(total))
                self.send_header("Accept-Ranges", "bytes")
                self.end_headers()
            try:
                while not release.wait(0.1):
                    self.wfile.write(b"x" * 10)
                    self.wfile.flush()
            except OSError:
                pass

        def log_message(self, *a):
            pass
    httpd = hs.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"

    def stop():
        release.set()
        httpd.shutdown()
    return base, seen, stop


def _download_in_thread(cfg, url, limit_s):
    out = {}

    def run():
        t0 = time.monotonic()
        try:
            download(cfg, url, cfg.downloads_dir / filename_for(url))
            out["err"] = None
        except DownloadError as e:
            out["err"] = e
        out["took"] = time.monotonic() - t0
    th = threading.Thread(target=run, daemon=True)
    th.start()
    th.join(limit_s)
    assert not th.is_alive(), (
        f"still downloading after {limit_s} s: a trickling server held the slot "
        "and the park never fired")
    return out


def test_a_trickling_server_parks_the_download(parking):
    """was: still downloading after 40 s with a 4 s park window (it never
    parked; in the field 10,846 s at 0 MB)."""
    base, _, stop = _trickle_world()
    try:
        out = _download_in_thread(parking, f"{base}/trickle.json.gz", 20)
    finally:
        stop()
    e = out["err"]
    assert e is not None and e.parked and e.retryable and not e.progressed
    assert out["took"] < 10, f"parked after {out['took']:.1f} s, not ~the 1.5 s window"


def test_a_trickling_server_in_parallel_segments_parks_too(parking):
    """Segments: a few bytes on each segment counted as progress, so the
    segmented attempt never ended. Now under 1 MB on all of them in the park
    window ends it; the single connection it falls back to parks."""
    parking.download_segments = 4
    base, seen, stop = _trickle_world(total=400 << 20, ranges=True)
    try:
        out = _download_in_thread(parking, f"{base}/trickle.json.gz", 30)
    finally:
        stop()
    e = out["err"]
    assert e is not None and e.parked, e
    assert len(seen["order"]) >= 1 + 4, "the probe and the segments ran first"
    assert out["took"] < 20, f"took {out['took']:.1f} s"


def test_one_slot_a_trickling_payer_steps_aside_then_takes_the_long_pause(parking, store):
    """The whole lifecycle with ONE slot and the trickling file first: the
    next file downloads while it is parked, and after download_park_limit
    fruitless parks it is paused (back by itself in download_pause_hours)."""
    base, seen, stop = _trickle_world()
    try:
        add_urls(store, [f"{base}/trickle.json.gz", f"{base}/good.json.gz"])
        assert _drain_with_deadline(parking, store, 120), "the queue never finished"
    finally:
        stop()
    rows = {u["url"].rsplit("/", 1)[-1]: u for u in store.list_urls()}
    assert rows["good.json.gz"]["status"] == "done"
    order = [n for n, _ in seen["order"]]
    assert order[0] == "trickle.json.gz" and order.index("good.json.gz") == 1
    p = rows["trickle.json.gz"]
    assert p["status"] == "paused" and p["auto_resumes"] == 1, p
    assert "stopped sending data" in p["error"] and "24 hours" in p["error"]


# ---------------------------------------------------------------------------
# "skip" on a downloading row (0.9.1): stop it now, keep its bytes, start the
# next file, and put it at the back of the line.


def _start_fetch(cfg, store, name):
    from mrfx.fetch import fetch_url_record
    rec = store.next_queued_url()
    assert rec["url"].endswith(name)
    out = {}

    def run():
        out["ret"] = fetch_url_record(cfg, store, rec)
        out["t"] = time.monotonic()
    th = threading.Thread(target=run, daemon=True)
    th.start()
    for _ in range(200):                       # until it is really downloading
        if any(u["status"] == "downloading" for u in store.list_urls()):
            break
        time.sleep(0.05)
    return rec, th, out


@pytest.mark.parametrize("server", ["silent", "midfile", "trickle"])
def test_skip_stops_a_download_at_once_and_keeps_its_bytes(parking, store, server):
    """Skip frees the slot within seconds whatever the server is doing — even
    one blocked in a 30 s read — keeps the bytes, and leaves the row paused
    with the skip message (never 'failed'). The next file then gets the slot."""
    parking.download_timeout_seconds = 30.0     # a long blocked read
    parking.download_park_seconds = 0           # no park: only the skip ends it
    if server == "trickle":
        base, _, stop = _trickle_world()
        name = "trickle.json.gz"
    else:
        base, body, _, stop = _world(silent_after=(2 << 20) if server == "midfile" else 0)
        name = "stalls.json.gz"
    try:
        add_urls(store, [f"{base}/{name}", f"{base}/good.json.gz"])
        rec, th, out = _start_fetch(parking, store, name)
        time.sleep(0.5)
        t0 = time.monotonic()
        assert store.skip_download(rec["id"]) is True
        th.join(15)
        assert not th.is_alive(), "skip did not free the slot"
        took = out["t"] - t0
        assert out["ret"] is False
        row = next(u for u in store.list_urls() if u["id"] == rec["id"])
        assert row["status"] == "paused" and "skipped by you" in row["error"], row
        assert not store.is_download_canceled(rec["id"]), "the one-shot flag was cleared"
        dest = parking.downloads_dir / filename_for(rec["url"])
        if server == "midfile":
            part = dest.with_suffix(dest.suffix + ".part")
            assert part.read_bytes() == body[:2 << 20], "the bytes so far are kept"
        # the next file is up next; the skipped one rests (an empty line
        # does not wake it), then rejoins
        assert store.next_queued_url()["url"].endswith("good.json.gz")
        assert store.wake_parked_urls(only_if_idle=True) == 0
        assert store.wake_paused_urls() == 0, "it rests for download_park_minutes"
    finally:
        stop()
    assert took < 5, f"skip took {took:.1f} s to free the slot ({server} server)"


def test_a_skipped_file_waits_24_hours_in_its_own_category(store):
    """0.9.2: skip -> its own "retry in 24 h" category (counted apart from the
    paused ones), back in the queue only when the 24 h are up; "try now" runs
    its one more try at once; "retry" on a failed row is a fresh start."""
    from mrfx.fetch import dedup_key
    a = store.enqueue_url("https://x/a.json.gz", dedup_key("https://x/a.json.gz"))
    store.update_url(a, status="downloading")
    assert store.skip_download(a) is True
    store.clear_download_cancel(a)
    row = store.list_urls()[0]
    assert row["status"] == "paused" and row["retry_once"] is True
    assert "tries again by itself in 24 hours" in row["error"]
    assert store.url_queue_counts() == {"retry_later": 1}
    assert store.wake_paused_urls() == 0 and store.wake_parked_urls(only_if_idle=True) == 0
    with store.connect() as con:                     # 24 h later
        con.execute("UPDATE url_queue SET retry_after = retry_after - INTERVAL 25 HOUR")
    assert store.wake_paused_urls() == 1
    rec = store.next_queued_url()
    assert rec["id"] == a and rec["retry_once"] is True, "the one more try is flagged"
    # "try now" keeps the flag; a failed row's "retry" clears it
    store.update_url(a, status="downloading")
    assert store.skip_download(a, hours=24) is True
    store.clear_download_cancel(a)
    assert store.set_url_status_by_id(a, "queued") is True
    assert store.list_urls()[0]["retry_once"] is True
    store.update_url(a, status="failed", retry_once=False)
    assert store.set_url_status_by_id(a, "queued") is True
    assert store.list_urls()[0]["retry_once"] is False


def _skipped_and_due(store, url):
    """A row in the "retry in 24 h" category whose 24 h are up."""
    from mrfx.fetch import dedup_key
    a = store.enqueue_url(url, dedup_key(url))
    store.update_url(a, status="downloading")
    assert store.skip_download(a, hours=0) is True
    store.clear_download_cancel(a)
    assert store.wake_paused_urls() == 1
    return a


def test_the_retry_of_a_skipped_file_fails_after_the_quiet_window(parking, store, monkeypatch):
    """Its one more try: nothing arriving for SKIP_RETRY_QUIET_SECONDS ->
    failed (no parks, no 24 h pauses), with a sentence saying so."""
    monkeypatch.setattr(type(store), "SKIP_RETRY_QUIET_SECONDS", 2.0)
    base, _, _, stop = _world()
    try:
        a = _skipped_and_due(store, f"{base}/stalls.json.gz")
        from mrfx.fetch import fetch_url_record
        t0 = time.monotonic()
        assert fetch_url_record(parking, store, store.next_queued_url()) is False
        took = time.monotonic() - t0
    finally:
        stop()
    row = store.list_urls()[0]
    assert row["id"] == a and row["status"] == "failed" and row["retry_once"] is False, row
    assert "skipped earlier and tried again" in row["error"] and "Press retry" in row["error"]
    assert took < 15, f"failed after {took:.1f} s, not ~the 2 s quiet window"


def test_the_retry_of_a_skipped_file_loads_it_when_it_works(parking, store):
    base, _, _, stop = _world()
    try:
        _skipped_and_due(store, f"{base}/good.json.gz")
        from mrfx.fetch import fetch_url_record
        assert fetch_url_record(parking, store, store.next_queued_url()) is True
    finally:
        stop()
    row = store.list_urls()[0]
    assert row["status"] == "fetched" and row["retry_once"] is False


def test_skip_answers_at_once_while_the_database_is_busy(parking, store):
    """The writer lock held by something long (a part swap, a rollup's DDL):
    skip still stops the download and answers within seconds; the download
    thread writes the new status as it steps aside. was: skip waited on the
    lock — "I press the skip button and nothing happens"."""
    parking.download_timeout_seconds = 30.0
    parking.download_park_seconds = 0
    base, _, _, stop = _world()
    try:
        add_urls(store, [f"{base}/stalls.json.gz"])
        rec, th, out = _start_fetch(parking, store, "stalls.json.gz")
        held, release = threading.Event(), threading.Event()

        def hold():
            with store.write_lock:
                held.set()
                release.wait(20)
        threading.Thread(target=hold, daemon=True).start()
        held.wait(5)
        t0 = time.monotonic()
        assert store.skip_download(rec["id"]) is True
        assert time.monotonic() - t0 < 5, "skip waited on the busy database"
        release.set()
        th.join(15)
        assert not th.is_alive()
    finally:
        stop()
    row = store.list_urls()[0]
    assert row["status"] == "paused" and row["retry_once"] is True, row
    assert "skipped by you" in row["error"]


def test_skip_refuses_a_row_that_is_not_downloading(store):
    from mrfx.fetch import dedup_key
    a = store.enqueue_url("https://x/a.json.gz", dedup_key("https://x/a.json.gz"))
    assert store.skip_download(a) is False
    assert not store.is_download_canceled(a)


def test_skip_endpoint(cfg, store):
    from fastapi.testclient import TestClient

    from mrfx.api import create_app
    from mrfx.fetch import dedup_key
    a = store.enqueue_url("https://x/a.json.gz", dedup_key("https://x/a.json.gz"))
    c = TestClient(create_app(cfg, store))
    assert c.post(f"/api/urls/{a}/skip").status_code == 409
    store.update_url(a, status="downloading")
    r = c.post(f"/api/urls/{a}/skip")
    assert r.status_code == 200 and r.json() == {"status": "paused"}
    store.clear_download_cancel(a)


def test_skip_stops_parallel_segments_at_once(parking, store):
    """Segments blocked on a silent server: skip ends the attempt at once and
    the row is the skipped one (not failed, not fallen back to download on)."""
    parking.download_segments = 4
    parking.download_timeout_seconds = 30.0
    parking.download_park_seconds = 0
    base, _, stop = _trickle_world(total=400 << 20, ranges=True)
    try:
        add_urls(store, [f"{base}/trickle.json.gz"])
        rec, th, out = _start_fetch(parking, store, "trickle.json.gz")
        time.sleep(1.0)
        t0 = time.monotonic()
        assert store.skip_download(rec["id"]) is True
        th.join(15)
        assert not th.is_alive(), "skip did not end the segmented download"
        took = out["t"] - t0
    finally:
        stop()
    row = store.list_urls()[0]
    assert out["ret"] is False and row["status"] == "paused" and "skipped by you" in row["error"]
    assert took < 5, f"took {took:.1f} s"
