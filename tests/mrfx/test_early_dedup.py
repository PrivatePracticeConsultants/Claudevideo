"""Early duplicate detection (fetch.early_duplicate_check) and size lanes.

Payers host copies of each other's files under different web addresses; a
copy used to be caught only after its full download (content sha256). Now a
download is stopped early only when it is PROVEN a copy of a file already
LOADED: exactly the same size, the same first `early_dedup_mb` MB, the same
last 1 MB and the same 8 samples between — and compressed, so that its end
carries a CRC of its whole content. A plain .json can differ in one rate in
the middle while matching all of that, so it always downloads in full and
the full-content check decides (audit 8 F3).
"""
from __future__ import annotations

import gzip
import http.server as hs
import json
import random
import re
import threading

from mrfx.fetch import add_urls, run_queue


def _doc(last_rate: float = 40.0, mid_rate: float | None = None, n: int = 40000) -> dict:
    """A real in-network file: n rate groups with random-looking extra text
    (so gzip cannot shrink it below a few MB), the middle group's rate
    `mid_rate` and the last group's rate `last_rate` (both 2-dp, same width)."""
    rnd = random.Random(7)
    groups = []
    for i in range(n):
        rate = round(rnd.uniform(20, 90), 2)
        if mid_rate is not None and i == n // 2:
            rate = mid_rate
        groups.append({
            "provider_groups": [{"npi": [rnd.randrange(10**9, 2 * 10**9)],
                                 "tin": {"type": "ein", "value": f"{rnd.randrange(10**8, 10**9)}"}}],
            "negotiated_prices": [{"negotiated_type": "negotiated", "negotiated_rate": rate,
                                   "billing_class": "professional", "service_code": ["11"],
                                   "billing_code_modifier": ["GP"],
                                   "additional_information": "%064x" % rnd.getrandbits(256)}]})
    groups.append({"provider_groups": [{"npi": [1999999999],
                                        "tin": {"type": "ein", "value": "439999999"}}],
                   "negotiated_prices": [{"negotiated_type": "negotiated",
                                          "negotiated_rate": last_rate,
                                          "billing_class": "professional",
                                          "service_code": ["11"],
                                          "billing_code_modifier": ["GP"]}]})
    return {"reporting_entity_name": "Twin Health", "reporting_entity_type": "issuer",
            "last_updated_on": "2026-06-01", "version": "2.0.0", "provider_references": [],
            "in_network": [{"negotiation_arrangement": "ffs", "billing_code_type": "CPT",
                            "billing_code_type_version": "2026", "billing_code": "97110",
                            "negotiated_rates": groups}]}


_CACHE: dict = {}


def _gz(last_rate: float = 40.0) -> bytes:
    k = ("gz", last_rate)
    if k not in _CACHE:
        _CACHE[k] = gzip.compress(json.dumps(_doc(last_rate)).encode(), mtime=0)
    return _CACHE[k]


def _plain(mid_rate: float) -> bytes:
    k = ("plain", mid_rate)
    if k not in _CACHE:
        _CACHE[k] = json.dumps(_doc(40.0, mid_rate=mid_rate, n=12000)).encode()
    return _CACHE[k]


