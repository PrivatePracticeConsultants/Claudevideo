"""Scenario 10: honesty. A 0-row file stays 0 and is not retried; allowed-amounts / TOC / blob
listing dropped in the inbox; a file whose payer name is missing; a file the fast path declines
(classic fallback) gives the same rows; `mrfx fastcheck` on 3 real files."""
import subprocess, sys, shutil
from h import *
from mrfx.ingest import scan_inbox, ingest_file

REAL = Path("/tmp/claude-0/-home-user-Claudevideo/4633d594-0139-5f8e-a935-ae9c76b72000/scratchpad")

if __name__ == "__main__":
    cfg = mkcfg("s10"); st = Store(cfg.store_dir)
    print("== 0-row file (no target codes) across 3 scans")
    put(cfg, "acme_none_2026-05.json", {**HDR, "in_network": [item("99213", groups=[grp(["1234567893"], "123456789")])]})
    for i in range(3):
        print("  scan", i, scan_inbox(cfg, st))
    print("  row:", [(f, s, n, (e or '')[:60]) for f, s, _, n, e in files_rows(st)], "| where:", where_is(cfg, "acme_none_2026-05.json"))
    print("  qa messages:", q(st, "SELECT json_extract_string(qa, '$.messages') FROM files"))

    print("== payer name missing")
    nopayer = {"version": "1.0.0", "last_updated_on": "2026-05-01", "in_network": [item("97110", groups=[grp(["1111111111"], "111111111")])]}
    put(cfg, "nopayer_2026-05.json", nopayer); print("  scan:", scan_inbox(cfg, st))
    print("  row:", [(f, s, n, (e or '')[:80]) for f, s, _, n, e in files_rows(st) if f.startswith("nopayer")])
    print("  payer on rows:", q(st, "SELECT payer, count(*) FROM rates WHERE source_file = 'nopayer_2026-05.json' GROUP BY 1"))
    print("  files.payer:", q(st, "SELECT payer FROM files WHERE filename = 'nopayer_2026-05.json'"))
    # with a companion needed and no payer: _companion_refs returns {} -> groups skipped?
    nop2 = {"version": "1.0.0", "last_updated_on": "2026-05-01", "in_network": [item("97110", refs=[1])]}
    put(cfg, "nopayer_refs_2026-05.json", nop2); scan_inbox(cfg, st)
    print("  no-payer file citing refs:", [(f, s, n, (e or '')[:80]) for f, s, _, n, e in files_rows(st) if f.startswith("nopayer_refs")],
          q(st, "SELECT ref_groups_skipped FROM files WHERE filename='nopayer_refs_2026-05.json'"))

    print("== TOC / allowed / blob listing in the inbox")
    put(cfg, "toc.json", {**HDR, "reporting_structure": [{"in_network_files": [{"location": "https://x/a.json"}]}]})
    put(cfg, "allowed.json", {**HDR, "out_of_network": []})
    put(cfg, "blobs.json", {"blobs": [{"name": "a.json.gz", "downloadUrl": "https://x/a.json.gz"}]})
    scan_inbox(cfg, st)
    for f, s, ft, n, e in files_rows(st):
        if f in ("toc.json", "allowed.json", "blobs.json"):
            print(f"  {f:14} {s:12} {ft:15} rows={n} where={where_is(cfg, f):10} {(e or '')[:90]}")

    print("== fast path declines -> classic fallback gives the same rows (fast_scan on vs off)")
    weird = {**HDR, "in_network": [item("97110", groups=[grp(["1111111111"], "111111111")], rate=33.0),
                                   item(97112, groups=[grp(["2222222222"], "222222222")], rate=44.0),   # numeric code
                                   item("97140", groups=[grp(["3333333333"], "333333333")], prices=[
                                       {"negotiated_type": "negotiated", "negotiated_rate": 55.0, "billing_class": "professional",
                                        "service_code": ["11", "11", "02"], "billing_code_modifier": ["GP", "CQ", "GP"]}])]}
    outs = {}
    for fast in (True, False):
        c2 = mkcfg(f"s10_fast{int(fast)}", fast=fast); s2 = Store(c2.store_dir)
        ingest_file(c2, s2, put(c2, "w_2026-05.json", weird))
        outs[fast] = q(s2, "SELECT billing_code, npi, tin_value, negotiated_rate, billing_code_modifier, service_code FROM rates ORDER BY ALL")
    print("  fast:", outs[True]); print("  classic:", outs[False]); print("  identical:", outs[True] == outs[False])

    print("== mrfx fastcheck on 3 real files")
    picks = [REAL / "real2/nd_hmhs_2026-10-27_000005200_in-network-rates_01_of_10.json.gz",
             REAL / "real/uhc_2026-11-01_UnitedHealthcare-Insurance-Company-of-New-York_Third-Party-Administrator_Empire_CSP-377-A351_in-network-rates.json.gz",
             REAL / "real/bcbsms_2026-10_320_33B0_in-network-rates_01_of_10.json.gz"]
    tmp = Path("/tmp/claude-0/-home-user-Claudevideo/4633d594-0139-5f8e-a935-ae9c76b72000/scratchpad/audit14/regress/r11tmp/ingest/runs/s10_real"); shutil.rmtree(tmp, ignore_errors=True); tmp.mkdir(parents=True)
    for p in picks:
        shutil.copy(p, tmp / p.name)
    cfgy = tmp / "mrfx.yaml"
    cfgy.write_text(f"inbox_dir: {tmp}/inbox\nprocessed_dir: {tmp}/processed\nfailed_dir: {tmp}/failed\nstore_dir: {tmp}/store\n"
                    f"downloads_dir: {tmp}/downloads\nentity_map_path: {tmp}/entity_map.yaml\nenrichment:\n  mode: off\n")
    r = subprocess.run(["/home/user/Claudevideo/.venv/bin/mrfx", "--config", str(cfgy), "fastcheck", *[str(tmp / p.name) for p in picks]],
                       capture_output=True, text=True, timeout=1800)
    print(r.stdout[-3000:]); print("  stderr tail:", r.stderr[-800:])
    shutil.rmtree(tmp, ignore_errors=True)
