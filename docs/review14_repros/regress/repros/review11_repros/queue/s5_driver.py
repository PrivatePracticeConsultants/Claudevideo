"""Child process for the kill tests: drains the queue in <root>. Guarded by
__main__ (spawned parser workers re-import __main__)."""
import sys
import logging
from pathlib import Path

if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).parent))
    from h import make_cfg, Store, run_queue, add_urls
    root = Path(sys.argv[1]); urls = sys.argv[2:]
    logging.basicConfig(level=logging.INFO, filename=str(root / "driver.log"),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = make_cfg(root, parallel_downloads=3, parallel_ingests=1, early_dedup_mb=0)
    store = Store(cfg.store_dir)
    if urls:
        add_urls(store, urls)
    # a marker so the controller can kill at the claim->ingest instant (s4k)
    if (root / "kill_after_claim").exists():
        import mrfx.fetch as mf
        real = store.claim_content_ingest

        def claim(*a, **k):
            r = real(*a, **k)
            if r is None:
                (root / "claimed").write_text(str(a[1]))
                import time; time.sleep(5)      # the controller kills us in here
            return r
        store.claim_content_ingest = claim
    n = run_queue(cfg, store, drain=True)
    (root / "drained").write_text(str(n))
