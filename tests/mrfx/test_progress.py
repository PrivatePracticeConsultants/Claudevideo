"""The 0–100 % progress bar (review 13 Group 2) — the 14 pins of
docs/review13_repros/progress/DESIGN.md §3.6.7. None of these ran on c8b80fc:
there was no /api/progress, no X-Mrfx-Progress and no mrfx/progress.py."""

from __future__ import annotations

import inspect
import threading
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from mrfx import progress
from mrfx.api import create_app
from tests.mrfx.test_review13 import _market, org, tins  # noqa: F401

TOK = "abcdefghijklmnop{:04d}"


def _tok(i):
    return TOK.format(i)


@pytest.fixture
def mkt(cfg, store):
    T, _ = _market(cfg, store, "Alpha Health", "71000", [40, 50, 60, 70, 80])
    _market(cfg, store, "Beta Health", "71000", [42, 52, 62, 72, 82])
    store.rebuild_rollups()
    return T


def _bench_body(T):
    return {"subject": T[0], "market": {"month": "latest", "state": "MO",
                                        "billing_class": "professional", "payers": []}}


def test_t1_bar_reaches_100_only_with_the_result(cfg, store, mkt):
    c = TestClient(create_app(cfg, store))
    plain = c.post("/api/benchmark/market", json=_bench_body(mkt)).json()
    seen, out = [], {}

    def run():
        out["r"] = c.post("/api/benchmark/market", json=_bench_body(mkt),
                          headers={"X-Mrfx-Progress": _tok(1)})
    t = threading.Thread(target=run)
    t.start()
    while t.is_alive():
        r = c.get(f"/api/progress/{_tok(1)}")
        if r.status_code == 200:
            seen.append(r.json())
        time.sleep(0.02)
    t.join()
    assert out["r"].status_code == 200 and out["r"].json() == plain
    running = [s["pct"] for s in seen if s["state"] in ("running", "waiting")]
    assert all(p <= 99 for p in running)
    assert running == sorted(running)
    final = c.get(f"/api/progress/{_tok(1)}").json()
    assert final["state"] == "done" and final["pct"] == 100


def _slow(store):
    with store.connect() as con:
        con.execute("SELECT count(*) FROM range(2000000000) t(i) WHERE i % 7 = 3").fetchone()
    return {"rows": []}


def test_t2_stop_interrupts_the_query_within_a_bound(cfg, store, mkt, monkeypatch):
    import mrfx.api as A
    monkeypatch.setattr(A, "compute_benchmark", lambda store_, s, m: _slow(store_))
    c = TestClient(create_app(cfg, store))
    out = {}

    def run():
        out["r"] = c.post("/api/benchmark/market", json=_bench_body(mkt),
                          headers={"X-Mrfx-Progress": _tok(2)})
        out["t"] = time.monotonic()
    t = threading.Thread(target=run)
    t.start()
    time.sleep(0.4)
    t_stop = time.monotonic()
    assert c.delete(f"/api/progress/{_tok(2)}").json() == {"stopped": True}
    t.join(10)
    assert out["r"].status_code == 409 and out["r"].json()["detail"] == "stopped"
    assert out["t"] - t_stop < 1.0
    t0 = time.monotonic()
    assert c.get("/api/boot").status_code == 200
    assert time.monotonic() - t0 < 0.5           # the server thread is free
    assert c.get(f"/api/progress/{_tok(2)}").json()["state"] == "stopped"


def test_t3_a_cancelled_run_never_delivers(cfg, store, mkt, monkeypatch):
    """Stop during the dossier's first section: 409, and every later section
    raised Stopped WITHOUT opening a connection."""
    import mrfx.dossier as D
    c = TestClient(create_app(cfg, store))
    opened = {"n": 0, "after": 0}
    real = store._connect_with_retry

    def counting():
        opened["n"] += 1
        if progress.current().cancelled:
            opened["after"] += 1
        return real()
    monkeypatch.setattr(store, "_connect_with_retry", counting)
    real_stage = D._DOSSIER_STAGE.get

    class Stage(dict):
        def get(self, k, d=None):
            if k == "Medicare volume":          # the stop arrives during section 2
                progress.current().cancel("stopped")
            return real_stage(k, d)
    monkeypatch.setattr(D, "_DOSSIER_STAGE", Stage(D._DOSSIER_STAGE))
    r = c.post("/api/report/dossier", json={"subject": mkt[0], "market": {"state": "MO"}},
               headers={"X-Mrfx-Progress": _tok(3)})
    assert r.status_code == 409 and r.json()["detail"] == "stopped"
    assert opened["after"] == 0
    assert "<html" not in r.text.lower()


