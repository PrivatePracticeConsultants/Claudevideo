"""Independent computation of the house basis straight from the raw `rates`
view (NOT from rates_by_tin / the materialised medians), for accuracy checks.
Rules (CLAUDE.md invariant 5, AI_HANDOFF): newest file_month per contract
(payer x TIN x tin_is_really_npi x code, over ALL of the contract's rows);
professional; dollar; > $0.01; no NPI-in-the-TIN-slot; base modifier sets
('' / GP / GO / GN); each contract variant (modifiers, class, POS set, month)
collapsed to median(DISTINCT rate) over its NPIs, rounded to 4 dp as the
spine does; practice rate per (payer, code) = median over variants; entity
rate = median of member-TIN rates; printed numbers half-up to 2 dp."""
import duckdb

BASE_MODS = "('', 'GP', 'GO', 'GN')"


def open_ro(store_dir: str):
    con = duckdb.connect(f"{store_dir}/mrfx.duckdb", read_only=True)
    return con


def house_ctes(extra_where: str = "TRUE") -> str:
    """CTE text ending in `pm` (payer, billing_code, tin_value, rate) and
    `pr` (billing_code, tin_value, rate). `extra_where` narrows raw rows
    (partition-key columns only: payer / tin_value / billing_code)."""
    return f"""
    raw AS (
        SELECT payer, tin_value, tin_is_really_npi, billing_code, npi,
               coalesce(array_to_string(billing_code_modifier, '|'), '') AS mods,
               billing_class, coalesce(array_to_string(service_code, '|'), '') AS pos,
               file_month, negotiated_rate, is_dollar_rate
        FROM rates WHERE tin_value IS NOT NULL AND {extra_where}
          AND (negotiated_rate IS NULL OR isfinite(negotiated_rate))
    ),
    newest AS (
        SELECT payer, tin_value, tin_is_really_npi, billing_code, max(file_month) AS m
        FROM raw GROUP BY ALL
    ),
    variants AS (
        SELECT r.payer, r.tin_value, r.tin_is_really_npi, r.billing_code, r.mods,
               r.billing_class, r.pos, r.file_month, r.is_dollar_rate,
               round(coalesce(median(DISTINCT r.negotiated_rate)
                                FILTER (NOT r.is_dollar_rate OR r.negotiated_rate > 0.01),
                              median(DISTINCT r.negotiated_rate)), 4) AS rate
        FROM raw r JOIN newest n
          ON n.payer = r.payer AND n.tin_value = r.tin_value
         AND n.tin_is_really_npi = r.tin_is_really_npi
         AND n.billing_code = r.billing_code AND n.m = r.file_month
        GROUP BY ALL
    ),
    house AS (
        SELECT * FROM variants
        WHERE NOT tin_is_really_npi AND is_dollar_rate AND rate > 0.01
          AND billing_class = 'professional' AND mods IN {BASE_MODS}
    ),
    pm AS (
        SELECT payer, billing_code, tin_value, median(rate) AS rate
        FROM house GROUP BY ALL
    ),
    pr AS (
        SELECT billing_code, tin_value, median(rate) AS rate
        FROM house GROUP BY ALL
    )"""


def midrank(v, others):
    """mid-rank % among `others` (subject excluded), rates compared to the cent."""
    from mrfx.rounding import half_up
    v = half_up(v, 2)
    o = [half_up(x, 2) for x in others]
    if not o:
        return None
    below = sum(1 for x in o if x < v)
    tied = sum(1 for x in o if x == v)
    return 100.0 * (below + 0.5 * tied) / len(o)
