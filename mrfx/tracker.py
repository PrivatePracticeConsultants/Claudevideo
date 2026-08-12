"""Find a Medicare Order & Referring Tracker installation and read what it has
already downloaded.

WHY THIS EXISTS. The two tools own disjoint halves of the same practice — the
tracker knows referrals and eligibility, this app knows contracts — and the
files that join them are already sitting on the user's disk. Making a
non-technical user find those paths, stop the server, type two CLI commands and
restart is friction with no informational content. This module removes it: the
app looks where the tracker actually keeps its data, tells the user what it
found, and imports it in place.

DISCOVERY IS READ-ONLY. Nothing here writes, moves, or deletes anything in the
tracker's folders — that data belongs to the other program, which has its own
retention rules (it keeps the 8 most recent snapshots and self-heals). We only
read.

The layout below is the tracker's, verified against its source:

    <DataDir>/                       ORF_DATA_DIR, else %LOCALAPPDATA%\\OrderReferringTracker,
                                     else ~/.order-referring-tracker
      state.json                     {"ReleaseDate": "YYYY-MM-DD", …}
      snapshots/OrderReferring_<YYYY-MM-DD>.csv
      changes/
      referral-map/                  RM_DATA_DIR, else <DataDir>/referral-map
        dataset-meta.json            {"FileName","Source","Year","Interval","RowCount"}
        pspp_<year>_days<interval>.txt      CMS shared-patient
        hop_teaming_<year>.csv              DocGraph Hop Teaming (CareSet)
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path

from .medicare import FMT_CMS, FMT_HOP, medicare_status

log = logging.getLogger(__name__)

SNAPSHOT_RE = re.compile(r"^OrderReferring_(\d{4}-\d{2}-\d{2})\.csv$", re.IGNORECASE)
PSPP_RE = re.compile(r"^pspp_(\d{4})_days(\d{1,3})\.txt$", re.IGNORECASE)
HOP_RE = re.compile(r"^hop_teaming_(\d{4})\.csv$", re.IGNORECASE)


def candidate_data_dirs(configured: str | Path | None = None) -> list[Path]:
    """Every place the tracker's data directory could be, best first. Mirrors
    the tracker's own resolution order so a user who set ORF_DATA_DIR (or
    pointed our config at it) is found first, and the default install after."""
    out: list[Path] = []

    def add(p) -> None:
        if not p:
            return
        q = Path(os.path.expandvars(str(p))).expanduser()
        if q not in out:
            out.append(q)

    add(configured)
    add(os.environ.get("ORF_DATA_DIR"))
    if os.environ.get("LOCALAPPDATA"):
        add(Path(os.environ["LOCALAPPDATA"]) / "OrderReferringTracker")
    add(Path.home() / ".order-referring-tracker")
    # Windows users often run both tools from the same drive; a portable copy
    # of the tracker keeps its data beside itself rather than in LOCALAPPDATA.
    add(Path.home() / "AppData" / "Local" / "OrderReferringTracker")
    return out


def _looks_like_tracker_dir(d: Path) -> bool:
    """A directory is the tracker's when it carries the tracker's own
    artifacts — not merely because it exists. Guards against pointing the app
    at an empty or unrelated folder and calling it 'connected'."""
    try:
        return d.is_dir() and (
            (d / "state.json").is_file()
            or (d / "snapshots").is_dir()
            or (d / "referral-map").is_dir())
    except OSError:
        return False


def find_data_dir(configured: str | Path | None = None) -> Path | None:
    for d in candidate_data_dirs(configured):
        if _looks_like_tracker_dir(d):
            return d
    return None


def _referral_map_dir(data_dir: Path) -> Path:
    env = os.environ.get("RM_DATA_DIR")
    return Path(os.path.expandvars(env)).expanduser() if env else data_dir / "referral-map"


def _size_mb(p: Path) -> float:
    try:
        return round(p.stat().st_size / 1e6, 1)
    except OSError:
        return 0.0


def _read_json(p: Path) -> dict | None:
    """The tracker writes these atomically, but a half-written or hand-edited
    file must never break discovery — an unreadable meta just means we fall
    back to reading the directory, exactly as the tracker itself does."""
    try:
        return json.loads(p.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None


def _prop(d: dict | None, name: str):
    """PowerShell's ConvertTo-Json preserves the case its hashtable used;
    match case-insensitively rather than betting on it."""
    if not d:
        return None
    for k, v in d.items():
        if k.lower() == name.lower():
            return v
    return None


def list_snapshots(data_dir: Path) -> list[dict]:
    """Roster snapshots the tracker has downloaded, newest first."""
    snaps = []
    for p in sorted((data_dir / "snapshots").glob("OrderReferring_*.csv")):
        m = SNAPSHOT_RE.match(p.name)
        if m:
            snaps.append({"release": m.group(1), "path": str(p), "size_mb": _size_mb(p)})
    snaps.sort(key=lambda s: s["release"], reverse=True)
    return snaps


def list_referral_datasets(data_dir: Path) -> list[dict]:
    """Shared-patient datasets on disk, newest year first.

    Reads dataset-meta.json when present (it names the tracker's ACTIVE
    dataset) and also scans the directory, because the tracker itself recovers
    from a missing meta that way — a user who copied the folder to a new
    machine still has usable files."""
    rm = _referral_map_dir(data_dir)
    found: dict[str, dict] = {}
    if not rm.is_dir():
        return []

    def record(path: Path, fmt: str, year: str, interval: str | None,
               rows: int | None = None, active: bool = False) -> None:
        ds_id = f"{fmt}_{year}" + (f"_{interval}d" if interval else "")
        label = ("CMS shared-patient" if fmt == FMT_CMS else "DocGraph Hop Teaming")
        if interval:
            label += f" {interval}-day"
        prev = found.get(ds_id)
        found[ds_id] = {
            "dataset_id": ds_id, "format": fmt, "year": year, "interval": interval,
            "label": label, "path": str(path), "size_mb": _size_mb(path),
            "rows": rows if rows is not None else (prev or {}).get("rows"),
            "tracker_active": active or bool((prev or {}).get("tracker_active")),
        }

    meta = _read_json(rm / "dataset-meta.json")
    fname = _prop(meta, "FileName")
    if fname:
        mp = rm / str(fname)
        if mp.is_file():
            src = str(_prop(meta, "Source") or "").lower()
            fmt = FMT_HOP if src == "hop-teaming" else FMT_CMS
            yr = str(_prop(meta, "Year") or "").strip()
            iv = _prop(meta, "Interval")
            iv = str(iv).strip() if iv not in (None, "", 0) else None
            rows = _prop(meta, "RowCount")
            if re.fullmatch(r"\d{4}", yr):
                record(mp, fmt, yr, iv if fmt == FMT_CMS else None,
                       int(rows) if isinstance(rows, (int, float)) else None, active=True)

    for p in sorted(rm.iterdir() if rm.is_dir() else []):
        if not p.is_file():
            continue
        m = PSPP_RE.match(p.name)
        if m:
            record(p, FMT_CMS, m.group(1), m.group(2))
            continue
        m = HOP_RE.match(p.name)
        if m:
            record(p, FMT_HOP, m.group(1), None)

    out = list(found.values())
    out.sort(key=lambda d: (d["year"], d["dataset_id"]), reverse=True)
    return out


def discover(store, configured: str | Path | None = None) -> dict:
    """Everything the dashboard's 'connect the tracker' card needs: where the
    tracker is, what it has downloaded, what this app has already imported, and
    therefore what is worth importing now."""
    data_dir = find_data_dir(configured)
    searched = [str(p) for p in candidate_data_dirs(configured)]
    status = medicare_status(store)
    loaded_release = (status.get("eligibility") or {}).get("release")
    loaded_ds = {d.get("dataset_id") for d in status.get("referrals") or []}

    out = {
        "found": data_dir is not None,
        "data_dir": str(data_dir) if data_dir else None,
        "searched": searched,
        "snapshots": [],
        "datasets": [],
        "pending": [],
        "status": status,
    }
    if not data_dir:
        return out

    snaps = list_snapshots(data_dir)
    for s in snaps:
        s["imported"] = s["release"] == loaded_release
    # Only the NEWEST roster is offered: this table is a snapshot of who may
    # order/refer TODAY, and importing an older one would move eligibility
    # backwards. Older files stay listed (the tracker keeps 8) but are not
    # pending work.
    out["snapshots"] = snaps
    if snaps and not snaps[0]["imported"]:
        newer = loaded_release is None or snaps[0]["release"] > loaded_release
        out["pending"].append({
            "kind": "eligibility", "path": snaps[0]["path"],
            "label": f"Order & Referring roster, release {snaps[0]['release']}",
            "size_mb": snaps[0]["size_mb"],
            "why": ("not imported yet" if loaded_release is None else
                    f"newer than the loaded {loaded_release}" if newer else
                    f"differs from the loaded {loaded_release}"),
        })

    datasets = list_referral_datasets(data_dir)
    for d in datasets:
        d["imported"] = d["dataset_id"] in loaded_ds
        if not d["imported"]:
            out["pending"].append({
                "kind": "referrals", "path": d["path"], "label": d["label"] + f" {d['year']}",
                "size_mb": d["size_mb"], "dataset_id": d["dataset_id"],
                "year": d["year"], "interval": d["interval"],
                "non_commercial": d["format"] == FMT_HOP,
                "why": "not imported yet",
            })
    out["datasets"] = datasets
    return out