def test_t4_a_progress_failure_never_fails_the_report(cfg, store, mkt, monkeypatch):
    c = TestClient(create_app(cfg, store))
    body = {"subject": mkt[0], "market": {"month": "latest", "state": "MO", "payers": []}}
    clean = c.post("/api/report/ratecard", json=body).text

    def boom(*a, **k):
        raise RuntimeError("progress broke")
    for name in ("_emit_locked", "_pct_locked", "_sample"):
        monkeypatch.setattr(progress.Progress, name, boom)
    r = c.post("/api/report/ratecard", json=body, headers={"X-Mrfx-Progress": _tok(4)})
    assert r.status_code == 200
    norm = lambda h: __import__("re").sub(r"Generated [^<\n]*", "", h)  # noqa: E731
    assert norm(r.text) == norm(clean)
    p = c.get(f"/api/progress/{_tok(4)}")
    assert p.status_code in (200, 404) and p.status_code < 500
    monkeypatch.setattr(progress.Registry, "add", boom)
    r2 = c.post("/api/report/ratecard", json=body, headers={"X-Mrfx-Progress": _tok(5)})
    assert r2.status_code == 200 and norm(r2.text) == norm(clean)


def test_t5_results_identical_with_and_without_a_token(cfg, store, mkt):
    import io
    import re
    import zipfile
    c = TestClient(create_app(cfg, store))
    H = {"X-Mrfx-Progress": _tok(6)}
    leads = {"market": {"month": "latest", "payers": [], "state": "MO", "therapy_only": False},
             "min_codes": 1}
    assert c.post("/api/leads", json=leads).json() == c.post("/api/leads", json=leads,
                                                              headers=H).json()
    ch = {"market": {"month": "2026-06", "payers": [], "state": "MO"}}
    assert c.post("/api/changes.csv", json=ch).content == c.post(
        "/api/changes.csv", json=ch, headers={"X-Mrfx-Progress": _tok(7)}).content
    rc = {"subject": mkt[0], "market": {"month": "latest", "state": "MO", "payers": []}}
    norm = lambda h: re.sub(r"Generated [^<\n]*", "", h)  # noqa: E731
    assert norm(c.post("/api/report/ratecard", json=rc).text) == norm(c.post(
        "/api/report/ratecard", json=rc, headers={"X-Mrfx-Progress": _tok(8)}).text)

    def member(resp):
        z = zipfile.ZipFile(io.BytesIO(resp.content))
        return z.read([n for n in z.namelist() if n.endswith(".csv")][0])
    q = "grain=tin&latest=1&dollar_only=1&payer=Alpha%20Health&cpt=97110"
    assert member(c.get(f"/api/export.zip?{q}")) == member(
        c.get(f"/api/export.zip?{q}&progress={_tok(9)}"))
    assert c.get(f"/api/progress/{_tok(9)}").json()["state"] == "done"


def test_t6_no_token_means_no_overhead(cfg, store, mkt, monkeypatch):
    calls = []
    real = progress.Progress.attach
    monkeypatch.setattr(progress.Progress, "attach",
                        lambda self, con: (calls.append(1), real(self, con)))
    c = TestClient(create_app(cfg, store))
    c.post("/api/benchmark/market", json=_bench_body(mkt))
    assert calls == []
    assert progress.current() is progress._NOOP
    c.post("/api/benchmark/market", json=_bench_body(mkt), headers={"X-Mrfx-Progress": _tok(10)})
    assert calls


OUTPUT_ROUTES = [
    "/api/rates", "/api/summary", "/api/export.csv", "/api/export.zip",
    "/api/export/outreach.zip", "/api/export/outreach.csv", "/api/entity/{grain}/{unit_id:path}",
    "/api/code/{code}", "/api/trend", "/api/overview", "/api/quality/freshness",
    "/api/quality/types", "/api/quality/posture", "/api/market/geography",
    "/api/market/negotiability", "/api/market/medicare", "/api/market/differential",
    "/api/territory/local", "/api/territory/radius", "/api/territory/concentration",
    "/api/roster", "/api/roster.csv", "/api/benchmark/market", "/api/benchmark/opportunity",
    "/api/quality/size", "/api/quality/subject-types", "/api/contract-gaps", "/api/report/pitch",
    "/api/report/negotiation", "/api/negotiate/compare", "/api/report/payer-compare",
    "/api/negotiate/proposal", "/api/report/proposal", "/api/leverage", "/api/leverage/summary",
    "/api/economics/visit", "/api/floors/compare", "/api/hospital/parity", "/api/schedule/fee",
    "/api/schedule/visit", "/api/report/ratecard", "/api/schedule/fee.csv",
    "/api/report/org-bundle.zip", "/api/schedule/visit/market", "/api/report/visitvalue",
    "/api/schedule/visit/market.csv", "/api/clients/digest", "/api/engagements/compare",
    "/api/contracts/renewals", "/api/contracts/new-to-network", "/api/newclinics",
    "/api/closures", "/api/territory/steal-share", "/api/report/dossier",
    "/api/report/market", "/api/utilization/growth", "/api/utilization/service-lines",
    "/api/market/sizing", "/api/remits/check", "/api/remits/check.csv", "/api/medicare/org",
    "/api/medicare/leaders", "/api/medicare/eligibility", "/api/leads", "/api/leads.csv",
    "/api/leaderboard", "/api/changes", "/api/changes.csv", "/api/trajectory",
    "/api/utilization/volumes", "/api/validate",
]


