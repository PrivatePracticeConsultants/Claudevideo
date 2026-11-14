"""Benchmarking & negotiation-pitch analytics (§7B).

All market math runs over **dollar-rate, base-modifier rows by default**
(no CQ/CO, no percentage rows) at the TIN/entity grain, pinned to an explicit
as-of month — deviations are labeled toggles passed in by the caller. The
opportunity model never invents utilization: volumes come from the caller.
"""

from __future__ import annotations

import datetime as dt
import html
import logging
import statistics

from .rounding import half_up, signed_pct
from . import progress
from . import __version__
from .catalog import code_info
from .config import MrfxConfig
from .modifiers import BASE_ASSISTANT_RULE_TEXT, BASE_RULE_TEXT, base_sql
from .store import BY_TIN_QUERY, LATEST_PARTITION, Store, mask_tin

log = logging.getLogger(__name__)

PERCENTILES = (10, 25, 40, 50, 75, 90)

BASIS_NOTE = (
    "Market statistics computed over dollar-rate, base-modifier rows "
    f"({BASE_RULE_TEXT}; no CQ/CO assistant rows, no percentage/per-diem rows), "
    "professional billing class unless stated otherwise."
)


# The one rule for "a practice's rate for a code" across payers (review 13
# item 4.2, decided: disclosed, not changed). It is the median over the
# practice's published base LINES on the market's basis — a payer that
# publishes the code under two place-of-service sets contributes two lines.
# Measured: collapsing to one rate per payer first would move 52 of 144,859
# practice x code rates (0.04 %) on the real 8-file store; the numbers are
# kept and the rule is printed with every all-payer figure.
PRACTICE_RATE_RULE = (
    "A practice's all-payer rate for a code is the median of its published base "
    "lines across payers: a payer that lists the code under more than one "
    "place-of-service set counts once per set (a per-payer figure is one rate "
    "per payer).")


def basis_note_for(market: dict) -> str:
    """BASIS_NOTE plus every toggle the query actually applied. The note must
    never claim a basis the SQL did not use: the fee schedule and contract
    gaps printed the bare BASIS_NOTE ("base-modifier, professional") even when
    market.base_only / billing_class / include_assistant had turned those off
    (round 6 P3) - only compute_benchmark appended the toggles."""
    m = market or {}
    return BASIS_NOTE + (
        f" [assistant rows INCLUDED by explicit toggle: {BASE_ASSISTANT_RULE_TEXT}]"
        if m.get("include_assistant") else ""
    ) + (
        " [non-dollar rows INCLUDED by explicit toggle]" if m.get("include_non_dollar") else ""
    ) + (
        # both escape hatches are API-reachable; the note must never
        # claim a basis the query didn't apply
        " [ALL modifier rows included: market.base_only=false]"
        if m.get("base_only", True) is False else ""
    ) + (
        " [all billing classes included: market.billing_class='']"
        if not m.get("billing_class", "professional") else ""
    )


# Fewer peer practices than this behind a percentile is flagged "thin" on
# every printed deliverable (the rate card's rule, schedule._THIN_PEERS).
THIN_PEERS = 5

# The opportunity models multiply a gap by the units the user typed — the
# PRACTICE's annual units. Per payer that assumes every unit is billed to
# that payer, which is why per-payer figures never add up across payers.
VOLUME_PER_PAYER_NOTE = (
    "Each payer is valued as if every one of the practice's annual units were "
    "billed to it, so the per-payer figures are alternatives, not a sum. For a "
    "combined figure, split the units by payer and run each payer on its own "
    "share.")

GHOST_RATE_NOTE = (
    "Ghost rates: payers publish rates for provider-code combinations that are "
    "never actually billed; presence of a rate does not mean a peer performs "
    "that code. Scope the market to a discipline or explicit code list to "
    "mitigate this — the market definition above states which filters were "
    "actually applied."
)
COLLECTION_NOTE = (
    "A published negotiated rate is not proof a peer collects it (contract "
    "vintages, carve-outs, lesser-of clauses). Benchmarks are directional "
    "market positioning, not a guarantee of attainability. A negotiated rate "
    "is also not per-visit revenue: MPPR, assistant-modifier reductions, "
    "sequestration, denials and patient cost-share sit between rate and cash."
)


class BenchmarkError(ValueError):
    pass


# Sentinel as-of "month": instead of a single calendar month, use each
# payer+provider+code line's NEWEST file. Payers republish their whole rate
# file monthly, so a line appears once per ingested month; pinning a month (or
# this "latest" collapse) never double-counts by pooling every month, and a
# line's newer publication fully supersedes its older one — see
# _rates_relation for the supersession grain and its one known limit.
LATEST_MONTH = "latest"


def is_latest(market: dict) -> bool:
    return str(market.get("month") or "").strip().lower() == LATEST_MONTH


# Every market key a client may send, by type (review 9 item 4.7). A key the
# app derives itself — `_plan_files`, `curated_tins`, anything starting with
# "_" — is NEVER taken from the wire: a client sending
# `_plan_files: ["alpha_2026-06.json"]` silently scoped Benchmark to one file
# ($40.00 with 9 peers instead of $41.00 with 10) and the echoed market did
# not show it. A wrong-typed value is a refusal in words; it used to reach
# DuckDB and come back as a 500 quoting the SQL ("can't be cast to VARCHAR[]
# … LINE 3: …").
_MARKET_TEXT = ("month", "prev_month", "billing_class", "discipline", "pos", "city",
                "plan", "peer_set", "primary_discipline", "entity_kind", "peer_group")
# who the comparison practices are (0.8.6, mrfx/naming.py practice_type)
PEER_GROUPS = {"clinics": "outpatient rehab clinics",
               "org": "practices with an organization NPI registered as a therapy clinic "
                      "or therapist group (261QP2000X, 225100000X …)",
               "therapy": "therapy practices (PT/OT/SLP clinician mix)",
               "all": "every practice the payer prices"}
_MARKET_FLAGS = ("base_only", "include_assistant", "include_non_dollar", "therapy_only",
                 "allow_national")
_MARKET_DERIVED = ("curated_tins",)


def _market_text(key: str, v):
    if v is None:
        return None
    if isinstance(v, bool) or not isinstance(v, (str, int, float)):
        raise BenchmarkError(f"market.{key} must be text, not {_kind(v)}")
    s = str(v).strip()
    if "\x00" in s:
        raise BenchmarkError(f"market.{key} contains a character that cannot be used")
    return s[:200]


def _market_flag(key: str, v):
    if v is None or isinstance(v, bool):
        return v
    if isinstance(v, (int, float)) and v in (0, 1):
        return bool(v)
    if isinstance(v, str) and v.strip().lower() in ("true", "false", "1", "0", "yes", "no", ""):
        return v.strip().lower() in ("true", "1", "yes")
    raise BenchmarkError(f"market.{key} must be true or false, not {_kind(v)}")


def _kind(v) -> str:
    return {dict: "an object", list: "a list", tuple: "a list", bool: "true/false",
            str: "text"}.get(type(v), "a number" if isinstance(v, (int, float)) else "that")


def _typed_market(market: dict) -> dict:
    m: dict = {}
    for k, v in market.items():
        k = str(k)
        if k.startswith("_") or k in _MARKET_DERIVED:
            continue                              # derived by the app, never the wire
        if k in _MARKET_TEXT:
            m[k] = _market_text(k, v)
        elif k in _MARKET_FLAGS:
            m[k] = _market_flag(k, v)
        elif k == "org_taxonomy":
            if v in (None, "", []):
                continue
            taxes = [v] if isinstance(v, str) else v
            if not isinstance(taxes, (list, tuple)):
                raise BenchmarkError("market.org_taxonomy must be a list of NPPES taxonomy codes")
            m[k] = sorted({_market_text("org_taxonomy", t).upper() for t in taxes
                           if t is not None and str(t).strip()})[:50]
        elif k == "codes":
            if v in (None, "", []):
                continue
            codes = [v] if isinstance(v, str) else v
            if not isinstance(codes, (list, tuple)):
                raise BenchmarkError("market.codes must be a list of billing codes")
            m[k] = [_market_text("codes", c).upper() for c in codes
                    if c is not None and str(c).strip()][:5000]
        elif k == "target_percentile":
            if v in (None, ""):
                continue
            try:
                if isinstance(v, bool):
                    raise ValueError
                m[k] = int(v)
            except (TypeError, ValueError):
                raise BenchmarkError(
                    f"market.target_percentile must be one of {PERCENTILES}") from None
        elif k in ("payers", "state"):
            m[k] = v                              # validated just below
        elif v is None or isinstance(v, (str, int, float, bool)):
            m[k] = v                              # unknown scalar: harmless, echoed
        # an unknown object/list key is dropped: nothing reads it
    return m


def normalize_market(market: dict | None) -> dict:
    """Copy of `market` with an explicit as-of vintage: an empty/missing month
    means LATEST_MONTH (newest file per contract). Every compute entry point
    runs this first, so the user never has to pick a month — while reports and
    methodology footers still carry an explicit vintage label (honesty rails
    downstream keep asserting month is set). Rate CHANGES are the exception:
    they compare two real months and keep their own strict validation."""
    # Defensive at the boundary, because every compute entry point runs this
    # first and its output goes straight into SQL parameters. Fuzzing found
    # three 500 classes that all land here: a non-dict market ('.get' on a
    # string), a NUMERIC month (DuckDB then tries to cast the whole VARCHAR
    # file_month column to INT32), and non-string payers (VARCHAR vs
    # INTEGER/STRUCT binder errors). Bad shape is a 422-able refusal; bad
    # VALUES are coerced to strings, which simply match nothing — honest.
    if market is not None and not isinstance(market, dict):
        raise BenchmarkError("market must be an object of filters, "
                             "e.g. {\"month\": \"latest\"}")
    m = _typed_market(market or {})
    if m.get("include_non_dollar"):
        # (API only.) A percentage-of-charges or per-diem "rate" is not a
        # price in dollars: pooled, an 80 (%) and a $40 gave a $60.00
        # median that no contract pays, while the rate card printed $40.00
        # (review 9 item 4.9). Refused rather than blended.
        raise BenchmarkError(
            "percentage and per-diem rates cannot be pooled with dollar rates — "
            "these views compare dollars. See them in the Explorer (untick "
            "'dollar rates only').")
    if m.get("peer_group") is not None:
        g = str(m["peer_group"]).strip().lower()
        if g in ("", "all"):
            m.pop("peer_group")
        elif g not in PEER_GROUPS:
            raise BenchmarkError("market.peer_group must be one of: " + ", ".join(PEER_GROUPS))
        else:
            m["peer_group"] = g
    if not str(m.get("month") or "").strip():
        m["month"] = LATEST_MONTH
    else:
        m["month"] = str(m["month"]).strip()
    if "prev_month" in m and m.get("prev_month") is not None:
        m["prev_month"] = str(m["prev_month"]).strip()
    payers = m.get("payers")
    if payers is not None:
        if isinstance(payers, str):          # a single payer is natural input
            payers = [payers]
        elif not isinstance(payers, (list, tuple)):
            raise BenchmarkError("payers must be a list of payer names")
        if any(isinstance(p, (dict, list, tuple)) for p in payers):
            raise BenchmarkError("payers must be a list of payer names")
        m["payers"] = [str(p).strip() for p in payers
                       if p is not None and str(p).strip()]
    # A state has to be a state. This used to be truncated to two characters
    # wherever it was consumed, so "Missouri" quietly became "MI" — Michigan's
    # peers, schedules and hospitals under a Missouri heading.
    if m.get("state") is not None:
        from .states import state_code
        try:
            m["state"] = state_code(m["state"])
        except ValueError as exc:
            raise BenchmarkError(str(exc)) from exc
    return m


def clean_volumes(volumes) -> dict[str, float]:
    """{billing code -> annual units} off the wire. A non-dict (fuzzing sent
    a bare string, whose .items() 500'd) or a non-numeric unit is a refusal,
    never an unhandled exception. Codes are upcased to match the store's."""
    if volumes in (None, "", {}, []):
        return {}
    if not isinstance(volumes, dict):
        raise BenchmarkError(
            'volumes must map a billing code to annual units, e.g. {"97110": 4200}')
    try:
        out = {str(k).strip().upper(): float(v) for k, v in volumes.items()}
    except (TypeError, ValueError):
        raise BenchmarkError(
            "volumes must map a billing code to annual units (numbers)")
    # A negative unit count is not a quantity. Left unchecked it produced a
    # NEGATIVE opportunity figure (-$12,000 on a -4,000-unit input) that flowed
    # into the pitch report, the proposal and the win report, and it weighted a
    # position calculation backwards. Found by the growth-feature audit.
    bad = sorted(k for k, v in out.items() if v < 0)
    if bad:
        raise BenchmarkError(
            "annual units cannot be negative — check "
            + ", ".join(bad[:5])
            + (" and others" if len(bad) > 5 else ""))
    if any(v != v or v in (float("inf"), float("-inf")) for v in out.values()):
        raise BenchmarkError("annual units must be real numbers")
    return out


def public_market(market: dict, drop: tuple = ()) -> dict:
    """The market as the USER stated it, for display, reports and provenance.

    Keys starting with '_' are resolved implementation detail (a plan name
    becomes a file list; that list can be hundreds of names). They must never
    reach the market-definition line of a methodology footer, which is meant to
    be a human-readable restatement of what was asked for."""
    return {k: v for k, v in market.items()
            if not k.startswith("_") and k not in drop}


def market_in_words(market: dict) -> str:
    """The market definition as a sentence for a client-facing footer. Every
    report printed it as raw JSON — `Market definition: {"month": "latest",
    "billing_class": "professional", …}` (review 9 item 4.9)."""
    m = public_market(market or {})
    parts = []
    month = str(m.get("month") or "latest")
    parts.append("each payer's newest published month" if month.lower() == "latest"
                 else f"the published month {month}")
    if m.get("prev_month"):
        parts.append(f"compared with {m['prev_month']}")
    parts.append(f"practices in {m['state']}" if m.get("state")
                 else "practices in every state")
    if m.get("city"):
        parts.append(f"in the city of {m['city']}")
    payers = m.get("payers") or []
    parts.append(("payer " if len(payers) == 1 else "payers ") + ", ".join(payers)
                 if payers else "every payer")
    if m.get("plan"):
        parts.append(f"plan {m['plan']}")
    if m.get("discipline"):
        parts.append(f"{m['discipline']} lines")
    if m.get("codes"):
        codes = list(m["codes"])
        parts.append("codes " + ", ".join(codes[:12]) + (" and more" if len(codes) > 12 else ""))
    if m.get("therapy_only"):
        parts.append("therapy practices only")
    if m.get("peer_group") in PEER_GROUPS:
        parts.append(f"compared with {PEER_GROUPS[m['peer_group']]}")
    if m.get("org_taxonomy"):
        parts.append("organization NPI taxonomy " + ", ".join(m["org_taxonomy"]))
    if m.get("peer_set"):
        parts.append(f"peer set {m['peer_set']}")
    cls = m.get("billing_class", "professional")
    parts.append(f"{cls} claims" if cls else "every billing class")
    parts.append("base modifiers only" if m.get("base_only", True) else "every modifier")
    if m.get("include_assistant"):
        parts.append("assistant (CQ/CO) lines included")
    if m.get("include_non_dollar"):
        parts.append("percentage and per-diem lines kept apart from dollars")
    if m.get("target_percentile") not in (None, 50):
        parts.append(f"target p{m['target_percentile']}")
    if m.get("pos"):
        parts.append(f"place of service {m['pos']}")
    return "; ".join(parts)


def _sql_str_list(values) -> str:
    """Inline a list of strings as SQL literals. Used only for the FROM-clause
    relation, which is composed before the WHERE parameters and so cannot carry
    placeholders of its own without corrupting their order."""
    return ", ".join("'" + str(v).replace("'", "''") + "'" for v in values)


