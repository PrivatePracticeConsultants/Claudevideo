"""Shared harness for the review-11 URL-queue repros.

Everything runs against a scratch MrfxConfig under /tmp/mrfx_review11/queue/<name>
(never the repo's data/) and a local http.server on port 0.

Usage from a repro script:
    from h import *            # noqa
    root, cfg, store = world("s1")
    srv = Server({"/a.json.gz": rate_file_bytes("a")})
    add_urls(store, [srv.url("/a.json.gz")]); drain(cfg, store)
    print(table(store))
"""
from __future__ import annotations

import gzip
import http.server as hs
import json
import logging
import shutil
import sys
import threading
import time
from pathlib import Path

REPO = Path("/home/user/Claudevideo")
sys.path.insert(0, str(REPO))
SCRATCH = Path("/tmp/mrfx_review11/queue")
FIXTURES = REPO / "tests/mrfx/fixtures"

from mrfx.config import MrfxConfig  # noqa: E402
from mrfx.fetch import add_urls, dedup_key, filename_for, run_queue  # noqa: E402,F401
from mrfx.store import Store  # noqa: E402

logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
LOG_LINES: list[str] = []


class _Capture(logging.Handler):
    def emit(self, record):
        LOG_LINES.append(self.format(record))


def capture_logs(level=logging.INFO):
    h = _Capture()
    h.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    logging.getLogger("mrfx").setLevel(level)
    logging.getLogger("mrfx").addHandler(h)
    return LOG_LINES


def world(name: str, fresh: bool = True, **over):
    root = SCRATCH / name
    if fresh and root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True, exist_ok=True)
    cfg = make_cfg(root, **over)
    store = Store(cfg.store_dir)
    return root, cfg, store


def make_cfg(root: Path, **over) -> MrfxConfig:
    kw = dict(
        inbox_dir=root / "inbox", processed_dir=root / "processed", failed_dir=root / "failed",
        store_dir=root / "store", downloads_dir=root / "downloads",
        entity_map_path=root / "entity_map.yaml",
        confirm_over_gb=5.0, render_js=False, parallel_ingests=1, parallel_downloads=1,
        download_timeout_seconds=5.0, download_retries=1,
        early_dedup_mb=0,            # the early check is its own subject; off unless a repro turns it on
    )
    kw.update(over)
    c = MrfxConfig(**kw)
    c.ensure_dirs()
    return c


# ---------------------------------------------------------------- fixtures

def _h(s: str) -> int:
    import zlib
    return zlib.crc32(s.encode())


