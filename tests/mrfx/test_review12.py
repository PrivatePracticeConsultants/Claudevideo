"""Review 12: the 0.7.5 zip audited end to end (docs/NEXT_STEPS_PROMPT_12.md).
Every finding was reproduced on 68d9bc7 (v0.7.5) by a script under
docs/review12_repros/; each test below pins one item, and its docstring says
what it got on 68d9bc7 ("was: ...")."""

from __future__ import annotations

import hashlib
import json
import threading
import time

import pytest
from fastapi.testclient import TestClient

from mrfx import fetch
from mrfx.api import create_app
from mrfx.ingest import scan_inbox
from mrfx.store import Store
from tests.mrfx.test_review11 import Srv, drain, gz, q, ranged, rate_doc


@pytest.fixture
def srv():
    servers = []

    def make(routes):
        s = Srv(routes)
        servers.append(s)
        return s
    yield make
    for s in servers:
        s.close()


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _doc(payer, day, items, tin="123456789", npi="1234567893"):
    return {"reporting_entity_name": payer, "reporting_entity_type": "issuer",
            "last_updated_on": day, "version": "2.0.0", "provider_references": [],
            "in_network": [{
                "negotiation_arrangement": "ffs", "name": code, "billing_code_type": "CPT",
                "billing_code_type_version": "2026", "billing_code": code, "description": code,
                "negotiated_rates": [{"provider_groups": [{"npi": [npi], "tin": {"type": "ein", "value": tin}}],
                                      "negotiated_prices": [{
                                          "negotiated_type": "negotiated", "negotiated_rate": rate,
                                          "billing_class": "professional", "service_code": ["11"],
                                          "billing_code_modifier": mods,
                                          "expiration_date": "9999-12-31"}]}]}
                for code, rate, mods in items]}


# ------------------------------------------- Group 1: wrong numbers, stuck --

def test_npi_drawer_current_rate_uses_the_contract_window(cfg, store):
    """1.1 (agent_2_5/npi_drawer_latest.py): May bare $40, June GP $42 — one
    contract whose base-modifier spelling changed. Every surface prints the
    current rate $42. was: NPI drawer chart median_rate 41.0 (TIN drawer,
    Explorer and Code page 42.0)."""
    tin, npi = "123456789", "1234567893"
    (cfg.inbox_dir / "p_2026-05_in-network.json").write_text(
        json.dumps(_doc("Payer P", "2026-05-01", [("97110", 40.0, [])])))
    (cfg.inbox_dir / "p_2026-06_in-network.json").write_text(
        json.dumps(_doc("Payer P", "2026-06-01", [("97110", 42.0, ["GP"])])))
    assert all(r["status"] == "done" for r in scan_inbox(cfg, store))
    store.rebuild_rollups()
    c = TestClient(create_app(cfg, store), base_url="http://localhost")
    npi_chart = c.get(f"/api/entity/npi/{npi}").json()["chart"]
    tin_chart = c.get(f"/api/entity/tin/{tin}").json()["chart"]
    ex_npi = c.get("/api/rates", params={"grain": "npi", "latest": "1", "q": npi}).json()["rows"]
    ex_tin = c.get("/api/rates", params={"grain": "tin", "latest": "1", "q": tin}).json()["rows"]
    code = c.get("/api/code/97110", params={"grain": "npi", "payer": "Payer P"}).json()
    got = {"npi drawer": [r["median_rate"] for r in npi_chart],
           "tin drawer": [r["median_rate"] for r in tin_chart],
           "explorer npi": [r["negotiated_rate"] for r in ex_npi],
           "explorer tin": [r["negotiated_rate"] for r in ex_tin],
           "code page": [r["median_rate"] for r in code["payer_rank"]]}
    assert got == {k: [42.0] for k in got}, got


def _unquoted_etag_server(body, cut_first=True, rotate=False):
    """IIS-style unquoted ETag beside a Last-Modified; the first connection
    (or every one) is cut after 300 KB."""
    state = {"n": 0}

    def route(req):
        state["n"] += 1
        etag = f"5d8c72a5edda8d6a:{state['n'] if rotate else 0}"
        h = {"ETag": etag, "Last-Modified": "Tue, 03 Nov 2026 10:00:00 GMT",
             "Accept-Ranges": "bytes"}
        cut = (300 << 10) if (rotate or (cut_first and state["n"] == 1)) else None
        rng = req.headers.get("Range")
        if rng:
            a = int(rng.split("=", 1)[1].split("-")[0])
            h["Content-Range"] = f"bytes {a}-{len(body) - 1}/{len(body)}"
            return 206, h, body[a:], cut
        return 200, h, body, cut
    return route


