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
        after_mb = int(re.search(r"skipped after ([\d,]+) MB", b["error"]).group(1))
        assert after_mb <= 2 < len(a) / 1e6, b["error"]
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
