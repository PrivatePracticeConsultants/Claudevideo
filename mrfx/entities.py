"""User-defined entity map: TIN(s) -> named entity (config/entity_map.yaml).

Real practices bill under multiple TINs (legacy TINs post-roll-up, pro/facility
splits, per-state entities). The map adds an *Entity* grain above TIN; unmapped
TINs stand alone as their own entities. Edits from the UI persist back to YAML.
"""

from __future__ import annotations

import logging
from pathlib import Path

import yaml

from .config import MrfxConfig
from .store import Store

log = logging.getLogger(__name__)


def load_entity_map(path: Path) -> dict[str, str]:
    """YAML -> {tin: entity_name}. Duplicate TINs keep the first entity."""
    if not Path(path).exists():
        return {}
    data = yaml.safe_load(Path(path).read_text()) or {}
    mapping: dict[str, str] = {}
    for entity in data.get("entities", []) or []:
        name = str(entity.get("name", "")).strip()
        if not name:
            continue
        for tin in entity.get("tins", []) or []:
            tin = str(tin).replace("-", "").strip()
            if tin and tin not in mapping:
                mapping[tin] = name
    return mapping


def save_entity_map(path: Path, mapping: dict[str, str]) -> None:
    """{tin: entity_name} -> YAML (grouped back into entities)."""
    by_name: dict[str, list[str]] = {}
    for tin, name in sorted(mapping.items()):
        by_name.setdefault(name, []).append(tin)
    data = {"entities": [{"name": n, "tins": tins} for n, tins in sorted(by_name.items())]}
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(yaml.safe_dump(data, sort_keys=False))


def sync_entity_map(cfg: MrfxConfig, store: Store) -> dict[str, str]:
    """Load YAML into the store's entity_map table. Returns the mapping."""
    mapping = load_entity_map(cfg.entity_map_path)
    store.set_entity_map(mapping)
    return mapping


def update_entity(cfg: MrfxConfig, store: Store, entity_name: str,
                  add_tins: list[str] | None = None,
                  remove_tins: list[str] | None = None) -> dict[str, str]:
    """UI edit: add/remove TINs for an entity; persists to YAML + store."""
    mapping = load_entity_map(cfg.entity_map_path)
    for tin in add_tins or []:
        mapping[str(tin).replace("-", "").strip()] = entity_name
    for tin in remove_tins or []:
        tin = str(tin).replace("-", "").strip()
        if mapping.get(tin) == entity_name:
            del mapping[tin]
    save_entity_map(cfg.entity_map_path, mapping)
    store.set_entity_map(mapping)
    return mapping
