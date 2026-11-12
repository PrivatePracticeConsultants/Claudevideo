"""The "Analytics activity" panel (review 14 item 1.2): watch the newly loaded
files go into the numbers every screen reads.

ONE dict feeds the dashboard panel (`GET /api/analytics/activity`) and
`mrfx status --analytics` (`text()`), built from what the store and the
fold-in scheduler really hold — the files table, the build plan, the running
job, the fold-in event ring in `meta`, the scheduler's decision. Nothing on it
is estimated except the ETA, which is labelled "about" and shown only after
one measured part. Its headline numbers ARE `Store.analytics_coverage()`'s,
so the panel and `/api/stats.analytics` agree at the same moment.
"""
from __future__ import annotations

import datetime as dt

from . import rollplan

WHAT_THIS_MEANS = (
    "Files load in minutes; adding them to the numbers every screen reads takes "
    "longer on a big store. Until a file is added, its rates are stored safely but "
    "not in any report. Nothing needs doing: files are added automatically, a few "
    "at a time while downloads run and right away when they stop. Pause holds the "
    "updates (a laptop on battery); Run now asks for the next one at once.")

ADVICE_MEMORY = ("Raise duckdb_memory_gb in config/mrfx.yaml (or close other programs) and "
                 "restart; it also retries by itself.")
ADVICE_DISK = "Free some disk space on the drive that holds the data folder; it retries by itself."


def _rows_text(n: float | None) -> str:
    if not n:
        return ""
    if n >= 1e6:
        return f"{n / 1e6:.1f} M rows/s"
    if n >= 1e3:
        return f"{n / 1e3:.0f} k rows/s"
    return f"{n:.0f} rows/s"


def _local(ts: str | None) -> str:
    """A stored naive-UTC timestamp as local 'Nov 12 10:42'."""
    if not ts:
        return ""
    try:
        t = dt.datetime.fromisoformat(str(ts)).replace(tzinfo=dt.timezone.utc).astimezone()
        return t.strftime("%b %d %H:%M").replace(" 0", " ")
    except (TypeError, ValueError):
        return str(ts)[:16]


def _ago(seconds: float | None) -> str:
    if seconds is None:
        return ""
    s = int(seconds)
    if s < 90:
        return f"{s} s"
    if s < 5400:
        return f"{round(s / 60)} min"
    return f"{s / 3600:.1f} h"


def _bar(cov: dict, job: dict | None) -> dict | None:
    """The big 0-100 % bar's words, from the running job (None when idle)."""
    if not job:
        return None
    kind = job.get("kind")
    payers = job.get("payers") or []
    files = job.get("files") or []
    if kind == "full":
        head = "Rebuilding the analytics" if cov.get("rebuilding") else "Building the analytics"
    elif kind == "names":
        head = "Rebuilding the practice names and places"
    elif kind == "names_refresh":
        head = "Adding newly identified practice names"
    elif kind == "remove":
        head = "Taking a removed file's numbers out"
    else:
        n = len(files)
        head = (f"Adding {n:,} newly loaded file{'s' if n != 1 else ''}" if n
                else "Adding the newly loaded files")
    bits = [head]
    if payers:
        bits.append(("payer " + payers[0]) if len(payers) == 1 else f"{len(payers)} payers")
    if job.get("slices", 0) > 1:
        lab = job.get("label") or ""
        if kind in ("full", "names"):
            bits.append(f"part {int(job.get('slice', 0)) + 1} of {job['slices']}"
                        + (f" ({lab})" if lab else ""))
        elif lab:
            bits.append(lab)
    elif job.get("label") and kind not in ("full", "names"):
        bits.append(job["label"])
    rate = _rows_text(job.get("rate_rows_s"))
    if rate:
        bits.append(rate)
    eta = rollplan.eta_text(job.get("eta_s")) if job.get("eta_s") else ""
    if eta:
        bits.append(eta)
    return {"pct": int(job.get("pct") or 0), "words": " — ".join(bits), "kind": kind,
            "eta_text": eta, "rate_text": rate, "files": files, "payers": payers,
            "slice": job.get("slice"), "slices": job.get("slices")}


