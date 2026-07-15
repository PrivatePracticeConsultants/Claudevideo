"""URL-drop ingestion: download MRF URLs and auto-expand table-of-contents URLs.

Give the app a URL (or many) and it:
  1. streams the file to a staging folder (retry/backoff, atomic, progress),
  2. sniffs what it is (in-network rate file / provider-reference / TOC index),
  3. if it's a TOC, stream-parses it and enqueues every in-network file URL it
     lists (deduped, capped), which then flow through the same pipeline,
  4. otherwise hands the file to the normal chunked ingest.

Background workers drain the queue (`parallel_ingests` at a time; default 0 =
auto = CPU cores-1, capped 8) with a one-file-ahead prefetch, so aggregating a
whole payer (hundreds of files) never fills the disk: bounded files in flight,
and the raw download is deleted after a successful ingest (config-controlled),
keeping only the compact Parquet.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import ssl
import threading
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
# Two tiers: names that are unambiguously signature material are ALWAYS
# stripped; the short generic Azure-SAS names (st, se, sp, …) and `token` are
# stripped ONLY when the query also carries a real signature param — a listing
# whose per-file URLs differ only in `?st=NY` or `?token=<file-id>` must NOT
# collapse every file to one dedup key (silent under-ingestion).
_VOLATILE_ALWAYS = re.compile(
    r"^(expires|signature|policy|key-pair-id|awsaccesskeyid|x-amz-.*|sig)$", re.I)
_VOLATILE_IF_SIGNED = re.compile(
    r"^(token|se|sp|sv|sr|st|ss|srt|spr|sip|skoid|sktid|skt|ske|sks|skv|rscd|rsct)$",
    re.I,
)
_SIGNATURE_MARKERS = re.compile(
    r"^(sig|signature|x-amz-signature|x-amz-credential|awsaccesskeyid|key-pair-id)$", re.I)


def dedup_key(url: str) -> str:
    """Identity of a file independent of volatile signed-query params. Signed
    CDN URLs (mrf.bcbs.com, S3 presigned, Azure SAS) differ only in their
    signature params; identity params (e.g. ?file=...) are kept, sorted.
    Host is case-insensitive; the path is NOT (S3 keys are case-sensitive)."""
    parts = urlsplit(url)
    pairs = [p.partition("=") for p in parts.query.split("&") if p]
    signed = any(_SIGNATURE_MARKERS.match(k) for k, _, _ in pairs if k)
    kept = sorted(
        f"{k}={v}"
        for k, _, v in pairs
        if k and not _VOLATILE_ALWAYS.match(k)
        and not (signed and _VOLATILE_IF_SIGNED.match(k))
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
    (SSL_CERT_FILE / REQUESTS_CA_BUNDLE) IN ADDITION to the system trust
    store. Corporate networks and VPNs often intercept HTTPS with their own
    certificate authority; without this, every download fails with
    CERTIFICATE_VERIFY_FAILED even though the browser works. Some setups
    layer TWO interceptors (a local proxy CA in the env bundle plus a
    network-level gateway CA only in the system store) — trusting the env
    bundle INSTEAD of the system store breaks those, so both are loaded.
    Verification is never disabled — we only add CAs."""
    cafile = os.environ.get("SSL_CERT_FILE") or os.environ.get("REQUESTS_CA_BUNDLE")
    if cafile and Path(cafile).exists():
        try:
            ctx = ssl.create_default_context(cafile=cafile)
            ctx.load_default_certs(ssl.Purpose.SERVER_AUTH)  # system store too
            return ctx
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


def _host_bypasses_proxy(host: str) -> bool:
    """Rough NO_PROXY match (suffix rules only — the common case). When the
    host bypasses the proxy, certificate discovery must bypass it too, or we
    repair a chain httpx will never see."""
    raw = os.environ.get("NO_PROXY") or os.environ.get("no_proxy") or ""
    host = host.lower()
    for entry in (e.strip().lower() for e in raw.split(",") if e.strip()):
        e = entry.lstrip("*").lstrip(".")
        if "/" in e or not e:
            continue  # CIDR entries would need an IP lookup — skip
        if host == e or host.endswith("." + e):
            return True
    return False


def _fetch_leaf_pem(host: str, port: int) -> str:
    """Read the certificate a server presents, using the SAME network path
    the downloads will use: behind an HTTP(S) proxy the direct route can be
    intercepted by something else entirely, and repairing the wrong chain
    helps nobody. Like ssl.get_server_certificate, no data is exchanged and
    no trust decision is made here — the assembled chain is fully verified
    against real roots + hostname before anything is trusted."""
    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
    if proxy and _host_bypasses_proxy(host):
        proxy = None  # httpx will connect direct for this host — read direct too
    if proxy:
        try:
            import socket

            pp = urlsplit(proxy if "://" in proxy else "http://" + proxy)
            sock = socket.create_connection((pp.hostname, pp.port or 8080), timeout=20)
            try:
                connect = (f"CONNECT {host}:{port} HTTP/1.1\r\n"
                           f"Host: {host}:{port}\r\n")
                if pp.username:  # corporate proxies with credentials in the URL
                    import base64

                    cred = base64.b64encode(
                        f"{pp.username}:{pp.password or ''}".encode()).decode()
                    connect += f"Proxy-Authorization: Basic {cred}\r\n"
                sock.sendall((connect + "\r\n").encode())
                resp = b""
                while b"\r\n\r\n" not in resp and len(resp) < 65536:
                    chunk = sock.recv(4096)
                    if not chunk:
                        break
                    resp += chunk
                if b" 200" not in resp.split(b"\r\n", 1)[0]:
                    raise OSError(f"proxy CONNECT refused: {resp[:60]!r}")
                ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE  # certificate DISCOVERY only
                with ctx.wrap_socket(sock, server_hostname=host) as ss:
                    der = ss.getpeercert(binary_form=True)
                return ssl.DER_cert_to_PEM_cert(der)
            finally:
                sock.close()
        except OSError:
            pass  # fall through to the direct read
    return ssl.get_server_certificate((host, port), timeout=20)


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
        leaf_pem = _fetch_leaf_pem(host, port)
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
        import warnings

        with warnings.catch_warnings():
            # certifi ships one ancient root with a non-positive serial;
            # cryptography warns on every load — irrelevant noise here
            warnings.simplefilter("ignore")
            roots = x509.load_pem_x509_certificates(Path(certifi.where()).read_bytes())
        verifier = PolicyBuilder().store(Store(roots)).build_server_verifier(x509.DNSName(host))
        verifier.verify(leaf, inters)
        cafile = os.environ.get("SSL_CERT_FILE") or os.environ.get("REQUESTS_CA_BUNDLE") or certifi.where()
        ctx = ssl.create_default_context(cafile=cafile)
        ctx.load_default_certs(ssl.Purpose.SERVER_AUTH)  # match ssl_verify(): system store too
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


