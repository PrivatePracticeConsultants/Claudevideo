"""Read-only look at a scratch store from a short-lived process (so the
controller never holds the DuckDB file while a driver is alive)."""
import json
import sys
from pathlib import Path

if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).parent))
    from h import make_cfg, Store, q
    root = Path(sys.argv[1])
    cfg = make_cfg(root)
    store = Store(cfg.store_dir)
    out = {
        "queue": q(store, "SELECT id, status, kind, url, filename, content_sha, error FROM url_queue ORDER BY id"),
        "files": q(store, "SELECT filename, status, rows_emitted FROM files ORDER BY 1"),
    }
    try:
        out["rates"] = q(store, "SELECT source_file, count(*) n FROM rates GROUP BY 1 ORDER BY 1")
    except Exception as e:  # noqa: BLE001
        out["rates"] = []
        out["rates_err"] = str(e)[:80]
    out["parts"] = sorted(p.name for p in (cfg.store_dir / "rates").glob("*")) if (cfg.store_dir / "rates").exists() else []
    out["downloads"] = sorted(p.name for p in cfg.downloads_dir.iterdir())
    store.close()
    print(json.dumps(out, default=str))