def build(store, recent: int = 50) -> dict:
    """The whole panel. Never raises (a panel that cannot be built says so)."""
    try:
        return _build(store, recent)
    except Exception as e:  # noqa: BLE001 — a screen line never breaks the app
        return {"error": f"the analytics activity is not available right now ({type(e).__name__})",
                "analytics": store.analytics_coverage(), "what_this_means": WHAT_THIS_MEANS}


def _build(store, recent: int) -> dict:
    fold = getattr(store, "foldin", None)
    sched = fold.state() if fold is not None and fold.is_running() else None
    # FRESH, and it refreshes the header's cache: the panel's "waiting"
    # list and its headline numbers come from one moment (a 3-s-old
    # coverage said "every file is in" beside "Waiting: 1 file" — 99M run)
    cov = store.analytics_coverage(max_age_seconds=0)
    job = store.rollup_job()
    with store.connect() as con:
        facts = store._coverage_facts(con)
    plan = facts["plan"]
    p_ts = (plan or {}).get("cover_ts") or ""
    running_files = set((job or {}).get("files") or [])
    sched_why = (sched or {}).get("why") or ""
    if sched is None:
        sched_why = "at the next analytics update"
    from .store import file_key
    waiting = []
    for (fn, ts, rows, payer), c in zip(facts["files"], facts["covered"]):
        if c:
            continue
        if store.analytics_paused() and fn not in running_files:
            why = "analytics paused — press Resume"
        elif fn in running_files:
            why = "being added now"
        elif plan is not None and plan.get("kind") == "full":
            pp = facts["part_payers"].get(f"{file_key(fn)}.parquet") or set()
            if ts and p_ts and ts <= p_ts:
                why = ("in the running build — its payer's part comes later" if
                       store.rollup_in_progress() else "in the part-way build — its payer's part comes later")
                if pp and pp <= facts["plan_done"]:
                    why = "in the build"
            else:
                why = "after the build finishes"
        else:
            why = sched_why or "next in line"
        waiting.append({"file": fn, "payer": payer, "rows": int(rows or 0),
                        "loaded_at": ts, "loaded_text": _local(ts), "why": why})
    waiting.sort(key=lambda r: (r["loaded_at"] or "", r["file"]), reverse=True)
    events = store.analytics_events(recent)
    for e in events:
        e["added_text"] = _local(e.get("added_at"))
        e["loaded_text"] = _local(e.get("loaded_at"))
        e["waited_text"] = _ago(e.get("waited_s"))
    # per payer: rows in the analytics vs loaded (the kept per-part counts of
    # the covered vs all done files), and its state
    last_add: dict = {}
    for e in reversed(store.analytics_events(store.FOLD_EVENTS_KEEP)):
        if e.get("payer"):
            last_add[e["payer"]] = e.get("added_at")
    rows_in: dict = {}
    rows_all: dict = {}
    waiting_payers: set = set()
    for (fn, _ts, _rows, fpayer), c in zip(facts["files"], facts["covered"]):
        part = f"{file_key(fn)}.parquet"
        payers = facts["part_payers"].get(part) or ({fpayer} if fpayer else set())
        for p in payers:
            n = facts["part_rows"].get((part, p), 0)
            rows_all[p] = rows_all.get(p, 0) + n
            if c:
                rows_in[p] = rows_in.get(p, 0) + n
            else:
                waiting_payers.add(p)
    covered = set(cov.get("covered") or [])
    per_payer = []
    for p in cov.get("loaded") or []:
        tot, dn = facts["plan_parts"].get(p, (0, 0))
        if p in facts["part_way"]:
            status, words = "part-way", f"part-way ({dn} of {tot} parts)"
        elif p in covered and p not in waiting_payers:
            status, words = "covered", "in the analytics"
        elif p in covered:
            status, words = "partly", "in the analytics; newer files waiting"
        else:
            status, words = "waiting", "waiting"
        per_payer.append({"payer": p, "status": status, "words": words,
                          "rows_in": int(rows_in.get(p, 0)), "rows_loaded": int(rows_all.get(p, 0)),
                          "last_added": _local(last_add.get(p)) if last_add.get(p) else ""})
    fail = cov.get("failure") if cov.get("state") == "failed" or cov.get("failure") else None
    problems = None
    if fail:
        sent = str(fail.get("sentence") or "")
        problems = {"sentence": sent, "at": _local(fail.get("at")),
                    "retry_at": (sched or {}).get("retry_at") or "",
                    "advice": ADVICE_MEMORY if "memory" in sent.lower()
                    else (ADVICE_DISK if "disk" in sent.lower() or "space" in sent.lower() else "")}
    keep = ("state", "reason", "pct", "eta_text", "covered_files", "done_files",
            "covered_payers", "loaded_payers", "covered_rows", "loaded_rows", "sentence",
            "paused", "rebuilding", "parts_saved", "new_files")
    return {
        "analytics": {k: cov.get(k) for k in keep},
        "bar": _bar(cov, job),
        "waiting": {"count": len(waiting), "rows": int(sum(w["rows"] for w in waiting)),
                    "files": waiting[:500]},
        "added": events,
        "payers": per_payer,
        "problems": problems,
        "scheduler": sched,
        "paused": bool(cov.get("paused")),
        "what_this_means": WHAT_THIS_MEANS,
    }


