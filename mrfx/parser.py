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
from .modifiers import normalize as normalize_modifiers
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


# one money token, optionally "$"-prefixed, thousands commas and a trailing
# currency word allowed: '$34.50', '34.50 USD', '1,234.50'. Anything else
# ("2 x 34.50", "Rate: 34.50 (2 units)") is not one rate and is refused —
# stripping every non-digit used to glue them into 234.5 / 34.502.
_MONEY_RE = re.compile(r"\s*\$?\s*(-?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?|-?\.\d+)"
                       r"\s*(?:usd|dollars?)?\s*", re.I)


def clean_rate(v) -> float | None:
    """Rate as number or string ('34.50', '$34.50', '34.50 USD') -> float, else None.

    Only FINITE numbers come back. float() accepts "nan", "Infinity" and
    "1e999" — a payer pipeline that stringifies its rates writes exactly
    those — and a NaN rate then poisoned the practice's median (DuckDB orders
    NaN above every number, so the rollup's `> 0.01` filter kept it). Round 7
    P0.1: they land under unparseable_rates instead. A JSON boolean is not a
    rate either (bool is an int in Python: True used to read as $1.00)."""
    import math
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        f = float(v)
        return f if math.isfinite(f) else None
    try:
        f = float(v)  # plain numerics (incl. "1e3") parse correctly first
    except (TypeError, ValueError, OverflowError):
        m = _MONEY_RE.fullmatch(str(v))
        if not m:
            return None
        try:
            f = float(m.group(1).replace(",", ""))
        except ValueError:
            return None
    return f if math.isfinite(f) else None


_NON_DIGIT_RE = re.compile(r"\D")


def clean_digits(v) -> str:
    """Identifier (NPI / EIN) as int, float, or dirty string -> digits only.

    Called for every NPI and TIN of every emitted provider group (~1.8M
    times for a 13M-row file), so the already-clean shapes skip the regex:
    a non-negative int's str() IS its digits, and an ASCII-digit string is
    returned as is. (isascii() matters: str.isdigit() also accepts e.g.
    superscript digits, which the \\D regex would strip.)"""
    if type(v) is int and v >= 0:
        return str(v)
    if type(v) is str and v.isascii() and v.isdigit():
        return v
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return _NON_DIGIT_RE.sub("", str(v))


from .refid import clean_ref_id  # noqa: E402,F401 — one exact rule (review 9 1.4)


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
    remote_ref_groups: int = 0        # skipped rate groups whose references are remote `location` links
    unknown_arrangement: int = 0      # items whose negotiation_arrangement is neither ffs nor
                                      # bundle/capitation (kept, counted — round 7 P2)
    missing_negotiated_type: int = 0  # prices with a blank/missing negotiated_type (not dollars)
    missing_ref_ids: int = 0          # provider_reference ids a rate group cited that no reference
                                      # file defined (remote links excluded) — even when the
                                      # group's OTHER ids resolved (round 7 P1.7)
    ambiguous_ref_ids: int = 0        # ids the payer's standalone reference files define
                                      # differently at the same vintage: not attached (audit 8)
    prices: int = 0                   # readable prices seen (denominator for price-level shares)
    modifier_split_rows: int = 0      # rows whose modifier element held several ("52,53" -> 52|53)
    pos_in_modifier_rows: int = 0     # rows whose "modifiers" were place-of-service codes (moved)
    future_header_date: str = ""      # header last_updated_on later than the day the file was read
    rows: int = 0

    def to_dict(self) -> dict:
        return dict(self.__dict__)


def normalize_negotiated_type(value) -> str:
    """'Fee_Schedule' / 'fee-schedule' -> 'fee schedule'. ONE function for the
    parser and the stored-row migration (store._normalize_part), so a part
    written by an older build reads exactly as a re-ingest would write it."""
    return re.sub(r"[\s_\-]+", " ", str(value or "").strip().lower())


def skipped_groups_reason(skipped: int, remote: int = 0, ambiguous: int = 0) -> str:
    """One sentence for "N rate groups skipped — why", shared by the log,
    the CLI and the API so the three never disagree."""
    if remote >= skipped:
        return ("their provider lists are remote links MRF Explorer doesn't download")
    parts = []
    if remote:
        parts.append(f"{remote} point at remote provider lists MRF Explorer doesn't download")
    if ambiguous:
        parts.append(f"{ambiguous} reference id(s) are defined differently by two of the payer's "
                     "provider reference files of the same month, so neither was used")
    rest = skipped - remote
    if not ambiguous or rest > ambiguous:
        parts.append("the rest need the payer's provider reference file" if parts
                     else "missing provider reference file")
    return "; ".join(parts)


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
    ingest_month: str | None = None   # UTC month the file was read (from ingested_at)

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
        key = (self.last_updated_on, self.source_file, self.ingest_month)
        if getattr(self, "_fm_key", None) != key:
            self._fm_key = key
            self._fm = self._compute_file_month()
        return self._fm

    def _compute_file_month(self) -> str:
        im = self.ingest_month or dt.datetime.now(dt.timezone.utc).strftime("%Y-%m")
        month, future = file_month_for(self.last_updated_on, self.source_file, im)
        self.qa.future_header_date = str(self.last_updated_on) if future else ""
        return month


