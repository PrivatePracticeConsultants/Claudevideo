"""URL-drop ingestion tests: direct file URLs, TOC auto-expansion, web-page
handling, dedup, retry, and the oversize guard — against a local HTTP server."""

import gzip
import http.server
import json
import threading
from pathlib import Path

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


def test_pasted_link_stays_visible_after_big_expansion(cfg, store, server):
    # A big index queues hundreds of children; the row the user pasted must
    # not scroll out of the dashboard's window.
    add_urls(store, [f"{server}/toc.json"])
    drain(cfg, store)
    listed = store.list_urls(limit=1)  # window smaller than the child count
    toc = [r for r in listed if dedup_key(r["url"]) == dedup_key(f"{server}/toc.json")]
    assert toc and toc[0]["parent_id"] is None       # pasted link pinned first
    assert listed[0]["parent_id"] is None
    assert sum(1 for r in listed if r["parent_id"] is not None) == 1  # children still capped


def test_byte_identical_file_on_second_url_skipped(cfg, store, server, http_root):
    # Blue plans host copies of each other's national files: same bytes, many
    # domains. The second copy must be skipped, not re-ingested.
    (http_root / "mirror_rates.json.gz").write_bytes((http_root / "rates.json.gz").read_bytes())
    add_urls(store, [f"{server}/rates.json.gz"])
    drain(cfg, store)
    with store.connect() as con:
        rows_after_first = con.execute("SELECT count(*) FROM rates").fetchone()[0]
    add_urls(store, [f"{server}/mirror_rates.json.gz"])
    drain(cfg, store)
    mirror = next(r for r in store.list_urls() if "mirror" in r["url"])
    assert mirror["status"] == "skipped" and mirror["kind"] == "duplicate"
    assert "identical" in mirror["error"] and "rates.json.gz" in mirror["error"]
    with store.connect() as con:  # no duplicate rows entered the store
        assert con.execute("SELECT count(*) FROM rates").fetchone()[0] == rows_after_first
    assert not any(cfg.downloads_dir.iterdir())  # mirror download cleaned up


def test_oversize_guard(cfg, store, server):
    cfg.confirm_over_gb = 64 / 1e9  # 64 bytes — the 128-byte file trips it
    add_urls(store, [f"{server}/big_header.bin"])
    drain(cfg, store)
    rec = next(r for r in store.list_urls() if "big_header" in r["url"])
    assert rec["status"] == "failed"
    assert "confirm_over_gb" in rec["error"]


def test_gatsby_hub_page_crawled(cfg, store, tmp_path):
    # Sapphire/HealthSparq-style hubs (Blue KC): empty HTML page, file list in
    # Gatsby static-query JSON. Suppressed entries must be respected.
    root = tmp_path / "hub"
    (root / "page-data" / "index").mkdir(parents=True)
    (root / "page-data" / "sq" / "d").mkdir(parents=True)
    # real Gatsby builds ship framework .json links in the HTML — these must
    # be filtered out, not queued as "files" (they'd short-circuit the crawl)
    (root / "index.html").write_text(
        '<!doctype html><html><head>'
        '<link rel="manifest" href="/manifest.json">'
        '<link as="fetch" rel="preload" href="/page-data/index/page-data.json">'
        '</head><body><div id="app"></div></body></html>'
    )
    (root / "rates.json.gz").write_bytes(gzip.compress((FIXTURES / "innetwork_mixed.json").read_bytes()))

    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **k):
            super().__init__(*a, directory=str(root), **k)

        def log_message(self, *a):
            pass

    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    (root / "page-data" / "index" / "page-data.json").write_text(json.dumps({"staticQueryHashes": ["abc123"]}))
    (root / "page-data" / "sq" / "d" / "abc123.json").write_text(json.dumps({
        "data": {"allTocsJson": {"edges": [
            {"node": {"url": f"{base}/rates.json.gz", "is_suppressed": False}},
            {"node": {"url": f"{base}/pulled.json.gz", "is_suppressed": True}},
        ]}}}))
    try:
        add_urls(store, [base])
        drain(cfg, store)
        recs = {dedup_key(r["url"]): r for r in store.list_urls()}
        hub = recs[dedup_key(base)]
        assert hub["status"] == "done" and hub["kind"] == "page"
        assert hub["child_count"] == 1  # suppressed entry NOT queued
        child = recs[dedup_key(f"{base}/rates.json.gz")]
        assert child["status"] == "done" and child["rows_emitted"] > 0
        assert dedup_key(f"{base}/pulled.json.gz") not in recs
    finally:
        httpd.shutdown()


