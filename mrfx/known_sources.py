"""Known MRF sources: the catalog of payer entry points the app has already
been tested against (config/known_sources.yaml). Queueable entries can be
handed straight to the URL queue; portal entries need a human click and are
listed for reference with instructions."""

from __future__ import annotations

import datetime as dt
import logging
from pathlib import Path

import yaml

log = logging.getLogger(__name__)


def first_of_month(today: dt.date | None = None) -> str:
    d = today or dt.date.today()
    return f"{d.year:04d}-{d.month:02d}-01"


def expand_placeholders(url: str, today: dt.date | None = None) -> str:
    """Monthly-dated index URLs carry a {FIRST_OF_MONTH} placeholder that
    resolves at queue time, so the catalog never goes stale."""
    return url.replace("{FIRST_OF_MONTH}", first_of_month(today))


def load_known_sources(path: Path | str, today: dt.date | None = None) -> list[dict]:
    """Returns the catalog with URLs expanded. Missing/broken file returns []
    (the feature degrades to 'no known sources', never an error)."""
    path = Path(path)  # tolerate a str path from a caller that didn't wrap it
    try:
        raw = yaml.safe_load(path.read_text()) or {}
    except (OSError, yaml.YAMLError) as e:
        log.warning("could not read known sources %s: %s", path, e)
        return []
    if not isinstance(raw, dict):
        log.warning("known sources %s: top level must be a mapping — ignoring", path)
        return []
    out = []
    for s in raw.get("sources", []) or []:
        if not isinstance(s, dict):
            continue  # a stray string/list entry must not 500 the API
        url = str(s.get("url") or "").strip()
        if not url:
            continue
        out.append({
            "name": str(s.get("name") or url),
            "url": expand_placeholders(url, today),
            "kind": str(s.get("kind") or "toc"),
            "queueable": bool(s.get("queueable")),
            "verified": str(s.get("verified") or ""),
            "notes": " ".join(str(s.get("notes") or "").split()),
        })
    return out
