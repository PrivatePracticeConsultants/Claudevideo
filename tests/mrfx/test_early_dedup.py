"""Early duplicate detection (fetch.early_duplicate_check).

Payers host copies of each other's files under different web addresses; the
copy used to be caught only after its full download (content sha256). Now a
download is stopped as soon as it is PROVEN to be a copy of a file already
ingested: exactly the same size, the same first `early_dedup_mb` MB, and the
same last 1 MB (fetched with one small range request). A file that only
STARTS the same — payers put the same huge provider list at the top of many
different files — is downloaded and ingested as usual.
"""
from __future__ import annotations

import http.server as hs
import json
import threading

from mrfx.fetch import add_urls, run_queue


def _mrf(last_rate: float) -> bytes:
    """A real in-network file of ~2.6 MB: a big identical top section (many
    rate groups), and a LAST item whose rate is `last_rate` — two files that
    differ only there have the same size and the same first megabytes."""
    groups = [{"provider_groups": [{"npi": [1000000000 + i],
                                    "tin": {"type": "ein", "value": f"43{i:07d}"}}],
               "negotiated_prices": [{"negotiated_type": "negotiated",
                                      "negotiated_rate": 40.0, "billing_class": "professional",
                                      "service_code": ["11"], "billing_code_modifier": ["GP"]}]}
              for i in range(12000)]
    last = {"provider_groups": [{"npi": [1999999999], "tin": {"type": "ein",
                                                              "value": "439999999"}}],
            "negotiated_prices": [{"negotiated_type": "negotiated",
                                   "negotiated_rate": last_rate, "billing_class": "professional",
                                   "service_code": ["11"], "billing_code_modifier": ["GP"]}]}
    doc = {"reporting_entity_name": "Twin Health", "reporting_entity_type": "issuer",
           "last_updated_on": "2026-06-01", "version": "2.0.0", "provider_references": [],
           "in_network": [{"negotiation_arrangement": "ffs", "billing_code_type": "CPT",
                           "billing_code_type_version": "2026", "billing_code": "97110",
                           "negotiated_rates": groups + [last]}]}
    return json.dumps(doc).encode()


ranges: list[str] = []


def _server(files: dict[str, bytes], sent: dict[str, int]):
    class H(hs.BaseHTTPRequestHandler):
        def do_GET(self):
            path = self.path.split("?", 1)[0]
            body = files.get(path)
            if body is None:
                self.send_response(404)
                self.end_headers()
                return
            rng = self.headers.get("Range", "")
            if rng:
                ranges.append(rng)
            start, end = 0, len(body) - 1
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
                    sent[path] = sent.get(path, 0) + len(chunk[i:i + (64 << 10)])
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, *a):
            pass

    httpd = hs.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


def _row(store, url):
    return [u for u in store.list_urls() if u["url"] == url][0]


