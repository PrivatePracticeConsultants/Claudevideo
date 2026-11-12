"""Review 13 (0.7.7): every item of Groups 1, 3, 4 and 5 and every feature of
Group 6 of docs/NEXT_STEPS_PROMPT_13.md, pinned with a small hand-worked
store. Each test FAILED on c8b80fc (0.7.6); the value it got there is in its
docstring ("was: …"). The progress-bar tests of DESIGN §3.6.7 live in
tests/mrfx/test_progress.py."""

from __future__ import annotations

import json

import duckdb
import pytest
from fastapi.testclient import TestClient

from mrfx.api import create_app
from mrfx.ingest import scan_inbox

CLINIC = "261QP2000X"


def price(rate, cls="professional", pos=("11",), mods=("GP",)):
    return {"negotiated_type": "negotiated", "negotiated_rate": float(rate),
            "billing_class": cls, "service_code": list(pos),
            "billing_code_modifier": list(mods), "expiration_date": "9999-12-31"}


def doc(payer, day, prices, npis):
    """prices: {code: {tin: rate | [price dicts]}}; npis: {tin: [npi, …]}."""
    items = []
    for code, by_tin in prices.items():
        nr = [{"provider_groups": [{"npi": npis[tin], "tin": {"type": "ein", "value": tin}}],
               "negotiated_prices": r if isinstance(r, list) else [price(r)]}
              for tin, r in by_tin.items()]
        items.append({"negotiation_arrangement": "ffs", "billing_code_type": "CPT",
                      "billing_code": code, "negotiated_rates": nr})
    return {"reporting_entity_name": payer, "last_updated_on": day, "version": "2.0.0",
            "in_network": items}


def load(cfg, store, docs):
    for fname, d in docs:
        (cfg.inbox_dir / fname).write_text(json.dumps(d))
    return scan_inbox(cfg, store)


def org(store, npi, label, state="MO", city="Town"):
    store.save_npi(npi, label, CLINIC, "x", city, state, entity_type="NPI-2",
                   zip_code="64105")


def tins(prefix, n):
    return [f"{prefix}{i:04d}" for i in range(1, n + 1)]


def _market(cfg, store, payer, prefix, rates, codes=("97110", "97112"), day="2026-06-01"):
    T = tins(prefix, len(rates))
    npis = {t: [f"1{t}"] for t in T}
    d = doc(payer, day, {c: dict(zip(T, [r + j for r in rates])) for j, c in enumerate(codes)},
            npis)
    out = load(cfg, store, [(f"{prefix}_{day[:7]}.json", d)])
    for i, t in enumerate(T):
        org(store, npis[t][0], f"{payer.upper()} P{i + 1} REHAB")
    return T, out


# ---------------------------------------------------------------- group 1


def _three_payers_rollup_failing(cfg, store, monkeypatch):
    """payer A loads with a working rollup; B and C land while every rollup
    attempt fails (a repeated out-of-memory, a kill) — the user's screenshot."""
    T, _ = _market(cfg, store, "Alpha Health", "71000", [40, 50, 60, 70])
    store.rebuild_rollups()

    def boom(*a, **k):
        raise duckdb.OutOfMemoryException("could not allocate block (simulated)")
    monkeypatch.setattr(store, "rebuild_rollups", boom)
    monkeypatch.setattr(store, "update_rollups_incremental", boom)
    _market(cfg, store, "Beta Health", "72000", [41, 51, 61])
    _market(cfg, store, "Gamma Health", "73000", [42, 52, 62])
    return T


