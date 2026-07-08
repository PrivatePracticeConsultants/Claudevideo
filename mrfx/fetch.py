"""URL-drop ingestion: download MRF URLs and auto-expand table-of-contents URLs.

Give the app a URL (or many) and it:
  1. streams the file to a staging folder (retry/backoff, atomic, progress),
  2. sniffs what it is (in-network rate file / provider-reference / TOC index),
  3. if it's a TOC, stream-parses it and enqueues every in-network file URL it
     lists (deduped, capped), which then flow through the same pipeline,
  4. otherwise hands the file to the normal chunked ingest.

A single background worker drains the queue sequentially, so aggregating a
whole payer (hundreds of files) never fills the disk: one file at a time, and
the raw download is deleted after a successful ingest (config-controlled),
keeping only the compact Parquet.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import ssl
import time
from pathlib import Path
from urllib.parse import unquote, urljoin, urlsplit

import httpx
import ijson

from .config import MrfxConfig
from .ingest import ingest_file
from .sniff import open_stream, preflight
from .store import Store

log = logging.getLogger(__name__)

RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}


def dedup_key(url: str) -> str:
    """Identity of a file independent of volatile signed-query params. Signed
    CDN URLs (mrf.bcbs.com, S3 presigned) differ only in Expires/Signature.
    Host is case-insensitive; the path is NOT (S3 keys are case-sensitive)."""
    parts = urlsplit(url)
    return f"{parts.scheme.lower()}://{parts.netloc.lower()}{parts.path}"


def filename_for(url: str) -> str:
    """A safe, collision-resistant local filename for a URL. Basenames repeat
    across payers ('..._in-network-rates_1_of_2.json.gz'), so prefix a short
    hash of the dedup key."""
    path = urlsplit(url).path
    base = unquote(path.rsplit("/", 1)[-1]) or "download.json"
    base = re.sub(r"[^A-Za-z0-9._-]", "_", base)[:120]
    if not re.search(r"\.(json|gz|zip)$", base, re.I):
        base += ".json"
    h = hashlib.sha1(dedup_key(url).encode()).hexdigest()[:8]
    return f"{h}_{base}"


# ---------------------------------------------------------------------------
# download
# ---------------------------------------------------------------------------


def ssl_verify():
    """TLS verification that honors the standard CA-bundle env vars
    (SSL_CERT_FILE / REQUESTS_CA_BUNDLE). Corporate networks and VPNs often
    intercept HTTPS with their own certificate authority; without this, every
    download fails with CERTIFICATE_VERIFY_FAILED even though the browser
    works. Verification is never disabled — we only add the extra CA."""
    cafile = os.environ.get("SSL_CERT_FILE") or os.environ.get("REQUESTS_CA_BUNDLE")
    if cafile and Path(cafile).exists():
        try:
            return ssl.create_default_context(cafile=cafile)
        except ssl.SSLError:
            pass
    return True


TLS_HELP = (
    " — your network appears to intercept secure connections (common on "
    "work laptops/VPNs). Ask IT for your organization's CA bundle file and "
    "set the SSL_CERT_FILE environment variable to its path, then retry."
)

# Some payer servers (health1.aetna.com is a real example) send an incomplete
# certificate chain. Browsers quietly repair this by downloading the missing
# intermediate from the CA (an "AIA" fetch); strict clients just fail. We do
# what the browser does — but only trust the repaired chain after verifying it
# end-to-end against the real root store for this exact hostname. Verification
# is never weakened; we only supply the certificate the server forgot to send.
_REPAIRED_CTX: dict[str, ssl.SSLContext] = {}


def _repair_incomplete_chain(url: str) -> ssl.SSLContext | None:
    try:
        import certifi
        from cryptography import x509
        from cryptography.hazmat.primitives import serialization
        from cryptography.x509.verification import PolicyBuilder, Store
    except ImportError:
        return None
    parts = urlsplit(url)
    host, port = parts.hostname, parts.port or 443
    if not host:
        return None
    if host in _REPAIRED_CTX:
        return _REPAIRED_CTX[host]
    try:
        # read only the certificate the server presents (no data is exchanged)
        leaf_pem = ssl.get_server_certificate((host, port), timeout=20)
        leaf = x509.load_pem_x509_certificate(leaf_pem.encode())
        inters: list = []
        cur = leaf
        for _ in range(3):  # follow at most a few AIA hops
            try:
                aia = cur.extensions.get_extension_for_class(x509.AuthorityInformationAccess)
            except x509.ExtensionNotFound:
                break
            ca_urls = [ad.access_location.value for ad in aia.value
                       if ad.access_method == x509.oid.AuthorityInformationAccessOID.CA_ISSUERS]
            if not ca_urls:
                break
            der = httpx.get(ca_urls[0], timeout=20, follow_redirects=True,
                            verify=ssl_verify()).content
            try:
                nxt = x509.load_der_x509_certificate(der)
            except ValueError:
                nxt = x509.load_pem_x509_certificate(der)
            inters.append(nxt)
            if nxt.issuer == nxt.subject:
                break
            cur = nxt
        if not inters:
            return None
        # PROVE the repaired chain is valid for this hostname against real
        # roots before trusting anything (expiry, signatures, name — the works)
        roots = x509.load_pem_x509_certificates(Path(certifi.where()).read_bytes())
        verifier = PolicyBuilder().store(Store(roots)).build_server_verifier(x509.DNSName(host))
        verifier.verify(leaf, inters)
        cafile = os.environ.get("SSL_CERT_FILE") or os.environ.get("REQUESTS_CA_BUNDLE") or certifi.where()
        ctx = ssl.create_default_context(cafile=cafile)
        ctx.load_verify_locations(cadata="\n".join(
            i.public_bytes(serialization.Encoding.PEM).decode() for i in inters
        ))
        _REPAIRED_CTX[host] = ctx
        log.info("%s: server sends an incomplete certificate chain; repaired it "
                 "(fetched + verified %d intermediate cert(s) like a browser does)", host, len(inters))
        return ctx
    except Exception as e:  # noqa: BLE001 — repair is best-effort; fall back to the friendly error
        log.debug("chain repair for %s failed: %s", host, e)
        return None


def _client(cfg: MrfxConfig, verify=None) -> httpx.Client:
    return httpx.Client(
        headers={"User-Agent": cfg.user_agent, "Accept-Encoding": "identity"},
        follow_redirects=True,
        timeout=httpx.Timeout(cfg.download_timeout_seconds, connect=30.0),
        verify=verify if verify is not None else ssl_verify(),
    )


class DownloadError(Exception):
    def __init__(self, msg: str, status: int | None = None, retryable: bool = False):
        super().__init__(msg)
        self.status = status
        self.retryable = retryable


def download(cfg: MrfxConfig, url: str, dest: Path, progress_cb=None,
             max_bytes: int | None = None) -> str:
    """Stream a URL to `dest` (atomic via .part). Returns the sha256 hex of
    the downloaded bytes (used to detect the same file arriving under a
    different domain). Retries transient failures; raises DownloadError on a
    terminal failure (e.g. expired signed URL) or when the payload exceeds
    max_bytes (the confirm_over_gb guard)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(dest.suffix + ".part")
    last_exc: Exception | None = None

    def too_big(n: int) -> DownloadError:
        return DownloadError(
            f"file is {n / 1e9:.1f} GB — larger than the confirm_over_gb "
            f"safety limit ({(max_bytes or 0) / 1e9:.1f} GB). If you meant to "
            "ingest it, raise confirm_over_gb in config/mrfx.yaml and retry.",
            retryable=False,
        )

    verify_override = None
    tried_chain_repair = False
    for attempt in range(cfg.download_retries + 1):
        try:
            with _client(cfg, verify=verify_override) as client, client.stream("GET", url) as resp:
                if resp.status_code in RETRYABLE_STATUS:
                    raise DownloadError(
                        f"HTTP {resp.status_code}", resp.status_code, retryable=True
                    )
                if resp.status_code == 403:
                    raise DownloadError(
                        "HTTP 403 — the link is forbidden or a signed URL has expired; "
                        "re-copy a fresh URL (for TOC-derived links, re-add the TOC).",
                        403, retryable=False,
                    )
                if resp.status_code == 404:
                    raise DownloadError("HTTP 404 — file not found at this URL", 404, retryable=False)
                if resp.status_code != 200:
                    raise DownloadError(f"HTTP {resp.status_code}", resp.status_code, retryable=False)

                total = int(resp.headers.get("Content-Length") or 0) or None
                if max_bytes and total and total > max_bytes:
                    raise too_big(total)
                done = 0
                last_report = 0.0
                sha = hashlib.sha256()
                with open(part, "wb") as f:
                    for chunk in resp.iter_bytes(chunk_size=1 << 20):
                        f.write(chunk)
                        sha.update(chunk)
                        done += len(chunk)
                        if max_bytes and done > max_bytes:
                            raise too_big(done)  # no Content-Length header case
                        if progress_cb and (done - last_report >= 8 << 20):
                            progress_cb(done, total)
                            last_report = done
                if progress_cb:
                    progress_cb(done, total)
            part.replace(dest)
            return sha.hexdigest()
        except DownloadError as e:
            last_exc = e
            if not e.retryable:
                part.unlink(missing_ok=True)
                raise
            log.warning("download %s: %s (attempt %d/%d)", url, e, attempt + 1, cfg.download_retries + 1)
        except (httpx.TransportError, httpx.TimeoutException) as e:
            last_exc = e
            # server sent an incomplete cert chain? repair it like a browser
            # and retry immediately (once, without burning an attempt)
            if "CERTIFICATE_VERIFY_FAILED" in str(e) and not tried_chain_repair:
                tried_chain_repair = True
                ctx = _repair_incomplete_chain(url)
                if ctx is not None:
                    verify_override = ctx
                    continue
            log.warning("download %s network error: %s (attempt %d/%d)", url, e, attempt + 1, cfg.download_retries + 1)
        # backoff before retry
        if attempt < cfg.download_retries:
            time.sleep(2.0 * (2**attempt))
    part.unlink(missing_ok=True)
    msg = f"download failed after {cfg.download_retries + 1} attempts: {last_exc}"
    if "CERTIFICATE_VERIFY_FAILED" in str(last_exc):
        msg += TLS_HELP
    raise DownloadError(msg)


