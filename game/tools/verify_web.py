#!/usr/bin/env python3
"""Prove the exported web build actually boots and renders in a real browser.

    python3 game/tools/verify_web.py [--build build/web] [--shot build/web_verify.png]

Exits non-zero if the canvas never comes up or the page throws, so it drops
straight into CI alongside run_tests.sh.

Why this exists rather than "the export succeeded, ship it": a Godot web export
can build cleanly and still fail to run, and the usual causes are invisible from
the command line. The renderer might not be gl_compatibility; the build might
need SharedArrayBuffer and die on a host without cross-origin-isolation headers;
the wasm might 404 behind a misconfigured path. All of those look like a black
rectangle to a playtester and like success to the exporter.

The server below is deliberately a plain static file server with NO COOP/COEP
headers, so a build that quietly depends on cross-origin isolation fails here
instead of on itch.io.
"""

import argparse
import functools
import http.server
import os
import socketserver
import sys
import threading
import time

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# Playwright's bundled browser version often disagrees with the one a sandbox
# ships. Prefer an explicitly provided binary, then the usual preinstall path.
CHROME_CANDIDATES = [
    os.environ.get("CHROME_PATH", ""),
    "/opt/pw-browsers/chromium-1194/chrome-linux/chrome",
]


def find_chrome():
    for path in CHROME_CANDIDATES:
        if path and os.path.exists(path):
            return path
    for root in ("/opt/pw-browsers",):
        if not os.path.isdir(root):
            continue
        for entry in sorted(os.listdir(root), reverse=True):
            candidate = os.path.join(root, entry, "chrome-linux", "chrome")
            if os.path.exists(candidate):
                return candidate
    return None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--build", default=os.path.join(REPO, "build", "web"))
    parser.add_argument("--shot", default=os.path.join(REPO, "build", "web_verify.png"))
    parser.add_argument("--port", type=int, default=8791)
    parser.add_argument("--settle", type=float, default=8.0,
                        help="seconds of real gameplay to run before screenshotting")
    args = parser.parse_args()

    index = os.path.join(args.build, "index.html")
    if not os.path.exists(index):
        print(f"No build at {index}. Run game/build_web.sh first.", file=sys.stderr)
        return 2

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("playwright is not installed (pip install playwright).", file=sys.stderr)
        return 2

    chrome = find_chrome()
    if chrome is None:
        print("No Chromium binary found; set CHROME_PATH.", file=sys.stderr)
        return 2

    handler = functools.partial(QuietHandler, directory=args.build)
    socketserver.TCPServer.allow_reuse_address = True
    httpd = socketserver.TCPServer(("127.0.0.1", args.port), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    errors = []
    logs = []
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                executable_path=chrome,
                args=["--use-gl=swiftshader", "--enable-unsafe-swiftshader"])
            page = browser.new_page(viewport={"width": 1280, "height": 720})
            page.on("console", lambda m: logs.append(f"{m.type}: {m.text}"))
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.goto(f"http://127.0.0.1:{args.port}/index.html",
                      wait_until="load", timeout=60000)

            # Godot boots asynchronously: fetch wasm, compile, then start the
            # main loop. Poll for the canvas rather than guessing a sleep.
            booted = False
            deadline = time.time() + 120
            while time.time() < deadline:
                size = page.evaluate(
                    "() => { const c = document.getElementById('canvas');"
                    "  return c ? [c.width, c.height] : [0, 0]; }")
                if size[0] > 16 and size[1] > 16:
                    booted = True
                    break
                time.sleep(1)

            time.sleep(args.settle)
            os.makedirs(os.path.dirname(args.shot), exist_ok=True)
            page.screenshot(path=args.shot)
            state = page.evaluate(
                "() => ({ isolated: window.crossOriginIsolated,"
                "         sab: typeof SharedArrayBuffer !== 'undefined' })")
            browser.close()
    finally:
        httpd.shutdown()

    print(f"canvas booted:       {booted}")
    print(f"crossOriginIsolated: {state['isolated']}  "
          f"(False proves the build needs no COOP/COEP headers)")
    print(f"screenshot:          {args.shot}")
    if errors:
        print("page errors:")
        for e in errors:
            print(f"  {e}")
    for line in logs:
        if "Godot Engine" in line or "OpenGL" in line or "Build configuration" in line:
            print(f"  {line}")
    return 0 if booted and not errors else 1


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *_args):
        pass


if __name__ == "__main__":
    sys.exit(main())
