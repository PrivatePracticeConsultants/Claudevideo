"""Scenario 2: same name / same content decisions on the inbox path.
A. identical bytes under two names (browser "x (1).json.gz") -> both loaded?
B. identical file re-dropped under its own name (later scan) -> recognised?
C. same name, different bytes, same payer+month -> held; replace / keep; restart mid-choice
D. same name, different month -> loaded beside under a content name
E. header month disagrees with filename month
F. unique_inbox_name collisions
G. bytes already loaded from a LINK (url path) re-dropped in the inbox under another name
"""
import shutil, subprocess, sys, os
from h import *
from mrfx.ingest import scan_inbox, ingest_file, resolve_choice, unique_inbox_name, same_name_verdict, NEEDS_CHOICE


def contract_lines(store):
    return q(store, "SELECT payer, billing_code, tin_value, npi, negotiated_rate, count(DISTINCT source_file), count(*) "
                    "FROM rates GROUP BY 1,2,3,4,5 HAVING count(*) > 1 ORDER BY 1,2,3")


if __name__ == "__main__":
    cfg = mkcfg("s2"); store = Store(cfg.store_dir)
    base = doc(n=3); raw = json.dumps(base).encode(); gz = gzip.compress(raw, mtime=0)

    print("== A. identical bytes under two names, same scan")
    put(cfg, "acme_2026-05.json.gz", gz); put(cfg, "acme_2026-05 (1).json.gz", gz)
    scan_inbox(cfg, store)
    print("  files:", files_rows(store))
    print("  rates per file:", rate_counts(store))
    print("  contract lines held twice:", contract_lines(store))

    print("== A2. identical bytes under a third name, LATER scan (store already knows the sha)")
    put(cfg, "acme_copy.json.gz", gz); scan_inbox(cfg, store)
    print("  rates per file:", rate_counts(store))
    print("  done_file_with_sha knows it:", store.done_file_with_sha(__import__('hashlib').sha256(gz).hexdigest()))

    print("== B. same file re-dropped under its own name (copy -> new mtime)")
    p = put(cfg, "acme_2026-05.json.gz", gz)
    r = scan_inbox(cfg, store)
    print("  scan:", r, "| rates per file:", rate_counts(store), "| where:", where_is(cfg, "acme_2026-05.json.gz"))
    print("  processed copy still there:", (cfg.processed_dir / "acme_2026-05.json.gz").exists())

    print("== C. same name, DIFFERENT bytes, same payer+month -> held")
    other = doc(n=4, seed=7); put(cfg, "acme_2026-05.json.gz", other, gz=True)
    r = scan_inbox(cfg, store)
    print("  scan:", r)
    held = [f for f, s, *_ in files_rows(store) if s == NEEDS_CHOICE]
    print("  held rows:", held, "| .held dir:", sorted(x.name for x in (cfg.inbox_dir / '.held').iterdir()))
    print("  scan again (must not re-hold / re-load):", scan_inbox(cfg, store), files_rows(store)[-1][:2])
    # C1: choose REPLACE but 'restart' before the ingest thread runs (resolve only)
    rr = resolve_choice(cfg, store, held[0], "replace")
    print("  resolve replace ->", rr["filename"], "| files row:", store.file_status("acme_2026-05.json.gz")["status"],
          "| .replacing:", sorted(x.name for x in (cfg.inbox_dir / '.replacing').iterdir()))
    print("  restart: scan_inbox picks the chosen replacement:", scan_inbox(cfg, store))
    print("  rates per file:", rate_counts(store), "(acme_2026-05.json.gz expected 4 = replacement, not 3)")
    print("  files:", [(f, s, n) for f, s, ft, n, e in files_rows(store)])
    print("  leftovers held/replacing:", sorted(x.name for x in (cfg.inbox_dir / '.held').iterdir()),
          sorted(x.name for x in (cfg.inbox_dir / '.replacing').iterdir()))
    # C2: choose KEEP
    third = doc(n=5, seed=9); put(cfg, "acme_2026-05.json.gz", third, gz=True)
    scan_inbox(cfg, store)
    held = [f for f, s, *_ in files_rows(store) if s == NEEDS_CHOICE]
    rk = resolve_choice(cfg, store, held[0], "keep")
    print("  resolve keep ->", rk["filename"], "| row:", store.file_status(rk["filename"])["status"])
    print("  restart scan (held file is in .held, status queued) ->", scan_inbox(cfg, store))
    print("  rates per file:", rate_counts(store), "(expected the kept file with 5 rows)")
    print("  row now:", store.file_status(rk["filename"])["status"], "| where:", where_is(cfg, rk["filename"]))
    # C3: a second restart while a chosen 'keep' never got loaded: is the queued row a trap?
    print("  stale held/replacing leftovers:", sorted(x.name for x in (cfg.inbox_dir / '.held').iterdir()))

    print("== D. same name, different month")
    put(cfg, "acme_2026-05.json.gz", doc(n=2, month="2026-06-01"), gz=True)
    r = scan_inbox(cfg, store); print("  scan:", r)
    print("  rates per file:", rate_counts(store))

    print("== E. header month disagrees with filename month; same name re-drop, header says 2026-07 in a file named 2026-05")
    put(cfg, "beta_2026-05.json.gz", doc(n=2, payer="Beta", month="2026-07-01"), gz=True); scan_inbox(cfg, store)
    print("  file_month recorded:", q(store, "SELECT filename, file_month, published_month, last_updated_on FROM files WHERE filename LIKE 'beta%'"))
    put(cfg, "beta_2026-05.json.gz", doc(n=3, payer="Beta", month="2026-07-01", seed=3), gz=True)
    r = scan_inbox(cfg, store); print("  same name, same header month, different bytes ->", r)
    put(cfg, "beta_2026-05.json.gz", doc(n=3, payer="Beta", month="2026-08-01", seed=4), gz=True)
    r = scan_inbox(cfg, store); print("  same name, header month 08 (filename 05) ->", r)

    print("== F. unique_inbox_name collisions")
    (cfg.inbox_dir / "x-abcd1234.json.gz").write_bytes(b"")
    store.upsert_file("x-abcd1234-1.json.gz", status="done")
    print("  ", unique_inbox_name(cfg.inbox_dir, "x.json.gz", store, "abcd1234"), "(expected x-abcd1234-2.json.gz)")
    print("  ", unique_inbox_name(cfg.inbox_dir, "rates.json.2026-05.json", store, "t"), "(expected rates.json.2026-05-t.json)")
    print("  ", unique_inbox_name(cfg.inbox_dir, ".json.gz", store, "t"), unique_inbox_name(cfg.inbox_dir, "noext", store, "t"))
    (cfg.inbox_dir / "x-abcd1234.json.gz").unlink()

    print("== G. contract lines held twice at the end:")
    for r in contract_lines(store):
        print("  ", r)
    print("  rollups_stale:", store.rollups_stale())
