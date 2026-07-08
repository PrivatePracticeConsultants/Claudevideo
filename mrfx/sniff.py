"""File identification + preflight: classify a dropped file from its header only.

Reads at most ~1 MB of the *decompressed* stream — never the whole file — and
reports type, payer, schema version, publication date, size estimates, and the
companion-file (provider-reference) verdict.
"""

from __future__ import annotations

import gzip
import io
import json
import logging
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

import ijson

from .config import MrfxConfig
from .store import Store

log = logging.getLogger(__name__)

HEADER_BYTES = 1 << 20  # ~1 MB decompressed
SNIFF_BYTES = 64 << 10  # first pass per spec §3.1

# rough streaming throughput assumption for the parse-time estimate
PARSE_MB_PER_SEC = 40.0

FILE_TYPES = ("in_network", "provider_reference", "toc", "unknown")
VERDICTS = ("READY", "NEEDS COMPANION", "NOT A RATE FILE", "UNREADABLE")


@dataclass
class Preflight:
    filename: str
    file_type: str = "unknown"
    payer: str | None = None
    reporting_entity_name: str | None = None
    schema_version: str | None = None
    last_updated_on: str | None = None
    compressed_bytes: int = 0
    est_uncompressed_bytes: int | None = None
    est_parse_seconds: int | None = None
    uses_provider_references: bool = False
    has_inline_reference_groups: bool = False
    companion_present: bool | None = None
    verdict: str = "UNREADABLE"
    messages: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {k: getattr(self, k) for k in self.__dataclass_fields__}


class _CountingRaw(io.RawIOBase):
    """Wrap the raw (compressed) file to report cumulative bytes read — drives
    an accurate progress bar off the exact compressed size, independent of the
    uncompressed-size estimate."""

    def __init__(self, fh, cb):
        self._fh = fh
        self._cb = cb
        self._n = 0

    def readable(self) -> bool:
        return True

    def readinto(self, b) -> int:
        data = self._fh.read(len(b))
        if data:
            b[: len(data)] = data
            self._n += len(data)
            self._cb(self._n)
        return len(data)

    def close(self):
        try:
            self._fh.close()
        finally:
            super().close()


def open_stream(path: Path, progress_cb=None) -> io.BufferedIOBase:
    """Binary stream for .json / .json.gz / .zip (first json member).

    progress_cb(compressed_bytes_read) is called as the underlying compressed
    bytes are consumed, so callers can render a progress bar."""
    raw: io.BufferedIOBase = open(path, "rb")
    head = raw.peek(4)[:4] if hasattr(raw, "peek") else raw.read(4)
    if progress_cb is not None:
        raw = io.BufferedReader(_CountingRaw(raw, progress_cb), buffer_size=1 << 20)
    if head[:2] == b"\x1f\x8b":
        return gzip.GzipFile(fileobj=raw)  # type: ignore[return-value]
    if head[:4] == b"PK\x03\x04":
        zf = zipfile.ZipFile(raw)
        members = [m for m in zf.namelist() if m.lower().endswith((".json", ".json.gz"))]
        if not members:
            raw.close()
            raise ValueError("zip archive contains no .json member")
        inner = zf.open(members[0])
        if members[0].lower().endswith(".gz"):
            return gzip.GzipFile(fileobj=inner)  # type: ignore[return-value]
        return inner  # type: ignore[return-value]
    return io.BufferedReader(raw, buffer_size=1 << 20) if not isinstance(raw, io.BufferedReader) else raw


def _estimate_uncompressed(path: Path, compressed: int) -> int | None:
    suffix = path.name.lower()
    if suffix.endswith(".gz") or suffix.endswith(".zip"):
        # MRF json compresses very well; ~8-12x is typical. Use 10x.
        return compressed * 10
    return compressed


