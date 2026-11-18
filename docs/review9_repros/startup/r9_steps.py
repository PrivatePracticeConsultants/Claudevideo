"""Time every step of Store.__init__ on a store (cold cache), pinned or not."""
import logging, os, sys, time
sys.path.insert(0, "/home/user/Claudevideo")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
import mrfx.store as S
D, pinned, rules = sys.argv[1], sys.argv[2] == "pin", sys.argv[3]
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
    if rules != "keep":
        s = S.Store(D)
        with s.write_lock, s.connect() as con:
            con.execute("UPDATE files SET row_rules = ?", [int(rules)])
        s.close()
    for n in ("_sweep_stale_spill", "_quarantine_corrupt_parts", "_init_tables", "_migrate_timestamps_to_utc",
              "_register_views", "_retry_pending_part_ops", "_migrate_row_rules", "_migrate_types_only",
              "_finish_rules", "_migrate_stale_rollups", "_migrate_latest_flag", "_connect_with_retry"):
        wrap(n)
    os.sync()
    try: open("/proc/sys/vm/drop_caches", "w").write("3\n")
    except OSError: print("could not drop caches")
    t = time.monotonic(); s = S.Store(D, keep_warm=pinned); print("TOTAL open %.1f s (pinned=%s)" % (time.monotonic() - t, pinned)); s.close()
