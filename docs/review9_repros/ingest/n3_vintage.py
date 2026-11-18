"""Standalone companions of two months. Ids are file-local, so month N's rate
file must resolve against month N's companion only."""
from h import *
from mrfx.ingest import ingest_file, scan_inbox

APR = {**HDR, "last_updated_on": "2026-04-01", "provider_references": [
    {"provider_group_id": 1, "provider_groups": [grp(["1111111111"], "111111111")]},
    {"provider_group_id": 2, "provider_groups": [grp(["2222222222"], "222222222")]}]}
MAY = {**HDR, "last_updated_on": "2026-05-01", "provider_references": [
    {"provider_group_id": 1, "provider_groups": [grp(["5555555555"], "555555555")]},
    {"provider_group_id": 2, "provider_groups": [grp(["6666666666"], "666666666")]}]}
RATES_MAY = {**HDR, "last_updated_on": "2026-05-01", "in_network": [item("97110", refs=[1, 2])]}
RATES_APR = {**HDR, "last_updated_on": "2026-04-01", "in_network": [item("97110", refs=[1, 2], rate=44.0)]}

def show(st, label):
    print(f"   {label}: rates ->", q(st, "SELECT source_file, file_month, tin_value, npi FROM rates ORDER BY 1, 3"))
    print("      files ->", q(st, "SELECT filename, rows_emitted, ref_groups_skipped, json_extract_string(qa,'$.missing_ref_ids') FROM files WHERE file_type='in_network' ORDER BY 1"))

if __name__ == "__main__":
    print("CASE 1: April companion loaded; May RATE file arrives before May's companion")
    cfg = mkcfg("n3_case1"); st = Store(cfg.store_dir)
    ingest_file(cfg, st, put(cfg, "acme_refs_2026-04.json", APR))
    r = ingest_file(cfg, st, put(cfg, "acme_rates_2026-05.json", RATES_MAY))
    print("   May rate file ->", r)
    show(st, "before May's companion")
    r = ingest_file(cfg, st, put(cfg, "acme_refs_2026-05.json", MAY))
    print("   May companion ->", r)
    show(st, "after May's companion (expected TINs 555555555 / 666666666)")

    print("CASE 2: both companions loaded; April's rate file is (re-)ingested afterwards")
    cfg = mkcfg("n3_case2"); st = Store(cfg.store_dir)
    ingest_file(cfg, st, put(cfg, "acme_refs_2026-04.json", APR))
    ingest_file(cfg, st, put(cfg, "acme_rates_2026-04.json", RATES_APR))
    show(st, "April alone (correct: 111111111 / 222222222)")
    ingest_file(cfg, st, put(cfg, "acme_refs_2026-05.json", MAY))
    ingest_file(cfg, st, put(cfg, "acme_rates_2026-05.json", RATES_MAY))
    # the user re-drops April's file (or its link is retried / a reset + reload)
    r = ingest_file(cfg, st, cfg.processed_dir / "acme_rates_2026-04.json")
    print("   April rate file re-ingested ->", r)
    show(st, "after (expected April still 111111111 / 222222222)")

    print("CASE 3: one inbox scan with April rates + both companions (reference files sort first)")
    cfg = mkcfg("n3_case3"); st = Store(cfg.store_dir)
    put(cfg, "acme_refs_2026-04.json", APR); put(cfg, "acme_refs_2026-05.json", MAY)
    put(cfg, "acme_rates_2026-04.json", RATES_APR); put(cfg, "acme_rates_2026-05.json", RATES_MAY)
    scan_inbox(cfg, st)
    show(st, "after scan (expected April -> 1111/2222, May -> 5555/6666)")
