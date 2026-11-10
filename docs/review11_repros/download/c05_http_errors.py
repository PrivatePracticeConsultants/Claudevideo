"""Case 5 — HTTP-level surprises, through the queue (row state is the output):
  html_200        200 text/html login page for a .json.gz URL
  zero_byte       200 with Content-Length: 0
  r429_retry_after 429 + Retry-After: 2 on the first 3 requests, then 200
  r503_forever    503 + Retry-After on every request: how long until what state
  r403_on_resume  200 cut at 300 KB, then 403 for the resume (signed URL expired)
  redirect_ext    /download?id=7 -> 302 -> /real/rates.json.gz : filename on disk
  redirect_loop   /loop -> 302 -> /loop
  redirect_to_html /file.json.gz -> 302 -> /login.html"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _harness import Srv, doc, gz, mk_cfg, quiet_logs, row, q, dl_files, brief  # noqa: E402

from mrfx.fetch import add_urls, filename_for, run_queue  # noqa: E402


def run(name, path, files, rules=None, behave=None, **cfgkw):
    cfg, store = mk_cfg("c05_" + name, **cfgkw)
    srv = Srv(files, rules or {})
    for p, b in (behave or {}).items():
        srv.behave[p] = b
    url = srv.base + path
    t0 = time.time()
    try:
        add_urls(store, [url])
        run_queue(cfg, store, drain=True)
    finally:
        srv.close()
    r = row(store, url)
    print(f"[{name}] {time.time() - t0:5.1f}s | {brief(r)}")
    print(f"    requests: {[(x['path'], x['status'], x['range'] or '-', x['sent']) for x in srv.log]}")
    print(f"    downloads dir: {dl_files(cfg)} | all rows: {q(store, 'SELECT status, kind FROM url_queue')}")
    return srv, cfg, store


if __name__ == "__main__":
    quiet_logs()
    good = gz(doc(n=300, pad_mb=1.0))
    html = b"<!doctype html><html><head><title>Sign in</title></head><body><form action='/login'>" \
           b"<input name=u><input name=p type=password></form></body></html>"

    run("html_200", "/file.json.gz", {"/file.json.gz": html},
        {"/file.json.gz": {"headers": {"Content-Type": "text/html"}}})
    run("zero_byte", "/file.json.gz", {"/file.json.gz": b""})

    def r429(h, body, rng, nth):
        if nth <= 3:
            h.send_response(429)
            h.send_header("Retry-After", "2")
            h.send_header("Content-Length", "0")
            h.end_headers()
            h._rec("/file.json.gz", "GET", rng, 429)
            return True
        return False
    run("r429_retry_after", "/file.json.gz", {"/file.json.gz": good}, behave={"/file.json.gz": r429},
        download_retries=4)

    run("r503_forever", "/file.json.gz", {"/file.json.gz": good},
        {"/file.json.gz": {"status": 503, "headers": {"Retry-After": "1"}}}, download_retries=4)

    def r403_on_resume(h, body, rng, nth):
        if rng:
            h.send_response(403)
            h.send_header("Content-Length", "0")
            h.end_headers()
            h._rec("/file.json.gz", "GET", rng, 403)
            return True
        return False
    run("r403_on_resume", "/file.json.gz", {"/file.json.gz": good},
        {"/file.json.gz": {"cut_at": [300 << 10]}}, behave={"/file.json.gz": r403_on_resume})

    def redirect(to):
        def b(h, body, rng, nth):
            h.send_response(302)
            h.send_header("Location", to)
            h.send_header("Content-Length", "0")
            h.end_headers()
            h._rec(h.path.split("?")[0], "GET", rng, 302)
            return True
        return b
    srv, cfg, store = run("redirect_ext", "/download?id=7", {"/real/rates.json.gz": good, "/download": b""},
                          behave={"/download": redirect("/real/rates.json.gz")})
    print("    filename_for(url):", filename_for(srv.base + "/download?id=7"))
    run("redirect_loop", "/loop", {"/loop": b""}, behave={"/loop": redirect("/loop")})
    run("redirect_to_html", "/file.json.gz", {"/file.json.gz": b"", "/login.html": html},
        {"/login.html": {"headers": {"Content-Type": "text/html"}}},
        behave={"/file.json.gz": redirect("/login.html")})
