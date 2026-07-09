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
import json
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


# Query params that carry a signature/expiry rather than identity. Dropping
# only these (not the whole query) keeps signed CDN re-pastes deduped while
# still distinguishing download endpoints like ?file=a.json vs ?file=b.json.
_VOLATILE_QUERY = re.compile(
    r"^(expires|signature|key-pair-id|awsaccesskeyid|x-amz-.*|token|sig|se|sp|sv|sr|st|ss|srt|spr|sip|skoid|sktid|skt|ske|sks|skv|rscd|rsct)$",
    re.I,
)


def dedup_key(url: str) -> str:
    """Identity of a file independent of volatile signed-query params. Signed
    CDN URLs (mrf.bcbs.com, S3 presigned, Azure SAS) differ only in their
    signature params; identity params (e.g. ?file=...) are kept, sorted.
    Host is case-insensitive; the path is NOT (S3 keys are case-sensitive)."""
    parts = urlsplit(url)
    kept = sorted(
        f"{k}={v}"
        for k, _, v in (p.partition("=") for p in parts.query.split("&") if p)
        if k and not _VOLATILE_QUERY.match(k)
    )
    query = ("?" + "&".join(kept)) if kept else ""
    return f"{parts.scheme.lower()}://{parts.netloc.lower()}{parts.path}{query}"


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
             max_bytes: int | None = None) -> tuple[str, str]:
    """Stream a URL to `dest` (atomic via .part). Returns (sha256_hex,
    final_url) — the hash detects the same file arriving under a different
    domain, and final_url (post-redirect) is the correct base for resolving
    relative links found inside the payload. Retries transient failures;
    raises DownloadError on a terminal failure (e.g. expired signed URL) or
    when the payload exceeds max_bytes (the confirm_over_gb guard)."""
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
                last_report = 0
                last_report_t = 0.0
                sha = hashlib.sha256()
                with open(part, "wb") as f:
                    for chunk in resp.iter_bytes(chunk_size=1 << 20):
                        f.write(chunk)
                        sha.update(chunk)
                        done += len(chunk)
                        if max_bytes and done > max_bytes:
                            raise too_big(done)  # no Content-Length header case
                        # dashboard polls every ~4s; don't hammer the store
                        # with a write-locked UPDATE for every 8 MB of a fast
                        # link — report on bytes AND wall-clock
                        now = time.monotonic()
                        if progress_cb and done - last_report >= 8 << 20 and now - last_report_t >= 1.5:
                            progress_cb(done, total)
                            last_report = done
                            last_report_t = now
                if progress_cb:
                    progress_cb(done, total)
                final_url = str(resp.url)
            part.replace(dest)
            return sha.hexdigest(), final_url
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


# drug-pricing rate files, named unambiguously in UHC/Optum listings
# ("...PPO-NDC_in-network-rates...", "..._prescription-drugs.json.gz").
# Bounded-token match so an employer literally named "NDC Corp" isn't caught.
_DRUG_FILE_RE = re.compile(r"(^|[-_])ndc([-_.]|$)|prescription-drug", re.I)


def expand_blobs_listing(path: Path, max_files: int, base_url: str = "") -> tuple[list[str], bool]:
    """Expand a transparency-portal blobs listing ({"blobs": [{"name",
    "downloadUrl"}, ...]} — UHC / Optum style). Rate files first: the UHC
    listing carries ~7k in-network files, ~67k per-employer index files that
    mostly re-list the same shared network files, and ~12k allowed-amounts
    files (no rates — not queued at all). In-network files named as DRUG
    pricing (NDC / prescription-drugs) are skipped too — no medical CPT/HCPCS
    rates in them. Returns (urls, truncated)."""
    rate_files: list[str] = []
    indexes: list[str] = []
    seen: set[str] = set()
    truncated = False

    with open_stream(path) as stream:
        for item in ijson.items(stream, "blobs.item"):
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "")
            loc = str(item.get("downloadUrl") or "").strip()
            if not loc:
                continue
            if base_url and not loc.lower().startswith(("http://", "https://")):
                loc = urljoin(base_url, loc)
            if not loc.lower().startswith(("http://", "https://")):
                continue
            low = name.lower()
            if "allowed" in low:
                continue  # out-of-network billed averages — no negotiated rates
            is_rate_file = "in-network" in low
            if is_rate_file and _DRUG_FILE_RE.search(low):
                # NDC / prescription-drug pricing — no medical CPT/HCPCS rates;
                # ingesting would only produce honest zero-row files
                continue
            k = dedup_key(loc)
            if k in seen:
                continue
            seen.add(k)
            bucket = rate_files if is_rate_file else indexes
            if len(bucket) < max_files:
                bucket.append(loc)
            elif bucket is rate_files:
                truncated = True
    out = (rate_files + indexes)[:max_files]
    if len(rate_files) + len(indexes) > len(out):
        truncated = True
    return out, truncated


