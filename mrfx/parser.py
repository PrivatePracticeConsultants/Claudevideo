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


# ---------------------------------------------------------------------------
# cleaning helpers — real payer files carry numeric codes, string rates,
# scalar where the schema says array, dashes/whitespace in identifiers, and
# arbitrary extra fields. Clean best-effort; count what couldn't be salvaged.
# ---------------------------------------------------------------------------


def as_list(v) -> list:
    """Schema-says-array fields that arrive as a scalar become a 1-list."""
    if v is None:
        return []
    if isinstance(v, (list, tuple)):
        return list(v)
    return [v]


def clean_code(v) -> str:
    """'97110', 97110, 97110.0, ' g0283 ' -> canonical uppercase string."""
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    s = str(v).strip().upper()
    if s.endswith(".0"):
        s = s[:-2]
    return s


def clean_rate(v) -> float | None:
    """Rate as number or string ('34.50', '$34.50', '34.50 USD') -> float, else None."""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = re.sub(r"[^0-9.\-]", "", str(v))
    try:
        return float(s) if s else None
    except ValueError:
        return None


def clean_digits(v) -> str:
    """Identifier (NPI / EIN) as int, float, or dirty string -> digits only."""
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return re.sub(r"\D", "", str(v))


def clean_ref_id(v) -> int | None:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


@dataclass
class QaCounters:
    billing_type_other: int = 0       # non-CPT/HCPCS billing_code_type items
    missing_code_type: int = 0        # target code accepted despite absent type
    multi_code_fields: int = 0        # multiple codes crammed into one billing_code
    zero_rates: int = 0               # $0.00 / $0.01 placeholder prices
    non_dollar_rows: int = 0          # percentage / per diem rows emitted
    unparseable_rates: int = 0        # prices whose negotiated_rate could not be read
    invalid_npis: int = 0             # NPIs that are not 10 digits after cleaning
    bad_ref_ids: int = 0              # provider_reference ids that are not numeric
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


def parse_provider_group(pg: dict, qa: QaCounters | None = None) -> PGroup:
    npis = []
    for n in as_list(pg.get("npi")):
        d = clean_digits(n)
        if not d:
            continue
        if len(d) != 10 and qa is not None:
            qa.invalid_npis += 1
        npis.append(d)
    tin = pg.get("tin") or {}
    tin_type = str(tin.get("type") or "").strip().lower() or None
    raw = tin.get("value")
    if tin_type == "npi":
        value = clean_digits(raw) or None
    else:
        value = clean_digits(raw) or None
        # a 9-digit value with no declared type is in practice an EIN
        if value and tin_type is None:
            tin_type = "ein"
    # dedupe while keeping order (payers repeat NPIs within a group)
    return (value, tin_type, tuple(dict.fromkeys(npis)))


