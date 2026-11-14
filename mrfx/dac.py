"""The CMS Doctors and Clinicians National Downloadable File (0.8.5).

Names the group each Medicare-enrolled clinician practises with ("Facility
Name"; org_nm in older releases) — mrfx/naming.py rule 2 names a tax ID with
no organization NPI by the group most of its clinicians are enrolled with.

`fetch_dac` keeps ONE copy in cms_dac_dir with a sidecar naming its release
(the dataset's "modified" date on data.cms.gov) and downloads again only when
CMS publishes a newer one; offline, the kept copy is used. `ensure_dac` loads
it into the store when the store holds another release. Never fabricates: no
file, no rule-2 names (the next rule applies)."""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)

DATASET = "mj5m-pzi6"
METASTORE = ("https://data.cms.gov/provider-data/api/1/metastore/schemas/dataset/"
             f"items/{DATASET}")
FILENAME = "DAC_NationalDownloadableFile.csv"
# data.cms.gov answers 403 to a User-Agent containing "Explorer" (and to a
# bare "Mozilla/5.0") — measured 2026-11-14; "mrfx/<version>" is accepted
from . import __version__ as _v
UA = f"mrfx/{_v}"


def _client():
    import httpx
    return httpx.Client(follow_redirects=True, headers={"User-Agent": UA},
                        timeout=httpx.Timeout(60.0, read=300.0))


def latest_release() -> tuple[str, str]:
    """(download URL, release date) of the current file, from data.cms.gov."""
    with _client() as c:
        r = c.get(METASTORE)
        r.raise_for_status()
        meta = r.json()
    url = next((d.get("downloadURL") for d in meta.get("distribution", [])
                if str(d.get("downloadURL", "")).lower().endswith(".csv")), None)
    if not url:
        raise RuntimeError("data.cms.gov lists no CSV for the Doctors and Clinicians file")
    return url, str(meta.get("modified") or "")


def kept(cache_dir: Path) -> dict | None:
    """The kept copy: {path, release, url, fetched_at}, or None."""
    side = Path(cache_dir) / (FILENAME + ".json")
    f = Path(cache_dir) / FILENAME
    if not (side.exists() and f.exists()):
        return None
    try:
        info = json.loads(side.read_text())
    except ValueError:
        return None
    return {**info, "path": str(f)}


def fetch_dac(cache_dir: Path, *, force: bool = False) -> dict:
    """The current file in `cache_dir`, downloading it when CMS has a newer
    release than the kept copy (or `force`). Offline: the kept copy."""
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    have = kept(cache_dir)
    try:
        url, release = latest_release()
    except Exception as e:  # noqa: BLE001 — offline: what is kept
        if have:
            log.info("CMS Doctors and Clinicians file: could not check for a newer "
                     "release (%s) — using the kept %s copy", e, have.get("release") or "?")
            return {**have, "downloaded": False}
        raise
    if have and have.get("release") == release and not force:
        return {**have, "downloaded": False}
    part = cache_dir / (FILENAME + f".{os.getpid()}.part")
    log.info("CMS Doctors and Clinicians file: downloading the %s release (~900 MB)…", release)
    try:
        with _client() as c, c.stream("GET", url) as r:
            r.raise_for_status()
            with open(part, "wb") as fh:
                for chunk in r.iter_bytes(1 << 20):
                    fh.write(chunk)
        os.replace(part, cache_dir / FILENAME)
    finally:
        part.unlink(missing_ok=True)
    info = {"release": release, "url": url.split("?")[0],
            "fetched_at": dt.datetime.now().isoformat(timespec="seconds")}
    (cache_dir / (FILENAME + ".json")).write_text(json.dumps(info, indent=1))
    return {**info, "path": str(cache_dir / FILENAME), "downloaded": True}


def ensure_dac(store, cache_dir: Path, *, force: bool = False, path: str | None = None,
               release: str | None = None) -> dict:
    """Load the file into `store` when it holds another release. `path`: a
    file the user downloaded (its `release` given or unknown). Returns
    {release, rows, loaded, path}."""
    if path:
        info = {"path": str(path), "release": release or "", "url": str(path)}
    else:
        info = fetch_dac(cache_dir, force=force)
    with store.connect() as con:
        have = store._meta_get(con, "dac_release")
        rows = con.execute("SELECT count(*) FROM dac_orgs").fetchone()[0]
    if not force and not path and rows and have == info.get("release"):
        return {"release": have, "rows": rows, "loaded": False, "path": info["path"]}
    n = store.load_dac(info["path"], release=info.get("release"), source=info.get("url"))
    log.info("CMS Doctors and Clinicians file (%s release): %s clinician-group rows loaded",
             info.get("release") or "unknown", f"{n:,}")
    return {"release": info.get("release"), "rows": n, "loaded": True, "path": info["path"]}
