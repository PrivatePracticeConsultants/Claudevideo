"""Streaming MRF parsers: in-network rate files and standalone provider-reference files.

Same discipline as the sibling pipeline (single ijson event pass, constant
memory, early abandonment of non-target billing codes) plus:
- payer-agnostic: payer label comes from the file's reporting_entity_name
- v1.0 files ingest best-effort with schema_version recorded per row
- rate groups citing provider-reference ids that cannot be resolved (no
  embedded table, no ingested companion file) are COUNTED and surfaced,
  never silently dropped
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field

import ijson

from .config import DOLLAR_TYPES, MrfxConfig
from .sniff import open_stream
from .store import Store

log = logging.getLogger(__name__)

ACCEPTED_CODE_TYPES = {"CPT", "HCPCS"}


@dataclass
class ParseResult:
    payer: str = "Unknown payer"
    schema_version: str | None = None
    last_updated_on: str | None = None
    rows: list[dict] = field(default_factory=list)
    items_seen: int = 0
    items_matched: int = 0
    ref_groups_skipped: int = 0
    embedded_refs: dict[int, tuple[frozenset[str], frozenset[str]]] = field(default_factory=dict)


def _providers_from_groups(provider_groups: list[dict]) -> tuple[frozenset[str], frozenset[str]]:
    npis: set[str] = set()
    tins: set[str] = set()
    for pg in provider_groups or []:
        for npi in pg.get("npi", []) or []:
            npis.add(str(npi))
        tin = pg.get("tin") or {}
        value = str(tin.get("value", "")).replace("-", "")
        if value and tin.get("type") == "ein":
            tins.add(value)
    return frozenset(npis), frozenset(tins)


class _Discarded:
    def event(self, event, value) -> None:
        pass

    value: dict = {}


_DISCARDED = _Discarded()


@dataclass
class _DeferredGroup:
    billing_code: str
    billing_code_type: str
    npis: frozenset[str]
    tins: frozenset[str]
    ref_ids: list[int]
    prices: list[dict]


class InNetworkParser:
    """One streaming pass over an in-network file."""

    def __init__(
        self,
        cfg: MrfxConfig,
        source_file: str,
        external_refs: dict[int, tuple[frozenset[str], frozenset[str]]] | None = None,
    ):
        self.cfg = cfg
        self.code_set = cfg.code_set  # None = all codes
        self.source_file = source_file
        self.external_refs = external_refs or {}
        self.result = ParseResult()
        self._refs_complete = False
        self._deferred: list[_DeferredGroup] = []
        self._ingested_at = dt.datetime.now(dt.timezone.utc).isoformat()

    def parse(self, stream) -> ParseResult:
        r = self.result
        builder = None
        building = None
        skipping = False

        for prefix, event, value in ijson.parse(stream, use_float=True):
            if builder is not None:
                if building == "item" and not skipping and event == "string":
                    if (
                        prefix == "in_network.item.billing_code"
                        and self.code_set is not None
                        and value not in self.code_set
                    ) or (
                        prefix == "in_network.item.billing_code_type"
                        and value not in ACCEPTED_CODE_TYPES
                    ):
                        skipping = True
                        builder = _DISCARDED
                end_prefix = "in_network.item" if building == "item" else "provider_references.item"
                if not skipping:
                    builder.event(event, value)
                if event == "end_map" and prefix == end_prefix:
                    if building == "ref":
                        self._add_ref(builder.value)
                    elif not skipping:
                        self._handle_item(builder.value)
                    if building == "item":
                        r.items_seen += 1
                    builder = None
                    building = None
                    skipping = False
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
                self._refs_complete = True
            elif event == "string" and prefix in ("reporting_entity_name", "version", "last_updated_on"):
                if prefix == "reporting_entity_name":
                    r.payer = self.cfg.normalize_payer(value)
                elif prefix == "version":
                    r.schema_version = value
                else:
                    r.last_updated_on = value

        self._refs_complete = True
        for dg in self._deferred:
            self._emit_group(dg.billing_code, dg.billing_code_type, dg.npis, dg.tins, dg.ref_ids, dg.prices)
        return r

    # -- refs -----------------------------------------------------------------

    def _add_ref(self, entry: dict) -> None:
        rid = entry.get("provider_group_id")
        if rid is None:
            return
        groups = entry.get("provider_groups")
        if groups is None and entry.get("location"):
            # this app is file-driven: remote reference locations are not
            # fetched; the user must drop the companion file in.
            self.result.embedded_refs.setdefault(int(rid), (frozenset(), frozenset()))
            return
        self.result.embedded_refs[int(rid)] = _providers_from_groups(groups or [])

    def _lookup_ref(self, rid: int) -> tuple[frozenset[str], frozenset[str]] | None:
        got = self.result.embedded_refs.get(rid)
        if got is not None and (got[0] or got[1]):
            return got
        # embedded-but-empty means a location-style entry we don't fetch:
        # fall through to a standalone reference file if one was ingested.
        return self.external_refs.get(rid)

    # -- items ----------------------------------------------------------------

    def _handle_item(self, item: dict) -> None:
        code = str(item.get("billing_code", ""))
        code_type = str(item.get("billing_code_type", ""))
        if self.code_set is not None and code not in self.code_set:
            return
        if code_type not in ACCEPTED_CODE_TYPES:
            return
        self.result.items_matched += 1
        for grp in item.get("negotiated_rates", []) or []:
            npis, tins = _providers_from_groups(grp.get("provider_groups", []) or [])
            ref_ids = [int(x) for x in (grp.get("provider_references") or [])]
            prices = grp.get("negotiated_prices", []) or []
            if not prices:
                continue
            if not self._refs_complete and any(
                rid not in self.result.embedded_refs and rid not in self.external_refs
                for rid in ref_ids
            ):
                self._deferred.append(_DeferredGroup(code, code_type, npis, tins, ref_ids, prices))
                continue
            self._emit_group(code, code_type, npis, tins, ref_ids, prices)

    def _emit_group(self, code, code_type, npis, tins, ref_ids, prices) -> None:
        npis = set(npis)
        tins = set(tins)
        missing_ref = False
        for rid in ref_ids:
            got = self._lookup_ref(rid)
            if got is None:
                missing_ref = True
                continue
            npis |= got[0]
            tins |= got[1]
        if missing_ref and not npis:
            self.result.ref_groups_skipped += 1
            return
        if not npis:
            return  # nothing attributable (empty group)
        tin_val = next(iter(sorted(tins)), None)
        for price in prices:
            modifiers = [str(m) for m in (price.get("billing_code_modifier") or [])]
            ntype = str(price.get("negotiated_type", "") or "")
            row_base = {
                "payer": self.result.payer,
                "source_file": self.source_file,
                "schema_version": self.result.schema_version,
                "last_updated_on": self.result.last_updated_on,
                "tin": tin_val,
                "billing_code": code,
                "billing_code_type": code_type,
                "billing_code_modifier": modifiers,
                "negotiated_rate": float(price.get("negotiated_rate", 0.0)),
                "negotiated_type": ntype,
                "is_dollar_rate": ntype in DOLLAR_TYPES,
                "billing_class": str(price.get("billing_class", "") or ""),
                "service_code": [str(s) for s in (price.get("service_code") or [])],
                "expiration_date": str(price.get("expiration_date", "") or ""),
                "ingested_at": self._ingested_at,
            }
            for npi in sorted(npis):
                self.result.rows.append({**row_base, "npi": npi})


def parse_provider_reference_file(cfg: MrfxConfig, stream) -> tuple[str, str | None, dict]:
    """Standalone provider-reference file -> (payer, last_updated_on, {ref_id: (npis, tins)})."""
    payer = "Unknown payer"
    last_updated = None
    refs: dict[int, tuple[list[str], list[str]]] = {}
    builder = None
    for prefix, event, value in ijson.parse(stream, use_float=True):
        if builder is not None:
            builder.event(event, value)
            if event == "end_map" and prefix == "provider_references.item":
                entry = builder.value
                rid = entry.get("provider_group_id")
                if rid is not None:
                    npis, tins = _providers_from_groups(entry.get("provider_groups") or [])
                    refs[int(rid)] = (sorted(npis), sorted(tins))
                builder = None
            continue
        if event == "start_map" and prefix == "provider_references.item":
            builder = ijson.ObjectBuilder()
            builder.event(event, value)
        elif event == "string" and prefix == "reporting_entity_name":
            payer = cfg.normalize_payer(value)
        elif event == "string" and prefix == "last_updated_on":
            last_updated = value
    return payer, last_updated, refs
