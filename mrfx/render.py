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
import re
import subprocess
import sys
import threading

from .config import MrfxConfig

log = logging.getLogger(__name__)

# one Chromium at a time: several processor threads hitting several JS pages
# at once would otherwise each spawn a browser (~200 MB apiece on small boxes)
_RENDER_LOCK = threading.Lock()

# auto-download of Chromium is attempted at most once per process: if it fails
# (offline, disk full), later renders must not each block on a 10-min download.
_AUTO_INSTALL_LOCK = threading.Lock()
_auto_install_attempted = False
_auto_install_enabled = True  # set from cfg.render_auto_install at the call site


class RenderBrowserMissing(Exception):
    """Playwright is installed but its Chromium browser is not — the fix is
    one command, so callers surface it on the queue row."""

# hard ceiling on a render: a portal that hasn't produced its file list in
# this long isn't going to (networkidle usually lands well under 15s)
RENDER_TIMEOUT_MS = 45_000
SETTLE_MS = 2_500          # after networkidle: lazy tabs, delayed XHRs
MAX_BODY_BYTES = 20 << 20  # never buffer a huge response into the page
_SKIP_RESOURCES = {"image", "media", "font"}  # invisible to link harvesting

# click-through: when the first render shows no links, the list is often one
# click away — behind a "View Plan List" button or a consent overlay. Only
# obviously-MRF-ish controls are clicked, and only a bounded number.
MAX_CLICKS = 6

_CLICKABLE_TEXT_RE = re.compile(
    r"(view|show|see|display|open|get|access)[\s\w]{0,24}(plan|file|list|mrf|rate)"
    r"|machine[\s-]?readable|in[\s-]?network|table\s+of\s+contents"
    r"|transparency\s+file", re.I)
_CONSENT_TEXT_RE = re.compile(
    r"^\s*(accept(\s+all)?(\s+cookies)?|i\s+(agree|accept)|agree|continue"
    r"|ok(ay)?|got\s+it|yes|proceed|confirm)\s*[.!»›]?\s*$", re.I)


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


def _auto_install_chromium() -> bool:
    """Best-effort self-heal: a fresh machine often has the playwright PACKAGE
    (pulled in by requirements) but never ran `playwright install chromium`, so
    the browser binary is absent. Download it once — `python -m playwright
    install chromium` — instead of dropping the queue row to a manual
    "run this command" error the layperson user then has to act on.

    Attempted at most once per process (a second failure would just re-block a
    ~10-minute download). Skipped when disabled in config or when
    PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD is set (managed environments ship the
    browser at a fixed path and forbid re-fetching). Returns True only when an
    install command actually ran to a clean exit."""
    global _auto_install_attempted
    with _AUTO_INSTALL_LOCK:
        if _auto_install_attempted:
            return False
        _auto_install_attempted = True
        if not _auto_install_enabled:
            return False
        if os.environ.get("PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD"):
            log.info("chromium is missing but PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD is "
                     "set — not auto-downloading; renders will show the manual fix")
            return False
        log.info("chromium browser is missing — downloading it once now "
                 "(python -m playwright install chromium); this can take a minute…")
        try:
            proc = subprocess.run(
                [sys.executable, "-m", "playwright", "install", "chromium"],
                capture_output=True, text=True, timeout=900)
        except Exception as e:  # noqa: BLE001 — install is best-effort
            log.warning("auto-download of chromium could not start: %s", str(e)[:200])
            return False
        if proc.returncode == 0:
            log.info("chromium downloaded successfully; retrying the render")
            return True
        log.warning("auto-download of chromium exited %d: %s", proc.returncode,
                    (proc.stderr or proc.stdout or "").strip()[:300])
        return False


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
        msg = str(e).lower()
        # Missing OS libraries are NOT a missing browser: "Host system is
        # missing dependencies … playwright install-deps" and the loader's
        # "error while loading shared libraries: libnss3.so" both need system
        # packages that downloading Chromium again cannot provide — re-raise so
        # the generic handler reports the real error instead of a false
        # "couldn't download the browser".
        if any(s in msg for s in ("install-deps", "missing dependencies",
                                  "shared object", "shared librar")):
            raise
        # Match the "browser was never installed" family loosely — Playwright's
        # wording drifts across versions ("Executable doesn't exist at …",
        # "Please run the following command … playwright install"). Liberal
        # matching keeps the auto-download self-heal working after a Playwright
        # bump; a genuinely different launch failure (crash, bad flag) won't
        # match and is re-raised.
        if not any(s in msg for s in ("doesn't exist", "does not exist",
                                      "playwright install", "please run")):
            raise
        exe = _find_chromium()
        if exe is not None:
            log.info("playwright's own chromium build is missing; using %s", exe)
            return pw.chromium.launch(executable_path=exe, **kwargs)
        # No binary anywhere: download it once and retry, so the user never has
        # to run a terminal command to get JS portals rendering.
        if _auto_install_chromium():
            try:
                return pw.chromium.launch(**kwargs)
            except Exception as e2:  # noqa: BLE001 — installed to a discoverable path?
                exe = _find_chromium()
                if exe is not None:
                    return pw.chromium.launch(executable_path=exe, **kwargs)
                raise RenderBrowserMissing(str(e2)[:200]) from e2
        raise RenderBrowserMissing(str(e)[:200]) from e