def test_1_2_header_and_pickers_say_what_the_analytics_cover(cfg, store, monkeypatch):
    """1.2 (P0). was: /api/stats and /api/boot carried no coverage field at
    all (KeyError 'analytics'); /api/payers = {'payers': ['Alpha Health']}
    while the header counted 3 payers; the pitch report's methodology said
    nothing about the two payers it could not see."""
    T = _three_payers_rollup_failing(cfg, store, monkeypatch)
    c = TestClient(create_app(cfg, store))
    s = c.get("/api/stats").json()
    a = s["analytics"]
    assert s["payers"] == 3
    assert a["state"] == "behind", a
    assert (a["covered_payers"], a["loaded_payers"]) == (1, 3)
    assert (a["covered_files"], a["done_files"]) == (1, 3)
    assert a["sentence"].startswith("Analytics cover 1 of 3 payers (1 of 3 files).")
    assert c.get("/api/boot").json()["analytics"]["state"] == "behind"
    p = c.get("/api/payers").json()
    assert p["payers"] == ["Alpha Health", "Beta Health", "Gamma Health"]
    assert p["covered"] == ["Alpha Health"]
    h = c.post("/api/report/pitch", json={
        "subject": T[0], "market": {"month": "latest", "state": "MO"}})
    assert h.status_code == 200, h.text
    assert "Analytics cover 1 of 3 payers (1 of 3 files)" in h.text
    # an uncovered payer answers the normal "no rows" (the page adds why)
    r = c.get("/api/rates", params={"grain": "tin", "payer": "Beta Health"}).json()
    assert r["total"] == 0


def test_1_2_coverage_is_current_once_the_analytics_catch_up(cfg, store, monkeypatch):
    """1.2. was: no 'analytics' field (KeyError)."""
    _three_payers_rollup_failing(cfg, store, monkeypatch)
    monkeypatch.undo()
    store.update_rollups_incremental()
    c = TestClient(create_app(cfg, store))
    a = c.get("/api/stats").json()["analytics"]
    assert a["state"] == "current" and a["sentence"] == ""
    assert (a["covered_payers"], a["loaded_payers"]) == (3, 3)
    assert c.get("/api/payers").json()["covered"] == [
        "Alpha Health", "Beta Health", "Gamma Health"]


def _spine_rows(store):
    with store.connect() as con:
        return con.execute("SELECT * FROM rates_by_tin_tbl ORDER BY ALL").fetchall()


def _dir_rows(store):
    with store.connect() as con:
        return con.execute("SELECT * FROM tin_directory_tbl ORDER BY ALL").fetchall()


def _seed_many(store):
    """Five payers, several codes and months, so the plan has many slices."""
    from mrfx.store import FINISHED_NOW
    rows = []
    for pi, payer in enumerate(["P-A", "P-B", "P-C", "P-D", "P-E"]):
        recs = []
        for t in range(12):
            for ci, code in enumerate(["97110", "97112", "97140", "97530", "92507"]):
                for m in ("2026-05", "2026-06"):
                    for k in range(1 + (t % 3)):
                        recs.append(dict(
                            payer=payer, tin_value=f"4{pi}{t:07d}", tin_type="ein",
                            npi=f"1{pi}{t:04d}{k:04d}", source_file=f"f{pi}.json",
                            billing_code=code, billing_code_type="CPT", discipline="pt",
                            is_timed=True, billing_class="professional",
                            negotiated_rate=40.0 + pi + ci + (t % 5) + k * 0.5,
                            negotiated_type="negotiated", is_dollar_rate=True,
                            modifier_set=["GP"] if k % 2 else [], service_code=["11"],
                            file_month=m, last_updated_on=m + "-01", expiration_date=None,
                            schema_version="2.0.0", tin_is_really_npi=False, state="MO"))
        with store.rates_part_writer(f"f{pi}.json") as w:
            w.write_batch(recs)
        store.upsert_file(f"f{pi}.json", payer=payer, file_type="in_network", status="done",
                          rows_emitted=len(recs), finished_at=FINISHED_NOW)
        rows.extend(recs)
    return rows


def _one_shot_reference(store):
    """The 0.7.6 full build (one statement per table over the whole store) —
    the reference the sliced plan must equal byte for byte."""
    with store.connect() as con:
        store._build_rollup_tables(con, split=1, threads=1)
    return _spine_rows(store), _dir_rows(store)


