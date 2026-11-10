"""Download-side harness: local server on 8600-8649, gz bodies big enough for
the early check at early_dedup_mb=1."""
import base64, gzip, http.server as hs, json, random, shutil, threading, time, zlib
from pathlib import Path
import sys
sys.path.insert(0, "/home/user/Claudevideo")
from mrfx.config import MrfxConfig
from mrfx.store import Store

HERE = Path("/tmp/mrfx_review9/ingest")


def mk_cfg(tag, fresh=True, **kw):
    root = HERE / "runs" / tag
    if fresh:
        shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True, exist_ok=True)
    c = MrfxConfig(inbox_dir=root / "inbox", processed_dir=root / "processed",
                   failed_dir=root / "failed", store_dir=root / "store",
                   downloads_dir=root / "downloads", entity_map_path=root / "entity_map.yaml",
                   confirm_over_gb=5.0, **kw)
    c.ensure_dirs()
    return c, Store(c.store_dir)


def doc(rate_mid=40.0, n=300, payer="Twin Health", pad_mb=0.0, seed=1, month="2026-06-01"):
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
                        "billing_code_type_version": "2026", "billing_code": "97110",
                        "negotiated_rates": groups}]
    if pad_mb:   # incompressible filler in a NON-target item (after the target one)
        rnd = random.Random(seed)
        d["in_network"].append({"negotiation_arrangement": "ffs", "billing_code_type": "CPT",
                                "billing_code": "99991", "negotiated_rates": [],
                                "description": base64.b64encode(rnd.randbytes(int(pad_mb * 750_000))).decode()})
    return json.dumps(d).encode()


def gz(data: bytes) -> bytes:
    return gzip.compress(data, mtime=0)


class Srv:
    """files: path -> bytes. Options per instance; `behave[path]` may override
    with a callable(handler, body, rng) -> True if it handled the request."""
    def __init__(self, files, ranges=True, chunked=False, delay_per_chunk=0.0, head=True,
                 port_from=8600):
        self.files = files
        self.log = []          # (path, method, range, status, t)
        self.sent = {}
        self.behave = {}
        self.delay = {}
        self.gate = {}         # path -> threading.Event the body waits on after 1st chunk
        srv = self

        class H(hs.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_HEAD(self):
                path = self.path.split("?", 1)[0]
                body = srv.files.get(path)
                if not head or body is None:
                    srv.log.append((path, "HEAD", "", 405 if body is not None else 404, time.time()))
                    self.send_response(405 if body is not None else 404)
                    self.send_header("Content-Length", "0"); self.end_headers(); return
                srv.log.append((path, "HEAD", "", 200, time.time()))
                self.send_response(200)
                if not chunked:
                    self.send_header("Content-Length", str(len(body)))
                if ranges:
                    self.send_header("Accept-Ranges", "bytes")
                self.end_headers()

            def do_GET(self):
                path = self.path.split("?", 1)[0]
                rng = self.headers.get("Range", "")
                body = srv.files.get(path)
                if body is None:
                    srv.log.append((path, "GET", rng, 404, time.time()))
                    self.send_response(404); self.send_header("Content-Length", "0")
                    self.end_headers(); return
                b = srv.behave.get(path)
                if b and b(self, body, rng):
                    return
                start, end = 0, len(body) - 1
                if rng.startswith("bytes=") and ranges:
                    a, bb = rng[6:].split("-")
                    start = int(a)
                    end = min(int(bb) if bb else len(body) - 1, len(body) - 1)
                    self.send_response(206)
                    self.send_header("Content-Range", f"bytes {start}-{end}/{len(body)}")
                    st = 206
                else:
                    self.send_response(200); st = 200
                srv.log.append((path, "GET", rng, st, time.time()))
                chunk = body[start:end + 1]
                if chunked:
                    self.send_header("Transfer-Encoding", "chunked")
                else:
                    self.send_header("Content-Length", str(len(chunk)))
                self.end_headers()
                d = srv.delay.get(path, delay_per_chunk)
                try:
                    for i in range(0, len(chunk), 64 << 10):
                        piece = chunk[i:i + (64 << 10)]
                        if chunked:
                            self.wfile.write(b"%x\r\n" % len(piece) + piece + b"\r\n")
                        else:
                            self.wfile.write(piece)
                        self.wfile.flush()
                        srv.sent[path] = srv.sent.get(path, 0) + len(piece)
                        g = srv.gate.get(path)
                        if g is not None and not rng:
                            g.wait(120)
                        if d:
                            time.sleep(d)
                    if chunked:
                        self.wfile.write(b"0\r\n\r\n")
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def log_message(self, *a):
                pass

            def handle_error(self, *a):
                pass

        class S(hs.ThreadingHTTPServer):
            def handle_error(self, request, client_address):
                pass

        for port in range(port_from, 8650):
            try:
                self.httpd = S(("127.0.0.1", port), H)
                break
            except OSError:
                continue
        else:
            raise RuntimeError("no free port 8600-8649")
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def hits(self, path, method="GET"):
        return [x for x in self.log if x[0] == path and x[1] == method]

    def close(self):
        self.httpd.shutdown(); self.httpd.server_close()


def row(store, url):
    with store.connect() as con:
        cur = con.execute("SELECT * FROM url_queue WHERE url = ?", [url])
        r = cur.fetchone()
        return dict(zip([d[0] for d in cur.description], r)) if r else None


def q(store, sql, *a):
    with store.connect() as con:
        return con.execute(sql, list(a)).fetchall()
