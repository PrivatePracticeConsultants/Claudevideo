"""Chromium proof for r_js_rounding.py: Explorer 'By code' (TX) vs Markets (TX) for 92526."""
import re
from playwright.sync_api import sync_playwright

def main():
    with sync_playwright() as p:
        br = p.chromium.launch(executable_path="/opt/pw-browsers/chromium", args=["--headless=new", "--no-sandbox"])
        page = br.new_page(viewport={"width": 1600, "height": 1000}); page.set_default_timeout(15000)
        page.goto("http://localhost:8710", wait_until="load")
        print("JS: (70.695).toLocaleString ->", page.evaluate("Number(70.695).toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2})"),
              "| (20.025) ->", page.evaluate("Number(20.025).toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2})"))
        page.click('button[data-view="explorer"]'); page.fill("#f-state", "TX"); page.dispatch_event("#f-state", "change")
        page.wait_for_function("/1,055/.test(document.querySelector('#summary-bycode').innerText)")
        row = [l for l in page.inner_text("#summary-bycode").splitlines() if l.startswith("92526")]
        print("Explorer (state TX) By code:", row)
        page.click('button[data-view="markets"]'); page.fill("#mk-code", "92526"); page.fill("#mk-state", "TX"); page.click("#mk-run")
        page.wait_for_function("/Market median/.test(document.querySelector('#mk-out').innerText)")
        print("Markets (TX):", re.findall(r"Market median[^.]*\.\d\d[^.)]*", page.inner_text("#mk-out"))[:2])
        br.close()

if __name__ == "__main__":
    main()
