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
import threading
from pathlib import Path

log = logging.getLogger(__name__)

UNAVAILABLE = ("PDF printing needs the optional built-in browser, which is not "
               "available on this computer ({why}). Use \"Open printable\" and your "
               "browser's Print -> Save as PDF meanwhile.")

# one print at a time: a Chromium per request on a laptop is plenty
_LOCK = threading.Lock()


class PdfUnavailable(RuntimeError):
    """No usable headless browser — the message is the user's sentence."""


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
    """The PDF of one HTML report. Raises PdfUnavailable with a sentence."""
    try:
        from playwright.sync_api import sync_playwright
    except Exception as e:  # noqa: BLE001
        raise PdfUnavailable(UNAVAILABLE.format(why="the playwright package is not installed")) from e
    from . import progress
    progress.current().stage("Printing the PDF")
    with _LOCK, sync_playwright() as pw:
        browser = _browser(pw, cfg)
        try:
            page = browser.new_page()
            return _print(page, html)
        finally:
            try:
                browser.close()
            except Exception:  # noqa: BLE001
                pass


def html_files_to_pdf(cfg, paths: list[Path]) -> tuple[list[Path], str | None]:
    """Print every HTML file beside itself as .pdf with ONE browser (the
    monthly packets' --pdf). Returns (written, why-not or None)."""
    written: list[Path] = []
    try:
        from playwright.sync_api import sync_playwright
    except Exception:  # noqa: BLE001
        return written, UNAVAILABLE.format(why="the playwright package is not installed")
    try:
        with _LOCK, sync_playwright() as pw:
            browser = _browser(pw, cfg)
            try:
                for p in paths:
                    page = browser.new_page()
                    try:
                        out = p.with_suffix(".pdf")
                        out.write_bytes(_print(page, p.read_text(encoding="utf-8")))
                        written.append(out)
                    finally:
                        page.close()
            finally:
                browser.close()
    except PdfUnavailable as e:
        return written, str(e)
    return written, None