def _scan_header(stream: io.BufferedIOBase, pf: Preflight) -> None:
    """Walk parse events over the first ~1 MB; stop early once classified."""
    limited = io.BytesIO(stream.read(HEADER_BYTES))
    top_keys: list[str] = []
    truncated = False  # did the document run past the 1 MB window?
    try:
        for prefix, event, value in ijson.parse(limited):
            if prefix == "" and event == "map_key":
                top_keys.append(value)
            elif prefix in ("reporting_entity_name", "version", "last_updated_on") and event == "string":
                setattr(pf, {"reporting_entity_name": "reporting_entity_name",
                             "version": "schema_version",
                             "last_updated_on": "last_updated_on"}[prefix], value)
            elif prefix == "provider_references.item.provider_groups" and event == "start_array":
                pf.has_inline_reference_groups = True
            # stop once we've started into a big payload array — the header is behind us
            if prefix in ("in_network.item", "reporting_structure.item") and event == "end_map":
                break
    except ijson.JSONError:
        # the 1 MB window sliced mid-token: the real document is larger than
        # what we read (IncompleteJSONError subclasses JSONError). Keys seen so
        # far stand, but we know there is MORE structure we haven't reached.
        truncated = True
    pf._top_keys = top_keys  # type: ignore[attr-defined]

    if "in_network" in top_keys:
        pf.file_type = "in_network"
        pf.uses_provider_references = "provider_references" in top_keys
    elif "provider_groups" in top_keys:
        # CMS standalone provider-reference file: top-level `provider_groups`.
        pf.file_type = "provider_reference"
    elif "provider_references" in top_keys:
        # Top-level `provider_references` is the EMBEDDED reference table of an
        # in-network file, not a standalone reference file. If the read was
        # truncated we simply haven't reached the `in_network` key yet (a large
        # embedded ref table precedes it) — treat it as an in-network file so
        # its rates are not dropped. Only when the whole document fit in the
        # window with no `in_network` is it genuinely reference-only.
        if truncated:
            pf.file_type = "in_network"
            pf.uses_provider_references = True
            pf.messages.append(
                "large embedded provider_references table precedes in_network; "
                "classified as a rate file (the in_network key is past the 1 MB header window)"
            )
        else:
            pf.file_type = "provider_reference"
    elif "reporting_structure" in top_keys:
        pf.file_type = "toc"
    elif top_keys:
        pf.file_type = "unknown"


def _detect_refs_deep(path: Path, pf: Preflight) -> None:
    """Header showed in_network but no top-level provider_references (yet).

    References can trail the in_network array, and rate groups can cite ids
    either way — sample the first in_network item for `provider_references`
    usage without reading beyond the header window.
    """
    try:
        with open_stream(path) as stream:
            limited = io.BytesIO(stream.read(HEADER_BYTES))
            for prefix, event, _ in ijson.parse(limited):
                if prefix == "in_network.item.negotiated_rates.item.provider_references" and event == "start_array":
                    pf.uses_provider_references = True
                    return
                if prefix == "in_network.item" and event == "end_map":
                    return
    except (ijson.JSONError, OSError, EOFError):
        pass


