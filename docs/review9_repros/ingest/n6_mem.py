"""Peak RSS of one ingest. usage: n6_mem.py <tree: head|old> <layout> <mb> <mode>
layout: norefs (file has NO provider_references; ids come from the payer's
        standalone companion) | refs_first | refs_last
mode:   small_fast | small_classic | big_classic | big_fast
Each run is its own process; prints rows, seconds, peak RSS."""
import json, os, resource, sys, time
from pathlib import Path

tree, layout, mb, mode = sys.argv[1], sys.argv[2], int(sys.argv[3]), sys.argv[4]
HERE = Path("/tmp/mrfx_review9/ingest")
sys.path.insert(0, "/home/user/Claudevideo" if tree == "head" else str(HERE / "old556"))
import mrfx
assert ("old556" in mrfx.__file__) == (tree == "old"), mrfx.__file__
from mrfx.config import MrfxConfig
from mrfx.store import Store
import mrfx.ingest as ing

NREF = 1000
CODES = ["97110", "97112", "97140", "97530", "97116", "97035", "97014", "97010"]

def ref_entry(i):
    return {"provider_group_id": i, "provider_groups": [{"npi": [1000000000 + i], "tin": {"type": "ein", "value": f"{100000000 + i}"}}]}

def gen(path: Path, target_mb: int, layout: str):
    hdr = '{"reporting_entity_name":"Acme Health","reporting_entity_type":"issuer","last_updated_on":"2026-05-01","version":"1.0.0",'
    refs = '"provider_references":[' + ",".join(json.dumps(ref_entry(i)) for i in range(1, NREF + 1)) + "]"
    n = 0
    with open(path, "w") as f:
        f.write(hdr)
        if layout == "refs_first":
            f.write(refs + ",")
        f.write('"in_network":[')
        written, ci, first = 0, 0, True
        per_item = 20000
        while written < target_mb * 1_000_000:
            code = CODES[ci % len(CODES)]; ci += 1
            parts = []
            for k in range(per_item):
                n += 1
                parts.append('{"provider_references":[%d],"negotiated_prices":[{"negotiated_type":"negotiated",'
                             '"negotiated_rate":%d.%02d,"expiration_date":"9999-12-31","service_code":["11"],'
                             '"billing_class":"professional"}]}' % (n % NREF + 1, 20 + n % 80, n % 100))
            s = ('' if first else ',') + ('{"negotiation_arrangement":"ffs","name":"x","billing_code_type":"CPT",'
                 '"billing_code_type_version":"2026","billing_code":"%s","description":"x","negotiated_rates":[' % code) + ",".join(parts) + "]}"
            first = False
            f.write(s); written += len(s)
        f.write("]")
        if layout == "refs_last":
            f.write("," + refs)
        f.write("}")
    return n

if __name__ == "__main__":
    root = HERE / "runs" / f"n6_{tree}_{layout}_{mb}_{mode}"
    import shutil
    shutil.rmtree(root, ignore_errors=True)
    cfg = MrfxConfig(inbox_dir=root/"inbox", processed_dir=root/"processed", failed_dir=root/"failed",
                     store_dir=root/"store", downloads_dir=root/"downloads",
                     entity_map_path=root/"entity_map.yaml", fast_scan=mode.endswith("fast"),
                     move_processed=False)
    cfg.ensure_dirs()
    st = Store(cfg.store_dir)
    if layout == "norefs":
        comp = cfg.inbox_dir / "acme_refs_2026-05.json"
        comp.write_text(json.dumps({"reporting_entity_name": "Acme Health", "last_updated_on": "2026-05-01",
                                    "provider_references": [ref_entry(i) for i in range(1, NREF + 1)]}))
        ing.ingest_file(cfg, st, comp)
    src = HERE / "runs" / f"n6_src_{layout}_{mb}.json"
    if not src.exists():
        gen(src, mb, layout)
    p = cfg.inbox_dir / "acme_rates_2026-05.json"
    os.link(src, p)
    if mode.startswith("big"):
        ing.LARGE_FILE_UNCOMPRESSED_BYTES = 1
    base = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
    t0 = time.time()
    r = ing.ingest_file(cfg, st, p, rebuild_rollups=False)
    dt = time.time() - t0
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
    print(f"RESULT tree={tree} layout={layout} file={p.stat().st_size/1e6:.0f}MB mode={mode} status={r.get('status')} "
          f"rows={r.get('rows')} skipped={r.get('ref_groups_skipped')} secs={dt:.0f} rss_before={base:.0f}MB PEAK_RSS={peak:.0f}MB")
    shutil.rmtree(root, ignore_errors=True)
