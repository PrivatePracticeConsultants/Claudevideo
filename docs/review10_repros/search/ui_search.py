"""Type into the REAL dashboard search boxes like a person and record, per scenario:
every /api request (url, start, headers, body-complete, status, bytes), DOM mutation
times of the result areas, long main-thread tasks, and the time from the first
keypress/click to the last result mutation.

usage: ui_search.py <base_url> <label> <cfg.json> [outdir]
cfg.json: {"name":..., "partial":..., "tin":..., "npi":..., "cpt":..., "cpt2":..., "subject":..., "state":...}
"""
import json
import subprocess
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

INIT = r"""
(() => {
  const L = (window.__log = { req: [], mut: [], acts: [], long: [] });
  const of = window.fetch;
  window.fetch = async function (input, init) {
    const url = typeof input === "string" ? input : input.url;
    const rec = { url, method: (init && init.method) || "GET", t0: performance.now() };
    L.req.push(rec);
    if (init && init.signal) init.signal.addEventListener("abort", () => { rec.aborted = performance.now(); });
    try {
      const r = await of.apply(this, arguments);
      rec.th = performance.now(); rec.status = r.status;
      r.clone().arrayBuffer().then((b) => { rec.t1 = performance.now(); rec.bytes = b.byteLength; })
        .catch((e) => { rec.t1 = performance.now(); rec.err = String(e); });
      return r;
    } catch (e) { rec.t1 = performance.now(); rec.err = String(e); throw e; }
  };
  for (const ev of ["keydown", "click", "change"])
    document.addEventListener(ev, (e) => L.acts.push({ t: performance.now(), ev, id: e.target && e.target.id }), true);
  try {
    new PerformanceObserver((l) => { for (const e of l.getEntries()) L.long.push({ t: e.startTime, d: e.duration }); })
      .observe({ entryTypes: ["longtask"] });
  } catch (e) {}
  const start = () => {
    new MutationObserver((ms) => {
      const t = performance.now(); const seen = new Set();
      for (const m of ms) {
        let n = m.target; while (n && !n.id) n = n.parentElement;
        const id = n ? n.id : "?"; if (id === "tooltip" || seen.has(id)) continue; seen.add(id);
        const el = n; let info = "";
        if (el) {
          if (id === "rates-body" || id === "cpt-body") info = "rows=" + el.children.length;
          else if (el.tagName === "DATALIST") info = "opts=" + el.children.length;
          else info = (el.querySelector(".loading") ? "LOADING " : "") + (el.textContent || "").trim().slice(0, 70);
        }
        L.mut.push({ t, id, info });
      }
    }).observe(document.documentElement, { childList: true, subtree: true, characterData: true });
  };
  if (document.documentElement) start(); else document.addEventListener("DOMContentLoaded", start);
})();
"""


def load():
    return subprocess.run(["uptime"], capture_output=True, text=True).stdout.strip().split("load average:")[-1].strip()