# Well-known transparency-portal listing APIs, probed (cheaply) when a pasted
# page is HTML with no file links: the UHC/Optum React portals serve their
# entire file list from these endpoints.
_BLOBS_API_PATHS = ("/api/v1/uhc/blobs/", "/api/v1/oh/blobs/", "/api/v1/orx/blobs/")


def probe_blobs_api(cfg: MrfxConfig, page_url: str) -> list[str]:
    """Return the portal's blobs-API URL(s) if the page's origin serves one.
    Reads only the first bytes of each candidate — the real listing (which can
    run to tens of MB) is downloaded later through the normal queue."""
    parts = urlsplit(page_url)
    origin = f"{parts.scheme}://{parts.netloc}"
    found: list[str] = []
    try:
        with _client(cfg) as client:
            for p in _BLOBS_API_PATHS:
                try:
                    with client.stream("GET", origin + p) as resp:
                        if resp.status_code != 200:
                            continue
                        head = b""
                        for chunk in resp.iter_bytes(chunk_size=2048):
                            head += chunk
                            break
                    if re.match(rb'\s*\{\s*"blobs"\s*:', head):
                        found.append(origin + p)
                except httpx.HTTPError:
                    continue
    except Exception as e:  # noqa: BLE001 — probe is best-effort
        log.debug("blobs-api probe %s: %s", origin, e)
    return found


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


# web-app plumbing that ends in .json but is never payer data — a Gatsby
# build's own preload/manifest links would otherwise be queued as "files"
_FRAMEWORK_ASSET_RE = re.compile(
    r"(/page-data/|/manifest\.json|\.webmanifest|/favicon|/asset-manifest|/app-data\.json)", re.I
)


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
            if _FRAMEWORK_ASSET_RE.search(u):
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


def crawl_gatsby_hub(cfg: MrfxConfig, page_url: str, max_files: int) -> list[str]:
    """Some payer MRF hubs (the Sapphire/HealthSparq platform — Blue KC is
    one) are Gatsby apps: the page is empty HTML, but the file list ships as
    static JSON the site itself fetches. Do what the browser would:
    /page-data/index/page-data.json names static-query blobs; each blob at
    /page-data/sq/d/<hash>.json carries TOC entries with a `url`. Returns []
    if the site isn't built this way (harmless one-request probe)."""
    parts = urlsplit(page_url)
    origin = f"{parts.scheme}://{parts.netloc}"
    seen: set[str] = set()
    out: list[str] = []

    def walk(node):
        """Collect node['url'] JSON links, skipping entries the payer marked
        suppressed (files pulled from publication — honesty over volume)."""
        if len(out) >= max_files:
            return
        if isinstance(node, dict):
            u = node.get("url")
            if isinstance(u, str) and re.search(r"\.json(\.gz)?(\?|$)", u, re.I) \
                    and not node.get("is_suppressed"):
                full = urljoin(origin + "/", u.strip())
                k = dedup_key(full)
                if k not in seen and full.lower().startswith(("http://", "https://")):
                    seen.add(k)
                    out.append(full)
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    try:
        with _client(cfg) as client:
            r = client.get(f"{origin}/page-data/index/page-data.json")
            if r.status_code != 200:
                return []
            hashes = (r.json() or {}).get("staticQueryHashes") or []
            for h in [str(x) for x in hashes][:10]:  # hubs ship a handful
                sq = client.get(f"{origin}/page-data/sq/d/{h}.json")
                if sq.status_code == 200:
                    walk(sq.json())
                if len(out) >= max_files:
                    break
    except (httpx.HTTPError, ValueError) as e:  # network or non-JSON — not a hub
        log.debug("gatsby probe %s: %s", origin, e)
        return []
    return out[:max_files]


# ---------------------------------------------------------------------------
# queue processing
# ---------------------------------------------------------------------------


_META_SUFFIX = ".fetchmeta"  # sidecar handing a prefetched download to the processor


def _meta_path(dest: Path) -> Path:
    return Path(str(dest) + _META_SUFFIX)


