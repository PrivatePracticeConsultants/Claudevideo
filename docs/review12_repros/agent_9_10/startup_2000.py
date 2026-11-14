"""Round 9 2.1/2.2: 2,000-part store; time Store() steps and count read calls; second start reads."""
import json, shutil, sys, time, logging
from pathlib import Path
import mrfx.store as S
from mrfx.config import MrfxConfig
from mrfx.ingest import ingest_file
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
def doc():
    return {"reporting_entity_name": "Acme", "version": "1", "last_updated_on": "2026-05-01",
            "in_network": [{"negotiation_arrangement": "ffs", "billing_code_type": "CPT", "billing_code": c,
              "negotiated_rates": [{"provider_groups": [{"npi": [f"1{j:09d}"], "tin": {"type": "ein", "value": f"{100000000+j}"}}],
                 "negotiated_prices": [{"negotiated_type": "negotiated", "negotiated_rate": 40 + j % 7,
                   "billing_class": "professional", "billing_code_modifier": ["GP"], "service_code": ["11"]}]} for j in range(200)]}
              for c in ("97110", "97140", "97530")]}
def _io():
    return {k: int(v) for k, v in (l.split(": ") for l in open("/proc/self/io").read().splitlines())}
def wrap(name):
    f = getattr(S.Store, name)
    def g(self, *a, **k):
        t = time.monotonic(); io0 = _io()
        try: return f(self, *a, **k)
        finally:
            io1 = _io()
            print(f"   step {name:32s} {time.monotonic()-t:7.2f} s  read_calls={io1['syscr']-io0['syscr']:6d} "
                  f"disk_MB={(io1['read_bytes']-io0['read_bytes'])/1e6:8.1f}", flush=True)
    setattr(S.Store, name, g)
if __name__ == "__main__":
    ROOT = Path(sys.argv[1]); N = int(sys.argv[2]); rules = sys.argv[3]
    if not ROOT.exists():
        c = MrfxConfig(inbox_dir=ROOT/"in", processed_dir=ROOT/"p", failed_dir=ROOT/"f", store_dir=ROOT/"store",
                       downloads_dir=ROOT/"dl", entity_map_path=ROOT/"em.yaml"); c.ensure_dirs()
        st = S.Store(c.store_dir)
        p = c.inbox_dir / "seed_2026-05.json"; p.write_text(json.dumps(doc()))
        ingest_file(c, st, p, rebuild_rollups=False)
        seed = st.rates_dir / f"{S.file_key('seed_2026-05.json')}.parquet"
        with st.write_lock, st.connect() as con:
            for i in range(N):
                name = f"f{i:05d}_2026-05.json"
                shutil.copy(seed, st.rates_dir / f"{S.file_key(name)}.parquet")
                con.execute("INSERT INTO files (filename, status, file_type, row_rules) VALUES (?, 'done', 'in_network', 2)", [name])
        st.close()
    if rules != "keep":
        s = S.Store(ROOT/"store")
        with s.write_lock, s.connect() as con:
            con.execute("UPDATE files SET row_rules = ?", [int(rules)])
        s.close()
    for n in ("_sweep_stale_spill", "_quarantine_corrupt_parts", "_init_tables", "_migrate_timestamps_to_utc",
              "_register_views", "_retry_pending_part_ops", "_migrate_row_rules", "_migrate_types_only",
              "_finish_rules", "_migrate_stale_rollups", "_migrate_latest_flag"):
        wrap(n)
    try: open("/proc/sys/vm/drop_caches", "w").write("3\n")
    except OSError: print("could not drop caches")
    io0 = _io(); t = time.monotonic(); s = S.Store(ROOT/"store", keep_warm=True); io1 = _io()
    print("TOTAL open %.1f s read_calls=%d" % (time.monotonic() - t, io1['syscr']-io0['syscr']))
    with s.connect() as con:
        print("row_rules now:", con.execute("SELECT row_rules, count(*) FROM files GROUP BY 1").fetchall())
    s.close()
