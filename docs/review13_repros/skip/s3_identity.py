"""S3: identity = sha256 of the RAW bytes.
 A. the SAME content re-compressed (gz level 1 vs 9), as .json vs .json.gz,
    via the inbox and via a link + inbox: how many files load, rates doubled?
 B. what a decompressed-content fingerprint would cost: sha256 over the
    inflated stream, measured on real payer files against inflate alone.
 C. LOCAL_HASH_MAX_BYTES: the > 4 GB inbox rule, exercised by lowering the
    threshold to 1 MB (the code reads the constant at call time) — a twin
    under a second name, inbox+inbox and link+inbox both orders."""
import gzip
import hashlib
import logging
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import gen  # noqa: E402
import h  # noqa: E402

REAL = Path("/tmp/claude-0/-home-user-Claudevideo/4633d594-0139-5f8e-a935-ae9c76b72000/scratchpad/real")


def counts(store):
    with store.connect() as con:
        files = con.execute("SELECT filename, status, rows_emitted, substr(error,1,90) FROM files ORDER BY filename").fetchall()
        rates = con.execute("SELECT count(*) FROM rates").fetchone()[0]
        fps = con.execute("SELECT filename, size, full_sha IS NOT NULL AS has_sha, trailer_proves, magic FROM content_fingerprints ORDER BY filename").fetchall()
    return files, rates, fps


def inbox_case(name, drops: dict, early_mb=15):
    from mrfx.ingest import scan_inbox
    cfg = h.scratch_cfg("s3_" + name, parallel_downloads=1, parallel_ingests=1, early_dedup_mb=early_mb)
    store = h.open_store(cfg)
    for fname, data in drops.items():
        (cfg.inbox_dir / fname).write_bytes(data)
    scan_inbox(cfg, store)
    files, rates, fps = counts(store)
    print(f"\n== inbox {name}")
    for f in files:
        print("   file:", f)
    print(f"   rates: {rates}  (one copy = {ONE} rows)")
    print(f"   fingerprints: {fps}")
    return cfg, store


ONE = None


def link_then_inbox(name, link_body, inbox_name, inbox_body, early_mb=15, inbox_first=False):
    from mrfx import fetch
    from mrfx.ingest import scan_inbox
    cfg = h.scratch_cfg("s3_" + name, parallel_downloads=1, parallel_ingests=1, early_dedup_mb=early_mb)
    store = h.open_store(cfg)
    h.ROUTES.clear(); h.LOG.clear()

    def fn(hh, head, entry):
        rng = h.parse_range(hh, len(link_body))
        if rng:
            a, b = rng
            h.send(hh, entry, 206, {"Content-Range": f"bytes {a}-{b}/{len(link_body)}", "ETag": '"x"'}, link_body[a:b + 1], head=head)
        else:
            h.send(hh, entry, 200, {"ETag": '"x"'}, link_body, head=head)
    h.ROUTES["/link.json.gz"] = fn
    httpd, base = h.start_server()
    if inbox_first:
        (cfg.inbox_dir / inbox_name).write_bytes(inbox_body)
        scan_inbox(cfg, store)
        fetch.add_urls(store, [base + "/link.json.gz"])
        h.drain(cfg, store, 120)
    else:
        fetch.add_urls(store, [base + "/link.json.gz"])
        h.drain(cfg, store, 120)
        (cfg.inbox_dir / inbox_name).write_bytes(inbox_body)
        scan_inbox(cfg, store)
    files, rates, fps = counts(store)
    rows = h.queue_rows(store, "id, status, kind, duplicate_of, substr(error,1,100) AS error")
    print(f"\n== {'inbox then link' if inbox_first else 'link then inbox'} {name}")
    for f in files:
        print("   file:", f)
    for r in rows:
        print("   url row:", r)
    print(f"   rates: {rates}  (one copy = {ONE} rows)")
    print(f"   fingerprints: {fps}")
    httpd.shutdown()