def test_blobs_listing_api_expands_rate_files_first(cfg, store, server, http_root):
    # UHC/Optum-style portal API: {"blobs": [{name, downloadUrl}, ...]}.
    # Rate files queue first; allowed-amounts and drug-pricing (NDC /
    # prescription-drugs) entries are not queued at all.
    (http_root / "blobs.json").write_text(json.dumps({"blobs": [
        {"name": "2026-07-01_x_allowed-amounts.json.gz", "downloadUrl": f"{server}/nope.json.gz"},
        {"name": "2026-07-01_p_PPO-NDC_in-network-rates.json.gz", "downloadUrl": f"{server}/drugs.json.gz"},
        {"name": "2026-07-01_q_prescription-drugs_in-network.json.gz", "downloadUrl": f"{server}/drugs2.json.gz"},
        {"name": "2026-07-01_employer_index.json", "downloadUrl": f"{server}/companion.json"},
        {"name": "2026-07-01_net_in-network-rates.json.gz", "downloadUrl": f"{server}/rates.json.gz"},
    ]}))
    add_urls(store, [f"{server}/blobs.json"])
    drain(cfg, store)
    recs = {dedup_key(r["url"]): r for r in store.list_urls()}
    listing = recs[dedup_key(f"{server}/blobs.json")]
    assert listing["status"] == "done" and listing["kind"] == "toc"
    assert listing["child_count"] == 2  # allowed-amounts entry never queued
    assert recs[dedup_key(f"{server}/rates.json.gz")]["status"] == "done"
    assert dedup_key(f"{server}/nope.json.gz") not in recs
    assert dedup_key(f"{server}/drugs.json.gz") not in recs    # NDC file not queued
    assert dedup_key(f"{server}/drugs2.json.gz") not in recs   # prescription-drugs not queued
    # in-network child was queued (lower id) before the index child
    assert recs[dedup_key(f"{server}/rates.json.gz")]["id"] < recs[dedup_key(f"{server}/companion.json")]["id"]


def test_react_portal_root_found_via_blobs_probe(cfg, store, http_root):
    # a React portal page: no links in HTML, but /api/v1/uhc/blobs/ exists
    root = http_root / "portal2"
    (root / "api" / "v1" / "uhc" / "blobs").mkdir(parents=True)
    (root / "index.html").write_text("<!doctype html><html><body><div id=root></div></body></html>")
    (root / "rates.json.gz").write_bytes(gzip.compress((FIXTURES / "innetwork_mixed.json").read_bytes()))

    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **k):
            super().__init__(*a, directory=str(root), **k)

        def log_message(self, *a):
            pass

    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    (root / "api" / "v1" / "uhc" / "blobs" / "index.html").write_text(json.dumps({"blobs": [
        {"name": "n_in-network-rates.json.gz", "downloadUrl": f"{base}/rates.json.gz"},
    ]}))
    try:
        add_urls(store, [base])
        drain(cfg, store)
        recs = {dedup_key(r["url"]): r for r in store.list_urls()}
        portal = recs[dedup_key(base)]
        assert portal["status"] == "done" and portal["kind"] == "page"
        assert recs[dedup_key(f"{base}/rates.json.gz")]["status"] == "done"
        assert recs[dedup_key(f"{base}/rates.json.gz")]["rows_emitted"] > 0
    finally:
        httpd.shutdown()


def test_parallel_ingest_matches_serial(cfg, store, server, http_root, tmp_path):
    # parallel_ingests=2: two parser processes, DB writes stay in this process.
    # Outputs must match the serial path exactly.
    import gzip as _gzip
    import json as _json

    doc = _json.loads((FIXTURES / "innetwork_mixed.json").read_text())
    doc["reporting_entity_name"] = "Second Payer LLC"
    (http_root / "rates_b.json.gz").write_bytes(_gzip.compress(_json.dumps(doc).encode()))
    cfg.parallel_ingests = 2
    add_urls(store, [f"{server}/rates.json.gz", f"{server}/rates_b.json.gz",
                     f"{server}/companion.json"])
    drain(cfg, store)
    recs = {dedup_key(r["url"]): r for r in store.list_urls()}
    a = recs[dedup_key(f"{server}/rates.json.gz")]
    b = recs[dedup_key(f"{server}/rates_b.json.gz")]
    assert a["status"] == "done" and b["status"] == "done"
    assert a["rows_emitted"] == b["rows_emitted"] > 0  # same fixture -> same rows
    with store.connect() as con:
        total = con.execute("SELECT count(*) FROM rates").fetchone()[0]
        payers = {r[0] for r in con.execute("SELECT DISTINCT payer FROM rates").fetchall()}
        dedup = con.execute("SELECT count(*) FROM rates_dedup").fetchone()[0]
    assert total == a["rows_emitted"] + b["rows_emitted"]
    assert "Second Payer LLC" in payers and dedup > 0
    assert not any(f.name.endswith(".progress") for f in cfg.downloads_dir.iterdir())