class _Srv:
    """A local file server with Range support; `deny` paths answer 403,
    `nolen` paths stream without a Content-Length and refuse ranges."""

    def __init__(self, files: dict[str, bytes]):
        self.files, self.deny, self.nolen = files, set(), set()
        self.browser_only = False
        self.hits: dict[str, int] = {}
        self.ranges: list[str] = []
        srv = self

        class H(hs.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self):
                path = self.path.split("?", 1)[0]
                srv.hits[path] = srv.hits.get(path, 0) + 1
                body = srv.files.get(path)
                if srv.browser_only and "Mozilla" not in self.headers.get("User-Agent", ""):
                    body, path = b"", path + "#ua"
                    srv.deny.add(path)
                if body is None or path in srv.deny:
                    self.send_response(404 if body is None else 403)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                rng = self.headers.get("Range", "")
                if rng:
                    srv.ranges.append(rng)
                start, end = 0, len(body) - 1
                if path in srv.nolen:
                    self.send_response(200)
                    self.send_header("Connection", "close")
                    self.end_headers()
                    try:
                        self.wfile.write(body)
                    except (BrokenPipeError, ConnectionResetError):
                        pass
                    self.close_connection = True
                    return
                if rng.startswith("bytes="):
                    a, b = rng[6:].split("-")
                    start = int(a)
                    end = min(int(b) if b else len(body) - 1, len(body) - 1)
                    self.send_response(206)
                    self.send_header("Content-Range", f"bytes {start}-{end}/{len(body)}")
                else:
                    self.send_response(200)
                chunk = body[start:end + 1]
                self.send_header("Content-Length", str(len(chunk)))
                self.end_headers()
                try:
                    for i in range(0, len(chunk), 64 << 10):
                        self.wfile.write(chunk[i:i + (64 << 10)])
                except (BrokenPipeError, ConnectionResetError):
                    self.close_connection = True

            def log_message(self, *a):
                pass

        self.httpd = hs.ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.httpd.shutdown()


def _row(store, url):
    return [u for u in store.list_urls() if u["url"] == url][0]


def _after_mb(err: str) -> float:
    return float(re.search(r"skipped after ([\d.,]+) MB", err).group(1).replace(",", ""))


# -- the early skip ------------------------------------------------------------------

def test_a_compressed_copy_is_skipped_after_its_first_mb(cfg, store):
    a = _gz()
    assert len(a) > 2 << 20
    cfg.early_dedup_mb = 1
    with _Srv({"/blue-a/rates.json.gz": a, "/blue-b/rates.json.gz": a}) as s:
        add_urls(store, [s.base + "/blue-a/rates.json.gz"])
        run_queue(cfg, store, drain=True)
        assert _row(store, s.base + "/blue-a/rates.json.gz")["status"] == "done"
        add_urls(store, [s.base + "/blue-b/rates.json.gz"])
        run_queue(cfg, store, drain=True)
        b = _row(store, s.base + "/blue-b/rates.json.gz")
        assert (b["status"], b["kind"]) == ("skipped", "duplicate"), b
        assert "already loaded" in b["error"] and "8 samples between" in b["error"]
        assert s.base + "/blue-a/rates.json.gz" in b["error"], "names the link, not a file name"
        assert 1.0 <= _after_mb(b["error"]) <= 2.2 < len(a) / 1e6 + 0.1
        # the end + 8 samples came from small range requests
        assert sum(1 for r in s.ranges if r.endswith(f"-{len(a) - 1}")) >= 1
        with store.connect() as con:
            assert con.execute("SELECT count(*) FROM inflight_fingerprints").fetchone()[0] == 0


def test_the_end_check_uses_the_identity_the_download_needed(cfg, store):
    """Audit 8 F5: a CDN that refuses non-browser agents. The download
    switches to a browser identity; the end/sample range requests must too,
    or every copy from that CDN downloads in full."""
    a = _gz()
    cfg.early_dedup_mb = 1
    with _Srv({"/a/r.json.gz": a, "/b/r.json.gz": a}) as s:
        s.browser_only = True
        add_urls(store, [s.base + "/a/r.json.gz"])
        run_queue(cfg, store, drain=True)
        assert _row(store, s.base + "/a/r.json.gz")["status"] == "done"
        add_urls(store, [s.base + "/b/r.json.gz"])
        run_queue(cfg, store, drain=True)
        b = _row(store, s.base + "/b/r.json.gz")
        assert (b["status"], b["kind"]) == ("skipped", "duplicate") and "8 samples" in b["error"], b


