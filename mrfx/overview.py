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

from .benchmark import BASIS_NOTE, normalize_market
from .catalog import code_info
from .store import Store
from .rounding import half_up

# a payer must price at least this many distinct codes to appear in the index —
# otherwise one lucky code makes it look like the best/worst payer in the market
_MIN_INDEX_CODES = 3
# a payer's code enters its index only when it prices this many practices
# for it: one practice is that practice's contract, not the payer's level
_MIN_INDEX_PRACTICES = 3


def market_overview(store: Store, state: str | None = None) -> dict:
    st = (state or "").strip().upper() or None
    # run on the report basis so the landing numbers match every analytical tab
    market = normalize_market({"month": "latest", **({"state": st} if st else {})})
    from . import progress
    from .benchmark import practice_base_sql
    prog = progress.current()
    # review 13 item 3.4: every section is a per-practice median, and the
    # materialised practice medians (practice_medians_v1 / _payer_v1, built once
    # per spine generation for the Benchmark) already hold them — the same
    # rows to the last bit (test_round7's both-ways test). The overview built
    # a temp table of the market's spine rows and recomputed them three times:
    # 26.9 s on a fresh start of the 99M-rate store.
    prog.stage("Reading this market's rows")
    pq, pparams = practice_base_sql(store, market, per_payer=True)    # payer, code, TIN, rate
    cq, cparams = practice_base_sql(store, market)                    # code, TIN, rate
    with store.connect() as con:
        prog.stage("Counting payers, codes and practices")
        counts = con.execute(
            f"""SELECT count(DISTINCT payer), count(DISTINCT billing_code),
                       count(DISTINCT tin_value) FROM ({pq})""", pparams).fetchone()

        # ONE comparable service per discipline — its moderate-complexity
        # evaluation — per-TIN median first, then across TINs. A median over
        # each discipline's own code mix read "OT $95.45 vs PT $32.23": the OT
        # rows were only its four evaluation codes, the 42 shared codes (97110,
        # 97530 …) were in neither (review 10 item 3.5)
        prog.stage("Typical evaluation rate per discipline")
        by_discipline = con.execute(
            f"""SELECT CASE billing_code WHEN '97162' THEN 'pt' WHEN '97166' THEN 'ot'
                            ELSE 'slp' END AS discipline,
                       median(rate) AS median, count(DISTINCT tin_value) AS practices
                FROM ({cq}) WHERE billing_code IN ('97162', '97166', '92523')
                GROUP BY 1 ORDER BY 1""", cparams).fetchall()

        prog.stage("Best-covered codes")
        top_codes = con.execute(
            f"""SELECT billing_code, count(DISTINCT tin_value) AS practices,
                       median(rate) AS median
                FROM ({cq}) GROUP BY billing_code
                ORDER BY practices DESC, billing_code LIMIT 15""", cparams).fetchall()

        # payer index: payer_median / market_median per code, then median over
        # codes — all on the per-TIN grain. The denominator is the code's
        # MARKET median — one rate per practice across all its payers, then
        # the median across practices: the same number this page prints under
        # "Most-priced codes" (round 7 P1.15). A payer's code counts only with
        # _MIN_INDEX_PRACTICES practices; `pcnt` counts DISTINCT practices.
        prog.stage("Payer index")
        payer_index = con.execute(
            f"""WITH per_tin AS ({pq}),
                base AS (
                    SELECT payer, billing_code, median(rate) AS payer_med
                    FROM per_tin GROUP BY payer, billing_code
                    HAVING count(DISTINCT tin_value) >= {_MIN_INDEX_PRACTICES}
                ),
                mkt AS (SELECT billing_code, median(rate) AS mkt_med
                        FROM ({cq}) GROUP BY billing_code),
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
                ORDER BY idx DESC, b.payer""", [*pparams, *cparams]).fetchall()

    disc_label = {"pt": "Physical therapy evaluation (97162)",
                  "ot": "Occupational therapy evaluation (97166)",
                  "slp": "Speech-language evaluation (92523)"}
    return {
        "state": st,
        "payers": counts[0], "codes": counts[1], "practices": counts[2],
        # the states IN SCOPE: under a state filter the card said "69" too
        "states": 1 if st else len(store.available_states()),
        "by_discipline": [
            {"discipline": d, "label": disc_label.get(d, d),
             "median": half_up(m, 2) if m is not None else None, "practices": n}
            for d, m, n in by_discipline],
        "top_codes": [
            {"billing_code": c, "description": code_info(c)[0],
             "practices": n, "median": half_up(m, 2) if m is not None else None}
            for c, n, m in top_codes],
        "payer_index": [
            {"payer": p, "index": idx, "codes": n_codes, "practices": int(n_prac)}
            for p, idx, n_codes, n_prac in payer_index],
        "basis_note": BASIS_NOTE + " Newest file per contract (latest).",
    }
