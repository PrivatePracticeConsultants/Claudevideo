"""Smaller reference cases at ingest_file level."""
from h import *
from mrfx.ingest import ingest_file, scan_inbox

G = lambda n, t: [grp([n], t)]
def rf(name, month, entries, payer="Acme Health"):
    return name, {**HDR, "reporting_entity_name": payer, "last_updated_on": month, "provider_references": entries}
R = lambda i, g: {"provider_group_id": i, "provider_groups": g}

if __name__ == "__main__":
    print("1. a companion id whose entry is EMPTY / a remote link")
    cfg = mkcfg("n11_1"); st = Store(cfg.store_dir)
    ingest_file(cfg, st, put(cfg, *rf("acme_refs_2026-05.json", "2026-05-01", [R(1, []), {"provider_group_id": 2, "location": "https://x/2.json"}, R(3, G("3333333333", "333333333"))])))
    r = ingest_file(cfg, st, put(cfg, "acme_rates_2026-05.json", {**HDR, "in_network": [item("97110", refs=[1]), item("97112", refs=[2]), item("97116", refs=[3])]}))
    print("   ->", r)
    qa = json.loads(q(st, "SELECT qa FROM files WHERE filename='acme_rates_2026-05.json'")[0][0])
    print("   qa:", {k: qa.get(k) for k in ("rows", "missing_ref_ids", "remote_ref_groups", "ambiguous_ref_ids")},
          "| the same two ids in the file's OWN table give ref_groups_skipped=2 (1 missing + 1 remote)")

    print("2. three same-month companions: two agree on id 1, one differs; id 2 only in one")
    cfg = mkcfg("n11_2"); st = Store(cfg.store_dir)
    ingest_file(cfg, st, put(cfg, *rf("a_refs_2026-05.json", "2026-05-01", [R(1, G("1111111111", "111111111")), R(2, G("2222222222", "222222222"))])))
    ingest_file(cfg, st, put(cfg, *rf("b_refs_2026-05.json", "2026-05-01", [R(1, G("1111111111", "111111111"))])))
    ingest_file(cfg, st, put(cfg, *rf("c_refs_2026-05.json", "2026-05-01", [R(1, G("5555555555", "555555555"))])))
    refs = st.load_provider_refs("Acme Health")
    print("   id 1 ->", refs.get(1), "| id 2 ->", refs.get(2))
    r = ingest_file(cfg, st, put(cfg, "acme_rates_2026-05.json", {**HDR, "in_network": [item("97110", refs=[1, 2])]}))
    print("   rate file ->", r, q(st, "SELECT tin_value FROM rates"))

    print("3. a reference-only file with an EMPTY table, and a rate file with an empty in_network + a table")
    cfg = mkcfg("n11_3"); st = Store(cfg.store_dir)
    print("   empty table ->", ingest_file(cfg, st, put(cfg, *rf("acme_refs_2026-05.json", "2026-05-01", []))))
    print("   empty in_network + table ->", ingest_file(cfg, st, put(cfg, "acme_rates_2026-05.json", {**HDR, "provider_references": [R(1, G("1111111111", "111111111"))], "in_network": []})))
    print("   files:", q(st, "SELECT filename, file_type, status, rows_emitted FROM files ORDER BY 1"), "| provider_refs rows:", q(st, "SELECT count(*) FROM provider_refs")[0][0])

    print("4. inbox: rate file first, its companion in a LATER scan (requeue path); move_processed on and off")
    for move in (True, False):
        cfg = mkcfg(f"n11_4_{move}", move_processed=move); st = Store(cfg.store_dir)
        put(cfg, "acme_rates_2026-05.json.gz", {**HDR, "in_network": [item("97110", refs=[1, 2])]}, gz=True)
        r1 = scan_inbox(cfg, st)
        put(cfg, *rf("acme_refs_2026-05.json", "2026-05-01", [R(1, G("1111111111", "111111111")), R(2, G("2222222222", "222222222"))]))
        r2 = scan_inbox(cfg, st)
        print(f"   move_processed={move}: first scan rows {r1[0].get('rows')} skipped {r1[0].get('ref_groups_skipped')}; second scan -> {[(x['file'], x['status'], x.get('refs'), x.get('requeued')) for x in r2]}"
              f" | rates rows {q(st, 'SELECT count(*) FROM rates')[0][0]} (2 expected) | files {q(st, 'SELECT filename, rows_emitted FROM files ORDER BY 1')}")
        r3 = scan_inbox(cfg, st)
        print("      third scan ->", r3, "| files:", q(st, "SELECT count(*) FROM files")[0][0])