# some CDNs fronting legally-public MRF data (e.g. BCBS South Carolina's
# CloudFront) refuse anything that doesn't look like a browser. We identify
# honestly first and only fall back to this UA after a 403.
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")


def _client(cfg: MrfxConfig, verify=None, ua: str | None = None) -> httpx.Client:
    return httpx.Client(
        # Some payer CDNs (Akamai/Cloudflare fronts) reset a connection almost
        # immediately for a request that doesn't look like a browser. Send the
        # headers a browser always would. Accept-Encoding stays `identity`: MRFs
        # are already gzip, and letting the CDN re-encode complicates resume.
        headers={
            "User-Agent": ua or cfg.user_agent,
            "Accept": "*/*",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "identity",
            "Connection": "keep-alive",
        },
        follow_redirects=True,
        timeout=httpx.Timeout(cfg.download_timeout_seconds, connect=30.0),
        verify=verify if verify is not None else ssl_verify(),
    )


class DownloadError(Exception):
    def __init__(self, msg: str, status: int | None = None, retryable: bool = False,
                 oversize: bool = False):
        super().__init__(msg)
        self.status = status
        self.retryable = retryable
        self.oversize = oversize  # tripped confirm_over_gb — not a real failure


# Concurrent downloads must never COLLECTIVELY overcommit the disk. The single
# sequential downloader got that for free: bytes already written show up in
# shutil.disk_usage, so each new download's up-front guard saw the truth. With
# N downloads in flight, the not-yet-written remainders are invisible to
# disk_usage — so every in-flight download reserves its remaining bytes here,
# and the guard counts everyone else's reservations against free space.
# Reservations shrink as bytes land (the written bytes then show up in
# disk_usage instead — never double-counted) and are always released in the
# download's finally.
_disk_reservations: dict[int, int] = {}
_reservation_lock = threading.Lock()


# unknown-length (chunked) responses have no Content-Length to reserve against,
# so they hold a fixed PROVISIONAL reservation (below) — enough that concurrent
# known-length downloads' disk math can still see them — and re-check the disk
# every this-many streamed bytes.
_UNKNOWN_LEN_CHECK_BYTES = 256 << 20
# provisional headroom an unknown-length download reserves so it isn't invisible
# to the collective disk guard (it can't reserve its true size — unknown). Small
# files over-reserve briefly, then release on completion; the safety is worth it.
_UNKNOWN_LEN_RESERVATION = 2 << 30
# runaway backstop: an attempt that ADVANCES the .part doesn't count against the
# retry budget (a flaky server that resets every N MB must still be able to
# finish a large file over many resumes). The real terminator for a stuck server
# is the consecutive-no-progress `attempt` budget; this only bounds the
# pathological progress-but-never-finishes case. Set high enough to complete the
# documented 22 GB max even against an origin that resets every ~10 MB (~2200
# resumes), so a single download() call can finish rather than needing repeated
# queue retries; each connection sleeps ≥1 s, so this also caps wall-clock.
_MAX_DOWNLOAD_CONNECTIONS = 2500


def _set_reservation(my_key: int, remaining: int) -> None:
    with _reservation_lock:
        _disk_reservations[my_key] = max(0, remaining)


def _clear_reservation(my_key: int) -> None:
    with _reservation_lock:
        _disk_reservations.pop(my_key, None)


def _reserve_disk_or_raise(my_key: int, parent: Path, total: int, resume_from: int) -> None:
    """Atomic check-AND-reserve. The free-space read, the sum of other
    downloads' reservations, the verdict, and this download's own reservation
    all happen under ONE lock hold — check-then-reserve as two steps would let
    two downloads that start simultaneously (a freshly expanded TOC hands the
    downloader threads a batch at once) both pass the check before either
    reserves, overcommitting the disk together."""
    need = (total - resume_from) + (2 << 30)  # +2GB working headroom
    with _reservation_lock:
        free = shutil.disk_usage(parent).free
        others = sum(v for k, v in _disk_reservations.items() if k != my_key)
        if free - others < need:
            promised = (f" ({others / 1e9:.1f} GB of it is already promised "
                        "to downloads in progress)") if others else ""
            raise DownloadError(
                f"not enough free disk space for this file: it needs "
                f"~{(total - resume_from) / 1e9:.1f} GB (plus working room) but only "
                f"{free / 1e9:.1f} GB is free{promised}. Free up space and press retry.",
                retryable=False,
            )
        _disk_reservations[my_key] = max(0, total - resume_from)


def download(cfg: MrfxConfig, url: str, dest: Path, progress_cb=None,
             max_bytes: int | None = None) -> tuple[str, str]:
    """Stream a URL to `dest` (atomic via .part). Returns (sha256_hex,
    final_url) — the hash detects the same file arriving under a different
    domain, and final_url (post-redirect) is the correct base for resolving
    relative links found inside the payload. Retries transient failures;
    raises DownloadError on a terminal failure (e.g. expired signed URL) or
    when the payload exceeds max_bytes (the confirm_over_gb guard)."""
    # the token object keeps this call's reservation key from being recycled
    # while the download is alive; the finally releases it on EVERY exit —
    # success, terminal failure, or an unexpected exception mid-stream
    _token = object()
    try:
        return _download_reserved(cfg, url, dest, progress_cb, max_bytes, id(_token))
    finally:
        _clear_reservation(id(_token))


