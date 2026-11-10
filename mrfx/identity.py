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

# local files are hashed after their ingest up to this size: a larger local
# drop keeps size + mtime identity only (re-reading tens of GB just to name
# it is not worth it; a URL download is hashed while it downloads anyway)
LOCAL_HASH_MAX_BYTES = 4 << 30


class ContentHasher:
    """sha256 of everything fed, plus gzip member-start candidates."""

    def __init__(self) -> None:
        self._sha = hashlib.sha256()
        self._pos = 0
        self._tail = b""            # last 2 bytes of the previous chunk
        self._gzip: bool | None = None
        self.candidates: list[int] = []
        self.overflow = False

    def update(self, chunk: bytes) -> None:
        if not chunk:
            return
        self._sha.update(chunk)
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