def test_rollups_rebuilt_once_queue_drains(cfg, store, server):
    # queue ingests defer the (expensive, full) rollup rebuild; it must still
    # run when the queue goes idle so dashboards see the new data
    add_urls(store, [f"{server}/rates.json.gz", f"{server}/companion.json"])
    drain(cfg, store)
    with store.connect() as con:
        raw = con.execute("SELECT count(*) FROM rates").fetchone()[0]
        dedup = con.execute("SELECT count(*) FROM rates_dedup").fetchone()[0]
    assert raw > 0 and dedup > 0  # rollup view populated after the drain


def test_dedup_key_keeps_identity_params_drops_signature_params():
    # signed CDN re-pastes collapse to one row...
    a = dedup_key("https://x.mrf.bcbs.com/f.json.gz?&Expires=1&Signature=abc&Key-Pair-Id=K1")
    b = dedup_key("https://x.mrf.bcbs.com/f.json.gz?&Expires=2&Signature=xyz&Key-Pair-Id=K2")
    assert a == b
    # ...but download endpoints serving DIFFERENT files via query stay distinct
    assert dedup_key("https://p.com/dl?file=a.json") != dedup_key("https://p.com/dl?file=b.json")
    # Azure SAS params are volatile too
    assert dedup_key("https://x.blob.core.windows.net/c/f.json?sv=1&se=2&sp=r&sig=q") == \
           dedup_key("https://x.blob.core.windows.net/c/f.json?sv=9&se=8&sp=r&sig=z")


def test_urls_api_never_splits_on_commas(cfg, store):
    from fastapi.testclient import TestClient
    from mrfx.api import create_app

    client = TestClient(create_app(cfg, store))
    url = "https://p.com/mrf/2026-07-01_Payer,-Inc._in-network.json.gz?ids=1,2"
    r = client.post("/api/urls", json={"urls": url + "\n"}).json()
    assert r == {"added": 1, "skipped": 0, "invalid": 0}
    (rec,) = store.list_urls()
    assert rec["url"] == url  # comma intact, one row


def test_retry_failed_is_bulk_and_unbounded(cfg, store, server):
    # failures beyond any display window must still be retried
    for i in range(4):
        add_urls(store, [f"{server}/missing_{i}.json.gz"])
    drain(cfg, store)
    assert store.url_queue_counts().get("failed") == 4
    assert store.requeue_failed() == 4
    assert store.url_queue_counts().get("queued") == 4


def test_user_transition_guards(cfg, store, server):
    add_urls(store, [f"{server}/rates.json.gz"])
    drain(cfg, store)
    (rec,) = store.list_urls()
    assert rec["status"] == "done"
    # done rows anchor duplicate detection — user actions must not touch them
    assert store.set_url_status_by_id(rec["id"], "skipped") is False
    assert store.set_url_status_by_id(rec["id"], "queued") is False
    add_urls(store, [f"{server}/missing.json.gz"])
    drain(cfg, store)
    failed = next(r for r in store.list_urls() if "missing" in r["url"])
    assert store.set_url_status_by_id(failed["id"], "queued") is True  # retry a failure