# ---------------------------------------------------------------------------
# TOC expansion
# ---------------------------------------------------------------------------


def expand_toc(path: Path, max_files: int, base_url: str = "") -> tuple[list[str], bool]:
    """Stream-parse a table-of-contents file and return the in-network file
    URLs it lists (deduped, order-preserved). Returns (urls, truncated) where
    truncated is True if the TOC listed more than max_files.

    Relative locations are resolved against the TOC's own URL. Never
    json.load()s — TOCs can list tens of thousands of files.
    """
    seen: set[str] = set()
    urls: list[str] = []
    truncated = False

    def take(loc) -> bool:
        nonlocal truncated
        if not loc or not isinstance(loc, str):
            return False
        loc = loc.strip()
        if base_url and not loc.lower().startswith(("http://", "https://")):
            loc = urljoin(base_url, loc)  # some payers list relative paths
        if not loc.lower().startswith(("http://", "https://")):
            return False
        k = dedup_key(loc)
        if k in seen:
            return False
        seen.add(k)
        urls.append(loc)
        if len(urls) >= max_files:
            truncated = True
            return True  # stop
        return False

    with open_stream(path) as stream:
        # reporting_structure[].in_network_files[].location  (CMS TiC index)
        for loc in ijson.items(stream, "reporting_structure.item.in_network_files.item.location"):
            if take(loc):
                break
    if not urls:
        # some payers put in_network_files at the top level instead
        with open_stream(path) as stream:
            for loc in ijson.items(stream, "in_network_files.item.location"):
                if take(loc):
                    break
    return urls, truncated


