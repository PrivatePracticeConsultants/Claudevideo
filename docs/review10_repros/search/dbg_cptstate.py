"""Does typing a state on the Code comparison page fire a request? usage: dbg_cptstate.py <base>"""
import sys
from playwright.sync_api import sync_playwright

if __name__ == "__main__":
    base = sys.argv[1]
    with sync_playwright() as p:
        br = p.chromium.launch(executable_path="/opt/pw-browsers/chromium", args=["--headless=new", "--no-sandbox"])
        page = br.new_page(viewport={"width": 1500, "height": 950})
        reqs = []
        page.on("request", lambda r: "/api/" in r.url and reqs.append(r.url.replace(base, "")))
        page.goto(base); page.wait_for_timeout(2500)
        page.click('nav button[data-view="cpt"]'); page.wait_for_timeout(1500)
        page.click('#cpt-chips .chip[data-cpt="97110"]'); page.wait_for_timeout(1500)
        reqs.clear()
        page.focus("#cpt-state-filter"); page.keyboard.type("ND", delay=120); page.wait_for_timeout(2500)
        print("after typing ND:", reqs, "| box value:", page.input_value("#cpt-state-filter"),
              "| heading:", page.inner_text("#view-cpt h2")[:90])
        print("cptSelected:", page.evaluate("() => state.cptSelected"))
        br.close()
