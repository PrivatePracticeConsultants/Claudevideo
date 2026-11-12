"""Every output on every view, against the UI harness (:8792, ~0.4 s injected
per connection of a run with a token): (A) click and press Stop as soon as the
bar shows; (B) click and let it finish. Recorded per action: bars left on the
page, the anchor's disabled state afterwards, dialogs, popups, console errors,
failed requests."""
import json, time, sys
from ui_common import *
BASE = "http://localhost:8792"

ACTIONS = [
    # view, setup [(selector, value)], button
    ("explorer", [], "#btn-export"),
    ("explorer", [], "#btn-export-zip"),
    ("explorer", [], "#btn-outreach"),
    ("overview", [], "#ov-refresh"),
    ("benchmark", [("#b-subject", "430000010"), ("#b-state", "MO")], "#b-run"),
    ("benchmark", [("#b-subject", "430000010"), ("#b-state", "MO")], "#b-gaps-run"),
    ("benchmark", [("#b-subject", "430000010"), ("#b-state", "MO")], "#b-report"),
    ("benchmark", [("#b-subject", "430000010"), ("#b-state", "MO")], "#b-report-pdf"),
    ("ratecard", [("#rc-subject", "430000010"), ("#rc-state", "MO")], "#rc-run"),
    ("ratecard", [("#rc-subject", "430000010"), ("#rc-state", "MO")], "#rc-report"),
    ("ratecard", [("#rc-subject", "430000010"), ("#rc-state", "MO")], "#rc-csv"),
    ("ratecard", [("#rc-subject", "430000010"), ("#rc-state", "MO")], "#rc-bundle"),
    ("ratecard", [("#rc-subject", "430000010"), ("#rc-state", "MO")], "#vv-run"),
    ("negotiate", [("#ng-subject", "430000010"), ("#ng-state", "MO")], "#ng-run"),
    ("clients", [], "#cl-refresh"),
    ("leads", [("#ld-state", "MO")], "#ld-run"),
    ("leads", [("#ld-state", "MO")], "#ld-board-run"),
    ("leads", [("#ds-subject", "430000010")], "#ds-run"),
    ("leads", [("#mr-state", "MO")], "#mr-run"),
    ("changes", [("#ch-subject", "430000010")], "#ch-run"),
    ("changes", [], "#ch-traj-run"),
    ("roster", [("#ro-payer", "Alpha Health")], "#ro-run"),
    ("roster", [("#ro-payer", "Alpha Health")], "#ro-csv"),
    ("medicare", [("#md-subject", "430000010")], "#md-run"),
]

def act(page, rec, view, setup, btn, stop):
    page.click(f'nav button[data-view="{view}"]'); page.wait_for_timeout(1200)
    for sel, val in setup:
        if page.query_selector(sel):
            page.fill(sel, val)
    b = page.query_selector(btn)
    if not b:
        return {"missing": True}
    n_dialogs = len(rec.dialogs); n_pages = len(page.context.pages)
    if b.is_disabled():
        return {"disabled_before": True}
    b.click()
    out = {}
    if stop:
        try:
            page.wait_for_selector(f"#jobbar-{view} .jobbar.on .jobbar-stop", timeout=6000)
            page.wait_for_timeout(300)
            page.click(f"#jobbar-{view} .jobbar.on .jobbar-stop")
            out["stop_clicked"] = True
        except Exception:
            out["stop_clicked"] = False
    for _ in range(90):
        page.wait_for_timeout(500)
        if not page.query_selector(f"#jobbar-{view} .jobbar"):
            break
    out["bars_left"] = [e.inner_text()[:90] for e in page.query_selector_all(".jobbar")]
    out["anchor_disabled_after"] = page.evaluate(f"document.querySelector('{btn}')?.disabled")
    out["dialogs"] = rec.dialogs[n_dialogs:]
    out["new_tabs"] = len(page.context.pages) - n_pages
    for p in page.context.pages[1:]:
        try:
            out.setdefault("tab_text", []).append(p.evaluate("document.body ? document.body.innerText.slice(0,120) : ''"))
        except Exception as e:
            out.setdefault("tab_text", []).append(f"<{e}>")
        p.close()
    return out

def main():
    with sync_playwright() as pw:
        b, ctx = launch(pw)
        page = ctx.new_page(); rec = Rec(page); rec.dialogs = []
        def on_dialog(d):
            rec.dialogs.append(d.message[:140]); d.dismiss()
        ctx.on("page", lambda p: p.on("dialog", on_dialog))
        page.on("dialog", on_dialog)
        page.goto(BASE + "/"); page.wait_for_timeout(3000)
        for mode in ("STOP", "FINISH"):
            for view, setup, btn in ACTIONS:
                t = time.time()
                try:
                    r = act(page, rec, view, setup, btn, mode == "STOP")
                except Exception as e:
                    r = {"exception": str(e)[:200]}
                print(f"[{mode}] {view:9s} {btn:16s} {time.time()-t:5.1f}s {json.dumps(r)}", flush=True)
                if r.get("bars_left") or r.get("anchor_disabled_after"):
                    page.screenshot(path=str(SHOTS / f"ui03_{mode}_{btn.strip('#')}.png"))
        rec.dump("ui03")
        b.close()

if __name__ == "__main__":
    main()
