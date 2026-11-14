"""`mrfx fastcheck <file>` — prove, on YOUR file, that the fast path and the
classic parser produce the same rates.

Runs the file through both parsers exactly as ingest would (two-pass for big
files), writes each result to a temporary Parquet, and compares them as exact
row MULTISETS with DuckDB `EXCEPT ALL` in both directions (out-of-core, so a
30M-row file doesn't need 30M rows of RAM). Also compares every QA counter,
the item counts and the header, and reports both timings. Nothing touches the
store. Use it on a sample of real files from a payer before trusting a long
grind — or any time a number looks off.
"""

from __future__ import annotations

import tempfile
import time
from pathlib import Path

from .config import MrfxConfig

_COLS_EXCEPT_INGESTED = "* EXCLUDE (ingested_at)"


def _parse_one(cfg: MrfxConfig, path: Path, fast: bool, two_pass: bool, out: Path) -> dict:
    import pyarrow.parquet as pq

    from .parser import (
        InNetworkParser,
        batch_to_table,
        skim_needed_ref_ids,
        skim_needed_ref_ids_fast,
    )
    from .sniff import open_stream
    from .store import PARQUET_COMPRESSION, RATES_SCHEMA

    info: dict = {"skim_s": 0.0}
    keep = None
    if two_pass and fast:
        # exactly what ingest runs for a big file on the fast path
        return _big_fast_one(cfg, path, out, info)
    if two_pass:
        t = time.perf_counter()
        with open_stream(path) as s:
            keep, target_items = (skim_needed_ref_ids_fast if fast else skim_needed_ref_ids)(cfg, s)
        info["skim_s"] = time.perf_counter() - t
        info["skim_target_items"] = target_items
        info["skim_ref_ids"] = keep
    writer = None

    def sink(batch) -> None:
        nonlocal writer
        if writer is None:
            writer = pq.ParquetWriter(out, RATES_SCHEMA, compression=PARQUET_COMPRESSION)
        writer.write_table(batch_to_table(batch, RATES_SCHEMA))

    parser = InNetworkParser(cfg, source_file=path.name, sink=sink, keep_ref_ids=keep)
    t = time.perf_counter()
    try:
        with open_stream(path) as s:
            r = parser.parse_fast(s) if fast else parser.parse(s)
    finally:
        if writer is not None:
            writer.close()
    info["extract_s"] = time.perf_counter() - t
    if writer is None:  # 0 rows: an empty table so the comparison still runs
        pq.write_table(RATES_SCHEMA.empty_table(), out)
    info.update(
        rows=r.qa.rows, qa=r.qa.to_dict(), items_seen=r.items_seen,
        items_matched=r.items_matched, ref_groups_skipped=r.ref_groups_skipped,
        header=(r.payer, r.schema_version, r.last_updated_on),
    )
    return info


def _compare_row_sets(duckdb, tmp: str, a: str, b: str) -> tuple[int, int]:
    """(rows only in a, rows only in b) as MULTISETS. First a streaming
    digest — row count plus two independent sums of per-row hashes, one scan
    each, no spill — which settles the common IDENTICAL case on any file
    size. Only when the digests differ does it run the exact EXCEPT ALL to
    count the differing rows; its spill stays inside this check's own temp
    folder (never the current directory)."""
    con = duckdb.connect()
    try:
        spill = Path(tmp) / "spill"
        spill.mkdir(exist_ok=True)
        con.execute(f"SET temp_directory = '{str(spill).replace(chr(39), chr(39) * 2)}'")
        cols = [r[0] for r in con.execute(f"DESCRIBE SELECT * FROM read_parquet('{a}')").fetchall()
                if r[0] != "ingested_at"]
        tup = ", ".join(f'"{c}"' for c in cols)
        digest = ("SELECT count(*), sum(hash({t})::HUGEINT), "
                  "sum(hash('salt', {t})::HUGEINT) FROM read_parquet('{x}')")
        if (con.execute(digest.format(t=tup, x=a)).fetchone()
                == con.execute(digest.format(t=tup, x=b)).fetchone()):
            return 0, 0
        q = ("SELECT count(*) FROM (SELECT {c} FROM read_parquet('{x}') "
             "EXCEPT ALL SELECT {c} FROM read_parquet('{y}'))")
        only_a = con.execute(q.format(c=_COLS_EXCEPT_INGESTED, x=a, y=b)).fetchone()[0]
        only_b = con.execute(q.format(c=_COLS_EXCEPT_INGESTED, x=b, y=a)).fetchone()[0]
        # a digest mismatch with no row difference cannot happen for equal
        # multisets; report it rather than claim IDENTICAL
        return (only_a, only_b) if (only_a or only_b) else (-1, -1)
    finally:
        con.close()


