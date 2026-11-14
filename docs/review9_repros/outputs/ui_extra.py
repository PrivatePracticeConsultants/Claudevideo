"""Extra UI steps: Overview tab, Code page, Negotiate proposal, entity-grain export refusal (big store)."""
import json, re, sys
from pathlib import Path
from playwright.sync_api import sync_playwright
OUT = Path("/tmp/mrfx_review9/outputs")
if __name__ == "__main__":
    base, label, subject, st, payer = sys.argv[1:6]
    log = {"errors": [], "dialogs": [], "failed": []}
    with sync_playwright() as p:
        br = p.chromium.launch(executable_path="/opt/pw-browsers/chromium", args=["--headless=new", "--no-sandbox"])
        ctx = br.new_context(viewport={"width": 1500, "height": 1000})
        page = ctx.new_page(); page.set_default_timeout(8000)
        page.on("pageerror", lambda e: log["errors"].append(str(e)[:300]))
        page.on("dialog", lambda d: (log["dialogs"].append(d.message[:400]), d.accept()))
        page.on("response", lambda r: "/api/" in r.url and r.status >= 400 and log["failed"].append((r.status, r.url.replace(base, "")[:120])))
        popups = []
        ctx.on("page", lambda pg: popups.append(pg))
        page.goto(base, wait_until="load"); page.wait_for_timeout(1500)
        def txt(sel, n=700):
            return re.sub(r"\s+", " ", page.inner_text(sel))[:n]
        def bad(sel):
            return sorted(set(re.findall(r"\bundefined\b|\bNaN\b|\bnull\b|\[object Object\]|\$-", page.inner_text(sel))))
        page.click('nav button[data-view="overview"]'); page.wait_for_timeout(25000 if label == "big" else 2500)
        print("OVERVIEW visible:", page.is_visible("#ov-state"), "| bad:", bad("#view-overview"), "|", txt("#view-overview", 900))
        page.screenshot(path=str(OUT / f"shots_{label}" / "overview.png"))
        try:
            page.fill("#ov-state", st); page.click("#ov-refresh"); page.wait_for_timeout(6000 if label == "big" else 1500)
            print("OVERVIEW", st, "| bad:", bad("#view-overview"), "|", txt("#view-overview", 600))
        except Exception as e:
            print("overview state failed:", str(e)[:200])
        page.click('nav button[data-view="cpt"]'); page.wait_for_timeout(1500)
        chips = page.locator("#cpt-chips button, #cpt-chips .chip")
        print("CPT chips:", chips.count())
        if chips.count():
            chips.first.click(); page.wait_for_timeout(6000 if label == "big" else 1500)
        print("CPT | bad:", bad("#view-cpt"), "|", txt("#view-cpt", 700))
        page.screenshot(path=str(OUT / f"shots_{label}" / "cpt.png"))
        page.click('nav button[data-view="negotiate"]'); page.wait_for_timeout(1000)
        page.fill("#ng-subject", subject); page.dispatch_event("#ng-subject", "change"); page.fill("#ng-state", st); page.wait_for_timeout(1500)
        try:
            page.select_option("#ng-payer", label=payer)
        except Exception as e:
            print("payer select:", str(e)[:120])
        vis = page.is_visible("#ng-proposal-run")
        print("proposal button visible:", vis)
        if not vis:
            page.evaluate("() => { let e = document.querySelector('#ng-proposal-run'); while (e) { if (e.tagName === 'DETAILS') e.open = true; e = e.parentElement; } }")
            page.wait_for_timeout(300)
        try:
            page.click("#ng-proposal-run"); page.wait_for_timeout(6000 if label == "big" else 1500)
            print("PROPOSAL | bad:", bad("#ng-out"), "|", txt("#ng-out", 800))
            page.click("#ng-proposal-report"); page.wait_for_timeout(8000 if label == "big" else 2500)
            for pg in popups:
                if not pg.is_closed():
                    t = pg.inner_text("body")
                    print("PROPOSAL REPORT popup chars", len(t), "bad", sorted(set(re.findall(r"\bundefined\b|\bNaN\b|\bnull\b|\$-", t))), "|", re.sub(r"\s+", " ", t[:500]))
        except Exception as e:
            print("proposal failed:", str(e)[:300])
        # entity-grain export refusal
        page.click('nav button[data-view="explorer"]'); page.wait_for_timeout(1500)
        g = page.locator('[data-grain="entity"]')
        print("entity grain buttons:", g.count())
        if g.count():
            g.first.click(); page.wait_for_timeout(4000)
            print("EXPLORER entity | ", txt("#rates-state", 400) or txt("#view-explorer", 300))
            page.click("#btn-export"); page.wait_for_timeout(8000)
        br.close()
    print("log", json.dumps(log)[:1500])