def spine_relation(market: dict) -> str:
    """The TIN-grain spine a market reads, aliased `t` by the caller.

    Normally the prebuilt `rates_by_tin`. Under a PLAN scope it is rebuilt on
    the fly from the raw rates of that plan's files, because the prebuilt spine
    has already aggregated ACROSS files: a TIN priced 28 in the narrow book and
    40 in the broad one is stored as a single row at their median (34) with
    only a representative source_file. There is no WHERE clause over that spine
    that recovers 28 — the blend happened before the filter could see it. Since
    this reuses store.BY_TIN_QUERY verbatim, only its input changes, so a
    plan-scoped number is computed exactly like the pooled one.

    Cost is a filtered scan of the plan's files rather than a lookup on the
    prebuilt spine; source_file is constant within each parquet part, so DuckDB
    prunes the rest of the store by row-group zonemap."""
    files = market.get("_plan_files")
    if files is None:
        return "rates_by_tin"
    if not files:                      # a plan with no ingested file: empty, never "everything"
        return f"({BY_TIN_QUERY.format(part='FALSE')})"
    scoped = BY_TIN_QUERY.format(part=f"source_file IN ({_sql_str_list(files)})")
    # the spine's trailing ORDER BY exists to cluster the MATERIALIZED table;
    # inside a subquery it is a pure cost, and every reader re-aggregates.
    # The newline before ')' is load-bearing: the query's last line is a SQL
    # comment, which would otherwise swallow the closing paren.
    return f"({scoped.rsplit('ORDER BY billing_code, tin_value', 1)[0]}\n)"


def _rates_relation(market: dict, prefilter: str = "") -> str:
    """The FROM-relation for market queries, aliased `t` by the caller.
    Pinned month → plain `rates_by_tin` (the file_month = ? clause in
    _market_where selects that snapshot). "Latest" → a subquery that keeps, per
    (payer, TIN, billing code), ALL rows of that line's newest file_month.

    Supersession is at the payer+TIN+code level on purpose. Finer (the full
    variant key incl. modifier/POS sets) blended vintages: when a payer's newer
    file restructures a line — drops the 'GP' variant, widens POS '11' to
    '11|12' — the old variant had no newer twin, survived forever, and dragged
    the median (measured: June GP 34.50 pooling with July 37.95 → 36.22).
    Coarser (per payer) would drop whole regions when a payer's shard files
    update on different cadences. Known limit: a code line absent from ALL of a
    payer's newer files lingers at its last-seen vintage.

    `prefilter` is SQL applied INSIDE the "latest" subquery, before the window.
    Only ever pass predicates on the PARTITION KEY columns (payer, tin_value,
    tin_is_really_npi, billing_code): those select whole partitions, so the
    max(file_month) each surviving row sees is identical to the unfiltered
    result. Filtering on anything else (a rate bound, a modifier, a month)
    would remove rows from WITHIN a partition and could promote a stale
    vintage to "latest" — silently wrong numbers.

    This matters because a window function cannot use the caller's WHERE: the
    unfiltered form sorts the ENTIRE spine on every call, so its cost grows
    with total store size even when the question is about one practice
    (measured: 0.64s -> 1.96s as the spine went 800k -> 3.2M rows, while the
    same query against a pinned month stayed flat at ~0.4s)."""
    base = spine_relation(market)
    where = f" WHERE {prefilter}" if prefilter else ""
    if is_latest(market):
        if base == "rates_by_tin":
            # the materialized spine carries the answer (store.SPINE_QUERY's
            # is_latest, the same partition as below): a scan filter instead
            # of a window over the whole spine on every report
            return (f"(SELECT * FROM rates_by_tin t0 WHERE t0.is_latest"
                    f"{' AND (' + prefilter + ')' if prefilter else ''})")
        # a PLAN scope's spine is built on the fly from that plan's files —
        # its newest months are worked out here. tin_is_really_npi is part of
        # the identity: an EIN row and a same-digit-string npi-typed row are
        # different providers — one must never supersede the other
        return (f"(SELECT * FROM {base} t0{where} QUALIFY file_month = max(file_month) "
                f"OVER (PARTITION BY {LATEST_PARTITION}))")
    # A prefilter is ALWAYS consumed, pinned month included — it is a plain
    # pushed-down filter there (no window to perturb), and a caller that binds
    # its placeholders must not have to guess whether the relation took them.
    # Guessing by string ("is the relation still literally rates_by_tin?") broke
    # the moment a plan scope made the pinned-month relation a subquery too.
    return f"(SELECT * FROM {base} t0{where})" if prefilter else base


def anchor_state(store: Store, market: dict, tins: list[str] | None = None) -> str | None:
    """The state whose Medicare locality a % of Medicare should use: the
    market's state if set, else the main state of the subject's practice(s)
    (tin_directory.primary_state, most common among its TINs, ties
    alphabetical), else None."""
    st = (market or {}).get("state")
    if st:
        return str(st).upper()
    if not tins:
        return None
    try:
        with store.connect() as con:
            row = con.execute(
                "SELECT primary_state FROM tin_directory WHERE tin_value IN "
                "(SELECT unnest(?::VARCHAR[])) AND primary_state IS NOT NULL "
                "GROUP BY primary_state ORDER BY count(*) DESC, primary_state LIMIT 1",
                [list(tins)]).fetchone()
    except Exception:  # noqa: BLE001 — no directory yet: no state context
        return None
    return row[0] if row else None


def month_label(month) -> str:
    """Human/report label for an as-of value."""
    return ("latest available (newest file per contract)"
            if str(month or "").strip().lower() == LATEST_MONTH else str(month))


def _market_where(market: dict, include_assistant: bool, include_non_dollar: bool) -> tuple[str, list]:
    """Shared WHERE over rates_by_tin joined tin_directory (alias t/td).
    Pair with _rates_relation(market) as the FROM-relation — for "latest" the
    relation already reduces to one row per contract, so no file_month filter."""
    clauses, params = ["t.tin_value IS NOT NULL", "NOT t.tin_is_really_npi"], []
    # A plan scope is applied by spine_relation, not here. If a market names a
    # plan that no entry point resolved, the query would run POOLED and the
    # answer would be labelled with the plan's name — a fabricated provenance.
    # Refuse instead: a view that wants plan scoping calls resolve_plan_scope.
    if market.get("plan") and "_plan_files" not in market:
        raise BenchmarkError(
            f"this view can't be scoped to a plan yet (asked for "
            f"{str(market['plan'])[:80]!r}) — remove the plan filter, or use the "
            "Benchmark, Negotiate or Rate-card views, which support it")
    month = market.get("month")
    if not month:
        raise BenchmarkError("an as-of month is required (7A.5) — pass market.month (or 'latest')")
    if not is_latest(market):
        clauses.append("t.file_month = ?")
        params.append(month)
    payers = market.get("payers") or []
    if payers:
        clauses.append(f"t.payer IN ({', '.join('?' for _ in payers)})")
        params += payers
    if not include_non_dollar:
        clauses.append("t.is_dollar_rate")
    # placeholder guard: $0 / $0.01 / negative dollar "rates" are payer
    # placeholders (the parser flags them as zero_rates), never real negotiated
    # prices — including them drags the market's p10/p25 (and any subject
    # median that mixes a placeholder with a real value) below the truth. Only
    # applies to dollar rows; a percentage row like 1.5 (150%) stays.
    clauses.append("(NOT t.is_dollar_rate OR t.negotiated_rate > 0.01)")
    if not include_assistant:
        clauses.append("t.modifier_set NOT LIKE '%CQ%' AND t.modifier_set NOT LIKE '%CO%'")
    if market.get("base_only", True):
        # ONE definition (mrfx/modifiers.py), shared with the Explorer's
        # "base modifiers only" filter. With include_assistant, any mix of
        # GP/GO/GN and CQ/CO — without it base_only would filter the assistant
        # rows straight back out and the toggle would change nothing but the
        # methodology note. KX/59/X{EPSU}/52/26/TC... rows stay excluded.
        clauses.append(base_sql("t.modifier_set", include_assistant))
    if market.get("billing_class", "professional"):
        clauses.append("t.billing_class = ?")
        params.append(market.get("billing_class", "professional"))
    # PLAN scope. A payer publishes one rate file shared by many plans, so the
    # plan lives on the FILE (from the TOC), not the rate row. The spine keeps
    # one filename per (payer, TIN, code) group plus a source_count, so this is
    # EXACT for single-file groups and would be a guess for multi-file ones —
    # those are excluded rather than half-counted, and the caller discloses how
    # many. Without this a payer's narrow-network and broad-PPO books blend
    # into one median that matches neither.
    # NOTE: a plan scope is NOT a clause here. It restricts the spine's INPUT
    # (see spine_relation) because the prebuilt spine has already blended the
    # files a plan scope wants to separate. Pair this WHERE with a relation
    # from spine_relation/_rates_relation or the scope is silently ignored.
    if market.get("discipline"):
        clauses.append("(t.discipline = ? OR t.discipline = 'unspecified')")
        params.append(market["discipline"])
    if market.get("pos"):
        clauses.append("('|' || t.service_code_set || '|') LIKE ('%|' || ? || '|%')")
        params.append(str(market["pos"]))
    attr, attr_params = _practice_attr_where(market)
    return " AND ".join(clauses + attr), params + attr_params


# The market filters that describe the PEER population, not the practice
# being benchmarked. The subject is whoever the user named: its own rate is
# the same on every page — Benchmark, Negotiate, the rate card — whatever
# state / city / therapists-only scope the peers are drawn from (audit 8
# P1-2: a MO+KS practice read "your rate $40" on Benchmark, $50 on the
# rate card; by hand, median(40, 60) = $50).
PEER_ONLY_KEYS = ("therapy_only", "state", "city", "peer_group", "org_taxonomy")


def subject_market(market: dict) -> dict:
    return {k: v for k, v in market.items() if k not in PEER_ONLY_KEYS}


def _practice_attr_where(market: dict) -> tuple[list, list]:
    """The market clauses that select WHOLE practices or whole codes (never
    some of a practice's lines for a code): practice attributes from the
    directory (alias td) and the code list. A per-(code, practice) median is
    unchanged by them, which is what lets the materialised medians
    (practice_base_sql) apply them afterwards. _market_where ends with these
    — one definition for both paths."""
    clauses, params = [], []
    if market.get("therapy_only"):
        # only PT/OT/SLP providers & therapy practices (by NPPES taxonomy) — the
        # market and the subject both restrict to real therapists, so the
        # benchmark isn't diluted by the MDs/DOs/NPs who billed a 97xxx code.
        clauses.append("coalesce(td.is_therapy, FALSE)")
    group = market.get("peer_group")
    if group == "clinics":
        # outpatient rehab clinics: org taxonomy, clinician mix, Medicare
        # private-practice billing, not at a hospital / SNF address
        clauses.append("td.practice_type = 'outpatient_clinic'")
    elif group == "therapy":
        clauses.append("coalesce(td.is_therapy, FALSE)")
    elif group == "org":
        # the organization NPI's own NPPES registration alone: one a payer
        # lists, or the NPPES organization of the practice's legal name — not
        # a hospital / facility that also registered a rehab NPI
        clauses.append("coalesce(td.org_clinic, FALSE) "
                       "AND td.practice_type NOT IN ('hospital', 'facility')")
    if market.get("org_taxonomy"):
        clauses.append("list_has_any(coalesce(td.org_taxonomies, []::VARCHAR[]), ?::VARCHAR[])")
        params.append(list(market["org_taxonomy"]))
    if market.get("state"):
        clauses.append("list_contains(td.states, ?)")
        params.append(market["state"].upper())
    if market.get("city"):
        clauses.append(
            "len(list_filter(coalesce(td.cities, []), c -> upper(c) = upper(?))) > 0"
        )
        params.append(market["city"])
    if market.get("codes"):
        codes = market["codes"]
        clauses.append(f"t.billing_code IN ({', '.join('?' for _ in codes)})")
        params += codes
    return clauses, params


# Round 7 P1.2 — the house-basis per-practice medians, materialised once per
# spine generation (Store.spine_materialised). Every benchmark-family reader
# (benchmark, negotiation, contract gaps, leads, leaderboard, roster, size
# premium, the fee schedule's peers, and through them the digest, packets and
# dossier) computed "median per (code, practice[, payer]) over the latest
# spine" from scratch: 2-8 s each on a 99M-rate store. Two tables, because a
# median of per-payer medians is NOT the all-payer median: (a) per
# (billing_code, tin_value) over ALL that practice's lines; (b) per (payer,
# billing_code, tin_value). The version is in the name: change the SQL, bump it.
PRACTICE_MEDIANS = "practice_medians_v1"
PAYER_PRACTICE_MEDIANS = "practice_medians_payer_v1"
# (c) per (discipline, payer, billing_code, tin_value) — the house basis with
# the discipline LINE filter (a discipline's own lines plus untagged ones,
# _market_where's rule) applied per discipline. Round 12 item 3: the Rate
# card's "Visit value in this market" is confined to one discipline; without
# this table every request was a live GROUP BY over the spine.
DISC_PAYER_PRACTICE_MEDIANS = "practice_medians_disc_payer_v1"
DISCIPLINES = ("PT", "OT", "SLP")


def _house_eligible(market: dict) -> bool:
    """True when `market` is on the HOUSE basis the tables are built on:
    latest, no plan scope, dollar rows, base modifiers, professional, no
    assistant rows, no discipline/POS line filter. Anything else reads the
    live query (exact, just not precomputed)."""
    return (is_latest(market) and market.get("_plan_files") is None
            and not market.get("plan")
            and not market.get("include_assistant")
            and not market.get("include_non_dollar")
            and bool(market.get("base_only", True))
            and market.get("billing_class", "professional") == "professional"
            and not market.get("discipline") and not market.get("pos"))


def _house_disc_eligible(market: dict) -> bool:
    """The house basis with ONE discipline (PT / OT / SLP) as the only line
    filter beyond it — what the per-discipline table holds."""
    return (market.get("discipline") in DISCIPLINES
            and _house_eligible({k: v for k, v in market.items() if k != "discipline"}))


def _house_disc_table_sql() -> tuple[str, list]:
    house = {"month": LATEST_MONTH}
    where, params = _market_where(house, False, False)
    disc_values = ", ".join(f"('{d}')" for d in DISCIPLINES)
    return (f"SELECT d.discipline, t.payer, t.billing_code, t.tin_value, "
            f"median(t.negotiated_rate) AS rate "
            f"FROM {_rates_relation(house)} t, (VALUES {disc_values}) d(discipline) "
            f"WHERE {where} AND (t.discipline = d.discipline OR t.discipline = 'unspecified') "
            f"GROUP BY d.discipline, t.payer, t.billing_code, t.tin_value "
            f"ORDER BY t.billing_code, t.tin_value, d.discipline, t.payer"), params


def _house_table_sql(per_payer: bool) -> tuple[str, list]:
    house = {"month": LATEST_MONTH}
    where, params = _market_where(house, False, False)
    keys = "t.payer, t.billing_code, t.tin_value" if per_payer else "t.billing_code, t.tin_value"
    # ordered by code so a code-filtered read prunes row groups
    return (f"SELECT {keys}, median(t.negotiated_rate) AS rate "
            f"FROM {_rates_relation(house)} t WHERE {where} "
            f"GROUP BY {keys} ORDER BY t.billing_code, t.tin_value"
            + (", t.payer" if per_payer else "")), params