def test_unquoted_etag_resume_is_accepted(cfg, srv):
    """1.2 (agent_diff/s1_unquoted_etag.py): an unquoted ETag + Last-Modified,
    the first connection cut at 300 KB. One resume finishes the file (0.7.4's
    count). was: the 206 was compared with Last-Modified, rejected as "the
    server's file changed", the .part deleted and the file fetched again from
    byte 0 — [200 cut, 206 rejected, 200] (the repro, every connection cut:
    20 requests, 5.9 MB served, FAILED)."""
    import os
    body = os.urandom(1_300_000)
    s = srv({"/f.bin": _unquoted_etag_server(body)})
    cfg.download_retries = 3
    dest = cfg.downloads_dir / "f.bin"
    sha, _ = fetch.download(cfg, s.base + "/f.bin", dest)
    assert sha == _sha(body)
    assert [(e[1], e[2] is not None) for e in s.hits("/f.bin")] == [(200, False), (206, True)]


def test_set_aside_sentence_says_what_is_kept(cfg, srv):
    """1.2, second half: a file that really changes on every connection (a new
    ETag each time) and is cut each time, set aside by the time cap. The
    sentence must not promise "nothing is re-downloaded" over a deleted
    .part. was: "paused after 6 s and 0 MB downloaded … resumes from here
    automatically, nothing is re-downloaded"."""
    import os
    body = os.urandom(900_000)
    s = srv({"/g.bin": _unquoted_etag_server(body, rotate=True)})
    cfg.download_retries = 3
    cfg.download_max_seconds = 6
    cfg.download_park_seconds = 0
    with pytest.raises(fetch.DownloadError) as ei:
        fetch.download(cfg, s.base + "/g.bin", cfg.downloads_dir / "g.bin")
    msg = str(ei.value)
    assert ei.value.set_aside
    assert "nothing is re-downloaded" not in msg, msg
    assert "start over" in msg, msg


def test_cli_ingest_does_not_wait_for_an_orphaned_link_load(cfg, store, monkeypatch):
    """1.3 (agent_diff/s2_cli_ingest_wait.py): a link row a killed server left
    'ingesting' with the same bytes' sha; the user drops those bytes in the
    inbox and runs `mrfx ingest`. was: "the same bytes are being loaded right
    now … waiting" — the scan never returned (20 s join), the inbox file
    stuck 'processing', the url row 'ingesting'."""
    from types import SimpleNamespace

    from mrfx import cli
    body = gz(rate_doc(n=30))
    fetch.add_urls(store, ["http://127.0.0.1:9/acme_2026-06.json.gz"])
    store.update_url(1, status="ingesting", content_sha=_sha(body), kind="in_network",
                     filename="x_acme_2026-06.json.gz")
    (cfg.inbox_dir / "acme_2026-06 (1).json.gz").write_bytes(body)
    # the orphan rule alone (no recovery ran): a fresh Store, as a new process
    s2 = Store(cfg.store_dir)
    out: dict = {}
    th = threading.Thread(target=lambda: out.setdefault("r", scan_inbox(cfg, s2)), daemon=True)
    th.start()
    th.join(20)
    assert not th.is_alive(), "the inbox scan waited on a load no process runs"
    assert [r["status"] for r in out["r"]] == ["done"]
    # and `mrfx ingest` puts the orphaned row back in the queue
    store.update_url(1, status="ingesting")
    monkeypatch.setattr(cli, "_something_owns_the_port", lambda *a, **k: False)
    cli.cmd_ingest(cfg, SimpleNamespace(path=None, force=False))
    assert q(store, "SELECT status FROM url_queue") == [("queued",)]


def _signed_index_server(srv, child_bytes):
    """An index whose child link carries a per-visit signature; the server
    refuses any signature but the newest (Azure SAS / CloudFront shape)."""
    st = {"sig": 0, "body": child_bytes, "etag": '"v1"'}
    s = srv({})

    def index(req):
        st["sig"] += 1
        doc = {"reporting_entity_name": "Twin Health", "reporting_structure": [
            {"reporting_plans": [{"plan_name": "P"}],
             "in_network_files": [{"location": f"{s.base}/child.json.gz?sig=S{st['sig']}"}]}]}
        return 200, {}, json.dumps(doc).encode(), None

    def child(req):
        if not req.path.endswith(f"sig=S{st['sig']}"):
            return 403, {}, b"expired", None
        return ranged(st["body"], req, etag=st["etag"])
    s.routes["/index.json"] = index
    for i in range(1, 6):
        s.routes[f"/child.json.gz?sig=S{i}"] = child
    return s, st