_MONTH_RE = re.compile(r"(20\d{2})[-_](0[1-9]|1[0-2])")


def month_in(v) -> str | None:
    """'YYYY-MM' found in a header date or a file name. The month is
    constrained to 01-12 INSIDE the pattern (not as a post check): a junk token
    like '2024_20' (month 20) in 'plan_2024_2025-03_rates.json' then fails, and
    the engine continues to the real '2025-03' that OVERLAPS it. Rejects
    '2024-13'."""
    m = _MONTH_RE.search(str(v or ""))
    return f"{m.group(1)}-{m.group(2)}" if m else None


def file_month_for(last_updated_on, source_file, ingest_month: str) -> tuple[str, bool]:
    """(file_month, header_was_in_the_future) — THE month rule, shared by the
    parser and the one-time fix of rows already stored (a pure function of
    stored columns: last_updated_on, source_file, ingested_at's month).

    The month is the payer's own statement of vintage — the header's
    last_updated_on — else the file name's date, else the month it was read.
    A header dated AFTER the month the file was read cannot be its vintage
    (measured: a UHC HEALTHSCOPE file read 2026-11-05 says 2026-12-01; the
    Months list offered December in November and "latest" preferred it, and
    when the real December file arrived the two would have pooled as one
    month): it is filed under the file name's month, else the month read."""
    hdr, name = month_in(last_updated_on), month_in(source_file)
    if hdr and hdr > ingest_month:
        return (name if name and name <= ingest_month else ingest_month), True
    return hdr or name or ingest_month, False


def published_month_of(source_file, ingest_month: str) -> str:
    """The month a file was PUBLISHED, as its name says (index / file date),
    else the month it was read — shown beside file_month, because payers'
    clocks differ (BCBS MS's October files carry a 2026-08-25 header)."""
    return month_in(source_file) or ingest_month


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


def share_strings(groups: list[PGroup], pool: dict) -> list[PGroup]:
    """The same groups with every TIN/NPI string replaced by ONE shared
    object per distinct value (`pool` maps value -> canonical object).

    A kept reference table repeats NPIs heavily — a real Aetna file: 7.4M
    NPI slots, 2.3M distinct — and each repeat was its own ~59-byte string.
    Sharing cut that table 606 MB -> 408 MB (measured), per worker process.
    Values are unchanged; only identity is shared."""
    get = pool.setdefault
    return [(get(t, t) if t is not None else None, ty, tuple(get(n, n) for n in ns))
            for t, ty, ns in groups]


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


class _DeferredSpool:
    """The rate groups a file defers until its reference table is complete,
    held in memory up to DEFER_MEMORY_GROUPS and spilled to a disk file
    beyond that (pickled one by one, read back in order). Since audit 8 a
    file with NO table of its own defers every group to its end of file:
    peak memory grew ~5 MB per MB of file (a 401 MB file: 2.3-2.8 GB, was
    0.4 GB) — invariant 1 says it must not grow with file size (review 9
    item 3.3). The spill sits next to the store's parts as
    .{key}-deferred.{pid}.spool.tmp, which the orphan sweep removes if killed."""

    def __init__(self, spill_dir: Path | None, key: str):
        self._mem: list = []
        self._spill_dir = spill_dir
        self._key = key
        self._path: Path | None = None
        self._fh = None
        self.spilled = 0

    def append(self, dg) -> None:
        if self._fh is None and (len(self._mem) < DEFER_MEMORY_GROUPS or self._spill_dir is None):
            self._mem.append(dg)
            return
        import pickle
        if self._fh is None:
            import os
            self._spill_dir.mkdir(parents=True, exist_ok=True)
            # unique per spool: a declined fast parse and its classic retry
            # run in one process and must never share (or delete) a spill
            self._path = self._spill_dir / f".{self._key}-deferred{id(self):x}.{os.getpid()}.spool.tmp"
            self._fh = open(self._path, "wb")  # noqa: SIM115 — closed in drain()
            for old in self._mem:
                pickle.dump(old, self._fh, protocol=pickle.HIGHEST_PROTOCOL)
                self.spilled += 1
            self._mem = []
        pickle.dump(dg, self._fh, protocol=pickle.HIGHEST_PROTOCOL)
        self.spilled += 1

    def __len__(self) -> int:
        return len(self._mem) + self.spilled

    def drain(self):
        """Every deferred group, in arrival order; removes the spill file."""
        if self._fh is None:
            yield from self._mem
            self._mem = []
            return
        import pickle
        self._fh.close()
        self._fh = None
        try:
            with open(self._path, "rb") as fh:
                while True:
                    try:
                        yield pickle.load(fh)
                    except EOFError:
                        break
        finally:
            self.close()

    def __del__(self):
        try:
            self.close()
        except Exception:  # noqa: BLE001 — interpreter shutdown
            pass

    def close(self) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None
        if self._path is not None:
            try:
                self._path.unlink(missing_ok=True)
            except OSError:
                pass
            self._path = None


# deferred groups held in memory before the rest spill to disk (a group is a
# few KB: ~40 MB at most)
DEFER_MEMORY_GROUPS = 10_000