def _download_reserved(cfg: MrfxConfig, url: str, dest: Path, progress_cb,
                       max_bytes: int | None, my_key: int) -> tuple[str, str]:
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(dest.suffix + ".part")
    val_p = Path(str(part) + ".val")  # ETag/Last-Modified guarding resumes
    last_exc: Exception | None = None

    def too_big(n: int) -> DownloadError:
        return DownloadError(
            f"file is {n / 1e9:.1f} GB — larger than the confirm_over_gb "
            f"safety limit ({(max_bytes or 0) / 1e9:.1f} GB). Press "
            "\"download anyway\" on this row to ingest it, or raise "
            "confirm_over_gb in config/mrfx.yaml for all files.",
            retryable=False, oversize=True,
        )

    verify_override = None
    tried_chain_repair = False
    ua_override: str | None = None
    attempt = 0            # CONSECUTIVE no-progress attempts, counted vs the budget
    connections = 0        # total connections, runaway backstop
    while attempt <= cfg.download_retries and connections < _MAX_DOWNLOAD_CONNECTIONS:
        connections += 1
        bytes_before = part.stat().st_size if part.exists() else 0
        try:
            # RESUME: a .part left by a network drop or a killed process picks
            # up where it stopped instead of re-downloading gigabytes. The
            # filename is derived from the URL identity, so the part is for
            # THIS file; if the server ignores/rejects the range we start over.
            resume_from = part.stat().st_size if part.exists() else 0
            req_headers = {"Range": f"bytes={resume_from}-"} if resume_from else {}
            # If-Range with the validator saved when the .part was started:
            # a payer republishing DIFFERENT bytes under the same URL between
            # attempts would otherwise get spliced into the old .part by the
            # range resume — with If-Range the server sends a full 200 for
            # changed content, and the 200 path below restarts cleanly
            if resume_from and val_p.exists():
                validator = val_p.read_text().strip()
                if validator:
                    req_headers["If-Range"] = validator
            with _client(cfg, verify=verify_override, ua=ua_override) as client,                     client.stream("GET", url, headers=req_headers) as resp:
                if resume_from and resp.status_code == 416:
                    # range not satisfiable — stale/oversized part; start over
                    part.unlink(missing_ok=True)
                    val_p.unlink(missing_ok=True)
                    raise DownloadError("stale partial download discarded", retryable=True)
                if resp.status_code in RETRYABLE_STATUS:
                    raise DownloadError(
                        f"HTTP {resp.status_code}", resp.status_code, retryable=True
                    )
                if resp.status_code == 403:
                    if ua_override is None:
                        # CDN refuses non-browser user agents — retry
                        # immediately looking like a browser. Free retry:
                        # `attempt` is not advanced, so this works even when
                        # the 403 lands on the final attempt (or retries=0).
                        ua_override = BROWSER_UA
                        last_exc = DownloadError("HTTP 403 for our user-agent", 403, retryable=True)
                        log.info("%s: HTTP 403 for our user-agent; retrying as a browser", url)
                        continue
                    raise DownloadError(
                        "HTTP 403 — access refused. Usual causes: a signed URL expired "
                        "(re-copy a fresh link, or re-add the TOC it came from), or the "
                        "site blocks automated downloads — if the link works in your "
                        "browser, download it there and drop the file into data/inbox/.",
                        403, retryable=False,
                    )
                if resp.status_code == 404:
                    raise DownloadError("HTTP 404 — file not found at this URL", 404, retryable=False)
                if resp.status_code not in (200, 206):
                    raise DownloadError(f"HTTP {resp.status_code}", resp.status_code, retryable=False)

                resuming = resume_from > 0 and resp.status_code == 206
                if resuming:
                    # Content-Range: bytes <from>-<to>/<total>
                    cr = resp.headers.get("Content-Range", "")
                    total = int(cr.rsplit("/", 1)[-1]) if "/" in cr and cr.rsplit("/", 1)[-1].isdigit() else None
                else:
                    if resume_from and req_headers.get("Range"):
                        # we asked for a range and got a full 200 — this server
                        # ignores our conditional resume (e.g. it treats the
                        # stored validator as weak). Drop it so the NEXT attempt
                        # sends a plain unconditional Range instead of repeating
                        # the doomed If-Range and restarting from 0 forever.
                        val_p.unlink(missing_ok=True)
                    resume_from = 0  # server sent the whole file (or fresh start)
                    total = int(resp.headers.get("Content-Length") or 0) or None
                if max_bytes and total and total > max_bytes:
                    raise too_big(total)

                # disk guard: a payer file must never run the machine out of
                # space mid-download — fail up front with the friendly fix.
                # Other in-flight downloads' unwritten remainders don't show in
                # disk_usage yet, so their reservations count against free;
                # check-and-reserve is one atomic step (see the helper).
                if total:
                    _reserve_disk_or_raise(my_key, dest.parent, total, resume_from)
                else:
                    # no Content-Length: reserve a provisional headroom so a
                    # concurrent known-length download counts this one in its
                    # up-front guard instead of overcommitting the disk.
                    _set_reservation(my_key, _UNKNOWN_LEN_RESERVATION)

                sha = hashlib.sha256()
                if resuming:
                    with open(part, "rb") as f:  # hash what we already have
                        for blk in iter(lambda: f.read(1 << 20), b""):
                            sha.update(blk)
                    log.info("resuming download at %.1f GB / %s", resume_from / 1e9,
                             f"{total / 1e9:.1f} GB" if total else "?")
                else:
                    # remember the content validator for future resumes. If-Range
                    # REQUIRES a strong validator: a weak ETag (W/"...", common
                    # behind CDNs / on-the-fly gzip) makes an RFC-compliant server
                    # answer a range request with a full 200 instead of a 206, so
                    # `resuming` stays false, the .part is truncated, and every
                    # retry restarts at byte 0 — a large file behind a flaky
                    # origin then never finishes. Prefer a strong ETag, else fall
                    # back to Last-Modified (a valid If-Range validator). Store
                    # nothing if neither exists, so resume sends a plain Range
                    # (unconditional 206) rather than a doomed If-Range.
                    etag = (resp.headers.get("ETag") or "").strip()
                    strong = etag if etag and not etag.upper().startswith("W/") else ""
                    validator = strong or (resp.headers.get("Last-Modified") or "").strip()
                    if validator:
                        val_p.write_text(validator)
                    else:
                        val_p.unlink(missing_ok=True)
                done = resume_from
                last_report = done
                last_report_t = 0.0
                last_free_check = done  # for the unknown-length mid-stream guard
                with open(part, "ab" if resuming else "wb") as f:
                    for chunk in resp.iter_bytes(chunk_size=1 << 20):
                        f.write(chunk)
                        sha.update(chunk)
                        done += len(chunk)
                        if total:
                            # written bytes now show in disk_usage; shrink the
                            # reservation so they're never counted twice
                            _set_reservation(my_key, total - done)
                        # periodic free-space floor re-check for BOTH branches:
                        # a known-length download reserved up front but never
                        # re-checked, and an unknown-length one can't reserve its
                        # true size — either way, concurrent downloads can erode
                        # the free space mid-stream, so fail friendly at the 2 GB
                        # floor instead of hitting a raw ENOSPC.
                        if done - last_free_check >= _UNKNOWN_LEN_CHECK_BYTES:
                            last_free_check = done
                            if shutil.disk_usage(dest.parent).free < (2 << 30):
                                raise DownloadError(
                                    "not enough free disk space to continue this download "
                                    f"(~{done / 1e9:.1f} GB written, under 2 GB left on the "
                                    "drive). Free up space and press retry — it resumes "
                                    "where it stopped.", retryable=False)
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
                # a proxy/server can close the stream early without an error —
                # a truncated download must never masquerade as the file
                # (zip/gzip would fail later with a confusing message). The
                # .part is kept, so the retry RESUMES from this exact byte.
                if total is not None and done < total:
                    raise DownloadError(
                        f"connection closed early ({done / 1e6:.0f} of {total / 1e6:.0f} MB) "
                        "— retrying from where it stopped", retryable=True)
                final_url = str(resp.url)
            part.replace(dest)
            val_p.unlink(missing_ok=True)
            return sha.hexdigest(), final_url
        except DownloadError as e:
            last_exc = e
            if not e.retryable:
                # keep the .part when a retry could finish it (the disk-space
                # and confirm_over_gb guards both tell the user to fix the
                # setting and press retry — the bytes are still good); discard
                # it when the content itself is the problem
                if "disk space" not in str(e) and "confirm_over_gb" not in str(e):
                    part.unlink(missing_ok=True)
                    val_p.unlink(missing_ok=True)
                raise
            log.warning("download %s: %s (attempt %d/%d)", url, e, attempt + 1, cfg.download_retries + 1)
        except (httpx.TransportError, httpx.TimeoutException) as e:
            last_exc = e
            # server sent an incomplete cert chain? repair it like a browser
            # and retry immediately — a free retry (attempt not advanced),
            # like the browser-UA fallback above
            if "CERTIFICATE_VERIFY_FAILED" in str(e) and not tried_chain_repair:
                tried_chain_repair = True
                ctx = _repair_incomplete_chain(url)
                if ctx is not None:
                    verify_override = ctx
                    continue
            log.warning("download %s network error: %s (attempt %d/%d)", url, e, attempt + 1, cfg.download_retries + 1)
        # A connection that delivered NEW bytes made forward progress: a server
        # that resets every N MB would otherwise burn the whole retry budget on
        # a large file even though each attempt advances toward completion. Give
        # it a free retry (reset the no-progress counter) and a short pause; only
        # CONSECUTIVE zero-progress attempts count against download_retries.
        # _MAX_DOWNLOAD_CONNECTIONS bounds the total so a truly stuck server ends.
        bytes_after = part.stat().st_size if part.exists() else 0
        if bytes_after > bytes_before:
            log.info("download %s: advanced to %.0f MB after connection %d; resuming",
                     url, bytes_after / 1e6, connections)
            attempt = 0
            time.sleep(1.0)
        else:
            attempt += 1
            if attempt <= cfg.download_retries:
                time.sleep(2.0 * (2**(attempt - 1)))
    # keep the .part: every failure that lands here was transient (terminal
    # ones raised above), so a later "retry" on the queue resumes the download
    # instead of restarting a multi-GB file from byte zero
    msg = f"download failed after {connections} attempts: {last_exc}"
    if "CERTIFICATE_VERIFY_FAILED" in str(last_exc):
        msg += TLS_HELP
    # retryable=True: the CAUSE was transient (terminal causes raised above),
    # so callers must not treat this like a dead URL and destroy kept files
    raise DownloadError(msg, retryable=True)


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
    excluded: dict[str, int] = {"allowed-amounts": 0, "drug-pricing": 0}

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
                excluded["allowed-amounts"] += 1
                continue  # out-of-network billed averages — no negotiated rates
            is_rate_file = "in-network" in low
            if is_rate_file and _DRUG_FILE_RE.search(low):
                # NDC / prescription-drug pricing — no medical CPT/HCPCS rates;
                # ingesting would only produce honest zero-row files
                excluded["drug-pricing"] += 1
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
    if any(excluded.values()):
        log.info("listing: excluded %d allowed-amounts and %d drug-pricing entries (no medical rates)",
                 excluded["allowed-amounts"], excluded["drug-pricing"])
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
# listings like mrfdata.hmhs.com use href="2026-06-25_...json.gz" with no
# path). data-* attributes too: CareFirst puts the real URL in data-key="..."
# behind an href="javascript:void(0)" download button.
_ATTR_LINK_RE = re.compile(
    r"""(?:href|src|data-[a-z0-9_-]+)\s*=\s*["'](?P<u>[^"']+?(?:\.json(?:\.gz)?|\.zip)(?:\?[^"']*)?)["']""",
    re.I,
)
# absolute file URLs anywhere else in the page (inlined JS/config blobs)
_ABS_LINK_RE = re.compile(
    r"""(?P<u>https?://[^"'\s<>]+?(?:\.json(?:\.gz)?|\.zip)(?:\?[^"'\s<>]*)?)["'<\s]""",
    re.I,
)
# relative .json paths quoted in inline JS/config or JSON listings: Cigna
# embeds its manifest as "/static/mrf/latest.json" in page settings, and
# HealthSparq metadata lists TOCs as "2026-07-01/tableOfContents/x_index
# .json.gz" (resolved against the listing's own URL). A slash is required —
# bare filenames ("package.json" in framework bundles) stay invisible.
_REL_JSON_RE = re.compile(
    r"""["'](?P<u>(?:/[^"'\s<>]+?|[^"'\s<>:]+/[^"'\s<>]+?)(?:\.json(?:\.gz)?|\.zip)(?:\?[^"'\s<>]*)?)["']""",
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
    r"(/page-data/|/manifest\.json|\.webmanifest|/favicon|/asset-manifest|/app-data\.json"
    r"|/jcr:content/"          # AEM page components (BCBS NC embeds them as .json)
    r"|search-api\.swiftype\.com|/api/v1/public/installs/"  # site-search widgets
    # web-app plumbing directories: the relative-path pattern would otherwise
    # lift every "locales/en.json"-style bundle path off rendered pages.
    # Deliberately NOT /static/ or /assets/ — payers host real MRF manifests
    # there (Cigna's /static/mrf/latest.json).
    r"|/wp-content/|/wp-includes/|/etc\.clientlibs/|/node_modules/"
    r"|[/\"'](?:locales|i18n|lang)/[a-z]{2}(?:[-_][A-Za-z]{2,4})?\.json"
    r"|/_next/static/|/webpack/)", re.I
)


