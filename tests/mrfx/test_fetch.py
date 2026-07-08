"""URL-drop ingestion tests: direct file URLs, TOC auto-expansion, web-page
handling, dedup, retry, and the oversize guard — against a local HTTP server."""

import gzip
import http.server
import json
import threading

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


def test_oversize_guard(cfg, store, server):
    cfg.confirm_over_gb = 64 / 1e9  # 64 bytes — the 128-byte file trips it
    add_urls(store, [f"{server}/big_header.bin"])
    drain(cfg, store)
    rec = next(r for r in store.list_urls() if "big_header" in r["url"])
    assert rec["status"] == "failed"
    assert "confirm_over_gb" in rec["error"]


def test_filename_for_is_safe_and_distinct():
    a = filename_for("https://x.com/payerA/2026-06_in-network-rates_1_of_2.json.gz?sig=1")
    b = filename_for("https://x.com/payerB/2026-06_in-network-rates_1_of_2.json.gz")
    assert a != b                      # same basename, different path -> distinct
    assert a.endswith(".json.gz")
    c = filename_for("https://x.com/app/public/")
    assert c.endswith(".json")         # page-ish URL still gets a usable name