def test_1_3_sliced_build_equals_the_one_shot_build(cfg, store, monkeypatch):
    """1.3 (P0): the durable plan — payer x code-range slices, hash halves,
    each committed alone — ends with the same spine and directory rows as the
    old one-shot build. was: no plan existed (a full rebuild was one
    transaction per table; 'rollup_plan' never written)."""
    _seed_many(store)
    ref = _one_shot_reference(store)
    seen = []
    real = store._run_slice

    def spy(con, plan, idx, log):
        seen.append(dict(plan["slices"][idx]))
        return real(con, plan, idx, log)
    monkeypatch.setattr(store, "_run_slice", spy)
    # tiny slices: every payer splits into several code ranges and hash halves
    monkeypatch.setattr(store, "_rollup_partition_rows", lambda split=1, threads=1: 37)
    store.rebuild_rollups()
    assert (_spine_rows(store), _dir_rows(store)) == ref
    assert sum(1 for s in seen if s["t"] == "spine") > 10
    assert any(s["t"] == "spine" and s["k"] > 1 for s in seen)
    assert store.rollup_plan() is None and not store.rollups_stale()


def test_1_3_a_killed_build_resumes_and_ends_identical(cfg, store, monkeypatch):
    """1.3 (P0): kill the build half way (the process dies inside a slice);
    the next attempt resumes from the last committed slice — never from the
    start — and ends with the same rows. Coverage after slice k equals the
    payers committed so far. was: a failed full rebuild rolled back to
    nothing ('rollup_plan' absent; every slice redone)."""
    _seed_many(store)
    ref = _one_shot_reference(store)
    with store.connect() as con:          # start from an EMPTY spine
        con.execute("DROP TABLE rates_by_tin_tbl")
        con.execute("DROP TABLE tin_directory_tbl")
        con.execute("DELETE FROM meta WHERE key LIKE 'rollup_%' OR key LIKE 'directory_%'")
        store._register_views(con)
    monkeypatch.setattr(store, "_rollup_partition_rows", lambda split=1, threads=1: 200)
    real = store._run_slice
    calls = {"n": 0}

    class Killed(Exception):
        pass

    def dies_half_way(con, plan, idx, log):
        calls["n"] += 1
        if calls["n"] > len(plan["slices"]) // 2:
            raise Killed("process killed")
        return real(con, plan, idx, log)
    monkeypatch.setattr(store, "_run_slice", dies_half_way)
    with pytest.raises(Killed):
        store.rebuild_rollups()
    plan = store.rollup_plan()
    assert plan is not None and 0 < plan["next"] < len(plan["slices"])
    # what the screens hold right now = exactly the payers whose slices committed
    committed = {s["payer"] for s in plan["slices"][:plan["next"]] if s["t"] == "spine"}
    assert set(store.spine_payers()) == committed and committed
    assert store.rollups_stale()
    assert store.analytics_coverage(max_age_seconds=0)["state"] == "failed"
    done_before = plan["next"]
    redone = []

    def counts(con, plan, idx, log):
        redone.append(idx)
        return real(con, plan, idx, log)
    monkeypatch.setattr(store, "_run_slice", counts)
    store.rebuild_rollups()                 # the restart
    assert min(redone) == done_before       # resumed, nothing before it redone
    assert (_spine_rows(store), _dir_rows(store)) == ref
    assert store.rollup_plan() is None and store.rollup_failure() is None


def test_1_3_an_out_of_memory_slice_is_halved_and_earlier_slices_kept(cfg, store, monkeypatch):
    """1.3: a slice that runs out of memory is cut in two and retried; the
    slices committed before it stay. was: one OOM restarted the whole build at
    a finer split (every committed partition thrown away)."""
    _seed_many(store)
    ref = _one_shot_reference(store)
    monkeypatch.setattr(store, "_rollup_partition_rows", lambda split=1, threads=1: 400)
    real = store._run_slice
    log = []

    def oom_once(con, plan, idx, lg):
        sl = plan["slices"][idx]
        log.append((idx, sl["t"], sl.get("payer"), sl["k"], len(sl.get("cc") or [])))
        if sl["t"] == "spine" and sl["payer"] == "P-C" and not getattr(oom_once, "fired", False):
            oom_once.fired = True
            raise duckdb.OutOfMemoryException("Out of Memory Error: could not allocate block")
        return real(con, plan, idx, lg)
    monkeypatch.setattr(store, "_run_slice", oom_once)
    store.rebuild_rollups()
    first_c = next(i for i, e in enumerate(log) if e[2] == "P-C")
    assert log[first_c + 1][0] == log[first_c][0]          # the same position, retried
    assert log[first_c + 1][2] == "P-C"
    # the retry is smaller: a code range in two, or one code in two hash halves
    assert (log[first_c + 1][4] < log[first_c][4]) or (log[first_c + 1][3] > log[first_c][3])
    assert all(e[0] < log[first_c][0] for e in log[:first_c])    # earlier slices not redone
    assert (_spine_rows(store), _dir_rows(store)) == ref


