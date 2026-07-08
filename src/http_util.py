"""Shared httpx client with retry/backoff and a streaming file-like adapter."""

from __future__ import annotations

import gzip
import io
import logging
import time
from contextlib import contextmanager
from typing import Iterator

import httpx

from .config import HttpConfig

log = logging.getLogger(__name__)

RETRYABLE_STATUS = {429, 500, 502, 503, 504}

GZIP_MAGIC = b"\x1f\x8b"


class FetchError(Exception):
    """A single fetch failed after retries. Carries the last HTTP status."""

    def __init__(self, url: str, status: int | None, msg: str):
        self.url = url
        self.status = status
        super().__init__(msg)


def make_client(http_cfg: HttpConfig) -> httpx.Client:
    return httpx.Client(
        headers={"User-Agent": http_cfg.user_agent},
        timeout=httpx.Timeout(http_cfg.timeout_seconds, connect=30.0),
        follow_redirects=True,
        # MRF payloads are .json.gz blobs; we do our own gzip streaming, so
        # don't ask servers to content-encode on top of that.
        http2=False,
    )


def _backoff_sleep(http_cfg: HttpConfig, attempt: int) -> None:
    delay = http_cfg.backoff_base_seconds * (2**attempt)
    log.info("retrying in %.1fs", delay)
    time.sleep(delay)


def get_with_retry(
    client: httpx.Client, http_cfg: HttpConfig, url: str
) -> httpx.Response:
    """GET a (small) resource fully, with retry/backoff on retryable statuses."""
    last_status: int | None = None
    for attempt in range(http_cfg.max_retries):
        try:
            resp = client.get(url)
            last_status = resp.status_code
            if resp.status_code == 200:
                return resp
            if resp.status_code in RETRYABLE_STATUS:
                log.warning("GET %s -> %s (attempt %d)", url, resp.status_code, attempt + 1)
                _backoff_sleep(http_cfg, attempt)
                continue
            raise FetchError(url, resp.status_code, f"GET {url} -> {resp.status_code}")
        except (httpx.TransportError, httpx.TimeoutException) as e:
            log.warning("GET %s network error: %s (attempt %d)", url, e, attempt + 1)
            _backoff_sleep(http_cfg, attempt)
    raise FetchError(url, last_status, f"GET {url} failed after {http_cfg.max_retries} attempts")


class _ResponseReader(io.RawIOBase):
    """Wrap an httpx streaming response's byte iterator as a file-like object."""

    def __init__(self, chunks: Iterator[bytes]):
        self._chunks = chunks
        self._buf = b""
        self._eof = False

    def readable(self) -> bool:
        return True

    def readinto(self, b) -> int:
        while len(self._buf) < len(b) and not self._eof:
            try:
                self._buf += next(self._chunks)
            except StopIteration:
                self._eof = True
        n = min(len(b), len(self._buf))
        b[:n] = self._buf[:n]
        self._buf = self._buf[n:]
        return n


def _maybe_gunzip(raw: io.BufferedIOBase, name_hint: str) -> io.BufferedIOBase:
    """Sniff the gzip magic bytes and wrap in a streaming decompressor if present."""
    head = raw.peek(2)[:2] if hasattr(raw, "peek") else b""
    if head == GZIP_MAGIC or (not head and name_hint.endswith(".gz")):
        return gzip.GzipFile(fileobj=raw)  # type: ignore[return-value]
    return raw


@contextmanager
def open_json_stream(
    source: str, client: httpx.Client | None, http_cfg: HttpConfig
) -> Iterator[io.BufferedIOBase]:
    """Yield a binary file-like for a local path or URL, gunzipping transparently.

    Never loads the payload into memory. Retries connection-level failures;
    non-retryable HTTP statuses raise FetchError for the caller to log & skip.
    """
    if not source.startswith(("http://", "https://")):
        with open(source, "rb") as f:
            buffered = io.BufferedReader(f, buffer_size=1 << 20)
            yield _maybe_gunzip(buffered, source)
        return

    assert client is not None, "client required for URL sources"
    last_status: int | None = None
    for attempt in range(http_cfg.max_retries):
        try:
            with client.stream("GET", source) as resp:
                last_status = resp.status_code
                if resp.status_code in RETRYABLE_STATUS:
                    log.warning(
                        "stream %s -> %s (attempt %d)", source, resp.status_code, attempt + 1
                    )
                    _backoff_sleep(http_cfg, attempt)
                    continue
                if resp.status_code != 200:
                    raise FetchError(
                        source, resp.status_code, f"GET {source} -> {resp.status_code}"
                    )
                reader = io.BufferedReader(
                    _ResponseReader(resp.iter_bytes(chunk_size=1 << 20)),
                    buffer_size=1 << 20,
                )
                yield _maybe_gunzip(reader, source.split("?")[0])
                return
        except (httpx.TransportError, httpx.TimeoutException) as e:
            log.warning("stream %s network error: %s (attempt %d)", source, e, attempt + 1)
            _backoff_sleep(http_cfg, attempt)
    raise FetchError(
        source, last_status, f"stream {source} failed after {http_cfg.max_retries} attempts"
    )
