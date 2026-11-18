"""A local file server that paces each response (bytes/sec per connection),
so a kill lands while downloads are genuinely in flight.
usage: throttle_server.py <port> <root> <bytes_per_sec>"""
import http.server
import sys
import time


class H(http.server.SimpleHTTPRequestHandler):
    rate = 2_000_000

    def log_message(self, *a):
        pass

    def copyfile(self, src, dst):
        step = 256 << 10
        t0 = time.monotonic()
        sent = 0
        while True:
            b = src.read(step)
            if not b:
                return
            dst.write(b)
            sent += len(b)
            ahead = sent / self.rate - (time.monotonic() - t0)
            if ahead > 0:
                time.sleep(ahead)


if __name__ == "__main__":
    port, root, rate = int(sys.argv[1]), sys.argv[2], int(sys.argv[3])
    H.rate = rate
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), lambda *a, **k: H(*a, directory=root, **k))
    srv.serve_forever()