_ROW_COLS = ("tin_value", "tin_type", "tin_is_really_npi", "npi")


class RowBatch:
    """A batch of emitted rate rows held COLUMN-wise, factored twice.

    Every row of one negotiated price shares all 16 price-level fields, and
    every price of one item fans out over the SAME provider groups. So a batch
    stores: `bases` (one dict per price), `groups` (one entry per distinct
    provider group: tin_value, tin_type, tin_is_really_npi, kept NPIs), and
    one (price, group) PAIR per fan-out — never a per-row Python object.
    `to_table` expands pairs into rows with numpy indexing and Arrow `take`,
    converting each NPI string once per batch instead of once per row.

    Measured on a byte-exact 2.7 GB slice of a real Aetna file (60M rows): the
    per-(price, group) list building was 37 s and the per-row string
    conversion most of `to_table`'s 27 s, of a 133 s run. Iterating still
    yields the same row dicts, in the same order, for sinks that want dicts.
    """

    __slots__ = ("bases", "pair_base", "pair_group", "groups", "_gid", "_keys", "_n")

    def __init__(self):
        self.bases: list[dict] = []
        self.pair_base: list[int] = []
        self.pair_group: list[int] = []
        self.groups: list[tuple] = []     # (tin_value, tin_type, really, npis)
        self._gid: dict[int, int] = {}    # id(group key) -> index in groups
        self._keys: list = []             # keeps keys alive so ids stay unique
        self._n = 0

    def __len__(self) -> int:
        return self._n

    def __bool__(self) -> bool:
        return self._n > 0

    def add_base(self, base: dict) -> int:
        self.bases.append(base)
        return len(self.bases) - 1

    def group(self, key, tin_value, tin_type, really: bool, npis: tuple) -> int:
        """Index of a provider group in this batch. `key` is the group's
        source object; the same key always yields the same (tin, npis), so
        it is registered once however many prices fan out over it."""
        i = self._gid.get(id(key))
        if i is None:
            i = len(self.groups)
            self.groups.append((tin_value, tin_type, really, npis))
            self._gid[id(key)] = i
            self._keys.append(key)
        return i

    def add_pairs(self, base_i: int, group_idx: list[int], nrows: int) -> None:
        """One price fanned out over `group_idx` (in emission order)."""
        self.pair_base.extend([base_i] * len(group_idx))
        self.pair_group.extend(group_idx)
        self._n += nrows

    def add(self, base_i: int, tin_value, tin_type, really: bool, npis) -> None:
        """Single-group form (kept for callers outside the parser)."""
        key = (tin_value, tin_type, really, tuple(npis))
        self.add_pairs(base_i, [self.group(key, tin_value, tin_type, really, key[3])], len(npis))

    def __iter__(self):
        for b, g in zip(self.pair_base, self.pair_group):
            tin_value, tin_type, really, npis = self.groups[g]
            base = self.bases[b]
            for n in npis:
                yield {**base, "tin_value": tin_value, "tin_type": tin_type,
                       "tin_is_really_npi": really, "npi": n}

    def _row_indices(self):
        """(row -> base index, row -> group index, row -> flat NPI index)."""
        glen = [len(g[3]) for g in self.groups]
        try:
            import numpy as np
        except ImportError:   # an install predating the numpy pin: plain Python
            starts, acc = [], 0
            for n in glen:
                starts.append(acc)
                acc += n
            rb, rg, ri = [], [], []
            for b, g in zip(self.pair_base, self.pair_group):
                n = glen[g]
                rb.extend([b] * n)
                rg.extend([g] * n)
                ri.extend(range(starts[g], starts[g] + n))
            return rb, rg, ri
        pb = np.asarray(self.pair_base, dtype=np.int64)
        pg = np.asarray(self.pair_group, dtype=np.int64)
        gl = np.asarray(glen, dtype=np.int64)
        gstart = np.cumsum(gl) - gl
        lens = gl[pg]
        first = np.cumsum(lens) - lens
        within = np.arange(int(lens.sum()), dtype=np.int64) - np.repeat(first, lens)
        return (np.repeat(pb, lens), np.repeat(pg, lens),
                np.repeat(gstart[pg], lens) + within)

    def to_table(self, schema):
        import pyarrow as pa

        row_base, row_group, row_npi = self._row_indices()
        base_schema = pa.schema([f for f in schema if f.name not in _ROW_COLS])
        bt = pa.Table.from_pylist(self.bases, schema=base_schema).take(
            pa.array(row_base, type=pa.int64()))
        rg = pa.array(row_group, type=pa.int64())
        per_group = {
            "tin_value": [g[0] for g in self.groups],
            "tin_type": [g[1] for g in self.groups],
            "tin_is_really_npi": [g[2] for g in self.groups],
        }
        cols = []
        for f in schema:
            if f.name == "npi":
                flat = pa.array([n for g in self.groups for n in g[3]], type=f.type)
                cols.append(flat.take(pa.array(row_npi, type=pa.int64())))
            elif f.name in per_group:
                cols.append(pa.array(per_group[f.name], type=f.type).take(rg))
            else:
                cols.append(bt.column(f.name))
        return pa.Table.from_arrays(cols, schema=schema)


