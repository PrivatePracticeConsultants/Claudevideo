"""Client watchlist + the "what changed for my book" digest.

The consultant's actual workday is a BOOK of client practices, but every tab
answers for one practice at a time. This composes the app's existing answers
across the saved list: which clients had rates move this month, which have
contract gaps worth chasing, and which of their referral sources lost Medicare
order/refer standing in the latest roster — the Monday-morning review in one
screen. Nothing here computes new numbers; every cell reuses the exact
functions its own tab uses, so the digest can never disagree with a drill-down.
"""

from __future__ import annotations

import datetime as dt
import logging

from .benchmark import BenchmarkError, resolve_subject_tins
from .store import Store

log = logging.getLogger(__name__)


def _ensure_table(con) -> None:
    con.execute("""
        CREATE TABLE IF NOT EXISTS client_watchlist (
            subject VARCHAR PRIMARY KEY,   -- entity name / TIN / NPI, as typed
            added_at TIMESTAMP
        )
    """)


def watchlist(store: Store) -> list[str]:
    with store.connect() as con:
        try:
            return [r[0] for r in con.execute(
                "SELECT subject FROM client_watchlist ORDER BY lower(subject)").fetchall()]
        except Exception:  # noqa: BLE001 — table absent = empty list
            return []


def add_client(store: Store, subject: str) -> list[str]:
    subject = str(subject or "").strip()
    if not subject:
        raise BenchmarkError("type a practice (entity name, TIN, or NPI) to add")
    if len(subject) > 200:
        raise BenchmarkError("that doesn't look like a practice name/TIN/NPI")
    # a name that matches no practice was saved and then reported as "no
    # longer matches" — it never did (review 10 group 4). Refuse it now.
    from .benchmark import resolve_subject_tins
    tins = resolve_subject_tins(store, subject)   # falls back to the text itself
    with store.connect() as con:
        known = tins and con.execute(
            "SELECT 1 FROM tin_directory WHERE tin_value IN (SELECT unnest(?::VARCHAR[])) "
            "LIMIT 1", [tins]).fetchone()
    if not known:
        raise BenchmarkError(
            f"no practice in the store matches {subject!r} — check the spelling, "
            "or use its tax ID or an NPI")
    _save_client(store, subject)
    return watchlist(store)


def _save_client(store: Store, subject: str) -> None:
    """Write one watchlist row (no matching check — add_client's caller has
    done it; tests use this to plant a client whose practice later stopped
    matching, e.g. after a store reset)."""
    with store.write_lock, store.connect() as con:
        _ensure_table(con)
        con.execute("INSERT OR REPLACE INTO client_watchlist VALUES (?, ?)",
                    [subject, dt.datetime.now(dt.timezone.utc)])


def remove_client(store: Store, subject: str) -> list[str]:
    with store.write_lock, store.connect() as con:
        _ensure_table(con)
        con.execute("DELETE FROM client_watchlist WHERE subject = ?", [subject])
    return watchlist(store)


DIGEST_GAPS_MARKET = {"month": "latest", "therapy_only": True}


def client_digest(store: Store, gaps_book: dict | None = None) -> dict:
    """One row per saved client. FAULT-ISOLATED per client and per section: a
    client whose name no longer resolves gets an error cell, never a dead
    digest; a store with only one month simply reports changes as unavailable
    rather than fabricating a comparison.

    Cached per (data generation, watchlist, Medicare layers): the Clients tab
    rebuilt it on every visit (23 s for 5 clients on a 99M-rate store) though
    nothing in it changes until data, the list or a Medicare import does.
    `gaps_book` lets the packet build hand in the gaps it already computed."""
    import json

    from .medicare import medicare_status

    subjects = watchlist(store)
    mstatus = medicare_status(store)
    # user_edit_generation: an entity-map / peer-set edit changes which TINs
    # a client is, so its gaps — a packet printed the stale digest beside
    # fresh gaps (audit 8 P2-10)
    key = (store.data_generation, getattr(store, "user_edit_generation", 0), tuple(subjects),
           json.dumps(mstatus, sort_keys=True, default=str))
    held = getattr(store, "_digest_cache", None)
    if held is not None and held[0] == key:
        return held[1]
    result = _build_digest(store, subjects, mstatus, gaps_book)
    store._digest_cache = (key, result)
    return result