def test_reexpanding_toc_does_not_undo_user_skip(cfg, store, server):
    add_urls(store, [f"{server}/toc.json"])
    drain(cfg, store)
    child = next(r for r in store.list_urls() if dedup_key(r["url"]) == dedup_key(f"{server}/rates.json.gz"))
    # user skips the (done) child? not allowed; simulate skipping a queued one:
    # force it back to skipped state directly to model an explicit user cancel
    with store.write_lock, store.connect() as con:
        con.execute("UPDATE url_queue SET status='skipped' WHERE id = ?", [child["id"]])
    # re-adding the TOC re-queues the TOC row itself but must NOT revive the
    # skipped child during expansion
    with store.write_lock, store.connect() as con:
        con.execute("UPDATE url_queue SET status='failed' WHERE dedup_key = ?",
                    [dedup_key(f"{server}/toc.json")])
    add_urls(store, [f"{server}/toc.json"])
    drain(cfg, store)
    child_after = next(r for r in store.list_urls() if r["id"] == child["id"])
    assert child_after["status"] == "skipped"
    # but pasting the child's URL DIRECTLY revives it
    add_urls(store, [f"{server}/rates.json.gz"])
    child_after = next(r for r in store.list_urls() if r["id"] == child["id"])
    assert child_after["status"] == "queued"


def test_add_urls_expands_month_placeholder(cfg, store):
    import datetime as dt

    add_urls(store, ["https://x.com/{FIRST_OF_MONTH}_payer_index.json"])
    (rec,) = store.list_urls()
    first = dt.date.today().replace(day=1).isoformat()
    assert rec["url"] == f"https://x.com/{first}_payer_index.json"


def test_filename_for_is_safe_and_distinct():
    a = filename_for("https://x.com/payerA/2026-06_in-network-rates_1_of_2.json.gz?sig=1")
    b = filename_for("https://x.com/payerB/2026-06_in-network-rates_1_of_2.json.gz")
    assert a != b                      # same basename, different path -> distinct
    assert a.endswith(".json.gz")
    c = filename_for("https://x.com/app/public/")
    assert c.endswith(".json")         # page-ish URL still gets a usable name


def test_store_connect_retries_transient_lock(cfg, store, monkeypatch):
    # a user opening the .duckdb file read-only (CLI/BI tool) can hold the
    # file lock for an instant; connect() must ride it out, not crash a run
    import duckdb as _duckdb

    real_connect = _duckdb.connect
    fails = {"n": 2}

    def flaky(path, *a, **k):
        if isinstance(path, str) and path.endswith("mrfx.duckdb") and fails["n"] > 0:
            fails["n"] -= 1
            raise _duckdb.IOException("IO Error: Could not set lock on file (simulated)")
        return real_connect(path, *a, **k)

    monkeypatch.setattr("mrfx.store.duckdb.connect", flaky)
    assert store.url_queue_counts() is not None  # survives two failed attempts
    assert fails["n"] == 0


def test_download_resumes_partial_file(cfg, tmp_path):
    # a .part left by a crash/network drop resumes via HTTP Range instead of
    # re-downloading; the final hash must equal the full file's
    import hashlib
    import http.server as hs

    payload = gzip.compress((FIXTURES / "innetwork_mixed.json").read_bytes()) * 3
    served_ranges = []

    class RangeHandler(hs.BaseHTTPRequestHandler):
        def do_GET(self):
            rng = self.headers.get("Range")
            if rng:
                start = int(rng.split("=")[1].rstrip("-"))
                served_ranges.append(start)
                body = payload[start:]
                self.send_response(206)
                self.send_header("Content-Range", f"bytes {start}-{len(payload)-1}/{len(payload)}")
            else:
                body = payload
                self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), RangeHandler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        from mrfx.fetch import download, filename_for

        url = f"http://127.0.0.1:{httpd.server_address[1]}/f.json.gz"
        dest = cfg.downloads_dir / filename_for(url)
        cut = len(payload) // 3
        Path(str(dest) + ".part").write_bytes(payload[:cut])  # simulate the crash
        sha, _ = download(cfg, url, dest)
        assert served_ranges == [cut]                          # resumed, not restarted
        assert sha == hashlib.sha256(payload).hexdigest()      # bytes identical
        assert dest.read_bytes() == payload
    finally:
        httpd.shutdown()


def test_download_disk_space_guard(cfg, server, monkeypatch):
    import collections
    import shutil as _shutil

    from mrfx.fetch import DownloadError, download, filename_for

    fake = collections.namedtuple("usage", "total used free")(100 << 30, 100 << 30, 1 << 30)
    monkeypatch.setattr("mrfx.fetch.shutil.disk_usage", lambda p: fake)  # 1GB free
    url = f"{server}/rates.json.gz"
    dest = cfg.downloads_dir / filename_for(url)
    with pytest.raises(DownloadError) as ei:
        download(cfg, url, dest)
    assert "disk space" in str(ei.value)
