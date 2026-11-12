"""XSS through payer-file names, the header search box and the hash route;
SSN-pattern TINs in the address bar."""
import time, urllib.parse
from ui_common import *

def main():
    with sync_playwright() as pw:
        b, ctx = launch(pw)
        page = ctx.new_page(); rec = Rec(page)
        page.goto(BASE + "/"); page.wait_for_timeout(2500)
        # explorer table shows names
        page.click('nav button[data-view="explorer"]'); page.wait_for_timeout(2500)
        print("xss after explorer:", page.evaluate("window.__xss || 0"))
        # header search
        page.fill("#find-practice", "ACME"); page.wait_for_timeout(800)
        page.press("#find-practice", "Enter"); page.wait_for_timeout(2500)
        print("find msg:", page.evaluate("document.querySelector('#find-msg')?.textContent"))
        print("xss after search+drawer:", page.evaluate("window.__xss || 0"))
        page.screenshot(path=str(SHOTS / "ui01_search_acme.png"))
        page.keyboard.press("Escape")
        for q in ['<svg', 'O\'BRIEN', 'ÉQUIPE', '"><', '%', '_', '\\']:
            page.fill("#find-practice", q); page.wait_for_timeout(500)
            page.press("#find-practice", "Enter"); page.wait_for_timeout(1200)
            print(f"find {q!r}:", page.evaluate("document.querySelector('#find-msg')?.textContent"),
                  "| xss:", page.evaluate("window.__xss || 0"))
            page.keyboard.press("Escape")
        # hash routes with odd input
        hashes = [
            "#benchmark?subject=" + urllib.parse.quote('<img src=x onerror="window.__xss=(window.__xss||0)+1">ACME REHAB') + "&state=MO",
            "#benchmark?subject=%22%3E%3Cimg%20src%3Dx%20onerror%3Dalert(1)%3E&state=%22%3E%3Cb%3E",
            '#"><img src=x onerror="window.__xss=99">',
            "#" + urllib.parse.quote("<img src=x onerror=window.__xss=98>"),
            "#negotiate?subject=%E6%9D%B1%E4%BA%AC&payer=%3Cb%3E&state=mo",
            "#medicare?subject=070000001",
            "#ratecard?subject=%",
            "#changes?subject=430001&state=MO&x=%E0%A4%A",
        ]
        for hsh in hashes:
            page.evaluate(f"location.hash = {hsh!r}")
            page.wait_for_timeout(2500)
            print("hash", hsh[:70], "-> view:", page.evaluate("document.querySelector('nav button.active')?.dataset.view"),
                  "| xss:", page.evaluate("window.__xss || 0"),
                  "| url:", page.url.split('#', 1)[-1][:90])
        page.screenshot(path=str(SHOTS / "ui01_after_hashes.png"))
        # SSN-pattern TIN typed into the benchmark subject box and Run: what does the URL say?
        page.evaluate("location.hash = '#benchmark'"); page.wait_for_timeout(1500)
        page.fill("#b-subject", "070000001"); page.click("#b-run"); page.wait_for_timeout(2500)
        print("benchmark Run with an SSN-pattern TIN -> url hash:", page.url.split('#', 1)[-1])
        print("benchmark out:", page.evaluate("document.querySelector('#b-out')?.innerText?.slice(0,200)"))
        # back/forward
        page.click('nav button[data-view="overview"]'); page.wait_for_timeout(800)
        page.click('nav button[data-view="leads"]'); page.wait_for_timeout(800)
        page.go_back(); page.wait_for_timeout(1500)
        print("after Back: view =", page.evaluate("document.querySelector('nav button.active')?.dataset.view"), "url:", page.url.split('#',1)[-1])
        page.go_forward(); page.wait_for_timeout(1500)
        print("after Forward: view =", page.evaluate("document.querySelector('nav button.active')?.dataset.view"), "url:", page.url.split('#',1)[-1])
        rec.dump("ui01")
        b.close()

if __name__ == "__main__":
    main()