def test_1_3_the_queue_never_gives_up_on_the_analytics(cfg, store, monkeypatch):
    """1.3: three failed attempts used to end the run's analytics ("giving up
    for this run"); now the failure is shown with its sentence and the next
    idle gap retries. was: after 3 failures the credits were dropped and no
    further attempt was made (attempts == 3, analytics key absent)."""
    import mrfx.fetch as F
    _market(cfg, store, "Alpha Health", "71000", [40, 50, 60])
    store.rebuild_rollups()
    attempts = {"n": 0}

    def fail(*a, **k):
        attempts["n"] += 1
        raise RuntimeError("Out of Memory Error: could not allocate block")
    monkeypatch.setattr(store, "update_rollups_incremental", fail)
    real_rebuild = store.rebuild_rollups

    def rebuild(*a, **k):
        attempts["n"] += 1
        if attempts["n"] < 8:
            store._note_rollup_failure(duckdb.OutOfMemoryException(
                "Out of Memory Error: could not allocate block"))
            raise duckdb.OutOfMemoryException("could not allocate block")
        return real_rebuild(*a, **k)
    monkeypatch.setattr(store, "rebuild_rollups", rebuild)
    a = store.analytics_coverage(max_age_seconds=0)
    assert a["state"] == "current"
    _market(cfg, store, "Beta Health", "72000", [41, 51, 61])    # rollup fails
    c = TestClient(create_app(cfg, store))
    a = c.get("/api/stats").json()["analytics"]
    assert a["state"] == "failed" and "ran out of memory" in a["reason"]
    assert "Files tab" in a["reason"]
    # the queue's own loop: no give-up — after the back-off, the next idle
    # gap tries again until it works
    monkeypatch.setattr(F, "STALE_CHECK_SECONDS", 0.0)
    import threading
    import time as _time
    clock = {"t": _time.monotonic()}

    class Clock:                     # fetch's own clock only: each idle wait
        def __getattr__(self, k):    # jumps past any back-off
            return getattr(_time, k)

        @staticmethod
        def monotonic():
            return clock["t"]
    monkeypatch.setattr(F, "time", Clock())
    stop = threading.Event()
    loops = {"n": 0}

    def tick_wait(timeout=None):
        clock["t"] += 4000.0
        loops["n"] += 1
        if (attempts["n"] >= 8 and not store.rollups_stale()) or loops["n"] > 40:
            stop.set()
        return stop.is_set()
    monkeypatch.setattr(stop, "wait", tick_wait)
    F.run_queue(cfg, store, stop=stop)
    assert attempts["n"] >= 8 and loops["n"] <= 40
    assert store.analytics_coverage(max_age_seconds=0)["state"] == "current"


def test_1_4_a_size_and_tag_less_probe_is_unknown_not_unchanged(cfg, store):
    """1.4 (P2, diff review 12): a 0.7.4-loaded child behind a server whose
    1-byte probe answers a chunked 200 with no size and no validator. was:
    left at its old month 2026-05, no request beyond the probe, no note."""
    from mrfx import fetch
    from tests.mrfx.test_review11 import Srv, drain, gz, ranged, rate_doc
    cfg.enrichment.mode = "off"
    child = {"b": gz(rate_doc(month="2026-05-01"))}
    s = Srv({})
    try:
        s.routes["/files/r.json.gz"] = lambda req: ranged(child["b"], req, etag='"v1"')
        idx = json.dumps({"reporting_entity_name": "Twin Health", "reporting_structure": [
            {"reporting_plans": [{"plan_name": "P"}],
             "in_network_files": [{"location": s.base + "/files/r.json.gz"}]}]}).encode()
        s.routes["/index.json"] = lambda req: ranged(idx, req)
        fetch.add_urls(store, [s.base + "/index.json"])
        drain(cfg, store)
        with store.write_lock, store.connect() as con:
            con.execute("UPDATE url_queue SET server_validator = NULL, server_size = NULL "
                        "WHERE kind = 'in_network'")
        child["b"] = gz(rate_doc(rate=45.0, n=80, month="2026-06-01"))
        s.routes["/files/r.json.gz"] = lambda req: (
            200, {"Transfer-Encoding": "chunked"}, child["b"], None)
        store.requeue_urls([s.base + "/index.json"])
        with store.write_lock, store.connect() as con:
            con.execute("UPDATE url_queue SET revalidate = 1 WHERE id = 1")
        drain(cfg, store)
        with store.connect() as con:
            months = con.execute("SELECT DISTINCT file_month FROM rates").fetchall()
        assert months == [("2026-06",)]
    finally:
        s.close()


