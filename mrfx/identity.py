"""What a file IS: its full sha256 and, for a gzip, whether it has one member.

Two questions depend on this (review 9, items 1.2 and 1.5):

- "Is this file the one already loaded under its name?" — a size + sampled
  fingerprint said yes for a corrected file of the same size, and a floored
  MiB count said no for the very same file, which was then loaded again and
  again. The answer now: the same size and mtime it had when it was read (the
  very file left in place), or an equal full sha256.
- "Does a gzip's last 1 MB prove its whole content?" — only for a gzip of ONE
  member: its trailer carries the CRC-32 and length of everything. A multi-
  member gzip (bgzip, concatenated shards; valid, read transparently) carries
  a trailer per member, and two such files can match at both ends and in
  every sampled window while differing in the middle.

`ContentHasher` hashes a stream as it is written (the downloader feeds it
chunk by chunk) and records every offset where a gzip member header COULD
start (the 3-byte magic 1f 8b 08 with sane flag / XFL / OS bytes). At the end
each candidate is checked by inflating 64 KB from it: a real member start
inflates, a coincidental 1f 8b 08 inside compressed data almost never does.
Unknown (too many candidates, unreadable) is reported as None and treated as
"not provable" — the safe direction: the copy is downloaded in full and its
full sha decides.
"""

from __future__ import annotations

import hashlib
import zlib
from pathlib import Path

GZ_MAGIC = b"\x1f\x8b\x08"
_MAX_CANDIDATES = 256
_TRIAL_BYTES = 64 << 10

# Review 13 items 5.1 / 5.2.3: every local file is now hashed in full before
# its load — above 4 GB an inbox file kept size + mtime only, so a 5 GB twin
# under a second name, or the same bytes once via a link and once via the
# inbox, loaded twice. The limit is kept as a constant (an absurdly large one)
# so callers keep one test.
LOCAL_HASH_MAX_BYTES = 1 << 62

UTF8_BOM = b"\xef\xbb\xbf"


class _ContentHash:
    """sha256 of the CONTENT a file carries — the byte stream a parser reads:
    a gzip inflated (every member, as gzip reads them), a plain file as is, a
    leading UTF-8 BOM dropped (review 13 item 5.2.1: the same document
    re-gzipped at another level, saved decoded by a browser, or as .json vs
    .json.gz had two identities and loaded twice). Fed the RAW bytes chunk by
    chunk; a zip cannot be read front to back (its directory is at the end),
    so `zip` is True and the caller asks content_digest(path) instead."""

    def __init__(self) -> None:
        self.kind: str | None = None          # 'gz' | 'zip' | 'plain'
        self._sha = hashlib.sha256()
        self._d = None
        self._head = b""                      # until the BOM question is settled
        self._bom_done = False
        self.size = 0
        self.failed = False

    @property
    def zip(self) -> bool:
        return self.kind == "zip"

    def _out(self, data: bytes) -> None:
        if not data:
            return
        if not self._bom_done:
            self._head += data
            if len(self._head) < 3:
                return
            data = self._head[3:] if self._head.startswith(UTF8_BOM) else self._head
            self._head = b""
            self._bom_done = True
        self._sha.update(data)
        self.size += len(data)

    def update(self, chunk: bytes) -> None:
        if not chunk or self.failed or self.kind == "zip":
            return
        if self.kind is None:
            self.kind = ("gz" if chunk[:2] == b"\x1f\x8b" else
                         "zip" if chunk[:4] == b"PK\x03\x04" else "plain")
            if self.kind == "zip":
                return
            if self.kind == "gz":
                self._d = zlib.decompressobj(31)
        if self.kind == "plain":
            self._out(chunk)
            return
        try:
            data = chunk
            while data:
                self._out(self._d.decompress(data))
                if self._d.eof:
                    data = self._d.unused_data
                    if data and data[:2] == b"\x1f\x8b":
                        self._d = zlib.decompressobj(31)       # the next member
                    elif data.strip(b"\x00"):
                        self.failed = True                     # not a gzip after all
                        return
                    else:
                        data = b""
                else:
                    data = b""
        except zlib.error:
            self.failed = True

    def result(self) -> tuple[str | None, int | None]:
        if self.failed or self.kind in (None, "zip"):
            return None, None
        if not self._bom_done and self._head:
            data = self._head[3:] if self._head.startswith(UTF8_BOM) else self._head
            self._head = b""
            self._bom_done = True
            self._sha.update(data)
            self.size += len(data)
        return self._sha.hexdigest(), self.size