def render_page_links(cfg: MrfxConfig, url: str, max_files: int) -> list[str]:
    """Render `url` in headless Chromium and return MRF file links found in
    the rendered DOM + captured JSON responses. Best-effort: returns [] when
    Playwright is missing or the render fails — callers fall through to the
    plain-language help message. Raises RenderBrowserMissing when the
    playwright PACKAGE is present but its browser was never installed, so
    the caller can put the one-line fix on the queue row."""
    if not render_available():
        return []
    global _auto_install_enabled
    _auto_install_enabled = cfg.render_auto_install
    import httpx

    from .fetch import (BROWSER_UA, _repair_incomplete_chain,
                        extract_links_from_text, ssl_verify)

    try:
        from playwright.sync_api import sync_playwright

        seen: set[str] = set()
        links: list[str] = []
        json_bodies: list[tuple[str, str]] = []  # (final_url, body_text)
        json_kept = {"bytes": 0}  # aggregate cap — 200 bodies x 20MB each would balloon
        final_urls: dict[str, str] = {}  # requested document URL -> post-redirect URL

        def make_client(verify):
            return httpx.Client(
                headers={"User-Agent": BROWSER_UA},
                follow_redirects=True,
                timeout=30.0,
                verify=verify,
            )

        # mutable so the chain-repair fallback below can swap the client in
        holder = {"client": make_client(ssl_verify())}

        def _send(req):
            """Issue the browser's request through httpx and return
            (response, body) with the body read STREAMING and hard-capped —
            r.content would buffer a clicked multi-GB rate file entirely into
            RAM before any size check could run. Returns (response, None)
            when the cap is exceeded."""
            headers = {k: v for k, v in req.headers.items()
                       if k.lower() not in ("host", "cookie", "content-length")}

            def go():
                request = holder["client"].build_request(
                    req.method, req.url, content=req.post_data_buffer, headers=headers)
                r = holder["client"].send(request, stream=True)
                try:
                    if int(r.headers.get("content-length") or 0) > MAX_BODY_BYTES:
                        return r, None
                    chunks: list[bytes] = []
                    total = 0
                    for chunk in r.iter_bytes():
                        total += len(chunk)
                        if total > MAX_BODY_BYTES:
                            return r, None
                        chunks.append(chunk)
                    return r, b"".join(chunks)
                finally:
                    r.close()

            try:
                return go()
            except httpx.ConnectError as e:
                # some payers (Aetna's health1 platform) serve an incomplete
                # certificate chain — repair it like a browser would, exactly
                # as the downloader does, then retry once
                if "CERTIFICATE_VERIFY_FAILED" not in str(e):
                    raise
                ctx = _repair_incomplete_chain(req.url)
                if ctx is None:
                    raise
                holder["client"].close()
                holder["client"] = make_client(ctx)
                return go()

        def fulfill(route):
            req = route.request
            if req.resource_type in _SKIP_RESOURCES:
                route.abort()
                return
            try:
                r, body = _send(req)
                if body is None:  # over the cap — never hand it to the page
                    route.abort()
                    return
                if req.is_navigation_request():
                    # httpx followed redirects internally, so the PAGE never
                    # sees the final URL — remember it or relative links in
                    # the DOM snapshot resolve against the pre-redirect host
                    final_urls[req.url] = str(r.url)
                ctype = (r.headers.get("content-type") or "").lower()
                if (len(json_bodies) < 200 and json_kept["bytes"] < (64 << 20) and
                        ("json" in ctype or str(r.url).split("?")[0].lower().endswith(".json"))):
                    json_bodies.append((str(r.url), body.decode(errors="replace")))
                    json_kept["bytes"] += len(body)
                route.fulfill(status=r.status_code, body=body,
                              headers={"content-type": r.headers.get("content-type", "")})
            except Exception:  # noqa: BLE001 — a failed asset must not sink the render
                try:
                    route.abort()
                except Exception:  # noqa: BLE001 — page may already be closing
                    pass

        dom_snapshots: list[tuple[str, str]] = []  # (page_url, html)
        clicks_done: list[str] = []

        def harvest_progress() -> int:
            """How many links the current snapshots + bodies would yield —
            cheap check used to stop clicking as soon as something appears."""
            probe_seen: set[str] = set()
            n = 0
            for u, html in dom_snapshots:
                n += len(extract_links_from_text(html, u, max_files, seen=probe_seen))
            for u, body in json_bodies:
                n += len(extract_links_from_text(body, u, max_files, seen=probe_seen))
            return n

        def snapshot_all(context) -> None:
            for pg in context.pages:
                try:
                    # base for relative links = where the document REALLY came
                    # from (post-redirect), not the address the page shows
                    dom_snapshots.append((final_urls.get(pg.url, pg.url), pg.content()))
                except Exception:  # noqa: BLE001 — page may be mid-navigation
                    pass

        def click_candidates(page, pattern, limit) -> int:
            """Click visible controls whose text matches `pattern`; returns
            how many were clicked. Elements are tagged first so the handles
            stay stable while we iterate."""
            try:
                labels = page.evaluate(
                    """() => Array.from(document.querySelectorAll(
                           'button, a, [role=button], input[type=button], input[type=submit], summary'))
                        .filter(el => el.offsetParent !== null)
                        .map((el, i) => { el.setAttribute('data-mrfx-i', i);
                                          return {i, text: (el.innerText || el.value || '').trim().slice(0, 90)}; })"""
                ) or []
            except Exception:  # noqa: BLE001
                return 0
            n = 0
            for ent in labels:
                if n >= limit:
                    break
                text = ent.get("text") or ""
                if not text or not pattern.search(text):
                    continue
                try:
                    page.click(f'[data-mrfx-i="{ent["i"]}"]', timeout=4000, no_wait_after=True)
                    clicks_done.append(text[:40])
                    n += 1
                    page.wait_for_timeout(1_800)  # let the click's XHRs land
                    try:
                        page.wait_for_load_state("networkidle", timeout=6_000)
                    except Exception:  # noqa: BLE001 — busy pages never go idle
                        pass
                except Exception:  # noqa: BLE001 — overlapped/destroyed element
                    continue
            return n

        try:
            with _RENDER_LOCK, sync_playwright() as pw:
                # launch OUTSIDE the browser-closing try (there is no browser
                # yet if it fails) but INSIDE the client-closing one — a
                # missing-browser error must not leak the httpx client
                browser = _launch(pw)
                try:
                    # service workers bypass route() interception entirely —
                    # block them or the browser fetches from the network
                    # itself, violating the everything-through-httpx invariant
                    context = browser.new_context(user_agent=BROWSER_UA,
                                                  service_workers="block")
                    context.route("**/*", fulfill)  # popups inherit the interception
                    page = context.new_page()
                    page.goto(url, wait_until="networkidle", timeout=RENDER_TIMEOUT_MS)
                    page.wait_for_timeout(SETTLE_MS)
                    snapshot_all(context)
                    if harvest_progress() == 0:
                        # nothing yet: clear consent overlays, then click the
                        # controls that look like they reveal the file list
                        click_candidates(page, _CONSENT_TEXT_RE, 2)
                        clicked = click_candidates(page, _CLICKABLE_TEXT_RE, MAX_CLICKS)
                        if clicked:
                            page.wait_for_timeout(SETTLE_MS)
                            snapshot_all(context)
                finally:
                    browser.close()
        finally:
            holder["client"].close()  # idempotent; covers every exit path

        for u, html in dom_snapshots:
            if len(links) >= max_files:
                break
            links += extract_links_from_text(html, u, max_files - len(links), seen=seen)
        n_dom = len(links)
        for resp_url, body in json_bodies:
            if len(links) >= max_files:
                break
            # response bodies resolve relative paths against the API host
            links += extract_links_from_text(body, resp_url, max_files - len(links), seen=seen)
        if links:
            log.info("%s — rendered in headless browser: %d link(s) "
                     "(%d from the page, %d from its API responses%s)",
                     url, len(links), n_dom, len(links) - n_dom,
                     f"; clicked: {', '.join(clicks_done)}" if clicks_done else "")
        else:
            log.info("%s — rendered in headless browser: no file links appeared%s",
                     url, f" (clicked: {', '.join(clicks_done)})" if clicks_done else "")
        return links
    except RenderBrowserMissing:
        raise  # actionable — the caller shows the one-line install fix
    except Exception as e:  # noqa: BLE001 — rendering is best-effort by contract
        log.warning("headless render of %s failed: %s", url, str(e)[:200])
        return []
