"""Walk every dashboard view of the shipped build in a real browser (the
live_install store holds one real UHC file, 252 rows): collect console
errors, failed requests and 5xx answers per view."""
import json
import os
import subprocess
import time
import urllib.request
from pathlib import Path

A = Path("/tmp/claude-0/-home-user-Claudevideo/4633d594-0139-5f8e-a935-ae9c76b72000/scratchpad/audit12")
SRC = A / "unzipped" / "Claudevideo"
DST = A / "live_install"
PORT = 8475


def main():
    from playwright.sync_api import sync_playwright
    py = SRC / ".venv" / "bin" / "python"
    env = dict(os.environ, PYTHONUTF8="1")
    env.pop("MRFX_CONFIG", None)
    log = open(A / "ui_walk_serve.log", "w")
    p = subprocess.Popen([str(py), "-m", "mrfx", "serve"], cwd=DST, env=env, stdout=log, stderr=subprocess.STDOUT)
    try:
        for _ in range(120):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{PORT}/api/files", timeout=2).read()
                break
            except Exception:
                time.sleep(0.5)
        with sync_playwright() as pw:
            b = pw.chromium.launch(executable_path="/opt/pw-browsers/chromium", args=["--no-sandbox"])
            page = b.new_page(viewport={"width": 1400, "height": 900})
            events = []
            page.on("console", lambda m: events.append(("console", m.type, m.text[:200])) if m.type in ("error", "warning") else None)
            page.on("pageerror", lambda e: events.append(("pageerror", "", str(e)[:200])))
            page.on("requestfailed", lambda r: events.append(("requestfailed", r.url[-80:], r.failure)))
            page.on("response", lambda r: events.append(("http", r.status, r.url[-100:])) if r.status >= 400 else None)
            page.goto(f"http://127.0.0.1:{PORT}/", wait_until="networkidle", timeout=60000)
            views = page.eval_on_selector_all("nav button[data-view]", "els => els.map(e => e.dataset.view)")
            print("views:", views)
            report = {}
            for v in views:
                before = len(events)
                t0 = time.monotonic()
                page.click(f"nav button[data-view='{v}']")
                try:
                    page.wait_for_load_state("networkidle", timeout=30000)
                except Exception as e:
                    events.append(("timeout", v, str(e)[:80]))
                time.sleep(1.0)
                report[v] = {"ms": round((time.monotonic() - t0) * 1000), "events": events[before:]}
            page.screenshot(path=str(A / "ui_files.png"), full_page=False)
            b.close()
        for v, r in report.items():
            print(f"{v:12s} {r['ms']:6d} ms  {'OK' if not r['events'] else r['events']}")
    finally:
        p.terminate()
        try:
            p.wait(30)
        except subprocess.TimeoutExpired:
            p.kill()
        log.close()
    txt = (A / "ui_walk_serve.log").read_text(errors="replace")
    bad = [l[:200] for l in txt.splitlines() if "Traceback" in l or "ERROR" in l or "WARN" in l]
    print("server-side error lines:", bad[:10])


if __name__ == "__main__":
    main()