def test_a_copy_under_another_address_is_skipped_after_the_first_mb(cfg, store):
    a, changed_tail = _mrf(40.0), _mrf(47.0)
    assert len(a) == len(changed_tail) and len(a) > 2 << 20
    files = {"/blue-a/rates.json": a, "/blue-b/rates.json": a,
             "/blue-c/rates.json": changed_tail}
    sent: dict[str, int] = {}
    httpd = _server(files, sent)
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    cfg.early_dedup_mb = 1
    try:
        add_urls(store, [base + "/blue-a/rates.json"])
        run_queue(cfg, store, drain=True)
        assert _row(store, base + "/blue-a/rates.json")["status"] == "done"

        # the same bytes under another address: stopped after ~1 MB
        add_urls(store, [base + "/blue-b/rates.json"])
        run_queue(cfg, store, drain=True)
        b = _row(store, base + "/blue-b/rates.json")
        assert (b["status"], b["kind"]) == ("skipped", "duplicate"), b
        assert "same first 1 MB and same last 1 MB" in b["error"]
        # the download stopped once ~1 MB had arrived (the rest of the 3.2 MB
        # file was never written), and the tail came from one range request
        import re
        after_mb = float(re.search(r"skipped after ([\d.,]+) MB", b["error"]).group(1))
        assert 1.0 <= after_mb <= 2.2 < len(a) / 1e6, b["error"]
        assert any(r.startswith("bytes=") and r.endswith(f"-{len(a) - 1}") for r in ranges)

        # same size, same first MB, different ending: NOT a copy — ingested
        add_urls(store, [base + "/blue-c/rates.json"])
        run_queue(cfg, store, drain=True)
        c = _row(store, base + "/blue-c/rates.json")
        assert c["status"] == "done", c
        with store.connect() as con:
            assert con.execute("SELECT count(*) FROM rates WHERE negotiated_rate = 47.0"
                               ).fetchone()[0] == 1

        # the original is forgotten: its copy is no longer a duplicate of
        # anything and downloads in full when pasted again
        store.forget_file(_row(store, base + "/blue-a/rates.json")["filename"])
        add_urls(store, [base + "/blue-b/rates.json"])
        run_queue(cfg, store, drain=True)
        assert _row(store, base + "/blue-b/rates.json")["status"] == "done"
    finally:
        httpd.shutdown()


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
    """The setting went 100 MB -> 15 MB: files loaded under 100 MB carry a
    100 MB head. A copy of one is not compared at 15 MB (nothing to compare
    with) but again once 100 MB have arrived — here 2 MB, then 1 MB."""
    a = _mrf(40.0)
    sent: dict[str, int] = {}
    httpd = _server({"/x/rates.json": a, "/y/rates.json": a}, sent)
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        cfg.early_dedup_mb = 2
        add_urls(store, [base + "/x/rates.json"])
        run_queue(cfg, store, drain=True)
        assert _row(store, base + "/x/rates.json")["status"] == "done"
        cfg.early_dedup_mb = 1
        add_urls(store, [base + "/y/rates.json"])
        run_queue(cfg, store, drain=True)
        y = _row(store, base + "/y/rates.json")
        assert (y["status"], y["kind"]) == ("skipped", "duplicate"), y
        import re
        after_mb = float(re.search(r"skipped after ([\d.,]+) MB", y["error"]).group(1))
        assert 2.0 <= after_mb < len(a) / 1e6, y["error"]   # looked again at 2 MB
    finally:
        httpd.shutdown()


def test_the_default_early_check_is_15_mb():
    from mrfx.config import MrfxConfig
    assert MrfxConfig().early_dedup_mb == 15


# -- copies downloading at the SAME time, and originals loaded before fingerprints

def test_a_copy_waits_for_an_identical_download_already_in_flight(cfg, store):
    """Two copies queued back to back (several states' indexes list the same
    national file): neither is loaded yet, so there was nothing to compare
    with and BOTH downloaded in full. Now the later row, once its first MB
    match the earlier row's, steps aside ("waiting"); when the earlier one
    has been loaded, the later one wakes, resumes and is skipped early."""
    import mrfx.fetch as F
    a = _mrf(40.0)
    sent: dict[str, int] = {}
    httpd = _server({"/p/rates.json": a, "/q/rates.json": a}, sent)
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    cfg.early_dedup_mb = 1
    try:
        add_urls(store, [base + "/p/rates.json", base + "/q/rates.json"])
        first = store.next_queued_url()
        assert first["url"].endswith("/p/rates.json")
        assert F.fetch_url_record(cfg, store, first) is True        # downloaded, NOT loaded
        second = store.next_queued_url()
        assert F.fetch_url_record(cfg, store, second) is False
        q = _row(store, base + "/q/rates.json")
        assert q["status"] == "paused" and "waiting" in q["error"], q
        # the earlier copy is loaded; the waiting one wakes and is skipped early
        assert F.process_url_record(cfg, store, store.next_fetched_url(),
                                    rebuild_rollups=False) is True
        assert _row(store, base + "/p/rates.json")["status"] == "done"
        assert store.wake_parked_urls() == 1
        again = store.next_queued_url()
        assert F.fetch_url_record(cfg, store, again) is False
        q = _row(store, base + "/q/rates.json")
        assert (q["status"], q["kind"]) == ("skipped", "duplicate"), q
        assert "same first 1 MB and same last 1 MB" in q["error"]
    finally:
        httpd.shutdown()


