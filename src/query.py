"""DuckDB convenience queries over the extracted Parquet dataset.

Usage:
    from src.query import Dataset
    ds = Dataset("data/out")
    print(ds.rate_distribution().to_df())

or from the shell:
    .venv/bin/python -m src.query data/out rate-distribution
"""

from __future__ import annotations

import sys
from pathlib import Path

import duckdb


class Dataset:
    def __init__(self, out_dir: str | Path = "data/out"):
        self.con = duckdb.connect()
        glob = str(Path(out_dir) / "**" / "*.parquet")
        self.con.execute(
            f"""
            CREATE OR REPLACE VIEW rates AS
            SELECT * FROM read_parquet('{glob}', hive_partitioning=1)
            """
        )

    def sql(self, query: str) -> duckdb.DuckDBPyRelation:
        return self.con.sql(query)

    def rate_distribution(self) -> duckdb.DuckDBPyRelation:
        """min / p25 / median / p75 / max per CPT per payer."""
        return self.con.sql(
            """
            SELECT payer, billing_code,
                   count(*)                                   AS n,
                   count(DISTINCT npi)                        AS n_npis,
                   round(min(negotiated_rate), 2)             AS min_rate,
                   round(quantile_cont(negotiated_rate, .25), 2) AS p25,
                   round(median(negotiated_rate), 2)          AS median,
                   round(quantile_cont(negotiated_rate, .75), 2) AS p75,
                   round(max(negotiated_rate), 2)             AS max_rate
            FROM rates
            WHERE negotiated_type IN ('negotiated', 'fee schedule')
            GROUP BY payer, billing_code
            ORDER BY billing_code, payer
            """
        )

    def provider_group_rates(self, billing_codes: list[str] | None = None) -> duckdb.DuckDBPyRelation:
        """Per-provider-group rate table for a given CPT set (default: all)."""
        where = ""
        if billing_codes:
            codes = ", ".join(f"'{c}'" for c in billing_codes)
            where = f"AND billing_code IN ({codes})"
        return self.con.sql(
            f"""
            SELECT org_name, npi, payer, billing_code, billing_code_modifier,
                   billing_class,
                   round(median(negotiated_rate), 2) AS median_rate,
                   round(min(negotiated_rate), 2)    AS min_rate,
                   round(max(negotiated_rate), 2)    AS max_rate,
                   count(*)                          AS n
            FROM rates
            WHERE negotiated_type IN ('negotiated', 'fee schedule') {where}
            GROUP BY ALL
            ORDER BY org_name, billing_code, billing_code_modifier
            """
        )

    def payer_comparison(self) -> duckdb.DuckDBPyRelation:
        """Anthem vs Blue KC median rate side-by-side for NPIs present in both."""
        return self.con.sql(
            """
            WITH per AS (
                SELECT npi, any_value(org_name) AS org_name, billing_code, payer,
                       median(negotiated_rate) AS median_rate
                FROM rates
                WHERE negotiated_type IN ('negotiated', 'fee schedule')
                GROUP BY npi, billing_code, payer
            )
            SELECT a.npi, a.org_name, a.billing_code,
                   round(a.median_rate, 2) AS anthem_mo,
                   round(b.median_rate, 2) AS blue_kc,
                   round(a.median_rate - b.median_rate, 2) AS diff
            FROM per a
            JOIN per b USING (npi, billing_code)
            WHERE a.payer = 'anthem_mo' AND b.payer = 'blue_kc'
            ORDER BY abs(a.median_rate - b.median_rate) DESC
            """
        )

    def modifier_breakout(self) -> duckdb.DuckDBPyRelation:
        """Base vs modifier (CQ, GP, ...) rate comparison per CPT per payer."""
        return self.con.sql(
            """
            SELECT payer, billing_code,
                   CASE WHEN billing_code_modifier = '' THEN '(base)'
                        ELSE billing_code_modifier END   AS modifier,
                   count(*)                              AS n,
                   round(median(negotiated_rate), 2)     AS median_rate,
                   round(min(negotiated_rate), 2)        AS min_rate,
                   round(max(negotiated_rate), 2)        AS max_rate
            FROM rates
            WHERE negotiated_type IN ('negotiated', 'fee schedule')
            GROUP BY ALL
            ORDER BY billing_code, payer, modifier
            """
        )


QUERIES = {
    "rate-distribution": lambda ds, _: ds.rate_distribution(),
    "provider-rates": lambda ds, args: ds.provider_group_rates(args or None),
    "payer-comparison": lambda ds, _: ds.payer_comparison(),
    "modifier-breakout": lambda ds, _: ds.modifier_breakout(),
}


def main(argv: list[str]) -> int:
    if len(argv) < 2 or argv[1] not in QUERIES:
        print(f"usage: python -m src.query <out_dir> <{'|'.join(QUERIES)}> [billing codes...]")
        return 2
    ds = Dataset(argv[0])
    QUERIES[argv[1]](ds, argv[2:]).show(max_rows=200)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