def test_a_plain_json_differing_only_in_the_middle_is_never_skipped(cfg, store):
    """Audit 8 F3: same size, same first MB, same last MB — one rate in the
    middle differs. Uncompressed, so nothing at its end vouches for the
    middle: it downloads in full and its distinct rate reaches the store."""
    x, y = _plain(98.88), _plain(99.99)   # outside the random 20-90 range
    assert len(x) == len(y) > 2 << 20 and x[:1 << 20] == y[:1 << 20] and x[-(1 << 20):] == y[-(1 << 20):]
    cfg.early_dedup_mb = 1
    with _Srv({"/x/rates.json": x, "/y/rates.json": y}) as s:
        add_urls(store, [s.base + "/x/rates.json"])
        run_queue(cfg, store, drain=True)
        add_urls(store, [s.base + "/y/rates.json"])
        run_queue(cfg, store, drain=True)
        assert _row(store, s.base + "/y/rates.json")["status"] == "done"
        with store.connect() as con:
            assert con.execute("SELECT count(*) FROM rates WHERE negotiated_rate = 99.99"
                               ).fetchone()[0] == 1


def test_the_same_link_again_is_never_downloaded(cfg, store):
    """Re-pasting a link that is already done — even with a fresh signature
    — queues nothing: the link itself is the identity (signature stripped)."""
    from mrfx.fetch import dedup_key
    rid = store.enqueue_url("https://cdn.example/x/rates.json.gz?sig=1&Expires=1",
                            dedup_key("https://cdn.example/x/rates.json.gz?sig=1&Expires=1"))
    store.update_url(rid, status="done")
    res = add_urls(store, ["https://cdn.example/x/rates.json.gz?sig=2&Expires=2"])
    assert res == {"added": 0, "skipped": 1, "invalid": 0}
    assert [u["status"] for u in store.list_urls()] == ["done"]


def test_a_fingerprint_taken_with_a_longer_head_is_checked_when_that_much_arrived(cfg, store):
    """Files loaded while the setting was larger carry a longer head: the
    copy is compared once that much has arrived."""
    a = _gz()
    with _Srv({"/x/rates.json.gz": a, "/y/rates.json.gz": a}) as s:
        cfg.early_dedup_mb = 2
        add_urls(store, [s.base + "/x/rates.json.gz"])
        run_queue(cfg, store, drain=True)
        cfg.early_dedup_mb = 1
        add_urls(store, [s.base + "/y/rates.json.gz"])
        run_queue(cfg, store, drain=True)
        y = _row(store, s.base + "/y/rates.json.gz")
        assert (y["status"], y["kind"]) == ("skipped", "duplicate"), y
        assert 2.0 <= _after_mb(y["error"]) < len(a) / 1e6


def test_the_default_early_check_is_15_mb():
    from mrfx.config import MrfxConfig
    assert MrfxConfig().early_dedup_mb == 15


# -- copies in flight ------------------------------------------------------------------

def test_a_waiting_copy_never_reconnects_and_is_skipped_without_its_link(cfg, store):
    """Audit 8 F1 + F6. B starts identically to A, which is downloaded but not
    loaded. B waits — and is NOT woken (no reconnect, no request) while A is
    in flight, though the queue is idle and its timer is due. Once A is
    loaded, B wakes and is skipped from what it already knows: its link
    meanwhile answers 403 (an expired signature) and is never asked."""
    import mrfx.fetch as F
    a = _gz()
    cfg.early_dedup_mb = 1
    with _Srv({"/p/rates.json.gz": a, "/q/rates.json.gz": a}) as s:
        add_urls(store, [s.base + "/p/rates.json.gz", s.base + "/q/rates.json.gz"])
        first = store.next_queued_url()
        assert F.fetch_url_record(cfg, store, first) is True        # downloaded, NOT loaded
        second = store.next_queued_url()
        assert F.fetch_url_record(cfg, store, second) is False
        q = _row(store, s.base + "/q/rates.json.gz")
        assert q["status"] == "paused" and q["error"].startswith("waiting:"), q
        asked = s.hits["/q/rates.json.gz"]
        with store.connect() as con:                                 # its timer is due
            con.execute("UPDATE url_queue SET retry_after = now() - INTERVAL 1 HOUR "
                        "WHERE id = ?", [second["id"]])
        assert store.wake_parked_urls(only_if_idle=True) == 0
        assert store.wake_paused_urls() == 0
        assert s.hits["/q/rates.json.gz"] == asked, "no reconnect while A is in flight"
        assert F.process_url_record(cfg, store, store.next_fetched_url(),
                                    rebuild_rollups=False) is True
        s.deny.add("/q/rates.json.gz")                               # B's link has expired
        assert store.wake_paused_urls() == 1
        again = store.next_queued_url()
        assert F.fetch_url_record(cfg, store, again) is False
        q = _row(store, s.base + "/q/rates.json.gz")
        assert (q["status"], q["kind"]) == ("skipped", "duplicate"), q
        assert s.hits["/q/rates.json.gz"] == asked, "decided from local data alone"


