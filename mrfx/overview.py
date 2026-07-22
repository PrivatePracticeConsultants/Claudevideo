"""Market overview (dashboard landing) — orient a (consulting) user at a glance.

Answers, from the spine already in the store (no new ingestion): how much data
is here, what does therapy reimbursement look like by discipline, which codes are
best covered, and — the consulting question — which PAYERS pay above vs below the
market. The payer index is MPFS-free and fair across payers: for each payer it is
the median over codes of (that payer's median rate for the code) ÷ (the market's
median rate for the code), so 1.10 means "pays ~10% above market", 0.90 "~10%
below". A payer that only prices a couple of codes is excluded (min_codes) so a
thin sample can't top the board.

All rates: dollar, real (> $0.01) — the same basis as every other view.
"""

from __future__ import annotations

from .catalog import code_info
from .store import Store

# a payer must price at least this many distinct codes to appear in the index —
# otherwise one lucky code makes it look like the best/worst payer in the market
_MIN_INDEX_CODES = 3


def market_overview(store: Store, state: str | None = None) -> dict:
    st = (state or "").strip().upper() or None
    # state scoping joins the directory (states live there, as an array); an
    # unscoped overview reads the whole book.
    join = "JOIN tin_directory d USING (tin_value)" if st else ""
    where = "r.is_dollar_rate AND r.negotiated_rate > 0.01"
    params: list = []
    if st:
        where += " AND list_contains(d.states, ?)"
        params.append(st)

    with store.connect() as con:
        counts = con.execute(
            f"""SELECT count(DISTINCT r.payer)        AS payers,
                       count(DISTINCT r.billing_code) AS codes,
                       count(DISTINCT r.tin_value)    AS practices
                FROM rates_by_tin r {join} WHERE {where}""", params).fetchone()

        by_discipline = con.execute(
            f"""SELECT r.discipline,
                       median(r.negotiated_rate)    AS median,
                       count(DISTINCT r.tin_value)  AS practices
                FROM rates_by_tin r {join}
                WHERE {where} AND r.discipline IN ('pt','ot','slp')
                GROUP BY r.discipline ORDER BY r.discipline""", params).fetchall()

        top_codes = con.execute(
            f"""SELECT r.billing_code,
                       count(DISTINCT r.tin_value) AS practices,
                       median(r.negotiated_rate)   AS median
                FROM rates_by_tin r {join} WHERE {where}
                GROUP BY r.billing_code
                ORDER BY practices DESC, r.billing_code LIMIT 15""", params).fetchall()

        # payer index: payer_median / market_median per code, then median over
        # codes. `pcnt` counts DISTINCT practices per payer directly — summing
        # the per-code counts would double-count any practice priced on >1 code.
        payer_index = con.execute(
            f"""WITH base AS (
                    SELECT r.payer, r.billing_code,
                           median(r.negotiated_rate)   AS payer_med
                    FROM rates_by_tin r {join} WHERE {where}
                    GROUP BY r.payer, r.billing_code
                ),
                mkt AS (SELECT billing_code, median(payer_med) AS mkt_med
                        FROM base GROUP BY billing_code),
                pcnt AS (SELECT r.payer, count(DISTINCT r.tin_value) AS practices
                         FROM rates_by_tin r {join} WHERE {where}
                         GROUP BY r.payer)
                SELECT b.payer,
                       round(median(b.payer_med / m.mkt_med), 3) AS idx,
                       count(DISTINCT b.billing_code)            AS codes,
                       any_value(p.practices)                    AS practices
                FROM base b JOIN mkt m USING (billing_code)
                JOIN pcnt p USING (payer)
                WHERE m.mkt_med > 0
                GROUP BY b.payer
                HAVING count(DISTINCT b.billing_code) >= {_MIN_INDEX_CODES}
                ORDER BY idx DESC""", params * 2).fetchall()

    disc_label = {"pt": "Physical therapy", "ot": "Occupational therapy",
                  "slp": "Speech-language pathology"}
    return {
        "state": st,
        "payers": counts[0], "codes": counts[1], "practices": counts[2],
        "states": len(store.available_states()),
        "by_discipline": [
            {"discipline": d, "label": disc_label.get(d, d),
             "median": round(m, 2) if m is not None else None, "practices": n}
            for d, m, n in by_discipline],
        "top_codes": [
            {"billing_code": c, "description": code_info(c)[0],
             "practices": n, "median": round(m, 2) if m is not None else None}
            for c, n, m in top_codes],
        "payer_index": [
            {"payer": p, "index": idx, "codes": n_codes, "practices": int(n_prac)}
            for p, idx, n_codes, n_prac in payer_index],
    }
