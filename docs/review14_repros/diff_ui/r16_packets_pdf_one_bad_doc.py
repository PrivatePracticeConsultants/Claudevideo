"""`mrfx packets --pdf`: one document Chromium cannot print (r08 saw
'Page.pdf: Protocol error (Page.printToPDF): Printing failed' on a big
report) aborts the whole batch with an exception — html_files_to_pdf catches
only PdfUnavailable (pdf.py:96), cmd_packets catches nothing around it
(cli.py:1204). The failure is injected for the 2nd of 3 documents."""
from pathlib import Path
import tempfile
import h
from mrfx.config import MrfxConfig
from mrfx import pdf
from playwright._impl._errors import Error as PwError

if __name__ == "__main__":
    d = Path(tempfile.mkdtemp(dir=Path(__file__).parent, prefix="t16_"))
    paths = []
    for i in range(3):
        p = d / f"{i+1}_doc.html"; p.write_text(f"<h1>doc {i+1}</h1>"); paths.append(p)
    real = pdf._print; n = {"i": 0}
    def flaky(page, html):
        n["i"] += 1
        if n["i"] == 2:
            raise PwError("Page.pdf: Protocol error (Page.printToPDF): Printing failed")
        return real(page, html)
    pdf._print = flaky
    try:
        written, why = pdf.html_files_to_pdf(MrfxConfig(render_auto_install=False), paths)
        print("returned:", [p.name for p in written], why)
    except Exception as e:
        print("html_files_to_pdf RAISED", type(e).__name__, str(e)[:100])
    print("PDFs on disk:", sorted(p.name for p in d.glob("*.pdf")), "— doc 3 never attempted")
    import shutil; shutil.rmtree(d)