def text(d: dict) -> str:
    """The same panel as plain text (`mrfx status --analytics`)."""
    a = d.get("analytics") or {}
    out = ["Analytics activity", "=================="]
    if d.get("error"):
        out.append(d["error"])
    head = (f"Analytics cover {int(a.get('covered_payers') or 0):,} of "
            f"{int(a.get('loaded_payers') or 0):,} payers ({int(a.get('covered_files') or 0):,} "
            f"of {int(a.get('done_files') or 0):,} files).")
    out.append(head + (" Current." if a.get("state") == "current" else ""))
    if a.get("reason"):
        out.append(a["reason"])
    if d.get("paused"):
        out.append("PAUSED — nothing is added until you resume (Files tab, or the API).")
    b = d.get("bar")
    if b:
        filled = int(b["pct"] / 5)
        out.append(f"[{'#' * filled}{'.' * (20 - filled)}] {b['pct']} %  {b['words']}")
    s = d.get("scheduler")
    if s and s.get("why") and not b:
        out.append("Next: " + s["why"])
    w = d.get("waiting") or {}
    out.append("")
    out.append(f"Waiting to be added: {int(w.get('count') or 0):,} file(s), "
               f"{int(w.get('rows') or 0):,} rows")
    for r in (w.get("files") or [])[:20]:
        out.append(f"  {r['file'][:60]:<60} {str(r.get('payer') or '')[:28]:<28} "
                   f"{r['rows']:>12,}  loaded {r['loaded_text']:<13} {r['why']}")
    out.append("")
    out.append("Added recently:")
    if not d.get("added"):
        out.append("  (none recorded yet)")
    for e in (d.get("added") or [])[:20]:
        out.append(f"  {e['file'][:60]:<60} added {e.get('added_text', ''):<13} "
                   f"waited {e.get('waited_text', ''):<8} {e.get('kind', '')}")
    out.append("")
    out.append("Per payer:")
    for p in d.get("payers") or []:
        out.append(f"  {p['payer'][:40]:<40} {p['words']:<40} {p['rows_in']:>12,} of "
                   f"{p['rows_loaded']:>12,} rows" + (f"  last added {p['last_added']}"
                                                      if p.get("last_added") else ""))
    pr = d.get("problems")
    if pr:
        out.append("")
        out.append("Problem: " + pr["sentence"] + (f" (at {pr['at']})" if pr.get("at") else ""))
        if pr.get("retry_at"):
            out.append(f"  retries at {pr['retry_at']}")
        if pr.get("advice"):
            out.append("  " + pr["advice"])
    out.append("")
    out.append("What this means: " + d.get("what_this_means", WHAT_THIS_MEANS))
    return "\n".join(out)
