"""`mrfx sources --check` (round 3, P3): the catalog's monthly re-audit in
one command — read-only, the app's own User-Agent, plain OK/CHANGED/DEAD."""

from __future__ import annotations

import gzip
import http.server
import json
import threading

import pytest

from mrfx.source_check import check_sources


@pytest.fixture
def site():
    pages = {
        "/toc.json": (b"application/json", json.dumps({"reporting_entity_name": "X", "reporting_structure": [
            {"in_network_files": [{"location": "https://x/a.json.gz"}]}]}).encode()),
        "/toc.json.gz": (b"application/gzip", gzip.compress(json.dumps(
            {"reporting_structure": []}).encode())),
        "/files.html": (b"text/html", b'<html><a href="https://x/a_in-network.json.gz">a</a></html>'),
        "/portal.html": (b"text/html", b"<html><script>app()</script></html>"),
        "/other.json": (b"application/json", b'{"hello": "world"}'),
    }
    seen_ua = []

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            seen_ua.append(self.headers.get("User-Agent"))
            if self.path not in pages:
                self.send_response(404)
                self.end_headers()
                return
            ctype, body = pages[self.path]
            self.send_response(200)
            self.send_header("Content-Type", ctype.decode())
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}", seen_ua
    srv.shutdown()


def test_check_reports_ok_changed_dead(cfg, site):
    base, seen_ua = site
    paths = ["/toc.json", "/toc.json.gz", "/files.html", "/portal.html", "/other.json", "/gone.json"]
    sources = [{"name": p, "url": base + p, "queueable": True, "states": ["MO"]} for p in paths]
    got = {r["name"]: r["result"] for r in check_sources(cfg, sources, workers=3)}
    assert got == {"/toc.json": "OK", "/toc.json.gz": "OK", "/files.html": "OK",
                   "/portal.html": "NEEDS-BROWSER", "/other.json": "CHANGED",
                   "/gone.json": "DEAD"}
    # a portal-only catalog entry that answers is reported as such
    portal = [{"name": "p", "url": base + "/portal.html", "queueable": False, "states": ["MO"]}]
    assert check_sources(cfg, portal)[0]["result"] == "PORTAL"
    assert set(seen_ua) == {cfg.user_agent}          # the app's own UA, never a disguise