# ---------------------------------------------------------------------------
# HTML pages (what a person pastes from their browser)
# ---------------------------------------------------------------------------

_HTML_SNIFF_RE = re.compile(rb"<\s*(!doctype\s+html|html|head|body)[\s>]", re.I)
# file links inside href/src attributes — relative ones included (directory
# listings like mrfdata.hmhs.com use href="2026-06-25_...json.gz" with no path)
_ATTR_LINK_RE = re.compile(
    r"""(?:href|src)\s*=\s*["'](?P<u>[^"']+?\.json(?:\.gz)?(?:\?[^"']*)?)["']""",
    re.I,
)
# absolute file URLs anywhere else in the page (inlined JS/config blobs)
_ABS_LINK_RE = re.compile(
    r"""(?P<u>https?://[^"'\s<>]+?\.json(?:\.gz)?(?:\?[^"'\s<>]*)?)["'<\s]""",
    re.I,
)


def looks_like_html(path: Path) -> bool:
    try:
        with open(path, "rb") as f:
            head = f.read(2048)
    except OSError:
        return False
    return bool(_HTML_SNIFF_RE.search(head))


def extract_links_from_page(path: Path, base_url: str, max_files: int) -> list[str]:
    """Best effort: pull .json / .json.gz links out of an HTML page (payer
    directory listings like mrfdata.hmhs.com are plain pages full of file
    links). Returns absolute, deduped URLs. Empty for JS-only portals."""
    try:
        with open(path, "rb") as f:
            text = f.read(20 << 20).decode(errors="replace")  # pages are small; cap at 20 MB
    except OSError:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for pattern in (_ATTR_LINK_RE, _ABS_LINK_RE):
        for m in pattern.finditer(text):
            u = urljoin(base_url, m.group("u"))
            if not u.lower().startswith(("http://", "https://")):
                continue
            k = dedup_key(u)
            if k in seen:
                continue
            seen.add(k)
            out.append(u)
            if len(out) >= max_files:
                return out
    return out