def extract_links_from_text(text: str, base_url: str, max_files: int,
                            seen: set[str] | None = None) -> list[str]:
    """Pull .json / .json.gz / .zip MRF links out of markup or JS/JSON text.
    Returns absolute, deduped URLs; pass `seen` (dedup keys) to accumulate
    across multiple sources (rendered DOM + captured API responses)."""
    seen = seen if seen is not None else set()
    out: list[str] = []
    for pattern in (_ATTR_LINK_RE, _ABS_LINK_RE, _REL_JSON_RE):
        for m in pattern.finditer(text):
            # payer filenames legitimately contain spaces ("carefirst ppo_
            # index.json" on Azure) — encode them or the request is invalid
            u = urljoin(base_url, m.group("u").strip()).replace(" ", "%20")
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


def extract_links_from_page(path: Path, base_url: str, max_files: int) -> list[str]:
    """Best effort: pull .json / .json.gz links out of an HTML page (payer
    directory listings like mrfdata.hmhs.com are plain pages full of file
    links). Returns absolute, deduped URLs. Empty for JS-only portals."""
    try:
        with open(path, "rb") as f:
            text = f.read(64 << 20).decode(errors="replace")  # match the 64 MB link-container admit limit
    except OSError:
        return []
    return extract_links_from_text(text, base_url, max_files)


