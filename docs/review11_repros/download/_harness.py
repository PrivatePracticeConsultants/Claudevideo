"""Shared harness for the round-11 download review: a scriptable hostile HTTP
server (threading, port 0) + scratch MrfxConfig/Store under
/tmp/mrfx_review11/download. Never points at the repo's data/.

Each case script imports this and is runnable standalone:
    /home/user/Claudevideo/.venv/bin/python docs/review11_repros/download/cNN_*.py
"""
from __future__ import annotations

import base64
import gzip
import hashlib
import http.server as hs
import json
import logging
import random
import shutil
import socketserver
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, "/home/user/Claudevideo")

from mrfx.config import MrfxConfig  # noqa: E402
from mrfx.store import Store  # noqa: E402

ROOT = Path("/tmp/mrfx_review11/download")
ROOT.mkdir(parents=True, exist_ok=True)


def quiet_logs(level=logging.WARNING):
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def mk_cfg(tag: str, fresh: bool = True, **kw):
    """Scratch config + store under ROOT/runs/<tag>. Small knobs by default so
    every case runs in seconds; override per case."""
    root = ROOT / "runs" / tag
    if fresh:
        shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True, exist_ok=True)
    base = dict(inbox_dir=root / "inbox", processed_dir=root / "processed",
                failed_dir=root / "failed", store_dir=root / "store",
                downloads_dir=root / "downloads", entity_map_path=root / "entity_map.yaml",
                confirm_over_gb=5.0, parallel_ingests=1, parallel_downloads=1,
                early_dedup_mb=1, download_timeout_seconds=5.0, download_retries=2,
                download_stall_seconds=30.0, download_park_seconds=0.0,
                download_max_seconds=0.0, download_pause_hours=0.0,
                render_js=False)
    base.update(kw)
    c = MrfxConfig(**base)
    c.ensure_dirs()
    return c, Store(c.store_dir)


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def sha_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for blk in iter(lambda: f.read(1 << 20), b""):
            h.update(blk)
    return h.hexdigest()


def doc(rate_mid=40.0, n=300, payer="Twin Health", pad_mb=0.0, seed=1, month="2026-06-01",
        code="97110") -> bytes:
    """A small valid in-network MRF; pad_mb adds incompressible filler in a
    NON-target item so the gz has a controllable size."""
    groups = []
    for i in range(n):
        r = rate_mid if i == n // 2 else 40.0
        groups.append({"provider_groups": [{"npi": [1000000000 + i],
                                            "tin": {"type": "ein", "value": f"43{i:07d}"}}],
                       "negotiated_prices": [{"negotiated_type": "negotiated",
                                              "negotiated_rate": r, "billing_class": "professional",
                                              "service_code": ["11"], "billing_code_modifier": ["GP"]}]})
    d = {"reporting_entity_name": payer, "reporting_entity_type": "issuer",
         "last_updated_on": month, "version": "2.0.0"}
    d["in_network"] = [{"negotiation_arrangement": "ffs", "billing_code_type": "CPT",
                        "billing_code_type_version": "2026", "billing_code": code,
                        "negotiated_rates": groups}]
    if pad_mb:
        rnd = random.Random(seed)
        d["in_network"].append({"negotiation_arrangement": "ffs", "billing_code_type": "CPT",
                                "billing_code": "99991", "negotiated_rates": [],
                                "description": base64.b64encode(rnd.randbytes(int(pad_mb * 750_000))).decode()})
    return json.dumps(d).encode()


def gz(data: bytes, level: int = 6) -> bytes:
    return gzip.compress(data, compresslevel=level, mtime=0)