def test_1_4_pooled_months_are_not_recounted_after_a_name_refresh(cfg, store):
    """1.4 (P2): the pooled-months note is an aggregate over the spine; a
    names-only refresh (data generation +1, spine unchanged) must not run it
    again. was: 2 spine aggregates for 2 calls across a name refresh."""
    from mrfx import quality
    day = "2026-06-01"
    T = tins("74000", 3)
    npis = {t: [f"1{t}"] for t in T}
    load(cfg, store, [("a_book.json", doc("Rho", day, {"97110": dict(zip(T, [40, 50, 60]))}, npis)),
                      ("b_book.json", doc("Rho", day, {"97110": dict(zip(T, [41, 50, 60]))}, npis))])
    store.rebuild_rollups()
    runs = {"n": 0}
    real = store.connect

    class Spy:
        def __init__(self, con):
            self._c = con

        def execute(self, sql, *a, **k):
            if "FROM rates_by_tin GROUP BY 1, 2" in sql:
                runs["n"] += 1
            return self._c.execute(sql, *a, **k)

        def __getattr__(self, k):
            return getattr(self._c, k)

        def __enter__(self):
            return self

        def __exit__(self, *e):
            return self._c.__exit__(*e)
    store.connect = lambda: Spy(real())
    try:
        first = quality.pooled_month_files(store)
        store._bump_generation(spine=False)      # what an enrichment name refresh does
        second = quality.pooled_month_files(store)
    finally:
        store.connect = real
    assert first == second and first and first[0]["files"] == 2
    assert runs["n"] == 1


# ---------------------------------------------------------------- group 5


class _SqlSpy:
    """Every statement run on a connection the store hands out."""

    def __init__(self, store, monkeypatch):
        self.sql: list[str] = []
        real = store.connect
        spy = self

        class Con:
            def __init__(self, con):
                self._c = con

            def execute(self, q, *a, **k):
                spy.sql.append(q)
                return self._c.execute(q, *a, **k)

            def __getattr__(self, k):
                return getattr(self._c, k)

            def __enter__(self):
                return self

            def __exit__(self, *e):
                return self._c.__exit__(*e)
        monkeypatch.setattr(store, "connect", lambda: Con(real()))

    def raw_scans(self):
        import re
        return [q for q in self.sql if re.search(r"FROM rates(\s|$|\))", q)]


def _two_payer_store(cfg, store):
    T, _ = _market(cfg, store, "Alpha Health", "71000", [40, 50, 60, 70, 80])
    _market(cfg, store, "Beta Health", "71000", [42, 52, 62, 72, 82])
    store.rebuild_rollups()
    return T


def test_3_1_the_entity_drawer_reads_only_its_members(cfg, store, monkeypatch):
    """3.1 (P2): the drawer of a NAME entity aggregated the whole spine into
    entities, then filtered (four whole-spine passes, 30.4 s cold on 99M).
    was: statements over rates_by_tin with no member-TIN restriction."""
    T = _two_payer_store(cfg, store)
    c = TestClient(create_app(cfg, store))
    key = c.get("/api/rates", params={"grain": "entity", "q": T[0]}).json()["rows"][0]["unit_id"]
    before = c.get(f"/api/entity/entity/{key}").json()
    spy = _SqlSpy(store, monkeypatch)
    got = TestClient(create_app(cfg, store)).get(f"/api/entity/entity/{key}").json()
    assert got == before and got["rates"]
    spine_stmts = [q for q in spy.sql if "FROM rates_by_tin t" in q]
    assert spine_stmts and all("unnest(?::VARCHAR[])" in q for q in spine_stmts), spine_stmts