def test_index_refresh_probes_the_childs_current_signed_link(cfg, store, srv):
    """1.4 (agent_diff/s4_refresh_old_url.py A): the child is republished and
    the index hands out a fresh signature. was: the re-check probed the stored
    ?sig=S1 (403), nothing was re-queued, the store stayed 2026-05 / 60 rows."""
    s, st = _signed_index_server(srv, gz(rate_doc(month="2026-05-01")))
    fetch.add_urls(store, [s.base + "/index.json"])
    drain(cfg, store)
    assert q(store, "SELECT DISTINCT file_month FROM rates") == [("2026-05",)]
    st.update(body=gz(rate_doc(rate=45.0, n=80, month="2026-06-01")), etag='"v2"')
    store.requeue_urls([s.base + "/index.json"])
    drain(cfg, store)
    assert q(store, "SELECT DISTINCT file_month FROM rates") == [("2026-06",)]
    assert q(store, "SELECT count(*) FROM rates")[0][0] == 80
    # the stored link is the one the index handed over last
    assert q(store, "SELECT url FROM url_queue WHERE kind = 'in_network'")[0][0].endswith(
        f"sig=S{st['sig']}")


def test_clear_queue_during_the_children_recheck_sticks(cfg, store, srv, monkeypatch):
    """1.5 (agent_diff/s7_clear_during_recheck.py): "Clear queue" pressed while
    the re-expanded index asks about its loaded children. was: right after the
    clear the index was skipped/canceled; 12 s later 'done' and every
    republished child downloaded and loaded again (file months 2026-06)."""
    s = srv({})
    bodies = {i: gz(rate_doc(n=3, payer=f"P{i}", month="2026-05-01")) for i in range(3)}
    etag = {"v": '"v1"'}
    for i in range(3):
        s.routes[f"/c{i}.json.gz"] = lambda req, i=i: ranged(bodies[i], req, etag=etag["v"])
    idx = json.dumps({"reporting_entity_name": "Twin Health", "reporting_structure": [
        {"reporting_plans": [{"plan_name": "P"}],
         "in_network_files": [{"location": f"{s.base}/c{i}.json.gz"} for i in range(3)]}]}).encode()
    s.routes["/idx.json"] = lambda req: ranged(idx, req)
    fetch.add_urls(store, [s.base + "/idx.json"])
    drain(cfg, store)
    for i in range(3):
        bodies[i] = gz(rate_doc(n=5, payer=f"P{i}", month="2026-06-01"))
    etag["v"] = '"v2"'
    real = fetch._server_now
    pressed = []
    once = threading.Lock()

    def asked(cfg_, url):
        with once:
            if not pressed:
                pressed.append(store.clear_queued())   # the user presses "Clear queue"
        return real(cfg_, url)
    monkeypatch.setattr(fetch, "_server_now", asked)
    store.requeue_urls([s.base + "/idx.json"])
    drain(cfg, store)
    assert pressed == [1]
    assert q(store, "SELECT status, kind FROM url_queue WHERE id = 1") == [("skipped", "canceled")]
    assert q(store, "SELECT DISTINCT file_month FROM rates") == [("2026-05",)]
    assert q(store, "SELECT count(*) FROM url_queue WHERE status = 'done' AND kind = 'in_network'")[0][0] == 3


# ------------------------------------- Group 2: waste, misleading states --

def test_non_finite_medicare_rate_is_refused_and_a_stored_one_is_no_anchor(cfg, store):
    """2.1 (agent_6_8/mpfs_probe.py): `97110,00,nan` uploaded. was: 200
    {"rows":1}, then /api/summary 422 "cannot convert float NaN to integer"
    and /api/benchmark/market 422 "Out of range float values are not JSON
    compliant: nan"."""
    (cfg.inbox_dir / "p_2026-05.json").write_text(
        json.dumps(_doc("Payer P", "2026-05-01", [("97110", 40.0, ["GP"])])))
    scan_inbox(cfg, store)
    c = TestClient(create_app(cfg, store), base_url="http://localhost")
    for bad in ("nan", "inf", "-5", "0"):
        r = c.post("/api/mpfs/upload", files={"file": ("m.csv", f"code,locality,non_facility_rate\n97110,00,{bad}\n".encode(), "text/csv")})
        assert r.status_code == 422 and "line 2" in r.json()["detail"], (bad, r.text)
    # an anchor 0.7.5 already stored: no anchor for that code, never a 422
    with store.write_lock, store.connect() as con:
        con.execute("INSERT INTO mpfs (code, locality, non_facility_rate, source, state) "
                    "VALUES ('97110', '00', 'NaN'::DOUBLE, 'old upload', NULL)")
    assert c.get("/api/summary").status_code == 200
    assert c.get("/api/code/97110").status_code == 200
    r = c.post("/api/benchmark/market", json={"subject": "", "market": {"month": "latest"}})
    assert r.status_code == 200, r.text