def fetch_url_record(cfg: MrfxConfig, store: Store, rec: dict) -> bool:
    """Download stage only (used by the prefetch thread): stream the file to
    disk, record its hash, and mark the row 'fetched' for the processor.
    Returns False on failure (row marked failed, worker keeps going)."""
    url_id, url = rec["id"], rec["url"]
    dest = cfg.downloads_dir / filename_for(url)
    try:
        content_sha, final_url = download(
            cfg, url, dest,
            progress_cb=lambda d, t: store.url_progress(url_id, d, t),
            max_bytes=int(cfg.confirm_over_gb * 1e9))
    except DownloadError as e:
        log.warning("url %s download failed: %s", url, e)
        store.update_url(url_id, status="failed", error=str(e))
        return False
    _meta_path(dest).write_text(json.dumps({"sha": content_sha, "final_url": final_url}))
    store.update_url(url_id, content_sha=content_sha, status="fetched")
    return True


def _enqueue_children(store: Store, urls: list[str], parent_id: int) -> int:
    """Queue discovered child URLs under their parent row; returns how many
    were newly queued (dupes of rows already queued/done return None)."""
    return sum(
        1 for u in urls
        if store.enqueue_url(u, dedup_key(u), parent_id=parent_id) is not None
    )


def process_url_record(cfg: MrfxConfig, store: Store, rec: dict, progress_bar=None,
                       rebuild_rollups: bool = True) -> bool:
    """Download, classify, and route one queued URL. Never raises — failures
    are recorded on the queue row so the worker keeps going. Returns True if
    the URL ingested rate/reference data (so callers batching deferred rollup
    rebuilds know work landed)."""
    url_id = rec["id"]
    url = rec["url"]
    dest = cfg.downloads_dir / filename_for(url)
    meta_p = _meta_path(dest)
    if dest.exists() and meta_p.exists():
        # prefetched by the downloader thread — pick up where it left off
        meta = json.loads(meta_p.read_text())
        content_sha, final_url = meta["sha"], meta["final_url"]
        meta_p.unlink(missing_ok=True)
    else:
        try:
            # (row already marked 'downloading' by next_queued_url when claimed)
            content_sha, final_url = download(
                cfg, url, dest,
                progress_cb=lambda d, t: store.url_progress(url_id, d, t),
                max_bytes=int(cfg.confirm_over_gb * 1e9))
        except DownloadError as e:
            log.warning("url %s download failed: %s", url, e)
            store.update_url(url_id, status="failed", error=str(e))
            return False
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
        return False

    try:
        pf = preflight(dest, cfg, store)
    except Exception as e:  # noqa: BLE001 — classification must never crash the worker
        store.update_url(url_id, status="failed", error=f"could not read file: {e}")
        dest.unlink(missing_ok=True)
        return False

    if pf.file_type in ("toc", "blob_listing"):
        store.update_url(url_id, status="expanding", kind="toc")
        expander = expand_toc if pf.file_type == "toc" else expand_blobs_listing
        try:
            # relative locations resolve against where the index actually came
            # from (post-redirect), not the possibly-redirected pasted URL
            child_urls, truncated = expander(dest, cfg.max_toc_files, base_url=final_url)
        except Exception as e:  # noqa: BLE001
            store.update_url(url_id, status="failed", kind="toc", error=f"index parse failed: {e}")
            dest.unlink(missing_ok=True)
            return False
        added = _enqueue_children(store, child_urls, url_id)
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
        dest.unlink(missing_ok=True)  # the index itself carries no rates
        return False

    if pf.file_type == "allowed_amounts":
        # common on listing pages next to the rate files — skip, don't scare
        store.update_url(url_id, status="skipped", kind="allowed_amounts",
                         error="out-of-network allowed-amounts file (no negotiated rates) — skipped")
        dest.unlink(missing_ok=True)
        return False

    if pf.file_type == "unknown":
        # A person pasting from their browser often pastes the PAGE, not the
        # file. If it's HTML, try to lift the file links off it; otherwise
        # explain what to do in plain language.
        if looks_like_html(dest):
            links = extract_links_from_page(dest, final_url, cfg.max_toc_files)
            dest.unlink(missing_ok=True)
            # a Gatsby-built MRF hub (Sapphire etc.) may render some links in
            # the HTML and ship the rest as static JSON next to the page —
            # probe it either way and merge (one cheap request for non-hubs)
            seen_keys = {dedup_key(u) for u in links}
            for hub_url in crawl_gatsby_hub(cfg, final_url, cfg.max_toc_files):
                if dedup_key(hub_url) not in seen_keys and len(links) < cfg.max_toc_files:
                    seen_keys.add(dedup_key(hub_url))
                    links.append(hub_url)
            if not links:
                # React portals (UHC/Optum) serve their whole file list from a
                # well-known API next to the page — probe before giving up
                links = probe_blobs_api(cfg, final_url)
            if links:
                added = _enqueue_children(store, links, url_id)
                log.info("%s — web page: found %d file links, %d newly queued", url, len(links), added)
                store.update_url(url_id, status="done", kind="page", child_count=added, error=None)
            else:
                store.update_url(url_id, status="failed", kind="page", error=PAGE_HELP)
            return False
        store.update_url(url_id, status="failed", kind="unknown",
                         error="; ".join(pf.messages) or "unrecognized file (not MRF JSON)")
        dest.unlink(missing_ok=True)
        return False

    # in-network rate file or standalone provider-reference file: run the
    # normal (chunked) ingest in place. Deliberately NOT moved into the inbox —
    # the folder watcher would race the queue worker on the same file.
    store.update_url(url_id, status="ingesting", kind=pf.file_type, filename=dest.name)
    try:
        result = ingest_file(cfg, store, dest, pf=pf, progress_bar=progress_bar,
                              rebuild_rollups=rebuild_rollups)
    except Exception as e:  # noqa: BLE001 — belt and suspenders; ingest_file already isolates
        store.update_url(url_id, status="failed", kind=pf.file_type, error=f"ingest crashed: {e}")
        return False

    status = result.get("status")
    if status == "done":
        n = result.get("rows", result.get("refs", 0))
        store.update_url(url_id, status="done", kind=pf.file_type,
                         rows_emitted=n or 0, error=None)
        if cfg.delete_raw_after_ingest:
            _cleanup_raw(cfg, dest)
        return True
    elif status == "pending_confirmation":
        store.update_url(url_id, status="failed", kind=pf.file_type,
                         error="file exceeds confirm_over_gb; raise the limit in config/mrfx.yaml and retry")
    else:
        store.update_url(url_id, status="failed", kind=pf.file_type,
                         error=result.get("error") or status)
    return False


