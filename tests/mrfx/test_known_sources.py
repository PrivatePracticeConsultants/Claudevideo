"""Known-sources catalog: the shipped file is valid, placeholders resolve,
and the catalog wires into the queue via API and add_urls."""

import datetime as dt
from pathlib import Path

from fastapi.testclient import TestClient

from mrfx.api import create_app
from mrfx.fetch import add_urls
from mrfx.known_sources import expand_placeholders, load_known_sources

REPO_CATALOG = Path(__file__).resolve().parents[2] / "config" / "known_sources.yaml"


def test_shipped_catalog_is_valid():
    sources = load_known_sources(REPO_CATALOG)
    assert len(sources) >= 20
    queueable = [s for s in sources if s["queueable"]]
    assert len(queueable) >= 15  # the tested payer indexes
    for s in sources:
        assert s["name"] and s["url"].startswith(("http://", "https://"))
        assert "{" not in s["url"]  # placeholders resolved
        assert s["verified"]       # honesty contract: every entry carries a date
    # entries that need a human click are marked, not silently queueable
    assert any(not s["queueable"] for s in sources)


def test_placeholder_resolves_to_current_month():
    url = "https://x.com/{FIRST_OF_MONTH}_payer_index.json"
    assert expand_placeholders(url, dt.date(2026, 7, 8)) == "https://x.com/2026-07-01_payer_index.json"
    assert expand_placeholders(url, dt.date(2026, 12, 31)) == "https://x.com/2026-12-01_payer_index.json"


def test_missing_catalog_degrades_to_empty(tmp_path):
    assert load_known_sources(tmp_path / "nope.yaml") == []


def test_non_utf8_yaml_never_crashes(tmp_path):
    # a Windows-ANSI byte (Notepad in a cp1252 locale) used to raise
    # UnicodeDecodeError out of both loaders — a 500 on the known-sources API
    # and a dead `mrfx serve` at startup for the registry (invariant #3: a
    # user-editable file must never kill the program)
    bad = tmp_path / "bad.yaml"
    bad.write_bytes("sources:\n  - name: caf\xe9\n".encode("cp1252"))
    assert isinstance(load_known_sources(bad), list)   # degrades, no raise

    from mrfx.registry import _load_yaml
    assert isinstance(_load_yaml(bad), dict)           # degrades, no raise
    # a directory at the path (IsADirectoryError/OSError) degrades too
    assert _load_yaml(tmp_path) == {}


def test_known_sources_enqueue(cfg, store):
    sources = load_known_sources(REPO_CATALOG)
    counts = add_urls(store, [s["url"] for s in sources if s["queueable"]])
    assert counts["added"] == sum(1 for s in sources if s["queueable"])
    assert counts["invalid"] == 0


def test_known_sources_api(cfg, store):
    client = TestClient(create_app(cfg, store))
    d = client.get("/api/known-sources").json()
    assert len(d["sources"]) >= 20
    r = client.post("/api/urls/known").json()
    assert r["added"] >= 15 and r["portals"] >= 5
    # queued rows visible in the queue listing, pinned as top-level
    q = client.get("/api/urls").json()
    assert q["counts"].get("queued", 0) == r["added"]
