"""ingest_file end to end: 3 containers x fast on/off x small/big x in-process/
pooled, two reference shapes. Rows and QA counters must be identical."""
import gzip, io, zipfile, itertools, sys
from h import *
import mrfx.ingest as ing
from mrfx.ingest import ingest_file, ParsePoolManager, set_parse_pool

A = [grp(["1111111111"], "111111111")]; B = [grp(["2222222222", "123"], "222222222")]
COMP = {**HDR, "provider_references": [{"provider_group_id": 1, "provider_groups": [grp(["9999999999"], "999999999")]},
                                       {"provider_group_id": 2, "provider_groups": B},
                                       {"provider_group_id": 3, "provider_groups": [grp([], "333333333")]}]}
SHAPES = {
  "own_after+companion": {**HDR, "in_network": [item("97110", refs=[1, 2, 8], rate=41.0), item("99213", refs=[1]),
                                                 item("97112", refs=[3], rate=0.0, ntype="Fee_Schedule"),
                                                 item("97140", refs=[1], arr="bundle"), item("97530", refs=["x"]),
                                                 item("97116", refs=[4])],
                          "provider_references": [{"provider_group_id": 1, "provider_groups": A},
                                                  {"provider_group_id": 4, "location": "https://x/4.json"}]},
  "no_table+companion": {**HDR, "in_network": [item("97110", refs=[1, 2, 8], rate=41.0), item("97112", refs=[3], rate=12.5),
                                                item("97116", refs=[77])]},
}

def pack(obj, container):
    data = json.dumps(obj).encode()
    if container == "json": return "x_2026-05.json", data
    if container == "gz": return "x_2026-05.json.gz", gzip.compress(data)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("readme.txt", "hello"); z.writestr("inner/x_2026-05.json", data)
    return "x_2026-05.zip", buf.getvalue()

if __name__ == "__main__":
    pool = ParsePoolManager(max_workers=1)
    real_pf = ing.preflight
    bad = 0
    try:
        for sname, shape in SHAPES.items():
            results = {}
            for container, fast, big, pooled in itertools.product(("json", "gz", "zip"), (True, False), (False, True), (False, True)):
                cfg = mkcfg("n10", fast=fast); st = Store(cfg.store_dir)
                ingest_file(cfg, st, put(cfg, "acme_refs_2026-05.json", COMP))
                name, data = pack(shape, container)
                p = cfg.inbox_dir / name; p.write_bytes(data)
                ing.LARGE_FILE_UNCOMPRESSED_BYTES = 1 if big else 1_500_000_000
                if big:
                    def big_pf(*a, **k):
                        pf = real_pf(*a, **k); pf.est_uncompressed_bytes = 10 ** 12; return pf
                    ing.preflight = big_pf
                else:
                    ing.preflight = real_pf
                set_parse_pool(pool if pooled else None)
                try:
                    r = ingest_file(cfg, st, p)
                finally:
                    set_parse_pool(None)
                rows = q(st, "SELECT billing_code, tin_value, npi, negotiated_rate, negotiated_type, is_dollar_rate, file_month, payer "
                             "FROM rates WHERE source_file = ? ORDER BY ALL", name)
                f = q(st, "SELECT status, rows_emitted, ref_groups_skipped, qa, file_month, payer FROM files WHERE filename = ?", name)[0]
                qa = {k: v for k, v in (json.loads(f[3]) if f[3] else {}).items() if k not in ("messages",)}
                results[(container, fast, big, pooled)] = (rows, f[0], f[1], f[2], qa, f[4], f[5])
                st.close()
            base_key = ("json", False, False, False)
            base = results[base_key]
            print(f"SHAPE {sname}: classic/small/in-process/.json -> status {base[1]} rows {base[2]} skipped {base[3]}")
            print("   rows:", [(r[0], r[1], r[2], r[3], r[4], r[5]) for r in base[0]])
            print("   qa:", {k: v for k, v in base[4].items() if v not in (0, "", 0.0, 1.0, None)})
            for k, v in results.items():
                if v != base:
                    bad += 1
                    what = [n for n, a, b in zip(("rows", "status", "rows_emitted", "skipped", "qa", "file_month", "payer"), v, base) if a != b]
                    print(f"   DIFF container={k[0]} fast={k[1]} big={k[2]} pooled={k[3]}: differs in {what}")
                    if "qa" in what:
                        print("        qa diff:", {x: (v[4].get(x), base[4].get(x)) for x in set(v[4]) | set(base[4]) if v[4].get(x) != base[4].get(x)})
                    if "rows" in what:
                        print("        rows:", [(r[0], r[1], r[2], r[3]) for r in v[0]])
            print(f"   {len(results)} paths compared")
    finally:
        ing.preflight = real_pf; ing.LARGE_FILE_UNCOMPRESSED_BYTES = 1_500_000_000
        pool.shutdown_now()
    print("paths that differ from the classic small in-process .json result:", bad)