def rate_file_obj(tag: str, n_rows: int = 2, entity: str = "Testco Health", month: str = "2026-05-01",
                  pad: int = 0):
    """A valid in-network file: n_rows provider groups on CPT 97110 (in the
    default target set), distinct content per tag. `pad` bytes of incompressible
    hex at the END make a 'big' file that still parses."""
    base = json.loads((FIXTURES / "innetwork_mixed.json").read_text())
    base["reporting_entity_name"] = entity
    base["last_updated_on"] = month
    item = json.loads(json.dumps(base["in_network"][0]))
    proto = item["negotiated_rates"][0]
    groups = []
    for i in range(n_rows):
        g = json.loads(json.dumps(proto))
        g["provider_groups"][0]["npi"] = [1000000000 + (_h(tag) % 899_999_999) + i]
        g["provider_groups"][0]["tin"]["value"] = f"{(_h(tag) % 90 + 10):02d}-{(i * 7919 + _h(tag)) % 10_000_000:07d}"
        for np_ in g["negotiated_prices"]:
            np_["negotiated_rate"] = round(20 + (_h(tag + str(i)) % 1000) / 10, 2)
        groups.append(g)
    item["negotiated_rates"] = groups
    base["in_network"] = [item]
    base.pop("provider_references", None)
    base["_tag"] = tag
    if pad:
        import os
        base["_pad"] = os.urandom(pad // 2).hex()
    return base


def rate_file_bytes(tag: str, gz: bool = True, **kw) -> bytes:
    raw = json.dumps(rate_file_obj(tag, **kw)).encode()
    return gzip.compress(raw, mtime=0) if gz else raw


def toc_obj(locations, plans=None, entity="Testco Health"):
    plans = plans or [{"plan_name": "Plan A", "plan_id_type": "EIN", "plan_id": "1",
                       "plan_market_type": "group"}]
    return {"reporting_entity_name": entity, "reporting_entity_type": "health insurance issuer",
            "version": "2.0.0",
            "reporting_structure": [{"reporting_plans": plans,
                                     "in_network_files": [{"description": "f", "location": u}
                                                          for u in locations]}]}


def toc_bytes(locations, **kw) -> bytes:
    return json.dumps(toc_obj(locations, **kw)).encode()


# ---------------------------------------------------------------- server

class Server:
    """routes: path -> bytes | dict(body=, status=, headers=, delay=, chunked=, stall=,
    redirect=) | callable(handler) -> None. Records every request."""

    def __init__(self, routes: dict):
        self.routes = dict(routes)
        self.log: list[tuple] = []      # (path, range, t)
        self.release = threading.Event()
        srv = self

        class H(hs.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self):
                srv.log.append((self.path, self.headers.get("Range"), time.monotonic()))
                r = srv.routes.get(self.path.split("?")[0])
                if r is None:
                    self.send_response(404); self.send_header("Content-Length", "0"); self.end_headers(); return
                if callable(r):
                    return r(self)
                if isinstance(r, (bytes, bytearray)):
                    r = {"body": bytes(r)}
                if "redirect" in r:
                    self.send_response(r.get("status", 302)); self.send_header("Location", r["redirect"])
                    self.send_header("Content-Length", "0"); self.end_headers(); return
                body = r.get("body", b"")
                st = r.get("status", 200)
                rng = self.headers.get("Range")
                start = 0
                if rng and r.get("ranges", True) and st == 200:
                    start = int(rng.split("=")[1].split("-")[0])
                    if start >= len(body):
                        self.send_response(416); self.send_header("Content-Length", "0"); self.end_headers(); return
                    self.send_response(206)
                    self.send_header("Content-Range", f"bytes {start}-{len(body)-1}/{len(body)}")
                else:
                    self.send_response(st)
                for k, v in (r.get("headers") or {}).items():
                    self.send_header(k, v)
                chunk = body[start:]
                if r.get("chunked"):
                    self.send_header("Transfer-Encoding", "chunked")
                else:
                    self.send_header("Content-Length", str(len(chunk)))
                self.end_headers()
                if r.get("delay"):
                    time.sleep(r["delay"])
                send_upto = r.get("stall_after")
                piece = r.get("piece", 64 << 10)
                sent = 0
                try:
                    while sent < len(chunk):
                        if send_upto is not None and sent >= send_upto:
                            srv.release.wait(r.get("stall_for", 120)); break
                        p = chunk[sent:sent + piece]
                        if r.get("chunked"):
                            self.wfile.write(f"{len(p):x}\r\n".encode() + p + b"\r\n")
                        else:
                            self.wfile.write(p)
                        self.wfile.flush()
                        sent += len(p)
                        if r.get("pace"):
                            time.sleep(r["pace"])
                    if r.get("chunked") and (send_upto is None or sent < send_upto):
                        self.wfile.write(b"0\r\n\r\n")
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def log_message(self, *a):
                pass

        self.httpd = hs.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def url(self, path: str) -> str:
        return self.base + path

    def hits(self, path_prefix: str) -> list:
        return [x for x in self.log if x[0].split("?")[0] == path_prefix]

    def stop(self):
        self.release.set()
        self.httpd.shutdown()


# ---------------------------------------------------------------- inspection

def q(store, sql, params=None):
    with store.connect() as con:
        cur = con.execute(sql, params or [])
        cols = [d[0] for d in cur.description] if cur.description else []
        rows = cur.fetchall()
    return [dict(zip(cols, r)) for r in rows]


QCOLS = ("id", "status", "kind", "parent_id", "park_count", "auto_resumes", "retries",
         "force_size", "big_lane", "waiting_on", "bytes_total", "rows_emitted", "child_count",
         "content_sha", "filename", "error")


def rows(store, cols=QCOLS):
    out = q(store, f"SELECT {', '.join(cols)}, url FROM url_queue ORDER BY id")
    for r in out:
        if r.get("content_sha"):
            r["content_sha"] = r["content_sha"][:8]
        if r.get("error"):
            r["error"] = r["error"][:90]
        r["url"] = r["url"].split("?")[0].rsplit("/", 1)[-1] + ("?" + r["url"].split("?", 1)[1][:20] if "?" in r["url"] else "")
    return out


def table(store, cols=QCOLS):
    out = []
    for r in rows(store, cols):
        out.append(" | ".join(f"{k}={r[k]}" for k in (*cols, "url") if r.get(k) not in (None, 0, False, "")))
    return "\n".join(out)


def files_table(store):
    return q(store, "SELECT filename, status, rows_emitted, payer, file_type FROM files ORDER BY filename")


def rates_per_file(store):
    try:
        return {r["source_file"]: r["n"] for r in q(
            store, "SELECT source_file, count(*) AS n FROM rates GROUP BY 1 ORDER BY 1")}
    except Exception as e:  # noqa: BLE001
        return {"<no rates view>": str(e)[:60]}


def parts(cfg):
    return sorted(p.name for p in (cfg.store_dir / "rates").glob("*")) if (cfg.store_dir / "rates").exists() else []


def downloads(cfg):
    return sorted(p.name for p in cfg.downloads_dir.iterdir())


# ---------------------------------------------------------------- driving

def drain(cfg, store, timeout=90):
    done = {}

    def run():
        try:
            done["n"] = run_queue(cfg, store, drain=True)
        except BaseException as e:  # noqa: BLE001
            done["exc"] = repr(e)
    t = threading.Thread(target=run, daemon=True)
    t0 = time.monotonic()
    t.start(); t.join(timeout)
    done["secs"] = round(time.monotonic() - t0, 2)
    done["finished"] = not t.is_alive()
    return done


def serve(cfg, store):
    """run_queue in serve mode (stop event); returns (stop, thread)."""
    stop = threading.Event()
    t = threading.Thread(target=run_queue, args=(cfg, store), kwargs={"stop": stop}, daemon=True)
    t.start()
    return stop, t


def wait_for(pred, timeout=30, every=0.1):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        v = pred()
        if v:
            return v
        time.sleep(every)
    return None


def status_of(store, url_frag):
    r = [x for x in q(store, "SELECT id, status, kind, error, url FROM url_queue") if url_frag in x["url"]]
    return r[0] if len(r) == 1 else r


def banner(s):
    print("\n" + "=" * 78 + "\n" + s + "\n" + "=" * 78)
