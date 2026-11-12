"""The durable, sliced analytics build (review 13 item 1.3) — the pure parts.

A FULL rollup used to be one pass per hash slice over the WHOLE raw store,
per table, inside one transaction per table: O(slices x rows), thrown away
whole by a kill, an out-of-memory error or the queue's three-failure give-up
(measured 65 min at 306M rows on a desktop; the field store at 1.16 B rows on
a laptop never finished and the dashboard answered from 3 of 49 payers).

The build is now a PLAN of slices kept in the store's `meta` table, each slice
committed on its own together with the plan's "next slice" pointer, so:

* a restart continues from the last committed slice (a kill loses at most the
  slice that was running);
* the spine is queryable while it is built: a payer's rows appear the moment
  its slice commits (the dashboard's coverage line moves 3 -> 4 -> ... -> 49);
* an out-of-memory slice is HALVED and retried — the code range in two, or a
  single code into two hash halves — and the halving is kept in the plan;
* a spine slice reads only its own payer's rows: the grain (and `is_latest`'s
  contract partition) includes payer and billing code, so a slice
  `payer = P AND billing_code in [lo, hi)` is exact, and Parquet's per-row-
  group statistics on payer and billing code (files group their items by
  code) let DuckDB skip every other row group. The old hash slices read every
  row group of every part on every slice.

Everything here is pure (no database): slice planning, the SQL predicate of a
slice, halving, and the plain-language failure sentence. Store runs them
(Store._run_plan)."""
from __future__ import annotations

import math

# A directory slice aggregates (TIN, NPI) pairs, not rate rows: its memory is
# a few percent of a spine slice over the same raw rows, so it is cut this
# many times coarser (measured on the 99M-rate store: one directory statement
# over all 99M rows peaked under 1 GB). An out-of-memory directory slice is
# halved like any other.
DIR_ROWS_FACTOR = 16

# Cost of a directory slice relative to a spine slice over the same rows, for
# the percentage and the ETA only (it never changes a number): a directory
# slice scans every raw row but aggregates far less.
DIR_WORK_FACTOR = 0.25

# Never cut a slice finer than this many hash classes: past it the failure is
# not the slice's size (a broken disk, a full temp drive) and is reported.
MAX_HASH_CLASSES = 1024


def q(s: str) -> str:
    """A SQL string literal."""
    return "'" + str(s).replace("'", "''") + "'"


def spine_slices(counts: list[tuple], rows_per_slice: int) -> list[dict]:
    """Spine slices from `(payer, billing_code, rows)` triples.

    Per payer (sorted, NULL last) the payer's codes (sorted, NULL apart) are
    packed into runs of at most `rows_per_slice` raw rows; a run covers the
    half-open code range [lo, next run's lo) so the runs of one payer
    partition the WHOLE code space (a code that appears after the plan was
    made still belongs to exactly one slice). A single code bigger than the
    budget gets its own run cut into k hash classes."""
    rows_per_slice = max(1, int(rows_per_slice))
    by_payer: dict = {}
    for payer, code, n in counts:
        by_payer.setdefault(payer, []).append((code, int(n or 0)))
    out: list[dict] = []
    payers = sorted((p for p in by_payer if p is not None)) + (
        [None] if None in by_payer else [])
    for payer in payers:
        codes = by_payer[payer]
        null_rows = sum(n for c, n in codes if c is None)
        named = sorted((c, n) for c, n in codes if c is not None)
        runs: list[list] = []
        cur: list = []
        cur_rows = 0
        for c, n in named:
            if cur and cur_rows + n > rows_per_slice:
                runs.append(cur)
                cur, cur_rows = [], 0
            cur.append([c, n])
            cur_rows += n
        if cur:
            runs.append(cur)
        for j, run in enumerate(runs):
            lo = run[0][0] if j > 0 else None          # first run: everything below
            hi = runs[j + 1][0][0] if j + 1 < len(runs) else None
            rows = sum(n for _c, n in run)
            k = max(1, math.ceil(rows / rows_per_slice)) if len(run) == 1 else 1
            for i in range(k):
                out.append({"t": "spine", "payer": payer, "lo": lo, "hi": hi,
                            "null_code": False, "cc": run, "k": k, "i": i,
                            "rows": rows, "work": rows})
        if null_rows:
            out.append({"t": "spine", "payer": payer, "lo": None, "hi": None,
                        "null_code": True, "cc": [[None, null_rows]], "k": 1, "i": 0,
                        "rows": null_rows, "work": null_rows})
    return out


