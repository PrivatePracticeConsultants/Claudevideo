"""One-click PDF of any printable report (review 13 Group 6.1).

Every report is a self-contained HTML page (no CDN, no fetch — the single-file
rule), so printing it is a pure function of its bytes: the optional headless
Chromium that mrfx/render.py already finds or installs for JavaScript portals
(render._launch: Playwright's own build, a browser at a known path such as
PLAYWRIGHT_BROWSERS_PATH, or — when `render_auto_install` allows — a one-time
`playwright install chromium`) loads the HTML with EVERY network request
refused and prints it with the page's own @page size (landscape where the
print CSS says so) and its colours.

Invariant 3: a missing browser is a sentence (PdfUnavailable), never a broken
report — the HTML stays one click away. Invariant 4: the PDF is the HTML,
printed; nothing is added or recomputed. Byte-stable except the creation date
Chromium stamps in the document's metadata."""
from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

log = logging.getLogger(__name__)

# review 14 Group 3: the print runs in a CHILD process — a Stop or the
# deadline kills it (Stop did nothing for the 18 s Chromium printed, and a
# 150k-row report's print was killed by the system for memory, taking the
# printer down with it) — and a report above the page budget is refused with
# a sentence before Chromium is started.
PRINT_TIMEOUT_S = 180.0
PAGE_BUDGET = 400                 # pages; ~40 table rows a landscape page
ROWS_PER_PAGE = 40
_SLOTS = threading.BoundedSemaphore(2)   # two prints at once at most

TOO_BIG = ("This report is about {pages:,} pages — more than the {budget:,} pages the "
           "built-in printer takes on. Use \"Open printable\" and your browser's Print -> "
           "Save as PDF, or narrow it (a state, a payer, fewer codes).")
FAILED = ("The built-in printer could not print this report ({why}). Use \"Open "
          "printable\" and your browser's Print -> Save as PDF.")

UNAVAILABLE = ("PDF printing needs the optional built-in browser, which is not "
               "available on this computer ({why}). Use \"Open printable\" and your "
               "browser's Print -> Save as PDF meanwhile.")

# one print at a time: a Chromium per request on a laptop is plenty
_LOCK = threading.Lock()


class PdfUnavailable(RuntimeError):
    """No usable headless browser — the message is the user's sentence."""


class PdfRefused(RuntimeError):
    """This report is not printed here (too big, too slow, the printer
    failed) — the message is the user's sentence."""


