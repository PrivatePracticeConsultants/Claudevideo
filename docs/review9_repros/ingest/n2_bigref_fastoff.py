"""A standalone provider-reference file > 1 MB (the sniff calls it in_network)
on the BIG path when the single-read fast path is not used: fast_scan off,
all-codes mode, or the pooled worker with fast_scan off."""
import sys
from h import *
import mrfx.ingest as ing
from mrfx.ingest import ingest_file, ParsePoolManager, set_parse_pool

def refs(n):
    return {**HDR, "provider_references": [
        {"provider_group_id": i, "provider_groups": [grp([str(1000000000 + i)], f"{100000000 + i}")]}
        for i in range(1, n + 1)]}

def run(label, fast, big, pooled=False, all_codes=False):
    cfg = mkcfg("n2_" + label, fast=fast)
    if all_codes:
        cfg.codes = []          # all-codes mode, if the config supports it this way
    st = Store(cfg.store_dir)
    ing.LARGE_FILE_UNCOMPRESSED_BYTES = 1 if big else 1_500_000_000
    real_pf = ing.preflight
    pool = None
    if pooled:
        if big:
            def big_pf(*a, **k):
                pf = real_pf(*a, **k); pf.est_uncompressed_bytes = 10 ** 12; return pf
            ing.preflight = big_pf
        pool = ParsePoolManager(max_workers=1); set_parse_pool(pool)
    try:
        b = {**HDR, "in_network": [item("97110", refs=[1, 2])]}
        r0 = ingest_file(cfg, st, put(cfg, "acme_B_2026-05.json", b))
        p = put(cfg, "acme_refs_2026-05.json", refs(20000))
        out = ingest_file(cfg, st, p)
        print(f"[{label}] fast_scan={fast} big={big} pooled={pooled} code_set={'ALL' if cfg.code_set is None else len(cfg.code_set)}")
        print("   reference file ->", {k: (v if k != 'requeued' else v) for k, v in out.items()})
        print("   files:", q(st, "SELECT filename, file_type, status, rows_emitted, json_extract_string(qa,'$.messages') FROM files ORDER BY 1"))
        print("   provider_refs ids stored:", q(st, "SELECT count(DISTINCT ref_id) FROM provider_refs")[0][0], "(expected 20000)")
        print("   B rows after:", q(st, "SELECT count(*) FROM rates")[0][0], "(expected 2)")
    finally:
        ing.preflight = real_pf
        ing.LARGE_FILE_UNCOMPRESSED_BYTES = 1_500_000_000
        if pool:
            set_parse_pool(None); pool.shutdown_now()

if __name__ == "__main__":
    import mrfx.parser as P
    if sys.argv[1:] == ["lowdisk"]:
        # default config (fast_scan on); the drive has < 2 GB free, so the
        # single-read path declines (parser.SPOOL_FREE_FLOOR) and two-pass runs
        P._free_disk = lambda path: 1 << 30
        run("fast_big_lowdisk", True, True)
        sys.exit()
    run("fast_big", True, True)
    run("classic_small", False, False)
    run("classic_big", False, True)
    run("classic_big_pooled", False, True, pooled=True)
    run("fast_big_pooled", True, True, pooled=True)