class Srv:
    """files: path -> bytes. Per-path `rules[path]` dict keys:
        ranges (bool, default True)   honour Range with 206 (+Accept-Ranges)
        accept_ranges (bool)          send the Accept-Ranges header (default = ranges)
        chunked (bool)                Transfer-Encoding: chunked, no Content-Length
        delay (float)                 sleep per 64 KB piece
        cut_at (int|list[int])        close the socket after this many body
                                      bytes of a response (list: per request n)
        etag / last_modified (str)    validators
        content_range (callable)      (start,end,total) -> header value or None
        status (int)                  force a status for every request
        headers (dict)                extra headers
        body_for_status (bytes)       body sent with a forced status
    `behave[path]` = callable(handler, body, range_header, nth) -> True when it
    handled the request itself (full control)."""

    def __init__(self, files: dict, rules: dict | None = None):
        self.files = dict(files)
        self.rules = rules or {}
        self.behave = {}
        self.log = []           # dicts: path, method, range, status, sent, t, headers
        self.count = {}
        self.lock = threading.Lock()
        srv = self

        class H(hs.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def _nth(self, path):
                with srv.lock:
                    srv.count[path] = srv.count.get(path, 0) + 1
                    return srv.count[path]

            def _rec(self, path, method, rng, status, sent=0, extra=None):
                e = {"path": path, "method": method, "range": rng, "status": status,
                     "sent": sent, "t": time.time(), "req_headers": dict(self.headers)}
                if extra:
                    e.update(extra)
                with srv.lock:
                    srv.log.append(e)
                return e

            def do_HEAD(self):
                self.do_GET(head=True)

            def do_GET(self, head=False):
                path = self.path.split("?", 1)[0]
                rng = self.headers.get("Range", "") or ""
                nth = self._nth(path)
                b = srv.behave.get(path)
                body = srv.files.get(path)
                if b is not None:
                    if b(self, body, rng, nth):
                        return
                    body = srv.files.get(path)   # behave may have swapped the content
                if body is None:
                    self._rec(path, "GET", rng, 404)
                    self.send_response(404)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                r = srv.rules.get(path, {})
                if r.get("status"):
                    st = r["status"]
                    bb = r.get("body_for_status", b"")
                    self._rec(path, "GET", rng, st)
                    self.send_response(st)
                    for k, v in (r.get("headers") or {}).items():
                        self.send_header(k, v)
                    self.send_header("Content-Length", str(len(bb)))
                    self.end_headers()
                    self.wfile.write(bb)
                    return
                ranges = r.get("ranges", True)
                total = len(body)
                start, end = 0, total - 1
                st = 200
                if rng.startswith("bytes=") and ranges:
                    a, bb_ = rng[6:].split("-", 1)
                    a = int(a) if a else None
                    if a is None:   # suffix range bytes=-N
                        start = max(0, total - int(bb_))
                    else:
                        start = a
                        end = min(int(bb_), total - 1) if bb_ else total - 1
                    if start >= total:
                        self._rec(path, "GET", rng, 416)
                        self.send_response(416)
                        self.send_header("Content-Range", f"bytes */{total}")
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                        return
                    st = 206
                self.send_response(st)
                if r.get("accept_ranges", ranges):
                    self.send_header("Accept-Ranges", "bytes")
                if st == 206:
                    crf = r.get("content_range")
                    cr = crf(start, end, total) if crf else f"bytes {start}-{end}/{total}"
                    if cr is not None:
                        self.send_header("Content-Range", cr)
                if r.get("etag"):
                    self.send_header("ETag", r["etag"])
                if r.get("last_modified"):
                    self.send_header("Last-Modified", r["last_modified"])
                for k, v in (r.get("headers") or {}).items():
                    self.send_header(k, v)
                piece_all = body[start:end + 1]
                chunked = r.get("chunked", False)
                if chunked:
                    self.send_header("Transfer-Encoding", "chunked")
                else:
                    self.send_header("Content-Length", str(len(piece_all)))
                self.end_headers()
                if head:
                    self._rec(path, "HEAD", rng, st)
                    return
                cut = r.get("cut_at")
                if isinstance(cut, list):
                    cut = cut[nth - 1] if nth - 1 < len(cut) else None
                rec = self._rec(path, "GET", rng, st)
                sent = 0
                d = r.get("delay", 0.0)
                try:
                    for i in range(0, len(piece_all), 64 << 10):
                        piece = piece_all[i:i + (64 << 10)]
                        if cut is not None and sent + len(piece) > cut:
                            piece = piece[:max(0, cut - sent)]
                            if piece:
                                self.wfile.write((b"%x\r\n" % len(piece) + piece + b"\r\n")
                                                 if chunked else piece)
                                self.wfile.flush()
                                sent += len(piece)
                            rec["sent"] = sent
                            rec["cut"] = True
                            # hard close: no terminating chunk, short body
                            self.close_connection = True
                            try:
                                self.connection.shutdown(1)
                            except OSError:
                                pass
                            return
                        self.wfile.write((b"%x\r\n" % len(piece) + piece + b"\r\n")
                                         if chunked else piece)
                        self.wfile.flush()
                        sent += len(piece)
                        rec["sent"] = sent
                        if d:
                            time.sleep(d)
                    if chunked:
                        self.wfile.write(b"0\r\n\r\n")
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    rec["client_closed"] = True

        class S(socketserver.ThreadingMixIn, hs.HTTPServer):
            daemon_threads = True
            allow_reuse_address = True

            def handle_error(self, request, client_address):
                pass

        self.httpd = S(("127.0.0.1", 0), H)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.port = self.httpd.server_address[1]
        self.base = f"http://127.0.0.1:{self.port}"

    def hits(self, path, method="GET"):
        with self.lock:
            return [x for x in self.log if x["path"] == path and x["method"] == method]

    def sent_bytes(self, path):
        return sum(x["sent"] for x in self.hits(path))

    def describe(self, path):
        return [(x["status"], x["range"] or "-", x["sent"]) for x in self.hits(path)]

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def row(store, url):
    with store.connect() as con:
        cur = con.execute("SELECT * FROM url_queue WHERE url = ?", [url])
        r = cur.fetchone()
        return dict(zip([d[0] for d in cur.description], r)) if r else None


def rows(store):
    with store.connect() as con:
        cur = con.execute("SELECT * FROM url_queue ORDER BY id")
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


def q(store, sql, *a):
    with store.connect() as con:
        return con.execute(sql, list(a)).fetchall()


def brief(r):
    if r is None:
        return "None"
    return (f"status={r['status']} kind={r.get('kind')} bytes_done={r.get('bytes_done')} "
            f"total={r.get('bytes_total')} sha={(r.get('content_sha') or '')[:12]} "
            f"err={(r.get('error') or '')[:150]!r}")


def dl_files(cfg):
    return sorted((p.name, p.stat().st_size) for p in cfg.downloads_dir.iterdir())
