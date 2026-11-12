"""A static file server that spreads every GET body over SECONDS seconds
(a slow payer CDN), so a trickle of links keeps the queue busy."""
import http.server
import os
import sys
import time


class H(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        secs = float(os.environ.get("THR_SECONDS", "10"))
        path = self.translate_path(self.path)
        if not os.path.isfile(path):
            self.send_error(404)
            return
        data = open(path, "rb").read()
        rng = self.headers.get("Range")
        if rng and rng.startswith("bytes=0-0"):
            self.send_response(206)
            self.send_header("Content-Range", f"bytes 0-0/{len(data)}")
            self.send_header("Content-Length", "1")
            self.send_header("Content-Type", "application/gzip")
            self.end_headers()
            self.wfile.write(data[:1])
            return
        self.send_response(200)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Content-Type", "application/gzip")
        self.end_headers()
        n = 20
        step = max(1, len(data) // n + 1)
        for i in range(0, len(data), step):
            try:
                self.wfile.write(data[i:i + step])
                self.wfile.flush()
            except OSError:
                return
            time.sleep(secs / n)


if __name__ == "__main__":
    port, d = int(sys.argv[1]), sys.argv[2]
    os.chdir(d)
    http.server.ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever()