def batch_to_table(rows, schema):
    """A sink's batch (RowBatch or a plain list of row dicts) as an Arrow table."""
    if isinstance(rows, RowBatch):
        return rows.to_table(schema)
    import pyarrow as pa
    return pa.Table.from_pylist(rows, schema=schema)


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
        self._buffer = RowBatch()
        self._flushed = False
        self._refs_complete = False
        # True once the file's OWN top-level provider_references array has
        # been read (even an empty one) — diagnostics/tests; resolution keys
        # on the ids the table defines (see _resolve_refs, audit 8 P0-1)
        self._own_table = False
        # True once a top-level in_network array was seen. A file with a
        # reference table and NO in_network is a standalone reference file
        # that the 1 MB sniff could not tell apart (its table outgrew the
        # window): ingest stores its references instead (audit 8 P1-2)
        self.saw_in_network = False
        try:
            from .store import file_key
            _spill = Path(cfg.store_dir) / "rates" if getattr(cfg, "store_dir", None) else None
            _key = file_key(source_file or "file")
        except Exception:  # noqa: BLE001 — no store: keep everything in memory
            _spill, _key = None, "file"
        self._deferred = _DeferredSpool(_spill, _key)
        self._ingested_at = dt.datetime.now(dt.timezone.utc).isoformat()
        self.result.ingest_month = self._ingested_at[:7]
        self._remote_rids: set[int] = set()
        self._strings: dict = {}   # shared NPI/TIN strings for kept references
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
            self._buffer = RowBatch()

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
                self._own_table = True
            elif event == "start_array" and prefix == "in_network":
                self.saw_in_network = True
            elif event == "string" and prefix in ("reporting_entity_name", "version", "last_updated_on"):
                self._set_header(prefix, value)

        return self._finish()

    def _finish(self) -> ParseResult:
        r = self.result
        self._refs_complete = True
        # Header fields may appear AFTER in_network. Rows still in the buffer
        # (not yet flushed) get the final header stamped now; already-flushed
        # rows relied on the preflight seed (correct for every real file, whose
        # header is at the top). Stamp BEFORE emitting the deferred groups:
        # emitting them can trigger a flush, which used to ship still-buffered
        # mid-stream rows with the seed even though the real header was known
        # (deferred rows themselves are built after the header, so they're
        # already right).
        for row in self._buffer.bases:
            row["payer"] = r.payer
            row["schema_version"] = r.schema_version
            row["last_updated_on"] = r.last_updated_on
            row["file_month"] = r.file_month
        for dg in self._deferred.drain():
            self._emit_group(dg.billing_code, dg.billing_code_type, dg.groups, dg.ref_ids, dg.prices)
        self._flush(final=True)
        return r

    @property
    def reference_only(self) -> bool:
        """The whole file was read and it is a reference table with no
        in_network array: a standalone provider-reference file."""
        return self._own_table and not self.saw_in_network

    def _set_header(self, key: str, value: str) -> None:
        r = self.result
        if key == "reporting_entity_name":
            new_payer = self.cfg.normalize_payer(value)
            if self._flushed and new_payer != r.payer:
                log.warning(
                    "%s: reporting_entity_name appeared after %d rows were already "
                    "written; those rows keep the preflight-seeded payer %r",
                    self.source_file, r.qa.rows, r.payer,
                )
            r.payer = new_payer
        elif key == "version":
            r.schema_version = value
        else:
            r.last_updated_on = value

    def _count_type_other(self, n: int) -> None:
        self.result.qa.billing_type_other += n

    def _want_item(self, events: list, final: bool):
        """Fast-path keep/skip verdict for one in_network member, mirroring
        the ijson loop above EXACTLY: it checks each billing_code /
        billing_code_type event as it streams (duplicates included) and stops
        at the first that says skip — which is also which QA counter fires."""
        qa = self.result.qa
        for kind, value in events:
            if kind == "type":
                if code_type_family(value) == "__other__":
                    qa.billing_type_other += 1
                    return False
            elif self.code_set is not None and clean_code(value) not in self.code_set:
                if isinstance(value, str) and MULTI_CODE_RE.search(value.strip()):
                    qa.multi_code_fields += 1
                return False
        return True if final else None

    def parse_fast(self, stream) -> ParseResult:
        """Same output as parse(), via the byte-level splitter: non-target
        in_network members (and non-kept references) are never tokenized in
        Python. Raises fastscan.FastScanUnsupported — before emitting any row —
        when it can't vouch for the file's shape; the caller then uses parse().
        """

        from .fastscan import decode_member, iter_members

        keep = self._keep_ref_ids

        def want_ref(raw) -> bool:
            rid = clean_ref_id(raw)
            return rid is not None and (keep is None or rid in keep)

        counts: dict = {}
        for array, data in iter_members(stream, self._want_item, want_ref,
                                        on_header=self._set_header, counts=counts,
                                        target_codes=self.code_set, ref_keep=keep,
                                        on_type_other=self._count_type_other):
            if data is None:
                if array == "provider_references":
                    self._refs_complete = True
                    self._own_table = True
                elif array == "in_network":
                    self.saw_in_network = True
                continue
            obj = decode_member(data)
            if array == "provider_references":
                self._add_ref(obj)
            else:
                self._handle_item(obj)
        self.result.items_seen = counts.get("in_network", 0)
        return self._finish()

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
            # a reference whose provider list lives at a remote URL. MRF
            # Explorer does NOT download these (and a dropped-in copy can't
            # help: that file has no provider_group_id to match on). Groups
            # citing it are skipped and counted as remote_ref_groups, so the
            # Files tab can say WHY instead of "missing companion file".
            self.result.embedded_refs.setdefault(rid, [])
            self._remote_rids.add(rid)
            return
        self.result.embedded_refs[rid] = share_strings(parse_groups(groups or []), self._strings)

    def _resolve_refs(self, ref_ids: list[int]) -> dict[int, list[PGroup] | None]:
        """Resolve a rate group's cited ref ids. An id the file's OWN
        provider_references table defines resolves only from it (for huge
        files the in-memory copy holds only the target-cited subset) — even
        when that entry is a remote link or empty: it is then missing, never
        filled from another file, whose ids mean other practices. Only an id
        the file does not define at all falls back to the payer's standalone
        companion; an id the companions define two different ways (None)
        stays unresolved. Callers resolve only once the table is complete
        (_handle_item defers until then)."""
        from .refid import REMOTE_REF
        out: dict[int, list[PGroup] | None] = {}
        own = self.result.embedded_refs
        for rid in ref_ids:
            if rid in own:
                out[rid] = own[rid] or None
            else:
                ext = self.external_refs.get(rid)
                if ext == REMOTE_REF:
                    self._remote_rids.add(rid)
                    ext = None
                out[rid] = ext or None
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
        # "fee for service" / "fee-for-service" / "FFS" are all ffs: only an
        # exact "ffs" passed before, and a payer spelling it out sent a whole
        # file to 0 rows under bundled_items (round 7 P2)
        arrangement = re.sub(r"[\s_\-]+", " ",
                             str(item.get("negotiation_arrangement") or "ffs").strip().lower())
        if arrangement == "fee for service":
            arrangement = "ffs"
        if arrangement in ("bundle", "capitation") or arrangement.startswith(("bundle", "capitat")):
            # bundle/capitation: negotiated_rate prices the whole bundle, NOT
            # this billing_code — letting it through would put a $500 bundle
            # price into an $85 per-service market median
            qa.bundled_items += 1
            return
        if arrangement not in ("", "ffs"):
            qa.unknown_arrangement += 1   # kept: a per-code price, labelled oddly
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
            # Defer while the reference table hasn't finished streaming
            # (in_network precedes provider_references, or the file has no
            # table and that is only known at its end); the common refs-first
            # layout never defers. A standalone companion that happens to
            # define the id is NOT a reason to emit early: whether this file
            # has its own table is not known yet (audit 8 P0-1).
            # ...and while ANY cited id is not in the table read so far: the
            # file's table is every provider_references array it contains (a
            # second array after in_network defined ids the first did not —
            # they were filled from a companion; review 9 item 3.5). Deferred
            # groups spill to disk (_DeferredSpool), so this costs no memory.
            if ref_ids and any(
                rid not in self.result.embedded_refs for rid in ref_ids
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
        missing_ref = remote_ref = False
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
                    if rid in self._remote_rids:
                        remote_ref = True
                    elif (rid not in self.result.embedded_refs and rid in self.external_refs
                          and self.external_refs[rid] is None):
                        self.result.qa.ambiguous_ref_ids += 1
                    else:
                        # counted even when the group's other ids resolved:
                        # [7, 8] with only 7 known emitted 7's practices and
                        # dropped 8's in silence, and the file was never
                        # re-ingested when the companion landed
                        self.result.qa.missing_ref_ids += 1
                    continue
                all_groups.extend(got)
        if missing_ref and not all_groups:
            self.result.ref_groups_skipped += 1
            if remote_ref:
                self.result.qa.remote_ref_groups += 1
            return
        if not all_groups:
            return

        r = self.result
        # Decide each provider group ONCE (not once per price — every price of
        # an item fans out over the same groups): TIN-only handling, the
        # therapy filter, and the per-price QA tallies.
        therapy = self._therapy_npis
        prepared = []          # (key, tin_value, tin_type, really, kept npis)
        per_price_rows = per_price_tin_only = per_price_dropped = 0
        for g in all_groups:
            tin_value, tin_type, npis = g
            really = tin_type == "npi"
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
                if therapy is not None and really and tin_value not in therapy:
                    per_price_dropped += 1
                    continue
                keep = (None,)
                per_price_tin_only += 1
            elif therapy is not None:
                # therapy_only_ingest: drop rows for NPIs that aren't a
                # PT/OT/SLP or therapy clinic (per NPPES). TIN-only rows
                # above are always kept (no NPI to classify).
                keep = tuple(n for n in npis if n in therapy)
                per_price_dropped += len(npis) - len(keep)
                if not keep:
                    continue
            else:
                keep = npis
            per_price_rows += len(keep)
            prepared.append((g, tin_value, tin_type, really, keep))
        gidx = None
        for price in prices:
            # modifiers.normalize: sorted+deduped ([GP,59] == [59,GP]), "52,53"
            # split into two, and a payer's place-of-service list published in
            # the modifier field moved to service_code — see mrfx/modifiers.py
            modifiers, service_codes, mod_split, pos_moved = normalize_modifiers(
                as_list(price.get("billing_code_modifier")),
                # sorted+deduped for the same grain reason as modifiers
                sorted({clean_code(s) for s in as_list(price.get("service_code")) if str(s).strip()}))
            # "fee_schedule" / "Fee-Schedule" are "fee schedule": an exact
            # match stored them as NOT dollars — out of every median (P2)
            ntype = normalize_negotiated_type(price.get("negotiated_type"))
            if not ntype:
                r.qa.missing_negotiated_type += 1
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
                "service_code": service_codes,
                "expiration_date": str(price.get("expiration_date", "") or ""),
                "ingested_at": self._ingested_at,
            }
            if mod_split:
                r.qa.modifier_split_rows += per_price_rows
            if pos_moved:
                r.qa.pos_in_modifier_rows += per_price_rows
            r.qa.non_therapy_dropped += per_price_dropped
            r.qa.tin_only_rows += per_price_tin_only
            r.qa.rows += per_price_rows
            # rows are emitted per provider GROUP so the NPI↔TIN pairing holds
            if self._sink is None:
                for _key, tin_value, tin_type, really, keep in prepared:
                    tin_flags = {"tin_value": tin_value, "tin_type": tin_type,
                                 "tin_is_really_npi": really}
                    r.rows.extend({**row_base, **tin_flags, "npi": n} for n in keep)
                continue
            buf = self._buffer
            if gidx is None:   # the buffer only swaps in _flush, after this loop
                gidx = [buf.group(key, tv, tt, re_, keep) for key, tv, tt, re_, keep in prepared]
            buf.add_pairs(buf.add_base(row_base), gidx, per_price_rows)
        self._flush()