def dir_slices(n_rows: int, rows_per_slice: int) -> list[dict]:
    """Directory slices: hash classes of tin_value, cut DIR_ROWS_FACTOR times
    coarser than a spine slice (see the constant)."""
    k = max(1, math.ceil(max(0, int(n_rows)) / max(1, rows_per_slice * DIR_ROWS_FACTOR)))
    work = max(1, int(n_rows * DIR_WORK_FACTOR / k)) if n_rows else 1
    return [{"t": "dir", "k": k, "i": i, "rows": int(n_rows), "work": work}
            for i in range(k)]


SPINE_HASH_KEY = "coalesce(billing_code, '') || coalesce(tin_value, '')"
DIR_HASH_KEY = "tin_value"


def slice_pred(sl: dict) -> str:
    """The SQL predicate of a slice — the SAME text selects its raw rows (the
    build) and its rows in the materialized table (the DELETE before it), so
    a slice replaces exactly what it recomputes."""
    if sl["t"] == "dir":
        if sl["k"] <= 1:
            return "TRUE"
        return f"hash({DIR_HASH_KEY}) % {sl['k']} = {sl['i']}"
    parts = ["payer IS NULL" if sl["payer"] is None else f"payer = {q(sl['payer'])}"]
    if sl.get("null_code"):
        parts.append("billing_code IS NULL")
    else:
        if sl.get("lo") is not None:
            parts.append(f"billing_code >= {q(sl['lo'])}")
        if sl.get("hi") is not None:
            parts.append(f"billing_code < {q(sl['hi'])}")
        if sl.get("lo") is None and sl.get("hi") is None:
            parts.append("billing_code IS NOT NULL")
    if sl["k"] > 1:
        parts.append(f"hash({SPINE_HASH_KEY}) % {sl['k']} = {sl['i']}")
    return " AND ".join(parts)


def halve(sl: dict) -> list[dict] | None:
    """Two slices that together cover exactly `sl`, each about half its size,
    or None when it cannot be cut further (MAX_HASH_CLASSES)."""
    if sl["t"] == "spine" and not sl.get("null_code") and len(sl.get("cc") or []) > 1 \
            and sl["k"] == 1:
        cc = sl["cc"]
        total = sum(n for _c, n in cc)
        acc, j = 0, 1
        for j in range(1, len(cc)):
            acc += cc[j - 1][1]
            if acc * 2 >= total:
                break
        left, right = cc[:j], cc[j:]
        mid = right[0][0]
        a = dict(sl, cc=left, hi=mid, rows=sum(n for _c, n in left))
        b = dict(sl, cc=right, lo=mid, rows=sum(n for _c, n in right))
        a["work"], b["work"] = a["rows"], b["rows"]
        return [a, b]
    k = sl["k"] * 2
    if k > MAX_HASH_CLASSES:
        return None
    half_work = max(1, int(sl.get("work", 1)))   # each hash half re-reads the range
    return [dict(sl, k=k, i=sl["i"], work=half_work),
            dict(sl, k=k, i=sl["i"] + sl["k"], work=half_work)]


def slice_label(sl: dict) -> str:
    """What the slice is, in words (log lines, the Files tab)."""
    if sl["t"] == "dir":
        return ("practice names and places" if sl["k"] <= 1
                else f"practice names and places, part {sl['i'] + 1} of {sl['k']}")
    if sl["t"] == "prune":
        return "removing payers no longer loaded"
    who = sl["payer"] if sl["payer"] is not None else "(no payer named)"
    cc = sl.get("cc") or []
    codes = [c for c, _n in cc if c is not None]
    if sl.get("null_code"):
        what = "lines with no billing code"
    elif len(codes) == 1:
        what = codes[0]
    elif codes:
        what = f"{codes[0]}–{codes[-1]}"
    else:
        what = "codes"
    part = f", part {sl['i'] + 1} of {sl['k']}" if sl["k"] > 1 else ""
    return f"{who} · {what}{part}"