PAGE_HELP = (
    "This link is a web page, not a data file, and no file links could be "
    "found on it (many payer portals load their file lists with JavaScript). "
    "Open the page in your browser, right-click the actual file links "
    "(they end in .json or .json.gz — often labeled 'in-network' or "
    "'Table of Contents') and choose 'Copy link address', then paste those "
    "here instead."
)


# ---------------------------------------------------------------------------
# queue processing
# ---------------------------------------------------------------------------


def process_url_record(cfg: MrfxConfig, store: Store, rec: dict, progress_bar=None) -> None:
    """Download, classify, and route one queued URL. Never raises — failures
    are recorded on the queue row so the worker keeps going."""
    url_id = rec["id"]
    url = rec["url"]
    dest = cfg.downloads_dir / filename_for(url)
    try:
        store.update_url(url_id, status="downloading")
        content_sha = download(cfg, url, dest,
                               progress_cb=lambda d, t: store.url_progress(url_id, d, t),
                               max_bytes=int(cfg.confirm_over_gb * 1e9))
    except DownloadError as e:
        log.warning("url %s download failed: %s", url, e)
        store.update_url(url_id, status="failed", error=str(e))
        return
    store.update_url(url_id, content_sha=content_sha)

    # Blue plans host copies of each other's national files — the same bytes
    # arrive under many domains. Skip byte-identical repeats instead of
    # re-parsing millions of duplicate rows into the store.
    twin = store.find_url_with_same_content(content_sha, exclude_id=url_id)
    if twin:
        store.update_url(url_id, status="skipped", kind="duplicate",
                         error="identical to a file already ingested (payers host copies "
                               f"of each other's files) — skipped as duplicate of {twin.split('?')[0]}")
        dest.unlink(missing_ok=True)
        log.info("%s — byte-identical to %s; skipped", url, twin.split("?")[0])
        return

    try:
        pf = preflight(dest, cfg, store)
    except Exception as e:  # noqa: BLE001 — classification must never crash the worker
        store.update_url(url_id, status="failed", error=f"could not read file: {e}")
        dest.unlink(missing_ok=True)
        return

    if pf.file_type == "toc":
        store.update_url(url_id, status="expanding", kind="toc")
        try:
            child_urls, truncated = expand_toc(dest, cfg.max_toc_files, base_url=url)
        except Exception as e:  # noqa: BLE001
            store.update_url(url_id, status="failed", kind="toc", error=f"TOC parse failed: {e}")
            dest.unlink(missing_ok=True)
            return
        added = 0
        for cu in child_urls:
            if store.enqueue_url(cu, dedup_key(cu), parent_id=url_id) is not None:
                added += 1
        msg = f"index expanded: {len(child_urls)} files listed, {added} newly queued"
        if truncated:
            msg += f" (capped at max_toc_files={cfg.max_toc_files})"
        if not child_urls:
            store.update_url(url_id, status="failed", kind="toc",
                             error="this index/TOC lists no in-network file URLs")
        else:
            log.info("%s — %s", url, msg)
            # child_count on a done TOC row is how the dashboard says
            # "found N files inside, working through them"
            store.update_url(url_id, status="done", kind="toc", child_count=added, error=None)
        dest.unlink(missing_ok=True)  # the TOC itself carries no rates
        return

    if pf.file_type == "allowed_amounts":
        # common on listing pages next to the rate files — skip, don't scare
        store.update_url(url_id, status="skipped", kind="allowed_amounts",
                         error="out-of-network allowed-amounts file (no negotiated rates) — skipped")
        dest.unlink(missing_ok=True)
        return

    if pf.file_type == "unknown":
        # A person pasting from their browser often pastes the PAGE, not the
        # file. If it's HTML, try to lift the file links off it; otherwise
        # explain what to do in plain language.
        if looks_like_html(dest):
            links = extract_links_from_page(dest, url, cfg.max_toc_files)
            dest.unlink(missing_ok=True)
            if links:
                added = 0
                for cu in links:
                    if store.enqueue_url(cu, dedup_key(cu), parent_id=url_id) is not None:
                        added += 1
                log.info("%s — web page: found %d file links, %d newly queued", url, len(links), added)
                store.update_url(url_id, status="done", kind="page", child_count=added, error=None)
            else:
                store.update_url(url_id, status="failed", kind="page", error=PAGE_HELP)
            return
        store.update_url(url_id, status="failed", kind="unknown",
                         error="; ".join(pf.messages) or "unrecognized file (not MRF JSON)")
        dest.unlink(missing_ok=True)
        return

    # in-network rate file or standalone provider-reference file: run the
    # normal (chunked) ingest in place. Deliberately NOT moved into the inbox —
    # the folder watcher would race the queue worker on the same file.
    store.update_url(url_id, status="ingesting", kind=pf.file_type, filename=dest.name)
    try:
        result = ingest_file(cfg, store, dest, pf=pf, progress_bar=progress_bar)
    except Exception as e:  # noqa: BLE001 — belt and suspenders; ingest_file already isolates
        store.update_url(url_id, status="failed", kind=pf.file_type, error=f"ingest crashed: {e}")
        return

    status = result.get("status")
    if status == "done":
        n = result.get("rows", result.get("refs", 0))
        store.update_url(url_id, status="done", kind=pf.file_type,
                         rows_emitted=n or 0, error=None)
        if cfg.delete_raw_after_ingest:
            _cleanup_raw(cfg, dest)
    elif status == "pending_confirmation":
        store.update_url(url_id, status="failed", kind=pf.file_type,
                         error="file exceeds confirm_over_gb; raise the limit in config/mrfx.yaml and retry")
    else:
        store.update_url(url_id, status="failed", kind=pf.file_type,
                         error=result.get("error") or status)


