import gzip
import json
from pathlib import Path

import pytest

from mrfx.config import MrfxConfig
from mrfx.store import Store

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def cfg(tmp_path) -> MrfxConfig:
    c = MrfxConfig(
        inbox_dir=tmp_path / "inbox",
        processed_dir=tmp_path / "processed",
        failed_dir=tmp_path / "failed",
        store_dir=tmp_path / "store",
        downloads_dir=tmp_path / "downloads",
        entity_map_path=tmp_path / "entity_map.yaml",
        payer_name_map={"Testco Health": "Testco"},
        confirm_over_gb=5.0,
    )
    c.ensure_dirs()
    return c


@pytest.fixture
def store(cfg) -> Store:
    return Store(cfg.store_dir)


def drop(cfg: MrfxConfig, fixture_name: str, gz: bool = False, rename: str | None = None) -> Path:
    src = FIXTURES / fixture_name
    dest = cfg.inbox_dir / (rename or (fixture_name + (".gz" if gz else "")))
    data = src.read_bytes()
    dest.write_bytes(gzip.compress(data) if gz else data)
    return dest


def make_fixture(tmp_path: Path, name: str, obj: dict) -> Path:
    p = tmp_path / name
    p.write_text(json.dumps(obj))
    return p