def practice_base_sql(store: Store, market: dict, *, per_payer: bool = False,
                      extra: str = "", extra_params=(), prefilter: str = "",
                      prefilter_params=(), dollar_only: bool = False,
                      scoped_if_cold: bool = False) -> tuple[str, list]:
    """(sql, params) for ONE median rate per (billing_code, tin_value) — or
    per (payer, billing_code, tin_value) with `per_payer` — under `market`.
    Columns: [payer,] billing_code, tin_value, rate.

    On the house basis it reads the materialised medians and applies the
    market's practice/code filters (and a payer list on the per-payer
    table) afterwards; otherwise it is the live GROUP BY over the spine.
    Both paths return the same rows to the last bit
    (tests/mrfx/test_round7.py runs every reader both ways).

    `extra` (" AND …") may name only t.payer, t.billing_code, t.tin_value
    and td.*; its params bind after the market's. `prefilter` narrows the
    live relation (see _rates_relation). `dollar_only` adds is_dollar_rate
    on the live path (the table holds dollar rows only).

    `scoped_if_cold` (a caller whose `prefilter` narrows the live query to
    a few payers / codes): read the materialised table only when it is
    already built for the spine as it stands; otherwise answer from the
    scoped live query — the same rows — and build the table in the
    background for next time. Building it in the request cost the whole
    spine on nearly every rate card while files load or an analytics
    build runs (each changes the spine's key)."""

    def table(name: str, built) -> str | None:
        if not (scoped_if_cold and prefilter):
            return store.spine_materialised(name, *built())
        rel = store.spine_materialised_current(name)
        if rel is None:
            store.warm_spine_materialised(name, *built())
        return rel
    ia = bool(market.get("include_assistant"))
    ind = bool(market.get("include_non_dollar"))
    where, params = _market_where(market, ia, ind)       # validates the market too
    keys = (["t.payer"] if per_payer else []) + ["t.billing_code", "t.tin_value"]
    payers = market.get("payers") or []
    rel = None
    if per_payer and _house_disc_eligible(market):
        rel = table(DISC_PAYER_PRACTICE_MEDIANS, _house_disc_table_sql)
    if rel is not None:
        clauses, cparams = ["t.discipline = ?"], [market["discipline"]]
        if payers:
            clauses.append(f"t.payer IN ({', '.join('?' for _ in payers)})")
            cparams += payers
        attr, attr_params = _practice_attr_where(market)
        clauses += attr
        cparams += attr_params
        join = (" LEFT JOIN tin_directory td USING (tin_value)"
                if any("td." in c for c in clauses) or "td." in extra else "")
        sql = (f"SELECT {', '.join(keys)}, t.rate FROM {rel} t{join} "
               f"WHERE {' AND '.join(clauses)}{extra}")
        return sql, [*cparams, *extra_params]
    if _house_eligible(market) and (per_payer or len(payers) <= 1):
        use_payer = per_payer or len(payers) == 1
        name = PAYER_PRACTICE_MEDIANS if use_payer else PRACTICE_MEDIANS
        rel = table(name, lambda: _house_table_sql(use_payer))
    if rel is not None:
        clauses, cparams = [], []
        if payers:
            clauses.append(f"t.payer IN ({', '.join('?' for _ in payers)})")
            cparams += payers
        attr, attr_params = _practice_attr_where(market)
        clauses += attr
        cparams += attr_params
        join = (" LEFT JOIN tin_directory td USING (tin_value)"
                if any("td." in c for c in clauses) or "td." in extra else "")
        sql = (f"SELECT {', '.join(keys)}, t.rate FROM {rel} t{join} "
               f"WHERE {' AND '.join(clauses) or 'TRUE'}{extra}")
        return sql, [*cparams, *extra_params]
    rel = _rates_relation(market, prefilter)
    sql = (f"SELECT {', '.join(keys)}, median(t.negotiated_rate) AS rate "
           f"FROM {rel} t LEFT JOIN tin_directory td USING (tin_value) "
           f"WHERE {where}{' AND t.is_dollar_rate' if dollar_only else ''}{extra} "
           f"GROUP BY {', '.join(keys)}")
    return sql, [*(prefilter_params if prefilter else ()), *params, *extra_params]


def mapped_entity_name(store: Store, subject: str) -> str | None:
    """The entity-map name `subject` refers to (case-insensitive), else None.
    A mapped entity is titled with ITS name, not one member's NPPES name
    ("Practice profile — NORTH CLINIC" over BIG GROUP's numbers; review 9 4.4)."""
    folded = str(subject or "").strip().casefold()
    for name in store.entity_map().values():
        if str(name).strip().casefold() == folded:
            return str(name)
    return None


def subject_tins_for_market(store: Store, subject: str, market: dict) -> list[str]:
    """The subject's TINs for a report scoped by `market` — ONE rule for
    Benchmark, Negotiate and the rate card (audit 8 P1-2).

    A bare organization NAME that practices in several states use is several
    practices (the Explorer lists "NAME · MO" and "NAME · KS"): with a market
    state, it means the one in that state, as the Explorer's entity does. A
    mapped entity, an "NAME · ST" key, a TIN or an NPI is exactly what the
    user named and keeps every rate it has, whatever state or therapists-only
    scope the PEERS are drawn from (subject_market). Falls back to every TIN
    when none of the same-named practices is in the state."""
    if market is not None and not isinstance(market, dict):
        # round 10 routed seven more surfaces through here: a non-object
        # market must be a 422 sentence on every one, never a .get 500
        raise BenchmarkError("market must be an object of filters")
    market = market or {}
    tins = resolve_subject_tins(store, subject)
    st = str(market.get("state") or "").strip().upper()
    if not st or len(tins) < 2 or " · " in subject:
        return tins
    folded = subject.strip().casefold()
    if any(str(n).strip().casefold() == folded for n in store.entity_map().values()):
        return tins                       # a mapped entity: one practice by definition
    if folded.replace("-", "").replace(" ", "").isdigit():
        return tins                       # a TIN or an NPI: exactly what was named
    with store.connect() as con:
        rows = con.execute(
            "SELECT tin_value, primary_state, lower(display_name) FROM tin_directory "
            "WHERE tin_value IN (SELECT unnest(?::VARCHAR[]))", [tins]).fetchall()
    # a name — typed in full or in part ("ACME" for "ACME REHAB") — that
    # resolves to ONE practice name used in several states takes the state
    # rule; a partial name used to resolve to every state's TINs ($50.50 p70
    # against $41.00 p10 for the full name; review 9 item 4.4)
    if not rows or len({name for _t, _s, name in rows}) != 1:
        return tins
    states = {s for _t, s, _n in rows}
    if len(states) < 2:
        return tins
    here = sorted(t for t, s, _n in rows if s == st)
    return here or tins


def resolve_subject_tins(store: Store, subject: str) -> list[str]:
    """Subject may be a manually-mapped entity name, an AUTO-grouped org name
    (every TIN whose NPPES organization name is `subject`), an NPI, or a raw TIN.

    NPIs are accepted because the app SHOWS them everywhere — the NPI grain, the
    entity detail panel, the exports — so pasting one into a subject box is the
    natural thing to do. Without this it fell through to the raw-TIN branch,
    matched nothing (rates are keyed by TIN, and the market basis further
    excludes NPI-in-the-TIN-slot rows), and every report came back "no published
    rates for this practice" for a provider that plainly has them.
    """
    return sorted(_resolve_subject_tins(store, subject))


def _resolve_subject_tins(store: Store, subject: str) -> list[str]:
    # (sorted by the caller: the "Subject tax IDs" line of a rate card came
    # back in three orders in six calls — invariant 7, review 9 item 4.4)
    emap = store.entity_map()
    tins = [t for t, name in emap.items() if name == subject]
    if tins:
        return tins
    # case-insensitive twin of the exact map match: a user who types their own
    # client's name in lower case must not fall through to the raw-TIN branch
    folded = subject.strip().casefold()
    tins = [t for t, name in emap.items() if str(name).strip().casefold() == folded]
    if tins:
        return tins
    ident = subject.replace("-", "").strip()
    with store.connect() as con:
        # "Unnamed group — 2 clinicians (NASHVILLE)" describes a practice; it
        # is not a name, and unrelated practices can share it — never pool
        unnamed = [r[0] for r in con.execute(
            "SELECT tin_value FROM tin_directory WHERE name_source = 'unnamed_group' "
            "AND lower(display_name) = lower(?) ORDER BY tin_value", [subject]).fetchall()]
        if len(unnamed) == 1:
            return unnamed
        if unnamed:
            raise BenchmarkError(
                f"{len(unnamed)} different practices are labelled \"{subject}\" (none has an "
                "organization name on file) — pick one by its tax ID: "
                + ", ".join(mask_tin(t) for t in unnamed[:10])
                + (" …" if len(unnamed) > 10 else "") + ".")
        # auto-grouped org: the same NPPES display_name the entity grain groups on
        named = [r[0] for r in con.execute(
            "SELECT tin_value FROM tin_directory WHERE display_name = ? "
            "AND name_source <> 'unnamed_group'", [subject],
        ).fetchall()]
        if named:
            return named
        # Same name, different case ("gateway physical therapy"). Payers publish
        # names in wildly inconsistent case, so an exact-case-only match made a
        # hand-typed name silently return NOTHING — which reads as "this
        # practice has no rates" rather than "that isn't how it's spelled here".
        named = [r[0] for r in con.execute(
            "SELECT tin_value FROM tin_directory WHERE lower(display_name) = lower(?) "
            "AND name_source <> 'unnamed_group'",
            [subject],
        ).fetchall()]
        if named:
            return named
        # the Explorer's entity key for an organization: "NAME · ST" (name +
        # the practice's main state — api._ENTITY_KEY_SQL)
        if " · " in subject:
            nm, _, st = subject.rpartition(" · ")
            named = [r[0] for r in con.execute(
                "SELECT tin_value FROM tin_directory WHERE lower(display_name) = lower(?) "
                "AND entity_kind = 'org' AND primary_state = upper(?) ORDER BY tin_value",
                [nm.strip(), st.strip()],
            ).fetchall()]
            if named:
                return named
        # A 10-digit all-digit id is an NPI (TINs/EINs are 9). Only treat it as
        # one when it isn't already a known TIN, so a payer that publishes NPIs
        # in the TIN slot still resolves to itself.
        if len(ident) == 10 and ident.isdigit():
            known_tin = con.execute(
                "SELECT 1 FROM tin_directory WHERE tin_value = ? LIMIT 1", [ident],
            ).fetchone()
            if not known_tin:
                # only the parts that hold this NPI (the kept per-part NPI
                # sets) — a scan of every raw rate took 22 s on a 99M-rate
                # store and ran several times per rate card
                by_npi = store.tins_billing_npi(ident)
                if by_npi:
                    return by_npi
        # A partial name ("Gateway" for "Gateway Physical Therapy"). Only when
        # it identifies exactly ONE practice: several matches is ambiguous, and
        # silently picking one would put another practice's rates under your
        # client's name. Digits are excluded — a partial TIN/NPI is a typo, not
        # a name, and must keep falling through to the raw-id branch below.
        if len(subject.strip()) >= 4 and not ident.isdigit():
            like = [(r[0], r[1]) for r in con.execute(
                "SELECT tin_value, display_name FROM tin_directory "
                "WHERE display_name ILIKE '%' || ? || '%'", [subject.strip()],
            ).fetchall()]
            names = {n for _t, n in like}
            if len(names) == 1:
                return [t for t, _n in like]
            if len(names) > 1:
                shown = ", ".join(sorted(names)[:6])
                raise BenchmarkError(
                    f"'{subject}' matches {len(names)} practices ({shown}"
                    f"{'…' if len(names) > 6 else ''}) — type more of the name, "
                    "or pick one from the suggestions.")
    return [ident]


def resolve_plan_scope(store: Store, market: dict) -> dict:
    """Turn `market["plan"]` into the concrete file list `_market_where` uses.

    Call this once per entry point, right after normalize_market: the plan
    name is user-facing, the file list is the implementation, and resolving it
    in one place keeps every tab's plan scoping identical."""
    name = str(market.get("plan") or "").strip()
    if not name:
        return market
    files = store.files_for_plan(name)
    if not files:
        raise BenchmarkError(
            f"no ingested files are tagged with the plan {name!r} — plan tags "
            "come from a payer's table-of-contents, so files added by pasting "
            "a direct rate link have none")
    return {**market, "_plan_files": files}


def plan_coverage(store: Store, payer: str | None = None) -> dict:
    """What plan information exists, stated honestly.

    `blended` is the finding that matters: a payer whose ingested files serve
    MORE THAN ONE plan is one whose pooled median mixes networks that may
    price very differently."""
    plans = store.plans_for_payer(payer)
    by_payer: dict[str, list] = {}
    for p in plans:
        by_payer.setdefault(p["payer"] or "(unknown payer)", []).append(p)
    return {
        "plans": plans,
        "by_payer": [{"payer": k, "n_plans": len(v),
                      "market_types": sorted({x["market_type"] for x in v if x["market_type"]}),
                      "blended": len(v) > 1,
                      "plans": v[:25]}
                     for k, v in sorted(by_payer.items())],
        "loaded": bool(plans),
        "note": ("Plan names come from a payer's table-of-contents, which maps "
                 "each rate file to the plans it serves. One file commonly "
                 "serves many plans, so a rate can belong to all of them. "
                 "Files added by pasting a direct rate link carry no plan tag, "
                 "and a payer shown with several plans is one whose pooled "
                 "median blends networks that may price differently — scope to "
                 "a plan to see that payer's book on its own."),
    }


def resolve_peer_set(store: Store, market: dict) -> tuple[str, dict]:
    """Returns (description, market-with-curated-tins) for provenance."""
    name = market.get("peer_set")
    if not name:
        return ("auto market (all entities matching the market definition)", market)
    sets = store.peer_sets()
    if name not in sets:
        raise BenchmarkError(f"peer set {name!r} not found")
    defn = sets[name]
    market = dict(market)
    market["curated_tins"] = [str(t).replace("-", "") for t in defn.get("tins", [])]
    return (f"curated peer set {name!r} ({len(market['curated_tins'])} TINs)", market)


def overall_position(rows: list[dict]) -> float | None:
    """A practice's ONE overall position from its per-code Benchmark rows —
    the rule Leads, the leaderboard and the payer roster apply in SQL: the
    MEDIAN of the UNROUNDED per-code mid-ranks over the codes whose rates
    differ (a code the subject and every peer are paid the same on ranks no
    one), rounded once. p50 when every compared code is all-tied; None with no
    compared code. Negotiate and the dossier took the median of positions
    already rounded to whole points (p32 where Leads said p31) and counted an
    all-tied code at p50 (p50 where Leads said p31) — review 9 item 4.3."""
    compared = [r for r in rows if r.get("subject_percentile") is not None]
    if not compared:
        return None
    ranked = [r["subject_percentile_exact"] if r.get("subject_percentile_exact") is not None
              else r["subject_percentile"]
              for r in compared if r.get("in_position", True)]
    return half_up(statistics.median(ranked)) if ranked else 50


