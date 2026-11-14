"""Case 2b — the splice with NO gzip trailer to catch it: a plain .json whose
content changes (two rates) between the first attempt and the resume. The
server has no validator (or ignores If-Range). v1 and v2 are the same size.
Expected (honest): the row restarts or fails. Observed: ?"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _harness import Srv, mk_cfg, quiet_logs, row, q, sha  # noqa: E402

from mrfx.fetch import add_urls, filename_for, run_queue  # noqa: E402


def plain_doc(rate_a: float, rate_b: float, pad_kb: int = 900) -> bytes:
    """Rate A early in the file, ~pad_kb of filler, rate B late in the file."""
    def grp(i, r):
        return {"provider_groups": [{"npi": [1000000000 + i], "tin": {"type": "ein", "value": f"43{i:07d}"}}],
                "negotiated_prices": [{"negotiated_type": "negotiated", "negotiated_rate": r,
                                       "billing_class": "professional", "service_code": ["11"],
                                       "billing_code_modifier": ["GP"]}]}
    d = {"reporting_entity_name": "Splice Health", "reporting_entity_type": "issuer",
         "last_updated_on": "2026-06-01", "version": "2.0.0",
         "in_network": [
             {"negotiation_arrangement": "ffs", "billing_code_type": "CPT", "billing_code_type_version": "2026",
              "billing_code": "97110", "negotiated_rates": [grp(i, rate_a) for i in range(10)]},
             {"negotiation_arrangement": "ffs", "billing_code_type": "CPT", "billing_code_type_version": "2026",
              "billing_code": "99991", "negotiated_rates": [], "description": "x" * (pad_kb * 1024)},
             {"negotiation_arrangement": "ffs", "billing_code_type": "CPT", "billing_code_type_version": "2026",
              "billing_code": "97140", "negotiated_rates": [grp(i, rate_b) for i in range(10)]},
         ]}
    return json.dumps(d).encode()


if __name__ == "__main__":
    quiet_logs()
    v1 = plain_doc(40.0, 40.0)
    v2 = plain_doc(55.0, 55.0)
    assert len(v1) == len(v2)
    cfg, store = mk_cfg("c02b_json_splice")
    srv = Srv({"/rates.json": v1}, {"/rates.json": {"cut_at": [300 << 10]}})

    def swap(h, body, rng, nth):
        if nth >= 2:
            srv.files["/rates.json"] = v2
        return False
    srv.behave["/rates.json"] = swap
    url = srv.base + "/rates.json"
    t0 = time.time()
    try:
        add_urls(store, [url])
        run_queue(cfg, store, drain=True)
    finally:
        srv.close()
    r = row(store, url)
    print(f"[json_splice] {time.time() - t0:.1f}s | row status={r['status']} kind={r['kind']} rows={r['rows_emitted']} "
          f"content_sha==v1 {r['content_sha'] == sha(v1)} ==v2 {r['content_sha'] == sha(v2)}")
    print(f"    requests: {srv.describe('/rates.json')}")
    print("    If-Range sent on the resume:", srv.hits('/rates.json')[1]['req_headers'].get('If-Range'))
    print("    rates in store by code:", q(store, "SELECT billing_code, negotiated_rate, count(*) FROM rates "
                                             "GROUP BY 1,2 ORDER BY 1,2"))
    print("    truth v1: 97110=40 97140=40 ; truth v2 (what the server serves now): 97110=55 97140=55")
    dest = cfg.downloads_dir / filename_for(url)
    print("    download kept on disk:", dest.exists())
