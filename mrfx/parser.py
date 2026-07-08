"""Streaming MRF parsers: in-network rate files and standalone provider-reference files.

Same discipline as the sibling pipeline (single ijson event pass, constant
memory, early abandonment of non-target billing codes) plus the V4 grain rules:

- **NPI↔TIN pairing is preserved per provider group** — each
  `{npi: [...], tin: {type, value}}` group emits its own rows, so a TIN never
  absorbs NPIs from a sibling group.
- `tin.type == "npi"` means the "TIN" is really an NPI, not an employer tax id:
  those rows are flagged `tin_is_really_npi` (§3.3/§7A.7), never merged into
  entity rollups by default.
- discipline resolves modifier-first (GP/GO/GN), code second, "unspecified"
  for unmodified shared codes; `is_timed` from the static catalog.
- per-file QA counters feed the data-quality report (§7A.3).
"""

from __future__ import annotations

import datetime as dt
import logging
import re
from dataclasses import dataclass, field

import ijson

from .catalog import is_timed, resolve_discipline
from .config import DOLLAR_TYPES, MrfxConfig
from .sniff import open_stream  # noqa: F401  (re-exported for ingest)

log = logging.getLogger(__name__)

ACCEPTED_CODE_TYPES = {"CPT", "HCPCS"}

# one provider group: (tin_value, tin_type, npis)
PGroup = tuple[str | None, str | None, tuple[str, ...]]


@dataclass
class QaCounters:
    billing_type_other: int = 0       # non-CPT/HCPCS billing_code_type items
    multi_code_fields: int = 0        # multiple codes crammed into one billing_code
    zero_rates: int = 0               # $0.00 / $0.01 placeholder prices
    non_dollar_rows: int = 0          # percentage / per diem rows emitted
    rows: int = 0

    def to_dict(self) -> dict:
        return dict(self.__dict__)


@dataclass
class ParseResult:
    payer: str = "Unknown payer"
    schema_version: str | None = None
    last_updated_on: str | None = None
    rows: list[dict] = field(default_factory=list)
    items_seen: int = 0
    items_matched: int = 0
    ref_groups_skipped: int = 0
    embedded_refs: dict[int, list[PGroup]] = field(default_factory=dict)
    qa: QaCounters = field(default_factory=QaCounters)

    @property
    def file_month(self) -> str:
        if self.last_updated_on and re.match(r"\d{4}-\d{2}", self.last_updated_on):
            return self.last_updated_on[:7]
        return dt.date.today().strftime("%Y-%m")


def parse_provider_group(pg: dict) -> PGroup:
    npis = tuple(str(n) for n in (pg.get("npi") or []))
    tin = pg.get("tin") or {}
    tin_type = (tin.get("type") or "").lower() or None
    value = str(tin.get("value", "")).strip()
    if tin_type == "ein":
        value = value.replace("-", "")
    return (value or None, tin_type, npis)


def parse_groups(provider_groups: list[dict]) -> list[PGroup]:
    return [parse_provider_group(pg) for pg in provider_groups or []]


MULTI_CODE_RE = re.compile(r"[,\s;/]")


class _Discarded:
    def event(self, event, value) -> None:
        pass

    value: dict = {}


_DISCARDED = _Discarded()


@dataclass
class _DeferredGroup:
    billing_code: str
    billing_code_type: str
    groups: list[PGroup]
    ref_ids: list[int]
    prices: list[dict]