def compute_benchmark(store: Store, subject: str, market: dict) -> dict:
    """Per-code subject vs market percentiles (§7B.1)."""
    progress.current().stage("Finding this practice")
    market = resolve_plan_scope(store, normalize_market(market))
    subject_tins = subject_tins_for_market(store, subject, market)
    peer_desc, market = resolve_peer_set(store, market)
    include_assistant = bool(market.get("include_assistant", False))
    include_non_dollar = bool(market.get("include_non_dollar", False))
    base_q, params = practice_base_sql(store, market)
    subj_q, subj_params = practice_base_sql(
        store, subject_market(market),
        extra=" AND t.tin_value IN (SELECT tin FROM subject_tins)")

    curated = market.get("curated_tins")
    # peers filter runs directly on the already-grouped `base` CTE (aliased b) —
    # re-joining the full rates_by_tin here just to re-apply a tin_value filter
    # was a needless second scan of the whole analytical table, paid once PER
    # PAYER by compute_payer_negotiation.
    peer_clause = "b.tin_value NOT IN (SELECT tin FROM subject_tins)"
    peer_params: list = []
    if curated:
        peer_clause += f" AND b.tin_value IN ({', '.join('?' for _ in curated)})"
        peer_params = list(curated)

    pct_selects = ", ".join(
        f"round(quantile_cont(rate, {p / 100}), 2) AS p{p}" for p in PERCENTILES
    )
    sql = f"""
    WITH subject_tins AS (SELECT unnest(?::VARCHAR[]) AS tin),
    base AS ({base_q}),
    subject AS (
        -- the subject's own TINs WITHOUT the peer-only filters (PEER_ONLY_KEYS)
        SELECT billing_code, round(median(rate), 2) AS subject_rate
        FROM ({subj_q}) GROUP BY billing_code
    ),
    peers AS (
        SELECT b.billing_code, b.tin_value, b.rate
        FROM base b
        WHERE {peer_clause}
    ),
    market_stats AS (
        SELECT billing_code, count(DISTINCT tin_value) AS n_peers, {pct_selects}
        FROM peers GROUP BY billing_code
    ),
    position AS (
        -- MID-RANK percentile: everything strictly below, plus HALF of the
        -- ties. This is the standard percentile-rank definition and the only
        -- one mutually consistent with the quantile_cont columns shown beside
        -- it — a subject whose rate equals the peer median scores exactly 50.
        --
        -- It used to be at-or-below ('<='), which counted the subject's own
        -- ties in full and so OVERSTATED the position: a practice sitting
        -- exactly at the market median was reported at p60 of 5 peers, and
        -- p75 when it tied with two of four. The same row then read
        -- "subject 34.00 · p50 34.00 · you are at p60", which is
        -- self-contradictory. Ties are not an edge case here — payers publish
        -- identical fee schedules to many practices, so they are the norm —
        -- and the bias ran the dangerous way: it made clients look BETTER
        -- paid than they are, understating the negotiating opportunity.
        --
        -- Both comparisons round the peer rate to 2dp first: subject_rate is
        -- already rounded, peers.rate is not, and an exact '=' between them
        -- misses real ties (median 33.325 vs a displayed 33.33).
        SELECT p.billing_code,
               100.0 * (
                   sum(CASE WHEN round(p.rate, 2) < s.subject_rate THEN 1 ELSE 0 END)
                   + 0.5 * sum(CASE WHEN round(p.rate, 2) = s.subject_rate THEN 1 ELSE 0 END)
               ) / count(*) AS pct_exact,
               -- does the code rank anyone? Not when the subject and every
               -- peer are paid the same (overall_position's code set)
               NOT (min(round(p.rate, 2)) = any_value(s.subject_rate)
                    AND max(round(p.rate, 2)) = any_value(s.subject_rate)) AS ranks
        FROM peers p JOIN subject s USING (billing_code)
        GROUP BY p.billing_code
    )
    SELECT m.billing_code, s.subject_rate, m.n_peers,
           m.p10, m.p25, m.p40, m.p50, m.p75, m.p90,
           round(pos.pct_exact, 0) AS subject_percentile,
           pos.pct_exact AS subject_percentile_exact,
           pos.ranks AS in_position
    FROM market_stats m
    LEFT JOIN subject s USING (billing_code)
    LEFT JOIN position pos USING (billing_code)
    ORDER BY m.billing_code
    """
    progress.current().stage("Comparing with peers")
    with store.connect() as con:
        cur = con.execute(sql, [subject_tins, *params, *subj_params, *peer_params])
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    progress.current().stage("Adding Medicare and freshness")
    # the Medicare anchor for the market's state, else the subject practice's
    # main state (store.medicare_anchor) — REST OF MISSOURI applied to a
    # Kansas client misstated every % (and the Explorer showed it bare)
    anchor = store.medicare_anchor(anchor_state(store, market, subject_tins))
    mpfs, mpfs_source = anchor["rates"], anchor["label"]
    if mpfs and not market.get("state"):
        # a pooled national market has no one Medicare locality: the peers'
        # "% of Medicare" uses the SUBJECT's state for every peer (round 6 P3)
        mpfs_source = (f"{mpfs_source} - the subject's state, applied to peers in "
                       "every state of this national pool")

    target = int(market.get("target_percentile", 50))
    if target not in PERCENTILES:
        raise BenchmarkError(f"target_percentile must be one of {PERCENTILES}")
    for r in rows:
        desc, _, timed = code_info(r["billing_code"])
        r["description"] = desc
        r["is_timed"] = timed
        t = r.get(f"p{target}")
        r["target_rate"] = t
        r["gap_to_target"] = (
            half_up(t - r["subject_rate"], 2)
            if t is not None and r["subject_rate"] is not None else None
        )
        if mpfs and r["billing_code"] in mpfs and mpfs[r["billing_code"]]:
            base = mpfs[r["billing_code"]]
            r["subject_pct_medicare"] = (
                half_up(100 * r["subject_rate"] / base, 0) if r["subject_rate"] else None
            )
            r["median_pct_medicare"] = half_up(100 * r["p50"] / base, 0) if r["p50"] else None

    return {
        "subject": subject,
        "subject_tins": [mask_tin(t) for t in subject_tins],
        "market": public_market(market, drop=("curated_tins",)),
        "peer_set": peer_desc,
        "target_percentile": target,
        "rows": rows,
        "mpfs_loaded": bool(mpfs),
        "mpfs_source": mpfs_source,
        "mpfs_refusal": anchor["refusal"],
        "basis_note": basis_note_for({**market, "include_assistant": include_assistant,
                                      "include_non_dollar": include_non_dollar}),
        # Which payers behind THIS benchmark have not re-published recently. A
        # stale file does not mean a stale contract, but a client is about to
        # quote these numbers, so the age belongs beside them rather than on
        # another tab. Cosmetic tier: never let it cost the benchmark.
        "stale_payers": _stale_payers_for(store, market),
    }


def _stale_payers_for(store: Store, market: dict) -> list[dict]:
    """Payers in this market whose own last_updated_on is old. Returns [] on
    any failure — a freshness caveat must never break a benchmark."""
    try:
        from .quality import payer_freshness
        scope = set(market.get("payers") or [])
        return [
            {"payer": f["payer"], "published": f["published"],
             "age_days": f["age_days"]}
            for f in payer_freshness(store)["payers"]
            if f["stale"] and (not scope or f["payer"] in scope)
        ][:10]
    except Exception:  # noqa: BLE001
        return []


def compute_opportunity(benchmark: dict, volumes: dict[str, float],
                        conservative_percentile: int = 40) -> dict:
    """(target-percentile rate − subject rate) × annual units, per code (§7B.3).

    `volumes` is owner-supplied {code: annual_units} — never defaulted.
    """
    if not volumes:
        raise BenchmarkError("opportunity model requires user-supplied annual units per code")
    # Re-validate here, not only at the API boundary: this is the function that
    # turns units into DOLLARS, and a direct caller (CLI, packet, report,
    # another module) must not be able to reach it with a negative or
    # non-finite unit count. clean_volumes is idempotent on good input.
    volumes = clean_volumes(volumes)
    if conservative_percentile not in PERCENTILES:
        raise BenchmarkError(
            f"conservative_percentile must be one of {PERCENTILES} — an unknown "
            "percentile would silently report a $0 conservative opportunity")
    target = benchmark["target_percentile"]
    if conservative_percentile > target:
        raise BenchmarkError(
            f"conservative_percentile ({conservative_percentile}) must not exceed the "
            f"target percentile ({target}) — the 'conservative' band would be the "
            "LARGER number and the report labels would mislead")
    rows, total_target, total_conservative = [], 0.0, 0.0
    for r in benchmark["rows"]:
        units = volumes.get(r["billing_code"])
        if units is None or r["subject_rate"] is None:
            continue
        t_rate = r.get(f"p{target}")
        c_rate = r.get(f"p{conservative_percentile}")
        gap_t = max(0.0, (t_rate or 0) - r["subject_rate"]) if t_rate is not None else 0.0
        gap_c = max(0.0, (c_rate or 0) - r["subject_rate"]) if c_rate is not None else 0.0
        rows.append({
            "billing_code": r["billing_code"],
            "description": r["description"],
            "annual_units": units,
            "subject_rate": r["subject_rate"],
            "target_rate": t_rate,
            "conservative_rate": c_rate,
            "opportunity_at_target": half_up(gap_t * units, 2),
            "opportunity_at_conservative": half_up(gap_c * units, 2),
        })
        total_target += gap_t * units
        total_conservative += gap_c * units
    return {
        "rows": rows,
        "total_at_target": half_up(total_target, 2),
        "total_at_conservative": half_up(total_conservative, 2),
        "target_percentile": target,
        "conservative_percentile": conservative_percentile,
        "assumptions": (
            f"Gap computed as (p{target} − subject rate) × owner-supplied annual units, "
            f"floored at $0 per code; conservative band at p{conservative_percentile}. "
            # the basis must echo what the benchmark actually ran with — an
            # unconditional "base-modifier basis" here was false whenever the
            # include_assistant / base_only toggles were used
            f"Rates as of {month_label(benchmark['market'].get('month'))}. "
            f"{benchmark.get('basis_note', BASIS_NOTE)} "
            "Negotiated rate ≠ collections — MPPR, cost-share, denials and "
            "sequestration sit between rate and cash."
        ),
    }


# ---------------------------------------------------------------------------
# payer-negotiation one-pager (§7B.4)
# ---------------------------------------------------------------------------


def subject_payers(store: Store, subject: str, market: dict) -> list[str]:
    """Distinct payers the subject actually has published rates with, scoped to
    the market's as-of month (and payer-scope, if the caller pre-narrowed it).

    A negotiation one-pager is per-payer, so this is the list of tables to
    build — computed from the subject's own rows, not from every payer in the
    store (a payer the subject doesn't contract with has nothing to negotiate).
    """
    market = resolve_plan_scope(store, normalize_market(market))
    subject_tins = subject_tins_for_market(store, subject, market)
    if not subject_tins:
        return []
    # "latest": payers the subject contracts with in ANY month present
    clauses = ["tin_value IN (SELECT unnest(?::VARCHAR[]))", "payer IS NOT NULL"]
    params: list = [subject_tins]
    if not is_latest(market):
        clauses.append("file_month = ?")
        params.append(market["month"])
    scope = market.get("payers") or []
    if scope:
        clauses.append(f"payer IN ({', '.join('?' for _ in scope)})")
        params += scope
    with store.connect() as con:
        rows = con.execute(
            f"SELECT DISTINCT payer FROM {spine_relation(market)} t "
            f"WHERE {' AND '.join(clauses)} ORDER BY payer",
            params,
        ).fetchall()
    return [r[0] for r in rows if r[0]]


def compute_payer_negotiation(store: Store, subject: str, market: dict,
                              volumes: dict[str, float] | None = None,
                              conservative_percentile: int = 40) -> dict:
    """Per-payer negotiation view (§7B.4).

    For each payer the subject contracts with, benchmark the subject against
    *that payer's other providers only* (intra-payer peer comparison — the
    number that matters at a contract renewal is "what is THIS payer paying my
    peers", not a blended cross-payer market). Optionally attach the annual
    opportunity per payer when the caller supplies volumes.
    """
    market = resolve_plan_scope(store, normalize_market(market))
    payers = subject_payers(store, subject, market)
    if not payers:
        raise BenchmarkError(
            "subject has no rates for the given as-of month and market scope "
            "— nothing to build a per-payer negotiation view from")
    volumes = clean_volumes(volumes) or None
    sections, total_target, total_conservative = [], 0.0, 0.0
    covered_payers = 0
    prog = progress.current()
    prog.stage("Benchmarking against each payer's other practices")
    for i_payer, payer in enumerate(payers):
        prog.tick(i_payer / len(payers))
        pmarket = {**market, "payers": [payer]}
        with prog.group():
            bench = compute_benchmark(store, subject, pmarket)
        prog.tick((i_payer + 1) / len(payers))
        # only codes where the subject actually has a rate with this payer AND
        # a peer set exists — an all-null section is noise on a one-pager
        priced = [r for r in bench["rows"] if r.get("subject_rate") is not None]
        if not priced:
            continue
        covered_payers += 1
        opp = None
        if volumes:
            # A payer with no volume-matching code just yields empty rows and a
            # $0 total (compute_opportunity does NOT raise for that). The only
            # BenchmarkError it raises is a genuine config error (bad
            # percentile) that applies to EVERY payer — letting it propagate to
            # the 422 handler is correct; swallowing it here would print a
            # fabricated "$0/yr" on the report (honesty invariant).
            opp = compute_opportunity(bench, volumes, conservative_percentile)
            total_target += opp["total_at_target"]
            total_conservative += opp["total_at_conservative"]
        # headline percentile: the one overall-position rule (overall_position;
        # a mean let one outlier code swing the headline)
        headline_pct = overall_position(priced)
        sections.append({
            "payer": payer,
            "benchmark": bench,
            "opportunity": opp,
            "n_codes": len(priced),
            "headline_percentile": headline_pct,
        })
    if not sections:
        raise BenchmarkError(
            "no payer has a benchmarkable code for this subject at the given "
            "as-of month (subject-priced codes with at least one peer)")
    # lowest-percentile payer first: that's where the subject is most underpaid
    # relative to peers, i.e. the strongest renegotiation case
    sections.sort(key=lambda s: (s["headline_percentile"] is None,
                                 s["headline_percentile"] if s["headline_percentile"] is not None else 999))
    # The volumes are the PRACTICE's annual units (the box says so, and "Fill
    # from Medicare" fills practice-wide units). Valuing every payer with all
    # of them and ADDING the payers counted each unit once per payer: two
    # payers each $10 under on 1,000 units printed $20,000/yr, where no payer
    # mix can make it more than $10,000 (round 7 P0.2). With more than one
    # payer the per-payer figures are ALTERNATIVES — "if every unit were
    # billed to this payer" — so there is no cross-payer total; the largest
    # single-payer figure is reported instead, named as what it is.
    valued = [s for s in sections if s.get("opportunity")]
    best = (max(valued, key=lambda s: (s["opportunity"]["total_at_target"], s["payer"]))
            if valued else None)
    additive = len(valued) <= 1
    return {
        "subject": subject,
        "subject_tins": sections[0]["benchmark"]["subject_tins"],
        "market": public_market(market, drop=("payers",)),
        "payers": [s["payer"] for s in sections],
        "sections": sections,
        "has_volumes": bool(volumes),
        "total_at_target": half_up(total_target, 2) if volumes and additive else None,
        "total_at_conservative": (half_up(total_conservative, 2)
                                  if volumes and additive else None),
        "best_single_payer": ({"payer": best["payer"],
                               "at_target": best["opportunity"]["total_at_target"],
                               "at_conservative": best["opportunity"]["total_at_conservative"]}
                              if best else None),
        "volume_note": (VOLUME_PER_PAYER_NOTE if volumes and not additive else None),
        "target_percentile": sections[0]["benchmark"]["target_percentile"],
        "conservative_percentile": conservative_percentile if volumes else None,
    }


# ---------------------------------------------------------------------------
# payer-comparison workspace (Negotiate tab): one subject, ONE payer, named
# comparable entities as columns, plus the cross-payer leverage metrics
# ---------------------------------------------------------------------------


def _entity_rates(store: Store, tins: list[str], market: dict) -> dict[str, float]:
    """{code: rate} for one entity under the market basis: the entity's rate is
    the median of its member TINs' medians — the same rule as everywhere else.
    A named entity's own rates ignore the peer-only filters (PEER_ONLY_KEYS)."""
    market = subject_market(market)
    where, params = _market_where(market, bool(market.get("include_assistant")),
                                  bool(market.get("include_non_dollar")))
    rel = _rates_relation(market)
    sql = f"""
    WITH base AS (
        SELECT t.billing_code, t.tin_value, median(t.negotiated_rate) AS rate
        FROM {rel} t LEFT JOIN tin_directory td USING (tin_value)
        WHERE {where} AND t.tin_value IN (SELECT unnest(?::VARCHAR[]))
        GROUP BY 1, 2)
    SELECT billing_code, round(median(rate), 2) FROM base GROUP BY 1
    """
    with store.connect() as con:
        return dict(con.execute(sql, [*params, tins]).fetchall())


