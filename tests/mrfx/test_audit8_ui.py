"""Audit 8, dashboard / API / docs: a same-name file is never silently
ignored, the Files poll is bounded, bad input is a 422 not a 500, the
download queue says "waiting", exports name themselves safely, the
starter list is the catalog."""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from mrfx.api import _safe_view, create_app
from mrfx.ingest import scan_inbox

HDR = {"reporting_entity_name": "Acme Health", "version": "1.0", "last_updated_on": "2026-05-01"}


def doc(rate):
    return {**HDR, "in_network": [{
        "negotiation_arrangement": "ffs", "billing_code_type": "CPT", "billing_code": "97110",
        "negotiated_rates": [{"provider_groups": [{"npi": ["1234567893"],
                                                   "tin": {"type": "ein", "value": "123456789"}}],
                              "negotiated_prices": [{"negotiated_type": "negotiated",
                                                     "negotiated_rate": rate,
                                                     "billing_class": "professional"}]}]}]}


def q(store, sql, *a):
    with store.connect() as con:
        return con.execute(sql, list(a)).fetchall()


@pytest.mark.parametrize("move", [True, False])
def test_inbox_file_under_a_loaded_name(cfg, store, move):
    cfg.move_processed = move
    (cfg.inbox_dir / "acme_2026-05.json").write_text(json.dumps(doc(40.0)))
    scan_inbox(cfg, store)
    # the SAME file again: left alone, nothing re-loaded
    if move:
        (cfg.inbox_dir / "acme_2026-05.json").write_text(json.dumps(doc(40.0)))
    assert scan_inbox(cfg, store) == []
    assert q(store, "SELECT count(*) FROM files")[0][0] == 1
    # a DIFFERENT file under that name, same payer and month: held for the
    # user's choice (review 9 4.5 — it used to be loaded beside the old one
    # and every number became the median of both)
    (cfg.inbox_dir / "acme_2026-05.json").write_text(json.dumps(doc(55.0)))
    res = scan_inbox(cfg, store)
    assert [r["status"] for r in res] == ["needs_choice"]
    assert q(store, "SELECT count(*) FROM files WHERE status = 'needs_choice'") == [(1,)]
    assert sorted(r[0] for r in q(store, "SELECT negotiated_rate FROM rates")) == [40.0]
    # another MONTH under that name: loaded beside it, under a content name
    june = {**doc(60.0), "last_updated_on": "2026-06-01"}
    (cfg.inbox_dir / "acme_2026-05.json").write_text(json.dumps(june))
    res = scan_inbox(cfg, store)
    assert len(res) == 1 and res[0]["status"] == "done"
    names = {r[0] for r in q(store, "SELECT filename FROM files WHERE status = 'done'")}
    assert "acme_2026-05.json" in names and len(names) == 2
    new = (names - {"acme_2026-05.json"}).pop()
    assert new.startswith("acme_2026-05-") and new.endswith(".json")
    assert sorted(r[0] for r in q(store, "SELECT negotiated_rate FROM rates")) == [40.0, 60.0]


def test_upload_under_a_loaded_name(cfg, store):
    c = TestClient(create_app(cfg, store))
    body = json.dumps(doc(40.0)).encode()
    r = c.post("/api/upload", files={"file": ("acme_2026-05.json", body)}).json()
    assert r["status"] == "queued" and r["filename"] == "acme_2026-05.json"
    import time
    deadline = time.monotonic() + 20      # the upload's own background scan loads it
    while (store.file_status("acme_2026-05.json") or {}).get("status") != "done":
        assert time.monotonic() < deadline, store.file_status("acme_2026-05.json")
        time.sleep(0.1)
    same = c.post("/api/upload", files={"file": ("acme_2026-05.json", body)}).json()
    assert same["status"] == "duplicate" and "already loaded" in same["note"]
    other = c.post("/api/upload", files={"file": ("acme_2026-05.json",
                                                  json.dumps(doc(60.0)).encode())}).json()
    # same payer and month, different bytes: held for Replace / Keep both
    assert other["status"] == "needs_choice" and other["replaces"] == "acme_2026-05.json"
    assert (cfg.inbox_dir / ".held" / other["filename"]).exists()
    r = c.post(f"/api/files/{other['filename']}/choose", json={"action": "replace"}).json()
    assert r["action"] == "replace"
    deadline = time.monotonic() + 20
    while q(store, "SELECT negotiated_rate FROM rates") != [(60.0,)] or \
            (store.file_status("acme_2026-05.json") or {}).get("status") != "done":
        assert time.monotonic() < deadline, q(store, "SELECT negotiated_rate FROM rates")
        time.sleep(0.1)
    june = c.post("/api/upload", files={"file": ("acme_2026-05.json", json.dumps(
        {**doc(65.0), "last_updated_on": "2026-06-01"}).encode())}).json()
    assert june["status"] == "queued" and june["renamed_from"] == "acme_2026-05.json"
    assert june["filename"] != "acme_2026-05.json" and june["filename"].endswith(".json")


def test_files_poll_is_paged_and_light(cfg, store):
    for i in range(7):
        store.upsert_file(f"f{i}.json", status="done", preflight={"big": "x" * 1000})
    c = TestClient(create_app(cfg, store))
    d = c.get("/api/files?limit=3").json()
    assert d["total"] == 7 and len(d["files"]) == 3
    assert all("preflight" not in f for f in d["files"])
    assert len(c.get("/api/files?limit=3&offset=6").json()["files"]) == 1
    assert c.get("/api/files?limit=999999").json()["limit"] == 5000


def test_bad_input_is_refused_in_words(cfg, store):
    c = TestClient(create_app(cfg, store))
    r = c.post("/api/urls", json={"urls": 5})
    assert r.status_code == 422 and "list of links" in r.json()["detail"]
    assert c.post("/api/urls", json={"urls": ["http://", "https:///x"]}).json()["invalid"] == 2
    assert c.get("/api/rates", params={"page": 10 ** 30}).status_code == 422


def test_waiting_copies_are_counted_on_their_own(cfg, store):
    a = store.enqueue_url("https://x.example/a.json.gz", "a")
    b = store.enqueue_url("https://y.example/a.json.gz", "b")
    store.enqueue_url("https://z.example/c.json.gz", "c")
    store.update_url(a, status="downloading")
    store.park_waiting(b, a, 10, "waiting: an identical-looking copy is already downloading")
    counts = store.url_queue_counts()
    assert counts.get("waiting") == 1 and "paused" not in counts
    d = TestClient(create_app(cfg, store)).get("/api/urls").json()
    s = d["settings"]
    assert s["early_dedup_mb"] == cfg.early_dedup_mb and s["downloads"] >= 1


def test_export_view_name_is_sanitised():
    assert _safe_view('x"\r\nSet-Cookie: a=b') == "xSet-Cookieab"
    assert _safe_view("") == "explorer" and _safe_view("leads_2") == "leads_2"
    assert len(_safe_view("a" * 500)) == 40


def test_starter_links_is_the_catalog():
    from mrfx.known_sources import starter_links_text
    root = Path(__file__).resolve().parents[2]
    assert (root / "config" / "starter_links.txt").read_text(encoding="utf-8") == \
        starter_links_text(root / "config" / "known_sources.yaml")