def _cleanup_raw(cfg: MrfxConfig, path: Path) -> None:
    """After a successful URL-driven ingest, remove the raw download so
    aggregating many payers doesn't fill the disk (Parquet is kept; the file
    can always be re-downloaded from its URL)."""
    try:
        path.unlink(missing_ok=True)
        _meta_path(path).unlink(missing_ok=True)
        (cfg.processed_dir / path.name).unlink(missing_ok=True)
        (cfg.failed_dir / path.name).unlink(missing_ok=True)
    except OSError as e:
        log.warning("could not delete raw %s: %s", path, e)


def add_urls(store: Store, urls: list[str]) -> dict:
    """Enqueue a batch of user-supplied URLs. Returns counts. {FIRST_OF_MONTH}
    placeholders (copied from config/known_sources.yaml) resolve here too, so
    a pasted catalog line works the same as `mrfx add --known`."""
    from .known_sources import expand_placeholders

    added = skipped = invalid = 0
    for raw in urls:
        u = expand_placeholders(raw.strip())
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


# The dedup/by-TIN rollups are a FULL rebuild over all rows (minutes once the
# store holds tens of millions). When grinding a big payer book, rebuilding
# after every file would be quadratic wall-clock — batch it instead: rebuild
# after this many ingested files, and once more when the queue goes idle.
ROLLUP_BATCH_FILES = 10

# how many files the downloader may fetch ahead of the parser (disk-bounded)
PREFETCH_AHEAD = 1