def skim_needed_ref_ids(cfg: MrfxConfig, stream, shape: dict | None = None) -> tuple[set[int], int]:
    """Fast first pass over a huge in-network file: find the provider_reference
    ids that the TARGET billing codes actually cite. The (millions-strong)
    provider_references table and every non-target in_network item are skipped
    without materializing, so this pass is cheap and bounded — the returned set
    is the therapist-relevant subset the extraction pass keeps in memory.

    Returns (needed_ref_ids, target_items_seen). target_items_seen counts
    in_network items whose billing code IS in the target set — when it is 0
    the file provably contains none of the codes the user cares about, so the
    caller can skip the extraction pass entirely (this scan was the answer).
    `shape` (optional) receives {"in_network": seen, "refs": seen} for the
    top-level arrays, so a reference-only file the 1 MB sniff misread is
    stored as references on this path too (review 9 item 3.1).
    """
    code_set = cfg.code_set
    needed: set[int] = set()
    target_items = 0
    builder = None
    skipping = False
    cur_code_ok = False

    for prefix, event, value in ijson.parse(stream, use_float=True):
        if shape is not None and event == "start_array":
            if prefix == "in_network":
                shape["in_network"] = True
            elif prefix == "provider_references":
                shape["refs"] = True
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


def skim_needed_ref_ids_fast(cfg: MrfxConfig, stream, shape: dict | None = None) -> tuple[set[int], int]:
    """skim_needed_ref_ids via the byte-level splitter (same contract and
    result): only target-code members are decoded; the reference table and
    non-target members are skipped as raw bytes."""

    from .fastscan import decode_member, iter_members

    code_set = cfg.code_set  # a property that rebuilds the set: read once
    probe = InNetworkParser.__new__(InNetworkParser)  # predicate only
    probe.code_set = code_set
    probe.result = ParseResult()
    needed: set[int] = set()
    target_items = 0
    for array, data in iter_members(stream, probe._want_item, None, target_codes=code_set):
        if data is None:
            if shape is not None and array in ("in_network", "provider_references"):
                shape["in_network" if array == "in_network" else "refs"] = True
            continue
        if array != "in_network":
            continue
        item = decode_member(data)
        # the ijson skim counts an item only once a string/number billing_code
        # in the target set has streamed — a code-less member is not a target
        code = item.get("billing_code")
        if not isinstance(code, (str, int, float)) or isinstance(code, bool) or (
                code_set is not None and clean_code(code) not in code_set):
            continue
        target_items += 1
        for grp in as_list(item.get("negotiated_rates")):
            if not isinstance(grp, dict):
                continue
            for x in as_list(grp.get("provider_references")):
                if isinstance(x, (dict, list)):
                    continue
                rid = clean_ref_id(x)
                if rid is not None:
                    needed.add(rid)
    return needed, target_items


