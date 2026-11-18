"""Walk the 14 dashboard views in Chromium against a running server.
usage: ui_walk.py <base_url> <out_dir> <subject_tin> <state>
Records per view: time to network idle, console errors, failed requests,
responses >= 400; screenshots Benchmark and Rate card AFTER running them."""
import json
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

VIEWS = ["overview", "clients", "explorer", "cpt", "markets", "changes", "benchmark",
         "negotiate", "ratecard", "leads", "medicare", "roster", "files", "sources"]


def main():
    base, out, tin, st = sys.argv[1:5]
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    log = {"views": [], "pageerrors": [], "console": [], "failed": [], "popups": []}
    cur = {"view": "load"}
    with sync_playwright() as p:
        br = p.chromium.launch(executable_path="/opt/pw-browsers/chromium", args=["--headless=new", "--no-sandbox"])
        ctx = br.new_context(viewport={"width": 1500, "height": 1000}, accept_downloads=True)
        page = ctx.new_page()
        page.set_default_timeout(15000)

        def wire(pg, name):
            pg.on("pageerror", lambda e: log["pageerrors"].append({"view": cur["view"], "page": name, "err": str(e)[:400]}))
            pg.on("console", lambda m: m.type == "error" and log["console"].append({"view": cur["view"], "page": name, "text": m.text[:300]}))

            def on_resp(r):
                if r.status >= 400:
                    try:
                        body = r.text()[:200]
                    except Exception:
                        body = ""
                    log["failed"].append({"view": cur["view"], "status": r.status, "url": r.url.replace(base, "")[:160], "body": body})
            pg.on("response", on_resp)
            pg.on("requestfailed", lambda r: log["failed"].append(
                {"view": cur["view"], "status": "FAILED", "url": r.url.replace(base, "")[:160], "body": str(r.failure)[:100]}))
        wire(page, "main")
        popups = []
        ctx.on("page", lambda pg: (wire(pg, "popup"), popups.append(pg)))

        t0 = time.perf_counter()
        page.goto(base + "/", wait_until="networkidle", timeout=180000)
        log["views"].append({"view": "initial load", "idle_s": round(time.perf_counter() - t0, 2)})
        for v in VIEWS:
            cur["view"] = v
            t0 = time.perf_counter()
            page.click(f'nav button[data-view="{v}"]')
            try:
                page.wait_for_load_state("networkidle", timeout=180000)
            except Exception as e:
                log["views"].append({"view": v, "idle_s": None, "err": str(e)[:200]})
                continue
            el = round(time.perf_counter() - t0, 2)
            text = page.inner_text(f"#view-{v}")[:400].replace("\n", " ") if page.query_selector(f"#view-{v}") else ""
            log["views"].append({"view": v, "idle_s": el, "text_head": text[:200]})
            print(f"{v:12s} {el:6.2f}s", flush=True)
        # Benchmark: run it for the subject, screenshot
        cur["view"] = "benchmark-run"
        page.click('nav button[data-view="benchmark"]')
        page.fill("#b-subject", tin)
        page.fill("#b-state", st)
        t0 = time.perf_counter()
        page.click("#b-run")
        try:
            page.wait_for_function("() => !document.querySelector('#b-out .loading')", timeout=180000)
            page.wait_for_load_state("networkidle", timeout=180000)
        except Exception as e:
            log["views"].append({"view": "benchmark run", "idle_s": None, "err": str(e)[:200]})
        log["views"].append({"view": "benchmark run (subject + state)", "idle_s": round(time.perf_counter() - t0, 2),
                             "text_head": page.inner_text("#b-out")[:300].replace("\n", " ")})
        page.screenshot(path=str(out / "benchmark.png"), full_page=True)
        # Rate card: run it with state + discipline (so the visit-value block shows)
        cur["view"] = "ratecard-run"
        page.click('nav button[data-view="ratecard"]')
        page.fill("#rc-subject", tin)
        page.fill("#rc-state", st)
        page.select_option("#rc-disc", "PT")
        t0 = time.perf_counter()
        page.click("#rc-run")
        try:
            page.wait_for_function("() => !document.querySelector('#rc-out .loading')", timeout=180000)
            page.wait_for_load_state("networkidle", timeout=180000)
        except Exception as e:
            log["views"].append({"view": "ratecard run", "idle_s": None, "err": str(e)[:200]})
        log["views"].append({"view": "rate card run (subject + state + PT)", "idle_s": round(time.perf_counter() - t0, 2),
                             "text_head": page.inner_text("#rc-out")[:300].replace("\n", " ")})
        # the visit-value block
        if page.query_selector("#vv-run"):
            t0 = time.perf_counter()
            page.click("#vv-run")
            try:
                page.wait_for_load_state("networkidle", timeout=180000)
                page.wait_for_timeout(500)
            except Exception as e:
                log["views"].append({"view": "visit value run", "idle_s": None, "err": str(e)[:200]})
            log["views"].append({"view": "visit value in this market (button)", "idle_s": round(time.perf_counter() - t0, 2)})
        page.screenshot(path=str(out / "ratecard.png"), full_page=True)
        for i, pg in enumerate(popups):
            try:
                log["popups"].append({"i": i, "url": pg.url[:120], "title": pg.title()[:80]})
            except Exception:
                pass
        br.close()
    (out / "ui_walk.json").write_text(json.dumps(log, indent=1))
    print(json.dumps({k: len(v) for k, v in log.items()}))


if __name__ == "__main__":
    main()
