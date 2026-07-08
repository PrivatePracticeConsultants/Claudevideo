"""Streaming in-network MRF parser + NPI/CPT filter (the core of the pipeline).

Design constraints (see spec):
- Never materialize an in-network file: single streaming pass over ijson
  events, gzip-aware, bounded memory.
- `provider_references` are first-class. They usually precede `in_network` in
  the payload; when they don't, rate groups that cite unresolved reference ids
  are deferred (only for target billing codes, so the buffer stays tiny) and
  resolved at end-of-file.
- Per-file checkpointing: completed files are skipped on re-run; 403/429/
  truncated-gzip mark the file failed and the batch continues.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import ijson

from .config import Config
from .http_util import FetchError, open_json_stream
from .npi_resolver import TargetSet
from .provider_refs import ProviderRefIndex, _parse_provider_groups
from .toc import SUPPORTED_SCHEMA_MAJOR, SourceFile
from .writer import RecordWriter

log = logging.getLogger(__name__)

ACCEPTED_CODE_TYPES = {"CPT", "HCPCS"}  # G0283 in the default set is HCPCS


class UnsupportedSchema(Exception):
    pass


@dataclass
class FileStats:
    items_seen: int = 0
    items_matched_code: int = 0
    rows: int = 0
    deferred_groups: int = 0


@dataclass
class DeferredGroup:
    """A negotiated_rates group citing reference ids not yet streamed past."""

    billing_code: str
    billing_code_type: str
    npis: frozenset[str]
    tins: frozenset[str]
    ref_ids: list[int]
    prices: list[dict]


# ---------------------------------------------------------------------------
# checkpoints
# ---------------------------------------------------------------------------


def _url_key(url: str) -> str:
    return hashlib.sha1(url.split("?", 1)[0].encode()).hexdigest()[:16]


class CheckpointStore:
    def __init__(self, raw_dir: Path):
        self.dir = raw_dir / "checkpoints"
        self.dir.mkdir(parents=True, exist_ok=True)

    def _path(self, url: str) -> Path:
        return self.dir / f"{_url_key(url)}.json"

    def status(self, url: str) -> str | None:
        p = self._path(url)
        if not p.exists():
            return None
        try:
            return json.loads(p.read_text()).get("status")
        except json.JSONDecodeError:
            return None

    def mark(self, url: str, status: str, **extra) -> None:
        payload = {
            "url": url.split("?", 1)[0],
            "status": status,
            "finished_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            **extra,
        }
        self._path(url).write_text(json.dumps(payload, indent=1))


# ---------------------------------------------------------------------------
# matching helpers
# ---------------------------------------------------------------------------


def _match_group(
    npis: frozenset[str], tins: frozenset[str], targets: TargetSet
) -> list[tuple[str, str, str | None]]:
    """Match one provider set against targets: NPI first, TIN second.

    Returns (npi, org_name, tin) rows. On a TIN-only match every NPI billing
    under that TIN is emitted so the rate stays attributable.
    """
    tin_val = next(iter(tins), None)
    hits = npis & targets.npis
    if hits:
        return [(n, targets.npi_to_org[n], tin_val) for n in sorted(hits)]
    tin_hit = next((t for t in tins if t in targets.tins), None)
    if tin_hit:
        org = targets.tin_to_org[tin_hit]
        return [(n, org, tin_hit) for n in sorted(npis)]
    return []


def _price_fields(price: dict) -> dict:
    modifiers = price.get("billing_code_modifier") or []
    return {
        "billing_code_modifier": "|".join(str(m) for m in modifiers),
        "negotiated_rate": float(price.get("negotiated_rate", 0.0)),
        "negotiated_type": price.get("negotiated_type", "") or "",
        "billing_class": price.get("billing_class", "") or "",
        "service_code": [str(s) for s in (price.get("service_code") or [])],
    }


# ---------------------------------------------------------------------------
# the streaming pass
# ---------------------------------------------------------------------------


class _InNetworkScanner:
    """Incremental builder over ijson events for one in-network file.

    Builds `provider_references.item` and `in_network.item` objects as they
    stream past. In-network items are abandoned early (stop accumulating, skip
    to the item's end) as soon as a non-target `billing_code` /
    `billing_code_type` is seen, so non-target items cost almost nothing.
    """

    def __init__(self, extractor: "Extractor", source: SourceFile, stats: FileStats):
        self.ex = extractor
        self.source = source
        self.stats = stats
        self.header: dict[str, str] = {}
        self.refs = ProviderRefIndex(client=extractor.client, http_cfg=extractor.cfg.http)
        self.deferred: list[DeferredGroup] = []
        self.rows: list[dict] = []

    def scan(self, stream) -> None:
        builder = None
        building = None  # 'ref' | 'item'
        skipping_item = False

        for prefix, event, value in ijson.parse(stream, use_float=True):
            if builder is not None:
                if building == "item" and not skipping_item and event == "string":
                    if prefix == "in_network.item.billing_code" and value not in self.ex.code_set:
                        skipping_item = True
                    elif (
                        prefix == "in_network.item.billing_code_type"
                        and value not in ACCEPTED_CODE_TYPES
                    ):
                        skipping_item = True
                    if skipping_item:
                        builder = _DISCARDED
                end_prefix = "in_network.item" if building == "item" else "provider_references.item"
                if not skipping_item:
                    builder.event(event, value)
                if event == "end_map" and prefix == end_prefix:
                    if building == "ref":
                        self.refs.add_entry(builder.value)
                    elif not skipping_item:
                        self._handle_item(builder.value)
                    if building == "item":
                        self.stats.items_seen += 1
                    builder = None
                    building = None
                    skipping_item = False
                continue

            if event == "start_map" and prefix == "in_network.item":
                builder = ijson.ObjectBuilder()
                builder.event(event, value)
                building = "item"
            elif event == "start_map" and prefix == "provider_references.item":
                builder = ijson.ObjectBuilder()
                builder.event(event, value)
                building = "ref"
            elif event == "end_array" and prefix == "provider_references":
                self.refs.complete = True
            elif prefix in ("last_updated_on", "version", "reporting_entity_name") and event == "string":
                self.header[prefix] = value
                if prefix == "version" and not str(value).startswith(f"{SUPPORTED_SCHEMA_MAJOR}."):
                    raise UnsupportedSchema(str(value))

        # EOF: the reference array (if any) is fully known now.
        self.refs.complete = True
        for dg in self.deferred:
            self._emit_group(
                dg.billing_code, dg.billing_code_type, dg.npis, dg.tins, dg.ref_ids, dg.prices
            )

    def _handle_item(self, item: dict) -> None:
        code = str(item.get("billing_code", ""))
        code_type = str(item.get("billing_code_type", ""))
        if code not in self.ex.code_set or code_type not in ACCEPTED_CODE_TYPES:
            return  # key-order edge case: billing_code streamed after other fields
        self.stats.items_matched_code += 1
        for grp in item.get("negotiated_rates", []) or []:
            inline = _parse_provider_groups(grp.get("provider_groups", []) or [])
            ref_ids = [int(r) for r in (grp.get("provider_references") or [])]
            prices = grp.get("negotiated_prices", []) or []
            if not prices:
                continue
            unresolved = [r for r in ref_ids if self.refs.get(r) is None]
            if unresolved:
                self.deferred.append(
                    DeferredGroup(code, code_type, inline.npis, inline.tins, ref_ids, prices)
                )
                self.stats.deferred_groups += 1
                continue
            self._emit_group(code, code_type, inline.npis, inline.tins, ref_ids, prices)

    def _emit_group(
        self,
        code: str,
        code_type: str,
        inline_npis: frozenset[str],
        inline_tins: frozenset[str],
        ref_ids: list[int],
        prices: list[dict],
    ) -> None:
        npis = set(inline_npis)
        tins = set(inline_tins)
        for rid in ref_ids:
            rg = self.refs.get(rid)
            if rg is not None:
                npis |= rg.npis
                tins |= rg.tins
        matches = _match_group(frozenset(npis), frozenset(tins), self.ex.targets)
        if not matches:
            return
        now = dt.datetime.now(dt.timezone.utc).isoformat()
        for price in prices:
            pf = _price_fields(price)
            for npi, org, tin in matches:
                self.rows.append(
                    {
                        "payer": self.source.payer,
                        "source_file_url": self.source.url.split("?", 1)[0],
                        "last_updated_on": self.header.get("last_updated_on", ""),
                        "npi": npi,
                        "tin": tin,
                        "org_name": org,
                        "billing_code": code,
                        "billing_code_type": code_type,
                        **pf,
                        "plan_name": self.source.plan_name,
                        "plan_id": self.source.plan_id,
                        "extracted_at": now,
                    }
                )
                self.stats.rows += 1


class _Discarded:
    """Sink for events of an item we've decided to skip."""

    def event(self, event, value) -> None:
        pass

    value: dict = {}


_DISCARDED = _Discarded()


# ---------------------------------------------------------------------------
# orchestration
# ---------------------------------------------------------------------------


@dataclass
class Extractor:
    cfg: Config
    targets: TargetSet
    client: httpx.Client
    writer: RecordWriter
    checkpoints: CheckpointStore = field(init=False)

    def __post_init__(self) -> None:
        self.code_set = self.cfg.billing_code_set
        self.checkpoints = CheckpointStore(self.cfg.paths.raw_dir)

    def extract_all(self, sources: list[SourceFile], retry_failed: bool = False) -> dict:
        summary = {"done": 0, "skipped_checkpoint": 0, "failed": 0, "skipped_schema": 0, "rows": 0}
        for i, source in enumerate(sources, 1):
            status = self.checkpoints.status(source.url)
            if status == "done" or status == "skipped_schema" or (status == "failed" and not retry_failed):
                summary["skipped_checkpoint"] += 1
                continue
            log.info("[%d/%d] extracting %s", i, len(sources), source.url.split("?")[0])
            result = self.extract_source(source)
            summary[result["status"]] = summary.get(result["status"], 0) + 1
            summary["rows"] += result.get("rows", 0)
        return summary

    def extract_source(self, source: SourceFile) -> dict:
        stats = FileStats()
        scanner = _InNetworkScanner(self, source, stats)
        try:
            with open_json_stream(source.url, self.client, self.cfg.http) as stream:
                scanner.scan(stream)
        except UnsupportedSchema as e:
            log.warning("%s declares schema %s (need %d.x) — skipping", source.url.split("?")[0], e, SUPPORTED_SCHEMA_MAJOR)
            self.checkpoints.mark(source.url, "skipped_schema", version=str(e))
            return {"status": "skipped_schema"}
        except FetchError as e:
            level = logging.WARNING if e.status in (403, 429) else logging.ERROR
            log.log(level, "fetch failed (HTTP %s) for %s — continuing batch", e.status, source.url.split("?")[0])
            self.checkpoints.mark(source.url, "failed", error=f"http {e.status}")
            return {"status": "failed"}
        except (EOFError, OSError, ijson.JSONError, httpx.HTTPError) as e:
            # truncated gzip, dropped connection, malformed JSON, ...
            log.error("stream error for %s: %s — continuing batch", source.url.split("?")[0], e)
            self.checkpoints.mark(source.url, "failed", error=f"{type(e).__name__}: {e}")
            return {"status": "failed"}

        self.writer.write(source, scanner.rows)
        self.checkpoints.mark(
            source.url,
            "done",
            rows=stats.rows,
            items_seen=stats.items_seen,
            items_matched_code=stats.items_matched_code,
            deferred_groups=stats.deferred_groups,
        )
        log.info(
            "done %s: %d items scanned, %d target-code items, %d rows (%d groups deferred)",
            source.url.split("?")[0].rsplit("/", 1)[-1],
            stats.items_seen,
            stats.items_matched_code,
            stats.rows,
            stats.deferred_groups,
        )
        return {"status": "done", "rows": stats.rows}
