"""Discover and parse table-of-contents (TOC) files for both BCBS-MO licensees.

Blue KC (Sapphire hub, https://bcbskc.sapphiremrfhub.com):
  The hub is a Gatsby site; the TOC listing is baked into its static-query
  data. Crawl: /page-data/index/page-data.json -> staticQueryHashes ->
  /page-data/sq/d/<hash>.json -> data.allTocsJson.edges[].node.url. Each url
  is a CMS TiC TOC JSON (`reporting_structure[]`).

Anthem MO (https://www.anthem.com/machine-readable-file/search):
  EIN/employer-group gated; there is no flat state index in the UI. The
  portal's own JS does: GET {region}/status.json to pick the live S3 region,
  name search via {region}/namesearch/<first-letter>.json (-> {name, ein}),
  then GET {region}/anthem/<9-digit-ein>.json which lists the group's
  'In-Network Negotiated Rates Files' (and BCBSA out-of-area rate files).
  We reproduce that flow at runtime; nothing is hardcoded beyond the region
  roots the portal itself uses (configurable in targets.yaml).
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import ijson

from .config import AnthemConfig, BlueKCConfig, Config, EmployerGroup
from .http_util import FetchError, get_with_retry, open_json_stream

log = logging.getLogger(__name__)

SUPPORTED_SCHEMA_MAJOR = 2  # CMS TiC v2.0


@dataclass
class SourceFile:
    """One in-network rate file to extract, with its TOC context."""

    payer: str  # 'anthem_mo' | 'blue_kc'
    url: str
    plan_names: list[str] = field(default_factory=list)
    plan_ids: list[str] = field(default_factory=list)
    description: str = ""

    @property
    def plan_name(self) -> str | None:
        return "; ".join(sorted(set(self.plan_names))[:5]) or None

    @property
    def plan_id(self) -> str | None:
        return "; ".join(sorted(set(self.plan_ids))[:5]) or None


def _dedupe_key(url: str) -> str:
    """Signed CDN URLs for the same file differ only in query params."""
    return url.split("?", 1)[0]


def _cache_path(raw_dir: Path, kind: str, name: str) -> Path:
    today = dt.date.today().isoformat()
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", name)[:150]
    d = raw_dir / "tocs"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{today}_{kind}_{safe}"


def _fetch_cached(client: httpx.Client, cfg: Config, url: str, kind: str, name: str) -> Path:
    """Download a small JSON resource to data/raw/tocs/ (dated), reusing today's copy."""
    path = _cache_path(cfg.paths.raw_dir, kind, name)
    if path.exists() and path.stat().st_size > 0:
        log.info("cache hit: %s", path.name)
        return path
    resp = get_with_retry(client, cfg.http, url)
    path.write_bytes(resp.content)
    log.info("cached %s (%d bytes)", path.name, len(resp.content))
    return path


# ---------------------------------------------------------------------------
# Blue KC
# ---------------------------------------------------------------------------


def discover_blue_kc(client: httpx.Client, cfg: Config) -> list[SourceFile]:
    kc: BlueKCConfig = cfg.payers.blue_kc
    hub = kc.hub_url.rstrip("/")

    page_data = get_with_retry(client, cfg.http, f"{hub}/page-data/index/page-data.json").json()
    hashes = page_data.get("staticQueryHashes", [])
    if not hashes:
        log.error("Blue KC hub: no staticQueryHashes found at %s — site layout may have changed", hub)
        return []

    toc_nodes: dict[str, dict] = {}
    for h in hashes:
        try:
            sq = get_with_retry(client, cfg.http, f"{hub}/page-data/sq/d/{h}.json").json()
        except FetchError as e:
            log.warning("Blue KC static query %s failed: %s", h, e)
            continue
        edges = sq.get("data", {}).get("allTocsJson", {}).get("edges", [])
        for edge in edges:
            node = edge.get("node", {})
            if node.get("url") and not node.get("is_suppressed"):
                toc_nodes.setdefault(node["url"], node)

    if kc.toc_name_filters:
        wanted = [f.lower() for f in kc.toc_name_filters]
        toc_nodes = {
            url: n
            for url, n in toc_nodes.items()
            if any(w in (n.get("payer_name", "") + " " + n.get("file_name", "")).lower() for w in wanted)
        }

    log.info("Blue KC hub: %d TOC files to parse", len(toc_nodes))

    sources: dict[str, SourceFile] = {}
    for url, node in sorted(toc_nodes.items()):
        try:
            path = _fetch_cached(client, cfg, url, "bluekc", node.get("file_name") or url.rsplit("/", 1)[-1])
            _parse_toc_file(path, "blue_kc", sources)
        except (FetchError, ValueError, ijson.JSONERROR) as e:
            log.warning("Blue KC TOC %s failed: %s — skipping", url, e)
    log.info("Blue KC: %d unique in-network files", len(sources))
    return list(sources.values())