def test_3_2_pair_readers_do_not_rescan_the_raw_rows(cfg, store, monkeypatch):
    """3.2 (P2): the city rate map, market sizing, the digest, new-to-network,
    steal-share and the drawer each ran SELECT DISTINCT tin_value, npi FROM
    rates on every call. was: a raw `FROM rates` scan on the second call too."""
    from mrfx.territory import local_rate_map
    _two_payer_store(cfg, store)
    m = {"month": "latest", "state": "MO"}
    first = local_rate_map(store, "97110", m, state="MO")
    spy = _SqlSpy(store, monkeypatch)
    second = local_rate_map(store, "97110", m, state="MO")
    assert first == second
    assert spy.raw_scans() == []


def test_3_3_outreach_rounds_each_value_once_and_names_by_day(cfg, store, monkeypatch):
    """3.3 (P2): half_up ran per cell (11,461,266 calls on the whole 99M
    store); the ZIP's member names carried HHMM. was: 4+ calls per priced
    cell; member names like mrfx_outreach_2026-11-12_0215.csv."""
    import io
    import re
    import zipfile
    import mrfx.outreach as O
    _two_payer_store(cfg, store)
    c = TestClient(create_app(cfg, store))
    calls = {"n": 0}
    real = O._half_up if hasattr(O, "_half_up") else O.half_up

    def counting(*a, **k):
        calls["n"] += 1
        return real(*a, **k)
    monkeypatch.setattr(O, "_half_up" if hasattr(O, "_half_up") else "half_up", counting)
    r = c.get("/api/export/outreach.zip?grain=tin&latest=1&dollar_only=1")
    z = zipfile.ZipFile(io.BytesIO(r.content))
    names = z.namelist()
    assert all(re.search(r"_\d{4}-\d\d-\d\d(_methodology)?\.(csv|txt)$", n) for n in names), names
    rows = list(__import__("csv").reader(io.StringIO(z.read(names[0]).decode("utf-8-sig"))))
    cells = sum(1 for row in rows[1:] for v in row[15:] if v != "")
    assert calls["n"] < cells, (calls["n"], cells)


def test_3_4_the_overview_reads_the_practice_medians(cfg, store, monkeypatch):
    """3.4 (P2): the overview copied the market's spine rows into a temp table
    and recomputed per-practice medians three times. was: CREATE TEMP TABLE
    __overview on every first visit."""
    from mrfx.overview import market_overview
    _two_payer_store(cfg, store)
    spy = _SqlSpy(store, monkeypatch)
    d = market_overview(store, "MO")
    assert not any("__overview" in q for q in spy.sql)
    assert d["payers"] == 2 and d["practices"] == 5
    top = {r["billing_code"]: r for r in d["top_codes"]}
    # per practice, both payers' rates: 97110 at (40,42), (50,52) … -> medians 41, 51, …
    assert top["97110"]["practices"] == 5 and top["97110"]["median"] == 61.0


def test_3_5_new_to_network_validate_stats_and_leads_csv_reuse(cfg, store, monkeypatch):
    """3.5 (P2/P3): new-to-network recomputed on every click; /api/validate read
    every part; /api/stats recounted the spine on every fresh start; leads.csv
    recomputed the screen's list. was: SQL on the 2nd new-to-network call; a
    scan of `rates` in validate; no spine_counts file; compute_leads ran twice."""
    T = _two_payer_store(cfg, store)
    c = TestClient(create_app(cfg, store))
    body = {"market": {"month": "latest"}, "therapy_only": False}
    first = c.post("/api/contracts/new-to-network", json=body).json()
    spy = _SqlSpy(store, monkeypatch)
    assert c.post("/api/contracts/new-to-network", json=body).json() == first
    assert spy.sql == []
    v = c.post("/api/validate", json={"code": "97110", "id": T[0]}).json()
    assert v["rows"] and not spy.raw_scans()
    monkeypatch.undo()
    c.get("/api/stats")
    assert list(store.dir.glob("spine_counts.v1.*.json"))
    store._bump_generation(spine=False)
    lb = {"market": {"month": "latest", "payers": [], "state": "MO", "therapy_only": False},
          "min_codes": 1}
    spy = _SqlSpy(store, monkeypatch)
    screen = c.post("/api/leads", json=lb).json()
    csv = c.post("/api/leads.csv", json=lb)
    assert screen["count"] >= 1 and csv.status_code == 200
    builds = [q for q in spy.sql if "TEMP TABLE _lead_base" in q]
    assert len(builds) == 1, len(builds)


