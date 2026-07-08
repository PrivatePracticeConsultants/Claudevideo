"""Payer registry (config/payer_registry.yaml) + local overrides.

The registry is guidance-only. `verified: false` entries must never render as
authoritative; a user-confirmed URL is persisted to registry_overrides.yaml
and flips the entry to verified locally.
"""

from __future__ import annotations

import datetime as dt
import logging
from pathlib import Path

import yaml

from .config import MrfxConfig

log = logging.getLogger(__name__)


def _as_list(v) -> list:
    if v is None:
        return []
    return v if isinstance(v, list) else [v]


def _load_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text()) or {}


class Registry:
    def __init__(self, cfg: MrfxConfig):
        self.cfg = cfg
        self.data = _load_yaml(cfg.registry_path)
        self.overrides = _load_yaml(cfg.registry_overrides_path)

    # -- normalization --------------------------------------------------------

    def _apply_override(self, card: dict) -> dict:
        ov = (self.overrides.get("entries") or {}).get(card["key"])
        if ov and ov.get("mrf_url"):
            card = {**card, "mrf_url": ov["mrf_url"], "verified": True,
                    "verified_locally": True, "confirmed_at": ov.get("confirmed_at")}
        return card

    def national(self) -> list[dict]:
        cards = []
        for entry in self.data.get("national_payers", []) or []:
            cards.append(self._apply_override({
                "key": f"national:{entry.get('name')}",
                "name": entry.get("name"),
                "parent": None,
                "mrf_url": entry.get("mrf_url"),
                "verified": bool(entry.get("verified")),
                "verified_locally": False,
                "notes": entry.get("notes"),
            }))
        return cards

    def state(self, code: str) -> list[dict]:
        code = code.upper()
        entry = (self.data.get("bcbs_by_state") or {}).get(code)
        if not entry:
            return []
        licensees = _as_list(entry.get("licensee"))
        parents = _as_list(entry.get("parent"))
        cards = []
        for i, lic in enumerate(licensees):
            # The state's mrf_url/verified flag describes the primary (first)
            # licensee; co-licensees start unverified with their pointer in notes.
            primary = i == 0
            cards.append(self._apply_override({
                "key": f"state:{code}:{lic}",
                "name": lic,
                "parent": parents[i] if i < len(parents) else None,
                "mrf_url": entry.get("mrf_url") if primary else None,
                "verified": bool(entry.get("verified")) and primary,
                "verified_locally": False,
                "notes": entry.get("notes"),
            }))
        return cards

    def states(self) -> list[str]:
        return sorted((self.data.get("bcbs_by_state") or {}).keys())

    def elevance_note(self) -> str | None:
        pattern = self.data.get("elevance_master_index_pattern")
        if not pattern:
            return None
        return (
            "Elevance/Anthem publishes one national master index covering all its "
            f"states at once ({pattern}); there is no per-state Anthem download."
        )

    # -- overrides -------------------------------------------------------------

    def save_override(self, key: str, mrf_url: str) -> dict:
        path = self.cfg.registry_overrides_path
        data = _load_yaml(path)
        entries = data.setdefault("entries", {})
        entries[key] = {
            "mrf_url": mrf_url,
            "confirmed_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(yaml.safe_dump(data, sort_keys=True))
        self.overrides = data
        return entries[key]
