"""PDF (6.1): logo, page size, determinism, a very long report, two at once,
and no Chromium (in a subprocess with an empty browsers path; auto-install OFF)."""
import base64, os, re, subprocess, sys, threading, time, zlib
from pathlib import Path
import h
from mrfx.config import MrfxConfig, ReportBranding
from mrfx import pdf

HERE = Path(__file__).parent

def mediaboxes(b):
    return sorted(set(re.findall(rb"/MediaBox \[([^\]]+)\]", b)))

def main():
    # a 1x1 red PNG logo
    png = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGP4z8DwHwAFAAH/iZk9HQAAAABJRU5ErkJggg==")
    logo = HERE / "logo.png"; logo.write_bytes(png)
    cfg = MrfxConfig(report_branding=ReportBranding(name="Acme Consulting", logo_path=logo),
                     render_auto_install=False)
    from mrfx.benchmark import _brand_header
    html = f"""<html><head><style>@page {{ size: landscape; margin: 10mm; }}</style></head>
      <body>{_brand_header(cfg)}<h1>Report</h1><p>Generated 2026-11-12</p></body></html>"""
    a = pdf.html_to_pdf(cfg, html); time.sleep(1.1); b = pdf.html_to_pdf(cfg, html)
    print("logo image objects in PDF:", a.count(b"/Subtype /Image"), "| MediaBox:", mediaboxes(a))
    diff = [i for i in range(min(len(a), len(b))) if a[i] != b[i]]
    print("two prints of the same HTML: len", len(a), len(b), "| differing bytes:", len(diff))
    if diff:
        i = diff[0]; print("   first difference near:", a[max(0, i-40):i+30])
    # a very long report: 40,000 table rows
    rows = "".join(f"<tr><td>{i}</td><td>CLINIC {i} REHAB</td><td>${i*1.01:,.2f}</td></tr>" for i in range(40000))
    big = f"<html><body><table>{rows}</table></body></html>"
    t = time.time()
    try:
        out = pdf.html_to_pdf(cfg, big)
        print(f"40k-row report: {len(big)/1e6:.1f} MB HTML -> {len(out)/1e6:.1f} MB PDF in {time.time()-t:.1f} s")
    except Exception as e:
        print(f"40k-row report raised {type(e).__module__}.{type(e).__name__} after {time.time()-t:.1f} s:",
              str(e).splitlines()[0][:160], "| is PdfUnavailable:", isinstance(e, pdf.PdfUnavailable))
    # two at once
    times = {}
    def one(k):
        s = time.time(); pdf.html_to_pdf(cfg, html); times[k] = (s, time.time())
    ts = [threading.Thread(target=one, args=(k,)) for k in "ab"]
    t0 = time.time(); [x.start() for x in ts]; [x.join() for x in ts]
    print("two concurrent PDFs: wall %.1f s; each ended at %s" % (time.time()-t0,
          {k: round(v[1]-t0, 1) for k, v in times.items()}))
    # no Chromium
    empty = HERE / "no_browsers"; empty.mkdir(exist_ok=True)
    env = dict(os.environ, PLAYWRIGHT_BROWSERS_PATH=str(empty), PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD="1",
               HOME=str(empty))
    code = ("import sys; sys.path.insert(0,'/home/user/Claudevideo');"
            "from mrfx.config import MrfxConfig; from mrfx import pdf\n"
            "try:\n pdf.html_to_pdf(MrfxConfig(render_auto_install=False), '<p>x</p>'); print('PRINTED')\n"
            "except pdf.PdfUnavailable as e: print('PdfUnavailable:', e)\n"
            "except Exception as e: print('OTHER', type(e).__name__, e)\n")
    r = subprocess.run([sys.executable, "-I", "-c", code], env=env, capture_output=True, text=True, timeout=120)
    print("no Chromium:", (r.stdout or r.stderr).strip()[:400])

if __name__ == "__main__":
    main()