def test_a_link_deferred_to_a_crashed_inbox_load_is_revived_at_start(cfg, store):
    """2.2 (agent_diff/s5_crash_inbox_claim.py): inbox A loading, link B with
    the same bytes deferred to it, the process died, A never loaded. was:
    recover_stuck_urls revived 0; B stayed skipped "being loaded from the
    inbox right now — will retry automatically" forever."""
    body = gz(rate_doc(n=20))
    sha = _sha(body)
    store.upsert_file("A.json.gz", status="processing", payer="Twin Health", file_type="in_network")
    assert store.claim_inbox_content(sha, "A.json.gz") is None
    url = "http://127.0.0.1:9/B.json.gz"
    fetch.add_urls(store, [url])
    dest = cfg.downloads_dir / fetch.filename_for(url)
    dest.write_bytes(body)
    store.update_url(1, content_sha=sha)
    assert store.claim_content_ingest(sha, 1, "in_network", dest.name) == ("A.json.gz", "ingesting")
    s2 = Store(cfg.store_dir)          # the restart: the in-memory claim is gone
    s2.recover_stuck_files()
    s2.recover_stuck_urls()
    (st, err), = q(s2, "SELECT status, error FROM url_queue WHERE id = 1")
    assert st == "queued", err
    assert "did not finish" in err


def test_refresh_of_a_child_loaded_by_0_7_4_compares_the_loaded_size(cfg, store, srv):
    """2.3 (agent_diff/s4_refresh_old_url.py C): a child loaded with no
    server record (as 0.7.4 left it), the server unchanged. was: the monthly
    refresh did 206 + a full 200 and re-parsed the same 60 rows."""
    child = gz(rate_doc(month="2026-05-01"))
    s = srv({"/files/r.json.gz": lambda req: ranged(child, req, etag='"v1"')})
    idx = json.dumps({"reporting_entity_name": "Twin Health", "reporting_structure": [
        {"reporting_plans": [{"plan_name": "P"}],
         "in_network_files": [{"location": s.base + "/files/r.json.gz"}]}]}).encode()
    s.routes["/index.json"] = lambda req: ranged(idx, req)
    fetch.add_urls(store, [s.base + "/index.json"])
    drain(cfg, store)
    with store.write_lock, store.connect() as con:
        con.execute("UPDATE url_queue SET server_validator = NULL, server_size = NULL "
                    "WHERE kind = 'in_network'")
    before = len(s.hits("/files/r.json.gz"))
    store.requeue_urls([s.base + "/index.json"])
    from mrfx.store import Store as _S  # noqa: F401
    # the monthly refresh marks the index revalidate=1
    with store.write_lock, store.connect() as con:
        con.execute("UPDATE url_queue SET revalidate = 1 WHERE id = 1")
    drain(cfg, store)
    hits = s.hits("/files/r.json.gz")[before:]
    assert [(h[1], h[2]) for h in hits] == [(206, "bytes=0-0")], hits
    assert q(store, "SELECT count(*) FROM rates")[0][0] == 60


def test_repasting_a_fresh_link_updates_a_queued_row_but_not_a_downloading_one(cfg, store):
    """2.4: a signed backlog re-pasted with fresh signatures. was: the queued
    row kept its old (expired) link — _enqueue_one returned None for
    queued / paused rows."""
    fetch.add_urls(store, ["https://cdn.example.com/a.json.gz?sig=OLD",
                           "https://cdn.example.com/b.json.gz?sig=OLD"])
    store.update_url(2, status="downloading")
    fetch.add_urls(store, ["https://cdn.example.com/a.json.gz?sig=NEW",
                           "https://cdn.example.com/b.json.gz?sig=NEW"])
    assert q(store, "SELECT id, status, url FROM url_queue ORDER BY id") == [
        (1, "queued", "https://cdn.example.com/a.json.gz?sig=NEW"),
        (2, "downloading", "https://cdn.example.com/b.json.gz?sig=OLD")]
    store.update_url(1, status="paused")
    fetch.add_urls(store, ["https://cdn.example.com/a.json.gz?sig=NEWER"])
    assert q(store, "SELECT url FROM url_queue WHERE id = 1")[0][0].endswith("sig=NEWER")