def test_an_original_loaded_before_fingerprints_is_fingerprinted_by_its_first_copy(cfg, store):
    """An original loaded before this feature has no fingerprint, so its first
    copy is caught only after a full download. That copy's identical bytes now
    fingerprint the original — the NEXT copy is caught after its first MB."""
    a = _mrf(40.0)
    sent: dict[str, int] = {}
    httpd = _server({"/o/rates.json": a, "/c1/rates.json": a, "/c2/rates.json": a}, sent)
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    cfg.early_dedup_mb = 1
    try:
        add_urls(store, [base + "/o/rates.json"])
        run_queue(cfg, store, drain=True)
        orig = _row(store, base + "/o/rates.json")
        assert orig["status"] == "done"
        store.drop_fingerprint(orig["filename"])            # "loaded last month"
        add_urls(store, [base + "/c1/rates.json"])
        run_queue(cfg, store, drain=True)
        c1 = _row(store, base + "/c1/rates.json")
        assert (c1["status"], c1["kind"]) == ("skipped", "duplicate")
        assert "same first" not in c1["error"]               # caught after the full download
        assert store.fingerprint_candidates(len(a))[0]["filename"] == orig["filename"]
        add_urls(store, [base + "/c2/rates.json"])
        run_queue(cfg, store, drain=True)
        c2 = _row(store, base + "/c2/rates.json")
        assert (c2["status"], c2["kind"]) == ("skipped", "duplicate")
        assert "same first 1 MB and same last 1 MB" in c2["error"]   # caught early
    finally:
        httpd.shutdown()


# -- size lanes ------------------------------------------------------------------------

def test_small_slots_take_small_files_and_hand_big_ones_over(cfg, store):
    """Of the download slots, small_file_downloads take only files of
    small_file_mb or less. A small slot that meets a bigger file hands it
    back sized, without writing a byte; a big slot takes known-big files
    first and helps with anything when none is waiting."""
    import mrfx.fetch as F
    from mrfx.fetch import dedup_key
    big, small = _mrf(40.0), b'{"reporting_entity_name": "x"}'
    sent: dict[str, int] = {}
    httpd = _server({"/big/rates.json": big, "/small/rates.json": small}, sent)
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        add_urls(store, [base + "/big/rates.json"])
        rec = store.next_queued_url("small", 1 << 20)          # unknown size: small slot tries
        assert F.fetch_url_record(cfg, store, rec, lane_max=1 << 20) is False
        row = _row(store, base + "/big/rates.json")
        assert (row["status"], row["error"], row["bytes_total"]) == ("queued", None, len(big))
        dest = cfg.downloads_dir / F.filename_for(base + "/big/rates.json")
        assert not dest.with_suffix(dest.suffix + ".part").exists(), "no byte written"
        # now known to be big: small slots never claim it, a big slot does
        assert store.next_queued_url("small", 1 << 20) is None
        assert store.next_queued_url("large", 1 << 20)["url"].endswith("/big/rates.json")
        # claim order with sizes known: small slot -> known-small first;
        # big slot -> known-big first, then anything
        ids = {}
        for name, size in (("u1", 0), ("s1", 500), ("b1", 5 << 20), ("s2", 900)):
            u = f"https://cdn.example/{name}.json"
            ids[name] = store.enqueue_url(u, dedup_key(u))
            if size:
                store.update_url(ids[name], bytes_total=size)
        assert store.next_queued_url("small", 1 << 20)["id"] == ids["s1"]
        assert store.next_queued_url("large", 1 << 20)["id"] == ids["b1"]
        assert store.next_queued_url("large", 1 << 20)["id"] == ids["u1"]   # helps when no big file
        assert store.next_queued_url("small", 1 << 20)["id"] == ids["s2"]
    finally:
        httpd.shutdown()


def test_lanes_drain_a_mixed_queue(cfg, store):
    """Two slots, one of them small-only: a big file and three small ones all
    finish, the big one through the big slot."""
    big = _mrf(40.0)
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
    sent: dict[str, int] = {}
    httpd = _server({"/big/rates.json": big, **smalls}, sent)
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    cfg.parallel_downloads, cfg.small_file_downloads, cfg.small_file_mb = 2, 1, 1
    try:
        add_urls(store, [base + p for p in ["/big/rates.json", *smalls]])
        run_queue(cfg, store, drain=True)
        assert {u["url"].rsplit("/", 2)[-2]: u["status"] for u in store.list_urls()} == {
            "big": "done", "s0": "done", "s1": "done", "s2": "done"}
    finally:
        httpd.shutdown()