PAGE_HELP = (
    "This link is a web page, not a data file, and no file links could be "
    "found on it (many payer portals load their file lists with JavaScript). "
    "Open the page in your browser, right-click the actual file links "
    "(they end in .json or .json.gz — often labeled 'in-network' or "
    "'Table of Contents') and choose 'Copy link address', then paste those "
    "here instead."
)

PAGE_HELP_NO_RENDERER = PAGE_HELP + (
    " Tip: installing Playwright lets the app render JavaScript pages "
    "automatically — run: pip install playwright && playwright install "
    "chromium — then press retry."
)


def _page_help() -> str:
    from .render import render_available

    return PAGE_HELP if render_available() else PAGE_HELP_NO_RENDERER


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
            data = r.json()
            if not isinstance(data, dict):
                return []  # catch-all routes answer 200 with arrays/strings
            hashes = data.get("staticQueryHashes") or []
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


_MONTH_DATE_RE = re.compile(r"(\d{4})-(\d{2})-01")


def _maybe_queue_prev_month(store: Store, rec: dict, err: str) -> None:
    """Payers publish monthly-dated files, and early in a month the new one
    often isn't posted yet — a 404 on a CURRENT-month-dated URL automatically
    queues last month's name instead of leaving the user a wall of failures."""
    if "404" not in err:
        return
    m = _MONTH_DATE_RE.search(rec.get("url") or "")
    if not m:
        return
    import datetime as dt

    today = dt.date.today()
    if (int(m.group(1)), int(m.group(2))) != (today.year, today.month):
        return  # only fall back one step, from the current month
    prev = (today.replace(day=1) - dt.timedelta(days=1)).replace(day=1)
    prev_url = rec["url"].replace(m.group(0), prev.isoformat(), 1)
    if store.enqueue_url(prev_url, dedup_key(prev_url), parent_id=rec.get("parent_id")) is not None:
        log.info("%s not published yet — queued last month's file instead", rec["url"])
        store.update_url(rec["id"],
                         error=err + " — this month's file isn't posted yet; "
                               "queued last month's version instead")


_META_SUFFIX = ".fetchmeta"  # sidecar handing a prefetched download to the processor


def _meta_path(dest: Path) -> Path:
    return Path(str(dest) + _META_SUFFIX)


def resolve_worker_count(cfg: MrfxConfig) -> int:
    """Effective parser-process count. 0 (or unset) = auto: (CPU cores - 1)
    capped at 8; a positive value is honored but still clamped to cores-1
    (extra processes past the core count only add contention). Never less
    than 1. Central so run_queue, the prefetch sizing, and the CLI banner
    all agree."""
    cores_cap = max(1, (os.cpu_count() or 2) - 1)
    configured = int(getattr(cfg, "parallel_ingests", 0) or 0)
    if configured <= 0:
        return min(cores_cap, 8)
    return max(1, min(configured, cores_cap))


# politeness ceiling for AUTO download concurrency: parsers chew ~30s per
# uncompressed GB while a fetch is pure I/O, so a handful of concurrent
# fetches saturates the pipeline — more would just hammer payer CDNs
_AUTO_DOWNLOAD_CAP = 4


def resolve_download_count(cfg: MrfxConfig) -> int:
    """Effective concurrent-download count. 0 (or unset) = auto: enough to
    keep the parser pool fed (min(workers, 4), floor 2 so even a one-core
    box overlaps network with parse); an explicit value is honored up to the
    config's ceiling of 8. 1 = the old strictly-sequential downloader."""
    configured = int(getattr(cfg, "parallel_downloads", 0) or 0)
    if configured <= 0:
        return max(2, min(_AUTO_DOWNLOAD_CAP, resolve_worker_count(cfg)))
    return max(1, min(configured, 8))


def _size_limit_for(cfg: MrfxConfig, rec: dict) -> int:
    """confirm_over_gb byte ceiling for this row — 0 (no ceiling) when the
    user pressed 'download anyway' on it. The disk-space guard inside
    download() is separate and ALWAYS applies, so this never risks the disk."""
    if rec.get("force_size"):
        return 0
    return int(cfg.confirm_over_gb * 1e9)


def _write_meta(dest: Path, content_sha: str, final_url: str) -> None:
    """Atomic sidecar write: a crash mid-write must leave either the old
    sidecar or the new one, never a half-JSON that fails parsing later."""
    meta_p = _meta_path(dest)
    tmp = Path(str(meta_p) + ".tmp")
    tmp.write_text(json.dumps({"sha": content_sha, "final_url": final_url}))
    tmp.replace(meta_p)