def content_digest(path: Path) -> tuple[str | None, int | None]:
    """(sha256, bytes) of the content a parser reads from `path` — through
    sniff.open_stream (the zip member it picks, the gzip inflated, the BOM
    dropped). One sequential read; (None, None) when it cannot be read."""
    from .sniff import open_stream
    h = hashlib.sha256()
    n = 0
    try:
        with open_stream(path) as s:
            for blk in iter(lambda: s.read(1 << 20), b""):
                h.update(blk)
                n += len(blk)
    except Exception:  # noqa: BLE001 — unreadable: no content identity
        return None, None
    return h.hexdigest(), n


class ContentHasher:
    """sha256 of everything fed, plus gzip member-start candidates."""

    def __init__(self, content: bool = True) -> None:
        self._sha = hashlib.sha256()
        # the content (inflated) identity rides on the same read (5.2.1)
        self.content = _ContentHash() if content else None
        self._pos = 0
        self._tail = b""            # last 2 bytes of the previous chunk
        self._gzip: bool | None = None
        self.candidates: list[int] = []
        self.overflow = False

    def update(self, chunk: bytes) -> None:
        if not chunk:
            return
        self._sha.update(chunk)
        if self.content is not None:
            self.content.update(chunk)
        if self._gzip is None:
            self._gzip = chunk[:2] == b"\x1f\x8b" if self._pos == 0 else False
        if self._gzip and not self.overflow:
            buf = self._tail + chunk
            base = self._pos - len(self._tail)
            i = buf.find(GZ_MAGIC)
            while i != -1:
                off = base + i
                if off > 0 and off not in self.candidates:
                    self.candidates.append(off)
                    if len(self.candidates) > _MAX_CANDIDATES:
                        self.overflow = True
                        break
                i = buf.find(GZ_MAGIC, i + 1)
            self._tail = buf[-2:]
        self._pos += len(chunk)

    def hexdigest(self) -> str:
        return self._sha.hexdigest()

    def content_identity(self, path: Path | None = None) -> tuple[str | None, int | None]:
        """(content sha, content bytes); a zip is read once more through its
        chosen member (content_digest) when `path` is given."""
        if self.content is None:
            return None, None
        if self.content.zip:
            return content_digest(path) if path is not None else (None, None)
        return self.content.result()

    def gz_members(self, path: Path) -> int | None:
        """1 (one member), 2 (more than one), or None (not a gzip / unknown)."""
        if not self._gzip:
            return None
        if self.overflow:
            return None
        try:
            with open(path, "rb") as f:
                for off in sorted(self.candidates):
                    f.seek(off)
                    if _is_member_start(f.read(_TRIAL_BYTES)):
                        return 2
        except OSError:
            return None
        return 1


def _is_member_start(data: bytes) -> bool:
    if len(data) < 18 or data[:3] != GZ_MAGIC:
        return False
    flg, xfl, os_ = data[3], data[8], data[9]
    if flg & 0xE0 or xfl not in (0, 2, 4) or (os_ > 13 and os_ != 255):
        return False
    try:
        d = zlib.decompressobj(31)
        out = d.decompress(data)
    except zlib.error:
        return False
    # a real member inflates to something (or ends cleanly inside the trial)
    return bool(out) or d.eof


def hash_file(path: Path) -> tuple[str, int | None]:
    """(sha256, gz_members) of a file on disk, in one sequential read."""
    h = ContentHasher()
    with open(path, "rb") as f:
        for blk in iter(lambda: f.read(1 << 20), b""):
            h.update(blk)
    return h.hexdigest(), h.gz_members(path)
