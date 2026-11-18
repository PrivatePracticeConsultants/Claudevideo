"""Known-answer test: hand-built MRFs whose every correct number is worked out
BY HAND below, pushed through the real pipeline (inbox scan -> parser -> store
-> rollups -> HTTP API -> CSV/ZIP export) and checked number by number.

Every other test checks one layer against itself. This one checks the answer
the user actually reads against arithmetic a person can redo on paper — the
question "if I look up NPI X for 97110, is the number right?".

The files (payer "Alpha Health"):

  A  2026-05 (header last_updated_on)
     97110  [X, Y] @ T1   $40 GP, $40 GP+59 (modifiers given as 59,GP)
            [X]    @ T2   $44 GP
            []     @ T3   $50 GP        (TIN-only contract: no NPI listed)
            [X]    @ T1   40 "percentage" (not dollars — never mixed in)
            [Y]    @ T1   $0.01 GP      (a placeholder, not a rate)
     97140  [X]    @ T1   $30 GP+59, and again as 59+GP (same contract twice)
     97530  [X]    @ T1   $500 bundle   (bundled arrangement -> excluded)
     97112  tin.type=npi, tin.value = X  $35 (NPI-in-the-TIN-slot)
     97116  provider_references [1] -> companion refs file: X @ T1  $33
  B  2026-06   97110 [X] @ T1 $42 GP      (June republication: a raise)
  B' 2026-07-named, byte-identical twin of B (an inbox re-drop: recognised
     as the same file and not loaded twice)
  S  2026-05   97110 [Z] @ 078-05-1120 (an SSN-pattern TIN) $45 GP

Raw rows by hand: A = 4+1+1+1+1 (97110) + 2 (97140) + 0 (bundle) + 1 + 1 = 12.
"""

from __future__ import annotations

import csv
import io
import json
import zipfile

import pytest

X, Y, Z = "1111111111", "1222222222", "1333333333"
T1, T2, T3 = "210000001", "220000002", "230000003"
SSN = "078051120"


def _price(rate, mods=("GP",), ntype="negotiated"):
    return {"negotiated_type": ntype, "negotiated_rate": rate, "billing_class": "professional",
            "service_code": ["11"], "billing_code_modifier": list(mods),
            "expiration_date": "9999-12-31"}


def _item(code, rates, arrangement="ffs"):
    return {"negotiation_arrangement": arrangement, "name": code, "billing_code_type": "CPT",
            "billing_code_type_version": "2026", "billing_code": code, "description": code,
            "negotiated_rates": rates}


def _grp(npis, tin, ttype="ein"):
    return {"npi": npis, "tin": {"type": ttype, "value": tin}}


def _hdr(month_day, **kw):
    return {"reporting_entity_name": "Alpha Health", "reporting_entity_type": "issuer",
            "last_updated_on": month_day, "version": "2.0.0", "provider_references": [], **kw}


FILE_A = _hdr("2026-05-01", in_network=[
    _item("97110", [
        {"provider_groups": [_grp([X, Y], T1)], "negotiated_prices": [_price(40.0), _price(40.0, ("59", "GP"))]},
        {"provider_groups": [_grp([X], T2)], "negotiated_prices": [_price(44.0)]},
        {"provider_groups": [_grp([], T3)], "negotiated_prices": [_price(50.0)]},
        {"provider_groups": [_grp([X], T1)], "negotiated_prices": [_price(40.0, ntype="percentage")]},
        {"provider_groups": [_grp([Y], T1)], "negotiated_prices": [_price(0.01)]},
    ]),
    _item("97140", [
        {"provider_groups": [_grp([X], T1)], "negotiated_prices": [_price(30.0, ("GP", "59"))]},
        {"provider_groups": [_grp([X], T1)], "negotiated_prices": [_price(30.0, ("59", "GP"))]},
    ]),
    _item("97530", [{"provider_groups": [_grp([X], T1)], "negotiated_prices": [_price(500.0)]}],
          arrangement="bundle"),
    _item("97112", [{"provider_groups": [_grp([X], X, "npi")], "negotiated_prices": [_price(35.0)]}]),
    _item("97116", [{"provider_references": [1], "negotiated_prices": [_price(33.0)]}]),
])
REFS = _hdr("2026-05-01")
REFS["provider_references"] = [{"provider_group_id": 1, "provider_groups": [_grp([X], T1)]}]
FILE_B = _hdr("2026-06-01", in_network=[
    _item("97110", [{"provider_groups": [_grp([X], T1)], "negotiated_prices": [_price(42.0)]}])])