def compute_payer_comparison(store: Store, subject: str, payer: str, market: dict,
                             comparables: list[str] | None = None) -> dict:
    """The Negotiate view: subject vs ONE payer's market, per CPT code, with
    user-named comparable entities as side-by-side columns and the cross-payer
    context ("what your OTHER payers pay you for the same code") that anchors a
    renegotiation ask. Every number follows the house basis: dollar base-modifier
    rows, pinned month, entity rate = median of member-TIN medians, placeholders
    excluded, percentiles over this payer's OTHER providers."""
    if not payer:
        raise BenchmarkError("pick the payer you are negotiating with")
    market = resolve_plan_scope(store, normalize_market(market))
    pmarket = {**market, "payers": [payer]}
    _prog = progress.current()
    _prog.stage("Comparing with this payer's peers")
    with _prog.group():
        bench = compute_benchmark(store, subject, pmarket)
    _prog.stage("Reading the comparables")

    # named comparables: what THIS payer pays entities the user considers
    # comparable — columns, one per name, in the order given
    comps, seen_comps = [], set()
    for name in (comparables or []):
        name = str(name).strip()
        if not name or name == subject or name in seen_comps:
            continue
        seen_comps.add(name)
        tins = subject_tins_for_market(store, name, pmarket)
        rates = _entity_rates(store, tins, pmarket)
        # a raw TIN typed WITH dashes must still be SSN-masked in the label
        digits = "".join(ch for ch in name if ch.isdigit())
        label = mask_tin(digits) if digits and len(digits) == len(name.replace("-", "").replace(" ", "")) else name
        comps.append({"name": label, "tins": [mask_tin(t) for t in tins], "rates": rates})

    # cross-payer leverage: the subject's own rates for the same codes with
    # every OTHER payer (same month/basis) — "you pay me less than my other
    # contracts do" is often the strongest single line in the room
    # the market minus its payer, KEEPING the resolved plan scope: building it
    # with public_market() dropped `_plan_files`, and _market_where then
    # refused every plan-scoped Negotiate compare / payer-compare / proposal
    other_market = {k: v for k, v in subject_market(market).items() if k != "payers"}
    o_where, o_params = _market_where(other_market, bool(market.get("include_assistant")),
                                      bool(market.get("include_non_dollar")))
    orel = _rates_relation(other_market)
    subject_tins = subject_tins_for_market(store, subject, market)
    with store.connect() as con:
        # two-level entity rule here too (invariant 5): per-TIN median first,
        # THEN median across TINs per payer, THEN median across payers — pooling
        # raw rows across TINs let a many-variant TIN outvote a sibling TIN
        others = {r[0]: {"rate": r[1], "n_payers": r[2]} for r in con.execute(
            f"""
            WITH per_tin AS (
                SELECT t.billing_code, t.payer, t.tin_value,
                       median(t.negotiated_rate) AS rate
                FROM {orel} t LEFT JOIN tin_directory td USING (tin_value)
                WHERE {o_where} AND t.payer != ?
                  AND t.tin_value IN (SELECT unnest(?::VARCHAR[]))
                GROUP BY 1, 2, 3),
            per_payer AS (
                SELECT billing_code, payer, median(rate) AS rate
                FROM per_tin GROUP BY 1, 2)
            SELECT billing_code, round(median(rate), 2), count(DISTINCT payer)
            FROM per_payer GROUP BY 1
            """, [*o_params, payer, subject_tins]).fetchall()}
        # the subject's own priced codes under this payer/basis — so a code
        # with ZERO peers still appears (market columns dashed) instead of
        # silently vanishing from a "every code you're priced on" table
        subject_priced = _entity_rates(store, subject_tins, pmarket)

    bench_by_code = {r["billing_code"]: r for r in bench["rows"]}
    rows = []
    for code in sorted(set(subject_priced) | {c for c, r in bench_by_code.items()
                                              if r.get("subject_rate") is not None}):
        r = bench_by_code.get(code)
        if r is None:
            # subject-priced code with NO peers under this payer market: keep it
            # visible with dashed market columns rather than silently dropping it
            desc, _, timed = code_info(code)
            r = {"billing_code": code, "subject_rate": subject_priced[code],
                 "n_peers": 0, "description": desc, "is_timed": timed,
                 "subject_percentile": None,
                 **{f"p{p}": None for p in PERCENTILES},
                 "target_rate": None, "gap_to_target": None}
        elif r.get("subject_rate") is None:
            continue
        subj, p50 = r["subject_rate"], r.get("p50")
        row = dict(r)
        row["gap_to_median"] = half_up(p50 - subj, 2) if p50 is not None else None
        # UPLIFT semantics, denominator = YOUR rate ("+45%" means your rate
        # × 1.45 reaches the median) — consistent across every % in this view
        row["gap_to_median_pct"] = (
            half_up(100 * (p50 - subj) / subj, 1) if p50 is not None and subj else None)
        o = others.get(code)
        row["other_payers_rate"] = o["rate"] if o else None
        row["n_other_payers"] = o["n_payers"] if o else 0
        row["comp_rates"] = [c["rates"].get(code) for c in comps]
        best = [v for v in row["comp_rates"] if v is not None]
        row["best_comparable"] = max(best) if best else None
        rows.append(row)
    if not rows:
        raise BenchmarkError(
            f"{subject} has no priced codes with {payer} in {month_label(market.get('month'))} "
            "under the market basis — nothing to negotiate from")

    def _avg(vals):
        vals = [v for v in vals if v is not None]
        return half_up(statistics.mean(vals), 1) if vals else None

    below = [r for r in rows if r.get("p50") is not None and r["subject_rate"] < r["p50"]]
    comp_summaries = []
    for i, c in enumerate(comps):
        pairs = [(r["subject_rate"], r["comp_rates"][i]) for r in rows
                 if r["comp_rates"][i] is not None]
        prem = [half_up(100 * (cr - sr) / sr, 1) for sr, cr in pairs if sr]
        comp_summaries.append({
            "name": c["name"], "n_shared_codes": len(pairs),
            "n_paid_more": sum(1 for sr, cr in pairs if cr > sr),
            "median_premium_pct": half_up(statistics.median(prem), 1) if prem else None,
        })
    return {
        "subject": subject,
        "subject_tins": bench["subject_tins"],
        "payer": payer,
        "market": public_market(market, drop=("payers",)),
        "rows": rows,
        "comparables": comp_summaries,
        "mpfs_loaded": bench["mpfs_loaded"],
        "mpfs_source": bench.get("mpfs_source"),
        "basis_note": bench["basis_note"],
        "peer_set": bench["peer_set"],
        "target_percentile": bench["target_percentile"],
        "summary": {
            "headline_percentile": overall_position(rows),
            "n_codes": len(rows),
            "n_below_median": len(below),
            # honest averages across the subject's priced codes (each code
            # weighted equally — utilization weighting needs volumes we don't have)
            # ONE denominator everywhere: the subject's own rate. "+45%" always
            # means "your rate × 1.45" — a payer analyst can check the arithmetic
            # and every % in the document agrees with every other.
            "avg_gap_to_median_pct": _avg([r["gap_to_median_pct"] for r in rows]),
            "avg_uplift_to_p75_pct": _avg([
                half_up(100 * (r["p75"] - r["subject_rate"]) / r["subject_rate"], 1)
                for r in rows if r.get("p75") and r["subject_rate"]]),
            "avg_other_payer_diff_pct": _avg([
                half_up(100 * (r["other_payers_rate"] - r["subject_rate"]) / r["subject_rate"], 1)
                for r in rows if r.get("other_payers_rate") and r["subject_rate"]]),
        },
    }


# ---------------------------------------------------------------------------
# contract-gap finder (§7B.5) — codes peers price that the subject does not
# ---------------------------------------------------------------------------


GAP_NOTE = (
    "A code appears here when peers publish a rate for it and the subject "
    "does not. Absence in an MRF is not proof the subject lacks the "
    "contract — it can be a publishing gap. Directional; confirm against "
    "the subject's actual fee schedule before acting.")


def contract_gaps(store: Store, subject: str, market: dict, *,
                  min_peers: int = 5, limit: int = 100) -> dict:
    """Codes that at least `min_peers` of the subject's market peers have a
    published rate for, but the subject has NO rate on — candidate contract or
    fee-schedule gaps to chase. Directional: absence in an MRF is a publishing
    gap as often as a real coverage gap, so this opens a question, not a claim."""
    res = contract_gaps_book(store, [subject], market, min_peers=min_peers,
                             limit=limit)[str(subject or "").strip()]
    if isinstance(res, BenchmarkError):
        raise res
    return res


def contract_gaps_book(store: Store, subjects: list[str], market: dict, *,
                       min_peers: int = 5, limit: int = 100) -> dict:
    """contract_gaps for a whole book at once: {subject: result | BenchmarkError}.

    The market's per-code peer statistics are computed ONCE. That is exact:
    a gap is a code the subject has NO rate for, so the subject contributes no
    row to that code's statistics and excluding it from the peers changes
    nothing. The client digest and the packets ran the whole national market
    aggregation once per client (3.8 s each on a 99M-rate store). A curated
    peer set changes the peer population per subject, so it keeps the
    per-subject query."""
    if min_peers < 1:
        raise BenchmarkError("min_peers must be at least 1")
    limit = max(1, min(int(limit), 1000))
    market = resolve_plan_scope(store, normalize_market(market))
    peer_desc, market = resolve_peer_set(store, market)
    where, params = _market_where(market, bool(market.get("include_assistant")),
                                  bool(market.get("include_non_dollar")))
    base, base_params = practice_base_sql(store, market)
    curated = market.get("curated_tins")
    # the subject's OWN codes, without the peer-only filters: an out-of-state
    # practice's priced codes were listed as "gaps" beside a Benchmark that
    # printed their rates (review 9 item 4.4)
    smarket = subject_market(market)
    s_where, s_params = _market_where(smarket, bool(market.get("include_assistant")),
                                      bool(market.get("include_non_dollar")))
    s_rel = _rates_relation(smarket)

    out: dict = {}
    tins_of: dict[str, list[str]] = {}
    for subject in subjects:
        key = str(subject or "").strip()
        if not key:
            out[key] = BenchmarkError("pick a subject practice")
            continue
        try:
            tins = subject_tins_for_market(store, key, market)
        except BenchmarkError as e:
            out[key] = e
            continue
        # a blank/unknown subject resolves to [""], which matches no rows —
        # EVERY peer-priced code would then read as a "gap" for a practice that
        # isn't there. Refuse instead of fabricating.
        if not any(t for t in tins):
            out[key] = BenchmarkError(
                "subject not found — pick a practice that has rates in the store")
            continue
        tins_of[key] = tins

    def result(subject: str, tins: list[str], rows: list[dict]) -> dict:
        for r in rows:
            desc, _, timed = code_info(r["billing_code"])
            r["description"] = desc
            r["is_timed"] = timed
        return {"subject": subject, "subject_tins": [mask_tin(t) for t in tins],
                "market": public_market(market, drop=("curated_tins",)),
                "peer_set": peer_desc, "min_peers": min_peers, "count": len(rows),
                "gaps": rows, "basis_note": basis_note_for(market), "gap_note": GAP_NOTE}

    stat_cols = """count(DISTINCT b.tin_value)          AS n_peers,
               round(median(b.rate), 2)             AS peer_median,
               round(quantile_cont(b.rate, .25), 2) AS peer_p25,
               round(quantile_cont(b.rate, .75), 2) AS peer_p75"""
    cols = ["billing_code", "n_peers", "peer_median", "peer_p25", "peer_p75"]
    with store.connect() as con:
        if curated:
            for subject, tins in tins_of.items():
                cur = con.execute(f"""
                    WITH subject_tins AS (SELECT unnest(?::VARCHAR[]) AS tin),
                    base AS ({base}),
                    subj AS (SELECT DISTINCT t.billing_code FROM {s_rel} t
                             LEFT JOIN tin_directory td USING (tin_value)
                             WHERE {s_where}
                               AND t.tin_value IN (SELECT tin FROM subject_tins)),
                    peers AS (
                        SELECT b.billing_code, {stat_cols}
                        FROM base b
                        WHERE b.tin_value NOT IN (SELECT tin FROM subject_tins)
                          AND b.tin_value IN ({', '.join('?' for _ in curated)})
                        GROUP BY b.billing_code)
                    SELECT p.billing_code, p.n_peers, p.peer_median, p.peer_p25, p.peer_p75
                    FROM peers p
                    WHERE p.n_peers >= ?
                      AND p.billing_code NOT IN (SELECT billing_code FROM subj
                                                 WHERE billing_code IS NOT NULL)
                    ORDER BY p.n_peers DESC, p.billing_code
                    LIMIT ?
                """, [tins, *base_params, *s_params, *curated, min_peers, limit])
                out[subject] = result(subject, tins,
                                      [dict(zip(cols, r)) for r in cur.fetchall()])
            return out
        if not tins_of:
            return out
        all_tins = sorted({t for ts in tins_of.values() for t in ts})
        market_stats = [dict(zip(cols, r)) for r in con.execute(f"""
            WITH base AS ({base})
            SELECT b.billing_code, {stat_cols}
            FROM base b GROUP BY b.billing_code
            HAVING count(DISTINCT b.tin_value) >= ?
            ORDER BY n_peers DESC, b.billing_code
        """, [*base_params, min_peers]).fetchall()]
        priced: dict[str, set] = {}
        for tin, code in con.execute(f"""
            SELECT DISTINCT t.tin_value, t.billing_code
            FROM {s_rel} t LEFT JOIN tin_directory td USING (tin_value)
            WHERE {s_where} AND t.tin_value IN (SELECT unnest(?::VARCHAR[]))
        """, [*s_params, all_tins]).fetchall():
            priced.setdefault(tin, set()).add(code)
    for subject, tins in tins_of.items():
        mine = set().union(*(priced.get(t, set()) for t in tins))
        rows = [dict(r) for r in market_stats if r["billing_code"] not in mine][:limit]
        out[subject] = result(subject, tins, rows)
    return out


def _anchors_section(store: Store, market: dict, subject_rates: dict) -> str:
    """Medicaid-floor and hospital-parity lines for a negotiation document
    (review 13 6.2): the rates the document already prints, beside the
    state's Medicaid rate and what the same payer pays hospitals. Never fails
    the document (invariant 3)."""
    try:
        from .anchors import anchors_html, negotiation_anchors
        return anchors_html(negotiation_anchors(store, market, subject_rates))
    except Exception as exc:  # noqa: BLE001
        log.warning("negotiation anchors failed: %s", exc)
        return ""


def render_payer_compare_report(cfg: MrfxConfig, store: Store, comp: dict) -> str:
    """Print-ready payer-negotiation comparison: the document the owner hands
    the payer. Per-code table (subject vs this payer's market vs named
    comparables vs the subject's other payers), leverage summary, and an ask
    table at median / p75 / best-comparable. Same honesty rails as every other
    report: pinned month, geographic-scope guard, full methodology footer."""
    if not comp.get("market", {}).get("month"):
        raise BenchmarkError("payer comparison report requires a pinned as-of month")
    geo_banner = _geo_banner_html(
        require_geographic_scope(comp["market"], "payer comparison report"))
    e = html.escape
    s = comp["summary"]
    month = comp["market"].get("month")
    comp_heads = "".join(f"<th class='num'>{e(c['name'])}</th>" for c in comp["comparables"])
    mp = comp.get("mpfs_loaded")
    mp_heads = "<th class='num'>% Medicare (you / median)</th>" if mp else ""

    def _pctm(r):
        if not mp:
            return ""
        a, b = r.get("subject_pct_medicare"), r.get("median_pct_medicare")
        return (f"<td class='num sub'>{_pctnum(a)} / {_pctnum(b)}</td>")

    rows_html = "".join(
        f"<tr><td>{e(r['billing_code'])}<div class='sub'>{e(r['description'] or '')}"
        f"{' · timed 15-min' if r['is_timed'] else ''}</div></td>"
        f"<td class='num'><b>{_m(r['subject_rate'])}</b></td>"
        f"<td class='num'>{_m(r.get('other_payers_rate'))}"
        f"<div class='sub'>{r.get('n_other_payers') or 0} payer(s)</div></td>"
        f"<td class='num'>{_m(r['p25'])}</td><td class='num'>{_m(r['p50'])}</td>"
        f"<td class='num'>{_m(r['p75'])}</td>"
        f"<td class='num'>{'p%.0f' % r['subject_percentile'] if r.get('subject_percentile') is not None else '–'}</td>"
        f"<td class='num gap'>{_m(r['gap_to_median'])}</td>"
        + "".join(f"<td class='num'>{_m(v)}</td>" for v in r["comp_rates"])
        + _pctm(r)
        + f"<td class='num sub'>{r['n_peers'] or 0}</td></tr>"
        for r in comp["rows"]
    )
    asks_html = "".join(
        f"<tr><td>{e(r['billing_code'])}</td><td class='num'>{_m(r['subject_rate'])}</td>"
        f"<td class='num'>{_m(r['p50'])}</td><td class='num'>{_m(r['p75'])}</td>"
        f"<td class='num'>{_m(r.get('best_comparable'))}</td></tr>"
        for r in comp["rows"]
    )
    comp_lines = "".join(
        f"<li><b>{e(c['name'])}</b>: this payer pays them more than you on "
        f"{c['n_paid_more']} of {c['n_shared_codes']} shared code(s)"
        + (f", median premium {signed_pct(c['median_premium_pct'])} vs your rate"
           if c['median_premium_pct'] is not None else "") + ".</li>"
        for c in comp["comparables"] if c["n_shared_codes"]
    )
    lev = []
    if s["headline_percentile"] is not None:
        lev.append(f"You sit at roughly <b>p{s['headline_percentile']}</b> among "
                   f"{e(comp['payer'])}'s other providers across your "
                   f"{s['n_codes']} priced code(s); <b>{s['n_below_median']}</b> "
                   "of them are below this payer's median.")
    if s["avg_gap_to_median_pct"] is not None and s["avg_gap_to_median_pct"] > 0:
        lev.append(f"Reaching this payer's <b>median</b> is an average uplift of "
                   f"<b>{s['avg_gap_to_median_pct']:.1f}%</b> across your codes"
                   + (f"; p75 would be {s['avg_uplift_to_p75_pct']:.1f}%"
                      if s["avg_uplift_to_p75_pct"] is not None else "") + ".")
    if s["avg_other_payer_diff_pct"] is not None and s["avg_other_payer_diff_pct"] > 0:
        lev.append(f"Your OTHER payers pay you on average "
                   f"<b>{s['avg_other_payer_diff_pct']:.1f}% more</b> for the same "
                   "codes — this contract prices below your own book.")
    footer = methodology_footer(store, {
        "market": {**comp["market"], "payers": [comp["payer"]]},
        "peer_set": comp["peer_set"], "basis_note": comp["basis_note"],
        "mpfs_loaded": comp["mpfs_loaded"], "mpfs_source": comp.get("mpfs_source"),
    })
    return f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<title>Payer negotiation — {e(comp['subject'])} vs {e(comp['payer'])}</title>
