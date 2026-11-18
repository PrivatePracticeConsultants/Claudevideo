"""Tiny scriptable HTTP server + scratch config for round-11 pipeline checks.
Each handler is registered per path with a function (handler, path) -> None."""
import gzip
import http.server
import io
import json
import sys
import threading
from pathlib import Path

sys.path.insert(0, "/home/user/Claudevideo")
from mrfx.config import MrfxConfig  # noqa: E402
from mrfx.store import Store  # noqa: E402

ROUTES: dict = {}
LOG: list = []


class H(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_HEAD(self):
        self._serve(head=True)

    def do_GET(self):
        self._serve(head=False)

    def _serve(self, head):
        LOG.append((self.command, self.path, dict(self.headers)))
        fn = ROUTES.get(self.path.split("?")[0])
        if fn is None:
            self.send_response(404); self.send_header("Content-Length", "0"); self.end_headers()
            return
        try:
            fn(self, head)
        except (BrokenPipeError, ConnectionResetError):
            pass


def start_server():
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, f"http://127.0.0.1:{httpd.server_address[1]}"


def rate_file_bytes(n_items=200, code="97110", payer="Test Payer", day="2026-06-01", tin0=430000000):
    items = []
    for i in range(n_items):
        items.append({"negotiation_arrangement": "ffs", "name": code, "billing_code_type": "CPT",
                      "billing_code_type_version": "2026", "billing_code": code, "description": code,
                      "negotiated_rates": [{"provider_groups": [{"npi": [1000000000 + i],
                                                                 "tin": {"type": "ein", "value": str(tin0 + i)}}],
                                            "negotiated_prices": [{"negotiated_type": "negotiated",
                                                                   "negotiated_rate": 40.0 + (i % 7),
                                                                   "billing_class": "professional",
                                                                   "service_code": ["11"],
                                                                   "billing_code_modifier": ["GP"],
                                                                   "expiration_date": "9999-12-31"}]}]})
    doc = {"reporting_entity_name": payer, "reporting_entity_type": "issuer", "last_updated_on": day,
           "version": "2.0.0", "provider_references": [], "in_network": items}
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb", mtime=0) as g:
        g.write(json.dumps(doc).encode())
    return buf.getvalue()


def scratch_cfg(root: Path, **over) -> MrfxConfig:
    root.mkdir(parents=True, exist_ok=True)
    base = dict(inbox_dir=root / "inbox", processed_dir=root / "processed", failed_dir=root / "failed",
                store_dir=root / "store", downloads_dir=root / "downloads",
                entity_map_path=root / "em.yaml", confirm_over_gb=5.0)
    base.update(over)
    cfg = MrfxConfig(**base)
    cfg.enrichment.mode = "off"
    for d in (cfg.inbox_dir, cfg.processed_dir, cfg.failed_dir, cfg.store_dir, cfg.downloads_dir):
        d.mkdir(parents=True, exist_ok=True)
    return cfg


def open_store(cfg) -> Store:
    return Store(cfg.store_dir, cfg.duckdb_memory_gb, temp_dir=cfg.duckdb_temp_dir)


def queue_rows(store):
    with store.connect() as con:
        cur = con.execute("SELECT id, status, kind, retries, error, bytes_done, bytes_total FROM url_queue ORDER BY id")
        return [dict(zip([d[0] for d in cur.description], r)) for r in cur.fetchall()]