def test_a_refetch_under_its_old_name_is_not_a_loaded_twin(cfg, store):
    """Audit 8 F2. X was loaded with May's bytes; the payer republishes at the
    same address and X is re-fetched (June) but not yet loaded. Y, a copy of
    June, must not be skipped as "already loaded" — X's LOADED fingerprint is
    still May's. Y waits for X; when X's load fails, Y goes ahead."""
    import mrfx.fetch as F
    may, june = _gz(40.0), _gz(41.0)
    cfg.early_dedup_mb = 1
    files = {"/x/rates.json.gz": may, "/y/rates.json.gz": june}
    with _Srv(files) as s:
        add_urls(store, [s.base + "/x/rates.json.gz"])
        run_queue(cfg, store, drain=True)
        files["/x/rates.json.gz"] = june
        assert store.requeue_urls([s.base + "/x/rates.json.gz"]) == 1
        x = store.next_queued_url()
        assert F.fetch_url_record(cfg, store, x) is True             # June fetched, not loaded
        add_urls(store, [s.base + "/y/rates.json.gz"])
        y = store.next_queued_url()
        assert F.fetch_url_record(cfg, store, y) is False
        yr = _row(store, s.base + "/y/rates.json.gz")
        assert yr["status"] == "paused" and yr["error"].startswith("waiting:"), yr
        store.update_url(x["id"], status="failed", error="ingest failed")
        assert store.wake_paused_urls() == 1
        assert F.fetch_url_record(cfg, store, store.next_queued_url()) is True
        assert _row(store, s.base + "/y/rates.json.gz")["status"] == "fetched"


def test_the_processor_path_parks_a_waiting_copy(cfg, store):
    """Audit 8 F8: a 'fetched' row whose download vanished re-downloads in the
    processor; meeting an in-flight twin it waits — it is not failed."""
    import mrfx.fetch as F
    a = _gz()
    cfg.early_dedup_mb = 1
    with _Srv({"/p/rates.json.gz": a, "/q/rates.json.gz": a}) as s:
        add_urls(store, [s.base + "/p/rates.json.gz", s.base + "/q/rates.json.gz"])
        assert F.fetch_url_record(cfg, store, store.next_queued_url()) is True
        q = store.next_queued_url()
        store.update_url(q["id"], status="ingesting")
        assert F.process_url_record(cfg, store, q, rebuild_rollups=False) is False
        assert _row(store, s.base + "/q/rates.json.gz")["status"] == "paused"


# -- originals loaded before fingerprints ----------------------------------------------

def test_an_original_without_a_fingerprint_is_fingerprinted_by_its_first_copy(cfg, store):
    a = _gz()
    cfg.early_dedup_mb = 1
    with _Srv({"/o/r.json.gz": a, "/c1/r.json.gz": a, "/c2/r.json.gz": a}) as s:
        add_urls(store, [s.base + "/o/r.json.gz"])
        run_queue(cfg, store, drain=True)
        orig = _row(store, s.base + "/o/r.json.gz")
        store.drop_fingerprint(orig["filename"])            # "loaded last month"
        add_urls(store, [s.base + "/c1/r.json.gz"])
        run_queue(cfg, store, drain=True)
        c1 = _row(store, s.base + "/c1/r.json.gz")
        assert (c1["status"], c1["kind"]) == ("skipped", "duplicate")
        assert "same first" not in c1["error"]               # caught after the full download
        add_urls(store, [s.base + "/c2/r.json.gz"])
        run_queue(cfg, store, drain=True)
        c2 = _row(store, s.base + "/c2/r.json.gz")
        assert (c2["status"], c2["kind"]) == ("skipped", "duplicate")
        assert "8 samples between" in c2["error"]            # caught early
        # forget the original: the early-skipped copy says so
        store.forget_file(orig["filename"])
        assert "was removed by the user" in _row(store, s.base + "/c2/r.json.gz")["error"]


