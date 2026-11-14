"""Drive the real dashboard in Chromium: every tab, the main buttons, looking for JS errors,
failed requests, 'undefined'/'NaN'/'null'/raw JSON shown to the user.
usage: ui_drive.py <base_url> <label> <subject> <state> <payer> <code> [slow]"""
import json
import re
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

OUT = Path("/tmp/mrfx_review10/consulting/ui")
OUT.mkdir(parents=True, exist_ok=True)
BAD = re.compile(r"\bundefined\b|\bNaN\b|\bnull\b|\[object Object\]|\{\"[a-z_]+\":|Traceback|HTTP 5\d\d|\$-|\bInfinity\b")


def main():
    base, label, subject, st, payer, code = sys.argv[1:7]
    slow = len(sys.argv) > 7 and sys.argv[7] == "slow"
    wait_ms = 90000 if slow else 20000
    shots = OUT / f"shots_{label}"
    shots.mkdir(exist_ok=True)
    ZIP = sys.argv[8] if len(sys.argv) > 8 else "64105"
    log = {"pageerrors": [], "console": [], "failed": [], "bad_text": [], "steps": [], "popups": [], "downloads": []}
    with sync_playwright() as p:
        br = p.chromium.launch(executable_path="/opt/pw-browsers/chromium", args=["--headless=new", "--no-sandbox"])
        ctx = br.new_context(viewport={"width": 1500, "height": 950}, accept_downloads=True)
        page = ctx.new_page()
        page.set_default_timeout(6000)
        cur = {"step": "load"}
        inflight = set()

        def wire(pg, name):
            pg.on("pageerror", lambda e: log["pageerrors"].append({"step": cur["step"], "page": name, "err": str(e)[:400]}))
            pg.on("console", lambda m: m.type == "error" and log["console"].append({"step": cur["step"], "page": name, "text": m.text[:300]}))

            def on_resp(r):
                if "/api/" in r.url and r.status >= 400:
                    try:
                        body = r.text()[:200]
                    except Exception:
                        body = ""
                    log["failed"].append({"step": cur["step"], "status": r.status, "url": r.url.replace(base, "")[:160], "body": body})
            pg.on("response", on_resp)
            pg.on("request", lambda r: "/api/" in r.url and inflight.add(r))
            pg.on("requestfinished", lambda r: inflight.discard(r))
            pg.on("requestfailed", lambda r: (inflight.discard(r), "/api/" in r.url and log["failed"].append(
                {"step": cur["step"], "status": "FAILED", "url": r.url.replace(base, "")[:160], "body": str(r.failure)[:100]})))
        wire(page, "main")

        pending = []

        def on_page(pg):
            wire(pg, "popup")
            pending.append(pg)

        def drain_popups():
            while pending:
                pg = pending.pop(0)
                t0 = time.time()
                txt = ""
                try:
                    while time.time() - t0 < wait_ms / 1000 and not pg.is_closed() and not pg.url.startswith("blob:"):
                        page.wait_for_timeout(200)
                    if pg.is_closed():
                        log["popups"].append({"step": cur["step"], "closed_by_app": True})
                        continue
                    pg.wait_for_load_state("load", timeout=wait_ms)
                    page.wait_for_timeout(400)
                    txt = pg.inner_text("body")[:300000]
                except Exception as e:
                    txt = f"(popup failed: {e})"
                hits = []
                for m in BAD.finditer(txt):
                    s0, e0 = max(0, m.start() - 60), min(len(txt), m.end() + 60)
                    hits.append(re.sub(r"\s+", " ", txt[s0:e0]))
                k = len(log["popups"]) + 1
                log["popups"].append({"step": cur["step"], "url": pg.url[:40], "chars": len(txt), "bad": hits[:5],
                                      "secs": round(time.time() - t0, 1), "head": re.sub(r"\s+", " ", txt[:200])})
                try:
                    pg.screenshot(path=str(shots / f"popup_{k}_{re.sub(chr(92) + 'W+', '_', cur['step'])}.png"), full_page=False)
                    (shots / f"popup_{k}.txt").write_text(txt)
                    pg.close()
                except Exception:
                    pass
        ctx.on("page", on_page)
        page.on("dialog", lambda d: (log.setdefault("dialogs", []).append({"step": cur["step"], "type": d.type, "msg": d.message[:300]}), d.accept()))
        page.on("download", lambda d: log["downloads"].append({"step": cur["step"], "name": d.suggested_filename}))

        def settle(extra=0.4):
            t0 = time.time()
            page.wait_for_timeout(250)
            while time.time() - t0 < wait_ms / 1000:
                # the Files tab polls; ignore polling endpoints
                busy = [r for r in inflight if not re.search(r"/api/(files|urls|stats)(\?|$)", r.url)]
                if not busy and not page.locator(".loading:visible").count():
                    break
                page.wait_for_timeout(150)
            else:
                log["steps"].append({"step": cur["step"], "TIMEOUT": [r.url.replace(base, "")[:100] for r in inflight][:5]})
            page.wait_for_timeout(int(extra * 1000))
            return round(time.time() - t0, 1)

        def check(view=None):
            sel = f"#view-{view}" if view else "body"
            try:
                txt = page.inner_text(sel)
            except Exception as e:
                txt = f"(inner_text failed {e})"
            hits = []
            for m in BAD.finditer(txt):
                s, e = max(0, m.start() - 60), min(len(txt), m.end() + 60)
                hits.append(re.sub(r"\s+", " ", txt[s:e]))
            if hits:
                log["bad_text"].append({"step": cur["step"], "hits": hits[:6]})
            return txt

        def step(name, fn, view=None, shot=True):
            cur["step"] = name
            t0 = time.time()
            err = None
            try:
                fn()
            except Exception as e:
                err = str(e)[:200]
            took = settle()
            drain_popups()
            txt = check(view)
            rec = {"step": name, "secs": round(time.time() - t0, 1), "chars": len(txt)}
            if err:
                rec["driver_error"] = err
            # the visible outcome, first 160 chars of the view's main output area
            log["steps"].append(rec)
            (shots / (re.sub(r"\W+", "_", name) + ".txt")).write_text(txt)
            (OUT / f"ui_{label}.json").write_text(json.dumps(log, indent=1))
            if shot:
                try:
                    page.screenshot(path=str(shots / (re.sub(r"\W+", "_", name) + ".png")))
                except Exception:
                    pass
            return txt

        def nav(view):
            page.click(f'nav button[data-view="{view}"]')

        def fill(sel, val):
            page.fill(sel, val)
            page.dispatch_event(sel, "change")

        page.goto(base, wait_until="load", timeout=wait_ms)
        step("load overview", lambda: None, "overview")
        step("overview state", lambda: (fill("#ov-state", st), page.click("#ov-refresh")), "overview")
        # explorer
        step("explorer", lambda: nav("explorer"), "explorer")
        step("explorer state filter", lambda: (fill("#f-state", st), page.keyboard.press("Enter")), "explorer")
        step("explorer q", lambda: (fill("#f-q", subject[:6]), page.keyboard.press("Enter")), "explorer")
        step("explorer clear", lambda: page.click("#f-clear"), "explorer")
        step("explorer row click", lambda: page.locator("#rates-body tr").first.click(), None)
        step("explorer close panel", lambda: page.keyboard.press("Escape"), "explorer")
        step("explorer export csv", lambda: page.click("#btn-export"), "explorer")
        step("explorer export zip", lambda: page.click("#btn-export-zip"), "explorer")
        step("explorer outreach zip", lambda: (fill("#f-state", st), page.keyboard.press("Enter"), page.wait_for_timeout(1000), page.click("#btn-outreach")), "explorer")
        # code page
        step("cpt", lambda: nav("cpt"), "cpt")
        step("cpt pick", lambda: page.locator("#view-cpt .chip, #view-cpt [data-code]").first.click(), "cpt")
        # markets
        step("markets", lambda: (nav("markets"), fill("#mk-code", code), fill("#mk-state", st), page.click("#mk-run")), "markets")
        # changes + trajectory
        step("changes", lambda: (nav("changes"), page.wait_for_timeout(1000), page.click("#ch-run")), "changes")
        step("trajectory", lambda: page.click("#ch-traj-run"), "changes")
        # benchmark
        step("benchmark", lambda: (nav("benchmark"), page.wait_for_timeout(800), fill("#b-subject", subject), fill("#b-state", st), page.click("#b-run")), "benchmark")
        step("benchmark pitch report", lambda: page.click("#b-report"), "benchmark")
        step("benchmark negotiation report", lambda: page.click("#b-negotiation"), "benchmark")
        step("benchmark gaps", lambda: page.click("#b-gaps-run"), "benchmark")
        # negotiate
        def neg():
            nav("negotiate"); page.wait_for_timeout(800)
            fill("#ng-subject", subject); fill("#ng-state", st)
            page.wait_for_timeout(1000)
            try:
                page.select_option("#ng-payer", label=payer)
            except Exception:
                page.select_option("#ng-payer", index=1)
            page.click("#ng-run")
        step("negotiate compare", neg, "negotiate")
        step("negotiate report", lambda: page.click("#ng-report"), "negotiate")
        step("negotiate proposal", lambda: page.click("#ng-proposal-run"), "negotiate")
        step("negotiate proposal report", lambda: page.click("#ng-proposal-report"), "negotiate")
        step("negotiate leverage", lambda: page.click("#lv-run"), "negotiate")
        step("negotiate economics", lambda: page.click("#ec-run"), "negotiate")
        # rate card
        step("ratecard", lambda: (nav("ratecard"), page.wait_for_timeout(800), fill("#rc-subject", subject), fill("#rc-state", st), page.click("#rc-run")), "ratecard")
        step("ratecard report", lambda: page.click("#rc-report"), "ratecard")
        step("ratecard csv", lambda: page.click("#rc-csv"), "ratecard")
        step("ratecard visit", lambda: page.click("#sv-run"), "ratecard")
        # leads
        step("leads", lambda: (nav("leads"), page.wait_for_timeout(800), fill("#ld-state", st), fill("#ld-mincodes", "1"), page.click("#ld-run")), "leads")
        step("leads csv", lambda: page.click("#ld-csv"), "leads")
        step("leaderboard", lambda: page.click("#ld-board-run"), "leads")
        step("leads size", lambda: (fill("#ss-subject", subject), page.click("#ss-run")), "leads")
        step("leads dossier", lambda: (fill("#ds-subject", subject), page.click("#ds-run")), "leads")
        step("leads market report", lambda: (fill("#mr-state", st), fill("#mr-code", code), page.click("#mr-run")), "leads")
        # roster
        step("roster", lambda: (nav("roster"), page.wait_for_timeout(500), fill("#ro-payer", payer), fill("#ro-state", st), fill("#ro-mincodes", "1"), page.click("#ro-run")), "roster")
        step("roster csv", lambda: page.click("#ro-csv"), "roster")
        # clients
        step("clients", lambda: (nav("clients"), page.wait_for_timeout(500), fill("#cl-subject", subject), page.click("#cl-add")), "clients")
        step("clients refresh", lambda: page.click("#cl-refresh"), "clients")
        step("clients renewals", lambda: page.click("#rn-run"), "clients")
        # medicare
        step("medicare", lambda: (nav("medicare"), page.wait_for_timeout(500), fill("#md-subject", subject), page.click("#md-run")), "medicare")

        # ---- review 10 extras
        step("benchmark fill from medicare", lambda: (nav("benchmark"), page.click("#b-vol-medicare")), "benchmark")
        step("benchmark rerun with volumes", lambda: page.click("#b-run"), "benchmark")
        step("negotiate hospital parity", lambda: (nav("negotiate"), page.click("#hp-run")), "negotiate")
        step("negotiate floors", lambda: page.click("#fl-run"), "negotiate")
        step("negotiate leverage all", lambda: page.click("#lv-all"), "negotiate")
        step("negotiate economics upv", lambda: (fill("#ec-upv", "4"), page.click("#ec-run")), "negotiate")
        step("ratecard bundle", lambda: (nav("ratecard"), page.click("#rc-bundle")), "ratecard")
        step("leads new to network", lambda: (nav("leads"), fill("#nn-zip", ZIP), page.click("#nn-run")), "leads")
        step("leads new clinics", lambda: (fill("#nc-zip", ZIP), page.click("#nc-run")), "leads")
        step("leads closures", lambda: (fill("#cz-zip", ZIP), page.click("#cz-run")), "leads")
        step("leads growth", lambda: (fill("#gr-subject", subject), page.click("#gr-run")), "leads")
        step("leads service lines", lambda: page.click("#gr-lines"), "leads")
        step("leads market sizing", lambda: (fill("#ms-zip", ZIP), page.click("#ms-run")), "leads")
        step("clients packets", lambda: (nav("clients"), page.click("#pk-build")), "clients")
        step("clients baseline", lambda: (fill("#en-subject", subject), page.click("#en-save")), "clients")
        step("clients engagement compare", lambda: page.click("#en-compare"), "clients")
        step("medicare leaders", lambda: (nav("medicare"), fill("#rl-zip", ZIP), page.click("#rl-run")), "medicare")
        step("medicare eligibility", lambda: (fill("#md-npis", "1901234561 1610000001 1999999999"), page.click("#md-check")), "medicare")
        step("medicare csv", lambda: page.click("#md-csv"), "medicare")
        # sources, files
        step("sources", lambda: nav("sources"), "sources")
        step("files", lambda: nav("files"), "files")
        (OUT / f"ui_{label}.json").write_text(json.dumps(log, indent=1))
        br.close()
    print(f"== {label}: pageerrors {len(log['pageerrors'])} console {len(log['console'])} failed {len(log['failed'])} bad_text {len(log['bad_text'])} popups {len(log['popups'])} downloads {len(log['downloads'])}")
    for k in ("pageerrors", "console", "failed", "bad_text", "popups", "downloads", "dialogs"):
        for x in log.get(k, []):
            print(f"  [{k}]", json.dumps(x)[:420])
    for s in log["steps"]:
        print("  step", json.dumps(s)[:200])


if __name__ == "__main__":
    main()