def _cleanup_raw(cfg: MrfxConfig, path: Path) -> None:
    """After a successful URL-driven ingest, remove the raw download so
    aggregating many payers doesn't fill the disk (Parquet is kept; the file
    can always be re-downloaded from its URL)."""
    try:
        path.unlink(missing_ok=True)
        (cfg.processed_dir / path.name).unlink(missing_ok=True)
        (cfg.failed_dir / path.name).unlink(missing_ok=True)
    except OSError as e:
        log.warning("could not delete raw %s: %s", path, e)


def add_urls(store: Store, urls: list[str]) -> dict:
    """Enqueue a batch of user-supplied URLs. Returns counts."""
    added = skipped = invalid = 0
    for raw in urls:
        u = raw.strip()
        if not u:
            continue
        if not u.lower().startswith(("http://", "https://")):
            invalid += 1
            continue
        if store.enqueue_url(u, dedup_key(u)) is not None:
            added += 1
        else:
            skipped += 1
    return {"added": added, "skipped": skipped, "invalid": invalid}


def run_queue(cfg: MrfxConfig, store: Store, stop=None, progress_bar=None, drain: bool = False) -> int:
    """Process queued URLs one at a time until the queue is empty (drain=True,
    for the CLI) or `stop` is set (the serve worker). Returns files processed."""
    recovered = store.recover_stuck_urls()
    if recovered:
        log.info("resumed %d URL(s) left mid-flight by a previous run", recovered)
    processed = 0
    while stop is None or not stop.is_set():
        rec = store.next_queued_url()
        if rec is None:
            if drain:
                break
            if stop is not None:
                stop.wait(3.0)
            continue
        try:
            process_url_record(cfg, store, rec, progress_bar=progress_bar)
        except Exception:  # noqa: BLE001 — a bad URL must never kill the worker
            log.exception("url worker: unexpected error on %s", rec.get("url"))
            store.update_url(rec["id"], status="failed", error="unexpected worker error")
        processed += 1
    return processed
