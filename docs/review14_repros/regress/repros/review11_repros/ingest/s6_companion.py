"""Scenario 6: companion provider-reference files on the INBOX path.
A. reference first, then the rate file  B. rate file first, reference later (is it re-read?)
C. never  D. an EMPTY reference file  E. a reference file whose entries are all remote (location)
F. a rate file re-dropped while its reference stays  G. memory growth: a rate file with many
ref groups citing a companion (peak RSS), on the small single-pass path."""
import gc
from h import *
from mrfx.ingest import scan_inbox, ingest_file

RATE = {**HDR, "in_network": [item("97110", refs=[1, 2], rate=41.0), item("97112", refs=[3], rate=12.5)]}
REF = {**HDR, "provider_references": [{"provider_group_id": 1, "provider_groups": [grp(["9999999999"], "999999999")]},
                                       {"provider_group_id": 2, "provider_groups": [grp(["2222222222"], "222222222")]},
                                       {"provider_group_id": 3, "provider_groups": [grp(["3333333333"], "333333333")]}]}


def show(store, label):
    print(f"  [{label}] files:", [(f, s, ft, n, (e or '')[:70]) for f, s, ft, n, e in files_rows(store)])
    print(f"  [{label}] rates:", rate_counts(store), "| refs stored:", q(store, "SELECT source_file, count(*) FROM provider_refs GROUP BY 1"))
    print(f"  [{label}] skipped/qa:", q(store, "SELECT filename, ref_groups_skipped, json_extract_string(qa,'$.missing_ref_ids'), "
                                              "json_extract_string(qa,'$.remote_ref_groups') FROM files WHERE file_type='in_network'"))


if __name__ == "__main__":
    print("== A. reference first")
    cfg = mkcfg("s6a"); st = Store(cfg.store_dir)
    put(cfg, "acme_refs_2026-05.json", REF); put(cfg, "acme_rates_2026-05.json", RATE)
    print("  scan:", [(r['file'], r['status'], r.get('rows', r.get('refs'))) for r in scan_inbox(cfg, st)])
    show(st, "A")

    print("== B. rate file first, reference in a LATER scan")
    cfg = mkcfg("s6b"); st = Store(cfg.store_dir)
    put(cfg, "acme_rates_2026-05.json", RATE)
    print("  scan1:", [(r['file'], r['status'], r.get('rows')) for r in scan_inbox(cfg, st)])
    show(st, "B1")
    put(cfg, "acme_refs_2026-05.json", REF)
    print("  scan2:", [(r['file'], r['status'], r.get('rows', r.get('refs'))) for r in scan_inbox(cfg, st)])
    show(st, "B2 (expected rates 3, skipped 0)")
    print("  scan3 (nothing new):", scan_inbox(cfg, st), "| rates:", rate_counts(st))

    print("== B'. same, but the raw copy of the rate file is GONE (move_processed=False and deleted)")
    cfg = mkcfg("s6b2", move_processed=False); st = Store(cfg.store_dir)
    p = put(cfg, "acme_rates_2026-05.json", RATE); scan_inbox(cfg, st); p.unlink()
    put(cfg, "acme_refs_2026-05.json", REF); scan_inbox(cfg, st)
    show(st, "B' (raw gone: should say so, not silently stay at 0)")

    print("== C. never (reference never arrives)")
    cfg = mkcfg("s6c"); st = Store(cfg.store_dir)
    put(cfg, "acme_rates_2026-05.json", RATE); scan_inbox(cfg, st); scan_inbox(cfg, st)
    show(st, "C")

    print("== D. empty reference file (provider_references: [])")
    cfg = mkcfg("s6d"); st = Store(cfg.store_dir)
    put(cfg, "acme_refs_2026-05.json", {**HDR, "provider_references": []}); put(cfg, "acme_rates_2026-05.json", RATE)
    print("  scan:", [(r['file'], r['status'], r.get('rows', r.get('refs')), r.get('error')) for r in scan_inbox(cfg, st)])
    show(st, "D")

    print("== E. reference file with only remote (location) entries")
    cfg = mkcfg("s6e"); st = Store(cfg.store_dir)
    put(cfg, "acme_refs_2026-05.json", {**HDR, "provider_references": [{"provider_group_id": i, "location": f"https://x/{i}.json"} for i in (1, 2, 3)]})
    put(cfg, "acme_rates_2026-05.json", RATE)
    print("  scan:", [(r['file'], r['status'], r.get('rows', r.get('refs'))) for r in scan_inbox(cfg, st)])
    show(st, "E")

    print("== F. reference re-dropped (same bytes) later: does the rate file get re-ingested needlessly?")
    cfg = mkcfg("s6f"); st = Store(cfg.store_dir)
    put(cfg, "acme_refs_2026-05.json", REF); put(cfg, "acme_rates_2026-05.json", RATE); scan_inbox(cfg, st)
    put(cfg, "acme_refs_2026-05.json", REF)
    print("  re-drop identical ref:", scan_inbox(cfg, st))
    put(cfg, "acme_refs_2026-05.json", {**REF, "provider_references": REF["provider_references"] + [{"provider_group_id": 4, "provider_groups": [grp(["4444444444"], "444444444")]}]})
    print("  re-drop a DIFFERENT ref (same payer+month):", scan_inbox(cfg, st))
    show(st, "F")

    print("== G. memory: 1,500,000 rate groups citing a 1,000-id companion, small single-pass path")
    cfg = mkcfg("s6g"); st = Store(cfg.store_dir)
    ref = {**HDR, "provider_references": [{"provider_group_id": i, "provider_groups": [grp([str(1000000000 + i)], f"{100000000 + i}")]} for i in range(1000)]}
    ingest_file(cfg, st, put(cfg, "acme_refs_2026-05.json", ref))
    # one big in_network item with many negotiated_rates groups citing companion ids
    groups = [{"provider_references": [i % 1000], "negotiated_prices": [{"negotiated_type": "negotiated", "negotiated_rate": 10.0 + (i % 50),
               "billing_class": "professional", "service_code": ["11"]}]} for i in range(300_000)]
    big = {**HDR, "in_network": [{"negotiation_arrangement": "ffs", "billing_code_type": "CPT", "billing_code": c, "negotiated_rates": groups}
                                 for c in ("97110", "97112", "97140", "97530", "97535")]}
    p = put(cfg, "acme_rates_2026-05.json.gz", big, gz=True)
    del groups, big; gc.collect()
    print("  file: %.1f MB gz, %.1f MB raw est" % (p.stat().st_size / 1e6, 0.0), "| RSS before: %.0f MB" % rss_mb())
    t0 = time.time(); r = ingest_file(cfg, st, p); dt_ = time.time() - t0
    print("  result:", r["status"], r.get("rows"), "| %.1fs" % dt_, "| peak RSS: %.0f MB" % peak_mb(), "| RSS after: %.0f MB" % rss_mb())
    print("  rates:", rate_counts(st))
