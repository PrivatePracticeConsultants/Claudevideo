"""Market overview (dashboard landing) — orient a (consulting) user at a glance.

Answers, from the spine already in the store (no new ingestion): how much data
is here, what does therapy reimbursement look like by discipline, which codes are
best covered, and — the consulting question — which PAYERS pay above vs below the
market. The payer index is MPFS-free and fair across payers: for each payer it is
the median over codes of (that payer's median rate for the code) ÷ (the market's
median rate for the code), so 1.10 means "pays ~10% above market", 0.90 "~10%
below". A payer that only prices a couple of codes is excluded (min_codes) so a
thin sample can't top the board.

Basis: the SAME house basis as the Benchmark/Markets/Rate-card tabs — dollar,
real (> $0.01), base-modifier (no CQ/CO), professional class, newest file per
contract ("latest") — computed on the per-TIN grain (each practice collapsed to
its own median first). So a code's landing-page median agrees with the same
code on the Markets/Benchmark tabs instead of drifting on a looser basis.
"""

from __future__ import annotations

from .benchmark import (BASIS_NOTE, _market_where, _rates_relation,
                        normalize_market)
from .catalog import code_info
from .store import Store

# a payer must price at least this many distinct codes to appear in the index —
# otherwise one lucky code makes it look like the best/worst payer in the market
_MIN_INDEX_CODES = 3


def market_overview(store: Store, state: str | None = None) -> dict:
    st = (state or "").strip().upper() or None
    # run on the report basis so the landing numbers match every analytical tab
    market = normalize_market({"month": "latest", **({"state": st} if st else {})})
    where, params = _market_where(market, include_assistant=False, include_non_dollar=False)
    rel = _rates_relation(market)
    j = "LEFT JOIN tin_directory td USING (tin_value)"

    with store.connect() as con:
        counts = con.execute(
            f"""SELECT count(DISTINCT t.payer)        AS payers,
                       count(DISTINCT t.billing_code) AS codes,
                       count(DISTINCT t.tin_value)    AS practices
                FROM {rel} t {j} WHERE {where}""", params).fetchone()

        # per-TIN median first (the house grain), then median across TINs.
        # lower(): the parser stores discipline as "PT"/"OT"/"SLP" (catalog.py)
        # — a literal lowercase IN-list matched nothing on real data and this
        # whole landing-page section silently never rendered.
        by_discipline = con.execute(
            f"""WITH per_tin AS (
                    SELECT lower(t.discipline) AS discipline, t.tin_value,
                           median(t.negotiated_rate) AS rate
                    FROM {rel} t {j}
                    WHERE {where} AND lower(t.discipline) IN ('pt','ot','slp')
                    GROUP BY lower(t.discipline), t.tin_value)
                SELECT discipline, median(rate) AS median,
                       count(DISTINCT tin_value) AS practices
                FROM per_tin GROUP BY discipline ORDER BY discipline""", params).fetchall()

        top_codes = con.execute(
            f"""WITH per_tin AS (
                    SELECT t.billing_code, t.tin_value, median(t.negotiated_rate) AS rate
                    FROM {rel} t {j} WHERE {where}
                    GROUP BY t.billing_code, t.tin_value)
                SELECT billing_code, count(DISTINCT tin_value) AS practices,
                       median(rate) AS median
                FROM per_tin GROUP BY billing_code
                ORDER BY practices DESC, billing_code LIMIT 15""", params).fetchall()

        # payer index: payer_median / market_median per code, then median over
        # codes — all on the per-TIN grain. `per_tin` collapses each practice to
        # one rate first; `pcnt` counts DISTINCT practices per payer (summing the
        # per-code counts would double-count a practice priced on >1 code).
        payer_index = con.execute(
            f"""WITH per_tin AS (
                    SELECT t.payer, t.billing_code, t.tin_value,
                           median(t.negotiated_rate) AS rate
                    FROM {rel} t {j} WHERE {where}
                    GROUP BY t.payer, t.billing_code, t.tin_value
                ),
                base AS (
                    SELECT payer, billing_code, median(rate) AS payer_med
                    FROM per_tin GROUP BY payer, billing_code
                ),
                mkt AS (SELECT billing_code, median(payer_med) AS mkt_med
                        FROM base GROUP BY billing_code),
                pcnt AS (SELECT payer, count(DISTINCT tin_value) AS practices
                         FROM per_tin GROUP BY payer)
                SELECT b.payer,
                       round(median(b.payer_med / m.mkt_med), 3) AS idx,
                       count(DISTINCT b.billing_code)            AS codes,
                       any_value(p.practices)                    AS practices
                FROM base b JOIN mkt m USING (billing_code)
                JOIN pcnt p USING (payer)
                WHERE m.mkt_med > 0
                GROUP BY b.payer
                HAVING count(DISTINCT b.billing_code) >= {_MIN_INDEX_CODES}
                ORDER BY idx DESC""", params).fetchall()

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
        "basis_note": BASIS_NOTE + " Newest file per contract (latest).",
    }