def _parse_toc_file(path: Path, payer: str, sources: dict[str, SourceFile]) -> None:
    """Stream-parse a CMS TiC TOC: reporting_structure[] -> in_network_files[].

    Accumulates into `sources`, deduped by URL-sans-query, merging plan context
    (the same underlying file is referenced by many plans).
    """
    with open(path, "rb") as f:
        version = None
        for prefix, event, value in ijson.parse(f):
            if prefix == "version" and event == "string":
                version = value
                break
            if prefix == "reporting_structure" and event == "start_array":
                break
        if version and not str(version).startswith(f"{SUPPORTED_SCHEMA_MAJOR}."):
            log.warning("TOC %s declares schema %s (expected %d.x) — skipping file", path.name, version, SUPPORTED_SCHEMA_MAJOR)
            return

    with open(path, "rb") as f:
        for entry in ijson.items(f, "reporting_structure.item"):
            plans = entry.get("reporting_plans", []) or []
            plan_names = [p.get("plan_name", "") for p in plans if p.get("plan_name")]
            plan_ids = [str(p.get("plan_id", "")) for p in plans if p.get("plan_id")]
            for inf in entry.get("in_network_files", []) or []:
                loc = inf.get("location")
                if not loc:
                    continue
                key = _dedupe_key(loc)
                src = sources.get(key)
                if src is None:
                    src = SourceFile(payer=payer, url=loc, description=inf.get("description", "") or "")
                    sources[key] = src
                src.plan_names.extend(plan_names)
                src.plan_ids.extend(plan_ids)


# ---------------------------------------------------------------------------
# Anthem
# ---------------------------------------------------------------------------

ANTHEM_GUIDANCE = """\
Anthem's MRF portal is EIN / employer-group gated: no employer groups are
configured, so the Anthem branch has nothing to fetch.

Add groups to config/targets.yaml under payers.anthem_mo.employer_groups:

    employer_groups:
      - ein: "43-0653611"                # 9-digit federal EIN, dash optional
      - name: "some employer name"       # resolved via the portal name search

How to find an EIN: see README section "Finding an Anthem employer EIN".
"""


def _anthem_region(client: httpx.Client, cfg: Config) -> str:
    """Pick the live S3 region the same way the portal's script.js does."""
    anthem: AnthemConfig = cfg.payers.anthem_mo
    try:
        resp = client.get(anthem.s3_primary.rstrip("/") + "/status.json")
        if resp.status_code == 200:
            return anthem.s3_primary.rstrip("/") + "/"
    except httpx.HTTPError as e:
        log.warning("Anthem status.json probe failed (%s); falling back to secondary region", e)
    return anthem.s3_secondary.rstrip("/") + "/"


