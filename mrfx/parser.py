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
from pathlib import Path

import ijson

from .catalog import is_timed, resolve_discipline
from .config import DOLLAR_TYPES, MrfxConfig
from .sniff import open_stream  # noqa: F401  (re-exported for ingest)

log = logging.getLogger(__name__)

# therapy_only_ingest is on but the NPPES lookup cache isn't built yet — warn
# ONCE per process instead of per file (the parser runs per file, often in a
# pool). We keep every row until the cache exists rather than drop blindly.
_WARNED_NO_THERAPY_CACHE = False


def _warn_no_therapy_cache() -> None:
    global _WARNED_NO_THERAPY_CACHE
    _WARNED_NO_THERAPY_CACHE = True
    log.warning(
        "therapy_only_ingest is on but the NPPES lookup cache isn't built yet — "
        "ingesting ALL providers for now. Run NPI identification (enrichment.mode="
        "bulk) so it can tell therapists apart; files ingested after that filter.")


def warn_if_slow_json_backend() -> bool:
    """`import ijson` silently falls back to a PURE-PYTHON parser (~10x
    slower) when its compiled C backend isn't installed — a trap that makes
    every ingest crawl with no error. Return True if the fast backend is
    active; warn loudly (once) and return False otherwise. Called at CLI
    startup so a bad install is visible, not just slow."""
    # ijson 3.x exposes the selected backend as a NAME STRING
    # (ijson.backend / ijson.backend_name), e.g. 'yajl2_c' | 'python'.
    name = str(getattr(ijson, "backend_name", None)
               or getattr(ijson, "backend", None) or "")
    fast = "yajl2" in name
    if not fast:  # definitive cross-version fallback: is parse the C one?
        try:
            from ijson.backends import yajl2_c
            fast = ijson.basic_parse is yajl2_c.basic_parse
        except Exception:  # noqa: BLE001 — C backend genuinely absent
            fast = False
    if fast:
        return True
    log.warning(
        "ijson is running its PURE-PYTHON backend (%s) — parsing will be "
        "~10x slower. Reinstall in a Python that has a prebuilt ijson wheel "
        "(pip install --force-reinstall ijson) to get the fast C backend.",
        name or "python")
    return False

ACCEPTED_CODE_TYPES = {"CPT", "HCPCS"}