def test_two_files_of_one_payer_month_are_pooled_and_said_so(cfg, store):
    """2.5: the rule is POOLING (measured on the real Tufts 2026-11 books:
    43,810 of 46,149 contract lines in both files, 1,350 at different rates —
    two plan books, not a correction), stated with its numbers on the Files
    tab and in every methodology. was: no word anywhere (the Files response
    had no pooled_months; the sidecar no "Pooled months")."""
    (cfg.inbox_dir / "p_book1_2026-05.json").write_text(json.dumps(
        _doc("Payer P", "2026-05-01", [("97110", 40.0, ["GP"]), ("97112", 30.0, ["GP"])])))
    (cfg.inbox_dir / "p_book2_2026-05.json").write_text(json.dumps(
        _doc("Payer P", "2026-05-01", [("97110", 44.0, ["GP"]), ("97112", 30.0, ["GP"])])))
    scan_inbox(cfg, store)
    store.rebuild_rollups()
    c = TestClient(create_app(cfg, store), base_url="http://localhost")
    d = c.get("/api/files").json()
    assert d["pooled_months"] == [{"payer": "Payer P", "file_month": "2026-05", "files": 2,
                                   "practices_in_several": 1, "lines_in_several": 2,
                                   "lines_differing": 1}]
    # the number itself: the median of the two books' rates
    rows = c.get("/api/rates", params={"grain": "tin", "latest": "1", "code": "97110"}).json()["rows"]
    assert [r["negotiated_rate"] for r in rows] == [42.0]
    from mrfx.api import FilterSet, methodology_text
    assert "Pooled months — Payer P 2026-05: 2 files" in methodology_text(
        cfg, store, "tin", FilterSet({}), "rate", "desc", "test")


def test_shipped_config_leaves_the_memory_budget_to_the_machine(cfg):
    """2.6: config/mrfx.yaml shipped `duckdb_memory_gb: 8` and lacked the keys
    the guides tell the user to set. was: duckdb_memory_gb set; max_toc_files,
    delete_raw_after_ingest, tracker_dir, tracker_auto_import,
    render_auto_install absent (not even commented)."""
    import re
    from pathlib import Path

    import yaml

    from mrfx.config import MrfxConfig
    from mrfx.store import auto_pool_gb
    root = Path(__file__).resolve().parents[2]
    text = (root / "config" / "mrfx.yaml").read_text()
    assert "duckdb_memory_gb" not in yaml.safe_load(text)
    fields = set(MrfxConfig.model_fields)
    told = set()
    for doc in ("GETTING_STARTED.md", "README.md", "HOW_TO_RUN.md"):
        t = (root / doc).read_text()
        told |= {f for f in fields if re.search(r"`" + re.escape(f) + r"(`|:| )", t)}
    missing = sorted(f for f in told
                     if not re.search(r"^\s*#?\s*" + re.escape(f) + r"\s*:", text, re.M))
    assert missing == [], missing
    # the automatic budget leaves the OS and two parser workers their room
    for gb in (8, 12, 16, 32, 64):
        pool = auto_pool_gb(gb * 10**9)
        assert 2 <= pool <= 12 and (pool == 2 or pool <= gb - 3 - 2 * 2), (gb, pool)


def _feed_cache(store, rows):
    import pyarrow as pa
    import pyarrow.parquet as pq

    from mrfx.enrich import _NPPES_CACHE_COLS
    from mrfx.nppes import cache_path
    pq.write_table(pa.table({c: pa.array([r.get(c) for r in rows], pa.string())
                             for c in _NPPES_CACHE_COLS}), cache_path(store))


def test_feeds_apply_the_radius_before_the_limit(cfg, store):
    """2.7: 60 newer events 226 miles away, 3 older ones at the origin; a
    10-row page within 25 miles. was: new clinics [] total 0 capped True
    (only the newest 50 nationwide were read, then clipped to the circle);
    closures the same."""
    import datetime as dt

    from mrfx import nppes

    def ago(n):
        return (dt.date.today() - dt.timedelta(days=n)).strftime("%m/%d/%Y")

    def row(npi, z, enum=None, deact=None):
        return {"npi": npi, "org_name": f"PT {npi}", "entity_type": "NPI-1",
                "taxonomy_code": "225100000X", "taxonomy_codes": "225100000X",
                "city": "Town", "state": "MO", "address": "1 Main", "zip": z,
                "phone": "8165550100", "enumeration_date": enum or "01/01/2015",
                "deactivation_date": deact, "reactivation_date": None}
    rows = [row(f"16{i:08d}", "63103", enum=ago(5), deact=ago(5)) for i in range(60)]
    rows += [row(f"17{i:08d}", "64105", enum=ago(100), deact=ago(100)) for i in range(3)]
    _feed_cache(store, rows)
    for fn, col in ((nppes.new_enumerations, "enumerated"), (nppes.closures, "deactivated")):
        d = fn(store, zip_code="64105", radius_miles=25, days=180, limit=10)
        assert (sorted(r["npi"] for r in d["rows"]), d["total"], d["capped"]) == (
            ["1700000000", "1700000001", "1700000002"], 3, False), (fn.__name__, d)