def fetch_url_record(cfg: MrfxConfig, store: Store, rec: dict) -> bool:
    """Download stage only (used by the prefetch thread): stream the file to
    disk, record its hash, and mark the row 'fetched' for the processor.
    Returns False on failure (row marked failed, worker keeps going)."""
    url_id, url = rec["id"], rec["url"]
    dest = cfg.downloads_dir / filename_for(url)
    keep_p = _meta_path(dest)
    if dest.exists() and keep_p.exists():
        # complete bytes already on disk (a kept duplicate download whose
        # twin failed, or a prior prefetch). NEVER re-download here: the
        # signed URL may have expired, and a terminal 403 would delete the
        # only copy of the bytes this row was revived to use.
        try:
            meta = json.loads(keep_p.read_text())
            store.update_url(url_id, content_sha=meta["sha"], status="fetched")
            return True
        except (ValueError, KeyError, OSError):
            keep_p.unlink(missing_ok=True)  # corrupt sidecar — fresh download below
    try:
        content_sha, final_url = download(
            cfg, url, dest,
            progress_cb=lambda d, t: store.url_progress(url_id, d, t),
            max_bytes=_size_limit_for(cfg, rec))
    except DownloadError as e:
        # over the size limit is NOT a failure — it's "too big, needs your OK"
        # (distinct amber state so it never buries a real error in the count)
        log.warning("url %s download stopped: %s", url, e)
        store.update_url(url_id, status="oversize" if e.oversize else "failed", error=str(e))
        # duplicates may have deferred to this row in an earlier life (it
        # ingested once, failed, was retried, and now its signed URL is dead):
        # they hold the kept bytes and must get their promised auto-retry
        _revive_twins(store, rec.get("content_sha") or "", url_id)
        _maybe_queue_prev_month(store, rec, str(e))
        if not e.retryable:
            # a stale complete download from an earlier run must not sit on
            # disk forever once its URL is TERMINALLY dead — but a transient
            # failure (network blip after retries) must not destroy it
            dest.unlink(missing_ok=True)
            _meta_path(dest).unlink(missing_ok=True)
        return False
    _write_meta(dest, content_sha, final_url)
    store.update_url(url_id, content_sha=content_sha, status="fetched")
    return True


def _mentions_allowed_amounts(dest: Path) -> bool:
    """Cheap scan of a (possibly gzipped) TOC for allowed_amount_file keys —
    only consulted when expansion found zero in-network files."""
    try:
        from .sniff import open_stream

        with open_stream(dest) as stream:
            return b"allowed_amount" in stream.read(8 << 20)
    except Exception:  # noqa: BLE001 — classification nicety, never fatal
        return False