FILE_S = _hdr("2026-05-01", in_network=[
    _item("97110", [{"provider_groups": [_grp([Z], SSN)], "negotiated_prices": [_price(45.0)]}])])


@pytest.fixture
def ka(cfg, store):
    from fastapi.testclient import TestClient

    from mrfx.api import create_app
    from mrfx.ingest import scan_inbox

    (cfg.inbox_dir / "alpha_refs_2026-05.json").write_text(json.dumps(REFS))
    (cfg.inbox_dir / "alpha_2026-05_in-network.json").write_text(json.dumps(FILE_A))
    (cfg.inbox_dir / "alpha_2026-06_in-network.json").write_text(json.dumps(FILE_B))
    (cfg.inbox_dir / "alpha_2026-07_in-network.json").write_text(json.dumps(FILE_B))
    (cfg.inbox_dir / "alpha_ssn_2026-05_in-network.json").write_text(json.dumps(FILE_S))
    results = {r["file"]: r for r in scan_inbox(cfg, store)}
    return results, TestClient(create_app(cfg, store))


def _key(r):
    return (r["tin_value"], r["billing_code"], r["modifier_set"], r["file_month"], r["negotiated_rate"])


def test_raw_row_counts_match_hand_count(ka):
    results, _ = ka
    assert results["alpha_2026-05_in-network.json"]["rows"] == 12
    assert results["alpha_2026-06_in-network.json"]["rows"] == 1
    # the byte-identical twin is the same file under another name: it is not
    # loaded twice (review 11 item 1.1 — before, it was, and every raw count
    # and source_count carried the copy)
    assert results["alpha_2026-07_in-network.json"]["status"] == "skipped"
    assert results["alpha_2026-07_in-network.json"]["duplicate_of"] == "alpha_2026-06_in-network.json"
    assert results["alpha_ssn_2026-05_in-network.json"]["rows"] == 1


def test_npi_lookup_is_exactly_the_hand_answer(ka):
    _, c = ka
    rows = c.get(f"/api/rates?grain=npi&q={X}&limit=100").json()["rows"]
    got = {_key(r): r for r in rows}
    assert set(got) == {
        (T2, "97110", "GP", "2026-05", 44.0),
        (T1, "97110", "GP", "2026-05", 40.0),
        (T1, "97110", "59|GP", "2026-05", 40.0),   # modifiers sorted
        (T1, "97110", "GP", "2026-06", 42.0),      # the June raise, its own month
        (T1, "97140", "59|GP", "2026-05", 30.0),   # GP,59 and 59,GP -> ONE row
        (X, "97112", "GP", "2026-05", 35.0),       # NPI-in-the-TIN-slot
        (T1, "97116", "GP", "2026-05", 33.0),      # resolved via provider_references
    }   # no bundle $500, no percentage 40, no $0.01
    assert got[(X, "97112", "GP", "2026-05", 35.0)]["tin_is_really_npi"] is True
    assert got[(T1, "97110", "GP", "2026-06", 42.0)]["source_count"] == 1   # B; its twin is a copy
    assert all(r["payer"] == "Alpha Health" for r in rows)


def test_percentage_shows_only_with_dollar_only_off_and_never_blends(ka):
    _, c = ka
    on = c.get(f"/api/rates?grain=npi&q={X}&limit=100").json()["rows"]
    off = c.get(f"/api/rates?grain=npi&q={X}&dollar_only=0&limit=100").json()["rows"]
    assert len(off) == len(on) + 1
    pct = [r for r in off if not r["is_dollar_rate"]]
    assert [(r["tin_value"], r["negotiated_rate"], r["negotiated_type"]) for r in pct] == \
        [(T1, 40.0, "percentage")]


