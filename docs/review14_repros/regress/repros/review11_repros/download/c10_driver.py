"""Kill-test driver: run_queue(drain=True) on a scratch store. Spawned by
c10_kill.py (never run against the repo's data/). Usage:
    c10_driver.py <run_root> <url> [<url> ...]"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))


def main():
    from _harness import mk_cfg
    from mrfx.fetch import add_urls, run_queue
    import logging
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    root, urls = sys.argv[1], sys.argv[2:]
    cfg, store = mk_cfg(Path(root).name, fresh=False, download_park_seconds=0.0,
                        download_stall_seconds=60.0, download_timeout_seconds=10.0,
                        parallel_downloads=2, parallel_ingests=1, early_dedup_mb=1)
    add_urls(store, urls)          # idempotent: an existing row is left alone
    n = run_queue(cfg, store, drain=True)
    print(f"DRIVER_DONE processed={n}", flush=True)


if __name__ == "__main__":
    main()
