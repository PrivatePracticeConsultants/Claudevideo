"""A client saved by an SSN-pattern TIN (the Clients box accepts any tax ID):
the digest shows the raw nine digits (pre-existing), and the round-13
jump-through now ALSO writes them into the address bar — the bookmarkable,
shareable URL — via hashFromInputs (app.js:2949)."""
from ui_common import *
BASE = "http://localhost:8791"

def main():
    with sync_playwright() as pw:
        b, ctx = launch(pw)
        page = ctx.new_page(); rec = Rec(page)
        page.goto(BASE + "/"); page.wait_for_timeout(2500)
        page.click('nav button[data-view="clients"]'); page.wait_for_timeout(5000)
        row = page.evaluate("""[...document.querySelectorAll('#cl-out tbody tr')].map(t => t.innerText.replace(/\\s+/g,' ').slice(0,80))""")
        print("digest rows:", row)
        page.screenshot(path=str(SHOTS / "ui04_clients_digest_ssn.png"))
        page.click('[data-cl-rc="070000001"]'); page.wait_for_timeout(4000)
        print("after the client's 'Rates' button -> address bar:", page.url)
        page.screenshot(path=str(SHOTS / "ui04_ssn_in_address_bar.png"))
        print("rate card heading:", page.evaluate("document.querySelector('#rc-out h3, #rc-out h2')?.innerText"))
        b.close()

if __name__ == "__main__":
    main()
