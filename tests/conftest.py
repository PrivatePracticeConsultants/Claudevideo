from pathlib import Path

import pytest

from src.config import Config
from src.npi_resolver import TargetProvider, TargetSet

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def targets() -> TargetSet:
    return TargetSet(
        [
            TargetProvider(npi="1111111111", tin="111111111", org_name="Ref PT Group"),
            TargetProvider(npi="2222222222", tin="222222222", org_name="Inline PT Group"),
        ]
    )


@pytest.fixture
def cfg(tmp_path) -> Config:
    return Config.model_validate(
        {
            "npi_targets": {
                "state": "MO",
                "taxonomies": [{"code": "225100000X", "search_description": "Physical Therapist"}],
            },
            "billing_codes": ["97110", "G0283"],
            "paths": {"raw_dir": str(tmp_path / "raw"), "out_dir": str(tmp_path / "out")},
        }
    )


# `pytest -m quick`: everything except the tests listed in slow_tests.txt
# (measured >= 0.8 s each). The default `pytest tests/` still runs them all.
_SLOW = {line.strip() for line in (Path(__file__).parent / "slow_tests.txt").read_text().splitlines()
         if line.strip() and not line.startswith("#")}


def pytest_configure(config):
    config.addinivalue_line("markers", "quick: fast inner-loop subset (see tests/slow_tests.txt)")


def pytest_collection_modifyitems(config, items):
    for item in items:
        if item.nodeid not in _SLOW:
            item.add_marker(pytest.mark.quick)