# -- size lanes ------------------------------------------------------------------------

def test_small_slots_take_small_files_and_hand_big_ones_over(cfg, store):
    import mrfx.fetch as F
    from mrfx.fetch import dedup_key
    big = _gz()
    with _Srv({"/big/rates.json.gz": big, "/nolen/rates.json.gz": big}) as s:
        s.nolen.add("/nolen/rates.json.gz")
        add_urls(store, [s.base + "/big/rates.json.gz", s.base + "/nolen/rates.json.gz"])
        for path in ("/big/rates.json.gz", "/nolen/rates.json.gz"):
            rec = store.next_queued_url("small_only", 1 << 20)
            assert rec["url"].endswith(path)
            assert F.fetch_url_record(cfg, store, rec, lane_max=1 << 20) is False
            row = _row(store, s.base + path)
            assert (row["status"], row["error"], row["big_lane"]) == ("queued", None, True)
            dest = cfg.downloads_dir / F.filename_for(s.base + path)
            assert not dest.with_suffix(dest.suffix + ".part").exists(), "no byte written"
        assert _row(store, s.base + "/big/rates.json.gz")["bytes_total"] == len(big)
        # handed over: the strict small slot never takes them; a helping small
        # slot does when nothing small waits (audit 8 F4); so does a big slot
        assert store.next_queued_url("small_only", 1 << 20) is None
        assert store.next_queued_url("small", 1 << 20)["url"].endswith("/big/rates.json.gz")
        assert store.next_queued_url("large", 1 << 20)["url"].endswith("/nolen/rates.json.gz")
        # a known size is never overwritten by "unknown" (audit 8 F9)
        rid = _row(store, s.base + "/big/rates.json.gz")["id"]
        store.url_progress(rid, 5, None)
        assert _row(store, s.base + "/big/rates.json.gz")["bytes_total"] == len(big)
        ids = {}
        for name, size in (("u1", 0), ("s1", 500), ("b1", 5 << 20), ("s2", 900)):
            u = f"https://cdn.example/{name}.json"
            ids[name] = store.enqueue_url(u, dedup_key(u))
            if size:
                store.update_url(ids[name], bytes_total=size)
        assert store.next_queued_url("small", 1 << 20)["id"] == ids["s1"]
        assert store.next_queued_url("large", 1 << 20)["id"] == ids["b1"]
        assert store.next_queued_url("large", 1 << 20)["id"] == ids["u1"]
        assert store.next_queued_url("small_only", 1 << 20)["id"] == ids["s2"]


def test_lanes_drain_a_mixed_queue(cfg, store):
    big = _gz()
    smalls = {f"/s{i}/rates.json": json.dumps(
        {"reporting_entity_name": f"S{i} Health", "reporting_entity_type": "issuer",
         "last_updated_on": "2026-06-01", "version": "2.0.0", "provider_references": [],
         "in_network": [{"negotiation_arrangement": "ffs", "billing_code_type": "CPT",
                         "billing_code_type_version": "2026", "billing_code": "97110",
                         "negotiated_rates": [{"provider_groups": [
                             {"npi": [1500000000 + i], "tin": {"type": "ein", "value": f"5{i}0000000"}}],
                             "negotiated_prices": [{"negotiated_type": "negotiated",
                                                    "negotiated_rate": 30.0 + i,
                                                    "billing_class": "professional",
                                                    "service_code": ["11"]}]}]}]}).encode()
        for i in range(3)}
    cfg.parallel_downloads, cfg.small_file_downloads, cfg.small_file_mb = 3, 2, 1
    with _Srv({"/big/rates.json.gz": big, **smalls}) as s:
        add_urls(store, [s.base + p for p in ["/big/rates.json.gz", *smalls]])
        run_queue(cfg, store, drain=True)
        assert {u["url"].rsplit("/", 2)[-2]: u["status"] for u in store.list_urls()} == {
            "big": "done", "s0": "done", "s1": "done", "s2": "done"}
