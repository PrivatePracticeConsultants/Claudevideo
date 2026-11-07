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

from .benchmark import BenchmarkError, contract_gaps, resolve_subject_tins
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
    with store.write_lock, store.connect() as con:
        _ensure_table(con)
        con.execute("INSERT OR REPLACE INTO client_watchlist VALUES (?, ?)",
                    [subject, dt.datetime.now(dt.timezone.utc)])
    return watchlist(store)


def remove_client(store: Store, subject: str) -> list[str]:
    with store.write_lock, store.connect() as con:
        _ensure_table(con)
        con.execute("DELETE FROM client_watchlist WHERE subject = ?", [subject])
    return watchlist(store)


def client_digest(store: Store) -> dict:
    """One row per saved client. FAULT-ISOLATED per client and per section: a
    client whose name no longer resolves gets an error cell, never a dead
    digest; a store with only one month simply reports changes as unavailable
    rather than fabricating a comparison."""
    from .medicare import medicare_status, org_referrals, recent_losses

    from .monitor import LATEST_PER_PAYER, payer_months

    subjects = watchlist(store)
    # rate moves compare EACH PAYER's newest file with that payer's previous
    # one: the store's newest month is a month only some payers published in
    # (round 6: one payer's 76 rows dated a month ahead hid every other
    # payer's moves from every client)
    pm = payer_months(store)
    have_medicare = bool(medicare_status(store).get("referrals"))
    can_diff = any(prev for _, _, prev in pm)

    rows = []
    for subject in subjects:
        row: dict = {"subject": subject, "error": None}
        try:
            tins = resolve_subject_tins(store, subject)
            with store.connect() as con:
                npis = [r[0] for r in con.execute(
                    "SELECT DISTINCT npi FROM rates WHERE tin_value IN "
                    "(SELECT unnest(?::VARCHAR[])) AND npi IS NOT NULL",
                    [tins]).fetchall()]
                display = subject_display_name(con, tins)
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

            # contract gaps — same function as the Benchmark tab's button
            try:
                g = contract_gaps(store, subject, {"month": "latest",
                                                   "therapy_only": True})
                gaps = g.get("gaps") or []
                row["gaps"] = {"n": len(gaps),
                               "top": [x.get("billing_code") for x in gaps[:3]]}
            except BenchmarkError as e:
                row["gaps"] = {"error": str(e)}

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


def subject_display_name(con, tins: list[str]) -> str | None:
    """One name for a (possibly multi-TIN) practice, the same on every call:
    the name of its TIN with the most NPIs, ties to the lowest tax ID.
    `any_value` here gave a different member's name from call to call."""
    if not tins:
        return None
    return (con.execute(
        "SELECT first(display_name ORDER BY npi_count DESC NULLS LAST, tin_value) "
        "FROM tin_directory WHERE display_name IS NOT NULL AND tin_value IN "
        "(SELECT unnest(?::VARCHAR[]))", [tins]).fetchone() or [None])[0]