def test_a_skipped_inbox_twin_does_not_keep_its_bytes(cfg, store):
    """2.8: a byte-identical second download "x (1).json.gz" is skipped AND,
    since the original is kept in data/processed, deleted — said in the
    sentence. was: moved to data/processed beside the original (two copies)."""
    body = gz(rate_doc(n=20))
    (cfg.inbox_dir / "tw_2026-05.json.gz").write_bytes(body)
    scan_inbox(cfg, store)
    (cfg.inbox_dir / "tw_2026-05 (1).json.gz").write_bytes(body)
    res = scan_inbox(cfg, store)
    assert [r["status"] for r in res] == ["skipped"]
    assert sorted(p.name for p in cfg.processed_dir.iterdir()) == ["tw_2026-05.json.gz"]
    assert not (cfg.inbox_dir / "tw_2026-05 (1).json.gz").exists()
    assert "this copy was deleted" in q(store, "SELECT error FROM files WHERE status = 'skipped'")[0][0]


def test_the_stored_row_update_opens_the_dashboard_first(cfg, store, monkeypatch):
    """2.9: the one-time stored-row update gets a budget before the dashboard
    opens and finishes behind it. was: no budget — Store() ran every file's
    update before returning (no `migrate_budget_s`: TypeError), so a slow
    drive sat silent until the whole scan ended."""
    for i in range(3):
        (cfg.inbox_dir / f"m{i}_2026-05.json").write_text(json.dumps(
            _doc(f"Payer {i}", "2026-05-01", [("97110", 40.0 + i, ["GP"])], tin=f"12345678{i}")))
    scan_inbox(cfg, store)
    with store.write_lock, store.connect() as con:
        con.execute("UPDATE files SET row_rules = 1")
    store.close()
    t0 = time.monotonic()
    s2 = Store(cfg.store_dir, migrate_budget_s=0)
    assert time.monotonic() - t0 < 30
    assert s2.rows_update and s2.rows_update["pending"] == 3
    assert q(s2, "SELECT count(*) FROM files WHERE row_rules = 1")[0][0] == 3
    th = s2.finish_row_rules_in_background()
    th.join(60)
    from mrfx.store import ROW_RULES_VERSION
    assert q(s2, "SELECT DISTINCT row_rules FROM files") == [(ROW_RULES_VERSION,)]
    assert s2.rows_update is None
    # every CLI open (no budget) still finishes it before handing the store over
    with s2.write_lock, s2.connect() as con:
        con.execute("UPDATE files SET row_rules = 1")
    s2.close()
    s3 = Store(cfg.store_dir)
    assert q(s3, "SELECT DISTINCT row_rules FROM files") == [(ROW_RULES_VERSION,)]


# ------------------------- Group 3: visit value in this market (Rate card) --
#
# Hand-worked case. The `ortho` visit = 97110 + 97530 + 97140 + 97112, one
# unit each, so a practice's visit value = the sum of its four rates. Every
# practice below is paid ONE rate r on all four codes (value 4r), PT lines (GP),
# located in MO:
#
#   payer A: T1 20, T2 25, T3 30, T4 35, T5 40, T6 45 -> 80 100 120 140 160 180
#            n 6, mean 130, median 130, P25 105 (pos 1.25), P75 155 (pos 3.75)
#   payer B: T1 30, T2 30, T3 35, T4 40, T5 50 -> 120 120 140 160 200;
#            T6 is priced on 97110 / 97530 / 97112 only -> EXCLUDED (1 pair)
#            n 5, mean 148, median 140, P25 120, P75 160
#   payer C: T1 10, T2 10 -> 2 practices < 5: suppressed (no numbers, no count)
#   out of scope: T7 in MO priced by A on OT lines (GO) only; T8 in KS (PT).
#   all payers, each practice's median of its own payer values (C counts):
#            T1 med(80,120,40)=80  T2 med(100,120,40)=100  T3 med(120,140)=130
#            T4 150  T5 180  T6 180 (A only)
#            n 6, mean 820/6 = 136.67, median 140, P25 107.50, P75 172.50
#   subject T3: A 120 -> others 80 100 140 160 180: 2 below -> 2/5 = p40, Q2
#                  (105 <= 120 < 130), vs mean -$10.00 (-7.7%), vs median -$10.00 (-7.7%)
#               B 140 -> others 120 120 160 200: 2 below -> 2/4 = p50, Q3
#                  (140 <= 140 < 160), vs mean -$8.00 (-5.4%), vs median $0.00 (+0.0%)
#               all payers med(120,140) = 130 -> others 80 100 150 180 180: 2/5 = p40,
#                  Q2 (107.50 <= 130 < 140), vs mean -$6.67 (-4.9%), vs median -$10.00 (-7.1%)

VV_TINS = {f"T{i}": f"{730000000 + i}" for i in range(1, 9)}
VV_STATE = {**{f"T{i}": "MO" for i in range(1, 8)}, "T8": "KS"}
ORTHO = ("97110", "97530", "97140", "97112")
VV_A = dict(T1=20, T2=25, T3=30, T4=35, T5=40, T6=45, T8=60)
VV_B = dict(T1=30, T2=30, T3=35, T4=40, T5=50)
VV_C = dict(T1=10, T2=10)