_WARNED_NO_NUMPY = False


def _fast_scan_unsupported(cfg: MrfxConfig):
    """The fast path's decline exception if the fast path is enabled AND
    importable, else None (classic parser). An install that predates the
    numpy requirement must keep ingesting — slower, with one warning — never
    fail every file on an ImportError."""
    global _WARNED_NO_NUMPY
    if not getattr(cfg, "fast_scan", True):
        return None
    try:
        from .fastscan import FastScanUnsupported
    except ImportError as e:
        if not _WARNED_NO_NUMPY:
            _WARNED_NO_NUMPY = True
            log.warning("fast scan unavailable (%s) — using the classic parser, which "
                        "is several times slower. Re-run: pip install -r requirements.txt", e)
        return None
    return FastScanUnsupported


def run_in_network_parse(cfg: MrfxConfig, make_parser, open_fn) -> tuple["InNetworkParser", ParseResult]:
    """Parse one in-network file: the byte-level fast path when enabled,
    else (or when it declines the file up front) the classic ijson path.
    `make_parser()` builds a fresh InNetworkParser; `open_fn()` opens a fresh
    stream context. Returns (parser, result)."""
    parser = make_parser()
    FastScanUnsupported = _fast_scan_unsupported(cfg)
    if FastScanUnsupported is not None:
        try:
            with open_fn() as stream:
                return parser, parser.parse_fast(stream)
        except FastScanUnsupported as e:
            if parser._flushed or parser.result.qa.rows:
                raise  # never happens (declines are up front) — never double-write
            log.info("%s: fast scan declined (%s) — using the classic parser",
                     parser.source_file, e)
            parser = make_parser()
    with open_fn() as stream:
        return parser, parser.parse(stream)