def preflight(path: Path, cfg: MrfxConfig, store: Store | None = None) -> Preflight:
    pf = Preflight(filename=path.name)
    try:
        pf.compressed_bytes = path.stat().st_size
    except OSError as e:
        pf.messages.append(f"cannot stat file: {e}")
        return pf
    pf.est_uncompressed_bytes = _estimate_uncompressed(path, pf.compressed_bytes)
    if pf.est_uncompressed_bytes:
        pf.est_parse_seconds = int(pf.est_uncompressed_bytes / (PARSE_MB_PER_SEC * 1e6)) + 1

    try:
        with open_stream(path) as stream:
            _scan_header(stream, pf)
    except Exception as e:  # noqa: BLE001 — preflight must never blow up on a bad file
        pf.messages.append(f"unreadable: {type(e).__name__}: {e}")
        pf.verdict = "UNREADABLE"
        return pf

    if pf.reporting_entity_name:
        pf.payer = cfg.normalize_payer(pf.reporting_entity_name)

    if pf.schema_version and not str(pf.schema_version).startswith("2."):
        pf.messages.append(
            f"schema version {pf.schema_version} (expected 2.x) — will ingest best-effort"
        )

    if pf.file_type == "toc":
        pf.verdict = "NOT A RATE FILE"
        pf.messages.append(
            "This is an index/TOC file, not a rate file; drop the in-network "
            "files it references (see its in_network_files[].location URLs)."
        )
        return pf
    if pf.file_type == "provider_reference":
        pf.verdict = "NOT A RATE FILE"
        pf.messages.append(
            "Standalone provider-reference file — it carries NPIs, not rates. "
            "Ingesting it fills the reference store so in-network files from "
            f"{pf.payer or 'this payer'} can resolve their provider groups."
        )
        return pf
    if pf.file_type == "unknown":
        pf.verdict = "UNREADABLE"
        pf.messages.append("no in_network / provider_references / reporting_structure key found")
        return pf

    # in-network file: companion check
    if not pf.uses_provider_references:
        _detect_refs_deep(path, pf)

    if not pf.uses_provider_references:
        pf.verdict = "READY"
        pf.messages.append("Inline provider_groups only — self-contained.")
        return pf

    if pf.has_inline_reference_groups:
        # top-level provider_references with embedded provider_groups: the file
        # is its own companion.
        pf.companion_present = True
        pf.verdict = "READY"
        pf.messages.append("Uses provider references; reference table is embedded in this file.")
        return pf

    companion = False
    if store is not None and pf.payer:
        companion = store.has_provider_refs(pf.payer, pf.last_updated_on) or store.has_provider_refs(pf.payer)
    if not companion:
        # also scan the inbox for a matching reference file waiting to ingest
        companion = _companion_in_inbox(cfg, pf)
    pf.companion_present = companion
    if companion:
        pf.verdict = "READY"
        pf.messages.append("Uses provider references; a matching reference file is present.")
    else:
        pf.verdict = "NEEDS COMPANION"
        pf.messages.append(
            "This file uses provider references. You must also drop in the matching "
            f"provider-reference file (same payer{' , same last_updated_on ' + pf.last_updated_on if pf.last_updated_on else ''}) "
            "or NPIs will be missing."
        )
    return pf


def _companion_in_inbox(cfg: MrfxConfig, pf: Preflight) -> bool:
    for p in sorted(cfg.inbox_dir.glob("*")):
        if p.name == pf.filename or not p.is_file():
            continue
        try:
            other = Preflight(filename=p.name)
            with open_stream(p) as stream:
                _scan_header(stream, other)
        except (OSError, EOFError, ValueError, zipfile.BadZipFile, gzip.BadGzipFile):
            continue
        if other.file_type == "provider_reference":
            other_payer = cfg.normalize_payer(other.reporting_entity_name or "")
            if pf.payer and other_payer == pf.payer:
                if not pf.last_updated_on or not other.last_updated_on or pf.last_updated_on == other.last_updated_on:
                    return True
    return False


def format_preflight(pf: Preflight) -> str:
    def size(n):
        if n is None:
            return "?"
        for unit in ("B", "KB", "MB", "GB", "TB"):
            if n < 1024:
                return f"{n:.1f} {unit}"
            n /= 1024
        return f"{n:.1f} PB"

    lines = [
        f"file:             {pf.filename}",
        f"type:             {pf.file_type}",
        f"payer:            {pf.payer or '?'} (reporting_entity_name: {pf.reporting_entity_name or '?'})",
        f"schema version:   {pf.schema_version or '?'}" + ("  ⚠ not 2.x" if pf.schema_version and not str(pf.schema_version).startswith("2.") else ""),
        f"last_updated_on:  {pf.last_updated_on or '?'}",
        f"size:             {size(pf.compressed_bytes)} compressed, ~{size(pf.est_uncompressed_bytes)} uncompressed",
        f"est. parse time:  ~{pf.est_parse_seconds}s" if pf.est_parse_seconds else "est. parse time:  ?",
        f"provider refs:    {'yes' if pf.uses_provider_references else 'no (inline provider_groups)'}"
        + ("" if not pf.uses_provider_references else f", companion present: {pf.companion_present}"),
        "",
        f"VERDICT: {pf.verdict}",
    ]
    lines += [f"  - {m}" for m in pf.messages]
    return "\n".join(lines)