def test_placeholder_is_never_a_rate(ka):
    _, c = ka
    rows = c.get(f"/api/rates?grain=npi&q={Y}&limit=100").json()["rows"]
    assert {_key(r) for r in rows} == {(T1, "97110", "GP", "2026-05", 40.0),
                                      (T1, "97110", "59|GP", "2026-05", 40.0)}


def test_tin_grain_rollup(ka):
    _, c = ka
    rows = c.get("/api/rates?grain=tin&limit=100").json()["rows"]
    by = {(r["unit_id"], r["billing_code"], r["modifier_set"], r["file_month"]): r for r in rows}
    t1 = by[(T1, "97110", "GP", "2026-05")]
    # X and Y both $40; Y's $0.01 placeholder is excluded from the median...
    assert (t1["negotiated_rate"], t1["npi_count"]) == (40.0, 2)
    # ...but min/variants describe every PUBLISHED value, placeholder included
    # (documented: the rate column is the clean number, min/max the raw spread)
    assert (t1["rate_min"], t1["rate_max"], t1["rate_variants"]) == (0.01, 40.0, 2)
    t3 = by[(T3, "97110", "GP", "2026-05")]
    assert (t3["negotiated_rate"], t3["npi_count"]) == (50.0, 0)   # TIN-only contract counts at TIN grain
    assert by[(T1, "97110", "GP", "2026-06")]["negotiated_rate"] == 42.0
    assert (T1, "97530", "GP", "2026-05") not in by                # bundle excluded


def test_code_view_uses_newest_month_per_contract(ka):
    """Regression: with no month pinned the code view pooled May $40 and June
    $42 into a '$41' median nobody published, and summed T1's NPIs once per
    month (2 NPIs shown as 3)."""
    _, c = ka
    d = c.get("/api/code/97110?grain=tin").json()
    assert d["as_of"] == "latest"
    ranked = [(r["unit_id"], r["modifier_set"], r["median_rate"], r["npi_count"], r["n"])
              for r in d["ranked"]]
    assert ranked == [
        (T3, "GP", 50.0, 0, 1),
        (SSN, "GP", 45.0, 1, 1),           # unit_id: raw drill-down key (UI masks it)
        (T2, "GP", 44.0, 1, 1),
        (T1, "GP", 42.0, 1, 1),            # June supersedes May for T1
    ]
    pr = d["payer_rank"][0]
    assert (pr["median_rate"], pr["n_entities"], pr["min_rate"], pr["max_rate"]) == (44.5, 4, 42.0, 50.0)
    # pinning May shows May's book, with T1's real roster of 2
    may = c.get("/api/code/97110?grain=tin&month=2026-05").json()
    t1 = {(r["unit_id"], r["modifier_set"]): r for r in may["ranked"]}
    assert (t1[(T1, "GP")]["median_rate"], t1[(T1, "GP")]["npi_count"]) == (40.0, 2)
    assert t1[(T1, "59|GP")]["median_rate"] == 40.0


def test_trend_is_per_month(ka):
    _, c = ka
    rows = c.get(f"/api/trend?grain=npi&q={X}&cpt=97110").json()["rows"]
    # the payer table's basis (review 10 item 3.1): base modifiers, so May's
    # 59|GP line is out; X's base lines are 40 (T1) and 44 (T2) -> 42. The raw
    # line median (40, 40, 44) read 40 here and 42 in the payer table. June: 42
    assert [(r["file_month"], r["median_rate"]) for r in rows] == [("2026-05", 42.0), ("2026-06", 42.0)]


def test_months_listed(ka):
    _, c = ka
    assert c.get("/api/months").json()["months"] == ["2026-06", "2026-05"]


def _csv_rows(raw: bytes) -> list[dict]:
    return list(csv.DictReader(io.StringIO(raw.decode("utf-8-sig"))))