def _enqueue_children(store: Store, urls: list[str], parent_id: int) -> int:
    """Queue discovered child URLs under their parent row; returns how many
    were newly queued. One lock/connection for the whole batch — thousands of
    children from one index must not thrash the store."""
    return store.enqueue_urls([(u, dedup_key(u)) for u in urls], parent_id=parent_id)


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
    meta = None
    if dest.exists() and meta_p.exists():
        # prefetched by the downloader thread — pick up where it left off.
        # The sidecar is NOT unlinked here: it must outlive the whole ingest
        # (hours on a national file), because a kill mid-ingest re-queues the
        # row and the reuse fast path requires dest+sidecar to avoid a fresh
        # download — where an expired signed URL would 403 terminally and
        # DELETE the only complete copy. Terminal states clean it up
        # (_cleanup_raw on success, the failure/skip paths below).
        try:
            meta = json.loads(meta_p.read_text())
            content_sha, final_url = meta["sha"], meta["final_url"]
        except (ValueError, KeyError, OSError):
            meta = None  # corrupt sidecar: fall through to a fresh download
            meta_p.unlink(missing_ok=True)
    if meta is None:
        try:
            # (row already marked 'downloading' by next_queued_url when claimed)
            content_sha, final_url = download(
                cfg, url, dest,
                progress_cb=lambda d, t: store.url_progress(url_id, d, t),
                max_bytes=_size_limit_for(cfg, rec))
        except DownloadError as e:
            log.warning("url %s download stopped: %s", url, e)
            store.update_url(url_id, status="oversize" if e.oversize else "failed",
                             error=str(e))
            _maybe_queue_prev_month(store, rec, str(e))
            # a failed re-download may strand twins that deferred to this row
            # on a PREVIOUS attempt (its sha is still on the record)
            _revive_twins(store, rec.get("content_sha") or "", url_id)
            if not e.retryable:
                dest.unlink(missing_ok=True)
                meta_p.unlink(missing_ok=True)
            return False
        store.update_url(url_id, content_sha=content_sha)
        # sidecar written on THIS path too, so a kill mid-ingest resumes from
        # the kept download instead of re-downloading (see the note above)
        _write_meta(dest, content_sha, final_url)

    # Blue plans host copies of each other's national files — the same bytes
    # arrive under many domains. Skip byte-identical repeats of files ALREADY
    # ingested instead of re-parsing millions of duplicate rows. This is the
    # done-only fast path; a twin still mid-ingest is arbitrated by the atomic
    # claim below (an unlocked skip here could race that twin's own skip and
    # strand the content with both rows skipped).
    twin = store.find_url_with_same_content(content_sha, exclude_id=url_id)
    if twin:
        twin_url = twin[0]
        store.update_url(url_id, status="skipped", kind="duplicate",
                         error="identical to a file already ingested (payers host copies "
                               f"of each other's files) — skipped as duplicate of {twin_url.split('?')[0]}")
        dest.unlink(missing_ok=True)
        meta_p.unlink(missing_ok=True)
        log.info("%s — byte-identical to %s; skipped", url, twin_url.split("?")[0])
        return False

    try:
        pf = preflight(dest, cfg, store)
    except Exception as e:  # noqa: BLE001 — classification must never crash the worker
        store.update_url(url_id, status="failed", error=f"could not read file: {e}")
        dest.unlink(missing_ok=True)
        meta_p.unlink(missing_ok=True)
        # twins that deferred to this row were promised an automatic retry if
        # it failed — honor that on THIS failure path too, or their content is
        # stranded 'skipped' forever
        _revive_twins(store, content_sha, url_id)
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
            # a TOC with only allowed_amount_file entries is a common TPA
            # shape (out-of-network averages, no negotiated rates) — that's
            # the payer's publication choice, not an error on our side
            if _mentions_allowed_amounts(dest):
                store.update_url(url_id, status="skipped", kind="toc",
                                 error="this TOC lists only out-of-network allowed-amounts "
                                       "files — no negotiated-rate files to ingest")
            else:
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
            browser_missing = False
            if not links and cfg.render_js:
                # JavaScript-only portal: render it in headless Chromium and
                # harvest links from the DOM and the page's API responses
                from .render import RenderBrowserMissing, render_page_links

                store.update_url(url_id, error="page has no static links — rendering it in a headless browser…")
                try:
                    links = render_page_links(cfg, final_url, cfg.max_toc_files)
                except RenderBrowserMissing:
                    browser_missing = True
            if links:
                added = _enqueue_children(store, links, url_id)
                log.info("%s — web page: found %d file links, %d newly queued", url, len(links), added)
                store.update_url(url_id, status="done", kind="page", child_count=added, error=None)
            else:
                msg = (PAGE_HELP + " The app tried to download the headless browser "
                       "automatically but couldn't (usually no internet, or disk full). "
                       "Once you're back online, press retry — or run this once yourself: "
                       "playwright install chromium") if browser_missing else _page_help()
                store.update_url(url_id, status="failed", kind="page", error=msg)
            return False
        # Not HTML and not a recognized MRF shape. Some payers publish custom
        # JSON wrappers that just list file URLs (BCBS Tennessee's
        # {"TOC_Files": [...]} directories, for example) — lift any MRF-shaped
        # links out of a small unknown file before giving up.
        if dest.stat().st_size <= 64 << 20:
            links = extract_links_from_page(dest, final_url, cfg.max_toc_files)
            if links:
                dest.unlink(missing_ok=True)
                added = _enqueue_children(store, links, url_id)
                log.info("%s — link container: %d file links, %d newly queued", url, len(links), added)
                store.update_url(url_id, status="done", kind="page", child_count=added, error=None)
                return False
        store.update_url(url_id, status="failed", kind="unknown",
                         error="; ".join(pf.messages) or "unrecognized file (not MRF JSON)")
        dest.unlink(missing_ok=True)
        return False

    # in-network rate file or standalone provider-reference file: run the
    # normal (chunked) ingest in place. Deliberately NOT moved into the inbox —
    # the folder watcher would race the queue worker on the same file.
    #
    # Atomically claim this content for ingest. A byte-identical twin may have
    # started or finished ingesting during our preflight — or be racing us in
    # another processor thread — and the cheap check above uses queue-id order,
    # which a retry can defeat. This check+claim is one locked step, so exactly
    # one twin per content ingests; a loser defers (and revives if we later
    # fail, via the kept download).
    # NOTE: on defer, the claim flips THIS row to skipped/duplicate inside the
    # same locked step (a caller-side flip would let two racing twins mutually
    # defer and both end skipped with nobody ingesting) — only the bytes are
    # handled here.
    twin2 = store.claim_content_ingest(content_sha, url_id, pf.file_type, dest.name)
    if twin2:
        twin2_url, twin2_status = twin2
        if twin2_status == "done":
            # the twin FINISHED while we were preflighting — same terminal
            # treatment as the early check: nothing to revive, drop the bytes
            dest.unlink(missing_ok=True)
            _meta_path(dest).unlink(missing_ok=True)
        else:
            # twin is mid-ingest and can still fail: keep the only other copy
            # so revive_skipped_duplicates can re-run this row without a
            # re-download (signed URLs expire)
            _write_meta(dest, content_sha, final_url)
        log.info("%s — byte-identical to %s (claimed concurrently); skipped",
                 url, twin2_url.split("?")[0])
        return False
    try:
        result = ingest_file(cfg, store, dest, pf=pf, progress_bar=progress_bar,
                              rebuild_rollups=rebuild_rollups)
    except Exception as e:  # noqa: BLE001 — belt and suspenders; ingest_file already isolates
        store.update_url(url_id, status="failed", kind=pf.file_type, error=f"ingest crashed: {e}")
        _revive_twins(store, content_sha, url_id)
        return False

    status = result.get("status")
    if status == "done":
        n = result.get("rows", result.get("refs", 0))
        store.update_url(url_id, status="done", kind=pf.file_type,
                         rows_emitted=n or 0, error=None)
        if cfg.delete_raw_after_ingest:
            _cleanup_raw(cfg, dest)
            # duplicates that deferred to this row kept their downloads in
            # case we failed — we succeeded, so those bytes can go
            for u in store.skipped_duplicate_urls(content_sha, url_id):
                dup = cfg.downloads_dir / filename_for(u)
                dup.unlink(missing_ok=True)
                _meta_path(dup).unlink(missing_ok=True)
        return True
    elif status == "pending_confirmation":
        store.update_url(url_id, status="oversize", kind=pf.file_type,
                         error="file exceeds the confirm_over_gb safety limit — press "
                               "\"download anyway\" on this row, or raise confirm_over_gb "
                               "in config/mrfx.yaml")
        _revive_twins(store, content_sha, url_id)
    else:
        store.update_url(url_id, status="failed", kind=pf.file_type,
                         error=result.get("error") or status)
        _revive_twins(store, content_sha, url_id)
    return False


def _revive_twins(store: Store, content_sha: str, failed_id: int) -> None:
    n = store.revive_skipped_duplicates(content_sha, failed_id)
    if n:
        log.info("re-queued %d duplicate link(s) that deferred to the failed row "
                 "(they resume from their kept downloads)", n)


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

