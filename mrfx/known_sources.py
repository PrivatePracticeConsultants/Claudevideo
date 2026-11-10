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
        # errors="replace": a non-UTF8 byte from a Windows editor degrades to a
        # replacement char instead of raising UnicodeDecodeError (a 500 on the
        # known-sources API and a traceback from `mrfx add --known`).
        raw = yaml.safe_load(path.read_text(encoding="utf-8", errors="replace")) or {}
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
            "url_template": url,
            # two-letter states the source covers; "US" = national (it
            # matches every state pick)
            "states": [str(x).strip().upper() for x in (s.get("states") or [])
                       if str(x).strip()],
            "kind": str(s.get("kind") or "toc"),
            "queueable": bool(s.get("queueable")),
            "verified": str(s.get("verified") or ""),
            "notes": " ".join(str(s.get("notes") or "").split()),
        })
    return out


def for_states(sources: list[dict], states) -> list[dict]:
    """The sources relevant to `states` (two-letter codes): those that cover
    any of them, plus national ones (US). No states -> all sources."""
    want = {str(s).strip().upper() for s in (states or []) if str(s).strip()}
    if not want:
        return sources
    return [s for s in sources if "US" in s.get("states", []) or want & set(s.get("states", []))]


def _name_tokens(name: str) -> set[str]:
    import re
    t = (name or "").lower().replace("bcbs", "blue cross blue shield")
    t = t.split("(")[0]           # "The Health Plan (West Virginia / …)"
    drop = {"the", "of", "and", "inc", "health", "plan", "plans", "insurance", "co"}
    return {w for w in re.findall(r"[a-z0-9]+", t) if w not in drop}


def _match_score(a: str, b: str) -> float:
    """Jaccard of the name tokens; 0 unless one name's tokens contain the
    other's or they overlap by ≥ 0.6. Greedy best-first pairing (below) keeps
    "BCBS of Kansas" off "BCBS of Kansas City" when both are listed."""
    ta, tb = _name_tokens(a), _name_tokens(b)
    if not ta or not tb:
        return 0.0
    j = len(ta & tb) / len(ta | tb)
    return j if (ta <= tb or tb <= ta or j >= 0.6) else 0.0


def merge_registry_cards(cards: list[dict], known: list[dict], state: str) -> list[dict]:
    """The registry's cards for `state` ("US" = the national list) with the
    tested-sources catalog folded in: a card the catalog covers is marked
    `tested` (its verified date, link, and whether Files can queue it); a
    tested source with no card becomes one. Order: the registry's cards in
    their order, then the tested-only cards by name (deterministic)."""
    st = (state or "").strip().upper()
    if st == "US":
        mine = [k for k in known if "US" in k.get("states", [])]
    else:
        mine = [k for k in known if st in k.get("states", []) and "US" not in k.get("states", [])]
    pairs = []
    for ci, c in enumerate(cards):
        for ki, k in enumerate(mine):
            sc = 2.0 if c.get("mrf_url") and c.get("mrf_url") == k["url"] else \
                _match_score(c.get("name", ""), k["name"])
            if sc > 0:
                pairs.append((-sc, ci, ki))
    match: dict[int, int] = {}
    used: set[int] = set()
    for _, ci, ki in sorted(pairs):
        if ci not in match and ki not in used:
            match[ci] = ki
            used.add(ki)
    out = []
    for ci, c in enumerate(cards):
        c = dict(c)
        if ci in match:
            c["tested"] = _tested(mine[match[ci]])
        out.append(c)
    for i, k in sorted(enumerate(mine), key=lambda x: (x[1]["name"], x[0])):
        if i in used:
            continue
        out.append({"key": f"tested:{st}:{k['name']}", "name": k["name"], "parent": "",
                    "mrf_url": None, "verified": False, "verified_locally": False,
                    "notes": None, "tested": _tested(k), "tested_only": True})
    return out


def _tested(k: dict) -> dict:
    return {"url": k["url"], "verified": k.get("verified") or "", "queueable": k["queueable"],
            "kind": k.get("kind") or "", "notes": k.get("notes") or ""}


def starter_links_text(path: Path | str) -> str:
    """config/starter_links.txt, generated from the catalog: every queueable
    entry, with its {FIRST_OF_MONTH} placeholder kept (resolved at queue
    time). The file drifted from the catalog by hand (38 of 109 queueable
    sources — audit 8 P2-H); tests/mrfx/test_audit8_ui.py pins the two."""
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8", errors="replace")) or {}
    rows = [(str(s.get("name") or s["url"]).strip(), str(s["url"]).strip())
            for s in (raw.get("sources") or [])
            if isinstance(s, dict) and s.get("queueable") and s.get("url")]
    seen: set = set()
    rows = [r for r in rows if not (r[1] in seen or seen.add(r[1]))]
    head = (
        "# MRF Explorer — starter links: every queueable source in the tested-sources\n"
        "# catalog (config/known_sources.yaml), generated from it — do not edit here.\n"
        "#\n"
        "# Load them all with:   mrfx add --known        (or --known --states MO,KS)\n"
        "#                  or:  mrfx add --file config/starter_links.txt\n"
        "# Or paste any subset into the dashboard's 'Paste file links' box. To keep your\n"
        "# own list, COPY this file and edit the copy: an update replaces this one.\n"
        "#\n"
        "# {FIRST_OF_MONTH} resolves to the current month automatically when queued.\n"
        "# Lines starting with # are ignored. Every link below cascades on its own:\n"
        "# indexes expand, portals are crawled, duplicates and drug/allowed-amounts\n"
        "# files are skipped, and files over confirm_over_gb wait for your go-ahead.\n"
        f"\n# ---- {len(rows)} auto-queueable sources ----\n")
    return head + "".join(f"# {n}\n{u}\n\n" for n, u in rows)