@pytest.mark.parametrize("grain", ["npi", "tin", "entity"])
def test_export_equals_screen(ka, grain):
    """What the user downloads is exactly what the table showed them."""
    _, c = ka
    qs = f"grain={grain}&sort=negotiated_rate&dir=desc"
    screen = c.get(f"/api/rates?{qs}&limit=1000").json()["rows"]
    exported = _csv_rows(c.get(f"/api/export.csv?{qs}").content)
    for _ in range(5):   # the same question twice is the same answer (invariant 7)
        assert _csv_rows(c.get(f"/api/export.csv?{qs}").content) == exported
    assert len(exported) == len(screen) > 0
    for s, e in zip(screen, exported):
        assert float(e["negotiated_rate"]) == s["negotiated_rate"]
        assert e["billing_code"] == s["billing_code"]
        assert e["file_month"] == s["file_month"]
        assert e["payer"] == s["payer"]
        assert e["modifiers"] == s["modifier_set"].replace("|", ";")
    z = zipfile.ZipFile(io.BytesIO(c.get(f"/api/export.zip?{qs}").content))
    names = z.namelist()
    assert any(n.endswith("_methodology.txt") for n in names)
    zcsv = [n for n in names if n.endswith(".csv")][0]
    assert _csv_rows(z.read(zcsv)) == exported
    # the sidecar describes the filter that was ACTUALLY applied
    method = z.read([n for n in names if n.endswith("_methodology.txt")][0]).decode()
    assert f"Grain: {grain}" in method
    # filters in words (review 10 item 3.13 — it was raw JSON)
    assert "ALL months pooled" in method and "dollar rates only: yes" in method
    z2 = zipfile.ZipFile(io.BytesIO(c.get(f"/api/export.zip?{qs}&month=2026-05&dollar_only=0").content))
    m2 = z2.read([n for n in z2.namelist() if n.endswith("_methodology.txt")][0]).decode()
    assert "As-of month: 2026-05 (only this month's rates)" in m2
    assert "dollar rates only: no" in m2


def _leaks(obj, path=""):
    """Every place a raw SSN-pattern TIN sits in a JSON body, EXCEPT the two
    machine keys the UI never renders: unit_id (the drill-down key —
    api._mask_row_tins documents it; the dashboard shows maskTin(unit_id) /
    display_name) and website_tins (sent back verbatim when saving a site)."""
    if isinstance(obj, dict):
        return [p for k, v in obj.items() if k not in ("unit_id", "website_tins")
                for p in _leaks(v, f"{path}.{k}")]
    if isinstance(obj, list):
        return [p for i, v in enumerate(obj) for p in _leaks(v, f"{path}[{i}]")]
    return [path] if isinstance(obj, str) and SSN in obj else []


def test_ssn_pattern_tin_is_masked_wherever_it_is_shown_or_exported(ka):
    _, c = ka
    shown = ["/api/rates?grain=tin&limit=1000", "/api/rates?grain=entity&limit=1000",
             f"/api/rates?grain=npi&q={Z}&limit=1000", "/api/rates?grain=tin&q=078&limit=1000",
             "/api/code/97110?grain=tin", "/api/code/97110?grain=npi",
             "/api/code/97110?grain=entity", f"/api/entity/npi/{Z}", f"/api/entity/tin/{SSN}",
             "/api/summary?grain=tin", "/api/files", "/api/overview", "/api/trend?grain=tin"]
    seen_masked = False
    for u in shown:
        r = c.get(u)
        assert r.status_code == 200, u
        assert _leaks(r.json()) == [], f"raw SSN-pattern TIN in a displayed field of {u}"
        seen_masked |= b"MASKED-SSN" in r.content
    # a FILE leaves the machine: there not even the drill-down key may be raw
    for u in ["/api/export.csv?grain=tin", "/api/export.csv?grain=npi",
              "/api/export.csv?grain=entity", "/api/export.zip?grain=tin"]:
        r = c.get(u)
        assert r.status_code == 200, u
        body = r.content
        if ".zip" in u:
            zf = zipfile.ZipFile(io.BytesIO(body))
            body = b"".join(zf.read(n) for n in zf.namelist())
        assert SSN.encode() not in body, f"raw SSN-pattern TIN in exported file {u}"
        seen_masked |= b"MASKED-SSN" in body
    assert seen_masked   # the row is there — shown masked, not silently dropped