# ...but a grind of large files can take many minutes to reach 10, leaving the
# dashboard (which reads the rollups, not the raw rates) looking stale — the
# "filters don't work while it's still processing" complaint. Also rebuild when
# the newest ingests are older than this, so analytics stay fresh mid-grind
# without the per-file quadratic cost.
ROLLUP_MAX_STALE_SECONDS = 90

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
    if cfg.delete_raw_after_ingest:
        # reap downloads stranded by a kill between the 'done' write and
        # _cleanup_raw — the row is terminal, so nothing else ever reclaims them
        swept = 0
        for name in store.done_download_filenames():
            f = cfg.downloads_dir / name
            if f.exists():
                f.unlink(missing_ok=True)
                _meta_path(f).unlink(missing_ok=True)
                swept += 1
        if swept:
            log.info("reclaimed %d finished download(s) left behind by an earlier kill", swept)
    processed = 0
    ingests_pending_rollup = 0
    import threading
    state = threading.Lock()

    rollup_failures = 0
    last_rebuild_at = time.monotonic()
    rollup_min_interval = float(ROLLUP_MAX_STALE_SECONDS)

    def rebuild_now():
        nonlocal ingests_pending_rollup, rollup_failures, last_rebuild_at, rollup_min_interval
        with state:
            n = ingests_pending_rollup
        if not n:
            return
        # stamp at attempt time so a failing rebuild doesn't re-fire the
        # time-based trigger every loop (batch/idle triggers still apply)
        last_rebuild_at = time.monotonic()
        log.info("updating analytics rollups (%d newly ingested file(s))...", n)
        try:
            store.rebuild_rollups()
        except Exception:  # noqa: BLE001 — rollups retry on the next batch
            rollup_failures += 1
            if rollup_failures >= 3:
                # never loop forever on a rebuild that keeps failing: the raw
                # rates are safely on disk; dashboards just lag until the next
                # successful rebuild (next batch, restart, or `mrfx serve`)
                log.error(
                    "rollup rebuild failed %d times — giving up for this run; "
                    "raw data is safe, analytics will refresh on the next "
                    "successful rebuild", rollup_failures)
                with state:
                    ingests_pending_rollup = 0
                return
            log.exception("rollup rebuild failed; will retry after the next file")
            return
        rollup_failures = 0
        # Re-stamp at COMPLETION and self-throttle by how long the rebuild
        # actually took: counting the 90s window from the start would make a
        # >90s rebuild permanently "stale" the moment it finished — a full
        # rebuild after every single file, the exact per-file quadratic cost
        # the batching exists to avoid, each one holding the write lock.
        duration = time.monotonic() - last_rebuild_at
        rollup_min_interval = max(float(ROLLUP_MAX_STALE_SECONDS), 2.0 * duration)
        last_rebuild_at = time.monotonic()
        with state:
            ingests_pending_rollup -= n

    # Pipeline: N downloader threads prefetch ahead while the parsers work —
    # network and CPU overlap instead of taking turns, and on many-small-file
    # payers several fetches run at once so the parsers never starve waiting
    # on one link. Roughly `prefetch_ahead` files sit fetched-but-unprocessed
    # on disk (each thread checks the buffer before claiming, so the peak is
    # prefetch_ahead - 1 + n_downloads — bounded). Disk safety does NOT rely
    # on the buffer count: every download reserves its remaining bytes
    # (_disk_reservations) and the up-front guard counts everyone else's
    # reservations against free space, so concurrent fetches can never
    # collectively overcommit the drive.
    dl_stop = threading.Event()

    # keep every parser fed: allow one fetched file per worker plus one spare
    prefetch_ahead = max(PREFETCH_AHEAD, resolve_worker_count(cfg) + 1)

    def downloader():
        while not dl_stop.is_set() and (stop is None or not stop.is_set()):
            try:
                counts = store.url_queue_counts()
                if counts.get("fetched", 0) >= prefetch_ahead:
                    dl_stop.wait(0.5)
                    continue
                rec = store.next_queued_url()  # atomic claim — threads never share a row
                if rec is None:
                    dl_stop.wait(0.5)
                    continue
                try:
                    fetch_url_record(cfg, store, rec)
                except Exception:  # noqa: BLE001 — downloader must never die
                    log.exception("prefetch: unexpected error on %s", rec.get("url"))
                    try:
                        store.update_url(rec["id"], status="failed", error="unexpected download error")
                        # a non-DownloadError failure (e.g. a store hiccup mid-write)
                        # still ends this row: revive any twins that deferred to its
                        # content so the shared content isn't orphaned as skipped
                        # (matches the DownloadError path at fetch_url_record).
                        _revive_twins(store, rec.get("content_sha") or "", rec["id"])
                    except Exception:  # noqa: BLE001 — correlated failure (disk full
                        # breaking both download AND the DB write): the row stays
                        # 'downloading' with no owner; say so instead of silently
                        # swallowing it — recover_stuck_urls re-queues it (and any
                        # stranded twins) on the next worker start.
                        log.exception(
                            "prefetch: could not record the failure for %s — the row "
                            "will be recovered on the next start", rec.get("url"))
            except Exception:  # noqa: BLE001 — even store hiccups must not kill the loop
                log.exception("prefetch: transient store error; retrying shortly")
                dl_stop.wait(2.0)

    n_downloads = resolve_download_count(cfg)
    dl_threads = [threading.Thread(target=downloader, daemon=True, name=f"mrfx-prefetch-{i}")
                  for i in range(n_downloads)]
    for t in dl_threads:
        t.start()
    if n_downloads > 1:
        log.info("parallel downloads: %d fetcher thread(s)", n_downloads)

    # Processor side: parallel_ingests threads each claim a fetched file and
    # run it through process_url_record. With parallel_ingests > 1 a process
    # pool does the CPU-heavy parsing (ingest_file dispatches to it), so the
    # threads mostly wait — the GIL never serializes the parsing itself. All
    # DuckDB writes happen in THIS process; workers only compute.
    parallel = resolve_worker_count(cfg)
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
                    try:
                        store.update_url(rec["id"], status="failed", error="unexpected worker error")
                        # duplicates may have deferred to this row while it
                        # was 'ingesting' — free them to retry
                        _revive_twins(store, rec.get("content_sha") or "", rec["id"])
                    except Exception:  # noqa: BLE001 — even the recovery write can
                        # hit a locked store; if it does, the row is unstuck by
                        # recover_stuck_urls on the next start — but this THREAD
                        # must survive or the whole queue stalls
                        log.exception("url worker: could not record the failure; continuing")
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

    interrupted = False
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
                         + counts.get("fetched", 0) + counts.get("expanding", 0)
                         + counts.get("ingesting", 0) + busy)
            stale = pend and (time.monotonic() - last_rebuild_at) >= rollup_min_interval
            if pend >= ROLLUP_BATCH_FILES or (in_flight == 0 and pend) or stale:
                rebuild_now()
                continue
            if in_flight == 0 and drain:
                break
            if stop is not None:
                stop.wait(0.5 if in_flight else 3.0)
            else:
                time.sleep(0.5 if in_flight else 3.0)
    except KeyboardInterrupt:
        # Ctrl-C: don't sit in multi-minute thread joins below — the threads
        # are daemons and every in-flight state recovers on the next start
        # (.part downloads resume; 'downloading'/'ingesting' rows are
        # re-queued by recover_stuck_urls). Exit fast; work is safe.
        interrupted = True
        raise
    finally:
        dl_stop.set()
        join_proc = 2.0 if interrupted else cfg.download_timeout_seconds + 60
        join_dl = 2.0 if interrupted else cfg.download_timeout_seconds + 30
        for t in proc_threads:
            t.join(timeout=join_proc)
        for t in dl_threads:
            t.join(timeout=join_dl)
        if pool is not None:
            from . import ingest as _ingest_mod

            _ingest_mod.set_parse_pool(None)
            pool.shutdown()
    if ingests_pending_rollup:
        rebuild_now()  # stop was set mid-batch — don't leave dashboards stale
    return processed
