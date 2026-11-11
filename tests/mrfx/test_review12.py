"""Review 12: the 0.7.5 zip audited end to end (docs/NEXT_STEPS_PROMPT_12.md).
Every finding was reproduced on 68d9bc7 (v0.7.5) by a script under
docs/review12_repros/; each test below pins one item, and its docstring says
what it got on 68d9bc7 ("was: ...")."""

from __future__ import annotations

import hashlib
import json
import threading

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