def code_type_family(raw) -> str | None:
    """Normalize a billing_code_type to 'CPT' / 'HCPCS', or None if it is a
    genuinely different code system.

    Tolerant of vendor spellings so a valid target row is not silently dropped:
    an empty/whitespace value returns None here so the caller treats it the same
    as an ABSENT type (accept best-effort by code shape) — previously an
    empty-string type was rejected while a missing key was accepted. Prefixes
    like 'CPT4' or 'HCPCS Level II' normalize to the family rather than being
    dropped as billing_type_other."""
    t = str(raw or "").strip().upper()
    if not t:
        return None
    if t.startswith("HCPCS"):
        return "HCPCS"
    if t.startswith("CPT"):
        return "CPT"
    return "__other__"  # a real non-CPT/HCPCS system — reject

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
    try:
        return float(v)  # plain numerics (incl. "1e3") parse correctly first
    except (TypeError, ValueError):
        pass
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
    """Provider_reference id as int. Only integral values pass ("12", 12,
    12.0): truncating "12.7" to 12 would silently attach those rates to a
    DIFFERENT provider group, so non-integral numerics count as bad_ref_ids
    instead."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return int(f) if f.is_integer() else None


@dataclass
class QaCounters:
    billing_type_other: int = 0       # non-CPT/HCPCS billing_code_type items
    missing_code_type: int = 0        # target code accepted despite absent type
    multi_code_fields: int = 0        # multiple codes crammed into one billing_code
    zero_rates: int = 0               # $0.00 / $0.01 placeholder prices
    non_dollar_rows: int = 0          # percentage / per diem rows emitted
    unparseable_rates: int = 0        # prices whose negotiated_rate could not be read
    invalid_npis: int = 0             # NPIs that are not 10 digits after cleaning
    tin_only_rows: int = 0            # TIN-only groups (no NPIs) emitted at npi=NULL
    non_therapy_dropped: int = 0      # rows skipped at ingest (therapy_only_ingest)
    bad_ref_ids: int = 0              # provider_reference ids that are not numeric
    bundled_items: int = 0            # bundle/capitation items excluded (not per-code rates)
    prices: int = 0                   # readable prices seen (denominator for price-level shares)
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
    source_file: str | None = None

    @property
    def file_month(self) -> str:
        """Publication month: header first (validated — '2024-13' must not
        pass), then the filename's date (payers stamp it: 2026-07_..._rates),
        then the ingestion month as a last resort. The old header-or-today
        rule silently split one publication across two months whenever a
        dateless file was ingested near a month boundary.

        Memoized on its inputs: this is read once per emitted row, so the
        regex used to run millions of times per file for a file-constant
        value. The cache key recomputes if last_updated_on/source_file
        change (the header-at-EOF case), so the value stays correct."""
        key = (self.last_updated_on, self.source_file)
        if getattr(self, "_fm_key", None) != key:
            self._fm_key = key
            self._fm = self._compute_file_month()
        return self._fm

    def _compute_file_month(self) -> str:
        for candidate in (self.last_updated_on, self.source_file):
            # constrain the month to 01-12 INSIDE the pattern (not as a post
            # check): a junk token like '2024_20' (month 20) in
            # 'plan_2024_2025-03_rates.json' then fails the regex, and the
            # engine continues to the real '2025-03' that OVERLAPS it. A plain
            # first-match-then-validate would consume the '20' of '2025' and
            # miss the valid date entirely. Also rejects '2024-13'.
            m = re.search(r"(20\d{2})[-_](0[1-9]|1[0-2])", str(candidate or ""))
            if m:
                return f"{m.group(1)}-{m.group(2)}"
        return dt.date.today().strftime("%Y-%m")


def parse_provider_group(pg: dict, qa: QaCounters | None = None) -> PGroup:
    npis = []
    for n in as_list(pg.get("npi")):
        d = clean_digits(n)
        if not d:
            continue
        if len(d) != 10:
            # a malformed NPI (wrong digit count) must not enter the emitted
            # rows — it would pollute the NPI-grain rollup and drill-down with a
            # fake id. Count it for QA visibility and drop it; the group's TIN
            # still carries the rate through its valid NPIs.
            if qa is not None:
                qa.invalid_npis += 1
            continue
        npis.append(d)
    # a truthy non-dict tin (some payers write "tin": "123456789" or a list
    # instead of the {type,value} object) would make tin.get() raise and fail
    # the WHOLE file — guard it like every other messy-field path does
    tin = pg.get("tin")
    tin = tin if isinstance(tin, dict) else {}
    tin_type = str(tin.get("type") or "").strip().lower() or None
    raw = tin.get("value")
    if tin_type == "npi":
        value = clean_digits(raw) or None
    else:
        value = clean_digits(raw) or None
        if value and tin_type is None:
            # a 9-digit value with no declared type is in practice an EIN; a
            # 10-digit one is an NPI in the TIN slot and must carry the
            # tin_is_really_npi flag or it pollutes the practice directory
            # and benchmark peer pool as a fake TIN
            tin_type = "ein" if len(value) == 9 else ("npi" if len(value) == 10 else None)
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
        keep_ref_ids: set[int] | None = None,
    ):
        self.cfg = cfg
        self.code_set = cfg.code_set  # None = all codes
        self.source_file = source_file
        self.external_refs = external_refs or {}
        # Huge files: only the references the target codes actually cite are
        # kept in memory (learned by a fast first pass). A multi-GB
        # provider_references table with millions of unused groups never lands
        # in RAM. None = keep all (small files).
        self._keep_ref_ids = keep_ref_ids
        self.result = ParseResult()
        self.result.source_file = source_file  # file_month falls back to its date
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
        # therapy_only_ingest: keep only PT/OT/SLP-provider rows. The set of
        # therapy NPIs comes from the NPPES fast-lookup cache; if it isn't built
        # yet, `_therapy_npis` is None and we keep everything (a logged warning),
        # never silently dropping a whole file. Loaded once per worker (cached).
        self._therapy_npis = None
        if getattr(cfg, "therapy_only_ingest", False):
            from .catalog import therapy_npi_set
            self._therapy_npis = therapy_npi_set(Path(cfg.store_dir) / "nppes_cache.parquet")
            if self._therapy_npis is None and not _WARNED_NO_THERAPY_CACHE:
                _warn_no_therapy_cache()

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
                        # an empty/whitespace type is NOT skipped here — it falls
                        # through so _handle_item accepts it best-effort by code
                        # shape (a missing key already does); only a genuinely
                        # different code system is dropped as billing_type_other
                        and code_type_family(value) == "__other__"
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
                            self.source_file, r.qa.rows, r.payer,
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
        # tolerant like the citation side (clean_ref_id): one dirty
        # provider_group_id ("12.0" as a string) must not kill the whole file
        rid = clean_ref_id(entry.get("provider_group_id"))
        if rid is None:
            return
        # Huge files: skip references the target codes never cite (bounds memory
        # to the therapist-relevant subset of a millions-strong ref table).
        if self._keep_ref_ids is not None and rid not in self._keep_ref_ids:
            return
        groups = entry.get("provider_groups")
        if groups is None and entry.get("location"):
            # file-driven app: remote reference locations are not fetched; the
            # user must drop the companion file in.
            self.result.embedded_refs.setdefault(rid, [])
            return
        self.result.embedded_refs[rid] = parse_groups(groups or [])

    def _resolve_refs(self, ref_ids: list[int]) -> dict[int, list[PGroup] | None]:
        """Resolve a rate group's cited ref ids from the in-memory table
        (for huge files this holds only the target-cited subset), falling back
        to a standalone companion file."""
        out: dict[int, list[PGroup] | None] = {}
        for rid in ref_ids:
            g = self.result.embedded_refs.get(rid)
            out[rid] = g if g else self.external_refs.get(rid)
        return out

    # -- items ----------------------------------------------------------------

    def _handle_item(self, item: dict) -> None:
        qa = self.result.qa
        code = clean_code(item.get("billing_code", ""))
        family = code_type_family(item.get("billing_code_type"))
        if not code or (self.code_set is not None and code not in self.code_set):
            return
        if family == "__other__":  # a real non-CPT/HCPCS system
            return
        if family is None:
            # target code with the type field missing OR blank: accept
            # best-effort, infer the family from the code shape, and count it.
            qa.missing_code_type += 1
            code_type = "HCPCS" if re.fullmatch(r"[A-Z]\d{4}", code) else "CPT"
        else:
            code_type = family  # normalized 'CPT'/'HCPCS' (e.g. from 'CPT4')
        arrangement = str(item.get("negotiation_arrangement") or "ffs").strip().lower()
        if arrangement not in ("", "ffs"):
            # bundle/capitation: negotiated_rate prices the whole bundle, NOT
            # this billing_code — letting it through would put a $500 bundle
            # price into an $85 per-service market median
            qa.bundled_items += 1
            return
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
            # Defer only when the reference table hasn't finished streaming yet
            # (in_network precedes provider_references); the common refs-first
            # layout never defers.
            if ref_ids and not self._refs_complete and any(
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
        if ref_ids:
            resolved = self._resolve_refs(ref_ids)
            # dict.fromkeys: some payers list the same reference id twice in
            # one item — resolving it twice emitted every (npi, tin, price)
            # row twice (aggregates use DISTINCT and were immune, but raw-row
            # views and the QA dup ratio showed the doubles)
            for rid in dict.fromkeys(ref_ids):
                got = resolved.get(rid)
                if not got:
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
            # sorted+deduped: [GP,59] and [59,GP] are the SAME modifier set —
            # publication order must not split the dedup grain into two medians
            modifiers = sorted({str(m).strip().upper() for m in as_list(price.get("billing_code_modifier")) if str(m).strip()})
            ntype = str(price.get("negotiated_type", "") or "").strip().lower()
            rate = clean_rate(price.get("negotiated_rate"))
            if rate is None:
                # no readable dollar figure: nothing to analyze, never fabricate 0.0
                r.qa.unparseable_rates += 1
                continue
            r.qa.prices += 1
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
                # sorted+deduped for the same grain reason as modifiers
                "service_code": sorted({clean_code(s) for s in as_list(price.get("service_code")) if str(s).strip()}),
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
                    # A group with a TIN but no NPIs is a TIN-only rate — the
                    # whole product is TIN-grain, so dropping it lost that
                    # payer's rate for the TIN. Emit one row at npi=NULL so the
                    # rate reaches the TIN/entity grains; it is filtered out of
                    # the NPI grain (DEDUP_QUERY). A group with neither TIN nor
                    # NPI carries nothing and is still skipped.
                    if not tin_value:
                        continue
                    # therapy_only_ingest: when the "TIN" is really an NPI in the
                    # tin slot (tin.type=="npi", empty npi array), it IS
                    # classifiable — drop it if that NPI isn't a therapist. A
                    # genuine EIN TIN-only rate has no NPI to judge, so it is
                    # always kept (the honest TIN-grain rate).
                    if (self._therapy_npis is not None
                            and tin_flags["tin_is_really_npi"]
                            and tin_value not in self._therapy_npis):
                        r.qa.non_therapy_dropped += 1
                        continue
                    row = {**row_base, **tin_flags, "npi": None}
                    (r.rows if self._sink is None else self._buffer).append(row)
                    r.qa.rows += 1
                    r.qa.tin_only_rows += 1
                    continue
                for npi in npis:
                    # therapy_only_ingest: drop rows for NPIs that aren't a
                    # PT/OT/SLP or therapy clinic (per NPPES). TIN-only rows above
                    # are always kept (no NPI to classify).
                    if self._therapy_npis is not None and npi not in self._therapy_npis:
                        r.qa.non_therapy_dropped += 1
                        continue
                    row = {**row_base, **tin_flags, "npi": npi}
                    if self._sink is None:
                        r.rows.append(row)
                    else:
                        self._buffer.append(row)
                    r.qa.rows += 1
        self._flush()


def skim_needed_ref_ids(cfg: MrfxConfig, stream, progress_marker=None) -> tuple[set[int], int]:
    """Fast first pass over a huge in-network file: find the provider_reference
    ids that the TARGET billing codes actually cite. The (millions-strong)
    provider_references table and every non-target in_network item are skipped
    without materializing, so this pass is cheap and bounded — the returned set
    is the therapist-relevant subset the extraction pass keeps in memory.

    Returns (needed_ref_ids, target_items_seen). target_items_seen counts
    in_network items whose billing code IS in the target set — when it is 0
    the file provably contains none of the codes the user cares about, so the
    caller can skip the extraction pass entirely (this scan was the answer).
    """
    code_set = cfg.code_set
    needed: set[int] = set()
    target_items = 0
    builder = None
    skipping = False
    cur_code_ok = False

    for prefix, event, value in ijson.parse(stream, use_float=True):
        # skip the whole provider_references array cheaply
        if prefix.startswith("provider_references"):
            continue
        if builder is not None:
            if not skipping and event in ("string", "number"):
                if prefix == "in_network.item.billing_code":
                    cur_code_ok = code_set is None or clean_code(value) in code_set
                    if not cur_code_ok:
                        skipping = True
                        builder = _DISCARDED
                elif (
                    prefix == "in_network.item.billing_code_type"
                    # mirror the extraction pass: empty/blank type is kept
                    # (best-effort), only a real other code system is skipped —
                    # otherwise the skim and extraction passes disagree on which
                    # ref ids to keep
                    and code_type_family(value) == "__other__"
                ):
                    skipping = True
                    builder = _DISCARDED
            if not skipping:
                # both shapes: the schema says provider_references is an array
                # of ids, but payers also write a bare scalar — the extraction
                # pass tolerates that (as_list), so the skim MUST collect it
                # too or every group citing it is dropped as "missing ref"
                if event in ("number", "string") and prefix in (
                    "in_network.item.negotiated_rates.item.provider_references.item",
                    "in_network.item.negotiated_rates.item.provider_references",
                ):
                    rid = clean_ref_id(value)
                    if rid is not None:
                        needed.add(rid)
            if event == "end_map" and prefix == "in_network.item":
                if not skipping and cur_code_ok:
                    target_items += 1
                builder = None
                skipping = False
                cur_code_ok = False
            continue
        if event == "start_map" and prefix == "in_network.item":
            builder = object()  # sentinel: "inside an item"
            skipping = False
    return needed, target_items


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
                rid = clean_ref_id(entry.get("provider_group_id"))
                if rid is not None:
                    refs[rid] = parse_groups(entry.get("provider_groups") or [])
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