def _vv_doc(payer, prices: dict, partial: dict | None = None, mods=("GP",)):
    items = []
    for code in ORTHO:
        groups = []
        for k, r in prices.items():
            if partial and code in partial.get(k, ()):
                continue
            groups.append({"provider_groups": [{"npi": [f"18{VV_TINS[k][1:]}"],
                                                "tin": {"type": "ein", "value": VV_TINS[k]}}],
                           "negotiated_prices": [{"negotiated_type": "negotiated",
                                                  "negotiated_rate": float(r),
                                                  "billing_class": "professional",
                                                  "service_code": ["11"],
                                                  "billing_code_modifier": list(mods),
                                                  "expiration_date": "9999-12-31"}]})
        if groups:
            items.append({"negotiation_arrangement": "ffs", "billing_code_type": "CPT",
                          "billing_code": code, "negotiated_rates": groups})
    return {"reporting_entity_name": payer, "last_updated_on": "2026-06-01",
            "version": "2.0.0", "in_network": items}


@pytest.fixture
def vv_app(cfg, store):
    docs = {"a_2026-06.json": _vv_doc("Payer A", VV_A),
            "a_ot_2026-06.json": _vv_doc("Payer A OT book", {"T7": 50}, mods=("GO",)),
            "b_2026-06.json": _vv_doc("Payer B", {**VV_B, "T6": 45},
                                      partial={"T6": ("97140",)}),
            "c_2026-06.json": _vv_doc("Payer C", VV_C)}
    for name, doc in docs.items():
        (cfg.inbox_dir / name).write_text(json.dumps(doc))
    assert all(r["status"] == "done" for r in scan_inbox(cfg, store))
    for k, tin in VV_TINS.items():
        store.save_npi(f"18{tin[1:]}", f"{k} REHAB", "225100000X", "x", "Town",
                       VV_STATE[k], entity_type="NPI-2", zip_code="64105")
    store.rebuild_rollups()
    return TestClient(create_app(cfg, store), base_url="http://localhost")


VV_BODY = {"market": {"state": "MO", "discipline": "PT", "month": "latest"}, "preset": "ortho"}


def _vv_rows(d):
    return {r["payer"]: r for r in d["rows"] + [d["all_payers"]]}


def test_visit_value_in_this_market_is_the_hand_answer(vv_app):
    """3.1-3.3 known answer (worked above). was: no such block —
    POST /api/schedule/visit/market answered 404."""
    r = vv_app.post("/api/schedule/visit/market", json={**VV_BODY, "subject": "730000003"})
    assert r.status_code == 200, r.text
    d = r.json()
    rows = _vv_rows(d)
    assert list(rows) == ["Payer A", "Payer B", "Payer C", "All payers"]
    stat = ("practices", "mean", "median", "p25", "p75", "incomplete_excluded", "suppressed")
    assert {k: tuple(rows[k][s] for s in stat) for k in rows} == {
        "Payer A": (6, 130.0, 130.0, 105.0, 155.0, 0, False),
        "Payer B": (5, 148.0, 140.0, 120.0, 160.0, 1, False),
        "Payer C": (None, None, None, None, None, 0, True),
        "All payers": (6, 136.67, 140.0, 107.5, 172.5, None, False)}
    pos = ("value", "percentile", "ranked_among", "quartile", "vs_mean", "vs_mean_pct",
           "vs_median", "vs_median_pct")
    assert tuple(rows["Payer A"]["subject"][k] for k in pos) == (
        120.0, 40, 6, "Q2", -10.0, -7.7, -10.0, -7.7)
    assert tuple(rows["Payer B"]["subject"][k] for k in pos) == (
        140.0, 50, 5, "Q3", -8.0, -5.4, 0.0, 0.0)
    assert rows["Payer C"]["subject"] is None
    assert tuple(rows["All payers"]["subject"][k] for k in pos) == (
        130.0, 40, 6, "Q2", -6.67, -4.9, -10.0, -7.1)
    assert d["incomplete_excluded"] == 1 and d["suppressed_payers"] == ["Payer C"]
    assert (d["state"], d["discipline"], d["units"]) == ("MO", "PT", 4)
    # the subject is optional: the market numbers do not move without it
    alone = vv_app.post("/api/schedule/visit/market", json=VV_BODY).json()
    assert {k: tuple(v[s] for s in stat) for k, v in _vv_rows(alone).items()} == \
        {k: tuple(v[s] for s in stat) for k, v in rows.items()}
    assert all("subject" not in v for v in _vv_rows(alone).values())


