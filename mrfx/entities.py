"""User-defined entity map: TIN(s) -> named entity (config/entity_map.yaml).

Real practices bill under multiple TINs (legacy TINs post-roll-up, pro/facility
splits, per-state entities). The map adds an *Entity* grain above TIN; unmapped
TINs stand alone as their own entities. Edits from the UI persist back to YAML.
"""

from __future__ import annotations

import logging
import re
import threading
from pathlib import Path

import yaml

from .config import MrfxConfig
from .store import Store

log = logging.getLogger(__name__)


def load_entity_map(path: Path) -> dict[str, str]:
    """YAML -> {tin: entity_name}. Duplicate TINs keep the first entity."""
    if not Path(path).exists():
        return {}
    try:
        data = yaml.safe_load(Path(path).read_text()) or {}
    except yaml.YAMLError as e:
        log.warning("entity map %s is not valid YAML (%s) — ignoring it; fix the "
                    "file and restart to get your groupings back", path, e)
        return {}
    if not isinstance(data, dict):
        log.warning("entity map %s: top level must be a mapping — ignoring it", path)
        return {}
    mapping: dict[str, str] = {}
    entries = data.get("entities") or []
    if not isinstance(entries, list):
        log.warning("entity map %s: 'entities' must be a list — ignoring it", path)
        return {}
    for entity in entries:
        # hand-edited files arrive with every shape; a bad entry must cost
        # only itself, never kill server startup (sync_entity_map runs there)
        if not isinstance(entity, dict):
            log.warning("entity map %s: skipping non-mapping entry %r", path, entity)
            continue
        name = str(entity.get("name", "")).strip()
        if not name:
            continue
        tins = entity.get("tins") or []
        if isinstance(tins, (str, int)):
            tins = [tins]  # a single scalar means ONE tin, not its characters
        if not isinstance(tins, list):
            log.warning("entity map %s: entry %r has unusable tins %r — skipped",
                        path, name, tins)
            continue
        for tin in tins:
            tin = str(tin).replace("-", "").strip()
            if tin and tin not in mapping:
                mapping[tin] = name
    return mapping


def save_entity_map(path: Path, mapping: dict[str, str]) -> None:
    """{tin: entity_name} -> YAML (grouped back into entities). Written via
    temp+rename: a crash mid-write must never leave a truncated file that the
    next edit would 'repair' by persisting a near-empty map over the user's
    groupings."""
    by_name: dict[str, list[str]] = {}
    for tin, name in sorted(mapping.items()):
        by_name.setdefault(name, []).append(tin)
    data = {"entities": [{"name": n, "tins": tins} for n, tins in sorted(by_name.items())]}
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(yaml.safe_dump(data, sort_keys=False))
    tmp.replace(path)


def sync_entity_map(cfg: MrfxConfig, store: Store) -> dict[str, str]:
    """Load YAML into the store's entity_map table. Returns the mapping."""
    mapping = load_entity_map(cfg.entity_map_path)
    store.set_entity_map(mapping)
    return mapping


def _tin_list(v) -> list:
    """A scalar means ONE tin — iterating a bare string here would map each
    CHARACTER to the entity and persist that junk over the user's YAML.
    (Floats/dicts arrive from hand-crafted API calls too: any non-sequence
    is ONE value, never iterated.)"""
    if v is None:
        return []
    if isinstance(v, (list, tuple, set)):
        return list(v)
    return [v]


# API edits are read-modify-write over one YAML file: without this lock two
# concurrent UI edits load the same snapshot and the loser's TINs vanish
_EDIT_LOCK = threading.Lock()


def update_entity(cfg: MrfxConfig, store: Store, entity_name: str,
                  add_tins: list[str] | None = None,
                  remove_tins: list[str] | None = None) -> dict[str, str]:
    """UI edit: add/remove TINs for an entity; persists to YAML + store."""
    with _EDIT_LOCK:
        mapping = load_entity_map(cfg.entity_map_path)
        for tin in _tin_list(add_tins):
            tin = re.sub(r"\D", "", str(tin))  # digits only ('12-3', '123.0' artifacts)
            if tin:
                mapping[tin] = entity_name
        for tin in _tin_list(remove_tins):
            tin = re.sub(r"\D", "", str(tin))
            if mapping.get(tin) == entity_name:
                del mapping[tin]
        save_entity_map(cfg.entity_map_path, mapping)
        store.set_entity_map(mapping)
        return mapping
