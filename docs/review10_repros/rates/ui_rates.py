"""Drive the Rates/Practice tabs in Chromium. usage: ui_rates.py <base> <label> <state> <city> <zip> <subject> [code]
Logs JS errors, failed requests, suspicious text, time-to-quiet per step; saves text + screenshots."""
import json, re, sys, time
from pathlib import Path
from playwright.sync_api import sync_playwright

OUT = Path("/tmp/mrfx_review10/rates/ui_out")
BAD = re.compile(r"\bundefined\b|\bNaN\b|\bnull\b|\[object Object\]|\{\"[a-z_]+\":|Traceback|HTTP 5\d\d|\$-|\bInfinity\b|-0\.0%|\$–")

def main():
    base, label, st, city, zipc, subject = sys.argv[1:7]
    code = sys.argv[7] if len(sys.argv) > 7 else "97110"
    shots = OUT / label; shots.mkdir(parents=True, exist_ok=True)
    log = {"pageerrors": [], "console": [], "failed": [], "bad": [], "steps": [], "downloads": [], "popups": []}
    with sync_playwright() as p:
        br = p.chromium.launch(executable_path="/opt/pw-browsers/chromium", args=["--headless=new", "--no-sandbox"])
        ctx = br.new_context(viewport={"width": 1600, "height": 1000}, accept_downloads=True)
        page = ctx.new_page(); page.set_default_timeout(8000)
        cur = {"step": "load"}; inflight = set(); reqlog = []
        page.on("pageerror", lambda e: log["pageerrors"].append({"step": cur["step"], "err": str(e)[:400]}))
        page.on("console", lambda m: m.type == "error" and log["console"].append({"step": cur["step"], "text": m.text[:300]}))
        def on_resp(r):
            if "/api/" in r.url and r.status >= 400:
                try: body = r.text()[:240]
                except Exception: body = ""
                log["failed"].append({"step": cur["step"], "status": r.status, "url": r.url.replace(base, "")[:200], "body": body})
        page.on("response", on_resp)
        page.on("request", lambda r: "/api/" in r.url and (inflight.add(r), reqlog.append((cur["step"], r.method, r.url.replace(base, "")[:160]))))
        page.on("requestfinished", lambda r: inflight.discard(r))
        page.on("requestfailed", lambda r: inflight.discard(r))
        popups = []
        ctx.on("page", lambda pg: popups.append(pg))
        page.on("download", lambda d: log["downloads"].append({"step": cur["step"], "name": d.suggested_filename}))

        def quiet(maxs=120):
            t0 = time.time(); page.wait_for_timeout(350)
            while time.time() - t0 < maxs:
                # the page polls /api/stats, /api/files, /api/urls: ignore those
                busy = [r for r in inflight if not re.search(r"/api/(stats|files|urls)(\?|$)", r.url)]
                if not busy:
                    page.wait_for_timeout(250)
                    if not [r for r in inflight if not re.search(r"/api/(stats|files|urls)(\?|$)", r.url)]:
                        break
                page.wait_for_timeout(100)
            return round(time.time() - t0, 2)

        def step(name, fn, sel="main"):
            cur["step"] = name; t0 = time.time(); err = None
            try: fn()
            except Exception as e: err = str(e)[:200]
            q = quiet()
            try: txt = page.inner_text(sel)
            except Exception as e: txt = f"(no text: {e})"
            hits = [re.sub(r"\s+", " ", txt[max(0, m.start() - 70):m.end() + 70]) for m in BAD.finditer(txt)]
            n = len(log["steps"]) + 1
            fn_ = re.sub(r"\W+", "_", name)[:60]
            (shots / f"{n:02d}_{fn_}.txt").write_text(txt)
            try: page.screenshot(path=str(shots / f"{n:02d}_{fn_}.png"))
            except Exception: pass
            log["steps"].append({"n": n, "step": name, "secs": round(time.time() - t0, 2), "err": err, "chars": len(txt), "bad": hits[:6]})
            if hits: log["bad"].append({"step": name, "hits": hits[:6]})
            print(f"{n:02d} {name:55s} {time.time() - t0:6.2f}s {'ERR ' + err if err else ''} {'BAD ' + str(hits[:2]) if hits else ''}", flush=True)
            return txt

        def tab(v): return lambda: page.click(f'button[data-view="{v}"]')
        step("load", lambda: page.goto(base, wait_until="load"), "body")
        # ---- Overview
        step("overview", tab("overview"))
        step("overview state", lambda: (page.fill("#ov-state", st), page.click("#ov-refresh")))
        step("overview state lower-case name", lambda: (page.fill("#ov-state", "zz"), page.click("#ov-refresh")))
        # ---- Clients
        step("clients", tab("clients"))
        step("clients add", lambda: (page.fill("#cl-subject", subject), page.click("#cl-add")))
        step("clients add bogus", lambda: (page.fill("#cl-subject", "NO SUCH PRACTICE XYZ"), page.click("#cl-add")))
        step("clients refresh", lambda: page.click("#cl-refresh"))
        # ---- Explorer
        step("explorer", tab("explorer"))
        step("explorer sort rate asc", lambda: page.click('th[data-sort="negotiated_rate"]'))
        step("explorer sort name", lambda: page.click('th[data-sort="display_name"]'))
        step("explorer sort rate", lambda: (page.click('th[data-sort="negotiated_rate"]')))
        step("explorer next page", lambda: page.click("#pg-next"))
        step("explorer page size 250", lambda: page.select_option("#pg-size", "250"))
        step("explorer month pick", lambda: page.select_option("#f-month", index=1))
        step("explorer state", lambda: (page.fill("#f-state", st), page.dispatch_event("#f-state", "change")))
        step("explorer city", lambda: (page.fill("#f-city", city), page.dispatch_event("#f-city", "change")))
        step("explorer code chip", lambda: page.click(f'#f-cpt-chips .chip[data-cpt="{code}"]'))
        step("explorer modifier base", lambda: page.select_option("#f-modifier", "base"))
        step("explorer class prof", lambda: page.select_option("#f-class", "professional"))
        step("explorer therapy only", lambda: page.check("#f-therapy"))
        step("explorer hide outliers", lambda: page.check("#f-outliers"))
        step("explorer rate range", lambda: (page.fill("#f-rate-min", "20"), page.dispatch_event("#f-rate-min", "change"), page.fill("#f-rate-max", "60"), page.dispatch_event("#f-rate-max", "change")))
        step("explorer grain entity", lambda: page.click('#f-grain button[data-g="entity"]'))
        step("explorer entity drawer", lambda: page.click("#rates-body tr.clickable >> nth=0"), "#drawer")
        step("explorer close drawer", lambda: page.keyboard.press("Escape"))
        step("explorer grain npi", lambda: page.click('#f-grain button[data-g="npi"]'))
        step("explorer npi drawer", lambda: page.click("#rates-body tr.clickable >> nth=0"), "#drawer")
        step("explorer close drawer2", lambda: page.keyboard.press("Escape"))
        step("explorer grain tin", lambda: page.click('#f-grain button[data-g="tin"]'))
        step("explorer tin drawer", lambda: page.click("#rates-body tr.clickable >> nth=0"), "#drawer")
        step("explorer close drawer3", lambda: page.keyboard.press("Escape"))
        step("explorer export csv", lambda: page.click("#btn-export"))
        step("explorer export zip", lambda: page.click("#btn-export-zip"))
        step("explorer clear", lambda: page.click("#f-clear"))
        step("explorer search name", lambda: (page.fill("#f-q", subject), page.dispatch_event("#f-q", "input")))
        step("explorer search junk", lambda: (page.fill("#f-q", "zzzzqqq"), page.dispatch_event("#f-q", "input")))
        step("explorer clear2", lambda: page.click("#f-clear"))
        step("explorer grain entity nofilter", lambda: page.click('#f-grain button[data-g="entity"]'))
        step("explorer grain npi nofilter", lambda: page.click('#f-grain button[data-g="npi"]'))
        step("explorer grain tin again", lambda: page.click('#f-grain button[data-g="tin"]'))
        # ---- Code comparison
        step("code tab", tab("cpt"))
        step("code pick", lambda: page.click(f'#cpt-chips .chip[data-cpt="{code}"]'))
        step("code state", lambda: (page.fill("#cpt-state-filter", st), page.dispatch_event("#cpt-state-filter", "input")))
        step("code base off", lambda: page.uncheck("#cpt-base-only"))
        step("code row drawer", lambda: page.click("#cpt-body tr.clickable >> nth=0"), "#drawer")
        step("code close drawer", lambda: page.keyboard.press("Escape"))
        step("code export", lambda: page.click("#cpt-export"))
        # ---- Markets
        step("markets tab", tab("markets"))
        step("markets run national", lambda: (page.fill("#mk-code", code), page.click("#mk-run")))
        step("markets run state+zip", lambda: (page.fill("#mk-state", st), page.fill("#mk-zip", zipc), page.click("#mk-run")))
        step("markets therapy only", lambda: (page.check("#mk-therapy"), page.click("#mk-run")))
        step("markets bad code", lambda: (page.fill("#mk-code", "99999"), page.click("#mk-run")))
        # ---- Rate changes
        step("changes tab", tab("changes"))
        step("changes run latest", lambda: page.click("#ch-run"))
        step("changes trajectory national", lambda: page.click("#ch-traj-run"))
        step("changes state + trajectory", lambda: (page.fill("#ch-state", st), page.click("#ch-traj-run")))
        step("changes run state", lambda: page.click("#ch-run"))
        step("changes pinned month", lambda: (page.select_option("#ch-month", index=1), page.fill("#ch-state", ""), page.click("#ch-run")))
        step("changes csv", lambda: page.click("#ch-csv"))
        step("changes bad state", lambda: (page.fill("#ch-state", "ZZ"), page.click("#ch-run")))
        # ---- Sources
        step("sources tab", tab("sources"))
        step("sources pick state", lambda: page.select_option("#src-state", st))
        for pg in popups:
            try: log["popups"].append({"url": pg.url[:60], "head": re.sub(r"\s+", " ", pg.inner_text("body")[:200])})
            except Exception as e: log["popups"].append({"err": str(e)[:100]})
        log["requests"] = reqlog
        br.close()
    (OUT / f"{label}.json").write_text(json.dumps(log, indent=1))
    print("pageerrors", len(log["pageerrors"]), "console", len(log["console"]), "failed", len(log["failed"]), "bad", len(log["bad"]), "downloads", len(log["downloads"]))
    for k in ("pageerrors", "console", "failed"):
        for x in log[k][:12]: print(k, x)

if __name__ == "__main__":
    main()