# ---------------------------------------------------------------- group 4


def test_4_1_the_quartile_follows_the_printed_percentile_in_a_tied_market():
    """4.1 (P1, real TX data: p26 printed beside "Q3"). A practice at a tied
    median/P25. was: percentile 33, quartile Q3."""
    from mrfx.visit_market import _position, _stats
    others = [105.44, 105.44, 110.31]
    v = 105.44
    st = _stats(others + [v])
    assert (st["p25"], st["median"]) == (105.44, 105.44)
    pos = _position(v, others, st)
    assert pos["percentile"] == 33 and pos["quartile"] == "Q2"
    from mrfx.visit_market import SUBJECT_NOTE
    assert "read from that printed percentile" in SUBJECT_NOTE


def test_4_2_the_practice_rate_rule_is_written_down_and_unchanged(cfg, store):
    """4.2 (P1, decided: disclose). The Benchmark's "YOU" for one code is the
    median over the practice's base LINES — Payer 01's two POS sets count
    twice: median(46.29, 46.29, 67.15, 46.01, 20.33) = 46.29, not the
    one-per-payer 46.15 — and the methodology now says so. was: the sentence
    was printed nowhere (PRACTICE_RATE_RULE did not exist)."""
    from mrfx.benchmark import PRACTICE_RATE_RULE, practice_base_sql
    T = tins("75000", 1)
    npis = {T[0]: [f"1{T[0]}"]}
    docs = [
        ("p1.json", doc("Payer 01", "2026-06-01", {"97110": {T[0]: [
            price(46.29, pos=("11",)), price(46.29, pos=("11", "22"))]}}, npis)),
        ("p2.json", doc("Payer 02", "2026-06-01", {"97110": {T[0]: 67.15}}, npis)),
        ("p3.json", doc("Payer 03", "2026-06-01", {"97110": {T[0]: 46.01}}, npis)),
        ("p4.json", doc("Payer 04", "2026-06-01", {"97110": {T[0]: 20.33}}, npis)),
    ]
    load(cfg, store, docs)
    store.rebuild_rollups()
    q, p = practice_base_sql(store, {"month": "latest"})
    with store.connect() as con:
        rate = con.execute(f"SELECT rate FROM ({q}) WHERE tin_value = ?", [*p, T[0]]).fetchone()[0]
    assert round(rate, 2) == 46.29
    assert "counts once per set" in PRACTICE_RATE_RULE


def test_4_3_the_three_small_wordings():
    """4.3 (P3). was: the config said "about 4 GB on a 16 GB laptop … about
    1 GB each" (the code gives 6 GB and budgets 2 GB per worker); the guide
    never said a practice is a TIN; the Clear-queue confirm did not mention the
    file a downloader claimed an instant before."""
    from pathlib import Path
    from mrfx.store import auto_pool_gb
    root = Path(__file__).resolve().parents[2]
    conf = (root / "config" / "mrfx.yaml").read_text()
    assert "6 GB on a" in conf and "16 GB laptop" in conf and "2 GB each" in conf
    assert auto_pool_gb(16 * (1 << 30)) == 6 and auto_pool_gb(32 * (1 << 30)) == 12
    gs = (root / "GETTING_STARTED.md").read_text()
    assert "A **practice** here is one tax ID (TIN)" in gs
    js = (root / "mrfx" / "web" / "app.js").read_text()
    assert "picked up in the instant before this click still" in js


