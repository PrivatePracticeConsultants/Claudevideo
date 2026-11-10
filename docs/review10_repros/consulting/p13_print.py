"""Print-readiness of every HTML document: at Letter width with print CSS, does anything overflow the page? pages per doc."""
import glob, os, sys
from playwright.sync_api import sync_playwright
if __name__ == "__main__":
    files = sorted(glob.glob("/tmp/mrfx_review10/consulting/bodies_run2/*report*.1") + glob.glob("/tmp/mrfx_review10/consulting/bodies_run2/*one_pager*.1")
                   + glob.glob("/tmp/mrfx_review10/consulting/bodies_run2/rate_card_name_.1") + glob.glob("/tmp/mrfx_review10/consulting/bodies_run2/dossier_name_.1")
                   + glob.glob("/tmp/mrfx_review10/consulting/out/full/b/*.html"))
    os.makedirs("/tmp/mrfx_review10/consulting/pdf", exist_ok=True)
    with sync_playwright() as p:
        br = p.chromium.launch(executable_path="/opt/pw-browsers/chromium", args=["--headless=new", "--no-sandbox"])
        pg = br.new_page(viewport={"width": 816, "height": 1056})
        errs = []
        pg.on("pageerror", lambda e: errs.append(str(e)[:100]))
        for f in files:
            html = open(f, encoding="utf-8").read()
            if "<html" not in html[:300].lower(): continue
            pg.set_content(html, wait_until="load")
            pg.emulate_media(media="print")
            r = pg.evaluate("""() => { const de = document.documentElement; const wide = [...document.querySelectorAll('table,svg,p,div')].filter(e => e.getBoundingClientRect().right > de.clientWidth + 1).map(e => e.tagName + ':' + Math.round(e.getBoundingClientRect().right));
                return {sw: de.scrollWidth, cw: de.clientWidth, wide: wide.slice(0, 4), tables: document.querySelectorAll('table').length, thead: document.querySelectorAll('thead').length}; }""")
            out = "/tmp/mrfx_review10/consulting/pdf/" + os.path.basename(f).replace(".html", "").replace(".1", "") + ("_fx" if "/out/" in f else "") + ".pdf"
            pg.pdf(path=out, format="Letter")
            import re
            n = len(re.findall(rb"/Type\s*/Page[^s]", open(out, "rb").read()))
            print(f"{os.path.basename(f)[:34]:34s} pages {n} scrollW {r['sw']} clientW {r['cw']} overflow {r['wide']} tables {r['tables']}")
        print("page errors:", errs)
        br.close()
