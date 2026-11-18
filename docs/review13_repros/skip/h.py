"""Scratch harness for the round-11 diff review (copy of r11/harness.py ideas,
self-contained). MRFX_SRC env var picks the mrfx tree (new repo by default,
or the extracted 1064203 copy)."""
import gzip
import http.server
import io
import json
import os
import sys
import threading
import time
from pathlib import Path

SRC = os.environ.get("MRFX_SRC", "/home/user/Claudevideo")
sys.path.insert(0, SRC)
from mrfx.config import MrfxConfig  # noqa: E402
from mrfx.store import Store  # noqa: E402

ROUTES: dict = {}
LOG: list = []          # (method, path, headers dict, bytes_written, status)
SCRATCH = Path(__file__).parent / "scratch"


class H(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_HEAD(self):
        self._serve(True)

    def do_GET(self):
        self._serve(False)

    def _serve(self, head):
        fn = ROUTES.get(self.path.split("?")[0])
        entry = [self.command, self.path, dict(self.headers), 0, None]
        LOG.append(entry)
        if fn is None:
            self.send_response(404); self.send_header("Content-Length", "0"); self.end_headers()
            entry[4] = 404
            return
        try:
            fn(self, head, entry)
        except (BrokenPipeError, ConnectionResetError):
            pass


def start_server():
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, f"http://127.0.0.1:{httpd.server_address[1]}"


def send(h, entry, status, headers, body, cut=None, pace=0.0, step=64 << 10, head=False):
    """Write a response; `cut` = close after that many body bytes; `pace` = sleep
    per `step` bytes. Records bytes actually written in entry[3]."""
    h.send_response(status)
    for k, v in headers.items():
        h.send_header(k, v)
    if "Content-Length" not in headers and "Transfer-Encoding" not in headers:
        h.send_header("Content-Length", str(len(body)))
    h.end_headers()
    entry[4] = status
    if head:
        return
    out = body if cut is None else body[:cut]
    chunked = headers.get("Transfer-Encoding") == "chunked"
    try:
        for i in range(0, len(out), step):
            piece = out[i:i + step]
            if chunked:
                h.wfile.write(f"{len(piece):x}\r\n".encode() + piece + b"\r\n")
            else:
                h.wfile.write(piece)
            h.wfile.flush()
            entry[3] += len(piece)
            if pace:
                time.sleep(pace)
        if chunked:
            h.wfile.write(b"0\r\n\r\n")
    except OSError:
        pass
    if cut is not None:
        h.close_connection = True


def parse_range(h, size):
    rng = h.headers.get("Range")
    if not rng:
        return None
    a, b = rng.split("=", 1)[1].split("-")
    a = int(a)
    b = int(b) if b else size - 1
    return a, b


def rate_file_bytes(n_items=200, code="97110", payer="Test Payer", day="2026-06-01",
                    tin0=430000000, rate0=40.0, pad=0, gz=True):
    items = []
    for i in range(n_items):
        items.append({"negotiation_arrangement": "ffs", "name": code, "billing_code_type": "CPT",
                      "billing_code_type_version": "2026", "billing_code": code, "description": code,
                      "negotiated_rates": [{"provider_groups": [{"npi": [1000000000 + i],
                                                                 "tin": {"type": "ein", "value": str(tin0 + i)}}],
                                            "negotiated_prices": [{"negotiated_type": "negotiated",
                                                                   "negotiated_rate": rate0 + (i % 7),
                                                                   "billing_class": "professional",
                                                                   "service_code": ["11"],
                                                                   "billing_code_modifier": ["GP"],
                                                                   "expiration_date": "9999-12-31"}]}]})
    if pad:
        items.append({"negotiation_arrangement": "ffs", "billing_code_type": "CPT",
                      "billing_code": "99991", "negotiated_rates": [], "description": "x" * pad})
    doc = {"reporting_entity_name": payer, "reporting_entity_type": "issuer", "last_updated_on": day,
           "version": "2.0.0", "provider_references": [], "in_network": items}
    raw = json.dumps(doc).encode()
    if not gz:
        return raw
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb", mtime=0) as g:
        g.write(raw)
    return buf.getvalue()


def scratch_cfg(name: str, **over) -> MrfxConfig:
    import shutil
    root = SCRATCH / name
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True)
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


def queue_rows(store, cols="id, status, kind, retries, error, bytes_done, bytes_total"):
    with store.connect() as con:
        cur = con.execute(f"SELECT {cols} FROM url_queue ORDER BY id")
        return [dict(zip([d[0] for d in cur.description], r)) for r in cur.fetchall()]


def drain(cfg, store, secs=60, settle=None):
    from mrfx.fetch import run_queue
    stop = threading.Event()
    th = threading.Thread(target=lambda: run_queue(cfg, store, stop=stop), daemon=True)
    th.start()
    t = time.monotonic() + secs
    while time.monotonic() < t:
        rows = queue_rows(store)
        if rows and all(r["status"] in ("done", "failed", "skipped", "oversize", "paused") for r in rows):
            break
        time.sleep(0.3)
    stop.set(); th.join(10)
    return th.is_alive()


def requests_since(n):
    return [(m, p, h.get("Range"), w, s) for m, p, h, w, s in LOG[n:]]
