"""An iframe download's bar after the server restarts mid-build: the poll gets
404 'unknown run' forever (startPoll ignores non-OK answers; the only end is
the 6-hour timer) — the bar stays at its last words and the anchor button
stays disabled. Fetch-based outputs end at 'the server restarted'."""
import subprocess, sys, time
from pathlib import Path
from ui_common import *
HERE = Path(__file__).parent
BASE = "http://localhost:8792"

def start():
    p = subprocess.Popen([sys.executable, str(HERE / "ui_harness_server.py"), "150000000"],
                         stdout=open(HERE / "harness_ui05.log", "a"), stderr=subprocess.STDOUT)
    for _ in range(60):
        try:
            import urllib.request; urllib.request.urlopen(BASE + "/api/stats", timeout=2); return p
        except Exception: time.sleep(0.5)
    return p

def main():
    srv = start()
    try:
        with sync_playwright() as pw:
            b, ctx = launch(pw)
            page = ctx.new_page(); rec = Rec(page)
            page.goto(BASE + "/"); page.wait_for_timeout(2500)
            page.click('nav button[data-view="explorer"]'); page.wait_for_timeout(2000)
            page.click("#btn-export"); page.wait_for_timeout(1500)
            print("bar before restart:", page.evaluate("[...document.querySelectorAll('#jobbar-explorer .jobbar')].map(e=>e.innerText)"))
            srv.terminate(); srv.wait(20)
            srv = start()
            page.wait_for_timeout(20000)
            print("bar 20 s after the restart:", page.evaluate("[...document.querySelectorAll('#jobbar-explorer .jobbar')].map(e=>e.innerText)"),
                  "| #btn-export disabled:", page.evaluate("document.querySelector('#btn-export').disabled"))
            polls = [x for x in rec.bad if "/api/progress/" in x]
            print("404 polls after the restart:", len(polls), polls[-1:] )
            page.screenshot(path=str(SHOTS / "ui05_export_bar_after_restart.png"))
            b.close()
    finally:
        srv.terminate(); srv.wait(20)

if __name__ == "__main__":
    main()
