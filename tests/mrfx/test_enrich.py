"""NPPES enrichment poisoning guards: transient failures must never write a
permanent "dead NPI" row — only a genuine empty result may."""

import http.server
import json
import threading

import pytest

import mrfx.enrich as enrich_mod
from mrfx.enrich import enrich_via_api
from tests.mrfx.conftest import drop
from mrfx.ingest import scan_inbox


class _Responder(http.server.BaseHTTPRequestHandler):
    payload: bytes = b"{}"
    status: int = 200

    def do_GET(self):
        self.send_response(self.status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(self.payload)

    def log_message(self, *a):
        pass


@pytest.fixture()
def nppes(monkeypatch):
    _Responder.payload = b"{}"
    _Responder.status = 200
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Responder)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    monkeypatch.setattr(enrich_mod, "NPPES_API",
                        f"http://127.0.0.1:{httpd.server_address[1]}/api/")
    monkeypatch.setattr(enrich_mod.time, "sleep", lambda s: None)
    yield _Responder
    httpd.shutdown()


def _seed_npis(cfg, store):
    drop(cfg, "innetwork_mixed.json", gz=True)
    scan_inbox(cfg, store)
    assert store.unenriched_npis(limit=10)


def test_nppes_200_error_body_does_not_poison(cfg, store, nppes):
    # NPPES 200-wraps errors: {"Errors": [...]} with NO "results" key. That
    # must count as a failure to retry, never as a dead NPI.
    _seed_npis(cfg, store)
    before = set(store.unenriched_npis(limit=100))
    assert before
    nppes.payload = json.dumps({"Errors": [{"description": "temporarily busted"}]}).encode()
    done = enrich_via_api(cfg, store)
    assert done == 0
    assert set(store.unenriched_npis(limit=100)) == before  # nothing poisoned


def test_nppes_non_200_does_not_poison(cfg, store, nppes):
    _seed_npis(cfg, store)
    before = set(store.unenriched_npis(limit=100))
    assert before
    nppes.payload = b'{"message": "rate limited"}'
    nppes.status = 429  # retried once, then non-200 path
    done = enrich_via_api(cfg, store)
    assert done == 0
    assert set(store.unenriched_npis(limit=100)) == before


def test_nppes_genuine_empty_result_marks_dead(cfg, store, nppes):
    # a real "no such NPI" answer (results key present, empty) IS recorded so
    # dead NPIs aren't retried forever
    _seed_npis(cfg, store)
    nppes.payload = json.dumps({"result_count": 0, "results": []}).encode()
    enrich_via_api(cfg, store)
    assert store.unenriched_npis(limit=100) == []


def test_concurrent_enrichment_names_every_npi(cfg, store, nppes):
    # the concurrent path must enrich the WHOLE batch (names + state), not just
    # the first — this is what makes the dashboard's names/state filter populate.
    _seed_npis(cfg, store)
    cfg.enrichment.api_concurrency = 4
    nppes.payload = json.dumps({"result_count": 1, "results": [{
        "enumeration_type": "NPI-1",
        "basic": {"organization_name": "Acme Rehab LLC"},
        "taxonomies": [{"primary": True, "code": "225100000X", "desc": "Physical Therapist"}],
        "addresses": [{"address_purpose": "LOCATION", "city": "Austin", "state": "TX"}],
    }]}).encode()
    n = enrich_via_api(cfg, store)
    assert n >= 2 and store.unenriched_npis(limit=100) == []  # all enriched, none left
    assert "TX" in store.available_states()                    # state now filterable
