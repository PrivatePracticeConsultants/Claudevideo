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
    assert seen["ranges"][-1] == f"bytes={2 << 20}-", "resumed from the kept 2 MB, not byte 0"
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
