"""Headless-browser fallback for JavaScript-only payer portals.

Many payers (Aetna, Kaiser, HealthSparq-hosted Blues, Premera, Regence…)
render their MRF file list client-side: the HTML the server sends contains no
links at all, and the list arrives later through the portal's own API calls.
Static scraping is blind there. This module renders the page in headless
Chromium (via Playwright) and harvests file links from BOTH:

  1. the rendered DOM (anchors, data-* attributes, inline config), and
  2. every JSON response the page fetches while loading — SPAs usually pull
     their file list from an API, so the URLs are in the response bodies
     even when the DOM never shows them (paginated lists, lazy tabs).

The browser does the RENDERING only — it never talks to the network itself.
Every request the page makes is intercepted and fetched with the app's own
HTTP stack (httpx), which honors proxy settings and CA bundles that Chromium
can't see (corporate/CI proxies), keeps TLS verification on, and reuses the
app's TLS chain-repair logic. The fetched bytes are handed back to the page,
and JSON bodies are scanned for file links on the way through.

Playwright is an OPTIONAL dependency: everything degrades to a friendly
"paste the links yourself" message when it isn't installed. Enable with:

    pip install playwright && playwright install chromium
"""

from __future__ import annotations

import glob
import logging
import os

from .config import MrfxConfig

log = logging.getLogger(__name__)

# hard ceiling on a render: a portal that hasn't produced its file list in
# this long isn't going to (networkidle usually lands well under 15s)
RENDER_TIMEOUT_MS = 45_000
SETTLE_MS = 2_500          # after networkidle: lazy tabs, delayed XHRs
MAX_BODY_BYTES = 20 << 20  # never buffer a huge response into the page
_SKIP_RESOURCES = {"image", "media", "font"}  # invisible to link harvesting


def render_available() -> bool:
    try:
        import playwright.sync_api  # noqa: F401
        return True
    except ImportError:
        return False


def _find_chromium() -> str | None:
    """Locate a Chromium binary when Playwright's own build is missing —
    e.g. the installed package expects build N but the machine has N-1 (or a
    distro-provided binary at a stable path). Checked only as a fallback."""
    roots = [os.environ.get("PLAYWRIGHT_BROWSERS_PATH", ""),
             os.path.expanduser("~/.cache/ms-playwright")]
    for root in filter(None, roots):
        stable = os.path.join(root, "chromium")  # some images ship this symlink
        if os.path.isfile(stable) and os.access(stable, os.X_OK):
            return stable
        hits = sorted(glob.glob(os.path.join(root, "chromium-*", "chrome-linux", "chrome")))
        if hits:
            return hits[-1]
    return None


def _launch(pw):
    kwargs = {"headless": True}
    args = ["--disable-background-networking"]
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        # Chromium refuses its sandbox as root (containers); the browser only
        # renders bytes our own verified HTTP client fetched, so the reduced
        # sandbox is not exposed to raw network input
        args.append("--no-sandbox")
    kwargs["args"] = args
    try:
        return pw.chromium.launch(**kwargs)
    except Exception as e:  # noqa: BLE001 — retry with a discovered binary
        exe = _find_chromium()
        if exe is None or "doesn't exist" not in str(e):
            raise
        log.info("playwright's own chromium build is missing; using %s", exe)
        return pw.chromium.launch(executable_path=exe, **kwargs)


def render_page_links(cfg: MrfxConfig, url: str, max_files: int) -> list[str]:
    """Render `url` in headless Chromium and return MRF file links found in
    the rendered DOM + captured JSON responses. Best-effort: returns [] when
    Playwright is missing or the render fails — callers fall through to the
    plain-language help message."""
    if not render_available():
        return []
    import httpx

    from .fetch import BROWSER_UA, extract_links_from_text, ssl_verify

    try:
        from playwright.sync_api import sync_playwright

        seen: set[str] = set()
        links: list[str] = []
        json_bodies: list[tuple[str, str]] = []  # (final_url, body_text)

        client = httpx.Client(
            headers={"User-Agent": BROWSER_UA},
            follow_redirects=True,
            timeout=30.0,
            verify=ssl_verify(),
        )

        def fulfill(route):
            req = route.request
            if req.resource_type in _SKIP_RESOURCES:
                route.abort()
                return
            try:
                r = client.request(
                    req.method, req.url,
                    content=req.post_data_buffer,
                    headers={k: v for k, v in req.headers.items()
                             if k.lower() not in ("host", "cookie", "content-length")},
                )
                body = r.content[: MAX_BODY_BYTES + 1]
                if len(body) > MAX_BODY_BYTES:
                    route.abort()
                    return
                ctype = (r.headers.get("content-type") or "").lower()
                if len(json_bodies) < 200 and (
                        "json" in ctype or str(r.url).split("?")[0].lower().endswith(".json")):
                    json_bodies.append((str(r.url), body.decode(errors="replace")))
                route.fulfill(status=r.status_code, body=body,
                              headers={"content-type": r.headers.get("content-type", "")})
            except Exception:  # noqa: BLE001 — a failed asset must not sink the render
                try:
                    route.abort()
                except Exception:  # noqa: BLE001 — page may already be closing
                    pass

        with sync_playwright() as pw:
            browser = _launch(pw)
            try:
                page = browser.new_page(user_agent=BROWSER_UA)
                page.route("**/*", fulfill)
                page.goto(url, wait_until="networkidle", timeout=RENDER_TIMEOUT_MS)
                page.wait_for_timeout(SETTLE_MS)
                final_url = page.url
                dom_html = page.content()
            finally:
                browser.close()
                client.close()

        links += extract_links_from_text(dom_html, final_url, max_files, seen=seen)
        n_dom = len(links)
        for resp_url, body in json_bodies:
            if len(links) >= max_files:
                break
            # response bodies resolve relative paths against the API host
            links += extract_links_from_text(body, resp_url, max_files - len(links), seen=seen)
        if links:
            log.info("%s — rendered in headless browser: %d link(s) "
                     "(%d from the page, %d from its API responses)",
                     url, len(links), n_dom, len(links) - n_dom)
        else:
            log.info("%s — rendered in headless browser: no file links appeared", url)
        return links
    except Exception as e:  # noqa: BLE001 — rendering is best-effort by contract
        log.warning("headless render of %s failed: %s", url, str(e)[:200])
        return []
