"""Files tab in Chromium on the tiny store: upload a new file, the same file again, a different file with
the same name; then render the download queue with a row in every status (intercepted /api/urls)."""
import json, re, sys, time
from pathlib import Path
from playwright.sync_api import sync_playwright
sys.path.insert(0, str(Path(__file__).parent / "t"))
OUT = Path("/tmp/mrfx_review9/outputs")
BASE = "http://localhost:8651"

if __name__ == "__main__":
    import fx
    T = {k: f"5{i}1000001" for i, k in enumerate("AB", start=1)}
    npis = {t: ["1" + t] for t in T.values()}
    up = OUT / "ui_upload"; up.mkdir(exist_ok=True)
    (up / "one").mkdir(exist_ok=True); (up / "two").mkdir(exist_ok=True)
    (up / "one" / "sigma_2026-06.json").write_text(json.dumps(fx.doc("Sigma Health", "2026-06-01", {"97110": {T["A"]: 61, T["B"]: 62}}, npis)))
    (up / "two" / "sigma_2026-06.json").write_text(json.dumps(fx.doc("Sigma Health", "2026-06-01", {"97110": {T["A"]: 71, T["B"]: 72}}, npis)))
    snap = json.loads((OUT / "urls_snapshot.json").read_text())
    log = {"errors": [], "dialogs": [], "failed": []}
    with sync_playwright() as p:
        br = p.chromium.launch(executable_path="/opt/pw-browsers/chromium", args=["--headless=new", "--no-sandbox"])
        page = br.new_context(viewport={"width": 1500, "height": 1100}).new_page()
        page.set_default_timeout(8000)
        page.on("pageerror", lambda e: log["errors"].append(str(e)[:300]))
        page.on("console", lambda m: m.type == "error" and log["errors"].append("console: " + m.text[:300]))
        page.on("dialog", lambda d: (log["dialogs"].append(d.message[:300]), d.accept()))
        page.on("response", lambda r: "/api/" in r.url and r.status >= 400 and log["failed"].append((r.status, r.url[-80:])))
        page.goto(BASE, wait_until="load")
        page.click('nav button[data-view="files"]')
        page.wait_for_timeout(1500)
        print("settings line:", re.sub(r"\s+", " ", page.inner_text("#view-files"))[:0] or [l for l in page.inner_text("#view-files").splitlines() if "at a time" in l or "Downloads:" in l][:2])

        def files_rows():
            rows = page.evaluate("""() => [...document.querySelectorAll('#view-files table tbody tr')].map(tr => tr.innerText.replace(/\\s+/g,' ').slice(0,170))""")
            return [r for r in rows if "sigma" in r.lower()]

        def api_files():
            return page.evaluate("""async () => { const d = await (await fetch('/api/files?limit=500')).json(); return d.files.filter(f => f.filename.includes('sigma')).map(f => [f.filename, f.status, f.rows_emitted ?? f.rows ?? null]) }""")

        for label, path in (("NEW", up / "one" / "sigma_2026-06.json"), ("SAME AGAIN", up / "one" / "sigma_2026-06.json"),
                            ("DIFFERENT, SAME NAME", up / "two" / "sigma_2026-06.json")):
            page.evaluate("""([name, text]) => { const dt = new DataTransfer(); dt.items.add(new File([text], name, {type: 'application/json'}));
                document.querySelector('#dropzone').dispatchEvent(new DragEvent('drop', {dataTransfer: dt, bubbles: true, cancelable: true})); }""", [path.name, path.read_text()])
            page.wait_for_timeout(2500)
            dz = re.sub(r"\s+", " ", page.inner_text("#dropzone"))
            for _ in range(40):
                st = api_files()
                if st and all(s[1] in ("done", "failed", "skipped") for s in st):
                    break
                page.wait_for_timeout(500)
            page.wait_for_timeout(4500)   # one poll
            print(f"--- upload {label}\n   dropzone says: {dz}\n   api: {api_files()}\n   table rows: {files_rows()}")
        page.screenshot(path=str(OUT / "shots_tiny" / "files_after_uploads.png"), full_page=True)
        rates = page.evaluate("""async () => { const d = await (await fetch('/api/rates?grain=tin&dollar_only=1&payer=Sigma%20Health&page_size=50')).json(); return d.rows.map(r => [r.display_name, r.negotiated_rate, r.source_files]) }""")
        print("Sigma rates in the Explorer now:", rates)
        # ---- queue rendering with one row per status
        page.route("**/api/urls", lambda route: route.fulfill(status=200, content_type="application/json", body=json.dumps(snap)))
        page.wait_for_timeout(5500)
        qtxt = page.evaluate("""() => [...document.querySelectorAll('#view-files tr')].map(tr => tr.innerText.replace(/\\s+/g,' ').slice(0,260)).filter(t => t.includes('files.example.org') || t.includes('_2026-06'))""")
        print("--- queue rows as rendered:")
        for u in snap["urls"]:
            key = u["url"].split("/")[-1][:14]
            hit = [t for t in qtxt if key in t]
            print(f"   {u['status']:11s} waiting_on={u.get('waiting_on')} park={u.get('park_count')} big_lane={u.get('big_lane')} -> {hit[0][:230] if hit else 'NOT RENDERED'}")
        counts_line = [l for l in page.inner_text("#view-files").splitlines() if re.search(r"queued|waiting|downloading", l)][:6]
        print("   counts/badges:", counts_line)
        whole = page.inner_text("#view-files")
        print("   bad tokens:", sorted(set(re.findall(r"\bundefined\b|\bNaN\b|\bnull\b|\[object Object\]", whole))))
        page.screenshot(path=str(OUT / "shots_tiny" / "files_queue.png"), full_page=True)
        br.close()
    print("log:", json.dumps(log)[:1500])