def _resolve_ein_by_name(client: httpx.Client, cfg: Config, s3url: str, name: str) -> list[tuple[str, str]]:
    """Portal name search: namesearch/<first-letter>.json -> [(ein, name)] matches."""
    q = name.strip().lower()
    shard = q[0] if q and q[0].isalpha() else "others"
    url = f"{s3url}namesearch/{shard}.json"
    path = _fetch_cached(client, cfg, url, "anthem_namesearch", f"{shard}.json")
    entries = json.loads(path.read_text()).get("namesearch", [])
    exact = [(e["ein"], e["name"]) for e in entries if e.get("name", "").lower() == q]
    if exact:
        return exact
    return [(e["ein"], e["name"]) for e in entries if q in e.get("name", "").lower()]


def _keep_anthem_file(anthem: AnthemConfig, url: str, displayname: str) -> bool:
    if not anthem.file_include_patterns:
        return True
    hay = (displayname + " " + url.split("?", 1)[0]).lower()
    return any(p.lower() in hay for p in anthem.file_include_patterns)


def discover_anthem(client: httpx.Client, cfg: Config) -> list[SourceFile]:
    anthem: AnthemConfig = cfg.payers.anthem_mo
    if not anthem.employer_groups:
        log.warning(ANTHEM_GUIDANCE)
        print(ANTHEM_GUIDANCE)
        return []

    s3url = _anthem_region(client, cfg)
    log.info("Anthem live region: %s", s3url)

    sources: dict[str, SourceFile] = {}
    for group in anthem.employer_groups:
        try:
            _discover_anthem_group(client, cfg, s3url, group, sources)
        except (FetchError, json.JSONDecodeError, KeyError) as e:
            log.warning("Anthem group %s could not be resolved (%s) — continuing", group.label(), e)
    log.info("Anthem: %d unique in-network files across %d groups", len(sources), len(anthem.employer_groups))
    return list(sources.values())


def _discover_anthem_group(
    client: httpx.Client,
    cfg: Config,
    s3url: str,
    group: EmployerGroup,
    sources: dict[str, SourceFile],
) -> None:
    anthem = cfg.payers.anthem_mo
    resolved: list[tuple[str, str]] = []
    if group.ein:
        resolved = [(group.ein, group.name or group.ein)]
    else:
        matches = _resolve_ein_by_name(client, cfg, s3url, group.name or "")
        if not matches:
            log.warning("Anthem name search found no match for %r — skipping group", group.name)
            return
        if len(matches) > 5:
            log.warning(
                "Anthem name search for %r matched %d employers; taking all — narrow the name or use an EIN",
                group.name,
                len(matches),
            )
        resolved = matches

    for ein, name in resolved:
        url = f"{s3url}anthem/{ein}.json"
        try:
            path = _fetch_cached(client, cfg, url, "anthem_ein", f"{ein}.json")
        except FetchError as e:
            log.warning("Anthem EIN %s (%s): %s — skipping", ein, name, e)
            continue
        data = json.loads(path.read_text())
        buckets = ["In-Network Negotiated Rates Files"]
        if anthem.include_out_of_area_files:
            buckets.append("Blue Cross Blue Shield Association Out-of-Area Rates Files")
        kept = skipped = 0
        for bucket in buckets:
            for entry in data.get(bucket, []) or []:
                loc, display = entry.get("url"), entry.get("displayname", "")
                if not loc:
                    continue
                if not _keep_anthem_file(anthem, loc, display):
                    skipped += 1
                    continue
                kept += 1
                key = _dedupe_key(loc)
                src = sources.get(key)
                if src is None:
                    src = SourceFile(payer="anthem_mo", url=loc, description=display)
                    sources[key] = src
                src.plan_names.append(name)
                src.plan_ids.append(ein)
        log.info(
            "Anthem EIN %s (%s): kept %d files, filtered out %d (file_include_patterns=%s)",
            ein, name, kept, skipped, anthem.file_include_patterns,
        )


def discover_all(client: httpx.Client, cfg: Config) -> list[SourceFile]:
    sources: list[SourceFile] = []
    if cfg.payers.blue_kc.enabled:
        sources.extend(discover_blue_kc(client, cfg))
    if cfg.payers.anthem_mo.enabled:
        sources.extend(discover_anthem(client, cfg))
    return sources