def _cited_ref_ids(item: dict) -> set[int]:
    out: set[int] = set()
    for grp in as_list(item.get("negotiated_rates")):
        if not isinstance(grp, dict):
            continue
        for x in as_list(grp.get("provider_references")):
            if isinstance(x, (dict, list)):
                continue
            rid = clean_ref_id(x)
            if rid is not None:
                out.add(rid)
    return out


def _is_target_item(item: dict, code_set) -> bool:
    """skim_needed_ref_ids' notion of a target item (string/number code)."""
    code = item.get("billing_code")
    if not isinstance(code, (str, int, float)) or isinstance(code, bool):
        return False
    return code_set is None or clean_code(code) in code_set


# the downloader's floor (fetch.py): never let scratch run the drive below it
SPOOL_FREE_FLOOR = 2 << 30
SPOOL_DISK_CHECK_BYTES = 64 << 20
REPLAY_TICK_BYTES = 1 << 20   # progress report cadence while replaying the spool


def _free_disk(path: Path) -> int:
    import shutil

    try:
        return shutil.disk_usage(path).free
    except OSError:
        return 1 << 62   # can't tell: don't block on a cosmetic probe


def run_big_file_fast(cfg: MrfxConfig, parser: "InNetworkParser", open_fn, spool_path,
                      progress=None) -> tuple[ParseResult, int]:
    """Big-file ingest in ONE full read (plus, if needed, a short read of the
    reference table), replacing skim-then-extract's two full reads.

    Pass 1 streams the whole file once: non-target members are skipped as raw
    bytes, the QA side effects of skipping are counted into `parser`, the
    TARGET in_network members are appended verbatim to an on-disk spool (they
    are the only bytes needed again), and the reference ids they cite are
    collected. References that come AFTER in_network are kept right there
    (the cited set is complete by then). References that came BEFORE it were
    skipped, so pass 2 re-reads the file only until provider_references
    closes and stops — it never re-scans in_network. Finally the spooled
    items are replayed through the normal _handle_item with every reference
    resolved. Memory: the kept references + one item at a time; the spool
    is on disk (`spool_path`, deleted here, swept as an orphan if killed).
    `progress` (optional) gets refs_closed() / second_pass() / replay(done,
    total) so a bar can follow the three phases (see SingleReadProgress).

    Returns (result, target_items). target_items == 0 means the file holds
    none of the target codes. Raises FastScanUnsupported — always before any
    row reaches the sink — if the file's shape can't be vouched for (the
    caller falls back to the classic two-pass with a FRESH parser)."""
    import struct

    from .fastscan import FastScanUnsupported, RefPolicy, decode_member, iter_members

    spool_path = Path(spool_path)
    if parser.code_set is None:
        # all-codes mode: EVERY item is a target, so the spool would be the
        # entire in_network section (~the uncompressed file) written as
        # scratch next to the store. The two-pass path needs no scratch disk
        # (and in all-codes mode its kept-reference set is the whole table
        # either way) — decline before creating anything
        raise FastScanUnsupported("all-codes mode — two-pass instead of a full-file spool")
    if _free_disk(spool_path.parent) < SPOOL_FREE_FLOOR:
        raise FastScanUnsupported("too little free disk for the single-read working copy")

    cited: set[int] = set()
    arrays: dict = {}
    parser._keep_ref_ids = cited          # _add_ref filters to the cited set
    policy = RefPolicy(active=False, ids=cited)
    refs_before = False
    target_items = 0
    counts: dict = {}
    spooled = next_disk_check = 0
    try:
        with open(spool_path, "wb") as spool:
            with open_fn() as stream:
                for array, data in iter_members(
                        stream, parser._want_item, None, on_header=parser._set_header,
                        counts=counts, target_codes=parser.code_set, ref_policy=policy,
                        on_type_other=parser._count_type_other):
                    if data is None:
                        arrays[array] = arrays.get(array, 0) + 1
                        if arrays[array] > 1 and array in ("in_network", "provider_references"):
                            # duplicate top-level keys (valid JSON, never seen
                            # in a real MRF): the classic parser's order-
                            # dependent reference resolution can't be replayed
                            # from a spool — decline; no row was emitted yet
                            raise FastScanUnsupported(f"more than one top-level {array} array")
                        if array == "provider_references" and not policy.active:
                            refs_before = True
                            if progress is not None:
                                progress.refs_closed()
                        elif array == "in_network":
                            policy.active = True  # later references: keep cited
                        continue
                    if array == "in_network":
                        item = decode_member(data)
                        if _is_target_item(item, parser.code_set):
                            target_items += 1
                        cited |= _cited_ref_ids(item)
                        spool.write(struct.pack("<Q", len(data)))
                        spool.write(data)
                        spooled += len(data) + 8
                        if spooled >= next_disk_check:
                            next_disk_check = spooled + SPOOL_DISK_CHECK_BYTES
                            if _free_disk(spool_path.parent) < SPOOL_FREE_FLOOR:
                                # no row has reached the sink yet: decline, and the
                                # caller's two-pass (no scratch disk) finishes it
                                raise FastScanUnsupported(
                                    "free disk fell under 2 GB while spooling — "
                                    "two-pass instead")
                    else:
                        parser._add_ref(decode_member(data))
        if refs_before and cited:
            if progress is not None:
                progress.second_pass()
            with open_fn() as stream:
                for array, data in iter_members(
                        stream, lambda events, final: False, None,
                        target_codes=parser.code_set,
                        ref_policy=RefPolicy(active=True, ids=cited)):
                    if array == "provider_references":
                        if data is None:
                            break         # the reference table is done: stop reading
                        parser._add_ref(decode_member(data))
        parser._refs_complete = True
        parser._own_table = arrays.get("provider_references", 0) > 0
        parser.saw_in_network = arrays.get("in_network", 0) > 0
        replayed = 0
        next_tick = 0
        with open(spool_path, "rb") as spool:
            while True:
                head = spool.read(8)
                if not head:
                    break
                (n,) = struct.unpack("<Q", head)
                parser._handle_item(decode_member(spool.read(n)))
                replayed += n + 8
                if progress is not None and replayed >= next_tick:
                    next_tick = replayed + REPLAY_TICK_BYTES
                    progress.replay(replayed, spooled)
        parser.result.items_seen = counts.get("in_network", 0)
        return parser._finish(), target_items
    finally:
        try:
            spool_path.unlink(missing_ok=True)
        except OSError:
            pass


