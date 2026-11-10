"""Overview: type a state, click Refresh -> 'change' + 'click' both load; the second request meets the
single-flight lock (503) and, being the newest ticket, paints its error over the real answer."""
import sys
from playwright.sync_api import sync_playwright

def main():
    base = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8710"
    states = sys.argv[2:] or ["WA", "ND", "OR", "TX"]
    with sync_playwright() as p:
        br = p.chromium.launch(executable_path="/opt/pw-browsers/chromium", args=["--headless=new", "--no-sandbox"])
        page = br.new_page(viewport={"width": 1500, "height": 900}); page.set_default_timeout(20000)
        seen = []
        page.on("response", lambda r: "/api/overview" in r.url and seen.append((r.url.split("/api/")[1], r.status)))
        page.goto(base, wait_until="load"); page.click('button[data-view="overview"]'); page.wait_for_timeout(2500)
        for st in states:           # each state is cold for this server process
            seen.clear()
            page.click("#ov-state"); page.fill("#ov-state", ""); page.keyboard.type(st)
            page.click("#ov-refresh"); page.wait_for_timeout(3500)
            txt = page.inner_text("#ov-out")[:160].replace("\n", " ")
            print(st, seen, "| screen:", txt)
        br.close()

if __name__ == "__main__":
    main()