def estimated_pages(html: str) -> int:
    rows = len(re.findall(r"<tr[\s>]", html, flags=re.I))
    return max(1, -(-rows // ROWS_PER_PAGE) + len(html) // 400_000)


def _print_child(cfg, html: str, out: Path, timeout: float | None = None) -> None:
    """Print `html` to `out` in a child process; kill it on a Stop or past
    the deadline. Raises PdfUnavailable / PdfRefused / progress.Stopped."""
    from . import progress
    pages = estimated_pages(html)
    if pages > PAGE_BUDGET:
        raise PdfRefused(TOO_BIG.format(pages=pages, budget=PAGE_BUDGET))
    timeout = PRINT_TIMEOUT_S if timeout is None else timeout
    with tempfile.TemporaryDirectory(prefix="mrfx-pdf-") as d:
        src = Path(d) / "report.html"
        src.write_text(html, encoding="utf-8")
        err = Path(d) / "err.txt"
        auto = "1" if getattr(cfg, "render_auto_install", True) else "0"
        with open(err, "wb") as eh:
            child = subprocess.Popen([sys.executable, "-m", "mrfx.pdf", str(src), str(out), auto],
                                     stdout=subprocess.DEVNULL, stderr=eh)
            t0 = time.monotonic()
            try:
                while child.poll() is None:
                    if getattr(progress.current(), "cancelled", False):
                        child.kill()
                        child.wait(10)
                        progress.check_cancelled()
                    if time.monotonic() - t0 > timeout:
                        child.kill()
                        child.wait(10)
                        raise PdfRefused(FAILED.format(
                            why=f"it was still printing after {int(timeout)} s"))
                    time.sleep(0.2)
            finally:
                if child.poll() is None:
                    child.kill()
        if child.returncode == 3:
            raise PdfUnavailable(err.read_text(errors="replace").strip()[-400:]
                                 or UNAVAILABLE.format(why="no browser"))
        if child.returncode != 0 or not out.exists():
            tail = err.read_text(errors="replace").strip().splitlines()
            why = (tail[-1] if tail else f"exit code {child.returncode}")[:200]
            if child.returncode and child.returncode < 0:
                why = "the printer was stopped by the system (out of memory?)"
            raise PdfRefused(FAILED.format(why=why))


def _child_main(argv: list[str]) -> int:
    """`python -m mrfx.pdf <in.html> <out.pdf> <auto_install 0|1>`."""
    src, out, auto = Path(argv[0]), Path(argv[1]), argv[2] == "1"

    class _Cfg:
        render_auto_install = auto
    try:
        from playwright.sync_api import sync_playwright
    except Exception:  # noqa: BLE001
        print(UNAVAILABLE.format(why="the playwright package is not installed"), file=sys.stderr)
        return 3
    try:
        with sync_playwright() as pw:
            try:
                browser = _browser(pw, _Cfg())
            except PdfUnavailable as e:
                print(str(e), file=sys.stderr)
                return 3
            try:
                data = _print(browser.new_page(), src.read_text(encoding="utf-8"))
            finally:
                try:
                    browser.close()
                except Exception:  # noqa: BLE001
                    pass
        tmp = out.with_name(out.name + ".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, out)
        return 0
    except Exception as e:  # noqa: BLE001 — the parent turns it into a sentence
        print(f"{type(e).__name__}: {str(e).splitlines()[0][:180] if str(e) else ''}",
              file=sys.stderr)
        return 1


def _browser(pw, cfg):
    from . import render
    render._auto_install_enabled = bool(getattr(cfg, "render_auto_install", True))
    try:
        return render._launch(pw)
    except Exception as e:  # noqa: BLE001 — every launch failure is "unavailable"
        raise PdfUnavailable(UNAVAILABLE.format(why=str(e).splitlines()[0][:160])) from e


def _print(page, html: str) -> bytes:
    # a report never needs the network: refuse every request (a stray
    # third-party URL in a payer's name must not be fetched while printing)
    page.route("**/*", lambda route: route.abort())
    page.set_content(html, wait_until="load")
    return page.pdf(print_background=True, prefer_css_page_size=True,
                    margin={"top": "10mm", "bottom": "10mm", "left": "10mm", "right": "10mm"})


def html_to_pdf(cfg, html: str) -> bytes:
    """The PDF of one HTML report, printed in a child process. Raises
    PdfUnavailable / PdfRefused with a sentence, progress.Stopped on a Stop."""
    from . import progress
    progress.current().stage("Printing the PDF")
    with _SLOTS, tempfile.TemporaryDirectory(prefix="mrfx-pdfout-") as d:
        out = Path(d) / "report.pdf"
        _print_child(cfg, html, out)
        return out.read_bytes()


def html_files_to_pdf(cfg, paths: list[Path]) -> tuple[list[Path], str | None]:
    """Print every HTML file beside itself as .pdf (the monthly packets'
    --pdf). One document that cannot be printed is SKIPPED with its reason
    and the rest continue (review 14 Group 3: one failure aborted the batch
    with a traceback). Returns (written, a sentence about what was not
    printed, or None)."""
    written: list[Path] = []
    skipped: list[str] = []
    for p in paths:
        try:
            with _SLOTS:
                _print_child(cfg, p.read_text(encoding="utf-8"), p.with_suffix(".pdf"))
            written.append(p.with_suffix(".pdf"))
        except PdfUnavailable as e:
            return written, str(e)
        except PdfRefused as e:
            skipped.append(f"{p.parent.name}/{p.name}: {e}")
        except Exception as e:  # noqa: BLE001 — one document never stops the batch
            skipped.append(f"{p.parent.name}/{p.name}: {type(e).__name__}")
    if skipped:
        return written, (f"{len(skipped)} document{'s were' if len(skipped) != 1 else ' was'} "
                         "not printed — " + "; ".join(skipped))
    return written, None


if __name__ == "__main__":
    raise SystemExit(_child_main(sys.argv[1:]))