def run_queue(cfg: MrfxConfig, store: Store, stop=None, progress_bar=None, drain: bool = False) -> int:
    """Process queued URLs one at a time until the queue is empty (drain=True,
    for the CLI) or `stop` is set (the serve worker). Returns files processed.

    Rollup rebuilds are batched (every ROLLUP_BATCH_FILES ingests + at idle):
    the raw rates land immediately; dashboards catch up in batches instead of
    stalling the queue for minutes after every single file."""
    recovered = store.recover_stuck_urls()
    if recovered:
        log.info("resumed %d URL(s) left mid-flight by a previous run", recovered)
    processed = 0
    ingests_pending_rollup = 0
    import threading
    state = threading.Lock()

    def rebuild_now():
        nonlocal ingests_pending_rollup
        with state:
            n = ingests_pending_rollup
        if not n:
            return
        log.info("updating analytics rollups (%d newly ingested file(s))...", n)
        try:
            store.rebuild_rollups()
        except Exception:  # noqa: BLE001 — rollups retry on the next batch
            log.exception("rollup rebuild failed; will retry after the next file")
            return
        with state:
            ingests_pending_rollup -= n

    # Pipeline: a downloader thread prefetches the NEXT file while the main
    # loop parses the current one — network and CPU overlap instead of taking
    # turns. At most PREFETCH_AHEAD files sit fetched-but-unprocessed on disk,
    # so disk stays bounded exactly as before (plus one file).
    dl_stop = threading.Event()

    # keep every parser fed: allow one fetched file per worker plus one spare
    prefetch_ahead = max(PREFETCH_AHEAD,
                         int(getattr(cfg, "parallel_ingests", 1) or 1) + 1)

    def downloader():
        while not dl_stop.is_set() and (stop is None or not stop.is_set()):
            try:
                counts = store.url_queue_counts()
                if counts.get("fetched", 0) >= prefetch_ahead:
                    dl_stop.wait(0.5)
                    continue
                rec = store.next_queued_url()
                if rec is None:
                    dl_stop.wait(0.5)
                    continue
                try:
                    fetch_url_record(cfg, store, rec)
                except Exception:  # noqa: BLE001 — downloader must never die
                    log.exception("prefetch: unexpected error on %s", rec.get("url"))
                    store.update_url(rec["id"], status="failed", error="unexpected download error")
            except Exception:  # noqa: BLE001 — even store hiccups must not kill the loop
                log.exception("prefetch: transient store error; retrying shortly")
                dl_stop.wait(2.0)

    dl_thread = threading.Thread(target=downloader, daemon=True, name="mrfx-prefetch")
    dl_thread.start()

    # Processor side: parallel_ingests threads each claim a fetched file and
    # run it through process_url_record. With parallel_ingests > 1 a process
    # pool does the CPU-heavy parsing (ingest_file dispatches to it), so the
    # threads mostly wait — the GIL never serializes the parsing itself. All
    # DuckDB writes happen in THIS process; workers only compute.
    parallel = max(1, min(int(getattr(cfg, "parallel_ingests", 1) or 1),
                          max(1, (os.cpu_count() or 2) - 1)))
    pool = None
    if parallel > 1:
        from . import ingest as _ingest_mod

        pool = _ingest_mod.ParsePoolManager(parallel)
        _ingest_mod.set_parse_pool(pool)
        log.info("parallel ingest: %d parser worker process(es)", parallel)

    active = 0

    def processor():
        nonlocal processed, ingests_pending_rollup, active
        while not dl_stop.is_set() and (stop is None or not stop.is_set()):
            with state:
                active += 1
            try:
                rec = store.next_fetched_url()
            except Exception:  # noqa: BLE001 — transient store error: back off, never die
                log.exception("url worker: transient store error while claiming; retrying")
                with state:
                    active -= 1
                dl_stop.wait(2.0)
                continue
            if rec is None:
                with state:
                    active -= 1
                dl_stop.wait(0.4)
                continue
            try:
                ok = False
                try:
                    ok = process_url_record(
                        cfg, store, rec,
                        progress_bar=progress_bar if parallel == 1 else None,
                        rebuild_rollups=False)
                except Exception:  # noqa: BLE001 — a bad URL must never kill the worker
                    log.exception("url worker: unexpected error on %s", rec.get("url"))
                    store.update_url(rec["id"], status="failed", error="unexpected worker error")
                with state:
                    processed += 1
                    if ok:
                        ingests_pending_rollup += 1
            finally:
                with state:
                    active -= 1

    proc_threads = [threading.Thread(target=processor, daemon=True, name=f"mrfx-proc-{i}")
                    for i in range(parallel)]
    for t in proc_threads:
        t.start()

    try:
        while stop is None or not stop.is_set():
            try:
                counts = store.url_queue_counts()
            except Exception:  # noqa: BLE001 — coordinator survives store hiccups
                log.exception("queue coordinator: transient store error; retrying")
                (stop.wait if stop is not None else time.sleep)(2.0)
                continue
            with state:
                busy = active
                pend = ingests_pending_rollup
            in_flight = (counts.get("queued", 0) + counts.get("downloading", 0)
                         + counts.get("fetched", 0) + busy)
            if pend >= ROLLUP_BATCH_FILES or (in_flight == 0 and pend):
                rebuild_now()
                continue
            if in_flight == 0 and drain:
                break
            if stop is not None:
                stop.wait(0.5 if in_flight else 3.0)
            else:
                time.sleep(0.5 if in_flight else 3.0)
    finally:
        dl_stop.set()
        for t in proc_threads:
            t.join(timeout=cfg.download_timeout_seconds + 60)
        dl_thread.join(timeout=cfg.download_timeout_seconds + 30)
        if pool is not None:
            from . import ingest as _ingest_mod

            _ingest_mod.set_parse_pool(None)
            pool.shutdown()
    if ingests_pending_rollup:
        rebuild_now()  # stop was set mid-batch — don't leave dashboards stale
    return processed
