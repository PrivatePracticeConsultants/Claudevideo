"""Payer file shapes found in the round-2 source audit that the queue could
not read (round 3, P2.5): indexes published as several JSON objects back to
back (UCare, 1199SEIU, Network Health), a ZIP inside the ZIP (CareSource),
and link pages far longer than max_toc_files (Allied: 32,397 indexes)."""

from __future__ import annotations

import io
import json
import zipfile

from mrfx.fetch import expand_toc, expand_toc_with_plans, extract_links_from_text


def _toc(i):
    return {"reporting_entity_name": "UCare", "reporting_structure": [{
        "reporting_plans": [{"plan_name": f"Plan {i}", "plan_id_type": "EIN", "plan_id": str(i)}],
        "in_network_files": [{"location": f"https://x.example/f{i}.json.gz"}]}]}


def test_an_index_of_several_json_objects_expands_all_of_them(tmp_path):
    p = tmp_path / "index.json"
    p.write_text(json.dumps(_toc(1)) + "\n" + json.dumps(_toc(2)) + json.dumps(_toc(3)))
    urls, capped = expand_toc(p, 100)
    assert urls == [f"https://x.example/f{i}.json.gz" for i in (1, 2, 3)] and not capped
    with_plans = expand_toc_with_plans(p, 100)
    urls2 = with_plans[0] if isinstance(with_plans, tuple) else with_plans
    assert sorted(urls2) == sorted(urls)


def test_a_zip_inside_a_zip_is_read(cfg, store):
    from mrfx.ingest import scan_inbox
    doc = {"reporting_entity_name": "CareCo", "reporting_entity_type": "issuer",
           "last_updated_on": "2026-05-01", "version": "2.0.0", "provider_references": [],
           "in_network": [{"negotiation_arrangement": "ffs", "name": "x",
                           "billing_code_type": "CPT", "billing_code_type_version": "2026",
                           "billing_code": "97110", "description": "x",
                           "negotiated_rates": [{
                               "provider_groups": [{"npi": ["1840000001"],
                                                    "tin": {"type": "ein", "value": "840000001"}}],
                               "negotiated_prices": [{"negotiated_type": "negotiated",
                                                      "negotiated_rate": 41.0,
                                                      "billing_class": "professional",
                                                      "expiration_date": "9999-12-31"}]}]}]}
    inner = io.BytesIO()
    with zipfile.ZipFile(inner, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("careco_in-network-rates.json", json.dumps(doc))
    with zipfile.ZipFile(cfg.inbox_dir / "careco_2026-05.zip", "w") as z:
        z.writestr("careco_2026-05.zip", inner.getvalue())
        z.writestr("readme.txt", "nested")
    res = {r["file"]: r for r in scan_inbox(cfg, store)}
    assert res["careco_2026-05.zip"]["status"] == "done" and res["careco_2026-05.zip"]["rows"] == 1


def test_the_link_cap_keeps_rate_files_and_indexes_first():
    page = "".join(f'<a href="https://x.example/group{i:05d}_allowed-amounts.json">a</a>'
                   for i in range(50))
    page += "".join(f'<a href="https://x.example/misc{i}.json">m</a>' for i in range(10))
    page += "".join(f'<a href="https://x.example/group{i:05d}_in-network-rates.json.gz">r</a>'
                    for i in range(30))
    page += '<a href="https://x.example/plan_index.json">i</a>'
    got = extract_links_from_text(page, "https://x.example/", 31)
    assert len(got) == 31
    assert all("in-network" in u or "index" in u for u in got)        # 30 rate files + the index
    assert got[:2] == ["https://x.example/group00000_in-network-rates.json.gz",
                       "https://x.example/group00001_in-network-rates.json.gz"]   # page order kept