def _build_digest(store: Store, subjects: list[str], mstatus: dict,
                  gaps_book: dict | None) -> dict:
    from .benchmark import contract_gaps_book
    from .medicare import org_referrals, recent_losses
    from .monitor import LATEST_PER_PAYER, payer_months

    # rate moves compare EACH PAYER's newest file with that payer's previous
    # one: the store's newest month is a month only some payers published in
    # (round 6: one payer's 76 rows dated a month ahead hid every other
    # payer's moves from every client)
    pm = payer_months(store)
    have_medicare = bool(mstatus.get("referrals"))
    can_diff = any(prev for _, _, prev in pm)
    # contract gaps for the WHOLE book in one market pass (exact — see
    # contract_gaps_book); was the national market aggregation per client
    if gaps_book is None:
        try:
            gaps_book = contract_gaps_book(store, subjects, DIGEST_GAPS_MARKET)
        except BenchmarkError as e:
            gaps_book = {s: e for s in subjects}

    rows = []
    for subject in subjects:
        row: dict = {"subject": subject, "error": None}
        try:
            tins = resolve_subject_tins(store, subject)
            with store.connect() as con:
                npis = [r[0] for r in con.execute(
                    f"SELECT DISTINCT npi FROM {store.pairs_relation()} WHERE tin_value IN "
                    "(SELECT unnest(?::VARCHAR[])) AND npi IS NOT NULL",
                    [tins]).fetchall()]
                from .benchmark import mapped_entity_name
                display = subject_display_name(con, tins, mapped_entity_name(store, subject))
                my_payers = {r[0] for r in con.execute(
                    "SELECT DISTINCT payer FROM rates_by_tin WHERE is_latest AND tin_value IN "
                    "(SELECT unnest(?::VARCHAR[]))", [tins]).fetchall()}
            row["display_name"] = display or subject
            if not npis and not display:
                row["error"] = "no longer matches any practice in the store"
                rows.append(row)
                continue

            # rate changes, each payer's newest file vs its previous one —
            # the Changes tab's default ("latest"), scoped to this client
            row["changes"] = None
            if can_diff:
                try:
                    from .monitor import compute_rate_changes
                    ch = compute_rate_changes(store, {"month": "latest"},
                                              subject=subject)
                    moves = ch.get("changes") or []
                    biggest = max(moves, key=lambda c: abs(c.get("pct_change") or 0),
                                  default=None)
                    compared = [c for c in ch.get("compared") or [] if c["payer"] in my_payers]
                    row["changes"] = {
                        "n": len(moves),
                        "month": "latest", "prev": None,
                        "compared": compared,
                        "span": "; ".join(f"{c['payer']} {c['prev_month']} → {c['new_month']}"
                                          for c in compared),
                        "biggest_pct": biggest.get("pct_change") if biggest else None,
                        "biggest_code": biggest.get("billing_code") if biggest else None,
                        "biggest_payer": biggest.get("payer") if biggest else None,
                        "biggest_span": (f"{biggest['prev_month']} → {biggest['new_month']}"
                                         if biggest else None),
                    }
                except BenchmarkError as e:
                    row["changes"] = {"error": str(e)}

            # contract gaps — the Benchmark tab button's numbers (contract_gaps
            # is defined through the same book function)
            g = gaps_book.get(subject)
            if isinstance(g, BenchmarkError) or g is None:
                row["gaps"] = {"error": str(g or "no contract-gap result")}
            else:
                gaps = g.get("gaps") or []
                row["gaps"] = {"n": len(gaps),
                               "top": [x.get("billing_code") for x in gaps[:3]]}

            # referral sources that lost order/refer standing — the alert
            row["lost_referrers"] = None
            if have_medicare and npis:
                ref = org_referrals(store, npis, "in", limit=500)
                lost = recent_losses(store, [r["npi"] for r in ref["rows"]])
                row["lost_referrers"] = [
                    {"npi": r["npi"], "name": r["name"], "patients": r["patients"],
                     "kind": lost[r["npi"]]}
                    for r in ref["rows"] if r["npi"] in lost]
        except Exception as e:  # noqa: BLE001 — one broken client must never
            # kill the whole digest (fault isolation)
            log.warning("digest failed for %s: %s", subject, e)
            row["error"] = str(e)
        rows.append(row)
    return {"clients": rows, "month": "latest", "basis": LATEST_PER_PAYER,
            "payer_months": [{"payer": p, "new_month": n, "prev_month": o} for p, n, o in pm],
            "can_diff": can_diff, "have_medicare": have_medicare}


def subject_display_name(con, tins: list[str], mapped: str | None = None) -> str | None:
    """One name for a (possibly multi-TIN) practice, the same on every call:
    a mapped entity's own name (`mapped`, from benchmark.mapped_entity_name),
    else the name of its TIN with the most NPIs, ties to the lowest tax ID.
    `any_value` here gave a different member's name from call to call."""
    if mapped:
        return mapped
    if not tins:
        return None
    return (con.execute(
        "SELECT first(display_name ORDER BY npi_count DESC NULLS LAST, tin_value) "
        "FROM tin_directory WHERE display_name IS NOT NULL AND tin_value IN "
        "(SELECT unnest(?::VARCHAR[]))", [tins]).fetchone() or [None])[0]