def test_python_and_sql_masking_agree_on_real_ssn_shapes():
    """ONE rule (round 6 P3): a 9-digit number whose prefix is not a valid EIN
    campus prefix is masked on screen (SQL) AND in every export (Python). The
    Python rule also demanded SSN structure, so 96x/97x (ITIN-shaped) and
    000-area numbers showed masked on screen and raw in the CSVs."""
    import duckdb

    from mrfx.api import _mask_tin_sql
    from mrfx.store import looks_like_ssn

    samples = [f"{p:02d}{rest}" for p in range(100) for rest in ("0511200", "1234567", "0000001")]
    con = duckdb.connect()
    for s in samples:
        sql_masked = con.execute(f"SELECT {_mask_tin_sql('t')} FROM (SELECT ? AS t)",
                                 [s]).fetchone()[0] != s
        assert sql_masked == looks_like_ssn(s), s
    # the shapes that used to disagree
    for s in ("961234567", "971234567", "000123456", "070000000"):
        assert looks_like_ssn(s), s
    assert not looks_like_ssn("431111111") and not looks_like_ssn("1712345678")


def test_code_comparison_export_matches_its_screen(ka):
    """The Code comparison page ranks each contract's NEWEST month; its
    'Export + methodology' button (latest=1) must export those same rows —
    not every month's — and say so in the sidecar."""
    _, c = ka
    screen = c.get("/api/code/97110?grain=tin").json()["ranked"]
    z = zipfile.ZipFile(io.BytesIO(c.get(
        "/api/export.zip?cpt=97110&grain=tin&view=code_97110&latest=1").content))
    names = z.namelist()
    rows = _csv_rows(z.read([n for n in names if n.endswith(".csv")][0]))
    assert sorted((r["payer"], r["modifiers"], float(r["negotiated_rate"]), r["file_month"]) for r in rows) == \
        sorted([("Alpha Health", "GP", 50.0, "2026-05"), ("Alpha Health", "GP", 45.0, "2026-05"),
                ("Alpha Health", "GP", 44.0, "2026-05"), ("Alpha Health", "GP", 42.0, "2026-06")])
    assert sum(r["n"] for r in screen) == len(rows)
    method = z.read([n for n in names if n.endswith("_methodology.txt")][0]).decode()
    assert "As-of month: latest" in method
    # the explorer (no latest flag) still shows every month, as labelled
    assert len(c.get("/api/rates?grain=tin&cpt=97110&limit=100").json()["rows"]) > len(rows)


def test_big_store_npi_and_entity_views_need_a_narrowing_filter(ka, monkeypatch):
    """At 306M rows the unfiltered NPI/entity views (and an NPI-view name
    search) ran out of temp disk after ~30 s; an exact NPI search or payer+code
    took 0.6-3 s. On a big store the API asks for the fast filter instead."""
    import mrfx.api as api_mod

    _, c = ka
    monkeypatch.setattr(api_mod, "NARROW_REQUIRED_ROWS", 1)   # this tiny store counts as "big"
    for u in ["/api/rates?grain=npi", "/api/rates?grain=npi&q=Alpha",
              "/api/rates?grain=npi&cpt=97110", "/api/rates?grain=entity&q=210000001",
              "/api/summary?grain=npi", "/api/export.csv?grain=entity"]:
        r = c.get(u)
        assert r.status_code == 422, u
        assert "narrower" in r.json()["detail"] or "BOTH a payer and a code" in r.json()["detail"]
    for u in [f"/api/rates?grain=npi&q={X}", f"/api/rates?grain=npi&q={T1}",
              "/api/rates?grain=npi&payer=Alpha%20Health&cpt=97110",
              "/api/rates?grain=entity&payer=Alpha%20Health&cpt=97110",
              "/api/rates?grain=tin", "/api/rates?grain=tin&q=Alpha"]:
        assert c.get(u).status_code == 200, u
    # the NPI search answers exactly as before (the guard and the exact-match
    # rewrite change speed, not results)
    rows = c.get(f"/api/rates?grain=npi&q={X}&limit=100").json()["rows"]
    assert len(rows) == 7 and all(r["unit_id"] == X for r in rows)
    by_tin = c.get(f"/api/rates?grain=npi&q={T1}&limit=100").json()["rows"]
    assert {r["unit_id"] for r in by_tin} == {X, Y} and all(r["tin_value"] == T1 for r in by_tin)