def parse_groups(provider_groups, qa: QaCounters | None = None) -> list[PGroup]:
    return [parse_provider_group(pg, qa) for pg in as_list(provider_groups) if isinstance(pg, dict)]


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

    # rows flushed to the sink in batches so a huge file never materializes
    # its whole row set in Python memory (constant-memory guarantee, §8.1)
    BATCH_ROWS = 50_000

    def __init__(
        self,
        cfg: MrfxConfig,
        source_file: str,
        external_refs: dict[int, list[PGroup]] | None = None,
        sink=None,
        header_defaults: dict | None = None,
    ):
        self.cfg = cfg
        self.code_set = cfg.code_set  # None = all codes
        self.source_file = source_file
        self.external_refs = external_refs or {}
        self.result = ParseResult()
        # Seed header from preflight (which already sniffed the first ~1 MB) so
        # rows are correct from the first batch even when the actual header
        # tokens stream past later. Prevents needing every row in memory to
        # re-stamp at EOF.
        if header_defaults:
            self.result.payer = header_defaults.get("payer") or self.result.payer
            self.result.schema_version = header_defaults.get("schema_version")
            self.result.last_updated_on = header_defaults.get("last_updated_on")
        self._sink = sink
        self._buffer: list[dict] = []
        self._flushed = False
        self._refs_complete = False
        self._deferred: list[_DeferredGroup] = []
        self._ingested_at = dt.datetime.now(dt.timezone.utc).isoformat()

    def _flush(self, final: bool = False) -> None:
        if self._sink is None or (not final and len(self._buffer) < self.BATCH_ROWS):
            return
        if self._buffer:
            self._sink(self._buffer)
            self._flushed = True
            self._buffer = []

    def parse(self, stream) -> ParseResult:
        r = self.result
        builder = None
        building = None
        skipping = False

        for prefix, event, value in ijson.parse(stream, use_float=True):
            if builder is not None:
                if building == "item" and not skipping and event in ("string", "number"):
                    if prefix == "in_network.item.billing_code":
                        code = clean_code(value)
                        if self.code_set is not None and code not in self.code_set:
                            skipping = True
                            if isinstance(value, str) and MULTI_CODE_RE.search(value.strip()):
                                r.qa.multi_code_fields += 1
                    elif (
                        prefix == "in_network.item.billing_code_type"
                        and str(value).strip().upper() not in ACCEPTED_CODE_TYPES
                    ):
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
                    new_payer = self.cfg.normalize_payer(value)
                    if self._flushed and new_payer != r.payer:
                        log.warning(
                            "%s: reporting_entity_name appeared after %d rows were already "
                            "written; those rows keep the preflight-seeded payer %r",
                            self.source_file, self.BATCH_ROWS, r.payer,
                        )
                    r.payer = new_payer
                elif prefix == "version":
                    r.schema_version = value
                else:
                    r.last_updated_on = value

        self._refs_complete = True
        for dg in self._deferred:
            self._emit_group(dg.billing_code, dg.billing_code_type, dg.groups, dg.ref_ids, dg.prices)
        # Header fields may appear AFTER in_network. Rows still in the buffer
        # (not yet flushed) get the final header stamped now; already-flushed
        # rows relied on the preflight seed (correct for every real file, whose
        # header is at the top). The tiny header-at-EOF hostile case fits in one
        # batch and is fully corrected here.
        for row in self._buffer:
            row["payer"] = r.payer
            row["schema_version"] = r.schema_version
            row["last_updated_on"] = r.last_updated_on
            row["file_month"] = r.file_month
        self._flush(final=True)
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
        qa = self.result.qa
        code = clean_code(item.get("billing_code", ""))
        code_type = str(item.get("billing_code_type") or "").strip().upper()
        if not code or (self.code_set is not None and code not in self.code_set):
            return
        if code_type and code_type not in ACCEPTED_CODE_TYPES:
            return
        if not code_type:
            # target code with the type field missing entirely: accept
            # best-effort, infer the family from the code shape, and count it.
            qa.missing_code_type += 1
            code_type = "HCPCS" if re.fullmatch(r"[A-Z]\d{4}", code) else "CPT"
        self.result.items_matched += 1
        for grp in as_list(item.get("negotiated_rates")):
            if not isinstance(grp, dict):
                continue
            groups = parse_groups(grp.get("provider_groups"), qa)
            ref_ids = []
            for x in as_list(grp.get("provider_references")):
                rid = clean_ref_id(x)
                if rid is None:
                    qa.bad_ref_ids += 1
                else:
                    ref_ids.append(rid)
            prices = [p for p in as_list(grp.get("negotiated_prices")) if isinstance(p, dict)]
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
            modifiers = [str(m).strip().upper() for m in as_list(price.get("billing_code_modifier")) if str(m).strip()]
            ntype = str(price.get("negotiated_type", "") or "").strip().lower()
            rate = clean_rate(price.get("negotiated_rate"))
            if rate is None:
                # no readable dollar figure: nothing to analyze, never fabricate 0.0
                r.qa.unparseable_rates += 1
                continue
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
                "billing_class": str(price.get("billing_class", "") or "").strip().lower(),
                "service_code": [clean_code(s) for s in as_list(price.get("service_code")) if str(s).strip()],
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
                    row = {**row_base, **tin_flags, "npi": npi}
                    if self._sink is None:
                        r.rows.append(row)
                    else:
                        self._buffer.append(row)
                    r.qa.rows += 1
        self._flush()


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