def test_visit_value_screen_printable_and_csv_print_the_same_numbers(vv_app):
    """3.2: one dict, three surfaces. was: no printable / CSV (404)."""
    import csv
    import io
    import re
    body = {**VV_BODY, "subject": "730000003"}
    d = vv_app.post("/api/schedule/visit/market", json=body).json()
    page = vv_app.post("/api/report/visitvalue", json=body)
    assert page.status_code == 200
    table = re.search(r"<table class='vv'>.*?</table>", page.text, re.S).group(0)
    printed = [[re.sub(r"<[^>]+>", "", c).replace("&amp;", "&") for c in
                re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)]
               for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", table.split("<tbody>")[1], re.S)]
    text = vv_app.post("/api/schedule/visit/market.csv", json=body).text.lstrip("﻿")
    data = [ln for ln in text.splitlines() if not ln.startswith("#")]
    recs = list(csv.DictReader(io.StringIO("\n".join(data))))
    for r, p, c in zip(d["rows"] + [d["all_payers"]], printed, recs):
        assert p[0] == c["payer"] == r["payer"]
        if r["suppressed"]:
            assert c["suppressed"] == "yes" and c["mean"] == "" and "not shown" in p[1]
            continue
        for k, i in (("mean", 2), ("median", 3), ("p25", 4), ("p75", 5)):
            assert p[i] == f"${r[k]:,.2f}" and c[k] == f"{r[k]:.2f}", (k, p, c)
        assert p[1] == f"{r['practices']:,}" == c["practices"]
        s = r.get("subject")
        if s:
            assert p[7] == f"${s['value']:,.2f}" and c["subject_value"] == f"{s['value']:.2f}"
            assert p[8] == f"p{s['percentile']} of {s['ranked_among']}"
            assert c["subject_percentile"] == f"{s['percentile']}" and p[9] == c["quartile"]
    assert len(printed) == len(recs) == 4
    # and the printable RATE CARD carries the same block when asked
    rc = vv_app.post("/api/report/ratecard", json={
        "subject": "730000003", "market": {"month": "latest", "state": "MO", "discipline": "PT"},
        "preset": "ortho", "visit_market": True})
    assert rc.status_code == 200 and table in rc.text
    fee = vv_app.post("/api/schedule/fee.csv", json={
        "subject": "730000003", "market": {"month": "latest", "state": "MO", "discipline": "PT"},
        "preset": "ortho", "visit_market": True}).text
    assert data[1] in fee


def test_visit_value_refuses_without_state_or_discipline_or_a_market(vv_app):
    """3.1: a sentence, never an empty table. was: 404 (no block)."""
    for market, words in (({"discipline": "PT"}, "pick a state"),
                          ({"state": "MO"}, "pick a discipline"),
                          ({"state": "MO", "discipline": "XX"}, "pick a discipline"),
                          ({"state": "KS", "discipline": "PT"}, "no payer prices this visit"),
                          ({"state": "MO", "discipline": "OT"}, "no payer prices this visit")):
        r = vv_app.post("/api/schedule/visit/market", json={"market": market, "preset": "ortho"})
        assert r.status_code == 422 and words in r.json()["detail"], (market, r.text)
    r = vv_app.post("/api/schedule/visit/market", json={**VV_BODY, "subject": "NO SUCH PLACE"})
    assert r.status_code == 422 and "no practice" in r.json()["detail"]


def test_visit_value_is_the_same_on_two_generations(vv_app, store):
    """3.2 determinism: the same question on an unchanged store after a
    rebuild (a new generation) returns the same bytes. was: 404."""
    body = {**VV_BODY, "subject": "730000003"}
    one = vv_app.post("/api/schedule/visit/market", json=body).content
    csv_one = vv_app.post("/api/schedule/visit/market.csv", json=body).content
    store.rebuild_rollups()
    two = vv_app.post("/api/schedule/visit/market", json=body).content
    csv_two = vv_app.post("/api/schedule/visit/market.csv", json=body).content
    assert one == two and csv_one == csv_two


def test_visit_value_reads_the_materialised_medians(vv_app, store, monkeypatch):
    """3.2: no new per-request scan of raw rates — the per-discipline
    practice medians (built once per spine generation) feed the block, and
    give the live query's numbers to the bit. was: 404 (no block)."""
    import mrfx.benchmark as bm
    from mrfx.visit_market import market_visit_value
    built = []
    real = store.spine_materialised
    monkeypatch.setattr(store, "spine_materialised",
                        lambda name, *a: built.append(name) or real(name, *a))
    fast = market_visit_value.uncached(store, VV_BODY["market"], None, "ortho", "730000003")
    assert built == [bm.DISC_PAYER_PRACTICE_MEDIANS]
    monkeypatch.setattr(bm, "_house_eligible", lambda m: False)
    live = market_visit_value.uncached(store, VV_BODY["market"], None, "ortho", "730000003")
    assert json.dumps(fast, sort_keys=True) == json.dumps(live, sort_keys=True)