def _big_fast_one(cfg: MrfxConfig, path: Path, out: Path, info: dict) -> dict:
    import pyarrow.parquet as pq

    from .fastscan import FastScanUnsupported
    from .parser import InNetworkParser, batch_to_table, run_big_file_fast
    from .sniff import open_stream
    from .store import PARQUET_COMPRESSION, RATES_SCHEMA

    writer = None

    def sink(batch) -> None:
        nonlocal writer
        if writer is None:
            writer = pq.ParquetWriter(out, RATES_SCHEMA, compression=PARQUET_COMPRESSION)
        writer.write_table(batch_to_table(batch, RATES_SCHEMA))

    t = time.perf_counter()
    try:
        r, target_items = run_big_file_fast(
            cfg, InNetworkParser(cfg, source_file=path.name, sink=sink),
            lambda: open_stream(path), Path(str(out) + ".spool.tmp"))
    except FastScanUnsupported as e:
        raise RuntimeError(f"fast path declined this file ({e}); ingest would use the "
                           "classic parser for it") from e
    finally:
        if writer is not None:
            writer.close()
    if writer is None:
        pq.write_table(RATES_SCHEMA.empty_table(), out)
    info["extract_s"] = time.perf_counter() - t
    info["skim_target_items"] = target_items
    info["skim_ref_ids"] = None    # single read: no separate kept-id set
    info.update(
        rows=r.qa.rows, qa=r.qa.to_dict(), items_seen=r.items_seen,
        items_matched=r.items_matched, ref_groups_skipped=r.ref_groups_skipped,
        header=(r.payer, r.schema_version, r.last_updated_on),
    )
    return info


def compare_parsers(cfg: MrfxConfig, path: Path, two_pass: bool | None = None) -> dict:
    """Both parsers on one file -> a report dict; report['identical'] is the
    verdict. Errors from either side are captured, not raised: a file both
    parsers reject is 'identical' (same outcome); one that only one rejects
    is a difference."""
    import duckdb

    from .ingest import LARGE_FILE_UNCOMPRESSED_BYTES
    from .sniff import preflight

    path = Path(path)
    if two_pass is None:
        est = preflight(path, cfg).est_uncompressed_bytes or 0
        two_pass = est >= LARGE_FILE_UNCOMPRESSED_BYTES
    rep: dict = {"file": path.name, "two_pass": two_pass, "differences": []}
    with tempfile.TemporaryDirectory(prefix="mrfx-fastcheck-") as tmp:
        outs = {}
        for mode in ("classic", "fast"):
            outs[mode] = Path(tmp) / f"{mode}.parquet"
            try:
                rep[mode] = _parse_one(cfg, path, mode == "fast", two_pass, outs[mode])
            except Exception as e:  # noqa: BLE001 — the report IS the product here
                rep[mode] = {"error": f"{type(e).__name__}: {e}"}
        c, f = rep["classic"], rep["fast"]
        if "error" in c or "error" in f:
            if ("error" in c) != ("error" in f):
                rep["differences"].append(
                    f"only one parser failed — classic: {c.get('error', 'ok')}; fast: {f.get('error', 'ok')}")
            rep["identical"] = not rep["differences"]
            return rep
        for k in ("rows", "qa", "items_seen", "items_matched", "ref_groups_skipped", "header"):
            if c[k] != f[k]:
                rep["differences"].append(f"{k}: classic={c[k]!r} fast={f[k]!r}")
        if two_pass:
            if c["skim_target_items"] != f["skim_target_items"]:
                rep["differences"].append(
                    f"skim target items: classic={c['skim_target_items']} fast={f['skim_target_items']}")
            # the fast skim may keep FEWER ids (classic over-collects ids cited
            # before a non-target code streams); it must never need MORE
            for side in (c, f):
                ids = side.pop("skim_ref_ids")
                side["skim_ref_ids"] = len(ids) if ids is not None else None
        a, b = (str(outs[m]).replace("'", "''") for m in ("classic", "fast"))
        try:
            only_c, only_f = _compare_row_sets(duckdb, tmp, a, b)
        except duckdb.Error as e:
            rep["differences"].append(f"could not compare the row sets: {e}")
            rep["identical"] = False
            return rep
        rep["rows_only_in_classic"], rep["rows_only_in_fast"] = only_c, only_f
        if only_c or only_f:
            rep["differences"].append(
                f"row sets differ: {only_c} rows only from classic, {only_f} only from fast")
    rep["identical"] = not rep["differences"]
    return rep


def format_report(rep: dict, size_gb: float | None = None) -> str:
    lines = [f"{rep['file']}  ({'two-pass' if rep['two_pass'] else 'single-pass'})"]
    for mode in ("classic", "fast"):
        r = rep.get(mode, {})
        if "error" in r:
            lines.append(f"  {mode:7}: FAILED — {r['error']}")
            continue
        total = r["skim_s"] + r["extract_s"]
        rate = f", {total / size_gb:.1f} s/GB uncompressed" if size_gb else ""
        lines.append(f"  {mode:7}: {r['rows']:,} rows in {total:.1f}s "
                     f"(skim {r['skim_s']:.1f}s + extract {r['extract_s']:.1f}s{rate})")
    c, f = rep.get("classic", {}), rep.get("fast", {})
    if "extract_s" in c and "extract_s" in f:
        tc, tf = c["skim_s"] + c["extract_s"], f["skim_s"] + f["extract_s"]
        if tf > 0:
            lines.append(f"  speedup: {tc / tf:.1f}x")
    if rep["identical"]:
        lines.append("  RESULT: IDENTICAL — same rows, same QA counters, same header")
    else:
        lines.append("  RESULT: DIFFERENT")
        lines.extend(f"    - {d}" for d in rep["differences"])
    return "\n".join(lines)
