"""Resolve the target NPI/TIN set from the NPPES API (+ explicit config list).

NPPES API notes:
- `taxonomy_description` accepts description text only (a raw taxonomy code is
  rejected with error 14), so we query by description and post-filter results
  against the exact taxonomy codes/prefixes in config.
- Pagination is `limit` (max 200) / `skip` (max 1000): a single distinct query
  can return at most 1,200 rows. When a query hits that ceiling we retry it
  split by city (from config `cities`, else cities observed so far) and emit a
  loud truncation warning recommending the NPPES bulk file (see README).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import httpx
import pyarrow as pa
import pyarrow.parquet as pq

from .config import Config, TaxonomyTarget
from .http_util import get_with_retry

log = logging.getLogger(__name__)

NPPES_API = "https://npiregistry.cms.hhs.gov/api/"
PAGE_LIMIT = 200
MAX_SKIP = 1000  # API rejects skip > 1000
QUERY_CEILING = MAX_SKIP + PAGE_LIMIT  # 1,200


@dataclass(frozen=True)
class TargetProvider:
    npi: str
    tin: str | None
    org_name: str


@dataclass
class TargetSet:
    """Fast-lookup view of the target providers."""

    providers: list[TargetProvider]

    def __post_init__(self) -> None:
        self.npi_to_org: dict[str, str] = {}
        self.tin_to_org: dict[str, str] = {}
        for p in self.providers:
            self.npi_to_org.setdefault(p.npi, p.org_name)
            if p.tin:
                self.tin_to_org.setdefault(p.tin, p.org_name)
        self.npis: frozenset[str] = frozenset(self.npi_to_org)
        self.tins: frozenset[str] = frozenset(self.tin_to_org)


def _query_page(
    client: httpx.Client,
    cfg: Config,
    description: str,
    city: str | None,
    skip: int,
    enumeration_type: str = "NPI-2",
) -> dict:
    params = {
        "version": "2.1",
        "enumeration_type": enumeration_type,
        "state": cfg.npi_targets.state,
        "taxonomy_description": description,
        "limit": PAGE_LIMIT,
        "skip": skip,
    }
    if city:
        params["city"] = city
    url = httpx.URL(NPPES_API, params=params)
    resp = get_with_retry(client, cfg.http, str(url))
    data = resp.json()
    if "Errors" in data:
        raise RuntimeError(f"NPPES error for {url}: {data['Errors']}")
    return data


def _run_query(
    client: httpx.Client,
    cfg: Config,
    description: str,
    city: str | None,
    enumeration_type: str = "NPI-2",
) -> tuple[list[dict], bool]:
    """Paginate one distinct NPPES query. Returns (results, truncated)."""
    results: list[dict] = []
    skip = 0
    while True:
        data = _query_page(client, cfg, description, city, skip, enumeration_type)
        page = data.get("results", [])
        results.extend(page)
        if len(page) < PAGE_LIMIT:
            return results, False
        skip += PAGE_LIMIT
        if skip > MAX_SKIP:
            # Last page was full and we can't paginate further.
            return results, True


def _org_matches(result: dict, taxonomies: list[TaxonomyTarget]) -> bool:
    for tax in result.get("taxonomies", []):
        code = tax.get("code", "")
        if any(t.matches(code) for t in taxonomies):
            return True
    return False


def _to_provider(result: dict) -> TargetProvider:
    basic = result.get("basic", {})
    ein = basic.get("ein")
    if ein and not ein.strip().strip("<>").isdigit():
        ein = None  # NPPES redacts most org EINs as "<UNAVAIL>"
    name = basic.get("organization_name") or " ".join(
        p for p in (basic.get("first_name"), basic.get("last_name")) if p
    )
    return TargetProvider(
        npi=str(result["number"]),
        tin=ein,
        org_name=name or "",
    )


def resolve_targets(client: httpx.Client, cfg: Config) -> TargetSet:
    """Query NPPES for every configured taxonomy, post-filter, union explicit list."""
    taxonomies = cfg.npi_targets.taxonomies
    # Distinct API queries: dedupe by search description (several taxonomy
    # targets can share one), crossed with configured cities (or statewide).
    descriptions = sorted({t.search_description for t in taxonomies})
    cities: list[str | None] = list(cfg.npi_targets.cities) or [None]

    by_npi: dict[str, TargetProvider] = {}
    truncated_queries: list[str] = []

    enumeration_types = ["NPI-2"]
    if cfg.npi_targets.include_individuals:
        enumeration_types.append("NPI-1")

    for enum_type in enumeration_types:
        for desc in descriptions:
            for city in cities:
                label = (
                    f"{enum_type} taxonomy_description={desc!r} state={cfg.npi_targets.state}"
                    + (f" city={city!r}" if city else "")
                )
                results, truncated = _run_query(client, cfg, desc, city, enum_type)
                matched = [r for r in results if _org_matches(r, taxonomies)]
                log.info("NPPES %s: %d results, %d after taxonomy filter", label, len(results), len(matched))
                if truncated:
                    truncated_queries.append(label)
                for r in matched:
                    p = _to_provider(r)
                    by_npi.setdefault(p.npi, p)

    if truncated_queries:
        for q in truncated_queries:
            log.warning(
                "NPPES query hit the ~%d-result API ceiling and is likely TRUNCATED: %s. "
                "Add `cities` to npi_targets to split the query, or switch to the NPPES "
                "bulk Data Dissemination file (see README).",
                QUERY_CEILING,
                q,
            )

    if len(by_npi) > QUERY_CEILING * 0.8 * len(descriptions):
        log.warning(
            "Target set (%d orgs) is approaching NPPES API pagination limits; "
            "consider the bulk-file resolver described in the README.",
            len(by_npi),
        )

    # Union hand-curated providers from config (they win on conflicts so a
    # curated org_name/tin overrides NPPES).
    for ep in cfg.npi_targets.explicit_providers:
        by_npi[ep.npi] = TargetProvider(
            npi=ep.npi, tin=ep.tin, org_name=ep.org_name or by_npi.get(ep.npi, TargetProvider(ep.npi, None, "")).org_name
        )

    providers = sorted(by_npi.values(), key=lambda p: p.npi)
    log.info("target set: %d NPIs, %d with TINs", len(providers), sum(1 for p in providers if p.tin))
    return TargetSet(providers)


TARGET_SCHEMA = pa.schema(
    [
        pa.field("npi", pa.string()),
        pa.field("tin", pa.string()),
        pa.field("org_name", pa.string()),
    ]
)


def save_targets(targets: TargetSet, raw_dir: Path) -> Path:
    raw_dir.mkdir(parents=True, exist_ok=True)
    path = raw_dir / "target_npis.parquet"
    table = pa.Table.from_pylist(
        [{"npi": p.npi, "tin": p.tin, "org_name": p.org_name} for p in targets.providers],
        schema=TARGET_SCHEMA,
    )
    pq.write_table(table, path)
    return path


def load_targets(raw_dir: Path) -> TargetSet:
    path = raw_dir / "target_npis.parquet"
    table = pq.read_table(path)
    providers = [
        TargetProvider(npi=r["npi"], tin=r["tin"], org_name=r["org_name"] or "")
        for r in table.to_pylist()
    ]
    return TargetSet(providers)