def test_t7_every_output_route_carries_the_decorator(cfg, store):
    from fastapi.routing import APIRoute
    app = create_app(cfg, store)
    marked = {r.path for r in app.routes if isinstance(r, APIRoute)
              and getattr(r.endpoint, "_mrfx_progress", None)}
    missing = [p for p in OUTPUT_ROUTES if p not in marked]
    assert missing == []
    from mrfx.progress_plans import PLANS
    for r in app.routes:
        if isinstance(r, APIRoute) and getattr(r.endpoint, "_mrfx_progress", None):
            assert r.endpoint._mrfx_progress in PLANS, r.path


def test_t8_supersede_still_interrupts_and_409s(cfg, store, mkt):
    app = create_app(cfg, store)
    c = TestClient(app)
    lock = app.state.summary_lock
    q = "/api/summary?grain=tin&latest=1&payer=Alpha%20Health"     # not cached yet
    view = {"X-Mrfx-View": "page1:summary"}
    q2 = "/api/summary?grain=tin&latest=1&payer=Alpha%20Health&cpt=97110"   # nor this
    out = {}
    lock.acquire()
    try:
        t1 = threading.Thread(target=lambda: out.__setitem__("a", c.get(
            q, headers={**view, "X-Mrfx-Progress": _tok(11)})))
        t1.start()
        time.sleep(0.3)
        t2 = threading.Thread(target=lambda: out.__setitem__("b", c.get(
            q2, headers={**view, "X-Mrfx-Progress": _tok(12)})))
        t2.start()
        time.sleep(0.3)
    finally:
        lock.release()
    t1.join(15)
    t2.join(15)
    assert out["a"].status_code == 409 and "superseded" in out["a"].json()["detail"]
    alone2 = TestClient(create_app(cfg, store)).get(q2).json()      # the same, asked alone
    assert out["b"].status_code == 200 and out["b"].json() == alone2
    assert c.get(f"/api/progress/{_tok(11)}").json()["state"] == "stopped"


def test_t9_waiting_is_shown_as_waiting(cfg, store, mkt):
    app = create_app(cfg, store)
    c = TestClient(app)
    lock = app.state.summary_lock
    out = {}
    lock.acquire()
    try:
        t = threading.Thread(target=lambda: out.__setitem__("r", c.get(
            "/api/summary?grain=tin&latest=1", headers={
                "X-Mrfx-View": "page2:summary", "X-Mrfx-Progress": _tok(13)})))
        t.start()
        deadline = time.monotonic() + 2
        st = None
        while time.monotonic() < deadline:
            r = c.get(f"/api/progress/{_tok(13)}")
            if r.status_code == 200 and r.json()["state"] == "waiting":
                st = r.json()
                break
            time.sleep(0.02)
        assert st is not None and st["indeterminate"] and "Waiting" in st["stage"]
    finally:
        lock.release()
    t.join(10)
    assert out["r"].status_code == 200
    assert c.get(f"/api/progress/{_tok(13)}").json()["state"] == "done"


