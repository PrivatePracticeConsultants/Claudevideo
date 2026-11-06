"""`mrfx sources --check` — re-audit the tested-sources catalog in one command.

Payer pages move constantly (18 of 72 entries were dead after four months),
so the catalog needs a monthly look. For every source (optionally only some
states'), this fetches the FIRST couple of megabytes with the app's own
User-Agent and TLS settings and says, in plain words:

  OK             the index (or a page with file links) answers and still looks like one
  NEEDS-BROWSER  the page answers but lists its files only when run in a browser
  PORTAL         a portal-only catalog entry (open it in a browser) that answers
  CHANGED        it answers, but no longer looks like an index / has no file links
  DEAD           HTTP error, DNS failure, timeout

It reads only — it never queues anything and never touches the store — and
every request goes out as MRF Explorer (cfg.user_agent): no browser identity,
no page rendering, no retry with a disguised client. A bot wall is reported.
"""

from __future__ import annotations

import concurrent.futures as cf
import gzip
import io
import re
import zlib

HEAD_BYTES = 2 << 20
_INDEX_KEYS = re.compile(rb'"(reporting_structure|in_network_files|in_network|blobs|'
                         rb'reporting_plans|provider_references)"')


def _sniff(cfg, url: str, head: bytes, ctype: str) -> tuple[str, str]:
    """(kind, detail) for the first bytes of a response — judged the way the
    app itself would load it (fetch.process_url_record): an index's keys, else
    any file links in the text (link containers, XML listings), and for a
    page with none: a Gatsby hub and the React portals' blobs API."""
    from .fetch import crawl_gatsby_hub, extract_links_from_text, probe_blobs_api
    body = head
    if head[:2] == b"\x1f\x8b":
        try:
            body = zlib.decompressobj(16 + zlib.MAX_WBITS).decompress(head, HEAD_BYTES)
        except zlib.error:
            body = b""
    elif head[:4] == b"PK\x03\x04":
        return "index", "a zip archive (read in full when queued)"
    stripped = body.lstrip(b"\xef\xbb\xbf \t\r\n")
    if stripped[:1] in (b"{", b"["):
        m = _INDEX_KEYS.search(stripped[:HEAD_BYTES])
        if m:
            return "index", f"JSON with {m.group(1).decode()}"
    text = body.decode("utf-8", "replace")
    links = extract_links_from_text(text, url, 10_000)
    if links:
        return "index", f"{len(links)} file link(s) in the response"
    is_html = b"<html" in body[:4096].lower() or "html" in (ctype or "")
    if is_html:
        for how, fn in (("a Gatsby file hub", lambda: crawl_gatsby_hub(cfg, url, 50)),
                        ("the portal's file API", lambda: probe_blobs_api(cfg, url))):
            try:
                found = fn()
            except Exception:  # noqa: BLE001 — a probe that fails is a "no"
                found = []
            if found:
                return "index", f"{len(found)}+ file link(s) via {how}"
        if re.search(rb"captcha|cf-chl|access denied|are you a robot", body[:200_000], re.I):
            return "changed", "a bot-check / access-denied page (open it in a browser)"
        # NOT rendered here: the app's headless renderer presents a desktop-
        # browser identity, and this check only ever asks as MRF Explorer
        return "browser", ("the page answers but shows its file links only when run in a "
                           "browser — open it in a browser to confirm")
    if stripped[:1] in (b"{", b"["):
        return "changed", "JSON, but no index keys or file links in its first 2 MB"
    return "changed", f"unrecognized content ({ctype or 'no content type'})"


def check_one(cfg, source: dict, timeout: float = 45.0) -> dict:
    from .fetch import _repair_incomplete_chain, ssl_verify
    out = {"name": source["name"], "url": source["url"], "queueable": source.get("queueable"),
           "states": source.get("states", [])}
    verify = ssl_verify()
    for attempt in (0, 1):
        try:
            _fetch_and_judge(cfg, source, out, verify, timeout)
            return out
        except Exception as e:  # noqa: BLE001 — one dead host never stops the audit
            if attempt == 0 and "CERTIFICATE_VERIFY_FAILED" in str(e):
                # the app's downloader repairs a server's incomplete chain
                # (fetches the missing intermediate like a browser) — so must
                # the check, or it calls a working source dead
                ctx = _repair_incomplete_chain(source["url"])
                if ctx is not None:
                    verify = ctx
                    continue
            out.update(result="DEAD", detail=f"{type(e).__name__}: {str(e)[:160]}")
            return out
    return out


def _fetch_and_judge(cfg, source: dict, out: dict, verify, timeout: float) -> None:
    import httpx
    with httpx.Client(headers={"User-Agent": cfg.user_agent}, verify=verify,
                      follow_redirects=True, timeout=timeout) as client, \
            client.stream("GET", source["url"]) as r:
        out["http"] = r.status_code
        if r.status_code >= 400:
            out.update(result="DEAD", detail=f"HTTP {r.status_code}")
            return
        buf = io.BytesIO()
        for chunk in r.iter_raw():
            buf.write(chunk)
            if buf.tell() >= HEAD_BYTES:
                break
        head = buf.getvalue()[:HEAD_BYTES]
        if r.headers.get("content-encoding", "").lower() == "gzip" and head[:2] == b"\x1f\x8b":
            try:
                head = gzip.decompress(head)
            except (OSError, EOFError):
                pass
        kind, detail = _sniff(cfg, str(r.url), head, r.headers.get("content-type", ""))
        if kind == "index":
            out.update(result="OK", detail=detail)
        elif not source.get("queueable"):
            # the catalog already says "portal — open in a browser"
            out.update(result="PORTAL", detail=f"answers (HTTP {r.status_code}); " + detail)
        elif kind == "browser":
            out.update(result="NEEDS-BROWSER", detail=detail)
        else:
            out.update(result="CHANGED", detail=detail)


def check_sources(cfg, sources: list[dict], workers: int = 8, progress=None) -> list[dict]:
    results = []
    with cf.ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(check_one, cfg, s): s for s in sources}
        for f in cf.as_completed(futs):
            results.append(f.result())
            if progress is not None:
                progress(results[-1])
    order = {s["url"]: i for i, s in enumerate(sources)}
    return sorted(results, key=lambda r: order.get(r["url"], 0))