def fast_big_file_available(cfg: MrfxConfig) -> bool:
    return _fast_scan_unsupported(cfg) is not None


def run_skim(cfg: MrfxConfig, open_fn, shape: dict | None = None) -> tuple[set[int], int]:
    """Pass 1 of a big file (see skim_needed_ref_ids), fast path first."""
    FastScanUnsupported = _fast_scan_unsupported(cfg)
    if FastScanUnsupported is not None:
        try:
            with open_fn() as stream:
                if shape is not None:
                    shape.clear()
                return skim_needed_ref_ids_fast(cfg, stream, shape)
        except FastScanUnsupported as e:
            log.info("fast skim declined (%s) — using the classic skim", e)
    with open_fn() as stream:
        if shape is not None:
            shape.clear()
        return skim_needed_ref_ids(cfg, stream, shape)


REF_BATCH = 200_000   # reference ids per write when streaming a reference file


def parse_provider_reference_file(cfg: MrfxConfig, stream, on_batch=None,
                                  ) -> tuple[str, str | None, dict[int, list[PGroup]]]:
    """Standalone provider-reference file -> (payer, last_updated_on, {ref_id: [PGroup]}).
    With `on_batch(refs)`, every REF_BATCH ids are handed over and dropped, so
    memory stays flat for a multi-million-id table; the returned dict then
    holds only the last partial batch (also handed over) — callers use the
    count they accumulated."""
    payer = "Unknown payer"
    last_updated = None
    refs: dict[int, list[PGroup]] = {}
    strings: dict = {}
    builder = None
    for prefix, event, value in ijson.parse(stream, use_float=True):
        if builder is not None:
            builder.event(event, value)
            if event == "end_map" and prefix == "provider_references.item":
                entry = builder.value
                rid = clean_ref_id(entry.get("provider_group_id"))
                if rid is not None:
                    groups = entry.get("provider_groups")
                    if groups is None and entry.get("location"):
                        # the providers are at a remote link MRF Explorer does
                        # not download: remembered as remote, so a rate file
                        # citing it counts remote_ref_groups (review 9 3.4)
                        from .refid import REMOTE_REF
                        refs[rid] = REMOTE_REF
                    else:
                        refs[rid] = share_strings(parse_groups(groups or []), strings)
                    if on_batch is not None and len(refs) >= REF_BATCH:
                        on_batch(refs)
                        refs = {}
                        strings = {}
                builder = None
            continue
        if event == "start_map" and prefix == "provider_references.item":
            builder = ijson.ObjectBuilder()
            builder.event(event, value)
        elif event == "string" and prefix == "reporting_entity_name":
            payer = cfg.normalize_payer(value)
        elif event == "string" and prefix == "last_updated_on":
            last_updated = value
    if on_batch is not None and refs:
        on_batch(refs)
    return payer, last_updated, refs
