"""SQL building blocks shared by the report modules.

"The ZIP a practice sits at" = the ZIP its NPIs' rate rows most often carry
(mode), so a multi-site group lands where its bulk is. DuckDB's mode() breaks a
tie by thread arrival — a practice spread evenly over four ZIPs got a
different home (and a different 15-mile market) on different calls. The ties
now go to the LOWEST ZIP: the same answer as `mode(zip ORDER BY zip)`, but
counted per (TIN, ZIP) and then picked, which costs what plain mode() did —
the ordered mode sorted every practice's raw rows (6,184 Alabama practices on
the real store: 0.70 s plain, 1.57 s ordered, 0.70 s here).
"""

from __future__ import annotations

# NPPES ZIP+4 / short ZIPs -> the 5-digit ZIP the Census centroids use
ZIP5 = "lpad(substr(trim(n.zip), 1, 5), 5, '0')"
_HAS_ZIP = "n.zip IS NOT NULL AND trim(n.zip) <> ''"
# among (zip, row count) pairs: the most rows, ties -> the lowest ZIP
_PICK = "arg_min(zip, struct_pack(c := -c, z := zip))"


def zip_counts_by_tin(tins_sql: str, extra: str = "") -> str:
    """(tin_value, zip, c[, extra...]) — rate rows per practice per ZIP for the
    TINs `tins_sql` selects (a subquery or `SELECT unnest(?::VARCHAR[])`).
    `extra` adds aggregates (", count(DISTINCT n.npi) AS nn"); an NPI has ONE
    ZIP, so a per-NPI count summed over a practice's ZIPs is its total."""
    return f"""
        SELECT r.tin_value, {ZIP5} AS zip, count(*) AS c{extra}
        FROM rates r JOIN npi_directory n ON n.npi = r.npi
        WHERE r.tin_value IN ({tins_sql}) AND {_HAS_ZIP}
        GROUP BY 1, 2"""


def modal_zip_by_tin(tins_sql: str) -> str:
    """(tin_value, zip): each practice's ZIP."""
    return (f"SELECT tin_value, {_PICK} AS zip FROM ({zip_counts_by_tin(tins_sql)}) "
            "GROUP BY tin_value")


def modal_zip_of(tins_sql: str) -> str:
    """One row, one column: the ZIP of a practice made of the given TINs
    (their rows pooled, as mode() over them all did)."""
    return (f"SELECT {_PICK} FROM (SELECT zip, sum(c) AS c FROM "
            f"({zip_counts_by_tin(tins_sql)}) GROUP BY zip)")


def pick_modal_zip() -> str:
    """The pick itself, for a caller that groups its own (zip, c) rows."""
    return _PICK