<style>
 body {{ font: 13px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif; color: #101828;
        max-width: 980px; margin: 32px auto; padding: 0 24px; }}
 h1 {{ font-size: 20px; margin-bottom: 2px; }} h2 {{ font-size: 15px; margin-top: 28px; }}
 .brand {{ color: #475467; font-size: 12px; text-transform: uppercase; letter-spacing: .06em; }}
 .brandbar {{ display: flex; align-items: center; gap: 10px; margin-bottom: 4px; }}
 .brandbar .brand {{ margin: 0; }} .logo {{ max-height: 40px; max-width: 200px; }}
 .meta {{ color: #475467; margin-bottom: 10px; }}
 table {{ border-collapse: collapse; width: 100%; font-variant-numeric: tabular-nums; }}
 th, td {{ text-align: left; padding: 6px 8px; border-bottom: 1px solid #eaecf0; vertical-align: middle; }}
 th {{ font-size: 11px; color: #667085; text-transform: uppercase; letter-spacing: .04em; }}
 .num {{ text-align: right; }} .sub {{ color: #667085; font-size: 11.5px; }}
 .gap {{ font-weight: 650; }}
 .band {{ font-size: 13.5px; background: #f2f4f7; padding: 10px 14px; border-radius: 6px; }}
 .band li {{ margin: 4px 0 4px 16px; }}
 .geo-warn {{ background: #fbe9d0; border: 1px solid #d99a3a; color: #7a4a00;
          padding: 10px 14px; border-radius: 6px; font-size: 12.5px; margin: 12px 0; }}
 footer {{ margin-top: 36px; border-top: 1px solid #d0d5dd; padding-top: 12px;
          color: #475467; font-size: 11px; white-space: pre-wrap; }}
 @page {{ size: landscape; margin: 10mm; }}
 @media print {{ body {{ margin: 0; max-width: none; }} table {{ font-size: 10.5px; }} th, td {{ padding: 3px 5px; }} * {{ -webkit-print-color-adjust: exact; print-color-adjust: exact; }} h2 {{ break-after: avoid; }} }}
</style></head><body>
{_brand_header(cfg)}
<h1>Payer negotiation — {e(comp['subject'])} vs {e(comp['payer'])}</h1>
<div class="meta">What {e(comp['payer'])} pays you, its market, your named
 comparables, and your other payers — per code, as of {e(month_label(month))}.</div>
{geo_banner}
<div class="band"><ul>{''.join(f'<li>{ln}</li>' for ln in lev)}{comp_lines}</ul></div>
<h2>Per-code comparison</h2>
<table><thead><tr><th>Code</th><th class="num">Your rate</th>
<th class="num">Your other payers</th><th class="num">P25</th><th class="num">Median</th>
<th class="num">P75</th><th class="num">Your %ile</th><th class="num">Gap to median</th>
{comp_heads}{mp_heads}<th class="num">Peers</th></tr></thead>
<tbody>{rows_html}</tbody></table>
<h2>Ask scenarios (per code)</h2>
<p class="sub">Reference points for the request — current rate, this payer's median
and P75, and the best rate this payer already pays one of your named comparables.</p>
<table><thead><tr><th>Code</th><th class="num">Current</th><th class="num">At median</th>
<th class="num">At P75</th><th class="num">Best comparable</th></tr></thead>
<tbody>{asks_html}</tbody></table>
{_anchors_section(store, comp["market"], {(comp["payer"], r["billing_code"]): r.get("subject_rate") for r in comp["rows"]})}
<footer>METHODOLOGY\n{e(footer)}</footer>
</body></html>"""


PROPOSAL_TARGETS = ("p50", "p75", "best_comparable", "pct_medicare")


def compute_rate_proposal(store: Store, subject: str, payer: str, market: dict,
                          target: dict, volumes: dict[str, float] | None = None,
                          comparables: list[str] | None = None) -> dict:
    """The proposal grid the payer actually receives: per code, current rate ->
    proposed rate at a stated, checkable basis.

    Targets are REAL reference points only — this payer's median or p75 among
    its other providers, the best rate it already pays a named comparable, or
    a % of the loaded Medicare fee schedule. No interpolated percentiles: a
    number in a payer-facing document must be reproducible from stated inputs.
    A proposed rate is never below the current one (those rows read 'keep');
    annual value uses owner-supplied volumes and only volumes — codes without
    a volume still get a proposed rate, with the value column honest-blank."""
    if isinstance(target, str):          # "p50" alone is natural input (was a 500)
        target = {"kind": target}
    elif not isinstance(target, dict):
        raise BenchmarkError(f"target must be one of {', '.join(PROPOSAL_TARGETS)}")
    kind = str(target.get("kind") or "").strip()
    if kind not in PROPOSAL_TARGETS:
        raise BenchmarkError(f"target must be one of {', '.join(PROPOSAL_TARGETS)}")
    pct = None
    if kind == "pct_medicare":
        try:
            pct = float(target.get("value"))
        except (TypeError, ValueError):
            raise BenchmarkError("a % of Medicare target needs a number, e.g. 110")
        if not (50 <= pct <= 400):
            raise BenchmarkError(f"{pct}% of Medicare is outside a plausible "
                                 "proposal range (50-400%)")
    comp = compute_payer_comparison(store, subject, payer, market,
                                    comparables=comparables)
    mpfs: dict[str, float] = {}
    if kind == "pct_medicare":
        if not comp.get("mpfs_loaded"):
            raise BenchmarkError("a % of Medicare target needs the MPFS loaded "
                                 "(Benchmark tab -> Load MPFS)")
        # the NORMALIZED market's state ("Missouri" -> "MO"), the one the
        # comparison itself used; the raw one read "MISSOURI" and was refused
        anchor = store.medicare_anchor(
            anchor_state(store, normalize_market(market),
                         resolve_subject_tins(store, subject)))
        if anchor["refusal"]:
            raise BenchmarkError(anchor["refusal"])
        mpfs = anchor["rates"]

    volumes = clean_volumes(volumes)
    basis_label = {"p50": f"{payer}'s median", "p75": f"{payer}'s p75",
                   "best_comparable": "best comparable rate this payer pays",
                   "pct_medicare": f"{pct:g}% of Medicare" if pct else ""}[kind]
    rows, total_value, priced_value_units = [], 0.0, 0
    for r in comp["rows"]:
        cur = r["subject_rate"]
        if kind == "p50":
            tgt = r.get("p50")
        elif kind == "p75":
            tgt = r.get("p75")
        elif kind == "best_comparable":
            tgt = r.get("best_comparable")
        else:
            m = mpfs.get(r["billing_code"])
            tgt = half_up(m * pct / 100.0, 2) if m is not None else None
        proposed = None if tgt is None else max(half_up(float(tgt), 2), cur)
        units = volumes.get(r["billing_code"])
        delta = None if proposed is None else half_up(proposed - cur, 2)
        value = (half_up(delta * units, 2)
                 if delta is not None and units is not None else None)
        if value is not None:
            total_value += value
            priced_value_units += 1
        rows.append({
            "billing_code": r["billing_code"], "description": r.get("description"),
            "is_timed": r.get("is_timed"), "current": cur, "proposed": proposed,
            "delta": delta,
            "delta_pct": (half_up(100 * delta / cur, 1)
                          if delta is not None and cur else None),
            "kept": proposed is not None and proposed == cur,
            "no_reference": proposed is None,
            "units": units, "annual_value": value,
            "n_peers": r.get("n_peers"),
        })
    return {
        "subject": comp["subject"], "payer": payer, "market": comp["market"],
        "target": {"kind": kind, "value": pct, "label": basis_label},
        "rows": rows,
        "basis_note": comp["basis_note"], "peer_set": comp["peer_set"],
        "mpfs_loaded": comp["mpfs_loaded"], "mpfs_source": comp.get("mpfs_source"),
        "summary": {
            "n_codes": len(rows),
            "n_raised": sum(1 for r in rows if r["delta"] and r["delta"] > 0),
            "n_kept": sum(1 for r in rows if r["kept"]),
            "n_no_reference": sum(1 for r in rows if r["no_reference"]),
            "has_volumes": bool(volumes),
            "n_valued": priced_value_units,
            "total_annual_value": half_up(total_value, 2) if volumes else None,
        },
    }


def render_proposal_report(cfg: MrfxConfig, store: Store, prop: dict) -> str:
    """Print-ready rate proposal — the one-pager handed to the payer. Same
    honesty rails as every report: pinned month, geographic-scope banner,
    stated basis per number, full methodology footer."""
    if not prop.get("market", {}).get("month"):
        raise BenchmarkError("a proposal document requires a pinned as-of month")
    geo_banner = _geo_banner_html(
        require_geographic_scope(prop["market"], "rate proposal"))
    e = html.escape
    s = prop["summary"]
    month = prop["market"].get("month")
    rows_html = "".join(
        f"""<tr><td>{e(r['billing_code'])}{' <span class="sub">15-min</span>' if r.get('is_timed') else ''}
        <div class="sub">{e(r.get('description') or '')}</div></td>
        <td class="num">{_m(r['current'])}</td>
        <td class="num gap">{'—' if r['no_reference'] else _m(r['proposed'])}</td>
        <td class="num">{'' if r['delta'] is None else ('keep' if r['kept'] else f"+{_m(r['delta'])} ({signed_pct(r['delta_pct'])})")}</td>
        <td class="num">{'' if r['units'] is None else f"{r['units']:,.0f}"}</td>
        <td class="num">{'' if r['annual_value'] is None else _m(r['annual_value'])}</td>
        <td class="num sub">{r.get('n_peers') if r.get('n_peers') is not None else ''}</td></tr>"""
        for r in prop["rows"])
    value_band = ""
    if s["has_volumes"] and s["total_annual_value"] is not None:
        value_band = (f'<p class="band">At the practice\'s stated annual volumes, this '
                      f'proposal is worth <b>{_m(s["total_annual_value"])}</b> per year '
                      f'({s["n_valued"]} of {s["n_codes"]} codes carry volumes; '
                      f'codes without a stated volume are excluded from the total). The units '
                      f'are counted as if all of them were billed to {e(prop["payer"])} — '
                      f'scale by its share of the practice\'s volume.</p>')
    nr = (f' {s["n_no_reference"]} code(s) show no proposal — the reference '
          f'basis has no rate to point to there.' if s["n_no_reference"] else "")
    footer = methodology_footer(store, {
        "market": {**prop["market"], "payers": [prop["payer"]]},
        "peer_set": prop["peer_set"], "basis_note": prop["basis_note"],
        "mpfs_loaded": prop["mpfs_loaded"], "mpfs_source": prop.get("mpfs_source"),
    })
    return f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<title>Rate proposal — {e(prop['subject'])} to {e(prop['payer'])}</title>
<style>
 body {{ font: 13px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif; color: #101828;
        max-width: 940px; margin: 32px auto; padding: 0 24px; }}
 h1 {{ font-size: 20px; margin-bottom: 2px; }} h2 {{ font-size: 15px; margin-top: 28px; }}
 .brand {{ color: #475467; font-size: 12px; text-transform: uppercase; letter-spacing: .06em; }}
 .brandbar {{ display: flex; align-items: center; gap: 10px; margin-bottom: 4px; }}
 .brandbar .brand {{ margin: 0; }} .logo {{ max-height: 40px; max-width: 200px; }}
 .meta {{ color: #475467; margin-bottom: 10px; }}
 table {{ border-collapse: collapse; width: 100%; font-variant-numeric: tabular-nums; }}
 th, td {{ text-align: left; padding: 6px 8px; border-bottom: 1px solid #eaecf0; vertical-align: middle; }}
 th {{ font-size: 11px; color: #667085; text-transform: uppercase; letter-spacing: .04em; }}
 .num {{ text-align: right; }} .sub {{ color: #667085; font-size: 11.5px; }}
 .gap {{ font-weight: 650; }}
 .band {{ font-size: 13.5px; background: #f2f4f7; padding: 10px 14px; border-radius: 6px; }}
 .geo-warn {{ background: #fbe9d0; border: 1px solid #d99a3a; color: #7a4a00;
          padding: 10px 14px; border-radius: 6px; font-size: 12.5px; margin: 12px 0; }}
 footer {{ margin-top: 36px; border-top: 1px solid #d0d5dd; padding-top: 12px;
          color: #475467; font-size: 11px; white-space: pre-wrap; }}
 @page {{ size: landscape; margin: 10mm; }}
 @media print {{ body {{ margin: 0; max-width: none; }} table {{ font-size: 10.5px; }} th, td {{ padding: 3px 5px; }} * {{ -webkit-print-color-adjust: exact; print-color-adjust: exact; }} h2 {{ break-after: avoid; }} }}
</style></head><body>
{_brand_header(cfg)}
<h1>Rate proposal — {e(prop['subject'])} to {e(prop['payer'])}</h1>
<div class="meta">Proposed rate basis: <b>{e(prop['target']['label'])}</b>, from published
 machine-readable rates as of {e(month_label(month))}. Proposed rates never fall below the
 current contract; rows at market read "keep".{e(nr)}</div>
{geo_banner}
{value_band}
<h2>Proposed fee schedule</h2>
<table><thead><tr><th>Code</th><th class="num">Current</th><th class="num">Proposed</th>
<th class="num">Change</th><th class="num">Annual units</th><th class="num">Annual value</th>
<th class="num">Peers</th></tr></thead>
<tbody>{rows_html}</tbody></table>
{_anchors_section(store, prop["market"], {(prop["payer"], r["billing_code"]): r.get("current") for r in prop["rows"]})}
<footer>METHODOLOGY\n{e(footer)}</footer>
</body></html>"""


def require_geographic_scope(market: dict, kind: str) -> str:
    """Client-facing reports must not silently pool multiple states — negotiated
    reimbursement varies by geography, so a national comparison has to be an
    explicit, labeled choice, never an accident in a deliverable (invariant 4:
    honesty). Returns a warning banner when national is deliberately allowed;
    raises otherwise. The exploratory Benchmark tab is unaffected — this guards
    the *report* boundary only."""
    if market.get("state"):
        return ""
    # coerce explicitly: a JSON string "false"/"no"/"0" is truthy in Python, so
    # `if market.get("allow_national")` would treat a literal refusal as consent
    an = market.get("allow_national")
    allowed = an is True or (isinstance(an, str) and an.strip().lower() in ("true", "1", "yes"))
    if allowed:
        return ("NATIONAL COMPARISON — no state filter was applied, so providers "
                "across every loaded state are pooled into one distribution. "
                "Negotiated reimbursement varies by geography; these percentiles "
                "blend markets and are not an in-state benchmark.")
    raise BenchmarkError(
        f"{kind} requires a state — reimbursement varies by state, so a "
        "client report must be scoped to one (set the State field). To run a "
        "deliberate pooled national comparison, pass market.allow_national=true.")


def _subject_has_scoped_rates(store: Store, subject: str, market: dict) -> bool:
    """Does the subject have any rate rows under the FULL market scope (state,
    discipline, basis, month)? Asked only when the market side is EMPTY: then
    the report has nothing to compare and its subject must be inside the
    market scope for it to say anything (the AK case of test_v4). The
    subject's TINs follow the subject rule (subject_tins_for_market)."""
    market = resolve_plan_scope(store, normalize_market(market))
    subject_tins = subject_tins_for_market(store, subject, market)
    if not subject_tins:
        return False
    where, params = _market_where(market,
                                  bool(market.get("include_assistant", False)),
                                  bool(market.get("include_non_dollar", False)))
    with store.connect() as con:
        return bool(con.execute(
            f"SELECT 1 FROM {_rates_relation(market)} t "
            f"LEFT JOIN tin_directory td USING (tin_value) WHERE {where} "
            "AND t.tin_value IN (SELECT unnest(?::VARCHAR[])) LIMIT 1",
            [*params, subject_tins]).fetchone())


def require_subject_content(store: Store, benchmark: dict, kind: str) -> None:
    """A client-facing report must be ABOUT its subject, not merely titled after
    it. A mistyped practice name — or a real one scoped to a state/discipline it
    has no rows under — still produced a branded document headed with the
    practice's name whose entire "Your rate" and "Gap" columns were dashes: an
    absence of data dressed as a finding (invariant 4).

    The test has to match what the page actually SHOWS. The benchmark LEFT JOINs
    the subject onto the market, so:
      - rows present, every `subject_rate` NULL -> the wrong-subject case; refuse.
      - no rows at all -> ambiguous. It means the MARKET side is empty, which
        happens both when the subject is real but has no peers under this scope
        (renders fine: their own rates are genuine, the market columns are just
        empty) and when the scope contains nobody at all. So ask the subject the
        SAME scoped question the report asks.
    Partial coverage is fine and useful — this fires only when NOTHING is priced.

    Unlike the rate card, this report IS the market comparison, so its subject
    stays inside the market scope; the rate card's subject deliberately does not,
    because there the peers are an annotation on the client's own fee schedule."""
    rows = benchmark.get("rows") or []
    if any(r.get("subject_rate") is not None for r in rows):
        return
    if not rows and _subject_has_scoped_rates(
            store, benchmark.get("subject") or "", benchmark.get("market") or {}):
        return
    raise BenchmarkError(
            f"{kind}: no published rates for this practice under the current "
            "filters — there is nothing to compare against the market. Check "
            "the practice name, tax ID or NPI, and widen the as-of month, state or "
            "discipline filters.")


def _geo_banner_html(note: str) -> str:
    return (f'<div class="geo-warn">⚠ {html.escape(note)}</div>') if note else ""


def render_negotiation_report(cfg: MrfxConfig, store: Store, neg: dict) -> str:
    """Print-ready per-payer negotiation one-pager. One benchmark table per
    payer the subject contracts with, ordered weakest-position-first, with a
    cross-payer opportunity summary when volumes were supplied."""
    if not neg.get("market", {}).get("month"):
        raise BenchmarkError("negotiation report requires a pinned as-of month")
    geo_banner = _geo_banner_html(
        require_geographic_scope(neg["market"], "negotiation report"))
    e = html.escape
    target = neg["target_percentile"]
    subject = neg["subject"]
    month = neg["market"].get("month")

    summary = ""
    if neg.get("has_volumes") and neg.get("total_at_target") is not None:
        summary = (
            f'<p class="band">Lifting this contract to p{target} is worth '
            f'<b>{_m(neg["total_at_target"])}</b>/yr '
            f'(conservative p{neg["conservative_percentile"]}: '
            f'<b>{_m(neg["total_at_conservative"])}</b>).</p>'
        )
    elif neg.get("has_volumes") and neg.get("best_single_payer"):
        b = neg["best_single_payer"]
        summary = (
            f'<p class="band">The largest single-payer opportunity is with '
            f'<b>{e(b["payer"])}</b>: <b>{_m(b["at_target"])}</b>/yr at p{target} '
            f'(conservative p{neg["conservative_percentile"]}: <b>{_m(b["at_conservative"])}</b>).</p>'
            f'<p class="note">{e(neg["volume_note"])}</p>'
        )

    def payer_section(sec) -> str:
        bench = sec["benchmark"]
        payer = sec["payer"]
        hp = sec.get("headline_percentile")
        hp_txt = (f"You sit at roughly <b>p{hp:.0f}</b> among this payer's other "
                  f"providers." if hp is not None else "")
        rows_html = "".join(
            f"<tr><td>{e(r['billing_code'])}<div class='sub'>{e(r['description'] or '')}</div></td>"
            f"<td class='num'>{_m(r['subject_rate'])}</td>"
            f"<td class='num'>{_m(r['p25'])}</td><td class='num'>{_m(r['p50'])}</td>"
            f"<td class='num'>{_m(r['p75'])}</td>"
            f"<td class='num'>{_m(r['target_rate'])}</td>"
            f"<td class='num gap'>{_m(r['gap_to_target'])}</td>"
            f"<td class='num sub'>{r['n_peers'] or 0}</td></tr>"
            for r in bench["rows"] if r.get("subject_rate") is not None
        )
        opp = sec.get("opportunity")
        opp_line = ""
        if opp:
            opp_line = (
                f'<p class="note">Annual opportunity with {e(payer)}: '
                f'<b>{_m(opp["total_at_target"])}</b> at p{target} '
                f'(conservative {_m(opp["total_at_conservative"])}).</p>'
            )
        return f"""
        <h2>{e(payer)}</h2>
        <p class="meta">{hp_txt} {sec['n_codes']} benchmarked code(s), as of {e(month_label(month))}.</p>
        <table><thead><tr><th>Code</th><th class="num">Your rate</th>
        <th class="num">P25</th><th class="num">Median</th><th class="num">P75</th>
        <th class="num">Target (p{target})</th><th class="num">Gap</th>
        <th class="num">Peers</th></tr></thead>
        <tbody>{rows_html}</tbody></table>
        {opp_line}
        """

    body_sections = "".join(payer_section(s) for s in neg["sections"])
    # methodology: reuse the footer of the weakest-position payer's benchmark,
    # but scope the payer list to every payer in the report
    foot_bench = dict(neg["sections"][0]["benchmark"])
    foot_bench["market"] = {**foot_bench["market"], "payers": neg["payers"]}
    footer = methodology_footer(store, foot_bench)
    return f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<title>Payer negotiation one-pager — {e(subject)}</title>
<style>
 body {{ font: 13px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif; color: #101828;
        max-width: 900px; margin: 32px auto; padding: 0 24px; }}
 h1 {{ font-size: 20px; margin-bottom: 2px; }} h2 {{ font-size: 15px; margin-top: 30px;
        border-bottom: 2px solid #101828; padding-bottom: 3px; }}
 .brand {{ color: #475467; font-size: 12px; text-transform: uppercase; letter-spacing: .06em; }}
 .brandbar {{ display: flex; align-items: center; gap: 10px; margin-bottom: 4px; }}
 .brandbar .brand {{ margin: 0; }} .logo {{ max-height: 40px; max-width: 200px; }}
 .meta {{ color: #475467; margin-bottom: 10px; }}
 table {{ border-collapse: collapse; width: 100%; font-variant-numeric: tabular-nums; }}
 th, td {{ text-align: left; padding: 6px 8px; border-bottom: 1px solid #eaecf0; vertical-align: middle; }}
 th {{ font-size: 11px; color: #667085; text-transform: uppercase; letter-spacing: .04em; }}
 .num {{ text-align: right; }} .sub {{ color: #667085; font-size: 11.5px; }}
 .gap {{ font-weight: 650; }}
 .band {{ font-size: 14px; background: #f2f4f7; padding: 10px 14px; border-radius: 6px; }}
 .note {{ color: #475467; font-size: 12px; }}
 .geo-warn {{ background: #fbe9d0; border: 1px solid #d99a3a; color: #7a4a00;
          padding: 10px 14px; border-radius: 6px; font-size: 12.5px; margin: 12px 0; }}
 footer {{ margin-top: 36px; border-top: 1px solid #d0d5dd; padding-top: 12px;
          color: #475467; font-size: 11px; white-space: pre-wrap; }}
 @page {{ size: landscape; margin: 10mm; }}
 @media print {{ body {{ margin: 0; max-width: none; }} table {{ font-size: 10.5px; }} th, td {{ padding: 3px 5px; }} * {{ -webkit-print-color-adjust: exact; print-color-adjust: exact; }} h2 {{ break-after: avoid; }} }}
</style></head><body>
{_brand_header(cfg)}
<h1>Payer negotiation one-pager — {e(subject)}</h1>
<div class="meta">Where each payer pays you versus the peers it pays, as of {e(month_label(month))}.
 Ordered weakest position first.</div>
{geo_banner}
{summary}
{body_sections}
{_anchors_section(store, neg["market"], {(sec["payer"], r["billing_code"]): r.get("subject_rate") for sec in neg["sections"] for r in sec["benchmark"]["rows"]})}
<footer>METHODOLOGY\n{e(footer)}</footer>
</body></html>"""


# ---------------------------------------------------------------------------
# pitch report (§7B.5)
# ---------------------------------------------------------------------------


def _pooled_lines(store: Store, payers: list) -> list[str]:
    """The pooled-months line for the market's payers (review 12 item 2.5)."""
    try:
        from .quality import pooled_files_sentence, pooled_month_files
        rows = [r for r in pooled_month_files(store) if not payers or r["payer"] in payers]
        line = pooled_files_sentence(rows)
        return [line] if line else []
    except Exception:  # noqa: BLE001 — a footer line never fails a report
        return []


def methodology_footer(store: Store, benchmark: dict) -> str:
    market = benchmark["market"]
    with store.connect() as con:
        # scope the source list to the market's payers when the market is
        # payer-scoped — a single-payer report must not claim every payer's
        # files as its sources
        payers = market.get("payers") or []
        q = ("SELECT filename, payer, last_updated_on FROM files WHERE status = 'done' "
             "AND file_type = 'in_network' ")
        if payers:
            q += f"AND payer IN ({', '.join('?' for _ in payers)}) "
        files = con.execute(q + "ORDER BY filename", payers).fetchall()
    # a plan-scoped number was computed from that plan's files ALONE — listing
    # the payer's whole catalogue underneath it would be a false provenance
    # claim, the one thing the footer exists to prevent
    plan = str(market.get("plan") or "").strip()
    plan_line = ""
    if plan:
        keep = set(store.files_for_plan(plan))
        files = [f for f in files if f[0] in keep]
        plan_line = (
            f"Plan scope: {plan}. Only the files that plan is listed on in its "
            "payer's table-of-contents are included. A payer commonly publishes "
            "ONE file for several plans, so rates shared with those plans are "
            "part of this scope.")
    lines = [
        f"Generated {dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} by MRF Explorer v{__version__}.",
        f"As-of month: {month_label(market.get('month'))}. Peer basis: {benchmark['peer_set']}.",
        f"Market definition: {market_in_words(market)}.",
        *([plan_line] if plan_line else []),
        benchmark["basis_note"],
        "Dedup rule: one row per (payer, TIN, code, modifier-set, billing class, "
        "place-of-service set, month); a TIN's rate is the median of its distinct "
        "published values excluding $0/$0.01 dollar placeholders (variants flagged).",
        PRACTICE_RATE_RULE,
        *_pooled_lines(store, payers),
        *store.analytics_note(),
        "Rate files in the store matching the market's payer scope (the "
        "benchmark draws on the subset matching the full market definition): "
        f"{'; '.join(f'{f[0]} ({f[1]}, {f[2]})' for f in files) or 'none'}.",
        GHOST_RATE_NOTE,
        COLLECTION_NOTE,
    ]
    if benchmark.get("mpfs_loaded"):
        lines.append(
            "% of Medicare anchor: the non-facility Medicare rate of "
            f"{benchmark.get('mpfs_source')} — chosen for the market's state, else "
            "the practice's main state; a state with no loaded locality shows no %.")
    elif benchmark.get("mpfs_refusal"):
        lines.append(benchmark["mpfs_refusal"])
    return "\n".join(lines)


def _erosion_block(store: Store, benchmark: dict, e) -> str:
    """The real-terms sentence, for a printed deliverable.

    Cosmetic tier in the strict sense: a market with one month, or a year the
    price index does not cover, produces NOTHING here — never an estimate, and
    never a failed report.
    """
    progress.current().stage("Measuring real-terms erosion")
    try:
        from .monitor import compute_payer_trajectory
        market = dict(benchmark.get("market") or {})
        payers = market.get("payers") or []
        # the report's as-of month bounds the trajectory (review 9 item 4.1)
        traj = compute_payer_trajectory(store, dict(market))
    except Exception:  # noqa: BLE001 — one month in the store is the usual case
        return ""
    rows = [p for p in traj.get("payers", [])
            if p.get("real_cumulative_pct") is not None
            and (not payers or p["payer"] in payers)]
    if not rows:
        return ""
    inf = traj.get("inflation") or {}
    lines = "".join(
        f"<tr><td>{e(p['payer'])}</td>"
        f"<td class='num muted'>{e(p['first_month'])}→{e(p['last_month'])}</td>"
        f"<td class='num'>{signed_pct(p['cumulative_pct'])}</td>"
        f"<td class='num gap'>{signed_pct(p['real_cumulative_pct'])}</td></tr>"
        for p in rows)
    worst = min(rows, key=lambda p: p["real_cumulative_pct"])
    # These are the MARKET's per-payer median trajectories (every practice in
    # scope, per month) — printed unlabelled inside a client's own report they
    # read as the client's contracts: the client's 97110 went $40 -> $50
    # while three peers went $60 -> $50, and the report told the client
    # "Zed Health: -16.7 % ... the cut is deeper than it looks" (round 7
    # P1.14). Say whose numbers they are.
    who = e(str(benchmark.get("subject") or "this practice"))
    return (
        "<h2>In real terms — the market, not this practice</h2>"
        "<p class='note'>Each line is how that payer's rates moved across the practices "
        "in this market scope that it priced on the same codes in both months (each "
        "practice against its own earlier rate; median per code, then across codes) — "
        f"the market's trajectory, not {who}'s own contracts.</p>"
        f"<p>Market — {e(worst['erosion_line'])}</p>"
        "<table><thead><tr><th>Payer</th><th class='num'>Span</th>"
        "<th class='num'>Change in dollars</th>"
        "<th class='num'>Change in real terms</th></tr></thead>"
        f"<tbody>{lines}</tbody></table>"
        f"<p class='note'>{e(inf.get('caveat') or '')}"
        + (f" Basis: an assumed {inf.get('assumed_pct_per_year')}%/yr, "
           "supplied by the analyst — not measured."
           if inf.get("basis") == "assumption" else
           f" Basis: {e(str(worst.get('index_name') or inf.get('index_name') or ''))} "
           f"({e(str(worst.get('index_source') or inf.get('source') or ''))}).")
        + "</p>")


def _stale_block(benchmark: dict, e) -> str:
    """Name any payer behind this report that has not re-published recently.
    A reader is about to quote these numbers to that payer."""
    stale = benchmark.get("stale_payers") or []
    if not stale:
        return ""
    names = "; ".join(
        f"{p['payer']} (last published {p['published']}"
        + (f", {p['age_days']} days ago)" if p.get("age_days") is not None else ")")
        for p in stale)
    return (
        "<p class='geo-warn'><b>Publication age:</b> "
        f"{e(names)}. An old file does not mean an old contract — payers "
        "refresh on their own cadences — but these rates have not been "
        "re-published recently, which is worth saying before acting on them."
        "</p>")


def render_pitch_report(cfg: MrfxConfig, store: Store, benchmark: dict,
                        opportunity: dict | None = None) -> str:
    """Print-ready standalone HTML. Refuses to render without an as-of month
    or methodology footer (§8.10 — they are generated here, unconditionally)."""
    if not benchmark.get("market", {}).get("month"):
        raise BenchmarkError("pitch report requires a pinned as-of month")
    prog = progress.current()
    prog.stage("Checking the practice has rates in this market")
    require_subject_content(store, benchmark, "pitch report")
    geo_banner = _geo_banner_html(
        require_geographic_scope(benchmark["market"], "pitch report"))
    prog.stage("Listing the source files")
    footer = methodology_footer(store, benchmark)
    e = html.escape
    target = benchmark["target_percentile"]

    def strip(r) -> str:
        pct = r.get("subject_percentile")
        if pct is None:
            return ""
        return (
            f'<div class="strip"><div class="you" style="left:{min(max(pct, 0), 100)}%"></div></div>'
            f'<span class="pctlbl">P{pct:.0f}</span>'
        )

    mp = benchmark.get("mpfs_loaded")
    # A printed report states one number per code as of one month; the client's
    # real question is which way it is moving. Cosmetic tier — series_by_code
    # returns {} rather than raising, so a trend column can never cost a
    # deliverable (invariant 3).
    from .spark import SPARK_CSS, SPARK_NOTE, series_by_code, sparkline
    prog.stage("Drawing the rate trend lines")
    series = series_by_code(
        store, subject_tins_for_market(store, benchmark["subject"],
                                       benchmark.get("market") or {}),
        benchmark.get("market") or {},
        codes=[r["billing_code"] for r in benchmark["rows"]])
    has_trend = any(len(v) >= 2 for v in series.values())

    def trend(r) -> str:
        return (f"<td class='num'>{sparkline(series.get(r['billing_code']), label=r['billing_code'])}</td>"
                if has_trend else "")

    rows_html = "".join(
        f"<tr><td>{e(r['billing_code'])}<div class='sub'>{e(r['description'] or '')}"
        f"{' · timed 15-min' if r['is_timed'] else ''}</div></td>"
        f"<td class='num'>{_m(r['subject_rate'])}</td>"
        f"<td class='num'>{_m(r['p25'])}</td><td class='num'>{_m(r['p50'])}</td>"
        f"<td class='num'>{_m(r['p75'])}</td>"
        f"<td class='num'>{_m(r['target_rate'])}</td>"
        f"<td class='num gap'>{_m(r['gap_to_target'])}</td>"
        + (f"<td class='num'>{_pctnum(r.get('subject_pct_medicare'))}</td>"
           f"<td class='num'>{_pctnum(r.get('median_pct_medicare'))}</td>" if mp else "")
        + trend(r)
        + f"<td class='pos'>{strip(r)}</td>"
        # the peer count behind every percentile (round 6 P3): P25-P75 over
        # one or two practices is a competitor's rate, not a market's
        + f"<td class='num sub'>{int(r.get('n_peers') or 0):,}"
        + (" <span class='thin'>thin</span>" if (r.get("n_peers") or 0) < THIN_PEERS else "")
        + "</td></tr>"
        for r in benchmark["rows"]
    )
    n_thin = sum(1 for r in benchmark["rows"] if (r.get("n_peers") or 0) < THIN_PEERS)
    thin_note = (
        f'<p class="note">{n_thin} code(s) marked <i>thin</i> rest on fewer than '
        f'{THIN_PEERS} peer practices: their percentiles describe one or two '
        'competitors, not a market. Weigh them accordingly.</p>' if n_thin else "")
    opp_html = ""
    if opportunity:
        opp_rows = "".join(
            f"<tr><td>{e(o['billing_code'])}</td><td class='num'>{o['annual_units']:,.0f}</td>"
            f"<td class='num'>{_m(o['subject_rate'])}</td><td class='num'>{_m(o['target_rate'])}</td>"
            f"<td class='num'>{_m(o['opportunity_at_conservative'])}</td>"
            f"<td class='num'>{_m(o['opportunity_at_target'])}</td></tr>"
            for o in opportunity["rows"]
        )
        opp_html = f"""
        <h2>Annual gross opportunity</h2>
        <p class="band">Conservative (p{opportunity['conservative_percentile']}):
           <b>{_m(opportunity['total_at_conservative'])}</b> &nbsp;·&nbsp;
           At target (p{opportunity['target_percentile']}):
           <b>{_m(opportunity['total_at_target'])}</b> per year</p>
        <table><thead><tr><th>Code</th><th class="num">Annual units</th>
        <th class="num">Your rate</th><th class="num">Target</th>
        <th class="num">Opportunity (conservative)</th><th class="num">Opportunity (target)</th></tr></thead>
        <tbody>{opp_rows}</tbody></table>
        <p class="note">{e(opportunity['assumptions'])}</p>
        """

    mp_heads = "<th class='num'>% Medicare (you)</th><th class='num'>% Medicare (median)</th>" if mp else ""
    trend_head = "<th class='num'>Your trend</th>" if has_trend else ""
    trend_note = (f'<p class="note">{e(SPARK_NOTE)}</p>' if has_trend else "")
    # The strongest sentence in a renegotiation letter: a rate held flat is a
    # pay cut once costs rise. Cosmetic tier — a missing index year yields
    # nothing here rather than an estimate, and never blocks the report.
    erosion_html = _erosion_block(store, benchmark, e)
    stale_html = _stale_block(benchmark, e)
    return f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<title>Rate benchmark — {e(benchmark['subject'])}</title>
<style>
 body {{ font: 13px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif; color: #101828;
        max-width: 900px; margin: 32px auto; padding: 0 24px; }}
 h1 {{ font-size: 20px; margin-bottom: 2px; }} h2 {{ font-size: 15px; margin-top: 28px; }}
 .brand {{ color: #475467; font-size: 12px; text-transform: uppercase; letter-spacing: .06em; }}
 .brandbar {{ display: flex; align-items: center; gap: 10px; margin-bottom: 4px; }}
 .brandbar .brand {{ margin: 0; }} .logo {{ max-height: 40px; max-width: 200px; }}
 .meta {{ color: #475467; margin-bottom: 18px; }}
 table {{ border-collapse: collapse; width: 100%; font-variant-numeric: tabular-nums; }}
 th, td {{ text-align: left; padding: 6px 8px; border-bottom: 1px solid #eaecf0; vertical-align: middle; }}
 th {{ font-size: 11px; color: #667085; text-transform: uppercase; letter-spacing: .04em; }}
 .num {{ text-align: right; }} .sub {{ color: #667085; font-size: 11.5px; }}
 .thin {{ color: #b54708; font-weight: 600; }}
 .gap {{ font-weight: 650; }}
 .strip {{ position: relative; width: 120px; height: 8px; background: #eee;
          border-radius: 4px; display: inline-block; vertical-align: middle; }}
 .you {{ position: absolute; top: -3px; width: 3px; height: 14px; background: #2a78d6; border-radius: 2px; }}
 .pctlbl {{ font-size: 11px; color: #475467; margin-left: 6px; }}
 .band {{ font-size: 14px; }}
 .note {{ color: #475467; font-size: 12px; }}
 .geo-warn {{ background: #fbe9d0; border: 1px solid #d99a3a; color: #7a4a00;
          padding: 10px 14px; border-radius: 6px; font-size: 12.5px; margin: 12px 0; }}
{SPARK_CSS}
 footer {{ margin-top: 36px; border-top: 1px solid #d0d5dd; padding-top: 12px;
          color: #475467; font-size: 11px; white-space: pre-wrap; }}
 @page {{ size: landscape; margin: 10mm; }}
 @media print {{ body {{ margin: 0; max-width: none; }} table {{ font-size: 10.5px; }} th, td {{ padding: 3px 5px; }} * {{ -webkit-print-color-adjust: exact; print-color-adjust: exact; }} }}
</style></head><body>
{_brand_header(cfg)}
<h1>Negotiated-rate benchmark — {e(benchmark['subject'])}</h1>
<div class="meta">As of {e(month_label(benchmark['market'].get('month')))} ·
 {e(benchmark['peer_set'])} · target: p{target}</div>
{geo_banner}
<table><thead><tr><th>Code</th><th class="num">Your rate</th><th class="num">P25</th>
<th class="num">Median</th><th class="num">P75</th><th class="num">Target (p{target})</th>
<th class="num">Gap</th>{mp_heads}{trend_head}<th>Your position</th><th class="num">Peers</th></tr></thead>
<tbody>{rows_html}</tbody></table>
{thin_note}
{trend_note}
{erosion_html}
{opp_html}
{stale_html}
<footer>METHODOLOGY\n{e(footer)}</footer>
</body></html>"""


def _brand_header(cfg: MrfxConfig) -> str:
    """Report-as-a-service header: the consultant's brand name plus, when a
    readable ``report_branding.logo_path`` is configured, their logo inlined as
    a data URI (reports must be self-contained single files — no external
    fetches when a client opens the HTML offline). A missing or unreadable logo
    is cosmetic and never fails the report (invariant 3)."""
    e = html.escape
    name = e(cfg.report_branding.name)
    logo = cfg.report_branding.logo_path
    if logo:
        try:
            import base64
            import mimetypes
            data = logo.read_bytes()
            mime = mimetypes.guess_type(str(logo))[0] or "image/png"
            b64 = base64.b64encode(data).decode("ascii")
            return (f'<div class="brandbar"><img class="logo" alt="{name}" '
                    f'src="data:{mime};base64,{b64}"><span class="brand">{name}</span></div>')
        except Exception as exc:  # noqa: BLE001 — logo is decoration, never blocks a report
            log.warning("report logo %s could not be embedded (%s); using text brand", logo, exc)
    return f'<div class="brand">{name}</div>'


def _m(v) -> str:
    if v is None:
        return "–"
    # negative dollars read as -$50.00, not $-50.00 (a subject above the target
    # produces a negative gap in the report's Gap column)
    return f"-${abs(v):,.2f}" if v < 0 else f"${v:,.2f}"


def _pctnum(v) -> str:
    return f"{half_up(v)}%" if v is not None else "–"


# ---------------------------------------------------------------------------
# Negotiate (0.8.6): the named clinics THIS payer pays more for the same codes
# ---------------------------------------------------------------------------

PAID_MORE_MIN_CITY = 5
PAID_MORE_NOTE = (
    "Clinics this payer pays more: every comparable practice (the 'Compare "
    "against' group — outpatient rehab clinics unless you chose otherwise) the "
    "same payer prices for at least one of your codes, in your city when at least "
    f"{PAID_MORE_MIN_CITY} such practices are there, otherwise in your state. A "
    "practice's rate for a code is the median of its published rates for it on "
    "the same basis as your own (newest month per contract, dollar rates, "
    "professional claims, base modifiers). 'Paid more' compares rates rounded to "
    "the cent. Ranked by how many of your codes it is paid more for, then by its "
    "median gap, then by tax ID.")


def clinics_paid_more(store: Store, subject: str, payer: str, market: dict, *,
                      limit: int = 100) -> dict:
    """The named practices `payer` pays more than `subject` for the same
    codes, in the subject's city (when PAID_MORE_MIN_CITY comparable practices
    are there) else its state — the evidence a renegotiation letter cites."""
    import statistics as _st

    from .store import mask_tin
    payer = str(payer or "").strip()
    if not payer:
        raise BenchmarkError("pick the payer you're negotiating with")
    # outpatient rehab clinics unless the caller CHOSE otherwise ("all" is a
    # choice: normalize_market drops it, so read what was sent)
    asked = (market or {}).get("peer_group") if isinstance(market, dict) else None
    market = resolve_plan_scope(store, normalize_market(market))
    if asked in (None, ""):
        market["peer_group"] = "clinics"
    m = {**market, "payers": [payer]}
    tins = subject_tins_for_market(store, subject, m)
    if not tins:
        raise BenchmarkError(f"no practice matches {subject!r}")
    state = (m.get("state") or anchor_state(store, m, tins) or "").upper() or None
    if not state:
        raise BenchmarkError("pick a state — the comparison is local")
    # the subject's own rate per code with this payer (median across its
    # TINs — the rate card's rule), on the shared basis
    sq, sp = practice_base_sql(store, subject_market(m), per_payer=True,
                               extra=" AND t.payer = ? AND t.tin_value IN (SELECT unnest(?::VARCHAR[]))",
                               extra_params=[payer, tins],
                               prefilter="payer = ? AND tin_value IN (SELECT unnest(?::VARCHAR[]))",
                               prefilter_params=[payer, tins], scoped_if_cold=True)
    with store.connect() as con:
        srows = con.execute(f"SELECT billing_code, tin_value, rate FROM ({sq})", sp).fetchall()
    by_code: dict[str, list[float]] = {}
    for code, _t, rate in srows:
        if rate is not None:
            by_code.setdefault(code, []).append(float(rate))
    mine = {c: half_up(_st.median(v), 2) for c, v in by_code.items()}
    if not mine:
        raise BenchmarkError(f"{subject} has no published rate with {payer} on this basis")
    codes = sorted(mine)
    # the subject's main city in that state (its NPIs' practice locations)
    named = store.practice_names_for(tins, state)
    city_votes: dict[str, int] = {}
    for t in tins:
        for i, c in enumerate((named.get(t) or {}).get("top_cities") or []):
            city_votes[c] = city_votes.get(c, 0) + (3 - i)
    city = sorted(city_votes.items(), key=lambda kv: (-kv[1], kv[0]))[0][0] if city_votes else None

    def peer_rows(geo: dict) -> list:
        in_c = ", ".join("?" for _ in codes)
        q, p = practice_base_sql(
            store, {**m, **geo, "codes": codes}, per_payer=True,
            extra=(f" AND t.payer = ? AND t.billing_code IN ({in_c})"
                   " AND t.tin_value NOT IN (SELECT unnest(?::VARCHAR[]))"),
            extra_params=[payer, *codes, tins],
            prefilter=f"payer = ? AND billing_code IN ({in_c})",
            prefilter_params=[payer, *codes], scoped_if_cold=True)
        with store.connect() as con:
            return con.execute(f"SELECT billing_code, tin_value, rate FROM ({q}) "
                               "ORDER BY tin_value, billing_code", p).fetchall()
    level, rows = "state", None
    if city:
        rows = peer_rows({"state": state, "city": city})
        if len({t for _c, t, _r in rows}) >= PAID_MORE_MIN_CITY:
            level = "city"
        else:
            rows = None
    if rows is None:
        rows = peer_rows({"state": state})
    peers: dict[str, dict[str, float]] = {}
    for code, tin, rate in rows:
        if rate is not None:
            peers.setdefault(tin, {})[code] = half_up(float(rate), 2)
    code_rows = []
    for c in codes:
        vals = sorted(v[c] for v in peers.values() if c in v)
        desc, _, _t = _code_info(c)
        code_rows.append({
            "code": c, "description": desc, "your_rate": mine[c], "practices": len(vals),
            "median": half_up(_st.median(vals), 2) if vals else None,
            "highest": vals[-1] if vals else None,
            "paid_more": sum(1 for v in vals if v > mine[c]),
            "gap_to_median": half_up(_st.median(vals) - mine[c], 2) if vals else None})
    clinics = []
    for tin, rates in peers.items():
        shared = [c for c in codes if c in rates]
        higher = [c for c in shared if rates[c] > mine[c]]
        if not higher:
            continue
        gaps = [100.0 * (rates[c] - mine[c]) / mine[c] for c in higher if mine[c]]
        clinics.append({"_tin": tin, "codes_compared": len(shared), "codes_paid_more": len(higher),
                        "median_gap_pct": half_up(_st.median(gaps), 1) if gaps else None,
                        "rates": [{"code": c, "rate": rates[c], "yours": mine[c],
                                   "gap": half_up(rates[c] - mine[c], 2)} for c in shared]})
    clinics.sort(key=lambda x: (-x["codes_paid_more"], -(x["median_gap_pct"] or 0), x["_tin"]))
    total = len(clinics)
    clinics = clinics[:max(1, min(int(limit or 100), 1000))]
    info = store.practice_names_for([x["_tin"] for x in clinics], state)
    with store.connect() as con:
        meta = {r[0]: r[1:] for r in con.execute(
            "SELECT tin_value, practice_type, type_reason, npi_count FROM tin_directory "
            "WHERE tin_value IN (SELECT unnest(?::VARCHAR[]))",
            [[x["_tin"] for x in clinics]]).fetchall()}
    for rank, x in enumerate(clinics, 1):
        tin = x.pop("_tin")
        nm = info.get(tin, {})
        pt, why, n_npi = meta.get(tin, (None, None, None))
        cities = list(nm.get("top_cities") or [])
        if level == "city" and city:
            # every clinic on a city list has a clinician there (the filter),
            # though a multi-site group's top three may be elsewhere — lead with it
            cities = [city] + [c for c in cities if c != city][:2]
        x.update(rank=rank, tin=mask_tin(tin), name=nm.get("practice_name") or f"TIN {mask_tin(tin)}",
                 name_source=nm.get("name_source"), cities=cities,
                 practice_type=pt, type_reason=why, npi_count=n_npi)
    group = PEER_GROUPS.get(m.get("peer_group"), "every practice the payer prices")
    where = f"{city}, {state}" if level == "city" else state
    return {
        "subject": subject, "payer": payer, "state": state, "city": city,
        "geography": level, "where": where, "peer_group": m.get("peer_group") or "all",
        "compared_with": group, "practices_compared": len(peers),
        "clinics_paid_more": total, "codes": code_rows, "clinics": clinics,
        "headline": (f"{payer} pays {total} of {len(peers)} comparable {group} in {where} "
                     f"more than you for at least one of your {len(codes)} codes."
                     if peers else f"{payer} prices no comparable {group} in {where} "
                     "for your codes."),
        "note": PAID_MORE_NOTE,
        "market": public_market(m, drop=("payers",)),
    }


def _code_info(code: str):
    from .catalog import code_info
    return code_info(code)