def test_tin_search_works_at_every_grain(ka):
    _, c = ka
    for grain in ("npi", "tin", "entity"):
        rows = c.get(f"/api/rates?grain={grain}&q={T2}&limit=100").json()["rows"]
        assert rows and all(T2 in r["tin_value"] for r in rows), grain
    # a partial number still substring-matches while typing
    assert c.get("/api/rates?grain=tin&q=22000&limit=100").json()["rows"]


def test_latest_is_decided_before_row_filters(ka):
    """Round 3 P0.2. T1's current 97110 rate is June's GP $42; May's GP $40 and
    59|GP $40 are superseded. A rate filter <= $41 must therefore show NO T1
    line (its current rate is $42) — not resurrect May's $40s. By hand, under
    rate_max=41 every latest line is above $41 (T1 42, T2 44, SSN 45, T3 50):
    the ranked list is empty. Same for a modifier filter: 59|GP exists only in
    May, so under latest T1 has no 59 line at all."""
    _, c = ka
    assert c.get("/api/code/97110?grain=tin&rate_max=41").json()["ranked"] == []
    assert c.get("/api/rates?grain=tin&cpt=97110&latest=1&rate_max=41&limit=100").json()["rows"] == []
    assert c.get("/api/rates?grain=npi&q=1111111111&cpt=97110&latest=1&mod_has=59&limit=100"
                 ).json()["rows"] == []
    # without latest (all months pooled) May's $40 rows are legitimately there
    pooled = c.get("/api/rates?grain=tin&cpt=97110&rate_max=41&limit=100").json()["rows"]
    assert sorted((r["unit_id"], r["modifier_set"], r["file_month"], r["negotiated_rate"])
                  for r in pooled) == [(T1, "59|GP", "2026-05", 40.0), (T1, "GP", "2026-05", 40.0)]


def test_latest_npi_under_two_tins_keeps_both_contracts(ka):
    """X bills 97110 under T1 (newest: June $42) and T2 (only May: $44). Two
    contracts — T1's June file must not supersede T2's May line. By hand:
    latest NPI view for X / 97110 = {(T1, June, 42), (T2, May, 44)}."""
    _, c = ka
    rows = c.get(f"/api/rates?grain=npi&q={X}&cpt=97110&latest=1&limit=100").json()["rows"]
    assert sorted((r["tin_value"], r["file_month"], r["negotiated_rate"]) for r in rows) == [
        (T1, "2026-06", 42.0), (T2, "2026-05", 44.0)]


def test_code_and_trend_views_refuse_whole_store_entity_scans_on_a_big_store(ka, monkeypatch):
    """Round 3 P0.2: /api/code and /api/trend skipped the big-store guard, so
    ?grain=entity on a 300M-row store ran the whole-store aggregation that
    ends in a temp-disk 500. Now they answer like the table: a plain 422."""
    _, c = ka
    monkeypatch.setattr("mrfx.api.NARROW_REQUIRED_ROWS", 0)
    for u in ("/api/code/97110?grain=entity", "/api/code/97110?grain=npi",
              "/api/trend?grain=entity", "/api/trend?grain=npi&cpt=97110"):
        r = c.get(u)
        assert r.status_code == 422 and "large" in r.json()["detail"], u
    # a narrowed question still answers
    assert c.get(f"/api/trend?grain=npi&q={X}&cpt=97110").status_code == 200
    assert c.get("/api/code/97110?grain=tin").status_code == 200
