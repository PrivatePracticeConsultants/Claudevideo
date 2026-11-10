"""Does the server stop working on a search nobody is waiting for any more?
Fire N one-letter name searches and hang up on each after 0.3 s (what the browser does when a
newer keystroke supersedes it — or would do, if it aborted), then time an exact-TIN search
(normally well under 0.2 s) every second until it is fast again.
usage: abandoned.py <port> <tin> [n=6]"""
import socket
import subprocess
import sys
import time
import urllib.request


def load():
    return subprocess.run(["uptime"], capture_output=True, text=True).stdout.split("load average:")[-1].strip()


def tin_search(port, tin):
    t0 = time.perf_counter()
    urllib.request.urlopen(f"http://localhost:{port}/api/rates?grain=tin&dollar_only=1&q={tin}&page_size=100", timeout=600).read()
    return time.perf_counter() - t0


if __name__ == "__main__":
    port, tin = int(sys.argv[1]), sys.argv[2]
    n = int(sys.argv[3]) if len(sys.argv) > 3 else 6
    print(f"baseline exact-TIN search: {[round(tin_search(port, tin), 3) for _ in range(3)]} s; load {load()}")
    t_start = time.perf_counter()
    for i in range(n):
        q = "practice"[: i + 1]
        for path in (f"/api/rates?grain=tin&dollar_only=1&q={q}&sort=negotiated_rate&dir=desc&page=1&page_size=100",
                     f"/api/summary?grain=tin&dollar_only=1&q={q}"):
            s = socket.create_connection(("localhost", port))
            s.sendall(f"GET {path} HTTP/1.1\r\nHost: localhost:{port}\r\nConnection: close\r\n\r\n".encode())
            s.settimeout(0.01)
            try:
                s.recv(1)
            except OSError:
                pass
            s.close()          # hang up: nobody will read this answer
        time.sleep(0.3)
    print(f"sent and abandoned {n} name searches (rates + summary each) in {time.perf_counter() - t_start:.1f} s")
    while True:
        dt = tin_search(port, tin)
        el = time.perf_counter() - t_start
        print(f"  t+{el:6.1f}s exact-TIN search took {dt:6.2f} s   load {load()}", flush=True)
        if dt < 0.3 or el > 300:
            break
        time.sleep(1)
