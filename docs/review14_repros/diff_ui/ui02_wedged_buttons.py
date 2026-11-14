"""A bar restores its anchor button to the disabled state it captured at mount
(app.js mountJobbar: wasDisabled). Two overlapping runs on the same anchor —
Enter pressed twice, or a tab re-entered while its run is in flight — capture
'disabled' for the second run; when the second ends last the button stays
disabled for good."""
from ui_common import *

def state(page, sel):
    return page.evaluate(f"(() => {{ const b = document.querySelector('{sel}'); return b ? b.disabled : null; }})()")

def main():
    with sync_playwright() as pw:
        b, ctx = launch(pw)
        page = ctx.new_page(); rec = Rec(page)
        page.goto(BASE + "/"); page.wait_for_timeout(2500)
        # 1) Roster: Enter twice in the payer box
        page.click('nav button[data-view="roster"]'); page.wait_for_timeout(1500)
        page.fill("#ro-payer", "Alpha Health")
        page.evaluate("""() => { const i = document.querySelector('#ro-payer');
            for (let k = 0; k < 2; k++) i.dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter', bubbles: true})); }""")
        page.wait_for_timeout(8000)
        print("roster: #ro-run disabled after both runs ended =", state(page, "#ro-run"),
              "| rows shown:", page.evaluate("document.querySelectorAll('#ro-out tbody tr').length"))
        page.screenshot(path=str(SHOTS / "ui02_roster_wedged.png"))
        # 2) Medicare: Enter twice in the practice box
        page.click('nav button[data-view="medicare"]'); page.wait_for_timeout(1500)
        page.fill("#md-subject", "430000010")
        page.evaluate("""() => { const i = document.querySelector('#md-subject');
            for (let k = 0; k < 2; k++) i.dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter', bubbles: true})); }""")
        page.wait_for_timeout(8000)
        print("medicare: #md-run disabled after both runs ended =", state(page, "#md-run"))
        # 3) Clients: open the tab (digest runs), leave and come back while it runs
        page.click('nav button[data-view="clients"]'); page.wait_for_timeout(150)
        page.click('nav button[data-view="overview"]'); page.wait_for_timeout(100)
        page.click('nav button[data-view="clients"]')
        page.wait_for_timeout(12000)
        print("clients: #cl-refresh disabled after both digests ended =", state(page, "#cl-refresh"))
        page.screenshot(path=str(SHOTS / "ui02_clients_refresh.png"))
        # reload to show it is the page state, not the server
        page.reload(); page.wait_for_timeout(2500)
        page.click('nav button[data-view="roster"]'); page.wait_for_timeout(1000)
        print("after reload: #ro-run disabled =", state(page, "#ro-run"))
        rec.dump("ui02")
        b.close()

if __name__ == "__main__":
    for i in range(2):
        print(f"== attempt {i+1}")
        main()