def failure_sentence(exc: BaseException, memory_gb: int | None = None) -> str:
    """The analytics build stopped — why, in words a user can act on."""
    msg = str(exc) or exc.__class__.__name__
    low = msg.lower()
    name = exc.__class__.__name__.lower()
    if "outofmemory" in name or "out of memory" in low or "could not allocate" in low \
            or "failed to pin block" in low:
        at = f" at {memory_gb} GB" if memory_gb else ""
        # (review 14: "when the queue is idle" was never true — the analytics
        # scheduler retries after a back-off, downloads or not)
        return (f"ran out of memory{at} — it tries again by itself in a few minutes, "
                "in smaller pieces; raising duckdb_memory_gb in config/mrfx.yaml helps")
    if "max_temp_directory_size" in low or "no space left" in low or "disk full" in low \
            or "not enough space" in low:
        return ("ran out of disk space for its temporary files — free some space "
                "on the store's drive; it tries again by itself in a few minutes")
    first = msg.strip().splitlines()[0] if msg.strip() else exc.__class__.__name__
    return f"stopped: {first[:160]} — it tries again by itself in a few minutes"


def eta_text(seconds: float | None) -> str:
    """'about 2 h left' / 'about 14 min left' / '' (unknown)."""
    if seconds is None or seconds != seconds or seconds < 0:
        return ""
    if seconds < 90:
        return "less than 2 min left"
    if seconds < 3600 * 1.5:
        return f"about {int(round(seconds / 60))} min left"
    h = seconds / 3600
    return f"about {h:.0f} h left" if h >= 10 else f"about {h:.1f} h left".replace(".0 h", " h")


def coverage_sentence(cov: dict | None, *, document: bool = False) -> str:
    """'Analytics cover 3 of 49 payers (3 of 615 files). <what is happening>'
    — empty when the analytics are current. `document=True` is the form a
    report's methodology and every CSV sidecar print (invariant 4: a rate card
    built from 3 of 49 payers must say so)."""
    if not cov or cov.get("state") in (None, "current"):
        return ""
    head = (f"Analytics cover {int(cov.get('covered_payers') or 0):,} of "
            f"{int(cov.get('loaded_payers') or 0):,} payers "
            f"({int(cov.get('covered_files') or 0):,} of "
            f"{int(cov.get('done_files') or 0):,} files).")
    if document:
        # what is TRUE of the rows in place (review 14 items 1.1c / 2.7): a
        # rebuild of an existing spine still holds every loaded file, so it is
        # never "the files loaded since are not in the analytics yet"
        new = int(cov.get("new_files") if cov.get("new_files") is not None
                  else max(0, int(cov.get("done_files") or 0) - int(cov.get("covered_files") or 0)))
        part_way = [p for p in (cov.get("part_way") or []) if p]
        tail = []
        if new:
            tail.append("Every number here comes from those; the files loaded since are not "
                        "in the analytics yet, so payers, practices and months they add are "
                        "missing from this document.")
        if part_way:
            names = ", ".join(part_way[:3]) + (f" and {len(part_way) - 3} more" if len(part_way) > 3 else "")
            tail.append(f"A rebuild is part-way through {names}: numbers that include "
                        f"{'it' if len(part_way) == 1 else 'them'} can change when it finishes.")
        if not new and not part_way:
            if cov.get("rebuilding") or cov.get("plan_kind"):
                ps = cov.get("parts_saved") or [0, 0]
                tail.append("The analytics are being rebuilt"
                            + (f" ({int(ps[0]):,} of {int(ps[1]):,} parts saved)" if ps and ps[1] else "")
                            + "; every loaded file is in them, and every number here comes "
                            "from the rows in place.")
            elif cov.get("pending_removal"):
                tail.append("A removed file's numbers are still being taken out of the "
                            "analytics, so numbers here can still include it.")
            elif cov.get("needs_full"):
                tail.append("The analytics are waiting for one full rebuild; every number "
                            "here comes from the rows in place.")
            else:
                tail.append("Every number here comes from those.")
        return head + " " + " ".join(tail)
    reason = (cov.get("reason") or "").strip()
    return head + (" " + reason if reason else "")