def test_t10_detached_jobs_report_progress_and_survive_a_stop(cfg, store, mkt, monkeypatch):
    import mrfx.packets as P
    from mrfx.clients import add_client
    add_client(store, mkt[0])
    add_client(store, mkt[1])
    gate = threading.Event()
    real = P.build_client_packet
    built = []

    def slow_packet(*a, **k):
        built.append(1)
        if len(built) == 1:
            gate.wait(10)
        return real(*a, **k)
    monkeypatch.setattr(P, "build_client_packet", slow_packet)
    cfg.packets_dir = cfg.store_dir.parent / "packets"
    c = TestClient(create_app(cfg, store))
    assert c.post("/api/packets/build").json() == {"started": True}
    deadline = time.monotonic() + 10
    st = {}
    while time.monotonic() < deadline:
        st = c.get("/api/packets").json()
        if st.get("progress", {}).get("stage") == "Building the packets":
            break
        time.sleep(0.05)
    assert st["progress"]["stage"] == "Building the packets"
    assert 0 < st["progress"]["pct"] < 100
    assert c.delete("/api/packets").json() == {"stopped": True}
    gate.set()
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline and c.get("/api/packets").json()["state"] == "running":
        time.sleep(0.05)
    st = c.get("/api/packets").json()
    assert st["state"] == "done" and "stopped by you" in st["message"]
    assert len(built) == 1                           # the second client never started
    assert c.post("/api/packets/build").json() == {"started": True}   # not wedged


def test_t11_poll_endpoints_are_async_and_answer_a_busy_server(cfg, store, monkeypatch):
    from fastapi.routing import APIRoute
    app = create_app(cfg, store)
    polls = [r for r in app.routes if isinstance(r, APIRoute) and r.path == "/api/progress/{token}"]
    assert len(polls) == 2 and all(inspect.iscoroutinefunction(r.endpoint) for r in polls)
    gate = threading.Event()

    def slow_stats(*a, **k):
        gate.wait(10)
        return {"rates": 0, "tins": 0, "payers": 0}
    monkeypatch.setattr(store, "store_stats", slow_stats)
    with TestClient(app) as c:
        ths = [threading.Thread(target=lambda: c.get("/api/stats")) for _ in range(110)]
        for t in ths:
            t.start()
        time.sleep(0.5)
        t0 = time.monotonic()
        r = c.get(f"/api/progress/{_tok(14)}")
        took = time.monotonic() - t0
        gate.set()
        for t in ths:
            t.join(20)
    assert r.status_code == 404 and took < 1.0


def test_t12_cli_prints_the_same_stages(cfg, store, mkt, monkeypatch, capsys):
    from mrfx import cli
    from mrfx.progress_plans import PLANS
    monkeypatch.setattr(cli, "_something_owns_the_port", lambda *a, **k: False)
    out = cfg.store_dir.parent / "x.csv"
    args = SimpleNamespace(out=str(out), payer=None, cpt=None, modifier=None, billing_class=None,
                           q=None, grain="tin", all_types=False, rate_min=None, rate_max=None)
    assert cli.cmd_export(cfg, args) == 0
    err = capsys.readouterr().err
    names = [n for n, _w in PLANS["export"]]
    seen = [n for n in names if n in err]
    assert seen == ["Writing the rows to a file", "Writing the methodology"]
    assert err.index("Writing the rows to a file") < err.index("Writing the methodology")
    assert "100 %" in err


def test_t13_the_sampler_tolerates_connection_churn(cfg, store, mkt):
    import sys
    c = TestClient(create_app(cfg, store))
    leaked = []
    old = threading.excepthook
    threading.excepthook = lambda a: leaked.append(a)
    try:
        for i in range(200):
            r = c.get("/api/code/97110?grain=tin", headers={"X-Mrfx-Progress": _tok(100 + i)})
            assert r.status_code == 200
    finally:
        threading.excepthook = old
    # the samplers end within one sampling tick of their run; on a loaded box
    # (the release check runs the race tests beside a CPU hog) that tick can
    # take longer than the 1 s this waited — wait for them, up to 15 s
    # (review 14 Group 4: no test may need a retry)
    t_end = time.monotonic() + 15
    alive = [t for t in threading.enumerate() if t.name == "mrfx-progress"]
    while alive and time.monotonic() < t_end:
        time.sleep(0.2)
        alive = [t for t in threading.enumerate() if t.name == "mrfx-progress"]
    assert leaked == [] and alive == [], (leaked, alive)
    assert sys.exc_info() == (None, None, None)


def test_t14_the_first_run_names_the_materialise_stage(cfg, store, mkt):
    from mrfx.benchmark import compute_benchmark
    store._bump_generation()                 # a fresh spine generation
    for f in store.dir.glob("practice_medians*.parquet"):
        f.unlink()

    def stages_of_one_run():
        names = []
        p = progress.Progress(None, [], on_update=lambda s: names.append(s["stage"]))
        tok = progress.set_current(p)
        try:
            compute_benchmark(store, mkt[0], {"month": "latest", "state": "MO"})
        finally:
            progress.reset_current(tok)
        return names
    first = stages_of_one_run()
    second = stages_of_one_run()
    key = "Preparing the practice medians (first run since new data)"
    assert key in first and key not in second