def main():
    base, label, cfgp = sys.argv[1:4]
    outdir = Path(sys.argv[4] if len(sys.argv) > 4 else "/tmp/mrfx_review10/search/out")
    cfg = json.loads(Path(cfgp).read_text())
    res = {"label": label, "base": base, "cfg": cfg, "scenarios": []}
    with sync_playwright() as p:
        br = p.chromium.launch(executable_path="/opt/pw-browsers/chromium", args=["--headless=new", "--no-sandbox"])
        ctx = br.new_context(viewport={"width": 1500, "height": 950})
        page = ctx.new_page()
        page.set_default_timeout(15000)
        page.add_init_script(INIT)
        errs = []
        page.on("pageerror", lambda e: errs.append(str(e)[:300]))

        def settle(quiet_ms=900, timeout_s=240):
            """No request pending and nothing changed for quiet_ms (the debounce is 300 ms)."""
            t_end = time.time() + timeout_s
            while time.time() < t_end:
                st = page.evaluate("""() => { const L = window.__log; const now = performance.now();
                    const pend = L.req.filter(r => r.t1 === undefined && !r.aborted).length;
                    const lastReq = Math.max(0, ...L.req.map(r => r.t1 || r.t0));
                    const lastMut = L.mut.length ? L.mut[L.mut.length-1].t : 0;
                    const lastAct = L.acts.length ? L.acts[L.acts.length-1].t : 0;
                    return {pend, idle: now - Math.max(lastReq, lastMut, lastAct)}; }""")
                if st["pend"] == 0 and st["idle"] > quiet_ms:
                    return True
                page.wait_for_timeout(100)
            return False

        def scenario(name, act, targets, quiet_ms=900):
            page.evaluate("() => { const L = window.__log; if (!L) return; L.req.length = 0; L.mut.length = 0; L.acts.length = 0; L.long.length = 0; }")
            ld = load()
            w0 = time.time()
            try:
                act()
                ok = settle(quiet_ms)
            except Exception as e:  # noqa: BLE001
                ok = False
                errs.append(f"{name}: {str(e)[:200]}")
            L = page.evaluate("() => window.__log")
            acts = L["acts"]
            t0 = acts[0]["t"] if acts else (L["req"][0]["t0"] if L["req"] else 0)
            if name.startswith("page load"):
                t0 = 0  # navigation start
            tlast_key = max([a["t"] for a in acts], default=t0)
            reqs = [{"url": r["url"].replace(base, ""), "m": r["method"], "start": round(r["t0"] - t0),
                     "hdr": round(r.get("th", r.get("t1", 0)) - t0) if (r.get("th") or r.get("t1")) else None,
                     "end": round(r["t1"] - t0) if r.get("t1") is not None else None,
                     "dur": round(r["t1"] - r["t0"]) if r.get("t1") is not None else None,
                     "status": r.get("status"), "bytes": r.get("bytes"),
                     "aborted": bool(r.get("aborted")), "err": r.get("err")}
                    for r in L["req"] if "/api/" in r["url"]]
            tm = [m for m in L["mut"] if m["id"] in targets]
            per = {}
            for m in tm:
                per.setdefault(m["id"], []).append({"t": round(m["t"] - t0), "info": m["info"]})
            last = max([m["t"] for m in tm], default=None)
            sc = {"name": name, "ok": ok, "load": ld, "wall_s": round(time.time() - w0, 2),
                  "typing_ms": round(tlast_key - t0),
                  "first_action_to_last_render_ms": round(last - t0) if last else None,
                  "last_key_to_last_render_ms": round(last - tlast_key) if last else None,
                  "n_requests": len(reqs), "requests": reqs,
                  "render": {k: (v if len(v) <= 8 else v[:3] + [{"...": len(v) - 6}] + v[-3:]) for k, v in per.items()},
                  "longtasks": [{"t": round(x["t"] - t0), "d": round(x["d"])} for x in L["long"]]}
            res["scenarios"].append(sc)
            print(f"[{label}] {name:38s} load {ld:18s} reqs {len(reqs):2d} typing {sc['typing_ms']:5d} ms  "
                  f"first-key->render {sc['first_action_to_last_render_ms']} ms  last-key->render {sc['last_key_to_last_render_ms']} ms"
                  f"{'' if ok else '  !! not settled'}", flush=True)
            return sc

        EX = {"rates-body", "rates-state", "summary-strip", "summary-bycode", "pg-label"}
        q = "#f-q"

        def clear_q():
            page.click(q); page.keyboard.press("Control+A"); page.keyboard.press("Delete")

        def typ(sel, text, delay):
            page.click(sel)
            page.keyboard.type(text, delay=delay)

        # ---- Explorer ----
        scenario("page load (Explorer default)", lambda: page.goto(base, wait_until="domcontentloaded"), EX | {"hdr-stats"})
        scenario("type full name @120ms/key", lambda: typ(q, cfg["name"], 120), EX)
        scenario("clear search box", clear_q, EX)
        scenario("type partial name @120ms/key", lambda: typ(q, cfg["partial"], 120), EX)
        scenario("clear search box (2)", clear_q, EX)
        scenario("type partial name @400ms/key (slow typist)", lambda: typ(q, cfg["partial"], 400), EX)
        scenario("clear search box (3)", clear_q, EX)
        scenario("type 9-digit TIN @120ms/key", lambda: typ(q, cfg["tin"], 120), EX)
        scenario("clear search box (4)", clear_q, EX)
        scenario("type 9-digit TIN @400ms/key (slow typist)", lambda: typ(q, cfg["tin"], 400), EX)
        scenario("click first row -> drawer", lambda: page.click("#rates-body tr.clickable"), {"drawer", "entity-chart"})
        scenario("close drawer", lambda: page.keyboard.press("Escape"), {"drawer"}, quiet_ms=400)
        scenario("clear search box (5)", clear_q, EX)
        scenario("type 10-digit NPI @120ms/key (TIN grain)", lambda: typ(q, cfg["npi"], 120), EX)
        scenario("switch grain -> NPI (NPI in box)", lambda: page.click('#f-grain button[data-g="npi"]'), EX)
        scenario("switch grain -> Entity (NPI in box)", lambda: page.click('#f-grain button[data-g="entity"]'), EX)
        scenario("switch grain -> TIN (NPI in box)", lambda: page.click('#f-grain button[data-g="tin"]'), EX)
        scenario("clear search box (6)", clear_q, EX)
        scenario(f"click CPT chip {cfg['cpt']}", lambda: page.click(f'#f-cpt-chips .chip[data-cpt="{cfg["cpt"]}"]'), EX)
        scenario("CPT on: type partial name @120ms/key", lambda: typ(q, cfg["partial"], 120), EX)
        scenario("CPT on + name: grain -> Entity", lambda: page.click('#f-grain button[data-g="entity"]'), EX)
        scenario("CPT on + name: grain -> NPI", lambda: page.click('#f-grain button[data-g="npi"]'), EX)
        scenario("CPT on + name: grain -> TIN", lambda: page.click('#f-grain button[data-g="tin"]'), EX)
        scenario("clear search box (7)", clear_q, EX)
        scenario("sort by Entity name (CPT on)", lambda: page.click('#rates-table thead th[data-sort="display_name"]'), EX)
        scenario("next page (CPT on)", lambda: page.click("#pg-next"), EX)
        scenario("page size 250 (CPT on)", lambda: page.select_option("#pg-size", "250"), EX)
        scenario("Clear all filters", lambda: page.click("#f-clear"), EX)
        scenario("type state code @120ms/key", lambda: typ("#f-state", cfg["state"], 120), EX)
        scenario("Clear all filters (2)", lambda: page.click("#f-clear"), EX)

        # ---- Code comparison ----
        CP = {"cpt-body", "cpt-state", "cpt-payer-rank", "cpt-hist", "cpt-trend"}
        scenario("open Code comparison (auto first code)", lambda: page.click('nav button[data-view="cpt"]'), CP)
        scenario(f"Code page: click {cfg['cpt']}", lambda: page.click(f'#cpt-chips .chip[data-cpt="{cfg["cpt"]}"]'), CP)
        scenario(f"Code page: click {cfg['cpt2']}", lambda: page.click(f'#cpt-chips .chip[data-cpt="{cfg["cpt2"]}"]'), CP)
        scenario("Code page: type state", lambda: typ("#cpt-state-filter", cfg["state"], 120), CP)

        # ---- Benchmark subject autocomplete + first result ----
        BM = {"b-subjects", "b-out", "b-result", "bench-out", "view-benchmark"}
        scenario("open Benchmark tab", lambda: page.click('nav button[data-view="benchmark"]'), BM | {"b-month", "b-payer-chips"})
        scenario("Benchmark: type subject name @120ms/key", lambda: typ("#b-subject", cfg["subject"], 120), {"b-subjects"})
        scenario("Benchmark: clear + type partial @400ms/key", lambda: (page.click("#b-subject"), page.keyboard.press("Control+A"),
                                                                    page.keyboard.press("Delete"), page.keyboard.type(cfg["partial"], delay=400)), {"b-subjects"})
        def run_bench():
            page.fill("#b-subject", cfg["subject"])
            if cfg.get("bstate"):
                page.fill("#b-state", cfg["bstate"])
            page.click("#b-run")
        scenario("Benchmark: Run (first result screen)", run_bench, BM)
        scenario("Benchmark: Run again", lambda: page.click("#b-run"), BM)
        res["pageerrors"] = errs
        br.close()
    out = outdir / f"ui_{label}.json"
    out.write_text(json.dumps(res, indent=1))
    print("wrote", out, "pageerrors:", len(errs))
    for e in errs[:10]:
        print("  ERR", e)


if __name__ == "__main__":
    main()