class InNetworkParser:
    """One streaming pass over an in-network file."""

    def __init__(
        self,
        cfg: MrfxConfig,
        source_file: str,
        external_refs: dict[int, list[PGroup]] | None = None,
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
                    if prefix == "in_network.item.billing_code":
                        if self.code_set is not None and value not in self.code_set:
                            skipping = True
                            if MULTI_CODE_RE.search(value.strip()):
                                r.qa.multi_code_fields += 1
                    elif prefix == "in_network.item.billing_code_type" and value not in ACCEPTED_CODE_TYPES:
                        r.qa.billing_type_other += 1
                        skipping = True
                    if skipping:
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
            self._emit_group(dg.billing_code, dg.billing_code_type, dg.groups, dg.ref_ids, dg.prices)
        return r

    # -- refs -----------------------------------------------------------------

    def _add_ref(self, entry: dict) -> None:
        rid = entry.get("provider_group_id")
        if rid is None:
            return
        groups = entry.get("provider_groups")
        if groups is None and entry.get("location"):
            # file-driven app: remote reference locations are not fetched; the
            # user must drop the companion file in.
            self.result.embedded_refs.setdefault(int(rid), [])
            return
        self.result.embedded_refs[int(rid)] = parse_groups(groups or [])

    def _lookup_ref(self, rid: int) -> list[PGroup] | None:
        got = self.result.embedded_refs.get(rid)
        if got:
            return got
        # embedded-but-empty (location-style) falls through to a standalone
        # reference file if one was ingested for this payer.
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
            groups = parse_groups(grp.get("provider_groups", []) or [])
            ref_ids = [int(x) for x in (grp.get("provider_references") or [])]
            prices = grp.get("negotiated_prices", []) or []
            if not prices:
                continue
            if not self._refs_complete and any(
                rid not in self.result.embedded_refs and rid not in self.external_refs
                for rid in ref_ids
            ):
                self._deferred.append(_DeferredGroup(code, code_type, groups, ref_ids, prices))
                continue
            self._emit_group(code, code_type, groups, ref_ids, prices)

    def _emit_group(
        self,
        code: str,
        code_type: str,
        groups: list[PGroup],
        ref_ids: list[int],
        prices: list[dict],
    ) -> None:
        all_groups = list(groups)
        missing_ref = False
        for rid in ref_ids:
            got = self._lookup_ref(rid)
            if got is None or got == []:
                missing_ref = True
                continue
            all_groups.extend(got)
        if missing_ref and not all_groups:
            self.result.ref_groups_skipped += 1
            return
        if not all_groups:
            return

        r = self.result
        for price in prices:
            modifiers = [str(m) for m in (price.get("billing_code_modifier") or [])]
            ntype = str(price.get("negotiated_type", "") or "")
            rate = float(price.get("negotiated_rate", 0.0))
            is_dollar = ntype in DOLLAR_TYPES
            if is_dollar and rate <= 0.01:
                r.qa.zero_rates += 1
            if not is_dollar:
                r.qa.non_dollar_rows += 1
            row_base = {
                "payer": r.payer,
                "source_file": self.source_file,
                "file_month": r.file_month,
                "schema_version": r.schema_version,
                "last_updated_on": r.last_updated_on,
                "billing_code": code,
                "billing_code_type": code_type,
                "discipline": resolve_discipline(code, modifiers),
                "is_timed": is_timed(code),
                "billing_code_modifier": modifiers,
                "negotiated_rate": rate,
                "negotiated_type": ntype,
                "is_dollar_rate": is_dollar,
                "billing_class": str(price.get("billing_class", "") or ""),
                "service_code": [str(s) for s in (price.get("service_code") or [])],
                "expiration_date": str(price.get("expiration_date", "") or ""),
                "ingested_at": self._ingested_at,
            }
            # rows are emitted per provider GROUP so the NPI↔TIN pairing holds
            for tin_value, tin_type, npis in all_groups:
                tin_flags = {
                    "tin_value": tin_value,
                    "tin_type": tin_type,
                    "tin_is_really_npi": tin_type == "npi",
                }
                if not npis:
                    continue
                for npi in npis:
                    r.rows.append({**row_base, **tin_flags, "npi": npi})
                    r.qa.rows += 1


def parse_provider_reference_file(cfg: MrfxConfig, stream) -> tuple[str, str | None, dict[int, list[PGroup]]]:
    """Standalone provider-reference file -> (payer, last_updated_on, {ref_id: [PGroup]})."""
    payer = "Unknown payer"
    last_updated = None
    refs: dict[int, list[PGroup]] = {}
    builder = None
    for prefix, event, value in ijson.parse(stream, use_float=True):
        if builder is not None:
            builder.event(event, value)
            if event == "end_map" and prefix == "provider_references.item":
                entry = builder.value
                rid = entry.get("provider_group_id")
                if rid is not None:
                    refs[int(rid)] = parse_groups(entry.get("provider_groups") or [])
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
