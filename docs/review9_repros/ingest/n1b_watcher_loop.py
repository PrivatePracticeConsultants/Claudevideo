"""move_processed: false + the real inbox watcher (cli._watcher_loop): one
2.3 MiB file dropped once. How many times is it loaded in 25 s?"""
import threading, time
from h import *
from mrfx.cli import _watcher_loop
from n1_samename import doc

if __name__ == "__main__":
    cfg = mkcfg("n1b", move_processed=False)
    st = Store(cfg.store_dir)
    stop = threading.Event()
    t = threading.Thread(target=_watcher_loop, args=(cfg, st, stop), daemon=True)
    t.start()
    time.sleep(2)
    put(cfg, "acme_2026-05.json", doc(7000))
    time.sleep(6)
    print("t+6s files rows:", q(st, 'SELECT count(*) FROM files')[0][0], "-- now ONE unrelated small file is dropped in")
    other = {**HDR, "reporting_entity_name": "Other Plan", "in_network": [item("97110", groups=[grp(["1234567893"], "123456789")])]}
    put(cfg, "other_2026-05.json", other)
    for s in (11, 16, 21, 26, 31, 36):
        time.sleep(5)
        print(f"t+{s:2d}s files rows: {q(st, 'SELECT count(*) FROM files')[0][0]}  "
              f"rates rows: {q(st, 'SELECT count(*) FROM rates')[0][0]}  "
              f"inbox: {[x.name[-28:] for x in cfg.inbox_dir.iterdir()]}")
    stop.set(); t.join(10)
    st.rebuild_rollups()
    print("rates_by_tin for one TIN:", q(st, "SELECT payer, billing_code, negotiated_rate, source_count FROM rates_by_tin WHERE tin_value='100000001'") if True else "")
