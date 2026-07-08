"""Resolve TiC `provider_references` IDs to NPI/TIN sets.

Handles both v2.0 patterns:
- inline: top-level `provider_references[]` entries carrying `provider_groups`
- remote: entries carrying only a `location` URL to a small provider-group
  JSON file (fetched lazily, cached in-memory per extraction run)

The extractor also accepts extra provider-reference files declared at the TOC
level via `load_url()`.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

import httpx

from .config import HttpConfig
from .http_util import FetchError, get_with_retry
from .npi_resolver import TargetSet

log = logging.getLogger(__name__)


@dataclass
class RefGroup:
    npis: frozenset[str]
    tins: frozenset[str]


def _parse_provider_groups(provider_groups: list[dict]) -> RefGroup:
    npis: set[str] = set()
    tins: set[str] = set()
    for pg in provider_groups or []:
        for npi in pg.get("npi", []) or []:
            npis.add(str(npi))
        tin = pg.get("tin") or {}
        value = str(tin.get("value", "")).replace("-", "")
        # `tin.type` is 'ein' or 'npi'; an npi-type tin is not an employer id.
        if value and tin.get("type") == "ein":
            tins.add(value)
    return RefGroup(frozenset(npis), frozenset(tins))


@dataclass
class ProviderRefIndex:
    """reference_id -> RefGroup, with lazy fetch for `location`-style entries."""

    client: httpx.Client | None = None
    http_cfg: HttpConfig | None = None
    resolved: dict[int, RefGroup] = field(default_factory=dict)
    pending_locations: dict[int, str] = field(default_factory=dict)
    complete: bool = False  # set once the file's provider_references array has fully streamed by

    def add_entry(self, entry: dict) -> None:
        """Ingest one `provider_references[]` element."""
        ref_id = entry.get("provider_group_id")
        if ref_id is None:
            log.warning("provider_reference entry without provider_group_id: %.200s", entry)
            return
        ref_id = int(ref_id)
        if "provider_groups" in entry and entry["provider_groups"] is not None:
            self.resolved[ref_id] = _parse_provider_groups(entry["provider_groups"])
        elif entry.get("location"):
            self.pending_locations[ref_id] = entry["location"]
        else:
            self.resolved[ref_id] = RefGroup(frozenset(), frozenset())

    def load_url(self, url: str) -> None:
        """Load a whole provider-reference file declared at the TOC level."""
        assert self.client is not None and self.http_cfg is not None
        resp = get_with_retry(self.client, self.http_cfg, url)
        data = resp.json()
        entries = data.get("provider_references", data if isinstance(data, list) else [])
        for entry in entries:
            self.add_entry(entry)

    def _fetch_location(self, ref_id: int) -> RefGroup:
        url = self.pending_locations.pop(ref_id)
        empty = RefGroup(frozenset(), frozenset())
        if self.client is None or self.http_cfg is None:
            log.warning("no HTTP client to fetch provider_reference location %s", url)
            self.resolved[ref_id] = empty
            return empty
        try:
            resp = get_with_retry(self.client, self.http_cfg, url)
            data = json.loads(resp.content)
        except (FetchError, json.JSONDecodeError) as e:
            log.warning("provider_reference location %s failed (%s); treating as empty", url, e)
            self.resolved[ref_id] = empty
            return empty
        group = _parse_provider_groups(data.get("provider_groups", []))
        self.resolved[ref_id] = group
        return group

    def get(self, ref_id: int) -> RefGroup | None:
        """RefGroup for an id, fetching a remote location if needed.

        Returns None when the id is unknown *and* the reference array hasn't
        finished streaming yet (caller should defer); an unknown id after
        completion resolves to an empty group.
        """
        ref_id = int(ref_id)
        if ref_id in self.resolved:
            return self.resolved[ref_id]
        if ref_id in self.pending_locations:
            return self._fetch_location(ref_id)
        if not self.complete:
            return None
        log.debug("provider_reference id %s not declared in file", ref_id)
        return RefGroup(frozenset(), frozenset())

    def matches_targets(self, ref_id: int, targets: TargetSet) -> bool:
        group = self.get(ref_id)
        return group is not None and bool(group.npis & targets.npis or group.tins & targets.tins)