def measure_inflated_sha():
    print("\n== B. cost of a decompressed-content fingerprint (real files)")
    for fn in ("uhc_2026-11-01_UnitedHealthcare-Insurance-Company-of-New-York_Third-Party-Administrator_Empire_CSP-377-A351_in-network-rates.json.gz",
               "uhc_2026-11-01_UnitedHealthcare-Insurance-Company-of-the-River-Valley_Insurer_Heritage-EPO_669_in-network-rates.json.gz"):
        p = REAL / fn
        if not p.exists():
            continue
        comp = p.stat().st_size
        # inflate only
        t0 = time.perf_counter()
        n = 0
        with gzip.open(p, "rb") as g:
            for blk in iter(lambda: g.read(4 << 20), b""):
                n += len(blk)
        t_inf = time.perf_counter() - t0
        # inflate + sha256 of the inflated stream
        t0 = time.perf_counter()
        hh = hashlib.sha256()
        with gzip.open(p, "rb") as g:
            for blk in iter(lambda: g.read(4 << 20), b""):
                hh.update(blk)
        t_both = time.perf_counter() - t0
        # sha256 of the raw (compressed) bytes, what the app does today
        t0 = time.perf_counter()
        hr = hashlib.sha256()
        with open(p, "rb") as f:
            for blk in iter(lambda: f.read(4 << 20), b""):
                hr.update(blk)
        t_raw = time.perf_counter() - t0
        print(f"   {fn[:60]}…: {comp / 1e6:,.0f} MB gz -> {n / 1e6:,.0f} MB inflated; inflate {t_inf:.2f}s "
              f"({n / 1e6 / t_inf:,.0f} MB/s), inflate+sha {t_both:.2f}s (+{t_both - t_inf:.2f}s = "
              f"{(t_both - t_inf) / (n / 1e9):.2f} s/GB inflated), raw sha {t_raw:.2f}s")


def local_hash_limit_cases():
    """LOCAL_HASH_MAX_BYTES lowered to 1 MB: a 3.5 MB inbox file is 'over 4 GB'."""
    import mrfx.identity as ident
    ident.LOCAL_HASH_MAX_BYTES = 1 << 20
    print(f"\n== C. LOCAL_HASH_MAX_BYTES lowered to {ident.LOCAL_HASH_MAX_BYTES:,} B (the > 4 GB rule on 3.5 MB files)")
    raw = gen.doc(n_items=1500, filler=2000, payer="Big Payer")
    big = gen.gz0(raw)
    inbox_case("C1_inbox_inbox_over_limit", {"big_a.json.gz": big, "big_b.json.gz": big}, early_mb=1)
    # same twin, but one drop after the other (two scans), to rule out same-scan ordering
    from mrfx.ingest import scan_inbox
    cfg = h.scratch_cfg("s3_C1b_two_scans", parallel_downloads=1, parallel_ingests=1, early_dedup_mb=1)
    store = h.open_store(cfg)
    (cfg.inbox_dir / "big_a.json.gz").write_bytes(big)
    scan_inbox(cfg, store)
    (cfg.inbox_dir / "big_b.json.gz").write_bytes(big)
    scan_inbox(cfg, store)
    files, rates, fps = counts(store)
    print("\n== inbox C1b two scans, over limit")
    for f in files:
        print("   file:", f)
    print(f"   rates: {rates}; fingerprints: {fps}")
    # SAME name re-dropped as a copy (new mtime): 'same' by sha is impossible over the limit
    shutil.copyfile(cfg.processed_dir / "big_a.json.gz", cfg.inbox_dir / "big_a.json.gz")
    scan_inbox(cfg, store)
    files, rates, fps = counts(store)
    print("   after re-dropping a COPY of big_a under its own name:")
    for f in files:
        print("   file:", f)
    print(f"   rates: {rates}; held: {sorted(p.name for p in (cfg.inbox_dir / '.held').glob('*')) if (cfg.inbox_dir / '.held').exists() else []}")
    link_then_inbox("C2_link_then_inbox_over_limit", big, "copy.json.gz", big, early_mb=1)
    link_then_inbox("C3_inbox_then_link_over_limit", big, "copy.json.gz", big, early_mb=1, inbox_first=True)
    ident.LOCAL_HASH_MAX_BYTES = 4 << 30


def main():
    global ONE
    logging.basicConfig(level=logging.ERROR)
    raw = gen.doc(n_items=300, filler=200, payer="Acme Health")
    ONE = 300
    g1, g9 = gen.gzn(raw, 1), gen.gzn(raw, 9)
    print(f"raw {len(raw):,} B; gz level1 {len(g1):,} B; gz level9 {len(g9):,} B; identical? {g1 == g9}")
    inbox_case("A1_gz1_vs_gz9", {"acme_l1.json.gz": g1, "acme_l9.json.gz": g9})
    inbox_case("A2_json_vs_gz", {"acme.json": raw, "acme.json.gz": g9})
    link_then_inbox("A3_link_gz_then_inbox_json", g9, "acme.json", raw)
    link_then_inbox("A4_link_gz_then_inbox_regz", g9, "acme_browser.json.gz", g1)
    measure_inflated_sha()
    local_hash_limit_cases()


if __name__ == "__main__":
    main()
