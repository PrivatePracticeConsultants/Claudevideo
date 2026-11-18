"""UI harness: the real app (create_app) on the scratch live store, served by
uvicorn on :8792, with ONE injected cost: every connection a run WITH a
progress token opens first executes a real ~1 s DuckDB statement (an
interruptible scan of range(), so DuckDB progress moves and Stop interrupts
it). Stands in for a big store so the bars are visible and Stop can land.
Token-less requests are untouched."""
import sys, threading
from pathlib import Path
sys.path.insert(0, "/home/user/Claudevideo")
import uvicorn
from mrfx.config import load_mrfx_config as load_config
from mrfx.store import Store
from mrfx import progress as P
import mrfx.api as api

CFG = Path(__file__).parent / "live" / "mrfx.yaml"
DELAY_ROWS = int(sys.argv[1]) if len(sys.argv) > 1 else 400_000_000

def main():
    cfg = load_config(CFG)
    store = Store(cfg.store_dir)
    orig = Store.connect
    def slow(self):
        con = orig(self)
        if P.current() is not P._NOOP:
            con.execute(f"SELECT count(*) FROM range({DELAY_ROWS}) t(i) WHERE i % 7 = 3").fetchone()
        return con
    Store.connect = slow
    app = api.create_app(cfg, store)
    api.TRUSTED_HOSTS.add("127.0.0.1")
    uvicorn.run(app, host="127.0.0.1", port=8792, log_level="warning")

if __name__ == "__main__":
    main()
